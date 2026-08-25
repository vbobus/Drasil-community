from __future__ import annotations

import unittest

from sqlmodel import Session, SQLModel, create_engine

from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn
from app.services.agent_kernel.contracts import (
    DecisionDelta,
    DecisionRequest,
    GoalStatePatch,
    GoalStateUpdateCommand,
    Provenance,
)
from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator
from app.services.agent_kernel.goal_state_reducer import transition_goal
from app.services.agent_kernel.session_memory import project_goal_state_memory
from app.services.agent_kernel.tool_registry import execute_kernel_tool, registered_kernel_tool_specs
from app.services.agent_kernel.state_store import GoalStateRevisionConflict
from app.services.agent_kernel.runtime import append_kernel_event
from app.services.strategist_runs import (
    create_strategist_run,
    mark_strategist_run_started,
    serialize_strategist_run,
)


class GoalStateCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.commit()

    def test_ensure_active_is_idempotent_and_correction_uses_typed_command(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = coordinator.ensure_active(
                session_id="session:1",
                original_request="Schedule the interview.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
                current_run_id="run:1",
            )
            self.assertEqual(
                coordinator.ensure_active(
                    session_id="session:1",
                    original_request="ignored on retry",
                    correlation_id="corr:retry",
                    causation_id="turn:1",
                    command_id="turn:1",
                    current_run_id="run:1",
                ).goal_id,
                state.goal_id,
            )
            corrected = coordinator.apply_model_update(
                session_id="session:1",
                source_turn_id="turn:2",
                correlation_id="corr:2",
                current_run_id="run:2",
                command=GoalStateUpdateCommand(
                    command_id="command:next-week",
                    disposition="correct",
                    goal_id=state.goal_id,
                    expected_revision=state.revision,
                    patches=[
                        GoalStatePatch(
                            path="temporal.requested_date",
                            operation="set",
                            value="next-week",
                            provenance=Provenance(source_kind="user", source_ref="turn:2"),
                        )
                    ],
                ),
            )
            self.assertEqual(corrected.revision, 2)
            self.assertEqual(corrected.semantic_slots[0].semantic_value.value, "next-week")

    def test_new_goal_explicitly_supersedes_the_prior_goal(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            first = coordinator.ensure_active(
                session_id="session:1",
                original_request="Schedule the team call.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            second = coordinator.apply_model_update(
                session_id="session:1",
                source_turn_id="turn:2",
                correlation_id="corr:2",
                current_run_id="run:2",
                command=GoalStateUpdateCommand(
                    command_id="command:new-interview",
                    disposition="new_goal",
                    original_request="Prepare a new interview request.",
                    patches=[],
                ),
            )
            self.assertNotEqual(second.goal_id, first.goal_id)
            self.assertEqual(coordinator.store.load("session:1", first.goal_id).status, "superseded")
            self.assertNotIn("team call", second.original_request.lower())

    def test_completed_team_call_goal_does_not_leak_into_new_interview_request(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            first = coordinator.ensure_active(
                session_id="session:1",
                original_request="Schedule the completed team call.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            completed = coordinator.sync_legacy_outcome(
                session_id="session:1",
                goal_id=first.goal_id,
                outcome="completed",
                command_id="complete:team-call",
            )
            self.assertEqual(completed.status, "applied")
            second = coordinator.ensure_active(
                session_id="session:1",
                original_request="Prepare a new interview request.",
                correlation_id="corr:2",
                causation_id="turn:2",
                command_id="turn:2",
            )
            self.assertNotEqual(second.goal_id, first.goal_id)
            self.assertEqual(second.original_request, "Prepare a new interview request.")

    def test_compatibility_completion_never_fabricates_approval_or_commit(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = coordinator.ensure_active(
                session_id="session:1",
                original_request="Answer the question.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            completed = coordinator.sync_legacy_outcome(
                session_id="session:1",
                goal_id=state.goal_id,
                outcome="completed",
                command_id="complete:answer",
            )
            self.assertEqual(completed.status, "applied")
            transitions = coordinator.store.replay("session:1", state.goal_id)
            self.assertEqual(
                [item.transition_kind for item in transitions],
                ["goal.opened", "goal.applied"],
            )

    def test_verified_legacy_commit_closes_an_already_approved_goal(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = coordinator.ensure_active(
                session_id="session:1",
                original_request="Create and schedule Team Call.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            reviewed = coordinator.sync_legacy_outcome(
                session_id="session:1",
                goal_id=state.goal_id,
                outcome="review",
                command_id="review:team-call",
            )
            approved = transition_goal(
                reviewed,
                "approved",
                command_id="approve:team-call",
            )
            approved = coordinator.store.save(
                "session:1",
                approved,
                expected_revision=reviewed.revision,
                command_id="approve:team-call",
                transition_kind="goal.approved",
            )
            session.commit()

            applied = coordinator.sync_legacy_outcome(
                session_id="session:1",
                goal_id=approved.goal_id,
                outcome="applied",
                command_id="legacy-commit:team-call",
                verified_commit=True,
            )

            self.assertEqual(applied.status, "applied")
            self.assertIsNone(coordinator.store.load_active("session:1"))
            self.assertEqual(
                [
                    item.transition_kind
                    for item in coordinator.store.replay("session:1", state.goal_id)
                ][-2:],
                ["goal.committing", "goal.applied"],
            )

    def test_native_review_failure_does_not_mark_goal_applied(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = coordinator.ensure_active(
                session_id="session:1",
                original_request="Move the recurring call to 8 PM.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )

            failed = coordinator.sync_legacy_outcome(
                session_id="session:1",
                goal_id=state.goal_id,
                outcome="system_failed",
                command_id="native-review:failed",
            )

            self.assertEqual(failed.status, "system_failed")
            transitions = coordinator.store.replay("session:1", state.goal_id)
            self.assertEqual(
                [item.transition_kind for item in transitions],
                ["goal.opened", "goal.system_failed"],
            )

    def test_session_memory_projection_uses_canonical_goal_not_event_window(self) -> None:
        with Session(self.engine) as session:
            state = GoalStateCoordinator(session).ensure_active(
                session_id="session:1",
                original_request="Use that task itself and schedule it.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            memory = project_goal_state_memory({"active_request": None}, state)
            self.assertEqual(memory["source"], "durable_goal_state")
            self.assertEqual(memory["active_request"]["original_request"], state.original_request)
            self.assertEqual(memory["goal_state"]["goalId"], state.goal_id)

    def test_goal_transition_is_a_registered_non_domain_tool(self) -> None:
        specs = registered_kernel_tool_specs()
        spec = next(item for item in specs if item.name == "set_goal_transition")
        self.assertFalse(spec.read_only)
        self.assertEqual(spec.side_effect, "goal_state_write")
        self.assertEqual(spec.approval_requirement, "none")
        self.assertNotIn("update_goal_state", {item.name for item in specs})
        callback = next(
            item for item in specs if item.name == "respond_to_planner_decision"
        )
        self.assertFalse(callback.read_only)
        self.assertEqual(callback.side_effect, "planner_state_write")
        self.assertEqual(callback.approval_requirement, "none")

    def test_goal_state_tool_returns_a_typed_recoverable_conflict(self) -> None:
        result = execute_kernel_tool(
            "set_goal_transition",
            {
                "disposition": "continue",
                "patches": [],
            },
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            goal_state_updater=lambda _arguments: (_ for _ in ()).throw(
                GoalStateRevisionConflict("stale goal revision")
            ),
        )
        self.assertEqual(result["error"], "goal_state_stale")
        self.assertTrue(result["retryable"])
        self.assertEqual(
            result["tool_error"]["details"]["conflictKind"],
            "goal_state_stale",
        )

    def test_model_can_persist_a_typed_decision_for_ui_projection(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = coordinator.ensure_active(
                session_id="session:1",
                original_request="Schedule the interview.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            decision = DecisionRequest(
                decision_id="decision:date",
                goal_id=state.goal_id,
                goal_revision=2,
                target_revision=3,
                question="Which date?",
                reason="Two valid dates remain.",
                options=[
                    DecisionDelta(
                        option_id="option:tomorrow",
                        label="Tomorrow",
                        patches=[
                            GoalStatePatch(
                                path="temporal.requested_date",
                                operation="set",
                                value="tomorrow",
                                provenance=Provenance(
                                    source_kind="inference",
                                    source_ref="turn:1",
                                    confidence=0.8,
                                ),
                            )
                        ],
                    )
                ],
            )
            updated = coordinator.apply_model_update(
                session_id="session:1",
                source_turn_id="turn:1",
                correlation_id="corr:1",
                current_run_id=None,
                command=GoalStateUpdateCommand(
                    command_id="command:decision",
                    disposition="request_decision",
                    goal_id=state.goal_id,
                    expected_revision=1,
                    decisions=[decision],
                ),
            )
            self.assertEqual(updated.status, "needs_user_decision")
            self.assertEqual(updated.unresolved_decisions[0].decision_id, "decision:date")

    def test_new_runs_link_to_the_active_goal_and_expose_trace_identity(self) -> None:
        with Session(self.engine) as session:
            goal = GoalStateCoordinator(session).ensure_active(
                session_id="session:1",
                original_request="Schedule the interview.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            turn = StrategistChatTurn(id="turn:run", session_id="session:1", prompt="Continue")
            session.add(turn)
            session.commit()
            run = create_strategist_run(
                session,
                strategist_session_id="session:1",
                turn_id=turn.id,
                prompt="Continue",
                request_payload={"prompt": "Continue"},
                control_plane="agent-kernel",
            )
            payload = serialize_strategist_run(run)
            self.assertEqual(payload["goal_id"], goal.goal_id)
            self.assertEqual(payload["goal_revision"], goal.revision)
            self.assertTrue(payload["correlation_id"])
            self.assertTrue(payload["causation_id"])
            event = append_kernel_event(
                session,
                strategist_session_id="session:1",
                correlation_id=run.correlation_id,
                sequence=0,
                event_type="test.identity",
                payload={},
                turn_id=turn.id,
            )
            self.assertEqual(event.goal_id, goal.goal_id)
            self.assertEqual(event.goal_revision, goal.revision)
            self.assertEqual(event.run_id, run.id)

    def test_run_claim_is_atomic_and_terminal_events_keep_goal_identity(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            goal = coordinator.ensure_active(
                session_id="session:1",
                original_request="Answer the planning question.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            turn = StrategistChatTurn(id="turn:claim", session_id="session:1", prompt="Continue")
            session.add(turn)
            session.commit()
            run = create_strategist_run(
                session,
                strategist_session_id="session:1",
                turn_id=turn.id,
                prompt="Continue",
                request_payload={},
                control_plane="agent-kernel",
            )
            self.assertTrue(mark_strategist_run_started(session, run))
            self.assertFalse(mark_strategist_run_started(session, run))
            completed = coordinator.sync_legacy_outcome(
                session_id="session:1",
                goal_id=goal.goal_id,
                outcome="completed",
                command_id="complete:claim",
                current_run_id=run.id,
            )
            run.goal_revision = completed.revision
            session.add(run)
            session.commit()
            event = append_kernel_event(
                session,
                strategist_session_id="session:1",
                correlation_id=run.correlation_id,
                sequence=1,
                event_type="final_answer",
                payload={},
                turn_id=turn.id,
            )
            self.assertEqual(event.goal_id, goal.goal_id)
            self.assertEqual(event.goal_revision, completed.revision)


if __name__ == "__main__":
    unittest.main()
