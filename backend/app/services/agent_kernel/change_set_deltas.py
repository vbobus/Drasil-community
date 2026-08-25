"""Pure application of stored, exact ChangeSet revision deltas."""

from __future__ import annotations

from app.services.agent_kernel.contracts import (
    ChangeSet,
    ChangeSetConstraintAdd,
    ChangeSetConstraintRemove,
    ChangeSetConstraintReplace,
    ChangeSetOperationAdd,
    ChangeSetOperationRemove,
    ChangeSetOperationReplace,
    ChangeSetRevisionDelta,
)


class ChangeSetDeltaError(ValueError):
    pass


def apply_change_set_delta(
    change_set: ChangeSet,
    delta: ChangeSetRevisionDelta,
    *,
    idempotency_key: str,
    correlation_id: str,
    causation_id: str,
) -> ChangeSet:
    if delta.expected_change_set_revision != change_set.revision:
        raise ChangeSetDeltaError(
            f"stale ChangeSet delta: expected {delta.expected_change_set_revision}, current {change_set.revision}"
        )
    operations = list(change_set.operations)
    constraints = list(change_set.constraints)
    for edit in delta.edits:
        if isinstance(edit, ChangeSetOperationAdd):
            if any(item.operation_id == edit.operation.operation_id for item in operations):
                raise ChangeSetDeltaError("operation.add identity already exists")
            operations.append(edit.operation)
        elif isinstance(edit, ChangeSetOperationReplace):
            matches = [index for index, item in enumerate(operations) if item.operation_id == edit.operation_id]
            if len(matches) != 1:
                raise ChangeSetDeltaError("operation.replace target must exist exactly once")
            if edit.replacement.operation_id != edit.operation_id:
                raise ChangeSetDeltaError("operation.replace cannot rebind operation identity")
            operations[matches[0]] = edit.replacement
        elif isinstance(edit, ChangeSetOperationRemove):
            matches = [index for index, item in enumerate(operations) if item.operation_id == edit.operation_id]
            if len(matches) != 1:
                raise ChangeSetDeltaError("operation.remove target must exist exactly once")
            operations.pop(matches[0])
        elif isinstance(edit, ChangeSetConstraintAdd):
            if any(item.constraint_id == edit.constraint.constraint_id for item in constraints):
                raise ChangeSetDeltaError("constraint.add identity already exists")
            constraints.append(edit.constraint)
        elif isinstance(edit, ChangeSetConstraintReplace):
            matches = [index for index, item in enumerate(constraints) if item.constraint_id == edit.constraint_id]
            if len(matches) != 1:
                raise ChangeSetDeltaError("constraint.replace target must exist exactly once")
            if edit.replacement.constraint_id != edit.constraint_id:
                raise ChangeSetDeltaError("constraint.replace cannot rebind constraint identity")
            constraints[matches[0]] = edit.replacement
        elif isinstance(edit, ChangeSetConstraintRemove):
            matches = [index for index, item in enumerate(constraints) if item.constraint_id == edit.constraint_id]
            if len(matches) != 1:
                raise ChangeSetDeltaError("constraint.remove target must exist exactly once")
            constraints.pop(matches[0])
        else:  # pragma: no cover - discriminated contract makes this unreachable
            raise ChangeSetDeltaError("unsupported ChangeSet delta edit")
    if not operations:
        raise ChangeSetDeltaError("a native ChangeSet cannot be revised to zero operations")
    return ChangeSet.model_validate(
        {
            **change_set.model_dump(mode="python"),
            "revision": change_set.revision + 1,
            "status": "draft",
            "operations": operations,
            "constraints": constraints,
            "simulation_result_ref": None,
            "approved_revision": None,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
        }
    )
