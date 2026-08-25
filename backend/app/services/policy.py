"""
Strategist response policy and clarification helpers.
"""

from __future__ import annotations

from typing import Any

from app.services.intent_router import detect_requested_fields

FIELD_LABELS = {
    "deadline_at": "Deadline",
    "priority": "Priority",
    "time_estimate_minutes": "Estimate",
    "notes": "Notes",
    "tags": "Tags",
    "is_pinned": "Pin state",
    "title": "Title",
    "status": "Status",
}


def build_entity_follow_up_prompt(prompt: str, title: str) -> str:
    prompt_norm = " ".join(prompt.strip().lower().split())
    if any(
        token in prompt_norm
        for token in ("update", "set", "change", "rename", "retitle", "pin", "unpin", "mark")
    ):
        return f"Update {title}"
    if any(
        token in prompt_norm
        for token in ("break down", "breakdown", "subtask", "children", "restructure")
    ):
        return f"Break down {title}"
    if any(
        token in prompt_norm
        for token in ("deadline", "due", "priority", "status", "estimate", "blocking", "what")
    ) or prompt_norm.endswith("?"):
        return f"Tell me about {title}"
    return f"Use {title}"


def build_entity_choices(
    candidate_entities: list[dict[str, Any]],
    *,
    prompt: str,
) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for candidate in candidate_entities[:4]:
        task = candidate.get("task")
        if not isinstance(task, dict):
            continue
        title = str(task.get("title") or "").strip()
        node_id = task.get("id")
        if not title or not isinstance(node_id, int):
            continue
        deadline = str(task.get("deadline") or "No deadline")
        priority = str(task.get("priority_label") or task.get("priority") or "Unknown priority")
        category = str(task.get("category") or "Unknown")
        follow_up_prompt = build_entity_follow_up_prompt(prompt, title)
        choices.append(
            {
                "kind": "entity",
                "label": title,
                "value": title,
                "description": f"{category} | {priority} | {deadline}",
                "target_node_id": node_id,
                "follow_up_prompt": follow_up_prompt,
            }
        )
    return choices


def build_field_choices(
    target_title: str | None = None,
    *,
    target_node_id: int | None = None,
) -> list[dict[str, Any]]:
    prefix = f"Update {target_title}" if target_title else "Update that task"
    return [
        {
            "kind": "field",
            "label": label,
            "value": field_name,
            "description": f"Clarify the {label.lower()} change first.",
            "target_node_id": target_node_id,
            "follow_up_prompt": f"{prefix} {label.lower()}",
        }
        for field_name, label in FIELD_LABELS.items()
    ]


def build_clarification_payload(
    *,
    prompt: str,
    candidate_entities: list[dict[str, Any]],
    include_field_choices: bool,
    resolved_entity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_title = None
    resolved_node_id = None
    if isinstance(resolved_entity, dict):
        if isinstance(resolved_entity.get("title"), str):
            resolved_title = str(resolved_entity["title"]).strip() or None
        if isinstance(resolved_entity.get("node_id"), int):
            resolved_node_id = int(resolved_entity["node_id"])

    entity_choices = build_entity_choices(candidate_entities, prompt=prompt)
    if resolved_title and include_field_choices:
        entity_choices = []

    target_title = resolved_title
    target_node_id = resolved_node_id
    if entity_choices:
        target_title = entity_choices[0]["label"]
        target_node_id = entity_choices[0]["target_node_id"]
    message = "I need one quick clarification before I make that call."
    if entity_choices and include_field_choices:
        message = "I found more than one likely task, and I also need the field you want to change."
    elif entity_choices:
        message = "I found more than one likely task. Pick the right one before I continue."
    elif include_field_choices:
        if target_title:
            message = f"Tell me which field you want to change for {target_title}."
        else:
            message = "Tell me which field you want to change before I prepare a patch."
    scoped_prompts = [choice["follow_up_prompt"] for choice in entity_choices[:3]]
    return {
        "message": message,
        "entity_choices": entity_choices,
        "field_choices": (
            build_field_choices(target_title, target_node_id=target_node_id)
            if include_field_choices
            else []
        ),
        "scoped_follow_up_prompts": scoped_prompts,
    }


def choose_policy(
    *,
    prompt: str,
    mode: str,
    intent_name: str,
    retrieval_result: dict[str, Any],
) -> dict[str, Any]:
    requested_fields = detect_requested_fields(prompt)
    candidate_entities = retrieval_result.get("candidate_entities", [])
    resolved_entities = retrieval_result.get("resolved_entities", [])
    overall_confidence = float(retrieval_result.get("overall_confidence") or 0.0)
    ambiguous = bool(retrieval_result.get("ambiguous"))
    has_entity_reference = bool(retrieval_result.get("has_entity_reference"))
    resolved_entity = None
    if (
        not ambiguous
        and isinstance(resolved_entities, list)
        and len(resolved_entities) == 1
        and isinstance(resolved_entities[0], dict)
    ):
        resolved_entity = resolved_entities[0]
    strong_single_resolution = (
        isinstance(resolved_entities, list)
        and len(resolved_entities) == 1
        and isinstance(resolved_entities[0], dict)
        and float(resolved_entities[0].get("confidence") or 0.0) >= 0.9
    )

    if intent_name == "open_strategy_chat":
        return {
            "policy": "answer_direct",
            "allow_provider": False,
            "reason": "Open-ended strategy conversation does not require a task target.",
            "clarification": None,
        }
    if intent_name == "metadata_gap_review":
        return {
            "policy": "answer_direct",
            "allow_provider": False,
            "reason": "Broad missing-metadata audit should not require one task target.",
            "clarification": None,
        }
    if intent_name == "graph_inspection":
        return {
            "policy": "answer_direct",
            "allow_provider": False,
            "reason": "Broad graph inspection should use read-only context without requiring one task target.",
            "clarification": None,
        }
    if intent_name == "strategic_synthesis":
        return {
            "policy": "answer_direct",
            "allow_provider": False,
            "reason": "Decision-support synthesis should rank evidence without requiring one task target.",
            "clarification": None,
        }

    if mode == "metadata":
        if ambiguous:
            return {
                "policy": "clarify",
                "allow_provider": False,
                "reason": "Ambiguous metadata target.",
                "clarification": build_clarification_payload(
                    prompt=prompt,
                    candidate_entities=candidate_entities,
                    include_field_choices=False,
                ),
            }
        if not requested_fields:
            return {
                "policy": "clarify",
                "allow_provider": False,
                "reason": "Metadata field missing.",
                "clarification": build_clarification_payload(
                    prompt=prompt,
                    candidate_entities=candidate_entities,
                    include_field_choices=True,
                    resolved_entity=resolved_entity,
                ),
            }
        if has_entity_reference and not resolved_entities:
            return {
                "policy": "not_found",
                "allow_provider": False,
                "reason": "Could not resolve metadata target.",
                "clarification": None,
            }
        return {
            "policy": "proposal",
            "allow_provider": True,
            "reason": "Safe metadata proposal allowed.",
            "clarification": None,
        }

    if mode == "action":
        return {
            "policy": "proposal",
            "allow_provider": False,
            "reason": "Action flow is app-owned.",
            "clarification": None,
        }

    if has_entity_reference and not resolved_entities:
        return {
            "policy": "not_found",
            "allow_provider": False,
            "reason": "No confident entity match.",
            "clarification": None,
        }

    if (
        ambiguous
        and mode in {"review", "branch"}
        and intent_name not in {"workload_review", "strategic_synthesis", "list_query", "graph_inspection"}
        and not strong_single_resolution
    ):
        return {
            "policy": "clarify",
            "allow_provider": False,
            "reason": "Ambiguous entity reference.",
            "clarification": build_clarification_payload(
                prompt=prompt,
                candidate_entities=candidate_entities,
                include_field_choices=False,
            ),
        }

    if (
        has_entity_reference
        and overall_confidence < 0.56
        and intent_name not in {"workload_review", "strategic_synthesis", "list_query", "graph_inspection"}
    ):
        if intent_name == "retrieve_fact" and overall_confidence < 0.5:
            return {
                "policy": "not_found",
                "allow_provider": False,
                "reason": "Low-confidence fact lookup did not match a task.",
                "clarification": None,
            }
        return {
            "policy": "clarify",
            "allow_provider": False,
            "reason": "Low-confidence entity resolution.",
            "clarification": build_clarification_payload(
                prompt=prompt,
                candidate_entities=candidate_entities,
                include_field_choices=False,
            ),
        }

    if mode == "branch":
        return {
            "policy": "proposal",
            "allow_provider": True,
            "reason": "Branch planning can continue.",
            "clarification": None,
        }

    return {
        "policy": "answer_direct",
        "allow_provider": False,
        "reason": "Direct deterministic answer is safe.",
        "clarification": None,
    }
