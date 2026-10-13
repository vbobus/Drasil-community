"""
Drasil - Calendar Blocks API Router
-----------------------------------
Endpoints:
  GET  /api/v1/calendar-blocks?start={iso}&end={iso} -> CalendarBlockRead[]
  GET  /api/v1/calendar-blocks/scheduled-task-ids      -> int[]
  POST /api/v1/calendar-blocks                      -> CalendarBlockRead
  PATCH /api/v1/calendar-blocks/{id}                -> CalendarBlockRead
  DELETE /api/v1/calendar-blocks/{id}               -> 204
  POST /api/v1/calendar-blocks/{id}/unschedule       -> planned day and membership
"""

from datetime import date, datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, alias_generators, model_validator
from sqlmodel import Session, select

from app.db.database import get_session
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceOccurrenceException, RecurrenceSeries
from app.services.calendar_assignment import moved_planned_day
from app.services.google_calendar import delete_google_event_for_block, try_sync_block_to_google
from app.services.recurring_schedules import materialize_active_recurrence_series

router = APIRouter(prefix="/api/v1/calendar-blocks", tags=["Calendar Blocks"])

VALID_VARIANTS = {"scheduled", "protected", "draft", "proposed"}
VALID_COMPLETION_STATUSES = {
    "not_started",
    "in_progress",
    "completed",
    "skipped",
    "missed",
    "partial",
}


class CalendarBlockRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: int
    task_id: int
    category_id: int
    title: str
    emoji: Optional[str]
    category_name: str
    category_color: str
    start_at: datetime
    end_at: datetime
    variant: str
    completion_status: str
    task_status: str
    task_deadline_at: Optional[str] = None
    task_planned_date: Optional[str] = None
    task_priority: Optional[str] = None
    time_estimate_minutes: Optional[int] = None
    is_pinned: bool
    is_protected: bool
    is_recurring: bool
    google_event_id: Optional[str] = None
    google_last_synced_at: Optional[datetime] = None
    google_sync_error: Optional[str] = None
    hierarchy_depth: int = 0
    rendered_child_task_ids: List[int] = Field(default_factory=list)


class CalendarBlockCreate(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    task_id: int
    category_id: int
    start_at: datetime
    end_at: datetime
    variant: Literal["scheduled", "protected"] = "scheduled"
    is_pinned: bool = False
    is_protected: bool = False
    is_recurring: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> "CalendarBlockCreate":
        if self.end_at <= self.start_at:
            raise ValueError("endAt must be after startAt")
        return self


class CalendarBlockUpdate(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_range_when_complete(self) -> "CalendarBlockUpdate":
        if self.start_at is not None and self.end_at is not None:
            if self.end_at <= self.start_at:
                raise ValueError("endAt must be after startAt")
        return self


def parse_range_datetime(value: str, param_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "type": "InvalidDateRange",
                    "detail": f"{param_name} must be a valid ISO datetime.",
                }
            },
        ) from exc


def assert_valid_range(start_at: datetime, end_at: datetime) -> None:
    if end_at <= start_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "type": "InvalidDateRange",
                    "detail": "end must be after start.",
                }
            },
        )


def block_to_read(
    block: CalendarBlock,
    task: StrategyNode,
    category: Category,
) -> CalendarBlockRead:
    return CalendarBlockRead(
        id=block.id or 0,
        task_id=block.task_id,
        category_id=block.category_id,
        title=task.title,
        emoji=task.emoji,
        category_name=category.name,
        category_color=category.accent_color,
        start_at=block.start_at,
        end_at=block.end_at,
        variant=block.variant,
        completion_status=block.completion_status,
        task_status=task.status,
        task_deadline_at=task.deadline_at,
        task_planned_date=task.planned_date,
        task_priority=task.priority,
        time_estimate_minutes=task.time_estimate_minutes,
        is_pinned=block.is_pinned,
        is_protected=block.is_protected,
        is_recurring=block.is_recurring,
        google_event_id=block.google_event_id,
        google_last_synced_at=block.google_last_synced_at,
        google_sync_error=block.google_sync_error,
        hierarchy_depth=0,
        rendered_child_task_ids=[],
    )


def get_block_with_identity(
    session: Session,
    block_id: int,
) -> tuple[CalendarBlock, StrategyNode, Category]:
    row = session.exec(
        select(CalendarBlock, StrategyNode, Category)
        .join(StrategyNode, StrategyNode.id == CalendarBlock.task_id)
        .join(Category, Category.id == CalendarBlock.category_id)
        .where(CalendarBlock.id == block_id)
        .where(StrategyNode.category_id == CalendarBlock.category_id)
    ).first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "NotFound",
                    "detail": f"Calendar block {block_id} not found.",
                }
            },
        )

    block, task, category = row
    return block, task, category


@router.get("", response_model=List[CalendarBlockRead])
def list_calendar_blocks(
    start: str = Query(...),
    end: str = Query(...),
    session: Session = Depends(get_session),
) -> List[CalendarBlockRead]:
    start_at = parse_range_datetime(start, "start")
    end_at = parse_range_datetime(end, "end")
    assert_valid_range(start_at, end_at)
    materialize_active_recurrence_series(
        session,
        through_date=end_at.date() + timedelta(days=90),
    )

    rows = session.exec(
        select(CalendarBlock, StrategyNode, Category)
        .join(StrategyNode, StrategyNode.id == CalendarBlock.task_id)
        .join(Category, Category.id == CalendarBlock.category_id)
        .where(CalendarBlock.start_at < end_at)
        .where(CalendarBlock.end_at > start_at)
        .where(StrategyNode.category_id == CalendarBlock.category_id)
        .order_by(CalendarBlock.start_at, CalendarBlock.id)
    ).all()

    return [block_to_read(block, task, category) for block, task, category in rows]


@router.get("/scheduled-task-ids", response_model=List[int])
def list_scheduled_task_ids(
    session: Session = Depends(get_session),
) -> List[int]:
    """Return accepted schedule membership independently of a calendar view range."""

    block_task_ids = session.exec(
        select(CalendarBlock.task_id)
        .where(CalendarBlock.variant.in_(("scheduled", "protected")))
        .distinct()
    ).all()
    recurring_task_ids = session.exec(
        select(RecurrenceSeries.task_id)
        .where(RecurrenceSeries.status == "active")
        .distinct()
    ).all()
    return sorted({int(task_id) for task_id in [*block_task_ids, *recurring_task_ids]})


@router.post(
    "",
    response_model=CalendarBlockRead,
    status_code=status.HTTP_201_CREATED,
)
def create_calendar_block(
    body: CalendarBlockCreate,
    session: Session = Depends(get_session),
) -> CalendarBlockRead:
    task = session.get(StrategyNode, body.task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "NotFound",
                    "detail": f"Task {body.task_id} not found.",
                }
            },
        )

    category = session.get(Category, body.category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "NotFound",
                    "detail": f"Category {body.category_id} not found.",
                }
            },
        )

    if task.category_id != body.category_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "type": "InvalidCalendarBlock",
                    "detail": (
                        f"Task {body.task_id} does not belong to category "
                        f"{body.category_id}."
                    ),
                }
            },
        )

    block = CalendarBlock(
        task_id=body.task_id,
        category_id=body.category_id,
        start_at=body.start_at,
        end_at=body.end_at,
        variant=body.variant,
        completion_status="not_started",
        is_pinned=body.is_pinned,
        is_protected=body.is_protected or body.variant == "protected",
        is_recurring=body.is_recurring,
    )
    session.add(block)
    session.commit()
    session.refresh(block)
    try_sync_block_to_google(session, block)

    return block_to_read(block, task, category)


class MoveTaskDayRequest(BaseModel):
    model_config = ConfigDict(alias_generator=alias_generators.to_camel, populate_by_name=True)
    task_id: int
    source_date: date
    target_date: date


class RemoveTaskDayRequest(BaseModel):
    model_config = ConfigDict(alias_generator=alias_generators.to_camel, populate_by_name=True)
    task_id: int
    planned_date: date


@router.post("/remove-task-day")
def remove_task_day(body: RemoveTaskDayRequest, session: Session = Depends(get_session)) -> dict:
    """Remove a card's day assignment, preserving task identities and other days."""
    task = session.get(StrategyNode, body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    nodes = session.exec(select(StrategyNode).where(StrategyNode.category_id == task.category_id)).all()
    children: dict[int, list[int]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)
    ids, pending = set(), [task.id]
    while pending:
        node_id = pending.pop()
        if node_id in ids:
            continue
        ids.add(node_id)
        pending.extend(children.get(node_id, []))
    members = session.exec(select(StrategyNode).where(StrategyNode.id.in_(ids)).order_by(StrategyNode.id).with_for_update()).all()
    start = datetime.combine(body.planned_date, datetime.min.time())
    end = start + timedelta(days=1)
    blocks = session.exec(select(CalendarBlock).where(CalendarBlock.task_id.in_(ids))
        .where(CalendarBlock.variant.in_(("scheduled", "protected")))
        .where(CalendarBlock.start_at < end).where(CalendarBlock.end_at > start)
        .order_by(CalendarBlock.id).with_for_update()).all()
    # External deletion may commit connection metadata. Finish it before any local mutation.
    for block in blocks:
        if block.start_at >= start and block.end_at <= end:
            if delete_google_event_for_block(session, block):
                raise HTTPException(status_code=502, detail="Could not remove the linked calendar booking. Please retry.")
    changed, removed = [], []
    planned_dates = {}
    for node in members:
        if node.planned_date == body.planned_date.isoformat():
            node.planned_date = None
            planned_dates[str(node.id)] = None
            session.add(node)
    for block in blocks:
        old_start, old_end = block.start_at, block.end_at
        if old_start < start and old_end > end:
            # Split a multiday booking around the removed day, retaining its earlier ID.
            tail = CalendarBlock(task_id=block.task_id, category_id=block.category_id,
                start_at=end, end_at=old_end, variant=block.variant,
                completion_status=block.completion_status, is_pinned=block.is_pinned,
                is_protected=block.is_protected)
            session.add(tail)
            changed.append(tail)
            block.end_at = start
        elif old_start < start:
            block.end_at = start
        elif old_end > end:
            block.start_at = end
        else:
            removed.append(block.id)
        if block.recurrence_series_id and block.recurrence_key:
            key = f"{block.recurrence_series_id}:{block.recurrence_key}"
            exception = session.get(RecurrenceOccurrenceException, key) or RecurrenceOccurrenceException(
                id=key, series_id=block.recurrence_series_id, recurrence_key=block.recurrence_key, disposition="cancelled")
            exception.disposition = "cancelled" if block.id in removed else "overridden"
            exception.start_at = None if block.id in removed else block.start_at
            exception.end_at = None if block.id in removed else block.end_at
            exception.updated_at = datetime.utcnow()
            session.add(exception)
        if block.id in removed:
            session.delete(block)
        else:
            session.add(block)
            changed.append(block)
    session.flush()
    result = {"plannedDates": planned_dates, "removedBlockIds": removed,
              "scheduledTaskIds": list_scheduled_task_ids(session)}
    session.commit()
    for block in changed:
        session.refresh(block)
        try_sync_block_to_google(session, block)
    member_by_id = {node.id: node for node in members}
    category = session.get(Category, task.category_id)
    result["updatedBlocks"] = [block_to_read(block, member_by_id[block.task_id], category).model_dump(mode="json", by_alias=True) for block in changed]
    return result


@router.post("/move-task-day", response_model=List[CalendarBlockRead])
def move_task_day(
    body: MoveTaskDayRequest,
    session: Session = Depends(get_session),
) -> List[CalendarBlockRead]:
    """Move the existing task and its source-day bookings in one transaction."""
    task = session.exec(select(StrategyNode).where(StrategyNode.id == body.task_id).with_for_update()).first()
    category = session.get(Category, task.category_id) if task else None
    if not task or not category:
        raise HTTPException(status_code=404, detail="Task not found.")
    start = datetime.combine(body.source_date, datetime.min.time())
    blocks = session.exec(select(CalendarBlock)
        .where(CalendarBlock.task_id == body.task_id)
        .where(CalendarBlock.category_id == task.category_id)
        .where(CalendarBlock.variant.in_(("scheduled", "protected")))
        .where(CalendarBlock.start_at < start + timedelta(days=1))
        .where(CalendarBlock.end_at > start)).all()
    shift = body.target_date - body.source_date
    if not shift:
        return [block_to_read(block, task, category) for block in blocks]
    if not blocks and task.planned_date != body.source_date.isoformat():
        raise HTTPException(status_code=409, detail="The task has already moved. Refresh and try again.")
    for block in blocks:
        block.start_at += shift
        block.end_at += shift
        session.add(block)
        if block.recurrence_series_id and block.recurrence_key:
            exception_id = f"{block.recurrence_series_id}:{block.recurrence_key}"
            exception = session.get(RecurrenceOccurrenceException, exception_id)
            if exception is None:
                exception = RecurrenceOccurrenceException(id=exception_id,
                    series_id=block.recurrence_series_id, recurrence_key=block.recurrence_key,
                    disposition="overridden")
            exception.disposition = "overridden"
            exception.start_at, exception.end_at = block.start_at, block.end_at
            exception.updated_at = datetime.utcnow()
            session.add(exception)
    task.planned_date = body.target_date.isoformat()
    task.is_backlog = False
    session.add(task)
    session.commit()
    for block in blocks:
        session.refresh(block)
        try_sync_block_to_google(session, block)
    return [block_to_read(block, task, category) for block in blocks]


@router.patch("/{block_id}", response_model=CalendarBlockRead)
def update_calendar_block(
    block_id: int,
    body: CalendarBlockUpdate,
    session: Session = Depends(get_session),
) -> CalendarBlockRead:
    block, task, category = get_block_with_identity(session, block_id)
    start_at = body.start_at if body.start_at is not None else block.start_at
    end_at = body.end_at if body.end_at is not None else block.end_at

    if end_at <= start_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "type": "InvalidCalendarBlock",
                    "detail": "endAt must be after startAt.",
                }
            },
        )

    # A booking moved off its assigned day carries that assignment with it.
    # A separate explicit plan on another day remains independent.
    target_day = moved_planned_day(task.planned_date, block.start_at, start_at)
    if block.variant in {"scheduled", "protected"} and target_day is not None:
        task.planned_date = target_day.isoformat()
        session.add(task)

    block.start_at = start_at
    block.end_at = end_at
    session.add(block)
    if block.recurrence_series_id and block.recurrence_key:
        exception_id = f"{block.recurrence_series_id}:{block.recurrence_key}"
        exception = session.get(RecurrenceOccurrenceException, exception_id) or RecurrenceOccurrenceException(
            id=exception_id, series_id=block.recurrence_series_id,
            recurrence_key=block.recurrence_key, disposition="overridden")
        exception.disposition = "overridden"
        exception.start_at, exception.end_at = start_at, end_at
        exception.updated_at = datetime.utcnow()
        session.add(exception)
    session.commit()
    session.refresh(block)
    try_sync_block_to_google(session, block)

    return block_to_read(block, task, category)


class UnscheduleBlockRequest(BaseModel):
    model_config = ConfigDict(alias_generator=alias_generators.to_camel, populate_by_name=True)
    planned_date: date


@router.post("/{block_id}/unschedule")
def unschedule_calendar_block(
    block_id: int,
    body: UnscheduleBlockRequest,
    session: Session = Depends(get_session),
) -> dict:
    """Move one accepted booking to a planned day without deleting its task."""
    block, task, _category = get_block_with_identity(session, block_id)
    if block.variant not in {"scheduled", "protected"}:
        raise HTTPException(status_code=409, detail="Only accepted bookings can be moved to an untimed day.")
    sync_error = delete_google_event_for_block(session, block)
    if sync_error:
        raise HTTPException(status_code=502, detail="Could not remove the linked calendar booking. Please retry.")
    if block.recurrence_series_id and block.recurrence_key:
        exception_id = f"{block.recurrence_series_id}:{block.recurrence_key}"
        exception = session.get(RecurrenceOccurrenceException, exception_id)
        if exception is None:
            exception = RecurrenceOccurrenceException(id=exception_id,
                series_id=block.recurrence_series_id, recurrence_key=block.recurrence_key,
                disposition="cancelled")
        exception.disposition = "cancelled"
        exception.start_at = None
        exception.end_at = None
        exception.updated_at = datetime.utcnow()
        session.add(exception)
    task.planned_date = body.planned_date.isoformat()
    session.add(task)
    session.delete(block)
    session.flush()
    result = {"taskId": task.id, "plannedDate": task.planned_date,
              "scheduledTaskIds": list_scheduled_task_ids(session)}
    session.commit()
    return result


@router.delete("/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_calendar_block(
    block_id: int,
    session: Session = Depends(get_session),
) -> None:
    block, _task, _category = get_block_with_identity(session, block_id)
    delete_google_event_for_block(session, block)
    session.delete(block)
    session.commit()
