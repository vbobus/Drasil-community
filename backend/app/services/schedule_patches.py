"""SchedulePatch helpers for Planner proposal validation and simulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from hashlib import sha256
import json
from typing import Any

from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode

SUPPORTED_OPS = {"create_block", "update_block_time", "remove_block", "skip_task"}


class SchedulePatchExecutionError(ValueError):
    def __init__(self, message: str, *, validation: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.validation = validation or {
            "status": "invalid",
            "errors": [message],
            "warnings": [],
            "operationCount": 0,
        }


@dataclass
class SchedulePatchExecution:
    created_blocks: list[CalendarBlock]
    updated_blocks: list[CalendarBlock]
    removed_blocks: list[CalendarBlock]
    skipped_task_ids: list[int]
    validation: dict[str, Any]

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "status": "applied",
            "createdBlockIds": [block.id for block in self.created_blocks if block.id is not None],
            "updatedBlockIds": [block.id for block in self.updated_blocks if block.id is not None],
            "removedBlockIds": [block.id for block in self.removed_blocks if block.id is not None],
            "removedGoogleEvents": [
                {
                    "blockId": block.id,
                    "googleEventId": block.google_event_id,
                }
                for block in self.removed_blocks
                if block.google_event_id
            ],
            "skippedTaskIds": self.skipped_task_ids,
            "operationCount": (
                len(self.created_blocks)
                + len(self.updated_blocks)
                + len(self.removed_blocks)
                + len(self.skipped_task_ids)
            ),
        }


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _dedupe_ints(values: list[int]) -> list[int]:
    deduped: list[int] = []
    for value in values:
        if isinstance(value, int) and value not in deduped:
            deduped.append(value)
    return deduped


def build_schedule_patch(
    *,
    intent_summary: str,
    working_set_task_ids: list[int],
    window_start: datetime,
    window_end: datetime,
    operations: list[dict[str, Any]],
    patch_id: str | None = None,
) -> dict[str, Any]:
    """Build the canonical SchedulePatch-shaped proposal payload."""

    normalized_ids = _dedupe_ints(working_set_task_ids)
    identity_payload = {
        "workingSetTaskIds": normalized_ids,
        "windowStart": _iso(window_start),
        "windowEnd": _iso(window_end),
        "operations": operations,
    }
    identity_hash = sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    stable_patch_id = patch_id or f"schedule-patch-{identity_hash}"
    return {
        "patchId": stable_patch_id,
        "intentSummary": intent_summary.strip() or "Review this schedule proposal.",
        "targetScope": "selected_working_set",
        "workingSetTaskIds": normalized_ids,
        "windowStart": _iso(window_start),
        "windowEnd": _iso(window_end),
        "operations": operations,
    }


def _operation_range(operation: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    return _parse_datetime(operation.get("startAt")), _parse_datetime(operation.get("endAt"))


def _ranges_overlap(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
) -> bool:
    return first_start < second_end and first_end > second_start


def _validate_block_operation(
    operation: dict[str, Any],
    *,
    op_index: int,
    tasks_by_id: dict[int, StrategyNode],
    accepted_blocks: list[CalendarBlock],
    accepted_blocks_by_id: dict[int, CalendarBlock],
    vacated_accepted_block_ids: set[int],
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[str], list[str], int | None]:
    errors: list[str] = []
    warnings: list[str] = []
    task_id = _as_int(operation.get("taskId"))
    category_id = _as_int(operation.get("categoryId"))
    if task_id is None:
        errors.append(f"Operation {op_index + 1} is missing taskId.")
    elif task_id not in tasks_by_id:
        errors.append(f"Operation {op_index + 1} references missing task {task_id}.")
    if category_id is None:
        errors.append(f"Operation {op_index + 1} is missing categoryId.")
    elif task_id is not None and task_id in tasks_by_id:
        task = tasks_by_id[task_id]
        if task.category_id != category_id:
            errors.append(
                f"Operation {op_index + 1} category {category_id} does not match task {task_id}."
            )

    start_at, end_at = _operation_range(operation)
    if start_at is None:
        errors.append(f"Operation {op_index + 1} has invalid startAt.")
    if end_at is None:
        errors.append(f"Operation {op_index + 1} has invalid endAt.")
    if start_at is not None and end_at is not None:
        if end_at <= start_at:
            errors.append(f"Operation {op_index + 1} endAt must be after startAt.")
        if start_at < window_start or end_at > window_end:
            errors.append(f"Operation {op_index + 1} falls outside the submitted window.")
        accepted_block_id = _as_int(operation.get("acceptedBlockId"))
        for block in accepted_blocks:
            if accepted_block_id is not None and block.id == accepted_block_id:
                continue
            if block.id is not None and int(block.id) in vacated_accepted_block_ids:
                continue
            block_start = _parse_datetime(block.start_at)
            block_end = _parse_datetime(block.end_at)
            if block_start is None or block_end is None:
                continue
            if _ranges_overlap(start_at, end_at, block_start, block_end):
                errors.append(
                    f"Operation {op_index + 1} overlaps accepted calendar block {block.id or 0}."
                )

    op = str(operation.get("op") or "").strip()
    proposal_block_id = str(operation.get("proposalBlockId") or "").strip()
    accepted_block_id = _as_int(operation.get("acceptedBlockId"))
    if op == "create_block" and not proposal_block_id:
        errors.append(f"Operation {op_index + 1} is missing proposalBlockId.")
    if op == "update_block_time":
        if not proposal_block_id and accepted_block_id is None:
            errors.append(
                f"Operation {op_index + 1} must target proposalBlockId or acceptedBlockId."
            )
        if accepted_block_id is not None:
            accepted_block = accepted_blocks_by_id.get(accepted_block_id)
            if accepted_block is None:
                errors.append(
                    f"Operation {op_index + 1} references missing accepted block {accepted_block_id}."
                )
            elif task_id is not None and accepted_block.task_id != task_id:
                errors.append(
                    f"Operation {op_index + 1} accepted block {accepted_block_id} does not belong to task {task_id}."
                )
            else:
                expected_start_raw = operation.get("expectedOriginalStartAt")
                expected_end_raw = operation.get("expectedOriginalEndAt")
                if expected_start_raw is not None or expected_end_raw is not None:
                    expected_start = _parse_datetime(expected_start_raw)
                    expected_end = _parse_datetime(expected_end_raw)
                    if expected_start is None or expected_end is None:
                        errors.append(
                            f"Operation {op_index + 1} has an invalid reviewed block version."
                        )
                    elif (
                        _parse_datetime(accepted_block.start_at) != expected_start
                        or _parse_datetime(accepted_block.end_at) != expected_end
                    ):
                        errors.append(
                            f"Accepted calendar block {accepted_block_id} changed after review; prepare a fresh proposal."
                        )
    return errors, warnings, task_id


def validate_schedule_patch(
    schedule_patch: dict[str, Any],
    *,
    tasks: list[StrategyNode],
    accepted_blocks: list[CalendarBlock],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """Validate a SchedulePatch against live task and accepted calendar state."""

    errors: list[str] = []
    warnings: list[str] = []
    submitted_window_start = _parse_datetime(schedule_patch.get("windowStart"))
    submitted_window_end = _parse_datetime(schedule_patch.get("windowEnd"))
    if submitted_window_start is None or submitted_window_end is None:
        errors.append("SchedulePatch has an invalid submitted window.")
    elif submitted_window_end <= submitted_window_start:
        errors.append("SchedulePatch windowEnd must be after windowStart.")
    elif submitted_window_start != window_start or submitted_window_end != window_end:
        errors.append("SchedulePatch window does not match the submitted Planner window.")
    operations = schedule_patch.get("operations")
    if not isinstance(operations, list):
        operations = []
        errors.append("SchedulePatch operations must be a list.")
    if not operations:
        errors.append("SchedulePatch has no operations.")

    tasks_by_id = {task.id: task for task in tasks if task.id is not None}
    accepted_blocks_by_id = {
        block.id: block for block in accepted_blocks if block.id is not None
    }
    vacated_accepted_block_ids = {
        accepted_block_id
        for operation in operations
        if isinstance(operation, dict)
        and str(operation.get("op") or "").strip() in {"update_block_time", "remove_block"}
        and (accepted_block_id := _as_int(operation.get("acceptedBlockId"))) is not None
    }
    seen_task_ops: set[tuple[str, int]] = set()
    seen_create_ranges: set[tuple[int, datetime | None, datetime | None]] = set()
    seen_update_targets: set[tuple[int, int | None, str]] = set()
    seen_task_mutations: dict[int, str] = {}
    ranged_operations: list[tuple[int, datetime, datetime, int | None]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            errors.append(f"Operation {index + 1} must be an object.")
            continue
        op = str(operation.get("op") or "").strip()
        task_id = _as_int(operation.get("taskId"))
        if op not in SUPPORTED_OPS:
            errors.append(f"Operation {index + 1} has unsupported op {op or 'unknown'}.")
            continue
        if task_id is not None:
            key = (op, task_id)
            duplicate_create = False
            if op == "create_block":
                create_start, create_end = _operation_range(operation)
                create_signature = (task_id, create_start, create_end)
                duplicate_create = create_signature in seen_create_ranges
                seen_create_ranges.add(create_signature)
            duplicate_update = False
            if op == "update_block_time":
                update_signature = (
                    task_id,
                    _as_int(operation.get("acceptedBlockId")),
                    str(operation.get("proposalBlockId") or "").strip(),
                )
                duplicate_update = update_signature in seen_update_targets
                seen_update_targets.add(update_signature)
            if key in seen_task_ops and (
                (op == "create_block" and duplicate_create)
                or (op == "update_block_time" and duplicate_update)
                or op not in {"create_block", "update_block_time"}
            ):
                errors.append(
                    f"SchedulePatch contains duplicate {op} operations for task {task_id}."
                )
            seen_task_ops.add(key)
            previous_op = seen_task_mutations.get(task_id)
            if previous_op is not None and previous_op != op:
                errors.append(
                    f"SchedulePatch contains incompatible {previous_op} and {op} operations for task {task_id}."
                )
            else:
                seen_task_mutations[task_id] = op
        if op in {"create_block", "update_block_time"}:
            op_errors, op_warnings, _task_id = _validate_block_operation(
                operation,
                op_index=index,
                tasks_by_id=tasks_by_id,
                accepted_blocks=accepted_blocks,
                accepted_blocks_by_id=accepted_blocks_by_id,
                vacated_accepted_block_ids=vacated_accepted_block_ids,
                window_start=window_start,
                window_end=window_end,
            )
            errors.extend(op_errors)
            warnings.extend(op_warnings)
            start_at, end_at = _operation_range(operation)
            if start_at is not None and end_at is not None and end_at > start_at:
                ranged_operations.append((index, start_at, end_at, task_id))
        elif op in {"remove_block", "skip_task"}:
            if task_id is None:
                errors.append(f"Operation {index + 1} is missing taskId.")
            elif task_id not in tasks_by_id:
                errors.append(f"Operation {index + 1} references missing task {task_id}.")
            if op == "remove_block":
                proposal_block_id = str(operation.get("proposalBlockId") or "").strip()
                accepted_block_id = _as_int(operation.get("acceptedBlockId"))
                if not proposal_block_id and accepted_block_id is None:
                    errors.append(
                        f"Operation {index + 1} must target proposalBlockId or acceptedBlockId."
                    )
                if accepted_block_id is not None:
                    accepted_block = accepted_blocks_by_id.get(accepted_block_id)
                    if accepted_block is None:
                        errors.append(
                            f"Operation {index + 1} references missing accepted block {accepted_block_id}."
                        )
                    elif task_id is not None and accepted_block.task_id != task_id:
                        errors.append(
                            f"Operation {index + 1} accepted block {accepted_block_id} does not belong to task {task_id}."
                        )
            if op == "skip_task":
                warnings.append(
                    f"Operation {index + 1} records a skipped task without writing a calendar block."
                )

    for position, (index, start_at, end_at, task_id) in enumerate(ranged_operations):
        for other_index, other_start, other_end, other_task_id in ranged_operations[position + 1 :]:
            if _ranges_overlap(start_at, end_at, other_start, other_end):
                errors.append(
                    "Operations "
                    f"{index + 1} and {other_index + 1} overlap"
                    + (
                        f" for tasks {task_id} and {other_task_id}."
                        if task_id is not None and other_task_id is not None
                        else "."
                    )
                )

    return {
        "status": "invalid" if errors else "warning" if warnings else "valid",
        "errors": errors,
        "warnings": warnings,
        "operationCount": len(operations),
    }


def _preview_block_from_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(operation.get("proposalBlockId") or f"proposal-{operation.get('taskId')}"),
        "taskId": operation.get("taskId"),
        "categoryId": operation.get("categoryId"),
        "title": operation.get("title"),
        "emoji": operation.get("emoji"),
        "categoryName": operation.get("categoryName"),
        "categoryColor": operation.get("categoryColor"),
        "startAt": operation.get("startAt"),
        "endAt": operation.get("endAt"),
        "variant": operation.get("variant") or "proposed",
        "completionStatus": operation.get("completionStatus") or "not_started",
        "taskStatus": operation.get("taskStatus"),
        "taskDeadlineAt": operation.get("taskDeadlineAt"),
        "taskPriority": operation.get("taskPriority"),
        "timeEstimateMinutes": operation.get("timeEstimateMinutes"),
        "isPinned": bool(operation.get("isPinned", False)),
        "isProtected": bool(operation.get("isProtected", False)),
        "isRecurring": bool(operation.get("isRecurring", False)),
        "recurrenceSeriesId": operation.get("recurrenceSeriesId"),
        "recurrenceKey": operation.get("recurrenceKey"),
        "hierarchyDepth": int(operation.get("hierarchyDepth") or 0),
        "renderedChildTaskIds": operation.get("renderedChildTaskIds") or [],
    }


def simulate_schedule_patch(
    schedule_patch: dict[str, Any],
    *,
    tasks: list[StrategyNode],
    accepted_blocks: list[CalendarBlock],
    validation: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a lightweight SchedulePatch blast-radius preview."""

    operations = schedule_patch.get("operations")
    if not isinstance(operations, list):
        operations = []
    task_ids: set[int] = set()
    accepted_block_ids: set[int] = set()
    preview_blocks: list[dict[str, Any]] = []
    skipped_task_ids: set[int] = set()
    counts = {
        "createBlocks": 0,
        "updateBlockTimes": 0,
        "removeBlocks": 0,
        "skipTasks": 0,
    }

    live_task_ids = {task.id for task in tasks if task.id is not None}
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        task_id = _as_int(operation.get("taskId"))
        if task_id is not None:
            task_ids.add(task_id)
        accepted_block_id = _as_int(operation.get("acceptedBlockId"))
        if accepted_block_id is not None:
            accepted_block_ids.add(accepted_block_id)
        op = str(operation.get("op") or "").strip()
        if op == "create_block":
            counts["createBlocks"] += 1
            preview_blocks.append(_preview_block_from_operation(operation))
        elif op == "update_block_time":
            counts["updateBlockTimes"] += 1
            preview_blocks.append(_preview_block_from_operation(operation))
        elif op == "remove_block":
            counts["removeBlocks"] += 1
        elif op == "skip_task":
            counts["skipTasks"] += 1
            if task_id is not None:
                skipped_task_ids.add(task_id)

    return {
        "operationCount": len(operations),
        "affectedTaskIds": sorted(task_id for task_id in task_ids if task_id in live_task_ids),
        "affectedCalendarBlockIds": sorted(accepted_block_ids),
        "previewBlocks": preview_blocks,
        "skippedTaskIds": sorted(skipped_task_ids),
        "conflicts": list(conflicts or []),
        "warnings": list((validation or {}).get("warnings") or []),
        **counts,
    }


def revise_schedule_patch_for_alternative(
    session: Session,
    *,
    source_proposal: dict[str, Any],
    conflict_index: int,
    alternative_index: int,
) -> dict[str, Any]:
    """Build a fresh proposal from one exact persisted conflict alternative."""

    payload = source_proposal.get("payload")
    planner_payload = payload.get("payload") if isinstance(payload, dict) else None
    outer_conflicts = payload.get("conflicts") if isinstance(payload, dict) else None
    conflict_payload = payload if isinstance(outer_conflicts, list) else planner_payload
    conflicts = conflict_payload.get("conflicts") if isinstance(conflict_payload, dict) else None
    if not isinstance(conflicts, list):
        simulation = source_proposal.get("schedulePatchSimulation") or source_proposal.get(
            "schedule_patch_simulation"
        )
        conflicts = simulation.get("conflicts") if isinstance(simulation, dict) else None
    if not isinstance(conflicts, list) or not (0 <= conflict_index < len(conflicts)):
        raise ValueError("The selected schedule conflict is no longer available.")
    conflict = conflicts[conflict_index]
    alternatives = conflict.get("alternatives") if isinstance(conflict, dict) else None
    if not isinstance(alternatives, list) or not (0 <= alternative_index < len(alternatives)):
        raise ValueError("The selected schedule alternative is no longer available.")
    alternative = alternatives[alternative_index]
    if not isinstance(alternative, dict):
        raise ValueError("The selected schedule alternative is invalid.")
    task_id = _as_int(conflict.get("taskId"))
    task = session.get(StrategyNode, task_id) if task_id is not None else None
    if task is None or task.id is None:
        raise ValueError("The task for this schedule alternative no longer exists.")
    category = session.get(Category, task.category_id)
    if category is None:
        raise ValueError("The task category for this schedule alternative no longer exists.")
    start_at = _parse_datetime(alternative.get("startAt"))
    end_at = _parse_datetime(alternative.get("endAt"))
    if start_at is None or end_at is None or end_at <= start_at:
        raise ValueError("The selected schedule alternative has an invalid time range.")

    source_patch = source_proposal.get("schedulePatch") or source_proposal.get("schedule_patch")
    if not isinstance(source_patch, dict):
        raise ValueError("The source SchedulePatch is missing.")
    source_window_start = _parse_datetime(source_patch.get("windowStart"))
    source_window_end = _parse_datetime(source_patch.get("windowEnd"))
    if source_window_start is None or source_window_end is None:
        raise ValueError("The source SchedulePatch window is invalid.")
    window_start = min(source_window_start, start_at)
    window_end = max(source_window_end, end_at)
    alternative_search_end = datetime.combine(
        max(source_window_end, end_at).date() + timedelta(days=1),
        time.min,
    )
    existing_operations = source_patch.get("operations")
    operations = [
        dict(operation)
        for operation in existing_operations
        if isinstance(operation, dict) and _as_int(operation.get("taskId")) != task.id
    ] if isinstance(existing_operations, list) else []
    duration_minutes = int((end_at - start_at).total_seconds() // 60)
    operation_mode = str(
        (conflict_payload or {}).get("operationMode")
        or (payload or {}).get("operationMode")
        or "create"
    )
    accepted_block_id: int | None = None
    accepted_block: CalendarBlock | None = None
    if operation_mode == "reschedule":
        persisted_block_ids = {
            block_id
            for operation in existing_operations or []
            if isinstance(operation, dict)
            and _as_int(operation.get("taskId")) == task.id
            and (block_id := _as_int(operation.get("acceptedBlockId"))) is not None
        }
        if len(persisted_block_ids) > 1:
            raise ValueError(
                "The persisted reschedule proposal targets multiple accepted blocks for this task."
            )
        matching_blocks = [
            block
            for block in session.exec(
                select(CalendarBlock).where(CalendarBlock.task_id == task.id)
            ).all()
            if block.id is not None
        ]
        if persisted_block_ids:
            accepted_block_id = next(iter(persisted_block_ids))
            accepted_block = next(
                (block for block in matching_blocks if block.id == accepted_block_id),
                None,
            )
            if accepted_block is None:
                raise ValueError("The accepted calendar block to reschedule no longer exists.")
        elif not matching_blocks:
            raise ValueError("The accepted calendar block to reschedule no longer exists.")
        elif len(matching_blocks) > 1:
            raise ValueError(
                "This task has multiple accepted calendar blocks. Choose the exact block before rescheduling."
            )
        else:
            accepted_block = matching_blocks[0]
            accepted_block_id = accepted_block.id
    operation: dict[str, Any] = {
        "op": "update_block_time" if accepted_block_id is not None else "create_block",
        "proposalBlockId": f"proposal-{task.id}-{start_at.strftime('%Y%m%dT%H%M%S')}",
        "taskId": task.id,
        "categoryId": task.category_id,
        "title": task.title,
        "emoji": task.emoji,
        "categoryName": category.name,
        "categoryColor": category.accent_color,
        "startAt": _iso(start_at),
        "endAt": _iso(end_at),
        "variant": "proposed",
        "completionStatus": "not_started",
        "taskStatus": task.status,
        "taskDeadlineAt": task.deadline_at,
        "taskPriority": task.priority,
        "timeEstimateMinutes": task.time_estimate_minutes,
        "allocatedTimeboxMinutes": duration_minutes,
        "isPinned": False,
        "isProtected": False,
        "isRecurring": False,
        "hierarchyDepth": 0,
        "renderedChildTaskIds": [],
        "source": "conflict_alternative",
    }
    if accepted_block_id is not None:
        operation["acceptedBlockId"] = accepted_block_id
        if accepted_block is not None:
            operation["expectedOriginalStartAt"] = _iso(accepted_block.start_at)
            operation["expectedOriginalEndAt"] = _iso(accepted_block.end_at)
    operations.append(operation)

    working_set_ids = [
        value
        for value in source_patch.get("workingSetTaskIds", [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if task.id not in working_set_ids:
        working_set_ids.append(task.id)
    patch = build_schedule_patch(
        intent_summary=str(source_patch.get("intentSummary") or "Use the selected conflict alternative."),
        working_set_task_ids=working_set_ids,
        window_start=window_start,
        window_end=window_end,
        operations=operations,
    )
    tasks = list(
        session.exec(select(StrategyNode).where(StrategyNode.id.in_(working_set_ids))).all()
    )
    accepted_blocks = list(
        session.exec(
            select(CalendarBlock)
            .where(CalendarBlock.start_at < alternative_search_end)
            .where(CalendarBlock.end_at > window_start)
        ).all()
    )
    validation = validate_schedule_patch(
        patch,
        tasks=tasks,
        accepted_blocks=accepted_blocks,
        window_start=window_start,
        window_end=window_end,
    )
    remaining_conflicts = [
        dict(item)
        for index, item in enumerate(conflicts)
        if index != conflict_index and isinstance(item, dict)
    ]
    simulation = simulate_schedule_patch(
        patch,
        tasks=tasks,
        accepted_blocks=accepted_blocks,
        validation=validation,
        conflicts=remaining_conflicts,
    )
    proposal_id = patch["patchId"].replace("schedule-patch", "schedule-proposal", 1)
    next_payload = dict(payload or {})
    next_payload["conflicts"] = remaining_conflicts
    if isinstance(planner_payload, dict):
        next_planner_payload = dict(planner_payload)
        next_planner_payload["conflicts"] = remaining_conflicts
        next_payload["payload"] = next_planner_payload
    next_payload["schedulePatchLifecycle"] = {"status": "pending"}
    next_payload["revisedFromProposalId"] = (
        source_proposal.get("proposalId") or source_proposal.get("proposal_id")
    )
    revised = {
        **source_proposal,
        "proposalId": proposal_id,
        "summary": "Prepared a revised schedule using the selected alternative.",
        "preview": [
            f"{item.get('title') or 'Task'}, {item.get('startAt')} - {item.get('endAt')}"
            for item in operations
            if item.get("startAt") and item.get("endAt")
        ],
        "validationWarnings": validation["warnings"],
        "schedulePatch": patch,
        "schedulePatchValidation": validation,
        "schedulePatchSimulation": simulation,
        "schedulePatchStatus": validation["status"],
        "payload": next_payload,
    }
    if validation["status"] == "invalid":
        duration = end_at - start_at
        candidate_start = max(start_at, source_window_start)
        candidate_end = candidate_start + duration
        relevant_blocks = sorted(
            (block for block in accepted_blocks if block.id != accepted_block_id),
            key=lambda item: item.start_at,
        )
        while candidate_end <= alternative_search_end:
            overlap = next(
                (
                    block
                    for block in relevant_blocks
                    if _ranges_overlap(
                        candidate_start,
                        candidate_end,
                        block.start_at,
                        block.end_at,
                    )
                ),
                None,
            )
            if overlap is None:
                break
            candidate_start = max(candidate_start, overlap.end_at)
            candidate_end = candidate_start + duration
        refreshed_alternatives = (
            [
                {
                    "startAt": _iso(candidate_start),
                    "endAt": _iso(candidate_end),
                    "reason": "First available same-day time after the live calendar change.",
                }
            ]
            if candidate_end <= alternative_search_end
            else []
        )
        refreshed_conflict = {
            **dict(conflict),
            "detail": (
                f"{task.title} can no longer use the selected time because the live calendar changed. "
                "Choose the refreshed alternative, edit the request, or reject this proposal."
            ),
            "alternatives": refreshed_alternatives,
        }
        refreshed_conflicts = [*remaining_conflicts]
        refreshed_conflicts.insert(min(conflict_index, len(refreshed_conflicts)), refreshed_conflict)
        stale_payload = dict(next_payload)
        stale_payload["conflicts"] = refreshed_conflicts
        stale_payload["revisionStale"] = True
        nested = stale_payload.get("payload")
        if isinstance(nested, dict):
            nested = dict(nested)
            nested["conflicts"] = refreshed_conflicts
            stale_payload["payload"] = nested
        stale_simulation = dict(simulation)
        stale_simulation["conflicts"] = refreshed_conflicts
        revised.update(
            {
                "proposalId": (
                    source_proposal.get("proposalId") or source_proposal.get("proposal_id")
                ),
                "summary": "The selected time became unavailable; refreshed alternatives are ready.",
                "schedulePatch": source_patch,
                "schedulePatchSimulation": stale_simulation,
                "payload": stale_payload,
            }
        )
    return revised


def apply_approved_schedule_patch(
    session: Session,
    schedule_patch: dict[str, Any],
) -> SchedulePatchExecution:
    """Live-validate and stage a complete approved SchedulePatch transaction."""

    window_start = _parse_datetime(schedule_patch.get("windowStart"))
    window_end = _parse_datetime(schedule_patch.get("windowEnd"))
    if window_start is None or window_end is None or window_end <= window_start:
        raise SchedulePatchExecutionError("The SchedulePatch window is invalid.")
    operations = schedule_patch.get("operations")
    if not isinstance(operations, list):
        raise SchedulePatchExecutionError("SchedulePatch operations must be a list.")
    task_ids = {
        task_id
        for operation in operations
        if isinstance(operation, dict)
        and (task_id := _as_int(operation.get("taskId"))) is not None
    }
    tasks = (
        list(session.exec(select(StrategyNode).where(StrategyNode.id.in_(task_ids))).all())
        if task_ids
        else []
    )
    accepted_blocks = list(session.exec(select(CalendarBlock)).all())
    validation = validate_schedule_patch(
        schedule_patch,
        tasks=tasks,
        accepted_blocks=accepted_blocks,
        window_start=window_start,
        window_end=window_end,
    )
    accepted_by_id = {
        block.id: block for block in accepted_blocks if block.id is not None
    }
    safety_errors: list[str] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        if str(operation.get("op") or "") not in {"update_block_time", "remove_block"}:
            continue
        block_id = _as_int(operation.get("acceptedBlockId"))
        block = accepted_by_id.get(block_id) if block_id is not None else None
        if block is not None and (block.is_protected or block.is_pinned):
            state = "protected" if block.is_protected else "pinned"
            safety_errors.append(
                f"Operation {index + 1} cannot change {state} calendar block {block.id}."
            )
    if safety_errors:
        validation["errors"].extend(safety_errors)
        validation["status"] = "invalid"
    if validation["status"] == "invalid":
        raise SchedulePatchExecutionError(
            "This schedule proposal changed or is no longer safe to apply. Review a fresh proposal.",
            validation=validation,
        )

    created_blocks: list[CalendarBlock] = []
    updated_blocks: list[CalendarBlock] = []
    removed_blocks: list[CalendarBlock] = []
    skipped_task_ids: list[int] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "")
        task_id = _as_int(operation.get("taskId"))
        if op == "skip_task":
            if task_id is not None:
                skipped_task_ids.append(task_id)
            continue
        if op == "create_block":
            start_at, end_at = _operation_range(operation)
            block = CalendarBlock(
                task_id=int(task_id),
                category_id=int(operation["categoryId"]),
                start_at=start_at,
                end_at=end_at,
                variant="scheduled",
                completion_status=str(operation.get("completionStatus") or "not_started"),
                is_pinned=bool(operation.get("isPinned", False)),
                is_protected=bool(operation.get("isProtected", False)),
                is_recurring=bool(operation.get("isRecurring", False)),
                recurrence_series_id=(
                    str(operation.get("recurrenceSeriesId"))
                    if operation.get("recurrenceSeriesId")
                    else None
                ),
                recurrence_key=(
                    str(operation.get("recurrenceKey"))
                    if operation.get("recurrenceKey")
                    else None
                ),
            )
            session.add(block)
            created_blocks.append(block)
            continue
        block_id = _as_int(operation.get("acceptedBlockId"))
        block = accepted_by_id.get(block_id) if block_id is not None else None
        if block is None:
            raise SchedulePatchExecutionError("An accepted calendar target disappeared during approval.")
        if op == "update_block_time":
            start_at, end_at = _operation_range(operation)
            block.start_at = start_at
            block.end_at = end_at
            session.add(block)
            updated_blocks.append(block)
        elif op == "remove_block":
            removed_blocks.append(block)
            session.delete(block)
    session.flush()
    return SchedulePatchExecution(
        created_blocks=created_blocks,
        updated_blocks=updated_blocks,
        removed_blocks=removed_blocks,
        skipped_task_ids=skipped_task_ids,
        validation=validation,
    )
