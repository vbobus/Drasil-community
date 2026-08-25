from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    ChangeSet,
    EntityRef,
    TaskCreateOperation,
)


def task_create(operation_id: str, temp_id: str, *, depends_on: list[str] | None = None):
    return TaskCreateOperation(
        operation_id=operation_id,
        title="Same title",
        workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
        temp_ref=temp_id,
        depends_on=depends_on or [],
    )


def change_set(*operations, **overrides):
    payload = {
        "change_set_id": "changeset:1",
        "goal_id": "goal:1",
        "operations": list(operations),
        "idempotency_key": "idem:1",
        "correlation_id": "corr:1",
        "causation_id": "turn:1",
    }
    payload.update(overrides)
    return ChangeSet(**payload)


class AgentChangeSetGraphTests(unittest.TestCase):
    def test_operation_ids_and_temp_producers_are_unique_without_payload_deduplication(self) -> None:
        first = task_create("op:1", "temp:1")
        second = task_create("op:2", "temp:2")
        draft = change_set(first, second)
        self.assertEqual([item.operation_id for item in draft.operations], ["op:1", "op:2"])

        with self.assertRaises(ValidationError):
            change_set(first, first.model_copy(update={"temp_ref": second.temp_ref}))
        with self.assertRaises(ValidationError):
            change_set(first, second.model_copy(update={"temp_ref": first.temp_ref}))

    def test_dependencies_must_exist_and_be_acyclic(self) -> None:
        with self.assertRaises(ValidationError):
            change_set(task_create("op:1", "temp:1", depends_on=["op:missing"]))
        with self.assertRaises(ValidationError):
            change_set(
                task_create("op:1", "temp:1", depends_on=["op:2"]),
                task_create("op:2", "temp:2", depends_on=["op:1"]),
            )

    def test_temp_consumers_require_compatible_producer_dependency(self) -> None:
        producer = task_create("op:create", "temp:task")
        consumer = CalendarCreateBlockOperation(
            operation_id="op:schedule",
            task_ref=EntityRef(
                kind="temporary", entity_id="temp:task", target_kind="task"
            ),
            start_at="2026-07-20T09:00:00-07:00",
            end_at="2026-07-20T10:00:00-07:00",
        )
        with self.assertRaises(ValidationError):
            change_set(producer, consumer)

        valid = change_set(producer, consumer.model_copy(update={"depends_on": ["op:create"]}))
        self.assertEqual(valid.dependency_order(), ["op:create", "op:schedule"])

    def test_dependency_order_is_stable_without_rewriting_input_order(self) -> None:
        parent = task_create("op:parent", "temp:parent")
        child = task_create("op:child", "temp:child", depends_on=["op:parent"])
        independent = task_create("op:independent", "temp:independent")
        draft = change_set(independent, parent, child)
        self.assertEqual(
            [item.operation_id for item in draft.operations],
            ["op:independent", "op:parent", "op:child"],
        )
        self.assertEqual(
            draft.dependency_order(), ["op:independent", "op:parent", "op:child"]
        )

    def test_story_84_accepts_only_whole_changeset_atomic_drafts(self) -> None:
        with self.assertRaises(ValidationError):
            change_set(task_create("op:1", "temp:1"), atomic_by_default=False)
        with self.assertRaises(ValidationError):
            change_set(task_create("op:1", "temp:1"), approved_revision=1)
        with self.assertRaises(ValidationError):
            change_set(task_create("op:1", "temp:1"), simulation_result_ref="simulation:1")

    def test_operation_and_payload_budgets_fail_closed(self) -> None:
        operations = [task_create(f"op:{index}", f"temp:{index}") for index in range(129)]
        with self.assertRaises(ValidationError):
            change_set(*operations)


if __name__ == "__main__":
    unittest.main()
