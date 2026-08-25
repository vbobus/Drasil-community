"""
Durable Strategist run lifecycle helpers.

Runs are the boundary between the UI's short wait window and the longer-lived
agent workflow. The run record survives request timeouts/restarts even when the
current implementation executes workers in-process.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, update
from sqlmodel import Session, select

from app.models.strategist_agent import StrategistAgentEvent, StrategistRun
from app.services.agent_kernel.state_store import GoalStateStore


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


def _event_payload(event: StrategistAgentEvent) -> dict[str, Any]:
    try:
        payload = json.loads(event.payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _public_tool_activity(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in {"list_calendar_blocks", "read_calendar", "get_calendar_state"}:
        return "Checking your calendar"
    if normalized == "handoff_to_planner":
        return "Sending your constraints to the scheduler"
    if normalized.startswith("search_") or normalized in {
        "query_task_graph",
        "list_tasks_by_filter",
        "get_task_snapshot",
        "get_task_branch",
        "get_subtree",
    }:
        return "Finding the relevant tasks"
    if normalized in {"propose_tree_operation", "propose_task_operation"}:
        return "Preparing task changes for review"
    if "preference" in normalized:
        return "Checking your scheduling preferences"
    if normalized == "load_skill":
        return "Loading the right planning workflow"
    return "Using workspace tools"


def _public_event_activity(event: StrategistAgentEvent) -> tuple[str, str] | None:
    payload = _event_payload(event)
    event_type = event.event_type
    if event_type == "user_turn":
        return "understanding", "Understanding your request"
    if event_type in {"tier_route_decision", "context_packet"}:
        return "context", "Reviewing the relevant workspace context"
    if event_type in {"model_action_loop_started", "model_rescue_started"}:
        return "planning", "Planning the next steps"
    if event_type == "tool_observation":
        return "tool", _public_tool_activity(str(payload.get("name") or ""))
    if event_type == "planner_handoff_started":
        return "calendar", "Scheduling around your constraints"
    if event_type == "planner_handoff_completed":
        return "validation", "Checking the proposed schedule"
    if event_type in {
        "proposal_tool_contract_compiled",
        "proposal_contract_repair",
        "model_contract_retry_started",
        "model_contract_retry_completed",
    }:
        return "validation", "Validating the proposed changes"
    if event_type == "provider_failure":
        return "warning", "Recovering from an interrupted model step"
    if event_type == "final_answer":
        return "finalizing", "Finalizing the response"
    return None


def public_strategist_run_activities(
    session: Session,
    run: StrategistRun,
) -> list[dict[str, Any]]:
    """Return a safe progress timeline without model chain-of-thought or raw arguments."""

    anchor = session.exec(
        select(StrategistAgentEvent)
        .where(StrategistAgentEvent.turn_id == run.turn_id)
        .order_by(StrategistAgentEvent.created_at.desc())
    ).first()
    if anchor is None:
        return []
    events = list(
        session.exec(
            select(StrategistAgentEvent)
            .where(StrategistAgentEvent.correlation_id == anchor.correlation_id)
            .order_by(StrategistAgentEvent.sequence, StrategistAgentEvent.created_at)
        ).all()
    )
    activities: list[dict[str, Any]] = []
    for event in events:
        public = _public_event_activity(event)
        if public is None:
            continue
        kind, label = public
        if activities and activities[-1]["label"] == label:
            continue
        activities.append(
            {
                "id": event.id,
                "kind": kind,
                "label": label,
                "status": "completed",
                "createdAt": event.created_at,
            }
        )
    if activities and run.status not in TERMINAL_RUN_STATUSES:
        activities[-1]["status"] = "active"
    elif activities and run.status == "failed":
        activities[-1]["status"] = "failed"
    return activities


def create_strategist_run(
    session: Session,
    *,
    strategist_session_id: str,
    turn_id: str,
    prompt: str,
    request_payload: dict[str, Any],
    control_plane: str | None,
) -> StrategistRun:
    active_goal = (
        GoalStateStore(session).load_active(strategist_session_id)
        if isinstance(session, Session)
        else None
    )
    run = StrategistRun(
        session_id=strategist_session_id,
        turn_id=turn_id,
        status="queued",
        control_plane=control_plane,
        goal_id=active_goal.goal_id if active_goal is not None else None,
        goal_revision=active_goal.revision if active_goal is not None else None,
        correlation_id=(active_goal.correlation_id if active_goal is not None else str(uuid4())),
        causation_id=turn_id,
        prompt=prompt,
        request_json=json.dumps(request_payload),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def link_strategist_run_to_goal(
    session: Session,
    run: StrategistRun,
    *,
    goal_id: str,
    goal_revision: int,
    causation_id: str,
) -> StrategistRun:
    run.goal_id = goal_id
    run.goal_revision = goal_revision
    run.causation_id = causation_id
    run.updated_at = datetime.utcnow()
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def get_strategist_run(session: Session, run_id: str) -> StrategistRun | None:
    return session.get(StrategistRun, run_id)


def list_incomplete_strategist_runs(
    session: Session,
    *,
    limit: int = 20,
) -> list[StrategistRun]:
    now = datetime.utcnow()
    statement = (
        select(StrategistRun)
        .where(StrategistRun.status.in_(["queued", "running"]))  # type: ignore[attr-defined]
        .where(
            or_(
                StrategistRun.status == "queued",
                StrategistRun.worker_token.is_(None),  # type: ignore[union-attr]
                StrategistRun.lease_expires_at.is_(None),  # type: ignore[union-attr]
                StrategistRun.lease_expires_at < now,
            )
        )
        .order_by(StrategistRun.created_at)
        .limit(limit)
    )
    return list(session.exec(statement).all())


def parse_run_request(run: StrategistRun) -> dict[str, Any]:
    try:
        parsed = json.loads(run.request_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def mark_strategist_run_started(session: Session, run: StrategistRun) -> bool:
    now = datetime.utcnow()
    token = str(uuid4())
    result = session.exec(
        update(StrategistRun)
        .where(StrategistRun.id == run.id)
        .where(
            or_(
                StrategistRun.status == "queued",
                StrategistRun.worker_token.is_(None),  # type: ignore[union-attr]
                StrategistRun.lease_expires_at.is_(None),  # type: ignore[union-attr]
                StrategistRun.lease_expires_at < now,
            )
        )
        .values(
            status="running",
            started_at=run.started_at or now,
            updated_at=now,
            worker_token=token,
            lease_expires_at=now + timedelta(minutes=2),
        )
    )
    session.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return False
    session.expire_all()
    session.refresh(run)
    return True


def mark_strategist_run_completed(session: Session, run: StrategistRun) -> StrategistRun:
    now = datetime.utcnow()
    run.status = "completed"
    run.completed_at = now
    run.updated_at = now
    run.error_text = None
    run.worker_token = None
    run.lease_expires_at = None
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def mark_strategist_run_failed(
    session: Session,
    run: StrategistRun,
    *,
    error_text: str,
) -> StrategistRun:
    now = datetime.utcnow()
    run.status = "failed"
    run.completed_at = now
    run.updated_at = now
    run.error_text = error_text
    run.worker_token = None
    run.lease_expires_at = None
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def serialize_strategist_run(run: StrategistRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "turn_id": run.turn_id,
        "status": run.status,
        "control_plane": run.control_plane,
        "goal_id": run.goal_id,
        "goal_revision": run.goal_revision,
        "correlation_id": run.correlation_id,
        "causation_id": run.causation_id,
        "worker_token": run.worker_token,
        "lease_expires_at": run.lease_expires_at.isoformat() if run.lease_expires_at else None,
        "prompt": run.prompt,
        "error_text": run.error_text,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "updated_at": run.updated_at,
    }
