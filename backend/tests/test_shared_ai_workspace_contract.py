import unittest
from datetime import datetime

from pydantic import ValidationError
from sqlmodel import Session, SQLModel, select

from app.api.ai import StrategistRequest, StrategistResponse, serialize_turn_record
from app.db.database import engine
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.tool_registry import execute_kernel_tool
from app.services.strategist_graph import (
    attach_response_task_references,
    build_standard_response_proposal,
    build_task_patch_application_summary,
)
from app.services.strategist_sessions import (
    complete_strategist_turn,
    create_pending_strategist_turn,
    create_strategist_session,
    parse_turn_response_payload,
)


class SharedAiWorkspaceContractTests(unittest.TestCase):
    def test_batch_cleanup_application_receipt_uses_plain_language(self) -> None:
        summary = build_task_patch_application_summary(
            {
                "operations": [
                    *({"op": "move", "task_id": task_id} for task_id in range(1, 7)),
                    *(
                        {"op": "delete", "mode": "archive", "task_id": task_id}
                        for task_id in range(7, 16)
                    ),
                ]
            },
            target_title="Academic",
        )

        self.assertEqual(
            summary,
            "Workspace cleanup complete: moved 6 task groups and archived 9 items.",
        )
        self.assertNotIn("TaskPatch", summary)
        self.assertNotIn("Academic", summary)

    def test_batch_cleanup_uses_whole_scope_title_not_first_destination(self) -> None:
        standard = build_standard_response_proposal(
            {
                "branch_proposal": {
                    "proposal_id": "cleanup-1",
                    "proposal_kind": "restructure",
                    "target_node_title": "Academic",
                    "summary": "Move coherent branches to their appropriate workspaces.",
                    "rationale": "The full workspace map supports these moves.",
                    "task_patch": {
                        "patch_id": "cleanup-patch-1",
                        "operations": [
                            {"op": "move", "task_id": 10, "category_id": 2},
                            {"op": "move", "task_id": 11, "category_id": 3},
                        ],
                    },
                }
            }
        )

        self.assertIsNotNone(standard)
        assert standard is not None
        self.assertEqual(standard["title"], "Review workspace cleanup")
        self.assertEqual(
            standard["body"],
            "Move coherent branches to their appropriate workspaces.",
        )
        self.assertEqual(
            standard["review_sections"],
            [
                {
                    "title": "Move to New location",
                    "items": [
                        {"text": "Task", "task_ids": [10]},
                        {"text": "Task", "task_ids": [11]},
                    ],
                }
            ],
        )

    def test_response_carries_exact_task_references_independent_of_ui_cache(self) -> None:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            category = session.exec(select(Category).where(Category.name == "Home")).first()
            if category is None:
                category = Category(name="Home", accent_color="#22c55e", icon_slug="house")
                session.add(category)
                session.commit()
                session.refresh(category)
            node = StrategyNode(
                category_id=int(category.id),
                title="Cross-workspace reference fixture",
                emoji="🏠",
            )
            session.add(node)
            session.commit()
            session.refresh(node)
            try:
                payload = attach_response_task_references(
                    session,
                    {
                        "summary": "Move Cross-workspace reference fixture to Home.",
                        "proposal": {
                            "target_node_ids": [int(node.id)],
                        },
                    },
                )
                references = payload["task_references"]
                matching = [item for item in references if item["task_id"] == node.id]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]["title"], node.title)
                self.assertEqual(matching[0]["category_name"], "Home")
                self.assertEqual(matching[0]["category_accent_color"], category.accent_color)
            finally:
                session.delete(node)
                session.commit()

    def test_response_does_not_link_incidental_task_title_text_without_an_id(self) -> None:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            category = session.exec(select(Category).where(Category.name == "Home")).first()
            if category is None:
                category = Category(name="Home", accent_color="#22c55e", icon_slug="house")
                session.add(category)
                session.commit()
                session.refresh(category)
            target = StrategyNode(
                category_id=int(category.id),
                title="Learn Kubernetes reference fixture",
            )
            incidental = StrategyNode(
                category_id=int(category.id),
                title="Networking reference fixture",
            )
            session.add(target)
            session.add(incidental)
            session.commit()
            session.refresh(target)
            session.refresh(incidental)
            try:
                payload = attach_response_task_references(
                    session,
                    {
                        "summary": f"Break down {target.title}.",
                        "proposal": {
                            "target_node_id": int(target.id),
                            "task_patch": {
                                "operations": [
                                    {
                                        "op": "create",
                                        "title": (
                                            "Practice configuration, "
                                            f"{incidental.title}, and storage"
                                        ),
                                    }
                                ]
                            },
                        },
                    },
                )
                self.assertEqual(
                    [item["task_id"] for item in payload["task_references"]],
                    [target.id],
                )
            finally:
                session.delete(target)
                session.delete(incidental)
                session.commit()

    def test_response_consumes_internal_grounded_ids_for_task_references(self) -> None:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            category = session.exec(select(Category).where(Category.name == "Home")).first()
            if category is None:
                category = Category(name="Home", accent_color="#22c55e", icon_slug="house")
                session.add(category)
                session.commit()
                session.refresh(category)
            target = StrategyNode(
                category_id=int(category.id),
                title="Grounded recommendation fixture",
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            try:
                payload = attach_response_task_references(
                    session,
                    {
                        "summary": f"Start with **{target.title}**.",
                        "_grounded_task_ids": [int(target.id)],
                    },
                )

                self.assertNotIn("_grounded_task_ids", payload)
                self.assertEqual(
                    [item["task_id"] for item in payload["task_references"]],
                    [target.id],
                )
            finally:
                session.delete(target)
                session.commit()

    def test_response_links_conflict_calendar_block_to_its_persisted_task(self) -> None:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            category = session.exec(select(Category).where(Category.name == "Work")).first()
            if category is None:
                category = Category(name="Work", accent_color="#6366f1", icon_slug="briefcase")
                session.add(category)
                session.commit()
                session.refresh(category)
            target = StrategyNode(
                category_id=int(category.id),
                title="Existing calendar conflict fixture",
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            block = CalendarBlock(
                task_id=int(target.id),
                category_id=int(category.id),
                start_at=datetime(2026, 8, 16, 17, 0),
                end_at=datetime(2026, 8, 16, 18, 0),
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            try:
                payload = attach_response_task_references(
                    session,
                    {
                        "review_document": {
                            "blocks": [
                                {
                                    "conflicts": [
                                        {
                                            "affectedEntities": [
                                                {
                                                    "ref": {
                                                        "kind": "calendar_block",
                                                        "entityId": f"calendar_block:{block.id}",
                                                    },
                                                    "label": target.title,
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                )

                self.assertEqual(
                    [item["task_id"] for item in payload["task_references"]],
                    [target.id],
                )
                self.assertEqual(payload["task_references"][0]["category_name"], "Work")
            finally:
                session.delete(block)
                session.delete(target)
                session.commit()

    def test_planner_handoff_accepts_only_live_attached_task_ids(self) -> None:
        category = Category(id=4, name="Work")
        task = StrategyNode(id=7, category_id=4, title="Draft launch plan")
        arguments = {
            "task_ids": [7],
            "instruction": "schedule these tomorrow",
            "window_start": "2026-07-13T00:00:00",
            "window_end": "2026-07-14T00:00:00",
            "operation_mode": "create",
            "scope_source": "attached_working_set",
            "request_constraints": [
                {
                    "kind": "unavailable_interval",
                    "hard": True,
                    "label": "Lunch",
                    "source_text": "leave 30 minutes at 3 for lunch",
                    "start_at": "2026-07-13T15:00:00",
                    "end_at": "2026-07-13T15:30:00",
                }
            ],
        }
        accepted = execute_kernel_tool(
            "handoff_to_planner",
            arguments,
            categories=[category],
            nodes=[task],
            focus_node=None,
            staged_node_ids=[7],
        )
        self.assertEqual(accepted["result_kind"], "planner_handoff")
        self.assertEqual(accepted["planner_handoff"]["task_ids"], [7])
        self.assertEqual(
            accepted["planner_handoff"]["request_constraints"][0]["label"],
            "Lunch",
        )

        rejected = execute_kernel_tool(
            "handoff_to_planner",
            {**arguments, "task_ids": [8]},
            categories=[category],
            nodes=[task],
            focus_node=None,
            staged_node_ids=[7],
        )
        self.assertIn("error", rejected)
        self.assertEqual(rejected["invalid_task_ids"], [8])

        explicit = execute_kernel_tool(
            "handoff_to_planner",
            {**arguments, "task_ids": [8], "scope_source": "explicit_named_task"},
            categories=[category],
            nodes=[task, StrategyNode(id=8, category_id=4, title="Named task")],
            focus_node=None,
            staged_node_ids=[7],
        )
        self.assertEqual(explicit["planner_handoff"]["task_ids"], [8])

        stale = execute_kernel_tool(
            "handoff_to_planner",
            {**arguments, "task_ids": [99], "scope_source": "explicit_named_task"},
            categories=[category],
            nodes=[task],
            focus_node=None,
            staged_node_ids=[7],
        )
        self.assertEqual(stale["invalid_task_ids"], [99])

        calendar_scoped = execute_kernel_tool(
            "handoff_to_planner",
            {**arguments, "task_ids": [8], "scope_source": "calendar_query"},
            categories=[category],
            nodes=[task, StrategyNode(id=8, category_id=4, title="Scheduled task")],
            focus_node=None,
            staged_node_ids=[7],
        )
        self.assertEqual(calendar_scoped["planner_handoff"]["task_ids"], [8])

    def test_planner_handoff_semantic_failure_is_model_repairable(self) -> None:
        category = Category(id=4, name="Work")
        task = StrategyNode(id=7, category_id=4, title="Team call")
        planner_calls: list[dict[str, object]] = []

        result = execute_kernel_tool(
            "handoff_to_planner",
            {
                "task_ids": [7],
                "instruction": "Move the Team call to 8 PM.",
                "window_start": "2026-07-21T20:00:00",
                "window_end": "2026-07-21T20:00:00",
                "operation_mode": "reschedule",
                "scope_source": "explicit_named_task",
            },
            categories=[category],
            nodes=[task],
            focus_node=None,
            staged_node_ids=[],
            planner_executor=lambda arguments: planner_calls.append(arguments) or {},
        )

        self.assertEqual(planner_calls, [])
        self.assertEqual(result["tool_error"]["category"], "model_repairable")
        self.assertEqual(
            result["tool_error"]["affectedPaths"],
            ["window_start", "window_end"],
        )
        self.assertIn("get_task_schedule", result["repair_hint"])

    def test_planner_result_returns_to_the_same_tool_loop_without_terminal_response(self) -> None:
        category = Category(id=4, name="Work")
        task = StrategyNode(id=7, category_id=4, title="Team call")

        result = execute_kernel_tool(
            "handoff_to_planner",
            {
                "task_ids": [7],
                "instruction": "Move the Team call to 8 PM.",
                "window_start": "2026-07-21T20:00:00",
                "window_end": "2026-07-22T00:00:00",
                "operation_mode": "reschedule",
                "scope_source": "explicit_named_task",
            },
            categories=[category],
            nodes=[task],
            focus_node=None,
            staged_node_ids=[],
            planner_executor=lambda _arguments: {
                "planner_executed": True,
                "proposal_ready": True,
                "planner_result": {"mode": "schedule_patch", "proposal": {"id": "p1"}},
            },
        )

        self.assertTrue(result["planner_executed"])
        self.assertTrue(result["proposal_ready"])
        self.assertEqual(result["planner_result"]["mode"], "schedule_patch")
        self.assertNotIn("terminal_response", result)

    def test_calendar_reader_is_injected_and_returns_typed_context(self) -> None:
        category = Category(id=4, name="Work")
        task = StrategyNode(id=7, category_id=4, title="Draft launch plan")
        seen: list[dict[str, object]] = []

        result = execute_kernel_tool(
            "list_calendar_blocks",
            {
                "start_at": "2026-07-16T00:00:00",
                "end_at": "2026-07-17T00:00:00",
            },
            categories=[category],
            nodes=[task],
            focus_node=None,
            staged_node_ids=[],
            calendar_reader=lambda arguments: (
                seen.append(arguments)
                or {"blocks": [], "task_ids": [], "count": 0}
            ),
        )

        self.assertEqual(seen[0]["start_at"], "2026-07-16T00:00:00")
        self.assertEqual(result["result_kind"], "calendar_context")
        self.assertEqual(result["count"], 0)

    def test_schedule_proposal_projects_without_taskpatch_compilation(self) -> None:
        standard = build_standard_response_proposal({
            "schedule_proposal": {
                "proposalId": "schedule-proposal-1",
                "proposalKind": "schedule_patch",
                "workingSetTaskIds": [7],
                "schedulePatch": {"patchId": "schedule-patch-1", "operations": []},
                "schedulePatchValidation": {"status": "valid", "warnings": []},
                "schedulePatchSimulation": {"previewBlocks": []},
                "schedulePatchStatus": "valid",
            }
        })
        self.assertIsNotNone(standard)
        assert standard is not None
        self.assertEqual(standard["proposal_kind"], "schedule_patch")
        self.assertNotIn("task_patch", standard)
        self.assertEqual(standard["schedule_patch"]["patchId"], "schedule-patch-1")

    def test_schedule_proposal_projects_companion_task_update(self) -> None:
        standard = build_standard_response_proposal({
            "schedule_proposal": {
                "proposalId": "schedule-proposal-2",
                "proposalKind": "schedule_patch",
                "workingSetTaskIds": [7],
                "taskPatch": {
                    "patchId": "task-patch-1",
                    "operations": [
                        {
                            "op": "update",
                            "taskId": 7,
                            "fields": {"time_estimate_minutes": 120},
                        }
                    ],
                },
                "taskPatchValidation": {"status": "valid", "errors": []},
                "taskPatchSimulation": {"operation_count": 1},
                "taskPatchStatus": "valid",
                "schedulePatch": {"patchId": "schedule-patch-2", "operations": []},
                "schedulePatchValidation": {"status": "valid", "warnings": []},
                "schedulePatchSimulation": {"previewBlocks": []},
                "schedulePatchStatus": "valid",
            }
        })

        self.assertIsNotNone(standard)
        assert standard is not None
        self.assertEqual(
            standard["task_patch"]["operations"][0]["fields"]["time_estimate_minutes"],
            120,
        )
        self.assertEqual(standard["task_patch_status"], "valid")

    def test_workspace_request_round_trips_calendar_and_local_time_context(self) -> None:
        request = StrategistRequest.model_validate(
            {
                "prompt": "schedule these tomorrow",
                "stagedNodeIds": [7, 9],
                "calendarViewStart": "2026-07-12T00:00:00",
                "calendarViewEnd": "2026-07-13T00:00:00",
                "localDateTime": "2026-07-12T15:30:00",
                "timeZone": "America/Vancouver",
            }
        )

        payload = request.model_dump(by_alias=True, mode="json")
        self.assertEqual(payload["calendarViewStart"], "2026-07-12T00:00:00")
        self.assertEqual(payload["calendarViewEnd"], "2026-07-13T00:00:00")
        self.assertEqual(payload["localDateTime"], "2026-07-12T15:30:00")
        self.assertEqual(payload["timeZone"], "America/Vancouver")

    def test_workspace_request_requires_calendar_range_as_a_pair(self) -> None:
        with self.assertRaises(ValidationError):
            StrategistRequest.model_validate(
                {
                    "prompt": "schedule these",
                    "calendarViewStart": "2026-07-12T00:00:00",
                }
            )

    def test_workspace_request_rejects_aware_or_reversed_local_ranges(self) -> None:
        invalid_payloads = (
            {
                "prompt": "schedule these",
                "calendarViewStart": "2026-07-12T00:00:00Z",
                "calendarViewEnd": "2026-07-13T00:00:00Z",
            },
            {
                "prompt": "schedule these",
                "calendarViewStart": "2026-07-13T00:00:00",
                "calendarViewEnd": "2026-07-12T00:00:00",
            },
            {
                "prompt": "schedule these",
                "localDateTime": "2026-07-12T08:00:00-07:00",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                StrategistRequest.model_validate(payload)

    def test_schedule_patch_is_a_first_class_shared_proposal(self) -> None:
        response = StrategistResponse.model_validate(
            {
                "providerLabel": "Planner",
                "mode": "schedule_patch",
                "summary": "A proposed schedule is ready.",
                "proposal": {
                    "proposalId": "schedule-proposal-1",
                    "proposalKind": "schedule_patch",
                    "operation": "schedule",
                    "legacyResponseKey": "scheduleProposal",
                    "title": "Schedule proposal",
                    "body": "Review the proposed blocks.",
                    "targetNodeIds": [7, 9],
                    "preview": [],
                    "validationWarnings": [],
                    "schedulePatch": {"patchId": "schedule-patch-1", "operations": []},
                    "schedulePatchValidation": {"status": "valid"},
                    "schedulePatchSimulation": {"previewBlocks": []},
                    "schedulePatchStatus": "valid",
                    "capabilityId": "planner",
                },
            }
        )

        proposal = response.model_dump(by_alias=True, mode="json")["proposal"]
        self.assertEqual(proposal["proposalKind"], "schedule_patch")
        self.assertEqual(proposal["schedulePatch"]["patchId"], "schedule-patch-1")
        self.assertEqual(proposal["capabilityId"], "planner")

    def test_schedule_patch_persists_and_serializes_without_changing_shape(self) -> None:
        payload = StrategistResponse.model_validate(
            {
                "providerLabel": "Planner",
                "mode": "schedule_patch",
                "summary": "A proposed schedule is ready.",
                "proposal": {
                    "proposalId": "schedule-proposal-history",
                    "proposalKind": "schedule_patch",
                    "operation": "schedule",
                    "legacyResponseKey": "scheduleProposal",
                    "title": "Schedule proposal",
                    "body": "Review it.",
                    "targetNodeIds": [7],
                    "preview": [],
                    "validationWarnings": [],
                    "schedulePatch": {"patchId": "schedule-patch-history", "operations": []},
                    "schedulePatchValidation": {"status": "warning"},
                    "schedulePatchSimulation": {"previewBlocks": []},
                    "schedulePatchStatus": "warning",
                    "capabilityId": "planner",
                },
            }
        ).model_dump(by_alias=True, mode="json")

        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            strategist_session = create_strategist_session(
                session,
                prompt="schedule it",
                provider="planner",
                provider_label="Planner",
                model_name=None,
            )
            turn = create_pending_strategist_turn(
                session,
                strategist_session=strategist_session,
                prompt="schedule it",
            )
            turn = complete_strategist_turn(
                session,
                strategist_session=strategist_session,
                turn=turn,
                response_payload=payload,
                error_text=None,
            )

            parsed = parse_turn_response_payload(turn)
            serialized = serialize_turn_record(turn)["response"]

        self.assertEqual(parsed, payload)
        self.assertEqual(serialized, payload)
        self.assertEqual(serialized["proposal"]["proposalKind"], "schedule_patch")


if __name__ == "__main__":
    unittest.main()
