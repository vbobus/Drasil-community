from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.agent_simulation import AgentSimulationEventRecord, AgentSimulationRecord
from app.models.agent_goal import AgentGoalTransitionRecord
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.change_set_authoring import NativeChangeSetAuthoringCoordinator
from app.services.agent_kernel.change_set_store import NativeChangeSetStore
from app.services.agent_kernel.change_set_deltas import apply_change_set_delta
from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    DraftChangeSetCommand,
    EntityRef,
    GoalState,
    Provenance,
    RequestedOperationClause,
    SemanticSlot,
    SemanticValue,
    SimulateChangeSetCommand,
)
from app.services.agent_kernel.simulation_coordinator import (
    ChangeSetSimulationCoordinator,
    SimulationInvariantError,
    SimulationRevisionConflict,
)
from app.services.agent_kernel.simulation import MAX_SIMULATION_EVIDENCE_FACTS
from app.services.agent_kernel.simulation_store import (
    SimulationAttemptIncomplete,
    SimulationCommandConflict,
    SimulationStore,
)
from app.services.strategist_sessions import delete_strategist_session
from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator
from app.services.agent_kernel.runtime import _apply_change_set_tool_simulate
from app.services.agent_kernel.state_store import GoalStateStore


UTC = timezone.utc
PROVENANCE = Provenance(source_kind="user", source_ref="turn:1")


class AgentSimulationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        event.listen(
            self.engine,
            "connect",
            lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
        )
        SQLModel.metadata.create_all(self.engine)
        operation = CalendarCreateBlockOperation(
            operation_id="op:schedule",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            start_at=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            temp_ref="temp:block",
            fulfills_clause_ids=["clause:schedule"],
        )
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.add(Category(id=1, name="Work"))
            session.add(StrategyNode(id=1, category_id=1, title="Interview", priority="high"))
            session.add(StrategyNode(id=2, category_id=1, title="Team call", priority="high"))
            session.commit()
            clause = RequestedOperationClause(
                clause_id="clause:schedule",
                operation_kind="calendar.create_block",
                cardinality="exactly_one",
                expected_operations=[operation],
                provenance=PROVENANCE,
            )
            GoalStateStore(session).open(
                "session:1",
                GoalState(
                    goal_id="goal:1",
                    status="planning",
                    original_request="Schedule the interview.",
                    semantic_slots=[
                        SemanticSlot(
                            path="operations.requested",
                            semantic_value=SemanticValue(
                                state="present",
                                value=[clause.model_dump(mode="json", by_alias=True)],
                                provenance=PROVENANCE,
                            ),
                        )
                    ],
                    correlation_id="corr:1",
                    causation_id="turn:1",
                ),
                command_id="goal:open",
            )
            session.commit()
            NativeChangeSetAuthoringCoordinator(session).draft(
                "session:1",
                DraftChangeSetCommand(
                    disposition="create", expected_goal_revision=1, operations=[operation]
                ),
                command_id="draft:1",
                correlation_id="corr:1",
                causation_id="draft:1",
            )
            session.commit()

    def test_clear_simulation_persists_exact_result_and_updates_lifecycle_without_domain_writes(self) -> None:
        with Session(self.engine) as session:
            before_task = session.get(StrategyNode, 1).model_dump()
            coordinator = ChangeSetSimulationCoordinator(session)
            result = coordinator.simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=2, expected_change_set_revision=1
                ),
                command_id="simulate:1",
                correlation_id="corr:run",
                causation_id="simulate:1",
            )
            session.commit()
            self.assertEqual(result.status, "ready_for_review")
            self.assertEqual(result.goal_revision, 3)
            self.assertEqual(result.resulting_goal_revision, 4)
            self.assertEqual(session.get(StrategyNode, 1).model_dump(), before_task)
            change_set = NativeChangeSetStore(session).load_current("session:1", "goal:1")
            self.assertEqual(change_set.status, "ready_for_review")
            self.assertEqual(change_set.simulation_result_ref, result.simulation_id)
            goal = GoalStateStore(session).load("session:1", "goal:1")
            self.assertEqual(goal.status, "ready_for_review")
            self.assertEqual(goal.revision, 4)
            transitions = session.exec(
                select(AgentGoalTransitionRecord).where(
                    AgentGoalTransitionRecord.goal_id == "goal:1"
                )
            ).all()
            self.assertIn("goal.simulating", [item.transition_kind for item in transitions])

    def test_large_authoritative_world_still_produces_a_valid_review_result(self) -> None:
        with Session(self.engine) as session:
            session.add_all(
                [
                    StrategyNode(
                        id=index,
                        category_id=1,
                        title=f"Unrelated task {index}",
                    )
                    for index in range(3, 2303)
                ]
            )
            session.commit()

            result = ChangeSetSimulationCoordinator(session).simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=2,
                    expected_change_set_revision=1,
                ),
                command_id="simulate:large-world",
                correlation_id="corr:large-world",
                causation_id="simulate:large-world",
            )

        self.assertEqual(result.status, "ready_for_review")
        self.assertEqual(
            len(result.input_snapshot.facts),
            MAX_SIMULATION_EVIDENCE_FACTS,
        )
        self.assertEqual(
            len(result.projected_final_facts),
            MAX_SIMULATION_EVIDENCE_FACTS,
        )
        self.assertLess(
            len(result.model_dump_json(by_alias=True).encode("utf-8")),
            512 * 1024,
        )
        self.assertIn(
            "op:schedule",
            {
                operation_id
                for fact in result.projected_final_facts
                for operation_id in fact.source_operation_ids
            },
        )

    def test_identical_command_replays_immutable_result_and_mismatch_fails(self) -> None:
        command = SimulateChangeSetCommand(
            expected_goal_revision=2, expected_change_set_revision=1
        )
        with Session(self.engine) as session:
            coordinator = ChangeSetSimulationCoordinator(session)
            first = coordinator.simulate(
                "session:1", command, command_id="simulate:replay",
                correlation_id="corr:run", causation_id="simulate:replay",
            )
            session.commit()
            replay = coordinator.simulate(
                "session:1", command, command_id="simulate:replay",
                correlation_id="corr:run", causation_id="simulate:replay",
            )
            self.assertEqual(replay, first)
            self.assertEqual(
                len(session.exec(select(AgentSimulationRecord)).all()), 1
            )
            with self.assertRaises(SimulationRevisionConflict):
                coordinator.simulate(
                    "session:1",
                    SimulateChangeSetCommand(
                        expected_goal_revision=3, expected_change_set_revision=1
                    ),
                    command_id="simulate:replay",
                    correlation_id="corr:run", causation_id="simulate:replay",
                )

    def test_stale_revision_fails_before_snapshot_or_lifecycle_changes(self) -> None:
        with Session(self.engine) as session:
            stale = ChangeSetSimulationCoordinator(session).simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=1, expected_change_set_revision=1
                ),
                command_id="simulate:stale",
                correlation_id="corr:run", causation_id="simulate:stale",
            )
            self.assertEqual(stale.status, "stale")
            self.assertEqual(stale.issues[0].code, "STALE_CHANGE_SET")
            self.assertEqual(GoalStateStore(session).load("session:1", "goal:1").status, "planning")
            self.assertEqual(len(session.exec(select(AgentSimulationRecord)).all()), 1)

    def test_pending_domain_mutation_is_rejected_without_autoflush(self) -> None:
        with Session(self.engine) as session:
            task = session.get(StrategyNode, 1)
            original = task.title
            task.title = "Pending mutation"
            with self.assertRaisesRegex(SimulationInvariantError, "clean domain session"):
                ChangeSetSimulationCoordinator(session).simulate(
                    "session:1",
                    SimulateChangeSetCommand(
                        expected_goal_revision=2, expected_change_set_revision=1
                    ),
                    command_id="simulate:pending-domain",
                    correlation_id="corr:run",
                    causation_id="simulate:pending-domain",
                )
            with session.no_autoflush:
                self.assertEqual(len(session.exec(select(AgentSimulationRecord)).all()), 0)
            session.rollback()
            self.assertEqual(session.get(StrategyNode, 1).title, original)

    def test_stored_changeset_decision_applies_exact_delta_and_invalidates_readiness(self) -> None:
        with Session(self.engine) as session:
            session.add(
                CalendarBlock(
                    id=1,
                    task_id=2,
                    category_id=1,
                    start_at=datetime(2026, 7, 20, 11, 0),
                    end_at=datetime(2026, 7, 20, 12, 0),
                )
            )
            session.commit()
            simulation = ChangeSetSimulationCoordinator(session).simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=2, expected_change_set_revision=1
                ),
                command_id="simulate:decision",
                correlation_id="corr:decision",
                causation_id="simulate:decision",
            )
            session.commit()
            self.assertEqual(simulation.status, "needs_user_decision")
            decision = simulation.conflict_graph.decision_request
            option = decision.options[0]
            updated = GoalStateCoordinator(session).apply_ui_decision(
                session_id="session:1",
                goal_id="goal:1",
                goal_revision=4,
                decision_id=decision.decision_id,
                option_id=option.option_id,
                source_event_id="event:decision:1",
            )
            revised = NativeChangeSetStore(session).load_current("session:1", "goal:1")
            self.assertEqual(updated.status, "planning")
            self.assertEqual(updated.revision, 5)
            self.assertEqual(revised.revision, 2)
            self.assertEqual(revised.status, "draft")
            self.assertIsNone(revised.simulation_result_ref)
            self.assertEqual(updated.resolved_decisions[0].option_id, option.option_id)

    def test_model_can_apply_stored_repair_and_resimulate_clear_in_same_run(self) -> None:
        with Session(self.engine) as session:
            session.add(
                CalendarBlock(
                    id=1,
                    task_id=2,
                    category_id=1,
                    start_at=datetime(2026, 7, 20, 11, 0),
                    end_at=datetime(2026, 7, 20, 12, 0),
                    is_pinned=True,
                    is_protected=True,
                )
            )
            session.commit()
            coordinator = ChangeSetSimulationCoordinator(session)
            conflicted = coordinator.simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=2, expected_change_set_revision=1
                ),
                command_id="simulate:repair:1",
                correlation_id="corr:repair",
                causation_id="simulate:repair:1",
            )
            session.commit()
            self.assertEqual(conflicted.status, "repairing")
            alternative = conflicted.conflict_graph.conflicts[0].alternatives[0]
            prior = NativeChangeSetStore(session).load_current("session:1", "goal:1")
            expected = apply_change_set_delta(
                prior,
                alternative.change_set_delta,
                idempotency_key="draft:repair",
                correlation_id="corr:repair",
                causation_id="draft:repair",
            )
            revised = NativeChangeSetAuthoringCoordinator(session).draft(
                "session:1",
                DraftChangeSetCommand(
                    disposition="revise",
                    expected_goal_revision=4,
                    expected_change_set_revision=1,
                    operations=expected.operations,
                    constraints=expected.constraints,
                    preconditions=expected.preconditions,
                    assumptions=expected.assumptions,
                    source_simulation_id=conflicted.simulation_id,
                    resolution_alternative_id=alternative.alternative_id,
                ),
                command_id="draft:repair",
                correlation_id="corr:repair",
                causation_id="draft:repair",
            )
            session.commit()
            self.assertEqual(revised.change_set.revision, 2)
            clear = coordinator.simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=5, expected_change_set_revision=2
                ),
                command_id="simulate:repair:2",
                correlation_id="corr:repair",
                causation_id="simulate:repair:2",
            )
            session.commit()
            self.assertEqual(clear.status, "ready_for_review")

    def test_started_attempt_is_durable_and_restart_recovery_finalizes_once(self) -> None:
        command = SimulateChangeSetCommand(
            expected_goal_revision=2, expected_change_set_revision=1
        )
        with Session(self.engine) as session:
            coordinator = ChangeSetSimulationCoordinator(session)
            with patch(
                "app.services.agent_kernel.simulation_coordinator.SimulationSnapshotLoader.load",
                side_effect=RuntimeError("injected crash after durable start"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    coordinator.simulate(
                        "session:1",
                        command,
                        command_id="simulate:restart",
                        correlation_id="corr:restart",
                        causation_id="simulate:restart",
                    )
            session.rollback()
            record = session.exec(select(AgentSimulationRecord)).one()
            self.assertEqual(record.status, "started")
            events = session.exec(select(AgentSimulationEventRecord)).all()
            self.assertEqual([item.event_kind for item in events], ["simulation.started"])
            record.updated_at = datetime.utcnow() - timedelta(minutes=1)
            session.add(record)
            session.commit()

        with Session(self.engine) as restarted:
            recovered = ChangeSetSimulationCoordinator(restarted).simulate(
                "session:1",
                command,
                command_id="simulate:restart",
                correlation_id="corr:restart",
                causation_id="simulate:restart",
            )
            restarted.commit()
            self.assertEqual(recovered.status, "ready_for_review")
            events = restarted.exec(
                select(AgentSimulationEventRecord).order_by(
                    AgentSimulationEventRecord.created_at
                )
            ).all()
            self.assertEqual(
                [item.event_kind for item in events],
                ["simulation.started", "simulation.ready_for_review"],
            )

    def test_fresh_duplicate_start_is_typed_in_progress_then_replays_winner(self) -> None:
        with Session(self.engine) as winner:
            coordinator = ChangeSetSimulationCoordinator(winner)
            goal = coordinator.goal_store.load_active("session:1")
            change_set = coordinator.change_set_store.load_current("session:1", goal.goal_id)
            command = SimulateChangeSetCommand(
                expected_goal_revision=2, expected_change_set_revision=1
            )
            fingerprint = coordinator._payload_fingerprint(command)
            store = SimulationStore(winner)
            store.start(
                "session:1",
                simulation_id=coordinator._simulation_id("session:1", "simulate:race"),
                command_id="simulate:race",
                payload_fingerprint=fingerprint,
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                change_set=change_set,
                correlation_id="corr:race",
                causation_id="simulate:race",
            )
            winner.commit()

        with Session(self.engine) as loser:
            store = SimulationStore(loser)
            with self.assertRaises(SimulationAttemptIncomplete):
                store.replay(
                    "session:1", "simulate:race", payload_fingerprint=fingerprint
                )

    def test_corrupt_redundant_simulation_metadata_fails_closed(self) -> None:
        with Session(self.engine) as session:
            result = ChangeSetSimulationCoordinator(session).simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=2, expected_change_set_revision=1
                ),
                command_id="simulate:integrity",
                correlation_id="corr:integrity",
                causation_id="simulate:integrity",
            )
            session.commit()
            record = session.get(AgentSimulationRecord, result.simulation_id)
            record.input_snapshot_fingerprint = "corrupt"
            session.add(record)
            session.commit()
            with self.assertRaisesRegex(
                SimulationCommandConflict, "identity is inconsistent"
            ):
                SimulationStore(session).load("session:1", result.simulation_id)

    def test_authoritative_session_cleanup_removes_kernel_history_only(self) -> None:
        with Session(self.engine) as session:
            result = ChangeSetSimulationCoordinator(session).simulate(
                "session:1",
                SimulateChangeSetCommand(
                    expected_goal_revision=2, expected_change_set_revision=1
                ),
                command_id="simulate:cleanup",
                correlation_id="corr:cleanup",
                causation_id="simulate:cleanup",
            )
            session.commit()
            self.assertIsNotNone(session.get(StrategyNode, 1))
            self.assertTrue(delete_strategist_session(session, "session:1"))
            session.commit()
            self.assertIsNone(session.get(StrategistChatSession, "session:1"))
            self.assertEqual(session.exec(select(AgentSimulationRecord)).all(), [])
            self.assertEqual(session.exec(select(AgentSimulationEventRecord)).all(), [])
            self.assertIsNotNone(session.get(StrategyNode, 1))

    def test_historical_replay_does_not_replace_current_simulation_memory(self) -> None:
        with Session(self.engine) as session:
            memory: dict = {}
            coordinator = ChangeSetSimulationCoordinator(session)
            first_payload = _apply_change_set_tool_simulate(
                coordinator=coordinator,
                session_memory=memory,
                session_id="session:1",
                source_turn_id="turn:memory",
                correlation_id="corr:memory",
                current_run_id=None,
                arguments={"expectedGoalRevision": 2, "expectedChangeSetRevision": 1},
            )
            first_id = first_payload["simulationId"]
            session.commit()
            prior = NativeChangeSetStore(session).load_current("session:1", "goal:1")
            revised = NativeChangeSetAuthoringCoordinator(session).draft(
                "session:1",
                DraftChangeSetCommand(
                    disposition="revise",
                    expected_goal_revision=4,
                    expected_change_set_revision=1,
                    operations=prior.operations,
                    constraints=prior.constraints,
                    preconditions=prior.preconditions,
                    assumptions=prior.assumptions,
                ),
                command_id="draft:memory:2",
                correlation_id="corr:memory",
                causation_id="draft:memory:2",
            )
            session.commit()
            second_payload = _apply_change_set_tool_simulate(
                coordinator=coordinator,
                session_memory=memory,
                session_id="session:1",
                source_turn_id="turn:memory",
                correlation_id="corr:memory",
                current_run_id=None,
                arguments={
                    "expectedGoalRevision": revised.goal_revision,
                    "expectedChangeSetRevision": revised.change_set.revision,
                },
            )
            second_id = second_payload["simulationId"]
            session.commit()
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(memory["current_change_set_simulation"]["simulationId"], second_id)

            replay = _apply_change_set_tool_simulate(
                coordinator=coordinator,
                session_memory=memory,
                session_id="session:1",
                source_turn_id="turn:memory",
                correlation_id="corr:memory",
                current_run_id=None,
                arguments={"expectedGoalRevision": 2, "expectedChangeSetRevision": 1},
            )
            self.assertEqual(replay["simulationId"], first_id)
            current = memory.get("current_change_set_simulation")
            self.assertTrue(
                current is None or current.get("simulationId") == second_id,
                current,
            )


if __name__ == "__main__":
    unittest.main()
