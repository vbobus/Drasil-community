"""
Drasil - Nodes API Router
-------------------------
Endpoints:
  GET    /api/v1/nodes?categoryId={id}   -> StrategyNodeRead[]
  POST   /api/v1/nodes                   -> StrategyNodeRead
  PATCH  /api/v1/nodes/{id}              -> StrategyNodeRead
  POST   /api/v1/nodes/{id}/move         -> StrategyNodeRead
  DELETE /api/v1/nodes/{id}              -> 204 No Content
"""

import json
from datetime import date
from typing import Any, Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field, alias_generators, ConfigDict, field_validator
from sqlmodel import Session, select

from app.db.database import get_session
from app.models.node import StrategyNode
from app.services.task_lifecycle import allocate_strategy_node_id, delete_task_dependents
from app.services.recurrence_rule_service import (
    RecurrenceRuleView,
    SQL_RECURRENCE_RULE_APPLICATION_SERVICE,
)

router = APIRouter(prefix="/api/v1/nodes", tags=["Nodes"])

VALID_PRIORITIES = {"low", "medium", "high"}
VALID_STATUSES = {"todo", "in-progress", "done"}


class NodeImageAttachment(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    name: str
    data_url: str

    @field_validator("id", "name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("attachment id and name are required")
        return trimmed

    @field_validator("data_url")
    @classmethod
    def validate_image_data_url(cls, value: str) -> str:
        if not value.startswith("data:image/"):
            raise ValueError("image attachments must use image data URLs")
        return value


def normalize_tags(value: Any) -> List[str]:
    if value in (None, ""):
        return []

    parsed = value
    if isinstance(value, str):
        parsed = json.loads(value)

    if not isinstance(parsed, list):
        raise ValueError("tags must be a list")

    normalized: List[str] = []
    for tag in parsed:
        if not isinstance(tag, str):
            raise ValueError("tags must contain strings")
        trimmed = tag.strip()
        if trimmed and trimmed not in normalized:
            normalized.append(trimmed)
    return normalized


def encode_tags(tags: List[str]) -> Optional[str]:
    normalized = normalize_tags(tags)
    return json.dumps(normalized) if normalized else None


def normalize_image_attachments(value: Any) -> List[dict[str, str]]:
    if value in (None, ""):
        return []

    parsed = value
    if isinstance(value, str):
        parsed = json.loads(value)

    if not isinstance(parsed, list):
        raise ValueError("imageAttachments must be a list")

    normalized: List[dict[str, str]] = []
    for attachment in parsed:
        validated = NodeImageAttachment.model_validate(attachment)
        normalized.append(validated.model_dump(by_alias=False))
    return normalized


def encode_image_attachments(attachments: List[NodeImageAttachment]) -> Optional[str]:
    normalized = normalize_image_attachments(
        [attachment.model_dump(by_alias=False) for attachment in attachments]
    )
    return json.dumps(normalized) if normalized else None


class NodeRecurrenceRuleRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    revision: int
    frequency: str
    interval: int
    days_of_week: List[str] = Field(default_factory=list)
    start_date: date
    end_date: Optional[date]
    preferred_start_time: str
    duration_minutes: int
    timezone: Optional[str]


class StrategyNodeRead(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    id: int
    category_id: int
    title: str
    notes: Optional[str]
    status: str
    priority: str
    node_type: str
    parent_id: Optional[int]
    planned_date: Optional[str] = None
    deadline_at: Optional[str]
    time_estimate_minutes: Optional[int]
    emoji: Optional[str]
    tags: List[str] = Field(default_factory=list)
    image_attachments: List[NodeImageAttachment] = Field(default_factory=list)
    is_pinned: bool
    sort_order: int
    is_backlog: bool
    is_recurring: bool = False
    recurrence_rule: Optional[NodeRecurrenceRuleRead] = None

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags(cls, value: Any) -> List[str]:
        return normalize_tags(value)

    @field_validator("image_attachments", mode="before")
    @classmethod
    def parse_image_attachments(cls, value: Any) -> List[dict[str, str]]:
        return normalize_image_attachments(value)


def node_recurrence_rule_read(value: RecurrenceRuleView) -> NodeRecurrenceRuleRead:
    return NodeRecurrenceRuleRead(
        id=value.id,
        revision=value.revision,
        frequency=value.frequency,
        interval=value.interval,
        days_of_week=list(value.days_of_week),
        start_date=value.start_date,
        end_date=value.end_date,
        preferred_start_time=value.preferred_start_time,
        duration_minutes=value.duration_minutes,
        timezone=value.timezone,
    )


class StrategyNodeCreate(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    title: str
    category_id: int
    notes: Optional[str] = None
    status: str = "todo"
    priority: str = "medium"
    node_type: str = "task"
    parent_id: Optional[int] = None
    planned_date: Optional[str] = None
    deadline_at: Optional[str] = None
    time_estimate_minutes: Optional[int] = Field(default=None, gt=0)
    emoji: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    image_attachments: List[NodeImageAttachment] = Field(default_factory=list)
    is_pinned: bool = False
    is_backlog: Optional[bool] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in VALID_STATUSES:
            raise ValueError("status must be todo, in-progress, or done")
        return normalized

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in VALID_PRIORITIES:
            raise ValueError("priority must be low, medium, or high")
        return normalized

    @field_validator("deadline_at", "planned_date")
    @classmethod
    def validate_deadline(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        return date.fromisoformat(value).isoformat()

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, value: Any) -> List[str]:
        return normalize_tags(value)

    @field_validator("image_attachments", mode="before")
    @classmethod
    def validate_image_attachments(cls, value: Any) -> List[dict[str, str]]:
        return normalize_image_attachments(value)


class StrategyNodeUpdate(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    title: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    planned_date: Optional[str] = None
    deadline_at: Optional[str] = None
    time_estimate_minutes: Optional[int] = None
    emoji: Optional[str] = None
    tags: Optional[List[str]] = None
    image_attachments: Optional[List[NodeImageAttachment]] = None
    is_pinned: Optional[bool] = None
    is_backlog: Optional[bool] = None
    node_type: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in VALID_PRIORITIES:
            raise ValueError("priority must be low, medium, or high")
        return normalized

    @field_validator("deadline_at", "planned_date")
    @classmethod
    def validate_deadline(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        return date.fromisoformat(value).isoformat()

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, value: Any) -> Optional[List[str]]:
        if value is None:
            return value
        return normalize_tags(value)

    @field_validator("image_attachments", mode="before")
    @classmethod
    def validate_image_attachments(
        cls, value: Any
    ) -> Optional[List[dict[str, str]]]:
        if value is None:
            return value
        return normalize_image_attachments(value)


class StrategyNodeMove(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    parent_id: Optional[int] = None
    target_index: int
    is_backlog: Optional[bool] = None


def get_node_or_404(session: Session, node_id: int) -> StrategyNode:
    node = session.get(StrategyNode, node_id)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"type": "NotFound", "detail": f"Node {node_id} not found."}},
        )
    return node


def list_siblings(session: Session, category_id: int, parent_id: Optional[int]) -> List[StrategyNode]:
    query = select(StrategyNode).where(StrategyNode.category_id == category_id)
    if parent_id is None:
        query = query.where(StrategyNode.parent_id == None)
    else:
        query = query.where(StrategyNode.parent_id == parent_id)
    return list(session.exec(query.order_by(StrategyNode.sort_order)).all())


def resequence_nodes(session: Session, nodes: List[StrategyNode]) -> None:
    for index, sibling in enumerate(nodes):
        sibling.sort_order = index
        session.add(sibling)


def apply_backlog_to_subtree(
    session: Session,
    node: StrategyNode,
    is_backlog: bool,
) -> None:
    frontier = [node]
    while frontier:
        current = frontier.pop()
        current.is_backlog = is_backlog
        session.add(current)
        children = session.exec(
            select(StrategyNode).where(StrategyNode.parent_id == current.id)
        ).all()
        frontier.extend(children)


def is_descendant(session: Session, node_id: int, candidate_parent_id: int) -> bool:
    frontier = [node_id]
    while frontier:
        current_id = frontier.pop()
        if current_id == candidate_parent_id:
            return True
        children = session.exec(
            select(StrategyNode.id).where(StrategyNode.parent_id == current_id)
        ).all()
        frontier.extend(children)
    return False


@router.get("", response_model=List[StrategyNodeRead])
def list_nodes(
    category_id: int = Query(alias="categoryId"),
    session: Session = Depends(get_session),
) -> List[StrategyNodeRead]:
    return read_workspace_nodes(session, category_id)


def read_workspace_nodes(session: Session, category_id: int | None = None) -> List[StrategyNodeRead]:
    query = select(StrategyNode).order_by(StrategyNode.category_id, StrategyNode.parent_id, StrategyNode.sort_order)
    if category_id is not None:
        query = query.where(StrategyNode.category_id == category_id)
    nodes = list(session.exec(query).all())
    active_rules = SQL_RECURRENCE_RULE_APPLICATION_SERVICE.get_active_for_tasks(
        session,
        task_ids=[int(node.id) for node in nodes if node.id is not None],
    )
    return [
        StrategyNodeRead.model_validate(node).model_copy(
            update={
                "is_recurring": int(node.id) in active_rules,
                "recurrence_rule": (
                    node_recurrence_rule_read(active_rules[int(node.id)])
                    if int(node.id) in active_rules
                    else None
                ),
            }
        )
        for node in nodes
        if node.id is not None
    ]


@router.post("", response_model=StrategyNodeRead, status_code=status.HTTP_201_CREATED)
def create_node(
    body: StrategyNodeCreate,
    session: Session = Depends(get_session),
) -> StrategyNode:
    siblings = list_siblings(session, body.category_id, body.parent_id)
    node_type = "habit" if body.node_type == "habit" else "task"
    is_backlog = body.is_backlog if body.is_backlog is not None else False

    if body.parent_id is not None:
        parent = get_node_or_404(session, body.parent_id)
        if parent.category_id != body.category_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Node cannot be created under a parent from a different category.",
            )
        if body.is_backlog is None:
            is_backlog = parent.is_backlog

    node = StrategyNode(
        id=allocate_strategy_node_id(session),
        title=body.title,
        notes=body.notes,
        status=body.status,
        priority=body.priority,
        category_id=body.category_id,
        node_type=node_type,
        parent_id=body.parent_id,
        planned_date=body.planned_date,
        deadline_at=body.deadline_at,
        time_estimate_minutes=body.time_estimate_minutes,
        emoji=body.emoji,
        tags=encode_tags(body.tags),
        image_attachments=encode_image_attachments(body.image_attachments),
        is_pinned=body.is_pinned,
        sort_order=len(siblings),
        is_backlog=is_backlog,
    )
    session.add(node)
    session.commit()
    session.refresh(node)
    return node


@router.patch("/{node_id}", response_model=StrategyNodeRead)
def update_node(
    node_id: int,
    body: StrategyNodeUpdate,
    session: Session = Depends(get_session),
) -> StrategyNode:
    node = get_node_or_404(session, node_id)

    if body.title is not None:
        node.title = body.title
    if body.notes is not None:
        node.notes = body.notes
    if body.status is not None:
        node.status = body.status
    if body.priority is not None:
        node.priority = body.priority
    if "planned_date" in body.model_fields_set:
        node.planned_date = body.planned_date
    if "deadline_at" in body.model_fields_set:
        node.deadline_at = body.deadline_at
    if "time_estimate_minutes" in body.model_fields_set:
        node.time_estimate_minutes = body.time_estimate_minutes
    if body.emoji is not None:
        node.emoji = body.emoji
    if "tags" in body.model_fields_set:
        node.tags = encode_tags(body.tags or [])
    if "image_attachments" in body.model_fields_set:
        node.image_attachments = encode_image_attachments(body.image_attachments or [])
    if body.is_pinned is not None:
        node.is_pinned = body.is_pinned
    if body.is_backlog is not None:
        apply_backlog_to_subtree(session, node, body.is_backlog)
    if body.node_type is not None:
        node.node_type = "habit" if body.node_type == "habit" else "task"

    session.add(node)
    session.commit()
    session.refresh(node)
    return node


@router.post("/{node_id}/move", response_model=StrategyNodeRead)
def move_node(
    node_id: int,
    body: StrategyNodeMove,
    session: Session = Depends(get_session),
) -> StrategyNode:
    node = get_node_or_404(session, node_id)
    target_parent = None

    if body.parent_id is not None:
        target_parent = get_node_or_404(session, body.parent_id)
        if target_parent.category_id != node.category_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Node cannot be moved to a different category tree.",
            )

    if body.parent_id == node.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Node cannot parent itself.")
    if body.parent_id is not None and is_descendant(session, node.id, body.parent_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Node cannot be moved under one of its descendants.",
        )

    old_siblings = [sibling for sibling in list_siblings(session, node.category_id, node.parent_id) if sibling.id != node.id]
    new_siblings = [sibling for sibling in list_siblings(session, node.category_id, body.parent_id) if sibling.id != node.id]

    target_index = max(0, min(body.target_index, len(new_siblings)))
    old_parent_id = node.parent_id
    node.parent_id = body.parent_id
    new_siblings.insert(target_index, node)

    resolved_is_backlog = body.is_backlog
    if resolved_is_backlog is None and target_parent is not None:
        resolved_is_backlog = target_parent.is_backlog
    if resolved_is_backlog is not None:
        apply_backlog_to_subtree(session, node, resolved_is_backlog)

    if old_parent_id != body.parent_id:
        resequence_nodes(session, old_siblings)
    resequence_nodes(session, new_siblings)
    session.add(node)
    session.commit()
    session.refresh(node)
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    node_id: int,
    session: Session = Depends(get_session),
) -> None:
    node = get_node_or_404(session, node_id)
    deleted_parent_id = node.parent_id
    deleted_category_id = node.category_id
    deleted_node_ids: list[int] = []

    def delete_recursive(parent_id: int) -> None:
        children = session.exec(select(StrategyNode).where(StrategyNode.parent_id == parent_id)).all()
        for child in children:
            delete_recursive(child.id)
            deleted_node_ids.append(int(child.id))
            session.delete(child)

    delete_recursive(node_id)
    deleted_node_ids.append(node_id)
    delete_task_dependents(session, deleted_node_ids)
    session.delete(node)
    session.commit()

    siblings = list_siblings(session, deleted_category_id, deleted_parent_id)
    resequence_nodes(session, siblings)
    session.commit()
