"""
Drasil Database Configuration
------------------------------
Uses SQLite (local-first, offline-first) via SQLModel/SQLAlchemy.
The .db file lives at the backend root (gitignored in prod, retained for local dev).

Architecture rules:
- snake_case column names in all SQLModel classes.
- Tables use plural snake_case names (e.g. strategy_nodes).
- camelCase ↔ snake_case conversion is handled by Pydantic alias_generator in each model.
"""

import atexit
import os
import sys
import tempfile
import threading
from pathlib import Path

from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine, make_url

from app.core.env import load_local_env
from app.services.account_auth import (
    AccountRecord,
    active_account,
    list_accounts,
    use_account,
)
from app.models.category import Category  # noqa: F401
from app.models.calendar_block import CalendarBlock  # noqa: F401
from app.models.calendar_integration import CalendarIntegration  # noqa: F401
from app.models.agent_goal import AgentGoalStateRecord, AgentGoalTransitionRecord  # noqa: F401
from app.models.agent_change_set import (  # noqa: F401
    AgentChangeSetCommandRecord,
    AgentChangeSetRecord,
)
from app.models.agent_simulation import (  # noqa: F401
    AgentSimulationEventRecord,
    AgentSimulationRecord,
)
from app.models.agent_execution import (  # noqa: F401
    AgentChangeSetExecutionRecord,
    AgentExternalSyncOutboxRecord,
    AgentReviewActionRecord,
)
from app.models.change_set import ChangeSetRecord  # noqa: F401
from app.models.node import StrategyNode, StrategyNodeIdSequence  # noqa: F401
from app.models.recurrence_series import (  # noqa: F401
    RecurrenceOccurrenceException,
    RecurrenceSeries,
)
from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn  # noqa: F401
from app.models.strategist_agent import StrategistAgentEvent, StrategistRun  # noqa: F401
from app.models.strategist_graph import StrategistGraphThread, StrategistProposalRecord  # noqa: F401
from app.models.strategist_session_entities import StrategistSessionEntity  # noqa: F401
from app.models.strategist_turn_mentions import StrategistTurnMention  # noqa: F401
from app.models.preference_memory import (  # noqa: F401
    PreferenceChangeLog,
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)

load_local_env()

DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[2] / "drasil.db"
DEFAULT_CHECKPOINT_DATABASE_PATH = (
    Path(__file__).resolve().parents[2] / "strategist_langgraph_checkpoints.db"
)


def _looks_like_test_process() -> bool:
    if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
        return True
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_XDIST_WORKER"):
        return True
    for arg in sys.argv:
        normalized_arg = str(arg).replace("\\", "/").casefold()
        file_name = normalized_arg.rsplit("/", 1)[-1]
        if (
            # Ad-hoc inline Python has no stable executable identity from which
            # to prove that development-account writes are intentional. Treat
            # it as isolated by default. Read-only diagnostics can still open
            # the development SQLite file explicitly in URI ``mode=ro``.
            normalized_arg in {"-", "-c"}
            or
            "pytest" in normalized_arg
            or "unittest" in normalized_arg
            or (
                normalized_arg.endswith(".py")
                and (
                    "/tests/" in f"/{normalized_arg.lstrip('/')}"
                    or file_name.startswith("test_")
                )
            )
            or ".tests.test_" in normalized_arg
        ):
            return True
    return False


def _safe_worker_id() -> str:
    raw = (
        os.getenv("DRASIL_TEST_WORKER_ID")
        or os.getenv("PYTEST_XDIST_WORKER")
        or f"pid-{os.getpid()}"
    )
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in raw)


def _remove_sqlite_files(db_path: Path) -> None:
    for path in (db_path, db_path.with_name(f"{db_path.name}-wal"), db_path.with_name(f"{db_path.name}-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _sqlite_database_path(database_url: str) -> Path | None:
    try:
        parsed_url = make_url(database_url)
    except Exception as exc:
        raise RuntimeError(
            "Tests require a valid isolated SQLite DRASIL_DATABASE_URL."
        ) from exc
    if parsed_url.get_backend_name() != "sqlite":
        raise RuntimeError(
            "Tests require an isolated SQLite database; refusing a non-SQLite "
            "DRASIL_DATABASE_URL."
        )
    if not parsed_url.database or parsed_url.database == ":memory:":
        return None
    return Path(parsed_url.database).expanduser().resolve()


def _assert_explicit_test_database_is_safe(database_url: str) -> Path | None:
    db_path = _sqlite_database_path(database_url)
    if db_path is not None and db_path == DEFAULT_DATABASE_PATH.resolve():
        raise RuntimeError(
            "Refusing to run tests against the normal Drasil development database. "
            "Use a dedicated temporary SQLite database."
        )
    return db_path


def _configure_explicit_test_checkpoint_database(db_path: Path | None) -> None:
    configured_checkpoint = os.getenv("STRATEGIST_CHECKPOINT_DB_PATH")
    if configured_checkpoint:
        checkpoint_path = Path(configured_checkpoint).expanduser().resolve()
        if checkpoint_path == DEFAULT_CHECKPOINT_DATABASE_PATH.resolve():
            raise RuntimeError(
                "Refusing to run tests against the normal Strategist checkpoint "
                "database. Use a dedicated temporary checkpoint database."
            )
        return
    if db_path is None:
        test_root = Path(
            os.getenv(
                "DRASIL_TEST_DB_ROOT",
                str(Path(tempfile.gettempdir()) / "drasil-test-dbs"),
            )
        )
        checkpoint_path = test_root / f"strategist-checkpoints-{_safe_worker_id()}.db"
    else:
        checkpoint_path = db_path.with_name(
            f"{db_path.stem}-strategist-checkpoints{db_path.suffix}"
        )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["STRATEGIST_CHECKPOINT_DB_PATH"] = checkpoint_path.as_posix()


def _configure_isolated_test_database() -> None:
    if not _looks_like_test_process():
        return
    configured_url = os.getenv("DRASIL_DATABASE_URL")
    explicit_isolation = (
        os.getenv("DRASIL_TEST_ISOLATED_DB") == "1"
        or bool(os.getenv("DRASIL_TEST_WORKER_ID"))
    )
    if configured_url and explicit_isolation:
        db_path = _assert_explicit_test_database_is_safe(configured_url)
        _configure_explicit_test_checkpoint_database(db_path)
        return
    test_root = Path(
        os.getenv(
            "DRASIL_TEST_DB_ROOT",
            str(Path(tempfile.gettempdir()) / "drasil-test-dbs"),
        )
    )
    test_root.mkdir(parents=True, exist_ok=True)
    worker_id = _safe_worker_id()
    db_path = test_root / f"drasil-{worker_id}.db"
    checkpoint_path = test_root / f"strategist-checkpoints-{worker_id}.db"
    _remove_sqlite_files(db_path)
    _remove_sqlite_files(checkpoint_path)
    os.environ["DRASIL_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["DRASIL_TEST_ISOLATED_DB"] = "1"
    os.environ["STRATEGIST_CHECKPOINT_DB_PATH"] = checkpoint_path.as_posix()
    if os.getenv("DRASIL_KEEP_TEST_DBS") != "1":
        atexit.register(_remove_sqlite_files, db_path)
        atexit.register(_remove_sqlite_files, checkpoint_path)


_configure_isolated_test_database()

# Local SQLite file. Resolve explicitly to the backend root so restarting from
# a different working directory cannot silently switch to another drasil.db.
DATABASE_PATH = Path(os.getenv("DRASIL_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser()
DATABASE_URL = os.getenv("DRASIL_DATABASE_URL", f"sqlite:///{DATABASE_PATH.resolve().as_posix()}")


def isolated_test_database_active() -> bool:
    """Return whether writes are bound to an explicit non-development test DB."""

    if not (
        os.getenv("DRASIL_TEST_ISOLATED_DB") == "1"
        or bool(os.getenv("DRASIL_TEST_WORKER_ID"))
    ):
        return False
    try:
        active_path = _sqlite_database_path(DATABASE_URL)
    except RuntimeError:
        return False
    return active_path is None or active_path != DEFAULT_DATABASE_PATH.resolve()


def assert_test_database_mutation_allowed() -> None:
    """Fail closed before a test fixture can mutate account/development data."""

    if isolated_test_database_active():
        return
    raise RuntimeError(
        "Refusing destructive test setup outside an isolated test database. "
        "Set DRASIL_TEST_ISOLATED_DB=1 with a dedicated temporary SQLite "
        "DRASIL_DATABASE_URL before importing application database state."
    )


# connect_args required for SQLite + async-style usage in FastAPI.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,  # Set to True for SQL query logging during debugging.
)

_ACCOUNT_ENGINES: dict[str, Engine] = {}
_ACCOUNT_ENGINES_LOCK = threading.Lock()


def engine_for_account(account: AccountRecord) -> Engine:
    """Return the cached engine for one isolated account database."""

    with _ACCOUNT_ENGINES_LOCK:
        cached = _ACCOUNT_ENGINES.get(account.id)
        if cached is not None:
            return cached
        account.database_path.parent.mkdir(parents=True, exist_ok=True)
        account_engine = create_engine(
            f"sqlite:///{account.database_path.as_posix()}",
            connect_args={"check_same_thread": False},
            echo=False,
        )

        @event.listens_for(account_engine, "connect")
        def configure_account_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA busy_timeout = 15000")
            finally:
                cursor.close()

        _ACCOUNT_ENGINES[account.id] = account_engine
        return account_engine


def get_active_engine() -> Engine:
    """Resolve the current account database and fail closed outside tests."""

    account = active_account(required=False)
    if account is not None:
        return engine_for_account(account)
    if os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
        return engine
    raise RuntimeError("Database access requires an active Drasil account.")


class DatabaseMigrationConflict(RuntimeError):
    """Existing local data conflicts with a required durable uniqueness boundary."""


def _assert_execution_idempotency_index_ready(connection: object) -> None:
    duplicate_groups = connection.execute(  # type: ignore[attr-defined]
        text(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM agent_change_set_executions "
            "GROUP BY session_id, idempotency_key HAVING COUNT(*) > 1"
            ")"
        )
    ).scalar_one()
    if int(duplicate_groups) > 0:
        raise DatabaseMigrationConflict(
            "agent execution idempotency migration found "
            f"{int(duplicate_groups)} conflicting identity group(s); "
            "resolve or quarantine those execution records before startup"
        )


def _ensure_revision_columns(
    target_engine: object,
    table_names: tuple[str, ...],
) -> None:
    inspector = inspect(target_engine)
    for table_name in table_names:
        table_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "revision" not in table_columns:
            with target_engine.begin() as connection:  # type: ignore[attr-defined]
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                    )
                )
        with target_engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(
                text(
                    f"UPDATE {table_name} SET revision = 1 "
                    "WHERE revision IS NULL OR revision < 1"
                )
            )


def create_db_and_tables(bind: Engine | None = None) -> None:
    """Create all tables registered on the SQLModel metadata."""
    db_engine = bind or get_active_engine()
    # Retain the established migration body while binding every operation to
    # this account-specific engine rather than the module's legacy/test engine.
    engine = db_engine
    SQLModel.metadata.create_all(db_engine)

    # Lightweight local migration support for SQLite dev databases.
    inspector = inspect(db_engine)

    strategist_session_columns = {
        column["name"]
        for column in inspector.get_columns("strategist_chat_sessions")
    }
    if "owner_principal_id" not in strategist_session_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE strategist_chat_sessions "
                    "ADD COLUMN owner_principal_id VARCHAR(128)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_strategist_chat_sessions_owner_principal_id "
                    "ON strategist_chat_sessions (owner_principal_id)"
                )
            )
    try:
        from app.services.principal_auth import require_authenticated_principal

        principal = require_authenticated_principal()
    except Exception:
        principal = None
    if principal is not None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE strategist_chat_sessions SET owner_principal_id = :principal_id "
                    "WHERE owner_principal_id IS NULL"
                ),
                {"principal_id": principal.principal_id},
            )

    outbox_columns = {
        column["name"]
        for column in inspector.get_columns("agent_external_sync_outbox")
    }
    if "effect_ordinal" not in outbox_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE agent_external_sync_outbox "
                    "ADD COLUMN effect_ordinal INTEGER NOT NULL DEFAULT 0"
                )
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_agent_external_sync_effect_ordinal "
                "ON agent_external_sync_outbox (effect_ordinal)"
            )
        )

    with engine.begin() as connection:
        _assert_execution_idempotency_index_ready(connection)
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_execution_idempotency "
                "ON agent_change_set_executions (session_id, idempotency_key)"
            )
        )

    # Native Agent Kernel optimistic concurrency. Existing local rows start at
    # revision 1; native CAS and preserved ORM writers advance these counters.
    revision_tables = (
        "categories",
        "strategy_nodes",
        "calendar_blocks",
        "recurrence_series",
        "user_preferences",
        "scheduling_constraints",
        "preference_links",
    )
    _ensure_revision_columns(engine, revision_tables)
    goal_transition_columns = {
        column["name"] for column in inspector.get_columns("agent_goal_transitions")
    }
    if "state_json" not in goal_transition_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE agent_goal_transitions ADD COLUMN state_json TEXT")
            )
            connection.execute(
                text(
                    "UPDATE agent_goal_transitions "
                    "SET state_json = ("
                    "SELECT agent_goal_states.state_json FROM agent_goal_states "
                    "WHERE agent_goal_states.goal_id = agent_goal_transitions.goal_id "
                    "AND agent_goal_states.revision = agent_goal_transitions.revision"
                    ") WHERE state_json IS NULL"
                )
            )

    simulation_columns = {
        column["name"] for column in inspector.get_columns("agent_simulations")
    }
    simulation_migrations = {
        "change_set_snapshot_json": (
            "ALTER TABLE agent_simulations ADD COLUMN "
            "change_set_snapshot_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "resulting_goal_revision": (
            "ALTER TABLE agent_simulations ADD COLUMN resulting_goal_revision INTEGER"
        ),
        "updated_at": "ALTER TABLE agent_simulations ADD COLUMN updated_at DATETIME",
    }
    for column_name, statement in simulation_migrations.items():
        if column_name not in simulation_columns:
            with engine.begin() as connection:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_simulations SET updated_at = created_at "
                "WHERE updated_at IS NULL"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_simulation_event_kind "
                "ON agent_simulation_events (simulation_id, event_kind)"
            )
        )

    existing_columns = {column["name"] for column in inspector.get_columns("strategy_nodes")}
    if "emoji" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE strategy_nodes ADD COLUMN emoji VARCHAR(16)"))
    if "notes" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE strategy_nodes ADD COLUMN notes VARCHAR(4000)"))
    if "priority" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE strategy_nodes ADD COLUMN priority VARCHAR(16) DEFAULT 'medium'")
            )
    if "deadline_at" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE strategy_nodes ADD COLUMN deadline_at VARCHAR(32)"))
    if "tags" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE strategy_nodes ADD COLUMN tags TEXT"))
    if "image_attachments" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE strategy_nodes ADD COLUMN image_attachments TEXT"))
    if "is_pinned" not in existing_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE strategy_nodes ADD COLUMN is_pinned BOOLEAN DEFAULT 0")
            )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE strategy_nodes SET priority = 'medium' WHERE priority IS NULL")
        )
        connection.execute(
            text("UPDATE strategy_nodes SET is_pinned = 0 WHERE is_pinned IS NULL")
        )

    calendar_block_columns = {
        column["name"] for column in inspector.get_columns("calendar_blocks")
    }
    calendar_block_migrations = {
        "google_event_id": "ALTER TABLE calendar_blocks ADD COLUMN google_event_id VARCHAR(256)",
        "google_event_etag": "ALTER TABLE calendar_blocks ADD COLUMN google_event_etag VARCHAR(256)",
        "google_last_synced_at": "ALTER TABLE calendar_blocks ADD COLUMN google_last_synced_at DATETIME",
        "google_sync_error": "ALTER TABLE calendar_blocks ADD COLUMN google_sync_error VARCHAR(2048)",
        "recurrence_series_id": "ALTER TABLE calendar_blocks ADD COLUMN recurrence_series_id VARCHAR(64)",
        "recurrence_key": "ALTER TABLE calendar_blocks ADD COLUMN recurrence_key VARCHAR(64)",
    }
    for column_name, statement in calendar_block_migrations.items():
        if column_name not in calendar_block_columns:
            with engine.begin() as connection:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_calendar_blocks_recurrence_identity "
                "ON calendar_blocks (recurrence_series_id, recurrence_key) "
                "WHERE recurrence_series_id IS NOT NULL AND recurrence_key IS NOT NULL"
            )
        )

    recurrence_series_columns = {
        column["name"] for column in inspector.get_columns("recurrence_series")
    }
    recurrence_series_migrations = {
        "month_days_json": (
            "ALTER TABLE recurrence_series ADD COLUMN month_days_json VARCHAR DEFAULT '[]'"
        ),
        "source_agent_change_set_id": (
            "ALTER TABLE recurrence_series ADD COLUMN source_agent_change_set_id VARCHAR(128)"
        ),
        "preference_override_authorized": (
            "ALTER TABLE recurrence_series ADD COLUMN "
            "preference_override_authorized BOOLEAN DEFAULT 0"
        ),
        "gap_minutes": (
            "ALTER TABLE recurrence_series ADD COLUMN gap_minutes INTEGER"
        ),
        "conflict_relocation": (
            "ALTER TABLE recurrence_series ADD COLUMN "
            "conflict_relocation VARCHAR(32) DEFAULT 'after_incoming'"
        ),
        "allow_priority_displacement": (
            "ALTER TABLE recurrence_series ADD COLUMN "
            "allow_priority_displacement BOOLEAN DEFAULT 0"
        ),
    }
    for column_name, statement in recurrence_series_migrations.items():
        if column_name not in recurrence_series_columns:
            with engine.begin() as connection:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recurrence_series SET preference_override_authorized = 0 "
                "WHERE preference_override_authorized IS NULL"
            )
        )
        connection.execute(
            text("UPDATE recurrence_series SET month_days_json = '[]' WHERE month_days_json IS NULL")
        )
        connection.execute(
            text(
                "UPDATE recurrence_series SET conflict_relocation = 'after_incoming' "
                "WHERE conflict_relocation IS NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE recurrence_series SET allow_priority_displacement = 0 "
                "WHERE allow_priority_displacement IS NULL"
            )
        )

    proposal_columns = {
        column["name"] for column in inspector.get_columns("strategist_proposal_records")
    }
    if "change_set_id" not in proposal_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE strategist_proposal_records "
                    "ADD COLUMN change_set_id VARCHAR(64)"
                )
            )

    change_set_command_columns = {
        column["name"] for column in inspector.get_columns("agent_change_set_commands")
    }
    change_set_command_migrations = {
        "change_set_snapshot_json": (
            "ALTER TABLE agent_change_set_commands ADD COLUMN "
            "change_set_snapshot_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "goal_snapshot_json": (
            "ALTER TABLE agent_change_set_commands ADD COLUMN "
            "goal_snapshot_json TEXT NOT NULL DEFAULT '{}'"
        ),
    }
    for column_name, statement in change_set_command_migrations.items():
        if column_name not in change_set_command_columns:
            with engine.begin() as connection:
                connection.execute(text(statement))

    run_columns = {
        column["name"] for column in inspector.get_columns("strategist_runs")
    }
    run_migrations = {
        "goal_id": "ALTER TABLE strategist_runs ADD COLUMN goal_id VARCHAR(128)",
        "goal_revision": "ALTER TABLE strategist_runs ADD COLUMN goal_revision INTEGER",
        "correlation_id": "ALTER TABLE strategist_runs ADD COLUMN correlation_id VARCHAR(64)",
        "causation_id": "ALTER TABLE strategist_runs ADD COLUMN causation_id VARCHAR(128)",
        "worker_token": "ALTER TABLE strategist_runs ADD COLUMN worker_token VARCHAR(64)",
        "lease_expires_at": "ALTER TABLE strategist_runs ADD COLUMN lease_expires_at DATETIME",
    }
    for column_name, statement in run_migrations.items():
        if column_name not in run_columns:
            with engine.begin() as connection:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE strategist_runs SET correlation_id = id "
                "WHERE correlation_id IS NULL OR correlation_id = ''"
            )
        )

    event_columns = {
        column["name"] for column in inspector.get_columns("strategist_agent_events")
    }
    event_migrations = {
        "causation_id": "ALTER TABLE strategist_agent_events ADD COLUMN causation_id VARCHAR(128)",
        "goal_id": "ALTER TABLE strategist_agent_events ADD COLUMN goal_id VARCHAR(128)",
        "goal_revision": "ALTER TABLE strategist_agent_events ADD COLUMN goal_revision INTEGER",
        "run_id": "ALTER TABLE strategist_agent_events ADD COLUMN run_id VARCHAR(64)",
    }
    for column_name, statement in event_migrations.items():
        if column_name not in event_columns:
            with engine.begin() as connection:
                connection.execute(text(statement))

    # Bootstrap a monotonic task-ID allocator before cleaning stale references,
    # then repair legacy rows left behind by pre-lifecycle hard deletes.
    from app.services.task_lifecycle import (
        bootstrap_strategy_node_id_sequence,
        reconcile_reused_task_identities,
    )

    with Session(engine) as session:
        bootstrap_strategy_node_id_sequence(session)
        reconcile_reused_task_identities(session)
        session.commit()

    # Preference values are intentionally flexible JSON. Reconcile historical
    # aliases and deactivate superseded same-scope gap rules after schema setup
    # so existing local databases follow the same contract as new writes.
    from app.services.preference_patches import reconcile_superseded_gap_preferences

    with Session(engine) as session:
        reconciliation = reconcile_superseded_gap_preferences(session)
        if any(reconciliation.values()):
            session.commit()


def get_session():
    """FastAPI dependency that yields a database session."""
    with Session(get_active_engine()) as session:
        yield session


def initialize_account_database(account: AccountRecord) -> None:
    """Create and seed one account's isolated application database."""

    account_engine = engine_for_account(account)
    with use_account(account):
        create_db_and_tables(account_engine)
        from app.db.seed import seed_default_categories

        seed_default_categories(account_engine)
    try:
        account.database_path.chmod(0o600)
    except OSError:
        pass


def initialize_existing_account_databases(recover_runs) -> None:
    """Prepare every registered account database at process startup."""

    accounts = list_accounts()
    if not accounts and os.getenv("DRASIL_TEST_ISOLATED_DB") == "1":
        create_db_and_tables(engine)
        from app.db.seed import seed_default_categories

        seed_default_categories(engine)
        recover_runs()
        return
    for account in accounts:
        initialize_account_database(account)
        with use_account(account):
            recover_runs()
