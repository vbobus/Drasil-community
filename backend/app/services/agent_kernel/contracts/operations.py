"""Strict native operation contracts for draft-only cross-domain ChangeSets."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_serializer, model_validator

from .common import ContractId, ContractModel
from .references import EntityRef, Provenance


Weekday: TypeAlias = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]
TaskPriority: TypeAlias = Literal["low", "medium", "high"]
TaskStatus: TypeAlias = Literal["not_started", "in_progress", "blocked", "completed", "archived"]


def _require_ref(ref: EntityRef | None, *allowed: str, field_name: str) -> None:
    if ref is None:
        return
    actual = ref.target_kind if ref.kind == "temporary" else ref.kind
    if actual not in allowed:
        raise ValueError(f"{field_name} must reference one of: {', '.join(allowed)}")


def _unique_refs(refs: list[EntityRef], *, field_name: str) -> None:
    identities = [(ref.kind, ref.target_kind, ref.entity_id) for ref in refs]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{field_name} cannot contain duplicate references")


def _temporary_ref(value: object, *, target_kind: str) -> object:
    if isinstance(value, str):
        return {"kind": "temporary", "entityId": value, "targetKind": target_kind}
    return value


def _validate_interval(start_at: datetime, end_at: datetime) -> None:
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ValueError("calendar intervals require timezone-aware datetimes")
    if end_at <= start_at:
        raise ValueError("calendar interval end_at must be after start_at")


class TaskFields(ContractModel):
    notes: str | None = Field(default=None, max_length=20_000)
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    estimate_minutes: int | None = Field(default=None, ge=1, le=10_080)
    planned_date: date | None = None
    deadline_at: datetime | None = None
    is_backlog: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=128)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for tag in value:
            cleaned = tag.strip()
            if not cleaned:
                continue
            if len(cleaned) > 200:
                raise ValueError("task tags cannot exceed 200 characters")
            if cleaned not in normalized:
                normalized.append(cleaned)
        return normalized


class TaskUpdatePatch(TaskFields):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    clear_fields: list[Literal["notes", "deadline_at", "planned_date"]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_nonempty(self) -> "TaskUpdatePatch":
        if len(self.clear_fields) != len(set(self.clear_fields)):
            raise ValueError("clear_fields cannot contain duplicates")
        addressed = self.model_fields_set - {"clear_fields"}
        explicit_nulls = {name for name in addressed if getattr(self, name) is None}
        if explicit_nulls:
            raise ValueError("null task updates require a typed clear_fields entry")
        if any(getattr(self, name) is not None for name in self.clear_fields):
            raise ValueError("a field cannot be updated and cleared together")
        if not addressed and not self.clear_fields:
            raise ValueError("task update requires at least one addressed field")
        return self

    @model_serializer(mode="wrap")
    def serialize_patch(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class CalendarSegment(ContractModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_interval(self) -> "CalendarSegment":
        _validate_interval(self.start_at, self.end_at)
        return self


class RecurrenceRule(ContractModel):
    frequency: Literal["daily", "weekly", "monthly"]
    interval: int = Field(default=1, ge=1, le=365)
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    month_days: list[int] = Field(default_factory=list, max_length=31)
    start_date: date
    end_date: date | None = None
    occurrence_count: int | None = Field(default=None, ge=1, le=10_000)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    preferred_start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int | None = Field(default=None, ge=1, le=10_080)

    @model_validator(mode="after")
    def validate_rule(self) -> "RecurrenceRule":
        if len(self.weekdays) != len(set(self.weekdays)):
            raise ValueError("weekdays cannot contain duplicates")
        if len(self.month_days) != len(set(self.month_days)) or any(
            day < 1 or day > 31 for day in self.month_days
        ):
            raise ValueError("month_days must be unique values from 1 through 31")
        if self.frequency == "daily" and (self.weekdays or self.month_days):
            raise ValueError("daily recurrence cannot declare weekdays or month_days")
        if self.frequency == "weekly" and not self.weekdays:
            raise ValueError("weekly recurrence requires weekdays")
        if self.frequency == "weekly" and self.month_days:
            raise ValueError("weekly recurrence cannot declare month_days")
        if self.frequency == "monthly" and not self.month_days:
            raise ValueError("monthly recurrence requires month_days")
        if self.frequency == "monthly" and self.weekdays:
            raise ValueError("monthly recurrence cannot declare weekdays")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("recurrence end_date cannot precede start_date")
        if self.end_date is not None and self.occurrence_count is not None:
            raise ValueError("recurrence must use end_date or occurrence_count, not both")
        return self


class RecurrenceRulePatch(ContractModel):
    frequency: Literal["daily", "weekly", "monthly"] | None = None
    interval: int | None = Field(default=None, ge=1, le=365)
    weekdays: list[Weekday] | None = Field(default=None, max_length=7)
    month_days: list[int] | None = Field(default=None, max_length=31)
    start_date: date | None = None
    end_date: date | None = None
    occurrence_count: int | None = Field(default=None, ge=1, le=10_000)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    preferred_start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int | None = Field(default=None, ge=1, le=10_080)

    @model_validator(mode="after")
    def validate_nonempty(self) -> "RecurrenceRulePatch":
        if not self.model_fields_set:
            raise ValueError("recurrence update requires at least one addressed field")
        if self.weekdays is not None and len(self.weekdays) != len(set(self.weekdays)):
            raise ValueError("weekdays cannot contain duplicates")
        if self.month_days is not None and (
            len(self.month_days) != len(set(self.month_days))
            or any(day < 1 or day > 31 for day in self.month_days)
        ):
            raise ValueError("month_days must be unique values from 1 through 31")
        if self.end_date is not None and self.occurrence_count is not None:
            raise ValueError("recurrence must use end_date or occurrence_count, not both")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("recurrence end_date cannot precede start_date")
        if self.frequency == "daily" and (self.weekdays or self.month_days):
            raise ValueError("daily recurrence cannot declare weekdays or month_days")
        if self.frequency == "weekly" and self.month_days:
            raise ValueError("weekly recurrence cannot declare month_days")
        if self.frequency == "monthly" and self.weekdays:
            raise ValueError("monthly recurrence cannot declare weekdays")
        return self


PreferenceScalar: TypeAlias = str | int | float | bool | None


class PreferenceScalarValue(ContractModel):
    kind: Literal["scalar"] = "scalar"
    value: PreferenceScalar


class PreferenceStringSetValue(ContractModel):
    kind: Literal["string_set"] = "string_set"
    values: list[str] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_unique(self) -> "PreferenceStringSetValue":
        if len(self.values) != len(set(self.values)):
            raise ValueError("preference string set cannot contain duplicates")
        return self


class PreferenceTimeWindowValue(ContractModel):
    kind: Literal["time_window"] = "time_window"
    start_local: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_local: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=100)


PreferenceValue: TypeAlias = Annotated[
    PreferenceScalarValue | PreferenceStringSetValue | PreferenceTimeWindowValue,
    Field(discriminator="kind"),
]


class PreferenceSemantics(ContractModel):
    domain: str = Field(default="general", min_length=1, max_length=64)
    strength: Literal["soft", "hard"] = "soft"
    scope: Literal["global", "workspace", "task", "calendar"] = "global"
    effective_from: datetime | None = None
    effective_until: datetime | None = None

    @model_validator(mode="after")
    def validate_effective_range(self) -> "PreferenceSemantics":
        for value in (self.effective_from, self.effective_until):
            if value is not None and value.tzinfo is None:
                raise ValueError("preference effective ranges require timezone-aware datetimes")
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("preference effective_until must follow effective_from")
        return self


class OperationBase(ContractModel):
    kind: str
    operation_id: ContractId
    depends_on: list[ContractId] = Field(default_factory=list, max_length=128)
    atomic_group_id: ContractId | None = None
    fulfills_clause_ids: list[ContractId] = Field(default_factory=list, max_length=128)
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def validate_operation_identity(self) -> "OperationBase":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on cannot contain duplicates")
        if self.operation_id in self.depends_on:
            raise ValueError("an operation cannot depend on itself")
        if len(self.fulfills_clause_ids) != len(set(self.fulfills_clause_ids)):
            raise ValueError("fulfills_clause_ids cannot contain duplicates")
        return self


class TaskCreateOperation(OperationBase):
    kind: Literal["task.create"] = "task.create"
    title: str = Field(min_length=1, max_length=500)
    workspace_ref: EntityRef
    parent_ref: EntityRef | None = None
    temp_ref: EntityRef | None = None
    fields: TaskFields = Field(default_factory=TaskFields)

    @field_validator("temp_ref", mode="before")
    @classmethod
    def migrate_temp_ref(cls, value: object) -> object:
        return _temporary_ref(value, target_kind="task")

    @model_validator(mode="after")
    def validate_refs(self) -> "TaskCreateOperation":
        _require_ref(self.workspace_ref, "workspace", field_name="workspace_ref")
        _require_ref(self.parent_ref, "task", field_name="parent_ref")
        if self.temp_ref is not None:
            _require_ref(self.temp_ref, "task", field_name="temp_ref")
            if self.temp_ref.kind != "temporary":
                raise ValueError("temp_ref must be temporary")
        return self


class TaskUpdateOperation(OperationBase):
    kind: Literal["task.update"] = "task.update"
    task_ref: EntityRef
    updates: TaskUpdatePatch

    @model_validator(mode="after")
    def validate_ref(self) -> "TaskUpdateOperation":
        _require_ref(self.task_ref, "task", field_name="task_ref")
        return self


class TaskMoveOperation(OperationBase):
    kind: Literal["task.move"] = "task.move"
    task_refs: list[EntityRef] = Field(min_length=1, max_length=128)
    destination_parent_ref: EntityRef | None = None
    destination_workspace_ref: EntityRef | None = None

    @model_validator(mode="after")
    def validate_refs(self) -> "TaskMoveOperation":
        _unique_refs(self.task_refs, field_name="task_refs")
        for ref in self.task_refs:
            _require_ref(ref, "task", field_name="task_refs")
        _require_ref(self.destination_parent_ref, "task", field_name="destination_parent_ref")
        _require_ref(
            self.destination_workspace_ref, "workspace", field_name="destination_workspace_ref"
        )
        if self.destination_parent_ref is None and self.destination_workspace_ref is None:
            raise ValueError("task.move requires a destination parent or workspace")
        return self


class _TaskSetOperation(OperationBase):
    task_refs: list[EntityRef] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_task_refs(self) -> "_TaskSetOperation":
        _unique_refs(self.task_refs, field_name="task_refs")
        for ref in self.task_refs:
            _require_ref(ref, "task", field_name="task_refs")
        return self


class TaskArchiveOperation(_TaskSetOperation):
    kind: Literal["task.archive"] = "task.archive"


class TaskDeleteOperation(_TaskSetOperation):
    kind: Literal["task.delete"] = "task.delete"


class CalendarCreateBlockOperation(OperationBase):
    kind: Literal["calendar.create_block"] = "calendar.create_block"
    task_ref: EntityRef
    start_at: datetime
    end_at: datetime
    temp_ref: EntityRef | None = None

    @field_validator("temp_ref", mode="before")
    @classmethod
    def migrate_temp_ref(cls, value: object) -> object:
        return _temporary_ref(value, target_kind="calendar_block")

    @model_validator(mode="after")
    def validate_block(self) -> "CalendarCreateBlockOperation":
        _require_ref(self.task_ref, "task", field_name="task_ref")
        if self.temp_ref is not None:
            _require_ref(self.temp_ref, "calendar_block", field_name="temp_ref")
            if self.temp_ref.kind != "temporary":
                raise ValueError("temp_ref must be temporary")
        _validate_interval(self.start_at, self.end_at)
        return self


class CalendarMoveBlockOperation(OperationBase):
    kind: Literal["calendar.move_block"] = "calendar.move_block"
    block_ref: EntityRef
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_block(self) -> "CalendarMoveBlockOperation":
        _require_ref(self.block_ref, "calendar_block", field_name="block_ref")
        _validate_interval(self.start_at, self.end_at)
        return self


class CalendarResizeBlockOperation(OperationBase):
    kind: Literal["calendar.resize_block"] = "calendar.resize_block"
    block_ref: EntityRef
    start_at: datetime | None = None
    end_at: datetime

    @model_validator(mode="after")
    def validate_block(self) -> "CalendarResizeBlockOperation":
        _require_ref(self.block_ref, "calendar_block", field_name="block_ref")
        if self.end_at.tzinfo is None:
            raise ValueError("calendar resize requires a timezone-aware end_at")
        if self.start_at is not None:
            _validate_interval(self.start_at, self.end_at)
        return self


class CalendarSplitBlockOperation(OperationBase):
    kind: Literal["calendar.split_block"] = "calendar.split_block"
    block_ref: EntityRef
    segments: list[CalendarSegment] = Field(min_length=2, max_length=128)

    @model_validator(mode="after")
    def validate_segments(self) -> "CalendarSplitBlockOperation":
        _require_ref(self.block_ref, "calendar_block", field_name="block_ref")
        for previous, current in zip(self.segments, self.segments[1:]):
            if current.start_at < previous.end_at:
                raise ValueError("split segments must be ordered and non-overlapping")
        return self


class CalendarDeleteBlockOperation(OperationBase):
    kind: Literal["calendar.delete_block"] = "calendar.delete_block"
    block_refs: list[EntityRef] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_refs(self) -> "CalendarDeleteBlockOperation":
        _unique_refs(self.block_refs, field_name="block_refs")
        for ref in self.block_refs:
            _require_ref(ref, "calendar_block", field_name="block_refs")
        return self


class RecurrenceCreateSeriesOperation(OperationBase):
    kind: Literal["recurrence.create_series"] = "recurrence.create_series"
    task_ref: EntityRef
    rule: RecurrenceRule
    temp_ref: EntityRef | None = None

    @field_validator("temp_ref", mode="before")
    @classmethod
    def migrate_temp_ref(cls, value: object) -> object:
        return _temporary_ref(value, target_kind="recurrence_series")

    @model_validator(mode="after")
    def validate_refs(self) -> "RecurrenceCreateSeriesOperation":
        _require_ref(self.task_ref, "task", field_name="task_ref")
        if self.temp_ref is not None:
            _require_ref(self.temp_ref, "recurrence_series", field_name="temp_ref")
            if self.temp_ref.kind != "temporary":
                raise ValueError("temp_ref must be temporary")
        return self


class RecurrenceUpdateSeriesOperation(OperationBase):
    kind: Literal["recurrence.update_series"] = "recurrence.update_series"
    series_ref: EntityRef
    updates: RecurrenceRulePatch

    @model_validator(mode="after")
    def validate_ref(self) -> "RecurrenceUpdateSeriesOperation":
        _require_ref(self.series_ref, "recurrence_series", field_name="series_ref")
        return self


class RecurrenceOccurrencePatch(ContractModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=10_080)

    @model_validator(mode="after")
    def validate_patch(self) -> "RecurrenceOccurrencePatch":
        if not self.model_fields_set:
            raise ValueError("occurrence update requires at least one addressed field")
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("occurrence start_at and end_at must be supplied together")
        if self.start_at is not None and self.end_at is not None:
            _validate_interval(self.start_at, self.end_at)
        return self


class RecurrenceUpdateOccurrenceOperation(OperationBase):
    kind: Literal["recurrence.update_occurrence"] = "recurrence.update_occurrence"
    occurrence_ref: EntityRef
    updates: RecurrenceOccurrencePatch

    @model_validator(mode="after")
    def validate_ref(self) -> "RecurrenceUpdateOccurrenceOperation":
        _require_ref(
            self.occurrence_ref, "recurrence_occurrence", field_name="occurrence_ref"
        )
        return self


class RecurrenceCancelOccurrenceOperation(OperationBase):
    kind: Literal["recurrence.cancel_occurrence"] = "recurrence.cancel_occurrence"
    occurrence_ref: EntityRef

    @model_validator(mode="after")
    def validate_ref(self) -> "RecurrenceCancelOccurrenceOperation":
        _require_ref(
            self.occurrence_ref, "recurrence_occurrence", field_name="occurrence_ref"
        )
        return self


class PreferenceUpsertOperation(OperationBase):
    kind: Literal["preference.upsert"] = "preference.upsert"
    preference_key: str = Field(min_length=1, max_length=200)
    value: PreferenceValue
    semantics: PreferenceSemantics = Field(default_factory=PreferenceSemantics)


class PreferenceSupersedeOperation(OperationBase):
    kind: Literal["preference.supersede"] = "preference.supersede"
    preference_ref: EntityRef
    value: PreferenceValue
    semantics: PreferenceSemantics = Field(default_factory=PreferenceSemantics)

    @model_validator(mode="after")
    def validate_ref(self) -> "PreferenceSupersedeOperation":
        _require_ref(self.preference_ref, "preference", field_name="preference_ref")
        return self


class PreferenceRetireOperation(OperationBase):
    kind: Literal["preference.retire"] = "preference.retire"
    preference_ref: EntityRef
    replacement_ref: EntityRef | None = None
    reason_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_ref(self) -> "PreferenceRetireOperation":
        _require_ref(self.preference_ref, "preference", field_name="preference_ref")
        if self.replacement_ref is not None:
            _require_ref(
                self.replacement_ref,
                "preference",
                field_name="replacement_ref",
            )
            if self.replacement_ref.entity_id == self.preference_ref.entity_id:
                raise ValueError("replacement_ref must identify a different preference")
        return self


class PreferenceRestoreOperation(OperationBase):
    kind: Literal["preference.restore"] = "preference.restore"
    preference_ref: EntityRef

    @model_validator(mode="after")
    def validate_ref(self) -> "PreferenceRestoreOperation":
        _require_ref(self.preference_ref, "preference", field_name="preference_ref")
        return self


class PreferenceDeleteOperation(OperationBase):
    kind: Literal["preference.delete"] = "preference.delete"
    preference_ref: EntityRef
    replacement_ref: EntityRef | None = None
    reason_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_ref(self) -> "PreferenceDeleteOperation":
        _require_ref(self.preference_ref, "preference", field_name="preference_ref")
        if self.replacement_ref is not None:
            _require_ref(
                self.replacement_ref,
                "preference",
                field_name="replacement_ref",
            )
            if self.replacement_ref.entity_id == self.preference_ref.entity_id:
                raise ValueError("replacement_ref must identify a different preference")
        return self


Operation: TypeAlias = Annotated[
    TaskCreateOperation
    | TaskUpdateOperation
    | TaskMoveOperation
    | TaskArchiveOperation
    | TaskDeleteOperation
    | CalendarCreateBlockOperation
    | CalendarMoveBlockOperation
    | CalendarResizeBlockOperation
    | CalendarSplitBlockOperation
    | CalendarDeleteBlockOperation
    | RecurrenceCreateSeriesOperation
    | RecurrenceUpdateSeriesOperation
    | RecurrenceUpdateOccurrenceOperation
    | RecurrenceCancelOccurrenceOperation
    | PreferenceUpsertOperation
    | PreferenceSupersedeOperation
    | PreferenceRetireOperation
    | PreferenceRestoreOperation
    | PreferenceDeleteOperation,
    Field(discriminator="kind"),
]

OPERATION_ADAPTER = TypeAdapter(Operation)


def parse_operation(value: object) -> Operation:
    return OPERATION_ADAPTER.validate_python(value)
