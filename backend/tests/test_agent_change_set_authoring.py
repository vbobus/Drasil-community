from __future__ import annotations

import unittest
from datetime import datetime

from sqlmodel import Session, SQLModel, create_engine

from app.models.agent_change_set import AgentChangeSetRecord
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.change_set_authoring import (
    ChangeSetAuthorizationError,
    ChangeSetConservationError,
    NativeChangeSetAuthoringCoordinator,
)
from app.services.agent_kernel.contracts import (
    DraftChangeSetCommand,
    ChangeSet,
    EntityAbsentAssertion,
    EntityExistsAssertion,
    EntityFieldEqualsAssertion,
    EntityRef,
    GoalState,
    Provenance,
    Preconditions,
    RequestedOperationClause,
    SemanticSlot,
    SemanticValue,
    TaskCreateOperation,
)
from app.services.agent_kernel.state_store import GoalStateStore
from app.services.agent_kernel.runtime import _apply_change_set_tool_draft


PROVENANCE = Provenance(source_kind="user", source_ref="turn:1")


class NativeChangeSetAuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:1"))
            session.add(Category(id=1, name="Work"))
            session.add(StrategyNode(id=1, category_id=1, title="Existing task"))
            session.commit()
            expected = TaskCreateOperation(
                operation_id="op:create",
                title="Launch",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                temp_ref="temp:task",
                fulfills_clause_ids=["clause:create"],
            )
            clause = RequestedOperationClause(
                clause_id="clause:create",
                operation_kind="task.create",
                cardinality="exactly_one",
                expected_operations=[expected],
                provenance=PROVENANCE,
            )
            GoalStateStore(session).open(
                "session:1",
                GoalState(
                    goal_id="goal:1",
                    original_request="Create a launch task.",
                    semantic_slots=[
                        SemanticSlot(
                            path="operations.requested",
                            semantic_value=SemanticValue(
                                state="present",
                                value=[clause.model_dump(mode="json", by_alias=True)],
                                provenance=PROVENANCE,
                            ),
                        )
                    ],
                    correlation_id="corr:1",
                    causation_id="turn:1",
                ),
                command_id="goal:open",
            )
            session.commit()

    @staticmethod
    def command(*, title: str = "Launch", workspace: str = "workspace:1") -> DraftChangeSetCommand:
        return DraftChangeSetCommand(
            disposition="create",
            expected_goal_revision=1,
            operations=[
                TaskCreateOperation(
                    operation_id="op:create",
                    title=title,
                    workspace_ref=EntityRef(kind="workspace", entity_id=workspace),
                    temp_ref="temp:task",
                    fulfills_clause_ids=["clause:create"],
                )
            ],
        )

    def test_create_assigns_server_identity_conserves_and_round_trips(self) -> None:
        with Session(self.engine) as session:
            coordinator = NativeChangeSetAuthoringCoordinator(session)
            result = coordinator.draft(
                "session:1",
                self.command(),
                command_id="tool-call:1",
                correlation_id="corr:run",
                causation_id="tool-call:1",
            )
            session.commit()
            self.assertTrue(result.persisted)
            self.assertTrue(result.conservation.complete)
            self.assertTrue(result.change_set.change_set_id.startswith("changeset:"))
            self.assertEqual(result.change_set.idempotency_key, "tool-call:1")
            self.assertEqual(result.goal_revision, 2)
            self.assertEqual(
                coordinator.store.load("session:1", result.change_set.change_set_id),
                result.change_set,
            )

    def test_authorization_and_conservation_fail_before_persistence(self) -> None:
        with Session(self.engine) as session:
            coordinator = NativeChangeSetAuthoringCoordinator(session)
            with self.assertRaises(ChangeSetAuthorizationError):
                coordinator.draft(
                    "session:1",
                    self.command(workspace="workspace:999"),
                    command_id="tool-call:unauthorized",
                    correlation_id="corr:run",
                    causation_id="tool-call:unauthorized",
                )
            self.assertIsNone(session.get(AgentChangeSetRecord, "changeset:missing"))

            missing_clause = self.command().model_copy(
                update={
                    "operations": [
                        self.command().operations[0].model_copy(
                            update={"fulfills_clause_ids": []}
                        )
                    ]
                }
            )
            with self.assertRaises(ChangeSetConservationError):
                coordinator.draft(
                    "session:1",
                    missing_clause,
                    command_id="tool-call:incomplete",
                    correlation_id="corr:run",
                    causation_id="tool-call:incomplete",
                )

    def test_revise_preserves_identity_and_uses_both_cas_revisions(self) -> None:
        with Session(self.engine) as session:
            coordinator = NativeChangeSetAuthoringCoordinator(session)
            first = coordinator.draft(
                "session:1",
                self.command(),
                command_id="tool-call:1",
                correlation_id="corr:run",
                causation_id="tool-call:1",
            )
            revise = DraftChangeSetCommand(
                disposition="revise",
                expected_goal_revision=first.goal_revision,
                expected_change_set_revision=first.change_set.revision,
                operations=self.command().operations,
            )
            second = coordinator.draft(
                "session:1",
                revise,
                command_id="tool-call:2",
                correlation_id="corr:run",
                causation_id="tool-call:2",
            )
            self.assertEqual(second.change_set.change_set_id, first.change_set.change_set_id)
            self.assertEqual(second.change_set.revision, 2)
            self.assertEqual(second.goal_revision, 3)

    def test_runtime_callback_binds_command_identity_and_refreshes_compact_memory(self) -> None:
        with Session(self.engine) as session:
            memory: dict = {}
            result = _apply_change_set_tool_draft(
                coordinator=NativeChangeSetAuthoringCoordinator(session),
                session_memory=memory,
                session_id="session:1",
                source_turn_id="turn:runtime",
                correlation_id="corr:runtime",
                current_run_id=None,
                arguments=self.command().model_dump(mode="json", by_alias=True),
            )
            self.assertTrue(result["persisted"])
            self.assertEqual(memory["current_change_set"]["operationIds"], ["op:create"])
            self.assertTrue(memory["current_change_set"]["conservationComplete"])
            self.assertNotIn("operations", memory["current_change_set"])
            self.assertEqual(memory["goal_state"]["currentChangeSetRevision"], 1)

    def test_preconditions_evaluate_exists_absent_and_field_values(self) -> None:
        operation = self.command().operations[0]
        valid = ChangeSet(
            change_set_id="changeset:preconditions",
            goal_id="goal:1",
            operations=[operation],
            preconditions=Preconditions(assertions=[
                EntityExistsAssertion(
                    assertion_id="assert:exists",
                    entity_ref=EntityRef(kind="task", entity_id="task:1"),
                ),
                EntityAbsentAssertion(
                    assertion_id="assert:absent",
                    entity_ref=EntityRef(kind="task", entity_id="task:999"),
                ),
                EntityFieldEqualsAssertion(
                    assertion_id="assert:title",
                    entity_ref=EntityRef(kind="task", entity_id="task:1"),
                    field="title",
                    expected_value="Existing task",
                ),
            ]),
            idempotency_key="command:preconditions",
            correlation_id="corr:1",
            causation_id="turn:1",
        )
        with Session(self.engine) as session:
            coordinator = NativeChangeSetAuthoringCoordinator(session)
            coordinator.authorizer.authorize(valid)
            invalid = valid.model_copy(update={
                "preconditions": Preconditions(assertions=[
                    EntityAbsentAssertion(
                        assertion_id="assert:wrongly-absent",
                        entity_ref=EntityRef(kind="task", entity_id="task:1"),
                    )
                ])
            })
            with self.assertRaises(ChangeSetAuthorizationError):
                coordinator.authorizer.authorize(invalid)

    def test_occurrence_identity_requires_series_and_a_recurring_block(self) -> None:
        with Session(self.engine) as session:
            session.add(CalendarBlock(
                id=10, task_id=1, category_id=1,
                start_at=datetime.fromisoformat("2026-07-20T09:00:00"),
                end_at=datetime.fromisoformat("2026-07-20T10:00:00"),
            ))
            session.add(CalendarBlock(
                id=11, task_id=1, category_id=1,
                start_at=datetime.fromisoformat("2026-07-21T09:00:00"),
                end_at=datetime.fromisoformat("2026-07-21T10:00:00"),
                is_recurring=True, recurrence_series_id="series-a", recurrence_key="occ-1",
            ))
            session.commit()
            authorizer = NativeChangeSetAuthoringCoordinator(session).authorizer
            self.assertIsNone(authorizer._resolve(
                EntityRef(kind="recurrence_occurrence", entity_id="recurrence_occurrence:10")
            ))
            resolved = authorizer._resolve(EntityRef(
                kind="recurrence_occurrence",
                entity_id="recurrence_occurrence:series-a:occ-1",
            ))
            self.assertEqual(resolved.id, 11)


if __name__ == "__main__":
    unittest.main()
