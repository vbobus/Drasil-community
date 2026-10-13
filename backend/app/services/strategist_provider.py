"""
Local strategist provider settings for Codex CLI vs BYOK OpenAI API.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal, TypedDict

from fastapi import HTTPException

from app.core.codex_cli import find_codex_cli_path
from app.services.account_auth import account_private_path, account_process_environment
from app.core.env import load_local_env

ProviderId = Literal["codex-cli", "openai-api"]
VALID_PROVIDER_IDS: set[str] = {"codex-cli", "openai-api"}
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
CODEX_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_MODEL_CATALOG_TTL_SECONDS = 300
LEGACY_CODEX_MODEL_ALIASES = {
    "gpt-5.1-codex-mini": DEFAULT_CODEX_MODEL,
    "codex-mini-latest": DEFAULT_CODEX_MODEL,
    "gpt-5.4-mini": DEFAULT_CODEX_MODEL,
}
_SETTINGS_LOCK = threading.Lock()
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class StoredProviderSettings(TypedDict, total=False):
    activeProvider: ProviderId
    codexModel: str
    codexReasoningEffort: str
    openAiApiKey: str
    openAiModel: str


def _fallback_codex_model_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": DEFAULT_CODEX_MODEL,
            "displayName": "GPT-5.6 Sol",
            "description": "High-reliability Codex model.",
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": ["low", "medium", "high", "xhigh"],
        },
    ]


def _parse_codex_model_catalog(payload: Any) -> list[dict[str, Any]]:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []

    normalized: list[tuple[int, dict[str, Any]]] = []
    for model in models:
        if not isinstance(model, dict) or model.get("visibility") != "list":
            continue
        model_id = str(model.get("slug") or "").strip()
        if not model_id:
            continue
        supported_levels = model.get("supported_reasoning_levels")
        efforts: list[str] = []
        if isinstance(supported_levels, list):
            for level in supported_levels:
                effort = (
                    str(level.get("effort") or "").strip().lower()
                    if isinstance(level, dict)
                    else ""
                )
                if effort in CODEX_REASONING_EFFORTS and effort not in efforts:
                    efforts.append(effort)
        default_effort = str(model.get("default_reasoning_level") or "").lower()
        if default_effort not in efforts:
            default_effort = efforts[0] if efforts else ""
        try:
            priority = int(model.get("priority", 9999))
        except (TypeError, ValueError):
            priority = 9999
        normalized.append(
            (
                priority,
                {
                    "id": model_id,
                    "displayName": str(model.get("display_name") or model_id),
                    "description": str(model.get("description") or ""),
                    "defaultReasoningEffort": default_effort or None,
                    "supportedReasoningEfforts": efforts,
                },
            ),
        )

    normalized.sort(key=lambda item: (item[0], item[1]["id"]))
    return [model for _, model in normalized]


def get_codex_model_catalog(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return the visible model inventory exposed by the authenticated Codex CLI."""
    from app.db.hosted import hosted_runtime
    if hosted_runtime():
        from app.services.codex_sandbox import enabled
        return _fallback_codex_model_catalog() if enabled() else []
    now = time.monotonic()
    codex_executable = find_codex_cli_path()
    executable_key = str(codex_executable or "").lower()
    from app.services.account_auth import active_account

    account = active_account(required=False)
    cache_key = f"{account.id if account is not None else 'legacy'}:{executable_key}"
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(cache_key)
        if (
            not force_refresh
            and cached is not None
            and now - cached[0] < CODEX_MODEL_CATALOG_TTL_SECONDS
        ):
            return [dict(model) for model in cached[1]]

        catalog: list[dict[str, Any]] = []
        if codex_executable:
            try:
                result = subprocess.run(
                    [str(codex_executable), "debug", "models"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    env=account_process_environment(),
                    check=False,
                )
                if result.returncode == 0:
                    catalog = _parse_codex_model_catalog(json.loads(result.stdout))
            except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
                catalog = []

        if not catalog:
            catalog = _fallback_codex_model_catalog()
        _CATALOG_CACHE[cache_key] = (now, catalog)
        return [dict(model) for model in catalog]


def _get_settings_path() -> Path:
    return account_private_path(".strategist-provider.json")


def _load_raw_settings() -> StoredProviderSettings:
    from app.db.hosted import hosted_storage
    if hosted_storage():
        from app.services.private_settings import load_settings
        return load_settings("provider")
    settings_path = _get_settings_path()
    if not settings_path.exists():
        return {}

    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _write_raw_settings(settings: StoredProviderSettings) -> None:
    from app.db.hosted import hosted_storage
    if hosted_storage():
        from app.services.private_settings import save_settings
        save_settings("provider", settings)
        return
    settings_path = _get_settings_path()
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _normalize_provider(value: str | None) -> ProviderId:
    from app.db.hosted import hosted_runtime
    from app.services.codex_sandbox import enabled
    if hosted_runtime() and not enabled():
        return "openai-api"
    if value in VALID_PROVIDER_IDS:
        return value  # type: ignore[return-value]
    return "codex-cli"


def _mask_api_key(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:7]}...{value[-4:]}"


def _normalize_codex_model(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return LEGACY_CODEX_MODEL_ALIASES.get(normalized, normalized)


def _normalize_codex_reasoning_effort(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized if normalized in CODEX_REASONING_EFFORTS else None


def get_effective_provider_settings() -> dict[str, Any]:
    load_local_env()
    raw_settings = _load_raw_settings()

    active_provider = _normalize_provider(
        raw_settings.get("activeProvider") or os.getenv("STRATEGIST_PROVIDER"),
    )
    openai_api_key = (
        raw_settings.get("openAiApiKey")
        or os.getenv("OPENAI_API_KEY")
        or None
    )
    if "codexModel" in raw_settings:
        codex_model = _normalize_codex_model(raw_settings.get("codexModel"))
    else:
        codex_model = _normalize_codex_model(os.getenv("STRATEGIST_CODEX_MODEL"))
    if "codexReasoningEffort" in raw_settings:
        codex_reasoning_effort = _normalize_codex_reasoning_effort(
            raw_settings.get("codexReasoningEffort"),
        )
    else:
        codex_reasoning_effort = _normalize_codex_reasoning_effort(
            os.getenv("STRATEGIST_CODEX_REASONING_EFFORT"),
        )
    openai_model = (
        raw_settings.get("openAiModel")
        or os.getenv("STRATEGIST_OPENAI_MODEL")
        or DEFAULT_OPENAI_MODEL
    ).strip() or DEFAULT_OPENAI_MODEL

    return {
        "activeProvider": active_provider,
        "codexModel": codex_model,
        "codexReasoningEffort": codex_reasoning_effort,
        "openAiApiKey": openai_api_key,
        "openAiModel": openai_model,
        "isOpenAiKeyConfigured": bool(openai_api_key),
        "maskedOpenAiKey": _mask_api_key(openai_api_key),
    }


def get_public_provider_settings(*, force_catalog_refresh: bool = False) -> dict[str, Any]:
    from app.db.hosted import hosted_runtime
    from app.services.codex_sandbox import enabled
    effective = get_effective_provider_settings()
    active_provider = effective["activeProvider"]
    model_catalog = get_codex_model_catalog(force_refresh=force_catalog_refresh)

    return {
        "activeProvider": active_provider,
        "supportsChatGptLogin": not hosted_runtime() or enabled(),
        "chatGptHosting": "sandbox" if enabled() else "local",
        "activeProviderLabel": (
            "ChatGPT login"
            if active_provider == "codex-cli"
            else "BYOK (OpenAI API)"
        ),
        "codexModel": effective["codexModel"],
        "codexModelOptions": [model["id"] for model in model_catalog],
        "codexModelCatalog": model_catalog,
        "codexReasoningEffort": effective["codexReasoningEffort"],
        "isOpenAiKeyConfigured": effective["isOpenAiKeyConfigured"],
        "maskedOpenAiKey": effective["maskedOpenAiKey"],
        "openAiModel": effective["openAiModel"],
    }


def save_provider_settings(
    *,
    active_provider: str,
    codex_model: str | None,
    codex_reasoning_effort: str | None,
    openai_api_key: str | None,
    openai_model: str | None,
    clear_openai_api_key: bool,
) -> dict[str, Any]:
    from app.db.hosted import hosted_runtime
    from app.services.codex_sandbox import enabled
    if hosted_runtime() and not enabled() and active_provider != "openai-api":
        raise HTTPException(status_code=400, detail="Hosted Drasil uses an OpenAI API key. ChatGPT CLI login is available in local installations.")
    normalized_provider = _normalize_provider(active_provider)
    next_codex_model = _normalize_codex_model(codex_model) or ""
    next_codex_reasoning_effort = _normalize_codex_reasoning_effort(
        codex_reasoning_effort,
    )
    if codex_reasoning_effort and not next_codex_reasoning_effort:
        raise HTTPException(status_code=400, detail="Choose a supported thinking level.")

    selected_model = next(
        (
            model
            for model in get_codex_model_catalog()
            if model["id"] == next_codex_model
        ),
        None,
    )
    if (
        selected_model
        and next_codex_reasoning_effort
        and next_codex_reasoning_effort
        not in selected_model["supportedReasoningEfforts"]
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{selected_model['displayName']} does not support the selected "
                "thinking level."
            ),
        )
    next_model = (openai_model or "").strip() or DEFAULT_OPENAI_MODEL

    with _SETTINGS_LOCK:
        raw_settings = _load_raw_settings()
        existing_key = (
            raw_settings.get("openAiApiKey")
            or os.getenv("OPENAI_API_KEY")
            or None
        )

        next_key = existing_key
        if clear_openai_api_key:
            next_key = None
            raw_settings.pop("openAiApiKey", None)
        elif openai_api_key is not None:
            trimmed_key = openai_api_key.strip()
            next_key = trimmed_key or None
            if trimmed_key:
                raw_settings["openAiApiKey"] = trimmed_key

        if normalized_provider == "openai-api" and not next_key:
            raise HTTPException(
                status_code=400,
                detail="Provide an OpenAI API key before switching Strategist to BYOK.",
            )

        raw_settings["activeProvider"] = normalized_provider
        raw_settings["codexModel"] = next_codex_model
        if next_codex_reasoning_effort:
            raw_settings["codexReasoningEffort"] = next_codex_reasoning_effort
        else:
            raw_settings.pop("codexReasoningEffort", None)
        raw_settings["openAiModel"] = next_model

        if clear_openai_api_key and normalized_provider == "openai-api" and not next_key:
            raw_settings["activeProvider"] = "codex-cli"

        _write_raw_settings(raw_settings)

    return get_public_provider_settings()
