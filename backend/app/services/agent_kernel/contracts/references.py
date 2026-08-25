"""Provenance and semantic entity references."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from .common import Confidence, ContractId, ContractModel, JsonValue, Revision


SourceKind = Literal["user", "tool", "retrieval", "inference", "default", "policy", "ui"]
EntityKind: TypeAlias = Literal[
    "task",
    "workspace",
    "calendar_block",
    "recurrence_series",
    "recurrence_occurrence",
    "preference",
    "scheduling_constraint",
    "preference_link",
]


class SemanticValue(ContractModel):
    state: Literal["unknown", "unresolved", "inferred", "present", "explicit_null", "cleared"]
    value: JsonValue = None
    provenance: "Provenance | None" = None

    @model_validator(mode="after")
    def validate_semantic_state(self) -> "SemanticValue":
        value_states = {"present", "inferred"}
        if self.state in value_states and self.value is None:
            raise ValueError(f"{self.state} requires a non-null value")
        if self.state not in value_states and self.value is not None:
            raise ValueError(f"{self.state} requires a null value")
        if self.state in {"present", "inferred", "explicit_null", "cleared"} and self.provenance is None:
            raise ValueError(f"{self.state} requires provenance")
        if self.state == "inferred" and (
            self.provenance is None or self.provenance.source_kind not in {"inference", "default"}
        ):
            raise ValueError("inferred values require inference or default provenance")
        if self.state in {"explicit_null", "cleared"} and (
            self.provenance is None or self.provenance.source_kind not in {"user", "ui"}
        ):
            raise ValueError(f"{self.state} requires explicit user or UI provenance")
        return self


class Provenance(ContractModel):
    source_kind: SourceKind
    source_ref: ContractId
    confidence: Confidence | None = None
    supersedes_ref: ContractId | None = None
    source_text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_confidence(self) -> "Provenance":
        if self.source_kind in {"inference", "default"} and self.confidence is None:
            raise ValueError(f"{self.source_kind} provenance requires confidence")
        return self


class EntityRef(ContractModel):
    kind: EntityKind | Literal["temporary"]
    entity_id: ContractId
    revision: Revision | None = None
    title: str | None = Field(default=None, max_length=500)
    target_kind: EntityKind | None = None

    @model_validator(mode="after")
    def validate_temporary_kind(self) -> "EntityRef":
        if self.kind == "temporary" and self.target_kind is None:
            raise ValueError("temporary references require target_kind")
        if self.kind != "temporary" and self.target_kind is not None:
            raise ValueError("existing entity references cannot declare target_kind")
        if self.kind == "temporary" and self.revision is not None:
            raise ValueError("temporary references cannot declare a persisted revision")
        return self


class EntitySelector(ContractModel):
    selector_id: ContractId
    kind: Literal["exact_ref", "title", "query", "working_set", "workspace", "set"]
    value: str | list[str]
    cardinality: Literal["exactly_one", "zero_or_one", "one_or_more", "any_set"]
    provenance: Provenance
    resolved_refs: list[EntityRef] = Field(default_factory=list)


SemanticValue.model_rebuild()
