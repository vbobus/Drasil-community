"""
Drasil — Category SQLModel table
---------------------------------
Architecture rules enforced here:
  - PascalCase class name, snake_case columns
  - __tablename__ is plural snake_case
  - No camelCase in this file — conversion is handled by CategoryRead schema
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Category(SQLModel, table=True):
    __tablename__ = "categories"

    id: Optional[int] = Field(default=None, primary_key=True)
    revision: int = Field(default=1, ge=1, index=True)
    name: str = Field(index=True, max_length=64)
    accent_color: str = Field(default="#6366f1", max_length=16)  # CSS hex
    icon_slug: str = Field(default="circle", max_length=32)      # icon key
    sort_order: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


from app.models.revisioning import register_revisioned_model  # noqa: E402

register_revisioned_model(Category)
