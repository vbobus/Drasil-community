"""Shared primitives for Agent Kernel v2 contracts."""

from __future__ import annotations

import copy
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, alias_generators


CONTRACT_VERSION = "2.0"

ContractId = Annotated[
    str,
    Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]+$"),
]
Revision = Annotated[int, Field(ge=1)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

class ContractModel(BaseModel):
    """Closed canonical model with consistent Python/JSON naming."""

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
        validate_assignment=True,
        validate_default=True,
        str_strip_whitespace=True,
    )


class VersionedContract(ContractModel):
    contract_version: str = Field(default=CONTRACT_VERSION, pattern=r"^2\.[0-9]+$")


class RevisionedContract(VersionedContract):
    revision: Revision = 1


class TraceIdentity(ContractModel):
    correlation_id: ContractId
    causation_id: ContractId


def strict_provider_schema(source: type[BaseModel] | TypeAdapter[Any]) -> dict[str, Any]:
    """Return a strict-provider projection without changing the canonical model."""

    adapter = source if isinstance(source, TypeAdapter) else TypeAdapter(source)
    schema = copy.deepcopy(adapter.json_schema(by_alias=True, mode="validation"))

    def normalize(value: Any, *, path: str = "$") -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if isinstance(value.get("additionalProperties"), dict):
                raise ValueError(
                    f"Canonical schema at {path} uses a free-form map that cannot be "
                    "represented by the strict provider subset without semantic loss."
                )
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["type"] = "object"
                value["additionalProperties"] = False
                value["required"] = list(properties)
            for key, nested in value.items():
                normalize(nested, path=f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                normalize(nested, path=f"{path}[{index}]")

    normalize(schema)
    return schema
