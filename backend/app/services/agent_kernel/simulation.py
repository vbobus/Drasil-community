"""Read-only native ChangeSet snapshot loading and final-world projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Hashable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import PreferenceLink, SchedulingConstraint, UserPreference
from app.models.recurrence_series import (
    RecurrenceOccurrenceException,
    RecurrenceSeries,
)
from app.services.preference_values import (
    decode_preference_value,
    enforcement_constraint_type,
)
from app.services.agent_kernel.contracts import (
    ChangeSet,
    EntityRef,
    ProjectedEffect,
    ProjectedFinalFact,
    RecurrenceRule,
    SimulationSnapshot,
    SnapshotFact,
)
from app.services.agent_kernel.recurrence_materialization import (
    DEFAULT_RECURRENCE_MATERIALIZATION_OCCURRENCES,
    recurrence_occurrence_dates,
)


class SimulationProjectionError(RuntimeError):
    def __init__(self, code: str, message: str, *, operation_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


MAX_SIMULATION_EVIDENCE_FACTS = 512

EntityIdentity = tuple[str, str]
EvidenceT = TypeVar("EvidenceT")


def _change_set_entity_identities(change_set: ChangeSet) -> set[EntityIdentity]:
    """Collect every persisted entity explicitly addressed by the ChangeSet."""

    identities: set[EntityIdentity] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            kind = str(value.get("kind") or "").strip()
            entity_id = str(value.get("entity_id") or "").strip()
            if kind and kind != "temporary" and entity_id:
                identities.add((kind, entity_id))
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)

    collect(change_set.model_dump(mode="python", by_alias=False))
    return identities


def _bounded_unique_evidence(
    items: list[EvidenceT],
    *,
    identity: Callable[[EvidenceT], Hashable],
    is_required: Callable[[EvidenceT], bool],
) -> list[EvidenceT]:
    """Return deterministic, unique evidence with operation-relevant items first.

    Simulation fingerprints still cover the complete projected world. These lists
    are bounded contract evidence, not another full copy of that world.
    """

    ranked = sorted(
        items,
        key=lambda item: (
            0 if is_required(item) else 1,
            identity(item),
        ),
    )
    selected: list[EvidenceT] = []
    seen: set[Hashable] = set()
    for item in ranked:
        key = identity(item)
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) == MAX_SIMULATION_EVIDENCE_FACTS:
            break
    return selected


def _bounded_snapshot_facts(
    facts: list[SnapshotFact],
    *,
    required_entities: set[EntityIdentity],
) -> list[SnapshotFact]:
    return _bounded_unique_evidence(
        facts,
        identity=lambda fact: fact.fact_id,
        is_required=lambda fact: (
            fact.entity_ref is not None
            and (fact.entity_ref.kind, fact.entity_ref.entity_id) in required_entities
        ),
    )


def _bounded_projected_facts(
    facts: list[ProjectedFinalFact],
    *,
    required_entities: set[EntityIdentity],
) -> list[ProjectedFinalFact]:
    return _bounded_unique_evidence(
        facts,
        identity=lambda fact: (
            fact.domain,
            fact.entity_ref.entity_id if fact.entity_ref is not None else fact.fingerprint,
        ),
        is_required=lambda fact: (
            bool(fact.source_operation_ids)
            or (
                fact.entity_ref is not None
                and (fact.entity_ref.kind, fact.entity_ref.entity_id) in required_entities
            )
        ),
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _aware_in_zone(value: datetime, timezone_name: str | None) -> datetime:
    if value.tzinfo is not None or not timezone_name:
        return _aware(value)
    try:
        return value.replace(tzinfo=ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        return _aware(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()}"


def _string_set(value: str | None) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = value.split(",")
    if not isinstance(parsed, list):
        return set()
    return {str(item).strip().lower() for item in parsed if str(item).strip()}


@dataclass
class ProjectedTask:
    ref: EntityRef
    workspace_ref: EntityRef
    title: str
    parent_ref: EntityRef | None = None
    notes: str | None = None
    priority: str = "medium"
    priority_authoritative: bool = True
    status: str = "not_started"
    estimate_minutes: int | None = None
    deadline_at: datetime | None = None
    source_operation_id: str | None = None


@dataclass
class ProjectedCalendarBlock:
    ref: EntityRef
    task_ref: EntityRef
    workspace_ref: EntityRef
    start_at: datetime
    end_at: datetime
    priority: str = "medium"
    priority_authoritative: bool = True
    deadline_at: datetime | None = None
    is_pinned: bool = False
    is_protected: bool = False
    is_recurring: bool = False
    anchoring: str = "unknown"
    movability: str = "unknown"
    series_ref: EntityRef | None = None
    occurrence_ref: EntityRef | None = None
    source_operation_id: str | None = None
    gap_minutes: int = 0


@dataclass
class ProjectedRecurrenceSeries:
    ref: EntityRef
    task_ref: EntityRef
    workspace_ref: EntityRef
    rule: dict[str, Any]
    status: str = "active"
    source_operation_id: str | None = None


@dataclass
class ProjectedPreference:
    ref: EntityRef
    key: str
    value: Any
    domain: str
    strength: str
    scope: str
    scope_id: str | None = None
    active: bool = True
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    source_operation_id: str | None = None


@dataclass
class ProjectedConstraint:
    constraint_id: str
    kind: str
    hard: bool
    value: Any
    description: str | None = None
    active: bool = True
    source: str = "stored"
    target_type: str = "global"
    target_id: str | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    source_preference_id: str | None = None


@dataclass
class ProjectedPreferenceLink:
    ref: EntityRef
    preference_id: str
    target_type: str
    target_id: str
    relation_type: str
    weight: float
    active: bool = True
    source_operation_id: str | None = None


@dataclass
class SimulationWorld:
    tasks: dict[str, ProjectedTask] = field(default_factory=dict)
    calendar_blocks: dict[str, ProjectedCalendarBlock] = field(default_factory=dict)
    recurrence_series: dict[str, ProjectedRecurrenceSeries] = field(default_factory=dict)
    preferences: dict[str, ProjectedPreference] = field(default_factory=dict)
    scheduling_constraints: dict[str, ProjectedConstraint] = field(default_factory=dict)
    preference_links: dict[str, ProjectedPreferenceLink] = field(default_factory=dict)
    cancelled_occurrence_ids: set[str] = field(default_factory=set)
    overridden_occurrence_ids: set[str] = field(default_factory=set)

    def canonical_payload(self) -> dict[str, Any]:
        def ref(value: EntityRef | None) -> Any:
            return value.model_dump(mode="json", by_alias=True) if value is not None else None

        return {
            "tasks": [
                {
                    "id": key, "ref": ref(item.ref), "workspace": ref(item.workspace_ref),
                    "parent": ref(item.parent_ref), "title": item.title, "notes": item.notes,
                    "priority": item.priority, "priorityAuthoritative": item.priority_authoritative,
                    "status": item.status,
                    "estimateMinutes": item.estimate_minutes,
                    "deadlineAt": item.deadline_at.isoformat() if item.deadline_at else None,
                    "sourceOperationId": item.source_operation_id,
                }
                for key, item in sorted(self.tasks.items())
            ],
            "calendarBlocks": [
                {
                    "id": key, "ref": ref(item.ref), "task": ref(item.task_ref),
                    "workspace": ref(item.workspace_ref), "startAt": item.start_at.isoformat(),
                    "endAt": item.end_at.isoformat(), "priority": item.priority,
                    "priorityAuthoritative": item.priority_authoritative,
                    "deadlineAt": item.deadline_at.isoformat() if item.deadline_at else None,
                    "pinned": item.is_pinned, "protected": item.is_protected,
                    "recurring": item.is_recurring, "anchoring": item.anchoring,
                    "movability": item.movability, "series": ref(item.series_ref),
                    "occurrence": ref(item.occurrence_ref),
                    "gapMinutes": item.gap_minutes,
                    "sourceOperationId": item.source_operation_id,
                }
                for key, item in sorted(self.calendar_blocks.items())
            ],
            "recurrenceSeries": [
                {
                    "id": key, "ref": ref(item.ref), "task": ref(item.task_ref),
                    "workspace": ref(item.workspace_ref), "rule": item.rule,
                    "status": item.status, "sourceOperationId": item.source_operation_id,
                }
                for key, item in sorted(self.recurrence_series.items())
            ],
            "preferences": [
                {
                    "id": key, "ref": ref(item.ref), "key": item.key, "value": item.value,
                    "domain": item.domain, "strength": item.strength, "scope": item.scope,
                    "scopeId": item.scope_id,
                    "active": item.active,
                    "effectiveFrom": item.effective_from.isoformat() if item.effective_from else None,
                    "effectiveUntil": item.effective_until.isoformat() if item.effective_until else None,
                    "sourceOperationId": item.source_operation_id,
                }
                for key, item in sorted(self.preferences.items())
            ],
            "constraints": [
                {"id": key, **vars(item)}
                for key, item in sorted(self.scheduling_constraints.items())
            ],
            "preferenceLinks": [
                {
                    "id": key,
                    "ref": ref(item.ref),
                    "preferenceId": item.preference_id,
                    "targetType": item.target_type,
                    "targetId": item.target_id,
                    "relationType": item.relation_type,
                    "weight": item.weight,
                    "active": item.active,
                    "sourceOperationId": item.source_operation_id,
                }
                for key, item in sorted(self.preference_links.items())
            ],
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.canonical_payload())


@dataclass(frozen=True)
class LoadedSimulationWorld:
    snapshot: SimulationSnapshot
    world: SimulationWorld


@dataclass(frozen=True)
class ProjectionResult:
    world: SimulationWorld
    effects: list[ProjectedEffect]
    final_facts: list[ProjectedFinalFact]
    output_fingerprint: str


class SimulationSnapshotLoader:
    """Copies authoritative rows into detached typed values; it never mutates ORM objects."""

    def __init__(self, session: Session):
        self.session = session

    def load(self, change_set: ChangeSet) -> LoadedSimulationWorld:
        world = SimulationWorld()
        facts: list[SnapshotFact] = []
        operation_kinds = {operation.kind for operation in change_set.operations}
        requires_calendar_state = bool(
            operation_kinds
            & {
                "task.move",
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
            }
        )
        requires_preference_state = bool(
            requires_calendar_state
            or any(kind.startswith("preference.") for kind in operation_kinds)
        )
        with self.session.no_autoflush:
            all_categories = {
                item.id: item for item in self.session.exec(select(Category)).all()
            }
            all_tasks = self.session.exec(select(StrategyNode)).all()
        categories = all_categories
        tasks = all_tasks
        if not requires_calendar_state:
            referenced_workspace_ids: set[int] = set()
            referenced_task_ids: set[int] = set()

            def collect_references(value: Any) -> None:
                if isinstance(value, dict):
                    entity_kind = str(value.get("kind") or "").strip()
                    entity_id = str(value.get("entity_id") or "").strip()
                    if entity_kind == "workspace" and entity_id.startswith("workspace:"):
                        raw_id = entity_id.removeprefix("workspace:")
                        if raw_id.isdigit():
                            referenced_workspace_ids.add(int(raw_id))
                    elif entity_kind == "task" and entity_id.startswith("task:"):
                        raw_id = entity_id.removeprefix("task:")
                        if raw_id.isdigit():
                            referenced_task_ids.add(int(raw_id))
                    for nested in value.values():
                        collect_references(nested)
                elif isinstance(value, list):
                    for nested in value:
                        collect_references(nested)

            for operation in change_set.operations:
                collect_references(operation.model_dump(mode="python", by_alias=False))
            referenced_workspace_ids.update(
                int(item.category_id)
                for item in all_tasks
                if item.id in referenced_task_ids
            )
            if referenced_workspace_ids:
                tasks = [
                    item
                    for item in all_tasks
                    if int(item.category_id) in referenced_workspace_ids
                ]
                categories = {
                    category_id: category
                    for category_id, category in all_categories.items()
                    if category_id in referenced_workspace_ids
                }
        for item in tasks:
            if item.id is None:
                continue
            key = f"task:{item.id}"
            task_ref = EntityRef(kind="task", entity_id=key, revision=item.revision)
            category = categories.get(item.category_id)
            workspace_ref = EntityRef(kind="workspace", entity_id=f"workspace:{item.category_id}", revision=(category.revision if category else None))
            deadline = datetime.fromisoformat(item.deadline_at) if item.deadline_at else None
            tags = _string_set(item.tags)
            semantic_status = {
                "todo": "not_started",
                "in-progress": "in_progress",
                "done": "completed",
            }.get(item.status, item.status)
            if "state:archived" in tags:
                semantic_status = "archived"
            world.tasks[key] = ProjectedTask(
                ref=task_ref,
                workspace_ref=workspace_ref,
                parent_ref=(EntityRef(kind="task", entity_id=f"task:{item.parent_id}", revision=(next((parent.revision for parent in tasks if parent.id == item.parent_id), None))) if item.parent_id else None),
                title=item.title,
                notes=item.notes,
                priority=item.priority, priority_authoritative=True,
                status=semantic_status,
                estimate_minutes=item.time_estimate_minutes,
                deadline_at=_aware(deadline) if deadline else None,
            )
        series_records: dict[str, RecurrenceSeries] = {}
        exception_records: list[RecurrenceOccurrenceException] = []
        calendar_records: list[CalendarBlock] = []
        if requires_calendar_state:
            with self.session.no_autoflush:
                series_records = {
                    item.id: item
                    for item in self.session.exec(select(RecurrenceSeries)).all()
                }
                exception_records = self.session.exec(
                    select(RecurrenceOccurrenceException)
                ).all()
                calendar_records = self.session.exec(select(CalendarBlock)).all()
        world.cancelled_occurrence_ids = {
            f"recurrence_occurrence:{item.series_id}:{item.recurrence_key}"
            for item in exception_records
            if item.disposition == "cancelled"
        }
        world.overridden_occurrence_ids = {
            f"recurrence_occurrence:{item.series_id}:{item.recurrence_key}"
            for item in exception_records
            if item.disposition == "overridden"
        }
        for item in calendar_records:
            if item.id is None:
                continue
            key = f"calendar_block:{item.id}"
            occurrence_ref = None
            series_ref = None
            if item.recurrence_series_id:
                series_ref = EntityRef(kind="recurrence_series", entity_id=f"recurrence_series:{item.recurrence_series_id}", revision=(series_records[item.recurrence_series_id].revision if item.recurrence_series_id in series_records else None))
                if item.recurrence_key:
                    occurrence_ref = EntityRef(
                        kind="recurrence_occurrence",
                        entity_id=f"recurrence_occurrence:{item.recurrence_series_id}:{item.recurrence_key}",
                        revision=item.revision,
                    )
            task = world.tasks.get(f"task:{item.task_id}")
            series_record = series_records.get(item.recurrence_series_id or "")
            fixed = item.is_pinned or item.is_protected or (
                series_record is not None and series_record.strict
            )
            flexible = series_record is not None and not series_record.strict
            world.calendar_blocks[key] = ProjectedCalendarBlock(
                ref=EntityRef(kind="calendar_block", entity_id=key, revision=item.revision),
                task_ref=(task.ref if task else EntityRef(kind="task", entity_id=f"task:{item.task_id}")),
                workspace_ref=EntityRef(kind="workspace", entity_id=f"workspace:{item.category_id}", revision=(categories[item.category_id].revision if item.category_id in categories else None)),
                start_at=_aware_in_zone(
                    item.start_at, series_record.timezone if series_record else None
                ),
                end_at=_aware_in_zone(
                    item.end_at, series_record.timezone if series_record else None
                ),
                priority=task.priority if task else "medium",
                priority_authoritative=task.priority_authoritative if task else False,
                deadline_at=task.deadline_at if task else None,
                is_pinned=item.is_pinned, is_protected=item.is_protected,
                is_recurring=item.is_recurring, series_ref=series_ref, occurrence_ref=occurrence_ref,
                anchoring="anchored" if fixed else "unanchored" if flexible else "unknown",
                movability="fixed" if fixed else "flexible" if flexible else "unknown",
                gap_minutes=max(0, int(series_record.gap_minutes or 0)) if series_record else 0,
            )
        for item in series_records.values():
            key = f"recurrence_series:{item.id}"
            world.recurrence_series[key] = ProjectedRecurrenceSeries(
                ref=EntityRef(kind="recurrence_series", entity_id=key, revision=item.revision),
                task_ref=(world.tasks[f"task:{item.task_id}"].ref if f"task:{item.task_id}" in world.tasks else EntityRef(kind="task", entity_id=f"task:{item.task_id}")),
                workspace_ref=EntityRef(kind="workspace", entity_id=f"workspace:{item.category_id}", revision=(categories[item.category_id].revision if item.category_id in categories else None)),
                rule={
                    "frequency": item.frequency, "interval": item.interval,
                    "weekdays": json.loads(item.days_of_week_json or "[]"),
                    "monthDays": json.loads(item.month_days_json or "[]"),
                    "startDate": item.start_date.isoformat(),
                    "endDate": item.end_date.isoformat() if item.end_date else None,
                    "occurrenceCount": item.occurrence_limit, "timezone": item.timezone,
                    "preferredStartTime": item.preferred_start_time,
                    "allowedStartTime": item.allowed_start_time,
                    "allowedEndTime": item.allowed_end_time,
                    "durationMinutes": item.duration_minutes,
                    "materializedThrough": item.materialized_through.isoformat() if item.materialized_through else None,
                },
                status=item.status,
            )
        preference_records: list[UserPreference] = []
        constraint_records: list[SchedulingConstraint] = []
        link_records: list[PreferenceLink] = []
        if requires_preference_state:
            with self.session.no_autoflush:
                preference_records = self.session.exec(
                    select(UserPreference).where(UserPreference.user_id == "local")
                ).all()
                constraint_records = self.session.exec(
                    select(SchedulingConstraint).where(
                        SchedulingConstraint.user_id == "local"
                    )
                ).all()
                link_records = self.session.exec(
                    select(PreferenceLink).where(PreferenceLink.user_id == "local")
                ).all()
        for item in preference_records:
            key = f"preference:{item.id}"
            world.preferences[key] = ProjectedPreference(
                ref=EntityRef(kind="preference", entity_id=key, revision=item.revision),
                key=item.preference_type, value=json.loads(item.value_json or "null"),
                domain=item.domain, strength=item.strength, scope=item.scope_type,
                scope_id=item.scope_id,
                active=item.active,
                effective_from=_aware(item.effective_from) if item.effective_from else None,
                effective_until=_aware(item.effective_until) if item.effective_until else None,
            )
        for item in constraint_records:
            world.scheduling_constraints[item.id] = ProjectedConstraint(
                constraint_id=item.id, kind=item.constraint_type, hard=item.hard,
                value=json.loads(item.value_json or "null"), description=item.description,
                active=item.active, source=item.source,
                target_type=item.target_type, target_id=item.target_id,
                effective_from=_aware(item.effective_from) if item.effective_from else None,
                effective_until=_aware(item.effective_until) if item.effective_until else None,
                source_preference_id=item.source_preference_id,
            )
        for item in link_records:
            key = f"preference_link:{item.id}"
            world.preference_links[key] = ProjectedPreferenceLink(
                ref=EntityRef(
                    kind="preference_link",
                    entity_id=key,
                    revision=item.revision,
                ),
                preference_id=item.preference_id,
                target_type=item.target_type,
                target_id=item.target_id,
                relation_type=item.relation_type,
                weight=item.weight,
                active=item.active,
            )
        payload = world.canonical_payload()
        for key, item in sorted(world.tasks.items()):
            facts.append(SnapshotFact(fact_id=f"fact:{key}", domain="task", entity_ref=item.ref, fingerprint=_fingerprint({"key": key, "value": vars(item)})))
        for category_id in sorted(key for key in categories if key is not None):
            category = categories[category_id]
            facts.append(SnapshotFact(
                fact_id=f"fact:workspace:{category_id}",
                domain="workspace",
                entity_ref=EntityRef(kind="workspace", entity_id=f"workspace:{category_id}", revision=category.revision),
                fingerprint=_fingerprint({"id": category_id, "name": category.name, "revision": category.revision}),
            ))
        for key, item in sorted(world.calendar_blocks.items()):
            facts.append(SnapshotFact(fact_id=f"fact:{key}", domain="calendar", entity_ref=item.ref, fingerprint=_fingerprint({"key": key, "value": vars(item)})))
        for key, item in sorted(world.recurrence_series.items()):
            facts.append(SnapshotFact(fact_id=f"fact:{key}", domain="recurrence", entity_ref=item.ref, fingerprint=_fingerprint({"key": key, "value": vars(item)})))
        for key, item in sorted(world.preferences.items()):
            facts.append(SnapshotFact(fact_id=f"fact:{key}", domain="preference", entity_ref=item.ref, fingerprint=_fingerprint({"key": key, "value": vars(item)})))
        for key, item in sorted(world.scheduling_constraints.items()):
            record = next(
                (candidate for candidate in constraint_records if candidate.id == key),
                None,
            )
            facts.append(SnapshotFact(
                fact_id=f"fact:constraint:{key}",
                domain="constraint",
                entity_ref=(
                    EntityRef(
                        kind="scheduling_constraint",
                        entity_id=f"scheduling_constraint:{key}",
                        revision=record.revision,
                    )
                    if record is not None
                    else None
                ),
                fingerprint=_fingerprint({"key": key, "value": vars(item)}),
            ))
        for item in sorted(link_records, key=lambda candidate: candidate.id):
            facts.append(
                SnapshotFact(
                    fact_id=f"fact:preference_link:{item.id}",
                    domain="preference",
                    entity_ref=EntityRef(
                        kind="preference_link",
                        entity_id=f"preference_link:{item.id}",
                        revision=item.revision,
                    ),
                    fingerprint=_fingerprint(
                        {
                            "preferenceId": item.preference_id,
                            "targetType": item.target_type,
                            "targetId": item.target_id,
                            "active": item.active,
                            "revision": item.revision,
                        }
                    ),
                )
            )
        now = datetime.now(timezone.utc)
        fingerprint = _fingerprint(payload)
        facts = _bounded_snapshot_facts(
            facts,
            required_entities=_change_set_entity_identities(change_set),
        )
        return LoadedSimulationWorld(
            snapshot=SimulationSnapshot(
                snapshot_id=f"snapshot:{fingerprint.split(':', 1)[1][:24]}",
                fingerprint=fingerprint,
                captured_at=now,
                freshness_boundary=now,
                facts=facts,
            ),
            world=world,
        )


class ChangeSetProjector:
    def project(self, change_set: ChangeSet, world: SimulationWorld) -> ProjectionResult:
        world = deepcopy(world)
        effects: list[ProjectedEffect] = []
        operations = {item.operation_id: item for item in change_set.operations}
        for operation_id in change_set.dependency_order():
            operation = operations[operation_id]
            refs = self._apply(operation, world)
            effects.append(ProjectedEffect(
                operation_id=operation_id,
                kind=self._effect_kind(operation.kind),
                entity_refs=refs,
                fulfills_clause_ids=operation.fulfills_clause_ids,
            ))
        self._expand_recurrences(world)
        self._validate_final_world(world)
        return ProjectionResult(
            world=world,
            effects=effects,
            final_facts=self._final_facts(world, change_set),
            output_fingerprint=world.fingerprint,
        )

    @staticmethod
    def _key(ref: EntityRef) -> str:
        return ref.entity_id

    @staticmethod
    def _same_entity(left: EntityRef, right: EntityRef) -> bool:
        return left.kind == right.kind and left.entity_id == right.entity_id

    @staticmethod
    def _created_ref(operation: Any, target_kind: str) -> EntityRef:
        if getattr(operation, "temp_ref", None) is not None:
            return operation.temp_ref
        return EntityRef(kind="temporary", entity_id=f"projected:{target_kind}:{operation.operation_id}", target_kind=target_kind)

    @staticmethod
    def _require(items: dict[str, Any], ref: EntityRef, operation_id: str) -> Any:
        item = items.get(ref.entity_id)
        if item is None:
            raise SimulationProjectionError("REFERENCE_NOT_FOUND", f"{ref.entity_id} is absent from projected state", operation_id=operation_id)
        return item

    @staticmethod
    def _project_enforcement_constraint(
        operation: Any,
        world: SimulationWorld,
        *,
        preference_ref: EntityRef,
        preference_key: str,
        logical_value: dict[str, Any],
        scope_id: str | None = None,
    ) -> EntityRef | None:
        if operation.semantics.strength != "hard":
            return None
        constraint_type = enforcement_constraint_type(
            preference_key,
            logical_value,
        )
        if constraint_type is None:
            return None
        ref = EntityRef(
            kind="temporary",
            entity_id=f"projected:scheduling_constraint:{operation.operation_id}",
            target_kind="scheduling_constraint",
        )
        provenance = getattr(operation, "provenance", None)
        source_text = str(getattr(provenance, "source_text", None) or "").strip()
        world.scheduling_constraints[ref.entity_id] = ProjectedConstraint(
            constraint_id=ref.entity_id,
            kind=constraint_type,
            hard=True,
            value=logical_value,
            description=source_text[:512] or None,
            active=True,
            source="user_explicit",
            target_type=operation.semantics.scope,
            target_id=scope_id,
            effective_from=operation.semantics.effective_from,
            effective_until=operation.semantics.effective_until,
            source_preference_id=preference_ref.entity_id,
        )
        return ref

    def _apply(self, operation: Any, world: SimulationWorld) -> list[EntityRef]:
        kind = operation.kind
        op_id = operation.operation_id
        if kind == "task.create":
            ref = self._created_ref(operation, "task")
            if ref.entity_id in world.tasks:
                raise SimulationProjectionError("DUPLICATE_ENTITY", "projected task identity already exists", operation_id=op_id)
            world.tasks[ref.entity_id] = ProjectedTask(
                ref=ref, workspace_ref=operation.workspace_ref, parent_ref=operation.parent_ref,
                title=operation.title, notes=operation.fields.notes,
                priority=operation.fields.priority or "medium",
                priority_authoritative=(
                    "priority" in operation.fields.model_fields_set
                    and operation.provenance is not None
                    and operation.provenance.source_kind in {"user", "ui"}
                ),
                status=operation.fields.status or "not_started",
                estimate_minutes=operation.fields.estimate_minutes,
                deadline_at=operation.fields.deadline_at, source_operation_id=op_id,
            )
            return [ref]
        if kind == "task.update":
            item = self._require(world.tasks, operation.task_ref, op_id)
            payload = operation.updates.model_dump(
                mode="python",
                exclude_unset=True,
                exclude_none=True,
            )
            clear_fields = payload.pop("clear_fields", [])
            for name, value in payload.items():
                setattr(item, name, value)
            if "priority" in payload:
                item.priority_authoritative = operation.provenance is not None and operation.provenance.source_kind in {"user", "ui"}
            for name in clear_fields:
                setattr(item, name, None)
            item.source_operation_id = op_id
            return [item.ref]
        if kind == "task.move":
            refs = []
            for task_ref in operation.task_refs:
                item = self._require(world.tasks, task_ref, op_id)
                if operation.destination_parent_ref is not None:
                    self._require(world.tasks, operation.destination_parent_ref, op_id)
                    item.parent_ref = operation.destination_parent_ref
                if operation.destination_workspace_ref is not None:
                    old_workspace = item.workspace_ref
                    item.workspace_ref = operation.destination_workspace_ref
                    if operation.destination_parent_ref is None:
                        item.parent_ref = None
                    descendants = self._descendants(world, item.ref.entity_id)
                    for descendant in descendants:
                        if descendant.workspace_ref.entity_id == old_workspace.entity_id:
                            descendant.workspace_ref = operation.destination_workspace_ref
                    moved_ids = {item.ref.entity_id, *(child.ref.entity_id for child in descendants)}
                    for block in world.calendar_blocks.values():
                        if block.task_ref.entity_id in moved_ids:
                            block.workspace_ref = operation.destination_workspace_ref
                    for series in world.recurrence_series.values():
                        if series.task_ref.entity_id in moved_ids:
                            series.workspace_ref = operation.destination_workspace_ref
                item.source_operation_id = op_id
                refs.append(item.ref)
            return refs
        if kind in {"task.archive", "task.delete"}:
            refs = []
            for task_ref in operation.task_refs:
                item = self._require(world.tasks, task_ref, op_id)
                refs.append(item.ref)
                descendants = self._descendants(world, item.ref.entity_id)
                affected_tasks = [item, *descendants]
                if kind == "task.archive":
                    for affected in affected_tasks:
                        affected.status = "archived"
                        affected.source_operation_id = op_id
                else:
                    affected_ids = {affected.ref.entity_id for affected in affected_tasks}
                    for affected_id in affected_ids:
                        world.tasks.pop(affected_id, None)
                    for key, block in list(world.calendar_blocks.items()):
                        if block.task_ref.entity_id in affected_ids:
                            world.calendar_blocks.pop(key)
                    for key, series in list(world.recurrence_series.items()):
                        if series.task_ref.entity_id in affected_ids:
                            world.recurrence_series.pop(key)
                    numeric_ids = {
                        value.removeprefix("task:") for value in affected_ids
                    }
                    for preference in world.preferences.values():
                        if (
                            preference.scope == "task"
                            and preference.scope_id in numeric_ids
                            and preference.active
                        ):
                            preference.active = False
                            preference.source_operation_id = op_id
                    for constraint in world.scheduling_constraints.values():
                        if (
                            constraint.target_type == "task"
                            and constraint.target_id in numeric_ids
                            and constraint.active
                        ):
                            constraint.active = False
                    for link in world.preference_links.values():
                        if (
                            link.target_type == "task"
                            and link.target_id in numeric_ids
                            and link.active
                        ):
                            link.active = False
                            link.source_operation_id = op_id
            return refs
        if kind == "calendar.create_block":
            self._require(world.tasks, operation.task_ref, op_id)
            ref = self._created_ref(operation, "calendar_block")
            task = world.tasks[operation.task_ref.entity_id]
            world.calendar_blocks[ref.entity_id] = ProjectedCalendarBlock(
                ref=ref, task_ref=operation.task_ref, workspace_ref=task.workspace_ref,
                start_at=_aware(operation.start_at), end_at=_aware(operation.end_at),
                priority=task.priority, priority_authoritative=task.priority_authoritative,
                deadline_at=task.deadline_at,
                source_operation_id=op_id,
                anchoring="unknown", movability="unknown",
            )
            return [ref]
        if kind in {"calendar.move_block", "calendar.resize_block"}:
            item = self._require(world.calendar_blocks, operation.block_ref, op_id)
            if kind == "calendar.move_block":
                item.start_at, item.end_at = _aware(operation.start_at), _aware(operation.end_at)
            else:
                item.start_at = _aware(operation.start_at) if operation.start_at else item.start_at
                item.end_at = _aware(operation.end_at)
            item.source_operation_id = op_id
            return [item.ref]
        if kind == "calendar.split_block":
            original = self._require(world.calendar_blocks, operation.block_ref, op_id)
            if original.is_recurring or original.occurrence_ref is not None:
                raise SimulationProjectionError(
                    "RECURRENCE_SCOPE_VIOLATION",
                    "recurring occurrences must be revised with recurrence.update_occurrence",
                    operation_id=op_id,
                )
            world.calendar_blocks.pop(operation.block_ref.entity_id)
            refs = []
            for index, segment in enumerate(operation.segments):
                ref = EntityRef(kind="temporary", entity_id=f"projected:block:{op_id}:{index}", target_kind="calendar_block")
                world.calendar_blocks[ref.entity_id] = ProjectedCalendarBlock(
                    ref=ref, task_ref=original.task_ref, workspace_ref=original.workspace_ref,
                    start_at=_aware(segment.start_at), end_at=_aware(segment.end_at),
                    priority=original.priority, priority_authoritative=original.priority_authoritative,
                    deadline_at=original.deadline_at,
                    is_pinned=original.is_pinned,
                    is_protected=original.is_protected, anchoring=original.anchoring,
                    movability=original.movability, source_operation_id=op_id,
                )
                refs.append(ref)
            return refs
        if kind == "calendar.delete_block":
            refs = []
            for block_ref in operation.block_refs:
                item = self._require(world.calendar_blocks, block_ref, op_id)
                refs.append(item.ref)
                world.calendar_blocks.pop(block_ref.entity_id)
            return refs
        if kind == "recurrence.create_series":
            task = self._require(world.tasks, operation.task_ref, op_id)
            ref = self._created_ref(operation, "recurrence_series")
            rule = operation.rule.model_dump(mode="json", by_alias=True)
            rule["preferredStartTime"] = rule.get("preferredStartTime") or "09:00"
            rule["durationMinutes"] = (
                rule.get("durationMinutes") or task.estimate_minutes or 30
            )
            world.recurrence_series[ref.entity_id] = ProjectedRecurrenceSeries(
                ref=ref, task_ref=operation.task_ref, workspace_ref=task.workspace_ref,
                rule=rule, source_operation_id=op_id,
            )
            return [ref]
        if kind == "recurrence.update_series":
            item = self._require(world.recurrence_series, operation.series_ref, op_id)
            item.rule.update(
                operation.updates.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_unset=True,
                    exclude_none=True,
                )
            )
            item.rule["materializedThrough"] = None
            item.source_operation_id = op_id
            return [item.ref]
        if kind in {"recurrence.update_occurrence", "recurrence.cancel_occurrence"}:
            matches = [item for item in world.calendar_blocks.values() if item.occurrence_ref and item.occurrence_ref.entity_id == operation.occurrence_ref.entity_id]
            if len(matches) != 1:
                raise SimulationProjectionError("OCCURRENCE_NOT_FOUND", "recurrence occurrence must resolve exactly once", operation_id=op_id)
            item = matches[0]
            if kind == "recurrence.cancel_occurrence":
                world.cancelled_occurrence_ids.add(operation.occurrence_ref.entity_id)
                world.calendar_blocks.pop(item.ref.entity_id)
            else:
                updates = operation.updates
                if updates.start_at is not None:
                    item.start_at, item.end_at = _aware(updates.start_at), _aware(updates.end_at)
                elif updates.duration_minutes is not None:
                    from datetime import timedelta
                    item.end_at = item.start_at + timedelta(minutes=updates.duration_minutes)
                item.source_operation_id = op_id
            return [operation.occurrence_ref]
        if kind == "preference.upsert":
            ref = EntityRef(kind="temporary", entity_id=f"projected:preference:{op_id}", target_kind="preference")
            semantics = operation.semantics
            logical_value = decode_preference_value(
                operation.value.model_dump(mode="json", by_alias=True)
            )
            world.preferences[ref.entity_id] = ProjectedPreference(
                ref=ref, key=operation.preference_key,
                value=logical_value,
                domain=semantics.domain, strength=semantics.strength, scope=semantics.scope,
                effective_from=semantics.effective_from, effective_until=semantics.effective_until,
                source_operation_id=op_id,
            )
            constraint_ref = self._project_enforcement_constraint(
                operation,
                world,
                preference_ref=ref,
                preference_key=operation.preference_key,
                logical_value=logical_value,
            )
            return [ref, constraint_ref] if constraint_ref is not None else [ref]
        if kind == "preference.supersede":
            prior = self._require(world.preferences, operation.preference_ref, op_id)
            prior.active = False
            preference_id = operation.preference_ref.entity_id.removeprefix(
                "preference:"
            )
            for constraint in world.scheduling_constraints.values():
                if (
                    constraint.source_preference_id == preference_id
                    and constraint.active
                ):
                    constraint.active = False
                    constraint.source_operation_id = op_id
            semantics = operation.semantics
            ref = EntityRef(kind="temporary", entity_id=f"projected:preference:{op_id}", target_kind="preference")
            logical_value = decode_preference_value(
                operation.value.model_dump(mode="json", by_alias=True)
            )
            world.preferences[ref.entity_id] = ProjectedPreference(
                ref=ref, key=prior.key, value=logical_value,
                domain=semantics.domain, strength=semantics.strength, scope=semantics.scope,
                scope_id=prior.scope_id,
                effective_from=semantics.effective_from, effective_until=semantics.effective_until,
                source_operation_id=op_id,
            )
            constraint_ref = self._project_enforcement_constraint(
                operation,
                world,
                preference_ref=ref,
                preference_key=prior.key,
                logical_value=logical_value,
                scope_id=prior.scope_id,
            )
            linked = sorted([
                item
                for item in world.preference_links.values()
                if item.preference_id == operation.preference_ref.entity_id.removeprefix("preference:")
                and item.active
            ], key=lambda item: item.ref.entity_id)
            for index, link in enumerate(linked):
                link.active = False
                link.source_operation_id = op_id
                replacement_ref = EntityRef(
                    kind="temporary",
                    entity_id=f"projected:preference_link:{op_id}:{index}",
                    target_kind="preference_link",
                )
                world.preference_links[replacement_ref.entity_id] = ProjectedPreferenceLink(
                    ref=replacement_ref,
                    preference_id=ref.entity_id,
                    target_type=link.target_type,
                    target_id=link.target_id,
                    relation_type=link.relation_type,
                    weight=link.weight,
                    source_operation_id=op_id,
                )
            return (
                [prior.ref, ref, constraint_ref]
                if constraint_ref is not None
                else [prior.ref, ref]
            )
        if kind == "preference.retire":
            item = self._require(world.preferences, operation.preference_ref, op_id)
            item.active = False
            item.source_operation_id = op_id
            preference_id = operation.preference_ref.entity_id.removeprefix("preference:")
            for constraint in world.scheduling_constraints.values():
                if constraint.source_preference_id == preference_id and constraint.active:
                    constraint.active = False
            for link in world.preference_links.values():
                if link.preference_id == preference_id and link.active:
                    link.active = False
                    link.source_operation_id = op_id
            return [item.ref]
        if kind == "preference.restore":
            item = self._require(world.preferences, operation.preference_ref, op_id)
            item.active = True
            item.source_operation_id = op_id
            preference_id = operation.preference_ref.entity_id.removeprefix("preference:")
            for constraint in world.scheduling_constraints.values():
                if constraint.source_preference_id == preference_id and not constraint.active:
                    constraint.active = True
            for link in world.preference_links.values():
                if link.preference_id == preference_id and not link.active:
                    link.active = True
                    link.source_operation_id = op_id
            return [item.ref]
        if kind == "preference.delete":
            item = self._require(world.preferences, operation.preference_ref, op_id)
            preference_id = operation.preference_ref.entity_id.removeprefix("preference:")
            del world.preferences[item.ref.entity_id]
            for key in [
                key
                for key, constraint in world.scheduling_constraints.items()
                if constraint.source_preference_id == preference_id
            ]:
                del world.scheduling_constraints[key]
            for key in [
                key
                for key, link in world.preference_links.items()
                if link.preference_id == preference_id
            ]:
                del world.preference_links[key]
            return [item.ref]
        raise SimulationProjectionError("UNSUPPORTED_OPERATION", f"unsupported operation {kind}", operation_id=op_id)

    @staticmethod
    def _descendants(world: SimulationWorld, root_id: str) -> list[ProjectedTask]:
        found: list[ProjectedTask] = []
        frontier = [root_id]
        while frontier:
            parent_id = frontier.pop()
            children = [
                task for task in world.tasks.values()
                if task.parent_ref is not None and task.parent_ref.entity_id == parent_id
            ]
            found.extend(children)
            frontier.extend(child.ref.entity_id for child in children)
        return found

    @staticmethod
    def _series_dates(rule: dict[str, Any], start: date, end: date) -> list[date]:
        frequency = rule.get("frequency")
        interval = int(rule.get("interval") or 1)
        start_date = date.fromisoformat(str(rule.get("startDate") or rule.get("start_date")))
        end_value = rule.get("endDate") or rule.get("end_date")
        rule_end = date.fromisoformat(str(end_value)) if end_value else end
        limit = min(int(rule.get("occurrenceCount") or rule.get("occurrence_count") or 128), 128)
        weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        wanted_weekdays = {weekdays[item] for item in (rule.get("weekdays") or [])}
        wanted_month_days = set(rule.get("monthDays") or rule.get("month_days") or [])
        result: list[date] = []
        cursor = max(start, start_date)
        last = min(end, rule_end)
        while cursor <= last:
            days = (cursor - start_date).days
            include = (
                (frequency == "daily" and days % interval == 0)
                or (frequency == "weekly" and (days // 7) % interval == 0 and cursor.weekday() in wanted_weekdays)
                or (frequency == "monthly" and ((cursor.year - start_date.year) * 12 + cursor.month - start_date.month) % interval == 0 and cursor.day in wanted_month_days)
            )
            if include:
                result.append(cursor)
                if len(result) >= limit:
                    break
            cursor += timedelta(days=1)
        return result

    def _expand_recurrences(self, world: SimulationWorld) -> None:
        for series in sorted(world.recurrence_series.values(), key=lambda item: item.ref.entity_id):
            # Rolling-horizon materialization is a separate authoritative writer.
            # A ChangeSet must project only recurrence series it actually creates
            # or updates; otherwise an occurrence-only edit silently claims
            # unrelated future block creation that the native executor will not do.
            if series.status != "active" or series.source_operation_id is None:
                continue
            rule = series.rule
            start_text = rule.get("preferredStartTime") or rule.get("preferred_start_time")
            duration = rule.get("durationMinutes") or rule.get("duration_minutes")
            if not start_text or not duration:
                continue
            recurrence_rule = RecurrenceRule.model_validate(
                {
                    "frequency": rule.get("frequency"),
                    "interval": rule.get("interval", 1),
                    "weekdays": rule.get("weekdays", []),
                    "month_days": rule.get("monthDays", rule.get("month_days", [])),
                    "start_date": rule.get("startDate", rule.get("start_date")),
                    "end_date": rule.get("endDate", rule.get("end_date")),
                    "occurrence_count": rule.get(
                        "occurrenceCount", rule.get("occurrence_count")
                    ),
                    "timezone": rule.get("timezone"),
                    "preferred_start_time": start_text,
                    "duration_minutes": duration,
                }
            )
            try:
                zone = ZoneInfo(recurrence_rule.timezone or "UTC")
            except ZoneInfoNotFoundError as error:
                raise SimulationProjectionError("INVALID_TIMEZONE", "recurrence timezone is not recognized", operation_id=series.source_operation_id) from error
            hour, minute = (int(part) for part in str(start_text).split(":"))
            series_key = series.ref.entity_id.split(":", 1)[-1]
            existing_occurrence_count = sum(
                1
                for block in world.calendar_blocks.values()
                if block.series_ref is not None
                and block.series_ref.entity_id == series.ref.entity_id
            )
            cancelled_occurrence_count = sum(
                1
                for occurrence_id in world.cancelled_occurrence_ids
                if occurrence_id.startswith(
                    f"recurrence_occurrence:{series_key}:"
                )
            )
            materialization_limit = (
                128
                if series.ref.kind == "temporary"
                else max(
                    DEFAULT_RECURRENCE_MATERIALIZATION_OCCURRENCES,
                    existing_occurrence_count + cancelled_occurrence_count,
                )
            )
            dates = recurrence_occurrence_dates(
                recurrence_rule,
                maximum_occurrences=materialization_limit,
            )
            desired_ids = {
                f"recurrence_occurrence:{series_key}:{item.isoformat()}"
                for item in dates
            }
            if series.source_operation_id is not None:
                for block_key, block in list(world.calendar_blocks.items()):
                    if (
                        block.series_ref is None
                        or block.series_ref.entity_id != series.ref.entity_id
                        or block.occurrence_ref is None
                    ):
                        continue
                    occurrence_id = block.occurrence_ref.entity_id
                    if occurrence_id in world.cancelled_occurrence_ids:
                        world.calendar_blocks.pop(block_key)
                        continue
                    if occurrence_id in world.overridden_occurrence_ids:
                        continue
                    if occurrence_id not in desired_ids:
                        world.calendar_blocks.pop(block_key)
                        continue
                    occurrence_date = date.fromisoformat(occurrence_id.rsplit(":", 1)[1])
                    block.start_at = datetime.combine(
                        occurrence_date, time(hour, minute), tzinfo=zone
                    )
                    block.end_at = block.start_at + timedelta(minutes=int(duration))
                    block.source_operation_id = series.source_operation_id
            for occurrence_date in dates:
                occurrence_id = f"recurrence_occurrence:{series_key}:{occurrence_date.isoformat()}"
                if occurrence_id in world.cancelled_occurrence_ids or any(
                    block.occurrence_ref is not None and block.occurrence_ref.entity_id == occurrence_id
                    for block in world.calendar_blocks.values()
                ):
                    continue
                occurrence_ref = EntityRef(kind="recurrence_occurrence", entity_id=occurrence_id)
                start_at = datetime.combine(occurrence_date, time(hour, minute), tzinfo=zone)
                end_at = start_at + timedelta(minutes=int(duration))
                block_ref = EntityRef(kind="temporary", entity_id=f"projected:occurrence:{series_key}:{occurrence_date.isoformat()}", target_kind="calendar_block")
                task = world.tasks[series.task_ref.entity_id]
                world.calendar_blocks[block_ref.entity_id] = ProjectedCalendarBlock(
                    ref=block_ref,
                    task_ref=series.task_ref,
                    workspace_ref=series.workspace_ref,
                    start_at=start_at,
                    end_at=end_at,
                    priority=task.priority, priority_authoritative=task.priority_authoritative,
                    deadline_at=task.deadline_at,
                    is_recurring=True,
                    series_ref=series.ref,
                    occurrence_ref=occurrence_ref,
                    anchoring="unknown",
                    movability="unknown",
                    source_operation_id=series.source_operation_id,
                )
            if series.source_operation_id is not None:
                series.rule["materializedThrough"] = (
                    dates[-1].isoformat() if dates else None
                )

    @staticmethod
    def _final_facts(
        world: SimulationWorld,
        change_set: ChangeSet,
    ) -> list[ProjectedFinalFact]:
        facts: list[ProjectedFinalFact] = []
        collections = (
            ("task", world.tasks),
            ("calendar", world.calendar_blocks),
            ("recurrence", world.recurrence_series),
            ("preference", world.preferences),
            ("preference", world.preference_links),
        )
        for domain, items in collections:
            for key, item in sorted(items.items()):
                source = getattr(item, "source_operation_id", None)
                facts.append(ProjectedFinalFact(
                    domain=domain,
                    entity_ref=getattr(item, "ref", None),
                    fingerprint=_fingerprint({"key": key, "value": vars(item)}),
                    source_operation_ids=[source] if source else [],
                ))
        for key, item in sorted(world.scheduling_constraints.items()):
            facts.append(ProjectedFinalFact(
                domain="constraint",
                fingerprint=_fingerprint({"key": key, "value": vars(item)}),
            ))
        return _bounded_projected_facts(
            facts,
            required_entities=_change_set_entity_identities(change_set),
        )

    @staticmethod
    def _effect_kind(kind: str) -> str:
        return {
            "task.create": "task.created", "task.update": "task.updated", "task.move": "task.moved",
            "task.archive": "task.archived", "task.delete": "task.deleted",
            "calendar.create_block": "calendar.created", "calendar.move_block": "calendar.moved",
            "calendar.resize_block": "calendar.resized", "calendar.split_block": "calendar.split",
            "calendar.delete_block": "calendar.deleted", "recurrence.create_series": "recurrence.created",
            "recurrence.update_series": "recurrence.updated", "recurrence.update_occurrence": "recurrence.occurrence_updated",
            "recurrence.cancel_occurrence": "recurrence.occurrence_cancelled",
            "preference.upsert": "preference.upserted", "preference.supersede": "preference.superseded",
            "preference.retire": "preference.retired",
            "preference.restore": "preference.restored",
            "preference.delete": "preference.deleted",
        }[kind]

    @staticmethod
    def _validate_final_world(world: SimulationWorld) -> None:
        for task in world.tasks.values():
            seen = {task.ref.entity_id}
            parent = task.parent_ref
            while parent is not None:
                if parent.entity_id in seen:
                    raise SimulationProjectionError("TASK_CYCLE", f"task {task.ref.entity_id} participates in a parent cycle")
                seen.add(parent.entity_id)
                parent_task = world.tasks.get(parent.entity_id)
                parent = parent_task.parent_ref if parent_task is not None else None
        for task in world.tasks.values():
            if task.parent_ref is not None and task.parent_ref.entity_id not in world.tasks:
                raise SimulationProjectionError("ORPHANED_TASK", f"task {task.ref.entity_id} has an absent parent")
            if (
                task.parent_ref is not None
                and not ChangeSetProjector._same_entity(
                    world.tasks[task.parent_ref.entity_id].workspace_ref,
                    task.workspace_ref,
                )
            ):
                raise SimulationProjectionError("WORKSPACE_MISMATCH", f"task {task.ref.entity_id} differs from its parent workspace")
        for block in world.calendar_blocks.values():
            if block.task_ref.entity_id not in world.tasks:
                raise SimulationProjectionError("ORPHANED_CALENDAR_BLOCK", f"calendar block {block.ref.entity_id} has an absent task")
            if block.end_at <= block.start_at:
                raise SimulationProjectionError("INVALID_INTERVAL", f"calendar block {block.ref.entity_id} has an invalid interval")
            if not ChangeSetProjector._same_entity(
                block.workspace_ref,
                world.tasks[block.task_ref.entity_id].workspace_ref,
            ):
                raise SimulationProjectionError("WORKSPACE_MISMATCH", f"calendar block {block.ref.entity_id} differs from its task workspace")
        for series in world.recurrence_series.values():
            if series.task_ref.entity_id not in world.tasks:
                raise SimulationProjectionError("ORPHANED_RECURRENCE_SERIES", f"recurrence series {series.ref.entity_id} has an absent task")
            if not ChangeSetProjector._same_entity(
                series.workspace_ref,
                world.tasks[series.task_ref.entity_id].workspace_ref,
            ):
                raise SimulationProjectionError("WORKSPACE_MISMATCH", f"recurrence series {series.ref.entity_id} differs from its task workspace")
