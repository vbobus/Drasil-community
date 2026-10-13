"""
Persisted Strategist agent-kernel events.

The agent kernel uses this as its continuity source. Provider-side session IDs
are treated as optional transport state, not as the source of truth.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class StrategistAgentEvent(SQLModel, table=True):
    __tablename__ = "strategist_agent_events"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(foreign_key="strategist_chat_sessions.id", index=True, max_length=64)
    turn_id: Optional[str] = Field(
        default=None,
        foreign_key="strategist_chat_turns.id",
        index=True,
        max_length=64,
    )
    correlation_id: str = Field(default_factory=lambda: str(uuid4()), index=True, max_length=64)
    causation_id: Optional[str] = Field(default=None, index=True, max_length=128)
    goal_id: Optional[str] = Field(default=None, index=True, max_length=128)
    goal_revision: Optional[int] = Field(default=None, index=True)
    run_id: Optional[str] = Field(default=None, index=True, max_length=64)
    sequence: int = Field(default=0, index=True)
    event_type: str = Field(max_length=64, index=True)
    source: str = Field(default="agent_kernel", max_length=64, index=True)
    # Full review/simulation events exceed 32k; preserve the continuity JSON.
    payload_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class StrategistRun(SQLModel, table=True):
    __tablename__ = "strategist_runs"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(foreign_key="strategist_chat_sessions.id", index=True, max_length=64)
    turn_id: str = Field(foreign_key="strategist_chat_turns.id", index=True, max_length=64)
    status: str = Field(default="queued", max_length=32, index=True)
    control_plane: Optional[str] = Field(default=None, max_length=64, index=True)
    goal_id: Optional[str] = Field(default=None, index=True, max_length=128)
    goal_revision: Optional[int] = Field(default=None, index=True)
    correlation_id: str = Field(default_factory=lambda: str(uuid4()), index=True, max_length=64)
    causation_id: Optional[str] = Field(default=None, index=True, max_length=128)
    worker_token: Optional[str] = Field(default=None, index=True, max_length=64)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    prompt: str = Field(max_length=4000)
    request_json: str = Field(max_length=32000)
    error_text: Optional[str] = Field(default=None, max_length=4000)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    started_at: Optional[datetime] = Field(default=None, index=True)
    completed_at: Optional[datetime] = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
