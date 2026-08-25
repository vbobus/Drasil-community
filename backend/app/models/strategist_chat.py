"""
Drasil - Strategist chat persistence tables
------------------------------------------
Stores chat sessions and turn history for the Strategist AI panel.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, SQLModel


class StrategistChatSession(SQLModel, table=True):
    __tablename__ = "strategist_chat_sessions"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    title: str = Field(default="New strategist chat", max_length=160)
    provider: str = Field(default="codex-cli", max_length=32, index=True)
    provider_label: str = Field(default="ChatGPT login", max_length=64)
    model_name: Optional[str] = Field(default=None, max_length=80)
    provider_session_id: Optional[str] = Field(default=None, max_length=128, index=True)
    owner_principal_id: Optional[str] = Field(default=None, max_length=128, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class StrategistChatTurn(SQLModel, table=True):
    __tablename__ = "strategist_chat_turns"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(foreign_key="strategist_chat_sessions.id", index=True, max_length=64)
    prompt: str = Field(max_length=4000)
    response_json: Optional[str] = Field(default=None)
    error_text: Optional[str] = Field(default=None, max_length=4000)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
