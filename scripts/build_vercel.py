"""Build the React frontend into Vercel's static CDN directory."""
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if (root / "requirements.txt").read_text() != (root / "backend/requirements.txt").read_text():
    raise RuntimeError("Sync requirements.txt from backend/requirements.txt before deploying.")
npm = "npm.cmd" if os.name == "nt" else "npm"
environment = {**os.environ, "VITE_DRASIL_HOSTED": "1"}
subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=root / "frontend", check=True, env=environment)
subprocess.run([npm, "run", "build"], cwd=root / "frontend", check=True, env=environment)
shutil.copytree(root / "frontend" / "dist", root / "public", dirs_exist_ok=True)
