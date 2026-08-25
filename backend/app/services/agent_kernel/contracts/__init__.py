"""Canonical Agent Kernel v2 contracts."""

from .change_sets import (
    ChangeSet,
    DraftChangeSetCommand,
    DraftChangeSetResult,
    EntityAbsentAssertion,
    EntityExistsAssertion,
    EntityFieldEqualsAssertion,
    Preconditions,
    PreconditionAssertion,
)
from .conservation import OperationConservationReport, OperationFingerprint
from .common import CONTRACT_VERSION, ContractModel, strict_provider_schema
from .change_set_deltas import (
    ChangeSetConstraintAdd,
    ChangeSetConstraintRemove,
    ChangeSetConstraintReplace,
    ChangeSetEdit,
    ChangeSetOperationAdd,
    ChangeSetOperationRemove,
    ChangeSetOperationReplace,
    ChangeSetRevisionDelta,
)
from .conflicts import (
    AlternativeFeasibilityProof,
    BlockerEvidence,
    Conflict,
    ConflictAlternative,
    ConflictGraph,
    OverlapInterval,
)
from .constraints import (
    CardinalityConstraintValue,
    Constraint,
    ConstraintScope,
    ExactTimeConstraintValue,
    MinimumGapConstraintValue,
    OrderingConstraintValue,
    PreserveConstraintValue,
    SameWorkspaceConstraintValue,
    TemporalWindowConstraintValue,
)
from .decisions import (
    ChangeSetDecisionDelta,
    DecisionConflictDetail,
    DecisionDelta,
    DecisionOption,
    DecisionRequest,
    ResolvedDecision,
)
from .goal_patches import GoalPatchOperation, GoalSemanticPath, GoalStatePatch
from .goal_transitions import GoalDeltaPatch, GoalTurnDecision
from .intent import OperationKind, RequestedOperationClause
from .goal_state import (
    Assumption,
    CorrectionDelta,
    DesiredOutcome,
    GoalState,
    GoalStateUpdateCommand,
    SemanticSlot,
    StoredContractMigrationIssue,
)
from .operations import (
    OPERATION_ADAPTER,
    CalendarCreateBlockOperation,
    CalendarDeleteBlockOperation,
    CalendarMoveBlockOperation,
    CalendarResizeBlockOperation,
    CalendarSegment,
    CalendarSplitBlockOperation,
    Operation,
    PreferenceRetireOperation,
    PreferenceRestoreOperation,
    PreferenceDeleteOperation,
    PreferenceSemantics,
    PreferenceScalarValue,
    PreferenceStringSetValue,
    PreferenceSupersedeOperation,
    PreferenceTimeWindowValue,
    PreferenceUpsertOperation,
    RecurrenceCancelOccurrenceOperation,
    RecurrenceCreateSeriesOperation,
    RecurrenceOccurrencePatch,
    RecurrenceRule,
    RecurrenceRulePatch,
    RecurrenceUpdateOccurrenceOperation,
    RecurrenceUpdateSeriesOperation,
    TaskArchiveOperation,
    TaskCreateOperation,
    TaskDeleteOperation,
    TaskFields,
    TaskMoveOperation,
    TaskUpdatePatch,
    TaskUpdateOperation,
    parse_operation,
)
from .simulation import (
    ChangeSetSimulationResult,
    ProjectedEffect,
    ProjectedFinalFact,
    SimulateChangeSetCommand,
    SimulationIssue,
    SimulationSnapshot,
    SnapshotFact,
)
from .references import EntityRef, EntitySelector, Provenance, SemanticValue
from .results import ErrorResolution, ToolError, ToolErrorCategory, ToolErrorCode, ToolResult
from .execution import (
    ApproveAndApplyChangeSetCommand,
    EntityBinding,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionOutcome,
    ExecutionFact,
    ExpectedField,
    ExternalSyncErrorCode,
    ExternalSyncItemStatus,
    LocalExecutionResult,
    MAX_LOCAL_EXECUTION_RESULT_BYTES,
    VerificationMismatch,
    VerificationResult,
)
from .review import (
    ApproveReviewAction,
    AssumptionBlock,
    ConflictResolutionBlock,
    ConflictReviewItem,
    DecisionReviewAction,
    EditReviewAction,
    EntityReferencesBlock,
    ExecutionStatusBlock,
    ExternalSyncReviewStatus,
    GroupedChangesBlock,
    RecurrenceSummaryBlock,
    RejectReviewAction,
    RetryReviewAction,
    ReviewAction,
    ReviewActionError,
    ReviewActionBinding,
    ReviewActionOutcome,
    ReviewChange,
    ReviewChangeGroup,
    ReviewDocument,
    ReviewEntity,
    ReviewInteractionResult,
    ScheduleSummaryBlock,
    SemanticDiffBlock,
    SemanticFallbackBlock,
    TaskStructureBlock,
    TaskStructureItem,
    TaskStructureSchedule,
    TaskStructureWorkspace,
    WarningBlock,
)

__all__ = [name for name in globals() if not name.startswith("_")]
