"""Canonical parsing and persistence for task deadline dates."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def parse_deadline_date(value: Any) -> date | None:
    """Return the calendar date represented by a supported deadline value.

    Task deadlines are date-only in the persisted/UI domain. Native agent
    contracts use ``datetime`` values, and older rows may therefore contain a
    full ISO datetime (including an offset). Treat the lexical calendar date as
    authoritative without shifting it through another timezone.
    """

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None

    normalized = value.strip()
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        pass

    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def serialize_deadline_date(value: Any) -> str | None:
    """Serialize a supported deadline value to the date-only storage contract."""

    parsed = parse_deadline_date(value)
    return parsed.isoformat() if parsed is not None else None
