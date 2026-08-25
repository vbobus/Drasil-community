"""Pydantic adapters for checked-in JSON Schema tool definitions."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


def _model_name(value: str) -> str:
    return "".join(part.title() for part in re.split(r"[^A-Za-z0-9]+", value) if part) or "Tool"


def _annotation_from_schema(name: str, schema: dict[str, Any]) -> Any:
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return Literal.__getitem__(tuple(enum_values))
    any_of = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(any_of, list) and any_of:
        annotations = tuple(_annotation_from_schema(f"{name}Choice", item) for item in any_of)
        union = annotations[0]
        for annotation in annotations[1:]:
            union = union | annotation
        return union
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        annotations = [
            type(None) if item == "null" else _annotation_from_schema(name, {**schema, "type": item})
            for item in schema_type
        ]
        union = annotations[0]
        for annotation in annotations[1:]:
            union = union | annotation
        return union
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "null":
        return type(None)
    if schema_type == "array":
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return list[_annotation_from_schema(f"{name}Item", item_schema)]
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        if not isinstance(schema.get("properties"), dict):
            return dict[str, Any]
        return model_from_object_schema(name, schema)
    return Any


def model_from_object_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Create a closed Pydantic input model from a registry object schema."""

    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {item for item in schema.get("required", []) if isinstance(item, str)}
    fields: dict[str, tuple[Any, Any]] = {}
    for property_name, property_schema in properties.items():
        if not isinstance(property_name, str) or not isinstance(property_schema, dict):
            continue
        annotation = _annotation_from_schema(
            f"{_model_name(name)}{_model_name(property_name)}",
            property_schema,
        )
        default: Any = ... if property_name in required else None
        if property_name not in required and type(None) not in getattr(annotation, "__args__", ()):
            annotation = annotation | None
        fields[property_name] = (
            annotation,
            Field(
                default=default,
                min_length=property_schema.get("minLength"),
                max_length=property_schema.get("maxLength"),
                ge=property_schema.get("minimum"),
                le=property_schema.get("maximum"),
                description=property_schema.get("description"),
            ),
        )
    return create_model(
        f"{_model_name(name)}Input",
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        **fields,
    )
