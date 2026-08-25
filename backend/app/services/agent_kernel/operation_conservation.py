"""Deterministic proof that typed GoalState operation intent survives a ChangeSet."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.agent_kernel.contracts import (
    ChangeSet,
    GoalState,
    OperationConservationReport,
    OperationFingerprint,
)


class OperationConservationError(ValueError):
    pass


def _canonical_payload(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def operation_fingerprint(operation: Any) -> OperationFingerprint:
    return OperationFingerprint(
        operation_id=operation.operation_id,
        kind=operation.kind,
        sha256=hashlib.sha256(_canonical_payload(operation)).hexdigest(),
    )


def _entity_identities(value: Any) -> set[tuple[str, str]]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=False)
    identities: set[tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            kind = item.get("kind")
            entity_id = item.get("entity_id", item.get("entityId"))
            if kind in {
                "task",
                "workspace",
                "calendar_block",
                "recurrence_series",
                "recurrence_occurrence",
                "preference",
                "temporary",
            } and entity_id:
                actual = item.get("target_kind", item.get("targetKind")) if kind == "temporary" else kind
                identities.add((str(actual), str(entity_id)))
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return identities


def evaluate_operation_conservation(
    goal_state: GoalState,
    change_set: ChangeSet,
) -> OperationConservationReport:
    if goal_state.goal_id != change_set.goal_id:
        raise OperationConservationError("ChangeSet belongs to a different goal")
    clauses = goal_state.requested_operation_clauses()
    by_clause: dict[str, list[Any]] = {clause.clause_id: [] for clause in clauses}
    extra_operation_ids: list[str] = []
    for operation in change_set.operations:
        linked = False
        for clause_id in operation.fulfills_clause_ids:
            if clause_id in by_clause:
                by_clause[clause_id].append(operation)
                linked = True
        if not linked:
            extra_operation_ids.append(operation.operation_id)

    covered: list[str] = []
    unresolved: list[str] = []
    unsupported: list[str] = []
    failed: list[str] = []
    for clause in clauses:
        if clause.disposition == "unresolved":
            unresolved.append(clause.clause_id)
            continue
        if clause.disposition == "unsupported":
            unsupported.append(clause.clause_id)
            continue
        operations = by_clause[clause.clause_id]
        if clause.disposition == "satisfied":
            if operations:
                failed.append(clause.clause_id)
            else:
                covered.append(clause.clause_id)
            continue
        if clause.expected_operations:
            expected = {
                operation.operation_id: operation_fingerprint(operation).sha256
                for operation in clause.expected_operations
            }
            actual = {
                operation.operation_id: operation_fingerprint(operation).sha256
                for operation in operations
            }
            if actual != expected:
                failed.append(clause.clause_id)
                continue
            covered.append(clause.clause_id)
            continue
        allowed_kinds = {clause.operation_kind, *clause.supporting_operation_kinds}
        if (
            not operations
            or not any(operation.kind == clause.operation_kind for operation in operations)
            or any(operation.kind not in allowed_kinds for operation in operations)
        ):
            failed.append(clause.clause_id)
            continue
        target_identities = {
            (
                str(ref.target_kind if ref.kind == "temporary" else ref.kind),
                ref.entity_id,
            )
            for ref in clause.target_refs
        }
        touched = set().union(*(_entity_identities(operation) for operation in operations))
        if target_identities and target_identities != touched:
            failed.append(clause.clause_id)
            continue
        cardinality_count = len(target_identities) if target_identities else len(operations)
        cardinality_ok = {
            "exactly_one": cardinality_count == 1,
            "zero_or_one": cardinality_count <= 1,
            "one_or_more": cardinality_count >= 1,
            "any_set": True,
        }[clause.cardinality]
        if not cardinality_ok:
            failed.append(clause.clause_id)
            continue
        covered.append(clause.clause_id)

    complete = not failed and not unresolved and not unsupported and not extra_operation_ids
    return OperationConservationReport(
        complete=complete,
        covered_clause_ids=covered,
        unresolved_clause_ids=unresolved,
        unsupported_clause_ids=unsupported,
        failed_clause_ids=failed,
        extra_operation_ids=extra_operation_ids,
        operation_fingerprints=[
            operation_fingerprint(operation) for operation in change_set.operations
        ],
    )


def verify_operation_round_trip(submitted: ChangeSet, canonical: ChangeSet) -> None:
    """Fail when persistence/provider adaptation drops, reorders, or rewrites an operation."""

    submitted_ids = [operation.operation_id for operation in submitted.operations]
    canonical_ids = [operation.operation_id for operation in canonical.operations]
    if submitted_ids != canonical_ids:
        raise OperationConservationError("operation identity or order changed during round trip")
    submitted_fingerprints = [
        operation_fingerprint(operation).sha256 for operation in submitted.operations
    ]
    canonical_fingerprints = [
        operation_fingerprint(operation).sha256 for operation in canonical.operations
    ]
    if submitted_fingerprints != canonical_fingerprints:
        raise OperationConservationError("operation semantics changed during round trip")
