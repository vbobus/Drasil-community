from __future__ import annotations

import unittest

from app.services.agent_kernel.contracts import TaskUpdatePatch

from app.services.agent_kernel.tool_registry import execute_kernel_tool
from app.services.agent_kernel.provider_transport import _terminal_proposal_payload_from_tool_result


class AgentChangeSetToolTests(unittest.TestCase):
    @staticmethod
    def execute(arguments: dict, **kwargs):
        return execute_kernel_tool(
            "draft_change_set",
            arguments,
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            **kwargs,
        )

    @staticmethod
    def arguments() -> dict:
        return {
            "disposition": "create",
            "expectedGoalRevision": 1,
            "operations": [
                {
                    "kind": "task.create",
                    "operationId": "op:create",
                    "title": "Launch",
                    "workspaceRef": {"kind": "workspace", "entityId": "workspace:1"},
                    "tempRef": {
                        "kind": "temporary",
                        "entityId": "temp:task",
                        "targetKind": "task",
                    },
                    "fulfillsClauseIds": ["clause:create"],
                }
            ],
        }

    def test_tool_requires_runtime_authorer_and_rejects_server_identity(self) -> None:
        unavailable = self.execute(self.arguments())
        self.assertEqual(unavailable["error"], "native_changeset_unavailable")
        self.assertFalse(unavailable["read_only"])
        self.assertEqual(unavailable["tool_error"]["code"], "TOOL_CONTEXT_UNAVAILABLE")

        injected = self.execute(
            {**self.arguments(), "changeSetId": "changeset:model"},
            change_set_authorer=lambda _arguments: {},
        )
        self.assertIn("Extra inputs are not permitted", injected["error"])
        self.assertEqual(injected["tool_error"]["code"], "TOOL_ARGUMENTS_INVALID")
        self.assertEqual(injected["tool_error"]["affectedPaths"], ["changeSetId"])

    def test_tool_validates_authorer_output(self) -> None:
        result = self.execute(
            self.arguments(),
            change_set_authorer=lambda _arguments: {"persisted": True},
        )
        self.assertEqual(result["error"], "native_changeset_output_invalid")
        self.assertEqual(result["tool_error"]["code"], "TOOL_OUTPUT_INVALID")

    def test_model_authored_contract_failure_is_repairable(self) -> None:
        def invalid_authorer(_arguments):
            TaskUpdatePatch(title=None)

        result = self.execute(self.arguments(), change_set_authorer=invalid_authorer)
        self.assertEqual(result["error"], "native_changeset_validation_failed")
        self.assertEqual(result["tool_error"]["code"], "TOOL_VALIDATION_FAILED")
        self.assertEqual(result["tool_error"]["category"], "model_repairable")

    def test_native_result_is_shadow_observation_not_legacy_terminal_proposal(self) -> None:
        self.assertIsNone(
            _terminal_proposal_payload_from_tool_result(
                tool_name="draft_change_set",
                result={"persisted": True, "changeSet": {"changeSetId": "changeset:1"}},
            )
        )


if __name__ == "__main__":
    unittest.main()
