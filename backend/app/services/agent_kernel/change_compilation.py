"""Deterministic server compilation of Strategist intent and Planner artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode
from app.models.preference_memory import UserPreference
from app.services.agent_kernel.change_set_authoring import (
    ChangeSetAuthorizationError,
    ChangeSetAuthoringContextError,
    ChangeSetConservationError,
    ChangeSetResolutionError,
    ChangeSetSchedulingEvidenceError,
    NativeChangeSetAuthoringCoordinator,
    TrustedScheduleEvidence,
)
from app.services.agent_kernel.change_set_store import (
    ActiveChangeSetConflict,
    ChangeSetRevisionConflict,
)
from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    CalendarDeleteBlockOperation,
    CalendarMoveBlockOperation,
    ChangeSetSimulationResult,
    Constraint,
    ConstraintScope,
    DraftChangeSetCommand,
    DraftChangeSetResult,
    EntityRef,
    ExactTimeConstraintValue,
    GoalStatePatch,
    GoalStateUpdateCommand,
    GoalState,
    Operation,
    Preconditions,
    PreferenceScalarValue,
    PreferenceDeleteOperation,
    PreferenceRetireOperation,
    PreferenceRestoreOperation,
    PreferenceSemantics,
    PreferenceStringSetValue,
    PreferenceSupersedeOperation,
    PreferenceTimeWindowValue,
    PreferenceUpsertOperation,
    Provenance,
    RecurrenceCreateSeriesOperation,
    RecurrenceRule,
    RequestedOperationClause,
    ReviewDocument,
    SimulateChangeSetCommand,
    TaskArchiveOperation,
    TaskCreateOperation,
    TaskDeleteOperation,
    TaskFields,
    TaskMoveOperation,
    TaskUpdateOperation,
    TaskUpdatePatch,
    parse_operation,
)
from app.services.agent_kernel.contracts.common import ContractModel
from app.services.agent_kernel.operation_conservation import operation_fingerprint
from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator
from app.services.agent_kernel.presentation import (
    ReviewDocumentProjector,
    ReviewProjectionError,
)
from app.services.agent_kernel.simulation_coordinator import (
    ChangeSetSimulationCoordinator,
    ChangeSetSimulationError,
)
from app.services.agent_kernel.state_store import (
    GoalStateNotFound,
    GoalStateRevisionConflict,
    GoalStateStore,
)
from app.services.agent_kernel.subagent_delegation import (
    PlannerSubagentResult,
    PlannerSubagentTask,
)
from app.services.agent_kernel.task_patch import build_task_patch_draft
from app.services.preference_values import (
    decode_preference_value,
    preference_family,
    preference_rules_overlap,
    preference_subject,
)


CompilationStatus = Literal[
    "succeeded",
    "needs_model_repair",
    "needs_user_decision",
    "stale_evidence",
    "authorization_denied",
    "retryable_failure",
    "fatal_invariant",
]


class ChangeCompilationRequest(ContractModel):
    """Small host-facing command; operation construction stays server-owned."""

    expected_goal_revision: int = Field(ge=1)
    expected_change_set_revision: int | None = Field(default=None, ge=1)


class ChangeCompilationIssue(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1, max_length=1_000)
    source: Literal["strategist", "planner", "server"]
    retryable: bool = False


class PlannerScheduleEvidence(ContractModel):
    """Server-attested digest of one independently validated Planner effect."""

    operation_id: str = Field(min_length=1, max_length=128)
    operation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_ref: str = Field(min_length=1, max_length=256)


class PlannerOperationBinding(ContractModel):
    """Attested owned-task binding for one adapted Planner operation."""

    operation_id: str = Field(min_length=1, max_length=128)
    task_ref: EntityRef

    @model_validator(mode="after")
    def validate_task_ref(self) -> "PlannerOperationBinding":
        if self.task_ref.kind != "task":
            raise ValueError("Planner operation bindings require persisted task refs")
        return self


class PlannerChangeArtifact(ContractModel):
    """Exact native effects returned by one owned, completed Planner task."""

    subagent_task_id: str = Field(min_length=1, max_length=128)
    parent_run_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    source_goal_revision: int = Field(ge=1)
    task_content_fingerprint: str = Field(min_length=16, max_length=64)
    operations: list[Operation] = Field(default_factory=list, max_length=128)
    constraints: list[Constraint] = Field(default_factory=list, max_length=128)
    unscheduled_task_refs: list[EntityRef] = Field(default_factory=list, max_length=128)
    evidence_handles: list[str] = Field(default_factory=list, max_length=32)
    schedule_evidence: list[PlannerScheduleEvidence] = Field(
        default_factory=list,
        max_length=128,
    )
    operation_bindings: list[PlannerOperationBinding] = Field(
        default_factory=list,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_unscheduled_refs(self) -> "PlannerChangeArtifact":
        identities = [
            (item.kind, item.target_kind, item.entity_id)
            for item in self.unscheduled_task_refs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("unscheduled task references cannot contain duplicates")
        if any(
            (item.target_kind if item.kind == "temporary" else item.kind) != "task"
            for item in self.unscheduled_task_refs
        ):
            raise ValueError("unscheduled work must reference tasks")
        return self


class SemanticPlanArtifact(ContractModel):
    """Server-normalized semantic intent ready to be stored on GoalState."""

    clauses: list[RequestedOperationClause] = Field(min_length=1, max_length=128)
    unscheduled_task_refs: list[EntityRef] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_unique_semantic_identities(self) -> "SemanticPlanArtifact":
        clause_ids = [item.clause_id for item in self.clauses]
        if len(clause_ids) != len(set(clause_ids)):
            raise ValueError("semantic plan clause IDs must be unique")
        unscheduled_identities = [
            (item.kind, item.target_kind, item.entity_id)
            for item in self.unscheduled_task_refs
        ]
        if len(unscheduled_identities) != len(set(unscheduled_identities)):
            raise ValueError(
                "semantic plan unscheduled task references must be unique"
            )
        return self


class ChangeCompilationManifest(ContractModel):
    strategist_operation_ids: list[str] = Field(default_factory=list)
    planner_operation_ids: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)
    constraint_ids: list[str] = Field(default_factory=list)
    unscheduled_task_refs: list[EntityRef] = Field(default_factory=list)
    source_subagent_task_id: str | None = None


class ChangeCompilationOutcome(ContractModel):
    status: CompilationStatus
    draft: DraftChangeSetResult | None = None
    manifest: ChangeCompilationManifest = Field(
        default_factory=ChangeCompilationManifest
    )
    issues: list[ChangeCompilationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_closed_outcome(self) -> "ChangeCompilationOutcome":
        if self.status == "succeeded":
            if self.draft is None or self.issues:
                raise ValueError("successful compilation requires one draft and no issues")
        elif self.draft is not None:
            raise ValueError("failed compilation cannot expose execution input")
        return self


class CompiledReviewOutcome(ContractModel):
    status: CompilationStatus
    compilation: ChangeCompilationOutcome
    simulation: ChangeSetSimulationResult | None = None
    review_document: ReviewDocument | None = None
    issues: list[ChangeCompilationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_lineage(self) -> "CompiledReviewOutcome":
        if self.review_document is not None:
            if self.simulation is None or self.compilation.draft is None:
                raise ValueError("review requires compilation and simulation lineage")
            change_set = self.compilation.draft.change_set
            if (
                self.review_document.change_set_id != change_set.change_set_id
                or self.review_document.change_set_revision != change_set.revision
                or self.review_document.simulation_id != self.simulation.simulation_id
            ):
                raise ValueError("review lineage differs from the compiled ChangeSet")
        return self


def _issue(
    code: str,
    path: str,
    message: str,
    source: Literal["strategist", "planner", "server"],
    *,
    retryable: bool = False,
) -> ChangeCompilationIssue:
    return ChangeCompilationIssue(
        code=code,
        path=path,
        message=message,
        source=source,
        retryable=retryable,
    )


def _ref_identity(ref: EntityRef) -> tuple[str, str]:
    return (
        str(ref.target_kind if ref.kind == "temporary" else ref.kind),
        ref.entity_id,
    )


def _operation_ref_identities(operation: Operation) -> set[tuple[str, str]]:
    value = operation.model_dump(mode="python", by_alias=False)
    identities: set[tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("kind") and item.get("entity_id"):
                kind = (
                    item.get("target_kind")
                    if item.get("kind") == "temporary"
                    else item.get("kind")
                )
                if kind:
                    identities.add((str(kind), str(item["entity_id"])))
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return identities


def _bind_planner_provenance(
    operation: Operation,
    clauses_by_id: dict[str, RequestedOperationClause],
) -> Operation:
    if operation.provenance is not None:
        return operation
    provenances = {
        clause.provenance.model_dump_json()
        for clause_id in operation.fulfills_clause_ids
        if (clause := clauses_by_id.get(clause_id)) is not None
    }
    if len(provenances) != 1:
        return operation
    provenance = Provenance.model_validate_json(next(iter(provenances)))
    return parse_operation(
        {
            **operation.model_dump(mode="python", by_alias=True),
            "provenance": provenance.model_dump(mode="python", by_alias=True),
        }
    )


class ServerChangeCompilationCoordinator:
    """Compile one goal and optional owned Planner artifact into one ChangeSet."""

    def __init__(self, session: Session):
        self.session = session
        self.goal_store = GoalStateStore(session)
        self.authoring = NativeChangeSetAuthoringCoordinator(session)
        self.simulation = ChangeSetSimulationCoordinator(session)

    def compile(
        self,
        session_id: str,
        request: ChangeCompilationRequest,
        *,
        command_id: str,
        correlation_id: str,
        causation_id: str,
        planner_artifact: PlannerChangeArtifact | None = None,
        expected_planner_task: PlannerSubagentTask | None = None,
    ) -> ChangeCompilationOutcome:
        try:
            goal = self.goal_store.load_active(session_id)
        except GoalStateNotFound:
            goal = None
        if goal is None:
            return self._failed(
                "stale_evidence",
                _issue(
                    "active_goal_missing",
                    "goal",
                    "There is no active goal to compile.",
                    "server",
                ),
            )
        if goal.revision != request.expected_goal_revision:
            return self._failed(
                "stale_evidence",
                _issue(
                    "goal_revision_stale",
                    "expectedGoalRevision",
                    (
                        f"Expected goal revision {request.expected_goal_revision}, "
                        f"but the active revision is {goal.revision}."
                    ),
                    "server",
                    retryable=True,
                ),
            )

        clauses = goal.requested_operation_clauses()
        if not clauses:
            return self._failed(
                "needs_model_repair",
                _issue(
                    "requested_operations_missing",
                    "operations.requested",
                    "Record the complete requested semantic plan before compilation.",
                    "strategist",
                ),
            )
        unresolved = [item for item in clauses if item.disposition == "unresolved"]
        if unresolved:
            return self._failed(
                "needs_user_decision",
                *[
                    _issue(
                        "requested_clause_unresolved",
                        f"operations.requested.{item.clause_id}",
                        "Resolve the stored user decision before compiling this clause.",
                        "strategist",
                    )
                    for item in unresolved
                ],
            )
        unsupported = [item for item in clauses if item.disposition == "unsupported"]
        if unsupported:
            return self._failed(
                "needs_model_repair",
                *[
                    _issue(
                        "requested_clause_unsupported",
                        f"operations.requested.{item.clause_id}",
                        "The requested effect is not supported by the current compiler.",
                        "strategist",
                    )
                    for item in unsupported
                ],
            )

        strategist_clauses = [
            item
            for item in clauses
            if item.disposition == "requested"
            and item.operation_source == "strategist"
        ]
        planner_clauses = [
            item
            for item in clauses
            if item.disposition == "requested"
            and item.operation_source == "planner"
        ]
        satisfied_planner_clauses = [
            item
            for item in clauses
            if item.disposition == "satisfied"
            and item.operation_source == "planner"
        ]
        missing_strategist = [
            item for item in strategist_clauses if not item.expected_operations
        ]
        if missing_strategist:
            return self._failed(
                "needs_model_repair",
                *[
                    _issue(
                        "strategist_operations_missing",
                        f"operations.requested.{item.clause_id}.expectedOperations",
                        "Supply the exact semantic task or preference effect.",
                        "strategist",
                    )
                    for item in missing_strategist
                ],
            )
        if (planner_clauses or satisfied_planner_clauses) and planner_artifact is None:
            return self._failed(
                "needs_model_repair",
                _issue(
                    "planner_artifact_missing",
                    "plannerArtifact",
                    "Complete the owned Planner task before compiling delegated scheduling.",
                    "planner",
                    retryable=True,
                ),
            )
        if planner_artifact is not None and expected_planner_task is None:
            return self._failed(
                "authorization_denied",
                _issue(
                    "planner_task_binding_missing",
                    "plannerArtifact.subagentTaskId",
                    "Planner output requires its exact host-owned task binding.",
                    "server",
                ),
            )
        if planner_artifact is not None and (
            planner_artifact.goal_id != goal.goal_id
            or planner_artifact.goal_revision != goal.revision
        ):
            return self._failed(
                "stale_evidence",
                _issue(
                    "planner_artifact_stale",
                    "plannerArtifact.goalRevision",
                    "The Planner artifact does not belong to the active goal revision.",
                    "planner",
                    retryable=True,
                ),
            )
        if planner_artifact is not None and expected_planner_task is not None:
            task = expected_planner_task
            identity_matches = (
                planner_artifact.subagent_task_id == task.task_id
                and planner_artifact.parent_run_id == task.parent.parent_run_id
                and planner_artifact.goal_id == task.parent.goal_id
                and planner_artifact.source_goal_revision == task.parent.goal_revision
                and planner_artifact.task_content_fingerprint
                == task.content_fingerprint
            )
            revision_matches = goal.revision in {
                task.parent.goal_revision,
                task.parent.goal_revision + 1,
            }
            if not identity_matches or not revision_matches:
                return self._failed(
                    "stale_evidence",
                    _issue(
                        "planner_task_binding_stale",
                        "plannerArtifact",
                        (
                            "Planner output does not match the exact assigned task, "
                            "parent run, fingerprint, goal, and source revision."
                        ),
                        "planner",
                        retryable=True,
                    ),
                )
            all_unscheduled = (
                bool(satisfied_planner_clauses)
                and not planner_clauses
                and not planner_artifact.operations
                and not planner_artifact.constraints
                and {
                    _ref_identity(ref)
                    for clause in satisfied_planner_clauses
                    for ref in clause.target_refs
                }
                == {
                    _ref_identity(ref)
                    for ref in planner_artifact.unscheduled_task_refs
                }
            )
            if not planner_clauses and not all_unscheduled:
                return self._failed(
                    "authorization_denied",
                    _issue(
                        "planner_delegation_missing",
                        "operations.requested",
                        "Planner output has no trusted delegated scheduling clause.",
                        "planner",
                    ),
                )
            assigned_task_refs = {
                ("task", f"task:{task_id}") for task_id in task.task_ids
            }
            if len(planner_artifact.operation_bindings) != len(
                {
                    item.operation_id
                    for item in planner_artifact.operation_bindings
                }
            ):
                return self._failed(
                    "authorization_denied",
                    _issue(
                        "planner_operation_binding_duplicate",
                        "plannerArtifact.operationBindings",
                        "Each Planner operation must have exactly one task binding.",
                        "planner",
                    ),
                )
            bindings_by_operation = {
                item.operation_id: item.task_ref
                for item in planner_artifact.operation_bindings
            }
            planner_operation_ids = {
                item.operation_id for item in planner_artifact.operations
            }
            if set(bindings_by_operation) != planner_operation_ids:
                return self._failed(
                    "authorization_denied",
                    _issue(
                        "planner_operation_binding_incomplete",
                        "plannerArtifact.operationBindings",
                        "Every Planner operation requires exactly one owned-task binding.",
                        "planner",
                    ),
                )
            for operation in planner_artifact.operations:
                task_ref = bindings_by_operation[operation.operation_id]
                if _ref_identity(task_ref) not in assigned_task_refs:
                    return self._failed(
                        "authorization_denied",
                        _issue(
                            "planner_task_scope_expanded",
                            f"plannerArtifact.operations.{operation.operation_id}",
                            "Planner output targets a task outside its assigned scope.",
                        "planner",
                    ),
                )
                task_refs = {
                    ref
                    for ref in _operation_ref_identities(operation)
                    if ref[0] == "task"
                }
                if (
                    operation.kind == "calendar.create_block"
                    and task_refs != {_ref_identity(task_ref)}
                ):
                    return self._failed(
                        "authorization_denied",
                        _issue(
                            "planner_task_scope_mismatch",
                            f"plannerArtifact.operations.{operation.operation_id}",
                            (
                                "Planner calendar-block creation must reference "
                                "the exact task named by its owned-task binding."
                            ),
                            "planner",
                        ),
                    )
                block_refs = [
                    ref
                    for ref in _operation_ref_identities(operation)
                    if ref[0] == "calendar_block"
                ]
                for _kind, entity_id in block_refs:
                    local_id = entity_id.removeprefix("calendar_block:")
                    try:
                        block = self.session.get(CalendarBlock, int(local_id))
                    except (TypeError, ValueError):
                        block = None
                    expected_task_id = int(
                        task_ref.entity_id.removeprefix("task:")
                    )
                    if block is None or block.task_id != expected_task_id:
                        return self._failed(
                            "authorization_denied",
                            _issue(
                                "planner_block_scope_mismatch",
                                f"plannerArtifact.operations.{operation.operation_id}",
                                (
                                    "Planner calendar-block effects must belong to "
                                    "the exact assigned task."
                                ),
                                "planner",
                            ),
                        )

        clauses_by_id = {item.clause_id: item for item in clauses}
        strategist_operations = [
            operation
            for clause in strategist_clauses
            for operation in clause.expected_operations
        ]
        planner_operations = (
            [
                _bind_planner_provenance(operation, clauses_by_id)
                for operation in planner_artifact.operations
            ]
            if planner_artifact is not None
            else []
        )
        source_by_operation_id: dict[str, str] = {}
        operations_by_id: dict[str, Operation] = {}
        ordered_operations: list[Operation] = []
        issues: list[ChangeCompilationIssue] = []
        for source, operations in (
            ("strategist", strategist_operations),
            ("planner", planner_operations),
        ):
            for operation in operations:
                existing = operations_by_id.get(operation.operation_id)
                if existing is not None:
                    if (
                        operation_fingerprint(existing).sha256
                        != operation_fingerprint(operation).sha256
                    ):
                        issues.append(
                            _issue(
                                "operation_identity_conflict",
                                f"operations.{operation.operation_id}",
                                (
                                    "The same operation ID was used for different "
                                    "semantic effects."
                                ),
                                source,  # type: ignore[arg-type]
                            )
                        )
                    continue
                operations_by_id[operation.operation_id] = operation
                source_by_operation_id[operation.operation_id] = source
                ordered_operations.append(operation)
        if issues:
            return self._failed("needs_model_repair", *issues)

        planner_clause_ids = {item.clause_id for item in planner_clauses}
        if planner_artifact is not None:
            for operation in planner_operations:
                linked = set(operation.fulfills_clause_ids)
                if not linked or not linked.issubset(planner_clause_ids):
                    issues.append(
                        _issue(
                            "planner_unrequested_effect",
                            f"plannerArtifact.operations.{operation.operation_id}",
                            (
                                "Planner output must fulfill only scheduling clauses "
                                "delegated by the active Strategist goal."
                            ),
                            "planner",
                        )
                    )
        if issues:
            return self._failed("needs_model_repair", *issues)

        explicit_unscheduled_refs = (
            list(planner_artifact.unscheduled_task_refs)
            if planner_artifact is not None
            else []
        )
        produced_task_refs = {
            _ref_identity(operation.temp_ref)
            for operation in strategist_operations
            if operation.kind == "task.create" and operation.temp_ref is not None
        }
        planner_touched = set().union(
            *(_operation_ref_identities(operation) for operation in planner_operations)
        ) if planner_operations else set()
        calendar_or_recurrence_touched = set().union(
            *(
                _operation_ref_identities(operation)
                for operation in ordered_operations
                if operation.kind.startswith("calendar.")
                or operation.kind.startswith("recurrence.")
            )
        ) if any(
            operation.kind.startswith("calendar.")
            or operation.kind.startswith("recurrence.")
            for operation in ordered_operations
        ) else set()
        derived_unscheduled_refs = [
            operation.temp_ref
            for operation in strategist_operations
            if operation.kind == "task.create"
            and operation.temp_ref is not None
            and _ref_identity(operation.temp_ref) not in calendar_or_recurrence_touched
        ]
        unscheduled_refs = list(
            {
                (ref.kind, ref.target_kind, ref.entity_id): ref
                for ref in [
                    *derived_unscheduled_refs,
                    *explicit_unscheduled_refs,
                ]
            }.values()
        )
        for ref in explicit_unscheduled_refs:
            identity = _ref_identity(ref)
            if identity in planner_touched:
                issues.append(
                    _issue(
                        "unscheduled_task_was_scheduled",
                        f"plannerArtifact.unscheduledTaskRefs.{ref.entity_id}",
                        "A task cannot be both intentionally unscheduled and scheduled.",
                        "planner",
                    )
                )
            if ref.kind == "temporary" and identity not in produced_task_refs:
                issues.append(
                    _issue(
                        "unscheduled_task_has_no_create",
                        f"plannerArtifact.unscheduledTaskRefs.{ref.entity_id}",
                        "Temporary unscheduled work must be created by Strategist intent.",
                        "planner",
                    )
                )
        if issues:
            return self._failed("needs_model_repair", *issues)

        if planner_artifact is not None and planner_artifact.constraints:
            allowed_operation_ids = {
                operation.operation_id for operation in planner_operations
            }
            allowed_refs = set().union(
                *(
                    _operation_ref_identities(operation)
                    for operation in planner_operations
                )
            ) if planner_operations else set()
            if expected_planner_task is not None:
                allowed_refs.update(
                    ("task", f"task:{task_id}")
                    for task_id in expected_planner_task.task_ids
                )
            for constraint in planner_artifact.constraints:
                scopes = [
                    scope
                    for scope in (constraint.scope, constraint.object_scope)
                    if scope is not None
                ]
                if any(scope.kind == "goal" for scope in scopes) or any(
                    not set(scope.operation_ids).issubset(allowed_operation_ids)
                    or not {
                        _ref_identity(ref) for ref in scope.entity_refs
                    }.issubset(allowed_refs)
                    for scope in scopes
                ):
                    return self._failed(
                        "authorization_denied",
                        _issue(
                            "planner_constraint_scope_expanded",
                            (
                                "plannerArtifact.constraints."
                                f"{constraint.constraint_id}"
                            ),
                            (
                                "Planner constraints must be bound to delegated "
                                "operations or entities in the owned task scope."
                            ),
                            "planner",
                        ),
                    )

        constraints = self._merge_constraints(
            goal.constraints,
            planner_artifact.constraints if planner_artifact is not None else [],
        )
        if isinstance(constraints, ChangeCompilationIssue):
            return self._failed("needs_model_repair", constraints)
        assumptions = [
            item.provenance
            for item in goal.assumptions
            if item.status == "active"
        ]
        preconditions = Preconditions(
            expected_entity_revisions=list(
                {
                    (ref.kind, ref.entity_id, ref.revision): ref
                    for ref in goal.resolved_refs
                    if ref.revision is not None and ref.kind != "temporary"
                }.values()
            )
        )
        current = self.authoring.store.load_current(session_id, goal.goal_id)
        if current is None:
            if request.expected_change_set_revision is not None:
                return self._failed(
                    "stale_evidence",
                    _issue(
                        "change_set_revision_unexpected",
                        "expectedChangeSetRevision",
                        "No current ChangeSet exists for the supplied revision.",
                        "server",
                    ),
                )
            disposition = "create"
        else:
            if request.expected_change_set_revision != current.revision:
                return self._failed(
                    "stale_evidence",
                    _issue(
                        "change_set_revision_stale",
                        "expectedChangeSetRevision",
                        (
                            f"Expected ChangeSet revision "
                            f"{request.expected_change_set_revision}, "
                            f"but the current revision is {current.revision}."
                        ),
                        "server",
                        retryable=True,
                    ),
                )
            disposition = "revise"

        command = DraftChangeSetCommand(
            disposition=disposition,
            expected_goal_revision=goal.revision,
            expected_change_set_revision=(
                current.revision if disposition == "revise" else None
            ),
            operations=ordered_operations,
            constraints=constraints,
            preconditions=preconditions,
            assumptions=assumptions,
        )
        planner_evidence = (
            [
                TrustedScheduleEvidence(
                    operation_id=item.operation_id,
                    operation_sha256=item.operation_sha256,
                    source_ref=item.source_ref,
                )
                for item in planner_artifact.schedule_evidence
            ]
            if planner_artifact is not None
            else []
        )
        exact_planner_ids = {
            operation.operation_id
            for operation in planner_operations
            if operation.kind
            in {
                "calendar.create_block",
                "calendar.move_block",
                "calendar.resize_block",
                "calendar.split_block",
            }
        }
        if {item.operation_id for item in planner_evidence} != exact_planner_ids:
            return self._failed(
                "authorization_denied",
                _issue(
                    "planner_schedule_evidence_incomplete",
                    "plannerArtifact.scheduleEvidence",
                    (
                        "Every exact Planner placement requires one independently "
                        "validated, digest-bound evidence record."
                    ),
                    "planner",
                ),
            )
        try:
            draft = self.authoring.draft(
                session_id,
                command,
                command_id=command_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                additional_trusted_schedule_evidence=planner_evidence,
            )
        except ChangeSetAuthorizationError as error:
            return self._failed(
                "authorization_denied",
                _issue(
                    "entity_authorization_failed",
                    "operations",
                    str(error),
                    "server",
                ),
            )
        except (ChangeSetConservationError, ValueError) as error:
            return self._failed(
                "needs_model_repair",
                _issue(
                    "compiled_plan_invalid",
                    "operations",
                    str(error),
                    "server",
                ),
            )
        except (GoalStateNotFound, GoalStateRevisionConflict) as error:
            return self._failed(
                "stale_evidence",
                _issue(
                    "compilation_state_stale",
                    "goal",
                    str(error),
                    "server",
                    retryable=True,
                ),
            )
        except (ActiveChangeSetConflict, ChangeSetRevisionConflict) as error:
            return self._failed(
                "stale_evidence",
                _issue(
                    "change_set_revision_conflict",
                    "changeSet",
                    str(error),
                    "server",
                    retryable=True,
                ),
            )
        except (
            ChangeSetAuthoringContextError,
            ChangeSetResolutionError,
            ChangeSetSchedulingEvidenceError,
        ) as error:
            return self._failed(
                "needs_model_repair",
                _issue(
                    "change_set_authoring_invalid",
                    "operations",
                    str(error),
                    "server",
                ),
            )

        return ChangeCompilationOutcome(
            status="succeeded",
            draft=draft,
            manifest=ChangeCompilationManifest(
                strategist_operation_ids=[
                    item.operation_id for item in strategist_operations
                ],
                planner_operation_ids=[
                    item.operation_id for item in planner_operations
                ],
                clause_ids=[item.clause_id for item in clauses],
                constraint_ids=[item.constraint_id for item in constraints],
                unscheduled_task_refs=unscheduled_refs,
                source_subagent_task_id=(
                    planner_artifact.subagent_task_id
                    if planner_artifact is not None
                    else None
                ),
            ),
        )

    def compile_semantic_plan(
        self,
        session_id: str,
        request: ChangeCompilationRequest,
        plan: SemanticPlanArtifact,
        *,
        command_id: str,
        correlation_id: str,
        causation_id: str,
        planner_artifact: PlannerChangeArtifact | None = None,
        expected_planner_task: PlannerSubagentTask | None = None,
    ) -> ChangeCompilationOutcome:
        """Atomically store one normalized plan and compile its exact revision."""

        try:
            current = self.goal_store.load_active(session_id)
        except GoalStateNotFound:
            current = None
        if current is None or current.revision != request.expected_goal_revision:
            return self._failed(
                "stale_evidence",
                _issue(
                    "goal_revision_stale",
                    "expectedGoalRevision",
                    "The semantic plan does not target the active goal revision.",
                    "server",
                    retryable=True,
                ),
            )
        recurrence_slot = next(
            (
                slot
                for slot in current.semantic_slots
                if slot.path == "recurrence.rule"
                and slot.semantic_value.state in {"present", "inferred"}
            ),
            None,
        )
        plan_operation_kinds = {
            operation.kind
            for clause in plan.clauses
            for operation in clause.expected_operations
        } | {
            clause.operation_kind
            for clause in plan.clauses
            if clause.operation_kind is not None
        }
        if recurrence_slot is not None and not any(
            kind.startswith("recurrence.")
            for kind in plan_operation_kinds
        ):
            return self._failed(
                "needs_model_repair",
                _issue(
                    "declared_recurrence_missing_from_plan",
                    "recurrence.rule",
                    (
                        "The model-declared recurring goal is not represented by a "
                        "recurrence operation in the semantic plan."
                    ),
                    "strategist",
                ),
            )
        provenance = plan.clauses[0].provenance
        transaction = self.session.begin_nested()
        try:
            updated = GoalStateCoordinator(self.session).apply_model_update(
                session_id=session_id,
                source_turn_id=causation_id,
                correlation_id=correlation_id,
                current_run_id=None,
                command=GoalStateUpdateCommand(
                    command_id=f"{command_id}:goal-plan",
                    disposition="correct",
                    goal_id=current.goal_id,
                    expected_revision=current.revision,
                    patches=[
                        GoalStatePatch(
                            path="operations.requested",
                            operation="set",
                            value=[
                                item.model_dump(mode="json", by_alias=True)
                                for item in plan.clauses
                            ],
                            provenance=provenance,
                        )
                    ],
                ),
                commit=False,
            )
        except GoalStateRevisionConflict as error:
            transaction.rollback()
            self.session.expire_all()
            return self._failed(
                "stale_evidence",
                _issue(
                    "semantic_plan_state_stale",
                    "operations.requested",
                    str(error),
                    "server",
                    retryable=True,
                ),
            )
        try:
            compiled = self.compile(
                session_id,
                ChangeCompilationRequest(
                    expected_goal_revision=updated.revision,
                    expected_change_set_revision=request.expected_change_set_revision,
                ),
                command_id=f"{command_id}:changeset",
                correlation_id=correlation_id,
                causation_id=causation_id,
                planner_artifact=planner_artifact,
                expected_planner_task=expected_planner_task,
            )
        except Exception:
            transaction.rollback()
            self.session.expire_all()
            raise
        if compiled.status != "succeeded":
            transaction.rollback()
            self.session.expire_all()
            return compiled
        transaction.commit()
        manifest = compiled.manifest.model_copy(
            update={
                "unscheduled_task_refs": list(
                    {
                        (ref.kind, ref.target_kind, ref.entity_id): ref
                        for ref in [
                            *compiled.manifest.unscheduled_task_refs,
                            *plan.unscheduled_task_refs,
                        ]
                    }.values()
                )
            }
        )
        return compiled.model_copy(update={"manifest": manifest})

    def compile_and_review(
        self,
        session_id: str,
        request: ChangeCompilationRequest,
        *,
        command_id: str,
        correlation_id: str,
        causation_id: str,
        planner_artifact: PlannerChangeArtifact | None = None,
        expected_planner_task: PlannerSubagentTask | None = None,
    ) -> CompiledReviewOutcome:
        compilation = self.compile(
            session_id,
            request,
            command_id=f"{command_id}:compile",
            correlation_id=correlation_id,
            causation_id=causation_id,
            planner_artifact=planner_artifact,
            expected_planner_task=expected_planner_task,
        )
        if compilation.status != "succeeded" or compilation.draft is None:
            return CompiledReviewOutcome(
                status=compilation.status,
                compilation=compilation,
                issues=compilation.issues,
            )
        try:
            simulation = self.simulation.simulate(
                session_id,
                SimulateChangeSetCommand(
                    expected_goal_revision=compilation.draft.goal_revision,
                    expected_change_set_revision=(
                        compilation.draft.change_set.revision
                    ),
                ),
                command_id=f"{command_id}:simulate",
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        except ChangeSetSimulationError as error:
            issue = _issue(
                "simulation_failed",
                "simulation",
                str(error),
                "server",
                retryable=True,
            )
            return CompiledReviewOutcome(
                status="retryable_failure",
                compilation=compilation,
                issues=[issue],
            )
        if simulation.status not in {"ready_for_review", "needs_user_decision"}:
            status: CompilationStatus = (
                "stale_evidence"
                if simulation.status == "stale"
                else "needs_model_repair"
            )
            issues = [
                _issue(
                    str(item.code).lower(),
                    "simulation",
                    item.summary,
                    "server",
                    retryable=item.category == "retryable_system",
                )
                for item in simulation.issues
            ] or [
                _issue(
                    "simulation_not_reviewable",
                    "simulation",
                    "The compiled ChangeSet did not produce a reviewable world.",
                    "server",
                )
            ]
            return CompiledReviewOutcome(
                status=status,
                compilation=compilation,
                simulation=simulation,
                issues=issues,
            )
        goal = self.goal_store.load(session_id, simulation.goal_id)
        change_set = self.authoring.store.load(
            session_id, simulation.change_set_id
        )
        try:
            review = ReviewDocumentProjector().project(
                session_id=session_id,
                goal=goal,
                change_set=change_set,
                simulation=simulation,
            )
        except ReviewProjectionError as error:
            issue = _issue(
                "review_projection_failed",
                "review",
                str(error),
                "server",
            )
            return CompiledReviewOutcome(
                status="fatal_invariant",
                compilation=compilation,
                simulation=simulation,
                issues=[issue],
            )
        return CompiledReviewOutcome(
            status=(
                "needs_user_decision"
                if simulation.status == "needs_user_decision"
                else "succeeded"
            ),
            compilation=compilation,
            simulation=simulation,
            review_document=review,
        )

    @staticmethod
    def _merge_constraints(
        goal_constraints: list[Constraint],
        planner_constraints: list[Constraint],
    ) -> list[Constraint] | ChangeCompilationIssue:
        ordered: list[Constraint] = []
        by_id: dict[str, Constraint] = {}
        for source, values in (
            ("strategist", goal_constraints),
            ("planner", planner_constraints),
        ):
            for constraint in values:
                prior = by_id.get(constraint.constraint_id)
                if prior is not None:
                    if prior != constraint:
                        return _issue(
                            "constraint_identity_conflict",
                            f"constraints.{constraint.constraint_id}",
                            (
                                "The same constraint ID was used for different "
                                "constraint semantics."
                            ),
                            source,  # type: ignore[arg-type]
                        )
                    continue
                by_id[constraint.constraint_id] = constraint
                ordered.append(constraint)
        return ordered

    @staticmethod
    def _failed(
        status: CompilationStatus,
        *issues: ChangeCompilationIssue,
    ) -> ChangeCompilationOutcome:
        return ChangeCompilationOutcome(status=status, issues=list(issues))


class PlannerScheduleArtifactAdapter:
    """Adapt validated SchedulePatch output into the native compiler boundary."""

    def delegated_clauses(
        self,
        task: PlannerSubagentTask,
        *,
        operation_mode: str,
        provenance: Provenance,
    ) -> list[RequestedOperationClause]:
        normalized_mode = operation_mode.strip().lower()
        operation_kind = {
            "create": "calendar.create_block",
            "schedule": "calendar.create_block",
            "reschedule": "calendar.move_block",
            "move": "calendar.move_block",
            "remove": "calendar.delete_block",
            "delete": "calendar.delete_block",
        }.get(normalized_mode)
        if operation_kind is None:
            raise ValueError(
                f"Planner task has unsupported operation mode {operation_mode!r}"
            )
        return [
            RequestedOperationClause(
                clause_id=self._delegated_clause_id(
                    task.task_id,
                    operation_kind,
                    target_ref,
                ),
                operation_kind=operation_kind,  # type: ignore[arg-type]
                operation_source="planner",
                cardinality="zero_or_one",
                target_refs=[target_ref],
                provenance=provenance,
            )
            for target_ref in (
                EntityRef(kind="task", entity_id=f"task:{task_id}")
                for task_id in task.task_ids
            )
        ]

    @staticmethod
    def finalize_clauses(
        clauses: list[RequestedOperationClause],
        artifact: PlannerChangeArtifact,
    ) -> list[RequestedOperationClause]:
        """Mark explicitly skipped task clauses satisfied without inventing effects."""

        unscheduled = {
            _ref_identity(ref) for ref in artifact.unscheduled_task_refs
        }
        fulfilled = {
            clause_id
            for operation in artifact.operations
            for clause_id in operation.fulfills_clause_ids
        }
        operations_by_clause = {
            clause.clause_id: [
                operation
                for operation in artifact.operations
                if clause.clause_id in operation.fulfills_clause_ids
            ]
            for clause in clauses
        }
        finalized: list[RequestedOperationClause] = []
        for clause in clauses:
            targets = {_ref_identity(ref) for ref in clause.target_refs}
            if targets and targets.issubset(unscheduled):
                finalized.append(clause.model_copy(update={"disposition": "satisfied"}))
                continue
            if clause.clause_id not in fulfilled:
                raise ValueError(
                    "Planner result neither fulfills nor explicitly skips a delegated task"
                )
            # The delegated scope is task-owned, while move/delete operations are
            # necessarily block-addressed. Ownership is already proven by the
            # PlannerOperationBinding, so conserve the exact native mutation target
            # here instead of comparing unlike task/block identities.
            operation_targets = sorted(
                set().union(
                    *(
                        _operation_ref_identities(operation)
                        for operation in operations_by_clause[clause.clause_id]
                    )
                )
            )
            finalized.append(
                clause.model_copy(
                    update={
                        "target_refs": [
                            EntityRef(kind=kind, entity_id=entity_id)  # type: ignore[arg-type]
                            for kind, entity_id in operation_targets
                        ]
                    }
                )
            )
        return finalized

    def adapt(
        self,
        result: PlannerSubagentResult,
        *,
        task: PlannerSubagentTask,
        target_goal_revision: int,
        planner_clauses: list[RequestedOperationClause],
        timezone_name: str,
        request_constraints: list[dict[str, Any]] | None = None,
    ) -> PlannerChangeArtifact:
        if result.status != "completed":
            raise ValueError("Planner artifact requires a completed subagent result")
        if (
            result.task_id != task.task_id
            or result.content_fingerprint != task.content_fingerprint
            or result.parent_run_id != task.parent.parent_run_id
            or result.goal_id != task.parent.goal_id
            or result.goal_revision != task.parent.goal_revision
        ):
            raise ValueError(
                "Planner artifact ownership does not match the exact assigned task"
            )
        proposal = self._proposal(result)
        validation = proposal.get("schedulePatchValidation")
        if not isinstance(validation, dict):
            raise ValueError("Planner schedule artifact has no typed validation")
        if validation.get("errors"):
            raise ValueError("Planner schedule artifact contains validation errors")
        if str(validation.get("status") or "") not in {"valid", "warning"}:
            raise ValueError("Planner schedule artifact is not validated")
        patch = proposal.get("schedulePatch")
        operations = patch.get("operations") if isinstance(patch, dict) else None
        if not isinstance(operations, list) or not operations:
            raise ValueError("Planner schedule artifact has no operations")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Planner schedule artifact has an invalid timezone") from error

        compiled: list[Operation] = []
        unscheduled: list[EntityRef] = []
        bindings: list[PlannerOperationBinding] = []
        assigned_task_ids = set(task.task_ids)
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise ValueError(f"Planner operation {index + 1} is malformed")
            kind = str(raw.get("op") or "").strip()
            task_id = raw.get("taskId")
            if isinstance(task_id, bool) or not isinstance(task_id, int):
                raise ValueError(f"Planner operation {index + 1} has no exact task ID")
            if task_id not in assigned_task_ids:
                raise ValueError(
                    f"Planner operation {index + 1} targets an unassigned task"
                )
            task_ref = EntityRef(kind="task", entity_id=f"task:{task_id}")
            if kind == "skip_task":
                unscheduled.append(task_ref)
                continue
            if kind == "create_block":
                native_kind = "calendar.create_block"
                target_ref = task_ref
            elif kind == "update_block_time":
                native_kind = "calendar.move_block"
                target_ref = self._block_ref(raw, index=index)
            elif kind == "remove_block":
                native_kind = "calendar.delete_block"
                target_ref = self._block_ref(raw, index=index)
            else:
                raise ValueError(
                    f"Planner operation {index + 1} uses unsupported kind {kind!r}"
                )
            clause = self._match_clause(
                planner_clauses,
                operation_kind=native_kind,
                target_ref=task_ref,
            )
            operation_id = self._operation_id(result.task_id, index, raw)
            common = {
                "operation_id": operation_id,
                "fulfills_clause_ids": [clause.clause_id],
                "provenance": clause.provenance,
            }
            if native_kind == "calendar.create_block":
                compiled.append(
                    CalendarCreateBlockOperation(
                        **common,
                        task_ref=task_ref,
                        start_at=self._datetime(raw.get("startAt"), zone),
                        end_at=self._datetime(raw.get("endAt"), zone),
                    )
                )
            elif native_kind == "calendar.move_block":
                compiled.append(
                    CalendarMoveBlockOperation(
                        **common,
                        block_ref=target_ref,
                        start_at=self._datetime(raw.get("startAt"), zone),
                        end_at=self._datetime(raw.get("endAt"), zone),
                    )
                )
            else:
                compiled.append(
                    CalendarDeleteBlockOperation(
                        **common,
                        block_refs=[target_ref],
                    )
                )
            bindings.append(
                PlannerOperationBinding(
                    operation_id=operation_id,
                    task_ref=task_ref,
                )
            )
        validation_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "taskFingerprint": task.content_fingerprint,
                    "schedulePatch": patch,
                    "schedulePatchValidation": validation,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        schedule_evidence = [
            PlannerScheduleEvidence(
                operation_id=operation.operation_id,
                operation_sha256=operation_fingerprint(operation).sha256,
                validation_sha256=validation_sha256,
                source_ref=f"planner-subagent:{task.task_id}",
            )
            for operation in compiled
            if operation.kind
            in {
                "calendar.create_block",
                "calendar.move_block",
                "calendar.resize_block",
                "calendar.split_block",
            }
        ]
        compiled_constraints: list[Constraint] = []
        for index, raw_constraint in enumerate(request_constraints or []):
            if (
                not isinstance(raw_constraint, dict)
                or raw_constraint.get("kind") != "exact_start"
                or raw_constraint.get("hard", True) is not True
            ):
                continue
            raw_task_ids = raw_constraint.get("task_ids")
            if not isinstance(raw_task_ids, list) or len(raw_task_ids) != 1:
                continue
            task_id = raw_task_ids[0]
            if isinstance(task_id, bool) or not isinstance(task_id, int):
                continue
            exact_start = self._datetime(raw_constraint.get("start_at"), zone)
            matching = [
                operation
                for operation, binding in zip(compiled, bindings, strict=True)
                if binding.task_ref.entity_id == f"task:{task_id}"
                and getattr(operation, "start_at", None) == exact_start
                and isinstance(getattr(operation, "end_at", None), datetime)
            ]
            for operation in matching:
                end_at = getattr(operation, "end_at")
                duration_minutes = int(
                    (end_at - exact_start).total_seconds() // 60
                )
                if duration_minutes <= 0 or operation.provenance is None:
                    continue
                constraint_key = hashlib.sha256(
                    (
                        f"{result.task_id}\x1f{index}\x1f{operation.operation_id}"
                        f"\x1f{exact_start.isoformat()}"
                    ).encode("utf-8")
                ).hexdigest()[:24]
                compiled_constraints.append(
                    Constraint(
                        constraint_id=f"constraint:planner-exact:{constraint_key}",
                        kind="exact_time",
                        strength="hard",
                        scope=ConstraintScope(
                            kind="operation_set",
                            operation_ids=[operation.operation_id],
                        ),
                        value=ExactTimeConstraintValue(
                            start_at=exact_start,
                            duration_minutes=duration_minutes,
                        ),
                        provenance=operation.provenance,
                    )
                )
        return PlannerChangeArtifact(
            subagent_task_id=result.task_id,
            parent_run_id=result.parent_run_id,
            goal_id=result.goal_id,
            goal_revision=target_goal_revision,
            source_goal_revision=result.goal_revision,
            task_content_fingerprint=result.content_fingerprint,
            operations=compiled,
            constraints=compiled_constraints,
            unscheduled_task_refs=unscheduled,
            evidence_handles=list(task.evidence_handles),
            schedule_evidence=schedule_evidence,
            operation_bindings=bindings,
        )

    @staticmethod
    def _proposal(result: PlannerSubagentResult) -> dict[str, Any]:
        artifact = result.artifact
        if not isinstance(artifact, dict):
            raise ValueError("Planner completed without an artifact")
        planner_result = artifact.get("planner_result")
        if isinstance(planner_result, dict):
            proposal = planner_result.get("proposal")
        else:
            proposal = artifact.get("proposal")
        if not isinstance(proposal, dict):
            raise ValueError("Planner completed without a schedule proposal")
        return proposal

    @staticmethod
    def _datetime(value: Any, zone: ZoneInfo) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Planner schedule operation has no exact datetime")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Planner schedule operation has an invalid datetime") from error
        return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed

    @staticmethod
    def _block_ref(value: dict[str, Any], *, index: int) -> EntityRef:
        block_id = value.get("acceptedBlockId")
        if isinstance(block_id, bool) or not isinstance(block_id, int):
            raise ValueError(
                f"Planner operation {index + 1} has no accepted calendar block ID"
            )
        return EntityRef(
            kind="calendar_block",
            entity_id=f"calendar_block:{block_id}",
        )

    @staticmethod
    def _match_clause(
        clauses: list[RequestedOperationClause],
        *,
        operation_kind: str,
        target_ref: EntityRef,
    ) -> RequestedOperationClause:
        target = _ref_identity(target_ref)
        matches = [
            item
            for item in clauses
            if item.operation_source == "planner"
            and item.operation_kind == operation_kind
            and target in {_ref_identity(ref) for ref in item.target_refs}
        ]
        if len(matches) != 1:
            raise ValueError(
                "Planner schedule operation does not match exactly one delegated clause"
            )
        return matches[0]

    @staticmethod
    def _delegated_clause_id(
        task_id: str,
        operation_kind: str,
        target_ref: EntityRef,
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [
                    task_id,
                    operation_kind,
                    target_ref.model_dump(mode="json", by_alias=True),
                ],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"clause:planner:{digest}"

    @staticmethod
    def _operation_id(
        task_id: str,
        index: int,
        operation: dict[str, Any],
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(
                operation,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:20]
        return f"planner-op:{task_id.split(':')[-1]}:{index}:{digest}"


class CompatibilityProposalAdapter:
    """Convert validated proposal IR into one server-compilable semantic plan."""

    _STATUS_MAP = {
        "todo": "not_started",
        "not_started": "not_started",
        "not-started": "not_started",
        "in_progress": "in_progress",
        "in-progress": "in_progress",
        "blocked": "blocked",
        "done": "completed",
        "completed": "completed",
        "archived": "archived",
    }
    _WEEKDAY_MAP = {
        0: "monday",
        1: "tuesday",
        2: "wednesday",
        3: "thursday",
        4: "friday",
        5: "saturday",
        6: "sunday",
    }

    @staticmethod
    def _task_create_workspace_ids(
        raw_operations: list[dict[str, Any]],
        *,
        session: Session | None,
    ) -> dict[str, int]:
        """Resolve the workspace owned by every created task tree node.

        A task parent owns the workspace of its descendants.  Proposal sources
        may redundantly repeat a workspace on each created node, but that value
        cannot override the parent relationship.  Resolve this at the native
        compiler boundary so every proposal path observes the same invariant.
        """
        creates_by_temp: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, raw in enumerate(raw_operations):
            if str(raw.get("op") or "") != "create":
                continue
            raw_temp = str(raw.get("temp_id") or f"generated-{index}").strip()
            creates_by_temp[raw_temp] = (index, raw)

        resolved: dict[str, int] = {}
        resolving: set[str] = set()

        def resolve(raw_temp: str) -> int:
            if raw_temp in resolved:
                return resolved[raw_temp]
            if raw_temp in resolving:
                raise ValueError("Created task parent relationships contain a cycle")
            indexed = creates_by_temp.get(raw_temp)
            if indexed is None:
                raise ValueError(f"Task operation references unknown parent_temp_id {raw_temp!r}")
            index, raw = indexed
            resolving.add(raw_temp)
            try:
                parent_temp = str(raw.get("parent_temp_id") or "").strip()
                if parent_temp:
                    if parent_temp not in creates_by_temp:
                        raise ValueError(
                            f"Task operation {index + 1} references unknown parent_temp_id "
                            f"{parent_temp!r}"
                        )
                    workspace_id = resolve(parent_temp)
                else:
                    parent_id = raw.get("parent_id")
                    parent = (
                        session.get(StrategyNode, parent_id)
                        if session is not None
                        and isinstance(parent_id, int)
                        and not isinstance(parent_id, bool)
                        else None
                    )
                    workspace_id = (
                        parent.category_id if parent is not None else raw.get("category_id")
                    )
                    if isinstance(workspace_id, bool) or not isinstance(workspace_id, int):
                        raise ValueError("Every created task requires an exact workspace ID")
                resolved[raw_temp] = workspace_id
                return workspace_id
            finally:
                resolving.discard(raw_temp)

        for raw_temp in creates_by_temp:
            resolve(raw_temp)
        return resolved

    def adapt_preference_proposal(
        self,
        *,
        proposal: dict[str, Any],
        provenance: Provenance,
        timezone_name: str,
        session: Session | None = None,
    ) -> SemanticPlanArtifact:
        patch = proposal.get("preference_patch", proposal.get("preferencePatch"))
        operations = patch.get("operations") if isinstance(patch, dict) else None
        if not isinstance(operations, list) or not operations:
            raise ValueError(
                "Preference semantic input has no validated operations"
            )
        operations = self._consolidate_preference_operations(operations)
        clauses: list[RequestedOperationClause] = []
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise ValueError("Preference operation must be an object")
            legacy_kind = str(raw.get("op") or "").strip()
            if legacy_kind in {
                "retire_preference",
                "restore_preference",
                "delete_preference",
            }:
                if session is None:
                    raise ValueError(
                        "Preference management requires authoritative saved-preference state"
                    )
                body = raw.get("preference")
                if not isinstance(body, dict):
                    raise ValueError(
                        "Preference management operation is missing exact preference identity"
                    )
                preference_id = str(
                    body.get("id")
                    or body.get("preferenceId")
                    or body.get("preference_id")
                    or ""
                ).strip().removeprefix("preference:")
                revision = body.get("revision")
                if (
                    not preference_id
                    or not isinstance(revision, int)
                    or isinstance(revision, bool)
                    or revision < 1
                ):
                    raise ValueError(
                        "Preference management requires an exact preference id and revision"
                    )
                saved = session.get(UserPreference, preference_id)
                if saved is None or saved.user_id != "local":
                    raise ValueError("Saved preference is unavailable to this user")
                if saved.revision != revision:
                    raise ValueError(
                        "Saved preference changed after it was read; read it again before proposing changes"
                    )
                if legacy_kind == "retire_preference" and not saved.active:
                    raise ValueError("Saved preference is already disabled")
                if legacy_kind == "restore_preference" and saved.active:
                    raise ValueError("Saved preference is already active")
                operation_kind = {
                    "retire_preference": "preference.retire",
                    "restore_preference": "preference.restore",
                    "delete_preference": "preference.delete",
                }[legacy_kind]
                clause_id = self._stable_id(
                    "clause",
                    "preference-management",
                    legacy_kind,
                    preference_id,
                    index,
                )
                operation_type = {
                    "retire_preference": PreferenceRetireOperation,
                    "restore_preference": PreferenceRestoreOperation,
                    "delete_preference": PreferenceDeleteOperation,
                }[legacy_kind]
                replacement_ref = None
                replacement = (
                    body.get("replacement")
                    if isinstance(body.get("replacement"), dict)
                    else None
                )
                if replacement is not None:
                    replacement_id = str(
                        replacement.get("id")
                        or replacement.get("preferenceId")
                        or replacement.get("preference_id")
                        or ""
                    ).strip().removeprefix("preference:")
                    replacement_revision = replacement.get("revision")
                    replacement_row = session.get(UserPreference, replacement_id)
                    if (
                        not replacement_id
                        or not isinstance(replacement_revision, int)
                        or isinstance(replacement_revision, bool)
                        or replacement_row is None
                        or replacement_row.user_id != "local"
                        or replacement_row.revision != replacement_revision
                        or not replacement_row.active
                    ):
                        raise ValueError(
                            "Preference cleanup replacement changed after analysis"
                        )
                    replacement_ref = EntityRef(
                        kind="preference",
                        entity_id=f"preference:{replacement_id}",
                        revision=replacement_revision,
                        title=(
                            str(replacement.get("description") or "").strip()
                            or replacement_row.description
                        ),
                    )
                management_fields = (
                    {
                        "replacement_ref": replacement_ref,
                        "reason_code": str(
                            body.get("reasonCode")
                            or body.get("reason_code")
                            or ""
                        ).strip()
                        or None,
                    }
                    if legacy_kind in {
                        "retire_preference",
                        "delete_preference",
                    }
                    else {}
                )
                operation = operation_type(
                    operation_id=self._stable_id(
                        "operation",
                        "preference-management",
                        legacy_kind,
                        preference_id,
                        index,
                    ),
                    preference_ref=EntityRef(
                        kind="preference",
                        entity_id=f"preference:{preference_id}",
                        revision=revision,
                    ),
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                    **management_fields,
                )
                clauses.append(
                    RequestedOperationClause(
                        clause_id=clause_id,
                        operation_kind=operation_kind,
                        cardinality="exactly_one",
                        target_refs=[operation.preference_ref],
                        expected_operations=[operation],
                        provenance=provenance,
                    )
                )
                continue
            if legacy_kind == "create_preference":
                body = raw.get("preference")
                if not isinstance(body, dict):
                    raise ValueError("Preference operation is missing preference meaning")
                preference_key = str(
                    body.get("preferenceType")
                    or body.get("preference_type")
                    or "general"
                ).strip()
                strength = str(body.get("strength") or "").strip().lower()
                scope = str(
                    body.get("scopeType") or body.get("scope_type") or "global"
                ).strip().lower()
                domain = str(body.get("domain") or "general").strip().lower()
                raw_value = body.get("value")
            elif legacy_kind == "create_constraint":
                body = raw.get("constraint")
                if not isinstance(body, dict):
                    raise ValueError("Constraint operation is missing constraint meaning")
                constraint_kind = str(
                    body.get("constraintType")
                    or body.get("constraint_type")
                    or "general"
                ).strip()
                preference_key = f"constraint.{constraint_kind}"
                strength = "hard" if body.get("hard") is True else "soft"
                scope = str(
                    body.get("targetType") or body.get("target_type") or "global"
                ).strip().lower()
                domain = "scheduling"
                raw_value = body.get("value")
            else:
                raise ValueError(
                    f"Unsupported preference semantic operation: {legacy_kind or 'unknown'}"
                )

            scope = {
                "category": "workspace",
                "workspace": "workspace",
                "task": "task",
                "calendar": "calendar",
            }.get(scope, "global")
            domain = domain[:64] or "general"
            clause_id = self._stable_id(
                "clause",
                "preference",
                legacy_kind,
                preference_key,
                index,
            )
            preference_group_id = self._stable_id(
                "group",
                "preference",
                preference_key,
                index,
            )
            initial_related = self._active_related_preferences(
                session,
                preference_key=preference_key,
                raw_value=raw_value,
                scope=scope,
                description=str(body.get("description") or ""),
                domain=domain,
            )
            merge_source = next(
                (
                    candidate
                    for candidate in initial_related
                    if {
                        "category": "workspace",
                        "workspace": "workspace",
                        "task": "task",
                        "calendar": "calendar",
                    }.get(candidate.scope_type.strip().lower(), "global")
                    == scope
                ),
                None,
            )
            raw_value = self._merge_partial_preference_value(
                preference_key,
                raw_value,
                merge_source,
            )
            value = self._preference_value(
                raw_value,
                timezone_name=timezone_name,
            )
            semantics = PreferenceSemantics(
                domain=domain,
                strength=(
                    "hard"
                    if strength in {"hard", "strong", "required"}
                    else "soft"
                ),
                scope=scope,
            )
            related = self._active_related_preferences(
                session,
                preference_key=preference_key,
                raw_value=raw_value,
                scope=scope,
                description=str(body.get("description") or ""),
                domain=domain,
            )
            if related:
                replaced = related[0]
                operation = PreferenceSupersedeOperation(
                    operation_id=self._stable_id(
                        "operation",
                        "preference",
                        "supersede",
                        preference_key,
                        index,
                    ),
                    atomic_group_id=preference_group_id,
                    preference_ref=EntityRef(
                        kind="preference",
                        entity_id=f"preference:{replaced.id}",
                        revision=replaced.revision,
                    ),
                    value=value,
                    semantics=semantics,
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                operation_kind = "preference.supersede"
            else:
                operation = PreferenceUpsertOperation(
                    operation_id=self._stable_id(
                        "operation",
                        "preference",
                        legacy_kind,
                        preference_key,
                        index,
                    ),
                    atomic_group_id=preference_group_id,
                    preference_key=preference_key,
                    value=value,
                    semantics=semantics,
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                operation_kind = "preference.upsert"
            clauses.append(
                RequestedOperationClause(
                    clause_id=clause_id,
                    operation_kind=operation_kind,
                    cardinality="exactly_one",
                    expected_operations=[operation],
                    provenance=provenance,
                )
            )
            for duplicate_index, duplicate in enumerate(related[1:], start=1):
                retire_clause_id = self._stable_id(
                    "clause",
                    "preference",
                    "retire-duplicate",
                    duplicate.id,
                    duplicate_index,
                )
                retire = PreferenceRetireOperation(
                    operation_id=self._stable_id(
                        "operation",
                        "preference",
                        "retire-duplicate",
                        duplicate.id,
                        duplicate_index,
                    ),
                    atomic_group_id=preference_group_id,
                    preference_ref=EntityRef(
                        kind="preference",
                        entity_id=f"preference:{duplicate.id}",
                        revision=duplicate.revision,
                    ),
                    reason_code="redundant_scoped_copy",
                    fulfills_clause_ids=[retire_clause_id],
                    provenance=provenance,
                )
                clauses.append(
                    RequestedOperationClause(
                        clause_id=retire_clause_id,
                        operation_kind="preference.retire",
                        cardinality="exactly_one",
                        target_refs=[retire.preference_ref],
                        expected_operations=[retire],
                        provenance=provenance,
                    )
                )
        return SemanticPlanArtifact(clauses=clauses)

    @staticmethod
    def _consolidate_preference_operations(
        operations: list[Any],
    ) -> list[dict[str, Any]]:
        """Keep one preference rule when a constraint is its enforcement form."""

        normalized = [
            copy.deepcopy(item) for item in operations if isinstance(item, dict)
        ]
        preferences_by_temp_id: dict[str, dict[str, Any]] = {}
        for operation in normalized:
            if str(operation.get("op") or "") != "create_preference":
                continue
            temp_id = str(
                operation.get("tempId") or operation.get("temp_id") or ""
            ).strip()
            if temp_id:
                preferences_by_temp_id[temp_id] = operation

        kept: list[dict[str, Any]] = []
        for operation in normalized:
            if str(operation.get("op") or "") != "create_constraint":
                kept.append(operation)
                continue
            source_temp_id = str(
                operation.get("sourcePreferenceTempId")
                or operation.get("source_preference_temp_id")
                or ""
            ).strip()
            source = preferences_by_temp_id.get(source_temp_id)
            constraint = operation.get("constraint")
            if source is None or not isinstance(constraint, dict):
                kept.append(operation)
                continue
            preference = source.get("preference")
            if not isinstance(preference, dict):
                kept.append(operation)
                continue
            current_value = preference.get("value")
            constraint_value = constraint.get("value")
            if isinstance(current_value, dict) and isinstance(constraint_value, dict):
                preference["value"] = {**constraint_value, **current_value}
            if constraint.get("hard") is True:
                preference["strength"] = "hard"
                if isinstance(preference.get("value"), dict):
                    constraint_type = str(
                        constraint.get("constraintType")
                        or constraint.get("constraint_type")
                        or ""
                    ).strip()
                    if constraint_type:
                        preference["value"][
                            "enforcementConstraintType"
                        ] = constraint_type
        return kept

    @staticmethod
    def _logical_preference_object(value: Any) -> dict[str, Any]:
        return decode_preference_value(value if isinstance(value, dict) else None)

    @classmethod
    def _merge_partial_preference_value(
        cls,
        preference_key: str,
        raw_value: Any,
        related: UserPreference | None,
    ) -> Any:
        """Apply a semantic field update without erasing compatible conditions.

        Natural-language updates commonly mention only the value being changed
        (for example, "make the gap 20 minutes").  Existing applicability is
        retained unless the user explicitly supplies a new condition or says
        that the rule applies to all tasks.
        """

        if (
            related is None
            or not isinstance(raw_value, dict)
            or cls._preference_family(preference_key, raw_value)
            != "inter_task_gap"
        ):
            return raw_value

        merged = copy.deepcopy(raw_value)
        condition_keys = {
            "minimumTaskDurationMinutes": (
                "minimumTaskDurationMinutes",
                "minimum_task_duration_minutes",
                "taskDurationAtLeastMinutes",
                "task_duration_at_least_minutes",
            ),
            "maximumTaskDurationMinutes": (
                "maximumTaskDurationMinutes",
                "maximum_task_duration_minutes",
                "taskDurationAtMostMinutes",
                "task_duration_at_most_minutes",
            ),
            "shorterTaskGapMinutes": (
                "shorterTaskGapMinutes",
                "shorter_task_gap_minutes",
            ),
        }
        if merged.get("appliesToAllTasks") is True:
            for aliases in condition_keys.values():
                for key in aliases:
                    merged.pop(key, None)
            return merged

        existing = decode_preference_value(related.value_json)
        for canonical, aliases in condition_keys.items():
            if any(key in merged for key in aliases):
                continue
            for key in aliases:
                if key in existing:
                    merged[canonical] = existing[key]
                    break
        return merged

    @classmethod
    def _preference_family(cls, preference_key: str, value: Any) -> str:
        return preference_family(preference_key, value)

    @classmethod
    def _active_related_preferences(
        cls,
        session: Session | None,
        *,
        preference_key: str,
        raw_value: Any,
        scope: str,
        description: str,
        domain: str,
    ) -> list[UserPreference]:
        """Resolve explicit semantic updates without comparing prose."""

        if session is None:
            return []
        family = cls._preference_family(preference_key, raw_value)
        subject = preference_subject(
            preference_key,
            raw_value,
            description=description,
            domain=domain,
        )
        candidates = session.exec(
            select(UserPreference)
            .where(UserPreference.user_id == "local")
            .where(UserPreference.active == True)  # noqa: E712
            .order_by(UserPreference.updated_at.desc(), UserPreference.created_at.desc())
        ).all()
        related: list[UserPreference] = []
        for candidate in candidates:
            try:
                stored_value = json.loads(candidate.value_json or "{}")
            except json.JSONDecodeError:
                stored_value = {}
            candidate_scope = {
                "category": "workspace",
                "workspace": "workspace",
                "task": "task",
                "calendar": "calendar",
            }.get(candidate.scope_type.strip().lower(), "global")
            if scope != "global" and candidate_scope != scope:
                continue
            candidate_subject = preference_subject(
                candidate.preference_type,
                stored_value,
                description=candidate.description or "",
                domain=candidate.domain,
            )
            if (
                candidate_subject == subject
                and preference_rules_overlap(family, raw_value, stored_value)
            ):
                related.append(candidate)
        # A same-scope rule is the canonical update target. Broader global
        # updates may still retire overlapping scoped copies, but a newer
        # scoped duplicate must not become the primary supersede target.
        return sorted(
            related,
            key=lambda candidate: (
                {
                    "category": "workspace",
                    "workspace": "workspace",
                    "task": "task",
                    "calendar": "calendar",
                }.get(candidate.scope_type.strip().lower(), "global")
                == scope
            ),
            reverse=True,
        )

    @staticmethod
    def _preference_value(
        value: Any,
        *,
        timezone_name: str,
    ) -> PreferenceScalarValue | PreferenceStringSetValue | PreferenceTimeWindowValue:
        if isinstance(value, dict):
            start = value.get("startTime", value.get("start_time"))
            end = value.get("endTime", value.get("end_time"))
            if isinstance(start, str) and isinstance(end, str):
                return PreferenceTimeWindowValue(
                    start_local=start,
                    end_local=end,
                    timezone=str(
                        value.get("timezone") or timezone_name or "UTC"
                    ),
                )
            text = value.get(
                "preferenceText",
                value.get("normalizedText", value.get("rule")),
            )
            has_structured_meaning = any(
                key in value
                for key in (
                    "interTaskGapMinutes",
                    "inter_task_gap_minutes",
                    "preferredInterTaskGapMinutes",
                    "preferred_inter_task_gap_minutes",
                    "gapMinutes",
                    "gap_minutes",
                    "bufferMinutes",
                    "buffer_minutes",
                    "minimumTaskDurationMinutes",
                    "minimum_task_duration_minutes",
                    "maximumTaskDurationMinutes",
                    "maximum_task_duration_minutes",
                    "shorterTaskGapMinutes",
                    "shorter_task_gap_minutes",
                    "appliesToAllTasks",
                )
            )
            if (
                isinstance(text, str)
                and text.strip()
                and not has_structured_meaning
            ):
                return PreferenceScalarValue(value=text.strip())
            return PreferenceScalarValue(
                value=json.dumps(
                    value,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) for item in value)
        ):
            return PreferenceStringSetValue(values=list(dict.fromkeys(value)))
        if isinstance(value, (str, int, float, bool)) or value is None:
            return PreferenceScalarValue(value=value)
        return PreferenceScalarValue(value=str(value))

    def adapt_task_proposal(
        self,
        *,
        proposal_kind: str,
        proposal: dict[str, Any],
        provenance: Provenance,
        timezone_name: str,
        session: Session | None = None,
    ) -> SemanticPlanArtifact:
        patch = build_task_patch_draft(
            proposal_kind=proposal_kind,
            proposal=proposal,
        )
        raw_operations = patch.get("operations")
        schedule_plan = proposal.get(
            "created_task_schedule_plan",
            proposal.get("createdTaskSchedulePlan"),
        )
        schedule_blocks = (
            schedule_plan.get("blocks")
            if isinstance(schedule_plan, dict)
            and schedule_plan.get("status") == "valid"
            else None
        )
        existing_schedule_only = bool(
            isinstance(raw_operations, list)
            and not raw_operations
            and isinstance(schedule_blocks, list)
            and schedule_blocks
            and all(
                isinstance(block, dict)
                and isinstance(block.get("target_task_id"), int)
                and not isinstance(block.get("target_task_id"), bool)
                and not str(block.get("target_temp_id") or "").strip()
                for block in schedule_blocks
            )
        )
        if (
            not isinstance(raw_operations, list)
            or (not raw_operations and not existing_schedule_only)
        ):
            raise ValueError("Compatibility proposal has no validated task operations")
        proposal_id = str(
            proposal.get("proposal_id")
            or proposal.get("proposalId")
            or patch.get("patch_id")
            or "proposal"
        )
        temp_refs: dict[str, EntityRef] = {}
        create_ids_by_temp: dict[str, str] = {}
        for index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict) or str(raw.get("op") or "") != "create":
                continue
            raw_temp = str(raw.get("temp_id") or f"generated-{index}").strip()
            temp_refs[raw_temp] = EntityRef(
                kind="temporary",
                entity_id=f"temp:{proposal_id}:{raw_temp}",
                target_kind="task",
                title=str(raw.get("title") or "").strip() or None,
            )
            create_ids_by_temp[raw_temp] = self._stable_id(
                "operation-task-create", proposal_id, index, raw
            )

        create_workspace_ids = self._task_create_workspace_ids(
            raw_operations,
            session=session,
        )

        clauses: list[RequestedOperationClause] = []
        task_creates: dict[str, TaskCreateOperation] = {}
        for index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict):
                raise ValueError(f"Task operation {index + 1} is malformed")
            op_kind = str(raw.get("op") or "").strip()
            clause_id = self._stable_id("clause-task", proposal_id, index, raw)
            operation_id = (
                create_ids_by_temp[
                    str(raw.get("temp_id") or f"generated-{index}").strip()
                ]
                if op_kind == "create"
                else self._stable_id("operation-task", proposal_id, index, raw)
            )
            operation: Operation
            if op_kind == "create":
                raw_temp = str(raw.get("temp_id") or f"generated-{index}").strip()
                temp_ref = temp_refs[raw_temp]
                parent_temp = str(raw.get("parent_temp_id") or "").strip()
                if parent_temp and parent_temp not in temp_refs:
                    raise ValueError(
                        f"Task operation {index + 1} references unknown parent_temp_id "
                        f"{parent_temp!r}"
                    )
                parent_id = raw.get("parent_id")
                parent_ref = (
                    temp_refs.get(parent_temp)
                    if parent_temp
                    else EntityRef(kind="task", entity_id=f"task:{parent_id}")
                    if isinstance(parent_id, int) and not isinstance(parent_id, bool)
                    else None
                )
                category_id = create_workspace_ids[raw_temp]
                operation = TaskCreateOperation(
                    operation_id=operation_id,
                    depends_on=(
                        [create_ids_by_temp[parent_temp]]
                        if parent_temp in create_ids_by_temp
                        else []
                    ),
                    title=str(raw.get("title") or "").strip(),
                    workspace_ref=EntityRef(
                        kind="workspace",
                        entity_id=f"workspace:{category_id}",
                    ),
                    parent_ref=parent_ref,
                    temp_ref=temp_ref,
                    fields=self._task_fields(raw.get("fields")),
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                task_creates[raw_temp] = operation
                target_refs = [temp_ref]
            elif op_kind == "update":
                task_ref = self._task_ref(raw.get("task_id"))
                operation = TaskUpdateOperation(
                    operation_id=operation_id,
                    task_ref=task_ref,
                    updates=self._task_update(raw.get("fields")),
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                target_refs = [task_ref]
            elif op_kind == "move":
                task_ref = self._task_ref(raw.get("task_id"))
                parent_temp = str(raw.get("parent_temp_id") or "").strip()
                if parent_temp and parent_temp not in temp_refs:
                    raise ValueError(
                        f"Task operation {index + 1} references unknown parent_temp_id "
                        f"{parent_temp!r}"
                    )
                parent_id = raw.get("parent_id")
                category_id = raw.get("category_id")
                parent_ref = (
                    temp_refs.get(parent_temp)
                    if parent_temp
                    else EntityRef(kind="task", entity_id=f"task:{parent_id}")
                    if isinstance(parent_id, int) and not isinstance(parent_id, bool)
                    else None
                )
                workspace_ref = (
                    EntityRef(
                        kind="workspace",
                        entity_id=f"workspace:{category_id}",
                    )
                    if parent_ref is None
                    and isinstance(category_id, int)
                    and not isinstance(category_id, bool)
                    else None
                )
                operation = TaskMoveOperation(
                    operation_id=operation_id,
                    depends_on=(
                        [create_ids_by_temp[parent_temp]]
                        if parent_temp in create_ids_by_temp
                        else []
                    ),
                    task_refs=[task_ref],
                    destination_parent_ref=parent_ref,
                    destination_workspace_ref=workspace_ref,
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                target_refs = [task_ref]
            elif op_kind == "delete":
                task_ref = self._task_ref(raw.get("task_id"))
                operation_type = (
                    TaskArchiveOperation
                    if str(raw.get("mode") or "") == "archive"
                    else TaskDeleteOperation
                )
                operation = operation_type(
                    operation_id=operation_id,
                    task_refs=[task_ref],
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                target_refs = [task_ref]
            else:
                raise ValueError(
                    f"Unsupported compatibility task operation {op_kind!r}"
                )
            clauses.append(
                RequestedOperationClause(
                    clause_id=clause_id,
                    operation_kind=operation.kind,
                    cardinality="exactly_one",
                    target_refs=target_refs,
                    expected_operations=[operation],
                    provenance=provenance,
                )
            )

        scheduled_temp_ids: set[str] = set()
        self._append_created_task_schedule(
            clauses=clauses,
            task_creates=task_creates,
            scheduled_temp_ids=scheduled_temp_ids,
            proposal=proposal,
            proposal_id=proposal_id,
            provenance=provenance,
            timezone_name=timezone_name,
            session=session,
        )
        self._append_created_task_recurrence(
            clauses=clauses,
            task_creates=task_creates,
            scheduled_temp_ids=scheduled_temp_ids,
            proposal=proposal,
            proposal_id=proposal_id,
            provenance=provenance,
            timezone_name=timezone_name,
            session=session,
        )
        return SemanticPlanArtifact(
            clauses=clauses,
            unscheduled_task_refs=[
                operation.temp_ref
                for raw_temp, operation in task_creates.items()
                if raw_temp not in scheduled_temp_ids
                and operation.temp_ref is not None
            ],
        )

    def _append_created_task_schedule(
        self,
        *,
        clauses: list[RequestedOperationClause],
        task_creates: dict[str, TaskCreateOperation],
        scheduled_temp_ids: set[str],
        proposal: dict[str, Any],
        proposal_id: str,
        provenance: Provenance,
        timezone_name: str,
        session: Session | None,
    ) -> None:
        plan = proposal.get(
            "created_task_schedule_plan",
            proposal.get("createdTaskSchedulePlan"),
        )
        if not isinstance(plan, dict):
            return
        if plan.get("status") != "valid":
            errors = [
                str(error).strip()
                for error in (plan.get("errors") or [])
                if str(error).strip()
            ]
            detail = "; ".join(dict.fromkeys(errors))
            raise ValueError(
                "Created-task scheduling plan is not valid"
                + (f": {detail}" if detail else "")
            )
        blocks = plan.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("Created-task scheduling plan has no blocks")
        zone = self._zone(timezone_name)
        by_temp: dict[str, list[CalendarCreateBlockOperation]] = {}
        by_existing: dict[
            tuple[str, Literal["calendar.create_block", "calendar.move_block"]],
            tuple[EntityRef, list[Operation]],
        ] = {}
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise ValueError("Created-task schedule block is malformed")
            raw_temp = str(block.get("target_temp_id") or "").strip()
            raw_task_id = block.get("target_task_id")
            create = task_creates.get(raw_temp) if raw_temp else None
            if create is not None and create.temp_ref is not None:
                task_ref = create.temp_ref
                depends_on = [create.operation_id]
                scheduled_temp_ids.add(raw_temp)
                target_key = raw_temp
            elif isinstance(raw_task_id, int) and not isinstance(raw_task_id, bool):
                task_ref = EntityRef(
                    kind="task",
                    entity_id=f"task:{raw_task_id}",
                )
                depends_on = []
                target_key = f"task:{raw_task_id}"
            else:
                raise ValueError(
                    "One-time schedule references neither a known temporary task nor "
                    "an existing task"
                )
            clause_id = self._stable_id("clause-calendar", proposal_id, target_key)
            if raw_temp:
                operation = CalendarCreateBlockOperation(
                    operation_id=self._stable_id(
                        "operation-calendar", proposal_id, index, block
                    ),
                    depends_on=depends_on,
                    task_ref=task_ref,
                    start_at=self._datetime(block.get("start_at"), zone),
                    end_at=self._datetime(block.get("end_at"), zone),
                    fulfills_clause_ids=[clause_id],
                    provenance=provenance,
                )
                by_temp.setdefault(raw_temp, []).append(operation)
            else:
                accepted_block_id = block.get("accepted_block_id")
                if isinstance(accepted_block_id, int) and not isinstance(
                    accepted_block_id, bool
                ):
                    block_ref = EntityRef(
                        kind="calendar_block",
                        entity_id=f"calendar_block:{accepted_block_id}",
                    )
                    operation_kind: Literal[
                        "calendar.create_block", "calendar.move_block"
                    ] = "calendar.move_block"
                    operation = CalendarMoveBlockOperation(
                        operation_id=self._stable_id(
                            "operation-calendar-move", proposal_id, index, block
                        ),
                        depends_on=[],
                        block_ref=block_ref,
                        start_at=self._datetime(block.get("start_at"), zone),
                        end_at=self._datetime(block.get("end_at"), zone),
                        fulfills_clause_ids=[clause_id],
                        provenance=provenance,
                    )
                    clause_target_ref = block_ref
                else:
                    operation_kind = "calendar.create_block"
                    operation = CalendarCreateBlockOperation(
                        operation_id=self._stable_id(
                            "operation-calendar", proposal_id, index, block
                        ),
                        depends_on=[],
                        task_ref=task_ref,
                        start_at=self._datetime(block.get("start_at"), zone),
                        end_at=self._datetime(block.get("end_at"), zone),
                        fulfills_clause_ids=[clause_id],
                        provenance=provenance,
                    )
                    clause_target_ref = task_ref
                group_key = (target_key, operation_kind)
                if group_key not in by_existing:
                    by_existing[group_key] = (clause_target_ref, [])
                by_existing[group_key][1].append(operation)
        for raw_temp, operations in by_temp.items():
            task_create = task_creates[raw_temp]
            task_ref = task_create.temp_ref
            assert task_ref is not None
            estimate_minutes = task_create.fields.estimate_minutes
            scheduled_minutes = sum(
                int((operation.end_at - operation.start_at).total_seconds() // 60)
                for operation in operations
            )
            if (
                estimate_minutes is not None
                and scheduled_minutes != estimate_minutes
            ):
                raise ValueError(
                    "Created-task scheduled duration must match its task estimate"
                )
            clauses.append(
                RequestedOperationClause(
                    clause_id=operations[0].fulfills_clause_ids[0],
                    operation_kind="calendar.create_block",
                    cardinality=(
                        "exactly_one" if len(operations) == 1 else "one_or_more"
                    ),
                    target_refs=[task_ref],
                    expected_operations=operations,
                    provenance=provenance,
                )
            )
        for (_target_key, operation_kind), (target_ref, operations) in by_existing.items():
            clauses.append(
                RequestedOperationClause(
                    clause_id=operations[0].fulfills_clause_ids[0],
                    operation_kind=operation_kind,
                    cardinality=(
                        "exactly_one" if len(operations) == 1 else "one_or_more"
                    ),
                    target_refs=[target_ref],
                    expected_operations=operations,
                    provenance=provenance,
                )
            )
        self._append_displacement_operations(
            clauses=clauses,
            raw_operations=plan.get("displacement_operations"),
            proposal_id=proposal_id,
            provenance=provenance,
            timezone_name=timezone_name,
            session=session,
        )

    def _append_created_task_recurrence(
        self,
        *,
        clauses: list[RequestedOperationClause],
        task_creates: dict[str, TaskCreateOperation],
        scheduled_temp_ids: set[str],
        proposal: dict[str, Any],
        proposal_id: str,
        provenance: Provenance,
        timezone_name: str,
        session: Session | None,
    ) -> None:
        recurring = proposal.get(
            "recurring_schedule",
            proposal.get("recurringSchedule"),
        )
        if not isinstance(recurring, dict):
            return
        raw_temp = str(recurring.get("target_temp_id") or "").strip()
        create = task_creates.get(raw_temp)
        if create is None or create.temp_ref is None:
            raise ValueError(
                "Recurring schedule references an unknown temporary task"
            )
        scheduled_temp_ids.add(raw_temp)
        clause_id = self._stable_id(
            "clause-recurrence", proposal_id, raw_temp
        )
        operation = RecurrenceCreateSeriesOperation(
            operation_id=self._stable_id(
                "operation-recurrence", proposal_id, recurring
            ),
            depends_on=[create.operation_id],
            task_ref=create.temp_ref,
            rule=self._recurrence_rule(
                recurring,
                fallback_timezone=timezone_name,
            ),
            temp_ref=EntityRef(
                kind="temporary",
                entity_id=f"temp:{proposal_id}:series:{raw_temp}",
                target_kind="recurrence_series",
            ),
            fulfills_clause_ids=[clause_id],
            provenance=provenance,
        )
        clauses.append(
            RequestedOperationClause(
                clause_id=clause_id,
                operation_kind="recurrence.create_series",
                cardinality="exactly_one",
                target_refs=[operation.temp_ref],
                expected_operations=[operation],
                provenance=provenance,
            )
        )
        plan = proposal.get(
            "recurring_schedule_plan",
            proposal.get("recurringSchedulePlan"),
        )
        if isinstance(plan, dict):
            self._append_displacement_operations(
                clauses=clauses,
                raw_operations=plan.get("displacement_operations"),
                proposal_id=proposal_id,
                provenance=provenance,
                timezone_name=timezone_name,
                session=session,
            )

    def _append_displacement_operations(
        self,
        *,
        clauses: list[RequestedOperationClause],
        raw_operations: Any,
        proposal_id: str,
        provenance: Provenance,
        timezone_name: str,
        session: Session | None,
    ) -> None:
        """Compile deterministic conflict moves with exact stale-state guards."""

        if raw_operations in (None, []):
            return
        if not isinstance(raw_operations, list):
            raise ValueError("Schedule displacement operations must be a list")
        if session is None:
            raise ValueError(
                "Schedule displacement compilation requires authoritative calendar state"
            )
        zone = self._zone(timezone_name)
        operations: list[CalendarMoveBlockOperation] = []
        block_refs: list[EntityRef] = []
        seen_block_ids: set[int] = set()
        for index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict):
                raise ValueError("Schedule displacement operation is malformed")
            if str(raw.get("op") or "").strip() != "update_block_time":
                raise ValueError("Schedule displacement uses an unsupported operation")
            block_id = raw.get("acceptedBlockId")
            task_id = raw.get("taskId")
            if (
                isinstance(block_id, bool)
                or not isinstance(block_id, int)
                or block_id in seen_block_ids
            ):
                raise ValueError(
                    "Every schedule displacement must target one unique accepted block"
                )
            if isinstance(task_id, bool) or not isinstance(task_id, int):
                raise ValueError("Schedule displacement has no exact task identity")
            block = session.get(CalendarBlock, block_id)
            if block is None or int(block.task_id) != task_id:
                raise ValueError(
                    "Schedule displacement target no longer belongs to the reviewed task"
                )
            expected_start = self._datetime(
                raw.get("expectedOriginalStartAt"), zone
            )
            expected_end = self._datetime(
                raw.get("expectedOriginalEndAt"), zone
            )
            if (
                block.start_at.replace(tzinfo=None)
                != expected_start.replace(tzinfo=None)
                or block.end_at.replace(tzinfo=None)
                != expected_end.replace(tzinfo=None)
            ):
                raise ValueError(
                    "Schedule displacement target changed after deterministic planning"
                )
            block_ref = EntityRef(
                kind="calendar_block",
                entity_id=f"calendar_block:{block_id}",
                revision=int(block.revision),
            )
            block_refs.append(block_ref)
            seen_block_ids.add(block_id)
            operations.append(
                CalendarMoveBlockOperation(
                    operation_id=self._stable_id(
                        "operation-calendar-displacement",
                        proposal_id,
                        index,
                        raw,
                    ),
                    block_ref=block_ref,
                    start_at=self._datetime(raw.get("startAt"), zone),
                    end_at=self._datetime(raw.get("endAt"), zone),
                    fulfills_clause_ids=[],
                    provenance=provenance,
                )
            )
        if not operations:
            return
        clause_id = self._stable_id(
            "clause-calendar-displacement",
            proposal_id,
            sorted(seen_block_ids),
        )
        operations = [
            operation.model_copy(update={"fulfills_clause_ids": [clause_id]})
            for operation in operations
        ]
        clauses.append(
            RequestedOperationClause(
                clause_id=clause_id,
                operation_kind="calendar.move_block",
                cardinality=(
                    "exactly_one" if len(operations) == 1 else "one_or_more"
                ),
                target_refs=block_refs,
                expected_operations=operations,
                provenance=provenance,
            )
        )

    @staticmethod
    def _stable_id(prefix: str, *values: object) -> str:
        payload = json.dumps(
            values,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return (
            f"{prefix}:"
            f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"
        )

    @staticmethod
    def _task_ref(value: Any) -> EntityRef:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Compatibility operation requires an exact task ID")
        return EntityRef(kind="task", entity_id=f"task:{value}")

    @classmethod
    def _task_fields(cls, value: Any) -> TaskFields:
        fields = value if isinstance(value, dict) else {}
        unsupported = {
            key
            for key in (
                "emoji",
                "image_attachments",
                "is_pinned",
            )
            if fields.get(key) not in (None, [], False, "")
        }
        node_type = str(fields.get("node_type") or "").strip().lower()
        if node_type and node_type != "task":
            unsupported.add("node_type")
        if unsupported:
            raise ValueError(
                "Native task metadata is missing compiler support for: "
                + ", ".join(sorted(unsupported))
            )
        status = fields.get("status")
        normalized_status = (
            cls._STATUS_MAP.get(str(status)) if status is not None else None
        )
        if status is not None and normalized_status is None:
            raise ValueError(f"Unsupported task status {status!r}")
        return TaskFields(
            notes=fields.get("notes"),
            priority=fields.get("priority"),
            status=normalized_status,
            estimate_minutes=fields.get(
                "time_estimate_minutes",
                fields.get("estimate_minutes"),
            ),
            planned_date=fields.get("planned_date"),
            deadline_at=fields.get("deadline_at"),
            is_backlog=fields.get("is_backlog"),
            tags=fields.get("tags"),
        )

    @classmethod
    def _task_update(cls, value: Any) -> TaskUpdatePatch:
        fields = value if isinstance(value, dict) else {}
        payload = cls._task_fields(fields).model_dump(
            mode="python",
            exclude_none=True,
        )
        if fields.get("title") is not None:
            payload["title"] = fields["title"]
        if not payload:
            raise ValueError("Compatibility metadata update has no supported fields")
        return TaskUpdatePatch(**payload)

    @staticmethod
    def _zone(timezone_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Compatibility proposal has an invalid timezone") from error

    @staticmethod
    def _datetime(value: Any, zone: ZoneInfo) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Compatibility schedule has no exact datetime")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Compatibility schedule has an invalid datetime") from error
        return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed

    @classmethod
    def _recurrence_rule(
        cls,
        value: dict[str, Any],
        *,
        fallback_timezone: str,
    ) -> RecurrenceRule:
        raw_days = value.get("days_of_week", value.get("weekdays", []))
        weekdays = (
            [
                cls._WEEKDAY_MAP.get(item, str(item).strip().lower())
                for item in raw_days
                if isinstance(item, (int, str)) and not isinstance(item, bool)
            ]
            if isinstance(raw_days, list)
            else []
        )
        frequency = str(value.get("frequency") or "").strip().lower()
        if frequency == "weekly" and not weekdays:
            raw_start_date = value.get("start_date")
            if isinstance(raw_start_date, datetime):
                anchor_date = raw_start_date.date()
            elif isinstance(raw_start_date, date):
                anchor_date = raw_start_date
            else:
                try:
                    anchor_date = date.fromisoformat(str(raw_start_date or ""))
                except ValueError:
                    anchor_date = None
            if anchor_date is not None:
                weekdays = [
                    (
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    )[anchor_date.weekday()]
                ]
        raw_month_days = value.get("month_days", [])
        return RecurrenceRule(
            frequency=frequency,
            interval=int(value.get("interval") or 1),
            weekdays=weekdays,
            month_days=(
                raw_month_days if isinstance(raw_month_days, list) else []
            ),
            start_date=value.get("start_date"),
            end_date=value.get("end_date"),
            occurrence_count=value.get("occurrence_count"),
            timezone=str(value.get("timezone") or fallback_timezone),
            preferred_start_time=value.get("preferred_start_time"),
            duration_minutes=value.get("duration_minutes"),
        )
