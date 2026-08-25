"""Transport-neutral application service for task recurrence rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, select

from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceSeries


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class RecurrenceRuleServiceError(ValueError):
    code = "invalid_recurrence_rule"


class RecurrenceRuleNotFoundError(RecurrenceRuleServiceError):
    code = "recurrence_rule_not_found"


class RecurrenceRuleConflictError(RecurrenceRuleServiceError):
    code = "recurrence_rule_conflict"


@dataclass(frozen=True)
class RecurrenceRuleInput:
    frequency: Literal["daily", "weekly"]
    start_date: date
    preferred_start_time: time
    duration_minutes: int
    timezone: str
    days_of_week: tuple[str, ...] = ()
    end_date: date | None = None
    interval: int = 1

    def validated(self) -> "RecurrenceRuleInput":
        if self.frequency not in {"daily", "weekly"}:
            raise RecurrenceRuleServiceError("Choose a daily or weekly recurrence.")
        if self.interval < 1 or self.interval > 52:
            raise RecurrenceRuleServiceError("The recurrence interval must be between 1 and 52.")
        if self.duration_minutes < 5 or self.duration_minutes > 1440:
            raise RecurrenceRuleServiceError("Duration must be between 5 minutes and 24 hours.")
        if self.end_date is not None and self.end_date < self.start_date:
            raise RecurrenceRuleServiceError("The end date cannot be before the start date.")
        normalized_days = tuple(
            day.strip().lower()
            for day in self.days_of_week
            if day.strip().lower() in WEEKDAYS
        )
        if len(normalized_days) != len(set(normalized_days)):
            raise RecurrenceRuleServiceError("Weekly recurrence days cannot be duplicated.")
        if self.frequency == "weekly" and not normalized_days:
            raise RecurrenceRuleServiceError("Choose at least one weekday.")
        if self.frequency == "daily" and normalized_days:
            raise RecurrenceRuleServiceError("Daily recurrence does not accept weekday filters.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise RecurrenceRuleServiceError("Choose a recognized timezone.") from error
        start_minutes = self.preferred_start_time.hour * 60 + self.preferred_start_time.minute
        if start_minutes + self.duration_minutes > 24 * 60:
            raise RecurrenceRuleServiceError(
                "This recurrence would cross midnight. Choose an earlier time or shorter duration."
            )
        return RecurrenceRuleInput(
            frequency=self.frequency,
            start_date=self.start_date,
            preferred_start_time=self.preferred_start_time.replace(second=0, microsecond=0),
            duration_minutes=self.duration_minutes,
            timezone=self.timezone,
            days_of_week=normalized_days,
            end_date=self.end_date,
            interval=self.interval,
        )


@dataclass(frozen=True)
class RecurrenceRuleView:
    id: str
    revision: int
    task_id: int
    category_id: int
    frequency: str
    interval: int
    days_of_week: tuple[str, ...]
    start_date: date
    end_date: date | None
    preferred_start_time: str
    duration_minutes: int
    timezone: str | None
    status: str


def _days(row: RecurrenceSeries) -> tuple[str, ...]:
    try:
        raw = json.loads(row.days_of_week_json or "[]")
    except (TypeError, ValueError):
        raw = []
    if not isinstance(raw, list):
        return ()
    return tuple(
        value.strip().lower()
        for value in raw
        if isinstance(value, str) and value.strip().lower() in WEEKDAYS
    )


def recurrence_rule_view(row: RecurrenceSeries) -> RecurrenceRuleView:
    return RecurrenceRuleView(
        id=row.id,
        revision=row.revision,
        task_id=row.task_id,
        category_id=row.category_id,
        frequency=row.frequency,
        interval=row.interval,
        days_of_week=_days(row),
        start_date=row.start_date,
        end_date=row.end_date,
        preferred_start_time=row.preferred_start_time,
        duration_minutes=row.duration_minutes,
        timezone=row.timezone,
        status=row.status,
    )


class SqlRecurrenceRuleApplicationService:
    """Own recurrence-definition writes without depending on HTTP or agent transports."""

    def get_active(self, session: Session, *, task_id: int) -> RecurrenceRuleView | None:
        rows = list(
            session.exec(
                select(RecurrenceSeries)
                .where(RecurrenceSeries.task_id == task_id)
                .where(RecurrenceSeries.status == "active")
                .order_by(RecurrenceSeries.updated_at.desc(), RecurrenceSeries.created_at.desc())
            ).all()
        )
        return recurrence_rule_view(rows[0]) if rows else None

    def get_active_for_tasks(
        self,
        session: Session,
        *,
        task_ids: list[int],
    ) -> dict[int, RecurrenceRuleView]:
        if not task_ids:
            return {}
        rows = list(
            session.exec(
                select(RecurrenceSeries)
                .where(RecurrenceSeries.task_id.in_(task_ids))
                .where(RecurrenceSeries.status == "active")
                .order_by(RecurrenceSeries.updated_at.desc(), RecurrenceSeries.created_at.desc())
            ).all()
        )
        result: dict[int, RecurrenceRuleView] = {}
        for row in rows:
            result.setdefault(row.task_id, recurrence_rule_view(row))
        return result

    def upsert(
        self,
        session: Session,
        *,
        task_id: int,
        rule: RecurrenceRuleInput,
        expected_revision: int | None,
    ) -> RecurrenceRuleView:
        task = session.get(StrategyNode, task_id)
        if task is None:
            raise RecurrenceRuleNotFoundError(f"Task {task_id} was not found.")
        value = rule.validated()
        active_rows = list(
            session.exec(
                select(RecurrenceSeries)
                .where(RecurrenceSeries.task_id == task_id)
                .where(RecurrenceSeries.status == "active")
                .order_by(RecurrenceSeries.updated_at.desc(), RecurrenceSeries.created_at.desc())
            ).all()
        )
        current = active_rows[0] if active_rows else None
        now = datetime.utcnow()
        if current is None:
            if expected_revision is not None:
                raise RecurrenceRuleConflictError(
                    "The recurrence rule changed. Refresh the task and try again."
                )
            end_clock = (
                datetime.combine(date.today(), value.preferred_start_time)
                + timedelta(minutes=value.duration_minutes)
            ).time()
            current = RecurrenceSeries(
                task_id=task_id,
                category_id=task.category_id,
                frequency=value.frequency,
                interval=value.interval,
                days_of_week_json=json.dumps(list(value.days_of_week)),
                month_days_json="[]",
                start_date=value.start_date,
                end_date=value.end_date,
                occurrence_limit=None,
                preferred_start_time=value.preferred_start_time.isoformat(timespec="minutes"),
                allowed_start_time=value.preferred_start_time.isoformat(timespec="minutes"),
                allowed_end_time=end_clock.isoformat(timespec="minutes"),
                duration_minutes=value.duration_minutes,
                timezone=value.timezone,
                strict=False,
                materialized_through=None,
                status="active",
                updated_at=now,
            )
        else:
            if expected_revision is None or expected_revision != current.revision:
                raise RecurrenceRuleConflictError(
                    "The recurrence rule changed. Refresh the task and try again."
                )
            end_clock = (
                datetime.combine(date.today(), value.preferred_start_time)
                + timedelta(minutes=value.duration_minutes)
            ).time()
            current.revision += 1
            current.category_id = task.category_id
            current.frequency = value.frequency
            current.interval = value.interval
            current.days_of_week_json = json.dumps(list(value.days_of_week))
            current.start_date = value.start_date
            current.end_date = value.end_date
            current.preferred_start_time = value.preferred_start_time.isoformat(timespec="minutes")
            current.allowed_start_time = current.preferred_start_time
            current.allowed_end_time = end_clock.isoformat(timespec="minutes")
            current.duration_minutes = value.duration_minutes
            current.timezone = value.timezone
            current.updated_at = now
            for duplicate in active_rows[1:]:
                duplicate.status = "inactive"
                duplicate.revision += 1
                duplicate.updated_at = now
                session.add(duplicate)
        session.add(current)
        session.commit()
        session.refresh(current)
        return recurrence_rule_view(current)

    def deactivate(
        self,
        session: Session,
        *,
        task_id: int,
        expected_revision: int,
    ) -> RecurrenceRuleView:
        rows = list(
            session.exec(
                select(RecurrenceSeries)
                .where(RecurrenceSeries.task_id == task_id)
                .where(RecurrenceSeries.status == "active")
                .order_by(RecurrenceSeries.updated_at.desc(), RecurrenceSeries.created_at.desc())
            ).all()
        )
        if not rows:
            raise RecurrenceRuleNotFoundError("This task does not have an active recurrence rule.")
        current = rows[0]
        if current.revision != expected_revision:
            raise RecurrenceRuleConflictError(
                "The recurrence rule changed. Refresh the task and try again."
            )
        now = datetime.utcnow()
        for row in rows:
            row.status = "inactive"
            row.revision += 1
            row.updated_at = now
            session.add(row)
        session.commit()
        session.refresh(current)
        return recurrence_rule_view(current)


SQL_RECURRENCE_RULE_APPLICATION_SERVICE = SqlRecurrenceRuleApplicationService()
