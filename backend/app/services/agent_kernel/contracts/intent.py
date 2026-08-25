"""Typed requested-operation clauses owned by GoalState operations.requested."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from .common import ContractId, ContractModel
from .operations import Operation
from .references import EntityRef, Provenance


OperationKind: TypeAlias = Literal[
    "task.create",
    "task.update",
    "task.move",
    "task.archive",
    "task.delete",
    "calendar.create_block",
    "calendar.move_block",
    "calendar.resize_block",
    "calendar.split_block",
    "calendar.delete_block",
    "recurrence.create_series",
    "recurrence.update_series",
    "recurrence.update_occurrence",
    "recurrence.cancel_occurrence",
    "preference.upsert",
    "preference.supersede",
    "preference.retire",
    "preference.restore",
    "preference.delete",
]


class RequestedOperationClause(ContractModel):
    clause_id: ContractId
    operation_kind: OperationKind | None
    operation_source: Literal["strategist", "planner"] = "strategist"
    legacy_operation_label: str | None = Field(default=None, min_length=1, max_length=256)
    supporting_operation_kinds: list[OperationKind] = Field(default_factory=list, max_length=17)
    cardinality: Literal["exactly_one", "zero_or_one", "one_or_more", "any_set"]
    target_refs: list[EntityRef] = Field(default_factory=list, max_length=512)
    expected_operations: list[Operation] = Field(default_factory=list, max_length=128)
    scope: Literal["goal", "entity_set", "series", "occurrence_set"] = "goal"
    disposition: Literal["requested", "unresolved", "unsupported", "satisfied"] = "requested"
    decision_id: ContractId | None = None
    unsupported_reason: Literal[
        "unsupported_operation_kind",
        "unsupported_scope",
        "missing_capability",
        "policy_forbidden",
    ] | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def validate_clause(self) -> "RequestedOperationClause":
        if self.operation_kind is None:
            if not (
                self.disposition == "unsupported"
                and self.unsupported_reason == "unsupported_operation_kind"
                and self.legacy_operation_label
            ):
                raise ValueError("missing operation kind requires visible unsupported legacy metadata")
        if self.operation_kind in self.supporting_operation_kinds:
            raise ValueError("supporting operation kinds cannot repeat the primary kind")
        if len(self.supporting_operation_kinds) != len(set(self.supporting_operation_kinds)):
            raise ValueError("supporting operation kinds cannot contain duplicates")
        identities = [(ref.kind, ref.target_kind, ref.entity_id) for ref in self.target_refs]
        if len(identities) != len(set(identities)):
            raise ValueError("requested-operation targets cannot contain duplicates")
        if self.scope == "series":
            if not self.target_refs or any(
                (ref.target_kind if ref.kind == "temporary" else ref.kind)
                != "recurrence_series"
                for ref in self.target_refs
            ):
                raise ValueError("series intent requires recurrence-series targets")
        if self.scope == "occurrence_set":
            if not self.target_refs or any(
                (ref.target_kind if ref.kind == "temporary" else ref.kind)
                != "recurrence_occurrence"
                for ref in self.target_refs
            ):
                raise ValueError("occurrence intent requires recurrence-occurrence targets")
        if self.cardinality == "exactly_one" and len(self.target_refs) > 1:
            raise ValueError("exactly_one intent cannot name multiple targets")
        if self.cardinality == "zero_or_one" and len(self.target_refs) > 1:
            raise ValueError("zero_or_one intent cannot name multiple targets")
        if self.disposition == "unresolved" and self.decision_id is None:
            raise ValueError("unresolved intent requires a durable decision_id")
        if self.disposition != "unresolved" and self.decision_id is not None:
            raise ValueError("only unresolved intent may reference a decision")
        if self.disposition == "unsupported" and self.unsupported_reason is None:
            raise ValueError("unsupported intent requires a typed unsupported_reason")
        if self.disposition != "unsupported" and self.unsupported_reason is not None:
            raise ValueError("unsupported_reason requires unsupported disposition")
        if (
            self.disposition == "requested"
            and self.operation_source == "strategist"
            and not self.expected_operations
        ):
            if not self.clause_id.startswith("clause:legacy-"):
                raise ValueError("requested intent requires exact expected operations")
        if self.operation_source == "planner":
            if self.operation_kind is not None and not (
                self.operation_kind.startswith("calendar.")
                or self.operation_kind.startswith("recurrence.")
            ):
                raise ValueError(
                    "Planner delegation is limited to calendar and recurrence intent"
                )
            if self.expected_operations:
                raise ValueError(
                    "Planner-delegated intent cannot pre-author exact operations"
                )
        expected_ids = [operation.operation_id for operation in self.expected_operations]
        if len(expected_ids) != len(set(expected_ids)):
            raise ValueError("expected operation IDs must be unique within a clause")
        allowed_kinds = {self.operation_kind, *self.supporting_operation_kinds}
        if any(operation.kind not in allowed_kinds for operation in self.expected_operations):
            raise ValueError("expected operations must use the primary or supporting kinds")
        if self.expected_operations and not any(
            operation.kind == self.operation_kind for operation in self.expected_operations
        ):
            raise ValueError("expected operations must include the primary operation kind")
        if any(
            self.clause_id not in operation.fulfills_clause_ids
            for operation in self.expected_operations
        ):
            raise ValueError("each expected operation must fulfill this clause")
        return self


_LEGACY_KIND_MAP: dict[str, OperationKind] = {
    "create": "task.create",
    "rename": "task.update",
    "update": "task.update",
    "move": "task.move",
    "archive": "task.archive",
    "delete": "task.delete",
    "schedule": "calendar.create_block",
    "reschedule": "calendar.move_block",
    "resize": "calendar.resize_block",
    "recur": "recurrence.create_series",
    "preference": "preference.upsert",
}

_TARGET_KIND_BY_OPERATION: dict[str, str] = {
    "task.update": "task", "task.move": "task", "task.archive": "task", "task.delete": "task",
    "calendar.move_block": "calendar_block", "calendar.resize_block": "calendar_block",
    "calendar.split_block": "calendar_block", "calendar.delete_block": "calendar_block",
    "recurrence.update_series": "recurrence_series",
    "recurrence.update_occurrence": "recurrence_occurrence",
    "recurrence.cancel_occurrence": "recurrence_occurrence",
    "preference.supersede": "preference", "preference.retire": "preference",
    "preference.restore": "preference", "preference.delete": "preference",
}


def _migrate_clause_payload(item: object) -> object:
    if not isinstance(item, dict):
        return item
    migrated = dict(item)
    operation_kind = migrated.get("operationKind", migrated.get("operation_kind"))
    target_kind = _TARGET_KIND_BY_OPERATION.get(str(operation_kind))
    refs_key = "targetRefs" if "targetRefs" in migrated else "target_refs"
    refs = migrated.get(refs_key)
    if target_kind and isinstance(refs, list):
        migrated[refs_key] = [
            {**ref, "targetKind": target_kind}
            if isinstance(ref, dict)
            and ref.get("kind") == "temporary"
            and not ref.get("targetKind", ref.get("target_kind"))
            else ref
            for ref in refs
        ]
    return migrated


def migrate_requested_operation_clauses(
    value: object,
    *,
    provenance: Provenance,
) -> list[dict[str, object]]:
    """Migrate the Story 8.3 string list to canonical typed clauses deterministically."""

    if not isinstance(value, list) or not value:
        raise ValueError("operations.requested requires a non-empty clause list")
    migrated: list[RequestedOperationClause] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            normalized = item.strip().lower()
            operation_kind = _LEGACY_KIND_MAP.get(normalized)
            if operation_kind is None and normalized in OperationKind.__args__:  # type: ignore[attr-defined]
                operation_kind = normalized  # type: ignore[assignment]
            if operation_kind is None:
                operation_kind = None
            digest = hashlib.sha256(
                json.dumps(
                    [provenance.source_ref, index, normalized],
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16]
            payload: dict[str, object] = {
                "clause_id": f"clause:legacy-{digest}",
                "operation_kind": operation_kind,
                "cardinality": "exactly_one",
                "provenance": provenance,
            }
            if operation_kind is None:
                payload.update(
                    disposition="unsupported",
                    unsupported_reason="unsupported_operation_kind",
                    legacy_operation_label=item.strip(),
                )
            migrated.append(RequestedOperationClause(**payload))
        else:
            migrated.append(RequestedOperationClause.model_validate(_migrate_clause_payload(item)))
    ids = [item.clause_id for item in migrated]
    if len(ids) != len(set(ids)):
        raise ValueError("requested-operation clause IDs must be unique")
    return [item.model_dump(mode="json", by_alias=True) for item in migrated]
