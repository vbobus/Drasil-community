"""
Drasil AI Router
----------------
Provides controlled Strategist AI planning proposals backed by existing task
and category data, with persisted chat sessions.
"""

import json
import os
import time
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import date, datetime
from typing import Any, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, alias_generators, field_validator, model_validator
from sqlalchemy import update
from sqlmodel import Session

from app.db.database import get_active_engine, get_session
from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode
from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn
from app.services.agent_kernel.provider_transport import get_provider_runtime_settings
from app.services.agent_kernel.contracts import ReviewDocument
from app.services.principal_auth import require_authenticated_principal
from app.services.account_auth import active_account, use_account
from app.services.google_calendar import delete_google_event_by_id, try_sync_block_to_google
from app.services.strategist_graph import bind_proposal_turn_ids, run_strategist_graph_turn
from app.services.strategist_runs import (
    create_strategist_run,
    get_strategist_run,
    list_incomplete_strategist_runs,
    mark_strategist_run_completed,
    mark_strategist_run_failed,
    mark_strategist_run_started,
    parse_run_request,
    public_strategist_run_activities,
    serialize_strategist_run,
)
from app.services.strategist_sessions import (
    complete_strategist_turn,
    create_pending_strategist_turn,
    create_strategist_session,
    get_session_turns,
    persist_session_entity_memory,
    get_strategist_session,
    list_strategist_sessions,
    record_strategist_turn,
)

router = APIRouter(prefix="/api/v1/ai", tags=["AI"])
DEFAULT_STRATEGIST_CONTROL_PLANE = "agent-kernel"


def _bounded_float_env(name: str, *, default: float, minimum: float, maximum: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def strategist_foreground_wait_seconds() -> float:
    return _bounded_float_env(
        "STRATEGIST_RUN_FOREGROUND_WAIT_SECONDS",
        default=8.0,
        minimum=0.0,
        maximum=60.0,
    )


def strategist_run_stream_max_seconds() -> float:
    return _bounded_float_env(
        "STRATEGIST_RUN_STREAM_MAX_SECONDS",
        default=7200.0,
        minimum=60.0,
        maximum=172800.0,
    )


STRATEGIST_RUN_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("STRATEGIST_RUN_WORKERS", "2")))
)
IN_FLIGHT_STRATEGIST_RUNS: dict[str, Future[None]] = {}
RUN_STREAM_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class StrategistRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    prompt: str = ""
    session_id: Optional[str] = None
    active_category_id: Optional[int] = None
    focus_node_id: Optional[int] = None
    staged_node_ids: List[int] = Field(default_factory=list)
    conversation_history: List[dict[str, str]] = Field(default_factory=list)
    ui_event: Optional["StrategistUiEvent"] = None
    control_plane: Optional[str] = None
    today_override: Optional[str] = None
    requested_capability_ids: List[str] = Field(default_factory=list)
    calendar_view_start: Optional[datetime] = None
    calendar_view_end: Optional[datetime] = None
    local_date_time: Optional[datetime] = None
    time_zone: Optional[str] = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("today_override")
    @classmethod
    def validate_today_override(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = str(value).strip()
        try:
            return date.fromisoformat(normalized).isoformat()
        except ValueError as error:
            raise ValueError("todayOverride must be an ISO date like 2026-05-07") from error

    @model_validator(mode="after")
    def validate_request(self) -> "StrategistRequest":
        if not self.prompt and self.ui_event is None:
            raise ValueError("prompt is required")
        if (self.calendar_view_start is None) != (self.calendar_view_end is None):
            raise ValueError("calendarViewStart and calendarViewEnd must be provided together")
        for field_name, value in (
            ("calendarViewStart", self.calendar_view_start),
            ("calendarViewEnd", self.calendar_view_end),
            ("localDateTime", self.local_date_time),
        ):
            if value is not None and value.tzinfo is not None:
                raise ValueError(f"{field_name} must be a naive local-wall-time value")
        if (
            self.calendar_view_start is not None
            and self.calendar_view_end is not None
            and self.calendar_view_end <= self.calendar_view_start
        ):
            raise ValueError("calendarViewEnd must be after calendarViewStart")
        if self.time_zone is not None:
            self.time_zone = self.time_zone.strip() or None
        if (self.local_date_time is None) != (self.time_zone is None):
            raise ValueError("localDateTime and timeZone must be provided together")
        if self.time_zone is not None:
            try:
                ZoneInfo(self.time_zone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("timeZone must be a valid IANA time zone") from error
        return self


class StrategistUiEvent(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    kind: str
    source_turn_id: Optional[str] = None
    target_node_id: Optional[int] = None
    target_node_ids: List[int] = Field(default_factory=list)
    proposal_id: Optional[str] = None
    proposal_kind: Optional[str] = None
    change_set_id: Optional[str] = None
    goal_id: Optional[str] = None
    goal_revision: Optional[int] = Field(default=None, ge=1)
    decision_id: Optional[str] = None
    option_id: Optional[str] = None
    event_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    action: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_typed_decision_identity(self) -> "StrategistUiEvent":
        typed_values = (
            self.goal_id,
            self.goal_revision,
            self.decision_id,
            self.option_id,
            self.event_id,
            self.idempotency_key,
        )
        if any(value is not None for value in typed_values):
            if self.kind != "clarification_choice" or any(
                value is None for value in typed_values
            ):
                raise ValueError(
                    "typed clarification choices require goalId, goalRevision, "
                    "decisionId, optionId, and eventId"
                )
            if any(
                not str(value).strip()
                for value in (
                    self.goal_id,
                    self.decision_id,
                    self.option_id,
                    self.event_id,
                    self.idempotency_key,
                )
            ):
                raise ValueError("typed clarification identity values must be nonblank")
            if self.event_id != self.idempotency_key:
                raise ValueError("typed clarification eventId and idempotencyKey must match")
        return self


StrategistRequest.model_rebuild()


class StrategistContextCard(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    label: str
    value: str
    detail: Optional[str] = None


class StrategistStandardProposal(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    proposal_id: Optional[str] = None
    change_set_id: Optional[str] = None
    change_set_status: Optional[str] = None
    proposal_kind: str
    operation: str
    legacy_response_key: str
    title: str
    body: str
    summary: Optional[str] = None
    rationale: Optional[str] = None
    target_node_id: Optional[int] = None
    target_node_ids: List[int] = Field(default_factory=list)
    target_node_title: Optional[str] = None
    preview: List[str] = Field(default_factory=list)
    review_sections: List[dict[str, Any]] = Field(default_factory=list)
    validation_warnings: List[str] = Field(default_factory=list)
    task_patch: Optional[dict[str, Any]] = None
    task_patch_validation: Optional[dict[str, Any]] = None
    task_patch_simulation: Optional[dict[str, Any]] = None
    task_patch_status: Optional[str] = None
    preference_patch: Optional[dict[str, Any]] = None
    preference_patch_validation: Optional[dict[str, Any]] = None
    preference_patch_simulation: Optional[dict[str, Any]] = None
    preference_patch_status: Optional[str] = None
    badge_suggestions: Optional[List[dict[str, Any]]] = None
    schedule_patch: Optional[dict[str, Any]] = None
    schedule_patch_validation: Optional[dict[str, Any]] = None
    schedule_patch_simulation: Optional[dict[str, Any]] = None
    schedule_patch_status: Optional[str] = None
    capability_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class StrategistClarificationChoice(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    kind: str
    label: str
    value: str
    description: Optional[str] = None
    target_node_id: Optional[int] = None
    follow_up_prompt: str = ""
    goal_id: Optional[str] = None
    goal_revision: Optional[int] = None
    decision_id: Optional[str] = None
    option_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_typed_identity(self) -> "StrategistClarificationChoice":
        typed_values = (self.goal_id, self.goal_revision, self.decision_id, self.option_id)
        if any(value is not None for value in typed_values):
            if any(value is None for value in typed_values):
                raise ValueError("typed clarification choices require complete identity")
            if any(
                not str(value).strip()
                for value in (self.goal_id, self.decision_id, self.option_id)
            ):
                raise ValueError("typed clarification identity values must be nonblank")
        elif not self.follow_up_prompt.strip():
            raise ValueError("legacy clarification choices require followUpPrompt")
        return self


class StrategistClarificationPayload(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    message: str
    entity_choices: List[StrategistClarificationChoice] = Field(default_factory=list)
    field_choices: List[StrategistClarificationChoice] = Field(default_factory=list)
    scoped_follow_up_prompts: List[str] = Field(default_factory=list)
    conflicts: List[dict[str, Any]] = Field(default_factory=list)


class StrategistTraceStep(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    kind: str
    title: str
    detail: str
    duration_ms: Optional[float] = None
    # Stable machine identity for release assertions. The UI continues to use
    # the safe public title/detail rather than exposing raw arguments/results.
    tool_name: Optional[str] = None


class StrategistTraceMetrics(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    model_steps: int = 0
    tool_calls: int = 0
    semantic_authoring_passes: int = 0
    repeated_unchanged_goal_updates: int = 0
    max_planner_callbacks_for_one_decision: int = 0
    production_authority_count: int = 1


class StrategistTrace(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    summary: str
    steps: List[StrategistTraceStep] = Field(default_factory=list)
    metrics: StrategistTraceMetrics = Field(default_factory=StrategistTraceMetrics)


class StrategistTaskReference(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    task_id: int
    title: str
    emoji: Optional[str] = None
    node_type: str = "task"
    category_id: int
    category_name: Optional[str] = None
    category_accent_color: Optional[str] = None


class StrategistResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    provider_label: str
    mode: str
    summary: str
    context_cards: List[StrategistContextCard] = Field(default_factory=list)
    suggested_prompts: List[str] = Field(default_factory=list)
    task_references: List[StrategistTaskReference] = Field(default_factory=list)
    review_document: Optional[ReviewDocument] = None
    proposal: Optional[StrategistStandardProposal] = None
    clarification: Optional[StrategistClarificationPayload] = None
    trace: Optional[StrategistTrace] = None
    action_result: Optional[dict[str, Any]] = None
    handoff_outcome: Optional[dict[str, Any]] = None


class StrategistSessionSummary(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    title: str
    provider_label: str
    model_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class StrategistTurnRecord(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    prompt: str
    response: Optional[StrategistResponse] = None
    error: Optional[str] = None
    created_at: datetime


class StrategistSessionDetail(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    session: StrategistSessionSummary
    turns: List[StrategistTurnRecord] = Field(default_factory=list)


class StrategistRunSummary(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    session_id: str
    turn_id: str
    status: str
    control_plane: Optional[str] = None
    goal_id: Optional[str] = None
    goal_revision: Optional[int] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    prompt: str
    error_text: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: datetime
    activities: List[dict[str, Any]] = Field(default_factory=list)


class StrategistRunResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    session: StrategistSessionSummary
    turn: StrategistTurnRecord
    run: Optional[StrategistRunSummary] = None


class StrategistSessionContextRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    node_ids: List[int] = Field(default_factory=list)
    source: str = "ui_apply"
    mention_text: Optional[str] = None


def serialize_session_summary(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "title": record.title,
        "provider_label": record.provider_label,
        "model_name": record.model_name,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def serialize_turn_record(record: Any) -> dict[str, Any]:
    parsed_response = None
    if record.response_json:
        try:
            maybe_response = json.loads(record.response_json)
            if isinstance(maybe_response, dict):
                parsed_response = maybe_response
        except json.JSONDecodeError:
            parsed_response = None
    return {
        "id": record.id,
        "prompt": record.prompt,
        "response": parsed_response,
        "error": record.error_text,
        "created_at": record.created_at,
    }


def build_conversation_history_from_turns(turns: list[Any]) -> list[dict[str, str]]:
    conversation_history: list[dict[str, str]] = []
    for turn in turns[-12:]:
        conversation_history.append({"role": "user", "content": turn.prompt})
        if turn.response_json:
            try:
                maybe_response = json.loads(turn.response_json)
            except json.JSONDecodeError:
                continue
            if isinstance(maybe_response, dict):
                summary = maybe_response.get("summary")
                content_parts: list[str] = []
                if isinstance(summary, str) and summary.strip():
                    content_parts.append(summary.strip())
                clarification = maybe_response.get("clarification")
                if isinstance(clarification, dict):
                    clarification_message = clarification.get("message")
                    if isinstance(clarification_message, str) and clarification_message.strip():
                        content_parts.append(f"Clarification: {clarification_message.strip()}")
                if content_parts:
                    conversation_history.append({"role": "assistant", "content": "\n".join(content_parts)})
    return conversation_history


def build_strategist_request_payload(body: StrategistRequest) -> dict[str, Any]:
    return body.model_dump(mode="json")


def execute_strategist_run(run_id: str, account=None) -> None:
    with use_account(account):
        _execute_strategist_run_in_account(run_id)


def _execute_strategist_run_in_account(run_id: str) -> None:
    with Session(get_active_engine()) as worker_session:
        run = get_strategist_run(worker_session, run_id)
        if run is None:
            return
        if run.status not in {"queued", "running"}:
            return
        request_payload = parse_run_request(run)
        ui_event = request_payload.get("ui_event")
        strategist_session = get_strategist_session(worker_session, run.session_id)
        turn_record = worker_session.get(StrategistChatTurn, run.turn_id)
        if strategist_session is None or turn_record is None:
            mark_strategist_run_failed(
                worker_session,
                run,
                error_text="Strategist run is missing its session or turn record.",
            )
            return

        try:
            prior_turns = [
                turn
                for turn in get_session_turns(worker_session, strategist_session.id)
                if turn.id != turn_record.id and turn.created_at <= turn_record.created_at
            ]
            schedule_action = None
            if run.goal_id:
                from app.services.agent_kernel.state_store import GoalStateStore

                owned_goal = GoalStateStore(worker_session).load_active(strategist_session.id)
                if owned_goal is None or owned_goal.goal_id != run.goal_id:
                    mark_strategist_run_failed(
                        worker_session,
                        run,
                        error_text="Strategist run no longer owns the active goal.",
                    )
                    return
            # Release the pre-lock read transaction before waiting. Otherwise a second
            # SQLite reader can hold a table lock while the first action worker writes.
            worker_session.rollback()
            if (
                isinstance(ui_event, dict)
                and str(ui_event.get("kind") or "").strip().lower()
                == "proposal_action"
            ):
                raise HTTPException(
                    status_code=410,
                    detail=(
                        "This older proposal card has been retired. Submit the "
                        "request again to create a current review."
                    ),
                )
            # Native ReviewDocument actions use the dedicated typed endpoint.
            # Normal Strategist turns no longer enter a proposal-family lock or
            # schedule apply handler.
            with nullcontext():
                if run.goal_id:
                    from app.services.agent_kernel.state_store import GoalStateStore

                    worker_session.expire_all()
                    locked_goal = GoalStateStore(worker_session).load_active(
                        strategist_session.id
                    )
                    if locked_goal is None or locked_goal.goal_id != run.goal_id:
                        mark_strategist_run_failed(
                            worker_session,
                            run,
                            error_text="Strategist run no longer owns the active goal.",
                        )
                        return
                # Schedule action workers can start concurrently, so include the run-state
                # write in the same per-proposal critical section as lifecycle execution.
                if not mark_strategist_run_started(worker_session, run):
                    return
                # End the pre-lock read transaction so lifecycle/calendar validation sees
                # commits made by an earlier action worker for this same proposal.
                worker_session.expire_all()
                schedule_action = None
                if schedule_action is not None:
                    complete_strategist_turn(
                        worker_session,
                        strategist_session=strategist_session,
                        turn=turn_record,
                        response_payload=schedule_action.response,
                        error_text=None,
                    )
                    sync_warnings: list[str] = []
                    for block in schedule_action.sync_created_or_updated:
                        try_sync_block_to_google(worker_session, block)
                        if block.google_sync_error:
                            sync_warnings.append(
                                f"Calendar block {block.id or 0} was applied in Drasil but Google sync needs attention."
                            )
                    for removed_event in schedule_action.sync_removed_events:
                        event_id = str(removed_event.get("googleEventId") or "")
                        if not event_id:
                            continue
                        delete_error = delete_google_event_by_id(worker_session, event_id)
                        if delete_error:
                            sync_warnings.append(
                                "Calendar block "
                                f"{removed_event.get('blockId') or 0} was removed in Drasil, "
                                "but its Google Calendar event still needs attention."
                            )
                    if sync_warnings:
                        action_result = schedule_action.response.get("actionResult")
                        if isinstance(action_result, dict):
                            action_result["syncWarnings"] = sync_warnings
                        schedule_action.response["summary"] = (
                            "The approved schedule is on your Drasil calendar. "
                            "Some Google Calendar updates need attention."
                        )
                        complete_strategist_turn(
                            worker_session,
                            strategist_session=strategist_session,
                            turn=turn_record,
                            response_payload=schedule_action.response,
                            error_text=None,
                        )
                    from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator

                    active_goal = GoalStateCoordinator(worker_session).store.load_active(
                        strategist_session.id
                    )
                    if (
                        active_goal is not None
                        and run.goal_id is not None
                        and run.goal_id == active_goal.goal_id
                    ):
                        action_result = schedule_action.response.get("actionResult")
                        action_status = (
                            str(action_result.get("status") or "").strip().lower()
                            if isinstance(action_result, dict)
                            else ""
                        )
                        if action_status in {"applied", "rejected"}:
                            action_goal = GoalStateCoordinator(worker_session).sync_legacy_outcome(
                                session_id=strategist_session.id,
                                goal_id=active_goal.goal_id,
                                outcome=action_status,
                                command_id=f"schedule-action:{run.id}:{action_status}",
                                current_run_id=run.id,
                                verified_commit=action_status == "applied",
                            )
                            from app.services.strategist_runs import link_strategist_run_to_goal

                            link_strategist_run_to_goal(
                                worker_session,
                                run,
                                goal_id=action_goal.goal_id,
                                goal_revision=action_goal.revision,
                                causation_id=action_goal.causation_id,
                            )
                    mark_strategist_run_completed(worker_session, run)
                    return
            raw_today_override = request_payload.get("today_override")
            today_override = (
                date.fromisoformat(raw_today_override)
                if isinstance(raw_today_override, str) and raw_today_override.strip()
                else None
            )
            turn_result = run_strategist_graph_turn(
                session=worker_session,
                prompt=str(request_payload.get("prompt") or ""),
                ui_event=ui_event if isinstance(ui_event, dict) else None,
                active_category_id=request_payload.get("active_category_id"),
                focus_node_id=request_payload.get("focus_node_id"),
                staged_node_ids=[
                    int(node_id)
                    for node_id in request_payload.get("staged_node_ids", [])
                    if isinstance(node_id, int) or str(node_id).isdigit()
                ],
                conversation_history=(
                    build_conversation_history_from_turns(prior_turns)
                    if prior_turns
                    else request_payload.get("conversation_history", [])
                ),
                strategist_session_id=strategist_session.id,
                strategist_turn_id=turn_record.id,
                provider_override=strategist_session.provider,
                provider_session_id=strategist_session.provider_session_id,
                model_override=strategist_session.model_name,
                today_override=today_override,
                include_diagnostics=True,
                control_plane=str(
                    request_payload.get("control_plane")
                    or os.getenv("STRATEGIST_CONTROL_PLANE", DEFAULT_STRATEGIST_CONTROL_PLANE)
                ),
                requested_capability_ids=[
                    str(capability_id)
                    for capability_id in request_payload.get("requested_capability_ids", [])
                    if str(capability_id).strip()
                ],
                workspace_context={
                    "calendar_view_start": request_payload.get("calendar_view_start"),
                    "calendar_view_end": request_payload.get("calendar_view_end"),
                    "local_date_time": request_payload.get("local_date_time"),
                    "time_zone": request_payload.get("time_zone"),
                    "attached_task_ids": request_payload.get("staged_node_ids", []),
                    "instruction": (
                        "Use authoritative local_date_time and time_zone to translate relative "
                        "dates. calendar_view_* is visible context only. For existing-task "
                        "scheduling, call handoff_to_planner and end the controller phase. "
                        "A combined new-task plus finite recurring schedule stays in one "
                        "create_node plus recurring_schedule proposal."
                    ),
                },
            )
            record_prompt = str(turn_result.get("record_prompt") or run.prompt).strip()
            if record_prompt and record_prompt != turn_record.prompt:
                turn_record.prompt = record_prompt
            complete_strategist_turn(
                worker_session,
                strategist_session=strategist_session,
                turn=turn_record,
                response_payload=turn_result["response"],
                error_text=None,
                provider_session_id=turn_result.get("provider_session_id"),
            )
            combined_sync_warnings: list[str] = []
            for block in turn_result.get("sync_created_or_updated", []):
                if not isinstance(block, CalendarBlock):
                    continue
                try_sync_block_to_google(worker_session, block)
                if block.google_sync_error:
                    combined_sync_warnings.append(
                        f"Calendar block {block.id or 0} was applied in Drasil but Google sync needs attention."
                    )
            if combined_sync_warnings:
                action_result = turn_result["response"].setdefault("actionResult", {})
                if isinstance(action_result, dict):
                    action_result["syncWarnings"] = combined_sync_warnings
                turn_result["response"]["summary"] = (
                    "The changes were saved in Drasil. "
                    "Some Google Calendar updates need attention."
                )
                complete_strategist_turn(
                    worker_session,
                    strategist_session=strategist_session,
                    turn=turn_record,
                    response_payload=turn_result["response"],
                    error_text=None,
                    provider_session_id=turn_result.get("provider_session_id"),
                )
            bind_proposal_turn_ids(
                worker_session,
                proposal_ids=[
                    proposal_id
                    for proposal_id in turn_result.get("proposal_ids", [])
                    if isinstance(proposal_id, str) and proposal_id.strip()
                ],
                turn_id=turn_record.id,
            )
            should_persist_on_clarification = bool(
                turn_result.get("persist_memory_on_clarification")
            )
            persist_session_entity_memory(
                worker_session,
                strategist_session_id=strategist_session.id,
                turn_id=turn_record.id,
                resolved_entities=(
                    turn_result["memory_entities"]
                    if (
                        isinstance(turn_result.get("memory_entities"), list)
                        and (
                            should_persist_on_clarification
                            or not isinstance(turn_result["response"].get("clarification"), dict)
                        )
                    )
                    else []
                ),
            )
            mark_strategist_run_completed(worker_session, run)
        except HTTPException as error:
            worker_session.rollback()
            complete_strategist_turn(
                worker_session,
                strategist_session=strategist_session,
                turn=turn_record,
                response_payload=None,
                error_text=str(error.detail),
            )
            mark_strategist_run_failed(worker_session, run, error_text=str(error.detail))
        except Exception as error:
            worker_session.rollback()
            error_text = f"Strategist failed: {error}"
            complete_strategist_turn(
                worker_session,
                strategist_session=strategist_session,
                turn=turn_record,
                response_payload=None,
                error_text=error_text,
            )
            mark_strategist_run_failed(worker_session, run, error_text=error_text)


def submit_strategist_run(run_id: str) -> Future[None]:
    existing = IN_FLIGHT_STRATEGIST_RUNS.get(run_id)
    if existing is not None and not existing.done():
        return existing
    future = STRATEGIST_RUN_EXECUTOR.submit(
        execute_strategist_run,
        run_id,
        active_account(required=False),
    )
    IN_FLIGHT_STRATEGIST_RUNS[run_id] = future
    return future


def ensure_strategist_run_active(run_id: str, status: str | None) -> None:
    if status in {"queued", "running"}:
        submit_strategist_run(run_id)


def recover_incomplete_strategist_runs(*, limit: int = 20) -> int:
    with Session(get_active_engine()) as session:
        runs = list_incomplete_strategist_runs(session, limit=limit)
    for run in runs:
        submit_strategist_run(run.id)
    return len(runs)


def build_strategist_run_response_payload(
    session: Session,
    *,
    run_id: str,
) -> dict[str, Any]:
    session.expire_all()
    run = get_strategist_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategist run not found.")
    ensure_strategist_run_active(run.id, run.status)
    strategist_session = get_strategist_session(session, run.session_id)
    turn_record = session.get(StrategistChatTurn, run.turn_id)
    if strategist_session is None or turn_record is None:
        raise HTTPException(status_code=404, detail="Strategist run session or turn not found.")
    run_payload = serialize_strategist_run(run)
    run_payload["activities"] = public_strategist_run_activities(session, run)
    return {
        "session": serialize_session_summary(strategist_session),
        "turn": serialize_turn_record(turn_record),
        "run": run_payload,
    }


def serialize_strategist_run_response_for_wire(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return StrategistRunResponse.model_validate(payload).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
    except Exception:
        return payload


def encode_sse_event(event_name: str, payload: dict[str, Any]) -> str:
    wire_payload = serialize_strategist_run_response_for_wire(payload)
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(wire_payload, default=str)}\n\n"
    )


@router.get("/strategist/sessions", response_model=List[StrategistSessionSummary])
def strategist_sessions_index(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [serialize_session_summary(item) for item in list_strategist_sessions(session)]


@router.get("/strategist/sessions/{session_id}", response_model=StrategistSessionDetail)
def strategist_session_detail(
    session_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    strategist_session = get_strategist_session(session, session_id)
    if strategist_session is None:
        raise HTTPException(status_code=404, detail="Strategist session not found.")
    turns = get_session_turns(session, session_id)
    return {
        "session": serialize_session_summary(strategist_session),
        "turns": [serialize_turn_record(turn) for turn in turns],
    }


@router.post("/strategist/sessions/{session_id}/context")
def strategist_session_context(
    session_id: str,
    body: StrategistSessionContextRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    strategist_session = get_strategist_session(session, session_id)
    if strategist_session is None:
        raise HTTPException(status_code=404, detail="Strategist session not found.")

    turns = get_session_turns(session, session_id)
    if not turns:
        raise HTTPException(status_code=400, detail="Strategist session has no turns to attach context to.")

    unique_node_ids: list[int] = []
    for node_id in body.node_ids:
        if isinstance(node_id, int) and node_id not in unique_node_ids:
            unique_node_ids.append(node_id)
    if not unique_node_ids:
        raise HTTPException(status_code=400, detail="At least one node id is required.")

    resolved_entities: list[dict[str, Any]] = []
    for index, node_id in enumerate(unique_node_ids):
        node = session.get(StrategyNode, node_id)
        if node is None or node.id is None:
            continue
        resolved_entities.append(
            {
                "node_id": node.id,
                "title": node.title,
                "role": "primary" if index == 0 else "related",
                "confidence": 0.94 if index == 0 else 0.86,
                "source": body.source or "ui_apply",
                "mention_text": body.mention_text or node.title,
            }
        )

    if not resolved_entities:
        raise HTTPException(status_code=404, detail="None of those nodes were found.")

    persist_session_entity_memory(
        session,
        strategist_session_id=session_id,
        turn_id=turns[-1].id,
        resolved_entities=resolved_entities,
    )
    return {
        "ok": True,
        "node_ids": [entity["node_id"] for entity in resolved_entities],
    }


@router.post("/strategist", response_model=StrategistRunResponse, response_model_exclude_none=True)
def run_strategist(
    body: StrategistRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if (
        body.ui_event is not None
        and str(body.ui_event.kind or "").strip().lower() == "proposal_action"
    ):
        raise HTTPException(
            status_code=410,
            detail=(
                "This older proposal card has been retired. Submit the request "
                "again to create a current canonical review."
            ),
        )
    principal = require_authenticated_principal()
    strategist_session = None
    try:
        if body.session_id:
            strategist_session = get_strategist_session(session, body.session_id)
            if strategist_session is None:
                raise HTTPException(status_code=404, detail="Strategist session not found.")
            if strategist_session.owner_principal_id is None:
                claimed = session.exec(
                    update(StrategistChatSession)
                    .where(
                        StrategistChatSession.id == strategist_session.id,
                        StrategistChatSession.owner_principal_id.is_(None),
                    )
                    .values(owner_principal_id=principal.principal_id)
                )
                session.commit()
                if claimed.rowcount != 1:  # type: ignore[attr-defined]
                    session.refresh(strategist_session)
                else:
                    strategist_session.owner_principal_id = principal.principal_id
            if strategist_session.owner_principal_id != principal.principal_id:
                raise HTTPException(status_code=403, detail="Strategist session access denied.")
        else:
            runtime_settings = get_provider_runtime_settings()
            strategist_session = create_strategist_session(
                session,
                prompt=body.prompt,
                provider=str(runtime_settings["provider"]),
                provider_label=str(runtime_settings["provider_label"]),
                model_name=(
                    str(runtime_settings["model_name"])
                    if isinstance(runtime_settings["model_name"], str)
                    else None
                ),
                owner_principal_id=principal.principal_id,
            )

        display_prompt = body.prompt or "Strategist review action"
        turn_record = create_pending_strategist_turn(
            session,
            strategist_session=strategist_session,
            prompt=display_prompt,
        )
        run = create_strategist_run(
            session,
            strategist_session_id=strategist_session.id,
            turn_id=turn_record.id,
            prompt=display_prompt,
            request_payload=build_strategist_request_payload(body),
            control_plane=body.control_plane
            or os.getenv("STRATEGIST_CONTROL_PLANE", DEFAULT_STRATEGIST_CONTROL_PLANE),
        )
        future = submit_strategist_run(run.id)
        try:
            future.result(timeout=strategist_foreground_wait_seconds())
        except TimeoutError:
            pass
        session.expire_all()
        strategist_session = get_strategist_session(session, strategist_session.id) or strategist_session
        turn_record = session.get(StrategistChatTurn, turn_record.id) or turn_record
        refreshed_run = get_strategist_run(session, run.id) or run
        refreshed_run_payload = serialize_strategist_run(refreshed_run)
        refreshed_run_payload["activities"] = public_strategist_run_activities(
            session,
            refreshed_run,
        )
        return {
            "session": serialize_session_summary(strategist_session),
            "turn": serialize_turn_record(turn_record),
            "run": refreshed_run_payload,
        }
    except HTTPException as error:
        if strategist_session is not None:
            record_strategist_turn(
                session,
                strategist_session=strategist_session,
                prompt=body.prompt,
                response_payload=None,
                error_text=str(error.detail),
            )
        raise
    except Exception as error:
        if strategist_session is not None:
            record_strategist_turn(
                session,
                strategist_session=strategist_session,
                prompt=body.prompt,
                response_payload=None,
                error_text=f"Strategist failed: {error}",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Strategist failed: {error}",
        ) from error


@router.get("/strategist/runs/{run_id}", response_model=StrategistRunResponse, response_model_exclude_none=True)
def strategist_run_detail(
    run_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return build_strategist_run_response_payload(session, run_id=run_id)


@router.get("/strategist/runs/{run_id}/events")
def strategist_run_events(run_id: str) -> StreamingResponse:
    request_account = active_account(required=False)

    def event_stream():
        with use_account(request_account):
            yield from _event_stream_for_account()

    def _event_stream_for_account():
        last_fingerprint: str | None = None
        last_heartbeat_at = 0.0
        deadline = time.monotonic() + strategist_run_stream_max_seconds()
        while time.monotonic() < deadline:
            now = time.monotonic()
            with Session(get_active_engine()) as stream_session:
                try:
                    payload = build_strategist_run_response_payload(
                        stream_session,
                        run_id=run_id,
                    )
                except HTTPException as error:
                    yield encode_sse_event(
                        "error",
                        {"detail": str(error.detail), "status_code": error.status_code},
                    )
                    return

            run_payload = payload.get("run") if isinstance(payload.get("run"), dict) else {}
            turn_payload = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
            fingerprint = json.dumps(
                {
                    "run_status": run_payload.get("status"),
                    "run_updated_at": run_payload.get("updated_at"),
                    "run_activities": run_payload.get("activities"),
                    "turn_response": bool(turn_payload.get("response")),
                    "turn_error": turn_payload.get("error"),
                },
                default=str,
                sort_keys=True,
            )
            if fingerprint != last_fingerprint:
                yield encode_sse_event("update", payload)
                last_fingerprint = fingerprint
                last_heartbeat_at = now
            elif now - last_heartbeat_at >= 15:
                yield encode_sse_event(
                    "heartbeat",
                    {
                        "run_id": run_id,
                        "status": str(run_payload.get("status") or "unknown"),
                    },
                )
                last_heartbeat_at = now

            status = str(run_payload.get("status") or "")
            if status in RUN_STREAM_TERMINAL_STATUSES:
                yield encode_sse_event("done", payload)
                return
            time.sleep(0.75)

        yield encode_sse_event(
            "heartbeat",
            {"run_id": run_id, "status": "stream_timeout"},
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
