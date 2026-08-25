from __future__ import annotations

import unittest
from datetime import date, datetime
from tempfile import TemporaryDirectory
from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.db.database import _ensure_revision_columns
from app.models.revisioning import EntityRevisionConflict
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import PreferenceLink, SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries


class AgentEntityRevisionTests(unittest.TestCase):
    def test_every_native_precondition_entity_starts_at_revision_one(self) -> None:
        rows = [
            Category(name="Work"),
            StrategyNode(category_id=1, title="Task"),
            CalendarBlock(task_id=1, category_id=1, start_at="2026-07-20T10:00:00", end_at="2026-07-20T11:00:00"),
            RecurrenceSeries(
                task_id=1,
                category_id=1,
                frequency="weekly",
                days_of_week_json='["monday"]',
                start_date="2026-07-20",
                preferred_start_time="10:00",
                allowed_start_time="10:00",
                allowed_end_time="11:00",
                duration_minutes=60,
            ),
            UserPreference(),
            SchedulingConstraint(),
            PreferenceLink(preference_id="preference:1", target_type="task", target_id="task:1"),
        ]
        self.assertTrue(all(row.revision == 1 for row in rows))

    def test_preserved_orm_writer_atomically_advances_revision(self) -> None:
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(
            engine,
            tables=[Category.__table__, StrategyNode.__table__],
        )
        with Session(engine) as session:
            category = Category(name="Work")
            session.add(category)
            session.flush()
            node = StrategyNode(category_id=int(category.id), title="Before")
            session.add(node)
            session.commit()
            node_id = int(node.id)

        with Session(engine) as session:
            node = session.get(StrategyNode, node_id)
            self.assertIsNotNone(node)
            node.title = "After"
            session.add(node)
            session.commit()
            self.assertEqual(node.revision, 2)

    def test_every_protected_model_advances_on_persisted_orm_update(self) -> None:
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            category = Category(name="Work")
            session.add(category)
            session.flush()
            node = StrategyNode(category_id=int(category.id), title="Task")
            session.add(node)
            session.flush()
            rows = [
                category,
                node,
                CalendarBlock(
                    task_id=int(node.id),
                    category_id=int(category.id),
                    start_at=datetime(2026, 7, 20, 10, 0),
                    end_at=datetime(2026, 7, 20, 11, 0),
                ),
                RecurrenceSeries(
                    task_id=int(node.id),
                    category_id=int(category.id),
                    frequency="weekly",
                    start_date=date(2026, 7, 20),
                    preferred_start_time="10:00",
                    allowed_start_time="10:00",
                    allowed_end_time="11:00",
                    duration_minutes=60,
                ),
                UserPreference(),
                SchedulingConstraint(),
                PreferenceLink(
                    preference_id="preference:1",
                    target_type="task",
                    target_id=str(node.id),
                ),
            ]
            session.add_all(rows[2:])
            session.commit()

            category.name = "Updated Work"
            node.title = "Updated Task"
            rows[2].is_pinned = True
            rows[3].status = "paused"
            rows[4].description = "Updated preference"
            rows[5].description = "Updated constraint"
            rows[6].weight = 0.5
            session.add_all(rows)
            session.commit()
            self.assertTrue(all(row.revision == 2 for row in rows))

    def test_existing_rows_receive_revision_one_backfill_idempotently(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE legacy_entities (id INTEGER PRIMARY KEY, name TEXT)")
            )
            connection.execute(
                text("INSERT INTO legacy_entities (id, name) VALUES (1, 'legacy')")
            )

        _ensure_revision_columns(engine, ("legacy_entities",))
        _ensure_revision_columns(engine, ("legacy_entities",))

        columns = {column["name"]: column for column in inspect(engine).get_columns("legacy_entities")}
        self.assertIn("revision", columns)
        self.assertFalse(columns["revision"]["nullable"])
        with engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT revision FROM legacy_entities WHERE id = 1")
                ).scalar_one(),
                1,
            )

    def test_two_sessions_reject_stale_ordinary_orm_update(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "revision-race.sqlite3"
            engine = create_engine(
                f"sqlite:///{database_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            SQLModel.metadata.create_all(
                engine,
                tables=[Category.__table__, StrategyNode.__table__],
            )
            with Session(engine) as setup:
                category = Category(name="Work")
                setup.add(category)
                setup.flush()
                node = StrategyNode(category_id=int(category.id), title="Before")
                setup.add(node)
                setup.commit()
                node_id = int(node.id)

            first = Session(engine)
            second = Session(engine)
            try:
                first_node = first.get(StrategyNode, node_id)
                stale_node = second.get(StrategyNode, node_id)
                first_node.title = "First"
                first.commit()
                stale_node.title = "Stale"
                with self.assertRaises(EntityRevisionConflict):
                    second.commit()
                second.rollback()
            finally:
                first.close()
                second.close()

            with Session(engine) as verify:
                node = verify.get(StrategyNode, node_id)
                self.assertEqual((node.title, node.revision), ("First", 2))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
