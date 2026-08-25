from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from app.services.agent_kernel.mcp_policy import allowed_mcp_tool_names
from app.services.agent_kernel.planner_tool_contracts import PlannerFrame
from app.services.agent_kernel.subagent_delegation import (
    PlannerSubagentCoordinator,
    SubagentBudget,
    SubagentLoopGuardError,
    SubagentOwnershipError,
    SubagentParentContext,
    create_planner_subagent_task,
)
from app.services import planner_capability


class PlannerSubagentDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 23, 10, 0, 0)
        self.parent = SubagentParentContext(
            parent_agent_id="strategist",
            parent_run_id="run-17",
            session_id="session-4",
            goal_id="goal-9",
            goal_revision=3,
            correlation_id="correlation-2",
        )
        self.budget = SubagentBudget(
            timeout_seconds=30,
            max_exchanges=2,
            max_tool_calls=7,
            max_tokens=8_000,
        )

    def _task(self, **overrides):
        values = {
            "parent": self.parent,
            "objective": "Place the two application tasks today.",
            "task_ids": [21, 22],
            "evidence_handles": ["evidence:session-4:goal-9:turn-2:tool-1"],
            "allowed_tool_names": [
                "get_task_snapshot",
                "list_calendar_blocks",
                "query_preferences",
                "search_capability_catalog",
                "load_skill",
            ],
            "budget": self.budget,
            "now": self.now,
        }
        values.update(overrides)
        return create_planner_subagent_task(**values)

    def test_host_builds_stable_owned_task_with_fail_closed_planner_allowlist(self) -> None:
        first = self._task()
        second = self._task()
        reduced_budget = self._task(
            budget=self.budget.model_copy(update={"timeout_seconds": 20})
        )

        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(first.task_id, reduced_budget.task_id)
        self.assertNotEqual(
            first.content_fingerprint,
            reduced_budget.content_fingerprint,
        )
        self.assertEqual(first.cancellation_id, second.cancellation_id)
        self.assertEqual(first.parent.parent_agent_id, "strategist")
        self.assertEqual(first.assigned_agent_id, "planner")
        self.assertEqual(first.parent.goal_id, "goal-9")
        self.assertEqual(first.parent.goal_revision, 3)
        self.assertLessEqual(
            set(first.allowed_tool_names),
            allowed_mcp_tool_names("planner"),
        )
        self.assertNotIn("handoff_to_planner", first.allowed_tool_names)
        self.assertNotIn("set_goal_transition", first.allowed_tool_names)

        with self.assertRaises(SubagentOwnershipError):
            self._task(allowed_tool_names=["handoff_to_planner"])

    def test_coordinator_returns_typed_completed_artifact_and_safe_events(self) -> None:
        events: list[tuple[str, dict]] = []
        coordinator = PlannerSubagentCoordinator(
            now=lambda: self.now,
            event_sink=lambda name, payload: events.append((name, payload)),
        )
        task = self._task()

        result = coordinator.run(
            task,
            lambda current: {
                "planner_executed": True,
                "planner_mode": "proposal",
                "proposal_ready": True,
                "planner_result": {"proposal": {"id": "proposal-1"}},
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": 3,
                    "tokens": 1_200,
                },
            },
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.task_id, task.task_id)
        self.assertEqual(result.artifact["planner_result"]["proposal"]["id"], "proposal-1")
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual(
            [name for name, _payload in events],
            ["planner_subagent_started", "planner_subagent_completed"],
        )
        self.assertTrue(all("objective" not in payload for _name, payload in events))
        self.assertTrue(all("artifact" not in payload for _name, payload in events))
        completed_event = events[-1][1]
        self.assertEqual(completed_event["token_budget"], task.budget.max_tokens)
        self.assertEqual(completed_event["token_over_budget_by"], 0)

    def test_over_budget_event_records_the_exact_token_overage(self) -> None:
        """Intent: release diagnostics must explain token-budget failures exactly."""

        events: list[tuple[str, dict]] = []
        coordinator = PlannerSubagentCoordinator(
            now=lambda: self.now,
            event_sink=lambda name, payload: events.append((name, payload)),
        )
        task = self._task()

        result = coordinator.run(
            task,
            lambda _current: {
                "status": "completed",
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": 1,
                    "tokens": task.budget.max_tokens + 321,
                },
            },
        )

        self.assertEqual(result.reason_code, "subagent_token_budget_exceeded")
        completed_event = events[-1][1]
        self.assertEqual(completed_event["token_count"], task.budget.max_tokens)
        self.assertEqual(completed_event["token_budget"], task.budget.max_tokens)
        self.assertEqual(completed_event["token_over_budget_by"], 321)

    def test_invalid_executor_result_records_bounded_failure_diagnostics(self) -> None:
        """Intent: a generic subagent failure must remain actionable in the ledger."""

        events: list[tuple[str, dict]] = []
        coordinator = PlannerSubagentCoordinator(
            now=lambda: self.now,
            event_sink=lambda name, payload: events.append((name, payload)),
        )
        task = self._task()

        result = coordinator.run(
            task,
            lambda _current: (_ for _ in ()).throw(
                RuntimeError("planner frame was malformed")
            ),
        )

        self.assertEqual(result.reason_code, "subagent_result_invalid")
        completed_event = events[-1][1]
        self.assertEqual(
            completed_event["failure_stage"],
            "executor_or_result_normalization",
        )
        self.assertEqual(completed_event["exception_type"], "RuntimeError")
        self.assertEqual(
            completed_event["exception_detail"],
            "planner frame was malformed",
        )
        self.assertNotIn("objective", completed_event)

    def test_repeated_task_id_and_recursive_ownership_fail_closed(self) -> None:
        coordinator = PlannerSubagentCoordinator(now=lambda: self.now)
        task = self._task()
        coordinator.run(task, lambda _current: {"status": "infeasible"})

        with self.assertRaises(SubagentLoopGuardError):
            coordinator.run(task, lambda _current: {"status": "completed"})
        changed_content_reuse = self._task(
            objective="A changed delegated objective.",
            task_id=task.task_id,
        )
        with self.assertRaises(SubagentLoopGuardError):
            coordinator.run(
                changed_content_reuse,
                lambda _current: {"status": "completed"},
            )

        recursive_parent = self.parent.model_copy(
            update={"parent_agent_id": "planner"}
        )
        with self.assertRaises(SubagentOwnershipError):
            recursive = self._task(parent=recursive_parent)
            coordinator.run(recursive, lambda _current: {"status": "completed"})

    def test_one_strategic_callback_per_decision_id(self) -> None:
        coordinator = PlannerSubagentCoordinator(now=lambda: self.now)
        task = self._task()
        initial = coordinator.run(
            task,
            lambda _current: {
                "status": "needs_strategic_decision",
                "strategic_decision": {
                    "decision_id": "model-authored-id-is-ignored",
                    "question": "May the lower-priority flexible block move?",
                    "options": ["move_blocker", "move_incoming"],
                },
            },
        )
        self.assertEqual(initial.status, "needs_strategic_decision")
        self.assertIsNotNone(initial.decision_request)
        decision_id = initial.decision_request.decision_id
        self.assertTrue(decision_id.startswith("planner-decision:"))
        self.assertNotEqual(decision_id, "model-authored-id-is-ignored")

        resumed = coordinator.respond_to_strategic_decision(
            task_id=task.task_id,
            decision_id=decision_id,
            selected_option="move_blocker",
            executor=lambda _task, _decision, _option: {
                "status": "completed",
                "planner_result": {"proposal": {"id": "proposal-after-decision"}},
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": 1,
                    "tokens": 300,
                },
            },
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.usage.exchanges, 2)

        with self.assertRaises(SubagentLoopGuardError):
            coordinator.respond_to_strategic_decision(
                task_id=task.task_id,
                decision_id=decision_id,
                selected_option="move_incoming",
                executor=lambda *_args: {"status": "completed"},
            )

    def test_task_allows_only_one_callback_even_if_provider_rotates_decision_ids(self) -> None:
        coordinator = PlannerSubagentCoordinator(now=lambda: self.now)
        task = self._task(
            budget=self.budget.model_copy(update={"max_exchanges": 4})
        )
        initial = coordinator.run(
            task,
            lambda _current: {
                "status": "needs_strategic_decision",
                "strategic_decision": {
                    "decision_id": "provider-one",
                    "question": "First choice?",
                    "options": ["a", "b"],
                },
            },
        )
        first_id = initial.decision_request.decision_id
        rotated = coordinator.respond_to_strategic_decision(
            task_id=task.task_id,
            decision_id=first_id,
            selected_option="a",
            executor=lambda _task, _decision, _option: {
                "status": "needs_strategic_decision",
                "strategic_decision": {
                    "decision_id": "provider-two",
                    "question": "Second choice?",
                    "options": ["c", "d"],
                },
            },
        )
        self.assertEqual(rotated.status, "needs_strategic_decision")
        with self.assertRaises(SubagentLoopGuardError):
            coordinator.respond_to_strategic_decision(
                task_id=task.task_id,
                decision_id=rotated.decision_request.decision_id,
                selected_option="c",
                executor=lambda *_args: {"status": "completed"},
            )

    def test_cancel_deadline_and_budget_fail_before_or_after_execution(self) -> None:
        cancelled = PlannerSubagentCoordinator(now=lambda: self.now)
        cancelled_task = self._task()
        cancelled.cancel(cancelled_task.cancellation_id)
        calls = 0

        def should_not_run(_task):
            nonlocal calls
            calls += 1
            return {"status": "completed"}

        cancelled_result = cancelled.run(cancelled_task, should_not_run)
        self.assertEqual(cancelled_result.status, "failed")
        self.assertEqual(cancelled_result.reason_code, "subagent_cancelled")
        self.assertEqual(calls, 0)

        expired = PlannerSubagentCoordinator(
            now=lambda: self.now + timedelta(seconds=31)
        )
        expired_result = expired.run(self._task(), should_not_run)
        self.assertEqual(expired_result.status, "retryable_failure")
        self.assertEqual(expired_result.reason_code, "subagent_deadline_exceeded")
        self.assertEqual(calls, 0)

        over_budget = PlannerSubagentCoordinator(now=lambda: self.now)
        budget_result = over_budget.run(
            self._task(),
            lambda _task: {
                "status": "completed",
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": 8,
                    "tokens": 1_000,
                },
            },
        )
        self.assertEqual(budget_result.status, "failed")
        self.assertEqual(budget_result.reason_code, "subagent_tool_budget_exceeded")

    def test_deadline_and_cancellation_during_execution_override_success(self) -> None:
        clock = [self.now]
        expired = PlannerSubagentCoordinator(now=lambda: clock[0])

        def finish_after_deadline(_task):
            clock[0] = self.now + timedelta(seconds=31)
            return {"status": "completed"}

        expired_result = expired.run(self._task(), finish_after_deadline)
        self.assertEqual(expired_result.status, "retryable_failure")
        self.assertEqual(expired_result.reason_code, "subagent_deadline_exceeded")

        cancelled = PlannerSubagentCoordinator(now=lambda: self.now)
        cancelled_task = self._task(objective="cancel while running")

        def finish_after_cancel(task):
            cancelled.cancel(task.cancellation_id)
            return {"status": "completed"}

        cancelled_result = cancelled.run(cancelled_task, finish_after_cancel)
        self.assertEqual(cancelled_result.status, "failed")
        self.assertEqual(cancelled_result.reason_code, "subagent_cancelled")

    def test_callback_receives_only_remaining_tool_and_token_budget(self) -> None:
        coordinator = PlannerSubagentCoordinator(now=lambda: self.now)
        task = self._task()
        initial = coordinator.run(
            task,
            lambda _current: {
                "status": "needs_strategic_decision",
                "strategic_decision": {
                    "question": "Choose.",
                    "options": ["a", "b"],
                },
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": 5,
                    "tokens": 7_000,
                },
            },
        )

        def callback(remaining_task, _decision, _option):
            self.assertEqual(remaining_task.budget.max_exchanges, 1)
            self.assertEqual(remaining_task.budget.max_tool_calls, 2)
            self.assertEqual(remaining_task.budget.max_tokens, 1_000)
            return {
                "status": "completed",
                "subagent_usage": {
                    "exchanges": 1,
                    "tool_calls": 2,
                    "tokens": 1_000,
                },
            }

        result = coordinator.respond_to_strategic_decision(
            task_id=task.task_id,
            decision_id=initial.decision_request.decision_id,
            selected_option="a",
            executor=callback,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.usage.tool_calls, 7)
        self.assertEqual(result.usage.tokens, 8_000)

    def test_malformed_contradictory_or_out_of_scope_results_fail_closed(self) -> None:
        examples = [
            (
                {"status": "needs_strategic_decision"},
                "subagent_result_invalid",
            ),
            (
                {
                    "status": "completed",
                    "subagent_usage": {"tokens": "not-an-integer"},
                },
                "subagent_result_invalid",
            ),
            (
                {"status": "completed", "error": "provider_failed"},
                "subagent_result_contradictory",
            ),
            (
                {
                    "status": "completed",
                    "proposal": {
                        "schedulePatch": {
                            "operations": [{"task_id": 999, "op": "create"}]
                        }
                    },
                },
                "subagent_artifact_scope_violation",
            ),
        ]
        for index, (payload, reason_code) in enumerate(examples):
            with self.subTest(reason_code=reason_code):
                coordinator = PlannerSubagentCoordinator(now=lambda: self.now)
                result = coordinator.run(
                    self._task(objective=f"malformed-{index}"),
                    lambda _task, value=payload: value,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason_code)

    def test_lifecycle_reason_codes_are_bounded_categories(self) -> None:
        events: list[tuple[str, dict]] = []
        coordinator = PlannerSubagentCoordinator(
            now=lambda: self.now,
            event_sink=lambda name, payload: events.append((name, payload)),
        )
        result = coordinator.run(
            self._task(),
            lambda _task: {
                "status": "failed",
                "reason_code": "secret token=credential " * 100,
            },
        )
        self.assertEqual(result.reason_code, "subagent_reported_failure")
        completed = events[-1][1]
        self.assertEqual(completed["reason_code"], "subagent_reported_failure")
        self.assertLessEqual(len(completed["reason_code"]), 64)

    def test_current_planner_modes_map_to_closed_statuses(self) -> None:
        examples = [
            ({"planner_mode": "clarification", "needs_user_input": True}, "needs_user_decision"),
            ({"status": "infeasible"}, "infeasible"),
            (
                {
                    "error": "planner_execution_failed",
                    "planner_error": {"retryable": True},
                },
                "retryable_failure",
            ),
            ({"error": "planner_execution_failed"}, "failed"),
        ]
        for index, (payload, expected) in enumerate(examples):
            with self.subTest(expected=expected):
                coordinator = PlannerSubagentCoordinator(now=lambda: self.now)
                result = coordinator.run(
                    self._task(objective=f"example-{index}"),
                    lambda _task, value=payload: value,
                )
                self.assertEqual(result.status, expected)

    def test_model_backed_planner_rejects_an_unowned_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "owned Planner subagent task"):
            planner_capability.build_planner_schedule_proposal(
                None,  # type: ignore[arg-type]
                prompt="Schedule the selected task.",
                working_set_task_ids=[21],
                window_start=self.now,
                window_end=self.now + timedelta(hours=1),
                slot_minutes=5,
                fallback_duration_minutes=30,
                provider_auth_preflight=True,
            )

    def test_planner_contract_has_a_typed_strategic_decision_variant(self) -> None:
        frame = PlannerFrame.model_validate(
            {
                "intent_summary": "Resolve a priority conflict.",
                "planning_rationale": "Either task can move.",
                "constraints_considered": [],
                "limitations": [],
                "selected_capability_ids": ["scheduling"],
                "decision": "needs_strategic_decision",
                "task_directives": [],
                "strategic_decision": {
                    "question": "Which task should retain the requested time?",
                    "options": ["incoming", "existing"],
                },
            }
        )
        self.assertEqual(frame.decision, "needs_strategic_decision")


if __name__ == "__main__":
    unittest.main()
