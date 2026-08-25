"""Presentation-only review documents and server-owned typed UI actions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from .common import ContractId, Revision, RevisionedContract
from .decisions import DecisionRequest
from .execution import EntityBinding, ExecutionFact, ExecutionOutcome
from .references import EntityRef


Fingerprint = str
DisplayDetail: TypeAlias = Annotated[str, Field(min_length=1, max_length=1000)]


class ReviewEntity(RevisionedContract):
    """An identity-bearing reference with display-safe copy."""

    ref: EntityRef
    label: str = Field(min_length=1, max_length=500)
    context: str | None = Field(default=None, min_length=1, max_length=500)


class ReviewChange(RevisionedContract):
    change_id: ContractId
    supporting_operation_ids: list[ContractId] = Field(
        default_factory=list,
        max_length=128,
    )
    summary: str = Field(min_length=1, max_length=1000)
    entities: list[ReviewEntity] = Field(default_factory=list, max_length=256)
    before: str | None = Field(default=None, max_length=2000)
    after: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_semantic_value(self) -> Self:
        if self.change_id in self.supporting_operation_ids:
            raise ValueError("a review change cannot support itself")
        if len(self.supporting_operation_ids) != len(
            set(self.supporting_operation_ids)
        ):
            raise ValueError("supporting operation ids cannot contain duplicates")
        if self.before is None and self.after is None:
            raise ValueError("a review change requires before or after copy")
        if self.before == self.after:
            raise ValueError("a review change must describe a semantic difference")
        return self


class ReviewChangeGroup(RevisionedContract):
    group_id: ContractId
    summary: str = Field(min_length=1, max_length=1000)
    changes: list[ReviewChange] = Field(min_length=1, max_length=128)


class ConflictReviewItem(RevisionedContract):
    conflict_id: ContractId
    summary: str = Field(min_length=1, max_length=1000)
    resolution: str = Field(min_length=1, max_length=1000)
    affected_entities: list[ReviewEntity] = Field(default_factory=list, max_length=32)
    additional_affected_entities: str | None = Field(default=None, min_length=1, max_length=500)
    scope_label: str | None = Field(default=None, min_length=1, max_length=300)
    intervals: list[DisplayDetail] = Field(default_factory=list, max_length=12)
    additional_intervals: str | None = Field(default=None, min_length=1, max_length=500)
    alternatives: list[DisplayDetail] = Field(default_factory=list, max_length=12)
    additional_alternatives: str | None = Field(default=None, min_length=1, max_length=500)


class ExternalSyncReviewStatus(RevisionedContract):
    sync_id: ContractId
    provider_label: str = Field(min_length=1, max_length=100)
    status: Literal["not_configured", "pending", "delivering", "succeeded", "failed"]
    summary: str = Field(min_length=1, max_length=500)
    attempts: int = Field(ge=0)
    retry_at: datetime | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=64)
    recovery: Literal["retry", "reconnect", "inspect"] | None = None
    remote_id: str | None = Field(default=None, max_length=256)
    remote_version: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        if self.retry_at is not None and self.retry_at.tzinfo is None:
            raise ValueError("external sync retry time must be timezone-aware")
        if self.status == "succeeded":
            if self.error_code or self.recovery or self.retry_at:
                raise ValueError("successful sync cannot retain error or retry evidence")
        elif self.status == "not_configured":
            if self.error_code is None or self.recovery != "reconnect" or self.retry_at:
                raise ValueError("not-configured sync requires reconnect guidance")
        elif self.status == "failed":
            if self.error_code is None or self.recovery != "inspect" or self.retry_at:
                raise ValueError("failed sync requires terminal inspection guidance")
        elif self.error_code is not None:
            if self.recovery != "retry":
                raise ValueError("active sync error requires retry guidance")
        elif self.recovery is not None:
            raise ValueError("active sync cannot expose recovery without error evidence")
        return self


class ReviewBlockBase(RevisionedContract):
    kind: str
    block_id: ContractId
    title: str = Field(min_length=1, max_length=300)


class EntityReferencesBlock(ReviewBlockBase):
    kind: Literal["entity_references"] = "entity_references"
    entities: list[ReviewEntity] = Field(min_length=1, max_length=512)
    additional_entities: str | None = Field(default=None, min_length=1, max_length=500)


class TaskStructureSchedule(RevisionedContract):
    range_label: str = Field(min_length=1, max_length=500)
    duration_minutes: int = Field(ge=1, le=10_080)


class TaskStructureItem(RevisionedContract):
    task: ReviewEntity
    state: Literal["new", "existing"]
    estimate_minutes: int | None = Field(default=None, ge=1, le=10_080)
    deadline_at: datetime | None = None
    scheduled_blocks: list[TaskStructureSchedule] = Field(
        default_factory=list,
        max_length=128,
    )
    children: list["TaskStructureItem"] = Field(default_factory=list, max_length=128)


class TaskStructureWorkspace(RevisionedContract):
    workspace: ReviewEntity
    roots: list[TaskStructureItem] = Field(min_length=1, max_length=128)


class TaskStructureBlock(ReviewBlockBase):
    kind: Literal["task_structure"] = "task_structure"
    workspaces: list[TaskStructureWorkspace] = Field(min_length=1, max_length=128)


class GroupedChangesBlock(ReviewBlockBase):
    kind: Literal["grouped_changes"] = "grouped_changes"
    groups: list[ReviewChangeGroup] = Field(min_length=1, max_length=128)


class SemanticDiffBlock(ReviewBlockBase):
    kind: Literal["semantic_diff"] = "semantic_diff"
    changes: list[ReviewChange] = Field(min_length=1, max_length=128)


class ScheduleSummaryBlock(ReviewBlockBase):
    kind: Literal["schedule_summary"] = "schedule_summary"
    cadence: str = Field(min_length=1, max_length=300)
    range_label: str = Field(min_length=1, max_length=500)
    time_zone: str | None = Field(default=None, min_length=1, max_length=100)
    details: list[DisplayDetail] = Field(default_factory=list, max_length=12)


class RecurrenceSummaryBlock(ReviewBlockBase):
    kind: Literal["recurrence_summary"] = "recurrence_summary"
    cadence: str = Field(min_length=1, max_length=300)
    range_label: str = Field(min_length=1, max_length=500)
    scope_label: str = Field(min_length=1, max_length=300)
    time_zone: str | None = Field(default=None, min_length=1, max_length=100)
    exceptions: list[DisplayDetail] = Field(default_factory=list, max_length=12)
    additional_exceptions: str | None = Field(default=None, min_length=1, max_length=500)


class ConflictResolutionBlock(ReviewBlockBase):
    kind: Literal["conflict_resolution"] = "conflict_resolution"
    conflicts: list[ConflictReviewItem] = Field(min_length=1, max_length=128)


class AssumptionBlock(ReviewBlockBase):
    kind: Literal["assumptions"] = "assumptions"
    statements: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        min_length=1,
        max_length=64,
    )


class WarningBlock(ReviewBlockBase):
    kind: Literal["warning"] = "warning"
    severity: Literal["info", "warning", "critical"]
    message: str = Field(min_length=1, max_length=1000)


class SemanticFallbackBlock(ReviewBlockBase):
    kind: Literal["semantic_fallback"] = "semantic_fallback"
    summary: str = Field(min_length=1, max_length=1000)
    effects: list[ReviewChange] = Field(min_length=1, max_length=128)


class ExecutionStatusBlock(ReviewBlockBase):
    kind: Literal["execution_status"] = "execution_status"
    status: Literal["committed_pending_verification", "applied", "verification_failed"]
    summary: str = Field(min_length=1, max_length=500)
    verification_status: Literal["pending", "verified", "verification_failed"]
    verification_summary: str = Field(min_length=1, max_length=500)
    recovery: Literal["reverify", "compensating_review", "manual_inspection"] | None = None
    execution_id: ContractId
    command_id: ContractId
    idempotency_key: ContractId
    replayed: bool = False
    bindings: list[EntityBinding] = Field(default_factory=list, max_length=512)
    facts: list[ExecutionFact] = Field(default_factory=list, max_length=2048)
    sync_items: list[ExternalSyncReviewStatus] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_status_pair(self) -> Self:
        expected = {
            "committed_pending_verification": "pending",
            "applied": "verified",
            "verification_failed": "verification_failed",
        }[self.status]
        if self.verification_status != expected:
            raise ValueError("execution and verification review statuses must agree")
        if self.status == "verification_failed" and self.recovery is None:
            raise ValueError("verification failure requires bounded recovery")
        if self.status != "verification_failed" and self.recovery is not None:
            raise ValueError("only verification failure may expose verification recovery")
        return self


ReviewBlock: TypeAlias = Annotated[
    EntityReferencesBlock
    | TaskStructureBlock
    | GroupedChangesBlock
    | SemanticDiffBlock
    | ScheduleSummaryBlock
    | RecurrenceSummaryBlock
    | ConflictResolutionBlock
    | AssumptionBlock
    | WarningBlock
    | SemanticFallbackBlock
    | ExecutionStatusBlock,
    Field(discriminator="kind"),
]


class ReviewActionBinding(RevisionedContract):
    session_id: ContractId
    goal_id: ContractId
    goal_revision: Revision
    change_set_id: ContractId
    change_set_revision: Revision
    simulation_id: ContractId
    simulation_input_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    simulation_output_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    correlation_id: ContractId
    causation_id: ContractId


class ReviewActionBase(RevisionedContract):
    action: str
    event_id: ContractId
    idempotency_key: ContractId
    review_id: ContractId
    review_revision: Revision
    binding: ReviewActionBinding


class ApproveReviewAction(ReviewActionBase):
    action: Literal["approve"] = "approve"
    command_id: ContractId


class EditReviewAction(ReviewActionBase):
    action: Literal["edit"] = "edit"
    correction: str | None = Field(default=None, min_length=1, max_length=10_000)


class RejectReviewAction(ReviewActionBase):
    action: Literal["reject"] = "reject"


class DecisionReviewAction(ReviewActionBase):
    action: Literal["decision"] = "decision"
    decision_id: ContractId
    option_id: ContractId


class RetryReviewAction(ReviewActionBase):
    action: Literal["retry"] = "retry"
    execution_id: ContractId
    original_command_id: ContractId
    original_idempotency_key: ContractId


ReviewAction: TypeAlias = Annotated[
    ApproveReviewAction
    | EditReviewAction
    | RejectReviewAction
    | DecisionReviewAction
    | RetryReviewAction,
    Field(discriminator="action"),
]


class ReviewInteractionResult(RevisionedContract):
    status: Literal[
        "execution_complete",
        "edit_requested",
        "rejected",
        "decision_recorded",
        "retry_replayed",
    ]
    review_id: ContractId
    review_revision: Revision
    session_id: ContractId
    goal_id: ContractId
    change_set_id: ContractId
    summary: str = Field(min_length=1, max_length=500)
    goal_revision: Revision
    change_set_revision: Revision
    correction: str | None = Field(default=None, max_length=10_000)
    execution: ExecutionOutcome | None = None

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        requires_execution = self.status in {"execution_complete", "retry_replayed"}
        if requires_execution != (self.execution is not None):
            raise ValueError("review action execution evidence contradicts its status")
        if (self.status == "edit_requested") != (self.correction is not None):
            if self.correction is not None:
                raise ValueError("only edit results may carry correction text")
        if self.execution is not None and self.execution.result is not None:
            result = self.execution.result
            if (
                result.session_id != self.session_id
                or result.goal_id != self.goal_id
                or result.goal_revision != self.goal_revision
                or result.change_set_id != self.change_set_id
                or result.change_set_revision != self.change_set_revision
            ):
                raise ValueError("execution evidence does not match the review interaction")
        return self


class ReviewActionError(RevisionedContract):
    code: Literal["stale_review", "authorization_denied", "invalid_action", "system_failure"]
    summary: str = Field(min_length=1, max_length=500)
    recovery: Literal["refresh", "revise", "retry", "none"]

    @model_validator(mode="after")
    def validate_recovery(self) -> Self:
        allowed = {
            "stale_review": {"refresh"},
            "authorization_denied": {"none"},
            "invalid_action": {"refresh", "revise", "none"},
            "system_failure": {"retry"},
        }[self.code]
        if self.recovery not in allowed:
            raise ValueError("review error recovery contradicts its code")
        return self


class ReviewActionOutcome(RevisionedContract):
    status: Literal["ok", "error"]
    result: ReviewInteractionResult | None = None
    error: ReviewActionError | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status == "ok" and (self.result is None or self.error is not None):
            raise ValueError("successful review action requires only a result")
        if self.status == "error" and (self.error is None or self.result is not None):
            raise ValueError("failed review action requires only an error")
        return self


class ReviewDocument(RevisionedContract):
    review_id: ContractId
    session_id: ContractId
    goal_id: ContractId
    goal_revision: Revision
    change_set_id: ContractId
    change_set_revision: Revision
    simulation_id: ContractId
    simulation_input_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    simulation_output_fingerprint: Fingerprint = Field(min_length=32, max_length=128)
    correlation_id: ContractId
    causation_id: ContractId
    summary: str = Field(min_length=1, max_length=2000)
    blocks: list[ReviewBlock] = Field(min_length=1, max_length=384)
    decisions: list[DecisionRequest] = Field(default_factory=list, max_length=16)
    controls: list[ReviewAction] = Field(default_factory=list, max_length=98)

    @model_validator(mode="after")
    def validate_identity_and_controls(self) -> Self:
        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("review block IDs must be unique")
        valid_decision_options: set[tuple[str, str]] = set()
        for decision in self.decisions:
            if (
                decision.status != "open"
                or decision.goal_id != self.goal_id
                or decision.goal_revision != self.goal_revision
            ):
                raise ValueError("review decisions must target the exact goal revision")
            valid_decision_options.update(
                (decision.decision_id, option.option_id) for option in decision.options
            )
        event_ids = [control.event_id for control in self.controls]
        idempotency_keys = [control.idempotency_key for control in self.controls]
        if len(event_ids) != len(set(event_ids)) or len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError("review control event and idempotency identities must be unique")
        for control in self.controls:
            if control.review_id != self.review_id or control.review_revision != self.revision:
                raise ValueError("review controls must target the exact document revision")
            binding = control.binding
            expected = (
                self.session_id,
                self.goal_id,
                self.goal_revision,
                self.change_set_id,
                self.change_set_revision,
                self.simulation_id,
                self.simulation_input_fingerprint,
                self.simulation_output_fingerprint,
                self.correlation_id,
                self.causation_id,
            )
            actual = (
                binding.session_id,
                binding.goal_id,
                binding.goal_revision,
                binding.change_set_id,
                binding.change_set_revision,
                binding.simulation_id,
                binding.simulation_input_fingerprint,
                binding.simulation_output_fingerprint,
                binding.correlation_id,
                binding.causation_id,
            )
            if actual != expected:
                raise ValueError("review control binding does not match the document")
            if isinstance(control, DecisionReviewAction) and (
                control.decision_id,
                control.option_id,
            ) not in valid_decision_options:
                raise ValueError("decision control must reference a displayed stored option")
        logical_controls = [
            (
                control.action,
                getattr(control, "decision_id", None),
                getattr(control, "option_id", None),
            )
            for control in self.controls
        ]
        if len(logical_controls) != len(set(logical_controls)):
            raise ValueError("review controls must be logically unique")
        if self.decisions and any(control.action == "approve" for control in self.controls):
            raise ValueError("open decisions cannot expose approval controls")
        return self
