"""Checked-in, replaceable provider/model compatibility matrix for Agent Kernel gates."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Literal, Mapping

from app.services.strategist_provider import DEFAULT_CODEX_MODEL, DEFAULT_OPENAI_MODEL


ProviderTransport = Literal["openai_api", "codex_app_server"]


class UnsupportedProviderModel(ValueError):
    pass


class UnsupportedReasoningEffort(ValueError):
    pass


@dataclass(frozen=True)
class ProviderMatrixEntry:
    provider: ProviderTransport
    model: str
    reasoning_efforts: tuple[str, ...]
    strict_tools: bool = True
    streaming: bool = True
    required: bool = True

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "ProviderMatrixEntry":
        allowed = {
            "provider", "model", "reasoningEfforts", "strictTools", "streaming", "required",
        }
        if set(value) - allowed:
            raise ValueError("provider matrix entry contains unsupported fields")
        provider = value.get("provider")
        if provider not in {"openai_api", "codex_app_server"}:
            raise ValueError("unsupported provider transport")
        model = _identifier(value.get("model"), "model")
        raw_efforts = value.get("reasoningEfforts", [])
        if not isinstance(raw_efforts, list) or len(raw_efforts) > 8:
            raise ValueError("reasoningEfforts must be a bounded list")
        efforts = tuple(_identifier(item, "reasoning effort") for item in raw_efforts)
        if len(efforts) != len(set(efforts)):
            raise ValueError("reasoning efforts must be unique")
        return cls(
            provider=provider,
            model=model,
            reasoning_efforts=efforts,
            strict_tools=value.get("strictTools", True) is True,
            streaming=value.get("streaming", True) is True,
            required=value.get("required", True) is True,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoningEfforts": list(self.reasoning_efforts),
            "strictTools": self.strict_tools,
            "streaming": self.streaming,
            "required": self.required,
        }


DEFAULT_SUPPORTED_MODEL_MATRIX = (
    ProviderMatrixEntry(
        provider="openai_api",
        model=DEFAULT_OPENAI_MODEL,
        reasoning_efforts=(),
        required=False,
    ),
    ProviderMatrixEntry(
        provider="codex_app_server",
        model=DEFAULT_CODEX_MODEL,
        reasoning_efforts=("low", "medium", "high"),
    ),
)


def load_supported_model_matrix(
    environ: Mapping[str, str] | None = None,
) -> tuple[ProviderMatrixEntry, ...]:
    """Load an intentional deployment override or the checked-in supported matrix.

    Invalid overrides fail closed instead of silently changing the declared support set.
    """
    source = environ if environ is not None else os.environ
    raw = source.get("AGENT_KERNEL_SUPPORTED_MODEL_MATRIX_JSON", "").strip()
    if not raw:
        return DEFAULT_SUPPORTED_MODEL_MATRIX
    try:
        payload = json.loads(raw)
        if not isinstance(payload, list) or not payload or len(payload) > 32:
            raise ValueError("provider matrix must be a non-empty bounded list")
        entries = tuple(ProviderMatrixEntry.from_payload(item) for item in payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Agent Kernel supported-model matrix") from exc
    identities = {(item.provider, item.model, item.reasoning_efforts) for item in entries}
    if len(identities) != len(entries):
        raise ValueError("provider matrix entries must be unique")
    required_transports = {item.provider for item in entries if item.required}
    if "codex_app_server" not in required_transports:
        raise ValueError("the OAuth-backed Codex app-server requires a matrix entry")
    return entries


def validate_configured_model(
    *,
    provider: ProviderTransport,
    model: str,
    reasoning_effort: str | None,
    matrix: tuple[ProviderMatrixEntry, ...] | None = None,
) -> ProviderMatrixEntry:
    entries = matrix or load_supported_model_matrix()
    for entry in entries:
        if entry.provider != provider or entry.model != model:
            continue
        if reasoning_effort is not None and entry.reasoning_efforts and reasoning_effort not in entry.reasoning_efforts:
            raise UnsupportedReasoningEffort("configured reasoning effort is unsupported")
        return entry
    raise UnsupportedProviderModel("configured provider/model is unsupported")


def resolve_configured_oauth_matrix_entry(
    *,
    model: str,
    reasoning_effort: str | None,
    catalog: Any,
    environ: Mapping[str, str] | None = None,
) -> ProviderMatrixEntry:
    """Resolve the active OAuth model while preserving explicit allowlists."""

    source = environ if environ is not None else os.environ
    matrix = load_supported_model_matrix(source)
    if source.get("AGENT_KERNEL_SUPPORTED_MODEL_MATRIX_JSON", "").strip():
        return validate_configured_model(
            provider="codex_app_server",
            model=model,
            reasoning_effort=reasoning_effort,
            matrix=matrix,
        )

    try:
        return validate_configured_model(
            provider="codex_app_server",
            model=model,
            reasoning_effort=reasoning_effort,
            matrix=matrix,
        )
    except (UnsupportedProviderModel, UnsupportedReasoningEffort):
        pass

    if not isinstance(catalog, list) or len(catalog) > 64:
        raise UnsupportedProviderModel("configured provider/model is unsupported")
    for raw_entry in catalog:
        if not isinstance(raw_entry, Mapping):
            continue
        catalog_model = _identifier(raw_entry.get("id"), "model")
        if catalog_model != model:
            continue
        raw_efforts = raw_entry.get("supportedReasoningEfforts", [])
        if not isinstance(raw_efforts, list) or len(raw_efforts) > 8:
            raise UnsupportedReasoningEffort(
                "configured reasoning effort is unsupported"
            )
        efforts = tuple(
            _identifier(value, "reasoning effort")
            for value in raw_efforts
        )
        if reasoning_effort and efforts and reasoning_effort not in efforts:
            raise UnsupportedReasoningEffort(
                "configured reasoning effort is unsupported"
            )
        return ProviderMatrixEntry(
            provider="codex_app_server",
            model=catalog_model,
            reasoning_efforts=efforts,
            required=True,
        )
    raise UnsupportedProviderModel("configured provider/model is unsupported")


def validate_matrix_gate_results(
    results: list[Mapping[str, Any]],
    *,
    matrix: tuple[ProviderMatrixEntry, ...] | None = None,
) -> list[str]:
    """Require one safe passing live result for every declared required matrix entry."""
    entries = matrix or load_supported_model_matrix()
    errors: list[str] = []
    for entry in entries:
        if not entry.required:
            continue
        matching = [
            result for result in results
            if result.get("provider") == entry.provider and result.get("model") == entry.model
        ]
        if not matching:
            errors.append(f"missing:{entry.provider}:{entry.model}")
            continue
        if not any(
            result.get("passed") is True
            and result.get("unsafeDecisions", 0) == 0
            and result.get("partialCommits", 0) == 0
            for result in matching
        ):
            errors.append(f"failed:{entry.provider}:{entry.model}")
    return errors


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError(f"{name} must be a bounded identifier")
    if any(character in value for character in "\r\n\t{}[]"):
        raise ValueError(f"{name} must not contain payload data")
    return value.strip()
