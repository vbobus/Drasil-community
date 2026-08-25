"""
Persisted Strategist session working-set entities.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, SQLModel


class StrategistSessionEntity(SQLModel, table=True):
    __tablename__ = "strategist_session_entities"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=64)
    session_id: str = Field(foreign_key="strategist_chat_sessions.id", index=True, max_length=64)
    node_id: int = Field(foreign_key="strategy_nodes.id", index=True)
    role: str = Field(default="related", max_length=32, index=True)
    salience: float = Field(default=1.0, index=True)
    mention_count: int = Field(default=1)
    last_mentioned_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    source: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
