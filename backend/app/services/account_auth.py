"""Drasil account registry, password authentication, and request context.

The registry is deliberately separate from every user's planning database.
Each account receives an isolated SQLite database and private configuration
directory, which prevents an omitted query filter from crossing account data.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from app.db.hosted import hosted_storage, hosted_runtime, RegistryConnection, registry_schema

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUTH_DATABASE_PATH = BACKEND_ROOT / "drasil-auth.db"
DEFAULT_LEGACY_DATABASE_PATH = BACKEND_ROOT / "drasil.db"
DEFAULT_ACCOUNT_DATA_ROOT = BACKEND_ROOT / "data" / "accounts"
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,47}$")
PUBLIC_API_PATHS = {
    "/api/v1/health",
    "/api/v1/accounts/status",
    "/api/v1/accounts/bootstrap",
    "/api/v1/accounts/login",
}
GOOGLE_OAUTH_CALLBACK_PATH = "/api/v1/calendar-integrations/google/callback"
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 8
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_FAILURES_LOCK = threading.Lock()
_REGISTRY_READY: set[str] = set()
_REGISTRY_LOCK = threading.Lock()
SESSION_COOKIE = "__Host-drasil-session"


class _ClosingConnection(sqlite3.Connection):
    """Make ``with _connect()`` commit or roll back and then close on Windows."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


@dataclass(frozen=True)
class AccountRecord:
    id: str
    username: str
    display_name: str
    is_admin: bool
    data_directory: Path
    database_path: Path

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "displayName": self.display_name,
            "isAdmin": self.is_admin,
        }


_ACTIVE_ACCOUNT: contextvars.ContextVar[AccountRecord | None] = contextvars.ContextVar(
    "drasil_active_account",
    default=None,
)


def _auth_database_path() -> Path:
    if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1" and not os.getenv(
        "DRASIL_AUTH_DATABASE_PATH"
    ):
        worker = os.getenv("DRASIL_TEST_WORKER_ID", f"pid-{os.getpid()}")
        return (Path(tempfile.gettempdir()) / "drasil-test-dbs" / f"auth-{worker}.db").resolve()
    return Path(
        os.getenv("DRASIL_AUTH_DATABASE_PATH", str(DEFAULT_AUTH_DATABASE_PATH))
    ).expanduser().resolve()


def _account_data_root() -> Path:
    if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1" and not os.getenv(
        "DRASIL_ACCOUNT_DATA_ROOT"
    ):
        worker = os.getenv("DRASIL_TEST_WORKER_ID", f"pid-{os.getpid()}")
        return (Path(tempfile.gettempdir()) / "drasil-test-dbs" / f"accounts-{worker}").resolve()
    return Path(
        os.getenv("DRASIL_ACCOUNT_DATA_ROOT", str(DEFAULT_ACCOUNT_DATA_ROOT))
    ).expanduser().resolve()


def _connect() -> sqlite3.Connection:
    if hosted_storage():
        return RegistryConnection()
    path = _auth_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, factory=_ClosingConnection)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_account_store() -> None:
    """Create the account registry without creating or mutating user data."""
    if hosted_storage():
        with _REGISTRY_LOCK:
            namespace = registry_schema()
            if namespace in _REGISTRY_READY:
                return
            _initialize_account_store()
            _REGISTRY_READY.add(namespace)
        return
    _initialize_account_store()


def _initialize_account_store() -> None:
    with _connect() as connection:
        if hosted_storage():
            from app.db.schema_version import REGISTRY_SCHEMA_VERSION, current_schema_version, record_schema_version
            if current_schema_version(connection.connection, REGISTRY_SCHEMA_VERSION):
                return
            connection.execute("SELECT pg_advisory_xact_lock(684291001)")
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{registry_schema()}"')
            if current_schema_version(connection.connection, REGISTRY_SCHEMA_VERSION):
                return
        else:
            connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                data_directory TEXT NOT NULL UNIQUE,
                database_path TEXT NOT NULL UNIQUE,
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_sessions (
                token_hash BLOB PRIMARY KEY,
                account_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_account_sessions_account_id
                ON account_sessions(account_id);
            CREATE INDEX IF NOT EXISTS ix_account_sessions_expires_at
                ON account_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS external_auth_states (
                state_hash BLOB PRIMARY KEY,
                account_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_external_auth_states_expires_at
                ON external_auth_states(expires_at);
            CREATE TABLE IF NOT EXISTS private_settings (
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                encrypted_value TEXT NOT NULL,
                PRIMARY KEY (account_id, name)
            );
            CREATE TABLE IF NOT EXISTS login_buckets (
                bucket TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        if hosted_storage():
            record_schema_version(connection.connection, REGISTRY_SCHEMA_VERSION)


def _normalize_username(value: str) -> str:
    username = value.strip().casefold()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 3-48 characters using letters, numbers, dots, dashes, or underscores."
        )
    return username


def _validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters.")
    if len(password) > 512:
        raise ValueError("Password is unexpectedly long.")


def _password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )


def _row_to_account(row: sqlite3.Row) -> AccountRecord:
    return AccountRecord(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        is_admin=bool(row["is_admin"]),
        data_directory=Path(str(row["data_directory"])).resolve(),
        database_path=Path(str(row["database_path"])).resolve(),
    )


def account_count() -> int:
    initialize_account_store()
    with _connect() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])


def list_accounts() -> list[AccountRecord]:
    initialize_account_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM accounts WHERE disabled = 0 ORDER BY display_name, username"
        ).fetchall()
    return [_row_to_account(row) for row in rows]


def create_account(
    *,
    username: str,
    display_name: str,
    password: str,
    is_admin: bool = False,
    first_account_only: bool = False,
) -> AccountRecord:
    """Create an isolated account; the first account claims a legacy database."""

    initialize_account_store()
    normalized_username = _normalize_username(username)
    normalized_display_name = display_name.strip()
    if not normalized_display_name or len(normalized_display_name) > 80:
        raise ValueError("Display name must contain 1-80 characters.")
    _validate_password(password)

    account_id = str(uuid4())
    data_directory = (
        Path(tempfile.gettempdir()) / "drasil" / account_id
        if hosted_storage() else _account_data_root() / account_id
    )
    if not hosted_storage():
        data_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    salt = secrets.token_bytes(16)
    now = time.time()

    try:
        with _connect() as connection:
            if hosted_storage():
                connection.execute("SELECT pg_advisory_xact_lock(684291002)")
            else:
                connection.execute("BEGIN IMMEDIATE")
            existing_count = int(
                connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            )
            if first_account_only and existing_count:
                raise ValueError("Drasil has already been initialized.")
            legacy_database_path = Path(
                os.getenv(
                    "DRASIL_LEGACY_DATABASE_PATH",
                    str(DEFAULT_LEGACY_DATABASE_PATH),
                )
            ).expanduser().resolve()
            use_legacy_database = not hosted_storage() and existing_count == 0 and legacy_database_path.exists()
            database_path = (
                legacy_database_path
                if use_legacy_database
                else (data_directory / "drasil.db").resolve()
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, username, display_name, password_salt, password_hash,
                    is_admin, data_directory, database_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    normalized_username,
                    normalized_display_name,
                    salt,
                    _password_digest(password, salt),
                    1 if is_admin or existing_count == 0 else 0,
                    str(data_directory.resolve()),
                    str(database_path),
                    now,
                ),
            )
    except Exception:
        try:
            data_directory.rmdir()
        except OSError:
            pass
        raise

    account = get_account(account_id)
    if account is None:
        raise RuntimeError("The new Drasil account could not be loaded.")
    if existing_count == 0 and not hosted_storage():
        _copy_legacy_private_configuration(account)
    return account


def _copy_legacy_private_configuration(account: AccountRecord) -> None:
    """Preserve existing single-user settings when the first account is created."""

    for file_name in (".strategist-provider.json", ".google-calendar.json"):
        source = BACKEND_ROOT / file_name
        destination = account.data_directory / file_name
        if source.is_file() and not destination.exists():
            shutil.copy2(source, destination)

    configured_codex_home = os.getenv("CODEX_HOME", "").strip()
    legacy_codex_home = (
        Path(configured_codex_home).expanduser()
        if configured_codex_home
        else Path.home() / ".codex"
    )
    source_auth = legacy_codex_home / "auth.json"
    destination_auth = account.data_directory / ".codex" / "auth.json"
    if source_auth.is_file() and not destination_auth.exists():
        destination_auth.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_auth, destination_auth)


def get_account(account_id: str) -> AccountRecord | None:
    initialize_account_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM accounts WHERE id = ? AND disabled = 0",
            (account_id,),
        ).fetchone()
    return _row_to_account(row) if row is not None else None


def find_account_by_username(username: str) -> AccountRecord | None:
    initialize_account_store()
    try:
        normalized_username = _normalize_username(username)
    except ValueError:
        return None
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM accounts WHERE username = ? AND disabled = 0",
            (normalized_username,),
        ).fetchone()
    return _row_to_account(row) if row is not None else None


def authenticate_account(username: str, password: str) -> AccountRecord | None:
    initialize_account_store()
    try:
        normalized_username = _normalize_username(username)
    except ValueError:
        return None
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM accounts WHERE username = ? AND disabled = 0",
            (normalized_username,),
        ).fetchone()
    if row is None:
        # Perform comparable work to reduce username-enumeration timing differences.
        _password_digest(password[:512], bytes(16))
        return None
    expected = bytes(row["password_hash"])
    actual = _password_digest(password, bytes(row["password_salt"]))
    return _row_to_account(row) if hmac.compare_digest(expected, actual) else None


def change_account_password(
    account: AccountRecord,
    *,
    current_password: str,
    new_password: str,
) -> bool:
    if authenticate_account(account.username, current_password) is None:
        return False
    _validate_password(new_password)
    salt = secrets.token_bytes(16)
    with _connect() as connection:
        connection.execute(
            "UPDATE accounts SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt, _password_digest(new_password, salt), account.id),
        )
        connection.execute(
            "DELETE FROM account_sessions WHERE account_id = ?",
            (account.id,),
        )
    return True


def create_session(account: AccountRecord) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    now = time.time()
    lifetime_seconds = max(
        900,
        int(os.getenv("DRASIL_SESSION_LIFETIME_SECONDS", str(30 * 24 * 3600))),
    )
    with _connect() as connection:
        connection.execute("DELETE FROM account_sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            """
            INSERT INTO account_sessions (
                token_hash, account_id, created_at, last_seen_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, account.id, now, now, now + lifetime_seconds),
        )
    return token


def resolve_session(token: str | None) -> AccountRecord | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    now = time.time()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT accounts.*, account_sessions.last_seen_at AS session_last_seen_at
            FROM account_sessions
            JOIN accounts ON accounts.id = account_sessions.account_id
            WHERE account_sessions.token_hash = ?
              AND account_sessions.expires_at > ?
              AND accounts.disabled = 0
            """,
            (token_hash, now),
        ).fetchone()
        if row is not None and float(row["session_last_seen_at"]) < now - 5 * 60:
            connection.execute(
                "UPDATE account_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
    return _row_to_account(row) if row is not None else None


def revoke_session(token: str | None) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM account_sessions WHERE token_hash = ?",
            (token_hash,),
        )


def _login_key(request: Request, username: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{host.casefold()}:{username.strip().casefold()}"


def assert_login_attempt_allowed(request: Request, username: str) -> str:
    """Apply a small process-local brake to repeated password guesses."""

    key = _login_key(request, username)
    if hosted_storage():
        initialize_account_store()
        # Count attempts BEFORE password hashing. PostgreSQL serializes competing
        # requests, including requests handled by different Vercel instances.
        host = request.client.host if request.client else "unknown"
        buckets = (("user:" + username.strip().casefold(), 12), ("ip:" + host, 60))
        now = time.time()
        blocked = False
        with _connect() as connection:
            connection.execute("DELETE FROM login_buckets WHERE expires_at <= ?", (now,))
            for value, maximum in sorted(buckets):
                digest = hashlib.sha256(value.encode()).hexdigest()
                row = connection.execute(
                    "INSERT INTO login_buckets (bucket, attempts, expires_at) VALUES (?, 1, ?) "
                    "ON CONFLICT (bucket) DO UPDATE SET attempts = login_buckets.attempts + 1 "
                    "RETURNING attempts", (digest, now + LOGIN_WINDOW_SECONDS),
                ).fetchone()
                blocked = blocked or row[0] > maximum
        if blocked:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.",
                                headers={"Retry-After": str(LOGIN_WINDOW_SECONDS)})
        return key
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    with _LOGIN_FAILURES_LOCK:
        recent = [stamp for stamp in _LOGIN_FAILURES.get(key, []) if stamp > cutoff]
        if recent:
            _LOGIN_FAILURES[key] = recent
        else:
            _LOGIN_FAILURES.pop(key, None)
        if len(recent) >= LOGIN_MAX_FAILURES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many sign-in attempts. Wait a few minutes and try again.",
                headers={"Retry-After": str(LOGIN_WINDOW_SECONDS)},
            )
    return key


def record_login_result(key: str, *, succeeded: bool) -> None:
    if hosted_storage():
        return
    with _LOGIN_FAILURES_LOCK:
        if succeeded:
            _LOGIN_FAILURES.pop(key, None)
        else:
            _LOGIN_FAILURES.setdefault(key, []).append(time.monotonic())


def register_external_auth_state(
    state_value: str,
    *,
    provider: str,
    lifetime_seconds: int = 15 * 60,
) -> None:
    initialize_account_store()
    account = active_account(required=False)
    if account is None and os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
        return
    if account is None:
        raise RuntimeError("External authorization requires an active Drasil account.")
    now = time.time()
    state_hash = hashlib.sha256(state_value.encode("utf-8")).digest()
    with _connect() as connection:
        connection.execute("DELETE FROM external_auth_states WHERE expires_at <= ?", (now,))
        connection.execute(
            """
            INSERT INTO external_auth_states (
                state_hash, account_id, provider, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (state_hash, account.id, provider, now, now + lifetime_seconds),
        )


def resolve_external_auth_state(
    state_value: str | None,
    *,
    provider: str,
) -> AccountRecord | None:
    if not state_value:
        return None
    initialize_account_store()
    now = time.time()
    state_hash = hashlib.sha256(state_value.encode("utf-8")).digest()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT accounts.*
            FROM external_auth_states
            JOIN accounts ON accounts.id = external_auth_states.account_id
            WHERE external_auth_states.state_hash = ?
              AND external_auth_states.provider = ?
              AND external_auth_states.expires_at > ?
              AND accounts.disabled = 0
            """,
            (state_hash, provider, now),
        ).fetchone()
    return _row_to_account(row) if row is not None else None


def revoke_external_auth_state(state_value: str | None, *, provider: str) -> None:
    if not state_value:
        return
    initialize_account_store()
    state_hash = hashlib.sha256(state_value.encode("utf-8")).digest()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM external_auth_states WHERE state_hash = ? AND provider = ?",
            (state_hash, provider),
        )


def request_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.casefold() == "bearer" and value.strip():
        return value.strip()
    return request.cookies.get(SESSION_COOKIE) if hosted_runtime() else None


def active_account(*, required: bool = True) -> AccountRecord | None:
    account = _ACTIVE_ACCOUNT.get()
    if account is None and required:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to a Drasil account to continue.",
        )
    return account


@contextlib.contextmanager
def use_account(account: AccountRecord | None) -> Iterator[None]:
    token = _ACTIVE_ACCOUNT.set(account)
    try:
        yield
    finally:
        _ACTIVE_ACCOUNT.reset(token)


def account_private_path(name: str) -> Path:
    account = active_account(required=False)
    if account is None:
        if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
            return BACKEND_ROOT / name
        raise RuntimeError("Private configuration access requires an active Drasil account.")
    path = account.data_directory / name
    if not hosted_storage():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def account_process_environment() -> dict[str, str]:
    """Return a child-process environment scoped to the active account."""

    environment = os.environ.copy()
    account = active_account(required=False)
    if account is None:
        if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
            return environment
        raise RuntimeError("Provider processes require an active Drasil account.")
    codex_home = account.data_directory / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def bootstrap_request_allowed(request: Request, supplied_token: str | None) -> bool:
    if not hosted_storage() and _is_loopback_request(request):
        return True
    configured = os.getenv("DRASIL_BOOTSTRAP_TOKEN", "").strip()
    return bool(
        configured
        and supplied_token
        and hmac.compare_digest(configured, supplied_token.strip())
    )


class AccountContextMiddleware(BaseHTTPMiddleware):
    """Authenticate API requests and establish their isolated account context."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if hosted_runtime() and os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
            return JSONResponse(status_code=503, content={"detail": "Invalid server configuration."})
        if hosted_runtime() and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = os.getenv("DRASIL_FRONTEND_ORIGIN", "").rstrip("/")
            if origin and origin.rstrip("/") != expected:
                return JSONResponse(status_code=403, content={"detail": "Request origin is not permitted."})
        if request.method == "OPTIONS" or not path.startswith("/api/"):
            return await call_next(request)
        if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
            return await call_next(request)
        if path in PUBLIC_API_PATHS:
            response = await call_next(request)
            if path.startswith("/api/v1/accounts"):
                response.headers["Cache-Control"] = "no-store"
            return response

        if path == GOOGLE_OAUTH_CALLBACK_PATH:
            account = await run_in_threadpool(resolve_external_auth_state,
                request.query_params.get("state"),
                provider="google-calendar",
            )
            if account is None:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "This Google Calendar connection is missing or expired."},
                )
            request.state.drasil_account = account
            with use_account(account):
                return await call_next(request)

        token = request_token(request)
        if token and token.startswith("dcw."):
            # A worker token is never a browser login and cannot approve or mutate tasks.
            if not path.startswith("/api/v1/companion/worker/"):
                return JSONResponse(status_code=403, content={"detail": "Worker credential cannot access this endpoint."})
            from app.services.companion import authenticate_worker
            account = await run_in_threadpool(authenticate_worker, token)
            if account is None:
                return JSONResponse(status_code=401, content={"detail": "Reconnect the companion worker."})
            request.state.drasil_account = account
            request.state.companion_worker = True
            with use_account(account):
                response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        from app.core.request_timing import timed
        with timed("auth"):
            account = await run_in_threadpool(resolve_session, token)
        if account is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Sign in to a Drasil account to continue."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.drasil_account = account
        request.state.drasil_session_token = token
        if hosted_runtime() and path.startswith("/api/v1/admin/") and not account.is_admin:
            return JSONResponse(status_code=403, content={"detail": "Administrator access required."})
        with use_account(account):
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
