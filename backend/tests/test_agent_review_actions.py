from __future__ import annotations

import unittest
import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sqlmodel import Session, SQLModel, create_engine, select

from app.services.agent_kernel.contracts import (
    ApproveReviewAction,
    DecisionReviewAction,
    DecisionDelta,
    DecisionRequest,
    EditReviewAction,
    ExecutionOutcome,
    GoalState,
    RejectReviewAction,
    ReviewActionBinding,
    RetryReviewAction,
)
from app.services.agent_kernel.review_actions import ReviewActionCoordinator
from app.api.review_actions import execute_review_action
from app.models.agent_execution import AgentReviewActionRecord
from app.services.agent_kernel.contracts.goal_patches import GoalStatePatch
from app.services.agent_kernel.contracts.references import Provenance
from app.services.principal_auth import AuthenticatedPrincipal, resolve_authenticated_principal
from app.main import safe_request_validation_error
from fastapi.exceptions import RequestValidationError


def binding() -> ReviewActionBinding:
    return ReviewActionBinding(
        session_id="session:one",
        goal_id="goal:one",
        goal_revision=4,
        change_set_id="changeset:one",
        change_set_revision=3,
        simulation_id="simulation:one",
        simulation_input_fingerprint="a" * 64,
        simulation_output_fingerprint="b" * 64,
        correlation_id="correlation:one",
        causation_id="causation:one",
    )


class FakeGoalStore:
    def __init__(self) -> None:
        self.goal = GoalState(
            goal_id="goal:one",
            revision=4,
            status="ready_for_review",
            original_request="Review this plan.",
            current_change_set_id="changeset:one",
            current_change_set_revision=3,
            correlation_id="correlation:one",
            causation_id="causation:one",
        )

    def load(self, session_id: str, goal_id: str):
        if session_id != "session:one" or goal_id != "goal:one":
            raise ValueError("not found")
        return self.goal

    def save(self, _session_id, state, **_kwargs):
        self.goal = state
        return state


class FakeChangeStore:
    def __init__(self) -> None:
        self.change_set = SimpleNamespace(
            change_set_id="changeset:one",
            goal_id="goal:one",
            revision=3,
            status="ready_for_review",
            simulation_result_ref="simulation:one",
            correlation_id="correlation:one",
            causation_id="causation:change-set",
            operations=[SimpleNamespace(kind="task.update")],
        )

    def load(self, session_id: str, change_set_id: str):
        if session_id != "session:one" or change_set_id != "changeset:one":
            raise ValueError("not found")
        return self.change_set

    def load_current(self, session_id: str, goal_id: str):
        return self.change_set if session_id == "session:one" and goal_id == "goal:one" else None

    def transition_status(self, _session_id, _change_set_id, **_kwargs):
        self.change_set.status = "rejected"
        return self.change_set

    def invalidate_review(self, _session_id, _change_set_id, **_kwargs):
        self.change_set.status = "draft"
        self.change_set.simulation_result_ref = None
        return self.change_set


class FakeSimulationStore:
    def load(self, session_id: str, simulation_id: str):
        if session_id != "session:one" or simulation_id != "simulation:one":
            raise ValueError("not found")
        return SimpleNamespace(
            simulation_id="simulation:one",
            resulting_goal_revision=4,
            change_set_id="changeset:one",
            change_set_revision=3,
            input_snapshot=SimpleNamespace(fingerprint="a" * 64),
            output_fingerprint="b" * 64,
            correlation_id="correlation:one",
            causation_id="causation:one",
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        return ExecutionOutcome.model_construct(status="ok", result=SimpleNamespace(
            session_id="session:one",
            goal_id="goal:one",
            goal_revision=5,
            change_set_id="changeset:one",
            change_set_revision=3,
        ), error=None)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.added = []

    def begin_nested(self):
        return nullcontext()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def exec(self, _statement):
        entity = _statement.column_descriptions[0].get("entity")
        if entity is AgentReviewActionRecord:
            receipt = next(
                (item for item in reversed(self.added) if isinstance(item, AgentReviewActionRecord)),
                None,
            )
            return SimpleNamespace(first=lambda: receipt)
        return SimpleNamespace(first=lambda: None)

    def add(self, value) -> None:
        self.added.append(value)


class FakeGoalCoordinator:
    def __init__(self) -> None:
        self.calls = []

    def apply_ui_decision(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(revision=5, current_change_set_revision=3)


def action_identity(
    action: str,
    *,
    action_binding: ReviewActionBinding | None = None,
    decision_id: str | None = None,
    option_id: str | None = None,
    execution_id: str | None = None,
) -> dict[str, str | int]:
    value = action_binding or binding()
    review_id = ReviewActionCoordinator._stable_id(
        "review",
        value.goal_id,
        value.goal_revision,
        value.change_set_id,
        value.change_set_revision,
        value.simulation_id,
    )
    if action == "decision":
        parts = (decision_id, option_id)
    elif action == "retry":
        parts = ("retry", execution_id)
    else:
        parts = (action,)
    result: dict[str, str | int] = {
        "event_id": ReviewActionCoordinator._stable_id("event", review_id, *parts),
        "idempotency_key": ReviewActionCoordinator._stable_id("idem", review_id, *parts),
        "review_id": review_id,
        "review_revision": 1,
    }
    if action == "approve":
        result["command_id"] = ReviewActionCoordinator._stable_id(
            "command", review_id, "approve"
        )
    return result


class ReviewActionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession()
        self.runtime = FakeRuntime()
        self.goal_coordinator = FakeGoalCoordinator()
        self.coordinator = ReviewActionCoordinator(
            self.session,
            goal_store=FakeGoalStore(),
            change_set_store=FakeChangeStore(),
            simulation_store=FakeSimulationStore(),
            goal_coordinator=self.goal_coordinator,
            transaction_runtime=self.runtime,
            execution_loader=lambda execution_id: (
                SimpleNamespace(
                    execution_id="execution:one",
                    session_id="session:one",
                    command_id=action_identity("approve")["command_id"],
                    idempotency_key=action_identity("approve")["idempotency_key"],
                    goal_id="goal:one",
                    goal_revision=6,
                    change_set_id="changeset:one",
                    change_set_revision=3,
                    simulation_id="simulation:one",
                )
                if execution_id == "execution:one"
                else None
            ),
        )

    def test_approve_constructs_exact_server_command(self) -> None:
        action = ApproveReviewAction(
            **action_identity("approve"),
            binding=binding(),
        )
        outcome = self.coordinator.execute(authenticated_session_id="session:one", action=action)

        self.assertEqual(outcome.status, "ok")
        command = self.runtime.commands[0]
        self.assertEqual(command.command_id, action.command_id)
        self.assertEqual(command.idempotency_key, action.idempotency_key)
        self.assertEqual(command.session_id, "session:one")
        self.assertEqual(command.expected_goal_revision, 4)
        self.assertEqual(command.expected_change_set_revision, 3)
        self.assertEqual(command.simulation_input_fingerprint, "a" * 64)
        self.assertEqual(command.simulation_output_fingerprint, "b" * 64)

    def test_compound_native_review_uses_one_transaction_authority(self) -> None:
        self.coordinator.change_set_store.change_set.operations.append(
            SimpleNamespace(kind="calendar.move_block")
        )
        action = ApproveReviewAction(
            **action_identity("approve"),
            binding=binding(),
        )
        outcome = self.coordinator.execute(
            authenticated_session_id="session:one",
            action=action,
        )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(len(self.runtime.commands), 1)

    def test_runtime_error_is_not_reported_as_success(self) -> None:
        self.coordinator.transaction_runtime.execute = Mock(return_value=SimpleNamespace(
            status="error",
            result=None,
            error=SimpleNamespace(
                code="STALE_WORLD",
                user_summary="The reviewed world state changed.",
                retryable=False,
            ),
        ))
        action = ApproveReviewAction(
            **action_identity("approve"),
            binding=binding(),
        )
        outcome = self.coordinator.execute(
            authenticated_session_id="session:one",
            action=action,
        )
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error.code, "stale_review")
        self.assertEqual(outcome.error.recovery, "refresh")

    def test_retry_rejects_browser_identity_that_does_not_match_execution_record(self) -> None:
        action = RetryReviewAction(
            **action_identity("retry", execution_id="execution:one"),
            binding=binding(),
            execution_id="execution:one",
            original_command_id="command:attacker-selected",
            original_idempotency_key="idem:attacker-selected",
        )
        outcome = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        self.assertEqual(outcome.error.code, "invalid_action")
        self.assertEqual(self.runtime.commands, [])

    def test_stale_and_cross_session_actions_fail_before_runtime(self) -> None:
        stale_binding = binding().model_copy(update={"goal_revision": 2})
        action = ApproveReviewAction(
            **action_identity("approve", action_binding=stale_binding),
            binding=stale_binding,
        )
        stale = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        denied = self.coordinator.execute(authenticated_session_id="session:other", action=action)
        self.assertEqual(stale.error.code, "stale_review")
        self.assertEqual(denied.error.code, "authorization_denied")
        self.assertEqual(self.runtime.commands, [])

    def test_reject_transitions_exact_goal_and_change_set_without_runtime(self) -> None:
        action = RejectReviewAction(
            **action_identity("reject"),
            binding=binding(),
        )
        outcome = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        self.assertEqual(outcome.result.status, "rejected")
        self.assertEqual(outcome.result.goal_revision, 5)
        self.assertEqual(self.runtime.commands, [])
        self.assertEqual(self.session.commits, 1)

    def test_retry_reuses_original_command_and_idempotency_identity(self) -> None:
        # A retry belongs to an already accepted canonical command and reuses
        # its immutable identity.
        action = RetryReviewAction(
            **action_identity("retry", execution_id="execution:one"),
            binding=binding(),
            execution_id="execution:one",
            original_command_id=action_identity("approve")["command_id"],
            original_idempotency_key=action_identity("approve")["idempotency_key"],
        )
        outcome = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        self.assertEqual(outcome.result.status, "retry_replayed")
        command = self.runtime.commands[0]
        self.assertEqual(command.command_id, action.original_command_id)
        self.assertEqual(command.idempotency_key, action.original_idempotency_key)

    def test_edit_is_typed_invalidates_review_and_does_not_execute(self) -> None:
        action = EditReviewAction(
            **action_identity("edit"),
            binding=binding(),
            correction="Move it to Tuesday instead.",
        )
        outcome = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        self.assertEqual(outcome.result.status, "edit_requested")
        self.assertEqual(outcome.result.goal_revision, 5)
        self.assertEqual(self.coordinator.goal_store.goal.status, "planning")
        self.assertEqual(self.coordinator.change_set_store.change_set.status, "draft")
        self.assertIsNone(
            self.coordinator.change_set_store.change_set.simulation_result_ref
        )
        self.assertEqual(outcome.result.correction, "Move it to Tuesday instead.")
        self.assertEqual(self.session.commits, 1)
        self.assertEqual(self.runtime.commands, [])

        replay = self.coordinator.execute(
            authenticated_session_id="session:one",
            action=action,
        )
        self.assertEqual(replay, outcome)
        self.assertEqual(self.session.commits, 1)

    def test_forged_review_control_identity_is_rejected_before_mutation(self) -> None:
        action = RejectReviewAction(
            **{
                **action_identity("reject"),
                "event_id": "event:browser-selected",
            },
            binding=binding(),
        )
        outcome = self.coordinator.execute(
            authenticated_session_id="session:one",
            action=action,
        )
        self.assertEqual(outcome.error.code, "invalid_action")
        self.assertEqual(self.session.commits, 0)
        self.assertEqual(self.coordinator.change_set_store.change_set.status, "ready_for_review")

    def test_decision_dispatches_exact_option_through_goal_coordinator(self) -> None:
        self.coordinator.goal_store.goal = self.coordinator.goal_store.goal.model_copy(update={
            "status": "needs_user_decision",
            "unresolved_decisions": [DecisionRequest(
                decision_id="decision:time",
                goal_id="goal:one",
                goal_revision=4,
                target_revision=5,
                question="Which time?",
                reason="One choice is required.",
                options=[DecisionDelta(
                    option_id="option:morning",
                    label="Morning",
                    patches=[GoalStatePatch(
                        path="conflict.entity_scope",
                        operation="set",
                        value=["task:one"],
                        provenance=Provenance(
                            source_kind="ui",
                            source_ref="event:decision",
                        ),
                    )],
                )],
            )],
        })
        self.coordinator.change_set_store.change_set.status = "needs_user_decision"
        action = DecisionReviewAction(
            **action_identity(
                "decision",
                decision_id="decision:time",
                option_id="option:morning",
            ),
            binding=binding(),
            decision_id="decision:time",
            option_id="option:morning",
        )
        outcome = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        self.assertEqual(outcome.result.status, "decision_recorded")
        self.assertEqual(outcome.result.goal_revision, 5)
        self.assertEqual(self.goal_coordinator.calls, [{
            "session_id": "session:one",
            "goal_id": "goal:one",
            "goal_revision": 4,
            "decision_id": "decision:time",
            "option_id": "option:morning",
            "source_event_id": action.event_id,
            "commit": False,
        }])
        self.assertEqual(self.runtime.commands, [])

    def test_api_route_passes_path_session_as_authenticated_boundary(self) -> None:
        action = EditReviewAction(
            **action_identity("edit"),
            binding=binding(),
        )
        expected = self.coordinator.execute(authenticated_session_id="session:one", action=action)
        fake = Mock()
        fake.execute.return_value = expected
        with patch("app.api.review_actions.ReviewActionCoordinator", return_value=fake):
            api_session = Mock()
            api_session.get.return_value = SimpleNamespace(
                owner_principal_id="principal:one"
            )
            response = execute_review_action(
                session_id="session:one",
                action=action,
                session=api_session,
                principal=AuthenticatedPrincipal(
                    principal_id="principal:one",
                    provider="test",
                ),
            )
        self.assertEqual(response, expected)
        fake.execute.assert_called_once_with(
            authenticated_session_id="session:one",
            action=action,
        )

    def test_api_route_rejects_principal_that_does_not_own_session(self) -> None:
        action = EditReviewAction(
            **action_identity("edit"),
            binding=binding(),
        )
        api_session = Mock()
        api_session.get.return_value = SimpleNamespace(
            owner_principal_id="principal:owner"
        )
        response = execute_review_action(
            session_id="session:one",
            action=action,
            session=api_session,
            principal=AuthenticatedPrincipal(
                principal_id="principal:attacker",
                provider="test",
            ),
        )
        self.assertEqual(response.error.code, "authorization_denied")

    def test_review_request_validation_uses_safe_closed_envelope(self) -> None:
        request = SimpleNamespace(
            url=SimpleNamespace(path="/api/v1/ai/strategist/sessions/one/review-actions")
        )
        response = asyncio.run(
            safe_request_validation_error(request, RequestValidationError([]))
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(payload["error"]["code"], "invalid_action")
        self.assertNotIn("detail", payload)

    def test_principal_is_derived_from_authenticated_provider_credential(self) -> None:
        with (
            patch.dict("os.environ", {"DRASIL_TEST_ISOLATED_DB": "0"}),
            patch("app.services.principal_auth.read_auth_snapshot", return_value={
                "isAuthenticated": True,
                "accountId": "account:secret",
            }),
        ):
            principal = resolve_authenticated_principal()
        self.assertEqual(principal.provider, "chatgpt")
        self.assertTrue(principal.principal_id.startswith("chatgpt:"))
        self.assertNotIn("account:secret", principal.principal_id)

    def test_durable_receipt_replays_nonapproval_outcome_after_commit(self) -> None:
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as database_session:
            coordinator = ReviewActionCoordinator(
                database_session,
                goal_store=FakeGoalStore(),
                change_set_store=FakeChangeStore(),
                simulation_store=FakeSimulationStore(),
                goal_coordinator=FakeGoalCoordinator(),
                transaction_runtime=FakeRuntime(),
            )
            action = EditReviewAction(
                **action_identity("edit"),
                binding=binding(),
                correction="Use Tuesday.",
            )
            first = coordinator.execute(
                authenticated_session_id="session:one",
                action=action,
            )
            second = coordinator.execute(
                authenticated_session_id="session:one",
                action=action,
            )
            self.assertEqual(second, first)
            receipts = database_session.exec(select(AgentReviewActionRecord)).all()
            self.assertEqual(len(receipts), 1)


if __name__ == "__main__":
    unittest.main()
