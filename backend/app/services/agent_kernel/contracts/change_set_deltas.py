"""Exact, typed edits used to revise a native ChangeSet."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from .common import ContractId, Revision, RevisionedContract
from .constraints import Constraint
from .operations import Operation


class ChangeSetEditBase(RevisionedContract):
    kind: str
    edit_id: ContractId


class ChangeSetOperationAdd(ChangeSetEditBase):
    kind: Literal["operation.add"] = "operation.add"
    operation: Operation


class ChangeSetOperationReplace(ChangeSetEditBase):
    kind: Literal["operation.replace"] = "operation.replace"
    operation_id: ContractId
    replacement: Operation


class ChangeSetOperationRemove(ChangeSetEditBase):
    kind: Literal["operation.remove"] = "operation.remove"
    operation_id: ContractId


class ChangeSetConstraintAdd(ChangeSetEditBase):
    kind: Literal["constraint.add"] = "constraint.add"
    constraint: Constraint


class ChangeSetConstraintReplace(ChangeSetEditBase):
    kind: Literal["constraint.replace"] = "constraint.replace"
    constraint_id: ContractId
    replacement: Constraint


class ChangeSetConstraintRemove(ChangeSetEditBase):
    kind: Literal["constraint.remove"] = "constraint.remove"
    constraint_id: ContractId


ChangeSetEdit: TypeAlias = Annotated[
    ChangeSetOperationAdd
    | ChangeSetOperationReplace
    | ChangeSetOperationRemove
    | ChangeSetConstraintAdd
    | ChangeSetConstraintReplace
    | ChangeSetConstraintRemove,
    Field(discriminator="kind"),
]


class ChangeSetRevisionDelta(RevisionedContract):
    """A revision-bound edit script; it is never interpreted from a label."""

    expected_change_set_revision: Revision
    edits: list[ChangeSetEdit] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_edits(self) -> "ChangeSetRevisionDelta":
        edit_ids = [edit.edit_id for edit in self.edits]
        if len(edit_ids) != len(set(edit_ids)):
            raise ValueError("ChangeSet delta edit IDs must be unique")
        return self
