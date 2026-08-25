"""Server-owned approval, atomic execution, verification, and sync contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import json
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .common import ContractId, Revision, RevisionedContract
from .references import EntityRef
from .results import ToolErrorCategory


Fingerprint = str
MAX_LOCAL_EXECUTION_RESULT_BYTES = 2_000_000


class ExecutionErrorCode(StrEnum):
    STALE_GOAL = "STALE_GOAL"
    STALE_CHANGE_SET = "STALE_CHANGE_SET"
    STALE_SIMULATION = "STALE_SIMULATION"
    STALE_WORLD = "STALE_WORLD"
    STALE_ENTITY = "STALE_ENTITY"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    INVALID_LIFECYCLE = "INVALID_LIFECYCLE"
    INVALID_APPROVAL_IDENTITY = "INVALID_APPROVAL_IDENTITY"
    COMMAND_REUSE_CONFLICT = "COMMAND_REUSE_CONFLICT"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    UNSUPPORTED_ATOMIC_MODE = "UNSUPPORTED_ATOMIC_MODE"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    LOCAL_APPLY_FAILED = "LOCAL_APPLY_FAILED"
    INVARIANT_FAILED = "INVARIANT_FAILED"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"
    SYNC_RETRYABLE = "SYNC_RETRYABLE"
    SYNC_PERMANENT = "SYNC_PERMANENT"


class ExternalSyncErrorCode(StrEnum):
    SYNC_NOT_CONFIGURED = "SYNC_NOT_CONFIGURED"
    SYNC_INVALID_PAYLOAD = "SYNC_INVALID_PAYLOAD"
    SYNC_RETRYABLE = "SYNC_RETRYABLE"
    SYNC_PERMANENT = "SYNC_PERMANENT"


_EXECUTION_ERROR_POLICY: dict[
    ExecutionErrorCode,
    tuple[ToolErrorCategory, bool, frozenset[str]],
] = {
    ExecutionErrorCode.STALE_GOAL: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.STALE_CHANGE_SET: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.STALE_SIMULATION: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.STALE_WORLD: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.STALE_ENTITY: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.PRECONDITION_FAILED: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"revise"})),
    ExecutionErrorCode.INVALID_LIFECYCLE: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.INVALID_APPROVAL_IDENTITY: (ToolErrorCategory.MODEL_REPAIRABLE, False, frozenset({"resimulate"})),
    ExecutionErrorCode.COMMAND_REUSE_CONFLICT: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect"})),
    ExecutionErrorCode.AUTHORIZATION_DENIED: (ToolErrorCategory.AUTHORIZATION, False, frozenset({"revise", "reconnect"})),
    ExecutionErrorCode.UNSUPPORTED_ATOMIC_MODE: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect"})),
    ExecutionErrorCode.UNSUPPORTED_OPERATION: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect"})),
    ExecutionErrorCode.LOCAL_APPLY_FAILED: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect"})),
    ExecutionErrorCode.INVARIANT_FAILED: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect"})),
    ExecutionErrorCode.VERIFICATION_MISMATCH: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect"})),
    ExecutionErrorCode.SYNC_RETRYABLE: (ToolErrorCategory.RETRYABLE_SYSTEM, True, frozenset({"retry"})),
    ExecutionErrorCode.SYNC_PERMANENT: (ToolErrorCategory.FATAL_INVARIANT, False, frozenset({"inspect", "reconnect"})),
}


class ExecutionError(RevisionedContract):
    code: ExecutionErrorCode
    category: ToolErrorCategory
    retryable: bool
    user_summary: str = Field(min_length=1, max_length=500)
    affected_operation_ids: list[ContractId] = Field(default_factory=list, max_length=128)
    entity_refs: list[EntityRef] = Field(default_factory=list, max_length=128)
    recovery: Literal["revise", "resimulate", "retry", "reconnect", "inspect", "none"] = "none"

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        category, retryable, recoveries = _EXECUTION_ERROR_POLICY[self.code]
        if self.category != category or self.retryable != retryable or self.recovery not in recoveries:
            raise ValueError("execution error fields contradict the closed policy for this code")
        return self


class ApproveAndApplyChangeSetCommand(RevisionedContract):
    """Typed UI/server command. This contract must never be model-facing."""

    command_id: ContractId
    idempotency_key: ContractId
    session_id: ContractId
    goal_id: ContractId
    expected_goal_revision: Revision
    change_set_id: ContractId
    expected_change_set_revision: Revision
    simulation_id: ContractId
    simulation_input_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    simulation_output_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    correlation_id: ContractId
    causation_id: ContractId


class EntityBinding(RevisionedContract):
    operation_id: ContractId
    temporary_id: ContractId | None = None
    entity_ref: EntityRef


class ExpectedField(RevisionedContract):
    field: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    value_json: str = Field(max_length=20_000)


class ExecutionFact(RevisionedContract):
    operation_id: ContractId
    disposition: Literal["present", "absent", "inactive"]
    entity_ref: EntityRef
    fields: list[ExpectedField] = Field(default_factory=list, max_length=128)
    fingerprint: Fingerprint = Field(min_length=32, max_length=128)

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        if self.disposition == "absent" and self.fields:
            raise ValueError("absent execution facts cannot declare fields")
        return self


class VerificationMismatch(RevisionedContract):
    operation_id: ContractId
    entity_ref: EntityRef | None = None
    field: str | None = Field(default=None, max_length=128)
    expected_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    actual_fingerprint: Fingerprint | None = Field(default=None, min_length=32, max_length=128)
    summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_difference(self) -> Self:
        if self.actual_fingerprint == self.expected_fingerprint:
            raise ValueError("verification mismatch fingerprints must differ")
        return self


class VerificationResult(RevisionedContract):
    status: Literal["pending", "verified", "verification_failed"]
    intended_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    actual_fingerprint: Fingerprint | None = Field(default=None, min_length=32, max_length=128)
    verified_at: datetime | None = None
    mismatches: list[VerificationMismatch] = Field(default_factory=list, max_length=128)
    recovery: Literal["reverify", "compensating_review", "manual_inspection"] | None = None
    recovery_summary: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "pending" and (
            self.actual_fingerprint
            or self.verified_at
            or self.mismatches
            or self.recovery
            or self.recovery_summary
        ):
            raise ValueError("pending verification cannot include actual evidence")
        if self.status == "verified":
            if self.actual_fingerprint != self.intended_fingerprint or self.verified_at is None:
                raise ValueError("verified execution requires equal fingerprints and a timestamp")
            if self.mismatches:
                raise ValueError("verified execution cannot contain mismatches")
            if self.recovery is not None or self.recovery_summary is not None:
                raise ValueError("verified execution cannot include recovery guidance")
        if self.status == "verification_failed":
            if (
                self.actual_fingerprint is None
                or self.verified_at is None
                or not self.mismatches
                or self.recovery is None
                or self.recovery_summary is None
            ):
                raise ValueError(
                    "verification failure requires evidence and bounded recovery guidance"
                )
            if self.actual_fingerprint == self.intended_fingerprint:
                raise ValueError("verification failure requires unequal aggregate fingerprints")
            if not self.recovery_summary.strip():
                raise ValueError("verification recovery guidance cannot be blank")
        if self.verified_at is not None and self.verified_at.tzinfo is None:
            raise ValueError("verification timestamp must be timezone-aware")
        return self


class ExternalSyncItemStatus(RevisionedContract):
    outbox_id: ContractId
    provider: Literal["google"]
    operation: Literal["create", "update", "delete"]
    entity_kind: Literal["calendar_block"]
    entity_id: ContractId
    status: Literal["not_configured", "pending", "delivering", "succeeded", "failed"]
    attempts: int = Field(ge=0)
    next_attempt_at: datetime | None = None
    remote_id: str | None = Field(default=None, max_length=256)
    remote_version: str | None = Field(default=None, max_length=256)
    last_error_code: ExternalSyncErrorCode | None = None
    error_summary: str | None = Field(default=None, min_length=1, max_length=500)
    recovery: Literal["retry", "reconnect", "inspect"] | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        if self.error_summary is not None and not self.error_summary.strip():
            raise ValueError("external sync error summary cannot be blank")
        if self.status == "succeeded":
            if self.last_error_code is not None or self.error_summary is not None or self.recovery is not None:
                raise ValueError("successful external sync cannot retain error evidence")
            if self.operation in {"create", "update"} and not self.remote_id:
                raise ValueError("successful create/update sync requires a remote identity")
        elif self.status == "not_configured":
            if self.last_error_code != ExternalSyncErrorCode.SYNC_NOT_CONFIGURED:
                raise ValueError("not-configured sync requires its closed error code")
            if self.next_attempt_at is not None or self.recovery != "reconnect":
                raise ValueError("not-configured sync must be parked with reconnect guidance")
        elif self.status == "failed":
            if self.last_error_code not in {
                ExternalSyncErrorCode.SYNC_INVALID_PAYLOAD,
                ExternalSyncErrorCode.SYNC_PERMANENT,
            }:
                raise ValueError("failed external sync requires a terminal sync error code")
            if self.error_summary is None or self.recovery != "inspect" or self.next_attempt_at is not None:
                raise ValueError("failed external sync requires bounded terminal evidence")
        elif self.status in {"pending", "delivering"}:
            if self.last_error_code not in {None, ExternalSyncErrorCode.SYNC_RETRYABLE}:
                raise ValueError("active external sync may carry only retryable error evidence")
            if self.last_error_code is None and (self.error_summary is not None or self.recovery is not None):
                raise ValueError("active external sync cannot carry unclassified error evidence")
            if self.last_error_code == ExternalSyncErrorCode.SYNC_RETRYABLE and (
                self.error_summary is None or self.recovery != "retry"
            ):
                raise ValueError("retryable external sync requires bounded retry guidance")
        return self


class LocalExecutionResult(RevisionedContract):
    execution_id: ContractId
    command_id: ContractId
    idempotency_key: ContractId
    session_id: ContractId
    goal_id: ContractId
    goal_revision: Revision
    change_set_id: ContractId
    change_set_revision: Revision
    simulation_id: ContractId
    status: Literal["committed_pending_verification", "applied", "verification_failed"]
    payload_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    bindings: list[EntityBinding] = Field(default_factory=list, max_length=512)
    facts: list[ExecutionFact] = Field(default_factory=list, max_length=2048)
    verification: VerificationResult
    sync_items: list[ExternalSyncItemStatus] = Field(default_factory=list, max_length=512)
    correlation_id: ContractId
    causation_id: ContractId

    @model_validator(mode="before")
    @classmethod
    def validate_aggregate_size(cls, value: Any) -> Any:
        if isinstance(value, dict):
            size = len(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                    "utf-8"
                )
            )
            if size > MAX_LOCAL_EXECUTION_RESULT_BYTES:
                raise ValueError("local execution result exceeds the canonical byte budget")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        expected = {
            "committed_pending_verification": "pending",
            "applied": "verified",
            "verification_failed": "verification_failed",
        }[self.status]
        if self.verification.status != expected:
            raise ValueError("execution status must match verification status")
        binding_keys = [(item.operation_id, item.temporary_id) for item in self.bindings]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("execution bindings must be unique")
        temporary_ids = [item.temporary_id for item in self.bindings if item.temporary_id is not None]
        if len(temporary_ids) != len(set(temporary_ids)):
            raise ValueError("temporary execution bindings must be globally unique")
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > MAX_LOCAL_EXECUTION_RESULT_BYTES:
            raise ValueError("local execution result exceeds the canonical byte budget")
        return self


class ExecutionOutcome(RevisionedContract):
    status: Literal["ok", "error"]
    result: LocalExecutionResult | None = None
    error: ExecutionError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "ok" and (self.result is None or self.error is not None):
            raise ValueError("successful execution requires only a result")
        if self.status == "error" and (self.error is None or self.result is not None):
            raise ValueError("failed execution requires only a typed error")
        return self
