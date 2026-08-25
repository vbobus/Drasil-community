from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.agent_kernel.planner_tool_contracts import (
    PLANNER_MODEL_TOOL_CONTRACTS,
    PlannerFrame,
    planner_model_tool_definitions,
)
from app.services.agent_kernel.tool_registry import registered_kernel_tool_specs


EXPECTED_ERROR_CODES = {
    "TOOL_NOT_FOUND",
    "TOOL_ARGUMENTS_INVALID",
    "TOOL_OUTPUT_INVALID",
    "TOOL_CONTEXT_UNAVAILABLE",
    "TOOL_AUTHORIZATION_DENIED",
    "TOOL_RETRYABLE_SYSTEM_FAILURE",
    "TOOL_VALIDATION_FAILED",
    "TOOL_INVARIANT_FAILED",
}


class AgentKernelV2ToolContractTests(unittest.TestCase):
    def test_every_strategist_tool_exposes_v2_contract_metadata(self) -> None:
        specs = registered_kernel_tool_specs()
        self.assertEqual(len(specs), 45)
        for spec in specs:
            self.assertEqual(spec.contract_version, "2.0")
            self.assertIsNotNone(spec.input_model)
            self.assertIsNotNone(spec.output_model)
            self.assertTrue(spec.side_effect)
            self.assertTrue(spec.approval_requirement)
            self.assertTrue(spec.idempotency_behavior)
            self.assertTrue(EXPECTED_ERROR_CODES <= set(spec.error_codes))
            v2_schema = spec.v2_tool_schema()
            self.assertIs(v2_schema["strict"], True)
            self.assertIs(v2_schema["parameters"]["additionalProperties"], False)
            self.assertEqual(
                set(v2_schema["parameters"].get("required", [])),
                set(v2_schema["parameters"].get("properties", {})),
            )

    def test_production_registration_uses_the_strict_v2_projection(self) -> None:
        from app.services.agent_kernel.tool_registry import registered_kernel_tool_schemas

        schemas = registered_kernel_tool_schemas()
        self.assertEqual(len(schemas), len(registered_kernel_tool_specs()))
        self.assertTrue(all(item.get("strict") is True for item in schemas))
        simulation = next(item for item in schemas if item["name"] == "simulate_change_set")
        self.assertIs(simulation["parameters"]["additionalProperties"], False)

    def test_v2_input_and_output_validation_are_runtime_methods(self) -> None:
        spec = next(item for item in registered_kernel_tool_specs() if item.name == "get_task_snapshot")
        validated = spec.validate_v2_input({"task_id": 1})
        self.assertEqual(validated["task_id"], 1)
        with self.assertRaises(ValidationError):
            spec.validate_v2_input({"task_id": 1, "unknown": True})
        with self.assertRaises(ValidationError):
            spec.validate_v2_output({"status": "ok", "correlation_id": "corr:1"})

        draft = next(item for item in registered_kernel_tool_specs() if item.name == "draft_change_set")
        self.assertFalse(draft.read_only)
        self.assertEqual(draft.side_effect, "draft_state_write")
        self.assertEqual(draft.approval_requirement, "required_before_commit")
        self.assertEqual(draft.idempotency_behavior, "server_tool_call_replay_safe")

    def test_planner_tools_share_the_canonical_contract_source(self) -> None:
        self.assertEqual(
            set(PLANNER_MODEL_TOOL_CONTRACTS),
            {"read_planner_context", "search_capability_catalog", "evaluate_schedule_plan", "load_skill"},
        )
        definitions = planner_model_tool_definitions()
        self.assertEqual({item["name"] for item in definitions}, set(PLANNER_MODEL_TOOL_CONTRACTS))
        self.assertTrue(all(item["strict"] is True for item in definitions))

    def test_planner_frame_is_canonical_and_closed(self) -> None:
        frame = PlannerFrame(
            intent_summary="Schedule both tasks.",
            planning_rationale="Earlier deadlines first.",
            constraints_considered=[],
            limitations=[],
            selected_capability_ids=["scheduling"],
            decision="plan",
            task_directives=[
                {
                    "task_id": 1,
                    "action": "schedule",
                    "allocated_minutes": 30,
                    "gap_after_minutes": None,
                    "rationale": "Due first.",
                }
            ],
        )
        self.assertEqual(frame.task_directives[0].task_id, 1)
        with self.assertRaises(ValidationError):
            PlannerFrame.model_validate({**frame.model_dump(), "unexpected": True})


if __name__ == "__main__":
    unittest.main()
