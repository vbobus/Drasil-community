"""
Codex CLI discovery helpers.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

from app.core.env import load_local_env


def _normalize_candidate(raw_value: str | None) -> Path | None:
    if not raw_value:
        return None
    candidate = Path(raw_value).expanduser()
    return candidate if candidate.exists() else None


def _find_antigravity_codex() -> Path | None:
    matches = list_codex_cli_candidates(include_non_antigravity=False)
    return matches[0] if matches else None


def _get_preferred_windows_tag() -> str | None:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "windows-aarch64"
    if machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    return None


def _candidate_rank(path: Path) -> tuple[int, int, int, int, str]:
    normalized = str(path).replace("\\", "/").lower()
    preferred_tag = _get_preferred_windows_tag()
    version_match = re.search(
        r"openai\.chatgpt-(\d+)\.(\d+)\.(\d+)",
        normalized,
    )
    version = (
        tuple(int(part) for part in version_match.groups())
        if version_match
        else (0, 0, 0)
    )
    if preferred_tag and preferred_tag in normalized:
        architecture_rank = 0
    elif "/windows-" not in normalized:
        architecture_rank = 1
    else:
        architecture_rank = 2
    return (
        architecture_rank,
        -version[0],
        -version[1],
        -version[2],
        normalized,
    )


def list_codex_cli_candidates(*, include_non_antigravity: bool = True) -> list[Path]:
    home = Path.home()
    extensions_dir = home / ".antigravity" / "extensions"
    candidates: list[Path] = []

    if include_non_antigravity:
        discovered = shutil.which("codex") or shutil.which("codex.exe")
        if discovered:
            candidates.append(Path(discovered))

    if extensions_dir.exists():
        candidates.extend(extensions_dir.glob("openai.chatgpt-*/bin/windows-*/codex.exe"))
    if include_non_antigravity:
        for editor_extensions_dir in (
            home / ".vscode" / "extensions",
            home / ".vscode-insiders" / "extensions",
        ):
            if editor_extensions_dir.exists():
                candidates.extend(
                    editor_extensions_dir.glob(
                        "openai.chatgpt-*/bin/windows-*/codex.exe",
                    ),
                )

    deduped: dict[str, Path] = {}
    for candidate in candidates:
        deduped[str(candidate).lower()] = candidate

    return sorted(deduped.values(), key=_candidate_rank)


def find_codex_cli_path() -> Path | None:
    load_local_env()

    configured_path = _normalize_candidate(os.getenv("CODEX_CLI_PATH"))
    if configured_path is not None:
        return configured_path

    candidates = list_codex_cli_candidates()
    if candidates:
        return candidates[0]

    return None


def require_codex_cli_path(context: str) -> str:
    codex_path = find_codex_cli_path()
    if codex_path is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{context} requires the local Codex CLI, but it was not found. "
                "Install/sign into ChatGPT with Codex support or set CODEX_CLI_PATH "
                "in backend/.env to the full codex.exe path."
            ),
        )
    return str(codex_path)
