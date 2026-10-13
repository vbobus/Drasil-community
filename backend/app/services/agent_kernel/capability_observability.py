"""Provider-neutral capability telemetry emitted from completed agent turns."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.services.agent_kernel.agent_observability import record_agent_event


logger = logging.getLogger("drasil.agent_capabilities")
rollout_logger = logging.getLogger("drasil.agent_kernel_rollout")


RolloutEventKind = Literal[
    "shadow_comparison", "gate_evaluation", "cutover_decision", "clarification",
    "repair_loop", "unsafe_decision", "transaction", "verification", "external_sync",
    "provider_call",
]


@dataclass(frozen=True)
class RolloutObservation:
    """Closed aggregate-safe event; arbitrary dimensions and prose are forbidden."""

    event: RolloutEventKind
    outcome: str
    family: str | None = None
    provider: str | None = None
    model: str | None = None
    rollout_cohort: str = "shadow"
    flag_enabled: bool = False
    divergence_count: int = 0
    repair_iterations: int = 0
    unsafe_decisions: int = 0
    prevented_partial_commits: int = 0
    actual_partial_commits: int = 0
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microunits: int | None = None
    reason_code: str | None = None
    request_id_hash: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "divergence_count", "repair_iterations", "unsafe_decisions",
            "prevented_partial_commits", "actual_partial_commits",
        ):
            _require_nonnegative(getattr(self, name), name)
        for name in ("latency_ms", "input_tokens", "output_tokens", "cost_microunits"):
            value = getattr(self, name)
            if value is not None:
                _require_nonnegative(value, name)
        for name in (
            "outcome", "family", "provider", "model", "rollout_cohort", "reason_code",
            "request_id_hash",
        ):
            _require_safe_identifier(getattr(self, name), name)
        if self.actual_partial_commits > 0 and self.outcome == "success":
            raise ValueError("a partial local commit cannot be recorded as success")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def emit_rollout_observation(observation: RolloutObservation) -> dict[str, Any]:
    payload = observation.to_payload()
    rollout_logger.info("agent kernel rollout observation", extra={"rollout_observation": payload})
    record_agent_event(
        f"rollout.{observation.event}",
        {key: value for key, value in payload.items() if value is not None},
    )
    return payload


def clarification_was_unnecessary(*, goal_delta_count: int, constraint_delta_count: int,
                                  decision_delta_count: int) -> bool:
    """A clarification is unnecessary only when it produces no typed semantic delta."""
    values = (goal_delta_count, constraint_delta_count, decision_delta_count)
    for value in values:
        _require_nonnegative(value, "clarification delta")
    return sum(values) == 0


def _require_nonnegative(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_safe_identifier(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ValueError(f"{name} must be a bounded identifier")
    forbidden = {"\n", "\r", "\t", "{", "}", "[", "]"}
    if any(item in value for item in forbidden):
        raise ValueError(f"{name} must not contain prose or payload data")


def emit_capability_observations(
    evidence: dict[str, Any],
    *,
    agent_id: str,
) -> list[dict[str, Any]]:
    """Emit one structured event per attempted skill load and return the events.

    Returning the records keeps the same information available to API diagnostics
    and tests even when a deployment has not configured a metrics exporter yet.
    """

    checks = {
        item.get("capability_id"): item
        for item in evidence.get("skill_evidence_checks", [])
        if isinstance(item, dict)
    }
    events: list[dict[str, Any]] = []
    for loaded in evidence.get("loaded_capabilities", []):
        if not isinstance(loaded, dict):
            continue
        check = checks.get(loaded.get("id"), {})
        event = {
            "event": "agent_capability_load",
            "agent_id": agent_id,
            "capability_id": loaded.get("id"),
            "capability_version": loaded.get("version"),
            "evidence_satisfied": check.get("requirements_satisfied"),
            "unmet_evidence_requirements": check.get("unmet_requirements", []),
            "load_duration_ms": loaded.get("load_duration_ms"),
            "failed": bool(loaded.get("failed")),
            "error": loaded.get("error"),
        }
        logger.info("agent capability observation", extra={"capability_observation": event})
        record_agent_event(
            "capability.load",
            {
                "agentId": event["agent_id"],
                "capabilityId": event["capability_id"],
                "capabilityVersion": event["capability_version"],
                "evidenceSatisfied": event["evidence_satisfied"],
                "unmetRequirementCount": len(event["unmet_evidence_requirements"]),
                "loadDurationMs": int(event["load_duration_ms"] or 0),
                "failed": event["failed"],
            },
        )
        events.append(event)
    return events
