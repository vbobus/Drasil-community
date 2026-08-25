from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path
import unittest

from fastapi import HTTPException

from app.api.ai import (
    StrategistRequest,
    StrategistUiEvent,
    execute_strategist_run,
    run_strategist,
)
from app.api.planner import PlannerProposeRequest, propose_schedule_patch
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.codex_app_server import (
    DIRECT_DYNAMIC_TOOL_NAMES,
    TERMINAL_SEMANTIC_PLAN_TOOL_NAMES,
)
from app.services.agent_kernel.controller import NativeAuthorityController
from app.services.agent_kernel.mcp_policy import allowed_mcp_tool_names
from app.services.agent_kernel.retirement import validate_retirement_guards
from app.services.agent_kernel.review_actions import ReviewActionCoordinator
from app.services.agent_kernel.runtime import (
    _proposals_from_typed_tool_trace,
    run_agent_kernel_turn,
)
from app.services.agent_kernel.tool_registry import execute_kernel_tool
from app.services.strategist_graph import run_strategist_graph_turn
from app.services.strategist_proposal_normalizer import normalize_branch_proposal


REPO_ROOT = Path(__file__).resolve().parents[2]


class AgentSingleAuthorityCutoverTests(unittest.TestCase):
    def test_strategist_mcp_has_semantic_plan_tools_and_no_retired_authoring(self) -> None:
        allowed = allowed_mcp_tool_names("strategist")

        self.assertTrue(
            {
                "submit_task_metadata_plan",
                "submit_task_tree_plan",
                "submit_preference_plan",
                "submit_preference_management_plan",
                "submit_preference_cleanup_plan",
            }
            <= allowed
        )
        self.assertTrue(
            {
                "propose_metadata_update",
                "propose_tree_operation",
                "propose_preference_memory",
                "draft_change_set",
                "simulate_change_set",
                "update_goal_state",
            }.isdisjoint(allowed)
        )

    def test_review_selection_has_no_legacy_or_shadow_input(self) -> None:
        parameters = inspect.signature(NativeAuthorityController.select).parameters
        source = inspect.getsource(NativeAuthorityController)

        self.assertNotIn("legacy_proposals", parameters)
        self.assertNotIn("legacy_timezone", parameters)
        self.assertNotIn("shadow", source.lower())
        self.assertNotIn("RolloutPolicy", source)

    def test_live_runtime_has_no_shadow_or_compatibility_authority_branch(self) -> None:
        source = inspect.getsource(run_agent_kernel_turn)

        for forbidden in (
            "legacy_proposals",
            "native_shadow_sampling_enabled()",
            "build_change_set_shadow_observation",
            'selected_authority == "legacy"',
        ):
            self.assertNotIn(forbidden, source)

    def test_live_approval_paths_do_not_dispatch_compatibility_apply(self) -> None:
        strategist_source = inspect.getsource(run_strategist_graph_turn)
        worker_source = inspect.getsource(execute_strategist_run)
        review_source = inspect.getsource(ReviewActionCoordinator)

        retirement_guard = strategist_source.index(
            "This older proposal card has been retired"
        )
        self.assertGreaterEqual(retirement_guard, 0)
        self.assertNotIn("handle_proposal_action(", strategist_source)
        self.assertNotIn("sync_response_proposals(", strategist_source)
        self.assertNotIn("handle_schedule_proposal_action(", worker_source)
        self.assertNotIn("RolloutPolicy", review_source)
        self.assertNotIn("rollout_policy", review_source)

    def test_retirement_guards_report_no_routable_competing_authority(self) -> None:
        self.assertEqual(validate_retirement_guards(REPO_ROOT), [])

    def test_codex_transport_uses_semantic_plan_tools_only(self) -> None:
        semantic_tools = {
            "submit_task_metadata_plan",
            "submit_task_tree_plan",
            "submit_preference_plan",
            "submit_preference_management_plan",
            "submit_preference_cleanup_plan",
        }
        old_tools = {
            "propose_metadata_update",
            "propose_tree_operation",
            "propose_preference_memory",
        }
        self.assertTrue(semantic_tools <= DIRECT_DYNAMIC_TOOL_NAMES)
        self.assertEqual(TERMINAL_SEMANTIC_PLAN_TOOL_NAMES, frozenset())
        self.assertFalse(old_tools & DIRECT_DYNAMIC_TOOL_NAMES)

    def test_retired_live_routes_fail_closed_before_authoring(self) -> None:
        with self.assertRaises(HTTPException) as strategist_error:
            run_strategist(
                StrategistRequest(
                    ui_event=StrategistUiEvent(
                        kind="proposal_action",
                        proposal_id="old-proposal",
                        action="approve",
                    )
                ),
                session=object(),  # type: ignore[arg-type]
            )
        self.assertEqual(strategist_error.exception.status_code, 410)

        now = datetime.now()
        with self.assertRaises(HTTPException) as planner_error:
            propose_schedule_patch(
                PlannerProposeRequest(
                    prompt="Schedule it",
                    working_set_task_ids=[1],
                    window_start=now,
                    window_end=now + timedelta(hours=1),
                ),
                session=object(),  # type: ignore[arg-type]
            )
        self.assertEqual(planner_error.exception.status_code, 410)

    def test_semantic_preference_submission_returns_meaning_not_approval(self) -> None:
        result = execute_kernel_tool(
            "submit_preference_plan",
            {
                "preference": {
                    "description": "Keep a 15 minute gap between long tasks",
                    "value": {
                        "preferenceText": "Keep a 15 minute gap between long tasks",
                        "interTaskGapMinutes": 15,
                    },
                    "strength": "hard",
                    "scopeType": "global",
                    "domain": "scheduling",
                },
                "constraints": [],
                "source_text": "Remember a 15 minute gap between long tasks",
            },
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            original_user_request="Remember a 15 minute gap between long tasks",
        )
        self.assertTrue(result["compiled"])
        self.assertEqual(
            result["semantic_plan_input"]["plan_kind"],
            "preference",
        )
        self.assertIsInstance(
            result["semantic_plan_input"]["payload"],
            dict,
        )
        self.assertNotIn("preference_memory_proposal", result)

    def test_semantic_tree_submission_preserves_independent_create_intent(self) -> None:
        work = Category(id=1, name="Work")
        result = execute_kernel_tool(
            "submit_task_tree_plan",
            {
                "proposal_kind": "create",
                "target_category_id": 1,
                "target_category_name": "Work",
                "structural_operations": [
                    {
                        "kind": "create_node",
                        "temp_id": "docker",
                        "title": "Learn Docker",
                    }
                ],
            },
            categories=[work],
            nodes=[],
            focus_node=None,
            active_category=work,
            staged_node_ids=[],
            original_user_request="Create a top-level task to learn Docker",
        )
        self.assertTrue(result["compiled"])
        semantic_input = result["semantic_plan_input"]
        self.assertEqual(semantic_input["plan_kind"], "branch")
        operations = semantic_input["payload"]["structural_operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["title"], "Learn Docker")
        self.assertIsNone(operations[0].get("parent_id"))

    def test_explicit_workspace_create_does_not_inherit_stale_focus(self) -> None:
        work = Category(id=1, name="Work")
        finance = Category(id=2, name="Finance")
        stale_focus = StrategyNode(
            id=41,
            category_id=work.id,
            title="Workout",
        )

        proposal = normalize_branch_proposal(
            {
                "proposal_kind": "create",
                "target_node_id": None,
                "target_node_title": finance.name,
                "target_category_id": finance.id,
                "target_category_name": finance.name,
                "structural_operations": [
                    {
                        "kind": "create_node",
                        "temp_id": "file_taxes",
                        "title": "File taxes",
                        "category_id": finance.id,
                        "parent_id": None,
                    }
                ],
            },
            nodes=[stale_focus],
            focus_node=stale_focus,
            evidence_bundle={
                "active_category": {
                    "category_id": work.id,
                    "name": work.name,
                },
                "available_categories": [
                    {"category_id": work.id, "name": work.name},
                    {"category_id": finance.id, "name": finance.name},
                ],
            },
        )

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal["target_category_id"], finance.id)
        self.assertEqual(proposal["target_node_ids"], [])
        self.assertIsNone(proposal["structural_operations"][0]["parent_id"])

    def test_semantic_tree_submission_preserves_validation_feedback(self) -> None:
        work = Category(id=1, name="Work")
        result = execute_kernel_tool(
            "submit_task_tree_plan",
            {
                "proposal_kind": "create",
                "target_category_id": 1,
                "structural_operations": [
                    {
                        "kind": "unsupported_operation",
                        "title": "Learn Docker",
                    }
                ],
            },
            categories=[work],
            nodes=[],
            focus_node=None,
            active_category=work,
            staged_node_ids=[],
            original_user_request="Create a top-level task to learn Docker",
        )

        self.assertFalse(result["compiled"])
        self.assertIsInstance(result.get("error"), str)
        self.assertTrue(result["error"])
        self.assertIsNone(result["semantic_plan_input"]["payload"])

    def test_compound_trace_preserves_task_and_preference_families(self) -> None:
        work = Category(id=1, name="Work")
        submissions = _proposals_from_typed_tool_trace(
            tool_trace=[
                {
                    "name": "submit_task_tree_plan",
                    "arguments": {},
                    "result": {
                        "compiled": True,
                        "semantic_plan_input": {
                            "plan_kind": "branch",
                            "payload": {
                                "proposal_kind": "create",
                                "target_category_id": 1,
                                "structural_operations": [
                                    {
                                        "kind": "create_node",
                                        "temp_id": "focus-task",
                                        "title": "Focus task",
                                    }
                                ],
                            },
                        },
                    },
                },
                {
                    "name": "submit_preference_plan",
                    "arguments": {},
                    "result": {
                        "compiled": True,
                        "semantic_plan_input": {
                            "plan_kind": "preference",
                            "payload": {
                                "description": "Keep mornings free",
                                "value": {"preferenceText": "Keep mornings free"},
                                "strength": "soft",
                                "scope_type": "global",
                                "domain": "scheduling",
                            },
                        },
                    },
                },
            ],
            nodes=[],
            focus_node=None,
            active_category=work,
            categories=[work],
        )

        self.assertEqual(set(submissions), {"branch", "preference_memory"})

    def test_latest_preference_management_repair_supersedes_earlier_invalid_call(
        self,
    ) -> None:
        valid_payload = {
            "proposal_id": "preference-proposal:fixed",
            "proposal_kind": "preference_memory",
            "preference_patch": {
                "version": 1,
                "operations": [
                    {
                        "op": "retire_preference",
                        "preference": {
                            "id": "pref-old",
                            "revision": 2,
                            "description": "Old broad gap rule",
                        },
                    }
                ],
            },
            "preference_patch_validation": {
                "status": "valid",
                "errors": [],
                "warnings": [],
            },
        }
        invalid_call = {
            "name": "submit_preference_management_plan",
            "arguments": {
                "actions": [
                    {
                        "action": "disable",
                        "id": "pref-old",
                        "revision": 2,
                        "description": "Old broad gap rule",
                    }
                ]
            },
            "result": {
                "error": "actions.0.preference_id: Field required",
                "result_kind": "error",
            },
        }
        valid_call = {
            "name": "submit_preference_management_plan",
            "arguments": {
                "actions": [
                    {
                        "action": "disable",
                        "preference_id": "pref-old",
                        "revision": 2,
                        "description": "Old broad gap rule",
                    }
                ]
            },
            "result": {
                "compiled": True,
                "semantic_plan_input": {
                    "plan_kind": "preference",
                    "payload": valid_payload,
                },
            },
        }

        repaired = _proposals_from_typed_tool_trace(
            tool_trace=[invalid_call, valid_call],
            nodes=[],
            focus_node=None,
            categories=[],
        )
        self.assertEqual(
            repaired["preference_memory"][0]["proposal_id"],
            "preference-proposal:fixed",
        )

        failed_correction = _proposals_from_typed_tool_trace(
            tool_trace=[valid_call, invalid_call],
            nodes=[],
            focus_node=None,
            categories=[],
        )
        self.assertNotIn("preference_memory", failed_correction)


if __name__ == "__main__":
    unittest.main()
