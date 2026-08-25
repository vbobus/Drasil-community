"""
Persisted Strategist turn-level mention resolution records.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, SQLModel


class StrategistTurnMention(SQLModel, table=True):
    __tablename__ = "strategist_turn_mentions"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(foreign_key="strategist_chat_sessions.id", index=True, max_length=64)
    turn_id: str = Field(foreign_key="strategist_chat_turns.id", index=True, max_length=64)
    node_id: Optional[int] = Field(default=None, foreign_key="strategy_nodes.id", index=True)
    role: str = Field(default="related", max_length=32, index=True)
    mention_text: str = Field(default="", max_length=256)
    confidence: float = Field(default=0.0)
    source: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
