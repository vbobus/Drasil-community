from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.agent_kernel.contracts import (
    CorrectionDelta,
    DecisionDelta,
    DecisionRequest,
    GoalState,
    GoalStatePatch,
    GoalStateUpdateCommand,
    Provenance,
    ResolvedDecision,
    SemanticSlot,
    SemanticValue,
)


class AgentGoalStateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user_provenance = Provenance(source_kind="user", source_ref="turn:1")

    def test_correction_delta_requires_closed_typed_patches(self) -> None:
        delta = CorrectionDelta(
            delta_id="delta:1",
            source_turn_id="turn:1",
            patches=[
                GoalStatePatch(
                    path="temporal.duration_minutes",
                    operation="set",
                    value=45,
                    provenance=self.user_provenance,
                )
            ],
            supersedes_paths=["temporal.duration_minutes"],
        )
        self.assertEqual(delta.patches[0].value, 45)

        with self.assertRaises(ValidationError):
            CorrectionDelta(
                delta_id="delta:bad",
                source_turn_id="turn:1",
                changes={"goalId": "goal:other"},
            )

        with self.assertRaises(ValidationError):
            GoalStatePatch(
                path="goal_id",
                operation="set",
                value="goal:other",
                provenance=self.user_provenance,
            )

    def test_patch_operation_encodes_null_clear_and_unknown_without_truthiness(self) -> None:
        for operation in ("set_null", "clear", "mark_unknown", "mark_unresolved"):
            patch = GoalStatePatch(
                path="temporal.requested_date",
                operation=operation,
                provenance=self.user_provenance,
            )
            self.assertIsNone(patch.value)

        with self.assertRaises(ValidationError):
            GoalStatePatch(
                path="temporal.duration_minutes",
                operation="set",
                value=None,
                provenance=self.user_provenance,
            )
        with self.assertRaises(ValidationError):
            GoalStatePatch(
                path="temporal.duration_minutes",
                operation="set",
                value="45",
                provenance=self.user_provenance,
            )
        with self.assertRaises(ValidationError):
            GoalStatePatch(
                path="scope.task_refs",
                operation="set",
                value="task:1",
                provenance=self.user_provenance,
            )

        with self.assertRaises(ValidationError):
            GoalStatePatch(
                path="temporal.duration_minutes",
                operation="clear",
                value=0,
                provenance=self.user_provenance,
            )

    def test_semantic_value_rejects_illegal_state_value_provenance_combinations(self) -> None:
        inferred = SemanticValue(
            state="inferred",
            value="next week",
            provenance=Provenance(
                source_kind="inference",
                source_ref="turn:1",
                confidence=0.8,
            ),
        )
        self.assertEqual(inferred.value, "next week")

        with self.assertRaises(ValidationError):
            SemanticValue(state="present", value=None, provenance=self.user_provenance)
        with self.assertRaises(ValidationError):
            SemanticValue(state="unknown", value="secret")
        with self.assertRaises(ValidationError):
            SemanticValue(
                state="inferred",
                value="next week",
                provenance=Provenance(source_kind="user", source_ref="turn:1"),
            )

    def test_decision_contract_carries_goal_revision_and_stored_delta(self) -> None:
        option = DecisionDelta(
            option_id="option:workout",
            label="Move the workout",
            patches=[
                GoalStatePatch(
                    path="conflict.occurrence_scope",
                    operation="set",
                    value=["occurrence:workout:1"],
                    provenance=Provenance(source_kind="ui", source_ref="event:1"),
                )
            ],
        )
        decision = DecisionRequest(
            decision_id="decision:1",
            goal_id="goal:1",
            goal_revision=2,
            target_revision=3,
            question="Which commitment should move?",
            reason="The commitments overlap.",
            options=[option],
        )
        self.assertEqual(decision.options[0].patches[0].path, "conflict.occurrence_scope")

        with self.assertRaises(ValidationError):
            DecisionRequest(
                decision_id="decision:1",
                goal_id="goal:1",
                goal_revision=2,
                target_revision=3,
                question="Which commitment should move?",
                reason="The commitments overlap.",
                options=[option, option],
            )

    def test_goal_state_owns_semantic_slots_and_resolved_decision_history(self) -> None:
        state = GoalState(
            goal_id="goal:1",
            original_request="Schedule both interviews next week.",
            semantic_slots=[
                SemanticSlot(
                    path="scope.task_refs",
                    semantic_value=SemanticValue(
                        state="present",
                        value=["task:1", "task:2"],
                        provenance=self.user_provenance,
                    ),
                )
            ],
            resolved_decisions=[
                ResolvedDecision(
                    decision_id="decision:1",
                    option_id="option:1",
                    source_event_id="event:1",
                    applied_revision=2,
                )
            ],
            correlation_id="corr:1",
            causation_id="turn:1",
        )
        restored = GoalState.model_validate_json(state.model_dump_json())
        self.assertEqual(restored.semantic_slots[0].semantic_value.value, ["task:1", "task:2"])
        self.assertEqual(restored.resolved_decisions[0].applied_revision, 2)

    def test_model_update_command_requires_an_explicit_disposition(self) -> None:
        command = GoalStateUpdateCommand(
            command_id="command:1",
            disposition="correct",
            goal_id="goal:1",
            expected_revision=1,
            patches=[
                GoalStatePatch(
                    path="temporal.requested_date",
                    operation="set",
                    value="next-week",
                    provenance=self.user_provenance,
                )
            ],
        )
        self.assertEqual(command.disposition, "correct")
        with self.assertRaises(ValidationError):
            GoalStateUpdateCommand(command_id="command:bad", disposition="correct")
        with self.assertRaises(ValidationError):
            GoalStateUpdateCommand(
                command_id="command:bad",
                disposition="new_goal",
                original_request=None,
            )
        with self.assertRaises(ValidationError):
            GoalStateUpdateCommand(
                command_id="command:bad-new-goal",
                disposition="new_goal",
                goal_id="goal:old",
                expected_revision=2,
                original_request="Start something else.",
            )

    def test_change_set_identity_and_projection_invariants_are_canonical(self) -> None:
        with self.assertRaises(ValidationError):
            GoalState(
                goal_id="goal:invalid",
                original_request="Review it.",
                current_change_set_revision=2,
                correlation_id="corr:1",
                causation_id="turn:1",
            )

        state = GoalState(
            goal_id="goal:projection",
            original_request="Use both tasks.",
            semantic_slots=[
                SemanticSlot(
                    path="scope.resolved_refs",
                    semantic_value=SemanticValue(
                        state="present",
                        value=[
                            {"kind": "task", "entityId": "task:1"},
                            {"kind": "task", "entityId": "task:2"},
                        ],
                        provenance=self.user_provenance,
                    ),
                )
            ],
            correlation_id="corr:1",
            causation_id="turn:1",
        )
        self.assertEqual([item.entity_id for item in state.resolved_refs], ["task:1", "task:2"])


if __name__ == "__main__":
    unittest.main()
