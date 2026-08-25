from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, SQLModel, create_engine

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries
from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    CalendarDeleteBlockOperation,
    CalendarMoveBlockOperation,
    CalendarResizeBlockOperation,
    CalendarSegment,
    CalendarSplitBlockOperation,
    ChangeSet,
    EntityRef,
    PreferenceRetireOperation,
    PreferenceScalarValue,
    PreferenceSupersedeOperation,
    PreferenceUpsertOperation,
    RecurrenceCancelOccurrenceOperation,
    RecurrenceCreateSeriesOperation,
    RecurrenceOccurrencePatch,
    RecurrenceRule,
    RecurrenceRulePatch,
    RecurrenceUpdateOccurrenceOperation,
    RecurrenceUpdateSeriesOperation,
    SnapshotFact,
    TaskArchiveOperation,
    TaskCreateOperation,
    TaskDeleteOperation,
    TaskMoveOperation,
    TaskUpdateOperation,
    TaskUpdatePatch,
)
from app.services.agent_kernel.simulation import (
    ChangeSetProjector,
    MAX_SIMULATION_EVIDENCE_FACTS,
    SimulationProjectionError,
    SimulationSnapshotLoader,
    _bounded_snapshot_facts,
)


def change_set(*operations) -> ChangeSet:
    return ChangeSet(
        change_set_id="changeset:simulation",
        goal_id="goal:simulation",
        operations=list(operations),
        idempotency_key="idem:simulation",
        correlation_id="corr:simulation",
        causation_id="turn:simulation",
    )


class AgentChangeSetSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(Category(id=1, name="Work"))
            session.add(StrategyNode(id=1, category_id=1, title="Existing"))
            session.add(StrategyNode(id=2, category_id=1, title="Blocker", priority="high"))
            session.add(
                CalendarBlock(
                    id=1,
                    task_id=2,
                    category_id=1,
                    start_at=datetime(2026, 7, 20, 9, 0),
                    end_at=datetime(2026, 7, 20, 10, 0),
                    is_pinned=True,
                    is_protected=True,
                )
            )
            session.commit()

    def test_snapshot_is_deterministic_detached_and_does_not_dirty_session(self) -> None:
        draft = change_set(
            TaskDeleteOperation(
                operation_id="op:delete", task_refs=[EntityRef(kind="task", entity_id="task:1")]
            )
        )
        with Session(self.engine) as session:
            first = SimulationSnapshotLoader(session).load(draft)
            second = SimulationSnapshotLoader(session).load(draft)
            self.assertEqual(first.snapshot.fingerprint, second.snapshot.fingerprint)
            self.assertFalse(session.dirty)
            self.assertFalse(session.new)
            first.world.tasks["task:1"].title = "Projected only"
            self.assertEqual(session.get(StrategyNode, 1).title, "Existing")

    def test_preference_supersede_projects_linked_constraint_inactive(self) -> None:
        with Session(self.engine) as session:
            session.add(
                UserPreference(
                    id="gap-old",
                    preference_type="inter_task_gap",
                    value_json='{"interTaskGapMinutes":15}',
                )
            )
            session.add(
                SchedulingConstraint(
                    id="gap-old-constraint",
                    source_preference_id="gap-old",
                    constraint_type="inter_task_gap",
                    value_json='{"interTaskGapMinutes":15}',
                )
            )
            session.commit()
            draft = change_set(
                PreferenceSupersedeOperation(
                    operation_id="op:supersede-gap",
                    preference_ref=EntityRef(
                        kind="preference",
                        entity_id="preference:gap-old",
                    ),
                    value=PreferenceScalarValue(
                        value='{"interTaskGapMinutes":15,"minimumTaskDurationMinutes":45}'
                    ),
                )
            )
            loaded = SimulationSnapshotLoader(session).load(draft)
            result = ChangeSetProjector().project(draft, loaded.world)

        projected_constraint = result.world.scheduling_constraints[
            "gap-old-constraint"
        ]
        self.assertFalse(projected_constraint.active)

    def test_temp_created_task_can_be_scheduled_in_one_projected_world(self) -> None:
        create = TaskCreateOperation(
            operation_id="op:create",
            title="Interview",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
            temp_ref="temp:interview",
        )
        schedule = CalendarCreateBlockOperation(
            operation_id="op:schedule",
            depends_on=["op:create"],
            task_ref=EntityRef(kind="temporary", entity_id="temp:interview", target_kind="task"),
            start_at=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc),
            temp_ref="temp:interview-block",
        )
        draft = change_set(create, schedule)
        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(draft)
            result = ChangeSetProjector().project(draft, loaded.world)
            self.assertIn("temp:interview", result.world.tasks)
            self.assertEqual(
                result.world.calendar_blocks["temp:interview-block"].task_ref.entity_id,
                "temp:interview",
            )
            self.assertEqual([effect.operation_id for effect in result.effects], ["op:create", "op:schedule"])

    def test_multiple_children_can_be_created_under_revisioned_existing_parent(self) -> None:
        draft = change_set(
            TaskCreateOperation(
                operation_id="op:gather-documents",
                title="Gather tax documents",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                parent_ref=EntityRef(kind="task", entity_id="task:1"),
                temp_ref="temp:gather-documents",
            ),
            TaskCreateOperation(
                operation_id="op:file-return",
                title="File the return",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                parent_ref=EntityRef(kind="task", entity_id="task:1"),
                temp_ref="temp:file-return",
            ),
        )

        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(draft)
            result = ChangeSetProjector().project(draft, loaded.world)

        self.assertEqual(
            result.world.tasks["temp:gather-documents"].parent_ref.entity_id,
            "task:1",
        )
        self.assertEqual(
            result.world.tasks["temp:file-return"].parent_ref.entity_id,
            "task:1",
        )

    def test_task_only_snapshot_excludes_unrelated_large_calendar_history(self) -> None:
        draft = change_set(
            TaskCreateOperation(
                operation_id="op:gather-documents",
                title="Gather tax documents",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                parent_ref=EntityRef(kind="task", entity_id="task:1"),
                temp_ref="temp:gather-documents",
            )
        )
        with Session(self.engine) as session:
            start = datetime(2020, 1, 1, 9, 0)
            session.add_all(
                [
                    CalendarBlock(
                        id=index,
                        task_id=2,
                        category_id=1,
                        start_at=start + timedelta(days=index),
                        end_at=start + timedelta(days=index, hours=1),
                    )
                    for index in range(2, 2102)
                ]
            )
            session.commit()

            loaded = SimulationSnapshotLoader(session).load(draft)
            projected = ChangeSetProjector().project(draft, loaded.world)

        self.assertEqual(loaded.world.calendar_blocks, {})
        self.assertEqual(loaded.world.recurrence_series, {})
        self.assertEqual(loaded.world.preferences, {})
        self.assertLess(len(loaded.snapshot.facts), 2048)
        self.assertEqual(
            {fact.domain for fact in loaded.snapshot.facts},
            {"task", "workspace"},
        )
        self.assertIn("temp:gather-documents", projected.world.tasks)

    def test_mixed_task_and_calendar_flow_bounds_full_world_evidence(self) -> None:
        create = TaskCreateOperation(
            operation_id="op:create-meal-prep",
            title="Cook and clean",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
            temp_ref="temp:meal-prep",
        )
        schedule = CalendarCreateBlockOperation(
            operation_id="op:schedule-meal-prep",
            depends_on=["op:create-meal-prep"],
            task_ref=EntityRef(
                kind="temporary",
                entity_id="temp:meal-prep",
                target_kind="task",
            ),
            start_at=datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
            temp_ref="temp:meal-prep-block",
        )
        draft = change_set(create, schedule)

        with Session(self.engine) as session:
            session.add_all(
                [
                    StrategyNode(
                        id=index,
                        category_id=1,
                        title=f"Unrelated task {index}",
                    )
                    for index in range(3, 2303)
                ]
            )
            session.commit()

            loaded = SimulationSnapshotLoader(session).load(draft)
            projected = ChangeSetProjector().project(draft, loaded.world)

        self.assertGreater(len(loaded.world.tasks), 2048)
        self.assertEqual(
            len(loaded.snapshot.facts),
            MAX_SIMULATION_EVIDENCE_FACTS,
        )
        self.assertIn(
            "fact:workspace:1",
            {fact.fact_id for fact in loaded.snapshot.facts},
        )
        self.assertEqual(
            len({fact.fact_id for fact in loaded.snapshot.facts}),
            len(loaded.snapshot.facts),
        )
        self.assertEqual(
            len(projected.final_facts),
            MAX_SIMULATION_EVIDENCE_FACTS,
        )
        projected_operation_ids = {
            operation_id
            for fact in projected.final_facts
            for operation_id in fact.source_operation_ids
        }
        self.assertEqual(
            projected_operation_ids,
            {"op:create-meal-prep", "op:schedule-meal-prep"},
        )
        combined_evidence_bytes = len(loaded.snapshot.model_dump_json().encode("utf-8")) + sum(
            len(fact.model_dump_json().encode("utf-8"))
            for fact in projected.final_facts
        )
        self.assertLess(combined_evidence_bytes, 400 * 1024)

    def test_snapshot_evidence_is_unique_and_prioritizes_addressed_entities(self) -> None:
        required = SnapshotFact(
            fact_id="fact:task:999",
            domain="task",
            entity_ref=EntityRef(kind="task", entity_id="task:999", revision=7),
            fingerprint="fingerprint:required",
        )
        duplicate = SnapshotFact(
            fact_id="fact:task:1",
            domain="task",
            entity_ref=EntityRef(kind="task", entity_id="task:1", revision=1),
            fingerprint="fingerprint:duplicate",
        )
        filler = [
            SnapshotFact(
                fact_id=f"fact:task:{index}",
                domain="task",
                entity_ref=EntityRef(
                    kind="task",
                    entity_id=f"task:{index}",
                    revision=1,
                ),
                fingerprint=f"fingerprint:{index}",
            )
            for index in range(2, 602)
        ]
        selected = _bounded_snapshot_facts(
            [
                duplicate,
                *filler,
                required,
                duplicate.model_copy(update={"fingerprint": "fingerprint:duplicate-new"}),
            ],
            required_entities={("task", "task:999")},
        )

        self.assertEqual(len(selected), MAX_SIMULATION_EVIDENCE_FACTS)
        self.assertEqual(selected[0], required)
        self.assertEqual(len({fact.fact_id for fact in selected}), len(selected))
        self.assertEqual(
            sum(fact.fact_id == "fact:task:1" for fact in selected),
            1,
        )

    def test_projected_move_and_delete_are_applied_before_final_validation(self) -> None:
        move = CalendarMoveBlockOperation(
            operation_id="op:move-blocker",
            block_ref=EntityRef(kind="calendar_block", entity_id="calendar_block:1"),
            start_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc),
        )
        delete = CalendarDeleteBlockOperation(
            operation_id="op:delete-blocker",
            depends_on=["op:move-blocker"],
            block_refs=[EntityRef(kind="calendar_block", entity_id="calendar_block:1")],
        )
        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(change_set(move, delete))
            result = ChangeSetProjector().project(change_set(move, delete), loaded.world)
            self.assertNotIn("calendar_block:1", result.world.calendar_blocks)

    def test_task_delete_projects_dependent_calendar_cleanup(self) -> None:
        draft = change_set(
            TaskDeleteOperation(
                operation_id="op:delete-task",
                task_refs=[EntityRef(kind="task", entity_id="task:2")],
            )
        )
        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(draft)
            result = ChangeSetProjector().project(draft, loaded.world)
            self.assertNotIn("task:2", result.world.tasks)
            self.assertNotIn("calendar_block:2", result.world.calendar_blocks)

    def test_created_recurrence_expands_bounded_composite_occurrences(self) -> None:
        recurring = RecurrenceCreateSeriesOperation(
            operation_id="op:recurrence",
            task_ref=EntityRef(kind="task", entity_id="task:1"),
            rule=RecurrenceRule(
                frequency="weekly",
                weekdays=["monday", "wednesday"],
                start_date=datetime(2026, 7, 20).date(),
                occurrence_count=6,
                timezone="UTC",
                preferred_start_time="09:00",
                duration_minutes=60,
            ),
            temp_ref="temp:series",
        )
        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(change_set(recurring))
            projected = ChangeSetProjector().project(change_set(recurring), loaded.world)
            occurrences = [
                item for item in projected.world.calendar_blocks.values()
                if item.series_ref is not None and item.series_ref.entity_id == "temp:series"
            ]
            self.assertEqual(len(occurrences), 6)
            self.assertTrue(all(item.occurrence_ref.entity_id.startswith("recurrence_occurrence:series:") for item in occurrences))

    def test_final_task_graph_rejects_parent_cycles(self) -> None:
        first = TaskMoveOperation(
            operation_id="op:move-1",
            task_refs=[EntityRef(kind="task", entity_id="task:1")],
            destination_parent_ref=EntityRef(kind="task", entity_id="task:2"),
        )
        second = TaskMoveOperation(
            operation_id="op:move-2",
            depends_on=["op:move-1"],
            task_refs=[EntityRef(kind="task", entity_id="task:2")],
            destination_parent_ref=EntityRef(kind="task", entity_id="task:1"),
        )
        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(change_set(first, second))
            with self.assertRaisesRegex(SimulationProjectionError, "parent cycle"):
                ChangeSetProjector().project(change_set(first, second), loaded.world)

    def test_recurring_occurrence_cannot_be_split_as_an_ordinary_block(self) -> None:
        with Session(self.engine) as session:
            block = session.get(CalendarBlock, 1)
            block.is_recurring = True
            block.recurrence_series_id = "series:1"
            block.recurrence_key = "2026-07-20"
            session.add(block)
            session.commit()
            split = CalendarSplitBlockOperation(
                operation_id="op:split-recurring",
                block_ref=EntityRef(kind="calendar_block", entity_id="calendar_block:1"),
                segments=[
                    CalendarSegment(start_at=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc), end_at=datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)),
                    CalendarSegment(start_at=datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc), end_at=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)),
                ],
            )
            loaded = SimulationSnapshotLoader(session).load(change_set(split))
            with self.assertRaisesRegex(SimulationProjectionError, "recurrence.update_occurrence"):
                ChangeSetProjector().project(change_set(split), loaded.world)

    def test_all_seventeen_native_operation_kinds_compose_in_dependency_order(self) -> None:
        with Session(self.engine) as session:
            session.add(
                RecurrenceSeries(
                    id="series-existing",
                    task_id=1,
                    category_id=1,
                    frequency="weekly",
                    interval=1,
                    days_of_week_json='["monday"]',
                    start_date=datetime(2026, 7, 1).date(),
                    preferred_start_time="08:00",
                    allowed_start_time="06:00",
                    allowed_end_time="12:00",
                    duration_minutes=60,
                )
            )
            session.add(
                CalendarBlock(
                    id=2,
                    task_id=1,
                    category_id=1,
                    start_at=datetime(2026, 7, 21, 8, 0),
                    end_at=datetime(2026, 7, 21, 9, 0),
                    is_recurring=True,
                    recurrence_series_id="series-existing",
                    recurrence_key="2026-07-21",
                )
            )
            session.add(UserPreference(id="pref-1", preference_type="gap", value_json='{"minutes":15}'))
            session.add(UserPreference(id="pref-2", preference_type="morning", value_json='true'))
            session.commit()

            operations = [
                TaskCreateOperation(operation_id="op:task-create", title="Created", workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"), temp_ref="temp:created"),
                TaskCreateOperation(operation_id="op:delete-create", title="Disposable", workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"), temp_ref="temp:disposable"),
                TaskUpdateOperation(operation_id="op:task-update", task_ref=EntityRef(kind="task", entity_id="task:1"), updates=TaskUpdatePatch(priority="high")),
                TaskMoveOperation(operation_id="op:task-move", depends_on=["op:task-create"], task_refs=[EntityRef(kind="temporary", entity_id="temp:created", target_kind="task")], destination_workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1")),
                TaskArchiveOperation(operation_id="op:task-archive", task_refs=[EntityRef(kind="task", entity_id="task:1")]),
                TaskDeleteOperation(operation_id="op:task-delete", depends_on=["op:delete-create"], task_refs=[EntityRef(kind="temporary", entity_id="temp:disposable", target_kind="task")]),
                CalendarCreateBlockOperation(operation_id="op:calendar-create", depends_on=["op:task-create"], task_ref=EntityRef(kind="temporary", entity_id="temp:created", target_kind="task"), start_at=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc), end_at=datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc), temp_ref="temp:new-block"),
                CalendarResizeBlockOperation(operation_id="op:calendar-resize", depends_on=["op:calendar-create"], block_ref=EntityRef(kind="temporary", entity_id="temp:new-block", target_kind="calendar_block"), end_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)),
                CalendarSplitBlockOperation(operation_id="op:calendar-split", depends_on=["op:calendar-resize"], block_ref=EntityRef(kind="temporary", entity_id="temp:new-block", target_kind="calendar_block"), segments=[CalendarSegment(start_at=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc), end_at=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)), CalendarSegment(start_at=datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc), end_at=datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc))]),
                CalendarMoveBlockOperation(operation_id="op:calendar-move", block_ref=EntityRef(kind="calendar_block", entity_id="calendar_block:1"), start_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc), end_at=datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)),
                CalendarDeleteBlockOperation(operation_id="op:calendar-delete", depends_on=["op:calendar-move"], block_refs=[EntityRef(kind="calendar_block", entity_id="calendar_block:1")]),
                RecurrenceCreateSeriesOperation(operation_id="op:recurrence-create", depends_on=["op:task-create"], task_ref=EntityRef(kind="temporary", entity_id="temp:created", target_kind="task"), rule=RecurrenceRule(frequency="weekly", weekdays=["monday", "wednesday"], start_date=datetime(2026, 7, 20).date(), timezone="UTC"), temp_ref="temp:new-series"),
                RecurrenceUpdateSeriesOperation(operation_id="op:recurrence-update", depends_on=["op:recurrence-create"], series_ref=EntityRef(kind="temporary", entity_id="temp:new-series", target_kind="recurrence_series"), updates=RecurrenceRulePatch(interval=2)),
                RecurrenceUpdateOccurrenceOperation(operation_id="op:occurrence-update", occurrence_ref=EntityRef(kind="recurrence_occurrence", entity_id="recurrence_occurrence:series-existing:2026-07-21"), updates=RecurrenceOccurrencePatch(start_at=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc), end_at=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))),
                RecurrenceCancelOccurrenceOperation(operation_id="op:occurrence-cancel", depends_on=["op:occurrence-update"], occurrence_ref=EntityRef(kind="recurrence_occurrence", entity_id="recurrence_occurrence:series-existing:2026-07-21")),
                PreferenceUpsertOperation(operation_id="op:preference-upsert", preference_key="focus_window", value=PreferenceScalarValue(value="morning")),
                PreferenceSupersedeOperation(operation_id="op:preference-supersede", preference_ref=EntityRef(kind="preference", entity_id="preference:pref-1"), value=PreferenceScalarValue(value=30)),
                PreferenceRetireOperation(operation_id="op:preference-retire", preference_ref=EntityRef(kind="preference", entity_id="preference:pref-2")),
            ]
            all_kinds = change_set(*operations)
            loaded = SimulationSnapshotLoader(session).load(all_kinds)
            result = ChangeSetProjector().project(all_kinds, loaded.world)
            self.assertEqual(len(result.effects), len(operations))
            self.assertEqual({effect.kind for effect in result.effects}, {
                "task.created", "task.updated", "task.moved", "task.archived", "task.deleted",
                "calendar.created", "calendar.moved", "calendar.resized", "calendar.split", "calendar.deleted",
                "recurrence.created", "recurrence.updated", "recurrence.occurrence_updated", "recurrence.occurrence_cancelled",
                "preference.upserted", "preference.superseded", "preference.retired",
            })
            self.assertNotIn("calendar_block:2", result.world.calendar_blocks)
            self.assertFalse(result.world.preferences["preference:pref-1"].active)
            self.assertFalse(result.world.preferences["preference:pref-2"].active)


if __name__ == "__main__":
    unittest.main()
