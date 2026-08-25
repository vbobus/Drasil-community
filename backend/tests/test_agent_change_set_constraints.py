from __future__ import annotations

import unittest

from pydantic import TypeAdapter, ValidationError

from app.services.agent_kernel.contracts import (
    CardinalityConstraintValue,
    ChangeSet,
    Constraint,
    ConstraintScope,
    EntityRef,
    ExactTimeConstraintValue,
    MinimumGapConstraintValue,
    Provenance,
    TaskCreateOperation,
    TemporalWindowConstraintValue,
    strict_provider_schema,
)


PROVENANCE = Provenance(source_kind="user", source_ref="turn:1")


def constraint(**overrides):
    payload = {
        "constraint_id": "constraint:1",
        "kind": "minimum_gap",
        "strength": "hard",
        "scope": ConstraintScope(
            kind="entity_set",
            entity_refs=[
                EntityRef(kind="task", entity_id="task:1"),
                EntityRef(kind="task", entity_id="task:2"),
            ],
        ),
        "value": MinimumGapConstraintValue(minutes=20),
        "provenance": PROVENANCE,
    }
    payload.update(overrides)
    return Constraint(**payload)


class AgentChangeSetConstraintTests(unittest.TestCase):
    def test_constraint_values_are_closed_and_kind_matched(self) -> None:
        self.assertEqual(constraint().value.minutes, 20)
        with self.assertRaises(ValidationError):
            constraint(value=TemporalWindowConstraintValue(start_at="2026-07-20T09:00:00-07:00", end_at="2026-07-20T10:00:00-07:00"))
        with self.assertRaises(ValidationError):
            MinimumGapConstraintValue.model_validate({"minutes": 20, "unknown": True})

    def test_scope_members_are_unique_and_kind_safe(self) -> None:
        task = EntityRef(kind="task", entity_id="task:1")
        with self.assertRaises(ValidationError):
            ConstraintScope(kind="entity_set", entity_refs=[task, task])
        with self.assertRaises(ValidationError):
            ConstraintScope(kind="series", entity_refs=[task])
        with self.assertRaises(ValidationError):
            ConstraintScope(
                kind="occurrence_set",
                entity_refs=[EntityRef(kind="recurrence_series", entity_id="series:1")],
            )

    def test_operation_scope_must_reference_this_changeset(self) -> None:
        operation = TaskCreateOperation(
            operation_id="op:create",
            title="Task",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:task",
        )
        invalid_constraint = constraint(
            kind="cardinality",
            scope=ConstraintScope(kind="operation_set", operation_ids=["op:missing"]),
            value=CardinalityConstraintValue(minimum=1, maximum=1),
        )
        with self.assertRaises(ValidationError):
            ChangeSet(
                change_set_id="changeset:1",
                goal_id="goal:1",
                operations=[operation],
                constraints=[invalid_constraint],
                idempotency_key="idem:1",
                correlation_id="corr:1",
                causation_id="turn:1",
            )

    def test_changeset_rejects_duplicate_constraint_identities(self) -> None:
        operation = TaskCreateOperation(
            operation_id="op:create",
            title="Task",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:task",
        )
        duplicate = constraint()
        with self.assertRaisesRegex(ValidationError, "constraint IDs must be unique"):
            ChangeSet(
                change_set_id="changeset:duplicates",
                goal_id="goal:1",
                operations=[operation],
                constraints=[duplicate, duplicate],
                idempotency_key="idem:duplicates",
                correlation_id="corr:1",
                causation_id="turn:1",
            )

    def test_constraint_kind_rejects_incompatible_entities(self) -> None:
        with self.assertRaises(ValidationError):
            constraint(
                kind="exact_time",
                scope=ConstraintScope(
                    kind="entity_set",
                    entity_refs=[EntityRef(kind="preference", entity_id="preference:1")],
                ),
                value=ExactTimeConstraintValue(
                    start_at="2026-07-20T09:00:00-07:00", duration_minutes=30
                ),
            )

    def test_constraint_temporary_reference_requires_compatible_producer(self) -> None:
        operation = TaskCreateOperation(
            operation_id="op:create",
            title="Task",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:task",
        )
        invalid_constraint = constraint(
            kind="cardinality",
            scope=ConstraintScope(
                kind="entity_set",
                entity_refs=[
                    EntityRef(
                        kind="temporary", entity_id="temp:missing", target_kind="task"
                    )
                ],
            ),
            value=CardinalityConstraintValue(minimum=1, maximum=1),
        )
        with self.assertRaises(ValidationError):
            ChangeSet(
                change_set_id="changeset:1",
                goal_id="goal:1",
                operations=[operation],
                constraints=[invalid_constraint],
                idempotency_key="idem:1",
                correlation_id="corr:1",
                causation_id="turn:1",
            )

    def test_constraint_schema_is_strict_provider_compatible(self) -> None:
        schema = strict_provider_schema(TypeAdapter(Constraint))
        self.assertIs(schema["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
