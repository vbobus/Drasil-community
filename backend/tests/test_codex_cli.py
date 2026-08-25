from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.codex_cli import find_codex_cli_path


class CodexCliDiscoveryTests(unittest.TestCase):
    def test_newest_discovered_installation_is_selected(self) -> None:
        current = Path(
            r"C:\Users\test\.vscode\extensions\openai.chatgpt-26.707.41301-win32-x64\bin\windows-x86_64\codex.exe",
        )
        older = Path(
            r"C:\Users\test\.antigravity\extensions\openai.chatgpt-26.506.31421-win32-x64\bin\windows-x86_64\codex.exe",
        )

        with patch.dict(os.environ, {"CODEX_CLI_PATH": ""}), patch(
            "app.core.codex_cli.load_local_env",
        ), patch(
            "app.core.codex_cli.list_codex_cli_candidates",
            return_value=[current, older],
        ):
            selected = find_codex_cli_path()

        self.assertEqual(selected, current)

    def test_candidate_sorting_prefers_newer_extension_version(self) -> None:
        from app.core.codex_cli import _candidate_rank

        older = Path(
            r"C:\Users\test\.antigravity\extensions\openai.chatgpt-26.506.31421-win32-x64\bin\windows-x86_64\codex.exe",
        )
        newer = Path(
            r"C:\Users\test\.vscode\extensions\openai.chatgpt-26.707.41301-win32-x64\bin\windows-x86_64\codex.exe",
        )

        self.assertEqual(sorted([older, newer], key=_candidate_rank)[0], newer)


if __name__ == "__main__":
    unittest.main()
