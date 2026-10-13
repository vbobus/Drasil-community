import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app as _app
from app.api.calendar_blocks import RemoveTaskDayRequest, remove_task_day
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.recurrence_series import RecurrenceOccurrenceException


class RemoveTaskDayTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Category(id=1, name='Work'))
        for task_id, parent, planned in [(1, None, '2026-10-02'), (2, 1, '2026-10-02'), (3, 1, '2026-10-03')]:
            self.session.add(StrategyNode(id=task_id, title=f'Task {task_id}', parent_id=parent, category_id=1,
                planned_date=planned, deadline_at='2026-10-07', notes='Keep me'))
        for block_id, task_id, day in [(1, 1, 2), (2, 2, 2), (3, 3, 3), (4, 1, 4)]:
            self.session.add(CalendarBlock(id=block_id, task_id=task_id, category_id=1,
                start_at=datetime(2026, 10, day, 12), end_at=datetime(2026, 10, day, 13)))
        self.session.commit()
        self.body = RemoveTaskDayRequest(taskId=1, plannedDate='2026-10-02')

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_removes_only_day_plan_and_bookings_preserving_tree_and_future(self):
        result = remove_task_day(self.body, self.session)
        self.assertEqual(result['plannedDates'], {'1': None, '2': None})
        self.assertEqual(result['removedBlockIds'], [1, 2])
        self.assertEqual(len(self.session.exec(select(StrategyNode)).all()), 3)
        future = self.session.get(StrategyNode, 3)
        self.assertEqual((future.planned_date, future.parent_id, future.notes), ('2026-10-03', 1, 'Keep me'))
        self.assertEqual(self.session.get(StrategyNode, 1).deadline_at, '2026-10-07')
        self.assertEqual([b.id for b in self.session.exec(select(CalendarBlock)).all()], [3, 4])
        self.assertEqual(remove_task_day(self.body, self.session)['removedBlockIds'], [])

    def test_database_failure_rolls_back_day_and_subtask_bookings(self):
        with patch.object(self.session, 'commit', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                remove_task_day(self.body, self.session)
        self.session.rollback()
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-02')
        self.assertIsNotNone(self.session.get(CalendarBlock, 2))

    def test_google_failure_preserves_local_task_and_booking(self):
        with patch('app.api.calendar_blocks.delete_google_event_for_block', return_value='offline'):
            with self.assertRaises(HTTPException) as error:
                remove_task_day(self.body, self.session)
        self.assertEqual(error.exception.status_code, 502)
        self.assertIsNotNone(self.session.get(CalendarBlock, 1))
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-02')

    def test_recurring_removed_day_has_durable_cancellation(self):
        block = self.session.get(CalendarBlock, 1)
        block.recurrence_series_id, block.recurrence_key = 'series-test', '2026-10-02'
        self.session.add(block)
        self.session.commit()
        remove_task_day(self.body, self.session)
        exception = self.session.exec(select(RecurrenceOccurrenceException)).one()
        self.assertEqual(exception.disposition, 'cancelled')

    def test_multiday_interval_preserves_before_and_after_removed_day(self):
        block = self.session.get(CalendarBlock, 1)
        block.start_at, block.end_at = datetime(2026, 10, 1, 22), datetime(2026, 10, 3, 2)
        self.session.add(block)
        self.session.commit()
        result = remove_task_day(self.body, self.session)
        updated = result['updatedBlocks']
        self.assertEqual(len(updated), 2)
        intervals = sorted((b['startAt'], b['endAt']) for b in updated)
        self.assertEqual(intervals, [('2026-10-01T22:00:00', '2026-10-02T00:00:00'), ('2026-10-03T00:00:00', '2026-10-03T02:00:00')])
        self.assertEqual(self.session.get(CalendarBlock, 1).end_at, datetime(2026, 10, 2))

    def test_other_day_plan_survives_removal_of_todays_booking(self):
        task = self.session.get(StrategyNode, 1)
        task.planned_date = '2026-10-04'
        self.session.add(task)
        self.session.commit()
        remove_task_day(self.body, self.session)
        self.assertEqual(self.session.get(StrategyNode, 1).planned_date, '2026-10-04')


if __name__ == '__main__':
    unittest.main()
