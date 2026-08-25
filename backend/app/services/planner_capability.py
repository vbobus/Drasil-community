"""Planner capability harness for SchedulePatch proposals."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, time, timedelta
from statistics import median
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.agent_profiles import get_agent_profile
from app.services.agent_kernel.capabilities import (
    compact_capability_catalog,
    load_skill,
    search_capabilities,
)
from app.services.agent_kernel.task_patch import simulate_task_patch, validate_task_patch
from app.services.agent_kernel.codex_app_server import (
    invoke_codex_app_server_native_tool_loop,
)
from app.services.agent_kernel.capability_observability import emit_capability_observations
from app.services.agent_kernel.provider_transport import (
    get_provider_runtime_settings,
    provider_transport_readiness,
)
from app.services.planner_tools import (
    apply_default_scheduling_heuristics,
    apply_planner_task_directives,
    filter_working_set,
    PlannerTaskRef,
    PlannerWorkingSet,
    local_wall_time,
    read_calendar_blocks_for_tasks,
    read_calendar_state,
    read_scheduled_task_ids,
    resolve_selected_working_set,
    run_deterministic_schedule_tool,
    working_set_context_payload,
)
from app.services.scheduling_solver import OccupiedInterval
from app.services.preference_memory import (
    build_model_preference_context,
    build_planner_preference_context,
    constraint_intervals_from_context,
    inter_task_gap_minutes_for_duration,
    inter_task_gap_policy_from_context,
)
from app.services.schedule_patches import (
    build_schedule_patch,
    simulate_schedule_patch,
    validate_schedule_patch,
)
from app.services.recurring_schedules import (
    build_recurring_schedule_plan,
    preference_override_authorized_from_prompt,
    schedule_patch_from_recurring_plan,
)
from app.services.handoff_outcomes import (
    clarification_for_handoff_outcome,
    classify_handoff_failures,
)
from app.services.request_scheduling_constraints import (
    apply_request_constraints,
    constraint_occupied_intervals,
    normalize_request_constraints,
    validate_operations_against_request_constraints,
)
from app.services.calendar_conflict_policy import (
    blocker_payload,
    decide_calendar_collision,
    overlapping_blockers,
)
from app.services.scheduling_solver import (
    SolverPlacement,
    SolverResult,
    SolverTask,
    solve_schedule_draft,
)
from app.services.agent_kernel.planner_tool_contracts import (
    PLANNER_FRAME_SCHEMA,
    PlannerFrame,
    planner_model_tool_definitions,
    validate_planner_tool_input,
    validate_planner_tool_output,
)
from app.services.agent_kernel.mcp_client import invoke_drasil_mcp_tool
from app.services.agent_kernel.mcp_invocation import (
    McpExecutionDependencies,
    McpInvocationContext,
)
from app.services.agent_kernel.mcp_policy import filter_mcp_specs_for_invocation
from app.services.agent_kernel.subagent_delegation import PlannerSubagentTask


_PLANNER_FRAME_SCHEMA: dict[str, Any] = PLANNER_FRAME_SCHEMA


def _planner_frame_contract_errors(frame: Any) -> list[str]:
    try:
        parsed = PlannerFrame.model_validate(frame)
    except Exception as error:
        return [f"Planner frame failed canonical validation: {error}"]
    if parsed.decision == "plan" and not parsed.task_directives:
        return ["Planner frame plan decision must include task directives."]
    if (
        parsed.decision == "needs_strategic_decision"
        and parsed.strategic_decision is None
    ):
        return [
            "Planner frame strategic-decision status requires a typed strategic decision."
        ]
    if (
        parsed.decision != "needs_strategic_decision"
        and parsed.strategic_decision is not None
    ):
        return [
            "Planner frame strategic decision is only valid for its matching status."
        ]
    return []


def _planner_model_frame(
    *,
    session: Session,
    prompt: str,
    runtime_settings: dict[str, Any],
    working_set: PlannerWorkingSet,
    accepted_blocks: list[CalendarBlock],
    calendar_task_context: dict[int, dict[str, Any]],
    preference_context: dict[str, Any],
    default_scheduling_policy: dict[str, Any],
    solver_accepted_blocks: list[CalendarBlock],
    preference_occupied: list[Any],
    gap_policy: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    alternative_window_end: datetime,
    slot_minutes: int,
    operation_mode: str,
    timeout_seconds: int = 90,
    subagent_task: PlannerSubagentTask | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Let the Planner model inspect evidence and skills before the solver runs.

    The model owns semantic interpretation, capability selection, task ordering,
    and bounded timebox choices. Exact placements remain the deterministic
    solver's job.
    """

    profile = get_agent_profile("planner", include_instructions=True)
    catalog = compact_capability_catalog(agent="planner")
    context_payload = {
        "working_set": working_set_context_payload(working_set),
        "calendar": {
            "window_start": _iso(window_start),
            "window_end": _iso(window_end),
            "accepted_blocks": [
                {
                    "id": block.id,
                    "task_id": block.task_id,
                    "start_at": _iso(block.start_at),
                    "end_at": _iso(block.end_at),
                    "task_context": calendar_task_context.get(int(block.task_id), {}),
                }
                for block in accepted_blocks
            ],
        },
        "preferences": build_model_preference_context(preference_context),
        "default_scheduling_policy": default_scheduling_policy,
        "operation_mode": operation_mode,
    }
    loaded_capabilities: list[dict[str, Any]] = []
    if subagent_task is None:
        raise ValueError(
            "Model-backed scheduling requires an owned Planner subagent task."
        )
    tool_definitions = [
        spec.v2_tool_schema()
        for spec in filter_mcp_specs_for_invocation(
            agent_id="planner",
            invocation_allowed_tool_names=subagent_task.allowed_tool_names,
        )
    ]

    def execute_local(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = validate_planner_tool_input(tool_name, arguments)
        if tool_name == "read_planner_context":
            result = {"read_only": True, **context_payload}
            validate_planner_tool_output(tool_name, result)
            return result
        if tool_name == "search_capability_catalog":
            result = {"read_only": True, **search_capabilities({**arguments, "agent": "planner"})}
            validate_planner_tool_output(tool_name, result)
            return result
        if tool_name == "load_skill":
            load_started_at = perf_counter()
            result = load_skill(arguments)
            capability = result.get("capability") if isinstance(result.get("capability"), dict) else {}
            loaded_capabilities.append(
                {
                    "id": capability.get("id") or arguments.get("capability_id"),
                    "version": capability.get("version"),
                    "load_duration_ms": round((perf_counter() - load_started_at) * 1000, 1),
                    "failed": bool(result.get("error")),
                    "error": result.get("error"),
                }
            )
            output = {"read_only": True, **result}
            validate_planner_tool_output(tool_name, output)
            return output
        if tool_name == "evaluate_schedule_plan":
            candidate_working_set, normalization = apply_planner_task_directives(
                working_set,
                arguments.get("task_directives"),
                gap_policy=gap_policy,
            )
            result = run_deterministic_schedule_tool(
                working_set=candidate_working_set,
                accepted_blocks=solver_accepted_blocks,
                extra_occupied=preference_occupied,
                window_start=window_start,
                window_end=window_end,
                slot_minutes=slot_minutes,
                alternative_window_end=alternative_window_end,
            )
            output = {
                "read_only": True,
                "normalization": normalization,
                "solver_status": result.solver_status,
                "placements": [
                    {
                        "task_id": placement.task_id,
                        "start_at": _iso(placement.start_at),
                        "end_at": _iso(placement.end_at),
                    }
                    for placement in result.placements
                ],
                "conflicts": _conflict_payloads(
                    result,
                    working_set=candidate_working_set,
                    accepted_blocks=solver_accepted_blocks,
                    extra_occupied=preference_occupied,
                    window_start=window_start,
                    window_end=window_end,
                ),
                "model_instruction": (
                    "Use this feasibility evidence. If the plan is partial or conflicting, revise only when "
                    "the user's goal and task evidence justify a better ordering or timebox; then return the final frame."
                ),
            }
            validate_planner_tool_output(tool_name, output)
            return output
        raise ValueError(f"Unknown Planner tool: {tool_name}")

    def execute(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invocation = McpInvocationContext(
            principal_id="local-runtime-user",
            agent_id="planner",
            application_user_id="local",
            local_date=window_start.date(),
            timezone="local",
            allowed_tool_names=frozenset(subagent_task.allowed_tool_names),
            chat_session_id=subagent_task.parent.session_id,
            run_id=subagent_task.task_id,
            active_goal_id=subagent_task.parent.goal_id,
            active_goal_revision=subagent_task.parent.goal_revision,
            correlation_id=subagent_task.parent.correlation_id,
            causation_id=subagent_task.parent.parent_run_id,
            selected_task_ids=tuple(subagent_task.task_ids),
            scope_window_start=window_start,
            scope_window_end=alternative_window_end,
        )
        return invoke_drasil_mcp_tool(
            session,
            invocation=invocation,
            name=tool_name,
            arguments=arguments,
            execution=McpExecutionDependencies(
                planner_context_reader=lambda: execute_local(
                    "read_planner_context", {}
                ),
                planner_plan_evaluator=lambda planner_arguments: execute_local(
                    "evaluate_schedule_plan", planner_arguments
                ),
            ),
        )

    model_prompt = (
        f"User planning request: {prompt.strip() or 'Schedule the selected tasks.'}\n\n"
        "Authoritative submitted Planner context:\n"
        f"{json.dumps(context_payload, ensure_ascii=True, default=str, separators=(',', ':'))}\n\n"
        f"Available capability catalog: {json.dumps(catalog, ensure_ascii=True, default=str)}\n\n"
        "The submitted context above is available now; do not claim task or calendar evidence is unavailable. "
        "Semantically load any capability that materially helps, then choose an ordered directive for every task. "
        "For create or reschedule requests, call evaluate_schedule_plan before returning the final frame. "
        "Do not invent exact placements or calendar operations. Your final JSON must satisfy this exact schema:\n"
        f"{json.dumps(_PLANNER_FRAME_SCHEMA, ensure_ascii=True, separators=(',', ':'))}"
    )
    estimated_input_tokens = max(
        1,
        (
            len(model_prompt)
            + len(str(profile["operating_instructions"]))
        )
        // 4,
    )
    if estimated_input_tokens >= subagent_task.budget.max_tokens:
        raise ValueError(
            "Owned Planner token budget is insufficient for the submitted context."
        )
    try:
        frame, trace = invoke_codex_app_server_native_tool_loop(
            prompt=model_prompt,
            schema=_PLANNER_FRAME_SCHEMA,
            configured_model=runtime_settings.get("model_name"),
            timeout_seconds=timeout_seconds,
            tool_definitions=tool_definitions,
            tool_executor=execute,
            reasoning_effort=runtime_settings.get("reasoning_effort"),
            max_tool_calls=subagent_task.budget.max_tool_calls,
            cancellation_check=cancellation_check,
            agent_label="Planner",
            client_name="drasil-planner",
            base_instructions=str(profile["operating_instructions"]),
            developer_instructions=(
                "You control semantic intent interpretation, evidence gathering, and skill selection. "
                "The authoritative submitted context is already in the user turn; read_planner_context is available "
                "only if you need to inspect it again. Do not claim the submitted task set is unavailable. "
                "Skills are guidance, not authority. "
                "Preserve evidence-backed task durations by default; change an allocated timebox only when the "
                "request or evidence justifies it, and explain why. Include every submitted task exactly once. "
                "Treat the submitted default_scheduling_policy as the baseline when explicit user instructions, "
                "hard constraints, or saved preferences do not decide differently. Prefer earlier deadlines, "
                "put longer/deeper work in the supplied high-energy windows, keep small related tasks together, "
                "use surrounding calendar task context to reduce context switching, and avoid consecutive heavy "
                "blocks without recovery. Keep tasks with the same parent or closely related context adjacent. "
                "Represent a current-request transition or break with gap_after_minutes on the task immediately "
                "before the gap. Use 0 when the current request explicitly asks for back-to-back tasks. Use null "
                "when the current request says nothing about gaps; the deterministic scheduler will then apply any "
                "remembered soft default and hard minimum. A break tied to an explicit clock time, such as lunch "
                "at 3:00 PM, is already represented as a protected interval; do not attach its duration as a gap "
                "to an unrelated earlier task. Long work may be split around that protected interval. "
                "A placement conflict is still a valid plan: return decision=plan with the fully captured task "
                "directives so the deterministic solver can report blockers and alternatives. Use cannot_plan only "
                "when the user's semantic intent itself cannot be represented safely, not when the requested time "
                "is occupied. Treat an exact requested start/end interval as the intended allocation duration when "
                "the request asks to move the same work to an alternative. "
                "When accepted calendar work blocks a requested time, use typed task priority and current-turn "
                "instructions as evidence. Protected, pinned, or likely fixed commitments stay put. A clearly "
                "higher-priority incoming task may move a lower-priority flexible block to a nearby validated "
                "opening; a lower-priority incoming task should move instead. Equal or unclear priority needs a "
                "specific clarification naming the blocker. Recurring conflicts affect only the colliding "
                "occurrence unless the user asks to change the entire series. "
                "Skip a task only when the user's request or a material constraint justifies exclusion. "
                "The deterministic scheduler owns exact placement and validation. Return only the requested JSON."
            ),
            allow_terminal_proposal_tools=False,
        )
        contract_errors = _planner_frame_contract_errors(frame)
        if contract_errors:
            raise ValueError("Invalid Planner frame: " + " ".join(contract_errors))
        # Validation accepts both provider camelCase aliases and Python field
        # names. Canonicalize once here so every downstream branch reads the
        # same representation; otherwise a valid ``strategicDecision`` was
        # accepted and then lost when code looked for ``strategic_decision``.
        frame = PlannerFrame.model_validate(frame).model_dump(
            mode="json",
            by_alias=False,
        )
        evidence = {
            "loaded_capabilities": loaded_capabilities,
            "skill_evidence_checks": [
                {
                    "capability_id": item.get("id"),
                    "requirements_satisfied": True,
                    "unmet_requirements": [],
                }
                for item in loaded_capabilities
            ],
        }
        observations = emit_capability_observations(evidence, agent_id="planner")
        estimated_output_tokens = max(
            1,
            len(json.dumps(frame, ensure_ascii=True, default=str)) // 4,
        )
        authoritative_input_tokens = trace.get("input_tokens")
        authoritative_output_tokens = trace.get("output_tokens")
        has_authoritative_usage = (
            isinstance(authoritative_input_tokens, int)
            and isinstance(authoritative_output_tokens, int)
        )
        return frame, {
            "status": "completed",
            "trace": trace,
            "usage": {
                "input_tokens": (
                    authoritative_input_tokens
                    if has_authoritative_usage
                    else estimated_input_tokens
                ),
                "output_tokens": (
                    authoritative_output_tokens
                    if has_authoritative_usage
                    else estimated_output_tokens
                ),
                "measurement": (
                    "provider_reported"
                    if has_authoritative_usage
                    else "bounded_character_estimate"
                ),
            },
            "loadedCapabilities": loaded_capabilities,
            "capabilityObservations": observations,
        }
    except Exception as error:
        evidence = {
            "loaded_capabilities": loaded_capabilities,
            "skill_evidence_checks": [],
        }
        return None, {
            "status": "failed",
            "error": str(error),
            "usage": {
                "input_tokens": estimated_input_tokens,
                "output_tokens": 0,
                "measurement": "bounded_character_estimate",
            },
            "loadedCapabilities": loaded_capabilities,
            "capabilityObservations": emit_capability_observations(
                evidence,
                agent_id="planner",
            ),
        }


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=None).isoformat()


def _proposal_block_id(task_id: int, start_at: datetime) -> str:
    return f"proposal-{task_id}-{start_at.strftime('%Y%m%dT%H%M%S')}"


SPLIT_SESSION_RE = re.compile(
    r"\b(?:split|chunk|divide)\b.*\b(?:sessions?|blocks?|across)\b",
    re.IGNORECASE,
)
ONE_OFF_BREAK_DURATION_RE = re.compile(
    r"\b(?P<minutes>\d{1,3})\s*(?:minutes?|mins?)\s+(?:gap|break)\b",
    re.IGNORECASE,
)
ONE_OFF_BREAK_TIME_RE = re.compile(
    r"\b(?:around|at)\s+(?:(?:exactly|sharp|sharply|about|roughly)\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)
INTER_TASK_GAP_RE = re.compile(
    r"\b(?:between\s+(?:the\s+)?tasks?|between\s+them|after\s+each\s+task|"
    r"between\s+each|transition\s+time)\b",
    re.IGNORECASE,
)
GENERIC_REQUESTED_GAP_RE = re.compile(
    r"\b(?:leave|take|add|include|give)\b.{0,24}\b\d+[-\s]*(?:minutes?|mins?)\b.{0,16}"
    r"\b(?:gap|break|buffer)\b",
    re.IGNORECASE,
)
CONTIGUOUS_REQUIRED_RE = re.compile(
    r"\b(?:uninterrupted|continuous|contiguous|one\s+(?:single\s+)?block|single\s+stretch)\b",
    re.IGNORECASE,
)


def _ceil_to_slot_boundary(value: datetime, slot_minutes: int) -> datetime:
    if slot_minutes <= 1:
        return value.replace(second=0, microsecond=0) + (
            timedelta(minutes=1) if value.second or value.microsecond else timedelta()
        )
    clean = value.replace(second=0, microsecond=0)
    remainder = clean.minute % slot_minutes
    extra = (slot_minutes - remainder) % slot_minutes
    if value.second or value.microsecond:
        extra = extra or slot_minutes
    return clean + timedelta(minutes=extra)


def _one_off_break_intervals_from_prompt(
    prompt: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[OccupiedInterval]:
    if not re.search(r"\b(?:gap|break|lunch|meal)\b", prompt, re.IGNORECASE):
        return []
    duration_match = ONE_OFF_BREAK_DURATION_RE.search(prompt)
    time_match = ONE_OFF_BREAK_TIME_RE.search(prompt)
    if duration_match is None or time_match is None:
        return []
    duration_minutes = int(duration_match.group("minutes"))
    hour = int(time_match.group("hour"))
    minute = int(time_match.group("minute") or 0)
    if not 0 <= minute <= 59 or not 1 <= hour <= 24 or not 1 <= duration_minutes <= 240:
        return []
    ampm = str(time_match.group("ampm") or "").lower().replace(".", "")
    if ampm:
        hour %= 12
        if ampm == "pm":
            hour += 12
        candidate_hours = [hour]
    elif hour <= 12:
        candidate_hours = sorted({hour % 12, (hour % 12) + 12})
    else:
        candidate_hours = [hour]

    label = "Lunch break" if re.search(r"\blunch\b", prompt, re.IGNORECASE) else "Requested break"
    intervals: list[OccupiedInterval] = []
    current_date = window_start.date()
    final_date = (window_end - timedelta(microseconds=1)).date()
    while current_date <= final_date:
        day_candidates = [
            datetime.combine(current_date, time(candidate_hour, minute))
            for candidate_hour in candidate_hours
            if 0 <= candidate_hour <= 23
        ]
        in_window = [
            candidate
            for candidate in day_candidates
            if window_start <= candidate < window_end
        ]
        if in_window:
            start_at = min(in_window)
            end_at = min(start_at + timedelta(minutes=duration_minutes), window_end)
            if end_at > start_at:
                intervals.append(
                    OccupiedInterval(
                        block_id=-(900_000 + len(intervals)),
                        start_at=start_at,
                        end_at=end_at,
                        source_type="user_break",
                        source_label=label,
                        report_conflict=False,
                    )
                )
        current_date += timedelta(days=1)
    return intervals


def _task_directives_without_misclassified_clock_break(
    directives: Any,
    *,
    prompt: str,
    temporary_breaks: list[OccupiedInterval],
) -> Any:
    # The deterministic policy compiler owns saved/default gaps. A model gap is
    # authoritative only when the current request explicitly asks for one;
    # otherwise it can accidentally reintroduce a superseded memory rule.
    if INTER_TASK_GAP_RE.search(prompt) or (
        not temporary_breaks and GENERIC_REQUESTED_GAP_RE.search(prompt)
    ):
        return directives
    if not isinstance(directives, list):
        return directives
    return [
        {**directive, "gap_after_minutes": None}
        if isinstance(directive, dict)
        else directive
        for directive in directives
    ]


def _requested_session_minutes(prompt: str, total_minutes: int) -> int | None:
    if total_minutes <= 0 or not SPLIT_SESSION_RE.search(prompt):
        return None
    hours = re.search(
        r"\b(\d+(?:\.5)?)\s*(?:hours?|hrs?)\s+(?:sessions?|blocks?)\b",
        prompt,
        re.IGNORECASE,
    )
    if hours is not None:
        minutes = int(float(hours.group(1)) * 60)
        return min(total_minutes, max(15, minutes))
    minutes_match = re.search(
        r"\b(\d+)\s*(?:minutes?|mins?)\s+(?:sessions?|blocks?)\b",
        prompt,
        re.IGNORECASE,
    )
    if minutes_match is not None:
        return min(total_minutes, max(15, int(minutes_match.group(1))))
    return min(total_minutes, 120)


LIKELY_ANCHORED_TITLE_RE = re.compile(
    r"\b(?:appointment|class|course|exam|interview|meeting|stand[ -]?up|"
    r"doctor|dentist|therapy|flight|train|lunch|dinner|break|pickup|drop[ -]?off)\b",
    re.IGNORECASE,
)


def _anchored_calendar_intervals(
    session: Session,
    accepted_blocks: list[CalendarBlock],
) -> list[OccupiedInterval]:
    """Identify accepted items the scheduler should plan around, not silently move."""

    intervals: list[OccupiedInterval] = []
    node_cache: dict[int, StrategyNode | None] = {}
    for block in accepted_blocks:
        node = node_cache.setdefault(block.task_id, session.get(StrategyNode, block.task_id))
        title = str(node.title if node is not None else f"Task {block.task_id}").strip()
        node_pinned = bool(node.is_pinned) if node is not None else False
        anchored = bool(
            block.is_protected
            or block.is_pinned
            or block.google_event_id
            or node_pinned
            or LIKELY_ANCHORED_TITLE_RE.search(title)
        )
        if not anchored:
            continue
        intervals.append(
            OccupiedInterval(
                block_id=int(block.id or 0),
                start_at=local_wall_time(block.start_at),
                end_at=local_wall_time(block.end_at),
                source_type="anchored_calendar",
                source_label=title,
            )
        )
    return intervals


def _calendar_occupancy_intervals(
    accepted_blocks: list[CalendarBlock],
) -> list[OccupiedInterval]:
    return [
        OccupiedInterval(
            block_id=int(block.id or 0),
            start_at=local_wall_time(block.start_at),
            end_at=local_wall_time(block.end_at),
            source_type="calendar",
        )
        for block in accepted_blocks
    ]


def _human_clock(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _exact_task_range(
    task: PlannerTaskRef,
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    """Return the concrete interval when this request has one exact placement."""

    if len(task.allowed_start_windows) == 1:
        allowed_start, allowed_end = task.allowed_start_windows[0]
        if allowed_end - allowed_start == timedelta(minutes=task.duration_minutes):
            return allowed_start, allowed_end
    if window_end - window_start == timedelta(minutes=task.duration_minutes):
        return window_start, window_end
    return None


def _relocation_operation(
    *,
    blocker: Any,
    displaced_block_ids: set[int],
    accepted_blocks: list[CalendarBlock],
    extra_occupied: list[OccupiedInterval],
    proposed_occupied: list[OccupiedInterval],
    search_start: datetime,
    search_end: datetime,
    slot_minutes: int,
    gap_policy: dict[str, Any],
    prefer_minimal_pushback: bool = False,
) -> tuple[dict[str, Any] | None, OccupiedInterval | None]:
    block = blocker.block
    duration_minutes = int((block.end_at - block.start_at).total_seconds() // 60)
    if duration_minutes <= 0 or search_end <= search_start:
        return None, None
    soft_gap, hard_gap = inter_task_gap_minutes_for_duration(
        gap_policy,
        duration_minutes=duration_minutes,
    )
    # A short exact-time insertion should move a flexible blocker by the
    # smallest safe amount. Soft transition preferences must not inflate a
    # five- or ten-minute action into a much larger displacement; hard gaps
    # remain authoritative.
    gap_minutes = hard_gap if prefer_minimal_pushback else max(soft_gap, hard_gap)
    occupied = [
        OccupiedInterval(
            block_id=int(item.id or 0),
            start_at=local_wall_time(item.start_at),
            end_at=local_wall_time(item.end_at),
            source_type="calendar",
        )
        for item in accepted_blocks
        if int(item.id or 0) not in displaced_block_ids
    ]
    result = solve_schedule_draft(
        tasks=[
            SolverTask(
                task_id=int(blocker.task.id),
                duration_minutes=duration_minutes,
                gap_before_minutes=gap_minutes,
                gap_after_minutes=gap_minutes,
            )
        ],
        window_start=search_start,
        window_end=search_end,
        occupied=[*occupied, *extra_occupied, *proposed_occupied],
        slot_minutes=slot_minutes,
    )
    if not result.placements:
        return None, None
    placement = result.placements[0]
    operation = {
        "op": "update_block_time",
        "acceptedBlockId": int(block.id),
        "taskId": int(blocker.task.id),
        "categoryId": int(blocker.task.category_id),
        "title": blocker.task.title,
        "emoji": blocker.task.emoji,
        "startAt": _iso(placement.start_at),
        "endAt": _iso(placement.end_at),
        "expectedOriginalStartAt": _iso(block.start_at),
        "expectedOriginalEndAt": _iso(block.end_at),
        "variant": "proposed",
        "completionStatus": block.completion_status,
        "taskPriority": blocker.task.priority,
        "timeEstimateMinutes": blocker.task.time_estimate_minutes,
        "isPinned": False,
        "isProtected": False,
        "isRecurring": bool(block.is_recurring),
        "recurrenceSeriesId": block.recurrence_series_id,
        "recurrenceKey": block.recurrence_key,
        "source": "priority_conflict_resolution",
    }
    interval = OccupiedInterval(
        block_id=-int(block.id),
        start_at=placement.start_at,
        end_at=placement.end_at,
        source_type="draft_relocation",
        source_label=blocker.task.title,
        report_conflict=False,
    )
    return operation, interval


def _priority_conflict_clarification(
    *,
    task: PlannerTaskRef,
    task_node: StrategyNode,
    decision: Any,
    requested_start: datetime,
    requested_end: datetime,
) -> dict[str, Any]:
    blockers = [blocker_payload(item) for item in decision.blockers]
    blocker_labels = ", ".join(str(item["taskTitle"]) for item in blockers)
    blocker_details = "; ".join(
        f"{item['taskTitle']} ({item['priority']} priority), "
        f"{_human_clock(datetime.fromisoformat(str(item['startAt'])))} to "
        f"{_human_clock(datetime.fromisoformat(str(item['endAt'])))}"
        + (f", {item['fixedReason']}" if item.get("fixedReason") else "")
        for item in blockers
    )
    can_move_existing = not any(
        item.get("fixedReason") in {"protected", "pinned"}
        for item in blockers
    )
    field_choices = [
        {
            "kind": "field",
            "label": f"Keep {blocker_labels} here",
            "value": "preserve_existing",
            "follow_up_prompt": (
                f"Do not move the existing task. Find another time for {task.title}."
            ),
        }
    ]
    if can_move_existing:
        field_choices.append(
            {
                "kind": "field",
                "label": f"Use this time for {task.title}",
                "value": "move_existing",
                "follow_up_prompt": (
                    f"Move the existing task and schedule {task.title} from "
                    f"{_human_clock(requested_start)} to {_human_clock(requested_end)}."
                ),
            }
        )
    return {
        "mode": "clarification",
        "summary": (
            f"{task.title} conflicts with {blocker_labels} at "
            f"{_human_clock(requested_start)}."
        ),
        "clarification": {
            "message": (
                f"{blocker_details}. {task.title} is {decision.incoming_priority} priority. "
                "Which commitment should keep this time?"
            ),
            "entity_choices": [],
            "field_choices": field_choices,
            "scoped_follow_up_prompts": [],
        },
        "conflictResolution": {
            "action": "clarify",
            "incomingTaskId": int(task_node.id),
            "incomingTaskTitle": task.title,
            "incomingPriority": decision.incoming_priority,
            "requestedStartAt": _iso(requested_start),
            "requestedEndAt": _iso(requested_end),
            "blockers": blockers,
        },
    }


def _resolve_single_task_priority_conflict(
    session: Session,
    *,
    prompt: str,
    working_set: PlannerWorkingSet,
    solver_result: SolverResult,
    accepted_blocks: list[CalendarBlock],
    extra_occupied: list[OccupiedInterval],
    window_start: datetime,
    window_end: datetime,
    alternative_window_end: datetime,
    slot_minutes: int,
    gap_policy: dict[str, Any],
    hard_exact_constraint: bool,
) -> dict[str, Any]:
    """Resolve a concrete one-task collision without silently breaking commitments."""

    if len(working_set.tasks) != 1 or not solver_result.conflicts:
        return {"solver_result": solver_result, "operations": [], "resolution": None}
    task = working_set.tasks[0]
    task_node = next(
        (
            node
            for node in working_set.task_nodes
            if node.id is not None and int(node.id) == task.task_id
        ),
        None,
    )
    exact_range = _exact_task_range(
        task,
        window_start=window_start,
        window_end=window_end,
    )
    if task_node is None or exact_range is None:
        return {"solver_result": solver_result, "operations": [], "resolution": None}
    requested_start, requested_end = exact_range
    task_ids = {int(block.task_id) for block in accepted_blocks}
    calendar_tasks = {
        int(node.id): node
        for node in session.exec(select(StrategyNode).where(StrategyNode.id.in_(task_ids))).all()
        if node.id is not None
    }
    blockers = overlapping_blockers(
        accepted_blocks,
        tasks_by_id=calendar_tasks,
        start_at=requested_start,
        end_at=requested_end,
    )
    decision = decide_calendar_collision(
        incoming_task=task_node,
        blockers=blockers,
        prompt=prompt,
        exact_time=True,
    )
    if decision.action in {"none"}:
        return {"solver_result": solver_result, "operations": [], "resolution": None}
    if decision.action == "clarify" or (
        decision.action == "preserve_existing" and hard_exact_constraint
    ):
        return {
            "solver_result": solver_result,
            "operations": [],
            "resolution": None,
            "clarification": _priority_conflict_clarification(
                task=task,
                task_node=task_node,
                decision=decision,
                requested_start=requested_start,
                requested_end=requested_end,
            ),
        }
    if decision.action == "preserve_existing":
        alternative = next(
            (
                item
                for conflict in solver_result.conflicts
                if conflict.task_id == task.task_id
                for item in conflict.alternatives
            ),
            None,
        )
        if alternative is None:
            return {
                "solver_result": solver_result,
                "operations": [],
                "resolution": None,
                "clarification": _priority_conflict_clarification(
                    task=task,
                    task_node=task_node,
                    decision=decision,
                    requested_start=requested_start,
                    requested_end=requested_end,
                ),
            }
        resolved_result = SolverResult(
            placements=[
                SolverPlacement(
                    task_id=task.task_id,
                    start_at=alternative.start_at,
                    end_at=alternative.end_at,
                )
            ],
            conflicts=[],
            solver_status="feasible",
        )
        return {
            "solver_result": resolved_result,
            "operations": [],
            "resolution": {
                "action": "moved_incoming",
                "incomingTaskId": task.task_id,
                "incomingTaskTitle": task.title,
                "incomingPriority": decision.incoming_priority,
                "requestedStartAt": _iso(requested_start),
                "requestedEndAt": _iso(requested_end),
                "scheduledStartAt": _iso(alternative.start_at),
                "scheduledEndAt": _iso(alternative.end_at),
                "blockers": [blocker_payload(item) for item in decision.blockers],
                "userMessage": (
                    f"{decision.blockers[0].task.title} has higher priority or is fixed at "
                    f"{_human_clock(requested_start)}, so I kept it there and moved {task.title} "
                    f"to {_human_clock(alternative.start_at)}."
                ),
            },
            "window_end": max(window_end, alternative.end_at),
        }

    displaced_ids = {
        int(item.block.id)
        for item in decision.blockers
        if item.block.id is not None
    }
    remaining_blocks = [
        block for block in accepted_blocks if int(block.id or 0) not in displaced_ids
    ]
    incoming_result = run_deterministic_schedule_tool(
        working_set=working_set,
        accepted_blocks=remaining_blocks,
        extra_occupied=extra_occupied,
        window_start=window_start,
        window_end=window_end,
        slot_minutes=slot_minutes,
        alternative_window_end=alternative_window_end,
    )
    if incoming_result.conflicts or not incoming_result.placements:
        return {"solver_result": solver_result, "operations": [], "resolution": None}
    proposed_occupied = [
        OccupiedInterval(
            block_id=-placement.task_id,
            start_at=placement.start_at,
            end_at=placement.end_at,
            source_type="draft",
            source_label=task.title,
            report_conflict=False,
        )
        for placement in incoming_result.placements
    ]
    relocation_operations: list[dict[str, Any]] = []
    relocated_payloads: list[dict[str, Any]] = []
    prefer_minimal_pushback = task.duration_minutes <= 15
    search_cursor = (
        requested_end
        if prefer_minimal_pushback
        else max(requested_end, max(item.block.end_at for item in decision.blockers))
    )
    for blocker in decision.blockers:
        operation, interval = _relocation_operation(
            blocker=blocker,
            displaced_block_ids=displaced_ids,
            accepted_blocks=accepted_blocks,
            extra_occupied=extra_occupied,
            proposed_occupied=proposed_occupied,
            search_start=search_cursor,
            search_end=alternative_window_end,
            slot_minutes=slot_minutes,
            gap_policy=gap_policy,
            prefer_minimal_pushback=prefer_minimal_pushback,
        )
        if operation is None or interval is None:
            return {
                "solver_result": solver_result,
                "operations": [],
                "resolution": None,
                "clarification": _priority_conflict_clarification(
                    task=task,
                    task_node=task_node,
                    decision=decision,
                    requested_start=requested_start,
                    requested_end=requested_end,
                ),
            }
        relocation_operations.append(operation)
        proposed_occupied.append(interval)
        search_cursor = interval.end_at
        payload = blocker_payload(blocker)
        payload["movedStartAt"] = operation["startAt"]
        payload["movedEndAt"] = operation["endAt"]
        relocated_payloads.append(payload)
    relocation_summary = "; ".join(
        f"{item['taskTitle']} was blocking "
        f"{_human_clock(datetime.fromisoformat(str(item['startAt'])))} to "
        f"{_human_clock(datetime.fromisoformat(str(item['endAt'])))} and moves to "
        f"{_human_clock(datetime.fromisoformat(str(item['movedStartAt'])))} to "
        f"{_human_clock(datetime.fromisoformat(str(item['movedEndAt'])))}"
        for item in relocated_payloads
    )
    return {
        "solver_result": incoming_result,
        "operations": relocation_operations,
        "resolution": {
            "action": "moved_existing",
            "incomingTaskId": task.task_id,
            "incomingTaskTitle": task.title,
            "incomingPriority": decision.incoming_priority,
            "requestedStartAt": _iso(requested_start),
            "requestedEndAt": _iso(requested_end),
            "blockers": relocated_payloads,
            "userMessage": (
                f"{task.title} takes precedence for this time because "
                f"{decision.reason.rstrip('.').lower()}. I kept it at "
                f"{_human_clock(requested_start)}. {relocation_summary}."
            ),
        },
        "window_end": max(
            window_end,
            max(interval.end_at for interval in proposed_occupied),
        ),
        "additional_task_nodes": [item.task for item in decision.blockers],
    }


def _split_session_working_set(
    working_set: PlannerWorkingSet,
    *,
    prompt: str,
    temporary_breaks: list[OccupiedInterval] | None = None,
    anchored_breaks: list[OccupiedInterval] | None = None,
    calendar_occupancy: list[OccupiedInterval] | None = None,
    window_start: datetime | None = None,
    forced_session_minutes_by_task_id: dict[int, int] | None = None,
    contiguous_task_ids: set[int] | None = None,
) -> tuple[PlannerWorkingSet, list[dict[str, Any]]]:
    split_tasks = []
    plan: list[dict[str, Any]] = []
    projected_cursor = window_start
    for task in working_set.tasks:
        if task.task_id in (contiguous_task_ids or set()):
            split_tasks.append(task)
            if projected_cursor is not None:
                projected_cursor += timedelta(
                    minutes=(
                        max(0, task.gap_before_minutes)
                        + task.duration_minutes
                        + max(0, task.gap_after_minutes)
                    )
                )
            continue
        session_minutes = _requested_session_minutes(prompt, task.duration_minutes)
        split_reason = "user_requested"
        session_allocations: list[int] | None = None
        split_anchor: OccupiedInterval | None = None
        forced_session_minutes = (forced_session_minutes_by_task_id or {}).get(task.task_id)
        if session_minutes is None and isinstance(forced_session_minutes, int):
            session_minutes = min(task.duration_minutes, max(15, forced_session_minutes))
            split_reason = "fragmented_availability"
        if (
            session_minutes is None
            and projected_cursor is not None
            and task.duration_minutes >= 120
            and (temporary_breaks or anchored_breaks)
        ):
            task_start = projected_cursor + timedelta(minutes=max(0, task.gap_before_minutes))
            task_end = task_start + timedelta(minutes=task.duration_minutes)
            crossing_break = next(
                (
                    interval
                    for interval in sorted(
                        [*(temporary_breaks or []), *(anchored_breaks or [])],
                        key=lambda item: item.start_at,
                    )
                    if task_start < interval.start_at < task_end
                ),
                None,
            )
            if crossing_break is not None:
                for occupied in sorted(
                    calendar_occupancy or [],
                    key=lambda item: item.end_at,
                ):
                    if (
                        occupied.start_at < crossing_break.start_at
                        and occupied.end_at > task_start
                    ):
                        task_start = max(task_start, occupied.end_at)
                available_before_break = int(
                    (crossing_break.start_at - task_start).total_seconds() // 60
                )
                remaining_after_break = task.duration_minutes - available_before_break
                if 30 <= available_before_break and remaining_after_break >= 15:
                    session_minutes = available_before_break
                    session_allocations = [available_before_break, remaining_after_break]
                    split_anchor = crossing_break
                    split_reason = (
                        "anchored_calendar_item_inside_long_task"
                        if crossing_break.source_type == "anchored_calendar"
                        else "protected_break_inside_long_task"
                    )
        if session_minutes is None or session_minutes >= task.duration_minutes:
            split_tasks.append(task)
            if projected_cursor is not None:
                projected_cursor += timedelta(
                    minutes=(
                        max(0, task.gap_before_minutes)
                        + task.duration_minutes
                        + max(0, task.gap_after_minutes)
                    )
                )
            continue
        allocations = session_allocations
        if allocations is None:
            allocations = []
            remaining = task.duration_minutes
            while remaining > 0:
                allocated = min(session_minutes, remaining)
                allocations.append(allocated)
                remaining -= allocated
        for allocated in allocations:
            split_tasks.append(replace(task, duration_minutes=allocated))
        segment = len(allocations)
        anchor_payload = (
            {
                "label": split_anchor.source_label or "an existing calendar item",
                "startAt": _iso(split_anchor.start_at),
                "endAt": _iso(split_anchor.end_at),
                "kind": split_anchor.source_type,
            }
            if split_anchor is not None
            else None
        )
        plan.append(
            {
                "taskId": task.task_id,
                "taskTitle": task.title,
                "totalMinutes": task.duration_minutes,
                "sessionMinutes": session_minutes,
                "sessionDurations": allocations,
                "sessionCount": segment,
                "reason": split_reason,
                "anchor": anchor_payload,
                "userMessage": (
                    f"{split_anchor.source_label or 'An existing calendar item'} is already set "
                    f"for {_human_clock(split_anchor.start_at)}–{_human_clock(split_anchor.end_at)}, so I split "
                    f"{task.title} into work before and after it."
                    if split_anchor is not None
                    else None
                ),
            }
        )
        if projected_cursor is not None:
            if split_anchor is not None and session_allocations is not None:
                projected_cursor = split_anchor.end_at + timedelta(
                    minutes=(
                        session_allocations[-1]
                        + max(0, task.gap_after_minutes)
                    )
                )
            else:
                projected_cursor += timedelta(
                    minutes=(
                        max(0, task.gap_before_minutes)
                        + task.duration_minutes
                        + max(0, task.gap_after_minutes)
                    )
                )
    if not plan:
        return working_set, []
    return (
        PlannerWorkingSet(
            tasks=split_tasks,
            fallback_duration_task_ids=working_set.fallback_duration_task_ids,
            task_nodes=working_set.task_nodes,
            categories_by_id=working_set.categories_by_id,
        ),
        plan,
    )


def _planner_agent_descriptor() -> dict[str, Any]:
    """Describe the shared agent contract without turning this harness into an LLM router."""

    return {
        "agentProfile": get_agent_profile("planner"),
        "capabilityCatalog": compact_capability_catalog(agent="planner"),
        "capabilitySelection": "available_for_agentic_planner_model_loop",
    }


def _operations_from_solver(
    *,
    working_set: PlannerWorkingSet,
    placements_by_task_id: dict[int, Any],
    operation_mode: str = "create",
    accepted_blocks_by_task_id: dict[int, CalendarBlock] | None = None,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for task_ref in working_set.tasks:
        placement = placements_by_task_id.get(task_ref.task_id)
        if placement is None:
            continue
        accepted_block = (accepted_blocks_by_task_id or {}).get(task_ref.task_id)
        effective_operation_mode = (
            "create"
            if operation_mode == "reschedule" and accepted_block is None
            else operation_mode
        )
        operation = {
            "op": "update_block_time" if effective_operation_mode == "reschedule" else "create_block",
                "proposalBlockId": _proposal_block_id(task_ref.task_id, placement.start_at),
                "taskId": task_ref.task_id,
                "categoryId": task_ref.category_id,
                "title": task_ref.title,
                "emoji": task_ref.emoji,
                "categoryName": task_ref.category_name,
                "categoryColor": task_ref.category_color,
                "startAt": _iso(placement.start_at),
                "endAt": _iso(placement.end_at),
                "variant": "proposed",
                "completionStatus": "not_started",
                "taskStatus": task_ref.status,
                "taskDeadlineAt": task_ref.deadline_at,
                "taskPriority": task_ref.priority,
                "timeEstimateMinutes": task_ref.time_estimate_minutes,
                "allocatedTimeboxMinutes": task_ref.duration_minutes,
                "isPinned": False,
                "isProtected": False,
                "isRecurring": False,
                "hierarchyDepth": 0,
                "renderedChildTaskIds": task_ref.rendered_child_task_ids,
                "source": "solver",
            }
        if accepted_block is not None and accepted_block.id is not None:
            operation["acceptedBlockId"] = accepted_block.id
            operation["expectedOriginalStartAt"] = _iso(accepted_block.start_at)
            operation["expectedOriginalEndAt"] = _iso(accepted_block.end_at)
        operations.append(operation)
    return operations


def _operations_from_split_placements(
    *,
    working_set: PlannerWorkingSet,
    placements: list[Any],
) -> list[dict[str, Any]]:
    refs_by_id = {task.task_id: task for task in working_set.tasks}
    operations: list[dict[str, Any]] = []
    for placement in placements:
        task_ref = refs_by_id.get(int(placement.task_id))
        if task_ref is None:
            continue
        allocated_minutes = int((placement.end_at - placement.start_at).total_seconds() // 60)
        operations.append(
            {
                "op": "create_block",
                "proposalBlockId": _proposal_block_id(task_ref.task_id, placement.start_at),
                "taskId": task_ref.task_id,
                "categoryId": task_ref.category_id,
                "title": task_ref.title,
                "emoji": task_ref.emoji,
                "categoryName": task_ref.category_name,
                "categoryColor": task_ref.category_color,
                "startAt": _iso(placement.start_at),
                "endAt": _iso(placement.end_at),
                "variant": "proposed",
                "completionStatus": "not_started",
                "taskStatus": task_ref.status,
                "taskDeadlineAt": task_ref.deadline_at,
                "taskPriority": task_ref.priority,
                "timeEstimateMinutes": task_ref.time_estimate_minutes,
                "allocatedTimeboxMinutes": allocated_minutes,
                "isPinned": False,
                "isProtected": False,
                "isRecurring": False,
                "hierarchyDepth": 0,
                "renderedChildTaskIds": task_ref.rendered_child_task_ids,
                "source": "solver_split_session",
            }
        )
    return operations


def _placement_reasons(
    *,
    operations: list[dict[str, Any]],
    working_set: PlannerWorkingSet,
    planner_plan: dict[str, Any],
    split_session_plan: list[dict[str, Any]],
    accepted_blocks: list[CalendarBlock],
    block_labels_by_id: dict[int, str],
    request_constraints: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Explain deterministic placement choices in language useful to the user and follow-ups."""

    reasons: list[dict[str, Any]] = []
    for split in split_session_plan:
        if not isinstance(split, dict):
            continue
        message = str(split.get("userMessage") or "").strip()
        if message:
            reasons.append(
                {
                    "kind": "split_around_anchor",
                    "message": message,
                    "taskIds": [split.get("taskId")],
                    "anchor": split.get("anchor"),
                }
            )

    task_by_id = {task.task_id: task for task in working_set.tasks}
    gap_policy = planner_plan.get("gapPolicy") if isinstance(planner_plan.get("gapPolicy"), dict) else {}
    conditional_rules = [
        rule
        for rule in gap_policy.get("conditionalGapRules", [])
        if isinstance(rule, dict)
        and isinstance(rule.get("gapMinutes"), int)
        and isinstance(rule.get("minimumTaskDurationMinutes"), int)
    ]
    ordered_operations = sorted(
        [
            operation
            for operation in operations
            if isinstance(operation.get("startAt"), str)
            and isinstance(operation.get("endAt"), str)
        ],
        key=lambda operation: str(operation["startAt"]),
    )
    for current, following in zip(ordered_operations, ordered_operations[1:]):
        current_id = current.get("taskId")
        following_id = following.get("taskId")
        if current_id == following_id or not isinstance(current_id, int) or not isinstance(following_id, int):
            continue
        try:
            current_end = datetime.fromisoformat(str(current["endAt"]))
            following_start = datetime.fromisoformat(str(following["startAt"]))
        except ValueError:
            continue
        gap_minutes = max(0, int((following_start - current_end).total_seconds() // 60))
        current_task = task_by_id.get(current_id)
        following_task = task_by_id.get(following_id)
        current_title = str(current.get("title") or (current_task.title if current_task else "this task"))
        following_title = str(
            following.get("title") or (following_task.title if following_task else "the next task")
        )
        blocker = next(
            (
                block
                for block in accepted_blocks
                if local_wall_time(block.start_at) < following_start
                and local_wall_time(block.end_at) > current_end
            ),
            None,
        )
        if blocker is not None:
            label = block_labels_by_id.get(int(blocker.id or 0), "An existing calendar item")
            reasons.append(
                {
                    "kind": "calendar_anchor",
                    "message": (
                        f"{label} is already set for {_human_clock(local_wall_time(blocker.start_at))}–"
                        f"{_human_clock(local_wall_time(blocker.end_at))}, so I planned around it."
                    ),
                    "taskIds": [current_id, following_id],
                    "anchor": {
                        "label": label,
                        "startAt": _iso(local_wall_time(blocker.start_at)),
                        "endAt": _iso(local_wall_time(blocker.end_at)),
                    },
                }
            )
            continue
        request_blocker = next(
            (
                constraint
                for constraint in request_constraints or []
                if constraint.get("kind") == "unavailable_interval"
                and isinstance(constraint.get("startAt"), str)
                and isinstance(constraint.get("endAt"), str)
                and datetime.fromisoformat(str(constraint["startAt"])) >= current_end
                and datetime.fromisoformat(str(constraint["endAt"])) <= following_start
            ),
            None,
        )
        if request_blocker is not None:
            label = str(request_blocker.get("label") or "Your requested break")
            start_at = datetime.fromisoformat(str(request_blocker["startAt"]))
            end_at = datetime.fromisoformat(str(request_blocker["endAt"]))
            reasons.append(
                {
                    "kind": "request_constraint",
                    "message": (
                        f"{label} is fixed for {_human_clock(start_at)}–{_human_clock(end_at)}, "
                        f"so I scheduled {following_title} after it."
                    ),
                    "taskIds": [current_id, following_id],
                    "anchor": {
                        "label": label,
                        "startAt": _iso(start_at),
                        "endAt": _iso(end_at),
                    },
                }
            )
            continue
        for rule in conditional_rules:
            threshold = int(rule["minimumTaskDurationMinutes"])
            preferred_gap = int(rule["gapMinutes"])
            current_minutes = current_task.duration_minutes if current_task is not None else None
            following_minutes = following_task.duration_minutes if following_task is not None else None
            both_short = (
                isinstance(current_minutes, int)
                and isinstance(following_minutes, int)
                and current_minutes < threshold
                and following_minutes < threshold
            )
            if both_short and gap_minutes == int(rule.get("shorterTaskGapMinutes") or 0):
                reasons.append(
                    {
                        "kind": "saved_gap_preference",
                        "message": (
                            f"{current_title} and {following_title} are back-to-back because "
                            f"both are under {threshold} minutes, matching your saved preference."
                        ),
                        "taskIds": [current_id, following_id],
                    }
                )
            elif not both_short and gap_minutes == preferred_gap:
                reasons.append(
                    {
                        "kind": "saved_gap_preference",
                        "message": (
                            f"I left {preferred_gap} minutes between "
                            f"{current_title} and {following_title}, matching your saved preference "
                            f"for tasks lasting {threshold} minutes or longer."
                        ),
                        "taskIds": [current_id, following_id],
                    }
                )
            break
    unique: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    for reason in reasons:
        message = str(reason.get("message") or "").strip()
        if not message or message in seen_messages:
            continue
        seen_messages.add(message)
        unique.append(reason)
    return unique


def created_task_schedule_groups(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return independent one-time placement groups from the proposal contract.

    The legacy shape is one group at the top level. ``schedule_groups`` extends
    that contract without changing approval/apply semantics: each group owns a
    distinct placement window, while all groups still compile into one proposal.
    """

    raw_groups = spec.get("schedule_groups")
    if isinstance(raw_groups, list):
        return [dict(group) for group in raw_groups if isinstance(group, dict)]
    return [spec]


def _bounded_duration_estimate(value: int | float) -> int:
    minutes = max(5, min(480, int(round(float(value) / 5) * 5)))
    return minutes


def _estimate_created_task_duration(
    *,
    operation: dict[str, Any],
    estimated_nodes: list[StrategyNode],
) -> dict[str, Any]:
    """Return a bounded Planner estimate without requiring another user turn.

    The Strategist should normally provide its common-sense estimate. This
    fallback learns from the user's saved task estimates before using a neutral
    planning default, so scheduling remains useful when a provider omits one.
    """

    title = str(operation.get("title") or "").strip().casefold()
    category_id = operation.get("category_id")
    observed = [
        node
        for node in estimated_nodes
        if isinstance(node.time_estimate_minutes, int)
        and not isinstance(node.time_estimate_minutes, bool)
        and node.time_estimate_minutes > 0
    ]
    exact_same_workspace = [
        int(node.time_estimate_minutes)
        for node in observed
        if node.title.strip().casefold() == title and node.category_id == category_id
    ]
    exact_any_workspace = [
        int(node.time_estimate_minutes)
        for node in observed
        if node.title.strip().casefold() == title
    ]
    workspace_history = [
        int(node.time_estimate_minutes)
        for node in observed
        if node.category_id == category_id and int(node.time_estimate_minutes) <= 480
    ]
    personal_history = [
        int(node.time_estimate_minutes)
        for node in observed
        if int(node.time_estimate_minutes) <= 480
    ]
    if exact_same_workspace:
        minutes = _bounded_duration_estimate(median(exact_same_workspace))
        source = "matching_task_history"
        confidence = 0.9
    elif exact_any_workspace:
        minutes = _bounded_duration_estimate(median(exact_any_workspace))
        source = "matching_task_history"
        confidence = 0.82
    elif workspace_history:
        minutes = _bounded_duration_estimate(median(workspace_history))
        source = "workspace_history"
        confidence = 0.68
    elif personal_history:
        minutes = _bounded_duration_estimate(median(personal_history))
        source = "personal_task_history"
        confidence = 0.58
    else:
        minutes = 45
        source = "common_sense_default"
        confidence = 0.4
    return {
        "minutes": minutes,
        "source": source,
        "confidence": confidence,
    }


def build_created_task_schedule_plan(
    session: Session,
    *,
    spec: dict[str, Any],
    create_operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Plan finite calendar blocks for tasks created by the same approval."""

    if "schedule_groups" in spec:
        groups = created_task_schedule_groups(spec)
        if not groups:
            return {
                "status": "invalid",
                "errors": ["Add at least one scheduling group."],
                "schedule_group_count": 0,
            }
        if any("schedule_groups" in group for group in groups):
            return {
                "status": "invalid",
                "errors": ["Scheduling groups cannot contain nested scheduling groups."],
                "schedule_group_count": len(groups),
            }
        target_temp_ids = [
            str(temp_id).strip()
            for group in groups
            for temp_id in group.get("target_temp_ids", [])
            if isinstance(temp_id, str) and str(temp_id).strip()
        ]
        duplicate_target_ids = sorted(
            {
                temp_id
                for temp_id in target_temp_ids
                if target_temp_ids.count(temp_id) > 1
            }
        )
        if duplicate_target_ids:
            return {
                "status": "invalid",
                "errors": [
                    "Each created task may appear in only one one-time scheduling group."
                ],
                "duplicate_target_temp_ids": duplicate_target_ids,
                "schedule_group_count": len(groups),
            }
        target_task_ids = [
            int(task_id)
            for group in groups
            for task_id in group.get("target_task_ids", [])
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        ]
        duplicate_task_ids = sorted(
            {
                task_id
                for task_id in target_task_ids
                if target_task_ids.count(task_id) > 1
            }
        )
        if duplicate_task_ids:
            return {
                "status": "invalid",
                "errors": [
                    "Each existing task may appear in only one one-time scheduling group."
                ],
                "duplicate_target_task_ids": duplicate_task_ids,
                "schedule_group_count": len(groups),
            }
        # Solve constrained/narrow groups first, then reserve their placements
        # while solving broader sibling windows.  Independent solves without
        # shared occupancy can choose the same time and force an unnecessary
        # model repair even though the combined request is feasible.
        def group_order(item: tuple[int, dict[str, Any]]) -> tuple[int, float, int]:
            index, group = item
            try:
                start = datetime.fromisoformat(str(group.get("window_start") or ""))
                end = datetime.fromisoformat(str(group.get("window_end") or ""))
                width = max(0.0, (end - start).total_seconds())
            except (TypeError, ValueError):
                width = float("inf")
            return (0 if group.get("fixed") is True else 1, width, index)

        group_plans_by_index: dict[int, dict[str, Any]] = {}
        reserved_intervals: list[OccupiedInterval] = []
        for group_index, group in sorted(enumerate(groups), key=group_order):
            plan = build_created_task_schedule_plan(
                session,
                spec={
                    **group,
                    "_compound_existing_target_task_ids": target_task_ids,
                    "_compound_reserved_intervals": list(reserved_intervals),
                },
                create_operations=create_operations,
            )
            group_plans_by_index[group_index] = plan
            if plan.get("status") != "valid":
                continue
            for block_index, block in enumerate(plan.get("blocks") or []):
                if not isinstance(block, dict):
                    continue
                try:
                    reserved_start = datetime.fromisoformat(str(block.get("start_at") or ""))
                    reserved_end = datetime.fromisoformat(str(block.get("end_at") or ""))
                except ValueError:
                    continue
                if reserved_end <= reserved_start:
                    continue
                reserved_intervals.append(
                    OccupiedInterval(
                        block_id=-(1_000_000 + group_index * 1_000 + block_index),
                        start_at=reserved_start,
                        end_at=reserved_end,
                        source_type="compound_proposal",
                        source_label=str(block.get("task_title") or "Proposed task"),
                    )
                )
        group_plans = [group_plans_by_index[index] for index in range(len(groups))]
        errors = list(
            dict.fromkeys(
                str(error).strip()
                for plan in group_plans
                for error in plan.get("errors", [])
                if isinstance(error, str) and error.strip()
            )
        )
        blocks_with_group = [
            (group_index, block)
            for group_index, plan in enumerate(group_plans)
            for block in plan.get("blocks", [])
            if isinstance(block, dict)
        ]
        cross_group_overlaps: list[dict[str, Any]] = []
        for index, (left_group, left_block) in enumerate(blocks_with_group):
            for right_group, right_block in blocks_with_group[index + 1 :]:
                if left_group == right_group:
                    continue
                left_start = datetime.fromisoformat(str(left_block.get("start_at")))
                left_end = datetime.fromisoformat(str(left_block.get("end_at")))
                right_start = datetime.fromisoformat(str(right_block.get("start_at")))
                right_end = datetime.fromisoformat(str(right_block.get("end_at")))
                if left_start < right_end and right_start < left_end:
                    cross_group_overlaps.append(
                        {
                            "left_target_temp_id": left_block.get("target_temp_id"),
                            "right_target_temp_id": right_block.get("target_temp_id"),
                            "left_start_at": left_block.get("start_at"),
                            "left_end_at": left_block.get("end_at"),
                            "right_start_at": right_block.get("start_at"),
                            "right_end_at": right_block.get("end_at"),
                        }
                    )
        if cross_group_overlaps:
            errors.append(
                "Separately scheduled task groups overlap each other; combine them into one "
                "shared window or choose non-overlapping windows."
            )
        valid = (
            all(plan.get("status") == "valid" for plan in group_plans)
            and not cross_group_overlaps
        )
        parsed_starts = [
            datetime.fromisoformat(str(plan["window_start"]))
            for plan in group_plans
            if plan.get("window_start")
        ]
        parsed_ends = [
            datetime.fromisoformat(str(plan["window_end"]))
            for plan in group_plans
            if plan.get("window_end")
        ]
        return {
            "status": "valid" if valid else "invalid",
            "errors": errors,
            "instruction": str(spec.get("instruction") or "").strip()
            or "Schedule the reviewed task groups in their specified windows.",
            "window_start": _iso(min(parsed_starts)) if parsed_starts else None,
            "window_end": _iso(max(parsed_ends)) if parsed_ends else None,
            "task_count": sum(int(plan.get("task_count") or 0) for plan in group_plans),
            "block_count": sum(int(plan.get("block_count") or 0) for plan in group_plans),
            "expected_minutes": sum(
                int(plan.get("expected_minutes") or 0) for plan in group_plans
            ),
            "placed_minutes": sum(
                int(plan.get("placed_minutes") or 0) for plan in group_plans
            ),
            "unplaced_minutes": sum(
                int(plan.get("unplaced_minutes") or 0) for plan in group_plans
            ),
            "blocks": [block for _, block in blocks_with_group],
            "split_session_plan": [
                item
                for plan in group_plans
                for item in plan.get("split_session_plan", [])
                if isinstance(item, dict)
            ],
            "planner_plan": [
                plan.get("planner_plan")
                for plan in group_plans
                if plan.get("planner_plan") is not None
            ],
            "placement_reasons": [
                item
                for plan in group_plans
                for item in plan.get("placement_reasons", [])
                if isinstance(item, dict)
            ],
            "conflict_resolutions": [
                item
                for plan in group_plans
                for item in plan.get("conflict_resolutions", [])
                if isinstance(item, dict)
            ],
            "priority_conflicts": [
                item
                for plan in group_plans
                for item in plan.get("priority_conflicts", [])
                if isinstance(item, dict)
            ],
            "conflicting_blocks": [
                item
                for plan in group_plans
                for item in plan.get("conflicting_blocks", [])
                if isinstance(item, dict)
            ],
            "preserved_blockers": [
                item
                for plan in group_plans
                for item in plan.get("preserved_blockers", [])
                if isinstance(item, dict)
            ],
            "displacement_operations": [
                operation
                for plan in group_plans
                for operation in plan.get("displacement_operations", [])
                if isinstance(operation, dict)
            ],
            "temporary_breaks": [
                item
                for plan in group_plans
                for item in plan.get("temporary_breaks", [])
                if isinstance(item, dict)
            ],
            "validation_warnings": list(
                dict.fromkeys(
                    str(warning).strip()
                    for plan in group_plans
                    for warning in plan.get("validation_warnings", [])
                    if isinstance(warning, str) and warning.strip()
                )
            ),
            "fallback_duration_temp_ids": list(
                dict.fromkeys(
                    str(temp_id).strip()
                    for plan in group_plans
                    for temp_id in plan.get("fallback_duration_temp_ids", [])
                    if isinstance(temp_id, str) and temp_id.strip()
                )
            ),
            "inferred_duration_temp_ids": list(
                dict.fromkeys(
                    str(temp_id).strip()
                    for plan in group_plans
                    for temp_id in plan.get("inferred_duration_temp_ids", [])
                    if isinstance(temp_id, str) and temp_id.strip()
                )
            ),
            "duration_estimates": {
                str(temp_id): estimate
                for plan in group_plans
                for temp_id, estimate in (
                    plan.get("duration_estimates", {}).items()
                    if isinstance(plan.get("duration_estimates"), dict)
                    else []
                )
            },
            "schedule_group_count": len(group_plans),
            "schedule_groups": group_plans,
            "cross_group_overlaps": cross_group_overlaps,
        }

    try:
        window_start = datetime.fromisoformat(str(spec.get("window_start") or ""))
        window_end = datetime.fromisoformat(str(spec.get("window_end") or ""))
        # This contract carries local wall time. Providers sometimes echo the
        # workspace's numeric offset; preserve the user's wall-clock fields
        # instead of rejecting an otherwise unambiguous appointment.
        if window_start.tzinfo is not None and window_start.utcoffset() is not None:
            window_start = window_start.replace(tzinfo=None)
        if window_end.tzinfo is not None and window_end.utcoffset() is not None:
            window_end = window_end.replace(tzinfo=None)
        window_start = local_wall_time(window_start)
        window_end = local_wall_time(window_end)
    except ValueError:
        return {"status": "invalid", "errors": ["Choose a valid scheduling window."]}
    slot_minutes = spec.get("slot_minutes", 5)
    if isinstance(slot_minutes, bool) or not isinstance(slot_minutes, int):
        slot_minutes = 5
    slot_minutes = max(1, min(60, slot_minutes))
    window_start = _ceil_to_slot_boundary(window_start, slot_minutes)
    if window_end <= window_start:
        return {"status": "invalid", "errors": ["The scheduling window must end after it starts."]}

    create_by_temp_id = {
        str(operation.get("temp_id") or "").strip(): operation
        for operation in create_operations
        if str(operation.get("temp_id") or "").strip()
    }
    requested_temp_ids = [
        str(item).strip()
        for item in spec.get("target_temp_ids", [])
        if isinstance(item, str) and str(item).strip()
    ]
    if not requested_temp_ids:
        requested_temp_ids = list(create_by_temp_id) if not spec.get("target_task_ids") else []
    requested_task_ids = list(
        dict.fromkeys(
            int(item)
            for item in spec.get("target_task_ids", [])
            if isinstance(item, int) and not isinstance(item, bool)
        )
    )
    if (
        (not requested_temp_ids and not requested_task_ids)
        or any(item not in create_by_temp_id for item in requested_temp_ids)
    ):
        return {
            "status": "invalid",
            "errors": [
                "Every scheduled target must reference a task created in this proposal "
                "or an existing task ID."
            ],
        }

    existing_working_set = (
        resolve_selected_working_set(
            session,
            task_ids=requested_task_ids,
            fallback_duration_minutes=45,
        )
        if requested_task_ids
        else PlannerWorkingSet(
            tasks=[],
            fallback_duration_task_ids=[],
            task_nodes=[],
            categories_by_id={},
        )
    )
    resolved_existing_ids = {task.task_id for task in existing_working_set.tasks}
    missing_existing_ids = sorted(set(requested_task_ids) - resolved_existing_ids)
    if missing_existing_ids:
        return {
            "status": "invalid",
            "errors": ["Every existing scheduling target must be a live task."],
            "missing_target_task_ids": missing_existing_ids,
        }

    categories = list(session.exec(select(Category)).all())
    category_by_id = {
        int(category.id): category
        for category in categories
        if isinstance(category.id, int)
    }
    synthetic_id_by_temp_id = {
        temp_id: -(index + 1)
        for index, temp_id in enumerate(requested_temp_ids)
    }
    temp_id_by_synthetic_id = {
        synthetic_id: temp_id
        for temp_id, synthetic_id in synthetic_id_by_temp_id.items()
    }
    tasks: list[PlannerTaskRef] = []
    synthetic_task_nodes: list[StrategyNode] = []
    fallback_duration_temp_ids: list[str] = []
    fallback_duration_task_ids: list[int] = []
    inferred_duration_temp_ids: list[str] = []
    duration_estimates: dict[str, dict[str, Any]] = {}
    estimated_nodes = list(
        session.exec(
            select(StrategyNode).where(
                StrategyNode.time_estimate_minutes.is_not(None)
            )
        ).all()
    )
    bounded_single_target_minutes = int(
        (window_end - window_start).total_seconds() // 60
    )
    if not (
        len(requested_temp_ids) == 1
        and not requested_task_ids
        and 5 <= bounded_single_target_minutes <= 30
    ):
        bounded_single_target_minutes = 0
    for temp_id in requested_temp_ids:
        operation = create_by_temp_id[temp_id]
        category_id = operation.get("category_id")
        fields = operation.get("fields") if isinstance(operation.get("fields"), dict) else {}
        duration = fields.get("time_estimate_minutes")
        if not isinstance(category_id, int) or category_id not in category_by_id:
            return {"status": "invalid", "errors": ["Choose a workspace for every new task."]}
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            estimate = (
                {
                    "minutes": bounded_single_target_minutes,
                    "source": "bounded_single_task_schedule_window",
                    "confidence": 0.9,
                }
                if bounded_single_target_minutes
                else _estimate_created_task_duration(
                    operation=operation,
                    estimated_nodes=estimated_nodes,
                )
            )
            duration = int(estimate["minutes"])
            duration_estimates[temp_id] = estimate
            inferred_duration_temp_ids.append(temp_id)
            fallback_duration_temp_ids.append(temp_id)
        category = category_by_id[category_id]
        synthetic_task_id = synthetic_id_by_temp_id[temp_id]
        if temp_id in fallback_duration_temp_ids:
            fallback_duration_task_ids.append(synthetic_task_id)
        task_title = str(operation.get("title") or "Task").strip() or "Task"
        task_priority = str(fields.get("priority") or "medium")
        task_status = str(fields.get("status") or "not_started")
        task_deadline = str(fields.get("deadline_at")) if fields.get("deadline_at") else None
        task_emoji = str(fields.get("emoji") or "").strip() or None
        tasks.append(
            PlannerTaskRef(
                task_id=synthetic_task_id,
                category_id=category_id,
                title=task_title,
                emoji=task_emoji,
                category_name=category.name,
                category_color=category.accent_color,
                status=task_status,
                priority=task_priority,
                deadline_at=task_deadline,
                time_estimate_minutes=duration,
                duration_minutes=duration,
                used_fallback_duration=temp_id in fallback_duration_temp_ids,
            )
        )
        # The shared collision policy reasons over StrategyNode evidence. New
        # tasks do not exist in the database until approval, so give the
        # planner an in-memory node with the same typed fields instead of
        # bypassing conflict resolution for create-and-schedule proposals.
        synthetic_task_nodes.append(
            StrategyNode(
                id=synthetic_task_id,
                category_id=category_id,
                title=task_title,
                emoji=task_emoji,
                status=task_status,
                priority=task_priority,
                deadline_at=task_deadline,
                time_estimate_minutes=duration,
            )
        )
    tasks.extend(existing_working_set.tasks)
    synthetic_task_nodes.extend(existing_working_set.task_nodes)
    fallback_duration_task_ids.extend(existing_working_set.fallback_duration_task_ids)
    working_set = PlannerWorkingSet(
        tasks=tasks,
        fallback_duration_task_ids=fallback_duration_task_ids,
        task_nodes=synthetic_task_nodes,
        categories_by_id=category_by_id,
    )
    instruction = str(spec.get("instruction") or "").strip()
    raw_alternative_window_end = spec.get("alternative_window_end")
    try:
        alternative_window_end = local_wall_time(
            datetime.fromisoformat(str(raw_alternative_window_end))
        )
    except (TypeError, ValueError):
        alternative_window_end = max(
            window_end + timedelta(hours=6),
            datetime.combine(window_end.date(), time(23, 59)),
        )
    if alternative_window_end <= window_end:
        alternative_window_end = window_end + timedelta(hours=6)
    accepted_blocks = read_calendar_state(
        session,
        window_start=window_start,
        # Relocating a flexible blocker requires visibility beyond the exact
        # incoming slot. The new task is still constrained to window_end; this
        # wider read is only the nearby search area for affected blocks.
        window_end=alternative_window_end,
    )
    compound_existing_target_task_ids = {
        int(task_id)
        for task_id in spec.get("_compound_existing_target_task_ids", [])
        if isinstance(task_id, int) and not isinstance(task_id, bool)
    }
    if compound_existing_target_task_ids:
        accepted_blocks = [
            block
            for block in accepted_blocks
            if block.task_id not in compound_existing_target_task_ids
        ]
    # Existing targets can be either unscheduled tasks (create a block) or an
    # already-scheduled task the user is moving. Resolve the source block once,
    # then remove it from occupancy so a move is never reported as conflicting
    # with itself. Ambiguous multi-block/series targets remain an explicit error.
    source_block_by_task_id: dict[int, CalendarBlock] = {}
    if requested_task_ids:
        active_source_blocks = [
            block
            for block in session.exec(
                select(CalendarBlock)
                .where(CalendarBlock.task_id.in_(requested_task_ids))
                .order_by(CalendarBlock.start_at, CalendarBlock.id)
            ).all()
            if str(block.completion_status or "").strip().casefold()
            not in {"completed", "done", "cancelled", "canceled"}
        ]
        for task_id in requested_task_ids:
            candidates = [
                block for block in active_source_blocks if block.task_id == task_id
            ]
            overlapping = [
                block
                for block in candidates
                if block.start_at < window_end and block.end_at > window_start
            ]
            same_day = [
                block
                for block in candidates
                if local_wall_time(block.start_at).date() == window_start.date()
            ]
            resolved = (
                overlapping
                if len(overlapping) == 1
                else same_day
                if len(same_day) == 1
                else candidates
            )
            if len(resolved) > 1:
                return {
                    "status": "invalid",
                    "errors": [
                        "The existing task has multiple active calendar blocks. "
                        "Choose the occurrence or block to move."
                    ],
                    "ambiguous_target_task_ids": [task_id],
                    "solver_status": "ambiguous_existing_schedule_target",
                }
            if resolved:
                source = resolved[0]
                if source.is_protected or source.is_pinned:
                    state = "protected" if source.is_protected else "pinned"
                    return {
                        "status": "invalid",
                        "errors": [
                            f"The existing task's calendar block is {state} and cannot be moved."
                        ],
                        "protected_target_task_ids": [task_id],
                        "solver_status": "protected_existing_schedule_target",
                    }
                source_block_by_task_id[task_id] = source
        source_block_ids = {
            int(block.id)
            for block in source_block_by_task_id.values()
            if isinstance(block.id, int)
        }
        accepted_blocks = [
            block
            for block in accepted_blocks
            if not isinstance(block.id, int) or int(block.id) not in source_block_ids
        ]
    anchored_breaks = _anchored_calendar_intervals(session, accepted_blocks)
    calendar_occupancy = _calendar_occupancy_intervals(accepted_blocks)
    preference_context = build_planner_preference_context(
        session,
        working_set=working_set,
        window_start=window_start,
        window_end=window_end,
        prompt=instruction,
    )
    occupied = constraint_intervals_from_context(
        preference_context,
        window_start=window_start,
        window_end=window_end,
    )
    temporary_breaks = _one_off_break_intervals_from_prompt(
        instruction,
        window_start=window_start,
        window_end=window_end,
    )
    raw_request_constraints: list[dict[str, Any]] = []
    for raw_constraint in spec.get("request_constraints", []):
        if not isinstance(raw_constraint, dict):
            continue
        constraint = dict(raw_constraint)
        raw_constraint_task_ids = [
            int(task_id)
            for task_id in constraint.get("task_ids", constraint.get("taskIds", []))
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        ]
        raw_constraint_temp_ids = [
            str(temp_id).strip()
            for temp_id in constraint.get(
                "target_temp_ids", constraint.get("targetTempIds", [])
            )
            if isinstance(temp_id, str) and temp_id.strip()
        ]
        constraint["task_ids"] = list(
            dict.fromkeys(
                [
                    *raw_constraint_task_ids,
                    *[
                        synthetic_id_by_temp_id[temp_id]
                        for temp_id in raw_constraint_temp_ids
                        if temp_id in synthetic_id_by_temp_id
                    ],
                ]
            )
        )
        before_temp_id = str(
            constraint.pop("before_temp_id", constraint.pop("beforeTempId", ""))
            or ""
        ).strip()
        after_temp_id = str(
            constraint.pop("after_temp_id", constraint.pop("afterTempId", ""))
            or ""
        ).strip()
        if before_temp_id in synthetic_id_by_temp_id:
            constraint["before_task_id"] = synthetic_id_by_temp_id[before_temp_id]
        if after_temp_id in synthetic_id_by_temp_id:
            constraint["after_task_id"] = synthetic_id_by_temp_id[after_temp_id]
        raw_request_constraints.append(constraint)
    normalized_request_constraints, request_constraint_errors = normalize_request_constraints(
        raw_request_constraints,
        live_task_ids={task.task_id for task in working_set.tasks},
        window_start=window_start,
        window_end=window_end,
    )
    if request_constraint_errors:
        return {
            "status": "invalid",
            "errors": request_constraint_errors,
            "solver_status": "invalid_request_constraints",
        }
    gap_policy = inter_task_gap_policy_from_context(preference_context)
    heuristic_working_set, default_policy = apply_default_scheduling_heuristics(
        working_set,
        prompt=instruction,
        window_start=window_start,
        window_end=window_end,
        preference_context=preference_context,
        surrounding_tasks=[],
        surrounding_task_windows=[],
    )
    solver_working_set, normalized_plan = apply_planner_task_directives(
        heuristic_working_set,
        [],
        gap_policy=gap_policy,
        default_heavy_task_ids=set(default_policy["heavyTaskIds"]),
        preserve_submitted_order=True,
    )
    solver_working_set = apply_request_constraints(
        solver_working_set,
        normalized_request_constraints,
        slot_minutes=slot_minutes,
    )
    normalized_plan["requestConstraints"] = normalized_request_constraints
    request_constraint_occupied = constraint_occupied_intervals(
        normalized_request_constraints
    )
    compound_reserved_intervals = [
        interval
        for interval in spec.get("_compound_reserved_intervals", [])
        if isinstance(interval, OccupiedInterval)
    ]
    contiguous_task_ids = {
        int(task_id)
        for constraint in normalized_request_constraints
        if constraint.get("kind") == "contiguous" and constraint.get("hard", True)
        for task_id in constraint.get("taskIds", [])
        if isinstance(task_id, int)
    }
    execution_set, split_plan = _split_session_working_set(
        solver_working_set,
        prompt=instruction,
        temporary_breaks=temporary_breaks,
        anchored_breaks=anchored_breaks,
        calendar_occupancy=calendar_occupancy,
        window_start=window_start,
        contiguous_task_ids=contiguous_task_ids,
    )
    solver_result = run_deterministic_schedule_tool(
        working_set=execution_set,
        accepted_blocks=accepted_blocks,
        extra_occupied=[
            *occupied,
            *temporary_breaks,
            *request_constraint_occupied,
            *compound_reserved_intervals,
        ],
        window_start=window_start,
        window_end=window_end,
        slot_minutes=slot_minutes,
        alternative_window_end=window_end,
    )
    priority_relocation_operations: list[dict[str, Any]] = []
    priority_conflict_resolution: dict[str, Any] | None = None
    effective_plan_window_end = window_end
    if not split_plan:
        priority_result = _resolve_single_task_priority_conflict(
            session,
            prompt=instruction,
            working_set=execution_set,
            solver_result=solver_result,
            accepted_blocks=accepted_blocks,
            extra_occupied=[
                *occupied,
                *temporary_breaks,
                *request_constraint_occupied,
                *compound_reserved_intervals,
            ],
            window_start=window_start,
            window_end=window_end,
            alternative_window_end=alternative_window_end,
            slot_minutes=slot_minutes,
            gap_policy=gap_policy,
            hard_exact_constraint=(
                len(execution_set.tasks) == 1
                and _exact_task_range(
                    execution_set.tasks[0],
                    window_start=window_start,
                    window_end=window_end,
                )
                is not None
            ),
        )
        clarification = priority_result.get("clarification")
        if isinstance(clarification, dict):
            conflict_resolution = clarification.get("conflictResolution")
            return {
                "status": "invalid",
                "errors": [
                    str(
                        clarification.get("clarification", {}).get("message")
                        or clarification.get("summary")
                        or "Choose which calendar item should keep this time."
                    )
                ],
                "solver_status": solver_result.solver_status,
                "instruction": instruction,
                "window_start": _iso(window_start),
                "window_end": _iso(window_end),
                "task_count": len(tasks),
                "block_count": 0,
                "priority_conflicts": (
                    [conflict_resolution]
                    if isinstance(conflict_resolution, dict)
                    else []
                ),
                "conflict_resolution": conflict_resolution,
            }
        solver_result = priority_result["solver_result"]
        priority_relocation_operations = list(priority_result.get("operations") or [])
        resolution = priority_result.get("resolution")
        priority_conflict_resolution = resolution if isinstance(resolution, dict) else None
        candidate_window_end = priority_result.get("window_end")
        if isinstance(candidate_window_end, datetime):
            effective_plan_window_end = max(window_end, candidate_window_end)
    expected_minutes = sum(task.duration_minutes for task in solver_working_set.tasks)
    placed_minutes = sum(
        int((placement.end_at - placement.start_at).total_seconds() // 60)
        for placement in solver_result.placements
    )
    if placed_minutes != expected_minutes and not CONTIGUOUS_REQUIRED_RE.search(instruction):
        task_by_id = {task.task_id: task for task in solver_working_set.tasks}
        forced_split_ids = {
            conflict.task_id
            for conflict in solver_result.conflicts
            if (
                conflict.task_id in task_by_id
                and task_by_id[conflict.task_id].duration_minutes > 120
            )
        }
        if forced_split_ids:
            retry_execution_set, retry_split_plan = _split_session_working_set(
                solver_working_set,
                prompt=instruction,
                temporary_breaks=temporary_breaks,
                anchored_breaks=anchored_breaks,
                calendar_occupancy=calendar_occupancy,
                window_start=window_start,
                contiguous_task_ids=contiguous_task_ids,
                forced_session_minutes_by_task_id={
                    task_id: 60 for task_id in forced_split_ids
                },
            )
            retry_result = run_deterministic_schedule_tool(
                working_set=retry_execution_set,
                accepted_blocks=accepted_blocks,
                extra_occupied=[
                    *occupied,
                    *temporary_breaks,
                    *request_constraint_occupied,
                    *compound_reserved_intervals,
                ],
                window_start=window_start,
                window_end=window_end,
                slot_minutes=slot_minutes,
                alternative_window_end=window_end,
            )
            retry_placed_minutes = sum(
                int((placement.end_at - placement.start_at).total_seconds() // 60)
                for placement in retry_result.placements
            )
            if retry_placed_minutes == expected_minutes:
                execution_set = retry_execution_set
                split_plan = retry_split_plan
                solver_result = retry_result
                placed_minutes = retry_placed_minutes
    if placed_minutes != expected_minutes:
        task_by_id = {task.task_id: task for task in solver_working_set.tasks}
        unplaced_task_ids = list(
            dict.fromkeys(
                int(conflict.task_id)
                for conflict in solver_result.conflicts
                if isinstance(conflict.task_id, int)
            )
        )[:32]
        unplaced_titles = list(
            dict.fromkeys(
                task_by_id[task_id].title
                for task_id in unplaced_task_ids
                if task_id in task_by_id and task_by_id[task_id].title
            )
        )
        conflicting_block_ids = {
            int(block_id)
            for conflict in solver_result.conflicts
            for block_id in conflict.conflicting_block_ids
            if isinstance(block_id, int)
        }
        blocker_records: list[dict[str, Any]] = []
        for block in accepted_blocks:
            if block.id is None or int(block.id) not in conflicting_block_ids:
                continue
            node = session.get(StrategyNode, block.task_id)
            blocker_records.append(
                {
                    "block_id": int(block.id),
                    "task_id": int(block.task_id),
                    "task_title": (
                        node.title if node is not None else f"Task {block.task_id}"
                    ),
                    "start_at": _iso(local_wall_time(block.start_at)),
                    "end_at": _iso(local_wall_time(block.end_at)),
                }
            )
        blocker_titles = list(
            dict.fromkeys(
                str(record["task_title"])
                for record in blocker_records
                if str(record.get("task_title") or "").strip()
            )
        )
        exact_ranges = list(
            dict.fromkeys(
                exact_range
                for task_id in unplaced_task_ids
                if task_id in task_by_id
                for exact_range in [
                    _exact_task_range(
                        task_by_id[task_id],
                        window_start=window_start,
                        window_end=window_end,
                    )
                ]
                if exact_range is not None
            )
        )

        def joined_titles(titles: list[str]) -> str:
            if len(titles) <= 1:
                return titles[0] if titles else "The requested work"
            return ", ".join(titles[:-1]) + f" and {titles[-1]}"

        capacity_message = "The requested work does not fully fit in the selected window."
        if blocker_titles and unplaced_titles:
            conflict_start, conflict_end = (
                exact_ranges[0]
                if len(exact_ranges) == 1
                else (window_start, window_end)
            )
            capacity_message = (
                f"{joined_titles(unplaced_titles)} cannot fit from "
                f"{_human_clock(conflict_start)} to {_human_clock(conflict_end)} because "
                f"the time conflicts with {joined_titles(blocker_titles)}. Which "
                "commitment should keep this time?"
            )
        elif unplaced_titles:
            capacity_message = (
                f"{joined_titles(unplaced_titles)} does not fully fit from "
                f"{_human_clock(window_start)} to {_human_clock(window_end)}. "
                "Please provide a wider window or reduce the requested duration."
            )
        return {
            "status": "invalid",
            "errors": [capacity_message],
            "solver_status": solver_result.solver_status,
            # Preserve bounded, typed capacity evidence for the repair loop and
            # regression diagnostics.  A generic infeasibility string loses the
            # distinction between an overfull window, a hard constraint clash,
            # and a solver defect, which makes the next model attempt guess.
            "instruction": instruction,
            "window_start": _iso(window_start),
            "window_end": _iso(window_end),
            "task_count": len(solver_working_set.tasks),
            "block_count": len(solver_result.placements),
            "expected_minutes": expected_minutes,
            "placed_minutes": placed_minutes,
            "unplaced_minutes": max(0, expected_minutes - placed_minutes),
            "unplaced_task_ids": unplaced_task_ids,
            "unplaced_task_titles": unplaced_titles,
            "conflicting_blocks": blocker_records,
        }

    task_by_id = {task.task_id: task for task in execution_set.tasks}
    blocks = []
    for placement in solver_result.placements:
        task = task_by_id[int(placement.task_id)]
        block = {
                "task_title": task.title,
                "start_at": _iso(placement.start_at),
                "end_at": _iso(placement.end_at),
                "allocated_minutes": int(
                    (placement.end_at - placement.start_at).total_seconds() // 60
                ),
            }
        if int(placement.task_id) in temp_id_by_synthetic_id:
            block["target_temp_id"] = temp_id_by_synthetic_id[int(placement.task_id)]
        else:
            block["target_task_id"] = int(placement.task_id)
            source = source_block_by_task_id.get(int(placement.task_id))
            if source is not None and isinstance(source.id, int):
                block["accepted_block_id"] = int(source.id)
                block["accepted_block_revision"] = int(source.revision)
                block["previous_start_at"] = _iso(local_wall_time(source.start_at))
                block["previous_end_at"] = _iso(local_wall_time(source.end_at))
        blocks.append(block)
    block_labels_by_id: dict[int, str] = {}
    for block in accepted_blocks:
        if block.id is None:
            continue
        node = session.get(StrategyNode, block.task_id)
        block_labels_by_id[int(block.id)] = node.title if node is not None else f"Task {block.task_id}"
    placement_reasons = _placement_reasons(
        operations=[
            {
                "taskId": (
                    synthetic_id_by_temp_id[str(block["target_temp_id"])]
                    if block.get("target_temp_id")
                    else int(block["target_task_id"])
                ),
                "title": block["task_title"],
                "startAt": block["start_at"],
                "endAt": block["end_at"],
            }
            for block in blocks
        ]
        + priority_relocation_operations,
        working_set=solver_working_set,
        planner_plan=normalized_plan,
        split_session_plan=split_plan,
        accepted_blocks=accepted_blocks,
        block_labels_by_id=block_labels_by_id,
        request_constraints=normalized_request_constraints,
    )
    request_constraint_violations = validate_operations_against_request_constraints(
        [
            {
                "taskId": (
                    synthetic_id_by_temp_id[str(block["target_temp_id"])]
                    if block.get("target_temp_id")
                    else block.get("target_task_id")
                ),
                "startAt": block["start_at"],
                "endAt": block["end_at"],
            }
            for block in blocks
        ],
        normalized_request_constraints,
    )
    if request_constraint_violations:
        return {
            "status": "invalid",
            "errors": [item["message"] for item in request_constraint_violations],
            "solver_status": "request_constraint_violation",
            "request_constraint_violations": request_constraint_violations,
        }
    if (
        isinstance(priority_conflict_resolution, dict)
        and priority_conflict_resolution.get("action") == "moved_existing"
    ):
        # The generic anchor explanation describes the pre-proposal calendar
        # and becomes misleading once this proposal explicitly relocates that
        # blocker. Keep one truthful, structured explanation instead.
        placement_reasons = [
            reason
            for reason in placement_reasons
            if reason.get("kind") != "calendar_anchor"
        ]
        conflict_message = str(
            priority_conflict_resolution.get("userMessage") or ""
        ).strip()
        if conflict_message:
            placement_reasons.insert(
                0,
                {
                    "kind": "priority_conflict_resolution",
                    "message": conflict_message,
                    "taskIds": [
                        int(value)
                        for value in (
                            priority_conflict_resolution.get("incomingTaskId"),
                            *(
                                blocker.get("taskId")
                                for blocker in priority_conflict_resolution.get(
                                    "blockers", []
                                )
                                if isinstance(blocker, dict)
                            ),
                        )
                        if isinstance(value, int)
                    ],
                },
            )
    return {
        "status": "valid",
        "errors": [],
        "instruction": instruction,
        "window_start": _iso(window_start),
        "window_end": _iso(effective_plan_window_end),
        "task_count": len(tasks),
        "block_count": len(blocks),
        "blocks": blocks,
        "split_session_plan": split_plan,
        "planner_plan": normalized_plan,
        "placement_reasons": placement_reasons,
        "conflict_resolution": priority_conflict_resolution,
        "conflict_resolutions": (
            [priority_conflict_resolution]
            if isinstance(priority_conflict_resolution, dict)
            else []
        ),
        "displacement_operations": priority_relocation_operations,
        "temporary_breaks": [
            {
                "start_at": _iso(interval.start_at),
                "end_at": _iso(interval.end_at),
                "label": interval.source_label,
            }
            for interval in temporary_breaks
        ],
        "fallback_duration_temp_ids": fallback_duration_temp_ids,
        "inferred_duration_temp_ids": inferred_duration_temp_ids,
        "duration_estimates": duration_estimates,
        "request_constraints": normalized_request_constraints,
        "validation_warnings": (
            [
                (
                    "Planner estimated missing task durations from saved task history "
                    "and bounded defaults; the estimates are included in this review."
                )
            ]
            if inferred_duration_temp_ids
            else []
        ),
    }


def _remove_operations(
    *,
    working_set: PlannerWorkingSet,
    accepted_blocks_by_task_id: dict[int, CalendarBlock],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for task_ref in working_set.tasks:
        accepted_block = accepted_blocks_by_task_id.get(task_ref.task_id)
        if accepted_block is None or accepted_block.id is None:
            continue
        operations.append(
            {
                "op": "remove_block",
                "acceptedBlockId": accepted_block.id,
                "taskId": task_ref.task_id,
                "categoryId": task_ref.category_id,
                "title": task_ref.title,
                "startAt": _iso(accepted_block.start_at),
                "endAt": _iso(accepted_block.end_at),
                "isRecurring": bool(accepted_block.is_recurring),
                "recurrenceSeriesId": accepted_block.recurrence_series_id,
                "recurrenceKey": accepted_block.recurrence_key,
            }
        )
    return operations


def _conflict_payloads(
    solver_result: Any,
    *,
    working_set: PlannerWorkingSet,
    accepted_blocks: list[CalendarBlock],
    extra_occupied: list[Any] | None = None,
    block_labels_by_id: dict[int, str] | None = None,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    tasks_by_id = {task.task_id: task for task in working_set.tasks}
    blocks_by_id = {block.id: block for block in accepted_blocks if block.id is not None}
    occupied_by_id = {
        int(interval.block_id): interval
        for interval in extra_occupied or []
        if isinstance(getattr(interval, "block_id", None), int)
    }
    conflicts: list[dict[str, Any]] = []
    for conflict in solver_result.conflicts:
        task = tasks_by_id.get(conflict.task_id)
        task_title = task.title if task is not None else f"Task {conflict.task_id}"
        conflicting_blocks = []
        for block_id in conflict.conflicting_block_ids:
            block = blocks_by_id.get(block_id)
            if block is None:
                occupied = occupied_by_id.get(block_id)
                if occupied is not None:
                    conflicting_blocks.append(
                        {
                            "blockId": block_id,
                            "taskId": None,
                            "startAt": _iso(occupied.start_at),
                            "endAt": _iso(occupied.end_at),
                            "state": str(getattr(occupied, "source_type", "preference")),
                            "label": str(
                                getattr(occupied, "source_label", None)
                                or "Scheduling preference"
                            ),
                        }
                    )
                continue
            state = "protected" if block.is_protected else "pinned" if block.is_pinned else "occupied"
            conflicting_blocks.append(
                {
                    "blockId": block_id,
                    "taskId": block.task_id,
                    "startAt": _iso(block.start_at),
                    "endAt": _iso(block.end_at),
                    "state": state,
                    "label": (block_labels_by_id or {}).get(
                        block_id,
                        f"Task {block.task_id}",
                    ),
                }
            )
        conflicts.append(
            {
                "taskId": conflict.task_id,
                "taskTitle": task_title,
                "type": conflict.type,
                "detail": (
                    f"{task_title} could not fit between {_iso(window_start)} and "
                    f"{_iso(window_end)} because that time is already occupied. "
                    "Choose another time, edit the request, or reject this proposal."
                ),
                "conflictingBlockIds": conflict.conflicting_block_ids,
                "conflictingBlocks": conflicting_blocks,
                "alternatives": [
                    {
                        "startAt": _iso(alternative.start_at),
                        "endAt": _iso(alternative.end_at),
                        "reason": alternative.reason,
                    }
                    for alternative in conflict.alternatives
                ],
            }
        )
    return conflicts


def _context_cards(
    *,
    working_set: PlannerWorkingSet,
    accepted_blocks: list[CalendarBlock],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    total_minutes = sum(task.duration_minutes for task in working_set.tasks)
    return [
        {
            "id": "working-set",
            "label": "Selected",
            "value": f"{len(working_set.tasks)} tasks",
            "detail": f"{total_minutes} minutes planned",
        },
        {
            "id": "calendar-window",
            "label": "Calendar",
            "value": f"{_iso(window_start)} to {_iso(window_end)}",
            "detail": f"{len(accepted_blocks)} accepted blocks in range",
        },
    ]


def _needs_configuration_response(
    *,
    runtime_settings: dict[str, Any],
    provider_readiness: dict[str, Any],
) -> dict[str, Any]:
    return {
        "providerLabel": runtime_settings["provider_label"],
        "mode": "needs_configuration",
        "summary": "Planner provider preflight did not pass. Check the configured AI provider before generating schedule proposals.",
        "contextCards": [],
        "suggestedPrompts": [],
        "proposal": None,
        "diagnostics": {
            **_planner_agent_descriptor(),
            "controlPlane": "planner-capability",
            "providerReadiness": provider_readiness,
            "solverStatus": None,
            "runtimeMs": 0,
        },
    }


def _already_scheduled_response(
    *,
    runtime_settings: dict[str, Any],
    provider_readiness: dict[str, Any],
    working_set: PlannerWorkingSet,
    accepted_blocks: list[CalendarBlock],
    already_scheduled_task_ids: list[int],
    preference_context: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    started_at: float,
    planner_frame: dict[str, Any] | None = None,
    planner_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "providerLabel": runtime_settings["provider_label"],
        "mode": "schedule_patch",
        "summary": (
            "The selected task already has a calendar block."
            if len(already_scheduled_task_ids) == 1
            else "The selected tasks already have calendar blocks."
        ),
        "contextCards": _context_cards(
            working_set=working_set,
            accepted_blocks=accepted_blocks,
            window_start=window_start,
            window_end=window_end,
        ),
        "suggestedPrompts": [],
        "proposal": None,
        "diagnostics": {
            **_planner_agent_descriptor(),
            "controlPlane": "planner-capability",
            "providerReadiness": provider_readiness,
            "solverStatus": "already_scheduled",
            "runtimeMs": int((perf_counter() - started_at) * 1000),
            "fallbackDurationTaskIds": working_set.fallback_duration_task_ids,
            "conflictCount": 0,
            "acceptedBlockCount": len(accepted_blocks),
            "alreadyScheduledTaskIds": already_scheduled_task_ids,
            "preferenceContext": preference_context,
            "plannerFrame": planner_frame,
            "plannerModel": planner_model or {"status": "not_run"},
        },
    }


_DURATION_NUMBER_WORDS = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "half": 0.5,
    "couple": 2.0,
}


def _explicit_duration_minutes(prompt: str) -> int | None:
    numeric = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(?:[-–]\s*)?"
        r"(minutes?|mins?|min|m|hours?|hrs?|hr|h)\b",
        prompt,
        re.IGNORECASE,
    )
    amount: float | None = None
    unit = ""
    if numeric is not None:
        amount = float(numeric.group(1))
        unit = numeric.group(2).lower()
    else:
        word = re.search(
            r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|half|couple)"
            r"(?:\s+of)?\s*(?:[-–]\s*)?(minutes?|mins?|hours?|hrs?)\b",
            prompt,
            re.IGNORECASE,
        )
        if word is not None:
            amount = _DURATION_NUMBER_WORDS.get(word.group(1).lower())
            unit = word.group(2).lower()
    if amount is None:
        return None
    minutes = round(amount * 60) if unit.startswith(("hour", "hr", "h")) else round(amount)
    return minutes if 1 <= minutes <= 1440 else None


def _build_companion_task_patch(
    *,
    working_set: PlannerWorkingSet,
    task_updates: list[dict[str, Any]] | None,
    inferred_estimate_minutes: int | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    scoped_ids = {task.task_id for task in working_set.tasks}
    updates_by_task_id: dict[int, dict[str, Any]] = {}
    for item in task_updates or []:
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id")
        fields = item.get("fields")
        if (
            isinstance(task_id, int)
            and not isinstance(task_id, bool)
            and task_id in scoped_ids
            and isinstance(fields, dict)
            and fields
        ):
            updates_by_task_id.setdefault(task_id, {}).update(fields)
    if (
        isinstance(inferred_estimate_minutes, int)
        and len(working_set.tasks) == 1
        and working_set.tasks[0].task_id in set(working_set.fallback_duration_task_ids)
    ):
        updates_by_task_id.setdefault(working_set.tasks[0].task_id, {}).setdefault(
            "time_estimate_minutes",
            inferred_estimate_minutes,
        )
    if not updates_by_task_id:
        return None, None, None
    task_patch = {
        "patch_id": f"schedule-companion-task-patch-{uuid4().hex}",
        "intent_summary": "Update task details together with the proposed schedule.",
        "target_scope": "multi_entity" if len(updates_by_task_id) > 1 else "task",
        "target_node_ids": sorted(updates_by_task_id),
        "operations": [
            {"op": "update", "task_id": task_id, "fields": fields}
            for task_id, fields in sorted(updates_by_task_id.items())
        ],
        "source_proposal_kind": "schedule_patch",
    }
    validation = validate_task_patch(task_patch, working_set.task_nodes)
    simulation = simulate_task_patch(task_patch, working_set.task_nodes)
    return task_patch, validation, simulation


def build_planner_schedule_proposal(
    session: Session,
    *,
    prompt: str,
    working_set_task_ids: list[int],
    window_start: datetime,
    window_end: datetime,
    slot_minutes: int,
    fallback_duration_minutes: int,
    provider_override: str | None = None,
    model_override: str | None = None,
    provider_auth_preflight: bool = True,
    operation_mode: str = "create",
    strict_custom_semantics: bool = False,
    deadline_monotonic: float | None = None,
    recurrence_spec: dict[str, Any] | None = None,
    recurrence_not_before: datetime | None = None,
    clarify_missing_duration: bool = False,
    task_updates: list[dict[str, Any]] | None = None,
    request_constraints: list[dict[str, Any]] | None = None,
    subagent_task: PlannerSubagentTask | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build a validated Planner SchedulePatch proposal without writes."""

    started_at = perf_counter()
    if (
        provider_auth_preflight
        and operation_mode != "remove"
        and subagent_task is None
    ):
        raise ValueError(
            "Model-backed scheduling requires an owned Planner subagent task."
        )

    runtime_settings = get_provider_runtime_settings(provider_override, model_override)
    provider_readiness = (
        provider_transport_readiness(runtime_settings)
        if provider_auth_preflight
        else {
            "provider": runtime_settings["provider"],
            "checked": False,
            "passed": True,
            "source": "skipped_for_fixture",
        }
    )
    if provider_readiness.get("passed") is False:
        return _needs_configuration_response(
            runtime_settings=runtime_settings,
            provider_readiness=provider_readiness,
        )
    normalized_window_start = _ceil_to_slot_boundary(
        local_wall_time(window_start),
        slot_minutes,
    )
    normalized_window_end = local_wall_time(window_end)
    alternative_window_end = datetime.combine(
        normalized_window_end.date() + timedelta(days=1),
        time.min,
    )
    if operation_mode not in {"create", "reschedule", "remove"}:
        raise ValueError("operationMode must be create, reschedule, or remove.")
    working_set = resolve_selected_working_set(
        session,
        task_ids=working_set_task_ids,
        fallback_duration_minutes=fallback_duration_minutes,
    )
    normalized_request_constraints, constraint_errors = normalize_request_constraints(
        request_constraints,
        live_task_ids={task.task_id for task in working_set.tasks},
        window_start=normalized_window_start,
        window_end=normalized_window_end,
    )
    if constraint_errors:
        return {
            "providerLabel": runtime_settings["provider_label"],
            "mode": "clarification",
            "summary": "I need a clearer scheduling constraint before I continue.",
            "contextCards": [],
            "suggestedPrompts": [],
            "proposal": None,
            "clarification": {
                "message": " ".join(constraint_errors),
                "entity_choices": [],
                "field_choices": [],
                "scoped_follow_up_prompts": [],
            },
            "diagnostics": {
                "solverStatus": "invalid_request_constraints",
                "requestConstraintErrors": constraint_errors,
            },
        }
    explicit_duration_minutes = _explicit_duration_minutes(prompt)
    prompt_has_duration = bool(
        explicit_duration_minutes is not None
        or re.search(
            r"\bbetween\b.+?\band\b|"
            r"\bfrom\b.+?(?:\bto\b|\buntil\b|\s[-–]\s)",
            prompt,
            re.IGNORECASE,
        )
    )
    if (
        explicit_duration_minutes is not None
        and len(working_set.tasks) == 1
        and working_set.tasks[0].task_id in set(working_set.fallback_duration_task_ids)
    ):
        working_set = PlannerWorkingSet(
            tasks=[replace(working_set.tasks[0], duration_minutes=explicit_duration_minutes)],
            fallback_duration_task_ids=working_set.fallback_duration_task_ids,
            task_nodes=working_set.task_nodes,
            categories_by_id=working_set.categories_by_id,
        )
    if (
        clarify_missing_duration
        and operation_mode != "remove"
        and not isinstance(recurrence_spec, dict)
        and working_set.fallback_duration_task_ids
        and not prompt_has_duration
    ):
        missing = [
            task
            for task in working_set.tasks
            if task.task_id in set(working_set.fallback_duration_task_ids)
        ]
        titles = [task.title for task in missing]
        if len(titles) == 1:
            target = titles[0]
            message = f"How much time should I set aside for {target}?"
            field_choices = [
                {
                    "kind": "field",
                    "label": label,
                    "value": str(minutes),
                    "follow_up_prompt": (
                        f'Schedule the task named "{target}" for {minutes} minutes '
                        f"between {normalized_window_start.isoformat(timespec='minutes')} and "
                        f"{normalized_window_end.isoformat(timespec='minutes')}."
                    ),
                }
                for label, minutes in (("30 minutes", 30), ("1 hour", 60), ("2 hours", 120))
            ]
        else:
            target = ", ".join(titles)
            message = f"How much time should I set aside for each of these tasks: {target}?"
            field_choices = []
        return {
            "providerLabel": runtime_settings["provider_label"],
            "mode": "clarification",
            "summary": "I need the expected duration before I schedule this.",
            "contextCards": [],
            "suggestedPrompts": [],
            "proposal": None,
            "clarification": {
                "message": message,
                "entity_choices": [],
                "field_choices": field_choices,
                "scoped_follow_up_prompts": [],
            },
            "diagnostics": {
                "solverStatus": "needs_duration",
                "fallbackDurationTaskIds": working_set.fallback_duration_task_ids,
            },
        }
    recurrence_duration = (
        recurrence_spec.get("duration_minutes")
        if isinstance(recurrence_spec, dict)
        and isinstance(recurrence_spec.get("duration_minutes"), int)
        and not isinstance(recurrence_spec.get("duration_minutes"), bool)
        else None
    )
    companion_task_patch, companion_task_validation, companion_task_simulation = (
        _build_companion_task_patch(
            working_set=working_set,
            task_updates=task_updates,
            inferred_estimate_minutes=(
                (explicit_duration_minutes or recurrence_duration)
                if operation_mode != "remove"
                else None
            ),
        )
    )
    if (
        isinstance(companion_task_validation, dict)
        and companion_task_validation.get("status") == "invalid"
    ):
        raise ValueError(
            "The task details requested with this schedule are invalid: "
            + "; ".join(str(item) for item in companion_task_validation.get("errors", []))
        )
    if isinstance(recurrence_spec, dict) and operation_mode == "create":
        if len(working_set.tasks) != 1:
            raise ValueError("A recurring schedule proposal must target exactly one task.")
        task_ref = working_set.tasks[0]
        plan = build_recurring_schedule_plan(
            session,
            spec=recurrence_spec,
            category_id=task_ref.category_id,
            task_title=task_ref.title,
            task_emoji=task_ref.emoji,
            task_id=task_ref.task_id,
            task_priority=task_ref.priority,
            not_before=recurrence_not_before,
            preference_override_authorized=(
                preference_override_authorized_from_prompt(prompt)
            ),
            allow_priority_displacement=True,
            prompt=prompt,
        )
        if plan.get("status") != "valid":
            priority_conflicts = [
                item
                for item in plan.get("priority_conflicts", [])
                if isinstance(item, dict)
            ]
            if priority_conflicts:
                first_conflict = priority_conflicts[0]
                blockers = [
                    item
                    for item in first_conflict.get("blockers", [])
                    if isinstance(item, dict)
                ]
                blocker_details = "; ".join(
                    f"{item.get('taskTitle') or 'Calendar item'} "
                    f"({item.get('priority') or 'medium'} priority), "
                    f"{_human_clock(datetime.fromisoformat(str(item.get('startAt'))))} to "
                    f"{_human_clock(datetime.fromisoformat(str(item.get('endAt'))))}"
                    + (f", {item.get('fixedReason')}" if item.get("fixedReason") else "")
                    for item in blockers
                )
                can_move_existing = not any(
                    item.get("fixedReason") in {"protected", "pinned"}
                    for item in blockers
                )
                recurring_choices = [
                    {
                        "kind": "field",
                        "label": "Keep the existing calendar item",
                        "value": "preserve_existing",
                        "follow_up_prompt": (
                            f"Do not move the existing task. Find another recurring time for {task_ref.title}."
                        ),
                    }
                ]
                if can_move_existing:
                    recurring_choices.append(
                        {
                            "kind": "field",
                            "label": f"Use this time for {task_ref.title}",
                            "value": "move_existing",
                            "follow_up_prompt": (
                                f"Move the existing task and keep the requested recurring time for {task_ref.title}."
                            ),
                        }
                    )
                return {
                    "providerLabel": runtime_settings["provider_label"],
                    "mode": "clarification",
                    "summary": (
                        f"{task_ref.title} conflicts with "
                        + ", ".join(
                            str(item.get("taskTitle") or "another calendar item")
                            for item in blockers
                        )
                        + "."
                    ),
                    "contextCards": [],
                    "suggestedPrompts": [],
                    "proposal": None,
                    "clarification": {
                        "message": (
                            f"{blocker_details}. {task_ref.title} is "
                            f"{first_conflict.get('incomingPriority') or 'medium'} priority. "
                            "Which commitment should keep this time?"
                        ),
                        "entity_choices": [],
                        "field_choices": recurring_choices,
                        "scoped_follow_up_prompts": [],
                    },
                    "conflictResolution": first_conflict,
                    "diagnostics": {
                        "recurringScheduleStatus": "priority_conflict",
                        "priorityConflict": first_conflict,
                    },
                }
            outcome = classify_handoff_failures(
                [{"proposal_kind": "schedule_patch", "errors": list(plan.get("errors") or [])}]
            )
            clarification = clarification_for_handoff_outcome(outcome)
            return {
                "providerLabel": runtime_settings["provider_label"],
                "mode": "clarification",
                "summary": str(outcome.get("message") or "I need one more scheduling detail."),
                "contextCards": [],
                "suggestedPrompts": [],
                "proposal": None,
                "clarification": clarification,
                "handoffOutcome": outcome,
                "diagnostics": {"recurringScheduleStatus": "invalid"},
            }
        schedule_patch = schedule_patch_from_recurring_plan(
            plan,
            task_id=task_ref.task_id,
            category_id=task_ref.category_id,
            task_title=task_ref.title,
            task_emoji=task_ref.emoji,
        )
        accepted_blocks = read_calendar_state(
            session,
            window_start=datetime.fromisoformat(schedule_patch["windowStart"]),
            window_end=datetime.fromisoformat(schedule_patch["windowEnd"]),
        )
        operation_task_ids = {
            int(item["taskId"])
            for item in schedule_patch.get("operations", [])
            if isinstance(item, dict)
            and isinstance(item.get("taskId"), int)
            and not isinstance(item.get("taskId"), bool)
        }
        task_nodes = list(
            session.exec(
                select(StrategyNode).where(StrategyNode.id.in_(operation_task_ids))
            ).all()
        )
        validation = validate_schedule_patch(
            schedule_patch,
            tasks=task_nodes,
            accepted_blocks=accepted_blocks,
            window_start=datetime.fromisoformat(schedule_patch["windowStart"]),
            window_end=datetime.fromisoformat(schedule_patch["windowEnd"]),
        )
        simulation = simulate_schedule_patch(
            schedule_patch,
            tasks=task_nodes,
            accepted_blocks=accepted_blocks,
            validation=validation,
        )
        proposal = {
            "proposalId": schedule_patch["patchId"].replace("recurring-schedule", "schedule-proposal", 1),
            "proposalKind": "schedule_patch",
            "title": f"Schedule {task_ref.title} daily",
            "body": (
                str(
                    next(
                        (
                            item.get("userMessage")
                            for item in plan.get("conflict_resolutions", [])
                            if isinstance(item, dict) and item.get("userMessage")
                        ),
                        "",
                    )
                ).strip()
                or f"{plan['occurrence_count']} calendar blocks of "
                f"{plan['duration_minutes']} minutes each."
            ),
            "summary": f"Schedule {task_ref.title} daily",
            "preview": [],
            "validationWarnings": list(
                dict.fromkeys(
                    [
                        *[
                            str(item)
                            for item in validation["warnings"]
                            if isinstance(item, str) and item.strip()
                        ],
                        *[
                            str(item)
                            for item in plan.get("validation_warnings", [])
                            if isinstance(item, str) and item.strip()
                        ],
                    ]
                )
            ),
            "schedulePatch": schedule_patch,
            "schedulePatchValidation": validation,
            "schedulePatchSimulation": simulation,
            "schedulePatchStatus": validation["status"],
            "taskPatch": companion_task_patch,
            "taskPatchValidation": companion_task_validation,
            "taskPatchSimulation": companion_task_simulation,
            "taskPatchStatus": (
                companion_task_validation.get("status")
                if isinstance(companion_task_validation, dict)
                else None
            ),
            "payload": {
                "recurringSchedulePlan": plan,
                "operationMode": operation_mode,
                "conflictResolution": plan.get("conflict_resolutions") or None,
            },
        }
        return {
            "providerLabel": runtime_settings["provider_label"],
            "mode": "schedule_patch",
            "summary": (
                f"I found a time on each day and adjusted "
                f"{len(plan.get('shifted_dates') or [])} day"
                f"{'' if len(plan.get('shifted_dates') or []) == 1 else 's'} around other events."
            ),
            "contextCards": [],
            "suggestedPrompts": [],
            "proposal": proposal,
            "diagnostics": {
                "recurringScheduleStatus": validation["status"],
                "recurringOccurrenceCount": plan["occurrence_count"],
            },
        }
    if isinstance(recurrence_spec, dict) and operation_mode == "reschedule":
        # A SchedulePatch can move concrete blocks, but it cannot faithfully
        # express a persisted recurrence-rule edit or an atomic split-series
        # exception. Returning one moved block here would silently discard the
        # requested cadence. The Agent Kernel owns the native recurrence draft.
        return {
            "providerLabel": runtime_settings["provider_label"],
            "mode": "native_recurrence",
            "summary": "Preparing the recurring schedule change for approval.",
            "contextCards": [],
            "suggestedPrompts": [],
            "proposal": None,
            "handoffOutcome": {
                "status": "repairable_internally",
                "message": "Preparing the recurring schedule change for approval.",
                "reasonCode": "native_recurrence_authoring_required",
                "requiresUserInput": False,
                "retryable": True,
            },
            "diagnostics": {
                **_planner_agent_descriptor(),
                "controlPlane": "planner-capability",
                "solverStatus": "delegated_native_recurrence",
                "operationMode": operation_mode,
                "recurrenceIntentPreserved": True,
            },
        }
    accepted_blocks = read_calendar_state(
        session,
        window_start=normalized_window_start,
        window_end=alternative_window_end,
    )
    source_blocks: list[CalendarBlock] = []
    if operation_mode in {"reschedule", "remove"}:
        source_blocks = read_calendar_blocks_for_tasks(
            session,
            [task.task_id for task in working_set.tasks],
        )
        accepted_ids = {
            int(block.id) for block in accepted_blocks if block.id is not None
        }
        accepted_blocks = [
            *accepted_blocks,
            *[
                block
                for block in source_blocks
                if block.id is None or int(block.id) not in accepted_ids
            ],
        ]
    preference_context = build_planner_preference_context(
        session,
        working_set=working_set,
        window_start=normalized_window_start,
        window_end=alternative_window_end,
        prompt=prompt,
    )
    preference_occupied = constraint_intervals_from_context(
        preference_context,
        window_start=normalized_window_start,
        window_end=alternative_window_end,
    )
    exact_request_ranges: list[tuple[datetime, datetime]] = []
    task_duration_by_id = {
        task.task_id: task.duration_minutes for task in working_set.tasks
    }
    for constraint in normalized_request_constraints:
        if (
            constraint.get("kind") != "exact_start"
            or constraint.get("hard", True) is not True
        ):
            continue
        task_ids = constraint.get("taskIds")
        if not isinstance(task_ids, list) or len(task_ids) != 1:
            continue
        raw_start = constraint.get("startAt")
        duration_minutes = task_duration_by_id.get(int(task_ids[0]))
        if not isinstance(raw_start, str) or not isinstance(duration_minutes, int):
            continue
        try:
            exact_start = datetime.fromisoformat(raw_start)
        except ValueError:
            continue
        exact_request_ranges.append(
            (exact_start, exact_start + timedelta(minutes=duration_minutes))
        )
    if exact_request_ranges:
        # The current command's exact placement is more specific and newer
        # than remembered availability. Treat it as a one-off exception while
        # preserving actual calendar commitments and current-turn blockers.
        preference_occupied = [
            interval
            for interval in preference_occupied
            if not any(
                interval.start_at < exact_end and interval.end_at > exact_start
                for exact_start, exact_end in exact_request_ranges
            )
        ]
    temporary_breaks = _one_off_break_intervals_from_prompt(
        prompt,
        window_start=normalized_window_start,
        window_end=alternative_window_end,
    )
    parsed_break_constraints = [
        {
            "kind": "unavailable_interval",
            "hard": True,
            "label": interval.source_label or "Requested break",
            "sourceText": prompt,
            "taskIds": [],
            "startAt": _iso(interval.start_at),
            "endAt": _iso(interval.end_at),
        }
        for interval in temporary_breaks
        if not any(
            item.get("kind") == "unavailable_interval"
            and item.get("startAt") == _iso(interval.start_at)
            and item.get("endAt") == _iso(interval.end_at)
            for item in normalized_request_constraints
        )
    ]
    normalized_request_constraints = [
        *normalized_request_constraints,
        *parsed_break_constraints,
    ]
    request_constraint_occupied = constraint_occupied_intervals(
        normalized_request_constraints
    )
    preference_occupied = [
        *preference_occupied,
        *request_constraint_occupied,
    ]
    gap_policy = inter_task_gap_policy_from_context(preference_context)
    already_scheduled_task_ids = read_scheduled_task_ids(
        session,
        [task.task_id for task in working_set.tasks],
        window_start=normalized_window_start,
        window_end=normalized_window_end,
    )
    planner_frame: dict[str, Any] | None = None
    planner_model: dict[str, Any] = {"status": "skipped_for_fixture"}
    accepted_blocks_by_task_id: dict[int, CalendarBlock] = {}
    source_mapping_blocks = (
        [
            block
            for block in source_blocks
            if block.start_at < normalized_window_end
            and block.end_at > normalized_window_start
        ]
        if operation_mode == "remove"
        else source_blocks
        if operation_mode == "reschedule"
        else accepted_blocks
    )
    for block in source_mapping_blocks:
        accepted_blocks_by_task_id.setdefault(block.task_id, block)
    accepted_block_labels_by_id: dict[int, str] = {}
    for block in accepted_blocks:
        if block.id is None:
            continue
        task_node = session.get(StrategyNode, block.task_id)
        accepted_block_labels_by_id[int(block.id)] = (
            task_node.title if task_node is not None else f"Task {block.task_id}"
        )
    anchored_breaks = _anchored_calendar_intervals(session, accepted_blocks)
    calendar_occupancy = _calendar_occupancy_intervals(accepted_blocks)
    schedulable_working_set = (
        filter_working_set(
            working_set,
            excluded_task_ids=set(already_scheduled_task_ids),
        )
        if operation_mode == "create"
        else working_set
    )
    if operation_mode == "create" and not schedulable_working_set.tasks:
        return _already_scheduled_response(
            runtime_settings=runtime_settings,
            provider_readiness=provider_readiness,
            working_set=working_set,
            accepted_blocks=accepted_blocks,
            already_scheduled_task_ids=already_scheduled_task_ids,
            preference_context=preference_context,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
            started_at=started_at,
            planner_frame=planner_frame,
            planner_model=planner_model,
        )

    selected_task_ids = {task.task_id for task in schedulable_working_set.tasks}
    calendar_task_context: dict[int, dict[str, Any]] = {}
    surrounding_tasks: list[StrategyNode] = []
    surrounding_task_windows: list[tuple[StrategyNode, datetime, datetime]] = []
    seen_surrounding_ids: set[int] = set()
    for block in accepted_blocks:
        task_node = session.get(StrategyNode, block.task_id)
        if task_node is None or task_node.id is None:
            continue
        calendar_task_context[int(task_node.id)] = {
            "title": task_node.title,
            "categoryId": task_node.category_id,
            "parentId": task_node.parent_id,
            "priority": task_node.priority,
            "deadlineAt": task_node.deadline_at,
            "durationMinutes": task_node.time_estimate_minutes,
        }
        if (
            int(task_node.id) not in selected_task_ids
            and int(task_node.id) not in seen_surrounding_ids
            and block.start_at < normalized_window_end
            and block.end_at > normalized_window_start
        ):
            surrounding_tasks.append(task_node)
            surrounding_task_windows.append(
                (task_node, block.start_at, block.end_at)
            )
            seen_surrounding_ids.add(int(task_node.id))

    heuristic_working_set, default_scheduling_policy = apply_default_scheduling_heuristics(
        schedulable_working_set,
        prompt=prompt,
        window_start=normalized_window_start,
        window_end=normalized_window_end,
        preference_context=preference_context,
        surrounding_tasks=surrounding_tasks,
        surrounding_task_windows=surrounding_task_windows,
    )
    heavy_task_ids = set(default_scheduling_policy["heavyTaskIds"])
    solver_accepted_blocks = (
        [
            block
            for block in accepted_blocks
            if block.task_id not in {task.task_id for task in schedulable_working_set.tasks}
        ]
        if operation_mode == "reschedule"
        else accepted_blocks
    )
    planner_plan: dict[str, Any] = {
        "status": "not_applied",
        "orderedTaskIds": [task.task_id for task in heuristic_working_set.tasks],
        "skippedTaskIds": [],
        "durationOverrides": [],
        "gapDirectives": [],
        "gapPolicy": gap_policy,
        "defaultSchedulingPolicy": default_scheduling_policy,
        "warnings": [],
    }
    solver_working_set, normalized_plan = apply_planner_task_directives(
        heuristic_working_set,
        [],
        gap_policy=gap_policy,
        default_heavy_task_ids=heavy_task_ids,
        preserve_submitted_order=True,
    )
    planner_plan = {
        **normalized_plan,
        "defaultSchedulingPolicy": default_scheduling_policy,
    }
    if provider_auth_preflight and operation_mode != "remove":
        planner_timeout_seconds = 90
        if deadline_monotonic is not None:
            planner_timeout_seconds = max(
                1,
                min(90, int(deadline_monotonic - perf_counter() - 5)),
            )
        if subagent_task is not None:
            planner_timeout_seconds = max(
                1,
                min(
                    planner_timeout_seconds,
                    int((subagent_task.deadline_at - datetime.now()).total_seconds()),
                ),
            )
        planner_frame, planner_model = _planner_model_frame(
            session=session,
            prompt=prompt,
            runtime_settings=runtime_settings,
            working_set=heuristic_working_set,
            accepted_blocks=accepted_blocks,
            calendar_task_context=calendar_task_context,
            preference_context=preference_context,
            default_scheduling_policy=default_scheduling_policy,
            solver_accepted_blocks=solver_accepted_blocks,
            preference_occupied=preference_occupied,
            gap_policy=gap_policy,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
            slot_minutes=slot_minutes,
            alternative_window_end=alternative_window_end,
            operation_mode=operation_mode,
            timeout_seconds=planner_timeout_seconds,
            subagent_task=subagent_task,
            cancellation_check=cancellation_check,
        )
        if (
            planner_frame is not None
            and planner_frame.get("decision") == "needs_strategic_decision"
        ):
            return {
                "providerLabel": runtime_settings["provider_label"],
                "mode": "strategic_decision",
                "summary": "Planner needs one bounded strategic decision.",
                "contextCards": [],
                "suggestedPrompts": [],
                "proposal": None,
                "strategicDecision": planner_frame.get("strategic_decision"),
                "diagnostics": {
                    **_planner_agent_descriptor(),
                    "controlPlane": "planner-capability",
                    "plannerFrame": planner_frame,
                    "plannerModel": planner_model,
                },
            }
        if planner_frame is not None and planner_frame.get("decision") == "plan":
            solver_working_set, normalized_plan = apply_planner_task_directives(
                heuristic_working_set,
                _task_directives_without_misclassified_clock_break(
                    planner_frame.get("task_directives"),
                    prompt=prompt,
                    temporary_breaks=request_constraint_occupied,
                ),
                gap_policy=gap_policy,
                default_heavy_task_ids=heavy_task_ids,
            )
            planner_plan = {
                **normalized_plan,
                "defaultSchedulingPolicy": default_scheduling_policy,
            }
        elif planner_frame is not None:
            planner_plan = {
                **planner_plan,
                "status": "model_could_not_plan_fallback",
                "warnings": [
                    "Planner could not produce a bounded semantic plan; preserved the submitted working set."
                ],
            }
        if strict_custom_semantics and (
            planner_frame is None or planner_frame.get("decision") != "plan"
        ):
            return {
                "providerLabel": runtime_settings["provider_label"],
                "mode": "clarification",
                "summary": (
                    "Planner could not safely represent every custom scheduling constraint. "
                    "Please clarify the requested window or constraint; no generic schedule was generated."
                ),
                "contextCards": _context_cards(
                    working_set=working_set,
                    accepted_blocks=accepted_blocks,
                    window_start=normalized_window_start,
                    window_end=normalized_window_end,
                ),
                "suggestedPrompts": [],
                "proposal": None,
                "diagnostics": {
                    "controlPlane": "planner_capability_harness",
                    "plannerFrame": planner_frame,
                    "plannerModel": planner_model,
                    "strictCustomSemantics": True,
                },
            }

    solver_working_set = apply_request_constraints(
        solver_working_set,
        normalized_request_constraints,
        slot_minutes=slot_minutes,
    )
    planner_plan["requestConstraints"] = normalized_request_constraints
    contiguous_task_ids = {
        int(task_id)
        for constraint in normalized_request_constraints
        if constraint.get("kind") == "contiguous" and constraint.get("hard", True)
        for task_id in constraint.get("taskIds", [])
        if isinstance(task_id, int)
    }
    solver_execution_set = solver_working_set
    split_session_plan: list[dict[str, Any]] = []
    priority_conflict_resolution: dict[str, Any] | None = None
    priority_relocation_operations: list[dict[str, Any]] = []
    priority_additional_task_nodes: list[StrategyNode] = []
    effective_patch_window_end = normalized_window_end
    if operation_mode == "create":
        solver_execution_set, split_session_plan = _split_session_working_set(
            solver_working_set,
            prompt=prompt,
            temporary_breaks=request_constraint_occupied,
            anchored_breaks=anchored_breaks,
            calendar_occupancy=calendar_occupancy,
            window_start=normalized_window_start,
            contiguous_task_ids=contiguous_task_ids,
        )

    if operation_mode == "remove":
        solver_result = None
        operations = _remove_operations(
            working_set=schedulable_working_set,
            accepted_blocks_by_task_id=accepted_blocks_by_task_id,
        )
    else:
        solver_result = run_deterministic_schedule_tool(
            working_set=solver_execution_set,
            accepted_blocks=solver_accepted_blocks,
            extra_occupied=preference_occupied,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
            slot_minutes=slot_minutes,
            alternative_window_end=alternative_window_end,
        )
        if operation_mode == "create" and not CONTIGUOUS_REQUIRED_RE.search(prompt):
            task_by_id = {task.task_id: task for task in solver_working_set.tasks}
            forced_split_ids = {
                conflict.task_id
                for conflict in solver_result.conflicts
                if (
                    conflict.task_id in task_by_id
                    and task_by_id[conflict.task_id].duration_minutes > 120
                )
            }
            if forced_split_ids:
                retry_execution_set, retry_split_plan = _split_session_working_set(
                    solver_working_set,
                    prompt=prompt,
                    temporary_breaks=request_constraint_occupied,
                    anchored_breaks=anchored_breaks,
                    calendar_occupancy=calendar_occupancy,
                    window_start=normalized_window_start,
                    forced_session_minutes_by_task_id={
                        task_id: 60 for task_id in forced_split_ids
                    },
                    contiguous_task_ids=contiguous_task_ids,
                )
                retry_result = run_deterministic_schedule_tool(
                    working_set=retry_execution_set,
                    accepted_blocks=solver_accepted_blocks,
                    extra_occupied=preference_occupied,
                    window_start=normalized_window_start,
                    window_end=normalized_window_end,
                    slot_minutes=slot_minutes,
                    alternative_window_end=alternative_window_end,
                )
                expected_retry_minutes = sum(
                    task.duration_minutes for task in solver_working_set.tasks
                )
                placed_retry_minutes = sum(
                    int((placement.end_at - placement.start_at).total_seconds() // 60)
                    for placement in retry_result.placements
                )
                if placed_retry_minutes == expected_retry_minutes:
                    solver_execution_set = retry_execution_set
                    split_session_plan = retry_split_plan
                    solver_result = retry_result
        if not split_session_plan:
            priority_result = _resolve_single_task_priority_conflict(
                session,
                prompt=prompt,
                working_set=solver_execution_set,
                solver_result=solver_result,
                accepted_blocks=accepted_blocks,
                extra_occupied=preference_occupied,
                window_start=normalized_window_start,
                window_end=normalized_window_end,
                alternative_window_end=alternative_window_end,
                slot_minutes=slot_minutes,
                gap_policy=gap_policy,
                hard_exact_constraint=any(
                    constraint.get("hard", True)
                    and constraint.get("kind") == "exact_start"
                    for constraint in normalized_request_constraints
                ),
            )
            clarification_response = priority_result.get("clarification")
            if isinstance(clarification_response, dict):
                return {
                    "providerLabel": runtime_settings["provider_label"],
                    "mode": "clarification",
                    "summary": clarification_response["summary"],
                    "contextCards": _context_cards(
                        working_set=working_set,
                        accepted_blocks=accepted_blocks,
                        window_start=normalized_window_start,
                        window_end=normalized_window_end,
                    ),
                    "suggestedPrompts": [],
                    "proposal": None,
                    "clarification": clarification_response["clarification"],
                    "conflictResolution": clarification_response["conflictResolution"],
                    "diagnostics": {
                        **_planner_agent_descriptor(),
                        "controlPlane": "planner-capability",
                        "solverStatus": solver_result.solver_status,
                        "priorityConflictResolution": clarification_response[
                            "conflictResolution"
                        ],
                    },
                }
            solver_result = priority_result["solver_result"]
            priority_relocation_operations = list(priority_result.get("operations") or [])
            resolution_payload = priority_result.get("resolution")
            priority_conflict_resolution = (
                resolution_payload if isinstance(resolution_payload, dict) else None
            )
            priority_additional_task_nodes = list(
                priority_result.get("additional_task_nodes") or []
            )
            candidate_window_end = priority_result.get("window_end")
            if isinstance(candidate_window_end, datetime):
                effective_patch_window_end = max(
                    effective_patch_window_end,
                    candidate_window_end,
                )
        if split_session_plan and operation_mode == "create":
            operations = _operations_from_split_placements(
                working_set=solver_execution_set,
                placements=solver_result.placements,
            )
        else:
            placements_by_task_id = {
                placement.task_id: placement for placement in solver_result.placements
            }
            operations = _operations_from_solver(
                working_set=solver_working_set,
                placements_by_task_id=placements_by_task_id,
                operation_mode=operation_mode,
                accepted_blocks_by_task_id=accepted_blocks_by_task_id,
            )
        operations.extend(priority_relocation_operations)
    task_nodes_by_id = {
        int(node.id): node
        for node in [
            *schedulable_working_set.task_nodes,
            *priority_additional_task_nodes,
        ]
        if node.id is not None
    }
    task_nodes: list[StrategyNode] = list(task_nodes_by_id.values())
    request_constraint_violations = validate_operations_against_request_constraints(
        operations,
        normalized_request_constraints,
    )
    schedule_patch = build_schedule_patch(
        intent_summary=(
            str((planner_frame or {}).get("intent_summary") or "").strip()
            or prompt.strip()
            or "Schedule the selected tasks in the visible calendar window."
        ),
        working_set_task_ids=[task.task_id for task in working_set.tasks],
        window_start=normalized_window_start,
        window_end=effective_patch_window_end,
        operations=operations,
    )
    validation = validate_schedule_patch(
        schedule_patch,
        tasks=task_nodes,
        accepted_blocks=accepted_blocks,
        window_start=normalized_window_start,
        window_end=effective_patch_window_end,
    )
    if request_constraint_violations:
        validation["errors"].extend(
            item["message"] for item in request_constraint_violations
        )
        validation["status"] = "invalid"
    conflicts = (
        _conflict_payloads(
            solver_result,
            working_set=solver_working_set,
            accepted_blocks=accepted_blocks,
            extra_occupied=preference_occupied,
            block_labels_by_id=accepted_block_labels_by_id,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
        )
        if solver_result is not None
        else []
    )
    operated_task_ids = {
        int(operation["taskId"])
        for operation in operations
        if isinstance(operation.get("taskId"), int)
    }
    unplaced_task_ids = [
        task.task_id
        for task in solver_working_set.tasks
        if task.task_id not in operated_task_ids
    ]
    if unplaced_task_ids and not validation["errors"]:
        validation["warnings"].append(
            "Some selected tasks could not be represented in this proposal: "
            + ", ".join(str(task_id) for task_id in unplaced_task_ids)
            + "."
        )
        validation["status"] = "warning"
    expected_split_sessions = sum(
        int(item.get("sessionCount") or 0) for item in split_session_plan
    )
    if (
        expected_split_sessions
        and solver_result is not None
        and len(solver_result.placements) < expected_split_sessions
        and not validation["errors"]
    ):
        validation["warnings"].append(
            "Some requested work sessions could not fit in the selected dates."
        )
        validation["status"] = "warning"
    for planner_warning in planner_plan.get("warnings") or []:
        if planner_warning not in validation["warnings"]:
            validation["warnings"].append(str(planner_warning))
    skipped_task_ids = [
        int(task_id)
        for task_id in planner_plan.get("skippedTaskIds") or []
        if isinstance(task_id, int) and not isinstance(task_id, bool)
    ]
    if skipped_task_ids and not validation["errors"]:
        validation["warnings"].append(
            "Planner intentionally left selected tasks out of this proposal: "
            + ", ".join(str(task_id) for task_id in skipped_task_ids)
            + "."
        )
    if validation["warnings"] and not validation["errors"]:
        validation["status"] = "warning"
    simulation = simulate_schedule_patch(
        schedule_patch,
        tasks=task_nodes,
        accepted_blocks=accepted_blocks,
        validation=validation,
        conflicts=conflicts,
    )
    placement_reasons = _placement_reasons(
        operations=operations,
        working_set=solver_working_set,
        planner_plan=planner_plan,
        split_session_plan=split_session_plan,
        accepted_blocks=accepted_blocks,
        block_labels_by_id=accepted_block_labels_by_id,
        request_constraints=normalized_request_constraints,
    )
    status = str(validation["status"])
    if request_constraint_violations:
        labels = list(
            dict.fromkeys(
                str(item.get("label") or "a requested constraint")
                for item in request_constraint_violations
            )
        )
        return {
            "providerLabel": runtime_settings["provider_label"],
            "mode": "clarification",
            "summary": "I could not find a schedule that satisfies every fixed requirement.",
            "contextCards": _context_cards(
                working_set=working_set,
                accepted_blocks=accepted_blocks,
                window_start=normalized_window_start,
                window_end=normalized_window_end,
            ),
            "suggestedPrompts": [],
            "proposal": None,
            "clarification": {
                "message": (
                    "The schedule still conflicts with "
                    + ", ".join(labels)
                    + ". Please change that restriction or give me a wider time window."
                ),
                "entity_choices": [],
                "field_choices": [],
                "scoped_follow_up_prompts": [],
            },
            "diagnostics": {
                **_planner_agent_descriptor(),
                "controlPlane": "planner-capability",
                "solverStatus": solver_result.solver_status if solver_result is not None else "proposal_only",
                "requestConstraints": normalized_request_constraints,
                "requestConstraintViolations": request_constraint_violations,
            },
        }
    proposal = {
        "proposalId": schedule_patch["patchId"].replace("schedule-patch", "schedule-proposal", 1),
        "proposalKind": "schedule_patch",
        "title": "Schedule proposal",
        "body": (
            str((priority_conflict_resolution or {}).get("userMessage") or "").strip()
            or "Review these proposed calendar placements."
        ),
        "summary": (
            f"Prepared {len(operations)} proposed calendar operation"
            f"{'' if len(operations) == 1 else 's'}."
        ),
        "preview": [
            (
                f"{operation['title']}, {operation['startAt']} - {operation['endAt']}"
                if operation.get("startAt") and operation.get("endAt")
                else f"{operation.get('title') or 'Task'}: {operation['op'].replace('_', ' ')}"
            )
            for operation in operations
        ],
        "validationWarnings": validation["warnings"],
        "schedulePatch": schedule_patch,
        "schedulePatchValidation": validation,
        "schedulePatchSimulation": simulation,
        "schedulePatchStatus": status,
        "placementReasons": placement_reasons,
        "taskPatch": companion_task_patch,
        "taskPatchValidation": companion_task_validation,
        "taskPatchSimulation": companion_task_simulation,
        "taskPatchStatus": (
            companion_task_validation.get("status")
            if isinstance(companion_task_validation, dict)
            else None
        ),
        "payload": {
            "workingSet": working_set_context_payload(working_set),
            "alreadyScheduledTaskIds": already_scheduled_task_ids,
            "conflicts": conflicts,
            "preferenceContext": preference_context,
            "temporaryBreaks": [
                {
                    "startAt": _iso(interval.start_at),
                    "endAt": _iso(interval.end_at),
                    "label": interval.source_label,
                }
                for interval in temporary_breaks
            ],
            "solverStatus": solver_result.solver_status if solver_result is not None else "proposal_only",
            "unplacedTaskIds": unplaced_task_ids,
            "operationMode": operation_mode,
            "plannerPlan": planner_plan,
            "splitSessionPlan": split_session_plan,
            "placementReasons": placement_reasons,
            "conflictResolution": priority_conflict_resolution,
            "requestConstraints": normalized_request_constraints,
            "requestConstraintViolations": request_constraint_violations,
        },
    }
    return {
        "providerLabel": runtime_settings["provider_label"],
        "mode": "schedule_patch",
        "summary": (
            str((priority_conflict_resolution or {}).get("userMessage") or "").strip()
            or
            str((planner_frame or {}).get("planning_rationale") or "").strip()
            or (
                "The Planner reasoning layer could not complete, so this proposal uses "
                "the validated deterministic scheduler and the submitted constraints only."
                if planner_model.get("status") == "failed"
                else ""
            )
            or (
                f"Prepared a reviewable schedule proposal for {len(working_set.tasks)} selected task"
                f"{'' if len(working_set.tasks) == 1 else 's'}."
            )
        ),
        "contextCards": _context_cards(
            working_set=working_set,
            accepted_blocks=accepted_blocks,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
        ),
        "suggestedPrompts": [],
        "proposal": proposal,
        "diagnostics": {
            **_planner_agent_descriptor(),
            "controlPlane": "planner-capability",
            "providerReadiness": provider_readiness,
            "solverStatus": solver_result.solver_status if solver_result is not None else "proposal_only",
            "runtimeMs": int((perf_counter() - started_at) * 1000),
            "fallbackDurationTaskIds": working_set.fallback_duration_task_ids,
            "conflictCount": len(conflicts),
            "acceptedBlockCount": len(accepted_blocks),
            "alreadyScheduledTaskIds": already_scheduled_task_ids,
            "preferenceConstraintCount": len(preference_occupied),
            "preferenceContext": preference_context,
            "temporaryBreaks": [
                {
                    "startAt": _iso(interval.start_at),
                    "endAt": _iso(interval.end_at),
                    "label": interval.source_label,
                }
                for interval in temporary_breaks
            ],
            "requestConstraints": normalized_request_constraints,
            "requestConstraintViolations": request_constraint_violations,
            "operationMode": operation_mode,
            "unplacedTaskIds": unplaced_task_ids,
            "plannerModel": planner_model,
            "plannerFrame": planner_frame,
            "plannerPlan": planner_plan,
            "splitSessionPlan": split_session_plan,
            "priorityConflictResolution": priority_conflict_resolution,
        },
    }
