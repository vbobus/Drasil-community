"""
Local Codex / ChatGPT authentication helpers for the MVP settings flow.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException

from app.core.codex_cli import find_codex_cli_path, require_codex_cli_path
from app.services.account_auth import account_process_environment, active_account
from app.services.codex_login_state import (
    get_codex_auth_file_path,
    has_usable_codex_auth_file,
    load_codex_auth_file,
    read_codex_auth_file_snapshot,
)

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4,8}-[A-Z0-9]{4,8}\b")


@dataclass
class DeviceAuthSession:
    session_id: str
    process: subprocess.Popen[str]
    started_at: float
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    verification_uri: Optional[str] = None
    user_code: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_ACTIVE_SESSIONS: dict[str, DeviceAuthSession] = {}
_ACTIVE_SESSION_LOCK = threading.Lock()


def _active_session_key() -> str:
    account = active_account(required=False)
    return account.id if account is not None else "legacy"


def strip_ansi(value: str) -> str:
    return ANSI_ESCAPE_RE.sub("", value)


def get_codex_path() -> str:
    return require_codex_cli_path("ChatGPT authentication")


def decode_jwt_payload(token: Optional[str]) -> dict[str, Any]:
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_auth_file() -> dict[str, Any]:
    return load_codex_auth_file(get_codex_auth_file_path())


def read_auth_snapshot() -> dict[str, Any]:
    auth = load_auth_file()
    file_snapshot = read_codex_auth_file_snapshot()
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    id_token_payload = decode_jwt_payload(tokens.get("id_token"))
    auth_payload = id_token_payload.get("https://api.openai.com/auth")
    auth_payload = auth_payload if isinstance(auth_payload, dict) else {}

    return {
        "isAuthenticated": bool(file_snapshot.get("isUsable")),
        "authMode": auth.get("auth_mode"),
        "planType": auth_payload.get("chatgpt_plan_type"),
        "email": id_token_payload.get("email"),
        "name": id_token_payload.get("name"),
        "accountId": tokens.get("account_id"),
        "lastRefresh": auth.get("last_refresh"),
    }


def read_codex_login_snapshot() -> dict[str, Any]:
    codex_path = find_codex_cli_path()
    if codex_path is None:
        return {
            "isAuthenticated": has_usable_codex_auth_file(),
            "authMode": None,
            "providerStatus": "missing",
        }

    try:
        result = subprocess.run(
            [str(codex_path), "login", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=account_process_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        if has_usable_codex_auth_file():
            return {
                "isAuthenticated": True,
                "authMode": read_codex_auth_file_snapshot().get("authMode"),
                "providerStatus": "ready",
                "statusWarning": "Codex login status failed, but a usable local auth file is present.",
            }
        return {
            "isAuthenticated": False,
            "authMode": None,
            "providerStatus": "error",
        }

    output = f"{result.stdout}\n{result.stderr}".strip()
    is_logged_in = result.returncode == 0 and "Logged in" in output
    auth_mode = "chatgpt" if "ChatGPT" in output else None
    if not is_logged_in and has_usable_codex_auth_file():
        file_snapshot = read_codex_auth_file_snapshot()
        return {
            "isAuthenticated": True,
            "authMode": file_snapshot.get("authMode"),
            "providerStatus": "ready",
            "statusWarning": "Codex login status reported Not logged in, but a usable local auth file is present.",
        }
    return {
        "isAuthenticated": is_logged_in,
        "authMode": auth_mode,
        "providerStatus": "ready",
    }


def _append_session_output(session: DeviceAuthSession, target: list[str], raw_line: str) -> None:
    cleaned = strip_ansi(raw_line).strip()
    if not cleaned:
        return
    with session.lock:
        target.append(cleaned)
        if session.verification_uri is None:
            match = DEVICE_URL_RE.search(cleaned)
            if match:
                session.verification_uri = match.group(0)
        if session.user_code is None:
            match = DEVICE_CODE_RE.search(cleaned)
            if match:
                session.user_code = match.group(0)


def _stream_reader(
    session: DeviceAuthSession,
    stream: Any,
    target: list[str],
) -> None:
    try:
        for raw_line in iter(stream.readline, ""):
            _append_session_output(session, target, raw_line)
    finally:
        stream.close()


def _monitor_session(session: DeviceAuthSession) -> None:
    return_code = session.process.wait()
    with session.lock:
        if session.status == "cancelled":
            return
        if return_code == 0:
            session.status = "completed"
        else:
            session.status = "failed"
            combined = " ".join(session.stderr_lines + session.stdout_lines).strip()
            session.error = combined or "Codex login did not complete successfully."


def cleanup_stale_session() -> None:
    key = _active_session_key()
    with _ACTIVE_SESSION_LOCK:
        session = _ACTIVE_SESSIONS.get(key)
        if session is None:
            return
        if session.status != "pending":
            _ACTIVE_SESSIONS.pop(key, None)
            return
        if time.time() - session.started_at > 20 * 60:
            try:
                session.process.terminate()
            except OSError:
                pass
            session.status = "cancelled"
            session.error = "Login session expired."
            _ACTIVE_SESSIONS.pop(key, None)


def get_active_session() -> DeviceAuthSession | None:
    cleanup_stale_session()
    with _ACTIVE_SESSION_LOCK:
        return _ACTIVE_SESSIONS.get(_active_session_key())


def serialize_session(session: DeviceAuthSession) -> dict[str, Any]:
    with session.lock:
        return {
            "sessionId": session.session_id,
            "status": session.status,
            "verificationUri": session.verification_uri,
            "userCode": session.user_code,
            "error": session.error,
        }


def get_auth_status() -> dict[str, Any]:
    snapshot = read_auth_snapshot()
    codex_status = read_codex_login_snapshot()
    session = get_active_session()
    is_authenticated = snapshot.get("isAuthenticated") or codex_status.get("isAuthenticated", False)
    auth_mode = snapshot.get("authMode") or codex_status.get("authMode")

    return {
        "provider": "codex-cli",
        **snapshot,
        "isAuthenticated": bool(is_authenticated),
        "authMode": auth_mode,
        "providerStatus": codex_status.get("providerStatus", "ready"),
        "pendingSession": serialize_session(session) if session is not None else None,
    }


def start_device_auth() -> dict[str, Any]:
    current_status = get_auth_status()

    if current_status.get("isAuthenticated"):
        return current_status

    session_key = _active_session_key()
    with _ACTIVE_SESSION_LOCK:
        existing_session = _ACTIVE_SESSIONS.get(session_key)
        if existing_session is not None and existing_session.status == "pending":
            return {
                **current_status,
                "pendingSession": serialize_session(existing_session),
            }

        codex_path = get_codex_path()
        try:
            process = subprocess.Popen(
                [codex_path, "login", "--device-auth"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=account_process_environment(),
            )
        except OSError as error:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to launch Codex login: {error}",
            ) from error
        session = DeviceAuthSession(
            session_id=f"codex-auth-{int(time.time() * 1000)}",
            process=process,
            started_at=time.time(),
        )
        _ACTIVE_SESSIONS[session_key] = session

        stdout_thread = threading.Thread(
            target=_stream_reader,
            args=(session, process.stdout, session.stdout_lines),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_stream_reader,
            args=(session, process.stderr, session.stderr_lines),
            daemon=True,
        )
        monitor_thread = threading.Thread(target=_monitor_session, args=(session,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        monitor_thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        with session.lock:
            if session.verification_uri and session.user_code:
                break
            if session.status == "failed":
                raise HTTPException(
                    status_code=502,
                    detail=session.error or "Failed to initialize ChatGPT login.",
                )
        time.sleep(0.15)

    payload = serialize_session(session)
    if not payload["verificationUri"] or not payload["userCode"]:
        raise HTTPException(
            status_code=502,
            detail="Could not initialize ChatGPT login instructions from Codex.",
        )

    return {
        **get_auth_status(),
        "pendingSession": payload,
    }


def logout_codex() -> dict[str, Any]:
    session_key = _active_session_key()
    session = get_active_session()
    if session is not None and session.status == "pending":
        try:
            session.process.terminate()
        except OSError:
            pass
        session.status = "cancelled"
        session.error = "Login cancelled by user."

    codex_path = get_codex_path()
    result = subprocess.run(
        [codex_path, "logout"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=account_process_environment(),
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise HTTPException(
            status_code=502,
            detail=detail or "Failed to disconnect ChatGPT authentication.",
        )

    with _ACTIVE_SESSION_LOCK:
        _ACTIVE_SESSIONS.pop(session_key, None)

    return get_auth_status()
