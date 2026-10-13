"""Deterministic semantic projection from native state into a ReviewDocument."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import hashlib
import re
from typing import Iterable, Mapping

from .contracts import (
    ApproveReviewAction,
    AssumptionBlock,
    ChangeSet,
    ChangeSetSimulationResult,
    ConflictResolutionBlock,
    ConflictReviewItem,
    DecisionReviewAction,
    EditReviewAction,
    EntityRef,
    EntityReferencesBlock,
    GoalState,
    GroupedChangesBlock,
    ExecutionStatusBlock,
    ExternalSyncReviewStatus,
    LocalExecutionResult,
    RecurrenceSummaryBlock,
    RejectReviewAction,
    ReviewActionBinding,
    ReviewChange,
    ReviewChangeGroup,
    ReviewDocument,
    ReviewEntity,
    ScheduleSummaryBlock,
    SemanticDiffBlock,
    SemanticFallbackBlock,
    TaskStructureBlock,
    TaskStructureItem,
    TaskStructureSchedule,
    TaskStructureWorkspace,
    WarningBlock,
)
from .operation_conservation import operation_fingerprint
from app.services.preference_values import decode_preference_value


class ReviewProjectionError(ValueError):
    """The canonical inputs cannot safely produce a current review."""


_KIND_SUMMARIES = {
    "task.create": "Create a task",
    "task.update": "Update a task",
    "task.move": "Move a task",
    "task.archive": "Archive a task",
    "task.delete": "Delete a task",
    "calendar.create_block": "Add time to the calendar",
    "calendar.move_block": "Move scheduled time",
    "calendar.resize_block": "Change scheduled duration",
    "calendar.split_block": "Split scheduled work",
    "calendar.delete_block": "Remove scheduled time",
    "recurrence.create_series": "Create a repeating schedule",
    "recurrence.update_series": "Update a repeating schedule",
    "recurrence.update_occurrence": "Change one occurrence",
    "recurrence.cancel_occurrence": "Cancel one occurrence",
    "preference.upsert": "Save a planning preference",
    "preference.supersede": "Replace a planning preference",
    "preference.retire": "Stop using a planning preference",
    "preference.restore": "Restore a planning preference",
    "preference.delete": "Permanently delete a planning preference",
}
SUPPORTED_REVIEW_OPERATION_KINDS = frozenset(_KIND_SUMMARIES)
_EXPECTED_EFFECT_KINDS = {
    "task.create": "task.created",
    "task.update": "task.updated",
    "task.move": "task.moved",
    "task.archive": "task.archived",
    "task.delete": "task.deleted",
    "calendar.create_block": "calendar.created",
    "calendar.move_block": "calendar.moved",
    "calendar.resize_block": "calendar.resized",
    "calendar.split_block": "calendar.split",
    "calendar.delete_block": "calendar.deleted",
    "recurrence.create_series": "recurrence.created",
    "recurrence.update_series": "recurrence.updated",
    "recurrence.update_occurrence": "recurrence.occurrence_updated",
    "recurrence.cancel_occurrence": "recurrence.occurrence_cancelled",
    "preference.upsert": "preference.upserted",
    "preference.supersede": "preference.superseded",
    "preference.retire": "preference.retired",
    "preference.restore": "preference.restored",
    "preference.delete": "preference.deleted",
}

_TECHNICAL_COPY_PATTERNS = (
    re.compile(r"\b(?:TaskPatch|SchedulePatch|PreferencePatch|ChangeSet)\b", re.IGNORECASE),
    re.compile(r"\b(?:validator|operation count|operations count|provider payload)\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+operations?\b", re.IGNORECASE),
    re.compile(r"\b[a-z]+_[a-z][a-z0-9_]*\b"),
    re.compile(r"\b(?:goal|changeset|simulation|task|calendar_block|recurrence_series|preference):[a-z0-9_-]+\b", re.IGNORECASE),
    re.compile(r"\b(?:chain of thought|hidden reasoning|internal reasoning)\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)[{[]\s*[\"'][a-zA-Z0-9_]"),
)

_DOMAIN_TITLES = {
    "task": "Tasks",
    "calendar": "Schedule",
    "recurrence": "Repeating work",
    "preference": "Preferences",
}

_ENTITY_FALLBACKS = {
    "task": "Task",
    "workspace": "Workspace",
    "calendar_block": "Scheduled time",
    "recurrence_series": "Repeating schedule",
    "recurrence_occurrence": "Occurrence",
    "preference": "Preference",
    "scheduling_constraint": "Scheduling rule",
    "preference_link": "Related preference",
    "temporary": "New item",
}


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _format_date(value: date) -> str:
    return value.strftime("%B %d, %Y").replace(" 0", " ")


def _format_time(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _format_datetime(value: datetime) -> str:
    return f"{_format_date(value.date())} at {_format_time(value)}"


def _format_datetime_range(start_at: datetime, end_at: datetime) -> str:
    if start_at.date() == end_at.date():
        return (
            f"{_format_date(start_at.date())}, "
            f"{_format_time(start_at)}\u2013{_format_time(end_at)}"
        )
    return f"{_format_datetime(start_at)} through {_format_datetime(end_at)}"


def _utc_offset_label(value: datetime) -> str | None:
    offset = value.utcoffset()
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    if total_minutes == 0:
        return "UTC"
    sign = "+" if total_minutes > 0 else "\u2212"
    absolute_minutes = abs(total_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _time_zone_label(
    value: datetime,
    *,
    preferred_time_zone: str | None = None,
) -> str:
    offset_label = _utc_offset_label(value)
    preferred = str(preferred_time_zone or "").strip()
    if preferred and preferred.lower() != "local":
        if preferred.upper() == "UTC":
            return "UTC"
        return f"{preferred} ({offset_label})" if offset_label else preferred
    zone_key = str(getattr(value.tzinfo, "key", None) or "").strip()
    if zone_key:
        return f"{zone_key} ({offset_label})" if offset_label else zone_key
    return offset_label or str(value.tzname() or "Local time")


def _time_zone_summary(
    values: Iterable[datetime],
    *,
    preferred_time_zone: str | None = None,
) -> str | None:
    date_times = list(values)
    if not date_times:
        return None
    preferred = str(preferred_time_zone or "").strip()
    if preferred and preferred.lower() != "local":
        offsets = {
            offset
            for item in date_times
            if (offset := _utc_offset_label(item)) is not None
        }
        if preferred.upper() == "UTC":
            return "UTC"
        if len(offsets) == 1:
            return f"{preferred} ({next(iter(offsets))})"
        return f"{preferred} (UTC offset changes during this range)"
    labels = {_time_zone_label(item) for item in date_times}
    return (
        next(iter(labels))
        if len(labels) == 1
        else "Multiple time zones"
    )


def _bounded_text(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit - 1].rstrip()}…"


def _entity(ref: EntityRef) -> ReviewEntity:
    kind = ref.target_kind if ref.kind == "temporary" and ref.target_kind else ref.kind
    return ReviewEntity(
        ref=ref,
        label=ref.title or _ENTITY_FALLBACKS.get(kind, "Planning item"),
        context="New" if ref.kind == "temporary" else None,
    )


def _unique_entities(
    refs: Iterable[EntityRef],
    entity_titles: Mapping[str, str] | None = None,
) -> list[ReviewEntity]:
    merged: dict[tuple[str, str, str | None], EntityRef] = {}
    for ref in refs:
        authoritative_title = (
            entity_titles.get(ref.entity_id) if entity_titles is not None else None
        )
        if isinstance(authoritative_title, str) and authoritative_title:
            ref = ref.model_copy(update={"title": authoritative_title})
        identity = (ref.kind, ref.entity_id, ref.target_kind)
        current = merged.get(identity)
        if current is None:
            merged[identity] = ref
            continue
        if (not current.title and ref.title) or (
            current.revision is None and ref.revision is not None
        ):
            merged[identity] = EntityRef.model_validate({
                **current.model_dump(mode="python"),
                "title": current.title or ref.title,
                "revision": current.revision or ref.revision,
            })
    return [_entity(ref) for ref in merged.values()]


def _with_review_entity_titles(
    change_set: ChangeSet,
    entity_titles: Mapping[str, str] | None,
) -> ChangeSet:
    if not entity_titles:
        return change_set
    payload = change_set.model_dump(mode="python")

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        entity_id = value.get("entity_id")
        if (
            isinstance(entity_id, str)
            and isinstance(entity_titles.get(entity_id), str)
        ):
            value["title"] = entity_titles[entity_id]
        for item in value.values():
            visit(item)

    visit(payload)
    return ChangeSet.model_validate(payload)


def _recurrence_text(rule: object) -> tuple[str, str, str | None]:
    frequency = getattr(rule, "frequency", None)
    interval = getattr(rule, "interval", None) or 1
    weekdays = getattr(rule, "weekdays", None) or []
    month_days = getattr(rule, "month_days", None) or []
    start_date = getattr(rule, "start_date", None)
    end_date = getattr(rule, "end_date", None)
    occurrence_count = getattr(rule, "occurrence_count", None)
    preferred_time = getattr(rule, "preferred_start_time", None)
    timezone = getattr(rule, "timezone", None)

    if frequency == "weekly" and weekdays:
        days = ", ".join(day.title() for day in weekdays)
        cadence = f"Every {days}" if interval == 1 else f"Every {interval} weeks on {days}"
    elif frequency == "monthly" and month_days:
        days = ", ".join(str(day) for day in month_days)
        cadence = f"Every month on day {days}" if interval == 1 else f"Every {interval} months on day {days}"
    elif frequency == "daily":
        cadence = "Every day" if interval == 1 else f"Every {interval} days"
    else:
        cadence = "Updated repeating cadence"
    if preferred_time:
        parsed = datetime.strptime(preferred_time, "%H:%M")
        cadence += f" at {parsed.strftime('%I:%M %p').lstrip('0')}"

    if start_date and end_date:
        range_label = f"{_format_date(start_date)} through {_format_date(end_date)}"
    elif start_date and occurrence_count:
        range_label = f"Starting {_format_date(start_date)} for the requested series"
    elif start_date:
        range_label = f"Starting {_format_date(start_date)}"
    else:
        range_label = "The current series range"
    return cadence, range_label, timezone


def _recurrence_patch_text(patch: object) -> tuple[str, str, str | None]:
    fields = set(getattr(patch, "model_fields_set", set()))
    values: list[str] = []
    frequency = getattr(patch, "frequency", None)
    interval = getattr(patch, "interval", None)
    weekdays = getattr(patch, "weekdays", None)
    month_days = getattr(patch, "month_days", None)
    preferred_time = getattr(patch, "preferred_start_time", None)
    duration = getattr(patch, "duration_minutes", None)
    if "frequency" in fields:
        values.append(f"Frequency: {frequency or 'unchanged'}")
    if "interval" in fields:
        values.append(f"Interval: every {interval} cycle{'s' if interval != 1 else ''}")
    if "weekdays" in fields:
        values.append("Days: " + (", ".join(day.title() for day in weekdays or []) or "none"))
    if "month_days" in fields:
        values.append("Month days: " + (", ".join(str(day) for day in month_days or []) or "none"))
    if "preferred_start_time" in fields:
        if preferred_time:
            parsed = datetime.strptime(preferred_time, "%H:%M")
            values.append(f"Preferred time: {parsed.strftime('%I:%M %p').lstrip('0')}")
        else:
            values.append("Remove the preferred time")
    if "duration_minutes" in fields:
        values.append(f"Duration: {duration} minutes")
    cadence = "; ".join(values) or "Keep the existing cadence"

    start_date = getattr(patch, "start_date", None)
    end_date = getattr(patch, "end_date", None)
    occurrence_count = getattr(patch, "occurrence_count", None)
    range_values: list[str] = []
    if "start_date" in fields:
        range_values.append(f"Start: {_format_date(start_date)}" if start_date else "Keep the current start")
    if "end_date" in fields:
        range_values.append(f"End: {_format_date(end_date)}" if end_date else "Remove the end date")
    if "occurrence_count" in fields:
        range_values.append(f"Occurrences: {occurrence_count}" if occurrence_count else "Remove the occurrence limit")
    range_label = "; ".join(range_values) or "Keep the current series range"
    return cadence, range_label, getattr(patch, "timezone", None)


def _preference_value_text(value: object) -> str:
    kind = getattr(value, "kind", None)
    if kind == "scalar":
        scalar = getattr(value, "value", None)
        if scalar is None:
            return "No value"
        logical = decode_preference_value(
            value.model_dump(mode="json", by_alias=True)
            if hasattr(value, "model_dump")
            else None
        )
        gap = logical.get("interTaskGapMinutes")
        threshold = logical.get("minimumTaskDurationMinutes")
        shorter_gap = logical.get("shorterTaskGapMinutes")
        if isinstance(gap, int) and isinstance(threshold, int):
            text = (
                f"{gap}-minute gap between tasks lasting at least "
                f"{threshold} minutes"
            )
            if shorter_gap == 0:
                text += "; shorter tasks can remain back-to-back"
            elif isinstance(shorter_gap, int):
                text += (
                    f"; use a {shorter_gap}-minute gap for shorter tasks"
                )
            return text
        if isinstance(gap, int):
            return f"{gap}-minute gap between tasks"
        if logical.get("preferenceText"):
            return str(logical["preferenceText"])
        return ", ".join(
            f"{str(key).replace('_', ' ')}: {item}"
            for key, item in logical.items()
        ) or str(scalar)
    if kind == "string_set":
        return ", ".join(getattr(value, "values", []))
    if kind == "time_window":
        return (
            f"{getattr(value, 'start_local')} to {getattr(value, 'end_local')} "
            f"in {getattr(value, 'timezone')}"
        )
    raise ReviewProjectionError("unsupported preference display value")


def _is_inter_task_gap_change(operation: object) -> bool:
    if str(getattr(operation, "kind", "")) not in {
        "preference.upsert",
        "preference.supersede",
    }:
        return False
    value = getattr(operation, "value", None)
    logical = decode_preference_value(
        value.model_dump(mode="json", by_alias=True)
        if hasattr(value, "model_dump")
        else None
    )
    return isinstance(logical.get("interTaskGapMinutes"), int)


def _preference_update_before(
    operation: object,
    *,
    supporting_operations: Iterable[object] = (),
) -> str:
    titles: list[str] = []
    seen_titles: set[str] = set()
    for candidate in (operation, *supporting_operations):
        preference_ref = getattr(candidate, "preference_ref", None)
        if not isinstance(preference_ref, EntityRef) or not preference_ref.title:
            continue
        title = " ".join(preference_ref.title.split())
        normalized_title = title.casefold()
        if title and normalized_title not in seen_titles:
            seen_titles.add(normalized_title)
            titles.append(title)

    if len(titles) > 1:
        visible_titles = titles[:4]
        lines = [
            "Replace these overlapping saved preferences:",
            *(f"- {title}" for title in visible_titles),
        ]
        remaining = len(titles) - len(visible_titles)
        if remaining:
            lines.append(
                f"- {remaining} more overlapping preference"
                f"{'s' if remaining != 1 else ''}"
            )
        return "\n".join(lines)
    if titles:
        return titles[0]
    return _operation_before(operation)


def _preference_update_after(operation: object) -> str:
    value_text = _preference_value_text(getattr(operation, "value"))
    semantics = getattr(operation, "semantics")
    if not _is_inter_task_gap_change(operation):
        return _operation_after(operation)

    if value_text.startswith("0-minute"):
        preference_text = f"Use no {value_text.removeprefix('0-minute ')}"
    else:
        verb = "Require" if semantics.strength == "hard" else "Prefer"
        preference_text = f"{verb} a {value_text}"
    scope_text = {
        "global": "Applies to all planning.",
        "workspace": "Applies in this workspace.",
        "task": "Applies to this task.",
        "calendar": "Applies to this calendar.",
    }[semantics.scope]
    return f"{preference_text}. {scope_text}"


def _operation_before(operation: object) -> str:
    kind = str(getattr(operation, "kind", ""))
    if kind in {"task.create", "calendar.create_block", "recurrence.create_series", "preference.upsert"}:
        return "Not present"
    if kind in {
        "task.delete",
        "calendar.delete_block",
        "preference.retire",
        "preference.delete",
    }:
        return "Present in the current plan"
    if kind == "preference.restore":
        return "Disabled for future planning"
    if kind == "preference.supersede":
        return "This existing preference will be replaced"
    if kind == "recurrence.cancel_occurrence":
        return "Included in the current series"
    return "Current reviewed state"


def _preference_cleanup_summary(operation: object) -> str:
    kind = str(getattr(operation, "kind", ""))
    replacement = getattr(operation, "replacement_ref", None)
    reason_code = str(getattr(operation, "reason_code", "") or "")
    if replacement is None:
        return _KIND_SUMMARIES[kind]
    if reason_code == "lower_provenance_conflict":
        return "Use the directly sourced preference"
    if reason_code in {"duplicate_older_copy", "redundant_scoped_copy"}:
        return "Remove an overlapping preference copy"
    return "Use the newer preference"


def _operation_after(operation: object) -> str:
    kind = str(getattr(operation, "kind", ""))
    if kind == "task.create":
        fields = getattr(operation, "fields")
        values = [f"New task: {getattr(operation, 'title', 'Untitled task')}"]
        if fields.priority:
            values.append(f"Priority: {fields.priority.title()}")
        if fields.status:
            values.append(f"Status: {fields.status.replace('_', ' ').title()}")
        if fields.estimate_minutes:
            values.append(f"Estimate: {fields.estimate_minutes} minutes")
        if fields.planned_date:
            values.append(f"Planned day: {_format_date(fields.planned_date)} (no set time)")
        if fields.deadline_at:
            values.append(f"Deadline: {_format_date(fields.deadline_at.date())}")
        if fields.notes:
            values.append("Includes notes")
        if getattr(fields, "is_backlog", None) is not None:
            values.append(
                "Placement: Backlog" if fields.is_backlog else "Placement: Active tree"
            )
        if getattr(fields, "tags", None):
            values.append("Tags: " + ", ".join(fields.tags))
        return "; ".join(values)
    if kind == "task.update":
        updates = getattr(operation, "updates")
        values: list[str] = []
        if getattr(updates, "title", None):
            values.append(f"Title: {updates.title}")
        if getattr(updates, "priority", None):
            values.append(f"Priority: {updates.priority.title()}")
        if getattr(updates, "status", None):
            values.append(f"Status: {updates.status.replace('_', ' ').title()}")
        if getattr(updates, "estimate_minutes", None):
            values.append(f"Estimate: {updates.estimate_minutes} minutes")
        if getattr(updates, "planned_date", None):
            values.append(f"Planned day: {_format_date(updates.planned_date)} (no set time)")
        if getattr(updates, "deadline_at", None):
            values.append(f"Deadline: {_format_date(updates.deadline_at.date())}")
        if getattr(updates, "notes", None):
            values.append("Notes updated")
        if getattr(updates, "is_backlog", None) is not None:
            values.append(
                "Placement: Backlog"
                if updates.is_backlog
                else "Placement: Active tree"
            )
        if getattr(updates, "tags", None) is not None:
            values.append(
                "Tags: " + ", ".join(updates.tags)
                if updates.tags
                else "Tags cleared"
            )
        if getattr(updates, "clear_fields", None):
            labels = {"notes": "notes", "deadline_at": "deadline", "planned_date": "planned day"}
            values.extend(f"Clear {labels[item]}" for item in updates.clear_fields)
        return "; ".join(values) or "Task details updated"
    if kind == "task.move":
        destination = getattr(operation, "destination_parent_ref", None) or getattr(
            operation, "destination_workspace_ref", None
        )
        return f"Move under {_entity(destination).label}" if destination else "Move to the selected destination"
    if kind == "task.archive":
        return "Archived while preserving history"
    if kind == "task.delete":
        return "Removed from the plan"
    if kind in {"calendar.create_block", "calendar.move_block"}:
        return _format_datetime_range(operation.start_at, operation.end_at)
    if kind == "calendar.resize_block":
        return f"Ends {_format_datetime(operation.end_at)}"
    if kind == "calendar.split_block":
        segments = getattr(operation, "segments", [])
        summary = "; ".join(
            _format_datetime_range(segment.start_at, segment.end_at)
            for segment in segments[:6]
        )
        additional = len(segments) - 6
        return f"{summary}; plus {additional} additional segments" if additional > 0 else summary
    if kind == "calendar.delete_block":
        return "Removed from the calendar"
    if kind == "recurrence.create_series":
        cadence, range_label, _ = _recurrence_text(operation.rule)
        return f"{cadence}; {range_label}"
    if kind == "recurrence.update_series":
        cadence, range_label, _ = _recurrence_patch_text(operation.updates)
        return f"{cadence}; {range_label}"
    if kind == "recurrence.update_occurrence":
        updates = operation.updates
        values = []
        if updates.start_at and updates.end_at:
            values.append(
                f"Move this occurrence to {_format_datetime_range(updates.start_at, updates.end_at)}"
            )
        if updates.duration_minutes is not None:
            values.append(f"Duration: {updates.duration_minutes} minutes")
        if not values:
            raise ReviewProjectionError("occurrence update has no displayable semantic value")
        return "; ".join(values)
    if kind == "recurrence.cancel_occurrence":
        return "This occurrence is cancelled; the rest of the series is unchanged"
    if kind in {"preference.upsert", "preference.supersede"}:
        semantics = operation.semantics
        values = [
            f"Value: {_preference_value_text(operation.value)}",
            f"Use as a {semantics.strength} {semantics.domain} preference",
            f"Scope: {semantics.scope}",
        ]
        if semantics.effective_from:
            values.append(f"Effective from {_format_datetime(semantics.effective_from)}")
        if semantics.effective_until:
            values.append(f"Effective until {_format_datetime(semantics.effective_until)}")
        return "; ".join(values)
    if kind == "preference.retire":
        replacement = getattr(operation, "replacement_ref", None)
        if replacement is not None:
            replacement_title = replacement.title or "the newer saved preference"
            reason_code = str(getattr(operation, "reason_code", "") or "")
            if reason_code in {"duplicate_older_copy", "redundant_scoped_copy"}:
                return (
                    f"Keep {replacement_title}; stop using this overlapping older copy"
                )
            if reason_code == "lower_provenance_conflict":
                return (
                    f"Keep the directly sourced preference: {replacement_title}; "
                    "stop using this conflicting record without equivalent user-source evidence"
                )
            return (
                f"Keep the newer preference: {replacement_title}; "
                "stop using this older conflicting preference"
            )
        return "Stop using this preference for future planning"
    if kind == "preference.restore":
        return "Use this preference again for future planning"
    if kind == "preference.delete":
        replacement = getattr(operation, "replacement_ref", None)
        if replacement is not None:
            replacement_title = replacement.title or "the newer saved preference"
            if (
                str(getattr(operation, "reason_code", "") or "")
                == "lower_provenance_conflict"
            ):
                return (
                    f"Keep the directly sourced preference: {replacement_title}; "
                    "permanently delete this conflicting record without equivalent "
                    "user-source evidence"
                )
            return (
                f"Keep the newer preference: {replacement_title}; permanently delete "
                "this older conflicting or overlapping preference"
            )
        return "Permanently delete this preference and its linked scheduling rules"
    return "Apply the described planning change"


def _operation_refs(operation: object, effect_refs: list[EntityRef]) -> list[EntityRef]:
    refs: list[EntityRef] = list(effect_refs)
    for name in (
        "task_ref",
        "task_refs",
        "workspace_ref",
        "parent_ref",
        "destination_parent_ref",
        "destination_workspace_ref",
        "block_ref",
        "block_refs",
        "series_ref",
        "occurrence_ref",
        "preference_ref",
        "temp_ref",
    ):
        value = getattr(operation, name, None)
        if isinstance(value, EntityRef):
            refs.append(value)
        elif isinstance(value, list):
            refs.extend(item for item in value if isinstance(item, EntityRef))
    return refs


def _task_structure_preview(
    *,
    review_id: str,
    operations: list[object],
) -> tuple[TaskStructureBlock | None, set[str]]:
    task_creates = [
        operation
        for operation in operations
        if getattr(operation, "kind", None) == "task.create"
    ]
    if not task_creates:
        return None, set()

    schedules_by_task: dict[str, list[TaskStructureSchedule]] = defaultdict(list)
    represented_ids: set[str] = set()
    for operation in operations:
        if getattr(operation, "kind", None) != "calendar.create_block":
            continue
        task_ref = getattr(operation, "task_ref", None)
        start_at = getattr(operation, "start_at", None)
        end_at = getattr(operation, "end_at", None)
        if (
            not isinstance(task_ref, EntityRef)
            or not isinstance(start_at, datetime)
            or not isinstance(end_at, datetime)
        ):
            continue
        duration_minutes = int((end_at - start_at).total_seconds() // 60)
        if duration_minutes <= 0:
            continue
        schedules_by_task[task_ref.entity_id].append(
            TaskStructureSchedule(
                range_label=_format_datetime_range(start_at, end_at),
                duration_minutes=duration_minutes,
            )
        )

    specs: dict[str, dict[str, object]] = {}
    workspace_refs: dict[str, EntityRef] = {}
    workspace_order: list[str] = []
    create_order: list[str] = []
    for operation in task_creates:
        task_ref = getattr(operation, "temp_ref", None)
        workspace_ref = getattr(operation, "workspace_ref", None)
        if not isinstance(task_ref, EntityRef) or not isinstance(
            workspace_ref, EntityRef
        ):
            continue
        if workspace_ref.entity_id not in workspace_refs:
            workspace_refs[workspace_ref.entity_id] = workspace_ref
            workspace_order.append(workspace_ref.entity_id)
        represented_ids.update((task_ref.entity_id, workspace_ref.entity_id))
        create_order.append(task_ref.entity_id)
        fields = getattr(operation, "fields", None)
        specs[task_ref.entity_id] = {
            "ref": task_ref,
            "state": "new",
            "estimate_minutes": getattr(fields, "estimate_minutes", None),
            "deadline_at": getattr(fields, "deadline_at", None),
            "scheduled_blocks": schedules_by_task.get(task_ref.entity_id, []),
            "children": [],
            "workspace_id": workspace_ref.entity_id,
            "parent_ref": getattr(operation, "parent_ref", None),
        }

    roots_by_workspace: dict[str, list[str]] = defaultdict(list)
    for task_id in create_order:
        spec = specs[task_id]
        workspace_id = str(spec["workspace_id"])
        parent_ref = spec.get("parent_ref")
        if isinstance(parent_ref, EntityRef):
            represented_ids.add(parent_ref.entity_id)
            if parent_ref.entity_id not in specs:
                specs[parent_ref.entity_id] = {
                    "ref": parent_ref,
                    "state": "existing",
                    "estimate_minutes": None,
                    "deadline_at": None,
                    "scheduled_blocks": [],
                    "children": [],
                    "workspace_id": workspace_id,
                    "parent_ref": None,
                }
                roots_by_workspace[workspace_id].append(parent_ref.entity_id)
            children = specs[parent_ref.entity_id]["children"]
            assert isinstance(children, list)
            if task_id not in children:
                children.append(task_id)
        elif task_id not in roots_by_workspace[workspace_id]:
            roots_by_workspace[workspace_id].append(task_id)

    def build_item(task_id: str, ancestors: frozenset[str]) -> TaskStructureItem:
        if task_id in ancestors:
            raise ReviewProjectionError("proposed task structure contains a cycle")
        spec = specs[task_id]
        task_ref = spec["ref"]
        child_ids = spec["children"]
        assert isinstance(task_ref, EntityRef)
        assert isinstance(child_ids, list)
        return TaskStructureItem(
            task=_entity(task_ref),
            state=str(spec["state"]),
            estimate_minutes=spec["estimate_minutes"],
            deadline_at=spec["deadline_at"],
            scheduled_blocks=list(spec["scheduled_blocks"]),
            children=[
                build_item(child_id, ancestors | {task_id})
                for child_id in child_ids
            ],
        )

    workspace_trees = [
        TaskStructureWorkspace(
            workspace=_entity(workspace_refs[workspace_id]),
            roots=[
                build_item(task_id, frozenset())
                for task_id in roots_by_workspace[workspace_id]
            ],
        )
        for workspace_id in workspace_order
        if roots_by_workspace[workspace_id]
    ]
    if not workspace_trees:
        return None, set()
    represented_ids.update(
        ref.entity_id
        for operation in operations
        if getattr(operation, "kind", None) == "calendar.create_block"
        for ref in _operation_refs(operation, [])
        if (
            ref.kind == "calendar_block"
            or ref.target_kind == "calendar_block"
        )
    )
    return (
        TaskStructureBlock(
            block_id=_stable_id("block", review_id, "task-structure"),
            title="Proposed task structure",
            workspaces=workspace_trees,
        ),
        represented_ids,
    )


def _resolution_text(recommendation: str) -> str:
    return {
        "accept": "The proposed arrangement remains valid.",
        "move_incoming": "Move the flexible incoming work.",
        "move_blocker": "Move the flexible existing commitment.",
        "split_incoming": "Split the flexible work around the commitment.",
        "preserve_existing": "Keep the existing commitment unchanged.",
        "revise_plan": "Revise the plan before approval.",
        "ask_user": "A decision is needed before approval.",
        "reject": "This arrangement cannot be applied safely.",
    }.get(recommendation, "Resolve the conflict before approval.")


def review_copy_hygiene_violations(document: ReviewDocument) -> list[str]:
    """Return technical-copy leaks from system-owned review prose.

    Entity labels, semantic before/after values, assumptions, and exception text may
    contain user-authored content, so they are intentionally not rewritten or scanned.
    """
    values = [document.summary]
    for decision in document.decisions:
        values.extend((decision.question, decision.reason))
        values.extend(option.label for option in decision.options)
    for block in document.blocks:
        values.append(block.title)
        if isinstance(block, EntityReferencesBlock):
            if block.additional_entities:
                values.append(block.additional_entities)
        elif isinstance(block, GroupedChangesBlock):
            values.extend(group.summary for group in block.groups)
            values.extend(change.summary for group in block.groups for change in group.changes)
        elif isinstance(block, SemanticDiffBlock):
            values.extend(change.summary for change in block.changes)
        elif isinstance(block, SemanticFallbackBlock):
            values.append(block.summary)
            values.extend(change.summary for change in block.effects)
        elif isinstance(block, ScheduleSummaryBlock):
            values.extend((block.cadence, block.range_label))
            if block.time_zone:
                values.append(block.time_zone)
            values.extend(block.details)
        elif isinstance(block, RecurrenceSummaryBlock):
            values.extend((block.cadence, block.range_label, block.scope_label))
            if block.time_zone:
                values.append(block.time_zone)
            values.extend(block.exceptions)
            if block.additional_exceptions:
                values.append(block.additional_exceptions)
        elif isinstance(block, ConflictResolutionBlock):
            values.extend(value for item in block.conflicts for value in (item.summary, item.resolution))
            values.extend(value for item in block.conflicts for value in item.intervals)
            values.extend(value for item in block.conflicts for value in item.alternatives)
            values.extend(
                value
                for item in block.conflicts
                for value in (
                    item.scope_label,
                    item.additional_affected_entities,
                    item.additional_intervals,
                    item.additional_alternatives,
                )
                if value
            )
        elif isinstance(block, WarningBlock):
            values.append(block.message)
        elif isinstance(block, ExecutionStatusBlock):
            values.extend((block.summary, block.verification_summary))
            values.extend(sync.summary for sync in block.sync_items)

    return [
        value
        for value in values
        if any(pattern.search(value) for pattern in _TECHNICAL_COPY_PATTERNS)
    ]


class ReviewDocumentProjector:
    def project(
        self,
        *,
        session_id: str,
        goal: GoalState,
        change_set: ChangeSet,
        simulation: ChangeSetSimulationResult,
        resolution_simulation: ChangeSetSimulationResult | None = None,
        execution: LocalExecutionResult | None = None,
        replayed: bool = False,
        entity_titles: Mapping[str, str] | None = None,
        display_time_zone: str | None = None,
    ) -> ReviewDocument:
        self._validate_inputs(goal, change_set, simulation, resolution_simulation)
        self._validate_execution(session_id, goal, change_set, simulation, execution)
        display_change_set = _with_review_entity_titles(change_set, entity_titles)
        review_id = _stable_id("review", goal.goal_id, goal.revision, change_set.change_set_id, change_set.revision, simulation.simulation_id)
        effect_by_operation = {effect.operation_id: effect for effect in simulation.projected_effects}
        changes: list[ReviewChange] = []
        all_refs: list[EntityRef] = []
        groups: dict[str, list[ReviewChange]] = defaultdict(list)
        domains: set[str] = set()
        operations_by_id = {
            operation.operation_id: operation
            for operation in display_change_set.operations
        }
        operation_order = display_change_set.dependency_order()
        preference_primary_by_group = {
            operation.atomic_group_id: operation
            for operation in display_change_set.operations
            if operation.atomic_group_id
            and operation.kind
            in {"preference.upsert", "preference.supersede"}
        }
        supporting_preference_ids_by_group: dict[str, list[str]] = defaultdict(
            list
        )
        for operation_id in operation_order:
            operation = operations_by_id[operation_id]
            if (
                operation.kind == "preference.retire"
                and operation.atomic_group_id in preference_primary_by_group
                and str(getattr(operation, "reason_code", "") or "")
                in {"duplicate_older_copy", "redundant_scoped_copy"}
            ):
                supporting_preference_ids_by_group[
                    operation.atomic_group_id
                ].append(operation.operation_id)
        supporting_preference_ids = {
            operation_id
            for operation_ids in supporting_preference_ids_by_group.values()
            for operation_id in operation_ids
        }
        for operation_id in operation_order:
            operation = operations_by_id[operation_id]
            if operation_id in supporting_preference_ids:
                continue
            effect = effect_by_operation[operation.operation_id]
            refs = _operation_refs(operation, effect.entity_refs)
            all_refs.extend(refs)
            domain = operation.kind.split(".", 1)[0]
            domains.add(domain)
            supporting_operation_ids = (
                supporting_preference_ids_by_group.get(
                    operation.atomic_group_id,
                    [],
                )
                if operation.atomic_group_id
                else []
            )
            supporting_operations = [
                operations_by_id[item]
                for item in supporting_operation_ids
            ]
            is_gap_change = _is_inter_task_gap_change(operation)
            change = ReviewChange(
                change_id=operation.operation_id,
                supporting_operation_ids=supporting_operation_ids,
                summary=(
                    (
                        "Replace overlapping gap preferences"
                        if supporting_operations
                        else "Update gap between tasks"
                    )
                    if is_gap_change
                    else
                    _preference_cleanup_summary(operation)
                    if operation.kind
                    in {"preference.retire", "preference.delete"}
                    else _KIND_SUMMARIES[operation.kind]
                ),
                entities=_unique_entities(refs, entity_titles),
                before=(
                    _preference_update_before(
                        operation,
                        supporting_operations=supporting_operations,
                    )
                    if is_gap_change
                    else _operation_before(operation)
                ),
                after=(
                    _preference_update_after(operation)
                    if is_gap_change
                    else _operation_after(operation)
                ),
            )
            changes.append(change)
            groups[operation.atomic_group_id or domain].append(change)

        blocks = []
        entities = _unique_entities(all_refs, entity_titles)
        displayed_operations = [
            operations_by_id[operation_id]
            for operation_id in operation_order
            if operation_id not in supporting_preference_ids
        ]
        compact_gap_review = (
            len(displayed_operations) == 1
            and _is_inter_task_gap_change(displayed_operations[0])
        )
        task_structure, represented_structure_ids = _task_structure_preview(
            review_id=review_id,
            operations=[
                operations_by_id[operation_id]
                for operation_id in operation_order
            ],
        )
        if task_structure is not None:
            blocks.append(task_structure)
        remaining_entities = [
            entity
            for entity in entities
            if entity.ref.entity_id not in represented_structure_ids
            and not (
                task_structure is not None
                and (
                    entity.ref.kind == "calendar_block"
                    or entity.ref.target_kind == "calendar_block"
                )
            )
        ]
        if remaining_entities and not compact_gap_review:
            shown_entities = remaining_entities[:512]
            blocks.append(EntityReferencesBlock(
                block_id=_stable_id("block", review_id, "entities"),
                title=(
                    "Other affected items"
                    if task_structure is not None
                    else "Affected items"
                ),
                entities=shown_entities,
                additional_entities=(
                    f"{len(remaining_entities) - len(shown_entities)} additional affected items are included."
                    if len(remaining_entities) > len(shown_entities)
                    else None
                ),
            ))

        if len(domains) >= 3:
            blocks.append(SemanticFallbackBlock(
                block_id=_stable_id("block", review_id, "fallback"),
                title="Plan changes",
                summary="These related planning changes will be applied together.",
                effects=changes,
            ))
        elif len(groups) > 1:
            review_groups = [
                ReviewChangeGroup(
                    group_id=_stable_id("group", review_id, key),
                    summary=("Related changes" if key.startswith("group:") else _DOMAIN_TITLES.get(key, "Planning changes")),
                    changes=value,
                )
                for key, value in groups.items()
            ]
            blocks.append(GroupedChangesBlock(block_id=_stable_id("block", review_id, "groups"), title="What will change", groups=review_groups))
        else:
            blocks.append(SemanticDiffBlock(block_id=_stable_id("block", review_id, "diff"), title="Before and after", changes=changes))

        calendar_operations = [
            item
            for item in display_change_set.operations
            if item.kind.startswith("calendar.")
        ]
        if calendar_operations:
            starts = [getattr(item, "start_at", None) for item in calendar_operations]
            ends = [getattr(item, "end_at", None) for item in calendar_operations]
            starts = [item for item in starts if isinstance(item, datetime)]
            ends = [item for item in ends if isinstance(item, datetime)]
            referenced_times = [
                ref.title
                for operation in calendar_operations
                for ref in (
                    list(getattr(operation, "block_refs", []) or [])
                    + ([getattr(operation, "block_ref")] if getattr(operation, "block_ref", None) else [])
                )
                if isinstance(ref, EntityRef) and ref.title
            ]
            range_label = (
                _format_datetime_range(min(starts), max(ends))
                if starts and ends
                else referenced_times[0]
                if len(referenced_times) == 1
                else f"{len(referenced_times)} selected scheduled items"
                if referenced_times
                else "The selected scheduled time"
            )
            operation_kinds = {item.kind for item in calendar_operations}
            cadence = (
                "Cancellation"
                if operation_kinds == {"calendar.delete_block"}
                else "New scheduled time"
                if operation_kinds == {"calendar.create_block"}
                else "Rescheduled work"
                if operation_kinds == {"calendar.move_block"}
                else "Duration change"
                if operation_kinds == {"calendar.resize_block"}
                else "Split scheduled work"
                if operation_kinds == {"calendar.split_block"}
                else "Calendar changes"
            )
            details = []
            for operation in calendar_operations:
                labels = [
                    ref.title
                    for ref in (
                        list(getattr(operation, "block_refs", []) or [])
                        + ([getattr(operation, "block_ref")] if getattr(operation, "block_ref", None) else [])
                    )
                    if isinstance(ref, EntityRef) and ref.title
                ]
                task_ref = getattr(operation, "task_ref", None)
                task_title = (
                    task_ref.title
                    if isinstance(task_ref, EntityRef) and task_ref.title
                    else None
                )
                start_at = getattr(operation, "start_at", None)
                end_at = getattr(operation, "end_at", None)
                duration_minutes = (
                    int((end_at - start_at).total_seconds() // 60)
                    if isinstance(start_at, datetime)
                    and isinstance(end_at, datetime)
                    else None
                )
                detail = _KIND_SUMMARIES[operation.kind]
                if operation.kind == "calendar.delete_block" and labels:
                    detail = f"{detail}: {', '.join(labels)}"
                elif task_title and duration_minutes:
                    detail = (
                        f"{task_title} \u2014 {duration_minutes} minutes scheduled: "
                        f"{_operation_after(operation)}"
                    )
                elif labels:
                    detail = (
                        f"{detail}: {', '.join(labels)}; "
                        f"{_operation_after(operation)}"
                    )
                else:
                    detail = f"{detail}: {_operation_after(operation)}"
                details.append(detail)
            if len(details) > 12:
                shown_count = 11
                kind_counts: dict[str, int] = defaultdict(int)
                for operation in calendar_operations:
                    kind_counts[operation.kind] += 1
                count_summary = ", ".join(
                    f"{count} {_KIND_SUMMARIES[kind].lower()}"
                    for kind, count in sorted(kind_counts.items())
                )
                details = [
                    *details[:shown_count],
                    (
                        f"{len(details) - shown_count} additional calendar changes "
                        f"follow the same reviewed pattern ({count_summary})."
                    ),
                ]
            blocks.append(ScheduleSummaryBlock(
                block_id=_stable_id("block", review_id, "schedule"),
                title="Schedule",
                cadence=cadence,
                range_label=range_label,
                time_zone=_time_zone_summary(
                    [*starts, *ends],
                    preferred_time_zone=display_time_zone,
                ),
                details=details,
            ))

        recurrence_operations = [
            item
            for item in display_change_set.operations
            if item.kind.startswith("recurrence.")
        ]
        for index, operation in enumerate(recurrence_operations):
            rule = getattr(operation, "rule", None) or getattr(operation, "updates", None)
            if operation.kind == "recurrence.cancel_occurrence":
                occurrence = operation.occurrence_ref.title or "The selected occurrence"
                cadence, range_label, timezone = "Existing cadence", occurrence, None
                scope_label = "This occurrence only"
            elif operation.kind == "recurrence.update_occurrence":
                occurrence = operation.occurrence_ref.title or "The selected occurrence"
                cadence, range_label, timezone = "Existing cadence", occurrence, None
                scope_label = "This occurrence only"
            elif operation.kind == "recurrence.update_series":
                cadence, range_label, timezone = _recurrence_patch_text(rule)
                scope_label = "This series"
            else:
                cadence, range_label, timezone = _recurrence_text(rule)
                scope_label = "This series"
            exceptions = self._recurrence_exceptions(operation.operation_id, operation, simulation)
            shown = exceptions[:6]
            additional = (
                f"{len(exceptions) - len(shown)} additional exceptions follow the same reviewed pattern."
                if len(exceptions) > len(shown)
                else None
            )
            blocks.append(RecurrenceSummaryBlock(
                block_id=_stable_id("block", review_id, "recurrence", index),
                title="Repeating schedule",
                cadence=cadence,
                range_label=range_label,
                scope_label=scope_label,
                time_zone=timezone,
                exceptions=shown,
                additional_exceptions=additional,
            ))

        conflict_source = resolution_simulation or simulation
        if conflict_source.conflict_graph.conflicts:
            conflicts: list[ConflictReviewItem] = []
            for conflict in conflict_source.conflict_graph.conflicts:
                blocker_refs = [item.entity_ref for item in conflict.blockers if item.entity_ref]
                affected_entities = _unique_entities(blocker_refs)
                interval_labels = [
                    _format_datetime_range(item.start_at, item.end_at)
                    for item in conflict.overlap_intervals
                ]
                shown_intervals = interval_labels[:12]
                alternatives = list(dict.fromkeys(
                    item.summary for item in conflict.alternatives
                ))
                selected = next(
                    (
                        item
                        for item in conflict.alternatives
                        if item.alternative_id == change_set.resolution_alternative_id
                    ),
                    None,
                )
                scopes = {item.recurrence_scope for item in conflict.blockers}
                scope_label = (
                    "This repeating series"
                    if "series" in scopes
                    else "Only the affected occurrence"
                    if "occurrence" in scopes
                    else "The listed scheduled work"
                )
                conflicts.append(ConflictReviewItem(
                    conflict_id=conflict.conflict_id,
                    summary=(
                        f"The requested time conflicts with the affected commitment or rule during {shown_intervals[0]}."
                        if len(shown_intervals) == 1
                        else "The requested time conflicts with the affected commitments or rules during the listed times."
                        if shown_intervals
                        else "The requested time conflicts with the affected commitment or scheduling rule shown below."
                    ),
                    resolution=(
                        f"Selected resolution: {selected.summary}"
                        if selected is not None
                        else _resolution_text(conflict.policy_recommendation)
                    ),
                    affected_entities=affected_entities[:32],
                    additional_affected_entities=(
                        f"{len(affected_entities) - 32} additional affected items are included."
                        if len(affected_entities) > 32
                        else None
                    ),
                    scope_label=scope_label,
                    intervals=shown_intervals,
                    additional_intervals=(
                        f"{len(interval_labels) - len(shown_intervals)} additional conflict intervals are covered."
                        if len(interval_labels) > len(shown_intervals)
                        else None
                    ),
                    alternatives=alternatives[:12],
                    additional_alternatives=(
                        f"{len(alternatives) - 12} additional alternatives are available."
                        if len(alternatives) > 12
                        else None
                    ),
                ))
            blocks.append(ConflictResolutionBlock(block_id=_stable_id("block", review_id, "conflicts"), title="Conflict resolution", conflicts=conflicts))

        assumption_copy = [item.statement for item in goal.assumptions if item.status == "active"]
        assumption_copy.extend(item.source_text for item in change_set.assumptions if item.source_text)
        if assumption_copy:
            bounded_assumptions = [_bounded_text(item) for item in assumption_copy]
            shown_assumptions = bounded_assumptions[:64]
            if len(bounded_assumptions) > 64:
                shown_assumptions = [
                    *shown_assumptions[:63],
                    f"{len(bounded_assumptions) - 63} additional assumptions are included.",
                ]
            blocks.append(AssumptionBlock(
                block_id=_stable_id("block", review_id, "assumptions"),
                title="Assumptions",
                statements=shown_assumptions,
            ))
        binding = ReviewActionBinding(
            session_id=session_id,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            simulation_id=simulation.simulation_id,
            simulation_input_fingerprint=simulation.input_snapshot.fingerprint,
            simulation_output_fingerprint=simulation.output_fingerprint,
            correlation_id=simulation.correlation_id,
            causation_id=simulation.causation_id,
        )
        decisions = list(goal.unresolved_decisions)
        controls = []
        if decisions:
            for decision in decisions:
                for option in decision.options:
                    controls.append(DecisionReviewAction(
                        event_id=_stable_id("event", review_id, decision.decision_id, option.option_id),
                        idempotency_key=_stable_id("idem", review_id, decision.decision_id, option.option_id),
                        review_id=review_id,
                        review_revision=1,
                        binding=binding,
                        decision_id=decision.decision_id,
                        option_id=option.option_id,
                    ))
            controls.extend([
                EditReviewAction(
                    event_id=_stable_id("event", review_id, "edit"),
                    idempotency_key=_stable_id("idem", review_id, "edit"),
                    review_id=review_id,
                    review_revision=1,
                    binding=binding,
                ),
                RejectReviewAction(
                    event_id=_stable_id("event", review_id, "reject"),
                    idempotency_key=_stable_id("idem", review_id, "reject"),
                    review_id=review_id,
                    review_revision=1,
                    binding=binding,
                ),
            ])
        else:
            controls = [
                ApproveReviewAction(
                    event_id=_stable_id("event", review_id, "approve"),
                    command_id=_stable_id("command", review_id, "approve"),
                    idempotency_key=_stable_id("idem", review_id, "approve"),
                    review_id=review_id,
                    review_revision=1,
                    binding=binding,
                ),
                EditReviewAction(
                    event_id=_stable_id("event", review_id, "edit"),
                    idempotency_key=_stable_id("idem", review_id, "edit"),
                    review_id=review_id,
                    review_revision=1,
                    binding=binding,
                ),
                RejectReviewAction(
                    event_id=_stable_id("event", review_id, "reject"),
                    idempotency_key=_stable_id("idem", review_id, "reject"),
                    review_id=review_id,
                    review_revision=1,
                    binding=binding,
                ),
            ]
        if execution is not None:
            verification = execution.verification
            verification_summary = verification.recovery_summary or {
                "verified": "Fresh local state matches the reviewed result.",
                "pending": "Fresh local verification is pending.",
                "verification_failed": "Fresh local state differs from the reviewed result.",
            }[verification.status]
            blocks.append(ExecutionStatusBlock(
                block_id=_stable_id("block", review_id, "execution", execution.execution_id),
                title="Execution status",
                status=execution.status,
                summary=(
                    "The reviewed local changes were verified."
                    if execution.status == "applied"
                    else "The local changes committed and are awaiting verification."
                    if execution.status == "committed_pending_verification"
                    else "The local changes committed but verification needs attention."
                ),
                verification_status=verification.status,
                verification_summary=verification_summary,
                recovery=verification.recovery,
                execution_id=execution.execution_id,
                command_id=execution.command_id,
                idempotency_key=execution.idempotency_key,
                replayed=replayed,
                bindings=execution.bindings,
                facts=execution.facts,
                sync_items=[self._sync_status(item) for item in execution.sync_items],
            ))
        entity_labels = [item.label for item in entities[:3]]
        domain_labels = [_DOMAIN_TITLES.get(domain, "Planning") for domain in sorted(domains)]
        summary_subject = ", ".join(entity_labels) if entity_labels else ", ".join(domain_labels)
        if task_structure is not None:
            workspace_labels = [
                tree.workspace.label for tree in task_structure.workspaces
            ]
            structure_scope = ", ".join(workspace_labels)
            summary_subject = (
                f"the proposed task structure and schedule in {structure_scope}"
                if calendar_operations
                else f"the proposed task structure in {structure_scope}"
            )
        elif compact_gap_review:
            summary_subject = "your updated gap-between-tasks preference"
        document = ReviewDocument(
            review_id=review_id,
            session_id=session_id,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            simulation_id=simulation.simulation_id,
            simulation_input_fingerprint=simulation.input_snapshot.fingerprint,
            simulation_output_fingerprint=simulation.output_fingerprint,
            correlation_id=simulation.correlation_id,
            causation_id=simulation.causation_id,
            summary=(
                f"Review {summary_subject} before applying it."
                if task_structure is not None
                else f"Review {summary_subject} before applying it."
                if compact_gap_review
                else f"Review the proposed changes for {summary_subject or 'this plan'} before applying them."
            ),
            blocks=blocks,
            decisions=decisions,
            controls=controls,
        )
        violations = review_copy_hygiene_violations(document)
        if violations:
            raise ReviewProjectionError("review contains technical implementation copy")
        return document

    @staticmethod
    def _recurrence_exceptions(
        operation_id: str,
        operation: object,
        simulation: ChangeSetSimulationResult,
    ) -> list[str]:
        values: list[str] = []
        if getattr(operation, "kind", None) == "recurrence.cancel_occurrence":
            title = operation.occurrence_ref.title or "the selected occurrence"
            values.append(f"Cancel {title}")
        elif getattr(operation, "kind", None) == "recurrence.update_occurrence":
            title = operation.occurrence_ref.title or "the selected occurrence"
            values.append(f"Change {title}")
        for conflict in simulation.conflict_graph.conflicts:
            if operation_id not in conflict.incoming_operation_ids:
                continue
            values.extend(
                f"Conflict on {_format_date(interval.start_at.date())}"
                for interval in conflict.overlap_intervals
            )
        return list(dict.fromkeys(values))

    @staticmethod
    def _sync_status(item: object) -> ExternalSyncReviewStatus:
        status = str(item.status)
        summary = item.error_summary or {
            "not_configured": "Calendar delivery needs a connected account.",
            "pending": "Calendar delivery is pending.",
            "delivering": "Calendar delivery is in progress.",
            "succeeded": "Calendar delivery succeeded.",
            "failed": "Calendar delivery needs inspection.",
        }[status]
        error_code = getattr(item, "last_error_code", None)
        return ExternalSyncReviewStatus(
            sync_id=item.outbox_id,
            provider_label="Google Calendar" if item.provider == "google" else "External calendar",
            status=status,
            summary=summary,
            attempts=item.attempts,
            retry_at=item.next_attempt_at,
            error_code=(error_code.value if hasattr(error_code, "value") else error_code),
            recovery=item.recovery,
            remote_id=item.remote_id,
            remote_version=item.remote_version,
        )

    @staticmethod
    def _validate_inputs(
        goal: GoalState,
        change_set: ChangeSet,
        simulation: ChangeSetSimulationResult,
        resolution_simulation: ChangeSetSimulationResult | None,
    ) -> None:
        identity_matches = (
            goal.current_change_set_id == change_set.change_set_id
            and goal.current_change_set_revision == change_set.revision
            and change_set.goal_id == goal.goal_id
            and change_set.simulation_result_ref == simulation.simulation_id
            and simulation.goal_id == goal.goal_id
            and simulation.resulting_goal_revision == goal.revision
            and simulation.change_set_id == change_set.change_set_id
            and simulation.change_set_revision == change_set.revision
        )
        if not identity_matches:
            raise ReviewProjectionError("review identity is stale or inconsistent")
        if simulation.status not in {"ready_for_review", "needs_user_decision"}:
            raise ReviewProjectionError("simulation is not reviewable")
        if goal.status not in {"ready_for_review", "needs_user_decision"}:
            raise ReviewProjectionError("goal is not reviewable")
        if change_set.status not in {"ready_for_review", "needs_user_decision"}:
            raise ReviewProjectionError("ChangeSet is not reviewable")
        expected_status = {
            "ready_for_review": "ready_for_review",
            "needs_user_decision": "needs_user_decision",
        }[simulation.status]
        if goal.status != expected_status or change_set.status != expected_status:
            raise ReviewProjectionError("review lifecycle does not match simulation disposition")
        if simulation.issues:
            raise ReviewProjectionError("reviewable simulation cannot retain typed failure issues")
        if simulation.status == "ready_for_review":
            if simulation.conflict_graph.status != "clear" or goal.unresolved_decisions:
                raise ReviewProjectionError("approval requires clear conflict and decision evidence")
        else:
            decision = simulation.conflict_graph.decision_request
            if (
                simulation.conflict_graph.status != "needs_user_decision"
                or decision is None
                or len(goal.unresolved_decisions) != 1
                or goal.unresolved_decisions[0].decision_id != decision.decision_id
            ):
                raise ReviewProjectionError("decision evidence is missing or inconsistent")
        report = simulation.conservation
        if (
            not report.complete
            or report.unresolved_clause_ids
            or report.unsupported_clause_ids
            or report.failed_clause_ids
            or report.extra_operation_ids
        ):
            raise ReviewProjectionError("requested changes were not fully conserved")
        expected_ids = {item.operation_id for item in change_set.operations}
        actual_ids = {item.operation_id for item in simulation.projected_effects}
        if expected_ids != actual_ids:
            raise ReviewProjectionError("simulation effects do not conserve the exact ChangeSet")
        expected_fingerprints = {
            item.operation_id: (item.kind, item.sha256)
            for item in (operation_fingerprint(operation) for operation in change_set.operations)
        }
        actual_fingerprints = {
            item.operation_id: (item.kind, item.sha256)
            for item in report.operation_fingerprints
        }
        if actual_fingerprints != expected_fingerprints:
            raise ReviewProjectionError("conservation fingerprints do not match the exact ChangeSet")
        operation_by_id = {item.operation_id: item for item in change_set.operations}
        if any(
            effect.kind != _EXPECTED_EFFECT_KINDS[operation_by_id[effect.operation_id].kind]
            for effect in simulation.projected_effects
        ):
            raise ReviewProjectionError("simulation effect kind contradicts its operation")
        has_resolution = (
            change_set.source_simulation_id is not None
            or change_set.resolution_alternative_id is not None
        )
        if has_resolution:
            if (
                resolution_simulation is None
                or resolution_simulation.simulation_id != change_set.source_simulation_id
                or not any(
                    alternative.alternative_id == change_set.resolution_alternative_id
                    for conflict in resolution_simulation.conflict_graph.conflicts
                    for alternative in conflict.alternatives
                )
            ):
                raise ReviewProjectionError("selected conflict resolution evidence is unavailable")
        elif resolution_simulation is not None:
            raise ReviewProjectionError("unrelated resolution simulation was supplied")

    @staticmethod
    def _validate_execution(
        session_id: str,
        goal: GoalState,
        change_set: ChangeSet,
        simulation: ChangeSetSimulationResult,
        execution: LocalExecutionResult | None,
    ) -> None:
        if execution is None:
            return
        if (
            execution.session_id != session_id
            or execution.goal_id != goal.goal_id
            or execution.goal_revision < goal.revision
            or execution.change_set_id != change_set.change_set_id
            or execution.change_set_revision != change_set.revision
            or execution.simulation_id != simulation.simulation_id
            or execution.correlation_id != simulation.correlation_id
            or execution.causation_id != simulation.causation_id
        ):
            raise ReviewProjectionError("execution evidence does not match the reviewed plan")
