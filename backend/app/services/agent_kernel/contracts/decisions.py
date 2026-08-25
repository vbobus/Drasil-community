"""Durable user-decision contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from .common import ContractId, Revision, RevisionedContract
from .change_set_deltas import ChangeSetRevisionDelta
from .goal_patches import GoalStatePatch
from .references import EntityRef


class DecisionDelta(RevisionedContract):
    option_id: ContractId
    label: str = Field(min_length=1, max_length=300)
    target: Literal["goal_state"] = "goal_state"
    patches: list[GoalStatePatch] = Field(min_length=1)


class ChangeSetDecisionDelta(RevisionedContract):
    option_id: ContractId
    label: str = Field(min_length=1, max_length=300)
    target: Literal["change_set"] = "change_set"
    change_set_id: ContractId
    alternative_id: ContractId
    placement_relation: Literal["before_incoming", "after_incoming"] | None = None
    delta: ChangeSetRevisionDelta


DecisionOption: TypeAlias = Annotated[
    DecisionDelta | ChangeSetDecisionDelta,
    Field(discriminator="target"),
]


class DecisionConflictDetail(RevisionedContract):
    date: str | None = Field(default=None, max_length=10)
    incoming_task_title: str = Field(min_length=1, max_length=500)
    incoming_priority: Literal["low", "medium", "high", "unknown"] = "unknown"
    requested_start_at: datetime
    requested_end_at: datetime
    blocker_task_title: str = Field(min_length=1, max_length=500)
    blocker_priority: Literal["low", "medium", "high", "unknown"] = "unknown"
    blocker_start_at: datetime
    blocker_end_at: datetime
    fixed_reason: str | None = Field(default=None, min_length=1, max_length=500)
    decision_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    recommended_action: Literal["move_existing", "preserve_existing"] | None = None

    @model_validator(mode="after")
    def validate_times(self) -> "DecisionConflictDetail":
        values = (
            self.requested_start_at,
            self.requested_end_at,
            self.blocker_start_at,
            self.blocker_end_at,
        )
        if any(value.tzinfo is None for value in values):
            raise ValueError("decision conflict times must be timezone-aware")
        if self.requested_end_at <= self.requested_start_at:
            raise ValueError("requested conflict end must be after start")
        if self.blocker_end_at <= self.blocker_start_at:
            raise ValueError("blocker conflict end must be after start")
        return self


class DecisionRequest(RevisionedContract):
    decision_id: ContractId
    goal_id: ContractId
    goal_revision: Revision
    target_revision: Revision
    question: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)
    status: Literal["open", "resolved", "superseded", "cancelled"] = "open"
    affected_entities: list[EntityRef] = Field(default_factory=list)
    conflicts: list[DecisionConflictDetail] = Field(default_factory=list, max_length=128)
    options: list[DecisionOption] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_revision_and_options(self) -> "DecisionRequest":
        if self.target_revision != self.goal_revision + 1:
            raise ValueError("target_revision must be exactly one greater than goal_revision")
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision option ids must be unique")
        return self


class ResolvedDecision(RevisionedContract):
    decision_id: ContractId
    option_id: ContractId
    source_event_id: ContractId
    applied_revision: Revision
