import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app as _app
from app.api.calendar_blocks import UnscheduleBlockRequest, unschedule_calendar_block
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceOccurrenceException


class UnscheduleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Category(id=1, name='Work'))
        self.session.add(StrategyNode(id=1, title='Sample appointment', category_id=1,
            planned_date='2026-10-02', deadline_at='2026-10-07', time_estimate_minutes=60))
        self.session.add(CalendarBlock(id=1, task_id=1, category_id=1,
            start_at=datetime(2026, 10, 2, 12), end_at=datetime(2026, 10, 2, 13)))
        self.session.add(CalendarBlock(id=2, task_id=1, category_id=1,
            start_at=datetime(2026, 10, 4, 12), end_at=datetime(2026, 10, 4, 13)))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_moves_only_selected_booking_preserving_task_and_other_bookings(self):
        result = unschedule_calendar_block(1, UnscheduleBlockRequest(plannedDate='2026-10-03'), self.session)
        self.assertEqual(result['plannedDate'], '2026-10-03')
        self.assertEqual(result['scheduledTaskIds'], [1])
        self.assertIsNone(self.session.get(CalendarBlock, 1))
        self.assertIsNotNone(self.session.get(CalendarBlock, 2))
        task = self.session.get(StrategyNode, 1)
        self.assertEqual((task.title, task.deadline_at, task.time_estimate_minutes, task.planned_date),
            ('Sample appointment', '2026-10-07', 60, '2026-10-03'))

    def test_database_failure_rolls_back_both_changes(self):
        with patch.object(self.session, 'commit', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                unschedule_calendar_block(1, UnscheduleBlockRequest(plannedDate='2026-10-03'), self.session)
        self.session.rollback()
        self.assertIsNotNone(self.session.get(CalendarBlock, 1))
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-02')

    def test_remote_failure_does_not_remove_booking_or_move_day(self):
        with patch('app.api.calendar_blocks.delete_google_event_for_block', return_value='offline'):
            with self.assertRaises(HTTPException) as error:
                unschedule_calendar_block(1, UnscheduleBlockRequest(plannedDate='2026-10-03'), self.session)
        self.assertEqual(error.exception.status_code, 502)
        self.assertIsNotNone(self.session.get(CalendarBlock, 1))
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-02')

    def test_recurring_occurrence_gets_durable_exception(self):
        block = self.session.get(CalendarBlock, 1)
        block.recurrence_series_id = 'series-test'
        block.recurrence_key = '2026-10-02'
        self.session.add(block)
        self.session.commit()
        unschedule_calendar_block(1, UnscheduleBlockRequest(plannedDate='2026-10-03'), self.session)
        exception = self.session.exec(select(RecurrenceOccurrenceException)).one()
        self.assertEqual((exception.series_id, exception.recurrence_key, exception.disposition),
            ('series-test', '2026-10-02', 'cancelled'))

    def test_invalid_day_is_rejected(self):
        with self.assertRaises(ValidationError):
            UnscheduleBlockRequest(plannedDate='2026-02-30')


if __name__ == '__main__':
    unittest.main()
