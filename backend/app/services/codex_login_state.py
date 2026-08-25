"""
Sanitized local Codex authentication-state helpers.

The Codex CLI can sometimes report "Not logged in" from `codex login status`
even when `codex exec` can successfully use the persisted ChatGPT OAuth
tokens. These helpers inspect only the shape of the local auth file so callers
can avoid blocking a valid one-time login without ever exposing token values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.services.account_auth import account_private_path, active_account


def get_codex_auth_file_path() -> Path:
    if active_account(required=False) is None and os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
        configured_home = os.getenv("CODEX_HOME", "").strip()
        codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
        return codex_home / "auth.json"
    return account_private_path(".codex/auth.json")


def load_codex_auth_file(auth_path: Path | None = None) -> dict[str, Any]:
    path = auth_path or get_codex_auth_file_path()
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_codex_auth_file_snapshot(auth_path: Path | None = None) -> dict[str, Any]:
    path = auth_path or get_codex_auth_file_path()
    auth = load_codex_auth_file(path)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    auth_mode = auth.get("auth_mode") if isinstance(auth.get("auth_mode"), str) else None
    has_chatgpt_token = any(
        bool(tokens.get(token_key))
        for token_key in ("access_token", "refresh_token", "id_token")
    )
    has_api_key = bool(auth.get("OPENAI_API_KEY") or auth.get("openai_api_key"))
    is_chatgpt_auth = auth_mode == "chatgpt" and has_chatgpt_token

    last_modified: float | None = None
    try:
        last_modified = path.stat().st_mtime if path.exists() else None
    except OSError:
        last_modified = None

    return {
        "exists": path.exists(),
        "authMode": auth_mode,
        "hasChatGptTokens": bool(is_chatgpt_auth),
        "hasApiKey": has_api_key,
        "isUsable": bool(is_chatgpt_auth or has_api_key),
        "lastModified": last_modified,
    }


def has_usable_codex_auth_file(auth_path: Path | None = None) -> bool:
    return bool(read_codex_auth_file_snapshot(auth_path).get("isUsable"))
