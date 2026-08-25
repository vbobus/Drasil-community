"""
Drasil - Strategist graph runtime persistence tables
----------------------------------------------------
Stores stable LangGraph thread metadata and durable proposal state so Strategist
review actions can resume by hard ID instead of rescanning old turns.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, SQLModel


class StrategistGraphThread(SQLModel, table=True):
    __tablename__ = "strategist_graph_threads"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    chat_session_id: str = Field(
        foreign_key="strategist_chat_sessions.id",
        index=True,
        max_length=64,
        unique=True,
    )
    thread_id: str = Field(default_factory=lambda: str(uuid4()), index=True, max_length=128)
    graph_version: str = Field(default="5.2", max_length=32)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class StrategistProposalRecord(SQLModel, table=True):
    __tablename__ = "strategist_proposal_records"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(foreign_key="strategist_chat_sessions.id", index=True, max_length=64)
    thread_id: Optional[str] = Field(default=None, index=True, max_length=128)
    source_turn_id: Optional[str] = Field(
        default=None,
        foreign_key="strategist_chat_turns.id",
        index=True,
        max_length=64,
    )
    change_set_id: Optional[str] = Field(
        default=None,
        foreign_key="strategist_change_sets.id",
        index=True,
        max_length=64,
    )
    proposal_kind: str = Field(max_length=32, index=True)
    status: str = Field(default="pending", max_length=32, index=True)
    target_node_id: Optional[int] = Field(default=None, index=True)
    target_node_ids_json: Optional[str] = Field(default=None)
    payload_json: str = Field(max_length=16000)
    result_json: Optional[str] = Field(default=None, max_length=16000)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
