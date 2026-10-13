"""Account-isolated Codex on Vercel Sandbox, with no public worker ports."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from fastapi import HTTPException

from app.services.account_auth import active_account, _connect
from app.services.private_settings import load_settings, save_settings

ROOT = "/vercel/sandbox/drasil"
IDLE_SECONDS = 300
MAX_SESSION_SECONDS = 2700  # Hobby's 45-minute session ceiling.
KEY = "codex-sandbox"


def enabled() -> bool:
    return os.getenv("DRASIL_CODEX_SANDBOX", "") == "1"


def require_enabled():
    if not enabled():
        raise HTTPException(503, "Hosted ChatGPT connection is not configured yet.")


def new_client():
    from vercel.sandbox.sync.client import SyncSandboxClient

    # Production uses Vercel's request-scoped OIDC credentials. Credentials are
    # never copied into the guest or returned to the browser.
    return SyncSandboxClient.create()


@contextmanager
def account_lock():
    account = active_account()
    value = int.from_bytes(
        hashlib.sha256(("codex:" + account.id).encode()).digest()[:8], "big", signed=True
    )
    with _connect() as connection:
        row = connection.execute("SELECT pg_try_advisory_xact_lock(?)", (value,)).fetchone()
        if not row[0]:
            raise HTTPException(
                409, "ChatGPT is busy with another request. Please try again shortly."
            )
        yield


def disconnected():
    return {
        "provider": "codex-cli",
        "isAuthenticated": False,
        "providerStatus": "ready" if enabled() else "unavailable",
        "pendingSession": None,
    }


def public_status(value):
    # Only an explicit public allowlist may leave the worker.
    keys = {
        "provider",
        "isAuthenticated",
        "providerStatus",
        "authMode",
        "email",
        "planType",
        "lastRefresh",
        "pendingSession",
    }
    result = {key: item for key, item in value.items() if key in keys}
    pending = result.get("pendingSession")
    if isinstance(pending, dict):
        result["pendingSession"] = {
            key: pending.get(key)
            for key in ("sessionId", "status", "verificationUri", "userCode", "error")
        }
        if pending.get("verificationUri") not in (None, "https://auth.openai.com/codex/device"):
            raise ValueError("Unexpected OpenAI verification address")
    return {**disconnected(), **result}


def _running(box):
    return str(getattr(box.status, "value", box.status)).lower() == "running"


def _started(session):
    started = session.started_at or session.requested_at or int(time.time() * 1000)
    return started / 1000 if started > 100_000_000_000 else started


def keep_awake(box, seconds=IDLE_SECONDS):
    """Let Vercel enforce shutdown, even if the function disappears.

    Extend the session deadline to last activity + idle interval. Passive status
    polls never call this. No in-function timers or frequent cron jobs required.
    """
    session = box.current_session
    if session is None:
        raise RuntimeError("Sandbox has no active session")
    session.refresh()
    elapsed = max(0, time.time() - _started(session))
    current = session.execution_time_limit.total_seconds()
    target = min(MAX_SESSION_SECONDS, math.ceil(elapsed + seconds))
    if target > current:
        session.extend_execution_time_limit(target - current)


def acquire_box(client, state):
    from vercel.sandbox.sync import SandboxResources, SnapshotRetention

    if not state.get("name"):
        account = active_account()
        digest = hashlib.sha256(account.id.encode()).hexdigest()[:20]
        state["name"] = f"drasil-codex-{digest}-{uuid4().hex[:8]}"
        save_settings(KEY, state)
    box, created = client.get_or_create_sandbox(
        name=state["name"],
        ports=[],
        persistent=True,
        execution_time_limit=IDLE_SECONDS,
        resources=SandboxResources(vcpus=1),
        snapshot_expiration=0,
        snapshot_retention=SnapshotRetention(count=1, expiration=0, delete_evicted=True),
        tags={"app": "drasil", "purpose": "private-codex"},
    )
    # Rotate near the Hobby ceiling between requests, never during a turn.
    session = box.current_session
    if session and time.time() - _started(session) > MAX_SESSION_SECONDS - 600:
        box.stop()
        box, _ = client.get_or_create_sandbox(name=state["name"])
    if created:
        result = box.run_process("codex", ["--version"], capture_output=True, kill_after=20)
        if result.returncode:
            raise RuntimeError("Sandbox image does not contain Codex")
    box.fs.write_text(
        ROOT + "/guest.py",
        Path(__file__).with_name("sandbox_guest.py").read_text(encoding="utf-8"),
        mode=0o600,
    )
    keep_awake(box)
    return box


def get_status():
    require_enabled()
    state = load_settings(KEY)
    status = state.get("status") or disconnected()
    pending = status.get("pendingSession") or {}
    if pending.get("status") != "pending":
        return public_status(status)
    # A passive lookup must not resume a sleeping VM or extend its timeout.
    with account_lock():
        state = load_settings(KEY)
        status = state.get("status") or disconnected()
        if (status.get("pendingSession") or {}).get("status") != "pending":
            return public_status(status)
        client = new_client()
        try:
            box = client.get_sandbox(name=state["name"])
            if _running(box):
                # Pin all operations to the observed session: no auto-resume race.
                status = public_status(
                    json.loads(box.current_session.fs.read_text(ROOT + "/status.json"))
                )
            else:
                status = disconnected()
            if status.get("isAuthenticated"):
                # Sign-in has a longer human interaction window. Stop once it
                # completes; subsequent chat wakes with the five-minute budget.
                box.stop()
                provider = load_settings("provider")
                save_settings("provider", {**provider, "activeProvider": "codex-cli"})
            state["status"] = status
            save_settings(KEY, state)
            return public_status(status)
        except HTTPException:
            raise
        except Exception:
            if time.time() - state.get("loginStarted", 0) > 600:
                status = disconnected()
                state["status"] = status
                save_settings(KEY, state)
                return status
            raise HTTPException(503, "Could not check ChatGPT sign-in. Please try again.") from None
        finally:
            client.close()


def start_login():
    require_enabled()
    with account_lock():
        state = load_settings(KEY)
        status = state.get("status") or disconnected()
        if status.get("isAuthenticated"):
            return public_status(status)
        pending = status.get("pendingSession") or {}
        if pending.get("status") == "pending" and time.time() - state.get("loginStarted", 0) < 600:
            return public_status(status)
        client = new_client()
        try:
            box = acquire_box(client, state)
            keep_awake(box, 600)
            session_id = uuid4().hex
            status = {
                **disconnected(),
                "pendingSession": {"sessionId": session_id, "status": "pending"},
            }
            state.update(status=status, loginStarted=time.time())
            save_settings(KEY, state)
            box.fs.write_text(ROOT + "/status.json", json.dumps(status), mode=0o600)
            box.create_process(
                "python3",
                [ROOT + "/guest.py", "login", session_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                kill_after=580,
            )
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                status = public_status(
                    json.loads(box.current_session.fs.read_text(ROOT + "/status.json"))
                )
                pending = status.get("pendingSession") or {}
                if (
                    status.get("isAuthenticated")
                    or pending.get("userCode")
                    or pending.get("status") == "failed"
                ):
                    state["status"] = status
                    save_settings(KEY, state)
                    if status.get("isAuthenticated"):
                        box.stop()
                        save_settings(
                            "provider", {**load_settings("provider"), "activeProvider": "codex-cli"}
                        )
                    return status
                time.sleep(0.4)
            raise HTTPException(504, "ChatGPT sign-in is taking too long. Try again in a moment.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                503,
                "Could not start the hosted AI. Check Sandbox availability and usage in Vercel.",
            ) from None
        finally:
            client.close()


def logout():
    require_enabled()
    with account_lock():
        state = load_settings(KEY)
        if state.get("name"):
            from vercel.sandbox.sync import SandboxApiError

            client = new_client()
            try:
                box = client.get_sandbox(name=state["name"])
                # Destroy all worker credentials, including retained snapshots.
                box.destroy(delete_orphan_snapshots=True)
            except SandboxApiError as error:
                if error.status_code != 404:
                    raise HTTPException(
                        503, "Could not disconnect ChatGPT. Please try again."
                    ) from None
            finally:
                client.close()
        save_settings(KEY, {})
        return disconnected()


class SandboxTransport:
    """Private filesystem inbox + streaming stdout for the existing RPC client."""

    def __init__(self, messages):
        self.messages = messages
        self.client = None
        self.box = None
        self.process = None
        self.closed = False
        self.error = False
        self.channel = uuid4().hex
        self.sequence = 0
        self.last_touch = 0.0
        self.lock = account_lock()
        self.locked = False

    def start(self):
        require_enabled()
        self.lock.__enter__()
        self.locked = True
        try:
            state = load_settings(KEY)
            if not (state.get("status") or {}).get("isAuthenticated"):
                raise HTTPException(401, "Connect ChatGPT in Settings → Connections to use AI.")
            from app.services.agent_stream import emit_progress

            emit_progress("activity", "Starting AI…")
            self.client = new_client()
            self.box = acquire_box(self.client, state)
            self.session = self.box.current_session
            self.process = self.session.create_process(
                "python3",
                [ROOT + "/guest.py", "relay", self.channel],
                stderr=subprocess.DEVNULL,
                kill_after=270,
            )
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            self.last_touch = time.monotonic()
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(("stdout", line.rstrip("\n")))
            if not self.closed:
                self.error = True
        except Exception:
            if not self.closed:
                self.error = True

    def send(self, payload):
        self.sequence += 1
        self.session.fs.write_text(
            f"{ROOT}/channels/{self.channel}/{self.sequence:06d}.json",
            json.dumps(payload) + "\n",
            mode=0o600,
        )

    def check(self):
        if self.error:
            raise RuntimeError("The hosted Codex connection closed. Please retry.")
        if time.monotonic() - self.last_touch > 60:
            keep_awake(self.box)
            self.last_touch = time.monotonic()

    def close(self):
        self.closed = True
        try:
            if self.process:
                self.process.terminate()
                if hasattr(self, "reader"):
                    self.reader.join(timeout=5)
            if self.box:
                keep_awake(self.box)
        except Exception:
            pass  # The platform deadline still enforces a bounded lifetime.
        finally:
            if self.client:
                self.client.close()
            if self.locked:
                self.lock.__exit__(None, None, None)
                self.locked = False
