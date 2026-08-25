"""User-facing Strategist response helpers.

These helpers are shared by the graph and kernel. Keeping them
outside the legacy Strategist orchestrator lets presentation and policy
normalization evolve without importing the old control plane.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Optional

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.deadline_values import parse_deadline_date
from app.services.retrieval import (
    format_deadline as retrieval_format_deadline,
    format_estimate as retrieval_format_estimate,
    priority_label,
)

DELETE_RAW_ID_RE = re.compile(
    r"\b(?:id|node[_ ]?id|parent[_ ]?id)\b\s*[:=]?\s*`?\d+`?",
    re.IGNORECASE,
)
DELETE_BACKTICK_NUMBER_RE = re.compile(r"`\d+`")
DELETE_FIELD_NAME_REPLACEMENTS = {
    "deadline_at": "deadline",
    "time_estimate_minutes": "estimate",
    "node_type": "type",
    "parent_id": "parent task",
    "staged_task_ids": "staged tasks",
    "is_pinned": "pinned",
}


def decode_string_list(raw_value: Optional[str]) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if isinstance(item, (str, int, float))]
    return []


def format_estimate(minutes: Optional[int]) -> str:
    if minutes is None:
        return "No estimate"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remainder = minutes % 60
    if remainder:
        return f"{hours}h {remainder}m"
    return f"{hours}h"


def build_focus_detail(focus_node: StrategyNode | None) -> str:
    if focus_node is None:
        return "Select a task in the tree or open a task page to target strategist actions."
    return " | ".join(
        [
            f"Priority {focus_node.priority}",
            focus_node.deadline_at or "No deadline",
            format_estimate(focus_node.time_estimate_minutes),
        ]
    )


def build_context_cards(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_count: int,
    today_override: date | None = None,
) -> list[dict[str, Any]]:
    today_value = today_override or date.today()
    backlog_count = sum(1 for node in nodes if node.is_backlog)
    due_soon_cutoff = today_value + timedelta(days=7)
    due_soon_count = 0
    for node in nodes:
        deadline_date = parse_deadline_date(node.deadline_at)
        if (
            deadline_date is not None
            and deadline_date <= due_soon_cutoff
            and node.status != "done"
        ):
            due_soon_count += 1
    active_count = len(nodes) - backlog_count
    category_names = ", ".join(category.name for category in categories[:3])
    if len(categories) > 3:
        category_names = f"{category_names}, +{len(categories) - 3} more"
    return [
        {
            "id": "all_trees",
            "label": "All trees",
            "value": f"{len(categories)} categories",
            "detail": f"{len(nodes)} tasks across {category_names or 'your workspaces'}",
        },
        {
            "id": "current_load",
            "label": "Current load",
            "value": f"{active_count} active | {backlog_count} backlog",
            "detail": f"{due_soon_count} due within 7 days",
        },
        {
            "id": "focus_task",
            "label": "Current task",
            "value": focus_node.title if focus_node is not None else "No task selected",
            "detail": build_focus_detail(focus_node),
        },
        {
            "id": "calendar_context",
            "label": "Calendar context",
            "value": f"{staged_node_count} staged for execution",
            "detail": (
                f"Planner handoff is ready from {active_category.name}."
                if active_category is not None
                else "Shared calendar surface is connected for planner handoff."
            ),
        },
    ]


def build_suggested_prompts(focus_node: StrategyNode | None) -> list[str]:
    if focus_node is None:
        return []
    return [
        f"Break down {focus_node.title} into concrete subtasks",
        f"Tighten metadata for {focus_node.title}",
        f"What should I do first in {focus_node.title}?",
    ]


def sanitize_user_facing_text(value: str) -> str:
    sanitized = DELETE_RAW_ID_RE.sub("", value)
    sanitized = DELETE_BACKTICK_NUMBER_RE.sub("", sanitized)
    for raw_field, user_field in DELETE_FIELD_NAME_REPLACEMENTS.items():
        sanitized = sanitized.replace(raw_field, user_field)
    sanitized = re.sub(r"\s{2,}", " ", sanitized)
    sanitized = re.sub(r"\s+([,.;:])", r"\1", sanitized)
    return sanitized.strip()


def normalize_summary(raw_value: Any, fallback: str) -> str:
    if isinstance(raw_value, str):
        trimmed = raw_value.strip()
        if trimmed:
            return sanitize_user_facing_text(trimmed)
    return fallback


def derive_response_policy(
    *,
    summary: str,
    action_proposal: dict[str, Any] | None,
    metadata_proposal: dict[str, Any] | None,
    branch_proposal: dict[str, Any] | None,
    clarification: dict[str, Any] | None = None,
) -> str:
    if clarification:
        return "clarify"
    if action_proposal or metadata_proposal or branch_proposal:
        return "proposal"

    lowered = summary.strip().lower()
    if not lowered:
        return "empty"
    if "could not find" in lowered or "no such task" in lowered or "not find a task" in lowered:
        return "not_found"
    if (
        "tell me which" in lowered
        or "which one" in lowered
        or "mention the desired" in lowered
        or "try the exact task title" in lowered
        or lowered.endswith("?")
    ):
        return "clarify"
    return "answer_direct"


def build_pending_proposal_response(
    *,
    provider_label: str,
    pending_proposal: dict[str, Any],
    action: str,
    context_cards: list[dict[str, Any]],
    suggested_prompts: list[str],
) -> dict[str, Any]:
    proposal_kind = str(pending_proposal.get("kind") or "review")
    proposal = pending_proposal.get("proposal") if isinstance(pending_proposal.get("proposal"), dict) else {}
    response = pending_proposal.get("response") if isinstance(pending_proposal.get("response"), dict) else {}
    action_proposal = (
        response.get("action_proposal")
        if isinstance(response.get("action_proposal"), dict)
        else proposal if proposal_kind == "action" else None
    )
    metadata_proposal = (
        response.get("metadata_proposal")
        if isinstance(response.get("metadata_proposal"), dict)
        else proposal if proposal_kind == "metadata" else None
    )
    branch_proposal = (
        response.get("branch_proposal")
        if isinstance(response.get("branch_proposal"), dict)
        else proposal if proposal_kind == "branch" else None
    )
    target_title = str(
        proposal.get("target_node_title")
        or proposal.get("targetNodeTitle")
        or "that proposal"
    ).strip() or "that proposal"
    mode = "branch" if proposal_kind == "branch" else "metadata" if proposal_kind == "metadata" else "action"

    if action == "reject":
        summary = f"Okay. I will treat the proposal for {target_title} as rejected and leave your current tasks unchanged."
        return {
            "provider_label": provider_label,
            "mode": mode,
            "summary": summary,
            "context_cards": context_cards,
            "suggested_prompts": suggested_prompts,
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
        }

    if action == "edit":
        if proposal_kind == "branch":
            summary = f"I still have the proposed breakdown for {target_title} in context. Tell me what to change, or use the edit action below to seed a revision prompt."
            prompts = [
                f"Revise the breakdown for {target_title} to make it smaller and more execution-focused",
                f"Revise the breakdown for {target_title} and merge overlapping subtasks",
                f"Revise the breakdown for {target_title} to focus on only the next two steps",
            ]
        elif proposal_kind == "metadata":
            summary = f"I still have the metadata patch for {target_title} in context. Tell me which field you want to change."
            prompts = [
                f"Change the priority update for {target_title}",
                f"Change the deadline update for {target_title}",
                f"Change the status update for {target_title}",
            ]
        else:
            summary = f"I still have the pending action for {target_title} in context. Tell me what you want to change."
            prompts = suggested_prompts
        return {
            "provider_label": provider_label,
            "mode": mode,
            "summary": summary,
            "context_cards": context_cards,
            "suggested_prompts": prompts,
            "action_proposal": action_proposal,
            "metadata_proposal": metadata_proposal,
            "branch_proposal": branch_proposal,
            "clarification": None,
        }

    summary = (
        f"I still have the proposal for {target_title} in context. "
        "Use the review controls below to approve, revise, or reject it."
    )
    return {
        "provider_label": provider_label,
        "mode": mode,
        "summary": summary,
        "context_cards": context_cards,
        "suggested_prompts": suggested_prompts,
        "action_proposal": action_proposal,
        "metadata_proposal": metadata_proposal,
        "branch_proposal": branch_proposal,
        "clarification": None,
    }


def is_archived_reference(node: StrategyNode) -> bool:
    return any(tag.lower() == "state:archived" for tag in decode_string_list(node.tags))


def build_task_fact_summary(prompt: str, node: StrategyNode) -> str:
    lowered = prompt.strip().lower()
    if "block" in lowered:
        return f"{node.title} is not currently blocked by a first-class blocker field. Check related tasks for dependency context."
    if "due" in lowered or "deadline" in lowered or "when" in lowered:
        if node.deadline_at:
            return f"{node.title} is due on {retrieval_format_deadline(node.deadline_at)}."
        return f"{node.title} does not have a deadline yet."
    if "priority" in lowered or any(color in lowered for color in ("red", "orange", "green")):
        return f"{node.title} is {priority_label(node.priority)}."
    if "estimate" in lowered or "how long" in lowered:
        return f"{node.title} is estimated at {retrieval_format_estimate(node.time_estimate_minutes)}."
    if "finish" in lowered or "status" in lowered:
        if is_archived_reference(node):
            return f"{node.title} is archived historical work, not an active task."
        return f"{node.title} is currently {node.status}."
    return (
        f"{node.title} is {priority_label(node.priority)}, "
        f"due {retrieval_format_deadline(node.deadline_at)}, "
        f"and estimated at {retrieval_format_estimate(node.time_estimate_minutes)}."
    )


__all__ = [
    "build_context_cards",
    "build_pending_proposal_response",
    "build_suggested_prompts",
    "build_task_fact_summary",
    "derive_response_policy",
    "normalize_summary",
]
