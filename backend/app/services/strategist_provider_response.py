"""Normalize model/provider payloads into the Strategist response shape."""

from __future__ import annotations

from typing import Any

from app.services.strategist_presentation import normalize_summary


def coerce_int_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.isdigit():
            return int(trimmed)
    return None


def first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw.get(key)
    return None


def normalize_agent_clarification(raw_clarification: Any) -> dict[str, Any] | None:
    if isinstance(raw_clarification, str) and raw_clarification.strip():
        message = normalize_summary(raw_clarification, "")
        if not message:
            return None
        return {
            "message": message,
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": [],
        }
    if not isinstance(raw_clarification, dict):
        return None

    message = normalize_summary(
        raw_clarification.get("message")
        or raw_clarification.get("question")
        or raw_clarification.get("summary"),
        "",
    )
    entity_choices = raw_clarification.get("entity_choices") or raw_clarification.get("entityChoices")
    field_choices = raw_clarification.get("field_choices") or raw_clarification.get("fieldChoices")
    scoped_prompts = raw_clarification.get("scoped_follow_up_prompts") or raw_clarification.get("scopedFollowUpPrompts")
    missing_fields = raw_clarification.get("missing_fields") or raw_clarification.get("missingFields")
    generic_options = raw_clarification.get("options")

    def normalize_choices(raw_choices: Any, *, default_kind: str = "entity") -> list[dict[str, Any]]:
        if not isinstance(raw_choices, list):
            return []
        normalized_choices: list[dict[str, Any]] = []
        for index, item in enumerate(raw_choices):
            if isinstance(item, str):
                label = item.strip()
                description = None
                value = label
                target_node_id = None
                follow_up_prompt = label
                choice_kind = default_kind
            elif isinstance(item, dict):
                label = str(item.get("label") or item.get("title") or "").strip()
                description = (
                    str(item["description"]).strip()
                    if isinstance(item.get("description"), str) and str(item["description"]).strip()
                    else None
                )
                value = str(item.get("value") or item.get("id") or label or index).strip()
                target_node_id = coerce_int_id(
                    first_present(
                        item,
                        "target_node_id",
                        "targetNodeId",
                        "target_task_id",
                        "targetTaskId",
                        "task_id",
                        "taskId",
                    )
                )
                choice_kind = str(item.get("kind") or default_kind).strip() or default_kind
                follow_up_prompt = str(
                    item.get("follow_up_prompt")
                    or item.get("followUpPrompt")
                    or label
                ).strip()
            else:
                continue
            if not label:
                continue
            normalized_choices.append(
                {
                    "kind": choice_kind,
                    "label": label,
                    "value": value or label,
                    "description": description,
                    "target_node_id": target_node_id,
                    "follow_up_prompt": follow_up_prompt or label,
                }
            )
        return normalized_choices

    normalized_entity_choices = normalize_choices(entity_choices)
    if not normalized_entity_choices:
        normalized_entity_choices = normalize_choices(generic_options)
    normalized_field_choices = normalize_choices(field_choices, default_kind="field")
    structured_prompt_keys = {
        str(value).strip().casefold()
        for choice in normalized_entity_choices + normalized_field_choices
        for value in (choice.get("label"), choice.get("follow_up_prompt"))
        if isinstance(value, str) and value.strip()
    }
    normalized_scoped_prompts: list[str] = []
    seen_scoped_prompt_keys: set[str] = set()
    if isinstance(scoped_prompts, list):
        for item in scoped_prompts:
            if not isinstance(item, str) or not item.strip():
                continue
            prompt = item.strip()
            prompt_key = prompt.casefold()
            if prompt_key in structured_prompt_keys or prompt_key in seen_scoped_prompt_keys:
                continue
            normalized_scoped_prompts.append(prompt)
            seen_scoped_prompt_keys.add(prompt_key)

    normalized_missing_fields: list[str] = []
    if isinstance(missing_fields, list):
        normalized_missing_fields = [
            str(item).strip()
            for item in missing_fields
            if isinstance(item, (str, int)) and str(item).strip()
        ]

    if not message and not normalized_entity_choices and not normalized_field_choices:
        return None
    return {
        "message": message or "I need one clarification before continuing.",
        "entity_choices": normalized_entity_choices,
        "field_choices": normalized_field_choices,
        "scoped_follow_up_prompts": normalized_scoped_prompts,
        "missing_fields": normalized_missing_fields,
    }


def normalize_provider_response_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    summary = normalize_summary(raw_payload.get("summary"), "")
    if not summary:
        summary = normalize_summary(raw_payload.get("message"), "")
    if not summary:
        summary = normalize_summary(raw_payload.get("answer"), "")
    if not summary:
        recommendation = raw_payload.get("recommendation")
        if isinstance(recommendation, str) and recommendation.strip():
            summary = f"I would go with {recommendation.strip()}."

    known_proposal_present = any(
        isinstance(raw_payload.get(key), dict)
        for key in (
            "action_proposal",
            "actionProposal",
            "metadata_proposal",
            "metadataProposal",
            "branch_proposal",
            "branchProposal",
            "preference_memory_proposal",
            "preferenceMemoryProposal",
        )
    )
    generic_proposal = raw_payload.get("proposal")
    unrecognized_proposal = (
        generic_proposal
        if isinstance(generic_proposal, dict) and not known_proposal_present
        else None
    )
    clarification = normalize_agent_clarification(raw_payload.get("clarification"))
    decision = raw_payload.get("decision")

    return {
        "decision": decision,
        "mode": raw_payload.get("mode"),
        "summary": summary,
        "action_proposal": raw_payload.get("action_proposal") or raw_payload.get("actionProposal"),
        "metadata_proposal": raw_payload.get("metadata_proposal") or raw_payload.get("metadataProposal"),
        "branch_proposal": raw_payload.get("branch_proposal") or raw_payload.get("branchProposal"),
        "preference_memory_proposal": (
            raw_payload.get("preference_memory_proposal")
            or raw_payload.get("preferenceMemoryProposal")
        ),
        "semantic_plan_input": (
            raw_payload.get("semantic_plan_input")
            or raw_payload.get("semanticPlanInput")
        ),
        "unrecognized_proposal": unrecognized_proposal,
        "clarification": clarification,
    }


__all__ = [
    "coerce_int_id",
    "first_present",
    "normalize_agent_clarification",
    "normalize_provider_response_payload",
]
