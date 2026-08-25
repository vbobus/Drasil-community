"""
Read-only Strategist retrieval worker.

This wraps the existing deterministic retrieval helpers in a more explicit
retrieval-plan / evidence-bundle shape so the LangGraph runtime can reason
about evidence instead of only a guessed entity list.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import re
from typing import Any, Literal

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.intent_router import is_task_list_query_prompt
from app.services.retrieval import (
    base_match_score,
    build_node_lookup,
    extract_aliases,
    get_children,
    get_recent_session_actions,
    get_parent_chain,
    get_related_entities,
    get_siblings,
    get_subtree,
    get_task_metadata,
    get_task_notes,
    get_task_snapshot,
    get_workload_overview,
    normalize_text,
    resolve_entities,
    search_entities,
    search_notes,
    serialize_task,
)


@dataclass
class RetrievalPlan:
    purpose: Literal[
        "situational_awareness",
        "target_resolution",
        "branch_understanding",
        "comparison",
        "proposal_grounding",
        "idea_grounding",
    ]
    scope_hints: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    must_resolve_exact_entity: bool = False
    allow_hypothetical: bool = False
    stop_rule: Literal[
        "enough_to_answer",
        "enough_to_propose",
        "top_candidates_ready",
        "insufficient_evidence",
    ] = "enough_to_answer"


@dataclass
class EvidenceReference:
    source: str
    detail: str
    task_id: int | None = None
    task_title: str | None = None
    relationship: str | None = None
    score: float | None = None


@dataclass
class EvidenceBundle:
    resolution: Literal[
        "resolved_entity",
        "entity_set",
        "ambiguous_candidates",
        "hypothetical_concept",
        "no_match",
    ]
    resolved_entity_ids: list[int] = field(default_factory=list)
    candidate_entity_ids: list[int] = field(default_factory=list)
    relevant_subtree_root_ids: list[int] = field(default_factory=list)
    note_evidence: list[dict[str, Any]] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)
    recent_action_evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    confidence_reasons: list[str] = field(default_factory=list)
    unresolved_gaps: list[str] = field(default_factory=list)


def _append_tool(
    tool_trace: list[dict[str, Any]],
    *,
    name: str,
    arguments: dict[str, Any],
    result: Any,
) -> Any:
    tool_trace.append(
        {
            "name": name,
            "arguments": arguments,
            "result": result,
        }
    )
    return result


def _evidence_ref(
    *,
    source: str,
    detail: str,
    task_id: int | None = None,
    task_title: str | None = None,
    relationship: str | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    return asdict(
        EvidenceReference(
            source=source,
            detail=detail,
            task_id=task_id,
            task_title=task_title,
            relationship=relationship,
            score=score,
        )
    )


def build_retrieval_plan(
    *,
    intent_name: str,
    mode: str,
    requested_fields: list[str],
    selected_node_id: int | None,
    has_working_set: bool,
) -> RetrievalPlan:
    if intent_name in {"workload_review", "strategic_synthesis", "list_query", "metadata_gap_review", "graph_inspection"}:
        return RetrievalPlan(
            purpose="situational_awareness",
            scope_hints=[
                "working_set" if has_working_set else "global",
                "global",
            ],
            required_evidence=["snapshot", "recent_edits", "references"],
            must_resolve_exact_entity=False,
            stop_rule="enough_to_answer",
        )
    if intent_name == "compare_entities":
        return RetrievalPlan(
            purpose="comparison",
            scope_hints=[
                "selected_ids" if selected_node_id is not None else "working_set",
                "global",
                "notes",
            ],
            required_evidence=["snapshot", "notes", "parent_chain", "siblings"],
            must_resolve_exact_entity=False,
            stop_rule="top_candidates_ready",
        )
    if mode in {"metadata", "action", "branch"}:
        return RetrievalPlan(
            purpose="proposal_grounding" if mode != "branch" else "branch_understanding",
            scope_hints=[
                "selected_ids" if selected_node_id is not None else "working_set",
                "branch",
                "notes",
            ],
            required_evidence=(
                ["snapshot", "notes", "parent_chain", "children", "siblings"]
                if mode == "branch"
                else ["snapshot", "notes", "metadata"]
            ),
            must_resolve_exact_entity=True,
            allow_hypothetical=mode == "branch",
            stop_rule="enough_to_propose",
        )
    required_evidence = ["snapshot"]
    if requested_fields:
        required_evidence.append("metadata")
    return RetrievalPlan(
        purpose="target_resolution",
        scope_hints=[
            "selected_ids" if selected_node_id is not None else "working_set",
            "notes",
            "global",
        ],
        required_evidence=required_evidence,
        must_resolve_exact_entity=False,
        stop_rule="enough_to_answer",
    )


def _selected_override_result(
    *,
    selected_node: StrategyNode,
    categories: list[Category],
) -> dict[str, Any]:
    category_name_by_id = {category.id: category.name for category in categories if category.id is not None}
    return {
        "has_entity_reference": True,
        "candidate_entities": [
            {
                "node_id": selected_node.id,
                "title": selected_node.title,
                "score": 1.0,
                "reasons": ["explicit UI selection"],
                "category_id": selected_node.category_id,
                "task": serialize_task(selected_node, category_name_by_id=category_name_by_id),
            }
        ],
        "resolved_entities": [
            {
                "node_id": selected_node.id,
                "title": selected_node.title,
                "role": "primary",
                "confidence": 1.0,
                "source": "ui_selection",
                "mention_text": selected_node.title,
            }
        ],
        "overall_confidence": 1.0,
        "ambiguous": False,
        "confidence_gap": 1.0,
    }


def _is_follow_up_phrase(prompt: str) -> bool:
    prompt_norm = normalize_text(prompt)
    if not prompt_norm:
        return False
    tokens = set(prompt_norm.split())
    return bool(
        tokens.intersection(
            {
                "it",
                "its",
                "that",
                "this",
                "there",
                "them",
                "those",
                "one",
                "breakdown",
            }
        )
    )


def _is_hypothetical_prompt(prompt: str, mode: str) -> bool:
    prompt_norm = normalize_text(prompt)
    if not prompt_norm:
        return False
    if any(
        phrase in prompt_norm
        for phrase in (
            "create ",
            "add ",
            "new task",
            "new subtask",
            "new branch",
            "new plan",
            "need a ",
            "need an ",
            "idea for ",
        )
    ):
        return True
    return mode == "branch" and any(
        phrase in prompt_norm
        for phrase in (
            "summer outreach plan",
            "outreach plan",
            "someday maybe",
        )
    )


def _is_standalone_hypothetical_creation(
    prompt: str,
    *,
    mode: str,
    selected_node_id: int | None,
    seed_node_ids: list[int],
    working_set: list[dict[str, Any]],
) -> bool:
    """Distinguish a new concept from a mutation beneath an existing task."""

    if (
        mode != "branch"
        or selected_node_id is not None
        or seed_node_ids
        or working_set
        or not _is_hypothetical_prompt(prompt, mode)
    ):
        return False
    normalized = normalize_text(prompt)
    return not bool(
        re.search(
            r"\b(?:subtask|child|branch)\s+(?:under|inside|within|of|for)\b"
            r"|\b(?:under|inside|within)\s+(?:the\s+)?(?:task|plan|project|branch)\b",
            normalized,
        )
    )


def _bounded_search_query_from_prompt(
    *,
    prompt: str,
    nodes: list[StrategyNode],
) -> str:
    prompt_text = " ".join(str(prompt or "").strip().split())
    if not prompt_text:
        return ""

    quoted: list[str] = []
    for quote in ('"', "'"):
        parts = prompt_text.split(quote)
        quoted.extend(
            part.strip()
            for index, part in enumerate(parts)
            if index % 2 == 1 and part.strip()
        )
    if quoted:
        return max(quoted, key=len)[:120]

    prompt_norm = normalize_text(prompt_text)
    title_matches = [
        str(node.title).strip()
        for node in nodes
        if str(node.title or "").strip()
        and normalize_text(str(node.title)) in prompt_norm
    ]
    if title_matches:
        return max(title_matches, key=len)[:120]

    lowered = prompt_text.lower()
    for marker in (" due date of ", " deadline of ", " priority of ", " status of ", " estimate of "):
        marker_index = lowered.find(marker)
        if marker_index >= 0:
            candidate = prompt_text[marker_index + len(marker):].strip()
            for stop_word in (" to ", " by ", " on ", " for "):
                stop_index = candidate.lower().find(stop_word)
                if stop_index > 0:
                    candidate = candidate[:stop_index].strip()
                    break
            if candidate:
                return candidate[:120]

    return prompt_text[:160]


def _should_skip_retrieval(
    *,
    prompt: str,
    intent_name: str,
    mode: str,
    requested_fields: list[str],
    selected_node_id: int | None,
    seed_node_ids: list[int],
    working_set: list[dict[str, Any]],
) -> bool:
    prompt_norm = normalize_text(prompt)
    if not prompt_norm:
        return True
    if intent_name in {"confirm_proposal", "open_strategy_chat"}:
        return True
    if (
        (
            intent_name in {"graph_inspection", "metadata_gap_review", "workload_review", "strategic_synthesis"}
            or (intent_name == "list_query" and is_task_list_query_prompt(prompt))
        )
        and selected_node_id is None
        and not seed_node_ids
        and not working_set
    ):
        return True
    if (
        mode == "review"
        and intent_name == "unknown"
        and not requested_fields
        and selected_node_id is None
        and not seed_node_ids
        and not working_set
    ):
        return True
    return False


def _build_seed_candidates(
    *,
    prompt: str,
    seed_node_ids: list[int],
    categories: list[Category],
    nodes: list[StrategyNode],
    working_set: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prompt_norm = normalize_text(prompt)
    follow_up_phrase = _is_follow_up_phrase(prompt)
    category_name_by_id = {
        category.id: category.name for category in categories if category.id is not None
    }
    node_lookup = build_node_lookup(nodes)
    working_set_by_id = {
        int(item["node_id"]): item
        for item in working_set
        if isinstance(item, dict) and isinstance(item.get("node_id"), int)
    }
    candidates: list[dict[str, Any]] = []
    for node_id in seed_node_ids:
        node = node_lookup.get(node_id)
        if node is None:
            continue
        phrases = [node.title, *extract_aliases(node), node.notes or ""]
        best_score = 0.0
        reasons: list[str] = ["hard-signal seed"]
        for phrase in phrases:
            phrase_norm = normalize_text(phrase)
            score = base_match_score(prompt_norm, phrase_norm)
            if score > best_score:
                best_score = score
        if follow_up_phrase:
            best_score = max(best_score, 0.82)
            reasons.append("follow-up phrasing matched the seeded target")
        elif best_score > 0.0:
            best_score = min(0.96, best_score + 0.1)
            reasons.append("matched seeded title, alias, or note text")
        salience = float(working_set_by_id.get(node_id, {}).get("salience") or 0.0)
        if salience > 0.0:
            best_score = max(best_score, min(0.92, 0.68 + salience * 0.24))
            reasons.append("recent session salience boosted the seed")
        if best_score <= 0.0:
            continue
        candidates.append(
            {
                "node_id": node.id,
                "title": node.title,
                "score": round(best_score, 4),
                "reasons": reasons,
                "category_id": node.category_id,
                "task": serialize_task(node, category_name_by_id=category_name_by_id, node_lookup=node_lookup),
            }
        )
    candidates.sort(key=lambda item: float(item["score"]), reverse=True)
    return candidates


def _merge_candidates(
    *,
    search_matches: list[dict[str, Any]],
    seed_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}
    for candidate in [*search_matches, *seed_candidates]:
        node_id = candidate.get("node_id")
        if not isinstance(node_id, int):
            continue
        existing = merged.get(node_id)
        if existing is None:
            merged[node_id] = dict(candidate)
            continue
        merged[node_id]["score"] = max(
            float(existing.get("score") or 0.0),
            float(candidate.get("score") or 0.0),
        )
        merged[node_id]["reasons"] = [
            *[
                reason
                for reason in existing.get("reasons", [])
                if isinstance(reason, str)
            ],
            *[
                reason
                for reason in candidate.get("reasons", [])
                if isinstance(reason, str) and reason not in existing.get("reasons", [])
            ],
        ]
        if not isinstance(merged[node_id].get("task"), dict) and isinstance(candidate.get("task"), dict):
            merged[node_id]["task"] = candidate["task"]
    ordered = list(merged.values())
    ordered.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return ordered


def run_retrieval_worker(
    *,
    prompt: str,
    intent_name: str,
    mode: str,
    requested_fields: list[str],
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
    selected_node_id: int | None = None,
    seed_node_ids: list[int] | None = None,
    today_value: Any | None = None,
) -> dict[str, Any]:
    working_set = working_set or []
    seed_node_ids = [node_id for node_id in (seed_node_ids or []) if isinstance(node_id, int)]
    effective_today = today_value if isinstance(today_value, date) else date.today()
    tool_trace: list[dict[str, Any]] = []
    retrieval_plan = build_retrieval_plan(
        intent_name=intent_name,
        mode=mode,
        requested_fields=requested_fields,
        selected_node_id=selected_node_id,
        has_working_set=bool(working_set),
    )
    node_lookup = build_node_lookup(nodes)
    selected_node = node_lookup.get(selected_node_id) if selected_node_id is not None else None
    skip_retrieval = _should_skip_retrieval(
        prompt=prompt,
        intent_name=intent_name,
        mode=mode,
        requested_fields=requested_fields,
        selected_node_id=selected_node_id,
        seed_node_ids=seed_node_ids,
        working_set=working_set,
    )

    if skip_retrieval:
        retrieval_result = {
            "has_entity_reference": False,
            "candidate_entities": [],
            "resolved_entities": [],
            "overall_confidence": 0.0,
            "ambiguous": False,
            "confidence_gap": None,
            "resolution": "no_match",
            "skipped": True,
        }
    elif selected_node is not None:
        retrieval_result = _selected_override_result(
            selected_node=selected_node,
            categories=categories,
        )
        retrieval_result["resolution"] = "resolved_entity"
        retrieval_result["skipped"] = False
    else:
        search_query = _bounded_search_query_from_prompt(prompt=prompt, nodes=nodes)
        search_matches = _append_tool(
            tool_trace,
            name="search_entities",
            arguments={
                "query": search_query,
                "original_prompt_chars": len(prompt),
                "query_policy": "bounded_title_or_phrase",
                "limit": 6,
            },
            result=search_entities(
                query=search_query,
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                working_set=working_set,
                requested_fields=requested_fields,
                limit=6,
            ),
        )
        seed_candidates = _build_seed_candidates(
            prompt=prompt,
            seed_node_ids=seed_node_ids,
            categories=categories,
            nodes=nodes,
            working_set=working_set,
        )
        retrieval_result = resolve_entities(
            prompt=prompt,
            intent_name=intent_name,
            requested_fields=requested_fields,
            categories=categories,
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            working_set=working_set,
        )
        merged_candidates = _merge_candidates(
            search_matches=(
                search_matches if isinstance(search_matches, list) else []
            ),
            seed_candidates=seed_candidates,
        )
        if merged_candidates:
            retrieval_result["candidate_entities"] = merged_candidates
            existing_resolved = (
                retrieval_result.get("resolved_entities", [])
                if isinstance(retrieval_result.get("resolved_entities"), list)
                else []
            )
            if not existing_resolved:
                if intent_name == "compare_entities" and len(merged_candidates) >= 2:
                    retrieval_result["resolved_entities"] = [
                        {
                            "node_id": candidate["node_id"],
                            "title": candidate["title"],
                            "role": "primary" if index == 0 else "comparison",
                            "confidence": float(candidate["score"]),
                            "source": "seeded_retrieval",
                            "mention_text": prompt.strip(),
                        }
                        for index, candidate in enumerate(merged_candidates[:2])
                    ]
                elif (
                    _is_follow_up_phrase(prompt)
                    or float(merged_candidates[0].get("score") or 0.0) >= 0.82
                ):
                    top_candidate = merged_candidates[0]
                    retrieval_result["resolved_entities"] = [
                        {
                            "node_id": top_candidate["node_id"],
                            "title": top_candidate["title"],
                            "role": "primary",
                            "confidence": float(top_candidate["score"]),
                            "source": "seeded_retrieval",
                            "mention_text": prompt.strip(),
                        }
                    ]
            top_score = float(merged_candidates[0].get("score") or 0.0)
            second_score = (
                float(merged_candidates[1].get("score") or 0.0)
                if len(merged_candidates) > 1
                else 0.0
            )
            retrieval_result["overall_confidence"] = max(
                float(retrieval_result.get("overall_confidence") or 0.0),
                top_score,
            )
            retrieval_result["confidence_gap"] = round(top_score - second_score, 4)
            if (
                not retrieval_result.get("ambiguous")
                and intent_name not in {"workload_review", "strategic_synthesis", "list_query", "compare_entities"}
                and len(merged_candidates) > 1
                and top_score >= 0.7
                and second_score >= 0.66
                and (top_score - second_score) < 0.16
                and selected_node is None
            ):
                retrieval_result["ambiguous"] = True
        retrieval_result["skipped"] = False

    if skip_retrieval:
        note_matches: list[dict[str, Any]] = []
    else:
        search_query = _bounded_search_query_from_prompt(prompt=prompt, nodes=nodes)
        note_matches = _append_tool(
            tool_trace,
            name="search_notes",
            arguments={
                "query": search_query,
                "original_prompt_chars": len(prompt),
                "query_policy": "bounded_title_or_phrase",
                "limit": 4,
            },
            result=search_notes(
                query=search_query,
                categories=categories,
                nodes=nodes,
                limit=4,
            ),
        )

    standalone_hypothetical = _is_standalone_hypothetical_creation(
        prompt,
        mode=mode,
        selected_node_id=selected_node_id,
        seed_node_ids=seed_node_ids,
        working_set=working_set,
    )
    if standalone_hypothetical:
        # Similar words in an existing title are discovery hints, not authority
        # to turn an explicit create-new request into an existing-task mutation.
        retrieval_result["resolved_entities"] = []
        retrieval_result["candidate_entities"] = []
        retrieval_result["overall_confidence"] = 0.0
        retrieval_result["ambiguous"] = False

    resolved_entities = (
        retrieval_result.get("resolved_entities", [])
        if isinstance(retrieval_result.get("resolved_entities"), list)
        else []
    )
    candidate_entities = (
        retrieval_result.get("candidate_entities", [])
        if isinstance(retrieval_result.get("candidate_entities"), list)
        else []
    )
    resolved_entity_ids = [
        int(item["node_id"])
        for item in resolved_entities
        if isinstance(item, dict) and isinstance(item.get("node_id"), int)
    ]
    candidate_entity_ids = [
        int(item["node_id"])
        for item in candidate_entities
        if isinstance(item, dict) and isinstance(item.get("node_id"), int)
    ]

    note_evidence: list[dict[str, Any]] = []
    for match in note_matches[:3]:
        task = match.get("task")
        if not isinstance(task, dict):
            continue
        note_evidence.append(
            _evidence_ref(
                source="search_notes",
                detail=str(match.get("snippet") or "").strip(),
                task_id=task.get("id") if isinstance(task.get("id"), int) else None,
                task_title=str(task.get("title") or "").strip() or None,
                score=float(match.get("score") or 0.0),
            )
        )

    graph_evidence: list[dict[str, Any]] = []
    recent_action_evidence: list[dict[str, Any]] = []
    if working_set:
        recent_actions_payload = _append_tool(
            tool_trace,
            name="get_recent_session_actions",
            arguments={},
            result=get_recent_session_actions(working_set),
        )
        for working_item in recent_actions_payload.get("recent_actions", [])[:3]:
            if not isinstance(working_item, dict):
                continue
            node_id = working_item.get("node_id")
            title = working_item.get("title")
            recent_action_evidence.append(
                _evidence_ref(
                    source=str(working_item.get("source") or "session_working_set"),
                    detail=f"Working-set salience {float(working_item.get('salience') or 0.0):.2f}",
                    task_id=node_id if isinstance(node_id, int) else None,
                    task_title=str(title).strip() or None if isinstance(title, str) else None,
                    score=float(working_item.get("salience") or 0.0),
                )
            )

    primary_ids = resolved_entity_ids or candidate_entity_ids[: (2 if intent_name == "compare_entities" else 1)]
    relevant_subtree_root_ids: list[int] = []
    for task_id in primary_ids[:2]:
        node = node_lookup.get(task_id)
        if node is None:
            continue
        if mode in {"metadata", "action"} or requested_fields:
            metadata_payload = _append_tool(
                tool_trace,
                name="get_task_metadata",
                arguments={"task_id": task_id},
                result=get_task_metadata(
                    task_id=task_id,
                    task_title=node.title,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    working_set=working_set,
                ),
            )
            metadata = metadata_payload.get("metadata")
            if isinstance(metadata, dict):
                graph_evidence.append(
                    _evidence_ref(
                        source="get_task_metadata",
                        detail=(
                            f"{metadata.get('priority_label', metadata.get('priority', 'priority unknown'))} | "
                            f"{metadata.get('deadline', 'No deadline')} | "
                            f"{metadata.get('estimate', 'No estimate')}"
                        ),
                        task_id=task_id,
                        task_title=node.title,
                        relationship="metadata",
                    )
                )
        snapshot = _append_tool(
            tool_trace,
            name="get_task_snapshot",
            arguments={"task_id": task_id},
            result=get_task_snapshot(
                task_id=task_id,
                task_title=node.title,
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                working_set=working_set,
            ),
        )
        task = snapshot.get("task")
        if isinstance(task, dict):
            graph_evidence.append(
                _evidence_ref(
                    source="get_task_snapshot",
                    detail=(
                        f"{task.get('category', 'Unknown')} | "
                        f"{task.get('priority_label', task.get('priority', 'priority unknown'))} | "
                        f"{task.get('deadline', 'No deadline')}"
                    ),
                    task_id=task_id,
                    task_title=node.title,
                )
            )
        notes_payload = _append_tool(
            tool_trace,
            name="get_task_notes",
            arguments={"task_id": task_id},
            result=get_task_notes(
                task_id=task_id,
                task_title=node.title,
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                working_set=working_set,
            ),
        )
        note_chunks = notes_payload.get("note_chunks", [])
        if isinstance(note_chunks, list) and note_chunks:
            note_evidence.append(
                _evidence_ref(
                    source="get_task_notes",
                    detail=str(note_chunks[0])[:220],
                    task_id=task_id,
                    task_title=node.title,
                )
            )
        if retrieval_plan.purpose in {"branch_understanding", "comparison"}:
            parent_payload = _append_tool(
                tool_trace,
                name="get_parent_chain",
                arguments={"task_id": task_id},
                result=get_parent_chain(
                    task_id=task_id,
                    task_title=node.title,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    working_set=working_set,
                ),
            )
            parents = parent_payload.get("parents", [])
            if isinstance(parents, list) and parents:
                graph_evidence.append(
                    _evidence_ref(
                        source="get_parent_chain",
                        detail=" > ".join(
                            str(parent.get("title") or "").strip()
                            for parent in parents
                            if isinstance(parent, dict) and str(parent.get("title") or "").strip()
                        ),
                        task_id=task_id,
                        task_title=node.title,
                        relationship="parent_chain",
                    )
                )
            if retrieval_plan.purpose == "branch_understanding":
                subtree_payload = _append_tool(
                    tool_trace,
                    name="get_subtree",
                    arguments={"task_id": task_id, "depth": 2},
                    result=get_subtree(
                        task_id=task_id,
                        task_title=node.title,
                        depth=2,
                        categories=categories,
                        nodes=nodes,
                        focus_node=focus_node,
                        active_category=active_category,
                        working_set=working_set,
                    ),
                )
                branch = subtree_payload.get("branch")
                branch_children = (
                    branch.get("children", [])
                    if isinstance(branch, dict) and isinstance(branch.get("children"), list)
                    else []
                )
                if branch_children:
                    relevant_subtree_root_ids.append(task_id)
                    graph_evidence.append(
                        _evidence_ref(
                            source="get_subtree",
                            detail=f"{len(branch_children)} child branches inspected with descendants",
                            task_id=task_id,
                            task_title=node.title,
                            relationship="subtree",
                        )
                    )
            children_payload = _append_tool(
                tool_trace,
                name="get_children",
                arguments={"task_id": task_id, "depth": 1},
                result=get_children(
                    task_id=task_id,
                    task_title=node.title,
                    depth=1,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    working_set=working_set,
                ),
            )
            children = children_payload.get("children", [])
            if isinstance(children, list) and children:
                if task_id not in relevant_subtree_root_ids:
                    relevant_subtree_root_ids.append(task_id)
                graph_evidence.append(
                    _evidence_ref(
                        source="get_children",
                        detail=f"{len(children)} direct child tasks inspected",
                        task_id=task_id,
                        task_title=node.title,
                        relationship="children",
                    )
                )
            siblings_payload = _append_tool(
                tool_trace,
                name="get_siblings",
                arguments={"task_id": task_id},
                result=get_siblings(
                    task_id=task_id,
                    task_title=node.title,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    working_set=working_set,
                ),
            )
            siblings = siblings_payload.get("siblings", [])
            if isinstance(siblings, list) and siblings:
                graph_evidence.append(
                    _evidence_ref(
                        source="get_siblings",
                        detail=f"{len(siblings)} siblings available for disambiguation",
                        task_id=task_id,
                        task_title=node.title,
                        relationship="siblings",
                    )
                )
        if "block" in prompt.lower() or "depend" in prompt.lower():
            related_payload = _append_tool(
                tool_trace,
                name="get_related_entities",
                arguments={"task_id": task_id},
                result=get_related_entities(
                    task_id=task_id,
                    task_title=node.title,
                    categories=categories,
                    nodes=nodes,
                    focus_node=focus_node,
                    active_category=active_category,
                    working_set=working_set,
                ),
            )
            related = related_payload.get("related", [])
            if isinstance(related, list):
                for relation in related[:3]:
                    if not isinstance(relation, dict):
                        continue
                    task = relation.get("task")
                    if not isinstance(task, dict):
                        continue
                    graph_evidence.append(
                        _evidence_ref(
                            source="get_related_entities",
                            detail=str(task.get("title") or "").strip(),
                            task_id=task.get("id") if isinstance(task.get("id"), int) else None,
                            task_title=str(task.get("title") or "").strip() or None,
                            relationship=str(relation.get("relationship") or "").strip() or None,
                        )
                    )

    if retrieval_plan.purpose == "situational_awareness" and intent_name != "list_query":
        workload = _append_tool(
            tool_trace,
            name="get_workload_overview",
            arguments={"category_name": active_category.name if active_category is not None else None, "days": 14},
            result=get_workload_overview(
                categories=categories,
                nodes=nodes,
                today_value=effective_today,
                category_name=active_category.name if active_category is not None else None,
                days=14,
            ),
        )
        graph_evidence.append(
            _evidence_ref(
                source="get_workload_overview",
                detail=(
                    f"{int(workload.get('active_count') or 0)} active tasks | "
                    f"{int(workload.get('backlog_count') or 0)} backlog | "
                    f"{len(workload.get('due_soon', [])) if isinstance(workload.get('due_soon'), list) else 0} due soon"
                ),
            )
        )

    confidence_reasons: list[str] = []
    for candidate in candidate_entities[:3]:
        reasons = candidate.get("reasons")
        if isinstance(reasons, list):
            confidence_reasons.extend(
                str(reason).strip()
                for reason in reasons
                if isinstance(reason, str) and str(reason).strip()
            )
    for entity in resolved_entities[:2]:
        source = entity.get("source")
        if isinstance(source, str) and source.strip():
            confidence_reasons.append(f"resolved via {source.strip()}")
    deduped_reasons: list[str] = []
    for reason in confidence_reasons:
        if reason not in deduped_reasons:
            deduped_reasons.append(reason)

    unresolved_gaps: list[str] = []
    if bool(retrieval_result.get("ambiguous")):
        unresolved_gaps.append("Multiple plausible task targets remain.")
    if bool(retrieval_result.get("has_entity_reference")) and not resolved_entity_ids:
        unresolved_gaps.append("The prompt appears to reference a task, but retrieval did not resolve one confidently.")
    if not note_evidence and "note" in prompt.lower():
        unresolved_gaps.append("No note-backed evidence was found for the requested concept.")
    if bool(retrieval_result.get("skipped")):
        unresolved_gaps.append("Retrieval was skipped because the turn could be answered without graph search.")

    if standalone_hypothetical:
        resolution: EvidenceBundle.__annotations__["resolution"] = "hypothetical_concept"
    elif resolved_entity_ids and not bool(retrieval_result.get("ambiguous")):
        resolution = (
            "entity_set" if len(resolved_entity_ids) > 1 else "resolved_entity"
        )
    elif bool(retrieval_result.get("ambiguous")) and candidate_entity_ids:
        resolution = "ambiguous_candidates"
    elif _is_hypothetical_prompt(prompt, mode):
        resolution = "hypothetical_concept"
    else:
        resolution = "no_match"
    retrieval_result["resolution"] = resolution

    evidence_bundle = asdict(
        EvidenceBundle(
            resolution=resolution,
            resolved_entity_ids=resolved_entity_ids,
            candidate_entity_ids=candidate_entity_ids,
            relevant_subtree_root_ids=relevant_subtree_root_ids,
            note_evidence=note_evidence,
            graph_evidence=graph_evidence,
            recent_action_evidence=recent_action_evidence,
            confidence=float(retrieval_result.get("overall_confidence") or 0.0),
            confidence_reasons=deduped_reasons[:8],
            unresolved_gaps=unresolved_gaps,
        )
    )

    return {
        "retrieval_plan": asdict(retrieval_plan),
        "retrieval_result": retrieval_result,
        "evidence_bundle": evidence_bundle,
        "tool_trace": tool_trace,
    }
