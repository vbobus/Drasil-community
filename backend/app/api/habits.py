"""Tracking uses the authenticated, account-scoped session shared by task APIs."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db.database import get_session
from app.models.habit import Habit, HabitCheckIn
from app.services.habits import HABIT_ICONS, local_today, scheduled, set_schedule, snapshot

router = APIRouter(prefix="/api/v1/habits", tags=["Habits"])


class HabitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    cue: str = Field(default="", max_length=240)
    icon: str = "sprout"
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)

    @field_validator("title", "cue")
    @classmethod
    def trim_text(cls, value: str, info):
        value = value.strip()
        if info.field_name == "title" and not value:
            raise ValueError("Name your habit.")
        return value

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]):
        if any(day < 0 or day > 6 for day in value) or len(set(value)) != len(value):
            raise ValueError("Choose unique weekdays from Monday (0) to Sunday (6).")
        return sorted(value)

    @field_validator("icon")
    @classmethod
    def validate_icon(cls, value: str):
        if value not in HABIT_ICONS:
            raise ValueError("Choose one of the habit icons.")
        return value


class CheckInInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: bool


class ArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


def require_habit(session: Session, habit_id: str) -> Habit:
    habit = session.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(404, "Habit not found.")
    return habit


@router.get("")
def read_habits(time_zone: str = Query(default="UTC", alias="timeZone"), session: Session = Depends(get_session)):
    return snapshot(session, local_today(time_zone))


@router.post("", status_code=201)
def create_habit(body: HabitInput, time_zone: str = Query(default="UTC", alias="timeZone"), session: Session = Depends(get_session)):
    today = local_today(time_zone)
    habit = Habit(title=body.title, cue=body.cue, icon=body.icon, created_on=today)
    set_schedule(habit, body.weekdays, today)
    session.add(habit)
    session.commit()
    return snapshot(session, today)


@router.put("/{habit_id}")
def update_habit(habit_id: str, body: HabitInput, time_zone: str = Query(default="UTC", alias="timeZone"), session: Session = Depends(get_session)):
    today = local_today(time_zone)
    habit = require_habit(session, habit_id)
    habit.title, habit.cue, habit.icon = body.title, body.cue, body.icon
    if ",".join(map(str, body.weekdays)) != habit.weekdays:
        set_schedule(habit, body.weekdays, today)
    session.add(habit)
    session.commit()
    return snapshot(session, today)


@router.patch("/{habit_id}/archive")
def archive_habit(habit_id: str, body: ArchiveInput, time_zone: str = Query(default="UTC", alias="timeZone"), session: Session = Depends(get_session)):
    today = local_today(time_zone)
    habit = require_habit(session, habit_id)
    habit.archived = body.archived
    session.add(habit)
    session.commit()
    return snapshot(session, today)


@router.put("/{habit_id}/check-ins/{day}")
def check_in(habit_id: str, day: date, body: CheckInInput, time_zone: str = Query(default="UTC", alias="timeZone"), session: Session = Depends(get_session)):
    today = local_today(time_zone)
    habit = require_habit(session, habit_id)
    if habit.archived:
        raise HTTPException(409, "Restore this habit before changing check-ins.")
    existing = session.get(HabitCheckIn, (habit_id, day))
    if day > today or day < habit.created_on or (body.completed and not scheduled(habit, day)):
        raise HTTPException(422, "Check in on a scheduled day between this habit's start and today.")
    if body.completed and existing is None:
        session.add(HabitCheckIn(habit_id=habit_id, day=day))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if session.get(HabitCheckIn, (habit_id, day)) is None:
                raise
    elif not body.completed and existing is not None:
        session.delete(existing)
        session.commit()
    return snapshot(session, today)
