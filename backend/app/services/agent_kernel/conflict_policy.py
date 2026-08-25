"""Typed, evidence-only conflict detection and deterministic recommendations."""

from __future__ import annotations

import hashlib
from itertools import chain, product
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

from app.services.agent_kernel.contracts import (
    AlternativeFeasibilityProof,
    BlockerEvidence,
    CalendarSegment,
    CalendarSplitBlockOperation,
    CalendarMoveBlockOperation,
    ChangeSet,
    ChangeSetDecisionDelta,
    ChangeSetOperationAdd,
    ChangeSetOperationReplace,
    ChangeSetRevisionDelta,
    Conflict,
    ConflictAlternative,
    ConflictGraph,
    Constraint,
    DecisionConflictDetail,
    DecisionRequest,
    EntityRef,
    OverlapInterval,
    RecurrenceOccurrencePatch,
    RecurrenceUpdateOccurrenceOperation,
    SimulationSnapshot,
)
from app.services.agent_kernel.change_set_deltas import apply_change_set_delta
from app.services.agent_kernel.simulation import ChangeSetProjector, ProjectedCalendarBlock, ProjectedConstraint, SimulationWorld, _fingerprint
from app.services.scheduling_solver import OccupiedInterval, SolverTask, solve_schedule_draft


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _overlap(
    left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime
) -> tuple[datetime, datetime] | None:
    start = max(left_start, right_start)
    end = min(left_end, right_end)
    if end <= start:
        return None
    # Comparisons between aware datetimes are absolute, but max/min preserve the
    # tzinfo of whichever operand wins. Equal boundaries can therefore produce a
    # mixed-zone pair (for example 17:00Z to 11:00-07:00) even though both values
    # describe a 10:00-11:00 local overlap. Compiled incoming commitments carry the
    # user's configured timezone for unqualified times, or the explicitly named
    # timezone when the request supplies one. Keep that effective display timezone
    # consistent across conflict copy and contracts.
    if left_start.tzinfo is not None:
        start = start.astimezone(left_start.tzinfo)
        end = end.astimezone(left_start.tzinfo)
    return start, end


def _clock_label(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{value.strftime('%M %p')}"


def _datetime_range_label(start_at: datetime, end_at: datetime) -> str:
    if start_at.tzinfo is not None and end_at.tzinfo is not None:
        end_at = end_at.astimezone(start_at.tzinfo)
    date_label = start_at.strftime("%B %d, %Y").replace(" 0", " ")
    if start_at.date() == end_at.date():
        return f"{date_label}, {_clock_label(start_at)}–{_clock_label(end_at)}"
    end_label = end_at.strftime("%B %d, %Y").replace(" 0", " ")
    return (
        f"{date_label}, {_clock_label(start_at)}–"
        f"{end_label}, {_clock_label(end_at)}"
    )


def _bounded_label(value: str, limit: int = 300) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 1].rstrip()}…"


_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_local_time(value: object) -> time | None:
    if isinstance(value, time):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def _day_is_allowed(value: object, day_index: int) -> bool:
    if not isinstance(value, list) or not value:
        return True
    allowed: set[int] = set()
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 6:
            allowed.add(item)
            continue
        text = str(item).strip().lower()
        if text.isdigit() and 0 <= int(text) <= 6:
            allowed.add(int(text))
        elif text in _WEEKDAY_INDEX:
            allowed.add(_WEEKDAY_INDEX[text])
    # A non-empty but wholly malformed hard-policy list must not disable the
    # constraint. Treat it as applying every day so the policy fails closed.
    return not allowed or day_index in allowed


def _recurring_window_overlap(
    value: dict[str, object],
    proposed: ProjectedCalendarBlock,
) -> tuple[datetime, datetime] | None:
    start_time = _parse_local_time(
        value.get("startTime") or value.get("start_time")
    )
    end_time = _parse_local_time(value.get("endTime") or value.get("end_time"))
    if start_time is None or end_time is None:
        return None
    raw_days = value.get("daysOfWeek") or value.get("days_of_week")
    first_day = proposed.start_at.date() - timedelta(days=1)
    last_day = proposed.end_at.date()
    day = first_day
    while day <= last_day:
        if _day_is_allowed(raw_days, day.weekday()):
            start_at = datetime.combine(day, start_time, tzinfo=proposed.start_at.tzinfo)
            end_at = datetime.combine(day, end_time, tzinfo=proposed.start_at.tzinfo)
            if end_at <= start_at:
                end_at += timedelta(days=1)
            overlap = _overlap(
                proposed.start_at,
                proposed.end_at,
                start_at,
                end_at,
            )
            if overlap is not None:
                return overlap
        day += timedelta(days=1)
    return None


class TypedConflictPolicy:
    """Computes facts and exact repair candidates without parsing prose or titles."""

    def evaluate(
        self,
        change_set: ChangeSet,
        world: SimulationWorld,
        snapshot: SimulationSnapshot,
        *,
        goal_revision: int,
        goal_constraints: Iterable[Constraint] = (),
        anchored_operation_ids: set[str] | None = None,
        base_world: SimulationWorld | None = None,
    ) -> ConflictGraph:
        goal_constraints = list(goal_constraints)
        anchored_operation_ids = anchored_operation_ids or set()
        self._snapshot = snapshot
        self._base_world = base_world
        self._all_constraints = [*goal_constraints, *change_set.constraints]
        self._preferences = [item for item in world.preferences.values() if item.active]
        self._represented_preference_ids = {
            item.source_preference_id
            for item in world.scheduling_constraints.values()
            if item.active and item.source_preference_id
        }
        conflicts: list[Conflict] = []
        incoming = sorted(
            (item for item in world.calendar_blocks.values() if item.source_operation_id),
            key=lambda item: (item.start_at, item.end_at, item.ref.entity_id),
        )
        all_blocks = sorted(
            world.calendar_blocks.values(),
            key=lambda item: (item.start_at, item.end_at, item.ref.entity_id),
        )
        operation_by_id = {item.operation_id: item for item in change_set.operations}

        for proposed in incoming:
            blockers = [
                item
                for item in all_blocks
                if item.ref.entity_id != proposed.ref.entity_id
                and _overlap(proposed.start_at, proposed.end_at, item.start_at, item.end_at)
            ]
            if blockers:
                conflicts.append(
                    self._calendar_conflict(
                        change_set,
                        proposed,
                        blockers,
                        world,
                        operation_by_id,
                        incoming_anchored=(proposed.source_operation_id in anchored_operation_ids),
                    )
                )
            for constraint in self._all_constraints:
                if (
                    constraint.strength != "hard"
                    or constraint.superseded_by is not None
                ):
                    continue
                if not self._constraint_applies(constraint, proposed):
                    continue
                interval = self._constraint_violation_interval(constraint, proposed, all_blocks)
                if interval is not None:
                    source = "goal_constraint" if constraint in goal_constraints else "change_set_constraint"
                    conflicts.append(
                        self._constraint_conflict(
                            change_set, proposed, constraint, interval, world, operation_by_id, source
                        )
                    )

        conflicts.extend(self._stored_constraint_conflicts(change_set, incoming, world))
        conflicts.extend(self._hard_preference_conflicts(change_set, incoming, world, operation_by_id))

        conflict_ids = [item.conflict_id for item in conflicts]
        for conflict in conflicts:
            verified: list[ConflictAlternative] = []
            for alternative in conflict.alternatives:
                if alternative.feasibility != "feasible" or alternative.change_set_delta is None:
                    verified.append(alternative)
                    continue
                proof = self._prove_delta(change_set, alternative.change_set_delta, conflict_ids)
                if proof is None:
                    verified.append(ConflictAlternative(
                        alternative_id=alternative.alternative_id,
                        summary=alternative.summary,
                        feasibility="unavailable",
                        affected_entities=alternative.affected_entities,
                        tradeoff=alternative.tradeoff,
                        placement_relation=alternative.placement_relation,
                        change_set_delta=alternative.change_set_delta,
                        unavailable_reason="hard_constraint",
                    ))
                else:
                    verified.append(alternative.model_copy(update={"feasibility_proof": proof}))
            conflict.alternatives = verified

        decision_conflicts = [
            item for item in conflicts if item.resolution_disposition == "needs_user_decision"
        ]
        decision_request = None
        if any(item.resolution_disposition == "unresolvable" for item in conflicts):
            status = "invalid"
        elif decision_conflicts:
            options: list[ChangeSetDecisionDelta] = []
            seen_delta_ids: set[str] = set()
            affected: dict[tuple[str, str], EntityRef] = {}
            decision_alternatives = self._complete_decision_alternatives(
                change_set, decision_conflicts, conflict_ids
            )
            if decision_alternatives:
                decision_conflicts[0].alternatives = [
                    *decision_conflicts[0].alternatives,
                    *decision_alternatives,
                ][:12]
            for conflict in conflicts:
                for blocker in conflict.blockers:
                    if blocker.entity_ref is not None:
                        affected[(blocker.entity_ref.kind, blocker.entity_ref.entity_id)] = blocker.entity_ref
            for conflict in decision_conflicts:
                for alternative in conflict.alternatives:
                    for entity in alternative.affected_entities:
                        affected[(entity.kind, entity.entity_id)] = entity
            for alternative in decision_alternatives:
                delta_id = _fingerprint(alternative.change_set_delta.model_dump(mode="json", by_alias=True))
                if delta_id in seen_delta_ids or len(options) >= 6:
                    continue
                seen_delta_ids.add(delta_id)
                options.append(
                    ChangeSetDecisionDelta(
                        option_id=f"option:{alternative.alternative_id.split(':', 1)[-1]}",
                        label=alternative.summary,
                        change_set_id=change_set.change_set_id,
                        alternative_id=alternative.alternative_id,
                        placement_relation=alternative.placement_relation,
                        delta=alternative.change_set_delta,
                    )
                )
            if not options:
                # A decision without executable semantics is unsafe, so leave the graph invalid.
                status = "invalid"
            else:
                status = "needs_user_decision"
                decision_id = _stable_id(
                    "decision", change_set.change_set_id, str(change_set.revision), snapshot.fingerprint
                )
                decision_request = DecisionRequest(
                    decision_id=decision_id,
                    goal_id=change_set.goal_id,
                    goal_revision=goal_revision + 1,
                    target_revision=goal_revision + 2,
                    question=self._decision_question(conflicts),
                    reason=self._decision_reason(conflicts),
                    affected_entities=list(affected.values()),
                    conflicts=self._decision_conflict_details(conflicts),
                    options=options,
                )
        elif conflicts:
            status = "resolved"
        else:
            status = "clear"

        return ConflictGraph(
            graph_id=_stable_id(
                "graph", change_set.change_set_id, str(change_set.revision), snapshot.fingerprint
            ),
            goal_id=change_set.goal_id,
            goal_revision=goal_revision,
            change_set_id=change_set.change_set_id,
            change_set_revision=change_set.revision,
            change_set_operation_ids=[item.operation_id for item in change_set.operations],
            snapshot_fingerprint=snapshot.fingerprint,
            status=status,
            conflicts=conflicts,
            decision_request=decision_request,
        )

    @staticmethod
    def _decision_question(conflicts: list[Conflict]) -> str:
        blocker_labels = list(dict.fromkeys(
            blocker.label
            or (blocker.entity_ref.title if blocker.entity_ref is not None else None)
            for conflict in conflicts
            for blocker in conflict.blockers
            if blocker.label or (blocker.entity_ref is not None and blocker.entity_ref.title)
        ))
        intervals = sorted(
            (
                (interval.start_at, interval.end_at)
                for conflict in conflicts
                for interval in conflict.overlap_intervals
            ),
            key=lambda item: item[0],
        )
        conflict_time = _datetime_range_label(*intervals[0]) if intervals else None
        named_blockers = ", ".join(f"“{label}”" for label in blocker_labels[:3])
        if len(blocker_labels) > 3:
            named_blockers += f", and {len(blocker_labels) - 3} more"
        if named_blockers and conflict_time:
            return _bounded_label(
                f"How should I resolve the conflict with {named_blockers} during {conflict_time}?",
                1000,
            )
        if named_blockers:
            return _bounded_label(f"How should I resolve the conflict with {named_blockers}?", 1000)
        if conflict_time:
            return f"How should I resolve the scheduling conflict during {conflict_time}?"
        return "How should I resolve the scheduling conflict?"

    @staticmethod
    def _decision_reason(conflicts: list[Conflict]) -> str:
        labels = list(dict.fromkeys(
            blocker.label
            or (blocker.entity_ref.title if blocker.entity_ref is not None else None)
            for conflict in conflicts
            for blocker in conflict.blockers
            if blocker.label or (blocker.entity_ref is not None and blocker.entity_ref.title)
        ))
        if labels:
            named = ", ".join(f"“{label}”" for label in labels[:3])
            return _bounded_label(
                f"The requested time overlaps or violates {named}. The available scheduling evidence does not authorize silently changing every affected commitment.",
                2000,
            )
        return (
            "The requested time conflicts with existing scheduling evidence, and the available "
            "priority and protection information does not authorize a silent change."
        )

    @staticmethod
    def _decision_conflict_details(conflicts: list[Conflict]) -> list[DecisionConflictDetail]:
        details: list[DecisionConflictDetail] = []
        for conflict in conflicts:
            if conflict.requested_start_at is None or conflict.requested_end_at is None:
                continue
            for blocker in conflict.blockers:
                blocker_start = blocker.start_at
                blocker_end = blocker.end_at
                if blocker_start is None or blocker_end is None:
                    interval = conflict.overlap_intervals[0] if conflict.overlap_intervals else None
                    if interval is None:
                        continue
                    blocker_start, blocker_end = interval.start_at, interval.end_at
                blocker_title = (
                    blocker.label
                    or (blocker.entity_ref.title if blocker.entity_ref is not None else None)
                    or "Existing scheduling rule"
                )
                fixed_reason = (
                    "This commitment is protected and cannot be moved automatically."
                    if blocker.protection == "protected"
                    else "This commitment is pinned and cannot be moved automatically."
                    if blocker.protection == "pinned"
                    else "This scheduling rule is fixed."
                    if blocker.movability == "fixed"
                    else None
                )
                decision_reason = (
                    "Both commitments have the same authoritative priority, so neither one is moved without your choice."
                    if conflict.policy_recommendation == "ask_user"
                    else "The requested time must be changed to satisfy this fixed scheduling rule."
                    if conflict.policy_recommendation == "move_incoming"
                    else None
                )
                recommended_action = (
                    "move_existing"
                    if conflict.policy_recommendation == "move_blocker"
                    else "preserve_existing"
                    if conflict.policy_recommendation == "move_incoming"
                    else None
                )
                details.append(DecisionConflictDetail(
                    date=conflict.requested_start_at.date().isoformat(),
                    incoming_task_title=conflict.incoming_label or "Requested scheduled work",
                    incoming_priority=conflict.incoming_priority,
                    requested_start_at=conflict.requested_start_at,
                    requested_end_at=conflict.requested_end_at,
                    blocker_task_title=blocker_title,
                    blocker_priority=blocker.priority,
                    blocker_start_at=blocker_start,
                    blocker_end_at=blocker_end,
                    fixed_reason=fixed_reason,
                    decision_reason=decision_reason,
                    recommended_action=recommended_action,
                ))
        return details[:128]

    @staticmethod
    def _constraint_applies(constraint: Constraint, block: ProjectedCalendarBlock) -> bool:
        scope = constraint.scope
        if scope.kind == "goal":
            return True
        if scope.kind == "operation_set":
            return block.source_operation_id in scope.operation_ids
        identities = {ref.entity_id for ref in scope.entity_refs}
        candidates = {
            block.ref.entity_id,
            block.task_ref.entity_id,
            block.workspace_ref.entity_id,
            block.series_ref.entity_id if block.series_ref else "",
            block.occurrence_ref.entity_id if block.occurrence_ref else "",
        }
        return bool(identities & candidates)

    def _has_matching_current_exact_time(
        self,
        block: ProjectedCalendarBlock,
    ) -> bool:
        """Whether this operation carries a newer explicit exact-time command."""

        for constraint in self._all_constraints:
            if (
                constraint.kind != "exact_time"
                or constraint.strength != "hard"
                or constraint.superseded_by is not None
                or not self._constraint_applies(constraint, block)
            ):
                continue
            expected_end = constraint.value.start_at + timedelta(
                minutes=constraint.value.duration_minutes
            )
            if (block.start_at, block.end_at) == (
                constraint.value.start_at,
                expected_end,
            ):
                return True
        return False

    @staticmethod
    def _blocks_for_scope(scope: object, blocks: list[ProjectedCalendarBlock]) -> list[ProjectedCalendarBlock]:
        kind = getattr(scope, "kind", None)
        if kind == "goal":
            return blocks
        operation_ids = set(getattr(scope, "operation_ids", []))
        entity_ids = {item.entity_id for item in getattr(scope, "entity_refs", [])}
        return [
            block for block in blocks
            if block.source_operation_id in operation_ids
            or bool(entity_ids & {
                block.ref.entity_id,
                block.task_ref.entity_id,
                block.workspace_ref.entity_id,
                block.series_ref.entity_id if block.series_ref else "",
                block.occurrence_ref.entity_id if block.occurrence_ref else "",
            })
        ]

    def _constraint_violation_interval(
        self,
        constraint: Constraint,
        proposed: ProjectedCalendarBlock,
        blocks: list[ProjectedCalendarBlock],
    ) -> tuple[datetime, datetime] | None:
        value = constraint.value
        if constraint.kind == "temporal_window":
            overlap = _overlap(proposed.start_at, proposed.end_at, value.start_at, value.end_at)
            if value.disposition == "forbidden":
                return overlap
            if proposed.start_at < value.start_at or proposed.end_at > value.end_at:
                return proposed.start_at, proposed.end_at
            return None
        if constraint.kind == "exact_time":
            expected_end = value.start_at + timedelta(minutes=value.duration_minutes)
            return None if (proposed.start_at, proposed.end_at) == (value.start_at, expected_end) else (proposed.start_at, proposed.end_at)
        if constraint.kind == "minimum_gap":
            scoped = self._blocks_for_scope(constraint.scope, blocks)
            if constraint.object_scope is not None:
                scoped += self._blocks_for_scope(constraint.object_scope, blocks)
            for other in scoped:
                if other.ref.entity_id == proposed.ref.entity_id:
                    continue
                gap = min(abs((proposed.start_at - other.end_at).total_seconds()), abs((other.start_at - proposed.end_at).total_seconds())) / 60
                if _overlap(proposed.start_at, proposed.end_at, other.start_at, other.end_at) or gap < value.minutes:
                    return proposed.start_at, proposed.end_at
            return None
        if constraint.kind == "cardinality":
            count = len(self._blocks_for_scope(constraint.scope, blocks))
            return None if value.minimum <= count <= value.maximum else (proposed.start_at, proposed.end_at)
        if constraint.kind == "ordering":
            subjects = self._blocks_for_scope(constraint.scope, blocks)
            objects = self._blocks_for_scope(constraint.object_scope, blocks)
            if not subjects or not objects:
                return proposed.start_at, proposed.end_at
            if value.relation == "before":
                valid = max(item.end_at for item in subjects) <= min(item.start_at for item in objects)
            elif value.relation == "after":
                valid = min(item.start_at for item in subjects) >= max(item.end_at for item in objects)
            else:
                ordered = sorted([*subjects, *objects], key=lambda item: item.start_at)
                valid = all(left.end_at == right.start_at for left, right in zip(ordered, ordered[1:]))
            return None if valid else (proposed.start_at, proposed.end_at)
        if constraint.kind == "same_workspace":
            scoped = [*self._blocks_for_scope(constraint.scope, blocks), *self._blocks_for_scope(constraint.object_scope, blocks)]
            return None if scoped and len({item.workspace_ref.entity_id for item in scoped}) == 1 else (proposed.start_at, proposed.end_at)
        if constraint.kind == "preserve":
            # Preserve constraints require a baseline comparison. Fail closed when the
            # addressed identity is absent from either side; other properties are
            # already enforced by final-world validation and ChangeSet conservation.
            if self._base_world is None:
                return proposed.start_at, proposed.end_at
            baseline_ids = set(self._base_world.tasks) | set(self._base_world.calendar_blocks) | set(self._base_world.recurrence_series)
            addressed = {ref.entity_id for ref in constraint.scope.entity_refs}
            return None if addressed.issubset(baseline_ids) else (proposed.start_at, proposed.end_at)
        return proposed.start_at, proposed.end_at

    def _stored_constraint_conflicts(
        self,
        change_set: ChangeSet,
        incoming: list[ProjectedCalendarBlock],
        world: SimulationWorld,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        operation_by_id = {item.operation_id: item for item in change_set.operations}
        for stored in sorted(world.scheduling_constraints.values(), key=lambda item: item.constraint_id):
            if not stored.active or not stored.hard:
                continue
            for proposed in incoming:
                if self._has_matching_current_exact_time(proposed):
                    continue
                if stored.effective_from and proposed.end_at <= stored.effective_from:
                    continue
                if stored.effective_until and proposed.start_at >= stored.effective_until:
                    continue
                if not self._stored_constraint_applies(stored, proposed):
                    continue
                interval = self._stored_constraint_violation(stored, proposed)
                if interval is not None:
                    conflicts.append(
                        self._stored_constraint_conflict(
                            change_set,
                            proposed,
                            stored,
                            interval,
                            world,
                            operation_by_id,
                        )
                    )
        return conflicts

    @staticmethod
    def _stored_constraint_applies(
        stored: ProjectedConstraint,
        proposed: ProjectedCalendarBlock,
    ) -> bool:
        target_type = str(stored.target_type or "global").strip().lower()
        target_id = str(stored.target_id or "").strip()
        if target_type == "global":
            return True
        if target_type == "task":
            return target_id in {
                proposed.task_ref.entity_id,
                proposed.task_ref.entity_id.split(":")[-1],
            }
        if target_type in {"category", "workspace"}:
            return target_id in {
                proposed.workspace_ref.entity_id,
                proposed.workspace_ref.entity_id.split(":")[-1],
            }
        # Unknown active hard-policy scopes are not safe to ignore. Apply the
        # policy globally so its violation handler can fail closed.
        return True

    @staticmethod
    def _stored_constraint_violation(
        stored: ProjectedConstraint,
        proposed: ProjectedCalendarBlock,
    ) -> tuple[datetime, datetime] | None:
        if (
            stored.kind not in {"forbidden_window", "unavailable_window", "temporal_window"}
            or not isinstance(stored.value, dict)
        ):
            # Unknown active hard policies fail closed only inside their scope.
            return proposed.start_at, proposed.end_at
        value = stored.value
        start_raw = (
            value.get("startAt")
            or value.get("start_at")
            or value.get("start")
            or value.get("from")
        )
        end_raw = (
            value.get("endAt")
            or value.get("end_at")
            or value.get("end")
            or value.get("until")
            or value.get("to")
        )
        start_at: datetime | None = None
        end_at: datetime | None = None
        if start_raw and end_raw:
            try:
                start_at = datetime.fromisoformat(str(start_raw))
                end_at = datetime.fromisoformat(str(end_raw))
            except ValueError:
                start_at = None
                end_at = None
            if start_at is not None and end_at is not None:
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=proposed.start_at.tzinfo)
                if end_at.tzinfo is None:
                    end_at = end_at.replace(tzinfo=proposed.end_at.tzinfo)
        if start_at is not None and end_at is not None and end_at > start_at:
            if (
                stored.kind == "temporal_window"
                and str(value.get("disposition") or "").lower() == "allowed"
            ):
                return (
                    None
                    if proposed.start_at >= start_at and proposed.end_at <= end_at
                    else (proposed.start_at, proposed.end_at)
                )
            return _overlap(proposed.start_at, proposed.end_at, start_at, end_at)
        recurring_overlap = _recurring_window_overlap(value, proposed)
        if _parse_local_time(
            value.get("startTime") or value.get("start_time")
        ) is not None and _parse_local_time(
            value.get("endTime") or value.get("end_time")
        ) is not None:
            if (
                stored.kind == "temporal_window"
                and str(value.get("disposition") or "").lower() == "allowed"
            ):
                return (
                    None
                    if recurring_overlap == (proposed.start_at, proposed.end_at)
                    else (proposed.start_at, proposed.end_at)
                )
            return recurring_overlap
        return proposed.start_at, proposed.end_at

    def _stored_constraint_conflict(self, change_set, incoming, stored, interval, world, operation_by_id) -> Conflict:
        alternative = self._move_incoming_alternative(change_set, incoming, world, operation_by_id, extra_occupied=(interval,))
        alternatives = [alternative] if alternative else [ConflictAlternative(
            alternative_id=_stable_id("alternative", stored.constraint_id, "unavailable"),
            summary="No feasible placement satisfies the persisted hard constraint.",
            feasibility="unavailable",
            unavailable_reason="hard_constraint",
        )]
        return Conflict(
            conflict_id=_stable_id("conflict", change_set.change_set_id, str(change_set.revision), incoming.source_operation_id or "unknown", stored.constraint_id),
            kind="constraint_violation",
            incoming_operation_ids=[incoming.source_operation_id],
            incoming_label=self._calendar_title(incoming, world),
            incoming_priority=(incoming.priority if incoming.priority in {"low", "medium", "high"} else "unknown"),
            requested_start_at=incoming.start_at,
            requested_end_at=incoming.end_at,
            blockers=[BlockerEvidence(
                blocker_id=stored.constraint_id,
                entity_ref=EntityRef(
                    kind="scheduling_constraint",
                    entity_id=(
                        stored.constraint_id
                        if stored.constraint_id.startswith("scheduling_constraint:")
                        else f"scheduling_constraint:{stored.constraint_id}"
                    ),
                    title=self._stored_constraint_label(stored),
                ),
                label=self._stored_constraint_label(stored),
                start_at=interval[0],
                end_at=interval[1],
                source="preference",
                fingerprint=_fingerprint(vars(stored)),
                observed_at=self._snapshot.captured_at,
                freshness_boundary=self._snapshot.freshness_boundary,
                anchoring="anchored", movability="fixed", protection="protected",
            )],
            overlap_intervals=[OverlapInterval(
                start_at=interval[0], end_at=interval[1], timezone=str(interval[0].tzinfo),
                incoming_operation_ids=[incoming.source_operation_id],
            )],
            policy_recommendation="move_incoming" if alternative else "revise_plan",
            resolution_disposition="model_repairable" if alternative else "unresolvable",
            confidence=1.0,
            alternatives=alternatives,
        )

    def _hard_preference_conflicts(self, change_set, incoming, world, operation_by_id) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for preference in self._preferences:
            if preference.domain != "scheduling" or preference.strength != "hard" or not isinstance(preference.value, dict):
                continue
            preference_id = preference.ref.entity_id.removeprefix("preference:")
            if preference_id in self._represented_preference_ids:
                continue
            if preference.value.get("kind") != "time_window":
                stored = ProjectedConstraint(
                    constraint_id=preference.ref.entity_id,
                    kind=str(
                        preference.value.get("rule")
                        or preference.value.get("constraintType")
                        or "hard_preference"
                    ),
                    hard=True,
                    value=preference.value,
                    source="preference",
                    target_type=preference.scope,
                    target_id=preference.scope_id,
                )
                for proposed in incoming:
                    if not self._stored_constraint_applies(stored, proposed):
                        continue
                    interval = self._stored_constraint_violation(stored, proposed)
                    if interval is None:
                        continue
                    stored = ProjectedConstraint(
                        constraint_id=preference.ref.entity_id,
                        kind=stored.kind,
                        hard=True,
                        value=preference.value,
                        source="preference",
                        target_type=preference.scope,
                        target_id=preference.scope_id,
                    )
                    conflicts.append(
                        self._stored_constraint_conflict(
                            change_set,
                            proposed,
                            stored,
                            interval,
                            world,
                            operation_by_id,
                        )
                    )
                continue
            try:
                allowed_start = time.fromisoformat(preference.value["startLocal"])
                allowed_end = time.fromisoformat(preference.value["endLocal"])
            except (KeyError, ValueError):
                continue
            for proposed in incoming:
                if self._has_matching_current_exact_time(proposed):
                    continue
                if preference.effective_from and proposed.end_at <= preference.effective_from:
                    continue
                if preference.effective_until and proposed.start_at >= preference.effective_until:
                    continue
                start_at = datetime.combine(proposed.start_at.date(), allowed_start, tzinfo=proposed.start_at.tzinfo)
                end_at = datetime.combine(proposed.start_at.date(), allowed_end, tzinfo=proposed.start_at.tzinfo)
                if proposed.start_at >= start_at and proposed.end_at <= end_at:
                    continue
                stored = ProjectedConstraint(
                    constraint_id=preference.ref.entity_id,
                    kind="allowed_window",
                    hard=True,
                    value=preference.value,
                    source="preference",
                )
                conflicts.append(self._stored_constraint_conflict(change_set, proposed, stored, (proposed.start_at, proposed.end_at), world, operation_by_id))
        return conflicts

    def _violates_hard_preferences(self, block: ProjectedCalendarBlock) -> bool:
        if self._has_matching_current_exact_time(block):
            return False
        for preference in self._preferences:
            if preference.domain != "scheduling" or preference.strength != "hard" or not isinstance(preference.value, dict):
                continue
            if (
                preference.ref.entity_id.removeprefix("preference:")
                in self._represented_preference_ids
            ):
                continue
            if preference.value.get("kind") != "time_window":
                stored = ProjectedConstraint(
                    constraint_id=preference.ref.entity_id,
                    kind=str(
                        preference.value.get("rule")
                        or preference.value.get("constraintType")
                        or "hard_preference"
                    ),
                    hard=True,
                    value=preference.value,
                    source="preference",
                    target_type=preference.scope,
                    target_id=preference.scope_id,
                )
                if not self._stored_constraint_applies(stored, block):
                    continue
                if self._stored_constraint_violation(stored, block) is not None:
                    return True
                continue
            try:
                allowed_start = time.fromisoformat(preference.value["startLocal"])
                allowed_end = time.fromisoformat(preference.value["endLocal"])
            except (KeyError, ValueError):
                return True
            start_at = datetime.combine(block.start_at.date(), allowed_start, tzinfo=block.start_at.tzinfo)
            end_at = datetime.combine(block.start_at.date(), allowed_end, tzinfo=block.start_at.tzinfo)
            if block.start_at < start_at or block.end_at > end_at:
                return True
        return False

    def _provisional_proof(self, delta: ChangeSetRevisionDelta, method: str) -> AlternativeFeasibilityProof:
        return AlternativeFeasibilityProof(
            snapshot_fingerprint=self._snapshot.fingerprint,
            delta_fingerprint=_fingerprint(delta.model_dump(mode="json", by_alias=True)),
            method=method,
        )

    def _complete_decision_alternatives(
        self,
        change_set: ChangeSet,
        conflicts: list[Conflict],
        conflict_ids: list[str],
    ) -> list[ConflictAlternative]:
        candidate_lists = [
            [item for item in conflict.alternatives if item.change_set_delta is not None]
            for conflict in conflicts
        ]
        if any(not items for items in candidate_lists):
            return []
        complete: list[ConflictAlternative] = []
        directional_combinations: list[tuple[ConflictAlternative, ...]] = []
        for relation in ("after_incoming", "before_incoming"):
            selected = [
                next(
                    (
                        alternative
                        for alternative in candidates
                        if alternative.placement_relation == relation
                    ),
                    None,
                )
                for candidates in candidate_lists
            ]
            if all(item is not None for item in selected):
                directional_combinations.append(tuple(selected))  # type: ignore[arg-type]
        seen_complete_deltas: set[str] = set()
        combinations = (
            directional_combinations
            if len(directional_combinations) == 2
            else chain(directional_combinations, product(*candidate_lists))
        )
        for combination in combinations:
            edits = []
            touched: set[tuple[str, str]] = set()
            valid = True
            affected: dict[tuple[str, str], EntityRef] = {}
            for alternative in combination:
                for edit in alternative.change_set_delta.edits:
                    target = (
                        edit.kind,
                        getattr(edit, "operation_id", None)
                        or getattr(edit, "constraint_id", None)
                        or edit.edit_id,
                    )
                    if target in touched:
                        valid = False
                        break
                    touched.add(target)
                    edits.append(edit)
                if not valid:
                    break
                for entity in alternative.affected_entities:
                    affected[(entity.kind, entity.entity_id)] = entity
            if not valid:
                continue
            try:
                delta = ChangeSetRevisionDelta(
                    expected_change_set_revision=change_set.revision,
                    edits=edits,
                )
            except ValueError:
                continue
            proof = self._prove_delta(change_set, delta, conflict_ids)
            if proof is None:
                continue
            if proof.delta_fingerprint in seen_complete_deltas:
                continue
            seen_complete_deltas.add(proof.delta_fingerprint)
            alternative_id = _stable_id("alternative", proof.delta_fingerprint)
            placement_relations = {
                item.placement_relation
                for item in combination
                if item.placement_relation is not None
            }
            placement_relation = (
                next(iter(placement_relations))
                if len(placement_relations) == 1
                else None
            )
            component_summaries = list(dict.fromkeys(
                item.summary.rstrip(".") for item in combination
            ))
            complete.append(ConflictAlternative(
                alternative_id=alternative_id,
                summary=(
                    "Move the existing conflicting commitments after the incoming occurrences."
                    if placement_relation == "after_incoming"
                    else "Move the existing conflicting commitments before the incoming occurrences."
                    if placement_relation == "before_incoming"
                    else _bounded_label("; ".join(component_summaries) + ".")
                ),
                feasibility="feasible",
                affected_entities=list(affected.values()),
                tradeoff="moves_existing" if any(item.tradeoff == "moves_existing" for item in combination) else combination[0].tradeoff,
                placement_relation=placement_relation,
                change_set_delta=delta,
                feasibility_proof=proof,
            ))
            if len(complete) >= 6:
                break
        return complete

    def _prove_delta(
        self,
        change_set: ChangeSet,
        delta: ChangeSetRevisionDelta,
        conflict_ids: list[str],
    ) -> AlternativeFeasibilityProof | None:
        if self._base_world is not None:
            try:
                candidate = apply_change_set_delta(
                    change_set,
                    delta,
                    idempotency_key=f"proof:{delta.expected_change_set_revision}",
                    correlation_id=change_set.correlation_id,
                    causation_id=change_set.causation_id,
                )
                projected = ChangeSetProjector().project(candidate, self._base_world).world
            except Exception:
                return None
            incoming = [item for item in projected.calendar_blocks.values() if item.source_operation_id]
            for left_index, left in enumerate(incoming):
                for right in list(projected.calendar_blocks.values()):
                    if left.ref.entity_id == right.ref.entity_id:
                        continue
                    if right.source_operation_id and right in incoming and incoming.index(right) < left_index:
                        continue
                    if _overlap(left.start_at, left.end_at, right.start_at, right.end_at):
                        return None
                if self._violates_hard_preferences(left):
                    return None
                for constraint in self._all_constraints:
                    if constraint.strength == "hard" and constraint.superseded_by is None and self._constraint_applies(constraint, left):
                        if self._constraint_violation_interval(constraint, left, list(projected.calendar_blocks.values())) is not None:
                            return None
            if self._stored_constraint_conflicts(candidate, incoming, projected):
                return None
        proof = self._provisional_proof(delta, "cp_sat_projection")
        return proof.model_copy(update={"checked_conflict_ids": conflict_ids})

    def _calendar_conflict(
        self,
        change_set: ChangeSet,
        incoming: ProjectedCalendarBlock,
        blockers: list[ProjectedCalendarBlock],
        world: SimulationWorld,
        operation_by_id: dict[str, object],
        *,
        incoming_anchored: bool,
    ) -> Conflict:
        evidence = [self._blocker_evidence(item, world) for item in blockers]
        intervals = []
        for blocker in blockers:
            interval = _overlap(incoming.start_at, incoming.end_at, blocker.start_at, blocker.end_at)
            if interval is None:
                continue
            intervals.append(
                OverlapInterval(
                    start_at=interval[0], end_at=interval[1], timezone=str(interval[0].tzinfo),
                    incoming_operation_ids=list(dict.fromkeys([
                        incoming.source_operation_id,
                        *([blocker.source_operation_id] if blocker.source_operation_id else []),
                    ])),
                    blocker_refs=[blocker.ref],
                )
            )
        fixed = any(item.movability == "fixed" for item in blockers)
        all_flexible = all(item.movability == "flexible" for item in blockers)
        priorities = {"low": 0, "medium": 1, "high": 2}
        strongest = max((priorities.get(item.priority, 1) for item in blockers), default=1)
        incoming_priority = priorities.get(incoming.priority, 1)
        priority_trusted = incoming.priority_authoritative and all(item.priority_authoritative for item in blockers)
        if fixed or (priority_trusted and incoming_priority < strongest and not incoming_anchored):
            recommendation = "move_incoming"
            disposition = "model_repairable"
            confidence = 1.0 if fixed else 0.9
        elif all_flexible and (incoming_anchored or (priority_trusted and incoming_priority > strongest)) and all(
            not item.source_operation_id for item in blockers
        ):
            recommendation = "move_blocker"
            disposition = "model_repairable"
            confidence = 0.9
        else:
            recommendation = "ask_user"
            disposition = "needs_user_decision"
            confidence = 0.5
        alternatives = []
        incoming_alt = self._move_incoming_alternative(
            change_set, incoming, world, operation_by_id, extra_occupied=()
        )
        if incoming_alt is not None:
            alternatives.append(incoming_alt)
        if not fixed:
            if incoming.is_recurring and all(item.occurrence_ref is not None for item in blockers):
                for relation in ("after_incoming", "before_incoming"):
                    blocker_alt = self._move_blockers_alternative(
                        change_set,
                        incoming,
                        blockers,
                        world,
                        placement_relation=relation,
                    )
                    if blocker_alt is not None:
                        alternatives.append(blocker_alt)
            else:
                blocker_alt = self._move_blockers_alternative(
                    change_set, incoming, blockers, world
                )
                if blocker_alt is not None:
                    alternatives.append(blocker_alt)
        if fixed:
            split_alt = self._split_incoming_alternative(
                change_set, incoming, blockers, world, operation_by_id
            )
            if split_alt is not None:
                alternatives.append(split_alt)
        return Conflict(
            conflict_id=_stable_id(
                "conflict", change_set.change_set_id, str(change_set.revision),
                incoming.source_operation_id or "unknown", *(item.ref.entity_id for item in blockers),
            ),
            kind="protected_commitment" if fixed else "calendar_overlap",
            incoming_operation_ids=list(dict.fromkeys([
                incoming.source_operation_id,
                *(item.source_operation_id for item in blockers if item.source_operation_id),
            ])),
            incoming_label=self._calendar_title(incoming, world),
            incoming_priority=(incoming.priority if incoming.priority in {"low", "medium", "high"} else "unknown"),
            requested_start_at=incoming.start_at,
            requested_end_at=incoming.end_at,
            blockers=evidence,
            overlap_intervals=intervals,
            policy_recommendation=recommendation,
            resolution_disposition=disposition,
            confidence=confidence,
            alternatives=alternatives,
        )

    def _constraint_conflict(
        self,
        change_set: ChangeSet,
        incoming: ProjectedCalendarBlock,
        constraint: Constraint,
        interval: tuple[datetime, datetime],
        world: SimulationWorld,
        operation_by_id: dict[str, object],
        source: str,
    ) -> Conflict:
        alternative = self._move_incoming_alternative(
            change_set,
            incoming,
            world,
            operation_by_id,
            extra_occupied=(interval,),
        )
        alternatives = [alternative] if alternative is not None else [
            ConflictAlternative(
                alternative_id=_stable_id("alternative", constraint.constraint_id, "unavailable"),
                summary="No feasible placement was found within the bounded horizon.",
                feasibility="unavailable",
                unavailable_reason="no_feasible_slot",
            )
        ]
        return Conflict(
            conflict_id=_stable_id(
                "conflict", change_set.change_set_id, str(change_set.revision),
                incoming.source_operation_id or "unknown", constraint.constraint_id,
            ),
            kind="constraint_violation",
            incoming_operation_ids=[incoming.source_operation_id],
            incoming_label=self._calendar_title(incoming, world),
            incoming_priority=(incoming.priority if incoming.priority in {"low", "medium", "high"} else "unknown"),
            requested_start_at=incoming.start_at,
            requested_end_at=incoming.end_at,
            blockers=[
                BlockerEvidence(
                    blocker_id=constraint.constraint_id,
                    entity_ref=EntityRef(
                        kind="scheduling_constraint",
                        entity_id=(
                            constraint.constraint_id
                            if constraint.constraint_id.startswith("scheduling_constraint:")
                            else f"scheduling_constraint:{constraint.constraint_id}"
                        ),
                        title=self._constraint_label(constraint),
                    ),
                    label=self._constraint_label(constraint),
                    start_at=interval[0],
                    end_at=interval[1],
                    source=source,
                    fingerprint=_fingerprint(constraint.model_dump(mode="json", by_alias=True)),
                    observed_at=self._snapshot.captured_at,
                    freshness_boundary=self._snapshot.freshness_boundary,
                    anchoring="anchored", movability="fixed", protection="protected",
                )
            ],
            overlap_intervals=[
                OverlapInterval(
                    start_at=interval[0], end_at=interval[1], timezone=str(interval[0].tzinfo),
                    incoming_operation_ids=[incoming.source_operation_id],
                )
            ],
            policy_recommendation="move_incoming" if alternative else "revise_plan",
            resolution_disposition="model_repairable",
            confidence=1.0,
            alternatives=alternatives,
        )

    @staticmethod
    def _calendar_title(item: ProjectedCalendarBlock, world: SimulationWorld) -> str:
        task = world.tasks.get(item.task_ref.entity_id)
        return (
            task.title
            if task is not None and task.title.strip()
            else item.task_ref.title
            or item.ref.title
            or "Scheduled work"
        )

    @staticmethod
    def _stored_constraint_label(stored: ProjectedConstraint) -> str:
        if stored.description and stored.description.strip():
            return stored.description.strip()
        if isinstance(stored.value, dict):
            source_text = stored.value.get("sourceText") or stored.value.get("preferenceText")
            if isinstance(source_text, str) and source_text.strip():
                return source_text.strip()
        return {
            "forbidden_window": "Saved unavailable-time rule",
            "unavailable_window": "Saved unavailable-time rule",
            "temporal_window": "Saved scheduling window",
            "allowed_window": "Saved allowed-hours rule",
        }.get(stored.kind, "Saved scheduling rule")

    @staticmethod
    def _constraint_label(constraint: Constraint) -> str:
        source_text = constraint.provenance.source_text
        if source_text and source_text.strip():
            return source_text.strip()
        return {
            "exact_time": "Requested exact time",
            "temporal_window": "Required scheduling window",
            "preserve": "Preserve existing commitment",
        }.get(constraint.kind, "Required scheduling rule")

    def _blocker_evidence(
        self,
        item: ProjectedCalendarBlock,
        world: SimulationWorld,
    ) -> BlockerEvidence:
        scope = "occurrence" if item.occurrence_ref else "none"
        label = self._calendar_title(item, world)
        entity_ref = (item.occurrence_ref or item.ref).model_copy(update={"title": label})
        return BlockerEvidence(
            blocker_id=_stable_id("blocker", item.ref.entity_id),
            entity_ref=entity_ref,
            label=label,
            start_at=item.start_at,
            end_at=item.end_at,
            source="projection" if item.source_operation_id else ("recurrence" if item.is_recurring else "calendar"),
            fingerprint=_fingerprint({
                "ref": item.ref.model_dump(mode="json", by_alias=True),
                "startAt": item.start_at.isoformat(), "endAt": item.end_at.isoformat(),
                "priority": item.priority, "pinned": item.is_pinned, "protected": item.is_protected,
            }),
            observed_at=self._snapshot.captured_at,
            freshness_boundary=self._snapshot.freshness_boundary,
            priority=item.priority if item.priority in {"low", "medium", "high"} else "unknown",
            priority_source="authoritative" if item.priority_authoritative else "proposed",
            deadline_at=item.deadline_at,
            anchoring=item.anchoring,
            movability=item.movability,
            protection="protected" if item.is_protected else "pinned" if item.is_pinned else "ordinary",
            recurrence_scope=scope,
            series_ref=item.series_ref,
            occurrence_ref=item.occurrence_ref,
        )

    def _move_incoming_alternative(
        self,
        change_set: ChangeSet,
        incoming: ProjectedCalendarBlock,
        world: SimulationWorld,
        operation_by_id: dict[str, object],
        *,
        extra_occupied: Iterable[tuple[datetime, datetime]],
    ) -> ConflictAlternative | None:
        operation = operation_by_id.get(incoming.source_operation_id or "")
        if operation is None or operation.kind not in {
            "calendar.create_block", "calendar.move_block", "calendar.resize_block"
        }:
            return None
        placement = self._solve_move(incoming, world, exclude={incoming.ref.entity_id}, extra_occupied=extra_occupied)
        if placement is None:
            return None
        replacement = operation.model_copy(update={"start_at": placement[0], "end_at": placement[1]})
        delta = ChangeSetRevisionDelta(
            expected_change_set_revision=change_set.revision,
            edits=[
                ChangeSetOperationReplace(
                    edit_id=_stable_id("edit", incoming.source_operation_id or "unknown", placement[0].isoformat()),
                    operation_id=incoming.source_operation_id,
                    replacement=replacement,
                )
            ],
        )
        incoming_title = self._calendar_title(incoming, world)
        return ConflictAlternative(
            alternative_id=_stable_id("alternative", incoming.source_operation_id or "unknown", placement[0].isoformat()),
            summary=(
                f"Move “{incoming_title}” to "
                f"{_datetime_range_label(placement[0], placement[1])}."
            ),
            feasibility="feasible",
            affected_entities=[incoming.ref.model_copy(update={"title": incoming_title})],
            tradeoff="moves_incoming",
            change_set_delta=delta,
            feasibility_proof=self._provisional_proof(delta, "cp_sat_projection"),
        )

    def _move_blockers_alternative(
        self,
        change_set: ChangeSet,
        incoming: ProjectedCalendarBlock,
        blockers: list[ProjectedCalendarBlock],
        world: SimulationWorld,
        *,
        placement_relation: str | None = None,
    ) -> ConflictAlternative | None:
        edits = []
        affected = []
        move_labels: list[str] = []
        reserved: list[tuple[datetime, datetime]] = [(incoming.start_at, incoming.end_at)]
        for blocker in blockers:
            if blocker.source_operation_id or blocker.ref.kind == "temporary":
                return None
            placement = (
                self._solve_directional_move(
                    blocker,
                    incoming,
                    world,
                    relation=placement_relation,
                    extra_occupied=reserved,
                )
                if placement_relation is not None
                else self._solve_move(
                    blocker,
                    world,
                    exclude={blocker.ref.entity_id},
                    extra_occupied=reserved,
                )
            )
            if placement is None:
                return None
            operation_id = _stable_id("op", "resolve", blocker.ref.entity_id, placement[0].isoformat())
            operation = (
                RecurrenceUpdateOccurrenceOperation(
                    operation_id=operation_id,
                    occurrence_ref=blocker.occurrence_ref,
                    updates=RecurrenceOccurrencePatch(
                        start_at=placement[0], end_at=placement[1]
                    ),
                )
                if blocker.occurrence_ref is not None
                else CalendarMoveBlockOperation(
                    operation_id=operation_id,
                    block_ref=blocker.ref,
                    start_at=placement[0], end_at=placement[1],
                )
            )
            edits.append(
                ChangeSetOperationAdd(
                    edit_id=_stable_id("edit", operation_id),
                    operation=operation,
                )
            )
            blocker_title = self._calendar_title(blocker, world)
            affected.append(
                (blocker.occurrence_ref or blocker.ref).model_copy(
                    update={"title": blocker_title}
                )
            )
            move_labels.append(
                f"“{blocker_title}” to {_datetime_range_label(placement[0], placement[1])}"
            )
            reserved.append(placement)
        delta = ChangeSetRevisionDelta(expected_change_set_revision=change_set.revision, edits=edits)
        if len(move_labels) == 1:
            summary = f"Move {move_labels[0]}."
        elif placement_relation == "after_incoming":
            summary = f"Move {len(move_labels)} existing occurrences after the requested time."
        elif placement_relation == "before_incoming":
            summary = f"Move {len(move_labels)} existing occurrences before the requested time."
        else:
            summary = _bounded_label(
                f"Move {len(move_labels)} existing commitments: {', '.join(move_labels[:3])}"
                + (f", and {len(move_labels) - 3} more" if len(move_labels) > 3 else "")
                + "."
            )
        return ConflictAlternative(
            alternative_id=_stable_id(
                "alternative",
                "move-blockers",
                placement_relation or "bounded-slot",
                *(item.entity_id for item in affected),
            ),
            summary=summary,
            feasibility="feasible", affected_entities=affected,
            tradeoff="moves_existing", placement_relation=placement_relation,
            change_set_delta=delta,
            feasibility_proof=self._provisional_proof(delta, "cp_sat_projection"),
        )

    @staticmethod
    def _solve_directional_move(
        target: ProjectedCalendarBlock,
        incoming: ProjectedCalendarBlock,
        world: SimulationWorld,
        *,
        relation: str,
        extra_occupied: Iterable[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime] | None:
        """Place a flexible blocker next to an incoming occurrence with its saved gap."""

        duration = target.end_at - target.start_at
        if duration <= timedelta(0) or relation not in {"before_incoming", "after_incoming"}:
            return None
        gap = timedelta(minutes=max(0, target.gap_minutes))
        accepted = [
            item
            for item in world.calendar_blocks.values()
            if item.ref.entity_id != target.ref.entity_id
            and item.source_operation_id is None
        ]
        occupied = [(item.start_at, item.end_at) for item in accepted]
        occupied.extend(extra_occupied)
        day_start = datetime.combine(
            incoming.start_at.date(), time.min, tzinfo=incoming.start_at.tzinfo
        )
        day_end = day_start + timedelta(days=1)
        step = timedelta(minutes=5)
        if relation == "before_incoming":
            candidate_end = incoming.start_at - gap
            candidate_start = candidate_end - duration
            while candidate_start >= day_start:
                buffered = (candidate_start - gap, candidate_end + gap)
                if not any(_overlap(buffered[0], buffered[1], start, end) for start, end in occupied):
                    return candidate_start, candidate_end
                candidate_start -= step
                candidate_end -= step
            return None
        search_start = max(incoming.end_at + gap, target.end_at)
        if search_start >= day_end:
            return None
        solver_occupied = [
            OccupiedInterval(
                block_id=index + 1,
                start_at=item.start_at,
                end_at=item.end_at,
                source_type="calendar",
            )
            for index, item in enumerate(accepted)
        ]
        solver_occupied.extend(
            OccupiedInterval(
                block_id=-(index + 1),
                start_at=start,
                end_at=end,
                source_type="draft",
                report_conflict=False,
            )
            for index, (start, end) in enumerate(extra_occupied)
        )
        result = solve_schedule_draft(
            tasks=[
                SolverTask(
                    task_id=1,
                    duration_minutes=int(duration.total_seconds() // 60),
                    gap_before_minutes=max(0, target.gap_minutes),
                    gap_after_minutes=max(0, target.gap_minutes),
                )
            ],
            window_start=search_start,
            window_end=day_end,
            occupied=solver_occupied,
            slot_minutes=5,
        )
        if not result.placements:
            return None
        placement = result.placements[0]
        return placement.start_at, placement.end_at

    def _split_incoming_alternative(
        self,
        change_set: ChangeSet,
        incoming: ProjectedCalendarBlock,
        blockers: list[ProjectedCalendarBlock],
        world: SimulationWorld,
        operation_by_id: dict[str, object],
    ) -> ConflictAlternative | None:
        if len(blockers) != 1:
            return None
        operation = operation_by_id.get(incoming.source_operation_id or "")
        blocker = blockers[0]
        if operation is None or operation.kind != "calendar.create_block":
            return None
        if not (
            incoming.start_at < blocker.start_at < blocker.end_at < incoming.end_at
        ):
            return None
        total_minutes = int((incoming.end_at - incoming.start_at).total_seconds() // 60)
        first_minutes = int((blocker.start_at - incoming.start_at).total_seconds() // 60)
        remaining_minutes = total_minutes - first_minutes
        if first_minutes <= 0 or remaining_minutes <= 0:
            return None
        second_end = blocker.end_at + timedelta(minutes=remaining_minutes)
        segments = [
            CalendarSegment(start_at=incoming.start_at, end_at=blocker.start_at),
            CalendarSegment(start_at=blocker.end_at, end_at=second_end),
        ]
        for other in world.calendar_blocks.values():
            if other.ref.entity_id in {incoming.ref.entity_id, blocker.ref.entity_id}:
                continue
            if any(
                _overlap(segment.start_at, segment.end_at, other.start_at, other.end_at)
                for segment in segments
            ):
                return None
        split_id = _stable_id("op", "split", incoming.source_operation_id or "unknown", blocker.ref.entity_id)
        split = CalendarSplitBlockOperation(
            operation_id=split_id,
            depends_on=[incoming.source_operation_id],
            block_ref=incoming.ref,
            segments=segments,
        )
        return ConflictAlternative(
            alternative_id=_stable_id("alternative", split_id),
            summary="Split the flexible work around the fixed commitment.",
            feasibility="feasible",
            affected_entities=[incoming.ref, blocker.ref],
            tradeoff="splits_work",
            change_set_delta=ChangeSetRevisionDelta(
                expected_change_set_revision=change_set.revision,
                edits=[
                    ChangeSetOperationAdd(
                        edit_id=_stable_id("edit", split_id), operation=split
                    )
                ],
            ),
            feasibility_proof=self._provisional_proof(
                ChangeSetRevisionDelta(
                    expected_change_set_revision=change_set.revision,
                    edits=[ChangeSetOperationAdd(edit_id=_stable_id("edit", split_id), operation=split)],
                ),
                "projection",
            ),
        )

    def _solve_move(
        self,
        target: ProjectedCalendarBlock,
        world: SimulationWorld,
        *,
        exclude: set[str],
        extra_occupied: Iterable[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime] | None:
        day = target.start_at.date()
        start_local, end_local = time(6, 0), time(22, 0)
        for preference in self._preferences:
            if preference.domain != "scheduling" or preference.strength != "soft" or not isinstance(preference.value, dict):
                continue
            if preference.effective_from and target.start_at < preference.effective_from:
                continue
            if preference.effective_until and target.start_at >= preference.effective_until:
                continue
            if preference.value.get("kind") != "time_window":
                continue
            try:
                start_local = time.fromisoformat(preference.value["startLocal"])
                end_local = time.fromisoformat(preference.value["endLocal"])
            except (KeyError, ValueError):
                continue
            break
        occupied = [
            OccupiedInterval(index + 1, item.start_at, item.end_at)
            for index, item in enumerate(
                item for key, item in sorted(world.calendar_blocks.items()) if key not in exclude
            )
        ]
        next_id = len(occupied) + 1
        for start_at, end_at in extra_occupied:
            occupied.append(OccupiedInterval(next_id, start_at, end_at, source_type="constraint"))
            next_id += 1
        duration = max(1, int((target.end_at - target.start_at).total_seconds() // 60))
        for offset in range(2):
            candidate_day = day + timedelta(days=offset)
            window_start = datetime.combine(candidate_day, start_local, tzinfo=target.start_at.tzinfo)
            window_end = datetime.combine(candidate_day, end_local, tzinfo=target.start_at.tzinfo)
            if target.deadline_at is not None:
                window_end = min(window_end, target.deadline_at.astimezone(window_end.tzinfo))
            if window_end <= window_start:
                continue
            result = solve_schedule_draft(
                tasks=[SolverTask(task_id=1, duration_minutes=duration)],
                window_start=window_start, window_end=window_end,
                alternative_window_end=window_end,
                occupied=occupied, slot_minutes=15,
            )
            if result.placements:
                placement = result.placements[0]
                if placement.start_at >= window_start and placement.end_at <= window_end:
                    return placement.start_at, placement.end_at
        return None
