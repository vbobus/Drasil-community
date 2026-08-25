from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine

from app.api.ai import StrategistClarificationChoice, StrategistUiEvent
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.contracts import (
    DecisionConflictDetail,
    DecisionDelta,
    DecisionRequest,
    GoalState,
    GoalStatePatch,
    Provenance,
)
from app.services.agent_kernel.goal_state_coordinator import (
    GoalStateCoordinator,
    decision_clarification_payload,
)
from app.services.agent_kernel.goal_state_reducer import GoalStateDecisionConflict


class GoalStateUiDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.add(StrategistChatSession(id="session:2"))
            session.commit()

    def decision_state(self) -> GoalState:
        option = DecisionDelta(
            option_id="option:workout",
            label="Move the workout",
            patches=[
                GoalStatePatch(
                    path="conflict.occurrence_scope",
                    operation="set",
                    value=["occurrence:workout:1"],
                    provenance=Provenance(source_kind="ui", source_ref="event:stored"),
                )
            ],
        )
        duplicate_label_option = DecisionDelta(
            option_id="option:call",
            label="Move the workout",
            patches=[
                GoalStatePatch(
                    path="conflict.occurrence_scope",
                    operation="set",
                    value=["occurrence:call:1"],
                    provenance=Provenance(source_kind="ui", source_ref="event:stored"),
                )
            ],
        )
        return GoalState(
            goal_id="goal:decision",
            status="needs_user_decision",
            original_request="Move the workout, not the call.",
            unresolved_decisions=[
                DecisionRequest(
                    decision_id="decision:move",
                    goal_id="goal:decision",
                    goal_revision=1,
                    target_revision=2,
                    question="Which commitment should move?",
                    reason="They overlap.",
                    conflicts=[
                        DecisionConflictDetail(
                            date="2026-08-13",
                            incoming_task_title="Send Kunji the updated resume",
                            requested_start_at=datetime(2026, 8, 13, 6, 30, tzinfo=timezone.utc),
                            requested_end_at=datetime(2026, 8, 13, 7, 0, tzinfo=timezone.utc),
                            blocker_task_title="Wake-up time",
                            blocker_start_at=datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc),
                            blocker_end_at=datetime(2026, 8, 13, 7, 0, tzinfo=timezone.utc),
                            fixed_reason="This scheduling rule is fixed.",
                            decision_reason="The request is before the saved wake-up time.",
                            recommended_action="preserve_existing",
                        )
                    ],
                    options=[option, duplicate_label_option],
                )
            ],
            correlation_id="corr:1",
            causation_id="turn:1",
        )

    def test_api_requires_complete_typed_decision_identity(self) -> None:
        event = StrategistUiEvent.model_validate(
            {
                "kind": "clarification_choice",
                "goalId": "goal:decision",
                "goalRevision": 1,
                "decisionId": "decision:move",
                "optionId": "option:workout",
                "eventId": "event:1",
                "idempotencyKey": "event:1",
                "payload": {"choiceLabel": "This label is presentation only"},
            }
        )
        self.assertEqual(event.option_id, "option:workout")
        with self.assertRaises(ValidationError):
            StrategistUiEvent.model_validate(
                {
                    "kind": "clarification_choice",
                    "goalId": "goal:decision",
                    "decisionId": "decision:move",
                }
            )
        with self.assertRaises(ValidationError):
            StrategistUiEvent.model_validate(
                {
                    "kind": "clarification_choice",
                    "goalId": " ",
                    "goalRevision": 1,
                    "decisionId": "decision:move",
                    "optionId": "option:workout",
                    "eventId": "event:1",
                    "idempotencyKey": "event:1",
                }
            )
        with self.assertRaises(ValidationError):
            StrategistClarificationChoice.model_validate(
                {
                    "kind": "field",
                    "label": "Tomorrow",
                    "value": "tomorrow",
                    "goalId": "goal:decision",
                }
            )

    def test_projection_contains_ids_and_application_uses_stored_delta_only(self) -> None:
        with Session(self.engine) as session:
            coordinator = GoalStateCoordinator(session)
            state = self.decision_state()
            coordinator.store.open("session:1", state, command_id="open:decision")
            session.commit()
            payload = decision_clarification_payload(state)
            self.assertIsNotNone(payload)
            choice = payload["field_choices"][0]  # type: ignore[index]
            self.assertEqual(choice["goal_id"], state.goal_id)
            self.assertEqual(choice["decision_id"], "decision:move")
            self.assertEqual(len(payload["field_choices"]), 1)  # type: ignore[arg-type]
            conflicts = payload["conflicts"]  # type: ignore[index]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["blocker_task_title"], "Wake-up time")
            self.assertEqual(conflicts[0]["requested_start_at"], "2026-08-13T06:30:00Z")

            applied = coordinator.apply_ui_decision(
                session_id="session:1",
                goal_id=state.goal_id,
                goal_revision=1,
                decision_id="decision:move",
                option_id="option:workout",
                source_event_id="event:1",
            )
            self.assertEqual(
                applied.semantic_slots[0].semantic_value.value,
                ["occurrence:workout:1"],
            )
            replay = coordinator.apply_ui_decision(
                session_id="session:1",
                goal_id=state.goal_id,
                goal_revision=1,
                decision_id="decision:move",
                option_id="option:workout",
                source_event_id="event:1",
            )
            self.assertEqual(replay, applied)
            with self.assertRaises(GoalStateDecisionConflict):
                coordinator.apply_ui_decision(
                    session_id="session:1",
                    goal_id=state.goal_id,
                    goal_revision=1,
                    decision_id="decision:move",
                    option_id="option:workout",
                    source_event_id="event:retry",
                )
            with self.assertRaises(GoalStateDecisionConflict):
                coordinator.apply_ui_decision(
                    session_id="session:1",
                    goal_id=state.goal_id,
                    goal_revision=2,
                    decision_id="decision:move",
                    option_id="option:tampered",
                    source_event_id="event:bad",
                )


if __name__ == "__main__":
    unittest.main()
