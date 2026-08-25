"""Durable cross-domain plans compiled from legacy proposal adapters.

The agent may still produce TaskPatch, SchedulePatch, or PreferencePatch while
the migration is in progress.  This module projects those adapter payloads into
one persisted ChangeSet so validation, review, resume, and apply have a stable
goal-level identity.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel import Session

from app.models.change_set import ChangeSetRecord


CHANGE_SET_OUTCOMES = {
    "draft",
    "ready_for_approval",
    "needs_user_input",
    "repairable_internally",
    "schedule_conflict",
    "unsupported",
    "system_failure",
    "applied",
    "rejected",
    "superseded",
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _operations_from_patch(
    *,
    domain: str,
    patch: Any,
) -> list[dict[str, Any]]:
    if not isinstance(patch, dict):
        return []
    operations = patch.get("operations")
    if not isinstance(operations, list):
        return []
    return [
        {"domain": domain, "operation": dict(operation)}
        for operation in operations
        if isinstance(operation, dict)
    ]


def compile_change_set_operations(
    *,
    proposal_kind: str,
    proposal_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compile all mutations in a proposal without discarding adapter details."""

    operations: list[dict[str, Any]] = []
    operations.extend(
        _operations_from_patch(
            domain="task",
            patch=proposal_payload.get("task_patch") or proposal_payload.get("taskPatch"),
        )
    )
    operations.extend(
        _operations_from_patch(
            domain="calendar",
            patch=proposal_payload.get("schedule_patch")
            or proposal_payload.get("schedulePatch"),
        )
    )
    operations.extend(
        _operations_from_patch(
            domain="memory",
            patch=proposal_payload.get("preference_patch")
            or proposal_payload.get("preferencePatch"),
        )
    )

    if not any(item.get("domain") == "task" for item in operations):
        structural_operations = proposal_payload.get(
            "structural_operations",
            proposal_payload.get("structuralOperations"),
        )
        if isinstance(structural_operations, list):
            operations.extend(
                {
                    "domain": "task",
                    "operation": dict(operation),
                }
                for operation in structural_operations
                if isinstance(operation, dict)
            )
        target_updates = proposal_payload.get(
            "target_updates",
            proposal_payload.get("targetUpdates"),
        )
        if isinstance(target_updates, list):
            operations.extend(
                {
                    "domain": "task",
                    "operation": {
                        "op": "update_node",
                        "node_id": item.get("target_node_id", item.get("targetNodeId")),
                        "updates": dict(item.get("updates") or {}),
                    },
                }
                for item in target_updates
                if isinstance(item, dict) and isinstance(item.get("updates"), dict)
            )
        updates = proposal_payload.get("updates")
        target_node_id = proposal_payload.get(
            "target_node_id",
            proposal_payload.get("targetNodeId"),
        )
        if isinstance(updates, dict) and target_node_id is not None:
            operations.append(
                {
                    "domain": "task",
                    "operation": {
                        "op": "update_node",
                        "node_id": target_node_id,
                        "updates": dict(updates),
                    },
                }
            )

    recurring_schedule = proposal_payload.get("recurring_schedule")
    if isinstance(recurring_schedule, dict):
        operations.append(
            {
                "domain": "calendar",
                "operation": {
                    "op": "create_series",
                    "target_temp_id": recurring_schedule.get("target_temp_id"),
                    "rule": dict(recurring_schedule),
                },
            }
        )
    created_schedule = proposal_payload.get("created_task_schedule")
    if isinstance(created_schedule, dict):
        operations.append(
            {
                "domain": "calendar",
                "operation": {
                    "op": "schedule_created_tasks",
                    **dict(created_schedule),
                },
            }
        )

    # Schedule proposals put their patch one level lower than task proposals.
    nested_payload = proposal_payload.get("payload")
    if isinstance(nested_payload, dict):
        operations.extend(
            _operations_from_patch(
                domain="task",
                patch=nested_payload.get("task_patch") or nested_payload.get("taskPatch"),
            )
        )
        if not any(item.get("domain") == "calendar" for item in operations):
            operations.extend(
                _operations_from_patch(
                    domain="calendar",
                    patch=nested_payload.get("schedule_patch")
                    or nested_payload.get("schedulePatch"),
                )
            )

    proposal_attempts = proposal_payload.get("proposal_attempts")
    if isinstance(proposal_attempts, list):
        for attempt in proposal_attempts:
            if not isinstance(attempt, dict):
                continue
            attempt_kind = str(
                attempt.get("proposal_kind")
                or attempt.get("proposalKind")
                or attempt.get("kind")
                or "proposal"
            ).strip()
            operations.extend(
                compile_change_set_operations(
                    proposal_kind=attempt_kind,
                    proposal_payload=attempt,
                )
            )

    # Keep the family only as provenance; execution dispatches per operation.
    for item in operations:
        item.setdefault("adapter_family", proposal_kind)
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in operations:
        signature = _json_dumps(
            {
                "domain": item.get("domain"),
                "operation": item.get("operation"),
            }
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduplicated.append(item)
    return deduplicated


def compile_change_set_constraints(proposal_payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        proposal_payload.get("request_constraints"),
        proposal_payload.get("requestConstraints"),
    ]
    nested_payload = proposal_payload.get("payload")
    if isinstance(nested_payload, dict):
        candidates.extend(
            [nested_payload.get("request_constraints"), nested_payload.get("requestConstraints")]
        )
    constraints: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if isinstance(item, dict) and item not in constraints:
                constraints.append(dict(item))
    proposal_attempts = proposal_payload.get("proposal_attempts")
    if isinstance(proposal_attempts, list):
        for attempt in proposal_attempts:
            if not isinstance(attempt, dict):
                continue
            for item in compile_change_set_constraints(attempt):
                if item not in constraints:
                    constraints.append(item)
    return constraints


def persist_change_set(
    session: Session,
    *,
    session_id: str,
    thread_id: str | None,
    source_turn_id: str | None,
    proposal_id: str | None,
    proposal_kind: str,
    proposal_payload: dict[str, Any],
    outcome: str,
    validation: dict[str, Any] | None = None,
    simulation: dict[str, Any] | None = None,
    failures: list[dict[str, Any]] | None = None,
    change_set_id: str | None = None,
    commit: bool = False,
) -> ChangeSetRecord:
    normalized_outcome = outcome if outcome in CHANGE_SET_OUTCOMES else "system_failure"
    record = session.get(ChangeSetRecord, change_set_id) if change_set_id else None
    if record is not None and record.session_id != session_id:
        raise ValueError("The saved change set belongs to a different AI workspace session.")
    if record is None:
        record = ChangeSetRecord(session_id=session_id)
    else:
        record.revision += 1
    record.thread_id = thread_id
    record.source_turn_id = source_turn_id or record.source_turn_id
    record.proposal_id = proposal_id
    record.status = (
        "pending_approval" if normalized_outcome == "ready_for_approval" else "blocked"
    )
    record.outcome = normalized_outcome
    record.goal = str(
        proposal_payload.get("summary")
        or proposal_payload.get("rationale")
        or record.goal
        or "Requested AI workspace change"
    ).strip()
    record.operations_json = _json_dumps(
        compile_change_set_operations(
            proposal_kind=proposal_kind,
            proposal_payload=proposal_payload,
        )
    )
    record.constraints_json = _json_dumps(
        compile_change_set_constraints(proposal_payload)
    )
    record.validation_json = _json_dumps(validation or {})
    record.simulation_json = _json_dumps(simulation or {})
    record.source_payload_json = _json_dumps(proposal_payload)
    record.failure_json = _json_dumps(failures) if failures else None
    record.updated_at = datetime.utcnow()
    session.add(record)
    session.flush()
    if commit:
        session.commit()
        session.refresh(record)
    return record


def update_change_set_status(
    session: Session,
    *,
    change_set_id: str | None,
    status: str,
    outcome: str | None = None,
    commit: bool = False,
) -> ChangeSetRecord | None:
    if not change_set_id:
        return None
    record = session.get(ChangeSetRecord, change_set_id)
    if record is None:
        return None
    record.status = status
    if outcome in CHANGE_SET_OUTCOMES:
        record.outcome = str(outcome)
    record.updated_at = datetime.utcnow()
    session.add(record)
    if commit:
        session.commit()
        session.refresh(record)
    return record


def mark_change_set_resumed(
    session: Session,
    change_set_id: str,
    *,
    session_id: str | None = None,
) -> ChangeSetRecord | None:
    record = session.get(ChangeSetRecord, change_set_id)
    if record is None or (session_id is not None and record.session_id != session_id):
        return None
    record.resume_count += 1
    record.status = "repairing"
    record.outcome = "repairable_internally"
    record.updated_at = datetime.utcnow()
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def change_set_resume_context(record: ChangeSetRecord) -> dict[str, Any]:
    def load(raw: str | None, fallback: Any) -> Any:
        if not raw:
            return fallback
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return fallback

    return {
        "change_set_id": record.id,
        "goal": record.goal,
        "operations": load(record.operations_json, []),
        "constraints": load(record.constraints_json, []),
        "validation": load(record.validation_json, {}),
        "failures": load(record.failure_json, []),
        "source_payload": load(record.source_payload_json, {}),
        "revision": record.revision,
        "resume_count": record.resume_count,
    }
