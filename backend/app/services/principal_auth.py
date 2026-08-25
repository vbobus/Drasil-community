"""Authenticated local principal derived from Drasil's configured AI credential."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from fastapi import HTTPException

from app.services.codex_auth import read_auth_snapshot
from app.services.account_auth import active_account
from app.services.strategist_provider import get_effective_provider_settings


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    principal_id: str
    provider: str


def _fingerprint(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def resolve_authenticated_principal() -> AuthenticatedPrincipal | None:
    """Resolve the server-authenticated provider identity without exposing secrets."""

    if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
        return AuthenticatedPrincipal(principal_id="test:local", provider="test")
    account = active_account(required=False)
    if account is not None:
        return AuthenticatedPrincipal(
            principal_id=f"account:{account.id}",
            provider="drasil-account",
        )
    snapshot = read_auth_snapshot()
    if snapshot.get("isAuthenticated"):
        identity = str(snapshot.get("accountId") or snapshot.get("email") or "").strip()
        if identity:
            return AuthenticatedPrincipal(
                principal_id=_fingerprint("chatgpt", identity),
                provider="chatgpt",
            )
    settings = get_effective_provider_settings()
    if settings.get("activeProvider") == "openai-api":
        api_key = str(settings.get("openAiApiKey") or "").strip()
        if api_key:
            return AuthenticatedPrincipal(
                principal_id=_fingerprint("openai-api", api_key),
                provider="openai-api",
            )
    return None


def require_authenticated_principal() -> AuthenticatedPrincipal:
    principal = resolve_authenticated_principal()
    if principal is not None:
        return principal
    raise HTTPException(
        status_code=401,
        detail="Authenticate the active Strategist provider before using review actions.",
    )
