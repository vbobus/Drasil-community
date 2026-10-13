import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app as _app
from app.api.calendar_blocks import CalendarBlockUpdate, update_calendar_block
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceOccurrenceException


class CalendarMoveAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Category(id=1, name='Work'))
        self.session.add(StrategyNode(id=1, category_id=1, title='Appointment',
            planned_date='2026-10-02', deadline_at='2026-10-07'))
        self.session.add(CalendarBlock(id=1, task_id=1, category_id=1,
            start_at=datetime(2026, 10, 2, 12), end_at=datetime(2026, 10, 2, 13)))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def move(self, start='2026-10-03T12:00:00', end='2026-10-03T13:00:00'):
        return update_calendar_block(1, CalendarBlockUpdate(startAt=start, endAt=end), self.session)

    def test_calendar_move_carries_assignment_and_preserves_identity_and_deadline(self):
        result = self.move()
        task = self.session.get(StrategyNode, 1)
        self.assertEqual((result.id, result.task_planned_date), (1, '2026-10-03'))
        self.assertEqual((task.planned_date, task.deadline_at), ('2026-10-03', '2026-10-07'))
        self.assertEqual(len(self.session.exec(select(StrategyNode)).all()), 1)
        self.assertEqual(len(self.session.exec(select(CalendarBlock)).all()), 1)

    def test_same_day_resize_does_not_change_assignment(self):
        result = self.move('2026-10-02T12:00:00', '2026-10-03T01:00:00')
        self.assertEqual(result.task_planned_date, '2026-10-02')

    def test_backward_midnight_move_carries_assignment(self):
        result = self.move('2026-10-01T23:30:00', '2026-10-02T00:30:00')
        self.assertEqual(result.task_planned_date, '2026-10-01')

    def test_unrelated_explicit_plan_and_absent_plan_are_preserved(self):
        for planned in ('2026-10-05', None):
            with self.subTest(planned=planned):
                task = self.session.get(StrategyNode, 1)
                task.planned_date = planned
                block = self.session.get(CalendarBlock, 1)
                block.start_at, block.end_at = datetime(2026, 10, 2, 12), datetime(2026, 10, 2, 13)
                self.session.add_all([task, block])
                self.session.commit()
                self.assertEqual(self.move().task_planned_date, planned)

    def test_failed_write_rolls_back_booking_and_assignment(self):
        with patch.object(self.session, 'commit', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self.move()
        self.session.rollback()
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-02')
        self.assertEqual(self.session.get(CalendarBlock, 1).start_at, datetime(2026, 10, 2, 12))

    def test_recurring_move_records_override_for_the_same_occurrence(self):
        block = self.session.get(CalendarBlock, 1)
        block.recurrence_series_id, block.recurrence_key = 'series-test', '2026-10-02'
        self.session.add(block)
        self.session.commit()
        result = self.move()
        exception = self.session.exec(select(RecurrenceOccurrenceException)).one()
        self.assertEqual((exception.disposition, exception.start_at), ('overridden', result.start_at))
        self.assertEqual(block.recurrence_key, '2026-10-02')


if __name__ == '__main__':
    unittest.main()
