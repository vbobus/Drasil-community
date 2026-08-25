"""
Drasil - Scheduling API Router
------------------------------
Dry-run deterministic scheduling endpoints.
"""

from datetime import datetime
from time import perf_counter
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, alias_generators
from sqlmodel import Session, select

from app.db.database import get_session
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.scheduling_solver import (
    OccupiedInterval,
    SolverTask,
    solve_schedule_draft,
)
from app.services.preference_memory import (
    build_preference_context_for_scope,
    constraint_intervals_from_context,
)

router = APIRouter(prefix="/api/v1/scheduling", tags=["Scheduling"])


class AutoScheduleDraftRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    task_ids: List[int]
    window_start: datetime
    window_end: datetime
    slot_minutes: int = 15
    fallback_duration_minutes: int = 30


class ScheduleAlternativeRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    start_at: datetime
    end_at: datetime
    reason: str


class ScheduleConflictRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    task_id: int
    type: str
    detail: str
    conflicting_block_ids: List[int] = Field(default_factory=list)
    alternatives: List[ScheduleAlternativeRead] = Field(default_factory=list)


class DraftCalendarBlockRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    task_id: int
    category_id: int
    title: str
    emoji: Optional[str] = None
    category_name: str
    category_color: str
    start_at: datetime
    end_at: datetime
    variant: str = "draft"
    completion_status: str = "not_started"
    task_status: str
    task_deadline_at: Optional[str] = None
    task_priority: Optional[str] = None
    time_estimate_minutes: Optional[int] = None
    is_pinned: bool = False
    is_protected: bool = False
    is_recurring: bool = False
    hierarchy_depth: int = 0
    rendered_child_task_ids: List[int] = Field(default_factory=list)


class ScheduleDiagnosticsRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    solver_status: str
    runtime_ms: int
    scheduled_task_count: int
    conflict_count: int
    occupied_block_count: int
    fallback_duration_task_ids: List[int] = Field(default_factory=list)
    already_scheduled_task_ids: List[int] = Field(default_factory=list)


class AutoScheduleDraftResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    draft_blocks: List[DraftCalendarBlockRead]
    conflicts: List[ScheduleConflictRead]
    diagnostics: ScheduleDiagnosticsRead


def _error(status_code: int, error_type: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "type": error_type,
                "detail": detail,
            }
        },
    )


def _local_wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _validate_request(body: AutoScheduleDraftRequest) -> tuple[list[int], datetime, datetime]:
    task_ids: list[int] = []
    for task_id in body.task_ids:
        if task_id <= 0:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "InvalidScheduleRequest",
                "taskIds must contain positive task ids.",
            )
        if task_id not in task_ids:
            task_ids.append(task_id)

    if not task_ids:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "InvalidScheduleRequest",
            "taskIds must contain at least one task id.",
        )

    if body.slot_minutes <= 0:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "InvalidScheduleRequest",
            "slotMinutes must be positive.",
        )

    if body.fallback_duration_minutes <= 0:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "InvalidScheduleRequest",
            "fallbackDurationMinutes must be positive.",
        )

    window_start = _local_wall_time(body.window_start)
    window_end = _local_wall_time(body.window_end)
    if window_end <= window_start:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "InvalidScheduleRequest",
            "windowEnd must be after windowStart.",
        )

    return task_ids, window_start, window_end


def _children_by_parent(nodes: list[StrategyNode]) -> dict[int, list[StrategyNode]]:
    children: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.parent_id is None:
            continue
        children.setdefault(node.parent_id, []).append(node)
    for child_nodes in children.values():
        child_nodes.sort(key=lambda item: (item.sort_order, item.id or 0))
    return children


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


def _draft_id(task_id: int, start_at: datetime) -> str:
    return f"draft-{task_id}-{start_at.strftime('%Y%m%dT%H%M%S')}"


def _task_ids_with_calendar_blocks(session: Session, task_ids: list[int]) -> list[int]:
    if not task_ids:
        return []

    rows = session.exec(
        select(CalendarBlock.task_id)
        .where(CalendarBlock.task_id.in_(task_ids))
        .order_by(CalendarBlock.start_at, CalendarBlock.id)
    ).all()
    scheduled_ids = {int(task_id) for task_id in rows}
    return [task_id for task_id in task_ids if task_id in scheduled_ids]


@router.post("/auto-draft", response_model=AutoScheduleDraftResponse)
def auto_schedule_draft(
    body: AutoScheduleDraftRequest,
    session: Session = Depends(get_session),
) -> AutoScheduleDraftResponse:
    started_at = perf_counter()
    task_ids, window_start, window_end = _validate_request(body)

    tasks = session.exec(select(StrategyNode).where(StrategyNode.id.in_(task_ids))).all()
    task_by_id = {task.id: task for task in tasks}
    for task_id in task_ids:
        if task_id not in task_by_id:
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "NotFound",
                f"Task {task_id} not found.",
            )

    category_ids = sorted({task.category_id for task in tasks})
    categories = session.exec(select(Category).where(Category.id.in_(category_ids))).all()
    category_by_id = {category.id: category for category in categories}
    category_nodes = session.exec(
        select(StrategyNode).where(StrategyNode.category_id.in_(category_ids))
    ).all()
    children_by_parent = _children_by_parent(category_nodes)

    already_scheduled_task_ids = _task_ids_with_calendar_blocks(session, task_ids)
    already_scheduled_set = set(already_scheduled_task_ids)
    schedulable_task_ids = [
        task_id for task_id in task_ids if task_id not in already_scheduled_set
    ]

    solver_tasks: list[SolverTask] = []
    fallback_duration_task_ids: list[int] = []
    for task_id in schedulable_task_ids:
        task = task_by_id[task_id]
        duration_minutes, used_fallback = _duration_for_task(
            task,
            children_by_parent,
            body.fallback_duration_minutes,
        )
        if used_fallback:
            fallback_duration_task_ids.append(task_id)
        solver_tasks.append(SolverTask(task_id=task_id, duration_minutes=duration_minutes))

    occupied_blocks = session.exec(
        select(CalendarBlock)
        .where(CalendarBlock.start_at < window_end)
        .where(CalendarBlock.end_at > window_start)
        .order_by(CalendarBlock.start_at, CalendarBlock.id)
    ).all()
    occupied = [
        OccupiedInterval(
            block_id=block.id or 0,
            start_at=_local_wall_time(block.start_at),
            end_at=_local_wall_time(block.end_at),
        )
        for block in occupied_blocks
    ]
    preference_context = build_preference_context_for_scope(
        session,
        task_ids=schedulable_task_ids,
        category_ids=[
            task_by_id[task_id].category_id for task_id in schedulable_task_ids
        ],
        task_titles=[task_by_id[task_id].title for task_id in schedulable_task_ids],
        category_names=[
            category_by_id[task_by_id[task_id].category_id].name
            for task_id in schedulable_task_ids
            if task_by_id[task_id].category_id in category_by_id
        ],
        window_start=window_start,
        window_end=window_end,
    )
    occupied.extend(
        constraint_intervals_from_context(
            preference_context,
            window_start=window_start,
            window_end=window_end,
        )
    )

    solver_result = solve_schedule_draft(
        tasks=solver_tasks,
        window_start=window_start,
        window_end=window_end,
        occupied=occupied,
        slot_minutes=body.slot_minutes,
    )
    placement_by_task_id = {
        placement.task_id: placement for placement in solver_result.placements
    }

    draft_blocks: list[DraftCalendarBlockRead] = []
    for task_id in schedulable_task_ids:
        placement = placement_by_task_id.get(task_id)
        if placement is None:
            continue
        task = task_by_id[task_id]
        category = category_by_id[task.category_id]
        child_ids = [
            child.id or 0
            for child in children_by_parent.get(task_id, [])
            if child.id is not None
        ]
        draft_blocks.append(
            DraftCalendarBlockRead(
                id=_draft_id(task_id, placement.start_at),
                task_id=task_id,
                category_id=task.category_id,
                title=task.title,
                emoji=task.emoji,
                category_name=category.name,
                category_color=category.accent_color,
                start_at=placement.start_at,
                end_at=placement.end_at,
                task_status=task.status,
                task_deadline_at=task.deadline_at,
                task_priority=task.priority,
                time_estimate_minutes=task.time_estimate_minutes,
                is_pinned=False,
                is_protected=False,
                is_recurring=False,
                hierarchy_depth=0,
                rendered_child_task_ids=child_ids,
            )
        )

    conflicts = [
        ScheduleConflictRead(
            task_id=conflict.task_id,
            type=conflict.type,
            detail=conflict.detail,
            conflicting_block_ids=conflict.conflicting_block_ids,
            alternatives=[
                ScheduleAlternativeRead(
                    start_at=alternative.start_at,
                    end_at=alternative.end_at,
                    reason=alternative.reason,
                )
                for alternative in conflict.alternatives
            ],
        )
        for conflict in solver_result.conflicts
    ]

    runtime_ms = int((perf_counter() - started_at) * 1000)
    return AutoScheduleDraftResponse(
        draft_blocks=draft_blocks,
        conflicts=conflicts,
        diagnostics=ScheduleDiagnosticsRead(
            solver_status=solver_result.solver_status,
            runtime_ms=runtime_ms,
            scheduled_task_count=len(draft_blocks),
            conflict_count=len(conflicts),
            occupied_block_count=len(occupied_blocks),
            fallback_duration_task_ids=fallback_duration_task_ids,
            already_scheduled_task_ids=already_scheduled_task_ids,
        ),
    )
