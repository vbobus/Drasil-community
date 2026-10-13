"""Privacy-safe semantic comparison evidence for native and legacy plans."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.agent_kernel.contracts import (
    ChangeSet,
    ChangeSetSimulationResult,
    GoalState,
    OperationConservationReport,
)


DimensionStatus = Literal["matched", "divergent", "not_comparable"]

_LEGACY_DOMAIN_MAP = {
    "action": "task",
    "metadata": "task",
    "branch": "task",
    "preference_memory": "preference",
    "schedule_patch": "calendar",
}
_TARGET_KEYS = {
    "taskId", "task_id", "targetNodeId", "target_node_id", "nodeId", "node_id",
    "blockId", "block_id", "seriesId", "series_id", "recurrenceSeriesId",
    "entityId", "entity_id", "parentId", "parent_id",
    "destinationParentId", "destination_parent_id",
    "acceptedBlockId", "accepted_block_id",
}
_TARGET_KIND_BY_KEY = {
    "taskId": "task",
    "task_id": "task",
    "targetNodeId": "task",
    "target_node_id": "task",
    "nodeId": "task",
    "node_id": "task",
    "blockId": "calendar_block",
    "block_id": "calendar_block",
    "seriesId": "recurrence_series",
    "series_id": "recurrence_series",
    "recurrenceSeriesId": "recurrence_series",
    "parentId": "task",
    "parent_id": "task",
    "destinationParentId": "task",
    "destination_parent_id": "task",
    "acceptedBlockId": "calendar_block",
    "accepted_block_id": "calendar_block",
}
_TARGET_LIST_KIND_BY_KEY = {
    "targetNodeIds": "task",
    "target_node_ids": "task",
    "taskIds": "task",
    "task_ids": "task",
    "blockIds": "calendar_block",
    "block_ids": "calendar_block",
}
_TARGET_PREFIX_ALIASES = {
    "workspace": ("workspace:", "category:"),
    "task": ("task:", "node:"),
    "calendar_block": ("calendar_block:", "calendar-block:", "block:"),
    "recurrence_series": ("recurrence_series:", "recurrence-series:", "series:"),
    "recurrence_occurrence": (
        "recurrence_occurrence:",
        "recurrence-occurrence:",
        "occurrence:",
    ),
    "preference": ("preference:",),
}


def build_change_set_shadow_observation(
    *,
    change_set: ChangeSet,
    conservation: OperationConservationReport,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None = None,
    simulation: ChangeSetSimulationResult | None = None,
    legacy_timezone: str | None = None,
) -> dict[str, Any]:
    """Compare available semantic dimensions without compiling a legacy plan.

    Raw plans, identifiers, titles, notes, and provider prose are used only in memory.
    The returned event contains bounded categories, counts, and salted-independent hashes,
    so it is safe for aggregate rollout telemetry.
    """

    legacy_families = sorted(legacy_proposals)
    native_domains = sorted({operation.kind.split(".", 1)[0] for operation in change_set.operations})
    legacy_domains = _legacy_domains(legacy_proposals)
    native_targets = _target_values(
        [operation.model_dump(mode="json", by_alias=True) for operation in change_set.operations]
    )
    legacy_targets = _legacy_target_values(legacy_proposals)
    legacy_operation_count = sum(
        _legacy_operation_count(family, payload)
        for family, payload in legacy_proposals.items()
    )
    recurrence_plan_diagnostics = _legacy_recurrence_plan_diagnostics(
        legacy_proposals
    )

    adapter_dimensions = _metadata_comparison_dimensions(
        change_set=change_set,
        legacy_proposals=legacy_proposals,
        goal=goal,
        simulation=simulation,
        targets_match=native_targets == legacy_targets,
    )
    if not adapter_dimensions:
        adapter_dimensions = _recurring_branch_comparison_dimensions(
            change_set=change_set,
            legacy_proposals=legacy_proposals,
            goal=goal,
            simulation=simulation,
            targets_match=native_targets == legacy_targets,
            legacy_timezone=legacy_timezone,
        )
    if not adapter_dimensions:
        adapter_dimensions = _task_action_comparison_dimensions(
            change_set=change_set,
            legacy_proposals=legacy_proposals,
            goal=goal,
            simulation=simulation,
            targets_match=native_targets == legacy_targets,
        )
    if not adapter_dimensions:
        adapter_dimensions = _mixed_task_calendar_comparison_dimensions(
            change_set=change_set,
            legacy_proposals=legacy_proposals,
            goal=goal,
            simulation=simulation,
            targets_match=native_targets == legacy_targets,
        )
    if not adapter_dimensions:
        adapter_dimensions = _task_branch_comparison_dimensions(
            change_set=change_set,
            legacy_proposals=legacy_proposals,
            goal=goal,
            simulation=simulation,
            targets_match=native_targets == legacy_targets,
        )
    if not adapter_dimensions:
        adapter_dimensions = _calendar_comparison_dimensions(
            change_set=change_set,
            legacy_proposals=legacy_proposals,
            goal=goal,
            simulation=simulation,
            targets_match=native_targets == legacy_targets,
            legacy_timezone=legacy_timezone,
        )
    if not adapter_dimensions:
        adapter_dimensions = _preference_comparison_dimensions(
            change_set=change_set,
            legacy_proposals=legacy_proposals,
            goal=goal,
            simulation=simulation,
            targets_match=native_targets == legacy_targets,
        )
    dimensions = [
        _dimension("operation_families", set(native_domains) == set(legacy_domains),
                   "family_set_mismatch"),
        adapter_dimensions.get(
            "operation_cardinality",
            _dimension(
                "operation_cardinality",
                len(change_set.operations) == legacy_operation_count,
                "operation_cardinality_mismatch",
            ),
        ),
        adapter_dimensions.get(
            "target_bindings",
            _dimension(
                "target_bindings",
                native_targets == legacy_targets,
                "target_binding_mismatch",
            ),
        ),
        {
            "name": "goal_clause_conservation",
            "status": "matched" if conservation.complete else "divergent",
            "reasonCode": "complete" if conservation.complete else "clause_conservation_failed",
            "nativeCount": len(conservation.covered_clause_ids),
        },
        adapter_dimensions.get(
            "semantic_values", _unverified("semantic_values", "comparison_adapter_unavailable")
        ),
        adapter_dimensions.get(
            "dependencies_and_atomic_groups",
            _unverified("dependencies_and_atomic_groups", "comparison_adapter_unavailable"),
        ),
        adapter_dimensions.get(
            "constraints_and_scope",
            _unverified("constraints_and_scope", "comparison_adapter_unavailable"),
        ),
        adapter_dimensions.get(
            "decisions_and_corrections",
            _unverified("decisions_and_corrections", "legacy_contract_has_no_canonical_evidence"),
        ),
        adapter_dimensions.get(
            "simulation_and_conflicts",
            _unverified("simulation_and_conflicts", "legacy_contract_has_no_final_state_fingerprint"),
        ),
        adapter_dimensions.get(
            "review_meaning",
            _unverified("review_meaning", "legacy_card_copy_is_not_semantic_authority"),
        ),
        adapter_dimensions.get(
            "typed_failures",
            _unverified("typed_failures", "legacy_outcomes_are_not_canonical_typed_errors"),
        ),
    ]
    statuses = {item["status"] for item in dimensions}
    comparison_status = (
        "divergent" if "divergent" in statuses
        else "not_comparable" if "not_comparable" in statuses
        else "matched"
    )
    divergent_dimensions = [
        item["name"] for item in dimensions if item["status"] == "divergent"
    ]
    not_comparable_dimensions = [
        item["name"] for item in dimensions if item["status"] == "not_comparable"
    ]

    return {
        "contractVersion": "2.0",
        "comparisonStatus": comparison_status,
        "dimensions": dimensions,
        "divergentDimensions": divergent_dimensions,
        "notComparableDimensions": not_comparable_dimensions,
        "native": {
            "revision": change_set.revision,
            "status": change_set.status,
            "atomicByDefault": change_set.atomic_by_default,
            "domains": native_domains,
            "operationKinds": sorted({
                operation.kind for operation in change_set.operations
            }),
            "operationCount": len(change_set.operations),
            "constraintCount": len(change_set.constraints),
            "targetCount": len(native_targets),
            "targetSetSha256": _sha256(sorted(native_targets)),
            "semanticPayloadSha256": _sha256(_native_semantics(change_set)),
            "conservationComplete": conservation.complete,
        },
        "legacy": {
            "families": legacy_families,
            "domains": legacy_domains,
            "operationCount": legacy_operation_count,
            "targetCount": len(legacy_targets),
            "targetSetSha256": _sha256(sorted(legacy_targets)),
            "payloadSha256": _sha256(legacy_proposals),
            "remainsLiveAuthority": True,
            **(
                {"recurrencePlan": recurrence_plan_diagnostics}
                if recurrence_plan_diagnostics
                else {}
            ),
        },
        "comparisonLimitations": [
            "Legacy proposal payloads are never compiled into native operations for rollout.",
            "A not-comparable dimension blocks parity evidence; shape equality is not semantic parity.",
        ],
    }


def _legacy_recurrence_plan_diagnostics(
    legacy_proposals: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    branch = legacy_proposals.get("branch")
    if not isinstance(branch, dict):
        return {}
    nested = branch.get("payload") if isinstance(branch.get("payload"), dict) else {}
    plan = (
        branch.get("recurring_schedule_plan", branch.get("recurringSchedulePlan"))
        or nested.get("recurring_schedule_plan", nested.get("recurringSchedulePlan"))
    )
    if not isinstance(plan, dict):
        return {}

    def count(key: str, alias: str) -> int:
        value = plan.get(key, plan.get(alias, []))
        return len(value) if isinstance(value, list) else 0

    return {
        "status": str(plan.get("status") or "unknown")[:32],
        "occurrenceCount": count("occurrences", "occurrences"),
        "displacementCount": count(
            "displacement_operations", "displacementOperations"
        ),
        "conflictingDateCount": count("conflicting_dates", "conflictingDates"),
        "priorityConflictCount": count("priority_conflicts", "priorityConflicts"),
        "preferenceConflictDateCount": count(
            "preference_conflict_dates", "preferenceConflictDates"
        ),
        "errorCount": count("errors", "errors"),
    }


def _dimension(name: str, matches: bool, divergent_reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "matched" if matches else "divergent",
        "reasonCode": "equivalent" if matches else divergent_reason,
    }


def _unverified(name: str, reason: str) -> dict[str, Any]:
    return {"name": name, "status": "not_comparable", "reasonCode": reason}


def _legacy_domains(legacy_proposals: dict[str, dict[str, Any]]) -> list[str]:
    domains = {
        _LEGACY_DOMAIN_MAP[family]
        for family in legacy_proposals
        if family in _LEGACY_DOMAIN_MAP
    }
    branch = legacy_proposals.get("branch")
    if isinstance(branch, dict) and isinstance(
        branch.get("recurring_schedule", branch.get("recurringSchedule")), dict
    ):
        domains.add("recurrence")
    if isinstance(branch, dict) and _legacy_created_task_schedule_groups(branch):
        domains.add("calendar")
    return sorted(domains)


def _metadata_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
) -> dict[str, dict[str, Any]]:
    if set(legacy_proposals) != {"metadata"}:
        return {}
    legacy = legacy_proposals["metadata"]
    native_values = [
        _normalize_task_updates(
            operation.model_dump(mode="json", by_alias=True).get("updates", {})
        )
        for operation in change_set.operations
        if operation.kind == "task.update"
    ]
    legacy_updates = legacy.get("updates", legacy.get("metadata_updates", {}))
    legacy_values = (
        [_normalize_task_updates(legacy_updates)]
        if isinstance(legacy_updates, dict)
        else []
    )
    values_match = (
        len(native_values) == len(change_set.operations)
        and native_values == legacy_values
    )
    graph_match = all(
        not operation.depends_on and operation.atomic_group_id is None
        for operation in change_set.operations
    )
    constraints_match = not change_set.constraints and targets_match
    decision_match = (
        goal is not None
        and not goal.unresolved_decisions
        and not goal.resolved_decisions
        and all(
            {patch.path for patch in delta.patches} <= {"operations.requested"}
            for delta in goal.correction_deltas
        )
    )
    simulation_comparable = simulation is not None
    simulation_match = (
        simulation is not None
        and simulation.status == "ready_for_review"
        and simulation.conflict_graph.status in {"clear", "resolved"}
        and not simulation.issues
        and simulation.conservation.complete
    )
    review_match = values_match and targets_match and len(change_set.operations) == 1
    return {
        "semantic_values": _dimension(
            "semantic_values", values_match, "metadata_value_mismatch"
        ),
        "dependencies_and_atomic_groups": _dimension(
            "dependencies_and_atomic_groups", graph_match, "operation_graph_mismatch"
        ),
        "constraints_and_scope": _dimension(
            "constraints_and_scope", constraints_match, "constraint_or_scope_mismatch"
        ),
        "decisions_and_corrections": (
            _dimension(
                "decisions_and_corrections", decision_match, "decision_state_mismatch"
            )
            if goal is not None
            else _unverified("decisions_and_corrections", "goal_state_unavailable")
        ),
        "simulation_and_conflicts": (
            _dimension(
                "simulation_and_conflicts", simulation_match, "simulation_outcome_mismatch"
            )
            if simulation_comparable
            else _unverified("simulation_and_conflicts", "simulation_evidence_unavailable")
        ),
        "review_meaning": _dimension(
            "review_meaning", review_match, "review_semantics_mismatch"
        ),
        "typed_failures": (
            _dimension("typed_failures", not simulation.issues, "typed_failure_mismatch")
            if simulation is not None
            else _unverified("typed_failures", "typed_outcome_evidence_unavailable")
        ),
    }
def _normalize_task_updates(value: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "planned_date": "plannedDate",
        "deadline_at": "deadlineAt",
        "deadline": "deadlineAt",
        "due_date": "deadlineAt",
        "dueDate": "deadlineAt",
        "estimate_minutes": "estimateMinutes",
        "time_estimate_minutes": "estimateMinutes",
        "timeEstimateMinutes": "estimateMinutes",
        "clear_fields": "clearFields",
    }
    normalized: dict[str, Any] = {}
    for key, raw in value.items():
        canonical_key = aliases.get(key, key)
        if raw in (None, [], {}) and canonical_key != "clearFields":
            continue
        if canonical_key == "clearFields" and not raw:
            continue
        if canonical_key == "deadlineAt" and isinstance(raw, str):
            normalized[canonical_key] = raw[:10]
        elif canonical_key == "status":
            normalized[canonical_key] = {
                "todo": "not_started",
                "done": "completed",
            }.get(str(raw), raw)
        else:
            normalized[canonical_key] = raw
    return normalized


def _recurring_branch_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
    legacy_timezone: str | None,
) -> dict[str, dict[str, Any]]:
    if set(legacy_proposals) != {"branch"}:
        return {}
    branch = legacy_proposals["branch"]
    schedule = branch.get("recurring_schedule", branch.get("recurringSchedule"))
    nested_payload = branch.get("payload") if isinstance(branch.get("payload"), dict) else {}
    schedule_plan = (
        nested_payload.get(
            "recurring_schedule_plan", nested_payload.get("recurringSchedulePlan")
        )
        or branch.get("recurring_schedule_plan", branch.get("recurringSchedulePlan"))
    )
    operations = branch.get(
        "structural_operations", branch.get("structuralOperations", [])
    )
    if not isinstance(schedule, dict) or not isinstance(operations, list):
        return {}
    creates = [
        item
        for item in operations
        if isinstance(item, dict) and item.get("kind") == "create_node"
    ]
    native_creates = [item for item in change_set.operations if item.kind == "task.create"]
    native_series = [
        item for item in change_set.operations if item.kind == "recurrence.create_series"
    ]
    native_displacements = [
        item
        for item in change_set.operations
        if item.kind == "recurrence.update_occurrence"
    ]
    if (
        len(creates) != 1
        or len(native_creates) != 1
        or len(native_series) != 1
        or len(native_creates) + len(native_series) + len(native_displacements)
        != len(change_set.operations)
    ):
        return {}
    native_create = _native_task_branch_semantics(native_creates[0])
    legacy_create = _legacy_task_branch_semantics(creates[0])
    native_create_for_recurrence = _recurring_task_create_semantics(native_create)
    legacy_create_for_recurrence = _recurring_task_create_semantics(legacy_create)
    native_rule = _normalize_native_recurrence_rule(
        native_series[0].rule.model_dump(mode="json", by_alias=True)
    )
    legacy_rule = _normalize_legacy_recurrence_rule(
        schedule, fallback_timezone=legacy_timezone
    )
    displacement_source = schedule_plan if isinstance(schedule_plan, dict) else schedule
    legacy_displacements = displacement_source.get(
        "displacement_operations", displacement_source.get("displacementOperations", [])
    )
    if not isinstance(legacy_displacements, list) or any(
        not isinstance(item, dict) for item in legacy_displacements
    ):
        return {}
    native_displacement_values = [
        _native_recurrence_displacement_semantics(item, native_rule)
        for item in native_displacements
    ]
    legacy_displacement_values = [
        _legacy_recurrence_displacement_semantics(
            item,
            relation=displacement_source.get(
                "conflict_relocation", displacement_source.get("conflictRelocation")
            ),
            timezone_name=legacy_rule.get("timezone"),
        )
        for item in legacy_displacements
    ]
    displacement_targets_match = (
        all(item is not None for item in native_displacement_values)
        and all(item is not None for item in legacy_displacement_values)
        and sorted(item["occurrenceDate"] for item in native_displacement_values)
        == sorted(item["occurrenceDate"] for item in legacy_displacement_values)
    )
    displacements_match = (
        displacement_targets_match
        and _sorted_semantics(native_displacement_values)
        == _sorted_semantics(legacy_displacement_values)
    )
    native_binding_ok = (
        native_creates[0].temp_ref is not None
        and native_series[0].task_ref == native_creates[0].temp_ref
    )
    legacy_binding_ok = (
        str(creates[0].get("temp_id") or "").strip()
        == str(schedule.get("target_temp_id") or "").strip()
        != ""
    )
    values_match = (
        native_create_for_recurrence == legacy_create_for_recurrence
        and native_rule == legacy_rule
        and native_binding_ok
        and legacy_binding_ok
        and displacements_match
    )
    create_id = native_creates[0].operation_id
    graph_match = (
        native_series[0].depends_on == [create_id]
        and native_creates[0].atomic_group_id is not None
        and native_creates[0].atomic_group_id == native_series[0].atomic_group_id
    )
    dimensions = _successful_adapter_dimensions(
        prefix="recurrence",
        values_match=values_match,
        graph_match=graph_match,
        scope_match=not change_set.constraints and displacement_targets_match,
        targets_match=displacement_targets_match,
        goal=goal,
        simulation=simulation,
    )
    recurrence_mismatches = [
        name
        for name, matched in (
            ("task", native_create_for_recurrence == legacy_create_for_recurrence),
            ("rule", native_rule == legacy_rule),
            ("temporary_binding", native_binding_ok and legacy_binding_ok),
            ("displacement", displacements_match),
        )
        if not matched
    ]
    if recurrence_mismatches:
        if recurrence_mismatches == ["rule"]:
            rule_fields = [
                key
                for key in sorted(set(native_rule) | set(legacy_rule))
                if native_rule.get(key) != legacy_rule.get(key)
            ][:8]
            dimensions["semantic_values"]["reasonCode"] = (
                "recurrence_rule_"
                + "_and_".join(rule_fields)
                + "_mismatch"
            )
        else:
            mismatch_paths: list[str] = []
            if "task" in recurrence_mismatches:
                mismatch_paths.extend(
                    _semantic_difference_paths(
                        native_create_for_recurrence,
                        legacy_create_for_recurrence,
                        prefix="task",
                    )
                )
            if "temporary_binding" in recurrence_mismatches:
                mismatch_paths.append("temporary_binding")
            if "displacement" in recurrence_mismatches:
                native_by_date = {
                    item["occurrenceDate"]: item
                    for item in native_displacement_values
                    if isinstance(item, dict) and item.get("occurrenceDate")
                }
                legacy_by_date = {
                    item["occurrenceDate"]: item
                    for item in legacy_displacement_values
                    if isinstance(item, dict) and item.get("occurrenceDate")
                }
                if set(native_by_date) != set(legacy_by_date):
                    mismatch_paths.append("displacement_occurrenceDate")
                else:
                    displacement_fields = sorted({
                        key
                        for occurrence_date in native_by_date
                        for key in set(native_by_date[occurrence_date])
                        | set(legacy_by_date[occurrence_date])
                        if native_by_date[occurrence_date].get(key)
                        != legacy_by_date[occurrence_date].get(key)
                    })
                    for key in displacement_fields[:4]:
                        if key not in {"start", "end"}:
                            mismatch_paths.append(f"displacement_{key}")
                            continue
                        native_missing = sum(
                            native_by_date[occurrence_date].get(key) is None
                            for occurrence_date in native_by_date
                        )
                        legacy_missing = sum(
                            legacy_by_date[occurrence_date].get(key) is None
                            for occurrence_date in legacy_by_date
                        )
                        if native_missing or legacy_missing:
                            mismatch_paths.append(
                                f"displacement_{key}_missing_native{native_missing}_"
                                f"legacy{legacy_missing}"
                            )
                            continue
                        deltas: set[int] = set()
                        native_invalid = 0
                        legacy_invalid = 0
                        for occurrence_date in native_by_date:
                            native_raw = native_by_date[occurrence_date].get(key)
                            legacy_raw = legacy_by_date[occurrence_date].get(key)
                            try:
                                native_timestamp = datetime.fromisoformat(str(native_raw))
                            except ValueError:
                                native_invalid += 1
                                continue
                            try:
                                legacy_timestamp = datetime.fromisoformat(str(legacy_raw))
                            except ValueError:
                                legacy_invalid += 1
                                continue
                            deltas.add(
                                int(
                                    (native_timestamp - legacy_timestamp).total_seconds()
                                    // 60
                                )
                            )
                        if native_invalid or legacy_invalid:
                            mismatch_paths.append(
                                f"displacement_{key}_invalid_native{native_invalid}_"
                                f"legacy{legacy_invalid}"
                            )
                            continue
                        if len(deltas) == 1:
                            delta = next(iter(deltas))
                            direction = "later" if delta > 0 else "earlier"
                            mismatch_paths.append(
                                f"displacement_{key}_native_{direction}_{abs(delta)}m"
                            )
                        elif deltas and len(deltas) <= 4:
                            encoded = "_".join(
                                (
                                    f"later{delta}m"
                                    if delta > 0
                                    else f"earlier{abs(delta)}m"
                                    if delta < 0
                                    else "same"
                                )
                                for delta in sorted(deltas)
                            )
                            mismatch_paths.append(
                                f"displacement_{key}_native_{encoded}"
                            )
                        elif deltas:
                            mismatch_paths.append(
                                f"displacement_{key}_native_delta_min{min(deltas)}m_"
                                f"max{max(deltas)}m_count{len(deltas)}"
                            )
                        else:
                            mismatch_paths.append(f"displacement_{key}_varied")
            bounded_paths = list(dict.fromkeys(mismatch_paths))[:8]
            dimensions["semantic_values"]["reasonCode"] = (
                "recurrence_"
                + "_and_".join(bounded_paths or recurrence_mismatches)
                + "_mismatch"
            )
    dimensions["operation_cardinality"] = _dimension(
        "operation_cardinality",
        len(change_set.operations) == 2 + len(legacy_displacements),
        "operation_cardinality_mismatch",
    )
    dimensions["target_bindings"] = _dimension(
        "target_bindings",
        displacement_targets_match,
        "recurrence_occurrence_target_mismatch",
    )
    decision_match = (
        goal is not None
        and not goal.unresolved_decisions
        and all(
            {patch.path for patch in delta.patches} <= {"operations.requested"}
            for delta in goal.correction_deltas
        )
        and (
            not native_displacements
            or (
                bool(goal.resolved_decisions)
                and change_set.source_simulation_id is not None
                and change_set.resolution_alternative_id is not None
            )
        )
    )
    dimensions["decisions_and_corrections"] = (
        _dimension(
            "decisions_and_corrections",
            decision_match,
            "recurrence_decision_trajectory_mismatch",
        )
        if goal is not None
        else _unverified("decisions_and_corrections", "goal_state_unavailable")
    )
    return dimensions


def _native_recurrence_displacement_semantics(
    operation: Any, rule: dict[str, Any]
) -> dict[str, Any] | None:
    occurrence_date = str(operation.occurrence_ref.entity_id).rsplit(":", 1)[-1]
    start_at = operation.updates.start_at
    end_at = operation.updates.end_at
    preferred_time = rule.get("preferredStartTime")
    duration = rule.get("durationMinutes")
    timezone_name = rule.get("timezone")
    if not (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", occurrence_date)
        and start_at is not None
        and end_at is not None
        and preferred_time
        and duration
        and timezone_name
    ):
        return None
    try:
        zone = ZoneInfo(str(timezone_name))
        incoming_start = datetime.fromisoformat(
            f"{occurrence_date}T{str(preferred_time)}"
        ).replace(tzinfo=zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    incoming_end = incoming_start + timedelta(minutes=int(duration))
    relation = (
        "before_incoming"
        if end_at <= incoming_start
        else "after_incoming"
        if start_at >= incoming_end
        else None
    )
    if relation is None:
        return None
    return {
        "occurrenceDate": occurrence_date,
        "start": _normalized_calendar_timestamp(start_at),
        "end": _normalized_calendar_timestamp(end_at),
        "relation": relation,
    }


def _recurring_task_create_semantics(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Exclude compiler defaults duplicated by the recurrence/task persistence layer."""

    if value is None:
        return None
    fields = value.get("fields")
    normalized_fields = (
        {key: raw for key, raw in fields.items() if key != "estimateMinutes"}
        if isinstance(fields, dict)
        else fields
    )
    if (
        isinstance(normalized_fields, dict)
        and normalized_fields.get("status") == "not_started"
    ):
        normalized_fields.pop("status")
    return {**value, "fields": normalized_fields}


def _legacy_recurrence_displacement_semantics(
    operation: dict[str, Any], *, relation: Any, timezone_name: Any
) -> dict[str, Any] | None:
    start_raw = operation.get("startAt", operation.get("start_at"))
    end_raw = operation.get("endAt", operation.get("end_at"))
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return None
    occurrence_date = start_raw[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", occurrence_date):
        return None
    return {
        "occurrenceDate": occurrence_date,
        "start": _normalized_calendar_timestamp(
            start_raw, naive_timezone=str(timezone_name or "") or None
        ),
        "end": _normalized_calendar_timestamp(
            end_raw, naive_timezone=str(timezone_name or "") or None
        ),
        "relation": relation,
    }


def _normalize_legacy_recurrence_rule(
    value: dict[str, Any], *, fallback_timezone: str | None = None
) -> dict[str, Any]:
    end_mode = value.get("end_mode", value.get("endMode"))
    has_semantic_end = "series_end_date" in value or "seriesEndDate" in value
    semantic_end = (
        value.get("series_end_date", value.get("seriesEndDate"))
        if has_semantic_end
        else None
        if end_mode == "never"
        else value.get("end_date", value.get("endDate"))
    )
    occurrence_count = (
        None
        if end_mode == "never" or has_semantic_end and semantic_end is None
        else value.get("occurrence_count", value.get("occurrenceCount"))
    )
    return _normalize_native_recurrence_rule({
        "frequency": value.get("frequency"),
        "interval": value.get("interval", 1),
        "weekdays": value.get("days_of_week", value.get("daysOfWeek", [])),
        "monthDays": value.get("month_days", value.get("monthDays", [])),
        "startDate": value.get("start_date", value.get("startDate")),
        "endDate": semantic_end,
        "occurrenceCount": occurrence_count,
        "timezone": value.get("timezone") or fallback_timezone,
        "preferredStartTime": value.get(
            "preferred_start_time", value.get("preferredStartTime")
        ),
        "durationMinutes": value.get(
            "duration_minutes", value.get("durationMinutes")
        ),
    })


def _normalize_native_recurrence_rule(value: dict[str, Any]) -> dict[str, Any]:
    preferred = value.get("preferredStartTime")
    return {
        "frequency": value.get("frequency"),
        "interval": value.get("interval", 1),
        "weekdays": sorted(value.get("weekdays") or []),
        "monthDays": sorted(value.get("monthDays") or []),
        "startDate": value.get("startDate"),
        "endDate": value.get("endDate"),
        "occurrenceCount": value.get("occurrenceCount"),
        "timezone": value.get("timezone"),
        "preferredStartTime": (
            str(preferred)[:5] if preferred is not None else None
        ),
        "durationMinutes": value.get("durationMinutes"),
    }


def _legacy_created_task_schedule_groups(
    branch: dict[str, Any],
) -> list[dict[str, Any]]:
    schedule = branch.get(
        "created_task_schedule", branch.get("createdTaskSchedule")
    )
    groups = (
        schedule.get("schedule_groups", schedule.get("scheduleGroups"))
        if isinstance(schedule, dict)
        else None
    )
    return (
        [item for item in groups if isinstance(item, dict)]
        if isinstance(groups, list)
        else []
    )


def _mixed_task_calendar_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
) -> dict[str, dict[str, Any]]:
    """Compare created tasks plus selected one-time calendar placements.

    The compatibility contract nests its calendar groups inside the branch
    proposal. Temp IDs are local to each authoring path, so relationship
    bindings are compared through the normalized titles they identify.
    """

    if set(legacy_proposals) != {"branch"}:
        return {}
    legacy = legacy_proposals["branch"]
    schedule_groups = _legacy_created_task_schedule_groups(legacy)
    if not schedule_groups:
        return {}
    legacy_operations = legacy.get(
        "structural_operations", legacy.get("structuralOperations", [])
    )
    if not isinstance(legacy_operations, list) or not legacy_operations:
        return {}
    legacy_creates = [
        item
        for item in legacy_operations
        if isinstance(item, dict) and item.get("kind") == "create_node"
    ]
    if len(legacy_creates) != len(legacy_operations):
        return {}

    native_creates = [
        operation
        for operation in change_set.operations
        if operation.kind == "task.create"
    ]
    native_calendar = [
        operation
        for operation in change_set.operations
        if operation.kind == "calendar.create_block"
    ]
    if len(native_creates) + len(native_calendar) != len(change_set.operations):
        return {}

    native_temp_titles = {
        operation.temp_ref.entity_id: _normalize_semantic_text(operation.title)
        for operation in native_creates
        if operation.temp_ref is not None
    }
    legacy_temp_titles = {
        str(item.get("temp_id", item.get("tempId")) or "").strip():
            _normalize_semantic_text(item.get("title"))
        for item in legacy_creates
        if str(item.get("temp_id", item.get("tempId")) or "").strip()
    }
    scheduled_native_titles = {
        native_temp_titles.get(operation.task_ref.entity_id)
        for operation in native_calendar
        if operation.task_ref.kind == "temporary"
    }
    scheduled_legacy_titles = {
        legacy_temp_titles.get(str(temp_id).strip())
        for group in schedule_groups
        for temp_id in _legacy_schedule_group_target_temp_ids(group)
    }

    native_values = [
        _native_mixed_created_task_semantics(
            operation,
            temp_titles=native_temp_titles,
            scheduled_titles=scheduled_native_titles,
        )
        for operation in native_creates
    ] + [
        _native_mixed_calendar_semantics(
            operation, temp_titles=native_temp_titles
        )
        for operation in native_calendar
    ]
    legacy_values = [
        _legacy_mixed_created_task_semantics(
            operation,
            temp_titles=legacy_temp_titles,
            scheduled_titles=scheduled_legacy_titles,
        )
        for operation in legacy_creates
    ] + [
        value
        for group in schedule_groups
        for value in _legacy_mixed_calendar_semantics(
            group, temp_titles=legacy_temp_titles
        )
    ]
    values_match = (
        all(item is not None for item in native_values + legacy_values)
        and _sorted_semantics(native_values) == _sorted_semantics(legacy_values)
    )
    operation_ids = {operation.operation_id for operation in change_set.operations}
    native_temp_producers = {
        operation.temp_ref.entity_id: operation.operation_id
        for operation in native_creates
        if operation.temp_ref is not None
    }
    required_temp_dependencies = [
        (
            operation,
            native_temp_producers.get(operation.task_ref.entity_id),
        )
        for operation in native_calendar
        if operation.task_ref.kind == "temporary"
    ] + [
        (
            operation,
            native_temp_producers.get(operation.parent_ref.entity_id),
        )
        for operation in native_creates
        if operation.parent_ref is not None
        and operation.parent_ref.kind == "temporary"
    ]
    graph_match = (
        all(set(operation.depends_on) <= operation_ids for operation in change_set.operations)
        and all(
            producer_id is not None and producer_id in operation.depends_on
            for operation, producer_id in required_temp_dependencies
        )
    )
    dimensions = _successful_adapter_dimensions(
        prefix="mixed_task_calendar",
        values_match=values_match,
        graph_match=graph_match,
        scope_match=not change_set.constraints and targets_match,
        targets_match=targets_match,
        goal=goal,
        simulation=simulation,
    )
    dimensions["operation_cardinality"] = _dimension(
        "operation_cardinality",
        len(native_values) == len(legacy_values),
        "operation_cardinality_mismatch",
    )
    if not values_match:
        native_ordered = sorted(
            native_values,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
        legacy_ordered = sorted(
            legacy_values,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
        paths = _semantic_difference_paths(
            native_ordered,
            legacy_ordered,
            prefix="mixed_task_calendar",
        )
        dimensions["semantic_values"]["reasonCode"] = (
            "_and_".join(paths) + "_mismatch"
            if paths
            else "mixed_task_calendar_value_mismatch"
        )
    return dimensions


def _legacy_schedule_group_target_temp_ids(group: dict[str, Any]) -> list[str]:
    raw = group.get("target_temp_ids", group.get("targetTempIds", []))
    return [str(item).strip() for item in raw] if isinstance(raw, list) else []


def _mixed_parent_identity(ref: Any, *, temp_titles: dict[str, str]) -> str | None:
    if ref is None:
        return None
    if getattr(ref, "kind", None) == "temporary":
        return temp_titles.get(str(ref.entity_id))
    return _ref_identity("task", ref)


def _native_mixed_created_task_semantics(
    operation: Any,
    *,
    temp_titles: dict[str, str],
    scheduled_titles: set[str | None],
) -> dict[str, Any]:
    title = _normalize_semantic_text(operation.title)
    fields = _normalize_task_updates(
        operation.fields.model_dump(mode="json", by_alias=True)
    )
    if title in scheduled_titles:
        fields.pop("estimateMinutes", None)
    return {
        "kind": "create",
        "title": title,
        "workspace": _ref_identity("workspace", operation.workspace_ref),
        "parent": _mixed_parent_identity(
            operation.parent_ref, temp_titles=temp_titles
        ),
        "fields": fields,
    }


def _legacy_mixed_created_task_semantics(
    operation: dict[str, Any],
    *,
    temp_titles: dict[str, str],
    scheduled_titles: set[str | None],
) -> dict[str, Any]:
    title = _normalize_semantic_text(operation.get("title"))
    fields = {
        key: raw
        for key, raw in operation.items()
        if key in {
            "notes", "priority", "status", "estimate_minutes",
            "time_estimate_minutes", "planned_date", "deadline_at", "deadline",
        }
    }
    normalized_fields = _normalize_task_updates(fields)
    if title in scheduled_titles:
        normalized_fields.pop("estimateMinutes", None)
    parent_temp_id = str(
        operation.get("parent_temp_id", operation.get("parentTempId")) or ""
    ).strip()
    parent = (
        temp_titles.get(parent_temp_id)
        if parent_temp_id
        else _optional_legacy_identity(
            operation.get("parent_id", operation.get("parentId"))
        )
    )
    return {
        "kind": "create",
        "title": title,
        "workspace": str(
            operation.get("category_id", operation.get("categoryId"))
        ).strip(),
        "parent": parent,
        "fields": normalized_fields,
    }


def _local_wall_time(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")[:19]
    if isinstance(value, str) and value.strip():
        return value.strip()[:19]
    return None


def _native_mixed_calendar_semantics(
    operation: Any,
    *,
    temp_titles: dict[str, str],
) -> dict[str, Any] | None:
    if operation.task_ref.kind != "temporary":
        return None
    return {
        "kind": "calendar.create",
        "task": temp_titles.get(operation.task_ref.entity_id),
        "startAt": _local_wall_time(operation.start_at),
        "endAt": _local_wall_time(operation.end_at),
    }


def _legacy_mixed_calendar_semantics(
    group: dict[str, Any],
    *,
    temp_titles: dict[str, str],
) -> list[dict[str, Any]]:
    start_at = _local_wall_time(
        group.get("window_start", group.get("windowStart"))
    )
    end_at = _local_wall_time(
        group.get("window_end", group.get("windowEnd"))
    )
    return [
        {
            "kind": "calendar.create",
            "task": temp_titles.get(temp_id),
            "startAt": start_at,
            "endAt": end_at,
        }
        for temp_id in _legacy_schedule_group_target_temp_ids(group)
    ]


def _task_branch_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
) -> dict[str, dict[str, Any]]:
    if set(legacy_proposals) != {"branch"}:
        return {}
    legacy = legacy_proposals["branch"]
    legacy_operations = legacy.get(
        "structural_operations", legacy.get("structuralOperations", [])
    )
    supported_kinds = {
        "create_node", "metadata_update", "move_subtree", "archive_node", "delete_node"
    }
    if not isinstance(legacy_operations, list) or not legacy_operations or any(
        not isinstance(item, dict) or item.get("kind") not in supported_kinds
        for item in legacy_operations
    ):
        return {}
    if any(
        item.get("kind") == "create_node"
        and
        item.get("category_id", item.get("categoryId")) is None
        for item in legacy_operations
    ):
        return {}
    native_operations = list(change_set.operations)
    native_values = [
        semantic
        for item in native_operations
        for semantic in _expanded_native_task_branch_semantics(item)
    ]
    legacy_values = [_legacy_task_branch_semantics(item) for item in legacy_operations]
    values_match = (
        all(item is not None for item in native_values)
        and _sorted_semantics(native_values) == _sorted_semantics(legacy_values)
    )
    operation_ids = {operation.operation_id for operation in native_operations}
    graph_match = (
        len({operation.atomic_group_id for operation in native_operations}) == 1
        and all(set(operation.depends_on) <= operation_ids for operation in native_operations)
    )
    scope_match = (
        not change_set.constraints
        and targets_match
    )
    decision_match = (
        goal is not None
        and not goal.unresolved_decisions
        and not goal.resolved_decisions
        and all(
            {patch.path for patch in delta.patches} <= {"operations.requested"}
            for delta in goal.correction_deltas
        )
    )
    simulation_match = (
        simulation is not None
        and simulation.status == "ready_for_review"
        and simulation.conflict_graph.status in {"clear", "resolved"}
        and not simulation.issues
        and simulation.conservation.complete
    )
    review_match = values_match and targets_match and scope_match
    dimensions = {
        "operation_cardinality": _dimension(
            "operation_cardinality",
            len(native_values) == len(legacy_values),
            "operation_cardinality_mismatch",
        ),
        "semantic_values": _dimension(
            "semantic_values", values_match, "task_branch_value_mismatch"
        ),
        "dependencies_and_atomic_groups": _dimension(
            "dependencies_and_atomic_groups", graph_match, "task_branch_graph_mismatch"
        ),
        "constraints_and_scope": _dimension(
            "constraints_and_scope", scope_match, "task_branch_scope_mismatch"
        ),
        "decisions_and_corrections": (
            _dimension("decisions_and_corrections", decision_match, "decision_state_mismatch")
            if goal is not None
            else _unverified("decisions_and_corrections", "goal_state_unavailable")
        ),
        "simulation_and_conflicts": (
            _dimension(
                "simulation_and_conflicts", simulation_match, "simulation_outcome_mismatch"
            )
            if simulation is not None
            else _unverified("simulation_and_conflicts", "simulation_evidence_unavailable")
        ),
        "review_meaning": _dimension(
            "review_meaning", review_match, "review_semantics_mismatch"
        ),
        "typed_failures": (
            _dimension("typed_failures", not simulation.issues, "typed_failure_mismatch")
            if simulation is not None
            else _unverified("typed_failures", "typed_outcome_evidence_unavailable")
        ),
    }
    if not values_match:
        native_ordered = sorted(
            native_values,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
        legacy_ordered = sorted(
            legacy_values,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
        paths = _semantic_difference_paths(
            native_ordered,
            legacy_ordered,
            prefix="task_branch",
        )
        dimensions["semantic_values"]["reasonCode"] = (
            "_and_".join(paths) + "_mismatch"
            if paths
            else "task_branch_value_mismatch"
        )
    return dimensions


def _task_action_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
) -> dict[str, dict[str, Any]]:
    """Compare the legacy hard-delete action envelope without parsing its prose."""

    if set(legacy_proposals) != {"action"}:
        return {}
    legacy = legacy_proposals["action"]
    structured_kinds = {
        str(legacy.get(key) or "").strip().lower()
        for key in ("kind", "proposal_kind", "proposalKind", "type", "operation", "action")
    }
    if not structured_kinds.intersection({"delete", "delete_node", "hard_delete", "remove_node"}):
        return {}
    raw_targets = legacy.get("target_node_ids", legacy.get("targetNodeIds"))
    if raw_targets is None:
        raw_targets = [legacy.get("target_node_id", legacy.get("targetNodeId"))]
    if not isinstance(raw_targets, list) or not raw_targets or any(
        item is None for item in raw_targets
    ):
        return {}
    legacy_values = [
        {"kind": "delete", "tasks": [str(item).strip()]}
        for item in raw_targets
    ]
    native_values = [
        semantic
        for operation in change_set.operations
        for semantic in _expanded_native_task_branch_semantics(operation)
    ]
    supported_native = all(operation.kind == "task.delete" for operation in change_set.operations)
    values_match = (
        supported_native
        and all(item is not None for item in native_values)
        and _sorted_semantics(native_values) == _sorted_semantics(legacy_values)
    )
    operation_ids = {operation.operation_id for operation in change_set.operations}
    graph_match = (
        supported_native
        and len({operation.atomic_group_id for operation in change_set.operations}) == 1
        and all(set(operation.depends_on) <= operation_ids for operation in change_set.operations)
    )
    scope_match = not change_set.constraints and targets_match
    decision_match = (
        goal is not None
        and not goal.unresolved_decisions
        and not goal.resolved_decisions
        and all(
            {patch.path for patch in delta.patches} <= {"operations.requested"}
            for delta in goal.correction_deltas
        )
    )
    simulation_match = (
        simulation is not None
        and simulation.status == "ready_for_review"
        and simulation.conflict_graph.status in {"clear", "resolved"}
        and not simulation.issues
        and simulation.conservation.complete
    )
    return {
        "operation_cardinality": _dimension(
            "operation_cardinality",
            len(native_values) == len(legacy_values),
            "operation_cardinality_mismatch",
        ),
        "target_bindings": _dimension(
            "target_bindings", targets_match, "task_delete_target_mismatch"
        ),
        "semantic_values": _dimension(
            "semantic_values", values_match, "task_delete_value_mismatch"
        ),
        "dependencies_and_atomic_groups": _dimension(
            "dependencies_and_atomic_groups", graph_match, "task_delete_graph_mismatch"
        ),
        "constraints_and_scope": _dimension(
            "constraints_and_scope", scope_match, "task_delete_scope_mismatch"
        ),
        "decisions_and_corrections": (
            _dimension("decisions_and_corrections", decision_match, "decision_state_mismatch")
            if goal is not None
            else _unverified("decisions_and_corrections", "goal_state_unavailable")
        ),
        "simulation_and_conflicts": (
            _dimension(
                "simulation_and_conflicts", simulation_match, "simulation_outcome_mismatch"
            )
            if simulation is not None
            else _unverified("simulation_and_conflicts", "simulation_evidence_unavailable")
        ),
        "review_meaning": _dimension(
            "review_meaning", values_match and scope_match, "review_semantics_mismatch"
        ),
        "typed_failures": (
            _dimension("typed_failures", not simulation.issues, "typed_failure_mismatch")
            if simulation is not None
            else _unverified("typed_failures", "typed_outcome_evidence_unavailable")
        ),
    }


def _ref_identity(kind: str, ref: Any) -> str | None:
    if ref is None:
        return None
    return _strip_target_prefix(kind, ref.entity_id)


def _native_task_branch_semantics(operation: Any) -> dict[str, Any] | None:
    if operation.kind == "task.create":
        return {
            "kind": "create",
            "title": _normalize_semantic_text(operation.title),
            "workspace": _ref_identity("workspace", operation.workspace_ref),
            "parent": _ref_identity("task", operation.parent_ref),
            "fields": _normalize_task_updates(
                operation.fields.model_dump(mode="json", by_alias=True)
            ),
        }
    if operation.kind == "task.update":
        return {
            "kind": "update",
            "tasks": [_ref_identity("task", operation.task_ref)],
            "fields": _normalize_task_updates(
                operation.updates.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            ),
        }
    if operation.kind == "task.move":
        return {
            "kind": "move",
            "tasks": sorted(_ref_identity("task", ref) for ref in operation.task_refs),
            "workspace": _ref_identity("workspace", operation.destination_workspace_ref),
            "parent": _ref_identity("task", operation.destination_parent_ref),
        }
    if operation.kind in {"task.archive", "task.delete"}:
        return {
            "kind": operation.kind.split(".", 1)[1],
            "tasks": sorted(_ref_identity("task", ref) for ref in operation.task_refs),
        }
    return None


def _expanded_native_task_branch_semantics(operation: Any) -> list[dict[str, Any] | None]:
    """Expand canonical bulk task operations into legacy-equivalent semantic units."""

    semantic = _native_task_branch_semantics(operation)
    if semantic is None:
        return [None]
    tasks = semantic.get("tasks")
    if semantic.get("kind") in {"move", "archive", "delete"} and isinstance(tasks, list):
        return [{**semantic, "tasks": [task]} for task in tasks]
    return [semantic]


def _sorted_semantics(values: list[dict[str, Any] | None]) -> list[str]:
    return sorted(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in values
    )


def _semantic_difference_paths(
    left: Any,
    right: Any,
    *,
    prefix: str,
    limit: int = 8,
) -> list[str]:
    """Return bounded field paths only; never include compared private values."""

    differences: list[str] = []

    def visit(first: Any, second: Any, path: str) -> None:
        if len(differences) >= limit or first == second:
            return
        if isinstance(first, dict) and isinstance(second, dict):
            for key in sorted(set(first) | set(second)):
                visit(first.get(key), second.get(key), f"{path}_{key}")
                if len(differences) >= limit:
                    break
            return
        if isinstance(first, list) and isinstance(second, list):
            if len(first) != len(second):
                differences.append(f"{path}_cardinality")
                if len(differences) >= limit:
                    return
            for index, (first_item, second_item) in enumerate(zip(first, second)):
                visit(first_item, second_item, f"{path}_{index}")
                if len(differences) >= limit:
                    break
            return
        if path not in differences:
            differences.append(path)

    visit(left, right, prefix)
    return differences


def _legacy_task_branch_semantics(operation: dict[str, Any]) -> dict[str, Any] | None:
    kind = operation.get("kind")
    if kind == "create_node":
        fields = {
            key: raw
            for key, raw in operation.items()
            if key in {
                "notes", "priority", "status", "estimate_minutes",
                "time_estimate_minutes", "planned_date", "deadline_at", "deadline",
            }
        }
        return {
            "kind": "create",
            "title": _normalize_semantic_text(operation.get("title")),
            "workspace": str(
                operation.get("category_id", operation.get("categoryId"))
            ).strip(),
            "parent": _optional_legacy_identity(
                operation.get("parent_id", operation.get("parentId"))
            ),
            "fields": _normalize_task_updates(fields),
        }
    if kind == "metadata_update":
        return {
            "kind": "update",
            "tasks": [_optional_legacy_identity(operation.get("node_id"))],
            "fields": _normalize_task_updates(operation.get("updates", {})),
        }
    if kind == "move_subtree":
        return {
            "kind": "move",
            "tasks": [_optional_legacy_identity(operation.get("node_id"))],
            "workspace": _optional_legacy_identity(
                operation.get("destination_category_id")
            ),
            "parent": _optional_legacy_identity(
                operation.get("destination_parent_id")
            ),
        }
    if kind in {"archive_node", "delete_node"}:
        return {
            "kind": "archive" if kind == "archive_node" else "delete",
            "tasks": [_optional_legacy_identity(operation.get("node_id"))],
        }
    return None


def _optional_legacy_identity(value: Any) -> str | None:
    return None if value is None else str(value).strip()


def _calendar_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
    legacy_timezone: str | None,
) -> dict[str, dict[str, Any]]:
    if set(legacy_proposals) != {"schedule_patch"}:
        return {}
    proposal = legacy_proposals["schedule_patch"]
    patch = proposal.get("schedulePatch", proposal.get("schedule_patch", proposal))
    legacy_operations = patch.get("operations") if isinstance(patch, dict) else None
    if not isinstance(legacy_operations, list) or not legacy_operations:
        return {}
    native_values = [_native_calendar_semantics(item) for item in change_set.operations]
    legacy_values = [
        _legacy_calendar_semantics(item, legacy_timezone=legacy_timezone)
        for item in legacy_operations
    ]
    values_comparable = all(
        _calendar_semantics_comparable(item)
        for item in native_values + legacy_values
    )
    values_match = (
        values_comparable
        and native_values == legacy_values
    )
    operation_ids = {operation.operation_id for operation in change_set.operations}
    graph_match = (
        len({operation.atomic_group_id for operation in change_set.operations}) == 1
        and all(set(operation.depends_on) <= operation_ids for operation in change_set.operations)
    )
    dimensions = _successful_adapter_dimensions(
        prefix="calendar",
        values_match=values_match,
        graph_match=graph_match,
        scope_match=not change_set.constraints and targets_match,
        targets_match=targets_match,
        goal=goal,
        simulation=simulation,
    )
    if not values_comparable:
        dimensions["semantic_values"] = _unverified(
            "semantic_values", "calendar_timezone_context_unavailable"
        )
    return dimensions


def _native_calendar_semantics(operation: Any) -> dict[str, Any] | None:
    if operation.kind == "calendar.create_block":
        return {
            "kind": "create",
            "task": _ref_identity("task", operation.task_ref),
            "start": _normalized_calendar_timestamp(operation.start_at),
            "end": _normalized_calendar_timestamp(operation.end_at),
        }
    if operation.kind == "calendar.move_block":
        return {
            "kind": "move",
            "block": _ref_identity("calendar_block", operation.block_ref),
            "start": _normalized_calendar_timestamp(operation.start_at),
            "end": _normalized_calendar_timestamp(operation.end_at),
        }
    if operation.kind == "calendar.resize_block":
        return {
            "kind": "resize",
            "block": _ref_identity("calendar_block", operation.block_ref),
            "start": _normalized_calendar_timestamp(operation.start_at),
            "end": _normalized_calendar_timestamp(operation.end_at),
        }
    if operation.kind == "calendar.delete_block":
        return {
            "kind": "delete",
            "blocks": sorted(
                _ref_identity("calendar_block", ref) for ref in operation.block_refs
            ),
        }
    return None


def _legacy_calendar_semantics(
    operation: Any, *, legacy_timezone: str | None
) -> dict[str, Any] | None:
    if not isinstance(operation, dict):
        return None
    kind = operation.get("op")
    if kind == "create_block":
        return {
            "kind": "create",
            "task": _optional_legacy_identity(operation.get("taskId")),
            "start": _normalized_calendar_timestamp(
                operation.get("startAt"), naive_timezone=legacy_timezone
            ),
            "end": _normalized_calendar_timestamp(
                operation.get("endAt"), naive_timezone=legacy_timezone
            ),
        }
    if kind == "update_block_time":
        return {
            "kind": "move",
            "block": _optional_legacy_identity(operation.get("acceptedBlockId")),
            "start": _normalized_calendar_timestamp(
                operation.get("startAt"), naive_timezone=legacy_timezone
            ),
            "end": _normalized_calendar_timestamp(
                operation.get("endAt"), naive_timezone=legacy_timezone
            ),
        }
    if kind == "remove_block":
        return {
            "kind": "delete",
            "blocks": [_optional_legacy_identity(operation.get("acceptedBlockId"))],
        }
    return None


def _normalized_calendar_timestamp(
    value: Any, *, naive_timezone: str | None = None
) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not naive_timezone:
            return None
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(naive_timezone))
        except ZoneInfoNotFoundError:
            return None
    return parsed.astimezone(timezone.utc).isoformat()


def _calendar_semantics_comparable(value: dict[str, Any] | None) -> bool:
    if value is None:
        return False
    if value.get("kind") in {"create", "move"}:
        return value.get("start") is not None and value.get("end") is not None
    if value.get("kind") == "resize":
        return value.get("end") is not None
    return True


def _successful_adapter_dimensions(
    *,
    prefix: str,
    values_match: bool,
    graph_match: bool,
    scope_match: bool,
    targets_match: bool,
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
) -> dict[str, dict[str, Any]]:
    decision_match = (
        goal is not None
        and not goal.unresolved_decisions
        and not goal.resolved_decisions
        and all(
            {patch.path for patch in delta.patches} <= {"operations.requested"}
            for delta in goal.correction_deltas
        )
    )
    simulation_match = (
        simulation is not None
        and simulation.status == "ready_for_review"
        and simulation.conflict_graph.status in {"clear", "resolved"}
        and not simulation.issues
        and simulation.conservation.complete
    )
    return {
        "semantic_values": _dimension(
            "semantic_values", values_match, f"{prefix}_value_mismatch"
        ),
        "dependencies_and_atomic_groups": _dimension(
            "dependencies_and_atomic_groups", graph_match, f"{prefix}_graph_mismatch"
        ),
        "constraints_and_scope": _dimension(
            "constraints_and_scope", scope_match, f"{prefix}_scope_mismatch"
        ),
        "decisions_and_corrections": (
            _dimension("decisions_and_corrections", decision_match, "decision_state_mismatch")
            if goal is not None
            else _unverified("decisions_and_corrections", "goal_state_unavailable")
        ),
        "simulation_and_conflicts": (
            _dimension(
                "simulation_and_conflicts", simulation_match, "simulation_outcome_mismatch"
            )
            if simulation is not None
            else _unverified("simulation_and_conflicts", "simulation_evidence_unavailable")
        ),
        "review_meaning": _dimension(
            "review_meaning",
            values_match and targets_match and scope_match,
            "review_semantics_mismatch",
        ),
        "typed_failures": (
            _dimension("typed_failures", not simulation.issues, "typed_failure_mismatch")
            if simulation is not None
            else _unverified("typed_failures", "typed_outcome_evidence_unavailable")
        ),
    }


def _preference_comparison_dimensions(
    *,
    change_set: ChangeSet,
    legacy_proposals: dict[str, dict[str, Any]],
    goal: GoalState | None,
    simulation: ChangeSetSimulationResult | None,
    targets_match: bool,
) -> dict[str, dict[str, Any]]:
    if set(legacy_proposals) != {"preference_memory"}:
        return {}
    proposal = legacy_proposals["preference_memory"]
    patch = proposal.get("preference_patch", proposal.get("preferencePatch", proposal))
    raw_operations = patch.get("operations") if isinstance(patch, dict) else None
    if not isinstance(raw_operations, list):
        return {}
    legacy_operations = [
        item
        for item in raw_operations
        if isinstance(item, dict) and item.get("op") == "create_preference"
    ]
    if not legacy_operations:
        return {}
    native_values = [_native_preference_semantics(item) for item in change_set.operations]
    legacy_values = [_legacy_preference_semantics(item) for item in legacy_operations]
    values_match = (
        all(item is not None for item in native_values)
        and native_values == legacy_values
    )
    operation_ids = {operation.operation_id for operation in change_set.operations}
    graph_match = (
        len({operation.atomic_group_id for operation in change_set.operations}) == 1
        and all(set(operation.depends_on) <= operation_ids for operation in change_set.operations)
    )
    dimensions = _successful_adapter_dimensions(
        prefix="preference",
        values_match=values_match,
        graph_match=graph_match,
        scope_match=not change_set.constraints and targets_match,
        targets_match=targets_match,
        goal=goal,
        simulation=simulation,
    )
    if not values_match and len(native_values) == len(legacy_values) == 1:
        native_value = native_values[0] or {}
        legacy_value = legacy_values[0] or {}
        mismatches = [
            name
            for name, key in (
                ("kind", "kind"),
                ("key", "key"),
                ("value", "value"),
                ("semantics", "semantics"),
            )
            if native_value.get(key) != legacy_value.get(key)
        ]
        if mismatches == ["value"]:
            dimensions["semantic_values"]["reasonCode"] = (
                "preference_value_"
                + _preference_value_mismatch_class(
                    native_value.get("value"), legacy_value.get("value")
                )
                + "_mismatch"
            )
        else:
            dimensions["semantic_values"]["reasonCode"] = (
                "preference_" + "_and_".join(mismatches) + "_mismatch"
            )
    return dimensions


def _native_preference_semantics(operation: Any) -> dict[str, Any] | None:
    if operation.kind != "preference.upsert":
        return None
    value = operation.value.model_dump(mode="json", by_alias=True)
    return {
        "kind": "upsert",
        "key": _normalize_semantic_text(operation.preference_key),
        "value": _normalize_preference_value(value),
        "semantics": operation.semantics.model_dump(mode="json", by_alias=True),
    }


def _legacy_preference_semantics(operation: dict[str, Any]) -> dict[str, Any] | None:
    preference = operation.get("preference")
    if not isinstance(preference, dict):
        return None
    return {
        "kind": "upsert",
        "key": _normalize_semantic_text(
            preference.get("preferenceType", preference.get("preference_type"))
        ),
        "value": _normalize_preference_value(preference.get("value")),
        "semantics": {
            "domain": _normalize_legacy_preference_domain(
                preference.get("domain", "general")
            ),
            "strength": _normalize_legacy_preference_strength(
                preference.get("strength", "soft")
            ),
            "scope": preference.get("scopeType", preference.get("scope_type", "global")),
            "effectiveFrom": preference.get(
                "effectiveFrom", preference.get("effective_from")
            ),
            "effectiveUntil": preference.get(
                "effectiveUntil", preference.get("effective_until")
            ),
        },
    }


def _normalize_preference_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("kind") == "scalar":
        return {"kind": "scalar", "value": _normalize_preference_scalar(value.get("value"))}
    if isinstance(value, dict) and "normalizedText" in value:
        return {
            "kind": "scalar",
            "value": _normalize_preference_scalar(value.get("normalizedText")),
        }
    if isinstance(value, dict) and "normalized_text" in value:
        return {
            "kind": "scalar",
            "value": _normalize_preference_scalar(value.get("normalized_text")),
        }
    return value if isinstance(value, dict) else {"kind": "scalar", "value": value}


def _normalize_preference_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = _normalize_semantic_text(value).rstrip(". ")
    for prefix in (
        "remember that i prefer ",
        "preference: ",
        "i prefer ",
        "prefer ",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break
    return normalized


def _preference_value_mismatch_class(native: Any, legacy: Any) -> str:
    """Classify scalar drift without exposing either private preference value."""

    if not (
        isinstance(native, dict)
        and isinstance(legacy, dict)
        and native.get("kind") == legacy.get("kind") == "scalar"
        and isinstance(native.get("value"), str)
        and isinstance(legacy.get("value"), str)
    ):
        return "shape"
    native_tokens = re.findall(r"[a-z0-9]+", native["value"].casefold())
    legacy_tokens = re.findall(r"[a-z0-9]+", legacy["value"].casefold())
    insignificant = {"a", "an", "the"}
    native_content = [token for token in native_tokens if token not in insignificant]
    legacy_content = [token for token in legacy_tokens if token not in insignificant]
    if native_content == legacy_content:
        return "article"
    if sorted(native_content) == sorted(legacy_content):
        return "order"
    native_set = set(native_content)
    legacy_set = set(legacy_content)
    if native_set < legacy_set or legacy_set < native_set:
        return "omission"
    return "substitution"


def _normalize_legacy_preference_domain(value: Any) -> str:
    normalized = _normalize_semantic_text(value)
    return "general" if normalized == "general" else "scheduling"


def _normalize_legacy_preference_strength(value: Any) -> str:
    return "hard" if _normalize_semantic_text(value) == "hard" else "soft"


def _native_semantics(change_set: ChangeSet) -> dict[str, Any]:
    """Return complete canonical meaning for hashing, excluding run-specific identity."""
    payload = change_set.model_dump(mode="json", by_alias=True)
    for key in (
        "changeSetId", "goalId", "revision", "idempotencyKey", "correlationId",
        "causationId", "createdAt", "updatedAt",
    ):
        payload.pop(key, None)
    return payload


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _target_values(value: Any) -> set[str]:
    found: set[str] = set()
    _collect_targets(value, found)
    return found


def _legacy_target_values(legacy_proposals: dict[str, dict[str, Any]]) -> set[str]:
    schedule = legacy_proposals.get("schedule_patch")
    if isinstance(schedule, dict):
        patch = schedule.get("schedulePatch", schedule.get("schedule_patch", schedule))
        operations = patch.get("operations") if isinstance(patch, dict) else None
        if isinstance(operations, list):
            other_families = {
                family: payload
                for family, payload in legacy_proposals.items()
                if family != "schedule_patch"
            }
            return _target_values([operations, other_families])
    branch = legacy_proposals.get("branch")
    if isinstance(branch, dict):
        operations = branch.get(
            "structural_operations", branch.get("structuralOperations", [])
        )
        if isinstance(operations, list) and operations and all(
            isinstance(item, dict) and item.get("kind") == "create_node"
            for item in operations
        ):
            other_families = {
                family: payload
                for family, payload in legacy_proposals.items()
                if family != "branch"
            }
            return _target_values([operations, other_families])
    return _target_values(legacy_proposals)


def _collect_targets(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _TARGET_LIST_KIND_BY_KEY and isinstance(nested, list):
                target_kind = _TARGET_LIST_KIND_BY_KEY[key]
                for item in nested:
                    found.add(
                        f"{target_kind}:{_strip_target_prefix(target_kind, item)}"
                    )
            elif (
                key in _TARGET_KEYS
                and nested is not None
                and not (key in {"entityId", "entity_id"} and value.get("kind") in {"workspace", "temporary"})
            ):
                canonical = _canonical_target_identity(key, nested, value)
                if canonical is not None:
                    found.add(canonical)
            else:
                _collect_targets(nested, found)
    elif isinstance(value, list):
        for nested in value:
            _collect_targets(nested, found)


def _canonical_target_identity(key: str, raw: Any, owner: dict[str, Any]) -> str | None:
    if key in {"entityId", "entity_id"}:
        kind = str(owner.get("kind") or "").strip()
        if kind in {"", "workspace", "temporary"}:
            return None
    else:
        kind = _TARGET_KIND_BY_KEY.get(key)
        if kind is None:
            return None
    identifier = _strip_target_prefix(kind, raw)
    return f"{kind}:{identifier}"


def _strip_target_prefix(kind: str, raw: Any) -> str:
    identifier = str(raw).strip()
    lowered = identifier.lower()
    for prefix in _TARGET_PREFIX_ALIASES.get(kind, (f"{kind}:",)):
        if lowered.startswith(prefix):
            identifier = identifier[len(prefix):]
            break
    return identifier


def _normalize_semantic_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _legacy_operation_count(family: str, payload: dict[str, Any]) -> int:
    candidates: list[Any] = []
    if family == "branch":
        candidates = [payload.get("structural_operations"), payload.get("structuralOperations")]
    elif family == "schedule_patch":
        patch = payload.get("schedulePatch", payload.get("schedule_patch", payload))
        candidates = [patch.get("operations") if isinstance(patch, dict) else None]
    elif family == "action":
        patch = payload.get("taskPatch", payload.get("task_patch", payload))
        candidates = [patch.get("operations") if isinstance(patch, dict) else None]
    elif family == "metadata":
        candidates = [payload.get("operations"), payload.get("metadata_updates")]
    elif family == "preference_memory":
        return 1
    for candidate in candidates:
        if isinstance(candidate, list):
            count = len(candidate)
            recurring = payload.get(
                "recurring_schedule", payload.get("recurringSchedule")
            )
            if family == "branch" and isinstance(recurring, dict):
                count += 1
                nested_payload = (
                    payload.get("payload")
                    if isinstance(payload.get("payload"), dict)
                    else {}
                )
                plan = (
                    nested_payload.get(
                        "recurring_schedule_plan",
                        nested_payload.get("recurringSchedulePlan"),
                    )
                    or payload.get(
                        "recurring_schedule_plan", payload.get("recurringSchedulePlan")
                    )
                )
                displacement_source = plan if isinstance(plan, dict) else recurring
                displacements = displacement_source.get(
                    "displacement_operations",
                    displacement_source.get("displacementOperations", []),
                )
                if isinstance(displacements, list):
                    count += len(displacements)
            if family == "branch":
                count += sum(
                    len(_legacy_schedule_group_target_temp_ids(group))
                    for group in _legacy_created_task_schedule_groups(payload)
                )
            return count
    return 1
