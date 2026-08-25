"""Bounded provider context and durable evidence handles for one active goal."""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session

from app.models.strategist_agent import StrategistAgentEvent
from app.services.agent_kernel.contracts import GoalState
from app.services.agent_kernel.goal_state_reducer import TERMINAL_GOAL_STATUSES
from app.services.agent_kernel.session_memory import build_tool_context_entry


MAX_COMPACT_GOAL_CONTEXT_BYTES = 24 * 1024
MAX_EVIDENCE_RESULT_BYTES = 24 * 1024
MAX_LATEST_TURN_CHARS = 4_000
MAX_SEMANTIC_SLOTS = 24
MAX_RESOLVED_REFS = 24
MAX_CONSTRAINTS = 24
MAX_UNRESOLVED_DECISIONS = 6
MAX_EVIDENCE_HANDLES = 12
EVIDENCE_HANDLE_PREFIX = "evidence:"

# Compact context is a transport projection, not the canonical goal store.  A
# normal multi-turn goal must never fail merely because one of its typed
# semantic surfaces grew beyond the preferred provider budget.  These profiles
# progressively retain fewer historical collection items while preserving the
# newest user turn, goal identity, pending review identity, and the fields of
# each retained typed item.
_COMPACT_CONTEXT_PROFILES = (
    (24, 32, 1_000, 4_000),
    (16, 32, 512, 3_000),
    (8, 24, 256, 2_000),
    (8, 16, 128, 1_000),
)


def evidence_handle_for_event(event: StrategistAgentEvent) -> str:
    if event.event_type != "tool_observation":
        raise ValueError("only tool observations can become evidence handles")
    return f"{EVIDENCE_HANDLE_PREFIX}{event.id}"


def resolve_evidence_handle(
    session: Session,
    *,
    session_id: str,
    goal_id: str,
    handle: str,
) -> dict[str, Any]:
    if not isinstance(handle, str) or not handle.startswith(EVIDENCE_HANDLE_PREFIX):
        raise KeyError("invalid evidence handle")
    event_id = handle[len(EVIDENCE_HANDLE_PREFIX) :]
    if not event_id:
        raise KeyError("invalid evidence handle")
    event = session.get(StrategistAgentEvent, event_id)
    if event is None or event.event_type != "tool_observation":
        raise KeyError("evidence handle was not found")
    if event.session_id != session_id or event.goal_id != goal_id:
        raise PermissionError("evidence handle does not belong to this session and goal")
    try:
        payload = json.loads(event.payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("stored evidence payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("stored evidence payload must be an object")
    name = payload.get("name")
    arguments = payload.get("arguments")
    result = payload.get("result")
    if not isinstance(name, str) or not isinstance(result, dict):
        raise ValueError("stored event is not a resolvable tool observation")
    compact = build_tool_context_entry(
        name=name,
        arguments=arguments if isinstance(arguments, dict) else {},
        result=result,
    )
    if _json_size(compact) > MAX_EVIDENCE_RESULT_BYTES:
        raise ValueError("bounded evidence result exceeds the allowed size")
    return compact


def build_compact_goal_context(
    *,
    goal_state: GoalState,
    latest_user_turn: str,
    runtime_context: dict[str, Any],
    pending_proposal: dict[str, Any] | None,
    recent_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one provider-facing continuity surface from canonical goal truth."""

    terminal = goal_state.status in TERMINAL_GOAL_STATUSES
    semantic_slots = [
        item.model_dump(mode="json", by_alias=True)
        for item in goal_state.semantic_slots[:MAX_SEMANTIC_SLOTS]
    ]
    resolved_refs = [
        item.model_dump(mode="json", by_alias=True)
        for item in goal_state.resolved_refs[:MAX_RESOLVED_REFS]
    ]
    constraints = [
        item.model_dump(mode="json", by_alias=True)
        for item in goal_state.constraints[:MAX_CONSTRAINTS]
    ]
    decisions = [
        item.model_dump(mode="json", by_alias=True)
        for item in goal_state.unresolved_decisions[:MAX_UNRESOLVED_DECISIONS]
    ]
    evidence_handles: list[str] = []
    if not terminal:
        for event in recent_events:
            handle = event.get("evidence_handle")
            if (
                isinstance(handle, str)
                and handle.startswith(EVIDENCE_HANDLE_PREFIX)
                and handle not in evidence_handles
            ):
                evidence_handles.append(handle)
            if len(evidence_handles) >= MAX_EVIDENCE_HANDLES:
                break

    context = {
        "schemaVersion": "1.0",
        "goal": {
            "status": goal_state.status,
            "originalRequest": goal_state.original_request,
        },
        "latestUserTurn": str(latest_user_turn or "").strip()[:MAX_LATEST_TURN_CHARS],
        "semanticSlots": semantic_slots,
        "resolvedReferences": resolved_refs,
        "constraints": constraints,
        "unresolvedDecisions": decisions,
        "pendingReview": (
            _compact_pending_review(pending_proposal, goal_id=goal_state.goal_id)
            if not terminal
            else None
        ),
        "evidenceHandles": evidence_handles,
        "uiEvidence": _compact_explicit_ui_evidence(runtime_context),
    }
    if _json_size(context) <= MAX_COMPACT_GOAL_CONTEXT_BYTES:
        return context

    for list_limit, dict_limit, string_limit, narrative_limit in (
        _COMPACT_CONTEXT_PROFILES
    ):
        bounded = _bounded_goal_context(
            context,
            list_limit=list_limit,
            dict_limit=dict_limit,
            string_limit=string_limit,
            narrative_limit=narrative_limit,
        )
        if _json_size(bounded) <= MAX_COMPACT_GOAL_CONTEXT_BYTES:
            return bounded

    # The preceding profiles cover the canonical contracts by a wide margin.
    # Keep a deterministic last-resort continuity surface for malformed or
    # future oversized extensions instead of aborting an otherwise valid turn.
    # The canonical goal and evidence remain durable and can be re-read by tool.
    pending = context.get("pendingReview")
    minimal_pending = None
    if isinstance(pending, dict):
        minimal_pending = {
            key: _bounded_json_surface(
                pending.get(key),
                list_limit=8,
                dict_limit=12,
                string_limit=128,
            )
            for key in (
                "proposalId",
                "status",
                "revision",
                "targetTaskId",
                "targetTaskIds",
                "summary",
                "semanticContract",
            )
            if pending.get(key) is not None
        }
    return {
        "schemaVersion": "1.0",
        "goal": {
            "status": context["goal"]["status"],
            "originalRequest": str(
                context["goal"].get("originalRequest") or ""
            )[:1_000],
        },
        "latestUserTurn": str(context.get("latestUserTurn") or "")[:1_000],
        "semanticSlots": _bounded_json_surface(
            context.get("semanticSlots", [])[:4],
            list_limit=8,
            dict_limit=12,
            string_limit=128,
        ),
        "resolvedReferences": _bounded_json_surface(
            context.get("resolvedReferences", [])[:8],
            list_limit=8,
            dict_limit=12,
            string_limit=128,
        ),
        "constraints": _bounded_json_surface(
            context.get("constraints", [])[:8],
            list_limit=8,
            dict_limit=12,
            string_limit=128,
        ),
        "unresolvedDecisions": _bounded_json_surface(
            context.get("unresolvedDecisions", [])[:4],
            list_limit=8,
            dict_limit=12,
            string_limit=128,
        ),
        "pendingReview": minimal_pending,
        "evidenceHandles": list(context.get("evidenceHandles", []))[:8],
        "uiEvidence": context.get("uiEvidence", {}),
        "contextTruncated": True,
    }


def _bounded_goal_context(
    context: dict[str, Any],
    *,
    list_limit: int,
    dict_limit: int,
    string_limit: int,
    narrative_limit: int,
) -> dict[str, Any]:
    goal = context.get("goal") if isinstance(context.get("goal"), dict) else {}
    return {
        "schemaVersion": "1.0",
        "goal": {
            "status": goal.get("status"),
            "originalRequest": str(goal.get("originalRequest") or "")[
                :narrative_limit
            ],
        },
        "latestUserTurn": str(context.get("latestUserTurn") or "")[
            :narrative_limit
        ],
        "semanticSlots": _bounded_json_surface(
            context.get("semanticSlots", []),
            list_limit=list_limit,
            dict_limit=dict_limit,
            string_limit=string_limit,
        ),
        "resolvedReferences": _bounded_json_surface(
            context.get("resolvedReferences", []),
            list_limit=list_limit,
            dict_limit=dict_limit,
            string_limit=string_limit,
        ),
        "constraints": _bounded_json_surface(
            context.get("constraints", []),
            list_limit=list_limit,
            dict_limit=dict_limit,
            string_limit=string_limit,
        ),
        "unresolvedDecisions": _bounded_json_surface(
            context.get("unresolvedDecisions", []),
            list_limit=list_limit,
            dict_limit=dict_limit,
            string_limit=string_limit,
        ),
        "pendingReview": _bounded_json_surface(
            context.get("pendingReview"),
            list_limit=list_limit,
            dict_limit=dict_limit,
            string_limit=string_limit,
        ),
        "evidenceHandles": list(context.get("evidenceHandles", []))[:list_limit],
        "uiEvidence": context.get("uiEvidence", {}),
        "contextTruncated": True,
    }


def _bounded_json_surface(
    value: Any,
    *,
    list_limit: int,
    dict_limit: int,
    string_limit: int,
    depth: int = 0,
) -> Any:
    """Bound an already-typed JSON surface without inventing semantic values."""

    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 10:
        return None
    if isinstance(value, list):
        return [
            _bounded_json_surface(
                item,
                list_limit=list_limit,
                dict_limit=dict_limit,
                string_limit=string_limit,
                depth=depth + 1,
            )
            for item in value[:list_limit]
        ]
    if isinstance(value, dict):
        return {
            str(key): _bounded_json_surface(
                child,
                list_limit=list_limit,
                dict_limit=dict_limit,
                string_limit=string_limit,
                depth=depth + 1,
            )
            for key, child in list(value.items())[:dict_limit]
        }
    return str(value)[:string_limit]


def _compact_pending_review(
    value: dict[str, Any] | None,
    *,
    goal_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    proposal_goal_id = value.get("goal_id", value.get("goalId"))
    if proposal_goal_id != goal_id:
        return None
    proposal_id = value.get("proposal_id", value.get("proposalId"))
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return None
    summary = str(value.get("summary") or "").strip()[:240]
    raw_target_task_ids = value.get(
        "target_task_ids",
        value.get("targetTaskIds", []),
    )
    target_task_ids = (
        raw_target_task_ids
        if isinstance(raw_target_task_ids, list)
        else []
    )
    compact = {
        "proposalId": proposal_id,
        "status": value.get("status"),
        "revision": value.get("revision"),
        "targetTaskId": value.get("target_task_id", value.get("targetTaskId")),
        "targetTaskIds": target_task_ids[:24],
        "summary": summary or None,
    }
    semantic_contract = value.get(
        "semantic_contract",
        value.get("semanticContract"),
    )
    if isinstance(semantic_contract, dict):
        # This is the bounded, server-authored continuation contract—not raw
        # proposal persistence. It lets the model fill one missing slot without
        # reconstructing and narrowing the rest of the user's request.
        compact["semanticContract"] = semantic_contract
    return compact


def _compact_explicit_ui_evidence(runtime_context: dict[str, Any]) -> dict[str, Any]:
    workspace = runtime_context.get("workspace_context")
    if not isinstance(workspace, dict):
        return {"attachedTaskIds": [], "selectedTaskIds": []}
    attached = workspace.get("attached_task_ids", workspace.get("attachedTaskIds", []))
    selected = workspace.get("selected_task_ids", workspace.get("selectedTaskIds", []))
    return {
        "attachedTaskIds": _bounded_int_ids(attached),
        "selectedTaskIds": _bounded_int_ids(selected),
    }


def _bounded_int_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool) and item not in result:
            result.append(item)
        if len(result) >= 24:
            break
    return result


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
