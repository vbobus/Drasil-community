from __future__ import annotations

import unittest

from app.services.agent_kernel.authority_inventory import (
    build_authority_inventory,
)
from app.services.agent_kernel.rollout import OperationFamily, RolloutPolicy


class AgentKernelAuthorityInventoryTests(unittest.TestCase):
    def test_inventory_reports_effective_authority_per_capability(self) -> None:
        inventory = build_authority_inventory(
            RolloutPolicy.for_testing([OperationFamily.TASK])
        )
        capabilities = {
            item["capability"]: item for item in inventory["capabilities"]
        }

        self.assertEqual(inventory["nativeAcceptedFallbackPolicy"], "prohibited")
        self.assertEqual(inventory["mcpProductionAuthority"], "authoritative")
        self.assertEqual(inventory["agentFacingAuthority"], "drasil_core_mcp")
        self.assertEqual(
            capabilities["task_reads"]["primaryPath"],
            "drasil_core_mcp",
        )
        self.assertEqual(
            capabilities["task_writes"]["primaryPath"],
            "server_compiled_changeset",
        )
        self.assertTrue(capabilities["task_writes"]["promotionEligible"])
        self.assertEqual(
            capabilities["calendar_writes"]["primaryPath"],
            "server_compiled_changeset",
        )
        self.assertTrue(
            capabilities["calendar_writes"]["promotionEligible"]
        )
        self.assertIsNone(capabilities["calendar_writes"]["compatibilityPath"])
        self.assertEqual(
            capabilities["preference_reads"]["applicationService"],
            "PreferenceApplicationService",
        )
        self.assertEqual(
            capabilities["scheduling"]["applicationService"],
            "PlannerSubagentCoordinator + PlannerSchedulingApplicationService",
        )
        self.assertEqual(
            capabilities["planner_delegation"]["primaryPath"],
            "strategist_owned_planner_subagent",
        )
        self.assertEqual(
            capabilities["change_compilation"]["primaryPath"],
            "server_change_compilation_coordinator",
        )
        self.assertIn(
            "ServerChangeCompilationCoordinator",
            capabilities["change_compilation"]["applicationService"],
        )
        self.assertTrue(
            capabilities["change_compilation"]["promotionEligible"]
        )
        self.assertTrue(capabilities["mcp_commands"]["promotionEligible"])

    def test_write_inventory_records_build_rollback_instead_of_dual_routing(self) -> None:
        inventory = build_authority_inventory(
            RolloutPolicy.for_testing([OperationFamily.RECURRENCE])
        )
        recurrence = next(
            item
            for item in inventory["capabilities"]
            if item["capability"] == "recurrence_writes"
        )

        self.assertIsNone(recurrence["requiredBuildId"])
        self.assertEqual(recurrence["evidenceBuilds"], [])
        self.assertIsNone(recurrence["rolloutFlag"])
        self.assertIn("previous known-good build", recurrence["retirementGate"])


if __name__ == "__main__":
    unittest.main()
