"""Session-scoped orchestration for model and UI GoalState commands."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5
from datetime import datetime

from sqlalchemy import update
from sqlmodel import Session

from app.models.agent_change_set import AgentChangeSetRecord

from app.services.agent_kernel.contracts import (
    CorrectionDelta,
    GoalState,
    GoalTurnDecision,
    GoalStateUpdateCommand,
    ResolvedDecision,
)
from app.services.agent_kernel.change_set_authoring import EntityReferenceAuthorizer
from app.services.agent_kernel.change_set_deltas import apply_change_set_delta
from app.services.agent_kernel.change_set_store import NativeChangeSetStore
from app.services.agent_kernel.operation_conservation import evaluate_operation_conservation
from app.services.agent_kernel.simulation_store import SimulationStore
from app.services.agent_kernel.goal_state_reducer import (
    ALLOWED_GOAL_TRANSITIONS,
    GoalStateTransitionError,
    apply_correction,
    apply_decision,
    rebase_open_decisions,
    register_decisions,
    transition_goal,
)
from app.services.agent_kernel.state_store import (
    GoalStateAuthorizationError,
    GoalStateNotFound,
    GoalStateRevisionConflict,
    GoalStateStore,
)


class GoalStateCoordinator:
    def __init__(self, session: Session):
        self.session = session
        self.store = GoalStateStore(session)

    @staticmethod
    def _goal_id(session_id: str, command_id: str) -> str:
        return f"goal:{uuid5(NAMESPACE_URL, f'drasil:{session_id}:{command_id}')}"

    def ensure_active(
        self,
        *,
        session_id: str,
        original_request: str,
        correlation_id: str,
        causation_id: str,
        command_id: str,
        current_run_id: str | None = None,
    ) -> GoalState:
        active = self.store.load_active(session_id)
        if active is not None:
            return self.store.resume(
                session_id,
                active.goal_id,
                current_run_id=current_run_id,
            )
        goal_id = self._goal_id(session_id, command_id)
        try:
            return self.store.load(session_id, goal_id)
        except GoalStateNotFound:
            pass
        state = GoalState(
            goal_id=goal_id,
            original_request=original_request or "Continue the typed UI interaction.",
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        opened = self.store.open(
            session_id,
            state,
            command_id=f"goal-open:{command_id}",
            current_run_id=current_run_id,
        )
        self.session.commit()
        return opened

    def apply_model_update(
        self,
        *,
        session_id: str,
        source_turn_id: str,
        correlation_id: str,
        current_run_id: str | None,
        command: GoalStateUpdateCommand,
        commit: bool = True,
    ) -> GoalState:
        if command.disposition == "new_goal":
            state = self._start_new_goal(
                session_id=session_id,
                source_turn_id=source_turn_id,
                correlation_id=correlation_id,
                current_run_id=current_run_id,
                command=command,
            )
            if commit:
                self.session.commit()
            return state

        assert command.goal_id is not None and command.expected_revision is not None
        current = self.store.load(session_id, command.goal_id)
        if command.expected_revision != current.revision:
            # A retried accepted command is safe even though its original expected
            # revision is now stale.
            if any(delta.delta_id == command.command_id for delta in current.correction_deltas):
                accepted = next(
                    delta for delta in current.correction_deltas if delta.delta_id == command.command_id
                )
                if accepted.patches == command.patches:
                    return current
                raise GoalStateRevisionConflict(
                    "idempotency command was already used for different correction intent"
                )
            raise GoalStateRevisionConflict(
                f"stale goal revision: expected {command.expected_revision}, current {current.revision}"
            )
        if command.disposition == "continue":
            return self.store.resume(
                session_id,
                current.goal_id,
                current_run_id=current_run_id,
            )
        if command.disposition == "request_decision":
            updated = register_decisions(current, command.decisions, command_id=command.command_id)
            saved = self.store.save(
                session_id,
                updated,
                expected_revision=current.revision,
                command_id=command.command_id,
                transition_kind="decision.requested",
                payload={"decisionIds": [item.decision_id for item in command.decisions]},
                current_run_id=current_run_id,
            )
            if commit:
                self.session.commit()
            return saved
        delta = CorrectionDelta(
            delta_id=command.command_id,
            source_turn_id=source_turn_id,
            patches=command.patches,
            supersedes_paths=[patch.path for patch in command.patches],
        )
        if command.decisions:
            expected_decision_revision = current.revision + 1
            for decision in command.decisions:
                if decision.goal_revision != expected_decision_revision:
                    raise ValueError("correction decisions must target the corrected goal revision")
        updated = apply_correction(current, delta, decisions=command.decisions)
        if updated is current:
            return current
        updated = GoalState.model_validate(
            {
                **updated.model_dump(mode="python"),
                "correlation_id": correlation_id,
                "causation_id": source_turn_id,
            }
        )
        saved = self.store.save(
            session_id,
            updated,
            expected_revision=current.revision,
            command_id=command.command_id,
            transition_kind="goal.corrected",
            payload={"paths": [patch.path for patch in command.patches]},
            current_run_id=current_run_id,
        )
        if commit:
            self.session.commit()
        return saved

    def apply_turn_decision(
        self,
        *,
        session_id: str,
        source_turn_id: str,
        current_user_turn: str,
        correlation_id: str,
        current_run_id: str | None,
        decision: GoalTurnDecision,
        authorized_reference_ids: set[str] | None = None,
    ) -> GoalState:
        """Bind a small Strategist decision to canonical server-owned authority."""

        command_id = f"goal-turn:{source_turn_id}"
        patches = [
            patch.bind_provenance(
                source_turn_id=source_turn_id,
                source_text=current_user_turn,
            )
            for patch in decision.patches
        ]
        if decision.disposition == "new":
            self._authorize_new_goal_references(
                decision,
                authorized_reference_ids=authorized_reference_ids or set(),
            )
            command = GoalStateUpdateCommand(
                command_id=command_id,
                disposition="new_goal",
                original_request=current_user_turn,
                patches=patches,
            )
            return self.apply_model_update(
                session_id=session_id,
                source_turn_id=source_turn_id,
                correlation_id=correlation_id,
                current_run_id=current_run_id,
                command=command,
            )

        current = self.store.load_active(session_id)
        if current is None:
            raise GoalStateNotFound("no active goal is available for this turn")
        if decision.disposition == "continue":
            payload = decision.model_dump(mode="json", by_alias=True)
            accepted = self.store.record_noop(
                session_id,
                current,
                command_id=command_id,
                transition_kind="goal.continued",
                payload=payload,
            )
            if current_run_id is not None:
                self.store.resume(
                    session_id,
                    current.goal_id,
                    current_run_id=current_run_id,
                )
            self.session.commit()
            return accepted

        command = GoalStateUpdateCommand(
            command_id=command_id,
            disposition="correct",
            goal_id=current.goal_id,
            expected_revision=current.revision,
            patches=patches,
        )
        return self.apply_model_update(
            session_id=session_id,
            source_turn_id=source_turn_id,
            correlation_id=correlation_id,
            current_run_id=current_run_id,
            command=command,
        )

    @staticmethod
    def _authorize_new_goal_references(
        decision: GoalTurnDecision,
        *,
        authorized_reference_ids: set[str],
    ) -> None:
        """Reject IDs the model did not receive from this turn's authority."""

        known_prefixes = (
            "task:",
            "workspace:",
            "calendar_block:",
            "recurrence_series:",
            "recurrence_occurrence:",
            "preference:",
            "scheduling_constraint:",
            "preference_link:",
        )

        def referenced_ids(value: object):
            if isinstance(value, dict):
                entity_id = value.get("entityId", value.get("entity_id"))
                if isinstance(entity_id, str):
                    yield entity_id
                for nested in value.values():
                    yield from referenced_ids(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from referenced_ids(nested)
            elif isinstance(value, str) and value.startswith(known_prefixes):
                yield value

        for patch in decision.patches:
            for entity_id in referenced_ids(patch.value):
                local_id = entity_id.split(":", 1)[-1]
                if (
                    entity_id not in authorized_reference_ids
                    and local_id not in authorized_reference_ids
                ):
                    raise GoalStateAuthorizationError(
                        f"new goal reference {entity_id} was not authorized by current-turn evidence"
                    )

    def _start_new_goal(
        self,
        *,
        session_id: str,
        source_turn_id: str,
        correlation_id: str,
        current_run_id: str | None,
        command: GoalStateUpdateCommand,
    ) -> GoalState:
        goal_id = self._goal_id(session_id, command.command_id)
        try:
            existing = self.store.load(session_id, goal_id)
            if existing.original_request != str(command.original_request):
                raise GoalStateRevisionConflict(
                    "idempotency command was already used for a different new goal"
                )
            expected_patches = [
                patch.model_dump(mode="json", by_alias=True)
                for patch in command.patches
            ]
            initial_delta = next(
                (
                    delta
                    for delta in existing.correction_deltas
                    if delta.delta_id == f"{command.command_id}:initial"
                ),
                None,
            )
            accepted_patches = (
                [
                    patch.model_dump(mode="json", by_alias=True)
                    for patch in initial_delta.patches
                ]
                if initial_delta is not None
                else []
            )
            if accepted_patches != expected_patches:
                raise GoalStateRevisionConflict(
                    "idempotency command was already used for different new-goal patches"
                )
            return existing
        except GoalStateNotFound:
            pass
        active = self.store.load_active(session_id)
        if active is not None:
            if active.status == "committing":
                raise GoalStateTransitionError(
                    "cannot start a new goal while the current goal is committing"
                )
            superseded = transition_goal(
                active,
                "superseded",
                command_id=f"{command.command_id}:supersede",
            )
            superseded = GoalState.model_validate(
                {
                    **superseded.model_dump(mode="python"),
                    "correlation_id": correlation_id,
                    "causation_id": source_turn_id,
                }
            )
            self.store.save(
                session_id,
                superseded,
                expected_revision=active.revision,
                command_id=f"{command.command_id}:supersede",
                transition_kind="goal.superseded",
                current_run_id=current_run_id,
            )
        opened = self.store.open(
            session_id,
            GoalState(
                goal_id=goal_id,
                original_request=str(command.original_request),
                correlation_id=correlation_id,
                causation_id=source_turn_id,
            ),
            command_id=command.command_id,
            current_run_id=current_run_id,
        )
        if not command.patches:
            return opened
        delta = CorrectionDelta(
            delta_id=f"{command.command_id}:initial",
            source_turn_id=source_turn_id,
            patches=command.patches,
            supersedes_paths=[],
        )
        updated = apply_correction(opened, delta)
        return self.store.save(
            session_id,
            updated,
            expected_revision=opened.revision,
            command_id=f"{command.command_id}:initial",
            transition_kind="goal.initialized",
            current_run_id=current_run_id,
        )

    def apply_ui_decision(
        self,
        *,
        session_id: str,
        goal_id: str,
        goal_revision: int,
        decision_id: str,
        option_id: str,
        source_event_id: str,
        current_run_id: str | None = None,
        commit: bool = True,
    ) -> GoalState:
        current = self.store.load(session_id, goal_id)
        decision = next(
            (item for item in current.unresolved_decisions if item.decision_id == decision_id),
            None,
        )
        selected = (
            next((item for item in decision.options if item.option_id == option_id), None)
            if decision is not None
            else None
        )
        if selected is not None and selected.target == "change_set":
            saved = self._apply_change_set_decision(
                session_id=session_id,
                current=current,
                goal_revision=goal_revision,
                decision_id=decision_id,
                option_id=option_id,
                source_event_id=source_event_id,
                current_run_id=current_run_id,
            )
            if commit:
                self.session.commit()
            return saved
        updated = apply_decision(
            current,
            decision_id=decision_id,
            option_id=option_id,
            source_event_id=source_event_id,
            expected_revision=goal_revision,
        )
        if updated is current:
            return current
        saved = self.store.save(
            session_id,
            updated,
            expected_revision=current.revision,
            command_id=source_event_id,
            transition_kind="decision.applied",
            payload={"decisionId": decision_id, "optionId": option_id},
            current_run_id=current_run_id,
        )
        if commit:
            self.session.commit()
        return saved

    def _apply_change_set_decision(
        self,
        *,
        session_id: str,
        current: GoalState,
        goal_revision: int,
        decision_id: str,
        option_id: str,
        source_event_id: str,
        current_run_id: str | None,
    ) -> GoalState:
        prior_resolution = next(
            (item for item in current.resolved_decisions if item.decision_id == decision_id),
            None,
        )
        if prior_resolution is not None:
            if (
                prior_resolution.option_id == option_id
                and prior_resolution.source_event_id == source_event_id
            ):
                return current
            raise GoalStateRevisionConflict("decision is already resolved differently")
        if current.revision != goal_revision or current.status != "needs_user_decision":
            raise GoalStateRevisionConflict("stale or inactive ChangeSet decision")
        decision = next(
            (item for item in current.unresolved_decisions if item.decision_id == decision_id),
            None,
        )
        if decision is None or decision.goal_revision != current.revision:
            raise GoalStateRevisionConflict("decision is not open on the current goal revision")
        option = next((item for item in decision.options if item.option_id == option_id), None)
        if option is None or option.target != "change_set":
            raise GoalStateRevisionConflict("selected option is not a ChangeSet delta")
        change_store = NativeChangeSetStore(self.session)
        prior = change_store.load_current(session_id, current.goal_id)
        if prior is None or prior.change_set_id != option.change_set_id:
            raise GoalStateRevisionConflict("decision does not target the current ChangeSet")
        if prior.simulation_result_ref is None:
            raise GoalStateRevisionConflict("decision has no exact simulation lineage")
        simulation = SimulationStore(self.session).load(
            session_id, prior.simulation_result_ref
        )
        stored_decision = simulation.conflict_graph.decision_request
        stored_option = (
            next((item for item in stored_decision.options if item.option_id == option_id), None)
            if stored_decision is not None and stored_decision.decision_id == decision_id
            else None
        )
        if stored_option != option:
            raise GoalStateRevisionConflict("decision option differs from immutable simulation truth")
        revised = apply_change_set_delta(
            prior,
            option.delta,
            idempotency_key=source_event_id,
            correlation_id=current.correlation_id,
            causation_id=source_event_id,
        )
        revised = revised.model_copy(
            update={
                "source_simulation_id": simulation.simulation_id,
                "resolution_alternative_id": option.alternative_id,
            }
        )
        revised = type(prior).model_validate(revised.model_dump(mode="python"))
        EntityReferenceAuthorizer(self.session).authorize(revised)
        prior_conservation = evaluate_operation_conservation(current, prior)
        if not prior_conservation.complete:
            raise GoalStateRevisionConflict("prior ChangeSet no longer conserves the active goal")
        resolved = ResolvedDecision(
            decision_id=decision_id,
            option_id=option_id,
            source_event_id=source_event_id,
            applied_revision=current.revision + 1,
        )
        remaining_decisions = rebase_open_decisions(
            (
                item
                for item in current.unresolved_decisions
                if item.decision_id != decision_id
            ),
            goal_revision=current.revision + 1,
        )
        updated_goal = GoalState.model_validate(
            {
                **current.model_dump(mode="python"),
                "revision": current.revision + 1,
                "status": (
                    "needs_user_decision" if remaining_decisions else "planning"
                ),
                "unresolved_decisions": remaining_decisions,
                "resolved_decisions": [*current.resolved_decisions, resolved],
                "current_change_set_revision": revised.revision,
                "approved_change_set_revision": None,
                "causation_id": source_event_id,
            }
        )
        command_payload = {
            "decisionId": decision_id,
            "optionId": option_id,
            "simulationId": simulation.simulation_id,
        }
        fingerprint, serialized = change_store._fingerprint(
            "decision", command_payload, revised
        )
        with self.session.begin_nested():
            result = self.session.exec(
                update(AgentChangeSetRecord)
                .where(
                    AgentChangeSetRecord.change_set_id == prior.change_set_id,
                    AgentChangeSetRecord.session_id == session_id,
                    AgentChangeSetRecord.revision == prior.revision,
                )
                .values(
                    status=revised.status,
                    revision=revised.revision,
                    idempotency_key=revised.idempotency_key,
                    causation_id=revised.causation_id,
                    snapshot_json=revised.model_dump_json(),
                    updated_at=datetime.utcnow(),
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise GoalStateRevisionConflict("concurrent ChangeSet decision revision")
            saved = self.store.save(
                session_id,
                updated_goal,
                expected_revision=current.revision,
                command_id=source_event_id,
                transition_kind="changeset.decision_applied",
                payload=command_payload,
                current_run_id=current_run_id,
            )
            change_store._append_command(
                session_id=session_id,
                change_set=revised,
                command_id=source_event_id,
                command_kind="decision",
                fingerprint=fingerprint,
                serialized_payload=serialized,
                linked_goal=saved,
            )
        return saved

    def sync_legacy_outcome(
        self,
        *,
        session_id: str,
        goal_id: str,
        outcome: str,
        command_id: str,
        current_run_id: str | None = None,
        verified_commit: bool = False,
    ) -> GoalState:
        """Project current legacy response status through explicit lifecycle edges."""

        targets = {
            "clarification": "needs_user_decision",
            "review": "ready_for_review",
            "completed": "applied",
            "applied": "applied",
            "rejected": "rejected",
            "system_failed": "system_failed",
        }
        target = targets[outcome]
        state = self.store.load(session_id, goal_id)
        if state.status == target:
            return state
        if (
            target == "applied"
            and target not in ALLOWED_GOAL_TRANSITIONS.get(state.status, set())
            and not verified_commit
        ):
            # Never fabricate review/approval/commit history merely to terminalize
            # a compatibility response. Approved mutations must commit explicitly.
            return state
        path = self._transition_path(state.status, target)
        if path is None:
            return state
        for index, status in enumerate(path, start=1):
            prior = state
            state = transition_goal(
                prior,
                status,
                command_id=f"{command_id}:{index}:{status}",
            )
            state = self.store.save(
                session_id,
                state,
                expected_revision=prior.revision,
                command_id=f"{command_id}:{index}:{status}",
                transition_kind=f"goal.{status}",
                current_run_id=current_run_id,
            )
        self.session.commit()
        return state

    @staticmethod
    def _transition_path(source: str, target: str) -> list[str] | None:
        queue: list[tuple[str, list[str]]] = [(source, [])]
        seen = {source}
        preferred_edges = {
            ("approved", "applied"): ("committing",),
            ("committing", "applied"): ("applied",),
        }
        while queue:
            current, path = queue.pop(0)
            candidates = ALLOWED_GOAL_TRANSITIONS.get(current, set())
            preferred = preferred_edges.get((current, target), ())
            ordered = [
                *[item for item in preferred if item in candidates],
                *sorted(item for item in candidates if item not in preferred),
            ]
            for candidate in ordered:
                if candidate == target:
                    return [*path, candidate]
                if candidate not in seen:
                    seen.add(candidate)
                    queue.append((candidate, [*path, candidate]))
        return None


def decision_clarification_payload(state: GoalState) -> dict[str, object] | None:
    """Compatibility projection of a stored decision into the existing UI shell."""

    decision = next(
        (item for item in state.unresolved_decisions if item.status == "open"),
        None,
    )
    if decision is None:
        return None
    seen_choice_labels: set[str] = set()
    field_choices = []
    for option in decision.options:
        label_key = " ".join(option.label.split()).casefold()
        if label_key in seen_choice_labels:
            continue
        seen_choice_labels.add(label_key)
        field_choices.append({
            "kind": "field",
            "label": option.label,
            "value": option.option_id,
            "follow_up_prompt": "",
            "goal_id": state.goal_id,
            "goal_revision": state.revision,
            "decision_id": decision.decision_id,
            "option_id": option.option_id,
        })
    return {
        "message": decision.question,
        "task_choices": [],
        "field_choices": field_choices,
        "scoped_follow_up_prompts": [],
        "conflicts": [
            conflict.model_dump(mode="json", by_alias=False)
            for conflict in decision.conflicts
        ],
    }
