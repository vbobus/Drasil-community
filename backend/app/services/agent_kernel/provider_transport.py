"""Provider transport boundary for the Strategist agent kernel.

This module owns Codex/OpenAI transport for the production harness. It must not
import the legacy ``app.services.strategist`` orchestrator; that keeps provider
swaps and OAuth/auth failures out of the old semantic control plane.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from fastapi import HTTPException

from app.core.codex_cli import find_codex_cli_path, require_codex_cli_path
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.core_contract import render_core_operating_instructions
from app.services.agent_kernel.codex_app_server import DIRECT_DYNAMIC_TOOL_NAMES
from app.services.agent_kernel.agent_observability import observe_generation
from app.services.agent_kernel.provider_matrix import (
    UnsupportedProviderModel,
    UnsupportedReasoningEffort,
    resolve_configured_oauth_matrix_entry,
    validate_configured_model,
)
from app.services.codex_login_state import has_usable_codex_auth_file
from app.services.strategist_agent_contract import (
    STRATEGIST_AGENT_CAPABILITIES,
    STRATEGIST_AGENT_CONTEXT_SURFACES,
    STRATEGIST_AGENT_OPERATING_CONTRACT,
    STRATEGIST_AGENT_PROPOSAL_CONTRACT,
    STRATEGIST_AGENTIC_LOOP_CONTRACT,
    STRATEGIST_DECISION_SYNTHESIS_CONTRACT,
    STRATEGIST_TREE_OPERATIONS_CONTRACT,
)
from app.services.strategist_provider import (
    get_codex_model_catalog,
    get_effective_provider_settings,
)
from app.services.strategist_provider import DEFAULT_CODEX_MODEL, DEFAULT_OPENAI_MODEL
from app.services.strategist_response_contract import (
    STRATEGIST_DIRECT_RESPONSE_SCHEMA,
    STRATEGIST_JSON_SCHEMA,
    STRATEGIST_TOOL_STEP_SCHEMA,
    parse_json_object_text,
)
from app.services.task_read_handlers import STRATEGIST_TOOL_DEFINITIONS

StrategistToolExecutor = Callable[..., dict[str, Any]]


SEMANTIC_TOOL_ARGUMENT_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "arguments_json": {
            "type": "string",
            "description": "One JSON object containing the semantic tool arguments.",
        }
    },
    "required": ["arguments_json"],
}


def _missing_authoritative_tool_executor(
    _name: str,
    _arguments: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any]:
    raise RuntimeError(
        "Provider-selected tools require the host-owned authoritative MCP executor."
    )
# The Strategist runs behind a durable run record.  These are last-resort
# runtime ceilings, not user-facing request timeouts; the app-server's
# meaningful-progress idle watchdog remains the mechanism for detecting a
# genuinely stuck provider turn.
DEFAULT_CODEX_STRUCTURED_STEP_TIMEOUT_SECONDS = 1800
DEFAULT_CODEX_TOOL_LOOP_TIMEOUT_SECONDS = 7200
DEFAULT_CODEX_TOOL_LOOP_MAX_STEPS = 4
DEFAULT_CODEX_TRANSIENT_DISCONNECT_RETRIES = 1
OPENCLAW_STYLE_MAX_TIMEOUT_SECONDS = 172800


def _bounded_env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 1)


def codex_structured_step_timeout_seconds() -> int:
    return _bounded_env_int(
        "STRATEGIST_MODEL_STEP_TIMEOUT_SECONDS",
        default=DEFAULT_CODEX_STRUCTURED_STEP_TIMEOUT_SECONDS,
        minimum=60,
        maximum=OPENCLAW_STYLE_MAX_TIMEOUT_SECONDS,
    )


def codex_tool_loop_timeout_seconds() -> int:
    return _bounded_env_int(
        "STRATEGIST_MODEL_LOOP_TIMEOUT_SECONDS",
        default=DEFAULT_CODEX_TOOL_LOOP_TIMEOUT_SECONDS,
        minimum=60,
        maximum=OPENCLAW_STYLE_MAX_TIMEOUT_SECONDS,
    )


def codex_tool_loop_max_steps() -> int:
    return _bounded_env_int(
        "STRATEGIST_MODEL_LOOP_MAX_STEPS",
        default=DEFAULT_CODEX_TOOL_LOOP_MAX_STEPS,
        minimum=1,
        maximum=7,
    )


def codex_transient_disconnect_retries() -> int:
    """Bound fresh transport retries after a safe pre-output disconnect.

    The app-server and upstream provider already perform their own internal
    retries.  This last-mile retry is intentionally limited and applies only
    when the failed attempt produced no output and executed no Drasil tools.
    """

    return _bounded_env_int(
        "STRATEGIST_CODEX_TRANSIENT_DISCONNECT_RETRIES",
        default=DEFAULT_CODEX_TRANSIENT_DISCONNECT_RETRIES,
        minimum=0,
        maximum=2,
    )


def _is_safe_retryable_codex_disconnect(error: Exception) -> bool:
    trace = getattr(error, "trace", None)
    if not isinstance(trace, dict) or str(trace.get("status") or "").lower() != "failed":
        return False
    if int(trace.get("dynamic_tool_call_count") or 0) != 0:
        return False
    if int(trace.get("final_text_chars") or 0) != 0:
        return False
    detail = str(error).lower()
    stderr_preview = trace.get("stderr_preview")
    transport_diagnostics = " ".join(
        str(line).lower()
        for line in stderr_preview
        if isinstance(line, str)
    ) if isinstance(stderr_preview, list) else ""
    if any(
        marker in transport_diagnostics
        for marker in (
            "forbidden by its access permissions",
            "os error 10013",
            "permission denied",
        )
    ):
        return False
    return any(
        marker in detail
        for marker in (
            "stream disconnected before completion",
            "connection reset before completion",
            "connection closed before completion",
        )
    )


def codex_native_tool_loop_enabled() -> bool:
    return os.getenv("STRATEGIST_CODEX_NATIVE_TOOL_LOOP_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def ensure_codex_login(codex_path: str) -> None:
    from app.services.codex_sandbox import enabled, get_status
    if enabled():
        if not get_status().get("isAuthenticated"):
            raise HTTPException(401, "Connect ChatGPT in Settings → Connections to use AI.")
        return
    try:
        result = subprocess.run(
            [codex_path, "login", "status"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=account_process_environment(),
        )
        combined_output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0 and "Logged in" in combined_output:
            return
    except (OSError, subprocess.SubprocessError):
        pass

    if not has_usable_codex_auth_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "Strategist requires an active Codex login. Run `codex login` first, "
                "or enable device code authorization before using `codex login --device-auth`."
            ),
        )


def get_provider_runtime_settings(
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    effective_settings = get_effective_provider_settings()
    active_provider = provider_override or effective_settings.get("activeProvider") or "codex-cli"
    provider_label = "ChatGPT login" if active_provider == "codex-cli" else "BYOK (OpenAI API)"
    model_name = model_override
    if model_name is None:
        if active_provider == "codex-cli":
            model_name = (
                effective_settings.get("codexModel")
                if isinstance(effective_settings.get("codexModel"), str)
                else DEFAULT_CODEX_MODEL
            )
        else:
            model_name = (
                effective_settings.get("openAiModel")
                if isinstance(effective_settings.get("openAiModel"), str)
                else DEFAULT_OPENAI_MODEL
            )
    provider_transport = (
        "codex_app_server" if active_provider == "codex-cli" else "openai_api"
    )
    reasoning_effort = (
        effective_settings.get("codexReasoningEffort")
        if active_provider == "codex-cli"
        and isinstance(effective_settings.get("codexReasoningEffort"), str)
        else None
    )
    matrix_supported = True
    matrix_error_code = None
    try:
        if active_provider == "codex-cli":
            # OAuth model availability is dynamic. Resolve the configured model
            # against the authenticated CLI catalog while retaining an explicit
            # deployment matrix as a closed allowlist when one is configured.
            resolve_configured_oauth_matrix_entry(
                model=str(model_name),
                reasoning_effort=reasoning_effort,
                catalog=get_codex_model_catalog(),
            )
        else:
            validate_configured_model(
                provider=provider_transport,
                model=str(model_name),
                reasoning_effort=reasoning_effort,
            )
    except UnsupportedReasoningEffort:
        matrix_supported = False
        matrix_error_code = "unsupported_reasoning_effort"
    except UnsupportedProviderModel:
        matrix_supported = False
        matrix_error_code = "unsupported_provider_model"
    return {
        "provider": active_provider,
        "provider_transport": provider_transport,
        "provider_label": provider_label,
        "model_name": model_name,
        "reasoning_effort": reasoning_effort,
        "matrix_supported": matrix_supported,
        "matrix_error_code": matrix_error_code,
    }


def extract_codex_cli_error(stdout: str, stderr: str) -> str:
    combined = "\n".join(part for part in [stderr.strip(), stdout.strip()] if part).strip()
    if not combined:
        return "Codex strategist request failed."

    for line in reversed(combined.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ERROR:"):
            payload = stripped.removeprefix("ERROR:").strip()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return payload
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            if isinstance(detail, str) and detail.strip():
                if "not supported when using Codex with a ChatGPT account" in detail:
                    return (
                        "The selected ChatGPT-login model is not supported by your Codex sign-in. "
                        "Switch to the configured/default ChatGPT-login model."
                    )
                return detail.strip()
            return payload

    if "not supported when using Codex with a ChatGPT account" in combined:
        return (
            "The selected ChatGPT-login model is not supported by your Codex sign-in. "
            "Switch to the configured/default ChatGPT-login model."
        )

    tail_lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if tail_lines:
        return tail_lines[-1]
    return "Codex strategist request failed."


def parse_codex_session_id(stdout: str, stderr: str) -> str | None:
    combined = "\n".join(part for part in [stdout, stderr] if part)
    match = re.search(r"session id:\s*([0-9a-f-]+)", combined, re.IGNORECASE)
    return match.group(1) if match else None


def build_conversation_transcript(conversation_history: list[dict[str, str]]) -> str:
    if not conversation_history:
        return "No earlier conversation in this chat session."
    lines: list[str] = []
    for message in conversation_history[-16:]:
        role = str(message.get("role") or "user").strip().lower()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        speaker = "User" if role == "user" else "Strategist"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines) if lines else "No earlier conversation in this chat session."


def build_tool_loop_prompt(
    *,
    mode: str,
    prompt: str,
    runtime_context: dict[str, Any],
    conversation_history: list[dict[str, str]] | None = None,
    tool_result: dict[str, Any] | None = None,
    tool_history: list[dict[str, Any]] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
) -> str:
    available_tool_definitions = STRATEGIST_TOOL_DEFINITIONS if tool_definitions is None else tool_definitions
    tool_lines = "\n".join(
        f"- {tool['name']}: {tool['description']}"
        for tool in available_tool_definitions
    )
    tool_instruction = (
        "If you need data, respond with kind='tool_call' and exactly one requested_tool with arguments_json as a JSON object string.\n"
        if available_tool_definitions
        else "No task tools are available for this turn. Respond with kind='final'.\n"
    )
    final_shape = (
        '{"decision":{"kind":"conversation|analysis|mutation|clarification","reason":"<brief>"},'
        '"summary":"<non-empty plain-English answer>",'
        '"action_proposal":null,'
        '"metadata_proposal":null,'
        '"branch_proposal":null,'
        '"preference_memory_proposal":null,'
        '"clarification":null}'
    )
    continuation_proposal_rule = (
        "Multi-turn proposal rule: when the transcript or session memory already "
        "establishes the task, change type, and candidate value, and the latest "
        "user selects or accepts that value, call submit_task_metadata_plan or "
        "submit_task_tree_plan now. Do not require proposal/apply/change wording.\n"
        "If the previous assistant asked for missing mutation details and the latest "
        "user supplied those details, call the typed proposal tool immediately instead "
        "of acknowledging that you now have enough information.\n"
        "When answering a pending clarification with user-supplied task/subtask details, "
        "use the direct parent/task from the transcript and session memory and draft "
        "one typed proposal; do not restart target discovery.\n"
        "A short follow-up like 'let's go with X', 'yes use that', or 'that works' after "
        "your recommendation is a selected-value task-edit continuation. Use the transcript "
        "and session memory; do not run broad retrieval before calling the proposal tool.\n"
        "A follow-up such as 'group by company', 'by company', 'by stage', or 'use the "
        "company strategy' after a decomposition recommendation is also a selected "
        "tree-edit continuation; call submit_task_tree_plan instead of giving more advice.\n"
        "For straightforward named-task edits with a concrete value, you may call the "
        "typed proposal tool directly using target_task_title; the harness validates "
        "and normalizes the target at the boundary.\n"
        "For metadata-only edits across multiple identified tasks with different values, "
        "call submit_task_metadata_plan once with target_updates, using an exact target_task_id "
        "and updates object for every task. Do not use tree operations for that case.\n"
        "Preference-memory rule: when the user asks you to remember a durable scheduling "
        "preference, or clearly states one that should affect future planning, call "
        "submit_preference_plan. Do not merely acknowledge it in prose. Ask a "
        "clarification if a hard preference lacks exact enforceable details. A request to "
        "remind the user to perform an action is task/schedule intent, not preference memory. "
        "Use a stable semantic preference type and structured applicability conditions. If "
        "a newer instruction overlaps remembered rules but the available evidence does not "
        "establish whether it replaces or complements them, ask one focused clarification. "
        "When it is an update, say which remembered rule or rules the review will replace.\n"
        "Preference-management rule: when the user asks to inspect or change specific "
        "saved preferences, call list_remembered_preferences first, then call "
        "submit_preference_management_plan with exact IDs and revisions. When the user "
        "asks for all conflicting, contradictory, outdated, duplicate, or redundant "
        "preferences, call analyze_preference_cleanup_candidates with contradictions for "
        "conflicting/superseded values, overlaps for duplicate/redundant copies, or all only "
        "when both were requested. Use submit_preference_cleanup_plan with the same scope and "
        "exact candidate-set fingerprint; never "
        "manually select a subset and present it as the complete batch. Treat "
        "remove as ambiguous between reversible disable and permanent deletion unless the "
        "user's wording or prior context clearly selects one; ask one focused clarification "
        "when needed. Never claim a saved preference changed without an approved review.\n"
    )
    recommendation_proposal_rule = (
        "Recommendation boundary rule: if the latest user only asks you to find, "
        "recommend, suggest, or compare a possible date/time/metadata value, answer "
        "with decision support and no approval card. Call a proposal tool only when "
        "the same request includes a mutation intent or a later user turn selects or "
        "accepts one concrete value.\n"
    )
    compound_mutation_rule = (
        "Compound mutation rule: preserve every requested clause. If the user asks "
        "for metadata plus decomposition/create/move/delete/restructure work, use "
        "submit_task_tree_plan with ordered structural_operations, including "
        "metadata_update operations when needed. If a required detail is missing, "
        "ask one clarification that carries the still-pending clauses instead of "
        "collapsing the request into a single-field question.\n"
        "When one request spans task/tree changes and durable preference memory, call "
        "the appropriate task semantic-plan tool and submit_preference_plan before "
        "returning the final response. A successful semantic-plan call does not end "
        "the turn while another requested family remains unsubmitted.\n"
        "If one request creates a task and schedules it as a finite daily recurrence, use "
        "one submit_task_tree_plan call with create_node plus recurring_schedule. Preserve "
        "the stated duration, give the created task a temp_id, and reference that temp_id in "
        "the recurrence. For existing tasks, pass structured recurrence to Planner so each "
        "date is placed independently and only conflicting days move.\n"
        "When creating subtasks in a multi-task context, choose the most specific "
        "intended parent from the transcript and tool evidence. Do not default to an "
        "ancestor/grandparent when the user names or corrects the direct parent.\n"
        "For every created node, decide its semantic tree parent independently from whether "
        "it needs calendar placement. Express ownership/decomposition with parent_temp_id. "
        "Inventory every explicit current-request clause before submission. Put every item the "
        "user asked to schedule in created_task_schedule, including fixed commitments and "
        "existing tasks. Do not omit an item merely because its duration or exact slot was not "
        "stated: Planner uses task history, saved preferences, calendar occupancy, and bounded "
        "estimates. Use schedule_groups when scheduled items have different windows. A successful "
        "proposal for one convenient target does not complete a compound request while another "
        "inventoried clause is still missing.\n"
        "For a task added without a clock time, set planned_date to its requested day, "
        "or the client local today when no day is given. This holds the task above the "
        "calendar timeline without booking time. Do not invent a deadline or a timed block. "
        "Use created_task_schedule only for an explicit time, time window, or request to "
        "find/schedule time; an untimed planned day alone needs no scheduling call. "
        "An explicit backlog or undated request may omit planned_date. "
        "If the user asks to create a new task without naming a parent, make it top-level. "
        "Honor an explicitly named category; otherwise choose the best available category "
        "from the category catalog using the task meaning and existing roots. Treat the active "
        "category as a tie-breaker, not the default. You must call submit_task_tree_plan with create_node; "
        "a prose sentence that says a task was proposed is invalid. Use typed arguments "
        "like target_category_name plus structural_operations=[{kind:'create_node', "
        "title:'...', deadline_at:'YYYY-MM-DD'}].\n"
        "If the user asks for a new reusable parent task plus new child subtasks, emit "
        "one parent create_node with temp_id and each child create_node with parent_temp_id "
        "pointing to that temp_id; do not emit the children as separate top-level creates.\n"
        "If the user asks to break down or decompose an existing task but does not list "
        "exact subtasks, infer a reasonable small set when the target is a broad project "
        "or deliverable, then propose those children with create_node operations. If the "
        "target appears atomic, like a single email/call/message where child content "
        "depends on user-specific recipients or details, ask for those subtasks while "
        "carrying any pending metadata clauses.\n"
        "If your summary says you decomposed, broke down, added subtasks, or created "
        "children, the proposal must include matching create_node operations.\n"
        "When a task title is an exact or clearly more specific match, do not clarify "
        "because another task shares a shorter alias.\n"
    )
    agent_contract = (
        f"{STRATEGIST_AGENT_OPERATING_CONTRACT}\n"
        f"{STRATEGIST_AGENT_CAPABILITIES}\n"
        f"{STRATEGIST_AGENT_CONTEXT_SURFACES}\n"
        f"{STRATEGIST_AGENT_PROPOSAL_CONTRACT}\n"
        f"{STRATEGIST_DECISION_SYNTHESIS_CONTRACT}\n"
        f"{STRATEGIST_TREE_OPERATIONS_CONTRACT}\n"
        f"{STRATEGIST_AGENTIC_LOOP_CONTRACT}\n"
    )
    if tool_result is not None:
        prior_tool_context = ""
        if tool_history:
            prior_tool_context = (
                "\nTool results already available in this turn. Do not request the same tool "
                "with the same arguments again; use these results unless a different branch "
                f"or deeper detail is required.\n{json.dumps(tool_history[-6:], ensure_ascii=True)}\n"
            )
        return (
            "Continue the current Strategist turn.\n"
            "Use the tool result below to either request one more read-only tool call or return the final answer.\n"
            "If you need another tool, respond with kind='tool_call'. If done, respond with kind='final'.\n"
            "If query_task_graph returned answer_candidate, prefer answer_candidate.summary and preserve every task in answer_candidate.task_ids unless you explicitly call another tool that contradicts it.\n"
            "For referential follow-ups like 'that task' or 'it', restate the resolved task title in the final summary.\n"
            f"The final response object must have exactly these top-level keys: {final_shape}\n\n"
            f"{agent_contract}"
            "Mutations must call a typed proposal tool or return a structured proposal object/clarification, never only prose.\n"
            "Prefer submit_task_metadata_plan for metadata-only edits. For a mixed request, use one "
            "submit_task_tree_plan call containing every metadata_update, create, move, archive, "
            "or delete operation in execution order. Never describe a change in the summary or "
            "rationale unless a matching typed operation is present.\n"
            f"{continuation_proposal_rule}"
            f"{recommendation_proposal_rule}"
            f"{compound_mutation_rule}"
            "Move/reparent/reorder/group/ungroup/flatten/restructure work must use submit_task_tree_plan.\n"
            "If the user request is normal conversation or external-world planning, answer the request directly; "
            "do not require a selected task unless the user asked to mutate the task tree.\n"
            f"{prior_tool_context}"
            f"Requested mode: {mode}\n\n"
            "Conversation transcript for this chat session:\n"
            f"{build_conversation_transcript(conversation_history or [])}\n\n"
            "User request:\n"
            f"{prompt}\n\n"
            f"Tool result JSON:\n{json.dumps(tool_result, ensure_ascii=True)}\n"
        )

    return (
        "You are Strategist AI inside a planning application.\n"
        f"Shared operating contract: {render_core_operating_instructions('strategist')}\n"
        "Infer the user's intent, gather only the necessary read-only evidence, then synthesize the final answer.\n"
        f"{agent_contract}"
        "Do not schedule calendar blocks, edit files, run shell commands, or mention internal ids in prose.\n"
        "Raw ids are allowed inside structured proposal fields.\n"
        "Explicit user text beats selected UI focus and session memory.\n"
        "Use tools for task/workload facts. For normal conversation or external-world planning without live tools, answer directly and state limits plainly.\n"
        "Do not create a mutation proposal for a read-only analysis request.\n"
        "If the user asks to create/update/delete/move/restructure tasks, call submit_task_metadata_plan/submit_task_tree_plan or return a clarification.\n"
        f"{continuation_proposal_rule}"
        f"{recommendation_proposal_rule}"
        f"{compound_mutation_rule}"
        "For broad list/filter/sort requests, prefer structured filters. For broad recommendations, gather bounded evidence and synthesize ranked judgment.\n"
        "When query_task_graph returns answer_candidate, treat it as the preferred synthesized evidence; do not let raw entity_matches override it.\n"
        "For referential follow-ups like 'that task' or 'it', restate the resolved task title in the final summary.\n"
        f"{tool_instruction}"
        "If you have enough data, respond with kind='final' and put the complete final Strategist response object into final_payload_json.\n"
        f"The final response object must have exactly these top-level keys: {final_shape}\n\n"
        "Available tools:\n"
        f"{tool_lines}\n\n"
        f"Requested mode: {mode}\n\n"
        "Current UI context JSON:\n"
        f"{json.dumps(runtime_context, ensure_ascii=True)}\n\n"
        "Conversation transcript for this chat session:\n"
        f"{build_conversation_transcript(conversation_history or [])}\n\n"
        "User request:\n"
        f"{prompt}\n"
    )


def build_native_tool_loop_prompt(
    *,
    mode: str,
    prompt: str,
    runtime_context: dict[str, Any],
    conversation_history: list[dict[str, str]] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    tool_history: list[dict[str, Any]] | None = None,
) -> str:
    available_tool_definitions = STRATEGIST_TOOL_DEFINITIONS if tool_definitions is None else tool_definitions
    tool_names = {
        str(tool.get("name") or "").strip()
        for tool in available_tool_definitions
        if str(tool.get("name") or "").strip()
    }
    bounded_metadata_lane = tool_names == {"submit_task_metadata_plan"} and any(
        isinstance(item, dict)
        and str(item.get("tool_name") or item.get("name") or "").strip()
        == "search_entities"
        and isinstance(item.get("result"), dict)
        and not item["result"].get("error")
        for item in (tool_history or [])
    )
    native_runtime_context = {
        key: value
        for key, value in runtime_context.items()
        if key
        not in {
            "contract",
            "tool_registry",
            "foreground_budget",
            "visible_transcript",
            "recent_events",
            "capability_catalog",
            "tier_route",
        }
    }
    prior_tool_evidence = _native_tool_history_checkpoint(tool_history or [])
    if prior_tool_evidence:
        native_runtime_context["prior_tool_evidence"] = prior_tool_evidence
    if bounded_metadata_lane:
        resolved_task: dict[str, Any] = {}
        for item in reversed(tool_history or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("tool_name") or item.get("name") or "").strip() != "search_entities":
                continue
            result = item.get("result")
            matches = result.get("matches") if isinstance(result, dict) else None
            if not isinstance(matches, list) or not matches:
                continue
            first_match = matches[0] if isinstance(matches[0], dict) else {}
            task = (
                first_match.get("task")
                if isinstance(first_match.get("task"), dict)
                else first_match
            )
            task_id = task.get("task_id", task.get("id"))
            task_title = task.get("task_title", task.get("title"))
            if isinstance(task_id, int) and not isinstance(task_id, bool):
                resolved_task["task_id"] = task_id
            if isinstance(task_title, str) and task_title.strip():
                resolved_task["task_title"] = task_title.strip()
            break
        return (
            "This is one approval-gated task metadata mutation. Never apply data directly. "
            "The exact task is supplied below. Call "
            "submit_task_metadata_plan exactly once using that task_id and the explicit "
            "field value in the user request. Do not retrieve, deliberate, repeat, or "
            "answer in prose.\n\n"
            "Exact task JSON:\n"
            f"{json.dumps(resolved_task, ensure_ascii=True, separators=(',', ':'))}\n\n"
            "User request:\n"
            f"{prompt}\n"
        )
    # Keep the human-readable manifest synchronized with the actual app-server
    # surface.  A tool advertised here as direct but registered with
    # ``deferLoading`` encourages unnecessary discovery calls and defeats the
    # point of keeping specialized schemas out of the initial model context.
    direct_tool_names = DIRECT_DYNAMIC_TOOL_NAMES
    direct_tools = [
        tool
        for tool in available_tool_definitions
        if str(tool.get("name") or "") in direct_tool_names
    ]
    deferred_tools = [
        str(tool.get("name") or "").strip()
        for tool in available_tool_definitions
        if str(tool.get("name") or "").strip()
        and str(tool.get("name") or "").strip() not in direct_tool_names
    ]
    direct_tool_lines = "\n".join(
        f"- {tool['name']}"
        for tool in direct_tools
        if isinstance(tool.get("name"), str)
    )
    deferred_manifest = (
        "Deferred task tools are searchable by tool purpose: "
        + ", ".join(sorted(deferred_tools))
        + ". Use the native tool search surface when available, or call "
        "search_tool_catalog when you are unsure which Drasil tool fits."
        if deferred_tools
        else "No deferred task tools are registered."
    )
    final_shape = (
        '{"decision":{"kind":"conversation|analysis|mutation|clarification","reason":"<brief>"},'
        '"summary":"<non-empty plain-English answer>",'
        '"semantic_plan_input":null,'
        '"clarification":null}'
    )
    continuation_proposal_rule = (
        "Multi-turn proposal rule: when the transcript or session memory already "
        "establishes the task, change type, and candidate value, and the latest "
        "user selects or accepts that value, call submit_task_metadata_plan or "
        "submit_task_tree_plan now. Do not require proposal/apply/change wording.\n"
        "If the previous assistant asked for missing mutation details and the latest "
        "user supplied those details, call the typed proposal tool immediately instead "
        "of acknowledging that you now have enough information.\n"
        "When answering a pending clarification with user-supplied task/subtask details, "
        "use the direct parent/task from the transcript and session memory and draft "
        "one typed proposal; do not restart target discovery.\n"
        "A short follow-up like 'let's go with X', 'yes use that', or 'that works' after "
        "your recommendation is a selected-value task-edit continuation. Use the transcript "
        "and session memory; do not run broad retrieval before calling the proposal tool.\n"
        "A follow-up such as 'group by company', 'by company', 'by stage', or 'use the "
        "company strategy' after a decomposition recommendation is also a selected "
        "tree-edit continuation; call submit_task_tree_plan instead of giving more advice.\n"
        "For straightforward named-task edits with a concrete value, you may call the "
        "typed proposal tool directly using target_task_title; the harness validates "
        "and normalizes the target at the boundary.\n"
    )
    recommendation_proposal_rule = (
        "Recommendation boundary rule: if the latest user only asks you to find, "
        "recommend, suggest, or compare a possible date/time/metadata value, answer "
        "with decision support and no approval card. Call a proposal tool only when "
        "the same request includes a mutation intent or a later user turn selects or "
        "accepts one concrete value.\n"
    )
    compound_mutation_rule = (
        "Compound mutation rule: preserve every requested clause. If the user asks "
        "for metadata plus decomposition/create/move/delete/restructure work, use "
        "submit_task_tree_plan with ordered structural_operations, including "
        "metadata_update operations when needed. If a required detail is missing, "
        "ask one clarification that carries the still-pending clauses instead of "
        "collapsing the request into a single-field question.\n"
        "If one request creates a task and schedules it as a finite daily recurrence, use "
        "one submit_task_tree_plan call with create_node plus recurring_schedule. Preserve "
        "the stated duration and use one approval for task creation and calendar blocks.\n"
        "When creating subtasks in a multi-task context, choose the most specific "
        "intended parent from the transcript and tool evidence. Do not default to an "
        "ancestor/grandparent when the user names or corrects the direct parent.\n"
        "For a task added without a clock time, set planned_date to its requested day, "
        "or the client local today when no day is given. This holds the task above the "
        "calendar timeline without booking time. Do not invent a deadline or a timed block. "
        "Use created_task_schedule only for an explicit time, time window, or request to "
        "find/schedule time; an untimed planned day alone needs no scheduling call. "
        "An explicit backlog or undated request may omit planned_date. "
        "If the user asks to create a new task without naming a parent, make it top-level. "
        "Honor an explicitly named category; otherwise choose the best available category "
        "from the category catalog using the task meaning and existing roots. Treat the active "
        "category as a tie-breaker, not the default. You must call submit_task_tree_plan with create_node; "
        "a prose sentence that says a task was proposed is invalid. Use typed arguments "
        "like target_category_name plus structural_operations=[{kind:'create_node', "
        "title:'...', deadline_at:'YYYY-MM-DD'}].\n"
        "If the user asks for a new reusable parent task plus new child subtasks, emit "
        "one parent create_node with temp_id and each child create_node with parent_temp_id "
        "pointing to that temp_id; do not emit the children as separate top-level creates.\n"
        "If the user asks to break down or decompose an existing task but does not list "
        "exact subtasks, infer a reasonable small set when the target is a broad project "
        "or deliverable, then propose those children with create_node operations. If the "
        "target appears atomic, like a single email/call/message where child content "
        "depends on user-specific recipients or details, ask for those subtasks while "
        "carrying any pending metadata clauses.\n"
        "If your summary says you decomposed, broke down, added subtasks, or created "
        "children, the proposal must include matching create_node operations.\n"
        "When a task title is an exact or clearly more specific match, do not clarify "
        "because another task shares a shorter alias.\n"
    )
    # Native transport needs only protocol-level reminders. Domain workflow
    # guidance belongs to the shared core contract and explicitly loaded skills.
    continuation_proposal_rule = (
        "Continuation rule: semantically decide whether the latest user turn selects or "
        "supplies details for a prior recommendation/clarification. When it does, reuse "
        "the established target and evidence and call the appropriate typed proposal tool "
        "without broad rediscovery.\n"
    )
    recommendation_proposal_rule = (
        "Recommendation rule: read-only comparison returns advice; a selected concrete "
        "value or requested mutation returns a typed reviewable proposal.\n"
    )
    compound_mutation_rule = (
        "Compound mutation rule: preserve every requested clause in one typed proposal "
        "when possible; otherwise return one clarification that preserves pending clauses. "
        "Resolve all named existing-task candidates with the broad task-graph query before "
        "falling back to narrow title searches; do not search separately for work the user "
        "is explicitly asking to create. When one request mixes persisted tasks and new tasks, "
        "submit one task-tree plan covering every new task and put unscheduled existing task "
        "IDs in the same created_task_schedule groups. This gives one solver pass the complete "
        "calendar problem. For an existing-only initial schedule, submit the same schedule-only "
        "plan with target_task_ids and no structural operations. Let the compiler use stored or "
        "bounded task estimates when duration is omitted. Use a separate Planner handoff only "
        "to reschedule or remove an "
        "existing calendar block. "
        "A finite recurring task request uses one create_node plus recurring_schedule proposal; "
        "any new recurring task, including one that must relocate accepted conflicts, stays in "
        "that single task-tree proposal. Set recurring_schedule.conflict_relocation and let the "
        "compiler invoke Planner atomically; do not hand off the blocker tasks separately. "
        "a reminder to do something is not preference memory. For every created node, decide "
        "tree parentage independently from calendar placement. Use parent_temp_id for semantic "
        "children. When the user asks to schedule several items, every requested item must "
        "appear in the scheduling ownership set, including fixed commitments and resolved "
        "existing tasks. Do not omit an item merely because its duration or exact slot was not "
        "stated: preserve the user's date/window constraint and let Planner use saved history, "
        "preferences, calendar occupancy, and bounded duration estimates. Use schedule_groups "
        "when items have different dates or windows. Before submitting, inventory the current "
        "request clause by clause and verify that each clause has a typed task/tree/calendar "
        "representation in the one plan.\n"
        "Execution-detail rule: a missing job posting, message body, recipient detail, or "
        "research result does not block creation and scheduling of the requested task shell. "
        "Clarify only when a missing value changes task identity, a hard calendar constraint, "
        "or a destructive target.\n"
    )
    return (
        "You are Strategist AI inside a planning application.\n"
        "The shared operating contract is already installed as thread base instructions; "
        "follow it without restating it. Use the category catalog for workspace choice, "
        "using the active workspace as context and a tie-breaker rather than automatic "
        "target authority.\n"
        "This is a native Codex app-server tool turn. Infer semantic intent yourself. "
        "Do not rely on router keyword labels as meaning, and do not emulate tool calls "
        "inside JSON. Call Drasil tools when task, workload, proposal, or session facts "
        "are needed; answer directly when no task evidence is needed.\n"
        "Contract summary: read-only questions may answer directly after enough evidence; "
        "task create/update/delete/move/restructure requests must return a reviewable "
        "proposal or a clarification, never an applied-change claim. The allowlisted "
        "drasil-core MCP surface is authoritative: call set_goal_transition once using only "
        "{\"disposition\":\"new|continue|correct\",\"patches\":[]}; use sparse semantic patches "
        "for new or correct turns when they establish durable meaning, including recurrence.rule "
        "for a repeating schedule. The model—not prompt matching—decides whether scheduling is "
        "one-time or recurring, then expresses that decision through created_task_schedule or "
        "recurring_schedule. Never send transition or changed_fields, then use the high-level proposal "
        "or scheduling capability to preserve the complete request in one review. Internal "
        "ChangeSet, approval, transaction, provenance, and database contracts are not model "
        "tools. Every change described to the user must have matching typed proposal data.\n"
        "For a multi-step new task, create a top-level task and explicit child task.create "
        "children for the described workflow. Preserve notes and estimates in task fields, "
        "and include requested calendar placement in the same goal and review. Treat "
        "a user's explicit statement that this is a new task as authoritative; do not search "
        "for a similarly worded existing task.\n"
        "For different metadata values across multiple identified tasks, call "
        "submit_task_metadata_plan once with target_updates and exact target_task_id values; "
        "do not encode metadata-only work as tree operations.\n"
        "Follow the provider-independent operating contract in Current UI/session context and "
        "any skill you explicitly load. The contract owns scope, evidence, clarification, and "
        "approval boundaries; this transport prompt only defines native tool protocol.\n"
        f"{continuation_proposal_rule}"
        f"{recommendation_proposal_rule}"
        f"{compound_mutation_rule}"
        "For task-world questions, query the graph or a specialized read tool before "
        "answering. If the answer depends on a task title, alias, due date, priority, "
        "status, workload, tree structure, or previous task reference, call "
        "query_task_graph or the relevant task read tool before final. Do not say the "
        "task fact is unavailable until a task evidence tool has failed to find it.\n"
        "Do not create a mutation proposal for a read-only analysis request. A task mutation "
        "must be represented by a typed proposal-tool call or an explicit valid proposal "
        "object; prose about a possible change is not a proposal.\n"
        "For referential follow-ups like 'that task' or 'it', restate the resolved task "
        "title in the final summary so the user can see which task the answer applies to.\n"
        "When query_task_graph returns answer_candidate, use answer_candidate.summary as "
        "the preferred synthesized evidence and preserve every task referenced by "
        "answer_candidate.task_ids unless a later tool result directly contradicts it. "
        "Raw entity_matches are secondary search candidates, not the final answer.\n"
        "For normal conversation or external-world planning, answer from general "
        "reasoning and state limits for live/current data.\n"
        "The current UI/session context is already supplied below. Do not call "
        "get_context_surfaces only to restate it; call it only when pending proposal, "
        "recent session, or refreshed context details are needed.\n"
        "Never call the same read tool with identical arguments more than once in a "
        "turn. Reuse prior_tool_evidence when present. If that evidence is sufficient, "
        "reason from it and answer, clarify, or call the appropriate proposal tool now.\n"
        f"The final response object must have exactly these top-level keys: {final_shape}\n\n"
        "Core Drasil tools available directly:\n"
        f"{direct_tool_lines or '- none'}\n\n"
        f"{deferred_manifest}\n\n"
        f"Requested mode: {mode}\n\n"
        "Current UI/session context JSON:\n"
        f"{json.dumps(native_runtime_context, ensure_ascii=True)}\n\n"
        "Conversation transcript for this chat session:\n"
        f"{build_conversation_transcript(conversation_history or [])}\n\n"
        "User request:\n"
        f"{prompt}\n"
    )


def _compact_native_task(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    task = value.get("task") if isinstance(value.get("task"), dict) else value
    compact = {
        key: task.get(key)
        for key in (
            "task_id",
            "id",
            "title",
            "status",
            "priority",
            "planned_date",
            "deadline_at",
            "deadline",
            "time_estimate_minutes",
            "estimate_minutes",
            "parent_task_id",
            "path",
            "child_count",
            "child_sample",
            "is_backlog",
            "tags",
            "category_id",
            "category",
            "category_name",
        )
        if task.get(key) not in (None, "", [], {})
    }
    notes = task.get("notes")
    if isinstance(notes, str) and notes.strip():
        compact["notes"] = notes.strip()[:240]
    children = task.get("children")
    if isinstance(children, list):
        compact["children"] = [
            item
            for item in (_compact_native_task(child) for child in children[:12])
            if item is not None
        ]
    return compact or None


def _compact_native_tool_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    compact = {
        key: result.get(key)
        for key in (
            "result_kind",
            "read_only",
            "observation_summary",
            "answer_candidate",
            "evidence_coverage",
            "model_instruction",
            "scope",
            "count",
            "truncated",
            "tool_result_handle",
            "task_count",
            "active_count",
            "backlog_count",
            "category_count",
            "cleanup_candidate_count",
            "messy_branch_count",
            "organization_map_count",
            "organization_map_offset",
            "organization_map_complete",
            "next_organization_offset",
            "due_soon_count",
            "overdue_count",
            "high_priority_count",
            "stale_count",
            "found",
            "simulation_summary",
            "simulation_page",
        )
        if result.get(key) not in (None, "", [], {})
    }
    for key in ("task", "branch"):
        task = _compact_native_task(result.get(key))
        if task is not None:
            compact[key] = task
    for key in (
        "tasks",
        "children",
        "matches",
        "entity_matches",
        "task_snapshots",
        "due_soon",
        "overdue",
        "high_priority",
        "stale",
        "cleanup_candidates",
        "messy_branches",
        "organization_map",
    ):
        values = result.get(key)
        if not isinstance(values, list):
            continue
        compact_values = [
            item
            for item in (
                _compact_native_task(value)
                for value in values[:100 if key == "organization_map" else 10]
            )
            if item is not None
        ]
        if compact_values:
            compact[key] = compact_values
    if isinstance(result.get("destination_catalog"), list):
        compact["destination_catalog"] = result["destination_catalog"][:20]
    if result.get("compiled") is not None:
        compact["compiled"] = result.get("compiled")
    if isinstance(result.get("semantic_plan_input"), dict):
        compact["semantic_plan_input"] = result["semantic_plan_input"]
    for key in ("metadata_proposal", "branch_proposal", "preference_memory_proposal"):
        if isinstance(result.get(key), dict):
            compact[key] = result[key]
    if result.get("result_kind") == "loaded_skill":
        for key in ("capability", "skill", "recommended_tools", "evidence_requirements"):
            if result.get(key) not in (None, "", [], {}):
                compact[key] = result.get(key)
    return compact or result


def _native_tool_history_checkpoint(
    tool_history: list[dict[str, Any]],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Return recent unique tool evidence for a fresh native recovery turn."""

    checkpoint_reversed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in reversed(tool_history):
        if not isinstance(entry, dict):
            continue
        name = entry.get("tool_name") or entry.get("name")
        arguments = entry.get("arguments")
        result = entry.get("result")
        if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(result, dict):
            continue
        key = json.dumps(
            {"name": name, "arguments": arguments},
            sort_keys=True,
            ensure_ascii=True,
            default=str,
            separators=(",", ":"),
        )
        if key in seen:
            continue
        seen.add(key)
        checkpoint_reversed.append(
            {
                "tool_name": name,
                "arguments": arguments,
                "result": _compact_native_tool_result(result),
            }
        )
        if len(checkpoint_reversed) >= max(1, limit):
            break
    return list(reversed(checkpoint_reversed))


def build_direct_response_prompt(
    *,
    mode: str,
    prompt: str,
    runtime_context: dict[str, Any],
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    final_shape = (
        '{"summary":"<non-empty plain-English answer>",'
        '"action_proposal":null,'
        '"metadata_proposal":null,'
        '"branch_proposal":null,'
        '"preference_memory_proposal":null,'
        '"clarification":null}'
    )
    return (
        "You are Strategist AI inside a planning application.\n"
        "This turn is a direct no-tool response route. Return only JSON matching the provided output schema.\n"
        "No task tools are available for this turn. Do not require a selected task, staged item, or pending proposal.\n"
        "Answer the user's latest request directly from general reasoning and the visible transcript.\n"
        "Keep the summary concise: usually 2 to 4 sentences, unless the user asked for a longer answer.\n"
        "For external-world planning, give stable non-live guidance. Do not browse, cite sources, or look up current schedules, prices, weather, or availability.\n"
        "If current external data would matter, state the limitation briefly and tell the user what to verify.\n"
        "Do not schedule calendar blocks, edit files, run shell commands, or mention internal ids in prose.\n"
        "Only include a proposal object if the user explicitly asked to create or mutate Drasil tasks; otherwise set proposal fields to null.\n"
        f"The final response object must have exactly these top-level keys: {final_shape}\n\n"
        f"Requested mode: {mode}\n\n"
        "Current UI/session context JSON:\n"
        f"{json.dumps(runtime_context, ensure_ascii=True)}\n\n"
        "Conversation transcript for this chat session:\n"
        f"{build_conversation_transcript(conversation_history or [])}\n\n"
        "User request:\n"
        f"{prompt}\n"
    )


def build_tool_history_fallback_payload(
    *,
    prompt: str,
    tool_history: list[dict[str, Any]],
) -> dict[str, Any]:
    def task_title(task: dict[str, Any]) -> str:
        return str(task.get("title") or task.get("task_title") or "").strip()

    def task_detail(task: dict[str, Any]) -> str:
        details = [
            str(task.get("priority_label") or task.get("priority") or "unknown priority"),
            str(task.get("deadline") or task.get("deadline_at") or "No deadline"),
            str(task.get("estimate") or task.get("time_estimate_minutes") or "No estimate"),
        ]
        return " | ".join(details)

    def fact_summary_from_task(task: dict[str, Any]) -> str:
        title = task_title(task) or "That task"
        lowered = prompt.lower()
        if any(token in lowered for token in ("when", "due", "deadline")):
            deadline = str(task.get("deadline") or task.get("deadline_at") or "").strip()
            return (
                f"{title} is due on {deadline}."
                if deadline and deadline.lower() != "no deadline"
                else f"{title} does not have a deadline yet."
            )
        if "priority" in lowered:
            return f"{title} is {task.get('priority_label') or task.get('priority') or 'unknown priority'}."
        if "status" in lowered or "done" in lowered:
            return f"{title} is currently {task.get('status') or 'unknown status'}."
        return f"{title}: {task_detail(task)}."

    for entry in reversed(tool_history):
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        answer_candidate = result.get("answer_candidate")
        if isinstance(answer_candidate, dict):
            summary = str(answer_candidate.get("summary") or "").strip()
            if summary:
                return _empty_final(summary=summary)
        matches = result.get("matches")
        if isinstance(matches, list) and matches:
            first_match = matches[0] if isinstance(matches[0], dict) else {}
            task = first_match.get("task") if isinstance(first_match.get("task"), dict) else None
            if task is not None:
                return _empty_final(summary=fact_summary_from_task(task))
        tasks = result.get("tasks")
        if isinstance(tasks, list):
            lines: list[str] = []
            for task in tasks[:12]:
                if not isinstance(task, dict):
                    continue
                title = str(task.get("title") or "").strip()
                if title:
                    lines.append(f"- {title} ({task_detail(task)})")
            if lines:
                return _empty_final(summary="Here is the retrieved task set I found:\n" + "\n".join(lines))
    return _empty_final(
        summary=(
            "I could not complete the model tool loop for that request. "
            "Try asking for a narrower tree review, or name the branch you want me to inspect first."
        )
    )


def _empty_final(*, summary: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "semantic_plan_input": None,
        "action_proposal": None,
        "metadata_proposal": None,
        "branch_proposal": None,
        "preference_memory_proposal": None,
        "clarification": None,
    }


def _terminal_proposal_payload_from_tool_result(
    *,
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name == "draft_change_set":
        # A native v2 draft is shadow evidence, not a legacy proposal family and
        # not a terminal approval/apply response. Keep it in model context so the
        # live path can still produce its established authoritative proposal.
        return None
    if tool_name in {
        "submit_task_metadata_plan",
        "submit_task_tree_plan",
        "submit_preference_plan",
        "submit_preference_management_plan",
        "submit_preference_cleanup_plan",
    }:
        # The host collects all successful family submissions from the complete
        # tool trace. Terminating here discarded later clauses in compound turns.
        return None
    if tool_name == "propose_metadata_update":
        proposal = result.get("metadata_proposal")
        proposal_key = "metadata_proposal"
        default_summary = "I drafted a reviewable metadata proposal."
    elif tool_name == "propose_tree_operation":
        proposal = result.get("branch_proposal")
        proposal_key = "branch_proposal"
        default_summary = "I drafted a reviewable task-tree proposal."
    elif tool_name == "propose_preference_memory":
        proposal = result.get("preference_memory_proposal") or result.get("preferenceMemoryProposal")
        proposal_key = "preference_memory_proposal"
        default_summary = "I drafted a reviewable memory proposal."
    else:
        return None
    if result.get("compiled") is not True:
        clarification = result.get("clarification")
        if isinstance(clarification, dict):
            payload = _empty_final(
                summary=str(clarification.get("message") or "").strip()
                or "I need one clarification before drafting the proposal."
            )
            payload["clarification"] = clarification
            return payload
        return None
    if not isinstance(proposal, dict):
        return None
    payload = _empty_final(
        summary=str(proposal.get("summary") or "").strip() or default_summary
    )
    payload[proposal_key] = proposal
    return payload


def parse_tool_step_payload(payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("kind")
    if kind not in {"tool_call", "final"}:
        raise HTTPException(status_code=502, detail="Strategist returned an invalid tool-loop step.")
    return payload


def invoke_codex_json_schema_via_exec(
    *,
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    output_filename: str,
    configured_model: str | None,
    timeout_seconds: int = 90,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    codex_path = require_codex_cli_path("Strategist")
    ensure_codex_login(codex_path)
    repo_root = Path(__file__).resolve().parents[4]

    with tempfile.TemporaryDirectory(prefix="drasil-strategist-json-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        schema_path = temp_dir_path / f"{schema_name}.json"
        output_path = temp_dir_path / output_filename
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [codex_path, "exec"]
        if configured_model:
            command.extend(["--model", configured_model])
        if reasoning_effort:
            command.extend(
                ["--config", f'model_reasoning_effort="{reasoning_effort}"'],
            )
        command.extend(
            [
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
        )
        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=account_process_environment(),
                timeout=timeout_seconds,
                check=False,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired as error:
            raise HTTPException(
                status_code=504,
                detail="Strategist model step timed out before it could return structured JSON.",
            ) from error
        if result.returncode != 0:
            detail = extract_codex_cli_error(result.stdout or "", result.stderr or "")
            raise HTTPException(status_code=502, detail=detail or "Codex strategist request failed.")
        if not output_path.exists():
            raise HTTPException(status_code=502, detail="Codex strategist did not produce structured JSON.")
        return parse_json_object_text(output_path.read_text(encoding="utf-8"))


def invoke_codex_json_schema(
    *,
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    output_filename: str,
    configured_model: str | None,
    timeout_seconds: int = 90,
    trace_collector: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if os.getenv("STRATEGIST_CODEX_APP_SERVER_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        try:
            ensure_codex_login(require_codex_cli_path("Strategist"))
            from app.services.agent_kernel.codex_app_server import (
                CodexAppServerIdleTimeoutError,
                CodexAppServerMalformedStructuredOutputError,
                CodexAppServerUnavailableError,
                invoke_codex_app_server_json_schema,
            )

            payload, app_server_trace = invoke_codex_app_server_json_schema(
                prompt=prompt,
                schema=schema,
                configured_model=configured_model,
                timeout_seconds=timeout_seconds,
                reasoning_effort=reasoning_effort,
            )
            if trace_collector is not None:
                trace_collector["app_server_trace"] = app_server_trace
            return payload
        except CodexAppServerIdleTimeoutError as error:
            if trace_collector is not None:
                trace_collector["provider_harness_error"] = "codex_app_server_idle_timeout"
                trace_collector["app_server_trace"] = error.trace
            raise HTTPException(
                status_code=504,
                detail=(
                    "Strategist Codex app-server turn stopped making meaningful "
                    "progress before returning structured JSON."
                ),
            ) from error
        except CodexAppServerUnavailableError as error:
            if trace_collector is not None:
                trace_collector["provider_harness_error"] = "codex_app_server_unavailable"
            raise HTTPException(
                status_code=502,
                detail=f"Codex app-server transport failed before returning structured JSON. {error}",
            ) from error
        except CodexAppServerMalformedStructuredOutputError as error:
            if trace_collector is not None:
                trace_collector["provider_harness_error"] = "codex_app_server_malformed_structured_output"
                trace_collector["app_server_trace"] = error.trace
                trace_collector["app_server_parse_error"] = error.parse_error
            raise HTTPException(
                status_code=502,
                detail="Codex app-server completed but returned structured-output text that could not be parsed.",
            ) from error

    return invoke_codex_json_schema_via_exec(
        prompt=prompt,
        schema=schema,
        schema_name=schema_name,
        output_filename=output_filename,
        configured_model=configured_model,
        timeout_seconds=timeout_seconds,
        reasoning_effort=reasoning_effort,
    )


@observe_generation(
    name="codex.direct-response",
    provider="codex-cli",
    model_argument="configured_model",
    prompt_argument="prompt",
)
def invoke_codex_direct_response(
    *,
    prompt: str,
    configured_model: str | None,
    timeout_seconds: int | None = None,
    trace_collector: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    parsed = invoke_codex_json_schema(
        prompt=prompt,
        schema=STRATEGIST_DIRECT_RESPONSE_SCHEMA,
        schema_name="strategist-direct-response-schema",
        output_filename="strategist-response-output.json",
        configured_model=configured_model,
        timeout_seconds=timeout_seconds or codex_structured_step_timeout_seconds(),
        trace_collector=trace_collector,
        reasoning_effort=(
            reasoning_effort
            or os.getenv("STRATEGIST_DIRECT_RESPONSE_REASONING_EFFORT", "").strip()
            or None
        ),
    )
    return parsed, None


def invoke_codex_structured_step(
    *,
    prompt: str,
    configured_model: str | None,
    provider_session_id: str | None,
    timeout_seconds: int | None = None,
    trace_collector: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    app_server_enabled = os.getenv("STRATEGIST_CODEX_APP_SERVER_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if app_server_enabled:
        parsed = invoke_codex_json_schema(
            prompt=prompt,
            schema=STRATEGIST_TOOL_STEP_SCHEMA,
            schema_name="strategist-tool-step-schema",
            output_filename="strategist-step-output.json",
            configured_model=configured_model,
            timeout_seconds=timeout_seconds or codex_structured_step_timeout_seconds(),
            trace_collector=trace_collector,
            reasoning_effort=reasoning_effort,
        )
        return parse_tool_step_payload(parsed), None

    codex_path = require_codex_cli_path("Strategist")
    ensure_codex_login(codex_path)
    repo_root = Path(__file__).resolve().parents[4]

    with tempfile.TemporaryDirectory(prefix="drasil-strategist-step-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        schema_path = temp_dir_path / "strategist-step-schema.json"
        output_path = temp_dir_path / "strategist-step-output.json"
        if provider_session_id:
            command = [codex_path, "exec", "resume"]
            if configured_model:
                command.extend(["--model", configured_model])
            if reasoning_effort:
                command.extend(
                    ["--config", f'model_reasoning_effort="{reasoning_effort}"'],
                )
            command.extend(["--skip-git-repo-check", "--output-last-message", str(output_path), provider_session_id, "-"])
        else:
            schema_path.write_text(json.dumps(STRATEGIST_TOOL_STEP_SCHEMA), encoding="utf-8")
            command = [codex_path, "exec"]
            if configured_model:
                command.extend(["--model", configured_model])
            if reasoning_effort:
                command.extend(
                    ["--config", f'model_reasoning_effort="{reasoning_effort}"'],
                )
            command.extend(
                [
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--color",
                    "never",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
            )

        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=account_process_environment(),
                timeout=timeout_seconds or codex_structured_step_timeout_seconds(),
                check=False,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired as error:
            raise HTTPException(
                status_code=504,
                detail="Strategist model step timed out before it could return a structured answer.",
            ) from error
        if result.returncode != 0:
            detail = extract_codex_cli_error(result.stdout or "", result.stderr or "")
            raise HTTPException(status_code=502, detail=detail or "Codex strategist request failed.")
        if not output_path.exists():
            raise HTTPException(status_code=502, detail="Codex strategist did not produce a structured response.")
        parsed = parse_json_object_text(output_path.read_text(encoding="utf-8"))
        return parse_tool_step_payload(parsed), parse_codex_session_id(result.stdout or "", result.stderr or "") or provider_session_id


@observe_generation(
    name="codex.tool-loop",
    provider="codex-cli",
    model_argument="configured_model",
    prompt_argument="prompt",
)
def invoke_codex_tool_loop(
    *,
    mode: str,
    prompt: str,
    runtime_context: dict[str, Any],
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]] | None,
    conversation_history: list[dict[str, str]] | None = None,
    provider_session_id: str | None,
    configured_model: str | None,
    reasoning_effort: str | None = None,
    today_override: date | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
    initial_tool_history: list[dict[str, Any]] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    tool_executor: StrategistToolExecutor | None = None,
    model_step_trace: list[dict[str, Any]] | None = None,
    step_timeout_seconds: int | None = None,
    loop_timeout_seconds: int | None = None,
    max_model_steps: int | None = None,
    max_tool_calls: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    next_session_id = provider_session_id
    available_tool_definitions = STRATEGIST_TOOL_DEFINITIONS if tool_definitions is None else tool_definitions
    execute_tool = tool_executor or _missing_authoritative_tool_executor
    tool_history: list[dict[str, Any]] = [dict(item) for item in (initial_tool_history or [])]
    tool_result_cache: dict[str, dict[str, Any]] = {}
    configured_step_timeout = step_timeout_seconds or codex_structured_step_timeout_seconds()
    configured_loop_timeout = loop_timeout_seconds or codex_tool_loop_timeout_seconds()
    configured_max_steps = max_model_steps or codex_tool_loop_max_steps()
    configured_max_tool_calls = max_tool_calls or max(1, configured_max_steps)
    loop_deadline = perf_counter() + configured_loop_timeout if configured_loop_timeout > 0 else None

    if codex_native_tool_loop_enabled() and os.getenv("STRATEGIST_CODEX_APP_SERVER_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        ensure_codex_login(require_codex_cli_path("Strategist"))
        from app.services.agent_kernel.codex_app_server import (
            CodexAppServerIdleTimeoutError,
            CodexAppServerMalformedStructuredOutputError,
            CodexAppServerUnavailableError,
            _terminal_proposal_payload_from_tool_result,
            invoke_codex_app_server_json_schema,
            invoke_codex_app_server_native_tool_loop,
        )

        native_prompt = build_native_tool_loop_prompt(
            mode=mode,
            prompt=prompt,
            runtime_context=runtime_context,
            conversation_history=conversation_history,
            tool_definitions=available_tool_definitions,
            tool_history=tool_history,
        )
        native_tool_names = {
            str(tool.get("name") or "").strip()
            for tool in available_tool_definitions
            if str(tool.get("name") or "").strip()
        }
        bounded_metadata_lane = native_tool_names == {
            "submit_task_metadata_plan"
        } and any(
            isinstance(item, dict)
            and str(item.get("tool_name") or item.get("name") or "").strip()
            == "search_entities"
            and isinstance(item.get("result"), dict)
            and not item["result"].get("error")
            for item in tool_history
        )
        contract_repair_lane = mode == "agent-kernel-contract-retry"
        semantic_submission_tool_names = frozenset(
            name
            for name in native_tool_names
            if name.startswith("submit_") or name == "handoff_to_planner"
        )

        def has_typed_terminal_boundary(payload: Any) -> bool:
            if not isinstance(payload, dict):
                return False
            return any(
                isinstance(payload.get(key), dict)
                for key in (
                    "action_proposal",
                    "metadata_proposal",
                    "branch_proposal",
                    "preference_memory_proposal",
                )
            )
        native_started_at = perf_counter()
        trace_entry: dict[str, Any] = {
            "step_index": 1,
            "phase": "native_tool_loop",
            "prompt_chars": len(native_prompt),
            "timeout_seconds": configured_loop_timeout,
            "provider_session_resumed": False,
            "native_dynamic_tools": True,
            "contract_repair_lane": contract_repair_lane,
            "available_tool_names": sorted(native_tool_names),
            "semantic_submission_tool_names": sorted(
                semantic_submission_tool_names
            ),
        }

        def execute_native_tool(tool_name: str, tool_arguments: dict[str, Any]) -> dict[str, Any]:
            return execute_tool(
                tool_name,
                tool_arguments,
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                staged_node_ids=staged_node_ids,
                working_set=working_set,
                today_override=today_override,
            )

        native_malformed_fallback = False
        transient_retry_limit = codex_transient_disconnect_retries()
        transient_retry_count = 0
        contract_repair_retry_count = 0
        contract_repair_attempt_limit = max(
            1,
            min(configured_max_steps, 2),
        )
        app_server_attempts: list[dict[str, Any]] = []
        while True:
            if loop_deadline is None:
                native_timeout_seconds = configured_loop_timeout
            else:
                remaining_seconds = loop_deadline - perf_counter()
                if remaining_seconds <= 0:
                    raise HTTPException(
                        status_code=504,
                        detail=(
                            "Strategist model loop exceeded its turn time budget before "
                            "returning a structured answer."
                        ),
                    )
                native_timeout_seconds = max(
                    1,
                    min(configured_loop_timeout, int(remaining_seconds)),
                )
            try:
                attempt_prompt = native_prompt
                if contract_repair_lane and contract_repair_retry_count:
                    recent_failures = [
                        {
                            "tool": str(call.get("name") or "")[:96],
                            "error": str(call.get("result", {}).get("error") or "")[:240],
                            "compiled": call.get("result", {}).get("compiled"),
                        }
                        for call in (tool_trace or [])[-4:]
                        if isinstance(call, dict)
                        and isinstance(call.get("result"), dict)
                        and (
                            call["result"].get("error")
                            or call["result"].get("compiled") is False
                        )
                    ]
                    attempt_prompt += (
                        "\n\nThe prior contract-repair attempt did not produce a valid typed "
                        "semantic boundary. This is the final protocol attempt. Call one "
                        "available semantic submission tool now with the complete request "
                        "ledger. Do not return a direct final response or an ungrounded "
                        "clarification; the semantic tool result can identify a genuinely "
                        "missing field. Prior validator failures JSON: "
                        f"{json.dumps(recent_failures, ensure_ascii=True)}"
                    )
                attempt_tool_trace_start = len(tool_trace or [])
                forced_tool_definition = next(
                    (
                        definition
                        for definition in available_tool_definitions
                        if str(definition.get("name") or "")
                        in semantic_submission_tool_names
                    ),
                    None,
                )
                force_single_semantic_tool = bool(
                    contract_repair_lane
                    and len(semantic_submission_tool_names) == 1
                    and isinstance(forced_tool_definition, dict)
                    and isinstance(forced_tool_definition.get("parameters"), dict)
                )
                if force_single_semantic_tool:
                    forced_tool_name = next(iter(semantic_submission_tool_names))
                    forced_envelope, schema_trace = invoke_codex_app_server_json_schema(
                        prompt=(
                            attempt_prompt
                            + "\n\nThis provider protocol does not expose a required-tool flag. "
                            + f"Return {forced_tool_name}'s complete arguments encoded in the "
                            + "arguments_json string. The host will parse, execute, and validate "
                            + "that semantic tool atomically. Use exactly this tool argument "
                            + "schema as the contract:\n"
                            + json.dumps(
                                forced_tool_definition["parameters"],
                                ensure_ascii=True,
                                separators=(",", ":"),
                            )
                        ),
                        # App-server structured outputs require every object
                        # property to be listed as required, whereas semantic
                        # tools intentionally have optional fields. A strict
                        # string envelope satisfies the provider protocol; the
                        # canonical host tool schema remains the authority.
                        schema=SEMANTIC_TOOL_ARGUMENT_ENVELOPE_SCHEMA,
                        configured_model=configured_model,
                        timeout_seconds=native_timeout_seconds,
                        reasoning_effort=reasoning_effort,
                    )
                    raw_forced_arguments = forced_envelope.get("arguments_json")
                    if not isinstance(raw_forced_arguments, str):
                        raise ValueError(
                            "semantic tool argument envelope omitted arguments_json"
                        )
                    forced_arguments = parse_json_object_text(raw_forced_arguments)
                    forced_result = execute_native_tool(
                        forced_tool_name,
                        forced_arguments,
                    )
                    if tool_trace is not None:
                        tool_trace.append(
                            {
                                "name": forced_tool_name,
                                "arguments": forced_arguments,
                                "result": forced_result,
                                "duration_ms": schema_trace.get("duration_ms"),
                                "schema_bound_semantic_submission": True,
                            }
                        )
                    parsed = _terminal_proposal_payload_from_tool_result(
                        tool_name=forced_tool_name,
                        result=forced_result,
                        terminal_semantic_plan_tool_names=semantic_submission_tool_names,
                    ) or {
                        "decision": {
                            "kind": "clarification"
                            if isinstance(forced_result.get("clarification"), dict)
                            else "mutation",
                            "reason": "The semantic submission was validated by the host.",
                        },
                        "summary": str(
                            forced_result.get("clarification", {}).get("message")
                            if isinstance(forced_result.get("clarification"), dict)
                            else forced_result.get("error")
                            or "The semantic submission requires repair."
                        ),
                        "action_proposal": None,
                        "metadata_proposal": None,
                        "branch_proposal": None,
                        "preference_memory_proposal": None,
                        "clarification": (
                            forced_result.get("clarification")
                            if isinstance(forced_result.get("clarification"), dict)
                            else None
                        ),
                    }
                    app_server_trace = {
                        **schema_trace,
                        "dynamic_tool_call_count": 1,
                        "dynamic_tool_names": [forced_tool_name],
                        "schema_bound_semantic_submission": True,
                    }
                else:
                    parsed, app_server_trace = invoke_codex_app_server_native_tool_loop(
                        prompt=attempt_prompt,
                        schema=STRATEGIST_JSON_SCHEMA,
                        configured_model=configured_model,
                        timeout_seconds=native_timeout_seconds,
                        tool_definitions=available_tool_definitions,
                        tool_executor=execute_native_tool,
                        tool_trace=tool_trace,
                        reasoning_effort=reasoning_effort,
                        max_tool_calls=configured_max_tool_calls,
                        base_instructions=(
                        "You author one exact Drasil metadata mutation by calling the "
                        "available semantic submission tool once. The server owns target "
                        "validation, ChangeSet compilation, approval, and application."
                        if bounded_metadata_lane
                        else (
                            "Repair the current Drasil mutation boundary. Produce one "
                            "complete typed semantic submission. A direct final response, "
                            "including an ungrounded clarification, is not terminal in this "
                            "repair lane."
                            if contract_repair_lane
                            else None
                        )
                    ),
                        developer_instructions=(
                        "Use the prefetched exact task and explicit user value. Call the "
                        "single available tool once; do not produce prose first."
                        if bounded_metadata_lane
                        else (
                            "Conserve every clause in the user's request. Call one of the "
                            "available semantic submission tools now. Let the tool validator "
                            "report any specific missing field; do not ask first."
                            if contract_repair_lane
                            else None
                        )
                    ),
                        terminal_semantic_plan_tool_names=(
                        frozenset({"submit_task_metadata_plan"})
                        if bounded_metadata_lane
                        else semantic_submission_tool_names
                        if contract_repair_lane
                        else frozenset()
                    ),
                        reasoning_summary=(
                        None if bounded_metadata_lane else "auto"
                        ),
                    )
                app_server_attempts.append(
                    {
                        "attempt": transient_retry_count + 1,
                        "status": "ok",
                        "trace": app_server_trace,
                    }
                )
                attempt_tool_calls = (tool_trace or [])[attempt_tool_trace_start:]
                successful_semantic_boundary = any(
                    isinstance(call, dict)
                    and str(call.get("name") or "") in semantic_submission_tool_names
                    and isinstance(call.get("result"), dict)
                    and not call["result"].get("error")
                    and call["result"].get("compiled") is not False
                    and (
                        call["result"].get("compiled") is True
                        or isinstance(call["result"].get("terminal_response"), dict)
                        or isinstance(call["result"].get("planner_handoff"), dict)
                    )
                    for call in attempt_tool_calls
                )
                if (
                    contract_repair_lane
                    and not successful_semantic_boundary
                    and not has_typed_terminal_boundary(parsed)
                    and contract_repair_retry_count + 1
                    < contract_repair_attempt_limit
                ):
                    app_server_attempts[-1]["status"] = "contract_miss"
                    contract_repair_retry_count += 1
                    continue
                break
            except CodexAppServerIdleTimeoutError as error:
                trace_entry.update(
                    {
                        "status": "timeout",
                        "duration_ms": _elapsed_ms(native_started_at),
                        "error_status_code": 504,
                        "error_detail": str(error),
                        "provider_harness_error": "codex_app_server_native_tool_loop_idle_timeout",
                        "app_server_trace": error.trace,
                        "app_server_attempts": app_server_attempts,
                        "transient_retry_count": transient_retry_count,
                    }
                )
                if model_step_trace is not None:
                    model_step_trace.append(trace_entry)
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Strategist Codex native tool turn stopped making meaningful "
                        "progress before returning structured output."
                    ),
                ) from error
            except CodexAppServerUnavailableError as error:
                error_trace = error.trace if isinstance(error.trace, dict) else {}
                app_server_attempts.append(
                    {
                        "attempt": transient_retry_count + 1,
                        "status": "error",
                        "error": str(error),
                        "trace": error_trace,
                    }
                )
                if (
                    transient_retry_count < transient_retry_limit
                    and _is_safe_retryable_codex_disconnect(error)
                ):
                    transient_retry_count += 1
                    continue
                trace_entry.update(
                    {
                        "status": "error",
                        "duration_ms": _elapsed_ms(native_started_at),
                        "error_status_code": 502,
                        "error_detail": str(error),
                        "provider_harness_error": "codex_app_server_native_tool_loop_unavailable",
                        "app_server_trace": error_trace,
                        "app_server_attempts": app_server_attempts,
                        "transient_retry_count": transient_retry_count,
                    }
                )
                if model_step_trace is not None:
                    model_step_trace.append(trace_entry)
                raise HTTPException(
                    status_code=502,
                    detail=f"Codex app-server native tool transport failed. {error}",
                ) from error
            except CodexAppServerMalformedStructuredOutputError as error:
                trace_entry.update(
                    {
                        "status": "error",
                        "duration_ms": _elapsed_ms(native_started_at),
                        "error_status_code": 502,
                        "error_detail": str(error),
                        "provider_harness_error": "codex_app_server_native_tool_loop_malformed_output",
                        "app_server_trace": error.trace,
                        "app_server_parse_error": error.parse_error,
                        "app_server_attempts": app_server_attempts,
                        "transient_retry_count": transient_retry_count,
                    }
                )
                if model_step_trace is not None:
                    model_step_trace.append(trace_entry)
                native_malformed_fallback = True
                break
        if not native_malformed_fallback:
            parsed_decision = (
                parsed.get("decision")
                if isinstance(parsed, dict)
                and isinstance(parsed.get("decision"), dict)
                else {}
            )
            parsed_clarification = (
                parsed.get("clarification")
                if isinstance(parsed, dict)
                else None
            )
            clarification_message = (
                parsed_clarification.get("message")
                or parsed_clarification.get("question")
                or parsed_clarification.get("summary")
                if isinstance(parsed_clarification, dict)
                else parsed_clarification
                if isinstance(parsed_clarification, str)
                else None
            )
            trace_entry.update(
                {
                    "status": "ok",
                    "duration_ms": _elapsed_ms(native_started_at),
                    "kind": "final",
                    "app_server_trace": app_server_trace,
                    "dynamic_tool_call_count": app_server_trace.get("dynamic_tool_call_count"),
                    "dynamic_tool_names": app_server_trace.get("dynamic_tool_names"),
                    "app_server_attempts": app_server_attempts,
                    "transient_retry_count": transient_retry_count,
                    "contract_repair_retry_count": contract_repair_retry_count,
                    "provider_transport_recovered": transient_retry_count > 0,
                    "terminal_diagnostic": {
                        "decision_kind": str(
                            parsed_decision.get("kind") or ""
                        )[:40],
                        "summary": str(
                            parsed.get("summary") or ""
                            if isinstance(parsed, dict)
                            else ""
                        )[:320],
                        "clarification_present": isinstance(
                            parsed_clarification, (dict, str)
                        ),
                        "clarification_message": (
                            str(clarification_message)[:320]
                            if clarification_message
                            else None
                        ),
                    },
                }
            )
            if model_step_trace is not None:
                model_step_trace.append(trace_entry)
            return parsed, None

    for item in tool_history:
        tool_name = item.get("tool_name")
        tool_arguments = item.get("arguments")
        tool_result_payload = item.get("result")
        if isinstance(tool_name, str) and isinstance(tool_arguments, dict) and isinstance(tool_result_payload, dict):
            cache_key = json.dumps({"name": tool_name, "arguments": tool_arguments}, sort_keys=True, separators=(",", ":"))
            tool_result_cache[cache_key] = dict(tool_result_payload)

    next_prompt = build_tool_loop_prompt(
        mode=mode,
        prompt=prompt,
        runtime_context=runtime_context,
        conversation_history=conversation_history,
        tool_result=tool_history[-1] if tool_history else None,
        tool_history=tool_history or None,
        tool_definitions=available_tool_definitions,
    )

    def remaining_step_timeout() -> int:
        if loop_deadline is None:
            return configured_step_timeout
        remaining = loop_deadline - perf_counter()
        if remaining <= 0:
            raise HTTPException(
                status_code=504,
                detail="Strategist model loop exceeded its turn time budget before returning a structured answer.",
            )
        return max(1, min(configured_step_timeout, int(remaining)))

    def call_structured_step(*, prompt_text: str, step_index: int, phase: str) -> dict[str, Any]:
        timeout_for_step = remaining_step_timeout()
        started_at = perf_counter()
        trace_entry: dict[str, Any] = {
            "step_index": step_index,
            "phase": phase,
            "prompt_chars": len(prompt_text),
            "timeout_seconds": timeout_for_step,
            "provider_session_resumed": bool(next_session_id),
        }
        try:
            step_payload, parsed_session_id = invoke_codex_structured_step(
                prompt=prompt_text,
                configured_model=configured_model,
                provider_session_id=next_session_id,
                timeout_seconds=timeout_for_step,
                trace_collector=trace_entry,
                reasoning_effort=reasoning_effort,
            )
        except HTTPException as error:
            trace_entry.update(
                {
                    "status": "timeout" if error.status_code == 504 else "error",
                    "duration_ms": _elapsed_ms(started_at),
                    "error_status_code": error.status_code,
                    "error_detail": str(error.detail),
                }
            )
            if model_step_trace is not None:
                model_step_trace.append(trace_entry)
            raise
        trace_entry.update({"status": "ok", "duration_ms": _elapsed_ms(started_at), "kind": step_payload.get("kind")})
        requested_tool = step_payload.get("requested_tool")
        if isinstance(requested_tool, dict) and isinstance(requested_tool.get("name"), str):
            trace_entry["requested_tool_name"] = requested_tool["name"]
        if model_step_trace is not None:
            model_step_trace.append(trace_entry)
        nonlocal_next_session_id[0] = parsed_session_id
        return step_payload

    nonlocal_next_session_id: list[str | None] = [next_session_id]

    for step_index in range(1, configured_max_steps + 1):
        next_session_id = nonlocal_next_session_id[0]
        step_payload = call_structured_step(prompt_text=next_prompt, step_index=step_index, phase="tool_loop")
        next_session_id = nonlocal_next_session_id[0]
        if step_payload.get("kind") == "final":
            raw_final_payload = step_payload.get("final_payload_json")
            if not isinstance(raw_final_payload, str) or not raw_final_payload.strip():
                raise HTTPException(status_code=502, detail="Codex strategist returned an empty final payload.")
            try:
                final_payload = json.loads(raw_final_payload)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=502, detail="Codex strategist returned invalid final JSON.") from error
            if not isinstance(final_payload, dict):
                raise HTTPException(status_code=502, detail="Codex strategist returned an unexpected final payload.")
            return final_payload, next_session_id

        requested_tool = step_payload.get("requested_tool")
        if not isinstance(requested_tool, dict):
            raise HTTPException(status_code=502, detail="Strategist requested an invalid tool call.")
        tool_name = requested_tool.get("name")
        raw_arguments = requested_tool.get("arguments_json")
        if not isinstance(tool_name, str) or not isinstance(raw_arguments, str):
            raise HTTPException(status_code=502, detail="Strategist tool call payload was malformed.")
        try:
            tool_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=502, detail="Strategist returned invalid JSON for tool arguments.") from error
        if not isinstance(tool_arguments, dict):
            raise HTTPException(status_code=502, detail="Strategist tool arguments must decode to an object.")

        cache_key = json.dumps({"name": tool_name, "arguments": tool_arguments}, sort_keys=True, separators=(",", ":"))
        if cache_key in tool_result_cache:
            executed_result = dict(tool_result_cache[cache_key])
            executed_result["cached"] = True
            tool_duration_ms = 0.0
        else:
            tool_started = perf_counter()
            executed_result = execute_tool(
                tool_name,
                tool_arguments,
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                staged_node_ids=staged_node_ids,
                working_set=working_set,
                today_override=today_override,
            )
            tool_duration_ms = _elapsed_ms(tool_started)
            tool_result_cache[cache_key] = executed_result
        tool_result = {"tool_name": tool_name, "arguments": tool_arguments, "result": executed_result}
        tool_history.append(tool_result)
        if tool_trace is not None:
            tool_trace.append({"name": tool_name, "arguments": tool_arguments, "result": executed_result, "duration_ms": tool_duration_ms})
        terminal_payload = _terminal_proposal_payload_from_tool_result(
            tool_name=tool_name,
            result=executed_result,
        )
        if terminal_payload is not None:
            return terminal_payload, next_session_id
        next_prompt = build_tool_loop_prompt(
            mode=mode,
            prompt=prompt,
            runtime_context=runtime_context,
            conversation_history=conversation_history,
            tool_result=tool_result,
            tool_history=tool_history,
            tool_definitions=available_tool_definitions,
        )

    if tool_history:
        final_prompt = (
            build_tool_loop_prompt(
                mode=mode,
                prompt=prompt,
                runtime_context=runtime_context,
                conversation_history=conversation_history,
                tool_result=tool_history[-1],
                tool_history=tool_history,
                tool_definitions=available_tool_definitions,
            )
            + "\nTool budget is exhausted. You must respond with kind='final' now using the tool results already shown. Do not request another tool.\n"
        )
        try:
            next_session_id = nonlocal_next_session_id[0]
            step_payload = call_structured_step(prompt_text=final_prompt, step_index=configured_max_steps + 1, phase="forced_final")
            next_session_id = nonlocal_next_session_id[0]
            if step_payload.get("kind") == "final":
                raw_final_payload = step_payload.get("final_payload_json")
                if isinstance(raw_final_payload, str) and raw_final_payload.strip():
                    final_payload = json.loads(raw_final_payload)
                    if isinstance(final_payload, dict):
                        return final_payload, next_session_id
        except (HTTPException, json.JSONDecodeError):
            pass
        return build_tool_history_fallback_payload(prompt=prompt, tool_history=tool_history), next_session_id

    raise HTTPException(status_code=502, detail="Strategist exceeded the tool-call limit before reaching a final answer.")


def extract_openai_output_text(response_payload: dict[str, Any]) -> str:
    output = response_payload.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if (
                isinstance(content_item, dict)
                and content_item.get("type") == "output_text"
                and isinstance(content_item.get("text"), str)
            ):
                parts.append(content_item["text"])
    return "".join(parts).strip()


@observe_generation(
    name="openai.responses",
    provider="openai-api",
    request_body_argument="request_body",
)
def call_openai_responses_api(
    *,
    api_key: str,
    request_body: dict[str, Any],
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    from app.services.agent_stream import check_cancelled, read_responses_stream
    check_cancelled()
    request_body = {**request_body, "stream": True}
    if str(request_body.get("model", "")).startswith(("gpt-5", "gpt-6", "o3", "o4")):
        request_body["reasoning"] = {"summary": "auto", **request_body.get("reasoning", {})}
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        from app.core.hosted_budget import remaining_timeout
        with urllib.request.urlopen(request, timeout=remaining_timeout(timeout_seconds)) as response:
            return read_responses_stream(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("error", {}).get("message") or body
        except json.JSONDecodeError:
            pass
        raise HTTPException(status_code=502, detail=f"OpenAI BYOK request failed: {detail}") from error
    except urllib.error.URLError as error:
        raise HTTPException(status_code=502, detail=f"Could not reach OpenAI BYOK endpoint: {error.reason}") from error


def invoke_openai_direct_response(
    *,
    prompt: str,
    model_name: str,
    timeout_seconds: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    effective_settings = get_effective_provider_settings()
    api_key = effective_settings.get("openAiApiKey")
    if not isinstance(api_key, str) or not api_key.strip():
        raise HTTPException(status_code=503, detail="Strategist BYOK is active, but no OpenAI API key is configured.")

    payload = call_openai_responses_api(
        api_key=api_key,
        request_body={
            "model": model_name,
            "instructions": (
                "You are a schema-bound Strategist direct-response step. "
                "Return JSON matching the requested schema exactly."
            ),
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "strategist_response",
                    "schema": STRATEGIST_DIRECT_RESPONSE_SCHEMA,
                    "strict": True,
                }
            },
        },
        timeout_seconds=timeout_seconds or 90,
    )
    output_text = extract_openai_output_text(payload)
    if not output_text:
        raise HTTPException(status_code=502, detail="OpenAI BYOK returned an empty structured response.")
    try:
        parsed_output = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=502, detail="OpenAI BYOK returned invalid structured JSON.") from error
    if not isinstance(parsed_output, dict):
        raise HTTPException(status_code=502, detail="OpenAI BYOK returned an unexpected payload.")
    return parsed_output, payload.get("id") if isinstance(payload.get("id"), str) else None


def extract_openai_function_calls(response_payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = response_payload.get("output")
    if not isinstance(output, list):
        return []
    return [
        item
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and isinstance(item.get("name"), str)
    ]


def invoke_openai_tool_loop(
    *,
    mode: str,
    prompt: str,
    runtime_context: dict[str, Any],
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]] | None,
    provider_session_id: str | None,
    model_name: str,
    conversation_history: list[dict[str, str]] | None = None,
    today_override: date | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
    initial_tool_history: list[dict[str, Any]] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    tool_executor: StrategistToolExecutor | None = None,
    model_step_trace: list[dict[str, Any]] | None = None,
    max_model_steps: int | None = None,
    max_tool_calls: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    effective_settings = get_effective_provider_settings()
    api_key = effective_settings.get("openAiApiKey")
    if not isinstance(api_key, str) or not api_key.strip():
        raise HTTPException(status_code=503, detail="Strategist BYOK is active, but no OpenAI API key is configured.")

    instructions = (
        "You are Strategist AI inside a planning application. Use provided functions for task/workload data. "
        "When enough information is available, return final JSON matching the schema."
    )
    available_tool_definitions = STRATEGIST_TOOL_DEFINITIONS if tool_definitions is None else tool_definitions
    execute_tool = tool_executor or _missing_authoritative_tool_executor
    configured_max_steps = max(1, max_model_steps or 7)
    configured_max_tool_calls = max(0, max_tool_calls if max_tool_calls is not None else 9)
    executed_tool_calls = 0
    tool_history: list[dict[str, Any]] = [dict(item) for item in (initial_tool_history or [])]
    request_body: dict[str, Any] = {
        "model": model_name,
        "instructions": instructions,
        "input": build_tool_loop_prompt(
            mode=mode,
            prompt=prompt,
            runtime_context=runtime_context,
            conversation_history=conversation_history,
            tool_result=tool_history[-1] if tool_history else None,
            tool_history=tool_history or None,
            tool_definitions=available_tool_definitions,
        ),
        "tools": available_tool_definitions,
        "tool_choice": "auto",
        "text": {"format": {"type": "json_schema", "name": "strategist_response", "schema": STRATEGIST_JSON_SCHEMA, "strict": True}},
    }
    if provider_session_id:
        request_body["previous_response_id"] = provider_session_id

    payload = call_openai_responses_api(api_key=api_key, request_body=request_body)
    if model_step_trace is not None:
        model_step_trace.append(
            {"phase": "tool_loop", "step_index": 1, "status": "ok", "provider": "openai-api"}
        )
    for step_index in range(2, configured_max_steps + 1):
        function_calls = extract_openai_function_calls(payload)
        if not function_calls:
            output_text = extract_openai_output_text(payload)
            if not output_text:
                raise HTTPException(status_code=502, detail="OpenAI BYOK returned an empty structured response.")
            try:
                parsed_output = json.loads(output_text)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=502, detail="OpenAI BYOK returned invalid structured JSON.") from error
            if not isinstance(parsed_output, dict):
                raise HTTPException(status_code=502, detail="OpenAI BYOK returned an unexpected payload.")
            return parsed_output, payload.get("id") if isinstance(payload.get("id"), str) else provider_session_id

        tool_outputs: list[dict[str, Any]] = []
        for function_call in function_calls:
            from app.services.agent_stream import emit_progress
            from app.services.strategist_runs import _public_tool_activity
            emit_progress("activity", _public_tool_activity(str(function_call.get("name", ""))))
            call_id = function_call.get("call_id")
            raw_arguments = function_call.get("arguments")
            tool_name = function_call.get("name")
            if not isinstance(call_id, str) or not isinstance(tool_name, str):
                raise HTTPException(status_code=502, detail="OpenAI BYOK returned an invalid tool call.")
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=502, detail="OpenAI BYOK returned invalid tool arguments.") from error
            if executed_tool_calls >= configured_max_tool_calls:
                result = {
                    "error": "agent_kernel_tool_budget_exhausted",
                    "tool_error": {
                        "code": "TOOL_RETRYABLE_SYSTEM_FAILURE",
                        "category": "retryable_system",
                        "retryable": True,
                        "messageForModel": "The bounded tool budget is exhausted; return a final typed system failure now.",
                    },
                }
                tool_duration_ms = 0.0
            else:
                tool_started = perf_counter()
                result = execute_tool(
                    tool_name,
                    arguments if isinstance(arguments, dict) else {},
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    staged_node_ids=staged_node_ids,
                    working_set=working_set,
                    today_override=today_override,
                )
                tool_duration_ms = _elapsed_ms(tool_started)
                executed_tool_calls += 1
            if tool_trace is not None:
                tool_trace.append({"name": tool_name, "arguments": arguments if isinstance(arguments, dict) else {}, "result": result, "duration_ms": tool_duration_ms})
            tool_history.append({"tool_name": tool_name, "arguments": arguments if isinstance(arguments, dict) else {}, "result": result})
            tool_outputs.append({"type": "function_call_output", "call_id": call_id, "output": json.dumps(result, default=str)})

        payload = call_openai_responses_api(
            api_key=api_key,
            request_body={
                "model": model_name,
                "previous_response_id": payload.get("id"),
                "input": tool_outputs,
                "tools": available_tool_definitions,
                "text": {"format": {"type": "json_schema", "name": "strategist_response", "schema": STRATEGIST_JSON_SCHEMA, "strict": True}},
            },
        )
        if model_step_trace is not None:
            model_step_trace.append(
                {
                    "phase": "tool_loop",
                    "step_index": step_index,
                    "status": "ok",
                    "provider": "openai-api",
                }
            )

    if tool_history:
        return build_tool_history_fallback_payload(prompt=prompt, tool_history=tool_history), payload.get("id") if isinstance(payload.get("id"), str) else provider_session_id
    raise HTTPException(status_code=502, detail="OpenAI BYOK exceeded the tool-call limit before reaching a final answer.")


def provider_transport_readiness(runtime_settings: dict[str, Any]) -> dict[str, Any]:
    """Cheap provider preflight for model-backed kernel turns."""
    provider = str(runtime_settings.get("provider") or "codex-cli").strip()
    matrix_supported_value = runtime_settings.get("matrix_supported")
    matrix_error_code = runtime_settings.get("matrix_error_code")
    if isinstance(matrix_supported_value, bool):
        matrix_supported = matrix_supported_value
    else:
        # Older/test callers may supply a hand-built runtime-settings dictionary.
        # Production settings always carry the explicit typed matrix decision.
        matrix_supported = True
    if provider == "codex-cli":
        from app.services.codex_sandbox import enabled, get_status
        if enabled():
            auth_ready = bool(get_status().get("isAuthenticated"))
            return {
                "provider": provider, "checked": True, "passed": auth_ready,
                "codex_cli_found": True, "auth_file_ready": auth_ready,
                "model_matrix_supported": matrix_supported,
                "model_matrix_error_code": matrix_error_code,
                "source": "sandbox_subscription_preflight",
            }
        codex_path = find_codex_cli_path()
        auth_ready = has_usable_codex_auth_file()
        return {
            "provider": provider,
            "checked": True,
            "passed": codex_path is not None and auth_ready,
            "codex_cli_found": codex_path is not None,
            "auth_file_ready": auth_ready,
            "model_matrix_supported": matrix_supported,
            "model_matrix_error_code": matrix_error_code,
            "source": "cheap_codex_transport_preflight",
        }
    if provider == "openai-api":
        settings = get_effective_provider_settings()
        return {
            "provider": provider,
            "checked": True,
            "passed": bool(settings.get("isOpenAiKeyConfigured")),
            "model_matrix_supported": matrix_supported,
            "model_matrix_error_code": matrix_error_code,
            "source": "provider_settings_preflight",
        }
    return {"provider": provider, "checked": True, "passed": False, "source": "unknown_provider"}


__all__ = [
    "STRATEGIST_JSON_SCHEMA",
    "STRATEGIST_DIRECT_RESPONSE_SCHEMA",
    "build_direct_response_prompt",
    "build_native_tool_loop_prompt",
    "call_openai_responses_api",
    "codex_native_tool_loop_enabled",
    "codex_structured_step_timeout_seconds",
    "codex_tool_loop_max_steps",
    "codex_tool_loop_timeout_seconds",
    "ensure_codex_login",
    "extract_codex_cli_error",
    "extract_openai_output_text",
    "get_provider_runtime_settings",
    "invoke_codex_direct_response",
    "invoke_codex_json_schema",
    "invoke_codex_json_schema_via_exec",
    "invoke_codex_tool_loop",
    "invoke_openai_direct_response",
    "invoke_openai_tool_loop",
    "parse_json_object_text",
    "provider_transport_readiness",
]
from app.services.account_auth import account_process_environment
