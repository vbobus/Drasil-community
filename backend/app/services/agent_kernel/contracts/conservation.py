"""Closed conservation evidence returned by native ChangeSet authoring."""

from __future__ import annotations

from pydantic import Field

from .common import ContractModel


class OperationFingerprint(ContractModel):
    operation_id: str
    kind: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class OperationConservationReport(ContractModel):
    complete: bool
    covered_clause_ids: list[str] = Field(default_factory=list)
    unresolved_clause_ids: list[str] = Field(default_factory=list)
    unsupported_clause_ids: list[str] = Field(default_factory=list)
    failed_clause_ids: list[str] = Field(default_factory=list)
    extra_operation_ids: list[str] = Field(default_factory=list)
    operation_fingerprints: list[OperationFingerprint] = Field(default_factory=list)
