"""Private Codex worker uploaded to an account's Sandbox; standard library only.

No listening ports. Input arrives through the authenticated Sandbox filesystem
API; output uses the authenticated process stream. Never print credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = Path("/vercel/sandbox/drasil")
AUTH = ROOT / "auth"
WORK = ROOT / "work"
STATUS = ROOT / "status.json"


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def launch():
    os.umask(0o077)
    AUTH.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env["CODEX_HOME"] = str(AUTH)
    process = subprocess.Popen(
        [
            "codex",
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            "features.shell_tool=false",
            "app-server",
            "--listen",
            "stdio://",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        cwd=WORK,
        env=env,
    )

    def terminate(*_):
        process.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    return process


def relay(channel):
    # Only the host generates channel IDs. Each request has its own process.
    if len(channel) != 32 or any(c not in "0123456789abcdef" for c in channel):
        raise ValueError("Invalid channel")
    inbox = ROOT / "channels" / channel
    inbox.mkdir(parents=True, exist_ok=True)
    process = launch()
    finished = threading.Event()

    def input_loop():
        sequence = 1
        deadline = time.monotonic() + 260
        while process.poll() is None and time.monotonic() < deadline:
            path = inbox / f"{sequence:06d}.json"
            try:
                raw = path.read_text(encoding="utf-8")
                # Upload may still be in progress. Only consume complete JSON.
                if not raw.endswith("\n"):
                    time.sleep(0.02)
                    continue
                payload = json.loads(raw)
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
                continue
            path.unlink()
            if payload.get("_close") is True:
                break
            if payload.get("method") in {"thread/start", "thread/resume", "turn/start"}:
                params = payload.setdefault("params", {})
                params["cwd"] = str(WORK)
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            sequence += 1
        process.terminate()
        finished.set()

    threading.Thread(target=input_loop, daemon=True).start()
    try:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    finally:
        process.terminate()
        process.wait(timeout=5)
        shutil.rmtree(inbox, ignore_errors=True)


def login_failure_message(error):
    if isinstance(error, (TimeoutError, queue.Empty)):
        return "ChatGPT sign-in timed out. Start a fresh connection to get a new code."
    return "ChatGPT sign-in did not complete. Check the message on OpenAI's page, then start a fresh connection."


def login(session_id):
    process = launch()
    events = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                events.put(json.loads(line))
            except ValueError:
                continue
        events.put({"_closed": True})

    threading.Thread(target=reader, daemon=True).start()
    sequence = 0

    def request(method, params):
        nonlocal sequence
        sequence += 1
        process.stdin.write(json.dumps({"id": sequence, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0.1, deadline - time.monotonic()))
            if event.get("_closed") or "error" in event:
                raise RuntimeError("Codex sign-in failed")
            if event.get("id") == sequence:
                return event.get("result", {})
        raise TimeoutError()

    status = {
        "provider": "codex-cli",
        "isAuthenticated": False,
        "providerStatus": "ready",
        "pendingSession": {"sessionId": session_id, "status": "pending", "error": None},
    }

    def connected(account):
        status.update(
            isAuthenticated=True,
            authMode="chatgpt",
            email=account.get("email"),
            planType=account.get("planType"),
            lastRefresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        status["pendingSession"] = None
        write_json(STATUS, status)

    try:
        request("initialize", {"clientInfo": {"name": "drasil", "version": "1.0.0"}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        existing = request("account/read", {}).get("account") or {}
        if existing.get("type") == "chatgpt":
            connected(existing)
            return
        result = request("account/login/start", {"type": "chatgptDeviceCode"})
        if result.get("verificationUrl") != "https://auth.openai.com/codex/device":
            raise RuntimeError("Unexpected verification address")
        status["pendingSession"].update(
            verificationUri=result["verificationUrl"], userCode=result["userCode"]
        )
        write_json(STATUS, status)
        deadline = time.monotonic() + 540
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=1)
            except queue.Empty:
                continue
            if event.get("_closed"):
                raise RuntimeError("Login ended")
            if event.get("method") == "account/login/completed":
                if not event.get("params", {}).get("success"):
                    raise RuntimeError("Login failed")
                account = request("account/read", {}).get("account") or {}
                if account.get("type") != "chatgpt":
                    raise RuntimeError("ChatGPT subscription required")
                connected(account)
                return
        raise TimeoutError()
    except Exception as error:
        status["pendingSession"].update(
            status="failed",
            error=login_failure_message(error),
        )
        write_json(STATUS, status)
    finally:
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    if sys.argv[1] == "relay":
        relay(sys.argv[2])
    elif sys.argv[1] == "login":
        login(sys.argv[2])
