"""Exact-revision, non-mutating ChangeSet simulation orchestration."""

from __future__ import annotations

import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.agent_change_set import AgentChangeSetRecord
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries
from app.services.agent_kernel.change_set_authoring import (
    ChangeSetAuthorizationError,
    EntityReferenceAuthorizer,
    trusted_schedule_evidence_from_goal,
)
from app.services.agent_kernel.change_set_store import NativeChangeSetStore
from app.services.agent_kernel.conflict_policy import TypedConflictPolicy
from app.services.agent_kernel.contracts import (
    BlockerEvidence,
    ChangeSet,
    ChangeSetSimulationResult,
    Conflict,
    ConflictGraph,
    GoalState,
    OperationConservationReport,
    SimulateChangeSetCommand,
    SimulationIssue,
)
from app.services.agent_kernel.goal_state_reducer import ALLOWED_GOAL_TRANSITIONS
from app.services.agent_kernel.operation_conservation import (
    evaluate_operation_conservation,
    operation_fingerprint,
)
from app.services.agent_kernel.change_set_deltas import apply_change_set_delta
from app.services.agent_kernel.simulation import ChangeSetProjector, SimulationSnapshotLoader
from app.services.agent_kernel.simulation import SimulationProjectionError, _fingerprint
from app.services.agent_kernel.simulation_store import (
    SimulationAttemptIncomplete,
    SimulationCommandConflict,
    SimulationStore,
)
from app.services.agent_kernel.state_store import GoalStateStore


class ChangeSetSimulationError(RuntimeError):
    pass


class SimulationRevisionConflict(ChangeSetSimulationError):
    pass


class SimulationInvariantError(ChangeSetSimulationError):
    pass


class SimulationCommandInProgress(ChangeSetSimulationError):
    pass


class ChangeSetSimulationCoordinator:
    def __init__(self, session: Session):
        self.session = session
        self.goal_store = GoalStateStore(session)
        self.change_set_store = NativeChangeSetStore(session)
        self.simulation_store = SimulationStore(session)

    @staticmethod
    def _payload_fingerprint(command: SimulateChangeSetCommand) -> str:
        payload = json.dumps(
            command.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _simulation_id(session_id: str, command_id: str) -> str:
        return f"simulation:{uuid5(NAMESPACE_URL, f'drasil:{session_id}:{command_id}')}"

    def _record_preflight_stale(
        self,
        session_id: str,
        goal: GoalState,
        change_set: ChangeSet,
        *,
        command_id: str,
        correlation_id: str,
        causation_id: str,
        payload_fingerprint: str,
        summary: str,
    ) -> ChangeSetSimulationResult:
        loaded = SimulationSnapshotLoader(self.session).load(change_set)
        conservation = evaluate_operation_conservation(goal, change_set)
        blocker = BlockerEvidence(
            blocker_id=f"failure:stale:{change_set.change_set_id}",
            source="change_set_constraint",
            fingerprint=_fingerprint({"summary": summary}),
            observed_at=loaded.snapshot.captured_at,
            freshness_boundary=loaded.snapshot.freshness_boundary,
            anchoring="unknown", movability="unknown", protection="unknown",
        )
        graph = ConflictGraph(
            graph_id=f"graph:{self._simulation_id(session_id, command_id).split(':', 1)[1]}",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            change_set_operation_ids=[item.operation_id for item in change_set.operations],
            snapshot_fingerprint=loaded.snapshot.fingerprint,
            status="invalid",
            conflicts=[Conflict(
                conflict_id=f"conflict:{self._simulation_id(session_id, command_id).split(':', 1)[1]}",
                kind="projection_invariant",
                incoming_operation_ids=[item.operation_id for item in change_set.operations],
                blockers=[blocker],
                policy_recommendation="revise_plan",
                resolution_disposition="unresolvable",
                confidence=1.0,
            )],
        )
        result = ChangeSetSimulationResult(
            simulation_id=self._simulation_id(session_id, command_id),
            command_id=command_id,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            resulting_goal_revision=goal.revision,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            input_snapshot=loaded.snapshot,
            output_fingerprint=loaded.world.fingerprint,
            status="stale",
            conflict_graph=graph,
            conservation=conservation,
            issues=[SimulationIssue(
                code="STALE_CHANGE_SET",
                category="retryable_system",
                summary=summary,
            )],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        try:
            with self.session.begin_nested():
                recorded = self.simulation_store.add(
                    session_id,
                    result,
                    payload_fingerprint=payload_fingerprint,
                    change_set=change_set,
                )
        except SimulationAttemptIncomplete as error:
            raise SimulationCommandInProgress(
                "an identical simulation command is already in progress; retry shortly"
            ) from error
        if recorded != result:
            return recorded
        return result

    def simulate(
        self,
        session_id: str,
        command: SimulateChangeSetCommand,
        *,
        command_id: str,
        correlation_id: str,
        causation_id: str,
    ) -> ChangeSetSimulationResult:
        domain_types = (Category, StrategyNode, CalendarBlock, RecurrenceSeries, UserPreference, SchedulingConstraint)
        pending_domain = [
            item for item in [*self.session.new, *self.session.dirty, *self.session.deleted]
            if isinstance(item, domain_types)
        ]
        if pending_domain:
            raise SimulationInvariantError(
                "simulation requires a clean domain session; pending domain mutations were not flushed"
            )
        fingerprint = self._payload_fingerprint(command)
        interrupted_attempt: SimulationAttemptIncomplete | None = None
        try:
            with self.session.no_autoflush:
                replay = self.simulation_store.replay(
                    session_id, command_id, payload_fingerprint=fingerprint
                )
        except SimulationAttemptIncomplete as error:
            interrupted_attempt = error
            replay = None
        except SimulationCommandConflict as error:
            raise SimulationRevisionConflict(str(error)) from error
        if replay is not None:
            return replay

        goal = self.goal_store.load_active(session_id)
        if goal is None:
            raise SimulationRevisionConflict("session has no active GoalState")
        change_set = self.change_set_store.load_current(session_id, goal.goal_id)
        if change_set is None:
            raise SimulationRevisionConflict("goal has no active native ChangeSet")
        if interrupted_attempt is not None:
            record = interrupted_attempt.record
            if not interrupted_attempt.recoverable:
                raise SimulationCommandInProgress(
                    "an identical simulation command is already in progress; retry shortly"
                )
            if (
                record.goal_id != goal.goal_id
                or record.goal_revision != goal.revision
                or goal.revision != command.expected_goal_revision + 1
                or goal.status != "simulating"
                or record.change_set_id != change_set.change_set_id
                or record.change_set_revision != change_set.revision
                or change_set.revision != command.expected_change_set_revision
                or change_set.status != "simulating"
            ):
                raise SimulationInvariantError(
                    "interrupted simulation state no longer matches its durable command"
                )
            stored_change_set = self.simulation_store.load_change_set(
                session_id, record.simulation_id
            )
            if stored_change_set != change_set:
                raise SimulationInvariantError(
                    "interrupted simulation ChangeSet snapshot is inconsistent"
                )
        else:
            if goal.revision != command.expected_goal_revision:
                return self._record_preflight_stale(
                    session_id, goal, change_set,
                    command_id=command_id, correlation_id=correlation_id, causation_id=causation_id,
                    payload_fingerprint=fingerprint,
                    summary=f"stale goal revision: expected {command.expected_goal_revision}, current {goal.revision}",
                )
            if (
                change_set.revision != command.expected_change_set_revision
                or goal.current_change_set_revision != change_set.revision
                or goal.current_change_set_id != change_set.change_set_id
            ):
                return self._record_preflight_stale(
                    session_id, goal, change_set,
                    command_id=command_id, correlation_id=correlation_id, causation_id=causation_id,
                    payload_fingerprint=fingerprint,
                    summary="stale or inconsistent ChangeSet revision",
                )
            simulating_change_set = ChangeSet.model_validate({
                **change_set.model_dump(mode="python"),
                "status": "simulating",
                "simulation_result_ref": None,
            })
            simulating_goal = GoalState.model_validate({
                **goal.model_dump(mode="python"),
                "revision": goal.revision + 1,
                "status": "simulating",
                "unresolved_decisions": [],
                "approved_change_set_revision": None,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
            })
            with self.session.begin_nested():
                started = self.session.exec(
                    update(AgentChangeSetRecord)
                    .where(
                        AgentChangeSetRecord.change_set_id == change_set.change_set_id,
                        AgentChangeSetRecord.session_id == session_id,
                        AgentChangeSetRecord.revision == change_set.revision,
                        AgentChangeSetRecord.status == change_set.status,
                    )
                    .values(
                        status="simulating",
                        snapshot_json=simulating_change_set.model_dump_json(),
                    )
                )
                if started.rowcount != 1:  # type: ignore[attr-defined]
                    try:
                        winner = self.simulation_store.replay(
                            session_id, command_id, payload_fingerprint=fingerprint
                        )
                    except SimulationAttemptIncomplete as error:
                        raise SimulationCommandInProgress(
                            "an identical simulation command is already in progress; retry shortly"
                        ) from error
                    if winner is not None:
                        return winner
                    raise SimulationRevisionConflict("concurrent ChangeSet simulation start conflict")
                self.goal_store.save(
                    session_id,
                    simulating_goal,
                    expected_revision=goal.revision,
                    command_id=f"goal-simulating:{command_id}",
                    transition_kind="goal.simulating",
                    payload={
                        "changeSetId": change_set.change_set_id,
                        "changeSetRevision": change_set.revision,
                    },
                )
                started_replay = self.simulation_store.start(
                    session_id,
                    simulation_id=self._simulation_id(session_id, command_id),
                    command_id=command_id,
                    payload_fingerprint=fingerprint,
                    goal_id=simulating_goal.goal_id,
                    goal_revision=simulating_goal.revision,
                    change_set=simulating_change_set,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                )
                if started_replay is not None:
                    return started_replay
            # The start marker and explicit simulating lifecycle are a deliberate
            # durable checkpoint. No product-domain rows are pending here.
            self.session.commit()
            goal = simulating_goal
            change_set = simulating_change_set

        loaded = SimulationSnapshotLoader(self.session).load(change_set)
        conservation = evaluate_operation_conservation(goal, change_set)
        projection = None
        failure_issue = None
        try:
            EntityReferenceAuthorizer(self.session).authorize(change_set)
            if not conservation.complete:
                if not self._authorized_resolution_lineage(session_id, goal, change_set):
                    raise SimulationInvariantError(
                        "ChangeSet no longer conserves the active goal"
                    )
                conservation = OperationConservationReport(
                    complete=True,
                    covered_clause_ids=conservation.covered_clause_ids,
                    unresolved_clause_ids=[],
                    unsupported_clause_ids=[],
                    failed_clause_ids=[],
                    extra_operation_ids=[],
                    operation_fingerprints=[
                        operation_fingerprint(operation)
                        for operation in change_set.operations
                    ],
                )
            projection = ChangeSetProjector().project(change_set, loaded.world)
            graph = TypedConflictPolicy().evaluate(
                change_set,
                projection.world,
                loaded.snapshot,
                goal_revision=goal.revision,
                goal_constraints=goal.constraints,
                anchored_operation_ids={
                    item.operation_id for item in trusted_schedule_evidence_from_goal(goal)
                },
                base_world=loaded.world,
            )
            refreshed = SimulationSnapshotLoader(self.session).load(change_set)
            if refreshed.snapshot.fingerprint != loaded.snapshot.fingerprint:
                raise SimulationRevisionConflict(
                    "authoritative world changed during simulation; retry against a fresh snapshot"
                )
            result_status = {
                "clear": "ready_for_review",
                "resolved": "repairing",
                "needs_user_decision": "needs_user_decision",
                "invalid": "failed",
            }[graph.status]
        except (ChangeSetAuthorizationError, SimulationProjectionError, SimulationRevisionConflict, SimulationInvariantError, ValueError) as error:
            if isinstance(error, ChangeSetAuthorizationError):
                issue_code, category, result_status = "AUTHORIZATION_DENIED", "authorization", "failed"
            elif isinstance(error, SimulationRevisionConflict):
                issue_code, category, result_status = "STALE_WORLD", "retryable_system", "stale"
            elif isinstance(error, SimulationProjectionError):
                issue_code = "RECURRENCE_BOUND_EXCEEDED" if error.code == "RECURRENCE_BOUND_EXCEEDED" else "PROJECTION_FAILED"
                category, result_status = "model_repairable", "failed"
            elif isinstance(error, SimulationInvariantError) and not conservation.complete:
                issue_code, category, result_status = "CONSERVATION_FAILED", "model_repairable", "failed"
            else:
                issue_code, category, result_status = "INVARIANT_FAILED", "fatal_invariant", "failed"
            failure_issue = SimulationIssue(
                code=issue_code,
                category=category,
                affected_operation_ids=([error.operation_id] if isinstance(error, SimulationProjectionError) and error.operation_id else []),
                summary=str(error)[:1000],
            )
            blocker = BlockerEvidence(
                blocker_id=f"failure:{issue_code.lower()}",
                source="change_set_constraint",
                fingerprint=_fingerprint({"code": issue_code, "summary": str(error)}),
                observed_at=loaded.snapshot.captured_at,
                freshness_boundary=loaded.snapshot.freshness_boundary,
                anchoring="unknown",
                movability="unknown",
                protection="unknown",
            )
            graph = ConflictGraph(
                graph_id=f"graph:{self._simulation_id(session_id, command_id).split(':', 1)[1]}",
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                change_set_id=change_set.change_set_id,
                change_set_revision=change_set.revision,
                change_set_operation_ids=[item.operation_id for item in change_set.operations],
                snapshot_fingerprint=loaded.snapshot.fingerprint,
                status="invalid",
                conflicts=[Conflict(
                    conflict_id=f"conflict:{self._simulation_id(session_id, command_id).split(':', 1)[1]}",
                    kind="projection_invariant",
                    incoming_operation_ids=[item.operation_id for item in change_set.operations],
                    blockers=[blocker],
                    policy_recommendation="revise_plan" if category == "model_repairable" else "reject",
                    resolution_disposition="unresolvable",
                    confidence=1.0,
                )],
            )
        result = ChangeSetSimulationResult(
            simulation_id=self._simulation_id(session_id, command_id),
            command_id=command_id,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            resulting_goal_revision=goal.revision + 1,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            input_snapshot=loaded.snapshot,
            output_fingerprint=projection.output_fingerprint if projection is not None else loaded.world.fingerprint,
            status=result_status,
            conflict_graph=graph,
            projected_effects=projection.effects if projection is not None else [],
            projected_final_facts=projection.final_facts if projection is not None else [],
            conservation=conservation,
            issues=(
                [
                    SimulationIssue(
                        code="INVARIANT_FAILED",
                        category="fatal_invariant",
                        affected_operation_ids=[
                            operation_id
                            for conflict in graph.conflicts
                            for operation_id in conflict.incoming_operation_ids
                        ],
                        summary="No bounded executable conflict resolution could be produced.",
                    )
                ]
                if graph.status == "invalid" and failure_issue is None
                else []
            ) + ([failure_issue] if failure_issue is not None else []),
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        change_set_status = {
            "ready_for_review": "ready_for_review",
            "repairing": "invalid",
            "needs_user_decision": "needs_user_decision",
            "failed": "invalid",
            "stale": "invalid",
        }[result.status]
        persisted_change_set = ChangeSet.model_validate(
            {
                **change_set.model_dump(mode="python"),
                "status": change_set_status,
                "simulation_result_ref": result.simulation_id,
            }
        )
        goal_status = {
            "ready_for_review": "ready_for_review",
            "repairing": "repairing",
            "needs_user_decision": "needs_user_decision",
            "failed": "system_failed",
            "stale": "repairing",
        }[result.status]
        if result.status == "failed" and failure_issue is not None and failure_issue.category == "model_repairable":
            goal_status = "repairing"
        if goal_status not in ALLOWED_GOAL_TRANSITIONS.get(goal.status, set()):
            raise SimulationInvariantError(
                f"invalid goal simulation transition {goal.status} -> {goal_status}"
            )
        persisted_goal = GoalState.model_validate(
            {
                **goal.model_dump(mode="python"),
                "revision": goal.revision + 1,
                "status": goal_status,
                "unresolved_decisions": (
                    [graph.decision_request] if graph.decision_request is not None else []
                ),
                "approved_change_set_revision": None,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
            }
        )
        try:
            with self.session.begin_nested():
                updated = self.session.exec(
                    update(AgentChangeSetRecord)
                    .where(
                        AgentChangeSetRecord.change_set_id == change_set.change_set_id,
                        AgentChangeSetRecord.session_id == session_id,
                        AgentChangeSetRecord.revision == change_set.revision,
                    )
                    .values(
                        status=persisted_change_set.status,
                        snapshot_json=persisted_change_set.model_dump_json(),
                    )
                )
                if updated.rowcount != 1:  # type: ignore[attr-defined]
                    raise SimulationRevisionConflict("concurrent ChangeSet simulation conflict")
                self.goal_store.save(
                    session_id,
                    persisted_goal,
                    expected_revision=goal.revision,
                    command_id=f"goal-simulation:{command_id}",
                    transition_kind=f"goal.{goal_status}",
                    payload={
                        "simulationId": result.simulation_id,
                        "changeSetId": change_set.change_set_id,
                        "changeSetRevision": change_set.revision,
                    },
                )
                finalized = self.simulation_store.finalize(
                    session_id,
                    result,
                    payload_fingerprint=fingerprint,
                    change_set=change_set,
                )
                if finalized != result:
                    return finalized
        except IntegrityError as error:
            self.session.expire_all()
            replay = self.simulation_store.replay(
                session_id, command_id, payload_fingerprint=fingerprint
            )
            if replay is not None:
                return replay
            raise SimulationRevisionConflict("concurrent simulation command conflict") from error
        return result

    def _authorized_resolution_lineage(
        self, session_id: str, goal: GoalState, change_set: ChangeSet
    ) -> bool:
        if (
            change_set.source_simulation_id is None
            or change_set.resolution_alternative_id is None
        ):
            return False
        try:
            source = self.simulation_store.load(
                session_id, change_set.source_simulation_id
            )
            prior = self.simulation_store.load_change_set(
                session_id, change_set.source_simulation_id
            )
        except Exception:
            return False
        if (
            source.change_set_id != change_set.change_set_id
            or source.change_set_revision + 1 != change_set.revision
            or prior.revision != source.change_set_revision
            or not evaluate_operation_conservation(goal, prior).complete
        ):
            return False
        alternative = next(
            (
                item
                for conflict in source.conflict_graph.conflicts
                for item in conflict.alternatives
                if item.alternative_id == change_set.resolution_alternative_id
            ),
            None,
        )
        if alternative is None or alternative.change_set_delta is None:
            return False
        expected = apply_change_set_delta(
            prior,
            alternative.change_set_delta,
            idempotency_key=change_set.idempotency_key,
            correlation_id=change_set.correlation_id,
            causation_id=change_set.causation_id,
        )
        return (
            expected.operations == change_set.operations
            and expected.constraints == change_set.constraints
        )
