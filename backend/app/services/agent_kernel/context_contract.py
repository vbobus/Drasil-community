"""Canonical model-facing context helpers for the Strategist agent kernel."""

from __future__ import annotations

from datetime import date
from typing import Any


_RECURRENCE_CONTRACT_FIELDS = (
    "target_temp_id",
    "frequency",
    "days_of_week",
    "start_date",
    "end_date",
    "occurrence_count",
    "end_mode",
    "duration_minutes",
    "preferred_start_time",
    "allowed_start_time",
    "allowed_end_time",
    "gap_minutes",
    "timezone",
    "strict",
    "conflict_relocation",
)


def _first_present(mapping: dict[str, Any], snake_key: str) -> Any:
    if snake_key in mapping:
        return mapping[snake_key]
    parts = snake_key.split("_")
    camel_key = parts[0] + "".join(part.title() for part in parts[1:])
    return mapping.get(camel_key)


def _compact_structural_operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_operations = payload.get(
        "structural_operations",
        payload.get("structuralOperations"),
    )
    if not isinstance(raw_operations, list):
        return []
    operations: list[dict[str, Any]] = []
    for raw_operation in raw_operations[:100]:
        if not isinstance(raw_operation, dict):
            continue
        operation = {
            key: raw_operation[key]
            for key in (
                "kind",
                "op",
                "temp_id",
                "node_id",
                "title",
                "parent_id",
                "parent_temp_id",
                "parent_task_title",
                "destination_parent_id",
                "destination_parent_task_title",
                "target_category_id",
                "target_category_name",
                "category_id",
                "time_estimate_minutes",
                "estimate_minutes",
                "duration_minutes",
                "updates",
            )
            if key in raw_operation
        }
        fields = raw_operation.get("fields")
        if isinstance(fields, dict):
            operation["fields"] = {
                key: fields[key]
                for key in (
                    "title",
                    "emoji",
                    "priority",
                    "status",
                    "planned_date",
                    "deadline_at",
                    "time_estimate_minutes",
                )
                if key in fields
            }
        if operation:
            operations.append(task_contract_surface(operation))
    return operations


def _compact_created_task_schedule_contract(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    raw_schedule = payload.get(
        "created_task_schedule",
        payload.get("createdTaskSchedule"),
    )
    if not isinstance(raw_schedule, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "target_temp_ids",
        "target_task_ids",
        "instruction",
        "window_start",
        "window_end",
        "slot_minutes",
        "request_constraints",
    ):
        value = _first_present(raw_schedule, key)
        if value is not None:
            compact[key] = value
    raw_groups = _first_present(raw_schedule, "schedule_groups")
    if isinstance(raw_groups, list):
        groups: list[dict[str, Any]] = []
        for raw_group in raw_groups[:100]:
            if not isinstance(raw_group, dict):
                continue
            group = {
                key: value
                for key in (
                    "target_temp_ids",
                    "target_task_ids",
                    "instruction",
                    "window_start",
                    "window_end",
                    "slot_minutes",
                    "request_constraints",
                )
                if (value := _first_present(raw_group, key)) is not None
            }
            if group:
                groups.append(group)
        if groups:
            compact["schedule_groups"] = groups
    return compact or None


def _compact_displaced_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Group conflict instances without copying a long occurrence-by-occurrence plan."""

    resolutions = plan.get("conflict_resolutions", plan.get("conflictResolutions"))
    if not isinstance(resolutions, list):
        return []
    groups: dict[tuple[Any, str], dict[str, Any]] = {}
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            continue
        resolution_date = str(resolution.get("date") or "").strip()
        blockers = resolution.get("blockers")
        if not isinstance(blockers, list):
            continue
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            task_id = blocker.get("taskId", blocker.get("task_id"))
            title = str(
                blocker.get("taskTitle", blocker.get("task_title")) or ""
            ).strip()
            key = (task_id if isinstance(task_id, int) else None, title.lower())
            group = groups.setdefault(
                key,
                {
                    "task_id": task_id if isinstance(task_id, int) else None,
                    "task_title": title or None,
                    "priority": blocker.get("priority"),
                    "is_recurring": bool(
                        blocker.get("isRecurring", blocker.get("is_recurring"))
                    ),
                    "affected_dates": [],
                    "original_times": [],
                    "proposed_times": [],
                    "accepted_block_ids": [],
                },
            )
            if resolution_date and resolution_date not in group["affected_dates"]:
                group["affected_dates"].append(resolution_date)
            original_time = (
                blocker.get("startAt", blocker.get("start_at")),
                blocker.get("endAt", blocker.get("end_at")),
            )
            if all(original_time) and original_time not in group["original_times"]:
                group["original_times"].append(original_time)
            proposed_time = (
                blocker.get("movedStartAt", blocker.get("moved_start_at")),
                blocker.get("movedEndAt", blocker.get("moved_end_at")),
            )
            if all(proposed_time) and proposed_time not in group["proposed_times"]:
                group["proposed_times"].append(proposed_time)
            block_id = blocker.get("blockId", blocker.get("block_id"))
            if isinstance(block_id, int) and block_id not in group["accepted_block_ids"]:
                group["accepted_block_ids"].append(block_id)

    weekday_order = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    compacted: list[dict[str, Any]] = []
    for group in groups.values():
        dates = sorted(str(value) for value in group.pop("affected_dates") if value)
        weekdays: list[str] = []
        for value in dates:
            try:
                weekday = date.fromisoformat(value).strftime("%A").lower()
            except ValueError:
                continue
            if weekday not in weekdays:
                weekdays.append(weekday)
        weekdays.sort(key=lambda value: weekday_order.get(value, 99))
        compacted.append(
            {
                **group,
                "affected_occurrence_count": len(dates),
                "affected_weekdays": weekdays,
                "affected_date_range": (
                    {"start_date": dates[0], "end_date": dates[-1]}
                    if dates
                    else None
                ),
                "original_times": [
                    {"start_at": start_at, "end_at": end_at}
                    for start_at, end_at in group["original_times"][:6]
                ],
                "proposed_times": [
                    {"start_at": start_at, "end_at": end_at}
                    for start_at, end_at in group["proposed_times"][:6]
                ],
                "accepted_block_ids": group["accepted_block_ids"][:100],
            }
        )
    return compacted[:20]


def _compact_recurring_schedule_contract(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw_schedule = payload.get("recurring_schedule", payload.get("recurringSchedule"))
    plan = payload.get("recurring_schedule_plan", payload.get("recurringSchedulePlan"))
    if not isinstance(raw_schedule, dict) and not isinstance(plan, dict):
        return None
    raw_schedule = raw_schedule if isinstance(raw_schedule, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    raw_end_mode = str(
        _first_present(raw_schedule, "end_mode")
        or _first_present(plan, "end_mode")
        or ""
    ).strip().lower()
    contract: dict[str, Any] = {}
    for key in _RECURRENCE_CONTRACT_FIELDS:
        # The plan's end/count describe its rolling materialization preview.
        # They are not durable recurrence limits for an ongoing series.
        if raw_end_mode == "never" and key in {"end_date", "occurrence_count"}:
            continue
        value = _first_present(raw_schedule, key)
        if value is None:
            value = _first_present(plan, key)
        if value is not None:
            contract[key] = value
    if isinstance(contract.get("days_of_week"), list) and contract["days_of_week"]:
        # The weekday set is more specific than a contradictory model-emitted
        # `frequency=daily`; expose one canonical recurrence truth to later
        # turns so an edit cannot expand weekdays into every day.
        contract["frequency"] = "weekly"
    task_title = plan.get("task_title", plan.get("taskTitle"))
    if isinstance(task_title, str) and task_title.strip():
        contract["task_title"] = task_title.strip()
    displaced_tasks = _compact_displaced_tasks(plan)
    if displaced_tasks:
        contract["conflict_resolution"] = {
            "incoming_task_keeps_requested_time": True,
            "displaced_existing_tasks": displaced_tasks,
            "scope_invariant": (
                "Move only the listed accepted calendar blocks on dates where they conflict. "
                "Do not create new occurrences for a displaced task or copy the incoming "
                "task's recurrence onto it."
            ),
        }
    return contract


def _pending_proposal_semantic_contract(
    pending_proposal: dict[str, Any],
) -> dict[str, Any] | None:
    payload = pending_proposal.get("payload")
    if not isinstance(payload, dict):
        return None
    proposal_kind = str(pending_proposal.get("proposal_kind") or "").strip()
    contract: dict[str, Any] = {
        "proposal_family": proposal_kind or None,
        "primary_target_task_id": pending_proposal.get(
            "target_task_id",
            pending_proposal.get("target_node_id"),
        ),
        "primary_target_task_title": pending_proposal.get(
            "target_task_title",
            pending_proposal.get("target_node_title"),
        ),
        "target_category_id": _first_present(payload, "target_category_id"),
        "target_category_name": _first_present(payload, "target_category_name"),
    }
    structural_operations = _compact_structural_operations(payload)
    if structural_operations:
        contract["structural_operations"] = structural_operations
    created_task_schedule = _compact_created_task_schedule_contract(payload)
    if created_task_schedule is not None:
        contract["created_task_schedule"] = created_task_schedule
    recurrence = _compact_recurring_schedule_contract(payload)
    if recurrence is not None:
        contract["recurring_schedule"] = recurrence
        contract["revision_invariant"] = (
            "Treat a requested edit as a revision of this proposal. Preserve its primary "
            "task, recurrence, operations, and unaffected fields. Existing conflict blockers "
            "are affected resources, not replacement schedule targets."
        )
    updates = payload.get("updates")
    if isinstance(updates, dict) and updates:
        contract["metadata_updates"] = task_contract_surface(updates)
    return contract if any(value is not None for value in contract.values()) else None


TASK_SURFACE_KEY_RENAMES = {
    "node_id": "task_id",
    "node_ids": "task_ids",
    "node_title": "task_title",
    "target_node_id": "target_task_id",
    "target_node_ids": "target_task_ids",
    "target_node_title": "target_task_title",
    "targetNodeId": "target_task_id",
    "targetNodeIds": "target_task_ids",
    "targetNodeTitle": "target_task_title",
    "staged_node_ids": "staged_task_ids",
    "focus_node_id": "focus_task_id",
    "input_focus_node_id": "input_focus_task_id",
    "selected_node_id": "selected_task_id",
    "parent_id": "parent_task_id",
    "source_node_id": "source_task_id",
    "destination_parent_id": "destination_parent_task_id",
    "subtree_root_ids": "task_branch_root_ids",
    "relevant_subtree_root_ids": "relevant_task_branch_root_ids",
    "observed_nodes": "observed_tasks",
    "node_count": "task_count",
    "included_nodes": "included_tasks",
    "total_nodes": "total_tasks",
}

TASK_ITEM_PARENT_KEYS = {
    "task",
    "branch",
    "children",
    "siblings",
    "parents",
    "related",
    "tasks",
    "matches",
    "staged_tasks",
    "working_set",
    "roots",
    "observed_tasks",
}
CATEGORY_PARENT_KEYS = {"active_category", "categories"}


def _looks_like_task_item(value: dict[str, Any], parent_key: str | None) -> bool:
    if parent_key in TASK_ITEM_PARENT_KEYS:
        return True
    return any(
        key in value
        for key in (
            "node_type",
            "parent_id",
            "priority",
            "planned_date",
            "deadline_at",
            "time_estimate_minutes",
            "child_count",
        )
    ) and "title" in value


def task_contract_surface(value: Any, *, parent_key: str | None = None) -> Any:
    """Convert persistence/retrieval naming into the agent's task vocabulary."""
    if isinstance(value, list):
        return [task_contract_surface(item, parent_key=parent_key) for item in value]
    if not isinstance(value, dict):
        return value

    task_like = _looks_like_task_item(value, parent_key)
    category_like = parent_key in CATEGORY_PARENT_KEYS
    converted: dict[str, Any] = {}
    for raw_key, raw_child in value.items():
        if raw_key == "id":
            if task_like:
                key = "task_id"
            elif category_like:
                key = "category_id"
            else:
                key = raw_key
        else:
            key = TASK_SURFACE_KEY_RENAMES.get(raw_key, raw_key)
        converted[key] = task_contract_surface(raw_child, parent_key=key)
    return converted


def compact_pending_proposal_for_model(
    pending_proposal: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Expose pending proposal continuity without leaking raw persistence payloads."""
    if not isinstance(pending_proposal, dict):
        return None
    compact = {
        "proposal_id": pending_proposal.get("proposal_id"),
        "proposal_kind": pending_proposal.get("proposal_kind"),
        "status": pending_proposal.get("status"),
        "target_task_id": pending_proposal.get(
            "target_task_id",
            pending_proposal.get("target_node_id"),
        ),
        "target_task_ids": pending_proposal.get(
            "target_task_ids",
            pending_proposal.get("target_node_ids", []),
        ),
        "source_turn_id": pending_proposal.get("source_turn_id"),
        "target_task_title": pending_proposal.get(
            "target_task_title",
            pending_proposal.get("target_node_title"),
        ),
        "summary": pending_proposal.get("summary"),
        "rationale": pending_proposal.get("rationale"),
    }
    payload = pending_proposal.get("payload")
    revision_of_proposal_id = (
        str(payload.get("revision_of_proposal_id") or "").strip()
        if isinstance(payload, dict)
        else ""
    )
    if revision_of_proposal_id:
        compact["revision_of_proposal_id"] = revision_of_proposal_id
    semantic_contract = _pending_proposal_semantic_contract(pending_proposal)
    if semantic_contract is not None:
        compact["semantic_contract"] = semantic_contract
    return compact
