"""Keep a task's planned day aligned with the booking that carries it."""

from datetime import date, datetime


def moved_planned_day(
    planned_date: date | str | None,
    source_start: datetime,
    target_start: datetime,
) -> date | None:
    """Return the new day only when the assignment belongs to the source booking."""
    source_day, target_day = source_start.date(), target_start.date()
    assigned_day = planned_date.isoformat() if isinstance(planned_date, date) else planned_date
    if source_day != target_day and assigned_day == source_day.isoformat():
        return target_day
    return None
