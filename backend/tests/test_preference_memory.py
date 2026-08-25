import json
import unittest
from datetime import datetime

from sqlmodel import Session

from app.db.database import create_db_and_tables, engine
from app.models.preference_memory import (
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)
from app.services.preference_memory import (
    PreferenceRetrievalScope,
    SqlPreferenceApplicationService,
    build_model_preference_context,
    build_preference_context_for_scope,
    constraint_intervals_from_context,
    inter_task_gap_policy_from_context,
    inter_task_gap_minutes_for_duration,
)


class PreferenceMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()

    def test_tagged_structured_gap_survives_retrieval_and_applies_at_threshold(self) -> None:
        user_id = "tagged-gap-threshold-test"
        with Session(engine) as session:
            session.add(
                UserPreference(
                    user_id=user_id,
                    domain="scheduling",
                    preference_type="inter_task_gap",
                    scope_type="global",
                    strength="high",
                    description="Leave 15 minutes between tasks lasting at least 45 minutes.",
                    value_json=json.dumps(
                        {
                            "kind": "scalar",
                            "value": json.dumps(
                                {
                                    "interTaskGapMinutes": 15,
                                    "minimumTaskDurationMinutes": 45,
                                    "shorterTaskGapMinutes": 0,
                                }
                            ),
                        }
                    ),
                )
            )
            session.commit()
            context = build_preference_context_for_scope(
                session,
                task_ids=[],
                category_ids=[],
                task_titles=["Cooking", "Cleaning"],
                category_names=["Home"],
                window_start=datetime(2040, 1, 1, 9, 0),
                window_end=datetime(2040, 1, 1, 17, 0),
                prompt="Schedule cooking and cleaning",
                user_id=user_id,
            )

        policy = inter_task_gap_policy_from_context(context)
        self.assertEqual(
            inter_task_gap_minutes_for_duration(policy, duration_minutes=44),
            (0, 0),
        )
        self.assertEqual(
            inter_task_gap_minutes_for_duration(policy, duration_minutes=45),
            (0, 15),
        )

    def test_complementary_duration_ranges_both_compile_into_gap_policy(self) -> None:
        policy = inter_task_gap_policy_from_context(
            {
                "softPreferences": [
                    {
                        "id": "long-tasks",
                        "preferenceType": "inter_task_gap",
                        "strength": "medium",
                        "scope": {"type": "global"},
                        "updatedAt": "2040-01-02T00:00:00",
                        "value": {
                            "interTaskGapMinutes": 15,
                            "minimumTaskDurationMinutes": 45,
                        },
                    },
                    {
                        "id": "short-tasks",
                        "preferenceType": "inter_task_gap",
                        "strength": "medium",
                        "scope": {"type": "global"},
                        "updatedAt": "2040-01-01T00:00:00",
                        "value": {
                            "interTaskGapMinutes": 5,
                            "maximumTaskDurationMinutes": 44,
                        },
                    },
                ],
                "hardConstraints": [],
            }
        )

        self.assertEqual(
            inter_task_gap_minutes_for_duration(policy, duration_minutes=30),
            (5, 0),
        )
        self.assertEqual(
            inter_task_gap_minutes_for_duration(policy, duration_minutes=45),
            (15, 0),
        )

    def test_retrieval_scopes_memory_without_dumping_unrelated_preferences(self) -> None:
        user_id = "memory-scope-test"
        category_id = 4101
        task_id = 4202
        with Session(engine) as session:
            focus_preference = UserPreference(
                user_id=user_id,
                domain="deep_work",
                preference_type="schedule_style",
                scope_type="category",
                scope_id=str(category_id),
                strength="strong",
                description="Prefer focused work in clear blocks.",
                value_json=json.dumps({"preferredTime": "morning"}),
            )
            task_preference = UserPreference(
                user_id=user_id,
                domain="scheduling",
                preference_type="task_specific",
                scope_type="task",
                scope_id=str(task_id),
                strength="medium",
                description="Leave a little review buffer after this task.",
                value_json=json.dumps({"bufferMinutes": 15}),
            )
            unrelated_preference = UserPreference(
                user_id=user_id,
                domain="finance",
                preference_type="budgeting",
                scope_type="global",
                strength="medium",
                description="Use a different finance review cadence.",
                value_json=json.dumps({"cadence": "weekly"}),
            )
            hard_constraint = SchedulingConstraint(
                user_id=user_id,
                constraint_type="forbidden_window",
                target_type="global",
                hard=True,
                description="School pickup is unavailable.",
                value_json=json.dumps(
                    {
                        "startAt": "2026-07-14T15:00:00",
                        "endAt": "2026-07-14T15:30:00",
                    }
                ),
            )
            session.add(focus_preference)
            session.add(task_preference)
            session.add(unrelated_preference)
            session.add(hard_constraint)
            session.commit()
            session.refresh(focus_preference)
            session.refresh(task_preference)
            session.refresh(unrelated_preference)
            session.add(
                PreferenceObservation(
                    user_id=user_id,
                    preference_id=focus_preference.id,
                    event_type="accepted_schedule",
                    evidence_json=json.dumps({"taskId": task_id}),
                    confidence=0.8,
                )
            )
            session.add(
                PreferenceObservation(
                    user_id=user_id,
                    preference_id=unrelated_preference.id,
                    event_type="ignored_schedule",
                    evidence_json=json.dumps({"area": "finance"}),
                    confidence=0.7,
                )
            )
            session.commit()

            context = build_preference_context_for_scope(
                session,
                user_id=user_id,
                task_ids=[task_id],
                category_ids=[category_id],
                task_titles=["Write launch memo"],
                category_names=["Focused Work"],
                prompt="Schedule focused writing time.",
                window_start=datetime(2026, 7, 14, 9, 0),
                window_end=datetime(2026, 7, 14, 17, 0),
            )

        soft_ids = {item["id"] for item in context["softPreferences"]}
        observation_preference_ids = {
            item["preferenceId"] for item in context["observations"]
        }
        self.assertIn(focus_preference.id, soft_ids)
        self.assertIn(task_preference.id, soft_ids)
        self.assertNotIn(unrelated_preference.id, soft_ids)
        self.assertEqual(context["hardConstraints"][0]["id"], hard_constraint.id)
        self.assertEqual(observation_preference_ids, {focus_preference.id})
        self.assertGreaterEqual(context["excludedMemoryCount"], 1)
        self.assertEqual(context["contextVersion"], "preference-memory-v1")

    def test_public_boundary_delegates_with_a_typed_scope(self) -> None:
        calls: list[PreferenceRetrievalScope] = []

        class RecordingPreferenceService:
            def query_context(
                self,
                session: Session,
                *,
                scope: PreferenceRetrievalScope,
                max_soft_preferences: int = 8,
                max_observations: int = 5,
            ) -> dict:
                calls.append(scope)
                return {
                    "contextVersion": "test",
                    "maxSoftPreferences": max_soft_preferences,
                    "maxObservations": max_observations,
                }

        with Session(engine) as session:
            result = build_preference_context_for_scope(
                session,
                user_id="boundary-user",
                task_ids=[42, 42],
                category_ids=[7, 7],
                task_titles=["Write memo"],
                category_names=["Work"],
                prompt="Schedule it.",
                window_start=datetime(2026, 7, 23, 9, 0),
                window_end=datetime(2026, 7, 23, 17, 0),
                max_soft_preferences=3,
                max_observations=2,
                service=RecordingPreferenceService(),
            )

        self.assertEqual(result["contextVersion"], "test")
        self.assertEqual(calls[0].task_ids, [42])
        self.assertEqual(calls[0].category_ids, [7])
        self.assertEqual(calls[0].user_id, "boundary-user")
        self.assertEqual(result["maxSoftPreferences"], 3)
        self.assertEqual(result["maxObservations"], 2)

    def test_explicit_preference_link_participates_in_scoped_retrieval(self) -> None:
        user_id = "linked-retrieval-user"
        task_id = 424242
        with Session(engine) as session:
            preference = UserPreference(
                user_id=user_id,
                domain="finance",
                preference_type="review_style",
                scope_type="global",
                description="Use a conservative review for the linked task.",
                value_json=json.dumps({"preferenceText": "Use a conservative review."}),
            )
            session.add(preference)
            session.commit()
            session.refresh(preference)
            session.add(
                PreferenceLink(
                    user_id=user_id,
                    preference_id=preference.id,
                    target_type="task",
                    target_id=str(task_id),
                )
            )
            session.commit()

            context = build_preference_context_for_scope(
                session,
                user_id=user_id,
                task_ids=[task_id],
                category_ids=[],
                task_titles=["Prepare launch memo"],
                window_start=datetime(2026, 7, 23, 9, 0),
                window_end=datetime(2026, 7, 23, 17, 0),
                prompt="Schedule the linked task.",
            )

        self.assertEqual(
            [item["id"] for item in context["softPreferences"]],
            [preference.id],
        )

    def test_model_projection_bounds_hard_details_and_omits_observations(self) -> None:
        context = {
            "contextVersion": "preference-memory-v1",
            "scope": {"taskIds": [1]},
            "hardConstraints": [
                {
                    "id": f"hard-{index}",
                    "constraintType": "forbidden_window",
                }
                for index in range(12)
            ],
            "softPreferences": [{"id": "soft-1"}],
            "observations": [{"id": "observation-1", "evidence": {"raw": True}}],
            "excludedMemoryCount": 9,
        }

        projected = build_model_preference_context(
            context,
            max_hard_constraint_details=4,
        )

        self.assertEqual(len(projected["hardConstraints"]), 4)
        self.assertEqual(projected["hardConstraintSummary"]["totalCount"], 12)
        self.assertEqual(projected["hardConstraintSummary"]["omittedDetailCount"], 8)
        self.assertEqual(projected["observationSummary"]["availableCount"], 1)
        self.assertNotIn("observations", projected)

    def test_sql_service_is_the_canonical_implementation(self) -> None:
        scope = PreferenceRetrievalScope(
            task_ids=[],
            category_ids=[],
            task_titles=[],
            category_names=[],
            prompt="",
            user_id="empty-preference-scope",
            window_start=datetime(2026, 7, 23, 9, 0),
            window_end=datetime(2026, 7, 23, 17, 0),
        )
        with Session(engine) as session:
            direct = SqlPreferenceApplicationService().query_context(
                session,
                scope=scope,
            )
            facade = build_preference_context_for_scope(
                session,
                user_id=scope.user_id,
                task_ids=[],
                category_ids=[],
                window_start=scope.window_start,
                window_end=scope.window_end,
            )

        self.assertEqual(direct, facade)

    def test_constraint_intervals_compile_only_overlapping_hard_windows(self) -> None:
        context = {
            "hardConstraints": [
                {
                    "id": "inside",
                    "constraintType": "forbidden_window",
                    "hard": True,
                    "value": {
                        "startAt": "2026-07-15T09:00:00",
                        "endAt": "2026-07-15T10:00:00",
                    },
                },
                {
                    "id": "soft",
                    "constraintType": "forbidden_window",
                    "hard": False,
                    "value": {
                        "startAt": "2026-07-15T10:00:00",
                        "endAt": "2026-07-15T11:00:00",
                    },
                },
                {
                    "id": "outside",
                    "constraintType": "forbidden_window",
                    "hard": True,
                    "value": {
                        "startAt": "2026-07-16T09:00:00",
                        "endAt": "2026-07-16T10:00:00",
                    },
                },
            ]
        }

        intervals = constraint_intervals_from_context(
            context,
            window_start=datetime(2026, 7, 15, 8, 0),
            window_end=datetime(2026, 7, 15, 12, 0),
        )

        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].start_at, datetime(2026, 7, 15, 9, 0))
        self.assertEqual(intervals[0].end_at, datetime(2026, 7, 15, 10, 0))
        self.assertLess(intervals[0].block_id, 0)

    def test_inter_task_gap_policy_compiles_soft_default_and_hard_minimum(self) -> None:
        policy = inter_task_gap_policy_from_context(
            {
                "softPreferences": [
                    {
                        "id": "preferred-gap",
                        "description": "I usually like a short break between tasks.",
                        "value": {"bufferMinutes": 15},
                    },
                    {
                        "id": "lower-ranked-gap",
                        "value": {"interTaskGapMinutes": 5},
                    },
                ],
                "hardConstraints": [
                    {
                        "id": "minimum-gap",
                        "constraintType": "inter_task_gap",
                        "hard": True,
                        "description": "Always leave at least 20 minutes.",
                        "value": {"interTaskGapMinutes": 20},
                    }
                ],
            }
        )

        self.assertEqual(policy["softDefaultMinutes"], 15)
        self.assertEqual(policy["hardMinimumMinutes"], 20)
        self.assertEqual(policy["effectiveDefaultMinutes"], 20)
        self.assertEqual([source["id"] for source in policy["sources"]], [
            "preferred-gap",
            "minimum-gap",
        ])

    def test_legacy_strong_availability_gap_is_treated_as_hard_minimum(self) -> None:
        policy = inter_task_gap_policy_from_context(
            {
                "softPreferences": [
                    {
                        "id": "legacy-always-gap",
                        "preferenceType": "availability",
                        "strength": "strong",
                        "description": "Leave 15 minutes between scheduled tasks.",
                        "value": {"interTaskGapMinutes": 15},
                    }
                ],
                "hardConstraints": [],
            }
        )

        self.assertEqual(policy["softDefaultMinutes"], 0)
        self.assertEqual(policy["hardMinimumMinutes"], 15)
        self.assertEqual(policy["sources"][0]["kind"], "legacy_hard_gap_preference")

    def test_newer_conditional_gap_rule_supersedes_older_global_minimum(self) -> None:
        policy = inter_task_gap_policy_from_context(
            {
                "softPreferences": [
                    {
                        "id": "conditional-gap",
                        "preferenceType": "schedule_style",
                        "strength": "strong",
                        "scope": {"type": "global", "id": None},
                        "updatedAt": "2026-07-17T13:00:00",
                        "description": (
                            "Schedule tasks shorter than 45 minutes back-to-back; prefer a "
                            "15-minute buffer between tasks of 45 minutes or longer."
                        ),
                        "value": {
                            "preferredInterTaskGapMinutes": 15,
                            "minimumTaskDurationMinutes": 45,
                            "shorterTaskGapMinutes": 0,
                        },
                    }
                ],
                "hardConstraints": [
                    {
                        "id": "older-global-gap",
                        "constraintType": "inter_task_gap",
                        "target": {"type": "global", "id": None},
                        "hard": True,
                        "updatedAt": "2026-07-16T13:00:00",
                        "description": "Always leave 15 minutes between tasks.",
                        "value": {"interTaskGapMinutes": 15},
                    }
                ],
            }
        )

        self.assertEqual(policy["softDefaultMinutes"], 0)
        self.assertEqual(policy["hardMinimumMinutes"], 0)
        self.assertEqual(
            policy["conditionalGapRules"][0]["minimumTaskDurationMinutes"],
            45,
        )
        self.assertEqual(policy["conditionalGapRules"][0]["gapMinutes"], 15)


if __name__ == "__main__":
    unittest.main()
