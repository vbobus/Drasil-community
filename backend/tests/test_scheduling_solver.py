import unittest
from datetime import datetime

from app.services.scheduling_solver import (
    OccupiedInterval,
    SolverTask,
    solve_schedule_draft,
)


class SchedulingSolverTests(unittest.TestCase):
    def test_hard_allowed_start_window_is_not_treated_as_a_soft_preference(self) -> None:
        result = solve_schedule_draft(
            tasks=[
                SolverTask(
                    task_id=91,
                    duration_minutes=30,
                    allowed_start_windows=((
                        datetime(2026, 7, 17, 15, 0),
                        datetime(2026, 7, 17, 15, 30),
                    ),),
                )
            ],
            window_start=datetime(2026, 7, 17, 14, 0),
            window_end=datetime(2026, 7, 17, 17, 0),
            occupied=[],
            slot_minutes=5,
        )

        self.assertEqual(result.placements[0].start_at, datetime(2026, 7, 17, 15, 0))

        blocked = solve_schedule_draft(
            tasks=[
                SolverTask(
                    task_id=91,
                    duration_minutes=30,
                    allowed_start_windows=((
                        datetime(2026, 7, 17, 15, 0),
                        datetime(2026, 7, 17, 15, 30),
                    ),),
                )
            ],
            window_start=datetime(2026, 7, 17, 14, 0),
            window_end=datetime(2026, 7, 17, 17, 0),
            occupied=[
                OccupiedInterval(
                    block_id=1,
                    start_at=datetime(2026, 7, 17, 15, 0),
                    end_at=datetime(2026, 7, 17, 15, 30),
                )
            ],
            slot_minutes=5,
        )
        self.assertEqual(blocked.placements, [])
        self.assertEqual(blocked.conflicts[0].type, "request_constraint")

    def test_solver_preserves_task_order_and_avoids_occupied_intervals(self) -> None:
        result = solve_schedule_draft(
            tasks=[
                SolverTask(task_id=1, duration_minutes=30),
                SolverTask(task_id=2, duration_minutes=45),
            ],
            window_start=datetime(2026, 6, 18, 9, 0),
            window_end=datetime(2026, 6, 18, 11, 0),
            occupied=[
                OccupiedInterval(
                    block_id=44,
                    start_at=datetime(2026, 6, 18, 9, 30),
                    end_at=datetime(2026, 6, 18, 10, 0),
                )
            ],
            slot_minutes=15,
        )

        self.assertEqual([placement.task_id for placement in result.placements], [1, 2])
        self.assertEqual(result.placements[0].start_at, datetime(2026, 6, 18, 9, 0))
        self.assertEqual(result.placements[0].end_at, datetime(2026, 6, 18, 9, 30))
        self.assertEqual(result.placements[1].start_at, datetime(2026, 6, 18, 10, 0))
        self.assertEqual(result.placements[1].end_at, datetime(2026, 6, 18, 10, 45))
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.solver_status, "feasible")

    def test_solver_preserves_requested_gap_between_ordered_tasks(self) -> None:
        result = solve_schedule_draft(
            tasks=[
                SolverTask(task_id=1, duration_minutes=60, gap_after_minutes=30),
                SolverTask(task_id=2, duration_minutes=30),
            ],
            window_start=datetime(2026, 7, 13, 12, 0),
            window_end=datetime(2026, 7, 13, 16, 0),
            occupied=[],
            slot_minutes=15,
        )

        self.assertEqual(result.solver_status, "feasible")
        self.assertEqual(result.placements[0].start_at, datetime(2026, 7, 13, 12, 0))
        self.assertEqual(result.placements[0].end_at, datetime(2026, 7, 13, 13, 0))
        self.assertEqual(result.placements[1].start_at, datetime(2026, 7, 13, 13, 30))
        self.assertEqual(result.placements[1].end_at, datetime(2026, 7, 13, 14, 0))

    def test_solver_returns_structured_conflict_when_window_is_full(self) -> None:
        result = solve_schedule_draft(
            tasks=[SolverTask(task_id=7, duration_minutes=60)],
            window_start=datetime(2026, 6, 18, 9, 0),
            window_end=datetime(2026, 6, 18, 10, 0),
            occupied=[
                OccupiedInterval(
                    block_id=99,
                    start_at=datetime(2026, 6, 18, 9, 0),
                    end_at=datetime(2026, 6, 18, 10, 0),
                )
            ],
            slot_minutes=15,
        )

        self.assertEqual(result.placements, [])
        self.assertEqual(result.solver_status, "conflict")
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.task_id, 7)
        self.assertEqual(conflict.type, "occupied_slot")
        self.assertEqual(conflict.conflicting_block_ids, [99])
        self.assertEqual(conflict.alternatives[0].start_at, datetime(2026, 6, 18, 10, 0))
        self.assertEqual(conflict.alternatives[0].end_at, datetime(2026, 6, 18, 11, 0))

    def test_conflict_alternative_respects_required_gap_after_existing_calendar_block(self) -> None:
        result = solve_schedule_draft(
            tasks=[
                SolverTask(
                    task_id=42,
                    duration_minutes=60,
                    gap_before_minutes=15,
                    gap_after_minutes=15,
                )
            ],
            window_start=datetime(2026, 7, 13, 13, 0),
            window_end=datetime(2026, 7, 13, 14, 0),
            alternative_window_end=datetime(2026, 7, 14, 0, 0),
            occupied=[
                OccupiedInterval(
                    block_id=21,
                    start_at=datetime(2026, 7, 13, 13, 30),
                    end_at=datetime(2026, 7, 13, 14, 0),
                    source_label="review launch plan",
                )
            ],
            slot_minutes=15,
        )

        self.assertEqual(result.solver_status, "conflict")
        self.assertEqual(result.placements, [])
        self.assertEqual(result.conflicts[0].conflicting_block_ids, [21])
        alternative = result.conflicts[0].alternatives[0]
        self.assertEqual(alternative.start_at, datetime(2026, 7, 13, 14, 15))
        self.assertEqual(alternative.end_at, datetime(2026, 7, 13, 15, 15))
        self.assertIn("required 15-minute task gap", alternative.reason)

    def test_trailing_gap_does_not_delay_task_after_an_existing_block(self) -> None:
        result = solve_schedule_draft(
            tasks=[SolverTask(task_id=42, duration_minutes=30, gap_after_minutes=30)],
            window_start=datetime(2026, 7, 13, 10, 0),
            window_end=datetime(2026, 7, 13, 11, 0),
            occupied=[
                OccupiedInterval(
                    block_id=21,
                    start_at=datetime(2026, 7, 13, 9, 0),
                    end_at=datetime(2026, 7, 13, 10, 0),
                )
            ],
            slot_minutes=15,
        )

        self.assertEqual(result.solver_status, "feasible")
        self.assertEqual(result.placements[0].start_at, datetime(2026, 7, 13, 10, 0))
        self.assertEqual(result.placements[0].end_at, datetime(2026, 7, 13, 10, 30))

    def test_overlapping_padded_calendar_intervals_do_not_hide_later_openings(self) -> None:
        result = solve_schedule_draft(
            tasks=[
                SolverTask(
                    task_id=149,
                    duration_minutes=5,
                    gap_before_minutes=15,
                    gap_after_minutes=15,
                )
            ],
            window_start=datetime(2026, 7, 16, 9, 0),
            window_end=datetime(2026, 7, 16, 21, 0),
            occupied=[
                OccupiedInterval(
                    block_id=25,
                    start_at=datetime(2026, 7, 16, 8, 0),
                    end_at=datetime(2026, 7, 16, 11, 30),
                ),
                OccupiedInterval(
                    block_id=26,
                    start_at=datetime(2026, 7, 16, 11, 45),
                    end_at=datetime(2026, 7, 16, 13, 15),
                ),
                OccupiedInterval(
                    block_id=27,
                    start_at=datetime(2026, 7, 16, 13, 30),
                    end_at=datetime(2026, 7, 16, 14, 0),
                ),
            ],
            slot_minutes=5,
        )

        self.assertEqual(result.solver_status, "feasible")
        self.assertEqual(result.placements[0].start_at, datetime(2026, 7, 16, 14, 15))
        self.assertEqual(result.placements[0].end_at, datetime(2026, 7, 16, 14, 20))

    def test_conflicts_do_not_expose_internal_draft_interval_ids(self) -> None:
        result = solve_schedule_draft(
            tasks=[
                SolverTask(task_id=11, duration_minutes=45),
                SolverTask(task_id=12, duration_minutes=45),
            ],
            window_start=datetime(2026, 6, 18, 9, 0),
            window_end=datetime(2026, 6, 18, 10, 0),
            occupied=[],
            slot_minutes=15,
        )

        self.assertEqual([placement.task_id for placement in result.placements], [11])
        self.assertEqual(result.solver_status, "partial")
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].task_id, 12)
        self.assertEqual(result.conflicts[0].conflicting_block_ids, [])


if __name__ == "__main__":
    unittest.main()
