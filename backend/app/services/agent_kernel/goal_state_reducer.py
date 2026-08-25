"""Deterministic reducers for canonical Agent Kernel GoalState."""

from __future__ import annotations

from typing import Iterable

from app.services.agent_kernel.contracts import (
    CorrectionDelta,
    DecisionRequest,
    GoalState,
    GoalStatePatch,
    GoalSemanticPath,
    Provenance,
    ResolvedDecision,
    SemanticSlot,
    SemanticValue,
)


class GoalStateReducerError(ValueError):
    """Base typed failure for a deterministic GoalState command."""


class GoalStateTransitionError(GoalStateReducerError):
    pass


class GoalStateDecisionConflict(GoalStateReducerError):
    pass


TERMINAL_GOAL_STATUSES = {
    "applied",
    "rejected",
    "superseded",
    "cancelled",
    "system_failed",
}

ALLOWED_GOAL_TRANSITIONS: dict[str, set[str]] = {
    "collecting_context": {"planning", "applied", "cancelled", "superseded", "system_failed"},
    "planning": {
        "collecting_context",
        "simulating",
        "repairing",
        "needs_user_decision",
        "ready_for_review",
        "applied",
        "cancelled",
        "superseded",
        "system_failed",
    },
    "simulating": {
        "repairing",
        "needs_user_decision",
        "ready_for_review",
        "applied",
        "cancelled",
        "superseded",
        "system_failed",
    },
    "repairing": {
        "simulating",
        "needs_user_decision",
        "ready_for_review",
        "applied",
        "cancelled",
        "superseded",
        "system_failed",
    },
    "needs_user_decision": {
        "planning",
        "simulating",
        "applied",
        "cancelled",
        "superseded",
        "system_failed",
    },
    "ready_for_review": {
        "planning",
        "approved",
        "applied",
        "rejected",
        "cancelled",
        "superseded",
        "system_failed",
    },
    "approved": {"planning", "committing", "rejected", "cancelled", "superseded", "system_failed"},
    "committing": {"applied", "system_failed"},
    "applied": set(),
    "rejected": set(),
    "superseded": set(),
    "cancelled": set(),
    "system_failed": set(),
}

_PROVENANCE_PRECEDENCE = {
    "default": 0,
    "inference": 1,
    "policy": 2,
    "retrieval": 2,
    "tool": 2,
    "user": 3,
    "ui": 3,
}


def _validated_copy(state: GoalState, **updates: object) -> GoalState:
    payload = state.model_dump(mode="python")
    payload.update(updates)
    return GoalState.model_validate(payload)


def _semantic_value(patch: GoalStatePatch, prior: SemanticValue | None) -> SemanticValue:
    provenance_data = patch.provenance.model_dump(mode="python")
    if prior is not None and prior.provenance is not None:
        provenance_data["supersedes_ref"] = prior.provenance.source_ref
    provenance = Provenance.model_validate(provenance_data)
    if patch.operation == "set":
        state = "inferred" if provenance.source_kind in {"inference", "default"} else "present"
        return SemanticValue(state=state, value=patch.value, provenance=provenance)
    state_by_operation = {
        "set_null": "explicit_null",
        "clear": "cleared",
        "mark_unknown": "unknown",
        "mark_unresolved": "unresolved",
    }
    semantic_state = state_by_operation[patch.operation]
    keep_provenance = provenance if semantic_state in {"explicit_null", "cleared"} else patch.provenance
    return SemanticValue(state=semantic_state, value=None, provenance=keep_provenance)


def _apply_patches(
    slots: Iterable[SemanticSlot],
    patches: Iterable[GoalStatePatch],
) -> tuple[list[SemanticSlot], list[GoalSemanticPath]]:
    by_path = {slot.path: slot for slot in slots}
    accepted_paths: list[GoalSemanticPath] = []
    for patch in patches:
        prior_slot = by_path.get(patch.path)
        prior = prior_slot.semantic_value if prior_slot else None
        prior_rank = (
            _PROVENANCE_PRECEDENCE.get(prior.provenance.source_kind, -1)
            if prior is not None and prior.provenance is not None
            else -1
        )
        incoming_rank = _PROVENANCE_PRECEDENCE[patch.provenance.source_kind]
        if prior is not None and incoming_rank < prior_rank:
            continue
        next_value = _semantic_value(patch, prior)
        next_slot = SemanticSlot(
            revision=(prior_slot.revision + 1 if prior_slot else 1),
            path=patch.path,
            semantic_value=next_value,
        )
        if prior_slot != next_slot:
            accepted_paths.append(patch.path)
        by_path[patch.path] = next_slot
    ordered_paths = [slot.path for slot in slots]
    ordered_paths.extend(path for path in by_path if path not in ordered_paths)
    return [by_path[path] for path in ordered_paths], accepted_paths


def rebase_open_decisions(
    decisions: Iterable[DecisionRequest], *, goal_revision: int
) -> list[DecisionRequest]:
    return [
        decision.model_copy(
            update={
                "goal_revision": goal_revision,
                "target_revision": goal_revision + 1,
            }
        )
        for decision in decisions
    ]


def _preserved_decisions_after_correction(
    decisions: Iterable[DecisionRequest], *, accepted_paths: set[GoalSemanticPath]
) -> list[DecisionRequest]:
    preserved: list[DecisionRequest] = []
    for decision in decisions:
        option_paths: set[GoalSemanticPath] = set()
        change_set_decision = False
        for option in decision.options:
            if option.target == "change_set":
                change_set_decision = True
                break
            option_paths.update(patch.path for patch in option.patches)
        if change_set_decision and accepted_paths:
            continue
        if option_paths.intersection(accepted_paths):
            continue
        preserved.append(decision)
    return preserved


def apply_correction(
    state: GoalState,
    delta: CorrectionDelta,
    *,
    decisions: list[DecisionRequest] | None = None,
) -> GoalState:
    if any(existing.delta_id == delta.delta_id for existing in state.correction_deltas):
        return state
    if state.status in TERMINAL_GOAL_STATUSES:
        raise GoalStateTransitionError(f"cannot correct terminal goal in {state.status}")
    if state.status == "committing":
        raise GoalStateTransitionError(
            f"cannot correct goal while exact approval is {state.status}"
        )
    slots, accepted_paths = _apply_patches(state.semantic_slots, delta.patches)
    changed = bool(accepted_paths)
    next_status = state.status
    approved_revision = state.approved_change_set_revision
    if changed and approved_revision is not None:
        approved_revision = None
        next_status = "planning"
    next_revision = state.revision + 1
    preserved_decisions = _preserved_decisions_after_correction(
        state.unresolved_decisions,
        accepted_paths=set(accepted_paths),
    )
    next_decisions = rebase_open_decisions(
        preserved_decisions, goal_revision=next_revision
    )
    incoming_decisions = list(decisions or [])
    existing_ids = {item.decision_id for item in next_decisions}
    if existing_ids.intersection(item.decision_id for item in incoming_decisions):
        raise GoalStateDecisionConflict("new decisions must not replace existing open decisions")
    next_decisions.extend(incoming_decisions)
    if next_decisions:
        next_status = "needs_user_decision"
    elif state.status == "needs_user_decision":
        next_status = "planning"
    return _validated_copy(
        state,
        revision=next_revision,
        semantic_slots=slots,
        correction_deltas=[
            *state.correction_deltas,
            delta.model_copy(update={"supersedes_paths": accepted_paths}),
        ],
        unresolved_decisions=next_decisions,
        approved_change_set_revision=approved_revision,
        status=next_status,
        causation_id=delta.source_turn_id,
    )


def apply_decision(
    state: GoalState,
    *,
    decision_id: str,
    option_id: str,
    source_event_id: str,
    expected_revision: int,
) -> GoalState:
    if state.status in TERMINAL_GOAL_STATUSES:
        raise GoalStateDecisionConflict(f"cannot decide terminal goal in {state.status}")
    prior_resolution = next(
        (item for item in state.resolved_decisions if item.decision_id == decision_id),
        None,
    )
    if prior_resolution is not None:
        if (
            prior_resolution.option_id == option_id
            and prior_resolution.source_event_id == source_event_id
            and expected_revision == prior_resolution.applied_revision - 1
        ):
            return state
        raise GoalStateDecisionConflict("decision is already resolved with a different option or event")
    if expected_revision != state.revision:
        raise GoalStateDecisionConflict(
            f"stale goal revision: expected {expected_revision}, current {state.revision}"
        )
    decision = next(
        (item for item in state.unresolved_decisions if item.decision_id == decision_id),
        None,
    )
    if decision is None or decision.status != "open":
        raise GoalStateDecisionConflict("decision is not open on this goal")
    if decision.goal_revision != state.revision or decision.target_revision != state.revision + 1:
        raise GoalStateDecisionConflict("decision revision does not match the active goal")
    option = next((item for item in decision.options if item.option_id == option_id), None)
    if option is None:
        raise GoalStateDecisionConflict("option does not belong to the stored decision")
    ui_patches = [
        patch.model_copy(
            update={
                "provenance": Provenance(
                    source_kind="ui",
                    source_ref=source_event_id,
                    source_text=patch.provenance.source_text,
                )
            }
        )
        for patch in option.patches
    ]
    slots, accepted_paths = _apply_patches(state.semantic_slots, ui_patches)
    if len(accepted_paths) != len(ui_patches):
        raise GoalStateDecisionConflict("selected option did not apply every stored semantic patch")
    resolved = ResolvedDecision(
        decision_id=decision_id,
        option_id=option_id,
        source_event_id=source_event_id,
        applied_revision=state.revision + 1,
    )
    remaining_decisions = rebase_open_decisions(
        (
            item
            for item in state.unresolved_decisions
            if item.decision_id != decision_id
        ),
        goal_revision=state.revision + 1,
    )
    next_status = (
        "needs_user_decision"
        if remaining_decisions
        else "planning" if state.status == "needs_user_decision" else state.status
    )
    return _validated_copy(
        state,
        revision=state.revision + 1,
        status=next_status,
        semantic_slots=slots,
        unresolved_decisions=remaining_decisions,
        resolved_decisions=[*state.resolved_decisions, resolved],
        approved_change_set_revision=None,
        causation_id=source_event_id,
    )


def register_decisions(
    state: GoalState,
    decisions: list[DecisionRequest],
    *,
    command_id: str,
) -> GoalState:
    if state.status in TERMINAL_GOAL_STATUSES:
        raise GoalStateDecisionConflict(f"cannot add a decision to terminal goal in {state.status}")
    if state.status == "committing":
        raise GoalStateDecisionConflict(
            f"cannot add a decision while exact approval is {state.status}"
        )
    if not decisions:
        raise GoalStateDecisionConflict("at least one decision is required")
    next_revision = state.revision + 1
    existing_ids = {
        item.decision_id for item in [*state.unresolved_decisions, *state.resolved_decisions]
    }
    for decision in decisions:
        if decision.decision_id in existing_ids:
            raise GoalStateDecisionConflict("decision id already exists on this goal")
        if decision.goal_id != state.goal_id or decision.goal_revision != next_revision:
            raise GoalStateDecisionConflict("decision must target the resulting goal revision")
    return _validated_copy(
        state,
        revision=next_revision,
        status="needs_user_decision",
        unresolved_decisions=[
            *rebase_open_decisions(
                state.unresolved_decisions, goal_revision=next_revision
            ),
            *decisions,
        ],
        approved_change_set_revision=None,
        causation_id=command_id,
    )
def transition_goal(state: GoalState, target_status: str, *, command_id: str) -> GoalState:
    del command_id  # Store-level audit/idempotency owns command identity.
    allowed = ALLOWED_GOAL_TRANSITIONS.get(state.status, set())
    if target_status not in allowed:
        raise GoalStateTransitionError(f"invalid goal transition {state.status} -> {target_status}")
    approved_revision = state.approved_change_set_revision
    if state.status == "approved" and target_status not in {"committing", "applied", "system_failed"}:
        approved_revision = None
    return _validated_copy(
        state,
        revision=state.revision + 1,
        status=target_status,
        approved_change_set_revision=approved_revision,
    )


def approve_goal(
    state: GoalState,
    *,
    change_set_revision: int,
    command_id: str,
) -> GoalState:
    """Approve the exact current ChangeSet revision and retain it through commit."""

    del command_id
    if state.status != "ready_for_review":
        raise GoalStateTransitionError("only a ready-for-review goal may be approved")
    if (
        state.current_change_set_id is None
        or state.current_change_set_revision != change_set_revision
    ):
        raise GoalStateTransitionError("approval must match the current ChangeSet revision")
    return _validated_copy(
        state,
        revision=state.revision + 1,
        status="approved",
        approved_change_set_revision=change_set_revision,
    )
