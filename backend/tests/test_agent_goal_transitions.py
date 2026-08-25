from __future__ import annotations

import unittest

from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine

from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.contracts import (
    GoalDeltaPatch,
    GoalTurnDecision,
)
from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator
from app.services.agent_kernel.planner_tool_contracts import (
    planner_model_tool_definitions,
)
from app.services.agent_kernel.state_store import GoalStateRevisionConflict
from app.services.agent_kernel.tool_registry import registered_kernel_tool_schemas


class AgentGoalTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:goal-turn"))
            session.commit()

    def test_provider_contract_has_only_semantic_fields(self) -> None:
        schema = GoalTurnDecision.model_json_schema()
        properties = set(schema["properties"])

        self.assertEqual(properties, {"disposition", "patches"})
        self.assertTrue(
            {
                "sessionId",
                "goalId",
                "expectedRevision",
                "commandId",
                "correlationId",
                "causationId",
                "provenance",
            }.isdisjoint(properties)
        )

    def test_only_strategist_provider_surface_has_the_transition_tool(self) -> None:
        strategist_names = {
            item["name"] for item in registered_kernel_tool_schemas()
        }
        planner_names = {
            item["name"] for item in planner_model_tool_definitions()
        }

        self.assertIn("set_goal_transition", strategist_names)
        self.assertNotIn("update_goal_state", strategist_names)
        self.assertNotIn("set_goal_transition", planner_names)

    def test_decision_validation_is_closed_and_sparse(self) -> None:
        with self.assertRaises(ValidationError):
            GoalTurnDecision(disposition="correct")
        with self.assertRaises(ValidationError):
            GoalTurnDecision(
                disposition="continue",
                patches=[
                    GoalDeltaPatch(
                        path="temporal.duration_minutes",
                        operation="set",
                        value=30,
                    )
                ],
            )
        with self.assertRaises(ValidationError):
            GoalTurnDecision(disposition="new-topic")  # type: ignore[arg-type]

    def test_continue_is_a_noop_and_exact_replay_is_idempotent(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            original = coordinator.ensure_active(
                session_id="session:goal-turn",
                original_request="Prepare the application.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )

            continued = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:2",
                current_user_turn="Add a checklist for it.",
                correlation_id="corr:2",
                current_run_id=None,
                decision=GoalTurnDecision(disposition="continue"),
            )
            replay = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:2",
                current_user_turn="Add a checklist for it.",
                correlation_id="corr:2",
                current_run_id=None,
                decision=GoalTurnDecision(disposition="continue"),
            )

            self.assertEqual(continued.revision, original.revision)
            self.assertEqual(replay, continued)

            corrected = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:3",
                current_user_turn="Make it due Friday.",
                correlation_id="corr:3",
                current_run_id=None,
                decision=GoalTurnDecision(
                    disposition="correct",
                    patches=[
                        GoalDeltaPatch(
                            path="temporal.requested_date",
                            operation="set",
                            value="2026-07-31",
                        )
                    ],
                ),
            )
            current_replay = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:2",
                current_user_turn="Add a checklist for it.",
                correlation_id="corr:2",
                current_run_id=None,
                decision=GoalTurnDecision(disposition="continue"),
            )
            self.assertEqual(current_replay, corrected)

            with self.assertRaises(GoalStateRevisionConflict):
                coordinator.apply_turn_decision(
                    session_id="session:goal-turn",
                    source_turn_id="turn:2",
                    current_user_turn="This is unrelated.",
                    correlation_id="corr:2",
                    current_run_id=None,
                    decision=GoalTurnDecision(disposition="new"),
                )

    def test_new_goal_references_require_current_turn_authority(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            coordinator.ensure_active(
                session_id="session:goal-turn",
                original_request="Prepare the application.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            decision = GoalTurnDecision(
                disposition="new",
                patches=[
                    GoalDeltaPatch(
                        path="scope.parent",
                        operation="set",
                        value="task:42",
                    )
                ],
            )
            with self.assertRaisesRegex(
                Exception,
                "not authorized by current-turn evidence",
            ):
                coordinator.apply_turn_decision(
                    session_id="session:goal-turn",
                    source_turn_id="turn:2",
                    current_user_turn="Create a child under task 42.",
                    correlation_id="corr:2",
                    current_run_id=None,
                    decision=decision,
                    authorized_reference_ids=set(),
                )

            accepted = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:2",
                current_user_turn="Create a child under task 42.",
                correlation_id="corr:2",
                current_run_id=None,
                decision=decision,
                authorized_reference_ids={"task:42"},
            )
            self.assertEqual(
                {
                    slot.path: slot.semantic_value.value
                    for slot in accepted.semantic_slots
                }["scope.parent"],
                "task:42",
            )

    def test_sparse_correction_preserves_unaddressed_state(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = coordinator.ensure_active(
                session_id="session:goal-turn",
                original_request="Prepare the application by Friday.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            seeded = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:seed",
                current_user_turn="It is due Friday and should take one hour.",
                correlation_id="corr:seed",
                current_run_id=None,
                decision=GoalTurnDecision(
                    disposition="correct",
                    patches=[
                        GoalDeltaPatch(
                            path="temporal.requested_date",
                            operation="set",
                            value="2026-07-24",
                        ),
                        GoalDeltaPatch(
                            path="temporal.duration_minutes",
                            operation="set",
                            value=60,
                        ),
                    ],
                ),
            )
            corrected = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:3",
                current_user_turn="Make that ninety minutes.",
                correlation_id="corr:3",
                current_run_id=None,
                decision=GoalTurnDecision(
                    disposition="correct",
                    patches=[
                        GoalDeltaPatch(
                            path="temporal.duration_minutes",
                            operation="set",
                            value=90,
                        )
                    ],
                ),
            )

            slots = {
                item.path: item.semantic_value.value
                for item in corrected.semantic_slots
            }
            self.assertEqual(state.revision, 1)
            self.assertEqual(seeded.revision, 2)
            self.assertEqual(corrected.revision, 3)
            self.assertEqual(slots["temporal.requested_date"], "2026-07-24")
            self.assertEqual(slots["temporal.duration_minutes"], 90)

    def test_new_goal_drops_prior_state_and_supersedes_old_goal(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            first = coordinator.ensure_active(
                session_id="session:goal-turn",
                original_request="Prepare a travel application.",
                correlation_id="corr:1",
                causation_id="turn:1",
                command_id="turn:1",
            )
            first = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:2",
                current_user_turn="Use Travel Planning as its parent.",
                correlation_id="corr:2",
                current_run_id=None,
                decision=GoalTurnDecision(
                    disposition="correct",
                    patches=[
                        GoalDeltaPatch(
                            path="scope.parent",
                            operation="set",
                            value="task:travel-planning",
                        )
                    ],
                ),
            )
            second = coordinator.apply_turn_decision(
                session_id="session:goal-turn",
                source_turn_id="turn:3",
                current_user_turn="Create a container learning checklist.",
                correlation_id="corr:3",
                current_run_id=None,
                decision=GoalTurnDecision(disposition="new"),
            )

            self.assertNotEqual(second.goal_id, first.goal_id)
            self.assertEqual(second.original_request, "Create a container learning checklist.")
            self.assertEqual(second.semantic_slots, [])
            self.assertIsNone(second.current_change_set_id)
            self.assertEqual(
                coordinator.store.load("session:goal-turn", first.goal_id).status,
                "superseded",
            )


if __name__ == "__main__":
    unittest.main()
