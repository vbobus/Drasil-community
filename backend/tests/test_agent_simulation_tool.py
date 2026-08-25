from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app.services.agent_kernel.contracts import (
    ChangeSetSimulationResult,
    ConflictGraph,
    EntityRef,
    OperationConservationReport,
    ProjectedEffect,
    SimulationSnapshot,
)
from app.services.agent_kernel.tool_registry import (
    compact_simulation_result_for_model,
    execute_kernel_tool,
)


class AgentSimulationToolTests(unittest.TestCase):
    @staticmethod
    def valid_result() -> ChangeSetSimulationResult:
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        snapshot = SimulationSnapshot(
            snapshot_id="snapshot:tool",
            fingerprint="fingerprint:tool",
            captured_at=now,
            freshness_boundary=now,
        )
        graph = ConflictGraph(
            graph_id="graph:tool",
            goal_id="goal:tool",
            goal_revision=2,
            change_set_id="changeset:tool",
            change_set_revision=1,
            change_set_operation_ids=["op:tool"],
            snapshot_fingerprint=snapshot.fingerprint,
            status="clear",
        )
        return ChangeSetSimulationResult(
            simulation_id="simulation:tool",
            command_id="command:tool",
            goal_id="goal:tool",
            goal_revision=2,
            resulting_goal_revision=3,
            change_set_id="changeset:tool",
            change_set_revision=1,
            input_snapshot=snapshot,
            output_fingerprint="fingerprint:output",
            status="ready_for_review",
            conflict_graph=graph,
            projected_effects=[
                ProjectedEffect(
                    operation_id="op:tool",
                    kind="calendar.created",
                    entity_refs=[
                        EntityRef(kind="calendar_block", entity_id="block:tool")
                    ],
                )
            ],
            conservation=OperationConservationReport(complete=True),
            correlation_id="corr:tool",
            causation_id="command:tool",
        )

    @staticmethod
    def execute(arguments: dict, **kwargs):
        return execute_kernel_tool(
            "simulate_change_set",
            arguments,
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            **kwargs,
        )

    def test_tool_accepts_only_expected_revisions_and_requires_runtime(self) -> None:
        arguments = {"expectedGoalRevision": 2, "expectedChangeSetRevision": 1}
        unavailable = self.execute(arguments)
        self.assertEqual(unavailable["error"], "native_changeset_simulation_unavailable")
        self.assertEqual(unavailable["tool_error"]["code"], "TOOL_CONTEXT_UNAVAILABLE")
        injected = self.execute(
            {**arguments, "changeSetId": "changeset:model"},
            change_set_simulator=lambda _arguments: {},
        )
        self.assertIn("Extra inputs are not permitted", injected["error"])
        self.assertEqual(injected["tool_error"]["code"], "TOOL_ARGUMENTS_INVALID")

    def test_invalid_runtime_output_fails_closed(self) -> None:
        result = self.execute(
            {"expectedGoalRevision": 2, "expectedChangeSetRevision": 1},
            change_set_simulator=lambda _arguments: {"status": "clear"},
        )
        self.assertEqual(result["error"], "native_changeset_simulation_output_invalid")
        self.assertEqual(result["tool_error"]["code"], "TOOL_OUTPUT_INVALID")

    def test_successful_runtime_output_remains_the_exact_strict_contract(self) -> None:
        canonical = self.valid_result().model_dump(mode="json", by_alias=True)
        result = self.execute(
            {"expectedGoalRevision": 2, "expectedChangeSetRevision": 1},
            change_set_simulator=lambda _arguments: canonical,
        )
        self.assertEqual(
            ChangeSetSimulationResult.model_validate(result), self.valid_result()
        )
        self.assertNotIn("result_kind", result)
        self.assertNotIn("observation_summary", result)

    def test_unexpected_runtime_failure_is_one_typed_retryable_error(self) -> None:
        def fail(_arguments):
            raise OSError("private database detail")

        result = self.execute(
            {"expectedGoalRevision": 2, "expectedChangeSetRevision": 1},
            change_set_simulator=fail,
        )
        self.assertEqual(result["error"], "native_changeset_simulation_failed")
        self.assertEqual(
            result["tool_error"]["code"], "TOOL_RETRYABLE_SYSTEM_FAILURE"
        )
        self.assertTrue(result["tool_error"]["retryable"])
        self.assertNotIn("private database detail", result["tool_error"]["messageForModel"])

    def test_simulation_handle_returns_bounded_typed_pages(self) -> None:
        stored = self.valid_result().model_dump(mode="json", by_alias=True)
        stored["result_kind"] = "native_change_set_simulation"
        result = execute_kernel_tool(
            "get_tool_result_by_handle",
            {
                "tool_result_handle": "handle:1",
                "section": "final_facts",
                "offset": 0,
                "limit": 5,
            },
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            tool_result_store={"handle:1": stored},
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["simulation_page"]["section"], "final_facts")
        self.assertLessEqual(len(result["simulation_page"]["items"]), 5)
        self.assertIsNone(result["tool_result"])

    def test_model_observation_is_compact_and_omits_full_simulation_sections(self) -> None:
        canonical = self.valid_result().model_dump(mode="json", by_alias=True)
        canonical["projectedFinalFacts"] = [
            {"factId": f"fact:{index}", "payload": "x" * 2_000}
            for index in range(100)
        ]
        compact = compact_simulation_result_for_model(
            canonical, tool_result_handle="handle:compact"
        )
        serialized = json.dumps(compact)
        self.assertLess(len(serialized.encode("utf-8")), 32 * 1024)
        self.assertNotIn("inputSnapshot", compact)
        self.assertNotIn("conflictGraph", compact)
        self.assertNotIn("projectedFinalFacts", compact)
        self.assertEqual(compact["tool_result_handle"], "handle:compact")


if __name__ == "__main__":
    unittest.main()
