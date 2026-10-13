"""PostgreSQL storage for hosted instances, with transaction-scoped namespaces.

Neon's transaction pooler can change connections between transactions. Never
rely on a session-level search_path or keep account data in process globals.
"""
from __future__ import annotations

import os
import re
import certifi
from functools import lru_cache
from uuid import UUID

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from app.core.request_timing import current_timings, timed
from time import perf_counter


def hosted_storage() -> bool:
    return os.getenv("DRASIL_STORAGE") == "postgres" or os.getenv("VERCEL") == "1"


def hosted_runtime() -> bool:
    return os.getenv("VERCEL") == "1"


def account_schema(account_id: str) -> str:
    return "account_" + UUID(account_id).hex


def registry_schema() -> str:
    name = os.getenv("DRASIL_REGISTRY_SCHEMA", "drasil_auth")
    if not re.fullmatch(r"drasil_auth(?:_test_[a-f0-9]{32})?", name):
        raise RuntimeError("Invalid account registry namespace.")
    return name


@lru_cache(maxsize=4)
def _connection_engine(value: str) -> Engine:
    if not value:
        raise RuntimeError("Hosted Drasil requires DATABASE_URL.")
    url = make_url(value)
    if url.get_backend_name() not in {"postgres", "postgresql"}:
        raise RuntimeError("Hosted Drasil requires PostgreSQL.")
    url = url.set(drivername="postgresql+psycopg")
    # Use the same maintained CA bundle on Windows and Linux. Require both
    # certificate and hostname verification, not just an encrypted connection.
    url = url.update_query_dict({"sslmode": "verify-full", "sslrootcert": certifi.where()})
    engine = create_engine(
        url, pool_size=4, max_overflow=2, pool_timeout=15,
        pool_pre_ping=True, pool_recycle=180, pool_use_lifo=True, hide_parameters=True,
        connect_args={"connect_timeout": 15, "prepare_threshold": None},
    )
    @event.listens_for(engine, "do_connect")
    def connect(dialect, connection_record, args, kwargs):
        with timed("connect"):
            return dialect.loaded_dbapi.connect(*args, **kwargs)
    @event.listens_for(engine, "before_cursor_execute")
    def before_query(connection, cursor, statement, parameters, context, executemany):
        context._drasil_started_at = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def after_query(connection, cursor, statement, parameters, context, executemany):
        timings = current_timings.get()
        if timings is not None:
            timings.sql_count += 1
            timings.values["sql"] = timings.values.get("sql", 0) + (perf_counter() - context._drasil_started_at) * 1000

    @event.listens_for(engine, "begin")
    def scope_transaction(connection):
        schema = connection.get_execution_options().get("drasil_schema")
        if schema is None:
            raise RuntimeError("An account namespace is required for every transaction.")
        connection.execute(text("SELECT set_config('search_path', :schema, true), "
                                "set_config('statement_timeout', '30000', true)"), {"schema": schema})

    return engine


@lru_cache(maxsize=32)
def postgres_engine(schema: str) -> Engine:
    if not re.fullmatch(r"(?:account_[a-f0-9]{32}|drasil_auth(?:_test_[a-f0-9]{32})?)", schema):
        raise RuntimeError("Invalid PostgreSQL namespace.")
    # Option engines share the same dialect and bounded pool, but keep their
    # transaction namespace on each Connection rather than the pooled socket.
    return _connection_engine(os.getenv("DATABASE_URL", "")).execution_options(drasil_schema=schema)


class RegistryRow:
    def __init__(self, row):
        self.row = row

    def __getitem__(self, key):
        return self.row[key] if isinstance(key, int) else self.row._mapping[key]


class RegistryResult:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        row = self.result.fetchone()
        return RegistryRow(row) if row is not None else None

    def fetchall(self):
        return [RegistryRow(row) for row in self.result.fetchall()]


class RegistryConnection:
    """Small adapter for the registry's existing parameterized SQL statements."""

    def __enter__(self):
        self.context = postgres_engine(registry_schema()).begin()
        self.connection = self.context.__enter__()
        return self

    def __exit__(self, *args):
        return self.context.__exit__(*args)

    def execute(self, statement: str, parameters=()):
        parts = statement.split("?")
        if len(parts) - 1 != len(parameters):
            raise ValueError("Registry parameter count mismatch.")
        query = parts[0]
        values = {}
        for index, value in enumerate(parameters):
            key = f"p{index}"
            query += f":{key}" + parts[index + 1]
            values[key] = value
        return RegistryResult(self.connection.execute(text(query), values))

    def executescript(self, script: str):
        # Only the fixed registry DDL uses this method, never user input.
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement.replace(" BLOB", " BYTEA").replace(" REAL", " DOUBLE PRECISION"))
