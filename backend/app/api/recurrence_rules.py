"""Direct task recurrence-rule API over the shared application service."""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, alias_generators, field_validator, model_validator
from sqlmodel import Session

from app.db.database import get_session
from app.services.recurrence_rule_service import (
    RecurrenceRuleConflictError,
    RecurrenceRuleInput,
    RecurrenceRuleNotFoundError,
    RecurrenceRuleServiceError,
    RecurrenceRuleView,
    SQL_RECURRENCE_RULE_APPLICATION_SERVICE,
    WEEKDAYS,
)


router = APIRouter(prefix="/api/v1/recurrence-rules", tags=["Recurrence Rules"])


class RecurrenceRuleWrite(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    frequency: Literal["daily", "weekly"]
    interval: int = Field(default=1, ge=1, le=52)
    days_of_week: list[str] = Field(default_factory=list)
    start_date: date
    end_date: date | None = None
    preferred_start_time: time
    duration_minutes: int = Field(ge=5, le=1440)
    timezone: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("days_of_week")
    @classmethod
    def validate_days(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        if any(item not in WEEKDAYS for item in normalized):
            raise ValueError("daysOfWeek contains an unsupported weekday")
        return normalized

    @model_validator(mode="after")
    def validate_cadence(self) -> "RecurrenceRuleWrite":
        if self.frequency == "weekly" and not self.days_of_week:
            raise ValueError("weekly recurrence requires at least one weekday")
        if self.frequency == "daily" and self.days_of_week:
            raise ValueError("daily recurrence does not accept weekdays")
        return self


class RecurrenceRuleRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    id: str
    revision: int
    task_id: int
    category_id: int
    frequency: str
    interval: int
    days_of_week: list[str]
    start_date: date
    end_date: date | None
    preferred_start_time: str
    duration_minutes: int
    timezone: str | None
    status: str


def _read(value: RecurrenceRuleView) -> RecurrenceRuleRead:
    return RecurrenceRuleRead.model_validate({
        **value.__dict__,
        "days_of_week": list(value.days_of_week),
    })


def _raise_service_error(error: RecurrenceRuleServiceError) -> None:
    if isinstance(error, RecurrenceRuleNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, RecurrenceRuleConflictError):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    raise HTTPException(
        status_code=code,
        detail={"error": {"type": error.code, "detail": str(error)}},
    ) from error


@router.get("", response_model=RecurrenceRuleRead | None)
def get_recurrence_rule(
    task_id: int = Query(alias="taskId"),
    session: Session = Depends(get_session),
) -> RecurrenceRuleRead | None:
    value = SQL_RECURRENCE_RULE_APPLICATION_SERVICE.get_active(
        session,
        task_id=task_id,
    )
    return _read(value) if value is not None else None


@router.put("/{task_id}", response_model=RecurrenceRuleRead)
def upsert_recurrence_rule(
    task_id: int,
    body: RecurrenceRuleWrite,
    session: Session = Depends(get_session),
) -> RecurrenceRuleRead:
    try:
        value = SQL_RECURRENCE_RULE_APPLICATION_SERVICE.upsert(
            session,
            task_id=task_id,
            rule=RecurrenceRuleInput(
                frequency=body.frequency,
                interval=body.interval,
                days_of_week=tuple(body.days_of_week),
                start_date=body.start_date,
                end_date=body.end_date,
                preferred_start_time=body.preferred_start_time,
                duration_minutes=body.duration_minutes,
                timezone=body.timezone,
            ),
            expected_revision=body.expected_revision,
        )
    except RecurrenceRuleServiceError as error:
        _raise_service_error(error)
    return _read(value)


@router.delete("/{task_id}", response_model=RecurrenceRuleRead)
def deactivate_recurrence_rule(
    task_id: int,
    expected_revision: int = Query(alias="expectedRevision", ge=1),
    session: Session = Depends(get_session),
) -> RecurrenceRuleRead:
    try:
        value = SQL_RECURRENCE_RULE_APPLICATION_SERVICE.deactivate(
            session,
            task_id=task_id,
            expected_revision=expected_revision,
        )
    except RecurrenceRuleServiceError as error:
        _raise_service_error(error)
    return _read(value)
