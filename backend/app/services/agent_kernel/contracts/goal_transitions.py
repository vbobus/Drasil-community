"""Small provider-facing decisions for server-owned GoalState transitions."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue, Field, model_validator

from .common import ContractModel
from .goal_patches import (
    GoalPatchOperation,
    GoalSemanticPath,
    GoalStatePatch,
)
from .references import Provenance


class GoalDeltaPatch(ContractModel):
    """One sparse semantic change without host-owned provenance or identity."""

    path: GoalSemanticPath
    operation: GoalPatchOperation
    value: JsonValue = None

    @model_validator(mode="after")
    def validate_semantics(self) -> "GoalDeltaPatch":
        GoalStatePatch(
            path=self.path,
            operation=self.operation,
            value=self.value,
            provenance=Provenance(
                source_kind="user",
                source_ref="turn:contract-validation",
            ),
        )
        return self

    def bind_provenance(self, *, source_turn_id: str, source_text: str) -> GoalStatePatch:
        return GoalStatePatch(
            path=self.path,
            operation=self.operation,
            value=self.value,
            provenance=Provenance(
                source_kind="user",
                source_ref=source_turn_id,
                source_text=source_text,
            ),
        )


class GoalTurnDecision(ContractModel):
    """Strategist semantic judgment; the Agent Host supplies all authority."""

    disposition: Literal["new", "continue", "correct"]
    patches: list[GoalDeltaPatch] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_disposition(self) -> "GoalTurnDecision":
        if self.disposition == "correct" and not self.patches:
            raise ValueError("correct requires at least one sparse patch")
        if self.disposition == "continue" and self.patches:
            raise ValueError("continue cannot carry semantic patches")
        paths = [patch.path for patch in self.patches]
        if len(paths) != len(set(paths)):
            raise ValueError("a turn may address each semantic path only once")
        return self
