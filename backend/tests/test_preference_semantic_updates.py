from __future__ import annotations

import json
import unittest
from datetime import datetime

from sqlmodel import Session, SQLModel, create_engine

from app.models.preference_memory import UserPreference
from app.services.agent_kernel.change_compilation import CompatibilityProposalAdapter
from app.services.agent_kernel.contracts import (
    ChangeSet,
    PreferenceSupersedeOperation,
    Provenance,
)
from app.services.agent_kernel.presentation import _preference_value_text
from app.services.agent_kernel.runtime import (
    _new_preference_review_label,
    _review_entity_titles,
)
from app.services.preference_patches import (
    build_preference_memory_proposal_from_tool_arguments,
)
from app.services.preference_values import decode_preference_value


class PreferenceSemanticUpdateTests(unittest.TestCase):
    def _engine(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
        return engine

    @staticmethod
    def _compile(prompt: str, *, preference_type: str = "schedule_style") -> dict:
        return build_preference_memory_proposal_from_tool_arguments(
            {
                "preference": {
                    "description": prompt,
                    "domain": "scheduling",
                    "preferenceType": preference_type,
                    "strength": "medium",
                    "scopeType": "global",
                },
                "source_text": prompt,
            },
            original_user_request=prompt,
        )

    @staticmethod
    def _operation(compiled: dict, session: Session):
        proposal = compiled["preference_memory_proposal"]
        plan = CompatibilityProposalAdapter().adapt_preference_proposal(
            proposal=proposal,
            provenance=Provenance(
                source_kind="user",
                source_ref="turn:gap-update",
                source_text="I'd like to add 20 minute gap between tasks.",
            ),
            timezone_name="America/Vancouver",
            session=session,
        )
        return [
            operation
            for clause in plan.clauses
            for operation in clause.expected_operations
        ][0]

    def test_text_only_gap_is_structured_even_when_model_uses_generic_type(
        self,
    ) -> None:
        prompt = "I'd like to add 20 minute gap between tasks."
        compiled = self._compile(prompt)
        preference = compiled["preference_memory_proposal"]["preference_patch"][
            "operations"
        ][0]["preference"]

        self.assertTrue(compiled["compiled"])
        self.assertEqual(preference["preferenceType"], "inter_task_gap")
        self.assertEqual(preference["domain"], "scheduling")
        self.assertEqual(preference["value"]["interTaskGapMinutes"], 20)
        self.assertNotIn("appliesToAllTasks", preference["value"])

    def test_amount_only_gap_update_preserves_existing_applicability(self) -> None:
        engine = self._engine()
        with Session(engine) as session:
            current = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="global",
                strength="medium",
                description=(
                    "Prefer a 15-minute gap between tasks lasting at least "
                    "45 minutes."
                ),
                value_json=json.dumps(
                    {
                        "interTaskGapMinutes": 15,
                        "minimumTaskDurationMinutes": 45,
                        "shorterTaskGapMinutes": 0,
                    }
                ),
            )
            session.add(current)
            session.commit()
            session.refresh(current)

            operation = self._operation(
                self._compile("I'd like to add 20 minute gap between tasks."),
                session,
            )

        self.assertIsInstance(operation, PreferenceSupersedeOperation)
        self.assertEqual(
            operation.preference_ref.entity_id,
            f"preference:{current.id}",
        )
        value = decode_preference_value(
            operation.value.model_dump(mode="json", by_alias=True)
        )
        self.assertEqual(value["interTaskGapMinutes"], 20)
        self.assertEqual(value["minimumTaskDurationMinutes"], 45)
        self.assertEqual(value["shorterTaskGapMinutes"], 0)
        self.assertEqual(
            _preference_value_text(operation.value),
            (
                "20-minute gap between tasks lasting at least 45 minutes; "
                "shorter tasks can remain back-to-back"
            ),
        )

    def test_explicit_every_task_gap_replaces_duration_condition(self) -> None:
        engine = self._engine()
        with Session(engine) as session:
            session.add(
                UserPreference(
                    domain="scheduling",
                    preference_type="inter_task_gap",
                    scope_type="global",
                    strength="medium",
                    description=(
                        "Prefer a 15-minute gap between tasks lasting at least "
                        "45 minutes."
                    ),
                    value_json=json.dumps(
                        {
                            "interTaskGapMinutes": 15,
                            "minimumTaskDurationMinutes": 45,
                            "shorterTaskGapMinutes": 0,
                        }
                    ),
                )
            )
            session.commit()

            operation = self._operation(
                self._compile(
                    "Use a 20-minute gap between every task regardless of duration."
                ),
                session,
            )

        value = decode_preference_value(
            operation.value.model_dump(mode="json", by_alias=True)
        )
        self.assertEqual(value["interTaskGapMinutes"], 20)
        self.assertTrue(value["appliesToAllTasks"])
        self.assertNotIn("minimumTaskDurationMinutes", value)
        self.assertNotIn("shorterTaskGapMinutes", value)
        self.assertEqual(
            _preference_value_text(operation.value),
            "20-minute gap between tasks",
        )

    def test_open_ended_preference_key_is_not_forced_into_fixed_taxonomy(
        self,
    ) -> None:
        prompt = "Alternate creative and administrative work when possible."
        compiled = self._compile(
            prompt,
            preference_type="work_mode_rotation",
        )
        preference = compiled["preference_memory_proposal"]["preference_patch"][
            "operations"
        ][0]["preference"]

        self.assertEqual(preference["preferenceType"], "work_mode_rotation")
        self.assertEqual(preference["value"]["preferenceText"], prompt)
        self.assertNotIn("interTaskGapMinutes", preference["value"])

    def test_new_preference_review_uses_meaning_instead_of_generic_metadata(
        self,
    ) -> None:
        engine = self._engine()
        with Session(engine) as session:
            gap_operation = self._operation(
                self._compile("I'd like to add 20 minute gap between tasks."),
                session,
            )
            open_ended_operation = self._operation(
                self._compile(
                    "Alternate creative and administrative work when possible.",
                    preference_type="work_mode_rotation",
                ),
                session,
            )

        self.assertEqual(
            _new_preference_review_label(gap_operation),
            "Gap between tasks",
        )
        self.assertEqual(
            _new_preference_review_label(open_ended_operation),
            "Alternate creative and administrative work when possible.",
        )

    def test_supersede_review_keeps_old_and_projected_new_labels_distinct(
        self,
    ) -> None:
        engine = self._engine()
        old_description = (
            "Schedule tasks shorter than 45 minutes back-to-back; prefer a "
            "15-minute buffer between tasks of 45 minutes or longer."
        )
        with Session(engine) as session:
            current = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="global",
                strength="medium",
                description=old_description,
                value_json=json.dumps(
                    {
                        "interTaskGapMinutes": 15,
                        "minimumTaskDurationMinutes": 45,
                        "shorterTaskGapMinutes": 0,
                    }
                ),
            )
            session.add(current)
            session.commit()
            session.refresh(current)
            operation = self._operation(
                self._compile("I'd like to add 20 minute gap between tasks."),
                session,
            )
            change_set = ChangeSet(
                change_set_id="changeset:gap-labels",
                goal_id="goal:gap-labels",
                operations=[operation],
                idempotency_key="idem:gap-labels",
                correlation_id="correlation:gap-labels",
                causation_id="causation:gap-labels",
            )

            titles = _review_entity_titles(
                session,
                change_set=change_set,
                nodes=[],
                categories=[],
            )

        self.assertEqual(
            titles[operation.preference_ref.entity_id],
            old_description,
        )
        self.assertEqual(
            titles[f"projected:preference:{operation.operation_id}"],
            "Gap between tasks",
        )

    def test_legacy_text_only_gap_is_recognized_as_same_semantic_subject(
        self,
    ) -> None:
        engine = self._engine()
        with Session(engine) as session:
            legacy = UserPreference(
                domain="scheduling",
                preference_type="schedule_style",
                scope_type="global",
                strength="medium",
                description="Leave 15 minutes between tasks.",
                value_json=json.dumps(
                    {"preferenceText": "Leave 15 minutes between tasks."}
                ),
            )
            session.add(legacy)
            session.commit()
            session.refresh(legacy)

            operation = self._operation(
                self._compile("I'd like to add 20 minute gap between tasks."),
                session,
            )

        self.assertIsInstance(operation, PreferenceSupersedeOperation)
        self.assertEqual(
            operation.preference_ref.entity_id,
            f"preference:{legacy.id}",
        )

    def test_global_update_uses_global_rule_before_newer_scoped_copy(self) -> None:
        engine = self._engine()
        with Session(engine) as session:
            global_rule = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="global",
                description="Use a 15-minute gap for tasks lasting 45 minutes.",
                value_json=json.dumps(
                    {
                        "interTaskGapMinutes": 15,
                        "minimumTaskDurationMinutes": 45,
                        "shorterTaskGapMinutes": 0,
                    }
                ),
                updated_at=datetime(2026, 7, 1, 12, 0),
            )
            newer_scoped_copy = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="category",
                scope_id="home",
                description="Use a 10-minute gap for Home tasks lasting 60 minutes.",
                value_json=json.dumps(
                    {
                        "interTaskGapMinutes": 10,
                        "minimumTaskDurationMinutes": 60,
                        "shorterTaskGapMinutes": 0,
                    }
                ),
                updated_at=datetime(2026, 7, 2, 12, 0),
            )
            session.add_all([global_rule, newer_scoped_copy])
            session.commit()
            session.refresh(global_rule)

            operation = self._operation(
                self._compile("I'd like to add 20 minute gap between tasks."),
                session,
            )

        value = decode_preference_value(
            operation.value.model_dump(mode="json", by_alias=True)
        )
        self.assertEqual(
            operation.preference_ref.entity_id,
            f"preference:{global_rule.id}",
        )
        self.assertEqual(value["minimumTaskDurationMinutes"], 45)


if __name__ == "__main__":
    unittest.main()
