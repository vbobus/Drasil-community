import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.services.codex_login_state import (
    has_usable_codex_auth_file,
    read_codex_auth_file_snapshot,
)
from app.services.agent_kernel.provider_transport import ensure_codex_login


class CodexLoginStateTests(unittest.TestCase):
    def test_chatgpt_auth_file_is_usable_without_exposing_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            auth_path = Path(temp_dir) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": "secret-access-token",
                            "refresh_token": "secret-refresh-token",
                            "id_token": "secret-id-token",
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = read_codex_auth_file_snapshot(auth_path)

        serialized_snapshot = json.dumps(snapshot)
        self.assertTrue(snapshot["isUsable"])
        self.assertTrue(snapshot["hasChatGptTokens"])
        self.assertNotIn("secret-access-token", serialized_snapshot)
        self.assertNotIn("secret-refresh-token", serialized_snapshot)
        self.assertNotIn("secret-id-token", serialized_snapshot)

    def test_empty_or_missing_auth_file_is_not_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing-auth.json"
            empty_path = Path(temp_dir) / "auth.json"
            empty_path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}), encoding="utf-8")

            self.assertFalse(has_usable_codex_auth_file(missing_path))
            self.assertFalse(has_usable_codex_auth_file(empty_path))

    def test_ensure_codex_login_accepts_auth_file_when_status_command_is_wrong(self) -> None:
        with (
            patch(
                "app.services.agent_kernel.provider_transport.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="Not logged in"),
            ),
            patch("app.services.agent_kernel.provider_transport.has_usable_codex_auth_file", return_value=True),
        ):
            ensure_codex_login("codex")

    def test_ensure_codex_login_still_blocks_without_cli_status_or_auth_file(self) -> None:
        with (
            patch(
                "app.services.agent_kernel.provider_transport.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="Not logged in"),
            ),
            patch("app.services.agent_kernel.provider_transport.has_usable_codex_auth_file", return_value=False),
        ):
            with self.assertRaises(HTTPException) as caught:
                ensure_codex_login("codex")

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("codex login", str(caught.exception.detail))


if __name__ == "__main__":
    unittest.main()
