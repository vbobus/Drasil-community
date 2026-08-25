"""Typed, server-owned ReviewDocument interaction endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db.database import get_session
from app.services.agent_kernel.contracts import ReviewAction, ReviewActionOutcome
from app.services.agent_kernel.review_actions import ReviewActionCoordinator
from app.models.strategist_chat import StrategistChatSession
from app.services.principal_auth import AuthenticatedPrincipal, resolve_authenticated_principal


router = APIRouter(prefix="/api/v1/ai/strategist", tags=["AI"])


@router.post(
    "/sessions/{session_id}/review-actions",
    response_model=ReviewActionOutcome,
    response_model_exclude_none=True,
)
def execute_review_action(
    session_id: str,
    action: ReviewAction,
    session: Session = Depends(get_session),
    principal: AuthenticatedPrincipal | None = Depends(resolve_authenticated_principal),
) -> ReviewActionOutcome:
    """Apply only the exact typed identity carried by the current review."""

    strategist_session = session.get(StrategistChatSession, session_id)
    if (
        principal is None
        or strategist_session is None
        or strategist_session.owner_principal_id is None
        or strategist_session.owner_principal_id != principal.principal_id
    ):
        return ReviewActionOutcome(
            status="error",
            error={
                "code": "authorization_denied",
                "summary": "This review does not belong to the authenticated account.",
                "recovery": "none",
            },
        )
    return ReviewActionCoordinator(session).execute(
        authenticated_session_id=session_id,
        action=action,
    )
