"""Server-owned authoring boundary for native draft-only ChangeSets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from sqlmodel import Session, select

from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import PreferenceLink, SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries
from app.services.agent_kernel.change_set_store import NativeChangeSetStore
from app.services.agent_kernel.contracts import (
    ChangeSet,
    DraftChangeSetCommand,
    DraftChangeSetResult,
    EntityRef,
    GoalState,
    OperationConservationReport,
    RecurrenceRule,
)
from app.services.agent_kernel.operation_conservation import (
    evaluate_operation_conservation,
    operation_fingerprint,
    verify_operation_round_trip,
)
from app.services.agent_kernel.change_set_deltas import apply_change_set_delta
from app.services.agent_kernel.simulation_store import SimulationResultNotFound, SimulationStore
from app.services.agent_kernel.state_store import GoalStateNotFound, GoalStateStore


class NativeChangeSetAuthoringError(RuntimeError):
    pass


class ChangeSetAuthorizationError(NativeChangeSetAuthoringError):
    pass


class ChangeSetConservationError(NativeChangeSetAuthoringError):
    pass


class ChangeSetAuthoringContextError(NativeChangeSetAuthoringError):
    pass


class ChangeSetSchedulingEvidenceError(NativeChangeSetAuthoringError):
    pass


class ChangeSetResolutionError(NativeChangeSetAuthoringError):
    pass


@dataclass(frozen=True)
class TrustedScheduleEvidence:
    operation_id: str
    operation_sha256: str
    source_ref: str

    @classmethod
    def from_operation(cls, operation: Any, *, source_ref: str) -> "TrustedScheduleEvidence":
        return cls(
            operation_id=operation.operation_id,
            operation_sha256=operation_fingerprint(operation).sha256,
            source_ref=source_ref,
        )


def trusted_schedule_evidence_from_goal(goal_state: GoalState) -> list[TrustedScheduleEvidence]:
    evidence: list[TrustedScheduleEvidence] = []
    for clause in goal_state.requested_operation_clauses():
        for operation in clause.expected_operations:
            if not operation.kind.startswith("calendar."):
                continue
            evidence.append(TrustedScheduleEvidence.from_operation(
                operation, source_ref=clause.provenance.source_ref
            ))
    return evidence


def validate_exact_schedule_evidence(
    change_set: ChangeSet,
    trusted_evidence: Iterable[TrustedScheduleEvidence] = (),
) -> None:
    """Exact placement must match server-owned structured evidence."""

    exact_kinds = {
        "calendar.create_block",
        "calendar.move_block",
        "calendar.resize_block",
        "calendar.split_block",
    }
    trusted = {
        (item.operation_id, item.operation_sha256) for item in trusted_evidence
    }
    for operation in change_set.operations:
        if operation.kind not in exact_kinds:
            continue
        identity = (operation.operation_id, operation_fingerprint(operation).sha256)
        if identity not in trusted:
            raise ChangeSetSchedulingEvidenceError(
                f"operation {operation.operation_id} requires matching trusted schedule evidence"
            )


def _local_id(entity_id: str, prefix: str) -> str:
    marker = f"{prefix}:"
    return entity_id[len(marker) :] if entity_id.startswith(marker) else entity_id


class EntityReferenceAuthorizer:
    """Resolve existing refs inside the current local authority before persistence."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _references(value: Any) -> Iterable[EntityRef]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="python", by_alias=False)
        if isinstance(value, dict):
            if value.get("kind") in {
                "task",
                "workspace",
                "calendar_block",
                "recurrence_series",
                "recurrence_occurrence",
                "preference",
                "scheduling_constraint",
                "preference_link",
                "temporary",
            } and "entity_id" in value:
                yield EntityRef.model_validate(value)
            for nested in value.values():
                yield from EntityReferenceAuthorizer._references(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from EntityReferenceAuthorizer._references(nested)

    def authorize(self, value: Any) -> None:
        if isinstance(value, ChangeSet):
            self._authorize_references([value.operations, value.constraints, value.assumptions])
            self._authorize_preconditions(value)
            self._validate_recurrence_updates(value)
            return
        self._authorize_references(value)

    def _authorize_references(self, value: Any) -> None:
        seen: set[tuple[str, str, int | None]] = set()
        for ref in self._references(value):
            if ref.kind == "temporary":
                continue
            identity = (ref.kind, ref.entity_id, ref.revision)
            if identity in seen:
                continue
            seen.add(identity)
            record = self._resolve(ref)
            if record is None:
                raise ChangeSetAuthorizationError(
                    f"{ref.kind} reference {ref.entity_id} is outside the current authority"
                )
            if ref.revision is not None:
                current_revision = getattr(record, "revision", None)
                if current_revision is None or int(current_revision) != ref.revision:
                    raise ChangeSetAuthorizationError(
                        f"{ref.kind} reference {ref.entity_id} has no matching revision {ref.revision}"
                    )

    def _authorize_preconditions(self, change_set: ChangeSet) -> None:
        for ref in change_set.preconditions.expected_entity_revisions:
            record = self._resolve(ref)
            current_revision = getattr(record, "revision", None) if record is not None else None
            if current_revision is None or int(current_revision) != ref.revision:
                raise ChangeSetAuthorizationError(
                    f"{ref.kind} reference {ref.entity_id} has no matching revision {ref.revision}"
                )
        for assertion in change_set.preconditions.assertions:
            record = self._resolve(assertion.entity_ref)
            if assertion.kind == "entity.absent":
                if record is not None:
                    raise ChangeSetAuthorizationError(
                        f"precondition {assertion.assertion_id} expected an absent entity"
                    )
                continue
            if record is None:
                raise ChangeSetAuthorizationError(
                    f"precondition {assertion.assertion_id} references a missing entity"
                )
            if assertion.kind == "entity.field_equals":
                if assertion.field.startswith("_") or not hasattr(record, assertion.field):
                    raise ChangeSetAuthorizationError(
                        f"precondition {assertion.assertion_id} names an unsupported field"
                    )
                actual = getattr(record, assertion.field)
                if actual != assertion.expected_value:
                    raise ChangeSetAuthorizationError(
                        f"precondition {assertion.assertion_id} field value does not match"
                    )

    def _validate_recurrence_updates(self, change_set: ChangeSet) -> None:
        for operation in change_set.operations:
            if operation.kind != "recurrence.update_series" or operation.series_ref.kind == "temporary":
                continue
            record = self._resolve(operation.series_ref)
            if record is None:
                continue
            patch = operation.updates
            values = {
                "frequency": record.frequency,
                "interval": record.interval,
                "weekdays": json.loads(record.days_of_week_json or "[]"),
                "month_days": json.loads(record.month_days_json or "[]"),
                "start_date": record.start_date,
                "end_date": record.end_date,
                "occurrence_count": record.occurrence_limit,
                "timezone": record.timezone,
            }
            values.update(
                patch.model_dump(
                    mode="python",
                    exclude_unset=True,
                    exclude_none=True,
                )
            )
            try:
                RecurrenceRule.model_validate(values)
            except Exception as error:
                raise ChangeSetAuthorizationError(
                    f"recurrence update {operation.operation_id} creates an invalid final rule"
                ) from error

    def _resolve(self, ref: EntityRef) -> Any | None:
        try:
            if ref.kind == "task":
                return self.session.get(StrategyNode, int(_local_id(ref.entity_id, "task")))
            if ref.kind == "workspace":
                return self.session.get(Category, int(_local_id(ref.entity_id, "workspace")))
            if ref.kind == "calendar_block":
                return self.session.get(
                    CalendarBlock, int(_local_id(ref.entity_id, "calendar_block"))
                )
            if ref.kind == "recurrence_series":
                return self.session.get(
                    RecurrenceSeries, _local_id(ref.entity_id, "recurrence_series")
                )
            if ref.kind == "preference":
                preference = self.session.get(
                    UserPreference, _local_id(ref.entity_id, "preference")
                )
                return preference if preference is not None and preference.user_id == "local" else None
            if ref.kind == "scheduling_constraint":
                constraint = self.session.get(
                    SchedulingConstraint,
                    _local_id(ref.entity_id, "scheduling_constraint"),
                )
                return constraint if constraint is not None and constraint.user_id == "local" else None
            if ref.kind == "preference_link":
                link = self.session.get(
                    PreferenceLink,
                    _local_id(ref.entity_id, "preference_link"),
                )
                return link if link is not None and link.user_id == "local" else None
            if ref.kind == "recurrence_occurrence":
                occurrence_id = _local_id(ref.entity_id, "recurrence_occurrence")
                if ":" not in occurrence_id:
                    return None
                series_id, recurrence_key = occurrence_id.rsplit(":", 1)
                return self.session.exec(
                    select(CalendarBlock).where(
                        CalendarBlock.recurrence_series_id == series_id,
                        CalendarBlock.recurrence_key == recurrence_key,
                        CalendarBlock.is_recurring.is_(True),
                    )
                ).first()
        except (TypeError, ValueError):
            return None
        return None


class NativeChangeSetAuthoringCoordinator:
    def __init__(self, session: Session):
        self.session = session
        self.goal_store = GoalStateStore(session)
        self.store = NativeChangeSetStore(session)
        self.authorizer = EntityReferenceAuthorizer(session)

    @staticmethod
    def _new_change_set_id(session_id: str, goal_id: str, command_id: str) -> str:
        stable = uuid5(NAMESPACE_URL, f"drasil:{session_id}:{goal_id}:{command_id}")
        return f"changeset:{stable}"

    def draft(
        self,
        session_id: str,
        command: DraftChangeSetCommand,
        *,
        command_id: str,
        correlation_id: str,
        causation_id: str,
        additional_trusted_schedule_evidence: Iterable[
            TrustedScheduleEvidence
        ] = (),
    ) -> DraftChangeSetResult:
        try:
            goal = self.goal_store.load_active(session_id)
        except GoalStateNotFound as error:
            raise ChangeSetAuthoringContextError(str(error)) from error
        if goal is None:
            raise ChangeSetAuthoringContextError("session has no active GoalState")

        if command.disposition == "create":
            change_set_id = self._new_change_set_id(session_id, goal.goal_id, command_id)
            revision = 1
        else:
            if goal.current_change_set_id is None:
                raise ChangeSetAuthoringContextError("goal has no native ChangeSet to revise")
            change_set_id = goal.current_change_set_id
            revision = int(command.expected_change_set_revision or 0) + 1

        submitted = ChangeSet(
            revision=revision,
            change_set_id=change_set_id,
            goal_id=goal.goal_id,
            operations=command.operations,
            constraints=command.constraints,
            preconditions=command.preconditions,
            assumptions=command.assumptions,
            atomic_by_default=command.atomic_by_default,
            idempotency_key=command_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            source_simulation_id=command.source_simulation_id,
            resolution_alternative_id=command.resolution_alternative_id,
        )
        authorized_resolution = False
        prior_change_set: ChangeSet | None = None
        if command.source_simulation_id is not None:
            prior_change_set = self.store.load(session_id, change_set_id)
            try:
                simulation = SimulationStore(self.session).load(
                    session_id, command.source_simulation_id
                )
            except SimulationResultNotFound as error:
                raise ChangeSetResolutionError(str(error)) from error
            if (
                simulation.change_set_id != prior_change_set.change_set_id
                or simulation.change_set_revision != prior_change_set.revision
                or simulation.goal_id != goal.goal_id
                or simulation.goal_revision > goal.revision
            ):
                raise ChangeSetResolutionError(
                    "simulation alternative does not target the current ChangeSet lineage"
                )
            alternative = next(
                (
                    item
                    for conflict in simulation.conflict_graph.conflicts
                    for item in conflict.alternatives
                    if item.alternative_id == command.resolution_alternative_id
                ),
                None,
            )
            if (
                alternative is None
                or alternative.feasibility != "feasible"
                or alternative.change_set_delta is None
            ):
                raise ChangeSetResolutionError("simulation alternative is absent or unavailable")
            expected = apply_change_set_delta(
                prior_change_set,
                alternative.change_set_delta,
                idempotency_key=command_id,
                correlation_id=correlation_id,
                causation_id=command_id,
            )
            if (
                expected.operations != submitted.operations
                or expected.constraints != submitted.constraints
                or prior_change_set.preconditions != submitted.preconditions
                or prior_change_set.assumptions != submitted.assumptions
            ):
                raise ChangeSetResolutionError(
                    "revision does not exactly match the stored simulation alternative delta"
                )
            authorized_resolution = True
        self.authorizer.authorize(submitted)
        report = evaluate_operation_conservation(
            goal, prior_change_set if authorized_resolution and prior_change_set else submitted
        )
        if authorized_resolution and report.complete:
            report = OperationConservationReport(
                complete=True,
                covered_clause_ids=report.covered_clause_ids,
                unresolved_clause_ids=[],
                unsupported_clause_ids=[],
                failed_clause_ids=[],
                extra_operation_ids=[],
                operation_fingerprints=[
                    operation_fingerprint(operation) for operation in submitted.operations
                ],
            )
        if (
            report.failed_clause_ids
            or report.unsupported_clause_ids
            or report.extra_operation_ids
        ):
            raise ChangeSetConservationError(
                "draft does not conserve every requested operation clause"
            )
        if not authorized_resolution:
            validate_exact_schedule_evidence(
                submitted,
                [
                    *trusted_schedule_evidence_from_goal(goal),
                    *additional_trusted_schedule_evidence,
                ],
            )
        command_payload = command.model_dump(mode="json", by_alias=True)
        if command.disposition == "create":
            canonical, linked_goal = self.store.create(
                session_id,
                submitted,
                expected_goal_revision=command.expected_goal_revision,
                command_id=command_id,
                command_payload=command_payload,
            )
        else:
            canonical, linked_goal = self.store.revise(
                session_id,
                submitted,
                expected_change_set_revision=int(command.expected_change_set_revision or 0),
                expected_goal_revision=command.expected_goal_revision,
                command_id=command_id,
                command_payload=command_payload,
            )
        verify_operation_round_trip(submitted, canonical)
        return DraftChangeSetResult(
            change_set=canonical,
            goal_revision=linked_goal.revision,
            conservation=report,
            persisted=True,
        )
