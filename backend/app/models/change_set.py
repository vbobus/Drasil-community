"""Durable, cross-domain mutation plans for the AI workspace."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, SQLModel


class ChangeSetRecord(SQLModel, table=True):
    """One resumable user goal from interpretation through approval and apply."""

    __tablename__ = "strategist_change_sets"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(
        foreign_key="strategist_chat_sessions.id",
        index=True,
        max_length=64,
    )
    thread_id: Optional[str] = Field(default=None, index=True, max_length=128)
    source_turn_id: Optional[str] = Field(
        default=None,
        foreign_key="strategist_chat_turns.id",
        index=True,
        max_length=64,
    )
    proposal_id: Optional[str] = Field(default=None, index=True, max_length=64)
    status: str = Field(default="draft", index=True, max_length=32)
    outcome: str = Field(default="draft", index=True, max_length=32)
    goal: str = Field(default="", max_length=4000)
    operations_json: str = Field(default="[]")
    constraints_json: str = Field(default="[]")
    validation_json: str = Field(default="{}")
    simulation_json: str = Field(default="{}")
    source_payload_json: str = Field(default="{}")
    failure_json: Optional[str] = Field(default=None)
    revision: int = Field(default=1)
    resume_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
