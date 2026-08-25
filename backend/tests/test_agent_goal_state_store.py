from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.agent_goal import AgentGoalStateRecord, AgentGoalTransitionRecord
from app.models.strategist_agent import StrategistRun
from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn
from app.services.agent_kernel.contracts import GoalState
from app.services.agent_kernel.state_store import (
    ActiveGoalConflict,
    GoalStateRevisionConflict,
    GoalStateSessionMismatch,
    GoalStateStore,
)


class GoalStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.add(StrategistChatSession(id="session:2"))
            session.commit()

    def state(self, goal_id: str = "goal:1") -> GoalState:
        return GoalState(
            goal_id=goal_id,
            original_request="Schedule the interviews next week.",
            correlation_id="corr:1",
            causation_id="turn:1",
        )

    def test_snapshot_survives_a_new_database_session_and_event_volume(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            store.open("session:1", self.state(), command_id="open:1")
            session.commit()

        with Session(self.engine) as session:
            for index in range(35):
                session.add(
                    AgentGoalTransitionRecord(
                        goal_id="goal:1",
                        session_id="session:1",
                        revision=1,
                        command_id=f"noise:{index}",
                        transition_kind="context.noise",
                        payload_json="{}",
                    )
                )
            session.commit()

        with Session(self.engine) as session:
            restored = GoalStateStore(session).load_active("session:1")
            self.assertIsNotNone(restored)
            self.assertEqual(restored.goal_id, "goal:1")

    def test_stored_unknown_constraint_loads_with_visible_migration_issue(self) -> None:
        state = self.state().model_dump(mode="json", by_alias=True)
        state["constraints"] = [{
            "constraintId": "constraint:legacy",
            "kind": "moon_phase",
            "strength": "hard",
            "scope": {"kind": "goal", "operationIds": [], "entityRefs": [], "revision": 1},
            "value": {"kind": "moon_phase", "phase": "full"},
            "provenance": {"sourceKind": "user", "sourceRef": "turn:1"},
            "revision": 1,
        }]
        with Session(self.engine) as session:
            import json
            session.add(AgentGoalStateRecord(
                goal_id="goal:1", session_id="session:1", status="collecting_context",
                revision=1, active_session_id="session:1", correlation_id="corr:1",
                causation_id="turn:1", state_json=json.dumps(state),
            ))
            session.commit()
        with Session(self.engine) as session:
            restored = GoalStateStore(session).load("session:1", "goal:1")
            self.assertEqual(restored.constraints, [])
            self.assertEqual(restored.migration_issues[0].code, "unsupported_constraint")

    def test_only_one_active_goal_per_session_and_terminal_goals_are_filtered(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            first = store.open("session:1", self.state(), command_id="open:1")
            with self.assertRaises(ActiveGoalConflict):
                store.open("session:1", self.state("goal:2"), command_id="open:2")
            terminal = first.model_copy(update={"revision": 2, "status": "superseded"})
            store.save(
                "session:1",
                terminal,
                expected_revision=1,
                command_id="close:1",
                transition_kind="goal.superseded",
            )
            self.assertIsNone(store.load_active("session:1"))
            second = store.open("session:1", self.state("goal:2"), command_id="open:2")
            self.assertEqual(second.goal_id, "goal:2")

    def test_concurrent_unique_constraint_is_translated_to_active_goal_conflict(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            store.open("session:1", self.state(), command_id="open:1")
            with patch.object(store, "load_active", return_value=None), self.assertRaises(
                ActiveGoalConflict
            ):
                store.open("session:1", self.state("goal:2"), command_id="open:2")

    def test_compare_and_save_rejects_stale_and_cross_session_writes(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            current = store.open("session:1", self.state(), command_id="open:1")
            next_state = current.model_copy(update={"revision": 2, "status": "planning"})
            store.save(
                "session:1",
                next_state,
                expected_revision=1,
                command_id="transition:1",
                transition_kind="goal.planning",
            )
            with self.assertRaises(GoalStateRevisionConflict):
                store.save(
                    "session:1",
                    next_state.model_copy(update={"revision": 2}),
                    expected_revision=1,
                    command_id="transition:stale",
                    transition_kind="goal.planning",
                )
            with self.assertRaises(GoalStateSessionMismatch):
                store.load("session:2", "goal:1")

    def test_command_replay_returns_accepted_historical_state_without_duplicate_audit(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            opened = store.open("session:1", self.state(), command_id="open:1")
            planning = opened.model_copy(update={"revision": 2, "status": "planning"})
            store.save(
                "session:1",
                planning,
                expected_revision=1,
                command_id="planning:1",
                transition_kind="goal.planning",
            )
            replay = store.open("session:1", self.state(), command_id="open:1")
            self.assertEqual(replay, opened)
            self.assertEqual(replay.revision, 1)
            self.assertEqual(store.load("session:1", "goal:1").revision, 2)
            transitions = list(
                session.exec(
                    select(AgentGoalTransitionRecord).where(
                        AgentGoalTransitionRecord.command_id == "open:1"
                    )
                ).all()
            )
            self.assertEqual(len(transitions), 1)

    def test_concurrent_resume_cannot_regress_a_newer_run_link(self) -> None:
        now = datetime(2026, 7, 19, 12, 0)
        with Session(self.engine) as session:
            session.add(
                StrategistChatTurn(
                    id="turn:1", session_id="session:1", prompt="Continue."
                )
            )
            for run_id, created_at in (
                ("run:old", now),
                ("run:middle", now + timedelta(seconds=1)),
                ("run:new", now + timedelta(seconds=2)),
            ):
                session.add(
                    StrategistRun(
                        id=run_id,
                        session_id="session:1",
                        turn_id="turn:1",
                        prompt="Continue.",
                        request_json="{}",
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )
            store = GoalStateStore(session)
            store.open(
                "session:1",
                self.state(),
                command_id="open:1",
                current_run_id="run:old",
            )
            session.commit()

        with Session(self.engine) as stale_session:
            stale_store = GoalStateStore(stale_session)
            stale_store.load("session:1", "goal:1")
            stale_session.get(AgentGoalStateRecord, "goal:1")
            with Session(self.engine) as winner_session:
                GoalStateStore(winner_session).resume(
                    "session:1",
                    "goal:1",
                    current_run_id="run:new",
                    command_id="resume:new",
                )
                winner_session.commit()
            with self.assertRaises(GoalStateRevisionConflict):
                stale_store.resume(
                    "session:1",
                    "goal:1",
                    current_run_id="run:middle",
                    command_id="resume:middle",
                )

        with Session(self.engine) as session:
            record = session.get(AgentGoalStateRecord, "goal:1")
            self.assertEqual(record.current_run_id, "run:new")

    def test_command_reuse_with_different_intent_fails_closed(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            store.open("session:1", self.state(), command_id="open:1")
            with self.assertRaises(GoalStateRevisionConflict):
                store.open(
                    "session:1",
                    self.state().model_copy(update={"original_request": "Different request."}),
                    command_id="open:1",
                )

    def test_resume_is_audited_and_session_cleanup_removes_goal_history(self) -> None:
        with Session(self.engine) as session:
            store = GoalStateStore(session)
            store.open("session:1", self.state(), command_id="open:1")
            store.resume(
                "session:1",
                "goal:1",
                current_run_id="run:1",
                command_id="resume:1",
            )
            transition = session.exec(
                select(AgentGoalTransitionRecord).where(
                    AgentGoalTransitionRecord.command_id == "resume:1"
                )
            ).one()
            self.assertEqual(transition.transition_kind, "goal.resumed")
            store.delete_session_history("session:1")
            self.assertIsNone(session.get(AgentGoalStateRecord, "goal:1"))
            self.assertFalse(
                list(
                    session.exec(
                        select(AgentGoalTransitionRecord).where(
                            AgentGoalTransitionRecord.session_id == "session:1"
                        )
                    ).all()
                )
            )


if __name__ == "__main__":
    unittest.main()
