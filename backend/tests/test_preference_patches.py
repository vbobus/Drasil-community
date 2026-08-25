import json
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlmodel import Session, select

from app.db.database import create_db_and_tables, engine
from app.db.seed import seed_default_categories
from app.models.preference_memory import (
    PreferenceChangeLog,
    SchedulingConstraint,
    UserPreference,
)
from app.services.agent_kernel.contracts import ApproveReviewAction
from app.services.agent_kernel.review_actions import ReviewActionCoordinator
from app.services.preference_memory import (
    build_preference_context_for_scope,
    constraint_intervals_from_context,
    inter_task_gap_policy_from_context,
)
from app.services.preference_patches import (
    apply_preference_patch,
    build_preference_memory_proposal,
    build_preference_memory_proposal_from_tool_arguments,
    reconcile_superseded_gap_preferences,
)
from app.services.strategist_graph import run_strategist_graph_turn
from app.services.strategist_sessions import create_strategist_session


class PreferencePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        seed_default_categories()

    def _preference_tool_args(
        self,
        prompt: str,
        *,
        memory_intent: str = "explicit_request",
    ) -> dict:
        lowered = prompt.lower()
        if "sleep from 11pm to 7am" in lowered:
            description = "Treat 11 PM to 7 AM as unavailable sleep time."
            value = {
                "sourceText": prompt,
                "extractionKind": "hard_constraint",
                "recurrence": "weekly",
                "daysOfWeek": [0, 1, 2, 3, 4, 5, 6],
                "startTime": "23:00",
                "endTime": "07:00",
                "timezone": "local",
                "rule": "sleep_window",
            }
            return {
                "preference": {
                    "description": description,
                    "domain": "sleep",
                    "preferenceType": "availability",
                    "strength": "hard",
                    "scopeType": "global",
                    "value": value,
                },
                "constraints": [
                    {
                        "description": description,
                        "constraintType": "forbidden_window",
                        "targetType": "global",
                        "hard": True,
                        "penaltyWeight": 1.0,
                        "value": value,
                    }
                ],
                "memory_intent": memory_intent,
                "source_text": prompt,
                "rationale": "The user stated a durable sleep schedule that should affect future scheduling.",
            }
        description = "Do not schedule work before 11 AM."
        value = {
            "sourceText": prompt,
            "extractionKind": "hard_constraint",
            "recurrence": "weekly",
            "daysOfWeek": [0, 1, 2, 3, 4, 5, 6],
            "startTime": "00:00",
            "endTime": "11:00",
            "timezone": "local",
            "rule": "do_not_schedule_before",
        }
        return {
            "preference": {
                "description": description,
                "domain": "scheduling",
                "preferenceType": "availability",
                "strength": "hard",
                "scopeType": "global",
                "value": value,
            },
            "constraints": [
                {
                    "description": description,
                    "constraintType": "forbidden_window",
                    "targetType": "global",
                    "hard": True,
                    "penaltyWeight": 1.0,
                    "value": value,
                }
            ],
            "memory_intent": memory_intent,
            "source_text": prompt,
            "rationale": "The user asked me to remember a durable scheduling constraint.",
        }

    def _run_preference_tool_turn(
        self,
        *,
        session: Session,
        prompt: str,
        strategist_session_id: str,
        memory_intent: str = "explicit_request",
    ) -> dict:
        def fake_tool_loop(**kwargs):
            transition_args = {"disposition": "continue", "patches": []}
            transition = kwargs["tool_executor"](
                "set_goal_transition",
                transition_args,
                categories=kwargs["categories"],
                nodes=kwargs["nodes"],
                focus_node=kwargs["focus_node"],
                active_category=kwargs["active_category"],
                staged_node_ids=kwargs["staged_node_ids"],
                working_set=kwargs.get("working_set"),
                today_override=kwargs.get("today_override"),
            )
            self.assertNotIn("error", transition, transition)
            kwargs["tool_trace"].append(
                {
                    "name": "set_goal_transition",
                    "arguments": transition_args,
                    "result": transition,
                    "duration_ms": 1.0,
                }
            )
            tool_args = self._preference_tool_args(prompt, memory_intent=memory_intent)
            result = kwargs["tool_executor"](
                "submit_preference_plan",
                tool_args,
                categories=kwargs["categories"],
                nodes=kwargs["nodes"],
                focus_node=kwargs["focus_node"],
                active_category=kwargs["active_category"],
                staged_node_ids=kwargs["staged_node_ids"],
                working_set=kwargs.get("working_set"),
                today_override=kwargs.get("today_override"),
            )
            self.assertNotIn("error", result, result)
            kwargs["tool_trace"].append(
                {
                    "name": "submit_preference_plan",
                    "arguments": tool_args,
                    "result": result,
                    "duration_ms": 1.0,
                }
            )
            return (
                {
                    "summary": (
                        "This sounds like a scheduling preference I can remember, but I need "
                        "your approval before saving it."
                    ),
                    "action_proposal": None,
                    "metadata_proposal": None,
                    "branch_proposal": None,
                    "preference_memory_proposal": result["semantic_plan_input"][
                        "payload"
                    ],
                    "clarification": None,
                },
                None,
            )

        with patch(
            "app.services.agent_kernel.runtime.provider_transport_readiness",
            return_value={
                "provider": "codex-cli",
                "checked": True,
                "passed": True,
                "source": "test",
            },
        ), patch(
            "app.services.agent_kernel.runtime.invoke_codex_tool_loop",
            side_effect=fake_tool_loop,
        ):
            return run_strategist_graph_turn(
                session=session,
                prompt=prompt,
                ui_event=None,
                active_category_id=None,
                focus_node_id=None,
                staged_node_ids=[],
                conversation_history=[],
                strategist_session_id=strategist_session_id,
                include_diagnostics=True,
                control_plane="agent-kernel",
            )

    def test_preference_patch_apply_creates_memory_and_audit_log(self) -> None:
        proposal = build_preference_memory_proposal(
            prompt="Remember I don't schedule tasks before 10am."
        )
        self.assertIsNotNone(proposal)
        preference_patch = proposal["preference_patch"]
        for operation in preference_patch["operations"]:
            payload_key = "preference" if operation["op"] == "create_preference" else "constraint"
            operation[payload_key]["userId"] = "preference-patch-test"

        with Session(engine) as session:
            result = apply_preference_patch(
                session,
                preference_patch=preference_patch,
                actor="test_user_approved",
            )
            preference_id = result["createdPreferenceIds"][0]
            constraint_id = result["createdConstraintIds"][0]

            preference = session.get(UserPreference, preference_id)
            constraint = session.get(SchedulingConstraint, constraint_id)
            change_logs = session.exec(
                select(PreferenceChangeLog).where(
                    PreferenceChangeLog.record_id.in_([preference_id, constraint_id])
                )
            ).all()

            context = build_preference_context_for_scope(
                session,
                task_ids=[101],
                category_ids=[202],
                task_titles=["Write strategy memo"],
                category_names=["Work"],
                user_id="preference-patch-test",
                window_start=datetime(2026, 7, 20, 9, 0),
                window_end=datetime(2026, 7, 20, 11, 0),
            )
            intervals = constraint_intervals_from_context(
                context,
                window_start=datetime(2026, 7, 20, 9, 0),
                window_end=datetime(2026, 7, 20, 11, 0),
            )

        self.assertIsNotNone(preference)
        self.assertIsNotNone(constraint)
        self.assertEqual(constraint.source_preference_id, preference_id)
        self.assertEqual(len(change_logs), 2)
        self.assertEqual(intervals[0].start_at, datetime(2026, 7, 20, 9, 0))
        self.assertEqual(intervals[0].end_at, datetime(2026, 7, 20, 10, 0))

    def test_usual_inter_task_gap_is_proposed_saved_and_compiled(self) -> None:
        compiled = build_preference_memory_proposal_from_tool_arguments(
            {
                "preference": {
                    "description": "Usually leave 15 minutes between scheduled tasks.",
                    "domain": "scheduling",
                    "preferenceType": "schedule_style",
                    "strength": "medium",
                    "scopeType": "global",
                    "value": {"preferredInterTaskGapMinutes": 15},
                },
                "memory_intent": "explicit_request",
                "source_text": "Remember that I usually like 15 minutes between tasks.",
            }
        )
        proposal = compiled["preference_memory_proposal"]
        preference_patch = proposal["preference_patch"]

        self.assertTrue(compiled["compiled"])
        self.assertEqual(proposal["preference_patch_status"], "valid")
        self.assertEqual(
            preference_patch["operations"][0]["preference"]["value"]["interTaskGapMinutes"],
            15,
        )

        with Session(engine) as session:
            apply_preference_patch(session, preference_patch=preference_patch)
            context = build_preference_context_for_scope(
                session,
                task_ids=[101, 102],
                category_ids=[202],
                task_titles=["First task", "Second task"],
                category_names=["Work"],
                prompt="Schedule these tasks.",
                window_start=datetime(2026, 7, 20, 9, 0),
                window_end=datetime(2026, 7, 20, 12, 0),
            )
            saved_preferences = session.exec(
                select(UserPreference).where(
                    UserPreference.description
                    == "Usually leave 15 minutes between scheduled tasks."
                )
            ).all()
            for saved_preference in saved_preferences:
                session.delete(saved_preference)
            session.commit()

        policy = inter_task_gap_policy_from_context(context)
        self.assertEqual(policy["softDefaultMinutes"], 15)
        self.assertEqual(policy["hardMinimumMinutes"], 0)

    def test_hard_inter_task_gap_compiler_creates_enforceable_constraint(self) -> None:
        compiled = build_preference_memory_proposal_from_tool_arguments(
            {
                "preference": {
                    "description": "Always leave at least 15 minutes between scheduled tasks.",
                    "domain": "scheduling",
                    "preferenceType": "availability",
                    "strength": "strong",
                    "enforcement": "hard_minimum",
                    "scopeType": "global",
                    "value": {"interTaskGapMinutes": 15},
                },
                "source_text": "Always give 15 minutes gap between tasks.",
            }
        )
        operations = compiled["preference_memory_proposal"]["preference_patch"]["operations"]

        self.assertEqual(operations[0]["preference"]["preferenceType"], "inter_task_gap")
        self.assertEqual(operations[0]["preference"]["strength"], "hard")
        self.assertEqual(operations[1]["op"], "create_constraint")
        self.assertEqual(operations[1]["constraint"]["constraintType"], "inter_task_gap")
        self.assertTrue(operations[1]["constraint"]["hard"])
        self.assertEqual(
            operations[1]["constraint"]["value"]["interTaskGapMinutes"],
            15,
        )

    def test_conditional_gap_compiler_structures_duration_threshold(self) -> None:
        compiled = build_preference_memory_proposal_from_tool_arguments(
            {
                "preference": {
                    "description": (
                        "Schedule tasks shorter than 45 minutes back-to-back; prefer a "
                        "15-minute buffer between tasks of 45 minutes or longer."
                    ),
                    "domain": "scheduling",
                    "preferenceType": "inter_task_gap",
                    "strength": "medium",
                    "scopeType": "global",
                    "value": {"interTaskGapMinutes": 15},
                },
                "source_text": (
                    "Only use my 15 minute gap for tasks that take at least 45 minutes."
                ),
            }
        )
        value = compiled["preference_memory_proposal"]["preference_patch"]["operations"][0][
            "preference"
        ]["value"]
        preference = compiled["preference_memory_proposal"]["preference_patch"]["operations"][0][
            "preference"
        ]

        self.assertEqual(preference["preferenceType"], "inter_task_gap")
        self.assertEqual(value["interTaskGapMinutes"], 15)
        self.assertEqual(value["minimumTaskDurationMinutes"], 45)
        self.assertEqual(value["shorterTaskGapMinutes"], 0)

    def test_reconciliation_canonicalizes_latest_alias_and_deactivates_older_rule(self) -> None:
        scope_id = "preexisting-gap-reconciliation"
        with Session(engine) as session:
            older = UserPreference(
                domain="scheduling",
                preference_type="availability",
                scope_type="task",
                scope_id=scope_id,
                strength="strong",
                description="Leave 15 minutes between scheduled tasks.",
                value_json=json.dumps({"interTaskGapMinutes": 15}),
                updated_at=datetime(2026, 7, 13, 12, 0),
            )
            latest = UserPreference(
                domain="scheduling",
                preference_type="schedule_style",
                scope_type="task",
                scope_id=scope_id,
                strength="strong",
                description=(
                    "Schedule tasks shorter than 45 minutes back-to-back; prefer a "
                    "15-minute buffer between tasks of 45 minutes or longer."
                ),
                value_json=json.dumps(
                    {
                        "kind": "scalar",
                        "value": json.dumps(
                            {
                                "preferredInterTaskGapMinutes": 15,
                                "minimumTaskDurationMinutes": 45,
                            }
                        ),
                    }
                ),
                updated_at=datetime(2026, 7, 17, 12, 0),
            )
            session.add_all([older, latest])
            session.commit()

            result = reconcile_superseded_gap_preferences(session)
            session.commit()
            session.refresh(older)
            session.refresh(latest)

            self.assertEqual(result["canonicalizedPreferences"], 1)
            self.assertEqual(result["deactivatedPreferences"], 1)
            self.assertFalse(older.active)
            self.assertTrue(latest.active)
            self.assertEqual(latest.preference_type, "inter_task_gap")
            value = json.loads(latest.value_json)
            self.assertEqual(value["interTaskGapMinutes"], 15)
            self.assertEqual(value["shorterTaskGapMinutes"], 0)

            session.delete(older)
            session.delete(latest)
            session.commit()

    def test_approved_gap_revision_deactivates_older_same_scope_rule(self) -> None:
        scope_id = "conditional-gap-regression-task"
        with Session(engine) as session:
            older_preference = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="task",
                scope_id=scope_id,
                strength="hard",
                description="Always leave 15 minutes between tasks.",
                value_json=json.dumps({"interTaskGapMinutes": 15}),
            )
            session.add(older_preference)
            session.flush()
            older_constraint = SchedulingConstraint(
                constraint_type="inter_task_gap",
                target_type="task",
                target_id=scope_id,
                hard=True,
                source_preference_id=older_preference.id,
                description="Always leave 15 minutes between tasks.",
                value_json=json.dumps({"interTaskGapMinutes": 15}),
            )
            session.add(older_constraint)
            session.commit()
            compiled = build_preference_memory_proposal_from_tool_arguments(
                {
                    "preference": {
                        "description": (
                            "Tasks shorter than 45 minutes can be back-to-back; prefer a "
                            "15-minute gap for tasks of 45 minutes or longer."
                        ),
                        "domain": "scheduling",
                        "preferenceType": "inter_task_gap",
                        "strength": "medium",
                        "scopeType": "task",
                        "scopeId": scope_id,
                        "value": {"interTaskGapMinutes": 15},
                    }
                }
            )
            apply_preference_patch(
                session,
                preference_patch=compiled["preference_memory_proposal"][
                    "preference_patch"
                ],
            )

            session.refresh(older_preference)
            session.refresh(older_constraint)
            self.assertFalse(older_preference.active)
            self.assertFalse(older_constraint.active)

            created = session.exec(
                select(UserPreference)
                .where(UserPreference.scope_id == scope_id)
                .where(UserPreference.active == True)
            ).all()
            self.assertEqual(len(created), 1)
            for item in session.exec(
                select(SchedulingConstraint).where(
                    SchedulingConstraint.target_id == scope_id
                )
            ).all():
                session.delete(item)
            for item in session.exec(
                select(UserPreference).where(UserPreference.scope_id == scope_id)
            ).all():
                session.delete(item)
            session.commit()

    def test_reconciliation_keeps_disjoint_gap_rules_as_complementary(self) -> None:
        scope_id = "complementary-gap-rules"
        with Session(engine) as session:
            shorter = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="task",
                scope_id=scope_id,
                description="Keep a short transition between shorter tasks.",
                value_json=json.dumps(
                    {
                        "interTaskGapMinutes": 5,
                        "maximumTaskDurationMinutes": 44,
                    }
                ),
                updated_at=datetime(2026, 7, 13, 12, 0),
            )
            longer = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="task",
                scope_id=scope_id,
                description="Keep a longer transition between longer tasks.",
                value_json=json.dumps(
                    {
                        "interTaskGapMinutes": 15,
                        "minimumTaskDurationMinutes": 45,
                    }
                ),
                updated_at=datetime(2026, 7, 17, 12, 0),
            )
            session.add_all([shorter, longer])
            session.commit()

            result = reconcile_superseded_gap_preferences(session)
            session.commit()
            session.refresh(shorter)
            session.refresh(longer)

            self.assertEqual(result["deactivatedPreferences"], 0)
            self.assertTrue(shorter.active)
            self.assertTrue(longer.active)

            session.delete(shorter)
            session.delete(longer)
            session.commit()

    def test_inferred_preference_prompt_builds_reviewable_memory_patch(self) -> None:
        proposal = build_preference_memory_proposal(
            prompt="I sleep from 11pm to 7am."
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["memory_intent"], "inferred_preference")
        self.assertEqual(proposal["rationale"], "This will guide future schedules.")
        self.assertIn(
            "Treat 11 PM to 7 AM as unavailable sleep time.",
            proposal["preview"],
        )
        constraint = proposal["preference_patch"]["operations"][1]["constraint"]
        self.assertEqual(constraint["value"]["startTime"], "23:00")
        self.assertEqual(constraint["value"]["endTime"], "07:00")

    def test_one_off_scheduling_prompt_does_not_build_memory_patch(self) -> None:
        proposal = build_preference_memory_proposal(prompt="Can you schedule this task before 10am?")
        imperative_proposal = build_preference_memory_proposal(
            prompt="Don't schedule this task before 10am."
        )

        self.assertIsNone(proposal)
        self.assertIsNone(imperative_proposal)

    def test_strategist_memory_prompt_requires_approval_before_saving(self) -> None:
        with Session(engine) as session:
            strategist_session = create_strategist_session(
                session,
                prompt="Remember my scheduling preference",
                provider="test",
                provider_label="Test Strategist",
                model_name=None,
            )
            result = self._run_preference_tool_turn(
                session=session,
                prompt="Remember I don't schedule tasks before 11am.",
                strategist_session_id=strategist_session.id,
            )
            review = result["response"]["review_document"]
            self.assertIsInstance(review, dict, result)
            saved_preferences_before_approval = session.exec(
                select(UserPreference).where(UserPreference.description.contains("before 11"))
            ).all()

            approve_control = next(
                control
                for control in review["controls"]
                if control["action"] == "approve"
            )
            approval = ReviewActionCoordinator(session).execute(
                authenticated_session_id=strategist_session.id,
                action=ApproveReviewAction.model_validate(approve_control),
            )
            saved_preferences_after_approval = session.exec(
                select(UserPreference).where(UserPreference.description.contains("before 11"))
            ).all()
            saved_constraints_after_approval = session.exec(
                select(SchedulingConstraint).where(
                    SchedulingConstraint.description.contains("before 11")
                )
            ).all()
            created_record_ids = [
                *[preference.id for preference in saved_preferences_after_approval],
                *[constraint.id for constraint in saved_constraints_after_approval],
            ]
            if created_record_ids:
                change_logs = session.exec(
                    select(PreferenceChangeLog).where(
                        PreferenceChangeLog.record_id.in_(created_record_ids)
                    )
                ).all()
                for change_log in change_logs:
                    session.delete(change_log)
            for constraint in saved_constraints_after_approval:
                session.delete(constraint)
            for preference in saved_preferences_after_approval:
                session.delete(preference)
            session.commit()

        self.assertEqual(result["diagnostics"]["control_plane"], "agent-kernel")
        self.assertTrue(
            any(
                call.get("name") == "submit_preference_plan"
                for call in result["diagnostics"]["tool_trace"]
            )
        )
        self.assertEqual(result["response"]["mode"], "proposal")
        self.assertIsInstance(review, dict)
        self.assertEqual(saved_preferences_before_approval, [])
        self.assertIn("Review", result["response"]["summary"])
        self.assertIn("before applying", result["response"]["summary"])
        self.assertEqual(approval.status, "ok", approval)
        self.assertEqual(approval.result.status, "execution_complete")
        self.assertEqual(len(saved_preferences_after_approval), 1)
        self.assertEqual(len(saved_constraints_after_approval), 1)

    def test_strategist_infers_memory_proposal_without_remember_keyword(self) -> None:
        with Session(engine) as session:
            strategist_session = create_strategist_session(
                session,
                prompt="My sleep schedule",
                provider="test",
                provider_label="Test Strategist",
                model_name=None,
            )
            result = self._run_preference_tool_turn(
                session=session,
                prompt="I sleep from 11pm to 7am.",
                strategist_session_id=strategist_session.id,
                memory_intent="inferred_preference",
            )
            review = result["response"]["review_document"]
            self.assertIsInstance(review, dict, result)
            saved_preferences_before_approval = session.exec(
                select(UserPreference).where(UserPreference.description.contains("sleep time"))
            ).all()

        self.assertEqual(result["diagnostics"]["control_plane"], "agent-kernel")
        self.assertTrue(
            any(
                call.get("name") == "submit_preference_plan"
                for call in result["diagnostics"]["tool_trace"]
            )
        )
        self.assertEqual(result["response"]["mode"], "proposal")
        self.assertIsInstance(review, dict)
        self.assertTrue(
            any(control["action"] == "approve" for control in review["controls"])
        )
        self.assertEqual(saved_preferences_before_approval, [])
        self.assertIn("Review", result["response"]["summary"])
        self.assertIn("before applying", result["response"]["summary"])


if __name__ == "__main__":
    unittest.main()
