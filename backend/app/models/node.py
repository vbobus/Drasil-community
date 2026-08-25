"""
Drasil — StrategyNode SQLModel table
-----------------------------------
Architecture rules:
  - PascalCase class name, snake_case columns
  - __tablename__ is plural snake_case
  - Used for Strategy, Task, and Habit nodes
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class StrategyNode(SQLModel, table=True):
    __tablename__ = "strategy_nodes"

    id: Optional[int] = Field(default=None, primary_key=True)
    revision: int = Field(default=1, ge=1, index=True)
    category_id: int = Field(foreign_key="categories.id", index=True)
    title: str = Field(max_length=256)
    notes: Optional[str] = Field(default=None, max_length=4000)
    status: str = Field(default="todo")  # todo | in-progress | done
    priority: str = Field(default="medium")  # low | medium | high
    node_type: str = Field(default="task")  # task | habit (legacy strategy values may still exist)
    parent_id: Optional[int] = Field(default=None, foreign_key="strategy_nodes.id")
    deadline_at: Optional[str] = Field(default=None, max_length=32)
    time_estimate_minutes: Optional[int] = Field(default=None)
    emoji: Optional[str] = Field(default=None, max_length=16)
    tags: Optional[str] = Field(default=None)
    image_attachments: Optional[str] = Field(default=None)
    is_pinned: bool = Field(default=False, index=True)
    sort_order: int = Field(default=0)
    is_backlog: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StrategyNodeIdSequence(SQLModel, table=True):
    """Persistent monotonic allocator preventing deleted task IDs from being reused."""

    __tablename__ = "strategy_node_id_sequences"

    id: int = Field(default=1, primary_key=True)
    last_id: int = Field(default=0)


from app.models.revisioning import register_revisioned_model  # noqa: E402

register_revisioned_model(StrategyNode)
