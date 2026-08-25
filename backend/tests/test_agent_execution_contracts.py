from datetime import datetime, timezone
import unittest

from pydantic import ValidationError

from app.services.agent_kernel.contracts import (
    ApproveAndApplyChangeSetCommand,
    EntityBinding,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionFact,
    ExecutionOutcome,
    ExpectedField,
    ExternalSyncErrorCode,
    ExternalSyncItemStatus,
    LocalExecutionResult,
    VerificationMismatch,
    VerificationResult,
    strict_provider_schema,
)
from app.services.agent_kernel.tool_registry import registered_kernel_tool_specs


def approval_command() -> ApproveAndApplyChangeSetCommand:
    return ApproveAndApplyChangeSetCommand(
        commandId="approve:1",
        idempotencyKey="apply:1",
        sessionId="session:1",
        goalId="goal:1",
        expectedGoalRevision=4,
        changeSetId="changeset:1",
        expectedChangeSetRevision=2,
        simulationId="simulation:1",
        simulationInputFingerprint="a" * 64,
        simulationOutputFingerprint="b" * 64,
        correlationId="correlation:1",
        causationId="turn:1",
    )


class AgentExecutionContractTests(unittest.TestCase):
    def test_approval_command_is_closed_and_strict_provider_compatible(self) -> None:
        command = approval_command()
        self.assertEqual(command.expected_goal_revision, 4)
        schema = strict_provider_schema(ApproveAndApplyChangeSetCommand)
        self.assertFalse(schema["additionalProperties"])
        with self.assertRaises(ValidationError):
            ApproveAndApplyChangeSetCommand.model_validate(
                {**command.model_dump(mode="json", by_alias=True), "modelComment": "apply it"}
            )

    def test_execution_result_requires_verified_success_and_separate_sync_status(self) -> None:
        verification = VerificationResult(
        status="verified",
        intendedFingerprint="c" * 64,
        actualFingerprint="c" * 64,
        verifiedAt=datetime.now(timezone.utc),
    )
        result = LocalExecutionResult(
        executionId="execution:1",
        commandId="approve:1",
        idempotencyKey="apply:1",
        sessionId="session:1",
        goalId="goal:1",
        goalRevision=7,
        changeSetId="changeset:1",
        changeSetRevision=2,
        simulationId="simulation:1",
        status="applied",
        payloadFingerprint="d" * 64,
        bindings=[
            EntityBinding(
                operationId="op:1",
                temporaryId="temp:task:1",
                entityRef={"kind": "task", "entityId": "task:42", "revision": 1},
            )
        ],
        verification=verification,
        syncItems=[
            ExternalSyncItemStatus(
                outboxId="outbox:1",
                provider="google",
                operation="create",
                entityKind="calendar_block",
                entityId="block:9",
                status="pending",
                attempts=0,
            )
        ],
        correlationId="correlation:1",
        causationId="turn:1",
    )
        self.assertEqual(result.status, "applied")
        self.assertEqual(result.sync_items[0].status, "pending")

        with self.assertRaises(ValidationError):
            LocalExecutionResult.model_validate(
                {**result.model_dump(mode="json", by_alias=True), "verification": None}
            )
        oversized_fact = ExecutionFact(
            operationId="operation:large",
            disposition="present",
            entityRef={"kind": "task", "entityId": "task:42"},
            fields=[ExpectedField(field="notes", valueJson="x" * 20_000)],
            fingerprint="f" * 64,
        ).model_dump(mode="json", by_alias=True)
        with self.assertRaises(ValidationError):
            LocalExecutionResult.model_validate(
                {
                    **result.model_dump(mode="json", by_alias=True),
                    "facts": [oversized_fact for _ in range(101)],
                }
            )

    def test_execution_error_uses_closed_safe_codes(self) -> None:
        error = ExecutionError(
        code=ExecutionErrorCode.STALE_CHANGE_SET,
        category="model_repairable",
        retryable=False,
        userSummary="This review is out of date. Generate a fresh review.",
        recovery="resimulate",
    )
        self.assertEqual(error.code, ExecutionErrorCode.STALE_CHANGE_SET)
        with self.assertRaises(ValidationError):
            ExecutionError(
                code="RAW_SQL_EXCEPTION",
                category="fatal_invariant",
                retryable=False,
                userSummary="unsafe",
            )
        with self.assertRaises(ValidationError):
            ExecutionError(
                code=ExecutionErrorCode.SYNC_RETRYABLE,
                category="fatal_invariant",
                retryable=False,
                userSummary="contradictory",
                recovery="none",
            )

    def test_approval_is_not_registered_as_a_model_tool(self) -> None:
        names = {item.name for item in registered_kernel_tool_specs()}
        self.assertNotIn("approve_and_apply_change_set", names)
        self.assertNotIn("apply_change_set", names)

    def test_execution_outcome_never_mix_success_and_error(self) -> None:
        with self.assertRaises(ValidationError):
            ExecutionOutcome(status="ok")

    def test_pending_verification_rejects_mismatches_and_failure_requires_recovery(self) -> None:
        mismatch = VerificationMismatch(
            operationId="op:1",
            expectedFingerprint="e" * 64,
            actualFingerprint="a" * 64,
            summary="Mismatch",
        )
        with self.assertRaises(ValidationError):
            VerificationResult(
                status="pending",
                intendedFingerprint="i" * 64,
                mismatches=[mismatch],
            )
        with self.assertRaises(ValidationError):
            VerificationResult(
                status="verification_failed",
                intendedFingerprint="i" * 64,
                actualFingerprint="a" * 64,
                verifiedAt=datetime.now(timezone.utc),
                mismatches=[mismatch],
            )

    def test_external_sync_status_requires_coherent_delivery_evidence(self) -> None:
        parked = ExternalSyncItemStatus(
            outboxId="outbox:parked",
            provider="google",
            operation="create",
            entityKind="calendar_block",
            entityId="calendar_block:9",
            status="not_configured",
            attempts=1,
            lastErrorCode=ExternalSyncErrorCode.SYNC_NOT_CONFIGURED,
            errorSummary="Connect Google Calendar to deliver this change.",
            recovery="reconnect",
        )
        self.assertIsNone(parked.next_attempt_at)
        self.assertEqual(
            ExternalSyncItemStatus.model_validate_json(parked.model_dump_json()),
            parked,
        )
        with self.assertRaises(ValidationError):
            ExternalSyncItemStatus(
                outboxId="outbox:bad-success",
                provider="google",
                operation="create",
                entityKind="calendar_block",
                entityId="calendar_block:9",
                status="succeeded",
                attempts=1,
                lastErrorCode=ExternalSyncErrorCode.SYNC_PERMANENT,
            )
        with self.assertRaises(ValidationError):
            ExternalSyncItemStatus(
                outboxId="outbox:bad-failure",
                provider="google",
                operation="delete",
                entityKind="calendar_block",
                entityId="calendar_block:9",
                status="failed",
                attempts=1,
            )

    def test_execution_evidence_rejects_ambiguous_or_contradictory_states(self) -> None:
        with self.assertRaises(ValidationError):
            ExecutionFact(
                operationId="operation:delete",
                disposition="absent",
                entityRef={"kind": "task", "entityId": "task:9"},
                fields=[ExpectedField(field="title", valueJson='"gone"')],
                fingerprint="f" * 64,
            )
        with self.assertRaises(ValidationError):
            VerificationMismatch(
                operationId="operation:1",
                expectedFingerprint="e" * 64,
                actualFingerprint="e" * 64,
                summary="Not actually different",
            )
        with self.assertRaises(ValidationError):
            VerificationResult(
                status="verification_failed",
                intendedFingerprint="a" * 64,
                actualFingerprint="a" * 64,
                verifiedAt=datetime.now(timezone.utc),
                mismatches=[
                    VerificationMismatch(
                        operationId="operation:1",
                        expectedFingerprint="e" * 64,
                        actualFingerprint="f" * 64,
                        summary="Different detail",
                    )
                ],
                recovery="manual_inspection",
                recoverySummary="Inspect the affected task.",
            )

    def test_temporary_bindings_are_globally_unique(self) -> None:
        verification = VerificationResult(
            status="verified",
            intendedFingerprint="c" * 64,
            actualFingerprint="c" * 64,
            verifiedAt=datetime.now(timezone.utc),
        )
        with self.assertRaises(ValidationError):
            LocalExecutionResult(
                executionId="execution:duplicate-temp",
                commandId="approve:duplicate-temp",
                idempotencyKey="apply:duplicate-temp",
                sessionId="session:1",
                goalId="goal:1",
                goalRevision=7,
                changeSetId="changeset:1",
                changeSetRevision=2,
                simulationId="simulation:1",
                status="applied",
                payloadFingerprint="d" * 64,
                bindings=[
                    EntityBinding(
                        operationId="operation:1",
                        temporaryId="temp:task:1",
                        entityRef={"kind": "task", "entityId": "task:41", "revision": 1},
                    ),
                    EntityBinding(
                        operationId="operation:2",
                        temporaryId="temp:task:1",
                        entityRef={"kind": "task", "entityId": "task:42", "revision": 1},
                    ),
                ],
                verification=verification,
                correlationId="correlation:1",
                causationId="turn:1",
            )


if __name__ == "__main__":
    unittest.main()
