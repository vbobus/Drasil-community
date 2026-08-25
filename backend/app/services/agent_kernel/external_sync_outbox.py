"""Database-claimed, retry-safe delivery for native external calendar effects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal
from uuid import uuid4

from sqlalchemy import exists, or_, update
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.models.agent_execution import (
    AgentChangeSetExecutionRecord,
    AgentExternalSyncOutboxRecord,
)


MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 3600
CLAIM_BATCH_SIZE = 32


@dataclass(frozen=True)
class ExternalDeliveryResult:
    status: Literal[
        "succeeded", "retryable_failure", "permanent_failure", "not_configured"
    ]
    remote_id: str | None = None
    remote_version: str | None = None
    error_code: str | None = None
    error_summary: str | None = None


Deliverer = Callable[[AgentExternalSyncOutboxRecord], ExternalDeliveryResult]


class ExternalSyncOutboxError(RuntimeError):
    pass


class ExternalSyncOutboxService:
    def __init__(
        self,
        session: Session,
        *,
        deliverer: Deliverer | None = None,
        clock: Callable[[], datetime] = datetime.utcnow,
    ):
        self.session = session
        self.deliverer = deliverer or self._deliver_google
        self.clock = clock

    def _deliver_google(
        self, row: AgentExternalSyncOutboxRecord
    ) -> ExternalDeliveryResult:
        from app.services.google_calendar import deliver_google_outbox_effect

        return deliver_google_outbox_effect(self.session, row)

    @staticmethod
    def _claim_id(worker_token: str) -> str:
        worker_hash = hashlib.sha256(worker_token.encode("utf-8")).hexdigest()[:16]
        return f"claim:{worker_hash}:{uuid4().hex}"

    @staticmethod
    def _validate_claim_args(worker_token: str, lease_seconds: int) -> None:
        if not worker_token.strip():
            raise ExternalSyncOutboxError("worker_token must not be empty")
        if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ExternalSyncOutboxError(
                f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
            )

    def _assert_dedicated_session(self) -> None:
        if self.session.new or self.session.dirty or self.session.deleted:
            raise ExternalSyncOutboxError(
                "outbox delivery requires a dedicated Session without unrelated pending writes"
            )

    def claim_next(
        self,
        *,
        worker_token: str,
        now: datetime | None = None,
        lease_seconds: int = 90,
    ) -> AgentExternalSyncOutboxRecord | None:
        self._validate_claim_args(worker_token, lease_seconds)
        self._assert_dedicated_session()
        current = now or self.clock()
        predecessor = aliased(AgentExternalSyncOutboxRecord)
        has_nonterminal_predecessor = exists(
            select(predecessor.outbox_id).where(
                predecessor.provider == AgentExternalSyncOutboxRecord.provider,
                predecessor.entity_kind == AgentExternalSyncOutboxRecord.entity_kind,
                predecessor.entity_id == AgentExternalSyncOutboxRecord.entity_id,
                predecessor.status.in_(["pending", "not_configured", "delivering"]),
                or_(
                    (
                        predecessor.execution_id
                        == AgentExternalSyncOutboxRecord.execution_id
                    )
                    & or_(
                        predecessor.effect_ordinal
                        < AgentExternalSyncOutboxRecord.effect_ordinal,
                        (
                            predecessor.effect_ordinal
                            == AgentExternalSyncOutboxRecord.effect_ordinal
                        )
                        & or_(
                            predecessor.created_at
                            < AgentExternalSyncOutboxRecord.created_at,
                            (
                                predecessor.created_at
                                == AgentExternalSyncOutboxRecord.created_at
                            )
                            & (
                                predecessor.outbox_id
                                < AgentExternalSyncOutboxRecord.outbox_id
                            ),
                        ),
                    ),
                    (
                        predecessor.execution_id
                        != AgentExternalSyncOutboxRecord.execution_id
                    )
                    & or_(
                        predecessor.created_at
                        < AgentExternalSyncOutboxRecord.created_at,
                        (
                            predecessor.created_at
                            == AgentExternalSyncOutboxRecord.created_at
                        )
                        & (
                            predecessor.outbox_id
                            < AgentExternalSyncOutboxRecord.outbox_id
                        ),
                    ),
                ),
            )
        )
        candidates = self.session.exec(
            select(AgentExternalSyncOutboxRecord)
            .join(
                AgentChangeSetExecutionRecord,
                AgentChangeSetExecutionRecord.execution_id
                == AgentExternalSyncOutboxRecord.execution_id,
            )
            .where(
                AgentChangeSetExecutionRecord.status == "applied",
                ~has_nonterminal_predecessor,
                or_(
                    (
                        AgentExternalSyncOutboxRecord.status == "pending"
                    )
                    & or_(
                        AgentExternalSyncOutboxRecord.next_attempt_at == None,  # noqa: E711
                        AgentExternalSyncOutboxRecord.next_attempt_at <= current,
                    ),
                    (
                        AgentExternalSyncOutboxRecord.status == "delivering"
                    )
                    & (AgentExternalSyncOutboxRecord.lease_expires_at <= current),
                ),
            )
            .order_by(
                AgentExternalSyncOutboxRecord.next_attempt_at,
                AgentExternalSyncOutboxRecord.created_at,
                AgentExternalSyncOutboxRecord.effect_ordinal,
                AgentExternalSyncOutboxRecord.outbox_id,
            )
            .limit(CLAIM_BATCH_SIZE)
        ).all()
        for candidate in candidates:
            claim_id = self._claim_id(worker_token)
            conditions = [
                AgentExternalSyncOutboxRecord.outbox_id == candidate.outbox_id,
                AgentExternalSyncOutboxRecord.status == candidate.status,
                AgentExternalSyncOutboxRecord.attempts == candidate.attempts,
            ]
            if candidate.status == "delivering":
                conditions.extend(
                    [
                        AgentExternalSyncOutboxRecord.claim_token
                        == candidate.claim_token,
                        AgentExternalSyncOutboxRecord.lease_expires_at
                        == candidate.lease_expires_at,
                        AgentExternalSyncOutboxRecord.lease_expires_at <= current,
                    ]
                )
            elif candidate.next_attempt_at is None:
                conditions.append(
                    AgentExternalSyncOutboxRecord.next_attempt_at == None  # noqa: E711
                )
            else:
                conditions.extend(
                    [
                        AgentExternalSyncOutboxRecord.next_attempt_at
                        == candidate.next_attempt_at,
                        AgentExternalSyncOutboxRecord.next_attempt_at <= current,
                    ]
                )
            result = self.session.exec(
                update(AgentExternalSyncOutboxRecord)
                .where(*conditions)
                .values(
                    status="delivering",
                    claim_token=claim_id,
                    lease_expires_at=current + timedelta(seconds=lease_seconds),
                    attempts=candidate.attempts + 1,
                    updated_at=current,
                )
            )
            if result.rowcount == 1:  # type: ignore[attr-defined]
                self.session.commit()
                self.session.expire_all()
                return self.session.get(
                    AgentExternalSyncOutboxRecord,
                    candidate.outbox_id,
                )
            self.session.rollback()
        return None

    @staticmethod
    def _retry_delay_seconds(row: AgentExternalSyncOutboxRecord) -> int:
        base = min(3600, 2 ** min(int(row.attempts), 12))
        if base >= 3600:
            return 3600
        digest = hashlib.sha256(
            f"{row.outbox_id}:{row.attempts}".encode("utf-8")
        ).digest()
        jitter = int.from_bytes(digest[:2], "big") % max(1, base // 4 + 1)
        return min(3600, base + jitter)

    def _finalize_claim(
        self,
        row: AgentExternalSyncOutboxRecord,
        *,
        claim_id: str,
        outcome: ExternalDeliveryResult,
        completed_at: datetime,
    ) -> bool:
        values: dict[str, object] = {
            "claim_token": None,
            "lease_expires_at": None,
            "updated_at": completed_at,
        }
        if outcome.status == "succeeded":
            values.update(
                status="succeeded",
                remote_id=outcome.remote_id or row.remote_id,
                remote_version=outcome.remote_version or row.remote_version,
                last_error_code=None,
                last_error_summary=None,
                next_attempt_at=None,
            )
        elif outcome.status == "permanent_failure":
            values.update(
                status="failed",
                last_error_code=outcome.error_code or "SYNC_PERMANENT",
                last_error_summary=(
                    outcome.error_summary or "External calendar delivery failed."
                )[:500],
                next_attempt_at=None,
            )
        elif outcome.status == "not_configured":
            values.update(
                status="not_configured",
                last_error_code=outcome.error_code or "SYNC_NOT_CONFIGURED",
                last_error_summary=(
                    outcome.error_summary
                    or "Connect the external calendar to deliver this change."
                )[:500],
                next_attempt_at=None,
            )
        else:
            delay = self._retry_delay_seconds(row)
            values.update(
                status="pending",
                last_error_code=outcome.error_code or "SYNC_RETRYABLE",
                last_error_summary=(
                    outcome.error_summary
                    or "External calendar delivery will be retried."
                )[:500],
                next_attempt_at=completed_at + timedelta(seconds=delay),
            )
        result = self.session.exec(
            update(AgentExternalSyncOutboxRecord)
            .where(
                AgentExternalSyncOutboxRecord.outbox_id == row.outbox_id,
                AgentExternalSyncOutboxRecord.status == "delivering",
                AgentExternalSyncOutboxRecord.claim_token == claim_id,
            )
            .values(**values)
        )
        return result.rowcount == 1  # type: ignore[attr-defined]

    def deliver_next(
        self, *, worker_token: str, now: datetime | None = None
    ) -> AgentExternalSyncOutboxRecord | None:
        claim_time = now or self.clock()
        row = self.claim_next(worker_token=worker_token, now=claim_time)
        if row is None:
            return None
        claim_id = str(row.claim_token or "")
        savepoint = self.session.begin_nested()
        try:
            outcome = self.deliverer(row)
            if outcome.status not in {
                "succeeded",
                "retryable_failure",
                "permanent_failure",
                "not_configured",
            }:
                outcome = ExternalDeliveryResult(
                    status="permanent_failure",
                    error_code="SYNC_INVALID_RESULT",
                    error_summary="External calendar delivery returned an invalid result.",
                )
            completed_at = max(claim_time, self.clock())
            if not self._finalize_claim(
                row,
                claim_id=claim_id,
                outcome=outcome,
                completed_at=completed_at,
            ):
                savepoint.rollback()
                self.session.rollback()
                return None
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            self.session.rollback()
            completed_at = max(claim_time, self.clock())
            outcome = ExternalDeliveryResult(
                status="retryable_failure",
                error_code="SYNC_RETRYABLE",
                error_summary="External calendar delivery failed and will be retried.",
            )
            if not self._finalize_claim(
                row,
                claim_id=claim_id,
                outcome=outcome,
                completed_at=completed_at,
            ):
                self.session.rollback()
                return None
        self.session.commit()
        self.session.expire_all()
        return self.session.get(AgentExternalSyncOutboxRecord, row.outbox_id)

    def list_for_execution(self, execution_id: str) -> list[AgentExternalSyncOutboxRecord]:
        return list(
            self.session.exec(
                select(AgentExternalSyncOutboxRecord)
                .where(AgentExternalSyncOutboxRecord.execution_id == execution_id)
                .order_by(AgentExternalSyncOutboxRecord.created_at)
            ).all()
        )
