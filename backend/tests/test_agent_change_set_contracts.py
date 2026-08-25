from __future__ import annotations

import unittest

from pydantic import TypeAdapter, ValidationError

from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    CalendarSegment,
    CalendarSplitBlockOperation,
    ChangeSet,
    EntityRef,
    PreferenceScalarValue,
    PreferenceUpsertOperation,
    RecurrenceCreateSeriesOperation,
    RecurrenceRule,
    RecurrenceRulePatch,
    RecurrenceUpdateSeriesOperation,
    TaskArchiveOperation,
    TaskCreateOperation,
    TaskFields,
    TaskUpdateOperation,
    TaskUpdatePatch,
    strict_provider_schema,
)


class AgentChangeSetContractTests(unittest.TestCase):
    def test_changeset_lifecycle_requires_exact_simulation_and_approval_metadata(self) -> None:
        operation = TaskCreateOperation(
            operationId="op:create",
            title="Task",
            workspaceRef={"kind": "workspace", "entityId": "workspace:1"},
        )
        base = {
            "changeSetId": "changeset:1",
            "goalId": "goal:1",
            "operations": [operation],
            "idempotencyKey": "draft:1",
            "correlationId": "corr:1",
            "causationId": "turn:1",
        }
        with self.assertRaises(ValidationError):
            ChangeSet(**base, status="applied")
        with self.assertRaises(ValidationError):
            ChangeSet(
                **base,
                status="ready_for_review",
                simulationResultRef="simulation:1",
                approvedRevision=1,
            )
        applied = ChangeSet(
            **base,
            status="applied",
            simulationResultRef="simulation:1",
            approvedRevision=1,
        )
        self.assertEqual(applied.approved_revision, 1)

    def test_temporary_reference_declares_eventual_kind_and_legacy_string_migrates(self) -> None:
        operation = TaskCreateOperation(
            operation_id="op:create",
            title="Parent",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            temp_ref="temp:parent",
        )
        self.assertEqual(operation.temp_ref.kind, "temporary")
        self.assertEqual(operation.temp_ref.target_kind, "task")

        with self.assertRaises(ValidationError):
            EntityRef(kind="temporary", entity_id="temp:missing-kind")
        with self.assertRaises(ValidationError):
            EntityRef(kind="task", entity_id="task:1", target_kind="task")

    def test_task_payloads_are_closed_typed_and_nonempty(self) -> None:
        created = TaskCreateOperation(
            operation_id="op:create",
            title="Prepare interview",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
            fields=TaskFields(priority="high", estimate_minutes=45),
        )
        self.assertEqual(created.fields.estimate_minutes, 45)

        with self.assertRaises(ValidationError):
            TaskFields.model_validate({"priority": "high", "unknown": True})
        with self.assertRaises(ValidationError):
            TaskUpdatePatch()
        with self.assertRaises(ValidationError):
            TaskUpdatePatch(title=None)
        self.assertEqual(TaskUpdatePatch(clear_fields=["notes"]).clear_fields, ["notes"])
        with self.assertRaises(ValidationError):
            TaskUpdateOperation(
                operation_id="op:update",
                task_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
                updates=TaskUpdatePatch(title="Wrong target"),
            )

    def test_plural_targets_are_nonempty_duplicate_free_and_correct_kind(self) -> None:
        task = EntityRef(kind="task", entity_id="task:1")
        with self.assertRaises(ValidationError):
            TaskArchiveOperation(operation_id="op:archive", task_refs=[task, task])
        with self.assertRaises(ValidationError):
            TaskArchiveOperation(
                operation_id="op:archive",
                task_refs=[EntityRef(kind="calendar_block", entity_id="block:1")],
            )

    def test_calendar_intervals_and_split_segments_are_typed(self) -> None:
        operation = CalendarCreateBlockOperation(
            operation_id="op:block",
            task_ref=EntityRef(
                kind="temporary",
                entity_id="temp:task",
                target_kind="task",
            ),
            start_at="2026-07-20T09:00:00-07:00",
            end_at="2026-07-20T10:00:00-07:00",
            depends_on=["op:create"],
        )
        self.assertLess(operation.start_at, operation.end_at)

        with self.assertRaises(ValidationError):
            CalendarCreateBlockOperation(
                operation_id="op:block",
                task_ref=EntityRef(kind="task", entity_id="task:1"),
                start_at="2026-07-20T10:00:00-07:00",
                end_at="2026-07-20T09:00:00-07:00",
            )
        with self.assertRaises(ValidationError):
            CalendarSplitBlockOperation(
                operation_id="op:split",
                block_ref=EntityRef(kind="calendar_block", entity_id="block:1"),
                segments=[
                    CalendarSegment(
                        start_at="2026-07-20T09:30:00-07:00",
                        end_at="2026-07-20T10:00:00-07:00",
                    ),
                    CalendarSegment(
                        start_at="2026-07-20T09:00:00-07:00",
                        end_at="2026-07-20T09:30:00-07:00",
                    ),
                ],
            )

    def test_recurrence_and_preferences_use_closed_values(self) -> None:
        recurrence = RecurrenceCreateSeriesOperation(
            operation_id="op:series",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            rule=RecurrenceRule(
                frequency="weekly",
                interval=1,
                weekdays=["monday", "wednesday"],
                start_date="2026-07-20",
            ),
        )
        self.assertEqual(recurrence.rule.frequency, "weekly")
        with self.assertRaises(ValidationError):
            RecurrenceRule(frequency="daily", weekdays=["monday"])
        with self.assertRaises(ValidationError):
            RecurrenceRulePatch(end_date="2026-08-01", occurrence_count=3)
        with self.assertRaises(ValidationError):
            RecurrenceRulePatch(frequency="weekly", month_days=[1])

        preference = PreferenceUpsertOperation(
            operation_id="op:preference",
            preference_key="focus_block_minutes",
            value=PreferenceScalarValue(value=50),
        )
        self.assertEqual(preference.value.kind, "scalar")

    def test_reloaded_sparse_recurrence_patch_does_not_promote_null_fields(self) -> None:
        change_set = ChangeSet(
            changeSetId="changeset:recurrence-sparse",
            goalId="goal:recurrence-sparse",
            operations=[
                RecurrenceUpdateSeriesOperation(
                    operationId="operation:recurrence-sparse",
                    seriesRef=EntityRef(
                        kind="recurrence_series",
                        entityId="recurrence_series:one",
                        revision=1,
                    ),
                    updates=RecurrenceRulePatch(interval=2),
                )
            ],
            idempotencyKey="draft:recurrence-sparse",
            correlationId="correlation:recurrence-sparse",
            causationId="turn:recurrence-sparse",
        )
        reloaded = ChangeSet.model_validate(
            change_set.model_dump(mode="json", by_alias=True)
        )
        operation = reloaded.operations[0]

        self.assertEqual(
            operation.updates.model_dump(
                mode="python", exclude_unset=True, exclude_none=True
            ),
            {"interval": 2},
        )

    def test_operation_schema_is_strict_provider_compatible(self) -> None:
        schema = strict_provider_schema(TypeAdapter(TaskCreateOperation))
        self.assertIs(schema["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
