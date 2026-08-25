"""
Minimal local .env loader.

This keeps the backend self-contained without adding a dotenv dependency.
"""

from __future__ import annotations

import os
from pathlib import Path

_HAS_LOADED_ENV = False
_DEFAULT_FRONTEND_ORIGIN = "http://localhost:5173"


def load_local_env() -> None:
    global _HAS_LOADED_ENV
    if _HAS_LOADED_ENV:
        return

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        _HAS_LOADED_ENV = True
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip('"').strip("'")
        if normalized_key:
            os.environ.setdefault(normalized_key, normalized_value)

    _HAS_LOADED_ENV = True


def cors_origins() -> list[str]:
    """Return the configured browser origins, preserving declaration order."""
    configured = os.getenv("DRASIL_CORS_ORIGINS", "")
    candidates = configured.split(",") if configured else []
    candidates.append(os.getenv("DRASIL_FRONTEND_ORIGIN", _DEFAULT_FRONTEND_ORIGIN))

    origins: list[str] = []
    for candidate in candidates:
        origin = candidate.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins
