from __future__ import annotations

import unittest

from app.services.agent_kernel.agent_profiles import clear_agent_profile_cache, get_agent_profile


REQUIRED_SECTIONS = (
    "## Mission",
    "## Ownership",
    "## Operating Loop",
    "## Evidence",
    "## Continuity",
    "## Decision Policy",
    "## Approval",
    "## Communication",
    "## Capabilities",
    "## Non-Negotiables",
)


class AgentProfileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_agent_profile_cache()

    def test_core_agent_documents_are_concise_sectioned_contracts(self) -> None:
        for agent_id in ("strategist", "planner"):
            text = str(get_agent_profile(agent_id, include_instructions=True)["operating_instructions"])
            lines = text.splitlines()
            self.assertGreaterEqual(len(lines), 80, agent_id)
            self.assertLessEqual(len(lines), 200, agent_id)
            self.assertLessEqual(max(map(len, lines)), 180, agent_id)
            self.assertFalse(any(marker in text for marker in ("ï¿½", "â€", "Â", "Ã")), agent_id)
            for section in REQUIRED_SECTIONS:
                self.assertEqual(text.count(section), 1, f"{agent_id}: {section}")

    def test_strategist_preserves_general_behavioral_contracts(self) -> None:
        text = str(get_agent_profile("strategist", include_instructions=True)["operating_instructions"])
        for phrase in (
            "exact full-title",
            "plural scope",
            "active GoalState",
            "one approval boundary",
            "affected occurrence",
            "current evidence",
        ):
            self.assertIn(phrase, text)

    def test_planner_preserves_conflict_and_solver_boundaries(self) -> None:
        text = str(get_agent_profile("planner", include_instructions=True)["operating_instructions"])
        for phrase in (
            "symmetrically",
            "protected and pinned",
            "affected occurrence",
            "deterministic solver",
            "one approval boundary",
            "current evidence",
        ):
            self.assertIn(phrase, text)

    def test_tool_field_details_are_not_duplicated_in_core_documents(self) -> None:
        for agent_id in ("strategist", "planner"):
            text = str(get_agent_profile(agent_id, include_instructions=True)["operating_instructions"])
            self.assertNotIn("request_constraints", text)
            self.assertNotIn("target_temp_id", text)
            self.assertNotIn("structural_operations", text)


if __name__ == "__main__":
    unittest.main()
