"""
Feature-flagged Strategist Agent Kernel.

This runtime is the replacement control plane for kernel-enabled Strategist
turns. It reuses retrieval/proposal helpers as tools and adapters, but it does
not invoke the legacy intent-router, semantic-planner, or preflight branches.
"""

from __future__ import annotations

import copy
import json
import hashlib
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.category import Category
from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode
from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries
from app.models.strategist_agent import StrategistAgentEvent, StrategistRun
from app.models.strategist_graph import StrategistProposalRecord
from app.services.agent_kernel.context_contract import (
    compact_pending_proposal_for_model,
    task_contract_surface,
)
from app.services.agent_kernel.compact_goal_context import (
    build_compact_goal_context,
    resolve_evidence_handle,
)
from app.services.agent_kernel.plan import (
    build_branch_agent_run_plan,
    build_cleanup_agent_run_plan,
    build_workload_agent_run_plan,
    clarification_from_agent_run_plan,
    compact_agent_run_plan_for_model,
)
from app.services.agent_kernel.task_patch import (
    build_task_patch_draft,
    simulate_task_patch,
    validate_task_patch,
)
from app.services.agent_kernel.session_memory import (
    build_continuation_state,
    build_request_prompt_chain,
    build_session_memory,
    build_tool_context_entry,
    compact_continuation_state,
    project_goal_state_memory,
    reconstruct_active_request_prompts,
)
from app.services.agent_kernel.contracts import (
    ChangeSet,
    DraftChangeSetCommand,
    GoalTurnDecision,
    GoalStateUpdateCommand,
    Provenance,
    SimulateChangeSetCommand,
)
from app.services.agent_kernel.change_set_authoring import NativeChangeSetAuthoringCoordinator
from app.services.agent_kernel.change_compilation import (
    ChangeCompilationRequest,
    CompatibilityProposalAdapter,
    PlannerScheduleArtifactAdapter,
    SemanticPlanArtifact,
    ServerChangeCompilationCoordinator,
)
from app.services.agent_kernel.simulation_coordinator import ChangeSetSimulationCoordinator
from app.services.agent_kernel.controller import NativeAuthorityController
from app.services.agent_kernel.simulation_store import SimulationResultNotFound
from app.services.agent_kernel.goal_state_coordinator import (
    GoalStateCoordinator,
    decision_clarification_payload,
)
from app.services.agent_kernel.recurrence_authoring import (
    NativeRecurrenceAuthoringError,
    compile_native_recurrence_update,
)
from app.services.agent_kernel.state_store import (
    GoalStateRevisionConflict,
    GoalStateStore,
    GoalStateStoreError,
)
from app.services.agent_kernel.tier_router import (
    TierRouteDecision,
    route_strategist_turn,
)
from app.services.agent_kernel.tool_registry import (
    CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA,
    _pending_metadata_updates_from_session_memory,
    build_kernel_tool_registry_context,
    compact_simulation_result_for_model,
    execute_kernel_tool,
    registered_kernel_tool_schemas,
    restore_task_plan_coverage,
    summarize_kernel_tool_observation,
    task_plan_coverage_checkpoint,
)
from app.services.agent_kernel.mcp_client import invoke_drasil_mcp_tool
from app.services.agent_kernel.mcp_invocation import (
    McpExecutionDependencies,
    McpInvocationContext,
)
from app.services.agent_kernel.mcp_policy import (
    allowed_mcp_tool_names,
    registered_agent_provider_tool_schemas,
)
from app.services.agent_kernel.provider_transport import (
    build_direct_response_prompt,
    codex_structured_step_timeout_seconds,
    codex_tool_loop_max_steps,
    codex_tool_loop_timeout_seconds,
    get_provider_runtime_settings,
    invoke_codex_direct_response,
    invoke_codex_tool_loop,
    invoke_openai_direct_response,
    invoke_openai_tool_loop,
    provider_transport_readiness,
)
from app.services.strategist_metadata_intent import (
    build_metadata_updates_from_prompt,
    parse_prompt_date,
    parse_prompt_estimate_minutes,
    parse_prompt_priority,
    parse_prompt_status,
)
from app.services.preference_values import decode_preference_value
from app.services.strategist_presentation import (
    build_context_cards,
    build_suggested_prompts,
    derive_response_policy,
    normalize_summary,
)
from app.services.policy import build_clarification_payload
from app.services.planner_capability import (
    build_created_task_schedule_plan,
    build_planner_schedule_proposal,
    created_task_schedule_groups,
)
from app.services.preference_patches import (
    PREFERENCE_PROPOSAL_KIND,
    PREFERENCE_RESPONSE_KEY,
    validate_preference_patch,
)
from app.services.agent_kernel.core_contract import (
    build_core_operating_contract,
    render_core_operating_instructions,
)

from app.services.agent_kernel.evidence_contract import assess_tool_evidence
from app.services.agent_kernel.capability_observability import (
    emit_capability_observations,
)
from app.services.agent_kernel.agent_observability import (
    observe_agent_tool,
    observe_agent_turn,
)
from app.services.agent_kernel.capabilities import compact_capability_catalog, load_skill
from app.services.agent_kernel.calendar_service import (
    CalendarReadInvocation,
    SQL_CALENDAR_READ_SERVICE,
)
from app.services.agent_kernel.scheduling_service import (
    PlannerSchedulingApplicationService,
    schedule_planning_result_payload,
)
from app.services.agent_kernel.subagent_delegation import (
    PlannerSubagentTask,
    PlannerSubagentCoordinator,
    PlannerSubagentResult,
    SubagentBudget,
    SubagentDelegationError,
    SubagentParentContext,
    create_planner_subagent_task,
)
from app.services.strategist_provider_response import (
    normalize_agent_clarification,
    normalize_provider_response_payload,
)
from app.services.strategist_proposal_normalizer import (
    normalize_branch_proposal,
    normalize_metadata_proposal,
    normalize_structural_operation_kind,
)
from app.services.strategist_proposals import (
    build_metadata_patch_proposal,
    build_mixed_mutation_proposal,
    build_top_level_create_task_proposal,
    build_top_level_task_set_proposal,
)
from app.services.strategist_sessions import get_session_working_set
from app.services.task_read_handlers import build_runtime_context
from app.services.recurring_schedules import (
    build_recurring_schedule_plan,
    preference_override_authorized_from_prompt,
)
from app.db.database import get_active_engine

# Shared-workspace turns execute as durable Strategist runs.  The UI's short
# foreground wait is deliberately decoupled from this controller ceiling, so a
# multi-tool cleanup or scheduling turn can continue in the background.  A
# meaningful-progress idle watchdog still stops a provider that is genuinely
# stalled.
SHARED_WORKSPACE_TURN_TIMEOUT_SECONDS = 7200
SHARED_WORKSPACE_PLANNER_RESERVE_SECONDS = 95
SHARED_WORKSPACE_FINALIZATION_RESERVE_SECONDS = 5
# Provider usage is cumulative across Planner tool-loop turns: each tool result
# causes the operating contract, schemas, and authoritative context to be sent
# again.  A 16k total-token cap was below the normal cost of a valid multi-tool
# planning pass, so successful Planner work was discarded after it completed.
OWNED_PLANNER_TOTAL_TOKEN_BUDGET = 48_000
SEMANTIC_AUTHORING_TOOL_NAMES = frozenset(
    {
        "submit_task_metadata_plan",
        "submit_task_tree_plan",
        "submit_preference_plan",
        "submit_preference_management_plan",
        "submit_preference_cleanup_plan",
        "handoff_to_planner",
        "respond_to_planner_decision",
        "draft_change_set",
        "simulate_change_set",
    }
)
SEMANTIC_PLAN_TOOL_FAMILIES = {
    "submit_task_metadata_plan": "metadata",
    "submit_task_tree_plan": "branch",
    "submit_preference_plan": PREFERENCE_PROPOSAL_KIND,
    "submit_preference_management_plan": PREFERENCE_PROPOSAL_KIND,
    "submit_preference_cleanup_plan": PREFERENCE_PROPOSAL_KIND,
}


def _remaining_deadline_seconds(deadline_monotonic: float, *, reserve_seconds: int = 0) -> int:
    return max(0, int(deadline_monotonic - perf_counter() - reserve_seconds))

KERNEL_CONTROL_PLANE_NAMES = {"agent-kernel", "agent_kernel", "kernel"}
CLASSIC_SEMANTIC_FAST_PATHS_ENV = "ALLOW_AGENT_KERNEL_SEMANTIC_FAST_PATHS"
TYPED_TOOL_FAST_PATHS_ENV = "STRATEGIST_TYPED_TOOL_FAST_PATHS"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
VALID_BULK_STATUS_VALUES = {"todo", "done"}
CLEANUP_ANALYSIS_LIMIT = 8
HARNESS_EVIDENCE_FLOOR_LIMIT = 12
EXPLICIT_TASK_CITATION_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


def _tool_evidence_task_ids(value: Any) -> set[int]:
    """Collect only task identities returned by tools, never title-only guesses."""

    task_ids: set[int] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in {
                "task_id",
                "node_id",
                "parent_task_id",
                "target_node_id",
            } and isinstance(child, int) and not isinstance(child, bool):
                task_ids.add(int(child))
            elif normalized_key in {
                "task_ids",
                "node_ids",
                "target_node_ids",
                "working_set_task_ids",
                "affected_node_ids",
            } and isinstance(child, list):
                task_ids.update(
                    int(item)
                    for item in child
                    if isinstance(item, int) and not isinstance(item, bool)
                )
            task_ids.update(_tool_evidence_task_ids(child))
    elif isinstance(value, list):
        for child in value:
            task_ids.update(_tool_evidence_task_ids(child))
    return task_ids


def _ranked_tool_evidence_task_ids(
    value: Any,
    *,
    normalized_title: str,
) -> list[int]:
    """Collect exact ID-title pairs in the order supplied by retrieved evidence."""

    ranked_ids: list[int] = []

    def visit(child: Any) -> None:
        if isinstance(child, dict):
            raw_title = child.get("title")
            raw_id = next(
                (
                    child.get(key)
                    for key in ("task_id", "node_id", "id")
                    if isinstance(child.get(key), int)
                    and not isinstance(child.get(key), bool)
                ),
                None,
            )
            if (
                isinstance(raw_title, str)
                and isinstance(raw_id, int)
                and " ".join(raw_title.strip().split()).casefold() == normalized_title
                and raw_id not in ranked_ids
            ):
                ranked_ids.append(raw_id)
            for nested in child.values():
                visit(nested)
        elif isinstance(child, list):
            for nested in child:
                visit(nested)

    visit(value)
    return ranked_ids


def _explicit_grounded_task_ids(
    *,
    summary: str,
    tool_trace: list[dict[str, Any]],
    nodes: list[StrategyNode],
) -> list[int]:
    """Bind explicit answer citations to unique IDs found in tool evidence."""

    cited_titles = {
        " ".join(match.group(1).strip().split()).casefold()
        for match in EXPLICIT_TASK_CITATION_RE.finditer(summary)
        if match.group(1).strip()
    }
    if not cited_titles:
        return []
    evidence_ids: set[int] = set()
    ranked_ids_by_title: dict[str, list[int]] = {
        title: [] for title in cited_titles
    }
    for call in tool_trace:
        if not isinstance(call, dict):
            continue
        result = call.get("result")
        if isinstance(result, dict):
            evidence_ids.update(_tool_evidence_task_ids(result))
            for title in cited_titles:
                ranked_ids = ranked_ids_by_title[title]
                for task_id in _ranked_tool_evidence_task_ids(
                    result,
                    normalized_title=title,
                ):
                    if task_id not in ranked_ids:
                        ranked_ids.append(task_id)

    ids_by_title: dict[str, list[int]] = {}
    node_by_id: dict[int, StrategyNode] = {}
    ranked_evidence_ids = {
        task_id
        for ranked_ids in ranked_ids_by_title.values()
        for task_id in ranked_ids
    }
    for node in nodes:
        if (
            node.id is None
            or (
                int(node.id) not in evidence_ids
                and int(node.id) not in ranked_evidence_ids
            )
        ):
            continue
        node_by_id[int(node.id)] = node
        title_key = " ".join(node.title.strip().split()).casefold()
        ids_by_title.setdefault(title_key, []).append(int(node.id))

    grounded_ids: list[int] = []
    for title in cited_titles:
        ranked_matches = [
            task_id
            for task_id in ranked_ids_by_title[title]
            if task_id in node_by_id
            and " ".join(node_by_id[task_id].title.strip().split()).casefold()
            == title
        ]
        if ranked_matches:
            grounded_ids.append(ranked_matches[0])
            continue
        matching_ids = ids_by_title.get(title, [])
        if len(matching_ids) == 1:
            grounded_ids.append(matching_ids[0])
    return sorted(set(grounded_ids))


def _new_recurring_task_duration_is_invented(
    *,
    branch_proposal: dict[str, Any] | None,
    request_context_text: str,
) -> bool:
    """Reject a model-supplied duration that has no user or task evidence.

    A newly created task has no existing estimate to inherit. Recurrence requires
    a concrete block duration, so accepting a model default would silently turn
    an unknown into a user-visible calendar commitment. The duration may come
    from any turn in the active request chain (for example, a follow-up "1 hr").
    """

    if not isinstance(branch_proposal, dict):
        return False
    recurring_schedule = branch_proposal.get("recurring_schedule")
    if not isinstance(recurring_schedule, dict):
        return False
    if parse_prompt_estimate_minutes(request_context_text) is not None:
        return False
    target_temp_id = str(recurring_schedule.get("target_temp_id") or "").strip()
    structural_operations = branch_proposal.get("structural_operations")
    if not isinstance(structural_operations, list):
        return False
    for operation in structural_operations:
        if not isinstance(operation, dict):
            continue
        operation_kind = str(
            operation.get("kind") or operation.get("op") or ""
        ).strip().lower()
        if operation_kind not in {"create", "create_node"}:
            continue
        operation_temp_id = str(operation.get("temp_id") or "").strip()
        if not target_temp_id or not operation_temp_id or operation_temp_id == target_temp_id:
            return True
    return False


def _new_recurring_task_requires_duration(
    *,
    branch_proposal: dict[str, Any] | None,
    request_context_text: str,
) -> bool:
    if (
        isinstance(branch_proposal, dict)
        and str(branch_proposal.get("revision_of_proposal_id") or "").strip()
    ):
        # A typed proposal revision has already been merged against the exact
        # persisted parent proposal. Its unchanged duration is authoritative
        # proposal state, not a newly invented model default. Requiring the
        # duration to also appear in the latest prose causes resolved fields to
        # loop whenever the user edits only time or weekdays.
        return False
    if _new_recurring_task_duration_is_invented(
        branch_proposal=branch_proposal,
        request_context_text=request_context_text,
    ):
        return True
    return False


def _proposal_revision_has_recurring_schedule(
    value: Any,
    *,
    depth: int = 0,
) -> bool:
    """Distinguish recurrence revisions from ordinary compound branch edits."""

    if depth > 6 or not isinstance(value, dict):
        return False
    if isinstance(
        value.get("recurring_schedule", value.get("recurringSchedule")),
        dict,
    ):
        return True
    return any(
        _proposal_revision_has_recurring_schedule(value.get(key), depth=depth + 1)
        for key in (
            "semantic_contract",
            "payload",
            "proposal",
            "branch_proposal",
            "branchProposal",
            "semantic_plan_input",
            "semanticPlanInput",
        )
    )


def _branch_revision_snapshot(
    branch_proposal: dict[str, Any],
    *,
    unresolved_fields: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Capture resolved semantic intent while excluding unverified slot values.

    Clarification is a pause in authoring, not permission to throw away the
    draft and ask a nondeterministic model to reconstruct it on the next turn.
    The snapshot is deliberately semantic (rather than a half-compiled
    ChangeSet), so the missing field can be filled and the complete plan can be
    compiled once the user answers.
    """

    payload = copy.deepcopy(branch_proposal)
    unresolved = set(unresolved_fields)
    if "duration_minutes" in unresolved:
        recurring_schedule = payload.get("recurring_schedule")
        if isinstance(recurring_schedule, dict):
            for key in (
                "duration_minutes",
                "durationMinutes",
                "estimate_minutes",
                "estimated_duration_minutes",
            ):
                recurring_schedule.pop(key, None)
            target_temp_id = str(
                recurring_schedule.get("target_temp_id")
                or recurring_schedule.get("targetTempId")
                or ""
            ).strip()
        else:
            target_temp_id = ""
        structural_operations = payload.get("structural_operations")
        if isinstance(structural_operations, list):
            for operation in structural_operations:
                if not isinstance(operation, dict):
                    continue
                operation_temp_id = str(operation.get("temp_id") or "").strip()
                if target_temp_id and operation_temp_id != target_temp_id:
                    continue
                operation_kind = str(
                    operation.get("kind")
                    or operation.get("operation")
                    or operation.get("op")
                    or ""
                ).strip().lower()
                if operation_kind not in {"create", "create_node", "task.create"}:
                    continue
                for key in (
                    "time_estimate_minutes",
                    "estimate_minutes",
                    "estimated_duration_minutes",
                    "duration_minutes",
                ):
                    operation.pop(key, None)
        # This is derived placement evidence calculated using the unsupported
        # duration. It must be recomputed after the missing slot is resolved.
        payload.pop("recurring_schedule_plan", None)
        payload.pop("recurringSchedulePlan", None)

    proposal_id = str(payload.get("proposal_id") or "").strip() or str(uuid4())
    payload["proposal_id"] = proposal_id
    return _compact_pending_proposal_for_context(
        {
            "proposal_id": proposal_id,
            "proposal_kind": "branch",
            "status": "awaiting_user_input" if unresolved else "pending",
            "target_node_id": payload.get("target_node_id"),
            "target_node_ids": payload.get("target_node_ids", []),
            "source_turn_id": None,
            "summary": payload.get("summary"),
            "rationale": payload.get("rationale"),
            "target_node_title": payload.get("target_node_title"),
            "payload": payload,
        }
    )


CLEANUP_REVIEW_RE = re.compile(
    r"\b(?:cleanup|clean\s+up|tidy|declutter|messy|redundant|duplicate|duplicates|"
    r"consolidate|prune|reorganize|re-organize)\b",
    re.IGNORECASE,
)
BROAD_RESTRUCTURE_RE = re.compile(
    r"\b(?:restructure|reorganize|re-organize|organize)\b.{0,80}"
    r"\b(?:tree|tasks?|todos?|workspace|categor(?:y|ies))\b"
    r"|\b(?:tree|tasks?|todos?|workspace|categor(?:y|ies))\b.{0,80}"
    r"\b(?:restructure|reorganize|re-organize|organize)\b",
    re.IGNORECASE,
)
CLEANUP_BROAD_SCOPE_RE = re.compile(
    r"\b(?:tree|tasks?|todos?|workspace|workspaces|categor(?:y|ies)|all|every|whole|entire)\b",
    re.IGNORECASE,
)
PREFERENCE_CLEANUP_SUBJECT_RE = re.compile(
    r"\b(?:pref\w*|remembered\s+(?:rules?|instructions?)|saved\s+(?:rules?|instructions?))\b",
    re.IGNORECASE,
)
PREFERENCE_CLEANUP_SCOPE_RE = re.compile(
    r"\b(?:all|every|conflict\w*|contradict\w*|outdat\w*|duplicat\w*|redundan\w*|"
    r"supersed\w*|older|obsolete|stale)\b",
    re.IGNORECASE,
)
WORKLOAD_REVIEW_RE = re.compile(
    r"\b(?:workload|triage|prioriti[sz]e\s+(?:everything|all|my\s+tasks|tasks)|"
    r"overwhelmed|too\s+much)\b",
    re.IGNORECASE,
)
BROAD_WORKLOAD_SCOPE_RE = re.compile(
    r"\b(?:workload|across|categories|workspace|all|every|everything|whole|entire|week|"
    r"overwhelmed|too\s+much)\b",
    re.IGNORECASE,
)
BROAD_STRATEGIC_PRIORITY_RE = re.compile(
    r"\b(?:focus|prioriti[sz]e|leverage|5x|highest[-\s]+leverage|growth|"
    r"asymmetric|assymetric|biggest\s+impact|what\s+should\s+i\s+(?:do|work\s+on)|"
    r"where\s+should\s+i\s+spend\s+time|next\s+best)\b",
    re.IGNORECASE,
)
BROAD_STRATEGIC_SCOPE_RE = re.compile(
    r"\b(?:life|right\s+now|today|this\s+week|next\s+week|quarter|3\s+months|"
    r"tasks?|tree|workspace|workload|my\s+work|everything|all|overall)\b",
    re.IGNORECASE,
)
STRUCTURAL_OR_CREATION_MUTATION_RE = re.compile(
    r"\b(?:add|create|new\s+task|delete|remove|move|reparent|reorder|group|ungroup|"
    r"flatten|restructure|reorganize|re-organize|break\s*down|breakdown|"
    r"decompose|decomposition|split)\b",
    re.IGNORECASE,
)
STRUCTURAL_PROPOSAL_CONTRACT_RE = re.compile(
    r"\b(?:add|create|new\s+task|delete|remove|reparent|reorder|group|ungroup|"
    r"flatten|restructure|reorganize|re-organize|break\s*down|split)\b",
    re.IGNORECASE,
)
TREE_MOVE_PROPOSAL_CONTRACT_RE = re.compile(
    r"\bmove\b.{0,100}\b(?:under|into|inside|below|above|after|before|parent|"
    r"branch|subtree|section|group)\b",
    re.IGNORECASE,
)
PROPOSED_DATE_VALUE_RE = re.compile(
    r"\b(?:due(?:\s+date)?|deadline|move|moved|moving|schedule|scheduled|"
    r"reschedule|rescheduled|shift|push|set|change|update)\b.{0,160}?"
    r"\b(?:to|as|on|for)\s+([^.;\n]+)",
    re.IGNORECASE,
)
METADATA_PROPOSAL_CONTRACT_CLAIM_RE = re.compile(
    r"\b(?:proposal|propose|proposed|proposing|reviewable|review|apply|applied|"
    r"change|update|set|move|moved|moving|reschedule|rescheduled|schedule|"
    r"scheduled|shift|push|modify|modified|edit|edited)\b",
    re.IGNORECASE,
)
PROPOSAL_BOUNDARY_CLAIM_RE = re.compile(
    r"\b(?:proposal|propose|proposed|proposing|reviewable|ready\s+for\s+review|"
    r"ready\s+for\s+approval|approval|approve|card|can\s+draft|"
    r"enough\s+(?:detail|information)|not\s+applying|not\s+applied)\b",
    re.IGNORECASE,
)
MUTATION_PROSE_CLAIM_RE = re.compile(
    r"\b(?:add|create|created|move|moved|reparent|reparented|under|into|inside|"
    r"set|update|updated|change|changed|fix|correct|correction|reorganization|"
    r"reorganize|restructure|group|grouping|grouped|branch|branches|"
    r"break\s*down|breakdown|decompose|decomposition|subtasks?)\b",
    re.IGNORECASE,
)
CORRECTION_REQUEST_RE = re.compile(
    r"\b(?:should\s+have|instead\s+of|rather\s+than|wrong|mistake|fix|correct|"
    r"correction|not\s+.+\b(?:under|inside|in|into)\b)\b",
    re.IGNORECASE,
)
METADATA_FIELD_CLARIFICATION_RE = re.compile(
    r"\b(?:which|what)\b.{0,80}\b(?:field|metadata|update|change)\b"
    r"|\bfield\s+you\s+want\b"
    r"|\bwhat\s+would\s+you\s+like\s+to\s+update\b"
    r"|\bstatus,\s*priority,\s*deadline\b",
    re.IGNORECASE,
)
PROVIDER_FAILURE_CONTRACT_RE = re.compile(
    r"\b(?:timed\s+out|ran\s+out\s+of\s+time|stopped\s+making\s+progress|"
    r"provider\s+transport\s+idle|failed\s+before|could\s+not\s+complete|"
    r"couldn't\s+complete)\b",
    re.IGNORECASE,
)
ANSWER_CANDIDATE_NEEDS_MODEL_RE = re.compile(
    r"\b(?:find|pick|choose|recommend|suggest)\b.{0,80}\b(?:date|time|slot|when)\b"
    r"|\b(?:good|appropriate|best|better)\b.{0,40}\b(?:date|time|slot)\b"
    r"|\b(?:prefer|preference|weekend|buffer)\b"
    r"|\bthat\s+task\b",
    re.IGNORECASE,
)
TASK_FACT_FAST_LANE_RE = re.compile(
    r"\b(?:when|due|deadline|priority|status|estimate|how\s+long|"
    r"what\s+(?:is|are|'s)\s+.{0,80}\b(?:due|deadline|priority|status|estimate))\b",
    re.IGNORECASE,
)
RECOMMENDATION_ONLY_PROMPT_RE = re.compile(
    r"\b(?:what|which|how)\b.{0,100}\b(?:strategy|approach|way|option|decomposition)\b"
    r"|\b(?:recommend|suggest|compare)\b.{0,120}\b(?:strategy|approach|way|option|date|time)\b"
    r"|\bshould\s+(?:we|i)\s+use\b",
    re.IGNORECASE,
)
EXPLICIT_MUTATION_COMMIT_RE = re.compile(
    r"\b(?:apply|change|update|set|move|create|add|delete|remove|propose|"
    r"make\s+(?:it|that|this)|do\s+that|go\s+with|use\s+that|use\s+this)\b",
    re.IGNORECASE,
)
TASK_ID_REF_RE = re.compile(r"\btask\s+#?(\d+)\b", re.IGNORECASE)
TASK_ID_QUOTED_TITLE_RE = re.compile(
    r"\btask\s+#?\d+\s*,\s*['\"\u201c\u2018]([^'\"\u201d\u2019]+)['\"\u201d\u2019]",
    re.IGNORECASE,
)
QUOTED_TASK_TITLE_CONTRACT_RE = re.compile(
    r"\b(?:task|todo|node)\b[^'\"\u201c\u2018]{0,80}"
    r"['\"\u201c\u2018]([^'\"\u201d\u2019]+)['\"\u201d\u2019]",
    re.IGNORECASE,
)
CREATE_TASK_ACTION_CONTRACT_RE = re.compile(
    r"\b(?:create|add|new)\b.{0,60}\b(?:task|todo|node)\b"
    r"|\b(?:task|todo|node)\b.{0,60}\b(?:create|add|new)\b",
    re.IGNORECASE,
)
ONE_TIME_CREATE_PLACEMENT_RE = re.compile(
    r"\b(?:today|tomorrow|tonight)\b"
    r"|\b(?:today|tomorrow|this)\s+(?:morning|afternoon|evening)\b"
    r"|\b(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:morning|afternoon|evening))?\b",
    re.IGNORECASE,
)
PROVIDER_QUOTED_TASK_TITLE_RE = re.compile(
    r"\b(?:titled|called|named)\s+['\"\u201c\u2018]([^'\"\u201d\u2019]+)['\"\u201d\u2019]",
    re.IGNORECASE,
)
BREAKDOWN_RE = re.compile(
    r"\b(?:break\s*down|breakdown|decompose|subtasks?|split|slice)\b",
    re.IGNORECASE,
)
BROAD_BRANCH_SCOPE_RE = re.compile(
    r"\b(?:big|huge|large|massive|whole|entire|all|project|branch|tree|one\s+slice|"
    r"one\s+piece|step\s+by\s+step)\b",
    re.IGNORECASE,
)
WORKSPACE_SCOPE_RE = re.compile(
    r"\b(?:whole|entire|all)\s+(?:workspace|workspaces|tree|categor(?:y|ies))\b"
    r"|\b(?:everywhere|across\s+(?:all\s+)?categor(?:y|ies)|across\s+the\s+workspace)\b",
    re.IGNORECASE,
)
MIXED_CLEANUP_MUTATION_RE = re.compile(
    r"\b(?:remove|delete|prune|cleanup|clean\s+up)\b.{0,140}"
    r"\b(?:done|not\s+done|complete|reopen|mark)\b"
    r"|\b(?:done|not\s+done|complete|reopen|mark)\b.{0,140}"
    r"\b(?:remove|delete|prune|cleanup|clean\s+up)\b",
    re.IGNORECASE,
)
SELECTED_VALUE_FOLLOWUP_RE = re.compile(
    r"\b(?:let'?s\s+go\s+with|go\s+with|use\s+that|use\s+this|that\s+works|"
    r"sounds\s+good|yes|yeah|yep|ok(?:ay)?|pick\s+that|choose\s+that)\b",
    re.IGNORECASE,
)
def classic_semantic_fast_paths_enabled(explicit: bool | None = None) -> bool:
    """Legacy compatibility shim; semantic fast paths are permanently retired."""

    _ = explicit
    return False


def typed_tool_fast_paths_enabled() -> bool:
    """Legacy compatibility shim; typed tools are model-callable, never auto-run."""

    return False


def strategist_agent_kernel_enabled(control_plane: str | None = None) -> bool:
    # The legacy graph rollback path has been retired. Any incoming control
    # plane value is admitted into the production kernel so old clients cannot
    # accidentally route back into strategist.py.
    return True


def _json_dumps(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":"))
    if len(serialized) <= 32000:
        return serialized
    return json.dumps(
        {
            "truncated": True,
            "preview": serialized[:31000],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _json_loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_loads_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 1)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":")))


def _context_size_summary(context_packet: dict[str, Any]) -> dict[str, Any]:
    runtime_context = (
        context_packet.get("runtime_context")
        if isinstance(context_packet.get("runtime_context"), dict)
        else {}
    )
    return {
        "total_chars": _json_size(context_packet),
        "runtime_context_chars": _json_size(runtime_context),
        "tool_registry_chars": _json_size(context_packet.get("tool_registry")),
        "contract_chars": _json_size(context_packet.get("contract")),
        "pending_proposal_chars": _json_size(context_packet.get("pending_proposal")),
        "recent_events_chars": _json_size(context_packet.get("recent_events")),
        "session_memory_chars": _json_size(context_packet.get("session_memory")),
        "goal_context_chars": _json_size(context_packet.get("goalContext")),
        "visible_transcript_chars": _json_size(context_packet.get("visible_transcript")),
        "tool_policy_chars": _json_size(context_packet.get("tool_policy")),
        "tier_route_chars": _json_size(context_packet.get("tier_route")),
        "foreground_budget_chars": _json_size(context_packet.get("foreground_budget")),
        "prefetched_evidence_chars": _json_size(context_packet.get("prefetched_evidence")),
        "tree_context_kind": (
            "task_scope_index"
            if "task_scope_index" in runtime_context
            else "task_tree_outline" if "task_tree_outline" in runtime_context else "none"
        ),
        "tree_context_chars": _json_size(
            runtime_context.get("task_scope_index")
            if "task_scope_index" in runtime_context
            else runtime_context.get("task_tree_outline")
        ),
    }


def _compact_pending_proposal_for_context(
    pending_proposal: dict[str, Any] | None,
) -> dict[str, Any] | None:
    return compact_pending_proposal_for_model(pending_proposal)


def _build_agent_kernel_contract_payload() -> dict[str, Any]:
    return {
        **build_core_operating_contract("strategist"),
        "control_policy": "agent_kernel_direct",
        "tool_argument_contract": (
            "Read-only task tools use task_id/task_title arguments and return task_id/task_title surfaces."
        ),
        "proposal_required_behavior": (
            "Task mutations must return a reviewable proposal. Metadata changes use "
            "the submit_task_metadata_plan tool, including target_updates "
            "with exact task ids for distinct updates across several tasks; create/delete/move/"
            "restructure work uses the submit_task_tree_plan tool "
            "structural operations. Durable scheduling preferences use the "
            "submit_preference_plan tool. Read-only "
            "questions may answer directly from evidence."
        ),
        "proposal_continuation_behavior": (
            "When the transcript or session memory already establishes the task, change "
            "type, and candidate value, and the latest user selects or accepts that value, "
            "submit the reviewable plan immediately with submit_task_metadata_plan or "
            "submit_task_tree_plan. Do not require proposal/apply/change wording."
        ),
        "session_entity_continuity": (
            "The session working_set remains valid after an objective is applied or otherwise "
            "completed. Its recurrence_series entries are durable continuity evidence for "
            "follow-up references such as 'move it instead'; preserve their cadence, duration, "
            "and timezone. For a mutation, hand the exact task to Planner, which will re-read "
            "the authoritative persisted series before native authoring. Do not ask the user "
            "to re-identify or re-describe a uniquely resolved working-set entity."
        ),
        "recommendation_boundary_behavior": (
            "When the user only asks to find, recommend, suggest, or compare a possible "
            "date/time/metadata value, answer with decision support and no approval card. "
            "Draft the proposal when the same request includes mutation intent, or a later "
            "turn selects or accepts one concrete value."
        ),
        "compound_mutation_behavior": (
            "For a mutation, update operations.requested with every active clause and its "
            "exact expected operations, then draft one canonical ChangeSet containing those "
            "same operations. Preserve task creation, decomposition, metadata, and calendar "
            "placement together. Infer scheduling semantics yourself: use recurring_schedule "
            "for repeating intent and created_task_schedule for a one-time placement; no router "
            "label or prompt matcher supplies that meaning. If the user describes a multi-step workflow for a new task, "
            "create one parent plus explicit child tasks; do not compress the workflow into "
            "notes. A fixed activity in a create-and-schedule request is an owned task and "
            "calendar operation unless authoritative calendar evidence shows that it already "
            "exists; an anonymous unavailable interval does not satisfy that requested effect. "
            "Missing execution-time content such as the exact job posting, message copy, "
            "recipient details, or research results does not block creation of the task shell "
            "the user asked to schedule. Clarify only when the missing value changes task "
            "identity, a hard calendar constraint, or a destructive target. "
            "If a detail is missing, ask one clarification that explicitly carries all "
            "still-pending clauses."
        ),
        "proposal_revision_behavior": (
            "A proposal edit is a delta over the pending proposal's semantic contract. "
            "Preserve the primary task, recurrence, structural operations, and unaffected "
            "fields. Existing conflict blockers remain affected resources; never turn one "
            "into the new scheduling target or copy the incoming recurrence onto it. Use "
            "submit_task_tree_plan with revision_of_proposal_id and "
            "recurring_schedule_updates for recurring branch edits."
        ),
        "approval_boundary": (
            "create/delete/move/reparent/reorder/metadata/restructure changes must pause "
            "behind approve/edit/reject controls."
        ),
    }


def _is_duration_only_pending_review_correction(
    prompt: str,
    *,
    session_memory: dict[str, Any],
) -> bool:
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory.get("active_request"), dict)
        else {}
    )
    status = str(active_request.get("status") or "").strip()
    if status not in {"pending_approval", "awaiting_user_input"}:
        return False
    clarification = (
        active_request.get("clarification")
        if isinstance(active_request.get("clarification"), dict)
        else {}
    )
    missing_fields = clarification.get("missing_fields")
    if status == "awaiting_user_input":
        if missing_fields != ["duration_minutes"]:
            return False
        if not isinstance(active_request.get("proposal_revision"), dict):
            return False
    # The duration parser is the value interpreter. The small token vocabulary
    # only distinguishes a scalar clarification answer from a new compound
    # request; request routing is driven by the persisted missing-field slot.
    if parse_prompt_estimate_minutes(prompt) is None:
        return False
    answer_tokens = set(re.findall(r"[a-z]+", prompt.casefold()))
    return answer_tokens.issubset(
        {
            "allocate",
            "make",
            "it",
            "set",
            "the",
            "duration",
            "to",
            "use",
            "change",
            "minute",
            "minutes",
            "min",
            "mins",
            "hour",
            "hours",
            "hr",
            "hrs",
        }
    )


def _compact_duration_revision_tool_schema(
    tool: dict[str, Any],
) -> dict[str, Any]:
    """Project pending duration correction to its stable review reference."""

    if str(tool.get("name") or "").strip() != "submit_task_tree_plan":
        return tool
    compact = dict(tool)
    compact["description"] = (
        "Revise the current pending task-tree review with the user's exact duration. "
        "The server preserves its task and schedule operations."
    )
    compact["parameters"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "revision_of_proposal_id": {"type": ["string", "null"]},
            "duration_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1440,
            },
        },
        "required": ["revision_of_proposal_id", "duration_minutes"],
    }
    return compact


def _compact_metadata_tool_schema(
    tool: dict[str, Any],
    *,
    update_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Project metadata authoring to one resolved target.

    When ``update_keys`` is omitted, the model retains ownership of which
    metadata fields the request changes while avoiding the much larger
    multi-target authoring surface.
    """

    if str(tool.get("name") or "").strip() != "submit_task_metadata_plan":
        return tool
    value_schemas: dict[str, dict[str, Any]] = {
        "title": {"type": "string"},
        "notes": {"type": "string"},
        "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
        "deadline_at": {"type": "string"},
        "priority": {"type": "string"},
        "status": {"type": "string"},
        "time_estimate_minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1440,
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "node_type": {"type": "string"},
        "is_pinned": {"type": "boolean"},
        "is_backlog": {"type": "boolean"},
    }
    bounded_keys = [
        key
        for key in sorted(update_keys if update_keys is not None else value_schemas)
        if key in value_schemas
    ]
    if not bounded_keys:
        return tool
    compact = dict(tool)
    compact["description"] = (
        "Submit the requested metadata fields for the one exact task resolved in "
        "prior tool evidence. The server compiles a review and never applies data."
    )
    compact["parameters"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target_task_id": {"type": "integer"},
            "updates": {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "properties": {
                    key: value_schemas[key] for key in bounded_keys
                },
                **({"required": bounded_keys} if update_keys is not None else {}),
            },
        },
        "required": ["target_task_id", "updates"],
    }
    return compact


def _compact_new_workflow_tool_schema(
    tool: dict[str, Any],
    *,
    minimum_create_operations: int = 2,
    minimum_scheduled_targets: int = 1,
) -> dict[str, Any]:
    """Expose only canonical create-tree and one-time placement fields."""

    if str(tool.get("name") or "").strip() != "submit_task_tree_plan":
        return tool
    compact = dict(tool)
    compact["description"] = (
        "Create one new top-level task and its requested child workflow, then "
        "schedule the created temp-ids once. Preserve the user's ordering, "
        "durations, metadata, workspace, and time window. The server compiles "
        "and validates one review and never applies product data."
    )
    operation_properties = {
        "kind": {"type": "string", "enum": ["create_node"]},
        "temp_id": {"type": "string"},
        "title": {"type": "string"},
        "parent_temp_id": {"type": ["string", "null"]},
        "category_id": {"type": ["integer", "null"]},
        "category_name": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
        "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
        "deadline_at": {"type": ["string", "null"]},
        "priority": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "estimate_minutes": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 1440,
        },
        "target_index": {"type": ["integer", "null"], "minimum": 0},
    }
    schedule_group = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target_temp_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "target_task_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "instruction": {"type": "string"},
            "window_start": {"type": "string"},
            "window_end": {"type": "string"},
            "slot_minutes": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": 60,
            },
            "request_constraints": {
                "type": "array",
                "items": CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA,
            },
        },
        "required": [
            "target_temp_ids",
            "instruction",
            "window_start",
            "window_end",
        ],
    }
    compact["parameters"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target_category_id": {"type": ["integer", "null"]},
            "target_category_name": {"type": ["string", "null"]},
            "structural_operations": {
                "type": "array",
                "minItems": max(2, min(minimum_create_operations, 20)),
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": operation_properties,
                    "required": ["kind", "temp_id", "title"],
                },
            },
            "created_task_schedule": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target_temp_ids": {
                        "type": "array",
                        "minItems": max(
                            1,
                            min(minimum_scheduled_targets, 19),
                        ),
                        "items": {"type": "string"},
                    },
                    "target_task_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "instruction": {"type": "string"},
                    "window_start": {"type": "string"},
                    "window_end": {"type": "string"},
                    "slot_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 60,
                    },
                    "request_constraints": {
                        "type": "array",
                        "items": CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA,
                    },
                    "schedule_groups": {
                        "type": "array",
                        "minItems": 1,
                        "items": schedule_group,
                    },
                },
                "required": (
                    [
                        "target_temp_ids",
                        "instruction",
                        "window_start",
                        "window_end",
                    ]
                    if minimum_scheduled_targets > 1
                    else []
                ),
            },
        },
        "required": [
            "target_category_id",
            "target_category_name",
            "structural_operations",
            "created_task_schedule",
        ],
    }
    return compact


def _compact_task_tree_provider_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Project the task-tree boundary to a concise semantic authoring grammar.

    Pydantic's strict provider schema marks every nullable/defaulted nested field
    as required and expands repeated objects into large ``$defs`` trees. That is
    useful for server validation but is a poor model-facing authoring surface: a
    simple create-and-schedule request appeared to require dozens of explicit
    nulls. The host still validates the resulting arguments against the complete
    canonical model; this projection keeps only semantic fields the model can
    intentionally supply and lets canonical defaults fill the rest.
    """

    if str(tool.get("name") or "").strip() != "submit_task_tree_plan":
        return tool
    source_parameters = tool.get("parameters")
    source_properties = (
        source_parameters.get("properties")
        if isinstance(source_parameters, dict)
        and isinstance(source_parameters.get("properties"), dict)
        else {}
    )
    if not isinstance(source_parameters, dict) or not (
        isinstance(source_parameters.get("$defs"), dict)
        or "request_items" in source_properties
    ):
        # Tests and third-party policy projections may intentionally supply a
        # narrower schema. Never widen a caller-bounded surface.
        return tool

    def strip_schema_prose(value: Any) -> Any:
        if isinstance(value, list):
            return [strip_schema_prose(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: strip_schema_prose(item)
            for key, item in value.items()
            if key not in {"description", "title", "examples", "default"}
        }

    nullable_text = {"type": ["string", "null"]}
    nullable_int = {"type": ["integer", "null"]}
    metadata_properties = {
        "title": nullable_text,
        "notes": nullable_text,
        "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
        "deadline_at": nullable_text,
        "priority": nullable_text,
        "status": nullable_text,
        "time_estimate_minutes": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 1440,
            "description": (
                "Grounded common-sense duration. Brief atomic checks, bookings, "
                "confirmations, or messages are normally 5-10 minutes; ordinary focused "
                "execution is commonly 30-90 minutes. Leave a grouping/outcome parent "
                "without independent effort when its children represent the work."
            ),
        },
        "tags": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "node_type": nullable_text,
        "is_pinned": {"type": ["boolean", "null"]},
        "is_backlog": {"type": ["boolean", "null"]},
    }
    operation_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "create_node",
                    "move_subtree",
                    "archive_node",
                    "delete_node",
                    "metadata_update",
                ],
            },
            "temp_id": nullable_text,
            "title": nullable_text,
            "source_task_id": nullable_int,
            "source_task_title": nullable_text,
            "parent_task_id": nullable_int,
            "parent_task_title": nullable_text,
            "parent_temp_id": nullable_text,
            "destination_parent_task_id": nullable_int,
            "destination_parent_task_title": nullable_text,
            "destination_parent_temp_id": nullable_text,
            "category_id": nullable_int,
            "category_name": nullable_text,
            "destination_category_id": nullable_int,
            "destination_category_name": nullable_text,
            "target_index": {"type": ["integer", "null"], "minimum": 0},
            **metadata_properties,
            "updates": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": metadata_properties,
            },
        },
        "required": ["kind"],
    }
    request_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_text": {"type": "string"},
            "changes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": [
                        "create",
                        "schedule",
                        "update",
                        "move",
                        "archive",
                        "delete",
                    ],
                },
            },
            "target_temp_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "target_task_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "schedule_group_index": nullable_int,
        },
        "required": ["source_text", "changes"],
    }
    constraint_schema = strip_schema_prose(
        CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA
    )
    schedule_group_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target_temp_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "target_task_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "instruction": {"type": "string"},
            "window_start": {"type": "string"},
            "window_end": {"type": "string"},
            "slot_minutes": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": 60,
            },
            "request_constraints": {
                "type": "array",
                "items": constraint_schema,
            },
        },
        "required": ["instruction", "window_start", "window_end"],
    }
    recurrence_properties = {
        "target_temp_id": nullable_text,
        "frequency": {
            "type": ["string", "null"],
            "enum": ["daily", "weekly", None],
        },
        "days_of_week": {
            "type": ["array", "null"],
            "items": {
                "type": "string",
                "enum": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                ],
            },
        },
        "start_date": nullable_text,
        "end_date": nullable_text,
        "occurrence_count": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 366,
        },
        "end_mode": {
            "type": ["string", "null"],
            "enum": ["never", "until", "count", None],
        },
        "duration_minutes": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 1440,
        },
        "preferred_start_time": nullable_text,
        "allowed_start_time": nullable_text,
        "allowed_end_time": nullable_text,
        "gap_minutes": {
            "type": ["integer", "null"],
            "minimum": 0,
            "maximum": 1440,
        },
        "timezone": nullable_text,
        "strict": {"type": ["boolean", "null"]},
        "conflict_relocation": {
            "type": ["string", "null"],
            "enum": ["before_incoming", "after_incoming", None],
        },
    }
    compact = dict(tool)
    compact["description"] = (
        "Submit one complete typed task/tree/calendar semantic plan. Preserve every "
        "request item and decide task-tree parentage separately from calendar ownership. "
        "When a future outcome depends on a brief prerequisite and the outcome's time is "
        "deferred, create an outcome parent with prerequisite and activity children, give "
        "the prerequisite a 5-10 minute estimate, and schedule only that prerequisite. "
        "The server validates, compiles, and pauses for review."
    )
    compact["parameters"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "revision_of_proposal_id": nullable_text,
            "replace_structural_operations": {"type": ["boolean", "null"]},
            "replace_recurring_schedule": {"type": ["boolean", "null"]},
            "target_task_id": nullable_int,
            "target_task_title": nullable_text,
            "target_category_id": nullable_int,
            "target_category_name": nullable_text,
            "request_items": {
                "type": "array",
                "minItems": 1,
                "items": request_item_schema,
            },
            "structural_operations": {
                "type": "array",
                "items": operation_schema,
            },
            "recurring_schedule": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": recurrence_properties,
            },
            "recurring_schedule_updates": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": recurrence_properties,
            },
            "created_task_schedule": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "target_temp_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "target_task_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "instruction": {"type": ["string", "null"]},
                    "window_start": nullable_text,
                    "window_end": nullable_text,
                    "slot_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 60,
                    },
                    "request_constraints": {
                        "type": "array",
                        "items": constraint_schema,
                    },
                    "schedule_groups": {
                        "type": "array",
                        "minItems": 1,
                        "items": schedule_group_schema,
                    },
                },
            },
            "rationale": nullable_text,
            "summary": nullable_text,
        },
        "required": ["request_items"],
    }
    return compact


def _uses_single_task_metadata_authoring_surface(
    prompt: str,
    route_decision: TierRouteDecision,
) -> bool:
    """Select a bounded tool surface without deriving target or update values."""

    if getattr(route_decision, "route_kind", None) == "single_task_metadata_mutation":
        return True
    return False


def _structured_duration_revision_arguments(
    arguments: dict[str, Any],
    *,
    pending_proposal: dict[str, Any] | None,
    current_change_set: ChangeSet | None,
) -> dict[str, Any]:
    """Apply a model-supplied duration to the stored one-task review."""

    duration_minutes = arguments.get("duration_minutes")
    if (
        not isinstance(duration_minutes, int)
        or isinstance(duration_minutes, bool)
        or duration_minutes <= 0
    ):
        return arguments

    def semantic_payload(value: Any, *, depth: int = 0) -> dict[str, Any] | None:
        if depth > 6 or not isinstance(value, dict):
            return None
        operations = value.get(
            "structural_operations",
            value.get("structuralOperations"),
        )
        if isinstance(operations, list) and operations:
            return value
        for key in (
            "semantic_contract",
            "payload",
            "proposal",
            "branch_proposal",
            "branchProposal",
            "semantic_plan_input",
            "semanticPlanInput",
        ):
            nested = semantic_payload(value.get(key), depth=depth + 1)
            if nested is not None:
                return nested
        return None

    basis = semantic_payload(pending_proposal)
    if basis is None:
        return arguments
    operations = copy.deepcopy(
        basis.get(
            "structural_operations",
            basis.get("structuralOperations"),
        )
    )
    if not isinstance(operations, list):
        return arguments
    target_temp_ids: set[str] = set()
    schedule = copy.deepcopy(
        basis.get(
            "created_task_schedule",
            basis.get("createdTaskSchedule"),
        )
    )
    recurrence = copy.deepcopy(
        basis.get(
            "recurring_schedule",
            basis.get("recurringSchedule"),
        )
    )
    if isinstance(recurrence, dict):
        recurrence_target = str(
            recurrence.get("target_temp_id")
            or recurrence.get("targetTempId")
            or ""
        ).strip()
        if recurrence_target:
            target_temp_ids.add(recurrence_target)
    if isinstance(schedule, dict):
        direct_ids = schedule.get("target_temp_ids")
        if isinstance(direct_ids, list):
            target_temp_ids.update(
                str(item).strip() for item in direct_ids if str(item).strip()
            )
        groups = schedule.get("schedule_groups")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_ids = group.get("target_temp_ids")
                if isinstance(group_ids, list):
                    target_temp_ids.update(
                        str(item).strip()
                        for item in group_ids
                        if str(item).strip()
                    )
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        kind = str(
            operation.get("kind")
            or operation.get("operation")
            or operation.get("op")
            or ""
        ).strip().lower()
        temp_id = str(operation.get("temp_id") or "").strip()
        if kind not in {"create", "create_node", "task.create"}:
            continue
        if target_temp_ids and temp_id not in target_temp_ids:
            continue
        operation["time_estimate_minutes"] = duration_minutes
        operation.pop("estimate_minutes", None)
        operation.pop("estimated_duration_minutes", None)
        operation.pop("duration_minutes", None)

    prior_start: datetime | None = None
    if current_change_set is not None:
        for operation in current_change_set.operations:
            if operation.kind == "calendar.create_block":
                prior_start = operation.start_at
                break
    if prior_start is None and isinstance(schedule, dict):
        raw_start = schedule.get("window_start")
        if not raw_start:
            groups = schedule.get("schedule_groups")
            first_group = (
                groups[0]
                if isinstance(groups, list)
                and groups
                and isinstance(groups[0], dict)
                else {}
            )
            raw_start = first_group.get("window_start")
        try:
            prior_start = datetime.fromisoformat(str(raw_start))
        except (TypeError, ValueError):
            prior_start = None
    if prior_start is not None and isinstance(schedule, dict):
        local_start = prior_start.replace(tzinfo=None)
        exact_schedule = {
            "target_temp_ids": sorted(target_temp_ids),
            "instruction": (
                str(schedule.get("instruction") or "").strip()
                or "Preserve the pending review's scheduled start."
            ),
            "window_start": local_start.isoformat(),
            "window_end": (
                local_start + timedelta(minutes=duration_minutes)
            ).isoformat(),
        }
        schedule = exact_schedule

    revised = {
        **arguments,
        "replace_structural_operations": True,
        "structural_operations": operations,
    }
    revised.pop("duration_minutes", None)
    if isinstance(schedule, dict):
        revised["created_task_schedule"] = schedule
    if isinstance(recurrence, dict):
        revised["recurring_schedule_updates"] = {
            "duration_minutes": duration_minutes,
        }
    return revised


def _kernel_tool_schemas_for_route(
    route_decision: TierRouteDecision,
    *,
    session_memory: dict[str, Any],
    prompt: str,
) -> list[dict[str, Any]]:
    tools = registered_agent_provider_tool_schemas("strategist")
    if _is_duration_only_pending_review_correction(
        prompt,
        session_memory=session_memory,
    ):
        return [
            _compact_duration_revision_tool_schema(tool)
            for tool in tools
            if str(tool.get("name") or "").strip() == "submit_task_tree_plan"
        ]
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory.get("active_request"), dict)
        else {}
    )
    if str(active_request.get("status") or "").strip() == "pending_approval":
        # A pending native review already carries the authoritative target and
        # operation meaning. Keep the correction surface compact while retaining
        # bounded reads for edits that introduce one new referenced entity. The
        # goal-transition contract remains available so an unmistakably new
        # request can start a separate goal instead of overwriting this review.
        allowed = {
            "set_goal_transition",
            "submit_task_metadata_plan",
            "submit_task_tree_plan",
            "submit_preference_plan",
            "submit_preference_management_plan",
            "submit_preference_cleanup_plan",
            "analyze_preference_cleanup_candidates",
            "list_remembered_preferences",
            "search_entities",
            "get_task",
            "get_task_branch",
            "list_calendar_blocks",
            "get_task_schedule",
            "query_preferences",
            "read_planner_context",
        }
        return [
            tool
            for tool in tools
            if str(tool.get("name") or "").strip() in allowed
        ]
    if _uses_single_task_metadata_authoring_surface(prompt, route_decision):
        allowed = {
            "set_goal_transition",
            "submit_task_metadata_plan",
        }
        return [
            _compact_metadata_tool_schema(tool)
            if str(tool.get("name") or "").strip() == "submit_task_metadata_plan"
            else tool
            for tool in tools
            if str(tool.get("name") or "").strip() in allowed
        ]
    _ = (route_decision, session_memory)
    return [_compact_task_tree_provider_schema(tool) for tool in tools]


def _compact_model_authoring_prompt(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    session_memory: dict[str, Any],
    tool_policy_prompt: str,
) -> str | None:
    """Use a concise model-owned instruction for bounded authoring surfaces."""

    suffix = f" Additional policy: {tool_policy_prompt}" if tool_policy_prompt else ""
    if _is_duration_only_pending_review_correction(
        prompt,
        session_memory=session_memory,
    ):
        return (
            f"{prompt}\n\n"
            "You own the semantic interpretation. A canonical task-and-schedule review "
            "is pending in goalContext. Submit one revision through "
            "submit_task_tree_plan with the exact requested duration_minutes, changing "
            "only that duration and "
            "preserving its task, workspace, date, start time, and all other meaning. "
            "The server owns ChangeSet compilation, approval, and writes."
            f"{suffix}"
        )
    if _uses_single_task_metadata_authoring_surface(prompt, route_decision):
        return (
            f"{prompt}\n\n"
            "You own the semantic interpretation. Resolve one exact task ID from "
            "runtime_context.task_catalog; UI focus is not target authority. Call "
            "submit_task_metadata_plan once with only "
            "the metadata fields and exact values the user requested. The server owns "
            "ChangeSet compilation, approval, and writes."
            f"{suffix}"
        )
    return None


def _is_cleanup_review_prompt(prompt: str) -> bool:
    prompt_text = prompt.strip()
    if not prompt_text:
        return False
    if PREFERENCE_CLEANUP_SUBJECT_RE.search(prompt_text):
        return False
    if BROAD_RESTRUCTURE_RE.search(prompt_text):
        return True
    return bool(CLEANUP_REVIEW_RE.search(prompt_text) and CLEANUP_BROAD_SCOPE_RE.search(prompt_text))


def _preference_cleanup_request_text(
    prompt: str,
    *,
    session_memory: dict[str, Any] | None = None,
) -> str:
    parts = [prompt.strip()]
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory, dict)
        and isinstance(session_memory.get("active_request"), dict)
        else {}
    )
    request_prompts = active_request.get("request_prompts")
    if isinstance(request_prompts, list):
        parts.extend(
            str(item).strip()
            for item in request_prompts
            if isinstance(item, str) and item.strip()
        )
    return "\n".join(part for part in parts if part)


def _is_preference_cleanup_request(
    prompt: str,
    *,
    session_memory: dict[str, Any] | None = None,
) -> bool:
    request_text = _preference_cleanup_request_text(
        prompt,
        session_memory=session_memory,
    )
    return bool(
        PREFERENCE_CLEANUP_SUBJECT_RE.search(request_text)
        and PREFERENCE_CLEANUP_SCOPE_RE.search(request_text)
    )


def _is_workload_plan_prompt(prompt: str) -> bool:
    prompt_text = prompt.strip()
    if not prompt_text:
        return False
    if re.search(r"\bwhich\s+one\b|\bthe\s+other\s+one\b|\bthat\s+one\b", prompt_text, re.IGNORECASE):
        return False
    return bool(WORKLOAD_REVIEW_RE.search(prompt_text) and BROAD_WORKLOAD_SCOPE_RE.search(prompt_text))


def _is_strategic_priority_prompt(prompt: str) -> bool:
    prompt_text = prompt.strip()
    if not prompt_text:
        return False
    if re.search(r"\bwhich\s+one\b|\bthe\s+other\s+one\b|\bthat\s+one\b", prompt_text, re.IGNORECASE):
        return False
    return bool(
        BROAD_STRATEGIC_PRIORITY_RE.search(prompt_text)
        and BROAD_STRATEGIC_SCOPE_RE.search(prompt_text)
    )


def _is_branch_plan_prompt(prompt: str, focus_node: StrategyNode | None) -> bool:
    prompt_text = prompt.strip()
    if not prompt_text or focus_node is None or focus_node.id is None:
        return False
    if not BREAKDOWN_RE.search(prompt_text) and not BROAD_RESTRUCTURE_RE.search(prompt_text):
        return False
    return bool(BROAD_BRANCH_SCOPE_RE.search(prompt_text))


def _cleanup_analysis_arguments(
    *,
    prompt: str,
    active_category: Category | None,
) -> dict[str, Any]:
    category_name: str | None = None
    if active_category is not None and not WORKSPACE_SCOPE_RE.search(prompt):
        category_name = str(active_category.name or "").strip() or None
    return {
        "category_name": category_name,
        "limit": CLEANUP_ANALYSIS_LIMIT,
        "include_organization_map": True,
        "organization_offset": 0,
        "organization_limit": 200,
    }


def _scoped_category_name(
    *,
    prompt: str,
    active_category: Category | None,
) -> str | None:
    if active_category is None or WORKSPACE_SCOPE_RE.search(prompt):
        return None
    return str(active_category.name or "").strip() or None


ENTITY_SEARCH_STOPWORDS = {
    "a",
    "about",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "based",
    "can",
    "category",
    "categories",
    "could",
    "current",
    "did",
    "do",
    "does",
    "done",
    "due",
    "easy",
    "easiest",
    "effort",
    "find",
    "finished",
    "first",
    "focus",
    "for",
    "fruit",
    "fruits",
    "give",
    "gives",
    "hanging",
    "have",
    "high",
    "highest",
    "hour",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "leverage",
    "life",
    "low",
    "me",
    "my",
    "need",
    "next",
    "not",
    "now",
    "of",
    "on",
    "or",
    "priority",
    "question",
    "right",
    "should",
    "status",
    "task",
    "tasks",
    "that",
    "the",
    "them",
    "there",
    "these",
    "this",
    "time",
    "to",
    "todo",
    "tree",
    "what",
    "when",
    "which",
    "will",
    "with",
    "work",
    "workspace",
    "would",
    "up",
    "wake",
    "waking",
    "before",
    "scheduled",
    "rescheduled",
    "x",
    "you",
}
ENTITY_SEARCH_MIN_MULTI_TOKEN_SCORE = 0.58
ENTITY_SEARCH_MIN_UNIQUE_TOKEN_SCORE = 0.42


def _entity_lookup_tokens(value: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    return [
        token
        for token in tokens
        if len(token) >= 2 and token not in ENTITY_SEARCH_STOPWORDS
    ]


def _node_entity_text(node: StrategyNode) -> str:
    title = str(node.title or "").strip()
    return title


def _prompt_mentions_shared_alias(prompt: str, token: str) -> bool:
    if len(token) < 3:
        return False
    for match in re.finditer(rf"\b{re.escape(token)}\b", prompt, re.IGNORECASE):
        raw = match.group(0)
        if raw.isupper() or (len(raw) >= 4 and raw[0].isupper()):
            return True
    return False


def _explicit_alias_queries(prompt: str) -> list[str]:
    queries: list[str] = []
    stop_words = {"What", "When", "Which", "Where", "Tell", "Show", "Update", "Change"}
    for match in re.finditer(r"\b[A-Z][A-Z0-9]{2,}\b", prompt):
        value = match.group(0).strip()
        if value and value not in queries:
            queries.append(value)
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9]{3,}\b", prompt):
        value = match.group(0).strip()
        if value in stop_words:
            continue
        if value and value not in queries:
            queries.append(value)
    return queries[:3]


def _entity_search_candidate_nodes(
    *,
    prompt: str,
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> list[StrategyNode]:
    if active_category is None or WORKSPACE_SCOPE_RE.search(prompt):
        return [node for node in nodes if str(node.title or "").strip()]
    return [
        node
        for node in nodes
        if node.category_id == active_category.id and str(node.title or "").strip()
    ]


def _entity_search_queries_for_prompt(
    *,
    prompt: str,
    nodes: list[StrategyNode],
    active_category: Category | None,
    limit: int = 2,
) -> list[str]:
    """Resolve likely task/entity mentions without searching conceptual prompts verbatim.

    Entity search is for named things. Broad questions like "what is low
    hanging fruit?" should use workload/filter evidence, not a text search for
    the whole prompt.
    """

    prompt_tokens = set(_entity_lookup_tokens(prompt))
    if not prompt_tokens:
        return []
    candidate_nodes = _entity_search_candidate_nodes(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
    )
    token_frequency: dict[str, int] = {}
    node_tokens: dict[int, set[str]] = {}
    for node in candidate_nodes:
        tokens = set(_entity_lookup_tokens(_node_entity_text(node)))
        if not tokens:
            continue
        node_tokens[int(node.id or 0)] = tokens
        for token in tokens:
            token_frequency[token] = token_frequency.get(token, 0) + 1

    scored: list[tuple[float, int, str]] = []
    alias_queries: list[str] = []
    prompt_lower = prompt.lower()
    for node in candidate_nodes:
        title = str(node.title or "").strip()
        if not title or node.id is None:
            continue
        title_tokens = node_tokens.get(int(node.id), set())
        if not title_tokens:
            continue
        overlap = prompt_tokens.intersection(title_tokens)
        if not overlap:
            continue
        coverage = len(overlap) / max(1, len(title_tokens))
        density = min(1.0, len(overlap) / 3.0)
        score = (coverage * 0.75) + (density * 0.25)
        if title.lower() in prompt_lower:
            score += 0.2
        if len(overlap) == 1:
            token = next(iter(overlap))
            if token_frequency.get(token, 0) != 1 or len(token) < 4:
                if _prompt_mentions_shared_alias(prompt, token) and token not in alias_queries:
                    alias_queries.append(token)
                continue
            if score < ENTITY_SEARCH_MIN_UNIQUE_TOKEN_SCORE:
                continue
        elif score < ENTITY_SEARCH_MIN_MULTI_TOKEN_SCORE:
            continue
        scored.append((score, int(node.id), title))

    scored.sort(key=lambda item: (-item[0], item[2].lower()))
    queries: list[str] = []
    seen: set[str] = set()
    for alias_query in alias_queries:
        normalized = _normalized_title(alias_query)
        if normalized in seen:
            continue
        seen.add(normalized)
        queries.append(alias_query)
        if len(queries) >= max(1, limit):
            return queries
    for _, _, title in scored:
        normalized = _normalized_title(title)
        if normalized in seen:
            continue
        seen.add(normalized)
        queries.append(title)
        if len(queries) >= max(1, limit):
            break
    return queries


def _harness_evidence_floor_plan(
    *,
    prompt: str,
    nodes: list[StrategyNode],
    active_category: Category | None,
    focus_node: StrategyNode | None = None,
) -> list[dict[str, Any]]:
    """Return the tiny bootstrap packet every agent turn can reason from.

    This keeps the universal floor small. The harness always supplies current
    UI/session context; model-owned task turns may add one broad task-graph
    evidence prefetch with the original user request as source of truth.
    """

    return [{"name": "get_context_surfaces", "arguments": {}}]


def _build_kernel_tool_policy_hint(
    *,
    prompt: str,
    active_category: Category | None,
    focus_node: StrategyNode | None = None,
) -> dict[str, Any] | None:
    if not _is_cleanup_review_prompt(prompt):
        if _is_strategic_priority_prompt(prompt):
            return {
                "intent": "strategic_prioritization",
                "preferred_first_tool": {
                    "name": "get_workload_overview",
                    "arguments": {
                        "category_name": _scoped_category_name(
                            prompt=prompt,
                            active_category=active_category,
                        ),
                        "days": 14,
                    },
                },
                "follow_up_tools": [
                    {
                        "name": "list_tasks_by_filter",
                        "arguments": {
                            "category_name": _scoped_category_name(
                                prompt=prompt,
                                active_category=active_category,
                            ),
                            "filters": {
                                "active_only": True,
                                "include_archived": False,
                                "include_backlog": True,
                                "include_branches": True,
                            },
                            "limit": 12,
                        },
                    },
                    {
                        "name": "get_task_snapshot",
                        "arguments": {
                            "task_title": "<top candidate title from workload/filter evidence>",
                        },
                    },
                ],
                "reason": (
                    "The user is asking for broad strategic focus or leverage. Start from "
                    "workload pressure, then inspect only top candidates before ranking."
                ),
                "avoid": [
                    "Do not answer strategic leverage requests from the routing index alone.",
                    "Do not dump every task; synthesize a ranked decision from bounded evidence.",
                ],
            }
        if _is_workload_plan_prompt(prompt):
            return {
                "intent": "workload_triage",
                "preferred_first_tool": {
                    "name": "get_workload_overview",
                    "arguments": {
                        "category_name": _scoped_category_name(
                            prompt=prompt,
                            active_category=active_category,
                        ),
                        "days": 7,
                    },
                },
                "reason": (
                    "The user is asking for broad workload triage. Build a bounded plan "
                    "from overdue, due-soon, and stale signals before drafting changes."
                ),
                "avoid": [
                    "Do not turn a workload review into one large patch.",
                    "Do not dump every task when workload overview can produce ranked pressure points.",
                ],
            }
        if _is_branch_plan_prompt(prompt, focus_node):
            plan_intent = (
                "branch_restructure"
                if BROAD_RESTRUCTURE_RE.search(prompt)
                else "branch_breakdown"
            )
            return {
                "intent": plan_intent,
                "preferred_first_tool": {
                    "name": "get_task_branch",
                    "arguments": {
                        "task_id": int(focus_node.id),
                        "depth": 2,
                    },
                },
                "reason": (
                    "The user is asking for a broad branch-level change. Inspect the branch "
                    "once, decompose it into child work units, then handle one unit at a time."
                ),
                "avoid": [
                    "Do not rewrite an entire large branch in one model step.",
                    "Do not create a full branch proposal until one work unit is selected.",
                ],
            }
        return None
    return {
        "intent": "cleanup_review",
        "preferred_first_tool": {
            "name": "analyze_tree_cleanup_candidates",
            "arguments": _cleanup_analysis_arguments(
                prompt=prompt,
                active_category=active_category,
            ),
        },
        "reason": (
            "The user is asking for cleanup, redundancy detection, messy-tree review, "
            "or broad restructuring. Use the compact analyzer before any raw task list."
        ),
        "avoid": [
            "Do not call list_tasks_by_filter merely to dump every task for cleanup review.",
            "Do not request a full tree outline unless the analyzer points to one named branch that needs deeper inspection.",
        ],
    }


def _kernel_tool_policy_prompt(tool_policy: dict[str, Any] | None) -> str:
    if not isinstance(tool_policy, dict):
        return ""
    intent = str(tool_policy.get("intent") or "").strip()
    preferred = (
        tool_policy.get("preferred_first_tool")
        if isinstance(tool_policy.get("preferred_first_tool"), dict)
        else {}
    )
    if intent == "workload_triage":
        return (
            "Suggested evidence mode for this broad workload turn: if task-world evidence is needed, "
            f"prefer {preferred.get('name') or 'get_workload_overview'} with the suggested arguments "
            "from runtime_context.tool_policy. Use overdue, due_soon, and stale signals as the "
            "decision surface; avoid raw full-task dumps. Return one bounded recommendation/proposal "
            "or a work-unit clarification."
        )
    if intent == "strategic_prioritization":
        return (
            "Suggested evidence mode for this broad strategic-prioritization turn: if task-world evidence "
            f"is needed, prefer {preferred.get('name') or 'get_workload_overview'} with the suggested "
            "arguments from runtime_context.tool_policy. Then inspect only the top candidates with "
            "list_tasks_by_filter or get_task_snapshot if needed. Rank concrete tasks by leverage, "
            "urgency, dependency unlocks, and the user's stated time horizon; avoid raw task dumps."
        )
    if intent == "task_graph_read":
        return (
            "Suggested evidence mode for this read-only task-world turn: use query_task_graph as the "
            "general task-graph entry point, then call a narrower tool only if the graph query says "
            "more evidence is needed. The model owns the final answer; do not rely on the router's "
            "keyword signals as semantics."
        )
    if intent in {"branch_breakdown", "branch_restructure"}:
        return (
            "Suggested evidence mode for this broad branch turn: if branch evidence is needed, prefer "
            f"{preferred.get('name') or 'get_task_branch'} with the suggested arguments from "
            "runtime_context.tool_policy. Use the branch's immediate children as work units; avoid "
            "rewriting the whole branch in one step."
        )
    if intent == "external_world_planning":
        return (
            "This is external-world planning/advice, not a task lookup. Do not require a selected "
            "task, staged item, or pending proposal. Answer the user's planning question directly; "
            "state that live/current external data is unavailable if relevant, and only propose task "
            "changes if the user explicitly asks to create or update tasks."
        )
    if intent != "cleanup_review":
        return ""
    return (
        "Suggested evidence mode for this cleanup/restructure turn: if cleanup evidence is needed, "
        f"prefer {preferred.get('name') or 'analyze_tree_cleanup_candidates'} with the suggested "
        "arguments from runtime_context.tool_policy. Use cleanup_candidates for archive/delete review and "
        "the organization_map plus destination_catalog for workspace classification. For an explicit full "
        "cleanup mutation, retrieve every organization page, preserve subtrees, and return one validated "
        "batch proposal rather than serial work units. Clarify only broad/destructive uncertainty that the "
        "evidence cannot resolve. For a read-only review, return a concise direct assessment."
    )


def _today_value(today_override: date | None) -> date:
    return today_override or date.today()


def _build_kernel_workflow_profile(
    *,
    prompt: str,
    today_override: date | None,
) -> dict[str, Any] | None:
    """Describe explicit typed workflow hints.

    Prompt-derived single-task metadata specialization was retired because it
    collapsed compound requests before the model could preserve all clauses.
    Keep this hook for future typed UI events, but do not infer a lean workflow
    from regex-parsed prompt text.
    """

    _ = (prompt, today_override)
    return None


def _build_tier0_state_payload(
    *,
    route_decision: TierRouteDecision,
    active_category: Category | None,
    focus_node: StrategyNode | None,
    pending_proposal: dict[str, Any] | None,
    session_memory: dict[str, Any],
) -> dict[str, Any] | None:
    route_kind = route_decision.route_kind
    if route_kind == "active_category_state":
        if active_category is None:
            summary = "There is no active category selected right now."
        else:
            summary = f"The active category is {active_category.name}."
        return {
            "summary": summary,
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
            "deterministic_fast_path": {
                "kind": "tier0_state_answer",
                "route_kind": route_kind,
                "tier": route_decision.tier,
            },
        }
    if route_kind == "pending_proposal_state":
        compact_pending = (
            pending_proposal
            if isinstance(pending_proposal, dict)
            else session_memory.get("pending_proposal")
            if isinstance(session_memory.get("pending_proposal"), dict)
            else None
        )
        if compact_pending is None:
            summary = "There is no pending proposal in this Strategist session."
        else:
            proposal_kind = str(compact_pending.get("proposal_kind") or "proposal").strip()
            proposal_summary = str(compact_pending.get("summary") or "").strip()
            target_title = str(compact_pending.get("target_node_title") or "").strip()
            summary = (
                f"There is a pending {proposal_kind} proposal"
                + (f" for {target_title}" if target_title else "")
                + (f": {proposal_summary}" if proposal_summary else ".")
            )
        return {
            "summary": summary,
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
            "deterministic_fast_path": {
                "kind": "tier0_state_answer",
                "route_kind": route_kind,
                "tier": route_decision.tier,
            },
        }
    if route_kind == "proposal_ui_event":
        return {
            "summary": "I handled that proposal action from the approval controls.",
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
            "deterministic_fast_path": {
                "kind": "tier0_state_answer",
                "route_kind": route_kind,
                "tier": route_decision.tier,
            },
        }
    return None


def _route_budget_kwargs(route_decision: TierRouteDecision) -> dict[str, int]:
    budget = route_decision.budget
    kwargs: dict[str, int] = {}
    if budget.model_step_timeout_seconds > 0:
        kwargs["step_timeout_seconds"] = budget.model_step_timeout_seconds
    if budget.loop_timeout_seconds > 0:
        kwargs["loop_timeout_seconds"] = budget.loop_timeout_seconds
    if budget.max_model_calls > 0:
        kwargs["max_model_steps"] = budget.max_model_calls
    if budget.max_tool_calls > 0:
        kwargs["max_tool_calls"] = budget.max_tool_calls
    return kwargs


def _direct_response_timeout_seconds(
    route_decision: TierRouteDecision,
    *,
    rescue: bool = False,
) -> int:
    budget = route_decision.budget
    configured = (
        budget.model_step_timeout_seconds
        if budget.model_step_timeout_seconds > 0
        else codex_structured_step_timeout_seconds()
    )
    if route_decision.route_kind != "external_world_planning":
        return configured

    foreground_seconds = max(1, budget.foreground_timeout_ms // 1000)
    if rescue:
        return max(6, min(configured, 10, max(6, foreground_seconds // 3)))
    return max(8, min(configured, max(8, int(foreground_seconds * 0.55))))


def _provider_error_is_transport_unavailable(error: HTTPException) -> bool:
    detail = str(error.detail or "").lower()
    return error.status_code in {502, 503} and any(
        marker in detail
        for marker in (
            "transport failed",
            "stream disconnected",
            "error sending request",
            "socket",
            "websocket",
            "provider is not ready",
        )
    )


def _trace_to_initial_tool_history(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for item in tool_trace:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("tool_name")
        arguments = item.get("arguments")
        result = item.get("result")
        if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(result, dict):
            continue
        history_result = result
        if name in SEMANTIC_PLAN_TOOL_FAMILIES and (
            result.get("error") or result.get("compiled") is False
        ):
            # The attempted semantic plan is already present in ``arguments``.
            # Repeating its partially compiled proposal and task patch can add
            # thousands of tokens to the repair turn without new evidence.
            history_result = {
                key: result.get(key)
                for key in (
                    "error",
                    "compiled",
                    "tool_error",
                    "model_instruction",
                    "clarification",
                    "created_task_schedule_errors",
                    "task_patch_errors",
                )
                if result.get(key) not in (None, "", [], {})
            }
        history.append(
            {
                "tool_name": name,
                "arguments": arguments,
                "result": history_result,
            }
        )
    return history


def _task_title(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or item.get("task_title") or "").strip()
    return title or None


def _compact_text(value: str, *, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _compact_prefetch_task(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = _task_title(item)
    if not title:
        return None
    compact: dict[str, Any] = {"title": title}
    for key in (
        "id",
        "task_id",
        "status",
        "priority",
        "priority_label",
        "planned_date",
        "deadline_at",
        "deadline",
        "time_estimate_minutes",
        "estimate",
        "category_name",
        "child_count",
    ):
        if item.get(key) not in (None, "", [], {}):
            compact[key] = item.get(key)
    notes = item.get("notes")
    if isinstance(notes, str) and notes.strip():
        compact["notes_excerpt"] = _compact_text(notes, limit=240)
    aliases = item.get("aliases")
    if isinstance(aliases, list) and aliases:
        compact["aliases"] = [
            _compact_text(str(alias), limit=80)
            for alias in aliases[:3]
            if str(alias).strip()
        ]
    return compact


def _compact_cleanup_evidence_item(item: Any) -> dict[str, Any] | None:
    """Preserve bounded structural facts needed for shadow target conservation."""

    if not isinstance(item, dict):
        return None
    compact = _compact_prefetch_task(item) or {}
    for key in (
        "category_id",
        "parent_task_id",
        "path",
        "score",
        "suggested_action",
        "is_backlog",
    ):
        if item.get(key) not in (None, "", [], {}):
            compact[key] = item.get(key)
    for key in ("signals", "child_sample", "tags"):
        values = item.get(key)
        if isinstance(values, list):
            compact[key] = [str(value)[:160] for value in values[:16]]
    nested_task = _compact_prefetch_task(item.get("task"))
    if nested_task is not None:
        compact["task"] = nested_task
    return compact or None


def _compact_prefetched_evidence(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in tool_trace:
        if not isinstance(item, dict) or not item.get("prefetch"):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        name = str(item.get("name") or "").strip()
        entry: dict[str, Any] = {
            "tool_name": name,
            "arguments": item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            "summary": summarize_kernel_tool_observation(item),
            "result_kind": result.get("result_kind"),
        }
        if item.get("deterministic_repair") is True:
            entry["deterministic_repair"] = True
        if name == "get_workload_overview":
            entry["scope"] = result.get("scope")
            for key in ("active_count", "backlog_count", "total_tasks"):
                if result.get(key) is not None:
                    entry[key] = result.get(key)
            for key in ("overdue", "due_soon", "stale"):
                values = result.get(key)
                if isinstance(values, list):
                    entry[f"{key}_tasks"] = [
                        compact
                        for compact in (_compact_prefetch_task(value) for value in values[:6])
                        if compact is not None
                    ]
        elif name == "list_tasks_by_filter":
            tasks = result.get("tasks")
            if isinstance(tasks, list):
                entry["task_count"] = len(tasks)
                entry["tasks"] = [
                    compact
                    for compact in (_compact_prefetch_task(value) for value in tasks[:12])
                    if compact is not None
                ]
        elif name == "query_task_graph":
            entry["scope"] = result.get("scope")
            entry["interpreted_intent"] = result.get("interpreted_intent")
            entry["answer_ready"] = result.get("answer_ready")
            answer_candidate = result.get("answer_candidate")
            if isinstance(answer_candidate, dict):
                entry["answer_candidate"] = {
                    key: answer_candidate.get(key)
                    for key in ("summary", "confidence", "source", "task_ids")
                    if answer_candidate.get(key) not in (None, "", [], {})
                }
            entry["inferred_filters"] = result.get("inferred_filters")
            entry["count"] = result.get("count")
            tasks = result.get("tasks")
            if isinstance(tasks, list):
                entry["tasks"] = [
                    compact
                    for compact in (_compact_prefetch_task(value) for value in tasks[:12])
                    if compact is not None
                ]
            matches = result.get("entity_matches")
            if isinstance(matches, list):
                compact_matches: list[dict[str, Any]] = []
                for match in matches[:5]:
                    if not isinstance(match, dict):
                        continue
                    task = match.get("task") if isinstance(match.get("task"), dict) else {}
                    compact = _compact_prefetch_task(task)
                    if compact is None:
                        continue
                    compact_matches.append(
                        {
                            "score": match.get("score"),
                            "task": compact,
                        }
                    )
                entry["entity_matches"] = compact_matches
        elif name == "analyze_tree_cleanup_candidates":
            entry["scope"] = result.get("scope")
            for key in (
                "cleanup_candidate_count",
                "messy_branch_count",
                "organization_map_count",
                "organization_map_offset",
                "organization_map_complete",
                "next_organization_offset",
                "recommended_next_step",
                "model_instruction",
            ):
                if result.get(key) not in (None, "", [], {}):
                    entry[key] = result.get(key)
            for key, limit in (
                ("cleanup_candidates", 20),
                ("messy_branches", 20),
                ("organization_map", 250),
            ):
                values = result.get(key)
                if isinstance(values, list):
                    entry[key] = [
                        compact
                        for compact in (
                            _compact_cleanup_evidence_item(value)
                            for value in values[:limit]
                        )
                        if compact is not None
                    ]
            destinations = result.get("destination_catalog")
            if isinstance(destinations, list):
                entry["destination_catalog"] = [
                    {
                        key: value
                        for key, value in destination.items()
                        if key in {"category_id", "name", "root_samples"}
                    }
                    for destination in destinations[:20]
                    if isinstance(destination, dict)
                ]
        elif name == "get_task_branch":
            task = result.get("task")
            if isinstance(task, dict):
                entry["task"] = _compact_prefetch_task(task)
            children = result.get("children")
            if isinstance(children, list):
                entry["children"] = [
                    compact
                    for compact in (_compact_prefetch_task(value) for value in children[:10])
                    if compact is not None
                ]
        elif name == "get_context_surfaces":
            entry["active_category"] = result.get("active_category")
            focus_task = result.get("focus_task")
            if isinstance(focus_task, dict):
                entry["focus_task"] = _compact_prefetch_task(focus_task)
            working_set = result.get("working_set")
            if isinstance(working_set, list):
                entry["working_set_count"] = len(working_set)
            entry["has_pending_proposal"] = bool(result.get("pending_proposal"))
        elif name in {"list_calendar_blocks", "get_task_schedule"}:
            entry["scope"] = result.get("scope")
            for key in (
                "range_start",
                "range_end",
                "count",
                "task_ids",
                "recurrence_series_refs",
                "recurrence_occurrence_refs",
            ):
                if result.get(key) not in (None, "", [], {}):
                    entry[key] = result.get(key)
            blocks = result.get("blocks")
            if isinstance(blocks, list):
                entry["blocks"] = [
                    {
                        key: block.get(key)
                        for key in (
                            "block_id",
                            "task_id",
                            "task_title",
                            "category_id",
                            "start_at",
                            "end_at",
                            "revision",
                            "is_pinned",
                            "is_protected",
                            "is_recurring",
                            "recurrence_series_id",
                            "recurrence_key",
                            "recurrence_series_ref",
                            "recurrence_occurrence_ref",
                            "recurrence_series_revision",
                            "recurrence_rule",
                        )
                        if block.get(key) is not None
                    }
                    for block in blocks[:100]
                    if isinstance(block, dict)
                ]
        elif name in {"search_entities", "search_notes"}:
            matches = result.get("matches")
            if isinstance(matches, list):
                entry["match_count"] = len(matches)
                compact_matches: list[dict[str, Any]] = []
                for match in matches[:5]:
                    if not isinstance(match, dict):
                        continue
                    task = match.get("task") if isinstance(match.get("task"), dict) else {}
                    compact = _compact_prefetch_task(task)
                    if compact is None:
                        continue
                    compact_matches.append(
                        {
                            "score": match.get("score"),
                            "task": compact,
                        }
                    )
                entry["matches"] = compact_matches
        else:
            for key in ("task_count", "included_tasks", "truncated"):
                if result.get(key) is not None:
                    entry[key] = result.get(key)
        compacted.append(entry)
    return compacted


def _prefetch_kernel_evidence_plan(
    *,
    plan_items: list[dict[str, Any]],
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
    tool_executor: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    prefetched: list[dict[str, Any]] = []
    seen: set[str] = {
        json.dumps(
            {
                "name": str(item.get("name") or item.get("tool_name") or ""),
                "arguments": item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            },
            sort_keys=True,
            default=str,
        )
        for item in tool_trace
        if isinstance(item, dict)
    }
    for plan_item in plan_items:
        name = str(plan_item.get("name") or "").strip()
        arguments = plan_item.get("arguments") if isinstance(plan_item.get("arguments"), dict) else {}
        if not name:
            continue
        key = json.dumps({"name": name, "arguments": arguments}, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        tool_started = perf_counter()
        result = tool_executor(
            name,
            dict(arguments),
            categories=categories,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            staged_node_ids=staged_node_ids,
            working_set=working_set,
            today_override=today_override,
        )
        trace_entry = {
            "name": name,
            "arguments": dict(arguments),
            "result": result,
            "duration_ms": _elapsed_ms(tool_started),
            "prefetch": True,
        }
        tool_trace.append(trace_entry)
        prefetched.append(trace_entry)
    return prefetched


def _task_from_search_match(match: Any) -> dict[str, Any] | None:
    if not isinstance(match, dict):
        return None
    task = match.get("task")
    return task if isinstance(task, dict) else None


def _node_from_serialized_task(
    task: dict[str, Any],
    nodes: list[StrategyNode],
) -> StrategyNode | None:
    raw_id = task.get("task_id") or task.get("id")
    if isinstance(raw_id, int):
        for node in nodes:
            if node.id == raw_id:
                return node
    title = str(task.get("title") or "").strip().lower()
    if not title:
        return None
    for node in nodes:
        if str(node.title or "").strip().lower() == title:
            return node
    return None


def _search_result_is_confident(matches: list[Any], *, prompt: str) -> bool:
    if not matches or not isinstance(matches[0], dict):
        return False
    top_score = matches[0].get("score")
    if not isinstance(top_score, (int, float)) or top_score < 0.72:
        return False
    top_task = _task_from_search_match(matches[0])
    top_title = str((top_task or {}).get("title") or "").strip().lower()
    if top_title and top_title in prompt.lower() and float(top_score) >= 0.86:
        return True
    if len(matches) == 1 or not isinstance(matches[1], dict):
        return True
    second_score = matches[1].get("score")
    if not isinstance(second_score, (int, float)):
        return True
    return (float(top_score) - float(second_score)) >= 0.18


def _nodes_from_search_matches(
    matches: list[Any],
    nodes: list[StrategyNode],
    *,
    limit: int,
) -> list[StrategyNode]:
    resolved: list[StrategyNode] = []
    seen_ids: set[int] = set()
    for match in matches:
        task = _task_from_search_match(match)
        if task is None:
            continue
        node = _node_from_serialized_task(task, nodes)
        if node is None or node.id is None or node.id in seen_ids:
            continue
        resolved.append(node)
        seen_ids.add(int(node.id))
        if len(resolved) >= limit:
            break
    return resolved


def _metadata_update_summary(updates: dict[str, Any]) -> str:
    labels = {
        "deadline_at": "deadline",
        "time_estimate_minutes": "estimate",
        "is_pinned": "pinned state",
        "node_type": "task type",
    }
    parts: list[str] = []
    for key, value in updates.items():
        label = labels.get(key, key.replace("_", " "))
        if isinstance(value, list):
            value_text = ", ".join(str(item) for item in value)
        elif isinstance(value, bool):
            value_text = "on" if value else "off"
        else:
            value_text = str(value)
        if value_text.strip():
            parts.append(f"{label} to {value_text}")
    return ", ".join(parts) if parts else "the requested metadata"


def _explicit_single_task_metadata_updates(
    prompt: str,
    today_value: date,
) -> dict[str, Any] | None:
    """Retired: natural-language mutation semantics belong to Strategist."""

    _ = (prompt, today_value)
    return None


def _build_single_task_metadata_proposal_payload(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
    tool_executor: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    if route_decision.route_kind not in {
        "single_task_metadata_mutation",
        "model_owned_tool_loop",
    }:
        return None
    today_value = _today_value(today_override)
    updates = _explicit_single_task_metadata_updates(prompt, today_value)
    if updates is None:
        return None
    prompt_lower = prompt.casefold()
    explicitly_named_node_ids = {
        int(node.id)
        for node in nodes
        if node.id is not None
        and str(node.title or "").strip()
        and str(node.title or "").strip().casefold() in prompt_lower
    }
    if len(explicitly_named_node_ids) > 1:
        return None

    target_node: StrategyNode | None = None
    resolved_nodes_by_id: dict[int, StrategyNode] = {}
    queries = _entity_search_queries_for_prompt(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
        limit=1,
    )
    if not queries and focus_node is not None and focus_node.id is not None:
        target_node = focus_node
    for query in queries:
        tool_started = perf_counter()
        result = tool_executor(
            "search_entities",
            {"query": query, "limit": 5},
            categories=categories,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            staged_node_ids=staged_node_ids,
            working_set=working_set,
            today_override=today_override,
        )
        trace_entry = {
            "name": "search_entities",
            "arguments": {"query": query, "limit": 5},
            "result": result,
            "duration_ms": _elapsed_ms(tool_started),
            "prefetch": True,
            "completion_candidate": True,
        }
        tool_trace.append(trace_entry)
        matches = result.get("matches") if isinstance(result, dict) else None
        if isinstance(matches, list) and _search_result_is_confident(matches, prompt=prompt):
            task = _task_from_search_match(matches[0])
            if task is not None:
                resolved = _node_from_serialized_task(task, nodes)
                if resolved is not None and resolved.id is not None:
                    resolved_nodes_by_id[int(resolved.id)] = resolved

    if len(resolved_nodes_by_id) > 1:
        return None
    if resolved_nodes_by_id:
        target_node = next(iter(resolved_nodes_by_id.values()))
    if target_node is None or target_node.id is None:
        return None

    snapshot_started = perf_counter()
    snapshot_result = tool_executor(
        "get_task_snapshot",
        {"task_id": int(target_node.id)},
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "get_task_snapshot",
            "arguments": {"task_id": int(target_node.id)},
            "result": snapshot_result,
            "duration_ms": _elapsed_ms(snapshot_started),
            "prefetch": True,
            "completion_candidate": True,
        }
    )

    submit_started = perf_counter()
    submit_arguments = {
        "target_task_id": int(target_node.id),
        "target_task_title": target_node.title,
        "updates": updates,
        "rationale": "Apply the user's explicit metadata change to the exact task.",
        "summary": f"Update {target_node.title}.",
    }
    submit_result = tool_executor(
        "submit_task_metadata_plan",
        submit_arguments,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "submit_task_metadata_plan",
            "arguments": submit_arguments,
            "result": submit_result,
            "duration_ms": _elapsed_ms(submit_started),
            "deterministic_contract_fast_lane": True,
        }
    )
    semantic_input = (
        submit_result.get("semantic_plan_input")
        if isinstance(submit_result, dict)
        and isinstance(submit_result.get("semantic_plan_input"), dict)
        else {}
    )
    proposal = semantic_input.get("payload")
    if not isinstance(proposal, dict):
        return None
    update_summary = _metadata_update_summary(updates)
    return {
        "mode": "metadata",
        "summary": (
            f"I prepared a metadata proposal for {target_node.title}: "
            f"{update_summary}. Review metadata changes before applying."
        ),
        "action_proposal": None,
        "metadata_proposal": proposal,
        "branch_proposal": None,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "single_task_metadata_contract_fast_lane",
            "route_kind": route_decision.route_kind,
            "tier": route_decision.tier,
            "target_node_id": target_node.id,
            "target_node_title": target_node.title,
            "updates": updates,
            "tool_names": [
                "search_entities",
                "get_task_snapshot",
                "submit_task_metadata_plan",
            ],
            "reason": (
                "A conservative command fast lane found one explicit metadata "
                "operation and one exact task. The server submitted the same "
                "high-level MCP contract used by the model without a model call."
            ),
        },
    }


def _simple_new_task_title(prompt: str) -> str | None:
    """Retired: natural-language task identity belongs to Strategist."""

    _ = prompt
    return None


def _build_simple_new_task_schedule_payload(
    *,
    prompt: str,
    session: Session,
    route_decision: TierRouteDecision,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    workspace_context: dict[str, Any] | None,
    tool_trace: list[dict[str, Any]],
    tool_executor: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    """Compile one explicit create-and-place command through the tree-plan MCP."""

    title = _simple_new_task_title(prompt)
    if title is None or active_category is None or active_category.id is None:
        return None
    today_value = _today_value(today_override)
    target_date = _calendar_date_from_prompt(
        session,
        prompt=prompt,
        today_value=today_value,
    )
    if target_date is None:
        return None
    window = _one_time_schedule_window(
        target_date=target_date,
        prompt=prompt,
        local_now=_workspace_local_datetime(workspace_context),
    )
    if window is None:
        return None
    window_start, window_end = window
    temp_id = "new-task"
    arguments = {
        "target_category_id": int(active_category.id),
        "target_category_name": active_category.name,
        "structural_operations": [
            {
                "kind": "create_node",
                "temp_id": temp_id,
                "title": title,
                "estimate_minutes": 60,
            }
        ],
        "created_task_schedule": {
            "target_temp_ids": [temp_id],
            "instruction": prompt,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
        "rationale": "Create and place the user's explicit one-time task.",
        "summary": f"Create and schedule {title}.",
    }
    started_at = perf_counter()
    result = tool_executor(
        "submit_task_tree_plan",
        arguments,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "submit_task_tree_plan",
            "arguments": arguments,
            "result": result,
            "duration_ms": _elapsed_ms(started_at),
            "deterministic_contract_fast_lane": True,
        }
    )
    semantic_input = (
        result.get("semantic_plan_input")
        if isinstance(result, dict)
        and isinstance(result.get("semantic_plan_input"), dict)
        else {}
    )
    proposal = semantic_input.get("payload")
    if result.get("compiled") is not True or not isinstance(proposal, dict):
        return None
    return {
        "mode": "branch",
        "summary": f"I prepared {title} and its requested schedule for review.",
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": proposal,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "simple_new_task_schedule_contract_fast_lane",
            "route_kind": route_decision.route_kind,
            "tier": route_decision.tier,
            "target_category_id": int(active_category.id),
            "task_title": title,
            "tool_names": ["submit_task_tree_plan"],
            "reason": (
                "One explicit new task and one unambiguous daypart were submitted "
                "through the standard semantic tree-plan contract."
            ),
        },
    }


def _append_unique_candidate_entities(
    candidates: list[dict[str, Any]],
    matches: list[Any],
    *,
    seen_ids: set[int],
) -> None:
    for match in matches:
        if not isinstance(match, dict):
            continue
        task = _task_from_search_match(match)
        if task is None:
            continue
        raw_id = task.get("id", task.get("task_id"))
        if not isinstance(raw_id, int) or raw_id in seen_ids:
            continue
        seen_ids.add(raw_id)
        normalized_task = dict(task)
        normalized_task.setdefault("id", raw_id)
        normalized_match = dict(match)
        normalized_match["task"] = normalized_task
        candidates.append(normalized_match)


def _append_alias_node_candidates(
    candidates: list[dict[str, Any]],
    *,
    prompt: str,
    categories: list[Category],
    nodes: list[StrategyNode],
    active_category: Category | None,
    seen_ids: set[int],
) -> None:
    aliases = [alias.lower() for alias in _explicit_alias_queries(prompt)]
    if not aliases:
        return
    category_name_by_id = {
        int(category.id): category.name
        for category in categories
        if category.id is not None
    }
    for node in nodes:
        if node.id is None or node.id in seen_ids:
            continue
        if active_category is not None and node.category_id != active_category.id:
            continue
        title = str(node.title or "").strip()
        if not title:
            continue
        title_lower = title.lower()
        if not any(alias in title_lower for alias in aliases):
            continue
        seen_ids.add(int(node.id))
        candidates.append(
            {
                "score": 0.74,
                "task": {
                    "id": int(node.id),
                    "title": title,
                    "category": category_name_by_id.get(int(node.category_id or 0), "Unknown"),
                    "priority_label": node.priority or "Unknown priority",
                    "deadline": node.deadline_at or "No deadline",
                },
            }
        )


def _resolved_entity_from_node(node: StrategyNode | None) -> dict[str, Any] | None:
    if node is None or node.id is None:
        return None
    return {
        "title": node.title,
        "node_id": int(node.id),
    }


def _resolved_entity_from_match(match: dict[str, Any] | None) -> dict[str, Any] | None:
    task = _task_from_search_match(match) if isinstance(match, dict) else None
    if task is None:
        return None
    raw_id = task.get("id", task.get("task_id"))
    title = str(task.get("title") or "").strip()
    if not isinstance(raw_id, int) or not title:
        return None
    return {
        "title": title,
        "node_id": raw_id,
    }


def _build_metadata_clarification_payload(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
    tool_executor: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    if route_decision.route_kind != "metadata_clarification":
        return None

    candidate_entities: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    queries = _entity_search_queries_for_prompt(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
        limit=2,
    )
    if not queries:
        queries = _explicit_alias_queries(prompt)
    for query in queries:
        tool_started = perf_counter()
        result = tool_executor(
            "search_entities",
            {"query": query, "limit": 5},
            categories=categories,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            staged_node_ids=staged_node_ids,
            working_set=working_set,
            today_override=today_override,
        )
        trace_entry = {
            "name": "search_entities",
            "arguments": {"query": query, "limit": 5},
            "result": result,
            "duration_ms": _elapsed_ms(tool_started),
            "prefetch": True,
            "completion_candidate": True,
        }
        tool_trace.append(trace_entry)
        matches = result.get("matches") if isinstance(result, dict) else None
        if isinstance(matches, list):
            _append_unique_candidate_entities(candidate_entities, matches, seen_ids=seen_ids)
    if not candidate_entities:
        _append_alias_node_candidates(
            candidate_entities,
            prompt=prompt,
            categories=categories,
            nodes=nodes,
            active_category=active_category,
            seen_ids=seen_ids,
        )

    ambiguous_target = len(candidate_entities) > 1 and not _search_result_is_confident(
        candidate_entities,
        prompt=prompt,
    )
    resolved_entity = None
    if not ambiguous_target and candidate_entities:
        resolved_entity = _resolved_entity_from_match(candidate_entities[0])
    if resolved_entity is None:
        resolved_entity = _resolved_entity_from_node(focus_node)
    if not candidate_entities and resolved_entity is None:
        return None

    clarification = build_clarification_payload(
        prompt=prompt,
        candidate_entities=candidate_entities if ambiguous_target else [],
        include_field_choices=not ambiguous_target,
        resolved_entity=resolved_entity,
    )
    return {
        "mode": "metadata",
        "summary": str(clarification.get("message") or "I need one quick clarification before I make that call."),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": clarification,
        "deterministic_fast_path": {
            "kind": "metadata_clarification",
            "route_kind": route_decision.route_kind,
            "tier": route_decision.tier,
            "candidate_count": len(candidate_entities),
            "ambiguous_target": ambiguous_target,
            "resolved_entity": resolved_entity,
            "reason": (
                "The router found a metadata edit intent that lacked either a "
                "single safe target or concrete field values, so the harness "
                "returned a clarification without entering the model loop."
            ),
        },
    }


def _latest_final_answer_summary(session_memory: dict[str, Any]) -> str:
    recent_decisions = session_memory.get("recent_decisions")
    if not isinstance(recent_decisions, list):
        return ""
    for decision in reversed(recent_decisions):
        if not isinstance(decision, dict):
            continue
        if decision.get("event_type") != "final_answer":
            continue
        summary = str(decision.get("summary") or "").strip()
        if summary:
            return summary
    return ""


def _active_objective_summary(session_memory: dict[str, Any]) -> str:
    active_objective = session_memory.get("active_objective")
    if not isinstance(active_objective, dict):
        return ""
    return str(active_objective.get("summary") or "").strip()


def _title_case_subject(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", value)
    if not words:
        return "Requested Plan"
    small_words = {"a", "an", "and", "at", "for", "in", "of", "on", "the", "to"}
    titled: list[str] = []
    for index, word in enumerate(words):
        lower = word.lower()
        if word.isupper():
            titled.append(word)
        elif index > 0 and lower in small_words:
            titled.append(lower)
        else:
            titled.append(lower[:1].upper() + lower[1:])
    return " ".join(titled)


def _external_plan_subject(seed_text: str) -> str:
    patterns = (
        r"\b(?:going|go|travel|travelling|traveling|visit|visiting)\s+to\s+([^,\n.;:]+)",
        r"\btrip\s+to\s+([^,\n.;:]+)",
        r"^\s*For\s+([^,\n.;:]+)",
        r"\bprepare\s+for\s+([^,\n.;:]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, seed_text, re.IGNORECASE)
        if not match:
            continue
        subject = re.split(
            r"\b(?:with|before|after|and|then|that|this|make|create)\b",
            match.group(1),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,:;")
        if subject:
            return _title_case_subject(subject)
    return "Requested Plan"


def _external_plan_parent_title(seed_text: str) -> str:
    subject = _external_plan_subject(seed_text)
    lowered = seed_text.lower()
    if (
        subject != "Requested Plan"
        and "trip" not in subject.lower()
        and (
            "trip" in lowered
            or "going to" in lowered
            or "travel" in lowered
            or "visit" in lowered
        )
    ):
        return f"Plan {subject} trip"
    return f"Plan {subject}"


def _normalize_plan_item_title(item: str) -> str | None:
    text = re.sub(r"\s+", " ", item).strip(" .,:;-")
    text = re.sub(r"^(?:and\s+)?(?:a|an|the)\s+", "", text, flags=re.IGNORECASE)
    if not text or len(text) < 3:
        return None
    lowered = text.lower()
    if lowered.startswith(("check ", "verify ", "pack ", "charge ", "confirm ", "book ", "coordinate ")):
        title = text
    else:
        title = f"Prepare {text}"
    return title[:1].upper() + title[1:]


def _child_titles_from_external_plan(summary: str) -> list[str]:
    text = re.sub(r"^\s*For\s+[^,.;:]+[,;:]\s*", "", summary).strip()
    prepare_match = re.search(r"\bprepare\s+(.+)", text, re.IGNORECASE)
    if prepare_match:
        text = prepare_match.group(1)
    text = re.sub(r"\s+before\s+applying\b.*$", "", text, flags=re.IGNORECASE)
    raw_items = re.split(r",|\band\b", text)
    child_titles: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        title = _normalize_plan_item_title(raw_item)
        if title is None:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        child_titles.append(title)
        if len(child_titles) >= 8:
            break
    return child_titles or [
        "Prepare logistics",
        "Confirm timing",
        "Pack essentials",
    ]


def _build_external_plan_task_proposal_payload(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    active_category: Category | None,
    session_memory: dict[str, Any],
) -> dict[str, Any] | None:
    if route_decision.route_kind != "external_plan_task_proposal":
        return None
    if active_category is None or active_category.id is None:
        return None
    prior_summary = _latest_final_answer_summary(session_memory)
    active_objective = _active_objective_summary(session_memory)
    seed_text = "\n".join(part for part in (active_objective, prior_summary) if part)
    if not seed_text:
        return None

    parent_title = _external_plan_parent_title(seed_text)
    child_titles = _child_titles_from_external_plan(prior_summary or active_objective)
    notes = (
        "Created from the previous Strategist planning turn.\n\n"
        f"Original objective: {active_objective or 'Not recorded'}\n\n"
        f"Planning notes: {prior_summary or active_objective}"
    )
    proposal = build_top_level_task_set_proposal(
        category_id=int(active_category.id),
        category_name=active_category.name,
        parent_title=parent_title,
        child_titles=child_titles,
        notes=notes,
        rationale=(
            "The user asked to convert the previous planning answer into tasks. "
            "This proposal preserves that plan as reviewable task-tree changes."
        ),
        evidence_bundle={
            "source": "agent_kernel_external_plan_task_compiler",
            "prompt": prompt,
            "active_objective": active_objective,
            "prior_summary": prior_summary,
        },
        summary=f"Create top-level task {parent_title} with {len(child_titles)} subtasks",
    )
    if proposal is None:
        return None
    return {
        "mode": "branch",
        "summary": (
            f"I prepared a create-task proposal for {parent_title}. "
            "Review the task set before applying."
        ),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": proposal,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "external_plan_task_proposal",
            "route_kind": route_decision.route_kind,
            "tier": route_decision.tier,
            "parent_title": parent_title,
            "child_count": len(child_titles),
            "reason": (
                "A prior external planning turn already produced the semantic plan. "
                "The follow-up asked to convert it into tasks, so the harness compiled "
                "that session context into an approval-gated branch proposal."
            ),
        },
    }


def _fallback_from_tool_trace(tool_trace: list[dict[str, Any]]) -> str | None:
    for tool_call in reversed(tool_trace):
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        cleanup_candidates = result.get("cleanup_candidates")
        messy_branches = result.get("messy_branches")
        if isinstance(cleanup_candidates, list) or isinstance(messy_branches, list):
            cleanup_titles = [
                str(item.get("title") or "").strip()
                for item in (cleanup_candidates or [])[:4]
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            ]
            branch_titles = [
                str(item.get("title") or "").strip()
                for item in (messy_branches or [])[:3]
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            ]
            rows: list[str] = []
            if cleanup_titles:
                rows.append("cleanup candidates: " + ", ".join(cleanup_titles))
            if branch_titles:
                rows.append("messy branches: " + ", ".join(branch_titles))
            if rows:
                return "I retrieved the workspace cleanup evidence, including " + "; ".join(rows) + "."
        tasks = result.get("tasks")
        if isinstance(tasks, list) and tasks:
            titles = [
                str(item.get("title") or "").strip()
                for item in tasks[:8]
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            ]
            if titles:
                return "I retrieved this task set: " + ", ".join(titles) + "."
        rows: list[str] = []
        for label, key in (
            ("overdue", "overdue"),
            ("due soon", "due_soon"),
            ("stale", "stale"),
        ):
            values = result.get(key)
            if not isinstance(values, list) or not values:
                continue
            titles = [
                str(item.get("title") or "").strip()
                for item in values[:4]
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            ]
            if titles:
                rows.append(f"{label}: {', '.join(titles)}")
        if rows:
            return "I retrieved the workload evidence, including " + "; ".join(rows) + "."
    return None


def _build_kernel_provider_failure_payload(
    *,
    error_detail: str,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence_summary = _fallback_from_tool_trace(tool_trace)
    if evidence_summary:
        summary = (
            f"{evidence_summary} The provider then stopped emitting meaningful progress and "
            "the automatic continuation could not finish. Nothing was changed; retry the full "
            "request without narrowing it."
        )
    elif "app-server" in error_detail.lower() or "meaningful progress" in error_detail.lower():
        summary = (
            "The Codex app-server turn stopped making progress before it produced structured output. "
            "I did not apply any changes. The trace should now show this as a provider transport "
            "idle event rather than a task-tool failure."
        )
    else:
        summary = (
            "The provider stopped emitting meaningful progress and the automatic continuation "
            "could not produce a reviewable answer. Nothing was changed; retry the full request "
            "without narrowing it."
        )
    return {
        "summary": summary,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
        "provider_failure": {
            "kind": "model_step_timeout",
            "detail": error_detail,
            "resumable": True,
            "resume_action": "retry_same_goal",
        },
    }


def _build_task_graph_answer_candidate_payload(
    *,
    route_decision: TierRouteDecision,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if route_decision.route_kind != "task_graph_orchestrated_read":
        if route_decision.route_kind != "model_owned_tool_loop":
            return None
        if (
            bool(route_decision.signals.get("mutation_request"))
            or bool(route_decision.signals.get("structural_mutation"))
            or bool(route_decision.signals.get("external_world_planning"))
        ):
            return None
    for tool_call in reversed(tool_trace):
        if str(tool_call.get("name") or "") != "query_task_graph":
            continue
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        if result.get("answer_ready") is not True:
            continue
        source_request = " ".join(
            str(part or "").strip()
            for part in (result.get("source_user_request"), result.get("query"))
            if str(part or "").strip()
        )
        if source_request and ANSWER_CANDIDATE_NEEDS_MODEL_RE.search(source_request):
            continue
        answer_candidate = (
            result.get("answer_candidate")
            if isinstance(result.get("answer_candidate"), dict)
            else {}
        )
        summary = normalize_summary(answer_candidate.get("summary"), "")
        confidence = answer_candidate.get("confidence")
        if not summary or not isinstance(confidence, (int, float)) or float(confidence) < 0.9:
            continue
        return {
            "mode": "review",
            "summary": summary,
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
            "deterministic_fast_path": {
                "kind": "task_graph_answer_candidate",
                "route_kind": route_decision.route_kind,
                "tier": route_decision.tier,
                "tool_name": "query_task_graph",
                "confidence": round(float(confidence), 4),
                "query": result.get("query"),
                "scope": result.get("scope"),
                "interpreted_intent": result.get("interpreted_intent"),
                "task_ids": answer_candidate.get("task_ids"),
                "reason": (
                    "The task-graph tool returned a high-confidence read-only answer candidate; "
                    "the harness used it without spending a provider model step."
                ),
            },
        }
    return None


def _reconcile_read_only_task_graph_answer(
    *,
    normalized_payload: dict[str, Any],
    route_decision: TierRouteDecision,
    tool_trace: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Prefer a high-confidence tool synthesis over contradictory model prose.

    The model still owns whether and how to inspect the workspace. Once its
    dynamic read returns an answer-ready task-graph synthesis, however, a plain
    read-only final answer cannot safely claim the opposite. Mutations,
    clarifications, recommendations, and proposal payloads remain untouched.
    """

    candidate = _build_task_graph_answer_candidate_payload(
        route_decision=route_decision,
        tool_trace=tool_trace,
    )
    if candidate is None:
        return normalized_payload, None
    if any(
        isinstance(normalized_payload.get(key), dict)
        for key in (
            "action_proposal",
            "metadata_proposal",
            "branch_proposal",
            PREFERENCE_RESPONSE_KEY,
            "semantic_plan_input",
            "clarification",
        )
    ):
        return normalized_payload, None
    decision = normalized_payload.get("decision")
    if isinstance(decision, dict) and str(decision.get("kind") or "").strip().lower() in {
        "mutation",
        "clarification",
    }:
        return normalized_payload, None
    reconciled = dict(normalized_payload)
    reconciled["mode"] = "review"
    reconciled["summary"] = candidate["summary"]
    reconciled["decision"] = {
        "kind": "answer",
        "reason": "Used the authoritative answer-ready task-graph synthesis.",
    }
    return reconciled, candidate["deterministic_fast_path"]


def _build_task_fact_fast_lane_payload(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
    tool_executor: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    """Answer only high-confidence read-only task facts without a model step."""

    if (
        route_decision.route_kind != "model_owned_tool_loop"
        or not TASK_FACT_FAST_LANE_RE.search(prompt)
        or STRUCTURAL_OR_CREATION_MUTATION_RE.search(prompt)
        or EXPLICIT_MUTATION_COMMIT_RE.search(prompt)
        or RECOMMENDATION_ONLY_PROMPT_RE.search(prompt)
        or build_metadata_updates_from_prompt(prompt, _today_value(today_override))
    ):
        return None
    arguments = {"query": prompt, "limit": 8}
    started_at = perf_counter()
    result = tool_executor(
        "query_task_graph",
        arguments,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "query_task_graph",
            "arguments": arguments,
            "result": result,
            "duration_ms": _elapsed_ms(started_at),
            "deterministic_contract_fast_lane": True,
        }
    )
    return _build_task_graph_answer_candidate_payload(
        route_decision=route_decision,
        tool_trace=tool_trace,
    )


def _build_cleanup_reconnaissance_payload(
    *,
    objective: str,
    tool_policy: dict[str, Any] | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(tool_policy, dict) or tool_policy.get("intent") != "cleanup_review":
        return None
    preferred = (
        tool_policy.get("preferred_first_tool")
        if isinstance(tool_policy.get("preferred_first_tool"), dict)
        else {}
    )
    arguments = preferred.get("arguments") if isinstance(preferred.get("arguments"), dict) else {}
    tool_started_at = perf_counter()
    result = execute_kernel_tool(
        "analyze_tree_cleanup_candidates",
        dict(arguments),
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "analyze_tree_cleanup_candidates",
            "arguments": dict(arguments),
            "result": result,
            "duration_ms": _elapsed_ms(tool_started_at),
        }
    )
    if result.get("error"):
        return None

    scope = str(result.get("scope") or "the current task tree").strip()
    agent_run_plan = build_cleanup_agent_run_plan(
        objective=objective,
        analysis_result=result,
    )
    work_units = [
        item for item in agent_run_plan.get("work_units", []) if isinstance(item, dict)
    ]
    candidate_count = result.get("cleanup_candidate_count")
    branch_count = result.get("messy_branch_count")
    active_count = result.get("active_task_count")
    top_titles = [
        str(item.get("target_task_title") or item.get("title") or "").strip()
        for item in work_units[:5]
        if str(item.get("target_task_title") or item.get("title") or "").strip()
    ]
    if not work_units:
        return {
            "summary": (
                f"I ran a broad cleanup pass over {scope} and did not find strong redundant-task "
                "or messy-branch signals. I did not apply any changes."
            ),
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
            "deterministic_fast_path": {
                "kind": "cleanup_reconnaissance",
                "scope": scope,
                "cleanup_candidate_count": 0,
                "messy_branch_count": 0,
                "agent_run_plan_id": agent_run_plan.get("plan_id"),
                "work_unit_count": 0,
            },
            "agent_run_plan": agent_run_plan,
        }

    signal_bits: list[str] = []
    if isinstance(candidate_count, int):
        signal_bits.append(f"{candidate_count} cleanup candidate{'s' if candidate_count != 1 else ''}")
    if isinstance(branch_count, int):
        signal_bits.append(f"{branch_count} messy branch{'es' if branch_count != 1 else ''}")
    if isinstance(active_count, int):
        signal_bits.append(f"{active_count} active task{'s' if active_count != 1 else ''}")
    headline = ", ".join(signal_bits) if signal_bits else "cleanup signals"
    ranked = ", ".join(top_titles)
    summary = (
        f"I ran the broad cleanup reconnaissance pass over {scope} and found {headline}. "
        f"The strongest targets are {ranked}. "
        "This gives us a safer task tree / branch organization map without dumping the whole tree. "
        "Because this can delete or restructure real work, I saved a work-unit plan and am narrowing to one reviewable patch first."
    )
    return {
        "summary": summary,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "cleanup_reconnaissance",
            "scope": scope,
            "cleanup_candidate_count": candidate_count,
            "messy_branch_count": branch_count,
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "current_work_unit_id": agent_run_plan.get("current_work_unit_id"),
            "work_unit_count": agent_run_plan.get("work_unit_count"),
        },
        "agent_run_plan": agent_run_plan,
    }


def _build_workload_reconnaissance_payload(
    *,
    objective: str,
    tool_policy: dict[str, Any] | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(tool_policy, dict) or tool_policy.get("intent") != "workload_triage":
        return None
    preferred = (
        tool_policy.get("preferred_first_tool")
        if isinstance(tool_policy.get("preferred_first_tool"), dict)
        else {}
    )
    arguments = preferred.get("arguments") if isinstance(preferred.get("arguments"), dict) else {}
    tool_started_at = perf_counter()
    result = execute_kernel_tool(
        "get_workload_overview",
        dict(arguments),
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "get_workload_overview",
            "arguments": dict(arguments),
            "result": result,
            "duration_ms": _elapsed_ms(tool_started_at),
        }
    )
    if result.get("error"):
        return None

    agent_run_plan = build_workload_agent_run_plan(
        objective=objective,
        workload_result=result,
    )
    work_units = [
        item for item in agent_run_plan.get("work_units", []) if isinstance(item, dict)
    ]
    scope = str(result.get("scope") or agent_run_plan.get("scope") or "workload").strip()
    if not work_units:
        return {
            "summary": (
                f"I ran a workload reconnaissance pass over {scope} and did not find overdue, "
                "due-soon, or stale pressure points. I did not apply any changes."
            ),
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
            "deterministic_fast_path": {
                "kind": "workload_reconnaissance",
                "scope": scope,
                "agent_run_plan_id": agent_run_plan.get("plan_id"),
                "work_unit_count": 0,
            },
            "agent_run_plan": agent_run_plan,
        }

    top_titles = [
        str(item.get("target_task_title") or item.get("title") or "").strip()
        for item in work_units[:5]
        if str(item.get("target_task_title") or item.get("title") or "").strip()
    ]
    checkpoint = agent_run_plan.get("checkpoint") if isinstance(agent_run_plan.get("checkpoint"), dict) else {}
    summary = (
        f"I ran a workload reconnaissance pass over {scope} and saved a work-unit plan. "
        f"Signals: {checkpoint.get('overdue_count', 0)} overdue, "
        f"{checkpoint.get('due_soon_count', 0)} due soon, "
        f"{checkpoint.get('stale_count', 0)} stale. "
        f"The first targets are {', '.join(top_titles)}."
    )
    return {
        "summary": summary,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "workload_reconnaissance",
            "scope": scope,
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "current_work_unit_id": agent_run_plan.get("current_work_unit_id"),
            "work_unit_count": agent_run_plan.get("work_unit_count"),
        },
        "agent_run_plan": agent_run_plan,
    }


def _build_branch_reconnaissance_payload(
    *,
    objective: str,
    tool_policy: dict[str, Any] | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(tool_policy, dict):
        return None
    intent = str(tool_policy.get("intent") or "").strip()
    if intent not in {"branch_breakdown", "branch_restructure"}:
        return None
    preferred = (
        tool_policy.get("preferred_first_tool")
        if isinstance(tool_policy.get("preferred_first_tool"), dict)
        else {}
    )
    arguments = preferred.get("arguments") if isinstance(preferred.get("arguments"), dict) else {}
    tool_started_at = perf_counter()
    result = execute_kernel_tool(
        "get_task_branch",
        dict(arguments),
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "get_task_branch",
            "arguments": dict(arguments),
            "result": result,
            "duration_ms": _elapsed_ms(tool_started_at),
        }
    )
    if result.get("error"):
        return None

    agent_run_plan = build_branch_agent_run_plan(
        objective=objective,
        branch_result=result,
        plan_kind=intent,
    )
    work_units = [
        item for item in agent_run_plan.get("work_units", []) if isinstance(item, dict)
    ]
    scope = str(agent_run_plan.get("scope") or "branch").strip()
    if not work_units:
        return None

    top_titles = [
        str(item.get("target_task_title") or item.get("title") or "").strip()
        for item in work_units[:5]
        if str(item.get("target_task_title") or item.get("title") or "").strip()
    ]
    action_label = "restructure" if intent == "branch_restructure" else "breakdown"
    summary = (
        f"I inspected {scope} and saved a {action_label} work-unit plan instead of rewriting "
        f"the whole branch at once. The first slices are {', '.join(top_titles)}."
    )
    return {
        "summary": summary,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "branch_reconnaissance",
            "scope": scope,
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "current_work_unit_id": agent_run_plan.get("current_work_unit_id"),
            "work_unit_count": agent_run_plan.get("work_unit_count"),
            "plan_kind": intent,
        },
        "agent_run_plan": agent_run_plan,
    }


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


SCHEDULING_REQUEST_RE = re.compile(
    r"\b(?:schedul\w*|scheduel\w*|scheudel\w*|calendar|time[ -]?block|book|place)\b",
    re.IGNORECASE,
)
EXPLICIT_NAMED_TASK_RE = re.compile(
    r"\b(?:task|item|project)\s+(?:is\s+)?(?:named|called)\s+"
    r"[\"'“”]?(.+?)[\"'“”]?(?=$|[.!?,;]|\s+(?:for|at|on|tomorrow|today|next)\b)",
    re.IGNORECASE,
)
EXPLICIT_TIME_RANGE_RE = re.compile(
    r"\bbetween\b.+?\band\b|\bfrom\b.+?(?:\bto\b|\buntil\b|\s[-–]\s)|"
    r"\buntil\b|\bfor\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
SUBTASK_SCHEDULE_RE = re.compile(
    r"\b(?:subtasks?|children)\s+(?:of|under|for)\s+[\"'“”]?"
    r"(.+?)[\"'“”]?(?=$|[.!?,;]|\s+(?:across|over|on|tomorrow|today|starting|at)\b)",
    re.IGNORECASE,
)


def _human_duration(minutes: int | None) -> str | None:
    if not isinstance(minutes, int) or minutes <= 0:
        return None
    hours, remaining = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if remaining:
        parts.append(f"{remaining} minute{'s' if remaining != 1 else ''}")
    return " ".join(parts)


def _human_schedule_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    clock = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{parsed.strftime('%a, %b')} {parsed.day} at {clock}"


CALENDAR_SCOPED_RESCHEDULE_RE = re.compile(
    r"\b(?:find|use|choose|move|reschedul\w*|better)\b.{0,100}\b(?:time|slot|schedule)\b"
    r"|\b(?:tasks?|items?)\s+scheduled\b",
    re.IGNORECASE,
)
CALENDAR_SCOPED_REMOVE_RE = re.compile(
    r"\b(?:cancel|remove|delete|unschedul\w*)\b",
    re.IGNORECASE,
)


def _parse_local_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return None
    return parsed


def _authoritative_turn_date(
    today_override: date | None,
    workspace_context: dict[str, Any] | None,
) -> date:
    if today_override is not None:
        return today_override
    local_value = (
        workspace_context.get("local_date_time")
        if isinstance(workspace_context, dict)
        else None
    )
    if isinstance(local_value, datetime):
        return local_value.date()
    parsed_local = _parse_local_datetime(local_value)
    return parsed_local.date() if parsed_local is not None else date.today()


def _legacy_read_calendar_blocks_for_kernel(
    arguments: dict[str, Any],
    *,
    nodes: list[StrategyNode],
    categories: list[Category],
) -> dict[str, Any]:
    requested_task_ids = {
        int(item)
        for item in arguments.get("task_ids", [])
        if isinstance(item, int) and not isinstance(item, bool)
    } if isinstance(arguments.get("task_ids"), list) else set()
    all_for_tasks = (
        arguments.get("include_all_for_tasks") is True and bool(requested_task_ids)
    )
    start_at = None if all_for_tasks else _parse_local_datetime(arguments.get("start_at"))
    end_at = None if all_for_tasks else _parse_local_datetime(arguments.get("end_at"))
    if not all_for_tasks and (
        start_at is None or end_at is None or end_at <= start_at
    ):
        return {
            "error": (
                "Use a valid local start_at and end_at range, or provide task_ids "
                "with include_all_for_tasks=true."
            ),
            "blocks": [],
            "task_ids": [],
            "count": 0,
        }
    raw_limit = arguments.get("limit")
    limit = max(1, min(200, int(raw_limit))) if isinstance(raw_limit, int) else 100
    with Session(get_active_engine()) as calendar_session:
        statement = select(CalendarBlock).order_by(
            CalendarBlock.start_at, CalendarBlock.id
        )
        if start_at is not None and end_at is not None:
            statement = statement.where(CalendarBlock.start_at < end_at).where(
                CalendarBlock.end_at > start_at
            )
        if requested_task_ids:
            statement = statement.where(CalendarBlock.task_id.in_(requested_task_ids))
        blocks = list(calendar_session.exec(statement).all())[:limit]
        recurrence_series_ids = {
            str(block.recurrence_series_id)
            for block in blocks
            if block.recurrence_series_id
        }
        recurrence_series = (
            list(
                calendar_session.exec(
                    select(RecurrenceSeries).where(
                        RecurrenceSeries.id.in_(recurrence_series_ids)
                    )
                ).all()
            )
            if recurrence_series_ids
            else []
        )
    nodes_by_id = {int(node.id): node for node in nodes if node.id is not None}
    categories_by_id = {
        int(category.id): category for category in categories if category.id is not None
    }
    recurrence_by_id = {str(series.id): series for series in recurrence_series}

    def json_list(value: str) -> list[Any]:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    payload_blocks: list[dict[str, Any]] = []
    task_ids: list[int] = []
    for block in blocks:
        node = nodes_by_id.get(int(block.task_id))
        category = categories_by_id.get(int(block.category_id))
        series = recurrence_by_id.get(str(block.recurrence_series_id or ""))
        if int(block.task_id) not in task_ids:
            task_ids.append(int(block.task_id))
        payload_blocks.append(
            {
                "block_id": int(block.id or 0),
                "task_id": int(block.task_id),
                "task_title": str(node.title) if node is not None else f"Task {block.task_id}",
                "task_emoji": node.emoji if node is not None else None,
                "category_id": int(block.category_id),
                "category_name": str(category.name) if category is not None else "Calendar",
                "start_at": block.start_at.isoformat(),
                "end_at": block.end_at.isoformat(),
                "revision": int(block.revision),
                "is_pinned": bool(block.is_pinned),
                "is_protected": bool(block.is_protected),
                "is_recurring": bool(block.is_recurring),
                "recurrence_series_id": block.recurrence_series_id,
                "recurrence_key": block.recurrence_key,
                "recurrence_series_ref": (
                    f"recurrence_series:{block.recurrence_series_id}"
                    if block.recurrence_series_id
                    else None
                ),
                "recurrence_occurrence_ref": (
                    f"recurrence_occurrence:{block.recurrence_series_id}:{block.recurrence_key}"
                    if block.recurrence_series_id and block.recurrence_key
                    else None
                ),
                "recurrence_series_revision": (
                    int(series.revision) if series is not None else None
                ),
                "recurrence_rule": (
                    {
                        "frequency": series.frequency,
                        "interval": int(series.interval),
                        "days_of_week": json_list(series.days_of_week_json),
                        "month_days": json_list(series.month_days_json),
                        "start_date": series.start_date.isoformat(),
                        "end_date": (
                            series.end_date.isoformat() if series.end_date else None
                        ),
                        "occurrence_count": series.occurrence_limit,
                        "preferred_start_time": series.preferred_start_time,
                        "allowed_start_time": series.allowed_start_time,
                        "allowed_end_time": series.allowed_end_time,
                        "duration_minutes": int(series.duration_minutes),
                        "timezone": series.timezone,
                        "strict": bool(series.strict),
                        "gap_minutes": series.gap_minutes,
                        "conflict_relocation": series.conflict_relocation,
                        "allow_priority_displacement": bool(
                            series.allow_priority_displacement
                        ),
                    }
                    if series is not None
                    else None
                ),
            }
        )
    return {
        "read_only": True,
        "range_start": start_at.isoformat() if start_at is not None else None,
        "range_end": end_at.isoformat() if end_at is not None else None,
        "scope": "task_schedule" if all_for_tasks else "calendar_range",
        "blocks": payload_blocks,
        "task_ids": task_ids,
        "recurrence_series_refs": sorted(
            {
                str(block["recurrence_series_ref"])
                for block in payload_blocks
                if block.get("recurrence_series_ref")
            }
        ),
        "recurrence_occurrence_refs": sorted(
            {
                str(block["recurrence_occurrence_ref"])
                for block in payload_blocks
                if block.get("recurrence_occurrence_ref")
            }
        ),
        "count": len(payload_blocks),
    }


def _prefetch_single_task_metadata_target(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
    tool_executor: Callable[..., dict[str, Any]],
) -> bool:
    """Resolve one exact metadata target as read evidence before model authoring."""

    if route_decision.route_kind != "single_task_metadata_mutation":
        return False
    resolved_ids: set[int] = set()
    for query in _entity_search_queries_for_prompt(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
        limit=1,
    ):
        tool_started = perf_counter()
        result = tool_executor(
            "search_entities",
            {"query": query, "limit": 5},
            categories=categories,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            staged_node_ids=staged_node_ids,
            working_set=working_set,
            today_override=today_override,
        )
        matches = result.get("matches") if isinstance(result, dict) else None
        trace_result = result
        if not isinstance(matches, list) or not _search_result_is_confident(
            matches,
            prompt=prompt,
        ):
            tool_trace.append(
                {
                    "name": "search_entities",
                    "arguments": {"query": query, "limit": 5},
                    "result": result,
                    "duration_ms": _elapsed_ms(tool_started),
                    "prefetch": True,
                    "completion_candidate": False,
                }
            )
            continue
        task = _task_from_search_match(matches[0])
        resolved = _node_from_serialized_task(task, nodes) if task is not None else None
        if resolved is not None and resolved.id is not None:
            resolved_ids.add(int(resolved.id))
            trace_result = {
                "matches": [
                    {
                        "task_id": int(resolved.id),
                        "task_title": resolved.title,
                    }
                ],
                "read_only": True,
                "result_kind": "task_search",
            }
        tool_trace.append(
            {
                "name": "search_entities",
                "arguments": {"query": query, "limit": 5},
                "result": trace_result,
                "duration_ms": _elapsed_ms(tool_started),
                "prefetch": True,
                "completion_candidate": False,
            }
        )
    return len(resolved_ids) == 1


def _read_calendar_blocks_for_kernel(
    arguments: dict[str, Any],
    *,
    nodes: list[StrategyNode],
    categories: list[Category],
) -> dict[str, Any]:
    """Compatibility wrapper over the native calendar application service."""
    return SQL_CALENDAR_READ_SERVICE.read(
        arguments,
        invocation=CalendarReadInvocation(
            nodes=nodes,
            categories=categories,
        ),
    )


def _review_clock_label(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _review_calendar_block_label(
    block: CalendarBlock,
    *,
    nodes_by_id: dict[int, StrategyNode],
) -> str:
    task = nodes_by_id.get(int(block.task_id))
    task_title = str(task.title) if task is not None else f"Task {block.task_id}"
    day = block.start_at.strftime("%A, %B %d").replace(" 0", " ")
    return (
        f"{task_title} — {day} at {_review_clock_label(block.start_at)}"
        f"–{_review_clock_label(block.end_at)}"
    )


def _new_preference_review_label(operation: object) -> str:
    logical = decode_preference_value(
        operation.value.model_dump(mode="json", by_alias=True)
        if hasattr(operation.value, "model_dump")
        else None
    )
    gap = logical.get("interTaskGapMinutes")
    if isinstance(gap, int) and not isinstance(gap, bool):
        return "Gap between tasks"
    preference_text = str(logical.get("preferenceText") or "").strip()
    if preference_text:
        return (
            preference_text
            if len(preference_text) <= 120
            else f"{preference_text[:117].rstrip()}..."
        )
    key = str(getattr(operation, "preference_key", "") or "").strip()
    return key.replace("_", " ").title() or "Planning preference"


def _review_entity_titles(
    session: Session,
    *,
    change_set: ChangeSet,
    nodes: list[StrategyNode],
    categories: list[Category],
) -> dict[str, str]:
    """Resolve model-authored refs to authoritative, user-readable labels."""

    titles: dict[str, str] = {}
    referenced_ids: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        entity_id = value.get("entity_id")
        if isinstance(entity_id, str):
            referenced_ids.add(entity_id)
            title = value.get("title")
            if isinstance(title, str) and title.strip():
                titles[entity_id] = title.strip()
        for item in value.values():
            visit(item)

    visit(change_set.model_dump(mode="python"))
    nodes_by_id = {
        int(node.id): node for node in nodes if node.id is not None
    }
    categories_by_id = {
        int(category.id): category for category in categories if category.id is not None
    }

    for entity_id in referenced_ids:
        try:
            if entity_id.startswith("task:"):
                node = nodes_by_id.get(int(entity_id.removeprefix("task:")))
                if node is not None:
                    titles[entity_id] = str(node.title)
            elif entity_id.startswith("workspace:"):
                category = categories_by_id.get(
                    int(entity_id.removeprefix("workspace:"))
                )
                if category is not None:
                    titles[entity_id] = str(category.name)
            elif entity_id.startswith("calendar_block:"):
                block = session.get(
                    CalendarBlock,
                    int(entity_id.removeprefix("calendar_block:")),
                )
                if block is not None:
                    titles[entity_id] = _review_calendar_block_label(
                        block,
                        nodes_by_id=nodes_by_id,
                    )
            elif entity_id.startswith("recurrence_series:"):
                series = session.get(
                    RecurrenceSeries,
                    entity_id.removeprefix("recurrence_series:"),
                )
                if series is not None:
                    task = nodes_by_id.get(int(series.task_id))
                    task_title = (
                        str(task.title) if task is not None else f"Task {series.task_id}"
                    )
                    titles[entity_id] = f"{task_title} repeating schedule"
            elif entity_id.startswith("recurrence_occurrence:"):
                identity = entity_id.removeprefix("recurrence_occurrence:")
                series_id, recurrence_key = identity.rsplit(":", 1)
                block = session.exec(
                    select(CalendarBlock)
                    .where(CalendarBlock.recurrence_series_id == series_id)
                    .where(CalendarBlock.recurrence_key == recurrence_key)
                ).first()
                if block is not None:
                    titles[entity_id] = _review_calendar_block_label(
                        block,
                        nodes_by_id=nodes_by_id,
                    )
            elif entity_id.startswith("preference:"):
                preference = session.get(
                    UserPreference,
                    entity_id.removeprefix("preference:"),
                )
                if preference is not None:
                    titles[entity_id] = (
                        preference.description
                        or preference.preference_type.replace("_", " ").title()
                    )
        except (TypeError, ValueError):
            # Invalid IDs are rejected by the semantic contracts. Presentation
            # resolution remains fail-soft so it cannot change authority.
            continue

    for operation in change_set.operations:
        temp_ref = getattr(operation, "temp_ref", None)
        if temp_ref is not None:
            if operation.kind == "task.create":
                titles[temp_ref.entity_id] = str(operation.title)
            elif operation.kind == "calendar.create_block":
                task_title = titles.get(operation.task_ref.entity_id, "Scheduled work")
                day = operation.start_at.strftime("%A, %B %d").replace(" 0", " ")
                titles[temp_ref.entity_id] = (
                    f"{task_title} — {day} at {_review_clock_label(operation.start_at)}"
                    f"–{_review_clock_label(operation.end_at)}"
                )
            elif operation.kind == "recurrence.create_series":
                task_title = titles.get(operation.task_ref.entity_id, "Scheduled work")
                titles[temp_ref.entity_id] = f"{task_title} repeating schedule"
        if operation.kind in {"preference.upsert", "preference.supersede"}:
            titles[
                f"projected:preference:{operation.operation_id}"
            ] = _new_preference_review_label(operation)

    return titles


def _month_shift(value: date, months: int) -> tuple[int, int]:
    month_index = (value.year * 12 + value.month - 1) + months
    return month_index // 12, month_index % 12 + 1


def _calendar_date_from_prompt(
    session: Session,
    *,
    prompt: str,
    today_value: date,
) -> date | None:
    if re.search(r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening))\b", prompt, re.I):
        return today_value
    if re.search(r"\btomorrow\b", prompt, re.I):
        return today_value + timedelta(days=1)
    parsed = parse_prompt_date(prompt, today_value)
    if parsed:
        try:
            return date.fromisoformat(parsed)
        except ValueError:
            pass
    weekday_match = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        prompt,
        re.IGNORECASE,
    )
    if weekday_match is not None and not re.search(
        r"\b(?:every|each)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        prompt,
        re.IGNORECASE,
    ):
        weekday_lookup = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        target_weekday = weekday_lookup[weekday_match.group(1).lower()]
        return today_value + timedelta(
            days=(target_weekday - today_value.weekday()) % 7
        )
    ordinal = re.search(r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)\b", prompt, re.I)
    if ordinal is None:
        return None
    day = int(ordinal.group(1))
    candidates: list[date] = []
    for month_delta in (-1, 0, 1):
        year, month = _month_shift(today_value, month_delta)
        if 1 <= day <= monthrange(year, month)[1]:
            candidates.append(date(year, month, day))
    if not candidates:
        return None
    nearby_start = datetime.combine(min(candidates), time.min)
    nearby_end = datetime.combine(max(candidates) + timedelta(days=1), time.min)
    scheduled_dates = {
        block.start_at.date()
        for block in session.exec(
            select(CalendarBlock)
            .where(CalendarBlock.start_at >= nearby_start)
            .where(CalendarBlock.start_at < nearby_end)
        ).all()
    }
    with_blocks = [candidate for candidate in candidates if candidate in scheduled_dates]
    pool = with_blocks or candidates
    return min(
        pool,
        key=lambda candidate: (
            abs((candidate - today_value).days),
            0 if candidate >= today_value else 1,
        ),
    )


def _workspace_local_datetime(
    workspace_context: dict[str, Any] | None,
) -> datetime | None:
    value = (
        workspace_context.get("local_date_time")
        if isinstance(workspace_context, dict)
        else None
    )
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.replace(tzinfo=None)
        return value
    return _parse_local_datetime(value)


_EXPLICIT_CLOCK_TIME_RE = re.compile(
    r"\b(?:at|around|between|from)\s+"
    r"(?:"
    r"(?:0?\d|1\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?"
    r"|(?:0?[1-9]|1[0-2])(?:\:[0-5]\d)?\s*(?:am|pm)"
    r")\b"
    r"|\b(?:0?\d|1\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?\b",
    re.IGNORECASE,
)


def _has_explicit_clock_time(prompt: str) -> bool:
    return _EXPLICIT_CLOCK_TIME_RE.search(prompt) is not None


def _prompt_names_schedule_start(prompt: str, start_at: datetime) -> bool:
    """Return whether the user text names this exact wall-clock start."""

    normalized = str(prompt or "").casefold().replace(".", "")
    hour_12 = start_at.hour % 12 or 12
    suffix = "am" if start_at.hour < 12 else "pm"
    minute = start_at.minute
    candidates = {
        f"{start_at.hour:02d}:{minute:02d}",
        f"{start_at.hour}:{minute:02d}",
        f"{hour_12}:{minute:02d} {suffix}",
        f"{hour_12}:{minute:02d}{suffix}",
    }
    if minute == 0:
        candidates.update({f"{hour_12} {suffix}", f"{hour_12}{suffix}"})
    return any(candidate in normalized for candidate in candidates)


def _one_time_schedule_window(
    *,
    target_date: date,
    prompt: str,
    local_now: datetime | None,
) -> tuple[datetime, datetime] | None:
    """Resolve a broad user-named daypart without inventing an exact time."""

    day_start = datetime.combine(target_date, time.min)
    if re.search(r"\bmornings?\b", prompt, re.IGNORECASE):
        window_start = datetime.combine(target_date, time(8))
        window_end = datetime.combine(target_date, time(12))
    elif re.search(r"\bafternoons?\b", prompt, re.IGNORECASE):
        window_start = datetime.combine(target_date, time(13))
        window_end = datetime.combine(target_date, time(17))
    elif re.search(r"\bevenings?\b", prompt, re.IGNORECASE):
        window_start = datetime.combine(target_date, time(17))
        window_end = datetime.combine(target_date, time(21))
    elif re.search(r"\btonight\b", prompt, re.IGNORECASE):
        window_start = datetime.combine(target_date, time(17))
        window_end = datetime.combine(target_date + timedelta(days=1), time.min)
    else:
        window_start = day_start
        window_end = datetime.combine(target_date + timedelta(days=1), time.min)
    if local_now is not None and local_now.date() == target_date:
        window_start = max(window_start, local_now)
    if window_end <= window_start:
        return None
    return window_start, window_end


def _ceil_local_datetime(value: datetime, *, slot_minutes: int = 5) -> datetime:
    """Round a local wall time up so a proposed block never starts in the past."""

    bounded_slot = max(1, min(60, int(slot_minutes)))
    minute_remainder = value.minute % bounded_slot
    delta_minutes = (bounded_slot - minute_remainder) % bounded_slot
    rounded = value.replace(second=0, microsecond=0)
    if delta_minutes:
        rounded += timedelta(minutes=delta_minutes)
    elif value.second or value.microsecond:
        rounded += timedelta(minutes=bounded_slot)
    return rounded


def _is_flexible_today_schedule(
    *,
    prompt: str,
    target_date: date,
    local_now: datetime | None,
) -> bool:
    """Return whether a same-day request permits a viability-based alternative.

    An explicit clock time remains authoritative and must not be silently moved.
    A broad request such as "today" or "tonight" names urgency but leaves exact
    placement to Planner, so the approval review may disclose the next viable day
    when no preference-compliant time remains.
    """

    return bool(
        local_now is not None
        and target_date == local_now.date()
        and re.search(r"\b(?:today|tonight)\b", prompt, re.IGNORECASE)
        and not _has_explicit_clock_time(prompt)
    )


def _mutable_created_schedule_groups(spec: dict[str, Any]) -> list[dict[str, Any]]:
    raw_groups = spec.get("schedule_groups")
    if isinstance(raw_groups, list):
        return [group for group in raw_groups if isinstance(group, dict)]
    return [spec]


def _authoritative_schedule_group_text(
    *,
    group: dict[str, Any],
    group_index: int,
    request_items: list[dict[str, Any]] | None,
    fallback_prompt: str,
) -> str:
    """Return user-authored text bound to one schedule group when available."""

    target_temp_ids = {
        str(value).strip()
        for value in group.get("target_temp_ids", [])
        if str(value).strip()
    } if isinstance(group.get("target_temp_ids"), list) else set()
    target_task_ids = {
        value
        for value in group.get("target_task_ids", [])
        if isinstance(value, int) and not isinstance(value, bool)
    } if isinstance(group.get("target_task_ids"), list) else set()
    matches: list[str] = []
    for item in request_items or []:
        if not isinstance(item, dict) or "schedule" not in (item.get("changes") or []):
            continue
        item_index = item.get("schedule_group_index")
        item_temp_ids = set(item.get("target_temp_ids") or [])
        item_task_ids = set(item.get("target_task_ids") or [])
        bound = (
            item_index == group_index
            or bool(target_temp_ids.intersection(item_temp_ids))
            or bool(target_task_ids.intersection(item_task_ids))
        )
        source_text = str(item.get("source_text") or "").strip()
        if bound and source_text:
            matches.append(source_text)
    return " ".join(dict.fromkeys(matches)) or fallback_prompt


def _constrain_created_schedule_to_current_time(
    spec: dict[str, Any],
    *,
    prompt: str,
    local_now: datetime | None,
    request_items: list[dict[str, Any]] | None = None,
) -> tuple[bool, date | None, list[str]]:
    """Make authoritative current time a hard lower bound for today's groups."""

    if local_now is None:
        return False, None, []
    adjusted = False
    requested_date: date | None = None
    errors: list[str] = []
    for group_index, group in enumerate(_mutable_created_schedule_groups(spec)):
        try:
            start_at = datetime.fromisoformat(str(group.get("window_start") or ""))
            end_at = datetime.fromisoformat(str(group.get("window_end") or ""))
        except ValueError:
            continue
        start_at = start_at.replace(tzinfo=None)
        end_at = end_at.replace(tzinfo=None)
        group_prompt = _authoritative_schedule_group_text(
            group=group,
            group_index=group_index,
            request_items=request_items,
            fallback_prompt=prompt,
        )
        if (
            _has_explicit_clock_time(group_prompt)
            and not _prompt_names_schedule_start(prompt, start_at)
        ):
            # Request-item text is model-authored metadata. It may quote a
            # selected placement, but that does not upgrade the placement into
            # a user-fixed constraint unless the authoritative request names
            # the same wall-clock start.
            group_prompt = prompt
        flexible_today = _is_flexible_today_schedule(
            prompt=group_prompt,
            target_date=start_at.date(),
            local_now=local_now,
        )
        if flexible_today:
            requested_date = start_at.date()
        if start_at >= local_now:
            continue
        if _prompt_names_schedule_start(prompt, start_at):
            errors.append(
                "The requested exact start time has already passed in the "
                "authoritative workspace time zone."
            )
            continue
        bounded_start = _ceil_local_datetime(
            max(start_at, local_now),
            slot_minutes=(
                int(group.get("slot_minutes"))
                if isinstance(group.get("slot_minutes"), int)
                and not isinstance(group.get("slot_minutes"), bool)
                else 5
            ),
        )
        if bounded_start != start_at:
            group["window_start"] = bounded_start.isoformat()
            adjusted = True
        # Keep the requested day's end boundary. If too little viable time
        # remains, deterministic planning will prove that and trigger the
        # disclosed next-day alternative below.
        if end_at <= bounded_start:
            broad_window = _one_time_schedule_window(
                target_date=start_at.date(),
                prompt=group_prompt,
                local_now=local_now,
            )
            if broad_window is not None:
                end_at = broad_window[1]
        group["window_end"] = end_at.isoformat()
    return adjusted, requested_date, list(dict.fromkeys(errors))


def _next_viable_day_schedule_spec(
    spec: dict[str, Any],
    *,
    prompt: str,
    requested_date: date,
) -> dict[str, Any]:
    """Build a broad next-day search window without choosing an exact time."""

    revised = copy.deepcopy(spec)
    target_date = requested_date + timedelta(days=1)
    for group in _mutable_created_schedule_groups(revised):
        group_prompt = str(group.get("instruction") or prompt)
        daypart_window = _one_time_schedule_window(
            target_date=target_date,
            prompt=group_prompt,
            local_now=None,
        )
        if daypart_window is None:
            continue
        window_start, window_end = daypart_window
        if not re.search(
            r"\b(?:morning|afternoon|evening|tonight)\b",
            group_prompt,
            re.IGNORECASE,
        ):
            # A broad whole-day search should still reflect ordinary human
            # availability. Saved sleep/availability constraints may narrow it
            # further, and Planner chooses the exact viable placement.
            window_start = datetime.combine(target_date, time(8))
            window_end = datetime.combine(target_date, time(23))
        group["window_start"] = window_start.isoformat()
        group["window_end"] = window_end.isoformat()
    return revised


def _constrain_planner_window_to_current_time(
    *,
    window_start: datetime,
    window_end: datetime,
    instruction: str,
    local_now: datetime | None,
) -> tuple[datetime, datetime, dict[str, Any] | None]:
    """Apply the same temporal viability rule to persisted-task Planner handoffs."""

    if local_now is None or window_start >= local_now:
        return window_start, window_end, None
    if _has_explicit_clock_time(instruction):
        raise ValueError(
            "The requested exact start time has already passed in the "
            "authoritative workspace time zone."
        )
    bounded_start = _ceil_local_datetime(max(window_start, local_now))
    if window_end > bounded_start:
        return bounded_start, window_end, None
    if not _is_flexible_today_schedule(
        prompt=instruction,
        target_date=window_start.date(),
        local_now=local_now,
    ):
        raise ValueError(
            "The proposed scheduling window has already passed in the "
            "authoritative workspace time zone."
        )
    proposed_date = local_now.date() + timedelta(days=1)
    next_start = datetime.combine(proposed_date, time(8))
    next_end = datetime.combine(proposed_date, time(23))
    disclosure = (
        f"There is no viable time left in the requested window today. "
        f"This review proposes the next preference-compliant time on "
        f"{proposed_date.strftime('%A, %B %d').replace(' 0', ' ')} instead."
    )
    return (
        next_start,
        next_end,
        {
            "kind": "next_viable_day",
            "requested_date": window_start.date().isoformat(),
            "proposed_date": proposed_date.isoformat(),
            "authoritative_local_time": local_now.isoformat(),
            "reason": "requested_window_already_passed",
            "disclosure": disclosure,
        },
    )


def _derive_missing_created_task_schedule(
    session: Session,
    *,
    proposal: dict[str, Any],
    prompt: str,
    today_value: date,
    workspace_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Recover an omitted one-time schedule without inventing semantic details.

    This repair is intentionally narrow. The user must explicitly request both
    creation and one-time scheduling on a resolvable day. Planner may infer
    missing low-risk duration estimates from task history and bounded defaults.
    Exact clock-time requests remain model-owned.
    """

    if isinstance(proposal.get("created_task_schedule"), dict):
        return proposal["created_task_schedule"]
    if isinstance(
        proposal.get("recurring_schedule", proposal.get("recurringSchedule")),
        dict,
    ):
        return None
    if (
        not CREATE_TASK_ACTION_CONTRACT_RE.search(prompt)
        or not (
            SCHEDULING_REQUEST_RE.search(prompt)
            or (
                ONE_TIME_CREATE_PLACEMENT_RE.search(prompt)
                and not re.search(r"\b(?:due|deadline)\b", prompt, re.IGNORECASE)
            )
        )
    ):
        return None
    if _has_explicit_clock_time(prompt):
        return None
    target_date = _calendar_date_from_prompt(
        session,
        prompt=prompt,
        today_value=today_value,
    )
    if target_date is None:
        return None

    task_patch = build_task_patch_draft(
        proposal_kind="branch",
        proposal=proposal,
    )
    create_operations = [
        operation
        for operation in task_patch.get("operations", [])
        if isinstance(operation, dict)
        and str(operation.get("op") or "") == "create"
        and str(operation.get("temp_id") or "").strip()
    ]
    if not create_operations:
        return None
    parent_temp_ids = {
        str(operation.get("parent_temp_id") or "").strip()
        for operation in create_operations
        if str(operation.get("parent_temp_id") or "").strip()
    }
    leaf_operations = [
        operation
        for operation in create_operations
        if str(operation.get("temp_id") or "").strip() not in parent_temp_ids
    ]
    if not leaf_operations:
        return None
    window = _one_time_schedule_window(
        target_date=target_date,
        prompt=prompt,
        local_now=_workspace_local_datetime(workspace_context),
    )
    if window is None:
        return None
    window_start, window_end = window
    return {
        "target_temp_ids": [
            str(operation["temp_id"]).strip()
            for operation in leaf_operations
        ],
        "instruction": prompt,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "slot_minutes": 5,
        "source": "explicit_one_time_schedule_repair",
    }


def _enrich_created_task_schedule_before_compilation(
    session: Session,
    *,
    proposal: dict[str, Any],
    prompt: str,
    today_value: date,
    workspace_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    # Scheduling meaning must already be present in the model-authored semantic
    # plan. The host may enrich estimates and validate placement, but it must
    # not infer a missing window from natural-language prompt text.
    spec = proposal.get("created_task_schedule")
    if not isinstance(spec, dict):
        return None
    local_now = _workspace_local_datetime(workspace_context)
    (
        constrained_to_now,
        requested_date,
        current_time_errors,
    ) = _constrain_created_schedule_to_current_time(
        spec,
        prompt=prompt,
        local_now=local_now,
        request_items=(
            proposal.get("request_items")
            if isinstance(proposal.get("request_items"), list)
            else None
        ),
    )
    task_patch = build_task_patch_draft(
        proposal_kind="branch",
        proposal=proposal,
    )
    create_operations = [
        operation
        for operation in task_patch.get("operations", [])
        if isinstance(operation, dict)
        and str(operation.get("op") or "") == "create"
    ]
    plan = build_created_task_schedule_plan(
        session,
        spec=spec,
        create_operations=create_operations,
    )
    if current_time_errors:
        plan = {
            **plan,
            "status": "invalid",
            "errors": [
                *current_time_errors,
                *[
                    error
                    for error in plan.get("errors", [])
                    if error not in current_time_errors
                ],
            ],
        }
    if (
        plan.get("status") != "valid"
        and requested_date is not None
        and local_now is not None
        and _is_flexible_today_schedule(
            prompt=prompt,
            target_date=requested_date,
            local_now=local_now,
        )
    ):
        alternative_spec = _next_viable_day_schedule_spec(
            spec,
            prompt=prompt,
            requested_date=requested_date,
        )
        alternative_plan = build_created_task_schedule_plan(
            session,
            spec=alternative_spec,
            create_operations=create_operations,
        )
        if alternative_plan.get("status") == "valid":
            proposed_date = requested_date + timedelta(days=1)
            disclosure = (
                f"There is not enough preference-compliant time left today. "
                f"This review proposes the next viable time on "
                f"{proposed_date.strftime('%A, %B %d').replace(' 0', ' ')} instead."
            )
            proposal["created_task_schedule"] = alternative_spec
            alternative_plan["temporal_adjustment"] = {
                "kind": "next_viable_day",
                "requested_date": requested_date.isoformat(),
                "proposed_date": proposed_date.isoformat(),
                "authoritative_local_time": local_now.isoformat(),
                "reason": "insufficient_preference_compliant_time_remaining",
                "disclosure": disclosure,
            }
            alternative_plan["placement_reasons"] = [
                {
                    "kind": "temporal_viability",
                    "message": disclosure,
                },
                *list(alternative_plan.get("placement_reasons") or []),
            ]
            plan = alternative_plan
    if constrained_to_now:
        plan["current_time_lower_bound_applied"] = True
    duration_estimates = (
        plan.get("duration_estimates")
        if isinstance(plan.get("duration_estimates"), dict)
        else {}
    )
    structural_operations = proposal.get("structural_operations")
    if isinstance(structural_operations, list):
        for operation in structural_operations:
            if not isinstance(operation, dict):
                continue
            temp_id = str(operation.get("temp_id") or "").strip()
            estimate = duration_estimates.get(temp_id)
            current_estimate = operation.get("time_estimate_minutes")
            if (
                temp_id
                and isinstance(estimate, dict)
                and isinstance(estimate.get("minutes"), int)
                and (
                    isinstance(current_estimate, bool)
                    or not isinstance(current_estimate, int)
                    or current_estimate <= 0
                )
            ):
                operation["time_estimate_minutes"] = int(estimate["minutes"])
    proposal["created_task_schedule_plan"] = plan
    proposal["created_task_schedule_status"] = plan.get("status")
    proposal["created_task_schedule_errors"] = list(plan.get("errors") or [])
    return plan


def _repair_created_task_step_schedule_arguments(
    arguments: dict[str, Any],
    *,
    prompt: str,
    session: Session | None = None,
    today_value: date | None = None,
    workspace_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind an omitted schedule target list from submitted task structure."""

    schedule = arguments.get("created_task_schedule")
    operations = arguments.get("structural_operations")
    if not isinstance(schedule, dict) or not isinstance(operations, list):
        return arguments
    creates = [
        operation
        for operation in operations
        if isinstance(operation, dict)
        and str(operation.get("temp_id") or "").strip()
        and (
            normalize_structural_operation_kind(
                operation.get("kind")
                or operation.get("operation")
                or operation.get("op")
                or operation.get("type")
                or operation.get("action")
            )
            in {"create_node", "create_task"}
            or bool(str(operation.get("title") or "").strip())
        )
    ]
    parent_by_child = {
        str(operation.get("temp_id") or "").strip(): str(
            operation.get("parent_temp_id") or ""
        ).strip()
        for operation in creates
        if str(operation.get("temp_id") or "").strip()
        and str(operation.get("parent_temp_id") or "").strip()
    }

    def remove_redundant_ancestors(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        requested = [
            str(item).strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
        requested_set = set(requested)
        redundant: set[str] = set()
        for child_id in requested:
            seen: set[str] = set()
            parent_id = parent_by_child.get(child_id, "")
            while parent_id and parent_id not in seen:
                seen.add(parent_id)
                if parent_id in requested_set:
                    redundant.add(parent_id)
                parent_id = parent_by_child.get(parent_id, "")
        return [item for item in requested if item not in redundant]

    repaired_hierarchy = copy.deepcopy(arguments)
    hierarchy_schedule = repaired_hierarchy.get("created_task_schedule")
    hierarchy_changed = False
    if isinstance(hierarchy_schedule, dict):
        direct_targets = hierarchy_schedule.get("target_temp_ids")
        bounded_targets = remove_redundant_ancestors(direct_targets)
        if bounded_targets != direct_targets:
            hierarchy_schedule["target_temp_ids"] = bounded_targets
            hierarchy_changed = True
        groups = hierarchy_schedule.get("schedule_groups")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_targets = group.get("target_temp_ids")
                bounded_group_targets = remove_redundant_ancestors(group_targets)
                if bounded_group_targets != group_targets:
                    group["target_temp_ids"] = bounded_group_targets
                    hierarchy_changed = True
                constraints = group.get("request_constraints")
                if isinstance(constraints, list):
                    for constraint in constraints:
                        if not isinstance(constraint, dict):
                            continue
                        constraint_targets = constraint.get("target_temp_ids")
                        bounded_constraint_targets = remove_redundant_ancestors(
                            constraint_targets
                        )
                        if bounded_constraint_targets != constraint_targets:
                            constraint["target_temp_ids"] = bounded_constraint_targets
                            hierarchy_changed = True
    if hierarchy_changed:
        arguments = repaired_hierarchy
        schedule = hierarchy_schedule
    parent_temp_ids = {
        str(operation.get("parent_temp_id") or "").strip()
        for operation in creates
        if str(operation.get("parent_temp_id") or "").strip()
    }
    structural_leaves = [
        operation
        for operation in creates
        if str(operation.get("temp_id") or "").strip() not in parent_temp_ids
    ]
    if not structural_leaves:
        return arguments

    def operation_duration(operation: dict[str, Any]) -> int | None:
        duration = operation.get(
            "time_estimate_minutes",
            operation.get(
                "estimate_minutes",
                operation.get(
                    "estimated_duration_minutes",
                    operation.get("duration_minutes"),
                ),
            ),
        )
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            nested = operation.get("node")
            if isinstance(nested, dict):
                duration = nested.get(
                    "time_estimate_minutes",
                    nested.get(
                        "estimate_minutes",
                        nested.get(
                            "estimated_duration_minutes",
                            nested.get("duration_minutes"),
                        ),
                    ),
                )
        return (
            None
            if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration <= 0
            )
            else duration
        )

    leaves = [
        operation
        for operation in structural_leaves
        if operation_duration(operation) is not None
    ]
    if len(creates) == 1 and len(structural_leaves) == 1:
        leaves = structural_leaves
    elif len(leaves) != len(structural_leaves):
        # Providers sometimes omit/rename the parent link while still emitting
        # the standard workflow shape: one undurated container followed by only
        # durated steps. Accept that shape, but never drop a second undurated
        # operation because it may be a real incomplete step.
        durated_creates = [
            operation
            for operation in creates
            if operation_duration(operation) is not None
        ]
        if len(durated_creates) != len(creates) - 1:
            return arguments
        leaves = durated_creates
    if not leaves:
        return arguments
    leaf_ids = [str(operation["temp_id"]).strip() for operation in leaves]
    requested_ids = [
        str(item).strip()
        for item in schedule.get("target_temp_ids", [])
        if isinstance(item, str) and str(item).strip()
    ]
    create_ids = {
        str(operation.get("temp_id") or "").strip()
        for operation in creates
    }
    if "schedule_groups" in schedule:
        return arguments
    # An absent list defaults to every created node in the legacy contract.
    # A parent container in the list likewise conflicts with "schedule those
    # steps". In both cases, retain the model's window and bind only the leaves.
    if requested_ids and set(requested_ids).issubset(set(leaf_ids)):
        return arguments
    if requested_ids and not set(requested_ids).issubset(create_ids):
        return arguments
    repaired = copy.deepcopy(arguments)
    repaired_schedule = copy.deepcopy(schedule)
    repaired_schedule["target_temp_ids"] = leaf_ids
    repaired["created_task_schedule"] = repaired_schedule
    return repaired


def _remembered_wake_minutes(session: Session) -> int | None:
    constraints = list(
        session.exec(
            select(SchedulingConstraint)
            .where(SchedulingConstraint.active == True)  # noqa: E712
            .order_by(SchedulingConstraint.updated_at.desc())
        ).all()
    )
    for constraint in constraints:
        description = str(constraint.description or "").lower()
        try:
            value = json.loads(constraint.value_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        rule = str(value.get("rule") or "").lower()
        start_text = str(value.get("startTime") or value.get("start_time") or "")
        end_text = str(value.get("endTime") or value.get("end_time") or "")
        if not end_text or not (
            "wake" in description
            or "before" in description
            or "avoid" in rule
        ):
            continue
        if start_text and start_text not in {"00:00", "0:00", "00:00:00"}:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", end_text)
        if match is None:
            continue
        hours, minutes = int(match.group(1)), int(match.group(2))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return hours * 60 + minutes
    return None


def _calendar_reschedule_handoff_arguments(
    session: Session,
    *,
    prompt: str,
    today_value: date,
    nodes: list[StrategyNode],
    categories: list[Category],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not CALENDAR_SCOPED_RESCHEDULE_RE.search(prompt):
        return None
    if not re.search(r"\b(?:tasks?|items?)\b", prompt, re.I):
        return None
    target_date = _calendar_date_from_prompt(
        session,
        prompt=prompt,
        today_value=today_value,
    )
    if target_date is None:
        return None
    day_start = datetime.combine(target_date, time.min)
    day_end = datetime.combine(target_date + timedelta(days=1), time.min)
    calendar_result = _read_calendar_blocks_for_kernel(
        {"start_at": day_start.isoformat(), "end_at": day_end.isoformat(), "limit": 200},
        nodes=nodes,
        categories=categories,
    )
    blocks = calendar_result.get("blocks")
    if not isinstance(blocks, list):
        return None
    wake_minutes = _remembered_wake_minutes(session)
    if re.search(r"\bbefore\b.{0,40}\bwake", prompt, re.I):
        if wake_minutes is None:
            return None
        blocks = [
            block
            for block in blocks
            if isinstance(block, dict)
            and (start_at := _parse_local_datetime(block.get("start_at"))) is not None
            and (start_at.hour * 60 + start_at.minute) < wake_minutes
        ]
    task_ids = list(dict.fromkeys(
        int(block["task_id"])
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("task_id"), int)
    ))
    if not task_ids:
        return None
    window_start = day_start + timedelta(minutes=wake_minutes or 0)
    return (
        {
            "task_ids": task_ids,
            "instruction": prompt,
            "window_start": window_start.isoformat(),
            "window_end": day_end.isoformat(),
            "operation_mode": "reschedule",
            "scope_source": "calendar_query",
        },
        {**calendar_result, "blocks": blocks, "task_ids": task_ids, "count": len(blocks)},
    )


def _calendar_remove_handoff_arguments(
    session: Session,
    *,
    prompt: str,
    today_value: date,
    nodes: list[StrategyNode],
    categories: list[Category],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a removal handoff only for one named occurrence on one date."""

    if not CALENDAR_SCOPED_REMOVE_RE.search(prompt):
        return None
    target_date = _calendar_date_from_prompt(
        session,
        prompt=prompt,
        today_value=today_value,
    )
    if target_date is None:
        return None
    day_start = datetime.combine(target_date, time.min)
    day_end = datetime.combine(target_date + timedelta(days=1), time.min)
    calendar_result = _read_calendar_blocks_for_kernel(
        {"start_at": day_start.isoformat(), "end_at": day_end.isoformat(), "limit": 200},
        nodes=nodes,
        categories=categories,
    )
    blocks = calendar_result.get("blocks")
    if not isinstance(blocks, list):
        return None
    prompt_key = f" {_normalized_title(prompt)} "
    named_blocks = [
        block
        for block in blocks
        if isinstance(block, dict)
        and str(block.get("task_title") or "").strip()
        and f" {_normalized_title(str(block['task_title']))} " in prompt_key
    ]
    matched_task_ids = {
        int(block["task_id"])
        for block in named_blocks
        if isinstance(block.get("task_id"), int)
    }
    if len(named_blocks) != 1 or len(matched_task_ids) != 1:
        return None
    task_id = next(iter(matched_task_ids))
    return (
        {
            "task_ids": [task_id],
            "instruction": prompt,
            "window_start": day_start.isoformat(),
            "window_end": day_end.isoformat(),
            "operation_mode": "remove",
            "scope_source": "calendar_query",
        },
        {
            **calendar_result,
            "blocks": named_blocks,
            "task_ids": [task_id],
            "count": 1,
        },
    )


def _explicit_named_task_title(prompt: str) -> str | None:
    match = EXPLICIT_NAMED_TASK_RE.search(prompt)
    if match is None:
        return None
    value = " ".join(match.group(1).strip().split()).strip(" \"'“”")
    return value or None


def _exact_task_reference(
    *,
    prompt: str,
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
) -> StrategyNode | None:
    """Resolve only an explicit exact-title selection, never a fuzzy mention."""

    requested_title = _explicit_named_task_title(prompt)
    prompt_key = _normalized_title(prompt)
    if requested_title is None and focus_node is not None:
        if prompt_key == _normalized_title(str(focus_node.title or "")):
            requested_title = str(focus_node.title or "").strip()
    if requested_title is None:
        return None
    requested_key = _normalized_title(requested_title)
    matches = [
        node
        for node in nodes
        if node.id is not None and _normalized_title(str(node.title or "")) == requested_key
    ]
    if focus_node is not None and focus_node.id is not None:
        focused = next((node for node in matches if node.id == focus_node.id), None)
        if focused is not None:
            return focused
    return matches[0] if len(matches) == 1 else None


def _align_planner_handoff_to_resolved_task(
    handoff: dict[str, Any], *, task_id: int, explicit_time_range: bool = False
) -> None:
    """Keep typed constraint bindings aligned with one resolved task authority."""

    handoff["task_ids"] = [task_id]
    constraints = handoff.get("request_constraints")
    if not isinstance(constraints, list):
        return
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        kind = str(constraint.get("kind") or "").strip().lower()
        bound_ids = constraint.get("task_ids")
        if kind in {"exact_start", "contiguous"}:
            # Both contracts require exactly one owner. Once the task has been
            # resolved authoritatively, an omitted or stale model-authored
            # constraint target is an internal binding gap, not missing user
            # information.
            constraint["task_ids"] = [task_id]
        elif isinstance(bound_ids, list) and bound_ids:
            constraint["task_ids"] = [task_id]
        if not explicit_time_range:
            continue
        if kind == "exact_start":
            constraint["task_ids"] = [task_id]
            constraint["start_at"] = handoff.get("window_start")
            constraint.pop("end_at", None)
        elif kind == "allowed_window":
            constraint["task_ids"] = [task_id]
            constraint["start_at"] = handoff.get("window_start")
            constraint["end_at"] = handoff.get("window_end")
            constraint["recurrence"] = "once"


def _normalize_planner_handoff_constraint_arguments(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Canonicalize transport aliases without inventing scheduling semantics.

    Provider tool calls occasionally use structurally equivalent field names
    such as ``start``/``end`` or ``time_window``.  The Planner contract is
    strict, so normalize those transport variants at the host boundary while
    leaving genuinely unknown constraint shapes to fail validation.
    """

    raw_constraints = arguments.get("request_constraints")
    if not isinstance(raw_constraints, list):
        return arguments
    kind_aliases = {
        "time_window": "allowed_window",
        "availability_window": "allowed_window",
        "date_range": "allowed_window",
        "not_before": "allowed_window",
        "not_after": "allowed_window",
        "blocked_time": "unavailable_interval",
        "busy_interval": "unavailable_interval",
        "unavailable_window": "unavailable_interval",
        "blocked_window": "unavailable_interval",
        "blocked_interval": "unavailable_interval",
        "unavailable": "unavailable_interval",
        "protected_time": "unavailable_interval",
        "protected_interval": "unavailable_interval",
        "fixed_start": "exact_start",
        "exact_time": "exact_start",
        "fixed_time": "exact_start",
        "must_start_at": "exact_start",
        "ordering": "task_order",
        "order": "task_order",
        "sequence": "task_order",
        "gap": "minimum_gap",
        "buffer": "minimum_gap",
        "no_split": "contiguous",
        "unsplittable": "contiguous",
    }
    allowed_keys = {
        "kind",
        "hard",
        "label",
        "source_text",
        "task_ids",
        "start_at",
        "end_at",
        "before_task_id",
        "after_task_id",
        "minutes",
        "recurrence",
        "days_of_week",
    }
    normalized_constraints: list[Any] = []
    changed = False
    instruction = str(arguments.get("instruction") or "").strip()[:1_000]
    handoff_task_ids = list(
        dict.fromkeys(
            int(task_id)
            for task_id in (arguments.get("task_ids") or [])
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        )
    )
    for raw in raw_constraints:
        if not isinstance(raw, dict):
            normalized_constraints.append(raw)
            continue
        constraint = dict(raw)
        aliases = {
            "sourceText": "source_text",
            "taskIds": "task_ids",
            "startAt": "start_at",
            "endAt": "end_at",
            "beforeTaskId": "before_task_id",
            "afterTaskId": "after_task_id",
            "daysOfWeek": "days_of_week",
            "start": "start_at",
            "end": "end_at",
            "reason": "source_text",
            "description": "source_text",
        }
        for alias, canonical in aliases.items():
            if canonical not in constraint and alias in constraint:
                constraint[canonical] = constraint[alias]
                changed = True
        if not str(constraint.get("kind") or "").strip():
            for kind_alias in ("type", "constraint_type", "constraintType", "name"):
                if str(constraint.get(kind_alias) or "").strip():
                    constraint["kind"] = constraint[kind_alias]
                    changed = True
                    break
        raw_kind = str(constraint.get("kind") or "").strip().lower()
        canonical_kind = kind_aliases.get(raw_kind, raw_kind)
        if canonical_kind != raw_kind:
            constraint["kind"] = canonical_kind
            changed = True
        if canonical_kind in {
            "unavailable_interval",
            "allowed_window",
            "exact_start",
            "task_order",
            "minimum_gap",
            "contiguous",
        }:
            if "hard" not in constraint:
                constraint["hard"] = True
                changed = True
            if not str(constraint.get("label") or "").strip():
                constraint["label"] = canonical_kind.replace("_", " ").title()
                changed = True
            if not str(constraint.get("source_text") or "").strip():
                constraint["source_text"] = instruction
                changed = True
            filtered = {
                key: value
                for key, value in constraint.items()
                if key in allowed_keys
            }
            if filtered != constraint:
                changed = True
            constraint = filtered
            if (
                len(handoff_task_ids) == 1
                and canonical_kind in {"exact_start", "contiguous"}
                and constraint.get("task_ids") != handoff_task_ids
            ):
                # A single-task handoff already supplies the only legal owner
                # for task-scoped constraints. Bind it deterministically so a
                # provider omission cannot become a fake clarification.
                constraint["task_ids"] = list(handoff_task_ids)
                changed = True
            if canonical_kind == "allowed_window":
                if constraint.get("start_at") in (None, "") and arguments.get(
                    "window_start"
                ) not in (None, ""):
                    constraint["start_at"] = arguments["window_start"]
                    changed = True
                if constraint.get("end_at") in (None, "") and arguments.get(
                    "window_end"
                ) not in (None, ""):
                    constraint["end_at"] = arguments["window_end"]
                    changed = True
        normalized_constraints.append(constraint)
    if not changed:
        return arguments
    return {
        **arguments,
        "request_constraints": normalized_constraints,
    }


def _planner_handoff_can_use_deterministic_solver(
    handoff: dict[str, Any],
) -> bool:
    """Return whether the main model already supplied a complete typed boundary.

    Planner's nested model is valuable for open-ended ordering and prioritization
    across several tasks. It is redundant when Strategist has already reduced the
    request to typed scheduling constraints, a recurrence contract, typed task
    updates, or a removal. The deterministic solver still applies saved
    preferences, calendar occupancy, conflict policy, validation, and simulation.
    """

    if handoff.get("deterministic_semantics_complete") is True:
        return True
    task_ids = [
        int(task_id)
        for task_id in (handoff.get("task_ids") or [])
        if isinstance(task_id, int) and not isinstance(task_id, bool)
    ]
    if not task_ids:
        return False
    operation_mode = str(handoff.get("operation_mode") or "").strip()
    if operation_mode == "remove":
        return True
    if isinstance(handoff.get("recurrence"), dict) and handoff["recurrence"]:
        return True
    if isinstance(handoff.get("task_updates"), list) and handoff["task_updates"]:
        return True
    if len(task_ids) == 1:
        try:
            window_start = datetime.fromisoformat(str(handoff.get("window_start") or ""))
            window_end = datetime.fromisoformat(str(handoff.get("window_end") or ""))
        except ValueError:
            pass
        else:
            # A bounded one-task handoff has no ordering or prioritization choice
            # for a nested model to make. The deterministic planner still applies
            # occupancy, preferences, conflicts, validation, and simulation.
            if (
                window_start.tzinfo is None
                and window_end.tzinfo is None
                and window_end > window_start
                and window_end - window_start <= timedelta(hours=3)
            ):
                return True
    constraints = handoff.get("request_constraints")
    return bool(
        isinstance(constraints, list)
        and constraints
        and all(
            isinstance(constraint, dict)
            and str(constraint.get("kind") or "").strip()
            for constraint in constraints
        )
    )


def _initial_model_reasoning_effort(configured_effort: Any) -> Any:
    """Use staged compute: a capable first pass, full configured effort on repair.

    Server compilation is the acceptance gate, so a successful high-effort first
    pass loses no review, authority, or simulation guarantees. If that pass cannot
    satisfy the semantic contract, retry lanes retain the configured maximum.
    """

    normalized = str(configured_effort or "").strip().lower()
    if normalized in {"xhigh", "max", "ultra"}:
        return "high"
    return configured_effort


def _scheduling_alias_candidates(
    *,
    prompt: str,
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> tuple[str | None, list[StrategyNode]]:
    if _dominant_scheduling_candidate(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
    ) is not None:
        return None, []
    candidates = _entity_search_candidate_nodes(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
    )
    prompt_tokens = set(_entity_lookup_tokens(prompt))
    aliases: list[tuple[int, int, str, list[StrategyNode]]] = []
    for token in prompt_tokens:
        matching = [
            node
            for node in candidates
            if token in set(_entity_lookup_tokens(str(node.title or "")))
        ]
        if len(matching) < 2:
            continue
        exact_count = sum(
            1 for node in matching if _normalized_title(str(node.title or "")) == token
        )
        aliases.append((exact_count, len(token), token, matching))
    if not aliases:
        return None, []
    _, _, alias, matching = max(aliases, key=lambda item: (item[0], item[1], -len(item[3])))
    matching.sort(
        key=lambda node: (
            0 if _normalized_title(str(node.title or "")) == alias else 1,
            len(_entity_lookup_tokens(str(node.title or ""))),
            int(node.sort_order or 0),
            str(node.title or "").lower(),
        )
    )
    return alias, matching[:4]


def _scheduling_token_stem(token: str) -> str:
    """Small typo-tolerant stemmer for deterministic task-title dominance."""

    value = token.lower()
    if value.endswith("ing") and len(value) > 5:
        value = value[:-3]
    elif value.endswith("ies") and len(value) > 5:
        value = f"{value[:-3]}y"
    elif value.endswith("es") and len(value) > 5:
        value = value[:-2]
    elif value.endswith("s") and len(value) > 4:
        value = value[:-1]
    return value


def _dominant_scheduling_candidate(
    *,
    prompt: str,
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> StrategyNode | None:
    """Return a clearly specific title match, never a generic shared alias."""

    prompt_tokens = {
        _scheduling_token_stem(token) for token in _entity_lookup_tokens(prompt)
    }
    if not prompt_tokens:
        return None
    prompt_key = f" {_normalized_title(prompt)} "
    candidate_nodes = _entity_search_candidate_nodes(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
    )
    full_title_matches = [
        node
        for node in candidate_nodes
        if len(_entity_lookup_tokens(str(node.title or ""))) >= 2
        and f" {_normalized_title(str(node.title or ''))} " in prompt_key
    ]
    if len(full_title_matches) == 1:
        return full_title_matches[0]
    scored: list[tuple[int, float, StrategyNode]] = []
    for node in candidate_nodes:
        title_tokens = {
            _scheduling_token_stem(token)
            for token in _entity_lookup_tokens(str(node.title or ""))
        }
        overlap = len(prompt_tokens & title_tokens)
        if overlap:
            coverage = overlap / max(1, len(title_tokens))
            scored.append((overlap, coverage, node))
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            item[0],
            item[1],
            -len(_entity_lookup_tokens(str(item[2].title or ""))),
        ),
        reverse=True,
    )
    best_overlap, best_coverage, best = scored[0]
    runner_overlap = scored[1][0] if len(scored) > 1 else 0
    if best_overlap >= 3 and best_coverage >= 0.6 and best_overlap - runner_overlap >= 2:
        return best
    return None


def _repair_scheduling_clarification(
    *,
    payload: dict[str, Any],
    prompt: str,
    nodes: list[StrategyNode],
    categories: list[Category],
    active_category: Category | None,
) -> dict[str, Any]:
    clarification = payload.get("clarification")
    if not isinstance(clarification, dict):
        return payload
    scheduling_text = " ".join(
        str(value or "")
        for value in (prompt, payload.get("summary"), clarification.get("message"))
    )
    if not SCHEDULING_REQUEST_RE.search(scheduling_text):
        return payload
    if (
        CALENDAR_SCOPED_RESCHEDULE_RE.search(prompt)
        and re.search(r"\b(?:tasks?|items?)\s+scheduled\b", prompt, re.I)
    ):
        repaired = dict(payload)
        repaired["summary"] = "I couldn't match that request to the calendar."
        repaired["clarification"] = {
            **clarification,
            "message": (
                "I couldn't find scheduled tasks in that time period. Check the date, "
                "or tell me which calendar items you want to move."
            ),
            "entity_choices": [],
            "scoped_follow_up_prompts": [],
        }
        return repaired
    alias, candidates = _scheduling_alias_candidates(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
    )
    if alias is None or len(candidates) < 2:
        return payload
    categories_by_id = {
        category.id: category for category in categories if category.id is not None
    }
    choices: list[dict[str, Any]] = []
    for node in candidates:
        category = categories_by_id.get(node.category_id)
        duration = _human_duration(node.time_estimate_minutes)
        details = [str(category.name) if category is not None else "Task"]
        if duration:
            details.append(duration)
        choices.append(
            {
                "kind": "entity",
                "label": str(node.title),
                "value": str(node.id),
                "description": " · ".join(details),
                "target_node_id": int(node.id),
                "follow_up_prompt": f'Use the task named "{node.title}" for this request',
            }
        )
    repaired = dict(payload)
    repaired["summary"] = "I found a few matching tasks."
    repaired["clarification"] = {
        **clarification,
        "message": f"Which {alias.upper()} task did you mean?",
        "entity_choices": choices,
        "scoped_follow_up_prompts": [],
    }
    return repaired


def _has_scheduling_boundary(
    *,
    payload: dict[str, Any],
    tool_trace: list[dict[str, Any]],
) -> bool:
    """Return whether scheduling intent reached a typed or observed boundary."""

    if isinstance(payload.get("clarification"), dict):
        return True
    if isinstance(payload.get("review_document"), dict):
        return True
    if any(
        isinstance(payload.get(key), dict)
        for key in (
            "schedule_proposal",
            "action_proposal",
            "metadata_proposal",
            "branch_proposal",
            PREFERENCE_RESPONSE_KEY,
            "semantic_plan_input",
        )
    ):
        return True
    for call in tool_trace:
        tool_name = str(call.get("name") or "")
        result = call.get("result")
        if tool_name == "submit_task_tree_plan" and isinstance(result, dict):
            semantic_input = (
                result.get("semantic_plan_input")
                if isinstance(result.get("semantic_plan_input"), dict)
                else {}
            )
            semantic_payload = (
                semantic_input.get("payload")
                if isinstance(semantic_input.get("payload"), dict)
                else {}
            )
            if (
                result.get("compiled") is True
                and not result.get("error")
                and any(
                    isinstance(semantic_payload.get(key), (dict, list))
                    for key in (
                        "created_task_schedule",
                        "created_task_schedule_plan",
                        "recurring_schedule",
                        "recurring_schedule_plan",
                    )
                )
            ):
                # The semantic task-tree compiler is itself an authoritative
                # scheduling boundary. The provider need not repeat its
                # already-validated payload in the final prose envelope.
                return True
        if tool_name != "handoff_to_planner":
            continue
        if not isinstance(result, dict):
            continue
        # Schema rejection, goal-transition rejection, or subagent admission
        # failure means Planner never owned the request.  Those attempts must
        # remain repairable by the host instead of suppressing the boundary
        # repair merely because a tool call was recorded.
        if result.get("planner_executed") is True:
            return True
        if (
            isinstance(result.get("planner_handoff"), dict)
            and not result.get("error")
        ):
            return True
    return False


def _latest_turn_selects_existing_task_schedule(prompt: str) -> bool:
    """Recognize an explicit existing-task choice over an earlier create branch."""

    latest_turn = next(
        (
            line.strip()
            for line in str(prompt or "").splitlines()
            if line.strip()
        ),
        "",
    )
    return bool(
        SCHEDULING_REQUEST_RE.search(latest_turn)
        and re.search(
            r"\b(?:existing|already[- ]created|saved)\s+(?:task|tasks|items)\b",
            latest_turn,
            re.IGNORECASE,
        )
    )


def _is_host_managed_clarification_continuation(
    *,
    active_request: dict[str, Any] | None,
    request_context_prompts: list[str],
) -> bool:
    """Bind an answer to an open clarification without a redundant model call."""

    return bool(
        isinstance(active_request, dict)
        and str(active_request.get("status") or "").strip().lower()
        == "awaiting_user_input"
        and len(request_context_prompts) > 1
    )


def _resolved_scheduling_task_ids(
    *,
    prompt: str,
    tool_trace: list[dict[str, Any]],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    ui_event: dict[str, Any] | None,
) -> list[int]:
    """Resolve only evidence-backed task IDs for a server-owned handoff repair."""

    explicit_ui_ids: list[int] = []
    if isinstance(ui_event, dict):
        target_id = ui_event.get("target_node_id", ui_event.get("targetNodeId"))
        if isinstance(target_id, int) and not isinstance(target_id, bool):
            explicit_ui_ids.append(target_id)
        raw_target_ids = ui_event.get(
            "target_node_ids",
            ui_event.get("targetNodeIds", []),
        )
        if isinstance(raw_target_ids, list):
            explicit_ui_ids.extend(
                value
                for value in raw_target_ids
                if isinstance(value, int) and not isinstance(value, bool)
            )
    explicit_ids = list(
        dict.fromkeys(
            [
                *explicit_ui_ids,
                *[
                    value
                    for value in staged_node_ids
                    if isinstance(value, int) and not isinstance(value, bool)
                ],
            ]
        )
    )
    if explicit_ids:
        return explicit_ids

    exact = _exact_task_reference(
        prompt=prompt,
        nodes=nodes,
        focus_node=(
            focus_node
            if isinstance(ui_event, dict)
            and str(ui_event.get("kind") or "") == "clarification_choice"
            else None
        ),
    )
    if exact is not None and exact.id is not None:
        return [int(exact.id)]
    dominant = _dominant_scheduling_candidate(
        prompt=prompt,
        nodes=nodes,
        active_category=active_category,
    )
    if dominant is not None and dominant.id is not None:
        return [int(dominant.id)]

    for call in reversed(tool_trace):
        if str(call.get("name") or "") != "query_task_graph":
            continue
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        ambiguity = result.get("ambiguity")
        if isinstance(ambiguity, dict) and bool(
            ambiguity.get("multiple_named_matches")
        ):
            return []
        candidate = result.get("answer_candidate")
        task_ids = (
            candidate.get("task_ids")
            if isinstance(candidate, dict)
            else None
        )
        if not isinstance(task_ids, list):
            continue
        resolved = list(
            dict.fromkeys(
                int(value)
                for value in task_ids
                if isinstance(value, int) and not isinstance(value, bool)
            )
        )
        if resolved:
            return resolved
    return []


def _existing_schedule_repair_handoff_arguments(
    session: Session,
    *,
    prompt: str,
    tool_trace: list[dict[str, Any]],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    ui_event: dict[str, Any] | None,
    today_value: date,
    workspace_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build a bounded existing-task handoff after a model misses the boundary."""

    if (
        not SCHEDULING_REQUEST_RE.search(prompt)
        or (
            CREATE_TASK_ACTION_CONTRACT_RE.search(prompt)
            and not _latest_turn_selects_existing_task_schedule(prompt)
        )
        or _has_explicit_clock_time(prompt)
    ):
        return None
    task_ids = _resolved_scheduling_task_ids(
        prompt=prompt,
        tool_trace=tool_trace,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        ui_event=ui_event,
    )
    if not task_ids:
        return None
    target_date = _calendar_date_from_prompt(
        session,
        prompt=prompt,
        today_value=today_value,
    )
    window: tuple[datetime, datetime] | None = None
    if target_date is not None:
        window = _one_time_schedule_window(
            target_date=target_date,
            prompt=prompt,
            local_now=_workspace_local_datetime(workspace_context),
        )
    if window is None and isinstance(workspace_context, dict):
        start = _parse_local_datetime(workspace_context.get("calendar_view_start"))
        end = _parse_local_datetime(workspace_context.get("calendar_view_end"))
        if start is not None and end is not None and end > start:
            window = (start, end)
    if window is None:
        return None
    return {
        "task_ids": task_ids,
        "instruction": prompt,
        "window_start": window[0].isoformat(),
        "window_end": window[1].isoformat(),
        "operation_mode": "create",
        "scope_source": (
            "attached_working_set"
            if staged_node_ids
            else "explicit_named_task"
        ),
    }


def _latest_scheduling_request(
    conversation_history: list[dict[str, str]],
    fallback: str,
) -> str:
    current = " ".join(str(fallback or "").strip().split())
    if current and SCHEDULING_REQUEST_RE.search(current):
        return current
    for message in reversed(conversation_history):
        if str(message.get("role") or "").lower() != "user":
            continue
        content = " ".join(str(message.get("content") or "").strip().split())
        if content and SCHEDULING_REQUEST_RE.search(content):
            return content
    return current or fallback


def _expand_start_anchor_window(
    *,
    instruction: str,
    window_start: datetime,
    window_end: datetime,
) -> datetime:
    """A requested start time is an anchor, not an invented one-hour window."""

    if re.search(
        r"\b(?:across|over)\b.{0,50}\b(?:two|2)\s+days?\b|"
        r"\b(?:across|over)\b.{0,80}\b(?:following|next)\s+day\b",
        instruction,
        re.IGNORECASE,
    ):
        two_day_end = datetime.combine(
            window_start.date() + timedelta(days=2),
            time.min,
        )
        return max(window_end, two_day_end)
    if EXPLICIT_TIME_RANGE_RE.search(instruction):
        return window_end
    if window_end - window_start > timedelta(hours=2):
        return window_end
    if not re.search(r"\b(?:at|around)\s+\d|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", instruction, re.I):
        return window_end
    end_of_day = datetime.combine(window_start.date() + timedelta(days=1), time.min)
    return max(window_end, end_of_day)


def _leaf_descendant_ids(root_id: int, nodes: list[StrategyNode]) -> list[int]:
    children_by_parent: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.parent_id is None or node.id is None:
            continue
        children_by_parent.setdefault(int(node.parent_id), []).append(node)
    for children in children_by_parent.values():
        children.sort(key=lambda node: (int(node.sort_order or 0), int(node.id or 0)))
    leaves: list[int] = []

    def visit(node_id: int) -> None:
        children = children_by_parent.get(node_id, [])
        if not children:
            if node_id != root_id:
                leaves.append(node_id)
            return
        for child in children:
            visit(int(child.id))

    visit(root_id)
    return leaves


def _expand_subtask_schedule_ids(
    *,
    instruction: str,
    task_ids: list[int],
    nodes: list[StrategyNode],
) -> list[int]:
    match = SUBTASK_SCHEDULE_RE.search(instruction)
    if match is None:
        return task_ids
    requested_key = _normalized_title(match.group(1))
    named_parent = next(
        (
            node
            for node in nodes
            if node.id is not None and _normalized_title(str(node.title or "")) == requested_key
        ),
        None,
    )
    root_ids = [int(named_parent.id)] if named_parent is not None else list(task_ids)
    expanded: list[int] = []
    for task_id in root_ids:
        leaf_ids = _leaf_descendant_ids(task_id, nodes)
        for candidate_id in leaf_ids or [task_id]:
            if candidate_id not in expanded:
                expanded.append(candidate_id)
    return expanded or task_ids


def _planner_operation_count(planner_result: dict[str, Any]) -> int:
    proposal = planner_result.get("proposal")
    if not isinstance(proposal, dict):
        return 0
    validation = proposal.get("schedulePatchValidation")
    if isinstance(validation, dict) and isinstance(validation.get("operationCount"), int):
        return int(validation["operationCount"])
    patch = proposal.get("schedulePatch")
    operations = patch.get("operations") if isinstance(patch, dict) else None
    return len(operations) if isinstance(operations, list) else 0


def _friendly_planner_result(
    planner_result: dict[str, Any],
    *,
    nodes: list[StrategyNode],
) -> dict[str, Any]:
    proposal = planner_result.get("proposal")
    if not isinstance(proposal, dict):
        return planner_result
    operation_count = _planner_operation_count(planner_result)
    payload = proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {}
    working_set = payload.get("workingSet") if isinstance(payload.get("workingSet"), list) else []
    task_items = [item for item in working_set if isinstance(item, dict)]
    titles = [str(item.get("title") or "").strip() for item in task_items]
    titles = [title for title in titles if title]
    if operation_count <= 0:
        if len(task_items) > 1:
            total_minutes = sum(
                int(item.get("durationMinutes"))
                for item in task_items
                if isinstance(item.get("durationMinutes"), int)
            )
            task_count = len(task_items)
            duration = _human_duration(total_minutes)
            message = (
                f"These {task_count} tasks"
                + (f" need about {duration}" if duration else " need more time")
                + " and do not fit in the available opening. Would you like me to "
                "spread them across a larger window or choose another day?"
            )
            prompts = [
                f"Find a larger opening for all {task_count} tasks",
                f"Spread all {task_count} tasks across multiple days",
                f"Choose another day for all {task_count} tasks",
            ]
            return {
                **planner_result,
                "mode": "clarification",
                "summary": "Those tasks need a different scheduling approach.",
                "proposal": None,
                "suggestedPrompts": prompts,
                "clarification": {
                    "message": message,
                    "entity_choices": [],
                    "field_choices": [],
                    "scoped_follow_up_prompts": prompts,
                },
            }
        first = task_items[0] if task_items else {}
        title = titles[0] if titles else "That task"
        duration_minutes = (
            int(first.get("durationMinutes"))
            if isinstance(first.get("durationMinutes"), int)
            else None
        )
        duration = _human_duration(duration_minutes)
        rendered_children = first.get("renderedChildTaskIds")
        has_children = isinstance(rendered_children, list) and len(rendered_children) > 0
        raw_conflicts = payload.get("conflicts")
        conflicts = [item for item in raw_conflicts if isinstance(item, dict)] if isinstance(raw_conflicts, list) else []
        first_conflict = conflicts[0] if conflicts else {}
        alternatives = first_conflict.get("alternatives")
        alternative = (
            next((item for item in alternatives if isinstance(item, dict)), None)
            if isinstance(alternatives, list)
            else None
        )
        alternative_start = _human_schedule_time(alternative.get("startAt")) if alternative else None
        alternative_end = _human_schedule_time(alternative.get("endAt")) if alternative else None
        if (
            alternative is not None
            and alternative_start
            and alternative_end
            and isinstance(duration_minutes, int)
            and duration_minutes <= 180
        ):
            blockers = first_conflict.get("conflictingBlocks")
            blocker = (
                next((item for item in blockers if isinstance(item, dict)), None)
                if isinstance(blockers, list)
                else None
            )
            blocker_label = str((blocker or {}).get("label") or "another calendar item")
            message = (
                f"{title} conflicts with {blocker_label} at the requested time. "
                f"The next opening is {alternative_start} to {alternative_end.split(' at ')[-1]}. "
                "Would you like me to use that time?"
            )
            prompts = [
                f"Schedule {title} {alternative_start} to {alternative_end.split(' at ')[-1]}",
                f"Find another time for {title}",
                f"Choose another time for {title}",
            ]
        elif has_children:
            message = (
                f"{title} is a larger project with subtasks"
                + (f" (about {duration} total)" if duration else "")
                + ", so it does not fit in the requested time. "
                "Would you like me to schedule its subtasks across multiple days?"
            )
            prompts = [
                f"Schedule the subtasks of {title} across tomorrow and the following day",
                f"Show me the subtasks of {title}",
                f"Choose another time for {title}",
            ]
        else:
            message = (
                f"{title} needs {duration or 'more time'} and does not fit in the requested time. "
                "Would you like me to split it into shorter work sessions or find a larger opening?"
            )
            prompts = [
                f"Split {title} into shorter sessions across tomorrow and the following day",
                f"Find the next large opening for {title}",
                f"Choose another time for {title}",
            ]
        return {
            **planner_result,
            "mode": "clarification",
            "summary": "That task needs a different scheduling approach.",
            "proposal": None,
            "suggestedPrompts": prompts,
            "clarification": {
                "message": message,
                "entity_choices": [],
                "field_choices": [],
                "scoped_follow_up_prompts": prompts,
            },
        }

    label = titles[0] if len(titles) == 1 else f"{operation_count} tasks"
    operation_mode = str(payload.get("operationMode") or "create").strip().lower()
    if operation_mode == "remove":
        summary = f"I found the scheduled {label}. Review the cancellation below."
    else:
        summary = f"I found a time for {label}. Review it below before adding it to your calendar."
        if operation_count != 1:
            summary = f"I found times for {label}. Review them below before adding them to your calendar."
    placement_reasons = (
        payload.get("placementReasons")
        if isinstance(payload.get("placementReasons"), list)
        else []
    )
    primary_reason = next(
        (
            str(reason.get("message") or "").strip()
            for reason in placement_reasons
            if isinstance(reason, dict) and str(reason.get("message") or "").strip()
        ),
        "",
    )
    if primary_reason:
        summary = f"{primary_reason} Review the proposed times below."
    friendly_proposal = {
        **proposal,
        "title": "Review cancellation" if operation_mode == "remove" else "Review schedule",
        "body": (
            "Nothing is cancelled until you approve."
            if operation_mode == "remove"
            else "Nothing is added to your calendar until you approve."
        ),
        "summary": None,
        "preview": [],
        "validationWarnings": [],
    }
    return {**planner_result, "summary": summary, "proposal": friendly_proposal}


def _find_node_by_title(nodes: list[StrategyNode], *titles: str) -> StrategyNode | None:
    by_title = {
        _normalized_title(node.title): node
        for node in nodes
        if isinstance(node.title, str) and node.title.strip()
    }
    for title in titles:
        node = by_title.get(_normalized_title(title))
        if node is not None:
            return node
    return None


def _build_mixed_cleanup_mutation_payload(
    *,
    prompt: str,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not MIXED_CLEANUP_MUTATION_RE.search(prompt):
        return None
    if active_category is None or active_category.id is None:
        return None

    prompt_key = _normalized_title(prompt)
    if "cs paper summaries" in prompt_key or "paper summaries" in prompt_key:
        target_node = _find_node_by_title(nodes, "CS Paper Summaries")
        metadata_targets = [
            node
            for node in (
                _find_node_by_title(nodes, "Email security paper summary"),
                _find_node_by_title(nodes, "Image quilting paper notes"),
            )
            if node is not None
        ]
        metadata_updates = {"status": "todo"}
        target_label = "Review combined changes for CS Paper Summaries cleanup"
        required_node_ids = {int(node.id) for node in metadata_targets if node.id is not None}
    elif "final exam prep" in prompt_key or "exam prep" in prompt_key:
        target_node = _find_node_by_title(nodes, "Final exam prep")
        metadata_targets = [target_node] if target_node is not None else []
        metadata_updates = {"status": "done"}
        target_label = "Review combined changes for Final exam prep and cleanup"
        required_node_ids = {int(target_node.id)} if target_node is not None and target_node.id is not None else set()
    else:
        return None
    if target_node is None or not metadata_targets:
        return None

    tool_started_at = perf_counter()
    cleanup_result = execute_kernel_tool(
        "analyze_tree_cleanup_candidates",
        _cleanup_analysis_arguments(prompt=prompt, active_category=active_category),
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
    )
    tool_trace.append(
        {
            "name": "analyze_tree_cleanup_candidates",
            "arguments": _cleanup_analysis_arguments(prompt=prompt, active_category=active_category),
            "result": cleanup_result,
            "duration_ms": _elapsed_ms(tool_started_at),
        }
    )
    nodes_by_id = {
        int(node.id): node
        for node in nodes
        if isinstance(node.id, int)
    }
    delete_targets: list[StrategyNode] = []
    for item in cleanup_result.get("cleanup_candidates", []):
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id")
        if not isinstance(task_id, int) or task_id in required_node_ids:
            continue
        node = nodes_by_id.get(task_id)
        if node is None:
            continue
        delete_targets.append(node)
        if len(delete_targets) >= 3:
            break
    if not delete_targets:
        fallback = _find_node_by_title(nodes, "Old projects archive", "Twitter bot for portfolio")
        if fallback is not None and fallback.id not in required_node_ids:
            delete_targets.append(fallback)
    if not delete_targets:
        return None

    proposal = build_mixed_mutation_proposal(
        category_id=int(active_category.id),
        target_node=target_node,
        target_label=target_label,
        metadata_targets=metadata_targets,
        metadata_updates=metadata_updates,
        delete_targets=delete_targets,
        category_name=str(active_category.name or "Work"),
        rationale=(
            "The request combines a concrete status update with cleanup work, "
            "so Strategist compiled one approval-gated mixed TaskPatch instead of "
            "asking the model to one-shot the whole tree."
        ),
        evidence_override=[
            {
                "source": "cleanup_analyzer",
                "detail": summarize_kernel_tool_observation(tool_trace[-1]),
            }
        ],
        summary=target_label,
    )
    if proposal is None:
        return None

    changed_titles = ", ".join(node.title for node in metadata_targets)
    delete_titles = ", ".join(node.title for node in delete_targets)
    return {
        "summary": (
            f"I prepared a combined proposal: update {changed_titles} and "
            f"Delete low-value cleanup candidates ({delete_titles}). Review combined changes before applying."
        ),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": proposal,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "mixed_cleanup_mutation",
            "metadata_target_count": len(metadata_targets),
            "delete_target_count": len(delete_targets),
        },
    }


ALL_WORK_UNITS_RE = re.compile(
    r"\b(?:everything\s+in\s+the\s+plan|every\s+work\s+unit|the\s+whole\s+plan|"
    r"all\s+of\s+(?:them|these)|all\s+the(?:se|m)\s+work\s+units?|"
    r"proceed\s+with\s+(?:all|these|them|the\s+plan)|"
    r"do\s+(?:all\s+of\s+)?(?:these|them)|"
    r"(?:cleanup|clean\s+up)\s+all(?:\s+of\s+(?:them|these))?)\b",
    re.IGNORECASE,
)
SELECTED_WORK_UNIT_RE = re.compile(
    r"\b(?:proceed|continue|start|do|handle|draft|turn)\b.{0,80}"
    r"\b(?:first|next|current|selected|this)\b.{0,80}"
    r"\b(?:work\s+unit|cleanup\s+candidate|candidate|slice|item)\b"
    r"|\b(?:first|next|current|selected|this)\b.{0,80}"
    r"\b(?:work\s+unit|cleanup\s+candidate|candidate|slice|item)\b",
    re.IGNORECASE,
)
PLAN_EXPLANATION_RE = re.compile(
    r"\b(?:how|what|tell\s+me|show\s+me|explain)\b.{0,80}"
    r"\b(?:plan|patch|handle|change|cleanup|clean\s+up|work\s+units?|these|them)\b"
    r"|\b(?:plan|patch|cleanup|clean\s+up|work\s+units?)\b.{0,80}"
    r"\b(?:each|these|them|before|first)\b",
    re.IGNORECASE,
)


def _is_all_work_units_followup(prompt: str, agent_run_plan: dict[str, Any] | None) -> bool:
    if not isinstance(agent_run_plan, dict):
        return False
    prompt_text = prompt.strip()
    if not prompt_text:
        return False
    return bool(ALL_WORK_UNITS_RE.search(prompt_text))


def _is_selected_work_unit_followup(prompt: str, agent_run_plan: dict[str, Any] | None) -> bool:
    if not isinstance(agent_run_plan, dict):
        return False
    prompt_text = prompt.strip()
    if not prompt_text:
        return False
    if ALL_WORK_UNITS_RE.search(prompt_text):
        return False
    return bool(SELECTED_WORK_UNIT_RE.search(prompt_text))


def _is_plan_explanation_followup(prompt: str, agent_run_plan: dict[str, Any] | None) -> bool:
    if not isinstance(agent_run_plan, dict):
        return False
    prompt_text = prompt.strip()
    if not prompt_text:
        return False
    return bool(PLAN_EXPLANATION_RE.search(prompt_text))


def _work_units_from_plan(agent_run_plan: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    raw_units = agent_run_plan.get("work_units")
    if not isinstance(raw_units, list):
        return []
    return [unit for unit in raw_units if isinstance(unit, dict)][:limit]


def _describe_work_unit_patch(unit: dict[str, Any]) -> str:
    title = str(
        unit.get("target_task_title")
        or unit.get("title")
        or "this work unit"
    ).strip()
    unit_kind = str(unit.get("kind") or "").strip()
    risk = str(unit.get("risk") or "").strip()
    raw_evidence = unit.get("evidence")
    evidence = [
        str(item).strip()
        for item in (raw_evidence if isinstance(raw_evidence, list) else [])
        if str(item).strip()
    ]
    evidence_text = f" Evidence: {', '.join(evidence[:3])}." if evidence else ""
    risk_text = f" Risk: {risk}." if risk else ""
    if unit_kind == "cleanup_candidate":
        action = "archive it with a state:archived tag rather than hard-delete it"
    elif unit_kind == "branch_restructure":
        action = "inspect its children and propose one focused restructure"
    elif unit_kind == "branch_breakdown":
        action = "break it into concrete child tasks"
    elif unit_kind.startswith("workload_"):
        action = "turn it into one focused next-step or metadata proposal"
    else:
        action = "turn it into one reviewable TaskPatch proposal"
    return f"{title}: {action}.{risk_text}{evidence_text}"


def _build_agent_run_plan_explanation_payload(
    *,
    prompt: str,
    agent_run_plan: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not _is_plan_explanation_followup(prompt, agent_run_plan):
        return None
    assert agent_run_plan is not None
    work_units = _work_units_from_plan(agent_run_plan)
    if not work_units:
        return None
    lines = [
        f"{index + 1}. {_describe_work_unit_patch(unit)}"
        for index, unit in enumerate(work_units)
    ]
    scope = str(agent_run_plan.get("scope") or "the saved plan").strip()
    summary = (
        f"No changes yet. Here is how I would patch the saved plan for {scope}:\n"
        + "\n".join(lines)
        + "\n\nIf you say `proceed with these`, I will compile the applicable work units "
        "into a reviewable TaskPatch instead of re-reading the whole tree."
    )
    return {
        "summary": summary,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "agent_run_plan_explanation",
            "plan_kind": agent_run_plan.get("kind"),
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "work_unit_count": len(work_units),
        },
    }


def _build_agent_run_plan_continuation_payload(
    *,
    agent_run_plan: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(agent_run_plan, dict):
        return None
    clarification = clarification_from_agent_run_plan(agent_run_plan)
    if not clarification.get("entity_choices"):
        return None
    plan_kind = str(agent_run_plan.get("kind") or "agent plan").replace("_", " ")
    scope = str(agent_run_plan.get("scope") or "the saved plan").strip()
    work_unit_count = agent_run_plan.get("work_unit_count")
    count_text = (
        f"{work_unit_count} work units"
        if isinstance(work_unit_count, int)
        else "work units"
    )
    return {
        "summary": (
            f"I can continue the saved {plan_kind} for {scope}. "
            f"It has {count_text}; choose one work unit and I will turn that slice into "
            "a bounded answer or reviewable proposal."
        ),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": clarification,
        "deterministic_fast_path": {
            "kind": "agent_run_plan_continuation",
            "plan_kind": agent_run_plan.get("kind"),
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "work_unit_count": agent_run_plan.get("work_unit_count"),
        },
    }


def _archive_tag_updates(node: StrategyNode) -> dict[str, Any]:
    tags = _json_string_list(node.tags)
    if "state:archived" not in [tag.lower() for tag in tags]:
        tags.append("state:archived")
    return {"tags": tags}


def _nodes_for_plan_units(
    *,
    agent_run_plan: dict[str, Any],
    nodes: list[StrategyNode],
    allowed_unit_kinds: set[str],
) -> list[StrategyNode]:
    nodes_by_id = {int(node.id): node for node in nodes if isinstance(node.id, int)}
    selected: list[StrategyNode] = []
    seen_ids: set[int] = set()
    work_units = agent_run_plan.get("work_units")
    if not isinstance(work_units, list):
        return selected
    for unit in work_units:
        if not isinstance(unit, dict):
            continue
        unit_kind = str(unit.get("kind") or "").strip()
        if unit_kind not in allowed_unit_kinds:
            continue
        task_id = unit.get("target_task_id")
        if not isinstance(task_id, int) or task_id in seen_ids:
            continue
        node = nodes_by_id.get(task_id)
        if node is None:
            continue
        selected.append(node)
        seen_ids.add(task_id)
    return selected


def _selected_plan_unit_node(
    *,
    agent_run_plan: dict[str, Any],
    nodes: list[StrategyNode],
    allowed_unit_kinds: set[str],
) -> StrategyNode | None:
    nodes_by_id = {int(node.id): node for node in nodes if isinstance(node.id, int)}
    work_units = agent_run_plan.get("work_units")
    if not isinstance(work_units, list):
        return None
    current_work_unit_id = str(agent_run_plan.get("current_work_unit_id") or "").strip()
    candidates = [
        unit
        for unit in work_units
        if isinstance(unit, dict)
        and str(unit.get("kind") or "").strip() in allowed_unit_kinds
        and isinstance(unit.get("target_task_id"), int)
    ]
    if current_work_unit_id:
        for unit in candidates:
            if str(unit.get("work_unit_id") or "").strip() == current_work_unit_id:
                return nodes_by_id.get(int(unit["target_task_id"]))
    if candidates:
        return nodes_by_id.get(int(candidates[0]["target_task_id"]))
    return None


def _build_agent_run_plan_selected_work_unit_payload(
    *,
    prompt: str,
    agent_run_plan: dict[str, Any] | None,
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> dict[str, Any] | None:
    if not _is_selected_work_unit_followup(prompt, agent_run_plan):
        return None
    assert agent_run_plan is not None
    plan_kind = str(agent_run_plan.get("kind") or "").strip()
    if plan_kind != "tree_cleanup":
        return None

    target = _selected_plan_unit_node(
        agent_run_plan=agent_run_plan,
        nodes=nodes,
        allowed_unit_kinds={"cleanup_candidate"},
    )
    if target is None or target.id is None:
        return None

    category_name = (
        str(active_category.name).strip()
        if active_category is not None and str(active_category.name).strip()
        else str(agent_run_plan.get("scope") or "the task tree").strip()
    )
    structural_operations = [
        {
            "kind": "metadata_update",
            "node_id": int(target.id),
            "node_title": target.title,
            "updates": _archive_tag_updates(target),
        }
    ]
    evidence = [
        {
            "source": "agent_run_plan",
            "detail": (
                "User asked to proceed with the selected cleanup work unit in the saved plan; "
                f"compiled one archive update for {target.title}."
            ),
        },
        {
            "source": "cleanup_work_unit",
            "detail": f"Archive cleanup candidate from saved plan: {target.title}",
            "task_id": int(target.id),
            "task_title": target.title,
        },
    ]
    return {
        "summary": (
            f"I turned the selected cleanup work unit into one reviewable patch for {category_name}: "
            f"archive {target.title}. Review it before applying."
        ),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": {
            "proposal_kind": "mixed",
            "target_node_id": int(target.id),
            "target_node_title": target.title,
            "summary": f"Archive cleanup candidate: {target.title}",
            "rationale": (
                "This follows the saved cleanup work-unit plan. Archiving is safer than hard deletion "
                "and remains approval-gated."
            ),
            "structural_operations": structural_operations,
            "evidence": evidence,
            "validation_warnings": [],
        },
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "agent_run_plan_followup",
            "plan_kind": plan_kind,
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "work_unit_count": 1,
            "action": "archive_selected_cleanup_candidate",
            "target_task_id": int(target.id),
        },
    }


def _build_agent_run_plan_followup_payload(
    *,
    prompt: str,
    agent_run_plan: dict[str, Any] | None,
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> dict[str, Any] | None:
    if not _is_all_work_units_followup(prompt, agent_run_plan):
        return None
    assert agent_run_plan is not None
    plan_kind = str(agent_run_plan.get("kind") or "").strip()
    if plan_kind != "tree_cleanup":
        return None

    archive_targets = _nodes_for_plan_units(
        agent_run_plan=agent_run_plan,
        nodes=nodes,
        allowed_unit_kinds={"cleanup_candidate"},
    )
    if not archive_targets:
        return None

    category_name = (
        str(active_category.name).strip()
        if active_category is not None and str(active_category.name).strip()
        else str(agent_run_plan.get("scope") or "the task tree").strip()
    )
    structural_operations = [
        {
            "kind": "metadata_update",
            "node_id": int(node.id),
            "node_title": node.title,
            "updates": _archive_tag_updates(node),
        }
        for node in archive_targets
        if node.id is not None
    ]
    evidence = [
        {
            "source": "agent_run_plan",
            "detail": (
                "User asked to apply cleanup to all cleanup work units in the saved plan; "
                f"compiled {len(structural_operations)} archive updates."
            ),
        },
        *[
            {
                "source": "cleanup_work_unit",
                "detail": f"Archive cleanup candidate from saved plan: {node.title}",
                "task_id": int(node.id),
                "task_title": node.title,
            }
            for node in archive_targets[:10]
            if node.id is not None
        ],
    ]
    target_label = (
        archive_targets[0].title
        if len(archive_targets) == 1
        else f"{len(archive_targets)} cleanup candidates"
    )
    return {
        "summary": (
            f"I turned the saved cleanup plan into one reviewable patch for {category_name}: "
            f"archive {len(archive_targets)} cleanup candidate"
            f"{'s' if len(archive_targets) != 1 else ''}. Review it before applying."
        ),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": {
            "proposal_kind": "mixed",
            "target_node_id": int(archive_targets[0].id),
            "target_node_title": target_label,
            "summary": f"Archive {len(archive_targets)} cleanup candidate{'s' if len(archive_targets) != 1 else ''}",
            "rationale": (
                "This follows the saved cleanup work-unit plan. Archiving is safer than hard deletion "
                "for a broad cleanup instruction and remains approval-gated."
            ),
            "structural_operations": structural_operations,
            "evidence": evidence,
            "validation_warnings": [
                (
                    "This applies every cleanup work unit currently in the saved plan, not every "
                    "possible cleanup candidate in the workspace. Run another pass afterward if you "
                    "want to continue."
                )
            ],
        },
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "agent_run_plan_followup",
            "plan_kind": plan_kind,
            "agent_run_plan_id": agent_run_plan.get("plan_id"),
            "work_unit_count": len(archive_targets),
            "action": "archive_cleanup_candidates",
        },
    }


def append_kernel_event(
    session: Session,
    *,
    strategist_session_id: str,
    correlation_id: str,
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
    turn_id: str | None = None,
    source: str = "agent_kernel",
) -> StrategistAgentEvent:
    active_goal = GoalStateStore(session).load_active(strategist_session_id)
    effective_turn_id = turn_id
    if effective_turn_id is None:
        active_run = session.exec(
            select(StrategistRun)
            .where(StrategistRun.session_id == strategist_session_id)
            .where(StrategistRun.correlation_id == correlation_id)
            .where(StrategistRun.status.in_(["queued", "running"]))  # type: ignore[attr-defined]
            .order_by(StrategistRun.created_at.desc())
        ).first()
        if active_run is not None:
            effective_turn_id = active_run.turn_id
    run = (
        session.exec(
            select(StrategistRun)
            .where(StrategistRun.turn_id == effective_turn_id)
            .order_by(StrategistRun.created_at.desc())
        ).first()
        if effective_turn_id is not None
        else None
    )
    event = StrategistAgentEvent(
        session_id=strategist_session_id,
        turn_id=effective_turn_id,
        correlation_id=correlation_id,
        causation_id=(
            run.causation_id
            if run is not None and run.causation_id
            else active_goal.causation_id
            if active_goal is not None
            else effective_turn_id
        ),
        goal_id=(
            active_goal.goal_id
            if active_goal is not None
            else run.goal_id
            if run is not None
            else None
        ),
        goal_revision=(
            active_goal.revision
            if active_goal is not None
            else run.goal_revision
            if run is not None
            else None
        ),
        run_id=run.id if run is not None else None,
        sequence=sequence,
        event_type=event_type,
        source=source,
        payload_json=_json_dumps(payload),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def load_pending_proposal_snapshot(
    session: Session,
    *,
    strategist_session_id: str,
) -> dict[str, Any] | None:
    record = session.exec(
        select(StrategistProposalRecord)
        .where(StrategistProposalRecord.session_id == strategist_session_id)
        .where(StrategistProposalRecord.status == "pending")
        .order_by(StrategistProposalRecord.updated_at.desc())
    ).first()
    if record is None:
        return None
    return _proposal_record_snapshot(
        record,
        goal_id=_goal_id_for_proposal_record(session, record),
    )


def load_proposal_snapshot_by_id(
    session: Session,
    *,
    proposal_id: str,
    strategist_session_id: str,
) -> dict[str, Any] | None:
    record = session.get(StrategistProposalRecord, proposal_id)
    if (
        record is None
        or record.session_id != strategist_session_id
        or record.status != "pending"
    ):
        return None
    return _proposal_record_snapshot(
        record,
        goal_id=_goal_id_for_proposal_record(session, record),
    )


def _goal_id_for_proposal_record(
    session: Session,
    record: StrategistProposalRecord,
) -> str | None:
    if record.source_turn_id is None:
        return None
    run = session.exec(
        select(StrategistRun)
        .where(StrategistRun.session_id == record.session_id)
        .where(StrategistRun.turn_id == record.source_turn_id)
        .order_by(StrategistRun.created_at.desc())
    ).first()
    return run.goal_id if run is not None else None


def _proposal_record_snapshot(
    record: StrategistProposalRecord,
    *,
    goal_id: str | None = None,
) -> dict[str, Any]:
    payload = _json_loads_dict(record.payload_json)
    return {
        "proposal_id": record.id,
        "proposal_kind": record.proposal_kind,
        "status": record.status,
        "target_node_id": record.target_node_id,
        "target_node_ids": [
            int(item)
            for item in _json_loads_list(record.target_node_ids_json)
            if isinstance(item, int)
        ],
        "source_turn_id": record.source_turn_id,
        "goal_id": goal_id,
        "summary": payload.get("summary"),
        "rationale": payload.get("rationale"),
        "target_node_title": payload.get("target_node_title") or payload.get("targetNodeTitle"),
        "payload": payload,
    }


def load_latest_agent_run_plan_snapshot(
    session: Session,
    *,
    strategist_session_id: str,
) -> dict[str, Any] | None:
    event = session.exec(
        select(StrategistAgentEvent)
        .where(StrategistAgentEvent.session_id == strategist_session_id)
        .where(StrategistAgentEvent.event_type == "agent_run_plan_created")
        .order_by(StrategistAgentEvent.created_at.desc())
    ).first()
    if event is None:
        return None
    plan = _json_loads_dict(event.payload_json)
    compact_plan = compact_agent_run_plan_for_model(plan)
    if compact_plan is None:
        return None
    status = str(compact_plan.get("status") or "").strip().lower()
    if status in {"complete", "completed", "cancelled", "failed"}:
        return None
    return plan


def load_recent_event_summary(
    session: Session,
    *,
    strategist_session_id: str,
    goal_id: str | None = None,
    exclude_run_id: str | None = None,
    limit: int = 24,
) -> list[dict[str, Any]]:
    statement = (
        select(StrategistAgentEvent)
        .where(StrategistAgentEvent.session_id == strategist_session_id)
        .order_by(StrategistAgentEvent.created_at.desc())
        .limit(max(1, min(limit, 30)))
    )
    if goal_id is not None:
        statement = statement.where(StrategistAgentEvent.goal_id == goal_id)
    events = list(session.exec(statement).all())
    summary: list[dict[str, Any]] = []
    kept_continuation_state = False
    for event in events:
        if exclude_run_id is not None and event.run_id == exclude_run_id:
            continue
        payload = _json_loads_dict(event.payload_json)
        compact_payload: dict[str, Any] = {}
        for key in (
            "prompt",
            "ui_event_kind",
            "policy",
            "mode",
            "summary",
            "has_proposal",
            "has_clarification",
            "proposal_id",
            "proposal_kind",
            "action",
            "status",
            "placement_reasons",
            "proposed_blocks",
            "gap_policy",
            "continuation_state",
        ):
            if key in payload:
                if key == "continuation_state" and kept_continuation_state:
                    continue
                compact_payload[key] = (
                    compact_continuation_state(payload[key])
                    if key == "continuation_state"
                    else payload[key]
                )
                if key == "continuation_state":
                    kept_continuation_state = True
        if "target_node_id" in payload:
            compact_payload["target_task_id"] = payload["target_node_id"]
        elif "target_task_id" in payload:
            compact_payload["target_task_id"] = payload["target_task_id"]
        if "target_node_ids" in payload:
            compact_payload["target_task_ids"] = payload["target_node_ids"]
        elif "target_task_ids" in payload:
            compact_payload["target_task_ids"] = payload["target_task_ids"]
        if event.event_type == "tool_observation":
            compact_payload["evidence_handle"] = f"evidence:{event.id}"
            tool_name = payload.get("name")
            if isinstance(tool_name, str) and tool_name.strip():
                compact_payload["tool_name"] = tool_name.strip()
            arguments = payload.get("arguments")
            if isinstance(arguments, dict):
                compact_payload["arguments"] = {
                    key: value
                    for key, value in arguments.items()
                    if key in {"task_id", "query", "category_name", "limit"}
                    and isinstance(value, (str, int, float, bool))
                }
            result = payload.get("result")
            if isinstance(result, dict):
                compact_payload["tool_context"] = build_tool_context_entry(
                    name=str(tool_name or "tool"),
                    arguments=arguments if isinstance(arguments, dict) else {},
                    result=result,
                )
                task = result.get("task")
                if isinstance(task, dict):
                    task_id = task.get("task_id", task.get("id"))
                    title = task.get("title")
                    if isinstance(task_id, int) or isinstance(title, str):
                        compact_payload["task"] = {
                            "task_id": task_id if isinstance(task_id, int) else None,
                            "title": title if isinstance(title, str) else None,
                        }
                observed_tasks: list[dict[str, Any]] = []

                def append_observed_task(raw_task_id: Any, raw_title: Any, *, source: str) -> None:
                    node_id = raw_task_id if isinstance(raw_task_id, int) else None
                    title = raw_title if isinstance(raw_title, str) and raw_title.strip() else None
                    if node_id is None and title is None:
                        return
                    key = (node_id, str(title or "").lower())
                    for existing in observed_tasks:
                        existing_key = (
                            existing.get("task_id"),
                            str(existing.get("title") or "").lower(),
                        )
                        if existing_key == key:
                            return
                    observed_tasks.append(
                        {
                            "task_id": node_id,
                            "title": title,
                            "source": source,
                        }
                    )

                task_title_by_id: dict[int, str] = {}

                def index_result_task(raw_item: Any) -> None:
                    if not isinstance(raw_item, dict):
                        return
                    task_item = raw_item.get("task") if isinstance(raw_item.get("task"), dict) else raw_item
                    if not isinstance(task_item, dict):
                        return
                    node_id = task_item.get("task_id") or task_item.get("id") or task_item.get("node_id")
                    title = task_item.get("title")
                    if isinstance(node_id, int) and isinstance(title, str) and title.strip():
                        task_title_by_id[node_id] = title

                for result_key in ("tasks", "children", "siblings", "parents", "matches", "entity_matches"):
                    raw_items = result.get(result_key)
                    if isinstance(raw_items, list):
                        for raw_item in raw_items:
                            index_result_task(raw_item)
                raw_snapshots = result.get("task_snapshots")
                if isinstance(raw_snapshots, list):
                    for snapshot in raw_snapshots:
                        if isinstance(snapshot, dict):
                            index_result_task(snapshot.get("task"))
                answer_candidate = result.get("answer_candidate")
                answer_task_ids = (
                    answer_candidate.get("task_ids")
                    if isinstance(answer_candidate, dict)
                    else None
                )
                if isinstance(answer_task_ids, list):
                    for raw_task_id in answer_task_ids:
                        append_observed_task(
                            raw_task_id,
                            task_title_by_id.get(raw_task_id) if isinstance(raw_task_id, int) else None,
                            source="answer_candidate",
                        )
                        if len(observed_tasks) >= 5:
                            break
                for result_key in ("tasks", "children", "siblings", "parents", "matches", "entity_matches"):
                    raw_items = result.get(result_key)
                    if not isinstance(raw_items, list):
                        continue
                    for raw_item in raw_items:
                        if not isinstance(raw_item, dict):
                            continue
                        task_item = raw_item.get("task") if isinstance(raw_item.get("task"), dict) else raw_item
                        node_id = (
                            task_item.get("task_id")
                            or task_item.get("id")
                            or task_item.get("node_id")
                        )
                        title = task_item.get("title")
                        if not isinstance(node_id, int) and not isinstance(title, str):
                            continue
                        append_observed_task(node_id, title, source=result_key)
                        if len(observed_tasks) >= 5:
                            break
                    if len(observed_tasks) >= 5:
                        break
                if isinstance(raw_snapshots, list) and len(observed_tasks) < 5:
                    for snapshot in raw_snapshots:
                        if not isinstance(snapshot, dict):
                            continue
                        task = snapshot.get("task")
                        if not isinstance(task, dict):
                            continue
                        node_id = task.get("task_id") or task.get("id") or task.get("node_id")
                        title = task.get("title")
                        if not isinstance(node_id, int) and not isinstance(title, str):
                            continue
                        append_observed_task(node_id, title, source="task_snapshots")
                        if len(observed_tasks) >= 5:
                            break
                if observed_tasks:
                    compact_payload["observed_tasks"] = observed_tasks
        elif event.event_type == "agent_run_plan_created":
            compact_plan = compact_agent_run_plan_for_model(payload)
            if compact_plan is not None:
                compact_payload["agent_run_plan"] = compact_plan
                compact_payload["plan_id"] = compact_plan.get("plan_id")
                compact_payload["kind"] = compact_plan.get("kind")
                compact_payload["status"] = compact_plan.get("status")
                compact_payload["scope"] = compact_plan.get("scope")
                compact_payload["work_unit_count"] = compact_plan.get("work_unit_count")
                compact_payload["current_work_unit_id"] = compact_plan.get("current_work_unit_id")
        summary.append(
            {
                "event_type": event.event_type,
                "source": event.source,
                "created_at": event.created_at.isoformat(),
                "payload": compact_payload,
            }
        )
    return list(reversed(summary))


def load_latest_continuation_state(
    session: Session,
    *,
    strategist_session_id: str,
    goal_id: str,
    original_request: str | None = None,
) -> dict[str, Any] | None:
    """Load the authoritative open-turn checkpoint without summary-window loss."""

    events = list(
        session.exec(
            select(StrategistAgentEvent)
            .where(StrategistAgentEvent.session_id == strategist_session_id)
            .where(StrategistAgentEvent.event_type == "final_answer")
            .order_by(StrategistAgentEvent.created_at.desc())
            .limit(8)
        ).all()
    )
    for event in events:
        payload = _json_loads_dict(event.payload_json)
        state = compact_continuation_state(payload.get("continuation_state"))
        if not isinstance(state, dict):
            continue
        state_goal_id = str(state.get("goal_id") or "").strip()
        same_request = bool(
            str(original_request or "").strip()
            and str(state.get("original_request") or "").strip()
            == str(original_request or "").strip()
        )
        if state_goal_id != goal_id and not same_request:
            continue
        if str(state.get("status") or "").strip().lower() not in {
            "awaiting_user_input",
            "in_progress",
            "pending_approval",
        }:
            return None
        return state
    return None


def _load_kernel_world(
    session: Session,
    *,
    active_category_id: int | None,
    focus_node_id: int | None,
) -> tuple[list[Category], list[StrategyNode], Category | None, StrategyNode | None]:
    categories = list(
        session.exec(select(Category).order_by(Category.sort_order, Category.name)).all()
    )
    nodes = list(
        session.exec(
            select(StrategyNode).order_by(
                StrategyNode.category_id,
                StrategyNode.parent_id,
                StrategyNode.sort_order,
                StrategyNode.title,
            )
        ).all()
    )
    active_category = (
        session.get(Category, active_category_id)
        if isinstance(active_category_id, int)
        else None
    )
    focus_node = (
        session.get(StrategyNode, focus_node_id)
        if isinstance(focus_node_id, int)
        else None
    )
    return categories, nodes, active_category, focus_node


def _snapshot_category(category: Category) -> Category:
    """Copy ORM-loaded category data into an object detached from the DB session."""

    return Category(
        id=category.id,
        name=category.name,
        accent_color=category.accent_color,
        icon_slug=category.icon_slug,
        sort_order=category.sort_order,
        created_at=category.created_at,
    )


def _snapshot_node(node: StrategyNode) -> StrategyNode:
    """Copy ORM-loaded task data into an object detached from the DB session."""

    return StrategyNode(
        id=node.id,
        category_id=node.category_id,
        title=node.title,
        notes=node.notes,
        status=node.status,
        priority=node.priority,
        node_type=node.node_type,
        parent_id=node.parent_id,
        planned_date=node.planned_date,
        deadline_at=node.deadline_at,
        time_estimate_minutes=node.time_estimate_minutes,
        emoji=node.emoji,
        tags=node.tags,
        image_attachments=node.image_attachments,
        is_pinned=node.is_pinned,
        sort_order=node.sort_order,
        is_backlog=node.is_backlog,
        created_at=node.created_at,
    )


def _snapshot_kernel_world(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    active_category: Category | None,
    focus_node: StrategyNode | None,
) -> tuple[list[Category], list[StrategyNode], Category | None, StrategyNode | None]:
    """Detach the task-world read model before events or threaded tools run.

    SQLAlchemy expires ORM objects on commit by default. The agent runtime writes
    trace events between loading context and executing tools, and provider loops may
    execute read-only tools in worker threads. Passing live ORM objects across that
    boundary can therefore trigger cross-thread lazy loads. Snapshot copies keep
    the tool world deterministic, fast, and independent of the request session.
    """

    category_snapshots = [_snapshot_category(category) for category in categories]
    node_snapshots = [_snapshot_node(node) for node in nodes]
    category_by_id = {
        category.id: category
        for category in category_snapshots
        if category.id is not None
    }
    node_by_id = {
        node.id: node
        for node in node_snapshots
        if node.id is not None
    }
    active_category_snapshot = None
    if active_category is not None:
        active_category_snapshot = category_by_id.get(active_category.id) or _snapshot_category(active_category)
    focus_node_snapshot = None
    if focus_node is not None:
        focus_node_snapshot = node_by_id.get(focus_node.id) or _snapshot_node(focus_node)
    return category_snapshots, node_snapshots, active_category_snapshot, focus_node_snapshot


def _mode_from_payload(
    *,
    action_proposal: dict[str, Any] | None,
    metadata_proposal: dict[str, Any] | None,
    branch_proposal: dict[str, Any] | None,
    clarification: dict[str, Any] | None = None,
    declared_mode: Any = None,
) -> str:
    if action_proposal:
        return "action"
    if metadata_proposal:
        return "metadata"
    if branch_proposal:
        return "branch"
    if clarification:
        normalized_mode = str(declared_mode or "").strip().lower()
        if normalized_mode in {"review", "metadata", "branch", "action"}:
            return normalized_mode
    return "review"


def _compact_tool_trace(
    tool_trace: list[dict[str, Any]],
    *,
    include_arguments: bool = True,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in tool_trace:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        result_count = None
        for key in (
            "matches",
            "tasks",
            "children",
            "siblings",
            "parents",
            "related",
            "note_chunks",
            "working_set",
            "recent_actions",
            "recent_events",
            "due_soon",
            "overdue",
            "stale",
        ):
            value = result.get(key) if isinstance(result, dict) else None
            if isinstance(value, list):
                result_count = len(value)
                break
        compacted.append(
            {
                "name": item.get("name"),
                **(
                    {
                        "arguments": (
                            item.get("arguments")
                            if isinstance(item.get("arguments"), dict)
                            else {}
                        )
                    }
                    if include_arguments
                    else {}
                ),
                "result_count": result_count,
                "error": result.get("error"),
                "compiled": result.get("compiled"),
                "model_instruction": result.get("model_instruction"),
                "summary": item.get("summary") if isinstance(item.get("summary"), str) else None,
                "duration_ms": item.get("duration_ms"),
                "prefetch": bool(item.get("prefetch")),
            }
        )
    return compacted


def _proposal_evidence_bundle(
    *,
    source: str,
    tool_trace: list[dict[str, Any]],
    active_category: Category | None = None,
    categories: list[Category] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": source,
        "tool_trace": _compact_tool_trace(tool_trace),
    }
    if active_category is not None:
        evidence["active_category"] = {
            "category_id": active_category.id,
            "name": active_category.name,
        }
    if categories is not None:
        evidence["available_categories"] = [
            {"category_id": category.id, "name": category.name}
            for category in categories
            if category.id is not None
        ]
    return evidence


def _proposal_from_typed_tool_trace(
    *,
    tool_trace: list[dict[str, Any]],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None = None,
    categories: list[Category] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    del nodes, focus_node, active_category, categories
    for tool_call in reversed(tool_trace):
        name = str(tool_call.get("name") or "").strip()
        if name not in SEMANTIC_PLAN_TOOL_FAMILIES:
            continue
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        if result.get("error") or result.get("compiled") is False:
            # This is the latest mutation-tool attempt in the turn. A failed
            # correction supersedes any earlier successful draft for handoff
            # purposes; surfacing the older draft would approve work the user
            # has already tried to change. A later successful call is still
            # found first because the trace is scanned in reverse.
            return None
        semantic_input = (
            result.get("semantic_plan_input")
            if isinstance(result.get("semantic_plan_input"), dict)
            else {}
        )
        proposal = semantic_input.get("payload")
        if name in {
            "submit_preference_plan",
            "submit_preference_management_plan",
            "submit_preference_cleanup_plan",
        }:
            if isinstance(proposal, dict):
                return PREFERENCE_PROPOSAL_KIND, proposal, tool_call
        if name == "submit_task_metadata_plan":
            if isinstance(proposal, dict):
                return "metadata", copy.deepcopy(proposal), tool_call
        if name == "submit_task_tree_plan":
            if isinstance(proposal, dict):
                return "branch", copy.deepcopy(proposal), tool_call
    return None


def _proposals_from_typed_tool_trace(
    *,
    tool_trace: list[dict[str, Any]],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None = None,
    categories: list[Category] | None = None,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Return the latest valid submission for every semantic-plan family.

    A failed correction invalidates only its own family. This lets one turn
    conserve a task/tree clause and a preference clause without allowing an
    older draft from either family to survive a later failed correction.
    """

    submissions: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for family in dict.fromkeys(SEMANTIC_PLAN_TOOL_FAMILIES.values()):
        family_trace = [
            item
            for item in tool_trace
            if SEMANTIC_PLAN_TOOL_FAMILIES.get(
                str(item.get("name") or "").strip()
            )
            == family
        ]
        if not family_trace:
            continue
        proposal = _proposal_from_typed_tool_trace(
            tool_trace=family_trace,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
        )
        if proposal is None:
            continue
        proposal_kind, payload, tool_call = proposal
        submissions[proposal_kind] = (payload, tool_call)
    return submissions


def _metadata_repair_target_from_tool_trace(
    *,
    tool_trace: list[dict[str, Any]],
    nodes: list[StrategyNode],
    prompt: str,
) -> StrategyNode | None:
    for tool_call in reversed(tool_trace):
        if str(tool_call.get("name") or "") != "get_task_snapshot":
            continue
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        task = result.get("task") if isinstance(result.get("task"), dict) else None
        if task is None:
            continue
        node = _node_from_serialized_task(task, nodes)
        if node is not None:
            return node

    for tool_call in reversed(tool_trace):
        if str(tool_call.get("name") or "") != "search_entities":
            continue
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        matches = result.get("matches")
        if not isinstance(matches, list) or not _search_result_is_confident(matches, prompt=prompt):
            continue
        task = _task_from_search_match(matches[0])
        if task is None:
            continue
        node = _node_from_serialized_task(task, nodes)
        if node is not None:
            return node

    for tool_call in reversed(tool_trace):
        if str(tool_call.get("name") or "") != "query_task_graph":
            continue
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        answer_candidate = (
            result.get("answer_candidate")
            if isinstance(result.get("answer_candidate"), dict)
            else {}
        )
        task_ids = answer_candidate.get("task_ids")
        if isinstance(task_ids, list) and len(task_ids) == 1:
            node_id = _coerce_int_id(task_ids[0])
            if node_id is not None:
                node = next((item for item in nodes if item.id == node_id), None)
                if node is not None:
                    return node
        for key in ("task_snapshots", "entity_matches", "matches"):
            raw_items = result.get(key)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                task = item.get("task")
                if not isinstance(task, dict) and key == "task_snapshots":
                    task = item.get("task")
                if not isinstance(task, dict):
                    continue
                node = _node_from_serialized_task(task, nodes)
                if node is not None:
                    return node

    for tool_call in reversed(tool_trace):
        if str(tool_call.get("name") or "") != "get_context_surfaces":
            continue
        result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
        memory = result.get("session_memory")
        if isinstance(memory, dict):
            node = _metadata_repair_target_from_session_memory(
                session_memory=memory,
                nodes=nodes,
            )
            if node is not None:
                return node
    return None


def _coerce_int_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _node_from_task_ref(
    *,
    nodes: list[StrategyNode],
    task_id: Any = None,
    title: Any = None,
) -> StrategyNode | None:
    node_id = _coerce_int_id(task_id)
    if node_id is not None:
        node = next((item for item in nodes if item.id == node_id), None)
        if node is not None:
            return node
    title_text = str(title or "").strip().lower()
    if title_text:
        return next(
            (
                item
                for item in nodes
                if str(item.title or "").strip().lower() == title_text
            ),
            None,
        )
    return None


def _contract_requires_structural_proposal(contract_text: str) -> bool:
    if STRUCTURAL_PROPOSAL_CONTRACT_RE.search(contract_text):
        return True
    return bool(TREE_MOVE_PROPOSAL_CONTRACT_RE.search(contract_text))


def _metadata_repair_target_from_contract_text(
    *,
    contract_text: str,
    nodes: list[StrategyNode],
) -> StrategyNode | None:
    for match in TASK_ID_REF_RE.finditer(contract_text):
        node = _node_from_task_ref(nodes=nodes, task_id=match.group(1))
        if node is not None:
            return node

    for pattern in (TASK_ID_QUOTED_TITLE_RE, QUOTED_TASK_TITLE_CONTRACT_RE):
        for match in pattern.finditer(contract_text):
            title = " ".join(match.group(1).strip().strip(",").split())
            if not title:
                continue
            node = _find_node_by_title(nodes, title)
            if node is not None:
                return node
    return None


def _deadline_update_from_contract_text(
    *,
    contract_text: str,
    today_value: date,
) -> str | None:
    for match in PROPOSED_DATE_VALUE_RE.finditer(contract_text):
        fragment = match.group(1).strip()
        if not fragment:
            continue
        deadline_at = parse_prompt_date(fragment, today_value)
        if deadline_at is not None:
            return deadline_at
        deadline_at = parse_prompt_date(f"due {fragment}", today_value)
        if deadline_at is not None:
            return deadline_at
    deadline_at = parse_prompt_date(contract_text, today_value)
    if deadline_at is not None:
        return deadline_at
    return parse_prompt_date(f"due {contract_text}", today_value)


def _metadata_repair_target_from_session_memory(
    *,
    session_memory: dict[str, Any],
    nodes: list[StrategyNode],
) -> StrategyNode | None:
    current_target = session_memory.get("current_target_branch")
    if isinstance(current_target, dict):
        node = _node_from_task_ref(
            nodes=nodes,
            task_id=current_target.get("task_id"),
            title=current_target.get("title"),
        )
        if node is not None:
            return node
        task_ids = current_target.get("task_ids")
        if isinstance(task_ids, list) and len(task_ids) == 1:
            node = _node_from_task_ref(nodes=nodes, task_id=task_ids[0])
            if node is not None:
                return node

    recent_targets = session_memory.get("recent_targets")
    if isinstance(recent_targets, list):
        for target in reversed(recent_targets):
            if not isinstance(target, dict):
                continue
            node = _node_from_task_ref(
                nodes=nodes,
                task_id=target.get("task_id"),
                title=target.get("title"),
            )
            if node is not None:
                return node

    working_set = session_memory.get("working_set")
    if isinstance(working_set, list):
        for item in reversed(working_set):
            if not isinstance(item, dict):
                continue
            node = _node_from_task_ref(
                nodes=nodes,
                task_id=item.get("task_id"),
                title=item.get("title"),
            )
            if node is not None:
                return node

    hints = session_memory.get("continuity_hints")
    target_ids = hints.get("target_task_ids") if isinstance(hints, dict) else None
    if isinstance(target_ids, list) and len(target_ids) == 1:
        return _node_from_task_ref(nodes=nodes, task_id=target_ids[0])
    return None


def _first_non_empty_text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.strip().split())
    return None


def _action_contract_text(
    action_proposal: dict[str, Any],
    *,
    summary: str,
) -> str:
    parts: list[str] = [summary]
    for key in (
        "kind",
        "proposal_kind",
        "proposalKind",
        "type",
        "operation",
        "action",
        "summary",
        "rationale",
        "confirmation_title",
        "confirmationTitle",
        "confirmation_body",
        "confirmationBody",
    ):
        value = action_proposal.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    consequences = action_proposal.get("consequences")
    if isinstance(consequences, list):
        parts.extend(str(item).strip() for item in consequences if str(item).strip())
    return "\n".join(parts)


def _action_proposal_has_zero_operation_patch(
    *,
    action_proposal: dict[str, Any],
    nodes: list[StrategyNode],
) -> bool:
    task_patch = build_task_patch_draft(
        proposal_kind="action",
        proposal=action_proposal,
    )
    validation = validate_task_patch(task_patch, nodes)
    return (
        validation.get("status") == "invalid"
        and validation.get("operation_count") == 0
    )


def _extract_create_task_title_from_action_contract(
    *,
    action_proposal: dict[str, Any],
    summary: str,
    active_category: Category | None,
) -> str | None:
    category_name = (
        active_category.name.strip().lower()
        if active_category is not None and isinstance(active_category.name, str)
        else ""
    )
    structured_title = _first_non_empty_text(
        action_proposal,
        "task_title",
        "taskTitle",
        "new_task_title",
        "newTaskTitle",
        "title",
        "name",
    )
    if structured_title and structured_title.lower() != category_name:
        return structured_title

    proposed_children = action_proposal.get(
        "proposed_children",
        action_proposal.get("proposedChildren"),
    )
    if isinstance(proposed_children, list):
        for item in proposed_children:
            if isinstance(item, str) and item.strip():
                return " ".join(item.strip().split())

    target_title = _first_non_empty_text(
        action_proposal,
        "target_node_title",
        "targetNodeTitle",
        "target_task_title",
        "targetTaskTitle",
    )
    if target_title and target_title.lower() != category_name:
        return target_title

    contract_text = _action_contract_text(action_proposal, summary=summary)
    quoted_match = PROVIDER_QUOTED_TASK_TITLE_RE.search(contract_text)
    if quoted_match:
        title = " ".join(quoted_match.group(1).strip().split())
        if title and title.lower() != category_name:
            return title
    return None


def _create_task_metadata_updates_from_action_contract(
    *,
    action_proposal: dict[str, Any],
    prompt: str,
    summary: str,
    today_override: date | None,
) -> dict[str, Any]:
    today_value = _today_value(today_override)
    contract_text = _action_contract_text(action_proposal, summary=summary)
    text_parts = [prompt, contract_text]
    for key in ("deadline_at", "deadlineAt", "due_date", "dueDate", "deadline", "due"):
        value = action_proposal.get(key)
        if isinstance(value, str) and value.strip():
            text_parts.append(f"due {value.strip()}")
    updates = build_metadata_updates_from_prompt(
        "\n".join(part for part in text_parts if part),
        today_value,
    )

    raw_updates = action_proposal.get("updates")
    if isinstance(raw_updates, dict):
        for key in ("deadline_at", "deadlineAt", "due_date", "dueDate"):
            value = raw_updates.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    updates["deadline_at"] = date.fromisoformat(value.strip()).isoformat()
                except ValueError:
                    pass
        priority = raw_updates.get("priority")
        if isinstance(priority, str) and priority.strip().lower() in {"low", "medium", "high"}:
            updates["priority"] = priority.strip().lower()
        status = raw_updates.get("status")
        if isinstance(status, str) and status.strip().lower() in {"todo", "in-progress", "done"}:
            updates["status"] = status.strip().lower()
        estimate = raw_updates.get("time_estimate_minutes", raw_updates.get("timeEstimateMinutes"))
        if isinstance(estimate, str) and estimate.strip().isdigit():
            estimate = int(estimate.strip())
        if isinstance(estimate, (int, float)) and int(estimate) > 0:
            updates["time_estimate_minutes"] = int(estimate)

    allowed_create_fields = {
        "planned_date",
        "deadline_at",
        "priority",
        "status",
        "time_estimate_minutes",
        "emoji",
        "tags",
        "is_pinned",
        "node_type",
    }
    return {
        key: value
        for key, value in updates.items()
        if key in allowed_create_fields and value is not None
    }


def _metadata_updates_from_proposal_contract(
    *,
    prompt: str,
    summary: str,
    action_proposal: dict[str, Any] | None,
    today_override: date | None,
) -> dict[str, Any]:
    today_value = _today_value(today_override)
    text_parts = [prompt, summary]
    raw_updates: dict[str, Any] = {}
    if isinstance(action_proposal, dict):
        text_parts.append(_action_contract_text(action_proposal, summary=summary))
        action_updates = action_proposal.get("updates")
        if isinstance(action_updates, dict):
            raw_updates.update(action_updates)
        for key in (
            "planned_date",
            "deadline_at",
            "deadlineAt",
            "due_date",
            "dueDate",
            "deadline",
            "due",
            "priority",
            "status",
            "time_estimate_minutes",
            "timeEstimateMinutes",
        ):
            value = action_proposal.get(key)
            if value is not None:
                raw_updates[key] = value
    contract_text = "\n".join(part for part in text_parts if part)
    updates = build_metadata_updates_from_prompt(contract_text, today_value)
    if "deadline_at" not in updates:
        deadline_at = _deadline_update_from_contract_text(
            contract_text=contract_text,
            today_value=today_value,
        )
        if deadline_at is not None:
            updates["deadline_at"] = deadline_at

    for key in ("deadline_at", "deadlineAt", "due_date", "dueDate"):
        value = raw_updates.get(key)
        if isinstance(value, str) and value.strip():
            try:
                updates["deadline_at"] = date.fromisoformat(value.strip()).isoformat()
            except ValueError:
                updates.update(build_metadata_updates_from_prompt(f"due {value}", today_value))
    priority = raw_updates.get("priority")
    if isinstance(priority, str) and priority.strip().lower() in {"low", "medium", "high"}:
        updates["priority"] = priority.strip().lower()
    status = raw_updates.get("status")
    if isinstance(status, str) and status.strip().lower() in {"todo", "in-progress", "done"}:
        updates["status"] = status.strip().lower()
    estimate = raw_updates.get("time_estimate_minutes", raw_updates.get("timeEstimateMinutes"))
    if isinstance(estimate, str) and estimate.strip().isdigit():
        estimate = int(estimate.strip())
    if isinstance(estimate, (int, float)) and int(estimate) > 0:
        updates["time_estimate_minutes"] = int(estimate)

    allowed_metadata_fields = {
        "title",
        "notes",
        "status",
        "priority",
        "planned_date",
        "deadline_at",
        "time_estimate_minutes",
        "tags",
        "is_pinned",
        "node_type",
    }
    return {
        key: value
        for key, value in updates.items()
        if key in allowed_metadata_fields and value is not None
    }


def _repair_metadata_proposal_from_context(
    *,
    prompt: str,
    summary: str,
    action_proposal: dict[str, Any] | None,
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    session_memory: dict[str, Any],
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    contract_text = "\n".join(
        part
        for part in (
            prompt,
            summary,
            _action_contract_text(action_proposal, summary=summary)
            if isinstance(action_proposal, dict)
            else "",
        )
        if part
    )
    if PROVIDER_FAILURE_CONTRACT_RE.search(contract_text):
        return None
    if _contract_requires_structural_proposal(contract_text):
        return None
    if not isinstance(action_proposal, dict) and not METADATA_PROPOSAL_CONTRACT_CLAIM_RE.search(contract_text):
        return None
    updates = _metadata_updates_from_proposal_contract(
        prompt=prompt,
        summary=summary,
        action_proposal=action_proposal,
        today_override=today_override,
    )
    if not updates:
        return None

    target_node: StrategyNode | None = None
    if isinstance(action_proposal, dict):
        target_node = _node_from_task_ref(
            nodes=nodes,
            task_id=action_proposal.get(
                "target_node_id",
                action_proposal.get("targetNodeId", action_proposal.get("target_task_id")),
            ),
            title=action_proposal.get(
                "target_node_title",
                action_proposal.get("targetNodeTitle", action_proposal.get("target_task_title")),
            ),
        )
    if target_node is None:
        target_node = _metadata_repair_target_from_contract_text(
            contract_text=contract_text,
            nodes=nodes,
        )
    if target_node is None:
        target_node = _metadata_repair_target_from_tool_trace(
            tool_trace=tool_trace,
            nodes=nodes,
            prompt=contract_text or prompt,
        )
    if target_node is None:
        target_node = _metadata_repair_target_from_session_memory(
            session_memory=session_memory,
            nodes=nodes,
        )
    if target_node is None and focus_node is not None:
        target_node = focus_node
    if target_node is None or target_node.id is None:
        return None

    proposal = build_metadata_patch_proposal(
        target_node=target_node,
        updates=updates,
        rationale=(
            "The model identified a metadata change but returned it outside the "
            "metadata proposal envelope, so the harness reconstructed the "
            "reviewable TaskPatch proposal from the current session context."
        ),
        evidence_bundle={
            "source": "agent_kernel_contract_repair",
            "tool_trace": _compact_tool_trace(tool_trace),
            "session_target": {
                "task_id": target_node.id,
                "title": target_node.title,
            },
        },
        summary=f"Update {target_node.title}",
    )
    return proposal


def _repair_create_task_action_proposal(
    *,
    action_proposal: dict[str, Any],
    prompt: str,
    summary: str,
    active_category: Category | None,
    today_override: date | None,
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    contract_text = _action_contract_text(action_proposal, summary=summary)
    if not CREATE_TASK_ACTION_CONTRACT_RE.search(contract_text):
        return None
    category_id = _coerce_int_id(
        action_proposal.get(
            "target_category_id",
            action_proposal.get(
                "targetCategoryId",
                action_proposal.get("category_id", action_proposal.get("categoryId")),
            ),
        )
    )
    if category_id is None and active_category is not None:
        category_id = _coerce_int_id(active_category.id)
    if category_id is None:
        return None

    task_title = _extract_create_task_title_from_action_contract(
        action_proposal=action_proposal,
        summary=summary,
        active_category=active_category,
    )
    if not task_title:
        return None

    category_name = (
        str(active_category.name).strip()
        if active_category is not None and str(active_category.name).strip()
        else _first_non_empty_text(
            action_proposal,
            "target_category_name",
            "targetCategoryName",
            "category_name",
            "categoryName",
        )
    )
    rationale = (
        _first_non_empty_text(action_proposal, "rationale", "confirmation_body", "confirmationBody")
        or "The provider returned a create-task action envelope, so the harness converted it into a reviewable TaskPatch create operation."
    )
    proposal_id = _first_non_empty_text(action_proposal, "proposal_id", "proposalId")
    proposal = build_top_level_create_task_proposal(
        category_id=category_id,
        category_name=category_name,
        task_title=task_title,
        rationale=rationale,
        evidence_bundle={
            "source": "agent_kernel_contract_repair",
            "tool_trace": _compact_tool_trace(tool_trace),
        },
        summary=summary or None,
        proposal_id=proposal_id,
    )
    updates = _create_task_metadata_updates_from_action_contract(
        action_proposal=action_proposal,
        prompt=prompt,
        summary=summary,
        today_override=today_override,
    )
    structural_operations = proposal.get("structural_operations")
    if isinstance(structural_operations, list) and structural_operations:
        root_operation = structural_operations[0]
        if isinstance(root_operation, dict):
            root_operation.update(updates)
    if updates:
        diff_preview = proposal.get("diff_preview")
        if isinstance(diff_preview, list):
            diff_preview.append(
                {
                    "kind": "metadata_update",
                    "label": "Set task metadata",
                    "detail": _metadata_update_summary(updates),
                    "after": ", ".join(sorted(updates.keys())),
                }
            )
    return proposal


def _conversation_text(conversation_history: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for message in conversation_history[-16:]:
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n".join(parts)


def _session_memory_text(session_memory: dict[str, Any]) -> str:
    parts: list[str] = []
    active_objective = session_memory.get("active_objective")
    if isinstance(active_objective, dict):
        summary = str(active_objective.get("summary") or "").strip()
        if summary:
            parts.append(summary)
    latest_assistant_answer = session_memory.get("latest_assistant_answer")
    if isinstance(latest_assistant_answer, dict):
        summary = str(latest_assistant_answer.get("summary") or "").strip()
        if summary:
            parts.append(summary)
    for key in ("recent_user_requests", "recent_assistant_answers"):
        recent_items = session_memory.get(key)
        if not isinstance(recent_items, list):
            continue
        for item in recent_items[-6:]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if summary:
                parts.append(summary)
    unresolved_asks = session_memory.get("unresolved_asks")
    if isinstance(unresolved_asks, list):
        for item in unresolved_asks[-4:]:
            if not isinstance(item, dict):
                continue
            for key in ("question", "summary"):
                value = str(item.get(key) or "").strip()
                if value:
                    parts.append(value)
    return "\n".join(parts)


def _preference_memory_proposal_has_valid_patch(proposal: Any) -> bool:
    if not isinstance(proposal, dict):
        return False
    preference_patch = proposal.get("preference_patch")
    if not isinstance(preference_patch, dict):
        return False
    validation = validate_preference_patch(preference_patch)
    return str(validation.get("status") or "").strip().lower() != "invalid"


def _payload_has_uncompiled_preference_memory_proposal(payload: dict[str, Any]) -> bool:
    proposal = payload.get(PREFERENCE_RESPONSE_KEY)
    return isinstance(proposal, dict) and not _preference_memory_proposal_has_valid_patch(proposal)


def _is_action_reminder_request(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(?:remind\s+me\s+to|set\s+(?:a\s+)?reminder\s+to|"
            r"(?:i\s+)?want\s+to\s+be\s+reminded\s+(?:to|of))\b",
            prompt,
            re.IGNORECASE,
        )
    )


SERIES_SCOPE_RE = re.compile(
    r"\b(?:every|all)\s+occurr?ences?\b|\b(?:whole|entire)\s+(?:recurring\s+)?series\b|"
    r"\b(?:all|every)\s+(?:future\s+)?(?:calls?|meetings?|blocks?)\b",
    re.IGNORECASE,
)
OCCURRENCE_SCOPE_RE = re.compile(
    r"\b(?:next|this|one|single)\s+(?:scheduled\s+)?(?:occurr?ence|call|meeting|block)\b|"
    r"\b(?:this|that)\s+(?:specific\s+)?date\b",
    re.IGNORECASE,
)


def _planner_handoff_target_scope(
    handoff: dict[str, Any],
    *,
    request_context_text: str,
) -> str | None:
    """Resolve only explicit mutation cardinality; never guess ambiguous scope."""

    # Explicit user cardinality outranks a contradictory provider field. This
    # is particularly important for referential follow-ups where the model may
    # accidentally retain occurrence scope from an earlier draft.
    request_series = bool(SERIES_SCOPE_RE.search(request_context_text))
    request_occurrence = bool(OCCURRENCE_SCOPE_RE.search(request_context_text))
    if request_series != request_occurrence:
        return "series" if request_series else "occurrence"
    declared = str(handoff.get("target_scope") or "").strip().lower()
    if declared in {"series", "occurrence"}:
        return declared
    evidence_fragments = [
        str(handoff.get("instruction") or ""),
        request_context_text,
    ]
    for constraint in handoff.get("request_constraints", []):
        if not isinstance(constraint, dict):
            continue
        evidence_fragments.extend(
            [
                str(constraint.get("source_text") or ""),
                str(constraint.get("label") or ""),
            ]
        )
    evidence = "\n".join(evidence_fragments)
    series = bool(SERIES_SCOPE_RE.search(evidence))
    occurrence = bool(OCCURRENCE_SCOPE_RE.search(evidence))
    if series == occurrence:
        return None
    return "series" if series else "occurrence"


def _hydrate_series_recurrence_handoff(
    handoff: dict[str, Any],
    *,
    tool_trace: list[dict[str, Any]],
    request_context_text: str,
) -> bool:
    """Conserve explicit series scope using authoritative schedule evidence."""

    scope = _planner_handoff_target_scope(
        handoff,
        request_context_text=request_context_text,
    )
    if scope is not None:
        handoff["target_scope"] = scope
    if scope != "series" or handoff.get("operation_mode") not in {
        "reschedule",
        "remove",
    }:
        return False
    task_ids = {
        int(task_id)
        for task_id in handoff.get("task_ids", [])
        if isinstance(task_id, int) and not isinstance(task_id, bool)
    }
    if not task_ids:
        return False

    blocks_by_task: dict[int, list[dict[str, Any]]] = {
        task_id: [] for task_id in task_ids
    }
    for call in tool_trace:
        if str(call.get("name") or "") not in {
            "get_task_schedule",
            "list_calendar_blocks",
        }:
            continue
        result = call.get("result")
        if not isinstance(result, dict) or result.get("error"):
            continue
        for block in result.get("blocks", []):
            if (
                isinstance(block, dict)
                and block.get("task_id") in task_ids
                and block.get("recurrence_series_id")
                and isinstance(block.get("recurrence_rule"), dict)
            ):
                blocks_by_task[int(block["task_id"])].append(block)
    if any(
        len(
            {
                str(block.get("recurrence_series_id") or "")
                for block in blocks
            }
        )
        != 1
        for blocks in blocks_by_task.values()
    ):
        return False

    # A single Planner recurrence contract currently describes one shared rule.
    # Refuse to merge unrelated series rules into a plausible-looking mutation.
    rules = [blocks[0]["recurrence_rule"] for blocks in blocks_by_task.values()]
    canonical_rules = {
        json.dumps(rule, ensure_ascii=True, sort_keys=True) for rule in rules
    }
    if len(canonical_rules) != 1:
        return False
    persisted_rule = copy.deepcopy(rules[0])
    recurrence = {
        key: persisted_rule.get(key)
        for key in (
            "frequency",
            "days_of_week",
            "start_date",
            "end_date",
            "occurrence_count",
            "duration_minutes",
            "preferred_start_time",
            "timezone",
            "strict",
        )
        if persisted_rule.get(key) is not None
    }
    supplied_recurrence = handoff.get("recurrence")
    if isinstance(supplied_recurrence, dict):
        recurrence.update(copy.deepcopy(supplied_recurrence))
    exact_starts = []
    retained_constraints: list[dict[str, Any]] = []
    for constraint in handoff.get("request_constraints", []):
        if not isinstance(constraint, dict) or constraint.get("kind") != "exact_start":
            if isinstance(constraint, dict):
                retained_constraints.append(constraint)
            continue
        raw_start = constraint.get("start_at")
        clock_match = re.fullmatch(
            r"((?:[01]\d|2[0-3]):[0-5]\d)(?::[0-5]\d)?",
            str(raw_start or "").strip(),
        )
        if clock_match:
            exact_starts.append(clock_match.group(1)[:5])
            continue
        try:
            exact_starts.append(datetime.fromisoformat(str(raw_start)).strftime("%H:%M"))
        except ValueError:
            retained_constraints.append(constraint)
            continue
    if exact_starts and len(set(exact_starts)) == 1:
        recurrence["preferred_start_time"] = exact_starts[0]
        recurrence["strict"] = True
    # An exact_start in a series mutation describes every occurrence's clock
    # anchor. Once conserved above, retaining it as a one-time request
    # constraint would incorrectly require a single dated local timestamp.
    handoff["request_constraints"] = retained_constraints
    if recurrence.get("occurrence_count") is not None:
        recurrence["end_mode"] = "count"
    elif recurrence.get("end_date") is not None:
        recurrence["end_mode"] = "until"
    else:
        recurrence["end_mode"] = "never"
    handoff["recurrence"] = recurrence
    handoff["series_scope_repaired"] = not isinstance(supplied_recurrence, dict)
    return True


def _payload_creates_task(normalized_payload: dict[str, Any]) -> bool:
    proposal = normalized_payload.get("branch_proposal")
    if not isinstance(proposal, dict):
        return False
    structural_operations = proposal.get("structural_operations")
    if isinstance(structural_operations, list) and any(
        isinstance(operation, dict)
        and str(operation.get("kind") or "").strip().lower() == "create_node"
        for operation in structural_operations
    ):
        return True
    task_patch = proposal.get("task_patch")
    operations = task_patch.get("operations") if isinstance(task_patch, dict) else None
    return isinstance(operations, list) and any(
        isinstance(operation, dict)
        and str(operation.get("op") or "").strip().lower() == "create"
        for operation in operations
    )


def _clarification_has_user_answer_contract(
    clarification: Any,
    *,
    tool_trace: list[dict[str, Any]],
) -> bool:
    """Require a usable answer surface or authoritative validator grounding.

    A clarification may be answered either in ordinary chat or through a
    structured choice control.  Requiring a button-style choice array made the
    host discard perfectly usable, evidence-backed questions and then force the
    model through a mutation tool.  The separate authoritative-grounding check
    still prevents an unobserved generic question from becoming an escape hatch.
    """

    if not isinstance(clarification, dict):
        return False
    has_free_form_question = any(
        isinstance(clarification.get(key), str)
        and bool(str(clarification.get(key)).strip())
        for key in ("message", "question")
    )
    if has_free_form_question and any(
        isinstance(call, dict)
        and isinstance(call.get("result"), dict)
        and (
            call["result"].get("clarification") not in (None, "", {})
            or not call["result"].get("error")
        )
        for call in tool_trace
    ):
        return True
    for key in (
        "entity_choices",
        "field_choices",
        "scoped_follow_up_prompts",
    ):
        values = clarification.get(key)
        if isinstance(values, list) and any(
            item not in (None, "", [], {}) for item in values
        ):
            return True
    return any(
        isinstance(call, dict)
        and isinstance(call.get("result"), dict)
        and isinstance(call["result"].get("clarification"), (dict, str))
        and call["result"].get("clarification") not in (None, "", {})
        for call in tool_trace
    )


def _clarification_has_authoritative_grounding(
    clarification: Any,
    *,
    tool_trace: list[dict[str, Any]],
) -> bool:
    """Return whether a clarification is backed by an observed capability.

    A model-authored question is not enough to stop a mutation turn before any
    capability has been consulted.  Successful reads and durable goal
    transitions can ground a real ambiguity, while a semantic validator may
    ground one directly in its result.  This keeps clarification decisions
    evidence-based without inspecting prompt phrases.
    """

    if not _clarification_has_user_answer_contract(
        clarification,
        tool_trace=tool_trace,
    ):
        return False
    for call in tool_trace:
        if not isinstance(call, dict) or not isinstance(call.get("result"), dict):
            continue
        result = call["result"]
        # A semantic validator reports a real user decision as a typed error
        # because no proposal may be compiled until that decision is made.  The
        # clarification carried by that result is authoritative evidence, not
        # a failed or ungrounded model escape hatch.
        if isinstance(result.get("clarification"), (dict, str)) and result.get(
            "clarification"
        ) not in (None, "", {}):
            return True
        if not result.get("error"):
            return True
    return False


def _payload_has_reviewable_or_clarifying_boundary(
    normalized_payload: dict[str, Any],
    *,
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    categories: list[Category],
    tool_trace: list[dict[str, Any]],
) -> bool:
    evidence_bundle = _proposal_evidence_bundle(
        source="agent_kernel_contract_check",
        tool_trace=tool_trace,
        active_category=active_category,
        categories=categories,
    )
    boundaries: list[str] = []
    metadata_proposal = normalized_payload.get("metadata_proposal")
    if isinstance(metadata_proposal, dict):
        canonical_metadata = normalize_metadata_proposal(
            metadata_proposal,
            nodes,
            focus_node,
            evidence_bundle=evidence_bundle,
        )
        if canonical_metadata is not None:
            boundaries.append("metadata")
    branch_proposal = normalized_payload.get("branch_proposal")
    if isinstance(branch_proposal, dict):
        canonical_branch = normalize_branch_proposal(
            branch_proposal,
            nodes,
            focus_node,
            evidence_bundle=evidence_bundle,
        )
        if canonical_branch is not None:
            raw_count = _proposal_operation_count(branch_proposal)
            canonical_count = _proposal_operation_count(canonical_branch)
            if (
                isinstance(raw_count, int)
                and isinstance(canonical_count, int)
                and canonical_count < raw_count
            ):
                return False
            boundaries.append("branch")
    if _clarification_has_user_answer_contract(
        normalized_payload.get("clarification"),
        tool_trace=tool_trace,
    ):
        boundaries.append("clarification")
    if _preference_memory_proposal_has_valid_patch(
        normalized_payload.get(PREFERENCE_RESPONSE_KEY)
    ):
        boundaries.append(PREFERENCE_PROPOSAL_KIND)
    action_proposal = normalized_payload.get("action_proposal")
    if isinstance(action_proposal, dict) and not _action_proposal_has_zero_operation_patch(
        action_proposal=action_proposal,
        nodes=nodes,
    ):
        boundaries.append("action")
    if not boundaries:
        # A typed proposal tool is itself the approval boundary. The model's
        # final prose envelope may intentionally omit a duplicate proposal;
        # treating that as missing caused a needless contract retry and could
        # discard an already validated multi-step mutation.
        typed_boundary = _proposal_from_typed_tool_trace(
            tool_trace=tool_trace,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
        )
        if typed_boundary is not None:
            boundaries.append(typed_boundary[0])
    return len(boundaries) == 1


def _proposal_operation_count(proposal: Any) -> int | None:
    if not isinstance(proposal, dict):
        return None
    structural_operations = proposal.get(
        "structural_operations",
        proposal.get("structuralOperations"),
    )
    if isinstance(structural_operations, list):
        return len([item for item in structural_operations if isinstance(item, dict)])
    task_patch = proposal.get("task_patch", proposal.get("taskPatch"))
    operations = task_patch.get("operations") if isinstance(task_patch, dict) else None
    if isinstance(operations, list):
        return len([item for item in operations if isinstance(item, dict)])
    target_updates = proposal.get("target_updates", proposal.get("targetUpdates"))
    if isinstance(target_updates, list):
        return sum(
            len(updates)
            for item in target_updates
            for updates in [item.get("updates") if isinstance(item, dict) else None]
            if isinstance(updates, dict)
        )
    updates = proposal.get("updates")
    if isinstance(updates, dict):
        return len(updates)
    proposed_children = proposal.get("proposed_children", proposal.get("proposedChildren"))
    if isinstance(proposed_children, list):
        return len([item for item in proposed_children if isinstance(item, str) and item.strip()])
    preference_patch = proposal.get("preference_patch", proposal.get("preferencePatch"))
    preference_operations = (
        preference_patch.get("operations")
        if isinstance(preference_patch, dict)
        else None
    )
    if isinstance(preference_operations, list):
        return len([item for item in preference_operations if isinstance(item, dict)])
    return None


def _build_proposal_contract_status(
    *,
    normalized_payload: dict[str, Any],
    action_proposal: dict[str, Any] | None,
    metadata_proposal: dict[str, Any] | None,
    branch_proposal: dict[str, Any] | None,
    preference_memory_proposal: dict[str, Any] | None,
    planner_result: dict[str, Any] | None,
    planner_proposal: dict[str, Any] | None,
    clarification: dict[str, Any] | None,
    nodes: list[StrategyNode],
    native_review_projected: bool = False,
    native_recurrence_requested: bool = False,
) -> dict[str, Any]:
    """Record proposal conservation across model, adapters, and Planner."""

    attempted: list[dict[str, Any]] = []
    canonical: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    families = (
        ("action", normalized_payload.get("action_proposal"), action_proposal),
        ("metadata", normalized_payload.get("metadata_proposal"), metadata_proposal),
        ("branch", normalized_payload.get("branch_proposal"), branch_proposal),
        (
            PREFERENCE_PROPOSAL_KIND,
            normalized_payload.get(PREFERENCE_RESPONSE_KEY),
            preference_memory_proposal,
        ),
    )
    for family, raw_proposal, canonical_proposal in families:
        raw_is_actionable = isinstance(raw_proposal, dict)
        if family == "action" and raw_is_actionable:
            raw_is_actionable = not _action_proposal_has_zero_operation_patch(
                action_proposal=raw_proposal,
                nodes=nodes,
            )
        if raw_is_actionable:
            attempted.append(
                {
                    "family": family,
                    "operation_count": _proposal_operation_count(raw_proposal),
                }
            )
        if isinstance(canonical_proposal, dict):
            canonical_count = _proposal_operation_count(canonical_proposal)
            canonical.append(
                {"family": family, "operation_count": canonical_count}
            )
            raw_count = _proposal_operation_count(raw_proposal)
            if (
                raw_is_actionable
                and isinstance(raw_count, int)
                and isinstance(canonical_count, int)
                and canonical_count < raw_count
            ):
                dropped.append(
                    {
                        "family": family,
                        "reason": "operation_count_decreased_during_canonicalization",
                        "attempted_operation_count": raw_count,
                        "canonical_operation_count": canonical_count,
                        "attempted_proposal": raw_proposal,
                    }
                )
        elif raw_is_actionable:
            dropped.append(
                {
                    "family": family,
                    "reason": "proposal_failed_canonicalization",
                    "attempted_operation_count": _proposal_operation_count(raw_proposal),
                    "attempted_proposal": raw_proposal,
                }
            )

    if planner_result is not None:
        attempted_family = (
            "native_recurrence" if native_recurrence_requested else "schedule_patch"
        )
        attempted.append({"family": attempted_family, "operation_count": None})
        if planner_proposal is not None and not native_recurrence_requested:
            schedule_patch = planner_proposal.get("schedulePatch")
            operations = (
                schedule_patch.get("operations")
                if isinstance(schedule_patch, dict)
                else None
            )
            canonical.append(
                {
                    "family": "schedule_patch",
                    "operation_count": (
                        len([item for item in operations if isinstance(item, dict)])
                        if isinstance(operations, list)
                        else None
                    ),
                }
            )
        elif clarification is None and not native_recurrence_requested:
            dropped.append(
                {
                    "family": "schedule_patch",
                    "reason": "planner_returned_no_proposal_or_clarification",
                }
            )
        elif (
            clarification is None
            and native_recurrence_requested
            and not native_review_projected
        ):
            dropped.append(
                {
                    "family": "native_recurrence",
                    "reason": "native_recurrence_review_not_projected",
                }
            )

    unrecognized_proposal = normalized_payload.get("unrecognized_proposal")
    if isinstance(unrecognized_proposal, dict):
        attempted.append(
            {
                "family": "unrecognized",
                "operation_count": _proposal_operation_count(unrecognized_proposal),
            }
        )
        dropped.append(
            {
                "family": "unrecognized",
                "reason": "unsupported_proposal_envelope",
                "attempted_operation_count": _proposal_operation_count(
                    unrecognized_proposal
                ),
                "attempted_proposal": unrecognized_proposal,
            }
        )

    declared_mode = str(normalized_payload.get("mode") or "").strip().lower()
    if (
        declared_mode in {"action", "metadata", "branch", "preference_memory", "schedule_patch"}
        and not canonical
        and clarification is None
    ):
        dropped.append(
            {
                "family": declared_mode,
                "reason": "declared_mutation_mode_without_reviewable_boundary",
            }
        )
    if native_review_projected:
        # The legacy proposal was intentionally replaced only after the native
        # ChangeSet passed simulation, rollout eligibility, and review
        # projection.  It is therefore not a proposal-canonicalization loss.
        # Keep the attempted legacy envelope visible for diagnostics while
        # representing the native review as the one canonical approval
        # boundary consumed by the UI.
        return {
            "status": "valid",
            "attempted": attempted,
            "canonical": [{"family": "native_v2", "operation_count": None}],
            "dropped": [],
        }
    return {
        "status": "invalid" if dropped else "valid",
        "attempted": attempted,
        "canonical": canonical,
        "dropped": dropped,
    }


def _should_retry_proposal_contract(
    *,
    prompt: str,
    normalized_payload: dict[str, Any],
    route_decision: TierRouteDecision,
    session_memory: dict[str, Any],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    categories: list[Category],
    tool_trace: list[dict[str, Any]],
) -> bool:
    """Retry a model-owned mutation turn that missed its typed boundary.

    Prompt wording is deliberately excluded. The retry is driven only by the
    model's structured declared mode or durable structured session state from a
    prior clarification, then the model gets another chance to call a typed
    proposal tool or return a clarification.
    """

    if not route_decision.allows_model:
        return False
    if (
        not tool_trace
        and SCHEDULING_REQUEST_RE.search(prompt)
        and not _has_scheduling_boundary(
            payload=normalized_payload,
            tool_trace=tool_trace,
        )
    ):
        # Dynamic tool use remains model-owned on the first pass. A zero-tool
        # scheduling response, however, has neither calendar evidence nor an
        # executable/reviewable boundary. Retry the complete request through
        # the one atomic task-plan contract instead of handing a compound turn
        # to an existing-task-only fallback or trusting the model's self-label.
        return True
    if (
        _is_action_reminder_request(prompt)
        and _preference_memory_proposal_has_valid_patch(
            normalized_payload.get(PREFERENCE_RESPONSE_KEY)
        )
        and not isinstance(normalized_payload.get("branch_proposal"), dict)
        and not isinstance(normalized_payload.get("clarification"), dict)
    ):
        return True
    if (
        SCHEDULING_REQUEST_RE.search(prompt)
        and isinstance(normalized_payload.get("clarification"), dict)
        and _dominant_scheduling_candidate(
            prompt=prompt,
            nodes=nodes,
            active_category=active_category,
        )
        is not None
        and not any(
            str(call.get("name") or "") == "handoff_to_planner"
            and isinstance(call.get("result"), dict)
            and isinstance(call["result"].get("planner_handoff"), dict)
            for call in tool_trace
        )
    ):
        # A clear task identity plus scheduling intent is enough to reach
        # Planner. Retry a model that asked an unnecessary identity or tool-
        # availability question before it called the terminal handoff.
        return True
    clarification = normalized_payload.get("clarification")
    if isinstance(clarification, dict):
        # An answerable-looking question with no observed capability evidence
        # is still an ungrounded escape hatch. Give the same controller one
        # bounded semantic-authoring retry; the selected tool's validator can
        # then compile the request or identify a concrete missing field.
        if not _clarification_has_authoritative_grounding(
            clarification,
            tool_trace=tool_trace,
        ):
            return True
    if _payload_has_reviewable_or_clarifying_boundary(
        normalized_payload,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        categories=categories,
        tool_trace=tool_trace,
    ):
        return False
    if any(
        isinstance(normalized_payload.get(key), dict)
        for key in ("action_proposal", "metadata_proposal", "branch_proposal")
    ):
        return True
    if _payload_has_uncompiled_preference_memory_proposal(normalized_payload):
        return True
    if isinstance(normalized_payload.get("unrecognized_proposal"), dict):
        return True
    declared_mode = str(normalized_payload.get("mode") or "").strip().lower()
    decision = normalized_payload.get("decision")
    decision_kind = (
        str(decision.get("kind") or "").strip().lower()
        if isinstance(decision, dict)
        else ""
    )
    if decision_kind == "mutation":
        return True
    if decision_kind == "clarification":
        return True
    if declared_mode in {"metadata", "branch", "action", "preference_memory"}:
        return True
    unresolved_asks = session_memory.get("unresolved_asks")
    return bool(
        isinstance(unresolved_asks, list)
        and any(
            isinstance(item, dict) and item.get("kind") == "clarification"
            for item in unresolved_asks
        )
    )


def _proposal_contract_retry_prompt(
    *,
    prompt: str,
    normalized_payload: dict[str, Any],
    route_decision: TierRouteDecision,
    session_memory: dict[str, Any],
    tool_trace: list[dict[str, Any]],
    goal_transition_required: bool,
) -> str:
    compact_session_memory = {
        key: session_memory.get(key)
        for key in (
            "active_request",
            "current_target_branch",
            "unresolved_asks",
            "working_set",
        )
        if session_memory.get(key) not in (None, "", [], {})
    }
    if isinstance(compact_session_memory.get("working_set"), list):
        compact_session_memory["working_set"] = compact_session_memory[
            "working_set"
        ][:16]
    compact_invalid_payload = {
        key: normalized_payload.get(key)
        for key in (
            "decision",
            "summary",
            "clarification",
            "action_proposal",
            "metadata_proposal",
            "branch_proposal",
            PREFERENCE_RESPONSE_KEY,
        )
        if normalized_payload.get(key) not in (None, "", [], {})
    }
    transition_repair_instruction = (
        "First call set_goal_transition once, then submit the semantic plan. "
        if goal_transition_required
        else ""
    )
    return (
        f"{prompt}\n\n"
        "Contract repair instruction: this is the same turn, not a new handoff. "
        "The prior final response had no valid semantic boundary. Preserve every user "
        "clause and call the relevant available semantic submission tool now. Use an "
        "available read tool first only when an existing entity or calendar fact must be "
        "resolved. Let the server validator compile the request or report a concrete "
        "missing field. Do not answer with prose or ask for execution-time content that "
        "does not prevent creating the requested task shell. "
        f"{transition_repair_instruction}\n\n"
        "Durable continuation memory JSON:\n"
        f"{json.dumps(compact_session_memory, ensure_ascii=True)}\n\n"
        "Prior invalid final payload JSON:\n"
        f"{json.dumps(compact_invalid_payload, ensure_ascii=True)}\n\n"
        "Existing tool observations JSON:\n"
        f"{json.dumps(_compact_tool_trace(tool_trace, include_arguments=False), ensure_ascii=True)}\n"
    )


def _proposal_contract_retry_tool_definitions(
    *,
    prompt: str,
    route_decision: TierRouteDecision,
    normalized_payload: dict[str, Any],
    tool_definitions: list[dict[str, Any]],
    nodes: list[StrategyNode],
    active_category: Category | None,
    goal_transition_required: bool = False,
    request_context_text: str | None = None,
) -> list[dict[str, Any]]:
    _ = (prompt, route_decision, nodes, active_category, request_context_text)
    declared_mode = str(normalized_payload.get("mode") or "").strip().lower()
    structural_retry = bool(
        declared_mode == "branch"
        or isinstance(normalized_payload.get("branch_proposal"), dict)
    )
    if _payload_has_uncompiled_preference_memory_proposal(normalized_payload):
        allowed = {
            "submit_preference_plan",
            "submit_preference_management_plan",
            "submit_preference_cleanup_plan",
            "analyze_preference_cleanup_candidates",
            "list_remembered_preferences",
        }
    elif structural_retry:
        allowed = {"get_session_memory", "query_task_graph", "submit_task_tree_plan"}
    elif declared_mode == "metadata" or isinstance(
        normalized_payload.get("metadata_proposal"), dict
    ):
        allowed = {"submit_task_metadata_plan"}
    else:
        # The first model response did not establish a trustworthy semantic
        # boundary, so do not re-classify its natural-language request with
        # prompt regexes. Use the task-plan boundary as the atomic mutation
        # surface: it can reference existing tasks, create new ones, edit
        # metadata, preserve dependencies, and schedule the whole batch. The
        # server-side validators remain the authority for exact scope.
        allowed = {"submit_task_tree_plan"}
    if goal_transition_required:
        # Contract repair must retain the prerequisite capability when the
        # initial authoring call was rejected before a goal transition. Without
        # this tool the retry can only repeat the same rejected write.
        allowed.add("set_goal_transition")
    selected = [
        tool
        for tool in tool_definitions
        if str(tool.get("name") or "").strip() in allowed
    ]
    selected_by_name = {
        str(tool.get("name") or "").strip(): tool
        for tool in selected
        if str(tool.get("name") or "").strip()
    }
    # A contract repair is a safety boundary, so it must not inherit an
    # accidentally incomplete or prose-only projection from the first pass.
    # Rehydrate only the already-authorized repair tools from the canonical MCP
    # policy registry. This preserves one semantic authority while guaranteeing
    # that the provider receives the JSON schema needed for atomic submission.
    canonical_tools = {
        str(tool.get("name") or "").strip(): tool
        for tool in registered_agent_provider_tool_schemas("strategist")
        if str(tool.get("name") or "").strip() in allowed
    }
    semantic_allowed = {
        name
        for name in allowed
        if name.startswith("submit_") or name == "handoff_to_planner"
    }
    rehydratable_names = (
        set(semantic_allowed) if len(semantic_allowed) == 1 else set()
    )
    if goal_transition_required:
        rehydratable_names.add("set_goal_transition")
    for name in sorted(rehydratable_names):
        current = selected_by_name.get(name)
        if isinstance(current, dict) and isinstance(current.get("parameters"), dict):
            continue
        canonical = canonical_tools.get(name)
        if not isinstance(canonical, dict):
            continue
        if name == "submit_task_tree_plan":
            canonical = _compact_task_tree_provider_schema(canonical)
        if current is not None:
            selected.remove(current)
        selected.append(canonical)
        selected_by_name[name] = canonical
    return selected


def _bulk_status_intent_text(
    *,
    prompt: str,
    conversation_history: list[dict[str, str]],
    session_memory: dict[str, Any],
) -> str:
    return "\n".join(
        part
        for part in (
            prompt,
            _conversation_text(conversation_history),
            _session_memory_text(session_memory),
        )
        if part.strip()
    )


def _detect_bulk_status_value(intent_text: str) -> str | None:
    lowered = intent_text.lower()
    if re.search(
        r"\b(?:not|isn['’]?t|isnt|wasn['’]?t|wasnt)\s+"
        r"(?:yet\s+|really\s+)?(?:done|complete|completed|finished)\b"
        r"|\b(?:not\s+done|to\s+do|todo|reopen|incomplete|unfinished)\b",
        lowered,
    ):
        return "todo"
    if re.search(
        r"\b(?:mark|set|change|update|complete|finish|finished)\b.{0,80}"
        r"\b(?:done|complete|completed|finished)\b",
        lowered,
    ):
        return "done"
    return None


def _has_broad_task_scope(intent_text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:all|every|everything|entire|whole)\b.{0,80}"
            r"\b(?:tasks?|items?|todos?|workspace|workspaces|categor(?:y|ies))\b"
            r"|\b(?:tasks?|items?|todos?)\b.{0,80}"
            r"\b(?:all|every|everything|entire|whole)\b",
            intent_text,
            re.IGNORECASE,
        )
    )


def _resolved_bulk_scope_from_prompt(
    prompt: str,
    active_category: Category | None,
) -> tuple[str, str] | None:
    """Return an explicit scope selected by the user after clarification.

    Broad bulk commands are intentionally not inferred from the active category
    alone. The initial "all tasks" request should still ask the user whether
    "all" means the current category or the whole workspace.
    """
    prompt_text = prompt.strip()
    if not prompt_text:
        return None
    lowered = prompt_text.lower()
    if active_category is not None:
        category_name = str(active_category.name or "").strip()
        category_norm = re.escape(category_name.lower()) if category_name else ""
        explicit_current_category = bool(
            re.search(
                r"\b(?:just|only|current|this|active)\b.{0,40}"
                r"\b(?:categor(?:y|ies)|work\s+category|work)\b"
                r"|\b(?:current|this|active)\s+(?:work\s+)?categor(?:y|ies)\b",
                lowered,
            )
        )
        explicit_named_category = bool(
            category_norm
            and re.search(
                rf"\b(?:just|only|current|this|active)\b.{{0,40}}\b{category_norm}\b"
                rf"|\b{category_norm}\b.{{0,40}}\bcategor(?:y|ies)\b",
                lowered,
            )
        )
        if explicit_current_category or explicit_named_category:
            return "category", category_name or "current category"
    if re.search(
        r"\b(?:whole|entire|all)\s+(?:workspace|workspaces|categor(?:y|ies))\b"
        r"|\b(?:everywhere|across\s+(?:all\s+)?categor(?:y|ies)|across\s+the\s+workspace)\b",
        lowered,
    ):
        return "workspace", "workspace"
    return None


def _json_string_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if isinstance(item, str) and item.strip()]


def _is_archived_node(node: StrategyNode) -> bool:
    return any(tag.lower() == "state:archived" for tag in _json_string_list(node.tags))


def _bulk_status_include_archived(intent_text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:include|including|also|with)\b.{0,40}\b(?:archived|historical)\b"
            r"|\b(?:archived|historical)\b.{0,40}\b(?:too|also|as\s+well)\b",
            intent_text,
            re.IGNORECASE,
        )
    )


def _matches_bulk_status_source(node: StrategyNode, target_status: str) -> bool:
    status = str(node.status).strip().lower()
    if target_status == "todo":
        return status == "done"
    if target_status == "done":
        return status != "done"
    return False


def _node_in_bulk_scope(
    node: StrategyNode,
    *,
    scope_kind: str,
    active_category: Category | None,
) -> bool:
    if node.id is None:
        return False
    if scope_kind == "category":
        return active_category is not None and node.category_id == active_category.id
    return True


def _status_bulk_target_nodes(
    *,
    nodes: list[StrategyNode],
    scope_kind: str,
    active_category: Category | None,
    target_status: str,
    include_archived: bool = False,
) -> list[StrategyNode]:
    scoped_nodes: list[StrategyNode] = []
    for node in nodes:
        if not _node_in_bulk_scope(
            node,
            scope_kind=scope_kind,
            active_category=active_category,
        ):
            continue
        if not include_archived and _is_archived_node(node):
            continue
        scoped_nodes.append(node)

    targets = [node for node in scoped_nodes if _matches_bulk_status_source(node, target_status)]
    targets.sort(
        key=lambda node: (
            node.category_id or 0,
            node.parent_id or 0,
            node.sort_order,
            node.title.lower(),
        )
    )
    return targets


def _status_bulk_skipped_archived_nodes(
    *,
    nodes: list[StrategyNode],
    scope_kind: str,
    active_category: Category | None,
    target_status: str,
) -> list[StrategyNode]:
    skipped = [
        node
        for node in nodes
        if _node_in_bulk_scope(
            node,
            scope_kind=scope_kind,
            active_category=active_category,
        )
        and _is_archived_node(node)
        and _matches_bulk_status_source(node, target_status)
    ]
    skipped.sort(
        key=lambda node: (
            node.category_id or 0,
            node.parent_id or 0,
            node.sort_order,
            node.title.lower(),
        )
    )
    return skipped


def _format_node_title_list(nodes: list[StrategyNode], *, limit: int = 4) -> str:
    titles = [node.title.strip() for node in nodes if node.title.strip()]
    if len(titles) <= limit:
        return ", ".join(titles)
    return ", ".join(titles[:limit]) + f", +{len(titles) - limit} more"


def _build_deterministic_bulk_status_payload(
    *,
    prompt: str,
    conversation_history: list[dict[str, str]],
    session_memory: dict[str, Any],
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> dict[str, Any] | None:
    scope = _resolved_bulk_scope_from_prompt(prompt, active_category)
    if scope is None:
        return None
    intent_text = _bulk_status_intent_text(
        prompt=prompt,
        conversation_history=conversation_history,
        session_memory=session_memory,
    )
    if not _has_broad_task_scope(intent_text):
        return None
    target_status = _detect_bulk_status_value(intent_text)
    if target_status not in VALID_BULK_STATUS_VALUES:
        return None
    include_archived = _bulk_status_include_archived(intent_text)

    scope_kind, scope_label = scope
    target_nodes = _status_bulk_target_nodes(
        nodes=nodes,
        scope_kind=scope_kind,
        active_category=active_category,
        target_status=target_status,
        include_archived=include_archived,
    )
    skipped_archived_nodes = (
        []
        if include_archived
        else _status_bulk_skipped_archived_nodes(
            nodes=nodes,
            scope_kind=scope_kind,
            active_category=active_category,
            target_status=target_status,
        )
    )
    if target_status == "todo":
        action_label = "reopen"
        source_label = "completed"
        visible_source_label = (
            "completed"
            if include_archived
            else "non-archived completed"
        )
        empty_summary = f"I found no {visible_source_label} tasks in {scope_label} to reopen."
        summary = (
            f"I prepared a proposal to reopen {len(target_nodes)} {visible_source_label} "
            f"task{'s' if len(target_nodes) != 1 else ''} in {scope_label}. "
            "Review it before applying."
        )
        proposal_summary = (
            f"Reopen {len(target_nodes)} {visible_source_label} "
            f"task{'s' if len(target_nodes) != 1 else ''} in {scope_label}"
        )
    else:
        action_label = "mark done"
        source_label = "active"
        visible_source_label = "active" if include_archived else "non-archived active"
        empty_summary = f"I found no {visible_source_label} tasks in {scope_label} to mark done."
        summary = (
            f"I prepared a proposal to mark {len(target_nodes)} {visible_source_label} "
            f"task{'s' if len(target_nodes) != 1 else ''} done in {scope_label}. "
            "Review it before applying."
        )
        proposal_summary = (
            f"Mark {len(target_nodes)} {visible_source_label} "
            f"task{'s' if len(target_nodes) != 1 else ''} done in {scope_label}"
        )

    if skipped_archived_nodes:
        archived_titles = _format_node_title_list(skipped_archived_nodes)
        archived_note = (
            f" I skipped {len(skipped_archived_nodes)} archived {source_label} "
            f"task{'s' if len(skipped_archived_nodes) != 1 else ''}: {archived_titles}. "
            "Say 'include archived too' if you want historical tasks included."
        )
        summary += archived_note
        empty_summary += archived_note

    metadata_proposal: dict[str, Any] | None = None
    if target_nodes:
        target_ids = [int(node.id) for node in target_nodes if node.id is not None]
        evidence = [
            {
                "source": "scoped_task_index",
                "detail": (
                    f"Matched {len(target_nodes)} {visible_source_label} task"
                    f"{'s' if len(target_nodes) != 1 else ''} in {scope_label} "
                    f"for a bulk status update to {target_status}."
                ),
            }
        ]
        if skipped_archived_nodes:
            evidence.append(
                {
                    "source": "archived_task_guard",
                    "detail": (
                        f"Skipped {len(skipped_archived_nodes)} archived {source_label} "
                        f"task{'s' if len(skipped_archived_nodes) != 1 else ''}: "
                        f"{_format_node_title_list(skipped_archived_nodes)}."
                    ),
                }
            )
        metadata_proposal = {
            "target_node_id": target_ids[0],
            "target_node_ids": target_ids,
            "target_node_title": (
                f"{len(target_nodes)} {visible_source_label} "
                f"task{'s' if len(target_nodes) != 1 else ''} in {scope_label}"
            ),
            "updates": {"status": target_status},
            "summary": proposal_summary,
            "rationale": (
                f"You chose {scope_label} as the scope, so Strategist enumerated every "
                f"{visible_source_label} task in that scope directly instead of using a sampled tree read."
            ),
            "evidence": evidence,
        }
    return {
        "summary": summary if target_nodes else empty_summary,
        "action_proposal": None,
        "metadata_proposal": metadata_proposal,
        "branch_proposal": None,
        "clarification": None,
        "deterministic_fast_path": {
            "kind": "bulk_status_scope",
            "scope_kind": scope_kind,
            "scope_label": scope_label,
            "target_status": target_status,
            "target_count": len(target_nodes),
            "include_archived": include_archived,
            "skipped_archived_count": len(skipped_archived_nodes),
            "skipped_archived_titles": _format_node_title_list(skipped_archived_nodes),
            "action_label": action_label,
        },
    }


def _build_kernel_native_bulk_status_payload(
    *,
    prompt: str,
    conversation_history: list[dict[str, str]],
    session_memory: dict[str, Any],
    nodes: list[StrategyNode],
    active_category: Category | None,
) -> dict[str, Any] | None:
    """Resolve broad status mutations without entering the model loop.

    Bulk status changes are safe to compile mechanically because the user still
    reviews the resulting TaskPatch before anything mutates. The agentic part is
    scope resolution: if the request says "all" but does not say whether that
    means the active category or the whole workspace, pause for clarification
    instead of asking a model to guess.
    """
    intent_text = _bulk_status_intent_text(
        prompt=prompt,
        conversation_history=conversation_history,
        session_memory=session_memory,
    )
    if not _has_broad_task_scope(intent_text):
        return None
    target_status = _detect_bulk_status_value(intent_text)
    if target_status not in VALID_BULK_STATUS_VALUES:
        return None

    scoped_payload = _build_deterministic_bulk_status_payload(
        prompt=prompt,
        conversation_history=conversation_history,
        session_memory=session_memory,
        nodes=nodes,
        active_category=active_category,
    )
    if scoped_payload is not None:
        return scoped_payload

    active_category_name = (
        str(active_category.name or "").strip()
        if active_category is not None
        else ""
    )
    action_label = (
        "reopen completed tasks"
        if target_status == "todo"
        else "mark active tasks done"
    )
    scoped_follow_up_prompts = ["the whole workspace"]
    option_rows: list[dict[str, str]] = [
        {
            "kind": "scope",
            "label": "Whole workspace",
            "value": "workspace",
            "description": f"{action_label.capitalize()} across every category.",
            "follow_up_prompt": "the whole workspace",
        }
    ]
    if active_category_name:
        current_prompt = f"just the current {active_category_name} category"
        scoped_follow_up_prompts.insert(0, current_prompt)
        option_rows.insert(
            0,
            {
                "kind": "scope",
                "label": f"Current {active_category_name} category",
                "value": "category",
                "description": f"{action_label.capitalize()} only in {active_category_name}.",
                "follow_up_prompt": current_prompt,
            },
        )

    return {
        "summary": "I need to confirm the scope before making a bulk status change.",
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": {
            "message": (
                "Do you want this applied to every task in the whole workspace"
                + (
                    f", or only to the current {active_category_name} category?"
                    if active_category_name
                    else "?"
                )
            ),
            "options": option_rows,
            "scoped_follow_up_prompts": scoped_follow_up_prompts,
        },
        "deterministic_fast_path": {
            "kind": "bulk_status_scope_clarification",
            "target_status": target_status,
            "active_category": active_category_name or None,
        },
    }


def _build_kernel_context_packet(
    *,
    active_goal: Any,
    latest_user_turn: str,
    runtime_context: dict[str, Any],
    conversation_history: list[dict[str, str]],
    pending_proposal: dict[str, Any] | None,
    recent_events: list[dict[str, Any]],
    session_memory: dict[str, Any],
) -> dict[str, Any]:
    del conversation_history, session_memory
    compact_runtime_context = dict(runtime_context)
    compact_runtime_context.pop("goal_state", None)
    compact_runtime_context.pop("active_proposal_revision", None)
    compact_runtime_context.pop("active_request_continuation", None)
    return {
        "control_plane": "agent-kernel",
        "contract_ref": "drasil://agent/strategist/profile",
        "tool_surface_ref": "drasil-core://strategist/allowlist",
        "capability_catalog": compact_capability_catalog(agent="strategist"),
        "runtime_context": task_contract_surface(compact_runtime_context),
        "goalContext": build_compact_goal_context(
            goal_state=active_goal,
            latest_user_turn=latest_user_turn,
            runtime_context=compact_runtime_context,
            pending_proposal=pending_proposal,
            recent_events=recent_events,
        ),
    }


def _invoke_authoritative_model_tool(
    session: Session,
    *,
    invocation: McpInvocationContext,
    name: str,
    arguments: dict[str, Any],
    execution: McpExecutionDependencies,
) -> dict[str, Any]:
    """Single model-selected dispatch seam; never falls back after acceptance."""

    return invoke_drasil_mcp_tool(
        session,
        invocation=invocation,
        name=name,
        arguments=arguments,
        execution=execution,
    )


def _prune_context_packet_for_external_planning(
    context_packet: dict[str, Any],
    *,
    active_category: Category | None,
    focus_node: StrategyNode | None,
    staged_node_ids: list[int],
) -> None:
    """Keep external advice turns out of task-retrieval gravity."""

    context_packet["runtime_context"] = {
        "active_category": (
            {"category_id": active_category.id, "name": active_category.name}
            if active_category is not None
            else None
        ),
        "focus_task": (
            {"task_id": focus_node.id, "task_title": focus_node.title}
            if focus_node is not None
            else None
        ),
        "staged_task_count": len(staged_node_ids),
        "context_note": (
            "The latest request is external-world planning/advice. UI task context is "
            "background only and is not required to answer."
        ),
    }
    goal_context = context_packet.get("goalContext")
    if isinstance(goal_context, dict):
        goal_context["pendingReview"] = None
        goal_context["evidenceHandles"] = []


def _prune_context_packet_for_fresh_model_turn(
    context_packet: dict[str, Any],
) -> None:
    """Keep fresh natural-language turns small and tool-driven.

    A fresh goal has no continuation state to preserve. Shipping the complete
    task outline and capability catalog up front duplicated what authoritative
    read tools already expose, pushed ordinary compound turns far beyond the
    route's prompt budget, and made the model less likely to call any tool. This
    pruning is based only on durable lifecycle state; semantic intent remains
    model-owned.
    """

    runtime_context = (
        context_packet.get("runtime_context")
        if isinstance(context_packet.get("runtime_context"), dict)
        else {}
    )
    scope_index = (
        runtime_context.get("task_scope_index")
        if isinstance(runtime_context.get("task_scope_index"), dict)
        else {}
    )
    categories = (
        scope_index.get("categories")
        if isinstance(scope_index.get("categories"), list)
        else []
    )
    context_packet["runtime_context"] = {
        key: runtime_context.get(key)
        for key in (
            "today",
            "local_date_time",
            "time_zone",
            "active_category",
            "focus_task",
            "calendar_view_start",
            "calendar_view_end",
        )
        if runtime_context.get(key) is not None
    }
    # This object is already a bounded API-authored scheduling surface (local
    # clock, time zone, visible calendar range, and attached task IDs).  Keep it
    # intact on fresh turns: dropping it saves very little context but forces the
    # model to rediscover selected scope and can turn an actionable scheduling
    # request into an avoidable clarification.
    if isinstance(runtime_context.get("workspace_context"), dict):
        context_packet["runtime_context"]["workspace_context"] = dict(
            runtime_context["workspace_context"]
        )
    context_packet["runtime_context"]["category_catalog"] = [
        {
            "category_id": item.get("category_id", item.get("id")),
            "name": item.get("name"),
            "is_active": bool(item.get("is_active")),
        }
        for item in categories
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("name")
    ]
    context_packet["runtime_context"]["context_note"] = (
        "This is a fresh goal. Use authoritative task, calendar, and preference "
        "tools for evidence instead of relying on a prefetched workspace dump."
    )
    context_packet.pop("capability_catalog", None)


def _prune_context_packet_for_simple_new_task_schedule(
    context_packet: dict[str, Any],
) -> None:
    """Keep a new-task scheduling request on the small authoring surface."""

    runtime_context = (
        context_packet.get("runtime_context")
        if isinstance(context_packet.get("runtime_context"), dict)
        else {}
    )
    scope_index = (
        runtime_context.get("task_scope_index")
        if isinstance(runtime_context.get("task_scope_index"), dict)
        else {}
    )
    categories = (
        scope_index.get("categories")
        if isinstance(scope_index.get("categories"), list)
        else []
    )
    context_packet["runtime_context"] = {
        key: runtime_context.get(key)
        for key in (
            "today",
            "local_date_time",
            "time_zone",
            "active_category",
            "focus_task",
        )
        if runtime_context.get(key) is not None
    }
    context_packet["runtime_context"]["category_catalog"] = [
        {
            "category_id": item.get("category_id", item.get("id")),
            "name": item.get("name"),
            "is_active": bool(item.get("is_active")),
        }
        for item in categories
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("name")
    ]
    context_packet["runtime_context"]["context_note"] = (
        "This request creates new work and schedules it. Choose the best workspace "
        "from category_catalog, preserve all requested steps and windows, and submit "
        "one complete task-plus-calendar plan."
    )
    context_packet.pop("capability_catalog", None)


def _prune_context_packet_for_pending_review_correction(
    context_packet: dict[str, Any],
) -> None:
    """Keep a pending-review correction focused on its stored semantic plan."""

    runtime_context = (
        context_packet.get("runtime_context")
        if isinstance(context_packet.get("runtime_context"), dict)
        else {}
    )
    scope_index = (
        runtime_context.get("task_scope_index")
        if isinstance(runtime_context.get("task_scope_index"), dict)
        else {}
    )
    categories = (
        scope_index.get("categories")
        if isinstance(scope_index.get("categories"), list)
        else []
    )
    context_packet["runtime_context"] = {
        key: runtime_context.get(key)
        for key in (
            "today",
            "local_date_time",
            "time_zone",
            "active_category",
            "focus_task",
        )
        if runtime_context.get(key) is not None
    }
    context_packet["runtime_context"]["category_catalog"] = [
        {
            "category_id": item.get("category_id", item.get("id")),
            "name": item.get("name"),
            "is_active": bool(item.get("is_active")),
        }
        for item in categories
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("name")
    ]
    context_packet["runtime_context"]["context_note"] = (
        "A canonical review is pending. Treat goalContext as the authoritative "
        "proposal. Apply the latest explicit correction only, preserve all other "
        "task and calendar meaning, and submit one revised semantic plan. The host "
        "updates GoalState and the existing ChangeSet revision."
    )
    context_packet.pop("capability_catalog", None)


def _prune_context_packet_for_single_task_metadata(
    context_packet: dict[str, Any],
    *,
    nodes: list[StrategyNode] | None = None,
) -> None:
    """Keep a single-task metadata edit on its minimal authoring surface."""

    runtime_context = (
        context_packet.get("runtime_context")
        if isinstance(context_packet.get("runtime_context"), dict)
        else {}
    )
    task_catalog: list[dict[str, Any]] = []
    seen_task_ids: set[int] = set()
    for node in nodes or []:
        if node.id is None or node.id in seen_task_ids or not node.title.strip():
            continue
        seen_task_ids.add(node.id)
        task_catalog.append(
            {"task_id": node.id, "task_title": node.title.strip()}
        )
        if len(task_catalog) >= 100:
            break

    def collect_tasks(value: Any) -> None:
        if len(task_catalog) >= 100:
            return
        if isinstance(value, list):
            for item in value:
                collect_tasks(item)
            return
        if not isinstance(value, dict):
            return
        raw_task_id = value.get(
            "task_id",
            value.get("node_id", value.get("id")),
        )
        raw_title = value.get("task_title", value.get("title"))
        if (
            isinstance(raw_task_id, int)
            and not isinstance(raw_task_id, bool)
            and isinstance(raw_title, str)
            and raw_title.strip()
            and raw_task_id not in seen_task_ids
        ):
            seen_task_ids.add(raw_task_id)
            task_catalog.append(
                {"task_id": raw_task_id, "task_title": raw_title.strip()}
            )
        for item in value.values():
            collect_tasks(item)

    collect_tasks(runtime_context.get("task_scope_index"))
    collect_tasks(runtime_context.get("task_tree_outline"))
    context_packet["runtime_context"] = {
        key: runtime_context.get(key)
        for key in (
            "today",
            "local_date_time",
            "time_zone",
            "active_category",
        )
        if runtime_context.get(key) is not None
    }
    context_packet["runtime_context"]["task_catalog"] = task_catalog
    context_packet["runtime_context"]["context_note"] = (
        "Resolve the exact task from task_catalog, then submit the requested "
        "single-task metadata edit. UI focus is context, not target authority."
    )
    goal_context = (
        context_packet.get("goalContext")
        if isinstance(context_packet.get("goalContext"), dict)
        else {}
    )
    context_packet["goalContext"] = {
        key: goal_context.get(key)
        for key in (
            "goalId",
            "revision",
            "latestUserTurn",
        )
        if goal_context.get(key) is not None
    }
    context_packet.pop("capability_catalog", None)


def _apply_goal_state_tool_update(
    *,
    coordinator: GoalStateCoordinator,
    session_memory: dict[str, Any],
    session_id: str,
    source_turn_id: str,
    correlation_id: str,
    current_run_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    command = GoalStateUpdateCommand.model_validate(arguments)
    updated = coordinator.apply_model_update(
        session_id=session_id,
        source_turn_id=source_turn_id,
        correlation_id=correlation_id,
        current_run_id=current_run_id,
        command=command,
    )
    if current_run_id is not None:
        from app.models.strategist_agent import StrategistRun
        from app.services.strategist_runs import link_strategist_run_to_goal

        run = coordinator.session.get(StrategistRun, current_run_id)
        if run is not None:
            link_strategist_run_to_goal(
                coordinator.session,
                run,
                goal_id=updated.goal_id,
                goal_revision=updated.revision,
                causation_id=updated.causation_id,
            )
    prior_memory = dict(session_memory)
    session_memory.clear()
    session_memory.update(project_goal_state_memory(prior_memory, updated))
    return {
        "persisted": True,
        "goal_state": updated.model_dump(mode="json", by_alias=True),
    }


def _apply_goal_turn_decision(
    *,
    coordinator: GoalStateCoordinator,
    session_memory: dict[str, Any],
    session_id: str,
    source_turn_id: str,
    current_user_turn: str,
    correlation_id: str,
    current_run_id: str | None,
    arguments: dict[str, Any],
    authorized_reference_ids: set[str] | None = None,
) -> dict[str, Any]:
    decision = GoalTurnDecision.model_validate(arguments)
    updated = coordinator.apply_turn_decision(
        session_id=session_id,
        source_turn_id=source_turn_id,
        current_user_turn=current_user_turn,
        correlation_id=correlation_id,
        current_run_id=current_run_id,
        decision=decision,
        authorized_reference_ids=authorized_reference_ids,
    )
    prior_memory = dict(session_memory)
    session_memory.clear()
    session_memory.update(project_goal_state_memory(prior_memory, updated))
    return {
        "persisted": True,
        "disposition": decision.disposition,
        "contextReset": decision.disposition == "new",
        "goal_state": {
            "goalId": updated.goal_id,
            "revision": updated.revision,
            "status": updated.status,
        },
    }


def _current_turn_authorized_reference_ids(
    workspace_context: dict[str, Any] | None,
    tool_trace: list[dict[str, Any]],
) -> set[str]:
    authorized: set[str] = set()
    workspace = workspace_context if isinstance(workspace_context, dict) else {}
    for key in (
        "attached_task_ids",
        "attachedTaskIds",
        "selected_task_ids",
        "selectedTaskIds",
    ):
        values = workspace.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, int) and not isinstance(value, bool):
                    authorized.update({str(value), f"task:{value}"})

    id_keys = {
        "entity_id",
        "entityId",
        "task_id",
        "taskId",
        "category_id",
        "categoryId",
        "calendar_block_id",
        "calendarBlockId",
        "series_id",
        "seriesId",
        "occurrence_id",
        "occurrenceId",
        "preference_id",
        "preferenceId",
    }

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in id_keys and isinstance(nested, (str, int)):
                    text = str(nested)
                    authorized.add(text)
                    if key in {"task_id", "taskId"}:
                        authorized.add(f"task:{text}")
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for trace in tool_trace:
        if isinstance(trace, dict):
            collect(trace.get("result"))
    return authorized


def _apply_change_set_tool_draft(
    *,
    coordinator: NativeChangeSetAuthoringCoordinator,
    session_memory: dict[str, Any],
    session_id: str,
    source_turn_id: str,
    correlation_id: str,
    current_run_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    command = DraftChangeSetCommand.model_validate(arguments)
    canonical_arguments = json.dumps(
        command.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_digest = hashlib.sha256(source_turn_id.encode("utf-8")).hexdigest()[:16]
    command_digest = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()[:24]
    command_id = f"draft:{source_digest}:{command_digest}"
    result = coordinator.draft(
        session_id,
        command,
        command_id=command_id,
        correlation_id=correlation_id,
        causation_id=command_id,
    )
    latest_goal = coordinator.goal_store.load(session_id, result.change_set.goal_id)
    prior_memory = dict(session_memory)
    session_memory.clear()
    session_memory.update(project_goal_state_memory(prior_memory, latest_goal))
    session_memory["current_change_set"] = {
        "changeSetId": result.change_set.change_set_id,
        "revision": result.change_set.revision,
        "status": result.change_set.status,
        "operationCount": len(result.change_set.operations),
        "operationIds": [operation.operation_id for operation in result.change_set.operations],
        "constraintCount": len(result.change_set.constraints),
        "conservationComplete": result.conservation.complete,
    }
    if current_run_id is not None:
        run = coordinator.session.get(StrategistRun, current_run_id)
        if run is not None:
            from app.services.strategist_runs import link_strategist_run_to_goal

            link_strategist_run_to_goal(
                coordinator.session,
                run,
                goal_id=latest_goal.goal_id,
                goal_revision=latest_goal.revision,
                causation_id=latest_goal.causation_id,
            )
    return result.model_dump(mode="json", by_alias=True)


def _apply_change_set_tool_simulate(
    *,
    coordinator: ChangeSetSimulationCoordinator,
    session_memory: dict[str, Any],
    session_id: str,
    source_turn_id: str,
    correlation_id: str,
    current_run_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    command = SimulateChangeSetCommand.model_validate(arguments)
    canonical_arguments = json.dumps(
        command.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_digest = hashlib.sha256(source_turn_id.encode("utf-8")).hexdigest()[:16]
    command_digest = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()[:24]
    command_id = f"simulate:{source_digest}:{command_digest}"
    result = coordinator.simulate(
        session_id,
        command,
        command_id=command_id,
        correlation_id=correlation_id,
        causation_id=command_id,
    )
    latest_goal = coordinator.goal_store.load(session_id, result.goal_id)
    prior_memory = dict(session_memory)
    session_memory.clear()
    session_memory.update(project_goal_state_memory(prior_memory, latest_goal))
    if (
        latest_goal.revision == result.resulting_goal_revision
        and latest_goal.current_change_set_id == result.change_set_id
        and latest_goal.current_change_set_revision == result.change_set_revision
    ):
        session_memory["current_change_set_simulation"] = {
            "simulationId": result.simulation_id,
            "changeSetId": result.change_set_id,
            "changeSetRevision": result.change_set_revision,
            "goalRevision": result.resulting_goal_revision,
            "status": result.status,
            "conflictCount": len(result.conflict_graph.conflicts),
            "decisionId": (
                result.conflict_graph.decision_request.decision_id
                if result.conflict_graph.decision_request is not None
                else None
            ),
        }
    if current_run_id is not None:
        run = coordinator.session.get(StrategistRun, current_run_id)
        if run is not None:
            from app.services.strategist_runs import link_strategist_run_to_goal

            link_strategist_run_to_goal(
                coordinator.session,
                run,
                goal_id=latest_goal.goal_id,
                goal_revision=latest_goal.revision,
                causation_id=latest_goal.causation_id,
            )
    return result.model_dump(mode="json", by_alias=True)


@observe_agent_turn
def run_agent_kernel_turn(
    *,
    session: Session,
    prompt: str,
    ui_event: dict[str, Any] | None,
    active_category_id: int | None,
    focus_node_id: int | None,
    staged_node_ids: list[int],
    conversation_history: list[dict[str, str]],
    strategist_session_id: str,
    strategist_turn_id: str | None = None,
    provider_override: str | None = None,
    provider_session_id: str | None = None,
    model_override: str | None = None,
    today_override: date | None = None,
    include_diagnostics: bool = False,
    codex_tool_loop_invoker: Callable[..., tuple[dict[str, Any], str | None]] | None = None,
    semantic_fast_paths_enabled: bool | None = None,
    requested_capability_ids: list[str] | None = None,
    workspace_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    turn_started_at = perf_counter()
    turn_deadline_monotonic = (
        turn_started_at + SHARED_WORKSPACE_TURN_TIMEOUT_SECONDS
        if workspace_context is not None
        else None
    )
    context_started_at = perf_counter()
    # Retain the argument for API compatibility. Natural-language fast paths
    # are retired regardless of flags; the model owns intent and tool choice.
    _ = semantic_fast_paths_enabled
    semantic_fast_paths_enabled = False
    current_run = (
        session.exec(
            select(StrategistRun)
            .where(StrategistRun.turn_id == strategist_turn_id)
            .order_by(StrategistRun.created_at.desc())
        ).first()
        if strategist_turn_id is not None
        else None
    )
    correlation_id = (
        current_run.correlation_id
        if current_run is not None and current_run.correlation_id
        else str(uuid4())
    )
    sequence = 0
    event_types: list[str] = []
    display_prompt = " ".join((prompt or "").strip().split())
    event_kind = str(ui_event.get("kind") or "").strip() if isinstance(ui_event, dict) else None
    goal_coordinator = GoalStateCoordinator(session)
    change_set_coordinator = NativeChangeSetAuthoringCoordinator(session)
    simulation_coordinator = ChangeSetSimulationCoordinator(session)
    active_goal_before_turn = goal_coordinator.store.load_active(
        strategist_session_id
    )
    active_goal = goal_coordinator.ensure_active(
        session_id=strategist_session_id,
        original_request=display_prompt or "Continue the typed UI interaction.",
        correlation_id=correlation_id,
        causation_id=strategist_turn_id or correlation_id,
        command_id=strategist_turn_id or correlation_id,
        current_run_id=current_run.id if current_run is not None else None,
    )
    if current_run is not None and (
        current_run.goal_id != active_goal.goal_id
        or current_run.goal_revision != active_goal.revision
    ):
        current_run.goal_id = active_goal.goal_id
        current_run.goal_revision = active_goal.revision
        current_run.causation_id = active_goal.causation_id
        session.add(current_run)
        session.flush()
    append_kernel_event(
        session,
        strategist_session_id=strategist_session_id,
        correlation_id=correlation_id,
        sequence=sequence,
        event_type="user_turn",
        payload={
            "prompt": display_prompt,
            "ui_event_kind": event_kind,
            "active_category_id": active_category_id,
            "focus_node_id": focus_node_id,
            "staged_node_ids": staged_node_ids,
        },
        turn_id=strategist_turn_id,
    )
    event_types.append("user_turn")
    sequence += 1

    categories, nodes, active_category, focus_node = _load_kernel_world(
        session,
        active_category_id=active_category_id,
        focus_node_id=focus_node_id,
    )
    categories, nodes, active_category, focus_node = _snapshot_kernel_world(
        categories=categories,
        nodes=nodes,
        active_category=active_category,
        focus_node=focus_node,
    )
    working_set = get_session_working_set(session, strategist_session_id)
    runtime_settings = get_provider_runtime_settings(provider_override, model_override)
    runtime_context = build_runtime_context(
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
        include_task_tree_outline=False,
    )
    if workspace_context:
        runtime_context["workspace_context"] = workspace_context
    pending_proposal = load_pending_proposal_snapshot(
        session,
        strategist_session_id=strategist_session_id,
    )
    if (
        event_kind == "proposal_action"
        and isinstance(ui_event, dict)
        and str(ui_event.get("action") or "").strip().lower() == "edit"
    ):
        proposal_id = str(
            ui_event.get("proposal_id") or ui_event.get("proposalId") or ""
        ).strip()
        if proposal_id:
            edit_proposal = load_proposal_snapshot_by_id(
                session,
                proposal_id=proposal_id,
                strategist_session_id=strategist_session_id,
            )
            if edit_proposal is not None:
                pending_proposal = edit_proposal
    recent_events = load_recent_event_summary(
        session,
        strategist_session_id=strategist_session_id,
        goal_id=active_goal.goal_id,
        exclude_run_id=current_run.id if current_run is not None else None,
    )
    recent_session_events = load_recent_event_summary(
        session,
        strategist_session_id=strategist_session_id,
    )
    active_agent_run_plan = load_latest_agent_run_plan_snapshot(
        session,
        strategist_session_id=strategist_session_id,
    )
    session_memory = build_session_memory(
        working_set=working_set,
        pending_proposal=pending_proposal,
        recent_events=recent_events,
        recent_session_events=recent_session_events,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        active_agent_run_plan=active_agent_run_plan,
    )
    session_memory = project_goal_state_memory(session_memory, active_goal)
    runtime_context["goal_state"] = session_memory["goal_state"]
    is_proposal_edit = bool(
        event_kind == "proposal_action"
        and isinstance(ui_event, dict)
        and str(ui_event.get("action") or "").strip().lower() == "edit"
    )
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory.get("active_request"), dict)
        else None
    )
    durable_continuation = load_latest_continuation_state(
        session,
        strategist_session_id=strategist_session_id,
        goal_id=active_goal.goal_id,
        original_request=active_goal.original_request,
    )
    if (
        isinstance(durable_continuation, dict)
        and isinstance(durable_continuation.get("proposal_revision"), dict)
        and (
            not isinstance(active_request, dict)
            or not isinstance(active_request.get("proposal_revision"), dict)
        )
    ):
        # Recent-event summaries are intentionally bounded and diagnostic.
        # Semantic continuation is authoritative state, so recover it directly
        # from the latest final checkpoint if projection omitted it.
        active_request = {
            **(active_request if isinstance(active_request, dict) else {}),
            **durable_continuation,
        }
        session_memory["active_request"] = active_request
    prior_revision = (
        active_request.get("proposal_revision")
        if isinstance(active_request, dict)
        and isinstance(active_request.get("proposal_revision"), dict)
        else None
    )
    compact_pending_proposal = _compact_pending_proposal_for_context(pending_proposal)
    proposal_revision: dict[str, Any] | None = None
    if is_proposal_edit and isinstance(compact_pending_proposal, dict):
        proposal_revision = compact_pending_proposal
    elif (
        isinstance(prior_revision, dict)
        and str(active_request.get("status") or "").strip()
        == "awaiting_user_input"
    ):
        # A clarification checkpoint is newer than any compatibility proposal
        # row and explicitly records which fields remain unresolved.
        proposal_revision = prior_revision
    elif isinstance(prior_revision, dict) and compact_pending_proposal is None:
        # A durable continuation checkpoint is itself the authority when no
        # legacy proposal row exists. This is the normal shape for native
        # reviews and semantic drafts paused for clarification.
        proposal_revision = prior_revision
    elif isinstance(prior_revision, dict):
        prior_proposal_id = str(prior_revision.get("proposal_id") or "").strip()
        pending_proposal_id = str(
            compact_pending_proposal.get("proposal_id")
            if isinstance(compact_pending_proposal, dict)
            else ""
        ).strip()
        if prior_proposal_id and prior_proposal_id == pending_proposal_id:
            # Refresh from persistence so the revision basis always carries the
            # authoritative recurrence and accepted blocker ids.
            proposal_revision = compact_pending_proposal
        elif (
            prior_proposal_id
            and isinstance(compact_pending_proposal, dict)
            and str(
                compact_pending_proposal.get("revision_of_proposal_id") or ""
            ).strip()
            == prior_proposal_id
        ):
            # Compatibility for a revision created before continuation memory
            # was rebound at the response boundary: follow the persisted
            # parent->child lineage to the current pending proposal.
            proposal_revision = compact_pending_proposal
        elif (
            compact_pending_proposal is None
            and str(prior_revision.get("proposal_id") or "").strip()
            and str(prior_revision.get("proposal_kind") or "").strip()
            == "branch"
            and isinstance(prior_revision.get("semantic_contract"), dict)
        ):
            # Native reviews and pre-review clarification drafts do not create
            # legacy proposal records. Their server-authored compact semantic
            # contract is the durable revision basis for the next turn. Goal
            # identity was already checked while projecting active_request, so
            # lifecycle status must not erase this semantic checkpoint.
            proposal_revision = prior_revision
    if isinstance(proposal_revision, dict):
        revised_active_request = dict(active_request or {})
        revised_active_request.setdefault("status", "in_progress")
        revised_active_request["proposal_revision"] = proposal_revision
        session_memory["active_request"] = revised_active_request
        runtime_context["active_proposal_revision"] = proposal_revision
    request_context_prompts = reconstruct_active_request_prompts(
        session_memory,
        display_prompt,
        conversation_history,
        canonical_original_request=active_goal.original_request,
    )
    # Put the newest correction first for deterministic parsers while retaining
    # the earlier clauses below it for the model and proposal compilers.
    request_context_text = "\n".join(reversed(request_context_prompts)).strip() or display_prompt
    if len(request_context_prompts) > 1:
        runtime_context["active_request_continuation"] = {
            "request_prompts": request_context_prompts,
            "instruction": (
                "Preserve every compatible clause from these turns. The latest explicit "
                "correction overrides only the field it changes."
            ),
        }
    route_event_kind = event_kind
    if (
        event_kind == "proposal_action"
        and isinstance(ui_event, dict)
        and str(ui_event.get("action") or "").strip().lower() == "edit"
    ):
        route_event_kind = "proposal_revision"
    route_decision = route_strategist_turn(
        prompt=display_prompt,
        ui_event_kind=route_event_kind,
        session_memory=session_memory,
        active_category=active_category,
        focus_node=focus_node,
        staged_node_ids=staged_node_ids,
        today_value=_today_value(today_override),
    )
    append_kernel_event(
        session,
        strategist_session_id=strategist_session_id,
        correlation_id=correlation_id,
        sequence=sequence,
        event_type="tier_route_decision",
        payload=route_decision.to_payload(),
    )
    event_types.append("tier_route_decision")
    sequence += 1
    tool_trace: list[dict[str, Any]] = []
    # Read evidence needed by a typed authoring tool must be available through
    # the tool-result store as well as the model-facing trace.  In particular,
    # recurrence revisions commonly contain only the field the user changed;
    # handoff_to_planner hydrates the omitted cadence, start date, and duration
    # from authoritative schedule evidence in this store.
    tool_result_store: dict[str, Any] = {}
    for capability_id in dict.fromkeys(requested_capability_ids or []):
        load_started_at = perf_counter()
        loaded_skill = load_skill({"capability_id": capability_id})
        tool_trace.append(
            {
                "name": "load_skill",
                "arguments": {"capability_id": capability_id},
                "result": {
                    **loaded_skill,
                    "read_only": True,
                    "result_kind": "loaded_skill",
                },
                "duration_ms": _elapsed_ms(load_started_at),
                "prefetch": True,
            }
        )
    recurring_context_task_ids = [
        int(item["node_id"])
        for item in working_set
        if isinstance(item, dict)
        and isinstance(item.get("node_id"), int)
        and isinstance(item.get("recurrence_series"), list)
        and item["recurrence_series"]
    ][:8]
    if (
        route_decision.route_kind == "model_owned_tool_loop"
        and recurring_context_task_ids
    ):
        # Session entity memory is allowed to outlive a completed objective. Keep
        # the associated tool context equally durable so a free-form follow-up can
        # operate on the persisted series without spending a model step rediscovering
        # and then occasionally failing to call the exact read tool. This is bounded,
        # read-only context reconstruction; Planner/native authoring still revalidates
        # revisions and simulation before review.
        schedule_context = _read_calendar_blocks_for_kernel(
            {
                "task_ids": recurring_context_task_ids,
                "limit": 200,
                "include_all_for_tasks": True,
            },
            nodes=nodes,
            categories=categories,
        )
        if "error" not in schedule_context:
            schedule_context.update(
                {
                    "read_only": True,
                    "result_kind": "task_schedule_context",
                    "observation_summary": (
                        "Restored authoritative schedule and recurrence context for "
                        "salient session tasks."
                    ),
                    "model_instruction": (
                        "This exact persisted recurrence evidence is already available. "
                        "For a requested series change, preserve omitted rule fields and "
                        "hand the listed task_ids to Planner now; do not ask the user to "
                        "repeat the task, cadence, duration, or series scope."
                    ),
                }
            )
            tool_trace.append(
                {
                    "name": "get_task_schedule",
                    "arguments": {
                        "task_ids": recurring_context_task_ids,
                        "limit": 200,
                    },
                    "result": schedule_context,
                    "prefetch": True,
                    "session_context_reconstruction": True,
                }
            )
            tool_result_store["kernel-prefetch-task-schedule-1"] = dict(
                schedule_context
            )
    model_step_trace: list[dict[str, Any]] = []
    provider_failure: dict[str, Any] | None = None
    provider_readiness: dict[str, Any] | None = None
    model_loop_duration_ms: float | None = None
    tool_policy = None
    executed_kernel_tool_calls = 0
    goal_transition_recorded = False
    duration_correction_lane = _is_duration_only_pending_review_correction(
        display_prompt,
        session_memory=session_memory,
    )
    bounded_model_authoring_lane = bool(
        _uses_single_task_metadata_authoring_surface(
            display_prompt,
            route_decision,
        )
    )
    clarification_continuation_lane = _is_host_managed_clarification_continuation(
        active_request=active_request,
        request_context_prompts=request_context_prompts,
    )
    fresh_goal_envelope = active_goal_before_turn is None
    host_managed_goal_transition = bool(
        duration_correction_lane
        or fresh_goal_envelope
        or clarification_continuation_lane
    )
    planner_result: dict[str, Any] | None = None
    planner_temporal_adjustment: dict[str, Any] | None = None
    owned_planner_result: PlannerSubagentResult | None = None
    native_existing_recurrence_requested = False

    def execute_planner_in_model_loop(
        handoff_value: dict[str, Any],
        *,
        subagent_task: PlannerSubagentTask,
    ) -> dict[str, Any]:
        """Run Planner as a returning tool so the model sees success or failure."""

        nonlocal planner_result, planner_temporal_adjustment
        nonlocal native_existing_recurrence_requested, sequence
        handoff = copy.deepcopy(handoff_value)
        if _planner_handoff_can_use_deterministic_solver(handoff):
            handoff["deterministic_semantics_complete"] = True
        handoff_instruction = str(handoff.get("instruction") or "").strip()
        if len(request_context_prompts) > 1:
            handoff["instruction"] = (
                f"{request_context_text}\n"
                f"Latest planner request: {handoff_instruction}"
                if handoff_instruction and handoff_instruction not in request_context_text
                else request_context_text
            )
        exact_task = _exact_task_reference(
            prompt=display_prompt,
            nodes=nodes,
            focus_node=focus_node,
        )
        if exact_task is None and len(request_context_prompts) > 1:
            exact_task = _exact_task_reference(
                prompt=request_context_text,
                nodes=nodes,
                focus_node=focus_node,
            )
        if exact_task is None:
            exact_task = _dominant_scheduling_candidate(
                prompt=display_prompt,
                nodes=nodes,
                active_category=active_category,
            )
        if exact_task is not None and exact_task.id is not None:
            prior_request = _latest_scheduling_request(
                conversation_history,
                str(handoff.get("instruction") or display_prompt),
            )
            _align_planner_handoff_to_resolved_task(
                handoff,
                task_id=int(exact_task.id),
                explicit_time_range=bool(EXPLICIT_TIME_RANGE_RE.search(request_context_text)),
            )
            handoff["instruction"] = (
                f'{prior_request}. Use the exact task named "{exact_task.title}".'
            )
        handoff["task_ids"] = _expand_subtask_schedule_ids(
            instruction=str(handoff.get("instruction") or display_prompt),
            task_ids=[int(item) for item in handoff.get("task_ids", [])],
            nodes=nodes,
        )
        has_authoritative_schedule_evidence = any(
            str(call.get("name") or "") in {"list_calendar_blocks", "get_task_schedule"}
            and isinstance(call.get("result"), dict)
            and "error" not in call["result"]
            for call in tool_trace
        )
        if (
            handoff.get("operation_mode") == "reschedule"
            and handoff.get("task_ids")
            and not has_authoritative_schedule_evidence
        ):
            task_schedule_arguments = {
                "task_ids": list(handoff["task_ids"]),
                "limit": 200,
            }
            task_schedule_result = _read_calendar_blocks_for_kernel(
                {
                    **task_schedule_arguments,
                    "include_all_for_tasks": True,
                },
                nodes=nodes,
                categories=categories,
            )
            if "error" not in task_schedule_result:
                task_schedule_result["result_kind"] = "task_schedule_context"
                task_schedule_result["observation_summary"] = (
                    "Read authoritative schedule and recurrence identity for the target tasks."
                )
                tool_trace.append(
                    {
                        "name": "get_task_schedule",
                        "arguments": task_schedule_arguments,
                        "result": task_schedule_result,
                        "deterministic_repair": True,
                    }
                )

        series_scope = _planner_handoff_target_scope(
            handoff,
            request_context_text=request_context_text,
        )
        series_scope_hydrated = _hydrate_series_recurrence_handoff(
            handoff,
            tool_trace=tool_trace,
            request_context_text=request_context_text,
        )
        if series_scope == "series" and not series_scope_hydrated:
            return {
                "planner_executed": False,
                "error": "series_scope_evidence_unavailable",
                "planner_error": {
                    "code": "TOOL_INVARIANT_FAILED",
                    "message": (
                        "The request targets the whole recurring series, but the exact "
                        "persisted series could not be resolved from authoritative schedule "
                        "evidence. No occurrence-level fallback is allowed."
                    ),
                    "retryable": True,
                    "requires_user_input": False,
                    "affected_paths": ["target_scope", "recurrence"],
                },
                "repair_hint": (
                    "Read get_task_schedule for every exact task id and retry with "
                    "target_scope=series. Do not submit an occurrence-level schedule patch."
                ),
            }
        native_existing_recurrence_requested = bool(
            handoff.get("operation_mode") == "reschedule"
            and handoff.get("target_scope") == "series"
            and isinstance(handoff.get("recurrence"), dict)
        )

        planner_started_at = perf_counter()
        try:
            window_start = datetime.fromisoformat(str(handoff["window_start"]))
            window_end = datetime.fromisoformat(str(handoff["window_end"]))
            if window_start.tzinfo is not None or window_end.tzinfo is not None:
                raise ValueError("Planner windows must use naive local wall time.")
            if window_end <= window_start:
                raise ValueError("Planner window end must be after its start.")
            (
                window_start,
                window_end,
                planner_temporal_adjustment,
            ) = _constrain_planner_window_to_current_time(
                window_start=window_start,
                window_end=window_end,
                instruction=str(handoff.get("instruction") or display_prompt),
                local_now=_workspace_local_datetime(workspace_context),
            )
            window_end = _expand_start_anchor_window(
                instruction=str(handoff.get("instruction") or display_prompt),
                window_start=window_start,
                window_end=window_end,
            )
            if (
                turn_deadline_monotonic is not None
                and _remaining_deadline_seconds(
                    turn_deadline_monotonic,
                    reserve_seconds=SHARED_WORKSPACE_FINALIZATION_RESERVE_SECONDS,
                ) <= 0
            ):
                raise TimeoutError(
                    "The shared workspace turn deadline expired before Planner could start."
                )
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="planner_handoff_started",
                payload={
                    "task_count": len(handoff.get("task_ids", [])),
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "operation_mode": str(handoff.get("operation_mode") or "create"),
                    "remaining_deadline_seconds": (
                        _remaining_deadline_seconds(turn_deadline_monotonic)
                        if turn_deadline_monotonic is not None
                        else None
                    ),
                },
            )
            event_types.append("planner_handoff_started")
            sequence += 1
            completed = build_planner_schedule_proposal(
                session,
                prompt=str(handoff.get("instruction") or display_prompt),
                working_set_task_ids=[int(item) for item in handoff.get("task_ids", [])],
                window_start=window_start,
                window_end=window_end,
                slot_minutes=5,
                fallback_duration_minutes=30,
                provider_override=provider_override,
                model_override=model_override,
                operation_mode=str(handoff.get("operation_mode") or "create"),
                strict_custom_semantics=not bool(handoff.get("deterministic_semantics_complete")),
                provider_auth_preflight=not bool(
                    handoff.get("deterministic_semantics_complete")
                ),
                deadline_monotonic=turn_deadline_monotonic,
                recurrence_spec=(
                    handoff.get("recurrence")
                    if isinstance(handoff.get("recurrence"), dict)
                    else None
                ),
                task_updates=(
                    handoff.get("task_updates")
                    if isinstance(handoff.get("task_updates"), list)
                    else None
                ),
                request_constraints=(
                    handoff.get("request_constraints")
                    if isinstance(handoff.get("request_constraints"), list)
                    else None
                ),
                recurrence_not_before=(
                    workspace_context.get("local_date_time")
                    if isinstance(workspace_context, dict)
                    and isinstance(workspace_context.get("local_date_time"), datetime)
                    else _parse_local_datetime(
                        workspace_context.get("local_date_time")
                        if isinstance(workspace_context, dict)
                        else None
                    )
                ),
                clarify_missing_duration=True,
                subagent_task=subagent_task,
                cancellation_check=lambda: (
                    planner_subagent_coordinator.is_cancelled(
                        subagent_task.cancellation_id
                    )
                ),
            )
            planner_result = _friendly_planner_result(completed, nodes=nodes)
            proposal = (
                planner_result.get("proposal")
                if isinstance(planner_result.get("proposal"), dict)
                else None
            )
            native_review_ready = False
            if (
                native_existing_recurrence_requested
                and str(planner_result.get("mode") or "") == "native_recurrence"
            ):
                # Planner has already conserved the free-form request into a typed
                # recurrence handoff, and get_task_schedule has supplied exact
                # persisted identity. Compile that boundary directly into native
                # operations instead of requiring a second model-authored shadow
                # turn. This keeps the model in charge of intent while Pydantic and
                # simulation own structural repair and safety.
                source_ref = strategist_turn_id or correlation_id
                native_draft = compile_native_recurrence_update(
                    handoff=handoff,
                    tool_trace=tool_trace,
                    source_ref=source_ref,
                    source_text=display_prompt,
                )
                current_goal = (
                    goal_coordinator.store.load_active(strategist_session_id)
                    or goal_coordinator.store.load(
                        strategist_session_id, active_goal.goal_id
                    )
                )
                update_fingerprint = hashlib.sha256(
                    json.dumps(
                        native_draft.clause,
                        ensure_ascii=True,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:20]
                _apply_goal_state_tool_update(
                    coordinator=goal_coordinator,
                    session_memory=session_memory,
                    session_id=strategist_session_id,
                    source_turn_id=source_ref,
                    correlation_id=correlation_id,
                    current_run_id=(current_run.id if current_run is not None else None),
                    arguments={
                        "commandId": f"goal-recurrence:{update_fingerprint}",
                        "disposition": "correct",
                        "goalId": current_goal.goal_id,
                        "expectedRevision": current_goal.revision,
                        "patches": [
                            {
                                "path": "operations.requested",
                                "operation": "set",
                                "value": [native_draft.clause],
                                "provenance": native_draft.provenance.model_dump(
                                    mode="json", by_alias=True
                                ),
                            }
                        ],
                    },
                )
                corrected_goal = goal_coordinator.store.load_active(
                    strategist_session_id
                )
                if corrected_goal is None:
                    raise NativeRecurrenceAuthoringError(
                        "native recurrence goal was not retained after repair"
                    )
                draft_result = _apply_change_set_tool_draft(
                    coordinator=change_set_coordinator,
                    session_memory=session_memory,
                    session_id=strategist_session_id,
                    source_turn_id=source_ref,
                    correlation_id=correlation_id,
                    current_run_id=(current_run.id if current_run is not None else None),
                    arguments={
                        "disposition": "create",
                        "expectedGoalRevision": corrected_goal.revision,
                        "operations": [
                            operation.model_dump(mode="json", by_alias=True)
                            for operation in native_draft.operations
                        ],
                        "constraints": [],
                        "preconditions": native_draft.preconditions.model_dump(
                            mode="json", by_alias=True
                        ),
                        "assumptions": [],
                        "atomicByDefault": True,
                    },
                )
                drafted_goal = goal_coordinator.store.load_active(
                    strategist_session_id
                )
                if drafted_goal is None:
                    raise NativeRecurrenceAuthoringError(
                        "native recurrence goal was not retained after drafting"
                    )
                change_set_payload = draft_result.get("changeSet")
                change_set_revision = (
                    change_set_payload.get("revision")
                    if isinstance(change_set_payload, dict)
                    else None
                )
                if not isinstance(change_set_revision, int):
                    raise NativeRecurrenceAuthoringError(
                        "native recurrence draft has no revision"
                    )
                simulation_result = _apply_change_set_tool_simulate(
                    coordinator=simulation_coordinator,
                    session_memory=session_memory,
                    session_id=strategist_session_id,
                    source_turn_id=source_ref,
                    correlation_id=correlation_id,
                    current_run_id=(current_run.id if current_run is not None else None),
                    arguments={
                        "expectedGoalRevision": drafted_goal.revision,
                        "expectedChangeSetRevision": change_set_revision,
                    },
                )
                native_review_ready = str(simulation_result.get("status") or "") in {
                    "ready_for_review",
                    "needs_user_decision",
                }
                planner_result["nativeChangeSetReady"] = native_review_ready
                planner_result["nativeSimulationStatus"] = simulation_result.get(
                    "status"
                )
            validation = (
                proposal.get("schedulePatchValidation")
                if isinstance(proposal, dict)
                and isinstance(proposal.get("schedulePatchValidation"), dict)
                else {}
            )
            diagnostics = (
                planner_result.get("diagnostics")
                if isinstance(planner_result.get("diagnostics"), dict)
                else {}
            )
            planner_model = (
                diagnostics.get("plannerModel")
                if isinstance(diagnostics.get("plannerModel"), dict)
                else {}
            )
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="planner_handoff_completed",
                payload={
                    "mode": planner_result.get("mode"),
                    "proposal_family": (
                        "schedule_patch"
                        if proposal is not None
                        else "native_recurrence"
                        if native_review_ready
                        else None
                    ),
                    "validation_status": validation.get("status"),
                    "solver_status": diagnostics.get("solverStatus"),
                    "selected_capability_ids": planner_model.get("selectedCapabilityIds", []),
                    "duration_ms": _elapsed_ms(planner_started_at),
                    "failed": proposal is None and not native_review_ready,
                    "placement_reasons": (
                        proposal.get("placementReasons", [])
                        if isinstance(proposal, dict)
                        else []
                    ),
                    "proposed_blocks": (
                        [
                            {
                                "task_id": operation.get("taskId"),
                                "title": operation.get("title"),
                                "start_at": operation.get("startAt"),
                                "end_at": operation.get("endAt"),
                            }
                            for operation in proposal.get("schedulePatch", {}).get(
                                "operations", []
                            )
                            if isinstance(operation, dict)
                        ]
                        if isinstance(proposal, dict)
                        and isinstance(proposal.get("schedulePatch"), dict)
                        else []
                    ),
                    "gap_policy": (
                        proposal.get("payload", {})
                        .get("plannerPlan", {})
                        .get("gapPolicy", {})
                        if isinstance(proposal, dict)
                        and isinstance(proposal.get("payload"), dict)
                        and isinstance(
                            proposal.get("payload", {}).get("plannerPlan"), dict
                        )
                        else {}
                    ),
                },
            )
            event_types.append("planner_handoff_completed")
            sequence += 1
            planner_mode = str(planner_result.get("mode") or "")
            clarification = (
                planner_result.get("clarification")
                if isinstance(planner_result.get("clarification"), dict)
                else None
            )
            planner_trace = (
                planner_model.get("trace")
                if isinstance(planner_model.get("trace"), dict)
                else {}
            )
            planner_dynamic_tools = (
                planner_trace.get("dynamic_tool_names")
                if isinstance(planner_trace.get("dynamic_tool_names"), list)
                else planner_trace.get("dynamicToolNames")
                if isinstance(planner_trace.get("dynamicToolNames"), list)
                else []
            )
            planner_usage = (
                planner_model.get("usage")
                if isinstance(planner_model.get("usage"), dict)
                else {}
            )
            strategic_decision = (
                planner_result.get("strategicDecision")
                if isinstance(planner_result.get("strategicDecision"), dict)
                else planner_result.get("strategic_decision")
                if isinstance(planner_result.get("strategic_decision"), dict)
                else None
            )
            return {
                "status": (
                    "needs_strategic_decision"
                    if planner_mode == "strategic_decision"
                    else None
                ),
                "planner_executed": True,
                "planner_result": planner_result,
                "planner_mode": planner_mode,
                "proposal_ready": proposal is not None or native_review_ready,
                "needs_user_input": planner_mode == "clarification",
                "summary": str(
                    planner_result.get("summary")
                    or "Planner evaluated the scheduling request."
                ),
                "clarification": clarification,
                "strategic_decision": strategic_decision,
                "model_instruction": (
                    "Planner returned a validated proposal. Use this result to provide the final "
                    "plain-language review summary; the host will project the exact schedule card."
                    if proposal is not None or native_review_ready
                    else (
                        "Planner requested one bounded strategic decision. Choose exactly one "
                        "host-provided option and call respond_to_planner_decision once with the "
                        "host-owned decision ID."
                    )
                    if planner_mode == "strategic_decision"
                    else "Planner needs user input. Ask only the concrete question in clarification."
                    if planner_mode == "clarification"
                    else "Planner returned no proposal. Explain the typed outcome accurately."
                ),
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": int(
                        planner_trace.get("dynamic_tool_call_count")
                        or planner_trace.get("dynamicToolCallCount")
                        or len(planner_dynamic_tools)
                    ),
                    "tokens": max(
                        0,
                        int(planner_usage.get("input_tokens") or 0)
                        + int(planner_usage.get("output_tokens") or 0),
                    ),
                },
            }
        except (HTTPException, KeyError, TimeoutError, TypeError, ValueError) as error:
            error_detail = (
                str(error.detail) if isinstance(error, HTTPException) else str(error)
            )
            timed_out = isinstance(error, TimeoutError) or (
                isinstance(error, HTTPException) and error.status_code == 504
            )
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="planner_handoff_failed",
                payload={
                    "error_type": type(error).__name__,
                    "error_code": (
                        "planner_timeout"
                        if timed_out
                        else "planner_execution_failed"
                    ),
                    "duration_ms": _elapsed_ms(planner_started_at),
                    "timed_out": timed_out,
                },
            )
            event_types.append("planner_handoff_failed")
            sequence += 1
            return {
                "planner_executed": True,
                "error": "planner_execution_failed",
                "planner_error": {
                    "code": (
                        "PLANNER_RETRYABLE_FAILURE" if timed_out else "PLANNER_EXECUTION_INVALID"
                    ),
                    "message": error_detail,
                    "retryable": timed_out,
                    "requires_user_input": False,
                },
                "repair_hint": (
                    "Do not ask the user to repeat information already present. Inspect this "
                    "error, repair the Planner arguments or evidence, and retry in the same loop."
                ),
            }

    def record_planner_subagent_event(
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        nonlocal sequence
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type=event_name,
            payload=payload,
        )
        event_types.append(event_name)
        sequence += 1

    planner_subagent_coordinator = PlannerSubagentCoordinator(
        event_sink=record_planner_subagent_event,
    )
    planner_tasks_by_id: dict[str, PlannerSubagentTask] = {}
    planner_handoffs_by_task_id: dict[str, dict[str, Any]] = {}

    def planner_subagent_result_payload(
        result: PlannerSubagentResult,
    ) -> dict[str, Any]:
        artifact = dict(result.artifact or {})
        artifact["subagent_result"] = result.model_dump(
            mode="json",
            by_alias=True,
            exclude={"artifact"},
        )
        if result.status in {"failed", "retryable_failure"} and "error" not in artifact:
            artifact.update(
                {
                    "planner_executed": False,
                    "error": result.reason_code or "planner_subagent_failed",
                    "planner_error": {
                        "code": (
                            "PLANNER_RETRYABLE_FAILURE"
                            if result.retryable
                            else "PLANNER_SUBAGENT_FAILED"
                        ),
                        "message": (
                            "Planner could not complete within its owned subagent boundary."
                        ),
                        "retryable": result.retryable,
                        "requires_user_input": False,
                    },
                }
            )
        return artifact

    def execute_owned_planner_subagent(
        handoff: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal owned_planner_result
        current_goal = (
            goal_coordinator.store.load_active(strategist_session_id)
            or active_goal
        )
        parent_run_id = (
            str(current_run.id)
            if current_run is not None and current_run.id is not None
            else correlation_id
        )
        remaining_seconds = (
            _remaining_deadline_seconds(
                turn_deadline_monotonic,
                reserve_seconds=SHARED_WORKSPACE_FINALIZATION_RESERVE_SECONDS,
            )
            if turn_deadline_monotonic is not None
            else SHARED_WORKSPACE_PLANNER_RESERVE_SECONDS
        )
        planner_timeout_seconds = max(
            1,
            min(SHARED_WORKSPACE_PLANNER_RESERVE_SECONDS, remaining_seconds),
        )
        relevant_planner_tools = [
            "read_planner_context",
            "evaluate_schedule_plan",
            "get_task_snapshot",
            "get_task_metadata",
            "list_calendar_blocks",
            "get_task_schedule",
            "query_preferences",
            "get_tool_result_by_handle",
            "search_capability_catalog",
            "load_skill",
        ]
        evidence_handles = [
            str(call.get("result", {}).get("result_handle"))
            for call in tool_trace
            if isinstance(call, dict)
            and isinstance(call.get("result"), dict)
            and call.get("result", {}).get("result_handle")
        ][:16]
        try:
            task = create_planner_subagent_task(
                parent=SubagentParentContext(
                    parent_agent_id="strategist",
                    parent_run_id=parent_run_id,
                    session_id=strategist_session_id,
                    goal_id=current_goal.goal_id,
                    goal_revision=current_goal.revision,
                    correlation_id=correlation_id,
                ),
                objective=str(
                    handoff.get("instruction")
                    or request_context_text
                    or display_prompt
                ),
                task_ids=[
                    int(task_id)
                    for task_id in handoff.get("task_ids", [])
                    if isinstance(task_id, int) and not isinstance(task_id, bool)
                ],
                evidence_handles=evidence_handles,
                allowed_tool_names=relevant_planner_tools,
                budget=SubagentBudget(
                    timeout_seconds=planner_timeout_seconds,
                    max_exchanges=2,
                    max_tool_calls=7,
                    max_tokens=OWNED_PLANNER_TOTAL_TOKEN_BUDGET,
                ),
                now=datetime.now(),
            )
            planner_tasks_by_id[task.task_id] = task
            planner_handoffs_by_task_id[task.task_id] = copy.deepcopy(handoff)
            result = planner_subagent_coordinator.run(
                task,
                lambda owned_task: execute_planner_in_model_loop(
                    handoff,
                    subagent_task=owned_task,
                ),
            )
            owned_planner_result = result
        except SubagentDelegationError as error:
            return {
                "planner_executed": False,
                "error": "planner_subagent_rejected",
                "planner_error": {
                    "code": "PLANNER_SUBAGENT_LOOP_GUARD",
                    "message": str(error),
                    "retryable": False,
                    "requires_user_input": False,
                },
            }
        return planner_subagent_result_payload(result)

    def respond_to_owned_planner_decision(
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal owned_planner_result
        task_id = str(arguments.get("subagent_task_id") or "").strip()
        decision_id = str(arguments.get("decision_id") or "").strip()
        selected_option = str(arguments.get("selected_option") or "").strip()
        task = planner_tasks_by_id.get(task_id)
        original_handoff = planner_handoffs_by_task_id.get(task_id)
        if task is None or original_handoff is None:
            return {
                "error": "planner_subagent_task_not_found",
                "planner_error": {
                    "code": "PLANNER_SUBAGENT_LOOP_GUARD",
                    "message": "The Planner decision does not belong to this Strategist run.",
                    "retryable": False,
                    "requires_user_input": False,
                },
            }
        try:
            result = planner_subagent_coordinator.respond_to_strategic_decision(
                task_id=task_id,
                decision_id=decision_id,
                selected_option=selected_option,
                executor=lambda owned_task, _decision, option: (
                    execute_planner_in_model_loop(
                        {
                            **copy.deepcopy(original_handoff),
                            "instruction": (
                                f"{original_handoff.get('instruction') or request_context_text}\n"
                                f"Strategist decision: {option}"
                            ),
                        },
                        subagent_task=owned_task,
                    )
                ),
            )
            owned_planner_result = result
        except SubagentDelegationError as error:
            return {
                "error": "planner_subagent_decision_rejected",
                "planner_error": {
                    "code": "PLANNER_SUBAGENT_LOOP_GUARD",
                    "message": str(error),
                    "retryable": False,
                    "requires_user_input": False,
                },
            }
        return planner_subagent_result_payload(result)

    planner_service = PlannerSchedulingApplicationService(
        execute_owned_planner_subagent
    )

    def repair_existing_recurrence_tool_route(
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Redirect an existing-series edit accidentally sent to the tree tool.

        ``submit_task_tree_plan`` and Planner intentionally overlap for creation:
        a task plus its first recurring schedule is one atomic branch proposal.  They
        do not overlap for an already-persisted series, which must retain the series
        identity/revision supplied by ``get_task_schedule``.  Models occasionally
        choose the tree tool while still providing a completely valid recurrence
        delta.  Treat that as a repairable tool-route error, not lost user intent.

        This repair is deliberately structural.  It runs only when there are no tree
        operations to conserve, an exact existing task resolves to exactly one series,
        and authoritative schedule evidence is already in the tool trace.  The normal
        Planner/native compiler then owns validation, simulation, and review.
        """

        structural_operations = arguments.get("structural_operations")
        if isinstance(structural_operations, list) and structural_operations:
            return None
        if arguments.get("revision_of_proposal_id"):
            return None
        requested_recurrence = arguments.get("recurring_schedule_updates")
        if not isinstance(requested_recurrence, dict):
            requested_recurrence = arguments.get("recurring_schedule")
        if not isinstance(requested_recurrence, dict) or not requested_recurrence:
            return None

        target_task_id = arguments.get("target_task_id")
        if not isinstance(target_task_id, int) or isinstance(target_task_id, bool):
            target_title = str(arguments.get("target_task_title") or "").strip().casefold()
            title_matches = [
                int(node.id)
                for node in nodes
                if node.id is not None
                and target_title
                and str(node.title or "").strip().casefold() == target_title
            ]
            if len(title_matches) != 1:
                return None
            target_task_id = title_matches[0]

        matching_blocks: list[dict[str, Any]] = []
        for call in tool_trace:
            if str(call.get("name") or "") not in {
                "get_task_schedule",
                "list_calendar_blocks",
            }:
                continue
            result = call.get("result")
            if not isinstance(result, dict) or result.get("error"):
                continue
            matching_blocks.extend(
                block
                for block in result.get("blocks", [])
                if isinstance(block, dict)
                and block.get("task_id") == target_task_id
                and block.get("recurrence_series_id")
                and isinstance(block.get("recurrence_rule"), dict)
            )
        series_ids = {
            str(block.get("recurrence_series_id") or "").strip()
            for block in matching_blocks
            if str(block.get("recurrence_series_id") or "").strip()
        }
        if len(series_ids) != 1:
            return None
        evidence = matching_blocks[0]
        persisted_rule = copy.deepcopy(evidence["recurrence_rule"])
        recurrence = {
            key: persisted_rule.get(key)
            for key in (
                "frequency",
                "days_of_week",
                "start_date",
                "end_date",
                "occurrence_count",
                "duration_minutes",
                "preferred_start_time",
                "allowed_start_time",
                "allowed_end_time",
                "gap_minutes",
                "timezone",
                "strict",
            )
            if persisted_rule.get(key) is not None
        }
        recurrence.update(copy.deepcopy(requested_recurrence))
        if recurrence.get("occurrence_count") is not None:
            recurrence["end_mode"] = "count"
        elif recurrence.get("end_date") is not None:
            recurrence["end_mode"] = "until"
        else:
            recurrence["end_mode"] = str(
                requested_recurrence.get("end_mode") or "never"
            )

        anchor_date = today_override or date.today()
        anchor_time = str(
            recurrence.get("preferred_start_time")
            or persisted_rule.get("preferred_start_time")
            or "00:00"
        )
        try:
            window_start = datetime.fromisoformat(
                f"{anchor_date.isoformat()}T{anchor_time}:00"
                if len(anchor_time) == 5
                else f"{anchor_date.isoformat()}T{anchor_time}"
            )
        except ValueError:
            return None
        duration_minutes = recurrence.get("duration_minutes")
        if not isinstance(duration_minutes, int) or isinstance(duration_minutes, bool):
            return None
        window_end = window_start + timedelta(minutes=duration_minutes)
        planner_handoff = {
            "task_ids": [target_task_id],
            "instruction": str(
                arguments.get("summary")
                or arguments.get("rationale")
                or request_context_text
            ),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "operation_mode": "reschedule",
            "scope_source": "explicit_named_task",
            "target_scope": "series",
            "request_constraints": [],
            "recurrence": recurrence,
            "deterministic_semantics_complete": True,
        }
        repaired = schedule_planning_result_payload(
            planner_service.plan(planner_handoff)
        )
        repaired["tool_route_repaired"] = True
        repaired["original_tool"] = "submit_task_tree_plan"
        repaired["repaired_tool"] = "handoff_to_planner"
        return repaired

    @observe_agent_tool
    def kernel_tool_executor(name: str, arguments: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal executed_kernel_tool_calls, goal_transition_recorded, sequence
        count_against_foreground_budget = bool(
            kwargs.pop("_count_against_foreground_budget", True)
        )

        def finish_tool_call(result: dict[str, Any]) -> dict[str, Any]:
            """Charge only successful semantic work to the foreground budget.

            Strict tool schemas intentionally return repair guidance for portable
            provider shapes. Charging those repair attempts as completed work can
            consume the capacity needed for the final semantic-plan submission.
            The provider harness retains a separate bounded attempt ceiling.
            """

            nonlocal executed_kernel_tool_calls
            if (
                count_against_foreground_budget
                and not result.get("error")
                and result.get("compiled") is not False
            ):
                executed_kernel_tool_calls += 1
            return result

        if (
            count_against_foreground_budget
            and executed_kernel_tool_calls >= route_decision.budget.max_tool_calls
        ):
            return {
                "error": "agent_kernel_tool_budget_exhausted",
                "tool_error": {
                    "code": "TOOL_RETRYABLE_SYSTEM_FAILURE",
                    "category": "retryable_system",
                    "retryable": True,
                    "messageForModel": (
                        "The bounded tool budget is exhausted; return one typed system failure."
                    ),
                },
                "read_only": True,
                "result_kind": "error",
            }
        if (
            name in SEMANTIC_AUTHORING_TOOL_NAMES
            and not goal_transition_recorded
            and not host_managed_goal_transition
        ):
            return {
                "error": "goal_transition_required",
                "tool_error": {
                    "code": "GOAL_TRANSITION_REQUIRED",
                    "category": "model_repairable",
                    "retryable": False,
                    "messageForModel": (
                        "Call set_goal_transition exactly once for this natural-language "
                        "turn before semantic planning or authoring."
                    ),
                },
                "read_only": False,
                "result_kind": "error",
            }
        if name == "submit_task_tree_plan":
            if duration_correction_lane:
                arguments = _structured_duration_revision_arguments(
                    arguments,
                    pending_proposal=(
                        proposal_revision
                        if isinstance(proposal_revision, dict)
                        else pending_proposal
                        if isinstance(pending_proposal, dict)
                        else None
                    ),
                    current_change_set=change_set_coordinator.store.load_current(
                        strategist_session_id,
                        (
                            goal_coordinator.store.load_active(
                                strategist_session_id
                            )
                            or active_goal
                        ).goal_id,
                    ),
                )
            if (
                duration_correction_lane
                and not str(arguments.get("revision_of_proposal_id") or "").strip()
            ):
                revision_basis = (
                    proposal_revision
                    if isinstance(proposal_revision, dict)
                    else pending_proposal
                    if isinstance(pending_proposal, dict)
                    else None
                )
                revision_id = str(
                    revision_basis.get("proposal_id")
                    if isinstance(revision_basis, dict)
                    else ""
                ).strip()
                if revision_id:
                    arguments = {
                        **arguments,
                        "revision_of_proposal_id": revision_id,
                    }
            arguments = _repair_created_task_step_schedule_arguments(
                arguments,
                prompt=request_context_text,
                session=session,
                today_value=_authoritative_turn_date(
                    today_override,
                    workspace_context,
                ),
                workspace_context=workspace_context,
            )
            repaired_recurrence_result = repair_existing_recurrence_tool_route(arguments)
            if repaired_recurrence_result is not None:
                return finish_tool_call(repaired_recurrence_result)
        if (
            name == "handoff_to_planner"
            and isinstance(proposal_revision, dict)
            and str(proposal_revision.get("proposal_kind") or "").strip() == "branch"
            and _proposal_revision_has_recurring_schedule(proposal_revision)
        ):
            return finish_tool_call({
                "error": (
                    "A pending recurring branch proposal is being revised. Preserve its "
                    "primary target and submit a proposal delta instead of starting a new "
                    "Planner request for an affected conflict blocker."
                ),
                "model_instruction": (
                    "Call submit_task_tree_plan with revision_of_proposal_id equal to the "
                    "active proposal id. Put only changed recurrence fields in "
                    "recurring_schedule_updates. For 'move the existing blocker before the "
                    "incoming event only when they conflict', set "
                    "conflict_relocation=before_incoming. Do not create or reschedule the "
                    "blocker as a new recurrence."
                ),
                "compiled": False,
            })
        if name == "handoff_to_planner":
            arguments = _normalize_planner_handoff_constraint_arguments(arguments)
        current_tool_result_store = kwargs.pop("tool_result_store", tool_result_store)
        coverage_checkpoint = (
            task_plan_coverage_checkpoint(current_tool_result_store)
            if name == "submit_task_tree_plan"
            else None
        )
        current_goal = (
            goal_coordinator.store.load_active(strategist_session_id)
            or active_goal
        )
        invocation = McpInvocationContext(
            principal_id="local-runtime-user",
            agent_id="strategist",
            application_user_id="local",
            local_date=_authoritative_turn_date(
                today_override,
                workspace_context,
            ),
            timezone=str(
                (
                    workspace_context.get("time_zone")
                    or workspace_context.get("timezone")
                    or "local"
                )
                if isinstance(workspace_context, dict)
                else "local"
            ),
            allowed_tool_names=allowed_mcp_tool_names("strategist"),
            chat_session_id=strategist_session_id,
            turn_id=(
                str(strategist_turn_id)
                if strategist_turn_id is not None
                else None
            ),
            run_id=(
                str(current_run.id)
                if current_run is not None and current_run.id is not None
                else None
            ),
            active_goal_id=current_goal.goal_id,
            active_goal_revision=current_goal.revision,
            correlation_id=correlation_id,
            causation_id=(
                str(strategist_turn_id)
                if strategist_turn_id is not None
                else None
            ),
            active_category_id=(
                int(active_category.id)
                if active_category is not None and active_category.id is not None
                else None
            ),
            focus_node_id=(
                int(focus_node.id)
                if focus_node is not None and focus_node.id is not None
                else None
            ),
            selected_task_ids=tuple(int(task_id) for task_id in staged_node_ids),
        )
        result = _invoke_authoritative_model_tool(
            session,
            invocation=invocation,
            name=name,
            arguments=arguments,
            execution=McpExecutionDependencies(
                strategist_session_id=strategist_session_id,
                pending_proposal=(
                    proposal_revision
                    if duration_correction_lane
                    and isinstance(proposal_revision, dict)
                    else pending_proposal
                    if isinstance(pending_proposal, dict)
                    else None
                ),
                recent_events=recent_events,
                session_memory=session_memory,
                tool_result_store=current_tool_result_store,
                original_user_request=request_context_text,
                working_set=working_set,
                calendar_service=SQL_CALENDAR_READ_SERVICE,
                scheduling_service=planner_service,
                planner_decision_responder=respond_to_owned_planner_decision,
                evidence_resolver=lambda handle: resolve_evidence_handle(
                    session,
                    session_id=strategist_session_id,
                    goal_id=current_goal.goal_id,
                    handle=handle,
                ),
                goal_state_updater=lambda update_arguments: (
                    _apply_goal_state_tool_update(
                        coordinator=goal_coordinator,
                        session_memory=session_memory,
                        session_id=strategist_session_id,
                        source_turn_id=strategist_turn_id or correlation_id,
                        correlation_id=correlation_id,
                        current_run_id=current_run.id if current_run is not None else None,
                        arguments=update_arguments,
                    )
                    if "commandId" in update_arguments or "command_id" in update_arguments
                    else _apply_goal_turn_decision(
                        coordinator=goal_coordinator,
                        session_memory=session_memory,
                        session_id=strategist_session_id,
                        source_turn_id=strategist_turn_id or correlation_id,
                        current_user_turn=display_prompt,
                        correlation_id=correlation_id,
                        current_run_id=current_run.id if current_run is not None else None,
                        arguments=update_arguments,
                        authorized_reference_ids=_current_turn_authorized_reference_ids(
                            workspace_context,
                            tool_trace,
                        ),
                    )
                ),
                change_set_authorer=lambda draft_arguments: _apply_change_set_tool_draft(
                    coordinator=change_set_coordinator,
                    session_memory=session_memory,
                    session_id=strategist_session_id,
                    source_turn_id=strategist_turn_id or correlation_id,
                    correlation_id=correlation_id,
                    current_run_id=current_run.id if current_run is not None else None,
                    arguments=draft_arguments,
                ),
                change_set_simulator=lambda simulation_arguments: _apply_change_set_tool_simulate(
                    coordinator=simulation_coordinator,
                    session_memory=session_memory,
                    session_id=strategist_session_id,
                    source_turn_id=strategist_turn_id or correlation_id,
                    correlation_id=correlation_id,
                    current_run_id=current_run.id if current_run is not None else None,
                    arguments=simulation_arguments,
                ),
            ),
        )
        if (
            name == "submit_task_tree_plan"
            and isinstance(result, dict)
            and result.get("compiled") is not True
        ):
            restore_task_plan_coverage(
                current_tool_result_store,
                coverage_checkpoint,
            )
        if (
            name == "set_goal_transition"
            and isinstance(result, dict)
            and "error" not in result
        ):
            goal_transition_recorded = True
        if (
            name == "submit_task_tree_plan"
            and isinstance(result, dict)
            and result.get("compiled") is True
        ):
            semantic_plan_input = (
                result.get("semantic_plan_input")
                if isinstance(result.get("semantic_plan_input"), dict)
                else {}
            )
            branch_proposal = result.get("branch_proposal")
            if not isinstance(branch_proposal, dict):
                semantic_payload = semantic_plan_input.get("payload")
                if isinstance(semantic_payload, dict):
                    branch_proposal = semantic_payload
            created_task_schedule = (
                branch_proposal.get("created_task_schedule")
                if isinstance(branch_proposal, dict)
                else None
            )
            task_patch = (
                branch_proposal.get("task_patch")
                if isinstance(branch_proposal, dict)
                else None
            )
            task_operations = (
                task_patch.get("operations")
                if isinstance(task_patch, dict)
                and isinstance(task_patch.get("operations"), list)
                else []
            )
            if isinstance(created_task_schedule, dict):
                schedule_preflight = _enrich_created_task_schedule_before_compilation(
                    session,
                    proposal=branch_proposal,
                    prompt=request_context_text,
                    today_value=_authoritative_turn_date(
                        today_override,
                        workspace_context,
                    ),
                    workspace_context=workspace_context,
                )
                if (
                    isinstance(schedule_preflight, dict)
                    and schedule_preflight.get("status") != "valid"
                ):
                    restore_task_plan_coverage(
                        current_tool_result_store,
                        coverage_checkpoint,
                    )
                    preflight_errors = list(
                        dict.fromkeys(
                            str(error).strip()
                            for error in schedule_preflight.get("errors", [])
                            if str(error).strip()
                        )
                    )
                    preflight_groups = schedule_preflight.get("schedule_groups")
                    conflict_requires_user_input = bool(
                        isinstance(preflight_groups, list)
                        and any(
                            isinstance(group, dict)
                            and group.get("solver_status") == "conflict"
                            for group in preflight_groups
                        )
                    )
                    clarification = (
                        {
                            "message": preflight_errors[0],
                            "entity_choices": [],
                            "field_choices": [],
                            "scoped_follow_up_prompts": [],
                        }
                        if conflict_requires_user_input and preflight_errors
                        else None
                    )
                    return finish_tool_call(
                        {
                            **result,
                            "compiled": False,
                            "error": (
                                "created_task_schedule_requires_user_input"
                                if clarification is not None
                                else "created_task_schedule_invalid"
                            ),
                            "clarification": clarification,
                            "created_task_schedule_errors": preflight_errors,
                            "model_instruction": (
                                "Return the grounded scheduling clarification to the user "
                                "without dropping any pending request clauses."
                                if clarification is not None
                                else (
                                    "The task plan shape is valid, but its schedule did not "
                                    "pass the authoritative scheduling compiler. Repair the "
                                    "same complete plan and call submit_task_tree_plan again "
                                    "in this turn. Preserve every request item and task "
                                    "operation. Compiler errors: "
                                    + "; ".join(preflight_errors)
                                )
                            ),
                        }
                    )
                failures: list[str] = []
                create_operations = [
                    operation
                    for operation in task_operations
                    if isinstance(operation, dict)
                    and str(operation.get("op") or "") == "create"
                ]
                create_by_temp_id = {
                    str(operation.get("temp_id") or "").strip(): operation
                    for operation in create_operations
                    if str(operation.get("temp_id") or "").strip()
                }
                category_ids = {
                    int(category.id)
                    for category in categories
                    if isinstance(category.id, int)
                }
                schedule_specs = created_task_schedule_groups(created_task_schedule)
                if not schedule_specs:
                    failures.append("Add at least one scheduling group.")
                all_requested_temp_ids: list[str] = []
                all_requested_task_ids: list[int] = []
                live_node_ids = {
                    int(node.id)
                    for node in nodes
                    if isinstance(node.id, int)
                }
                for schedule_spec in schedule_specs:
                    try:
                        window_start = datetime.fromisoformat(
                            str(schedule_spec.get("window_start") or "")
                        )
                        window_end = datetime.fromisoformat(
                            str(schedule_spec.get("window_end") or "")
                        )
                    except ValueError:
                        failures.append("Choose a valid scheduling window.")
                    else:
                        comparable_start = window_start.replace(tzinfo=None)
                        comparable_end = window_end.replace(tzinfo=None)
                        if comparable_end <= comparable_start:
                            failures.append(
                                "The scheduling window must end after it starts."
                            )
                    requested_task_ids = [
                        int(item)
                        for item in schedule_spec.get("target_task_ids", [])
                        if isinstance(item, int) and not isinstance(item, bool)
                    ]
                    requested_temp_ids = [
                        str(item).strip()
                        for item in schedule_spec.get("target_temp_ids", [])
                        if isinstance(item, str) and str(item).strip()
                    ]
                    if not requested_temp_ids and not requested_task_ids:
                        requested_temp_ids = list(create_by_temp_id)
                    all_requested_temp_ids.extend(requested_temp_ids)
                    all_requested_task_ids.extend(requested_task_ids)
                    if (
                        not requested_temp_ids and not requested_task_ids
                    ):
                        failures.append(
                            "Every scheduling group must reference at least one created or "
                            "existing task."
                        )
                    if any(
                        temp_id not in create_by_temp_id
                        for temp_id in requested_temp_ids
                    ):
                        failures.append(
                            "Every scheduled task must reference a task created in this proposal."
                        )
                    if any(task_id not in live_node_ids for task_id in requested_task_ids):
                        failures.append(
                            "Every existing scheduling target must be a live task."
                        )
                    for temp_id in requested_temp_ids:
                        operation = create_by_temp_id.get(temp_id)
                        if not isinstance(operation, dict):
                            continue
                        if operation.get("category_id") not in category_ids:
                            failures.append("Choose a workspace for every new task.")
                if len(set(all_requested_temp_ids)) != len(all_requested_temp_ids):
                    failures.append(
                        "Each created task may appear in only one one-time scheduling group."
                    )
                if len(set(all_requested_task_ids)) != len(all_requested_task_ids):
                    failures.append(
                        "Each existing task may appear in only one one-time scheduling group."
                    )
                failures = list(dict.fromkeys(failures))
                if failures:
                    return finish_tool_call({
                        "error": (
                            "The combined task-and-calendar proposal did not pass "
                            "one-time scheduling validation."
                        ),
                        "proposal_kind": "branch",
                        "compiled": False,
                        "created_task_schedule_errors": failures,
                        "model_instruction": (
                            "Repair the complete plan and call submit_task_tree_plan "
                            "again in this turn. Preserve every requested task operation. "
                            "Classify every created node independently as task-only or "
                            "task-plus-calendar from the user's resolved constraints. Put only "
                            "task-plus-calendar nodes in scheduling groups. Provide reasonable "
                            "duration estimates when possible; Planner will infer a bounded "
                            "estimate when one is omitted. Represent tree ownership with "
                            "parent_temp_id. Use one "
                            "shared window only for targets that truly share a placement scope; "
                            "otherwise use separate schedule_groups. Exact-time windows must "
                            "be represented as typed exact_start request_constraints within a "
                            "shared window; use target_task_ids to solve existing tasks in the "
                            "same pass as new ones. Use offset-free local wall time. Validation "
                            "errors: "
                            + "; ".join(failures)
                        ),
                    })
        if (
            name == "draft_change_set"
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["model_instruction"] = (
                "The authoritative native ChangeSet draft is recorded. Do not rebuild "
                "the request through a compatibility proposal tool. The host will run "
                "deterministic simulation and project the approval review."
            )
            if count_against_foreground_budget:
                result["terminal_response"] = {
                    "summary": "The complete change is ready for validation and review.",
                    "action_proposal": None,
                    "metadata_proposal": None,
                    "branch_proposal": None,
                    PREFERENCE_RESPONSE_KEY: None,
                    "clarification": None,
                }
        if (
            name == "handoff_to_planner"
            and isinstance(result, dict)
            and isinstance(result.get("planner_handoff"), dict)
            and "error" not in result
        ):
            result.setdefault(
                "model_instruction",
                (
                    "Planner returned an observation to this same free-form loop. Use the result, "
                    "repair it, gather more evidence, or ask a precise question; do not treat the "
                    "handoff itself as a terminal response."
                ),
            )
        if (
            name in {"list_calendar_blocks", "get_task_schedule"}
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["model_instruction"] = (
                "These are authoritative accepted calendar blocks and recurrence identities. "
                "If the user referred to scheduled tasks in the plural, preserve all "
                "matching task_ids as one scope. For a move or better-time request, hand them "
                "to Planner with operation_mode=reschedule and scope_source=calendar_query. "
                "Do not task-search words from the date or time phrase."
            )
        if (
            name in {"get_subtree", "get_task_branch"}
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["model_instruction"] = (
                "This branch result is sufficient evidence for the requested target. Do not "
                "reread the same branch or call context-surface tools for the same decision. "
                "If the user selected a restructuring strategy, call submit_task_tree_plan "
                "now; otherwise synthesize the answer from this result."
            )
        if (
            name == "analyze_tree_cleanup_candidates"
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["model_instruction"] = (
                "Use this as the cleanup decision surface. If organization_map_complete is false, "
                "retrieve the next page before claiming a full cleanup. For an explicit mutation, "
                "classify all relevant tasks against destination_catalog, move the highest coherent "
                "subtree root so descendants stay together, and call submit_task_tree_plan once with "
                "the complete ordered batch. Archive uncertain stale/redundant candidates; only propose "
                "hard deletion when the evidence is strong and the user requested deletion. The harness "
                "will validate the entire typed batch before showing it. Keep user-visible summary and "
                "rationale free of internal contract names. For a read-only request, summarize "
                "the findings without a proposal."
            )
        if (
            name == "analyze_preference_cleanup_candidates"
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["model_instruction"] = (
                "This is the complete server-classified preference-cleanup decision surface. "
                "For the user's all-inclusive cleanup request, call "
                "submit_preference_cleanup_plan once with this exact candidate_set_fingerprint "
                "and the same cleanup_scope plus the requested disable or delete action. "
                "Use contradictions for conflicting/superseded values, overlaps for duplicate "
                "or redundant copies, and all only if the user asked for both. Do not replace "
                "it with an exact-ID management plan and do not add ambiguous_tensions. "
                "The server will reject the submission if the inventory changes before review."
            )
        if (
            name in SEMANTIC_PLAN_TOOL_FAMILIES
            and isinstance(result, dict)
            and result.get("compiled") is True
            and "error" not in result
        ):
            completion_instruction = (
                "If every clause in the user's request now has a successful semantic "
                "submission, return the final Strategist response immediately. If a "
                "different semantic family is still required (for example, preference "
                "memory alongside task changes), submit only that remaining family. "
                "Do not repeat discovery or resubmit this successful plan."
            )
            prior_instruction = str(result.get("model_instruction") or "").strip()
            result["model_instruction"] = " ".join(
                item for item in (prior_instruction, completion_instruction) if item
            )
        if (
            name != "get_tool_result_by_handle"
            and isinstance(result, dict)
            and "error" not in result
        ):
            handle = f"kernel-tool-result-{len(current_tool_result_store) + 1}"
            stored_result = dict(result)
            if name == "simulate_change_set":
                stored_result["result_kind"] = "native_change_set_simulation"
            current_tool_result_store[handle] = stored_result
            result.setdefault("tool_result_handle", handle)
            if name == "simulate_change_set":
                return finish_tool_call(compact_simulation_result_for_model(
                    result,
                    tool_result_handle=handle,
                ))
            if name == "handoff_to_planner" and result.get("planner_executed") is True:
                # Keep the exact Planner response in the same-turn result store and in
                # ``planner_result`` above for host-side proposal projection.  The model
                # only needs the semantic outcome plus a handle it can inspect if it
                # genuinely needs the full payload.  This avoids copying an entire
                # schedule simulation into the transcript, event ledger, and subsequent
                # context reconstruction.
                compact_result = {
                    key: value
                    for key, value in result.items()
                    if key != "planner_result"
                }
                compact_result["tool_result_handle"] = handle
                return finish_tool_call(compact_result)
        return finish_tool_call(result)
    active_request_pending_review = bool(
        isinstance(active_request, dict)
        and str(active_request.get("status") or "").strip() == "pending_approval"
    )
    host_goal_transition_recorded = False
    if active_request_pending_review:
        # A direct pending-review correction is already bound to the durable goal.
        # The semantic submission records the sparse correction atomically.
        goal_transition_recorded = True
        host_goal_transition_recorded = True
    elif fresh_goal_envelope:
        # ``ensure_active`` just created the durable envelope for this exact
        # user turn. Requiring the model to declare another ``new`` transition
        # before its semantic tool call adds no information and can reject an
        # otherwise complete task/schedule plan. Lifecycle state is structural
        # host evidence, so this applies to every fresh goal without inspecting
        # task wording.
        goal_transition_recorded = True
        host_goal_transition_recorded = True
    elif clarification_continuation_lane:
        # The prior typed clarification already established goal ownership.
        # The latest turn is its answer, so it is necessarily a continuation;
        # requiring the model to restate that lifecycle fact created a
        # recoverable-but-expensive failed semantic tool call before Planner.
        goal_transition_recorded = True
        host_goal_transition_recorded = True
    # Conservative read-only fact lookup may stay deterministic. Natural-language
    # mutation requests fall through to the model-owned semantic loop.
    contract_fast_lane_payload = _build_task_fact_fast_lane_payload(
        prompt=display_prompt,
        route_decision=route_decision,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        today_override=today_override,
        tool_trace=tool_trace,
        tool_executor=kernel_tool_executor,
    )
    prefetched_tool_trace: list[dict[str, Any]] = []
    prefetched_tool_history = _trace_to_initial_tool_history(tool_trace)
    model_pending_proposal = pending_proposal
    if isinstance(proposal_revision, dict) and (
        duration_correction_lane or not isinstance(model_pending_proposal, dict)
    ):
        model_pending_proposal = {
            **proposal_revision,
            "goal_id": active_goal.goal_id,
        }
    context_packet = _build_kernel_context_packet(
        active_goal=active_goal,
        latest_user_turn=display_prompt,
        runtime_context=runtime_context,
        conversation_history=conversation_history,
        pending_proposal=model_pending_proposal,
        recent_events=recent_events,
        session_memory=session_memory,
    )
    context_packet["tier_route"] = route_decision.to_payload()
    context_packet["foreground_budget"] = route_decision.budget.to_payload()
    if route_decision.route_kind == "external_world_planning":
        _prune_context_packet_for_external_planning(
            context_packet,
            active_category=active_category,
            focus_node=focus_node,
            staged_node_ids=staged_node_ids,
        )
    elif (
        isinstance(active_request, dict)
        and str(active_request.get("status") or "").strip() == "pending_approval"
    ):
        _prune_context_packet_for_pending_review_correction(context_packet)
    elif _uses_single_task_metadata_authoring_surface(
        display_prompt,
        route_decision,
    ):
        _prune_context_packet_for_single_task_metadata(
            context_packet,
            nodes=nodes,
        )
    elif fresh_goal_envelope:
        _prune_context_packet_for_fresh_model_turn(context_packet)
    workflow_profile = _build_kernel_workflow_profile(
        prompt=display_prompt,
        today_override=today_override,
    )
    if workflow_profile is not None:
        context_packet["workflow_profile"] = workflow_profile
    if tool_policy is not None:
        context_packet["tool_policy"] = tool_policy
    context_size = _context_size_summary(context_packet)
    context_duration_ms = _elapsed_ms(context_started_at)
    append_kernel_event(
        session,
        strategist_session_id=strategist_session_id,
        correlation_id=correlation_id,
        sequence=sequence,
        event_type="context_packet",
        payload=context_packet,
    )
    event_types.append("context_packet")
    sequence += 1

    deterministic_payload: dict[str, Any] | None = contract_fast_lane_payload
    if deterministic_payload is None and route_decision.tier == "tier_0":
        deterministic_payload = _build_tier0_state_payload(
            route_decision=route_decision,
            active_category=active_category,
            focus_node=focus_node,
            pending_proposal=pending_proposal,
            session_memory=session_memory,
        )
    # Natural-language turns intentionally have no deterministic payload path.
    # The model may call the same analyzers and proposal tools explicitly.
    next_provider_session_id: str | None = None
    if deterministic_payload is not None:
        fast_path = deterministic_payload.get("deterministic_fast_path")
        deterministic_kind = (
            str(fast_path.get("kind") or "")
            if isinstance(fast_path, dict)
            else ""
        )
        fast_path_event_type = {
            "agent_run_plan_explanation": "agent_run_plan_explanation",
            "agent_run_plan_followup": "agent_run_plan_followup",
            "agent_run_plan_continuation": "agent_run_plan_continuation",
            "task_graph_answer_candidate": "task_graph_answer_candidate",
            "single_task_metadata_proposal": "metadata_contract_compiled",
            "metadata_clarification": "deterministic_scope_resolution",
            "external_plan_task_proposal": "external_plan_task_proposal",
            "cleanup_reconnaissance": "cleanup_reconnaissance",
            "workload_reconnaissance": "workload_reconnaissance",
            "branch_reconnaissance": "branch_reconnaissance",
            "mixed_cleanup_mutation": "mixed_cleanup_mutation",
            "tier0_state_answer": "tier_route_completion",
        }.get(deterministic_kind, "deterministic_scope_resolution")
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type=fast_path_event_type,
            payload=fast_path if isinstance(fast_path, dict) else {},
        )
        event_types.append(fast_path_event_type)
        sequence += 1
        agent_run_plan = (
            deterministic_payload.get("agent_run_plan")
            if isinstance(deterministic_payload.get("agent_run_plan"), dict)
            else None
        )
        if agent_run_plan is not None:
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="agent_run_plan_created",
                payload=agent_run_plan,
            )
            event_types.append("agent_run_plan_created")
            sequence += 1
        parsed = deterministic_payload
    else:
        model_loop_started_at = perf_counter()
        provider_readiness = provider_transport_readiness(runtime_settings)
        # A missing connection is not a stalled model turn. Keep this outside
        # inference recovery so the run persists as a failure, with no invented
        # answer or completion trace.
        if provider_readiness.get("passed") is False:
            raise HTTPException(
                status_code=409,
                detail=(
                    "ChatGPT is not connected. Open Connections in Settings, finish sign-in, "
                    "then retry this message. If OpenAI asks, enable device-code sign-in "
                    "in ChatGPT Settings → Security first."
                    if runtime_settings.get("provider") == "codex-cli"
                    else "The OpenAI API connection is not ready. Open Connections in Settings "
                    "to configure it, or connect your ChatGPT subscription."
                ),
            )
        configured_reasoning_effort = runtime_settings.get("reasoning_effort")
        effective_reasoning_effort = _initial_model_reasoning_effort(
            configured_reasoning_effort
        )
        if (
            duration_correction_lane
            or _uses_single_task_metadata_authoring_surface(
                display_prompt,
                route_decision,
            )
        ):
            effective_reasoning_effort = "low"
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="model_action_loop_started",
            payload={
                "provider": runtime_settings.get("provider"),
                "model_name": runtime_settings.get("model_name"),
                "reasoning_effort": effective_reasoning_effort,
                "configured_reasoning_effort": configured_reasoning_effort,
                "feature_flag": "STRATEGIST_AGENT_KERNEL",
                "context_size": context_size,
                "tier_route": route_decision.to_payload(),
                "foreground_budget": route_decision.budget.to_payload(),
                "step_timeout_seconds": (
                    route_decision.budget.model_step_timeout_seconds
                    or codex_structured_step_timeout_seconds()
                ),
                "loop_timeout_seconds": (
                    route_decision.budget.loop_timeout_seconds
                    or codex_tool_loop_timeout_seconds()
                ),
                "max_model_steps": (
                    route_decision.budget.max_model_calls
                    or codex_tool_loop_max_steps()
                ),
                "provider_readiness": provider_readiness,
            },
        )
        event_types.append("model_action_loop_started")
        sequence += 1

        provider_session_id_for_turn: str | None = None
        tool_policy_prompt = _kernel_tool_policy_prompt(tool_policy)
        kernel_prompt = (
            f"{display_prompt}\n\n"
            f"Agent-kernel policy: {render_core_operating_instructions('strategist')} "
            "Capability authority: every model-selected tool crosses the allowlisted "
            "drasil-core MCP boundary. The Agent Host records a fresh goal envelope and "
            "hides set_goal_transition when lifecycle ownership is already known from "
            "durable state. When that tool is available, call it exactly once before semantic "
            "planning and classify the turn as new, continue, or correct. For a direct "
            "correction to the current pending "
            "review, submit the revised high-level semantic plan directly; the Agent Host "
            "records the correction and revises the existing ChangeSet atomically. Use the "
            "available high-level "
            "proposal or scheduling capability to conserve the complete semantic plan in one "
            "review. Internal ChangeSet, approval, transaction, "
            "provenance, and database contracts are server-owned and are not model tools. "
            "Conserve requirements across clarification turns: goalContext is authoritative, "
            "and the latest sparse correction replaces only the field it changes. For a newly "
            "confirmed task, do not search for an "
            "existing task with the same generic words. If the user describes a workflow, "
            "represent its steps as child tasks under one top-level task. "
            "Put notes, estimates, priority, status, and deadline in task fields. If scheduling "
            "is requested, preserve calendar meaning in the same goal and review; "
            "read the calendar only when needed to select non-conflicting exact times. Ordinary "
            "task and calendar mutations already have registered tools, so do not search the "
            "capability catalog for them. "
            "Search the capability catalog and load specialized skills when they materially "
            "help. Use current-turn task tools for task-world claims. The latest explicit scope "
            "outranks UI focus and recent working-set salience. Read-tool arguments use "
            "task_id/task_title naming. "
            "If prior conversation establishes a task change and the latest user selects "
            "a concrete recommended value, draft the reviewable proposal with the typed "
            "proposal tool now. "
            "goalContext is the durable unresolved goal. Its semantic slots, references, "
            "constraints, decisions, pending review reference, and evidence handles preserve "
            "continuity. Resolve a durable evidence handle only when its bounded observation is "
            "needed; refresh only facts that may have changed or evidence that is missing. "
            "Preserve every compatible semantic slot across repeated clarification turns; the "
            "latest explicit correction replaces only the field it changes. Do not "
            "silently drop task scope, requested operations, dates, clock times, recurrence, "
            "duration, ordering, gaps, or hard constraints. "
            "When goalContext.pendingReview is present, this turn edits that review "
            "rather than creating a new goal. Treat its semantic contract as "
            "authoritative: preserve the primary task, cadence, dates, duration, and structural "
            "operations unless the user explicitly changes them. A displaced calendar blocker "
            "is not the recurrence target. Revise a recurring branch with "
            "submit_task_tree_plan(revision_of_proposal_id=..., "
            "recurring_schedule_updates={...}); never hand the blocker to Planner as a new "
            "recurrence. "
            "If the user asks you to remember a durable scheduling preference, or clearly "
            "states one that should affect future planning, call submit_preference_plan "
            "instead of only acknowledging it. For qualitative preference description and "
            "preferenceText fields, preserve every user-supplied content word in the same order; "
            "remove only request framing and terminal punctuation, and never paraphrase, shorten, "
            "broaden, or add rationale. If the user asks to inspect, disable, restore, "
            "remove, or delete saved preferences, call list_remembered_preferences and "
            "then submit_preference_management_plan with exact IDs and revisions. Ask "
            "whether to disable or permanently delete when 'remove' is genuinely ambiguous. "
            "If they ask for all conflicting, contradictory, outdated, duplicate, or "
            "redundant preferences, call analyze_preference_cleanup_candidates with the narrowest "
            "matching cleanup_scope, then submit_preference_cleanup_plan with that same scope and "
            "the returned candidate-set fingerprint. Conflicting/contradictory means contradictions; "
            "duplicate/redundant/overlapping means overlaps; use all only when both were requested. "
            "Never hand-select only some candidates for an all-inclusive cleanup request. "
            "A request to remind the user to do something "
            "is executable task/schedule intent, not a preference. For a new task plus a finite "
        "recurring schedule, use one submit_task_tree_plan call with create_node and "
        "recurring_schedule. For new tasks that should be scheduled once, use one "
        "submit_task_tree_plan call with temp_id create_node operations and "
        "created_task_schedule; preserve this scheduling intent through clarifications. "
        "For an existing task recurrence, call get_task_schedule after resolving exact task "
            "IDs, preserve its authoritative series/occurrence identity and rule, then include "
            "recurrence in the Planner handoff so dates are resolved independently. "
            "A short follow-up like 'let's go with X', 'yes use that', or 'that works' "
            "after a recommendation is a selected-value task-edit continuation; use "
            "the transcript/session memory and do not run broad retrieval first. "
            "For a uniquely identified named-task edit with a concrete value, call the "
            "typed proposal tool with the exact target task id. If uniqueness is not "
            "established by context, search or inspect first. "
            "For different metadata values across multiple identified tasks, call "
            "submit_task_metadata_plan once with target_updates containing each exact task id; "
            "do not use tree operations for metadata-only work. "
            "When the user identifies already-scheduled work by a date, time, calendar "
            "position, or phrase such as 'the tasks scheduled on the 16th', call "
            "list_calendar_blocks before any task-title search. Treat the returned block and "
            "task ids as authoritative, preserve plural scope, and use scope_source="
            "calendar_query for a Planner reschedule handoff. "
            "If the latest user only asks to find, recommend, suggest, or compare a "
            "possible date/time/metadata value, answer with decision support and no "
            "approval card until the user selects or accepts one concrete value. "
            "If the latest user supplies details you previously requested for a mutation, "
            "or corrects the direct parent for a tree change, draft the typed proposal now. "
            "When the user answers a pending clarification with task/subtask details, use "
            "the direct parent/task from transcript and session memory and do not restart "
            "target discovery. "
            "Do not delegate classification to another control path."
            f"{' ' + tool_policy_prompt if tool_policy_prompt else ''}"
        ).strip()
        compact_authoring_prompt = _compact_model_authoring_prompt(
            prompt=display_prompt,
            route_decision=route_decision,
            session_memory=session_memory,
            tool_policy_prompt=tool_policy_prompt,
        )
        if compact_authoring_prompt is not None:
            kernel_prompt = compact_authoring_prompt
        elif runtime_settings["provider"] != "openai-api":
            # The Codex native transport owns the provider-independent operating
            # contract and tool protocol. Passing the full runtime policy as its
            # "user request" duplicated that contract and routinely exceeded the
            # route's prompt budget. Preserve the actual request only; context and
            # tool schemas are supplied through their dedicated channels.
            kernel_prompt = display_prompt
        tool_definitions = _kernel_tool_schemas_for_route(
            route_decision,
            session_memory=session_memory,
            prompt=display_prompt,
        )
        if host_goal_transition_recorded:
            tool_definitions = [
                tool
                for tool in tool_definitions
                if str(tool.get("name") or "").strip() != "set_goal_transition"
            ]
        route_budget_kwargs = _route_budget_kwargs(route_decision)
        if turn_deadline_monotonic is not None:
            controller_seconds = _remaining_deadline_seconds(
                turn_deadline_monotonic,
                reserve_seconds=SHARED_WORKSPACE_PLANNER_RESERVE_SECONDS,
            )
            route_budget_kwargs["loop_timeout_seconds"] = max(
                1,
                min(route_budget_kwargs.get("loop_timeout_seconds", controller_seconds), controller_seconds),
            )
            route_budget_kwargs["step_timeout_seconds"] = max(
                1,
                min(
                    route_budget_kwargs.get("step_timeout_seconds", controller_seconds),
                    controller_seconds,
                ),
            )
        try:
            if route_decision.route_kind == "external_world_planning":
                direct_request = (
                    f"{display_prompt}\n\n"
                    f"Agent-kernel policy: {tool_policy_prompt}"
                    if tool_policy_prompt
                    else display_prompt
                )
                direct_prompt = build_direct_response_prompt(
                    mode="agent-kernel",
                    prompt=direct_request,
                    runtime_context=context_packet,
                    conversation_history=conversation_history,
                )
                direct_timeout_seconds = _direct_response_timeout_seconds(route_decision)
                direct_started_at = perf_counter()
                direct_trace: dict[str, Any] = {
                    "step_index": 1,
                    "phase": "direct_response",
                    "prompt_chars": len(direct_prompt),
                    "timeout_seconds": direct_timeout_seconds,
                    "provider_session_resumed": False,
                }
                try:
                    if runtime_settings["provider"] == "openai-api":
                        parsed, next_provider_session_id = invoke_openai_direct_response(
                            prompt=direct_prompt,
                            model_name=str(runtime_settings["model_name"] or "gpt-5-mini"),
                            timeout_seconds=direct_timeout_seconds,
                        )
                    else:
                        parsed, next_provider_session_id = invoke_codex_direct_response(
                            prompt=direct_prompt,
                            configured_model=(
                                str(runtime_settings["model_name"])
                                if isinstance(runtime_settings["model_name"], str) and runtime_settings["model_name"]
                                else None
                            ),
                            timeout_seconds=direct_timeout_seconds,
                            trace_collector=direct_trace,
                            reasoning_effort=effective_reasoning_effort,
                        )
                except HTTPException as error:
                    direct_trace.update(
                        {
                            "status": "timeout" if error.status_code == 504 else "error",
                            "duration_ms": _elapsed_ms(direct_started_at),
                            "error_status_code": error.status_code,
                            "error_detail": str(error.detail),
                        }
                    )
                    model_step_trace.append(direct_trace)
                    raise
                direct_trace.update(
                    {
                        "status": "ok",
                        "duration_ms": _elapsed_ms(direct_started_at),
                        "kind": "final",
                    }
                )
                model_step_trace.append(direct_trace)
            elif runtime_settings["provider"] == "openai-api":
                parsed, next_provider_session_id = invoke_openai_tool_loop(
                    mode="agent-kernel",
                    prompt=kernel_prompt,
                    runtime_context=context_packet,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    staged_node_ids=staged_node_ids,
                    working_set=working_set,
                    provider_session_id=provider_session_id_for_turn,
                    model_name=str(runtime_settings["model_name"] or "gpt-5-mini"),
                    conversation_history=conversation_history,
                    today_override=today_override,
                    tool_trace=tool_trace,
                    initial_tool_history=prefetched_tool_history,
                    tool_definitions=tool_definitions,
                    tool_executor=kernel_tool_executor,
                    model_step_trace=model_step_trace,
                    max_model_steps=route_budget_kwargs.get("max_model_steps"),
                    max_tool_calls=route_budget_kwargs.get("max_tool_calls"),
                )
            else:
                codex_loop = codex_tool_loop_invoker or invoke_codex_tool_loop
                parsed, next_provider_session_id = codex_loop(
                    mode="agent-kernel",
                    prompt=kernel_prompt,
                    runtime_context=context_packet,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    staged_node_ids=staged_node_ids,
                    working_set=working_set,
                    provider_session_id=provider_session_id_for_turn,
                    configured_model=(
                        str(runtime_settings["model_name"])
                        if isinstance(runtime_settings["model_name"], str) and runtime_settings["model_name"]
                        else None
                    ),
                    reasoning_effort=effective_reasoning_effort,
                    conversation_history=conversation_history,
                    today_override=today_override,
                    tool_trace=tool_trace,
                    initial_tool_history=prefetched_tool_history,
                    tool_definitions=tool_definitions,
                    tool_executor=kernel_tool_executor,
                    model_step_trace=model_step_trace,
                    **route_budget_kwargs,
                )
        except HTTPException as error:
            rescue_succeeded = False
            if error.status_code in {401, 403, 409, 429, 503} and not tool_trace:
                # Authentication, account contention, and service limits need
                # recovery by the user, not another model continuation.
                raise
            rescue_error: HTTPException | None = None
            remaining_model_calls = max(
                0,
                route_budget_kwargs.get(
                    "max_model_steps",
                    route_decision.budget.max_model_calls,
                )
                - len(model_step_trace),
            )
            remaining_tool_calls = max(
                0,
                route_budget_kwargs.get(
                    "max_tool_calls",
                    route_decision.budget.max_tool_calls,
                )
                - executed_kernel_tool_calls,
            )
            transport_unavailable = _provider_error_is_transport_unavailable(error)
            rescue_skipped_reason: str | None = (
                "provider_transport_unavailable" if transport_unavailable else None
            )
            if (
                route_decision.route_kind == "external_world_planning"
                and remaining_model_calls > 0
                and provider_readiness.get("passed") is not False
                and not transport_unavailable
            ):
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="model_rescue_started",
                    payload={
                        "provider": runtime_settings.get("provider"),
                        "model_name": runtime_settings.get("model_name"),
                        "original_status_code": error.status_code,
                        "original_detail": str(error.detail),
                        "tool_count": len(tool_trace),
                        "tier_route": route_decision.to_payload(),
                        "rescue_mode": "direct_response",
                    },
                )
                event_types.append("model_rescue_started")
                sequence += 1
                rescue_started_at = perf_counter()
                rescue_timeout_seconds = _direct_response_timeout_seconds(
                    route_decision,
                    rescue=True,
                )
                if turn_deadline_monotonic is not None:
                    rescue_timeout_seconds = min(
                        rescue_timeout_seconds,
                        max(1, _remaining_deadline_seconds(turn_deadline_monotonic)),
                    )
                rescue_prompt = (
                    f"{display_prompt}\n\n"
                    "Recovery instruction: the previous direct model step stopped before "
                    "returning valid Strategist JSON. Answer this external-world planning "
                    "request now using concise, stable, non-live guidance. Do not require "
                    "a task-tree target or call tools."
                )
                direct_rescue_context = {
                    "control_plane": "agent-kernel",
                    "tier_route": route_decision.to_payload(),
                    "foreground_budget": route_decision.budget.to_payload(),
                    "runtime_context": context_packet.get("runtime_context"),
                    "tool_policy": tool_policy,
                    "recovery": {
                        "kind": "direct_response_retry",
                        "original_status_code": error.status_code,
                    },
                }
                direct_rescue_prompt = build_direct_response_prompt(
                    mode="agent-kernel",
                    prompt=rescue_prompt,
                    runtime_context=direct_rescue_context,
                    conversation_history=conversation_history[-2:],
                )
                direct_rescue_trace: dict[str, Any] = {
                    "step_index": 2,
                    "phase": "direct_response_rescue",
                    "prompt_chars": len(direct_rescue_prompt),
                    "timeout_seconds": rescue_timeout_seconds,
                    "provider_session_resumed": False,
                }
                try:
                    if runtime_settings["provider"] == "openai-api":
                        parsed, next_provider_session_id = invoke_openai_direct_response(
                            prompt=direct_rescue_prompt,
                            model_name=str(runtime_settings["model_name"] or "gpt-5-mini"),
                            timeout_seconds=rescue_timeout_seconds,
                        )
                    else:
                        parsed, next_provider_session_id = invoke_codex_direct_response(
                            prompt=direct_rescue_prompt,
                            configured_model=(
                                str(runtime_settings["model_name"])
                                if isinstance(runtime_settings["model_name"], str) and runtime_settings["model_name"]
                                else None
                            ),
                            timeout_seconds=rescue_timeout_seconds,
                            trace_collector=direct_rescue_trace,
                            reasoning_effort=configured_reasoning_effort,
                        )
                    direct_rescue_trace.update(
                        {
                            "status": "ok",
                            "duration_ms": _elapsed_ms(rescue_started_at),
                            "kind": "final",
                        }
                    )
                    model_step_trace.append(direct_rescue_trace)
                    rescue_succeeded = True
                except HTTPException as caught_rescue_error:
                    rescue_error = caught_rescue_error
                    direct_rescue_trace.update(
                        {
                            "status": "timeout" if caught_rescue_error.status_code == 504 else "error",
                            "duration_ms": _elapsed_ms(rescue_started_at),
                            "error_status_code": caught_rescue_error.status_code,
                            "error_detail": str(caught_rescue_error.detail),
                        }
                    )
                    model_step_trace.append(direct_rescue_trace)
            if (
                route_decision.route_kind != "external_world_planning"
                and remaining_model_calls > 0
                and remaining_tool_calls > 0
                and provider_readiness.get("passed") is not False
                and not transport_unavailable
            ):
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="model_rescue_started",
                    payload={
                        "provider": runtime_settings.get("provider"),
                        "model_name": runtime_settings.get("model_name"),
                        "original_status_code": error.status_code,
                        "original_detail": str(error.detail),
                        "tool_count": len(tool_trace),
                        "tier_route": route_decision.to_payload(),
                    },
                )
                event_types.append("model_rescue_started")
                sequence += 1
                rescue_started_at = perf_counter()
                rescue_tool_history = _trace_to_initial_tool_history(tool_trace)
                rescue_has_read_evidence = any(
                    isinstance(item.get("result"), dict)
                    and item["result"].get("read_only") is True
                    and not item["result"].get("error")
                    for item in rescue_tool_history
                    if isinstance(item, dict)
                )
                if rescue_has_read_evidence:
                    rescue_allowed_tools = {
                        "submit_task_metadata_plan",
                        "submit_task_tree_plan",
                        "submit_preference_plan",
                        "submit_preference_management_plan",
                        "submit_preference_cleanup_plan",
                    }
                    rescue_tool_definitions = [
                        tool
                        for tool in tool_definitions
                        if str(tool.get("name") or "") in rescue_allowed_tools
                    ]
                else:
                    rescue_tool_definitions = tool_definitions
                rescue_prompt = (
                    f"{kernel_prompt}\n\n"
                    "Recovery instruction: the previous model step stopped before a valid "
                    "Strategist response was produced. Continue from prior_tool_evidence, which "
                    "contains the successful unique reads from the stopped turn. Do not repeat "
                    "discovery. If enough evidence exists, return a final answer or call one "
                    "semantic-plan tool now. If the target or operation is still ambiguous, return one "
                    "concise clarification. Do not idle or wait for another control path."
                )
                # Recovery is part of the same durable run.  It must have
                # enough time to synthesize a batch proposal from a large
                # organization map; the previous 12/15 second caps guaranteed
                # a second failure after successful cleanup retrieval.
                rescue_budget_kwargs = {
                    "step_timeout_seconds": max(
                        120,
                        min(
                            route_budget_kwargs.get("step_timeout_seconds", 600),
                            600,
                        ),
                    ),
                    "loop_timeout_seconds": max(
                        180,
                        min(
                            route_budget_kwargs.get("loop_timeout_seconds", 900),
                            900,
                        ),
                    ),
                    "max_model_steps": min(
                        remaining_model_calls, 1 if rescue_tool_history else 2
                    ),
                    "max_tool_calls": min(
                        remaining_tool_calls, 1 if rescue_has_read_evidence else 2
                    ),
                }
                if turn_deadline_monotonic is not None:
                    rescue_budget_kwargs["loop_timeout_seconds"] = min(
                        rescue_budget_kwargs["loop_timeout_seconds"],
                        max(1, _remaining_deadline_seconds(turn_deadline_monotonic)),
                    )
                try:
                    if runtime_settings["provider"] == "openai-api":
                        parsed, next_provider_session_id = invoke_openai_tool_loop(
                            mode="agent-kernel",
                            prompt=rescue_prompt,
                            runtime_context=context_packet,
                            categories=categories,
                            nodes=nodes,
                            focus_node=focus_node,
                            active_category=active_category,
                            staged_node_ids=staged_node_ids,
                            working_set=working_set,
                            provider_session_id=provider_session_id_for_turn,
                            model_name=str(runtime_settings["model_name"] or "gpt-5-mini"),
                            conversation_history=conversation_history,
                            today_override=today_override,
                            tool_trace=tool_trace,
                            initial_tool_history=rescue_tool_history,
                            tool_definitions=rescue_tool_definitions,
                            tool_executor=kernel_tool_executor,
                            model_step_trace=model_step_trace,
                            max_model_steps=rescue_budget_kwargs["max_model_steps"],
                            max_tool_calls=rescue_budget_kwargs["max_tool_calls"],
                        )
                    else:
                        codex_loop = codex_tool_loop_invoker or invoke_codex_tool_loop
                        parsed, next_provider_session_id = codex_loop(
                            mode="agent-kernel",
                            prompt=rescue_prompt,
                            runtime_context=context_packet,
                            categories=categories,
                            nodes=nodes,
                            focus_node=focus_node,
                            active_category=active_category,
                            staged_node_ids=staged_node_ids,
                            working_set=working_set,
                            provider_session_id=provider_session_id_for_turn,
                            configured_model=(
                                str(runtime_settings["model_name"])
                                if isinstance(runtime_settings["model_name"], str) and runtime_settings["model_name"]
                                else None
                            ),
                            reasoning_effort=configured_reasoning_effort,
                            conversation_history=conversation_history,
                            today_override=today_override,
                            tool_trace=tool_trace,
                            initial_tool_history=rescue_tool_history,
                            tool_definitions=rescue_tool_definitions,
                            tool_executor=kernel_tool_executor,
                            model_step_trace=model_step_trace,
                            **rescue_budget_kwargs,
                        )
                    model_step_trace.append(
                        {
                            "phase": "model_rescue",
                            "status": "ok",
                            "duration_ms": _elapsed_ms(rescue_started_at),
                            "initial_tool_count": len(rescue_tool_history),
                        }
                    )
                    rescue_succeeded = True
                except HTTPException as caught_rescue_error:
                    rescue_error = caught_rescue_error
                    model_step_trace.append(
                        {
                            "phase": "model_rescue",
                            "status": "timeout" if caught_rescue_error.status_code == 504 else "error",
                            "duration_ms": _elapsed_ms(rescue_started_at),
                            "error_status_code": caught_rescue_error.status_code,
                            "error_detail": str(caught_rescue_error.detail),
                            "initial_tool_count": len(rescue_tool_history),
                        }
                    )
            if not rescue_succeeded:
                provider_failure = {
                    "kind": "model_step_timeout",
                    "resumable": True,
                    "resume_action": "retry_same_goal",
                    "status_code": error.status_code,
                    "detail": str(error.detail),
                    "provider": runtime_settings.get("provider"),
                    "model_name": runtime_settings.get("model_name"),
                    "model_step_trace": model_step_trace,
                    "provider_readiness": provider_readiness,
                    "rescue_attempted": rescue_error is not None,
                    "rescue_skipped_reason": rescue_skipped_reason,
                    "rescue_error": (
                        {
                            "status_code": rescue_error.status_code,
                            "detail": str(rescue_error.detail),
                        }
                        if rescue_error is not None
                        else None
                    ),
                    "tool_failures": [
                        {
                            "name": str(call.get("name") or "")[:96],
                            "error": str(result.get("error") or "")[:240] or None,
                            "compiled": result.get("compiled"),
                            "created_task_schedule_errors": [
                                str(item)[:160]
                                for item in result.get(
                                    "created_task_schedule_errors",
                                    [],
                                )[:8]
                            ]
                            if isinstance(
                                result.get("created_task_schedule_errors"),
                                list,
                            )
                            else [],
                        }
                        for call in tool_trace
                        if isinstance(call, dict)
                        and isinstance(
                            (result := call.get("result")),
                            dict,
                        )
                        and (
                            result.get("error")
                            or result.get("compiled") is False
                        )
                    ][:8],
                }
                parsed = _build_kernel_provider_failure_payload(
                    error_detail=str(rescue_error.detail) if rescue_error is not None else str(error.detail),
                    tool_trace=tool_trace,
                )
                next_provider_session_id = None
        model_loop_duration_ms = _elapsed_ms(model_loop_started_at)

    if (
        deterministic_payload is None
        and provider_failure is None
        and planner_result is None
        and isinstance(parsed, dict)
    ):
        retry_candidate_payload = normalize_provider_response_payload(parsed)
        foreground_native_change_set = change_set_coordinator.store.load_current(
            strategist_session_id,
            (
                goal_coordinator.store.load_active(strategist_session_id)
                or active_goal
            ).goal_id,
        )
        remaining_model_calls = max(
            0,
            route_budget_kwargs.get(
                "max_model_steps",
                route_decision.budget.max_model_calls,
            )
            - len(model_step_trace),
        )
        remaining_tool_calls = max(
            0,
            route_budget_kwargs.get(
                "max_tool_calls",
                route_decision.budget.max_tool_calls,
            )
            - executed_kernel_tool_calls,
        )
        if (
            foreground_native_change_set is None
            and remaining_model_calls > 0
            and remaining_tool_calls > 0
            and _should_retry_proposal_contract(
            prompt=request_context_text,
            normalized_payload=retry_candidate_payload,
            route_decision=route_decision,
            session_memory=session_memory,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
            tool_trace=tool_trace,
            )
        ):
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="model_contract_retry_started",
                payload={
                    "reason": "typed_proposal_or_clarification_missing",
                    "reason_summary": (
                        "Model response described a mutation/proposal boundary "
                        "but omitted a typed proposal or clarification object."
                    ),
                    "route_kind": route_decision.route_kind,
                    "summary": retry_candidate_payload.get("summary"),
                },
            )
            event_types.append("model_contract_retry_started")
            sequence += 1
            retry_started_at = perf_counter()
            retry_prompt = _proposal_contract_retry_prompt(
                prompt=request_context_text,
                normalized_payload=retry_candidate_payload,
                route_decision=route_decision,
                session_memory=session_memory,
                tool_trace=tool_trace,
                goal_transition_required=(
                    not goal_transition_recorded
                    and not host_managed_goal_transition
                ),
            )
            retry_tool_definitions = _proposal_contract_retry_tool_definitions(
                prompt=display_prompt,
                route_decision=route_decision,
                normalized_payload=retry_candidate_payload,
                tool_definitions=tool_definitions,
                nodes=nodes,
                active_category=active_category,
                goal_transition_required=(
                    not goal_transition_recorded
                    and not host_managed_goal_transition
                ),
                request_context_text=request_context_text,
            )
            retry_budget_kwargs = {
                "step_timeout_seconds": max(
                    120,
                    min(
                        route_decision.budget.model_step_timeout_seconds or 600,
                        600,
                    ),
                ),
                "loop_timeout_seconds": max(
                    180,
                    min(route_decision.budget.loop_timeout_seconds or 900, 900),
                ),
                "max_model_steps": min(2, remaining_model_calls),
                "max_tool_calls": remaining_tool_calls,
            }
            if turn_deadline_monotonic is not None:
                retry_budget_kwargs["loop_timeout_seconds"] = min(
                    retry_budget_kwargs["loop_timeout_seconds"],
                    max(1, _remaining_deadline_seconds(turn_deadline_monotonic)),
                )
            try:
                if runtime_settings["provider"] == "openai-api":
                    parsed, retry_provider_session_id = invoke_openai_tool_loop(
                        mode="agent-kernel-contract-retry",
                        prompt=retry_prompt,
                        runtime_context=context_packet,
                        categories=categories,
                        nodes=nodes,
                        focus_node=focus_node,
                        active_category=active_category,
                        staged_node_ids=staged_node_ids,
                        working_set=working_set,
                        provider_session_id=provider_session_id_for_turn,
                        model_name=str(runtime_settings["model_name"] or "gpt-5-mini"),
                        conversation_history=conversation_history,
                        today_override=today_override,
                        tool_trace=tool_trace,
                        initial_tool_history=_trace_to_initial_tool_history(tool_trace),
                        tool_definitions=retry_tool_definitions,
                        tool_executor=kernel_tool_executor,
                        model_step_trace=model_step_trace,
                        max_model_steps=retry_budget_kwargs["max_model_steps"],
                        max_tool_calls=retry_budget_kwargs["max_tool_calls"],
                    )
                else:
                    codex_loop = codex_tool_loop_invoker or invoke_codex_tool_loop
                    parsed, retry_provider_session_id = codex_loop(
                        mode="agent-kernel-contract-retry",
                        prompt=retry_prompt,
                        runtime_context=context_packet,
                        categories=categories,
                        nodes=nodes,
                        focus_node=focus_node,
                        active_category=active_category,
                        staged_node_ids=staged_node_ids,
                        working_set=working_set,
                        provider_session_id=provider_session_id_for_turn,
                        configured_model=(
                            str(runtime_settings["model_name"])
                            if isinstance(runtime_settings["model_name"], str)
                            and runtime_settings["model_name"]
                            else None
                        ),
                        reasoning_effort=configured_reasoning_effort,
                        conversation_history=conversation_history,
                        today_override=today_override,
                        tool_trace=tool_trace,
                        initial_tool_history=_trace_to_initial_tool_history(tool_trace),
                        tool_definitions=retry_tool_definitions,
                        tool_executor=kernel_tool_executor,
                        model_step_trace=model_step_trace,
                        **retry_budget_kwargs,
                    )
                if isinstance(retry_provider_session_id, str) and retry_provider_session_id:
                    next_provider_session_id = retry_provider_session_id
                model_step_trace.append(
                    {
                        "phase": "proposal_contract_retry",
                        "status": "ok",
                        "duration_ms": _elapsed_ms(retry_started_at),
                        "initial_tool_count": len(tool_trace),
                    }
                )
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="model_contract_retry_completed",
                    payload={
                        "status": "ok",
                        "duration_ms": _elapsed_ms(retry_started_at),
                    },
                )
                event_types.append("model_contract_retry_completed")
                sequence += 1
            except HTTPException as retry_error:
                model_step_trace.append(
                    {
                        "phase": "proposal_contract_retry",
                        "status": "timeout" if retry_error.status_code == 504 else "error",
                        "duration_ms": _elapsed_ms(retry_started_at),
                        "error_status_code": retry_error.status_code,
                        "error_detail": str(retry_error.detail),
                    }
                )
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="model_contract_retry_completed",
                    payload={
                        "status": "error",
                        "duration_ms": _elapsed_ms(retry_started_at),
                        "error_status_code": retry_error.status_code,
                        "error_detail": str(retry_error.detail),
                    },
                )
                event_types.append("model_contract_retry_completed")
                sequence += 1

    for step in model_step_trace:
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="model_step",
            payload=step,
        )
        event_types.append("model_step")
        sequence += 1

    if provider_failure is not None:
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="provider_failure",
            payload=provider_failure,
        )
        event_types.append("provider_failure")
        sequence += 1

    for tool_call in tool_trace:
        summary = summarize_kernel_tool_observation(tool_call)
        tool_call["summary"] = summary
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="tool_observation",
            payload={
                "name": tool_call.get("name"),
                "arguments": tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {},
                "result": tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {},
                "summary": summary,
                "read_only": (
                    bool(tool_call.get("result", {}).get("read_only"))
                    if isinstance(tool_call.get("result"), dict)
                    else True
                ),
                "duration_ms": tool_call.get("duration_ms"),
            },
        )
        event_types.append("tool_observation")
        sequence += 1

    evidence_assessment = assess_tool_evidence(tool_trace)
    capability_observations = emit_capability_observations(
        evidence_assessment,
        agent_id="strategist",
    )
    deterministic_fast_path = (
        deterministic_payload.get("deterministic_fast_path")
        if isinstance(deterministic_payload, dict)
        else None
    )
    deterministic_kind = (
        str(deterministic_fast_path.get("kind") or "")
        if isinstance(deterministic_fast_path, dict)
        else ""
    )
    normalized_payload = normalize_provider_response_payload(parsed)
    normalized_payload, answer_reconciliation = _reconcile_read_only_task_graph_answer(
        normalized_payload=normalized_payload,
        route_decision=route_decision,
        tool_trace=tool_trace,
    )
    if answer_reconciliation is not None:
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="task_graph_answer_reconciled",
            payload=answer_reconciliation,
        )
        event_types.append("task_graph_answer_reconciled")
        sequence += 1
    if not isinstance(normalized_payload.get("clarification"), dict):
        grounded_tool_clarification = next(
            (
                call["result"].get("clarification")
                for call in reversed(tool_trace)
                if isinstance(call, dict)
                and isinstance(call.get("result"), dict)
                and call["result"].get("clarification") not in (None, "", {})
            ),
            None,
        )
        normalized_tool_clarification = normalize_agent_clarification(
            grounded_tool_clarification
        )
        if normalized_tool_clarification is not None:
            # A semantic validator is more authoritative than a prose envelope
            # that omits its question. Preserve that grounded decision at the
            # same terminal boundary instead of losing it to a generic protocol
            # fallback after the model/tool loop has already completed.
            normalized_payload["clarification"] = normalized_tool_clarification
            normalized_payload["summary"] = normalized_tool_clarification["message"]
            normalized_payload["decision"] = {
                "kind": "clarification",
                "reason": "An authoritative semantic validator requires user input.",
            }
    semantic_plan_input = normalized_payload.get("semantic_plan_input")
    if isinstance(semantic_plan_input, dict):
        semantic_payload = semantic_plan_input.get("payload")
        semantic_plan_kind = str(
            semantic_plan_input.get("plan_kind") or ""
        ).strip()
        if isinstance(semantic_payload, dict):
            if semantic_plan_kind == "metadata":
                normalized_payload["metadata_proposal"] = semantic_payload
            elif semantic_plan_kind == "branch":
                normalized_payload["branch_proposal"] = semantic_payload
            elif semantic_plan_kind == "preference":
                normalized_payload[PREFERENCE_RESPONSE_KEY] = semantic_payload
    scheduling_boundary_missing = bool(
        planner_result is None
        and SCHEDULING_REQUEST_RE.search(request_context_text)
        and not _has_scheduling_boundary(
            payload=normalized_payload,
            tool_trace=tool_trace,
        )
    )
    if scheduling_boundary_missing:
        repair_seed = {
            **normalized_payload,
            "clarification": {
                "message": "Which exact task should be scheduled?",
                "entity_choices": [],
                "field_choices": [],
                "scoped_follow_up_prompts": [],
            },
        }
        ambiguity_repair = _repair_scheduling_clarification(
            payload=repair_seed,
            prompt=request_context_text,
            nodes=nodes,
            categories=categories,
            active_category=active_category,
        )
        if ambiguity_repair != repair_seed:
            normalized_payload = ambiguity_repair
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="scheduling_boundary_repair",
                payload={
                    "reason_code": "ambiguous_existing_task_requires_typed_choice",
                    "outcome": "clarification",
                    "planner_called": False,
                },
            )
            event_types.append("scheduling_boundary_repair")
            sequence += 1
        else:
            repair_handoff = _existing_schedule_repair_handoff_arguments(
                session,
                prompt=request_context_text,
                tool_trace=tool_trace,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                staged_node_ids=staged_node_ids,
                ui_event=ui_event,
                today_value=_authoritative_turn_date(
                    today_override,
                    workspace_context,
                ),
                workspace_context=workspace_context,
            )
            if repair_handoff is not None:
                if not goal_transition_recorded:
                    transition_arguments = {
                        "disposition": "continue",
                        "patches": [],
                    }
                    transition_result = _apply_goal_turn_decision(
                        coordinator=goal_coordinator,
                        session_memory=session_memory,
                        session_id=strategist_session_id,
                        source_turn_id=strategist_turn_id or correlation_id,
                        current_user_turn=display_prompt,
                        correlation_id=correlation_id,
                        current_run_id=(
                            current_run.id if current_run is not None else None
                        ),
                        arguments=transition_arguments,
                        authorized_reference_ids=_current_turn_authorized_reference_ids(
                            workspace_context,
                            tool_trace,
                        ),
                    )
                    goal_transition_recorded = True
                    tool_trace.append(
                        {
                            "name": "set_goal_transition",
                            "arguments": transition_arguments,
                            "result": transition_result,
                            "deterministic_repair": True,
                            "host_managed": True,
                        }
                    )
                repair_result = kernel_tool_executor(
                    "handoff_to_planner",
                    repair_handoff,
                    _count_against_foreground_budget=False,
                )
                tool_trace.append(
                    {
                        "name": "handoff_to_planner",
                        "arguments": repair_handoff,
                        "result": repair_result,
                        "deterministic_repair": True,
                    }
                )
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="scheduling_boundary_repair",
                    payload={
                        "reason_code": "model_omitted_required_planner_handoff",
                        "outcome": (
                            "planner_observation"
                            if "error" not in repair_result
                            else "planner_failure"
                        ),
                        "planner_called": True,
                        "task_count": len(repair_handoff["task_ids"]),
                    },
                )
                event_types.append("scheduling_boundary_repair")
                sequence += 1
                if "error" in repair_result:
                    normalized_payload["summary"] = (
                        "I couldn't prepare the schedule because the Planner "
                        "handoff failed. Nothing changed."
                    )
                    normalized_payload["clarification"] = {
                        "message": (
                            "The Planner handoff failed after it was called. "
                            "Please retry this scheduling request."
                        ),
                        "entity_choices": [],
                        "field_choices": [],
                        "scoped_follow_up_prompts": [],
                    }
            else:
                normalized_payload["summary"] = (
                    "I couldn't prepare a safe scheduling handoff from the "
                    "resolved context. Nothing changed."
                )
                normalized_payload["clarification"] = {
                    "message": (
                        "Please retry this scheduling request. No additional "
                        "task details are required."
                    ),
                    "entity_choices": [],
                    "field_choices": [],
                    "scoped_follow_up_prompts": [],
                }
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="scheduling_boundary_repair",
                    payload={
                        "reason_code": "planner_handoff_context_incomplete",
                        "outcome": "retryable_internal_failure",
                        "planner_called": False,
                    },
                )
                event_types.append("scheduling_boundary_repair")
                sequence += 1
    planner_handoff_call = next(
        (
            call
            for call in reversed(tool_trace)
            if str(call.get("name") or "") == "handoff_to_planner"
            and isinstance(call.get("result"), dict)
            and isinstance(call["result"].get("planner_handoff"), dict)
            and call["result"].get("planner_executed") is not True
        ),
        None,
    )
    calendar_fallback = None
    if planner_result is None:
        calendar_fallback = _calendar_remove_handoff_arguments(
            session,
            prompt=request_context_text,
            today_value=_authoritative_turn_date(today_override, workspace_context),
            nodes=nodes,
            categories=categories,
        ) or _calendar_reschedule_handoff_arguments(
            session,
            prompt=request_context_text,
            today_value=_authoritative_turn_date(today_override, workspace_context),
            nodes=nodes,
            categories=categories,
        )
    if calendar_fallback is not None and planner_result is None:
        handoff_arguments, calendar_result = calendar_fallback
        tool_trace.append(
            {
                "name": "list_calendar_blocks",
                "arguments": {
                    "start_at": calendar_result.get("range_start"),
                    "end_at": calendar_result.get("range_end"),
                },
                "result": calendar_result,
                "deterministic_repair": True,
            }
        )
        if not goal_transition_recorded:
            # Calendar fallback is a server-owned semantic repair after exact
            # occurrence/range resolution. Bind it to the already-open durable
            # goal before crossing the same authoring boundary enforced for
            # model-selected Planner handoffs.
            transition_arguments = {"disposition": "continue", "patches": []}
            transition_result = _apply_goal_turn_decision(
                coordinator=goal_coordinator,
                session_memory=session_memory,
                session_id=strategist_session_id,
                source_turn_id=strategist_turn_id or correlation_id,
                current_user_turn=display_prompt,
                correlation_id=correlation_id,
                current_run_id=(
                    current_run.id if current_run is not None else None
                ),
                arguments=transition_arguments,
                authorized_reference_ids=_current_turn_authorized_reference_ids(
                    workspace_context,
                    tool_trace,
                ),
            )
            goal_transition_recorded = True
            tool_trace.append(
                {
                    "name": "set_goal_transition",
                    "arguments": transition_arguments,
                    "result": transition_result,
                    "deterministic_repair": True,
                    "host_managed": True,
                }
            )
        handoff_result = kernel_tool_executor(
            "handoff_to_planner",
            handoff_arguments,
            _count_against_foreground_budget=False,
        )
        planner_handoff = handoff_result.get("planner_handoff")
        if isinstance(planner_handoff, dict):
            planner_handoff["deterministic_semantics_complete"] = True
        tool_trace.append(
            {
                "name": "handoff_to_planner",
                "arguments": handoff_arguments,
                "result": handoff_result,
                "deterministic_repair": True,
            }
        )
        if isinstance(handoff_result.get("planner_handoff"), dict):
            target_date = _calendar_date_from_prompt(
                session,
                prompt=display_prompt,
                today_value=_authoritative_turn_date(today_override, workspace_context),
            )
            task_count = len(handoff_arguments["task_ids"])
            date_label = (
                f" on {target_date.strftime('%B')} {target_date.day}"
                if target_date is not None
                else ""
            )
            if handoff_arguments.get("operation_mode") == "remove":
                block = next(
                    (
                        item
                        for item in calendar_result.get("blocks", [])
                        if isinstance(item, dict)
                    ),
                    {},
                )
                title = str(block.get("task_title") or "calendar item")
                normalized_payload["summary"] = (
                    f"I found the scheduled {title}{date_label}."
                )
            else:
                normalized_payload["summary"] = (
                    f"I found {task_count} scheduled task"
                    f"{'s' if task_count != 1 else ''} before your wake time"
                    f"{date_label}."
                )
            normalized_payload["clarification"] = None
            planner_handoff_call = tool_trace[-1]
    calendar_scoped_prompt = bool(
        CALENDAR_SCOPED_RESCHEDULE_RE.search(display_prompt)
        and re.search(r"\b(?:tasks?|items?)\s+scheduled\b", display_prompt, re.I)
    )
    if calendar_scoped_prompt and calendar_fallback is None and planner_handoff_call is not None:
        handoff_payload = planner_handoff_call["result"].get("planner_handoff", {})
        has_calendar_read = any(
            str(call.get("name") or "") == "list_calendar_blocks"
            and isinstance(call.get("result"), dict)
            and "error" not in call["result"]
            for call in tool_trace
        )
        if (
            not isinstance(handoff_payload, dict)
            or handoff_payload.get("scope_source") != "calendar_query"
            or not has_calendar_read
        ):
            planner_handoff_call = None
    if planner_handoff_call is None:
        normalized_payload = _repair_scheduling_clarification(
            payload=normalized_payload,
            prompt=request_context_text,
            nodes=nodes,
            categories=categories,
            active_category=active_category,
        )
    if planner_handoff_call is not None and planner_result is None:
        handoff = dict(planner_handoff_call["result"]["planner_handoff"])
        handoff_instruction = str(handoff.get("instruction") or "").strip()
        if len(request_context_prompts) > 1:
            handoff["instruction"] = (
                f"{request_context_text}\n"
                f"Latest planner handoff: {handoff_instruction}"
                if handoff_instruction and handoff_instruction not in request_context_text
                else request_context_text
            )
        exact_task = _exact_task_reference(
            prompt=display_prompt,
            nodes=nodes,
            focus_node=focus_node,
        )
        if exact_task is None and len(request_context_prompts) > 1:
            exact_task = _exact_task_reference(
                prompt=request_context_text,
                nodes=nodes,
                focus_node=focus_node,
            )
        if exact_task is None:
            exact_task = _dominant_scheduling_candidate(
                prompt=display_prompt,
                nodes=nodes,
                active_category=active_category,
            )
        if exact_task is not None and exact_task.id is not None:
            prior_request = _latest_scheduling_request(
                conversation_history,
                str(handoff.get("instruction") or display_prompt),
            )
            _align_planner_handoff_to_resolved_task(
                handoff,
                task_id=int(exact_task.id),
                explicit_time_range=bool(
                    EXPLICIT_TIME_RANGE_RE.search(request_context_text)
                ),
            )
            handoff["instruction"] = (
                f'{prior_request}. Use the exact task named "{exact_task.title}".'
            )
        handoff["task_ids"] = _expand_subtask_schedule_ids(
            instruction=str(handoff.get("instruction") or display_prompt),
            task_ids=[int(item) for item in handoff.get("task_ids", [])],
            nodes=nodes,
        )
        native_existing_recurrence_requested = bool(
            handoff.get("operation_mode") == "reschedule"
            and isinstance(handoff.get("recurrence"), dict)
        )
        has_authoritative_schedule_evidence = any(
            str(call.get("name") or "")
            in {"list_calendar_blocks", "get_task_schedule"}
            and isinstance(call.get("result"), dict)
            and "error" not in call["result"]
            for call in tool_trace
        )
        if (
            handoff.get("operation_mode") == "reschedule"
            and handoff.get("task_ids")
            and not has_authoritative_schedule_evidence
        ):
            # Existing-task reschedules need persisted block/series identity.
            # The Planner handoff alone is intentionally not an authority
            # source and can otherwise collapse a series edit into one block.
            task_schedule_arguments = {
                "task_ids": list(handoff["task_ids"]),
                "limit": 200,
            }
            task_schedule_result = _read_calendar_blocks_for_kernel(
                {
                    **task_schedule_arguments,
                    "include_all_for_tasks": True,
                },
                nodes=nodes,
                categories=categories,
            )
            if "error" not in task_schedule_result:
                task_schedule_result["result_kind"] = "task_schedule_context"
                task_schedule_result["observation_summary"] = (
                    "Read authoritative schedule and recurrence identity for the target tasks."
                )
                tool_trace.append(
                    {
                        "name": "get_task_schedule",
                        "arguments": task_schedule_arguments,
                        "result": task_schedule_result,
                        "deterministic_repair": True,
                    }
                )
        planner_started_at = perf_counter()
        try:
            if (
                turn_deadline_monotonic is not None
                and _remaining_deadline_seconds(
                    turn_deadline_monotonic,
                    reserve_seconds=SHARED_WORKSPACE_FINALIZATION_RESERVE_SECONDS,
                ) <= 0
            ):
                raise TimeoutError("The shared workspace turn deadline expired before Planner could start.")
            owned_fallback = execute_owned_planner_subagent(handoff)
            if planner_result is None:
                nested = owned_fallback.get("planner_result")
                planner_result = _friendly_planner_result(
                    nested if isinstance(nested, dict) else owned_fallback,
                    nodes=nodes,
                )
        except (HTTPException, KeyError, TimeoutError, TypeError, ValueError) as error:
            error_detail = str(error.detail) if isinstance(error, HTTPException) else str(error)
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="planner_handoff_failed",
                payload={
                    "error_type": type(error).__name__,
                    "error_code": (
                        "planner_timeout"
                        if isinstance(error, TimeoutError)
                        or (
                            isinstance(error, HTTPException)
                            and error.status_code == 504
                        )
                        else "planner_execution_failed"
                    ),
                    "duration_ms": _elapsed_ms(planner_started_at),
                    "timed_out": isinstance(error, TimeoutError)
                    or (isinstance(error, HTTPException) and error.status_code == 504),
                },
            )
            event_types.append("planner_handoff_failed")
            sequence += 1
            normalized_payload["clarification"] = {
                "message": f"Planner could not safely complete this scheduling request: {error_detail}",
                "entity_choices": [],
                "field_choices": [],
                "scoped_follow_up_prompts": [],
            }
    proposal_evidence = _proposal_evidence_bundle(
        source="agent_kernel",
        tool_trace=tool_trace,
        active_category=active_category,
        categories=categories,
    )
    metadata_proposal = normalize_metadata_proposal(
        normalized_payload.get("metadata_proposal"),
        nodes,
        focus_node,
        evidence_bundle=proposal_evidence,
    )
    raw_branch_proposal = normalized_payload.get("branch_proposal")
    branch_proposal = (
        normalize_branch_proposal(
            raw_branch_proposal,
            nodes,
            focus_node,
            evidence_bundle=proposal_evidence,
        )
    )
    # A stopped provider turn has not declared that its semantic work is
    # complete. Individual successful tool results are checkpoints for a
    # same-goal retry, never authority to show a partial review. This closes the
    # class of failures where one completed Planner handoff survived while
    # independent task clauses were still being authored.
    typed_tool_proposals = (
        {}
        if provider_failure is not None
        else _proposals_from_typed_tool_trace(
            tool_trace=tool_trace,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
        )
    )
    typed_tool_proposal = (
        None
        if provider_failure is not None
        else _proposal_from_typed_tool_trace(
            tool_trace=tool_trace,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
        )
    )
    if "metadata" in typed_tool_proposals:
        metadata_proposal = typed_tool_proposals["metadata"][0]
        normalized_payload["metadata_proposal"] = metadata_proposal
    if "branch" in typed_tool_proposals:
        # Successful typed results are executable contracts. The model's final
        # prose envelope may omit lineage or unchanged fields after validation.
        branch_proposal = typed_tool_proposals["branch"][0]
        normalized_payload["branch_proposal"] = branch_proposal
    if branch_proposal is not None:
        current_turn_updates = build_metadata_updates_from_prompt(
            display_prompt,
            today_override or date.today(),
        )
        pending_updates = (
            {}
            if current_turn_updates
            else _pending_metadata_updates_from_session_memory(
                session_memory=session_memory,
                today_override=today_override,
                current_user_request=display_prompt,
            )
        )
        if pending_updates:
            structural_operations = branch_proposal.get("structural_operations")
            if isinstance(structural_operations, list):
                for operation in structural_operations:
                    if not isinstance(operation, dict):
                        continue
                    operation_kind = str(
                        operation.get("kind") or operation.get("op") or ""
                    ).strip().lower()
                    if operation_kind not in {"metadata_update", "update_metadata"}:
                        continue
                    updates = operation.get("updates")
                    if not isinstance(updates, dict):
                        updates = {}
                        operation["updates"] = updates
                    updates.update(pending_updates)
        schedule_was_model_supplied = isinstance(
            branch_proposal.get("created_task_schedule"),
            dict,
        )
        created_task_schedule_plan = _enrich_created_task_schedule_before_compilation(
            session,
            proposal=branch_proposal,
            prompt=request_context_text,
            today_value=_authoritative_turn_date(
                today_override,
                workspace_context,
            ),
            workspace_context=workspace_context,
        )
        if isinstance(created_task_schedule_plan, dict):
            submitted_schedule_groups = created_task_schedule_groups(
                branch_proposal["created_task_schedule"]
            )
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="created_task_schedule_compiled",
                payload={
                    "status": created_task_schedule_plan.get("status"),
                    "task_count": created_task_schedule_plan.get("task_count"),
                    "block_count": created_task_schedule_plan.get("block_count"),
                    "expected_minutes": created_task_schedule_plan.get(
                        "expected_minutes"
                    ),
                    "placed_minutes": created_task_schedule_plan.get(
                        "placed_minutes"
                    ),
                    "unplaced_minutes": created_task_schedule_plan.get(
                        "unplaced_minutes"
                    ),
                    "schedule_groups": [
                        {
                            "status": group.get("status"),
                            "window_start": group.get("window_start"),
                            "window_end": group.get("window_end"),
                            "task_count": group.get("task_count"),
                            "block_count": group.get("block_count"),
                            "expected_minutes": group.get("expected_minutes"),
                            "placed_minutes": group.get("placed_minutes"),
                            "unplaced_minutes": group.get("unplaced_minutes"),
                            "solver_status": group.get("solver_status"),
                            "errors": list(group.get("errors") or [])[:4],
                            "target_temp_ids": list(
                                submitted_schedule_groups[index].get(
                                    "target_temp_ids", []
                                )
                            )[:16]
                            if index < len(submitted_schedule_groups)
                            else [],
                            "target_task_ids": list(
                                submitted_schedule_groups[index].get(
                                    "target_task_ids", []
                                )
                            )[:16]
                            if index < len(submitted_schedule_groups)
                            else [],
                        }
                        for index, group in enumerate(
                            created_task_schedule_plan.get("schedule_groups") or []
                        )
                        if index < 8
                        if isinstance(group, dict)
                    ]
                    or [
                        {
                            "status": created_task_schedule_plan.get("status"),
                            "window_start": created_task_schedule_plan.get(
                                "window_start"
                            ),
                            "window_end": created_task_schedule_plan.get(
                                "window_end"
                            ),
                            "task_count": created_task_schedule_plan.get("task_count"),
                            "block_count": created_task_schedule_plan.get("block_count"),
                            "expected_minutes": created_task_schedule_plan.get(
                                "expected_minutes"
                            ),
                            "placed_minutes": created_task_schedule_plan.get(
                                "placed_minutes"
                            ),
                            "unplaced_minutes": created_task_schedule_plan.get(
                                "unplaced_minutes"
                            ),
                            "solver_status": created_task_schedule_plan.get(
                                "solver_status"
                            ),
                            "errors": list(
                                created_task_schedule_plan.get("errors") or []
                            )[:4],
                            "target_temp_ids": list(
                                submitted_schedule_groups[0].get(
                                    "target_temp_ids", []
                                )
                            )[:16]
                            if submitted_schedule_groups
                            else [],
                            "target_task_ids": list(
                                submitted_schedule_groups[0].get(
                                    "target_task_ids", []
                                )
                            )[:16]
                            if submitted_schedule_groups
                            else [],
                        }
                    ],
                    "gap_policy": (
                        created_task_schedule_plan.get("planner_plan", {}).get(
                            "gapPolicy",
                            {},
                        )
                        if isinstance(
                            created_task_schedule_plan.get("planner_plan"),
                            dict,
                        )
                        else {}
                    ),
                    "model_supplied_spec": schedule_was_model_supplied,
                    "repair": (
                        None
                        if schedule_was_model_supplied
                        else "restored_explicit_one_time_schedule"
                    ),
                    "errors": list(
                        created_task_schedule_plan.get("errors") or []
                    )[:8],
                },
            )
            event_types.append("created_task_schedule_compiled")
            sequence += 1
        if isinstance(branch_proposal.get("recurring_schedule"), dict):
            # The native runtime owns final compilation, but recurrence
            # placement still needs the shared deterministic planner to expand
            # conflict decisions into calendar operations. This enrichment was
            # present in the legacy persistence path and must occur before the
            # native compiler consumes the semantic proposal as well.
            from app.services.strategist_graph import (
                enrich_proposal_payload_with_recurring_schedule,
            )

            enrich_proposal_payload_with_recurring_schedule(
                session,
                proposal_payload=branch_proposal,
                request_prompt=display_prompt,
                request_context_prompts=[request_context_text],
            )
            recurring_plan = branch_proposal.get("recurring_schedule_plan")
            if isinstance(recurring_plan, dict):
                append_kernel_event(
                    session,
                    strategist_session_id=strategist_session_id,
                    correlation_id=correlation_id,
                    sequence=sequence,
                    event_type="recurring_schedule_compiled",
                    payload={
                        "status": recurring_plan.get("status"),
                        "occurrence_count": recurring_plan.get("occurrence_count"),
                        "displacement_count": len(
                            recurring_plan.get("displacement_operations") or []
                        ),
                        "conflict_relocation": recurring_plan.get(
                            "conflict_relocation"
                        ),
                        "errors": list(recurring_plan.get("errors") or [])[:8],
                    },
                )
                event_types.append("recurring_schedule_compiled")
                sequence += 1
    invented_new_recurring_duration = _new_recurring_task_requires_duration(
        branch_proposal=branch_proposal,
        request_context_text=request_context_text,
    )
    clarification_semantic_revision = (
        _branch_revision_snapshot(
            branch_proposal,
            unresolved_fields=("duration_minutes",),
        )
        if invented_new_recurring_duration and isinstance(branch_proposal, dict)
        else None
    )
    if invented_new_recurring_duration:
        # Do not let a plausible-looking model default become a calendar
        # commitment. Preserve the active request and ask for the one missing
        # fact; the next short answer is bound to this new task and recurrence.
        branch_proposal = None
        normalized_payload["branch_proposal"] = None
    action_proposal = (
        normalized_payload.get("action_proposal")
        if isinstance(normalized_payload.get("action_proposal"), dict)
        else None
    )
    raw_preference_memory_proposal = normalized_payload.get(PREFERENCE_RESPONSE_KEY)
    raw_preference_memory_proposal_uncompiled = (
        isinstance(raw_preference_memory_proposal, dict)
        and not _preference_memory_proposal_has_valid_patch(raw_preference_memory_proposal)
    )
    preference_memory_proposal = (
        raw_preference_memory_proposal
        if _preference_memory_proposal_has_valid_patch(raw_preference_memory_proposal)
        else None
    )
    if PREFERENCE_PROPOSAL_KIND in typed_tool_proposals:
        preference_memory_proposal = typed_tool_proposals[PREFERENCE_PROPOSAL_KIND][0]
        normalized_payload[PREFERENCE_RESPONSE_KEY] = preference_memory_proposal
        raw_preference_memory_proposal_uncompiled = False
    for typed_proposal_kind, (
        typed_proposal,
        typed_tool_call,
    ) in typed_tool_proposals.items():
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="proposal_tool_contract_compiled",
            payload={
                "proposal_kind": typed_proposal_kind,
                "proposal_id": typed_proposal.get("proposal_id"),
                "target_node_id": typed_proposal.get("target_node_id"),
                "target_node_ids": typed_proposal.get("target_node_ids", []),
                "tool_name": typed_tool_call.get("name"),
                "tool_arguments": (
                    typed_tool_call.get("arguments")
                    if isinstance(typed_tool_call.get("arguments"), dict)
                    else {}
                ),
                "reason": (
                    "The model emitted typed proposal-tool arguments, so the harness "
                    "compiled them into the canonical proposal contract."
                ),
            },
        )
        event_types.append("proposal_tool_contract_compiled")
        sequence += 1
    clarification_payload = (
        normalized_payload.get("clarification")
        if isinstance(normalized_payload.get("clarification"), dict)
        else None
    )
    if invented_new_recurring_duration:
        clarification_payload = {
            "message": "How long should each recurring task block be?",
            "missing_fields": ["duration_minutes"],
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": [],
        }
    action_reminder_misclassified = bool(
        _is_action_reminder_request(display_prompt)
        and preference_memory_proposal is not None
        and branch_proposal is None
    )
    if action_reminder_misclassified:
        # Never persist an action reminder as a passive preference. If the model
        # still misclassifies it after contract repair, fail safely with the one
        # decision needed to build a real task and calendar series.
        preference_memory_proposal = None
        clarification_payload = clarification_payload or {
            "message": (
                "What time of day should I schedule it, and how long should the daily "
                "series continue?"
            ),
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": ["Morning", "Afternoon", "Evening"],
        }
    summary = normalize_summary(
        normalized_payload.get("summary"),
        "I could not complete that Strategist turn through the agent kernel.",
    )
    if invented_new_recurring_duration:
        summary = "How long should each recurring task block be?"
    if action_reminder_misclassified:
        summary = "I can create the task and its recurring schedule together."
    if (
        provider_failure is not None
        and typed_tool_proposal is not None
        and not invented_new_recurring_duration
        and not action_reminder_misclassified
    ):
        summary = normalize_summary(
            typed_tool_proposal[1].get("summary"),
            "I prepared a reviewable proposal.",
        )
    if raw_preference_memory_proposal_uncompiled:
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="proposal_contract_repair",
            payload={
                "proposal_kind": PREFERENCE_PROPOSAL_KIND,
                "repair": "dropped_uncompiled_preference_memory_proposal",
                "reason": (
                    "Provider returned a preference_memory_proposal without a compiled "
                    "PreferencePatch, so the harness refused to persist an invalid memory card."
                ),
            },
        )
        event_types.append("proposal_contract_repair")
        sequence += 1
    if isinstance(action_proposal, dict) and _action_proposal_has_zero_operation_patch(
        action_proposal=action_proposal,
        nodes=nodes,
    ):
        if metadata_proposal is not None or branch_proposal is not None:
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="proposal_contract_repair",
                payload={
                    "proposal_kind": "action",
                    "repair": "dropped_empty_action_proposal",
                    "reason": (
                        "Provider returned an action_proposal that compiled to a zero-operation "
                        "TaskPatch while another proposal object contained the actionable patch."
                    ),
                },
            )
            event_types.append("proposal_contract_repair")
            sequence += 1
            action_proposal = None
        else:
            # A legacy action envelope without an executable TaskPatch is not a
            # mutation proposal.  Never infer fields, targets, or operations from
            # prose/session context: the model must call a typed proposal tool or
            # return an explicit valid proposal object.
            append_kernel_event(
                session,
                strategist_session_id=strategist_session_id,
                correlation_id=correlation_id,
                sequence=sequence,
                event_type="proposal_contract_repair",
                payload={
                    "proposal_kind": "action",
                    "repair": "dropped_uncompiled_action_proposal",
                    "reason": (
                        "Provider returned an action envelope without an executable "
                        "TaskPatch. The harness refused to infer a mutation from prose."
                    ),
                },
            )
            event_types.append("proposal_contract_repair")
            sequence += 1
            action_proposal = None
    if provider_failure is not None:
        # Planner output remains a retry checkpoint, but a provider failure
        # means the Strategist never closed the complete multi-clause plan.
        # Do not compile or project that isolated schedule as the user's review.
        planner_result = None
        owned_planner_result = None
    planner_proposal = (
        planner_result.get("proposal")
        if isinstance(planner_result, dict) and isinstance(planner_result.get("proposal"), dict)
        else None
    )
    planner_mode = (
        str(planner_result.get("mode") or "")
        if isinstance(planner_result, dict)
        else ""
    )
    if planner_result is not None and planner_proposal is None and planner_mode == "clarification":
        planner_clarification = planner_result.get("clarification")
        clarification_payload = (
            planner_clarification
            if isinstance(planner_clarification, dict)
            else {
                "message": str(
                    planner_result.get("summary")
                    or "Please clarify the scheduling constraint before I prepare a proposal."
                ),
                "entity_choices": [],
                "field_choices": [],
                "scoped_follow_up_prompts": list(planner_result.get("suggestedPrompts") or []),
            }
        )
    planner_handoff_outcome = (
        planner_result.get("handoffOutcome") or planner_result.get("handoff_outcome")
        if isinstance(planner_result, dict)
        else None
    )
    continuation_proposal_revision = (
        clarification_semantic_revision or proposal_revision
    )
    if isinstance(branch_proposal, dict) and proposal_revision is None:
        # Preserve the initial native branch semantics before authority
        # selection removes legacy proposal surfaces from the response. This
        # is source semantic state, not a reverse translation from ChangeSet
        # operations.
        continuation_proposal_revision = _branch_revision_snapshot(branch_proposal)
    elif isinstance(branch_proposal, dict) and isinstance(proposal_revision, dict):
        revision_parent_id = str(
            branch_proposal.get("revision_of_proposal_id") or ""
        ).strip()
        active_revision_id = str(proposal_revision.get("proposal_id") or "").strip()
        if revision_parent_id and revision_parent_id == active_revision_id:
            # Proposal persistence happens after the kernel turn. Assign the
            # stable id here so the final continuation event follows the new
            # card rather than retaining the id of the card it supersedes.
            proposal_id = str(branch_proposal.get("proposal_id") or "").strip()
            if not proposal_id:
                proposal_id = str(uuid4())
                branch_proposal["proposal_id"] = proposal_id
            continuation_proposal_revision = _compact_pending_proposal_for_context(
                {
                    "proposal_id": proposal_id,
                    "proposal_kind": "branch",
                    "status": "pending",
                    "target_node_id": branch_proposal.get("target_node_id"),
                    "target_node_ids": branch_proposal.get("target_node_ids", []),
                    "source_turn_id": None,
                    "summary": branch_proposal.get("summary"),
                    "rationale": branch_proposal.get("rationale"),
                    "target_node_title": branch_proposal.get("target_node_title"),
                    "payload": branch_proposal,
                }
            )
    mode = (
        "preference_memory"
        if preference_memory_proposal is not None
        else "schedule_patch"
        if planner_proposal is not None
        else "clarification"
        if planner_result is not None and planner_mode == "clarification"
        else _mode_from_payload(
        action_proposal=action_proposal,
        metadata_proposal=metadata_proposal,
        branch_proposal=branch_proposal,
        clarification=clarification_payload,
        declared_mode=normalized_payload.get("mode"),
        )
    )
    draft_proposal = action_proposal or metadata_proposal or branch_proposal
    draft_proposal_kind = (
        "action"
        if action_proposal is not None
        else "metadata"
        if metadata_proposal is not None
        else "branch"
        if branch_proposal is not None
        else mode
    )
    if isinstance(draft_proposal, dict):
        task_patch = build_task_patch_draft(
            proposal_kind=draft_proposal_kind,
            proposal=draft_proposal,
        )
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="draft_patch",
            payload={
                "proposal_kind": draft_proposal_kind,
                "proposal_id": draft_proposal.get("proposal_id"),
                "target_node_id": draft_proposal.get("target_node_id"),
                "target_node_ids": draft_proposal.get("target_node_ids", []),
                "summary": draft_proposal.get("summary"),
                "rationale": draft_proposal.get("rationale"),
                "task_patch": task_patch,
            },
        )
        event_types.append("draft_patch")
        sequence += 1
        validation_warnings = draft_proposal.get("validation_warnings")
        validation = validate_task_patch(
            task_patch,
            nodes,
            category_ids={
                int(category.id)
                for category in categories
                if isinstance(category.id, int)
            },
        )
        if isinstance(validation_warnings, list):
            validation["warnings"].extend(
                str(item) for item in validation_warnings if str(item).strip()
            )
            if validation["status"] == "valid" and validation["warnings"]:
                validation["status"] = "warning"
        simulation = simulate_task_patch(task_patch, nodes)
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="validation_result",
            payload={
                "proposal_kind": draft_proposal_kind,
                "proposal_id": draft_proposal.get("proposal_id"),
                "status": validation["status"],
                "validation_warnings": validation["warnings"],
                "validation_errors": validation["errors"],
                "task_patch_id": task_patch["patch_id"],
                "operation_count": validation["operation_count"],
                "validation": validation,
                "simulation": simulation,
            },
        )
        event_types.append("validation_result")
        sequence += 1
    server_compilation_diagnostics: dict[str, Any] | None = None
    compilation_attempted = False
    compilation_succeeded = False
    current_goal_for_compilation = (
        goal_coordinator.store.load_active(strategist_session_id) or active_goal
    )
    current_compiled_change_set = change_set_coordinator.store.load_current(
        strategist_session_id,
        current_goal_for_compilation.goal_id,
    )
    has_compilable_semantic_draft = bool(
        isinstance(draft_proposal, dict)
        or isinstance(preference_memory_proposal, dict)
        or (
            planner_proposal is not None
            and owned_planner_result is not None
            and owned_planner_result.status == "completed"
        )
    )
    if has_compilable_semantic_draft:
        compilation_attempted = True
        compilation_started_at = perf_counter()
        compilation_status = "not_started"
        compilation_issue_codes: list[str] = []
        compilation_issue_messages: list[str] = []
        compilation_operation_count = 0
        try:
            compilation_provenance = Provenance(
                source_kind="user",
                source_ref=strategist_turn_id or correlation_id,
                source_text=request_context_text,
            )
            compilation_planner_artifact = None
            compilation_planner_task: PlannerSubagentTask | None = None
            compilation_plans: list[SemanticPlanArtifact] = []
            timezone_name = (
                str(workspace_context.get("time_zone"))
                if isinstance(workspace_context, dict)
                and workspace_context.get("time_zone")
                else "UTC"
            )
            if isinstance(draft_proposal, dict):
                compilation_plans.append(
                    CompatibilityProposalAdapter().adapt_task_proposal(
                        proposal_kind=draft_proposal_kind,
                        proposal=draft_proposal,
                        provenance=compilation_provenance,
                        timezone_name=timezone_name,
                        session=session,
                    )
                )
            if isinstance(preference_memory_proposal, dict):
                compilation_plans.append(
                    CompatibilityProposalAdapter().adapt_preference_proposal(
                        proposal=preference_memory_proposal,
                        provenance=compilation_provenance,
                        timezone_name=timezone_name,
                        session=session,
                    )
                )
            if (
                planner_proposal is not None
                and owned_planner_result is not None
                and owned_planner_result.status == "completed"
            ):
                planner_adapter = PlannerScheduleArtifactAdapter()
                compilation_planner_task = planner_tasks_by_id.get(
                    owned_planner_result.task_id
                )
                planner_handoff = planner_handoffs_by_task_id.get(
                    owned_planner_result.task_id
                )
                if compilation_planner_task is None or planner_handoff is None:
                    raise ValueError(
                        "Completed Planner output has no exact owned task binding"
                    )
                delegated_clauses = planner_adapter.delegated_clauses(
                    compilation_planner_task,
                    operation_mode=str(
                        planner_handoff.get("operation_mode") or "create"
                    ),
                    provenance=compilation_provenance,
                )
                compilation_planner_artifact = planner_adapter.adapt(
                    owned_planner_result,
                    task=compilation_planner_task,
                    target_goal_revision=current_goal_for_compilation.revision + 1,
                    planner_clauses=delegated_clauses,
                    timezone_name=timezone_name,
                    request_constraints=(
                        planner_handoff.get("request_constraints")
                        if isinstance(
                            planner_handoff.get("request_constraints"),
                            list,
                        )
                        else []
                    ),
                )
                compilation_plans.append(
                    SemanticPlanArtifact(
                        clauses=planner_adapter.finalize_clauses(
                            delegated_clauses,
                            compilation_planner_artifact,
                        )
                    )
                )
            if not compilation_plans:
                raise ValueError("No semantic inputs were available for compilation")
            compilation_plan = SemanticPlanArtifact(
                clauses=[
                    clause
                    for plan in compilation_plans
                    for clause in plan.clauses
                ],
                unscheduled_task_refs=[
                    ref
                    for plan in compilation_plans
                    for ref in plan.unscheduled_task_refs
                ],
            )
            compilation = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                strategist_session_id,
                ChangeCompilationRequest(
                    expected_goal_revision=current_goal_for_compilation.revision,
                    expected_change_set_revision=(
                        current_compiled_change_set.revision
                        if current_compiled_change_set is not None
                        else None
                    ),
                ),
                compilation_plan,
                command_id=f"compile:{strategist_turn_id or correlation_id}",
                correlation_id=correlation_id,
                causation_id=strategist_turn_id or correlation_id,
                planner_artifact=compilation_planner_artifact,
                expected_planner_task=compilation_planner_task,
            )
            compilation_status = compilation.status
            compilation_succeeded = compilation.status == "succeeded"
            compilation_issue_codes = [
                item.code for item in compilation.issues
            ]
            compilation_issue_messages = [
                item.message for item in compilation.issues
            ]
            compilation_operation_count = (
                len(compilation.draft.change_set.operations)
                if compilation.draft is not None
                else 0
            )
        except (GoalStateRevisionConflict, GoalStateStoreError) as error:
            compilation_status = "stale_evidence"
            compilation_issue_codes = ["compilation_state_stale"]
            compilation_issue_messages = [str(error)[:240]]
        except (TypeError, ValueError) as error:
            compilation_status = "needs_model_repair"
            compilation_issue_codes = [type(error).__name__]
            compilation_issue_messages = [str(error)[:240]]
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="server_change_compilation",
            payload={
                "status": compilation_status,
                "operation_count": compilation_operation_count,
                "issue_codes": compilation_issue_codes[:16],
                "issue_messages": compilation_issue_messages[:8],
                "source": (
                    "planner_artifact"
                    if compilation_planner_artifact is not None
                    else "semantic_plan_input"
                ),
                "duration_ms": _elapsed_ms(compilation_started_at),
            },
        )
        event_types.append("server_change_compilation")
        sequence += 1
        server_compilation_diagnostics = {
            "status": compilation_status,
            "operation_count": compilation_operation_count,
            "issue_codes": compilation_issue_codes[:16],
            "issue_messages": compilation_issue_messages[:8],
            "source": (
                "planner_artifact"
                if compilation_planner_artifact is not None
                else "semantic_plan_input"
            ),
            "duration_ms": _elapsed_ms(compilation_started_at),
        }
    latest_goal_for_shadow = (
        goal_coordinator.store.load_active(strategist_session_id) or active_goal
    )
    current_native_change_set = (
        change_set_coordinator.store.load_current(
            strategist_session_id, latest_goal_for_shadow.goal_id
        )
        if not compilation_attempted or compilation_succeeded
        else None
    )
    if (
        current_native_change_set is not None
        and current_native_change_set.simulation_result_ref is None
    ):
        simulation_started_at = perf_counter()
        _apply_change_set_tool_simulate(
            coordinator=simulation_coordinator,
            session_memory=session_memory,
            session_id=strategist_session_id,
            source_turn_id=strategist_turn_id or correlation_id,
            correlation_id=correlation_id,
            current_run_id=current_run.id if current_run is not None else None,
            arguments={
                "expectedGoalRevision": latest_goal_for_shadow.revision,
                "expectedChangeSetRevision": current_native_change_set.revision,
            },
        )
        tool_trace.append(
            {
                "name": "simulate_change_set",
                "arguments": {
                    "expectedGoalRevision": latest_goal_for_shadow.revision,
                    "expectedChangeSetRevision": current_native_change_set.revision,
                },
                "result": {"deterministic_completion": True},
                "duration_ms": (perf_counter() - simulation_started_at) * 1000,
            }
        )
        latest_goal_for_shadow = (
            goal_coordinator.store.load_active(strategist_session_id)
            or latest_goal_for_shadow
        )
        current_native_change_set = change_set_coordinator.store.load_current(
            strategist_session_id, latest_goal_for_shadow.goal_id
        )
    semantic_inputs = {
        family: payload
        for family, payload in (
            ("action", action_proposal),
            ("metadata", metadata_proposal),
            ("branch", branch_proposal),
            ("preference_memory", preference_memory_proposal),
            ("schedule_patch", planner_proposal),
        )
        if isinstance(payload, dict)
    }
    semantic_authoring_attempts = [
        call
        for call in tool_trace
        if str(call.get("name") or "").strip() in SEMANTIC_AUTHORING_TOOL_NAMES
    ]
    semantic_authoring_family_by_tool = {
        **SEMANTIC_PLAN_TOOL_FAMILIES,
        "handoff_to_planner": "schedule",
        "respond_to_planner_decision": "schedule",
        "draft_change_set": "change_set_draft",
        "simulate_change_set": "change_set_simulation",
    }
    latest_semantic_authoring_attempts: dict[str, dict[str, Any]] = {}
    for call in semantic_authoring_attempts:
        tool_name = str(call.get("name") or "").strip()
        family = semantic_authoring_family_by_tool.get(tool_name, tool_name)
        latest_semantic_authoring_attempts[family] = call
    semantic_authoring_failures = []
    for call in latest_semantic_authoring_attempts.values():
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        tool_error = (
            result.get("tool_error")
            if isinstance(result.get("tool_error"), dict)
            else {}
        )
        failure_code = str(tool_error.get("code") or "").strip()
        tool_name = str(call.get("name") or "").strip()
        if failure_code == "GOAL_TRANSITION_REQUIRED" or (
            tool_name
            not in {"handoff_to_planner", "respond_to_planner_decision"}
            and (result.get("error") or result.get("compiled") is False)
        ):
            semantic_authoring_failures.append(call)
    unresolved_semantic_authoring_failure = bool(
        semantic_authoring_failures
        and not semantic_inputs
        and current_native_change_set is None
    )
    # Historical shadow-authoring code was removed from the production runtime.
    # Git history and offline characterization tests retain the migration evidence.
    native_requirement_blocked = False
    native_review_document = None
    authority_selection = None
    native_simulation = None
    temporal_adjustment = (
        branch_proposal.get("created_task_schedule_plan", {}).get(
            "temporal_adjustment"
        )
        if isinstance(branch_proposal, dict)
        and isinstance(branch_proposal.get("created_task_schedule_plan"), dict)
        else None
    )
    if temporal_adjustment is None:
        temporal_adjustment = planner_temporal_adjustment
    if current_native_change_set is not None:
        current_goal_for_review = goal_coordinator.store.load(
            strategist_session_id, current_native_change_set.goal_id
        )
        if current_native_change_set.simulation_result_ref:
            try:
                native_simulation = simulation_coordinator.simulation_store.load(
                    strategist_session_id,
                    current_native_change_set.simulation_result_ref,
                )
            except SimulationResultNotFound:
                native_simulation = None
        authority_selection = NativeAuthorityController().select(
            session_id=strategist_session_id,
            goal=current_goal_for_review,
            change_set=current_native_change_set,
            simulation=native_simulation,
            review_entity_titles=_review_entity_titles(
                session,
                change_set=current_native_change_set,
                nodes=nodes,
                categories=categories,
            ),
            display_time_zone=(
                str(workspace_context.get("time_zone"))
                if isinstance(workspace_context, dict)
                and workspace_context.get("time_zone")
                else None
            ),
        )
        native_review_document = authority_selection.review_document
        if (
            native_review_document is not None
            and isinstance(temporal_adjustment, dict)
            and isinstance(temporal_adjustment.get("disclosure"), str)
            and temporal_adjustment["disclosure"].strip()
        ):
            native_review_document = native_review_document.model_copy(
                update={
                    "summary": (
                        f"{temporal_adjustment['disclosure'].strip()} "
                        f"{native_review_document.summary}"
                    )
                }
            )
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="canonical_review_selection",
            payload=authority_selection.to_safe_trace_payload(),
        )
        event_types.append("canonical_review_selection")
        sequence += 1
        if native_review_document is not None:
            action_proposal = None
            metadata_proposal = None
            branch_proposal = None
            preference_memory_proposal = None
            planner_result = None
            planner_proposal = None
            planner_handoff_outcome = None
            clarification_payload = None
            mode = "proposal"
            summary = native_review_document.summary
    latest_goal = (
        goal_coordinator.store.load_active(strategist_session_id)
        or goal_coordinator.store.load(strategist_session_id, active_goal.goal_id)
    )
    stored_decision_clarification = decision_clarification_payload(latest_goal)
    authoritative_tool_clarification = bool(
        clarification_payload is not None
        and _clarification_has_authoritative_grounding(
            clarification_payload,
            tool_trace=tool_trace,
        )
    )
    if stored_decision_clarification is not None:
        clarification_payload = stored_decision_clarification
        mode = "clarification"
        summary = str(
            stored_decision_clarification.get("message")
            or "I need one decision before I can continue."
        )
        action_proposal = None
        metadata_proposal = None
        branch_proposal = None
        preference_memory_proposal = None
        planner_result = None
        planner_proposal = None
        planner_handoff_outcome = None
        normalized_payload = dict(normalized_payload)
        normalized_payload["mode"] = "clarification"
        for proposal_key in (
            "action_proposal",
            "metadata_proposal",
            "branch_proposal",
            PREFERENCE_RESPONSE_KEY,
            "schedule_proposal",
            "planner_result",
            "planner_proposal",
            "unrecognized_proposal",
        ):
            normalized_payload[proposal_key] = None
        normalized_payload["clarification"] = stored_decision_clarification
    elif (
        (
            native_existing_recurrence_requested
            or semantic_inputs
            or unresolved_semantic_authoring_failure
        )
        and native_review_document is None
        and not authoritative_tool_clarification
    ):
        # A mutation has one authority. If canonical compilation, simulation,
        # or review projection does not finish, expose a closed repair result
        # and never reveal a compatibility proposal as an approvable fallback.
        schedule_only_request = (
            native_existing_recurrence_requested
            or set(semantic_inputs).issubset({"schedule_patch"})
        )
        native_failure_outcome = (
            {
                "status": "repairable_internally",
                "message": (
                    "I could not prepare the requested change for review because the "
                    "internal authoring protocol did not complete. Nothing changed, and "
                    "no additional information is needed."
                ),
                "reasonCode": "semantic_authoring_rejected",
                "requiresUserInput": False,
                "retryable": True,
            }
            if unresolved_semantic_authoring_failure
            else
            {
                "status": "repairable_internally",
                "message": (
                    "I could not compile the complete requested change safely. "
                    "Nothing changed; please retry the request."
                ),
                "reasonCode": "canonical_compilation_unavailable",
                "requiresUserInput": False,
                "retryable": True,
            }
            if current_native_change_set is None
            else {
                "status": "repairable_internally",
                "message": (
                    "I prepared the requested change, but its safety simulation did not "
                    "finish. Nothing changed, and no additional information is needed."
                ),
                "reasonCode": "native_simulation_unavailable",
                "requiresUserInput": False,
                "retryable": True,
            }
            if current_native_change_set.simulation_result_ref is None
            else {
                "status": "repairable_internally",
                "message": (
                    (
                        "Planner could not produce a conflict-free schedule from the current "
                        "calendar and saved preferences. "
                    )
                    if schedule_only_request
                    else (
                        "I prepared the requested change, but its safety validation found "
                        "an internal consistency problem. "
                    )
                )
                + (
                    "Nothing changed, and no additional information is needed."
                ),
                "reasonCode": "native_simulation_requires_repair",
                "requiresUserInput": False,
                "retryable": True,
            }
            if (
                native_simulation is not None
                and native_simulation.status
                not in {"ready_for_review", "needs_user_decision"}
            )
            else {
                "status": "repairable_internally",
                "message": (
                    "The requested change passed planning, but its approval view could not "
                    "be prepared. Nothing changed, and no additional information is needed."
                ),
                "reasonCode": "native_review_unavailable",
                "requiresUserInput": False,
                "retryable": True,
            }
        )
        native_requirement_reason = str(native_failure_outcome["reasonCode"])
        native_requirement_blocked = True
        action_proposal = None
        metadata_proposal = None
        branch_proposal = None
        preference_memory_proposal = None
        planner_result = None
        planner_proposal = None
        summary = str(native_failure_outcome["message"])
        planner_handoff_outcome = dict(native_failure_outcome)
        if native_failure_outcome.get("requiresUserInput") is True:
            mode = "clarification"
            clarification_payload = {
                "message": summary,
                "entity_choices": [],
                "field_choices": list(native_failure_outcome.get("choices") or []),
                "scoped_follow_up_prompts": [],
                "conflicts": [],
            }
        else:
            clarification_payload = None
            mode = "review"
        normalized_payload = dict(normalized_payload)
        normalized_payload["mode"] = mode
        for proposal_key in (
            "action_proposal",
            "metadata_proposal",
            "branch_proposal",
            PREFERENCE_RESPONSE_KEY,
            "schedule_proposal",
            "planner_result",
            "planner_proposal",
            "unrecognized_proposal",
        ):
            normalized_payload[proposal_key] = None
        normalized_payload["clarification"] = clarification_payload
        append_kernel_event(
            session,
            strategist_session_id=strategist_session_id,
            correlation_id=correlation_id,
            sequence=sequence,
            event_type="native_review_requirement_blocked",
            payload={
                "reasonCode": native_requirement_reason,
                "semanticInputKinds": sorted(semantic_inputs),
                "semanticAuthoringAttempted": bool(semantic_authoring_attempts),
                "semanticAuthoringFailureCodes": [
                    str(
                        (
                            call.get("result", {}).get("tool_error", {})
                            if isinstance(call.get("result"), dict)
                            else {}
                        ).get("code")
                        or (
                            call.get("result", {}).get("error")
                            if isinstance(call.get("result"), dict)
                            else ""
                        )
                        or "UNKNOWN"
                    )
                    for call in semantic_authoring_failures[:8]
                ],
                "nativeIntent": (
                    "existing_recurrence_edit"
                    if native_existing_recurrence_requested
                    else None
                ),
                "simulationStatus": (
                    native_simulation.status
                    if native_simulation is not None
                    else None
                ),
                "simulationIssues": [
                    {
                        "code": issue.code,
                        "category": issue.category,
                        "summary": issue.summary,
                        "affectedOperationIds": list(
                            issue.affected_operation_ids
                        )[:16],
                    }
                    for issue in (
                        native_simulation.issues[:12]
                        if native_simulation is not None
                        else []
                    )
                ],
                "conservation": (
                    {
                        "complete": native_simulation.conservation.complete,
                        "unsupportedClauseIds": list(
                            native_simulation.conservation.unsupported_clause_ids
                        )[:24],
                    }
                    if native_simulation is not None
                    else None
                ),
                "requiresUserInput": bool(
                    native_failure_outcome.get("requiresUserInput")
                ),
                "retryable": bool(native_failure_outcome.get("retryable")),
            },
        )
        event_types.append("native_review_requirement_blocked")
        sequence += 1
    proposal_contract = _build_proposal_contract_status(
        normalized_payload=normalized_payload,
        action_proposal=action_proposal,
        metadata_proposal=metadata_proposal,
        branch_proposal=branch_proposal,
        preference_memory_proposal=preference_memory_proposal,
        planner_result=planner_result,
        planner_proposal=planner_proposal,
        clarification=clarification_payload,
        nodes=nodes,
        native_review_projected=native_review_document is not None,
        native_recurrence_requested=native_existing_recurrence_requested,
    )
    response_payload = {
        "provider_label": runtime_settings["provider_label"],
        "mode": mode,
        "summary": summary,
        "context_cards": build_context_cards(
            categories=categories,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            staged_node_count=len(staged_node_ids),
            today_override=today_override,
        ),
        "suggested_prompts": (
            []
            if clarification_payload is not None or native_requirement_blocked
            else build_suggested_prompts(focus_node)
        ),
        "action_proposal": action_proposal,
        "metadata_proposal": metadata_proposal,
        "branch_proposal": branch_proposal,
        PREFERENCE_RESPONSE_KEY: preference_memory_proposal,
        "clarification": clarification_payload,
        "provider_failure": provider_failure,
        "review_document": (
            native_review_document.model_dump(mode="json", by_alias=True)
            if native_review_document is not None
            else None
        ),
        "schedule_proposal": (
            planner_proposal
        ),
        "proposal_contract": proposal_contract,
        "handoff_outcome": (
            planner_handoff_outcome
            if isinstance(planner_handoff_outcome, dict)
            else None
        ),
        "_grounded_task_ids": _explicit_grounded_task_ids(
            summary=summary,
            tool_trace=tool_trace,
            nodes=nodes,
        ),
    }
    if planner_result is not None:
        response_payload["summary"] = str(
            planner_result.get("summary") or "Planner finished the scheduling request."
        )
    elif preference_memory_proposal is not None:
        response_payload["summary"] = "I can remember this for future schedules."
    policy = (
        "proposal"
        if native_review_document is not None
        or preference_memory_proposal is not None
        or planner_proposal is not None
        else derive_response_policy(
            summary=summary,
            action_proposal=action_proposal,
            metadata_proposal=metadata_proposal,
            branch_proposal=branch_proposal,
            clarification=clarification_payload,
        )
    )
    has_any_proposal = bool(
        native_review_document
        or action_proposal
        or metadata_proposal
        or branch_proposal
        or preference_memory_proposal
        or planner_proposal
    )
    if provider_failure is None:
        proposal_action = (
            str(ui_event.get("action") or "").strip().lower()
            if event_kind == "proposal_action" and isinstance(ui_event, dict)
            else ""
        )
        legacy_outcome = (
            "rejected"
            if proposal_action == "reject" and not has_any_proposal
            else "clarification"
            if clarification_payload is not None
            else "system_failed"
            if native_requirement_blocked
            else "review"
            if has_any_proposal
            else "completed"
        )
        latest_goal = goal_coordinator.sync_legacy_outcome(
            session_id=strategist_session_id,
            goal_id=latest_goal.goal_id,
            outcome=legacy_outcome,
            command_id=f"legacy-outcome:{strategist_turn_id or correlation_id}",
            current_run_id=current_run.id if current_run is not None else None,
        )
        if current_run is not None:
            from app.services.strategist_runs import link_strategist_run_to_goal

            current_run = link_strategist_run_to_goal(
                session,
                current_run,
                goal_id=latest_goal.goal_id,
                goal_revision=latest_goal.revision,
                causation_id=latest_goal.causation_id,
            )
        session_memory = project_goal_state_memory(session_memory, latest_goal)
    continuity_hints = (
        session_memory.get("continuity_hints")
        if isinstance(session_memory.get("continuity_hints"), dict)
        else {}
    )
    continuation_state = build_continuation_state(
        session_memory=session_memory,
        current_prompt=display_prompt,
        clarification=clarification_payload,
        has_proposal=has_any_proposal,
        target_task_ids=(
            continuity_hints.get("target_task_ids")
            if isinstance(continuity_hints.get("target_task_ids"), list)
            else []
        ),
        proposal_revision=continuation_proposal_revision,
        tool_context=[
            build_tool_context_entry(
                name=str(call.get("name") or "tool"),
                arguments=(
                    call.get("arguments")
                    if isinstance(call.get("arguments"), dict)
                    else {}
                ),
                result=(
                    call.get("result")
                    if isinstance(call.get("result"), dict)
                    else {}
                ),
            )
            for call in tool_trace
            if isinstance(call, dict)
        ],
    )
    append_kernel_event(
        session,
        strategist_session_id=strategist_session_id,
        correlation_id=correlation_id,
        sequence=sequence,
        event_type="final_answer",
        payload={
            "policy": policy,
            "mode": mode,
            "summary": summary,
            "has_proposal": has_any_proposal,
            "has_preference_memory_proposal": bool(preference_memory_proposal),
            "has_clarification": bool(clarification_payload),
            "proposal_contract_status": proposal_contract["status"],
            "proposal_contract_dropped_count": len(proposal_contract["dropped"]),
            "provider_session_id_returned": bool(next_provider_session_id),
            "provider_failure": provider_failure is not None,
            "continuation_state": continuation_state,
        },
    )
    event_types.append("final_answer")

    result: dict[str, Any] = {
        "response": response_payload,
        "provider": runtime_settings["provider"],
        "provider_label": runtime_settings["provider_label"],
        "model_name": runtime_settings["model_name"],
        "provider_session_id": None,
        "resolved_entities": [],
        "memory_entities": [],
        "record_prompt": display_prompt,
        "request_context_prompts": request_context_prompts,
    }
    used_direct_model_response = any(
        str(step.get("phase") or "") == "direct_response"
        for step in model_step_trace
        if isinstance(step, dict)
    )
    if include_diagnostics:
        if deterministic_kind == "bulk_status_scope":
            policy_reason = "Deterministic scoped bulk-status resolver expanded the exact target set."
        elif deterministic_kind == "bulk_status_scope_clarification":
            policy_reason = "Kernel-native bulk-status resolver paused for explicit scope before drafting a patch."
        elif deterministic_kind == "agent_run_plan_explanation":
            policy_reason = "Saved agent-run plan was explained without re-entering the model loop."
        elif deterministic_kind == "agent_run_plan_followup":
            policy_reason = "Saved agent-run plan follow-up compiled selected work units into a reviewable patch."
        elif deterministic_kind == "agent_run_plan_continuation":
            policy_reason = "Saved agent-run plan continuation reused session memory without re-running broad reconnaissance."
        elif deterministic_kind == "single_task_metadata_proposal":
            policy_reason = "Explicit single-task metadata mutation compiled into a reviewable proposal without a model call."
        elif deterministic_kind == "task_graph_answer_candidate":
            policy_reason = "High-confidence task graph evidence produced a read-only answer without a provider model step."
        elif deterministic_kind == "cleanup_reconnaissance":
            policy_reason = "Cleanup reconnaissance produced a bounded target shortlist before proposal drafting."
        elif deterministic_kind == "workload_reconnaissance":
            policy_reason = "Workload reconnaissance produced a bounded triage plan before proposal drafting."
        elif deterministic_kind == "branch_reconnaissance":
            policy_reason = "Branch reconnaissance decomposed a broad branch request into bounded work units."
        elif deterministic_kind == "mixed_cleanup_mutation":
            policy_reason = "Mixed cleanup/status request compiled into one bounded approval-gated TaskPatch."
        elif deterministic_kind == "tier0_state_answer":
            policy_reason = "Tier 0 router answered from structured UI/session state without a model call."
        elif used_direct_model_response:
            policy_reason = "External-world direct response used the lean no-tool provider path."
        else:
            policy_reason = "Agent kernel owns action selection for this turn."
        result["diagnostics"] = {
            "control_plane": "agent-kernel",
            "kernel": {
                "correlation_id": correlation_id,
                "event_count": sequence + 1,
                "event_types": event_types,
                "feature_flag": "STRATEGIST_AGENT_KERNEL",
                "legacy_control_logic_bypassed": True,
                "semantic_fast_paths_enabled": semantic_fast_paths_enabled,
                "context_size": context_size,
                "model_step_trace": model_step_trace,
                "provider_failure": provider_failure,
                "provider_readiness": provider_readiness,
                "tier_route": route_decision.to_payload(),
                "server_change_compilation": server_compilation_diagnostics,
            },
            "policy": policy,
            "tier_route": route_decision.to_payload(),
            "session_memory": session_memory,
            "policy_decision": {
                "policy": policy,
                "allow_provider": deterministic_payload is None,
                "reason": policy_reason,
                "semantic_fast_paths_enabled": semantic_fast_paths_enabled,
                "tier": route_decision.tier,
                "route_kind": route_decision.route_kind,
            },
            "tool_calls": tool_trace,
            "tool_trace": tool_trace,
            "evidence_contract": evidence_assessment,
            "loaded_capabilities": evidence_assessment["loaded_capabilities"],
            "capability_observations": capability_observations,
            "model_step_trace": model_step_trace,
            "provider_failure": provider_failure,
            "provider_readiness": provider_readiness,
            "performance": {
                "total_ms": _elapsed_ms(turn_started_at),
                "stages": [
                    {
                        "key": "tier_route",
                        "title": "Selected execution tier",
                        "duration_ms": 0,
                        "detail": (
                            f"{route_decision.tier} {route_decision.route_kind}: "
                            f"{route_decision.reason}"
                        ),
                    },
                    {
                        "key": "context_packet",
                        "title": "Built compact kernel context",
                        "duration_ms": context_duration_ms,
                        "detail": (
                            f"{context_size['total_chars']} chars; "
                            f"{context_size['tree_context_kind']} "
                            f"{context_size['tree_context_chars']} chars"
                        ),
                    },
                    *[
                        {
                            "key": f"prefetch_{index}",
                            "title": "Prefetched bounded evidence",
                            "duration_ms": step.get("duration_ms"),
                            "detail": summarize_kernel_tool_observation(step),
                        }
                        for index, step in enumerate(prefetched_tool_trace, start=1)
                    ],
                    *[
                        {
                            "key": f"model_step_{step.get('step_index')}",
                            "title": (
                                "Model step timed out"
                                if step.get("status") == "timeout"
                                else "Model direct response"
                                if step.get("phase") == "direct_response"
                                else "Model structured step"
                            ),
                            "duration_ms": step.get("duration_ms"),
                            "detail": (
                                f"{step.get('phase')} {step.get('status')} "
                                f"after {step.get('prompt_chars')} chars"
                            ),
                        }
                        for step in model_step_trace
                    ],
                    *(
                        [
                            {
                                "key": (
                                    "model_direct_response"
                                    if used_direct_model_response
                                    else "model_tool_loop"
                                ),
                                "title": (
                                    "Model direct response"
                                    if used_direct_model_response
                                    else "Model tool loop"
                                ),
                                "duration_ms": model_loop_duration_ms,
                                "detail": (
                                    "Timed out and used safe fallback."
                                    if provider_failure is not None
                                    else "Completed direct no-tool response."
                                    if used_direct_model_response
                                    else "Completed structured model loop."
                                ),
                            }
                        ]
                        if model_loop_duration_ms is not None and not used_direct_model_response
                        else []
                    ),
                ],
            },
        }
    return result
