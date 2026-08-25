"""Durable CAS store for canonical draft-only Agent Kernel ChangeSets."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.agent_change_set import AgentChangeSetCommandRecord, AgentChangeSetRecord
from app.services.agent_kernel.contracts import ChangeSet, GoalState
from app.services.agent_kernel.state_store import (
    GoalStateRevisionConflict,
    GoalStateStore,
)


class NativeChangeSetStoreError(RuntimeError):
    pass


class ChangeSetNotFound(NativeChangeSetStoreError):
    pass


class ChangeSetSessionMismatch(NativeChangeSetStoreError):
    pass


class ChangeSetRevisionConflict(NativeChangeSetStoreError):
    pass


class ChangeSetCommandConflict(NativeChangeSetStoreError):
    pass


class ActiveChangeSetConflict(NativeChangeSetStoreError):
    pass


class NativeChangeSetStore:
    def __init__(self, session: Session):
        self.session = session
        self.goal_store = GoalStateStore(session)

    @staticmethod
    def _canonical_payload(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _fingerprint(
        cls, command_kind: str, command_payload: dict[str, Any], change_set: ChangeSet
    ) -> tuple[str, str]:
        payload = {
            "commandKind": command_kind,
            "command": command_payload,
            "changeSet": change_set.model_dump(mode="json", by_alias=True),
        }
        serialized = cls._canonical_payload(payload)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), serialized

    def _command(self, session_id: str, command_id: str) -> AgentChangeSetCommandRecord | None:
        return self.session.exec(
            select(AgentChangeSetCommandRecord).where(
                AgentChangeSetCommandRecord.session_id == session_id,
                AgentChangeSetCommandRecord.command_id == command_id,
            )
        ).first()

    @staticmethod
    def _from_record(record: AgentChangeSetRecord) -> ChangeSet:
        return ChangeSet.model_validate_json(record.snapshot_json)

    @staticmethod
    def _temp_bindings(change_set: ChangeSet) -> tuple[dict[str, str], dict[str, str]]:
        by_operation: dict[str, str] = {}
        by_temp: dict[str, str] = {}
        for operation in change_set.operations:
            temp_ref = getattr(operation, "temp_ref", None)
            if temp_ref is None:
                continue
            by_operation[operation.operation_id] = temp_ref.entity_id
            by_temp[temp_ref.entity_id] = operation.operation_id
        return by_operation, by_temp

    def load(self, session_id: str, change_set_id: str) -> ChangeSet:
        record = self.session.get(AgentChangeSetRecord, change_set_id)
        if record is None:
            raise ChangeSetNotFound(f"ChangeSet {change_set_id} does not exist")
        if record.session_id != session_id:
            raise ChangeSetSessionMismatch("ChangeSet does not belong to this session")
        return self._from_record(record)

    def load_current(self, session_id: str, goal_id: str) -> ChangeSet | None:
        record = self.session.exec(
            select(AgentChangeSetRecord).where(
                AgentChangeSetRecord.session_id == session_id,
                AgentChangeSetRecord.current_goal_id == goal_id,
            )
        ).first()
        return self._from_record(record) if record is not None else None

    def _validated_replay(
        self,
        replay: AgentChangeSetCommandRecord,
        *,
        fingerprint: str,
        change_set_id: str,
        goal_id: str,
        command_kind: str,
    ) -> tuple[ChangeSet, GoalState]:
        if (
            replay.payload_fingerprint != fingerprint
            or replay.change_set_id != change_set_id
            or replay.goal_id != goal_id
            or replay.command_kind != command_kind
        ):
            raise ChangeSetCommandConflict(
                "idempotency command was already used for different ChangeSet intent"
            )
        try:
            change_set = ChangeSet.model_validate_json(replay.change_set_snapshot_json)
            goal = GoalState.model_validate_json(replay.goal_snapshot_json)
        except Exception as error:
            raise ChangeSetCommandConflict("idempotency record has no valid result snapshot") from error
        if change_set.revision != replay.revision or goal.current_change_set_revision != replay.revision:
            raise ChangeSetCommandConflict("idempotency result snapshot revision is inconsistent")
        return change_set, goal

    def _linked_goal(
        self,
        *,
        session_id: str,
        change_set: ChangeSet,
        expected_goal_revision: int,
        command_id: str,
    ) -> GoalState:
        current = self.goal_store.load(session_id, change_set.goal_id)
        if current.revision != expected_goal_revision:
            raise ChangeSetRevisionConflict(
                f"stale goal revision: expected {expected_goal_revision}, current {current.revision}"
            )
        linked = GoalState.model_validate(
            {
                **current.model_dump(mode="python"),
                "revision": current.revision + 1,
                "current_change_set_id": change_set.change_set_id,
                "current_change_set_revision": change_set.revision,
                "approved_change_set_revision": None,
                "correlation_id": change_set.correlation_id,
                "causation_id": change_set.causation_id,
            }
        )
        try:
            return self.goal_store.save(
                session_id,
                linked,
                expected_revision=current.revision,
                command_id=f"goal-link:{command_id}",
                transition_kind="changeset.linked",
                payload={
                    "changeSetId": change_set.change_set_id,
                    "changeSetRevision": change_set.revision,
                },
            )
        except GoalStateRevisionConflict as error:
            raise ChangeSetRevisionConflict(str(error)) from error

    def create(
        self,
        session_id: str,
        change_set: ChangeSet,
        *,
        expected_goal_revision: int,
        command_id: str,
        command_payload: dict[str, Any],
    ) -> tuple[ChangeSet, GoalState]:
        canonical = ChangeSet.model_validate(change_set.model_dump(mode="python"))
        if canonical.revision != 1 or canonical.status != "draft":
            raise ChangeSetRevisionConflict("new native ChangeSet must start as draft revision 1")
        fingerprint, serialized_payload = self._fingerprint(
            "create", command_payload, canonical
        )
        replay = self._command(session_id, command_id)
        if replay is not None:
            return self._validated_replay(
                replay,
                fingerprint=fingerprint,
                change_set_id=canonical.change_set_id,
                goal_id=canonical.goal_id,
                command_kind="create",
            )
        if self.load_current(session_id, canonical.goal_id) is not None:
            raise ActiveChangeSetConflict("goal already has a current native ChangeSet")
        if self.session.get(AgentChangeSetRecord, canonical.change_set_id) is not None:
            raise ActiveChangeSetConflict("ChangeSet identity already exists")
        now = datetime.utcnow()
        try:
            with self.session.begin_nested():
                self.session.add(
                    AgentChangeSetRecord(
                        change_set_id=canonical.change_set_id,
                        goal_id=canonical.goal_id,
                        session_id=session_id,
                        current_goal_id=canonical.goal_id,
                        status=canonical.status,
                        revision=canonical.revision,
                        idempotency_key=canonical.idempotency_key,
                        correlation_id=canonical.correlation_id,
                        causation_id=canonical.causation_id,
                        snapshot_json=canonical.model_dump_json(),
                        created_at=now,
                        updated_at=now,
                    )
                )
                self.session.flush()
                linked_goal = self._linked_goal(
                    session_id=session_id,
                    change_set=canonical,
                    expected_goal_revision=expected_goal_revision,
                    command_id=command_id,
                )
                self._append_command(
                    session_id=session_id,
                    change_set=canonical,
                    command_id=command_id,
                    command_kind="create",
                    fingerprint=fingerprint,
                    serialized_payload=serialized_payload,
                    linked_goal=linked_goal,
                )
        except IntegrityError as error:
            self.session.expire_all()
            replay = self._command(session_id, command_id)
            if replay is not None:
                return self._validated_replay(
                    replay,
                    fingerprint=fingerprint,
                    change_set_id=canonical.change_set_id,
                    goal_id=canonical.goal_id,
                    command_kind="create",
                )
            raise ActiveChangeSetConflict("goal already has a current native ChangeSet") from error
        return canonical, linked_goal

    def revise(
        self,
        session_id: str,
        change_set: ChangeSet,
        *,
        expected_change_set_revision: int,
        expected_goal_revision: int,
        command_id: str,
        command_payload: dict[str, Any],
    ) -> tuple[ChangeSet, GoalState]:
        canonical = ChangeSet.model_validate(change_set.model_dump(mode="python"))
        if canonical.status != "draft" or canonical.revision != expected_change_set_revision + 1:
            raise ChangeSetRevisionConflict("revision must produce the next draft revision")
        fingerprint, serialized_payload = self._fingerprint(
            "revise", command_payload, canonical
        )
        replay = self._command(session_id, command_id)
        if replay is not None:
            return self._validated_replay(
                replay,
                fingerprint=fingerprint,
                change_set_id=canonical.change_set_id,
                goal_id=canonical.goal_id,
                command_kind="revise",
            )
        record = self.session.get(AgentChangeSetRecord, canonical.change_set_id)
        if record is None:
            raise ChangeSetNotFound(f"ChangeSet {canonical.change_set_id} does not exist")
        if record.session_id != session_id:
            raise ChangeSetSessionMismatch("ChangeSet does not belong to this session")
        if record.goal_id != canonical.goal_id:
            raise ChangeSetRevisionConflict("ChangeSet goal identity cannot change")
        prior = self._from_record(record)
        revisable_statuses = {
            "draft",
            "simulating",
            "invalid",
            "needs_user_decision",
            "ready_for_review",
            "rejected",
        }
        if prior.status not in revisable_statuses:
            raise ChangeSetRevisionConflict(
                f"cannot revise ChangeSet while lifecycle is {prior.status}"
            )
        prior_operations = {operation.operation_id: operation for operation in prior.operations}
        next_operations = {operation.operation_id: operation for operation in canonical.operations}
        for operation_id in prior_operations.keys() & next_operations.keys():
            if self._operation_identity(prior_operations[operation_id]) != self._operation_identity(
                next_operations[operation_id]
            ):
                raise ChangeSetRevisionConflict(
                    f"operation {operation_id} cannot change target, dependency, kind, or atomic group"
                )
        prior_by_operation, prior_by_temp = self._temp_bindings(prior)
        next_by_operation, next_by_temp = self._temp_bindings(canonical)
        for operation_id in prior_by_operation.keys() & next_by_operation.keys():
            if prior_by_operation[operation_id] != next_by_operation[operation_id]:
                raise ChangeSetRevisionConflict(
                    f"operation {operation_id} cannot rebind its temporary reference"
                )
        for temp_id in prior_by_temp.keys() & next_by_temp.keys():
            if prior_by_temp[temp_id] != next_by_temp[temp_id]:
                raise ChangeSetRevisionConflict(
                    f"temporary reference {temp_id} cannot move to a different producer"
                )
        try:
            with self.session.begin_nested():
                result = self.session.exec(
                update(AgentChangeSetRecord)
                .where(
                    AgentChangeSetRecord.change_set_id == canonical.change_set_id,
                    AgentChangeSetRecord.session_id == session_id,
                    AgentChangeSetRecord.revision == expected_change_set_revision,
                    AgentChangeSetRecord.status.in_(revisable_statuses),
                )
                .values(
                    status=canonical.status,
                    revision=canonical.revision,
                    idempotency_key=canonical.idempotency_key,
                    correlation_id=canonical.correlation_id,
                    causation_id=canonical.causation_id,
                    snapshot_json=canonical.model_dump_json(),
                    updated_at=datetime.utcnow(),
                )
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    self.session.expire_all()
                    replay = self._command(session_id, command_id)
                    if replay is not None:
                        return self._validated_replay(
                            replay,
                            fingerprint=fingerprint,
                            change_set_id=canonical.change_set_id,
                            goal_id=canonical.goal_id,
                            command_kind="revise",
                        )
                    raise ChangeSetRevisionConflict(
                        f"stale ChangeSet revision: expected {expected_change_set_revision}"
                    )
                self.session.expire_all()
                linked_goal = self._linked_goal(
                    session_id=session_id,
                    change_set=canonical,
                    expected_goal_revision=expected_goal_revision,
                    command_id=command_id,
                )
                self._append_command(
                    session_id=session_id,
                    change_set=canonical,
                    command_id=command_id,
                    command_kind="revise",
                    fingerprint=fingerprint,
                    serialized_payload=serialized_payload,
                    linked_goal=linked_goal,
                )
        except IntegrityError as error:
            self.session.expire_all()
            replay = self._command(session_id, command_id)
            if replay is not None:
                return self._validated_replay(
                    replay,
                    fingerprint=fingerprint,
                    change_set_id=canonical.change_set_id,
                    goal_id=canonical.goal_id,
                    command_kind="revise",
                )
            raise ChangeSetRevisionConflict("concurrent ChangeSet revision conflict") from error
        return canonical, linked_goal

    @staticmethod
    def _operation_identity(operation: Any) -> str:
        payload = operation.model_dump(mode="json", by_alias=True)
        identity: dict[str, Any] = {
            "kind": payload.get("kind"),
            "atomicGroupId": payload.get("atomicGroupId"),
            "dependsOn": payload.get("dependsOn", []),
        }

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("kind") in {
                    "task", "workspace", "calendar_block", "recurrence_series",
                    "recurrence_occurrence", "preference", "temporary",
                } and "entityId" in value:
                    identity.setdefault("references", []).append(value)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        for key, value in payload.items():
            if key not in {"provenance", "fulfillsClauseIds"}:
                visit(value)
        return NativeChangeSetStore._canonical_payload(identity)

    def _append_command(
        self,
        *,
        session_id: str,
        change_set: ChangeSet,
        command_id: str,
        command_kind: str,
        fingerprint: str,
        serialized_payload: str,
        linked_goal: GoalState,
    ) -> None:
        self.session.add(
            AgentChangeSetCommandRecord(
                change_set_id=change_set.change_set_id,
                goal_id=change_set.goal_id,
                session_id=session_id,
                revision=change_set.revision,
                command_id=command_id,
                command_kind=command_kind,
                payload_fingerprint=fingerprint,
                payload_json=serialized_payload,
                change_set_snapshot_json=change_set.model_dump_json(),
                goal_snapshot_json=linked_goal.model_dump_json(),
            )
        )
        self.session.flush()

    def delete_session_history(self, session_id: str) -> None:
        self.session.exec(
            delete(AgentChangeSetCommandRecord).where(
                AgentChangeSetCommandRecord.session_id == session_id
            )
        )
        self.session.exec(
            delete(AgentChangeSetRecord).where(AgentChangeSetRecord.session_id == session_id)
        )
        self.session.flush()

    def transition_status(
        self,
        session_id: str,
        change_set_id: str,
        *,
        expected_revision: int,
        expected_status: str,
        target_status: str,
    ) -> ChangeSet:
        allowed = {
            ("ready_for_review", "approved"),
            ("approved", "applied"),
            ("ready_for_review", "rejected"),
            ("needs_user_decision", "rejected"),
        }
        if (expected_status, target_status) not in allowed:
            raise ChangeSetRevisionConflict(
                f"invalid ChangeSet transition {expected_status} -> {target_status}"
            )
        current = self.load(session_id, change_set_id)
        if current.revision != expected_revision or current.status != expected_status:
            raise ChangeSetRevisionConflict("stale ChangeSet lifecycle transition")
        approved_revision = (
            expected_revision if target_status in {"approved", "applied"} else None
        )
        transitioned = ChangeSet.model_validate(
            {
                **current.model_dump(mode="python"),
                "status": target_status,
                "approved_revision": approved_revision,
            }
        )
        result = self.session.exec(
            update(AgentChangeSetRecord)
            .where(
                AgentChangeSetRecord.change_set_id == change_set_id,
                AgentChangeSetRecord.session_id == session_id,
                AgentChangeSetRecord.revision == expected_revision,
                AgentChangeSetRecord.status == expected_status,
            )
            .values(
                status=target_status,
                snapshot_json=transitioned.model_dump_json(),
                updated_at=datetime.utcnow(),
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            self.session.expire_all()
            raise ChangeSetRevisionConflict("concurrent ChangeSet lifecycle transition")
        self.session.expire_all()
        return transitioned

    def invalidate_review(
        self,
        session_id: str,
        change_set_id: str,
        *,
        expected_revision: int,
        expected_status: str,
    ) -> ChangeSet:
        """Atomically retire review/simulation readiness before re-authoring."""

        if expected_status not in {"ready_for_review", "needs_user_decision"}:
            raise ChangeSetRevisionConflict("only a reviewable ChangeSet can be invalidated")
        current = self.load(session_id, change_set_id)
        if current.revision != expected_revision or current.status != expected_status:
            raise ChangeSetRevisionConflict("stale ChangeSet review invalidation")
        invalidated = ChangeSet.model_validate({
            **current.model_dump(mode="python"),
            "status": "draft",
            "simulation_result_ref": None,
            "approved_revision": None,
            "source_simulation_id": None,
            "resolution_alternative_id": None,
        })
        result = self.session.exec(
            update(AgentChangeSetRecord)
            .where(
                AgentChangeSetRecord.change_set_id == change_set_id,
                AgentChangeSetRecord.session_id == session_id,
                AgentChangeSetRecord.revision == expected_revision,
                AgentChangeSetRecord.status == expected_status,
            )
            .values(
                status=invalidated.status,
                snapshot_json=invalidated.model_dump_json(),
                updated_at=datetime.utcnow(),
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            self.session.expire_all()
            raise ChangeSetRevisionConflict("concurrent ChangeSet review invalidation")
        self.session.expire_all()
        return invalidated
