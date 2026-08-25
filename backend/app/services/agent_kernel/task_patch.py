"""TaskPatch draft helpers for the Strategist agent kernel.

The TaskPatch shape is an internal IR: the model may draft intent, but Drasil
still validates, renders, persists, and applies approved changes.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.models.node import StrategyNode


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _nodes_by_id(nodes: list[StrategyNode]) -> dict[int, StrategyNode]:
    return {int(node.id): node for node in nodes if isinstance(node.id, int)}


def _children_by_parent(nodes: list[StrategyNode]) -> dict[int | None, list[int]]:
    children: dict[int | None, list[int]] = {}
    for node in nodes:
        node_id = _as_int(node.id)
        if node_id is None:
            continue
        children.setdefault(node.parent_id, []).append(node_id)
    return children


def _descendant_ids(
    node_id: int,
    children_by_parent: dict[int | None, list[int]],
) -> list[int]:
    descendants: list[int] = []
    seen: set[int] = set()

    def visit(parent_id: int) -> None:
        for child_id in children_by_parent.get(parent_id, []):
            if child_id in seen:
                continue
            seen.add(child_id)
            descendants.append(child_id)
            visit(child_id)

    visit(node_id)
    return descendants


def _append_unique_int(target: list[int], value: Any) -> None:
    node_id = _as_int(value)
    if node_id is not None and node_id not in target:
        target.append(node_id)


def _proposal_target_ids(proposal: dict[str, Any]) -> list[int]:
    target_ids: list[int] = []
    raw_target_updates = proposal.get("target_updates", proposal.get("targetUpdates"))
    if isinstance(raw_target_updates, list):
        for item in raw_target_updates:
            if not isinstance(item, dict):
                continue
            _append_unique_int(target_ids, item.get("target_node_id", item.get("targetNodeId")))

    raw_target_ids = proposal.get("target_node_ids", proposal.get("targetNodeIds"))
    if isinstance(raw_target_ids, list):
        for node_id in raw_target_ids:
            _append_unique_int(target_ids, node_id)

    target_node_id = proposal.get("target_node_id", proposal.get("targetNodeId"))
    if _as_int(target_node_id) is not None and target_node_id not in target_ids:
        target_ids.insert(0, int(target_node_id))

    structural_operations = proposal.get("structural_operations")
    if isinstance(structural_operations, list):
        for operation in structural_operations:
            if isinstance(operation, dict):
                _append_unique_int(target_ids, operation.get("node_id"))
                _append_unique_int(target_ids, operation.get("target_node_id"))

    return target_ids


def _metadata_target_updates(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    raw_target_updates = proposal.get("target_updates", proposal.get("targetUpdates"))
    if not isinstance(raw_target_updates, list):
        return []
    target_updates: list[dict[str, Any]] = []
    for item in raw_target_updates:
        if not isinstance(item, dict):
            continue
        node_id = _as_int(item.get("target_node_id", item.get("targetNodeId")))
        updates = item.get("updates")
        if node_id is not None and isinstance(updates, dict) and updates:
            target_updates.append({"target_node_id": node_id, "updates": updates})
    return target_updates


def _fields_from_structural_create(operation: dict[str, Any]) -> dict[str, Any]:
    field_keys = (
        "notes",
        "priority",
        "status",
        "deadline_at",
        "time_estimate_minutes",
        "emoji",
        "tags",
        "image_attachments",
        "is_pinned",
        "is_backlog",
        "node_type",
    )
    return {key: operation[key] for key in field_keys if key in operation}


def _operation_from_structural_operation(operation: dict[str, Any]) -> dict[str, Any]:
    kind = str(operation.get("kind") or "").strip()
    if kind == "create_node":
        return _compact_dict(
            {
                "op": "create",
                "legacy_kind": kind,
                "title": str(operation.get("title") or "").strip(),
                "category_id": _as_int(operation.get("category_id")),
                "parent_id": _as_int(operation.get("parent_id")),
                "parent_temp_id": operation.get("parent_temp_id")
                if isinstance(operation.get("parent_temp_id"), str)
                else None,
                "temp_id": operation.get("temp_id")
                if isinstance(operation.get("temp_id"), str)
                else None,
                "target_index": _as_int(operation.get("target_index")),
                "fields": _fields_from_structural_create(operation),
            }
        )
    if kind == "move_subtree":
        return _compact_dict(
            {
                "op": "move",
                "legacy_kind": kind,
                "task_id": _as_int(operation.get("node_id")),
                "parent_id": _as_int(operation.get("destination_parent_id")),
                "category_id": _as_int(operation.get("destination_category_id")),
                "category_name": operation.get("destination_category_name"),
                "parent_temp_id": operation.get("destination_parent_temp_id")
                if isinstance(operation.get("destination_parent_temp_id"), str)
                else None,
                "target_index": _as_int(operation.get("target_index")),
                "node_title": operation.get("node_title"),
                "destination_parent_title": operation.get("destination_parent_title"),
            }
        )
    if kind == "archive_node":
        return _compact_dict(
            {
                "op": "delete",
                "legacy_kind": kind,
                "task_id": _as_int(operation.get("node_id")),
                "mode": "archive",
                "subtree_policy": "include_children",
                "node_title": operation.get("node_title"),
            }
        )
    if kind == "delete_node":
        allow_delete_subtree = bool(operation.get("allow_delete_subtree"))
        return _compact_dict(
            {
                "op": "delete",
                "legacy_kind": kind,
                "task_id": _as_int(operation.get("node_id")),
                "mode": "hard_delete",
                "subtree_policy": (
                    "include_children" if allow_delete_subtree else "reject_if_children"
                ),
                "allow_delete_subtree": allow_delete_subtree,
                "node_title": operation.get("node_title"),
            }
        )
    if kind == "metadata_update":
        updates = operation.get("updates") if isinstance(operation.get("updates"), dict) else {}
        return _compact_dict(
            {
                "op": "update",
                "legacy_kind": kind,
                "task_id": _as_int(operation.get("node_id")),
                "fields": updates,
            }
        )
    return {
        "op": kind or "unknown",
        "source": "structural_operation",
        "operation": operation,
    }


def _is_delete_action_proposal(proposal: dict[str, Any]) -> bool:
    kind_text = " ".join(
        str(proposal.get(key) or "")
        for key in ("kind", "proposal_kind", "proposalKind", "type", "operation", "action")
    ).strip().lower()
    if not kind_text:
        return False
    return any(token in kind_text for token in ("delete", "remove", "archive"))


def build_task_patch_draft(
    *,
    proposal_kind: str,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Build the first internal TaskPatch-shaped draft from proposal payloads."""
    proposal_id = (
        str(proposal.get("proposal_id") or proposal.get("proposalId") or "").strip()
        or str(uuid4())
    )
    target_node_ids = _proposal_target_ids(proposal)
    operations: list[dict[str, Any]] = []
    if proposal_kind == "metadata":
        shared_updates = proposal.get("updates") if isinstance(proposal.get("updates"), dict) else {}
        target_updates = _metadata_target_updates(proposal)
        if target_updates:
            for item in target_updates:
                operations.append(
                    {
                        "op": "update",
                        "task_id": item["target_node_id"],
                        "fields": item["updates"],
                    }
                )
        elif target_node_ids and shared_updates:
            for node_id in target_node_ids:
                operations.append({"op": "update", "task_id": node_id, "fields": shared_updates})
    elif proposal_kind == "branch":
        structural_operations = proposal.get("structural_operations")
        if isinstance(structural_operations, list) and structural_operations:
            operations.extend(
                _operation_from_structural_operation(operation)
                for operation in structural_operations
                if isinstance(operation, dict)
            )
        else:
            target_node_id = proposal.get("target_node_id", proposal.get("targetNodeId"))
            target_category_id = proposal.get("target_category_id", proposal.get("targetCategoryId"))
            for title in proposal.get("proposed_children", []):
                if isinstance(title, str) and title.strip():
                    operations.append(
                        {
                            "op": "create",
                            "category_id": target_category_id if isinstance(target_category_id, int) else None,
                            "parent_id": target_node_id if isinstance(target_node_id, int) else None,
                            "title": title.strip(),
                        }
                    )
    elif proposal_kind == "action" and _is_delete_action_proposal(proposal):
        for target_node_id in target_node_ids:
            operations.append(
                {
                    "op": "delete",
                    "task_id": target_node_id,
                    "mode": "hard_delete",
                    "subtree_policy": "include_children",
                }
            )
    return {
        "patch_id": proposal_id,
        "intent_summary": (
            str(proposal.get("summary") or proposal.get("rationale") or "").strip()
            or "Review this Strategist task patch."
        ),
        "target_scope": "multi_entity" if len(target_node_ids) > 1 else "task",
        "target_node_ids": target_node_ids,
        "operations": operations,
        "source_proposal_kind": proposal_kind,
    }


def _update_validation_errors(
    operation: dict[str, Any],
    nodes_by_id: dict[int, StrategyNode],
) -> list[str]:
    errors: list[str] = []
    task_id = _as_int(operation.get("task_id"))
    fields = operation.get("fields")
    if task_id is None:
        errors.append("Update operation is missing task_id.")
    elif task_id not in nodes_by_id:
        errors.append(f"Update operation references missing task {task_id}.")
    if not isinstance(fields, dict) or not fields:
        errors.append("Update operation is missing non-empty fields.")
    return errors


def _create_validation_messages(
    operation: dict[str, Any],
    nodes_by_id: dict[int, StrategyNode],
    created_temp_ids: set[str],
    deleted_task_ids: set[int],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    title = str(operation.get("title") or "").strip()
    if not title:
        errors.append("Create operation is missing title.")

    parent_id = operation.get("parent_id")
    if parent_id is not None:
        parent_node_id = _as_int(parent_id)
        if parent_node_id is None:
            errors.append("Create operation has an invalid parent_id.")
        elif parent_node_id not in nodes_by_id:
            errors.append(f"Create operation references missing parent task {parent_node_id}.")
        elif parent_node_id in deleted_task_ids:
            errors.append(
                f"Create operation cannot use task {parent_node_id} as a parent because this patch also deletes it."
            )

    parent_temp_id = operation.get("parent_temp_id")
    if isinstance(parent_temp_id, str) and parent_temp_id.strip():
        if parent_temp_id not in created_temp_ids:
            errors.append("Create operation references a temporary parent before it is created.")

    temp_id = str(operation.get("temp_id") or "").strip()
    if temp_id:
        if temp_id in created_temp_ids:
            errors.append(f"Create operation reuses temporary id {temp_id}.")
        else:
            created_temp_ids.add(temp_id)

    category_id = _as_int(operation.get("category_id"))
    parent_node_id = _as_int(parent_id)
    if category_id is not None and parent_node_id is not None:
        parent = nodes_by_id.get(parent_node_id)
        if parent is not None and parent.category_id != category_id:
            errors.append("Create operation parent is in a different category.")
    if category_id is None:
        warnings.append("Create operation has no category_id; approval adapter must infer it.")
    return errors, warnings


def _move_validation_errors(
    operation: dict[str, Any],
    nodes_by_id: dict[int, StrategyNode],
    children_by_parent: dict[int | None, list[int]],
    created_temp_ids: set[str],
    deleted_task_ids: set[int],
    category_ids: set[int] | None,
) -> list[str]:
    errors: list[str] = []
    task_id = _as_int(operation.get("task_id"))
    if task_id is None:
        errors.append("Move operation is missing task_id.")
        return errors
    node = nodes_by_id.get(task_id)
    if node is None:
        errors.append(f"Move operation references missing task {task_id}.")
        return errors

    target_category_id = _as_int(operation.get("category_id"))
    if target_category_id is None:
        target_category_id = int(node.category_id)
    if category_ids is not None and target_category_id not in category_ids:
        errors.append(f"Move operation references missing category {target_category_id}.")
    parent_id = operation.get("parent_id")
    if parent_id is not None:
        parent_node_id = _as_int(parent_id)
        if parent_node_id is None:
            errors.append("Move operation has an invalid parent_id.")
        elif parent_node_id not in nodes_by_id:
            errors.append(f"Move operation references missing destination {parent_node_id}.")
        elif parent_node_id in deleted_task_ids:
            errors.append(
                f"Move operation cannot use task {parent_node_id} as a destination because this patch also deletes it."
            )
        else:
            destination = nodes_by_id[parent_node_id]
            if destination.category_id != target_category_id:
                errors.append("Move operation destination parent is in a different category.")
            if parent_node_id == task_id:
                errors.append("Move operation would parent a task to itself.")
            if parent_node_id in _descendant_ids(task_id, children_by_parent):
                errors.append("Move operation would move a task under one of its descendants.")

    parent_temp_id = operation.get("parent_temp_id")
    if isinstance(parent_temp_id, str) and parent_temp_id.strip():
        if parent_temp_id not in created_temp_ids:
            errors.append("Move operation references a temporary parent before it is created.")
    return errors


def _delete_validation_messages(
    operation: dict[str, Any],
    nodes_by_id: dict[int, StrategyNode],
    children_by_parent: dict[int | None, list[int]],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    task_id = _as_int(operation.get("task_id"))
    if task_id is None:
        errors.append("Delete operation is missing task_id.")
        return errors, warnings
    if task_id not in nodes_by_id:
        errors.append(f"Delete operation references missing task {task_id}.")
        return errors, warnings
    descendants = _descendant_ids(task_id, children_by_parent)
    if str(operation.get("mode") or "").strip().lower() == "archive":
        return errors, warnings
    subtree_policy = str(operation.get("subtree_policy") or "").strip().lower()
    if descendants and subtree_policy == "include_children":
        warnings.append(
            f"Delete operation will also remove {len(descendants)} descendant task(s)."
        )
    elif descendants:
        warnings.append(
            "Delete operation targets a task with descendants; approval validation must confirm preservation or explicit subtree delete."
        )
    return errors, warnings


def validate_task_patch(
    task_patch: dict[str, Any],
    nodes: list[StrategyNode],
    *,
    category_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Validate a TaskPatch draft against the current task snapshot."""
    errors: list[str] = []
    warnings: list[str] = []
    operations = task_patch.get("operations")
    if not isinstance(operations, list):
        operations = []
        errors.append("TaskPatch operations must be a list.")
    if not operations:
        errors.append("TaskPatch has no operations.")

    nodes_by_id = _nodes_by_id(nodes)
    children_by_parent = _children_by_parent(nodes)
    created_temp_ids: set[str] = set()
    seen_operation_targets: dict[tuple[str, int], int] = {}
    deleted_task_ids = {
        task_id
        for operation in operations
        if isinstance(operation, dict)
        and str(operation.get("op") or "").strip().lower() == "delete"
        for task_id in [_as_int(operation.get("task_id"))]
        if task_id is not None
    }
    archived_task_ids = {
        task_id
        for operation in operations
        if isinstance(operation, dict)
        and str(operation.get("op") or "").strip().lower() == "delete"
        and str(operation.get("mode") or "").strip().lower() == "archive"
        for task_id in [_as_int(operation.get("task_id"))]
        if task_id is not None
    }
    for operation in operations:
        if not isinstance(operation, dict):
            errors.append("TaskPatch operation must be an object.")
            continue
        op = str(operation.get("op") or "").strip().lower()
        task_id = _as_int(operation.get("task_id"))
        if op in {"update", "move", "delete"} and task_id is not None:
            target_key = (op, task_id)
            seen_operation_targets[target_key] = seen_operation_targets.get(target_key, 0) + 1
            if seen_operation_targets[target_key] > 1:
                warnings.append(f"TaskPatch contains multiple {op} operations for task {task_id}.")
        if op in {"update", "move"} and task_id in deleted_task_ids:
            errors.append(
                f"TaskPatch cannot {op} task {task_id} because the same patch also removes it."
            )
        if op == "update":
            errors.extend(_update_validation_errors(operation, nodes_by_id))
        elif op == "create":
            create_errors, create_warnings = _create_validation_messages(
                operation,
                nodes_by_id,
                created_temp_ids,
                deleted_task_ids,
            )
            errors.extend(create_errors)
            warnings.extend(create_warnings)
        elif op == "move":
            errors.extend(
                _move_validation_errors(
                    operation,
                    nodes_by_id,
                    children_by_parent,
                    created_temp_ids,
                    deleted_task_ids,
                    category_ids,
                )
            )
        elif op == "delete":
            delete_errors, delete_warnings = _delete_validation_messages(
                operation,
                nodes_by_id,
                children_by_parent,
            )
            errors.extend(delete_errors)
            warnings.extend(delete_warnings)
        else:
            warnings.append(f"TaskPatch operation has unsupported op {op or 'unknown'}.")

    archived_subtree_ids: set[int] = set()
    for archived_task_id in archived_task_ids:
        archived_subtree_ids.add(archived_task_id)
        archived_subtree_ids.update(_descendant_ids(archived_task_id, children_by_parent))
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "").strip().lower()
        task_id = _as_int(operation.get("task_id"))
        is_own_archive = (
            op == "delete"
            and str(operation.get("mode") or "").strip().lower() == "archive"
            and task_id in archived_task_ids
        )
        if task_id in archived_subtree_ids and not is_own_archive:
            errors.append(
                f"TaskPatch cannot {op or 'change'} task {task_id} because its subtree is also archived."
            )

    return {
        "status": "invalid" if errors else "warning" if warnings else "valid",
        "errors": errors,
        "warnings": warnings,
        "operation_count": len(operations),
    }


def simulate_task_patch(
    task_patch: dict[str, Any],
    nodes: list[StrategyNode],
) -> dict[str, Any]:
    """Return a lightweight preview of the TaskPatch blast radius."""
    operations = task_patch.get("operations")
    if not isinstance(operations, list):
        operations = []
    children_by_parent = _children_by_parent(nodes)
    affected_node_ids: set[int] = set()
    deleted_subtree_node_ids: set[int] = set()
    created_titles: list[str] = []
    warnings: list[str] = []
    counts = {
        "creates": 0,
        "updates": 0,
        "moves": 0,
        "archives": 0,
        "deletes": 0,
    }

    for operation in operations:
        if not isinstance(operation, dict):
            warnings.append("Skipped non-object operation during simulation.")
            continue
        op = str(operation.get("op") or "").strip().lower()
        task_id = _as_int(operation.get("task_id"))
        parent_id = _as_int(operation.get("parent_id"))
        if op == "update":
            counts["updates"] += 1
            if task_id is not None:
                affected_node_ids.add(task_id)
        elif op == "create":
            counts["creates"] += 1
            title = str(operation.get("title") or "").strip()
            if title:
                created_titles.append(title)
            if parent_id is not None:
                affected_node_ids.add(parent_id)
        elif op == "move":
            counts["moves"] += 1
            if task_id is not None:
                affected_node_ids.add(task_id)
            if parent_id is not None:
                affected_node_ids.add(parent_id)
        elif op == "delete":
            is_archive = str(operation.get("mode") or "").strip().lower() == "archive"
            counts["archives" if is_archive else "deletes"] += 1
            if task_id is not None:
                affected_node_ids.add(task_id)
                if str(operation.get("subtree_policy") or "").strip().lower() == "include_children":
                    for descendant_id in _descendant_ids(task_id, children_by_parent):
                        affected_node_ids.add(descendant_id)
                        if not is_archive:
                            deleted_subtree_node_ids.add(descendant_id)
        else:
            warnings.append(f"Unsupported op {op or 'unknown'} was not simulated.")

    return {
        "operation_count": len(operations),
        "affected_node_ids": sorted(affected_node_ids),
        "deleted_subtree_node_ids": sorted(deleted_subtree_node_ids),
        "created_titles": created_titles,
        "warnings": warnings,
        **counts,
    }
