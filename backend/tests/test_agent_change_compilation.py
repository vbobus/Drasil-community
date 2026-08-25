from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.agent_change_set import AgentChangeSetRecord
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import UserPreference
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.change_compilation import (
    ChangeCompilationRequest,
    CompatibilityProposalAdapter,
    PlannerChangeArtifact,
    PlannerOperationBinding,
    PlannerScheduleArtifactAdapter,
    PlannerScheduleEvidence,
    SemanticPlanArtifact,
    ServerChangeCompilationCoordinator,
)
from app.services.agent_kernel.change_set_store import ActiveChangeSetConflict
from app.services.agent_kernel.contracts import (
    CalendarCreateBlockOperation,
    EntityRef,
    GoalState,
    Provenance,
    PreferenceScalarValue,
    PreferenceRetireOperation,
    PreferenceSupersedeOperation,
    PreferenceUpsertOperation,
    RequestedOperationClause,
    SemanticSlot,
    SemanticValue,
    SimulateChangeSetCommand,
    TaskCreateOperation,
    TaskFields,
)
from app.services.agent_kernel.state_store import GoalStateStore
from app.services.agent_kernel.operation_conservation import operation_fingerprint
from app.services.agent_kernel.subagent_delegation import (
    PlannerSubagentTask,
    PlannerSubagentResult,
    SubagentBudget,
    SubagentParentContext,
    SubagentUsage,
)


PROVENANCE = Provenance(
    source_kind="user",
    source_ref="turn:compound",
    source_text="Create a visa task and schedule the appointment.",
)


def _temporary(entity_id: str, target_kind: str, title: str | None = None) -> EntityRef:
    return EntityRef(
        kind="temporary",
        entity_id=entity_id,
        target_kind=target_kind,
        title=title,
    )


def _planner_task(
    *,
    goal_revision: int,
    task_id: str = "planner-task:owned",
    parent_run_id: str = "run:1",
) -> PlannerSubagentTask:
    now = datetime(2026, 7, 23, 12, 0)
    return PlannerSubagentTask(
        task_id=task_id,
        cancellation_id=f"cancel:{task_id}",
        content_fingerprint="b" * 64,
        parent=SubagentParentContext(
            parent_agent_id="strategist",
            parent_run_id=parent_run_id,
            session_id="session:compile",
            goal_id="goal:compile",
            goal_revision=goal_revision,
            correlation_id="corr:compile",
        ),
        assigned_agent_id="planner",
        objective="Schedule the exact assigned task.",
        task_ids=[10],
        allowed_tool_names=["evaluate_schedule_plan"],
        budget=SubagentBudget(
            timeout_seconds=30,
            max_exchanges=2,
            max_tool_calls=4,
            max_tokens=4_000,
        ),
        created_at=now,
        deadline_at=now + timedelta(seconds=30),
    )


def _planner_artifact(
    task: PlannerSubagentTask,
    *,
    goal_revision: int,
    operations: list,
    unscheduled_task_refs: list[EntityRef] | None = None,
) -> PlannerChangeArtifact:
    operations = [
        (
            operation.model_copy(update={"provenance": PROVENANCE})
            if operation.provenance is None
            else operation
        )
        for operation in operations
    ]
    schedule_evidence = [
        PlannerScheduleEvidence(
            operation_id=operation.operation_id,
            operation_sha256=operation_fingerprint(operation).sha256,
            validation_sha256="c" * 64,
            source_ref=f"planner-subagent:{task.task_id}",
        )
        for operation in operations
        if operation.kind
        in {
            "calendar.create_block",
            "calendar.move_block",
            "calendar.resize_block",
            "calendar.split_block",
        }
    ]
    bindings = [
        PlannerOperationBinding(
            operation_id=operation.operation_id,
            task_ref=(
                operation.task_ref
                if operation.kind == "calendar.create_block"
                else EntityRef(kind="task", entity_id="task:10")
            ),
        )
        for operation in operations
    ]
    return PlannerChangeArtifact(
        subagent_task_id=task.task_id,
        parent_run_id=task.parent.parent_run_id,
        goal_id=task.parent.goal_id,
        goal_revision=goal_revision,
        source_goal_revision=task.parent.goal_revision,
        task_content_fingerprint=task.content_fingerprint,
        operations=operations,
        unscheduled_task_refs=unscheduled_task_refs or [],
        schedule_evidence=schedule_evidence,
        operation_bindings=bindings,
    )


class AgentChangeCompilationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(StrategistChatSession(id="session:compile"))
            session.add(Category(id=1, name="Work"))
            session.add(
                StrategyNode(
                    id=10,
                    category_id=1,
                    title="ICBC appointment",
                    time_estimate_minutes=60,
                )
            )
            session.commit()

    def test_semantic_plan_rejects_duplicate_combined_identities(self) -> None:
        clauses, parent, _child = self._compound_clauses()
        with self.assertRaisesRegex(ValueError, "clause IDs must be unique"):
            SemanticPlanArtifact(clauses=[clauses[0], clauses[0]])
        assert parent.temp_ref is not None
        with self.assertRaisesRegex(
            ValueError,
            "unscheduled task references must be unique",
        ):
            SemanticPlanArtifact(
                clauses=[clauses[0]],
                unscheduled_task_refs=[parent.temp_ref, parent.temp_ref],
            )

    def _open_goal(
        self,
        session: Session,
        clauses: list[RequestedOperationClause],
        *,
        goal_id: str = "goal:compile",
        recurrence_rule: dict[str, object] | None = None,
    ) -> GoalState:
        semantic_slots = [
            SemanticSlot(
                path="operations.requested",
                semantic_value=SemanticValue(
                    state="present",
                    value=[
                        item.model_dump(mode="json", by_alias=True)
                        for item in clauses
                    ],
                    provenance=PROVENANCE,
                ),
            )
        ]
        if recurrence_rule is not None:
            semantic_slots.append(
                SemanticSlot(
                    path="recurrence.rule",
                    semantic_value=SemanticValue(
                        state="present",
                        value=recurrence_rule,
                        provenance=PROVENANCE,
                    ),
                )
            )
        return GoalStateStore(session).open(
            "session:compile",
            GoalState(
                goal_id=goal_id,
                original_request="Create the work and schedule the resolved appointment.",
                semantic_slots=semantic_slots,
                correlation_id="corr:compile",
                causation_id="turn:compound",
            ),
            command_id="goal:open",
        )

    @staticmethod
    def _compound_clauses() -> tuple[
        list[RequestedOperationClause],
        TaskCreateOperation,
        TaskCreateOperation,
    ]:
        parent_ref = _temporary("temp:visa", "task", "Apply for B-1 visa")
        child_ref = _temporary("temp:circle-back", "task", "Confirm appointment time")
        parent = TaskCreateOperation(
            operation_id="op:create-visa",
            title="Apply for B-1 visa",
            workspace_ref=EntityRef(
                kind="workspace",
                entity_id="workspace:1",
                title="Work",
            ),
            temp_ref=parent_ref,
            fields=TaskFields(
                notes="Track the application requirements.",
                priority="high",
                estimate_minutes=120,
                deadline_at=datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc),
            ),
            fulfills_clause_ids=["clause:create-visa"],
            provenance=PROVENANCE,
        )
        child = TaskCreateOperation(
            operation_id="op:create-circle-back",
            depends_on=["op:create-visa"],
            title="Confirm appointment time",
            workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
            parent_ref=parent_ref,
            temp_ref=child_ref,
            fields=TaskFields(estimate_minutes=15),
            fulfills_clause_ids=["clause:create-circle-back"],
            provenance=PROVENANCE,
        )
        clauses = [
            RequestedOperationClause(
                clause_id="clause:create-visa",
                operation_kind="task.create",
                cardinality="exactly_one",
                target_refs=[parent_ref],
                expected_operations=[parent],
                provenance=PROVENANCE,
            ),
            RequestedOperationClause(
                clause_id="clause:create-circle-back",
                operation_kind="task.create",
                cardinality="exactly_one",
                target_refs=[child_ref],
                expected_operations=[child],
                provenance=PROVENANCE,
            ),
            RequestedOperationClause(
                clause_id="clause:schedule-appointment",
                operation_kind="calendar.create_block",
                operation_source="planner",
                cardinality="exactly_one",
                target_refs=[
                    EntityRef(
                        kind="task",
                        entity_id="task:10",
                        title="ICBC appointment",
                    )
                ],
                provenance=PROVENANCE,
            ),
        ]
        return clauses, parent, child

    def test_compiles_strategist_tree_and_owned_planner_schedule_into_one_change_set(
        self,
    ) -> None:
        clauses, parent, child = self._compound_clauses()
        with Session(self.engine) as session:
            goal = self._open_goal(session, clauses)
            session.commit()
            calendar = CalendarCreateBlockOperation(
                operation_id="op:planner-icbc",
                task_ref=EntityRef(
                    kind="task",
                    entity_id="task:10",
                    title="ICBC appointment",
                ),
                start_at=datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc),
                fulfills_clause_ids=["clause:schedule-appointment"],
            )
            planner_task = _planner_task(goal_revision=goal.revision)
            artifact = _planner_artifact(
                planner_task,
                goal_revision=goal.revision,
                operations=[calendar],
                unscheduled_task_refs=[child.temp_ref],
            )

            outcome = ServerChangeCompilationCoordinator(session).compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                planner_artifact=artifact,
                command_id="compile:compound",
                correlation_id="corr:run",
                causation_id="turn:compound",
                expected_planner_task=planner_task,
            )

            self.assertEqual(outcome.status, "succeeded", outcome.issues)
            self.assertIsNotNone(outcome.draft)
            assert outcome.draft is not None
            change_set = outcome.draft.change_set
            self.assertEqual(
                [item.operation_id for item in change_set.operations],
                [
                    parent.operation_id,
                    child.operation_id,
                    calendar.operation_id,
                ],
            )
            self.assertEqual(child.parent_ref, parent.temp_ref)
            self.assertEqual(child.fields.estimate_minutes, 15)
            self.assertEqual(parent.fields.priority, "high")
            self.assertEqual(
                outcome.manifest.unscheduled_task_refs,
                [parent.temp_ref, child.temp_ref],
            )
            self.assertTrue(outcome.draft.conservation.complete)

    def test_independent_roots_are_not_attached_to_prior_or_sibling_tasks(self) -> None:
        first_ref = _temporary("temp:visa", "task")
        second_ref = _temporary("temp:docker", "task")
        operations = [
            TaskCreateOperation(
                operation_id="op:visa",
                title="Apply for B-1 visa",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                temp_ref=first_ref,
                fulfills_clause_ids=["clause:visa"],
            ),
            TaskCreateOperation(
                operation_id="op:docker",
                title="Learn Docker",
                workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                temp_ref=second_ref,
                fulfills_clause_ids=["clause:docker"],
            ),
        ]
        clauses = [
            RequestedOperationClause(
                clause_id=f"clause:{name}",
                operation_kind="task.create",
                cardinality="exactly_one",
                target_refs=[ref],
                expected_operations=[operation],
                provenance=PROVENANCE,
            )
            for name, ref, operation in (
                ("visa", first_ref, operations[0]),
                ("docker", second_ref, operations[1]),
            )
        ]
        with Session(self.engine) as session:
            goal = self._open_goal(session, clauses)
            session.commit()
            outcome = ServerChangeCompilationCoordinator(session).compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                command_id="compile:roots",
                correlation_id="corr:run",
                causation_id="turn:compound",
            )
            assert outcome.draft is not None
            self.assertTrue(
                all(item.parent_ref is None for item in outcome.draft.change_set.operations)
            )

    def test_missing_or_stale_planner_artifact_returns_repair_without_persistence(
        self,
    ) -> None:
        clauses, _parent, _child = self._compound_clauses()
        with Session(self.engine) as session:
            goal = self._open_goal(session, clauses)
            session.commit()
            coordinator = ServerChangeCompilationCoordinator(session)
            missing = coordinator.compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                command_id="compile:missing",
                correlation_id="corr:run",
                causation_id="turn:compound",
            )
            self.assertEqual(missing.status, "needs_model_repair")
            self.assertIn("planner_artifact_missing", [item.code for item in missing.issues])
            self.assertEqual(
                len(session.exec(select(AgentChangeSetRecord)).all()),
                0,
            )

            planner_task = _planner_task(goal_revision=goal.revision)
            stale = coordinator.compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                planner_artifact=_planner_artifact(
                    planner_task,
                    goal_revision=goal.revision + 1,
                    operations=[],
                ),
                expected_planner_task=planner_task,
                command_id="compile:stale",
                correlation_id="corr:run",
                causation_id="turn:compound",
            )
            self.assertEqual(stale.status, "stale_evidence")
            self.assertEqual(
                len(session.exec(select(AgentChangeSetRecord)).all()),
                0,
            )
            exact_artifact = _planner_artifact(
                planner_task,
                goal_revision=goal.revision,
                operations=[],
            )
            wrong_task = _planner_task(
                goal_revision=goal.revision,
                task_id="planner-task:other",
            )
            wrong_owner = coordinator.compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                planner_artifact=exact_artifact,
                expected_planner_task=wrong_task,
                command_id="compile:wrong-owner",
                correlation_id="corr:run",
                causation_id="turn:compound",
            )
            self.assertEqual(wrong_owner.status, "stale_evidence")
            self.assertIn(
                "planner_task_binding_stale",
                [item.code for item in wrong_owner.issues],
            )

    def test_conflicting_operation_identity_fails_closed(self) -> None:
        clauses, parent, _child = self._compound_clauses()
        with Session(self.engine) as session:
            goal = self._open_goal(session, clauses)
            session.commit()
            contradictory = CalendarCreateBlockOperation(
                operation_id=parent.operation_id,
                task_ref=EntityRef(kind="task", entity_id="task:10"),
                start_at=datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc),
                fulfills_clause_ids=["clause:schedule-appointment"],
            )
            planner_task = _planner_task(
                goal_revision=goal.revision,
                task_id="planner-task:bad",
            )
            artifact = _planner_artifact(
                planner_task,
                goal_revision=goal.revision,
                operations=[contradictory],
            )
            outcome = ServerChangeCompilationCoordinator(session).compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                planner_artifact=artifact,
                command_id="compile:collision",
                correlation_id="corr:run",
                causation_id="turn:compound",
                expected_planner_task=planner_task,
            )
            self.assertEqual(outcome.status, "needs_model_repair")
            self.assertIn("operation_identity_conflict", [item.code for item in outcome.issues])
            self.assertEqual(
                len(session.exec(select(AgentChangeSetRecord)).all()),
                0,
            )

    def test_preference_operation_is_conserved_by_the_same_compiler(self) -> None:
        preference = PreferenceUpsertOperation(
            operation_id="op:preference-focus",
            preference_key="focus_block_minutes",
            value=PreferenceScalarValue(value=50),
            fulfills_clause_ids=["clause:preference-focus"],
            provenance=PROVENANCE,
        )
        clause = RequestedOperationClause(
            clause_id="clause:preference-focus",
            operation_kind="preference.upsert",
            cardinality="exactly_one",
            expected_operations=[preference],
            provenance=PROVENANCE,
        )
        with Session(self.engine) as session:
            goal = self._open_goal(session, [clause])
            session.commit()

            outcome = ServerChangeCompilationCoordinator(session).compile(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                command_id="compile:preference",
                correlation_id="corr:preference",
                causation_id="turn:preference",
            )

            self.assertEqual(outcome.status, "succeeded", outcome.issues)
            assert outcome.draft is not None
            self.assertEqual(outcome.draft.change_set.operations, [preference])
            self.assertTrue(outcome.draft.conservation.complete)

    def test_schedule_patch_adapter_requires_owned_completed_validated_output(self) -> None:
        schedule_clause = RequestedOperationClause(
            clause_id="clause:schedule",
            operation_kind="calendar.create_block",
            operation_source="planner",
            cardinality="exactly_one",
            target_refs=[EntityRef(kind="task", entity_id="task:10")],
            provenance=PROVENANCE,
        )
        planner_task = _planner_task(goal_revision=1)
        result = PlannerSubagentResult(
            task_id=planner_task.task_id,
            content_fingerprint=planner_task.content_fingerprint,
            parent_run_id=planner_task.parent.parent_run_id,
            goal_id=planner_task.parent.goal_id,
            goal_revision=planner_task.parent.goal_revision,
            status="completed",
            artifact={
                "planner_result": {
                    "proposal": {
                        "schedulePatchValidation": {
                            "status": "valid",
                            "errors": [],
                        },
                        "schedulePatch": {
                            "operations": [
                                {
                                    "op": "create_block",
                                    "proposalBlockId": "proposal:10",
                                    "taskId": 10,
                                    "startAt": "2026-07-23T13:30:00",
                                    "endAt": "2026-07-23T14:30:00",
                                }
                            ]
                        },
                    }
                }
            },
            usage=SubagentUsage(exchanges=1),
        )
        artifact = PlannerScheduleArtifactAdapter().adapt(
            result,
            task=planner_task,
            target_goal_revision=1,
            planner_clauses=[schedule_clause],
            timezone_name="America/Vancouver",
            request_constraints=[
                {
                    "kind": "exact_start",
                    "hard": True,
                    "task_ids": [10],
                    "start_at": "2026-07-23T13:30:00",
                }
            ],
        )
        self.assertEqual(len(artifact.operations), 1)
        operation = artifact.operations[0]
        self.assertEqual(operation.fulfills_clause_ids, ["clause:schedule"])
        self.assertEqual(operation.task_ref.entity_id, "task:10")
        self.assertIsNotNone(operation.start_at.tzinfo)
        self.assertEqual(len(artifact.constraints), 1)
        exact = artifact.constraints[0]
        self.assertEqual(exact.kind, "exact_time")
        self.assertEqual(exact.scope.operation_ids, [operation.operation_id])
        self.assertEqual(exact.value.start_at, operation.start_at)
        self.assertEqual(exact.value.duration_minutes, 60)

        invalid = result.model_copy(update={"status": "failed"})
        with self.assertRaisesRegex(ValueError, "completed"):
            PlannerScheduleArtifactAdapter().adapt(
                invalid,
                task=planner_task,
                target_goal_revision=1,
                planner_clauses=[schedule_clause],
                timezone_name="America/Vancouver",
            )

    def test_planner_delete_conserves_task_ownership_and_exact_block_target(self) -> None:
        with Session(self.engine) as session:
            block = CalendarBlock(
                task_id=10,
                category_id=1,
                start_at=datetime(2026, 7, 23, 13, 30),
                end_at=datetime(2026, 7, 23, 14, 30),
            )
            session.add(block)
            session.flush()
            assert block.id is not None
            planner_task = _planner_task(goal_revision=1)
            adapter = PlannerScheduleArtifactAdapter()
            delegated = adapter.delegated_clauses(
                planner_task,
                operation_mode="remove",
                provenance=PROVENANCE,
            )
            goal = self._open_goal(session, delegated)
            session.commit()
            self.assertEqual(goal.revision, planner_task.parent.goal_revision)

            result = PlannerSubagentResult(
                task_id=planner_task.task_id,
                content_fingerprint=planner_task.content_fingerprint,
                parent_run_id=planner_task.parent.parent_run_id,
                goal_id=planner_task.parent.goal_id,
                goal_revision=planner_task.parent.goal_revision,
                status="completed",
                artifact={
                    "planner_result": {
                        "proposal": {
                            "schedulePatchValidation": {
                                "status": "valid",
                                "errors": [],
                            },
                            "schedulePatch": {
                                "operations": [
                                    {
                                        "op": "remove_block",
                                        "acceptedBlockId": int(block.id),
                                        "taskId": 10,
                                    }
                                ]
                            },
                        }
                    }
                },
            )
            artifact = adapter.adapt(
                result,
                task=planner_task,
                target_goal_revision=goal.revision + 1,
                planner_clauses=delegated,
                timezone_name="America/Vancouver",
            )
            finalized = adapter.finalize_clauses(delegated, artifact)

            self.assertEqual(
                finalized[0].target_refs,
                [
                    EntityRef(
                        kind="calendar_block",
                        entity_id=f"calendar_block:{block.id}",
                    )
                ],
            )
            self.assertEqual(
                artifact.operation_bindings[0].task_ref,
                EntityRef(kind="task", entity_id="task:10"),
            )
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                SemanticPlanArtifact(clauses=finalized),
                command_id="compile:planner-delete",
                correlation_id="corr:planner-delete",
                causation_id="turn:planner-delete",
                planner_artifact=artifact,
                expected_planner_task=planner_task,
            )

            self.assertEqual(outcome.status, "succeeded", outcome.issues)

    def test_planner_adapter_rejects_wrong_owned_task_and_expanded_scope(self) -> None:
        planner_task = _planner_task(goal_revision=1)
        result = PlannerSubagentResult(
            task_id=planner_task.task_id,
            content_fingerprint=planner_task.content_fingerprint,
            parent_run_id=planner_task.parent.parent_run_id,
            goal_id=planner_task.parent.goal_id,
            goal_revision=planner_task.parent.goal_revision,
            status="completed",
            artifact={
                "planner_result": {
                    "proposal": {
                        "schedulePatchValidation": {
                            "status": "valid",
                            "errors": [],
                        },
                        "schedulePatch": {
                            "operations": [
                                {
                                    "op": "create_block",
                                    "taskId": 11,
                                    "startAt": "2026-07-23T13:30:00",
                                    "endAt": "2026-07-23T14:30:00",
                                }
                            ]
                        },
                    }
                }
            },
        )
        clauses = PlannerScheduleArtifactAdapter().delegated_clauses(
            planner_task,
            operation_mode="create",
            provenance=PROVENANCE,
        )
        with self.assertRaisesRegex(ValueError, "unassigned task"):
            PlannerScheduleArtifactAdapter().adapt(
                result,
                task=planner_task,
                target_goal_revision=2,
                planner_clauses=clauses,
                timezone_name="America/Vancouver",
            )
        wrong_parent = result.model_copy(update={"parent_run_id": "run:other"})
        with self.assertRaisesRegex(ValueError, "exact assigned task"):
            PlannerScheduleArtifactAdapter().adapt(
                wrong_parent,
                task=planner_task,
                target_goal_revision=2,
                planner_clauses=clauses,
                timezone_name="America/Vancouver",
            )

    def test_compiler_rejects_calendar_create_task_that_disagrees_with_binding(self) -> None:
        with Session(self.engine) as session:
            initial_task = _planner_task(goal_revision=1)
            delegated = PlannerScheduleArtifactAdapter().delegated_clauses(
                initial_task,
                operation_mode="create",
                provenance=PROVENANCE,
            )
            goal = self._open_goal(session, delegated)
            planner_task = _planner_task(goal_revision=goal.revision)
            operation = CalendarCreateBlockOperation(
                operation_id="op:cross-task",
                task_ref=EntityRef(kind="task", entity_id="task:11"),
                start_at=datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc),
                fulfills_clause_ids=[delegated[0].clause_id],
                provenance=PROVENANCE,
            )
            artifact = _planner_artifact(
                planner_task,
                goal_revision=goal.revision + 1,
                operations=[operation],
            ).model_copy(
                update={
                    "operation_bindings": [
                        PlannerOperationBinding(
                            operation_id=operation.operation_id,
                            task_ref=EntityRef(kind="task", entity_id="task:10"),
                        )
                    ]
                }
            )

            outcome = ServerChangeCompilationCoordinator(session).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan=SemanticPlanArtifact(clauses=delegated),
                planner_artifact=artifact,
                expected_planner_task=planner_task,
                command_id="compile:cross-task",
                correlation_id="corr:cross-task",
                causation_id="turn:cross-task",
            )

            self.assertEqual(outcome.status, "authorization_denied")
            self.assertEqual(outcome.issues[0].code, "planner_task_scope_mismatch")

    def test_compiler_rejects_duplicate_planner_operation_bindings(self) -> None:
        with Session(self.engine) as session:
            initial_task = _planner_task(goal_revision=1)
            delegated = PlannerScheduleArtifactAdapter().delegated_clauses(
                initial_task,
                operation_mode="create",
                provenance=PROVENANCE,
            )
            goal = self._open_goal(session, delegated)
            planner_task = _planner_task(goal_revision=goal.revision)
            operation = CalendarCreateBlockOperation(
                operation_id="op:duplicate-binding",
                task_ref=EntityRef(kind="task", entity_id="task:10"),
                start_at=datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc),
                fulfills_clause_ids=[delegated[0].clause_id],
                provenance=PROVENANCE,
            )
            artifact = _planner_artifact(
                planner_task,
                goal_revision=goal.revision + 1,
                operations=[operation],
            )
            duplicate = PlannerOperationBinding(
                operation_id=operation.operation_id,
                task_ref=EntityRef(kind="task", entity_id="task:10"),
            )
            artifact = artifact.model_copy(
                update={"operation_bindings": [duplicate, duplicate]}
            )

            outcome = ServerChangeCompilationCoordinator(session).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan=SemanticPlanArtifact(clauses=delegated),
                planner_artifact=artifact,
                expected_planner_task=planner_task,
                command_id="compile:duplicate-binding",
                correlation_id="corr:duplicate-binding",
                causation_id="turn:duplicate-binding",
            )

            self.assertEqual(outcome.status, "authorization_denied")
            self.assertEqual(
                outcome.issues[0].code,
                "planner_operation_binding_duplicate",
            )

    def test_all_unscheduled_planner_result_composes_with_strategist_work(self) -> None:
        clauses, parent, _child = self._compound_clauses()
        strategist_clause = clauses[0]
        with Session(self.engine) as session:
            goal = self._open_goal(session, [strategist_clause])
            session.commit()
            planner_task = _planner_task(goal_revision=goal.revision)
            result = PlannerSubagentResult(
                task_id=planner_task.task_id,
                content_fingerprint=planner_task.content_fingerprint,
                parent_run_id=planner_task.parent.parent_run_id,
                goal_id=planner_task.parent.goal_id,
                goal_revision=planner_task.parent.goal_revision,
                status="completed",
                artifact={
                    "planner_result": {
                        "proposal": {
                            "schedulePatchValidation": {
                                "status": "valid",
                                "errors": [],
                            },
                            "schedulePatch": {
                                "operations": [
                                    {"op": "skip_task", "taskId": 10}
                                ]
                            },
                        }
                    }
                },
            )
            adapter = PlannerScheduleArtifactAdapter()
            delegated = adapter.delegated_clauses(
                planner_task,
                operation_mode="create",
                provenance=PROVENANCE,
            )
            artifact = adapter.adapt(
                result,
                task=planner_task,
                target_goal_revision=goal.revision + 1,
                planner_clauses=delegated,
                timezone_name="America/Vancouver",
            )
            finalized = adapter.finalize_clauses(delegated, artifact)
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan=SemanticPlanArtifact(
                    clauses=[strategist_clause, *finalized],
                ),
                planner_artifact=artifact,
                expected_planner_task=planner_task,
                command_id="compile:all-unscheduled",
                correlation_id="corr:all-unscheduled",
                causation_id="turn:all-unscheduled",
            )
            self.assertEqual(outcome.status, "succeeded", outcome.issues)
            assert outcome.draft is not None
            self.assertEqual(
                [operation.operation_id for operation in outcome.draft.change_set.operations],
                [parent.operation_id],
            )
            self.assertEqual(
                outcome.manifest.unscheduled_task_refs,
                [parent.temp_ref, EntityRef(kind="task", entity_id="task:10")],
            )

    def test_failed_semantic_compilation_rolls_back_goal_revision_and_plan(self) -> None:
        clauses, _parent, _child = self._compound_clauses()
        original_clause = clauses[0]
        with Session(self.engine) as session:
            goal = self._open_goal(session, [original_clause])
            session.commit()
            planner_task = _planner_task(goal_revision=goal.revision)
            delegated = PlannerScheduleArtifactAdapter().delegated_clauses(
                planner_task,
                operation_mode="create",
                provenance=PROVENANCE,
            )
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan=SemanticPlanArtifact(clauses=delegated),
                command_id="compile:rollback",
                correlation_id="corr:rollback",
                causation_id="turn:rollback",
            )
            self.assertEqual(outcome.status, "needs_model_repair")
            reloaded = GoalStateStore(session).load_active("session:compile")
            assert reloaded is not None
            self.assertEqual(reloaded.revision, goal.revision)
            self.assertEqual(
                [item.clause_id for item in reloaded.requested_operation_clauses()],
                [original_clause.clause_id],
            )

    def test_compatibility_adapter_rejects_unknown_parent_temp_id(self) -> None:
        proposal = {
            "proposal_id": "proposal:dangling-parent",
            "structural_operations": [
                {
                    "kind": "create_node",
                    "temp_id": "child",
                    "parent_temp_id": "missing",
                    "title": "Child",
                    "category_id": 1,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "unknown parent_temp_id"):
            CompatibilityProposalAdapter().adapt_task_proposal(
                proposal_kind="branch",
                proposal=proposal,
                provenance=PROVENANCE,
                timezone_name="UTC",
            )

    def test_compatibility_adapter_enforces_parent_owned_workspace_before_simulation(
        self,
    ) -> None:
        proposal = {
            "proposal_id": "proposal:parent-owned-workspace",
            "structural_operations": [
                {
                    "kind": "create_node",
                    "temp_id": "child",
                    "parent_temp_id": "parent",
                    "title": "Cook dinner",
                    "category_id": 999,
                },
                {
                    "kind": "create_node",
                    "temp_id": "parent",
                    "title": "Birthday commitment",
                    "category_id": 1,
                },
                {
                    "kind": "create_node",
                    "temp_id": "grandchild",
                    "parent_temp_id": "child",
                    "title": "Prepare ingredients",
                    "category_id": 998,
                },
            ],
        }
        plan = CompatibilityProposalAdapter().adapt_task_proposal(
            proposal_kind="branch",
            proposal=proposal,
            provenance=PROVENANCE,
            timezone_name="UTC",
        )
        creates = {
            operation.title: operation
            for clause in plan.clauses
            for operation in clause.expected_operations
            if operation.kind == "task.create"
        }
        self.assertEqual(
            {operation.workspace_ref.entity_id for operation in creates.values()},
            {"workspace:1"},
        )

        with Session(self.engine) as session:
            goal = self._open_goal(session, plan.clauses)
            session.commit()
            coordinator = ServerChangeCompilationCoordinator(session)
            compilation = coordinator.compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan,
                command_id="compile:parent-owned-workspace",
                correlation_id="corr:parent-owned-workspace",
                causation_id="turn:parent-owned-workspace",
            )
            self.assertEqual(compilation.status, "succeeded", compilation.issues)
            assert compilation.draft is not None
            simulation = coordinator.simulation.simulate(
                "session:compile",
                SimulateChangeSetCommand(
                    expected_goal_revision=compilation.draft.goal_revision,
                    expected_change_set_revision=compilation.draft.change_set.revision,
                ),
                command_id="simulate:parent-owned-workspace",
                correlation_id="corr:parent-owned-workspace",
                causation_id="turn:parent-owned-workspace",
            )
            self.assertEqual(simulation.status, "ready_for_review", simulation.issues)

    def test_compatibility_adapter_uses_existing_parent_workspace(self) -> None:
        with Session(self.engine) as session:
            plan = CompatibilityProposalAdapter().adapt_task_proposal(
                proposal_kind="branch",
                proposal={
                    "proposal_id": "proposal:existing-parent-workspace",
                    "structural_operations": [
                        {
                            "kind": "create_node",
                            "temp_id": "child",
                            "parent_id": 10,
                            "title": "Prepare documents",
                            "category_id": 999,
                        }
                    ],
                },
                provenance=PROVENANCE,
                timezone_name="UTC",
                session=session,
            )
        operation = plan.clauses[0].expected_operations[0]
        self.assertEqual(operation.workspace_ref.entity_id, "workspace:1")

    def test_compatibility_adapter_compiles_backlog_placement(self) -> None:
        plan = CompatibilityProposalAdapter().adapt_task_proposal(
            proposal_kind="branch",
            proposal={
                "proposal_id": "proposal:backlog-task",
                "structural_operations": [
                    {
                        "kind": "create_node",
                        "temp_id": "later",
                        "title": "Review later",
                        "category_id": 1,
                        "is_backlog": True,
                        "tags": ["planning", "later"],
                    }
                ],
            },
            provenance=PROVENANCE,
            timezone_name="UTC",
        )

        operation = plan.clauses[0].expected_operations[0]
        self.assertEqual(operation.kind, "task.create")
        self.assertTrue(operation.fields.is_backlog)
        self.assertEqual(operation.fields.tags, ["planning", "later"])

    def test_compatibility_adapter_uses_typed_schedule_instead_of_source_text(
        self,
    ) -> None:
        proposal = {
            "proposal_id": "proposal:weekly-team-meeting",
            "structural_operations": [
                {
                    "kind": "create_node",
                    "temp_id": "team-meeting",
                    "title": "Team meeting",
                    "category_id": 1,
                    "time_estimate_minutes": 60,
                }
            ],
            "created_task_schedule_plan": {
                "status": "valid",
                "blocks": [
                    {
                        "target_temp_id": "team-meeting",
                        "start_at": "2026-07-31T18:00:00",
                        "end_at": "2026-07-31T19:00:00",
                    }
                ],
                "displacement_operations": [],
            },
        }
        provenance = Provenance(
            source_kind="user",
            source_ref="turn:weekly-team-meeting",
            source_text=(
                "create a task for a team meeting on friday 6pm every week"
            ),
        )

        one_time_plan = CompatibilityProposalAdapter().adapt_task_proposal(
            proposal_kind="branch",
            proposal=proposal,
            provenance=provenance,
            timezone_name="America/Vancouver",
        )
        self.assertIn(
            "calendar.create_block",
            [
                operation.kind
                for clause in one_time_plan.clauses
                for operation in clause.expected_operations
            ],
        )

        recurring_proposal = {
            key: value
            for key, value in proposal.items()
            if key != "created_task_schedule_plan"
        }
        recurring_proposal["recurring_schedule"] = {
            "target_temp_id": "team-meeting",
            "frequency": "weekly",
            "interval": 1,
            "days_of_week": ["friday"],
            "start_date": "2026-07-31",
            "end_mode": "never",
            "duration_minutes": 60,
            "preferred_start_time": "18:00",
            "allowed_start_time": "18:00",
            "allowed_end_time": "19:00",
            "timezone": "America/Vancouver",
        }
        plan = CompatibilityProposalAdapter().adapt_task_proposal(
            proposal_kind="branch",
            proposal=recurring_proposal,
            provenance=provenance,
            timezone_name="America/Vancouver",
        )
        self.assertIn(
            "recurrence.create_series",
            [
                operation.kind
                for clause in plan.clauses
                for operation in clause.expected_operations
            ],
        )

    def test_compiler_conserves_model_declared_recurrence_structurally(
        self,
    ) -> None:
        proposal = {
            "proposal_id": "proposal:model-declared-recurrence",
            "structural_operations": [
                {
                    "kind": "create_node",
                    "temp_id": "team-meeting",
                    "title": "Team meeting",
                    "category_id": 1,
                    "time_estimate_minutes": 60,
                }
            ],
            "created_task_schedule_plan": {
                "status": "valid",
                "blocks": [
                    {
                        "target_temp_id": "team-meeting",
                        "start_at": "2026-07-31T18:00:00",
                        "end_at": "2026-07-31T19:00:00",
                    }
                ],
                "displacement_operations": [],
            },
        }
        plan = CompatibilityProposalAdapter().adapt_task_proposal(
            proposal_kind="branch",
            proposal=proposal,
            provenance=PROVENANCE,
            timezone_name="America/Vancouver",
        )
        with Session(self.engine) as session:
            goal = self._open_goal(
                session,
                plan.clauses,
                recurrence_rule={
                    "frequency": "weekly",
                    "days_of_week": ["friday"],
                    "preferred_start_time": "18:00",
                },
            )
            session.commit()
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(
                    expected_goal_revision=goal.revision,
                ),
                plan,
                command_id="compile:model-declared-recurrence",
                correlation_id="corr:model-declared-recurrence",
                causation_id="turn:model-declared-recurrence",
            )

        self.assertEqual(outcome.status, "needs_model_repair")
        self.assertEqual(
            outcome.issues[0].code,
            "declared_recurrence_missing_from_plan",
        )

    def test_recurring_conflict_moves_compile_atomically_with_the_series(self) -> None:
        with Session(self.engine) as session:
            block = CalendarBlock(
                task_id=10,
                category_id=1,
                start_at=datetime(2026, 7, 20, 17, 0),
                end_at=datetime(2026, 7, 20, 19, 0),
                is_recurring=True,
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            proposal = {
                "proposal_id": "proposal:recurrence-with-move",
                "structural_operations": [
                    {
                        "kind": "create_node",
                        "temp_id": "team-call",
                        "title": "Team call",
                        "category_id": 1,
                        "time_estimate_minutes": 60,
                    }
                ],
                "recurring_schedule": {
                    "target_temp_id": "team-call",
                    "frequency": "weekly",
                    "days_of_week": ["monday"],
                    "start_date": "2026-07-20",
                    "end_mode": "never",
                    "duration_minutes": 60,
                    "preferred_start_time": "18:30",
                    "allowed_start_time": "18:30",
                    "allowed_end_time": "19:30",
                    "timezone": "America/Vancouver",
                },
                "recurring_schedule_plan": {
                    "status": "valid",
                    "displacement_operations": [
                        {
                            "op": "update_block_time",
                            "acceptedBlockId": int(block.id),
                            "taskId": 10,
                            "startAt": "2026-07-20T19:45:00",
                            "endAt": "2026-07-20T21:45:00",
                            "expectedOriginalStartAt": "2026-07-20T17:00:00",
                            "expectedOriginalEndAt": "2026-07-20T19:00:00",
                        }
                    ],
                },
            }
            plan = CompatibilityProposalAdapter().adapt_task_proposal(
                proposal_kind="branch",
                proposal=proposal,
                provenance=PROVENANCE,
                timezone_name="America/Vancouver",
                session=session,
            )
            operation_kinds = [
                operation.kind
                for clause in plan.clauses
                for operation in clause.expected_operations
            ]
            self.assertEqual(
                operation_kinds,
                ["task.create", "recurrence.create_series", "calendar.move_block"],
            )
            move = plan.clauses[-1].expected_operations[0]
            self.assertEqual(move.block_ref.revision, block.revision)

            goal = self._open_goal(
                session,
                plan.clauses,
                recurrence_rule={
                    "frequency": "weekly",
                    "days_of_week": ["monday"],
                    "preferred_start_time": "18:30",
                },
            )
            session.commit()
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan,
                command_id="compile:recurrence-with-move",
                correlation_id="corr:recurrence-with-move",
                causation_id="turn:recurrence-with-move",
            )

        self.assertEqual(outcome.status, "succeeded", outcome.issues)
        assert outcome.draft is not None
        self.assertEqual(len(outcome.draft.change_set.operations), 3)

    def test_change_set_conflicts_return_typed_retryable_outcome(self) -> None:
        clauses, _parent, _child = self._compound_clauses()
        with Session(self.engine) as session:
            goal = self._open_goal(session, [clauses[0]])
            session.commit()
            coordinator = ServerChangeCompilationCoordinator(session)
            with patch.object(
                coordinator.authoring,
                "draft",
                side_effect=ActiveChangeSetConflict("concurrent create"),
            ):
                outcome = coordinator.compile(
                    "session:compile",
                    ChangeCompilationRequest(expected_goal_revision=goal.revision),
                    command_id="compile:conflict",
                    correlation_id="corr:conflict",
                    causation_id="turn:conflict",
                )
            self.assertEqual(outcome.status, "stale_evidence")
            self.assertTrue(outcome.issues[0].retryable)
            self.assertEqual(
                outcome.issues[0].code,
                "change_set_revision_conflict",
            )

    def test_compile_simulate_and_review_share_one_exact_identity(self) -> None:
        task_ref = EntityRef(
            kind="task",
            entity_id="task:10",
            title="ICBC appointment",
        )
        clause = RequestedOperationClause(
            clause_id="clause:schedule",
            operation_kind="calendar.create_block",
            operation_source="planner",
            cardinality="exactly_one",
            target_refs=[task_ref],
            provenance=PROVENANCE,
        )
        with Session(self.engine) as session:
            goal = self._open_goal(session, [clause])
            session.commit()
            planner_task = _planner_task(
                goal_revision=goal.revision,
                task_id="planner-task:review",
                parent_run_id="run:review",
            )
            artifact = _planner_artifact(
                planner_task,
                goal_revision=goal.revision,
                operations=[
                    CalendarCreateBlockOperation(
                        operation_id="op:schedule-review",
                        task_ref=task_ref,
                        start_at=datetime(
                            2026, 7, 24, 13, 30, tzinfo=timezone.utc
                        ),
                        end_at=datetime(
                            2026, 7, 24, 14, 30, tzinfo=timezone.utc
                        ),
                        fulfills_clause_ids=["clause:schedule"],
                    )
                ],
            )
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_and_review(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                planner_artifact=artifact,
                expected_planner_task=planner_task,
                command_id="review:compound",
                correlation_id="corr:review",
                causation_id="turn:compound",
            )

            self.assertEqual(outcome.status, "succeeded", outcome.issues)
            self.assertIsNotNone(outcome.review_document)
            self.assertIsNotNone(outcome.simulation)
            self.assertIsNotNone(outcome.compilation.draft)
            assert outcome.review_document is not None
            assert outcome.simulation is not None
            assert outcome.compilation.draft is not None
            review = outcome.review_document
            self.assertEqual(
                review.change_set_id,
                outcome.compilation.draft.change_set.change_set_id,
            )
            self.assertEqual(review.simulation_id, outcome.simulation.simulation_id)
            approvals = [
                item for item in review.controls if item.action == "approve"
            ]
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0].review_id, review.review_id)
            self.assertEqual(
                approvals[0].binding.change_set_revision,
                review.change_set_revision,
            )

    def test_compatibility_compound_proposal_becomes_one_semantic_plan(self) -> None:
        proposal = {
            "proposal_id": "proposal:appointments",
            "structural_operations": [
                {
                    "kind": "create_node",
                    "temp_id": "icbc",
                    "title": "ICBC appointment",
                    "category_id": 1,
                    "time_estimate_minutes": 60,
                },
                {
                    "kind": "create_node",
                    "temp_id": "investor",
                    "title": "Meet investor",
                    "category_id": 1,
                    "time_estimate_minutes": 60,
                },
                {
                    "kind": "create_node",
                    "temp_id": "circle_back",
                    "parent_temp_id": "investor",
                    "title": "Confirm investor meeting time",
                    "category_id": 1,
                    "deadline_at": "2026-07-23T20:00:00-07:00",
                    "notes": "Ask the investor for the final time.",
                    "time_estimate_minutes": 15,
                },
            ],
            "created_task_schedule_plan": {
                "status": "valid",
                "blocks": [
                    {
                        "target_temp_id": "icbc",
                        "start_at": "2026-07-23T13:30:00",
                        "end_at": "2026-07-23T14:30:00",
                    }
                ],
                "displacement_operations": [],
            },
        }
        plan = CompatibilityProposalAdapter().adapt_task_proposal(
            proposal_kind="branch",
            proposal=proposal,
            provenance=PROVENANCE,
            timezone_name="America/Vancouver",
        )
        operations = [
            operation
            for clause in plan.clauses
            for operation in clause.expected_operations
        ]
        creates = [
            operation for operation in operations if operation.kind == "task.create"
        ]
        calendar = [
            operation
            for operation in operations
            if operation.kind == "calendar.create_block"
        ]
        by_title = {operation.title: operation for operation in creates}
        self.assertEqual(len(creates), 3)
        self.assertEqual(len(calendar), 1)
        self.assertEqual(
            by_title["Confirm investor meeting time"].parent_ref,
            by_title["Meet investor"].temp_ref,
        )
        self.assertEqual(
            by_title["Confirm investor meeting time"].fields.notes,
            "Ask the investor for the final time.",
        )
        self.assertEqual(
            calendar[0].task_ref,
            by_title["ICBC appointment"].temp_ref,
        )
        self.assertEqual(
            plan.unscheduled_task_refs,
            [
                by_title["Meet investor"].temp_ref,
                by_title["Confirm investor meeting time"].temp_ref,
            ],
        )

        mismatched_schedule = {
            **proposal,
            "created_task_schedule_plan": {
                **proposal["created_task_schedule_plan"],
                "blocks": [
                    {
                        "target_temp_id": "icbc",
                        "start_at": "2026-07-23T13:30:00",
                        "end_at": "2026-07-23T14:00:00",
                    }
                ],
            },
        }
        with self.assertRaisesRegex(
            ValueError,
            "scheduled duration must match its task estimate",
        ):
            CompatibilityProposalAdapter().adapt_task_proposal(
                proposal_kind="branch",
                proposal=mismatched_schedule,
                provenance=PROVENANCE,
                timezone_name="America/Vancouver",
            )

        with Session(self.engine) as session:
            goal = self._open_goal(
                session,
                [
                    RequestedOperationClause(
                        clause_id="clause:placeholder",
                        operation_kind="task.create",
                        cardinality="exactly_one",
                        target_refs=[
                            _temporary("temp:placeholder", "task")
                        ],
                        expected_operations=[
                            TaskCreateOperation(
                                operation_id="op:placeholder",
                                title="Placeholder",
                                workspace_ref=EntityRef(
                                    kind="workspace",
                                    entity_id="workspace:1",
                                ),
                                temp_ref="temp:placeholder",
                                fulfills_clause_ids=["clause:placeholder"],
                            )
                        ],
                        provenance=PROVENANCE,
                    )
                ],
            )
            session.commit()
            outcome = ServerChangeCompilationCoordinator(
                session
            ).compile_semantic_plan(
                "session:compile",
                ChangeCompilationRequest(expected_goal_revision=goal.revision),
                plan,
                command_id="compile:compatibility",
                correlation_id="corr:compatibility",
                causation_id="turn:compound",
            )
            self.assertEqual(outcome.status, "succeeded")
            assert outcome.draft is not None
            self.assertEqual(len(outcome.draft.change_set.operations), 4)
            self.assertTrue(outcome.draft.conservation.complete)

    def test_validated_preference_meaning_compiles_to_typed_operations(self) -> None:
        proposal = {
            "preference_patch": {
                "operations": [
                    {
                        "op": "create_preference",
                        "preference": {
                            "preferenceType": "focus_window",
                            "domain": "scheduling",
                            "strength": "hard",
                            "scopeType": "global",
                            "value": {
                                "startTime": "09:00",
                                "endTime": "11:00",
                                "timezone": "America/Vancouver",
                            },
                        },
                    },
                    {
                        "op": "create_constraint",
                        "constraint": {
                            "constraintType": "inter_task_gap",
                            "targetType": "global",
                            "hard": True,
                            "value": {"interTaskGapMinutes": 15},
                        },
                    },
                ]
            }
        }
        plan = CompatibilityProposalAdapter().adapt_preference_proposal(
            proposal=proposal,
            provenance=PROVENANCE,
            timezone_name="America/Vancouver",
        )
        self.assertEqual(len(plan.clauses), 2)
        operations = [
            operation
            for clause in plan.clauses
            for operation in clause.expected_operations
        ]
        self.assertTrue(
            all(isinstance(item, PreferenceUpsertOperation) for item in operations)
        )
        self.assertEqual(
            [item.preference_key for item in operations],
            ["focus_window", "constraint.inter_task_gap"],
        )
        self.assertEqual(operations[0].semantics.strength, "hard")
        self.assertEqual(operations[0].value.kind, "time_window")
        self.assertEqual(operations[1].semantics.domain, "scheduling")

    def test_gap_update_consolidates_enforcement_and_explicitly_replaces_older_rules(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            older = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="global",
                description="Leave 15 minutes between tasks.",
                value_json='{"interTaskGapMinutes":15}',
            )
            duplicate = UserPreference(
                domain="scheduling",
                preference_type="task_buffer",
                scope_type="global",
                description="Keep a 15 minute buffer.",
                value_json='{"bufferMinutes":15}',
            )
            complementary = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="global",
                description="Keep a 5 minute gap between shorter tasks.",
                value_json=(
                    '{"interTaskGapMinutes":5,'
                    '"maximumTaskDurationMinutes":44}'
                ),
            )
            scoped_older = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="category",
                scope_id="home",
                description="Leave 15 minutes between tasks in Home.",
                value_json='{"interTaskGapMinutes":15}',
            )
            session.add(older)
            session.add(duplicate)
            session.add(complementary)
            session.add(scoped_older)
            session.commit()
            session.refresh(older)
            session.refresh(duplicate)

            plan = CompatibilityProposalAdapter().adapt_preference_proposal(
                proposal={
                    "preference_patch": {
                        "operations": [
                            {
                                "op": "create_preference",
                                "tempId": "preference-1",
                                "preference": {
                                    "preferenceType": "inter_task_gap",
                                    "domain": "scheduling",
                                    "scopeType": "global",
                                    "strength": "medium",
                                    "value": {
                                        "interTaskGapMinutes": 15,
                                        "minimumTaskDurationMinutes": 45,
                                        "shorterTaskGapMinutes": 0,
                                    },
                                },
                            },
                            {
                                "op": "create_constraint",
                                "sourcePreferenceTempId": "preference-1",
                                "constraint": {
                                    "constraintType": "inter_task_gap",
                                    "targetType": "global",
                                    "hard": True,
                                    "value": {"interTaskGapMinutes": 15},
                                },
                            },
                        ]
                    }
                },
                provenance=PROVENANCE,
                timezone_name="America/Vancouver",
                session=session,
            )

        operations = [
            operation
            for clause in plan.clauses
            for operation in clause.expected_operations
        ]
        self.assertEqual(len(operations), 3)
        self.assertIsInstance(operations[0], PreferenceSupersedeOperation)
        self.assertIsInstance(operations[1], PreferenceRetireOperation)
        self.assertIsInstance(operations[2], PreferenceRetireOperation)
        self.assertEqual(operations[0].semantics.strength, "hard")
        self.assertEqual(
            {operation.atomic_group_id for operation in operations},
            {operations[0].atomic_group_id},
        )
        self.assertTrue(operations[0].atomic_group_id)
        self.assertEqual(
            [
                operation.reason_code
                for operation in operations[1:]
                if isinstance(operation, PreferenceRetireOperation)
            ],
            ["redundant_scoped_copy", "redundant_scoped_copy"],
        )
        self.assertNotIn(
            "constraint.inter_task_gap",
            [
                operation.preference_key
                for operation in operations
                if isinstance(operation, PreferenceUpsertOperation)
            ],
        )
        referenced_preference_ids = {
            operation.preference_ref.entity_id
            for operation in operations
            if isinstance(
                operation,
                (PreferenceSupersedeOperation, PreferenceRetireOperation),
            )
        }
        self.assertNotIn(
            f"preference:{complementary.id}",
            referenced_preference_ids,
        )
        self.assertIn(
            f"preference:{scoped_older.id}",
            referenced_preference_ids,
        )

    def test_preference_replacement_never_targets_another_users_memory(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            foreign = UserPreference(
                user_id="another-user",
                domain="scheduling",
                preference_type="availability",
                scope_type="global",
                strength="hard",
                description="Do not schedule before 10 AM.",
                value_json=(
                    '{"startTime":"00:00","endTime":"10:00",'
                    '"rule":"do_not_schedule_before"}'
                ),
            )
            session.add(foreign)
            session.commit()
            session.refresh(foreign)

            plan = CompatibilityProposalAdapter().adapt_preference_proposal(
                proposal={
                    "preference_patch": {
                        "operations": [
                            {
                                "op": "create_preference",
                                "preference": {
                                    "preferenceType": "availability",
                                    "domain": "scheduling",
                                    "scopeType": "global",
                                    "strength": "hard",
                                    "value": {
                                        "startTime": "00:00",
                                        "endTime": "11:00",
                                        "rule": "do_not_schedule_before",
                                    },
                                },
                            }
                        ]
                    }
                },
                provenance=PROVENANCE,
                timezone_name="America/Vancouver",
                session=session,
            )

        operations = [
            operation
            for clause in plan.clauses
            for operation in clause.expected_operations
        ]
        self.assertEqual(len(operations), 1)
        self.assertIsInstance(operations[0], PreferenceUpsertOperation)

    def test_wake_time_update_supersedes_only_wake_time_not_other_schedule_styles(
        self,
    ) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            older_wake = UserPreference(
                domain="sleep",
                preference_type="schedule_style",
                scope_type="global",
                description="Wake up at 7:30 AM.",
                value_json='{"preferenceText":"Wake up at 7:30 AM."}',
            )
            deep_work = UserPreference(
                domain="deep_work",
                preference_type="schedule_style",
                scope_type="global",
                description="Prefer deep work in early mornings.",
                value_json='{"preferenceText":"Prefer deep work in early mornings."}',
            )
            session.add_all([older_wake, deep_work])
            session.commit()
            session.refresh(older_wake)
            session.refresh(deep_work)

            plan = CompatibilityProposalAdapter().adapt_preference_proposal(
                proposal={
                    "preference_patch": {
                        "operations": [
                            {
                                "op": "create_preference",
                                "preference": {
                                    "preferenceType": "schedule_style",
                                    "domain": "sleep",
                                    "scopeType": "global",
                                    "description": "Wake up at 7 AM.",
                                    "value": {
                                        "preferenceText": "Wake up at 7 AM."
                                    },
                                },
                            }
                        ]
                    }
                },
                provenance=PROVENANCE,
                timezone_name="America/Vancouver",
                session=session,
            )

        operations = [
            operation
            for clause in plan.clauses
            for operation in clause.expected_operations
        ]
        self.assertEqual(len(operations), 1)
        self.assertIsInstance(operations[0], PreferenceSupersedeOperation)
        self.assertEqual(
            operations[0].preference_ref.entity_id,
            f"preference:{older_wake.id}",
        )
        self.assertEqual(operations[0].semantics.domain, "sleep")
        self.assertNotEqual(
            operations[0].preference_ref.entity_id,
            f"preference:{deep_work.id}",
        )


if __name__ == "__main__":
    unittest.main()
