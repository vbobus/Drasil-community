#!/usr/bin/env python3
"""Fail closed when a Drasil public tree contains private or generated data."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REQUIRED_FILES = {
    ".dockerignore",
    ".env.example",
    ".github/workflows/ci.yml",
    ".gitignore",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "compose.yaml",
    "desktop/package.json",
    "backend/.env.example",
    "frontend/.env.example",
}

BANNED_DIRECTORY_NAMES = {
    ".agent",
    ".agents",
    ".codex",
    ".eval-artifacts",
    ".impeccable",
    ".pytest_cache",
    ".ruff_cache",
    ".secrets",
    ".tmp",
    ".venv",
    "__pycache__",
    "_bmad",
    "_bmad-output",
    "blob-report",
    "coverage",
    "dist",
    "evals",
    "htmlcov",
    "node_modules",
    "out",
    "playwright-report",
    "scratch",
    "test-results",
    "tmp",
    "venv",
}

BANNED_FILE_NAMES = {
    ".google-calendar.json",
    ".strategist-provider.json",
    "drasil_specification.md",
    "evals.json",
    "auth.json",
    "credentials.json",
    "private_extensions.py",
    "strategist_eval.py",
    "strategist_eval_fixture.py",
}

PRIVATE_EVALUATION_TEST_MARKERS = (
    "app.evals",
    "offline_shadow_evidence",
    "strategist_eval_fixture",
    "unfinished_task_move.json",
    "driving_appointment_tool_call.json",
)

BANNED_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".duckdb",
    ".har",
    ".log",
    ".sqlite",
    ".sqlite3",
    ".trace",
}

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "OpenAI-style API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Stripe secret key": re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),
    "private deployment coordinates": re.compile(
        r"\bdrasil-[a-z0-9]+\.vercel\.app|[a-z0-9]+-projects-[a-f0-9]{8,}|\bdpl_[A-Za-z0-9]{20,}\b",
        re.IGNORECASE,
    ),
    "personal Windows home path": re.compile(r"[A-Z]:[/\\]Users[/\\](?!Public\b|example\b|test\b)[A-Za-z0-9_.-]+", re.IGNORECASE),
}

SENSITIVE_ENV_NAME = re.compile(
    r"(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE
)
TEXT_FILE_SUFFIXES = {
    ".css",
    ".cjs",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".ps1",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".webmanifest",
    ".yaml",
    ".yml",
}
MAX_PUBLIC_FILE_BYTES = 10 * 1024 * 1024


def _display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _validate_env_example(path: Path, text: str, issues: list[str]) -> None:
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if SENSITIVE_ENV_NAME.search(name) and value.strip():
            issues.append(
                f"{path.as_posix()}:{line_number}: sensitive example value must be blank"
            )


def verify_public_tree(root: Path) -> tuple[int, int]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Public tree does not exist: {root}")

    issues: list[str] = []
    file_count = 0
    total_bytes = 0

    for required in sorted(REQUIRED_FILES):
        if not (root / required).is_file():
            issues.append(f"missing required public file: {required}")

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)

        retained_directories: list[str] = []
        for directory_name in directory_names:
            directory = current_path / directory_name
            if directory_name == ".git":
                continue
            relative = _display(directory, root)
            if directory.is_symlink():
                issues.append(f"symlink is not allowed: {relative}")
                continue
            if directory_name.casefold() in BANNED_DIRECTORY_NAMES:
                issues.append(f"private or generated directory present: {relative}")
                continue
            retained_directories.append(directory_name)
        directory_names[:] = retained_directories

        for file_name in file_names:
            path = current_path / file_name
            relative = _display(path, root)
            lowered_name = file_name.casefold()

            if path.is_symlink():
                issues.append(f"symlink is not allowed: {relative}")
                continue
            if lowered_name in BANNED_FILE_NAMES:
                issues.append(f"private file present: {relative}")
            if relative.casefold().startswith("backend/tests/fixtures/"):
                issues.append(f"private incident fixture present: {relative}")
            if lowered_name == ".env" or (
                lowered_name.startswith(".env.") and lowered_name != ".env.example"
            ):
                issues.append(f"real environment file present: {relative}")
            if any(lowered_name.endswith(suffix) for suffix in BANNED_SUFFIXES):
                issues.append(f"database or generated artifact present: {relative}")

            size = path.stat().st_size
            file_count += 1
            total_bytes += size
            if size > MAX_PUBLIC_FILE_BYTES:
                issues.append(f"unexpectedly large public file ({size} bytes): {relative}")

            if path.suffix.casefold() not in TEXT_FILE_SUFFIXES and file_name not in {
                ".dockerignore",
                ".editorconfig",
                ".env.example",
                ".gitattributes",
                ".gitignore",
                "LICENSE",
                "NOTICE",
                "Dockerfile",
            }:
                continue

            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                issues.append(f"expected UTF-8 text file is binary: {relative}")
                continue

            for description, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    issues.append(f"possible {description} in {relative}")

            if relative.casefold().startswith(("backend/tests/", "frontend/tests/")):
                normalized_text = text.casefold()
                if "eval" in lowered_name or any(
                    marker in normalized_text
                    for marker in PRIVATE_EVALUATION_TEST_MARKERS
                ):
                    issues.append(f"private evaluation test present: {relative}")

            if lowered_name == ".env.example":
                _validate_env_example(Path(relative), text, issues)

    if issues:
        details = "\n".join(f"- {issue}" for issue in sorted(set(issues)))
        raise ValueError(f"Public export verification failed:\n{details}")

    return file_count, total_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        type=Path,
        help="Public repository root (default: current directory)",
    )
    args = parser.parse_args()

    try:
        file_count, total_bytes = verify_public_tree(args.root)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    print(
        f"Public export verified: {file_count} files, "
        f"{total_bytes / (1024 * 1024):.1f} MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
