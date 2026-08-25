"""
Shared operating contract for the Strategist agent kernel.

This module intentionally contains no regex intent routing. It describes the
agent harness: what the model may do, what context it can use, and which actions
must pause behind approval-gated proposal objects.
"""

from __future__ import annotations

from typing import Any

from app.services.strategist_prompts import (
    STRATEGIST_AGENT_CAPABILITIES,
    STRATEGIST_AGENT_CONTEXT_SURFACES,
    STRATEGIST_AGENT_OPERATING_CONTRACT,
    STRATEGIST_AGENT_PROPOSAL_CONTRACT,
    STRATEGIST_AGENTIC_LOOP_CONTRACT,
    STRATEGIST_DECISION_SYNTHESIS_CONTRACT,
    STRATEGIST_TREE_OPERATIONS_CONTRACT,
)

AGENTIC_PLAN_CONFIDENCE_FLOOR = 0.45

MUTATION_ACTION_KINDS = {
    "metadata_update",
    "delete_candidates",
    "delete_specific",
    "create_task",
    "move_tasks",
    "restructure",
}


def semantic_plan_action_kinds(plan: dict[str, Any] | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    actions = plan.get("actions")
    if not isinstance(actions, list):
        return []
    return [
        str(action.get("kind") or "").strip()
        for action in actions
        if isinstance(action, dict) and str(action.get("kind") or "").strip()
    ]


def semantic_plan_is_actionable(plan: dict[str, Any] | None) -> bool:
    """Return true when the semantic planner should own the turn."""

    action_kinds = semantic_plan_action_kinds(plan)
    if not action_kinds or not isinstance(plan, dict):
        return False
    if bool(plan.get("requires_proposal")):
        return True
    try:
        confidence = float(plan.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence >= AGENTIC_PLAN_CONFIDENCE_FLOOR


def should_use_semantic_plan_fallback_preflights(plan: dict[str, Any] | None) -> bool:
    """Preflight heuristics are a fallback only when no usable semantic plan exists."""

    return not semantic_plan_is_actionable(plan)


def build_agent_contract_payload(
    *,
    semantic_plan: dict[str, Any] | None,
    semantic_plan_requires_proposal: bool,
) -> dict[str, Any]:
    action_kinds = semantic_plan_action_kinds(semantic_plan)
    return {
        "control_policy": "semantic_plan_primary",
        "fallback_preflights": (
            "fallback_only_when_no_usable_semantic_plan"
            if semantic_plan_is_actionable(semantic_plan)
            else "allowed_because_no_usable_semantic_plan"
        ),
        "semantic_action_kinds": action_kinds,
        "semantic_plan_requires_proposal": semantic_plan_requires_proposal,
        "proposal_required_behavior": (
            "If semantic_plan_requires_proposal is true, gather enough task evidence "
            "with tools and return a structured proposal object or clarification. "
            "Do not answer with read-only prose only."
        ),
        "combined_mutation_behavior": (
            "If any semantic action is structural, the response must be one branch/mixed "
            "proposal containing the structural operations. Do not satisfy a combined "
            "request with metadata_proposal alone."
        ),
        "decision_synthesis_behavior": STRATEGIST_DECISION_SYNTHESIS_CONTRACT,
        "atomic_tree_operations": {
            "proposal_field": "branch_proposal.structural_operations",
            "supported_operations": [
                "create_node",
                "move_subtree",
                "delete_node",
                "metadata_update",
            ],
            "contract": STRATEGIST_TREE_OPERATIONS_CONTRACT,
        },
        "context_surfaces": [
            "current_ui_context",
            "visible_conversation_transcript",
            "session_working_set_when_referential",
            "read_only_task_tools",
        ],
        "approval_boundary": (
            "create/delete/move/reparent/reorder/metadata/restructure changes must pause "
            "as proposal objects with approve/edit/reject controls"
        ),
    }
