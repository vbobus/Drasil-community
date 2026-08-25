from datetime import datetime, timedelta
import json
import unittest

from sqlmodel import Session

from app.db.database import create_db_and_tables, engine
from app.models.strategist_agent import StrategistAgentEvent
from app.models.strategist_chat import StrategistChatSession
from app.services.agent_kernel.mcp_read_rollout import (
    MCP_READ_PROMOTION_REQUESTED_ENV,
    MCP_READ_PROMOTION_TOOLS_ENV,
    McpReadPromotionPolicy,
    evaluate_mcp_read_promotion,
)


class McpReadRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        with Session(engine, expire_on_commit=False) as session:
            chat = StrategistChatSession(title="MCP read rollout test")
            session.add(chat)
            session.commit()
            self.session_id = chat.id
            self.build_id = f"build-{chat.id}"
        self.now = datetime(2026, 7, 22, 18, 0, 0)

    def _record(
        self,
        session: Session,
        *,
        tool: str,
        status: str = "matched",
        matched: bool = True,
        build_id: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        session.add(
            StrategistAgentEvent(
                session_id=self.session_id,
                event_type="mcp_read_shadow_comparison",
                source="mcp_shadow",
                payload_json=json.dumps(
                    {
                        "build_id": build_id or self.build_id,
                        "tool_name": tool,
                        "attempted": True,
                        "status": status,
                        "matched": matched,
                        "primary_digest": "a" * 64,
                        "candidate_digest": (
                            None
                            if status == "candidate_failure"
                            else (
                                "a" * 64
                                if status == "matched"
                                else "b" * 64
                            )
                        ),
                        "difference_paths": (
                            []
                            if status in {"matched", "candidate_failure"}
                            else ["$.count"]
                        ),
                        "error_type": (
                            "TimeoutError"
                            if status == "candidate_failure"
                            else None
                        ),
                        "authoritative_result": "native",
                        "candidate_result_used": False,
                    }
                ),
                created_at=created_at or self.now,
            )
        )

    def _policy(
        self,
        *,
        requested: bool = True,
        tools: tuple[str, ...] = ("search_entities",),
        build_id: str | None = None,
        minimum: int = 2,
    ) -> McpReadPromotionPolicy:
        return McpReadPromotionPolicy(
            requested=requested,
            required_build_id=build_id or self.build_id,
            required_tools=tools,
            min_matches_per_tool=minimum,
            max_evidence_age_days=14,
            clock=lambda: self.now,
        )

    def test_clean_exact_build_evidence_can_become_eligible(self) -> None:
        with Session(engine) as session:
            for tool in ("search_entities", "get_task_snapshot"):
                self._record(session, tool=tool)
                self._record(session, tool=tool)
            session.commit()

            report = evaluate_mcp_read_promotion(
                session,
                policy=self._policy(
                    tools=("search_entities", "get_task_snapshot")
                ),
            )

        self.assertTrue(report.eligible)
        self.assertEqual(
            report.reason_code,
            "mcp_read_promotion_eligible",
        )
        self.assertEqual(report.blockers, ())
        self.assertTrue(all(item.match_count == 2 for item in report.evidence))
        self.assertFalse(report.to_payload()["changesAuthority"])

    def test_any_mismatch_or_candidate_failure_blocks_promotion(self) -> None:
        with Session(engine) as session:
            self._record(session, tool="search_entities")
            self._record(
                session,
                tool="search_entities",
                status="mismatched",
                matched=False,
            )
            self._record(
                session,
                tool="search_entities",
                status="candidate_failure",
                matched=False,
            )
            session.commit()

            report = evaluate_mcp_read_promotion(
                session,
                policy=self._policy(minimum=1),
            )

        self.assertFalse(report.eligible)
        self.assertEqual(report.reason_code, "shadow_evidence_insufficient")
        self.assertTrue(
            any("blocking_comparisons:2" in item for item in report.blockers)
        )

    def test_wrong_build_and_stale_evidence_do_not_count(self) -> None:
        with Session(engine) as session:
            self._record(
                session,
                tool="search_entities",
                build_id="older-build",
            )
            self._record(
                session,
                tool="search_entities",
                created_at=self.now - timedelta(days=15),
            )
            session.commit()

            report = evaluate_mcp_read_promotion(
                session,
                policy=self._policy(minimum=1),
            )

        self.assertFalse(report.eligible)
        self.assertEqual(report.evidence[0].sample_count, 0)
        self.assertIn(
            "search_entities:insufficient_matches:0/1",
            report.blockers,
        )

    def test_missing_build_or_unrequested_promotion_fails_closed(self) -> None:
        with Session(engine) as session:
            self._record(session, tool="search_entities")
            self._record(session, tool="search_entities")
            session.commit()

            missing_build = evaluate_mcp_read_promotion(
                session,
                policy=self._policy(
                    build_id="missing_build_identity",
                ),
            )
            not_requested = evaluate_mcp_read_promotion(
                session,
                policy=self._policy(requested=False),
            )

        self.assertFalse(missing_build.eligible)
        self.assertIn("missing_build_identity", missing_build.blockers)
        self.assertFalse(not_requested.eligible)
        self.assertEqual(
            not_requested.reason_code,
            "promotion_not_requested",
        )

    def test_environment_rejects_unsupported_promotion_tools(self) -> None:
        policy = McpReadPromotionPolicy.from_environment(
            {
                "DRASIL_BUILD_ID": self.build_id,
                MCP_READ_PROMOTION_REQUESTED_ENV: "true",
                MCP_READ_PROMOTION_TOOLS_ENV: (
                    "search_entities,draft_change_set"
                ),
            }
        )

        self.assertEqual(policy.required_tools, ("search_entities",))
        self.assertIn(
            "unsupported_tools:draft_change_set",
            policy.configuration_errors,
        )

    def test_structurally_invalid_exact_build_event_blocks_promotion(
        self,
    ) -> None:
        with Session(engine) as session:
            self._record(session, tool="search_entities")
            self._record(session, tool="search_entities")
            session.add(
                StrategistAgentEvent(
                    session_id=self.session_id,
                    event_type="mcp_read_shadow_comparison",
                    source="mcp_shadow",
                    payload_json=json.dumps(
                        {
                            "build_id": self.build_id,
                            "tool_name": "search_entities",
                            "attempted": True,
                            "status": "matched",
                            "matched": True,
                            "authoritative_result": "native",
                            "candidate_result_used": True,
                        }
                    ),
                    created_at=self.now,
                )
            )
            session.commit()

            report = evaluate_mcp_read_promotion(
                session,
                policy=self._policy(),
            )

        self.assertFalse(report.eligible)
        self.assertEqual(report.evidence[0].invalid_event_count, 1)
        self.assertIn(
            "search_entities:blocking_comparisons:1",
            report.blockers,
        )

if __name__ == "__main__":
    unittest.main()
