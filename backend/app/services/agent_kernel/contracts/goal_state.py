"""Canonical durable semantic goal state."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import ContractId, RevisionedContract
from .constraints import Constraint
from .decisions import DecisionRequest, ResolvedDecision
from .goal_patches import GoalSemanticPath, GoalStatePatch
from .intent import RequestedOperationClause, migrate_requested_operation_clauses
from .references import EntityRef, EntitySelector, Provenance, SemanticValue


class DesiredOutcome(RevisionedContract):
    outcome_id: ContractId
    description: str = Field(min_length=1, max_length=2000)
    provenance: Provenance
    satisfied: bool = False


class CorrectionDelta(RevisionedContract):
    delta_id: ContractId
    source_turn_id: ContractId
    patches: list[GoalStatePatch] = Field(min_length=1)
    supersedes_paths: list[GoalSemanticPath] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_patch_paths(self) -> "CorrectionDelta":
        paths = [patch.path for patch in self.patches]
        if len(paths) != len(set(paths)):
            raise ValueError("a correction may address each semantic path only once")
        if not set(self.supersedes_paths).issubset(paths):
            raise ValueError("supersedes_paths must be addressed by this correction")
        return self


class Assumption(RevisionedContract):
    assumption_id: ContractId
    statement: str
    provenance: Provenance
    status: Literal["active", "superseded", "rejected"] = "active"


class StoredContractMigrationIssue(RevisionedContract):
    issue_id: ContractId
    path: str = Field(min_length=1, max_length=512)
    code: Literal["unsupported_constraint", "temporary_kind_unresolved"]
    legacy_kind: str | None = Field(default=None, max_length=256)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SemanticSlot(RevisionedContract):
    path: GoalSemanticPath
    semantic_value: SemanticValue

    @model_validator(mode="after")
    def migrate_requested_operations(self) -> "SemanticSlot":
        if self.path != "operations.requested" or self.semantic_value.state not in {
            "present",
            "inferred",
        }:
            return self
        provenance = self.semantic_value.provenance
        if provenance is None:
            raise ValueError("operations.requested requires provenance")
        migrated = migrate_requested_operation_clauses(
            self.semantic_value.value,
            provenance=provenance,
        )
        object.__setattr__(
            self,
            "semantic_value",
            self.semantic_value.model_copy(update={"value": migrated}),
        )
        return self


class GoalStateUpdateCommand(RevisionedContract):
    """Internal compatibility command with server-bound durable authority."""

    command_id: ContractId
    disposition: Literal["continue", "correct", "request_decision", "new_goal"]
    goal_id: ContractId | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    original_request: str | None = Field(default=None, min_length=1, max_length=10000)
    patches: list[GoalStatePatch] = Field(default_factory=list)
    decisions: list[DecisionRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_disposition(self) -> "GoalStateUpdateCommand":
        if self.disposition in {"continue", "correct", "request_decision"} and (
            self.goal_id is None or self.expected_revision is None
        ):
            raise ValueError(f"{self.disposition} requires goal_id and expected_revision")
        if self.disposition == "correct" and not self.patches:
            raise ValueError("correct requires one or more typed patches")
        if self.disposition == "continue" and self.patches:
            raise ValueError("continue cannot carry semantic patches")
        if self.disposition == "continue" and self.decisions:
            raise ValueError("continue cannot create decisions")
        if self.disposition == "request_decision" and (self.patches or not self.decisions):
            raise ValueError("request_decision requires decisions and cannot carry patches")
        if self.disposition == "correct" and self.decisions and not self.patches:
            raise ValueError("correct decisions must accompany semantic patches")
        if self.disposition == "new_goal" and not self.original_request:
            raise ValueError("new_goal requires original_request")
        if self.disposition == "new_goal" and (
            self.goal_id is not None or self.expected_revision is not None or self.decisions
        ):
            raise ValueError("new_goal cannot carry prior goal identity or decisions")
        if self.disposition != "new_goal" and self.original_request is not None:
            raise ValueError("original_request is only valid for new_goal")
        paths = [patch.path for patch in self.patches]
        if len(paths) != len(set(paths)):
            raise ValueError("a command may address each semantic path only once")
        return self


class GoalState(RevisionedContract):
    goal_id: ContractId
    status: Literal[
        "collecting_context",
        "planning",
        "simulating",
        "repairing",
        "needs_user_decision",
        "ready_for_review",
        "approved",
        "committing",
        "applied",
        "rejected",
        "superseded",
        "cancelled",
        "system_failed",
    ] = "collecting_context"
    original_request: str = Field(min_length=1, max_length=10000)
    correction_deltas: list[CorrectionDelta] = Field(default_factory=list)
    semantic_slots: list[SemanticSlot] = Field(default_factory=list)
    desired_outcomes: list[DesiredOutcome] = Field(default_factory=list)
    selectors: list[EntitySelector] = Field(default_factory=list)
    resolved_refs: list[EntityRef] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    relevant_preferences: list[EntityRef] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    migration_issues: list[StoredContractMigrationIssue] = Field(default_factory=list)
    unresolved_decisions: list[DecisionRequest] = Field(default_factory=list)
    resolved_decisions: list[ResolvedDecision] = Field(default_factory=list)
    current_change_set_id: ContractId | None = None
    current_change_set_revision: int | None = Field(default=None, ge=1)
    approved_change_set_revision: int | None = Field(default=None, ge=1)
    correlation_id: ContractId
    causation_id: ContractId

    @model_validator(mode="after")
    def validate_owned_collections(self) -> "GoalState":
        paths = [slot.path for slot in self.semantic_slots]
        if len(paths) != len(set(paths)):
            raise ValueError("semantic slot paths must be unique")
        unresolved = [decision.decision_id for decision in self.unresolved_decisions]
        resolved = [decision.decision_id for decision in self.resolved_decisions]
        if len(unresolved) != len(set(unresolved)) or len(resolved) != len(set(resolved)):
            raise ValueError("decision ids must be unique within each history")
        if set(unresolved).intersection(resolved):
            raise ValueError("a decision cannot be both unresolved and resolved")
        unresolved_clause_decisions = {
            clause.decision_id
            for clause in self.requested_operation_clauses()
            if clause.disposition == "unresolved"
        }
        if not unresolved_clause_decisions.issubset(set(unresolved)):
            raise ValueError("unresolved operation clauses require their open durable decisions")
        for decision in self.unresolved_decisions:
            if decision.goal_id != self.goal_id:
                raise ValueError("unresolved decisions must belong to this goal")
            if decision.status != "open":
                raise ValueError("unresolved_decisions may contain only open decisions")
            if decision.goal_revision != self.revision:
                raise ValueError("open decisions must target the current goal revision")
        if self.current_change_set_revision is not None and self.current_change_set_id is None:
            raise ValueError("current_change_set_revision requires current_change_set_id")
        if self.approved_change_set_revision is not None:
            if self.current_change_set_id is None or self.current_change_set_revision is None:
                raise ValueError("approved_change_set_revision requires a current ChangeSet")
            if self.approved_change_set_revision != self.current_change_set_revision:
                raise ValueError("approval must identify the current ChangeSet revision")

        slots_by_path = {slot.path: slot.semantic_value for slot in self.semantic_slots}
        projection_models = {
            "scope.selectors": ("selectors", EntitySelector),
            "scope.resolved_refs": ("resolved_refs", EntityRef),
            "constraints.active": ("constraints", Constraint),
            "preferences.relevant": ("relevant_preferences", EntityRef),
            "outcomes.desired": ("desired_outcomes", DesiredOutcome),
        }
        from pydantic import TypeAdapter

        for path, (attribute, model) in projection_models.items():
            semantic_value = slots_by_path.get(path)
            if semantic_value is None:
                continue
            projected = (
                TypeAdapter(list[model]).validate_python(semantic_value.value)  # type: ignore[valid-type]
                if semantic_value.state in {"present", "inferred"}
                else []
            )
            object.__setattr__(self, attribute, projected)
        return self

    def requested_operation_clauses(self) -> list[RequestedOperationClause]:
        slot = next(
            (item for item in self.semantic_slots if item.path == "operations.requested"),
            None,
        )
        if slot is None or slot.semantic_value.state not in {"present", "inferred"}:
            return []
        from pydantic import TypeAdapter

        return TypeAdapter(list[RequestedOperationClause]).validate_python(
            slot.semantic_value.value
        )
