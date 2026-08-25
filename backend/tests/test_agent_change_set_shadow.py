from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.agent_kernel.change_set_shadow import (
    _preference_value_mismatch_class,
    build_change_set_shadow_observation,
)
from app.services.agent_kernel.contracts import (
    ChangeSet,
    CalendarCreateBlockOperation,
    CalendarMoveBlockOperation,
    EntityRef,
    OperationConservationReport,
    OperationFingerprint,
    PreferenceScalarValue,
    PreferenceSemantics,
    PreferenceUpsertOperation,
    RecurrenceCreateSeriesOperation,
    RecurrenceOccurrencePatch,
    RecurrenceRule,
    RecurrenceUpdateOccurrenceOperation,
    TaskCreateOperation,
    TaskArchiveOperation,
    TaskDeleteOperation,
    TaskMoveOperation,
    TaskUpdateOperation,
    TaskUpdatePatch,
)


class AgentChangeSetShadowTests(unittest.TestCase):
    def test_preference_mismatch_classification_is_bounded_and_privacy_safe(self) -> None:
        scalar = lambda value: {"kind": "scalar", "value": value}
        self.assertEqual(
            _preference_value_mismatch_class(
                scalar("focused work in the morning"),
                scalar("focused work in morning"),
            ),
            "article",
        )
        self.assertEqual(
            _preference_value_mismatch_class(
                scalar("morning focused work"),
                scalar("focused work morning"),
            ),
            "order",
        )
        self.assertEqual(
            _preference_value_mismatch_class(
                scalar("focused work in the morning"),
                scalar("work in the morning"),
            ),
            "omission",
        )

    def test_observation_preserves_native_semantics_without_legacy_compilation(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:1",
            goal_id="goal:1",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create",
                    title="Launch",
                    workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
                    temp_ref="temp:task",
                    atomic_group_id="group:1",
                    fulfills_clause_ids=["clause:create"],
                )
            ],
            idempotency_key="command:1",
            correlation_id="corr:1",
            causation_id="turn:1",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:create"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:create", kind="task.create", sha256="a" * 64
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={"branch": {"structural_operations": [{"kind": "create_node"}]}},
        )
        self.assertEqual(observation["comparisonStatus"], "not_comparable")
        self.assertEqual(observation["contractVersion"], "2.0")
        self.assertEqual(observation["native"]["operationCount"], 1)
        self.assertEqual(len(observation["native"]["semanticPayloadSha256"]), 64)
        self.assertNotIn("changeset:1", str(observation))
        self.assertNotIn("workspace:1", str(observation))
        self.assertNotIn("Launch", str(observation))
        self.assertTrue(observation["legacy"]["remainsLiveAuthority"])

        divergent = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={"preference_memory": {"kind": "preference"}},
        )
        self.assertEqual(divergent["comparisonStatus"], "divergent")
        self.assertIn("operation_families", divergent["divergentDimensions"])

    def test_target_bindings_normalize_legacy_and_canonical_task_identities(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:targets",
            goal_id="goal:targets",
            operations=[
                TaskUpdateOperation(
                    operation_id="op:update",
                    task_ref=EntityRef(kind="task", entity_id="task:42"),
                    updates=TaskUpdatePatch(
                        deadline_at=datetime(2026, 6, 11, tzinfo=timezone.utc)
                    ),
                    fulfills_clause_ids=["clause:update"],
                )
            ],
            idempotency_key="command:targets",
            correlation_id="corr:targets",
            causation_id="turn:targets",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:update"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:update", kind="task.update", sha256="b" * 64
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "metadata": {
                    "target_node_id": 42,
                    "updates": {"deadline_at": "2026-06-11"},
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[],
                resolved_decisions=[],
                correction_deltas=[],
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        target_dimension = next(
            item for item in observation["dimensions"] if item["name"] == "target_bindings"
        )
        self.assertEqual(target_dimension["status"], "matched")
        self.assertEqual(
            observation["native"]["targetSetSha256"],
            observation["legacy"]["targetSetSha256"],
        )
        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["notComparableDimensions"], [])

    def test_task_create_compares_temp_binding_values_and_workspace_scope(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:create",
            goal_id="goal:create",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create-task",
                    title="Learners test",
                    workspace_ref=EntityRef(kind="workspace", entity_id="category:7"),
                    temp_ref=EntityRef(
                        kind="temporary",
                        entity_id="temp:task:1",
                        target_kind="task",
                    ),
                    fields={"deadlineAt": "2026-05-26T00:00:00Z"},
                    fulfills_clause_ids=["clause:create-task"],
                )
            ],
            idempotency_key="command:create",
            correlation_id="corr:create",
            causation_id="turn:create",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:create-task"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:create-task", kind="task.create", sha256="c" * 64
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "branch": {
                    "target_node_id": 7,
                    "structural_operations": [
                        {
                            "kind": "create_node",
                            "temp_id": "new_task",
                            "title": "  learners TEST ",
                            "category_id": 7,
                            "deadline_at": "2026-05-26",
                        }
                    ],
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["divergentDimensions"], [])
        self.assertEqual(observation["notComparableDimensions"], [])

    def test_mixed_created_tasks_and_selected_schedule_compare_as_one_plan(self) -> None:
        appointment_ref = EntityRef(
            kind="temporary",
            entity_id="temp:task:appointment",
            target_kind="task",
        )
        meeting_ref = EntityRef(
            kind="temporary",
            entity_id="temp:task:meeting",
            target_kind="task",
        )
        follow_up_ref = EntityRef(
            kind="temporary",
            entity_id="temp:task:follow-up",
            target_kind="task",
        )
        change_set = ChangeSet(
            change_set_id="changeset:mixed-create-schedule",
            goal_id="goal:mixed-create-schedule",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create-appointment",
                    title="Vehicle appointment",
                    workspace_ref=EntityRef(
                        kind="workspace", entity_id="workspace:7"
                    ),
                    temp_ref=appointment_ref,
                    atomic_group_id="group:appointment",
                    fulfills_clause_ids=["clause:mixed"],
                ),
                CalendarCreateBlockOperation(
                    operation_id="op:schedule-appointment",
                    task_ref=appointment_ref,
                    start_at=datetime(
                        2040, 8, 20, 13, 30, tzinfo=ZoneInfo("America/Vancouver")
                    ),
                    end_at=datetime(
                        2040, 8, 20, 14, 30, tzinfo=ZoneInfo("America/Vancouver")
                    ),
                    depends_on=["op:create-appointment"],
                    atomic_group_id="group:appointment",
                    fulfills_clause_ids=["clause:mixed"],
                ),
                TaskCreateOperation(
                    operation_id="op:create-meeting",
                    title="Meet a contact",
                    workspace_ref=EntityRef(
                        kind="workspace", entity_id="workspace:7"
                    ),
                    temp_ref=meeting_ref,
                    fields={"deadlineAt": "2040-08-21T23:59:59-07:00"},
                    atomic_group_id="group:meeting",
                    fulfills_clause_ids=["clause:mixed"],
                ),
                TaskCreateOperation(
                    operation_id="op:create-follow-up",
                    title="Confirm the meeting time",
                    workspace_ref=EntityRef(
                        kind="workspace", entity_id="workspace:7"
                    ),
                    parent_ref=meeting_ref,
                    temp_ref=follow_up_ref,
                    depends_on=["op:create-meeting"],
                    atomic_group_id="group:meeting",
                    fulfills_clause_ids=["clause:mixed"],
                ),
            ],
            idempotency_key="command:mixed-create-schedule",
            correlation_id="corr:mixed-create-schedule",
            causation_id="turn:mixed-create-schedule",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:mixed"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id=operation.operation_id,
                    kind=operation.kind,
                    sha256=str(index + 1) * 64,
                )
                for index, operation in enumerate(change_set.operations)
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "branch": {
                    "structural_operations": [
                        {
                            "kind": "create_node",
                            "temp_id": "vehicle",
                            "title": "Vehicle appointment",
                            "category_id": 7,
                            # This is compatibility scheduling metadata rather
                            # than a distinct user-requested task field.
                            "time_estimate_minutes": 60,
                        },
                        {
                            "kind": "create_node",
                            "temp_id": "contact",
                            "title": "Meet a contact",
                            "category_id": 7,
                            "deadline_at": "2040-08-21",
                        },
                        {
                            "kind": "create_node",
                            "temp_id": "follow-up",
                            "parent_temp_id": "contact",
                            "title": "Confirm the meeting time",
                            "category_id": 7,
                        },
                    ],
                    "created_task_schedule": {
                        "schedule_groups": [
                            {
                                "target_temp_ids": ["vehicle"],
                                "window_start": "2040-08-20T13:30:00",
                                "window_end": "2040-08-20T14:30:00",
                            }
                        ]
                    },
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )

        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["native"]["domains"], ["calendar", "task"])
        self.assertEqual(observation["legacy"]["domains"], ["calendar", "task"])
        self.assertEqual(observation["native"]["operationCount"], 4)
        self.assertEqual(observation["legacy"]["operationCount"], 4)

    def test_task_branch_compares_move_and_archive_semantics(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:organize",
            goal_id="goal:organize",
            operations=[
                TaskMoveOperation(
                    operation_id="op:move",
                    task_refs=[EntityRef(kind="task", entity_id="task:42")],
                    destination_workspace_ref=EntityRef(
                        kind="workspace", entity_id="workspace:9"
                    ),
                    atomic_group_id="group:organize",
                    fulfills_clause_ids=["clause:move"],
                ),
                TaskArchiveOperation(
                    operation_id="op:archive",
                    task_refs=[
                        EntityRef(kind="task", entity_id="task:43"),
                        EntityRef(kind="task", entity_id="task:44"),
                    ],
                    atomic_group_id="group:organize",
                    fulfills_clause_ids=["clause:archive"],
                ),
            ],
            idempotency_key="command:organize",
            correlation_id="corr:organize",
            causation_id="turn:organize",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:move", "clause:archive"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:move", kind="task.move", sha256="d" * 64
                ),
                OperationFingerprint(
                    operation_id="op:archive", kind="task.archive", sha256="e" * 64
                ),
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "branch": {
                    "structural_operations": [
                        {
                            "kind": "move_subtree",
                            "node_id": 42,
                            "destination_category_id": 9,
                            "destination_parent_id": None,
                        },
                        {"kind": "archive_node", "node_id": 43},
                        {"kind": "archive_node", "node_id": 44},
                    ]
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["divergentDimensions"], [])
        self.assertEqual(observation["notComparableDimensions"], [])

    def test_task_delete_action_compares_exact_bulk_targets(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:delete",
            goal_id="goal:delete",
            operations=[
                TaskDeleteOperation(
                    operation_id="op:delete",
                    task_refs=[
                        EntityRef(kind="task", entity_id="task:43"),
                        EntityRef(kind="task", entity_id="task:44"),
                    ],
                    atomic_group_id="group:delete",
                    fulfills_clause_ids=["clause:delete"],
                )
            ],
            idempotency_key="command:delete",
            correlation_id="corr:delete",
            causation_id="turn:delete",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:delete"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:delete", kind="task.delete", sha256="6" * 64
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "action": {
                    "proposal_kind": "delete",
                    "kind": "delete_node",
                    "target_node_ids": [43, 44],
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )

        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["native"]["targetCount"], 2)
        self.assertEqual(observation["legacy"]["targetCount"], 2)

    def test_task_delete_action_fails_on_target_substitution(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:delete-mismatch",
            goal_id="goal:delete-mismatch",
            operations=[
                TaskDeleteOperation(
                    operation_id="op:delete",
                    task_refs=[EntityRef(kind="task", entity_id="task:43")],
                    atomic_group_id="group:delete",
                    fulfills_clause_ids=["clause:delete"],
                )
            ],
            idempotency_key="command:delete-mismatch",
            correlation_id="corr:delete-mismatch",
            causation_id="turn:delete-mismatch",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:delete"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:delete", kind="task.delete", sha256="7" * 64
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "action": {
                    "proposal_kind": "delete",
                    "kind": "delete_node",
                    "target_node_ids": [44],
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )

        self.assertEqual(observation["comparisonStatus"], "divergent")
        self.assertIn("target_bindings", observation["divergentDimensions"])

    def test_schedule_patch_compares_calendar_create_semantics(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:schedule",
            goal_id="goal:schedule",
            operations=[
                CalendarCreateBlockOperation(
                    operation_id="op:block",
                    task_ref=EntityRef(kind="task", entity_id="task:7"),
                    start_at=datetime(
                        2042, 1, 1, 9, tzinfo=ZoneInfo("America/Vancouver")
                    ),
                    end_at=datetime(
                        2042, 1, 1, 10, tzinfo=ZoneInfo("America/Vancouver")
                    ),
                    temp_ref=EntityRef(
                        kind="temporary",
                        entity_id="temp:block:1",
                        target_kind="calendar_block",
                    ),
                    fulfills_clause_ids=["clause:schedule"],
                )
            ],
            idempotency_key="command:schedule",
            correlation_id="corr:schedule",
            causation_id="turn:schedule",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:schedule"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:block",
                    kind="calendar.create_block",
                    sha256="f" * 64,
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "schedule_patch": {
                    "placementReasons": [{"taskId": 99, "message": "presentation only"}],
                    "schedulePatch": {
                        "operations": [
                            {
                                "op": "create_block",
                                "taskId": 7,
                                "proposalBlockId": "draft-1",
                                "startAt": "2042-01-01T09:00:00",
                                "endAt": "2042-01-01T10:00:00",
                            }
                        ]
                    }
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
            legacy_timezone="America/Vancouver",
        )
        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["divergentDimensions"], [])
        self.assertEqual(observation["notComparableDimensions"], [])

        missing_timezone = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "schedule_patch": {
                    "schedulePatch": {
                        "operations": [
                            {
                                "op": "create_block",
                                "taskId": 7,
                                "startAt": "2042-01-01T09:00:00",
                                "endAt": "2042-01-01T10:00:00",
                            }
                        ]
                    }
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        self.assertIn("semantic_values", missing_timezone["notComparableDimensions"])

    def test_schedule_patch_compares_existing_block_move_semantics(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:move-block",
            goal_id="goal:move-block",
            operations=[
                CalendarMoveBlockOperation(
                    operation_id="op:move-block",
                    block_ref=EntityRef(
                        kind="calendar_block", entity_id="calendar_block:22"
                    ),
                    start_at=datetime(2042, 1, 1, 11, tzinfo=timezone.utc),
                    end_at=datetime(2042, 1, 1, 12, tzinfo=timezone.utc),
                    fulfills_clause_ids=["clause:move-block"],
                )
            ],
            idempotency_key="command:move-block",
            correlation_id="corr:move-block",
            causation_id="turn:move-block",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:move-block"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:move-block",
                    kind="calendar.move_block",
                    sha256="1" * 64,
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "schedule_patch": {
                    "schedulePatch": {
                        "operations": [
                            {
                                "op": "update_block_time",
                                "acceptedBlockId": 22,
                                "startAt": "2042-01-01T11:00:00+00:00",
                                "endAt": "2042-01-01T12:00:00+00:00",
                            }
                        ]
                    }
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        self.assertEqual(observation["comparisonStatus"], "matched")

    def test_preference_patch_compares_soft_preference_semantics(self) -> None:
        change_set = ChangeSet(
            change_set_id="changeset:preference",
            goal_id="goal:preference",
            operations=[
                PreferenceUpsertOperation(
                    operation_id="op:preference",
                    preference_key="schedule_style",
                    value=PreferenceScalarValue(
                        value="Preference: focused work in the morning."
                    ),
                    semantics=PreferenceSemantics(
                        domain="scheduling", strength="soft", scope="global"
                    ),
                    fulfills_clause_ids=["clause:preference"],
                )
            ],
            idempotency_key="command:preference",
            correlation_id="corr:preference",
            causation_id="turn:preference",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:preference"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:preference",
                    kind="preference.upsert",
                    sha256="2" * 64,
                )
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "preference_memory": {
                    "preference_patch": {
                        "operations": [
                            {
                                "op": "create_preference",
                                "preference": {
                                    "domain": "deep_work",
                                    "preferenceType": "schedule_style",
                                    "scopeType": "global",
                                    "strength": "medium",
                                    "value": {
                                        "normalizedText": "focused work in the morning"
                                    },
                                },
                            }
                        ]
                    }
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        self.assertEqual(observation["comparisonStatus"], "matched")

    def test_branch_compares_created_task_and_recurrence_as_two_families(self) -> None:
        task_temp = EntityRef(
            kind="temporary", entity_id="temp:task:daily", target_kind="task"
        )
        change_set = ChangeSet(
            change_set_id="changeset:recurring",
            goal_id="goal:recurring",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create-recurring-task",
                    title="Check appointment rebooking",
                    workspace_ref=EntityRef(kind="workspace", entity_id="workspace:7"),
                    temp_ref=task_temp,
                    fields={"estimateMinutes": 5},
                    atomic_group_id="group:recurring",
                    fulfills_clause_ids=["clause:create"],
                ),
                RecurrenceCreateSeriesOperation(
                    operation_id="op:create-series",
                    task_ref=task_temp,
                    rule=RecurrenceRule(
                        frequency="daily",
                        start_date="2040-03-01",
                        occurrence_count=14,
                        preferred_start_time="18:00",
                        duration_minutes=5,
                    ),
                    depends_on=["op:create-recurring-task"],
                    atomic_group_id="group:recurring",
                    fulfills_clause_ids=["clause:recurrence"],
                ),
            ],
            idempotency_key="command:recurring",
            correlation_id="corr:recurring",
            causation_id="turn:recurring",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:create", "clause:recurrence"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id="op:create-recurring-task",
                    kind="task.create",
                    sha256="3" * 64,
                ),
                OperationFingerprint(
                    operation_id="op:create-series",
                    kind="recurrence.create_series",
                    sha256="4" * 64,
                ),
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "branch": {
                    "structural_operations": [
                        {
                            "kind": "create_node",
                            "temp_id": "daily-check",
                            "title": "Check appointment rebooking",
                            "category_id": 7,
                            "time_estimate_minutes": 5,
                        }
                    ],
                    "recurring_schedule": {
                        "target_temp_id": "daily-check",
                        "frequency": "daily",
                        "start_date": "2040-03-01",
                        "occurrence_count": 14,
                        "duration_minutes": 5,
                        "preferred_start_time": "18:00",
                    },
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[], resolved_decisions=[], correction_deltas=[]
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )
        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["native"]["domains"], ["recurrence", "task"])
        self.assertEqual(observation["legacy"]["domains"], ["recurrence", "task"])

    def test_recurring_branch_compares_occurrence_resolution_and_decision_trajectory(self) -> None:
        task_temp = EntityRef(
            kind="temporary", entity_id="temp:task:team", target_kind="task"
        )
        change_set = ChangeSet(
            revision=2,
            change_set_id="changeset:recurring-resolution",
            goal_id="goal:recurring-resolution",
            operations=[
                TaskCreateOperation(
                    operation_id="op:create-team",
                    title="Team call",
                    workspace_ref=EntityRef(kind="workspace", entity_id="workspace:7"),
                    temp_ref=task_temp,
                    fields={},
                    atomic_group_id="group:team",
                    fulfills_clause_ids=["clause:team"],
                ),
                RecurrenceCreateSeriesOperation(
                    operation_id="op:create-team-series",
                    task_ref=task_temp,
                    rule=RecurrenceRule(
                        frequency="weekly",
                        weekdays=["monday", "tuesday", "wednesday", "thursday", "friday"],
                        start_date="2026-07-20",
                        end_date="2026-07-24",
                        timezone="America/Vancouver",
                        preferred_start_time="18:30",
                        duration_minutes=60,
                    ),
                    depends_on=["op:create-team"],
                    atomic_group_id="group:team",
                    fulfills_clause_ids=["clause:team"],
                ),
                RecurrenceUpdateOccurrenceOperation(
                    operation_id="op:move-workout-monday",
                    occurrence_ref=EntityRef(
                        kind="recurrence_occurrence",
                        entity_id="recurrence_occurrence:workout:2026-07-20",
                    ),
                    updates=RecurrenceOccurrencePatch(
                        start_at=datetime(
                            2026, 7, 20, 19, 45, tzinfo=ZoneInfo("America/Vancouver")
                        ),
                        end_at=datetime(
                            2026, 7, 20, 21, 45, tzinfo=ZoneInfo("America/Vancouver")
                        ),
                    ),
                ),
            ],
            source_simulation_id="simulation:recurring-resolution",
            resolution_alternative_id="alternative:after",
            idempotency_key="command:recurring-resolution",
            correlation_id="corr:recurring-resolution",
            causation_id="turn:recurring-resolution",
        )
        report = OperationConservationReport(
            complete=True,
            covered_clause_ids=["clause:team"],
            operation_fingerprints=[
                OperationFingerprint(
                    operation_id=operation.operation_id,
                    kind=operation.kind,
                    sha256=str(index + 1) * 64,
                )
                for index, operation in enumerate(change_set.operations)
            ],
        )
        observation = build_change_set_shadow_observation(
            change_set=change_set,
            conservation=report,
            legacy_proposals={
                "branch": {
                    "structural_operations": [
                        {
                            "kind": "create_node",
                            "temp_id": "team-call",
                            "title": "Team call",
                            "category_id": 7,
                            "time_estimate_minutes": 60,
                        }
                    ],
                    "recurring_schedule": {
                        "target_temp_id": "team-call",
                        "frequency": "weekly",
                        "days_of_week": [
                            "monday",
                            "tuesday",
                            "wednesday",
                            "thursday",
                            "friday",
                        ],
                        "start_date": "2026-07-20",
                        "end_date": "2026-07-24",
                        "timezone": "America/Vancouver",
                        "preferred_start_time": "18:30",
                        "duration_minutes": 60,
                    },
                    "payload": {
                        "recurring_schedule_plan": {
                            "conflict_relocation": "after_incoming",
                            "displacement_operations": [
                                {
                                    "acceptedBlockId": 42,
                                    "startAt": "2026-07-20T19:45:00",
                                    "endAt": "2026-07-20T21:45:00",
                                }
                            ],
                        }
                    },
                }
            },
            goal=SimpleNamespace(
                unresolved_decisions=[],
                resolved_decisions=[SimpleNamespace(decision_id="decision:1")],
                correction_deltas=[],
            ),
            simulation=SimpleNamespace(
                status="ready_for_review",
                conflict_graph=SimpleNamespace(status="clear"),
                issues=[],
                conservation=SimpleNamespace(complete=True),
            ),
        )

        self.assertEqual(observation["comparisonStatus"], "matched")
        self.assertEqual(observation["native"]["operationCount"], 3)
        self.assertEqual(observation["legacy"]["operationCount"], 3)


if __name__ == "__main__":
    unittest.main()
