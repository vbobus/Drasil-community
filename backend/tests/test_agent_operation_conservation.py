from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.agent_kernel.contracts import (
    ChangeSet,
    DecisionDelta,
    DecisionRequest,
    EntityRef,
    GoalState,
    GoalStatePatch,
    Provenance,
    RequestedOperationClause,
    SemanticSlot,
    SemanticValue,
    TaskCreateOperation,
    TaskDeleteOperation,
    TaskUpdateOperation,
    TaskUpdatePatch,
)
from app.services.agent_kernel.operation_conservation import (
    OperationConservationError,
    evaluate_operation_conservation,
    verify_operation_round_trip,
)


PROVENANCE = Provenance(source_kind="user", source_ref="turn:1")


def goal(*clauses: RequestedOperationClause, decisions=()) -> GoalState:
    return GoalState(
        goal_id="goal:1",
        original_request="Create and rename the task.",
        semantic_slots=[
            SemanticSlot(
                path="operations.requested",
                semantic_value=SemanticValue(
                    state="present",
                    value=[item.model_dump(mode="json", by_alias=True) for item in clauses],
                    provenance=PROVENANCE,
                ),
            )
        ],
        unresolved_decisions=list(decisions),
        correlation_id="corr:1",
        causation_id="turn:1",
    )


def draft(*operations) -> ChangeSet:
    return ChangeSet(
        change_set_id="changeset:1",
        goal_id="goal:1",
        operations=list(operations),
        idempotency_key="idem:1",
        correlation_id="corr:1",
        causation_id="turn:1",
    )


class AgentOperationConservationTests(unittest.TestCase):
    def test_requested_operation_slot_migrates_legacy_strings_to_typed_clauses(self) -> None:
        patch = GoalStatePatch(
            path="operations.requested",
            operation="set",
            value=["create", "schedule"],
            provenance=PROVENANCE,
        )
        self.assertEqual(patch.value[0]["operationKind"], "task.create")
        self.assertEqual(patch.value[1]["operationKind"], "calendar.create_block")
        unsupported = GoalStatePatch(
            path="operations.requested",
            operation="set",
            value=["teleport"],
            provenance=PROVENANCE,
        )
        self.assertEqual(unsupported.value[0]["disposition"], "unsupported")
        self.assertEqual(unsupported.value[0]["legacyOperationLabel"], "teleport")

        with self.assertRaises(ValidationError):
            GoalStatePatch(
                path="operations.requested",
                operation="set",
                value=[{"clauseId": "clause:bad"}],
                provenance=PROVENANCE,
            )

    def test_conservation_requires_every_active_clause(self) -> None:
        create = TaskCreateOperation(
            operation_id="op:create",
            title="Task",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:task",
            fulfills_clause_ids=["clause:create"],
        )
        rename = TaskUpdateOperation(
            operation_id="op:rename",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            updates=TaskUpdatePatch(title="Renamed"),
            fulfills_clause_ids=["clause:rename"],
        )
        create_clause = RequestedOperationClause(
            clause_id="clause:create",
            operation_kind="task.create",
            cardinality="exactly_one",
            expected_operations=[create],
            provenance=PROVENANCE,
        )
        rename_clause = RequestedOperationClause(
            clause_id="clause:rename",
            operation_kind="task.update",
            cardinality="exactly_one",
            target_refs=[EntityRef(kind="task", entity_id="task:1")],
            expected_operations=[rename],
            provenance=PROVENANCE,
        )
        report = evaluate_operation_conservation(goal(create_clause, rename_clause), draft(create))
        self.assertFalse(report.complete)
        self.assertEqual(report.failed_clause_ids, ["clause:rename"])

    def test_fanout_and_semantic_fingerprints_are_conserved(self) -> None:
        first = TaskUpdateOperation(
            operation_id="op:title",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            updates=TaskUpdatePatch(title="New title"),
            fulfills_clause_ids=["clause:update"],
        )
        second = TaskUpdateOperation(
            operation_id="op:duration",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            updates=TaskUpdatePatch(estimate_minutes=60),
            fulfills_clause_ids=["clause:update"],
        )
        clause = RequestedOperationClause(
            clause_id="clause:update",
            operation_kind="task.update",
            cardinality="one_or_more",
            target_refs=[EntityRef(kind="task", entity_id="task:1")],
            expected_operations=[first, second],
            provenance=PROVENANCE,
        )
        change_set = draft(first, second)
        report = evaluate_operation_conservation(goal(clause), change_set)
        self.assertTrue(report.complete)
        self.assertEqual(report.covered_clause_ids, ["clause:update"])
        self.assertEqual(len(report.operation_fingerprints), 2)

        rewritten = change_set.model_copy(
            update={
                "operations": [
                    first,
                    second.model_copy(update={"updates": TaskUpdatePatch(estimate_minutes=30)}),
                ]
            }
        )
        with self.assertRaises(OperationConservationError):
            verify_operation_round_trip(change_set, rewritten)

    def test_unresolved_and_unsupported_dispositions_remain_explicit(self) -> None:
        unresolved = RequestedOperationClause(
            clause_id="clause:unresolved",
            operation_kind="calendar.create_block",
            cardinality="exactly_one",
            disposition="unresolved",
            decision_id="decision:schedule-time",
            provenance=PROVENANCE,
        )
        unsupported = RequestedOperationClause(
            clause_id="clause:unsupported",
            operation_kind="recurrence.create_series",
            cardinality="exactly_one",
            disposition="unsupported",
            unsupported_reason="missing_capability",
            provenance=PROVENANCE,
        )
        create = TaskCreateOperation(
            operation_id="op:create",
            title="Task",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:task",
            fulfills_clause_ids=["clause:create"],
        )
        create_clause = RequestedOperationClause(
            clause_id="clause:create",
            operation_kind="task.create",
            cardinality="exactly_one",
            expected_operations=[create],
            provenance=PROVENANCE,
        )
        decision = DecisionRequest(
            decision_id="decision:schedule-time",
            goal_id="goal:1",
            goal_revision=1,
            target_revision=2,
            question="When should it be scheduled?",
            reason="Exact time is unresolved.",
            options=[
                DecisionDelta(
                    option_id="option:morning",
                    label="Morning",
                    patches=[
                        GoalStatePatch(
                            path="temporal.requested_time",
                            operation="set",
                            value="09:00",
                            provenance=PROVENANCE,
                        )
                    ],
                )
            ],
        )
        state = goal(create_clause, unresolved, unsupported, decisions=[decision])
        report = evaluate_operation_conservation(state, draft(create))
        self.assertFalse(report.complete)
        self.assertEqual(report.unresolved_clause_ids, ["clause:unresolved"])
        self.assertEqual(report.unsupported_clause_ids, ["clause:unsupported"])

    def test_exact_intent_rejects_extra_targets_and_field_rewrites(self) -> None:
        expected = TaskUpdateOperation(
            operation_id="op:update",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            updates=TaskUpdatePatch(title="Exact title"),
            fulfills_clause_ids=["clause:update"],
        )
        clause = RequestedOperationClause(
            clause_id="clause:update",
            operation_kind="task.update",
            cardinality="exactly_one",
            target_refs=[EntityRef(kind="task", entity_id="task:1")],
            expected_operations=[expected],
            provenance=PROVENANCE,
        )
        rewritten = expected.model_copy(
            update={"updates": TaskUpdatePatch(title="Different title")}
        )
        self.assertFalse(evaluate_operation_conservation(goal(clause), draft(rewritten)).complete)
        extra = TaskDeleteOperation(
            operation_id="op:extra",
            task_refs=[EntityRef(kind="task", entity_id="task:2")],
            fulfills_clause_ids=["clause:update"],
        )
        with self.assertRaises(ValidationError):
            RequestedOperationClause(
                clause_id="clause:update",
                operation_kind="task.update",
                supporting_operation_kinds=["task.delete"],
                cardinality="one_or_more",
                expected_operations=[extra],
                provenance=PROVENANCE,
            )

    def test_satisfied_clause_cannot_hide_a_submitted_operation(self) -> None:
        satisfied = RequestedOperationClause(
            clause_id="clause:satisfied",
            operation_kind="task.delete",
            cardinality="exactly_one",
            disposition="satisfied",
            provenance=PROVENANCE,
        )
        operation = TaskDeleteOperation(
            operation_id="op:delete",
            task_refs=[EntityRef(kind="task", entity_id="task:1")],
            fulfills_clause_ids=["clause:satisfied"],
        )
        self.assertFalse(evaluate_operation_conservation(goal(satisfied), draft(operation)).complete)


if __name__ == "__main__":
    unittest.main()
