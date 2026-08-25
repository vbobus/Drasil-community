import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db.database import create_db_and_tables, engine
from app.db.seed import seed_default_categories
from app.main import app
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceSeries


class CalendarBlockApiTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        seed_default_categories()
        self.client = TestClient(app)

    def _create_category(self, session: Session, name: str, accent_color: str) -> Category:
        category = Category(
            name=name,
            accent_color=accent_color,
            icon_slug="briefcase",
            sort_order=901,
        )
        session.add(category)
        session.commit()
        session.refresh(category)
        return category

    def _cleanup_category_fixture(self, category_id: int) -> None:
        from app.models.calendar_block import CalendarBlock

        with Session(engine) as session:
            for series in session.exec(
                select(RecurrenceSeries).where(RecurrenceSeries.category_id == category_id)
            ).all():
                session.delete(series)
            for block in session.exec(
                select(CalendarBlock).where(CalendarBlock.category_id == category_id)
            ).all():
                session.delete(block)
            for node in session.exec(
                select(StrategyNode).where(StrategyNode.category_id == category_id)
            ).all():
                session.delete(node)
            category = session.get(Category, category_id)
            if category:
                session.delete(category)
            session.commit()

    def _create_task(
        self,
        session: Session,
        category: Category,
        title: str,
        *,
        emoji: str = "T",
        status: str = "todo",
        priority: str = "medium",
        deadline_at: str | None = None,
        estimate: int | None = None,
    ) -> StrategyNode:
        node = StrategyNode(
            category_id=category.id,
            title=title,
            node_type="task",
            status=status,
            priority=priority,
            deadline_at=deadline_at,
            time_estimate_minutes=estimate,
            emoji=emoji,
            sort_order=0,
        )
        session.add(node)
        session.commit()
        session.refresh(node)
        return node

    def test_get_calendar_blocks_returns_range_overlaps_with_identity(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Work", "#1f6feb")
            task = self._create_task(session, category, "Write calendar contract", emoji="C")
            category_id = category.id
            task_id = task.id

        create_response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": task_id,
                "categoryId": category_id,
                "startAt": "2026-06-04T08:45:00-07:00",
                "endAt": "2026-06-04T10:15:00-07:00",
            },
        )
        self.assertEqual(create_response.status_code, 201)

        list_response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2026-06-04T09:00:00-07:00",
                "end": "2026-06-04T09:30:00-07:00",
            },
        )

        self.assertEqual(list_response.status_code, 200)
        blocks = list_response.json()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["taskId"], task_id)
        self.assertEqual(blocks[0]["categoryId"], category_id)
        self.assertEqual(blocks[0]["title"], "Write calendar contract")
        self.assertEqual(blocks[0]["emoji"], "C")
        self.assertEqual(blocks[0]["categoryName"], "Calendar API Work")
        self.assertEqual(blocks[0]["categoryColor"], "#1f6feb")
        self.assertEqual(blocks[0]["variant"], "scheduled")
        self.assertEqual(blocks[0]["completionStatus"], "not_started")

    def test_get_calendar_blocks_returns_live_task_status_metadata(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Metadata", "#6d5dfc")
            task = self._create_task(
                session,
                category,
                "Calendar status source",
                emoji="S",
                status="done",
                priority="high",
                deadline_at="2026-06-12",
                estimate=95,
            )
            category_id = category.id
            task_id = task.id

        create_response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": task_id,
                "categoryId": category_id,
                "startAt": "2026-06-04T13:00:00-07:00",
                "endAt": "2026-06-04T14:35:00-07:00",
            },
        )
        self.assertEqual(create_response.status_code, 201)

        list_response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2026-06-04T12:00:00-07:00",
                "end": "2026-06-04T15:00:00-07:00",
            },
        )

        self.assertEqual(list_response.status_code, 200)
        blocks = list_response.json()
        matching_block = next(block for block in blocks if block["taskId"] == task_id)
        self.assertEqual(matching_block["taskStatus"], "done")
        self.assertEqual(matching_block["taskPriority"], "high")
        self.assertEqual(matching_block["taskDeadlineAt"], "2026-06-12")
        self.assertEqual(matching_block["timeEstimateMinutes"], 95)

    def test_scheduled_task_ids_are_not_limited_to_the_visible_range(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Membership", "#2563eb")
            future_task = self._create_task(session, category, "Scheduled next month")
            recurring_task = self._create_task(session, category, "Recurring later")
            unscheduled_task = self._create_task(session, category, "Not scheduled")
            category_id = int(category.id)
            future_task_id = int(future_task.id)
            recurring_task_id = int(recurring_task.id)
            unscheduled_task_id = int(unscheduled_task.id)
            session.add(
                RecurrenceSeries(
                    task_id=recurring_task_id,
                    category_id=category_id,
                    frequency="weekly",
                    days_of_week_json='["monday"]',
                    start_date=date(2057, 9, 3),
                    preferred_start_time="17:00",
                    allowed_start_time="17:00",
                    allowed_end_time="17:00",
                    duration_minutes=30,
                    status="active",
                )
            )
            session.commit()
        self.addCleanup(self._cleanup_category_fixture, category_id)

        create_response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": future_task_id,
                "categoryId": category_id,
                "startAt": "2057-10-10T09:00:00",
                "endAt": "2057-10-10T10:00:00",
            },
        )
        self.assertEqual(create_response.status_code, 201)

        unrelated_day = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2057-09-01T00:00:00",
                "end": "2057-09-02T00:00:00",
            },
        )
        self.assertEqual(unrelated_day.status_code, 200)
        self.assertNotIn(
            future_task_id,
            [block["taskId"] for block in unrelated_day.json()],
        )

        membership_response = self.client.get(
            "/api/v1/calendar-blocks/scheduled-task-ids"
        )
        self.assertEqual(membership_response.status_code, 200)
        scheduled_task_ids = set(membership_response.json())
        self.assertIn(future_task_id, scheduled_task_ids)
        self.assertIn(recurring_task_id, scheduled_task_ids)
        self.assertNotIn(unscheduled_task_id, scheduled_task_ids)

    def test_later_visible_range_materializes_and_returns_ongoing_recurrence(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Recurrence", "#16a34a")
            task = self._create_task(
                session,
                category,
                "Workout",
                emoji="W",
                estimate=120,
            )
            series = RecurrenceSeries(
                task_id=int(task.id),
                category_id=int(category.id),
                frequency="weekly",
                days_of_week_json='["monday", "wednesday"]',
                start_date=date(2057, 7, 16),
                preferred_start_time="17:00",
                allowed_start_time="17:00",
                allowed_end_time="17:00",
                duration_minutes=120,
                status="active",
            )
            session.add(series)
            session.commit()
            category_id = int(category.id)
            task_id = int(task.id)
        self.addCleanup(self._cleanup_category_fixture, category_id)

        response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2057-09-01T00:00:00",
                "end": "2057-09-08T00:00:00",
            },
        )

        self.assertEqual(response.status_code, 200)
        workout_blocks = [
            block for block in response.json() if block["taskId"] == task_id
        ]
        self.assertEqual(len(workout_blocks), 2)
        self.assertEqual(
            [block["startAt"] for block in workout_blocks],
            ["2057-09-03T17:00:00", "2057-09-05T17:00:00"],
        )
        self.assertTrue(all(block["title"] == "Workout" for block in workout_blocks))

    def test_get_calendar_blocks_rejects_invalid_range(self) -> None:
        response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2026-06-04T10:00:00-07:00",
                "end": "2026-06-04T09:00:00-07:00",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("end must be after start", response.text)

    def test_manual_calendar_block_create_validates_task_category_match(self) -> None:
        with Session(engine) as session:
            correct_category = self._create_category(session, "Calendar API Correct", "#0f766e")
            wrong_category = self._create_category(session, "Calendar API Wrong", "#d97706")
            task = self._create_task(session, correct_category, "Schedule me")
            wrong_category_id = wrong_category.id
            task_id = task.id

        response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": task_id,
                "categoryId": wrong_category_id,
                "startAt": "2026-06-04T11:00:00-07:00",
                "endAt": "2026-06-04T11:30:00-07:00",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("does not belong", response.text)

    def test_manual_calendar_block_create_rejects_invalid_datetime_range(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Range", "#9333ea")
            task = self._create_task(session, category, "Invalid range")
            category_id = category.id
            task_id = task.id

        response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": task_id,
                "categoryId": category_id,
                "startAt": "2026-06-04T12:00:00-07:00",
                "endAt": "2026-06-04T12:00:00-07:00",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("endAt must be after startAt", response.text)

    def test_manual_calendar_block_can_be_rescheduled_and_deleted(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Edit", "#1f6feb")
            task = self._create_task(session, category, "Move and remove me", emoji="M")
            category_id = category.id
            task_id = task.id

        create_response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": task_id,
                "categoryId": category_id,
                "startAt": "2026-06-04T08:00:00-07:00",
                "endAt": "2026-06-04T08:30:00-07:00",
            },
        )
        self.assertEqual(create_response.status_code, 201)
        block_id = create_response.json()["id"]

        update_response = self.client.patch(
            f"/api/v1/calendar-blocks/{block_id}",
            json={
                "startAt": "2026-06-04T09:15:00-07:00",
                "endAt": "2026-06-04T09:45:00-07:00",
            },
        )

        self.assertEqual(update_response.status_code, 200)
        updated_block = update_response.json()
        self.assertEqual(updated_block["id"], block_id)
        self.assertEqual(updated_block["startAt"], "2026-06-04T09:15:00")
        self.assertEqual(updated_block["endAt"], "2026-06-04T09:45:00")
        self.assertEqual(updated_block["title"], "Move and remove me")

        list_response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2026-06-04T09:00:00-07:00",
                "end": "2026-06-04T10:00:00-07:00",
            },
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertIn(block_id, [block["id"] for block in list_response.json()])

        delete_response = self.client.delete(f"/api/v1/calendar-blocks/{block_id}")
        self.assertEqual(delete_response.status_code, 204)

        empty_response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2026-06-04T09:00:00-07:00",
                "end": "2026-06-04T10:00:00-07:00",
            },
        )
        self.assertEqual(empty_response.status_code, 200)
        self.assertNotIn(block_id, [block["id"] for block in empty_response.json()])

    def test_cross_date_intervals_remain_canonical_and_use_half_open_overlap(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Calendar API Multi Day", "#2563eb")
            spanning = self._create_task(session, category, "Multi-day release")
            ending_at_start = self._create_task(session, category, "Boundary before")
            starting_at_end = self._create_task(session, category, "Boundary after")
            category_id = category.id
            spanning_id = spanning.id
            ending_at_start_id = ending_at_start.id
            starting_at_end_id = starting_at_end.id
        self.addCleanup(self._cleanup_category_fixture, category_id)

        create_response = self.client.post(
            "/api/v1/calendar-blocks",
            json={
                "taskId": spanning_id,
                "categoryId": category_id,
                "startAt": "2026-07-13T23:30:00",
                "endAt": "2026-07-16T01:00:00",
            },
        )
        self.assertEqual(create_response.status_code, 201)
        canonical = create_response.json()
        block_id = canonical["id"]
        self.assertEqual(canonical["startAt"], "2026-07-13T23:30:00")
        self.assertEqual(canonical["endAt"], "2026-07-16T01:00:00")

        for start_at, end_at in (
            ("2026-07-13T00:00:00", "2026-07-14T00:00:00"),
            ("2026-07-14T00:00:00", "2026-07-15T00:00:00"),
            ("2026-07-16T00:00:00", "2026-07-17T00:00:00"),
        ):
            response = self.client.get(
                "/api/v1/calendar-blocks",
                params={"start": start_at, "end": end_at},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item["id"] for item in response.json() if item["id"] == block_id],
                [block_id],
            )

        for task_id, start_at, end_at in (
            (ending_at_start_id, "2026-07-13T12:00:00", "2026-07-14T00:00:00"),
            (starting_at_end_id, "2026-07-15T00:00:00", "2026-07-15T12:00:00"),
        ):
            response = self.client.post(
                "/api/v1/calendar-blocks",
                json={
                    "taskId": task_id,
                    "categoryId": category_id,
                    "startAt": start_at,
                    "endAt": end_at,
                },
            )
            self.assertEqual(response.status_code, 201)

        boundary_response = self.client.get(
            "/api/v1/calendar-blocks",
            params={
                "start": "2026-07-14T00:00:00",
                "end": "2026-07-15T00:00:00",
            },
        )
        boundary_task_ids = {item["taskId"] for item in boundary_response.json()}
        self.assertNotIn(ending_at_start_id, boundary_task_ids)
        self.assertNotIn(starting_at_end_id, boundary_task_ids)
        self.assertIn(spanning_id, boundary_task_ids)

        update_response = self.client.patch(
            f"/api/v1/calendar-blocks/{block_id}",
            json={
                "startAt": "2026-07-14T22:45:00",
                "endAt": "2026-07-17T00:15:00",
            },
        )
        self.assertEqual(update_response.status_code, 200)
        updated = update_response.json()
        self.assertEqual(updated["id"], block_id)
        self.assertEqual(updated["taskId"], spanning_id)
        self.assertEqual(updated["categoryId"], category_id)
        self.assertEqual(updated["startAt"], "2026-07-14T22:45:00")
        self.assertEqual(updated["endAt"], "2026-07-17T00:15:00")

        invalid_update = self.client.patch(
            f"/api/v1/calendar-blocks/{block_id}",
            json={
                "startAt": "2026-07-17T00:15:00",
                "endAt": "2026-07-17T00:15:00",
            },
        )
        self.assertEqual(invalid_update.status_code, 422)


if __name__ == "__main__":
    unittest.main()
