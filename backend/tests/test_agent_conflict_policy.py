from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, SQLModel, create_engine

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries
from app.services.agent_kernel.conflict_policy import TypedConflictPolicy
from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    ChangeSet,
    Constraint,
    ConstraintScope,
    EntityRef,
    ExactTimeConstraintValue,
    Provenance,
    RecurrenceCreateSeriesOperation,
    RecurrenceRule,
    TaskCreateOperation,
    TaskFields,
    TemporalWindowConstraintValue,
)
from app.services.agent_kernel.simulation import ChangeSetProjector, SimulationSnapshotLoader


UTC = timezone.utc


def draft(start_hour: int = 9, start_minute: int = 30, duration_minutes: int = 60) -> ChangeSet:
    start = datetime(2026, 7, 20, start_hour, start_minute, tzinfo=UTC)
    return ChangeSet(
        change_set_id="changeset:conflict",
        goal_id="goal:conflict",
        operations=[
            CalendarCreateBlockOperation(
                operation_id="op:incoming",
                task_ref=EntityRef(kind="task", entity_id="task:1"),
                start_at=start,
                end_at=start + timedelta(minutes=duration_minutes),
                temp_ref="temp:incoming",
            )
        ],
        idempotency_key="idem:conflict",
        correlation_id="corr:conflict",
        causation_id="turn:conflict",
    )


class AgentConflictPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(Category(id=1, name="Work"))
            session.add(StrategyNode(id=1, category_id=1, title="Interview", priority="high"))
            session.add(StrategyNode(id=2, category_id=1, title="Team call", priority="high"))
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

    def _evaluate(self, change_set: ChangeSet, *, goal_constraints=None, anchored_operation_ids=None):
        with Session(self.engine) as session:
            loaded = SimulationSnapshotLoader(session).load(change_set)
            projection = ChangeSetProjector().project(change_set, loaded.world)
            return TypedConflictPolicy().evaluate(
                change_set,
                projection.world,
                loaded.snapshot,
                goal_revision=3,
                goal_constraints=goal_constraints or [],
                anchored_operation_ids=anchored_operation_ids or set(),
                base_world=loaded.world,
            )

    def test_protected_blocker_yields_complete_model_repairable_alternative(self) -> None:
        graph = self._evaluate(draft())
        self.assertEqual(graph.status, "resolved")
        self.assertEqual(len(graph.conflicts), 1)
        item = graph.conflicts[0]
        self.assertEqual(item.policy_recommendation, "move_incoming")
        self.assertEqual(item.blockers[0].protection, "protected")
        self.assertEqual(item.alternatives[0].feasibility, "feasible")
        self.assertEqual(item.alternatives[0].change_set_delta.expected_change_set_revision, 1)

    def test_equal_unprotected_commitments_produce_one_precise_decision(self) -> None:
        with Session(self.engine) as session:
            block = session.get(CalendarBlock, 1)
            block.is_pinned = False
            block.is_protected = False
            session.add(block)
            session.commit()
        graph = self._evaluate(draft())
        self.assertEqual(graph.status, "needs_user_decision")
        self.assertIsNotNone(graph.decision_request)
        self.assertEqual(len(graph.decision_request.options), 2)
        self.assertTrue(all(option.target == "change_set" for option in graph.decision_request.options))
        labels = [option.label for option in graph.decision_request.options]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(any("Interview" in label for label in labels), labels)
        self.assertTrue(any("Team call" in label for label in labels), labels)
        self.assertFalse(any("complete feasible resolution" in label for label in labels))
        self.assertIn("Team call", graph.decision_request.question)
        self.assertIn("July 20, 2026", graph.decision_request.question)
        self.assertEqual(len(graph.decision_request.conflicts), 1)
        detail = graph.decision_request.conflicts[0]
        self.assertEqual(detail.incoming_task_title, "Interview")
        self.assertEqual(detail.blocker_task_title, "Team call")
        self.assertEqual(detail.requested_start_at, datetime(2026, 7, 20, 9, 30, tzinfo=UTC))
        self.assertEqual(detail.blocker_start_at, datetime(2026, 7, 20, 9, 0, tzinfo=UTC))

    def test_mixed_timezone_overlap_uses_incoming_local_timezone_everywhere(self) -> None:
        with Session(self.engine) as session:
            block = session.get(CalendarBlock, 1)
            block.start_at = datetime(2026, 8, 16, 17, 0)
            block.end_at = datetime(2026, 8, 16, 18, 0)
            block.is_pinned = False
            block.is_protected = False
            session.add(block)
            session.commit()

        local_zone = ZoneInfo("America/Vancouver")
        local_start = datetime(2026, 8, 16, 8, 0, tzinfo=local_zone)
        change_set = ChangeSet(
            change_set_id="changeset:mixed-timezone-conflict",
            goal_id="goal:mixed-timezone-conflict",
            operations=[
                CalendarCreateBlockOperation(
                    operation_id="op:incoming-mixed-timezone",
                    task_ref=EntityRef(kind="task", entity_id="task:1"),
                    start_at=local_start,
                    end_at=local_start + timedelta(hours=3),
                    temp_ref="temp:incoming-mixed-timezone",
                )
            ],
            idempotency_key="idem:mixed-timezone-conflict",
            correlation_id="corr:mixed-timezone-conflict",
            causation_id="turn:mixed-timezone-conflict",
        )

        graph = self._evaluate(change_set)

        self.assertEqual(graph.status, "needs_user_decision")
        overlap = graph.conflicts[0].overlap_intervals[0]
        self.assertEqual(overlap.start_at, datetime(2026, 8, 16, 10, 0, tzinfo=local_zone))
        self.assertEqual(overlap.end_at, datetime(2026, 8, 16, 11, 0, tzinfo=local_zone))
        self.assertEqual(overlap.timezone, "America/Vancouver")
        self.assertIsNotNone(graph.decision_request)
        self.assertIn("10:00 AM", graph.decision_request.question)
        self.assertIn("11:00 AM", graph.decision_request.question)
        self.assertNotIn("5:00 PM", graph.decision_request.question)

    def test_explicit_current_turn_forbidden_window_is_a_hard_blocker_without_task(self) -> None:
        unavailable = Constraint(
            constraint_id="constraint:unavailable",
            kind="temporal_window",
            strength="hard",
            scope=ConstraintScope(kind="goal"),
            value=TemporalWindowConstraintValue(
                disposition="forbidden",
                start_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            ),
            provenance=Provenance(source_kind="user", source_ref="turn:current"),
        )
        graph = self._evaluate(draft(10, 0), goal_constraints=[unavailable])
        constraint_conflicts = [
            item for item in graph.conflicts if item.kind == "constraint_violation"
        ]
        self.assertEqual(len(constraint_conflicts), 1)
        blocker = constraint_conflicts[0].blockers[0]
        self.assertEqual(blocker.source, "goal_constraint")
        self.assertIsNotNone(blocker.entity_ref)
        self.assertEqual(blocker.entity_ref.title, "Required scheduling window")
        self.assertEqual(blocker.label, "Required scheduling window")
        self.assertEqual(blocker.movability, "fixed")

    def test_exact_time_constraint_cannot_be_silently_ignored(self) -> None:
        exact = Constraint(
            constraint_id="constraint:exact",
            kind="exact_time",
            strength="hard",
            scope=ConstraintScope(kind="operation_set", operation_ids=["op:incoming"]),
            value=ExactTimeConstraintValue(
                start_at=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
                duration_minutes=60,
            ),
            provenance=Provenance(source_kind="user", source_ref="turn:current"),
        )
        graph = self._evaluate(draft(), goal_constraints=[exact])
        self.assertTrue(any(item.kind == "constraint_violation" for item in graph.conflicts))
        self.assertNotEqual(graph.status, "clear")

    def test_recurring_forbidden_window_respects_time_and_category_scope(self) -> None:
        with Session(self.engine) as session:
            session.add_all(
                [
                    SchedulingConstraint(
                        id="global-after-eleven",
                        constraint_type="forbidden_window",
                        target_type="global",
                        hard=True,
                        active=True,
                        value_json=(
                            '{"startTime":"23:00","endTime":"23:59",'
                            '"daysOfWeek":[0,1,2,3,4,5,6]}'
                        ),
                    ),
                    SchedulingConstraint(
                        id="other-category-after-four",
                        constraint_type="forbidden_window",
                        target_type="category",
                        target_id="2",
                        hard=True,
                        active=True,
                        value_json=(
                            '{"startTime":"16:00","endTime":"20:00",'
                            '"daysOfWeek":["monday"]}'
                        ),
                    ),
                ]
            )
            session.commit()

        graph = self._evaluate(draft(17, 0, duration_minutes=30))

        self.assertEqual(graph.status, "clear")
        self.assertEqual(graph.conflicts, [])

    def test_matching_recurring_forbidden_window_blocks_only_the_matching_time(self) -> None:
        with Session(self.engine) as session:
            session.add(
                SchedulingConstraint(
                    id="work-evening",
                    constraint_type="forbidden_window",
                    target_type="category",
                    target_id="1",
                    hard=True,
                    active=True,
                    value_json=(
                        '{"startTime":"17:00","endTime":"20:00",'
                        '"daysOfWeek":["monday"]}'
                    ),
                )
            )
            session.commit()

        graph = self._evaluate(draft(17, 0, duration_minutes=30))

        self.assertEqual(graph.status, "resolved")
        conflicts = [
            item for item in graph.conflicts if item.kind == "constraint_violation"
        ]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].blockers[0].blocker_id, "work-evening")

    def test_saved_wake_time_conflict_names_the_rule_and_exact_overlap(self) -> None:
        with Session(self.engine) as session:
            session.add(
                SchedulingConstraint(
                    id="sleep-until-seven",
                    constraint_type="forbidden_window",
                    target_type="global",
                    hard=True,
                    active=True,
                    description="Treat 11:00 PM to 7:00 AM as unavailable sleep time.",
                    value_json=(
                        '{"startTime":"23:00","endTime":"07:00",'
                        '"daysOfWeek":[0,1,2,3,4,5,6]}'
                    ),
                )
            )
            session.commit()

        graph = self._evaluate(draft(6, 30, duration_minutes=30))

        sleep_conflict = next(
            item
            for item in graph.conflicts
            if any(blocker.blocker_id == "sleep-until-seven" for blocker in item.blockers)
        )
        blocker = sleep_conflict.blockers[0]
        self.assertEqual(
            blocker.label,
            "Treat 11:00 PM to 7:00 AM as unavailable sleep time.",
        )
        self.assertEqual(blocker.start_at, datetime(2026, 7, 20, 6, 30, tzinfo=UTC))
        self.assertEqual(blocker.end_at, datetime(2026, 7, 20, 7, 0, tzinfo=UTC))
        self.assertEqual(sleep_conflict.incoming_label, "Interview")
        self.assertEqual(sleep_conflict.requested_start_at, datetime(2026, 7, 20, 6, 30, tzinfo=UTC))

    def test_current_exact_time_is_a_one_off_exception_to_saved_wake_time(self) -> None:
        with Session(self.engine) as session:
            session.add(
                SchedulingConstraint(
                    id="sleep-until-seven-exact-override",
                    constraint_type="forbidden_window",
                    target_type="global",
                    hard=True,
                    active=True,
                    description="Treat 11:00 PM to 7:00 AM as unavailable sleep time.",
                    value_json=(
                        '{"startTime":"23:00","endTime":"07:00",'
                        '"daysOfWeek":[0,1,2,3,4,5,6]}'
                    ),
                )
            )
            session.commit()

        change_set = draft(6, 30, duration_minutes=30)
        change_set.constraints = [
            Constraint(
                constraint_id="constraint:exact-wake-exception",
                kind="exact_time",
                strength="hard",
                scope=ConstraintScope(
                    kind="operation_set",
                    operation_ids=["op:incoming"],
                ),
                value=ExactTimeConstraintValue(
                    start_at=datetime(2026, 7, 20, 6, 30, tzinfo=UTC),
                    duration_minutes=30,
                ),
                provenance=Provenance(
                    source_kind="user",
                    source_ref="turn:exact-reschedule",
                ),
            )
        ]

        graph = self._evaluate(change_set)

        self.assertEqual(graph.status, "clear")
        self.assertEqual(graph.conflicts, [])

    def test_malformed_recurring_weekdays_fail_closed(self) -> None:
        with Session(self.engine) as session:
            session.add(
                SchedulingConstraint(
                    id="malformed-weekday",
                    constraint_type="forbidden_window",
                    target_type="global",
                    hard=True,
                    active=True,
                    value_json=(
                        '{"startTime":"09:00","endTime":"11:00",'
                        '"daysOfWeek":["Mon"]}'
                    ),
                )
            )
            session.commit()

        graph = self._evaluate(draft(9, 30, duration_minutes=30))

        self.assertTrue(
            any(
                item.blockers[0].blocker_id == "malformed-weekday"
                for item in graph.conflicts
                if item.kind == "constraint_violation"
            )
        )

    def test_unknown_active_hard_constraint_scope_fails_closed(self) -> None:
        with Session(self.engine) as session:
            session.add(
                SchedulingConstraint(
                    id="unknown-hard-scope",
                    constraint_type="unsupported_policy",
                    target_type="future_scope",
                    target_id="opaque",
                    hard=True,
                    active=True,
                    value_json="{}",
                )
            )
            session.commit()

        graph = self._evaluate(draft(9, 30, duration_minutes=30))

        self.assertTrue(
            any(
                item.blockers[0].blocker_id == "unknown-hard-scope"
                for item in graph.conflicts
                if item.kind == "constraint_violation"
            )
        )

    def test_hard_preference_backed_by_constraint_is_not_enforced_twice(self) -> None:
        with Session(self.engine) as session:
            preference = UserPreference(
                id="sleep-preference",
                domain="scheduling",
                preference_type="availability",
                scope_type="global",
                strength="hard",
                active=True,
                value_json=(
                    '{"rule":"forbidden_window","startTime":"23:00",'
                    '"endTime":"23:59"}'
                ),
            )
            session.add(preference)
            session.add(
                SchedulingConstraint(
                    id="sleep-constraint",
                    constraint_type="forbidden_window",
                    target_type="global",
                    hard=True,
                    active=True,
                    source_preference_id=preference.id,
                    value_json=(
                        '{"rule":"forbidden_window","startTime":"23:00",'
                        '"endTime":"23:59"}'
                    ),
                )
            )
            session.commit()

        allowed = self._evaluate(draft(17, 0, duration_minutes=30))
        blocked = self._evaluate(draft(23, 15, duration_minutes=30))

        self.assertEqual(allowed.status, "clear")
        preference_conflicts = [
            item
            for item in blocked.conflicts
            if item.kind == "constraint_violation"
        ]
        self.assertEqual(len(preference_conflicts), 1)
        self.assertEqual(
            preference_conflicts[0].blockers[0].blocker_id,
            "sleep-constraint",
        )

    def test_anchored_incoming_moves_only_the_typed_recurrence_occurrence(self) -> None:
        with Session(self.engine) as session:
            session.add(
                RecurrenceSeries(
                    id="workout-series",
                    task_id=2,
                    category_id=1,
                    frequency="weekly",
                    interval=1,
                    days_of_week_json='["monday", "wednesday"]',
                    start_date=datetime(2026, 7, 1).date(),
                    preferred_start_time="08:00",
                    allowed_start_time="06:00",
                    allowed_end_time="12:00",
                    duration_minutes=60,
                    strict=False,
                    materialized_through=datetime(2026, 7, 20).date(),
                )
            )
            block = session.get(CalendarBlock, 1)
            block.is_pinned = False
            block.is_protected = False
            block.is_recurring = True
            block.recurrence_series_id = "workout-series"
            block.recurrence_key = "2026-07-20"
            session.add(block)
            session.commit()
        graph = self._evaluate(draft(), anchored_operation_ids={"op:incoming"})
        item = graph.conflicts[0]
        self.assertEqual(item.policy_recommendation, "move_blocker")
        edits = item.alternatives[-1].change_set_delta.edits
        recurrence_edits = [
            edit for edit in edits if edit.kind == "operation.add"
        ]
        self.assertEqual(recurrence_edits[0].operation.kind, "recurrence.update_occurrence")
        self.assertEqual(
            recurrence_edits[0].operation.occurrence_ref.entity_id,
            "recurrence_occurrence:workout-series:2026-07-20",
        )

    def test_multi_blocker_repair_reserves_each_generated_placement(self) -> None:
        with Session(self.engine) as session:
            session.add(
                RecurrenceSeries(
                    id="flex-series",
                    task_id=2,
                    category_id=1,
                    frequency="weekly",
                    interval=1,
                    days_of_week_json='["monday"]',
                    start_date=datetime(2026, 7, 1).date(),
                    preferred_start_time="09:00",
                    allowed_start_time="06:00",
                    allowed_end_time="12:00",
                    duration_minutes=60,
                    strict=False,
                    materialized_through=datetime(2026, 7, 20).date(),
                )
            )
            first = session.get(CalendarBlock, 1)
            first.is_pinned = False
            first.is_protected = False
            first.is_recurring = True
            first.recurrence_series_id = "flex-series"
            first.recurrence_key = "2026-07-20-a"
            session.add(first)
            session.add(CalendarBlock(
                id=2,
                task_id=2,
                category_id=1,
                start_at=datetime(2026, 7, 20, 10, 0),
                end_at=datetime(2026, 7, 20, 11, 0),
                is_recurring=True,
                recurrence_series_id="flex-series",
                recurrence_key="2026-07-20-b",
            ))
            session.commit()
        graph = self._evaluate(
            draft(9, 0, duration_minutes=120),
            anchored_operation_ids={"op:incoming"},
        )
        repair = next(
            alternative for alternative in graph.conflicts[0].alternatives
            if alternative.tradeoff == "moves_existing" and alternative.feasibility == "feasible"
        )
        placements = [
            (edit.operation.updates.start_at, edit.operation.updates.end_at)
            for edit in repair.change_set_delta.edits
        ]
        self.assertEqual(len(placements), 2)
        self.assertNotEqual(placements[0], placements[1])
        self.assertLessEqual(placements[0][1], placements[1][0])

    def test_recurring_conflict_decision_has_complete_typed_before_and_after_deltas(self) -> None:
        with Session(self.engine) as session:
            session.add(
                RecurrenceSeries(
                    id="workout-series",
                    task_id=2,
                    category_id=1,
                    frequency="weekly",
                    interval=1,
                    days_of_week_json='["monday"]',
                    start_date=datetime(2026, 7, 20).date(),
                    end_date=datetime(2026, 10, 14).date(),
                    preferred_start_time="09:00",
                    allowed_start_time="06:00",
                    allowed_end_time="23:00",
                    duration_minutes=60,
                    timezone="America/Vancouver",
                    strict=False,
                    gap_minutes=15,
                    materialized_through=datetime(2026, 10, 14).date(),
                )
            )
            block = session.get(CalendarBlock, 1)
            block.is_pinned = False
            block.is_protected = False
            block.is_recurring = True
            block.recurrence_series_id = "workout-series"
            block.recurrence_key = "2026-07-20"
            session.add(block)
            cursor = datetime(2026, 7, 21).date()
            next_id = 2
            while cursor <= datetime(2026, 10, 14).date():
                if cursor.weekday() in {0, 2}:
                    session.add(
                        CalendarBlock(
                            id=next_id,
                            task_id=2,
                            category_id=1,
                            start_at=datetime.combine(cursor, datetime.min.time()).replace(
                                hour=9
                            ),
                            end_at=datetime.combine(cursor, datetime.min.time()).replace(
                                hour=10
                            ),
                            is_recurring=True,
                            recurrence_series_id="workout-series",
                            recurrence_key=cursor.isoformat(),
                        )
                    )
                    next_id += 1
                cursor += timedelta(days=1)
            session.commit()
        temp_task = EntityRef(kind="temporary", entity_id="temp:team", target_kind="task")
        recurring = ChangeSet(
            change_set_id="changeset:recurring-conflict",
            goal_id="goal:recurring-conflict",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create-team",
                    atomic_group_id="atomic:team",
                    title="Team call",
                    workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                    temp_ref=temp_task,
                    fields=TaskFields(estimate_minutes=60),
                ),
                RecurrenceCreateSeriesOperation(
                    operation_id="op:create-series",
                    depends_on=["op:create-team"],
                    atomic_group_id="atomic:team",
                    task_ref=temp_task,
                    rule=RecurrenceRule(
                        frequency="weekly",
                        weekdays=["monday", "tuesday", "wednesday", "thursday", "friday"],
                        start_date=datetime(2026, 7, 20).date(),
                        end_date=datetime(2026, 10, 14).date(),
                        timezone="America/Vancouver",
                        preferred_start_time="09:30",
                        duration_minutes=60,
                    ),
                ),
            ],
            idempotency_key="idem:recurring-conflict",
            correlation_id="corr:recurring-conflict",
            causation_id="turn:recurring-conflict",
        )
        graph = self._evaluate(recurring)

        self.assertEqual(graph.status, "needs_user_decision")
        self.assertIsNotNone(graph.decision_request)
        options = graph.decision_request.options
        self.assertEqual(
            len(options),
            2,
            [
                (alternative.placement_relation, alternative.summary)
                for alternative in graph.conflicts[0].alternatives
            ],
        )
        self.assertIn("after the incoming", options[0].label)
        self.assertIn("before the incoming", options[1].label)
        self.assertEqual(
            [option.placement_relation for option in options],
            ["after_incoming", "before_incoming"],
        )
        placements = [
            [
                (
                    edit.operation.updates.start_at,
                    edit.operation.updates.end_at,
                )
                for edit in option.delta.edits
            ]
            for option in options
        ]
        self.assertEqual([len(items) for items in placements], [26, 26])
        self.assertEqual(
            [items[0] for items in placements],
            [
                (
                    datetime(2026, 7, 20, 10, 45, tzinfo=ZoneInfo("America/Vancouver")),
                    datetime(2026, 7, 20, 11, 45, tzinfo=ZoneInfo("America/Vancouver")),
                ),
                (
                    datetime(2026, 7, 20, 8, 15, tzinfo=ZoneInfo("America/Vancouver")),
                    datetime(2026, 7, 20, 9, 15, tzinfo=ZoneInfo("America/Vancouver")),
                ),
            ],
        )

    def test_long_flexible_incoming_can_split_around_fixed_commitment_with_duration_conserved(self) -> None:
        graph = self._evaluate(draft(8, 0, duration_minutes=180))
        split = next(
            alternative
            for alternative in graph.conflicts[0].alternatives
            if alternative.tradeoff == "splits_work"
        )
        operation = split.change_set_delta.edits[0].operation
        self.assertEqual(operation.kind, "calendar.split_block")
        duration = sum(
            int((segment.end_at - segment.start_at).total_seconds() // 60)
            for segment in operation.segments
        )
        self.assertEqual(duration, 180)


if __name__ == "__main__":
    unittest.main()
