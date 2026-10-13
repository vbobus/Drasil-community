"""
Drasil - External calendar integration state.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from sqlalchemy import Column
from app.db.encrypted import PrivateToken


class CalendarIntegration(SQLModel, table=True):
    __tablename__ = "calendar_integrations"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(default="google", max_length=32, index=True)
    is_connected: bool = Field(default=False, index=True)
    google_calendar_id: Optional[str] = Field(default=None, max_length=256, index=True)
    google_calendar_summary: str = Field(default="Drasil", max_length=128)
    google_account_email: Optional[str] = Field(default=None, max_length=256)
    time_zone: str = Field(default="UTC", max_length=64)
    access_token: Optional[str] = Field(default=None, sa_column=Column(PrivateToken()))
    refresh_token: Optional[str] = Field(default=None, sa_column=Column(PrivateToken()))
    token_type: Optional[str] = Field(default=None, max_length=64)
    scope: Optional[str] = Field(default=None, max_length=1024)
    expires_at: Optional[datetime] = Field(default=None)
    oauth_state: Optional[str] = Field(default=None, max_length=256, index=True)
    oauth_redirect_uri: Optional[str] = Field(default=None, max_length=1024)
    sync_token: Optional[str] = Field(default=None, max_length=2048)
    last_synced_at: Optional[datetime] = Field(default=None)
    last_sync_error: Optional[str] = Field(default=None, max_length=2048)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
