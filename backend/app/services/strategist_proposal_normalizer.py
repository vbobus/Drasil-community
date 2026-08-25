"""Normalize model-authored proposal payloads into TaskPatch proposal objects."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.models.node import StrategyNode
from app.services.strategist_provider_response import coerce_int_id, first_present
from app.services.strategist_proposals import (
    build_branch_children_proposal,
    build_bulk_metadata_patch_proposal,
    build_metadata_patch_proposal,
    build_subtree_rewrite_proposal,
    build_top_level_create_task_proposal,
    build_top_level_task_set_proposal,
    normalize_proposal_diff_preview,
    normalize_proposal_evidence,
)

VALID_PRIORITIES = {"low", "medium", "high"}
VALID_STATUSES = {"todo", "in-progress", "done"}
VALID_NODE_TYPES = {"task", "habit"}


def normalize_deadline_date(value: Any) -> str | None:
    """Normalize a task deadline to its date-only domain representation."""

    if not isinstance(value, str) or not value.strip():
        return None
    trimmed = value.strip()
    try:
        return date.fromisoformat(trimmed).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(trimmed.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def normalize_title_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def semantic_resolution_tokens(value: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "branch",
        "card",
        "child",
        "children",
        "crd",
        "existing",
        "for",
        "my",
        "node",
        "subtask",
        "subtasks",
        "task",
        "tasks",
        "the",
        "this",
        "that",
        "to",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_title_key(value))
        if token not in stopwords and len(token) > 1
    }


def resolve_semantic_node_from_candidates(
    *,
    text_values: list[str],
    candidates: list[StrategyNode],
) -> StrategyNode | None:
    for text in text_values:
        normalized = normalize_title_key(text)
        if not normalized:
            continue
        exact_matches = [
            node for node in candidates if normalize_title_key(node.title) == normalized
        ]
        if exact_matches:
            exact_matches.sort(key=lambda node: (len(node.title), node.sort_order, node.title.lower()))
            return exact_matches[0]
        query_tokens = semantic_resolution_tokens(text)
        if not query_tokens:
            continue
        token_matches = [
            node
            for node in candidates
            if query_tokens.issubset(semantic_resolution_tokens(node.title))
        ]
        if token_matches:
            token_matches.sort(key=lambda node: (len(node.title), node.sort_order, node.title.lower()))
            return token_matches[0]
    return None


def build_children_by_parent(nodes: list[StrategyNode]) -> dict[int, list[StrategyNode]]:
    children_by_parent: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children_by_parent.setdefault(node.parent_id, []).append(node)
    for children in children_by_parent.values():
        children.sort(key=lambda item: (item.sort_order, item.title.lower()))
    return children_by_parent


def collect_descendants(
    node: StrategyNode,
    children_by_parent: dict[int, list[StrategyNode]],
) -> list[StrategyNode]:
    if node.id is None:
        return []
    descendants: list[StrategyNode] = []

    def visit(parent_id: int) -> None:
        for child in children_by_parent.get(parent_id, []):
            descendants.append(child)
            if child.id is not None:
                visit(child.id)

    visit(node.id)
    return descendants


def normalize_metadata_updates(raw_updates: Any) -> dict[str, Any]:
    if not isinstance(raw_updates, dict):
        return {}
    normalized: dict[str, Any] = {}
    title = raw_updates.get("title")
    if isinstance(title, str):
        trimmed = title.strip()
        if trimmed:
            normalized["title"] = trimmed
    notes = raw_updates.get("notes")
    if isinstance(notes, str):
        trimmed = notes.strip()
        if trimmed:
            normalized["notes"] = trimmed
    status = raw_updates.get("status")
    if isinstance(status, str):
        lowered = status.strip().lower()
        if lowered in VALID_STATUSES:
            normalized["status"] = lowered
    priority = raw_updates.get("priority")
    if isinstance(priority, str):
        lowered = priority.strip().lower()
        if lowered in VALID_PRIORITIES:
            normalized["priority"] = lowered
    deadline_at = raw_updates.get("deadline_at", raw_updates.get("deadlineAt"))
    normalized_deadline = normalize_deadline_date(deadline_at)
    if normalized_deadline:
        normalized["deadline_at"] = normalized_deadline
    time_estimate_minutes = raw_updates.get("time_estimate_minutes", raw_updates.get("timeEstimateMinutes"))
    if isinstance(time_estimate_minutes, str) and time_estimate_minutes.strip().isdigit():
        time_estimate_minutes = int(time_estimate_minutes.strip())
    if isinstance(time_estimate_minutes, (int, float)):
        minutes = int(time_estimate_minutes)
        if minutes > 0:
            normalized["time_estimate_minutes"] = minutes
    tags = raw_updates.get("tags")
    if isinstance(tags, list):
        deduped: list[str] = []
        for tag in tags:
            if isinstance(tag, str):
                trimmed = tag.strip().lower()
                if trimmed and trimmed not in deduped:
                    deduped.append(trimmed)
        if deduped:
            normalized["tags"] = deduped
    is_pinned = raw_updates.get("is_pinned", raw_updates.get("isPinned"))
    if isinstance(is_pinned, bool):
        normalized["is_pinned"] = is_pinned
    node_type = raw_updates.get("node_type", raw_updates.get("nodeType"))
    if isinstance(node_type, str):
        lowered = node_type.strip().lower()
        if lowered in VALID_NODE_TYPES:
            normalized["node_type"] = lowered
    return normalized


def normalize_metadata_target_updates(raw_target_updates: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_target_updates, list):
        return []
    normalized_items: list[dict[str, Any]] = []
    seen_node_ids: set[int] = set()
    for raw_item in raw_target_updates:
        if not isinstance(raw_item, dict):
            continue
        target_node_id = coerce_int_id(
            first_present(
                raw_item,
                "target_node_id",
                "targetNodeId",
                "target_task_id",
                "targetTaskId",
                "task_id",
                "taskId",
            )
        )
        if target_node_id is None or target_node_id in seen_node_ids:
            continue
        updates = normalize_metadata_updates(raw_item.get("updates"))
        if not updates:
            continue
        seen_node_ids.add(target_node_id)
        target_node_title = first_present(
            raw_item,
            "target_node_title",
            "targetNodeTitle",
            "target_task_title",
            "targetTaskTitle",
            "task_title",
            "taskTitle",
        )
        normalized_items.append(
            {
                "target_node_id": target_node_id,
                "target_node_title": (
                    target_node_title.strip()
                    if isinstance(target_node_title, str) and target_node_title.strip()
                    else "Task"
                ),
                "updates": updates,
            }
        )
    return normalized_items


def normalize_badge_suggestions(raw_suggestions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_suggestions, list):
        return []
    suggestions: list[dict[str, Any]] = []
    for raw_item in raw_suggestions:
        if not isinstance(raw_item, dict):
            continue
        target_node_id = coerce_int_id(
            first_present(
                raw_item,
                "target_node_id",
                "targetNodeId",
                "target_task_id",
                "targetTaskId",
                "task_id",
                "taskId",
            )
        )
        target_node_title = first_present(
            raw_item,
            "target_node_title",
            "targetNodeTitle",
            "target_task_title",
            "targetTaskTitle",
            "task_title",
            "taskTitle",
        )
        emoji = raw_item.get("emoji")
        color = raw_item.get("color")
        label = raw_item.get("label")
        rationale = raw_item.get("rationale")
        suggestion: dict[str, Any] = {}
        if target_node_id is not None:
            suggestion["target_node_id"] = target_node_id
        if isinstance(target_node_title, str) and target_node_title.strip():
            suggestion["target_node_title"] = target_node_title.strip()
        if isinstance(emoji, str) and emoji.strip():
            suggestion["emoji"] = emoji.strip()[:16]
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{3,8}", color.strip()):
            suggestion["color"] = color.strip()
        if isinstance(label, str) and label.strip():
            suggestion["label"] = label.strip()
        if isinstance(rationale, str) and rationale.strip():
            suggestion["rationale"] = rationale.strip()
        if any(key in suggestion for key in ("emoji", "color", "label")):
            suggestions.append(suggestion)
    return suggestions[:6]


def raw_target_node_ids_from_proposal(raw_proposal: dict[str, Any]) -> list[int]:
    target_node_ids: list[int] = []
    raw_target_updates = raw_proposal.get("target_updates", raw_proposal.get("targetUpdates"))
    if isinstance(raw_target_updates, list):
        for raw_item in raw_target_updates:
            if not isinstance(raw_item, dict):
                continue
            node_id = coerce_int_id(
                first_present(
                    raw_item,
                    "target_node_id",
                    "targetNodeId",
                    "target_task_id",
                    "targetTaskId",
                    "task_id",
                    "taskId",
                )
            )
            if node_id is not None and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    raw_ids = first_present(
        raw_proposal,
        "target_node_ids",
        "targetNodeIds",
        "target_task_ids",
        "targetTaskIds",
        "task_ids",
        "taskIds",
    )
    if isinstance(raw_ids, list):
        for node_id in raw_ids:
            node_id = coerce_int_id(node_id)
            if node_id is not None and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    target_node_id = coerce_int_id(
        first_present(
            raw_proposal,
            "target_node_id",
            "targetNodeId",
            "target_task_id",
            "targetTaskId",
            "task_id",
            "taskId",
        )
    )
    if target_node_id is not None and target_node_id not in target_node_ids:
        target_node_ids.insert(0, target_node_id)
    return target_node_ids


def resolve_proposal_target_node(
    raw_proposal: Any,
    *,
    nodes: list[StrategyNode],
    fallback_focus_node: StrategyNode | None,
) -> StrategyNode | None:
    if not isinstance(raw_proposal, dict):
        return fallback_focus_node

    target_node_id = coerce_int_id(
        first_present(
            raw_proposal,
            "target_node_id",
            "targetNodeId",
            "target_task_id",
            "targetTaskId",
            "task_id",
            "taskId",
        )
    )
    if target_node_id is not None:
        for node in nodes:
            if node.id == target_node_id:
                return node

    target_node_title = first_present(
        raw_proposal,
        "target_node_title",
        "targetNodeTitle",
        "target_task_title",
        "targetTaskTitle",
        "task_title",
        "taskTitle",
    )
    if isinstance(target_node_title, str):
        normalized_title = normalize_title_key(target_node_title)
        exact = [node for node in nodes if normalize_title_key(node.title) == normalized_title]
        if len(exact) == 1:
            return exact[0]
        query_tokens = semantic_resolution_tokens(target_node_title)
        if len(query_tokens) >= 2:
            token_matches = [
                node
                for node in nodes
                if node.id is not None and query_tokens.issubset(semantic_resolution_tokens(node.title))
            ]
            if len(token_matches) == 1:
                return token_matches[0]
            if token_matches:
                token_matches.sort(key=lambda node: (len(node.title), node.sort_order, node.title.lower()))
                return token_matches[0]

    return fallback_focus_node


def normalize_structural_operation_kind(kind: Any) -> str:
    normalized = (
        str(kind or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    aliases = {
        "create": "create_node",
        "task.create": "create_node",
        "create_task": "create_node",
        "create_subtask": "create_node",
        "create_child": "create_node",
        "create_child_task": "create_node",
        "add_child": "create_node",
        "add_child_task": "create_node",
        "add": "create_node",
        "add_task": "create_node",
        "add_node": "create_node",
        "new_task": "create_node",
        "new_node": "create_node",
        "move": "move_subtree",
        "task.move": "move_subtree",
        "move_task": "move_subtree",
        "reparent": "move_subtree",
        "reparent_node": "move_subtree",
        "move_node": "move_subtree",
        "reorder": "move_subtree",
        "archive": "archive_node",
        "task.archive": "archive_node",
        "archive_task": "archive_node",
        "delete": "delete_node",
        "task.delete": "delete_node",
        "delete_task": "delete_node",
        "remove": "delete_node",
        "remove_task": "delete_node",
        "remove_node": "delete_node",
        "update": "metadata_update",
        "task.update": "metadata_update",
        "update_task": "metadata_update",
        "update_metadata": "metadata_update",
        "metadata": "metadata_update",
        # The proposal tool intentionally accepts human/model-friendly rename
        # operation names.  A rename is persisted as a normal metadata update
        # so it can be composed atomically with create/move/delete operations.
        "rename": "metadata_update",
        "rename_node": "metadata_update",
        "rename_task": "metadata_update",
        "set_title": "metadata_update",
        "update_title": "metadata_update",
    }
    return aliases.get(normalized, normalized)


def normalize_structural_operation_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def normalize_structural_operation_envelope(raw_operation: dict[str, Any]) -> dict[str, Any]:
    """Accept model-friendly structural operation aliases at the proposal boundary."""

    operation = dict(raw_operation)
    nested_node = operation.get("node")
    if isinstance(nested_node, dict):
        for key in (
            "title",
            "category_id",
            "categoryId",
            "category_name",
            "categoryName",
            "status",
            "priority",
            "deadline_at",
            "deadlineAt",
            "notes",
            "node_type",
            "nodeType",
            "time_estimate_minutes",
            "timeEstimateMinutes",
            "emoji",
            "tags",
            "is_pinned",
            "isPinned",
            "is_backlog",
            "isBacklog",
        ):
            if operation.get(key) in (None, "", [], {}) and nested_node.get(key) not in (None, "", [], {}):
                operation[key] = nested_node[key]
        operation.pop("node", None)
    if "kind" not in operation and isinstance(operation.get("operation"), str):
        operation["kind"] = operation["operation"]
    if "kind" not in operation and isinstance(operation.get("op"), str):
        operation["kind"] = operation["op"]
    if "parent_id" not in operation:
        for key in ("parent_task_id", "parentTaskId"):
            if key in operation:
                operation["parent_id"] = operation[key]
                break
    if "parent_title" not in operation:
        for key in ("parent_task_title", "parentTaskTitle"):
            if key in operation:
                operation["parent_title"] = operation[key]
                break
    if "destination_parent_id" not in operation:
        for key in ("destination_parent_task_id", "destinationParentTaskId"):
            if key in operation:
                operation["destination_parent_id"] = operation[key]
                break
    if "destination_parent_title" not in operation:
        for key in ("destination_parent_task_title", "destinationParentTaskTitle"):
            if key in operation:
                operation["destination_parent_title"] = operation[key]
                break
    return operation


def _normalized_category_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _same_title(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return normalize_title_key(left) == normalize_title_key(right)


def _temp_id_from_title(title: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", normalize_title_key(title))
    return "_".join(tokens[:6]) or "new_parent"


def _raw_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _proposal_nested_create_hint_text(raw_proposal: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("summary", "rationale", "description", "confirmation_body", "body"):
        value = raw_proposal.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    proposed_children = _raw_string_list(
        raw_proposal.get("proposed_children", raw_proposal.get("proposedChildren"))
    )
    if proposed_children:
        parts.append(" ".join(proposed_children))
    return " ".join(parts).lower()


def _proposal_requests_nested_top_level_create(raw_proposal: dict[str, Any]) -> bool:
    text = _proposal_nested_create_hint_text(raw_proposal)
    return bool(
        re.search(
            r"\b(parent|child|children|subtask|subtasks|nested|hierarchy|under|inside)\b",
            text,
        )
    )


def _active_category_from_evidence(
    evidence_bundle: dict[str, Any] | None,
) -> tuple[int | None, str | None]:
    if not isinstance(evidence_bundle, dict):
        return None, None
    active_category = evidence_bundle.get("active_category")
    if not isinstance(active_category, dict):
        return None, None
    category_id = coerce_int_id(first_present(active_category, "category_id", "id"))
    category_name = first_present(active_category, "name", "category_name", "categoryName")
    return (
        category_id,
        category_name.strip()
        if isinstance(category_name, str) and category_name.strip()
        else None,
    )


def _raw_category_name(raw_proposal: dict[str, Any]) -> str | None:
    value = first_present(
        raw_proposal,
        "target_category_name",
        "targetCategoryName",
        "category_name",
        "categoryName",
    )
    return value.strip() if isinstance(value, str) and value.strip() else None


def _available_categories_from_evidence(
    evidence_bundle: dict[str, Any] | None,
) -> list[tuple[int, str]]:
    if not isinstance(evidence_bundle, dict):
        return []
    raw_categories = evidence_bundle.get("available_categories")
    if not isinstance(raw_categories, list):
        return []
    categories: list[tuple[int, str]] = []
    for raw_category in raw_categories:
        if not isinstance(raw_category, dict):
            continue
        category_id = coerce_int_id(
            first_present(raw_category, "category_id", "categoryId", "id")
        )
        category_name = first_present(
            raw_category,
            "name",
            "category_name",
            "categoryName",
        )
        if (
            category_id is not None
            and isinstance(category_name, str)
            and category_name.strip()
        ):
            categories.append((category_id, category_name.strip()))
    return categories


def _category_id_from_evidence(
    raw_proposal: dict[str, Any],
    *,
    evidence_bundle: dict[str, Any] | None,
) -> int | None:
    available_categories = _available_categories_from_evidence(evidence_bundle)
    raw_name = _raw_category_name(raw_proposal)
    explicit_category_id = coerce_int_id(
        first_present(
            raw_proposal,
            "target_category_id",
            "targetCategoryId",
            "category_id",
            "categoryId",
        )
    )
    if explicit_category_id is not None:
        if available_categories:
            matching_category = next(
                (
                    category
                    for category in available_categories
                    if category[0] == explicit_category_id
                ),
                None,
            )
            if matching_category is None:
                return None
            if (
                raw_name
                and _normalized_category_name(raw_name)
                != _normalized_category_name(matching_category[1])
            ):
                return None
        return explicit_category_id
    if raw_name:
        normalized_raw_name = _normalized_category_name(raw_name)
        matching_category = next(
            (
                category
                for category in available_categories
                if _normalized_category_name(category[1]) == normalized_raw_name
            ),
            None,
        )
        if matching_category is not None:
            return matching_category[0]
        # A named workspace that does not exist must never silently fall back to
        # the active workspace and put a new task in the wrong tree.
        if available_categories:
            return None
    active_category_id, active_category_name = _active_category_from_evidence(evidence_bundle)
    if active_category_id is None:
        return None
    if raw_name and active_category_name:
        if _normalized_category_name(raw_name) == _normalized_category_name(active_category_name):
            return active_category_id
        return None
    # With more than one known workspace, omission is not consent to place all
    # new tasks in whichever tree happens to be active (usually Work).
    if len(available_categories) > 1:
        return None
    return active_category_id


def _category_name_from_evidence(
    raw_proposal: dict[str, Any],
    *,
    evidence_bundle: dict[str, Any] | None,
) -> str | None:
    raw_name = _raw_category_name(raw_proposal)
    if raw_name:
        normalized_raw_name = _normalized_category_name(raw_name)
        matching_category = next(
            (
                category
                for category in _available_categories_from_evidence(evidence_bundle)
                if _normalized_category_name(category[1]) == normalized_raw_name
            ),
            None,
        )
        return matching_category[1] if matching_category is not None else raw_name
    explicit_category_id = coerce_int_id(
        first_present(
            raw_proposal,
            "target_category_id",
            "targetCategoryId",
            "category_id",
            "categoryId",
        )
    )
    if explicit_category_id is not None:
        matching_category = next(
            (
                category
                for category in _available_categories_from_evidence(evidence_bundle)
                if category[0] == explicit_category_id
            ),
            None,
        )
        if matching_category is not None:
            return matching_category[1]
    _, active_category_name = _active_category_from_evidence(evidence_bundle)
    return active_category_name


def node_by_int_id(nodes: list[StrategyNode], node_id: int | None) -> StrategyNode | None:
    if node_id is None:
        return None
    for node in nodes:
        if node.id == node_id:
            return node
    return None


def structural_resolution_candidates(
    *,
    target_node: StrategyNode,
    nodes: list[StrategyNode],
) -> list[StrategyNode]:
    candidates: list[StrategyNode] = []
    if target_node.id is not None:
        children_by_parent = build_children_by_parent(nodes)
        candidates.extend(
            node
            for node in collect_descendants(target_node, children_by_parent)
            if node.id is not None
        )
    if target_node.id is not None:
        candidates.insert(0, target_node)
    seen_ids: set[int] = set()
    deduped: list[StrategyNode] = []
    for node in candidates:
        if node.id is None or node.id in seen_ids:
            continue
        seen_ids.add(node.id)
        deduped.append(node)
    for node in nodes:
        if (
            node.id is not None
            and node.id not in seen_ids
            and node.category_id == target_node.category_id
        ):
            seen_ids.add(node.id)
            deduped.append(node)
    return deduped


def resolve_structural_node_reference(
    *,
    raw_id: Any,
    text_values: list[Any],
    target_node: StrategyNode,
    nodes: list[StrategyNode],
) -> StrategyNode | None:
    node_id = coerce_int_id(raw_id)
    node = node_by_int_id(nodes, node_id)
    if node is not None:
        return node
    texts = [
        value.strip()
        for value in text_values
        if isinstance(value, str) and value.strip()
    ]
    if not texts:
        return None
    return resolve_semantic_node_from_candidates(
        text_values=texts,
        candidates=structural_resolution_candidates(
            target_node=target_node,
            nodes=nodes,
        ),
    )


def normalize_structural_operation(
    raw_operation: dict[str, Any],
    *,
    target_node: StrategyNode,
    nodes: list[StrategyNode],
    evidence_bundle: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    raw_operation = normalize_structural_operation_envelope(raw_operation)
    kind = normalize_structural_operation_kind(
        first_present(raw_operation, "kind", "type", "operation", "op")
    )
    if kind not in {
        "create_node",
        "move_subtree",
        "archive_node",
        "delete_node",
        "metadata_update",
    }:
        return None

    category_id = coerce_int_id(
        first_present(raw_operation, "category_id", "categoryId")
    ) or target_node.category_id
    if kind == "create_node":
        title = normalize_structural_operation_text(raw_operation.get("title"))
        if not title or category_id is None:
            return None
        parent_id = coerce_int_id(first_present(raw_operation, "parent_id", "parentId"))
        if parent_id is None:
            parent_id = coerce_int_id(
                first_present(raw_operation, "parent_task_id", "parentTaskId")
            )
        parent_temp_id = normalize_structural_operation_text(
            first_present(raw_operation, "parent_temp_id", "parentTempId")
        )
        parent_title = normalize_structural_operation_text(
            first_present(
                raw_operation,
                "parent_title",
                "parentTitle",
                "parent_task_title",
                "parentTaskTitle",
            )
        )
        if parent_id is None and not parent_temp_id and parent_title:
            parent_node = resolve_structural_node_reference(
                raw_id=None,
                text_values=[parent_title],
                target_node=target_node,
                nodes=nodes,
            )
            if parent_node is not None and parent_node.id is not None:
                parent_id = int(parent_node.id)
                parent_title = parent_node.title
        if parent_id is None and not parent_temp_id and target_node.id is not None:
            parent_id = int(target_node.id)
        operation: dict[str, Any] = {
            "kind": "create_node",
            "title": title,
            "category_id": int(category_id),
            "parent_id": parent_id,
            "parent_temp_id": parent_temp_id or None,
            "parent_title": (
                parent_title
                or target_node.title
            ),
        }
        temp_id = normalize_structural_operation_text(
            first_present(raw_operation, "temp_id", "tempId")
        )
        if temp_id:
            operation["temp_id"] = temp_id
        notes = normalize_structural_operation_text(raw_operation.get("notes"))
        if notes:
            operation["notes"] = notes
        priority = normalize_structural_operation_text(raw_operation.get("priority")).lower()
        if priority in VALID_PRIORITIES:
            operation["priority"] = priority
        status = normalize_structural_operation_text(raw_operation.get("status")).lower()
        if status in VALID_STATUSES:
            operation["status"] = status
        node_type = normalize_structural_operation_text(
            first_present(raw_operation, "node_type", "nodeType")
        ).lower()
        if node_type in VALID_NODE_TYPES:
            operation["node_type"] = node_type
        emoji = normalize_structural_operation_text(raw_operation.get("emoji"))
        if emoji:
            operation["emoji"] = emoji
        deadline_at = normalize_structural_operation_text(
            first_present(raw_operation, "deadline_at", "deadlineAt")
        )
        if deadline_at:
            try:
                operation["deadline_at"] = date.fromisoformat(deadline_at).isoformat()
            except ValueError:
                pass
        estimate = coerce_int_id(
            first_present(raw_operation, "time_estimate_minutes", "timeEstimateMinutes")
        )
        if estimate is not None:
            operation["time_estimate_minutes"] = estimate
        tags = raw_operation.get("tags")
        if isinstance(tags, list):
            normalized_tags = [str(item).strip() for item in tags if str(item).strip()]
            if normalized_tags:
                operation["tags"] = normalized_tags
        is_pinned = first_present(raw_operation, "is_pinned", "isPinned")
        if isinstance(is_pinned, bool):
            operation["is_pinned"] = is_pinned
        is_backlog = first_present(raw_operation, "is_backlog", "isBacklog")
        if isinstance(is_backlog, bool):
            operation["is_backlog"] = is_backlog
        return operation

    source_node = resolve_structural_node_reference(
        raw_id=first_present(
            raw_operation,
            "node_id",
            "nodeId",
            "task_id",
            "taskId",
            "target_node_id",
            "targetNodeId",
            "target_task_id",
            "targetTaskId",
            "source_node_id",
            "sourceNodeId",
            "source_task_id",
            "sourceTaskId",
        ),
        text_values=[
            raw_operation.get("node_title"),
            raw_operation.get("nodeTitle"),
            raw_operation.get("task_title"),
            raw_operation.get("taskTitle"),
            raw_operation.get("target_node_title"),
            raw_operation.get("targetNodeTitle"),
            raw_operation.get("target_task_title"),
            raw_operation.get("targetTaskTitle"),
            raw_operation.get("source_title"),
            raw_operation.get("sourceTitle"),
            raw_operation.get("source_task_title"),
            raw_operation.get("sourceTaskTitle"),
            raw_operation.get("title"),
        ],
        target_node=target_node,
        nodes=nodes,
    )
    if source_node is None or source_node.id is None:
        return None

    if kind == "move_subtree":
        raw_destination_category_id = first_present(
            raw_operation,
            "destination_category_id",
            "destinationCategoryId",
            "destination_workspace_id",
            "destinationWorkspaceId",
        )
        raw_destination_category_name = first_present(
            raw_operation,
            "destination_category_name",
            "destinationCategoryName",
            "destination_workspace_name",
            "destinationWorkspaceName",
        )
        destination_category_id = None
        destination_category_name = ""
        if raw_destination_category_id is not None or (
            isinstance(raw_destination_category_name, str)
            and raw_destination_category_name.strip()
        ):
            destination_category_probe = {
                "target_category_id": raw_destination_category_id,
                "target_category_name": raw_destination_category_name,
            }
            destination_category_id = _category_id_from_evidence(
                destination_category_probe,
                evidence_bundle=evidence_bundle,
            )
            destination_category_name = (
                _category_name_from_evidence(
                    destination_category_probe,
                    evidence_bundle=evidence_bundle,
                )
                or ""
            )
            if destination_category_id is None:
                return None
        destination_parent_id = coerce_int_id(
            first_present(
                raw_operation,
                "destination_parent_id",
                "destinationParentId",
                "destination_parent_task_id",
                "destinationParentTaskId",
                "dest_parent_id",
                "destParentId",
                "dest_parent_task_id",
                "destParentTaskId",
            )
        )
        destination_parent_temp_id = normalize_structural_operation_text(
            first_present(
                raw_operation,
                "destination_parent_temp_id",
                "destinationParentTempId",
                "dest_parent_temp_id",
                "destParentTempId",
            )
        )
        destination_title = normalize_structural_operation_text(
            first_present(
                raw_operation,
                "destination_parent_title",
                "destinationParentTitle",
                "dest_parent_title",
                "destParentTitle",
                "parent_title",
                "parentTitle",
            )
        )
        if destination_parent_id is None and not destination_parent_temp_id and destination_title:
            destination_node = resolve_structural_node_reference(
                raw_id=None,
                text_values=[destination_title],
                target_node=target_node,
                nodes=nodes,
            )
            if destination_node is not None and destination_node.id is not None:
                destination_parent_id = int(destination_node.id)
        destination_parent = node_by_int_id(nodes, destination_parent_id)
        if destination_parent is not None:
            if (
                destination_category_id is not None
                and destination_parent.category_id != destination_category_id
            ):
                return None
            destination_category_id = int(destination_parent.category_id)
            destination_category_name = destination_category_name or str(
                next(
                    (
                        name
                        for category_id, name in _available_categories_from_evidence(
                            evidence_bundle
                        )
                        if category_id == destination_parent.category_id
                    ),
                    "",
                )
            )
        cross_workspace_root_move = (
            destination_category_id is not None
            and destination_category_id != source_node.category_id
            and destination_parent_id is None
            and not destination_parent_temp_id
        )
        if (
            destination_parent_id is None
            and not destination_parent_temp_id
            and target_node.id is not None
            and not cross_workspace_root_move
        ):
            destination_parent_id = int(target_node.id)
            destination_title = destination_title or target_node.title
        if cross_workspace_root_move:
            destination_title = destination_title or (
                f"top level of {destination_category_name}"
                if destination_category_name
                else "destination workspace"
            )
        operation = {
            "kind": "move_subtree",
            "node_id": int(source_node.id),
            "node_title": source_node.title,
            "destination_parent_id": destination_parent_id,
            "destination_parent_temp_id": destination_parent_temp_id or None,
            "destination_parent_title": destination_title or target_node.title,
        }
        if (
            destination_category_id is not None
            and destination_category_id != source_node.category_id
        ):
            operation["destination_category_id"] = destination_category_id
            operation["destination_category_name"] = destination_category_name or None
        target_index = coerce_int_id(first_present(raw_operation, "target_index", "targetIndex"))
        if target_index is not None:
            operation["target_index"] = target_index
        return operation

    if kind in {"archive_node", "delete_node"}:
        operation = {
            "kind": kind,
            "node_id": int(source_node.id),
            "node_title": source_node.title,
        }
        allow_delete_subtree = first_present(
            raw_operation,
            "allow_delete_subtree",
            "allowDeleteSubtree",
            "delete_subtree",
            "deleteSubtree",
        )
        if isinstance(allow_delete_subtree, bool):
            operation["allow_delete_subtree"] = allow_delete_subtree
        return operation

    raw_updates = first_present(
        raw_operation,
        "updates",
        "metadata",
        "metadata_updates",
        "metadataUpdates",
    )
    updates = normalize_metadata_updates(raw_updates)
    updates.update(normalize_metadata_updates(raw_operation))
    if not updates:
        return None
    return {
        "kind": "metadata_update",
        "node_id": int(source_node.id),
        "node_title": source_node.title,
        "updates": updates,
    }


def repair_creates_parented_to_deleted_nodes(
    structural_operations: list[dict[str, Any]],
    *,
    nodes: list[StrategyNode],
) -> tuple[list[dict[str, Any]], list[str]]:
    deleted_node_ids = {
        int(operation["node_id"])
        for operation in structural_operations
        if operation.get("kind") == "delete_node" and isinstance(operation.get("node_id"), int)
    }
    if not deleted_node_ids:
        return structural_operations, []

    nodes_by_id = {
        int(node.id): node
        for node in nodes
        if isinstance(node.id, int)
    }
    repaired_operations: list[dict[str, Any]] = []
    repaired_any = False
    for operation in structural_operations:
        if operation.get("kind") != "create_node":
            repaired_operations.append(operation)
            continue
        parent_id = operation.get("parent_id")
        if not isinstance(parent_id, int) or parent_id not in deleted_node_ids:
            repaired_operations.append(operation)
            continue
        deleted_parent = nodes_by_id.get(parent_id)
        repaired_operation = dict(operation)
        replacement_parent_id = deleted_parent.parent_id if deleted_parent is not None else None
        repaired_operation["parent_id"] = replacement_parent_id
        if replacement_parent_id is None:
            repaired_operation["parent_title"] = "top level"
        else:
            replacement_parent = nodes_by_id.get(replacement_parent_id)
            if replacement_parent is not None:
                repaired_operation["parent_title"] = replacement_parent.title
            else:
                repaired_operation.pop("parent_title", None)
        repaired_operations.append(repaired_operation)
        repaired_any = True

    if not repaired_any:
        return structural_operations, []
    return repaired_operations, [
        "Adjusted replacement creates so they land at the deleted task's former level instead of under the task being deleted."
    ]


def normalize_top_level_create_branch_proposal(
    raw_proposal: dict[str, Any],
    *,
    evidence_bundle: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    raw_operations = raw_proposal.get(
        "structural_operations",
        raw_proposal.get("structuralOperations"),
    )
    if not isinstance(raw_operations, list):
        return None
    category_id = _category_id_from_evidence(
        raw_proposal,
        evidence_bundle=evidence_bundle,
    )
    category_name = _category_name_from_evidence(
        raw_proposal,
        evidence_bundle=evidence_bundle,
    )
    normalized_operations: list[dict[str, Any]] = []
    parent_title: str | None = None
    parent_notes: str | None = None
    child_titles: list[str] = []
    parent_operation_index: int | None = None
    flat_child_candidate_indices: list[int] = []

    def append_child_title(title: str) -> None:
        if not title.strip():
            return
        normalized_title = normalize_title_key(title)
        if normalize_title_key(parent_title or "") == normalized_title:
            return
        if all(normalize_title_key(existing) != normalized_title for existing in child_titles):
            child_titles.append(title)

    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict):
            continue
        raw_operation = normalize_structural_operation_envelope(raw_operation)
        kind = normalize_structural_operation_kind(
            first_present(raw_operation, "kind", "type", "operation", "op")
        )
        if kind != "create_node":
            continue
        title = normalize_structural_operation_text(raw_operation.get("title"))
        operation_category_id = coerce_int_id(
            first_present(raw_operation, "category_id", "categoryId")
        )
        if operation_category_id is None:
            operation_category_id = _category_id_from_evidence(
                raw_operation,
                evidence_bundle=evidence_bundle,
            )
        operation_category_id = operation_category_id or category_id
        if not title or operation_category_id is None:
            continue
        parent_id = coerce_int_id(
            first_present(raw_operation, "parent_id", "parentId", "parent_task_id", "parentTaskId")
        )
        parent_temp_id = normalize_structural_operation_text(
            first_present(raw_operation, "parent_temp_id", "parentTempId")
        )
        parent_title_hint = normalize_structural_operation_text(
            first_present(
                raw_operation,
                "parent_title",
                "parentTitle",
                "parent_task_title",
                "parentTaskTitle",
            )
        )
        temp_id = normalize_structural_operation_text(
            first_present(raw_operation, "temp_id", "tempId")
        )
        operation: dict[str, Any] = {
            "kind": "create_node",
            "title": title,
            "category_id": int(operation_category_id),
            "parent_id": parent_id,
            "parent_temp_id": parent_temp_id or None,
        }
        if temp_id:
            operation["temp_id"] = temp_id
        if parent_title_hint:
            operation["parent_title"] = parent_title_hint
        notes = normalize_structural_operation_text(raw_operation.get("notes"))
        if notes:
            operation["notes"] = notes
        deadline_at = normalize_structural_operation_text(
            first_present(raw_operation, "deadline_at", "deadlineAt")
        )
        if deadline_at:
            operation["deadline_at"] = deadline_at
        priority = normalize_structural_operation_text(raw_operation.get("priority")).lower()
        if priority in VALID_PRIORITIES:
            operation["priority"] = priority
        status = normalize_structural_operation_text(raw_operation.get("status")).lower()
        if status in VALID_STATUSES:
            operation["status"] = status
        estimate = coerce_int_id(
            first_present(raw_operation, "time_estimate_minutes", "timeEstimateMinutes")
        )
        if estimate is not None:
            operation["time_estimate_minutes"] = estimate
        node_type = normalize_structural_operation_text(
            first_present(raw_operation, "node_type", "nodeType")
        ).lower()
        if node_type in VALID_NODE_TYPES:
            operation["node_type"] = node_type
        emoji = normalize_structural_operation_text(raw_operation.get("emoji"))
        if emoji:
            operation["emoji"] = emoji
        tags = raw_operation.get("tags")
        if isinstance(tags, list):
            operation["tags"] = [str(item).strip() for item in tags if str(item).strip()]
        is_pinned = first_present(raw_operation, "is_pinned", "isPinned")
        if isinstance(is_pinned, bool):
            operation["is_pinned"] = is_pinned
        is_backlog = first_present(raw_operation, "is_backlog", "isBacklog")
        if isinstance(is_backlog, bool):
            operation["is_backlog"] = is_backlog
        normalized_operations.append(operation)
        operation_index = len(normalized_operations) - 1
        if parent_id is None and not parent_temp_id and parent_title is None:
            parent_title = title
            parent_notes = notes or None
            parent_operation_index = operation_index
            if category_id is None:
                category_id = int(operation_category_id)
        elif parent_id is None and not parent_temp_id:
            flat_child_candidate_indices.append(operation_index)
        elif parent_id is not None or parent_temp_id:
            append_child_title(title)

    if parent_title and parent_operation_index is not None:
        parent_operation = normalized_operations[parent_operation_index]
        parent_temp_id = (
            str(parent_operation.get("temp_id") or "").strip()
            or _temp_id_from_title(parent_title)
        )
        has_nested_intent = _proposal_requests_nested_top_level_create(raw_proposal)
        repaired_child_indices = [
            index
            for index in flat_child_candidate_indices
            if (
                has_nested_intent
                or _same_title(normalized_operations[index].get("parent_title"), parent_title)
            )
        ]
        proposed_child_titles = [
            title
            for title in _raw_string_list(
                raw_proposal.get("proposed_children", raw_proposal.get("proposedChildren"))
            )
            if not _same_title(title, parent_title)
        ]
        if repaired_child_indices or (has_nested_intent and proposed_child_titles):
            parent_operation["temp_id"] = parent_temp_id
            for index in repaired_child_indices:
                operation = normalized_operations[index]
                operation["parent_temp_id"] = parent_temp_id
                operation["parent_title"] = parent_title
                append_child_title(str(operation.get("title") or ""))
            for title in proposed_child_titles:
                if any(
                    _same_title(title, operation.get("title"))
                    for operation in normalized_operations
                    if isinstance(operation, dict)
                ):
                    append_child_title(title)
                    continue
                normalized_operations.append(
                    {
                        "kind": "create_node",
                        "title": title,
                        "category_id": int(category_id) if category_id is not None else None,
                        "parent_id": None,
                        "parent_temp_id": parent_temp_id,
                        "parent_title": parent_title,
                    }
                )
                append_child_title(title)

    if category_id is None or not parent_title or not normalized_operations:
        return None
    proposal_id = (
        raw_proposal.get("proposal_id").strip()
        if isinstance(raw_proposal.get("proposal_id"), str) and raw_proposal.get("proposal_id").strip()
        else None
    )
    rationale = (
        raw_proposal.get("rationale").strip()
        if isinstance(raw_proposal.get("rationale"), str) and raw_proposal.get("rationale").strip()
        else "Review this new task branch before applying it."
    )
    summary = (
        raw_proposal.get("summary").strip()
        if isinstance(raw_proposal.get("summary"), str) and raw_proposal.get("summary").strip()
        else None
    )
    validation_warnings = (
        raw_proposal.get("validation_warnings")
        if isinstance(raw_proposal.get("validation_warnings"), list)
        else []
    )
    if child_titles:
        proposal = build_top_level_task_set_proposal(
            category_id=category_id,
            category_name=category_name,
            parent_title=parent_title,
            child_titles=child_titles,
            notes=parent_notes,
            rationale=rationale,
            evidence_bundle=evidence_bundle,
            evidence_override=normalize_proposal_evidence(raw_proposal.get("evidence")),
            validation_warnings=validation_warnings,
            summary=summary,
            proposal_id=proposal_id,
        )
    else:
        proposal = build_top_level_create_task_proposal(
            category_id=category_id,
            category_name=category_name,
            task_title=parent_title,
            notes=parent_notes,
            rationale=rationale,
            evidence_bundle=evidence_bundle,
            evidence_override=normalize_proposal_evidence(raw_proposal.get("evidence")),
            validation_warnings=validation_warnings,
            summary=summary,
            proposal_id=proposal_id,
        )
    if proposal is None:
        return None
    proposal["structural_operations"] = normalized_operations
    proposal["proposed_children"] = [parent_title, *child_titles]
    raw_diff_preview = normalize_proposal_diff_preview(raw_proposal.get("diff_preview"))
    if raw_diff_preview:
        proposal["diff_preview"] = raw_diff_preview
    return proposal


def normalize_metadata_proposal(
    raw_proposal: Any,
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    evidence_bundle: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw_proposal, dict):
        return None
    updates = normalize_metadata_updates(raw_proposal.get("updates"))
    target_updates = normalize_metadata_target_updates(
        raw_proposal.get("target_updates", raw_proposal.get("targetUpdates"))
    )
    if not updates and not target_updates:
        return None
    rationale = raw_proposal.get("rationale")
    normalized_rationale = (
        rationale.strip()
        if isinstance(rationale, str) and rationale.strip()
        else "Review this metadata patch before applying it."
    )
    validation_warnings = (
        raw_proposal.get("validation_warnings")
        if isinstance(raw_proposal.get("validation_warnings"), list)
        else []
    )
    summary = (
        raw_proposal.get("summary").strip()
        if isinstance(raw_proposal.get("summary"), str) and raw_proposal.get("summary").strip()
        else None
    )
    badge_suggestions = normalize_badge_suggestions(
        raw_proposal.get("badge_suggestions", raw_proposal.get("badgeSuggestions"))
    )
    proposal_id = (
        raw_proposal.get("proposal_id").strip()
        if isinstance(raw_proposal.get("proposal_id"), str) and raw_proposal.get("proposal_id").strip()
        else None
    )
    target_node_ids = raw_target_node_ids_from_proposal(raw_proposal)
    target_nodes = [
        node
        for node_id in target_node_ids
        for node in nodes
        if node.id == node_id
    ]
    if len(target_nodes) > 1:
        target_title = first_present(
            raw_proposal,
            "target_node_title",
            "targetNodeTitle",
            "target_task_title",
            "targetTaskTitle",
        )
        target_label = (
            target_title.strip()
            if isinstance(target_title, str) and target_title.strip()
            else f"{len(target_nodes)} tasks"
        )
        proposal = build_bulk_metadata_patch_proposal(
            target_nodes=target_nodes,
            target_label=target_label,
            updates=updates,
            target_updates=target_updates,
            rationale=normalized_rationale,
            evidence_bundle=evidence_bundle,
            evidence_override=normalize_proposal_evidence(raw_proposal.get("evidence")),
            validation_warnings=validation_warnings,
            summary=summary,
            proposal_id=proposal_id,
        )
        if badge_suggestions:
            proposal["badge_suggestions"] = badge_suggestions
        return proposal

    target_node = target_nodes[0] if target_nodes else resolve_proposal_target_node(
        raw_proposal,
        nodes=nodes,
        fallback_focus_node=focus_node,
    )
    if target_node is None or target_node.id is None:
        return None
    single_updates = dict(updates)
    if target_updates and target_updates[0].get("target_node_id") == target_node.id:
        single_updates.update(target_updates[0].get("updates", {}))
    if not single_updates:
        return None
    proposal = build_metadata_patch_proposal(
        target_node=target_node,
        updates=single_updates,
        rationale=normalized_rationale,
        evidence_bundle=evidence_bundle,
        evidence_override=normalize_proposal_evidence(raw_proposal.get("evidence")),
        diff_preview_override=normalize_proposal_diff_preview(raw_proposal.get("diff_preview")),
        validation_warnings=validation_warnings,
        summary=summary,
        proposal_id=proposal_id,
    )
    if badge_suggestions:
        proposal["badge_suggestions"] = badge_suggestions
    return proposal


def _preserve_recurring_schedule(
    proposal: dict[str, Any] | None,
    raw_proposal: dict[str, Any],
) -> dict[str, Any] | None:
    if proposal is None:
        return None
    recurring_schedule = raw_proposal.get(
        "recurring_schedule",
        raw_proposal.get("recurringSchedule"),
    )
    if isinstance(recurring_schedule, dict):
        proposal["recurring_schedule"] = recurring_schedule
        proposal["proposal_kind"] = "create_and_schedule"
    created_task_schedule = raw_proposal.get(
        "created_task_schedule",
        raw_proposal.get("createdTaskSchedule"),
    )
    if isinstance(created_task_schedule, dict):
        proposal["created_task_schedule"] = created_task_schedule
        proposal["proposal_kind"] = "create_and_schedule"
    return proposal


def normalize_branch_proposal(
    raw_proposal: Any,
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    evidence_bundle: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw_proposal, dict):
        return None
    raw_operations = raw_proposal.get(
        "structural_operations",
        raw_proposal.get("structuralOperations"),
    )
    proposal_kind_text = str(
        raw_proposal.get("proposal_kind", raw_proposal.get("proposalKind")) or ""
    ).strip().lower()
    raw_target_node_id = coerce_int_id(
        first_present(
            raw_proposal,
            "target_node_id",
            "targetNodeId",
            "target_task_id",
            "targetTaskId",
        )
    )
    raw_target_node_title = first_present(
        raw_proposal,
        "target_node_title",
        "targetNodeTitle",
        "target_task_title",
        "targetTaskTitle",
    )
    raw_target_category_id = coerce_int_id(
        first_present(
            raw_proposal,
            "target_category_id",
            "targetCategoryId",
            "category_id",
            "categoryId",
        )
    )
    raw_target_category_name = first_present(
        raw_proposal,
        "target_category_name",
        "targetCategoryName",
        "category_name",
        "categoryName",
    )
    _, active_category_name = _active_category_from_evidence(evidence_bundle)
    resolved_target_category_name = _category_name_from_evidence(
        raw_proposal,
        evidence_bundle=evidence_bundle,
    )
    target_title_is_category = bool(
        isinstance(raw_target_node_title, str)
        and raw_target_node_title.strip()
        and any(
            isinstance(category_name, str)
            and _normalized_category_name(raw_target_node_title)
            == _normalized_category_name(category_name)
            for category_name in (
                raw_target_category_name,
                resolved_target_category_name,
                active_category_name,
            )
        )
    )
    has_explicit_target_category = raw_target_category_id is not None or bool(
        isinstance(raw_target_category_name, str) and raw_target_category_name.strip()
    )
    normalized_operation_envelopes = (
        [
            normalize_structural_operation_envelope(item)
            for item in raw_operations
            if isinstance(item, dict)
        ]
        if isinstance(raw_operations, list)
        else []
    )
    operation_kinds = [
        normalize_structural_operation_kind(
            first_present(item, "kind", "type", "operation", "op")
        )
        for item in normalized_operation_envelopes
    ]
    has_unparented_create = any(
        kind == "create_node"
        and coerce_int_id(
            first_present(
                item,
                "parent_id",
                "parentId",
                "parent_task_id",
                "parentTaskId",
            )
        )
        is None
        and not normalize_structural_operation_text(
            first_present(item, "parent_temp_id", "parentTempId")
        )
        and not normalize_structural_operation_text(
            first_present(
                item,
                "parent_title",
                "parentTitle",
                "parent_task_title",
                "parentTaskTitle",
            )
        )
        for item, kind in zip(normalized_operation_envelopes, operation_kinds)
    )
    explicit_top_level_create_shape = bool(
        raw_target_node_id is None
        and not (
            isinstance(raw_target_node_title, str)
            and raw_target_node_title.strip()
            and not target_title_is_category
        )
        and has_explicit_target_category
        and operation_kinds
        and all(kind == "create_node" for kind in operation_kinds)
        and has_unparented_create
    )
    if (
        (
            proposal_kind_text
            in {"create", "create_node", "create_task", "top_level_create"}
            or explicit_top_level_create_shape
        )
        and raw_target_node_id is None
        and (
            not (isinstance(raw_target_node_title, str) and raw_target_node_title.strip())
            or target_title_is_category
        )
    ):
        top_level_create = normalize_top_level_create_branch_proposal(
            raw_proposal,
            evidence_bundle=evidence_bundle,
        )
        if top_level_create is not None:
            return _preserve_recurring_schedule(top_level_create, raw_proposal)
    target_node = resolve_proposal_target_node(
        raw_proposal,
        nodes=nodes,
        fallback_focus_node=focus_node,
    )
    if target_node is None or target_node.id is None:
        top_level_create = normalize_top_level_create_branch_proposal(
            raw_proposal,
            evidence_bundle=evidence_bundle,
        )
        if top_level_create is not None:
            return _preserve_recurring_schedule(top_level_create, raw_proposal)
        return None
    if isinstance(raw_operations, list) and any(isinstance(item, dict) for item in raw_operations):
        structural_operations: list[dict[str, Any]] = []
        for raw_operation in raw_operations:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_structural_operation(
                raw_operation,
                target_node=target_node,
                nodes=nodes,
                evidence_bundle=evidence_bundle,
            )
            if operation is None:
                continue
            structural_operations.append(operation)
        if structural_operations:
            repair_warnings: list[str]
            structural_operations, repair_warnings = repair_creates_parented_to_deleted_nodes(
                structural_operations,
                nodes=nodes,
            )
            raw_validation_warnings = (
                raw_proposal.get("validation_warnings")
                if isinstance(raw_proposal.get("validation_warnings"), list)
                else []
            )
            validation_warnings = [
                item
                for item in raw_validation_warnings
                if isinstance(item, str) and item.strip()
            ]
            for warning in repair_warnings:
                if warning not in validation_warnings:
                    validation_warnings.append(warning)
            return _preserve_recurring_schedule(build_subtree_rewrite_proposal(
                target_node=target_node,
                structural_operations=structural_operations,
                rationale=(
                    raw_proposal.get("rationale").strip()
                    if isinstance(raw_proposal.get("rationale"), str)
                    and raw_proposal.get("rationale").strip()
                    else "Review this ordered task-tree mutation before applying it."
                ),
                evidence_bundle=evidence_bundle,
                evidence_override=normalize_proposal_evidence(raw_proposal.get("evidence")),
                validation_warnings=validation_warnings,
                summary=(
                    raw_proposal.get("summary").strip()
                    if isinstance(raw_proposal.get("summary"), str)
                    and raw_proposal.get("summary").strip()
                    else None
                ),
                proposal_id=(
                    raw_proposal.get("proposal_id").strip()
                    if isinstance(raw_proposal.get("proposal_id"), str)
                    and raw_proposal.get("proposal_id").strip()
                    else None
                ),
            ), raw_proposal)
    raw_children = raw_proposal.get("proposed_children", raw_proposal.get("proposedChildren"))
    if not isinstance(raw_children, list):
        return None
    proposed_children = [
        item.strip() for item in raw_children if isinstance(item, str) and item.strip()
    ]
    if not proposed_children:
        return None
    rationale = raw_proposal.get("rationale")
    proposal_kind = (
        "create"
        if str(raw_proposal.get("proposal_kind", raw_proposal.get("proposalKind")) or "").strip().lower() == "create"
        else "branch"
    )
    return _preserve_recurring_schedule(build_branch_children_proposal(
        target_node=target_node,
        proposed_children=proposed_children[:8],
        rationale=(
            rationale.strip()
            if isinstance(rationale, str) and rationale.strip()
            else "These are concrete child-task proposals for review."
        ),
        proposal_kind=proposal_kind,
        evidence_bundle=evidence_bundle,
        evidence_override=normalize_proposal_evidence(raw_proposal.get("evidence")),
        diff_preview_override=normalize_proposal_diff_preview(raw_proposal.get("diff_preview")),
        validation_warnings=(
            raw_proposal.get("validation_warnings")
            if isinstance(raw_proposal.get("validation_warnings"), list)
            else []
        ),
        summary=(
            raw_proposal.get("summary").strip()
            if isinstance(raw_proposal.get("summary"), str) and raw_proposal.get("summary").strip()
            else None
        ),
        proposal_id=(
            raw_proposal.get("proposal_id").strip()
            if isinstance(raw_proposal.get("proposal_id"), str) and raw_proposal.get("proposal_id").strip()
            else None
        ),
    ), raw_proposal)


__all__ = [
    "normalize_branch_proposal",
    "normalize_metadata_proposal",
]
