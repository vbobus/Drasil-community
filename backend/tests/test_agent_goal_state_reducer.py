from __future__ import annotations

import unittest

from app.services.agent_kernel.contracts import (
    CorrectionDelta,
    DecisionDelta,
    DecisionRequest,
    GoalState,
    GoalStatePatch,
    Provenance,
    SemanticSlot,
    SemanticValue,
)
from app.services.agent_kernel.goal_state_reducer import (
    GoalStateDecisionConflict,
    GoalStateTransitionError,
    approve_goal,
    apply_correction,
    apply_decision,
    register_decisions,
    transition_goal,
)


def provenance(kind: str, source: str, confidence: float | None = None) -> Provenance:
    return Provenance(source_kind=kind, source_ref=source, confidence=confidence)


def state_with_slots(*slots: tuple[str, object]) -> GoalState:
    return GoalState(
        goal_id="goal:1",
        original_request="Schedule both interviews for 45 minutes next week.",
        semantic_slots=[
            SemanticSlot(
                path=path,
                semantic_value=SemanticValue(
                    state="present",
                    value=value,
                    provenance=provenance("user", "turn:1"),
                ),
            )
            for path, value in slots
        ],
        correlation_id="corr:1",
        causation_id="turn:1",
    )


class GoalStateReducerTests(unittest.TestCase):
    def test_actually_next_week_only_supersedes_date(self) -> None:
        state = state_with_slots(
            ("scope.task_refs", ["task:interview:1", "task:interview:2"]),
            ("temporal.duration_minutes", 45),
            ("temporal.requested_date", "this-week"),
        )
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:next-week",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="temporal.requested_date",
                        operation="set",
                        value="next-week",
                        provenance=provenance("user", "turn:2"),
                    )
                ],
                supersedes_paths=["temporal.requested_date"],
            ),
        )
        values = {slot.path: slot.semantic_value.value for slot in corrected.semantic_slots}
        self.assertEqual(values["scope.task_refs"], ["task:interview:1", "task:interview:2"])
        self.assertEqual(values["temporal.duration_minutes"], 45)
        self.assertEqual(values["temporal.requested_date"], "next-week")
        self.assertEqual(corrected.revision, 2)

    def test_use_that_task_itself_retains_schedule_intent(self) -> None:
        state = state_with_slots(
            ("operations.requested", ["schedule"]),
            (
                "scope.selectors",
                [{
                    "selectorId": "selector:working-set",
                    "kind": "working_set",
                    "value": "that task",
                    "cardinality": "exactly_one",
                    "provenance": {"sourceKind": "user", "sourceRef": "turn:1"},
                    "resolvedRefs": [],
                }],
            ),
            ("temporal.duration_minutes", 45),
        )
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:task-itself",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="scope.task_refs",
                        operation="set",
                        value=["task:interview"],
                        provenance=provenance("user", "turn:2"),
                    )
                ],
            ),
        )
        values = {slot.path: slot.semantic_value.value for slot in corrected.semantic_slots}
        self.assertEqual(
            [item["operationKind"] for item in values["operations.requested"]],
            ["calendar.create_block"],
        )
        self.assertEqual(values["scope.task_refs"], ["task:interview"])
        self.assertEqual(values["temporal.duration_minutes"], 45)

    def test_duration_answer_is_recorded_once_and_reused_after_unrelated_correction(self) -> None:
        state = state_with_slots(("operations.requested", ["create", "schedule"]))
        duration_delta = CorrectionDelta(
            delta_id="delta:duration-answer",
            source_turn_id="turn:2",
            patches=[
                GoalStatePatch(
                    path="temporal.duration_minutes",
                    operation="set",
                    value=60,
                    provenance=provenance("user", "turn:2"),
                )
            ],
        )
        with_duration = apply_correction(state, duration_delta)
        corrected = apply_correction(
            with_duration,
            CorrectionDelta(
                delta_id="delta:week",
                source_turn_id="turn:3",
                patches=[
                    GoalStatePatch(
                        path="temporal.requested_date",
                        operation="set",
                        value="next-week",
                        provenance=provenance("user", "turn:3"),
                    )
                ],
            ),
        )
        values = {slot.path: slot.semantic_value.value for slot in corrected.semantic_slots}
        self.assertEqual(values["temporal.duration_minutes"], 60)
        self.assertEqual(
            [delta.delta_id for delta in corrected.correction_deltas].count("delta:duration-answer"),
            1,
        )

    def test_all_protected_collection_fields_survive_one_targeted_change(self) -> None:
        state = state_with_slots(
            ("scope.task_refs", ["task:1", "task:2"]),
            (
                "scope.resolved_refs",
                [
                    {"kind": "task", "entityId": "task:1"},
                    {"kind": "task", "entityId": "task:2"},
                ],
            ),
            ("scope.workspace", "workspace:work"),
            ("scope.parent", "task:parent"),
            ("operations.requested", ["create", "schedule"]),
            ("recurrence.rule", {"frequency": "weekly", "days": ["monday", "friday"]}),
            ("recurrence.occurrence_scope", ["occurrence:1", "occurrence:2"]),
            ("temporal.duration_minutes", 45),
            ("temporal.requested_time", "09:00"),
            ("temporal.ordering", ["task:1", "task:2"]),
            ("temporal.gaps", 15),
        )
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:date-only",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="temporal.requested_date",
                        operation="set",
                        value="next-week",
                        provenance=provenance("user", "turn:2"),
                    )
                ],
            ),
        )
        before = {slot.path: slot.semantic_value for slot in state.semantic_slots}
        after = {slot.path: slot.semantic_value for slot in corrected.semantic_slots}
        for path, value in before.items():
            self.assertEqual(after[path], value, path)

    def test_explicit_value_survives_later_inference_and_zero_is_not_missing(self) -> None:
        state = state_with_slots(("temporal.gaps", 0))
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:inferred-gap",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="temporal.gaps",
                        operation="set",
                        value=20,
                        provenance=provenance("inference", "turn:2", 0.7),
                    )
                ],
            ),
        )
        self.assertEqual(corrected.semantic_slots[0].semantic_value.value, 0)

    def test_explicit_clear_is_preserved_as_a_semantic_state(self) -> None:
        state = state_with_slots(("temporal.requested_time", "09:00"))
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:clear-time",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="temporal.requested_time",
                        operation="clear",
                        provenance=provenance("user", "turn:2"),
                    )
                ],
            ),
        )
        self.assertEqual(corrected.semantic_slots[0].semantic_value.state, "cleared")
        self.assertIsNone(corrected.semantic_slots[0].semantic_value.value)

    def test_replaying_the_same_correction_is_idempotent(self) -> None:
        state = state_with_slots(("temporal.duration_minutes", 30))
        delta = CorrectionDelta(
            delta_id="delta:duration",
            source_turn_id="turn:2",
            patches=[
                GoalStatePatch(
                    path="temporal.duration_minutes",
                    operation="set",
                    value=45,
                    provenance=provenance("user", "turn:2"),
                )
            ],
        )
        once = apply_correction(state, delta)
        twice = apply_correction(once, delta)
        self.assertEqual(twice, once)

    def test_revision_invalidates_older_approval(self) -> None:
        state = state_with_slots(("temporal.duration_minutes", 30)).model_copy(
            update={
                "status": "approved",
                "current_change_set_id": "changeset:1",
                "current_change_set_revision": 2,
                "approved_change_set_revision": 2,
            }
        )
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:duration",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="temporal.duration_minutes",
                        operation="set",
                        value=60,
                        provenance=provenance("user", "turn:2"),
                    )
                ],
            ),
        )
        self.assertIsNone(corrected.approved_change_set_revision)
        self.assertEqual(corrected.status, "planning")

    def test_correction_cannot_replace_a_committing_approval(self) -> None:
        state = state_with_slots(("temporal.duration_minutes", 30)).model_copy(
            update={
                "status": "committing",
                "current_change_set_id": "changeset:1",
                "current_change_set_revision": 2,
                "approved_change_set_revision": 2,
            }
        )
        with self.assertRaises(GoalStateTransitionError):
            apply_correction(
                state,
                CorrectionDelta(
                    delta_id="delta:late",
                    source_turn_id="turn:late",
                    patches=[
                        GoalStatePatch(
                            path="temporal.duration_minutes",
                            operation="set",
                            value=60,
                            provenance=provenance("user", "turn:late"),
                        )
                    ],
                ),
            )

    def test_typed_decision_applies_stored_option_and_replay_is_safe(self) -> None:
        state = state_with_slots(
            ("recurrence.rule", {"cadence": "weekly"}),
            ("conflict.entity_scope", ["task:workout", "task:call"]),
        ).model_copy(update={"status": "needs_user_decision"})
        decision = DecisionRequest(
            decision_id="decision:move",
            goal_id=state.goal_id,
            goal_revision=state.revision,
            target_revision=state.revision + 1,
            question="Which commitment should move?",
            reason="They overlap.",
            options=[
                DecisionDelta(
                    option_id="option:workout",
                    label="Move the workout, not the call",
                    patches=[
                        GoalStatePatch(
                            path="conflict.occurrence_scope",
                            operation="set",
                            value=["occurrence:workout:1"],
                            provenance=provenance("ui", "event:move"),
                        )
                    ],
                )
            ],
        )
        state = state.model_copy(update={"unresolved_decisions": [decision]})
        applied = apply_decision(
            state,
            decision_id=decision.decision_id,
            option_id="option:workout",
            source_event_id="event:move",
            expected_revision=1,
        )
        values = {slot.path: slot.semantic_value.value for slot in applied.semantic_slots}
        self.assertEqual(values["recurrence.rule"], {"cadence": "weekly"})
        self.assertEqual(values["conflict.occurrence_scope"], ["occurrence:workout:1"])
        self.assertFalse(applied.unresolved_decisions)
        self.assertEqual(applied.resolved_decisions[0].option_id, "option:workout")
        self.assertEqual(
            apply_decision(
                applied,
                decision_id=decision.decision_id,
                option_id="option:workout",
                source_event_id="event:move",
                expected_revision=1,
            ),
            applied,
        )
        with self.assertRaises(GoalStateDecisionConflict):
            apply_decision(
                applied,
                decision_id=decision.decision_id,
                option_id="option:call",
                source_event_id="event:other",
                expected_revision=2,
            )
        with self.assertRaises(GoalStateDecisionConflict):
            apply_decision(
                applied,
                decision_id=decision.decision_id,
                option_id="option:workout",
                source_event_id="event:different",
                expected_revision=1,
            )

    def test_lifecycle_transitions_are_explicit(self) -> None:
        state = state_with_slots(("operations.requested", ["schedule"]))
        planning = transition_goal(state, "planning", command_id="transition:1")
        self.assertEqual(planning.status, "planning")
        applied = transition_goal(planning, "applied", command_id="transition:complete")
        self.assertEqual(applied.status, "applied")

    def test_register_decision_is_a_production_reducer_path(self) -> None:
        state = state_with_slots(("operations.requested", ["schedule"]))
        decision = DecisionRequest(
            decision_id="decision:date",
            goal_id=state.goal_id,
            goal_revision=2,
            target_revision=3,
            question="Which date?",
            reason="Two dates remain plausible.",
            options=[
                DecisionDelta(
                    option_id="option:tomorrow",
                    label="Tomorrow",
                    patches=[
                        GoalStatePatch(
                            path="temporal.requested_date",
                            operation="set",
                            value="tomorrow",
                            provenance=provenance("inference", "turn:1", 0.8),
                        )
                    ],
                )
            ],
        )
        updated = register_decisions(state, [decision], command_id="decision-command")
        self.assertEqual(updated.status, "needs_user_decision")
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.unresolved_decisions[0].decision_id, "decision:date")

    def test_correction_preserves_unaffected_open_decision_and_records_only_accepted_paths(self) -> None:
        state = state_with_slots(("temporal.duration_minutes", 45))
        decision = DecisionRequest(
            decision_id="decision:date",
            goal_id=state.goal_id,
            goal_revision=1,
            target_revision=2,
            question="Which date?",
            reason="Date is unresolved.",
            options=[
                DecisionDelta(
                    option_id="option:tomorrow",
                    label="Tomorrow",
                    patches=[
                        GoalStatePatch(
                            path="temporal.requested_date",
                            operation="set",
                            value="tomorrow",
                            provenance=provenance("ui", "event:date"),
                        )
                    ],
                )
            ],
        )
        state = state.model_copy(update={"unresolved_decisions": [decision]})
        corrected = apply_correction(
            state,
            CorrectionDelta(
                delta_id="delta:ignored",
                source_turn_id="turn:2",
                patches=[
                    GoalStatePatch(
                        path="temporal.duration_minutes",
                        operation="set",
                        value=60,
                        provenance=provenance("inference", "turn:2", 0.7),
                    )
                ],
                supersedes_paths=["temporal.duration_minutes"],
            ),
        )
        self.assertEqual(corrected.correction_deltas[-1].supersedes_paths, [])
        self.assertEqual(corrected.unresolved_decisions[0].goal_revision, 2)
        self.assertEqual(corrected.status, "needs_user_decision")

        accepted = apply_correction(
            corrected,
            CorrectionDelta(
                delta_id="delta:accepted",
                source_turn_id="turn:3",
                patches=[
                    GoalStatePatch(
                        path="temporal.duration_minutes",
                        operation="set",
                        value=60,
                        provenance=provenance("user", "turn:3"),
                    )
                ],
            ),
        )
        self.assertEqual(accepted.unresolved_decisions[0].goal_revision, 3)
        self.assertEqual(
            accepted.correction_deltas[-1].supersedes_paths,
            ["temporal.duration_minutes"],
        )

        overlapping = apply_correction(
            accepted,
            CorrectionDelta(
                delta_id="delta:date",
                source_turn_id="turn:4",
                patches=[
                    GoalStatePatch(
                        path="temporal.requested_date",
                        operation="set",
                        value="tomorrow",
                        provenance=provenance("user", "turn:4"),
                    )
                ],
            ),
        )
        self.assertFalse(overlapping.unresolved_decisions)
        self.assertEqual(overlapping.status, "planning")

    def test_register_and_partial_resolution_preserve_other_open_decisions(self) -> None:
        state = state_with_slots(("operations.requested", ["schedule"]))

        def decision(decision_id: str, option_id: str, path: str, revision: int):
            return DecisionRequest(
                decision_id=decision_id,
                goal_id=state.goal_id,
                goal_revision=revision,
                target_revision=revision + 1,
                question=f"Choose {path}?",
                reason="A durable choice is required.",
                options=[
                    DecisionDelta(
                        option_id=option_id,
                        label=option_id,
                        patches=[
                            GoalStatePatch(
                                path=path,
                                operation="set",
                                value=(
                                    "tomorrow"
                                    if path == "temporal.requested_date"
                                    else "17:00"
                                ),
                                provenance=provenance("ui", f"event:{option_id}"),
                            )
                        ],
                    )
                ],
            )

        first = register_decisions(
            state,
            [decision("decision:first", "option:first", "temporal.requested_date", 2)],
            command_id="decision:first",
        )
        both = register_decisions(
            first,
            [decision("decision:second", "option:second", "temporal.requested_time", 3)],
            command_id="decision:second",
        )
        self.assertEqual(
            [item.decision_id for item in both.unresolved_decisions],
            ["decision:first", "decision:second"],
        )
        self.assertTrue(all(item.goal_revision == 3 for item in both.unresolved_decisions))

        resolved = apply_decision(
            both,
            decision_id="decision:first",
            option_id="option:first",
            source_event_id="event:resolve:first",
            expected_revision=3,
        )
        self.assertEqual(resolved.status, "needs_user_decision")
        self.assertEqual(
            [item.decision_id for item in resolved.unresolved_decisions],
            ["decision:second"],
        )
        self.assertEqual(resolved.unresolved_decisions[0].goal_revision, 4)

        advanced = apply_correction(
            resolved,
            CorrectionDelta(
                delta_id="delta:unrelated",
                source_turn_id="turn:later",
                patches=[
                    GoalStatePatch(
                        path="temporal.duration_minutes",
                        operation="set",
                        value=30,
                        provenance=provenance("user", "turn:later"),
                    )
                ],
            ),
        )
        replay = apply_decision(
            advanced,
            decision_id="decision:first",
            option_id="option:first",
            source_event_id="event:resolve:first",
            expected_revision=3,
        )
        self.assertIs(replay, advanced)

    def test_every_declared_lifecycle_edge_is_executable_and_terminals_are_closed(self) -> None:
        from app.services.agent_kernel.goal_state_reducer import (
            ALLOWED_GOAL_TRANSITIONS,
            TERMINAL_GOAL_STATUSES,
        )

        for source, targets in ALLOWED_GOAL_TRANSITIONS.items():
            for target in targets:
                with self.subTest(source=source, target=target):
                    base = state_with_slots(("operations.requested", ["schedule"])).model_copy(
                        update={"status": source}
                    )
                    transitioned = transition_goal(base, target, command_id=f"{source}:{target}")
                    self.assertEqual(transitioned.status, target)
                    self.assertEqual(transitioned.revision, base.revision + 1)
        for status in TERMINAL_GOAL_STATUSES:
            with self.subTest(terminal=status):
                self.assertEqual(ALLOWED_GOAL_TRANSITIONS[status], set())

    def test_exact_approval_revision_survives_committing_and_applied(self) -> None:
        ready = GoalState(
            revision=4,
            goalId="goal:approval",
            status="ready_for_review",
            originalRequest="Apply the reviewed plan.",
            currentChangeSetId="changeset:approval",
            currentChangeSetRevision=2,
            correlationId="corr:approval",
            causationId="turn:approval",
        )
        approved = approve_goal(ready, change_set_revision=2, command_id="approve:1")
        committing = transition_goal(approved, "committing", command_id="commit:1")
        applied = transition_goal(committing, "applied", command_id="applied:1")
        self.assertEqual(applied.approved_change_set_revision, 2)
        self.assertEqual(applied.revision, 7)


if __name__ == "__main__":
    unittest.main()
