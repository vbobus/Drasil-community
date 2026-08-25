"""Typed Planner capability tools for selected tasks and calendar state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from typing import Any

from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.scheduling_solver import (
    OccupiedInterval,
    SolverResult,
    SolverTask,
    solve_schedule_draft,
)


class PlannerToolError(ValueError):
    def __init__(self, error_type: str, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.error_type = error_type
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class PlannerTaskRef:
    task_id: int
    category_id: int
    title: str
    emoji: str | None
    category_name: str
    category_color: str
    status: str
    priority: str | None
    deadline_at: str | None
    time_estimate_minutes: int | None
    duration_minutes: int
    used_fallback_duration: bool
    parent_id: int | None = None
    gap_before_minutes: int = 0
    gap_after_minutes: int = 0
    is_deep_work: bool = False
    preferred_start_windows: tuple[tuple[datetime, datetime], ...] = ()
    allowed_start_windows: tuple[tuple[datetime, datetime], ...] = ()
    rendered_child_task_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class PlannerWorkingSet:
    tasks: list[PlannerTaskRef]
    fallback_duration_task_ids: list[int]
    task_nodes: list[StrategyNode]
    categories_by_id: dict[int, Category]


DEEP_WORK_RE = re.compile(
    r"\b(?:deep work|focus|focused|draft|write|writing|research|study|design|"
    r"build|coding|code|analy[sz]e|strategy|case study|creative)\b",
    re.IGNORECASE,
)
EXPLICIT_SEQUENCE_RE = re.compile(
    r"\b(?:first|then|followed by|in this order|must happen before|must happen after)\b",
    re.IGNORECASE,
)
CONTEXT_TOKEN_STOPWORDS = {
    "and", "for", "from", "into", "of", "on", "task", "the", "to", "with",
    "review", "final", "finish", "complete", "update",
}
DEFAULT_HIGH_ENERGY_WINDOWS = ((9 * 60, 12 * 60),)
HEAVY_TASK_MINUTES = 90
SMALL_BATCH_MAX_MINUTES = 45
HEAVY_RECOVERY_MINUTES = 30


def _parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _priority_rank(value: str | None) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(value or "").lower(), 3)


def _context_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 3 and token not in CONTEXT_TOKEN_STOPWORDS
    }


def _tasks_are_related(
    first: PlannerTaskRef,
    second: PlannerTaskRef,
) -> bool:
    if first.parent_id is not None and first.parent_id == second.parent_id:
        return True
    shared_tokens = _context_tokens(first.title) & _context_tokens(second.title)
    return bool(shared_tokens and first.category_id == second.category_id)


def _ref_related_to_node(task: PlannerTaskRef, node: StrategyNode) -> bool:
    if task.parent_id is not None and task.parent_id == node.parent_id:
        return True
    if task.parent_id == node.id or node.parent_id == task.task_id:
        return True
    return bool(
        task.category_id == node.category_id
        and (_context_tokens(task.title) & _context_tokens(str(node.title or "")))
    )


def _preference_text(item: dict[str, Any]) -> str:
    value = item.get("value")
    return " ".join(
        part
        for part in (
            str(item.get("domain") or ""),
            str(item.get("preferenceType") or ""),
            str(item.get("description") or ""),
            json.dumps(value, ensure_ascii=True) if isinstance(value, dict) else "",
        )
        if part
    ).lower()


def _high_energy_window_specs(
    preference_context: dict[str, Any] | None,
) -> tuple[tuple[tuple[int, int], ...], str]:
    combined = ""
    soft_preferences = (preference_context or {}).get("softPreferences")
    if isinstance(soft_preferences, list):
        for item in soft_preferences:
            if not isinstance(item, dict):
                continue
            text = _preference_text(item)
            if not any(term in text for term in ("deep_work", "deep work", "energy", "focus")):
                continue
            if re.search(
                r"\bearly mornings?\b|\bmornings?\b|\bafternoons?\b|"
                r"\bevenings?\b|\blate nights?\b|\bnight(?:time)?\b",
                text,
            ):
                combined = text
                break
    windows: list[tuple[int, int]] = []
    if re.search(r"\bearly mornings?\b", combined):
        windows.append((6 * 60, 10 * 60))
    elif re.search(r"\bmornings?\b", combined):
        windows.append((8 * 60, 12 * 60))
    if re.search(r"\bafternoons?\b", combined):
        windows.append((13 * 60, 17 * 60))
    if re.search(r"\bevenings?\b", combined):
        windows.append((17 * 60, 21 * 60))
    if re.search(r"\blate nights?\b|\bnight(?:time)?\b", combined):
        windows.append((20 * 60, 24 * 60))
    if windows:
        return tuple(dict.fromkeys(windows)), "saved_preference"
    return DEFAULT_HIGH_ENERGY_WINDOWS, "common_sense_default"


def _daily_preferred_windows(
    *,
    window_start: datetime,
    window_end: datetime,
    specs: tuple[tuple[int, int], ...],
) -> tuple[tuple[datetime, datetime], ...]:
    windows: list[tuple[datetime, datetime]] = []
    day = window_start.date()
    while datetime.combine(day, time.min) < window_end:
        for start_minutes, end_minutes in specs:
            start_at = datetime.combine(day, time.min) + timedelta(minutes=start_minutes)
            end_at = datetime.combine(day, time.min) + timedelta(minutes=end_minutes)
            clipped_start = max(start_at, window_start)
            clipped_end = min(end_at, window_end)
            if clipped_end > clipped_start:
                windows.append((clipped_start, clipped_end))
        day += timedelta(days=1)
    return tuple(windows)


def apply_default_scheduling_heuristics(
    working_set: PlannerWorkingSet,
    *,
    prompt: str,
    window_start: datetime,
    window_end: datetime,
    preference_context: dict[str, Any] | None = None,
    surrounding_tasks: list[StrategyNode] | None = None,
    surrounding_task_windows: list[tuple[StrategyNode, datetime, datetime]] | None = None,
) -> tuple[PlannerWorkingSet, dict[str, Any]]:
    """Apply deterministic defaults when the user has not supplied a stronger choice."""

    original_index = {task.task_id: index for index, task in enumerate(working_set.tasks)}
    nodes_by_id = {
        int(node.id): node for node in working_set.task_nodes if node.id is not None
    }
    deep_task_ids: list[int] = []
    heavy_task_ids: list[int] = []
    enriched: list[PlannerTaskRef] = []
    energy_specs, energy_source = _high_energy_window_specs(preference_context)
    preferred_windows = _daily_preferred_windows(
        window_start=window_start,
        window_end=window_end,
        specs=energy_specs,
    )
    for task in working_set.tasks:
        node = nodes_by_id.get(task.task_id)
        task_text = " ".join(
            str(value or "") for value in (
                task.title,
                getattr(node, "notes", None),
                getattr(node, "tags", None),
            )
        )
        is_deep = task.duration_minutes >= HEAVY_TASK_MINUTES or bool(
            DEEP_WORK_RE.search(task_text)
        )
        if is_deep:
            deep_task_ids.append(task.task_id)
        if task.duration_minutes >= HEAVY_TASK_MINUTES:
            heavy_task_ids.append(task.task_id)
        context_windows = tuple(
            (
                max(start_at - timedelta(minutes=60), window_start),
                min(end_at + timedelta(minutes=60), window_end),
            )
            for context_node, start_at, end_at in surrounding_task_windows or []
            if task.duration_minutes <= SMALL_BATCH_MAX_MINUTES
            and _ref_related_to_node(task, context_node)
            and min(end_at + timedelta(minutes=60), window_end)
            > max(start_at - timedelta(minutes=60), window_start)
        )
        enriched.append(
            replace(
                task,
                is_deep_work=is_deep,
                preferred_start_windows=(
                    preferred_windows if is_deep else context_windows
                ),
            )
        )

    explicit_sequence = bool(EXPLICIT_SEQUENCE_RE.search(prompt))
    selected_parent_ids = {task.parent_id for task in enriched}
    structural_sequence = (
        len(enriched) > 1
        and len(selected_parent_ids) == 1
        and None not in selected_parent_ids
    )
    if explicit_sequence or structural_sequence:
        ordered = enriched
    else:
        far_future = datetime.max.replace(microsecond=0)

        def base_key(task: PlannerTaskRef) -> tuple[Any, ...]:
            deadline = _parse_deadline(task.deadline_at)
            surrounding_affinity = any(
                _ref_related_to_node(task, node) for node in surrounding_tasks or []
            )
            return (
                deadline or far_future,
                _priority_rank(task.priority),
                0 if task.is_deep_work else 1,
                0 if surrounding_affinity else 1,
                original_index[task.task_id],
            )

        remaining = sorted(enriched, key=base_key)
        ordered = []
        while remaining:
            candidate_pool = remaining
            if ordered:
                previous = ordered[-1]
                previous_deadline = _parse_deadline(previous.deadline_at)
                same_deadline = [
                    task
                    for task in remaining
                    if _parse_deadline(task.deadline_at) == previous_deadline
                ]
                if same_deadline:
                    candidate_pool = same_deadline
                related_small = [
                    task
                    for task in candidate_pool
                    if previous.duration_minutes <= SMALL_BATCH_MAX_MINUTES
                    and task.duration_minutes <= SMALL_BATCH_MAX_MINUTES
                    and _tasks_are_related(previous, task)
                ]
                if related_small:
                    candidate_pool = related_small
                elif previous.duration_minutes >= HEAVY_TASK_MINUTES:
                    lighter = [
                        task
                        for task in candidate_pool
                        if task.duration_minutes < HEAVY_TASK_MINUTES
                    ]
                    if lighter:
                        candidate_pool = lighter
            next_task = min(candidate_pool, key=base_key)
            ordered.append(next_task)
            remaining.remove(next_task)

    context_ordered: list[PlannerTaskRef] = []
    for task in ordered:
        previous = context_ordered[-1] if context_ordered else None
        context_ordered.append(
            replace(task, preferred_start_windows=())
            if previous is not None
            and task.is_deep_work
            and _tasks_are_related(previous, task)
            else task
        )
    ordered = context_ordered

    heavy_recovery_gaps: dict[int, int] = {}
    for current, following in zip(ordered, ordered[1:]):
        if (
            current.duration_minutes >= HEAVY_TASK_MINUTES
            and following.duration_minutes >= HEAVY_TASK_MINUTES
        ):
            heavy_recovery_gaps[current.task_id] = HEAVY_RECOVERY_MINUTES

    ordered_ids = [task.task_id for task in ordered]
    batched_pairs = [
        [first.task_id, second.task_id]
        for first, second in zip(ordered, ordered[1:])
        if first.duration_minutes <= SMALL_BATCH_MAX_MINUTES
        and second.duration_minutes <= SMALL_BATCH_MAX_MINUTES
        and _tasks_are_related(first, second)
    ]
    return (
        PlannerWorkingSet(
            tasks=ordered,
            fallback_duration_task_ids=[
                task_id
                for task_id in working_set.fallback_duration_task_ids
                if task_id in ordered_ids
            ],
            task_nodes=[nodes_by_id[task_id] for task_id in ordered_ids if task_id in nodes_by_id],
            categories_by_id=working_set.categories_by_id,
        ),
        {
            "status": "applied",
            "explicitSequencePreserved": explicit_sequence,
            "structuralSequencePreserved": structural_sequence,
            "orderedTaskIds": ordered_ids,
            "deepWorkTaskIds": deep_task_ids,
            "heavyTaskIds": heavy_task_ids,
            "heavyRecoveryGapMinutesByTaskId": heavy_recovery_gaps,
            "batchedRelatedTaskPairs": batched_pairs,
            "highEnergyWindows": [
                {"startMinutes": start, "endMinutes": end}
                for start, end in energy_specs
            ],
            "highEnergyWindowSource": energy_source,
            "surroundingContextTaskIds": [
                int(node.id)
                for node in surrounding_tasks or []
                if node.id is not None
            ],
        },
    )


def local_wall_time(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise PlannerToolError(
            "InvalidPlannerRequest",
            "Planner datetimes must use local wall time without a UTC offset.",
        )
    return value


def _normalize_task_ids(task_ids: list[int]) -> list[int]:
    normalized: list[int] = []
    for task_id in task_ids:
        if isinstance(task_id, bool) or task_id <= 0:
            raise PlannerToolError("InvalidPlannerRequest", "workingSetTaskIds must contain positive task ids.")
        if task_id not in normalized:
            normalized.append(task_id)
    if not normalized:
        raise PlannerToolError("InvalidPlannerRequest", "workingSetTaskIds must contain at least one task id.")
    return normalized


def _children_by_parent(nodes: list[StrategyNode]) -> dict[int, list[StrategyNode]]:
    children: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.parent_id is None or node.id is None:
            continue
        children.setdefault(node.parent_id, []).append(node)
    for child_nodes in children.values():
        child_nodes.sort(key=lambda item: (item.sort_order, item.id or 0))
    return children


def _ancestor_ids(node: StrategyNode, nodes_by_id: dict[int, StrategyNode]) -> set[int]:
    ancestors: set[int] = set()
    parent_id = node.parent_id
    while parent_id is not None:
        if parent_id in ancestors:
            break
        ancestors.add(parent_id)
        parent = nodes_by_id.get(parent_id)
        if parent is None:
            break
        parent_id = parent.parent_id
    return ancestors


def _leaf_estimate_total(
    node_id: int,
    children_by_parent: dict[int, list[StrategyNode]],
) -> int:
    children = children_by_parent.get(node_id, [])
    if not children:
        return 0
    total = 0
    stack = list(children)
    while stack:
        node = stack.pop()
        descendants = children_by_parent.get(node.id or 0, [])
        if descendants:
            stack.extend(descendants)
            continue
        if node.time_estimate_minutes and node.time_estimate_minutes > 0:
            total += node.time_estimate_minutes
    return total


def _duration_for_task(
    task: StrategyNode,
    children_by_parent: dict[int, list[StrategyNode]],
    fallback_duration_minutes: int,
) -> tuple[int, bool]:
    if task.time_estimate_minutes and task.time_estimate_minutes > 0:
        return task.time_estimate_minutes, False
    descendant_total = _leaf_estimate_total(task.id or 0, children_by_parent)
    if descendant_total > 0:
        return descendant_total, False
    return fallback_duration_minutes, True


def resolve_selected_working_set(
    session: Session,
    *,
    task_ids: list[int],
    fallback_duration_minutes: int,
) -> PlannerWorkingSet:
    """Resolve selected working-set task refs from live task and category state."""

    normalized_task_ids = _normalize_task_ids(task_ids)
    tasks = session.exec(select(StrategyNode).where(StrategyNode.id.in_(normalized_task_ids))).all()
    task_by_id = {task.id: task for task in tasks}
    for task_id in normalized_task_ids:
        if task_id not in task_by_id:
            raise PlannerToolError("NotFound", f"Task {task_id} not found.", status_code=404)

    category_ids = sorted({task.category_id for task in tasks})
    categories = session.exec(select(Category).where(Category.id.in_(category_ids))).all()
    categories_by_id = {category.id: category for category in categories if category.id is not None}
    category_nodes = session.exec(
        select(StrategyNode).where(StrategyNode.category_id.in_(category_ids))
    ).all()
    all_nodes_by_id = {node.id: node for node in category_nodes if node.id is not None}
    children_by_parent = _children_by_parent(category_nodes)
    selected_ids = set(normalized_task_ids)
    root_ids = [
        task_id
        for task_id in normalized_task_ids
        if not (_ancestor_ids(task_by_id[task_id], all_nodes_by_id) & selected_ids)
    ]

    refs: list[PlannerTaskRef] = []
    fallback_ids: list[int] = []
    for task_id in root_ids:
        task = task_by_id[task_id]
        category = categories_by_id.get(task.category_id)
        if category is None:
            raise PlannerToolError(
                "NotFound",
                f"Category {task.category_id} for task {task_id} not found.",
                status_code=404,
            )
        duration_minutes, used_fallback = _duration_for_task(
            task,
            children_by_parent,
            fallback_duration_minutes,
        )
        if used_fallback:
            fallback_ids.append(task_id)
        refs.append(
            PlannerTaskRef(
                task_id=task_id,
                category_id=task.category_id,
                title=task.title,
                emoji=task.emoji,
                category_name=category.name,
                category_color=category.accent_color,
                status=task.status,
                priority=task.priority,
                deadline_at=task.deadline_at,
                time_estimate_minutes=task.time_estimate_minutes,
                duration_minutes=duration_minutes,
                used_fallback_duration=used_fallback,
                parent_id=task.parent_id,
                rendered_child_task_ids=[
                    child.id or 0
                    for child in children_by_parent.get(task_id, [])
                    if child.id is not None
                ],
            )
        )
    return PlannerWorkingSet(
        tasks=refs,
        fallback_duration_task_ids=fallback_ids,
        task_nodes=[task_by_id[task_id] for task_id in root_ids],
        categories_by_id=categories_by_id,
    )


def read_calendar_state(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[CalendarBlock]:
    """Read accepted calendar occupancy for the visible Planner window."""

    return session.exec(
        select(CalendarBlock)
        .where(CalendarBlock.start_at < window_end)
        .where(CalendarBlock.end_at > window_start)
        .order_by(CalendarBlock.start_at, CalendarBlock.id)
    ).all()


def read_calendar_blocks_for_tasks(
    session: Session,
    task_ids: list[int],
) -> list[CalendarBlock]:
    """Read accepted source blocks for task-scoped reschedule/remove operations."""

    if not task_ids:
        return []
    return list(
        session.exec(
            select(CalendarBlock)
            .where(CalendarBlock.task_id.in_(task_ids))
            .order_by(CalendarBlock.start_at, CalendarBlock.id)
        ).all()
    )


def read_scheduled_task_ids(
    session: Session,
    task_ids: list[int],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[int]:
    """Return selected task ids with accepted blocks in the relevant Planner window."""

    if not task_ids:
        return []

    statement = select(CalendarBlock.task_id).where(CalendarBlock.task_id.in_(task_ids))
    if window_start is not None:
        statement = statement.where(CalendarBlock.end_at > window_start)
    if window_end is not None:
        statement = statement.where(CalendarBlock.start_at < window_end)
    rows = session.exec(statement.order_by(CalendarBlock.start_at, CalendarBlock.id)).all()
    scheduled_ids = {int(task_id) for task_id in rows}
    return [task_id for task_id in task_ids if task_id in scheduled_ids]


def filter_working_set(
    working_set: PlannerWorkingSet,
    *,
    excluded_task_ids: set[int],
) -> PlannerWorkingSet:
    """Remove already-committed tasks from solver input while preserving context."""

    if not excluded_task_ids:
        return working_set

    return PlannerWorkingSet(
        tasks=[
            task for task in working_set.tasks if task.task_id not in excluded_task_ids
        ],
        fallback_duration_task_ids=[
            task_id
            for task_id in working_set.fallback_duration_task_ids
            if task_id not in excluded_task_ids
        ],
        task_nodes=[
            task
            for task in working_set.task_nodes
            if task.id is not None and task.id not in excluded_task_ids
        ],
        categories_by_id=working_set.categories_by_id,
    )


def apply_planner_task_directives(
    working_set: PlannerWorkingSet,
    directives: Any,
    *,
    gap_policy: dict[str, Any] | None = None,
    default_heavy_task_ids: set[int] | None = None,
    preserve_submitted_order: bool = False,
) -> tuple[PlannerWorkingSet, dict[str, Any]]:
    """Compile model task choices into bounded solver input.

    The model may order, timebox, or intentionally skip tasks, but it cannot
    introduce task ids or erase the whole submitted working set. Invalid or
    incomplete choices degrade to the live task evidence instead of becoming
    solver authority.
    """

    tasks_by_id = {task.task_id: task for task in working_set.tasks}
    nodes_by_id = {
        int(task.id): task
        for task in working_set.task_nodes
        if task.id is not None
    }
    ordered_tasks: list[PlannerTaskRef] = []
    seen_task_ids: set[int] = set()
    skipped_task_ids: list[int] = []
    duration_overrides: list[dict[str, int]] = []
    gap_directives: list[dict[str, int]] = []
    requested_gaps_by_task_id: dict[int, int | None] = {}
    warnings: list[str] = []

    raw_soft_default = (gap_policy or {}).get("softDefaultMinutes", 0)
    soft_default_minutes = (
        raw_soft_default
        if isinstance(raw_soft_default, int)
        and not isinstance(raw_soft_default, bool)
        and 0 <= raw_soft_default <= 1440
        else 0
    )
    raw_hard_minimum = (gap_policy or {}).get("hardMinimumMinutes", 0)
    hard_minimum_minutes = (
        raw_hard_minimum
        if isinstance(raw_hard_minimum, int)
        and not isinstance(raw_hard_minimum, bool)
        and 0 <= raw_hard_minimum <= 1440
        else 0
    )
    conditional_gap_rules = [
        rule
        for rule in (gap_policy or {}).get("conditionalGapRules", [])
        if isinstance(rule, dict)
        and isinstance(rule.get("gapMinutes"), int)
        and not isinstance(rule.get("gapMinutes"), bool)
        and isinstance(rule.get("minimumTaskDurationMinutes"), int)
        and not isinstance(rule.get("minimumTaskDurationMinutes"), bool)
    ]

    def conditional_gap_for_task(task: PlannerTaskRef, enforcement: str) -> int:
        return max(
            [
                int(rule["gapMinutes"])
                for rule in conditional_gap_rules
                if str(rule.get("enforcement") or "soft").strip().lower() == enforcement
                and task.duration_minutes >= int(rule["minimumTaskDurationMinutes"])
                and (
                    not isinstance(rule.get("maximumTaskDurationMinutes"), int)
                    or isinstance(rule.get("maximumTaskDurationMinutes"), bool)
                    or task.duration_minutes
                    <= int(rule["maximumTaskDurationMinutes"])
                )
            ]
            or [0]
        )

    if not isinstance(directives, list):
        directives = []
        warnings.append("Planner returned no usable task directives; preserved the submitted task order.")
    if preserve_submitted_order and not directives:
        ordered_tasks = list(working_set.tasks)
        seen_task_ids = {task.task_id for task in ordered_tasks}

    for index, directive in enumerate(directives):
        if not isinstance(directive, dict):
            warnings.append(f"Ignored Planner directive {index + 1} because it was not an object.")
            continue
        raw_task_id = directive.get("task_id")
        if isinstance(raw_task_id, bool) or not isinstance(raw_task_id, int):
            warnings.append(f"Ignored Planner directive {index + 1} because task_id was invalid.")
            continue
        task = tasks_by_id.get(raw_task_id)
        if task is None:
            warnings.append(f"Ignored Planner directive for task {raw_task_id}; it is outside the submitted working set.")
            continue
        if raw_task_id in seen_task_ids:
            warnings.append(f"Ignored duplicate Planner directive for task {raw_task_id}.")
            continue
        seen_task_ids.add(raw_task_id)

        action = str(directive.get("action") or "schedule").strip().lower()
        if action == "skip":
            skipped_task_ids.append(raw_task_id)
            continue
        if action != "schedule":
            warnings.append(
                f"Planner directive for task {raw_task_id} used unsupported action {action!r}; treated it as schedule."
            )

        duration = directive.get("allocated_minutes")
        planned_task = task
        if duration is not None:
            if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 1440:
                warnings.append(
                    f"Ignored invalid Planner timebox for task {raw_task_id}; preserved {task.duration_minutes} minutes."
                )
            elif duration != task.duration_minutes:
                planned_task = replace(task, duration_minutes=duration)
                duration_overrides.append(
                    {
                        "taskId": raw_task_id,
                        "evidenceMinutes": task.duration_minutes,
                        "allocatedMinutes": duration,
                    }
                )
        gap_after_minutes = directive.get("gap_after_minutes")
        if gap_after_minutes is not None and (
            isinstance(gap_after_minutes, bool)
            or not isinstance(gap_after_minutes, int)
            or not 0 <= gap_after_minutes <= 1440
        ):
            warnings.append(
                f"Ignored invalid Planner gap for task {raw_task_id}; used the remembered default."
            )
            gap_after_minutes = None
        requested_gaps_by_task_id[raw_task_id] = gap_after_minutes
        ordered_tasks.append(planned_task)

    missing_task_ids = [
        task.task_id for task in working_set.tasks if task.task_id not in seen_task_ids
    ]
    if missing_task_ids:
        warnings.append(
            "Planner omitted submitted tasks; appended them in their original order: "
            + ", ".join(str(task_id) for task_id in missing_task_ids)
            + "."
        )
        ordered_tasks.extend(tasks_by_id[task_id] for task_id in missing_task_ids)

    if not ordered_tasks and working_set.tasks:
        warnings.append("Planner skipped every submitted task; preserved the submitted working set instead.")
        ordered_tasks = list(working_set.tasks)
        skipped_task_ids = []

    automatic_heavy_gaps = {
        current.task_id: HEAVY_RECOVERY_MINUTES
        for current, following in zip(ordered_tasks, ordered_tasks[1:])
        if current.task_id in (default_heavy_task_ids or set())
        and following.task_id in (default_heavy_task_ids or set())
    }
    resolved_tasks: list[PlannerTaskRef] = []
    hard_minimum_applied_task_ids: list[int] = []
    conditional_rule_applied_task_ids: list[int] = []
    for task in ordered_tasks:
        requested_gap = requested_gaps_by_task_id.get(task.task_id)
        conditional_soft_gap = conditional_gap_for_task(task, "soft")
        conditional_hard_gap = conditional_gap_for_task(task, "hard")
        if conditional_soft_gap or conditional_hard_gap:
            conditional_rule_applied_task_ids.append(task.task_id)
        heuristic_gap_after = (
            int(automatic_heavy_gaps.get(task.task_id) or 0)
            if requested_gap is None
            else 0
        )
        preferred_gap_after = (
            requested_gap
            if requested_gap is not None
            else max(soft_default_minutes, conditional_soft_gap, heuristic_gap_after)
        )
        preferred_gap_before = (
            0
            if requested_gap == 0
            else max(soft_default_minutes, conditional_soft_gap)
        )
        effective_hard_minimum = max(hard_minimum_minutes, conditional_hard_gap)
        effective_gap_before = max(preferred_gap_before, effective_hard_minimum)
        effective_gap_after = max(preferred_gap_after, effective_hard_minimum)
        if (
            effective_gap_before > preferred_gap_before
            or effective_gap_after > preferred_gap_after
        ):
            hard_minimum_applied_task_ids.append(task.task_id)
        if effective_gap_after:
            gap_directives.append(
                {
                    "taskId": task.task_id,
                    "gapAfterMinutes": effective_gap_after,
                }
            )
        resolved_tasks.append(
            replace(
                task,
                gap_before_minutes=effective_gap_before,
                gap_after_minutes=effective_gap_after,
            )
        )
    ordered_tasks = resolved_tasks

    ordered_task_ids = [task.task_id for task in ordered_tasks]
    return (
        PlannerWorkingSet(
            tasks=ordered_tasks,
            fallback_duration_task_ids=[
                task_id
                for task_id in working_set.fallback_duration_task_ids
                if task_id in ordered_task_ids
            ],
            task_nodes=[nodes_by_id[task_id] for task_id in ordered_task_ids if task_id in nodes_by_id],
            categories_by_id=working_set.categories_by_id,
        ),
        {
            "status": "normalized_with_warnings" if warnings else "accepted",
            "orderedTaskIds": ordered_task_ids,
            "skippedTaskIds": skipped_task_ids,
            "durationOverrides": duration_overrides,
            "gapDirectives": gap_directives,
            "gapPolicy": {
                "softDefaultMinutes": soft_default_minutes,
                "hardMinimumMinutes": hard_minimum_minutes,
                "hardMinimumAppliedTaskIds": hard_minimum_applied_task_ids,
                "conditionalGapRules": conditional_gap_rules,
                "conditionalRuleAppliedTaskIds": conditional_rule_applied_task_ids,
                "sources": list((gap_policy or {}).get("sources") or []),
            },
            "warnings": warnings,
        },
    )


def run_deterministic_schedule_tool(
    *,
    working_set: PlannerWorkingSet,
    accepted_blocks: list[CalendarBlock],
    extra_occupied: list[OccupiedInterval] | None = None,
    window_start: datetime,
    window_end: datetime,
    slot_minutes: int,
    alternative_window_end: datetime | None = None,
) -> SolverResult:
    """Call the existing deterministic solver for Planner proposals."""

    accepted_occupied = [
        OccupiedInterval(
            block_id=block.id or 0,
            start_at=local_wall_time(block.start_at),
            end_at=local_wall_time(block.end_at),
        )
        for block in accepted_blocks
    ]
    return solve_schedule_draft(
        tasks=[
            SolverTask(
                task_id=task.task_id,
                duration_minutes=task.duration_minutes,
                gap_before_minutes=task.gap_before_minutes,
                gap_after_minutes=task.gap_after_minutes,
                preferred_start_windows=task.preferred_start_windows,
                allowed_start_windows=task.allowed_start_windows,
            )
            for task in working_set.tasks
        ],
        window_start=window_start,
        window_end=window_end,
        occupied=[*accepted_occupied, *(extra_occupied or [])],
        slot_minutes=slot_minutes,
        alternative_window_end=alternative_window_end,
    )


def working_set_context_payload(working_set: PlannerWorkingSet) -> list[dict[str, Any]]:
    return [
        {
            "taskId": task.task_id,
            "title": task.title,
            "categoryId": task.category_id,
            "categoryName": task.category_name,
            "status": task.status,
            "priority": task.priority,
            "deadlineAt": task.deadline_at,
            "timeEstimateMinutes": task.time_estimate_minutes,
            "durationMinutes": task.duration_minutes,
            "usedFallbackDuration": task.used_fallback_duration,
            "parentId": task.parent_id,
            "renderedChildTaskIds": task.rendered_child_task_ids,
        }
        for task in working_set.tasks
    ]
