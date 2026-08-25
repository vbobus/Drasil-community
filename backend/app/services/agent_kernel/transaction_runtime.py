"""Exact-revision approval, atomic local apply, durable replay, and verification."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.agent_execution import (
    AgentChangeSetExecutionRecord,
    AgentExternalSyncOutboxRecord,
)
from app.models.calendar_block import CalendarBlock
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.preference_memory import PreferenceLink, SchedulingConstraint, UserPreference
from app.models.recurrence_series import RecurrenceSeries
from app.services.agent_kernel.change_set_authoring import (
    ChangeSetAuthorizationError,
    EntityReferenceAuthorizer,
)
from app.services.agent_kernel.change_set_store import (
    ChangeSetRevisionConflict,
    NativeChangeSetStore,
)
from app.services.agent_kernel.contracts import (
    ApproveAndApplyChangeSetCommand,
    EntityRef,
    ExecutionError,
    ExecutionErrorCode,
    ExecutionFact,
    ExecutionOutcome,
    ExternalSyncItemStatus,
    LocalExecutionResult,
    MAX_LOCAL_EXECUTION_RESULT_BYTES,
    VerificationMismatch,
    VerificationResult,
)
from app.services.agent_kernel.domain_mutations import NativeDomainMutationExecutor
from app.services.agent_kernel.domain_mutations import DomainMutationError, StaleEntityRevision
from app.services.agent_kernel.contracts import ToolErrorCategory
from app.services.agent_kernel.goal_state_reducer import (
    GoalStateTransitionError,
    approve_goal,
    transition_goal,
)
from app.services.agent_kernel.simulation import ChangeSetProjector, SimulationSnapshotLoader
from app.services.agent_kernel.simulation_store import SimulationStore
from app.services.agent_kernel.state_store import GoalStateStore


logger = logging.getLogger(__name__)


class TransactionRuntimeError(RuntimeError):
    pass


class StaleApproval(TransactionRuntimeError):
    def __init__(self, message: str, *, code: ExecutionErrorCode = ExecutionErrorCode.STALE_WORLD):
        super().__init__(message)
        self.code = code


class ApprovalCommandConflict(TransactionRuntimeError):
    pass


class PostCommitVerificationFailed(TransactionRuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_execution_result_json(value: str) -> LocalExecutionResult:
    if len(value.encode("utf-8")) > MAX_LOCAL_EXECUTION_RESULT_BYTES:
        raise TransactionRuntimeError("stored execution result exceeds the canonical byte budget")
    return LocalExecutionResult.model_validate_json(value)


_WORLD_COLLECTIONS = (
    "tasks",
    "calendarBlocks",
    "recurrenceSeries",
    "preferences",
    "constraints",
    "preferenceLinks",
)


def _normalize_world_value(
    value: Any,
    bindings: dict[str, EntityRef] | None = None,
) -> Any:
    bindings = bindings or {}
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_world_value(item, bindings) for item in value]
    if not isinstance(value, dict):
        if isinstance(value, str) and value in bindings:
            return bindings[value].entity_id
        return value
    if "entityId" in value and "kind" in value:
        entity_id = str(value["entityId"])
        bound = bindings.get(entity_id)
        return {
            "kind": bound.kind if bound is not None else value.get("kind"),
            "entityId": bound.entity_id if bound is not None else entity_id,
        }
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {
            "sourceOperationId",
            "source_operation_id",
            "priorityAuthoritative",
            "allowedStartTime",
            "allowedEndTime",
            "anchoring",
            "movability",
        }:
            continue
        if key in {"id", "entityId"} and isinstance(item, str) and item in bindings:
            normalized[key] = bindings[item].entity_id
            continue
        if key in {"sourcePreferenceId", "source_preference_id"} and isinstance(
            item,
            str,
        ):
            bound = bindings.get(item)
            normalized[key] = (
                bound.entity_id.removeprefix("preference:")
                if bound is not None
                else item.removeprefix("preference:")
            )
            continue
        if key in {"startAt", "endAt", "deadlineAt", "effectiveFrom", "effectiveUntil"} and isinstance(item, str):
            try:
                normalized[key] = datetime.fromisoformat(item).replace(tzinfo=None).isoformat()
                continue
            except ValueError:
                pass
        normalized[key] = _normalize_world_value(item, bindings)
    if "strength" in normalized:
        normalized["strength"] = {
            "hard": "high",
            "soft": "medium",
        }.get(normalized["strength"], normalized["strength"])
    return normalized


def _normalized_world(
    payload: dict[str, Any],
    bindings: dict[str, EntityRef] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for collection in _WORLD_COLLECTIONS:
        items: dict[str, dict[str, Any]] = {}
        for raw in payload.get(collection, []):
            normalized = _normalize_world_value(raw, bindings)
            items[str(normalized["id"])] = normalized
        result[collection] = items
    return result


def _world_delta(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    prior = _normalized_world(before)
    intended = _normalized_world(after)
    facts: list[dict[str, Any]] = []
    for collection in _WORLD_COLLECTIONS:
        for entity_id in sorted(set(prior[collection]) | set(intended[collection])):
            if prior[collection].get(entity_id) == intended[collection].get(entity_id):
                continue
            if entity_id not in intended[collection]:
                facts.append(
                    {
                        "collection": collection,
                        "entityId": entity_id,
                        "disposition": "absent",
                    }
                )
            else:
                facts.append(
                    {
                        "collection": collection,
                        "entityId": entity_id,
                        "disposition": "present",
                        "value": intended[collection][entity_id],
                    }
                )
    return facts


def _bind_world_delta(
    facts: list[dict[str, Any]], bindings: list[Any]
) -> list[dict[str, Any]]:
    binding_map = {
        item.temporary_id: item.entity_ref
        for item in bindings
        if item.temporary_id is not None
    }
    normalized = [_normalize_world_value(item, binding_map) for item in facts]
    return sorted(
        normalized,
        key=lambda item: (item["collection"], item["entityId"], item["disposition"]),
    )


def _compare_world_delta(
    payload: dict[str, Any], facts: list[dict[str, Any]]
) -> tuple[list[VerificationMismatch], str]:
    world = _normalized_world(payload)
    mismatches: list[VerificationMismatch] = []
    actual_evidence: list[dict[str, Any]] = []
    for fact in facts:
        collection = fact["collection"]
        entity_id = fact["entityId"]
        actual = world[collection].get(entity_id)
        expected = fact.get("value")
        matches = actual is None if fact["disposition"] == "absent" else actual == expected
        actual_evidence.append(
            {
                "collection": collection,
                "entityId": entity_id,
                "disposition": "absent" if actual is None else "present",
                "value": actual,
            }
        )
        if matches:
            continue
        expected_fingerprint = _fingerprint(
            {"disposition": fact["disposition"], "value": expected}
        )
        actual_fingerprint = _fingerprint(
            {
                "disposition": "absent" if actual is None else "present",
                "value": actual,
            }
        )
        mismatches.append(
            VerificationMismatch(
                operation_id="operation:approved-projection",
                expected_fingerprint=expected_fingerprint,
                actual_fingerprint=actual_fingerprint,
                summary=f"{collection} entity {entity_id} differs from approved simulation intent.",
            )
        )
    return mismatches, _fingerprint(actual_evidence)


class AgentChangeSetTransactionRuntime:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def command_fingerprint(command: ApproveAndApplyChangeSetCommand) -> str:
        payload = command.model_dump(mode="json", by_alias=True)
        payload.pop("commandId", None)
        payload.pop("idempotencyKey", None)
        return _fingerprint(payload)

    def _execution_for_command(
        self, command: ApproveAndApplyChangeSetCommand
    ) -> AgentChangeSetExecutionRecord | None:
        return self.session.exec(
            select(AgentChangeSetExecutionRecord).where(
                AgentChangeSetExecutionRecord.session_id == command.session_id,
                or_(
                    AgentChangeSetExecutionRecord.command_id == command.command_id,
                    AgentChangeSetExecutionRecord.idempotency_key
                    == command.idempotency_key,
                    (
                        AgentChangeSetExecutionRecord.change_set_id
                        == command.change_set_id
                    )
                    & (
                        AgentChangeSetExecutionRecord.change_set_revision
                        == command.expected_change_set_revision
                    ),
                ),
            )
        ).first()

    @staticmethod
    def _validate_replay(
        record: AgentChangeSetExecutionRecord,
        command: ApproveAndApplyChangeSetCommand,
        fingerprint: str,
    ) -> None:
        if (
            record.payload_fingerprint != fingerprint
            or (
                record.command_id == command.command_id
                and record.idempotency_key != command.idempotency_key
            )
            or record.goal_id != command.goal_id
            or record.change_set_id != command.change_set_id
            or record.change_set_revision != command.expected_change_set_revision
            or record.simulation_id != command.simulation_id
        ):
            raise ApprovalCommandConflict(
                "approval command identity was already used for different intent"
            )

    def _result_with_current_sync(
        self, record: AgentChangeSetExecutionRecord
    ) -> LocalExecutionResult:
        result = _parse_execution_result_json(record.result_json)
        rows = self.session.exec(
            select(AgentExternalSyncOutboxRecord).where(
                AgentExternalSyncOutboxRecord.execution_id == record.execution_id
            )
        ).all()
        sync_items = [
            ExternalSyncItemStatus(
                outbox_id=item.outbox_id,
                provider=item.provider,
                operation=item.operation,
                entity_kind=item.entity_kind,
                entity_id=item.entity_id,
                status=item.status,
                attempts=item.attempts,
                next_attempt_at=item.next_attempt_at,
                remote_id=item.remote_id,
                remote_version=item.remote_version,
                last_error_code=item.last_error_code,
                error_summary=item.last_error_summary,
                recovery=(
                    "reconnect"
                    if item.last_error_code == "SYNC_NOT_CONFIGURED"
                    else "retry"
                    if item.last_error_code == "SYNC_RETRYABLE"
                    else "inspect"
                    if item.last_error_code in {"SYNC_INVALID_PAYLOAD", "SYNC_PERMANENT"}
                    else None
                ),
            )
            for item in rows
        ]
        return LocalExecutionResult.model_validate(
            result.model_copy(update={"sync_items": sync_items}).model_dump(mode="python")
        )

    def approve_and_apply(
        self,
        command: ApproveAndApplyChangeSetCommand,
        *,
        fail_after_operation: str | None = None,
        verify_after_commit: bool = True,
    ) -> LocalExecutionResult:
        fingerprint = self.command_fingerprint(command)
        replay = self._execution_for_command(command)
        if replay is not None:
            self._validate_replay(replay, command, fingerprint)
            if replay.status == "committed_pending_verification":
                self.session.rollback()
                return self._verify(replay.execution_id)
            return self._result_with_current_sync(replay)

        try:
            goal_store = GoalStateStore(self.session)
            change_store = NativeChangeSetStore(self.session)
            simulation_store = SimulationStore(self.session)
            goal = goal_store.load(command.session_id, command.goal_id)
            change_set = change_store.load(command.session_id, command.change_set_id)
            simulation = simulation_store.load(command.session_id, command.simulation_id)
            self._preflight(command, goal, change_set, simulation)
            loaded_world = SimulationSnapshotLoader(self.session).load(change_set)
            approved_projection = ChangeSetProjector().project(
                change_set, loaded_world.world
            )
            if approved_projection.output_fingerprint != simulation.output_fingerprint:
                raise StaleApproval(
                    "approved simulation projection cannot be reproduced exactly",
                    code=ExecutionErrorCode.STALE_SIMULATION,
                )
            intended_delta = _world_delta(
                loaded_world.world.canonical_payload(),
                approved_projection.world.canonical_payload(),
            )
            approved_revisions = {
                (fact.entity_ref.kind, fact.entity_ref.entity_id): int(
                    fact.entity_ref.revision
                )
                for fact in simulation.input_snapshot.facts
                if fact.entity_ref is not None and fact.entity_ref.revision is not None
            }

            approved_goal = approve_goal(
                goal,
                change_set_revision=change_set.revision,
                command_id=command.command_id,
            )
            approved_goal = goal_store.save(
                command.session_id,
                approved_goal,
                expected_revision=goal.revision,
                command_id=f"{command.command_id}:goal-approved",
                transition_kind="goal.approved",
                payload={
                    "changeSetId": change_set.change_set_id,
                    "changeSetRevision": change_set.revision,
                    "simulationId": simulation.simulation_id,
                },
            )
            change_store.transition_status(
                command.session_id,
                change_set.change_set_id,
                expected_revision=change_set.revision,
                expected_status="ready_for_review",
                target_status="approved",
            )
            committing_goal = transition_goal(
                approved_goal, "committing", command_id=command.command_id
            )
            committing_goal = goal_store.save(
                command.session_id,
                committing_goal,
                expected_revision=approved_goal.revision,
                command_id=f"{command.command_id}:goal-committing",
                transition_kind="goal.committing",
                payload={"changeSetId": change_set.change_set_id},
            )

            mutation = NativeDomainMutationExecutor(
                self.session,
                fail_after_operation=fail_after_operation,
                approved_revisions=approved_revisions,
            ).apply(change_set)
            submitted_operation_ids = {item.operation_id for item in change_set.operations}
            applied_operation_ids = {item.operation_id for item in mutation.facts}
            simulated_operation_ids = {
                item.operation_id for item in simulation.projected_effects
            }
            if (
                applied_operation_ids != submitted_operation_ids
                or simulated_operation_ids != submitted_operation_ids
            ):
                raise TransactionRuntimeError(
                    "native execution did not conserve the exact simulated operation set"
                )
            stable_projection = _bind_world_delta(intended_delta, mutation.bindings)
            current_world = SimulationSnapshotLoader(self.session).load(change_set)
            actual_transaction_delta = _world_delta(
                loaded_world.world.canonical_payload(),
                current_world.world.canonical_payload(),
            )
            actual_transaction_delta = sorted(
                actual_transaction_delta,
                key=lambda item: (
                    item["collection"],
                    item["entityId"],
                    item["disposition"],
                ),
            )
            if actual_transaction_delta != stable_projection:
                raise TransactionRuntimeError(
                    "native execution changed facts outside the exact approved projection"
                )
            projection_mismatches, _actual_projection_fingerprint = _compare_world_delta(
                current_world.world.canonical_payload(), stable_projection
            )
            if projection_mismatches:
                raise TransactionRuntimeError(
                    "native execution differs from the exact approved simulation projection"
                )
            intended_fingerprint = _fingerprint(stable_projection)
            execution_id = f"execution:{fingerprint[:32]}"
            sync_items: list[ExternalSyncItemStatus] = []
            from app.services.google_calendar import build_google_outbox_payload

            for index, effect in enumerate(mutation.external_effects):
                with self.session.no_autoflush:
                    provider_payload = build_google_outbox_payload(
                        self.session,
                        operation=effect.operation,
                        entity_id=effect.entity_id,
                        base_payload=effect.payload,
                    )
                semantic_key = (
                    f"google:{effect.operation}:{effect.entity_id}:"
                    f"{change_set.change_set_id}:{change_set.revision}:{index}:"
                    f"{_fingerprint(provider_payload)[:16]}"
                )
                outbox_id = f"outbox:{hashlib.sha256(semantic_key.encode()).hexdigest()[:32]}"
                row = AgentExternalSyncOutboxRecord(
                    outbox_id=outbox_id,
                    execution_id=execution_id,
                    session_id=command.session_id,
                    change_set_id=change_set.change_set_id,
                    change_set_revision=change_set.revision,
                    provider="google",
                    operation=effect.operation,
                    entity_kind="calendar_block",
                    entity_id=effect.entity_id,
                    semantic_key=semantic_key,
                    effect_ordinal=index,
                    payload_json=_canonical(provider_payload),
                )
                self.session.add(row)
                sync_items.append(
                    ExternalSyncItemStatus(
                        outbox_id=outbox_id,
                        provider="google",
                        operation=effect.operation,
                        entity_kind="calendar_block",
                        entity_id=effect.entity_id,
                        status="pending",
                        attempts=0,
                    )
                )
            verification = VerificationResult(
                status="pending", intended_fingerprint=intended_fingerprint
            )
            pending = LocalExecutionResult(
                execution_id=execution_id,
                command_id=command.command_id,
                idempotency_key=command.idempotency_key,
                session_id=command.session_id,
                goal_id=command.goal_id,
                goal_revision=committing_goal.revision,
                change_set_id=change_set.change_set_id,
                change_set_revision=change_set.revision,
                simulation_id=simulation.simulation_id,
                status="committed_pending_verification",
                payload_fingerprint=fingerprint,
                bindings=mutation.bindings,
                facts=mutation.facts,
                verification=verification,
                sync_items=sync_items,
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
            )
            self.session.add(
                AgentChangeSetExecutionRecord(
                    execution_id=execution_id,
                    session_id=command.session_id,
                    command_id=command.command_id,
                    idempotency_key=command.idempotency_key,
                    goal_id=command.goal_id,
                    goal_revision=committing_goal.revision,
                    change_set_id=change_set.change_set_id,
                    change_set_revision=change_set.revision,
                    simulation_id=simulation.simulation_id,
                    payload_fingerprint=fingerprint,
                    status=pending.status,
                    intended_fingerprint=intended_fingerprint,
                    projection_json=_canonical(
                        stable_projection
                    ),
                    result_json=pending.model_dump_json(),
                )
            )
            self.session.flush()
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            winner = self._execution_for_command(command)
            if winner is None:
                raise ApprovalCommandConflict("concurrent approval could not be reconciled") from error
            self._validate_replay(winner, command, fingerprint)
            if winner.status == "committed_pending_verification":
                self.session.rollback()
                return self._verify(winner.execution_id)
            return self._result_with_current_sync(winner)
        except Exception:
            self.session.rollback()
            raise
        if not verify_after_commit:
            return pending
        return self._verify(execution_id)

    def execute(self, command: ApproveAndApplyChangeSetCommand) -> ExecutionOutcome:
        """Safe application boundary for the future typed UI action endpoint."""

        try:
            return ExecutionOutcome(status="ok", result=self.approve_and_apply(command))
        except ApprovalCommandConflict:
            error = ExecutionError(
                code=ExecutionErrorCode.COMMAND_REUSE_CONFLICT,
                category=ToolErrorCategory.FATAL_INVARIANT,
                retryable=False,
                user_summary="This approval command was already used for another review.",
                recovery="inspect",
            )
        except StaleApproval as stale:
            error = ExecutionError(
                code=stale.code,
                category=ToolErrorCategory.MODEL_REPAIRABLE,
                retryable=False,
                user_summary="This review is out of date. Generate and review a fresh plan.",
                recovery="resimulate",
            )
        except ChangeSetAuthorizationError as authorization_error:
            precondition = "precondition" in str(authorization_error).lower()
            error = ExecutionError(
                code=(
                    ExecutionErrorCode.PRECONDITION_FAILED
                    if precondition
                    else ExecutionErrorCode.AUTHORIZATION_DENIED
                ),
                category=(
                    ToolErrorCategory.MODEL_REPAIRABLE
                    if precondition
                    else ToolErrorCategory.AUTHORIZATION
                ),
                retryable=False,
                user_summary=(
                    "This review no longer has authority over every referenced item."
                ),
                recovery="revise",
            )
        except (ChangeSetRevisionConflict, GoalStateTransitionError):
            error = ExecutionError(
                code=ExecutionErrorCode.INVALID_LIFECYCLE,
                category=ToolErrorCategory.MODEL_REPAIRABLE,
                retryable=False,
                user_summary="This review is no longer at an executable lifecycle state.",
                recovery="resimulate",
            )
        except StaleEntityRevision:
            error = ExecutionError(
                code=ExecutionErrorCode.STALE_ENTITY,
                category=ToolErrorCategory.MODEL_REPAIRABLE,
                retryable=False,
                user_summary="An affected item changed after this review. Generate a fresh plan.",
                recovery="resimulate",
            )
        except DomainMutationError as mutation_error:
            if "unsupported operation" in str(mutation_error):
                code = ExecutionErrorCode.UNSUPPORTED_OPERATION
            elif "non-atomic" in str(mutation_error) or "partial" in str(mutation_error):
                code = ExecutionErrorCode.UNSUPPORTED_ATOMIC_MODE
            else:
                code = ExecutionErrorCode.LOCAL_APPLY_FAILED
            error = ExecutionError(
                code=code,
                category=ToolErrorCategory.FATAL_INVARIANT,
                retryable=False,
                user_summary="Drasil could not apply the complete reviewed change safely.",
                recovery="inspect",
            )
        except Exception:
            logger.exception(
                "Unexpected native ChangeSet execution failure for session=%s command=%s",
                command.session_id,
                command.command_id,
            )
            error = ExecutionError(
                code=ExecutionErrorCode.INVARIANT_FAILED,
                category=ToolErrorCategory.FATAL_INVARIANT,
                retryable=False,
                user_summary="Drasil stopped before it could safely confirm this change.",
                recovery="inspect",
            )
        return ExecutionOutcome(status="error", error=error)

    def _preflight(self, command: Any, goal: Any, change_set: Any, simulation: Any) -> None:
        if goal.revision != command.expected_goal_revision or goal.status != "ready_for_review":
            raise StaleApproval(
                "goal revision or lifecycle no longer matches the review",
                code=ExecutionErrorCode.STALE_GOAL,
            )
        if (
            goal.current_change_set_id != command.change_set_id
            or goal.current_change_set_revision != command.expected_change_set_revision
            or goal.approved_change_set_revision is not None
        ):
            raise StaleApproval(
                "goal no longer points to the approved ChangeSet revision",
                code=ExecutionErrorCode.INVALID_APPROVAL_IDENTITY,
            )
        if (
            change_set.revision != command.expected_change_set_revision
            or change_set.status != "ready_for_review"
            or not change_set.atomic_by_default
            or change_set.simulation_result_ref != command.simulation_id
        ):
            raise StaleApproval(
                "ChangeSet revision or lifecycle no longer matches the review",
                code=(
                    ExecutionErrorCode.UNSUPPORTED_ATOMIC_MODE
                    if not change_set.atomic_by_default
                    else ExecutionErrorCode.STALE_CHANGE_SET
                ),
            )
        if (
            simulation.status != "ready_for_review"
            or simulation.resulting_goal_revision != command.expected_goal_revision
            or simulation.change_set_id != command.change_set_id
            or simulation.change_set_revision != command.expected_change_set_revision
            or simulation.input_snapshot.fingerprint != command.simulation_input_fingerprint
            or simulation.output_fingerprint != command.simulation_output_fingerprint
            or not simulation.conservation.complete
            or simulation.issues
            or simulation.conflict_graph.status not in {"clear", "resolved"}
        ):
            raise StaleApproval(
                "simulation evidence no longer matches the approval",
                code=ExecutionErrorCode.STALE_SIMULATION,
            )
        EntityReferenceAuthorizer(self.session).authorize(change_set)
        fresh = SimulationSnapshotLoader(self.session).load(change_set)
        if fresh.snapshot.fingerprint != simulation.input_snapshot.fingerprint:
            raise StaleApproval(
                "authoritative world changed after simulation",
                code=ExecutionErrorCode.STALE_WORLD,
            )

    @staticmethod
    def _entity(session: Session, ref: EntityRef) -> Any | None:
        entity_id = ref.entity_id
        try:
            if ref.kind == "task":
                return session.get(StrategyNode, int(entity_id.removeprefix("task:")))
            if ref.kind == "workspace":
                return session.get(Category, int(entity_id.removeprefix("workspace:")))
            if ref.kind == "calendar_block":
                return session.get(CalendarBlock, int(entity_id.removeprefix("calendar_block:")))
            if ref.kind == "recurrence_series":
                return session.get(RecurrenceSeries, entity_id.removeprefix("recurrence_series:"))
            if ref.kind == "preference":
                return session.get(UserPreference, entity_id.removeprefix("preference:"))
            if ref.kind == "scheduling_constraint":
                return session.get(
                    SchedulingConstraint,
                    entity_id.removeprefix("scheduling_constraint:"),
                )
            if ref.kind == "preference_link":
                return session.get(
                    PreferenceLink,
                    entity_id.removeprefix("preference_link:"),
                )
            if ref.kind == "recurrence_occurrence":
                raw = entity_id.removeprefix("recurrence_occurrence:")
                series_id, recurrence_key = raw.rsplit(":", 1)
                rows = session.exec(
                    select(CalendarBlock).where(
                        CalendarBlock.recurrence_series_id == series_id,
                        CalendarBlock.recurrence_key == recurrence_key,
                    )
                ).all()
                return rows[0] if len(rows) == 1 else None
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _normalize_actual(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None).isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    @classmethod
    def _verify_fact(
        cls, session: Session, fact: ExecutionFact
    ) -> tuple[str, VerificationMismatch | None]:
        row = cls._entity(session, fact.entity_ref)
        if fact.disposition == "absent":
            actual = {"disposition": "absent"} if row is None else {"disposition": "present"}
        else:
            actual = {}
            if row is None:
                actual = {"disposition": "absent"}
            else:
                for expected in fact.fields:
                    if not hasattr(row, expected.field):
                        actual[expected.field] = None
                    else:
                        actual[expected.field] = cls._normalize_actual(
                            getattr(row, expected.field)
                        )
        actual_fingerprint = _fingerprint(actual)
        if actual_fingerprint == fact.fingerprint:
            return actual_fingerprint, None
        return actual_fingerprint, VerificationMismatch(
            operation_id=fact.operation_id,
            entity_ref=fact.entity_ref,
            expected_fingerprint=fact.fingerprint,
            actual_fingerprint=actual_fingerprint,
            summary="Persisted affected facts do not match the approved result.",
        )

    def _verify(self, execution_id: str) -> LocalExecutionResult:
        bind = self.session.get_bind()
        with Session(bind) as fresh:
            record = fresh.get(AgentChangeSetExecutionRecord, execution_id)
            if record is None:
                raise TransactionRuntimeError("execution record does not exist")
            if record.status != "committed_pending_verification":
                return _parse_execution_result_json(record.result_json)
            claim = fresh.exec(
                update(AgentChangeSetExecutionRecord)
                .where(
                    AgentChangeSetExecutionRecord.execution_id == execution_id,
                    AgentChangeSetExecutionRecord.status
                    == "committed_pending_verification",
                )
                .values(status="verifying", updated_at=datetime.utcnow())
            )
            if claim.rowcount != 1:  # type: ignore[attr-defined]
                fresh.rollback()
                winner = fresh.get(AgentChangeSetExecutionRecord, execution_id)
                if winner is None:
                    raise TransactionRuntimeError("execution record disappeared during verification")
                return _parse_execution_result_json(winner.result_json)
            fresh.expire_all()
            record = fresh.get(AgentChangeSetExecutionRecord, execution_id)
            if record is None:
                raise TransactionRuntimeError("execution record disappeared during verification")
            pending = _parse_execution_result_json(record.result_json)
            change_store = NativeChangeSetStore(fresh)
            change_set = change_store.load(record.session_id, record.change_set_id)
            goal_store = GoalStateStore(fresh)
            goal = goal_store.load(record.session_id, record.goal_id)
            exact_lifecycle = (
                change_set.revision == record.change_set_revision
                and change_set.status == "approved"
                and change_set.approved_revision == record.change_set_revision
                and goal.revision == record.goal_revision
                and goal.status == "committing"
                and goal.current_change_set_id == record.change_set_id
                and goal.current_change_set_revision == record.change_set_revision
                and goal.approved_change_set_revision == record.change_set_revision
            )
            intended_projection = json.loads(record.projection_json)
            current_world = SimulationSnapshotLoader(fresh).load(change_set)
            mismatches, actual_fingerprint = _compare_world_delta(
                current_world.world.canonical_payload(), intended_projection
            )
            if not exact_lifecycle:
                actual_lifecycle = {
                    "goalRevision": goal.revision,
                    "goalStatus": goal.status,
                    "changeSetStatus": change_set.status,
                }
                mismatches.append(
                    VerificationMismatch(
                        operation_id="operation:approval-lifecycle",
                        expected_fingerprint=_fingerprint(
                            {
                                "goalRevision": record.goal_revision,
                                "goalStatus": "committing",
                                "changeSetStatus": "approved",
                            }
                        ),
                        actual_fingerprint=_fingerprint(actual_lifecycle),
                        summary="Approval lifecycle changed before exact verification completed.",
                    )
                )
                actual_fingerprint = _fingerprint(
                    {"worldFingerprint": actual_fingerprint, "lifecycle": actual_lifecycle}
                )
            verified_at = datetime.now(timezone.utc)
            if mismatches:
                verification = VerificationResult(
                    status="verification_failed",
                    intended_fingerprint=record.intended_fingerprint,
                    actual_fingerprint=actual_fingerprint,
                    verified_at=verified_at,
                    mismatches=mismatches,
                    recovery="compensating_review",
                    recovery_summary=(
                        "Keep the committed local state visible and open a bounded review "
                        "before attempting any compensating ChangeSet."
                    ),
                )
                if exact_lifecycle:
                    failed_goal = transition_goal(
                        goal, "system_failed", command_id=f"{record.command_id}:verification"
                    )
                    failed_goal = goal_store.save(
                        record.session_id,
                        failed_goal,
                        expected_revision=goal.revision,
                        command_id=f"{record.command_id}:goal-verification-failed",
                        transition_kind="goal.verification_failed",
                        payload={"executionId": execution_id},
                    )
                    goal_revision = failed_goal.revision
                else:
                    goal_revision = goal.revision
                status = "verification_failed"
            else:
                verification = VerificationResult(
                    status="verified",
                    intended_fingerprint=record.intended_fingerprint,
                    actual_fingerprint=record.intended_fingerprint,
                    verified_at=verified_at,
                )
                change_store.transition_status(
                    record.session_id,
                    record.change_set_id,
                    expected_revision=record.change_set_revision,
                    expected_status="approved",
                    target_status="applied",
                )
                applied_goal = transition_goal(
                    goal, "applied", command_id=f"{record.command_id}:verification"
                )
                applied_goal = goal_store.save(
                    record.session_id,
                    applied_goal,
                    expected_revision=goal.revision,
                    command_id=f"{record.command_id}:goal-applied",
                    transition_kind="goal.applied",
                    payload={"executionId": execution_id},
                )
                status = "applied"
                goal_revision = applied_goal.revision
            outbox = fresh.exec(
                select(AgentExternalSyncOutboxRecord).where(
                    AgentExternalSyncOutboxRecord.execution_id == execution_id
                )
            ).all()
            sync_items = [
                ExternalSyncItemStatus(
                    outbox_id=item.outbox_id,
                    provider="google",
                    operation=item.operation,
                    entity_kind="calendar_block",
                    entity_id=item.entity_id,
                    status=item.status,
                    attempts=item.attempts,
                    remote_id=item.remote_id,
                    remote_version=item.remote_version,
                    last_error_code=item.last_error_code,
                )
                for item in outbox
            ]
            final = pending.model_copy(
                update={
                    "goal_revision": goal_revision,
                    "status": status,
                    "verification": verification,
                    "sync_items": sync_items,
                }
            )
            final = LocalExecutionResult.model_validate(final.model_dump(mode="python"))
            record.status = status
            record.goal_revision = goal_revision
            record.actual_fingerprint = verification.actual_fingerprint
            record.verification_json = verification.model_dump_json()
            record.result_json = final.model_dump_json()
            record.updated_at = datetime.utcnow()
            fresh.add(record)
            fresh.commit()
        self.session.expire_all()
        if final.status == "verification_failed":
            return final
        return final
