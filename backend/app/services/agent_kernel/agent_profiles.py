"""Versioned identities and durable duties for Drasil runtime agents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.agent_kernel.capabilities import capability_catalog


AGENT_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class AgentProfileError(ValueError):
    """Raised when a checked-in runtime agent profile is invalid."""


@dataclass(frozen=True)
class AgentProfile:
    id: str
    version: str
    title: str
    summary: str
    duties: tuple[str, ...]
    boundaries: tuple[str, ...]
    available_capabilities: tuple[str, ...]
    operating_instructions: str

    def compact(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "duties": list(self.duties),
            "boundaries": list(self.boundaries),
            "available_capabilities": list(self.available_capabilities),
        }


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentProfileError(f"Agent profile field {key!r} must be a non-empty string.")
    return value.strip()


def _string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AgentProfileError(
            f"Agent profile field {key!r} must be a non-empty list of strings."
        )
    return tuple(item.strip() for item in value)


@lru_cache(maxsize=1)
def agent_profiles() -> tuple[AgentProfile, ...]:
    known_capabilities = {item.id: item for item in capability_catalog()}
    profiles: list[AgentProfile] = []
    for path in sorted(AGENT_PROFILES_DIR.glob("*/profile.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentProfileError(f"Could not load agent profile {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AgentProfileError(f"Agent profile {path} must contain an object.")
        agent_id = _required_text(payload, "id")
        if not _ID_RE.fullmatch(agent_id) or path.parent.name != agent_id:
            raise AgentProfileError(
                f"Agent id {agent_id!r} must be kebab-case and match its directory name."
            )
        instructions_path = path.parent / "AGENT.md"
        try:
            operating_instructions = instructions_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentProfileError(f"Agent {agent_id!r} is missing AGENT.md: {exc}") from exc
        capabilities = _string_list(payload, "available_capabilities")
        unknown = set(capabilities).difference(known_capabilities)
        if unknown:
            raise AgentProfileError(
                f"Agent {agent_id!r} references unknown capabilities: {sorted(unknown)}"
            )
        unavailable = {
            capability_id
            for capability_id in capabilities
            if agent_id not in known_capabilities[capability_id].available_to
        }
        if unavailable:
            raise AgentProfileError(
                f"Capabilities are not declared for agent {agent_id!r}: {sorted(unavailable)}"
            )
        profiles.append(
            AgentProfile(
                id=agent_id,
                version=_required_text(payload, "version"),
                title=_required_text(payload, "title"),
                summary=_required_text(payload, "summary"),
                duties=_string_list(payload, "duties"),
                boundaries=_string_list(payload, "boundaries"),
                available_capabilities=capabilities,
                operating_instructions=operating_instructions,
            )
        )
    ids = [profile.id for profile in profiles]
    if len(ids) != len(set(ids)):
        raise AgentProfileError("Agent profile ids must be unique.")
    return tuple(profiles)


def get_agent_profile(agent_id: str, *, include_instructions: bool = False) -> dict[str, Any]:
    normalized = str(agent_id or "").strip().lower()
    profile = next((item for item in agent_profiles() if item.id == normalized), None)
    if profile is None:
        raise AgentProfileError(f"Unknown Drasil runtime agent: {normalized}")
    payload = profile.compact()
    if include_instructions:
        payload["operating_instructions"] = profile.operating_instructions
    return payload


def clear_agent_profile_cache() -> None:
    agent_profiles.cache_clear()
