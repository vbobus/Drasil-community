from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.agent_kernel.change_set_deltas import apply_change_set_delta
from app.services.agent_kernel.contracts import (
    ChangeSet,
    ChangeSetOperationRemove,
    ChangeSetRevisionDelta,
    EntityRef,
    TaskArchiveOperation,
    TaskDeleteOperation,
)


def draft() -> ChangeSet:
    return ChangeSet(
        change_set_id="changeset:1",
        goal_id="goal:1",
        operations=[
            TaskArchiveOperation(
                operation_id="op:archive",
                task_refs=[EntityRef(kind="task", entity_id="task:1")],
            ),
            TaskDeleteOperation(
                operation_id="op:delete",
                task_refs=[EntityRef(kind="task", entity_id="task:2")],
            ),
        ],
        idempotency_key="idem:1",
        correlation_id="corr:1",
        causation_id="turn:1",
    )


class AgentConflictDecisionTests(unittest.TestCase):
    def test_exact_delta_creates_next_draft_and_invalidates_simulation(self) -> None:
        current = draft().model_copy(
            update={"status": "ready_for_review", "simulation_result_ref": "simulation:1"}
        )
        current = ChangeSet.model_validate(current.model_dump())
        revised = apply_change_set_delta(
            current,
            ChangeSetRevisionDelta(
                expected_change_set_revision=1,
                edits=[
                    ChangeSetOperationRemove(
                        edit_id="edit:1", operation_id="op:delete"
                    )
                ],
            ),
            idempotency_key="decision:event:1",
            correlation_id="corr:2",
            causation_id="decision:event:1",
        )
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.status, "draft")
        self.assertIsNone(revised.simulation_result_ref)
        self.assertEqual([item.operation_id for item in revised.operations], ["op:archive"])

    def test_delta_rejects_stale_or_absent_targets(self) -> None:
        for revision, operation_id in ((2, "op:archive"), (1, "op:missing")):
            with self.assertRaises((ValueError, ValidationError)):
                apply_change_set_delta(
                    draft(),
                    ChangeSetRevisionDelta(
                        expected_change_set_revision=revision,
                        edits=[
                            ChangeSetOperationRemove(
                                edit_id="edit:bad", operation_id=operation_id
                            )
                        ],
                    ),
                    idempotency_key="decision:event:bad",
                    correlation_id="corr:2",
                    causation_id="decision:event:bad",
                )


if __name__ == "__main__":
    unittest.main()
