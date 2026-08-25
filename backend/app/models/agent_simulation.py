"""Immutable exact-revision Agent Kernel simulation results."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentSimulationRecord(SQLModel, table=True):
    __tablename__ = "agent_simulations"
    __table_args__ = (
        UniqueConstraint("session_id", "command_id", name="uq_agent_simulation_command"),
    )

    simulation_id: str = Field(primary_key=True, max_length=128)
    session_id: str = Field(
        foreign_key="strategist_chat_sessions.id", index=True, max_length=64
    )
    goal_id: str = Field(foreign_key="agent_goal_states.goal_id", index=True, max_length=128)
    goal_revision: int = Field(ge=1, index=True)
    resulting_goal_revision: int | None = Field(default=None, ge=1, index=True)
    change_set_id: str = Field(
        foreign_key="agent_change_sets.change_set_id", index=True, max_length=128
    )
    change_set_revision: int = Field(ge=1, index=True)
    command_id: str = Field(index=True, max_length=128)
    payload_fingerprint: str = Field(max_length=64)
    input_snapshot_fingerprint: str = Field(default="", index=True, max_length=80)
    status: str = Field(index=True, max_length=32)
    change_set_snapshot_json: str = Field(sa_column=Column(Text, nullable=False))
    result_json: str = Field(default="", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AgentSimulationEventRecord(SQLModel, table=True):
    __tablename__ = "agent_simulation_events"
    __table_args__ = (
        UniqueConstraint(
            "simulation_id", "event_kind", name="uq_agent_simulation_event_kind"
        ),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    simulation_id: str = Field(
        foreign_key="agent_simulations.simulation_id", index=True, max_length=128
    )
    session_id: str = Field(index=True, max_length=64)
    event_kind: str = Field(index=True, max_length=48)
    correlation_id: str = Field(index=True, max_length=128)
    causation_id: str = Field(index=True, max_length=128)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
