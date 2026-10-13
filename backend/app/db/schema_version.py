"""Version markers avoid repeating DDL on every serverless cold start.

Bump the corresponding version when adding a migration. Unknown/new accounts
still use the existing atomic migration under the database advisory lock.
"""
from sqlalchemy import text

ACCOUNT_SCHEMA_VERSION = 2
REGISTRY_SCHEMA_VERSION = 1


def current_schema_version(connection, version):
    if connection.execute(text("SELECT to_regclass('drasil_schema_version')")).scalar() is None:
        return False
    current = connection.execute(text("SELECT version FROM drasil_schema_version WHERE id = 1")).scalar()
    return current is not None and current >= version


def record_schema_version(connection, version):
    connection.execute(text("CREATE TABLE IF NOT EXISTS drasil_schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"))
    connection.execute(text("INSERT INTO drasil_schema_version (id, version) VALUES (1, :version) "
        "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version"), {"version": version})
