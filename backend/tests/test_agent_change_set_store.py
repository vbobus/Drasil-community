from __future__ import annotations

import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.agent_change_set import AgentChangeSetCommandRecord, AgentChangeSetRecord
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.change_set_store import (
    ChangeSetCommandConflict,
    ChangeSetRevisionConflict,
    ChangeSetSessionMismatch,
    NativeChangeSetStore,
)
from app.services.agent_kernel.contracts import ChangeSet, EntityRef, GoalState, TaskCreateOperation
from app.services.agent_kernel.state_store import GoalStateStore


class NativeChangeSetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.add(StrategistChatSession(id="session:2"))
            session.commit()
            GoalStateStore(session).open(
                "session:1",
                GoalState(
                    goal_id="goal:1",
                    original_request="Create the task.",
                    correlation_id="corr:1",
                    causation_id="turn:1",
                ),
                command_id="goal:open",
            )
            session.commit()

    @staticmethod
    def draft(*, revision: int = 1, title: str = "Task") -> ChangeSet:
        return ChangeSet(
            revision=revision,
            change_set_id="changeset:1",
            goal_id="goal:1",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create",
                    title=title,
                    workspace_ref=EntityRef(kind="workspace", entity_id="workspace:work"),
                    temp_ref="temp:task",
                )
            ],
            idempotency_key="tool-call:1",
            correlation_id="corr:1",
            causation_id="tool-call:1",
        )

    def test_create_persists_canonical_snapshot_and_updates_goal_pointer(self) -> None:
        with Session(self.engine) as session:
            saved, goal = NativeChangeSetStore(session).create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload={"disposition": "create", "operations": ["op:create"]},
            )
            session.commit()
            self.assertEqual(saved.revision, 1)
            self.assertEqual(goal.current_change_set_id, "changeset:1")
            self.assertEqual(goal.current_change_set_revision, 1)
            self.assertEqual(goal.revision, 2)

        with Session(self.engine) as session:
            restored = NativeChangeSetStore(session).load("session:1", "changeset:1")
            self.assertEqual(restored, saved)
            self.assertEqual(
                len(list(session.exec(select(AgentChangeSetCommandRecord)).all())), 1
            )

    def test_identical_replay_is_safe_but_command_reuse_with_new_payload_fails(self) -> None:
        payload = {"disposition": "create", "operations": ["op:create"]}
        with Session(self.engine) as session:
            store = NativeChangeSetStore(session)
            first, first_goal = store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload=payload,
            )
            replay, replay_goal = store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload=payload,
            )
            self.assertEqual(replay, first)
            self.assertEqual(replay_goal, first_goal)
            with self.assertRaises(ChangeSetCommandConflict):
                store.create(
                    "session:1",
                    self.draft(title="Different"),
                    expected_goal_revision=1,
                    command_id="tool-call:1",
                    command_payload={"disposition": "create", "operations": ["changed"]},
                )

    def test_revision_uses_cas_preserves_identity_and_invalidates_approval(self) -> None:
        with Session(self.engine) as session:
            store = NativeChangeSetStore(session)
            _, goal = store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload={"disposition": "create"},
            )
            revised, revised_goal = store.revise(
                "session:1",
                self.draft(revision=2, title="Revised"),
                expected_change_set_revision=1,
                expected_goal_revision=goal.revision,
                command_id="tool-call:2",
                command_payload={"disposition": "revise"},
            )
            self.assertEqual(revised.revision, 2)
            self.assertEqual(revised_goal.current_change_set_revision, 2)
            self.assertIsNone(revised_goal.approved_change_set_revision)
            replayed_create, replayed_goal = store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload={"disposition": "create"},
            )
            self.assertEqual(replayed_create.revision, 1)
            self.assertEqual(replayed_goal.revision, 2)
            with self.assertRaises(ChangeSetRevisionConflict):
                store.revise(
                    "session:1",
                    self.draft(revision=2),
                    expected_change_set_revision=1,
                    expected_goal_revision=revised_goal.revision,
                    command_id="tool-call:stale",
                    command_payload={"disposition": "revise"},
                )

    def test_revision_cannot_rebind_a_retained_temp_producer(self) -> None:
        with Session(self.engine) as session:
            store = NativeChangeSetStore(session)
            _, goal = store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload={"disposition": "create"},
            )
            rebound = self.draft(revision=2).model_copy(
                update={
                    "operations": [
                        self.draft().operations[0].model_copy(
                            update={
                                "temp_ref": EntityRef(
                                    kind="temporary",
                                    entity_id="temp:other",
                                    target_kind="task",
                                )
                            }
                        )
                    ]
                }
            )
            with self.assertRaises(ChangeSetRevisionConflict):
                store.revise(
                    "session:1",
                    rebound,
                    expected_change_set_revision=1,
                    expected_goal_revision=goal.revision,
                    command_id="tool-call:2",
                    command_payload={"disposition": "revise"},
                )

    def test_revision_cannot_reassign_a_retained_atomic_group(self) -> None:
        with Session(self.engine) as session:
            store = NativeChangeSetStore(session)
            _, goal = store.create(
                "session:1", self.draft(), expected_goal_revision=1,
                command_id="tool-call:1", command_payload={"disposition": "create"},
            )
            changed = self.draft(revision=2).model_copy(
                update={"operations": [self.draft().operations[0].model_copy(
                    update={"atomic_group_id": "group:new"}
                )]}
            )
            with self.assertRaises(ChangeSetRevisionConflict):
                store.revise(
                    "session:1", changed, expected_change_set_revision=1,
                    expected_goal_revision=goal.revision, command_id="tool-call:2",
                    command_payload={"disposition": "revise"},
                )

    def test_cross_session_access_and_cleanup_fail_closed(self) -> None:
        with Session(self.engine) as session:
            store = NativeChangeSetStore(session)
            store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload={"disposition": "create"},
            )
            with self.assertRaises(ChangeSetSessionMismatch):
                store.load("session:2", "changeset:1")
            store.delete_session_history("session:1")
            self.assertIsNone(session.get(AgentChangeSetRecord, "changeset:1"))

    def test_revision_cannot_overwrite_an_applied_changeset(self) -> None:
        with Session(self.engine) as session:
            store = NativeChangeSetStore(session)
            saved, goal = store.create(
                "session:1",
                self.draft(),
                expected_goal_revision=1,
                command_id="tool-call:1",
                command_payload={"disposition": "create"},
            )
            applied = ChangeSet.model_validate(
                {
                    **saved.model_dump(mode="python"),
                    "status": "applied",
                    "simulation_result_ref": "simulation:1",
                    "approved_revision": saved.revision,
                }
            )
            record = session.get(AgentChangeSetRecord, saved.change_set_id)
            record.status = "applied"
            record.snapshot_json = applied.model_dump_json()
            session.add(record)
            session.flush()
            with self.assertRaises(ChangeSetRevisionConflict):
                store.revise(
                    "session:1",
                    self.draft(revision=2),
                    expected_change_set_revision=1,
                    expected_goal_revision=goal.revision,
                    command_id="tool-call:2",
                    command_payload={"disposition": "revise"},
                )


if __name__ == "__main__":
    unittest.main()
