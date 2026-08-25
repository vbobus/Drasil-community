"""Fail-closed promotion gate for MCP read authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any, Callable, Mapping

from sqlmodel import Session, select

from app.models.strategist_agent import StrategistAgentEvent
from app.services.agent_kernel.mcp_read_shadow import (
    MCP_READ_SHADOW_TOOL_NAMES,
    mcp_read_build_id,
)


MCP_READ_PROMOTION_REQUESTED_ENV = "DRASIL_MCP_READ_PROMOTION_REQUESTED"
MCP_READ_PROMOTION_TOOLS_ENV = "DRASIL_MCP_READ_PROMOTION_TOOLS"
MCP_READ_MIN_MATCHES_ENV = "DRASIL_MCP_READ_MIN_MATCHES_PER_TOOL"
MCP_READ_MAX_EVIDENCE_AGE_DAYS_ENV = (
    "DRASIL_MCP_READ_MAX_EVIDENCE_AGE_DAYS"
)
DEFAULT_MIN_MATCHES_PER_TOOL = 25
DEFAULT_MAX_EVIDENCE_AGE_DAYS = 14
MAX_SHADOW_EVENTS = 10_000


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _strict_bool(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and value.strip().lower() in {"1", "true", "yes", "on"}
    )


def _bounded_integer(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class McpReadPromotionPolicy:
    requested: bool
    required_build_id: str
    required_tools: tuple[str, ...]
    min_matches_per_tool: int = DEFAULT_MIN_MATCHES_PER_TOOL
    max_evidence_age_days: int = DEFAULT_MAX_EVIDENCE_AGE_DAYS
    configuration_errors: tuple[str, ...] = ()
    clock: Callable[[], datetime] = _utcnow_naive

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "McpReadPromotionPolicy":
        source = os.environ if environment is None else environment
        raw_tools = str(
            source.get(MCP_READ_PROMOTION_TOOLS_ENV) or ""
        ).strip()
        requested_tools = tuple(
            sorted(
                {
                    item.strip()
                    for item in raw_tools.split(",")
                    if item.strip()
                }
            )
        )
        invalid_tools = tuple(
            tool
            for tool in requested_tools
            if tool not in MCP_READ_SHADOW_TOOL_NAMES
        )
        errors: list[str] = []
        if invalid_tools:
            errors.append(
                "unsupported_tools:" + ",".join(invalid_tools)
            )
        return cls(
            requested=_strict_bool(
                source.get(MCP_READ_PROMOTION_REQUESTED_ENV)
            ),
            required_build_id=mcp_read_build_id(source),
            required_tools=tuple(
                tool
                for tool in requested_tools
                if tool in MCP_READ_SHADOW_TOOL_NAMES
            ),
            min_matches_per_tool=_bounded_integer(
                source.get(MCP_READ_MIN_MATCHES_ENV),
                default=DEFAULT_MIN_MATCHES_PER_TOOL,
                minimum=1,
                maximum=10_000,
            ),
            max_evidence_age_days=_bounded_integer(
                source.get(MCP_READ_MAX_EVIDENCE_AGE_DAYS_ENV),
                default=DEFAULT_MAX_EVIDENCE_AGE_DAYS,
                minimum=1,
                maximum=90,
            ),
            configuration_errors=tuple(errors),
        )


@dataclass(frozen=True)
class McpReadToolEvidence:
    tool_name: str
    sample_count: int
    match_count: int
    mismatch_count: int
    candidate_failure_count: int
    candidate_error_count: int
    invalid_event_count: int
    latest_at: str | None

    @property
    def blocking_count(self) -> int:
        return (
            self.mismatch_count
            + self.candidate_failure_count
            + self.candidate_error_count
            + self.invalid_event_count
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "toolName": self.tool_name,
            "sampleCount": self.sample_count,
            "matchCount": self.match_count,
            "mismatchCount": self.mismatch_count,
            "candidateFailureCount": self.candidate_failure_count,
            "candidateErrorCount": self.candidate_error_count,
            "invalidEventCount": self.invalid_event_count,
            "blockingCount": self.blocking_count,
            "latestAt": self.latest_at,
        }


@dataclass(frozen=True)
class McpReadPromotionReport:
    requested: bool
    eligible: bool
    reason_code: str
    required_build_id: str
    required_tools: tuple[str, ...]
    min_matches_per_tool: int
    max_evidence_age_days: int
    evidence: tuple[McpReadToolEvidence, ...]
    blockers: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "eligible": self.eligible,
            "reasonCode": self.reason_code,
            "requiredBuildId": self.required_build_id,
            "requiredTools": list(self.required_tools),
            "minMatchesPerTool": self.min_matches_per_tool,
            "maxEvidenceAgeDays": self.max_evidence_age_days,
            "evidence": [item.to_payload() for item in self.evidence],
            "blockers": list(self.blockers),
            "changesAuthority": False,
        }


def _event_payload(event: StrategistAgentEvent) -> dict[str, Any] | None:
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_comparison_payload(payload: dict[str, Any]) -> bool:
    status = payload.get("status")
    if (
        payload.get("attempted") is not True
        or payload.get("authoritative_result") != "native"
        or payload.get("candidate_result_used") is not False
        or status
        not in {
            "matched",
            "mismatched",
            "candidate_failure",
            "candidate_error",
        }
        or not _valid_digest(payload.get("primary_digest"))
    ):
        return False
    if status == "candidate_failure":
        return (
            payload.get("matched") is False
            and isinstance(payload.get("error_type"), str)
            and bool(payload["error_type"])
        )
    if (
        not _valid_digest(payload.get("candidate_digest"))
        or not isinstance(payload.get("difference_paths"), list)
    ):
        return False
    if status == "matched":
        return (
            payload.get("matched") is True
            and payload["primary_digest"] == payload["candidate_digest"]
            and not payload["difference_paths"]
        )
    return payload.get("matched") is False


def evaluate_mcp_read_promotion(
    session: Session,
    *,
    policy: McpReadPromotionPolicy | None = None,
) -> McpReadPromotionReport:
    effective = policy or McpReadPromotionPolicy.from_environment()
    blockers: list[str] = list(effective.configuration_errors)
    if effective.required_build_id == "missing_build_identity":
        blockers.append("missing_build_identity")
    if not effective.required_tools:
        blockers.append("no_promotion_tools_selected")

    cutoff = effective.clock() - timedelta(
        days=effective.max_evidence_age_days
    )
    events = list(
        session.exec(
            select(StrategistAgentEvent)
            .where(
                StrategistAgentEvent.event_type
                == "mcp_read_shadow_comparison"
            )
            .where(StrategistAgentEvent.created_at >= cutoff)
            .order_by(StrategistAgentEvent.created_at.desc())
            .limit(MAX_SHADOW_EVENTS + 1)
        ).all()
    )
    if len(events) > MAX_SHADOW_EVENTS:
        blockers.append("evidence_window_truncated")
        events = events[:MAX_SHADOW_EVENTS]
    counters: dict[str, dict[str, Any]] = {
        tool: {
            "sample_count": 0,
            "match_count": 0,
            "mismatch_count": 0,
            "candidate_failure_count": 0,
            "candidate_error_count": 0,
            "invalid_event_count": 0,
            "latest_at": None,
        }
        for tool in effective.required_tools
    }
    for event in events:
        payload = _event_payload(event)
        if not isinstance(payload, dict):
            continue
        if payload.get("build_id") != effective.required_build_id:
            continue
        tool_name = str(payload.get("tool_name") or "")
        if tool_name not in counters:
            continue
        counter = counters[tool_name]
        counter["sample_count"] += 1
        created_at = event.created_at.isoformat()
        if counter["latest_at"] is None:
            counter["latest_at"] = created_at
        if not _valid_comparison_payload(payload):
            counter["invalid_event_count"] += 1
            continue
        status = str(payload.get("status") or "")
        if status == "matched" and payload.get("matched") is True:
            counter["match_count"] += 1
        elif status == "mismatched":
            counter["mismatch_count"] += 1
        elif status == "candidate_failure":
            counter["candidate_failure_count"] += 1
        elif status == "candidate_error":
            counter["candidate_error_count"] += 1
        else:
            counter["invalid_event_count"] += 1

    evidence = tuple(
        McpReadToolEvidence(tool_name=tool, **counters[tool])
        for tool in effective.required_tools
    )
    for item in evidence:
        if item.match_count < effective.min_matches_per_tool:
            blockers.append(
                f"{item.tool_name}:insufficient_matches:"
                f"{item.match_count}/{effective.min_matches_per_tool}"
            )
        if item.blocking_count:
            blockers.append(
                f"{item.tool_name}:blocking_comparisons:"
                f"{item.blocking_count}"
            )

    blockers = list(dict.fromkeys(blockers))
    if not effective.requested:
        reason_code = "promotion_not_requested"
    elif blockers:
        reason_code = "shadow_evidence_insufficient"
    else:
        reason_code = "mcp_read_promotion_eligible"
    return McpReadPromotionReport(
        requested=effective.requested,
        eligible=effective.requested and not blockers,
        reason_code=reason_code,
        required_build_id=effective.required_build_id,
        required_tools=effective.required_tools,
        min_matches_per_tool=effective.min_matches_per_tool,
        max_evidence_age_days=effective.max_evidence_age_days,
        evidence=evidence,
        blockers=tuple(blockers),
    )
