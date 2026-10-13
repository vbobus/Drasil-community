"""Account-local state for the outbound-only personal companion worker."""
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class CompanionSettings(SQLModel, table=True):
    __tablename__ = "companion_settings"
    id: int = Field(default=1, primary_key=True)
    enabled: bool = False
    time_zone: str = "America/Vancouver"
    check_in_hours: str = "9,18"
    daily_run_limit: int = 6
    personal_context: str = Field(default="", sa_column=Column(Text, nullable=False))
    worker_token_hash: str = ""
    last_seen_at: Optional[datetime] = None
    worker_status: str = "not_connected"
    memory_status: str = "not_connected"


class CompanionRun(SQLModel, table=True):
    __tablename__ = "companion_runs"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    schedule_key: Optional[str] = Field(default=None, unique=True, index=True)
    kind: str = "message"
    prompt: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default="queued", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    lease_hash: str = ""
    lease_until: Optional[datetime] = None
    result_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    error: Optional[str] = None
    dismissed: bool = False
