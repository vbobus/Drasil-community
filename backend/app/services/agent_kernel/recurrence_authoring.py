"""Deterministic native authoring for an evidence-backed recurrence update.

Planner deliberately delegates persisted series edits to the Agent Kernel because a
legacy SchedulePatch cannot conserve series scope.  This module closes that handoff
without another model call: it turns the model-selected, typed Planner handoff plus
authoritative ``get_task_schedule`` evidence into canonical v2 operations.  Pydantic
contracts validate the result; they do not infer intent or choose a target.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.services.agent_kernel.contracts import (
    EntityRef,
    Preconditions,
    Provenance,
    RecurrenceUpdateSeriesOperation,
)


class NativeRecurrenceAuthoringError(ValueError):
    """The typed handoff and authoritative schedule evidence cannot form one edit."""


@dataclass(frozen=True)
class NativeRecurrenceUpdateDraft:
    operations: tuple[RecurrenceUpdateSeriesOperation, ...]
    clause: dict[str, Any]
    preconditions: Preconditions
    provenance: Provenance


_HANDOFF_TO_RULE = {
    "frequency": "frequency",
    "days_of_week": "weekdays",
    "start_date": "start_date",
    "end_date": "end_date",
    "occurrence_count": "occurrence_count",
    "timezone": "timezone",
    "preferred_start_time": "preferred_start_time",
    "duration_minutes": "duration_minutes",
}
_EVIDENCE_RULE_KEYS = {
    "weekdays": "days_of_week",
}


def _stable_suffix(*values: object) -> str:
    payload = json.dumps(values, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _schedule_results(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item["result"]
        for item in tool_trace
        if isinstance(item, dict)
        and str(item.get("name") or "") in {"get_task_schedule", "list_calendar_blocks"}
        and isinstance(item.get("result"), dict)
        and not item["result"].get("error")
    ]


def compile_native_recurrence_update(
    *,
    handoff: dict[str, Any],
    tool_trace: list[dict[str, Any]],
    source_ref: str,
    source_text: str,
) -> NativeRecurrenceUpdateDraft:
    """Compile a review-only series update from exact persisted evidence.

    The function fails closed if a requested task maps to zero or multiple series.
    Only rule fields whose desired values differ from persistence are emitted, which
    prevents a simple clock-time correction from needlessly rewriting cadence or
    duration.
    """

    if handoff.get("operation_mode") != "reschedule":
        raise NativeRecurrenceAuthoringError("recurrence authoring requires reschedule mode")
    requested_rule = handoff.get("recurrence")
    if not isinstance(requested_rule, dict):
        raise NativeRecurrenceAuthoringError("recurrence authoring requires a typed rule")
    task_ids = {
        int(item)
        for item in handoff.get("task_ids", [])
        if isinstance(item, int) and not isinstance(item, bool)
    }
    if not task_ids:
        raise NativeRecurrenceAuthoringError("recurrence authoring requires exact task ids")

    series: dict[str, dict[str, Any]] = {}
    series_by_task: dict[int, set[str]] = {task_id: set() for task_id in task_ids}
    for result in _schedule_results(tool_trace):
        for block in result.get("blocks", []):
            if not isinstance(block, dict) or block.get("task_id") not in task_ids:
                continue
            series_id = str(block.get("recurrence_series_id") or "").strip()
            if not series_id:
                continue
            task_id = int(block["task_id"])
            series_by_task[task_id].add(series_id)
            series.setdefault(series_id, block)
    ambiguous = {
        task_id: sorted(ids)
        for task_id, ids in series_by_task.items()
        if len(ids) != 1
    }
    if ambiguous:
        raise NativeRecurrenceAuthoringError(
            f"each recurrence target must resolve to exactly one series: {ambiguous}"
        )

    provenance = Provenance(
        source_kind="user",
        source_ref=source_ref,
        source_text=source_text[:4000] or None,
    )
    clause_id = f"clause:recurrence-update:{_stable_suffix(source_ref, sorted(series))}"
    operations: list[RecurrenceUpdateSeriesOperation] = []
    refs: list[EntityRef] = []
    for series_id in sorted(series):
        block = series[series_id]
        current_rule = block.get("recurrence_rule")
        if not isinstance(current_rule, dict):
            raise NativeRecurrenceAuthoringError(
                f"series {series_id} has no authoritative recurrence rule"
            )
        updates: dict[str, Any] = {}
        for source_key, target_key in _HANDOFF_TO_RULE.items():
            if source_key not in requested_rule or requested_rule[source_key] is None:
                continue
            desired = requested_rule[source_key]
            current = current_rule.get(_EVIDENCE_RULE_KEYS.get(target_key, target_key))
            if desired != current:
                updates[target_key] = desired
        if not updates:
            raise NativeRecurrenceAuthoringError(
                f"series {series_id} already has the requested recurrence values"
            )
        revision = block.get("recurrence_series_revision")
        ref = EntityRef(
            kind="recurrence_series",
            entity_id=f"recurrence_series:{series_id}",
            revision=revision if isinstance(revision, int) else None,
            title=str(block.get("task_title") or "Repeating schedule")[:500],
        )
        operation = RecurrenceUpdateSeriesOperation(
            operation_id=(
                f"op:recurrence-update:{_stable_suffix(source_ref, series_id, updates)}"
            ),
            fulfills_clause_ids=[clause_id],
            provenance=provenance,
            series_ref=ref,
            updates=updates,
        )
        refs.append(ref)
        operations.append(operation)

    serialized_operations = [
        operation.model_dump(mode="json", by_alias=True) for operation in operations
    ]
    clause = {
        "clauseId": clause_id,
        "operationKind": "recurrence.update_series",
        "supportingOperationKinds": [],
        "cardinality": "exactly_one" if len(operations) == 1 else "one_or_more",
        "targetRefs": [ref.model_dump(mode="json", by_alias=True) for ref in refs],
        "expectedOperations": serialized_operations,
        "scope": "series",
        "disposition": "requested",
        "provenance": provenance.model_dump(mode="json", by_alias=True),
    }
    return NativeRecurrenceUpdateDraft(
        operations=tuple(operations),
        clause=clause,
        preconditions=Preconditions(
            expected_entity_revisions=[ref for ref in refs if ref.revision is not None]
        ),
        provenance=provenance,
    )
