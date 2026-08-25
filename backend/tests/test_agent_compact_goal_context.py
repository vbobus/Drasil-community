from __future__ import annotations

import json
import unittest

from sqlmodel import Session, SQLModel, create_engine

from app.models.strategist_agent import StrategistAgentEvent
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.compact_goal_context import (
    MAX_COMPACT_GOAL_CONTEXT_BYTES,
    build_compact_goal_context,
    evidence_handle_for_event,
    resolve_evidence_handle,
)
from app.services.agent_kernel.contracts import GoalState
from app.services.agent_kernel.goal_state_coordinator import GoalStateCoordinator
from app.services.agent_kernel.runtime import (
    _build_kernel_context_packet,
    _prune_context_packet_for_fresh_model_turn,
    _prune_context_packet_for_pending_review_correction,
    _prune_context_packet_for_simple_new_task_schedule,
)


def _encoded_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


class AgentCompactGoalContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all(
                [
                    StrategistChatSession(id="session:one"),
                    StrategistChatSession(id="session:two"),
                ]
            )
            session.commit()

    def test_context_is_bounded_and_has_one_authoritative_surface(self) -> None:
        goal = GoalState(
            goal_id="goal:compact",
            original_request="Prepare the application.",
            correlation_id="corr:compact",
            causation_id="turn:compact",
        )
        context = build_compact_goal_context(
            goal_state=goal,
            latest_user_turn="Continue with the checklist.",
            runtime_context={
                "active_category": {"id": 1, "name": "Work"},
                "focus_task": {"id": 55, "title": "Unrelated stale focus"},
            },
            pending_proposal={
                "proposal_id": "proposal:1",
                "goal_id": goal.goal_id,
                "status": "pending",
                "summary": "Review the application checklist." + ("x" * 1000),
                "payload": {"private": "must not be copied"},
            },
            recent_events=[],
        )

        encoded = json.dumps(context, sort_keys=True)
        self.assertLessEqual(len(encoded.encode("utf-8")), MAX_COMPACT_GOAL_CONTEXT_BYTES)
        self.assertEqual(context["latestUserTurn"], "Continue with the checklist.")
        self.assertNotIn("visible_transcript", context)
        self.assertNotIn("session_memory", context)
        self.assertNotIn("tool_registry", context)
        self.assertNotIn("contract", context)
        self.assertNotIn("goalId", context["goal"])
        self.assertNotIn("revision", context["goal"])
        self.assertNotIn("payload", context["pendingReview"])
        self.assertLessEqual(len(context["pendingReview"]["summary"]), 240)
        self.assertNotIn("Unrelated stale focus", encoded)

    def test_oversized_pending_semantics_are_compacted_without_aborting_turn(self) -> None:
        goal = GoalState(
            goal_id="goal:oversized",
            original_request="Keep the recurring plan and explain its conflicts.",
            correlation_id="corr:oversized",
            causation_id="turn:oversized",
        )
        semantic_contract = {
            "proposal_family": "branch",
            "recurring_schedule": {
                "task_title": "Team call",
                "frequency": "weekly",
                "days_of_week": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                ],
                "preferred_start_time": "19:00",
                "duration_minutes": 60,
            },
            "structural_operations": [
                {
                    "kind": "task.create",
                    "temp_id": f"task:{index}",
                    "title": "x" * 2_000,
                    "notes": "y" * 2_000,
                }
                for index in range(100)
            ],
        }

        context = build_compact_goal_context(
            goal_state=goal,
            latest_user_turn="What exactly conflicts?",
            runtime_context={},
            pending_proposal={
                "proposal_id": "proposal:oversized",
                "goal_id": goal.goal_id,
                "status": "pending",
                "semantic_contract": semantic_contract,
            },
            recent_events=[],
        )

        self.assertLessEqual(_encoded_size(context), MAX_COMPACT_GOAL_CONTEXT_BYTES)
        self.assertTrue(context["contextTruncated"])
        recurrence = context["pendingReview"]["semanticContract"][
            "recurring_schedule"
        ]
        self.assertEqual(recurrence["task_title"], "Team call")
        self.assertEqual(
            recurrence["days_of_week"],
            ["monday", "tuesday", "wednesday", "thursday", "friday"],
        )

    def test_provider_packet_does_not_repeat_bulk_context_surfaces(self) -> None:
        goal = GoalState(
            goal_id="goal:packet",
            original_request="Prepare the application.",
            correlation_id="corr:packet",
            causation_id="turn:packet",
        )
        packet = _build_kernel_context_packet(
            active_goal=goal,
            latest_user_turn="Add a checklist.",
            runtime_context={"goal_state": {"large": "internal"}},
            conversation_history=[{"role": "user", "content": "old transcript"}],
            pending_proposal=None,
            recent_events=[],
            session_memory={"large": "legacy memory"},
        )

        self.assertIn("goalContext", packet)
        self.assertEqual(packet["goalContext"]["latestUserTurn"], "Add a checklist.")
        for obsolete in (
            "contract",
            "tool_registry",
            "visible_transcript",
            "session_memory",
            "pending_proposal",
            "recent_events",
        ):
            self.assertNotIn(obsolete, packet)
        self.assertNotIn("goal_state", packet["runtime_context"])
        self.assertNotIn("goalId", packet["goalContext"]["goal"])
        self.assertNotIn("revision", packet["goalContext"]["goal"])

    def test_pending_review_is_goal_bound_and_scalar_targets_are_rejected(self) -> None:
        goal = GoalState(
            goal_id="goal:current",
            original_request="Prepare the application.",
            correlation_id="corr:current",
            causation_id="turn:current",
        )
        wrong_goal = build_compact_goal_context(
            goal_state=goal,
            latest_user_turn="Continue.",
            runtime_context={},
            pending_proposal={
                "proposal_id": "proposal:old",
                "goal_id": "goal:old",
                "targetTaskIds": 42,
            },
            recent_events=[],
        )
        self.assertIsNone(wrong_goal["pendingReview"])

        malformed_targets = build_compact_goal_context(
            goal_state=goal,
            latest_user_turn="Continue.",
            runtime_context={},
            pending_proposal={
                "proposal_id": "proposal:current",
                "goal_id": goal.goal_id,
                "targetTaskIds": 42,
            },
            recent_events=[],
        )
        self.assertEqual(malformed_targets["pendingReview"]["targetTaskIds"], [])

    def test_simple_schedule_packet_keeps_only_category_routing_context(self) -> None:
        packet = {
            "capability_catalog": {"capabilities": [{"id": "large"}]},
            "runtime_context": {
                "today": "2026-07-23",
                "local_date_time": "2026-07-23T12:00:00",
                "time_zone": "America/Vancouver",
                "active_category": {"category_id": 1, "name": "Work"},
                "working_set": [
                    {"task_id": index, "title": "x" * 100}
                    for index in range(20)
                ],
                "task_scope_index": {
                    "categories": [
                        {
                            "category_id": 1,
                            "name": "Work",
                            "is_active": True,
                            "roots": [{"task_id": 1, "title": "Large branch"}],
                        },
                        {
                            "category_id": 3,
                            "name": "Home",
                            "is_active": False,
                            "roots": [{"task_id": 42, "title": "Cooking"}],
                        },
                    ]
                },
            },
            "goalContext": {"latestUserTurn": "Create Cooking tomorrow evening."},
        }

        _prune_context_packet_for_simple_new_task_schedule(packet)

        self.assertNotIn("capability_catalog", packet)
        self.assertNotIn("working_set", packet["runtime_context"])
        self.assertNotIn("task_scope_index", packet["runtime_context"])
        self.assertEqual(
            packet["runtime_context"]["category_catalog"],
            [
                {"category_id": 1, "name": "Work", "is_active": True},
                {"category_id": 3, "name": "Home", "is_active": False},
            ],
        )
        self.assertLess(len(json.dumps(packet)), 1200)

    def test_fresh_model_turn_uses_tools_instead_of_prefetched_workspace_dump(self) -> None:
        packet = {
            "capability_catalog": {"capabilities": [{"id": "large"}]},
            "runtime_context": {
                "today": "2026-08-07",
                "local_date_time": "2026-08-07T13:25:00",
                "time_zone": "America/Vancouver",
                "active_category": {"category_id": 1, "name": "Work"},
                "task_tree_outline": "x" * 20000,
                "working_set": [{"task_id": 10, "title": "stale"}],
                "task_scope_index": {
                    "categories": [
                        {"category_id": 1, "name": "Work", "is_active": True},
                        {"category_id": 3, "name": "Home", "is_active": False},
                    ],
                    "tasks": [{"task_id": index, "title": "x" * 100} for index in range(50)],
                },
            },
            "goalContext": {"latestUserTurn": "Handle the complete request."},
        }

        _prune_context_packet_for_fresh_model_turn(packet)

        self.assertNotIn("capability_catalog", packet)
        self.assertNotIn("task_tree_outline", packet["runtime_context"])
        self.assertNotIn("task_scope_index", packet["runtime_context"])
        self.assertNotIn("working_set", packet["runtime_context"])
        self.assertEqual(
            packet["runtime_context"]["category_catalog"],
            [
                {"category_id": 1, "name": "Work", "is_active": True},
                {"category_id": 3, "name": "Home", "is_active": False},
            ],
        )
        self.assertLess(len(json.dumps(packet)), 1200)

    def test_pending_review_correction_packet_keeps_goal_and_category_context(self) -> None:
        packet = {
            "capability_catalog": {"capabilities": [{"id": "large"}]},
            "runtime_context": {
                "today": "2026-07-24",
                "time_zone": "America/Vancouver",
                "working_set": [
                    {"task_id": index, "title": "x" * 100}
                    for index in range(20)
                ],
                "task_scope_index": {
                    "categories": [
                        {
                            "category_id": 3,
                            "name": "Home",
                            "is_active": False,
                            "roots": [{"task_id": 42, "title": "Cooking"}],
                        }
                    ]
                },
                "active_request_continuation": {
                    "request_prompts": [
                        "Add Cooking tomorrow evening.",
                        "Allocate 2.5 hrs.",
                    ]
                },
            },
            "goalContext": {
                "pendingReview": {"reviewId": "review:1"},
                "semanticSlots": [{"path": "operations.requested"}],
            },
        }

        _prune_context_packet_for_pending_review_correction(packet)

        self.assertNotIn("capability_catalog", packet)
        self.assertNotIn("working_set", packet["runtime_context"])
        self.assertNotIn("task_scope_index", packet["runtime_context"])
        self.assertNotIn("active_request_continuation", packet["runtime_context"])
        self.assertEqual(
            packet["runtime_context"]["category_catalog"],
            [{"category_id": 3, "name": "Home", "is_active": False}],
        )
        self.assertEqual(
            packet["goalContext"]["pendingReview"]["reviewId"],
            "review:1",
        )
        self.assertIn(
            "existing ChangeSet revision",
            packet["runtime_context"]["context_note"],
        )

    def test_terminal_goal_context_drops_pending_review_and_evidence(self) -> None:
        goal = GoalState(
            goal_id="goal:terminal",
            status="applied",
            original_request="Finished work.",
            correlation_id="corr:terminal",
            causation_id="turn:terminal",
        )
        context = build_compact_goal_context(
            goal_state=goal,
            latest_user_turn="Start something else.",
            runtime_context={},
            pending_proposal={"proposal_id": "proposal:stale", "status": "pending"},
            recent_events=[{"evidence_handle": "evidence:stale"}],
        )

        self.assertIsNone(context["pendingReview"])
        self.assertEqual(context["evidenceHandles"], [])

    def test_durable_evidence_handle_is_session_and_goal_bound(self) -> None:
        with Session(self.engine) as session:
            goal = GoalStateCoordinator(session).ensure_active(
                session_id="session:one",
                original_request="Inspect the application task.",
                correlation_id="corr:one",
                causation_id="turn:one",
                command_id="turn:one",
            )
            event = StrategistAgentEvent(
                id="event:tool:1",
                session_id="session:one",
                correlation_id="corr:one",
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                sequence=2,
                event_type="tool_observation",
                payload_json=json.dumps(
                    {
                        "name": "get_task",
                        "arguments": {"task_id": 42},
                        "result": {
                            "task": {
                                "task_id": 42,
                                "title": "Application",
                                "notes": "bounded evidence",
                            },
                            "large_private_blob": "x" * 30000,
                        },
                    }
                ),
            )
            session.add(event)
            session.commit()
            handle = evidence_handle_for_event(event)

            resolved = resolve_evidence_handle(
                session,
                session_id="session:one",
                goal_id=goal.goal_id,
                handle=handle,
            )
            self.assertEqual(resolved["tool_name"], "get_task")
            self.assertEqual(resolved["result"]["task"]["task_id"], 42)
            self.assertNotIn("large_private_blob", json.dumps(resolved))

            with self.assertRaises(PermissionError):
                resolve_evidence_handle(
                    session,
                    session_id="session:two",
                    goal_id=goal.goal_id,
                    handle=handle,
                )
            with self.assertRaises(PermissionError):
                resolve_evidence_handle(
                    session,
                    session_id="session:one",
                    goal_id="goal:other",
                    handle=handle,
                )


if __name__ == "__main__":
    unittest.main()
