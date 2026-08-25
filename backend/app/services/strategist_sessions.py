"""
Strategist chat session persistence helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select

from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn
from app.services.entity_memory import build_working_set_snapshot, persist_turn_memory


class ScheduleProposalActionError(ValueError):
    """Raised when a typed SchedulePatch action cannot resolve its exact source."""


@dataclass(frozen=True)
class ScheduleProposalActionSource:
    turn: StrategistChatTurn
    response: dict[str, Any]
    proposal: dict[str, Any]
    lifecycle_status: str


def build_session_title(prompt: str) -> str:
    trimmed = " ".join(prompt.strip().split())
    if not trimmed:
        return "New strategist chat"
    return trimmed[:80]


def list_strategist_sessions(session: Session) -> list[StrategistChatSession]:
    return list(
        session.exec(
            select(StrategistChatSession).order_by(StrategistChatSession.updated_at.desc())
        ).all()
    )


def get_strategist_session(session: Session, session_id: str) -> StrategistChatSession | None:
    return session.get(StrategistChatSession, session_id)


def delete_strategist_session(session: Session, session_id: str) -> bool:
    """Delete one Strategist session and its owned history atomically.

    Product-domain tasks, calendars, recurrences, and preferences are not owned
    by a chat session and are deliberately untouched.
    """

    strategist_session = session.get(StrategistChatSession, session_id)
    if strategist_session is None:
        return False

    from app.models.change_set import ChangeSetRecord
    from app.models.strategist_agent import StrategistAgentEvent, StrategistRun
    from app.models.strategist_graph import StrategistGraphThread, StrategistProposalRecord
    from app.models.strategist_session_entities import StrategistSessionEntity
    from app.models.strategist_turn_mentions import StrategistTurnMention
    from app.services.agent_kernel.state_store import GoalStateStore

    with session.begin_nested():
        GoalStateStore(session).delete_session_history(session_id)
        session.exec(
            delete(StrategistTurnMention).where(
                StrategistTurnMention.session_id == session_id
            )
        )
        session.exec(
            delete(StrategistSessionEntity).where(
                StrategistSessionEntity.session_id == session_id
            )
        )
        session.exec(
            delete(StrategistAgentEvent).where(
                StrategistAgentEvent.session_id == session_id
            )
        )
        session.exec(delete(StrategistRun).where(StrategistRun.session_id == session_id))
        session.exec(
            delete(StrategistProposalRecord).where(
                StrategistProposalRecord.session_id == session_id
            )
        )
        session.exec(delete(ChangeSetRecord).where(ChangeSetRecord.session_id == session_id))
        session.exec(
            delete(StrategistGraphThread).where(
                StrategistGraphThread.chat_session_id == session_id
            )
        )
        session.exec(
            delete(StrategistChatTurn).where(StrategistChatTurn.session_id == session_id)
        )
        session.delete(strategist_session)
        session.flush()
    return True


def get_session_turns(session: Session, session_id: str) -> list[StrategistChatTurn]:
    return list(
        session.exec(
            select(StrategistChatTurn)
            .where(StrategistChatTurn.session_id == session_id)
            .order_by(StrategistChatTurn.created_at)
        ).all()
    )


def create_strategist_session(
    session: Session,
    *,
    prompt: str,
    provider: str,
    provider_label: str,
    model_name: str | None,
    owner_principal_id: str | None = None,
) -> StrategistChatSession:
    strategist_session = StrategistChatSession(
        title=build_session_title(prompt),
        provider=provider,
        provider_label=provider_label,
        model_name=model_name,
        owner_principal_id=owner_principal_id,
    )
    session.add(strategist_session)
    session.commit()
    session.refresh(strategist_session)
    return strategist_session


def record_strategist_turn(
    session: Session,
    *,
    strategist_session: StrategistChatSession,
    prompt: str,
    response_payload: dict[str, Any] | None,
    error_text: str | None,
    provider_session_id: str | None = None,
) -> StrategistChatTurn:
    turn = StrategistChatTurn(
        session_id=strategist_session.id,
        prompt=prompt,
        response_json=json.dumps(response_payload) if response_payload is not None else None,
        error_text=error_text,
    )
    session.add(turn)

    strategist_session.updated_at = datetime.utcnow()
    if provider_session_id:
        strategist_session.provider_session_id = provider_session_id
    if not strategist_session.title or strategist_session.title == "New strategist chat":
        strategist_session.title = build_session_title(prompt)
    session.add(strategist_session)
    session.commit()
    session.refresh(turn)
    session.refresh(strategist_session)
    return turn


def create_pending_strategist_turn(
    session: Session,
    *,
    strategist_session: StrategistChatSession,
    prompt: str,
) -> StrategistChatTurn:
    turn = StrategistChatTurn(
        session_id=strategist_session.id,
        prompt=prompt,
        response_json=None,
        error_text=None,
    )
    session.add(turn)
    strategist_session.updated_at = datetime.utcnow()
    if not strategist_session.title or strategist_session.title == "New strategist chat":
        strategist_session.title = build_session_title(prompt)
    session.add(strategist_session)
    session.commit()
    session.refresh(turn)
    session.refresh(strategist_session)
    return turn


def complete_strategist_turn(
    session: Session,
    *,
    strategist_session: StrategistChatSession,
    turn: StrategistChatTurn,
    response_payload: dict[str, Any] | None,
    error_text: str | None,
    provider_session_id: str | None = None,
) -> StrategistChatTurn:
    turn.response_json = json.dumps(response_payload) if response_payload is not None else None
    turn.error_text = error_text
    session.add(turn)

    strategist_session.updated_at = datetime.utcnow()
    if provider_session_id:
        strategist_session.provider_session_id = provider_session_id
    if not strategist_session.title or strategist_session.title == "New strategist chat":
        strategist_session.title = build_session_title(turn.prompt)
    session.add(strategist_session)
    session.commit()
    session.refresh(turn)
    session.refresh(strategist_session)
    return turn


def parse_turn_response_payload(record: StrategistChatTurn) -> dict[str, Any] | None:
    if not record.response_json:
        return None
    try:
        parsed = json.loads(record.response_json)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def resolve_schedule_proposal_action_source(
    session: Session,
    *,
    strategist_session_id: str,
    source_turn_id: str,
    proposal_id: str,
    allowed_statuses: set[str] | None = None,
) -> ScheduleProposalActionSource:
    """Resolve a SchedulePatch action by its complete durable identity."""

    turn = session.get(StrategistChatTurn, source_turn_id)
    if turn is None or turn.session_id != strategist_session_id:
        raise ScheduleProposalActionError(
            "This schedule proposal no longer belongs to the active workspace. Generate a fresh proposal."
        )
    response = parse_turn_response_payload(turn)
    proposal = response.get("proposal") if isinstance(response, dict) else None
    if not isinstance(proposal, dict):
        raise ScheduleProposalActionError("The source turn does not contain a schedule proposal.")
    if str(proposal.get("proposalKind") or proposal.get("proposal_kind") or "") != "schedule_patch":
        raise ScheduleProposalActionError("The requested proposal is not a SchedulePatch.")
    persisted_proposal_id = str(
        proposal.get("proposalId") or proposal.get("proposal_id") or ""
    )
    if not proposal_id or persisted_proposal_id != proposal_id:
        raise ScheduleProposalActionError(
            "The schedule proposal identity does not match its source turn. Generate a fresh proposal."
        )
    payload = proposal.get("payload")
    lifecycle = payload.get("schedulePatchLifecycle") if isinstance(payload, dict) else None
    lifecycle_status = (
        str(lifecycle.get("status") or "pending")
        if isinstance(lifecycle, dict)
        else "pending"
    )
    accepted_statuses = allowed_statuses or {"pending"}
    if lifecycle_status not in accepted_statuses:
        raise ScheduleProposalActionError(
            f"This schedule proposal is already {lifecycle_status}. Generate a fresh proposal to continue."
        )
    return ScheduleProposalActionSource(
        turn=turn,
        response=response,
        proposal=proposal,
        lifecycle_status=lifecycle_status,
    )


def set_schedule_proposal_lifecycle(
    session: Session,
    *,
    source: ScheduleProposalActionSource,
    status: str,
    action_turn_id: str | None = None,
    execution: dict[str, Any] | None = None,
    superseded_by_proposal_id: str | None = None,
    commit: bool = False,
) -> ScheduleProposalActionSource:
    """Persist SchedulePatch lifecycle state on its canonical source turn."""

    if status not in {"pending", "superseded", "rejected", "applied"}:
        raise ValueError("Unsupported SchedulePatch lifecycle status.")
    response = dict(source.response)
    proposal = dict(source.proposal)
    payload = dict(proposal.get("payload") or {})
    lifecycle: dict[str, Any] = {
        "status": status,
        "updatedAt": datetime.utcnow().isoformat(),
    }
    if action_turn_id:
        lifecycle["actionTurnId"] = action_turn_id
    if execution is not None:
        lifecycle["execution"] = execution
    if superseded_by_proposal_id:
        lifecycle["supersededByProposalId"] = superseded_by_proposal_id
    payload["schedulePatchLifecycle"] = lifecycle
    proposal["payload"] = payload
    response["proposal"] = proposal
    source.turn.response_json = json.dumps(response)
    session.add(source.turn)
    if commit:
        session.commit()
        session.refresh(source.turn)
    return ScheduleProposalActionSource(
        turn=source.turn,
        response=response,
        proposal=proposal,
        lifecycle_status=status,
    )


def get_latest_session_proposal(
    session: Session,
    strategist_session_id: str | None,
) -> dict[str, Any] | None:
    if not strategist_session_id:
        return None

    turns = get_session_turns(session, strategist_session_id)
    for turn in reversed(turns):
        payload = parse_turn_response_payload(turn)
        if not isinstance(payload, dict):
            continue
        action_proposal = payload.get("action_proposal") or payload.get("actionProposal")
        metadata_proposal = payload.get("metadata_proposal") or payload.get("metadataProposal")
        branch_proposal = payload.get("branch_proposal") or payload.get("branchProposal")
        if isinstance(branch_proposal, dict):
            return {
                "kind": "branch",
                "proposal": branch_proposal,
                "response": payload,
                "turn_id": turn.id,
                "prompt": turn.prompt,
            }
        if isinstance(metadata_proposal, dict):
            return {
                "kind": "metadata",
                "proposal": metadata_proposal,
                "response": payload,
                "turn_id": turn.id,
                "prompt": turn.prompt,
            }
        if isinstance(action_proposal, dict):
            return {
                "kind": "action",
                "proposal": action_proposal,
                "response": payload,
                "turn_id": turn.id,
                "prompt": turn.prompt,
            }
    return None


def get_session_working_set(
    session: Session,
    strategist_session_id: str | None,
) -> list[dict[str, Any]]:
    return build_working_set_snapshot(
        db=session,
        strategist_session_id=strategist_session_id,
    )


def persist_session_entity_memory(
    session: Session,
    *,
    strategist_session_id: str | None,
    turn_id: str,
    resolved_entities: list[dict[str, Any]],
) -> None:
    persist_turn_memory(
        session,
        strategist_session_id=strategist_session_id,
        turn_id=turn_id,
        resolved_entities=resolved_entities,
    )
