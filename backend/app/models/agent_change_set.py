"""Canonical Agent Kernel v2 ChangeSet snapshots and command audit."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentChangeSetRecord(SQLModel, table=True):
    __tablename__ = "agent_change_sets"

    change_set_id: str = Field(primary_key=True, max_length=128)
    goal_id: str = Field(foreign_key="agent_goal_states.goal_id", index=True, max_length=128)
    session_id: str = Field(
        foreign_key="strategist_chat_sessions.id", index=True, max_length=64
    )
    current_goal_id: Optional[str] = Field(default=None, unique=True, index=True, max_length=128)
    status: str = Field(default="draft", index=True, max_length=32)
    revision: int = Field(default=1, ge=1, index=True)
    idempotency_key: str = Field(index=True, max_length=128)
    correlation_id: str = Field(index=True, max_length=128)
    causation_id: str = Field(index=True, max_length=128)
    snapshot_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AgentChangeSetCommandRecord(SQLModel, table=True):
    __tablename__ = "agent_change_set_commands"
    __table_args__ = (
        UniqueConstraint("session_id", "command_id", name="uq_agent_change_set_command"),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    change_set_id: str = Field(
        foreign_key="agent_change_sets.change_set_id", index=True, max_length=128
    )
    goal_id: str = Field(foreign_key="agent_goal_states.goal_id", index=True, max_length=128)
    session_id: str = Field(
        foreign_key="strategist_chat_sessions.id", index=True, max_length=64
    )
    revision: int = Field(ge=1, index=True)
    command_id: str = Field(index=True, max_length=128)
    command_kind: str = Field(index=True, max_length=32)
    payload_fingerprint: str = Field(max_length=64)
    payload_json: str = Field(sa_column=Column(Text, nullable=False))
    change_set_snapshot_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    goal_snapshot_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
