"""Fail-closed Agent Kernel v2 write rollout policy.

Natural-language text, proposal families, and browser payloads are deliberately absent
from this module.  Native write authority is derived only from canonical operation kinds,
server configuration, and machine-readable gate evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .provider_matrix import load_supported_model_matrix


class OperationFamily(StrEnum):
    TASK = "task"
    CALENDAR = "calendar"
    RECURRENCE = "recurrence"
    PREFERENCE = "preference"


class GateKind(StrEnum):
    DETERMINISTIC = "deterministic"
    TRAJECTORY = "trajectory"
    LIVE_MODEL = "live_model"
    BROWSER = "browser"


LOGICAL_FLAGS: dict[OperationFamily, str] = {
    OperationFamily.TASK: "agent_kernel_v2_task_writes",
    OperationFamily.CALENDAR: "agent_kernel_v2_calendar_writes",
    OperationFamily.RECURRENCE: "agent_kernel_v2_recurrence_writes",
    OperationFamily.PREFERENCE: "agent_kernel_v2_preference_writes",
}
ENV_FLAGS = {family: name.upper() for family, name in LOGICAL_FLAGS.items()}
REQUIRED_GATES = frozenset(GateKind)
FAMILY_OPERATION_KINDS: dict[OperationFamily, frozenset[str]] = {
    OperationFamily.TASK: frozenset({
        "task.create",
        "task.update",
        "task.move",
        "task.archive",
        "task.delete",
    }),
    OperationFamily.CALENDAR: frozenset({
        "calendar.create_block",
        "calendar.move_block",
        "calendar.resize_block",
        "calendar.split_block",
        "calendar.delete_block",
    }),
    OperationFamily.RECURRENCE: frozenset({
        "recurrence.create_series",
        "recurrence.update_series",
        "recurrence.update_occurrence",
        "recurrence.cancel_occurrence",
    }),
    OperationFamily.PREFERENCE: frozenset({
        "preference.upsert",
        "preference.supersede",
        "preference.retire",
        "preference.restore",
        "preference.delete",
    }),
}
NATIVE_SHADOW_MODEL_TOOLS_ENV = "AGENT_KERNEL_V2_SHADOW_MODEL_TOOLS_ENABLED"
NATIVE_SHADOW_SAMPLING_ENV = "AGENT_KERNEL_V2_SHADOW_SAMPLING_ENABLED"
NATIVE_REVIEW_REQUIRED_ENV = "AGENT_KERNEL_V2_NATIVE_REVIEW_REQUIRED"
GATE_MANIFEST_PATH_ENV = "AGENT_KERNEL_V2_GATE_MANIFEST_PATH"
MAX_GATE_MANIFEST_BYTES = 512 * 1024


def native_shadow_model_tools_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Keep experimental v2 authoring tools off the production model surface by default."""

    source = environ if environ is not None else os.environ
    return _strict_bool(source.get(NATIVE_SHADOW_MODEL_TOOLS_ENV))


def native_shadow_sampling_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Enable an isolated, non-authoritative v2 authoring turn."""

    source = environ if environ is not None else os.environ
    return _strict_bool(source.get(NATIVE_SHADOW_SAMPLING_ENV))


def native_review_required_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Fail closed instead of exposing a legacy mutation fallback."""

    source = environ if environ is not None else os.environ
    return _strict_bool(source.get(NATIVE_REVIEW_REQUIRED_ENV))


@dataclass(frozen=True)
class GateEvidence:
    family: OperationFamily
    gate: GateKind
    suite_id: str
    build_id: str
    recorded_at: str
    passed: bool
    operation_kinds: tuple[str, ...] = ()
    unsafe_decisions: int = 0
    partial_commits: int = 0
    provider: str | None = None
    model: str | None = None

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "GateEvidence":
        allowed = {
            "family", "gate", "suiteId", "buildId", "recordedAt", "passed",
            "operationKinds", "unsafeDecisions", "partialCommits", "provider", "model",
        }
        if set(value) - allowed:
            raise ValueError("gate evidence contains unsupported fields")
        family = OperationFamily(str(value["family"]))
        evidence = cls(
            family=family,
            gate=GateKind(str(value["gate"])),
            suite_id=_bounded_identifier(value.get("suiteId"), "suiteId"),
            build_id=_bounded_identifier(value.get("buildId"), "buildId"),
            recorded_at=_iso_timestamp(value.get("recordedAt")),
            passed=value.get("passed") is True,
            operation_kinds=_operation_kinds(
                value.get("operationKinds", ()), family=family
            ),
            unsafe_decisions=_nonnegative_int(value.get("unsafeDecisions", 0)),
            partial_commits=_nonnegative_int(value.get("partialCommits", 0)),
            provider=_optional_identifier(value.get("provider"), "provider"),
            model=_optional_identifier(value.get("model"), "model"),
        )
        if evidence.gate is GateKind.LIVE_MODEL and not (evidence.provider and evidence.model):
            raise ValueError("live-model evidence requires provider and model")
        return evidence

    @property
    def blocking(self) -> bool:
        return not self.passed or self.unsafe_decisions != 0 or self.partial_commits != 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "gate": self.gate.value,
            "suiteId": self.suite_id,
            "buildId": self.build_id,
            "recordedAt": self.recorded_at,
            "passed": self.passed,
            "operationKinds": list(self.operation_kinds),
            "unsafeDecisions": self.unsafe_decisions,
            "partialCommits": self.partial_commits,
            "provider": self.provider,
            "model": self.model,
        }


@dataclass(frozen=True)
class RolloutDecision:
    eligible: bool
    families: tuple[OperationFamily, ...]
    enabled_families: tuple[OperationFamily, ...]
    missing_gates: tuple[str, ...]
    reason_code: str

    def to_safe_payload(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "families": [item.value for item in self.families],
            "enabledFamilies": [item.value for item in self.enabled_families],
            "missingGates": list(self.missing_gates),
            "reasonCode": self.reason_code,
        }


@dataclass(frozen=True)
class RolloutPolicy:
    enabled: Mapping[OperationFamily, bool]
    evidence: tuple[GateEvidence, ...] = ()
    required_live_entries: tuple[tuple[str, str], ...] = ()
    required_build_id: str | None = None
    max_evidence_age_days: int = 30
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "RolloutPolicy":
        source = environ if environ is not None else os.environ
        enabled = {
            family: _strict_bool(source.get(env_name))
            for family, env_name in ENV_FLAGS.items()
        }
        raw_manifest = _gate_manifest_json(source)
        evidence: tuple[GateEvidence, ...] = ()
        if raw_manifest.strip():
            try:
                payload = json.loads(raw_manifest)
                if not isinstance(payload, list) or len(payload) > 256:
                    raise ValueError("gate manifest must be a bounded list")
                evidence = tuple(GateEvidence.from_payload(item) for item in payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # Invalid evidence never turns writes on.  Retain flags so the
                # decision can explain that the gates, rather than the flag, blocked cutover.
                evidence = ()
        try:
            required_live_entries = tuple(
                (entry.provider, entry.model)
                for entry in load_supported_model_matrix(source)
                if entry.required
            )
        except ValueError:
            required_live_entries = (("invalid_matrix", "invalid_matrix"),)
        configured_build_id = source.get("AGENT_KERNEL_V2_BUILD_ID")
        try:
            required_build_id = _bounded_identifier(
                configured_build_id, "AGENT_KERNEL_V2_BUILD_ID"
            )
        except ValueError:
            required_build_id = "missing_build_identity"
        return cls(
            enabled=enabled,
            evidence=evidence,
            required_live_entries=required_live_entries,
            required_build_id=required_build_id,
        )

    @classmethod
    def for_testing(cls, families: Iterable[OperationFamily]) -> "RolloutPolicy":
        selected = frozenset(families)
        now = datetime.now(timezone.utc).isoformat()
        evidence = tuple(
            GateEvidence(
                family=family,
                gate=gate,
                suite_id=f"test-{family.value}-{gate.value}",
                build_id="test-build",
                recorded_at=now,
                passed=True,
                operation_kinds=tuple(sorted(FAMILY_OPERATION_KINDS[family])),
                provider="test-provider" if gate is GateKind.LIVE_MODEL else None,
                model="test-model" if gate is GateKind.LIVE_MODEL else None,
            )
            for family in selected
            for gate in GateKind
        )
        return cls(
            enabled={family: family in selected for family in OperationFamily},
            evidence=evidence,
            required_live_entries=(("test-provider", "test-model"),),
            required_build_id="test-build",
        )

    def evaluate(self, operations: Sequence[Any]) -> RolloutDecision:
        try:
            families = represented_families(operations)
        except ValueError:
            return RolloutDecision(False, (), (), (), "unsupported_operation_family")
        return self.evaluate_families(families)

    def evaluate_families(
        self,
        families: Iterable[OperationFamily],
    ) -> RolloutDecision:
        """Evaluate an explicit closed family set for inventory and routing."""

        try:
            selected = tuple(
                sorted(
                    {OperationFamily(family) for family in families},
                    key=lambda item: item.value,
                )
            )
        except (TypeError, ValueError):
            return RolloutDecision(
                False,
                (),
                (),
                (),
                "unsupported_operation_family",
            )
        families = selected
        if not families:
            return RolloutDecision(False, (), (), (), "empty_change_set")
        enabled = tuple(family for family in families if self.enabled.get(family) is True)
        if len(enabled) != len(families):
            return RolloutDecision(False, families, enabled, (), "family_disabled")

        missing: list[str] = []
        for family in families:
            by_gate: dict[GateKind, list[GateEvidence]] = {gate: [] for gate in GateKind}
            for item in self.evidence:
                if item.family is family:
                    by_gate[item.gate].append(item)
            for gate in GateKind:
                candidates = by_gate[gate]
                fresh_candidates = [item for item in candidates if self._fresh(item)]
                if not fresh_candidates or any(item.blocking for item in fresh_candidates):
                    missing.append(f"{family.value}:{gate.value}")
                    continue
                required_operation_kinds = FAMILY_OPERATION_KINDS[family]
                covered_operation_kinds = {
                    operation_kind
                    for item in fresh_candidates
                    for operation_kind in item.operation_kinds
                }
                if not required_operation_kinds.issubset(covered_operation_kinds):
                    missing.append(f"{family.value}:{gate.value}")
                    continue
                if gate is GateKind.LIVE_MODEL:
                    live_entries_cover_family = all(
                        required_operation_kinds.issubset({
                            operation_kind
                            for item in fresh_candidates
                            if (item.provider, item.model) == required_entry
                            for operation_kind in item.operation_kinds
                        })
                        for required_entry in self.required_live_entries
                    )
                    if not live_entries_cover_family:
                        missing.append(f"{family.value}:{gate.value}")
        if missing:
            return RolloutDecision(False, families, enabled, tuple(sorted(missing)), "gate_evidence_missing")
        return RolloutDecision(True, families, enabled, (), "native_write_eligible")

    def _fresh(self, evidence: GateEvidence) -> bool:
        if (
            self.required_build_id is not None
            and evidence.build_id != self.required_build_id
        ):
            return False
        recorded = datetime.fromisoformat(evidence.recorded_at.replace("Z", "+00:00"))
        age = self.clock().astimezone(timezone.utc) - recorded.astimezone(timezone.utc)
        return age.total_seconds() >= 0 and age.days <= self.max_evidence_age_days


def represented_families(operations: Sequence[Any]) -> tuple[OperationFamily, ...]:
    families: set[OperationFamily] = set()
    for operation in operations:
        kind = getattr(operation, "kind", None)
        if not isinstance(kind, str) or "." not in kind:
            raise ValueError("operation kind is not canonical")
        try:
            families.add(OperationFamily(kind.split(".", 1)[0]))
        except ValueError as exc:
            raise ValueError("operation family is unsupported") from exc
    return tuple(sorted(families, key=lambda item: item.value))


def _strict_bool(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _gate_manifest_json(source: Mapping[str, str]) -> str:
    """Load one server-owned inline or file-backed manifest, failing closed."""

    inline = source.get("AGENT_KERNEL_V2_GATE_MANIFEST_JSON", "").strip()
    if inline:
        return inline
    configured_path = source.get(GATE_MANIFEST_PATH_ENV, "").strip()
    if not configured_path:
        return ""
    try:
        path = Path(configured_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[4] / path
        if not path.is_file() or path.stat().st_size > MAX_GATE_MANIFEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _bounded_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError(f"{name} must be a bounded identifier")
    return value.strip()


def _optional_identifier(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _bounded_identifier(value, name)


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("gate counters must be non-negative integers")
    return value


def _operation_kinds(
    value: Any, *, family: OperationFamily
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError("operationKinds must be a bounded list")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("operationKinds must contain canonical strings")
    normalized = tuple(sorted({item.strip() for item in value if item.strip()}))
    if len(normalized) != len(value):
        raise ValueError("operationKinds cannot be empty or contain duplicates")
    if any(item not in FAMILY_OPERATION_KINDS[family] for item in normalized):
        raise ValueError("operationKinds contains an operation outside its family")
    return normalized


def _iso_timestamp(value: Any) -> str:
    raw = _bounded_identifier(value, "recordedAt")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("recordedAt must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("recordedAt must include a timezone")
    return raw
