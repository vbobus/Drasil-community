"""
Drasil - CalendarBlock SQLModel table
-------------------------------------
Accepted calendar blocks for the execution surface.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class CalendarBlock(SQLModel, table=True):
    __tablename__ = "calendar_blocks"
    __table_args__ = (
        Index(
            "uq_calendar_blocks_recurrence_identity",
            "recurrence_series_id",
            "recurrence_key",
            unique=True,
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    revision: int = Field(default=1, ge=1, index=True)
    task_id: int = Field(foreign_key="strategy_nodes.id", index=True)
    category_id: int = Field(foreign_key="categories.id", index=True)
    start_at: datetime = Field(index=True)
    end_at: datetime = Field(index=True)
    variant: str = Field(default="scheduled", max_length=24, index=True)
    completion_status: str = Field(default="not_started", max_length=24)
    is_pinned: bool = Field(default=False, index=True)
    is_protected: bool = Field(default=False, index=True)
    is_recurring: bool = Field(default=False, index=True)
    recurrence_series_id: Optional[str] = Field(
        default=None,
        index=True,
        max_length=64,
    )
    recurrence_key: Optional[str] = Field(default=None, index=True, max_length=64)
    google_event_id: Optional[str] = Field(default=None, max_length=256, index=True)
    google_event_etag: Optional[str] = Field(default=None, max_length=256)
    google_last_synced_at: Optional[datetime] = Field(default=None)
    google_sync_error: Optional[str] = Field(default=None, max_length=2048)
    created_at: datetime = Field(default_factory=datetime.utcnow)


from app.models.revisioning import register_revisioned_model  # noqa: E402

register_revisioned_model(CalendarBlock)
