from __future__ import annotations

from datetime import datetime
import unittest

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db.database import create_db_and_tables, engine
from app.main import app
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceSeries


class RecurrenceRuleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        self.client = TestClient(app)
        with Session(engine) as session:
            category = Category(
                name=f"Recurrence rules {datetime.utcnow().timestamp()}",
                accent_color="#8b5cf6",
                icon_slug="refresh-cw",
                sort_order=991,
            )
            session.add(category)
            session.commit()
            session.refresh(category)
            task = StrategyNode(
                category_id=int(category.id),
                title="Review the week",
                node_type="task",
                status="todo",
                priority="medium",
                time_estimate_minutes=45,
                sort_order=0,
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            self.category_id = int(category.id)
            self.task_id = int(task.id)

    def tearDown(self) -> None:
        with Session(engine) as session:
            series_rows = list(
                session.exec(
                    select(RecurrenceSeries).where(
                        RecurrenceSeries.task_id == self.task_id
                    )
                ).all()
            )
            series_ids = {row.id for row in series_rows}
            blocks = list(
                session.exec(
                    select(CalendarBlock).where(
                        (CalendarBlock.task_id == self.task_id)
                        | CalendarBlock.recurrence_series_id.in_(series_ids)
                    )
                ).all()
            )
            for block in blocks:
                session.delete(block)
            for row in series_rows:
                session.delete(row)
            task = session.get(StrategyNode, self.task_id)
            if task is not None:
                session.delete(task)
            category = session.get(Category, self.category_id)
            if category is not None:
                session.delete(category)
            session.commit()

    def _payload(self) -> dict[str, object]:
        return {
            "frequency": "weekly",
            "daysOfWeek": ["monday", "friday"],
            "startDate": "2026-07-24",
            "endDate": None,
            "preferredStartTime": "09:30",
            "durationMinutes": 45,
            "timezone": "America/Vancouver",
        }

    def test_create_edit_stop_and_node_projection_preserve_task_type(self) -> None:
        created = self.client.put(
            f"/api/v1/recurrence-rules/{self.task_id}",
            json=self._payload(),
        )
        self.assertEqual(created.status_code, 200, created.text)
        created_rule = created.json()
        self.assertEqual(created_rule["revision"], 1)
        self.assertEqual(created_rule["daysOfWeek"], ["monday", "friday"])

        nodes = self.client.get(
            "/api/v1/nodes",
            params={"categoryId": self.category_id},
        )
        self.assertEqual(nodes.status_code, 200, nodes.text)
        projected = next(item for item in nodes.json() if item["id"] == self.task_id)
        self.assertTrue(projected["isRecurring"])
        self.assertEqual(projected["nodeType"], "task")
        self.assertEqual(projected["recurrenceRule"]["id"], created_rule["id"])

        stale_missing_revision = self.client.put(
            f"/api/v1/recurrence-rules/{self.task_id}",
            json={**self._payload(), "preferredStartTime": "10:00"},
        )
        self.assertEqual(stale_missing_revision.status_code, 409)

        edited = self.client.put(
            f"/api/v1/recurrence-rules/{self.task_id}",
            json={
                **self._payload(),
                "frequency": "daily",
                "daysOfWeek": [],
                "preferredStartTime": "10:00",
                "expectedRevision": created_rule["revision"],
            },
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        edited_rule = edited.json()
        self.assertEqual(edited_rule["revision"], 2)
        self.assertEqual(edited_rule["frequency"], "daily")

        with Session(engine) as session:
            active = list(
                session.exec(
                    select(RecurrenceSeries)
                    .where(RecurrenceSeries.task_id == self.task_id)
                    .where(RecurrenceSeries.status == "active")
                ).all()
            )
            self.assertEqual(len(active), 1)
            task = session.get(StrategyNode, self.task_id)
            self.assertEqual(task.node_type, "task")
            block = CalendarBlock(
                task_id=self.task_id,
                category_id=self.category_id,
                start_at=datetime(2026, 7, 27, 10, 0),
                end_at=datetime(2026, 7, 27, 10, 45),
                is_recurring=True,
                recurrence_series_id=edited_rule["id"],
                recurrence_key="2026-07-27",
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            block_id = int(block.id)

        stale_stop = self.client.delete(
            f"/api/v1/recurrence-rules/{self.task_id}",
            params={"expectedRevision": 1},
        )
        self.assertEqual(stale_stop.status_code, 409)
        stopped = self.client.delete(
            f"/api/v1/recurrence-rules/{self.task_id}",
            params={"expectedRevision": edited_rule["revision"]},
        )
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["status"], "inactive")

        nodes_after = self.client.get(
            "/api/v1/nodes",
            params={"categoryId": self.category_id},
        )
        projected_after = next(
            item for item in nodes_after.json() if item["id"] == self.task_id
        )
        self.assertFalse(projected_after["isRecurring"])
        self.assertIsNone(projected_after["recurrenceRule"])
        with Session(engine) as session:
            self.assertIsNotNone(session.get(CalendarBlock, block_id))

    def test_validation_rejects_weekly_without_days_and_cross_midnight(self) -> None:
        no_days = self.client.put(
            f"/api/v1/recurrence-rules/{self.task_id}",
            json={**self._payload(), "daysOfWeek": []},
        )
        self.assertEqual(no_days.status_code, 422)
        cross_midnight = self.client.put(
            f"/api/v1/recurrence-rules/{self.task_id}",
            json={
                **self._payload(),
                "preferredStartTime": "23:30",
                "durationMinutes": 60,
            },
        )
        self.assertEqual(cross_midnight.status_code, 422)


if __name__ == "__main__":
    unittest.main()
