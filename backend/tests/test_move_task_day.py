import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app as _app
from app.api.calendar_blocks import MoveTaskDayRequest, move_task_day
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceOccurrenceException


class MoveTaskDayTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Category(id=1, name='Work'))
        self.session.add(StrategyNode(id=1, title='Appointment', category_id=1,
            planned_date='2026-10-02', deadline_at='2026-10-07'))
        for block_id, day in [(1, 2), (2, 4)]:
            self.session.add(CalendarBlock(id=block_id, task_id=1, category_id=1,
                start_at=datetime(2026, 10, day, 12), end_at=datetime(2026, 10, day, 13)))
        self.session.commit()
        self.request = MoveTaskDayRequest(taskId=1, sourceDate='2026-10-02', targetDate='2026-10-03')

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_move_preserves_identity_time_duration_and_other_bookings(self):
        moved = move_task_day(self.request, self.session)
        self.assertEqual([block.id for block in moved], [1])
        self.assertEqual((moved[0].start_at, moved[0].end_at),
            (datetime(2026, 10, 3, 12), datetime(2026, 10, 3, 13)))
        self.assertEqual(self.session.get(CalendarBlock, 2).start_at, datetime(2026, 10, 4, 12))
        self.assertEqual(len(self.session.exec(select(CalendarBlock)).all()), 2)
        task = self.session.get(StrategyNode, 1)
        self.assertEqual((task.planned_date, task.deadline_at), ('2026-10-03', '2026-10-07'))
        with self.assertRaises(HTTPException) as error:
            move_task_day(self.request, self.session)
        self.assertEqual(error.exception.status_code, 409)

    def test_failure_rolls_back_task_and_all_bookings(self):
        with patch.object(self.session, 'commit', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                move_task_day(self.request, self.session)
        self.session.rollback()
        self.assertEqual(self.session.get(CalendarBlock, 1).start_at, datetime(2026, 10, 2, 12))
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-02')

    def test_recurring_move_persists_override_without_changing_series_identity(self):
        block = self.session.get(CalendarBlock, 1)
        block.recurrence_series_id, block.recurrence_key = 'series-test', '2026-10-02'
        self.session.add(block)
        self.session.commit()
        move_task_day(self.request, self.session)
        exception = self.session.exec(select(RecurrenceOccurrenceException)).one()
        self.assertEqual((exception.disposition, exception.start_at), ('overridden', datetime(2026, 10, 3, 12)))
        self.assertEqual(self.session.get(CalendarBlock, 1).recurrence_key, '2026-10-02')

    def test_untimed_task_moves_without_creating_a_booking(self):
        for block in self.session.exec(select(CalendarBlock)).all():
            self.session.delete(block)
        self.session.commit()
        self.assertEqual(move_task_day(self.request, self.session), [])
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-03')
        self.assertEqual(self.session.exec(select(CalendarBlock)).all(), [])
