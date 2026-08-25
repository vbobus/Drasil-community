"""Persistent user preference and scheduling constraint memory."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _memory_id() -> str:
    return str(uuid4())


class UserPreference(SQLModel, table=True):
    """Canonical user preference record.

    Flexible details stay in JSON text so the local SQLite shape can migrate to
    JSONB/vector-backed retrieval later without changing the product contract.
    """

    __tablename__ = "user_preferences"

    id: str = Field(default_factory=_memory_id, primary_key=True, max_length=64)
    revision: int = Field(default=1, ge=1, index=True)
    user_id: str = Field(default="local", index=True, max_length=64)
    domain: str = Field(default="scheduling", index=True, max_length=64)
    preference_type: str = Field(default="general", index=True, max_length=64)
    scope_type: str = Field(default="global", index=True, max_length=32)
    scope_id: Optional[str] = Field(default=None, index=True, max_length=64)
    strength: str = Field(default="medium", index=True, max_length=24)
    source: str = Field(default="user_explicit", index=True, max_length=48)
    confidence: float = Field(default=1.0, index=True)
    active: bool = Field(default=True, index=True)
    effective_from: Optional[datetime] = Field(default=None, index=True)
    effective_until: Optional[datetime] = Field(default=None, index=True)
    description: Optional[str] = Field(default=None, max_length=512)
    value_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class SchedulingConstraint(SQLModel, table=True):
    """Typed constraints that can be enforced by scheduling tools."""

    __tablename__ = "scheduling_constraints"

    id: str = Field(default_factory=_memory_id, primary_key=True, max_length=64)
    revision: int = Field(default=1, ge=1, index=True)
    user_id: str = Field(default="local", index=True, max_length=64)
    constraint_type: str = Field(default="forbidden_window", index=True, max_length=64)
    target_type: str = Field(default="global", index=True, max_length=32)
    target_id: Optional[str] = Field(default=None, index=True, max_length=64)
    hard: bool = Field(default=True, index=True)
    penalty_weight: float = Field(default=1.0, index=True)
    source_preference_id: Optional[str] = Field(default=None, index=True, max_length=64)
    source: str = Field(default="user_explicit", index=True, max_length=48)
    confidence: float = Field(default=1.0, index=True)
    active: bool = Field(default=True, index=True)
    effective_from: Optional[datetime] = Field(default=None, index=True)
    effective_until: Optional[datetime] = Field(default=None, index=True)
    description: Optional[str] = Field(default=None, max_length=512)
    value_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PreferenceObservation(SQLModel, table=True):
    """Evidence record used to refine preference confidence over time."""

    __tablename__ = "preference_observations"

    id: str = Field(default_factory=_memory_id, primary_key=True, max_length=64)
    user_id: str = Field(default="local", index=True, max_length=64)
    preference_id: str = Field(index=True, max_length=64)
    event_type: str = Field(default="observed_behavior", index=True, max_length=64)
    source: str = Field(default="system_observed", index=True, max_length=48)
    weight: float = Field(default=1.0, index=True)
    confidence: float = Field(default=0.5, index=True)
    observed_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    evidence_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    note: Optional[str] = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PreferenceLink(SQLModel, table=True):
    """Graph-like relationship between memory and product entities."""

    __tablename__ = "preference_links"

    id: str = Field(default_factory=_memory_id, primary_key=True, max_length=64)
    revision: int = Field(default=1, ge=1, index=True)
    user_id: str = Field(default="local", index=True, max_length=64)
    preference_id: str = Field(index=True, max_length=64)
    target_type: str = Field(index=True, max_length=32)
    target_id: str = Field(index=True, max_length=64)
    relation_type: str = Field(default="applies_to", index=True, max_length=64)
    weight: float = Field(default=1.0, index=True)
    active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PreferenceChangeLog(SQLModel, table=True):
    """Audit trail for preference updates and future approval-gated writes."""

    __tablename__ = "preference_change_log"

    id: str = Field(default_factory=_memory_id, primary_key=True, max_length=64)
    user_id: str = Field(default="local", index=True, max_length=64)
    record_type: str = Field(index=True, max_length=48)
    record_id: str = Field(index=True, max_length=64)
    change_type: str = Field(index=True, max_length=48)
    actor: str = Field(default="system", index=True, max_length=48)
    reason: Optional[str] = Field(default=None, max_length=512)
    before_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    after_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


from app.models.revisioning import register_revisioned_model  # noqa: E402

register_revisioned_model(UserPreference)
register_revisioned_model(SchedulingConstraint)
register_revisioned_model(PreferenceLink)
