from __future__ import annotations

import unittest

from pydantic import TypeAdapter, ValidationError

from app.services.agent_kernel.contracts import (
    ApproveReviewAction,
    AssumptionBlock,
    ConflictResolutionBlock,
    ConflictReviewItem,
    DecisionDelta,
    DecisionReviewAction,
    DecisionRequest,
    EntityRef,
    EntityReferencesBlock,
    ExecutionStatusBlock,
    ExternalSyncReviewStatus,
    GroupedChangesBlock,
    RecurrenceSummaryBlock,
    ReviewAction,
    ReviewActionBinding,
    ReviewActionError,
    ReviewChange,
    ReviewChangeGroup,
    ReviewDocument,
    ReviewEntity,
    ScheduleSummaryBlock,
    SemanticDiffBlock,
    SemanticFallbackBlock,
    TaskStructureBlock,
    TaskStructureItem,
    TaskStructureSchedule,
    TaskStructureWorkspace,
    WarningBlock,
)
from app.services.agent_kernel.contracts.goal_patches import GoalStatePatch
from app.services.agent_kernel.contracts.references import Provenance


def binding() -> ReviewActionBinding:
    return ReviewActionBinding(
        session_id="session:review",
        goal_id="goal:review",
        goal_revision=4,
        change_set_id="changeset:review",
        change_set_revision=3,
        simulation_id="simulation:review",
        simulation_input_fingerprint="a" * 64,
        simulation_output_fingerprint="b" * 64,
        correlation_id="correlation:review",
        causation_id="causation:review",
    )


class ReviewContractTests(unittest.TestCase):
    def test_every_review_block_round_trips_as_closed_camel_case_contract(self) -> None:
        task = ReviewEntity(
            ref=EntityRef(kind="task", entity_id="task:42", revision=2, title="Write brief"),
            label="Write brief",
            context="Work",
        )
        change = ReviewChange(
            change_id="change:task-title",
            summary="Rename the task",
            entities=[task],
            before="Draft brief",
            after="Write brief",
        )
        blocks = [
            EntityReferencesBlock(block_id="block:entities", title="Affected work", entities=[task]),
            TaskStructureBlock(
                block_id="block:structure",
                title="Proposed task structure",
                workspaces=[
                    TaskStructureWorkspace(
                        workspace=ReviewEntity(
                            ref=EntityRef(
                                kind="workspace",
                                entity_id="workspace:work",
                                title="Work",
                            ),
                            label="Work",
                        ),
                        roots=[
                            TaskStructureItem(
                                task=task,
                                state="new",
                                estimate_minutes=60,
                                scheduled_blocks=[
                                    TaskStructureSchedule(
                                        range_label=(
                                            "July 20, 2026, 9:00 AM\u201310:00 AM"
                                        ),
                                        duration_minutes=60,
                                    )
                                ],
                            )
                        ],
                    )
                ],
            ),
            GroupedChangesBlock(
                block_id="block:groups",
                title="Changes",
                groups=[ReviewChangeGroup(group_id="group:task", summary="Task details", changes=[change])],
            ),
            SemanticDiffBlock(block_id="block:diff", title="Before and after", changes=[change]),
            ScheduleSummaryBlock(
                block_id="block:schedule",
                title="Schedule",
                cadence="Once",
                range_label="July 20, 2026 from 9:00 AM to 10:00 AM",
                time_zone="America/Vancouver",
            ),
            RecurrenceSummaryBlock(
                block_id="block:recurrence",
                title="Repeats",
                cadence="Every weekday at 9:00 AM",
                range_label="July 20 through August 14, 2026",
                scope_label="This series",
                exceptions=["Skipped July 27"],
                additional_exceptions="Other dates follow the same cadence.",
            ),
            ConflictResolutionBlock(
                block_id="block:conflicts",
                title="Conflicts resolved",
                conflicts=[
                    ConflictReviewItem(
                        conflict_id="conflict:one",
                        summary="Two commitments overlap on July 21.",
                        resolution="Keep the interview fixed and move the flexible task.",
                        affected_entities=[task],
                        alternatives=["Keep the task where it is and choose another interview time."],
                    )
                ],
            ),
            AssumptionBlock(block_id="block:assumptions", title="Assumptions", statements=["The task can move within the workday."]),
            WarningBlock(block_id="block:warning", title="What to know", severity="warning", message="One date needs attention."),
            SemanticFallbackBlock(block_id="block:fallback", title="Other changes", summary="Update related planning details.", effects=[change]),
            ExecutionStatusBlock(
                block_id="block:execution",
                title="Execution status",
                execution_id="execution:one",
                command_id="command:execution",
                idempotency_key="idem:execution",
                status="verification_failed",
                summary="Local changes committed and need inspection.",
                verification_status="verification_failed",
                verification_summary="The fresh read did not match the reviewed result.",
                recovery="manual_inspection",
                sync_items=[
                    ExternalSyncReviewStatus(
                        sync_id=f"sync:{status}",
                        provider_label="Google Calendar",
                        status=status,
                        summary=f"Delivery is {status.replace('_', ' ')}.",
                        attempts=index,
                        error_code=(
                            "SYNC_NOT_CONFIGURED"
                            if status == "not_configured"
                            else "SYNC_PERMANENT"
                            if status == "failed"
                            else None
                        ),
                        recovery=(
                            "reconnect"
                            if status == "not_configured"
                            else "inspect"
                            if status == "failed"
                            else None
                        ),
                    )
                    for index, status in enumerate(
                        ["not_configured", "pending", "delivering", "succeeded", "failed"]
                    )
                ],
            ),
        ]
        document = ReviewDocument(
            review_id="review:one",
            session_id="session:review",
            goal_id="goal:review",
            goal_revision=4,
            change_set_id="changeset:review",
            change_set_revision=3,
            simulation_id="simulation:review",
            simulation_input_fingerprint="a" * 64,
            simulation_output_fingerprint="b" * 64,
            correlation_id="correlation:review",
            causation_id="causation:review",
            summary="Review the proposed plan.",
            blocks=blocks,
            controls=[ApproveReviewAction(
                event_id="event:approve",
                command_id="command:approve",
                idempotency_key="idem:approve",
                review_id="review:one",
                review_revision=1,
                binding=binding(),
            )],
        )

        payload = document.model_dump(by_alias=True, mode="json")
        restored = ReviewDocument.model_validate(payload)
        self.assertEqual(restored, document)
        self.assertIn("changeSetRevision", payload)
        self.assertNotIn("change_set_revision", payload)
        self.assertEqual(len(restored.blocks), 11)

    def test_open_decision_suppresses_approval_controls(self) -> None:
        decision = DecisionRequest(
            decision_id="decision:one",
            goal_id="goal:review",
            goal_revision=4,
            target_revision=5,
            question="Which commitment should stay fixed?",
            reason="Both cannot occupy the same time.",
            affected_entities=[],
            options=[
                DecisionDelta(
                    option_id="option:one",
                    label="Keep the interview",
                    patches=[GoalStatePatch(
                        path="conflict.entity_scope",
                        operation="set",
                        value=["task:interview"],
                        provenance=Provenance(source_kind="ui", source_ref="event:decision"),
                    )],
                )
            ],
        )
        with self.assertRaisesRegex(ValidationError, "open decisions cannot expose approval controls"):
            ReviewDocument(
                review_id="review:one",
                session_id="session:review",
                goal_id="goal:review",
                goal_revision=4,
                change_set_id="changeset:review",
                change_set_revision=3,
                simulation_id="simulation:review",
                simulation_input_fingerprint="a" * 64,
                simulation_output_fingerprint="b" * 64,
                correlation_id="correlation:review",
                causation_id="causation:review",
                summary="A decision is required.",
                blocks=[WarningBlock(block_id="block:decision", title="Decision needed", severity="info", message="Choose one option.")],
                decisions=[decision],
                controls=[ApproveReviewAction(
                    event_id="event:approve",
                    command_id="command:approve",
                    idempotency_key="idem:approve",
                    review_id="review:one",
                    review_revision=1,
                    binding=binding(),
                )],
            )

    def test_action_union_rejects_missing_identity_and_extra_fields(self) -> None:
        adapter = TypeAdapter(ReviewAction)
        with self.assertRaises(ValidationError):
            adapter.validate_python({"action": "approve", "reviewId": "review:one"})
        with self.assertRaises(ValidationError):
            adapter.validate_python({
                "action": "approve",
                "eventId": "event:approve",
                "commandId": "command:approve",
                "idempotencyKey": "idem:approve",
                "reviewId": "review:one",
                "reviewRevision": 1,
                "binding": binding().model_dump(by_alias=True, mode="json"),
                "rawPayload": {},
            })

    def test_display_contracts_are_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            AssumptionBlock(block_id="block:assumptions", title="Assumptions", statements=["x" * 1001])
        with self.assertRaises(ValidationError):
            RecurrenceSummaryBlock(
                block_id="block:recurrence",
                title="Repeats",
                cadence="Daily",
                range_label="Forever",
                scope_label="Series",
                exceptions=[f"Exception {index}" for index in range(13)],
            )

    def test_document_rejects_cross_session_and_invalid_decision_controls(self) -> None:
        decision = DecisionRequest(
            decision_id="decision:one",
            goal_id="goal:review",
            goal_revision=4,
            target_revision=5,
            question="Which time should be used?",
            reason="Only one can be selected.",
            options=[DecisionDelta(
                option_id="option:one",
                label="Morning",
                patches=[GoalStatePatch(
                    path="conflict.entity_scope",
                    operation="set",
                    value=["task:morning"],
                    provenance=Provenance(source_kind="ui", source_ref="event:decision"),
                )],
            )],
        )
        control = DecisionReviewAction(
            event_id="event:decision",
            idempotency_key="idem:decision",
            review_id="review:one",
            review_revision=1,
            binding=binding(),
            decision_id=decision.decision_id,
            option_id="option:missing",
        )
        common = dict(
            review_id="review:one",
            session_id="session:review",
            goal_id="goal:review",
            goal_revision=4,
            change_set_id="changeset:review",
            change_set_revision=3,
            simulation_id="simulation:review",
            simulation_input_fingerprint="a" * 64,
            simulation_output_fingerprint="b" * 64,
            correlation_id="correlation:review",
            causation_id="causation:review",
            summary="Choose a time.",
            blocks=[WarningBlock(block_id="block:decision", title="Decision", severity="info", message="Choose one option.")],
            decisions=[decision],
        )
        with self.assertRaisesRegex(ValidationError, "displayed stored option"):
            ReviewDocument(**common, controls=[control])
        valid_control = control.model_copy(update={"option_id": "option:one"})
        with self.assertRaisesRegex(ValidationError, "binding"):
            ReviewDocument(**{**common, "session_id": "session:other"}, controls=[valid_control])

    def test_review_action_error_policy_is_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "contradicts its code"):
            ReviewActionError(
                code="stale_review",
                summary="This review is stale.",
                recovery="retry",
            )


if __name__ == "__main__":
    unittest.main()
