"""
External calendar integration endpoints.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, alias_generators
from sqlmodel import Session

from app.db.database import get_session
from app.services.account_auth import revoke_external_auth_state
from app.services.google_calendar import (
    GoogleCalendarError,
    build_google_authorization,
    complete_google_oauth,
    disconnect_google_calendar,
    public_google_calendar_status,
    pull_google_time_edits,
    push_all_blocks_to_google,
    render_google_oauth_popup_result,
)

router = APIRouter(
    prefix="/api/v1/calendar-integrations",
    tags=["Calendar Integrations"],
)


class GoogleConnectRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )


class GoogleSyncRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    direction: Literal["push", "pull", "both"] = "both"


@router.get("/google/status")
def google_calendar_status(session: Session = Depends(get_session)) -> dict:
    return public_google_calendar_status(session)


@router.post("/google/connect")
def google_calendar_connect(
    _body: GoogleConnectRequest,
    session: Session = Depends(get_session),
) -> dict:
    return build_google_authorization(session)


def _oauth_error_message(error: object) -> str:
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, dict):
            nested_error = detail.get("error")
            if isinstance(nested_error, dict) and isinstance(nested_error.get("detail"), str):
                return nested_error["detail"]
            if isinstance(detail.get("detail"), str):
                return detail["detail"]
        if isinstance(detail, str):
            return detail
    return str(error)


@router.get("/google/callback", response_class=HTMLResponse)
def google_calendar_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    if error:
        revoke_external_auth_state(state, provider="google-calendar")
        return HTMLResponse(
            render_google_oauth_popup_result(
                ok=False,
                error=error_description or error,
            ),
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            render_google_oauth_popup_result(
                ok=False,
                error="Google did not return the authorization details Drasil needs.",
            ),
            status_code=400,
        )
    try:
        status_payload = complete_google_oauth(session, code=code, state=state)
        revoke_external_auth_state(state, provider="google-calendar")
        return HTMLResponse(
            render_google_oauth_popup_result(ok=True, status_payload=status_payload),
        )
    except HTTPException as error_response:
        return HTMLResponse(
            render_google_oauth_popup_result(
                ok=False,
                error=_oauth_error_message(error_response),
            ),
            status_code=error_response.status_code,
        )
    except GoogleCalendarError as error:
        return HTMLResponse(
            render_google_oauth_popup_result(ok=False, error=str(error)),
            status_code=502,
        )


@router.delete("/google")
def google_calendar_disconnect(session: Session = Depends(get_session)) -> dict:
    return disconnect_google_calendar(session)


@router.post("/google/sync")
def google_calendar_sync(
    body: GoogleSyncRequest,
    session: Session = Depends(get_session),
) -> dict:
    try:
        result: dict[str, object] = {"direction": body.direction}
        if body.direction in {"pull", "both"}:
            result["pull"] = pull_google_time_edits(session)
        if body.direction in {"push", "both"}:
            result["push"] = push_all_blocks_to_google(session)
        result["status"] = public_google_calendar_status(session)
        return result
    except HTTPException:
        raise
    except GoogleCalendarError as error:
        raise HTTPException(status_code=error.status_code or 502, detail=str(error)) from error
