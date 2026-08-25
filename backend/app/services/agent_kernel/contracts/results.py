"""Stable result and error envelopes for v2 tools."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from .common import ContractId, JsonValue, RevisionedContract
from .references import EntityRef


class ToolErrorCategory(StrEnum):
    MODEL_REPAIRABLE = "model_repairable"
    USER_DECISION = "user_decision"
    RETRYABLE_SYSTEM = "retryable_system"
    AUTHORIZATION = "authorization"
    FATAL_INVARIANT = "fatal_invariant"


class ToolErrorCode(StrEnum):
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_ARGUMENTS_INVALID = "TOOL_ARGUMENTS_INVALID"
    TOOL_OUTPUT_INVALID = "TOOL_OUTPUT_INVALID"
    TOOL_CONTEXT_UNAVAILABLE = "TOOL_CONTEXT_UNAVAILABLE"
    TOOL_AUTHORIZATION_DENIED = "TOOL_AUTHORIZATION_DENIED"
    TOOL_RETRYABLE_SYSTEM_FAILURE = "TOOL_RETRYABLE_SYSTEM_FAILURE"
    TOOL_VALIDATION_FAILED = "TOOL_VALIDATION_FAILED"
    TOOL_INVARIANT_FAILED = "TOOL_INVARIANT_FAILED"
    CONFLICT_REQUIRES_DECISION = "CONFLICT_REQUIRES_DECISION"


class ErrorResolution(RevisionedContract):
    resolution_id: ContractId
    label: str
    delta: dict[str, JsonValue] = Field(default_factory=dict)


class ToolError(RevisionedContract):
    code: ToolErrorCode
    category: ToolErrorCategory
    retryable: bool
    message_for_model: str
    user_summary: str | None = None
    affected_paths: list[str] = Field(default_factory=list)
    entity_refs: list[EntityRef] = Field(default_factory=list)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    repair_guidance: str | None = None
    resolutions: list[ErrorResolution] = Field(default_factory=list)
    idempotency_key: ContractId | None = None


class ToolResult(RevisionedContract):
    status: Literal["ok", "error"]
    data: JsonValue = None
    error: ToolError | None = None
    correlation_id: ContractId
    causation_id: ContractId
    freshness: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "ok" and self.error is not None:
            raise ValueError("successful tool results cannot include an error")
        if self.status == "error" and self.error is None:
            raise ValueError("failed tool results require a ToolError")
        return self

    @classmethod
    def failure(
        cls,
        error: ToolError,
        *,
        correlation_id: ContractId,
        causation_id: ContractId,
    ) -> "ToolResult":
        return cls(
            status="error",
            data=None,
            error=error,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
