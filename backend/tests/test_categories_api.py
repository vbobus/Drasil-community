import unittest
from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db.database import create_db_and_tables, engine
from app.main import app
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import (
    RecurrenceOccurrenceException,
    RecurrenceSeries,
)


class CategoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        self.client = TestClient(app)

    def test_create_and_update_persist_workspace_icon_slug(self) -> None:
        created_response = self.client.post(
            "/api/v1/categories",
            json={"name": "Icon API Workspace", "iconSlug": "rocket"},
        )

        self.assertEqual(created_response.status_code, 201)
        created = created_response.json()
        self.assertEqual(created["iconSlug"], "rocket")

        updated_response = self.client.patch(
            f"/api/v1/categories/{created['id']}",
            json={"iconSlug": "book-open"},
        )

        self.assertEqual(updated_response.status_code, 200)
        self.assertEqual(updated_response.json()["iconSlug"], "book-open")
        with Session(engine) as session:
            persisted = session.get(Category, created["id"])
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.icon_slug, "book-open")

        delete_response = self.client.delete(f"/api/v1/categories/{created['id']}")
        self.assertEqual(delete_response.status_code, 204)

    def test_delete_workspace_removes_its_tasks_and_scheduling_records(self) -> None:
        with Session(engine) as session:
            category = Category(name="Delete Cascade Workspace", sort_order=999)
            session.add(category)
            session.commit()
            session.refresh(category)
            self.assertIsNotNone(category.id)

            task = StrategyNode(category_id=int(category.id), title="Delete me")
            session.add(task)
            session.commit()
            session.refresh(task)
            self.assertIsNotNone(task.id)

            series = RecurrenceSeries(
                id="delete-cascade-series",
                task_id=int(task.id),
                category_id=int(category.id),
                frequency="weekly",
                interval=1,
                days_of_week_json="[1]",
                start_date=date(2026, 8, 12),
                preferred_start_time="09:00",
                allowed_start_time="09:00",
                allowed_end_time="10:00",
                duration_minutes=60,
            )
            session.add(series)
            session.add(
                RecurrenceOccurrenceException(
                    id="delete-cascade-series:2026-08-17",
                    series_id=series.id,
                    recurrence_key="2026-08-17",
                    disposition="cancelled",
                )
            )
            session.add(
                CalendarBlock(
                    task_id=int(task.id),
                    category_id=int(category.id),
                    start_at=datetime(2026, 8, 17, 9, 0),
                    end_at=datetime(2026, 8, 17, 10, 0),
                    recurrence_series_id=series.id,
                    recurrence_key="2026-08-17",
                )
            )
            session.commit()
            category_id = int(category.id)
            task_id = int(task.id)

        response = self.client.delete(f"/api/v1/categories/{category_id}")

        self.assertEqual(response.status_code, 204)
        with Session(engine) as session:
            self.assertIsNone(session.get(Category, category_id))
            self.assertIsNone(session.get(StrategyNode, task_id))
            self.assertEqual(
                session.exec(
                    select(CalendarBlock).where(CalendarBlock.category_id == category_id)
                ).all(),
                [],
            )
            self.assertIsNone(session.get(RecurrenceSeries, "delete-cascade-series"))
            self.assertIsNone(
                session.get(
                    RecurrenceOccurrenceException,
                    "delete-cascade-series:2026-08-17",
                )
            )


if __name__ == "__main__":
    unittest.main()
