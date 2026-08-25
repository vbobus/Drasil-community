from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.services.agent_kernel.provider_transport import get_provider_runtime_settings
from app.services.strategist_provider import (
    _parse_codex_model_catalog,
    save_provider_settings,
)


MODEL_CATALOG = [
    {
        "id": "gpt-5.6-terra",
        "displayName": "GPT-5.6-Terra",
        "description": "",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
    },
    {
        "id": "gpt-5.6-luna",
        "displayName": "GPT-5.6-Luna",
        "description": "",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": ["low", "medium", "high", "xhigh", "max"],
    },
]


class StrategistProviderSettingsTests(unittest.TestCase):
    def test_catalog_includes_visible_models_and_their_supported_efforts(self) -> None:
        catalog = _parse_codex_model_catalog(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "visibility": "list",
                        "priority": 1,
                        "default_reasoning_level": "low",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "ultra"},
                        ],
                    },
                    {
                        "slug": "codex-auto-review",
                        "display_name": "Auto review",
                        "visibility": "hide",
                        "priority": 0,
                        "supported_reasoning_levels": [{"effort": "medium"}],
                    },
                ],
            },
        )

        self.assertEqual([model["id"] for model in catalog], ["gpt-5.6-sol"])
        self.assertEqual(catalog[0]["defaultReasoningEffort"], "low")
        self.assertEqual(catalog[0]["supportedReasoningEfforts"], ["low", "ultra"])

    def test_save_persists_supported_model_and_thinking_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "provider.json"
            with patch(
                "app.services.strategist_provider._get_settings_path",
                return_value=settings_path,
            ), patch(
                "app.services.strategist_provider.get_codex_model_catalog",
                return_value=MODEL_CATALOG,
            ):
                result = save_provider_settings(
                    active_provider="codex-cli",
                    codex_model="gpt-5.6-terra",
                    codex_reasoning_effort="ultra",
                    openai_api_key=None,
                    openai_model=None,
                    clear_openai_api_key=False,
                )

            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["codexModel"], "gpt-5.6-terra")
            self.assertEqual(saved["codexReasoningEffort"], "ultra")
            self.assertEqual(result["codexReasoningEffort"], "ultra")

    def test_save_rejects_thinking_level_the_model_does_not_support(self) -> None:
        with patch(
            "app.services.strategist_provider.get_codex_model_catalog",
            return_value=MODEL_CATALOG,
        ):
            with self.assertRaises(HTTPException) as raised:
                save_provider_settings(
                    active_provider="codex-cli",
                    codex_model="gpt-5.6-luna",
                    codex_reasoning_effort="ultra",
                    openai_api_key=None,
                    openai_model=None,
                    clear_openai_api_key=False,
                )

        self.assertEqual(raised.exception.status_code, 400)

    def test_runtime_exposes_saved_codex_thinking_level(self) -> None:
        with patch(
            "app.services.agent_kernel.provider_transport.get_effective_provider_settings",
            return_value={
                "activeProvider": "codex-cli",
                "codexModel": "gpt-5.6-sol",
                "codexReasoningEffort": "max",
            },
        ), patch(
            "app.services.agent_kernel.provider_transport.get_codex_model_catalog",
            return_value=[
                {
                    "id": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "description": "",
                    "defaultReasoningEffort": "low",
                    "supportedReasoningEfforts": [
                        "low", "medium", "high", "xhigh", "max", "ultra"
                    ],
                }
            ],
        ):
            settings = get_provider_runtime_settings()

        self.assertEqual(settings["model_name"], "gpt-5.6-sol")
        self.assertEqual(settings["reasoning_effort"], "max")
        self.assertTrue(settings["matrix_supported"])


if __name__ == "__main__":
    unittest.main()
