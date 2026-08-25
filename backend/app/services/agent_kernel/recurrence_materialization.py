"""Shared deterministic occurrence expansion for simulation and execution."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DEFAULT_RECURRENCE_MATERIALIZATION_HORIZON_DAYS = 90
DEFAULT_RECURRENCE_MATERIALIZATION_OCCURRENCES = 10


def recurrence_occurrence_dates(
    rule: Any,
    *,
    horizon_days: int = DEFAULT_RECURRENCE_MATERIALIZATION_HORIZON_DAYS,
    maximum_occurrences: int = 128,
) -> list[date]:
    """Expand the persisted recurrence horizon without reading mutable state."""

    start_date = rule.start_date
    end_date = rule.end_date
    interval = max(1, int(rule.interval))
    horizon_end = start_date + timedelta(days=horizon_days)
    limit_date = min(end_date, horizon_end) if end_date else horizon_end
    max_count = min(
        int(rule.occurrence_count or 10_000),
        maximum_occurrences,
    )
    weekdays = {
        _WEEKDAYS.index(str(value))
        for value in rule.weekdays
        if str(value) in _WEEKDAYS
    }
    month_days = {int(value) for value in rule.month_days}
    dates: list[date] = []
    cursor = start_date
    while cursor <= limit_date and len(dates) < max_count:
        elapsed = (cursor - start_date).days
        include = False
        if rule.frequency == "daily":
            include = elapsed % interval == 0
        elif rule.frequency == "weekly":
            include = (elapsed // 7) % interval == 0 and cursor.weekday() in weekdays
        elif rule.frequency == "monthly":
            month_offset = (
                (cursor.year - start_date.year) * 12
                + cursor.month
                - start_date.month
            )
            include = month_offset % interval == 0 and cursor.day in month_days
        if include:
            dates.append(cursor)
        cursor += timedelta(days=1)
    return dates
