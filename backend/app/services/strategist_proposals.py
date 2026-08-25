"""
Shared Strategist proposal builders.

These helpers standardize reviewable proposal payloads around the richer
proposal envelope from the LangGraph RFC while preserving the existing
metadata / branch / action response contract.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from app.models.node import StrategyNode
from app.services.retrieval import (
    format_deadline as retrieval_format_deadline,
    format_estimate as retrieval_format_estimate,
    priority_label,
)

ProposalKind = Literal["metadata", "branch", "create", "move", "delete", "reorder", "restructure", "mixed"]


def _normalize_string_list(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    normalized: list[str] = []
    for item in raw_value:
        if isinstance(item, str):
            trimmed = item.strip()
            if trimmed and trimmed not in normalized:
                normalized.append(trimmed)
    return normalized


def _decode_tags(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    return _normalize_string_list(parsed)


def normalize_proposal_evidence(raw_evidence: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_evidence, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            continue
        source = str(raw_item.get("source") or "").strip()
        detail = str(raw_item.get("detail") or "").strip()
        if not source or not detail:
            continue
        item: dict[str, Any] = {
            "source": source,
            "detail": detail,
        }
        task_id = raw_item.get("task_id")
        if isinstance(task_id, int):
            item["task_id"] = task_id
        task_title = raw_item.get("task_title")
        if isinstance(task_title, str) and task_title.strip():
            item["task_title"] = task_title.strip()
        relationship = raw_item.get("relationship")
        if isinstance(relationship, str) and relationship.strip():
            item["relationship"] = relationship.strip()
        score = raw_item.get("score")
        if isinstance(score, (int, float)):
            item["score"] = round(float(score), 4)
        normalized.append(item)
    return normalized


def normalize_proposal_diff_preview(raw_diff_preview: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_diff_preview, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_item in raw_diff_preview:
        if not isinstance(raw_item, dict):
            continue
        kind = str(raw_item.get("kind") or "").strip()
        label = str(raw_item.get("label") or "").strip()
        if not kind or not label:
            continue
        item: dict[str, Any] = {
            "kind": kind,
            "label": label,
        }
        detail = raw_item.get("detail")
        if isinstance(detail, str) and detail.strip():
            item["detail"] = detail.strip()
        target_node_id = raw_item.get("target_node_id")
        if isinstance(target_node_id, int):
            item["target_node_id"] = target_node_id
        before = raw_item.get("before")
        if isinstance(before, str) and before.strip():
            item["before"] = before.strip()
        after = raw_item.get("after")
        if isinstance(after, str) and after.strip():
            item["after"] = after.strip()
        normalized.append(item)
    return normalized


def _fallback_evidence_for_node(node: StrategyNode) -> list[dict[str, Any]]:
    return [
        {
            "source": "task_snapshot",
            "detail": (
                f"{priority_label(node.priority)} | "
                f"{retrieval_format_deadline(node.deadline_at)} | "
                f"{retrieval_format_estimate(node.time_estimate_minutes)}"
            ),
            "task_id": node.id,
            "task_title": node.title,
        }
    ]


def _select_evidence(
    *,
    evidence_bundle: dict[str, Any] | None,
    evidence_override: list[dict[str, Any]] | None,
    target_node_ids: list[int],
    fallback_node: StrategyNode | None,
) -> list[dict[str, Any]]:
    if evidence_override:
        return evidence_override[:4]

    selected: list[dict[str, Any]] = []
    sources = (
        evidence_bundle.get("graph_evidence", []) if isinstance(evidence_bundle, dict) else [],
        evidence_bundle.get("note_evidence", []) if isinstance(evidence_bundle, dict) else [],
        evidence_bundle.get("recent_action_evidence", []) if isinstance(evidence_bundle, dict) else [],
    )
    for source_group in sources:
        if not isinstance(source_group, list):
            continue
        for raw_item in source_group:
            if not isinstance(raw_item, dict):
                continue
            task_id = raw_item.get("task_id")
            if target_node_ids and isinstance(task_id, int) and task_id not in target_node_ids:
                continue
            normalized = normalize_proposal_evidence([raw_item])
            if not normalized:
                continue
            item = normalized[0]
            if item not in selected:
                selected.append(item)
            if len(selected) >= 4:
                return selected

    if not selected and isinstance(evidence_bundle, dict):
        for key in ("graph_evidence", "note_evidence", "recent_action_evidence"):
            normalized = normalize_proposal_evidence(evidence_bundle.get(key))
            for item in normalized:
                if item not in selected:
                    selected.append(item)
                if len(selected) >= 4:
                    return selected

    if not selected and fallback_node is not None:
        selected.extend(_fallback_evidence_for_node(fallback_node))
    return selected[:4]


def _metadata_field_label(field_name: str) -> str:
    labels = {
        "title": "title",
        "notes": "notes",
        "status": "status",
        "priority": "priority",
        "deadline_at": "deadline",
        "time_estimate_minutes": "estimate",
        "tags": "tags",
        "is_pinned": "pinned state",
        "node_type": "task type",
    }
    return labels.get(field_name, field_name.replace("_", " "))


def _format_metadata_value(field_name: str, value: Any) -> str:
    if field_name == "deadline_at":
        return retrieval_format_deadline(value if isinstance(value, str) else None)
    if field_name == "time_estimate_minutes":
        return retrieval_format_estimate(int(value) if isinstance(value, int) else None)
    if field_name == "priority":
        return priority_label(value) if isinstance(value, str) else "No priority"
    if field_name == "status":
        if not isinstance(value, str):
            return "No status"
        return value.replace("-", " ").title()
    if field_name == "tags":
        if isinstance(value, list):
            return ", ".join(_normalize_string_list(value)) or "No tags"
        return "No tags"
    if field_name == "is_pinned":
        return "Pinned" if bool(value) else "Not pinned"
    if field_name == "node_type":
        if not isinstance(value, str):
            return "Task"
        return "Habit" if value == "habit" else "Task"
    if field_name == "notes":
        if not isinstance(value, str) or not value.strip():
            return "No notes"
        compact = " ".join(value.strip().split())
        return compact[:96] + ("..." if len(compact) > 96 else "")
    if field_name == "title":
        return value.strip() if isinstance(value, str) and value.strip() else "Untitled"
    return str(value).strip() if value is not None else "Unset"


def _existing_metadata_value(node: StrategyNode, field_name: str) -> Any:
    if field_name == "tags":
        return _decode_tags(node.tags)
    return getattr(node, field_name, None)


def build_metadata_diff_preview(
    *,
    target_node: StrategyNode,
    updates: dict[str, Any],
) -> list[dict[str, Any]]:
    diff_preview: list[dict[str, Any]] = []
    for field_name, next_value in updates.items():
        before = _format_metadata_value(field_name, _existing_metadata_value(target_node, field_name))
        after = _format_metadata_value(field_name, next_value)
        diff_preview.append(
            {
                "kind": "metadata_update",
                "label": f"Change {_metadata_field_label(field_name)}",
                "detail": f"{before} -> {after}",
                "target_node_id": target_node.id,
                "before": before,
                "after": after,
            }
        )
    return diff_preview


def _preview_lines_from_diff(diff_preview: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in diff_preview:
        label = str(item.get("label") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if detail:
            lines.append(detail)
        elif label:
            lines.append(label)
    return lines


def _proposal_fields(
    *,
    proposal_id: str | None,
    proposal_kind: ProposalKind,
    summary: str,
    rationale: str,
    target_node_ids: list[int],
    subtree_root_ids: list[int],
    evidence_bundle: dict[str, Any] | None,
    evidence_override: list[dict[str, Any]] | None,
    diff_preview: list[dict[str, Any]],
    validation_warnings: list[str] | None,
    fallback_node: StrategyNode | None,
) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "proposal_kind": proposal_kind,
        "summary": summary,
        "rationale": rationale,
        "evidence": _select_evidence(
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            target_node_ids=target_node_ids,
            fallback_node=fallback_node,
        ),
        "diff_preview": diff_preview,
        "validation_warnings": _normalize_string_list(validation_warnings),
        "target_node_ids": target_node_ids,
        "subtree_root_ids": subtree_root_ids,
    }
    if proposal_id:
        proposal["proposal_id"] = proposal_id
    return proposal


def build_metadata_patch_proposal(
    *,
    target_node: StrategyNode,
    updates: dict[str, Any],
    rationale: str,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    diff_preview_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    diff_preview = diff_preview_override or build_metadata_diff_preview(
        target_node=target_node,
        updates=updates,
    )
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="metadata",
            summary=summary or f"Update {target_node.title}",
            rationale=rationale,
            target_node_ids=[target_node.id] if target_node.id is not None else [],
            subtree_root_ids=[target_node.id] if target_node.id is not None else [],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=target_node,
        ),
        "target_node_id": target_node.id,
        "target_node_title": target_node.title,
        "updates": updates,
        "preview": _preview_lines_from_diff(diff_preview),
    }


def build_bulk_metadata_patch_proposal(
    *,
    target_nodes: list[StrategyNode],
    target_label: str,
    updates: dict[str, Any],
    target_updates: list[dict[str, Any]] | None = None,
    rationale: str,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    concrete_targets = [
        node for node in target_nodes if node.id is not None
    ]
    primary = concrete_targets[0] if concrete_targets else None
    target_node_ids = [int(node.id) for node in concrete_targets if node.id is not None]
    updates_by_node_id: dict[int, dict[str, Any]] = {}
    if isinstance(target_updates, list):
        for raw_item in target_updates:
            if not isinstance(raw_item, dict):
                continue
            raw_node_id = raw_item.get("target_node_id")
            raw_updates = raw_item.get("updates")
            if isinstance(raw_node_id, int) and isinstance(raw_updates, dict) and raw_updates:
                updates_by_node_id[raw_node_id] = dict(raw_updates)
    merged_updates_by_node_id: dict[int, dict[str, Any]] = {}
    diff_preview: list[dict[str, Any]] = []
    for node in concrete_targets:
        node_updates = dict(updates)
        if node.id is not None:
            node_updates.update(updates_by_node_id.get(int(node.id), {}))
            merged_updates_by_node_id[int(node.id)] = dict(node_updates)
        for item in build_metadata_diff_preview(target_node=node, updates=node_updates):
            label = str(item.get("label") or "Change metadata").strip()
            item["label"] = f"{label} on {node.title}"
            diff_preview.append(item)
    normalized_target_updates: list[dict[str, Any]] = []
    if updates_by_node_id:
        for node in concrete_targets:
            if node.id is None:
                continue
            final_updates = merged_updates_by_node_id.get(int(node.id), {})
            if not final_updates:
                continue
            normalized_target_updates.append(
                {
                    "target_node_id": int(node.id),
                    "target_node_title": node.title,
                    "updates": final_updates,
                }
            )
    display_updates = dict(updates)
    if normalized_target_updates:
        common_updates = dict(normalized_target_updates[0]["updates"])
        for item in normalized_target_updates[1:]:
            item_updates = item["updates"]
            for field_name in list(common_updates.keys()):
                if item_updates.get(field_name) != common_updates[field_name]:
                    common_updates.pop(field_name, None)
        display_updates = common_updates
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="metadata",
            summary=summary or f"Update {target_label}",
            rationale=rationale,
            target_node_ids=target_node_ids,
            subtree_root_ids=target_node_ids[:],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=primary,
        ),
        "target_node_id": primary.id if primary is not None else None,
        "target_node_title": target_label,
        "updates": display_updates,
        "target_updates": normalized_target_updates,
        "preview": _preview_lines_from_diff(diff_preview),
    }


def build_branch_children_proposal(
    *,
    target_node: StrategyNode,
    proposed_children: list[str],
    rationale: str,
    proposal_kind: Literal["branch", "create"] = "branch",
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    diff_preview_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    diff_preview = diff_preview_override or [
        {
            "kind": "create_node",
            "label": f"Create {child_title}",
            "detail": f"Add under {target_node.title}",
            "target_node_id": target_node.id,
            "after": child_title,
        }
        for child_title in proposed_children
    ]
    default_summary = (
        f"Create {len(proposed_children)} task{'s' if len(proposed_children) != 1 else ''} under {target_node.title}"
        if proposal_kind == "create"
        else f"Break down {target_node.title} into {len(proposed_children)} child task{'s' if len(proposed_children) != 1 else ''}"
    )
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind=proposal_kind,
            summary=summary or default_summary,
            rationale=rationale,
            target_node_ids=[target_node.id] if target_node.id is not None else [],
            subtree_root_ids=[target_node.id] if target_node.id is not None else [],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=target_node,
        ),
        "target_node_id": target_node.id,
        "target_category_id": target_node.category_id,
        "target_node_title": target_node.title,
        "proposed_children": proposed_children,
    }


def build_create_task_proposal(
    *,
    parent_node: StrategyNode,
    task_title: str,
    rationale: str,
    notes: str | None = None,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    proposal = build_branch_children_proposal(
        target_node=parent_node,
        proposed_children=[task_title],
        rationale=rationale,
        proposal_kind="create",
        evidence_bundle=evidence_bundle,
        evidence_override=evidence_override,
        validation_warnings=validation_warnings,
        summary=summary or f'Create "{task_title}" under {parent_node.title}',
        proposal_id=proposal_id,
    )
    normalized_notes = notes.strip() if isinstance(notes, str) and notes.strip() else None
    if normalized_notes is not None and parent_node.id is not None:
        proposal["structural_operations"] = [
            {
                "kind": "create_node",
                "title": task_title,
                "category_id": parent_node.category_id,
                "parent_id": parent_node.id,
                "notes": normalized_notes,
            }
        ]
        proposal["diff_preview"].append(
            {
                "kind": "metadata_update",
                "label": "Add notes",
                "detail": "Seed notes with logistics, ferry/weather checks, packing, itinerary, and return timing.",
                "target_node_id": parent_node.id,
                "after": "notes",
            }
        )
    return proposal


def build_top_level_create_task_proposal(
    *,
    category_id: int,
    category_name: str | None,
    task_title: str,
    rationale: str,
    notes: str | None = None,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    normalized_category_name = (
        category_name.strip()
        if isinstance(category_name, str) and category_name.strip()
        else "the active category"
    )
    normalized_notes = notes.strip() if isinstance(notes, str) and notes.strip() else None
    structural_operation: dict[str, Any] = {
        "kind": "create_node",
        "title": task_title,
        "category_id": category_id,
        "parent_id": None,
    }
    if normalized_notes is not None:
        structural_operation["notes"] = normalized_notes

    diff_preview = [
        {
            "kind": "create_node",
            "label": f"Create {task_title}",
            "detail": f"Add as a top-level task in {normalized_category_name}",
            "after": task_title,
        }
    ]
    if normalized_notes is not None:
        diff_preview.append(
            {
                "kind": "metadata_update",
                "label": "Add notes",
                "detail": "Seed notes with logistics, ferry/weather checks, packing, itinerary, and return timing.",
                "after": "notes",
            }
        )

    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="create",
            summary=summary or f'Create "{task_title}" in {normalized_category_name}',
            rationale=rationale,
            target_node_ids=[],
            subtree_root_ids=[],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=None,
        ),
        "target_node_id": None,
        "target_category_id": category_id,
        "target_node_title": normalized_category_name,
        "proposed_children": [task_title],
        "structural_operations": [structural_operation],
    }


def build_top_level_task_set_proposal(
    *,
    category_id: int,
    category_name: str | None,
    parent_title: str,
    child_titles: list[str],
    rationale: str,
    notes: str | None = None,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any] | None:
    normalized_children = [
        title.strip()
        for title in child_titles
        if isinstance(title, str) and title.strip()
    ]
    if not normalized_children:
        return None
    normalized_category_name = (
        category_name.strip()
        if isinstance(category_name, str) and category_name.strip()
        else "the active category"
    )
    parent_temp_id = "new_parent"
    normalized_notes = notes.strip() if isinstance(notes, str) and notes.strip() else None
    structural_operations: list[dict[str, Any]] = [
        {
            "kind": "create_node",
            "temp_id": parent_temp_id,
            "title": parent_title,
            "category_id": category_id,
            "parent_id": None,
            **({"notes": normalized_notes} if normalized_notes is not None else {}),
        }
    ]
    diff_preview = [
        {
            "kind": "create_node",
            "label": f"Create {parent_title}",
            "detail": f"Add as a top-level task in {normalized_category_name}",
            "after": parent_title,
        }
    ]
    if normalized_notes is not None:
        diff_preview.append(
            {
                "kind": "metadata_update",
                "label": f"Add notes to {parent_title}",
                "detail": "Seed notes with logistics, ferry/weather checks, packing, itinerary, and return timing.",
                "after": "notes",
            }
        )
    for child_title in normalized_children:
        structural_operations.append(
            {
                "kind": "create_node",
                "title": child_title,
                "category_id": category_id,
                "parent_temp_id": parent_temp_id,
            }
        )
        diff_preview.append(
            {
                "kind": "create_node",
                "label": f"Create {child_title}",
                "detail": f"Add under {parent_title}",
                "after": child_title,
            }
        )

    proposed_children = [parent_title, *normalized_children]
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="create",
            summary=summary or f'Create "{parent_title}" with {len(normalized_children)} subtasks in {normalized_category_name}',
            rationale=rationale,
            target_node_ids=[],
            subtree_root_ids=[],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=None,
        ),
        "target_node_id": None,
        "target_category_id": category_id,
        "target_node_title": normalized_category_name,
        "proposed_children": proposed_children,
        "structural_operations": structural_operations,
    }


def build_delete_node_proposal(
    *,
    target_node: StrategyNode,
    category_name: str,
    descendant_count: int,
    consequences: list[str],
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    warnings = _normalize_string_list(validation_warnings)
    if descendant_count > 0:
        warnings.append(
            f"This will also delete {descendant_count} nested subtask{'s' if descendant_count != 1 else ''}."
        )
    diff_preview = [
        {
            "kind": "delete_node",
            "label": f"Delete {target_node.title}",
            "detail": f"Remove from {category_name}",
            "target_node_id": target_node.id,
            "before": target_node.title,
        }
    ]
    if descendant_count > 0:
        diff_preview.append(
            {
                "kind": "delete_descendants",
                "label": "Delete nested subtasks",
                "detail": (
                    f"{descendant_count} nested subtask"
                    f"{'s' if descendant_count != 1 else ''} will also be removed"
                ),
                "target_node_id": target_node.id,
            }
        )
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="delete",
            summary=summary or f"Delete {target_node.title}",
            rationale="Deletion remains approval-gated and will only apply after review.",
            target_node_ids=[target_node.id] if target_node.id is not None else [],
            subtree_root_ids=[target_node.id] if target_node.id is not None else [],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=warnings,
            fallback_node=target_node,
        ),
        "kind": "delete_node",
        "target_node_id": target_node.id,
        "target_node_title": target_node.title,
        "confirmation_title": f"Delete {target_node.title}?",
        "confirmation_body": (
            f"This will remove {target_node.title} from {category_name}"
            + (
                f" and also delete {descendant_count} nested subtask{'s' if descendant_count != 1 else ''}."
                if descendant_count > 0
                else "."
            )
        ),
        "confirm_label": "Yes, delete it",
        "cancel_label": "No, keep it",
        "consequences": consequences,
    }


def build_bulk_delete_nodes_proposal(
    *,
    target_nodes: list[StrategyNode],
    category_name: str,
    descendant_counts: dict[int, int],
    consequences: list[str],
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any] | None:
    concrete_targets = [
        node for node in target_nodes if node.id is not None
    ]
    if not concrete_targets:
        return None

    target_node_ids = [int(node.id) for node in concrete_targets if node.id is not None]
    target_label = (
        concrete_targets[0].title
        if len(concrete_targets) == 1
        else f"{len(concrete_targets)} tasks"
    )
    warnings = _normalize_string_list(validation_warnings)
    total_descendants = sum(descendant_counts.get(int(node.id), 0) for node in concrete_targets if node.id is not None)
    if total_descendants > 0:
        warnings.append(
            f"This will also delete {total_descendants} nested subtask{'s' if total_descendants != 1 else ''}."
        )
    diff_preview: list[dict[str, Any]] = []
    for node in concrete_targets:
        node_id = int(node.id)
        diff_preview.append(
            {
                "kind": "delete_node",
                "label": f"Delete {node.title}",
                "detail": f"Remove from {category_name}",
                "target_node_id": node_id,
                "before": node.title,
            }
        )
        descendant_count = descendant_counts.get(node_id, 0)
        if descendant_count > 0:
            diff_preview.append(
                {
                    "kind": "delete_descendants",
                    "label": f"Delete nested subtasks under {node.title}",
                    "detail": (
                        f"{descendant_count} nested subtask"
                        f"{'s' if descendant_count != 1 else ''} will also be removed"
                    ),
                    "target_node_id": node_id,
                }
            )

    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="delete",
            summary=summary or f"Delete {target_label}",
            rationale="Deletion remains approval-gated and will only apply after review.",
            target_node_ids=target_node_ids,
            subtree_root_ids=target_node_ids,
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=warnings,
            fallback_node=concrete_targets[0],
        ),
        "kind": "delete_node",
        "target_node_id": int(concrete_targets[0].id),
        "target_node_title": target_label,
        "confirmation_title": f"Delete {target_label}?",
        "confirmation_body": (
            f"This will remove {', '.join(node.title for node in concrete_targets)} from {category_name}"
            + (
                f" and also delete {total_descendants} nested subtask{'s' if total_descendants != 1 else ''}."
                if total_descendants > 0
                else "."
            )
        ),
        "confirm_label": "Yes, delete",
        "cancel_label": "No, keep them",
        "consequences": consequences,
    }


def build_mixed_mutation_proposal(
    *,
    category_id: int,
    target_node: StrategyNode,
    target_label: str,
    metadata_targets: list[StrategyNode],
    metadata_updates: dict[str, Any],
    delete_targets: list[StrategyNode],
    category_name: str,
    rationale: str,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any] | None:
    concrete_metadata_targets = [
        node for node in metadata_targets if node.id is not None
    ]
    concrete_delete_targets = [
        node for node in delete_targets if node.id is not None
    ]
    if not concrete_metadata_targets and not concrete_delete_targets:
        return None

    target_node_ids: list[int] = []
    structural_operations: list[dict[str, Any]] = []
    diff_preview: list[dict[str, Any]] = []
    for node in concrete_metadata_targets:
        node_id = int(node.id)
        if node_id not in target_node_ids:
            target_node_ids.append(node_id)
        structural_operations.append(
            {
                "kind": "metadata_update",
                "node_id": node_id,
                "node_title": node.title,
                "updates": dict(metadata_updates),
            }
        )
        for item in build_metadata_diff_preview(target_node=node, updates=metadata_updates):
            label = str(item.get("label") or "Change metadata").strip()
            item["label"] = f"{label} on {node.title}"
            diff_preview.append(item)

    for node in concrete_delete_targets:
        node_id = int(node.id)
        if node_id not in target_node_ids:
            target_node_ids.append(node_id)
        structural_operations.append(
            {
                "kind": "delete_node",
                "node_id": node_id,
                "node_title": node.title,
            }
        )
        diff_preview.append(
            {
                "kind": "delete_node",
                "label": f"Delete {node.title}",
                "detail": f"Remove from {category_name}",
                "target_node_id": node_id,
                "before": node.title,
            }
        )

    warnings = _normalize_string_list(validation_warnings)
    if concrete_delete_targets and concrete_metadata_targets:
        warnings.append(
            "Approval applies metadata updates first, then deletes the selected redundant tasks."
        )

    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="mixed",
            summary=summary or target_label,
            rationale=rationale,
            target_node_ids=target_node_ids,
            subtree_root_ids=target_node_ids[:],
            evidence_bundle=None,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=warnings,
            fallback_node=target_node,
        ),
        "target_node_id": target_node.id,
        "target_category_id": category_id,
        "target_node_title": target_label,
        "proposed_children": [],
        "structural_operations": structural_operations,
    }


def build_move_subtree_proposal(
    *,
    moving_node: StrategyNode,
    destination_parent: StrategyNode | None,
    target_index: int,
    rationale: str,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    destination_title = destination_parent.title if destination_parent is not None else "top level"
    diff_preview = [
        {
            "kind": "move_subtree",
            "label": f"Move {moving_node.title}",
            "detail": f"Place under {destination_title} at position {target_index + 1}",
            "target_node_id": moving_node.id,
            "before": "current location",
            "after": destination_title,
        }
    ]
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="move",
            summary=summary or f"Move {moving_node.title} under {destination_title}",
            rationale=rationale,
            target_node_ids=[moving_node.id] if moving_node.id is not None else [],
            subtree_root_ids=[moving_node.id] if moving_node.id is not None else [],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=moving_node,
        ),
        "target_node_id": moving_node.id,
        "target_node_title": moving_node.title,
        "destination_parent_id": destination_parent.id if destination_parent is not None else None,
        "destination_parent_title": destination_title,
        "target_index": target_index,
    }


def build_reorder_children_proposal(
    *,
    parent_node: StrategyNode | None,
    ordered_child_ids: list[int],
    ordered_child_titles: list[str],
    rationale: str,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    parent_title = parent_node.title if parent_node is not None else "top-level tasks"
    diff_preview = [
        {
            "kind": "reorder_children",
            "label": f"Reorder children under {parent_title}",
            "detail": " -> ".join(title for title in ordered_child_titles if title),
            "target_node_id": parent_node.id if parent_node is not None else None,
        }
    ]
    target_node_ids = [child_id for child_id in ordered_child_ids if isinstance(child_id, int)]
    if parent_node is not None and parent_node.id is not None:
        subtree_root_ids = [parent_node.id]
    else:
        subtree_root_ids = target_node_ids[:]
    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="reorder",
            summary=summary or f"Reorder children under {parent_title}",
            rationale=rationale,
            target_node_ids=target_node_ids,
            subtree_root_ids=subtree_root_ids,
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=validation_warnings,
            fallback_node=parent_node,
        ),
        "target_node_id": parent_node.id if parent_node is not None else None,
        "target_node_title": parent_title,
        "ordered_child_ids": target_node_ids,
        "ordered_child_titles": [title for title in ordered_child_titles if title],
    }


def build_structural_reorganization_proposal(
    *,
    category_id: int,
    destination_title: str,
    move_nodes: list[StrategyNode],
    existing_destination_node: StrategyNode | None = None,
    create_destination: bool = True,
    child_additions_by_parent: dict[int, list[str]] | None = None,
    rationale: str,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any] | None:
    concrete_move_nodes = [
        node for node in move_nodes if node.id is not None and node.category_id == category_id
    ]
    additions_by_parent = {
        parent_id: [title.strip() for title in titles if isinstance(title, str) and title.strip()]
        for parent_id, titles in (child_additions_by_parent or {}).items()
        if isinstance(parent_id, int)
    }
    additions_by_parent = {
        parent_id: titles for parent_id, titles in additions_by_parent.items() if titles
    }
    if not concrete_move_nodes and not additions_by_parent and not create_destination:
        return None

    destination_title = destination_title.strip() or "Job"
    destination_temp_id = "new_parent"
    structural_operations: list[dict[str, Any]] = []
    diff_preview: list[dict[str, Any]] = []

    destination_node_id = (
        existing_destination_node.id
        if existing_destination_node is not None and existing_destination_node.id is not None
        else None
    )
    destination_ref: int | str | None = destination_node_id
    if create_destination and destination_node_id is None:
        destination_ref = destination_temp_id
        structural_operations.append(
            {
                "kind": "create_node",
                "temp_id": destination_temp_id,
                "title": destination_title,
                "category_id": category_id,
                "parent_id": None,
            }
        )
        diff_preview.append(
            {
                "kind": "create_node",
                "label": f"Create {destination_title}",
                "detail": "Add as a top-level task",
                "after": destination_title,
            }
        )

    for index, node in enumerate(concrete_move_nodes):
        structural_operations.append(
            {
                "kind": "move_subtree",
                "node_id": node.id,
                "node_title": node.title,
                "destination_parent_id": destination_node_id,
                "destination_parent_temp_id": (
                    destination_temp_id if destination_node_id is None else None
                ),
                "destination_parent_title": destination_title,
                "target_index": index,
            }
        )
        diff_preview.append(
            {
                "kind": "move_subtree",
                "label": f"Move {node.title}",
                "detail": f"Place under {destination_title}",
                "target_node_id": node.id,
                "before": "current location",
                "after": destination_title,
            }
        )

    node_by_id = {
        int(node.id): node
        for node in concrete_move_nodes
        if node.id is not None
    }
    for parent_id, child_titles in additions_by_parent.items():
        parent_node = node_by_id.get(parent_id)
        for child_title in child_titles:
            structural_operations.append(
                {
                    "kind": "create_node",
                    "title": child_title,
                    "category_id": category_id,
                    "parent_id": parent_id,
                }
            )
            diff_preview.append(
                {
                    "kind": "create_node",
                    "label": f"Create {child_title}",
                    "detail": (
                        f"Add under {parent_node.title}"
                        if parent_node is not None
                        else "Add under the selected parent"
                    ),
                    "target_node_id": parent_id,
                    "after": child_title,
                }
            )

    target_node_ids = [
        int(node.id)
        for node in concrete_move_nodes
        if node.id is not None
    ]
    for parent_id in additions_by_parent:
        if parent_id not in target_node_ids:
            target_node_ids.append(parent_id)
    if destination_node_id is not None and destination_node_id not in target_node_ids:
        target_node_ids.insert(0, destination_node_id)

    proposed_children = [
        str(item.get("title") or "").strip()
        for item in structural_operations
        if item.get("kind") == "create_node" and str(item.get("title") or "").strip()
    ]
    warnings = _normalize_string_list(validation_warnings)
    if any(item.get("kind") == "move_subtree" for item in structural_operations):
        warnings.append(
            "Approval creates any new parent first, then moves existing subtrees, then creates new child tasks."
        )

    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="restructure",
            summary=summary or f"Reorganize job-related tasks under {destination_title}",
            rationale=rationale,
            target_node_ids=target_node_ids,
            subtree_root_ids=target_node_ids[:],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=warnings,
            fallback_node=existing_destination_node or (concrete_move_nodes[0] if concrete_move_nodes else None),
        ),
        "target_node_id": destination_node_id,
        "target_category_id": category_id,
        "target_node_title": destination_title,
        "proposed_children": proposed_children,
        "structural_operations": structural_operations,
    }


def build_subtree_rewrite_proposal(
    *,
    target_node: StrategyNode,
    structural_operations: list[dict[str, Any]],
    rationale: str,
    evidence_bundle: dict[str, Any] | None = None,
    evidence_override: list[dict[str, Any]] | None = None,
    validation_warnings: list[str] | None = None,
    summary: str | None = None,
    proposal_id: str | None = None,
) -> dict[str, Any] | None:
    if target_node.id is None or target_node.category_id is None:
        return None
    normalized_operations = [
        operation for operation in structural_operations if isinstance(operation, dict)
    ]
    if not normalized_operations:
        return None

    diff_preview: list[dict[str, Any]] = []
    proposed_children: list[str] = []
    target_node_ids: list[int] = [int(target_node.id)]
    for operation in normalized_operations:
        kind = str(operation.get("kind") or "").strip()
        if kind == "create_node":
            title = str(operation.get("title") or "").strip()
            if title:
                proposed_children.append(title)
            detail_parent = (
                str(operation.get("parent_title") or "").strip()
                or target_node.title
            )
            diff_preview.append(
                {
                    "kind": "create_node",
                    "label": f"Create {title or 'new group'}",
                    "detail": f"Add under {detail_parent}",
                    "target_node_id": target_node.id,
                    "after": title or "new group",
                }
            )
        elif kind == "move_subtree":
            node_title = str(operation.get("node_title") or "").strip()
            destination_title = (
                str(operation.get("destination_parent_title") or "").strip()
                or target_node.title
            )
            node_id = operation.get("node_id")
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
            diff_preview.append(
                {
                    "kind": "move_subtree",
                    "label": f"Move {node_title or 'task'}",
                    "detail": f"Place under {destination_title}",
                    "target_node_id": node_id if isinstance(node_id, int) else target_node.id,
                    "before": target_node.title,
                    "after": destination_title,
                }
            )
        elif kind == "archive_node":
            node_id = operation.get("node_id")
            node_title = str(operation.get("node_title") or "").strip()
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
            diff_preview.append(
                {
                    "kind": "archive_node",
                    "label": f"Archive {node_title or 'task'}",
                    "detail": "Move this task and its descendants out of the active tree.",
                    "target_node_id": node_id if isinstance(node_id, int) else target_node.id,
                    "before": node_title or "active task",
                    "after": "archived",
                }
            )
        elif kind == "metadata_update":
            node_id = operation.get("node_id")
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
            diff_preview.append(
                {
                    "kind": "metadata_update",
                    "label": "Update metadata",
                    "detail": "Apply metadata update inside the restructured branch.",
                    "target_node_id": node_id if isinstance(node_id, int) else target_node.id,
                    "after": "metadata",
                }
            )
        elif kind == "delete_node":
            node_id = operation.get("node_id")
            node_title = str(operation.get("node_title") or "").strip()
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
            diff_preview.append(
                {
                    "kind": "delete_node",
                    "label": f"Remove {node_title or 'grouping task'}",
                    "detail": "Remove the now-empty grouping node after its children are moved.",
                    "target_node_id": node_id if isinstance(node_id, int) else target_node.id,
                    "before": node_title or "grouping task",
                    "after": "removed",
                }
            )

    warnings = _normalize_string_list(validation_warnings)
    cross_workspace_moves = [
        item
        for item in normalized_operations
        if item.get("kind") == "move_subtree"
        and isinstance(item.get("destination_category_id"), int)
        and item.get("destination_category_id") != target_node.category_id
    ]
    if cross_workspace_moves:
        warnings.append(
            "Moving a parent keeps its descendants together and updates linked calendar blocks to the destination workspace."
        )
    elif any(item.get("kind") == "move_subtree" for item in normalized_operations):
        if any(item.get("kind") == "create_node" for item in normalized_operations):
            warnings.append(
                "Approval keeps the selected parent in place, creates any new grouping tasks under it, then reparents existing subtasks into those groups."
            )
        elif any(item.get("kind") == "delete_node" for item in normalized_operations):
            warnings.append(
                "Approval keeps the selected parent in place, moves existing descendants, then removes grouping nodes that are no longer needed."
            )
        else:
            warnings.append(
                "Approval keeps the selected parent in place and reparents existing subtasks inside that branch."
            )

    return {
        **_proposal_fields(
            proposal_id=proposal_id,
            proposal_kind="restructure",
            summary=summary or f"Restructure subtasks under {target_node.title}",
            rationale=rationale,
            target_node_ids=target_node_ids,
            subtree_root_ids=[int(target_node.id)],
            evidence_bundle=evidence_bundle,
            evidence_override=evidence_override,
            diff_preview=diff_preview,
            validation_warnings=warnings,
            fallback_node=target_node,
        ),
        "target_node_id": int(target_node.id),
        "target_category_id": int(target_node.category_id),
        "target_node_title": target_node.title,
        "proposed_children": proposed_children,
        "structural_operations": normalized_operations,
    }
