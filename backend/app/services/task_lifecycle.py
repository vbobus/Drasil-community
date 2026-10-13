"""Task identity allocation and dependent-record lifecycle cleanup."""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import func, update
from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode, StrategyNodeIdSequence
from app.models.preference_memory import PreferenceLink, SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceOccurrenceException, RecurrenceSeries
from app.models.strategist_session_entities import StrategistSessionEntity
from app.models.strategist_turn_mentions import StrategistTurnMention


def _task_reference_id(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("task:"):
        text = text.removeprefix("task:")
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _max_task_identity(session: Session) -> int:
    candidates = [
        session.exec(select(func.max(StrategyNode.id))).one(),
        session.exec(select(func.max(CalendarBlock.task_id))).one(),
        session.exec(select(func.max(RecurrenceSeries.task_id))).one(),
        session.exec(select(func.max(StrategistSessionEntity.node_id))).one(),
        session.exec(select(func.max(StrategistTurnMention.node_id))).one(),
    ]
    referenced_ids = [
        *(
            _task_reference_id(item)
            for item in session.exec(
                select(UserPreference.scope_id).where(UserPreference.scope_type == "task")
            ).all()
        ),
        *(
            _task_reference_id(item)
            for item in session.exec(
                select(SchedulingConstraint.target_id).where(
                    SchedulingConstraint.target_type == "task"
                )
            ).all()
        ),
        *(
            _task_reference_id(item)
            for item in session.exec(
                select(PreferenceLink.target_id).where(PreferenceLink.target_type == "task")
            ).all()
        ),
    ]
    return max(
        [int(item) for item in candidates if item is not None]
        + [item for item in referenced_ids if item is not None],
        default=0,
    )


def bootstrap_strategy_node_id_sequence(session: Session) -> StrategyNodeIdSequence:
    sequence = session.get(StrategyNodeIdSequence, 1)
    maximum = _max_task_identity(session)
    if sequence is None:
        sequence = StrategyNodeIdSequence(id=1, last_id=maximum)
    elif sequence.last_id < maximum:
        sequence.last_id = maximum
    session.add(sequence)
    session.flush()
    return sequence


def allocate_strategy_node_id(session: Session) -> int:
    """Reserve a never-reused task ID in the caller's transaction."""
    if session.get_bind().dialect.name == "postgresql":
        return int(session.exec(
            update(StrategyNodeIdSequence)
            .where(StrategyNodeIdSequence.id == 1)
            .values(last_id=StrategyNodeIdSequence.last_id + 1)
            .returning(StrategyNodeIdSequence.last_id)
        ).one()[0])
    sequence = bootstrap_strategy_node_id_sequence(session)
    sequence.last_id += 1
    session.add(sequence)
    session.flush()
    return int(sequence.last_id)


def delete_task_dependents(session: Session, task_ids: Iterable[int]) -> dict[str, int]:
    """Remove active records whose identity would become unsafe after hard delete."""

    ids = sorted({int(task_id) for task_id in task_ids})
    if not ids:
        return {
            "calendar_blocks": 0,
            "recurrence_series": 0,
            "recurrence_exceptions": 0,
            "preferences": 0,
            "constraints": 0,
            "links": 0,
            "session_entities": 0,
            "mentions": 0,
        }

    blocks = list(
        session.exec(select(CalendarBlock).where(CalendarBlock.task_id.in_(ids))).all()
    )
    for block in blocks:
        session.delete(block)

    series_rows = list(
        session.exec(select(RecurrenceSeries).where(RecurrenceSeries.task_id.in_(ids))).all()
    )
    series_ids = {series.id for series in series_rows}
    exceptions = (
        list(
            session.exec(
                select(RecurrenceOccurrenceException).where(
                    RecurrenceOccurrenceException.series_id.in_(series_ids)
                )
            ).all()
        )
        if series_ids
        else []
    )
    for exception in exceptions:
        session.delete(exception)
    for series in series_rows:
        session.delete(series)

    id_strings = {str(task_id) for task_id in ids}
    preferences = list(
        session.exec(
            select(UserPreference)
            .where(UserPreference.scope_type == "task")
            .where(UserPreference.scope_id.in_(id_strings))
            .where(UserPreference.active == True)  # noqa: E712
        ).all()
    )
    for preference in preferences:
        preference.active = False
        preference.revision += 1
        session.add(preference)
    preference_ids = {preference.id for preference in preferences}

    constraints = list(
        session.exec(
            select(SchedulingConstraint)
            .where(
                (
                    (SchedulingConstraint.target_type == "task")
                    & SchedulingConstraint.target_id.in_(id_strings)
                )
                | SchedulingConstraint.source_preference_id.in_(preference_ids)
            )
            .where(SchedulingConstraint.active == True)  # noqa: E712
        ).all()
    )
    for constraint in constraints:
        constraint.active = False
        constraint.revision += 1
        session.add(constraint)

    links = list(
        session.exec(
            select(PreferenceLink)
            .where(
                (
                    (PreferenceLink.target_type == "task")
                    & PreferenceLink.target_id.in_(id_strings)
                )
                | PreferenceLink.preference_id.in_(preference_ids)
            )
            .where(PreferenceLink.active == True)  # noqa: E712
        ).all()
    )
    for link in links:
        link.active = False
        link.revision += 1
        session.add(link)

    session_entities = list(
        session.exec(
            select(StrategistSessionEntity).where(StrategistSessionEntity.node_id.in_(ids))
        ).all()
    )
    for entity in session_entities:
        session.delete(entity)

    mentions = list(
        session.exec(
            select(StrategistTurnMention).where(StrategistTurnMention.node_id.in_(ids))
        ).all()
    )
    for mention in mentions:
        mention.node_id = None
        session.add(mention)

    return {
        "calendar_blocks": len(blocks),
        "recurrence_series": len(series_rows),
        "recurrence_exceptions": len(exceptions),
        "preferences": len(preferences),
        "constraints": len(constraints),
        "links": len(links),
        "session_entities": len(session_entities),
        "mentions": len(mentions),
    }


def reconcile_reused_task_identities(session: Session) -> dict[str, int]:
    """Remove legacy dependents that predate the task currently owning their ID."""

    stale_preferences: list[UserPreference] = []
    for preference in session.exec(
        select(UserPreference).where(
            UserPreference.scope_type == "task",
            UserPreference.active == True,  # noqa: E712
        )
    ).all():
        task_id = _task_reference_id(preference.scope_id)
        task = session.get(StrategyNode, task_id) if task_id is not None else None
        if task is None or task.created_at > preference.created_at:
            stale_preferences.append(preference)
    stale_preference_ids = {preference.id for preference in stale_preferences}

    stale_constraints: list[SchedulingConstraint] = []
    for constraint in session.exec(
        select(SchedulingConstraint).where(SchedulingConstraint.active == True)  # noqa: E712
    ).all():
        task_id = (
            _task_reference_id(constraint.target_id)
            if constraint.target_type == "task"
            else None
        )
        task = session.get(StrategyNode, task_id) if task_id is not None else None
        if (
            constraint.source_preference_id in stale_preference_ids
            or (constraint.target_type == "task" and task is None)
            or (
                task is not None
                and task.created_at > constraint.created_at
            )
        ):
            stale_constraints.append(constraint)

    stale_links: list[PreferenceLink] = []
    for link in session.exec(
        select(PreferenceLink).where(PreferenceLink.active == True)  # noqa: E712
    ).all():
        task_id = (
            _task_reference_id(link.target_id) if link.target_type == "task" else None
        )
        task = session.get(StrategyNode, task_id) if task_id is not None else None
        if (
            link.preference_id in stale_preference_ids
            or (link.target_type == "task" and task is None)
            or (task is not None and task.created_at > link.created_at)
        ):
            stale_links.append(link)

    for row in [*stale_preferences, *stale_constraints, *stale_links]:
        row.active = False
        row.revision += 1
        session.add(row)

    stale_series: list[RecurrenceSeries] = []
    for series in session.exec(select(RecurrenceSeries)).all():
        task = session.get(StrategyNode, series.task_id)
        if (
            task is None
            or task.created_at > series.created_at
        ):
            stale_series.append(series)

    stale_series_ids = {series.id for series in stale_series}
    existing_series_ids = {
        series.id for series in session.exec(select(RecurrenceSeries)).all()
    }
    stale_blocks: list[CalendarBlock] = []
    for block in session.exec(select(CalendarBlock)).all():
        task = session.get(StrategyNode, block.task_id)
        if (
            block.recurrence_series_id in stale_series_ids
            or (
                block.recurrence_series_id is not None
                and block.recurrence_series_id not in existing_series_ids
            )
            or task is None
            or task.created_at > block.created_at
        ):
            stale_blocks.append(block)

    stale_exceptions = (
        list(
            session.exec(
                select(RecurrenceOccurrenceException).where(
                    RecurrenceOccurrenceException.series_id.in_(stale_series_ids)
                )
            ).all()
        )
        if stale_series_ids
        else []
    )
    for block in stale_blocks:
        session.delete(block)
    for exception in stale_exceptions:
        session.delete(exception)
    for series in stale_series:
        session.delete(series)
    session.flush()
    return {
        "calendar_blocks": len(stale_blocks),
        "recurrence_series": len(stale_series),
        "recurrence_exceptions": len(stale_exceptions),
        "preferences": len(stale_preferences),
        "constraints": len(stale_constraints),
        "links": len(stale_links),
    }
