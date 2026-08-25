"""Native ChangeSet schema; authoring and execution arrive in later stories."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import Field, model_validator

from .common import ContractId, RevisionedContract
from .conservation import OperationConservationReport
from .constraints import Constraint
from .operations import Operation
from .references import EntityRef, Provenance


MAX_CHANGE_SET_OPERATIONS = 128
MAX_CHANGE_SET_CONSTRAINTS = 128
MAX_CHANGE_SET_REFERENCES = 512
MAX_CHANGE_SET_BYTES = 256 * 1024


AssertionScalar: TypeAlias = str | int | float | bool | None


class AssertionBase(RevisionedContract):
    kind: str
    assertion_id: ContractId
    entity_ref: EntityRef

    @model_validator(mode="after")
    def validate_persisted_ref(self) -> "AssertionBase":
        if self.entity_ref.kind == "temporary":
            raise ValueError("precondition assertions require persisted entity references")
        return self


class EntityExistsAssertion(AssertionBase):
    kind: Literal["entity.exists"] = "entity.exists"


class EntityAbsentAssertion(AssertionBase):
    kind: Literal["entity.absent"] = "entity.absent"


class EntityFieldEqualsAssertion(AssertionBase):
    kind: Literal["entity.field_equals"] = "entity.field_equals"
    field: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    expected_value: AssertionScalar


PreconditionAssertion: TypeAlias = Annotated[
    EntityExistsAssertion | EntityAbsentAssertion | EntityFieldEqualsAssertion,
    Field(discriminator="kind"),
]


class Preconditions(RevisionedContract):
    expected_entity_revisions: list[EntityRef] = Field(default_factory=list)
    assertions: list[PreconditionAssertion] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_preconditions(self) -> "Preconditions":
        identities = [
            (ref.kind, ref.entity_id) for ref in self.expected_entity_revisions
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("expected entity revisions cannot contain duplicates")
        if any(
            ref.kind == "temporary" or ref.revision is None
            for ref in self.expected_entity_revisions
        ):
            raise ValueError("expected entity revisions require persisted revisioned references")
        assertion_ids = [assertion.assertion_id for assertion in self.assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("precondition assertion IDs must be unique")
        return self


class DraftChangeSetCommand(RevisionedContract):
    """The only model-authored input; all identity and trace fields are server-owned."""

    disposition: Literal["create", "revise"]
    expected_goal_revision: int = Field(ge=1)
    expected_change_set_revision: int | None = Field(default=None, ge=1)
    operations: list[Operation] = Field(min_length=1, max_length=MAX_CHANGE_SET_OPERATIONS)
    constraints: list[Constraint] = Field(default_factory=list, max_length=MAX_CHANGE_SET_CONSTRAINTS)
    preconditions: Preconditions = Field(default_factory=Preconditions)
    assumptions: list[Provenance] = Field(default_factory=list)
    atomic_by_default: Literal[True] = True
    source_simulation_id: ContractId | None = None
    resolution_alternative_id: ContractId | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> "DraftChangeSetCommand":
        if self.disposition == "create" and self.expected_change_set_revision is not None:
            raise ValueError("create cannot declare an expected ChangeSet revision")
        if self.disposition == "revise" and self.expected_change_set_revision is None:
            raise ValueError("revise requires the expected current ChangeSet revision")
        if (self.source_simulation_id is None) != (self.resolution_alternative_id is None):
            raise ValueError("simulation resolution requires both simulation and alternative IDs")
        if self.source_simulation_id is not None and self.disposition != "revise":
            raise ValueError("simulation alternatives may only revise an existing ChangeSet")
        return self


class DraftChangeSetResult(RevisionedContract):
    change_set: "ChangeSet"
    goal_revision: int = Field(ge=1)
    conservation: OperationConservationReport
    persisted: Literal[True] = True


class ChangeSet(RevisionedContract):
    change_set_id: ContractId
    goal_id: ContractId
    status: Literal[
        "draft", "simulating", "invalid", "needs_user_decision", "ready_for_review", "approved", "rejected", "applied"
    ] = "draft"
    operations: list[Operation] = Field(min_length=1, max_length=MAX_CHANGE_SET_OPERATIONS)
    constraints: list[Constraint] = Field(default_factory=list, max_length=MAX_CHANGE_SET_CONSTRAINTS)
    preconditions: Preconditions = Field(default_factory=Preconditions)
    assumptions: list[Provenance] = Field(default_factory=list)
    atomic_by_default: bool = True
    simulation_result_ref: ContractId | None = None
    approved_revision: int | None = Field(default=None, ge=1)
    idempotency_key: ContractId
    correlation_id: ContractId
    causation_id: ContractId
    source_simulation_id: ContractId | None = None
    resolution_alternative_id: ContractId | None = None

    @model_validator(mode="after")
    def validate_native_graph(self) -> "ChangeSet":
        if not self.atomic_by_default:
            raise ValueError("Story 8.4 native ChangeSets must remain atomic by default")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation IDs must be unique within a ChangeSet")
        constraint_ids = [constraint.constraint_id for constraint in self.constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraint IDs must be unique within a ChangeSet")
        operation_id_set = set(operation_ids)
        producers: dict[str, tuple[str, str]] = {}
        for operation in self.operations:
            temp_ref = getattr(operation, "temp_ref", None)
            if temp_ref is None:
                continue
            if temp_ref.entity_id in producers:
                raise ValueError("temporary references must have exactly one producer")
            producers[temp_ref.entity_id] = (
                operation.operation_id,
                str(temp_ref.target_kind),
            )
        dependencies = {
            operation.operation_id: list(operation.depends_on) for operation in self.operations
        }
        for operation_id, dependency_ids in dependencies.items():
            missing = [item for item in dependency_ids if item not in operation_id_set]
            if missing:
                raise ValueError(
                    f"operation {operation_id} depends on missing operations: {', '.join(missing)}"
                )
        ordered = self._stable_dependency_order(dependencies, operation_ids)
        if len(ordered) != len(operation_ids):
            raise ValueError("operation dependency graph contains a cycle")
        for constraint in self.constraints:
            for scope in (constraint.scope, constraint.object_scope):
                if scope is None or scope.kind != "operation_set":
                    continue
                missing = [item for item in scope.operation_ids if item not in operation_id_set]
                if missing:
                    raise ValueError(
                        f"constraint {constraint.constraint_id} references missing operations: "
                        + ", ".join(missing)
                    )
            for scope in (constraint.scope, constraint.object_scope):
                if scope is None:
                    continue
                for ref in scope.entity_refs:
                    if ref.kind != "temporary":
                        continue
                    producer = producers.get(ref.entity_id)
                    if producer is None:
                        raise ValueError(
                            f"constraint {constraint.constraint_id} temporary reference "
                            f"{ref.entity_id} has no producer"
                        )
                    if producer[1] != ref.target_kind:
                        raise ValueError(
                            f"constraint {constraint.constraint_id} temporary reference "
                            f"{ref.entity_id} expects {ref.target_kind} but produces {producer[1]}"
                        )

        ancestors: dict[str, set[str]] = {operation_id: set() for operation_id in operation_ids}
        for operation_id in ordered:
            for dependency_id in dependencies[operation_id]:
                ancestors[operation_id].add(dependency_id)
                ancestors[operation_id].update(ancestors[dependency_id])
        for operation in self.operations:
            for temporary_id, target_kind in self._temporary_consumers(operation):
                producer = producers.get(temporary_id)
                if producer is None:
                    raise ValueError(f"temporary reference {temporary_id} has no producer")
                producer_id, produced_kind = producer
                if target_kind != produced_kind:
                    raise ValueError(
                        f"temporary reference {temporary_id} expects {target_kind} but produces {produced_kind}"
                    )
                if producer_id not in ancestors[operation.operation_id]:
                    raise ValueError(
                        f"operation {operation.operation_id} must depend on producer {producer_id}"
                    )

        unsimulated = {"draft", "simulating"}
        simulated_unapproved = {
            "invalid",
            "needs_user_decision",
            "ready_for_review",
            "rejected",
        }
        approved = {"approved", "applied"}
        if self.status in unsimulated:
            if self.simulation_result_ref is not None or self.approved_revision is not None:
                raise ValueError(
                    f"{self.status} ChangeSets cannot carry simulation or approval evidence"
                )
        elif self.status in simulated_unapproved:
            if self.simulation_result_ref is None or self.approved_revision is not None:
                raise ValueError(
                    f"{self.status} ChangeSets require simulation evidence without approval"
                )
        elif self.status in approved:
            if (
                self.simulation_result_ref is None
                or self.approved_revision != self.revision
            ):
                raise ValueError(
                    f"{self.status} ChangeSets require exact simulation and approval evidence"
                )
        if (self.source_simulation_id is None) != (self.resolution_alternative_id is None):
            raise ValueError("ChangeSet resolution lineage requires simulation and alternative IDs")
        if self.source_simulation_id is not None and self.revision < 2:
            raise ValueError("ChangeSet resolution lineage requires a revised ChangeSet")

        dumped = self.model_dump(mode="json", by_alias=True)
        if self._reference_count(dumped) > MAX_CHANGE_SET_REFERENCES:
            raise ValueError("ChangeSet exceeds the reference budget")
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > MAX_CHANGE_SET_BYTES:
            raise ValueError("ChangeSet exceeds the serialized payload budget")
        return self

    @staticmethod
    def _stable_dependency_order(
        dependencies: dict[str, list[str]], operation_ids: list[str]
    ) -> list[str]:
        remaining = {key: set(value) for key, value in dependencies.items()}
        ordered: list[str] = []
        while remaining:
            ready = [item for item in operation_ids if item in remaining and not remaining[item]]
            if not ready:
                return ordered
            for operation_id in ready:
                ordered.append(operation_id)
                remaining.pop(operation_id)
                for dependency_set in remaining.values():
                    dependency_set.discard(operation_id)
        return ordered

    @staticmethod
    def _temporary_consumers(operation: Operation) -> list[tuple[str, str]]:
        payload = operation.model_dump(mode="json", by_alias=False)
        payload.pop("temp_ref", None)
        found: list[tuple[str, str]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("kind") == "temporary" and value.get("target_kind"):
                    found.append((str(value.get("entity_id")), str(value.get("target_kind"))))
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        return found

    @staticmethod
    def _reference_count(value: Any) -> int:
        count = 0
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
            } and ("entityId" in value or "entity_id" in value):
                count += 1
            count += sum(ChangeSet._reference_count(item) for item in value.values())
        elif isinstance(value, list):
            count += sum(ChangeSet._reference_count(item) for item in value)
        return count

    def dependency_order(self) -> list[str]:
        """Return a stable topological order without rewriting canonical operation order."""

        operation_ids = [operation.operation_id for operation in self.operations]
        return self._stable_dependency_order(
            {operation.operation_id: list(operation.depends_on) for operation in self.operations},
            operation_ids,
        )
