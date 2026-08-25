from __future__ import annotations

import unittest

from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.db.database import (
    DatabaseMigrationConflict,
    _assert_execution_idempotency_index_ready,
)
from app.models.agent_execution import (
    AgentChangeSetExecutionRecord,
    AgentExternalSyncOutboxRecord,
)


class AgentExecutionPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    @staticmethod
    def execution(**updates: object) -> AgentChangeSetExecutionRecord:
        values = {
            "execution_id": "execution:1",
            "session_id": "session:1",
            "command_id": "approve:1",
            "idempotency_key": "apply:1",
            "goal_id": "goal:1",
            "goal_revision": 4,
            "change_set_id": "changeset:1",
            "change_set_revision": 2,
            "simulation_id": "simulation:1",
            "payload_fingerprint": "a" * 64,
            "status": "committed_pending_verification",
            "intended_fingerprint": "b" * 64,
            "projection_json": "[]",
            "result_json": "{}",
        }
        values.update(updates)
        return AgentChangeSetExecutionRecord(**values)

    def test_session_command_is_unique_independently(self) -> None:
        with Session(self.engine) as session:
            session.add(self.execution())
            session.commit()
            session.add(
                self.execution(
                    execution_id="execution:2",
                    idempotency_key="apply:2",
                    change_set_id="changeset:2",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_changeset_revision_is_unique_independently(self) -> None:
        with Session(self.engine) as session:
            session.add(self.execution())
            session.commit()
            session.add(
                self.execution(
                    execution_id="execution:2",
                    command_id="approve:2",
                    idempotency_key="apply:2",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_outbox_semantic_key_is_unique(self) -> None:
        with Session(self.engine) as session:
            session.add(self.execution())
            session.commit()
            base = {
                "execution_id": "execution:1",
                "session_id": "session:1",
                "change_set_id": "changeset:1",
                "change_set_revision": 2,
                "provider": "google",
                "operation": "create",
                "entity_kind": "calendar_block",
                "entity_id": "block:1",
                "semantic_key": "google:create:block:1:v1",
                "payload_json": "{}",
            }
            session.add(AgentExternalSyncOutboxRecord(outbox_id="outbox:1", **base))
            session.commit()
            session.add(AgentExternalSyncOutboxRecord(outbox_id="outbox:2", **base))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_existing_duplicate_idempotency_groups_fail_with_bounded_migration_error(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE agent_change_set_executions ("
                    "execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                    "idempotency_key TEXT NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_change_set_executions VALUES "
                    "('one', 'session:1', 'apply:1'), "
                    "('two', 'session:1', 'apply:1')"
                )
            )
            with self.assertRaisesRegex(
                DatabaseMigrationConflict,
                r"1 conflicting identity group\(s\)",
            ):
                _assert_execution_idempotency_index_ready(connection)

    def test_session_idempotency_key_is_unique(self) -> None:
        with Session(self.engine) as session:
            session.add(self.execution())
            session.commit()
            session.add(
                self.execution(
                    execution_id="execution:2",
                    command_id="approve:2",
                    change_set_id="changeset:2",
                    idempotency_key="apply:1",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()


if __name__ == "__main__":
    unittest.main()
