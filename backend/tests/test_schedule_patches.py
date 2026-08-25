import unittest
from datetime import datetime

from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode
from app.services.schedule_patches import (
    build_schedule_patch,
    simulate_schedule_patch,
    validate_schedule_patch,
)


class SchedulePatchTests(unittest.TestCase):
    def _task(self, task_id: int, category_id: int = 23) -> StrategyNode:
        return StrategyNode(
            id=task_id,
            category_id=category_id,
            title=f"Task {task_id}",
            node_type="task",
            status="todo",
            priority="medium",
            sort_order=0,
        )

    def _create_operation(
        self,
        *,
        task_id: int = 2301,
        category_id: int = 23,
        start_at: str = "2026-06-18T09:00:00",
        end_at: str = "2026-06-18T10:00:00",
    ) -> dict:
        return {
            "op": "create_block",
            "proposalBlockId": f"proposal-{task_id}",
            "taskId": task_id,
            "categoryId": category_id,
            "title": f"Task {task_id}",
            "categoryName": "Work",
            "categoryColor": "#1f6feb",
            "startAt": start_at,
            "endAt": end_at,
            "variant": "proposed",
            "completionStatus": "not_started",
            "source": "solver",
        }

    def test_valid_schedule_patch_simulates_proposed_calendar_blocks(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        patch = build_schedule_patch(
            intent_summary="Schedule the selected work.",
            working_set_task_ids=[2301],
            window_start=window_start,
            window_end=window_end,
            operations=[self._create_operation()],
        )

        validation = validate_schedule_patch(
            patch,
            tasks=[self._task(2301)],
            accepted_blocks=[],
            window_start=window_start,
            window_end=window_end,
        )
        simulation = simulate_schedule_patch(
            patch,
            tasks=[self._task(2301)],
            accepted_blocks=[],
        )

        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["errors"], [])
        self.assertEqual(validation["operationCount"], 1)
        self.assertEqual(simulation["operationCount"], 1)
        self.assertEqual(simulation["affectedTaskIds"], [2301])
        self.assertEqual(simulation["affectedCalendarBlockIds"], [])
        self.assertEqual(len(simulation["previewBlocks"]), 1)
        self.assertEqual(simulation["previewBlocks"][0]["variant"], "proposed")

    def test_schedule_patch_validation_rejects_accepted_block_collision(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        patch = build_schedule_patch(
            intent_summary="Schedule the selected work.",
            working_set_task_ids=[2301],
            window_start=window_start,
            window_end=window_end,
            operations=[
                self._create_operation(
                    start_at="2026-06-18T09:30:00",
                    end_at="2026-06-18T10:30:00",
                )
            ],
        )
        accepted = CalendarBlock(
            id=7001,
            task_id=999,
            category_id=23,
            start_at=datetime(2026, 6, 18, 9, 0),
            end_at=datetime(2026, 6, 18, 10, 0),
            variant="scheduled",
        )

        validation = validate_schedule_patch(
            patch,
            tasks=[self._task(2301)],
            accepted_blocks=[accepted],
            window_start=window_start,
            window_end=window_end,
        )

        self.assertEqual(validation["status"], "invalid")
        self.assertTrue(
            any("accepted calendar block 7001" in error for error in validation["errors"])
        )

    def test_schedule_patch_validation_rejects_missing_task_and_category_mismatch(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        patch = build_schedule_patch(
            intent_summary="Schedule the selected work.",
            working_set_task_ids=[2301, 2302],
            window_start=window_start,
            window_end=window_end,
            operations=[
                self._create_operation(task_id=2301, category_id=99),
                self._create_operation(task_id=2302, category_id=23),
            ],
        )

        validation = validate_schedule_patch(
            patch,
            tasks=[self._task(2301, category_id=23)],
            accepted_blocks=[],
            window_start=window_start,
            window_end=window_end,
        )

        self.assertEqual(validation["status"], "invalid")
        self.assertTrue(any("category" in error for error in validation["errors"]))
        self.assertTrue(any("missing task 2302" in error for error in validation["errors"]))

    def test_validation_rejects_invalid_window_duplicates_overlaps_and_unsupported_ops(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        first = self._create_operation(task_id=2301)
        duplicate = dict(first, proposalBlockId="proposal-duplicate")
        overlapping = self._create_operation(
            task_id=2302,
            start_at="2026-06-18T09:30:00",
            end_at="2026-06-18T10:30:00",
        )
        patch = build_schedule_patch(
            intent_summary="Invalid proposal.",
            working_set_task_ids=[2301, 2302],
            window_start=window_start,
            window_end=window_end,
            operations=[first, duplicate, overlapping, {"op": "teleport", "taskId": 2302}],
        )
        patch["windowEnd"] = "2026-06-20T00:00:00"

        validation = validate_schedule_patch(
            patch,
            tasks=[self._task(2301), self._task(2302)],
            accepted_blocks=[],
            window_start=window_start,
            window_end=window_end,
        )

        self.assertEqual(validation["status"], "invalid")
        self.assertTrue(any("does not match" in error for error in validation["errors"]))
        self.assertTrue(any("duplicate" in error for error in validation["errors"]))
        self.assertTrue(any("overlap" in error for error in validation["errors"]))
        self.assertTrue(any("unsupported" in error for error in validation["errors"]))

    def test_update_and_remove_operations_require_verified_targets(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        accepted = CalendarBlock(
            id=7010,
            task_id=2301,
            category_id=23,
            start_at=datetime(2026, 6, 18, 9, 0),
            end_at=datetime(2026, 6, 18, 10, 0),
            variant="scheduled",
        )
        update = self._create_operation(
            task_id=2301,
            start_at="2026-06-18T10:00:00",
            end_at="2026-06-18T11:00:00",
        )
        update.update({"op": "update_block_time", "acceptedBlockId": 7010})
        valid_patch = build_schedule_patch(
            intent_summary="Move the block.",
            working_set_task_ids=[2301],
            window_start=window_start,
            window_end=window_end,
            operations=[update],
        )
        valid = validate_schedule_patch(
            valid_patch,
            tasks=[self._task(2301)],
            accepted_blocks=[accepted],
            window_start=window_start,
            window_end=window_end,
        )
        self.assertEqual(valid["status"], "valid")

        invalid_patch = build_schedule_patch(
            intent_summary="Remove an unknown block.",
            working_set_task_ids=[2301],
            window_start=window_start,
            window_end=window_end,
            operations=[{"op": "remove_block", "taskId": 2301, "acceptedBlockId": 9999}],
        )
        invalid = validate_schedule_patch(
            invalid_patch,
            tasks=[self._task(2301)],
            accepted_blocks=[accepted],
            window_start=window_start,
            window_end=window_end,
        )
        self.assertEqual(invalid["status"], "invalid")
        self.assertTrue(any("missing accepted block" in error for error in invalid["errors"]))

    def test_update_rejects_a_block_that_changed_after_review(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        moved_after_review = CalendarBlock(
            id=7011,
            task_id=2301,
            category_id=23,
            start_at=datetime(2026, 6, 18, 9, 30),
            end_at=datetime(2026, 6, 18, 10, 30),
            variant="scheduled",
        )
        operation = self._create_operation(
            task_id=2301,
            start_at="2026-06-18T11:00:00",
            end_at="2026-06-18T12:00:00",
        )
        operation.update(
            {
                "op": "update_block_time",
                "acceptedBlockId": 7011,
                "expectedOriginalStartAt": "2026-06-18T09:00:00",
                "expectedOriginalEndAt": "2026-06-18T10:00:00",
            }
        )
        patch = build_schedule_patch(
            intent_summary="Move the reviewed block.",
            working_set_task_ids=[2301],
            window_start=window_start,
            window_end=window_end,
            operations=[operation],
        )

        validation = validate_schedule_patch(
            patch,
            tasks=[self._task(2301)],
            accepted_blocks=[moved_after_review],
            window_start=window_start,
            window_end=window_end,
        )

        self.assertEqual(validation["status"], "invalid")
        self.assertTrue(
            any("changed after review" in error for error in validation["errors"]),
            validation,
        )

    def test_patch_identity_and_simulation_metadata_are_complete(self) -> None:
        window_start = datetime(2026, 6, 18, 0, 0)
        window_end = datetime(2026, 6, 19, 0, 0)
        first = build_schedule_patch(
            intent_summary="First.",
            working_set_task_ids=[2301],
            window_start=window_start,
            window_end=window_end,
            operations=[self._create_operation(task_id=2301)],
        )
        second = build_schedule_patch(
            intent_summary="Second.",
            working_set_task_ids=[2302],
            window_start=window_start,
            window_end=window_end,
            operations=[self._create_operation(task_id=2302)],
        )
        self.assertNotEqual(first["patchId"], second["patchId"])

        validation = validate_schedule_patch(
            first,
            tasks=[self._task(2301)],
            accepted_blocks=[],
            window_start=window_start,
            window_end=window_end,
        )
        conflicts = [{"taskId": 2301, "type": "occupied_slot", "detail": "Busy."}]
        simulation = simulate_schedule_patch(
            first,
            tasks=[self._task(2301)],
            accepted_blocks=[],
            validation=validation,
            conflicts=conflicts,
        )
        self.assertEqual(simulation["conflicts"], conflicts)
        self.assertEqual(simulation["warnings"], [])


if __name__ == "__main__":
    unittest.main()
