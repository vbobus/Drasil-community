"""Strict, kind-owned constraint contracts for goals and native ChangeSets."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from .common import ContractId, ContractModel, RevisionedContract
from .references import EntityRef, Provenance


ConstraintKind: TypeAlias = Literal[
    "minimum_gap",
    "temporal_window",
    "exact_time",
    "cardinality",
    "ordering",
    "same_workspace",
    "preserve",
]


class ConstraintScope(RevisionedContract):
    kind: Literal["goal", "operation_set", "entity_set", "series", "occurrence_set"]
    operation_ids: list[ContractId] = Field(default_factory=list, max_length=128)
    entity_refs: list[EntityRef] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def validate_scope_members(self) -> "ConstraintScope":
        if len(self.operation_ids) != len(set(self.operation_ids)):
            raise ValueError("constraint operation IDs cannot contain duplicates")
        identities = [
            (ref.kind, ref.target_kind, ref.entity_id) for ref in self.entity_refs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("constraint entity references cannot contain duplicates")
        if self.kind == "goal" and (self.operation_ids or self.entity_refs):
            raise ValueError("goal scope cannot include operation or entity members")
        if self.kind == "operation_set":
            if not self.operation_ids or self.entity_refs:
                raise ValueError("operation_set scope requires only operation IDs")
        if self.kind in {"entity_set", "series", "occurrence_set"}:
            if not self.entity_refs or self.operation_ids:
                raise ValueError(f"{self.kind} scope requires only entity references")
        if self.kind == "series":
            for ref in self.entity_refs:
                actual = ref.target_kind if ref.kind == "temporary" else ref.kind
                if actual != "recurrence_series":
                    raise ValueError("series scope requires recurrence-series references")
        if self.kind == "occurrence_set":
            for ref in self.entity_refs:
                actual = ref.target_kind if ref.kind == "temporary" else ref.kind
                if actual != "recurrence_occurrence":
                    raise ValueError(
                        "occurrence_set scope requires recurrence-occurrence references"
                    )
        return self


class MinimumGapConstraintValue(ContractModel):
    kind: Literal["minimum_gap"] = "minimum_gap"
    minutes: int = Field(ge=0, le=10_080)


class TemporalWindowConstraintValue(ContractModel):
    kind: Literal["temporal_window"] = "temporal_window"
    disposition: Literal["allowed", "forbidden"] = "allowed"
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "TemporalWindowConstraintValue":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("temporal windows require timezone-aware datetimes")
        if self.end_at <= self.start_at:
            raise ValueError("temporal window end_at must be after start_at")
        return self


class ExactTimeConstraintValue(ContractModel):
    kind: Literal["exact_time"] = "exact_time"
    start_at: datetime
    duration_minutes: int = Field(ge=1, le=10_080)

    @model_validator(mode="after")
    def validate_timezone(self) -> "ExactTimeConstraintValue":
        if self.start_at.tzinfo is None:
            raise ValueError("exact times require a timezone-aware datetime")
        return self


class CardinalityConstraintValue(ContractModel):
    kind: Literal["cardinality"] = "cardinality"
    minimum: int = Field(ge=0, le=512)
    maximum: int = Field(ge=0, le=512)

    @model_validator(mode="after")
    def validate_range(self) -> "CardinalityConstraintValue":
        if self.maximum < self.minimum:
            raise ValueError("cardinality maximum cannot be less than minimum")
        return self


class OrderingConstraintValue(ContractModel):
    kind: Literal["ordering"] = "ordering"
    relation: Literal["before", "after", "consecutive"]


class SameWorkspaceConstraintValue(ContractModel):
    kind: Literal["same_workspace"] = "same_workspace"


class PreserveConstraintValue(ContractModel):
    kind: Literal["preserve"] = "preserve"
    property: Literal[
        "identity", "cardinality", "series_scope", "occurrence_scope", "parent", "workspace"
    ]


ConstraintValue: TypeAlias = Annotated[
    MinimumGapConstraintValue
    | TemporalWindowConstraintValue
    | ExactTimeConstraintValue
    | CardinalityConstraintValue
    | OrderingConstraintValue
    | SameWorkspaceConstraintValue
    | PreserveConstraintValue,
    Field(discriminator="kind"),
]


class Constraint(RevisionedContract):
    constraint_id: ContractId
    kind: ConstraintKind
    strength: Literal["hard", "soft"]
    scope: ConstraintScope
    object_scope: ConstraintScope | None = None
    value: ConstraintValue
    provenance: Provenance
    superseded_by: ContractId | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_value(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        raw_value = migrated.get("value")
        kind = migrated.get("kind")
        if isinstance(raw_value, dict) and "kind" not in raw_value and isinstance(kind, str):
            migrated["value"] = {"kind": kind, **raw_value}
        return migrated

    @model_validator(mode="after")
    def validate_kind_semantics(self) -> "Constraint":
        if self.value.kind != self.kind:
            raise ValueError("constraint kind must match its typed value kind")
        if self.kind in {"ordering", "same_workspace"} and self.object_scope is None:
            raise ValueError(f"{self.kind} requires an object scope")
        if self.kind not in {"ordering", "same_workspace", "minimum_gap"} and self.object_scope:
            raise ValueError(f"{self.kind} does not accept an object scope")
        if self.kind == "minimum_gap" and self.object_scope is None:
            member_count = len(self.scope.operation_ids) + len(self.scope.entity_refs)
            if self.scope.kind != "goal" and member_count < 2:
                raise ValueError(
                    "minimum_gap requires an object scope or at least two subject members"
                )
        if self.kind == "exact_time":
            if self.scope.kind == "goal":
                raise ValueError("exact_time requires one concrete subject")
            members = len(self.scope.operation_ids) + len(self.scope.entity_refs)
            if members != 1:
                raise ValueError("exact_time requires exactly one subject")
            invalid = {
                ref.target_kind if ref.kind == "temporary" else ref.kind
                for ref in self.scope.entity_refs
            } - {"task", "calendar_block", "recurrence_occurrence"}
            if invalid:
                raise ValueError("exact_time has an incompatible entity scope")
        if self.kind == "temporal_window":
            invalid = {
                ref.target_kind if ref.kind == "temporary" else ref.kind
                for ref in self.scope.entity_refs
            } & {"preference", "workspace"}
            if invalid:
                raise ValueError("temporal_window has an incompatible entity scope")
        if self.kind in {"ordering", "same_workspace"}:
            if self.scope.kind == "goal" or self.object_scope.kind == "goal":
                raise ValueError(f"{self.kind} requires concrete subject and object scopes")
        if self.kind == "same_workspace":
            refs = [*self.scope.entity_refs, *self.object_scope.entity_refs]
            if any(
                (ref.target_kind if ref.kind == "temporary" else ref.kind)
                not in {"task", "workspace"}
                for ref in refs
            ):
                raise ValueError("same_workspace accepts only task or workspace entities")
        if self.kind == "preserve":
            if self.value.property == "series_scope" and self.scope.kind != "series":
                raise ValueError("series_scope preservation requires series scope")
            if self.value.property == "occurrence_scope" and self.scope.kind != "occurrence_set":
                raise ValueError("occurrence_scope preservation requires occurrence scope")
        return self
