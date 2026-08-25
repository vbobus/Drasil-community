"""Atomic persistence boundary for canonical durable GoalState."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.agent_goal import AgentGoalStateRecord, AgentGoalTransitionRecord
from app.services.agent_kernel.contracts import GoalState
from app.services.agent_kernel.goal_state_reducer import TERMINAL_GOAL_STATUSES


class GoalStateStoreError(RuntimeError):
    pass


class GoalStateNotFound(GoalStateStoreError):
    pass


class GoalStateSessionMismatch(GoalStateStoreError):
    pass


class GoalStateAuthorizationError(GoalStateStoreError):
    pass


class GoalStateRevisionConflict(GoalStateStoreError):
    pass


class ActiveGoalConflict(GoalStateStoreError):
    pass


_KNOWN_CONSTRAINT_KINDS = {
    "minimum_gap", "temporal_window", "exact_time", "cardinality", "ordering",
    "same_workspace", "preserve",
}


def _payload_hash(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _migrate_constraint_list(
    values: Any, *, path: str, issues: list[dict[str, Any]]
) -> Any:
    if not isinstance(values, list):
        return values
    migrated: list[Any] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            migrated.append(item)
            continue
        kind = item.get("kind")
        issue_code: str | None = None
        if kind not in _KNOWN_CONSTRAINT_KINDS:
            issue_code = "unsupported_constraint"
        else:
            for scope_name in ("scope", "objectScope", "object_scope"):
                scope = item.get(scope_name)
                if not isinstance(scope, dict):
                    continue
                scope_kind = scope.get("kind")
                refs_key = "entityRefs" if "entityRefs" in scope else "entity_refs"
                refs = scope.get(refs_key, [])
                for ref in refs if isinstance(refs, list) else []:
                    if not isinstance(ref, dict) or ref.get("kind") != "temporary":
                        continue
                    if ref.get("targetKind", ref.get("target_kind")):
                        continue
                    inferred = {
                        "series": "recurrence_series",
                        "occurrence_set": "recurrence_occurrence",
                    }.get(scope_kind)
                    if inferred:
                        ref["targetKind"] = inferred
                    else:
                        issue_code = "temporary_kind_unresolved"
        if issue_code is None:
            migrated.append(item)
            continue
        digest = _payload_hash(item)
        issue = {
            "issueId": f"migration:{digest[:24]}",
            "path": f"{path}[{index}]",
            "code": issue_code,
            "legacyKind": str(kind) if kind is not None else None,
            "payloadSha256": digest,
        }
        if not any(existing.get("issueId") == issue["issueId"] for existing in issues):
            issues.append(issue)
    return migrated


def _migrate_stored_goal_state(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    migrated = dict(payload)
    issues = list(migrated.get("migrationIssues", migrated.get("migration_issues", [])) or [])
    constraints_key = "constraints"
    migrated[constraints_key] = _migrate_constraint_list(
        migrated.get(constraints_key, []), path="constraints", issues=issues
    )
    slots_key = "semanticSlots" if "semanticSlots" in migrated else "semantic_slots"
    slots = migrated.get(slots_key, [])
    if isinstance(slots, list):
        for slot_index, slot in enumerate(slots):
            if not isinstance(slot, dict) or slot.get("path") != "constraints.active":
                continue
            semantic_key = "semanticValue" if "semanticValue" in slot else "semantic_value"
            semantic = slot.get(semantic_key)
            if isinstance(semantic, dict):
                semantic["value"] = _migrate_constraint_list(
                    semantic.get("value"),
                    path=f"semanticSlots[{slot_index}].semanticValue.value",
                    issues=issues,
                )
    migrated["migrationIssues"] = issues
    migrated.pop("migration_issues", None)
    return migrated


class GoalStateStore:
    def __init__(self, session: Session):
        self.session = session

    def _transition_for_command(
        self,
        session_id: str,
        command_id: str,
    ) -> AgentGoalTransitionRecord | None:
        return self.session.exec(
            select(AgentGoalTransitionRecord).where(
                AgentGoalTransitionRecord.session_id == session_id,
                AgentGoalTransitionRecord.command_id == command_id,
            )
        ).first()

    @staticmethod
    def _state_from_record(record: AgentGoalStateRecord) -> GoalState:
        payload = json.loads(record.state_json)
        return GoalState.model_validate(_migrate_stored_goal_state(payload))

    @staticmethod
    def _active_session_id(session_id: str, state: GoalState) -> str | None:
        return None if state.status in TERMINAL_GOAL_STATUSES else session_id

    def _validated_replay(
        self,
        replay: AgentGoalTransitionRecord,
        *,
        goal_id: str,
        transition_kind: str,
        payload: dict[str, Any],
    ) -> GoalState:
        replay_payload = json.loads(replay.payload_json or "{}")
        if (
            replay.goal_id != goal_id
            or replay.transition_kind != transition_kind
            or replay_payload != payload
        ):
            raise GoalStateRevisionConflict(
                "idempotency command was already used for different goal-state intent"
            )
        if replay.state_json:
            accepted = GoalState.model_validate(
                _migrate_stored_goal_state(json.loads(replay.state_json))
            )
            if accepted.goal_id != replay.goal_id or accepted.revision != replay.revision:
                raise GoalStateRevisionConflict(
                    "stored command replay snapshot does not match its audit identity"
                )
            return accepted
        current = self.load(replay.session_id, replay.goal_id)
        if current.revision == replay.revision:
            return current
        raise GoalStateRevisionConflict(
            "historical command replay snapshot is unavailable for this legacy audit row"
        )

    def load(self, session_id: str, goal_id: str) -> GoalState:
        record = self.session.get(AgentGoalStateRecord, goal_id)
        if record is None:
            raise GoalStateNotFound(f"goal {goal_id} does not exist")
        if record.session_id != session_id:
            raise GoalStateSessionMismatch("goal does not belong to this session")
        return self._state_from_record(record)

    def load_active(self, session_id: str) -> GoalState | None:
        record = self.session.exec(
            select(AgentGoalStateRecord).where(
                AgentGoalStateRecord.active_session_id == session_id
            )
        ).first()
        return self._state_from_record(record) if record is not None else None

    def replay(self, session_id: str, goal_id: str) -> list[AgentGoalTransitionRecord]:
        self.load(session_id, goal_id)
        return list(
            self.session.exec(
                select(AgentGoalTransitionRecord)
                .where(AgentGoalTransitionRecord.session_id == session_id)
                .where(AgentGoalTransitionRecord.goal_id == goal_id)
                .order_by(
                    AgentGoalTransitionRecord.revision,
                    AgentGoalTransitionRecord.created_at,
                )
            ).all()
        )

    def record_noop(
        self,
        session_id: str,
        state: GoalState,
        *,
        command_id: str,
        transition_kind: str,
        payload: dict[str, Any],
    ) -> GoalState:
        """Audit a semantic no-op without incrementing the GoalState revision."""

        replay = self._transition_for_command(session_id, command_id)
        if replay is not None:
            self._validated_replay(
                replay,
                goal_id=state.goal_id,
                transition_kind=transition_kind,
                payload=payload,
            )
            return self.load(session_id, state.goal_id)
        try:
            with self.session.begin_nested():
                self._append_transition(
                    session_id=session_id,
                    state=state,
                    command_id=command_id,
                    transition_kind=transition_kind,
                    payload=payload,
                )
        except IntegrityError:
            self.session.expire_all()
            replay = self._transition_for_command(session_id, command_id)
            if replay is None:
                raise GoalStateRevisionConflict(
                    "concurrent no-op transition could not be replayed"
                )
            self._validated_replay(
                replay,
                goal_id=state.goal_id,
                transition_kind=transition_kind,
                payload=payload,
            )
        return self.load(session_id, state.goal_id)

    def open(
        self,
        session_id: str,
        state: GoalState,
        *,
        command_id: str,
        current_run_id: str | None = None,
    ) -> GoalState:
        canonical = GoalState.model_validate(state.model_dump(mode="python"))
        replay_payload = {"originalRequest": canonical.original_request}
        replay = self._transition_for_command(session_id, command_id)
        if replay is not None:
            return self._validated_replay(
                replay,
                goal_id=canonical.goal_id,
                transition_kind="goal.opened",
                payload=replay_payload,
            )
        if canonical.status in TERMINAL_GOAL_STATUSES:
            raise ActiveGoalConflict("cannot open a goal in a terminal state")
        active = self.load_active(session_id)
        if active is not None:
            raise ActiveGoalConflict(f"session already has active goal {active.goal_id}")
        if self.session.get(AgentGoalStateRecord, canonical.goal_id) is not None:
            raise ActiveGoalConflict(f"goal {canonical.goal_id} already exists")
        now = datetime.utcnow()
        try:
            with self.session.begin_nested():
                self.session.add(
                    AgentGoalStateRecord(
                        goal_id=canonical.goal_id,
                        session_id=session_id,
                        status=canonical.status,
                        revision=canonical.revision,
                        active_session_id=session_id,
                        current_run_id=current_run_id,
                        correlation_id=canonical.correlation_id,
                        causation_id=canonical.causation_id,
                        state_json=canonical.model_dump_json(),
                        created_at=now,
                        updated_at=now,
                    )
                )
                self.session.flush()
        except IntegrityError as error:
            raise ActiveGoalConflict("session already has an active goal") from error
        self._append_transition(
            session_id=session_id,
            state=canonical,
            command_id=command_id,
            transition_kind="goal.opened",
            payload=replay_payload,
        )
        return canonical

    def save(
        self,
        session_id: str,
        state: GoalState,
        *,
        expected_revision: int,
        command_id: str,
        transition_kind: str,
        payload: dict[str, Any] | None = None,
        current_run_id: str | None = None,
    ) -> GoalState:
        canonical = GoalState.model_validate(state.model_dump(mode="python"))
        transition_payload = payload or {}
        replay = self._transition_for_command(session_id, command_id)
        if replay is not None:
            return self._validated_replay(
                replay,
                goal_id=canonical.goal_id,
                transition_kind=transition_kind,
                payload=transition_payload,
            )
        record = self.session.get(AgentGoalStateRecord, canonical.goal_id)
        if record is None:
            raise GoalStateNotFound(f"goal {canonical.goal_id} does not exist")
        if record.session_id != session_id:
            raise GoalStateSessionMismatch("goal does not belong to this session")
        if canonical.revision != expected_revision + 1:
            raise GoalStateRevisionConflict("next state must increment revision exactly once")
        result = self.session.exec(
            update(AgentGoalStateRecord)
            .where(
                AgentGoalStateRecord.goal_id == canonical.goal_id,
                AgentGoalStateRecord.session_id == session_id,
                AgentGoalStateRecord.revision == expected_revision,
            )
            .values(
                status=canonical.status,
                revision=canonical.revision,
                active_session_id=self._active_session_id(session_id, canonical),
                current_run_id=current_run_id if current_run_id is not None else record.current_run_id,
                correlation_id=canonical.correlation_id,
                causation_id=canonical.causation_id,
                state_json=canonical.model_dump_json(),
                updated_at=datetime.utcnow(),
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            self.session.expire_all()
            replay = self._transition_for_command(session_id, command_id)
            if replay is not None:
                return self._validated_replay(
                    replay,
                    goal_id=canonical.goal_id,
                    transition_kind=transition_kind,
                    payload=transition_payload,
                )
            raise GoalStateRevisionConflict(
                f"stale goal revision: expected {expected_revision}"
            )
        self.session.expire_all()
        self._append_transition(
            session_id=session_id,
            state=canonical,
            command_id=command_id,
            transition_kind=transition_kind,
            payload=transition_payload,
        )
        return canonical

    def close(
        self,
        session_id: str,
        state: GoalState,
        *,
        expected_revision: int,
        command_id: str,
        transition_kind: str,
    ) -> GoalState:
        if state.status not in TERMINAL_GOAL_STATUSES:
            raise GoalStateStoreError("close requires a terminal GoalState")
        return self.save(
            session_id,
            state,
            expected_revision=expected_revision,
            command_id=command_id,
            transition_kind=transition_kind,
        )

    def resume(
        self,
        session_id: str,
        goal_id: str,
        *,
        current_run_id: str | None = None,
        command_id: str | None = None,
    ) -> GoalState:
        state = self.load(session_id, goal_id)
        if state.status in TERMINAL_GOAL_STATUSES:
            raise GoalStateStoreError("terminal goals cannot be resumed")
        if current_run_id is not None:
            self._link_current_run_compare_and_swap(
                session_id=session_id,
                goal_id=goal_id,
                current_run_id=current_run_id,
            )
            audit_command_id = command_id or f"goal-resume:{goal_id}:{current_run_id}"
            replay = self._transition_for_command(session_id, audit_command_id)
            if replay is None:
                self._append_transition(
                    session_id=session_id,
                    state=state,
                    command_id=audit_command_id,
                    transition_kind="goal.resumed",
                    payload={"currentRunId": current_run_id},
                )
        return state

    def _link_current_run_compare_and_swap(
        self,
        *,
        session_id: str,
        goal_id: str,
        current_run_id: str,
    ) -> None:
        from app.models.strategist_agent import StrategistRun

        incoming_run = self.session.get(StrategistRun, current_run_id)

        def assert_not_older(observed_run_id: str | None) -> None:
            if observed_run_id is None or observed_run_id == current_run_id:
                return
            observed_run = self.session.get(StrategistRun, observed_run_id)
            if observed_run is None or incoming_run is None:
                raise GoalStateRevisionConflict(
                    "cannot replace an existing goal run link without comparable run identity"
                )
            incoming_order = (incoming_run.created_at, incoming_run.id)
            observed_order = (observed_run.created_at, observed_run.id)
            if incoming_order < observed_order:
                raise GoalStateRevisionConflict("cannot regress goal linkage to an older run")

        for _attempt in range(3):
            record = self.session.get(AgentGoalStateRecord, goal_id)
            if record is None:
                raise GoalStateNotFound(f"goal {goal_id} does not exist")
            if record.session_id != session_id:
                raise GoalStateSessionMismatch("goal does not belong to this session")
            observed_run_id = record.current_run_id
            if observed_run_id == current_run_id:
                return
            assert_not_older(observed_run_id)
            result = self.session.exec(
                update(AgentGoalStateRecord)
                .where(
                    AgentGoalStateRecord.goal_id == goal_id,
                    AgentGoalStateRecord.session_id == session_id,
                    AgentGoalStateRecord.current_run_id == observed_run_id,
                )
                .values(current_run_id=current_run_id, updated_at=datetime.utcnow())
            )
            if result.rowcount == 1:  # type: ignore[attr-defined]
                self.session.expire_all()
                return
            self.session.expire_all()
        raise GoalStateRevisionConflict("concurrent goal run linkage did not stabilize")

    def delete_session_history(self, session_id: str) -> None:
        """Explicit cleanup boundary to call before deleting a Strategist session."""

        from app.services.agent_kernel.change_set_store import NativeChangeSetStore
        from app.services.agent_kernel.simulation_store import SimulationStore
        from app.models.agent_execution import (
            AgentChangeSetExecutionRecord,
            AgentExternalSyncOutboxRecord,
            AgentReviewActionRecord,
        )
        from app.models.agent_change_set import AgentChangeSetRecord
        from app.models.recurrence_series import RecurrenceSeries

        # Acquire SQLite's write lock before checking active claims. This closes
        # the claim-vs-cleanup race: new pending work is cancelled atomically,
        # while an already-delivering provider effect keeps history intact until
        # its fenced worker persists the remote identity.
        self.session.exec(
            update(AgentExternalSyncOutboxRecord)
            .where(
                AgentExternalSyncOutboxRecord.session_id == session_id,
                AgentExternalSyncOutboxRecord.status.in_(["pending", "not_configured"]),
            )
            .values(
                status="cancelled",
                claim_token=None,
                lease_expires_at=None,
                next_attempt_at=None,
                updated_at=datetime.utcnow(),
            )
        )
        active_delivery = self.session.exec(
            select(AgentExternalSyncOutboxRecord.outbox_id).where(
                AgentExternalSyncOutboxRecord.session_id == session_id,
                AgentExternalSyncOutboxRecord.status == "delivering",
            )
        ).first()
        if active_delivery is not None:
            raise GoalStateStoreError(
                "session history cleanup must wait for active external delivery"
            )

        self.session.exec(
            delete(AgentExternalSyncOutboxRecord).where(
                AgentExternalSyncOutboxRecord.session_id == session_id
            )
        )
        self.session.exec(
            delete(AgentChangeSetExecutionRecord).where(
                AgentChangeSetExecutionRecord.session_id == session_id
            )
        )
        self.session.exec(
            delete(AgentReviewActionRecord).where(
                AgentReviewActionRecord.session_id == session_id
            )
        )

        retained_source_ids = list(
            self.session.exec(
                select(AgentChangeSetRecord.change_set_id).where(
                    AgentChangeSetRecord.session_id == session_id
                )
            ).all()
        )
        if retained_source_ids:
            self.session.exec(
                update(RecurrenceSeries)
                .where(RecurrenceSeries.source_agent_change_set_id.in_(retained_source_ids))
                .values(
                    source_agent_change_set_id=None,
                    revision=RecurrenceSeries.revision + 1,
                )
            )

        SimulationStore(self.session).delete_session_history(session_id)
        NativeChangeSetStore(self.session).delete_session_history(session_id)

        self.session.exec(
            delete(AgentGoalTransitionRecord).where(
                AgentGoalTransitionRecord.session_id == session_id
            )
        )
        self.session.exec(
            delete(AgentGoalStateRecord).where(AgentGoalStateRecord.session_id == session_id)
        )
        self.session.flush()

    def _append_transition(
        self,
        *,
        session_id: str,
        state: GoalState,
        command_id: str,
        transition_kind: str,
        payload: dict[str, Any],
    ) -> None:
        self.session.add(
            AgentGoalTransitionRecord(
                goal_id=state.goal_id,
                session_id=session_id,
                revision=state.revision,
                command_id=command_id,
                transition_kind=transition_kind,
                payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
                state_json=state.model_dump_json(),
            )
        )
        self.session.flush()
