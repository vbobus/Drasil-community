"""Vercel's FastAPI entrypoint; browser assets are served by its CDN."""
import sys
from time import perf_counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

import_started_at = perf_counter()
from app.main import app  # noqa: E402, F401
app.state.import_ms = round((perf_counter() - import_started_at) * 1000, 2)

web_root = Path(__file__).resolve().parent / "public"
if web_root.is_dir():
    app.frontend("/", directory=web_root)
