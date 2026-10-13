"""Typed, request-scoped scheduling constraints.

The model may interpret natural language, but exact scheduling requirements must
cross the Planner boundary as data.  This module normalizes that data, compiles
it into solver inputs, and validates the resulting operations before handoff.
It intentionally contains no persistence: these constraints apply to one user
request and sit above remembered preferences for that turn.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from app.services.planner_tools import PlannerWorkingSet
from app.services.scheduling_solver import OccupiedInterval


SUPPORTED_REQUEST_CONSTRAINTS = {
    "unavailable_interval",
    "allowed_window",
    "exact_start",
    "task_order",
    "minimum_gap",
    "contiguous",
}

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def constraint_safe_slot_minutes(requested: int, constraints: Any) -> int:
    """An exact appointment owns its clock time, never the solver's grid.

    A coarse model-supplied grid can round 12:45 up to 13:00 before validation,
    falsely rejecting a valid appointment. Minute precision also preserves the
    appointment's duration rather than rounding it up to a grid-sized block.
    This changes solver resolution only; bounds and conflict checks still apply.
    """
    if isinstance(constraints, list) and any(
        isinstance(item, dict)
        and item.get('kind') == 'exact_start'
        and item.get('hard', True)
        for item in constraints
    ):
        return 1
    return requested


def _local_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed


def _task_ids(value: Any, live_task_ids: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            int(item)
            for item in value
            if isinstance(item, int)
            and not isinstance(item, bool)
            and int(item) in live_task_ids
        )
    )


def _rebase_blocked_interval_to_window(
    start_at: datetime,
    end_at: datetime,
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    """Repair a blocked clock interval whose model-generated date drifted.

    The user's natural-language clock time is authoritative; the ISO date is a
    transport detail produced by the model.  When that date falls completely
    outside the Planner window, try the same local clock time on each date in
    the requested window. This keeps any verbally supplied unavailable time
    useful even if the model resolved it to an adjacent day.
    """

    duration = end_at - start_at
    if duration <= timedelta(0) or duration > timedelta(days=1):
        return None
    candidate_date = window_start.date()
    final_date = (window_end - timedelta(microseconds=1)).date()
    if candidate_date <= start_at.date() <= final_date:
        # The date itself is already in scope. If the interval does not overlap
        # (for example, a commitment ended exactly when scheduling begins), it is
        # simply satisfied; do not invent a recurrence on a later window day.
        return None
    while candidate_date <= final_date:
        candidate_start = datetime.combine(candidate_date, start_at.time())
        candidate_end = candidate_start + duration
        if candidate_start < window_end and candidate_end > window_start:
            return candidate_start, candidate_end
        candidate_date += timedelta(days=1)
    return None


def _daily_intervals(
    start_at: datetime,
    end_at: datetime,
    *,
    window_start: datetime,
    window_end: datetime,
    weekdays: set[int] | None,
) -> list[tuple[datetime, datetime]]:
    """Expand a recurring local-clock interval over the Planner window."""

    intervals: list[tuple[datetime, datetime]] = []
    candidate_date = window_start.date()
    final_date = (window_end - timedelta(microseconds=1)).date()
    while candidate_date <= final_date:
        if weekdays is None or candidate_date.weekday() in weekdays:
            candidate_start = datetime.combine(candidate_date, start_at.time())
            candidate_end = datetime.combine(candidate_date, end_at.time())
            if candidate_end <= candidate_start:
                candidate_end += timedelta(days=1)
            if candidate_start < window_end and candidate_end > window_start:
                intervals.append(
                    (max(candidate_start, window_start), min(candidate_end, window_end))
                )
        candidate_date += timedelta(days=1)
    return intervals


def _canonical_intervals(constraint: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    raw_intervals = constraint.get("intervals")
    if isinstance(raw_intervals, list):
        parsed: list[tuple[datetime, datetime]] = []
        for raw in raw_intervals:
            if not isinstance(raw, dict):
                continue
            start_at = _local_datetime(raw.get("startAt"))
            end_at = _local_datetime(raw.get("endAt"))
            if start_at is not None and end_at is not None and end_at > start_at:
                parsed.append((start_at, end_at))
        if parsed:
            return parsed
    start_at = _local_datetime(constraint.get("startAt"))
    end_at = _local_datetime(constraint.get("endAt"))
    if start_at is not None and end_at is not None and end_at > start_at:
        return [(start_at, end_at)]
    return []


def normalize_request_constraints(
    raw_constraints: Any,
    *,
    live_task_ids: set[int],
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return canonical constraints plus errors that make a handoff unsafe."""

    if raw_constraints is None:
        return [], []
    if not isinstance(raw_constraints, list):
        return [], ["Request constraints must be a list."]

    constraints: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_constraints):
        if not isinstance(raw, dict):
            errors.append(f"Constraint {index + 1} must be an object.")
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in SUPPORTED_REQUEST_CONSTRAINTS:
            errors.append(f"Constraint {index + 1} uses unsupported kind {kind!r}.")
            continue
        constraint: dict[str, Any] = {
            "kind": kind,
            "hard": bool(raw.get("hard", True)),
            "label": str(raw.get("label") or "Requested constraint").strip(),
            "sourceText": str(raw.get("source_text") or raw.get("sourceText") or "").strip(),
            "taskIds": _task_ids(raw.get("task_ids", raw.get("taskIds")), live_task_ids),
        }
        start_at = _local_datetime(raw.get("start_at", raw.get("startAt")))
        end_at = _local_datetime(raw.get("end_at", raw.get("endAt")))
        if kind in {"unavailable_interval", "allowed_window"}:
            recurrence = str(raw.get("recurrence") or "once").strip().lower()
            if recurrence not in {"once", "daily"}:
                errors.append(f"Constraint {index + 1} has an unsupported recurrence.")
                continue
            if start_at is None or end_at is None or (
                recurrence == "once" and end_at <= start_at
            ):
                errors.append(f"Constraint {index + 1} needs a valid local start and end time.")
                continue
            if recurrence == "daily":
                raw_days = raw.get("days_of_week", raw.get("daysOfWeek"))
                weekdays: set[int] | None = None
                if raw_days is not None:
                    if not isinstance(raw_days, list):
                        errors.append(f"Constraint {index + 1} days of week must be a list.")
                        continue
                    normalized_days = {
                        WEEKDAY_NAMES.get(str(value).strip().lower())
                        for value in raw_days
                    }
                    if None in normalized_days:
                        errors.append(f"Constraint {index + 1} has an invalid day of week.")
                        continue
                    weekdays = {int(value) for value in normalized_days if value is not None}
                intervals = _daily_intervals(
                    start_at,
                    end_at,
                    window_start=window_start,
                    window_end=window_end,
                    weekdays=weekdays,
                )
                if not intervals:
                    if kind == "unavailable_interval":
                        continue
                    errors.append(
                        f"Constraint {index + 1} has no allowed time in the scheduling window."
                    )
                    continue
                constraint["recurrence"] = "daily"
                if raw_days is not None:
                    constraint["daysOfWeek"] = [
                        name
                        for name, value in WEEKDAY_NAMES.items()
                        if value in (weekdays or set())
                    ]
                constraint["intervals"] = [
                    {"startAt": item_start.isoformat(), "endAt": item_end.isoformat()}
                    for item_start, item_end in intervals
                ]
                constraint["startAt"] = intervals[0][0].isoformat()
                constraint["endAt"] = intervals[0][1].isoformat()
                constraints.append(constraint)
                continue
            if end_at <= window_start or start_at >= window_end:
                if kind == "unavailable_interval":
                    original_start_at = start_at
                    original_end_at = end_at
                    repaired = _rebase_blocked_interval_to_window(
                        start_at,
                        end_at,
                        window_start=window_start,
                        window_end=window_end,
                    )
                    if repaired is None:
                        # A blocked interval wholly outside the active window is
                        # already satisfied or irrelevant. It must never force
                        # the user to clarify an otherwise valid request.
                        continue
                    start_at, end_at = repaired
                    constraint["normalization"] = "rebased_clock_to_scheduling_window"
                    constraint["originalStartAt"] = original_start_at.isoformat()
                    constraint["originalEndAt"] = original_end_at.isoformat()
                else:
                    errors.append(f"Constraint {index + 1} is outside the scheduling window.")
                    continue
            constraint["startAt"] = max(start_at, window_start).isoformat()
            constraint["endAt"] = min(end_at, window_end).isoformat()
            constraint["intervals"] = [
                {"startAt": constraint["startAt"], "endAt": constraint["endAt"]}
            ]
        elif kind == "exact_start":
            if start_at is None or not (window_start <= start_at < window_end):
                errors.append(f"Constraint {index + 1} needs an exact start inside the scheduling window.")
                continue
            if len(constraint["taskIds"]) != 1:
                errors.append(f"Constraint {index + 1} must target exactly one task.")
                continue
            constraint["startAt"] = start_at.isoformat()
        elif kind == "task_order":
            before_id = raw.get("before_task_id", raw.get("beforeTaskId"))
            after_id = raw.get("after_task_id", raw.get("afterTaskId"))
            if before_id not in live_task_ids or after_id not in live_task_ids or before_id == after_id:
                errors.append(f"Constraint {index + 1} needs two different live task IDs.")
                continue
            constraint["beforeTaskId"] = int(before_id)
            constraint["afterTaskId"] = int(after_id)
        elif kind == "minimum_gap":
            minutes = raw.get("minutes")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= 1440:
                errors.append(f"Constraint {index + 1} needs gap minutes from 0 to 1440.")
                continue
            if constraint["taskIds"] and len(constraint["taskIds"]) != 2:
                errors.append(f"Constraint {index + 1} must target two tasks or the whole ordered set.")
                continue
            constraint["minutes"] = minutes
        elif kind == "contiguous":
            if len(constraint["taskIds"]) != 1:
                errors.append(f"Constraint {index + 1} must target exactly one task.")
                continue
        constraints.append(constraint)
    return constraints, errors


def constraint_occupied_intervals(
    constraints: list[dict[str, Any]],
) -> list[OccupiedInterval]:
    intervals: list[OccupiedInterval] = []
    for constraint in constraints:
        if constraint.get("kind") != "unavailable_interval" or not constraint.get("hard", True):
            continue
        for start_at, end_at in _canonical_intervals(constraint):
            intervals.append(
                OccupiedInterval(
                    block_id=-(950_000 + len(intervals)),
                    start_at=start_at,
                    end_at=end_at,
                    source_type="request_constraint",
                    source_label=str(
                        constraint.get("label") or "Requested unavailable time"
                    ),
                    report_conflict=False,
                )
            )
    return intervals


def apply_request_constraints(
    working_set: PlannerWorkingSet,
    constraints: list[dict[str, Any]],
    *,
    slot_minutes: int,
) -> PlannerWorkingSet:
    """Compile ordering, gap, and allowed-time constraints into solver input."""

    tasks = list(working_set.tasks)
    by_id = {task.task_id: task for task in tasks}

    # A stable topological pass is sufficient for the small, already bounded
    # working sets handled by Planner. Cycles remain in submitted order and are
    # rejected by post-solve validation.
    order_edges = [
        (int(item["beforeTaskId"]), int(item["afterTaskId"]))
        for item in constraints
        if item.get("kind") == "task_order" and item.get("hard", True)
    ]
    for _ in range(max(1, len(tasks) ** 2)):
        changed = False
        positions = {task.task_id: index for index, task in enumerate(tasks)}
        for before_id, after_id in order_edges:
            if positions.get(before_id, -1) > positions.get(after_id, -1):
                task = tasks.pop(positions[before_id])
                after_index = next(index for index, item in enumerate(tasks) if item.task_id == after_id)
                tasks.insert(after_index, task)
                changed = True
                break
        if not changed:
            break

    global_gap = max(
        [
            int(item["minutes"])
            for item in constraints
            if item.get("kind") == "minimum_gap"
            and item.get("hard", True)
            and not item.get("taskIds")
        ]
        or [0]
    )
    for index, task in enumerate(tasks):
        before_gap = max(task.gap_before_minutes, global_gap if index > 0 else 0)
        after_gap = max(task.gap_after_minutes, global_gap if index < len(tasks) - 1 else 0)
        by_id[task.task_id] = replace(task, gap_before_minutes=before_gap, gap_after_minutes=after_gap)

    for item in constraints:
        if not item.get("hard", True):
            continue
        kind = item.get("kind")
        ids = item.get("taskIds") if isinstance(item.get("taskIds"), list) else []
        if kind == "minimum_gap" and len(ids) == 2:
            first, second = int(ids[0]), int(ids[1])
            minutes = int(item["minutes"])
            if first in by_id:
                by_id[first] = replace(by_id[first], gap_after_minutes=max(by_id[first].gap_after_minutes, minutes))
            if second in by_id:
                by_id[second] = replace(by_id[second], gap_before_minutes=max(by_id[second].gap_before_minutes, minutes))
        elif kind == "allowed_window":
            targets = ids or list(by_id)
            intervals = _canonical_intervals(item)
            if intervals:
                for task_id in targets:
                    if task_id in by_id:
                        by_id[task_id] = replace(
                            by_id[task_id],
                            allowed_start_windows=(
                                *by_id[task_id].allowed_start_windows,
                                *intervals,
                            ),
                        )
        elif kind == "exact_start" and len(ids) == 1:
            start_at = _local_datetime(item.get("startAt"))
            task_id = int(ids[0])
            if start_at is not None and task_id in by_id:
                by_id[task_id] = replace(
                    by_id[task_id],
                    allowed_start_windows=((
                        start_at,
                        start_at + timedelta(minutes=by_id[task_id].duration_minutes),
                    ),),
                )

    return replace(working_set, tasks=[by_id[task.task_id] for task in tasks])


def validate_operations_against_request_constraints(
    operations: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return user-meaningful hard-constraint violations."""

    scheduled: list[tuple[int, datetime, datetime]] = []
    for operation in operations:
        task_id = operation.get("taskId")
        start_at = _local_datetime(operation.get("startAt"))
        end_at = _local_datetime(operation.get("endAt"))
        if isinstance(task_id, int) and start_at is not None and end_at is not None:
            scheduled.append((task_id, start_at, end_at))
    by_task: dict[int, list[tuple[datetime, datetime]]] = {}
    for task_id, start_at, end_at in scheduled:
        by_task.setdefault(task_id, []).append((start_at, end_at))

    violations: list[dict[str, Any]] = []
    for constraint in constraints:
        if not constraint.get("hard", True):
            continue
        kind = str(constraint.get("kind") or "")
        label = str(constraint.get("label") or "Requested constraint")
        ids = constraint.get("taskIds") if isinstance(constraint.get("taskIds"), list) else []
        violated = False
        if kind == "unavailable_interval":
            intervals = _canonical_intervals(constraint)
            violated = bool(
                intervals
                and any(
                    item_start < blocked_end and item_end > blocked_start
                    for blocked_start, blocked_end in intervals
                    for _, item_start, item_end in scheduled
                )
            )
        elif kind == "allowed_window":
            intervals = _canonical_intervals(constraint)
            targets = set(ids) if ids else set(by_task)
            violated = bool(
                intervals
                and (
                    any(task_id not in by_task for task_id in targets)
                    or any(
                        not any(
                            item_start >= allowed_start and item_end <= allowed_end
                            for allowed_start, allowed_end in intervals
                        )
                        for task_id in targets
                        for item_start, item_end in by_task.get(task_id, [])
                    )
                )
            )
        elif kind == "exact_start" and len(ids) == 1:
            expected = _local_datetime(constraint.get("startAt"))
            starts = sorted(start for start, _ in by_task.get(int(ids[0]), []))
            violated = expected is not None and (not starts or starts[0] != expected)
        elif kind == "task_order":
            before = by_task.get(int(constraint["beforeTaskId"]), [])
            after = by_task.get(int(constraint["afterTaskId"]), [])
            violated = bool(
                not before
                or not after
                or max(end for _, end in before) > min(start for start, _ in after)
            )
        elif kind == "minimum_gap":
            minutes = int(constraint.get("minutes") or 0)
            if len(ids) == 2:
                first = by_task.get(int(ids[0]), [])
                second = by_task.get(int(ids[1]), [])
                violated = bool(
                    not first
                    or not second
                    or min(start for start, _ in second) - max(end for _, end in first) < timedelta(minutes=minutes)
                )
            else:
                ordered = sorted(scheduled, key=lambda item: item[1])
                violated = any(
                    following[1] - current[2] < timedelta(minutes=minutes)
                    for current, following in zip(ordered, ordered[1:])
                    if current[0] != following[0]
                )
        elif kind == "contiguous" and len(ids) == 1:
            violated = len(by_task.get(int(ids[0]), [])) != 1
        if violated:
            violations.append(
                {
                    "kind": kind,
                    "label": label,
                    "sourceText": constraint.get("sourceText"),
                    "message": f"The proposal does not satisfy: {label}.",
                }
            )
    return violations
