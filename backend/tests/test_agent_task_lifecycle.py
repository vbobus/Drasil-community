from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from app.db import database as _database  # noqa: F401  # register complete metadata
from app.models.agent_change_set import AgentChangeSetRecord
from app.models.agent_goal import AgentGoalStateRecord
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import PreferenceLink, SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceOccurrenceException, RecurrenceSeries
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.state_store import GoalStateStore
from app.services.task_lifecycle import (
    allocate_strategy_node_id,
    delete_task_dependents,
    reconcile_reused_task_identities,
)


class AgentTaskLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    @staticmethod
    def _task(session: Session) -> tuple[Category, StrategyNode]:
        category = Category(name="Work")
        session.add(category)
        session.flush()
        task = StrategyNode(category_id=int(category.id), title="Task")
        session.add(task)
        session.flush()
        return category, task

    def test_allocator_respects_retained_task_memory_identities(self) -> None:
        with Session(self.engine) as session:
            session.add(UserPreference(scope_type="task", scope_id="99"))
            session.add(
                SchedulingConstraint(target_type="task", target_id="task:101")
            )
            session.add(
                PreferenceLink(
                    preference_id="preference:legacy",
                    target_type="task",
                    target_id="105",
                )
            )
            session.flush()
            self.assertEqual(allocate_strategy_node_id(session), 106)

    def test_task_cleanup_removes_exceptions_and_preference_owned_dependents(self) -> None:
        with Session(self.engine) as session:
            category, task = self._task(session)
            series = RecurrenceSeries(
                id="series:1",
                task_id=int(task.id),
                category_id=int(category.id),
                frequency="weekly",
                start_date=date(2026, 7, 20),
                preferred_start_time="10:00",
                allowed_start_time="10:00",
                allowed_end_time="11:00",
                duration_minutes=60,
            )
            preference = UserPreference(scope_type="task", scope_id=str(task.id))
            session.add(series)
            session.add(preference)
            session.flush()
            exception = RecurrenceOccurrenceException(
                id="series:1:2026-07-20",
                series_id=series.id,
                recurrence_key="2026-07-20",
                disposition="cancelled",
            )
            constraint = SchedulingConstraint(
                target_type="global",
                source_preference_id=preference.id,
            )
            link = PreferenceLink(
                preference_id=preference.id,
                target_type="workspace",
                target_id=str(category.id),
            )
            session.add(exception)
            session.add(constraint)
            session.add(link)
            session.commit()

            result = delete_task_dependents(session, [int(task.id)])
            session.delete(task)
            session.commit()

            self.assertEqual(result["recurrence_exceptions"], 1)
            self.assertIsNone(session.get(RecurrenceOccurrenceException, exception.id))
            self.assertFalse(session.get(UserPreference, preference.id).active)
            self.assertFalse(session.get(SchedulingConstraint, constraint.id).active)
            self.assertFalse(session.get(PreferenceLink, link.id).active)

    def test_reconciliation_removes_orphan_recurring_block_and_stale_memory(self) -> None:
        with Session(self.engine) as session:
            category, task = self._task(session)
            stale_time = task.created_at - timedelta(minutes=5)
            preference = UserPreference(
                scope_type="task",
                scope_id=str(task.id),
                created_at=stale_time,
            )
            block = CalendarBlock(
                task_id=int(task.id),
                category_id=int(category.id),
                start_at=datetime(2026, 7, 20, 10, 0),
                end_at=datetime(2026, 7, 20, 11, 0),
                is_recurring=True,
                recurrence_series_id="series:missing",
                recurrence_key="2026-07-20",
            )
            session.add(preference)
            session.add(block)
            session.commit()
            block_id = int(block.id)

            result = reconcile_reused_task_identities(session)
            session.commit()

            self.assertEqual(result["calendar_blocks"], 1)
            self.assertIsNone(session.get(CalendarBlock, block_id))
            self.assertFalse(session.get(UserPreference, preference.id).active)

    def test_session_cleanup_nulls_retained_recurrence_provenance(self) -> None:
        with Session(self.engine) as session:
            category, task = self._task(session)
            session.add(StrategistChatSession(id="session:1"))
            session.add(
                AgentGoalStateRecord(
                    goal_id="goal:1",
                    session_id="session:1",
                    status="ready_for_review",
                    revision=1,
                    active_session_id="session:1",
                    correlation_id="corr:1",
                    causation_id="turn:1",
                    state_json="{}",
                )
            )
            session.flush()
            session.add(
                AgentChangeSetRecord(
                    change_set_id="changeset:1",
                    goal_id="goal:1",
                    session_id="session:1",
                    status="applied",
                    revision=1,
                    idempotency_key="changeset:1",
                    correlation_id="corr:1",
                    causation_id="turn:1",
                    snapshot_json="{}",
                )
            )
            session.add(
                RecurrenceSeries(
                    id="series:1",
                    task_id=int(task.id),
                    category_id=int(category.id),
                    frequency="weekly",
                    start_date=date(2026, 7, 20),
                    preferred_start_time="10:00",
                    allowed_start_time="10:00",
                    allowed_end_time="11:00",
                    duration_minutes=60,
                    source_agent_change_set_id="changeset:1",
                )
            )
            session.commit()

            GoalStateStore(session).delete_session_history("session:1")
            session.commit()

            retained = session.get(RecurrenceSeries, "series:1")
            self.assertIsNotNone(retained)
            self.assertIsNone(retained.source_agent_change_set_id)


if __name__ == "__main__":
    unittest.main()
