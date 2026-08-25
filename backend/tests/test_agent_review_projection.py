from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    CalendarDeleteBlockOperation,
    CalendarMoveBlockOperation,
    ChangeSet,
    ChangeSetSimulationResult,
    ConflictGraph,
    EntityRef,
    GoalState,
    OperationConservationReport,
    PreferenceScalarValue,
    PreferenceRetireOperation,
    PreferenceSupersedeOperation,
    ProjectedEffect,
    RecurrenceCreateSeriesOperation,
    RecurrenceRule,
    SemanticFallbackBlock,
    SimulationSnapshot,
    SnapshotFact,
    TaskCreateOperation,
    TaskFields,
    TaskUpdateOperation,
    TaskUpdatePatch,
    WarningBlock,
)
from app.services.agent_kernel.presentation import (
    _operation_after,
    _preference_cleanup_summary,
    SUPPORTED_REVIEW_OPERATION_KINDS,
    ReviewDocumentProjector,
    ReviewProjectionError,
    review_copy_hygiene_violations,
)
from app.services.agent_kernel.operation_conservation import operation_fingerprint


class ReviewProjectionTests(unittest.TestCase):
    def test_preference_cleanup_explains_which_newer_rule_is_kept(self) -> None:
        operation = PreferenceRetireOperation(
            operation_id="operation:retire-old-wake",
            preference_ref=EntityRef(
                kind="preference",
                entity_id="preference:old-wake",
                revision=1,
                title="Wake up at 7:30 AM.",
            ),
            replacement_ref=EntityRef(
                kind="preference",
                entity_id="preference:new-wake",
                revision=1,
                title="Wake up at 7 AM.",
            ),
            reason_code="superseded_by_newer_value",
        )

        self.assertEqual(
            _preference_cleanup_summary(operation),
            "Use the newer preference",
        )
        self.assertEqual(
            _operation_after(operation),
            (
                "Keep the newer preference: Wake up at 7 AM.; "
                "stop using this older conflicting preference"
            ),
        )

    def test_operation_copy_table_covers_every_native_operation_kind(self) -> None:
        self.assertEqual(SUPPORTED_REVIEW_OPERATION_KINDS, {
            "task.create", "task.update", "task.move", "task.archive", "task.delete",
            "calendar.create_block", "calendar.move_block", "calendar.resize_block",
            "calendar.split_block", "calendar.delete_block",
            "recurrence.create_series", "recurrence.update_series",
            "recurrence.update_occurrence", "recurrence.cancel_occurrence",
            "preference.upsert", "preference.supersede", "preference.retire",
            "preference.restore", "preference.delete",
        })

    def _fixture(self) -> tuple[GoalState, ChangeSet, ChangeSetSimulationResult]:
        task_ref = EntityRef(
            kind="temporary",
            entity_id="temp:interview",
            target_kind="task",
            title="Prepare interview",
        )
        child_ref = EntityRef(
            kind="temporary",
            entity_id="temp:research-company",
            target_kind="task",
            title="Research the company",
        )
        operations = [
            TaskCreateOperation(
                operation_id="operation:create-task",
                atomic_group_id="group:interview",
                title="Prepare interview",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work", title="Work"),
                temp_ref=task_ref,
                fields=TaskFields(estimate_minutes=60),
            ),
            TaskCreateOperation(
                operation_id="operation:create-child",
                depends_on=["operation:create-task"],
                atomic_group_id="group:interview",
                title="Research the company",
                workspace_ref=EntityRef(
                    kind="workspace",
                    entity_id="workspace:work",
                    title="Work",
                ),
                parent_ref=task_ref,
                temp_ref=child_ref,
                fields=TaskFields(estimate_minutes=30),
            ),
            CalendarCreateBlockOperation(
                operation_id="operation:schedule-task",
                depends_on=["operation:create-child"],
                atomic_group_id="group:interview",
                task_ref=child_ref,
                start_at=datetime.fromisoformat("2026-07-20T09:00:00-07:00"),
                end_at=datetime.fromisoformat("2026-07-20T09:30:00-07:00"),
            ),
            RecurrenceCreateSeriesOperation(
                operation_id="operation:repeat-task",
                depends_on=["operation:create-task"],
                atomic_group_id="group:interview",
                task_ref=task_ref,
                rule=RecurrenceRule(
                    frequency="weekly",
                    weekdays=["monday", "wednesday"],
                    start_date=datetime(2026, 7, 20).date(),
                    end_date=datetime(2026, 8, 14).date(),
                    timezone="America/Vancouver",
                    preferred_start_time="09:00",
                    duration_minutes=60,
                ),
            ),
        ]
        simulation_id = "simulation:review"
        change_set = ChangeSet(
            revision=3,
            change_set_id="changeset:review",
            goal_id="goal:review",
            status="ready_for_review",
            operations=operations,
            simulation_result_ref=simulation_id,
            idempotency_key="idem:changeset",
            correlation_id="correlation:review",
            causation_id="causation:review",
        )
        snapshot = SimulationSnapshot(
            snapshot_id="snapshot:review",
            fingerprint="a" * 64,
            captured_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
            freshness_boundary=datetime(2026, 7, 19, tzinfo=timezone.utc),
            facts=[SnapshotFact(fact_id="fact:workspace", domain="workspace", entity_ref=EntityRef(kind="workspace", entity_id="workspace:work", title="Work"), fingerprint="fact:fingerprint")],
        )
        graph = ConflictGraph(
            graph_id="graph:review",
            goal_id="goal:review",
            goal_revision=1,
            change_set_id="changeset:review",
            change_set_revision=3,
            change_set_operation_ids=[item.operation_id for item in operations],
            snapshot_fingerprint=snapshot.fingerprint,
            status="clear",
        )
        simulation = ChangeSetSimulationResult(
            simulation_id=simulation_id,
            command_id="command:simulate",
            goal_id="goal:review",
            goal_revision=1,
            resulting_goal_revision=2,
            change_set_id="changeset:review",
            change_set_revision=3,
            input_snapshot=snapshot,
            output_fingerprint="b" * 64,
            status="ready_for_review",
            conflict_graph=graph,
            projected_effects=[
                ProjectedEffect(
                    operation_id=operation.operation_id,
                    kind=kind,
                    entity_refs=[
                        child_ref
                        if operation.operation_id in {
                            "operation:create-child",
                            "operation:schedule-task",
                        }
                        else task_ref
                    ],
                )
                for operation, kind in zip(
                    operations,
                    [
                        "task.created",
                        "task.created",
                        "calendar.created",
                        "recurrence.created",
                    ],
                )
            ],
            conservation=OperationConservationReport(
                complete=True,
                operation_fingerprints=[operation_fingerprint(item) for item in operations],
            ),
            correlation_id="correlation:review",
            causation_id="causation:simulation",
        )
        goal = GoalState(
            revision=2,
            goal_id="goal:review",
            status="ready_for_review",
            original_request="Create, schedule, and repeat interview preparation.",
            current_change_set_id="changeset:review",
            current_change_set_revision=3,
            correlation_id="correlation:review",
            causation_id="causation:goal",
        )
        return goal, change_set, simulation

    def test_mixed_domain_projection_uses_readable_fallback_and_exact_controls(self) -> None:
        goal, change_set, simulation = self._fixture()
        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )

        fallback = next(block for block in review.blocks if isinstance(block, SemanticFallbackBlock))
        self.assertEqual(len(fallback.effects), len(change_set.operations))
        self.assertIn("related planning changes", fallback.summary.lower())
        self.assertFalse(any("operation:" in effect.summary for effect in fallback.effects))
        self.assertEqual([control.action for control in review.controls], ["approve", "edit", "reject"])
        approve = review.controls[0]
        self.assertEqual(approve.binding.goal_revision, goal.revision)
        self.assertEqual(approve.binding.change_set_revision, change_set.revision)
        self.assertEqual(approve.binding.simulation_input_fingerprint, simulation.input_snapshot.fingerprint)
        self.assertEqual(approve.binding.simulation_output_fingerprint, simulation.output_fingerprint)

    def test_calendar_times_are_local_with_one_explicit_time_zone_label(self) -> None:
        goal, change_set, simulation = self._fixture()
        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
            display_time_zone="America/Vancouver",
        )

        fallback = next(
            block for block in review.blocks
            if isinstance(block, SemanticFallbackBlock)
        )
        calendar_change = next(
            change for change in fallback.effects
            if change.change_id == "operation:schedule-task"
        )
        schedule = next(
            block for block in review.blocks
            if block.kind == "schedule_summary"
        )
        expected_range = "July 20, 2026, 9:00 AM\u20139:30 AM"
        self.assertEqual(calendar_change.after, expected_range)
        self.assertEqual(schedule.range_label, expected_range)
        self.assertTrue(all(expected_range in detail for detail in schedule.details))
        self.assertEqual(
            schedule.time_zone,
            "America/Vancouver (UTC\u221207:00)",
        )
        self.assertNotIn("-07:00", calendar_change.after)
        self.assertNotIn("-07:00", schedule.range_label)

        structure = next(
            block for block in review.blocks
            if block.kind == "task_structure"
        )
        self.assertEqual(structure.workspaces[0].workspace.label, "Work")
        root = structure.workspaces[0].roots[0]
        self.assertEqual(root.task.label, "Prepare interview")
        self.assertEqual(root.estimate_minutes, 60)
        self.assertEqual([child.task.label for child in root.children], [
            "Research the company",
        ])
        child = root.children[0]
        self.assertEqual(child.estimate_minutes, 30)
        self.assertEqual(child.scheduled_blocks[0].duration_minutes, 30)
        self.assertEqual(child.scheduled_blocks[0].range_label, expected_range)

    def test_due_date_review_preserves_exact_task_and_revision_without_reselection(self) -> None:
        goal, change_set, simulation = self._fixture()
        task_ref = EntityRef(
            kind="task",
            entity_id="task:407",
            revision=6,
            title="Complete travel application",
        )
        operation = TaskUpdateOperation(
            operation_id="operation:update-deadline",
            task_ref=task_ref,
            updates=TaskUpdatePatch(deadline_at="2026-08-31"),
        )
        change_set = change_set.model_copy(update={
            "operations": [operation],
        })
        simulation = simulation.model_copy(update={
            "projected_effects": [
                ProjectedEffect(
                    operation_id=operation.operation_id,
                    kind="task.updated",
                    entity_refs=[task_ref],
                )
            ],
            "conservation": OperationConservationReport(
                complete=True,
                operation_fingerprints=[operation_fingerprint(operation)],
            ),
        })

        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )

        affected = next(block for block in review.blocks if block.kind == "entity_references")
        differences = next(block for block in review.blocks if block.kind == "semantic_diff")
        self.assertEqual(
            [(item.ref.entity_id, item.ref.revision, item.label) for item in affected.entities],
            [("task:407", 6, "Complete travel application")],
        )
        self.assertEqual(differences.changes[0].after, "Deadline: August 31, 2026")
        self.assertEqual(
            [control.action for control in review.controls],
            ["approve", "edit", "reject"],
        )
        self.assertEqual(
            review.controls[0].binding.change_set_revision,
            change_set.revision,
        )

    def test_recurrence_summary_is_cadence_range_and_bounded_exceptions(self) -> None:
        goal, change_set, simulation = self._fixture()
        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )
        recurrence = next(block for block in review.blocks if block.kind == "recurrence_summary")
        self.assertIn("Monday", recurrence.cadence)
        self.assertIn("Wednesday", recurrence.cadence)
        self.assertIn("July 20", recurrence.range_label)
        self.assertIn("August 14", recurrence.range_label)
        self.assertEqual(recurrence.time_zone, "America/Vancouver")
        self.assertLessEqual(len(recurrence.exceptions), 6)

    def test_calendar_cancellation_preserves_rich_ref_and_exact_time(self) -> None:
        goal, change_set, simulation = self._fixture()
        block_ref = EntityRef(
            kind="calendar_block",
            entity_id="calendar_block:98",
            revision=1,
            title="Team call — Wednesday, July 22 at 6:30 PM–7:30 PM",
        )
        operation = CalendarDeleteBlockOperation(
            operation_id="operation:cancel-wednesday-call",
            block_refs=[block_ref],
        )
        change_set = change_set.model_copy(update={
            "operations": [operation],
        })
        simulation = simulation.model_copy(update={
            "projected_effects": [ProjectedEffect(
                operation_id=operation.operation_id,
                kind="calendar.deleted",
                entity_refs=[EntityRef(
                    kind="calendar_block",
                    entity_id="calendar_block:98",
                    revision=1,
                )],
            )],
            "conservation": OperationConservationReport(
                complete=True,
                operation_fingerprints=[operation_fingerprint(operation)],
            ),
        })

        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )

        affected = next(block for block in review.blocks if block.kind == "entity_references")
        schedule = next(block for block in review.blocks if block.kind == "schedule_summary")
        expected = "Team call — Wednesday, July 22 at 6:30 PM–7:30 PM"
        self.assertEqual([item.label for item in affected.entities], [expected])
        self.assertIn(expected, review.summary)
        self.assertEqual(schedule.cadence, "Cancellation")
        self.assertEqual(schedule.range_label, expected)
        self.assertEqual(schedule.details, [f"Remove scheduled time: {expected}"])

    def test_large_repeated_schedule_changes_are_bounded_without_losing_count(self) -> None:
        goal, change_set, simulation = self._fixture()
        operations = [
            CalendarMoveBlockOperation(
                operation_id=f"operation:move-workout:{index}",
                block_ref=EntityRef(
                    kind="calendar_block",
                    entity_id=f"calendar_block:{100 + index}",
                    revision=1,
                    title=f"Workout occurrence {index + 1}",
                ),
                start_at=datetime(2026, 7, 20 + index % 7, 19, 45, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 20 + index % 7, 21, 45, tzinfo=timezone.utc),
            )
            for index in range(26)
        ]
        change_set = change_set.model_copy(update={"operations": operations})
        simulation = simulation.model_copy(update={
            "projected_effects": [
                ProjectedEffect(
                    operation_id=operation.operation_id,
                    kind="calendar.moved",
                    entity_refs=[operation.block_ref],
                )
                for operation in operations
            ],
            "conservation": OperationConservationReport(
                complete=True,
                operation_fingerprints=[
                    operation_fingerprint(operation) for operation in operations
                ],
            ),
        })

        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )

        schedule = next(
            block for block in review.blocks if block.kind == "schedule_summary"
        )
        self.assertEqual(len(schedule.details), 12)
        self.assertIn("15 additional calendar changes", schedule.details[-1])
        self.assertIn("26 move scheduled time", schedule.details[-1])

    def test_authoritative_entity_title_overrides_model_authored_label(self) -> None:
        goal, change_set, simulation = self._fixture()
        operation = CalendarDeleteBlockOperation(
            operation_id="operation:cancel-wednesday-call",
            block_refs=[EntityRef(
                kind="calendar_block",
                entity_id="calendar_block:98",
                revision=1,
                title="Scheduled time",
            )],
        )
        change_set = change_set.model_copy(update={"operations": [operation]})
        simulation = simulation.model_copy(update={
            "projected_effects": [ProjectedEffect(
                operation_id=operation.operation_id,
                kind="calendar.deleted",
                entity_refs=[operation.block_refs[0]],
            )],
            "conservation": OperationConservationReport(
                complete=True,
                operation_fingerprints=[operation_fingerprint(operation)],
            ),
        })
        expected = "Team call — Wednesday, July 22 at 6:30 PM–7:30 PM"

        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
            entity_titles={"calendar_block:98": expected},
        )

        affected = next(block for block in review.blocks if block.kind == "entity_references")
        self.assertEqual([item.label for item in affected.entities], [expected])

    def test_projection_rejects_stale_or_non_conserved_evidence(self) -> None:
        goal, change_set, simulation = self._fixture()
        stale = simulation.model_copy(update={"change_set_revision": 2})
        with self.assertRaisesRegex(ReviewProjectionError, "identity"):
            ReviewDocumentProjector().project(
                session_id="session:review",
                goal=goal,
                change_set=change_set,
                simulation=stale,
            )
        unsafe = simulation.model_copy(update={"conservation": OperationConservationReport(complete=False)})
        with self.assertRaisesRegex(ReviewProjectionError, "conserved"):
            ReviewDocumentProjector().project(
                session_id="session:review",
                goal=goal,
                change_set=change_set,
                simulation=unsafe,
            )

        wrong_fingerprints = simulation.model_copy(update={
            "conservation": simulation.conservation.model_copy(
                update={
                    "operation_fingerprints": [
                        item.model_copy(update={"sha256": "f" * 64})
                        for item in simulation.conservation.operation_fingerprints
                    ],
                },
            ),
        })
        with self.assertRaisesRegex(ReviewProjectionError, "fingerprints"):
            ReviewDocumentProjector().project(
                session_id="session:review",
                goal=goal,
                change_set=change_set,
                simulation=wrong_fingerprints,
            )

        wrong_effect = simulation.projected_effects[0].model_copy(update={"kind": "task.updated"})
        wrong_effects = simulation.model_copy(update={
            "projected_effects": [wrong_effect, *simulation.projected_effects[1:]],
        })
        with self.assertRaisesRegex(ReviewProjectionError, "effect kind"):
            ReviewDocumentProjector().project(
                session_id="session:review",
                goal=goal,
                change_set=change_set,
                simulation=wrong_effects,
            )

    def test_each_operation_has_one_truthful_semantic_representation(self) -> None:
        goal, change_set, simulation = self._fixture()
        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )
        changes = [
            change
            for block in review.blocks
            for change in (
                block.effects if block.kind == "semantic_fallback"
                else block.changes if block.kind == "semantic_diff"
                else [item for group in block.groups for item in group.changes]
                if block.kind == "grouped_changes"
                else []
            )
        ]
        self.assertCountEqual(
            [
                operation_id
                for change in changes
                for operation_id in (
                    change.change_id,
                    *change.supporting_operation_ids,
                )
            ],
            [operation.operation_id for operation in change_set.operations],
        )
        self.assertTrue(all(change.before and change.after for change in changes))
        self.assertEqual(review.session_id, "session:review")

    def test_gap_update_consolidates_redundant_copy_into_one_semantic_change(
        self,
    ) -> None:
        goal, change_set, simulation = self._fixture()
        global_title = (
            "Schedule tasks shorter than 45 minutes back-to-back; prefer a "
            "15-minute buffer between tasks of 45 minutes or longer."
        )
        scoped_title = (
            "Prefer a 15-minute gap around tasks lasting at least 45 minutes."
        )
        group_id = "group:gap-update"
        replacement = PreferenceSupersedeOperation(
            operation_id="operation:update-gap",
            atomic_group_id=group_id,
            preference_ref=EntityRef(
                kind="preference",
                entity_id="preference:current-gap",
                revision=2,
                title=global_title,
            ),
            value=PreferenceScalarValue(
                value=json.dumps(
                    {
                        "interTaskGapMinutes": 20,
                        "minimumTaskDurationMinutes": 45,
                        "shorterTaskGapMinutes": 0,
                    }
                )
            ),
            semantics={
                "domain": "scheduling",
                "strength": "soft",
                "scope": "global",
            },
        )
        redundant_copy = PreferenceRetireOperation(
            operation_id="operation:retire-gap-copy",
            atomic_group_id=group_id,
            preference_ref=EntityRef(
                kind="preference",
                entity_id="preference:duplicate-gap",
                revision=1,
                title=scoped_title,
            ),
            reason_code="redundant_scoped_copy",
        )
        operations = [replacement, redundant_copy]
        change_set = change_set.model_copy(update={"operations": operations})
        simulation = simulation.model_copy(update={
            "projected_effects": [
                ProjectedEffect(
                    operation_id=replacement.operation_id,
                    kind="preference.superseded",
                    entity_refs=[
                        replacement.preference_ref,
                        EntityRef(
                            kind="temporary",
                            entity_id="projected:preference:update-gap",
                            target_kind="preference",
                            title="Gap between tasks",
                        ),
                    ],
                ),
                ProjectedEffect(
                    operation_id=redundant_copy.operation_id,
                    kind="preference.retired",
                    entity_refs=[redundant_copy.preference_ref],
                ),
            ],
            "conservation": OperationConservationReport(
                complete=True,
                operation_fingerprints=[
                    operation_fingerprint(operation)
                    for operation in operations
                ],
            ),
        })

        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )

        self.assertFalse(
            any(block.kind == "entity_references" for block in review.blocks)
        )
        difference = next(
            block for block in review.blocks
            if block.kind == "semantic_diff"
        )
        self.assertEqual(len(difference.changes), 1)
        change = difference.changes[0]
        self.assertEqual(
            change.summary,
            "Replace overlapping gap preferences",
        )
        self.assertEqual(
            change.before,
            (
                "Replace these overlapping saved preferences:\n"
                f"- {global_title}\n"
                f"- {scoped_title}"
            ),
        )
        self.assertEqual(
            change.supporting_operation_ids,
            ["operation:retire-gap-copy"],
        )
        self.assertEqual(
            change.after,
            (
                "Prefer a 20-minute gap between tasks lasting at least 45 "
                "minutes; shorter tasks can remain back-to-back. Applies to "
                "all planning."
            ),
        )
        self.assertEqual(
            review.summary,
            "Review your updated gap-between-tasks preference before applying it.",
        )

    def test_copy_hygiene_flags_system_leaks_without_rewriting_user_labels(self) -> None:
        goal, change_set, simulation = self._fixture()
        review = ReviewDocumentProjector().project(
            session_id="session:review",
            goal=goal,
            change_set=change_set,
            simulation=simulation,
        )
        structure = next(
            block for block in review.blocks
            if block.kind == "task_structure"
        )
        structure.workspaces[0].roots[0].task.label = "TaskPatch migration notes"
        self.assertEqual(review_copy_hygiene_violations(review), [])

        technical_messages = [
            "The SchedulePatch validator failed.",
            "The start_at field is invalid.",
            "3 operations will run.",
            "Refresh goal:review before continuing.",
            'Provider payload {"secret_key": "value"} was rejected.',
            "Internal reasoning is available in diagnostics.",
        ]
        for index, message in enumerate(technical_messages):
            with self.subTest(message=message):
                leaked = review.model_copy(update={
                    "blocks": [
                        *review.blocks,
                        WarningBlock(
                            block_id=f"block:technical-copy:{index}",
                            title="What to know",
                            severity="warning",
                            message=message,
                        ),
                    ],
                })
                self.assertEqual(review_copy_hygiene_violations(leaked), [message])


if __name__ == "__main__":
    unittest.main()
