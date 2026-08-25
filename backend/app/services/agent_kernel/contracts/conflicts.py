"""Closed, typed cross-domain simulation conflict graph contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from .change_set_deltas import ChangeSetRevisionDelta
from .common import Confidence, ContractId, Revision, RevisionedContract
from .decisions import DecisionRequest
from .references import EntityRef


ConflictKind: TypeAlias = Literal[
    "calendar_overlap",
    "constraint_violation",
    "protected_commitment",
    "recurrence_scope",
    "dependency_violation",
    "deadline_risk",
    "referential_integrity",
    "projection_invariant",
]
PolicyRecommendation: TypeAlias = Literal[
    "accept",
    "move_incoming",
    "move_blocker",
    "split_incoming",
    "preserve_existing",
    "revise_plan",
    "ask_user",
    "reject",
]
# A 90-day recurring conflict can legitimately carry two complete, exact
# occurrence-level deltas (before/after) for up to 128 bounded occurrences.
# Keep the graph finite while allowing that canonical decision surface.
MAX_CONFLICT_GRAPH_BYTES = 1024 * 1024


class OverlapInterval(RevisionedContract):
    start_at: datetime
    end_at: datetime
    timezone: str = Field(min_length=1, max_length=100)
    incoming_operation_ids: list[ContractId] = Field(default_factory=list, max_length=128)
    blocker_refs: list[EntityRef] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_interval(self) -> "OverlapInterval":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("overlap intervals require timezone-aware datetimes")
        if self.end_at <= self.start_at:
            raise ValueError("overlap interval end_at must be after start_at")
        if len(self.incoming_operation_ids) != len(set(self.incoming_operation_ids)):
            raise ValueError("overlap incoming operation IDs must be unique")
        identities = [(ref.kind, ref.entity_id) for ref in self.blocker_refs]
        if len(identities) != len(set(identities)):
            raise ValueError("overlap blocker references must be unique")
        return self


class BlockerEvidence(RevisionedContract):
    blocker_id: ContractId
    entity_ref: EntityRef | None = None
    label: str | None = Field(default=None, min_length=1, max_length=500)
    start_at: datetime | None = None
    end_at: datetime | None = None
    source: Literal[
        "task",
        "calendar",
        "recurrence",
        "goal_constraint",
        "change_set_constraint",
        "preference",
        "projection",
    ]
    observed_revision: Revision | None = None
    observed_at: datetime
    freshness_boundary: datetime
    fingerprint: ContractId
    priority: Literal["low", "medium", "high", "unknown"] = "unknown"
    priority_source: Literal["authoritative", "proposed", "unknown"] = "unknown"
    deadline_at: datetime | None = None
    dependency_impact: Literal["none", "upstream", "downstream", "both", "unknown"] = (
        "unknown"
    )
    anchoring: Literal["anchored", "unanchored", "unknown"] = "unknown"
    movability: Literal["fixed", "flexible", "unknown"] = "unknown"
    protection: Literal["protected", "pinned", "ordinary", "unknown"] = "unknown"
    recurrence_scope: Literal["none", "occurrence", "series"] = "none"
    series_ref: EntityRef | None = None
    occurrence_ref: EntityRef | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_stored_freshness(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if "observed_at" not in migrated and "observedAt" not in migrated:
            migrated["observed_at"] = legacy_time
        if "freshness_boundary" not in migrated and "freshnessBoundary" not in migrated:
            migrated["freshness_boundary"] = legacy_time
        return migrated

    @model_validator(mode="after")
    def validate_scope(self) -> "BlockerEvidence":
        if self.observed_at.tzinfo is None or self.freshness_boundary.tzinfo is None:
            raise ValueError("blocker freshness times must be timezone-aware")
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("blocker start_at and end_at must be supplied together")
        if self.start_at is not None and self.end_at is not None:
            if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
                raise ValueError("blocker times must be timezone-aware")
            if self.end_at <= self.start_at:
                raise ValueError("blocker end_at must be after start_at")
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            raise ValueError("blocker deadlines require timezone-aware datetimes")
        if self.recurrence_scope == "none" and (
            self.series_ref is not None or self.occurrence_ref is not None
        ):
            raise ValueError("non-recurring blocker cannot carry recurrence references")
        if self.recurrence_scope == "occurrence" and self.occurrence_ref is None:
            raise ValueError("occurrence blocker requires an occurrence reference")
        if self.recurrence_scope == "series" and self.series_ref is None:
            raise ValueError("series blocker requires a series reference")
        return self


class AlternativeFeasibilityProof(RevisionedContract):
    snapshot_fingerprint: ContractId
    delta_fingerprint: ContractId
    method: Literal["projection", "cp_sat_projection"]
    checked_conflict_ids: list[ContractId] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_conflicts(self) -> "AlternativeFeasibilityProof":
        if len(self.checked_conflict_ids) != len(set(self.checked_conflict_ids)):
            raise ValueError("feasibility proof conflict IDs must be unique")
        return self


class ConflictAlternative(RevisionedContract):
    alternative_id: ContractId
    summary: str = Field(min_length=1, max_length=1000)
    feasibility: Literal["feasible", "unavailable"]
    affected_entities: list[EntityRef] = Field(default_factory=list, max_length=128)
    tradeoff: Literal[
        "none", "moves_incoming", "moves_existing", "splits_work", "drops_requested_change"
    ] = "none"
    placement_relation: Literal["before_incoming", "after_incoming"] | None = None
    change_set_delta: ChangeSetRevisionDelta | None = None
    feasibility_proof: AlternativeFeasibilityProof | None = None
    unavailable_reason: Literal[
        "protected",
        "hard_constraint",
        "no_feasible_slot",
        "scope_not_authorized",
        "stale_snapshot",
        "solver_budget",
    ] | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_stored_proof(cls, value: object) -> object:
        if not isinstance(value, dict) or value.get("feasibility") != "feasible":
            return value
        migrated = dict(value)
        if "feasibility_proof" not in migrated and "feasibilityProof" not in migrated:
            migrated["feasibility_proof"] = {
                "snapshot_fingerprint": "legacy:unknown-snapshot",
                "delta_fingerprint": "legacy:unknown-delta",
                "method": "projection",
            }
        return migrated

    @model_validator(mode="after")
    def validate_feasibility(self) -> "ConflictAlternative":
        if self.feasibility == "feasible" and self.change_set_delta is None:
            raise ValueError("feasible alternatives require an exact ChangeSet delta")
        if self.feasibility == "feasible" and self.feasibility_proof is None:
            raise ValueError("feasible alternatives require a snapshot-bound proof")
        if self.feasibility == "feasible" and self.unavailable_reason is not None:
            raise ValueError("feasible alternatives cannot carry an unavailable reason")
        if self.feasibility == "unavailable" and self.unavailable_reason is None:
            raise ValueError("unavailable alternatives require a stable reason")
        if self.feasibility == "unavailable" and self.feasibility_proof is not None:
            raise ValueError("unavailable alternatives cannot carry a feasibility proof")
        if self.placement_relation is not None and self.tradeoff != "moves_existing":
            raise ValueError("placement_relation is valid only when moving existing commitments")
        return self


class Conflict(RevisionedContract):
    conflict_id: ContractId
    kind: ConflictKind
    incoming_operation_ids: list[ContractId] = Field(min_length=1, max_length=128)
    incoming_label: str | None = Field(default=None, min_length=1, max_length=500)
    incoming_priority: Literal["low", "medium", "high", "unknown"] = "unknown"
    requested_start_at: datetime | None = None
    requested_end_at: datetime | None = None
    blockers: list[BlockerEvidence] = Field(min_length=1, max_length=128)
    overlap_intervals: list[OverlapInterval] = Field(default_factory=list, max_length=128)
    policy_recommendation: PolicyRecommendation
    resolution_disposition: Literal[
        "clear", "model_repairable", "needs_user_decision", "unresolvable"
    ]
    confidence: Confidence
    alternatives: list[ConflictAlternative] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_members(self) -> "Conflict":
        if len(self.incoming_operation_ids) != len(set(self.incoming_operation_ids)):
            raise ValueError("conflict incoming operation IDs must be unique")
        blocker_ids = [item.blocker_id for item in self.blockers]
        if len(blocker_ids) != len(set(blocker_ids)):
            raise ValueError("conflict blockers must be unique")
        alternative_ids = [item.alternative_id for item in self.alternatives]
        if len(alternative_ids) != len(set(alternative_ids)):
            raise ValueError("conflict alternatives must be unique")
        if self.resolution_disposition == "needs_user_decision" and not self.alternatives:
            raise ValueError("decision conflicts require alternatives")
        if (self.requested_start_at is None) != (self.requested_end_at is None):
            raise ValueError("requested start and end must be supplied together")
        if self.requested_start_at is not None and self.requested_end_at is not None:
            if self.requested_start_at.tzinfo is None or self.requested_end_at.tzinfo is None:
                raise ValueError("requested conflict times must be timezone-aware")
            if self.requested_end_at <= self.requested_start_at:
                raise ValueError("requested conflict end must be after start")
        return self


class ConflictGraph(RevisionedContract):
    graph_id: ContractId
    goal_id: ContractId
    goal_revision: Revision
    change_set_id: ContractId
    change_set_revision: Revision
    change_set_operation_ids: list[ContractId] = Field(min_length=1, max_length=128)
    snapshot_fingerprint: ContractId
    status: Literal["clear", "resolved", "needs_user_decision", "invalid"]
    conflicts: list[Conflict] = Field(default_factory=list, max_length=128)
    decision_request: DecisionRequest | None = None

    @model_validator(mode="after")
    def validate_graph(self) -> "ConflictGraph":
        conflict_ids = [item.conflict_id for item in self.conflicts]
        if len(conflict_ids) != len(set(conflict_ids)):
            raise ValueError("ConflictGraph conflict IDs must be unique")
        if self.status == "clear" and self.conflicts:
            raise ValueError("clear ConflictGraph cannot contain conflicts")
        if self.status != "clear" and not self.conflicts:
            raise ValueError("non-clear ConflictGraph requires conflicts")
        if self.status == "needs_user_decision" and self.decision_request is None:
            raise ValueError("needs_user_decision requires exactly one DecisionRequest")
        if self.status != "needs_user_decision" and self.decision_request is not None:
            raise ValueError("only needs_user_decision may carry a DecisionRequest")
        if self.decision_request is not None:
            if self.decision_request.goal_id != self.goal_id:
                raise ValueError("decision goal must match ConflictGraph goal")
            if self.decision_request.goal_revision != self.goal_revision + 1:
                raise ValueError("decision must target the resulting persisted goal revision")
            for option in self.decision_request.options:
                if option.target == "change_set" and option.change_set_id != self.change_set_id:
                    raise ValueError("decision ChangeSet target must match ConflictGraph")
        if len(self.change_set_operation_ids) != len(set(self.change_set_operation_ids)):
            raise ValueError("ConflictGraph ChangeSet operation IDs must be unique")
        known_operations = set(self.change_set_operation_ids)
        for conflict in self.conflicts:
            if not set(conflict.incoming_operation_ids).issubset(known_operations):
                raise ValueError("conflict references an operation outside the exact ChangeSet")
            for alternative in conflict.alternatives:
                delta = alternative.change_set_delta
                if delta is not None and delta.expected_change_set_revision != self.change_set_revision:
                    raise ValueError("alternative delta targets a stale ChangeSet revision")
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > MAX_CONFLICT_GRAPH_BYTES:
            raise ValueError("ConflictGraph exceeds the serialized payload budget")
        return self
