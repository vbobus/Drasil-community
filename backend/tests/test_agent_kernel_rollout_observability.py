from __future__ import annotations

import unittest

from app.services.agent_kernel.capability_observability import (
    RolloutObservation,
    clarification_was_unnecessary,
    emit_rollout_observation,
)


class RolloutObservabilityTests(unittest.TestCase):
    def test_closed_rollout_event_preserves_aggregate_facts(self) -> None:
        payload = emit_rollout_observation(RolloutObservation(
            event="shadow_comparison",
            outcome="divergent",
            family="task",
            provider="openai",
            model="configured-model",
            divergence_count=2,
            latency_ms=125,
            input_tokens=40,
            output_tokens=20,
            cost_microunits=None,
            reason_code="semantic_value_mismatch",
            request_id_hash="a" * 64,
        ))
        self.assertEqual(payload["divergence_count"], 2)
        self.assertIsNone(payload["cost_microunits"])
        self.assertNotIn("prompt", payload)
        self.assertNotIn("task_title", payload)
        self.assertNotIn("raw_payload", payload)

    def test_payload_like_or_unbounded_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RolloutObservation(
                event="provider_call",
                outcome="failed",
                reason_code='{"private":"payload"}',
            )
        with self.assertRaises(ValueError):
            RolloutObservation(
                event="transaction",
                outcome="success",
                actual_partial_commits=1,
            )

    def test_unnecessary_clarification_uses_typed_delta_counts(self) -> None:
        self.assertTrue(clarification_was_unnecessary(
            goal_delta_count=0,
            constraint_delta_count=0,
            decision_delta_count=0,
        ))
        self.assertFalse(clarification_was_unnecessary(
            goal_delta_count=1,
            constraint_delta_count=0,
            decision_delta_count=0,
        ))


if __name__ == "__main__":
    unittest.main()
