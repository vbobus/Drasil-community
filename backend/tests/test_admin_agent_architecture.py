from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db.database import create_db_and_tables, engine
from app.main import app
from app.models.strategist_agent import StrategistAgentEvent, StrategistRun
from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn


class AgentArchitectureAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        self.client = TestClient(app)

    def test_architecture_snapshot_comes_from_runtime_registries(self) -> None:
        response = self.client.get("/api/v1/admin/agent-architecture")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("toolContractBoundary", payload["definition"])
        self.assertIn("server-injected", payload["definition"]["mcpInvocationBoundary"])
        self.assertEqual(
            {item["id"] for item in payload["agents"]},
            {"strategist", "planner"},
        )
        tool_names = {item["name"] for item in payload["tools"]}
        self.assertIn("handoff_to_planner", tool_names)
        handoff = next(
            item for item in payload["tools"] if item["name"] == "handoff_to_planner"
        )
        self.assertIn(
            "target_scope",
            {field["name"] for field in handoff["inputFields"]},
        )
        cleanup = next(
            item
            for item in payload["tools"]
            if item["name"] == "analyze_tree_cleanup_candidates"
        )
        self.assertIn("drasil_mcp", cleanup["externalSurfaces"])
        self.assertEqual(handoff["externalSurfaces"], [])
        self.assertEqual(payload["mcpServers"], [])
        self.assertIn("authoritative application capability", payload["mcpBoundaryNote"])
        self.assertIn("provider-side", payload["mcpBoundaryNote"])
        self.assertEqual(
            payload["executionMigration"]["taskReadAuthority"],
            "native_application_service",
        )
        self.assertEqual(
            payload["executionMigration"]["legacyReadDispatcher"],
            "compatibility_parity_only",
        )
        self.assertEqual(
            payload["executionMigration"]["calendarReadAuthority"],
            "native_sql_application_service",
        )
        self.assertEqual(
            payload["executionMigration"]["plannerExecutionBoundary"],
            "scheduling_application_service",
        )
        self.assertEqual(
            payload["executionMigration"]["mcpReadClient"],
            "implemented_shadow_only",
        )
        self.assertEqual(
            payload["executionMigration"]["mcpReadAuthority"],
            "native",
        )
        self.assertFalse(
            payload["executionMigration"]["mcpReadPromotionEligible"]
        )
        self.assertFalse(payload["mcpReadPromotion"]["eligible"])
        self.assertFalse(payload["mcpReadPromotion"]["changesAuthority"])
        shadow_connection = next(
            item
            for item in payload["connections"]
            if item["id"] == "local-mcp-read-shadow"
        )
        self.assertEqual(shadow_connection["kind"], "shadow_transport")
        self.assertFalse(payload["executionMigration"]["retirementAllowed"])
        authority = payload["authorityInventory"]
        self.assertEqual(authority["nativeAcceptedFallbackPolicy"], "prohibited")
        self.assertEqual(authority["mcpProductionAuthority"], "authoritative")
        capabilities = {
            item["capability"]: item for item in authority["capabilities"]
        }
        self.assertEqual(
            capabilities["task_reads"]["primaryPath"],
            "drasil_core_mcp",
        )
        self.assertEqual(
            capabilities["calendar_reads"]["primaryPath"],
            "drasil_core_mcp",
        )
        self.assertEqual(
            capabilities["preference_reads"]["applicationService"],
            "PreferenceApplicationService",
        )
        self.assertEqual(
            capabilities["scheduling"]["applicationService"],
            "PlannerSubagentCoordinator + PlannerSchedulingApplicationService",
        )
        self.assertTrue(capabilities["mcp_commands"]["promotionEligible"])
        self.assertTrue(payload["database"]["tables"])
        self.assertTrue(
            any(route["path"] == "/api/v1/ai/strategist" for route in payload["apiRoutes"])
        )

    def test_session_trace_is_compact_and_redacts_sensitive_tool_arguments(self) -> None:
        with Session(engine) as session:
            chat = StrategistChatSession(title="Architecture trace test")
            session.add(chat)
            session.commit()
            session.refresh(chat)
            turn = StrategistChatTurn(
                session_id=chat.id,
                prompt="move every occurrence to 7 pm",
                response_json=json.dumps(
                    {
                        "mode": "proposal",
                        "summary": "Review the repeating schedule change.",
                        "review_document": {"kind": "recurrence"},
                    }
                ),
            )
            session.add(turn)
            session.commit()
            session.refresh(turn)
            run = StrategistRun(
                session_id=chat.id,
                turn_id=turn.id,
                status="completed",
                control_plane="agent-kernel",
                prompt=turn.prompt,
                request_json="{}",
            )
            session.add(run)
            session.add(
                StrategistAgentEvent(
                    session_id=chat.id,
                    turn_id=turn.id,
                    run_id=run.id,
                    sequence=1,
                    event_type="tool_observation",
                    payload_json=json.dumps(
                        {
                            "name": "handoff_to_planner",
                            "arguments": {
                                "target_scope": "series",
                                "api_token": "must-not-leak",
                            },
                            "result": {
                                "result_kind": "planner_handoff",
                                "planner_mode": "native_recurrence",
                                "proposal_ready": True,
                            },
                        }
                    ),
                )
            )
            session.commit()
            session_id = chat.id

        response = self.client.get(
            f"/api/v1/admin/agent-architecture/sessions/{session_id}"
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["turns"][0]["response"]["proposalKind"], "review_document")
        event = payload["events"][0]
        self.assertEqual(event["details"]["tool"], "handoff_to_planner")
        self.assertEqual(event["details"]["arguments"]["target_scope"], "series")
        self.assertEqual(event["details"]["arguments"]["api_token"], "[redacted]")
        self.assertEqual(
            event["details"]["captured"]["arguments"]["target_scope"],
            "series",
        )
        self.assertEqual(
            event["details"]["captured"]["arguments"]["api_token"],
            "[redacted]",
        )
        self.assertEqual(payload["runs"][0]["request"], {})
        self.assertNotIn("must-not-leak", response.text)


if __name__ == "__main__":
    unittest.main()
