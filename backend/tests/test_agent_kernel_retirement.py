from __future__ import annotations

from pathlib import Path
import unittest

from app.services.agent_kernel.retirement import (
    build_retirement_inventory,
    validate_retirement_guards,
)
from app.services.agent_kernel.rollout import OperationFamily, RolloutPolicy


ROOT = Path(__file__).resolve().parents[2]


class AgentKernelRetirementTests(unittest.TestCase):
    def test_present_historical_adapters_are_explicitly_non_routable(self) -> None:
        inventory = build_retirement_inventory(
            RolloutPolicy.from_environment({}),
            root=ROOT,
        )
        self.assertTrue(inventory)
        self.assertTrue(
            all(item["state"] == "retired_from_agent_authority" for item in inventory)
        )
        self.assertTrue(all(item["routable"] is False for item in inventory))
        self.assertTrue(
            all(
                item["gateReasonCode"] == "single_authority_cutover"
                for item in inventory
            )
        )

    def test_rollout_policy_cannot_reactivate_an_offline_adapter(self) -> None:
        disabled = build_retirement_inventory(
            RolloutPolicy.from_environment({}),
            root=ROOT,
        )
        enabled = build_retirement_inventory(
            RolloutPolicy.for_testing([OperationFamily.PREFERENCE]),
            root=ROOT,
        )

        self.assertEqual(disabled, enabled)

    def test_architecture_guards_remain_green(self) -> None:
        self.assertEqual(validate_retirement_guards(ROOT), [])


if __name__ == "__main__":
    unittest.main()
