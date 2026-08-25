"""Server-owned dispatch for exact, typed ReviewDocument interactions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from sqlmodel import select

from app.models.agent_execution import AgentChangeSetExecutionRecord, AgentReviewActionRecord
from .change_set_store import NativeChangeSetStore
from .change_set_store import ChangeSetNotFound, ChangeSetSessionMismatch
from .contracts import (
    ApproveAndApplyChangeSetCommand,
    ApproveReviewAction,
    DecisionReviewAction,
    EditReviewAction,
    RejectReviewAction,
    RetryReviewAction,
    ReviewAction,
    ReviewActionError,
    ReviewActionOutcome,
    ReviewInteractionResult,
)
from .goal_state_coordinator import GoalStateCoordinator
from .goal_state_reducer import transition_goal
from .simulation_store import SimulationResultNotFound, SimulationStore
from .state_store import GoalStateNotFound, GoalStateSessionMismatch, GoalStateStore
from .transaction_runtime import AgentChangeSetTransactionRuntime


class ReviewActionCoordinator:
    """Validate presentation identity, then call an existing semantic/runtime boundary."""

    def __init__(
        self,
        session: Session,
        *,
        goal_store: GoalStateStore | None = None,
        change_set_store: NativeChangeSetStore | None = None,
        simulation_store: SimulationStore | None = None,
        goal_coordinator: GoalStateCoordinator | None = None,
        transaction_runtime: AgentChangeSetTransactionRuntime | None = None,
        execution_loader: Callable[[str], Any | None] | None = None,
    ) -> None:
        self.session = session
        self.goal_store = goal_store or GoalStateStore(session)
        self.change_set_store = change_set_store or NativeChangeSetStore(session)
        self.simulation_store = simulation_store or SimulationStore(session)
        self.goal_coordinator = goal_coordinator or GoalStateCoordinator(session)
        self.transaction_runtime = transaction_runtime or AgentChangeSetTransactionRuntime(session)
        self.execution_loader = execution_loader or (
            lambda execution_id: session.get(AgentChangeSetExecutionRecord, execution_id)
        )

    def execute(
        self,
        *,
        authenticated_session_id: str,
        action: ReviewAction,
    ) -> ReviewActionOutcome:
        if action.binding.session_id != authenticated_session_id:
            return self._error(
                "authorization_denied",
                "This review does not belong to the current session.",
                "none",
            )
        try:
            replay = self._load_action_receipt(authenticated_session_id, action)
            if replay is not None:
                return replay
            if isinstance(action, ApproveReviewAction):
                if not self._valid_static_identity(action):
                    return self._error("invalid_action", "This review control is not current.", "refresh")
                if not self._approval_execution_exists(authenticated_session_id, action):
                    current = self._load_current(authenticated_session_id, action)
                    if current is None:
                        return self._error(
                            "stale_review",
                            "This review is out of date. Refresh it before continuing.",
                            "refresh",
                        )
                    _goal, change_set, _simulation = current
                return self._record_outcome(
                    authenticated_session_id,
                    action,
                    self._execute_approval(action, replay=False),
                )
            if isinstance(action, RetryReviewAction):
                if not self._valid_static_identity(action):
                    return self._error("invalid_action", "This retry control is not current.", "refresh")
                replay_identity = self._load_retry_identity(authenticated_session_id, action)
                if replay_identity is None:
                    return self._error(
                        "invalid_action",
                        "The original execution could not be matched safely.",
                        "refresh",
                    )
                return self._record_outcome(
                    authenticated_session_id,
                    action,
                    self._execute_approval(
                        action,
                        replay=True,
                        replay_identity=replay_identity,
                    ),
                )
            current = self._load_current(authenticated_session_id, action)
            if current is None:
                return self._error(
                    "stale_review",
                    "This review is out of date. Refresh it before continuing.",
                    "refresh",
                )
            goal, change_set, _simulation = current
            if not self._valid_current_identity(action, goal, change_set):
                return self._error("invalid_action", "This review control is not current.", "refresh")
            if isinstance(action, DecisionReviewAction):
                updated = self.goal_coordinator.apply_ui_decision(
                    session_id=authenticated_session_id,
                    goal_id=action.binding.goal_id,
                    goal_revision=action.binding.goal_revision,
                    decision_id=action.decision_id,
                    option_id=action.option_id,
                    source_event_id=action.event_id,
                    commit=False,
                )
                outcome = self._ok(
                    action,
                    "decision_recorded",
                    "Your choice was saved. The plan will be checked again before approval.",
                    goal_revision=updated.revision,
                    change_set_revision=(
                        updated.current_change_set_revision or change_set.revision
                    ),
                )
                return self._record_outcome(authenticated_session_id, action, outcome)
            if isinstance(action, EditReviewAction):
                outcome = self._request_edit(authenticated_session_id, action, goal, change_set)
                return self._record_outcome(authenticated_session_id, action, outcome)
            if isinstance(action, RejectReviewAction):
                outcome = self._reject(authenticated_session_id, action, goal, change_set)
                return self._record_outcome(authenticated_session_id, action, outcome)
            return self._error(
                "invalid_action",
                "This review action is not supported.",
                "none",
            )
        except Exception:
            self.session.rollback()
            return self._error(
                "system_failure",
                "Drasil could not complete this review action safely.",
                "retry",
            )

    @staticmethod
    def _stable_id(prefix: str, *parts: object) -> str:
        digest = hashlib.sha256(
            "|".join(str(part) for part in parts).encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}:{digest}"

    @classmethod
    def _review_id_from_binding(cls, action: ReviewAction) -> str:
        binding = action.binding
        return cls._stable_id(
            "review",
            binding.goal_id,
            binding.goal_revision,
            binding.change_set_id,
            binding.change_set_revision,
            binding.simulation_id,
        )

    @classmethod
    def _valid_static_identity(cls, action: ReviewAction) -> bool:
        review_id = cls._review_id_from_binding(action)
        if action.review_id != review_id or action.review_revision != 1:
            return False
        if isinstance(action, DecisionReviewAction):
            parts = (action.decision_id, action.option_id)
        elif isinstance(action, RetryReviewAction):
            parts = ("retry", action.execution_id)
        else:
            parts = (action.action,)
        if action.event_id != cls._stable_id("event", review_id, *parts):
            return False
        if action.idempotency_key != cls._stable_id("idem", review_id, *parts):
            return False
        if isinstance(action, ApproveReviewAction):
            return action.command_id == cls._stable_id("command", review_id, "approve")
        return True

    @classmethod
    def _valid_current_identity(cls, action: ReviewAction, goal: Any, change_set: Any) -> bool:
        if not cls._valid_static_identity(action):
            return False
        if isinstance(action, DecisionReviewAction):
            return any(
                decision.status == "open"
                and decision.decision_id == action.decision_id
                and any(option.option_id == action.option_id for option in decision.options)
                for decision in goal.unresolved_decisions
            )
        if isinstance(action, (EditReviewAction, RejectReviewAction)):
            return change_set.status in {"ready_for_review", "needs_user_decision"}
        return True

    @staticmethod
    def _action_fingerprint(action: ReviewAction) -> str:
        payload = json.dumps(
            action.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_action_receipt(
        self,
        session_id: str,
        action: ReviewAction,
    ) -> ReviewActionOutcome | None:
        record = self.session.exec(
            select(AgentReviewActionRecord).where(
                AgentReviewActionRecord.session_id == session_id,
                AgentReviewActionRecord.idempotency_key == action.idempotency_key,
            )
        ).first()
        if record is None:
            return None
        if record.request_fingerprint != self._action_fingerprint(action):
            return self._error(
                "invalid_action",
                "This action identity was already used for another request.",
                "none",
            )
        return ReviewActionOutcome.model_validate_json(record.outcome_json)

    def _approval_execution_exists(
        self,
        session_id: str,
        action: ApproveReviewAction,
    ) -> bool:
        return self.session.exec(
            select(AgentChangeSetExecutionRecord.execution_id).where(
                AgentChangeSetExecutionRecord.session_id == session_id,
                (
                    (AgentChangeSetExecutionRecord.command_id == action.command_id)
                    | (AgentChangeSetExecutionRecord.idempotency_key == action.idempotency_key)
                ),
            )
        ).first() is not None

    def _record_outcome(
        self,
        session_id: str,
        action: ReviewAction,
        outcome: ReviewActionOutcome,
    ) -> ReviewActionOutcome:
        fingerprint = self._action_fingerprint(action)
        record_id = self._stable_id("review-action", session_id, action.idempotency_key)
        self.session.add(AgentReviewActionRecord(
            action_record_id=record_id,
            session_id=session_id,
            review_id=action.review_id,
            event_id=action.event_id,
            idempotency_key=action.idempotency_key,
            action_kind=action.action,
            request_fingerprint=fingerprint,
            outcome_json=outcome.model_dump_json(),
        ))
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            replay = self._load_action_receipt(session_id, action)
            if replay is not None:
                return replay
            raise
        return outcome

    def _load_current(self, session_id: str, action: ReviewAction) -> tuple[Any, Any, Any] | None:
        binding = action.binding
        try:
            goal = self.goal_store.load(session_id, binding.goal_id)
            change_set = self.change_set_store.load(session_id, binding.change_set_id)
            simulation = self.simulation_store.load(session_id, binding.simulation_id)
        except (GoalStateNotFound, ChangeSetNotFound, SimulationResultNotFound):
            return None
        except (GoalStateSessionMismatch, ChangeSetSessionMismatch):
            return None
        current = (
            goal.revision == binding.goal_revision
            and goal.current_change_set_id == binding.change_set_id
            and goal.current_change_set_revision == binding.change_set_revision
            and change_set.goal_id == binding.goal_id
            and change_set.revision == binding.change_set_revision
            and change_set.simulation_result_ref == binding.simulation_id
            and simulation.resulting_goal_revision == binding.goal_revision
            and simulation.change_set_id == binding.change_set_id
            and simulation.change_set_revision == binding.change_set_revision
            and simulation.input_snapshot.fingerprint == binding.simulation_input_fingerprint
            and simulation.output_fingerprint == binding.simulation_output_fingerprint
            and simulation.correlation_id == binding.correlation_id
            and simulation.causation_id == binding.causation_id
            and goal.correlation_id == binding.correlation_id
            and change_set.correlation_id == binding.correlation_id
        )
        return (goal, change_set, simulation) if current else None

    def _load_retry_identity(
        self,
        session_id: str,
        action: RetryReviewAction,
    ) -> tuple[str, str] | None:
        record = self.execution_loader(action.execution_id)
        binding = action.binding
        if record is None or (
            record.session_id != session_id
            or record.command_id != action.original_command_id
            or record.idempotency_key != action.original_idempotency_key
            or record.goal_id != binding.goal_id
            or record.change_set_id != binding.change_set_id
            or record.change_set_revision != binding.change_set_revision
            or record.simulation_id != binding.simulation_id
        ):
            return None
        return record.command_id, record.idempotency_key

    def _execute_approval(
        self,
        action: ApproveReviewAction | RetryReviewAction,
        *,
        replay: bool,
        replay_identity: tuple[str, str] | None = None,
    ) -> ReviewActionOutcome:
        binding = action.binding
        replay_command_id, replay_idempotency_key = replay_identity or ("", "")
        command = ApproveAndApplyChangeSetCommand(
            command_id=(replay_command_id if replay else action.command_id),
            idempotency_key=(
                replay_idempotency_key if replay else action.idempotency_key
            ),
            session_id=binding.session_id,
            goal_id=binding.goal_id,
            expected_goal_revision=binding.goal_revision,
            change_set_id=binding.change_set_id,
            expected_change_set_revision=binding.change_set_revision,
            simulation_id=binding.simulation_id,
            simulation_input_fingerprint=binding.simulation_input_fingerprint,
            simulation_output_fingerprint=binding.simulation_output_fingerprint,
            correlation_id=binding.correlation_id,
            causation_id=binding.causation_id,
        )
        execution = self.transaction_runtime.execute(command)
        if execution.status == "error":
            error = execution.error
            if error is None:
                return self._error(
                    "system_failure",
                    "Drasil could not complete this review action safely.",
                    "retry",
                )
            code = str(error.code)
            if code in {
                "STALE_GOAL", "STALE_CHANGE_SET", "STALE_SIMULATION", "STALE_WORLD",
                "STALE_ENTITY", "PRECONDITION_FAILED", "INVALID_LIFECYCLE",
                "INVALID_APPROVAL_IDENTITY",
            }:
                return self._error("stale_review", error.user_summary, "refresh")
            if code == "AUTHORIZATION_DENIED":
                return self._error("authorization_denied", error.user_summary, "none")
            if error.retryable:
                return self._error("system_failure", error.user_summary, "retry")
            return self._error("invalid_action", error.user_summary, "none")
        result = execution.result
        if result is None:
            return self._error(
                "system_failure",
                "Drasil could not complete this review action safely.",
                "retry",
            )
        return self._ok(
            action,
            "retry_replayed" if replay else "execution_complete",
            (
                "The original result was replayed without applying the changes again."
                if replay
                else "The reviewed changes reached the verified execution boundary."
            ),
            goal_revision=result.goal_revision,
            change_set_revision=result.change_set_revision,
            execution=execution,
        )

    def _reject(
        self,
        session_id: str,
        action: RejectReviewAction,
        goal: Any,
        change_set: Any,
    ) -> ReviewActionOutcome:
        with self.session.begin_nested():
            rejected_change_set = self.change_set_store.transition_status(
                session_id,
                change_set.change_set_id,
                expected_revision=change_set.revision,
                expected_status=change_set.status,
                target_status="rejected",
            )
            rejected_goal = transition_goal(goal, "rejected", command_id=action.event_id)
            saved_goal = self.goal_store.save(
                session_id,
                rejected_goal,
                expected_revision=goal.revision,
                command_id=action.event_id,
                transition_kind="review.rejected",
                payload={
                    "reviewId": action.review_id,
                    "reviewRevision": action.review_revision,
                    "changeSetId": change_set.change_set_id,
                    "changeSetRevision": change_set.revision,
                },
            )
        return self._ok(
            action,
            "rejected",
            "This plan was rejected and no reviewed changes were applied.",
            goal_revision=saved_goal.revision,
            change_set_revision=rejected_change_set.revision,
        )

    def _request_edit(
        self,
        session_id: str,
        action: EditReviewAction,
        goal: Any,
        change_set: Any,
    ) -> ReviewActionOutcome:
        """Retire readiness so the exact reviewed revision cannot still be approved."""
        with self.session.begin_nested():
            invalidated_change_set = self.change_set_store.invalidate_review(
                session_id,
                change_set.change_set_id,
                expected_revision=change_set.revision,
                expected_status=change_set.status,
            )
            planning_goal = transition_goal(goal, "planning", command_id=action.event_id)
            saved_goal = self.goal_store.save(
                session_id,
                planning_goal,
                expected_revision=goal.revision,
                command_id=action.event_id,
                transition_kind="review.edit_requested",
                payload={
                    "reviewId": action.review_id,
                    "reviewRevision": action.review_revision,
                    "changeSetId": change_set.change_set_id,
                    "changeSetRevision": change_set.revision,
                    "hasCorrection": action.correction is not None,
                    "correction": action.correction,
                },
            )
        return self._ok(
            action,
            "edit_requested",
            "This review was retired so the plan can be revised and checked again.",
            goal_revision=saved_goal.revision,
            change_set_revision=invalidated_change_set.revision,
            correction=action.correction,
        )

    @staticmethod
    def _ok(
        action: ReviewAction,
        status: str,
        summary: str,
        *,
        goal_revision: int,
        change_set_revision: int,
        execution: Any | None = None,
        correction: str | None = None,
    ) -> ReviewActionOutcome:
        return ReviewActionOutcome(
            status="ok",
            result=ReviewInteractionResult(
                status=status,
                review_id=action.review_id,
                review_revision=action.review_revision,
                session_id=action.binding.session_id,
                goal_id=action.binding.goal_id,
                change_set_id=action.binding.change_set_id,
                summary=summary,
                goal_revision=goal_revision,
                change_set_revision=change_set_revision,
                correction=correction,
                execution=execution,
            ),
        )

    @staticmethod
    def _error(code: str, summary: str, recovery: str) -> ReviewActionOutcome:
        return ReviewActionOutcome(
            status="error",
            error=ReviewActionError(code=code, summary=summary, recovery=recovery),
        )
