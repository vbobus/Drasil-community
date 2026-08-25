"""Structured response schemas and JSON parsing for Strategist providers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

STRATEGIST_TOOL_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "final"]},
        "summary": {"type": "string"},
        "final_payload_json": {"type": "string"},
        "requested_tool": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "arguments_json": {"type": "string"},
            },
            "required": ["name", "arguments_json"],
        },
    },
    "required": ["kind", "summary", "final_payload_json", "requested_tool"],
}

_METADATA_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "priority": {"type": ["string", "null"]},
        "deadline_at": {"type": ["string", "null"]},
        "time_estimate_minutes": {"type": ["integer", "null"]},
        "tags": {"type": ["array", "null"], "items": {"type": "string"}},
        "is_pinned": {"type": ["boolean", "null"]},
        "node_type": {"type": ["string", "null"]},
    },
    "required": [
        "title",
        "notes",
        "status",
        "priority",
        "deadline_at",
        "time_estimate_minutes",
        "tags",
        "is_pinned",
        "node_type",
    ],
}

_METADATA_UPDATE_REQUIRED: list[str] = [
    "title",
    "notes",
    "status",
    "priority",
    "deadline_at",
    "time_estimate_minutes",
    "tags",
    "is_pinned",
    "node_type",
]
_BRANCH_STRUCTURAL_OPERATION_REQUIRED: list[str] = [
    "kind",
    "node_id",
    "node_title",
    "title",
    "temp_id",
    "category_id",
    "parent_id",
    "parent_temp_id",
    "parent_title",
    "destination_parent_id",
    "destination_parent_temp_id",
    "destination_parent_title",
    "target_index",
    "notes",
    "status",
    "priority",
    "deadline_at",
    "time_estimate_minutes",
    "node_type",
    "emoji",
    "tags",
    "is_pinned",
    "is_backlog",
    "updates",
]
_BRANCH_PROPOSAL_REQUIRED: list[str] = [
    "proposal_kind",
    "summary",
    "target_node_id",
    "target_category_id",
    "target_node_title",
    "target_node_ids",
    "subtree_root_ids",
    "rationale",
    "proposed_children",
    "structural_operations",
    "validation_warnings",
]

STRATEGIST_DIRECT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["conversation", "analysis", "mutation", "clarification"],
                },
                "reason": {"type": "string"},
            },
            "required": ["kind", "reason"],
        },
        "summary": {"type": "string"},
        "action_proposal": {"type": "null"},
        "metadata_proposal": {"type": "null"},
        "branch_proposal": {"type": "null"},
        "preference_memory_proposal": {"type": "null"},
        "clarification": {"type": "null"},
    },
    "required": [
        "decision",
        "summary",
        "action_proposal",
        "metadata_proposal",
        "branch_proposal",
        "preference_memory_proposal",
        "clarification",
    ],
}

_CLARIFICATION_CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string"},
        "label": {"type": "string"},
        "value": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "target_node_id": {"type": ["integer", "null"]},
        "follow_up_prompt": {"type": "string"},
    },
    "required": [
        "kind",
        "label",
        "value",
        "description",
        "target_node_id",
        "follow_up_prompt",
    ],
}

STRATEGIST_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["conversation", "analysis", "mutation", "clarification"],
                },
                "reason": {"type": "string"},
            },
            "required": ["kind", "reason"],
        },
        "summary": {"type": "string"},
        "action_proposal": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string"},
                "target_node_id": {"type": "integer"},
                "target_node_title": {"type": "string"},
                "confirmation_title": {"type": "string"},
                "confirmation_body": {"type": "string"},
                "confirm_label": {"type": "string"},
                "cancel_label": {"type": "string"},
                "consequences": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "kind",
                "target_node_id",
                "target_node_title",
                "confirmation_title",
                "confirmation_body",
                "confirm_label",
                "cancel_label",
                "consequences",
            ],
        },
        "metadata_proposal": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "summary": {"type": ["string", "null"]},
                "rationale": {"type": "string"},
                "target_node_id": {"type": ["integer", "null"]},
                "target_node_title": {"type": ["string", "null"]},
                "target_node_ids": {"type": "array", "items": {"type": "integer"}},
                "updates": _METADATA_UPDATE_SCHEMA,
                "target_updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "target_node_id": {"type": "integer"},
                            "target_node_title": {"type": "string"},
                            "updates": _METADATA_UPDATE_SCHEMA,
                        },
                        "required": ["target_node_id", "target_node_title", "updates"],
                    },
                },
                "preview": {"type": "array", "items": {"type": "string"}},
                "validation_warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "summary",
                "rationale",
                "target_node_id",
                "target_node_title",
                "target_node_ids",
                "updates",
                "target_updates",
                "preview",
                "validation_warnings",
            ],
        },
        "branch_proposal": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "proposal_kind": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
                "target_node_id": {"type": ["integer", "null"]},
                "target_category_id": {"type": ["integer", "null"]},
                "target_node_title": {"type": ["string", "null"]},
                "target_node_ids": {"type": "array", "items": {"type": "integer"}},
                "subtree_root_ids": {"type": "array", "items": {"type": "integer"}},
                "rationale": {"type": "string"},
                "proposed_children": {"type": "array", "items": {"type": "string"}},
                "structural_operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {"type": "string"},
                            "node_id": {"type": ["integer", "null"]},
                            "node_title": {"type": ["string", "null"]},
                            "title": {"type": ["string", "null"]},
                            "temp_id": {"type": ["string", "null"]},
                            "category_id": {"type": ["integer", "null"]},
                            "parent_id": {"type": ["integer", "null"]},
                            "parent_temp_id": {"type": ["string", "null"]},
                            "parent_title": {"type": ["string", "null"]},
                            "destination_parent_id": {"type": ["integer", "null"]},
                            "destination_parent_temp_id": {"type": ["string", "null"]},
                            "destination_parent_title": {"type": ["string", "null"]},
                            "target_index": {"type": ["integer", "null"]},
                            "notes": {"type": ["string", "null"]},
                            "status": {"type": ["string", "null"]},
                            "priority": {"type": ["string", "null"]},
                            "deadline_at": {"type": ["string", "null"]},
                            "time_estimate_minutes": {"type": ["integer", "null"]},
                            "node_type": {"type": ["string", "null"]},
                            "emoji": {"type": ["string", "null"]},
                            "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                            "is_pinned": {"type": ["boolean", "null"]},
                            "is_backlog": {"type": ["boolean", "null"]},
                            "updates": {
                                "type": ["object", "null"],
                                "additionalProperties": False,
                                "properties": dict(_METADATA_UPDATE_SCHEMA["properties"]),
                                "required": _METADATA_UPDATE_REQUIRED,
                            },
                        },
                        "required": _BRANCH_STRUCTURAL_OPERATION_REQUIRED,
                    },
                },
                "validation_warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": _BRANCH_PROPOSAL_REQUIRED,
        },
        "preference_memory_proposal": {"type": "null"},
        "clarification": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "message": {"type": "string"},
                "entity_choices": {"type": "array", "items": _CLARIFICATION_CHOICE_SCHEMA},
                "field_choices": {"type": "array", "items": _CLARIFICATION_CHOICE_SCHEMA},
                "scoped_follow_up_prompts": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "message",
                "entity_choices",
                "field_choices",
                "scoped_follow_up_prompts",
            ],
        },
    },
    "required": [
        "decision",
        "summary",
        "action_proposal",
        "metadata_proposal",
        "branch_proposal",
        "preference_memory_proposal",
        "clarification",
    ],
}


def parse_json_object_text(raw_text: str) -> dict[str, Any]:
    candidates = [raw_text.strip()]
    fenced_match = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if fenced_match not in candidates:
        candidates.append(fenced_match)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise HTTPException(status_code=502, detail="Strategist returned invalid JSON.")


__all__ = [
    "STRATEGIST_DIRECT_RESPONSE_SCHEMA",
    "STRATEGIST_JSON_SCHEMA",
    "STRATEGIST_TOOL_STEP_SCHEMA",
    "parse_json_object_text",
]
