from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.agent_execution import AgentChangeSetExecutionRecord, AgentExternalSyncOutboxRecord
from app.models.agent_goal import AgentGoalStateRecord
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceSeries
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.change_set_authoring import NativeChangeSetAuthoringCoordinator
from app.services.agent_kernel.contracts import (
    ApproveAndApplyChangeSetCommand,
    CalendarCreateBlockOperation,
    DraftChangeSetCommand,
    EntityRef,
    GoalState,
    Provenance,
    RequestedOperationClause,
    RecurrenceCreateSeriesOperation,
    RecurrenceRule,
    SemanticSlot,
    SemanticValue,
    SimulateChangeSetCommand,
    TaskCreateOperation,
)
from app.services.agent_kernel.recurrence_materialization import (
    recurrence_occurrence_dates,
)
from app.services.agent_kernel.simulation_coordinator import ChangeSetSimulationCoordinator
from app.services.agent_kernel.state_store import GoalStateStore
from app.services.agent_kernel.transaction_runtime import (
    ApprovalCommandConflict,
    AgentChangeSetTransactionRuntime,
    StaleApproval,
    TransactionRuntimeError,
)
from app.services.agent_kernel.domain_mutations import (
    ExternalEffect,
    NativeDomainMutationExecutor,
)
from app.services.strategist_sessions import delete_strategist_session


UTC = timezone.utc
PROVENANCE = Provenance(source_kind="user", source_ref="turn:1")


class AgentTransactionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        event.listen(
            self.engine,
            "connect",
            lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
        )
        SQLModel.metadata.create_all(self.engine)
        operations = [
            TaskCreateOperation(
                operationId="op:create-task",
                title="Interview prep",
                workspaceRef=EntityRef(kind="workspace", entityId="workspace:1", revision=1),
                tempRef="temp:task",
                fulfillsClauseIds=["clause:create"],
            ),
            CalendarCreateBlockOperation(
                operationId="op:create-block",
                dependsOn=["op:create-task"],
                taskRef=EntityRef(kind="temporary", entityId="temp:task", targetKind="task"),
                startAt=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
                endAt=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
                tempRef="temp:block",
                fulfillsClauseIds=["clause:schedule"],
            ),
        ]
        clauses = [
            RequestedOperationClause(
                clauseId="clause:create",
                operationKind="task.create",
                cardinality="exactly_one",
                expectedOperations=[operations[0]],
                provenance=PROVENANCE,
            ),
            RequestedOperationClause(
                clauseId="clause:schedule",
                operationKind="calendar.create_block",
                cardinality="exactly_one",
                expectedOperations=[operations[1]],
                provenance=PROVENANCE,
            ),
        ]
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.add(Category(id=1, name="Work"))
            session.commit()
            GoalStateStore(session).open(
                "session:1",
                GoalState(
                    goalId="goal:1",
                    status="planning",
                    originalRequest="Create and schedule interview prep.",
                    semanticSlots=[
                        SemanticSlot(
                            path="operations.requested",
                            semanticValue=SemanticValue(
                                state="present",
                                value=[item.model_dump(mode="json", by_alias=True) for item in clauses],
                                provenance=PROVENANCE,
                            ),
                        )
                    ],
                    correlationId="corr:1",
                    causationId="turn:1",
                ),
                command_id="goal:open",
            )
            session.commit()
            NativeChangeSetAuthoringCoordinator(session).draft(
                "session:1",
                DraftChangeSetCommand(
                    disposition="create",
                    expectedGoalRevision=1,
                    operations=operations,
                    preconditions={
                        "expectedEntityRevisions": [
                            {"kind": "workspace", "entityId": "workspace:1", "revision": 1}
                        ]
                    },
                ),
                command_id="draft:1",
                correlation_id="corr:1",
                causation_id="draft:1",
            )
            session.commit()
            self.simulation = ChangeSetSimulationCoordinator(session).simulate(
                "session:1",
                SimulateChangeSetCommand(expectedGoalRevision=2, expectedChangeSetRevision=1),
                command_id="simulate:1",
                correlation_id="corr:1",
                causation_id="simulate:1",
            )
            session.commit()

    def command(self, **updates: object) -> ApproveAndApplyChangeSetCommand:
        values = {
            "commandId": "approve:1",
            "idempotencyKey": "apply:1",
            "sessionId": "session:1",
            "goalId": "goal:1",
            "expectedGoalRevision": self.simulation.resulting_goal_revision,
            "changeSetId": self.simulation.change_set_id,
            "expectedChangeSetRevision": self.simulation.change_set_revision,
            "simulationId": self.simulation.simulation_id,
            "simulationInputFingerprint": self.simulation.input_snapshot.fingerprint,
            "simulationOutputFingerprint": self.simulation.output_fingerprint,
            "correlationId": "corr:1",
            "causationId": "approve:1",
        }
        values.update(updates)
        return ApproveAndApplyChangeSetCommand(**values)

    def test_apply_is_atomic_verified_and_exact_retry_replays_result(self) -> None:
        with Session(self.engine) as session:
            runtime = AgentChangeSetTransactionRuntime(session)
            result = runtime.approve_and_apply(self.command())
            replay = runtime.approve_and_apply(self.command())
            self.assertEqual(result, replay)
            self.assertEqual(result.status, "applied")
            self.assertEqual(result.verification.status, "verified")

        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 1)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 1)
            self.assertEqual(len(session.exec(select(AgentChangeSetExecutionRecord)).all()), 1)
            self.assertEqual(len(session.exec(select(AgentExternalSyncOutboxRecord)).all()), 1)
            goal = GoalStateStore(session).load("session:1", "goal:1")
            self.assertEqual(goal.status, "applied")
            self.assertEqual(goal.approved_change_set_revision, 1)

    def test_recurrence_materialization_is_identical_in_simulation_and_execution(self) -> None:
        recurrence_rule = RecurrenceRule(
            frequency="weekly",
            weekdays=["monday", "tuesday", "wednesday", "thursday"],
            startDate="2026-07-21",
            timezone="America/Vancouver",
            preferredStartTime="20:00",
            durationMinutes=60,
        )
        operations = [
            TaskCreateOperation(
                operationId="op:create-recurring-task",
                title="Team call",
                workspaceRef=EntityRef(
                    kind="workspace", entityId="workspace:2", revision=1
                ),
                tempRef="temp:recurring-task",
                fulfillsClauseIds=["clause:create-recurring-task"],
            ),
            RecurrenceCreateSeriesOperation(
                operationId="op:create-series",
                dependsOn=["op:create-recurring-task"],
                taskRef=EntityRef(
                    kind="temporary",
                    entityId="temp:recurring-task",
                    targetKind="task",
                ),
                rule=recurrence_rule,
                tempRef="temp:series",
                fulfillsClauseIds=["clause:create-series"],
            ),
        ]
        clauses = [
            RequestedOperationClause(
                clauseId="clause:create-recurring-task",
                operationKind="task.create",
                cardinality="exactly_one",
                expectedOperations=[operations[0]],
                provenance=PROVENANCE,
            ),
            RequestedOperationClause(
                clauseId="clause:create-series",
                operationKind="recurrence.create_series",
                cardinality="exactly_one",
                expectedOperations=[operations[1]],
                provenance=PROVENANCE,
            ),
        ]
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:recurrence"))
            session.add(Category(id=2, name="Calendar"))
            session.commit()
            GoalStateStore(session).open(
                "session:recurrence",
                GoalState(
                    goalId="goal:recurrence",
                    status="planning",
                    originalRequest="Create a one-hour Team call Monday through Thursday.",
                    semanticSlots=[
                        SemanticSlot(
                            path="operations.requested",
                            semanticValue=SemanticValue(
                                state="present",
                                value=[
                                    item.model_dump(mode="json", by_alias=True)
                                    for item in clauses
                                ],
                                provenance=PROVENANCE,
                            ),
                        )
                    ],
                    correlationId="corr:recurrence",
                    causationId="turn:recurrence",
                ),
                command_id="goal:open:recurrence",
            )
            session.commit()
            draft = NativeChangeSetAuthoringCoordinator(session).draft(
                "session:recurrence",
                DraftChangeSetCommand(
                    disposition="create",
                    expectedGoalRevision=1,
                    operations=operations,
                    preconditions={
                        "expectedEntityRevisions": [
                            {
                                "kind": "workspace",
                                "entityId": "workspace:2",
                                "revision": 1,
                            }
                        ]
                    },
                ),
                command_id="draft:recurrence",
                correlation_id="corr:recurrence",
                causation_id="draft:recurrence",
            )
            session.commit()
            simulation = ChangeSetSimulationCoordinator(session).simulate(
                "session:recurrence",
                SimulateChangeSetCommand(
                    expectedGoalRevision=2,
                    expectedChangeSetRevision=draft.change_set.revision,
                ),
                command_id="simulate:recurrence",
                correlation_id="corr:recurrence",
                causation_id="simulate:recurrence",
            )
            session.commit()
            result = AgentChangeSetTransactionRuntime(session).approve_and_apply(
                ApproveAndApplyChangeSetCommand(
                    commandId="approve:recurrence",
                    idempotencyKey="apply:recurrence",
                    sessionId="session:recurrence",
                    goalId="goal:recurrence",
                    expectedGoalRevision=simulation.resulting_goal_revision,
                    changeSetId=simulation.change_set_id,
                    expectedChangeSetRevision=simulation.change_set_revision,
                    simulationId=simulation.simulation_id,
                    simulationInputFingerprint=simulation.input_snapshot.fingerprint,
                    simulationOutputFingerprint=simulation.output_fingerprint,
                    correlationId="corr:recurrence",
                    causationId="approve:recurrence",
                )
            )

            blocks = session.exec(select(CalendarBlock)).all()
            series = session.exec(select(RecurrenceSeries)).one()

        expected_dates = recurrence_occurrence_dates(recurrence_rule)
        self.assertEqual(result.status, "applied")
        self.assertEqual(result.verification.status, "verified")
        self.assertEqual(len(blocks), len(expected_dates))
        self.assertEqual(series.materialized_through, expected_dates[-1])

    def test_stale_approval_and_mismatched_command_reuse_mutate_nothing(self) -> None:
        with Session(self.engine) as session:
            runtime = AgentChangeSetTransactionRuntime(session)
            with self.assertRaises(StaleApproval):
                runtime.approve_and_apply(self.command(expectedGoalRevision=99))
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 0)
            result = runtime.approve_and_apply(self.command())
            self.assertEqual(result.status, "applied")
            with self.assertRaises(ApprovalCommandConflict):
                runtime.approve_and_apply(self.command(simulationOutputFingerprint="f" * 64))

    def test_failure_after_task_create_rolls_back_goal_domain_execution_and_outbox(self) -> None:
        with Session(self.engine) as session:
            runtime = AgentChangeSetTransactionRuntime(session)
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                runtime.approve_and_apply(
                    self.command(), fail_after_operation="op:create-task"
                )
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 0)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 0)
            self.assertEqual(len(session.exec(select(AgentChangeSetExecutionRecord)).all()), 0)
            self.assertEqual(len(session.exec(select(AgentExternalSyncOutboxRecord)).all()), 0)
            self.assertEqual(GoalStateStore(session).load("session:1", "goal:1").status, "ready_for_review")

    def test_restart_resumes_verification_without_reapplying_and_mismatch_is_durable(self) -> None:
        with Session(self.engine) as session:
            pending = AgentChangeSetTransactionRuntime(session).approve_and_apply(
                self.command(), verify_after_commit=False
            )
            self.assertEqual(pending.status, "committed_pending_verification")
        with Session(self.engine) as session:
            task = session.exec(select(StrategyNode)).one()
            task.title = "Tampered after commit"
            session.add(task)
            session.commit()
        with Session(self.engine) as session:
            failed = AgentChangeSetTransactionRuntime(session).approve_and_apply(self.command())
            self.assertEqual(failed.status, "verification_failed")
            self.assertEqual(failed.verification.recovery, "compensating_review")
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 1)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 1)
            self.assertEqual(GoalStateStore(session).load("session:1", "goal:1").status, "system_failed")

    def test_idempotency_key_and_changeset_identity_replay_original_result(self) -> None:
        with Session(self.engine) as session:
            runtime = AgentChangeSetTransactionRuntime(session)
            original = runtime.approve_and_apply(self.command())
            by_key = runtime.approve_and_apply(self.command(commandId="approve:2"))
            by_change_set = runtime.approve_and_apply(
                self.command(commandId="approve:3", idempotencyKey="apply:3")
            )
            self.assertEqual(by_key, original)
            self.assertEqual(by_change_set, original)
            self.assertEqual(
                len(session.exec(select(AgentChangeSetExecutionRecord)).all()), 1
            )

    def test_precommit_projection_detects_executor_drift_and_rolls_back(self) -> None:
        original_apply = NativeDomainMutationExecutor.apply

        def drift(executor: NativeDomainMutationExecutor, change_set: object):
            result = original_apply(executor, change_set)
            task_binding = next(
                item for item in result.bindings if item.entity_ref.kind == "task"
            )
            task = executor.session.get(
                StrategyNode,
                int(task_binding.entity_ref.entity_id.removeprefix("task:")),
            )
            task.title = "Executor drift"
            executor.session.add(task)
            executor.session.flush()
            return result

        with Session(self.engine) as session:
            with patch.object(NativeDomainMutationExecutor, "apply", drift):
                with self.assertRaises(TransactionRuntimeError):
                    AgentChangeSetTransactionRuntime(session).approve_and_apply(
                        self.command()
                    )
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 0)
            self.assertEqual(len(session.exec(select(AgentChangeSetExecutionRecord)).all()), 0)

    def test_outbox_effect_ordinals_prevent_same_entity_collisions(self) -> None:
        original_apply = NativeDomainMutationExecutor.apply

        def duplicate_effect(executor: NativeDomainMutationExecutor, change_set: object):
            result = original_apply(executor, change_set)
            effect = result.external_effects[0]
            result.external_effects.append(
                ExternalEffect(effect.operation, effect.entity_id, dict(effect.payload))
            )
            return result

        with Session(self.engine) as session:
            with patch.object(NativeDomainMutationExecutor, "apply", duplicate_effect):
                result = AgentChangeSetTransactionRuntime(session).approve_and_apply(
                    self.command()
                )
            self.assertEqual(result.status, "applied")
            rows = session.exec(select(AgentExternalSyncOutboxRecord)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({item.semantic_key for item in rows}), 2)
            self.assertEqual(sorted(item.effect_ordinal for item in rows), [0, 1])

    def test_safe_boundary_reports_specific_stale_goal_code(self) -> None:
        with Session(self.engine) as session:
            outcome = AgentChangeSetTransactionRuntime(session).execute(
                self.command(expectedGoalRevision=99)
            )
            self.assertEqual(outcome.status, "error")
            self.assertEqual(outcome.error.code, "STALE_GOAL")

    def test_pending_verification_does_not_apply_a_corrected_goal(self) -> None:
        with Session(self.engine) as session:
            AgentChangeSetTransactionRuntime(session).approve_and_apply(
                self.command(), verify_after_commit=False
            )
            row = session.get(AgentGoalStateRecord, "goal:1")
            state = GoalState.model_validate_json(row.state_json)
            corrected = state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "status": "planning",
                    "approved_change_set_revision": None,
                }
            )
            row.revision = corrected.revision
            row.status = corrected.status
            row.state_json = corrected.model_dump_json()
            session.add(row)
            session.commit()
        with Session(self.engine) as session:
            result = AgentChangeSetTransactionRuntime(session).approve_and_apply(
                self.command()
            )
            self.assertEqual(result.status, "verification_failed")
            self.assertEqual(
                GoalStateStore(session).load("session:1", "goal:1").status,
                "planning",
            )

    def test_session_cleanup_removes_execution_history_but_keeps_product_rows(self) -> None:
        with Session(self.engine) as session:
            AgentChangeSetTransactionRuntime(session).approve_and_apply(self.command())
            self.assertTrue(delete_strategist_session(session, "session:1"))
            session.commit()
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(AgentChangeSetExecutionRecord)).all()), 0)
            self.assertEqual(len(session.exec(select(AgentExternalSyncOutboxRecord)).all()), 0)
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 1)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 1)


if __name__ == "__main__":
    unittest.main()
