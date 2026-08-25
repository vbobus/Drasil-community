"""
Deterministic scheduling dry-run solver.

This service owns placement math for auto-schedule previews. It deliberately
returns draft placements only; API callers decide how to render them.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil, floor

from ortools.sat.python import cp_model


@dataclass(frozen=True)
class SolverTask:
    task_id: int
    duration_minutes: int
    gap_before_minutes: int = 0
    gap_after_minutes: int = 0
    preferred_start_windows: tuple[tuple[datetime, datetime], ...] = ()
    allowed_start_windows: tuple[tuple[datetime, datetime], ...] = ()


@dataclass(frozen=True)
class OccupiedInterval:
    block_id: int
    start_at: datetime
    end_at: datetime
    source_type: str = "calendar"
    source_label: str | None = None
    report_conflict: bool = True


@dataclass(frozen=True)
class SolverPlacement:
    task_id: int
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class SolverAlternative:
    start_at: datetime
    end_at: datetime
    reason: str


@dataclass(frozen=True)
class SolverConflict:
    task_id: int
    type: str
    detail: str
    conflicting_block_ids: list[int] = field(default_factory=list)
    alternatives: list[SolverAlternative] = field(default_factory=list)


@dataclass(frozen=True)
class SolverResult:
    placements: list[SolverPlacement]
    conflicts: list[SolverConflict]
    solver_status: str


@dataclass(frozen=True)
class _SlotInterval:
    block_id: int
    start_slot: int
    end_slot: int
    report_conflict: bool = True


def _ceil_divide(value: int, divisor: int) -> int:
    return int(ceil(value / divisor))


def _minutes_between(start_at: datetime, end_at: datetime) -> int:
    return int((end_at - start_at).total_seconds() // 60)


def _datetime_from_slot(window_start: datetime, slot: int, slot_minutes: int) -> datetime:
    return window_start + timedelta(minutes=slot * slot_minutes)


def _slot_intervals(
    occupied: list[OccupiedInterval],
    window_start: datetime,
    window_end: datetime,
    slot_minutes: int,
    task_gap_before_minutes: int = 0,
    task_gap_after_minutes: int = 0,
) -> list[_SlotInterval]:
    intervals: list[_SlotInterval] = []
    for block in occupied:
        pad_calendar_task = block.report_conflict and block.source_type == "calendar"
        padded_start = (
            block.start_at - timedelta(minutes=max(0, task_gap_after_minutes))
            if pad_calendar_task
            else block.start_at
        )
        padded_end = (
            block.end_at + timedelta(minutes=max(0, task_gap_before_minutes))
            if pad_calendar_task
            else block.end_at
        )
        start_at = max(padded_start, window_start)
        end_at = min(padded_end, window_end)
        if end_at <= start_at:
            continue

        start_minutes = max(0, _minutes_between(window_start, start_at))
        end_minutes = max(0, _minutes_between(window_start, end_at))
        start_slot = floor(start_minutes / slot_minutes)
        end_slot = _ceil_divide(end_minutes, slot_minutes)
        if end_slot > start_slot:
            intervals.append(
                _SlotInterval(
                    block_id=block.block_id,
                    start_slot=start_slot,
                    end_slot=end_slot,
                    report_conflict=block.report_conflict,
                )
            )
    return intervals


def _solve_earliest_start(
    *,
    duration_slots: int,
    horizon_slots: int,
    occupied_slots: list[_SlotInterval],
    earliest_start_slot: int = 0,
    latest_start_slot: int | None = None,
) -> int | None:
    effective_latest_start_slot = horizon_slots - duration_slots
    if latest_start_slot is not None:
        effective_latest_start_slot = min(
            effective_latest_start_slot,
            latest_start_slot,
        )
    if duration_slots > horizon_slots or earliest_start_slot > effective_latest_start_slot:
        return None

    model = cp_model.CpModel()
    start = model.NewIntVar(earliest_start_slot, effective_latest_start_slot, "draft_start")
    end = model.NewIntVar(duration_slots, horizon_slots, "draft_end")
    model.Add(end == start + duration_slots)
    intervals = [model.NewIntervalVar(start, duration_slots, end, "draft_interval")]

    # Padding a task around consecutive calendar blocks can make the padded
    # fixed intervals overlap each other. CP-SAT's NoOverlap would then reject
    # the whole model even when there is plenty of free time later. Merge the
    # occupied union before adding it to the model; conflict reporting still
    # uses the original intervals and IDs.
    merged_occupied: list[_SlotInterval] = []
    for occupied in sorted(occupied_slots, key=lambda item: (item.start_slot, item.end_slot)):
        if merged_occupied and occupied.start_slot <= merged_occupied[-1].end_slot:
            previous = merged_occupied[-1]
            merged_occupied[-1] = _SlotInterval(
                block_id=previous.block_id,
                start_slot=previous.start_slot,
                end_slot=max(previous.end_slot, occupied.end_slot),
                report_conflict=previous.report_conflict or occupied.report_conflict,
            )
        else:
            merged_occupied.append(occupied)

    for index, occupied in enumerate(merged_occupied):
        fixed_start = model.NewConstant(occupied.start_slot)
        fixed_end = model.NewConstant(occupied.end_slot)
        fixed_size = max(1, occupied.end_slot - occupied.start_slot)
        intervals.append(
            model.NewIntervalVar(
                fixed_start,
                fixed_size,
                fixed_end,
                f"occupied_{occupied.block_id}_{index}",
            )
        )

    model.AddNoOverlap(intervals)
    model.Minimize(start)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 0.25
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return int(solver.Value(start))
    return None


def _conflicting_block_ids(occupied_slots: list[_SlotInterval]) -> list[int]:
    ids: list[int] = []
    for interval in occupied_slots:
        if interval.report_conflict and interval.block_id != 0 and interval.block_id not in ids:
            ids.append(interval.block_id)
    return ids


def _alternative_after_window(
    task: SolverTask,
    *,
    window_end: datetime,
    alternative_window_end: datetime,
    occupied: list[OccupiedInterval],
    slot_minutes: int,
) -> SolverAlternative | None:
    horizon_minutes = _minutes_between(window_end, alternative_window_end)
    horizon_slots = max(0, horizon_minutes // slot_minutes)
    duration_slots = _ceil_divide(task.duration_minutes, slot_minutes)
    occupied_slots = _slot_intervals(
        occupied,
        window_end,
        alternative_window_end,
        slot_minutes,
        task_gap_before_minutes=task.gap_before_minutes,
        task_gap_after_minutes=task.gap_after_minutes,
    )
    start_slot = _solve_earliest_start(
        duration_slots=duration_slots,
        horizon_slots=horizon_slots,
        occupied_slots=occupied_slots,
    )
    if start_slot is None:
        return None
    start_at = _datetime_from_slot(window_end, start_slot, slot_minutes)
    return SolverAlternative(
        start_at=start_at,
        end_at=start_at + timedelta(minutes=task.duration_minutes),
        reason=(
            "First available slot after the requested window"
            + (
                f" with the required {task.gap_after_minutes}-minute task gap."
                if task.gap_after_minutes
                else "."
            )
        ),
    )


def solve_schedule_draft(
    *,
    tasks: list[SolverTask],
    window_start: datetime,
    window_end: datetime,
    occupied: list[OccupiedInterval],
    slot_minutes: int,
    alternative_window_end: datetime | None = None,
) -> SolverResult:
    """Return earliest deterministic non-overlapping draft placements."""

    horizon_minutes = _minutes_between(window_start, window_end)
    horizon_slots = max(0, horizon_minutes // slot_minutes)
    scheduled_intervals = list(occupied)
    placements: list[SolverPlacement] = []
    conflicts: list[SolverConflict] = []
    earliest_start_slot = 0

    for task in tasks:
        duration_slots = _ceil_divide(task.duration_minutes, slot_minutes)
        occupied_slots = _slot_intervals(
            scheduled_intervals,
            window_start,
            window_end,
            slot_minutes,
            task_gap_before_minutes=task.gap_before_minutes,
            task_gap_after_minutes=task.gap_after_minutes,
        )
        if placements:
            previous_end_slot = _ceil_divide(
                _minutes_between(window_start, placements[-1].end_at),
                slot_minutes,
            )
            earliest_start_slot = max(
                earliest_start_slot,
                previous_end_slot
                + _ceil_divide(max(0, task.gap_before_minutes), slot_minutes),
            )
        start_slot: int | None = None
        for allowed_start, allowed_end in task.allowed_start_windows:
            clipped_start = max(allowed_start, window_start)
            clipped_end = min(allowed_end, window_end)
            if clipped_end <= clipped_start:
                continue
            allowed_earliest_slot = max(
                earliest_start_slot,
                _ceil_divide(
                    max(0, _minutes_between(window_start, clipped_start)),
                    slot_minutes,
                ),
            )
            allowed_latest_slot = floor(
                _minutes_between(window_start, clipped_end) / slot_minutes
            ) - duration_slots
            start_slot = _solve_earliest_start(
                duration_slots=duration_slots,
                horizon_slots=horizon_slots,
                occupied_slots=occupied_slots,
                earliest_start_slot=allowed_earliest_slot,
                latest_start_slot=allowed_latest_slot,
            )
            if start_slot is not None:
                break
        if task.allowed_start_windows and start_slot is None:
            conflicts.append(
                SolverConflict(
                    task_id=task.task_id,
                    type="request_constraint",
                    detail=f"No slot satisfies the requested time constraint for task {task.task_id}.",
                    conflicting_block_ids=_conflicting_block_ids(occupied_slots),
                    alternatives=[],
                )
            )
            continue
        for preferred_start, preferred_end in task.preferred_start_windows:
            if start_slot is not None:
                break
            clipped_start = max(preferred_start, window_start)
            clipped_end = min(preferred_end, window_end)
            if clipped_end <= clipped_start:
                continue
            preferred_earliest_slot = max(
                earliest_start_slot,
                _ceil_divide(
                    max(0, _minutes_between(window_start, clipped_start)),
                    slot_minutes,
                ),
            )
            preferred_latest_slot = max(
                0,
                floor(_minutes_between(window_start, clipped_end) / slot_minutes) - 1,
            )
            start_slot = _solve_earliest_start(
                duration_slots=duration_slots,
                horizon_slots=horizon_slots,
                occupied_slots=occupied_slots,
                earliest_start_slot=preferred_earliest_slot,
                latest_start_slot=preferred_latest_slot,
            )
            if start_slot is not None:
                break
        if start_slot is None and not task.allowed_start_windows:
            start_slot = _solve_earliest_start(
                duration_slots=duration_slots,
                horizon_slots=horizon_slots,
                occupied_slots=occupied_slots,
                earliest_start_slot=earliest_start_slot,
            )
        if start_slot is None:
            alternative = _alternative_after_window(
                task,
                window_end=window_end,
                alternative_window_end=(
                    alternative_window_end
                    if alternative_window_end is not None and alternative_window_end > window_end
                    else window_end + timedelta(days=1)
                ),
                occupied=scheduled_intervals,
                slot_minutes=slot_minutes,
            )
            conflicts.append(
                SolverConflict(
                    task_id=task.task_id,
                    type="occupied_slot",
                    detail=(
                        f"No available {task.duration_minutes} minute slot "
                        "in the selected window."
                    ),
                    conflicting_block_ids=_conflicting_block_ids(occupied_slots),
                    alternatives=[alternative] if alternative is not None else [],
                )
            )
            continue

        start_at = _datetime_from_slot(window_start, start_slot, slot_minutes)
        end_at = start_at + timedelta(minutes=task.duration_minutes)
        placements.append(
            SolverPlacement(
                task_id=task.task_id,
                start_at=start_at,
                end_at=end_at,
            )
        )
        scheduled_intervals.append(
            OccupiedInterval(
                block_id=-task.task_id,
                start_at=start_at,
                end_at=end_at,
                source_type="draft",
                source_label="Proposed task placement",
                report_conflict=False,
            )
        )
        earliest_start_slot = (
            start_slot
            + duration_slots
            + _ceil_divide(max(0, task.gap_after_minutes), slot_minutes)
        )

    solver_status = "feasible" if not conflicts else "partial" if placements else "conflict"
    return SolverResult(
        placements=placements,
        conflicts=conflicts,
        solver_status=solver_status,
    )
