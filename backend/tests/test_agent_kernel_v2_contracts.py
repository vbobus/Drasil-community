from __future__ import annotations

import unittest

from pydantic import TypeAdapter, ValidationError

from app.services.agent_kernel.contracts import (
    ChangeSet,
    Constraint,
    ConstraintScope,
    DecisionDelta,
    DecisionRequest,
    DesiredOutcome,
    EntityRef,
    GoalState,
    GoalStatePatch,
    Operation,
    Provenance,
    ReviewChange,
    ReviewDocument,
    SemanticDiffBlock,
    TaskCreateOperation,
    ToolError,
    ToolErrorCategory,
    ToolResult,
    parse_operation,
    strict_provider_schema,
)


class AgentKernelV2ContractTests(unittest.TestCase):
    def test_canonical_models_reject_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            EntityRef(kind="task", entity_id="task:1", unexpected=True)

    def test_goal_state_preserves_provenance_and_trace_identity(self) -> None:
        provenance = Provenance(
            source_kind="user",
            source_ref="turn:1",
            confidence=1.0,
        )
        state = GoalState(
            goal_id="goal:1",
            original_request="Create and schedule the interview task.",
            desired_outcomes=[
                DesiredOutcome(
                    outcome_id="outcome:1",
                    description="The task exists and is scheduled.",
                    provenance=provenance,
                )
            ],
            correlation_id="corr:1",
            causation_id="turn:1",
        )

        self.assertEqual(state.contract_version, "2.0")
        self.assertEqual(state.revision, 1)
        self.assertEqual(
            GoalState.model_validate_json(state.model_dump_json()).desired_outcomes[0].provenance,
            provenance,
        )

    def test_operation_union_is_discriminated_and_closed(self) -> None:
        operation = parse_operation(
            {
                "kind": "task.create",
                "operation_id": "op:1",
                "title": "Prepare interview",
                "workspace_ref": {"kind": "workspace", "entity_id": "workspace:work"},
                "temp_ref": "temp:interview",
            }
        )
        self.assertIsInstance(operation, TaskCreateOperation)

        with self.assertRaises(ValidationError):
            parse_operation(
                {
                    "kind": "task.create",
                    "operation_id": "op:1",
                    "title": "Prepare interview",
                    "workspace_ref": {"kind": "workspace", "entity_id": "workspace:work"},
                    "unknown": "discard me",
                }
            )

    def test_all_seventeen_operation_members_round_trip_through_one_union(self) -> None:
        task = {"kind": "task", "entity_id": "task:1"}
        task_two = {"kind": "task", "entity_id": "task:2"}
        workspace = {"kind": "workspace", "entity_id": "workspace:1"}
        block = {"kind": "calendar_block", "entity_id": "calendar_block:1"}
        series = {"kind": "recurrence_series", "entity_id": "recurrence_series:1"}
        occurrence = {
            "kind": "recurrence_occurrence",
            "entity_id": "recurrence_occurrence:1",
        }
        preference = {"kind": "preference", "entity_id": "preference:1"}
        examples = [
            {"kind": "task.create", "operation_id": "op:01", "title": "A", "workspace_ref": workspace},
            {"kind": "task.update", "operation_id": "op:02", "task_ref": task, "updates": {"title": "B"}},
            {"kind": "task.move", "operation_id": "op:03", "task_refs": [task], "destination_parent_ref": task_two},
            {"kind": "task.archive", "operation_id": "op:04", "task_refs": [task]},
            {"kind": "task.delete", "operation_id": "op:05", "task_refs": [task]},
            {
                "kind": "calendar.create_block", "operation_id": "op:06", "task_ref": task,
                "start_at": "2026-07-20T09:00:00-07:00", "end_at": "2026-07-20T10:00:00-07:00",
            },
            {
                "kind": "calendar.move_block", "operation_id": "op:07", "block_ref": block,
                "start_at": "2026-07-20T10:00:00-07:00", "end_at": "2026-07-20T11:00:00-07:00",
            },
            {
                "kind": "calendar.resize_block", "operation_id": "op:08", "block_ref": block,
                "end_at": "2026-07-20T11:30:00-07:00",
            },
            {
                "kind": "calendar.split_block", "operation_id": "op:09", "block_ref": block,
                "segments": [
                    {"start_at": "2026-07-20T09:00:00-07:00", "end_at": "2026-07-20T09:30:00-07:00"},
                    {"start_at": "2026-07-20T09:30:00-07:00", "end_at": "2026-07-20T10:00:00-07:00"},
                ],
            },
            {"kind": "calendar.delete_block", "operation_id": "op:10", "block_refs": [block]},
            {
                "kind": "recurrence.create_series", "operation_id": "op:11", "task_ref": task,
                "rule": {"frequency": "weekly", "weekdays": ["monday"], "start_date": "2026-07-20"},
            },
            {
                "kind": "recurrence.update_series", "operation_id": "op:12", "series_ref": series,
                "updates": {"interval": 2},
            },
            {
                "kind": "recurrence.update_occurrence", "operation_id": "op:13",
                "occurrence_ref": occurrence, "updates": {"duration_minutes": 45},
            },
            {"kind": "recurrence.cancel_occurrence", "operation_id": "op:14", "occurrence_ref": occurrence},
            {
                "kind": "preference.upsert", "operation_id": "op:15",
                "preference_key": "focus_minutes", "value": {"kind": "scalar", "value": 50},
            },
            {
                "kind": "preference.supersede", "operation_id": "op:16", "preference_ref": preference,
                "value": {"kind": "string_set", "values": ["morning"]},
            },
            {"kind": "preference.retire", "operation_id": "op:17", "preference_ref": preference},
        ]
        parsed = [parse_operation(example) for example in examples]
        self.assertEqual(len(parsed), 17)
        self.assertEqual(len({item.kind for item in parsed}), 17)
        for item in parsed:
            self.assertEqual(parse_operation(item.model_dump(mode="json")), item)
        strict_provider_schema(TypeAdapter(Operation))

    def test_constraint_scope_supports_sets_without_single_task_assumption(self) -> None:
        constraint = Constraint(
            constraint_id="constraint:1",
            kind="minimum_gap",
            strength="hard",
            scope=ConstraintScope(
                kind="entity_set",
                entity_refs=[
                    EntityRef(kind="task", entity_id="task:1"),
                    EntityRef(kind="task", entity_id="task:2"),
                ],
            ),
            value={"minutes": 20},
            provenance=Provenance(source_kind="user", source_ref="turn:1"),
        )
        self.assertEqual(len(constraint.scope.entity_refs), 2)

        with self.assertRaises(ValidationError):
            ConstraintScope(kind="operation_set", operation_ids=[])

    def test_changeset_review_decision_and_tool_error_are_versioned(self) -> None:
        operation = TaskCreateOperation(
            operation_id="op:1",
            title="Prepare interview",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:interview",
        )
        change_set = ChangeSet(
            change_set_id="changeset:1",
            goal_id="goal:1",
            operations=[operation],
            idempotency_key="idem:1",
            correlation_id="corr:1",
            causation_id="turn:1",
        )
        decision = DecisionRequest(
            decision_id="decision:1",
            goal_id="goal:1",
            goal_revision=1,
            target_revision=2,
            question="Which commitment keeps the time?",
            reason="Both are anchored.",
            options=[
                DecisionDelta(
                    option_id="keep-existing",
                    label="Keep the existing commitment",
                    patches=[
                        GoalStatePatch(
                            path="conflict.entity_scope",
                            operation="set",
                            value=["task:1"],
                            provenance=Provenance(source_kind="ui", source_ref="event:1"),
                        )
                    ],
                )
            ],
        )
        review = ReviewDocument(
            review_id="review:1",
            session_id="session:1",
            goal_id="goal:1",
            goal_revision=1,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            simulation_id="simulation:1",
            simulation_input_fingerprint="a" * 64,
            simulation_output_fingerprint="b" * 64,
            correlation_id="corr:1",
            causation_id="turn:1",
            summary="Create the task.",
            blocks=[
                SemanticDiffBlock(
                    block_id="block:task",
                    title="Task",
                    changes=[
                        ReviewChange(
                            change_id="change:task",
                            summary="Create Prepare interview",
                            after="Prepare interview",
                        )
                    ],
                )
            ],
            decisions=[decision],
        )
        error = ToolError(
            code="CONFLICT_REQUIRES_DECISION",
            category=ToolErrorCategory.USER_DECISION,
            message_for_model="Two anchored commitments overlap.",
            entity_refs=[EntityRef(kind="task", entity_id="task:1")],
            retryable=False,
        )
        result = ToolResult.failure(error, correlation_id="corr:1", causation_id="tool:1")

        self.assertEqual(review.change_set_revision, 1)
        self.assertEqual(result.error.code, "CONFLICT_REQUIRES_DECISION")
        self.assertIsNone(result.data)

    def test_strict_provider_schema_closes_every_object(self) -> None:
        schema = strict_provider_schema(TypeAdapter(EntityRef))

        def assert_closed(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" or "properties" in value:
                    self.assertIs(value.get("additionalProperties"), False)
                    properties = value.get("properties", {})
                    self.assertEqual(set(value.get("required", [])), set(properties))
                for nested in value.values():
                    assert_closed(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_closed(nested)

        assert_closed(schema)

        goal_schema = strict_provider_schema(TypeAdapter(GoalState))
        self.assertIs(goal_schema["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
