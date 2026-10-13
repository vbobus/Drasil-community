"""Flush-only native execution for the closed Story 8.4 operation union."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.agent_change_set import AgentChangeSetRecord
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import (
    PreferenceChangeLog,
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)
from app.services.preference_values import (
    decode_preference_value,
    encode_preference_value,
    enforcement_constraint_type,
)
from app.models.recurrence_series import (
    RecurrenceOccurrenceException,
    RecurrenceSeries,
)
from app.models.revisioning import EXPLICIT_REVISION_CONTROL
from app.models.strategist_session_entities import StrategistSessionEntity
from app.models.strategist_turn_mentions import StrategistTurnMention
from app.services.deadline_values import serialize_deadline_date
from app.services.agent_kernel.contracts import (
    ChangeSet,
    EntityBinding,
    EntityRef,
    ExecutionFact,
    ExpectedField,
    RecurrenceRule,
)
from app.services.agent_kernel.recurrence_materialization import (
    DEFAULT_RECURRENCE_MATERIALIZATION_OCCURRENCES,
    recurrence_occurrence_dates,
)
from app.services.calendar_assignment import moved_planned_day
from app.services.task_lifecycle import allocate_strategy_node_id


class DomainMutationError(RuntimeError):
    pass


class StaleEntityRevision(DomainMutationError):
    pass


@dataclass
class ExternalEffect:
    operation: str
    entity_id: str
    payload: dict[str, Any]


@dataclass
class DomainMutationResult:
    bindings: list[EntityBinding] = field(default_factory=list)
    facts: list[ExecutionFact] = field(default_factory=list)
    external_effects: list[ExternalEffect] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        payload = [item.model_dump(mode="json", by_alias=True) for item in self.facts]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _numeric_id(value: str, *prefixes: str) -> int:
    normalized = value
    for prefix in prefixes:
        marker = f"{prefix}:"
        if normalized.startswith(marker):
            normalized = normalized[len(marker):]
            break
    try:
        return int(normalized)
    except ValueError as error:
        raise DomainMutationError(f"invalid persisted numeric reference: {value}") from error


def _status(value: str | None) -> str | None:
    return {
        "not_started": "todo",
        "in_progress": "in-progress",
        "completed": "done",
    }.get(value, value)


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


class NativeDomainMutationExecutor:
    """Apply operations under a transaction owned by the caller; never commit."""

    def __init__(
        self,
        session: Session,
        *,
        fail_after_operation: str | None = None,
        approved_revisions: dict[tuple[str, str], int] | None = None,
    ):
        self.session = session
        self.fail_after_operation = fail_after_operation
        self.temp_refs: dict[str, EntityRef] = {}
        self.expected_revisions: dict[tuple[str, str], int] = dict(
            approved_revisions or {}
        )
        self.claimed_revisions: dict[tuple[type[Any], Any], int] = {}
        self.result = DomainMutationResult()

    def apply(self, change_set: ChangeSet) -> DomainMutationResult:
        prior_control = self.session.info.get(EXPLICIT_REVISION_CONTROL)
        self.session.info[EXPLICIT_REVISION_CONTROL] = True
        try:
            if not change_set.atomic_by_default:
                raise DomainMutationError("partial or non-atomic native execution is unsupported")
            self.expected_revisions.update(
                {
                    (ref.kind, ref.entity_id): int(ref.revision)
                    for ref in change_set.preconditions.expected_entity_revisions
                    if ref.revision is not None
                }
            )
            by_id = {operation.operation_id: operation for operation in change_set.operations}
            for operation_id in change_set.dependency_order():
                operation = by_id[operation_id]
                handler = getattr(
                    self,
                    f"_apply_{operation.kind.replace('.', '_')}",
                    None,
                )
                if handler is None:
                    raise DomainMutationError(
                        f"unsupported operation kind: {operation.kind}"
                    )
                handler(operation, change_set)
                self.session.flush()
                if self.fail_after_operation == operation.operation_id:
                    raise DomainMutationError(
                        f"injected failure after {operation.operation_id}"
                    )
            return self.result
        finally:
            if prior_control is None:
                self.session.info.pop(EXPLICIT_REVISION_CONTROL, None)
            else:
                self.session.info[EXPLICIT_REVISION_CONTROL] = prior_control

    def _resolve(self, ref: EntityRef) -> EntityRef:
        if ref.kind != "temporary":
            return ref
        resolved = self.temp_refs.get(ref.entity_id)
        if resolved is None or resolved.kind != ref.target_kind:
            raise DomainMutationError(f"temporary reference is not bound: {ref.entity_id}")
        return resolved

    def _expected(self, ref: EntityRef) -> int | None:
        resolved = self._resolve(ref)
        return self.expected_revisions.get(
            (resolved.kind, resolved.entity_id)
        ) or ref.revision or resolved.revision

    def _assert_current(self, ref: EntityRef, row: Any) -> None:
        resolved = self._resolve(ref)
        expected = self.claimed_revisions.get(
            (type(row), row.id), self._expected(ref)
        )
        if expected is None:
            raise StaleEntityRevision(
                f"authoritative dependency requires an approved revision: {resolved.entity_id}"
            )
        if int(getattr(row, "revision", 0)) != expected:
            raise StaleEntityRevision(f"stale entity revision: {resolved.entity_id}")

    def _claim(self, model: type[Any], pk: Any, ref: EntityRef) -> Any:
        row = self.session.get(model, pk)
        if row is None:
            raise DomainMutationError(f"entity does not exist: {ref.entity_id}")
        claim_key = (model, pk)
        expected = self.claimed_revisions.get(claim_key, self._expected(ref))
        if expected is None:
            raise StaleEntityRevision(f"mutation target requires an approved revision: {ref.entity_id}")
        result = self.session.exec(
            update(model)
            .where(model.id == pk, model.revision == expected)
            .values(revision=expected + 1)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise StaleEntityRevision(f"stale entity revision: {ref.entity_id}")
        self.session.flush()
        self.session.expire(row)
        claimed = self.session.get(model, pk)
        self.claimed_revisions[claim_key] = expected + 1
        return claimed

    def _claim_implied(
        self,
        model: type[Any],
        row: Any,
        *,
        kind: str,
        entity_id: str,
    ) -> Any:
        return self._claim(
            model,
            row.id,
            EntityRef(
                kind=kind,
                entity_id=entity_id,
                revision=self.expected_revisions.get((kind, entity_id)),
            ),
        )

    def _bind_id(self, operation_id: str, temporary_id: str, ref: EntityRef) -> None:
        self.temp_refs[temporary_id] = ref
        self.result.bindings.append(
            EntityBinding(
                operation_id=operation_id,
                temporary_id=temporary_id,
                entity_ref=ref,
            )
        )

    def _bind(self, operation: Any, ref: EntityRef) -> None:
        temp_ref = getattr(operation, "temp_ref", None)
        if temp_ref is not None:
            temporary_id = temp_ref.entity_id
        else:
            target = {
                "task.create": "task",
                "calendar.create_block": "calendar_block",
                "recurrence.create_series": "recurrence_series",
                "preference.upsert": "preference",
                "preference.supersede": "preference",
            }.get(operation.kind)
            if target is None:
                return
            temporary_id = (
                f"projected:preference:{operation.operation_id}"
                if target == "preference"
                else f"projected:{target}:{operation.operation_id}"
            )
        self._bind_id(operation.operation_id, temporary_id, ref)

    def _fact(
        self,
        operation_id: str,
        ref: EntityRef,
        *,
        disposition: str = "present",
        values: dict[str, Any] | None = None,
    ) -> None:
        payload = values or {"disposition": disposition}
        self.result.facts.append(
            ExecutionFact(
                operation_id=operation_id,
                disposition=disposition,
                entity_ref=ref,
                fields=[ExpectedField(field=key, value_json=_canonical(value)) for key, value in sorted((values or {}).items())],
                fingerprint=_fingerprint(payload),
            )
        )

    @staticmethod
    def _task_values(row: StrategyNode) -> dict[str, Any]:
        return {
            "title": row.title,
            "notes": row.notes,
            "status": row.status,
            "priority": row.priority,
            "category_id": row.category_id,
            "parent_id": row.parent_id,
            "planned_date": row.planned_date,
            "deadline_at": row.deadline_at,
            "time_estimate_minutes": row.time_estimate_minutes,
            "is_backlog": row.is_backlog,
            "tags": row.tags,
            "revision": row.revision,
        }

    @staticmethod
    def _block_values(row: CalendarBlock) -> dict[str, Any]:
        return {
            "task_id": row.task_id,
            "category_id": row.category_id,
            "start_at": row.start_at.replace(tzinfo=None).isoformat(),
            "end_at": row.end_at.replace(tzinfo=None).isoformat(),
            "recurrence_series_id": row.recurrence_series_id,
            "recurrence_key": row.recurrence_key,
            "revision": row.revision,
        }

    def _apply_task_create(self, operation: Any, _change_set: ChangeSet) -> None:
        workspace = self._resolve(operation.workspace_ref)
        category_id = _numeric_id(workspace.entity_id, "workspace")
        category = self.session.get(Category, category_id)
        if category is None:
            raise DomainMutationError("task workspace does not exist")
        self._assert_current(operation.workspace_ref, category)
        parent_id = None
        parent = None
        if operation.parent_ref is not None:
            parent_ref = self._resolve(operation.parent_ref)
            parent_id = _numeric_id(parent_ref.entity_id, "task")
            parent = self.session.get(StrategyNode, parent_id)
            if parent is None or parent.category_id != category_id:
                raise DomainMutationError("task parent is missing or belongs to another workspace")
            self._assert_current(operation.parent_ref, parent)
        fields = operation.fields
        row = StrategyNode(
            id=allocate_strategy_node_id(self.session),
            category_id=category_id,
            title=operation.title,
            notes=fields.notes,
            status=_status(fields.status) or "todo",
            priority=fields.priority or "medium",
            parent_id=parent_id,
            planned_date=fields.planned_date.isoformat() if fields.planned_date else None,
            deadline_at=serialize_deadline_date(fields.deadline_at),
            time_estimate_minutes=fields.estimate_minutes,
            tags=(
                json.dumps(fields.tags, ensure_ascii=True)
                if fields.tags
                else None
            ),
            is_backlog=(
                fields.is_backlog
                if fields.is_backlog is not None
                else bool(parent.is_backlog) if parent is not None else False
            ),
        )
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(kind="task", entity_id=f"task:{row.id}", revision=row.revision, title=row.title)
        self._bind(operation, ref)
        self._fact(operation.operation_id, ref, values=self._task_values(row))

    def _apply_task_update(self, operation: Any, _change_set: ChangeSet) -> None:
        ref = self._resolve(operation.task_ref)
        row = self._claim(StrategyNode, _numeric_id(ref.entity_id, "task"), operation.task_ref)
        updates = operation.updates
        for name in ("title", "notes", "priority"):
            value = getattr(updates, name)
            if value is not None:
                setattr(row, name, value)
        if updates.status is not None:
            row.status = _status(updates.status) or row.status
        if updates.estimate_minutes is not None:
            row.time_estimate_minutes = updates.estimate_minutes
        if updates.planned_date is not None:
            row.planned_date = updates.planned_date.isoformat()
        if updates.deadline_at is not None:
            row.deadline_at = serialize_deadline_date(updates.deadline_at)
        if updates.tags is not None:
            row.tags = (
                json.dumps(updates.tags, ensure_ascii=True)
                if updates.tags
                else None
            )
        if updates.is_backlog is not None:
            row.is_backlog = updates.is_backlog
            for descendant_id in self._descendant_ids(int(row.id)):
                descendant = self.session.get(StrategyNode, descendant_id)
                if descendant is not None:
                    descendant.is_backlog = updates.is_backlog
                    self.session.add(descendant)
        for name in updates.clear_fields:
            setattr(row, "deadline_at" if name == "deadline_at" else name, None)
        self.session.add(row)
        self.session.flush()
        actual_ref = EntityRef(kind="task", entity_id=ref.entity_id, revision=row.revision, title=row.title)
        self._fact(operation.operation_id, actual_ref, values=self._task_values(row))

    def _descendant_ids(self, root_id: int) -> list[int]:
        found: list[int] = []
        frontier = [root_id]
        while frontier:
            parent = frontier.pop()
            children = self.session.exec(select(StrategyNode).where(StrategyNode.parent_id == parent)).all()
            for child in children:
                if child.id is not None:
                    found.append(int(child.id))
                    frontier.append(int(child.id))
        return found

    def _apply_task_move(self, operation: Any, _change_set: ChangeSet) -> None:
        parent_id = None
        category_id = None
        if operation.destination_parent_ref is not None:
            parent_ref = self._resolve(operation.destination_parent_ref)
            parent = self.session.get(StrategyNode, _numeric_id(parent_ref.entity_id, "task"))
            if parent is None:
                raise DomainMutationError("destination parent does not exist")
            self._assert_current(operation.destination_parent_ref, parent)
            parent_id, category_id = int(parent.id), parent.category_id
        if operation.destination_workspace_ref is not None:
            workspace_ref = self._resolve(operation.destination_workspace_ref)
            category_id = _numeric_id(workspace_ref.entity_id, "workspace")
            category = self.session.get(Category, category_id)
            if category is None:
                raise DomainMutationError("destination workspace does not exist")
            self._assert_current(operation.destination_workspace_ref, category)
        for task_ref in operation.task_refs:
            ref = self._resolve(task_ref)
            task_id = _numeric_id(ref.entity_id, "task")
            if parent_id == task_id or parent_id in self._descendant_ids(task_id):
                raise DomainMutationError("task move would create a cycle")
            row = self._claim(StrategyNode, task_id, task_ref)
            row.parent_id = parent_id
            if category_id is not None and row.category_id != category_id:
                moved_ids = [task_id, *self._descendant_ids(task_id)]
                for moved_id in moved_ids:
                    moved = row if moved_id == task_id else self.session.get(StrategyNode, moved_id)
                    if moved is not None:
                        if moved_id != task_id:
                            moved = self._claim_implied(
                                StrategyNode,
                                moved,
                                kind="task",
                                entity_id=f"task:{moved_id}",
                            )
                        moved.category_id = category_id
                        self.session.add(moved)
                for block in self.session.exec(select(CalendarBlock).where(CalendarBlock.task_id.in_(moved_ids))).all():
                    block = self._claim_implied(
                        CalendarBlock,
                        block,
                        kind="calendar_block",
                        entity_id=f"calendar_block:{block.id}",
                    )
                    block.category_id = category_id
                    self.session.add(block)
                for series in self.session.exec(select(RecurrenceSeries).where(RecurrenceSeries.task_id.in_(moved_ids))).all():
                    series = self._claim_implied(
                        RecurrenceSeries,
                        series,
                        kind="recurrence_series",
                        entity_id=f"recurrence_series:{series.id}",
                    )
                    series.category_id = category_id
                    self.session.add(series)
            self.session.add(row)
            self.session.flush()
            self._fact(operation.operation_id, EntityRef(kind="task", entity_id=ref.entity_id, revision=row.revision), values=self._task_values(row))

    def _apply_task_archive(self, operation: Any, _change_set: ChangeSet) -> None:
        for task_ref in operation.task_refs:
            ref = self._resolve(task_ref)
            root_id = _numeric_id(ref.entity_id, "task")
            row = self._claim(StrategyNode, root_id, task_ref)
            for task_id in [root_id, *self._descendant_ids(root_id)]:
                item = row if task_id == root_id else self.session.get(StrategyNode, task_id)
                if item is not None:
                    if task_id != root_id:
                        item = self._claim_implied(
                            StrategyNode,
                            item,
                            kind="task",
                            entity_id=f"task:{task_id}",
                        )
                    tags = []
                    try:
                        parsed = json.loads(item.tags or "[]")
                        if isinstance(parsed, list):
                            tags = [str(value) for value in parsed]
                    except (TypeError, ValueError):
                        tags = [value.strip() for value in (item.tags or "").split(",") if value.strip()]
                    if "state:archived" not in {value.lower() for value in tags}:
                        tags.append("state:archived")
                    item.tags = _canonical(tags)
                    self.session.add(item)
            self.session.flush()
            self._fact(operation.operation_id, EntityRef(kind="task", entity_id=ref.entity_id, revision=row.revision), values=self._task_values(row))

    def _apply_task_delete(self, operation: Any, _change_set: ChangeSet) -> None:
        for task_ref in operation.task_refs:
            ref = self._resolve(task_ref)
            root_id = _numeric_id(ref.entity_id, "task")
            root = self._claim(StrategyNode, root_id, task_ref)
            ids = [root_id, *self._descendant_ids(root_id)]
            dependent_blocks = sorted(
                self.session.exec(select(CalendarBlock).where(CalendarBlock.task_id.in_(ids))).all()
                , key=lambda item: int(item.id)
            )
            dependent_series = sorted(
                self.session.exec(select(RecurrenceSeries).where(RecurrenceSeries.task_id.in_(ids))).all()
                , key=lambda item: item.id
            )
            id_strings = {str(item) for item in ids}
            dependent_preferences = list(
                self.session.exec(
                    select(UserPreference).where(
                        UserPreference.scope_type == "task",
                        UserPreference.scope_id.in_(id_strings),
                        UserPreference.active == True,  # noqa: E712
                    )
                ).all()
            )
            dependent_preference_ids = {
                preference.id for preference in dependent_preferences
            }
            dependent_constraints = list(
                self.session.exec(
                    select(SchedulingConstraint).where(
                        (
                            (
                                SchedulingConstraint.target_type == "task"
                            )
                            & SchedulingConstraint.target_id.in_(id_strings)
                        )
                        | SchedulingConstraint.source_preference_id.in_(
                            dependent_preference_ids
                        ),
                        SchedulingConstraint.active == True,  # noqa: E712
                    )
                ).all()
            )
            dependent_links = list(
                self.session.exec(
                    select(PreferenceLink).where(
                        (
                            (PreferenceLink.target_type == "task")
                            & PreferenceLink.target_id.in_(id_strings)
                        )
                        | PreferenceLink.preference_id.in_(
                            dependent_preference_ids
                        ),
                        PreferenceLink.active == True,  # noqa: E712
                    )
                ).all()
            )
            for item in ids:
                if item == root_id:
                    continue
                descendant = self.session.get(StrategyNode, item)
                if descendant is not None:
                    self._claim_implied(
                        StrategyNode,
                        descendant,
                        kind="task",
                        entity_id=f"task:{item}",
                    )
            for block in dependent_blocks:
                self._claim_implied(
                    CalendarBlock,
                    block,
                    kind="calendar_block",
                    entity_id=f"calendar_block:{block.id}",
                )
                self.session.delete(block)
            for series in dependent_series:
                self._claim_implied(
                    RecurrenceSeries,
                    series,
                    kind="recurrence_series",
                    entity_id=f"recurrence_series:{series.id}",
                )
                for exception in self.session.exec(
                    select(RecurrenceOccurrenceException).where(
                        RecurrenceOccurrenceException.series_id == series.id
                    )
                ).all():
                    self.session.delete(exception)
                self.session.delete(series)
            for preference in dependent_preferences:
                preference = self._claim_implied(
                    UserPreference,
                    preference,
                    kind="preference",
                    entity_id=f"preference:{preference.id}",
                )
                preference.active = False
                self.session.add(preference)
            for constraint in dependent_constraints:
                constraint = self._claim_implied(
                    SchedulingConstraint,
                    constraint,
                    kind="scheduling_constraint",
                    entity_id=f"scheduling_constraint:{constraint.id}",
                )
                constraint.active = False
                self.session.add(constraint)
            for link in dependent_links:
                link = self._claim_implied(
                    PreferenceLink,
                    link,
                    kind="preference_link",
                    entity_id=f"preference_link:{link.id}",
                )
                link.active = False
                self.session.add(link)
            for entity in self.session.exec(
                select(StrategistSessionEntity).where(
                    StrategistSessionEntity.node_id.in_(ids)
                )
            ).all():
                self.session.delete(entity)
            for mention in self.session.exec(
                select(StrategistTurnMention).where(
                    StrategistTurnMention.node_id.in_(ids)
                )
            ).all():
                mention.node_id = None
                self.session.add(mention)
            for task_id in reversed(ids):
                item = root if task_id == root_id else self.session.get(StrategyNode, task_id)
                if item is not None:
                    self.session.delete(item)
            self.session.flush()
            self._fact(operation.operation_id, EntityRef(kind="task", entity_id=ref.entity_id), disposition="absent")
            for block in dependent_blocks:
                block_ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{block.id}")
                self._fact(operation.operation_id, block_ref, disposition="absent")
                if block.google_event_id:
                    self.result.external_effects.append(
                        ExternalEffect("delete", block_ref.entity_id, {"remote_id": block.google_event_id})
                    )
            for series in dependent_series:
                self._fact(
                    operation.operation_id,
                    EntityRef(kind="recurrence_series", entity_id=f"recurrence_series:{series.id}"),
                    disposition="absent",
                )
            for preference in dependent_preferences:
                self._fact(
                    operation.operation_id,
                    EntityRef(kind="preference", entity_id=f"preference:{preference.id}", revision=preference.revision),
                    disposition="inactive",
                    values={"active": False, "revision": preference.revision},
                )
            for constraint in dependent_constraints:
                self._fact(
                    operation.operation_id,
                    EntityRef(kind="scheduling_constraint", entity_id=f"scheduling_constraint:{constraint.id}", revision=constraint.revision),
                    disposition="inactive",
                    values={"active": False, "revision": constraint.revision},
                )
            for link in dependent_links:
                self._fact(
                    operation.operation_id,
                    EntityRef(kind="preference_link", entity_id=f"preference_link:{link.id}", revision=link.revision),
                    disposition="inactive",
                    values={"active": False, "revision": link.revision},
                )

    def _apply_calendar_create_block(self, operation: Any, _change_set: ChangeSet) -> None:
        task_ref = self._resolve(operation.task_ref)
        task = self.session.get(StrategyNode, _numeric_id(task_ref.entity_id, "task"))
        if task is None:
            raise DomainMutationError("calendar block task does not exist")
        self._assert_current(operation.task_ref, task)
        row = CalendarBlock(
            task_id=int(task.id),
            category_id=task.category_id,
            start_at=_naive(operation.start_at),
            end_at=_naive(operation.end_at),
        )
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{row.id}", revision=row.revision)
        self._bind(operation, ref)
        self._fact(operation.operation_id, ref, values=self._block_values(row))
        self.result.external_effects.append(ExternalEffect("create", ref.entity_id, self._block_values(row)))

    def _calendar_row(self, ref: EntityRef) -> CalendarBlock:
        resolved = self._resolve(ref)
        return self._claim(CalendarBlock, _numeric_id(resolved.entity_id, "calendar_block"), ref)

    def _move_booking_assignment(self, operation: Any, row: CalendarBlock, start_at: datetime) -> None:
        task = self.session.get(StrategyNode, row.task_id)
        if task is None:
            raise DomainMutationError("calendar block task does not exist")
        target_day = moved_planned_day(task.planned_date, row.start_at, start_at)
        if target_day is None:
            return
        task = self._claim_implied(
            StrategyNode, task, kind="task", entity_id=f"task:{task.id}"
        )
        task.planned_date = target_day.isoformat()
        self.session.add(task)
        self.session.flush()
        ref = EntityRef(kind="task", entity_id=f"task:{task.id}", revision=task.revision)
        self._fact(operation.operation_id, ref, values=self._task_values(task))

    def _apply_calendar_move_block(self, operation: Any, _change_set: ChangeSet) -> None:
        row = self._calendar_row(operation.block_ref)
        self._move_booking_assignment(operation, row, operation.start_at)
        row.start_at, row.end_at = _naive(operation.start_at), _naive(operation.end_at)
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{row.id}", revision=row.revision)
        self._fact(operation.operation_id, ref, values=self._block_values(row))
        self.result.external_effects.append(ExternalEffect("update", ref.entity_id, self._block_values(row)))

    def _apply_calendar_resize_block(self, operation: Any, _change_set: ChangeSet) -> None:
        row = self._calendar_row(operation.block_ref)
        if operation.start_at is not None:
            self._move_booking_assignment(operation, row, operation.start_at)
            row.start_at = _naive(operation.start_at)
        row.end_at = _naive(operation.end_at)
        if row.end_at <= row.start_at:
            raise DomainMutationError("resized block has an invalid interval")
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{row.id}", revision=row.revision)
        self._fact(operation.operation_id, ref, values=self._block_values(row))
        self.result.external_effects.append(ExternalEffect("update", ref.entity_id, self._block_values(row)))

    def _apply_calendar_split_block(self, operation: Any, _change_set: ChangeSet) -> None:
        row = self._calendar_row(operation.block_ref)
        if row.is_recurring or row.recurrence_series_id:
            raise DomainMutationError("recurring occurrences require recurrence operations")
        source_id = int(row.id)
        source_ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{source_id}")
        values = {"task_id": row.task_id, "category_id": row.category_id}
        remote_id = row.google_event_id
        self.session.delete(row)
        self._fact(operation.operation_id, source_ref, disposition="absent")
        if remote_id:
            self.result.external_effects.append(ExternalEffect("delete", source_ref.entity_id, {"remote_id": remote_id}))
        for index, segment in enumerate(operation.segments):
            created = CalendarBlock(**values, start_at=_naive(segment.start_at), end_at=_naive(segment.end_at))
            self.session.add(created)
            self.session.flush()
            ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{created.id}", revision=created.revision)
            self._bind_id(
                operation.operation_id,
                f"projected:block:{operation.operation_id}:{index}",
                ref,
            )
            self._fact(operation.operation_id, ref, values=self._block_values(created))
            self.result.external_effects.append(ExternalEffect("create", ref.entity_id, self._block_values(created)))

    def _apply_calendar_delete_block(self, operation: Any, _change_set: ChangeSet) -> None:
        for block_ref in operation.block_refs:
            row = self._calendar_row(block_ref)
            ref = EntityRef(kind="calendar_block", entity_id=f"calendar_block:{row.id}")
            remote_id = row.google_event_id
            self.session.delete(row)
            self.session.flush()
            self._fact(operation.operation_id, ref, disposition="absent")
            if remote_id:
                self.result.external_effects.append(ExternalEffect("delete", ref.entity_id, {"remote_id": remote_id}))

    @staticmethod
    def _weekday_numbers(values: list[str]) -> set[int]:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        return {names.index(value) for value in values}

    def _occurrence_dates(
        self,
        rule: Any,
        *,
        maximum_occurrences: int = 128,
    ) -> list[date]:
        return recurrence_occurrence_dates(
            rule,
            maximum_occurrences=maximum_occurrences,
        )

    def _apply_recurrence_create_series(self, operation: Any, change_set: ChangeSet) -> None:
        task_ref = self._resolve(operation.task_ref)
        task = self.session.get(StrategyNode, _numeric_id(task_ref.entity_id, "task"))
        if task is None:
            raise DomainMutationError("recurrence task does not exist")
        self._assert_current(operation.task_ref, task)
        rule = operation.rule
        row = RecurrenceSeries(
            task_id=int(task.id),
            category_id=task.category_id,
            frequency=rule.frequency,
            interval=rule.interval,
            days_of_week_json=_canonical(rule.weekdays),
            month_days_json=_canonical(rule.month_days),
            start_date=rule.start_date,
            end_date=rule.end_date,
            occurrence_limit=rule.occurrence_count,
            preferred_start_time=rule.preferred_start_time or "09:00",
            allowed_start_time=rule.preferred_start_time or "09:00",
            allowed_end_time="23:59",
            duration_minutes=rule.duration_minutes or task.time_estimate_minutes or 30,
            timezone=rule.timezone,
            materialized_through=None,
            source_agent_change_set_id=(
                change_set.change_set_id
                if self.session.get(AgentChangeSetRecord, change_set.change_set_id) is not None
                else None
            ),
        )
        self.session.add(row)
        self.session.flush()
        start_time = datetime.strptime(row.preferred_start_time, "%H:%M").time()
        dates = self._occurrence_dates(rule)
        for occurrence_date in dates:
            # Persist local wall-clock values consistently with the existing calendar model.
            start_at = datetime.combine(occurrence_date, start_time)
            block = CalendarBlock(
                task_id=int(task.id),
                category_id=task.category_id,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=row.duration_minutes),
                is_recurring=True,
                recurrence_series_id=row.id,
                recurrence_key=occurrence_date.isoformat(),
            )
            self.session.add(block)
            self.session.flush()
            block_ref = EntityRef(
                kind="calendar_block",
                entity_id=f"calendar_block:{block.id}",
                revision=block.revision,
            )
            self._fact(operation.operation_id, block_ref, values=self._block_values(block))
            projected_series_id = (
                operation.temp_ref.entity_id
                if operation.temp_ref is not None
                else f"projected:recurrence_series:{operation.operation_id}"
            )
            series_key = projected_series_id.split(":", 1)[-1]
            self._bind_id(
                operation.operation_id,
                f"projected:occurrence:{series_key}:{occurrence_date.isoformat()}",
                block_ref,
            )
            self._bind_id(
                operation.operation_id,
                f"recurrence_occurrence:{series_key}:{occurrence_date.isoformat()}",
                EntityRef(
                    kind="recurrence_occurrence",
                    entity_id=(
                        f"recurrence_occurrence:{row.id}:"
                        f"{occurrence_date.isoformat()}"
                    ),
                    revision=block.revision,
                ),
            )
            self.result.external_effects.append(
                ExternalEffect("create", block_ref.entity_id, self._block_values(block))
            )
        row.materialized_through = dates[-1] if dates else None
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(kind="recurrence_series", entity_id=f"recurrence_series:{row.id}", revision=row.revision)
        self._bind(operation, ref)
        values = {
            "task_id": row.task_id,
            "frequency": row.frequency,
            "interval": row.interval,
            "days_of_week_json": row.days_of_week_json,
            "month_days_json": row.month_days_json,
            "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat() if row.end_date else None,
            "occurrence_limit": row.occurrence_limit,
            "materialized_through": row.materialized_through.isoformat() if row.materialized_through else None,
            "revision": row.revision,
        }
        self._fact(operation.operation_id, ref, values=values)

    def _apply_recurrence_update_series(self, operation: Any, _change_set: ChangeSet) -> None:
        ref = self._resolve(operation.series_ref)
        row = self._claim(RecurrenceSeries, ref.entity_id.removeprefix("recurrence_series:"), operation.series_ref)
        prior_blocks = sorted(
            self.session.exec(
                select(CalendarBlock).where(CalendarBlock.recurrence_series_id == row.id)
            ).all(),
            key=lambda item: int(item.id),
        )
        updates = operation.updates.model_dump(
            mode="python",
            exclude_unset=True,
            exclude_none=True,
        )
        mapping = {"weekdays": "days_of_week_json", "month_days": "month_days_json", "occurrence_count": "occurrence_limit"}
        for key, value in updates.items():
            target = mapping.get(key, key)
            if key in {"weekdays", "month_days"}:
                value = _canonical(value)
            setattr(row, target, value)
        self.session.add(row)
        self.session.flush()
        rule = RecurrenceRule(
            frequency=row.frequency,
            interval=row.interval,
            weekdays=json.loads(row.days_of_week_json or "[]"),
            month_days=json.loads(row.month_days_json or "[]"),
            start_date=row.start_date,
            end_date=row.end_date,
            occurrence_count=row.occurrence_limit,
            timezone=row.timezone,
            preferred_start_time=row.preferred_start_time,
            duration_minutes=row.duration_minutes,
        )
        start_time = datetime.strptime(row.preferred_start_time, "%H:%M").time()
        exceptions = {
            item.recurrence_key: item
            for item in self.session.exec(
                select(RecurrenceOccurrenceException).where(
                    RecurrenceOccurrenceException.series_id == row.id
                )
            ).all()
        }
        # Preserve an already-visible horizon while bounding expansion of a
        # long-running edited series. Cancelled exceptions still occupy a place
        # in that rolling preview even though no block is recreated for them.
        materialization_limit = max(
            DEFAULT_RECURRENCE_MATERIALIZATION_OCCURRENCES,
            len(prior_blocks) + len(exceptions),
        )
        dates = self._occurrence_dates(
            rule,
            maximum_occurrences=materialization_limit,
        )
        desired_keys = {item.isoformat() for item in dates}
        prior_by_key = {
            item.recurrence_key: item
            for item in prior_blocks
            if item.recurrence_key is not None
        }
        for recurrence_key, block in prior_by_key.items():
            exception = exceptions.get(recurrence_key)
            if recurrence_key in desired_keys and not (
                exception is not None and exception.disposition == "cancelled"
            ):
                continue
            block = self._claim_implied(
                CalendarBlock,
                block,
                kind="calendar_block",
                entity_id=f"calendar_block:{block.id}",
            )
            if block.google_event_id:
                self.result.external_effects.append(
                    ExternalEffect(
                        "delete",
                        f"calendar_block:{block.id}",
                        {"remote_id": block.google_event_id},
                    )
                )
            self.session.delete(block)
            self._fact(
                operation.operation_id,
                EntityRef(
                    kind="calendar_block",
                    entity_id=f"calendar_block:{block.id}",
                ),
                disposition="absent",
            )
        for occurrence_date in dates:
            recurrence_key = occurrence_date.isoformat()
            exception = exceptions.get(recurrence_key)
            if exception is not None and exception.disposition == "cancelled":
                continue
            start_at = (
                exception.start_at
                if exception is not None and exception.start_at is not None
                else datetime.combine(occurrence_date, start_time)
            )
            end_at = (
                exception.end_at
                if exception is not None and exception.end_at is not None
                else start_at + timedelta(minutes=row.duration_minutes)
            )
            block = prior_by_key.get(recurrence_key)
            created = block is None
            if block is None:
                block = CalendarBlock(
                    task_id=row.task_id,
                    category_id=row.category_id,
                    start_at=start_at,
                    end_at=end_at,
                    is_recurring=True,
                    recurrence_series_id=row.id,
                    recurrence_key=recurrence_key,
                )
            else:
                block = self._claim_implied(
                    CalendarBlock,
                    block,
                    kind="calendar_block",
                    entity_id=f"calendar_block:{block.id}",
                )
                block.task_id = row.task_id
                block.category_id = row.category_id
                block.start_at = start_at
                block.end_at = end_at
            self.session.add(block)
            self.session.flush()
            block_ref = EntityRef(
                kind="calendar_block",
                entity_id=f"calendar_block:{block.id}",
                revision=block.revision,
            )
            self._fact(operation.operation_id, block_ref, values=self._block_values(block))
            if created:
                series_key = row.id.split(":", 1)[-1]
                self._bind_id(
                    operation.operation_id,
                    f"projected:occurrence:{series_key}:{recurrence_key}",
                    block_ref,
                )
            self.result.external_effects.append(
                ExternalEffect(
                    "create" if created else "update",
                    block_ref.entity_id,
                    self._block_values(block),
                )
            )
        row.materialized_through = dates[-1] if dates else None
        self.session.add(row)
        self.session.flush()
        actual_ref = EntityRef(kind="recurrence_series", entity_id=ref.entity_id, revision=row.revision)
        self._fact(
            operation.operation_id,
            actual_ref,
            values={
                "frequency": row.frequency,
                "interval": row.interval,
                "days_of_week_json": row.days_of_week_json,
                "month_days_json": row.month_days_json,
                "start_date": row.start_date.isoformat(),
                "end_date": row.end_date.isoformat() if row.end_date else None,
                "occurrence_limit": row.occurrence_limit,
                "materialized_through": row.materialized_through.isoformat() if row.materialized_through else None,
                "revision": row.revision,
            },
        )

    def _occurrence_identity(self, ref: EntityRef) -> tuple[str, str]:
        resolved = self._resolve(ref)
        raw = resolved.entity_id.removeprefix("recurrence_occurrence:")
        try:
            return raw.rsplit(":", 1)
        except ValueError as error:
            raise DomainMutationError("invalid recurrence occurrence identity") from error

    def _occurrence(self, ref: EntityRef) -> CalendarBlock:
        series_id, recurrence_key = self._occurrence_identity(ref)
        matches = list(self.session.exec(select(CalendarBlock).where(
            CalendarBlock.recurrence_series_id == series_id,
            CalendarBlock.recurrence_key == recurrence_key,
        )).all())
        if len(matches) != 1:
            raise DomainMutationError("recurrence occurrence must resolve exactly once")
        return self._claim(CalendarBlock, matches[0].id, ref)

    def _apply_recurrence_update_occurrence(self, operation: Any, change_set: ChangeSet) -> None:
        row = self._occurrence(operation.occurrence_ref)
        updates = operation.updates
        if updates.start_at is not None:
            row.start_at, row.end_at = _naive(updates.start_at), _naive(updates.end_at)
        elif updates.duration_minutes is not None:
            row.end_at = row.start_at + timedelta(minutes=updates.duration_minutes)
        self.session.add(row)
        series_id, recurrence_key = self._occurrence_identity(operation.occurrence_ref)
        exception_id = f"{series_id}:{recurrence_key}"
        exception = self.session.get(RecurrenceOccurrenceException, exception_id)
        if exception is None:
            exception = RecurrenceOccurrenceException(
                id=exception_id,
                series_id=series_id,
                recurrence_key=recurrence_key,
                disposition="overridden",
            )
        else:
            exception.revision += 1
        exception.disposition = "overridden"
        exception.start_at = row.start_at
        exception.end_at = row.end_at
        exception.source_change_set_id = change_set.change_set_id
        exception.updated_at = datetime.utcnow()
        self.session.add(exception)
        self.session.flush()
        ref = EntityRef(kind="recurrence_occurrence", entity_id=operation.occurrence_ref.entity_id, revision=row.revision)
        self._fact(operation.operation_id, ref, values=self._block_values(row))
        self.result.external_effects.append(ExternalEffect("update", f"calendar_block:{row.id}", self._block_values(row)))

    def _apply_recurrence_cancel_occurrence(self, operation: Any, change_set: ChangeSet) -> None:
        row = self._occurrence(operation.occurrence_ref)
        remote_id = row.google_event_id
        entity_id = f"calendar_block:{row.id}"
        series_id, recurrence_key = self._occurrence_identity(operation.occurrence_ref)
        exception_id = f"{series_id}:{recurrence_key}"
        exception = self.session.get(RecurrenceOccurrenceException, exception_id)
        if exception is None:
            exception = RecurrenceOccurrenceException(
                id=exception_id,
                series_id=series_id,
                recurrence_key=recurrence_key,
                disposition="cancelled",
            )
        else:
            exception.revision += 1
        exception.disposition = "cancelled"
        exception.start_at = None
        exception.end_at = None
        exception.source_change_set_id = change_set.change_set_id
        exception.updated_at = datetime.utcnow()
        self.session.add(exception)
        self.session.delete(row)
        self.session.flush()
        self._fact(operation.operation_id, EntityRef(kind="recurrence_occurrence", entity_id=operation.occurrence_ref.entity_id), disposition="absent")
        if remote_id:
            self.result.external_effects.append(ExternalEffect("delete", entity_id, {"remote_id": remote_id}))

    @staticmethod
    def _preference_value(operation: Any) -> str:
        """Persist the logical value, not the transport union's tagged wrapper."""
        return encode_preference_value(operation.value)

    @staticmethod
    def _preference_description(operation: Any) -> str | None:
        provenance = getattr(operation, "provenance", None)
        source_text = str(getattr(provenance, "source_text", None) or "").strip()
        return source_text[:512] or None

    def _create_enforcement_constraint(
        self,
        operation: Any,
        preference: UserPreference,
    ) -> None:
        if operation.semantics.strength != "hard":
            return
        logical_value = decode_preference_value(preference.value_json)
        constraint_type = enforcement_constraint_type(
            preference.preference_type,
            logical_value,
        )
        if constraint_type is None:
            return
        row = SchedulingConstraint(
            user_id=preference.user_id,
            constraint_type=constraint_type,
            target_type=preference.scope_type,
            target_id=preference.scope_id,
            hard=True,
            source_preference_id=preference.id,
            source=preference.source,
            confidence=preference.confidence,
            effective_from=preference.effective_from,
            effective_until=preference.effective_until,
            description=preference.description,
            value_json=preference.value_json,
        )
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(
            kind="scheduling_constraint",
            entity_id=row.id,
            revision=row.revision,
        )
        self._bind_id(
            operation.operation_id,
            f"projected:scheduling_constraint:{operation.operation_id}",
            ref,
        )
        self._fact(
            operation.operation_id,
            ref,
            values={
                "active": row.active,
                "hard": row.hard,
                "source_preference_id": row.source_preference_id,
                "value_json": row.value_json,
                "revision": row.revision,
            },
        )

    def _apply_preference_upsert(self, operation: Any, _change_set: ChangeSet) -> None:
        semantics = operation.semantics
        existing = self.session.exec(select(UserPreference).where(
            UserPreference.user_id == "local",
            UserPreference.preference_type == operation.preference_key,
            UserPreference.scope_type == semantics.scope,
            UserPreference.active == True,  # noqa: E712
        )).first()
        if existing is not None:
            raise DomainMutationError("preference.upsert cannot overwrite an active preference; use supersede")
        row = UserPreference(
            domain=semantics.domain,
            preference_type=operation.preference_key,
            scope_type=semantics.scope,
            strength="high" if semantics.strength == "hard" else "medium",
            effective_from=semantics.effective_from,
            effective_until=semantics.effective_until,
            description=self._preference_description(operation),
            value_json=self._preference_value(operation),
        )
        self.session.add(row)
        self.session.flush()
        ref = EntityRef(kind="preference", entity_id=f"preference:{row.id}", revision=row.revision)
        self._bind(operation, ref)
        self._fact(operation.operation_id, ref, values={"active": row.active, "value_json": row.value_json, "revision": row.revision})
        self._create_enforcement_constraint(operation, row)

    def _apply_preference_supersede(self, operation: Any, _change_set: ChangeSet) -> None:
        ref = self._resolve(operation.preference_ref)
        prior = self._claim(UserPreference, ref.entity_id.removeprefix("preference:"), operation.preference_ref)
        prior.active = False
        prior.updated_at = datetime.utcnow()
        self.session.add(prior)
        semantics = operation.semantics
        row = UserPreference(
            domain=semantics.domain,
            preference_type=prior.preference_type,
            scope_type=semantics.scope,
            scope_id=prior.scope_id,
            strength="high" if semantics.strength == "hard" else "medium",
            effective_from=semantics.effective_from,
            effective_until=semantics.effective_until,
            description=self._preference_description(operation),
            value_json=self._preference_value(operation),
        )
        self.session.add(row)
        self.session.flush()
        for constraint in self.session.exec(
            select(SchedulingConstraint).where(
                SchedulingConstraint.source_preference_id == prior.id,
                SchedulingConstraint.active == True,  # noqa: E712
            )
        ).all():
            constraint = self._claim_implied(
                SchedulingConstraint,
                constraint,
                kind="scheduling_constraint",
                entity_id=f"scheduling_constraint:{constraint.id}",
            )
            constraint.active = False
            constraint.updated_at = datetime.utcnow()
            self.session.add(constraint)
            self._fact(
                operation.operation_id,
                EntityRef(
                    kind="scheduling_constraint",
                    entity_id=f"scheduling_constraint:{constraint.id}",
                    revision=constraint.revision,
                ),
                disposition="inactive",
                values={"active": False, "revision": constraint.revision},
            )
        for index, link in enumerate(self.session.exec(select(PreferenceLink).where(PreferenceLink.preference_id == prior.id, PreferenceLink.active == True).order_by(PreferenceLink.id)).all()):  # noqa: E712
            link = self._claim_implied(
                PreferenceLink,
                link,
                kind="preference_link",
                entity_id=f"preference_link:{link.id}",
            )
            link.active = False
            self.session.add(link)
            replacement_link = PreferenceLink(
                preference_id=row.id,
                target_type=link.target_type,
                target_id=link.target_id,
                relation_type=link.relation_type,
                weight=link.weight,
            )
            self.session.add(replacement_link)
            self.session.flush()
            self._bind_id(
                operation.operation_id,
                f"projected:preference_link:{operation.operation_id}:{index}",
                EntityRef(
                    kind="preference_link",
                    entity_id=f"preference_link:{replacement_link.id}",
                    revision=replacement_link.revision,
                ),
            )
            self._fact(
                operation.operation_id,
                EntityRef(kind="preference_link", entity_id=f"preference_link:{link.id}", revision=link.revision),
                disposition="inactive",
                values={"active": False, "revision": link.revision},
            )
            self._fact(
                operation.operation_id,
                EntityRef(kind="preference_link", entity_id=f"preference_link:{replacement_link.id}", revision=replacement_link.revision),
                values={
                    "active": True,
                    "preference_id": replacement_link.preference_id,
                    "target_type": replacement_link.target_type,
                    "target_id": replacement_link.target_id,
                    "revision": replacement_link.revision,
                },
            )
        self.session.add(PreferenceChangeLog(record_type="preference", record_id=prior.id, change_type="superseded", actor="agent_kernel"))
        self._bind(
            operation,
            EntityRef(
                kind="preference",
                entity_id=f"preference:{row.id}",
                revision=row.revision,
            ),
        )
        self._fact(operation.operation_id, EntityRef(kind="preference", entity_id=ref.entity_id, revision=prior.revision), disposition="inactive", values={"active": False, "revision": prior.revision})
        self._fact(operation.operation_id, EntityRef(kind="preference", entity_id=f"preference:{row.id}", revision=row.revision), values={"active": True, "value_json": row.value_json, "revision": row.revision})
        self._create_enforcement_constraint(operation, row)

    def _apply_preference_retire(self, operation: Any, _change_set: ChangeSet) -> None:
        ref = self._resolve(operation.preference_ref)
        row = self._claim(UserPreference, ref.entity_id.removeprefix("preference:"), operation.preference_ref)
        row.active = False
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        for constraint in self.session.exec(select(SchedulingConstraint).where(SchedulingConstraint.source_preference_id == row.id, SchedulingConstraint.active == True)).all():  # noqa: E712
            constraint = self._claim_implied(
                SchedulingConstraint,
                constraint,
                kind="scheduling_constraint",
                entity_id=f"scheduling_constraint:{constraint.id}",
            )
            constraint.active = False
            self.session.add(constraint)
            self._fact(
                operation.operation_id,
                EntityRef(kind="scheduling_constraint", entity_id=f"scheduling_constraint:{constraint.id}", revision=constraint.revision),
                disposition="inactive",
                values={"active": False, "revision": constraint.revision},
            )
        for link in self.session.exec(select(PreferenceLink).where(PreferenceLink.preference_id == row.id, PreferenceLink.active == True)).all():  # noqa: E712
            link = self._claim_implied(
                PreferenceLink,
                link,
                kind="preference_link",
                entity_id=f"preference_link:{link.id}",
            )
            link.active = False
            self.session.add(link)
            self._fact(
                operation.operation_id,
                EntityRef(kind="preference_link", entity_id=f"preference_link:{link.id}", revision=link.revision),
                disposition="inactive",
                values={"active": False, "revision": link.revision},
            )
        self.session.add(PreferenceChangeLog(record_type="preference", record_id=row.id, change_type="retired", actor="agent_kernel"))
        self.session.flush()
        self._fact(operation.operation_id, EntityRef(kind="preference", entity_id=ref.entity_id, revision=row.revision), disposition="inactive", values={"active": False, "revision": row.revision})

    def _apply_preference_restore(self, operation: Any, _change_set: ChangeSet) -> None:
        ref = self._resolve(operation.preference_ref)
        row = self._claim(
            UserPreference,
            ref.entity_id.removeprefix("preference:"),
            operation.preference_ref,
        )
        row.active = True
        row.updated_at = datetime.utcnow()
        self.session.add(row)
        for constraint in self.session.exec(
            select(SchedulingConstraint).where(
                SchedulingConstraint.user_id == row.user_id,
                SchedulingConstraint.source_preference_id == row.id,
                SchedulingConstraint.active == False,  # noqa: E712
            )
        ).all():
            constraint = self._claim_implied(
                SchedulingConstraint,
                constraint,
                kind="scheduling_constraint",
                entity_id=f"scheduling_constraint:{constraint.id}",
            )
            constraint.active = True
            constraint.updated_at = datetime.utcnow()
            self.session.add(constraint)
            self._fact(
                operation.operation_id,
                EntityRef(
                    kind="scheduling_constraint",
                    entity_id=f"scheduling_constraint:{constraint.id}",
                    revision=constraint.revision,
                ),
                values={"active": True, "revision": constraint.revision},
            )
        for link in self.session.exec(
            select(PreferenceLink).where(
                PreferenceLink.user_id == row.user_id,
                PreferenceLink.preference_id == row.id,
                PreferenceLink.active == False,  # noqa: E712
            )
        ).all():
            link = self._claim_implied(
                PreferenceLink,
                link,
                kind="preference_link",
                entity_id=f"preference_link:{link.id}",
            )
            link.active = True
            self.session.add(link)
            self._fact(
                operation.operation_id,
                EntityRef(
                    kind="preference_link",
                    entity_id=f"preference_link:{link.id}",
                    revision=link.revision,
                ),
                values={"active": True, "revision": link.revision},
            )
        self.session.add(
            PreferenceChangeLog(
                user_id=row.user_id,
                record_type="preference",
                record_id=row.id,
                change_type="restored",
                actor="agent_kernel",
            )
        )
        self.session.flush()
        self._fact(
            operation.operation_id,
            EntityRef(
                kind="preference",
                entity_id=ref.entity_id,
                revision=row.revision,
            ),
            values={"active": True, "revision": row.revision},
        )

    def _apply_preference_delete(self, operation: Any, _change_set: ChangeSet) -> None:
        ref = self._resolve(operation.preference_ref)
        row = self._claim(
            UserPreference,
            ref.entity_id.removeprefix("preference:"),
            operation.preference_ref,
        )
        constraints = self.session.exec(
            select(SchedulingConstraint).where(
                SchedulingConstraint.user_id == row.user_id,
                SchedulingConstraint.source_preference_id == row.id,
            )
        ).all()
        observations = self.session.exec(
            select(PreferenceObservation).where(
                PreferenceObservation.user_id == row.user_id,
                PreferenceObservation.preference_id == row.id,
            )
        ).all()
        links = self.session.exec(
            select(PreferenceLink).where(
                PreferenceLink.user_id == row.user_id,
                PreferenceLink.preference_id == row.id,
            )
        ).all()
        self.session.add(
            PreferenceChangeLog(
                user_id=row.user_id,
                record_type="preference",
                record_id=row.id,
                change_type="deleted",
                actor="agent_kernel",
                before_json=json.dumps(
                    {
                        "active": row.active,
                        "preferenceType": row.preference_type,
                        "scopeType": row.scope_type,
                        "scopeId": row.scope_id,
                        "description": row.description,
                        "value": decode_preference_value(row.value_json),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
        for record in [*constraints, *observations, *links]:
            self.session.delete(record)
        self.session.delete(row)
        self.session.flush()
        self._fact(
            operation.operation_id,
            EntityRef(kind="preference", entity_id=ref.entity_id),
            disposition="absent",
        )
