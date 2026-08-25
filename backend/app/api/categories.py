"""
Drasil — Category API Router
------------------------------
Endpoints:
  GET    /api/v1/categories         → CategoryRead[]
  POST   /api/v1/categories         → CategoryRead
  PATCH  /api/v1/categories/{id}    → CategoryRead
  DELETE /api/v1/categories/{id}    → 204 No Content

Architecture rules:
  - Routes validate input via Pydantic schemas — zero business logic here.
  - Response schemas use alias_generator for camelCase JSON output.
  - Error format: { "error": { "type": "...", "detail": "..." } }
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, alias_generators, ConfigDict
from sqlalchemy import delete
from sqlmodel import Session, select

from app.db.database import get_session
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import (
    RecurrenceOccurrenceException,
    RecurrenceSeries,
)
from app.models.strategist_session_entities import StrategistSessionEntity
from app.models.strategist_turn_mentions import StrategistTurnMention

router = APIRouter(prefix="/api/v1/categories", tags=["Categories"])


# ── Pydantic Schemas (pure BaseModel avoids SQLModel subclass warnings) ───────

class CategoryRead(BaseModel):
    """Response schema — camelCase via alias_generator for JS conventions."""
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
    id: int
    name: str
    accent_color: str
    icon_slug: str
    sort_order: int


class CategoryCreate(BaseModel):
    """Request schema for creating a new category."""
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )
    name: str
    icon_slug: str = "circle"


class CategoryUpdate(BaseModel):
    """Request schema for patching a category — all fields optional."""
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )
    name: Optional[str] = None
    accent_color: Optional[str] = None
    icon_slug: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[CategoryRead])
def list_categories(session: Session = Depends(get_session)) -> list[Category]:
    """Return all categories ordered by sort_order."""
    return session.exec(select(Category).order_by(Category.sort_order)).all()


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    body: CategoryCreate, session: Session = Depends(get_session)
) -> Category:
    """Create a new category workspace."""
    existing = session.exec(
        select(Category).order_by(Category.sort_order.desc())
    ).first()
    next_order = (existing.sort_order + 1) if existing else 0

    category = Category(name=body.name, icon_slug=body.icon_slug, sort_order=next_order)
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


@router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: int,
    body: CategoryUpdate,
    session: Session = Depends(get_session),
) -> Category:
    """Rename or recolor a category workspace."""
    category = session.get(Category, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"type": "NotFound", "detail": f"Category {category_id} not found."}},
        )
    if body.name is not None:
        category.name = body.name
    if body.accent_color is not None:
        category.accent_color = body.accent_color
    if body.icon_slug is not None:
        category.icon_slug = body.icon_slug
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int, session: Session = Depends(get_session)
) -> None:
    """Delete a category workspace."""
    category = session.get(Category, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"type": "NotFound", "detail": f"Category {category_id} not found."}},
        )
    node_ids = list(
        session.exec(
            select(StrategyNode.id).where(StrategyNode.category_id == category_id)
        ).all()
    )
    recurrence_series_ids = list(
        session.exec(
            select(RecurrenceSeries.id).where(
                RecurrenceSeries.category_id == category_id
            )
        ).all()
    )

    if recurrence_series_ids:
        session.exec(
            delete(RecurrenceOccurrenceException).where(
                RecurrenceOccurrenceException.series_id.in_(recurrence_series_ids)
            )
        )
    session.exec(delete(CalendarBlock).where(CalendarBlock.category_id == category_id))
    session.exec(
        delete(RecurrenceSeries).where(RecurrenceSeries.category_id == category_id)
    )
    if node_ids:
        session.exec(
            delete(StrategistSessionEntity).where(
                StrategistSessionEntity.node_id.in_(node_ids)
            )
        )
        session.exec(
            delete(StrategistTurnMention).where(
                StrategistTurnMention.node_id.in_(node_ids)
            )
        )
    session.exec(delete(StrategyNode).where(StrategyNode.category_id == category_id))
    session.delete(category)
    session.commit()
