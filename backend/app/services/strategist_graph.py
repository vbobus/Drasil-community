"""
Strategist LangGraph runtime adapter.

This keeps the existing Strategist response contract intact while moving
conversation state, proposal state, and hard-ID UI events behind a persisted
graph/checkpoint seam.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlmodel import Session, select
from typing_extensions import TypedDict

from app.api.nodes import (
    StrategyNodeCreate,
    StrategyNodeUpdate,
    apply_backlog_to_subtree,
    create_node,
    delete_node,
    encode_image_attachments,
    encode_tags,
    is_descendant,
    list_siblings,
    resequence_nodes,
    update_node,
)
from app.models.category import Category
from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode
from app.models.strategist_chat import StrategistChatTurn
from app.models.strategist_graph import StrategistGraphThread, StrategistProposalRecord
from app.models.recurrence_series import RecurrenceSeries
from app.services.intent_router import (
    choose_mode_from_candidates,
    detect_requested_fields,
    is_open_conversation_prompt,
    score_intent_candidates,
)
from app.services.retrieval import decode_tags, resolve_entities
from app.services.agent_kernel.provider_transport import get_provider_runtime_settings
from app.services.strategist_presentation import (
    build_context_cards,
    build_pending_proposal_response,
    build_suggested_prompts,
)
from app.services.agent_kernel import (
    append_kernel_event,
    run_agent_kernel_turn,
    strategist_agent_kernel_enabled,
)
from app.services.agent_kernel.task_patch import (
    build_task_patch_draft,
    simulate_task_patch,
    validate_task_patch,
)
from app.services.agent_kernel.task_patch_runtime import apply_task_patch
from app.services.change_sets import (
    change_set_resume_context,
    mark_change_set_resumed,
    persist_change_set,
    update_change_set_status,
)
from app.services.calendar_conflict_policy import prompt_has_explicit_clock_time
from app.services.handoff_outcomes import (
    clarification_for_handoff_outcome,
    classify_handoff_failures,
)
from app.services.preference_patches import (
    PREFERENCE_PROPOSAL_KIND,
    PREFERENCE_RESPONSE_KEY,
    apply_preference_patch,
    simulate_preference_patch,
    validate_preference_patch,
)
from app.services.recurring_schedules import (
    build_recurring_schedule_plan,
    create_recurrence_series_from_plan,
    preference_override_authorized_from_prompt,
    schedule_patch_from_recurring_plan,
)
from app.services.planner_capability import build_created_task_schedule_plan
from app.services.schedule_patches import (
    apply_approved_schedule_patch,
    build_schedule_patch,
    simulate_schedule_patch,
    validate_schedule_patch,
)
from app.services.strategist_sessions import (
    get_session_turns,
    get_session_working_set,
    parse_turn_response_payload,
)
from app.services.strategist_proposals import (
    build_bulk_metadata_patch_proposal,
    build_metadata_patch_proposal,
)
from app.services.task_lifecycle import allocate_strategy_node_id, delete_task_dependents

GRAPH_VERSION = "5.2"


def _checkpoint_db_path() -> Path:
    from app.services.account_auth import account_private_path, active_account

    if active_account(required=False) is not None:
        return account_private_path("strategist_langgraph_checkpoints.db")
    configured = os.getenv("STRATEGIST_CHECKPOINT_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    return account_private_path("strategist_langgraph_checkpoints.db")


PROPOSAL_KIND_BY_RESPONSE_KEY = {
    "action_proposal": "action",
    "metadata_proposal": "metadata",
    "branch_proposal": "branch",
}

STANDARD_PROPOSAL_LEGACY_KEYS = {
    "action_proposal": "actionProposal",
    "metadata_proposal": "metadataProposal",
    "branch_proposal": "branchProposal",
    PREFERENCE_RESPONSE_KEY: "preferenceMemoryProposal",
}
PROPOSAL_REVIEW_APPROVE_RE = re.compile(r"\b(approve|approved|confirm|apply|yes)\b", re.IGNORECASE)
PROPOSAL_REVIEW_REJECT_RE = re.compile(
    r"\b(reject|dismiss|cancel|skip|no thanks|don't apply|do not apply)\b",
    re.IGNORECASE,
)
PROPOSAL_EDIT_RE = re.compile(r"\b(edit|revise|adjust|change)\b", re.IGNORECASE)
REVERT_PREVIOUS_ACTION_RE = re.compile(
    r"\b(undo|revert|reverse|rollback|roll\s+back)\b.*\b(previous|prior|last|recent|that|it|action|change|proposal)\b"
    r"|\b(previous|prior|last|recent)\b.*\b(action|change|proposal)\b.*\b(undo|revert|reverse|rollback|roll\s+back)\b",
    re.IGNORECASE,
)
METADATA_DIFF_FIELD_TERMS = {
    "title": ("title",),
    "notes": ("notes",),
    "status": ("status",),
    "priority": ("priority",),
    "deadline_at": ("deadline",),
    "time_estimate_minutes": ("estimate",),
    "tags": ("tags",),
    "is_pinned": ("pinned",),
    "node_type": ("task type", "type"),
}
VALID_STATUS_VALUES = {"todo", "in-progress", "done"}
VALID_PRIORITY_VALUES = {"low", "medium", "high"}
_UNSUPPORTED_REVERSE_VALUE = object()

_RECURRENCE_CONTEXT_RE = re.compile(
    r"\b(?:daily|each|every|ongoing|recurr(?:ing|ence)?|repeat(?:ing|s)?|weekly|weekdays?)\b",
    re.IGNORECASE,
)
_TASK_CONTEXT_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_TASK_CONTEXT_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "my",
    "of",
    "on",
    "task",
    "the",
    "to",
    "with",
}


@dataclass
class ProblemFrame:
    commitment_level: Literal[
        "chat_only",
        "draft",
        "proposal_required",
        "approved_for_execution",
    ]
    subject_kind: Literal[
        "none",
        "concept",
        "existing_entity",
        "entity_set",
        "hypothetical_entity",
        "external_reference",
    ]
    requires_exact_target: bool
    reason: str
    source: Literal["prompt", "ui_event"]


@dataclass
class FocusState:
    active_category_id: Optional[int]
    requested_focus_node_id: Optional[int]
    selected_node_id: Optional[int]
    staged_node_ids: list[int] = field(default_factory=list)


@dataclass
class UiContext:
    session_id: str
    thread_id: str
    prompt: str
    display_prompt: str
    event_kind: Optional[str] = None
    source_turn_id: Optional[str] = None


@dataclass
class ProposalState:
    pending_proposal_id: Optional[str]
    pending_proposal_kind: Optional[str]
    target_node_ids: list[int] = field(default_factory=list)
    source_turn_id: Optional[str] = None
    status: str = "none"


class StrategistGraphState(TypedDict, total=False):
    messages: list[dict[str, str]]
    ui_context: dict[str, Any]
    focus_state: dict[str, Any]
    problem_frame: dict[str, Any]
    proposal_state: dict[str, Any]
    latest_response: dict[str, Any]
    diagnostics: dict[str, Any]
    normalized_input: dict[str, Any]
    source_turn_context: dict[str, Any]
    result: dict[str, Any]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


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


def _normalize_prompt(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def detect_proposal_review_action(prompt: str) -> str | None:
    normalized = _normalize_prompt(prompt)
    if not normalized:
        return None
    if PROPOSAL_REVIEW_APPROVE_RE.search(normalized):
        return "approve"
    if PROPOSAL_REVIEW_REJECT_RE.search(normalized):
        return "reject"
    if PROPOSAL_EDIT_RE.search(normalized):
        return "edit"
    return None


def detect_previous_action_revert(prompt: str) -> bool:
    normalized = _normalize_prompt(prompt)
    if not normalized:
        return False
    if normalized.lower() in {"undo", "revert", "rollback", "roll back"}:
        return True
    return bool(REVERT_PREVIOUS_ACTION_RE.search(normalized))


def build_display_prompt(prompt: str, ui_event: dict[str, Any] | None) -> str:
    normalized = _normalize_prompt(prompt)
    if normalized:
        return normalized
    if not isinstance(ui_event, dict):
        return ""
    kind = str(ui_event.get("kind") or "").strip().lower()
    action = str(ui_event.get("action") or "").strip().lower()
    payload = ui_event.get("payload") if isinstance(ui_event.get("payload"), dict) else {}
    choice_label = payload.get("choiceLabel")
    proposal_label = payload.get("proposalLabel")
    if isinstance(choice_label, str) and choice_label.strip():
        return choice_label.strip()
    if isinstance(proposal_label, str) and proposal_label.strip():
        return proposal_label.strip()
    if kind == "proposal_action":
        if action == "approve":
            return "Approve proposal"
        if action == "edit":
            return "Edit proposal"
        if action == "reject":
            return "Reject proposal"
    if kind == "clarification_choice":
        return "Selected clarification choice"
    if kind == "focus_change":
        return "Changed strategist focus"
    return ""


def _tool_result_count(tool_call: dict[str, Any]) -> int | None:
    result = tool_call.get("result")
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("matches", "tasks", "items", "working_set"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def _summarize_tool_call(tool_call: dict[str, Any]) -> str:
    name = str(tool_call.get("name") or "tool").strip()
    args = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    count = _tool_result_count(tool_call)
    if name in {"search_entities", "search_notes"}:
        query = str(args.get("query") or "").strip()
        suffix = f" and found {count} candidate{'s' if count != 1 else ''}" if count is not None else ""
        return f"Searched planning data for {query!r}{suffix}."
    if name == "get_workload_overview":
        scope = str(args.get("category_name") or "all categories").strip()
        days = args.get("days")
        window = f" over {days} days" if isinstance(days, int) else ""
        return f"Read workload overview for {scope}{window}."
    if name == "list_tasks_by_filter":
        filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
        suffix = f" and returned {count} task{'s' if count != 1 else ''}" if count is not None else ""
        return f"Listed tasks matching {', '.join(filters.keys()) or 'the requested filters'}{suffix}."
    if name.startswith("get_task_") or name in {"get_subtree", "get_children", "get_parent_chain"}:
        return "Read the relevant task details and relationships."
    if name == "list_calendar_blocks":
        return f"Checked {count or 0} calendar block{'s' if count != 1 else ''} in the requested range."
    if name == "handoff_to_planner":
        return "Passed the selected tasks, time window, and explicit constraints to the scheduler."
    if name in {"propose_tree_operation", "propose_task_operation"}:
        return "Prepared the requested task changes for validation and review."
    if "preference" in name:
        return "Reviewed the relevant saved scheduling preferences."
    return "Used a workspace tool to continue the request."


def _public_tool_title(name: str) -> str:
    normalized = name.strip().lower()
    if normalized == "list_calendar_blocks":
        return "Checked the calendar"
    if normalized == "handoff_to_planner":
        return "Sent constraints to the scheduler"
    if normalized.startswith("search_") or normalized in {
        "query_task_graph",
        "list_tasks_by_filter",
        "get_task_snapshot",
        "get_task_branch",
        "get_subtree",
    }:
        return "Found relevant tasks"
    if normalized in {"propose_tree_operation", "propose_task_operation"}:
        return "Prepared task changes"
    if "preference" in normalized:
        return "Checked scheduling preferences"
    if normalized == "load_skill":
        return "Loaded the planning workflow"
    return "Used workspace tools"


def _coerce_duration_ms(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return round(float(value), 1)
    return None


def _format_duration_ms(duration_ms: float) -> str:
    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.1f}s"
    return f"{duration_ms:.0f}ms"


def _performance_stage_lookup(diagnostics: dict[str, Any]) -> dict[str, float]:
    performance = diagnostics.get("performance")
    stages = performance.get("stages") if isinstance(performance, dict) else []
    lookup: dict[str, float] = {}
    if not isinstance(stages, list):
        return lookup
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        key = str(stage.get("key") or "").strip()
        duration_ms = _coerce_duration_ms(stage.get("duration_ms"))
        if not key or duration_ms is None:
            continue
        lookup[key] = max(lookup.get(key, 0.0), duration_ms)
    return lookup


def _lookup_duration(stage_lookup: dict[str, float], *keys: str) -> float | None:
    for key in keys:
        if key in stage_lookup:
            return stage_lookup[key]
    return None


def _summarize_performance(diagnostics: dict[str, Any]) -> tuple[str, float | None] | None:
    performance = diagnostics.get("performance")
    if not isinstance(performance, dict):
        return None
    stages = performance.get("stages")
    if not isinstance(stages, list):
        return None
    measured: list[tuple[str, str, float]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        key = str(stage.get("key") or "").strip()
        if key.endswith("_total"):
            continue
        title = str(stage.get("title") or key.replace("_", " ")).strip()
        duration_ms = _coerce_duration_ms(stage.get("duration_ms"))
        if not title or duration_ms is None:
            continue
        measured.append((key, title, duration_ms))
    if not measured:
        return None
    total_ms = _coerce_duration_ms(performance.get("total_ms"))
    slowest = sorted(measured, key=lambda item: item[2], reverse=True)[:3]
    slowest_text = "; ".join(
        f"{title} {_format_duration_ms(duration_ms)}"
        for _, title, duration_ms in slowest
    )
    total_text = (
        f"Total {_format_duration_ms(total_ms)}. " if total_ms is not None else ""
    )
    return f"{total_text}Slowest stages: {slowest_text}.", total_ms


def build_public_reasoning_trace(diagnostics: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    kernel_diagnostics = diagnostics.get("kernel")
    review_blocked = isinstance(kernel_diagnostics, dict) and "native_review_requirement_blocked" in kernel_diagnostics.get("event_types", [])
    stage_lookup = _performance_stage_lookup(diagnostics)
    allowed_step_kinds = {"frame", "context", "plan", "tool", "decision"}

    def append_step(
        *,
        kind: str,
        title: str,
        detail: str,
        duration_ms: float | None = None,
        tool_name: str | None = None,
    ) -> None:
        normalized_kind = kind if kind in allowed_step_kinds else "decision"
        step: dict[str, Any] = {
            "kind": normalized_kind,
            "title": title,
            "detail": detail,
        }
        if duration_ms is not None:
            step["duration_ms"] = duration_ms
        if normalized_kind == "tool" and tool_name:
            step["tool_name"] = tool_name
        steps.append(step)

    problem_frame = diagnostics.get("problem_frame") if isinstance(diagnostics.get("problem_frame"), dict) else {}
    if problem_frame:
        subject_kind = str(problem_frame.get("subject_kind") or "request").strip().lower()
        frame_details = {
            "entity_set": "Recognized that the request involves multiple related tasks.",
            "existing_entity": "Identified the existing task or branch the request refers to.",
            "hypothetical_entity": "Recognized that the request may create new work in the task tree.",
            "external_reference": "Noted the external reference that may affect the plan.",
            "concept": "Understood this as a planning question before gathering context.",
            "none": "Checked what information was available for the request.",
        }
        append_step(
            kind="frame",
            title="Understood the request",
            detail=frame_details.get(
                subject_kind,
                "Identified the goal and the context needed to continue.",
            ),
        )
    elif isinstance(diagnostics.get("intent_candidates"), list) and diagnostics["intent_candidates"]:
        first_intent = diagnostics["intent_candidates"][0]
        if isinstance(first_intent, dict):
            append_step(
                kind="frame",
                title="Understood the request",
                detail="Identified the goal and the context needed to continue.",
            )

    focus_binding = diagnostics.get("focus_binding") if isinstance(diagnostics.get("focus_binding"), dict) else {}
    if focus_binding:
        input_focus = focus_binding.get("input_focus_node_id")
        bound = bool(focus_binding.get("bound_as_target"))
        working_set_used = bool(focus_binding.get("working_set_used"))
        if input_focus and bound:
            detail = "Used the selected task because the prompt explicitly referred to it."
        elif input_focus:
            detail = "Kept the selected task as background context, not as the answer target."
        else:
            detail = "No selected task was bound as the answer target."
        if working_set_used:
            detail += " Used recent conversation context because this was a follow-up."
        else:
            detail += " Kept the search focused on the current request."
        append_step(
            kind="context",
            title="Checked session context",
            detail=detail,
            duration_ms=_lookup_duration(stage_lookup, "turn_context_pack"),
        )

    semantic_plan = (
        diagnostics.get("semantic_action_plan")
        if isinstance(diagnostics.get("semantic_action_plan"), dict)
        else {}
    )
    semantic_actions = semantic_plan.get("actions") if isinstance(semantic_plan, dict) else []
    if isinstance(semantic_actions, list) and semantic_actions:
        action_count = len([action for action in semantic_actions if isinstance(action, dict)])
        proposal_note = (
            " The changes will stay reviewable until you approve them."
            if bool(semantic_plan.get("requires_proposal"))
            else " No changes need approval for this step."
        )
        append_step(
            kind="plan",
            title="Planned the work",
            detail=(
                f"Kept {action_count} related action{'s' if action_count != 1 else ''} "
                f"together as one request.{proposal_note}"
            ),
            duration_ms=_lookup_duration(stage_lookup, "semantic_planner"),
        )

    retrieval_plan = diagnostics.get("retrieval_plan") if isinstance(diagnostics.get("retrieval_plan"), dict) else {}
    if retrieval_plan:
        append_step(
            kind="plan",
            title="Identified the needed context",
            detail="Determined which task and calendar details were needed before continuing.",
            duration_ms=_lookup_duration(
                stage_lookup,
                "semantic_planner",
                "read_only_preflight",
                "planner_context",
            ),
        )

    tool_count = 0
    tool_trace = diagnostics.get("tool_trace", diagnostics.get("tool_calls", []))
    if isinstance(tool_trace, list):
        tool_count = sum(1 for tool_call in tool_trace if isinstance(tool_call, dict))
        for tool_call in tool_trace[:8]:
            if not isinstance(tool_call, dict):
                continue
            append_step(
                kind="tool",
                title=_public_tool_title(str(tool_call.get("name") or "")),
                detail=_summarize_tool_call(tool_call),
                duration_ms=_coerce_duration_ms(tool_call.get("duration_ms")),
                tool_name=str(tool_call.get("name") or "").strip()[:96] or None,
            )
        if tool_count > 8:
            append_step(
                kind="decision",
                title="Additional tool activity",
                detail=f"{tool_count - 8} additional tool calls are omitted from this compact trace.",
            )

    policy = diagnostics.get("policy_decision") if isinstance(diagnostics.get("policy_decision"), dict) else {}
    if policy:
        policy_name = str(policy.get("policy") or "answer_direct").replace("_", " ")
        if "proposal" in policy_name:
            policy_detail = "Prepared a reviewable proposal without applying changes automatically."
        elif "clarif" in policy_name:
            policy_detail = "Paused only for information needed to continue safely."
        else:
            policy_detail = "Prepared the final response from the gathered context."
        append_step(
            kind="decision",
            title="Prepared the response",
            detail=policy_detail,
            duration_ms=_lookup_duration(stage_lookup, "contract_validation"),
        )

    performance_summary = _summarize_performance(diagnostics)
    if performance_summary is not None:
        _, total_ms = performance_summary
        append_step(
            kind="decision",
            title="Could not prepare the review" if review_blocked else "Completed the request",
            detail=(
                f"Stopped after {_format_duration_ms(total_ms)} without applying changes."
                if review_blocked and total_ms is not None
                else f"Finished in {_format_duration_ms(total_ms)}."
                if total_ms is not None
                else "Finished the requested work."
            ),
            duration_ms=total_ms,
        )

    model_step_trace = (
        diagnostics.get("model_step_trace")
        if isinstance(diagnostics.get("model_step_trace"), list)
        else []
    )
    kernel = (
        diagnostics.get("kernel")
        if isinstance(diagnostics.get("kernel"), dict)
        else {}
    )
    event_types = (
        kernel.get("event_types")
        if isinstance(kernel.get("event_types"), list)
        else []
    )
    successful_goal_updates = [
        item
        for item in tool_trace
        if isinstance(item, dict)
        and str(item.get("name") or "") == "set_goal_transition"
        and isinstance(item.get("result"), dict)
        and not item["result"].get("error")
    ] if isinstance(tool_trace, list) else []
    callbacks_by_decision: dict[str, int] = {}
    if isinstance(tool_trace, list):
        for item in tool_trace:
            if (
                not isinstance(item, dict)
                or str(item.get("name") or "")
                != "respond_to_planner_decision"
            ):
                continue
            arguments = (
                item.get("arguments")
                if isinstance(item.get("arguments"), dict)
                else {}
            )
            decision_id = str(
                arguments.get("decision_id")
                or arguments.get("decisionId")
                or "unknown"
            )
            callbacks_by_decision[decision_id] = (
                callbacks_by_decision.get(decision_id, 0) + 1
            )
    forbidden_authority_events = {
        "native_shadow_sampling",
        "native_changeset_shadow_comparison",
    }
    summary = (
        f"Completed the request using {tool_count} workspace step{'s' if tool_count != 1 else ''}."
        if tool_count
        else "Framed first and answered without task-tool lookup."
    )
    if review_blocked:
        summary = f"Could not prepare the review after {tool_count} workspace step{'s' if tool_count != 1 else ''}. Nothing changed."
    return {
        "summary": summary,
        "steps": steps,
        "metrics": {
            "model_steps": len(
                [item for item in model_step_trace if isinstance(item, dict)]
            ),
            "tool_calls": tool_count,
            "semantic_authoring_passes": event_types.count(
                "server_change_compilation"
            ),
            "repeated_unchanged_goal_updates": max(
                0, len(successful_goal_updates) - 1
            ),
            "max_planner_callbacks_for_one_decision": max(
                callbacks_by_decision.values(),
                default=0,
            ),
            "production_authority_count": (
                2
                if any(
                    event_type in forbidden_authority_events
                    for event_type in event_types
                )
                else 1
            ),
        },
    }


@lru_cache(maxsize=1)
def get_graph_checkpointer() -> SqliteSaver:
    checkpoint_path = _checkpoint_db_path()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    return SqliteSaver(connection)


def ensure_graph_thread(
    session: Session,
    *,
    strategist_session_id: str,
) -> StrategistGraphThread:
    existing = session.exec(
        select(StrategistGraphThread).where(
            StrategistGraphThread.chat_session_id == strategist_session_id
        )
    ).first()
    if existing is not None:
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    thread = StrategistGraphThread(
        chat_session_id=strategist_session_id,
        thread_id=f"strategist-{strategist_session_id}",
        graph_version=GRAPH_VERSION,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def get_pending_session_proposal(
    session: Session,
    strategist_session_id: str,
) -> StrategistProposalRecord | None:
    return session.exec(
        select(StrategistProposalRecord)
        .where(StrategistProposalRecord.session_id == strategist_session_id)
        .where(StrategistProposalRecord.status == "pending")
        .order_by(StrategistProposalRecord.updated_at.desc())
    ).first()


def get_latest_approved_session_proposal(
    session: Session,
    strategist_session_id: str,
) -> StrategistProposalRecord | None:
    return session.exec(
        select(StrategistProposalRecord)
        .where(StrategistProposalRecord.session_id == strategist_session_id)
        .where(StrategistProposalRecord.status == "approved")
        .order_by(StrategistProposalRecord.updated_at.desc())
    ).first()


def get_proposal_record(
    session: Session,
    proposal_id: str | None,
) -> StrategistProposalRecord | None:
    if not proposal_id:
        return None
    return session.get(StrategistProposalRecord, proposal_id)


def serialize_proposal_record(record: StrategistProposalRecord | None) -> dict[str, Any]:
    if record is None:
        return {}
    payload = _json_loads_dict(record.payload_json)
    target_node_ids = _json_loads_list(record.target_node_ids_json)
    return {
        "id": record.id,
        "kind": record.proposal_kind,
        "status": record.status,
        "source_turn_id": record.source_turn_id,
        "target_node_id": record.target_node_id,
        "target_node_ids": [int(item) for item in target_node_ids if isinstance(item, int)],
        "payload": payload,
    }


def build_proposal_state_snapshot(
    record: StrategistProposalRecord | None,
) -> ProposalState:
    if record is None:
        return ProposalState(
            pending_proposal_id=None,
            pending_proposal_kind=None,
            status="none",
        )
    return ProposalState(
        pending_proposal_id=record.id,
        pending_proposal_kind=record.proposal_kind,
        target_node_ids=[
            int(item)
            for item in _json_loads_list(record.target_node_ids_json)
            if isinstance(item, int)
        ],
        source_turn_id=record.source_turn_id,
        status=record.status,
    )


def build_problem_frame_for_prompt(
    *,
    session: Session,
    prompt: str,
    active_category_id: int | None,
    focus_node_id: int | None,
    strategist_session_id: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> ProblemFrame:
    normalized_prompt = _normalize_prompt(prompt)
    if not normalized_prompt:
        return ProblemFrame(
            commitment_level="chat_only",
            subject_kind="none",
            requires_exact_target=False,
            reason="No free-form prompt text was supplied.",
            source="prompt",
        )

    if is_open_conversation_prompt(normalized_prompt, conversation_history):
        return ProblemFrame(
            commitment_level="chat_only",
            subject_kind="concept",
            requires_exact_target=False,
            reason="Framed as open conversation before task retrieval.",
            source="prompt",
        )

    categories = list(session.exec(select(Category).order_by(Category.sort_order)).all())
    nodes = list(
        session.exec(
            select(StrategyNode).order_by(StrategyNode.category_id, StrategyNode.sort_order)
        ).all()
    )
    focus_node = session.get(StrategyNode, focus_node_id) if focus_node_id is not None else None
    active_category = (
        session.get(Category, active_category_id) if active_category_id is not None else None
    )
    working_set = get_session_working_set(session, strategist_session_id)
    intent_candidates = score_intent_candidates(prompt=normalized_prompt, focus_node=focus_node)
    top_intent = (
        str(intent_candidates[0]["intent"])
        if intent_candidates and isinstance(intent_candidates[0].get("intent"), str)
        else "unknown"
    )
    mode = choose_mode_from_candidates(
        prompt=normalized_prompt,
        focus_node=focus_node,
        intent_candidates=intent_candidates,
    )
    if top_intent in {"graph_inspection", "metadata_gap_review", "workload_review", "unknown"}:
        frame_label = (
            "unknown targetless review"
            if top_intent == "unknown"
            else f"broad {top_intent.replace('_', ' ')}"
        )
        return ProblemFrame(
            commitment_level="chat_only",
            subject_kind="concept",
            requires_exact_target=False,
            reason=(
                f"Framed as {frame_label} before entity retrieval; "
                "no exact task target is required."
            ),
            source="prompt",
        )
    retrieval_result = resolve_entities(
        prompt=normalized_prompt,
        intent_name=top_intent,
        requested_fields=detect_requested_fields(normalized_prompt),
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )

    resolved_entities = retrieval_result.get("resolved_entities", [])
    resolved_count = (
        len(resolved_entities) if isinstance(resolved_entities, list) else 0
    )
    has_entity_reference = bool(retrieval_result.get("has_entity_reference"))
    lowered = normalized_prompt.lower()

    if "http://" in lowered or "https://" in lowered:
        subject_kind: ProblemFrame.__annotations__["subject_kind"] = "external_reference"
    elif resolved_count > 1:
        subject_kind = "entity_set"
    elif resolved_count == 1:
        subject_kind = "existing_entity"
    elif any(token in lowered for token in ("create ", "add ", "new task", "new subtask")):
        subject_kind = "hypothetical_entity"
    elif has_entity_reference:
        subject_kind = "concept"
    else:
        subject_kind = "concept"

    if mode == "branch":
        commitment_level: ProblemFrame.__annotations__["commitment_level"] = "draft"
    elif mode in {"metadata", "action"}:
        commitment_level = "proposal_required"
    else:
        commitment_level = "chat_only"

    requires_exact_target = mode in {"metadata", "branch", "action"} or subject_kind in {
        "existing_entity",
        "entity_set",
    }
    reason = (
        f"Framed from prompt with top intent {top_intent} and mode {mode}; "
        f"subject kind is {subject_kind}."
    )
    return ProblemFrame(
        commitment_level=commitment_level,
        subject_kind=subject_kind,
        requires_exact_target=requires_exact_target,
        reason=reason,
        source="prompt",
    )


def build_problem_frame_for_ui_event(
    *,
    ui_event: dict[str, Any],
    source_turn_prompt: str,
) -> ProblemFrame:
    kind = str(ui_event.get("kind") or "").strip().lower()
    action = str(ui_event.get("action") or "").strip().lower()
    if kind == "proposal_action":
        if action == "approve":
            commitment_level: ProblemFrame.__annotations__["commitment_level"] = (
                "approved_for_execution"
            )
        elif action == "edit":
            commitment_level = "draft"
        else:
            commitment_level = "proposal_required"
        return ProblemFrame(
            commitment_level=commitment_level,
            subject_kind="existing_entity",
            requires_exact_target=True,
            reason=f"Structured proposal action {action or 'review'} is resuming a pending proposal by ID.",
            source="ui_event",
        )
    if kind == "clarification_choice":
        return ProblemFrame(
            commitment_level="proposal_required",
            subject_kind="existing_entity",
            requires_exact_target=True,
            reason=(
                "Structured clarification choice supplied an exact target node ID "
                f"while resuming '{_normalize_prompt(source_turn_prompt) or 'the prior request'}'."
            ),
            source="ui_event",
        )
    return ProblemFrame(
        commitment_level="chat_only",
        subject_kind="concept",
        requires_exact_target=False,
        reason="Structured UI event did not require a mutation resume path.",
        source="ui_event",
    )


def build_proposal_response_context(
    *,
    session: Session,
    active_category_id: int | None,
    focus_node_id: int | None,
    staged_node_ids: list[int],
    fallback_title: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    categories = list(session.exec(select(Category).order_by(Category.sort_order)).all())
    nodes = list(
        session.exec(
            select(StrategyNode).order_by(StrategyNode.category_id, StrategyNode.sort_order)
        ).all()
    )
    focus_node = session.get(StrategyNode, focus_node_id) if focus_node_id is not None else None
    active_category = (
        session.get(Category, active_category_id) if active_category_id is not None else None
    )
    context_cards = build_context_cards(
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_count=len(staged_node_ids),
    )
    suggested_prompts = build_suggested_prompts(focus_node)
    if focus_node is None and fallback_title:
        suggested_prompts = [
            f"What should I do next in {fallback_title}?",
            f"Break down {fallback_title} into concrete subtasks",
            "Review my workload across categories",
        ]
    return context_cards, suggested_prompts


def get_metadata_target_node_ids(proposal_payload: dict[str, Any]) -> list[int]:
    target_node_ids: list[int] = []
    raw_target_updates = proposal_payload.get("target_updates")
    if isinstance(raw_target_updates, list):
        for item in raw_target_updates:
            if not isinstance(item, dict):
                continue
            node_id = item.get("target_node_id")
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    raw_target_node_ids = proposal_payload.get("target_node_ids")
    if isinstance(raw_target_node_ids, list):
        for node_id in raw_target_node_ids:
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    raw_subtree_root_ids = proposal_payload.get("subtree_root_ids")
    if isinstance(raw_subtree_root_ids, list):
        for node_id in raw_subtree_root_ids:
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    target_node_id = proposal_payload.get("target_node_id")
    if isinstance(target_node_id, int) and target_node_id not in target_node_ids:
        target_node_ids.insert(0, target_node_id)
    return target_node_ids


def get_metadata_updates_for_targets(proposal_payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    target_node_ids = get_metadata_target_node_ids(proposal_payload)
    base_updates = (
        dict(proposal_payload["updates"])
        if isinstance(proposal_payload.get("updates"), dict)
        else {}
    )
    updates_by_node_id = {
        node_id: dict(base_updates)
        for node_id in target_node_ids
    }
    raw_target_updates = proposal_payload.get("target_updates")
    if isinstance(raw_target_updates, list):
        for raw_item in raw_target_updates:
            if not isinstance(raw_item, dict):
                continue
            node_id = raw_item.get("target_node_id")
            item_updates = raw_item.get("updates")
            if not isinstance(node_id, int) or not isinstance(item_updates, dict):
                continue
            merged = updates_by_node_id.setdefault(node_id, dict(base_updates))
            merged.update(item_updates)
    return {
        node_id: updates
        for node_id, updates in updates_by_node_id.items()
        if updates
    }


def read_node_metadata_value(node: StrategyNode, field_name: str) -> Any:
    if field_name == "tags":
        return [
            str(item)
            for item in _json_loads_list(node.tags)
            if isinstance(item, str) and item.strip()
        ]
    if field_name == "image_attachments":
        return _json_loads_list(node.image_attachments)
    return getattr(node, field_name, None)


def capture_metadata_before_state(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    updates_by_node_id = get_metadata_updates_for_targets(proposal_payload)
    before_state: list[dict[str, Any]] = []
    for node_id, updates in updates_by_node_id.items():
        node = session.get(StrategyNode, node_id)
        if node is None:
            continue
        fields = [str(field_name) for field_name in updates.keys()]
        before_state.append(
            {
                "node_id": node.id,
                "title": node.title,
                "before": {
                    field_name: read_node_metadata_value(node, field_name)
                    for field_name in fields
                },
                "after": {
                    field_name: updates.get(field_name)
                    for field_name in fields
                },
            }
        )
    return before_state


def _json_stable_key(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def build_uniform_reverse_updates_from_before_state(
    *,
    before_state: list[dict[str, Any]],
    updates: dict[str, Any],
) -> dict[str, Any] | None:
    if not before_state:
        return None
    reverse_updates: dict[str, Any] = {}
    for field_name in updates.keys():
        values: list[Any] = []
        for item in before_state:
            before = item.get("before") if isinstance(item.get("before"), dict) else {}
            if field_name not in before:
                return None
            values.append(before[field_name])
        if not values:
            return None
        first_key = _json_stable_key(values[0])
        if any(_json_stable_key(value) != first_key for value in values[1:]):
            return None
        reverse_updates[str(field_name)] = values[0]
    return reverse_updates


def build_reverse_target_updates_from_before_state(
    *,
    before_state: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    target_updates: list[dict[str, Any]] = []
    for item in before_state:
        if not isinstance(item, dict):
            return None
        node_id = item.get("node_id")
        before = item.get("before")
        if not isinstance(node_id, int) or not isinstance(before, dict) or not before:
            return None
        target_updates.append(
            {
                "target_node_id": node_id,
                "target_node_title": str(item.get("title") or "Task").strip() or "Task",
                "updates": dict(before),
            }
        )
    return target_updates or None


def _metadata_diff_item_matches_field(item: dict[str, Any], field_name: str) -> bool:
    label = str(item.get("label") or "").strip().lower()
    if not label:
        return False
    return any(term in label for term in METADATA_DIFF_FIELD_TERMS.get(field_name, (field_name,)))


def parse_formatted_metadata_value(field_name: str, raw_value: Any) -> Any:
    if not isinstance(raw_value, str):
        return _UNSUPPORTED_REVERSE_VALUE
    value = raw_value.strip()
    lowered = value.lower()
    if field_name == "status":
        normalized = lowered.replace(" ", "-")
        return normalized if normalized in VALID_STATUS_VALUES else _UNSUPPORTED_REVERSE_VALUE
    if field_name == "priority":
        for priority in VALID_PRIORITY_VALUES:
            if lowered.startswith(priority) or f"{priority} priority" in lowered:
                return priority
        return _UNSUPPORTED_REVERSE_VALUE
    if field_name == "deadline_at":
        if lowered in {"no deadline", "unset", "none"}:
            return None
        try:
            return datetime.strptime(value, "%b %d, %Y").date().isoformat()
        except ValueError:
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                return _UNSUPPORTED_REVERSE_VALUE
    if field_name == "time_estimate_minutes":
        if lowered in {"no estimate", "unset", "none"}:
            return None
        match = re.fullmatch(
            r"(?:(?P<hours>\d+)\s*h)?(?:\s*(?P<minutes>\d+)\s*m)?",
            lowered,
        )
        if not match or not (match.group("hours") or match.group("minutes")):
            return _UNSUPPORTED_REVERSE_VALUE
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        return hours * 60 + minutes
    if field_name == "tags":
        if lowered in {"no tags", "unset", "none"}:
            return []
        return [part.strip() for part in value.split(",") if part.strip()]
    if field_name == "is_pinned":
        if lowered == "pinned":
            return True
        if lowered == "not pinned":
            return False
        return _UNSUPPORTED_REVERSE_VALUE
    if field_name == "node_type":
        if lowered == "habit":
            return "habit"
        if lowered == "task":
            return "task"
        return _UNSUPPORTED_REVERSE_VALUE
    if field_name in {"title", "notes"} and "..." not in value:
        if lowered in {"untitled", "no notes", "unset", "none"}:
            return "" if field_name == "title" else None
        return value
    return _UNSUPPORTED_REVERSE_VALUE


def build_uniform_reverse_updates_from_diff(
    *,
    proposal_payload: dict[str, Any],
    target_node_ids: list[int],
) -> dict[str, Any] | None:
    updates = proposal_payload.get("updates")
    diff_preview = proposal_payload.get("diff_preview")
    if not isinstance(updates, dict) or not isinstance(diff_preview, list):
        return None
    reverse_updates: dict[str, Any] = {}
    for field_name in updates.keys():
        values: list[Any] = []
        for node_id in target_node_ids:
            matching_item = None
            for raw_item in diff_preview:
                if not isinstance(raw_item, dict):
                    continue
                if raw_item.get("target_node_id") != node_id:
                    continue
                if _metadata_diff_item_matches_field(raw_item, str(field_name)):
                    matching_item = raw_item
                    break
            if matching_item is None:
                return None
            parsed_value = parse_formatted_metadata_value(
                str(field_name),
                matching_item.get("before"),
            )
            if parsed_value is _UNSUPPORTED_REVERSE_VALUE:
                return None
            values.append(parsed_value)
        if not values:
            return None
        first_key = _json_stable_key(values[0])
        if any(_json_stable_key(value) != first_key for value in values[1:]):
            return None
        reverse_updates[str(field_name)] = values[0]
    return reverse_updates


def build_reverse_metadata_proposal(
    session: Session,
    *,
    proposal_record: StrategistProposalRecord,
) -> tuple[dict[str, Any] | None, str]:
    if proposal_record.proposal_kind != "metadata":
        return None, "Only metadata proposals can be safely reversed right now."
    proposal_payload = _json_loads_dict(proposal_record.payload_json)
    updates_by_node_id = get_metadata_updates_for_targets(proposal_payload)
    if not updates_by_node_id:
        return None, "The previous metadata proposal did not include a reversible update payload."

    target_node_ids = get_metadata_target_node_ids(proposal_payload)
    if not target_node_ids:
        return None, "The previous metadata proposal did not include target task IDs."

    result_payload = _json_loads_dict(proposal_record.result_json)
    before_state = result_payload.get("metadata_before_state")
    reverse_updates = (
        build_uniform_reverse_updates_from_before_state(
            before_state=before_state,
            updates={
                field_name: True
                for updates in updates_by_node_id.values()
                for field_name in updates.keys()
            },
        )
        if isinstance(before_state, list)
        else None
    )
    reverse_target_updates = (
        build_reverse_target_updates_from_before_state(before_state=before_state)
        if isinstance(before_state, list)
        else None
    )
    if reverse_updates is None:
        reverse_updates = build_uniform_reverse_updates_from_diff(
            proposal_payload=proposal_payload,
            target_node_ids=target_node_ids,
        )
    if reverse_updates is None and reverse_target_updates is None:
        return (
            None,
            "The previous metadata change touched values that cannot be reversed safely as one uniform patch.",
        )

    target_nodes: list[StrategyNode] = []
    for node_id in target_node_ids:
        node = session.get(StrategyNode, node_id)
        if node is None:
            return None, f"Task {node_id} from the previous proposal no longer exists."
        target_nodes.append(node)
    rationale = (
        f"Reverses approved proposal {proposal_record.id} by restoring the metadata values "
        "that were present before it was applied."
    )
    if len(target_nodes) == 1:
        single_reverse_updates = (
            reverse_updates
            or (reverse_target_updates[0]["updates"] if reverse_target_updates else {})
        )
        proposal = build_metadata_patch_proposal(
            target_node=target_nodes[0],
            updates=single_reverse_updates,
            rationale=rationale,
            summary=f"Revert previous metadata change on {target_nodes[0].title}",
        )
    else:
        proposal = build_bulk_metadata_patch_proposal(
            target_nodes=target_nodes,
            target_label=f"{len(target_nodes)} tasks from previous action",
            updates=reverse_updates or {},
            target_updates=(None if reverse_updates is not None else reverse_target_updates),
            rationale=rationale,
            summary=f"Revert previous metadata change for {len(target_nodes)} tasks",
        )
    proposal["reverts_proposal_id"] = proposal_record.id
    return proposal, ""


def build_previous_action_revert_response(
    session: Session,
    *,
    strategist_session_id: str,
    active_category_id: int | None,
    focus_node_id: int | None,
    staged_node_ids: list[int],
    provider_label: str,
) -> dict[str, Any]:
    previous_proposal = get_latest_approved_session_proposal(session, strategist_session_id)
    fallback_title = None
    if previous_proposal is not None:
        fallback_title = str(
            _json_loads_dict(previous_proposal.payload_json).get("target_node_title") or ""
        ).strip() or None
    context_cards, suggested_prompts = build_proposal_response_context(
        session=session,
        active_category_id=active_category_id,
        focus_node_id=focus_node_id,
        staged_node_ids=staged_node_ids,
        fallback_title=fallback_title,
    )
    if previous_proposal is None:
        summary = "I could not find an approved proposal in this session to reverse."
        metadata_proposal = None
    else:
        metadata_proposal, reason = build_reverse_metadata_proposal(
            session,
            proposal_record=previous_proposal,
        )
        if metadata_proposal is None:
            summary = f"I found the previous approved action, but I cannot reverse it safely yet. {reason}"
        else:
            summary = (
                "I prepared a reversal patch for the previous approved metadata action. "
                "Review it before applying."
            )
            focus_title = str(metadata_proposal.get("target_node_title") or "").strip()
            if focus_title:
                suggested_prompts = [
                    "Approve the reversal",
                    f"What changed in {focus_title}?",
                    "Review my workload across categories",
                ]
    return {
        "provider_label": provider_label,
        "mode": "metadata" if metadata_proposal is not None else "review",
        "summary": summary,
        "context_cards": context_cards,
        "suggested_prompts": suggested_prompts,
        "action_proposal": None,
        "metadata_proposal": metadata_proposal,
        "branch_proposal": None,
        "clarification": None,
    }


def apply_metadata_proposal(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
) -> list[StrategyNode]:
    updates_by_node_id = get_metadata_updates_for_targets(proposal_payload)
    if not updates_by_node_id:
        raise ValueError("Metadata proposal is missing its target node or updates.")
    updated_nodes: list[StrategyNode] = []
    for node_id, updates in updates_by_node_id.items():
        body = StrategyNodeUpdate.model_validate(updates)
        updated_nodes.append(update_node(node_id=node_id, body=body, session=session))
    return updated_nodes


def apply_metadata_updates_without_commit(
    session: Session,
    *,
    node: StrategyNode,
    updates: dict[str, Any],
) -> StrategyNode:
    body = StrategyNodeUpdate.model_validate(updates)
    if body.title is not None:
        node.title = body.title
    if body.notes is not None:
        node.notes = body.notes
    if body.status is not None:
        node.status = body.status
    if body.priority is not None:
        node.priority = body.priority
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


def delete_node_without_commit(
    session: Session,
    *,
    node_id: int,
) -> None:
    node = session.get(StrategyNode, node_id)
    if node is None:
        return
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


def _structural_operations_from_payload(proposal_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_operations = proposal_payload.get("structural_operations")
    if not isinstance(raw_operations, list):
        return []
    return [item for item in raw_operations if isinstance(item, dict)]


def _resolve_structural_parent_id(
    raw_parent_id: Any,
    raw_parent_temp_id: Any,
    temp_nodes: dict[str, StrategyNode],
) -> int | None:
    if isinstance(raw_parent_id, int):
        return raw_parent_id
    if isinstance(raw_parent_temp_id, str):
        parent_node = temp_nodes.get(raw_parent_temp_id)
        if parent_node is not None and parent_node.id is not None:
            return int(parent_node.id)
    return None


def _validate_structural_operations(
    session: Session,
    *,
    operations: list[dict[str, Any]],
) -> None:
    temp_ids = {
        str(operation.get("temp_id"))
        for operation in operations
        if operation.get("kind") == "create_node"
        and isinstance(operation.get("temp_id"), str)
        and str(operation.get("temp_id")).strip()
    }
    created_temp_ids: set[str] = set()
    temp_parent_refs: dict[str, int | str | None] = {}
    metadata_update_node_ids: list[int] = []
    delete_node_ids: list[int] = []
    delete_operations_by_node_id: dict[int, dict[str, Any]] = {}
    moved_parent_overrides: dict[int, int | str | None] = {}
    move_operation_indices: dict[int, int] = {}
    for operation_index, operation in enumerate(operations):
        kind = str(operation.get("kind") or "").strip()
        if kind == "create_node":
            title = str(operation.get("title") or "").strip()
            category_id = operation.get("category_id")
            if not title or not isinstance(category_id, int):
                raise ValueError("Structural create operation is missing title or category.")
            parent_id = operation.get("parent_id")
            parent_temp_id = operation.get("parent_temp_id")
            parent_ref: int | str | None = None
            if isinstance(parent_id, int):
                parent = session.get(StrategyNode, parent_id)
                if parent is None or parent.category_id != category_id:
                    raise ValueError("Structural create operation references an invalid parent.")
                parent_ref = parent_id
            elif isinstance(parent_temp_id, str) and parent_temp_id not in created_temp_ids:
                raise ValueError("Structural create operation references a parent that has not been created yet.")
            elif isinstance(parent_temp_id, str):
                parent_ref = f"temp:{parent_temp_id}"
            temp_id = operation.get("temp_id")
            if isinstance(temp_id, str) and temp_id.strip():
                if temp_id in created_temp_ids:
                    raise ValueError("Structural create operation reused a temporary id.")
                created_temp_ids.add(temp_id)
                temp_parent_refs[temp_id] = parent_ref
        elif kind == "move_subtree":
            node_id = operation.get("node_id")
            if not isinstance(node_id, int):
                raise ValueError("Structural move operation is missing its node id.")
            node = session.get(StrategyNode, node_id)
            if node is None:
                raise ValueError("Structural move operation references a missing task.")
            destination_category_id = operation.get("destination_category_id")
            if not isinstance(destination_category_id, int):
                destination_category_id = int(node.category_id)
            if session.get(Category, destination_category_id) is None:
                raise ValueError("Structural move operation references a missing destination category.")
            destination_parent_id = operation.get("destination_parent_id")
            destination_parent_temp_id = operation.get("destination_parent_temp_id")
            if isinstance(destination_parent_id, int):
                destination_parent = session.get(StrategyNode, destination_parent_id)
                if destination_parent is None:
                    raise ValueError("Structural move operation references a missing destination.")
                if destination_parent.category_id != destination_category_id:
                    raise ValueError(
                        "Structural move destination parent is in a different category."
                    )
                if destination_parent.id == node.id:
                    raise ValueError("Structural move operation would parent a task to itself.")
                if is_descendant(session, node.id, destination_parent.id):
                    raise ValueError("Structural move operation would move a task under its descendant.")
            elif isinstance(destination_parent_temp_id, str):
                if destination_parent_temp_id not in temp_ids:
                    raise ValueError("Structural move operation references an unknown new parent.")
                if destination_parent_temp_id not in created_temp_ids:
                    raise ValueError("Structural move operation references a parent that has not been created yet.")
            elif destination_parent_id is not None:
                raise ValueError("Structural move operation has an invalid destination.")
            moved_parent_overrides[node_id] = (
                destination_parent_id
                if isinstance(destination_parent_id, int)
                else f"temp:{destination_parent_temp_id}"
                if isinstance(destination_parent_temp_id, str)
                else None
            )
            move_operation_indices[node_id] = operation_index
        elif kind == "metadata_update":
            node_id = operation.get("node_id")
            updates = operation.get("updates")
            if not isinstance(node_id, int) or not isinstance(updates, dict) or not updates:
                raise ValueError("Structural metadata operation is missing its node id or updates.")
            node = session.get(StrategyNode, node_id)
            if node is None:
                raise ValueError("Structural metadata operation references a missing task.")
            StrategyNodeUpdate.model_validate(updates)
            metadata_update_node_ids.append(node_id)
        elif kind == "delete_node":
            node_id = operation.get("node_id")
            if not isinstance(node_id, int):
                raise ValueError("Structural delete operation is missing its node id.")
            node = session.get(StrategyNode, node_id)
            if node is None:
                raise ValueError("Structural delete operation references a missing task.")
            delete_node_ids.append(node_id)
            delete_operations_by_node_id[node_id] = operation
        elif kind == "archive_node":
            node_id = operation.get("node_id")
            if not isinstance(node_id, int) or session.get(StrategyNode, node_id) is None:
                raise ValueError("Structural archive operation references a missing task.")
        else:
            raise ValueError(f"Unsupported structural operation: {kind or 'unknown'}.")

    def effective_parent_ref(node_ref: int | str) -> int | str | None:
        if isinstance(node_ref, str):
            if not node_ref.startswith("temp:"):
                return None
            return temp_parent_refs.get(node_ref.removeprefix("temp:"))
        node_id = node_ref
        if node_id in moved_parent_overrides:
            return moved_parent_overrides[node_id]
        node = session.get(StrategyNode, node_id)
        if node is None:
            return None
        return node.parent_id

    def effectively_is_descendant(ancestor_id: int, node_ref: int | str) -> bool:
        current_parent_ref = effective_parent_ref(node_ref)
        seen_refs: set[int | str] = set()
        while current_parent_ref is not None and current_parent_ref not in seen_refs:
            if current_parent_ref == ancestor_id:
                return True
            seen_refs.add(current_parent_ref)
            current_parent_ref = effective_parent_ref(current_parent_ref)
        return False

    def original_descendant_ids(parent_id: int) -> list[int]:
        descendants: list[int] = []

        def visit(node_id: int) -> None:
            children = session.exec(select(StrategyNode).where(StrategyNode.parent_id == node_id)).all()
            for child in children:
                if child.id is None:
                    continue
                child_id = int(child.id)
                descendants.append(child_id)
                visit(child_id)

        visit(parent_id)
        return descendants

    for delete_node_id in delete_node_ids:
        delete_operation_index = next(
            (
                index
                for index, operation in enumerate(operations)
                if operation.get("kind") == "delete_node" and operation.get("node_id") == delete_node_id
            ),
            len(operations),
        )
        delete_operation = delete_operations_by_node_id.get(delete_node_id, {})
        allow_delete_subtree = bool(delete_operation.get("allow_delete_subtree"))
        for descendant_id in original_descendant_ids(delete_node_id):
            if descendant_id in delete_node_ids:
                continue
            if effectively_is_descendant(delete_node_id, descendant_id):
                if not allow_delete_subtree:
                    raise ValueError(
                        "Structural delete operation would delete existing descendants that were not moved away."
                    )
            else:
                move_index = move_operation_indices.get(descendant_id)
                if move_index is not None and move_index > delete_operation_index:
                    raise ValueError(
                        "Structural delete operation must come after moves that preserve its children."
                    )
        for temp_id in created_temp_ids:
            if effectively_is_descendant(delete_node_id, f"temp:{temp_id}") and not allow_delete_subtree:
                raise ValueError(
                    "Structural delete operation would remove newly-created restructuring groups."
                )
        for metadata_node_id in metadata_update_node_ids:
            if effectively_is_descendant(delete_node_id, metadata_node_id):
                raise ValueError(
                    "Structural mixed proposal cannot update a task that is also being deleted."
                )
        for other_delete_node_id in delete_node_ids:
            if delete_node_id == other_delete_node_id:
                continue
            if effectively_is_descendant(delete_node_id, other_delete_node_id):
                raise ValueError("Structural delete operation includes nested duplicate delete targets.")


def apply_structural_branch_proposal(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
) -> list[StrategyNode]:
    operations = _structural_operations_from_payload(proposal_payload)
    if not operations:
        return []
    _validate_structural_operations(session, operations=operations)

    temp_nodes: dict[str, StrategyNode] = {}
    affected_nodes: list[StrategyNode] = []

    def move_category_for_subtree(node: StrategyNode, category_id: int) -> None:
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
            for block in session.exec(
                select(CalendarBlock).where(CalendarBlock.task_id.in_(moved_ids))
            ).all():
                block.category_id = category_id
                session.add(block)
            for series in session.exec(
                select(RecurrenceSeries).where(RecurrenceSeries.task_id.in_(moved_ids))
            ).all():
                series.category_id = category_id
                session.add(series)

    def archive_subtree(node: StrategyNode) -> None:
        frontier = [node]
        while frontier:
            current = frontier.pop()
            tags = decode_tags(current.tags)
            if "state:archived" not in [tag.lower() for tag in tags]:
                tags.append("state:archived")
            current.tags = encode_tags(tags)
            session.add(current)
            affected_nodes.append(current)
            if current.id is not None:
                frontier.extend(
                    session.exec(
                        select(StrategyNode).where(StrategyNode.parent_id == current.id)
                    ).all()
                )

    for operation in operations:
        kind = str(operation.get("kind") or "").strip()
        if kind == "create_node":
            category_id = int(operation["category_id"])
            parent_id = _resolve_structural_parent_id(
                operation.get("parent_id"),
                operation.get("parent_temp_id"),
                temp_nodes,
            )
            parent = session.get(StrategyNode, parent_id) if parent_id is not None else None
            siblings = list_siblings(session, category_id, parent_id)
            node = StrategyNode(
                id=allocate_strategy_node_id(session),
                title=str(operation.get("title") or "").strip(),
                notes=(
                    str(operation["notes"]).strip()
                    if isinstance(operation.get("notes"), str) and str(operation["notes"]).strip()
                    else None
                ),
                priority=(
                    str(operation.get("priority") or "medium").strip().lower()
                    if str(operation.get("priority") or "medium").strip().lower() in {"low", "medium", "high"}
                    else "medium"
                ),
                status=(
                    str(operation.get("status") or "todo").strip()
                    if str(operation.get("status") or "todo").strip() in {"todo", "in-progress", "done"}
                    else "todo"
                ),
                node_type="habit" if operation.get("node_type") == "habit" else "task",
                category_id=category_id,
                parent_id=parent_id,
                sort_order=len(siblings),
                is_backlog=bool(parent.is_backlog) if parent is not None else False,
            )
            session.add(node)
            session.flush()
            temp_id = operation.get("temp_id")
            if isinstance(temp_id, str) and temp_id.strip():
                temp_nodes[temp_id] = node
            affected_nodes.append(node)
        elif kind == "move_subtree":
            node = session.get(StrategyNode, int(operation["node_id"]))
            if node is None:
                raise ValueError("Structural move operation references a missing task.")
            destination_parent_id = _resolve_structural_parent_id(
                operation.get("destination_parent_id"),
                operation.get("destination_parent_temp_id"),
                temp_nodes,
            )
            target_parent = (
                session.get(StrategyNode, destination_parent_id)
                if destination_parent_id is not None
                else None
            )
            destination_category_id = operation.get("destination_category_id")
            if not isinstance(destination_category_id, int):
                destination_category_id = (
                    int(target_parent.category_id)
                    if target_parent is not None
                    else int(node.category_id)
                )
            old_category_id = int(node.category_id)
            old_siblings = [
                sibling
                for sibling in list_siblings(session, old_category_id, node.parent_id)
                if sibling.id != node.id
            ]
            new_siblings = [
                sibling
                for sibling in list_siblings(
                    session,
                    destination_category_id,
                    destination_parent_id,
                )
                if sibling.id != node.id
            ]
            raw_target_index = operation.get("target_index")
            target_index = (
                raw_target_index if isinstance(raw_target_index, int) else len(new_siblings)
            )
            target_index = max(0, min(target_index, len(new_siblings)))
            old_parent_id = node.parent_id
            node.parent_id = destination_parent_id
            if destination_category_id != old_category_id:
                move_category_for_subtree(node, destination_category_id)
            if target_parent is not None:
                apply_backlog_to_subtree(session, node, target_parent.is_backlog)
            new_siblings.insert(target_index, node)
            if old_parent_id != destination_parent_id or old_category_id != destination_category_id:
                resequence_nodes(session, old_siblings)
            resequence_nodes(session, new_siblings)
            session.add(node)
            affected_nodes.append(node)
        elif kind == "metadata_update":
            node = session.get(StrategyNode, int(operation["node_id"]))
            if node is None:
                raise ValueError("Structural metadata operation references a missing task.")
            updates = operation.get("updates")
            if not isinstance(updates, dict):
                raise ValueError("Structural metadata operation is missing updates.")
            affected_nodes.append(
                apply_metadata_updates_without_commit(
                    session,
                    node=node,
                    updates=updates,
                )
            )
        elif kind == "delete_node":
            delete_node_without_commit(
                session,
                node_id=int(operation["node_id"]),
            )
        elif kind == "archive_node":
            node = session.get(StrategyNode, int(operation["node_id"]))
            if node is None:
                raise ValueError("Structural archive operation references a missing task.")
            archive_subtree(node)

    session.commit()
    refreshed_nodes: list[StrategyNode] = []
    seen_ids: set[int] = set()
    for node in affected_nodes:
        if node.id is None or node.id in seen_ids:
            continue
        session.refresh(node)
        refreshed_nodes.append(node)
        seen_ids.add(node.id)
    return refreshed_nodes


def apply_branch_proposal(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
) -> list[StrategyNode]:
    if _structural_operations_from_payload(proposal_payload):
        return apply_structural_branch_proposal(session, proposal_payload=proposal_payload)

    target_node_id = proposal_payload.get("target_node_id")
    target_category_id = proposal_payload.get("target_category_id")
    proposed_children = proposal_payload.get("proposed_children")
    if (
        not isinstance(target_node_id, int)
        or not isinstance(target_category_id, int)
        or not isinstance(proposed_children, list)
    ):
        raise ValueError("Branch proposal is missing its target branch data.")
    created_nodes: list[StrategyNode] = []
    for child_title in proposed_children:
        if not isinstance(child_title, str) or not child_title.strip():
            continue
        created_nodes.append(
            create_node(
                body=StrategyNodeCreate.model_validate(
                    {
                        "title": child_title.strip(),
                        "category_id": target_category_id,
                        "parent_id": target_node_id,
                    }
                ),
                session=session,
            )
        )
    if not created_nodes:
        raise ValueError("Branch proposal did not contain any valid child titles.")
    return created_nodes


def apply_action_proposal(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
) -> None:
    if str(proposal_payload.get("kind") or "").strip().lower() != "delete_node":
        raise ValueError("Only delete-node action proposals can be applied right now.")
    target_node_ids: list[int] = []
    raw_target_node_ids = proposal_payload.get("target_node_ids")
    if isinstance(raw_target_node_ids, list):
        for node_id in raw_target_node_ids:
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    target_node_id = proposal_payload.get("target_node_id")
    if isinstance(target_node_id, int) and target_node_id not in target_node_ids:
        target_node_ids.append(target_node_id)
    if not target_node_ids:
        raise ValueError("Delete proposal is missing its target node id.")
    for node_id in target_node_ids:
        if session.get(StrategyNode, node_id) is None:
            continue
        delete_node(node_id=node_id, session=session)


def supersede_other_pending_proposals(
    session: Session,
    *,
    strategist_session_id: str,
    keep_proposal_ids: set[str],
) -> None:
    pending_records = list(
        session.exec(
            select(StrategistProposalRecord)
            .where(StrategistProposalRecord.session_id == strategist_session_id)
            .where(StrategistProposalRecord.status == "pending")
        ).all()
    )
    for record in pending_records:
        if record.id in keep_proposal_ids:
            continue
        record.status = "superseded"
        record.updated_at = datetime.utcnow()
        session.add(record)
        update_change_set_status(
            session,
            change_set_id=record.change_set_id,
            status="superseded",
            outcome="superseded",
        )
    if pending_records:
        session.commit()


def collect_proposal_target_node_ids(proposal_payload: dict[str, Any]) -> list[int]:
    target_node_ids: list[int] = []
    raw_target_updates = proposal_payload.get("target_updates")
    if isinstance(raw_target_updates, list):
        for item in raw_target_updates:
            if not isinstance(item, dict):
                continue
            node_id = item.get("target_node_id")
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    raw_target_node_ids = proposal_payload.get("target_node_ids")
    if isinstance(raw_target_node_ids, list):
        for node_id in raw_target_node_ids:
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    raw_subtree_root_ids = proposal_payload.get("subtree_root_ids")
    if isinstance(raw_subtree_root_ids, list):
        for node_id in raw_subtree_root_ids:
            if isinstance(node_id, int) and node_id not in target_node_ids:
                target_node_ids.append(node_id)
    target_node_id = (
        proposal_payload.get("target_node_id")
        if isinstance(proposal_payload.get("target_node_id"), int)
        else None
    )
    if target_node_id is not None and target_node_id not in target_node_ids:
        target_node_ids.append(target_node_id)
    task_patch = proposal_payload.get("task_patch")
    operations = task_patch.get("operations") if isinstance(task_patch, dict) else None
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            for key in ("task_id", "parent_id"):
                node_id = operation.get(key)
                if isinstance(node_id, int) and node_id not in target_node_ids:
                    target_node_ids.append(node_id)
    return target_node_ids


def collect_standard_proposal_preview(proposal_payload: dict[str, Any]) -> list[str]:
    preview: list[str] = []
    raw_diff_preview = proposal_payload.get("diff_preview")
    if isinstance(raw_diff_preview, list):
        for item in raw_diff_preview:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if label and detail:
                preview.append(f"{label}: {detail}")
            elif label:
                preview.append(label)
            elif detail:
                preview.append(detail)
    if not preview:
        for fallback_key in ("preview", "consequences", "proposed_children"):
            raw_items = proposal_payload.get(fallback_key)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if isinstance(item, str) and item.strip() and item.strip() not in preview:
                    preview.append(item.strip())
    return preview


def summarize_task_patch_operation(operation: dict[str, Any]) -> str | None:
    op = str(operation.get("op") or "").strip().lower()
    if op == "update":
        task_id = operation.get("task_id")
        fields = operation.get("fields") if isinstance(operation.get("fields"), dict) else {}
        field_names = ", ".join(sorted(fields.keys()))
        return f"Update task {task_id}: {field_names or 'metadata'}"
    if op == "create":
        title = str(operation.get("title") or "new task").strip() or "new task"
        parent = operation.get("parent_id") or operation.get("parent_temp_id")
        return f"Create {title}" + (f" under {parent}" if parent else "")
    if op == "move":
        task_id = operation.get("task_id")
        parent = operation.get("parent_id") or operation.get("parent_temp_id") or "top level"
        return f"Move task {task_id} under {parent}"
    if op == "delete":
        task_id = operation.get("task_id")
        mode = str(operation.get("mode") or "delete").replace("_", " ")
        return f"{mode.title()} task {task_id}"
    return None


def enrich_proposal_payload_with_task_patch(
    *,
    proposal_kind: str,
    proposal_payload: dict[str, Any],
    nodes: list[StrategyNode],
    category_ids: set[int] | None = None,
) -> dict[str, Any]:
    task_patch = proposal_payload.get("task_patch")
    if not isinstance(task_patch, dict):
        task_patch = build_task_patch_draft(
            proposal_kind=proposal_kind,
            proposal=proposal_payload,
        )
    validation = validate_task_patch(task_patch, nodes, category_ids=category_ids)
    simulation = simulate_task_patch(task_patch, nodes)
    proposal_payload["task_patch"] = task_patch
    proposal_payload["task_patch_validation"] = validation
    proposal_payload["task_patch_simulation"] = simulation
    proposal_payload["task_patch_status"] = validation["status"]
    validation_warnings = [
        item
        for item in proposal_payload.get("validation_warnings", [])
        if isinstance(item, str) and item.strip()
    ]
    for warning in validation["warnings"]:
        if warning not in validation_warnings:
            validation_warnings.append(warning)
    proposal_payload["validation_warnings"] = validation_warnings
    return proposal_payload


def _prompt_references_recurring_task(prompt: str, task_title: str) -> bool:
    normalized_prompt = " ".join(_TASK_CONTEXT_WORD_RE.findall(str(prompt or "").lower()))
    normalized_title = " ".join(_TASK_CONTEXT_WORD_RE.findall(str(task_title or "").lower()))
    if not normalized_prompt or not normalized_title:
        return False
    if normalized_title in normalized_prompt:
        return True
    title_terms = {
        term
        for term in normalized_title.split()
        if term not in _TASK_CONTEXT_STOP_WORDS and len(term) > 1
    }
    if not title_terms:
        return False
    prompt_terms = set(normalized_prompt.split())
    matching_terms = title_terms & prompt_terms
    required_matches = 1 if len(title_terms) == 1 else 2
    return (
        len(matching_terms) >= required_matches
        and bool(_RECURRENCE_CONTEXT_RE.search(prompt))
    )


def _recurring_policy_prompt(
    *,
    request_prompt: str | None,
    request_context_prompts: list[str] | None,
    task_title: str,
) -> str:
    """Recover the user-authored placement anchor across clarification turns.

    A duration-only reply can produce a complete recurring proposal while no
    longer containing the clock time from the request that opened the flow.
    Collision policy must still see that original request, but only when it
    refers to this recurring task; unrelated times elsewhere in a long chat
    must not leak into the proposal.
    """

    current_prompt = str(request_prompt or "").strip()
    if prompt_has_explicit_clock_time(current_prompt):
        return current_prompt
    for candidate in reversed((request_context_prompts or [])[-12:]):
        prior_prompt = str(candidate or "").strip()
        if (
            prior_prompt
            and prompt_has_explicit_clock_time(prior_prompt)
            and _prompt_references_recurring_task(prior_prompt, task_title)
        ):
            return "\n".join(part for part in (prior_prompt, current_prompt) if part)
    return current_prompt


def enrich_proposal_payload_with_recurring_schedule(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
    request_prompt: str | None = None,
    request_context_prompts: list[str] | None = None,
) -> dict[str, Any]:
    spec = proposal_payload.get("recurring_schedule")
    task_patch = proposal_payload.get("task_patch")
    if not isinstance(spec, dict) or not isinstance(task_patch, dict):
        return proposal_payload
    operations = task_patch.get("operations")
    create_operations = [
        operation
        for operation in operations if isinstance(operation, dict)
        and str(operation.get("op") or "") == "create"
    ] if isinstance(operations, list) else []
    target_temp_id = str(
        spec.get("target_temp_id") or spec.get("targetTempId") or ""
    ).strip()
    create_operation = next(
        (
            operation for operation in create_operations
            if target_temp_id and str(operation.get("temp_id") or "").strip() == target_temp_id
        ),
        create_operations[0] if len(create_operations) == 1 else None,
    )
    if create_operation is None:
        proposal_payload["recurring_schedule_status"] = "invalid"
        proposal_payload["recurring_schedule_errors"] = [
            "The recurring schedule must reference the task created in this proposal."
        ]
        return proposal_payload
    category_id = create_operation.get("category_id")
    fields = create_operation.get("fields") if isinstance(create_operation.get("fields"), dict) else {}
    recurring_duration = spec.get("duration_minutes", spec.get("durationMinutes"))
    if (
        "time_estimate_minutes" not in fields
        and isinstance(recurring_duration, int)
        and not isinstance(recurring_duration, bool)
    ):
        fields = dict(fields)
        fields["time_estimate_minutes"] = recurring_duration
        create_operation["fields"] = fields
    if not isinstance(category_id, int):
        proposal_payload["recurring_schedule_status"] = "invalid"
        proposal_payload["recurring_schedule_errors"] = [
            "Choose a workspace for this recurring task."
        ]
        return proposal_payload
    title = str(create_operation.get("title") or "").strip()
    policy_prompt = _recurring_policy_prompt(
        request_prompt=request_prompt,
        request_context_prompts=request_context_prompts,
        task_title=title,
    )
    plan = build_recurring_schedule_plan(
        session,
        spec=spec,
        category_id=category_id,
        task_title=title,
        task_emoji=(str(fields.get("emoji") or "").strip() or None),
        task_priority=(str(fields.get("priority") or "medium").strip() or "medium"),
        preference_override_authorized=(
            preference_override_authorized_from_prompt(policy_prompt)
        ),
        allow_priority_displacement=True,
        prompt=policy_prompt,
    )
    plan["target_temp_id"] = str(create_operation.get("temp_id") or target_temp_id).strip()
    if plan.get("status") == "valid":
        canonical_schedule = dict(spec)
        for key in (
            "frequency",
            "days_of_week",
            "start_date",
            "end_mode",
            "duration_minutes",
            "preferred_start_time",
            "allowed_start_time",
            "allowed_end_time",
            "gap_minutes",
            "timezone",
            "strict",
            "conflict_relocation",
        ):
            if key in plan:
                canonical_schedule[key] = plan[key]
        # ``plan.end_date`` is the end of the currently materialized preview,
        # not necessarily the end of the user's recurrence.  Copying that
        # rolling horizon back into the durable recurrence spec made the next
        # proposal edit reinterpret an ongoing series as a finite one.  Keep
        # the open-ended contract explicit and leave the preview boundary in
        # ``recurring_schedule_plan.materialization_end_date`` only.
        if plan.get("end_mode") == "never":
            canonical_schedule["end_date"] = None
            canonical_schedule.pop("endDate", None)
            canonical_schedule.pop("occurrence_count", None)
            canonical_schedule.pop("occurrenceCount", None)
        else:
            canonical_schedule["end_date"] = plan.get("series_end_date")
            if "occurrence_count" in spec:
                canonical_schedule["occurrence_count"] = spec["occurrence_count"]
            elif "occurrenceCount" in spec:
                canonical_schedule["occurrence_count"] = spec["occurrenceCount"]
        canonical_schedule["target_temp_id"] = plan["target_temp_id"]
        proposal_payload["recurring_schedule"] = canonical_schedule
    proposal_payload["recurring_schedule_plan"] = plan
    proposal_payload["recurring_schedule_status"] = plan.get("status")
    proposal_payload["recurring_schedule_errors"] = list(plan.get("errors") or [])
    validation_warnings = [
        str(item).strip()
        for item in proposal_payload.get("validation_warnings", [])
        if isinstance(item, str) and item.strip()
    ]
    for warning in plan.get("validation_warnings", []):
        if isinstance(warning, str) and warning.strip() and warning not in validation_warnings:
            validation_warnings.append(warning)
    proposal_payload["validation_warnings"] = validation_warnings
    if plan.get("status") == "valid":
        frequency = str(plan.get("frequency") or "daily")
        if frequency == "weekly":
            weekday_names = [
                str(item).title()
                for item in plan.get("days_of_week", [])
                if isinstance(item, str) and item.strip()
            ]
            cadence = f"weekly on {', '.join(weekday_names)}" if weekday_names else "weekly"
        else:
            cadence = "daily"
        proposal_payload["summary"] = f"Create and schedule {title}"
        if plan.get("end_mode") == "never":
            proposal_payload["rationale"] = (
                f"Add one {int(plan.get('duration_minutes') or 0)}-minute task and schedule it "
                f"{cadence}, starting {plan.get('start_date')}."
            )
        else:
            proposal_payload["rationale"] = (
                f"Add one {int(plan.get('duration_minutes') or 0)}-minute task and schedule it "
                f"{cadence} from {plan.get('start_date')} through {plan.get('end_date')}."
            )
    return proposal_payload


def enrich_proposal_payload_with_created_task_schedule(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
) -> dict[str, Any]:
    spec = proposal_payload.get("created_task_schedule")
    task_patch = proposal_payload.get("task_patch")
    if not isinstance(spec, dict) or not isinstance(task_patch, dict):
        return proposal_payload
    operations = (
        task_patch.get("operations")
        if isinstance(task_patch.get("operations"), list)
        else []
    )
    create_operations = [
        operation
        for operation in operations
        if isinstance(operation, dict)
        and str(operation.get("op") or "") == "create"
    ]
    plan = build_created_task_schedule_plan(
        session,
        spec=spec,
        create_operations=create_operations,
    )
    proposal_payload["created_task_schedule_plan"] = plan
    proposal_payload["created_task_schedule_status"] = plan.get("status")
    proposal_payload["created_task_schedule_errors"] = list(plan.get("errors") or [])
    if plan.get("status") == "valid":
        task_count = int(plan.get("task_count") or 0)
        block_count = int(plan.get("block_count") or 0)
        proposal_payload["summary"] = (
            f"Create and schedule {task_count} task{'' if task_count == 1 else 's'}"
        )
        proposal_payload["rationale"] = (
            f"Create the tasks and add {block_count} reviewed calendar block"
            f"{'' if block_count == 1 else 's'} in one approval."
        )
    return proposal_payload


def schedule_patch_from_created_task_schedule_plan(
    session: Session,
    *,
    plan: dict[str, Any],
    temp_node_ids: dict[str, int],
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    working_set_task_ids: list[int] = []
    for index, block in enumerate(plan.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        temp_id = str(block.get("target_temp_id") or "").strip()
        task_id = temp_node_ids.get(temp_id)
        if not isinstance(task_id, int):
            raw_task_id = block.get("target_task_id")
            task_id = (
                raw_task_id
                if isinstance(raw_task_id, int) and not isinstance(raw_task_id, bool)
                else None
            )
        task = session.get(StrategyNode, task_id) if isinstance(task_id, int) else None
        if task is None or task.id is None:
            raise ValueError("A reviewed calendar block could not resolve its task.")
        if int(task.id) not in working_set_task_ids:
            working_set_task_ids.append(int(task.id))
        accepted_block_id = block.get("accepted_block_id")
        base_operation = {
            "taskId": int(task.id),
            "categoryId": int(task.category_id),
            "title": task.title,
            "emoji": task.emoji,
            "startAt": block.get("start_at"),
            "endAt": block.get("end_at"),
            "taskStatus": task.status,
            "taskDeadlineAt": task.deadline_at,
            "taskPriority": task.priority,
            "timeEstimateMinutes": task.time_estimate_minutes,
            "allocatedTimeboxMinutes": block.get("allocated_minutes"),
            "source": "created_task_schedule",
        }
        if isinstance(accepted_block_id, int) and not isinstance(
            accepted_block_id, bool
        ):
            operations.append(
                {
                    **base_operation,
                    "op": "update_block_time",
                    "acceptedBlockId": accepted_block_id,
                    "acceptedBlockRevision": block.get("accepted_block_revision"),
                }
            )
        else:
            operations.append(
                {
                    **base_operation,
                    "op": "create_block",
                    "proposalBlockId": f"created-task-{task.id}-{index + 1}",
                    "variant": "proposed",
                    "completionStatus": "not_started",
                    "isPinned": False,
                    "isProtected": False,
                    "isRecurring": False,
                    "hierarchyDepth": 0,
                    "renderedChildTaskIds": [],
                }
            )
    operations.extend(
        operation
        for operation in plan.get("displacement_operations", [])
        if isinstance(operation, dict)
    )
    return build_schedule_patch(
        intent_summary=str(plan.get("instruction") or "Create and schedule new tasks."),
        working_set_task_ids=working_set_task_ids,
        window_start=datetime.fromisoformat(str(plan.get("window_start"))),
        window_end=datetime.fromisoformat(str(plan.get("window_end"))),
        operations=operations,
    )


def enrich_proposal_payload_with_scheduled_duration(
    session: Session,
    *,
    proposal_payload: dict[str, Any],
    local_date_time: datetime | None,
) -> dict[str, Any]:
    """Cascade a task-duration edit into its upcoming scheduled blocks."""

    task_patch = proposal_payload.get("task_patch")
    operations = task_patch.get("operations") if isinstance(task_patch, dict) else None
    if not isinstance(operations, list):
        return proposal_payload
    intent_text = " ".join(
        str(value or "")
        for value in (
            proposal_payload.get("summary"),
            proposal_payload.get("rationale"),
            task_patch.get("intent_summary") if isinstance(task_patch, dict) else None,
        )
    )
    if re.search(r"\b(?:estimate\s+only|only\s+(?:the\s+)?estimate)\b", intent_text, re.I):
        return proposal_payload
    duration_updates: dict[int, int] = {}
    for operation in operations:
        if not isinstance(operation, dict) or str(operation.get("op") or "") != "update":
            continue
        task_id = operation.get("task_id")
        fields = operation.get("fields") if isinstance(operation.get("fields"), dict) else {}
        duration = fields.get("time_estimate_minutes")
        if (
            isinstance(task_id, int)
            and isinstance(duration, int)
            and not isinstance(duration, bool)
            and duration > 0
        ):
            duration_updates[task_id] = duration
    if not duration_updates:
        return proposal_payload

    effective_now = local_date_time or datetime.utcnow()
    tasks = [
        task
        for task_id in duration_updates
        for task in [session.get(StrategyNode, task_id)]
        if task is not None and task.id is not None
    ]
    task_by_id = {int(task.id): task for task in tasks if task.id is not None}
    all_blocks = list(
        session.exec(
            select(CalendarBlock).where(CalendarBlock.task_id.in_(list(duration_updates)))
        ).all()
    )
    eligible_blocks = [
        block
        for block in all_blocks
        if block.id is not None
        and block.end_at > effective_now
        and block.completion_status not in {"completed", "done", "cancelled"}
        and not block.is_pinned
        and not block.is_protected
        and int((block.end_at - block.start_at).total_seconds() // 60)
        != duration_updates.get(block.task_id)
    ]
    if not eligible_blocks:
        return proposal_payload

    window_start = min(block.start_at for block in eligible_blocks)
    window_end = max(
        max(block.end_at, block.start_at + timedelta(minutes=duration_updates[block.task_id]))
        for block in eligible_blocks
    )
    schedule_patch = build_schedule_patch(
        intent_summary="Update upcoming scheduled blocks to match the task duration.",
        working_set_task_ids=sorted(duration_updates),
        window_start=window_start,
        window_end=window_end,
        operations=[
            {
                "op": "update_block_time",
                "acceptedBlockId": int(block.id),
                "taskId": int(block.task_id),
                "categoryId": int(block.category_id),
                "taskTitle": task_by_id[int(block.task_id)].title,
                "taskEmoji": task_by_id[int(block.task_id)].emoji,
                "startAt": block.start_at.isoformat(timespec="seconds"),
                "endAt": (
                    block.start_at + timedelta(minutes=duration_updates[block.task_id])
                ).isoformat(timespec="seconds"),
                "expectedOriginalStartAt": block.start_at.isoformat(timespec="seconds"),
                "expectedOriginalEndAt": block.end_at.isoformat(timespec="seconds"),
                "variant": "proposed",
                "completionStatus": block.completion_status,
                "isPinned": block.is_pinned,
                "isProtected": block.is_protected,
                "isRecurring": block.is_recurring,
            }
            for block in eligible_blocks
        ],
    )
    validation = validate_schedule_patch(
        schedule_patch,
        tasks=tasks,
        accepted_blocks=list(session.exec(select(CalendarBlock)).all()),
        window_start=window_start,
        window_end=window_end,
    )
    simulation = simulate_schedule_patch(
        schedule_patch,
        tasks=tasks,
        accepted_blocks=list(session.exec(select(CalendarBlock)).all()),
        validation=validation,
    )
    proposal_payload["scheduled_duration_patch"] = schedule_patch
    proposal_payload["scheduled_duration_validation"] = validation
    proposal_payload["scheduled_duration_simulation"] = simulation
    proposal_payload["scheduled_duration_status"] = validation.get("status")
    proposal_payload["scheduled_block_count"] = len(eligible_blocks)
    proposal_payload["scheduled_duration_minutes"] = next(iter(duration_updates.values()))
    diff_preview = proposal_payload.get("diff_preview")
    if not isinstance(diff_preview, list):
        diff_preview = []
        proposal_payload["diff_preview"] = diff_preview
    count = len(eligible_blocks)
    duration = next(iter(duration_updates.values()))
    diff_preview.append(
        {
            "kind": "scheduled_duration_update",
            "label": "Calendar",
            "detail": (
                f"Resize {count} upcoming calendar block{'' if count == 1 else 's'} "
                f"to {duration} minutes"
            ),
        }
    )
    return proposal_payload


def _workspace_local_date_time(workspace_context: dict[str, Any] | None) -> datetime | None:
    if not isinstance(workspace_context, dict):
        return None
    value = workspace_context.get("local_date_time")
    if isinstance(value, datetime):
        return value if value.tzinfo is None else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is None else None
    return None


def build_proposal_review_sections(proposal_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project arbitrary TaskPatch operations into a small, generic review model."""

    task_patch = (
        proposal_payload.get("task_patch")
        if isinstance(proposal_payload.get("task_patch"), dict)
        else {}
    )
    operations = (
        task_patch.get("operations")
        if isinstance(task_patch.get("operations"), list)
        else []
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_order: list[str] = []

    def append_item(section_title: str, item: dict[str, Any]) -> None:
        if section_title not in grouped:
            grouped[section_title] = []
            group_order.append(section_title)
        grouped[section_title].append(item)

    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "").strip().lower()
        task_id = next(
            (
                int(value)
                for value in (
                    operation.get("task_id"),
                    operation.get("node_id"),
                )
                if isinstance(value, int)
            ),
            None,
        )
        title = str(
            operation.get("node_title")
            or operation.get("task_title")
            or operation.get("title")
            or "Task"
        ).strip()
        item = {
            "text": title,
            "task_ids": [task_id] if task_id is not None else [],
        }
        if op == "move":
            destination = str(
                operation.get("category_name")
                or operation.get("destination_parent_title")
                or "New location"
            ).strip()
            append_item(f"Move to {destination}", item)
        elif op == "delete" and str(operation.get("mode") or "").lower() == "archive":
            append_item("Archive", item)
        elif op == "delete":
            append_item("Delete", item)
        elif op == "create":
            append_item("Create", item)
        elif op == "update":
            append_item("Update", item)
        elif op == "reorder":
            append_item("Reorder", item)
        else:
            append_item("Changes", item)

    return [
        {"title": title, "items": grouped[title]}
        for title in group_order
        if grouped[title]
    ]


def build_standard_response_proposal(response_payload: dict[str, Any]) -> dict[str, Any] | None:
    schedule_payload = response_payload.get("schedule_proposal")
    if isinstance(schedule_payload, dict):
        validation = schedule_payload.get("schedulePatchValidation")
        simulation = schedule_payload.get("schedulePatchSimulation")
        schedule_patch = schedule_payload.get("schedulePatch")
        planner_payload = (
            schedule_payload.get("payload")
            if isinstance(schedule_payload.get("payload"), dict)
            else {}
        )
        operation_mode = str(
            schedule_payload.get("operationMode")
            or planner_payload.get("operationMode")
            or "schedule"
        ).strip().lower()
        target_ids = schedule_payload.get("workingSetTaskIds")
        if not isinstance(target_ids, list):
            target_ids = (
                schedule_patch.get("workingSetTaskIds", [])
                if isinstance(schedule_patch, dict)
                else []
            )
        standard_proposal = {
            "proposal_id": str(schedule_payload.get("proposalId") or "").strip() or None,
            "change_set_id": str(schedule_payload.get("change_set_id") or "").strip() or None,
            "change_set_status": schedule_payload.get("change_set_status"),
            "proposal_kind": "schedule_patch",
            "operation": operation_mode,
            "legacy_response_key": "scheduleProposal",
            "title": (
                "Review cancellation"
                if operation_mode == "remove"
                else "Review schedule proposal"
            ),
            "body": (
                str(
                    schedule_payload.get("body")
                    or "Nothing is cancelled until you approve."
                )
                if operation_mode == "remove"
                else str(
                    schedule_payload.get("summary")
                    or "Review these proposed calendar blocks."
                )
            ),
            "summary": schedule_payload.get("summary"),
            "rationale": schedule_payload.get("rationale"),
            "target_node_id": None,
            "target_node_ids": [int(item) for item in target_ids if isinstance(item, int)],
            "target_node_title": "Attached working set",
            "preview": [
                str(item) for item in schedule_payload.get("preview", []) if isinstance(item, str)
            ],
            "validation_warnings": list(
                dict.fromkeys(
                    [
                        *(
                            [str(item) for item in validation.get("warnings", [])]
                            if isinstance(validation, dict)
                            and isinstance(validation.get("warnings"), list)
                            else []
                        ),
                        *[
                            str(item)
                            for item in schedule_payload.get("validationWarnings", [])
                            if isinstance(item, str) and item.strip()
                        ],
                    ]
                )
            ),
            "schedule_patch": schedule_patch,
            "schedule_patch_validation": validation,
            "schedule_patch_simulation": simulation,
            "schedule_patch_status": schedule_payload.get("schedulePatchStatus"),
            "capability_id": "planner",
            "payload": schedule_payload,
        }
        if isinstance(schedule_payload.get("taskPatch"), dict):
            standard_proposal.update(
                {
                    "task_patch": schedule_payload.get("taskPatch"),
                    "task_patch_validation": schedule_payload.get("taskPatchValidation"),
                    "task_patch_simulation": schedule_payload.get("taskPatchSimulation"),
                    "task_patch_status": schedule_payload.get("taskPatchStatus"),
                }
            )
        return standard_proposal
    for response_key, proposal_kind in PROPOSAL_KIND_BY_RESPONSE_KEY.items():
        proposal_payload = response_payload.get(response_key)
        if not isinstance(proposal_payload, dict):
            continue

        target_title = str(proposal_payload.get("target_node_title") or "").strip()
        summary = str(proposal_payload.get("summary") or "").strip() or None
        rationale = str(proposal_payload.get("rationale") or "").strip() or None
        proposal_operation = str(
            proposal_payload.get("proposal_kind")
            or proposal_payload.get("kind")
            or proposal_kind
        ).strip()
        target_node_id = (
            proposal_payload.get("target_node_id")
            if isinstance(proposal_payload.get("target_node_id"), int)
            else None
        )
        task_patch = (
            proposal_payload.get("task_patch")
            if isinstance(proposal_payload.get("task_patch"), dict)
            else {}
        )
        task_patch_operations = (
            task_patch.get("operations")
            if isinstance(task_patch.get("operations"), list)
            else []
        )
        cleanup_operation_count = sum(
            1
            for operation in task_patch_operations
            if isinstance(operation, dict)
            and str(operation.get("op") or "").strip().lower() in {"move", "delete"}
        )
        is_batch_tree_cleanup = cleanup_operation_count > 1

        if response_key == "action_proposal":
            title = str(proposal_payload.get("confirmation_title") or "").strip()
            if not title:
                title = f"Review action for {target_title}" if target_title else "Review action"
            body = (
                rationale
                or str(proposal_payload.get("confirmation_body") or "").strip()
                or summary
                or "Review this proposed task action before applying it."
            )
        elif response_key == "metadata_proposal":
            scheduled_block_count = int(proposal_payload.get("scheduled_block_count") or 0)
            scheduled_duration = int(proposal_payload.get("scheduled_duration_minutes") or 0)
            if scheduled_block_count and scheduled_duration:
                title = f"Update {target_title} to {scheduled_duration} minutes"
                body = (
                    f"Update the task and {scheduled_block_count} upcoming calendar block"
                    f"{'' if scheduled_block_count == 1 else 's'}."
                )
            else:
                title = f"Review task changes for {target_title}" if target_title else "Review task changes"
                body = summary or "Review these task changes before applying them."
        else:
            recurring_plan = proposal_payload.get("recurring_schedule_plan")
            created_task_plan = proposal_payload.get("created_task_schedule_plan")
            if isinstance(recurring_plan, dict):
                recurring_title = str(recurring_plan.get("task_title") or target_title or "task").strip()
                title = f"Create and schedule {recurring_title}"
                occurrence_count = int(recurring_plan.get("occurrence_count") or 0)
                duration_minutes = int(recurring_plan.get("duration_minutes") or 0)
                frequency = str(recurring_plan.get("frequency") or "daily").strip().lower()
                cadence_label = "weekly" if frequency == "weekly" else "daily"
                conflict_message = str(
                    next(
                        (
                            item.get("userMessage")
                            for item in recurring_plan.get("conflict_resolutions", [])
                            if isinstance(item, dict) and item.get("userMessage")
                        ),
                        "",
                    )
                ).strip()
                body = conflict_message or (
                    f"One {duration_minutes}-minute task with {occurrence_count} upcoming "
                    f"{cadence_label} calendar blocks, all linked to the same task."
                    if recurring_plan.get("end_mode") == "never"
                    else f"One {duration_minutes}-minute task with {occurrence_count} "
                    f"{cadence_label} calendar blocks, all linked to the same task."
                )
            elif isinstance(created_task_plan, dict):
                task_count = int(created_task_plan.get("task_count") or 0)
                block_count = int(created_task_plan.get("block_count") or 0)
                created_blocks = [
                    item
                    for item in created_task_plan.get("blocks", [])
                    if isinstance(item, dict)
                ]
                created_title = str(
                    created_blocks[0].get("task_title")
                    if len(created_blocks) == 1
                    else ""
                ).strip()
                title = (
                    f"Create and schedule {created_title}"
                    if created_title
                    else f"Create and schedule {task_count} task{'' if task_count == 1 else 's'}"
                )
                conflict_resolution = created_task_plan.get("conflict_resolution")
                conflict_message = str(
                    conflict_resolution.get("userMessage")
                    if isinstance(conflict_resolution, dict)
                    else ""
                ).strip()
                body = conflict_message or (
                    f"Create the tasks and add {block_count} calendar block"
                    f"{'' if block_count == 1 else 's'} after approval."
                )
            elif proposal_operation == "create":
                if target_node_id is None and target_title:
                    title = f"Create top-level task in {target_title}"
                else:
                    title = f"Create task under {target_title}" if target_title else "Create task"
            elif proposal_operation == "mixed":
                title = f"Review combined changes for {target_title}" if target_title else "Review combined changes"
            elif proposal_operation in {"move", "reorder", "restructure"}:
                title = (
                    "Review workspace cleanup"
                    if is_batch_tree_cleanup
                    else f"Review tree reorganization for {target_title}"
                    if target_title
                    else "Review tree reorganization"
                )
            else:
                title = f"Review task breakdown for {target_title}" if target_title else "Review task breakdown"
            if not isinstance(recurring_plan, dict) and not isinstance(created_task_plan, dict):
                body = (
                    summary or rationale or "Review these workspace changes before applying them."
                    if is_batch_tree_cleanup
                    else rationale or summary or "Review these task-tree changes before applying them."
                )

        validation_warnings = [
            item.strip()
            for item in proposal_payload.get("validation_warnings", [])
            if isinstance(item, str) and item.strip()
        ]

        standard_proposal = {
            "proposal_id": str(proposal_payload.get("proposal_id") or "").strip() or None,
            "change_set_id": str(proposal_payload.get("change_set_id") or "").strip() or None,
            "change_set_status": proposal_payload.get("change_set_status"),
            "proposal_kind": proposal_kind,
            "operation": proposal_operation or proposal_kind,
            "legacy_response_key": STANDARD_PROPOSAL_LEGACY_KEYS[response_key],
            "title": title,
            "body": body,
            "summary": summary,
            "rationale": rationale,
            "target_node_id": target_node_id,
            "target_node_ids": collect_proposal_target_node_ids(proposal_payload),
            "target_node_title": target_title or None,
            "preview": collect_standard_proposal_preview(proposal_payload),
            "review_sections": build_proposal_review_sections(proposal_payload),
            "validation_warnings": validation_warnings,
            "task_patch": proposal_payload.get("task_patch"),
            "task_patch_validation": proposal_payload.get("task_patch_validation"),
            "task_patch_simulation": proposal_payload.get("task_patch_simulation"),
            "task_patch_status": proposal_payload.get("task_patch_status"),
            "schedule_patch": proposal_payload.get("scheduled_duration_patch"),
            "schedule_patch_validation": proposal_payload.get("scheduled_duration_validation"),
            "schedule_patch_simulation": proposal_payload.get("scheduled_duration_simulation"),
            "schedule_patch_status": proposal_payload.get("scheduled_duration_status"),
            "payload": proposal_payload,
        }
        badge_suggestions = proposal_payload.get("badge_suggestions")
        if isinstance(badge_suggestions, list) and badge_suggestions:
            standard_proposal["badge_suggestions"] = badge_suggestions
        return standard_proposal

    preference_payload = response_payload.get(PREFERENCE_RESPONSE_KEY)
    if isinstance(preference_payload, dict):
        summary = str(preference_payload.get("summary") or "").strip() or None
        rationale = str(preference_payload.get("rationale") or "").strip() or None
        validation = preference_payload.get("preference_patch_validation")
        status = (
            str(validation.get("status") or "").strip().lower()
            if isinstance(validation, dict)
            else str(preference_payload.get("preference_patch_status") or "").strip().lower()
        )
        return {
            "proposal_id": str(preference_payload.get("proposal_id") or "").strip() or None,
            "change_set_id": str(preference_payload.get("change_set_id") or "").strip() or None,
            "change_set_status": preference_payload.get("change_set_status"),
            "proposal_kind": PREFERENCE_PROPOSAL_KIND,
            "operation": "save_memory",
            "legacy_response_key": STANDARD_PROPOSAL_LEGACY_KEYS[PREFERENCE_RESPONSE_KEY],
            "title": "Remember this preference?",
            "body": "This will guide future schedules.",
            "summary": summary,
            "rationale": rationale,
            "target_node_id": None,
            "target_node_ids": [],
            "target_node_title": "Preferences",
            "preview": collect_standard_proposal_preview(preference_payload),
            "validation_warnings": [
                item.strip()
                for item in preference_payload.get("validation_warnings", [])
                if isinstance(item, str) and item.strip()
            ],
            "preference_patch": preference_payload.get("preference_patch"),
            "preference_patch_validation": preference_payload.get("preference_patch_validation"),
            "preference_patch_simulation": preference_payload.get("preference_patch_simulation"),
            "preference_patch_status": status or preference_payload.get("preference_patch_status"),
            "payload": preference_payload,
        }

    return None


def build_task_patch_application_summary(
    task_patch: dict[str, Any],
    *,
    target_title: str | None = None,
) -> str:
    """Describe an applied task patch without exposing mutation internals."""

    operations = (
        task_patch.get("operations")
        if isinstance(task_patch.get("operations"), list)
        else []
    )
    counts = {
        "move": 0,
        "archive": 0,
        "delete": 0,
        "create": 0,
        "update": 0,
        "reorder": 0,
        "other": 0,
    }
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "").strip().lower()
        if op == "delete" and str(operation.get("mode") or "").strip().lower() == "archive":
            counts["archive"] += 1
        elif op in counts and op != "other":
            counts[op] += 1
        else:
            counts["other"] += 1

    is_workspace_cleanup = len(operations) > 1 and (
        counts["move"] + counts["archive"] + counts["delete"] > 0
    )
    if not is_workspace_cleanup:
        clean_target = str(target_title or "").strip()
        return f"Changes applied to {clean_target}." if clean_target else "Changes applied."

    details: list[str] = []
    if counts["move"]:
        count = counts["move"]
        details.append(f"moved {count} task group{'s' if count != 1 else ''}")
    if counts["archive"]:
        count = counts["archive"]
        details.append(f"archived {count} item{'s' if count != 1 else ''}")
    if counts["delete"]:
        count = counts["delete"]
        details.append(f"deleted {count} item{'s' if count != 1 else ''}")
    for key, verb in (("create", "created"), ("update", "updated"), ("reorder", "reordered")):
        count = counts[key]
        if count:
            details.append(f"{verb} {count} item{'s' if count != 1 else ''}")
    if counts["other"]:
        count = counts["other"]
        details.append(f"applied {count} other change{'s' if count != 1 else ''}")

    if not details:
        return "Workspace cleanup complete."
    if len(details) == 1:
        detail_text = details[0]
    else:
        detail_text = f"{', '.join(details[:-1])} and {details[-1]}"
    return f"Workspace cleanup complete: {detail_text}."


def _response_task_ids(value: Any) -> set[int]:
    ids: set[int] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in {
                "task_id",
                "node_id",
                "target_node_id",
                "destination_parent_id",
            } and isinstance(child, int):
                ids.add(int(child))
            elif normalized_key in {
                "task_ids",
                "node_ids",
                "target_node_ids",
                "working_set_task_ids",
                "affected_node_ids",
            } and isinstance(child, list):
                ids.update(int(item) for item in child if isinstance(item, int))
            ids.update(_response_task_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.update(_response_task_ids(child))
    return ids


def _response_entity_ids(value: Any, *, kind: str) -> set[str]:
    """Collect typed entity identities without inferring them from display text."""

    ids: set[str] = set()
    if isinstance(value, dict):
        entity_kind = str(value.get("kind") or "").strip().lower()
        entity_id = value.get("entity_id", value.get("entityId"))
        if entity_kind == kind and isinstance(entity_id, (str, int)):
            ids.add(str(entity_id).strip())
        for child in value.values():
            ids.update(_response_entity_ids(child, kind=kind))
    elif isinstance(value, list):
        for child in value:
            ids.update(_response_entity_ids(child, kind=kind))
    return ids


def _numeric_entity_id(value: str, *, prefix: str) -> int | None:
    normalized = value.strip()
    if normalized.startswith(f"{prefix}:"):
        normalized = normalized.split(":", 1)[1]
    try:
        return int(normalized)
    except (TypeError, ValueError):
        return None


def attach_response_task_references(
    session: Session,
    response_payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach metadata only for canonical task IDs present in the response contract."""

    nodes = list(session.exec(select(StrategyNode)).all())
    categories = list(session.exec(select(Category)).all())
    category_by_id = {
        int(category.id): category
        for category in categories
        if isinstance(category.id, int)
    }
    grounded_ids = response_payload.pop("_grounded_task_ids", [])
    referenced_ids = _response_task_ids(response_payload)
    referenced_ids.update(
        task_id
        for entity_id in _response_entity_ids(response_payload, kind="task")
        if (task_id := _numeric_entity_id(entity_id, prefix="task")) is not None
    )
    calendar_block_ids = {
        block_id
        for entity_id in _response_entity_ids(response_payload, kind="calendar_block")
        if (
            block_id := _numeric_entity_id(entity_id, prefix="calendar_block")
        ) is not None
    }
    if calendar_block_ids:
        referenced_ids.update(
            int(block.task_id)
            for block in session.exec(
                select(CalendarBlock).where(CalendarBlock.id.in_(calendar_block_ids))
            ).all()
        )
    if isinstance(grounded_ids, list):
        referenced_ids.update(
            int(task_id)
            for task_id in grounded_ids
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        )

    references: list[dict[str, Any]] = []
    for node in sorted(
        (node for node in nodes if node.id is not None and int(node.id) in referenced_ids),
        key=lambda item: (item.title.casefold(), int(item.id or 0)),
    ):
        category = category_by_id.get(int(node.category_id))
        references.append(
            {
                "task_id": int(node.id),
                "title": node.title,
                "emoji": node.emoji,
                "node_type": node.node_type,
                "category_id": int(node.category_id),
                "category_name": category.name if category is not None else None,
                "category_accent_color": (
                    category.accent_color if category is not None else None
                ),
            }
        )
    response_payload["task_references"] = references
    return response_payload


def _proposal_handoff_errors(proposal_payload: dict[str, Any]) -> list[str]:
    """Collect downstream validation failures before a proposal leaves the kernel."""

    errors: list[str] = []
    validation = proposal_payload.get("task_patch_validation")
    if isinstance(validation, dict):
        errors.extend(
            str(item).strip()
            for item in validation.get("errors", [])
            if isinstance(item, str) and item.strip()
        )
    for key in (
        "recurring_schedule_errors",
        "created_task_schedule_errors",
        "scheduled_duration_errors",
    ):
        errors.extend(
            str(item).strip()
            for item in proposal_payload.get(key, [])
            if isinstance(item, str) and item.strip()
        )
    return list(dict.fromkeys(errors))


def _proposal_attempts_in_response(
    response_payload: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    attempts: list[tuple[str, str, dict[str, Any]]] = []
    schedule_payload = response_payload.get("schedule_proposal")
    if isinstance(schedule_payload, dict):
        attempts.append(("schedule_proposal", "schedule_patch", schedule_payload))
    for response_key, proposal_kind in PROPOSAL_KIND_BY_RESPONSE_KEY.items():
        proposal_payload = response_payload.get(response_key)
        if isinstance(proposal_payload, dict):
            attempts.append((response_key, proposal_kind, proposal_payload))
    preference_payload = response_payload.get(PREFERENCE_RESPONSE_KEY)
    if isinstance(preference_payload, dict):
        attempts.append(
            (PREFERENCE_RESPONSE_KEY, PREFERENCE_PROPOSAL_KIND, preference_payload)
        )
    return attempts


def _apply_visible_proposal_handoff_failure(
    response_payload: dict[str, Any],
    failures: list[dict[str, Any]],
    *,
    change_set_id: str | None = None,
) -> None:
    existing = response_payload.get("proposal_handoff_failure")
    existing_failures = (
        list(existing.get("failures", []))
        if isinstance(existing, dict) and isinstance(existing.get("failures"), list)
        else []
    )
    outcome = classify_handoff_failures(failures)
    response_payload["proposal_handoff_failure"] = {
        "status": "invalid",
        "failures": [*existing_failures, *failures],
    }
    response_payload["handoff_outcome"] = {
        **outcome,
        "change_set_id": change_set_id,
    }
    response_payload["mode"] = (
        "clarification" if outcome["status"] == "needs_user_input" else "review"
    )
    response_payload["summary"] = str(outcome["message"])
    response_payload["clarification"] = clarification_for_handoff_outcome(outcome)


def _standard_proposal_projection_errors(
    standard_proposal: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(standard_proposal, dict):
        return ["The validated proposal could not be projected into the standard UI contract."]
    errors: list[str] = []
    if not str(standard_proposal.get("proposal_id") or "").strip():
        errors.append("The standard proposal is missing its stable proposal id.")
    proposal_kind = str(standard_proposal.get("proposal_kind") or "").strip()
    if proposal_kind == "schedule_patch":
        status = str(standard_proposal.get("schedule_patch_status") or "").strip().lower()
        schedule_patch = standard_proposal.get("schedule_patch")
        operations = (
            schedule_patch.get("operations")
            if isinstance(schedule_patch, dict)
            else None
        )
        if status in {"valid", "warning"} and not (
            isinstance(operations, list)
            and any(isinstance(item, dict) for item in operations)
        ):
            errors.append("An approvable schedule proposal has no calendar operations.")
    elif proposal_kind == PREFERENCE_PROPOSAL_KIND:
        preference_patch = standard_proposal.get("preference_patch")
        operations = (
            preference_patch.get("operations")
            if isinstance(preference_patch, dict)
            else None
        )
        if not (
            isinstance(operations, list)
            and any(isinstance(item, dict) for item in operations)
        ):
            errors.append("The standard preference proposal has no memory operations.")
    else:
        task_patch = standard_proposal.get("task_patch")
        operations = task_patch.get("operations") if isinstance(task_patch, dict) else None
        if not (
            isinstance(operations, list)
            and any(isinstance(item, dict) for item in operations)
        ):
            errors.append("The standard task proposal has no task operations.")
    return errors


def sync_response_proposals(
    session: Session,
    *,
    strategist_session_id: str,
    thread_id: str,
    response_payload: dict[str, Any],
    request_prompt: str | None = None,
    request_context_prompts: list[str] | None = None,
    local_date_time: datetime | None = None,
    source_turn_id: str | None = None,
    resume_change_set_id: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    keep_proposal_ids: set[str] = set()
    proposal_handoff_failures: list[dict[str, Any]] = []
    response_attempts = _proposal_attempts_in_response(response_payload)
    proposal_contract = response_payload.get("proposal_contract")
    dropped_contract_items = (
        proposal_contract.get("dropped", [])
        if isinstance(proposal_contract, dict)
        and isinstance(proposal_contract.get("dropped"), list)
        else []
    )
    if len(response_attempts) > 1 or dropped_contract_items:
        failure_reason = (
            "Multiple approval envelopes reached a response layer that requires one atomic proposal."
            if len(response_attempts) > 1
            else "Part of the requested mutation was lost during proposal canonicalization."
        )
        failures = [
            {
                "response_key": response_key,
                "proposal_kind": proposal_kind,
                "summary": str(proposal_payload.get("summary") or "").strip(),
                "errors": [failure_reason],
                "proposal": proposal_payload,
            }
            for response_key, proposal_kind, proposal_payload in response_attempts
        ]
        if dropped_contract_items:
            failures.append(
                {
                    "response_key": "proposal_contract",
                    "proposal_kind": "contract",
                    "summary": "Proposal canonicalization was incomplete.",
                    "errors": [failure_reason],
                    "dropped": dropped_contract_items,
                }
            )
        for response_key, _, _ in response_attempts:
            response_payload[response_key] = None
        response_payload.pop("proposal", None)
        outcome = classify_handoff_failures(failures)
        combined_payload = {
            "summary": str(response_payload.get("summary") or "Requested AI workspace change"),
            "proposal_attempts": [
                {**item[2], "proposal_kind": item[1]}
                for item in response_attempts
            ]
            + [
                {
                    **item["attempted_proposal"],
                    "proposal_kind": str(item.get("family") or "proposal"),
                }
                for item in dropped_contract_items
                if isinstance(item, dict)
                and isinstance(item.get("attempted_proposal"), dict)
            ],
        }
        change_set = persist_change_set(
            session,
            session_id=strategist_session_id,
            thread_id=thread_id,
            source_turn_id=source_turn_id,
            proposal_id=None,
            proposal_kind="contract",
            proposal_payload=combined_payload,
            outcome=str(outcome["status"]),
            failures=failures,
            change_set_id=resume_change_set_id,
            commit=True,
        )
        _apply_visible_proposal_handoff_failure(
            response_payload,
            failures,
            change_set_id=change_set.id,
        )
        attach_response_task_references(session, response_payload)
        return response_payload, []
    nodes = list(session.exec(select(StrategyNode)).all())
    category_ids = {
        int(category_id)
        for category_id in session.exec(select(Category.id)).all()
        if isinstance(category_id, int)
    }
    schedule_payload = response_payload.get("schedule_proposal")
    if isinstance(schedule_payload, dict):
        schedule_proposal_id = str(schedule_payload.get("proposalId") or "").strip() or str(uuid4())
        schedule_payload["proposalId"] = schedule_proposal_id
        schedule_validation = schedule_payload.get("schedulePatchValidation")
        schedule_simulation = schedule_payload.get("schedulePatchSimulation")
        schedule_change_set = persist_change_set(
            session,
            session_id=strategist_session_id,
            thread_id=thread_id,
            source_turn_id=source_turn_id,
            proposal_id=schedule_proposal_id,
            proposal_kind="schedule_patch",
            proposal_payload=schedule_payload,
            outcome="ready_for_approval",
            validation=(schedule_validation if isinstance(schedule_validation, dict) else {}),
            simulation=(schedule_simulation if isinstance(schedule_simulation, dict) else {}),
            change_set_id=resume_change_set_id,
        )
        schedule_payload["change_set_id"] = schedule_change_set.id
        schedule_payload["change_set_status"] = schedule_change_set.status
    for response_key, proposal_kind in PROPOSAL_KIND_BY_RESPONSE_KEY.items():
        proposal_payload = response_payload.get(response_key)
        if not isinstance(proposal_payload, dict):
            continue
        proposal_id = str(proposal_payload.get("proposal_id") or "").strip() or str(uuid4())
        proposal_payload["proposal_id"] = proposal_id
        enrich_proposal_payload_with_task_patch(
            proposal_kind=proposal_kind,
            proposal_payload=proposal_payload,
            nodes=nodes,
            category_ids=category_ids,
        )
        enrich_proposal_payload_with_created_task_schedule(
            session,
            proposal_payload=proposal_payload,
        )
        enrich_proposal_payload_with_recurring_schedule(
            session,
            proposal_payload=proposal_payload,
            request_prompt=request_prompt,
            request_context_prompts=request_context_prompts,
        )
        enrich_proposal_payload_with_scheduled_duration(
            session,
            proposal_payload=proposal_payload,
            local_date_time=local_date_time,
        )
        if isinstance(proposal_payload.get("recurring_schedule_plan"), dict):
            enrich_proposal_payload_with_task_patch(
                proposal_kind=proposal_kind,
                proposal_payload=proposal_payload,
                nodes=nodes,
                category_ids=category_ids,
            )
        validation = proposal_payload.get("task_patch_validation")
        validation_status = (
            str(validation.get("status") or "").strip().lower()
            if isinstance(validation, dict)
            else ""
        )
        if (
            validation_status == "invalid"
            or proposal_payload.get("recurring_schedule_status") == "invalid"
            or proposal_payload.get("created_task_schedule_status") == "invalid"
            or proposal_payload.get("scheduled_duration_status") == "invalid"
        ):
            handoff_errors = _proposal_handoff_errors(proposal_payload)
            proposal_payload["proposal_id"] = None
            proposal_payload["approval_blocked_reason"] = (
                "This task and schedule proposal needs a correction before it can be approved."
            )
            proposal_handoff_failures.append(
                {
                    "response_key": response_key,
                    "proposal_kind": proposal_kind,
                    "summary": str(proposal_payload.get("summary") or "").strip(),
                    "errors": handoff_errors,
                    "proposal": proposal_payload,
                }
            )
            outcome = classify_handoff_failures([proposal_handoff_failures[-1]])
            change_set = persist_change_set(
                session,
                session_id=strategist_session_id,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
                proposal_id=None,
                proposal_kind=proposal_kind,
                proposal_payload=proposal_payload,
                outcome=str(outcome["status"]),
                validation=(validation if isinstance(validation, dict) else {}),
                simulation=(
                    proposal_payload.get("task_patch_simulation")
                    if isinstance(proposal_payload.get("task_patch_simulation"), dict)
                    else {}
                ),
                failures=[proposal_handoff_failures[-1]],
                change_set_id=resume_change_set_id,
                commit=True,
            )
            proposal_handoff_failures[-1]["change_set_id"] = change_set.id
            response_payload[response_key] = None
            continue
        target_node_id = (
            proposal_payload.get("target_node_id")
            if isinstance(proposal_payload.get("target_node_id"), int)
            else None
        )
        target_node_ids = collect_proposal_target_node_ids(proposal_payload)
        change_set = persist_change_set(
            session,
            session_id=strategist_session_id,
            thread_id=thread_id,
            source_turn_id=source_turn_id,
            proposal_id=proposal_id,
            proposal_kind=proposal_kind,
            proposal_payload=proposal_payload,
            outcome="ready_for_approval",
            validation=(validation if isinstance(validation, dict) else {}),
            simulation=(
                proposal_payload.get("task_patch_simulation")
                if isinstance(proposal_payload.get("task_patch_simulation"), dict)
                else {}
            ),
            change_set_id=resume_change_set_id,
        )
        proposal_payload["change_set_id"] = change_set.id
        proposal_payload["change_set_status"] = change_set.status
        existing = session.get(StrategistProposalRecord, proposal_id)
        record = existing or StrategistProposalRecord(id=proposal_id, session_id=strategist_session_id)
        record.thread_id = thread_id
        record.source_turn_id = source_turn_id
        record.change_set_id = change_set.id
        record.proposal_kind = proposal_kind
        record.status = "pending"
        record.target_node_id = target_node_id
        record.target_node_ids_json = _json_dumps(target_node_ids)
        record.payload_json = _json_dumps(proposal_payload)
        record.updated_at = datetime.utcnow()
        session.add(record)
        keep_proposal_ids.add(proposal_id)

    preference_payload = response_payload.get(PREFERENCE_RESPONSE_KEY)
    if isinstance(preference_payload, dict):
        proposal_id = str(preference_payload.get("proposal_id") or "").strip() or str(uuid4())
        preference_payload["proposal_id"] = proposal_id
        preference_patch = preference_payload.get("preference_patch")
        if not isinstance(preference_patch, dict):
            preference_patch = {
                "patchId": f"preference-patch-{uuid4()}",
                "patchVersion": "preference-patch-v1",
                "intentSummary": str(preference_payload.get("summary") or ""),
                "operations": [],
            }
        validation = validate_preference_patch(preference_patch)
        simulation = simulate_preference_patch(preference_patch)
        preference_payload["preference_patch"] = preference_patch
        preference_payload["preference_patch_validation"] = validation
        preference_payload["preference_patch_simulation"] = simulation
        preference_payload["preference_patch_status"] = validation["status"]
        validation_warnings = [
            item
            for item in preference_payload.get("validation_warnings", [])
            if isinstance(item, str) and item.strip()
        ]
        for warning in validation["warnings"]:
            if warning not in validation_warnings:
                validation_warnings.append(warning)
        preference_payload["validation_warnings"] = validation_warnings
        if validation["status"] == "invalid":
            preference_payload["proposal_id"] = None
            preference_payload["approval_blocked_reason"] = (
                "PreferencePatch validation failed, so this memory update was not persisted for approval."
            )
            proposal_handoff_failures.append(
                {
                    "response_key": PREFERENCE_RESPONSE_KEY,
                    "proposal_kind": PREFERENCE_PROPOSAL_KIND,
                    "summary": str(preference_payload.get("summary") or "").strip(),
                    "errors": [
                        str(item).strip()
                        for item in validation.get("errors", [])
                        if isinstance(item, str) and item.strip()
                    ],
                    "proposal": preference_payload,
                }
            )
            outcome = classify_handoff_failures([proposal_handoff_failures[-1]])
            preference_change_set = persist_change_set(
                session,
                session_id=strategist_session_id,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
                proposal_id=None,
                proposal_kind=PREFERENCE_PROPOSAL_KIND,
                proposal_payload=preference_payload,
                outcome=str(outcome["status"]),
                validation=validation,
                simulation=simulation,
                failures=[proposal_handoff_failures[-1]],
                change_set_id=resume_change_set_id,
                commit=True,
            )
            proposal_handoff_failures[-1]["change_set_id"] = preference_change_set.id
            response_payload[PREFERENCE_RESPONSE_KEY] = None
        else:
            preference_change_set = persist_change_set(
                session,
                session_id=strategist_session_id,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
                proposal_id=proposal_id,
                proposal_kind=PREFERENCE_PROPOSAL_KIND,
                proposal_payload=preference_payload,
                outcome="ready_for_approval",
                validation=validation,
                simulation=simulation,
                change_set_id=resume_change_set_id,
            )
            preference_payload["change_set_id"] = preference_change_set.id
            preference_payload["change_set_status"] = preference_change_set.status
            existing = session.get(StrategistProposalRecord, proposal_id)
            record = existing or StrategistProposalRecord(
                id=proposal_id,
                session_id=strategist_session_id,
            )
            record.thread_id = thread_id
            record.source_turn_id = source_turn_id
            record.change_set_id = preference_change_set.id
            record.proposal_kind = PREFERENCE_PROPOSAL_KIND
            record.status = "pending"
            record.target_node_id = None
            record.target_node_ids_json = _json_dumps([])
            record.payload_json = _json_dumps(preference_payload)
            record.updated_at = datetime.utcnow()
            session.add(record)
            keep_proposal_ids.add(proposal_id)

    if proposal_handoff_failures:
        # Never turn an approval-producing model action into an ordinary-looking
        # assistant message. Keep the rejected handoff for diagnostics and give
        # the client a visible, structured failure state instead of silently
        # dropping the proposal on the floor.
        has_reviewable_mutation = any(
            isinstance(response_payload.get(response_key), dict)
            for response_key in PROPOSAL_KIND_BY_RESPONSE_KEY
        ) or isinstance(response_payload.get("schedule_proposal"), dict)
        if not has_reviewable_mutation:
            failure_change_set_id = next(
                (
                    str(item.get("change_set_id"))
                    for item in proposal_handoff_failures
                    if item.get("change_set_id")
                ),
                None,
            )
            _apply_visible_proposal_handoff_failure(
                response_payload,
                proposal_handoff_failures,
                change_set_id=failure_change_set_id,
            )

    standard_proposal = build_standard_response_proposal(response_payload)
    remaining_attempts = _proposal_attempts_in_response(response_payload)
    projection_errors = (
        _standard_proposal_projection_errors(standard_proposal)
        if remaining_attempts
        else []
    )
    if projection_errors:
        session.rollback()
        keep_proposal_ids.clear()
        projection_failures = [
            {
                "response_key": response_key,
                "proposal_kind": proposal_kind,
                "summary": str(proposal_payload.get("summary") or "").strip(),
                "errors": projection_errors,
                "proposal": proposal_payload,
            }
            for response_key, proposal_kind, proposal_payload in remaining_attempts
        ]
        for response_key, _, _ in remaining_attempts:
            response_payload[response_key] = None
        response_payload.pop("proposal", None)
        outcome = classify_handoff_failures(projection_failures)
        projection_change_set = persist_change_set(
            session,
            session_id=strategist_session_id,
            thread_id=thread_id,
            source_turn_id=source_turn_id,
            proposal_id=None,
            proposal_kind="projection",
            proposal_payload={
                "summary": str(response_payload.get("summary") or "Requested AI workspace change"),
                "proposal_attempts": [
                    {**item[2], "proposal_kind": item[1]}
                    for item in remaining_attempts
                ],
            },
            outcome=str(outcome["status"]),
            failures=projection_failures,
            change_set_id=resume_change_set_id,
            commit=True,
        )
        _apply_visible_proposal_handoff_failure(
            response_payload,
            projection_failures,
            change_set_id=projection_change_set.id,
        )
    elif standard_proposal is not None:
        response_payload["proposal"] = standard_proposal
    else:
        response_payload.pop("proposal", None)

    if keep_proposal_ids:
        supersede_other_pending_proposals(
            session,
            strategist_session_id=strategist_session_id,
            keep_proposal_ids=keep_proposal_ids,
        )
        session.commit()

    attach_response_task_references(session, response_payload)

    return response_payload, sorted(keep_proposal_ids)


def bind_proposal_turn_ids(
    session: Session,
    *,
    proposal_ids: list[str],
    turn_id: str,
) -> None:
    if not proposal_ids:
        return
    records = list(
        session.exec(
            select(StrategistProposalRecord).where(StrategistProposalRecord.id.in_(proposal_ids))
        ).all()
    )
    for record in records:
        record.source_turn_id = turn_id
        record.updated_at = datetime.utcnow()
        session.add(record)
        if record.change_set_id:
            change_set = update_change_set_status(
                session,
                change_set_id=record.change_set_id,
                status="pending_approval",
            )
            if change_set is not None:
                change_set.source_turn_id = turn_id
                session.add(change_set)
    session.commit()


def build_stale_proposal_response(
    *,
    proposal_record: StrategistProposalRecord | None,
    provider_label: str,
    context_cards: list[dict[str, Any]],
    suggested_prompts: list[str],
) -> dict[str, Any]:
    proposal_payload = (
        _json_loads_dict(proposal_record.payload_json) if proposal_record is not None else {}
    )
    target_title = str(
        proposal_payload.get("target_node_title") or "that proposal"
    ).strip() or "that proposal"
    status = str(proposal_record.status if proposal_record is not None else "missing")
    return {
        "provider_label": provider_label,
        "mode": "review",
        "summary": (
            f"The proposal for {target_title} is no longer pending "
            f"(status: {status}). Ask Strategist to prepare a fresh change if you still want to act on it."
        ),
        "context_cards": context_cards,
        "suggested_prompts": suggested_prompts,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
    }


def handle_proposal_action(
    session: Session,
    *,
    proposal_record: StrategistProposalRecord | None,
    action: str,
    active_category_id: int | None,
    focus_node_id: int | None,
    staged_node_ids: list[int],
    provider_label: str,
) -> dict[str, Any]:
    fallback_title = None
    if proposal_record is not None:
        fallback_title = str(
            _json_loads_dict(proposal_record.payload_json).get("target_node_title") or ""
        ).strip() or None
    resolved_focus_node_id = focus_node_id
    if resolved_focus_node_id is None and proposal_record is not None:
        resolved_focus_node_id = proposal_record.target_node_id
    context_cards, suggested_prompts = build_proposal_response_context(
        session=session,
        active_category_id=active_category_id,
        focus_node_id=resolved_focus_node_id,
        staged_node_ids=staged_node_ids,
        fallback_title=fallback_title,
    )

    if proposal_record is None or proposal_record.status != "pending":
        return {
            "response": build_stale_proposal_response(
                proposal_record=proposal_record,
                provider_label=provider_label,
                context_cards=context_cards,
                suggested_prompts=suggested_prompts,
            ),
            "memory_entities": [],
            "proposal_ids": [],
        }

    proposal_payload = _json_loads_dict(proposal_record.payload_json)
    pending_payload = {
        "kind": proposal_record.proposal_kind,
        "proposal": proposal_payload,
        "response": {
            "action_proposal": proposal_payload if proposal_record.proposal_kind == "action" else None,
            "metadata_proposal": proposal_payload if proposal_record.proposal_kind == "metadata" else None,
            "branch_proposal": proposal_payload if proposal_record.proposal_kind == "branch" else None,
            PREFERENCE_RESPONSE_KEY: (
                proposal_payload
                if proposal_record.proposal_kind == PREFERENCE_PROPOSAL_KIND
                else None
            ),
        },
    }

    if action in {"reject", "edit"}:
        response_payload = build_pending_proposal_response(
            provider_label=provider_label,
            pending_proposal=pending_payload,
            action=action,
            context_cards=context_cards,
            suggested_prompts=suggested_prompts,
        )
        if action == "edit" and proposal_record.proposal_kind == PREFERENCE_PROPOSAL_KIND:
            response_payload["mode"] = "review"
            response_payload["summary"] = (
                "I still have that memory update in context. Tell me what to change, "
                "or approve/reject the current version."
            )
            response_payload[PREFERENCE_RESPONSE_KEY] = proposal_payload
            standard_proposal = build_standard_response_proposal(response_payload)
            if standard_proposal is not None:
                response_payload["proposal"] = standard_proposal
        if action == "reject":
            proposal_record.status = "rejected"
            proposal_record.result_json = _json_dumps(
                {
                    "action": action,
                    "summary": response_payload["summary"],
                }
            )
            proposal_record.updated_at = datetime.utcnow()
            session.add(proposal_record)
            update_change_set_status(
                session,
                change_set_id=proposal_record.change_set_id,
                status="rejected",
                outcome="rejected",
            )
            session.commit()
        return {
            "response": response_payload,
            "memory_entities": [],
            "proposal_ids": [proposal_record.id] if action == "edit" else [],
        }

    if proposal_record.proposal_kind == PREFERENCE_PROPOSAL_KIND:
        preference_patch = proposal_payload.get("preference_patch")
        if not isinstance(preference_patch, dict):
            summary = (
                "That memory proposal is missing its PreferencePatch, so I did not save it. "
                "Ask Strategist to prepare a fresh memory update."
            )
            response_payload = {
                "provider_label": provider_label,
                "mode": "review",
                "summary": summary,
                "context_cards": context_cards,
                "suggested_prompts": [
                    "Remember I do not schedule work before 10am",
                    "Remember I prefer deep work in the morning",
                    "Review my workload across categories",
                ],
                "action_proposal": None,
                "metadata_proposal": None,
                "branch_proposal": None,
                "clarification": None,
            }
            proposal_record.status = "superseded"
            proposal_record.result_json = _json_dumps(
                {
                    "action": action,
                    "summary": summary,
                    "missing_preference_patch": True,
                }
            )
            proposal_record.updated_at = datetime.utcnow()
            session.add(proposal_record)
            update_change_set_status(
                session,
                change_set_id=proposal_record.change_set_id,
                status="superseded",
                outcome="superseded",
            )
            session.commit()
            return {
                "response": response_payload,
                "memory_entities": [],
                "proposal_ids": [],
            }

        validation = validate_preference_patch(preference_patch)
        if validation.get("status") == "invalid":
            summary = (
                "That memory proposal is no longer approvable because its PreferencePatch is invalid. "
                "I did not save any preference memory."
            )
            response_payload = {
                "provider_label": provider_label,
                "mode": "review",
                "summary": summary,
                "context_cards": context_cards,
                "suggested_prompts": [
                    "Prepare a fresh memory proposal",
                    "Remember I prefer deep work in the morning",
                    "Remember I do not schedule work before 10am",
                ],
                "action_proposal": None,
                "metadata_proposal": None,
                "branch_proposal": None,
                "clarification": None,
            }
            proposal_record.status = "superseded"
            proposal_record.result_json = _json_dumps(
                {
                    "action": action,
                    "summary": summary,
                    "invalid_preference_patch": True,
                    "validation": validation,
                }
            )
            proposal_record.updated_at = datetime.utcnow()
            session.add(proposal_record)
            update_change_set_status(
                session,
                change_set_id=proposal_record.change_set_id,
                status="superseded",
                outcome="superseded",
            )
            session.commit()
            return {
                "response": response_payload,
                "memory_entities": [],
                "proposal_ids": [],
            }

        patch_result = apply_preference_patch(
            session,
            preference_patch=preference_patch,
            actor="user_approved",
        )
        created_count = len(patch_result.get("createdPreferenceIds", [])) + len(
            patch_result.get("createdConstraintIds", [])
        )
        summary = (
            f"Saved {created_count} scheduling memory record"
            f"{'' if created_count == 1 else 's'}. Future schedule proposals can retrieve it when relevant."
        )
        response_payload = {
            "provider_label": provider_label,
            "mode": "review",
            "summary": summary,
            "context_cards": context_cards,
            "suggested_prompts": [
                "Schedule selected tasks with my preferences",
                "Show me what scheduling preferences you remember",
                "Remember another scheduling preference",
            ],
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
        }
        proposal_record.status = "approved"
        proposal_record.result_json = _json_dumps(
            {
                "action": action,
                "summary": summary,
                "preference_patch": preference_patch,
                "preference_patch_validation": validation,
                **patch_result,
            }
        )
        proposal_record.updated_at = datetime.utcnow()
        session.add(proposal_record)
        update_change_set_status(
            session,
            change_set_id=proposal_record.change_set_id,
            status="applied",
            outcome="applied",
        )
        session.commit()
        return {
            "response": response_payload,
            "memory_entities": [],
            "proposal_ids": [],
        }

    task_patch = proposal_payload.get("task_patch")
    if not isinstance(task_patch, dict):
        summary = (
            "That saved proposal is outdated, so I did not apply it. "
            "Please prepare a fresh review."
        )
        response_payload = {
            "provider_label": provider_label,
            "mode": "review",
            "summary": summary,
            "context_cards": context_cards,
            "suggested_prompts": [
                "Prepare a fresh proposal",
                "Review my workload across categories",
                "Find tasks with unclear metadata",
            ],
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
        }
        proposal_record.status = "superseded"
        proposal_record.result_json = _json_dumps(
            {
                "action": action,
                "summary": summary,
                "missing_task_patch": True,
            }
        )
        proposal_record.updated_at = datetime.utcnow()
        session.add(proposal_record)
        update_change_set_status(
            session,
            change_set_id=proposal_record.change_set_id,
            status="superseded",
            outcome="superseded",
        )
        session.commit()
        return {
            "response": response_payload,
            "memory_entities": [],
            "proposal_ids": [],
        }

    current_nodes = list(session.exec(select(StrategyNode)).all())
    current_category_ids = {
        int(category_id)
        for category_id in session.exec(select(Category.id)).all()
        if isinstance(category_id, int)
    }
    validation = validate_task_patch(
        task_patch,
        current_nodes,
        category_ids=current_category_ids,
    )
    if validation.get("status") == "invalid":
        summary = (
            "The tasks changed after this review was prepared, so I did not apply anything. "
            "Please prepare a fresh review."
        )
        response_payload = {
            "provider_label": provider_label,
            "mode": "review",
            "summary": summary,
            "context_cards": context_cards,
            "suggested_prompts": [
                "Prepare a fresh proposal",
                "Review my workload across categories",
                "Find tasks with unclear metadata",
            ],
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "clarification": None,
        }
        proposal_record.status = "superseded"
        proposal_record.result_json = _json_dumps(
            {
                "action": action,
                "summary": summary,
                "invalid_task_patch": True,
                "validation": validation,
            }
        )
        proposal_record.updated_at = datetime.utcnow()
        session.add(proposal_record)
        update_change_set_status(
            session,
            change_set_id=proposal_record.change_set_id,
            status="superseded",
            outcome="superseded",
        )
        session.commit()
        return {
            "response": response_payload,
            "memory_entities": [],
            "proposal_ids": [],
        }

    memory_entities: list[dict[str, Any]] = []
    target_node_ids = collect_proposal_target_node_ids(proposal_payload)
    pre_apply_target_nodes = [
        node
        for node_id in target_node_ids
        for node in [session.get(StrategyNode, node_id)]
        if node is not None and node.id is not None
    ]
    recurring_plan = proposal_payload.get("recurring_schedule_plan")
    has_recurring_schedule = isinstance(recurring_plan, dict)
    created_task_plan = proposal_payload.get("created_task_schedule_plan")
    has_created_task_schedule = isinstance(created_task_plan, dict)
    scheduled_duration_patch = proposal_payload.get("scheduled_duration_patch")
    has_scheduled_duration_update = isinstance(scheduled_duration_patch, dict)
    if has_recurring_schedule and recurring_plan.get("status") != "valid":
        raise ValueError("This recurring schedule proposal is no longer valid. Prepare a fresh proposal.")
    if has_created_task_schedule and created_task_plan.get("status") != "valid":
        raise ValueError("This task and schedule proposal is no longer valid. Prepare a fresh proposal.")
    patch_result = apply_task_patch(
        session,
        task_patch=task_patch,
        commit=not (
            has_recurring_schedule
            or has_created_task_schedule
            or has_scheduled_duration_update
        ),
    )
    affected_nodes = [
        node
        for node in patch_result.get("affected_nodes", [])
        if isinstance(node, StrategyNode) and node.id is not None
    ]
    memory_source = f"task_patch_{proposal_record.proposal_kind}_apply"
    memory_seed_nodes = affected_nodes or pre_apply_target_nodes
    for index, node in enumerate(memory_seed_nodes):
        memory_entities.append(
            {
                "node_id": node.id,
                "title": node.title,
                "role": "primary" if index == 0 else "related",
                "confidence": 0.99,
                "source": memory_source,
                "mention_text": node.title,
            }
        )
    deleted_node_ids = [
        node_id
        for node_id in patch_result.get("deleted_node_ids", [])
        if isinstance(node_id, int)
    ]
    recurring_execution = None
    recurring_blocks: list[Any] = []
    recurring_updated_blocks: list[Any] = []
    created_task_blocks: list[Any] = []
    resized_blocks: list[Any] = []
    if has_recurring_schedule:
        assert isinstance(recurring_plan, dict)
        target_temp_id = str(recurring_plan.get("target_temp_id") or "").strip()
        temp_node_ids = patch_result.get("temp_node_ids")
        recurring_task_id = (
            temp_node_ids.get(target_temp_id)
            if isinstance(temp_node_ids, dict) and target_temp_id
            else None
        )
        if not isinstance(recurring_task_id, int):
            created_nodes = patch_result.get("created_nodes")
            if isinstance(created_nodes, list) and len(created_nodes) == 1:
                recurring_task_id = created_nodes[0].id
        recurring_task = (
            session.get(StrategyNode, recurring_task_id)
            if isinstance(recurring_task_id, int)
            else None
        )
        if recurring_task is None or recurring_task.id is None:
            raise ValueError("The recurring schedule could not resolve its newly created task.")
        recurrence_series = create_recurrence_series_from_plan(
            session,
            plan=recurring_plan,
            task_id=int(recurring_task.id),
            category_id=int(recurring_task.category_id),
            source_change_set_id=proposal_record.change_set_id,
        )
        schedule_patch = schedule_patch_from_recurring_plan(
            recurring_plan,
            task_id=int(recurring_task.id),
            category_id=int(recurring_task.category_id),
            task_title=recurring_task.title,
            task_emoji=recurring_task.emoji,
            recurrence_series_id=recurrence_series.id,
        )
        recurring_execution = apply_approved_schedule_patch(session, schedule_patch)
        recurring_blocks = list(recurring_execution.created_blocks)
        recurring_updated_blocks = list(recurring_execution.updated_blocks)
        session.commit()
        for node in affected_nodes:
            session.refresh(node)
        for block in recurring_blocks:
            session.refresh(block)
        for block in recurring_updated_blocks:
            session.refresh(block)
    elif has_created_task_schedule:
        assert isinstance(created_task_plan, dict)
        temp_node_ids = patch_result.get("temp_node_ids")
        if not isinstance(temp_node_ids, dict):
            raise ValueError("The newly created tasks could not be resolved for scheduling.")
        created_schedule_patch = schedule_patch_from_created_task_schedule_plan(
            session,
            plan=created_task_plan,
            temp_node_ids=temp_node_ids,
        )
        created_execution = apply_approved_schedule_patch(session, created_schedule_patch)
        created_task_blocks = list(created_execution.created_blocks)
        session.commit()
        for node in affected_nodes:
            session.refresh(node)
        for block in created_task_blocks:
            session.refresh(block)
    elif has_scheduled_duration_update:
        assert isinstance(scheduled_duration_patch, dict)
        duration_execution = apply_approved_schedule_patch(session, scheduled_duration_patch)
        resized_blocks = list(duration_execution.updated_blocks)
        session.commit()
        for node in affected_nodes:
            session.refresh(node)
        for block in resized_blocks:
            session.refresh(block)
    result_payload_extra = {
        "task_patch": task_patch,
        "task_patch_validation": proposal_payload.get("task_patch_validation"),
        "task_patch_simulation": proposal_payload.get("task_patch_simulation"),
        "affected_node_ids": [
            node.id for node in affected_nodes if isinstance(node.id, int)
        ],
        "deleted_node_ids": deleted_node_ids,
        "created_block_ids": [
            block.id
            for block in [*recurring_blocks, *created_task_blocks]
            if isinstance(block.id, int)
        ],
        "updated_block_ids": [
            block.id
            for block in [*recurring_updated_blocks, *resized_blocks]
            if isinstance(block.id, int)
        ],
    }
    if has_recurring_schedule:
        recurring_task_title = (
            affected_nodes[0].title if affected_nodes else "the recurring task"
        )
        summary = (
            f"Created {recurring_task_title} and added "
            f"{len(recurring_blocks)} calendar blocks"
            + (
                f", moving {len(recurring_updated_blocks)} lower-priority conflict"
                f"{'' if len(recurring_updated_blocks) == 1 else 's'} to nearby openings"
                if recurring_updated_blocks
                else ""
            )
            + "."
        )
    elif has_created_task_schedule:
        summary = (
            f"Created {len(affected_nodes)} task{'' if len(affected_nodes) == 1 else 's'} "
            f"and added {len(created_task_blocks)} calendar block"
            f"{'' if len(created_task_blocks) == 1 else 's'}."
        )
    elif has_scheduled_duration_update:
        target_label = (
            affected_nodes[0].title if len(affected_nodes) == 1 else "the selected tasks"
        )
        duration_minutes = int(proposal_payload.get("scheduled_duration_minutes") or 0)
        summary = (
            f"Updated {target_label} to {duration_minutes} minutes and resized "
            f"{len(resized_blocks)} upcoming calendar block"
            f"{'' if len(resized_blocks) == 1 else 's'}."
        )
    elif proposal_record.proposal_kind == "metadata":
        target_label = (
            affected_nodes[0].title
            if len(affected_nodes) == 1
            else f"{len(affected_nodes)} tasks"
        )
        summary = f"Updated {target_label}."
    elif proposal_record.proposal_kind == "action":
        if pre_apply_target_nodes:
            target_label = ", ".join(node.title for node in pre_apply_target_nodes[:3])
            if len(pre_apply_target_nodes) > 3:
                target_label = f"{target_label}, and {len(pre_apply_target_nodes) - 3} more"
        else:
            target_label = f"{len(deleted_node_ids)} tasks"
        summary = f"Deleted {target_label}."
    else:
        target_title = str(proposal_payload.get("target_node_title") or "that task").strip() or "that task"
        summary = build_task_patch_application_summary(
            task_patch,
            target_title=target_title,
        )
    first_node = (
        affected_nodes[0]
        if affected_nodes
        else (pre_apply_target_nodes[0] if pre_apply_target_nodes else None)
    )
    response_payload = {
        "provider_label": provider_label,
        "mode": "review",
        "summary": summary,
        "context_cards": context_cards,
        "suggested_prompts": (
            build_suggested_prompts(first_node)
            if first_node is not None
            else [
                "Review my workload across categories",
                "Find tasks with unclear metadata",
                "What should I focus on next?",
            ]
        ),
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "clarification": None,
    }
    if has_recurring_schedule or has_created_task_schedule:
        applied_blocks = recurring_blocks or created_task_blocks
        response_payload["actionResult"] = {
            "proposalId": proposal_record.id,
            "status": "applied",
            "idempotent": False,
            "createdBlockIds": [
                block.id for block in applied_blocks if isinstance(block.id, int)
            ],
            "updatedBlockIds": [
                block.id
                for block in recurring_updated_blocks
                if isinstance(block.id, int)
            ],
        }

    proposal_record.status = "approved"
    proposal_record.result_json = _json_dumps(
        {
            "action": action,
            "summary": response_payload["summary"],
            **result_payload_extra,
        }
    )
    proposal_record.updated_at = datetime.utcnow()
    session.add(proposal_record)
    update_change_set_status(
        session,
        change_set_id=proposal_record.change_set_id,
        status="applied",
        outcome="applied",
    )
    session.commit()
    return {
        "response": response_payload,
        "memory_entities": memory_entities,
        "proposal_ids": [],
        "sync_created_or_updated": (
            [*recurring_blocks, *recurring_updated_blocks]
            or created_task_blocks
            or resized_blocks
        ),
    }


def build_proposal_action_diagnostics(
    *,
    response_payload: dict[str, Any],
    memory_entities: list[dict[str, Any]],
    action: str,
    today_value: date | None = None,
) -> dict[str, Any]:
    resolved_ids = [
        entity["node_id"]
        for entity in memory_entities
        if isinstance(entity, dict) and isinstance(entity.get("node_id"), int)
    ]
    graph_evidence = [
        {
            "source": "proposal_record",
            "detail": f"Applied pending proposal action '{action}' for {entity.get('title')}.",
            "task_id": entity.get("node_id"),
            "task_title": entity.get("title"),
        }
        for entity in memory_entities
        if isinstance(entity, dict)
    ]
    return {
        "mode": str(response_payload.get("mode") or "review"),
        "today": (today_value or date.today()).isoformat(),
        "control_plane": "graph",
        "intent_candidates": [
            {
                "intent": "confirm_proposal",
                "mode": str(response_payload.get("mode") or "review"),
                "score": 1.0,
                "reasons": ["resolved against pending proposal state"],
            }
        ],
        "conversation_first": False,
        "focus_binding": {
            "input_focus_node_id": None,
            "bound_as_target": False,
            "selected_node_id": resolved_ids[0] if resolved_ids else None,
            "working_set_used": False,
        },
        "retrieval_confidence": {
            "overall": 0.99 if resolved_ids else 0.5,
            "ambiguous": False,
            "confidence_gap": None,
            "has_entity_reference": bool(resolved_ids),
        },
        "policy_decision": {
            "policy": "answer_direct",
            "reason": "Resolved against pending proposal context.",
            "allow_provider": False,
        },
        "retrieval_plan": {
            "purpose": "proposal_action",
            "scope_hints": ["pending_proposal"],
            "required_evidence": ["proposal_record"],
            "must_resolve_exact_entity": False,
            "allow_hypothetical": False,
            "stop_rule": "proposal_action_applied",
        },
        "evidence_bundle": {
            "resolution": "resolved_entity" if resolved_ids else "no_match",
            "resolved_entity_ids": resolved_ids,
            "candidate_entity_ids": resolved_ids,
            "relevant_subtree_root_ids": resolved_ids,
            "note_evidence": [],
            "graph_evidence": graph_evidence,
            "recent_action_evidence": [],
            "confidence": 0.99 if resolved_ids else 0.5,
            "confidence_reasons": ["pending proposal carried hard target ids"],
            "unresolved_gaps": [],
        },
        "resolved_entities": memory_entities,
        "resolved_focus_node_id": resolved_ids[0] if resolved_ids else None,
        "resolved_focus_node_title": (
            str(memory_entities[0].get("title"))
            if memory_entities and isinstance(memory_entities[0], dict)
            else None
        ),
        "tool_calls": [],
        "tool_trace": [],
        "policy": "answer_direct",
    }


def build_graph(
    *,
    session: Session,
    include_diagnostics: bool,
) -> Any:
    raise HTTPException(
        status_code=410,
        detail="The legacy Strategist graph runtime has been retired. Use the agent-kernel harness.",
    )

def run_strategist_graph_turn(
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
    control_plane: str | None = None,
    requested_capability_ids: list[str] | None = None,
    workspace_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph_thread = ensure_graph_thread(
        session,
        strategist_session_id=strategist_session_id,
    )
    ui_event_kind = (
        str(ui_event.get("kind") or "").strip().lower()
        if isinstance(ui_event, dict)
        else None
    )
    if strategist_agent_kernel_enabled(control_plane):
        runtime_settings = get_provider_runtime_settings(provider_override, model_override)
        kernel_runner = run_agent_kernel_turn
        if ui_event_kind == "proposal_action":
            raise HTTPException(
                status_code=410,
                detail=(
                    "This older proposal card has been retired. Submit the "
                    "request again to create a current review."
                ),
            )
        effective_prompt = prompt
        effective_focus_node_id = focus_node_id
        resuming_change_set_id: str | None = None
        if ui_event_kind == "resume_change_set":
            change_set_id = str(
                ui_event.get("change_set_id")
                or ui_event.get("changeSetId")
                or ""
            ).strip()
            change_set = (
                mark_change_set_resumed(
                    session,
                    change_set_id,
                    session_id=strategist_session_id,
                )
                if change_set_id
                else None
            )
            if change_set is None:
                return {
                    "response": {
                        "provider_label": str(runtime_settings.get("provider_label") or ""),
                        "mode": "review",
                        "summary": "That saved draft is no longer available. Please restate the change you want.",
                        "context_cards": [],
                        "suggested_prompts": [],
                        "clarification": None,
                        "handoff_outcome": {
                            "status": "system_failure",
                            "message": "The saved draft could not be resumed.",
                            "change_set_id": change_set_id or None,
                        },
                    },
                    "memory_entities": [],
                    "proposal_ids": [],
                }
            resume_context = change_set_resume_context(change_set)
            resuming_change_set_id = change_set.id
            if change_set.resume_count > 3:
                update_change_set_status(
                    session,
                    change_set_id=change_set.id,
                    status="failed",
                    outcome="system_failure",
                    commit=True,
                )
                return {
                    "response": {
                        "provider_label": str(runtime_settings.get("provider_label") or ""),
                        "mode": "review",
                        "summary": "I still couldn’t prepare this review safely. No changes were applied.",
                        "context_cards": [],
                        "suggested_prompts": [],
                        "clarification": None,
                        "handoff_outcome": {
                            "status": "system_failure",
                            "message": "The saved draft needs a new request before it can continue.",
                            "change_set_id": change_set.id,
                        },
                    },
                    "memory_entities": [],
                    "proposal_ids": [],
                }
            effective_prompt = (
                "Resume and repair the saved mutation goal below. Preserve every resolved "
                "operation and constraint. Return exactly one complete typed approval proposal; "
                "ask the user only if the saved validation identifies a genuine unresolved choice.\n\n"
                f"Saved ChangeSet JSON:\n{json.dumps(resume_context, ensure_ascii=True)}"
            )
        if ui_event_kind == "clarification_choice":
            payload = ui_event.get("payload") if isinstance(ui_event.get("payload"), dict) else {}
            typed_goal_id = str(
                ui_event.get("goal_id") or ui_event.get("goalId") or ""
            ).strip()
            typed_decision_id = str(
                ui_event.get("decision_id") or ui_event.get("decisionId") or ""
            ).strip()
            typed_option_id = str(
                ui_event.get("option_id") or ui_event.get("optionId") or ""
            ).strip()
            typed_event_id = str(
                ui_event.get("event_id") or ui_event.get("eventId") or ""
            ).strip()
            typed_revision = ui_event.get("goal_revision") or ui_event.get("goalRevision")
            if (
                typed_goal_id
                and typed_decision_id
                and typed_option_id
                and typed_event_id
                and isinstance(typed_revision, int)
            ):
                from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator
                from app.services.agent_kernel.goal_state_reducer import GoalStateDecisionConflict
                from app.services.agent_kernel.state_store import (
                    GoalStateNotFound,
                    GoalStateRevisionConflict,
                    GoalStateSessionMismatch,
                )

                try:
                    GoalStateCoordinator(session).apply_ui_decision(
                        session_id=strategist_session_id,
                        goal_id=typed_goal_id,
                        goal_revision=typed_revision,
                        decision_id=typed_decision_id,
                        option_id=typed_option_id,
                        source_event_id=typed_event_id,
                    )
                except GoalStateNotFound as error:
                    raise HTTPException(status_code=404, detail="Goal decision was not found.") from error
                except GoalStateSessionMismatch as error:
                    raise HTTPException(status_code=403, detail="Goal decision belongs to another session.") from error
                except GoalStateDecisionConflict as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                except GoalStateRevisionConflict as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                effective_prompt = ""
                ui_event = {
                    "kind": "clarification_choice",
                    "source_turn_id": ui_event.get("source_turn_id") or ui_event.get("sourceTurnId"),
                    "goal_id": typed_goal_id,
                    "goal_revision": typed_revision,
                    "decision_id": typed_decision_id,
                    "option_id": typed_option_id,
                    "event_id": typed_event_id,
                    "payload": {},
                }
            else:
                if any(
                    value is not None
                    for value in (
                        ui_event.get("goal_id") or ui_event.get("goalId"),
                        ui_event.get("goal_revision") or ui_event.get("goalRevision"),
                        ui_event.get("decision_id") or ui_event.get("decisionId"),
                        ui_event.get("option_id") or ui_event.get("optionId"),
                        ui_event.get("event_id") or ui_event.get("eventId"),
                    )
                ):
                    raise HTTPException(status_code=422, detail="Invalid typed decision identity.")
                follow_up_prompt = (
                    str(payload.get("followUpPrompt") or payload.get("follow_up_prompt") or "").strip()
                    if isinstance(payload, dict)
                    else ""
                )
                effective_prompt = follow_up_prompt or prompt or build_display_prompt(prompt, ui_event)
            if isinstance(ui_event.get("target_node_id"), int):
                effective_focus_node_id = int(ui_event["target_node_id"])

        result = kernel_runner(
            session=session,
            prompt=effective_prompt,
            ui_event=ui_event,
            active_category_id=active_category_id,
            focus_node_id=effective_focus_node_id,
            staged_node_ids=staged_node_ids,
            conversation_history=conversation_history,
            strategist_session_id=strategist_session_id,
            strategist_turn_id=strategist_turn_id,
            provider_override=provider_override,
            provider_session_id=provider_session_id,
            model_override=model_override,
            today_override=today_override,
            include_diagnostics=include_diagnostics,
            requested_capability_ids=requested_capability_ids,
            workspace_context=workspace_context,
        )
        response_payload = result.get("response") if isinstance(result.get("response"), dict) else {}
        # Kernel output is already compiled, simulated, and projected by the
        # server-owned authority. Never canonicalize compatibility-shaped
        # diagnostic fields into proposal records or legacy ChangeSets.
        updated_response = response_payload
        proposal_ids: list[str] = []
        attach_response_task_references(session, updated_response)
        diagnostics = (
            result.get("diagnostics", {})
            if isinstance(result.get("diagnostics"), dict)
            else {}
        )
        updated_response["trace"] = build_public_reasoning_trace(diagnostics)
        result["response"] = updated_response
        result["proposal_ids"] = proposal_ids
        if include_diagnostics:
            result["diagnostics"] = diagnostics
        return result

    raise HTTPException(
        status_code=410,
        detail="The legacy Strategist graph runtime has been retired. Use the agent-kernel harness.",
    )
