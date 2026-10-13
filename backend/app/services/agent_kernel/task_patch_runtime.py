"""Deterministic TaskPatch application runtime."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.api.nodes import (
    StrategyNodeCreate,
    StrategyNodeUpdate,
    apply_backlog_to_subtree,
    encode_image_attachments,
    encode_tags,
    is_descendant,
    list_siblings,
    resequence_nodes,
)
from app.models.node import StrategyNode
from app.models.category import Category
from app.models.calendar_block import CalendarBlock
from app.models.recurrence_series import RecurrenceSeries
from app.services.agent_kernel.task_patch import validate_task_patch
from app.services.retrieval import decode_tags
from app.services.task_lifecycle import allocate_strategy_node_id, delete_task_dependents


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _resolve_parent_id(
    operation: dict[str, Any],
    temp_nodes: dict[str, StrategyNode],
) -> int | None:
    parent_id = _as_int(operation.get("parent_id"))
    if parent_id is not None:
        return parent_id
    parent_temp_id = operation.get("parent_temp_id")
    if isinstance(parent_temp_id, str) and parent_temp_id.strip():
        parent = temp_nodes.get(parent_temp_id)
        if parent is None or parent.id is None:
            raise ValueError("TaskPatch references a temporary parent before it is created.")
        return int(parent.id)
    return None


def _delete_node_without_commit(
    session: Session,
    *,
    node_id: int,
) -> list[int]:
    node = session.get(StrategyNode, node_id)
    if node is None:
        return []
    deleted_parent_id = node.parent_id
    deleted_category_id = node.category_id
    deleted_node_ids: list[int] = []

    def delete_recursive(parent_id: int) -> None:
        children = session.exec(select(StrategyNode).where(StrategyNode.parent_id == parent_id)).all()
        for child in children:
            if child.id is not None:
                delete_recursive(int(child.id))
                deleted_node_ids.append(int(child.id))
            session.delete(child)

    delete_recursive(node_id)
    deleted_node_ids.append(node_id)
    delete_task_dependents(session, deleted_node_ids)
    session.delete(node)
    session.flush()
    siblings = list_siblings(session, deleted_category_id, deleted_parent_id)
    resequence_nodes(session, siblings)
    return deleted_node_ids


def _apply_update_without_commit(
    session: Session,
    *,
    node: StrategyNode,
    fields: dict[str, Any],
) -> StrategyNode:
    body = StrategyNodeUpdate.model_validate(fields)
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
    return node


def _archive_node_without_commit(
    session: Session,
    *,
    node: StrategyNode,
) -> StrategyNode:
    frontier = [node]
    while frontier:
        current = frontier.pop()
        tags = decode_tags(current.tags)
        if "state:archived" not in [tag.lower() for tag in tags]:
            tags.append("state:archived")
        current.tags = encode_tags(tags)
        session.add(current)
        if current.id is not None:
            frontier.extend(
                session.exec(
                    select(StrategyNode).where(StrategyNode.parent_id == current.id)
                ).all()
            )
    return node


def _apply_category_to_subtree(
    session: Session,
    *,
    node: StrategyNode,
    category_id: int,
) -> list[int]:
    moved_ids: list[int] = []
    frontier = [node]
    while frontier:
        current = frontier.pop()
        current.category_id = category_id
        session.add(current)
        if current.id is None:
            continue
        moved_ids.append(int(current.id))
        frontier.extend(
            session.exec(
                select(StrategyNode).where(StrategyNode.parent_id == current.id)
            ).all()
        )
    if moved_ids:
        blocks = session.exec(
            select(CalendarBlock).where(CalendarBlock.task_id.in_(moved_ids))
        ).all()
        for block in blocks:
            block.category_id = category_id
            session.add(block)
        series_rows = session.exec(
            select(RecurrenceSeries).where(RecurrenceSeries.task_id.in_(moved_ids))
        ).all()
        for series in series_rows:
            series.category_id = category_id
            session.add(series)
    return moved_ids


def _create_node_without_commit(
    session: Session,
    *,
    operation: dict[str, Any],
    temp_nodes: dict[str, StrategyNode],
) -> StrategyNode:
    parent_id = _resolve_parent_id(operation, temp_nodes)
    parent = session.get(StrategyNode, parent_id) if parent_id is not None else None
    if parent_id is not None and parent is None:
        raise ValueError("TaskPatch create operation references missing parent task.")
    category_id = _as_int(operation.get("category_id"))
    if category_id is None and parent is not None:
        category_id = parent.category_id
    if category_id is None:
        raise ValueError("TaskPatch create operation is missing category_id.")
    fields = operation.get("fields") if isinstance(operation.get("fields"), dict) else {}
    body_payload = {
        **fields,
        "title": str(operation.get("title") or "").strip(),
        "category_id": category_id,
        "parent_id": parent_id,
    }
    body = StrategyNodeCreate.model_validate(body_payload)
    siblings = list_siblings(session, body.category_id, body.parent_id)
    node = StrategyNode(
        id=allocate_strategy_node_id(session),
        title=body.title,
        notes=body.notes,
        status=body.status,
        priority=body.priority,
        category_id=body.category_id,
        node_type="habit" if body.node_type == "habit" else "task",
        parent_id=body.parent_id,
        planned_date=body.planned_date,
        deadline_at=body.deadline_at,
        time_estimate_minutes=body.time_estimate_minutes,
        emoji=body.emoji,
        tags=encode_tags(body.tags),
        image_attachments=encode_image_attachments(body.image_attachments),
        is_pinned=body.is_pinned,
        sort_order=len(siblings),
        is_backlog=(
            body.is_backlog
            if body.is_backlog is not None
            else bool(parent.is_backlog) if parent is not None else False
        ),
    )
    session.add(node)
    session.flush()
    temp_id = operation.get("temp_id")
    if isinstance(temp_id, str) and temp_id.strip():
        temp_nodes[temp_id] = node
    target_index = _as_int(operation.get("target_index"))
    if target_index is not None:
        new_siblings = [
            sibling
            for sibling in list_siblings(session, node.category_id, node.parent_id)
            if sibling.id != node.id
        ]
        new_siblings.insert(max(0, min(target_index, len(new_siblings))), node)
        resequence_nodes(session, new_siblings)
    return node


def _move_node_without_commit(
    session: Session,
    *,
    operation: dict[str, Any],
    temp_nodes: dict[str, StrategyNode],
) -> StrategyNode:
    task_id = _as_int(operation.get("task_id"))
    if task_id is None:
        raise ValueError("TaskPatch move operation is missing task_id.")
    node = session.get(StrategyNode, task_id)
    if node is None:
        raise ValueError(f"TaskPatch move operation references missing task {task_id}.")
    destination_parent_id = _resolve_parent_id(operation, temp_nodes)
    destination_parent = (
        session.get(StrategyNode, destination_parent_id)
        if destination_parent_id is not None
        else None
    )
    if destination_parent_id is not None and destination_parent is None:
        raise ValueError("TaskPatch move operation references missing destination task.")
    destination_category_id = _as_int(operation.get("category_id"))
    if destination_category_id is None and destination_parent is not None:
        destination_category_id = int(destination_parent.category_id)
    if destination_category_id is None:
        destination_category_id = int(node.category_id)
    if session.get(Category, destination_category_id) is None:
        raise ValueError("TaskPatch move operation references missing destination category.")
    if destination_parent is not None:
        if destination_parent.category_id != destination_category_id:
            raise ValueError("TaskPatch move destination parent is in a different category.")
        if destination_parent.id == node.id:
            raise ValueError("TaskPatch move operation would parent a task to itself.")
        if node.id is not None and is_descendant(session, int(node.id), int(destination_parent.id)):
            raise ValueError("TaskPatch move operation would move a task under one of its descendants.")
    old_category_id = int(node.category_id)
    old_siblings = [
        sibling
        for sibling in list_siblings(session, old_category_id, node.parent_id)
        if sibling.id != node.id
    ]
    new_siblings = [
        sibling
        for sibling in list_siblings(session, destination_category_id, destination_parent_id)
        if sibling.id != node.id
    ]
    old_parent_id = node.parent_id
    node.parent_id = destination_parent_id
    if destination_category_id != old_category_id:
        _apply_category_to_subtree(
            session,
            node=node,
            category_id=destination_category_id,
        )
    if destination_parent is not None:
        apply_backlog_to_subtree(session, node, destination_parent.is_backlog)
    target_index = _as_int(operation.get("target_index"))
    if target_index is None:
        target_index = len(new_siblings)
    new_siblings.insert(max(0, min(target_index, len(new_siblings))), node)
    if old_parent_id != destination_parent_id or old_category_id != destination_category_id:
        resequence_nodes(session, old_siblings)
    resequence_nodes(session, new_siblings)
    session.add(node)
    return node


def apply_task_patch(
    session: Session,
    *,
    task_patch: dict[str, Any],
    commit: bool = True,
) -> dict[str, Any]:
    nodes = list(session.exec(select(StrategyNode)).all())
    category_ids = {
        int(category_id)
        for category_id in session.exec(select(Category.id)).all()
        if isinstance(category_id, int)
    }
    validation = validate_task_patch(task_patch, nodes, category_ids=category_ids)
    if validation["errors"]:
        raise ValueError("TaskPatch is invalid: " + "; ".join(validation["errors"]))

    operations = task_patch.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("TaskPatch has no operations.")

    temp_nodes: dict[str, StrategyNode] = {}
    affected_nodes: list[StrategyNode] = []
    created_nodes: list[StrategyNode] = []
    updated_nodes: list[StrategyNode] = []
    moved_nodes: list[StrategyNode] = []
    deleted_node_ids: list[int] = []

    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("TaskPatch operation must be an object.")
        op = str(operation.get("op") or "").strip().lower()
        if op == "create":
            node = _create_node_without_commit(session, operation=operation, temp_nodes=temp_nodes)
            created_nodes.append(node)
            affected_nodes.append(node)
        elif op == "update":
            task_id = _as_int(operation.get("task_id"))
            fields = operation.get("fields")
            if task_id is None or not isinstance(fields, dict):
                raise ValueError("TaskPatch update operation is missing task_id or fields.")
            node = session.get(StrategyNode, task_id)
            if node is None:
                raise ValueError(f"TaskPatch update operation references missing task {task_id}.")
            updated = _apply_update_without_commit(session, node=node, fields=fields)
            updated_nodes.append(updated)
            affected_nodes.append(updated)
        elif op == "move":
            moved = _move_node_without_commit(session, operation=operation, temp_nodes=temp_nodes)
            moved_nodes.append(moved)
            affected_nodes.append(moved)
        elif op == "delete":
            task_id = _as_int(operation.get("task_id"))
            if task_id is None:
                raise ValueError("TaskPatch delete operation is missing task_id.")
            node = session.get(StrategyNode, task_id)
            if node is None:
                continue
            mode = str(operation.get("mode") or "hard_delete").strip().lower()
            if mode == "archive":
                archived = _archive_node_without_commit(session, node=node)
                updated_nodes.append(archived)
                affected_nodes.append(archived)
                continue
            children = session.exec(select(StrategyNode).where(StrategyNode.parent_id == task_id)).all()
            subtree_policy = str(operation.get("subtree_policy") or "").strip().lower()
            if children and subtree_policy != "include_children":
                raise ValueError("TaskPatch delete operation targets a task with children.")
            deleted_node_ids.extend(_delete_node_without_commit(session, node_id=task_id))
        else:
            raise ValueError(f"Unsupported TaskPatch operation: {op or 'unknown'}.")

    if commit:
        session.commit()
    else:
        session.flush()

    refreshed_nodes: list[StrategyNode] = []
    seen_ids: set[int] = set()
    for node in affected_nodes:
        if node.id is None or node.id in deleted_node_ids or node.id in seen_ids:
            continue
        session.refresh(node)
        refreshed_nodes.append(node)
        seen_ids.add(int(node.id))

    return {
        "affected_nodes": refreshed_nodes,
        "created_nodes": [node for node in refreshed_nodes if any(node.id == created.id for created in created_nodes)],
        "updated_nodes": [node for node in refreshed_nodes if any(node.id == updated.id for updated in updated_nodes)],
        "moved_nodes": [node for node in refreshed_nodes if any(node.id == moved.id for moved in moved_nodes)],
        "deleted_node_ids": deleted_node_ids,
        "temp_node_ids": {
            temp_id: int(node.id)
            for temp_id, node in temp_nodes.items()
            if node.id is not None
        },
        "validation": validation,
    }
