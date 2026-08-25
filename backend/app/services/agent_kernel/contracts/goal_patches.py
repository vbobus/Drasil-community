"""Closed semantic patch vocabulary for durable GoalState reduction."""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError, model_validator

from .common import ContractId, ContractModel
from .constraints import Constraint
from .intent import migrate_requested_operation_clauses
from .references import EntityRef, EntitySelector, Provenance


GoalSemanticPath = Literal[
    "outcomes.desired",
    "scope.task_refs",
    "scope.selectors",
    "scope.resolved_refs",
    "scope.workspace",
    "scope.parent",
    "operations.requested",
    "recurrence.rule",
    "recurrence.series_scope",
    "recurrence.occurrence_scope",
    "temporal.duration_minutes",
    "temporal.requested_date",
    "temporal.requested_time",
    "temporal.requested_window",
    "temporal.ordering",
    "temporal.gaps",
    "conflict.entity_scope",
    "conflict.occurrence_scope",
    "constraints.active",
    "preferences.relevant",
]

GoalPatchOperation = Literal[
    "set",
    "set_null",
    "clear",
    "mark_unknown",
    "mark_unresolved",
]

_STRING_LIST_PATHS = {
    "scope.task_refs",
    "recurrence.occurrence_scope",
    "temporal.ordering",
    "conflict.entity_scope",
    "conflict.occurrence_scope",
}
_NONEMPTY_STRING_PATHS = {
    "scope.workspace",
    "scope.parent",
    "temporal.requested_date",
    "temporal.requested_time",
}


def _validate_string_list(path: str, value: JsonValue) -> None:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{path} requires a non-empty list of non-blank identifiers")


def _validate_path_value(path: GoalSemanticPath, value: JsonValue) -> None:
    if path == "operations.requested":
        return
    if path in _STRING_LIST_PATHS:
        _validate_string_list(path, value)
        return
    if path in _NONEMPTY_STRING_PATHS:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path} requires a non-blank string")
        if path == "temporal.requested_date":
            try:
                date.fromisoformat(value)
            except ValueError:
                # Relative date tokens remain model semantics until planning resolves them.
                if value not in {"today", "tomorrow", "this-week", "next-week"}:
                    raise ValueError(f"{path} requires an ISO date or supported relative token")
        if path == "temporal.requested_time":
            try:
                time.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{path} requires an ISO local time") from error
        return
    if path == "temporal.duration_minutes":
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("temporal.duration_minutes requires a positive integer")
        return
    if path == "temporal.gaps":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("temporal.gaps requires a non-negative integer")
        return
    if path in {"scope.selectors", "scope.resolved_refs", "preferences.relevant"}:
        model = EntitySelector if path == "scope.selectors" else EntityRef
        try:
            TypeAdapter(list[model]).validate_python(value)  # type: ignore[valid-type]
        except ValidationError as error:
            raise ValueError(f"{path} requires canonical {model.__name__} objects") from error
        return
    if path == "constraints.active":
        try:
            TypeAdapter(list[Constraint]).validate_python(value)
        except ValidationError as error:
            raise ValueError("constraints.active requires canonical Constraint objects") from error
        return
    if path == "outcomes.desired":
        if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
            raise ValueError("outcomes.desired requires a non-empty list of outcome objects")
        return
    if path in {"recurrence.rule", "temporal.requested_window"}:
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{path} requires a non-empty object")
        return
    if path == "recurrence.series_scope":
        if isinstance(value, str) and value.strip():
            return
        _validate_string_list(path, value)


class GoalStatePatch(ContractModel):
    """One sparse semantic change; identity and lifecycle fields are not addressable."""

    path: GoalSemanticPath
    operation: GoalPatchOperation
    value: JsonValue = None
    provenance: Provenance
    supersedes_ref: ContractId | None = None

    @model_validator(mode="after")
    def validate_operation_value(self) -> "GoalStatePatch":
        if self.operation == "set" and self.value is None:
            raise ValueError("set requires a non-null value; use set_null for explicit null")
        if self.operation == "set":
            _validate_path_value(self.path, self.value)
            if self.path == "operations.requested":
                object.__setattr__(
                    self,
                    "value",
                    migrate_requested_operation_clauses(
                        self.value,
                        provenance=self.provenance,
                    ),
                )
        if self.operation != "set" and self.value is not None:
            raise ValueError(f"{self.operation} requires a null payload")
        if self.operation in {"set_null", "clear"} and self.provenance.source_kind not in {
            "user",
            "ui",
        }:
            raise ValueError(f"{self.operation} requires explicit user or UI provenance")
        return self
