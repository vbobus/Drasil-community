from __future__ import annotations

import unittest
from datetime import datetime

from pydantic import TypeAdapter, ValidationError

from app.services.agent_kernel.contracts import (
    AlternativeFeasibilityProof,
    BlockerEvidence,
    ChangeSetOperationRemove,
    ChangeSetRevisionDelta,
    ChangeSetSimulationResult,
    ChangeSetDecisionDelta,
    Conflict,
    ConflictAlternative,
    ConflictGraph,
    DecisionRequest,
    EntityRef,
    GoalStatePatch,
    OverlapInterval,
    OperationConservationReport,
    ProjectedEffect,
    Provenance,
    SimulationSnapshot,
    SnapshotFact,
    strict_provider_schema,
)


UTC_START = datetime.fromisoformat("2026-07-20T09:00:00+00:00")
UTC_END = datetime.fromisoformat("2026-07-20T10:00:00+00:00")


def blocker(entity_id: str = "block:1") -> BlockerEvidence:
    return BlockerEvidence(
        blocker_id=f"blocker:{entity_id}",
        entity_ref=EntityRef(kind="calendar_block", entity_id=entity_id, revision=1),
        source="calendar",
        observed_revision=1,
        observed_at=UTC_START,
        freshness_boundary=UTC_START,
        fingerprint=f"fingerprint:{entity_id}",
        priority="high",
        anchoring="anchored",
        movability="fixed",
        protection="protected",
    )


def conflict() -> Conflict:
    return Conflict(
        conflict_id="conflict:1",
        kind="calendar_overlap",
        incoming_operation_ids=["op:incoming"],
        blockers=[blocker()],
        overlap_intervals=[
            OverlapInterval(start_at=UTC_START, end_at=UTC_END, timezone="UTC")
        ],
        policy_recommendation="ask_user",
        resolution_disposition="needs_user_decision",
        confidence=0.5,
        alternatives=[
            ConflictAlternative(
                alternative_id="alternative:1",
                summary="Keep the protected block.",
                feasibility="feasible",
                affected_entities=[EntityRef(kind="calendar_block", entity_id="block:1")],
                change_set_delta=ChangeSetRevisionDelta(
                    expected_change_set_revision=1,
                    edits=[
                        ChangeSetOperationRemove(
                            edit_id="edit:1", operation_id="op:incoming"
                        )
                    ],
                ),
                feasibility_proof=AlternativeFeasibilityProof(
                    snapshot_fingerprint="fingerprint:world",
                    delta_fingerprint="fingerprint:delta",
                    method="projection",
                ),
            )
        ],
    )


class AgentConflictGraphContractTests(unittest.TestCase):
    def test_overlap_and_blocker_evidence_are_closed_and_validated(self) -> None:
        item = blocker()
        self.assertEqual(item.movability, "fixed")
        with self.assertRaises(ValidationError):
            OverlapInterval(start_at=UTC_END, end_at=UTC_START, timezone="UTC")
        with self.assertRaises(ValidationError):
            BlockerEvidence(
                blocker_id="blocker:invalid",
                entity_ref=EntityRef(kind="calendar_block", entity_id="block:1"),
                source="calendar",
                observed_revision=1,
                observed_at=UTC_START,
                freshness_boundary=UTC_START,
                fingerprint="fingerprint:block:1",
                movability="sometimes",
            )

    def test_graph_requires_unique_members_and_one_graph_level_decision(self) -> None:
        decision = DecisionRequest(
            decision_id="decision:1",
            goal_id="goal:1",
            goal_revision=2,
            target_revision=3,
            question="Which commitment should move?",
            reason="Both commitments are protected.",
            options=[
                ChangeSetDecisionDelta(
                    option_id="option:1",
                    label="Keep the existing commitment",
                    change_set_id="changeset:1",
                    alternative_id="alternative:1",
                    delta=ChangeSetRevisionDelta(
                        expected_change_set_revision=1,
                        edits=[
                            ChangeSetOperationRemove(
                                edit_id="edit:decision", operation_id="op:incoming"
                            )
                        ],
                    ),
                )
            ],
        )
        graph = ConflictGraph(
            graph_id="graph:1",
            goal_id="goal:1",
            goal_revision=1,
            change_set_id="changeset:1",
            change_set_revision=1,
            change_set_operation_ids=["op:incoming"],
            snapshot_fingerprint="snapshot:1",
            status="needs_user_decision",
            conflicts=[conflict()],
            decision_request=decision,
        )
        self.assertEqual(graph.decision_request.options[0].target, "change_set")
        with self.assertRaises(ValidationError):
            ConflictGraph.model_validate(
                {**graph.model_dump(), "conflicts": [conflict(), conflict()]}
            )
        with self.assertRaises(ValidationError):
            ConflictGraph(
                graph_id="graph:clear",
                goal_id="goal:1",
                goal_revision=1,
                change_set_id="changeset:1",
                change_set_revision=1,
                change_set_operation_ids=["op:incoming"],
                snapshot_fingerprint="snapshot:1",
                status="clear",
                conflicts=[conflict()],
            )

    def test_simulation_result_binds_exact_revisions_snapshot_and_graph(self) -> None:
        snapshot = SimulationSnapshot(
            snapshot_id="snapshot:1",
            fingerprint="fingerprint:world",
            captured_at=UTC_START,
            freshness_boundary=UTC_START,
            facts=[
                SnapshotFact(
                    fact_id="fact:block:1",
                    domain="calendar",
                    entity_ref=EntityRef(
                        kind="calendar_block", entity_id="block:1", revision=1
                    ),
                    fingerprint="fingerprint:block:1",
                )
            ],
        )
        graph = ConflictGraph(
            graph_id="graph:clear",
            goal_id="goal:1",
            goal_revision=1,
            change_set_id="changeset:1",
            change_set_revision=1,
            change_set_operation_ids=["op:incoming"],
            snapshot_fingerprint=snapshot.fingerprint,
            status="clear",
        )
        result = ChangeSetSimulationResult(
            simulation_id="simulation:1",
            command_id="command:1",
            goal_id="goal:1",
            goal_revision=1,
            change_set_id="changeset:1",
            change_set_revision=1,
            input_snapshot=snapshot,
            output_fingerprint="fingerprint:output",
            status="ready_for_review",
            conflict_graph=graph,
            projected_effects=[
                ProjectedEffect(
                    operation_id="op:incoming",
                    kind="calendar.created",
                    entity_refs=[EntityRef(kind="calendar_block", entity_id="block:new")],
                )
            ],
            conservation=OperationConservationReport(complete=True),
            correlation_id="corr:1",
            causation_id="turn:1",
        )
        self.assertEqual(result.conflict_graph.snapshot_fingerprint, snapshot.fingerprint)
        with self.assertRaises(ValidationError):
            ChangeSetSimulationResult.model_validate(
                {
                    **result.model_dump(),
                    "conflict_graph": {
                        **graph.model_dump(),
                        "change_set_revision": 2,
                    },
                }
            )

    def test_all_new_model_facing_contracts_have_strict_provider_schemas(self) -> None:
        for contract in (ConflictGraph, ChangeSetSimulationResult):
            schema = strict_provider_schema(TypeAdapter(contract))
            self.assertFalse(schema.get("additionalProperties", True))

    def test_graph_rejects_unknown_incoming_operations_and_stale_alternative_deltas(self) -> None:
        item = conflict()
        payload = {
            "graph_id": "graph:1",
            "goal_id": "goal:1",
            "goal_revision": 1,
            "change_set_id": "changeset:1",
            "change_set_revision": 2,
            "change_set_operation_ids": ["op:other"],
            "snapshot_fingerprint": "snapshot:1",
            "status": "resolved",
            "conflicts": [item],
        }
        with self.assertRaises(ValidationError):
            ConflictGraph.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
