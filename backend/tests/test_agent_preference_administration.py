from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from app.models.preference_memory import (
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)
from app.services.agent_kernel.change_compilation import CompatibilityProposalAdapter
from app.services.agent_kernel.contracts import (
    ChangeSet,
    PreferenceDeleteOperation,
    PreferenceRestoreOperation,
    Provenance,
)
from app.services.agent_kernel.domain_mutations import NativeDomainMutationExecutor
from app.services.agent_kernel.tool_registry import execute_kernel_tool
from app.services.preference_admin import (
    analyze_remembered_preference_cleanup,
    list_remembered_preference_inventory,
)
from app.services.preference_patches import (
    build_preference_management_proposal_from_tool_arguments,
)


PROVENANCE = Provenance(
    source_kind="user",
    source_ref="turn:preference-admin",
    source_text="Disable the old preference and delete the duplicate.",
)


class AgentPreferenceAdministrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)

    def test_inventory_is_user_scoped_and_returns_exact_management_identity(self) -> None:
        with Session(self.engine) as session:
            local = UserPreference(
                id="local-pref",
                revision=3,
                user_id="local",
                preference_type="inter_task_gap",
                description="Leave 15 minutes between longer tasks.",
            )
            foreign = UserPreference(
                id="foreign-pref",
                user_id="someone-else",
                description="Private preference.",
            )
            constraint = SchedulingConstraint(
                id="local-constraint",
                revision=2,
                user_id="local",
                source_preference_id=local.id,
                constraint_type="inter_task_gap",
            )
            session.add(local)
            session.add(foreign)
            session.add(constraint)
            session.commit()

            inventory = list_remembered_preference_inventory(
                session,
                user_id="local",
            )

        self.assertEqual(inventory["count"], 1)
        item = inventory["preferences"][0]
        self.assertEqual(item["id"], "local-pref")
        self.assertEqual(item["entity_ref"], "preference:local-pref")
        self.assertEqual(item["revision"], 3)
        self.assertEqual(item["constraints"][0]["revision"], 2)

    def test_agent_inventory_tool_uses_injected_authoritative_reader(self) -> None:
        result = execute_kernel_tool(
            "list_remembered_preferences",
            {"include_inactive": True, "query": "gap", "limit": 25},
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            preference_inventory_reader=lambda arguments: {
                "preferences": [{"id": "pref-a", "revision": 4}],
                "count": 1,
                "active_count": 1,
                "inactive_count": 0,
                "arguments": arguments,
            },
        )

        self.assertTrue(result["read_only"])
        self.assertEqual(result["preferences"][0]["revision"], 4)
        self.assertEqual(result["arguments"]["query"], "gap")

    def test_cleanup_analysis_returns_complete_batch_and_keeps_ambiguity_separate(
        self,
    ) -> None:
        base = datetime(2026, 7, 1, 9, 0)
        records = [
            UserPreference(
                id="gap-canonical",
                preference_type="inter_task_gap",
                scope_type="global",
                description=(
                    "Schedule short tasks back-to-back and leave 15 minutes "
                    "around tasks lasting at least 45 minutes."
                ),
                value_json=(
                    '{"interTaskGapMinutes":15,'
                    '"minimumTaskDurationMinutes":45,'
                    '"shorterTaskGapMinutes":0}'
                ),
                updated_at=base + timedelta(days=2),
            ),
            UserPreference(
                id="gap-broad-category",
                preference_type="inter_task_gap",
                scope_type="category",
                scope_id="7",
                description="Leave 15 minutes between tasks.",
                value_json='{"interTaskGapMinutes":15}',
                updated_at=base + timedelta(days=3),
            ),
            UserPreference(
                id="gap-redundant-category",
                preference_type="inter_task_gap",
                scope_type="category",
                scope_id="8",
                description="Leave 15 minutes around tasks lasting at least 45 minutes.",
                value_json=(
                    '{"interTaskGapMinutes":15,'
                    '"minimumTaskDurationMinutes":45,'
                    '"shorterTaskGapMinutes":0}'
                ),
                updated_at=base + timedelta(days=3),
            ),
            UserPreference(
                id="wake-old",
                domain="sleep",
                preference_type="schedule_style",
                description="Wake-up time to 7:30.",
                updated_at=base,
            ),
            UserPreference(
                id="wake-new",
                domain="sleep",
                preference_type="schedule_style",
                description="Wake-up time to 7.",
                updated_at=base + timedelta(days=1),
            ),
            UserPreference(
                id="start-old",
                preference_type="availability",
                description="Usually avoid scheduling before 8 AM.",
                value_json='{"preferredEarliestStartTime":"08:00"}',
                updated_at=base,
            ),
            UserPreference(
                id="start-new",
                preference_type="availability",
                strength="hard",
                description="Do not schedule before 10 AM.",
                value_json='{"rule":"do_not_schedule_before","endTime":"10:00"}',
                updated_at=base + timedelta(days=1),
            ),
            UserPreference(
                id="deep-work-ambiguous",
                domain="deep_work",
                preference_type="schedule_style",
                description="Prefer deep work in early mornings or late nights.",
                updated_at=base,
            ),
        ]
        with Session(self.engine) as session:
            session.add_all(records)
            session.commit()
            analysis = analyze_remembered_preference_cleanup(
                session,
                user_id="local",
            )

        self.assertTrue(analysis["complete"])
        self.assertEqual(analysis["active_inventory_count"], len(records))
        self.assertEqual(
            {item["id"] for item in analysis["candidates"]},
            {
                "gap-broad-category",
                "gap-redundant-category",
                "wake-old",
                "start-old",
            },
        )
        self.assertEqual(
            {item["id"] for item in analysis["ambiguous"]},
            {"deep-work-ambiguous"},
        )
        self.assertTrue(
            analysis["candidate_set_fingerprint"].startswith("sha256:")
        )
        with Session(self.engine) as session:
            contradictions = analyze_remembered_preference_cleanup(
                session,
                user_id="local",
                cleanup_scope="contradictions",
            )
            overlaps = analyze_remembered_preference_cleanup(
                session,
                user_id="local",
                cleanup_scope="overlaps",
            )
        self.assertEqual(
            {item["id"] for item in contradictions["candidates"]},
            {"gap-broad-category", "wake-old", "start-old"},
        )
        self.assertEqual(
            {item["id"] for item in overlaps["candidates"]},
            {"gap-redundant-category"},
        )
        self.assertNotEqual(
            contradictions["candidate_set_fingerprint"],
            overlaps["candidate_set_fingerprint"],
        )

    def test_newer_unlinked_record_does_not_override_direct_user_source(
        self,
    ) -> None:
        base = datetime(2026, 7, 1, 9, 0)
        sourced = UserPreference(
            id="sourced-start-time",
            preference_type="availability",
            description="Usually avoid scheduling before 8 AM.",
            value_json=(
                '{"preferredEarliestStartTime":"08:00",'
                '"sourceText":"I usually wake up at 8 AM."}'
            ),
            updated_at=base,
        )
        unlinked_newer = UserPreference(
            id="unlinked-newer-start-time",
            preference_type="availability",
            description="Do not schedule before 10 AM.",
            value_json='{"rule":"do_not_schedule_before","endTime":"10:00"}',
            updated_at=base + timedelta(days=1),
        )
        with Session(self.engine) as session:
            session.add_all([sourced, unlinked_newer])
            session.commit()
            result = analyze_remembered_preference_cleanup(
                session,
                user_id="local",
                cleanup_scope="contradictions",
            )

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(
            result["candidates"][0]["id"],
            "unlinked-newer-start-time",
        )
        self.assertEqual(
            result["candidates"][0]["reason_code"],
            "lower_provenance_conflict",
        )
        self.assertEqual(
            result["candidates"][0]["conflicts_with"][0]["id"],
            "sourced-start-time",
        )

    def test_cleanup_identifies_semantic_paraphrase_duplicate(self) -> None:
        base = datetime(2026, 7, 1, 9, 0)
        older = UserPreference(
            id="worded-gap-older",
            domain="scheduling",
            preference_type="schedule_style",
            description="Please leave fifteen minutes of buffer between tasks.",
            value_json=(
                '{"preferenceText":"Please leave fifteen minutes of buffer '
                'between tasks."}'
            ),
            updated_at=base,
        )
        newer = UserPreference(
            id="structured-gap-newer",
            domain="scheduling",
            preference_type="inter_task_gap",
            description="Use a 15-minute transition between every task.",
            value_json='{"interTaskGapMinutes":15}',
            updated_at=base + timedelta(days=1),
        )
        with Session(self.engine) as session:
            session.add_all([older, newer])
            session.commit()
            result = analyze_remembered_preference_cleanup(
                session,
                user_id="local",
                cleanup_scope="overlaps",
            )

        self.assertEqual(result["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["id"], older.id)
        self.assertIn(
            candidate["reason_code"],
            {"duplicate_older_copy", "semantic_duplicate_older_copy"},
        )
        self.assertEqual(candidate["conflicts_with"][0]["id"], newer.id)

    def test_cleanup_submission_conserves_every_server_candidate_and_rejects_stale_set(
        self,
    ) -> None:
        analysis = {
            "candidates": [
                {
                    "id": "old-wake",
                    "revision": 2,
                    "description": "Wake at 7:30.",
                },
                {
                    "id": "old-gap",
                    "revision": 4,
                    "description": "Leave a gap between every task.",
                },
            ],
            "candidate_count": 2,
            "candidate_set_fingerprint": "sha256:complete-batch",
            "ambiguous": [],
            "ambiguous_count": 0,
            "kept_preferences": [],
            "active_inventory_count": 2,
            "complete": True,
        }
        stale = execute_kernel_tool(
            "submit_preference_cleanup_plan",
            {
                "action": "disable",
                "cleanup_scope": "all",
                "candidate_set_fingerprint": "sha256:stale",
            },
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            preference_cleanup_analyzer=lambda _arguments: analysis,
        )
        self.assertFalse(stale["compiled"])
        self.assertEqual(stale["error"], "preference_cleanup_candidate_set_stale")

        result = execute_kernel_tool(
            "submit_preference_cleanup_plan",
            {
                "action": "disable",
                "cleanup_scope": "all",
                "candidate_set_fingerprint": "sha256:complete-batch",
            },
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            preference_cleanup_analyzer=lambda _arguments: analysis,
        )
        self.assertTrue(result["compiled"])
        self.assertTrue(result["complete_batch"])
        self.assertEqual(result["candidate_count"], 2)
        operations = result["semantic_plan_input"]["payload"]["preference_patch"][
            "operations"
        ]
        self.assertEqual(
            {item["preference"]["id"] for item in operations},
            {"old-wake", "old-gap"},
        )

    def test_cleanup_submission_supports_large_batch_without_silent_truncation(
        self,
    ) -> None:
        candidates = [
            {
                "id": f"older-preference-{index}",
                "revision": 1,
                "description": f"Older preference {index}",
            }
            for index in range(40)
        ]
        analysis = {
            "candidates": candidates,
            "candidate_count": len(candidates),
            "candidate_set_fingerprint": "sha256:large-complete-batch",
            "ambiguous": [],
            "ambiguous_count": 0,
            "kept_preferences": [],
            "active_inventory_count": len(candidates),
            "complete": True,
        }
        result = execute_kernel_tool(
            "submit_preference_cleanup_plan",
            {
                "action": "disable",
                "cleanup_scope": "all",
                "candidate_set_fingerprint": "sha256:large-complete-batch",
            },
            categories=[],
            nodes=[],
            focus_node=None,
            staged_node_ids=[],
            preference_cleanup_analyzer=lambda _arguments: analysis,
        )
        self.assertTrue(result["compiled"])
        self.assertEqual(result["candidate_count"], 40)
        operations = result["semantic_plan_input"]["payload"]["preference_patch"][
            "operations"
        ]
        self.assertEqual(len(operations), 40)
        self.assertEqual(
            {item["preference"]["id"] for item in operations},
            {item["id"] for item in candidates},
        )

    def test_management_plan_compiles_exact_disable_restore_and_delete_operations(self) -> None:
        compiled = build_preference_management_proposal_from_tool_arguments(
            {
                "actions": [
                    {
                        "action": "restore",
                        "preference_id": "disabled-pref",
                        "revision": 2,
                        "description": "Use mornings for focus work.",
                    },
                    {
                        "action": "delete",
                        "preference_id": "duplicate-pref",
                        "revision": 5,
                        "description": "Old duplicate gap rule.",
                    },
                ]
            }
        )
        self.assertTrue(compiled["compiled"])

        with Session(self.engine) as session:
            session.add(
                UserPreference(
                    id="disabled-pref",
                    revision=2,
                    active=False,
                    description="Use mornings for focus work.",
                )
            )
            session.add(
                UserPreference(
                    id="duplicate-pref",
                    revision=5,
                    description="Old duplicate gap rule.",
                )
            )
            session.commit()
            artifact = CompatibilityProposalAdapter().adapt_preference_proposal(
                proposal=compiled["preference_memory_proposal"],
                provenance=PROVENANCE,
                timezone_name="America/Vancouver",
                session=session,
            )

        operations = [
            operation
            for clause in artifact.clauses
            for operation in clause.expected_operations
        ]
        self.assertIsInstance(operations[0], PreferenceRestoreOperation)
        self.assertIsInstance(operations[1], PreferenceDeleteOperation)
        self.assertEqual(
            operations[0].preference_ref.entity_id,
            "preference:disabled-pref",
        )
        self.assertEqual(operations[1].preference_ref.revision, 5)

    def test_restore_and_delete_mutate_linked_memory_atomically(self) -> None:
        with Session(self.engine) as session:
            session.add(
                UserPreference(id="restore-pref", active=False, revision=1)
            )
            session.add(
                SchedulingConstraint(
                    id="restore-constraint",
                    source_preference_id="restore-pref",
                    active=False,
                    revision=1,
                )
            )
            session.add(
                PreferenceLink(
                    id="restore-link",
                    preference_id="restore-pref",
                    target_type="workspace",
                    target_id="1",
                    active=False,
                    revision=1,
                )
            )
            session.add(UserPreference(id="delete-pref", revision=1))
            session.add(
                SchedulingConstraint(
                    id="delete-constraint",
                    source_preference_id="delete-pref",
                )
            )
            session.add(
                PreferenceObservation(
                    id="delete-observation",
                    preference_id="delete-pref",
                )
            )
            session.add(
                PreferenceLink(
                    id="delete-link",
                    preference_id="delete-pref",
                    target_type="workspace",
                    target_id="1",
                )
            )
            session.commit()
            change_set = ChangeSet(
                changeSetId="changeset:preference-admin",
                goalId="goal:preference-admin",
                idempotencyKey="draft:preference-admin",
                correlationId="corr:preference-admin",
                causationId="turn:preference-admin",
                operations=[
                    PreferenceRestoreOperation(
                        operationId="op:restore",
                        preferenceRef={
                            "kind": "preference",
                            "entityId": "preference:restore-pref",
                            "revision": 1,
                        },
                    ),
                    PreferenceDeleteOperation(
                        operationId="op:delete",
                        preferenceRef={
                            "kind": "preference",
                            "entityId": "preference:delete-pref",
                            "revision": 1,
                        },
                    ),
                ],
            )
            NativeDomainMutationExecutor(
                session,
                approved_revisions={
                    (
                        "scheduling_constraint",
                        "scheduling_constraint:restore-constraint",
                    ): 1,
                    ("preference_link", "preference_link:restore-link"): 1,
                },
            ).apply(change_set)
            session.commit()

            self.assertTrue(session.get(UserPreference, "restore-pref").active)
            self.assertTrue(
                session.get(SchedulingConstraint, "restore-constraint").active
            )
            self.assertTrue(session.get(PreferenceLink, "restore-link").active)
            self.assertIsNone(session.get(UserPreference, "delete-pref"))
            self.assertIsNone(
                session.get(SchedulingConstraint, "delete-constraint")
            )
            self.assertIsNone(
                session.get(PreferenceObservation, "delete-observation")
            )
            self.assertIsNone(session.get(PreferenceLink, "delete-link"))


if __name__ == "__main__":
    unittest.main()
