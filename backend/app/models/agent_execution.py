"""Durable native ChangeSet execution and external-sync outbox records."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentChangeSetExecutionRecord(SQLModel, table=True):
    __tablename__ = "agent_change_set_executions"
    __table_args__ = (
        UniqueConstraint("session_id", "command_id", name="uq_agent_execution_command"),
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_execution_idempotency",
        ),
        UniqueConstraint(
            "change_set_id",
            "change_set_revision",
            name="uq_agent_execution_changeset_revision",
        ),
    )

    execution_id: str = Field(primary_key=True, max_length=128)
    session_id: str = Field(index=True, max_length=64)
    command_id: str = Field(index=True, max_length=128)
    idempotency_key: str = Field(index=True, max_length=128)
    goal_id: str = Field(index=True, max_length=128)
    goal_revision: int = Field(ge=1, index=True)
    change_set_id: str = Field(index=True, max_length=128)
    change_set_revision: int = Field(ge=1, index=True)
    simulation_id: str = Field(index=True, max_length=128)
    payload_fingerprint: str = Field(max_length=64)
    status: str = Field(index=True, max_length=48)
    intended_fingerprint: str = Field(max_length=128)
    actual_fingerprint: Optional[str] = Field(default=None, max_length=128)
    projection_json: str = Field(sa_column=Column(Text, nullable=False))
    result_json: str = Field(sa_column=Column(Text, nullable=False))
    verification_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AgentExternalSyncOutboxRecord(SQLModel, table=True):
    __tablename__ = "agent_external_sync_outbox"
    __table_args__ = (
        UniqueConstraint("semantic_key", name="uq_agent_external_sync_semantic_key"),
    )

    outbox_id: str = Field(primary_key=True, max_length=128)
    execution_id: str = Field(
        foreign_key="agent_change_set_executions.execution_id", index=True, max_length=128
    )
    session_id: str = Field(index=True, max_length=64)
    change_set_id: str = Field(index=True, max_length=128)
    change_set_revision: int = Field(ge=1, index=True)
    provider: str = Field(index=True, max_length=32)
    operation: str = Field(index=True, max_length=16)
    entity_kind: str = Field(index=True, max_length=32)
    entity_id: str = Field(index=True, max_length=128)
    semantic_key: str = Field(index=True, max_length=256)
    effect_ordinal: int = Field(default=0, ge=0, index=True)
    payload_json: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default="pending", index=True, max_length=32)
    attempts: int = Field(default=0, ge=0)
    claim_token: Optional[str] = Field(default=None, index=True, max_length=128)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    next_attempt_at: Optional[datetime] = Field(default=None, index=True)
    remote_id: Optional[str] = Field(default=None, max_length=256)
    remote_version: Optional[str] = Field(default=None, max_length=256)
    last_error_code: Optional[str] = Field(default=None, max_length=64)
    last_error_summary: Optional[str] = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AgentReviewActionRecord(SQLModel, table=True):
    """Durable receipt for an exact typed review action and its safe outcome."""

    __tablename__ = "agent_review_actions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_review_action_idempotency",
        ),
    )

    action_record_id: str = Field(primary_key=True, max_length=128)
    session_id: str = Field(index=True, max_length=64)
    review_id: str = Field(index=True, max_length=128)
    event_id: str = Field(index=True, max_length=128)
    idempotency_key: str = Field(index=True, max_length=128)
    action_kind: str = Field(index=True, max_length=32)
    request_fingerprint: str = Field(max_length=64)
    outcome_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
