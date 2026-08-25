"""Remembered preference settings API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, alias_generators
from sqlmodel import Session, select

from app.db.database import get_session
from app.models.preference_memory import (
    PreferenceChangeLog,
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)
from app.services.preference_admin import (
    analyze_remembered_preference_cleanup,
    find_semantic_duplicate_matches,
)
from app.services.preference_values import (
    decode_preference_value,
    encode_preference_value,
    enforcement_constraint_type,
)

router = APIRouter(prefix="/api/v1/preferences", tags=["Preferences"])
DEFAULT_USER_ID = "local"


class RememberedConstraintRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    constraint_type: str
    target_type: str
    target_id: str | None = None
    hard: bool
    active: bool
    description: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class RememberedPreferenceIssueRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    reason_code: str
    reason: str
    related_preference_id: str | None = None
    related_description: str | None = None


class RememberedPreferenceRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    revision: int
    domain: str
    preference_type: str
    scope_type: str
    scope_id: str | None = None
    strength: str
    source: str
    confidence: float
    active: bool
    description: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    issue: RememberedPreferenceIssueRead | None = None
    constraints: list[RememberedConstraintRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RememberedPreferencesSummary(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    active_count: int
    inactive_count: int
    hard_constraint_count: int
    issue_count: int = 0


class RememberedPreferencesResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    preferences: list[RememberedPreferenceRead]
    summary: RememberedPreferencesSummary


class RememberedPreferenceCreate(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    domain: str = Field(default="scheduling", min_length=1, max_length=64)
    preference_type: str = Field(default="general", min_length=1, max_length=64)
    scope_type: Literal["global", "task", "category"] = "global"
    scope_id: str | None = Field(default=None, max_length=64)
    strength: Literal["low", "medium", "strong", "high", "hard"] = "medium"
    active: bool = True
    description: str = Field(min_length=1, max_length=512)
    value: dict[str, Any] = Field(default_factory=dict)
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class RememberedPreferenceUpdate(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    revision: int | None = Field(default=None, ge=1)
    domain: str | None = Field(default=None, min_length=1, max_length=64)
    preference_type: str | None = Field(default=None, min_length=1, max_length=64)
    scope_type: Literal["global", "task", "category"] | None = None
    scope_id: str | None = Field(default=None, max_length=64)
    strength: Literal["low", "medium", "strong", "high", "hard"] | None = None
    active: bool | None = None
    description: str | None = Field(default=None, min_length=1, max_length=512)
    value: dict[str, Any] | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    return decode_preference_value(value)


def _constraint_read(constraint: SchedulingConstraint) -> RememberedConstraintRead:
    return RememberedConstraintRead(
        id=constraint.id,
        constraint_type=constraint.constraint_type,
        target_type=constraint.target_type,
        target_id=constraint.target_id,
        hard=constraint.hard,
        active=constraint.active,
        description=constraint.description,
        value=_json_loads(constraint.value_json),
        created_at=constraint.created_at,
        updated_at=constraint.updated_at,
    )


def _preference_read(
    preference: UserPreference,
    constraints_by_preference_id: dict[str, list[SchedulingConstraint]],
    *,
    issue: RememberedPreferenceIssueRead | None = None,
) -> RememberedPreferenceRead:
    return RememberedPreferenceRead(
        id=preference.id,
        revision=preference.revision,
        domain=preference.domain,
        preference_type=preference.preference_type,
        scope_type=preference.scope_type,
        scope_id=preference.scope_id,
        strength=preference.strength,
        source=preference.source,
        confidence=preference.confidence,
        active=preference.active,
        description=preference.description,
        value=_json_loads(preference.value_json),
        effective_from=preference.effective_from,
        effective_until=preference.effective_until,
        issue=issue,
        constraints=[
            _constraint_read(constraint)
            for constraint in constraints_by_preference_id.get(preference.id, [])
        ],
        created_at=preference.created_at,
        updated_at=preference.updated_at,
    )


def _change_log(
    *,
    user_id: str,
    record_type: str,
    record_id: str,
    before_active: bool,
    after_active: bool,
) -> PreferenceChangeLog:
    return PreferenceChangeLog(
        user_id=user_id,
        record_type=record_type,
        record_id=record_id,
        change_type="activate" if after_active else "deactivate",
        actor="user_settings",
        reason="Updated from remembered preferences settings.",
        before_json=_json_dumps({"active": before_active}),
        after_json=_json_dumps({"active": after_active}),
    )


def _preference_snapshot(preference: UserPreference) -> dict[str, Any]:
    return {
        "revision": preference.revision,
        "domain": preference.domain,
        "preferenceType": preference.preference_type,
        "scopeType": preference.scope_type,
        "scopeId": preference.scope_id,
        "strength": preference.strength,
        "active": preference.active,
        "description": preference.description,
        "value": _json_loads(preference.value_json),
        "effectiveFrom": preference.effective_from.isoformat()
        if preference.effective_from
        else None,
        "effectiveUntil": preference.effective_until.isoformat()
        if preference.effective_until
        else None,
    }


def _validate_scope_and_dates(
    *,
    scope_type: str,
    scope_id: str | None,
    effective_from: datetime | None,
    effective_until: datetime | None,
) -> None:
    if scope_type == "global" and scope_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Global preferences cannot have a scope ID.",
        )
    if scope_type != "global" and not str(scope_id or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Task and category preferences require a scope ID.",
        )
    if effective_from and effective_until and effective_until <= effective_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Preference expiry must be after its effective start.",
        )


def _duplicate_conflict(matches: list[dict[str, Any]]) -> HTTPException:
    match = matches[0]
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": {
                "type": "DuplicatePreference",
                "detail": "An active preference with the same meaning already exists.",
                "existingPreferenceId": match["id"],
                "existingDescription": match.get("description"),
                "similarity": match["score"],
            }
        },
    )


def _sync_linked_constraints(
    session: Session,
    *,
    preference: UserPreference,
    constraints: list[SchedulingConstraint],
) -> list[SchedulingConstraint]:
    value = _json_loads(preference.value_json)
    constraint_type = enforcement_constraint_type(
        preference.preference_type,
        value,
    )
    should_enforce = (
        preference.strength in {"hard", "strong", "high"}
        and constraint_type is not None
    )
    if should_enforce and not constraints:
        constraints = [
            SchedulingConstraint(
                user_id=preference.user_id,
                constraint_type=constraint_type or "general",
                target_type=preference.scope_type,
                target_id=preference.scope_id,
                hard=True,
                source_preference_id=preference.id,
                source="user_settings",
                confidence=1.0,
                active=preference.active,
                effective_from=preference.effective_from,
                effective_until=preference.effective_until,
                description=preference.description,
                value_json=encode_preference_value(value),
            )
        ]
    for constraint in constraints:
        if should_enforce:
            constraint.constraint_type = constraint_type or constraint.constraint_type
            constraint.target_type = preference.scope_type
            constraint.target_id = preference.scope_id
            constraint.hard = True
            constraint.source = "user_settings"
            constraint.confidence = 1.0
            constraint.active = preference.active
            constraint.effective_from = preference.effective_from
            constraint.effective_until = preference.effective_until
            constraint.description = preference.description
            constraint.value_json = encode_preference_value(value)
            constraint.updated_at = datetime.utcnow()
        else:
            constraint.active = False
            constraint.updated_at = datetime.utcnow()
        session.add(constraint)
    return constraints


def _issue_map(session: Session) -> dict[str, RememberedPreferenceIssueRead]:
    analysis = analyze_remembered_preference_cleanup(
        session,
        user_id=DEFAULT_USER_ID,
    )
    issues: dict[str, RememberedPreferenceIssueRead] = {}
    for candidate in analysis.get("candidates", []):
        related = (candidate.get("conflicts_with") or [{}])[0]
        issues[candidate["id"]] = RememberedPreferenceIssueRead(
            reason_code=candidate["reason_code"],
            reason=candidate["reason"],
            related_preference_id=related.get("id"),
            related_description=related.get("description"),
        )
    for ambiguous in analysis.get("ambiguous", []):
        related = (ambiguous.get("related_preferences") or [{}])[0]
        issues.setdefault(
            ambiguous["id"],
            RememberedPreferenceIssueRead(
                reason_code=ambiguous["reason_code"],
                reason=ambiguous["reason"],
                related_preference_id=related.get("id"),
                related_description=related.get("description"),
            ),
        )
    return issues


@router.get("/remembered", response_model=RememberedPreferencesResponse)
def list_remembered_preferences(
    include_inactive: bool = Query(False, alias="includeInactive"),
    session: Session = Depends(get_session),
) -> RememberedPreferencesResponse:
    preferences = session.exec(
        select(UserPreference)
        .where(UserPreference.user_id == DEFAULT_USER_ID)
        .order_by(UserPreference.active.desc(), UserPreference.updated_at.desc())
    ).all()
    if not include_inactive:
        preferences = [preference for preference in preferences if preference.active]

    preference_ids = [preference.id for preference in preferences]
    constraints_by_preference_id: dict[str, list[SchedulingConstraint]] = {}
    if preference_ids:
        constraints = session.exec(
            select(SchedulingConstraint)
            .where(SchedulingConstraint.user_id == DEFAULT_USER_ID)
            .where(SchedulingConstraint.source_preference_id.in_(preference_ids))
            .order_by(SchedulingConstraint.created_at)
        ).all()
        for constraint in constraints:
            if not constraint.source_preference_id:
                continue
            constraints_by_preference_id.setdefault(
                constraint.source_preference_id,
                [],
            ).append(constraint)

    all_preferences = session.exec(
        select(UserPreference).where(UserPreference.user_id == DEFAULT_USER_ID)
    ).all()
    all_constraints = session.exec(
        select(SchedulingConstraint).where(SchedulingConstraint.user_id == DEFAULT_USER_ID)
    ).all()
    issues = _issue_map(session)
    return RememberedPreferencesResponse(
        preferences=[
            _preference_read(
                preference,
                constraints_by_preference_id,
                issue=issues.get(preference.id),
            )
            for preference in preferences
        ],
        summary=RememberedPreferencesSummary(
            active_count=sum(1 for preference in all_preferences if preference.active),
            inactive_count=sum(1 for preference in all_preferences if not preference.active),
            hard_constraint_count=sum(
                1 for constraint in all_constraints if constraint.hard and constraint.active
            ),
            issue_count=len(issues),
        ),
    )


@router.post(
    "/remembered",
    response_model=RememberedPreferenceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_remembered_preference(
    body: RememberedPreferenceCreate,
    session: Session = Depends(get_session),
) -> RememberedPreferenceRead:
    scope_id = body.scope_id.strip() if body.scope_id else None
    _validate_scope_and_dates(
        scope_type=body.scope_type,
        scope_id=scope_id,
        effective_from=body.effective_from,
        effective_until=body.effective_until,
    )
    matches = find_semantic_duplicate_matches(
        session,
        user_id=DEFAULT_USER_ID,
        preference_type=body.preference_type,
        domain=body.domain,
        scope_type=body.scope_type,
        scope_id=scope_id,
        description=body.description,
        value=body.value,
    )
    if matches:
        raise _duplicate_conflict(matches)

    preference = UserPreference(
        user_id=DEFAULT_USER_ID,
        domain=body.domain.strip(),
        preference_type=body.preference_type.strip(),
        scope_type=body.scope_type,
        scope_id=scope_id,
        strength=body.strength,
        source="user_settings",
        confidence=1.0,
        active=body.active,
        effective_from=body.effective_from,
        effective_until=body.effective_until,
        description=body.description.strip(),
        value_json=encode_preference_value(body.value),
    )
    session.add(preference)
    session.flush()
    constraints = _sync_linked_constraints(
        session,
        preference=preference,
        constraints=[],
    )
    session.add(
        PreferenceChangeLog(
            user_id=DEFAULT_USER_ID,
            record_type="user_preference",
            record_id=preference.id,
            change_type="create",
            actor="user_settings",
            reason="Created from remembered preferences settings.",
            before_json=None,
            after_json=_json_dumps(_preference_snapshot(preference)),
        )
    )
    session.commit()
    session.refresh(preference)
    for constraint in constraints:
        session.refresh(constraint)
    return _preference_read(preference, {preference.id: constraints})


@router.patch("/remembered/{preference_id}", response_model=RememberedPreferenceRead)
def update_remembered_preference(
    preference_id: str,
    body: RememberedPreferenceUpdate,
    session: Session = Depends(get_session),
) -> RememberedPreferenceRead:
    preference = session.get(UserPreference, preference_id)
    if preference is None or preference.user_id != DEFAULT_USER_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "NotFound",
                    "detail": f"Remembered preference {preference_id} not found.",
                }
            },
        )

    changed_fields = body.model_fields_set - {"revision"}
    if not changed_fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one preference field must be updated.",
        )
    semantic_fields = changed_fields - {"active"}
    if semantic_fields and body.revision is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Editing preference meaning requires its current revision.",
        )
    if body.revision is not None and body.revision != preference.revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The preference changed after it was loaded. Refresh and try again.",
        )

    constraints = session.exec(
        select(SchedulingConstraint)
        .where(SchedulingConstraint.user_id == DEFAULT_USER_ID)
        .where(SchedulingConstraint.source_preference_id == preference.id)
        .order_by(SchedulingConstraint.created_at)
    ).all()
    links = session.exec(
        select(PreferenceLink)
        .where(PreferenceLink.user_id == DEFAULT_USER_ID)
        .where(PreferenceLink.preference_id == preference.id)
        .order_by(PreferenceLink.created_at, PreferenceLink.id)
    ).all()
    before = _preference_snapshot(preference)
    previous_preference_active = preference.active
    next_scope_type = body.scope_type or preference.scope_type
    if "scope_id" in body.model_fields_set:
        next_scope_id = body.scope_id.strip() if body.scope_id else None
    else:
        next_scope_id = preference.scope_id
    if next_scope_type == "global":
        next_scope_id = None
    next_effective_from = (
        body.effective_from
        if "effective_from" in body.model_fields_set
        else preference.effective_from
    )
    next_effective_until = (
        body.effective_until
        if "effective_until" in body.model_fields_set
        else preference.effective_until
    )
    _validate_scope_and_dates(
        scope_type=next_scope_type,
        scope_id=next_scope_id,
        effective_from=next_effective_from,
        effective_until=next_effective_until,
    )
    next_value = body.value if body.value is not None else _json_loads(preference.value_json)
    next_description = body.description or preference.description or ""
    if semantic_fields:
        matches = find_semantic_duplicate_matches(
            session,
            user_id=DEFAULT_USER_ID,
            preference_type=body.preference_type or preference.preference_type,
            domain=body.domain or preference.domain,
            scope_type=next_scope_type,
            scope_id=next_scope_id,
            description=next_description,
            value=next_value,
            exclude_preference_id=preference.id,
        )
        if matches:
            raise _duplicate_conflict(matches)

    if body.active is not None and previous_preference_active != body.active:
        session.add(
            _change_log(
                user_id=preference.user_id,
                record_type="user_preference",
                record_id=preference.id,
                before_active=previous_preference_active,
                after_active=body.active,
            )
        )
    if body.domain is not None:
        preference.domain = body.domain.strip()
    if body.preference_type is not None:
        preference.preference_type = body.preference_type.strip()
    preference.scope_type = next_scope_type
    preference.scope_id = next_scope_id
    if body.strength is not None:
        preference.strength = body.strength
    if body.active is not None:
        preference.active = body.active
    if body.description is not None:
        preference.description = body.description.strip()
    if body.value is not None:
        preference.value_json = encode_preference_value(body.value)
    preference.effective_from = next_effective_from
    preference.effective_until = next_effective_until
    preference.updated_at = datetime.utcnow()
    session.add(preference)

    constraint_active_before = {
        constraint.id: constraint.active for constraint in constraints
    }
    if semantic_fields:
        constraints = _sync_linked_constraints(
            session,
            preference=preference,
            constraints=list(constraints),
        )
    else:
        for constraint in constraints:
            constraint.active = preference.active
            constraint.updated_at = datetime.utcnow()
            session.add(constraint)
    for constraint in constraints:
        previous_constraint_active = constraint_active_before.get(
            constraint.id,
            False,
        )
        if previous_constraint_active != constraint.active:
            session.add(
                _change_log(
                    user_id=constraint.user_id,
                    record_type="scheduling_constraint",
                    record_id=constraint.id,
                    before_active=previous_constraint_active,
                    after_active=constraint.active,
                )
            )

    for link in links:
        if body.active is not None:
            link.active = body.active
            session.add(link)

    if semantic_fields:
        session.add(
            PreferenceChangeLog(
                user_id=preference.user_id,
                record_type="user_preference",
                record_id=preference.id,
                change_type="update",
                actor="user_settings",
                reason="Edited from remembered preferences settings.",
                before_json=_json_dumps(before),
                after_json=_json_dumps(_preference_snapshot(preference)),
            )
        )

    session.commit()
    session.refresh(preference)
    refreshed_constraints = session.exec(
        select(SchedulingConstraint)
        .where(SchedulingConstraint.user_id == DEFAULT_USER_ID)
        .where(SchedulingConstraint.source_preference_id == preference.id)
        .order_by(SchedulingConstraint.created_at)
    ).all()
    return _preference_read(preference, {preference.id: refreshed_constraints})


@router.delete(
    "/remembered/{preference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_remembered_preference(
    preference_id: str,
    session: Session = Depends(get_session),
) -> None:
    preference = session.get(UserPreference, preference_id)
    if preference is None or preference.user_id != DEFAULT_USER_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "NotFound",
                    "detail": f"Remembered preference {preference_id} not found.",
                }
            },
        )

    constraints = session.exec(
        select(SchedulingConstraint).where(
            SchedulingConstraint.source_preference_id == preference.id
        )
    ).all()
    observations = session.exec(
        select(PreferenceObservation).where(
            PreferenceObservation.preference_id == preference.id
        )
    ).all()
    links = session.exec(
        select(PreferenceLink).where(PreferenceLink.preference_id == preference.id)
    ).all()
    session.add(
        PreferenceChangeLog(
            user_id=preference.user_id,
            record_type="user_preference",
            record_id=preference.id,
            change_type="delete",
            actor="user_settings",
            reason="Permanently deleted from remembered preferences settings.",
            before_json=_json_dumps(
                {
                    "active": preference.active,
                    "preferenceType": preference.preference_type,
                    "scopeType": preference.scope_type,
                    "scopeId": preference.scope_id,
                    "description": preference.description,
                    "value": _json_loads(preference.value_json),
                }
            ),
            after_json=None,
        )
    )
    for record in [*constraints, *observations, *links]:
        session.delete(record)
    session.delete(preference)
    session.commit()
