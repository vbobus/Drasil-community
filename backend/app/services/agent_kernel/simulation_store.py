"""Immutable simulation result replay and audit storage."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.agent_simulation import AgentSimulationEventRecord, AgentSimulationRecord
from app.services.agent_kernel.contracts import ChangeSet, ChangeSetSimulationResult


class SimulationStoreError(RuntimeError):
    pass


class SimulationCommandConflict(SimulationStoreError):
    pass


class SimulationResultNotFound(SimulationStoreError):
    pass


class SimulationAttemptIncomplete(SimulationStoreError):
    def __init__(self, record: AgentSimulationRecord):
        super().__init__("simulation command is already in progress")
        self.record = record

    @property
    def recoverable(self) -> bool:
        return datetime.utcnow() - self.record.updated_at >= timedelta(seconds=30)


class SimulationStore:
    def __init__(self, session: Session):
        self.session = session

    def _command_record(
        self, session_id: str, command_id: str
    ) -> AgentSimulationRecord | None:
        return self.session.exec(
            select(AgentSimulationRecord).where(
                AgentSimulationRecord.session_id == session_id,
                AgentSimulationRecord.command_id == command_id,
            )
        ).first()

    @staticmethod
    def _validate_payload_identity(
        record: AgentSimulationRecord, payload_fingerprint: str
    ) -> None:
        if record.payload_fingerprint != payload_fingerprint:
            raise SimulationCommandConflict(
                "simulation command identity was already used for different revision intent"
            )

    @staticmethod
    def _validated_change_set(record: AgentSimulationRecord) -> ChangeSet:
        try:
            change_set = ChangeSet.model_validate_json(record.change_set_snapshot_json)
        except Exception as error:
            raise SimulationCommandConflict("stored simulation ChangeSet is invalid") from error
        if (
            change_set.change_set_id != record.change_set_id
            or change_set.goal_id != record.goal_id
            or change_set.revision != record.change_set_revision
        ):
            raise SimulationCommandConflict(
                "stored simulation ChangeSet identity is inconsistent"
            )
        return change_set

    @classmethod
    def _validated_result(
        cls, record: AgentSimulationRecord
    ) -> ChangeSetSimulationResult:
        if record.status == "started" or not record.result_json:
            raise SimulationAttemptIncomplete(record)
        try:
            result = ChangeSetSimulationResult.model_validate_json(record.result_json)
        except Exception as error:
            raise SimulationCommandConflict("stored simulation result is invalid") from error
        cls._validated_change_set(record)
        if (
            result.simulation_id != record.simulation_id
            or result.command_id != record.command_id
            or result.goal_id != record.goal_id
            or result.goal_revision != record.goal_revision
            or result.resulting_goal_revision != record.resulting_goal_revision
            or result.change_set_id != record.change_set_id
            or result.change_set_revision != record.change_set_revision
            or result.input_snapshot.fingerprint != record.input_snapshot_fingerprint
            or result.status != record.status
        ):
            raise SimulationCommandConflict("stored simulation identity is inconsistent")
        return result

    def replay(
        self, session_id: str, command_id: str, *, payload_fingerprint: str
    ) -> ChangeSetSimulationResult | None:
        record = self._command_record(session_id, command_id)
        if record is None:
            return None
        self._validate_payload_identity(record, payload_fingerprint)
        return self._validated_result(record)

    def start(
        self,
        session_id: str,
        *,
        simulation_id: str,
        command_id: str,
        payload_fingerprint: str,
        goal_id: str,
        goal_revision: int,
        change_set: ChangeSet,
        correlation_id: str,
        causation_id: str,
    ) -> ChangeSetSimulationResult | None:
        existing = self._command_record(session_id, command_id)
        if existing is not None:
            self._validate_payload_identity(existing, payload_fingerprint)
            return self._validated_result(existing)
        try:
            with self.session.begin_nested():
                self.session.add(
                    AgentSimulationRecord(
                        simulation_id=simulation_id,
                        session_id=session_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                        change_set_id=change_set.change_set_id,
                        change_set_revision=change_set.revision,
                        command_id=command_id,
                        payload_fingerprint=payload_fingerprint,
                        status="started",
                        change_set_snapshot_json=change_set.model_dump_json(),
                    )
                )
                self.session.flush()
                self.session.add(
                    AgentSimulationEventRecord(
                        simulation_id=simulation_id,
                        session_id=session_id,
                        event_kind="simulation.started",
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                    )
                )
                self.session.flush()
        except IntegrityError:
            self.session.expire_all()
            winner = self._command_record(session_id, command_id)
            if winner is None:
                raise SimulationCommandConflict(
                    "concurrent simulation command could not be reconciled"
                )
            self._validate_payload_identity(winner, payload_fingerprint)
            return self._validated_result(winner)
        return None

    def finalize(
        self,
        session_id: str,
        result: ChangeSetSimulationResult,
        *,
        payload_fingerprint: str,
        change_set: ChangeSet,
    ) -> ChangeSetSimulationResult:
        record = self._command_record(session_id, result.command_id)
        if record is None:
            raise SimulationCommandConflict("simulation attempt was not started")
        self._validate_payload_identity(record, payload_fingerprint)
        if record.status != "started":
            return self._validated_result(record)
        stored_change_set = self._validated_change_set(record)
        if (
            stored_change_set.change_set_id != change_set.change_set_id
            or stored_change_set.revision != change_set.revision
            or result.simulation_id != record.simulation_id
            or result.goal_id != record.goal_id
            or result.goal_revision != record.goal_revision
            or result.change_set_id != record.change_set_id
            or result.change_set_revision != record.change_set_revision
        ):
            raise SimulationCommandConflict("simulation finalization identity is inconsistent")
        record.resulting_goal_revision = result.resulting_goal_revision
        record.input_snapshot_fingerprint = result.input_snapshot.fingerprint
        record.status = result.status
        record.result_json = result.model_dump_json()
        record.updated_at = datetime.utcnow()
        self.session.add(record)
        terminal_kind = f"simulation.{result.status}"
        existing_event = self.session.exec(
            select(AgentSimulationEventRecord).where(
                AgentSimulationEventRecord.simulation_id == result.simulation_id,
                AgentSimulationEventRecord.event_kind == terminal_kind,
            )
        ).first()
        if existing_event is None:
            self.session.add(
                AgentSimulationEventRecord(
                    simulation_id=result.simulation_id,
                    session_id=session_id,
                    event_kind=terminal_kind,
                    correlation_id=result.correlation_id,
                    causation_id=result.causation_id,
                )
            )
        self.session.flush()
        return result

    def add(
        self,
        session_id: str,
        result: ChangeSetSimulationResult,
        *,
        payload_fingerprint: str,
        change_set: ChangeSet,
    ) -> ChangeSetSimulationResult:
        replay = self.start(
            session_id,
            simulation_id=result.simulation_id,
            command_id=result.command_id,
            payload_fingerprint=payload_fingerprint,
            goal_id=result.goal_id,
            goal_revision=result.goal_revision,
            change_set=change_set,
            correlation_id=result.correlation_id,
            causation_id=result.causation_id,
        )
        if replay is not None:
            return replay
        return self.finalize(
            session_id,
            result,
            payload_fingerprint=payload_fingerprint,
            change_set=change_set,
        )

    def load_change_set(self, session_id: str, simulation_id: str) -> ChangeSet:
        record = self.session.get(AgentSimulationRecord, simulation_id)
        if record is None or record.session_id != session_id:
            raise SimulationResultNotFound("simulation result does not exist in this session")
        return self._validated_change_set(record)

    def load(self, session_id: str, simulation_id: str) -> ChangeSetSimulationResult:
        record = self.session.get(AgentSimulationRecord, simulation_id)
        if record is None or record.session_id != session_id:
            raise SimulationResultNotFound("simulation result does not exist in this session")
        return self._validated_result(record)

    def delete_session_history(self, session_id: str) -> None:
        simulations = self.session.exec(
            select(AgentSimulationRecord.simulation_id).where(
                AgentSimulationRecord.session_id == session_id
            )
        ).all()
        if simulations:
            self.session.exec(
                delete(AgentSimulationEventRecord).where(
                    AgentSimulationEventRecord.simulation_id.in_(simulations)
                )
            )
        self.session.exec(
            delete(AgentSimulationRecord).where(AgentSimulationRecord.session_id == session_id)
        )
        self.session.flush()
