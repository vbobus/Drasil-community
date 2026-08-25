import unittest


class LegacyStrategistGraphClarificationFlowRetiredTests(unittest.TestCase):
    @unittest.skip(
        "Legacy Strategist graph rollback is retired; "
        "production coverage lives in test_strategist_agent_kernel."
    )
    def test_legacy_graph_flow_retired(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
