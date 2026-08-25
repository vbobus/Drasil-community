"""Provider-neutral application boundary for authoritative calendar reads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlmodel import Session, select

from app.db import database as database_module
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceSeries


@dataclass(frozen=True)
class CalendarReadInvocation:
    nodes: list[StrategyNode]
    categories: list[Category]


class CalendarReadApplicationService(Protocol):
    def read(
        self,
        arguments: dict[str, Any],
        *,
        invocation: CalendarReadInvocation,
    ) -> dict[str, Any]: ...


def _parse_local_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return None
    return parsed


def _json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


class SqlCalendarReadApplicationService:
    """Read accepted blocks and recurrence identity from authoritative SQL."""

    def read(
        self,
        arguments: dict[str, Any],
        *,
        invocation: CalendarReadInvocation,
    ) -> dict[str, Any]:
        requested_task_ids = (
            {
                int(item)
                for item in arguments.get("task_ids", [])
                if isinstance(item, int) and not isinstance(item, bool)
            }
            if isinstance(arguments.get("task_ids"), list)
            else set()
        )
        all_for_tasks = (
            arguments.get("include_all_for_tasks") is True
            and bool(requested_task_ids)
        )
        start_at = (
            None
            if all_for_tasks
            else _parse_local_datetime(arguments.get("start_at"))
        )
        end_at = (
            None
            if all_for_tasks
            else _parse_local_datetime(arguments.get("end_at"))
        )
        if not all_for_tasks and (
            start_at is None or end_at is None or end_at <= start_at
        ):
            return {
                "error": (
                    "Use a valid local start_at and end_at range, or provide task_ids "
                    "with include_all_for_tasks=true."
                ),
                "blocks": [],
                "task_ids": [],
                "count": 0,
            }
        raw_limit = arguments.get("limit")
        limit = (
            max(1, min(200, int(raw_limit)))
            if isinstance(raw_limit, int)
            else 100
        )
        # Resolve the engine at call time. Test/account isolation can replace the
        # configured engine after this module is imported; retaining the original
        # object would make authoritative reads silently query a different account.
        with Session(database_module.get_active_engine()) as calendar_session:
            statement = select(CalendarBlock).order_by(
                CalendarBlock.start_at,
                CalendarBlock.id,
            )
            if start_at is not None and end_at is not None:
                statement = statement.where(
                    CalendarBlock.start_at < end_at
                ).where(CalendarBlock.end_at > start_at)
            if requested_task_ids:
                statement = statement.where(
                    CalendarBlock.task_id.in_(requested_task_ids)
                )
            blocks = list(calendar_session.exec(statement).all())[:limit]
            recurrence_series_ids = {
                str(block.recurrence_series_id)
                for block in blocks
                if block.recurrence_series_id
            }
            recurrence_series = (
                list(
                    calendar_session.exec(
                        select(RecurrenceSeries).where(
                            RecurrenceSeries.id.in_(recurrence_series_ids)
                        )
                    ).all()
                )
                if recurrence_series_ids
                else []
            )
        nodes_by_id = {
            int(node.id): node
            for node in invocation.nodes
            if node.id is not None
        }
        categories_by_id = {
            int(category.id): category
            for category in invocation.categories
            if category.id is not None
        }
        recurrence_by_id = {
            str(series.id): series for series in recurrence_series
        }

        payload_blocks: list[dict[str, Any]] = []
        task_ids: list[int] = []
        for block in blocks:
            node = nodes_by_id.get(int(block.task_id))
            category = categories_by_id.get(int(block.category_id))
            series = recurrence_by_id.get(
                str(block.recurrence_series_id or "")
            )
            if int(block.task_id) not in task_ids:
                task_ids.append(int(block.task_id))
            payload_blocks.append(
                {
                    "block_id": int(block.id or 0),
                    "task_id": int(block.task_id),
                    "task_title": (
                        str(node.title)
                        if node is not None
                        else f"Task {block.task_id}"
                    ),
                    "task_emoji": node.emoji if node is not None else None,
                    "category_id": int(block.category_id),
                    "category_name": (
                        str(category.name)
                        if category is not None
                        else "Calendar"
                    ),
                    "start_at": block.start_at.isoformat(),
                    "end_at": block.end_at.isoformat(),
                    "revision": int(block.revision),
                    "is_pinned": bool(block.is_pinned),
                    "is_protected": bool(block.is_protected),
                    "is_recurring": bool(block.is_recurring),
                    "recurrence_series_id": block.recurrence_series_id,
                    "recurrence_key": block.recurrence_key,
                    "recurrence_series_ref": (
                        f"recurrence_series:{block.recurrence_series_id}"
                        if block.recurrence_series_id
                        else None
                    ),
                    "recurrence_occurrence_ref": (
                        "recurrence_occurrence:"
                        f"{block.recurrence_series_id}:{block.recurrence_key}"
                        if block.recurrence_series_id and block.recurrence_key
                        else None
                    ),
                    "recurrence_series_revision": (
                        int(series.revision) if series is not None else None
                    ),
                    "recurrence_rule": (
                        {
                            "frequency": series.frequency,
                            "interval": int(series.interval),
                            "days_of_week": _json_list(
                                series.days_of_week_json
                            ),
                            "month_days": _json_list(
                                series.month_days_json
                            ),
                            "start_date": series.start_date.isoformat(),
                            "end_date": (
                                series.end_date.isoformat()
                                if series.end_date
                                else None
                            ),
                            "occurrence_count": series.occurrence_limit,
                            "preferred_start_time": (
                                series.preferred_start_time
                            ),
                            "allowed_start_time": series.allowed_start_time,
                            "allowed_end_time": series.allowed_end_time,
                            "duration_minutes": int(series.duration_minutes),
                            "timezone": series.timezone,
                            "strict": bool(series.strict),
                            "gap_minutes": series.gap_minutes,
                            "conflict_relocation": (
                                series.conflict_relocation
                            ),
                            "allow_priority_displacement": bool(
                                series.allow_priority_displacement
                            ),
                        }
                        if series is not None
                        else None
                    ),
                }
            )
        return {
            "read_only": True,
            "range_start": (
                start_at.isoformat() if start_at is not None else None
            ),
            "range_end": end_at.isoformat() if end_at is not None else None,
            "scope": "task_schedule" if all_for_tasks else "calendar_range",
            "blocks": payload_blocks,
            "task_ids": task_ids,
            "recurrence_series_refs": sorted(
                {
                    str(block["recurrence_series_ref"])
                    for block in payload_blocks
                    if block.get("recurrence_series_ref")
                }
            ),
            "recurrence_occurrence_refs": sorted(
                {
                    str(block["recurrence_occurrence_ref"])
                    for block in payload_blocks
                    if block.get("recurrence_occurrence_ref")
                }
            ),
            "count": len(payload_blocks),
        }


SQL_CALENDAR_READ_SERVICE: CalendarReadApplicationService = (
    SqlCalendarReadApplicationService()
)
