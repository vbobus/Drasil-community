from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import update
from sqlmodel import Session, SQLModel, create_engine

from app.models.agent_execution import (
    AgentChangeSetExecutionRecord,
    AgentExternalSyncOutboxRecord,
)
from app.models.calendar_block import CalendarBlock
from app.models.calendar_integration import CalendarIntegration
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.external_sync_outbox import (
    ExternalDeliveryResult,
    ExternalSyncOutboxError,
    ExternalSyncOutboxService,
)
from app.services.agent_kernel.state_store import GoalStateStore, GoalStateStoreError
from app.services.google_calendar import (
    GOOGLE_CALENDAR_API_BASE,
    GOOGLE_TOKEN_URL,
    GoogleCalendarError,
    deterministic_google_event_id,
    wake_google_outbox_deliveries,
)


class AgentExternalSyncOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        database_path = Path(self.directory.name) / "outbox.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(
                AgentChangeSetExecutionRecord(
                    execution_id="execution:1",
                    session_id="session:1",
                    command_id="approve:1",
                    idempotency_key="apply:1",
                    goal_id="goal:1",
                    goal_revision=6,
                    change_set_id="changeset:1",
                    change_set_revision=1,
                    simulation_id="simulation:1",
                    payload_fingerprint="a" * 64,
                    status="applied",
                    intended_fingerprint="b" * 64,
                    projection_json="[]",
                    result_json="{}",
                )
            )
            session.add(Category(id=1, name="Work"))
            session.add(StrategyNode(id=1, category_id=1, title="Current title"))
            session.add(
                CalendarBlock(
                    id=1,
                    task_id=1,
                    category_id=1,
                    start_at=datetime(2026, 7, 20, 10),
                    end_at=datetime(2026, 7, 20, 11),
                )
            )
            session.add(
                CalendarIntegration(
                    provider="google",
                    is_connected=True,
                    google_calendar_id="calendar:1",
                    access_token="token",
                    refresh_token="refresh",
                    expires_at=datetime.utcnow() + timedelta(hours=1),
                )
            )
            session.add(self._outbox())
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    @staticmethod
    def _event(summary: str = "Approved title") -> dict[str, object]:
        return {
            "summary": summary,
            "description": "Approved snapshot",
            "start": {"dateTime": "2026-07-20T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-07-20T11:00:00", "timeZone": "UTC"},
            "extendedProperties": {
                "private": {
                    "drasilBlockId": "1",
                    "drasilTaskId": "1",
                    "drasilCategoryId": "1",
                }
            },
        }

    @classmethod
    def _outbox(cls, **updates: object) -> AgentExternalSyncOutboxRecord:
        values: dict[str, object] = {
            "outbox_id": "outbox:1",
            "execution_id": "execution:1",
            "session_id": "session:1",
            "change_set_id": "changeset:1",
            "change_set_revision": 1,
            "provider": "google",
            "operation": "create",
            "entity_kind": "calendar_block",
            "entity_id": "calendar_block:1",
            "semantic_key": "google:create:calendar_block:1:changeset:1:1:0",
            "payload_json": json.dumps(
                {"event": cls._event(), "remote_id": None, "local_revision": 1}
            ),
        }
        values.update(updates)
        return AgentExternalSyncOutboxRecord(**values)

    def test_deterministic_google_id_is_base32hex_and_stable(self) -> None:
        first = deterministic_google_event_id("google:create:block:1")
        second = deterministic_google_event_id("google:create:block:1")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[a-v0-9]+$")

    def test_claim_requires_verified_applied_execution(self) -> None:
        now = datetime(2026, 7, 20, 9)
        with Session(self.engine) as session:
            execution = session.get(AgentChangeSetExecutionRecord, "execution:1")
            execution.status = "committed_pending_verification"
            session.add(execution)
            session.commit()
            service = ExternalSyncOutboxService(session)
            self.assertIsNone(service.claim_next(worker_token="worker:1", now=now))
            execution.status = "applied"
            session.add(execution)
            session.commit()
            self.assertIsNotNone(
                service.claim_next(worker_token="worker:1", now=now)
            )

    def test_claim_success_and_exact_replay_do_not_redeliver(self) -> None:
        calls: list[str] = []

        def deliver(row: AgentExternalSyncOutboxRecord) -> ExternalDeliveryResult:
            calls.append(row.outbox_id)
            return ExternalDeliveryResult(
                status="succeeded",
                remote_id="event:1",
                remote_version="etag:1",
            )

        with Session(self.engine) as session:
            service = ExternalSyncOutboxService(session, deliverer=deliver)
            first = service.deliver_next(worker_token="worker:1")
            second = service.deliver_next(worker_token="worker:1")
            self.assertEqual(first.status, "succeeded")
            self.assertIsNone(second)
            self.assertEqual(calls, ["outbox:1"])

    def test_stale_worker_cannot_finalize_after_lease_reclaim(self) -> None:
        start = datetime(2026, 7, 20, 9)
        with Session(self.engine) as first_session:
            first_service = ExternalSyncOutboxService(first_session)
            first_claim = first_service.claim_next(
                worker_token="worker:shared",
                now=start,
            )
            first_token = str(first_claim.claim_token)

        with Session(self.engine) as second_session:
            second_claim = ExternalSyncOutboxService(second_session).claim_next(
                worker_token="worker:shared",
                now=start + timedelta(seconds=91),
            )
            self.assertNotEqual(first_token, second_claim.claim_token)

        with Session(self.engine) as stale_session:
            stale_service = ExternalSyncOutboxService(stale_session)
            self.assertFalse(
                stale_service._finalize_claim(
                    first_claim,
                    claim_id=first_token,
                    outcome=ExternalDeliveryResult(status="succeeded"),
                    completed_at=start + timedelta(seconds=92),
                )
            )
            stale_session.rollback()

    def test_later_effect_waits_for_nonterminal_predecessor(self) -> None:
        now = datetime(2026, 7, 20, 9)
        with Session(self.engine) as session:
            session.add(
                self._outbox(
                    outbox_id="outbox:2",
                    operation="update",
                    effect_ordinal=1,
                    semantic_key="google:update:calendar_block:1:changeset:1:1:1",
                    created_at=now + timedelta(seconds=1),
                )
            )
            session.commit()
            first = ExternalSyncOutboxService(session).claim_next(
                worker_token="worker:1",
                now=now + timedelta(seconds=2),
            )
            self.assertEqual(first.outbox_id, "outbox:1")
            self.assertIsNone(
                ExternalSyncOutboxService(session).claim_next(
                    worker_token="worker:2",
                    now=now + timedelta(seconds=2),
                )
            )
            service = ExternalSyncOutboxService(session)
            self.assertTrue(
                service._finalize_claim(
                    first,
                    claim_id=str(first.claim_token),
                    outcome=ExternalDeliveryResult(
                        status="succeeded", remote_id="event:1"
                    ),
                    completed_at=now + timedelta(seconds=3),
                )
            )
            session.commit()
            second = service.claim_next(
                worker_token="worker:2",
                now=now + timedelta(seconds=4),
            )
            self.assertEqual(second.outbox_id, "outbox:2")

    def test_provider_uses_immutable_event_snapshot_not_live_task(self) -> None:
        with Session(self.engine) as session:
            task = session.get(StrategyNode, 1)
            task.title = "Changed after approval"
            session.add(task)
            session.commit()
            bodies: list[dict[str, object]] = []

            def request(_method: str, _url: str, **kwargs: object) -> dict[str, str]:
                bodies.append(dict(kwargs.get("body") or {}))
                return {"id": "event:1", "etag": "etag:1"}

            with patch("app.services.google_calendar._google_request", side_effect=request):
                result = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:1"
                )
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(bodies[0]["summary"], "Approved title")

    def test_response_loss_retry_reuses_deterministic_create_identity(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        call_index = 0

        def request(method: str, _url: str, **kwargs: object) -> dict[str, str]:
            nonlocal call_index
            body = dict(kwargs.get("body") or {})
            calls.append((method, body))
            call_index += 1
            if call_index == 1:
                raise GoogleCalendarError("response lost")
            if call_index == 2:
                raise GoogleCalendarError("already exists", status_code=409)
            return {"id": deterministic_google_event_id(self._outbox().semantic_key), "etag": "etag:1"}

        with Session(self.engine) as session:
            with patch("app.services.google_calendar._google_request", side_effect=request):
                first = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:1"
                )
                self.assertEqual(first.status, "pending")
                first.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
                session.add(first)
                session.commit()
                second = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:2"
                )
            self.assertEqual(second.status, "succeeded")
            expected_id = deterministic_google_event_id(self._outbox().semantic_key)
            self.assertEqual(calls[0][1]["id"], expected_id)
            self.assertEqual(calls[1][1]["id"], expected_id)
            self.assertEqual([item[0] for item in calls], ["POST", "POST", "PATCH"])

    def test_delete_410_is_success_and_update_404_recreates(self) -> None:
        with Session(self.engine) as session:
            outbox = session.get(AgentExternalSyncOutboxRecord, "outbox:1")
            outbox.operation = "delete"
            outbox.semantic_key = "google:delete:calendar_block:1"
            outbox.payload_json = json.dumps({"remote_id": "event:old"})
            session.add(outbox)
            session.commit()
            with patch(
                "app.services.google_calendar._google_request",
                side_effect=GoogleCalendarError("gone", status_code=410),
            ):
                deleted = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:1"
                )
            self.assertEqual(deleted.status, "succeeded")

        with Session(self.engine) as session:
            session.add(
                self._outbox(
                    outbox_id="outbox:2",
                    operation="update",
                    semantic_key="google:update:calendar_block:1:2",
                    payload_json=json.dumps(
                        {"event": self._event("Moved"), "remote_id": "event:old"}
                    ),
                    created_at=datetime.utcnow() + timedelta(seconds=1),
                )
            )
            session.commit()
            methods: list[str] = []

            def request(method: str, _url: str, **_kwargs: object) -> dict[str, str]:
                methods.append(method)
                if method == "PATCH":
                    raise GoogleCalendarError("missing", status_code=404)
                return {"id": "event:new", "etag": "etag:new"}

            with patch("app.services.google_calendar._google_request", side_effect=request):
                recreated = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:2"
                )
            self.assertEqual(recreated.status, "succeeded")
            self.assertEqual(methods, ["PATCH", "POST"])

    def test_invalid_payload_is_terminal_and_not_retried(self) -> None:
        with Session(self.engine) as session:
            outbox = session.get(AgentExternalSyncOutboxRecord, "outbox:1")
            outbox.payload_json = "{"
            session.add(outbox)
            session.commit()
            result = ExternalSyncOutboxService(session).deliver_next(
                worker_token="worker:1"
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.last_error_code, "SYNC_INVALID_PAYLOAD")
            self.assertIsNone(
                ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:2"
                )
            )

    def test_invalid_provider_operation_and_entity_envelopes_are_terminal(self) -> None:
        cases = [
            {"provider": "other"},
            {"operation": "cancel_all"},
            {"entity_kind": "task"},
            {"entity_id": "calendar_block:not-an-int"},
        ]
        for index, updates in enumerate(cases, start=1):
            with self.subTest(updates=updates), Session(self.engine) as session:
                row = session.get(AgentExternalSyncOutboxRecord, "outbox:1")
                row.status = "pending"
                row.claim_token = None
                row.lease_expires_at = None
                row.next_attempt_at = None
                row.provider = "google"
                row.operation = "create"
                row.entity_kind = "calendar_block"
                row.entity_id = "calendar_block:1"
                for key, value in updates.items():
                    setattr(row, key, value)
                row.semantic_key = f"invalid:{index}"
                session.add(row)
                session.commit()
                result = ExternalSyncOutboxService(session).deliver_next(
                    worker_token=f"worker:{index}"
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.last_error_code, "SYNC_INVALID_PAYLOAD")

    def test_missing_refresh_parks_until_reconnect_wakes_row(self) -> None:
        with Session(self.engine) as session:
            integration = session.get(CalendarIntegration, 1)
            integration.access_token = None
            integration.refresh_token = None
            integration.expires_at = None
            session.add(integration)
            session.commit()
            parked = ExternalSyncOutboxService(session).deliver_next(
                worker_token="worker:1"
            )
            self.assertEqual(parked.status, "not_configured")
            self.assertIsNone(parked.next_attempt_at)
            self.assertIsNone(
                ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:2"
                )
            )
            self.assertEqual(wake_google_outbox_deliveries(session), 1)
            session.commit()
            self.assertEqual(
                session.get(AgentExternalSyncOutboxRecord, "outbox:1").status,
                "pending",
            )

    def test_early_401_refreshes_once_and_rate_limit_403_retries(self) -> None:
        with Session(self.engine) as session:
            outbox = session.get(AgentExternalSyncOutboxRecord, "outbox:1")
            outbox.operation = "update"
            outbox.payload_json = json.dumps(
                {"event": self._event(), "remote_id": "event:1"}
            )
            session.add(outbox)
            session.commit()
            event_calls = 0

            def request(method: str, url: str, **_kwargs: object) -> dict[str, object]:
                nonlocal event_calls
                if url == GOOGLE_TOKEN_URL:
                    return {"access_token": "refreshed", "expires_in": 3600}
                event_calls += 1
                if event_calls == 1:
                    raise GoogleCalendarError("expired", status_code=401)
                return {"id": "event:1", "etag": "etag:2"}

            with (
                patch("app.services.google_calendar._google_request", side_effect=request),
                patch(
                    "app.services.google_calendar._read_google_oauth_config",
                    return_value={
                        "client_id": "client",
                        "client_secret": "secret",
                        "redirect_uri": "http://localhost/callback",
                        "time_zone": "UTC",
                    },
                ),
            ):
                refreshed = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:1"
                )
            self.assertEqual(refreshed.status, "succeeded")
            self.assertEqual(event_calls, 2)

        with Session(self.engine) as session:
            session.add(
                self._outbox(
                    outbox_id="outbox:2",
                    semantic_key="google:create:calendar_block:1:2",
                    created_at=datetime.utcnow() + timedelta(seconds=1),
                )
            )
            session.commit()
            with patch(
                "app.services.google_calendar._google_request",
                side_effect=GoogleCalendarError(
                    "quota",
                    status_code=403,
                    reason="userRateLimitExceeded",
                ),
            ):
                limited = ExternalSyncOutboxService(session).deliver_next(
                    worker_token="worker:2"
                )
            self.assertEqual(limited.status, "pending")
            self.assertEqual(limited.last_error_code, "SYNC_RETRYABLE")

    def test_provider_exception_rolls_back_partial_local_writes(self) -> None:
        def deliver(_row: AgentExternalSyncOutboxRecord) -> ExternalDeliveryResult:
            category = session.get(Category, 1)
            category.name = "Partial write"
            session.add(category)
            session.flush()
            raise RuntimeError("boom")

        with Session(self.engine) as session:
            result = ExternalSyncOutboxService(session, deliverer=deliver).deliver_next(
                worker_token="worker:1"
            )
            self.assertEqual(result.status, "pending")
        with Session(self.engine) as verify:
            self.assertEqual(verify.get(Category, 1).name, "Work")

    def test_retry_uses_completion_time_and_reaches_one_hour_cap(self) -> None:
        start = datetime(2026, 7, 20, 9)
        completed = start + timedelta(seconds=30)
        with Session(self.engine) as session:
            row = session.get(AgentExternalSyncOutboxRecord, "outbox:1")
            row.attempts = 11
            session.add(row)
            session.commit()
            result = ExternalSyncOutboxService(
                session,
                deliverer=lambda _row: ExternalDeliveryResult(
                    status="retryable_failure"
                ),
                clock=lambda: completed,
            ).deliver_next(worker_token="worker:1", now=start)
            self.assertEqual(result.attempts, 12)
            self.assertEqual(
                result.next_attempt_at,
                completed + timedelta(hours=1),
            )

    def test_cleanup_waits_for_active_delivery_and_sync_failure_keeps_local_applied(self) -> None:
        with Session(self.engine) as session:
            claim = ExternalSyncOutboxService(session).claim_next(
                worker_token="worker:1",
                now=datetime(2026, 7, 20, 9),
            )
            self.assertIsNotNone(claim)
            with self.assertRaises(GoalStateStoreError):
                GoalStateStore(session).delete_session_history("session:1")
            session.rollback()
            self.assertIsNotNone(
                session.get(AgentExternalSyncOutboxRecord, "outbox:1")
            )

        with Session(self.engine) as session:
            row = session.get(AgentExternalSyncOutboxRecord, "outbox:1")
            row.status = "pending"
            row.claim_token = None
            row.lease_expires_at = None
            session.add(row)
            session.commit()
            failed = ExternalSyncOutboxService(
                session,
                deliverer=lambda _row: ExternalDeliveryResult(
                    status="permanent_failure",
                    error_code="SYNC_PERMANENT",
                ),
            ).deliver_next(worker_token="worker:2")
            self.assertEqual(failed.status, "failed")
            self.assertEqual(
                session.get(AgentChangeSetExecutionRecord, "execution:1").status,
                "applied",
            )

    def test_claim_rejects_unsafe_lease_and_dirty_session(self) -> None:
        with Session(self.engine) as session:
            service = ExternalSyncOutboxService(session)
            with self.assertRaises(ExternalSyncOutboxError):
                service.claim_next(worker_token="worker:1", lease_seconds=0)
            session.add(Category(name="Unrelated"))
            with self.assertRaises(ExternalSyncOutboxError):
                service.claim_next(worker_token="worker:1")


if __name__ == "__main__":
    unittest.main()
