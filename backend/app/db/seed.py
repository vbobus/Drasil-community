"""
Drasil — Database Seed
------------------------
Seeds default category workspaces on first launch.
Idempotent: checks if categories table is empty before inserting.
Never drops or re-seeds existing data.
"""

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.db.database import get_active_engine
from app.models.category import Category

DEFAULT_CATEGORIES = [
    {"name": "Work",            "accent_color": "#6366f1", "icon_slug": "briefcase",  "sort_order": 0},
    {"name": "Health",          "accent_color": "#10b981", "icon_slug": "heart",      "sort_order": 1},
    {"name": "Home",            "accent_color": "#f59e0b", "icon_slug": "home",       "sort_order": 2},
    {"name": "Finance",         "accent_color": "#0ea5e9", "icon_slug": "dollar",     "sort_order": 3},
    {"name": "Social",          "accent_color": "#ec4899", "icon_slug": "users",      "sort_order": 4},
    {"name": "Personal Growth", "accent_color": "#8b5cf6", "icon_slug": "trending",   "sort_order": 5},
    {"name": "Leisure",         "accent_color": "#14b8a6", "icon_slug": "coffee",     "sort_order": 6},
]


def seed_default_categories(bind: Engine | None = None) -> None:
    """Insert the 7 default categories if the table is empty."""
    with Session(bind or get_active_engine()) as session:
        existing = session.exec(select(Category)).first()
        if existing:
            return  # Already seeded — do nothing

        for data in DEFAULT_CATEGORIES:
            session.add(Category(**data))
        session.commit()
