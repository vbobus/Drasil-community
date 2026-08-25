from __future__ import annotations

import json
import unittest

from app.services.agent_kernel.provider_matrix import (
    DEFAULT_SUPPORTED_MODEL_MATRIX,
    load_supported_model_matrix,
    resolve_configured_oauth_matrix_entry,
    validate_matrix_gate_results,
    validate_configured_model,
)


class AgentKernelProviderMatrixTests(unittest.TestCase):
    def test_checked_in_matrix_requires_oauth_transport_and_keeps_byok_optional(self) -> None:
        self.assertEqual(
            {item.provider for item in DEFAULT_SUPPORTED_MODEL_MATRIX if item.required},
            {"codex_app_server"},
        )
        self.assertEqual(
            {item.provider for item in DEFAULT_SUPPORTED_MODEL_MATRIX},
            {"openai_api", "codex_app_server"},
        )
        for entry in DEFAULT_SUPPORTED_MODEL_MATRIX:
            self.assertTrue(entry.strict_tools)
            self.assertTrue(entry.streaming)

    def test_closed_override_is_configuration_driven(self) -> None:
        payload = [
            {
                "provider": "openai_api",
                "model": "pinned-openai",
                "reasoningEfforts": [],
                "required": False,
            },
            {
                "provider": "codex_app_server",
                "model": "pinned-codex",
                "reasoningEfforts": ["high"],
            },
        ]
        matrix = load_supported_model_matrix({
            "AGENT_KERNEL_SUPPORTED_MODEL_MATRIX_JSON": json.dumps(payload),
        })
        self.assertEqual(matrix[1].model, "pinned-codex")
        self.assertEqual(
            validate_configured_model(
                provider="codex_app_server",
                model="pinned-codex",
                reasoning_effort="high",
                matrix=matrix,
            ),
            matrix[1],
        )

    def test_invalid_or_incomplete_matrix_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            load_supported_model_matrix({
                "AGENT_KERNEL_SUPPORTED_MODEL_MATRIX_JSON": json.dumps([
                    {"provider": "openai_api", "model": "only-one", "required": False},
                ]),
            })
        with self.assertRaises(ValueError):
            load_supported_model_matrix({
                "AGENT_KERNEL_SUPPORTED_MODEL_MATRIX_JSON": "not-json",
            })

    def test_unsupported_model_or_reasoning_level_is_typed_failure(self) -> None:
        entry = next(
            item for item in DEFAULT_SUPPORTED_MODEL_MATRIX
            if item.provider == "codex_app_server"
        )
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            validate_configured_model(
                provider=entry.provider,
                model=entry.model,
                reasoning_effort="unsupported",
                matrix=DEFAULT_SUPPORTED_MODEL_MATRIX,
            )

    def test_default_oauth_gate_resolves_the_active_visible_codex_model(self) -> None:
        entry = resolve_configured_oauth_matrix_entry(
            model="new-visible-codex-model",
            reasoning_effort="medium",
            catalog=[
                {
                    "id": "new-visible-codex-model",
                    "supportedReasoningEfforts": ["low", "medium", "high"],
                },
            ],
            environ={},
        )

        self.assertEqual(entry.provider, "codex_app_server")
        self.assertEqual(entry.model, "new-visible-codex-model")
        self.assertTrue(entry.required)

    def test_explicit_provider_matrix_remains_a_closed_allowlist(self) -> None:
        override = {
            "AGENT_KERNEL_SUPPORTED_MODEL_MATRIX_JSON": json.dumps([
                {
                    "provider": "codex_app_server",
                    "model": "pinned-codex",
                    "reasoningEfforts": ["high"],
                },
            ]),
        }
        with self.assertRaisesRegex(ValueError, "provider/model"):
            resolve_configured_oauth_matrix_entry(
                model="catalog-only-model",
                reasoning_effort="high",
                catalog=[
                    {
                        "id": "catalog-only-model",
                        "supportedReasoningEfforts": ["high"],
                    },
                ],
                environ=override,
            )

    def test_required_matrix_entries_cannot_be_skipped_or_grandfathered(self) -> None:
        results = [
            {
                "provider": entry.provider,
                "model": entry.model,
                "passed": True,
                "unsafeDecisions": 0,
                "partialCommits": 0,
            }
            for entry in DEFAULT_SUPPORTED_MODEL_MATRIX
        ]
        self.assertEqual(validate_matrix_gate_results(results), [])
        self.assertTrue(validate_matrix_gate_results(results[:-1]))
        required_result = next(
            result for result in results if result["provider"] == "codex_app_server"
        )
        required_result["partialCommits"] = 1
        self.assertIn(
            f"failed:{required_result['provider']}:{required_result['model']}",
            validate_matrix_gate_results(results),
        )
        with self.assertRaisesRegex(ValueError, "provider/model"):
            validate_configured_model(
                provider="openai_api",
                model="unknown-model",
                reasoning_effort=None,
                matrix=DEFAULT_SUPPORTED_MODEL_MATRIX,
            )


if __name__ == "__main__":
    unittest.main()
