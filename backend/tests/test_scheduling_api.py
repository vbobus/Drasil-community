import unittest
import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.db.database import (
    assert_test_database_mutation_allowed,
    create_db_and_tables,
    engine,
)
from app.db.seed import seed_default_categories
from app.main import app
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries


class SchedulingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_test_database_mutation_allowed()
        create_db_and_tables()
        with Session(engine) as session:
            session.exec(delete(CalendarBlock))
            session.exec(delete(RecurrenceSeries))
            session.exec(delete(SchedulingConstraint))
            session.exec(delete(UserPreference))
            session.commit()
        seed_default_categories()
        self.client = TestClient(app)

    def _create_category(self, session: Session, name: str, accent_color: str) -> Category:
        category = Category(
            name=name,
            accent_color=accent_color,
            icon_slug="briefcase",
            sort_order=950,
        )
        session.add(category)
        session.commit()
        session.refresh(category)
        return category

    def _create_task(
        self,
        session: Session,
        category: Category,
        title: str,
        *,
        estimate: int | None = 30,
        parent_id: int | None = None,
        sort_order: int = 0,
        emoji: str = "T",
    ) -> StrategyNode:
        node = StrategyNode(
            category_id=category.id,
            title=title,
            node_type="task",
            status="todo",
            priority="medium",
            parent_id=parent_id,
            time_estimate_minutes=estimate,
            emoji=emoji,
            sort_order=sort_order,
        )
        session.add(node)
        session.commit()
        session.refresh(node)
        return node

    def _calendar_block_count(self) -> int:
        with Session(engine) as session:
            return len(session.exec(select(CalendarBlock)).all())

    def test_auto_draft_places_selected_tasks_without_persisting_calendar_blocks(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Scheduling API Work", "#1f6feb")
            parent = self._create_task(
                session,
                category,
                "Launch case study",
                estimate=None,
                emoji="L",
            )
            first_child = self._create_task(
                session,
                category,
                "Draft outline",
                estimate=45,
                parent_id=parent.id,
                sort_order=0,
                emoji="D",
            )
            second_child = self._create_task(
                session,
                category,
                "Review visuals",
                estimate=30,
                parent_id=parent.id,
                sort_order=1,
                emoji="R",
            )
            task_id = parent.id
            first_child_id = first_child.id
            second_child_id = second_child.id
            category_id = category.id

        before_count = self._calendar_block_count()
        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [task_id],
                "windowStart": "2026-06-18T09:00:00",
                "windowEnd": "2026-06-18T12:00:00",
                "slotMinutes": 15,
                "fallbackDurationMinutes": 30,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._calendar_block_count(), before_count)
        payload = response.json()
        self.assertEqual(payload["diagnostics"]["scheduledTaskCount"], 1)
        self.assertEqual(payload["diagnostics"]["conflictCount"], 0)
        self.assertEqual(payload["conflicts"], [])
        self.assertEqual(len(payload["draftBlocks"]), 1)
        draft = payload["draftBlocks"][0]
        self.assertEqual(draft["taskId"], task_id)
        self.assertEqual(draft["categoryId"], category_id)
        self.assertEqual(draft["title"], "Launch case study")
        self.assertEqual(draft["emoji"], "L")
        self.assertEqual(draft["categoryName"], "Scheduling API Work")
        self.assertEqual(draft["categoryColor"], "#1f6feb")
        self.assertEqual(draft["variant"], "draft")
        self.assertEqual(draft["completionStatus"], "not_started")
        self.assertEqual(draft["startAt"], "2026-06-18T09:00:00")
        self.assertEqual(draft["endAt"], "2026-06-18T10:15:00")
        self.assertEqual(draft["renderedChildTaskIds"], [first_child_id, second_child_id])
        self.assertEqual(payload["diagnostics"]["fallbackDurationTaskIds"], [])

    def test_auto_draft_returns_conflict_and_alternative_for_occupied_window(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Scheduling API Conflict", "#6d5dfc")
            task = self._create_task(session, category, "Write launch email", estimate=60)
            blocker = self._create_task(session, category, "Existing focus block", estimate=60)
            block = CalendarBlock(
                task_id=blocker.id,
                category_id=category.id,
                start_at=datetime(2026, 6, 18, 9, 0),
                end_at=datetime(2026, 6, 18, 10, 0),
                variant="scheduled",
                is_protected=True,
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            task_id = task.id
            block_id = block.id

        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [task_id],
                "windowStart": "2026-06-18T09:00:00",
                "windowEnd": "2026-06-18T10:00:00",
                "slotMinutes": 15,
                "fallbackDurationMinutes": 30,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["draftBlocks"], [])
        self.assertEqual(payload["diagnostics"]["solverStatus"], "conflict")
        self.assertEqual(payload["diagnostics"]["scheduledTaskCount"], 0)
        self.assertEqual(payload["diagnostics"]["conflictCount"], 1)
        self.assertEqual(payload["diagnostics"]["occupiedBlockCount"], 1)
        conflict = payload["conflicts"][0]
        self.assertEqual(conflict["taskId"], task_id)
        self.assertEqual(conflict["type"], "occupied_slot")
        self.assertIn(block_id, conflict["conflictingBlockIds"])
        self.assertIn("No available 60 minute slot", conflict["detail"])
        self.assertEqual(conflict["alternatives"][0]["startAt"], "2026-06-18T10:00:00")
        self.assertEqual(conflict["alternatives"][0]["endAt"], "2026-06-18T11:00:00")

    def test_auto_draft_reports_fallback_duration_and_deduplicates_task_ids(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Scheduling API Fallback", "#0f766e")
            task = self._create_task(session, category, "Untimed admin", estimate=None)
            task_id = task.id

        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [task_id, task_id],
                "windowStart": "2026-06-18T13:00:00",
                "windowEnd": "2026-06-18T14:00:00",
                "slotMinutes": 15,
                "fallbackDurationMinutes": 25,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["draftBlocks"]), 1)
        self.assertEqual(payload["draftBlocks"][0]["endAt"], "2026-06-18T13:25:00")
        self.assertEqual(payload["diagnostics"]["fallbackDurationTaskIds"], [task_id])

    def test_auto_draft_skips_selected_tasks_that_already_have_calendar_blocks(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Scheduling API Commitments", "#4f46e5")
            scheduled_task = self._create_task(
                session,
                category,
                "Already placed task",
                estimate=60,
                emoji="A",
            )
            unscheduled_task = self._create_task(
                session,
                category,
                "Needs a draft",
                estimate=30,
                emoji="N",
            )
            block = CalendarBlock(
                task_id=scheduled_task.id,
                category_id=category.id,
                start_at=datetime(2026, 6, 22, 9, 0),
                end_at=datetime(2026, 6, 22, 10, 0),
                variant="scheduled",
            )
            session.add(block)
            session.commit()
            scheduled_task_id = scheduled_task.id
            unscheduled_task_id = unscheduled_task.id

        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [scheduled_task_id, unscheduled_task_id],
                "windowStart": "2026-06-22T09:00:00",
                "windowEnd": "2026-06-22T12:00:00",
                "slotMinutes": 15,
                "fallbackDurationMinutes": 30,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["diagnostics"]["alreadyScheduledTaskIds"], [scheduled_task_id])
        self.assertEqual(payload["diagnostics"]["scheduledTaskCount"], 1)
        self.assertEqual(len(payload["draftBlocks"]), 1)
        self.assertEqual(payload["draftBlocks"][0]["taskId"], unscheduled_task_id)
        self.assertEqual(payload["draftBlocks"][0]["startAt"], "2026-06-22T10:00:00")
        self.assertEqual(payload["draftBlocks"][0]["endAt"], "2026-06-22T10:30:00")

    def test_auto_draft_respects_hard_preference_constraints(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Scheduling API Memory", "#7c3aed")
            task = self._create_task(session, category, "Plan memory rollout", estimate=30)
            session.add(
                SchedulingConstraint(
                    constraint_type="forbidden_window",
                    target_type="global",
                    hard=True,
                    description="Morning school run is unavailable.",
                    value_json=json.dumps(
                        {
                            "startAt": "2026-07-08T09:00:00",
                            "endAt": "2026-07-08T10:00:00",
                        }
                    ),
                )
            )
            session.commit()
            task_id = task.id

        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [task_id],
                "windowStart": "2026-07-08T09:00:00",
                "windowEnd": "2026-07-08T11:00:00",
                "slotMinutes": 15,
                "fallbackDurationMinutes": 30,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["diagnostics"]["scheduledTaskCount"], 1)
        self.assertEqual(payload["draftBlocks"][0]["startAt"], "2026-07-08T10:00:00")
        self.assertEqual(payload["draftBlocks"][0]["endAt"], "2026-07-08T10:30:00")

    def test_auto_draft_rejects_invalid_window_with_error_wrapper(self) -> None:
        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [123],
                "windowStart": "2026-06-18T14:00:00",
                "windowEnd": "2026-06-18T13:00:00",
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["detail"]["error"]["type"], "InvalidScheduleRequest")
        self.assertIn("windowEnd must be after windowStart", payload["detail"]["error"]["detail"])

    def test_auto_draft_rejects_missing_task_with_error_wrapper(self) -> None:
        response = self.client.post(
            "/api/v1/scheduling/auto-draft",
            json={
                "taskIds": [987654321],
                "windowStart": "2026-06-18T09:00:00",
                "windowEnd": "2026-06-18T10:00:00",
            },
        )

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["detail"]["error"]["type"], "NotFound")
        self.assertIn("Task 987654321 not found", payload["detail"]["error"]["detail"])


if __name__ == "__main__":
    unittest.main()
