"""Persistent recurrence rules used to materialize rolling calendar horizons."""

from datetime import date, datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class RecurrenceSeries(SQLModel, table=True):
    __tablename__ = "recurrence_series"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    revision: int = Field(default=1, ge=1, index=True)
    task_id: int = Field(foreign_key="strategy_nodes.id", index=True)
    category_id: int = Field(foreign_key="categories.id", index=True)
    frequency: str = Field(max_length=24, index=True)
    interval: int = Field(default=1)
    days_of_week_json: str = Field(default="[]")
    month_days_json: str = Field(default="[]")
    start_date: date = Field(index=True)
    end_date: Optional[date] = Field(default=None, index=True)
    occurrence_limit: Optional[int] = Field(default=None)
    preferred_start_time: str = Field(max_length=16)
    allowed_start_time: str = Field(max_length=16)
    allowed_end_time: str = Field(max_length=16)
    duration_minutes: int
    timezone: Optional[str] = Field(default=None, max_length=128)
    strict: bool = Field(default=False, index=True)
    preference_override_authorized: bool = Field(default=False, index=True)
    gap_minutes: Optional[int] = Field(default=None)
    conflict_relocation: str = Field(default="after_incoming", max_length=32)
    allow_priority_displacement: bool = Field(default=False, index=True)
    status: str = Field(default="active", index=True, max_length=24)
    materialized_through: Optional[date] = Field(default=None, index=True)
    source_change_set_id: Optional[str] = Field(
        default=None,
        foreign_key="strategist_change_sets.id",
        index=True,
        max_length=64,
    )
    source_agent_change_set_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_change_sets.change_set_id",
        index=True,
        max_length=128,
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class RecurrenceOccurrenceException(SQLModel, table=True):
    """Durable cancellation/override keyed by exact series occurrence identity."""

    __tablename__ = "recurrence_occurrence_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "recurrence_key",
            name="uq_recurrence_occurrence_exception_identity",
        ),
    )

    id: str = Field(primary_key=True, max_length=160)
    revision: int = Field(default=1, ge=1, index=True)
    series_id: str = Field(index=True, max_length=64)
    recurrence_key: str = Field(index=True, max_length=64)
    disposition: str = Field(index=True, max_length=24)
    start_at: Optional[datetime] = Field(default=None)
    end_at: Optional[datetime] = Field(default=None)
    source_change_set_id: Optional[str] = Field(default=None, index=True, max_length=128)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


from app.models.revisioning import register_revisioned_model  # noqa: E402

register_revisioned_model(RecurrenceSeries)
register_revisioned_model(RecurrenceOccurrenceException)
