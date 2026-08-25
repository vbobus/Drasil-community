"""Recurring schedule proposals for one task.

Finite requests are materialized completely. Open-ended requests persist a
recurrence rule and materialize a rolling preview horizon as ordinary
CalendarBlock rows, keeping approval, rendering, and sync behavior consistent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import re
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.models.category import Category
from app.models.calendar_block import CalendarBlock
from app.models.recurrence_series import RecurrenceSeries
from app.models.node import StrategyNode
from app.services.planner_tools import read_calendar_state
from app.services.preference_memory import (
    build_preference_context_for_scope,
    constraint_intervals_from_context,
    inter_task_gap_policy_from_context,
    inter_task_gap_minutes_for_duration,
)
from app.services.scheduling_solver import OccupiedInterval, SolverTask, solve_schedule_draft
from app.services.calendar_conflict_policy import (
    blocker_payload,
    decide_calendar_collision,
    overlapping_blockers,
    prompt_has_explicit_clock_time,
)
from app.services.agent_kernel.recurrence_materialization import (
    DEFAULT_RECURRENCE_MATERIALIZATION_HORIZON_DAYS,
)


WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
WEEKDAY_ALIASES = {
    name: index for index, name in enumerate(WEEKDAY_NAMES)
} | {
    name[:3]: index for index, name in enumerate(WEEKDAY_NAMES)
}
DEFAULT_ROLLING_HORIZON_DAYS = (
    DEFAULT_RECURRENCE_MATERIALIZATION_HORIZON_DAYS
)


def preference_override_authorized_from_prompt(prompt: str | None) -> bool:
    """Return whether the current request explicitly authorizes overriding memory.

    Exact placement and override authorization are deliberately separate. Model
    output may mark a recurrence as strict to preserve its clock time, but only
    current user language can authorize bypassing a conflicting saved rule.
    """

    text = str(prompt or "").strip().casefold().replace("’", "'")
    if not text:
        return False
    return any(
        re.search(pattern, text, re.IGNORECASE) is not None
        for pattern in (
            r"\bstrict(?:\s+constraint|\s+requirement|\s+time)?\b",
            r"\b(?:fixed|non[-\s]?negotiable|immovable)\b",
            r"\b(?:can(?:not|'t)|cant)\s+(?:be\s+)?chang(?:e|ed)\b",
            r"\b(?:must|should)\s+not\s+(?:move|change)\b",
            r"\b(?:do\s+not|don't)\s+(?:move|change)\b",
            r"\boverrid(?:e|es|ing)\b.{0,80}\b(?:saved|usual|preference|rule)\b",
        )
    )


def gap_override_authorized_from_prompt(prompt: str | None) -> bool:
    """Require explicit language before bypassing a remembered transition gap."""

    text = str(prompt or "").strip().casefold().replace("â€™", "'")
    if not text:
        return False
    return any(
        re.search(pattern, text, re.IGNORECASE) is not None
        for pattern in (
            r"\b(?:ignore|override|skip|remove)\b.{0,40}\b(?:gap|buffer|transition)\b",
            r"\b(?:no|without)\s+(?:a\s+)?(?:gap|buffer|transition(?:\s+time)?)\b",
            r"\bback[-\s]?to[-\s]?back\b",
            r"\bstrict\s+(?:constraint|requirement|time)\b",
            r"\bnon[-\s]?negotiable\b",
        )
    )


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_date(value: Any) -> date | None:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


def _parse_weekdays(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    weekdays: list[int] = []
    for item in value:
        weekday: int | None = None
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 6:
            weekday = item
        elif isinstance(item, str):
            weekday = WEEKDAY_ALIASES.get(item.strip().lower())
        if weekday is not None and weekday not in weekdays:
            weekdays.append(weekday)
    return weekdays


def _matches_recurrence_day(
    candidate: date,
    *,
    frequency: str,
    weekdays: list[int],
) -> bool:
    return frequency == "daily" or candidate.weekday() in weekdays


def _end_date_for_occurrence_count(
    *,
    start_date: date,
    occurrence_count: int,
    frequency: str,
    weekdays: list[int],
) -> date:
    cursor = start_date
    remaining = occurrence_count
    while True:
        if _matches_recurrence_day(cursor, frequency=frequency, weekdays=weekdays):
            remaining -= 1
            if remaining == 0:
                return cursor
        cursor += timedelta(days=1)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _human_clock(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _human_date(value: date) -> str:
    return value.strftime("%a, %b %d").replace(" 0", " ")


def _ceil_to_five_minutes(value: datetime) -> datetime:
    trimmed = value.replace(second=0, microsecond=0)
    remainder = trimmed.minute % 5
    if value.second or value.microsecond or remainder:
        return trimmed + timedelta(minutes=5 - remainder if remainder else 5)
    return trimmed


def _calendar_occupied(blocks: list[CalendarBlock]) -> list[OccupiedInterval]:
    return [
        OccupiedInterval(
            block_id=int(block.id or 0),
            start_at=block.start_at,
            end_at=block.end_at,
            source_type="calendar",
        )
        for block in blocks
    ]


def _relocate_recurring_blocker(
    *,
    blocker: Any,
    occupied: list[OccupiedInterval],
    displaced_block_ids: set[int],
    requested_start: datetime,
    requested_end: datetime,
    gap_minutes: int,
    conflict_relocation: str,
) -> tuple[dict[str, Any] | None, OccupiedInterval | None]:
    block = blocker.block
    duration_minutes = int((block.end_at - block.start_at).total_seconds() // 60)
    if duration_minutes <= 0 or block.id is None:
        return None, None
    available_occupied = [
        interval
        for interval in occupied
        if int(interval.block_id) not in displaced_block_ids
    ]
    proposed_incoming = OccupiedInterval(
        block_id=-999_999,
        start_at=requested_start,
        end_at=requested_end,
        source_type="draft",
        report_conflict=False,
    )
    placement_start: datetime | None = None
    placement_end: datetime | None = None
    if conflict_relocation == "before_incoming":
        # Keep the moved block as close to the incoming commitment as possible,
        # while preserving the same gap policy. Searching backwards is
        # intentional: the forward solver otherwise chooses midnight/the first
        # opening and turns a small conflict repair into a surprising day-wide
        # move.
        day_start = datetime.combine(requested_start.date(), time.min)
        candidate_end = requested_start - timedelta(minutes=gap_minutes)
        candidate_start = candidate_end - timedelta(minutes=duration_minutes)
        step = timedelta(minutes=5)
        while candidate_start >= day_start:
            buffered_start = candidate_start - timedelta(minutes=gap_minutes)
            buffered_end = candidate_end + timedelta(minutes=gap_minutes)
            if not any(
                buffered_start < interval.end_at and interval.start_at < buffered_end
                for interval in available_occupied
            ):
                placement_start = candidate_start
                placement_end = candidate_end
                break
            candidate_start -= step
            candidate_end -= step
    else:
        # The relocated block must also respect the same inter-task gap that the
        # incoming occurrence will validate. Starting exactly at requested_end
        # made the displacement look successful, then caused the strict
        # occurrence to fail its buffered overlap check against that newly
        # relocated block.
        search_start = max(
            requested_end + timedelta(minutes=gap_minutes),
            block.end_at,
        )
        search_end = datetime.combine(
            requested_start.date() + timedelta(days=1),
            time.min,
        )
        if search_end > search_start:
            result = solve_schedule_draft(
                tasks=[
                    SolverTask(
                        task_id=int(blocker.task.id),
                        duration_minutes=duration_minutes,
                        gap_before_minutes=gap_minutes,
                        gap_after_minutes=gap_minutes,
                    )
                ],
                window_start=search_start,
                window_end=search_end,
                occupied=[*available_occupied, proposed_incoming],
                slot_minutes=5,
            )
            if result.placements:
                placement_start = result.placements[0].start_at
                placement_end = result.placements[0].end_at
    if placement_start is None or placement_end is None:
        return None, None
    operation = {
        "op": "update_block_time",
        "acceptedBlockId": int(block.id),
        "taskId": int(blocker.task.id),
        "categoryId": int(blocker.task.category_id),
        "title": blocker.task.title,
        "emoji": blocker.task.emoji,
        "startAt": _iso(placement_start),
        "endAt": _iso(placement_end),
        # Approval must only move the exact block state the user reviewed. If
        # Google Calendar or the user moves it in the meantime, live
        # SchedulePatch validation rejects this stale operation instead of
        # overwriting the newer placement.
        "expectedOriginalStartAt": _iso(block.start_at),
        "expectedOriginalEndAt": _iso(block.end_at),
        "variant": "proposed",
        "completionStatus": block.completion_status,
        "taskPriority": blocker.task.priority,
        "timeEstimateMinutes": blocker.task.time_estimate_minutes,
        "isPinned": False,
        "isProtected": False,
        "isRecurring": bool(block.is_recurring),
        "recurrenceSeriesId": block.recurrence_series_id,
        "recurrenceKey": block.recurrence_key,
        "source": "priority_conflict_resolution",
    }
    return operation, OccupiedInterval(
        block_id=-int(block.id),
        start_at=placement_start,
        end_at=placement_end,
        source_type="draft_relocation",
        source_label=blocker.task.title,
        report_conflict=False,
    )


def _place_occurrence(
    *,
    day: date,
    duration_minutes: int,
    preferred_time: time,
    allowed_start_time: time,
    allowed_end_time: time,
    gap_minutes: int,
    occupied: list[OccupiedInterval],
    not_before: datetime | None = None,
    strict: bool = False,
) -> tuple[datetime, datetime] | None:
    day_start = datetime.combine(day, allowed_start_time)
    day_end = datetime.combine(day, allowed_end_time)
    if day_end <= day_start:
        day_end += timedelta(days=1)
    if not_before is not None and not_before.date() == day:
        day_start = max(day_start, _ceil_to_five_minutes(not_before))
    if day_start >= day_end:
        return None
    preferred_start = datetime.combine(day, preferred_time)
    if preferred_start < day_start:
        preferred_start = day_start
    if preferred_start >= day_end:
        preferred_start = day_start

    if strict:
        exact_start = datetime.combine(day, preferred_time)
        exact_end = exact_start + timedelta(minutes=duration_minutes)
        if exact_start < day_start or exact_end > day_end:
            return None
        buffered_start = exact_start - timedelta(minutes=gap_minutes)
        buffered_end = exact_end + timedelta(minutes=gap_minutes)
        if any(
            buffered_start < interval.end_at and interval.start_at < buffered_end
            for interval in occupied
        ):
            return None
        return exact_start, exact_end

    task = SolverTask(
        task_id=-1,
        duration_minutes=duration_minutes,
        gap_before_minutes=gap_minutes,
        gap_after_minutes=gap_minutes,
    )
    # Prefer the requested anchor and move later on only the days that need it.
    result = solve_schedule_draft(
        tasks=[task],
        window_start=preferred_start,
        window_end=day_end,
        occupied=occupied,
        slot_minutes=5,
    )
    if result.placements:
        placement = result.placements[0]
        return placement.start_at, placement.end_at

    # If the rest of the preferred period is full, use an earlier opening in
    # the same user-supplied period rather than jumping to another date.
    if preferred_start > day_start:
        result = solve_schedule_draft(
            tasks=[task],
            window_start=day_start,
            window_end=preferred_start,
            occupied=occupied,
            slot_minutes=5,
        )
        if result.placements:
            placement = result.placements[0]
            return placement.start_at, placement.end_at
    return None


def build_recurring_schedule_plan(
    session: Session,
    *,
    spec: dict[str, Any],
    category_id: int,
    task_title: str,
    task_emoji: str | None = None,
    task_id: int | None = None,
    task_priority: str | None = None,
    not_before: datetime | None = None,
    preference_override_authorized: bool = False,
    allow_priority_displacement: bool = False,
    prompt: str = "",
) -> dict[str, Any]:
    """Resolve a finite series or the next horizon of an open-ended series."""

    frequency = str(spec.get("frequency") or "daily").strip().lower()
    start_date = _parse_date(spec.get("start_date") or spec.get("startDate"))
    end_date = _parse_date(spec.get("end_date") or spec.get("endDate"))
    occurrence_count = spec.get("occurrence_count", spec.get("occurrenceCount"))
    end_mode = str(spec.get("end_mode") or spec.get("endMode") or "").strip().lower()
    if not end_mode and bool(spec.get("ongoing") or spec.get("indefinite")):
        end_mode = "never"
    if end_mode == "never":
        # ``never`` is the durable series contract.  Older pending proposals
        # may still contain an `end_date`/`occurrence_count` copied from their
        # rolling preview; do not let those implementation fields cap a later
        # revision of the ongoing series.
        end_date = None
        occurrence_count = None
    raw_days_of_week = spec.get("days_of_week", spec.get("daysOfWeek"))
    weekdays = _parse_weekdays(raw_days_of_week)
    # A populated weekday set is the recurrence truth. Models occasionally
    # describe "every weekday" as frequency=daily plus Monday-Friday; treating
    # that contradictory shape as literal daily silently expands the series to
    # weekends. Canonicalize it at the typed boundary instead.
    if raw_days_of_week is not None and weekdays:
        frequency = "weekly"
    duration = spec.get("duration_minutes", spec.get("durationMinutes"))
    requested_preferred_time = _parse_time(
        spec.get("preferred_start_time") or spec.get("preferredStartTime")
    )
    requested_allowed_start = _parse_time(
        spec.get("allowed_start_time") or spec.get("allowedStartTime")
    )
    requested_allowed_end = _parse_time(
        spec.get("allowed_end_time") or spec.get("allowedEndTime")
    )
    strict = bool(spec.get("strict", False))
    conflict_relocation = str(
        spec.get("conflict_relocation")
        or spec.get("conflictRelocation")
        or "after_incoming"
    ).strip().lower()
    rolling_horizon_days = spec.get(
        "rolling_horizon_days",
        spec.get("rollingHorizonDays", DEFAULT_ROLLING_HORIZON_DAYS),
    )
    if not isinstance(rolling_horizon_days, int) or isinstance(rolling_horizon_days, bool):
        rolling_horizon_days = DEFAULT_ROLLING_HORIZON_DAYS
    rolling_horizon_days = min(366, max(28, rolling_horizon_days))
    # A daily time is a preference, not required user input. When the user did
    # not name one, give Planner a broad waking-day window and a sensible
    # morning anchor; calendar conflicts and saved constraints decide the exact
    # placement on each date.
    automatic_time = requested_preferred_time is None
    user_anchored_time = bool(
        strict
        or (
            requested_preferred_time is not None
            and prompt_has_explicit_clock_time(prompt)
        )
    )
    preferred_time = requested_preferred_time or time(9, 0)
    allowed_start = requested_allowed_start or (
        time(8, 0) if automatic_time else preferred_time
    )
    allowed_end = requested_allowed_end or time(21, 0)
    errors: list[str] = []
    if frequency not in {"daily", "weekly"}:
        errors.append("Choose a daily or weekly recurrence.")
    if conflict_relocation not in {"before_incoming", "after_incoming"}:
        errors.append("Choose whether conflicting flexible tasks should move before or after this schedule.")
    if start_date is None:
        errors.append("Choose the first day for this recurring schedule.")
    if frequency == "weekly" and start_date is not None:
        if raw_days_of_week is None:
            weekdays = [start_date.weekday()]
        elif not weekdays:
            errors.append("Choose at least one weekday for this weekly schedule.")
    if end_date is None and isinstance(occurrence_count, int) and occurrence_count > 0 and start_date:
        if frequency in {"daily", "weekly"} and (frequency == "daily" or weekdays):
            end_date = _end_date_for_occurrence_count(
                start_date=start_date,
                occurrence_count=occurrence_count,
                frequency=frequency,
                weekdays=weekdays,
            )
    series_end_date = end_date
    if end_date is None and end_mode == "never" and start_date is not None:
        end_date = start_date + timedelta(days=rolling_horizon_days)
    if end_date is None:
        errors.append("Choose an end date, occurrence count, or an ongoing recurrence.")
    if start_date and end_date and end_date < start_date:
        errors.append("The recurring schedule must end on or after its first day.")
    if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 1440:
        errors.append("Choose a duration between 1 minute and 24 hours.")
    if errors:
        return {
            "status": "invalid",
            "errors": errors,
            "issues": [
                {
                    "code": (
                        "missing_recurrence_end"
                        if "ongoing recurrence" in error
                        else "invalid_recurrence"
                    ),
                    "outcome": (
                        "needs_user_input"
                        if "ongoing recurrence" in error
                        else "unsupported"
                    ),
                    "message": error,
                }
                for error in errors
            ],
            "occurrences": [],
        }

    assert start_date is not None and end_date is not None
    assert preferred_time is not None and allowed_start is not None and allowed_end is not None
    assert isinstance(duration, int)
    series_start = datetime.combine(start_date, time.min)
    series_end = datetime.combine(end_date + timedelta(days=1), time.min)
    category = session.get(Category, category_id)
    category_name = category.name if category is not None else ""
    preference_context = build_preference_context_for_scope(
        session,
        task_ids=[],
        category_ids=[category_id],
        task_titles=[task_title],
        category_names=[category_name] if category_name else [],
        window_start=series_start,
        window_end=series_end,
        prompt="",
    )
    explicit_gap = spec.get("gap_minutes", spec.get("gapMinutes"))
    has_explicit_gap = isinstance(explicit_gap, int) and not isinstance(
        explicit_gap, bool
    )
    explicit_gap_minutes = max(0, explicit_gap) if has_explicit_gap else 0
    gap_policy = inter_task_gap_policy_from_context(preference_context)
    soft_gap_minutes, hard_gap_minutes = inter_task_gap_minutes_for_duration(
        gap_policy,
        duration_minutes=duration,
    )
    # Fixing the incoming item's clock time does not implicitly discard a saved
    # transition-gap preference. It changes the decision from "move the incoming
    # item" to "ask which adjacent item may move." Only explicit back-to-back or
    # ignore-gap language authorizes bypassing that preference.
    gap_minutes = (
        explicit_gap_minutes
        if gap_override_authorized_from_prompt(prompt)
        else max(soft_gap_minutes, hard_gap_minutes, explicit_gap_minutes)
    )

    accepted_blocks = read_calendar_state(
        session,
        window_start=series_start,
        window_end=series_end + timedelta(days=1),
    )
    blocker_series_ids = {
        str(block.recurrence_series_id)
        for block in accepted_blocks
        if block.recurrence_series_id
    }
    blocker_series_gaps = {
        str(series.id): max(0, int(series.gap_minutes or 0))
        for series in session.exec(
            select(RecurrenceSeries).where(RecurrenceSeries.id.in_(blocker_series_ids))
        ).all()
    } if blocker_series_ids else {}
    calendar_occupied = _calendar_occupied(accepted_blocks)
    calendar_task_ids = {int(block.task_id) for block in accepted_blocks}
    calendar_tasks = {
        int(node.id): node
        for node in session.exec(
            select(StrategyNode).where(StrategyNode.id.in_(calendar_task_ids))
        ).all()
        if node.id is not None
    }
    incoming_task = (
        session.get(StrategyNode, task_id)
        if isinstance(task_id, int)
        else None
    )
    if incoming_task is None:
        incoming_task = StrategyNode(
            id=task_id,
            category_id=category_id,
            title=task_title,
            emoji=task_emoji,
            priority=str(task_priority or "medium"),
        )
    preference_occupied = constraint_intervals_from_context(
        preference_context,
        window_start=series_start,
        window_end=series_end,
    )
    # Saved fixed/forbidden windows guide flexible scheduling. An exact request
    # is checked against them separately: explicit fixed/strict language may
    # authorize an override, otherwise the user must confirm before approval.
    occupied = list(calendar_occupied)
    if not strict:
        occupied.extend(preference_occupied)

    occurrences: list[dict[str, Any]] = []
    shifted_dates: list[str] = []
    conflicting_dates: list[str] = []
    preference_conflict_dates: list[str] = []
    conflicting_preference_constraints: list[str] = []
    overridden_preference_constraints: list[str] = []
    priority_conflicts: list[dict[str, Any]] = []
    conflict_resolutions: list[dict[str, Any]] = []
    preserved_blockers: list[dict[str, Any]] = []
    displacement_operations: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        if not _matches_recurrence_day(cursor, frequency=frequency, weekdays=weekdays):
            cursor += timedelta(days=1)
            continue
        requested_start = datetime.combine(cursor, preferred_time)
        requested_end = requested_start + timedelta(minutes=duration)
        requested_buffered_start = requested_start - timedelta(minutes=gap_minutes)
        requested_buffered_end = requested_end + timedelta(minutes=gap_minutes)
        if strict:
            day_preference_conflict = False
            for interval in preference_occupied:
                if (
                    requested_buffered_start < interval.end_at
                    and interval.start_at < requested_buffered_end
                ):
                    label = str(
                        interval.source_label or "a saved scheduling preference"
                    ).strip()
                    target = (
                        overridden_preference_constraints
                        if preference_override_authorized
                        else conflicting_preference_constraints
                    )
                    if label and label not in target:
                        target.append(label)
                    if not preference_override_authorized:
                        day_preference_conflict = True
            if day_preference_conflict:
                preference_conflict_dates.append(cursor.isoformat())
                cursor += timedelta(days=1)
                continue
        day_blockers = overlapping_blockers(
            accepted_blocks,
            tasks_by_id=calendar_tasks,
            start_at=requested_start,
            end_at=requested_end,
        )
        gap_only_blocker_ids: set[int] = set()
        if (
            (strict or user_anchored_time)
            and not gap_override_authorized_from_prompt(prompt)
            and blocker_series_gaps
        ):
            max_blocker_gap = max(blocker_series_gaps.values(), default=0)
            nearby_blockers = overlapping_blockers(
                accepted_blocks,
                tasks_by_id=calendar_tasks,
                start_at=requested_start - timedelta(minutes=max_blocker_gap),
                end_at=requested_end + timedelta(minutes=max_blocker_gap),
            )
            direct_ids = {
                int(item.block.id)
                for item in day_blockers
                if item.block.id is not None
            }
            for blocker in nearby_blockers:
                block_id = int(blocker.block.id) if blocker.block.id is not None else None
                if block_id is not None and block_id in direct_ids:
                    continue
                blocker_gap = blocker_series_gaps.get(
                    str(blocker.block.recurrence_series_id or ""),
                    0,
                )
                if blocker_gap <= 0:
                    continue
                expanded_start = blocker.block.start_at - timedelta(minutes=blocker_gap)
                expanded_end = blocker.block.end_at + timedelta(minutes=blocker_gap)
                if requested_start < expanded_end and expanded_start < requested_end:
                    day_blockers.append(blocker)
                    if block_id is not None:
                        gap_only_blocker_ids.add(block_id)
        if day_blockers:
            decision = decide_calendar_collision(
                incoming_task=incoming_task,
                blockers=day_blockers,
                prompt=prompt,
                exact_time=user_anchored_time,
            )
            if allow_priority_displacement and decision.action == "displace_existing":
                displaced_ids = {
                    int(item.block.id)
                    for item in decision.blockers
                    if item.block.id is not None
                }
                candidate_operations: list[dict[str, Any]] = []
                candidate_intervals: list[OccupiedInterval] = []
                for blocker in decision.blockers:
                    blocker_gap_minutes = blocker_series_gaps.get(
                        str(blocker.block.recurrence_series_id or ""),
                        0,
                    )
                    operation, interval = _relocate_recurring_blocker(
                        blocker=blocker,
                        occupied=[*occupied, *candidate_intervals],
                        displaced_block_ids=displaced_ids,
                        requested_start=requested_start,
                        requested_end=requested_end,
                        # Moving an existing recurring occurrence must preserve
                        # its own durable spacing contract as well as any gap
                        # required by the incoming schedule.
                        gap_minutes=max(gap_minutes, blocker_gap_minutes),
                        conflict_relocation=conflict_relocation,
                    )
                    if operation is None or interval is None:
                        candidate_operations = []
                        candidate_intervals = []
                        break
                    candidate_operations.append(operation)
                    candidate_intervals.append(interval)
                if candidate_operations:
                    occupied = [
                        interval
                        for interval in occupied
                        if int(interval.block_id) not in displaced_ids
                    ]
                    occupied.extend(candidate_intervals)
                    displacement_operations.extend(candidate_operations)
                    resolution_blockers: list[dict[str, Any]] = []
                    for blocker, operation in zip(decision.blockers, candidate_operations):
                        payload = blocker_payload(blocker)
                        payload["movedStartAt"] = operation["startAt"]
                        payload["movedEndAt"] = operation["endAt"]
                        resolution_blockers.append(payload)
                    relocation_summary = "; ".join(
                        f"{item['taskTitle']} was blocking "
                        f"{_human_clock(datetime.fromisoformat(str(item['startAt'])))} to "
                        f"{_human_clock(datetime.fromisoformat(str(item['endAt'])))} and moves to "
                        f"{_human_clock(datetime.fromisoformat(str(item['movedStartAt'])))} to "
                        f"{_human_clock(datetime.fromisoformat(str(item['movedEndAt'])))}"
                        for item in resolution_blockers
                    )
                    conflict_resolutions.append(
                        {
                            "date": cursor.isoformat(),
                            "action": "moved_existing",
                            "incomingTaskTitle": task_title,
                            "incomingPriority": decision.incoming_priority,
                            "requestedStartAt": _iso(requested_start),
                            "requestedEndAt": _iso(requested_end),
                            "blockers": resolution_blockers,
                            "userMessage": (
                                f"{task_title} takes precedence on {_human_date(cursor)} because "
                                f"{decision.reason.rstrip('.').lower()}. "
                                f"I kept it at {_human_clock(requested_start)}. "
                                f"{relocation_summary}."
                            ),
                        }
                    )
                elif strict or user_anchored_time:
                    conflicting_dates.append(cursor.isoformat())
                    priority_conflicts.append(
                        {
                            "date": cursor.isoformat(),
                            "incomingTaskTitle": task_title,
                            "incomingPriority": decision.incoming_priority,
                            "requestedStartAt": _iso(requested_start),
                            "requestedEndAt": _iso(requested_end),
                            "decisionReason": decision.reason,
                            "recommendedAction": "move_existing",
                            "blockers": [blocker_payload(item) for item in decision.blockers],
                        }
                    )
                    errors.append(
                        f"The requested time conflicts with your calendar on {cursor.isoformat()}: "
                        f"{task_title} is blocked by "
                        + ", ".join(item.task.title for item in decision.blockers)
                        + "; no safe nearby time was found for the lower-priority task."
                    )
                    cursor += timedelta(days=1)
                    continue
            elif decision.action in {"clarify", "preserve_existing"} or (
                decision.action == "displace_existing"
                and not allow_priority_displacement
            ):
                blocker_details = [blocker_payload(item) for item in decision.blockers]
                conflict_payload = {
                    "date": cursor.isoformat(),
                    "incomingTaskTitle": task_title,
                    "incomingPriority": decision.incoming_priority,
                    "requestedStartAt": _iso(requested_start),
                    "requestedEndAt": _iso(requested_end),
                    "decisionReason": decision.reason,
                    "recommendedAction": decision.action,
                    "blockers": blocker_details,
                }
                if gap_only_blocker_ids and all(
                    item.block.id is not None
                    and int(item.block.id) in gap_only_blocker_ids
                    for item in decision.blockers
                ):
                    required_gap = max(
                        blocker_series_gaps.get(
                            str(item.block.recurrence_series_id or ""),
                            0,
                        )
                        for item in decision.blockers
                    )
                    available_gap = min(
                        max(
                            0,
                            int(
                                (
                                    requested_start - item.block.end_at
                                    if item.block.end_at <= requested_start
                                    else item.block.start_at - requested_end
                                ).total_seconds()
                                // 60
                            ),
                        )
                        for item in decision.blockers
                    )
                    conflict_payload.update(
                        {
                            "conflictKind": "minimum_gap",
                            "gapMinutes": required_gap,
                            "availableGapMinutes": available_gap,
                        }
                    )
                preserved_blockers.append(conflict_payload)
                if strict or user_anchored_time:
                    conflicting_dates.append(cursor.isoformat())
                    priority_conflicts.append(preserved_blockers[-1])
                    if conflict_payload.get("conflictKind") == "minimum_gap":
                        first_blocker = decision.blockers[0]
                        errors.append(
                            f"The requested time on {cursor.isoformat()} leaves only "
                            f"{conflict_payload['availableGapMinutes']} minutes between "
                            f"{first_blocker.task.title} and {task_title}, but a "
                            f"{conflict_payload['gapMinutes']}-minute gap is required."
                        )
                    else:
                        errors.append(
                            f"The requested time conflicts with your calendar on {cursor.isoformat()}: "
                            f"{task_title} at {_iso(requested_start)} is blocked by "
                            + ", ".join(
                                f"{item.task.title} ({item.priority} priority, "
                                f"{_iso(item.block.start_at)} to {_iso(item.block.end_at)})"
                                + (f" [{item.fixed_reason}]" if item.fixed_reason else "")
                                for item in decision.blockers
                            )
                            + ". Confirm which task should keep this time."
                        )
                    cursor += timedelta(days=1)
                    continue
        placement = _place_occurrence(
            day=cursor,
            duration_minutes=duration,
            preferred_time=preferred_time,
            allowed_start_time=allowed_start,
            allowed_end_time=allowed_end,
            gap_minutes=gap_minutes,
            occupied=occupied,
            not_before=not_before,
            strict=strict,
        )
        if placement is None:
            buffer_blockers = (
                overlapping_blockers(
                    accepted_blocks,
                    tasks_by_id=calendar_tasks,
                    start_at=requested_buffered_start,
                    end_at=requested_buffered_end,
                )
                if gap_minutes > 0
                else []
            )
            if buffer_blockers:
                blocker_details = [blocker_payload(item) for item in buffer_blockers]
                available_gaps = [
                    max(
                        0,
                        int(
                            (
                                requested_start - item.block.end_at
                                if item.block.end_at <= requested_start
                                else item.block.start_at - requested_end
                                if item.block.start_at >= requested_end
                                else timedelta(0)
                            ).total_seconds()
                            // 60
                        ),
                    )
                    for item in buffer_blockers
                ]
                can_move_existing = all(
                    item.fixed_reason not in {"protected", "pinned"}
                    for item in buffer_blockers
                )
                gap_conflict = {
                    "date": cursor.isoformat(),
                    "incomingTaskTitle": task_title,
                    "incomingPriority": str(task_priority or "medium"),
                    "requestedStartAt": _iso(requested_start),
                    "requestedEndAt": _iso(requested_end),
                    "decisionReason": (
                        f"A {gap_minutes}-minute transition gap is required, but only "
                        f"{min(available_gaps) if available_gaps else 0} minutes is available."
                    ),
                    "recommendedAction": (
                        "move_existing" if can_move_existing else "preserve_existing"
                    ),
                    "conflictKind": "minimum_gap",
                    "gapMinutes": gap_minutes,
                    "availableGapMinutes": (
                        min(available_gaps) if available_gaps else 0
                    ),
                    "blockers": blocker_details,
                }
                conflicting_dates.append(cursor.isoformat())
                preserved_blockers.append(gap_conflict)
                priority_conflicts.append(gap_conflict)
                errors.append(
                    f"The requested time on {cursor.isoformat()} leaves only "
                    f"{gap_conflict['availableGapMinutes']} minutes between "
                    f"{', '.join(item.task.title for item in buffer_blockers)} and "
                    f"{task_title}, but a {gap_minutes}-minute gap is required."
                )
            elif any(
                requested_start < interval.end_at
                and interval.start_at < requested_end
                for interval in calendar_occupied
            ):
                conflicting_dates.append(cursor.isoformat())
                errors.append(
                    "The requested time conflicts with your calendar on "
                    f"{cursor.isoformat()}."
                )
            elif not_before is not None and requested_start < not_before:
                errors.append(
                    f"The requested time has already passed on {cursor.isoformat()}."
                )
            else:
                errors.append(
                    "The requested time does not fit its allowed window on "
                    f"{cursor.isoformat()}."
                )
            cursor += timedelta(days=1)
            continue
        start_at, end_at = placement
        if start_at != requested_start:
            shifted_dates.append(cursor.isoformat())
        occurrences.append(
            {
                "occurrence_id": f"occurrence-{uuid4()}",
                "date": cursor.isoformat(),
                "start_at": _iso(start_at),
                "end_at": _iso(end_at),
                "shifted": start_at != requested_start,
            }
        )
        occupied.append(
            OccupiedInterval(
                block_id=-(len(occurrences)),
                start_at=start_at,
                end_at=end_at,
                source_type="draft",
                report_conflict=False,
            )
        )
        cursor += timedelta(days=1)

    for label in conflicting_preference_constraints:
        errors.append(
            "The requested exact time conflicts with saved scheduling rule: "
            f"{label}. Confirm whether to override this saved rule."
        )
    validation_warnings = [
        (
            "This exact recurring time overrides your saved "
            f"\u201c{label}\u201d preference."
        )
        for label in overridden_preference_constraints
    ]
    return {
        "status": "invalid" if errors else "valid",
        "errors": errors,
        "frequency": frequency,
        "days_of_week": [WEEKDAY_NAMES[index] for index in weekdays]
        if frequency == "weekly"
        else [],
        "task_title": task_title,
        "task_emoji": task_emoji,
        "category_id": category_id,
        "category_name": category_name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "series_end_date": series_end_date.isoformat() if series_end_date else None,
        "end_mode": "never" if series_end_date is None else "until",
        "materialization_end_date": end_date.isoformat(),
        "rolling_horizon_days": rolling_horizon_days if series_end_date is None else None,
        "duration_minutes": duration,
        "preferred_start_time": preferred_time.isoformat(timespec="minutes"),
        "allowed_start_time": allowed_start.isoformat(timespec="minutes"),
        "allowed_end_time": allowed_end.isoformat(timespec="minutes"),
        "time_selection": "automatic" if automatic_time else "requested",
        "gap_minutes": gap_minutes,
        "strict": strict,
        "conflict_relocation": conflict_relocation,
        "allow_priority_displacement": allow_priority_displacement,
        "preference_override_authorized": preference_override_authorized,
        "conflicting_dates": conflicting_dates,
        "preference_conflict_dates": preference_conflict_dates,
        "conflicting_preference_constraints": conflicting_preference_constraints,
        "overridden_preference_constraints": overridden_preference_constraints,
        "validation_warnings": validation_warnings,
        "priority_conflicts": priority_conflicts,
        "conflict_resolutions": conflict_resolutions,
        "preserved_blockers": preserved_blockers,
        "displacement_operations": displacement_operations,
        "timezone": _clean_text(spec.get("timezone")),
        "occurrence_count": len(occurrences),
        "shifted_dates": shifted_dates,
        "occurrences": occurrences,
    }


def schedule_patch_from_recurring_plan(
    plan: dict[str, Any],
    *,
    task_id: int,
    category_id: int,
    task_title: str,
    task_emoji: str | None = None,
    recurrence_series_id: str | None = None,
) -> dict[str, Any]:
    occurrences = [item for item in plan.get("occurrences", []) if isinstance(item, dict)]
    if not occurrences:
        raise ValueError("The recurring schedule proposal has no occurrences.")
    start_date = _parse_date(plan.get("start_date"))
    end_date = _parse_date(plan.get("end_date"))
    if start_date is None or end_date is None:
        raise ValueError("The recurring schedule proposal has an invalid date range.")
    occurrence_starts = [
        datetime.fromisoformat(str(item["start_at"]))
        for item in occurrences
        if item.get("start_at")
    ]
    occurrence_ends = [
        datetime.fromisoformat(str(item["end_at"]))
        for item in occurrences
        if item.get("end_at")
    ]
    displacement_operations = [
        item
        for item in plan.get("displacement_operations", [])
        if isinstance(item, dict)
    ]
    occurrence_starts.extend(
        datetime.fromisoformat(str(item["startAt"]))
        for item in displacement_operations
        if item.get("startAt")
    )
    occurrence_ends.extend(
        datetime.fromisoformat(str(item["endAt"]))
        for item in displacement_operations
        if item.get("endAt")
    )
    window_start = min(occurrence_starts, default=datetime.combine(start_date, time.min))
    window_end = max(
        occurrence_ends,
        default=datetime.combine(end_date + timedelta(days=1), time.min),
    )
    return {
        "patchId": f"recurring-schedule-{uuid4()}",
        "windowStart": _iso(window_start),
        "windowEnd": _iso(window_end),
        "workingSetTaskIds": [task_id],
        "operations": [
            {
                "op": "create_block",
                "proposalBlockId": str(item.get("occurrence_id") or f"occurrence-{index}"),
                "taskId": task_id,
                "categoryId": category_id,
                "title": task_title,
                "emoji": task_emoji,
                "categoryName": plan.get("category_name"),
                "startAt": item.get("start_at"),
                "endAt": item.get("end_at"),
                "variant": "proposed",
                "completionStatus": "not_started",
                "timeEstimateMinutes": plan.get("duration_minutes"),
                "allocatedTimeboxMinutes": plan.get("duration_minutes"),
                "isPinned": False,
                "isProtected": False,
                "isRecurring": True,
                "recurrenceSeriesId": recurrence_series_id,
                "recurrenceKey": str(item.get("date") or ""),
                "source": (
                    "rolling_recurring_series"
                    if plan.get("end_mode") == "never"
                    else "finite_recurring_series"
                ),
            }
            for index, item in enumerate(occurrences)
        ]
        + displacement_operations,
    }


def create_recurrence_series_from_plan(
    session: Session,
    *,
    plan: dict[str, Any],
    task_id: int,
    category_id: int,
    source_change_set_id: str | None = None,
) -> RecurrenceSeries:
    start_date = _parse_date(plan.get("start_date"))
    if start_date is None:
        raise ValueError("The recurring schedule is missing its start date.")
    end_date = _parse_date(plan.get("series_end_date"))
    materialized_through = _parse_date(
        plan.get("materialization_end_date") or plan.get("end_date")
    )
    series = RecurrenceSeries(
        task_id=task_id,
        category_id=category_id,
        frequency=str(plan.get("frequency") or "daily"),
        interval=int(plan.get("interval") or 1),
        days_of_week_json=json.dumps(plan.get("days_of_week") or []),
        start_date=start_date,
        end_date=end_date,
        occurrence_limit=(
            int(plan["occurrence_count"])
            if end_date is not None and isinstance(plan.get("occurrence_count"), int)
            else None
        ),
        preferred_start_time=str(plan.get("preferred_start_time") or "09:00"),
        allowed_start_time=str(plan.get("allowed_start_time") or plan.get("preferred_start_time") or "08:00"),
        allowed_end_time=str(plan.get("allowed_end_time") or "21:00"),
        duration_minutes=int(plan.get("duration_minutes") or 0),
        timezone=_clean_text(plan.get("timezone")),
        strict=bool(plan.get("strict", False)),
        preference_override_authorized=bool(
            plan.get("preference_override_authorized", False)
        ),
        gap_minutes=(
            int(plan["gap_minutes"])
            if isinstance(plan.get("gap_minutes"), int)
            and not isinstance(plan.get("gap_minutes"), bool)
            else None
        ),
        conflict_relocation=str(
            plan.get("conflict_relocation") or "after_incoming"
        ),
        allow_priority_displacement=bool(
            plan.get("allow_priority_displacement", False)
        ),
        materialized_through=materialized_through,
        source_change_set_id=source_change_set_id,
    )
    session.add(series)
    session.flush()
    return series


def materialize_active_recurrence_series(
    session: Session,
    *,
    through_date: date,
) -> dict[str, Any]:
    """Extend approved open-ended series through a requested calendar horizon."""

    from app.services.schedule_patches import apply_approved_schedule_patch

    created_blocks: list[CalendarBlock] = []
    errors: list[dict[str, Any]] = []
    series_rows = list(
        session.exec(
            select(RecurrenceSeries).where(RecurrenceSeries.status == "active")
        ).all()
    )
    for series in series_rows:
        if series.end_date is not None and series.end_date < through_date:
            target_end = series.end_date
        else:
            target_end = through_date
        current_end = series.materialized_through or (series.start_date - timedelta(days=1))
        if target_end <= current_end:
            continue
        extension_start = max(series.start_date, current_end + timedelta(days=1))
        try:
            weekdays = json.loads(series.days_of_week_json or "[]")
        except (TypeError, ValueError):
            weekdays = []
        task = session.get(StrategyNode, series.task_id)
        if task is None:
            series.status = "orphaned"
            session.add(series)
            errors.append({"series_id": series.id, "errors": ["The linked task no longer exists."]})
            continue
        extension_spec: dict[str, Any] = {
            "frequency": series.frequency,
            "days_of_week": weekdays,
            "start_date": extension_start.isoformat(),
            "end_date": target_end.isoformat(),
            "duration_minutes": series.duration_minutes,
            "preferred_start_time": series.preferred_start_time,
            "allowed_start_time": series.allowed_start_time,
            "allowed_end_time": series.allowed_end_time,
            "timezone": series.timezone,
            "strict": series.strict,
            "conflict_relocation": series.conflict_relocation,
        }
        if isinstance(series.gap_minutes, int):
            extension_spec["gap_minutes"] = series.gap_minutes
        plan = build_recurring_schedule_plan(
            session,
            spec=extension_spec,
            category_id=series.category_id,
            task_title=task.title,
            task_id=int(task.id),
            task_priority=task.priority,
            preference_override_authorized=series.preference_override_authorized,
            allow_priority_displacement=series.allow_priority_displacement,
            prompt=(
                f"Keep the recurring {task.title} at its approved exact time and move "
                f"lower-priority flexible conflicts {series.conflict_relocation.replace('_incoming', '')}."
            ),
        )
        if plan.get("status") != "valid":
            errors.append({"series_id": series.id, "errors": list(plan.get("errors") or [])})
            continue
        if not plan.get("occurrences") and not plan.get("displacement_operations"):
            # A short rolling-horizon extension can fall entirely between two
            # valid recurrence dates. That is a successful no-op, not an
            # invalid proposal or a failed calendar read.
            continue
        patch = schedule_patch_from_recurring_plan(
            plan,
            task_id=int(task.id),
            category_id=series.category_id,
            task_title=task.title,
            task_emoji=task.emoji,
            recurrence_series_id=series.id,
        )
        execution = apply_approved_schedule_patch(session, patch)
        created_blocks.extend(execution.created_blocks)
        series.materialized_through = target_end
        series.updated_at = datetime.utcnow()
        session.add(series)
    if created_blocks or errors:
        session.commit()
        for block in created_blocks:
            session.refresh(block)
    return {
        "created_blocks": created_blocks,
        "created_count": len(created_blocks),
        "errors": errors,
    }
