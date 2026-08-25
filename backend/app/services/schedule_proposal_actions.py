"""Deterministic typed actions for durable SchedulePatch proposals."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock
from typing import Any

from sqlmodel import Session

from app.models.calendar_block import CalendarBlock
from app.models.strategist_chat import StrategistChatTurn
from app.services.agent_kernel.task_patch_runtime import apply_task_patch
from app.services.change_sets import update_change_set_status
from app.services.recurring_schedules import create_recurrence_series_from_plan
from app.services.schedule_patches import (
    apply_approved_schedule_patch,
    revise_schedule_patch_for_alternative,
)
from app.services.strategist_sessions import (
    ScheduleProposalActionError,
    parse_turn_response_payload,
    resolve_schedule_proposal_action_source,
    set_schedule_proposal_lifecycle,
)


@dataclass
class ScheduleProposalActionResult:
    response: dict[str, Any]
    sync_created_or_updated: list[CalendarBlock] = field(default_factory=list)
    sync_removed: list[CalendarBlock] = field(default_factory=list)
    sync_removed_events: list[dict[str, Any]] = field(default_factory=list)


_PROPOSAL_LOCKS_GUARD = Lock()


@dataclass
class _ProposalLockEntry:
    lock: RLock = field(default_factory=RLock)
    users: int = 0


_PROPOSAL_LOCKS: dict[str, _ProposalLockEntry] = {}


@contextmanager
def schedule_proposal_action_lock(ui_event: dict[str, Any] | None):
    """Serialize one proposal's lifecycle transition through the commit boundary."""

    if not isinstance(ui_event, dict) or str(ui_event.get("kind") or "") != "proposal_action":
        yield
        return
    key = str(
        ui_event.get("proposal_id")
        or ui_event.get("proposalId")
        or ui_event.get("source_turn_id")
        or ui_event.get("sourceTurnId")
        or "unknown-proposal"
    )
    with _PROPOSAL_LOCKS_GUARD:
        entry = _PROPOSAL_LOCKS.setdefault(key, _ProposalLockEntry())
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _PROPOSAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _PROPOSAL_LOCKS.get(key) is entry:
                _PROPOSAL_LOCKS.pop(key, None)


def is_schedule_proposal_ui_event(
    session: Session,
    *,
    strategist_session_id: str,
    ui_event: dict[str, Any] | None,
) -> bool:
    if not isinstance(ui_event, dict) or str(ui_event.get("kind") or "") != "proposal_action":
        return False
    declared_kind = str(
        ui_event.get("proposal_kind") or ui_event.get("proposalKind") or ""
    )
    if declared_kind == "schedule_patch":
        return True
    source_turn_id = str(ui_event.get("source_turn_id") or ui_event.get("sourceTurnId") or "")
    turn = session.get(StrategistChatTurn, source_turn_id) if source_turn_id else None
    if turn is None:
        return False
    response = parse_turn_response_payload(turn)
    proposal = response.get("proposal") if isinstance(response, dict) else None
    return isinstance(proposal, dict) and str(
        proposal.get("proposalKind") or proposal.get("proposal_kind") or ""
    ) == "schedule_patch"


def _response(
    *,
    summary: str,
    action_result: dict[str, Any],
    proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "providerLabel": "Planner",
        "mode": "schedule_patch",
        "summary": summary,
        "contextCards": [],
        "suggestedPrompts": [],
        "proposal": proposal,
        "actionResult": action_result,
        "trace": {
            "summary": "Handled an exact SchedulePatch UI action without a model call.",
            "steps": [
                {
                    "kind": "decision",
                    "title": "Schedule proposal action",
                    "detail": summary,
                }
            ],
        },
    }


def handle_schedule_proposal_action(
    session: Session,
    *,
    strategist_session_id: str,
    action_turn_id: str,
    ui_event: dict[str, Any] | None,
) -> ScheduleProposalActionResult | None:
    """Handle a SchedulePatch typed action, or return None for other proposal families."""

    if not is_schedule_proposal_ui_event(
        session,
        strategist_session_id=strategist_session_id,
        ui_event=ui_event,
    ):
        return None
    assert isinstance(ui_event, dict)
    source_turn_id = str(ui_event.get("source_turn_id") or ui_event.get("sourceTurnId") or "")
    proposal_id = str(ui_event.get("proposal_id") or ui_event.get("proposalId") or "")
    action = str(ui_event.get("action") or "").strip().lower()
    if action not in {"approve", "edit", "reject"}:
        raise ValueError("Schedule proposal action must be approve, edit, or reject.")
    allowed_statuses = {"pending", "applied"} if action == "approve" else (
        {"pending", "rejected"} if action == "reject" else {"pending"}
    )
    source = resolve_schedule_proposal_action_source(
        session,
        strategist_session_id=strategist_session_id,
        source_turn_id=source_turn_id,
        proposal_id=proposal_id,
        allowed_statuses=allowed_statuses,
    )
    change_set_id = str(
        source.proposal.get("changeSetId")
        or source.proposal.get("change_set_id")
        or ""
    ).strip() or None
    proposal_status = str(
        source.proposal.get("schedulePatchStatus")
        or source.proposal.get("schedule_patch_status")
        or "invalid"
    )
    if action == "approve" and proposal_status not in {"valid", "warning"}:
        raise ScheduleProposalActionError(
            "This schedule proposal is dirty or invalid. Generate a fresh proposal before approval."
        )

    if action == "reject":
        if source.lifecycle_status == "pending":
            set_schedule_proposal_lifecycle(
                session,
                source=source,
                status="rejected",
                action_turn_id=action_turn_id,
            )
            update_change_set_status(
                session,
                change_set_id=change_set_id,
                status="rejected",
                outcome="rejected",
            )
        return ScheduleProposalActionResult(
            response=_response(
                summary="The schedule proposal was rejected. No calendar changes were made.",
                action_result={
                    "proposalId": proposal_id,
                    "sourceTurnId": source_turn_id,
                    "status": "rejected",
                    "idempotent": source.lifecycle_status == "rejected",
                },
            )
        )

    if action == "approve":
        if source.lifecycle_status == "applied":
            lifecycle = source.proposal.get("payload", {}).get("schedulePatchLifecycle", {})
            execution = dict(lifecycle.get("execution") or {})
            retry_block_ids = {
                int(block_id)
                for key in ("createdBlockIds", "updatedBlockIds")
                for block_id in execution.get(key, [])
                if isinstance(block_id, int) and not isinstance(block_id, bool)
            }
            retry_blocks = [
                block
                for block_id in sorted(retry_block_ids)
                if (block := session.get(CalendarBlock, block_id)) is not None
            ]
            retry_removed_events = [
                dict(item)
                for item in execution.get("removedGoogleEvents", [])
                if isinstance(item, dict) and str(item.get("googleEventId") or "")
            ]
            return ScheduleProposalActionResult(
                response=_response(
                    summary="This schedule proposal was already applied.",
                    action_result={
                        **execution,
                        "proposalId": proposal_id,
                        "sourceTurnId": source_turn_id,
                        "status": "applied",
                        "idempotent": True,
                    },
                ),
                sync_created_or_updated=retry_blocks,
                sync_removed_events=retry_removed_events,
            )
        patch = source.proposal.get("schedulePatch") or source.proposal.get("schedule_patch")
        if not isinstance(patch, dict):
            raise ValueError("The persisted SchedulePatch is missing.")
        patch = {
            **patch,
            "operations": [
                dict(operation) if isinstance(operation, dict) else operation
                for operation in patch.get("operations", [])
            ],
        }
        task_patch = source.proposal.get("taskPatch") or source.proposal.get("task_patch")
        try:
            task_result = (
                apply_task_patch(session, task_patch=task_patch, commit=False)
                if isinstance(task_patch, dict)
                else None
            )
            proposal_payload = source.proposal.get("payload")
            recurring_plan = (
                proposal_payload.get("recurringSchedulePlan")
                or proposal_payload.get("recurring_schedule_plan")
                if isinstance(proposal_payload, dict)
                else None
            )
            if isinstance(recurring_plan, dict):
                create_operations = [
                    operation
                    for operation in patch.get("operations", [])
                    if isinstance(operation, dict)
                    and str(operation.get("op") or "") == "create_block"
                ]
                if not create_operations:
                    raise ValueError("The recurring schedule has no calendar occurrences.")
                task_id = int(create_operations[0]["taskId"])
                category_id = int(create_operations[0]["categoryId"])
                series = create_recurrence_series_from_plan(
                    session,
                    plan=recurring_plan,
                    task_id=task_id,
                    category_id=category_id,
                    source_change_set_id=change_set_id,
                )
                for operation in create_operations:
                    operation["recurrenceSeriesId"] = series.id
                    operation.setdefault(
                        "recurrenceKey",
                        str(operation.get("startAt") or "")[:10],
                    )
            execution = apply_approved_schedule_patch(session, patch)
        except Exception:
            session.rollback()
            raise
        updated_task_ids = [
            int(node.id)
            for node in (task_result or {}).get("affected_nodes", [])
            if node.id is not None
        ]
        execution_payload = {
            **execution.payload,
            "updatedTaskIds": updated_task_ids,
        }
        set_schedule_proposal_lifecycle(
            session,
            source=source,
            status="applied",
            action_turn_id=action_turn_id,
            execution=execution_payload,
        )
        update_change_set_status(
            session,
            change_set_id=change_set_id,
            status="applied",
            outcome="applied",
            commit=True,
        )
        return ScheduleProposalActionResult(
            response=_response(
                summary=(
                    "The task details and schedule are now updated."
                    if updated_task_ids
                    else "The approved schedule is now on your calendar."
                ),
                action_result={
                    **execution_payload,
                    "proposalId": proposal_id,
                    "sourceTurnId": source_turn_id,
                    "idempotent": False,
                },
            ),
            sync_created_or_updated=[*execution.created_blocks, *execution.updated_blocks],
            sync_removed=execution.removed_blocks,
            sync_removed_events=[
                {
                    "blockId": block.id,
                    "googleEventId": block.google_event_id,
                }
                for block in execution.removed_blocks
                if block.google_event_id
            ],
        )

    payload = ui_event.get("payload")
    conflict_index = payload.get("conflictIndex") if isinstance(payload, dict) else None
    alternative_index = payload.get("alternativeIndex") if isinstance(payload, dict) else None
    if type(conflict_index) is not int or type(alternative_index) is not int:
        return ScheduleProposalActionResult(
            response=_response(
                summary=(
                    "Tell the AI workspace what you want changed, or choose one of the exact "
                    "time alternatives shown on the conflict. No calendar changes were made."
                ),
                action_result={
                    "proposalId": proposal_id,
                    "sourceTurnId": source_turn_id,
                    "status": "edit_requested",
                    "idempotent": False,
                },
            )
        )
    revised = revise_schedule_patch_for_alternative(
        session,
        source_proposal=source.proposal,
        conflict_index=conflict_index,
        alternative_index=alternative_index,
    )
    revised.setdefault("operation", "schedule")
    revised.setdefault("legacyResponseKey", "scheduleProposal")
    revised.setdefault("title", "Revised schedule proposal")
    revised.setdefault("body", "Review the selected conflict alternative before approval.")
    revised.setdefault("targetNodeIds", revised.get("schedulePatch", {}).get("workingSetTaskIds", []))
    revised.setdefault("validationWarnings", [])
    if revised.get("schedulePatchStatus") == "invalid":
        return ScheduleProposalActionResult(
            response=_response(
                summary=(
                    "That time is no longer available. I refreshed the conflict choices; "
                    "no calendar changes were made."
                ),
                proposal=revised,
                action_result={
                    "proposalId": proposal_id,
                    "sourceTurnId": source_turn_id,
                    "status": "stale",
                    "idempotent": False,
                },
            )
        )
    revised_proposal_id = str(revised.get("proposalId") or "")
    set_schedule_proposal_lifecycle(
        session,
        source=source,
        status="superseded",
        action_turn_id=action_turn_id,
        superseded_by_proposal_id=revised_proposal_id,
    )
    update_change_set_status(
        session,
        change_set_id=change_set_id,
        status="superseded",
        outcome="superseded",
    )
    return ScheduleProposalActionResult(
        response=_response(
            summary="A revised schedule is ready using the selected alternative.",
            proposal=revised,
            action_result={
                "proposalId": revised_proposal_id,
                "sourceProposalId": proposal_id,
                "sourceTurnId": source_turn_id,
                "status": "revised",
                "idempotent": False,
            },
        )
    )
