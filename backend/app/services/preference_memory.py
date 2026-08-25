"""Scoped retrieval for persistent user preferences and scheduling constraints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.models.preference_memory import (
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)
from app.services.scheduling_solver import OccupiedInterval
from app.services.preference_values import decode_preference_value

DEFAULT_USER_ID = "local"
CONTEXT_VERSION = "preference-memory-v1"
SCHEDULING_DOMAINS = {
    "availability",
    "calendar",
    "deep_work",
    "energy",
    "focus",
    "planning",
    "schedule",
    "scheduling",
    "sleep",
    "time",
    "work",
}
DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "availability": {"available", "availability", "busy", "free"},
    "calendar": {"calendar", "meeting", "event", "appointment"},
    "deep_work": {"deep work", "focus", "focused", "writing", "coding"},
    "energy": {"energy", "tired", "fresh", "morning", "afternoon", "evening"},
    "focus": {"focus", "focused", "concentrate", "distraction"},
    "planning": {"plan", "planning", "schedule", "prioritize"},
    "schedule": {"schedule", "calendar", "time", "slot"},
    "scheduling": {"schedule", "calendar", "time", "slot"},
    "sleep": {"sleep", "bed", "wake", "night", "morning"},
    "time": {"time", "hour", "morning", "afternoon", "evening"},
    "work": {"work", "office", "job", "meeting", "project"},
}
HARD_INTERVAL_TYPES = {"fixed_block", "forbidden_window", "unavailable_window"}
INTER_TASK_GAP_CONSTRAINT_TYPES = {
    "inter_task_gap",
    "minimum_inter_task_gap",
    "task_buffer",
}
INTER_TASK_GAP_VALUE_KEYS = (
    "interTaskGapMinutes",
    "inter_task_gap_minutes",
    "preferredInterTaskGapMinutes",
    "preferred_inter_task_gap_minutes",
    "gapMinutes",
    "gap_minutes",
    "bufferMinutes",
    "buffer_minutes",
)


@dataclass(frozen=True)
class PreferenceRetrievalScope:
    task_ids: list[int]
    category_ids: list[int]
    task_titles: list[str]
    category_names: list[str]
    window_start: datetime
    window_end: datetime
    prompt: str = ""
    user_id: str = DEFAULT_USER_ID


def _json_loads(value: str | None) -> dict[str, Any]:
    return decode_preference_value(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _parse_time_minutes(value: Any) -> int | None:
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if ":" not in text:
        return None
    raw_hour, raw_minute = text.split(":", 1)
    try:
        hour = int(raw_hour)
        minute = int(raw_minute)
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _datetime_from_time_minutes(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time(hour=minutes // 60, minute=minutes % 60))


def _day_allowed(day: date, raw_days: Any) -> bool:
    if not isinstance(raw_days, list) or not raw_days:
        return True
    allowed: set[int] = set()
    names = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    for item in raw_days:
        if isinstance(item, int) and 0 <= item <= 6:
            allowed.add(item)
        elif isinstance(item, str):
            text = item.strip().lower()
            if text.isdigit() and 0 <= int(text) <= 6:
                allowed.add(int(text))
            elif text in names:
                allowed.add(names[text])
    return day.weekday() in allowed


def _scope_id_matches(scope_id: str | None, ids: set[str]) -> bool:
    return scope_id is not None and scope_id in ids


def _scope_matches(
    *,
    scope_type: str,
    scope_id: str | None,
    task_ids: set[str],
    category_ids: set[str],
) -> bool:
    if scope_type == "global":
        return True
    if scope_type == "task":
        return _scope_id_matches(scope_id, task_ids)
    if scope_type == "category":
        return _scope_id_matches(scope_id, category_ids)
    return False


def _effective_in_window(
    *,
    effective_from: datetime | None,
    effective_until: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    starts = (
        effective_from.replace(tzinfo=None)
        if effective_from and effective_from.tzinfo
        else effective_from
    )
    ends = (
        effective_until.replace(tzinfo=None)
        if effective_until and effective_until.tzinfo
        else effective_until
    )
    if ends is not None and ends <= window_start:
        return False
    if starts is not None and starts >= window_end:
        return False
    return True


def _context_text(scope: PreferenceRetrievalScope) -> str:
    return " ".join(
        [
            scope.prompt,
            *scope.task_titles,
            *scope.category_names,
        ]
    ).lower()


def _domain_relevant(domain: str, text: str) -> bool:
    normalized = domain.strip().lower()
    if normalized in SCHEDULING_DOMAINS:
        return True
    keywords = DOMAIN_KEYWORDS.get(normalized, set())
    return any(keyword in text for keyword in keywords)


def _preference_text(preference: UserPreference) -> str:
    return " ".join(
        [
            preference.domain,
            preference.preference_type,
            preference.description or "",
            preference.value_json or "",
        ]
    ).lower()


def _preference_score(preference: UserPreference, scope: PreferenceRetrievalScope) -> int:
    task_ids = {str(task_id) for task_id in scope.task_ids}
    category_ids = {str(category_id) for category_id in scope.category_ids}
    text = _context_text(scope)
    score = 0
    if preference.scope_type == "task" and preference.scope_id in task_ids:
        score += 100
    elif preference.scope_type == "category" and preference.scope_id in category_ids:
        score += 80
    elif preference.scope_type == "global":
        score += 20

    domain = preference.domain.strip().lower()
    if _domain_relevant(domain, text):
        score += 25
    record_text = _preference_text(preference)
    for token in text.split():
        if len(token) >= 4 and token in record_text:
            score += 3
    if preference.strength == "strong":
        score += 10
    elif preference.strength == "medium":
        score += 5
    return score


def _preference_payload(preference: UserPreference) -> dict[str, Any]:
    return {
        "id": preference.id,
        "domain": preference.domain,
        "preferenceType": preference.preference_type,
        "scope": {
            "type": preference.scope_type,
            "id": preference.scope_id,
        },
        "strength": preference.strength,
        "source": preference.source,
        "confidence": preference.confidence,
        "description": preference.description,
        "value": _json_loads(preference.value_json),
        "effectiveFrom": _iso(preference.effective_from),
        "effectiveUntil": _iso(preference.effective_until),
        "updatedAt": _iso(preference.updated_at),
    }


def _constraint_payload(constraint: SchedulingConstraint) -> dict[str, Any]:
    return {
        "id": constraint.id,
        "constraintType": constraint.constraint_type,
        "target": {
            "type": constraint.target_type,
            "id": constraint.target_id,
        },
        "hard": constraint.hard,
        "penaltyWeight": constraint.penalty_weight,
        "sourcePreferenceId": constraint.source_preference_id,
        "source": constraint.source,
        "confidence": constraint.confidence,
        "description": constraint.description,
        "value": _json_loads(constraint.value_json),
        "effectiveFrom": _iso(constraint.effective_from),
        "effectiveUntil": _iso(constraint.effective_until),
        "updatedAt": _iso(constraint.updated_at),
    }


def _observation_payload(observation: PreferenceObservation) -> dict[str, Any]:
    return {
        "id": observation.id,
        "preferenceId": observation.preference_id,
        "eventType": observation.event_type,
        "source": observation.source,
        "weight": observation.weight,
        "confidence": observation.confidence,
        "observedAt": _iso(observation.observed_at),
        "evidence": _json_loads(observation.evidence_json),
        "note": observation.note,
    }


def _interval_overlaps_payload(
    value: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    start_at = _parse_datetime(
        value.get("startAt")
        or value.get("start_at")
        or value.get("start")
        or value.get("from")
    )
    end_at = _parse_datetime(
        value.get("endAt")
        or value.get("end_at")
        or value.get("end")
        or value.get("until")
        or value.get("to")
    )
    if start_at is None or end_at is None:
        return True
    return end_at > window_start and start_at < window_end


def _build_preference_context_for_scope(
    session: Session,
    *,
    task_ids: list[int],
    category_ids: list[int],
    task_titles: list[str] | None = None,
    category_names: list[str] | None = None,
    window_start: datetime,
    window_end: datetime,
    prompt: str = "",
    user_id: str = DEFAULT_USER_ID,
    max_soft_preferences: int = 8,
    max_observations: int = 5,
) -> dict[str, Any]:
    """Return a compact, scoped memory payload for planning/scheduling."""

    scope = PreferenceRetrievalScope(
        task_ids=list(dict.fromkeys(task_ids)),
        category_ids=list(dict.fromkeys(category_ids)),
        task_titles=task_titles or [],
        category_names=category_names or [],
        window_start=window_start,
        window_end=window_end,
        prompt=prompt,
        user_id=user_id,
    )
    task_id_set = {str(task_id) for task_id in scope.task_ids}
    category_id_set = {str(category_id) for category_id in scope.category_ids}
    text = _context_text(scope)

    constraint_scope_filters = [SchedulingConstraint.target_type == "global"]
    if task_id_set:
        constraint_scope_filters.append(
            and_(
                SchedulingConstraint.target_type == "task",
                SchedulingConstraint.target_id.in_(task_id_set),
            )
        )
    if category_id_set:
        constraint_scope_filters.append(
            and_(
                SchedulingConstraint.target_type == "category",
                SchedulingConstraint.target_id.in_(category_id_set),
            )
        )
    constraints = session.exec(
        select(SchedulingConstraint)
        .where(SchedulingConstraint.user_id == user_id)
        .where(SchedulingConstraint.active == True)
        .where(or_(*constraint_scope_filters))
        .where(
            or_(
                SchedulingConstraint.effective_from == None,  # noqa: E711
                SchedulingConstraint.effective_from < window_end,
            )
        )
        .where(
            or_(
                SchedulingConstraint.effective_until == None,  # noqa: E711
                SchedulingConstraint.effective_until > window_start,
            )
        )
        .order_by(SchedulingConstraint.created_at)
    ).all()
    hard_constraints: list[SchedulingConstraint] = []
    skipped_constraints = 0
    for constraint in constraints:
        if not constraint.hard:
            skipped_constraints += 1
            continue
        if not _effective_in_window(
            effective_from=constraint.effective_from,
            effective_until=constraint.effective_until,
            window_start=window_start,
            window_end=window_end,
        ):
            skipped_constraints += 1
            continue
        if not _scope_matches(
            scope_type=constraint.target_type,
            scope_id=constraint.target_id,
            task_ids=task_id_set,
            category_ids=category_id_set,
        ):
            skipped_constraints += 1
            continue
        value = _json_loads(constraint.value_json)
        if not _interval_overlaps_payload(
            value,
            window_start=window_start,
            window_end=window_end,
        ):
            skipped_constraints += 1
            continue
        hard_constraints.append(constraint)

    link_scope_filters = []
    if task_id_set:
        link_scope_filters.append(
            and_(
                PreferenceLink.target_type == "task",
                PreferenceLink.target_id.in_(task_id_set),
            )
        )
    if category_id_set:
        link_scope_filters.append(
            and_(
                PreferenceLink.target_type == "category",
                PreferenceLink.target_id.in_(category_id_set),
            )
        )
    linked_preference_ids: set[str] = set()
    if link_scope_filters:
        linked_preference_ids = set(
            session.exec(
                select(PreferenceLink.preference_id)
                .where(PreferenceLink.user_id == user_id)
                .where(PreferenceLink.active == True)
                .where(or_(*link_scope_filters))
            ).all()
        )

    preference_scope_filters = [UserPreference.scope_type == "global"]
    if task_id_set:
        preference_scope_filters.append(
            and_(
                UserPreference.scope_type == "task",
                UserPreference.scope_id.in_(task_id_set),
            )
        )
    if category_id_set:
        preference_scope_filters.append(
            and_(
                UserPreference.scope_type == "category",
                UserPreference.scope_id.in_(category_id_set),
            )
        )
    if linked_preference_ids:
        preference_scope_filters.append(UserPreference.id.in_(linked_preference_ids))
    preferences = session.exec(
        select(UserPreference)
        .where(UserPreference.user_id == user_id)
        .where(UserPreference.active == True)
        .where(or_(*preference_scope_filters))
        .where(
            or_(
                UserPreference.effective_from == None,  # noqa: E711
                UserPreference.effective_from < window_end,
            )
        )
        .where(
            or_(
                UserPreference.effective_until == None,  # noqa: E711
                UserPreference.effective_until > window_start,
            )
        )
        .order_by(UserPreference.updated_at.desc(), UserPreference.created_at.desc())
    ).all()
    scored_preferences: list[tuple[int, UserPreference]] = []
    skipped_preferences = 0
    source_constraint_preference_ids = {
        constraint.source_preference_id
        for constraint in hard_constraints
        if constraint.source_preference_id
    }
    for preference in preferences:
        if preference.id in source_constraint_preference_ids:
            skipped_preferences += 1
            continue
        if preference.strength == "hard":
            skipped_preferences += 1
            continue
        if not _effective_in_window(
            effective_from=preference.effective_from,
            effective_until=preference.effective_until,
            window_start=window_start,
            window_end=window_end,
        ):
            skipped_preferences += 1
            continue
        if not _scope_matches(
            scope_type=preference.scope_type,
            scope_id=preference.scope_id,
            task_ids=task_id_set,
            category_ids=category_id_set,
        ) and preference.id not in linked_preference_ids:
            skipped_preferences += 1
            continue
        if (
            preference.scope_type == "global"
            and preference.id not in linked_preference_ids
            and not _domain_relevant(preference.domain, text)
        ):
            skipped_preferences += 1
            continue
        score = _preference_score(preference, scope)
        if preference.id in linked_preference_ids:
            score += 90
        if score <= 0:
            skipped_preferences += 1
            continue
        scored_preferences.append((score, preference))

    scored_preferences.sort(
        key=lambda item: (
            item[0],
            item[1].confidence,
            item[1].updated_at,
        ),
        reverse=True,
    )
    selected_preferences = [
        preference for _, preference in scored_preferences[:max_soft_preferences]
    ]
    skipped_preferences += max(0, len(scored_preferences) - len(selected_preferences))

    selected_preference_ids = [preference.id for preference in selected_preferences]
    observations: list[PreferenceObservation] = []
    skipped_observations = 0
    if selected_preference_ids:
        observation_candidates = session.exec(
            select(PreferenceObservation)
            .where(PreferenceObservation.user_id == user_id)
            .where(PreferenceObservation.preference_id.in_(selected_preference_ids))
            .order_by(PreferenceObservation.observed_at.desc())
        ).all()
        observations = observation_candidates[:max_observations]
        skipped_observations = max(0, len(observation_candidates) - len(observations))

    return {
        "contextVersion": CONTEXT_VERSION,
        "scope": {
            "userId": user_id,
            "taskIds": scope.task_ids,
            "categoryIds": scope.category_ids,
            "windowStart": _iso(window_start),
            "windowEnd": _iso(window_end),
        },
        "hardConstraints": [
            _constraint_payload(constraint) for constraint in hard_constraints
        ],
        "softPreferences": [
            _preference_payload(preference) for preference in selected_preferences
        ],
        "observations": [
            _observation_payload(observation) for observation in observations
        ],
        "excludedMemoryCount": (
            skipped_constraints + skipped_preferences + skipped_observations
        ),
        "retrievalTrace": [
            {
                "kind": "scope",
                "detail": (
                    f"{len(scope.task_ids)} tasks, {len(scope.category_ids)} categories, "
                    f"{_iso(window_start)} to {_iso(window_end)}"
                ),
            },
            {
                "kind": "hard_constraints",
                "candidateCount": len(constraints),
                "includedCount": len(hard_constraints),
            },
            {
                "kind": "soft_preferences",
                "candidateCount": len(preferences),
                "includedCount": len(selected_preferences),
            },
            {
                "kind": "observations",
                "includedCount": len(observations),
            },
        ],
    }


class PreferenceApplicationService(Protocol):
    """Provider- and transport-neutral preference query boundary."""

    def query_context(
        self,
        session: Session,
        *,
        scope: PreferenceRetrievalScope,
        max_soft_preferences: int = 8,
        max_observations: int = 5,
    ) -> dict[str, Any]: ...


class SqlPreferenceApplicationService:
    """Query authoritative preference records through one application service."""

    def query_context(
        self,
        session: Session,
        *,
        scope: PreferenceRetrievalScope,
        max_soft_preferences: int = 8,
        max_observations: int = 5,
    ) -> dict[str, Any]:
        return _build_preference_context_for_scope(
            session,
            task_ids=scope.task_ids,
            category_ids=scope.category_ids,
            task_titles=scope.task_titles,
            category_names=scope.category_names,
            window_start=scope.window_start,
            window_end=scope.window_end,
            prompt=scope.prompt,
            user_id=scope.user_id,
            max_soft_preferences=max_soft_preferences,
            max_observations=max_observations,
        )


SQL_PREFERENCE_APPLICATION_SERVICE: PreferenceApplicationService = (
    SqlPreferenceApplicationService()
)


def build_preference_context_for_scope(
    session: Session,
    *,
    task_ids: list[int],
    category_ids: list[int],
    task_titles: list[str] | None = None,
    category_names: list[str] | None = None,
    window_start: datetime,
    window_end: datetime,
    prompt: str = "",
    user_id: str = DEFAULT_USER_ID,
    max_soft_preferences: int = 8,
    max_observations: int = 5,
    service: PreferenceApplicationService = SQL_PREFERENCE_APPLICATION_SERVICE,
) -> dict[str, Any]:
    """Compatibility function over the canonical preference application service."""

    return service.query_context(
        session,
        scope=PreferenceRetrievalScope(
            task_ids=list(dict.fromkeys(task_ids)),
            category_ids=list(dict.fromkeys(category_ids)),
            task_titles=list(task_titles or []),
            category_names=list(category_names or []),
            window_start=window_start,
            window_end=window_end,
            prompt=prompt,
            user_id=user_id,
        ),
        max_soft_preferences=max_soft_preferences,
        max_observations=max_observations,
    )


def build_planner_preference_context(
    session: Session,
    *,
    working_set: Any,
    window_start: datetime,
    window_end: datetime,
    prompt: str = "",
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    tasks = list(getattr(working_set, "tasks", []) or [])
    return build_preference_context_for_scope(
        session,
        task_ids=[int(task.task_id) for task in tasks],
        category_ids=[int(task.category_id) for task in tasks],
        task_titles=[str(task.title) for task in tasks],
        category_names=[str(task.category_name) for task in tasks],
        window_start=window_start,
        window_end=window_end,
        prompt=prompt,
        user_id=user_id,
    )


def build_model_preference_context(
    preference_context: dict[str, Any],
    *,
    max_hard_constraint_details: int = 8,
) -> dict[str, Any]:
    """Project authoritative memory into a bounded provider-facing surface."""

    raw_hard = preference_context.get("hardConstraints")
    hard_constraints = raw_hard if isinstance(raw_hard, list) else []
    raw_soft = preference_context.get("softPreferences")
    soft_preferences = raw_soft if isinstance(raw_soft, list) else []
    raw_observations = preference_context.get("observations")
    observations = raw_observations if isinstance(raw_observations, list) else []
    detail_limit = max(0, min(int(max_hard_constraint_details), 16))
    selected_hard = hard_constraints[:detail_limit]
    hard_type_counts: dict[str, int] = {}
    for item in hard_constraints:
        if not isinstance(item, dict):
            continue
        key = str(item.get("constraintType") or "other")
        hard_type_counts[key] = hard_type_counts.get(key, 0) + 1
    return {
        "contextVersion": preference_context.get("contextVersion"),
        "scope": preference_context.get("scope"),
        "hardConstraints": selected_hard,
        "hardConstraintSummary": {
            "totalCount": len(hard_constraints),
            "includedDetailCount": len(selected_hard),
            "omittedDetailCount": max(0, len(hard_constraints) - len(selected_hard)),
            "typeCounts": hard_type_counts,
            "enforcement": "All applicable hard constraints are enforced by the solver.",
        },
        "softPreferences": soft_preferences,
        "observationSummary": {
            "availableCount": len(observations),
            "includedDetailCount": 0,
        },
        "excludedMemoryCount": preference_context.get("excludedMemoryCount", 0),
    }


def _gap_minutes_from_value(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in INTER_TASK_GAP_VALUE_KEYS:
        raw_minutes = value.get(key)
        if isinstance(raw_minutes, bool) or not isinstance(raw_minutes, (int, float)):
            continue
        minutes = int(raw_minutes)
        if float(raw_minutes) != float(minutes) or not 0 <= minutes <= 1440:
            continue
        return minutes
    return None


def _bounded_minutes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    minutes = int(value)
    if float(value) != float(minutes) or not 0 <= minutes <= 1440:
        return None
    return minutes


def _gap_duration_threshold(
    value: Any,
    *,
    description: Any = None,
) -> int | None:
    if isinstance(value, dict):
        for key in (
            "minimumTaskDurationMinutes",
            "minimum_task_duration_minutes",
            "taskDurationAtLeastMinutes",
            "task_duration_at_least_minutes",
            "appliesToTasksAtLeastMinutes",
            "applies_to_tasks_at_least_minutes",
        ):
            minutes = _bounded_minutes(value.get(key))
            if minutes is not None and minutes > 0:
                return minutes

    text = str(description or "")
    for pattern in (
        r"\b(?:tasks?\s+)?(?:shorter|less)\s+than\s+(\d+)\s*(?:minutes?|mins?)\b",
        r"\b(\d+)\s*(?:minutes?|mins?)\s*(?:or\s+longer|and\s+longer|or\s+more|\+)\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match is not None:
            minutes = _bounded_minutes(int(match.group(1)))
            if minutes is not None and minutes > 0:
                return minutes
    return None


def _gap_duration_maximum(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in (
        "maximumTaskDurationMinutes",
        "maximum_task_duration_minutes",
        "taskDurationAtMostMinutes",
        "task_duration_at_most_minutes",
    ):
        minutes = _bounded_minutes(value.get(key))
        if minutes is not None:
            return minutes
    return None


def _short_task_gap_minutes(value: Any, *, threshold: int | None) -> int | None:
    if not isinstance(value, dict):
        return 0 if threshold is not None else None
    for key in (
        "shorterTaskGapMinutes",
        "shorter_task_gap_minutes",
        "belowThresholdGapMinutes",
        "below_threshold_gap_minutes",
    ):
        minutes = _bounded_minutes(value.get(key))
        if minutes is not None:
            return minutes
    return 0 if threshold is not None else None


def _gap_rule_from_item(
    item: dict[str, Any],
    *,
    minutes: int,
    enforcement: str,
) -> dict[str, Any] | None:
    value = item.get("value")
    threshold = _gap_duration_threshold(
        value,
        description=item.get("description"),
    )
    maximum = _gap_duration_maximum(value)
    if threshold is None and maximum is None:
        return None
    rule = {
        "gapMinutes": minutes,
        "minimumTaskDurationMinutes": threshold or 0,
        "shorterTaskGapMinutes": _short_task_gap_minutes(value, threshold=threshold) or 0,
        "enforcement": enforcement,
        "source": {
            "id": item.get("id"),
            "description": item.get("description"),
        },
    }
    if maximum is not None:
        rule["maximumTaskDurationMinutes"] = maximum
    return rule


def _gap_preference_sort_key(item: dict[str, Any], index: int) -> tuple[int, datetime, int]:
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    scope_rank = {"global": 1, "category": 2, "project": 2, "task": 3}.get(
        str(scope.get("type") or "global").strip().lower(),
        0,
    )
    updated_at = _parse_datetime(item.get("updatedAt")) or datetime.min
    return scope_rank, updated_at, -index


def _gap_item_scope_rank(item: dict[str, Any], *, constraint: bool = False) -> int:
    key = "target" if constraint else "scope"
    scope = item.get(key) if isinstance(item.get(key), dict) else {}
    return {"global": 1, "category": 2, "project": 2, "task": 3}.get(
        str(scope.get("type") or "global").strip().lower(),
        0,
    )


def inter_task_gap_policy_from_context(
    preference_context: dict[str, Any],
) -> dict[str, Any]:
    """Compile remembered gap memory into a deterministic scheduling policy.

    The highest-ranked scoped soft preference supplies the default. Every
    applicable hard constraint contributes a minimum, with the strictest
    minimum winning. Current-turn directives may replace only the soft value.
    """

    soft_default_minutes = 0
    soft_source: dict[str, Any] | None = None
    hard_minimum_minutes = 0
    hard_sources: list[dict[str, Any]] = []
    conditional_rules: list[dict[str, Any]] = []
    selected_soft_updated_at: datetime | None = None
    selected_soft_scope_rank = 0
    soft_preferences = preference_context.get("softPreferences")
    if isinstance(soft_preferences, list):
        gap_preferences: list[tuple[dict[str, Any], int, int]] = []
        for index, item in enumerate(soft_preferences):
            if not isinstance(item, dict):
                continue
            minutes = _gap_minutes_from_value(item.get("value"))
            if minutes is None:
                continue
            gap_preferences.append((item, minutes, index))

        gap_preferences.sort(
            key=lambda candidate: _gap_preference_sort_key(candidate[0], candidate[2]),
            reverse=True,
        )
        for item, minutes, _index in gap_preferences:
            preference_type = str(item.get("preferenceType") or "").strip().lower()
            strength = str(item.get("strength") or "").strip().lower()
            hard_preference = strength in {"hard", "high", "strong", "required"}
            conditional_rule = _gap_rule_from_item(
                item,
                minutes=minutes,
                enforcement="hard" if hard_preference else "soft",
            )
            selected_soft_updated_at = _parse_datetime(item.get("updatedAt"))
            selected_soft_scope_rank = _gap_item_scope_rank(item)
            if hard_preference:
                if conditional_rule is not None:
                    conditional_rules.append(conditional_rule)
                else:
                    hard_minimum_minutes = max(hard_minimum_minutes, minutes)
                hard_sources.append(
                    {
                        "kind": "legacy_hard_gap_preference",
                        "id": item.get("id"),
                        "description": item.get("description"),
                        "minutes": minutes,
                    }
                )
                continue
            if conditional_rule is not None:
                conditional_rules.append(conditional_rule)
                soft_default_minutes = max(
                    soft_default_minutes,
                    int(conditional_rule["shorterTaskGapMinutes"]),
                )
            elif soft_source is None:
                soft_default_minutes = minutes
            if soft_source is None:
                soft_source = {
                    "kind": "soft_preference",
                    "id": item.get("id"),
                    "description": item.get("description"),
                    "conditional": conditional_rule is not None,
                }

    hard_constraints = preference_context.get("hardConstraints")
    if isinstance(hard_constraints, list):
        for item in hard_constraints:
            if not isinstance(item, dict) or item.get("hard") is not True:
                continue
            constraint_type = str(item.get("constraintType") or "").strip().lower()
            if constraint_type not in INTER_TASK_GAP_CONSTRAINT_TYPES:
                continue
            minutes = _gap_minutes_from_value(item.get("value"))
            if minutes is None:
                continue
            constraint_updated_at = _parse_datetime(item.get("updatedAt"))
            if (
                selected_soft_updated_at is not None
                and constraint_updated_at is not None
                and selected_soft_updated_at >= constraint_updated_at
                and selected_soft_scope_rank >= _gap_item_scope_rank(item, constraint=True)
            ):
                continue
            conditional_rule = _gap_rule_from_item(
                item,
                minutes=minutes,
                enforcement="hard",
            )
            if conditional_rule is not None:
                conditional_rules.append(conditional_rule)
            else:
                hard_minimum_minutes = max(hard_minimum_minutes, minutes)
            hard_sources.append(
                {
                    "kind": "hard_constraint",
                    "id": item.get("id"),
                    "description": item.get("description"),
                    "minutes": minutes,
                }
            )

    sources = [*([soft_source] if soft_source is not None else []), *hard_sources]
    return {
        "softDefaultMinutes": soft_default_minutes,
        "hardMinimumMinutes": hard_minimum_minutes,
        "effectiveDefaultMinutes": max(soft_default_minutes, hard_minimum_minutes),
        "conditionalGapRules": conditional_rules,
        "sources": sources,
    }


def inter_task_gap_minutes_for_duration(
    gap_policy: dict[str, Any],
    *,
    duration_minutes: int,
) -> tuple[int, int]:
    """Return soft and hard gap minutes for one task under a compiled policy."""

    soft = _bounded_minutes(gap_policy.get("softDefaultMinutes")) or 0
    hard = _bounded_minutes(gap_policy.get("hardMinimumMinutes")) or 0
    for rule in gap_policy.get("conditionalGapRules", []):
        if not isinstance(rule, dict):
            continue
        threshold = _bounded_minutes(rule.get("minimumTaskDurationMinutes"))
        maximum = _bounded_minutes(rule.get("maximumTaskDurationMinutes"))
        minutes = _bounded_minutes(rule.get("gapMinutes"))
        if (
            threshold is None
            or minutes is None
            or duration_minutes < threshold
            or (maximum is not None and duration_minutes > maximum)
        ):
            continue
        if str(rule.get("enforcement") or "soft").strip().lower() == "hard":
            hard = max(hard, minutes)
        else:
            soft = max(soft, minutes)
    return soft, hard


def constraint_intervals_from_context(
    preference_context: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[OccupiedInterval]:
    """Compile hard fixed/forbidden windows into solver occupancy."""

    intervals: list[OccupiedInterval] = []
    hard_constraints = preference_context.get("hardConstraints")
    if not isinstance(hard_constraints, list):
        return intervals

    for index, item in enumerate(hard_constraints):
        if not isinstance(item, dict) or item.get("hard") is not True:
            continue
        constraint_type = str(item.get("constraintType") or "")
        if constraint_type not in HARD_INTERVAL_TYPES:
            continue
        value = item.get("value") if isinstance(item.get("value"), dict) else {}
        start_at = _parse_datetime(
            value.get("startAt")
            or value.get("start_at")
            or value.get("start")
            or value.get("from")
        )
        end_at = _parse_datetime(
            value.get("endAt")
            or value.get("end_at")
            or value.get("end")
            or value.get("until")
            or value.get("to")
        )
        if start_at is None or end_at is None or end_at <= start_at:
            start_minutes = _parse_time_minutes(
                value.get("startTime") or value.get("start_time")
            )
            end_minutes = _parse_time_minutes(
                value.get("endTime") or value.get("end_time")
            )
            if start_minutes is None or end_minutes is None:
                continue
            day = window_start.date() - timedelta(days=1)
            last_day = window_end.date()
            while day <= last_day:
                if _day_allowed(day, value.get("daysOfWeek") or value.get("days_of_week")):
                    recurring_start = _datetime_from_time_minutes(day, start_minutes)
                    recurring_end = _datetime_from_time_minutes(day, end_minutes)
                    if recurring_end <= recurring_start:
                        recurring_end += timedelta(days=1)
                    if recurring_end > window_start and recurring_start < window_end:
                        intervals.append(
                            OccupiedInterval(
                                block_id=-900000 - len(intervals),
                                start_at=max(recurring_start, window_start),
                                end_at=min(recurring_end, window_end),
                                source_type="preference",
                                source_label=str(
                                    item.get("description")
                                    or item.get("constraintType")
                                    or "Scheduling preference"
                                ),
                            )
                        )
                day += timedelta(days=1)
            continue
        if end_at <= window_start or start_at >= window_end:
            continue
        intervals.append(
            OccupiedInterval(
                block_id=-900000 - len(intervals),
                start_at=max(start_at, window_start),
                end_at=min(end_at, window_end),
                source_type="preference",
                source_label=str(
                    item.get("description")
                    or item.get("constraintType")
                    or "Scheduling preference"
                ),
            )
        )
    return intervals
