"""Account-local habit definitions and one completion per habit/local date."""

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlmodel import Field, SQLModel


class Habit(SQLModel, table=True):
    __tablename__ = "habits"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    title: str = Field(max_length=120)
    cue: str = Field(default="", max_length=240)
    icon: str = Field(default="sprout", max_length=32)
    weekdays: str = Field(default="0,1,2,3,4,5,6", max_length=13)
    schedule_history: str = Field(default="[]")
    created_on: date
    archived: bool = Field(default=False)


class HabitCheckIn(SQLModel, table=True):
    __tablename__ = "habit_check_ins"

    habit_id: str = Field(foreign_key="habits.id", primary_key=True)
    day: date = Field(primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
