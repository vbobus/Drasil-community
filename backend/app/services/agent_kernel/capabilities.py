"""Versioned, model-discoverable capabilities for Drasil runtime agents.

Capabilities provide guidance, not authority.  Loading a skill never grants a
tool permission, changes task scope, or bypasses proposal/evidence policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol


CAPABILITIES_DIR = Path(__file__).resolve().parent / "capabilities"
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CapabilityCatalogError(ValueError):
    """Raised when a checked-in capability package violates its contract."""


class CapabilityRetriever(Protocol):
    """Provider-neutral candidate retrieval boundary."""

    strategy: str

    def retrieve(
        self,
        *,
        goal: str,
        agent: str,
        desired_output: str,
        limit: int,
        manifests: tuple["CapabilityManifest", ...],
    ) -> tuple[list["CapabilityManifest"], bool]: ...


@dataclass(frozen=True)
class CapabilityManifest:
    id: str
    version: str
    title: str
    summary: str
    available_to: tuple[str, ...]
    use_for: tuple[str, ...]
    not_for: tuple[str, ...]
    recommended_tools: tuple[str, ...]
    compatible_output_modes: tuple[str, ...]
    evidence_requirements: dict[str, Any]
    dependencies: tuple[str, ...]
    maximum_loaded_characters: int
    directory: Path

    def compact(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "available_to": list(self.available_to),
            "use_for": list(self.use_for),
            "compatible_output_modes": list(self.compatible_output_modes),
        }


class BoundedCatalogModelSemanticRetriever:
    """Expose a small bounded catalog for semantic choice by the active model."""

    strategy = "model_semantic_selection_over_bounded_catalog"

    def retrieve(
        self,
        *,
        goal: str,
        agent: str,
        desired_output: str,
        limit: int,
        manifests: tuple[CapabilityManifest, ...],
    ) -> tuple[list[CapabilityManifest], bool]:
        _ = goal
        eligible = [
            manifest
            for manifest in manifests
            if (not agent or agent in manifest.available_to)
            and (
                not desired_output
                or desired_output in manifest.compatible_output_modes
            )
        ]
        return eligible[:limit], len(eligible) > limit


DEFAULT_CAPABILITY_RETRIEVER: CapabilityRetriever = BoundedCatalogModelSemanticRetriever()


def _string_list(payload: dict[str, Any], key: str, *, required: bool = False) -> tuple[str, ...]:
    raw = payload.get(key)
    if raw is None and not required:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise CapabilityCatalogError(f"Capability field {key!r} must be a list of non-empty strings.")
    return tuple(item.strip() for item in raw)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityCatalogError(f"Capability field {key!r} must be a non-empty string.")
    return value.strip()


def _load_manifest(path: Path) -> CapabilityManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityCatalogError(f"Could not load capability manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CapabilityCatalogError(f"Capability manifest {path} must contain an object.")
    capability_id = _required_text(payload, "id")
    if not _ID_RE.fullmatch(capability_id) or path.parent.name != capability_id:
        raise CapabilityCatalogError(
            f"Capability id {capability_id!r} must be kebab-case and match its directory name."
        )
    skill_path = path.parent / "SKILL.md"
    if not skill_path.is_file():
        raise CapabilityCatalogError(f"Capability {capability_id!r} is missing SKILL.md.")
    evidence_requirements = payload.get("evidence_requirements", {})
    if not isinstance(evidence_requirements, dict):
        raise CapabilityCatalogError("Capability field 'evidence_requirements' must be an object.")
    maximum = payload.get("maximum_loaded_characters", 6000)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1000 <= maximum <= 20000:
        raise CapabilityCatalogError("maximum_loaded_characters must be an integer from 1000 to 20000.")
    return CapabilityManifest(
        id=capability_id,
        version=_required_text(payload, "version"),
        title=_required_text(payload, "title"),
        summary=_required_text(payload, "summary"),
        available_to=_string_list(payload, "available_to", required=True),
        use_for=_string_list(payload, "use_for", required=True),
        not_for=_string_list(payload, "not_for"),
        recommended_tools=_string_list(payload, "recommended_tools"),
        compatible_output_modes=_string_list(payload, "compatible_output_modes", required=True),
        evidence_requirements=dict(evidence_requirements),
        dependencies=_string_list(payload, "dependencies"),
        maximum_loaded_characters=maximum,
        directory=path.parent,
    )


@lru_cache(maxsize=1)
def capability_catalog() -> tuple[CapabilityManifest, ...]:
    manifests = tuple(
        _load_manifest(path)
        for path in sorted(CAPABILITIES_DIR.glob("*/capability.json"))
    )
    ids = [manifest.id for manifest in manifests]
    if len(ids) != len(set(ids)):
        raise CapabilityCatalogError("Capability ids must be unique.")
    known = set(ids)
    for manifest in manifests:
        missing = set(manifest.dependencies).difference(known)
        if missing:
            raise CapabilityCatalogError(
                f"Capability {manifest.id!r} has unknown dependencies: {sorted(missing)}"
            )
    return manifests


def compact_capability_catalog(*, agent: str | None = None) -> dict[str, Any]:
    normalized_agent = str(agent or "").strip().lower()
    capabilities = [
        item.compact()
        for item in capability_catalog()
        if not normalized_agent or normalized_agent in item.available_to
    ]
    return {
        "name": "drasil-runtime-capability-catalog",
        "version": "v1",
        "capability_count": len(capabilities),
        "capabilities": capabilities,
        "selection_policy": (
            "The model may search and load zero or more skills. Catalog matches are suggestions, "
            "not routes or authorization. Skills never select task scope or bypass evidence, tool, "
            "proposal, or approval policy."
        ),
    }


def validate_capability_tool_references(available_tool_names: set[str]) -> None:
    """Fail fast when a checked-in skill recommends a tool that does not exist."""

    for manifest in capability_catalog():
        missing = set(manifest.recommended_tools).difference(available_tool_names)
        if missing:
            raise CapabilityCatalogError(
                f"Capability {manifest.id!r} recommends unknown tools: {sorted(missing)}"
            )


def search_capabilities(
    arguments: dict[str, Any],
    *,
    retriever: CapabilityRetriever | None = None,
) -> dict[str, Any]:
    """Return a bounded semantic-selection surface for the controlling model.

    Drasil deliberately does not rank capabilities with keywords or regexes.
    While the catalog is small, the controlling model receives every eligible
    compact manifest and performs the semantic selection itself.  The response
    contract leaves room for a future provider-neutral embedding retriever
    without changing the model-facing tool.
    """

    goal = str(arguments.get("goal") or "").strip()
    agent = str(arguments.get("agent") or "strategist").strip().lower()
    desired_output = str(arguments.get("desired_output") or "").strip().lower()
    raw_limit = arguments.get("limit", 5)
    limit = max(1, min(int(raw_limit) if isinstance(raw_limit, int) else 5, 10))
    active_retriever = retriever or DEFAULT_CAPABILITY_RETRIEVER
    eligible, catalog_truncated = active_retriever.retrieve(
        goal=goal,
        agent=agent,
        desired_output=desired_output,
        limit=limit,
        manifests=capability_catalog(),
    )
    matches = [
        {
            **manifest.compact(),
            "not_for": list(manifest.not_for),
            "recommended_tools": list(manifest.recommended_tools),
        }
        for manifest in eligible
    ]
    return {
        "goal": goal,
        "agent": agent,
        "desired_output": desired_output or None,
        "count": len(matches),
        "capabilities": matches,
        "retrieval_strategy": active_retriever.strategy,
        "catalog_truncated": catalog_truncated,
        "selection_note": (
            "Semantically compare the goal with each manifest's summary, use_for, and not_for. "
            "No lexical score or deterministic route has been applied. Load only capabilities "
            "that materially help; simple turns may need no skill."
        ),
    }


def load_skill(arguments: dict[str, Any]) -> dict[str, Any]:
    capability_id = str(arguments.get("capability_id") or "").strip().lower()
    manifest = next((item for item in capability_catalog() if item.id == capability_id), None)
    if manifest is None:
        return {
            "error": f"Unknown Drasil capability: {capability_id}",
            "available_capability_ids": [item.id for item in capability_catalog()],
        }
    text = (manifest.directory / "SKILL.md").read_text(encoding="utf-8").strip()
    truncated = len(text) > manifest.maximum_loaded_characters
    if truncated:
        text = text[: manifest.maximum_loaded_characters].rstrip()
    return {
        "capability": manifest.compact(),
        "skill": text,
        "recommended_tools": list(manifest.recommended_tools),
        "evidence_requirements": manifest.evidence_requirements,
        "dependencies": list(manifest.dependencies),
        "truncated": truncated,
        "authority_boundary": (
            "This skill is guidance only. It does not grant tool permissions, choose task scope, "
            "or override evidence, mutation, proposal, approval, or safety policy."
        ),
    }


def clear_capability_cache() -> None:
    """Test/development hook for re-reading checked-in capability packages."""

    capability_catalog.cache_clear()
