from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.models.recurrence_series import (
    RecurrenceOccurrenceException,
    RecurrenceSeries,
)
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
    TaskArchiveOperation,
    TaskCreateOperation,
    TaskDeleteOperation,
    TaskFields,
    TaskMoveOperation,
    TaskUpdateOperation,
    TaskUpdatePatch,
)
from app.services.agent_kernel.domain_mutations import DomainMutationError, NativeDomainMutationExecutor


UTC = timezone.utc


class AgentDomainMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        event.listen(
            self.engine,
            "connect",
            lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(Category(id=1, name="Work"))
            session.commit()

    def change_set(self) -> ChangeSet:
        return ChangeSet(
            changeSetId="changeset:1",
            goalId="goal:1",
            idempotencyKey="draft:1",
            correlationId="corr:1",
            causationId="turn:1",
            operations=[
                TaskCreateOperation(
                    operationId="op:create-task",
                    title="Interview prep",
                    workspaceRef=EntityRef(kind="workspace", entityId="workspace:1", revision=1),
                    tempRef="temp:task",
                ),
                CalendarCreateBlockOperation(
                    operationId="op:create-block",
                    dependsOn=["op:create-task"],
                    taskRef=EntityRef(kind="temporary", entityId="temp:task", targetKind="task"),
                    startAt=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
                    endAt=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
                    tempRef="temp:block",
                ),
            ],
        )

    def test_temp_create_then_schedule_is_flush_only_and_stable(self) -> None:
        with Session(self.engine) as session:
            result = NativeDomainMutationExecutor(session).apply(self.change_set())
            self.assertTrue(session.in_transaction())
            self.assertEqual(len(result.bindings), 2)
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 1)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 1)
            session.rollback()

        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 0)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 0)

    def test_injected_failure_rolls_back_under_coordinator_transaction(self) -> None:
        with Session(self.engine) as session:
            executor = NativeDomainMutationExecutor(session, fail_after_operation="op:create-task")
            with self.assertRaises(DomainMutationError):
                executor.apply(self.change_set())
            session.rollback()

        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StrategyNode)).all()), 0)
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), 0)

    @staticmethod
    def native(operations: list[object], suffix: str) -> ChangeSet:
        return ChangeSet(
            changeSetId=f"changeset:{suffix}",
            goalId="goal:1",
            idempotencyKey=f"draft:{suffix}",
            correlationId="corr:1",
            causationId=f"turn:{suffix}",
            operations=operations,
        )

    def test_all_task_operation_kinds_execute_by_exact_identity(self) -> None:
        with Session(self.engine) as session:
            session.add_all(
                [
                    StrategyNode(id=10, category_id=1, title="Update"),
                    StrategyNode(id=11, category_id=1, title="Move"),
                    StrategyNode(id=12, category_id=1, title="Archive"),
                    StrategyNode(id=13, category_id=1, title="Delete"),
                ]
            )
            session.commit()
            operations = [
                TaskCreateOperation(operationId="op:create", title="Created", workspaceRef={"kind": "workspace", "entityId": "workspace:1", "revision": 1}, tempRef="temp:created"),
                TaskUpdateOperation(operationId="op:update", taskRef={"kind": "task", "entityId": "task:10", "revision": 1}, updates=TaskUpdatePatch(title="Updated")),
                TaskMoveOperation(operationId="op:move", taskRefs=[{"kind": "task", "entityId": "task:11", "revision": 1}], destinationParentRef={"kind": "task", "entityId": "task:10", "revision": 1}),
                TaskArchiveOperation(operationId="op:archive", taskRefs=[{"kind": "task", "entityId": "task:12", "revision": 1}]),
                TaskDeleteOperation(operationId="op:delete", taskRefs=[{"kind": "task", "entityId": "task:13", "revision": 1}]),
            ]
            NativeDomainMutationExecutor(session).apply(self.native(operations, "tasks"))
            session.commit()
            self.assertEqual(session.get(StrategyNode, 10).title, "Updated")
            self.assertEqual(session.get(StrategyNode, 11).parent_id, 10)
            self.assertIn("state:archived", session.get(StrategyNode, 12).tags)
            self.assertIsNone(session.get(StrategyNode, 13))

    def test_backlog_placement_is_applied_to_create_and_update_subtree(self) -> None:
        with Session(self.engine) as session:
            session.add_all(
                [
                    StrategyNode(id=20, category_id=1, title="Parent"),
                    StrategyNode(id=21, category_id=1, parent_id=20, title="Child"),
                ]
            )
            session.commit()
            operations = [
                TaskCreateOperation(
                    operationId="op:create-backlog",
                    title="Created in backlog",
                    workspaceRef={
                        "kind": "workspace",
                        "entityId": "workspace:1",
                        "revision": 1,
                    },
                    fields=TaskFields(
                        is_backlog=True,
                        tags=["planning", "later"],
                    ),
                ),
                TaskUpdateOperation(
                    operationId="op:update-backlog",
                    taskRef={
                        "kind": "task",
                        "entityId": "task:20",
                        "revision": 1,
                    },
                    updates=TaskUpdatePatch(
                        is_backlog=True,
                        tags=["reviewed"],
                    ),
                ),
            ]

            NativeDomainMutationExecutor(session).apply(
                self.native(operations, "backlog")
            )
            session.commit()

            created = session.exec(
                select(StrategyNode).where(
                    StrategyNode.title == "Created in backlog"
                )
            ).one()
            self.assertTrue(created.is_backlog)
            self.assertEqual(created.tags, '["planning", "later"]')
            self.assertTrue(session.get(StrategyNode, 20).is_backlog)
            self.assertEqual(session.get(StrategyNode, 20).tags, '["reviewed"]')
            self.assertTrue(session.get(StrategyNode, 21).is_backlog)

    def test_all_calendar_operation_kinds_execute_and_split_cardinality_is_exact(self) -> None:
        with Session(self.engine) as session:
            session.add(StrategyNode(id=20, category_id=1, title="Calendar"))
            session.commit()
            base = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
            for block_id in range(20, 24):
                session.add(CalendarBlock(id=block_id, task_id=20, category_id=1, start_at=base + timedelta(hours=block_id - 20), end_at=base + timedelta(hours=block_id - 19)))
            session.commit()
            operations = [
                CalendarCreateBlockOperation(operationId="op:create", taskRef={"kind": "task", "entityId": "task:20", "revision": 1}, startAt=base + timedelta(hours=8), endAt=base + timedelta(hours=9), tempRef="temp:new-block"),
                CalendarMoveBlockOperation(operationId="op:move", blockRef={"kind": "calendar_block", "entityId": "calendar_block:20", "revision": 1}, startAt=base + timedelta(hours=10), endAt=base + timedelta(hours=11)),
                CalendarResizeBlockOperation(operationId="op:resize", blockRef={"kind": "calendar_block", "entityId": "calendar_block:21", "revision": 1}, endAt=base + timedelta(hours=4)),
                CalendarSplitBlockOperation(operationId="op:split", blockRef={"kind": "calendar_block", "entityId": "calendar_block:22", "revision": 1}, segments=[CalendarSegment(startAt=base + timedelta(hours=2), endAt=base + timedelta(hours=2, minutes=20)), CalendarSegment(startAt=base + timedelta(hours=2, minutes=40), endAt=base + timedelta(hours=3))]),
                CalendarDeleteBlockOperation(operationId="op:delete", blockRefs=[{"kind": "calendar_block", "entityId": "calendar_block:23", "revision": 1}]),
            ]
            before = len(session.exec(select(CalendarBlock)).all())
            NativeDomainMutationExecutor(session).apply(self.native(operations, "calendar"))
            session.commit()
            self.assertEqual(len(session.exec(select(CalendarBlock)).all()), before + 1)
            self.assertIsNone(session.get(CalendarBlock, 22))
            self.assertIsNone(session.get(CalendarBlock, 23))

    def test_all_recurrence_operation_kinds_use_series_occurrence_identity(self) -> None:
        with Session(self.engine) as session:
            session.add(StrategyNode(id=30, category_id=1, title="Workout"))
            session.add(StrategyNode(id=31, category_id=1, title="Unrelated"))
            session.add(RecurrenceSeries(id="series-a", task_id=30, category_id=1, frequency="weekly", days_of_week_json='["monday"]', start_date=date(2026, 7, 20), preferred_start_time="08:00", allowed_start_time="08:00", allowed_end_time="10:00", duration_minutes=60))
            session.commit()
            session.add(CalendarBlock(id=30, task_id=30, category_id=1, start_at=datetime(2026, 7, 20, 8), end_at=datetime(2026, 7, 20, 9), is_recurring=True, recurrence_series_id="series-a", recurrence_key="2026-07-20"))
            session.add(CalendarBlock(id=31, task_id=30, category_id=1, start_at=datetime(2026, 7, 27, 8), end_at=datetime(2026, 7, 27, 9), is_recurring=True, recurrence_series_id="series-a", recurrence_key="2026-07-27"))
            session.commit()
            operations = [
                RecurrenceCreateSeriesOperation(operationId="op:create-series", taskRef={"kind": "task", "entityId": "task:30", "revision": 1}, tempRef="temp:series", rule=RecurrenceRule(frequency="weekly", weekdays=["wednesday"], startDate="2026-07-22", occurrenceCount=2, preferredStartTime="09:00", durationMinutes=30)),
                RecurrenceUpdateSeriesOperation(operationId="op:update-series", seriesRef={"kind": "recurrence_series", "entityId": "recurrence_series:series-a", "revision": 1}, updates=RecurrenceRulePatch(preferredStartTime="07:00")),
                RecurrenceUpdateOccurrenceOperation(operationId="op:update-occ", occurrenceRef={"kind": "recurrence_occurrence", "entityId": "recurrence_occurrence:series-a:2026-07-20", "revision": 1}, updates=RecurrenceOccurrencePatch(durationMinutes=30)),
                RecurrenceCancelOccurrenceOperation(operationId="op:cancel-occ", occurrenceRef={"kind": "recurrence_occurrence", "entityId": "recurrence_occurrence:series-a:2026-07-27", "revision": 1}),
            ]
            NativeDomainMutationExecutor(
                session,
                approved_revisions={
                    ("calendar_block", "calendar_block:30"): 1,
                    ("calendar_block", "calendar_block:31"): 1,
                },
            ).apply(self.native(operations, "recurrence"))
            session.commit()
            self.assertEqual(session.get(RecurrenceSeries, "series-a").preferred_start_time, "07:00")
            updated_occurrence = session.exec(select(CalendarBlock).where(CalendarBlock.recurrence_series_id == "series-a", CalendarBlock.recurrence_key == "2026-07-20")).one()
            self.assertEqual(updated_occurrence.end_at, datetime(2026, 7, 20, 7, 30))
            self.assertEqual(len(session.exec(select(CalendarBlock).where(CalendarBlock.recurrence_series_id == "series-a", CalendarBlock.recurrence_key == "2026-07-27")).all()), 0)
            self.assertEqual(session.get(StrategyNode, 31).title, "Unrelated")

    def test_all_preference_operation_kinds_preserve_active_set(self) -> None:
        with Session(self.engine) as session:
            session.add(UserPreference(id="pref-a", preference_type="gap", value_json='{"kind":"scalar","value":10}'))
            session.add(UserPreference(id="pref-b", preference_type="window", value_json='{"kind":"scalar","value":"morning"}'))
            session.add(
                SchedulingConstraint(
                    id="constraint-pref-a",
                    source_preference_id="pref-a",
                    constraint_type="inter_task_gap",
                    active=True,
                )
            )
            session.commit()
            operations = [
                PreferenceUpsertOperation(operationId="op:upsert", preferenceKey="focus", value=PreferenceScalarValue(value=True)),
                PreferenceSupersedeOperation(operationId="op:supersede", preferenceRef={"kind": "preference", "entityId": "preference:pref-a", "revision": 1}, value=PreferenceScalarValue(value=20)),
                PreferenceRetireOperation(operationId="op:retire", preferenceRef={"kind": "preference", "entityId": "preference:pref-b", "revision": 1}),
            ]
            NativeDomainMutationExecutor(
                session,
                approved_revisions={
                    (
                        "scheduling_constraint",
                        "scheduling_constraint:constraint-pref-a",
                    ): 1,
                },
            ).apply(self.native(operations, "preferences"))
            session.commit()
            self.assertFalse(session.get(UserPreference, "pref-a").active)
            self.assertFalse(session.get(UserPreference, "pref-b").active)
            self.assertFalse(
                session.get(SchedulingConstraint, "constraint-pref-a").active
            )
            active = session.exec(select(UserPreference).where(UserPreference.active == True)).all()  # noqa: E712
            self.assertEqual({item.preference_type for item in active}, {"focus", "gap"})

    def test_multiple_operations_share_one_transaction_local_revision_chain(self) -> None:
        with Session(self.engine) as session:
            session.add(StrategyNode(id=50, category_id=1, title="Shared target"))
            session.commit()
            session.add(
                CalendarBlock(
                    id=50,
                    task_id=50,
                    category_id=1,
                    start_at=datetime(2026, 7, 20, 8),
                    end_at=datetime(2026, 7, 20, 9),
                )
            )
            session.commit()
            operations = [
                CalendarMoveBlockOperation(
                    operationId="op:move",
                    blockRef={"kind": "calendar_block", "entityId": "calendar_block:50", "revision": 1},
                    startAt=datetime(2026, 7, 20, 10, tzinfo=UTC),
                    endAt=datetime(2026, 7, 20, 11, tzinfo=UTC),
                ),
                CalendarResizeBlockOperation(
                    operationId="op:resize",
                    dependsOn=["op:move"],
                    blockRef={"kind": "calendar_block", "entityId": "calendar_block:50", "revision": 1},
                    endAt=datetime(2026, 7, 20, 12, tzinfo=UTC),
                ),
            ]
            NativeDomainMutationExecutor(session).apply(self.native(operations, "same-target"))
            session.commit()
            block = session.get(CalendarBlock, 50)
            self.assertEqual(block.revision, 3)
            self.assertEqual(block.end_at, datetime(2026, 7, 20, 12))

    def test_implied_descendant_mutation_requires_approved_revision(self) -> None:
        with Session(self.engine) as session:
            session.add(StrategyNode(id=60, category_id=1, title="Root"))
            session.add(StrategyNode(id=61, category_id=1, parent_id=60, title="Child"))
            session.commit()
            operation = TaskArchiveOperation(
                operationId="op:archive",
                taskRefs=[{"kind": "task", "entityId": "task:60", "revision": 1}],
            )
            with self.assertRaises(DomainMutationError):
                NativeDomainMutationExecutor(
                    session,
                    approved_revisions={("task", "task:61"): 2},
                ).apply(self.native([operation], "stale-descendant"))
            session.rollback()
            self.assertIsNone(session.get(StrategyNode, 60).tags)
            self.assertIsNone(session.get(StrategyNode, 61).tags)

    def test_cancelled_occurrence_survives_series_update_and_horizon_is_bounded(self) -> None:
        with Session(self.engine) as session:
            session.add(StrategyNode(id=70, category_id=1, title="Recurring"))
            session.add(
                RecurrenceSeries(
                    id="series-70",
                    task_id=70,
                    category_id=1,
                    frequency="weekly",
                    days_of_week_json='["monday"]',
                    start_date=date(2026, 7, 20),
                    end_date=date(2027, 7, 20),
                    preferred_start_time="08:00",
                    allowed_start_time="08:00",
                    allowed_end_time="10:00",
                    duration_minutes=60,
                )
            )
            session.commit()
            session.add(
                CalendarBlock(
                    id=70,
                    task_id=70,
                    category_id=1,
                    start_at=datetime(2026, 7, 20, 8),
                    end_at=datetime(2026, 7, 20, 9),
                    is_recurring=True,
                    recurrence_series_id="series-70",
                    recurrence_key="2026-07-20",
                )
            )
            session.commit()
            operations = [
                RecurrenceCancelOccurrenceOperation(
                    operationId="op:cancel",
                    occurrenceRef={"kind": "recurrence_occurrence", "entityId": "recurrence_occurrence:series-70:2026-07-20", "revision": 1},
                ),
                RecurrenceUpdateSeriesOperation(
                    operationId="op:update",
                    dependsOn=["op:cancel"],
                    seriesRef={"kind": "recurrence_series", "entityId": "recurrence_series:series-70", "revision": 1},
                    updates=RecurrenceRulePatch(preferredStartTime="09:00"),
                ),
            ]
            NativeDomainMutationExecutor(session).apply(self.native(operations, "exceptions"))
            session.commit()
            self.assertIsNotNone(
                session.get(RecurrenceOccurrenceException, "series-70:2026-07-20")
            )
            occurrences = session.exec(
                select(CalendarBlock).where(
                    CalendarBlock.recurrence_series_id == "series-70"
                )
            ).all()
            self.assertTrue(all(item.recurrence_key != "2026-07-20" for item in occurrences))
            self.assertLessEqual(len(occurrences), 9)


if __name__ == "__main__":
    unittest.main()
