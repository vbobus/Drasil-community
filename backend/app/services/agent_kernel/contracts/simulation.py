"""Canonical exact-revision ChangeSet simulation contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import ContractId, Revision, RevisionedContract
from .conservation import OperationConservationReport
from .conflicts import ConflictGraph
from .references import EntityRef


MAX_SIMULATION_RESULT_BYTES = 512 * 1024


class SnapshotFact(RevisionedContract):
    fact_id: ContractId
    domain: Literal["task", "workspace", "calendar", "recurrence", "preference", "constraint"]
    entity_ref: EntityRef | None = None
    fingerprint: ContractId


class SimulationSnapshot(RevisionedContract):
    snapshot_id: ContractId
    fingerprint: ContractId
    captured_at: datetime
    freshness_boundary: datetime
    facts: list[SnapshotFact] = Field(default_factory=list, max_length=2048)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SimulationSnapshot":
        if self.captured_at.tzinfo is None or self.freshness_boundary.tzinfo is None:
            raise ValueError("simulation snapshot times must be timezone-aware")
        identities = [fact.fact_id for fact in self.facts]
        if len(identities) != len(set(identities)):
            raise ValueError("simulation snapshot facts must be unique")
        return self


class ProjectedEffect(RevisionedContract):
    operation_id: ContractId
    kind: Literal[
        "task.created", "task.updated", "task.moved", "task.archived", "task.deleted",
        "calendar.created", "calendar.moved", "calendar.resized", "calendar.split", "calendar.deleted",
        "recurrence.created", "recurrence.updated", "recurrence.occurrence_updated", "recurrence.occurrence_cancelled",
        "preference.upserted", "preference.superseded", "preference.retired",
        "preference.restored", "preference.deleted",
    ]
    entity_refs: list[EntityRef] = Field(default_factory=list, max_length=128)
    fulfills_clause_ids: list[ContractId] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_clauses(self) -> "ProjectedEffect":
        if len(self.fulfills_clause_ids) != len(set(self.fulfills_clause_ids)):
            raise ValueError("projected effect clause IDs must be unique")
        return self


class ProjectedFinalFact(RevisionedContract):
    domain: Literal["task", "calendar", "recurrence", "preference", "constraint"]
    entity_ref: EntityRef | None = None
    fingerprint: ContractId
    source_operation_ids: list[ContractId] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_sources(self) -> "ProjectedFinalFact":
        if len(self.source_operation_ids) != len(set(self.source_operation_ids)):
            raise ValueError("projected fact source operation IDs must be unique")
        return self


class SimulationIssue(RevisionedContract):
    code: Literal[
        "STALE_GOAL", "STALE_CHANGE_SET", "STALE_WORLD", "AUTHORIZATION_DENIED",
        "PRECONDITION_FAILED", "CONSERVATION_FAILED", "PROJECTION_FAILED",
        "RECURRENCE_BOUND_EXCEEDED", "GRAPH_BOUND_EXCEEDED", "SOLVER_BUDGET_EXCEEDED",
        "INVARIANT_FAILED",
    ]
    category: Literal["model_repairable", "retryable_system", "authorization", "fatal_invariant"]
    affected_operation_ids: list[ContractId] = Field(default_factory=list, max_length=128)
    entity_refs: list[EntityRef] = Field(default_factory=list, max_length=128)
    summary: str = Field(min_length=1, max_length=1000)


class SimulateChangeSetCommand(RevisionedContract):
    expected_goal_revision: Revision
    expected_change_set_revision: Revision


class ChangeSetSimulationResult(RevisionedContract):
    simulation_id: ContractId
    command_id: ContractId
    goal_id: ContractId
    goal_revision: Revision
    resulting_goal_revision: Revision
    change_set_id: ContractId
    change_set_revision: Revision
    input_snapshot: SimulationSnapshot
    output_fingerprint: ContractId
    status: Literal["ready_for_review", "repairing", "needs_user_decision", "stale", "failed"]
    conflict_graph: ConflictGraph
    projected_effects: list[ProjectedEffect] = Field(default_factory=list, max_length=512)
    projected_final_facts: list[ProjectedFinalFact] = Field(default_factory=list, max_length=2048)
    conservation: OperationConservationReport
    issues: list[SimulationIssue] = Field(default_factory=list, max_length=128)
    correlation_id: ContractId
    causation_id: ContractId

    @model_validator(mode="before")
    @classmethod
    def migrate_stored_result_evidence(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "resulting_goal_revision" not in migrated and "resultingGoalRevision" not in migrated:
            migrated["resulting_goal_revision"] = migrated.get(
                "goal_revision", migrated.get("goalRevision")
            )
        if "projected_final_facts" not in migrated and "projectedFinalFacts" not in migrated:
            migrated["projected_final_facts"] = []
        migrated.setdefault("conservation", {
            "complete": False,
            "unsupported_clause_ids": ["legacy:simulation-evidence-missing"],
        })
        return migrated

    @model_validator(mode="after")
    def validate_identity_and_status(self) -> "ChangeSetSimulationResult":
        graph = self.conflict_graph
        if self.resulting_goal_revision < self.goal_revision:
            raise ValueError("resulting goal revision cannot precede the evaluated revision")
        if (
            graph.goal_id != self.goal_id
            or graph.goal_revision != self.goal_revision
            or graph.change_set_id != self.change_set_id
            or graph.change_set_revision != self.change_set_revision
            or graph.snapshot_fingerprint != self.input_snapshot.fingerprint
        ):
            raise ValueError("simulation result identity must match its ConflictGraph and snapshot")
        expected_graph_status = {
            "ready_for_review": {"clear", "resolved"},
            "repairing": {"invalid", "resolved"},
            "needs_user_decision": {"needs_user_decision"},
            "stale": {"invalid"},
            "failed": {"invalid"},
        }[self.status]
        if graph.status not in expected_graph_status:
            raise ValueError("simulation result status is inconsistent with ConflictGraph status")
        if self.status in {"stale", "failed"} and not self.issues:
            raise ValueError("stale or failed simulation results require typed issues")
        operation_ids = [effect.operation_id for effect in self.projected_effects]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("projected effects require one entry per operation")
        if self.status not in {"stale", "failed"} and set(operation_ids) != set(graph.change_set_operation_ids):
            raise ValueError("successful simulation effects must cover the exact ChangeSet")
        if self.status in {"stale", "failed"} and not set(operation_ids).issubset(set(graph.change_set_operation_ids)):
            raise ValueError("projected effects reference operations outside the exact ChangeSet")
        fact_ids = [
            (fact.domain, fact.entity_ref.entity_id if fact.entity_ref is not None else fact.fingerprint)
            for fact in self.projected_final_facts
        ]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("projected final facts must be unique")
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > MAX_SIMULATION_RESULT_BYTES:
            raise ValueError("simulation result exceeds the serialized payload budget")
        return self
