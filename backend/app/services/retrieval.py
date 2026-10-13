"""
Deterministic Strategist retrieval helpers and domain tools.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.deadline_values import parse_deadline_date

PRONOUN_RE = re.compile(r"\b(it|its|that one|this one|that task|this task)\b", re.IGNORECASE)
OTHER_ONE_RE = re.compile(r"\b(other one|the other one)\b", re.IGNORECASE)
COMPARE_RE = re.compile(r"\b(compare|versus|vs\.?|with|factor in|which one|tighter deadline)\b", re.IGNORECASE)
FIELD_DUE_RE = re.compile(r"\b(due|deadline|when('| i)?s)\b", re.IGNORECASE)
FIELD_PRIORITY_RE = re.compile(r"\b(priority|urgent|high|medium|low|red|orange|green)\b", re.IGNORECASE)
FIELD_ESTIMATE_RE = re.compile(r"\b(estimate|how long|hours?|hrs?|minutes?|mins?)\b", re.IGNORECASE)
STALE_TAG_RE = re.compile(r"^stale:(\d+)d$", re.IGNORECASE)
DEPENDENCY_RE = re.compile(r"depends on:\s*([^.\n]+)", re.IGNORECASE)
GENERIC_TOKENS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "it",
    "its",
    "my",
    "of",
    "one",
    "or",
    "show",
    "task",
    "that",
    "the",
    "this",
    "to",
    "update",
    "what",
    "when",
    "which",
}


def normalize_text(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def decode_tags(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def extract_aliases(node: StrategyNode) -> list[str]:
    notes = node.notes or ""
    aliases: list[str] = []
    match = re.search(r"search aliases:\s*(.+)$", notes, re.IGNORECASE | re.MULTILINE)
    if match:
        aliases.extend(
            item.strip()
            for item in match.group(1).split(",")
            if item.strip()
        )
    for tag in decode_tags(node.tags):
        if tag.startswith("cross-listed-alias:"):
            alias = tag.split(":", 1)[1].strip().replace("_", " ")
            if alias:
                aliases.append(alias)
    deduped: list[str] = []
    for alias in aliases:
        if alias not in deduped:
            deduped.append(alias)
    return deduped


def format_deadline(deadline_at: str | None) -> str:
    if not deadline_at:
        return "No deadline"
    parsed = parse_deadline_date(deadline_at)
    if parsed is None:
        return deadline_at
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def format_estimate(minutes: int | None) -> str:
    if minutes is None:
        return "No estimate"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remainder = minutes % 60
    return f"{hours}h" if remainder == 0 else f"{hours}h {remainder}m"


def priority_label(priority: str) -> str:
    lowered = normalize_text(priority)
    if lowered == "high":
        return "high priority (red)"
    if lowered == "medium":
        return "medium priority (orange)"
    if lowered == "low":
        return "low priority (green)"
    return lowered or "unspecified priority"


def build_category_name_map(categories: list[Category]) -> dict[int, str]:
    return {category.id: category.name for category in categories if category.id is not None}


def build_node_lookup(nodes: list[StrategyNode]) -> dict[int, StrategyNode]:
    return {node.id: node for node in nodes if node.id is not None}


def build_children_map(nodes: list[StrategyNode]) -> dict[int, list[StrategyNode]]:
    children_by_parent: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children_by_parent.setdefault(node.parent_id, []).append(node)
    for children in children_by_parent.values():
        children.sort(key=lambda item: item.sort_order)
    return children_by_parent


def build_parent_chain(node: StrategyNode, node_lookup: dict[int, StrategyNode]) -> list[StrategyNode]:
    parents: list[StrategyNode] = []
    parent_id = node.parent_id
    while parent_id is not None and parent_id in node_lookup:
        parent = node_lookup[parent_id]
        parents.append(parent)
        parent_id = parent.parent_id
    return list(reversed(parents))


def serialize_task(
    node: StrategyNode,
    *,
    category_name_by_id: dict[int, str],
    node_lookup: dict[int, StrategyNode] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": node.id,
        "title": node.title,
        "category": category_name_by_id.get(node.category_id, "Unknown"),
        "status": node.status,
        "priority": node.priority,
        "priority_label": priority_label(node.priority),
        "planned_date": node.planned_date,
        "deadline_at": node.deadline_at,
        "deadline": format_deadline(node.deadline_at),
        "time_estimate_minutes": node.time_estimate_minutes,
        "estimate": format_estimate(node.time_estimate_minutes),
        "node_type": node.node_type,
        "is_backlog": node.is_backlog,
        "is_pinned": node.is_pinned,
        "parent_id": node.parent_id,
        "notes": node.notes or "",
        "tags": decode_tags(node.tags),
        "aliases": extract_aliases(node),
    }
    if node_lookup is not None:
        payload["ancestors"] = [parent.title for parent in build_parent_chain(node, node_lookup)]
    return payload


def is_archived(node: StrategyNode) -> bool:
    return any(tag.lower() == "state:archived" for tag in decode_tags(node.tags))


def is_completed(node: StrategyNode) -> bool:
    if node.status == "done":
        return True
    return any(tag.lower() == "state:completed" for tag in decode_tags(node.tags))


def stale_days(node: StrategyNode) -> int | None:
    for tag in decode_tags(node.tags):
        match = STALE_TAG_RE.match(tag)
        if match:
            return int(match.group(1))
    return None


def extract_dependency_titles(node: StrategyNode) -> list[str]:
    notes = node.notes or ""
    matches = DEPENDENCY_RE.findall(notes)
    return [match.strip() for match in matches if match.strip()]


def dependency_matches_title(candidate: str, node: StrategyNode) -> bool:
    normalized_candidate = normalize_text(candidate)
    title = normalize_text(node.title)
    aliases = [normalize_text(alias) for alias in extract_aliases(node)]
    return normalized_candidate == title or normalized_candidate in aliases


def find_dependencies(node: StrategyNode, nodes: list[StrategyNode]) -> list[StrategyNode]:
    dependency_titles = extract_dependency_titles(node)
    matches: list[StrategyNode] = []
    for candidate_title in dependency_titles:
        for other in nodes:
            if other.id == node.id:
                continue
            if dependency_matches_title(candidate_title, other):
                matches.append(other)
    return matches


def find_dependents(node: StrategyNode, nodes: list[StrategyNode]) -> list[StrategyNode]:
    matches: list[StrategyNode] = []
    for other in nodes:
        if other.id == node.id:
            continue
        if any(dependency_matches_title(dep_title, node) for dep_title in extract_dependency_titles(other)):
            matches.append(other)
    return matches


def related_node_ids(node: StrategyNode, nodes: list[StrategyNode]) -> set[int]:
    node_lookup = build_node_lookup(nodes)
    children = build_children_map(nodes)
    related: set[int] = set()
    if node.parent_id is not None:
        related.add(node.parent_id)
    if node.id is not None:
        related.update(child.id for child in children.get(node.id, []) if child.id is not None)
    related.update(dep.id for dep in find_dependencies(node, nodes) if dep.id is not None)
    related.update(dep.id for dep in find_dependents(node, nodes) if dep.id is not None)
    parent_chain = build_parent_chain(node, node_lookup)
    related.update(parent.id for parent in parent_chain if parent.id is not None)
    return related


def base_match_score(prompt_norm: str, phrase_norm: str) -> float:
    if not prompt_norm or not phrase_norm:
        return 0.0
    if prompt_norm == phrase_norm:
        return 1.0
    if phrase_norm in prompt_norm:
        return min(0.95, 0.72 + min(len(phrase_norm), 32) / 100)
    prompt_tokens = set(prompt_norm.split())
    phrase_tokens = set(phrase_norm.split())
    if phrase_tokens and phrase_tokens.issubset(prompt_tokens):
        return 0.54 + min(len(phrase_tokens), 4) * 0.06
    shared_tokens = phrase_tokens.intersection(prompt_tokens)
    informative_shared = {
        token for token in shared_tokens if token not in GENERIC_TOKENS and len(token) >= 4
    }
    if informative_shared:
        informative_phrase_tokens = {
            token for token in phrase_tokens if token not in GENERIC_TOKENS and len(token) >= 4
        }
        coverage = len(informative_shared) / max(1, len(informative_phrase_tokens))
        return min(
            0.72,
            0.22 + len(informative_shared) * 0.08 + min(0.22, coverage * 0.18),
        )
    return 0.0


def score_node_for_prompt(
    *,
    prompt_norm: str,
    node: StrategyNode,
    requested_fields: list[str],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    working_set_by_id: dict[int, dict[str, Any]],
    nodes: list[StrategyNode],
    node_lookup: dict[int, StrategyNode],
) -> dict[str, Any] | None:
    parent_chain = build_parent_chain(node, node_lookup)
    path_titles = [parent.title for parent in parent_chain] + [node.title]
    phrases = [
        node.title,
        *extract_aliases(node),
        " ".join(path_titles[-2:]) if len(path_titles) >= 2 else "",
        " ".join(path_titles),
    ]
    best_score = 0.0
    reasons: list[str] = []
    for phrase in phrases:
        if not phrase:
            continue
        score = base_match_score(prompt_norm, normalize_text(phrase))
        if score > best_score:
            best_score = score
            reasons = [f"matched '{phrase}'"]

    note_score = base_match_score(prompt_norm, normalize_text(node.notes or ""))
    if note_score > 0.0:
        best_score = max(best_score, note_score * 0.55)
        reasons.append("matched notes")

    if best_score <= 0.0:
        return None

    score = best_score
    if parent_chain:
        ancestor_scores = [
            base_match_score(prompt_norm, normalize_text(parent.title))
            for parent in parent_chain
        ]
        if max(ancestor_scores, default=0.0) >= 0.4 and best_score >= 0.45:
            score += 0.16
            reasons.append("matched ancestor path")
    if active_category is not None and active_category.id == node.category_id:
        score += 0.05
        reasons.append("active category bias")
    if focus_node is not None and focus_node.id == node.id:
        score += 0.18
        reasons.append("focused task bias")
    if node.id is not None and node.id in working_set_by_id:
        salience = float(working_set_by_id[node.id].get("salience") or 0.0)
        score += min(0.24, 0.08 + salience * 0.2)
        reasons.append("working-set bias")
    if focus_node is not None and node.id is not None and focus_node.id != node.id:
        if focus_node.id in related_node_ids(node, nodes):
            score += 0.08
            reasons.append("graph-related bias")

    if "deadline_at" in requested_fields:
        score += 0.18 if node.deadline_at else -0.04
    if "time_estimate_minutes" in requested_fields:
        score += 0.08 if node.time_estimate_minutes is not None else -0.03
    if "priority" in requested_fields:
        score += 0.05

    return {
        "node_id": node.id,
        "title": node.title,
        "score": round(score, 4),
        "reasons": reasons,
        "category_id": node.category_id,
    }


def search_entities(
    *,
    query: str,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
    requested_fields: list[str] | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    prompt_norm = normalize_text(query)
    if not prompt_norm:
        return []

    requested_fields = requested_fields or []
    working_set_by_id = {
        int(item["node_id"]): item
        for item in (working_set or [])
        if isinstance(item.get("node_id"), int)
    }
    category_name_by_id = build_category_name_map(categories)
    node_lookup = build_node_lookup(nodes)
    ranked: list[dict[str, Any]] = []
    for node in nodes:
        scored = score_node_for_prompt(
            prompt_norm=prompt_norm,
            node=node,
            requested_fields=requested_fields,
            focus_node=focus_node,
            active_category=active_category,
            working_set_by_id=working_set_by_id,
            nodes=nodes,
            node_lookup=node_lookup,
        )
        if scored is None:
            continue
        ranked.append(
            {
                **scored,
                "task": serialize_task(node, category_name_by_id=category_name_by_id, node_lookup=node_lookup),
            }
        )

    ranked.sort(key=lambda item: float(item["score"]), reverse=True)
    return ranked[:limit]


def search_notes(
    *,
    query: str,
    categories: list[Category],
    nodes: list[StrategyNode],
    limit: int = 6,
) -> list[dict[str, Any]]:
    query_norm = normalize_text(query)
    if not query_norm:
        return []
    category_name_by_id = build_category_name_map(categories)
    matches: list[dict[str, Any]] = []
    for node in nodes:
        notes = node.notes or ""
        if not notes.strip():
            continue
        chunks = [chunk.strip() for chunk in re.split(r"[\n.]+", notes) if chunk.strip()]
        best_chunk = ""
        best_score = 0.0
        for chunk in chunks or [notes]:
            chunk_score = base_match_score(query_norm, normalize_text(chunk))
            if chunk_score > best_score:
                best_score = chunk_score
                best_chunk = chunk
        if best_score <= 0.0:
            continue
        matches.append(
            {
                "score": round(best_score, 4),
                "task": serialize_task(node, category_name_by_id=category_name_by_id),
                "snippet": (best_chunk or notes)[:220],
            }
        )
    matches.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return matches[:limit]


def get_task_notes(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), int):
        return {"task": None, "notes": None, "note_chunks": []}
    node = build_node_lookup(nodes).get(task["id"])
    if node is None:
        return {"task": None, "notes": None, "note_chunks": []}
    notes = node.notes or ""
    chunks = [chunk.strip() for chunk in re.split(r"[\n.]+", notes) if chunk.strip()]
    return {
        "task": task,
        "notes": notes or None,
        "note_chunks": chunks[:8],
    }


def get_parent_chain(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), int):
        return {"task": None, "parents": []}
    node_lookup = build_node_lookup(nodes)
    node = node_lookup.get(task["id"])
    if node is None:
        return {"task": None, "parents": []}
    category_name_by_id = build_category_name_map(categories)
    return {
        "task": task,
        "parents": [
            serialize_task(parent, category_name_by_id=category_name_by_id, node_lookup=node_lookup)
            for parent in build_parent_chain(node, node_lookup)
        ],
    }


def get_children(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), int):
        return {"task": None, "children": []}
    node_lookup = build_node_lookup(nodes)
    node = node_lookup.get(task["id"])
    if node is None:
        return {"task": None, "children": []}
    category_name_by_id = build_category_name_map(categories)
    children_by_parent = build_children_map(nodes)
    return {
        "task": task,
        "children": [
            serialize_branch(
                child,
                category_name_by_id=category_name_by_id,
                node_lookup=node_lookup,
                children_by_parent=children_by_parent,
                depth=max(0, min(depth, 4)) - 1,
            )
            for child in children_by_parent.get(node.id or 0, [])
        ],
    }


def get_siblings(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), int):
        return {"task": None, "siblings": []}
    node_lookup = build_node_lookup(nodes)
    node = node_lookup.get(task["id"])
    if node is None or node.parent_id is None:
        return {"task": task, "siblings": []}
    category_name_by_id = build_category_name_map(categories)
    children_by_parent = build_children_map(nodes)
    siblings = [
        sibling
        for sibling in children_by_parent.get(node.parent_id, [])
        if sibling.id != node.id
    ]
    return {
        "task": task,
        "siblings": [
            serialize_task(sibling, category_name_by_id=category_name_by_id, node_lookup=node_lookup)
            for sibling in siblings[:8]
        ],
    }


def get_task_snapshot(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    query = task_title or str(task_id or "")
    matches = search_entities(
        query=query,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
        limit=1,
    )
    if task_id is not None:
        node_lookup = build_node_lookup(nodes)
        node = node_lookup.get(task_id)
    else:
        node = None
    if node is None and matches:
        matched_id = matches[0].get("node_id")
        node = build_node_lookup(nodes).get(matched_id) if isinstance(matched_id, int) else None
    if node is None and focus_node is not None:
        node = focus_node
    if node is None:
        return {"task": None}

    category_name_by_id = build_category_name_map(categories)
    node_lookup = build_node_lookup(nodes)
    children_by_parent = build_children_map(nodes)
    dependencies = find_dependencies(node, nodes)
    dependents = find_dependents(node, nodes)
    return {
        "task": serialize_task(node, category_name_by_id=category_name_by_id, node_lookup=node_lookup),
        "child_titles": [child.title for child in children_by_parent.get(node.id or 0, [])[:8]],
        "dependencies": [serialize_task(item, category_name_by_id=category_name_by_id) for item in dependencies[:6]],
        "dependents": [serialize_task(item, category_name_by_id=category_name_by_id) for item in dependents[:6]],
    }


def get_task_metadata(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict):
        return {"task": None, "metadata": None}
    return {
        "task": task,
        "metadata": {
            "status": task.get("status"),
            "priority": task.get("priority"),
            "priority_label": task.get("priority_label"),
            "planned_date": task.get("planned_date"),
            "deadline_at": task.get("deadline_at"),
            "deadline": task.get("deadline"),
            "time_estimate_minutes": task.get("time_estimate_minutes"),
            "estimate": task.get("estimate"),
            "node_type": task.get("node_type"),
            "is_backlog": task.get("is_backlog"),
            "is_pinned": task.get("is_pinned"),
            "tags": task.get("tags"),
            "aliases": task.get("aliases"),
        },
    }


def serialize_branch(
    node: StrategyNode,
    *,
    category_name_by_id: dict[int, str],
    node_lookup: dict[int, StrategyNode],
    children_by_parent: dict[int, list[StrategyNode]],
    depth: int,
) -> dict[str, Any]:
    payload = serialize_task(node, category_name_by_id=category_name_by_id, node_lookup=node_lookup)
    if depth <= 0 or node.id is None:
        payload["children"] = []
        return payload
    payload["children"] = [
        serialize_branch(
            child,
            category_name_by_id=category_name_by_id,
            node_lookup=node_lookup,
            children_by_parent=children_by_parent,
            depth=depth - 1,
        )
        for child in children_by_parent.get(node.id, [])
    ]
    return payload


def get_task_branch(
    *,
    task_id: int | None,
    task_title: str | None,
    depth: int,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict):
        return {"branch": None}
    node_id = task.get("id")
    if not isinstance(node_id, int):
        return {"branch": None}
    node = build_node_lookup(nodes).get(node_id)
    if node is None:
        return {"branch": None}
    category_name_by_id = build_category_name_map(categories)
    node_lookup = build_node_lookup(nodes)
    children_by_parent = build_children_map(nodes)
    return {
        "branch": serialize_branch(
            node,
            category_name_by_id=category_name_by_id,
            node_lookup=node_lookup,
            children_by_parent=children_by_parent,
            depth=max(1, min(depth, 4)),
        )
    }


def get_subtree(
    *,
    task_id: int | None,
    task_title: str | None,
    depth: int,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return get_task_branch(
        task_id=task_id,
        task_title=task_title,
        depth=depth,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )


def get_related_entities(
    *,
    task_id: int | None,
    task_title: str | None,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = get_task_snapshot(
        task_id=task_id,
        task_title=task_title,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
    )
    task = snapshot.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), int):
        return {"task": None, "related": []}
    node = build_node_lookup(nodes).get(task["id"])
    if node is None:
        return {"task": None, "related": []}
    category_name_by_id = build_category_name_map(categories)
    children_by_parent = build_children_map(nodes)
    node_lookup = build_node_lookup(nodes)

    related: list[dict[str, Any]] = []
    if node.parent_id is not None and node.parent_id in node_lookup:
        related.append(
            {
                "relationship": "parent",
                "task": serialize_task(
                    node_lookup[node.parent_id],
                    category_name_by_id=category_name_by_id,
                    node_lookup=node_lookup,
                ),
            }
        )
    for child in children_by_parent.get(node.id or 0, [])[:6]:
        related.append(
            {
                "relationship": "child",
                "task": serialize_task(child, category_name_by_id=category_name_by_id, node_lookup=node_lookup),
            }
        )
    for dependency in find_dependencies(node, nodes)[:6]:
        related.append(
            {
                "relationship": "depends_on",
                "task": serialize_task(dependency, category_name_by_id=category_name_by_id, node_lookup=node_lookup),
            }
        )
    for dependent in find_dependents(node, nodes)[:6]:
        related.append(
            {
                "relationship": "blocks",
                "task": serialize_task(dependent, category_name_by_id=category_name_by_id, node_lookup=node_lookup),
            }
        )
    return {"task": task, "related": related}


def get_session_working_set(working_set: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"working_set": working_set or []}


def get_recent_session_actions(working_set: list[dict[str, Any]] | None) -> dict[str, Any]:
    recent_actions: list[dict[str, Any]] = []
    for item in working_set or []:
        if not isinstance(item, dict):
            continue
        recent_actions.append(
            {
                "node_id": item.get("node_id"),
                "title": item.get("title"),
                "source": item.get("source"),
                "salience": item.get("salience"),
                "last_mentioned_at": item.get("last_mentioned_at"),
            }
        )
    return {"recent_actions": recent_actions}


def get_workload_overview(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    today_value: date,
    category_name: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    category_name_by_id = build_category_name_map(categories)
    scope = normalize_text(category_name or "")
    node_lookup = build_node_lookup(nodes)

    def in_scope(node: StrategyNode) -> bool:
        if not scope:
            return True
        if normalize_text(category_name_by_id.get(node.category_id, "")) == scope:
            return True
        if normalize_text(node.title) == scope:
            return True
        return any(
            normalize_text(parent.title) == scope
            for parent in build_parent_chain(node, node_lookup)
        )

    due_cutoff = today_value + timedelta(days=max(1, min(days, 30)))
    scoped_nodes = [node for node in nodes if in_scope(node)]
    due_soon: list[StrategyNode] = []
    overdue: list[StrategyNode] = []
    stale: list[StrategyNode] = []
    high_priority: list[StrategyNode] = []

    for node in scoped_nodes:
        if is_completed(node) or is_archived(node):
            continue
        if normalize_text(node.priority) == "high":
            high_priority.append(node)
        if node.deadline_at:
            deadline_value = parse_deadline_date(node.deadline_at)
            if deadline_value is not None:
                if deadline_value < today_value:
                    overdue.append(node)
                elif deadline_value <= due_cutoff:
                    due_soon.append(node)
        stale_marker = stale_days(node)
        if stale_marker is not None and stale_marker >= 30:
            stale.append(node)

    due_soon.sort(key=lambda item: item.deadline_at or "9999-99-99")
    overdue.sort(key=lambda item: item.deadline_at or "9999-99-99")
    stale.sort(key=lambda item: stale_days(item) or 0, reverse=True)
    high_priority.sort(
        key=lambda item: (
            item.deadline_at or "9999-99-99",
            normalize_text(item.title),
        )
    )

    due_soon_limit = 12
    overdue_limit = 12
    high_priority_limit = 12
    stale_limit = 8
    active_nodes = [
        node
        for node in scoped_nodes
        if not is_completed(node) and not is_archived(node)
    ]
    scoped_category_ids = {node.category_id for node in scoped_nodes}

    return {
        "scope": category_name or "all categories",
        "days": days,
        "task_count": len(scoped_nodes),
        "active_count": len(active_nodes),
        "backlog_count": sum(1 for node in scoped_nodes if node.is_backlog),
        "category_count": len(scoped_category_ids),
        "due_soon_count": len(due_soon),
        "overdue_count": len(overdue),
        "high_priority_count": len(high_priority),
        "stale_count": len(stale),
        "due_soon": [
            serialize_task(node, category_name_by_id=category_name_by_id)
            for node in due_soon[:due_soon_limit]
        ],
        "overdue": [
            serialize_task(node, category_name_by_id=category_name_by_id)
            for node in overdue[:overdue_limit]
        ],
        "high_priority": [
            serialize_task(node, category_name_by_id=category_name_by_id)
            for node in high_priority[:high_priority_limit]
        ],
        "stale": [
            serialize_task(node, category_name_by_id=category_name_by_id)
            for node in stale[:stale_limit]
        ],
        "evidence_coverage": {
            "kind": "bounded_complete_scope_scan",
            "complete_scope_scan": True,
            "scope": category_name or "all categories",
            "scanned_task_count": len(scoped_nodes),
            "scanned_active_count": len(active_nodes),
            "lists_are_ranked_and_bounded": True,
            "truncated": {
                "due_soon": len(due_soon) > due_soon_limit,
                "overdue": len(overdue) > overdue_limit,
                "high_priority": len(high_priority) > high_priority_limit,
                "stale": len(stale) > stale_limit,
            },
        },
        "model_instruction": (
            "This result scanned the complete requested scope and returned bounded ranked "
            "pressure lists. Prioritize overdue and near-deadline work first, then high-priority "
            "and dependency-unlocking work. If a list is truncated or deeper reasoning depends "
            "on task notes/relationships, inspect only the leading candidates with a narrower tool."
        ),
    }


def list_tasks_by_filter(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    today_value: date,
    category_name: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    filters = filters or {}
    category_name_by_id = build_category_name_map(categories)
    scope = normalize_text(category_name or "")
    node_lookup = build_node_lookup(nodes)
    children_by_parent = build_children_map(nodes)
    safe_limit = max(1, min(int(limit or 40), 100))

    def in_scope(node: StrategyNode) -> bool:
        if not scope:
            return True
        if normalize_text(category_name_by_id.get(node.category_id, "")) == scope:
            return True
        if normalize_text(node.title) == scope:
            return True
        return any(
            normalize_text(parent.title) == scope
            for parent in build_parent_chain(node, node_lookup)
        )

    active_only = bool(filters.get("active_only", True))
    include_archived = bool(filters.get("include_archived", False))
    include_backlog = bool(filters.get("include_backlog", True))
    include_branches = bool(filters.get("include_branches", True))
    leaf_only = bool(filters.get("leaf_only", False))
    deadline_filter = str(filters.get("deadline") or "").strip().lower()
    deadline_start_filter = str(filters.get("deadline_start") or "").strip()
    deadline_end_filter = str(filters.get("deadline_end") or "").strip()
    estimate_filter = str(filters.get("estimate") or "").strip().lower()
    sort_mode = str(filters.get("sort") or "").strip().lower()
    max_estimate_minutes = None
    min_estimate_minutes = None
    try:
        raw_max_estimate = filters.get("max_estimate_minutes")
        if raw_max_estimate not in (None, ""):
            max_estimate_minutes = max(1, min(int(raw_max_estimate), 7 * 24 * 60))
    except (TypeError, ValueError):
        max_estimate_minutes = None
    try:
        raw_min_estimate = filters.get("min_estimate_minutes")
        if raw_min_estimate not in (None, ""):
            min_estimate_minutes = max(1, min(int(raw_min_estimate), 7 * 24 * 60))
    except (TypeError, ValueError):
        min_estimate_minutes = None
    statuses = {
        normalize_text(str(item))
        for item in filters.get("statuses", [])
        if isinstance(item, str) and str(item).strip()
    }
    priorities = {
        normalize_text(str(item))
        for item in filters.get("priorities", [])
        if isinstance(item, str) and str(item).strip()
    }
    query = normalize_text(str(filters.get("query") or ""))
    days = max(1, min(int(filters.get("days") or 30), 365))
    due_cutoff = today_value + timedelta(days=days)

    def deadline_matches(node: StrategyNode) -> bool:
        if not deadline_filter:
            return True
        has_deadline = bool(node.deadline_at)
        if deadline_filter == "present":
            return has_deadline
        if deadline_filter == "missing":
            return not has_deadline
        if not has_deadline:
            return False
        deadline_value = parse_deadline_date(node.deadline_at)
        if deadline_value is None:
            return False
        if deadline_filter == "between":
            try:
                start_value = (
                    date.fromisoformat(deadline_start_filter)
                    if deadline_start_filter
                    else today_value
                )
                end_value = (
                    date.fromisoformat(deadline_end_filter)
                    if deadline_end_filter
                    else due_cutoff
                )
            except ValueError:
                return False
            if end_value < start_value:
                start_value, end_value = end_value, start_value
            return start_value <= deadline_value <= end_value
        if deadline_filter == "overdue":
            return deadline_value < today_value
        if deadline_filter in {"within", "due_soon"}:
            return today_value <= deadline_value <= due_cutoff
        return True

    def estimate_matches(node: StrategyNode) -> bool:
        if not estimate_filter:
            if max_estimate_minutes is None and min_estimate_minutes is None:
                return True
        has_estimate = node.time_estimate_minutes is not None
        if estimate_filter == "present":
            if not has_estimate:
                return False
        if estimate_filter == "missing":
            return not has_estimate
        if max_estimate_minutes is not None:
            if not has_estimate or int(node.time_estimate_minutes or 0) > max_estimate_minutes:
                return False
        if min_estimate_minutes is not None:
            if not has_estimate or int(node.time_estimate_minutes or 0) < min_estimate_minutes:
                return False
        return True

    def text_matches(node: StrategyNode) -> bool:
        if not query:
            return True
        searchable = " ".join(
            [
                node.title,
                node.notes or "",
                *extract_aliases(node),
                *decode_tags(node.tags),
                category_name_by_id.get(node.category_id, ""),
            ]
        )
        return query in normalize_text(searchable)

    matches: list[StrategyNode] = []
    for node in nodes:
        if not in_scope(node):
            continue
        if active_only and is_completed(node):
            continue
        if not include_archived and is_archived(node):
            continue
        if not include_backlog and node.is_backlog:
            continue
        if leaf_only and node.id is not None and children_by_parent.get(node.id):
            continue
        if not include_branches and node.node_type != "task":
            continue
        if statuses and normalize_text(node.status) not in statuses:
            continue
        if priorities and normalize_text(node.priority) not in priorities:
            continue
        if not deadline_matches(node):
            continue
        if not estimate_matches(node):
            continue
        if not text_matches(node):
            continue
        matches.append(node)

    def sort_key(node: StrategyNode) -> tuple[Any, ...]:
        deadline_sort = node.deadline_at or "9999-99-99"
        priority_rank = {"high": 0, "medium": 1, "low": 2}.get(normalize_text(node.priority), 3)
        if sort_mode == "priority_desc":
            return (priority_rank, deadline_sort, node.sort_order, node.title.lower())
        if sort_mode == "priority_asc":
            reverse_priority_rank = {"low": 0, "medium": 1, "high": 2}.get(normalize_text(node.priority), 3)
            return (reverse_priority_rank, deadline_sort, node.sort_order, node.title.lower())
        if sort_mode == "estimate_desc":
            estimate_sort = -(node.time_estimate_minutes or 0)
            missing_estimate = 1 if node.time_estimate_minutes is None else 0
            return (missing_estimate, estimate_sort, deadline_sort, priority_rank, node.sort_order, node.title.lower())
        if sort_mode == "estimate_asc":
            estimate_sort = node.time_estimate_minutes or 0
            missing_estimate = 1 if node.time_estimate_minutes is None else 0
            return (missing_estimate, estimate_sort, deadline_sort, priority_rank, node.sort_order, node.title.lower())
        return (deadline_sort, priority_rank, node.sort_order, node.title.lower())

    matches.sort(key=sort_key)
    return {
        "scope": category_name or "all categories",
        "filters": {
            "active_only": active_only,
            "include_archived": include_archived,
            "include_backlog": include_backlog,
            "include_branches": include_branches,
            "leaf_only": leaf_only,
            "deadline": deadline_filter or None,
            "deadline_start": deadline_start_filter if deadline_filter == "between" and deadline_start_filter else None,
            "deadline_end": deadline_end_filter if deadline_filter == "between" and deadline_end_filter else None,
            "estimate": estimate_filter or None,
            "max_estimate_minutes": max_estimate_minutes,
            "min_estimate_minutes": min_estimate_minutes,
            "statuses": sorted(statuses),
            "priorities": sorted(priorities),
            "query": query or None,
            "days": days if deadline_filter in {"within", "due_soon"} else None,
            "sort": sort_mode or None,
        },
        "count": len(matches),
        "truncated": len(matches) > safe_limit,
        "tasks": [
            serialize_task(node, category_name_by_id=category_name_by_id, node_lookup=node_lookup)
            for node in matches[:safe_limit]
        ],
    }


def _memory_resolutions(
    *,
    working_set: list[dict[str, Any]],
    prompt: str,
    target_count: int,
) -> list[dict[str, Any]]:
    if not working_set:
        return []
    if OTHER_ONE_RE.search(prompt) and len(working_set) >= 2:
        secondary = working_set[1]
        return [
            {
                "node_id": secondary.get("node_id"),
                "title": secondary.get("title"),
                "role": "comparison",
                "confidence": 0.76,
                "source": "session_other_one",
                "mention_text": "the other one",
            }
        ]
    if PRONOUN_RE.search(prompt):
        primary = working_set[0]
        return [
            {
                "node_id": primary.get("node_id"),
                "title": primary.get("title"),
                "role": "primary",
                "confidence": 0.84,
                "source": "session_pronoun",
                "mention_text": "pronoun",
            }
        ]
    if target_count > 1 and len(working_set) >= target_count:
        return [
            {
                "node_id": item.get("node_id"),
                "title": item.get("title"),
                "role": "primary" if index == 0 else "comparison",
                "confidence": 0.62,
                "source": "session_working_set",
                "mention_text": "working set",
            }
            for index, item in enumerate(working_set[:target_count])
        ]
    return []


def _contextual_follow_up_prefers_memory(prompt: str, intent_name: str, target_count: int) -> bool:
    prompt_norm = normalize_text(prompt)
    if not prompt_norm or target_count < 2:
        return False
    if OTHER_ONE_RE.search(prompt) or PRONOUN_RE.search(prompt):
        return True
    if intent_name == "compare_entities" and re.search(
        r"\b(which one|tighter deadline|tighter|sooner|earlier|prioritize|priority)\b",
        prompt_norm,
        re.IGNORECASE,
    ):
        return True
    return False


def resolve_entities(
    *,
    prompt: str,
    intent_name: str,
    requested_fields: list[str],
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None = None,
    active_category: Category | None = None,
    working_set: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    working_set = working_set or []
    if intent_name == "list_query":
        target_count = 4
    elif intent_name == "compare_entities" or COMPARE_RE.search(prompt):
        target_count = 2
    else:
        target_count = 1
    candidates = search_entities(
        query=prompt,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        working_set=working_set,
        requested_fields=requested_fields,
        limit=6,
    )

    if not candidates:
        memory_only = _memory_resolutions(
            working_set=working_set,
            prompt=prompt,
            target_count=target_count,
        )
        return {
            "has_entity_reference": bool(memory_only or focus_node is not None or PRONOUN_RE.search(prompt)),
            "candidate_entities": [],
            "resolved_entities": memory_only,
            "overall_confidence": round(max((item["confidence"] for item in memory_only), default=0.0), 4),
            "ambiguous": False,
            "confidence_gap": None,
        }

    candidate_nodes = [item for item in candidates if isinstance(item.get("node_id"), int)]
    resolved: list[dict[str, Any]] = []
    top_score = float(candidate_nodes[0]["score"])
    second_score = float(candidate_nodes[1]["score"]) if len(candidate_nodes) > 1 else 0.0
    confidence_gap = round(top_score - second_score, 4)

    if _contextual_follow_up_prefers_memory(prompt, intent_name, target_count) and len(working_set) >= target_count:
        memory_only = _memory_resolutions(working_set=working_set, prompt=prompt, target_count=target_count)
        if memory_only:
            resolved.extend(memory_only)
    elif OTHER_ONE_RE.search(prompt) and len(working_set) >= 2:
        memory_only = _memory_resolutions(working_set=working_set, prompt=prompt, target_count=target_count)
        if memory_only:
            resolved.extend(memory_only)
    elif PRONOUN_RE.search(prompt) and working_set:
        resolved.extend(_memory_resolutions(working_set=working_set, prompt=prompt, target_count=target_count))
    else:
        for index, candidate in enumerate(candidate_nodes[:target_count]):
            resolved.append(
                {
                    "node_id": candidate["node_id"],
                    "title": candidate["title"],
                    "role": "primary" if index == 0 else "comparison",
                    "confidence": float(candidate["score"]),
                    "source": "retrieval",
                    "mention_text": prompt.strip(),
                }
            )
        if len(resolved) < target_count and working_set:
            seen_node_ids = {
                item["node_id"]
                for item in resolved
                if isinstance(item.get("node_id"), int)
            }
            for memory_item in working_set:
                node_id = memory_item.get("node_id")
                if not isinstance(node_id, int) or node_id in seen_node_ids:
                    continue
                resolved.append(
                    {
                        "node_id": node_id,
                        "title": memory_item.get("title"),
                        "role": "comparison" if resolved else "primary",
                        "confidence": 0.62,
                        "source": "session_working_set",
                        "mention_text": "working set",
                    }
                )
                seen_node_ids.add(node_id)
                if len(resolved) >= target_count:
                    break

    ambiguous = (
        len(candidate_nodes) > 1
        and target_count == 1
        and top_score >= 0.7
        and second_score >= 0.66
        and confidence_gap < 0.16
        and not FIELD_DUE_RE.search(prompt)
    )
    if ambiguous:
        top_title_norm = normalize_text(str(candidate_nodes[0].get("title") or ""))
        second_title_norm = normalize_text(str(candidate_nodes[1].get("title") or ""))
        exact_multiword_top = (
            top_title_norm
            and top_title_norm in normalize_text(prompt)
            and len(top_title_norm.split()) >= 2
        )
        second_is_broader_partial = (
            second_title_norm
            and second_title_norm != top_title_norm
            and second_title_norm in top_title_norm
        )
        if exact_multiword_top and second_is_broader_partial:
            ambiguous = False

    if intent_name == "compare_entities" and len(resolved) < 2 and len(candidate_nodes) >= 2:
        resolved = [
            {
                "node_id": candidate["node_id"],
                "title": candidate["title"],
                "role": "primary" if index == 0 else "comparison",
                "confidence": float(candidate["score"]),
                "source": "retrieval",
                "mention_text": prompt.strip(),
            }
            for index, candidate in enumerate(candidate_nodes[:2])
        ]

    return {
        "has_entity_reference": bool(candidate_nodes or focus_node is not None or PRONOUN_RE.search(prompt)),
        "candidate_entities": candidate_nodes,
        "resolved_entities": resolved,
        "overall_confidence": round(max((float(item["confidence"]) for item in resolved), default=0.0), 4),
        "ambiguous": ambiguous,
        "confidence_gap": confidence_gap,
    }
