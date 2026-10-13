"""Planned days persist separately from deadlines and timed calendar blocks."""
import unittest

from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app as _app  # Register models.
from app.api.nodes import StrategyNodeCreate, StrategyNodeUpdate, StrategyNodeRead, create_node, update_node
from app.db.database import create_db_and_tables
from app.models.category import Category
from app.models.calendar_block import CalendarBlock


class PlannedDayTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Category(id=1, name='Work'))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_create_move_and_clear_day_preserves_deadline_and_never_books_time(self):
        node = create_node(StrategyNodeCreate(title='Prepare documents', categoryId=1,
            plannedDate='2026-10-02', deadlineAt='2026-10-07', timeEstimateMinutes=60), self.session)
        node_id = node.id
        self.assertEqual(StrategyNodeRead.model_validate(node).model_dump(by_alias=True)['plannedDate'], '2026-10-02')
        updated = update_node(node_id, StrategyNodeUpdate(plannedDate='2026-10-03'), self.session)
        self.assertEqual(updated.planned_date, '2026-10-03')
        self.assertEqual(updated.deadline_at, '2026-10-07')
        cleared = update_node(node_id, StrategyNodeUpdate(plannedDate=None), self.session)
        self.assertIsNone(cleared.planned_date)
        self.assertEqual(cleared.deadline_at, '2026-10-07')
        self.assertFalse(self.session.exec(select(CalendarBlock)).all())

    def test_invalid_planned_days_are_rejected(self):
        for value in ['2026-02-30', 'tomorrow', '2026-10-02T12:45:00']:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                StrategyNodeCreate(title='Task', categoryId=1, plannedDate=value)

    def test_existing_sqlite_schema_adds_nullable_day_without_reassigning_tasks(self):
        create_node(StrategyNodeCreate(title='Old task', categoryId=1), self.session)
        with self.engine.begin() as connection:
            connection.execute(text('ALTER TABLE strategy_nodes DROP COLUMN planned_date'))
        create_db_and_tables(self.engine)
        self.assertIn('planned_date', {item['name'] for item in inspect(self.engine).get_columns('strategy_nodes')})
        with self.engine.connect() as connection:
            self.assertIsNone(connection.execute(text('SELECT planned_date FROM strategy_nodes')).scalar())


if __name__ == '__main__':
    unittest.main()
