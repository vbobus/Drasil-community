"""Durable Agent Kernel GoalState snapshots and transition audit records."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentGoalStateRecord(SQLModel, table=True):
    __tablename__ = "agent_goal_states"

    goal_id: str = Field(primary_key=True, max_length=128)
    session_id: str = Field(
        foreign_key="strategist_chat_sessions.id",
        index=True,
        max_length=64,
    )
    status: str = Field(index=True, max_length=32)
    revision: int = Field(default=1, ge=1, index=True)
    active_session_id: Optional[str] = Field(default=None, unique=True, index=True, max_length=64)
    current_run_id: Optional[str] = Field(default=None, index=True, max_length=64)
    correlation_id: str = Field(index=True, max_length=128)
    causation_id: str = Field(index=True, max_length=128)
    state_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AgentGoalTransitionRecord(SQLModel, table=True):
    __tablename__ = "agent_goal_transitions"
    __table_args__ = (
        UniqueConstraint("session_id", "command_id", name="uq_agent_goal_transition_command"),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    goal_id: str = Field(foreign_key="agent_goal_states.goal_id", index=True, max_length=128)
    session_id: str = Field(
        foreign_key="strategist_chat_sessions.id",
        index=True,
        max_length=64,
    )
    revision: int = Field(ge=1, index=True)
    command_id: str = Field(index=True, max_length=128)
    transition_kind: str = Field(index=True, max_length=64)
    payload_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    state_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
