"""
Google Calendar integration service.

Drasil remains the source of truth. Google Calendar receives mirrored events in
a dedicated Drasil calendar; inbound Google sync only updates block start/end.
"""

from __future__ import annotations

import json
import os
import secrets
import base64
import hashlib
import re
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlmodel import Session, select

from app.core.env import load_local_env
from app.services.account_auth import account_private_path, register_external_auth_state
from app.models.calendar_block import CalendarBlock
from app.models.calendar_integration import CalendarIntegration
from app.models.category import Category
from app.models.node import StrategyNode

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_PROVIDER = "google"
GOOGLE_CALENDAR_SUMMARY = "Drasil"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.app.created",
)
DRASIL_SYNC_VERSION = "1"
GOOGLE_OAUTH_POPUP_MESSAGE_TYPE = "drasil:google-calendar:oauth"
GOOGLE_REAUTHORIZATION_MESSAGE = (
    "Your Google Calendar connection has expired or was revoked. "
    "Reconnect Google Calendar to continue syncing."
)


def deterministic_google_event_id(semantic_key: str) -> str:
    """Return a stable Google-compatible base32hex ID for retry-safe creates."""

    digest = hashlib.sha256(semantic_key.encode("utf-8")).digest()
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


class GoogleCalendarError(RuntimeError):
    """Raised when a Google Calendar request cannot be completed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class GoogleCalendarAuthorizationError(GoogleCalendarError):
    """Raised when Google's saved OAuth grant can no longer be used."""


class GoogleOutboxPayloadError(GoogleCalendarError):
    """Raised when a durable outbox envelope cannot be delivered safely."""


def _is_invalid_google_grant_error(value: str | None) -> bool:
    normalized = str(value or "").lower()
    return "invalid_grant" in normalized or GOOGLE_REAUTHORIZATION_MESSAGE.lower() in normalized


def _get_settings_path() -> Path:
    return account_private_path(".google-calendar.json")


def _load_raw_settings() -> dict[str, str]:
    settings_path = _get_settings_path()
    if not settings_path.exists():
        return {}

    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    return {
        str(key): str(value)
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _read_google_client_json_config() -> dict[str, str]:
    raw_path = (
        os.getenv("GOOGLE_CALENDAR_CLIENT_JSON")
        or os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET_FILE")
        or os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE")
    )
    if not raw_path:
        return {}

    config_path = Path(raw_path).expanduser()
    if not config_path.is_absolute():
        config_path = Path(__file__).resolve().parents[2] / config_path
    if not config_path.exists():
        return {}

    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}
    web_config = parsed.get("web")
    if not isinstance(web_config, dict):
        return {}

    redirect_uris = web_config.get("redirect_uris")
    redirect_uri = None
    if isinstance(redirect_uris, list):
        redirect_uri = next((value for value in redirect_uris if isinstance(value, str) and value), None)

    config: dict[str, str] = {}
    client_id = web_config.get("client_id")
    client_secret = web_config.get("client_secret")
    if isinstance(client_id, str) and client_id:
        config["client_id"] = client_id
    if isinstance(client_secret, str) and client_secret:
        config["client_secret"] = client_secret
    if redirect_uri:
        config["redirect_uri"] = redirect_uri
    return config


def _utcnow() -> datetime:
    return datetime.utcnow()


def _read_google_oauth_config() -> dict[str, str | None]:
    load_local_env()
    raw_settings = _load_raw_settings()
    client_json_config = _read_google_client_json_config()
    return {
        "client_id": raw_settings.get("clientId") or os.getenv("GOOGLE_CALENDAR_CLIENT_ID") or client_json_config.get("client_id"),
        "client_secret": raw_settings.get("clientSecret")
        or os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET")
        or client_json_config.get("client_secret"),
        "redirect_uri": raw_settings.get("redirectUri") or os.getenv(
            "GOOGLE_CALENDAR_REDIRECT_URI",
            client_json_config.get(
                "redirect_uri",
                "http://localhost:8000/api/v1/calendar-integrations/google/callback",
            ),
        ),
        "time_zone": raw_settings.get("timeZone")
        or os.getenv("DRASIL_TIMEZONE")
        or os.getenv("GOOGLE_CALENDAR_TIME_ZONE")
        or os.getenv("TZ")
        or "UTC",
    }


def _read_frontend_origin() -> str:
    load_local_env()
    return (
        os.getenv("DRASIL_FRONTEND_ORIGIN")
        or os.getenv("FRONTEND_ORIGIN")
        or "http://localhost:5173"
    )


def render_google_oauth_popup_result(
    *,
    ok: bool,
    status_payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    target_origin = _read_frontend_origin()
    payload = {
        "type": GOOGLE_OAUTH_POPUP_MESSAGE_TYPE,
        "ok": ok,
        "status": status_payload if ok else None,
        "error": error if not ok else None,
    }
    payload_json = json.dumps(payload).replace("</", "<\\/")
    target_origin_json = json.dumps(target_origin)
    title = "Google Calendar connected" if ok else "Google Calendar connection failed"
    message = (
        "You can close this window and return to Drasil."
        if ok
        else (error or "Drasil could not finish the Google Calendar connection.")
    )
    auto_close = "window.setTimeout(function () { window.close(); }, 900);" if ok else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #f6f8fc;
        color: #132a52;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(420px, calc(100vw - 32px));
        padding: 24px;
        border: 1px solid #dbe4f4;
        border-radius: 18px;
        background: white;
        box-shadow: 0 22px 44px -34px rgba(31, 48, 86, 0.42);
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 1.12rem;
      }}
      p {{
        margin: 0;
        color: #5d7090;
        line-height: 1.5;
      }}
      button {{
        margin-top: 18px;
        min-height: 36px;
        padding: 0 14px;
        border: 1px solid #dbe4f4;
        border-radius: 12px;
        background: #ffffff;
        color: #183760;
        font: inherit;
        font-weight: 700;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>{escape(title)}</h1>
      <p>{escape(message)}</p>
      <button type="button" onclick="window.close()">Close</button>
    </main>
    <script>
      (function () {{
        var payload = {payload_json};
        var targetOrigin = {target_origin_json};
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage(payload, targetOrigin);
        }}
        {auto_close}
      }}());
    </script>
  </body>
</html>"""


def _assert_google_oauth_configured() -> dict[str, str]:
    config = _read_google_oauth_config()
    missing = [
        name
        for name in ("client_id", "client_secret", "redirect_uri")
        if not config.get(name)
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "type": "GoogleCalendarNotConfigured",
                    "detail": (
                        "Google Calendar sign-in is not available because this Drasil build "
                        "does not have app-level Google OAuth credentials configured."
                    ),
                    "missing": missing,
                }
            },
        )
    return {key: str(value) for key, value in config.items() if value is not None}


def _google_request(
    method: str,
    url: str,
    *,
    access_token: str | None = None,
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {"Accept": "application/json"}
    payload: bytes | None = None
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    elif form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        payload = urlencode(form).encode("utf-8")

    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            raw_body = response.read()
    except HTTPError as error:
        try:
            error_body = error.read().decode("utf-8")
        except Exception:
            error_body = str(error)
        error_code = ""
        error_reason = ""
        try:
            parsed_error = json.loads(error_body)
            if isinstance(parsed_error, dict):
                raw_error = parsed_error.get("error")
                if isinstance(raw_error, str):
                    error_code = raw_error
                elif isinstance(raw_error, dict):
                    error_code = str(raw_error.get("status") or raw_error.get("code") or "")
                    errors = raw_error.get("errors")
                    if isinstance(errors, list):
                        first = next(
                            (item for item in errors if isinstance(item, dict)),
                            None,
                        )
                        if first is not None:
                            error_reason = str(first.get("reason") or "")
        except json.JSONDecodeError:
            pass
        if error_code.lower() == "invalid_grant":
            raise GoogleCalendarAuthorizationError(
                GOOGLE_REAUTHORIZATION_MESSAGE,
                status_code=status.HTTP_401_UNAUTHORIZED,
            ) from error
        raise GoogleCalendarError(
            f"Google Calendar request failed with HTTP {error.code}: {error_body}",
            status_code=error.code,
            reason=error_reason or error_code or None,
        ) from error
    except URLError as error:
        raise GoogleCalendarError(f"Google Calendar request failed: {error}") from error

    if not raw_body:
        return {}
    parsed = json.loads(raw_body.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def get_google_integration(session: Session) -> CalendarIntegration | None:
    return session.exec(
        select(CalendarIntegration).where(CalendarIntegration.provider == GOOGLE_PROVIDER)
    ).first()


def get_or_create_google_integration(session: Session) -> CalendarIntegration:
    integration = get_google_integration(session)
    if integration:
        return integration

    config = _read_google_oauth_config()
    integration = CalendarIntegration(
        provider=GOOGLE_PROVIDER,
        google_calendar_summary=GOOGLE_CALENDAR_SUMMARY,
        time_zone=str(config["time_zone"] or "UTC"),
    )
    session.add(integration)
    session.commit()
    session.refresh(integration)
    return integration


def public_google_calendar_status(session: Session) -> dict[str, Any]:
    integration = get_google_integration(session)
    config = _read_google_oauth_config()
    is_configured = bool(config["client_id"] and config["client_secret"] and config["redirect_uri"])
    requires_reconnect = bool(
        integration and _is_invalid_google_grant_error(integration.last_sync_error)
    )
    return {
        "provider": GOOGLE_PROVIDER,
        "isConfigured": is_configured,
        "isConnected": bool(
            integration and integration.is_connected and not requires_reconnect
        ),
        "requiresReconnect": requires_reconnect,
        "calendarId": integration.google_calendar_id if integration else None,
        "calendarSummary": integration.google_calendar_summary if integration else GOOGLE_CALENDAR_SUMMARY,
        "accountEmail": integration.google_account_email if integration else None,
        "timeZone": integration.time_zone if integration else config["time_zone"],
        "lastSyncedAt": integration.last_synced_at.isoformat() if integration and integration.last_synced_at else None,
        "lastSyncError": (
            GOOGLE_REAUTHORIZATION_MESSAGE
            if requires_reconnect
            else integration.last_sync_error if integration else None
        ),
        "scopes": list(GOOGLE_SCOPES),
        "syncPolicy": {
            "sourceOfTruth": "drasil",
            "googleTimeEdits": "sync_back",
            "googleTitleAndDescriptionEdits": "ignored",
            "completedTitlePrefix": "[Done]",
            "calendarMode": "dedicated_drasil_calendar",
        },
    }


def build_google_authorization(session: Session) -> dict[str, Any]:
    config = _assert_google_oauth_configured()
    integration = get_or_create_google_integration(session)
    state = secrets.token_urlsafe(32)
    register_external_auth_state(state, provider="google-calendar")
    resolved_redirect_uri = config["redirect_uri"]

    integration.oauth_state = state
    integration.oauth_redirect_uri = resolved_redirect_uri
    integration.time_zone = config.get("time_zone", integration.time_zone)
    integration.updated_at = _utcnow()
    session.add(integration)
    session.commit()

    params = {
        "client_id": config["client_id"],
        "redirect_uri": resolved_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return {
        "authorizationUrl": f"{GOOGLE_AUTH_URL}?{urlencode(params)}",
        "state": state,
        "scopes": list(GOOGLE_SCOPES),
        "redirectUri": resolved_redirect_uri,
    }


def _apply_token_response(
    integration: CalendarIntegration,
    token_response: dict[str, Any],
) -> None:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleCalendarError("Google OAuth token response did not include access_token.")

    integration.access_token = access_token
    refresh_token = token_response.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        integration.refresh_token = refresh_token
    token_type = token_response.get("token_type")
    integration.token_type = token_type if isinstance(token_type, str) else "Bearer"
    scope = token_response.get("scope")
    integration.scope = scope if isinstance(scope, str) else " ".join(GOOGLE_SCOPES)
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int):
        integration.expires_at = _utcnow() + timedelta(seconds=max(0, expires_in - 60))
    else:
        integration.expires_at = _utcnow() + timedelta(minutes=50)
    integration.updated_at = _utcnow()


def ensure_google_access_token(
    session: Session,
    integration: CalendarIntegration,
) -> str:
    if (
        integration.access_token
        and integration.expires_at
        and integration.expires_at > _utcnow() + timedelta(seconds=30)
    ):
        return integration.access_token
    try:
        access_token = ensure_google_access_token_without_commit(session, integration)
    except GoogleCalendarAuthorizationError:
        # Preserve the legacy immediate-sync boundary: reconnect state must be
        # durable even though the provider request failed. Outbox delivery calls
        # the non-committing helper directly and owns its transaction instead.
        session.commit()
        raise
    session.commit()
    session.refresh(integration)
    return access_token


def ensure_google_access_token_without_commit(
    session: Session,
    integration: CalendarIntegration,
) -> str:
    """Refresh credentials under a transaction owned by the outbox worker."""

    if (
        integration.access_token
        and integration.expires_at
        and integration.expires_at > _utcnow() + timedelta(seconds=30)
    ):
        return integration.access_token

    if not integration.refresh_token:
        integration.is_connected = False
        integration.access_token = None
        integration.expires_at = None
        integration.sync_token = None
        integration.last_sync_error = GOOGLE_REAUTHORIZATION_MESSAGE
        integration.updated_at = _utcnow()
        session.add(integration)
        session.flush()
        raise GoogleCalendarAuthorizationError(
            GOOGLE_REAUTHORIZATION_MESSAGE,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    config = _assert_google_oauth_configured()
    try:
        token_response = _google_request(
            "POST",
            GOOGLE_TOKEN_URL,
            form={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "refresh_token": integration.refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except GoogleCalendarAuthorizationError as error:
        integration.is_connected = False
        integration.access_token = None
        integration.refresh_token = None
        integration.expires_at = None
        integration.sync_token = None
        integration.last_sync_error = GOOGLE_REAUTHORIZATION_MESSAGE
        integration.updated_at = _utcnow()
        session.add(integration)
        session.flush()
        raise error
    _apply_token_response(integration, token_response)
    session.add(integration)
    session.flush()
    return str(integration.access_token)


def _create_drasil_calendar(access_token: str, time_zone: str) -> dict[str, Any]:
    return _google_request(
        "POST",
        f"{GOOGLE_CALENDAR_API_BASE}/calendars",
        access_token=access_token,
        body={
            "summary": GOOGLE_CALENDAR_SUMMARY,
            "description": "Drasil-managed execution calendar.",
            "timeZone": time_zone,
        },
    )


def complete_google_oauth(
    session: Session,
    *,
    code: str,
    state: str,
) -> dict[str, Any]:
    config = _assert_google_oauth_configured()
    integration = get_google_integration(session)
    if not integration or not integration.oauth_state or integration.oauth_state != state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "type": "InvalidGoogleOAuthState",
                    "detail": "Google Calendar OAuth state did not match the pending connection.",
                }
            },
        )

    token_response = _google_request(
        "POST",
        GOOGLE_TOKEN_URL,
        form={
            "code": code,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": integration.oauth_redirect_uri or config["redirect_uri"],
            "grant_type": "authorization_code",
        },
    )
    _apply_token_response(integration, token_response)
    access_token = str(integration.access_token)
    calendar = _create_drasil_calendar(access_token, integration.time_zone)
    calendar_id = calendar.get("id")
    if not isinstance(calendar_id, str) or not calendar_id:
        raise GoogleCalendarError("Google Calendar create response did not include calendar id.")

    integration.google_calendar_id = calendar_id
    integration.google_calendar_summary = str(calendar.get("summary") or GOOGLE_CALENDAR_SUMMARY)
    integration.is_connected = True
    integration.oauth_state = None
    integration.oauth_redirect_uri = None
    integration.last_sync_error = None
    integration.updated_at = _utcnow()
    session.add(integration)
    wake_google_outbox_deliveries(session)
    session.commit()
    session.refresh(integration)
    push_all_blocks_to_google(session)
    return public_google_calendar_status(session)


def wake_google_outbox_deliveries(session: Session) -> int:
    """Wake parked Google effects after credentials become usable."""

    from app.models.agent_execution import AgentExternalSyncOutboxRecord

    now = _utcnow()
    result = session.exec(
        update(AgentExternalSyncOutboxRecord)
        .where(
            AgentExternalSyncOutboxRecord.provider == GOOGLE_PROVIDER,
            AgentExternalSyncOutboxRecord.status == "not_configured",
        )
        .values(
            status="pending",
            next_attempt_at=now,
            last_error_code=None,
            last_error_summary=None,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def disconnect_google_calendar(session: Session) -> dict[str, Any]:
    integration = get_google_integration(session)
    if integration:
        integration.is_connected = False
        integration.access_token = None
        integration.refresh_token = None
        integration.oauth_state = None
        integration.oauth_redirect_uri = None
        integration.sync_token = None
        integration.last_sync_error = None
        integration.updated_at = _utcnow()
        session.add(integration)
        session.commit()
    return public_google_calendar_status(session)


def _format_datetime_for_google(value: datetime) -> str:
    return value.isoformat()


def _task_children_by_parent(nodes: Iterable[StrategyNode]) -> dict[int | None, list[StrategyNode]]:
    children_by_parent: dict[int | None, list[StrategyNode]] = {}
    for node in nodes:
        children_by_parent.setdefault(node.parent_id, []).append(node)
    for children in children_by_parent.values():
        children.sort(key=lambda node: (node.sort_order, node.id or 0))
    return children_by_parent


def _render_task_hierarchy(task: StrategyNode, children_by_parent: dict[int | None, list[StrategyNode]]) -> str:
    lines: list[str] = []

    def visit(node: StrategyNode, depth: int) -> None:
        prefix = "  " * depth + "- "
        status = "[x]" if node.status == "done" else "[ ]"
        estimate = f" ({node.time_estimate_minutes}m)" if node.time_estimate_minutes else ""
        emoji = f"{node.emoji} " if node.emoji else ""
        lines.append(f"{prefix}{status} {emoji}{node.title}{estimate}")
        if node.id is None:
            return
        for child in children_by_parent.get(node.id, []):
            visit(child, depth + 1)

    visit(task, 0)
    return "\n".join(lines)


def google_event_payload(
    *,
    block: CalendarBlock,
    task: StrategyNode,
    category: Category,
    all_category_nodes: list[StrategyNode],
    time_zone: str,
) -> dict[str, Any]:
    children_by_parent = _task_children_by_parent(all_category_nodes)
    title = f"{task.emoji} {task.title}" if task.emoji else task.title
    if task.status == "done":
        title = f"[Done] {title}"

    hierarchy = _render_task_hierarchy(task, children_by_parent)
    description = "\n".join(
        [
            "Managed by Drasil.",
            "Drasil is the source of truth. Google time edits sync back; Google title and description edits are ignored.",
            "",
            "Task hierarchy:",
            hierarchy,
            "",
            f"Category: {category.name}",
            f"Variant: {block.variant}",
            f"Completion: {block.completion_status}",
        ]
    )

    return {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": _format_datetime_for_google(block.start_at),
            "timeZone": time_zone,
        },
        "end": {
            "dateTime": _format_datetime_for_google(block.end_at),
            "timeZone": time_zone,
        },
        "extendedProperties": {
            "private": {
                "drasilSource": "drasil",
                "drasilSyncVersion": DRASIL_SYNC_VERSION,
                "drasilBlockId": str(block.id or ""),
                "drasilTaskId": str(task.id or ""),
                "drasilCategoryId": str(category.id or ""),
                "drasilVariant": block.variant,
            }
        },
    }


def build_google_outbox_payload(
    session: Session,
    *,
    operation: str,
    entity_id: str,
    base_payload: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the exact provider payload while the local transaction is open."""

    if operation == "delete":
        remote_id = base_payload.get("remote_id")
        if not isinstance(remote_id, str) or not remote_id:
            raise GoogleOutboxPayloadError(
                "calendar delete effect is missing its durable remote identity"
            )
        return {"remote_id": remote_id}
    if operation not in {"create", "update"}:
        raise GoogleOutboxPayloadError("unsupported Google outbox operation")
    match = re.fullmatch(r"calendar_block:(\d+)", entity_id)
    if match is None:
        raise GoogleOutboxPayloadError("invalid calendar block identity")
    block = session.get(CalendarBlock, int(match.group(1)))
    if block is None:
        raise GoogleOutboxPayloadError("calendar block does not exist at enqueue time")
    task, category, category_nodes = _block_identity(session, block)
    integration = get_google_integration(session)
    configured_zone = str(_read_google_oauth_config().get("time_zone") or "UTC")
    time_zone = integration.time_zone if integration is not None else configured_zone
    event = google_event_payload(
        block=block,
        task=task,
        category=category,
        all_category_nodes=category_nodes,
        time_zone=time_zone,
    )
    return {
        "event": event,
        "remote_id": block.google_event_id,
        "local_revision": block.revision,
        "local_fingerprint": hashlib.sha256(
            json.dumps(base_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
    }


def _block_identity(
    session: Session,
    block: CalendarBlock,
) -> tuple[StrategyNode, Category, list[StrategyNode]]:
    task = session.get(StrategyNode, block.task_id)
    category = session.get(Category, block.category_id)
    if not task or not category:
        raise GoogleCalendarError("Calendar block task or category could not be resolved.")
    category_nodes = session.exec(
        select(StrategyNode).where(StrategyNode.category_id == block.category_id)
    ).all()
    return task, category, list(category_nodes)


def _integration_for_sync(session: Session) -> CalendarIntegration | None:
    integration = get_google_integration(session)
    if not integration or not integration.is_connected or not integration.google_calendar_id:
        return None
    return integration


def sync_block_to_google(session: Session, block: CalendarBlock) -> bool:
    integration = _integration_for_sync(session)
    if not integration:
        return False

    access_token = ensure_google_access_token(session, integration)
    task, category, category_nodes = _block_identity(session, block)
    payload = google_event_payload(
        block=block,
        task=task,
        category=category,
        all_category_nodes=category_nodes,
        time_zone=integration.time_zone,
    )
    calendar_id = integration.google_calendar_id
    if block.google_event_id:
        event = _google_request(
            "PATCH",
            f"{GOOGLE_CALENDAR_API_BASE}/calendars/{calendar_id}/events/{block.google_event_id}",
            access_token=access_token,
            body=payload,
        )
    else:
        event = _google_request(
            "POST",
            f"{GOOGLE_CALENDAR_API_BASE}/calendars/{calendar_id}/events",
            access_token=access_token,
            body=payload,
        )

    event_id = event.get("id")
    if isinstance(event_id, str):
        block.google_event_id = event_id
    etag = event.get("etag")
    block.google_event_etag = etag if isinstance(etag, str) else block.google_event_etag
    block.google_last_synced_at = _utcnow()
    block.google_sync_error = None
    integration.last_synced_at = block.google_last_synced_at
    integration.last_sync_error = None
    integration.updated_at = _utcnow()
    session.add(block)
    session.add(integration)
    session.commit()
    session.refresh(block)
    return True


def deliver_google_outbox_effect(
    session: Session,
    outbox: "Any",
) -> "Any":
    """Deliver one outbox effect without committing the worker transaction."""

    from app.services.agent_kernel.external_sync_outbox import ExternalDeliveryResult
    from app.models.agent_execution import AgentExternalSyncOutboxRecord

    try:
        if outbox.provider != GOOGLE_PROVIDER:
            raise GoogleOutboxPayloadError("unsupported external calendar provider")
        if outbox.operation not in {"create", "update", "delete"}:
            raise GoogleOutboxPayloadError("unsupported external calendar operation")
        if outbox.entity_kind != "calendar_block":
            raise GoogleOutboxPayloadError("unsupported external calendar entity kind")
        identity_match = re.fullmatch(r"calendar_block:(\d+)", str(outbox.entity_id))
        if identity_match is None:
            raise GoogleOutboxPayloadError("invalid calendar block identity")
        block_id = int(identity_match.group(1))
        try:
            payload = json.loads(outbox.payload_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise GoogleOutboxPayloadError("invalid canonical outbox JSON") from error
        if not isinstance(payload, dict):
            raise GoogleOutboxPayloadError("canonical outbox payload must be an object")

        integration = _integration_for_sync(session)
        if integration is None:
            return ExternalDeliveryResult(
                status="not_configured",
                error_code="SYNC_NOT_CONFIGURED",
                error_summary="Connect Google Calendar to deliver this change.",
            )
        access_token = ensure_google_access_token_without_commit(session, integration)
        calendar_id = str(integration.google_calendar_id)
        refreshed_after_401 = False

        def provider_request(
            method: str,
            url: str,
            *,
            body: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            nonlocal access_token, refreshed_after_401
            try:
                return _google_request(
                    method,
                    url,
                    access_token=access_token,
                    body=body,
                )
            except GoogleCalendarError as error:
                if error.status_code != 401 or refreshed_after_401:
                    raise
                refreshed_after_401 = True
                integration.access_token = None
                integration.expires_at = None
                access_token = ensure_google_access_token_without_commit(
                    session,
                    integration,
                )
                return _google_request(
                    method,
                    url,
                    access_token=access_token,
                    body=body,
                )

        if outbox.operation == "delete":
            remote_id = payload.get("remote_id") or outbox.remote_id
            if not isinstance(remote_id, str) or not remote_id:
                raise GoogleOutboxPayloadError(
                    "calendar delete effect is missing its durable remote identity"
                )
            try:
                provider_request(
                    "DELETE",
                    f"{GOOGLE_CALENDAR_API_BASE}/calendars/{calendar_id}/events/{remote_id}",
                )
            except GoogleCalendarError as error:
                if error.status_code not in {404, 410}:
                    raise
            integration.last_synced_at = _utcnow()
            integration.last_sync_error = None
            integration.updated_at = _utcnow()
            session.add(integration)
            session.flush()
            return ExternalDeliveryResult(status="succeeded", remote_id=remote_id)

        raw_event = payload.get("event")
        if not isinstance(raw_event, dict):
            raise GoogleOutboxPayloadError(
                "create/update effect is missing its canonical event payload"
            )
        event_payload = json.loads(json.dumps(raw_event))
        prior_remote = session.exec(
            select(AgentExternalSyncOutboxRecord)
            .where(
                AgentExternalSyncOutboxRecord.provider == GOOGLE_PROVIDER,
                AgentExternalSyncOutboxRecord.entity_kind == "calendar_block",
                AgentExternalSyncOutboxRecord.entity_id == outbox.entity_id,
                AgentExternalSyncOutboxRecord.status == "succeeded",
                AgentExternalSyncOutboxRecord.remote_id != None,  # noqa: E711
                AgentExternalSyncOutboxRecord.outbox_id != outbox.outbox_id,
                AgentExternalSyncOutboxRecord.created_at <= outbox.created_at,
            )
            .order_by(AgentExternalSyncOutboxRecord.created_at.desc())
        ).first()
        prior_remote_id = prior_remote.remote_id if prior_remote is not None else None
        deterministic_id = deterministic_google_event_id(outbox.semantic_key)

        def create_event() -> tuple[dict[str, Any], str]:
            requested_id = deterministic_id
            event_payload["id"] = requested_id
            try:
                created = provider_request(
                    "POST",
                    f"{GOOGLE_CALENDAR_API_BASE}/calendars/{calendar_id}/events",
                    body=event_payload,
                )
            except GoogleCalendarError as error:
                if error.status_code != 409:
                    raise
                event_payload.pop("id", None)
                created = provider_request(
                    "PATCH",
                    f"{GOOGLE_CALENDAR_API_BASE}/calendars/{calendar_id}/events/{requested_id}",
                    body=event_payload,
                )
            return created, requested_id

        if outbox.operation == "create":
            event, remote_id = create_event()
        else:
            remote_id = payload.get("remote_id") or outbox.remote_id or prior_remote_id
            if not isinstance(remote_id, str) or not remote_id:
                raise GoogleOutboxPayloadError(
                    "calendar update effect is missing its durable remote identity"
                )
            try:
                event = provider_request(
                    "PATCH",
                    f"{GOOGLE_CALENDAR_API_BASE}/calendars/{calendar_id}/events/{remote_id}",
                    body=event_payload,
                )
            except GoogleCalendarError as error:
                if error.status_code not in {404, 410}:
                    raise
                event, remote_id = create_event()

        remote_id = str(event.get("id") or remote_id)
        remote_version = str(event.get("etag") or "") or None
        block = session.get(CalendarBlock, block_id)
        extended = event_payload.get("extendedProperties")
        private = extended.get("private", {}) if isinstance(extended, dict) else {}
        if not isinstance(private, dict):
            private = {}
        if (
            block is not None
            and str(block.task_id) == str(private.get("drasilTaskId"))
            and str(block.category_id) == str(private.get("drasilCategoryId"))
        ):
            block.google_event_id = remote_id
            block.google_event_etag = remote_version
            block.google_last_synced_at = _utcnow()
            block.google_sync_error = None
            session.add(block)
        integration.last_synced_at = _utcnow()
        integration.last_sync_error = None
        integration.updated_at = _utcnow()
        session.add(integration)
        session.flush()
        return ExternalDeliveryResult(
            status="succeeded",
            remote_id=remote_id,
            remote_version=remote_version,
        )
    except GoogleOutboxPayloadError:
        return ExternalDeliveryResult(
            status="permanent_failure",
            error_code="SYNC_INVALID_PAYLOAD",
            error_summary="The stored calendar delivery payload is invalid.",
        )
    except GoogleCalendarAuthorizationError:
        return ExternalDeliveryResult(
            status="not_configured",
            error_code="SYNC_NOT_CONFIGURED",
            error_summary=GOOGLE_REAUTHORIZATION_MESSAGE,
        )
    except HTTPException:
        return ExternalDeliveryResult(
            status="not_configured",
            error_code="SYNC_NOT_CONFIGURED",
            error_summary="Google Calendar authorization or configuration must be repaired.",
        )
    except GoogleCalendarError as error:
        retryable_403_reasons = {
            "ratelimitexceeded",
            "userratelimitexceeded",
            "quotaexceeded",
            "backenderror",
        }
        reason = str(error.reason or "").replace("_", "").lower()
        retryable = (
            error.status_code is None
            or error.status_code in {408, 409, 425, 429}
            or (error.status_code is not None and error.status_code >= 500)
            or (error.status_code == 403 and reason in retryable_403_reasons)
        )
        integration = get_google_integration(session)
        if integration is not None:
            integration.last_sync_error = (
                "Google Calendar delivery will be retried."
                if retryable
                else "Google Calendar rejected this change."
            )
            integration.updated_at = _utcnow()
            session.add(integration)
            session.flush()
        return ExternalDeliveryResult(
            status="retryable_failure" if retryable else "permanent_failure",
            error_code="SYNC_RETRYABLE" if retryable else "SYNC_PERMANENT",
            error_summary=(
                "Google Calendar delivery will be retried."
                if retryable
                else "Google Calendar rejected this change."
            ),
        )


def try_sync_block_to_google(session: Session, block: CalendarBlock) -> None:
    try:
        sync_block_to_google(session, block)
    except (GoogleCalendarError, HTTPException) as error:
        block.google_sync_error = str(error)
        integration = get_google_integration(session)
        if integration:
            integration.last_sync_error = str(error)
            integration.updated_at = _utcnow()
            session.add(integration)
        session.add(block)
        session.commit()


def delete_google_event_by_id(session: Session, google_event_id: str) -> str | None:
    integration = _integration_for_sync(session)
    if not integration or not google_event_id:
        return None
    try:
        access_token = ensure_google_access_token(session, integration)
        _google_request(
            "DELETE",
            (
                f"{GOOGLE_CALENDAR_API_BASE}/calendars/"
                f"{integration.google_calendar_id}/events/{google_event_id}"
            ),
            access_token=access_token,
        )
    except GoogleCalendarError as error:
        # Google treats a missing/cancelled event as already deleted. Replayed
        # approval sync must remain idempotent after a prior successful delete.
        if error.status_code in {404, 410}:
            return None
        integration.last_sync_error = str(error)
        integration.updated_at = _utcnow()
        session.add(integration)
        session.commit()
        return str(error)
    except HTTPException as error:
        integration.last_sync_error = str(error)
        integration.updated_at = _utcnow()
        session.add(integration)
        session.commit()
        return str(error)
    return None


def delete_google_event_for_block(session: Session, block: CalendarBlock) -> str | None:
    return delete_google_event_by_id(session, str(block.google_event_id or ""))


def _google_sync_zone(time_zone: str) -> ZoneInfo:
    try:
        return ZoneInfo(time_zone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _parse_google_event_time(value: dict[str, Any] | None, time_zone: str) -> datetime | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("dateTime")
    if not isinstance(raw, str) or not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=None)
    return parsed.astimezone(_google_sync_zone(time_zone)).replace(tzinfo=None)


def pull_google_time_edits(session: Session) -> dict[str, int]:
    integration = _integration_for_sync(session)
    if not integration:
        return {"updated": 0, "scanned": 0}

    access_token = ensure_google_access_token(session, integration)
    params = urlencode(
        {
            "singleEvents": "true",
            "showDeleted": "true",
            "privateExtendedProperty": "drasilSource=drasil",
        }
    )
    response = _google_request(
        "GET",
        f"{GOOGLE_CALENDAR_API_BASE}/calendars/{integration.google_calendar_id}/events?{params}",
        access_token=access_token,
    )
    items = response.get("items")
    events = items if isinstance(items, list) else []
    updated = 0
    for event in events:
        if not isinstance(event, dict) or event.get("status") == "cancelled":
            continue
        private_props = (
            event.get("extendedProperties", {}).get("private", {})
            if isinstance(event.get("extendedProperties"), dict)
            else {}
        )
        block_id_raw = private_props.get("drasilBlockId") if isinstance(private_props, dict) else None
        try:
            block_id = int(str(block_id_raw))
        except (TypeError, ValueError):
            continue
        block = session.get(CalendarBlock, block_id)
        if not block:
            continue
        start_at = _parse_google_event_time(event.get("start"), integration.time_zone)
        end_at = _parse_google_event_time(event.get("end"), integration.time_zone)
        if not start_at or not end_at or end_at <= start_at:
            continue
        if block.start_at == start_at and block.end_at == end_at:
            continue
        block.start_at = start_at
        block.end_at = end_at
        block.google_event_id = str(event.get("id") or block.google_event_id or "")
        etag = event.get("etag")
        if isinstance(etag, str):
            block.google_event_etag = etag
        block.google_last_synced_at = _utcnow()
        block.google_sync_error = None
        session.add(block)
        updated += 1

    integration.last_synced_at = _utcnow()
    integration.last_sync_error = None
    integration.updated_at = _utcnow()
    next_sync_token = response.get("nextSyncToken")
    if isinstance(next_sync_token, str):
        integration.sync_token = next_sync_token
    session.add(integration)
    session.commit()
    return {"updated": updated, "scanned": len(events)}


def push_all_blocks_to_google(session: Session) -> dict[str, int]:
    integration = _integration_for_sync(session)
    if not integration:
        return {"pushed": 0, "failed": 0}

    from app.models.agent_execution import AgentExternalSyncOutboxRecord

    native_pending_entities = set(
        session.exec(
            select(AgentExternalSyncOutboxRecord.entity_id).where(
                AgentExternalSyncOutboxRecord.provider == GOOGLE_PROVIDER,
                AgentExternalSyncOutboxRecord.entity_kind == "calendar_block",
                AgentExternalSyncOutboxRecord.status.in_(
                    ["pending", "not_configured", "delivering"]
                ),
            )
        ).all()
    )
    blocks = session.exec(select(CalendarBlock).order_by(CalendarBlock.start_at)).all()
    pushed = 0
    failed = 0
    for block in blocks:
        if f"calendar_block:{block.id}" in native_pending_entities:
            continue
        try:
            if sync_block_to_google(session, block):
                pushed += 1
        except (GoogleCalendarError, HTTPException) as error:
            failed += 1
            block.google_sync_error = str(error)
            integration.last_sync_error = str(error)
            integration.updated_at = _utcnow()
            session.add(block)
            session.add(integration)
            session.commit()
    return {"pushed": pushed, "failed": failed}
