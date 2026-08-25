"""Neutral task-graph query definitions and deterministic handlers.

The Agent Kernel and compatibility adapter both depend on this module. It owns
task-read behavior but no model, MCP, proposal, approval, or mutation policy.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.retrieval import (
    build_children_map,
    build_category_name_map,
    build_node_lookup,
    build_parent_chain,
    decode_tags,
    get_children,
    get_parent_chain,
    get_recent_session_actions,
    get_related_entities,
    get_siblings,
    get_subtree,
    get_task_metadata,
    get_task_notes,
    get_session_working_set,
    get_task_branch,
    get_task_snapshot,
    get_workload_overview,
    is_archived,
    is_completed,
    list_tasks_by_filter,
    normalize_text,
    search_entities,
    search_notes,
    serialize_task,
    stale_days,
)

ToolHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def build_compact_tree_outline(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    active_category_id: int | None = None,
    max_depth: int = 4,
    max_nodes: int = 120,
) -> dict[str, Any]:
    """Return structure only: ids, titles, node types, and parent-child shape."""
    children_by_parent = build_children_map(nodes)
    roots_by_category: dict[int, list[StrategyNode]] = {}
    nodes_by_category: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.category_id is not None:
            nodes_by_category.setdefault(node.category_id, []).append(node)
        if node.parent_id is None and node.category_id is not None:
            roots_by_category.setdefault(node.category_id, []).append(node)
    for roots in roots_by_category.values():
        roots.sort(key=lambda item: (item.sort_order, item.title.lower()))

    emitted_count = 0
    truncated = False

    def outline_node(node: StrategyNode, depth: int) -> dict[str, Any]:
        nonlocal emitted_count, truncated
        emitted_count += 1
        children = children_by_parent.get(node.id or -1, [])
        payload: dict[str, Any] = {
            "id": node.id,
            "title": node.title,
            "type": node.node_type,
            "child_count": len(children),
        }
        if depth >= max_depth or emitted_count >= max_nodes:
            if children:
                truncated = True
            return payload
        child_payloads: list[dict[str, Any]] = []
        for child in children:
            if emitted_count >= max_nodes:
                truncated = True
                break
            child_payloads.append(outline_node(child, depth + 1))
        if child_payloads:
            payload["children"] = child_payloads
        return payload

    def status_counts(category_nodes: list[StrategyNode]) -> dict[str, int]:
        counts = {"todo": 0, "in-progress": 0, "done": 0, "other": 0}
        for node in category_nodes:
            status = str(node.status or "").strip().lower()
            if status in counts:
                counts[status] += 1
            else:
                counts["other"] += 1
        return counts

    sorted_categories = sorted(
        categories,
        key=lambda item: (
            0 if active_category_id is not None and item.id == active_category_id else 1,
            item.sort_order,
            item.name.lower(),
        ),
    )
    category_payloads: list[dict[str, Any]] = []
    for category in sorted_categories:
        roots = roots_by_category.get(category.id or -1, [])
        category_nodes = nodes_by_category.get(category.id or -1, [])
        category_payloads.append(
            {
                "id": category.id,
                "name": category.name,
                "is_active": active_category_id is not None and category.id == active_category_id,
                "node_count": len(category_nodes),
                "root_count": len(roots),
                "status_counts": status_counts(category_nodes),
                "roots": [outline_node(root, 1) for root in roots if emitted_count < max_nodes],
            }
        )
        if emitted_count >= max_nodes:
            truncated = True
            break

    return {
        "description": "Scope-first structural map. Use category counts to choose scope, then fetch detailed fields or a deeper branch only when needed.",
        "traversal_strategy": "Prefer top-down progressive disclosure: category/status counts -> root ids -> get_subtree/get_task_snapshot for the one relevant branch.",
        "total_nodes": len(nodes),
        "included_nodes": emitted_count,
        "max_depth": max_depth,
        "truncated": truncated,
        "categories": category_payloads,
    }


def build_tree_scope_index(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    active_category_id: int | None = None,
    max_roots_per_category: int = 12,
) -> dict[str, Any]:
    """Return a small routing index; branch details stay behind read-only tools."""
    children_by_parent = build_children_map(nodes)
    roots_by_category: dict[int, list[StrategyNode]] = {}
    nodes_by_category: dict[int, list[StrategyNode]] = {}
    for node in nodes:
        if node.category_id is not None:
            nodes_by_category.setdefault(node.category_id, []).append(node)
        if node.parent_id is None and node.category_id is not None:
            roots_by_category.setdefault(node.category_id, []).append(node)
    for roots in roots_by_category.values():
        roots.sort(key=lambda item: (item.sort_order, item.title.lower()))

    def status_counts(category_nodes: list[StrategyNode]) -> dict[str, int]:
        counts = {"todo": 0, "in-progress": 0, "done": 0, "other": 0}
        for node in category_nodes:
            status = str(node.status or "").strip().lower()
            counts[status if status in counts else "other"] += 1
        return counts

    sorted_categories = sorted(
        categories,
        key=lambda item: (
            0 if active_category_id is not None and item.id == active_category_id else 1,
            item.sort_order,
            item.name.lower(),
        ),
    )
    category_payloads: list[dict[str, Any]] = []
    for category in sorted_categories:
        category_id = category.id or -1
        roots = roots_by_category.get(category_id, [])
        category_nodes = nodes_by_category.get(category_id, [])
        emitted_roots = roots[:max(1, max_roots_per_category)]
        category_payloads.append(
            {
                "id": category.id,
                "name": category.name,
                "is_active": active_category_id is not None and category.id == active_category_id,
                "node_count": len(category_nodes),
                "root_count": len(roots),
                "status_counts": status_counts(category_nodes),
                "roots": [
                    {
                        "id": root.id,
                        "title": root.title,
                        "type": root.node_type,
                        "status": root.status,
                        "priority": root.priority,
                        "child_count": len(children_by_parent.get(root.id or -1, [])),
                    }
                    for root in emitted_roots
                ],
                "roots_truncated": len(roots) > len(emitted_roots),
            }
        )

    return {
        "description": "Small routing index for choosing category and root scope before fetching details.",
        "traversal_strategy": (
            "Use this index to choose scope only. For branch detail, call get_subtree/get_task_snapshot. "
            "For broad scans, call list_tasks_by_filter or get_workload_overview instead of walking roots."
        ),
        "total_nodes": len(nodes),
        "categories": category_payloads,
    }

STRATEGIST_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "query_task_graph",
        "description": (
            "Interpret a task-world question against the Drasil task graph and return compact "
            "evidence. Use this for ambiguous read-only task questions, filtered task lists, "
            "deadline/priority/status questions, and multi-turn context queries before choosing "
            "more specific tools. When answer_candidate is present, treat its summary and task_ids "
            "as the preferred synthesized evidence; raw entity_matches are secondary candidates. "
            "Broad workload questions return a bounded active scope scan ordered by deadline then "
            "priority. Named-task lookups remain candidate lookups when no strong match exists, so "
            "a missing target cannot be mistaken for unrelated tasks in the active scope. "
            "It does not create proposals or apply changes."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "category_name": {"type": ["string", "null"]},
                "filters": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "active_only": {"type": ["boolean", "null"]},
                        "include_archived": {"type": ["boolean", "null"]},
                        "include_backlog": {"type": ["boolean", "null"]},
                        "include_branches": {"type": ["boolean", "null"]},
                        "leaf_only": {"type": ["boolean", "null"]},
                        "deadline": {"type": ["string", "null"]},
                        "deadline_start": {"type": ["string", "null"]},
                        "deadline_end": {"type": ["string", "null"]},
                        "estimate": {"type": ["string", "null"]},
                        "max_estimate_minutes": {"type": ["integer", "null"]},
                        "min_estimate_minutes": {"type": ["integer", "null"]},
                        "statuses": {"type": ["array", "null"], "items": {"type": "string"}},
                        "priorities": {"type": ["array", "null"], "items": {"type": "string"}},
                        "query": {"type": ["string", "null"]},
                        "days": {"type": ["integer", "null"]},
                        "sort": {
                            "type": ["string", "null"],
                            "enum": [
                                "deadline_asc",
                                "deadline_desc",
                                "priority_desc",
                                "priority_asc",
                                "estimate_desc",
                                "estimate_asc",
                                None,
                            ],
                        },
                    },
                },
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "search_entities",
        "description": "Search tasks and branches by title, alias, or context phrase.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "search_notes",
        "description": "Search note bodies and metadata lines when the user references embedded details.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "get_task_snapshot",
        "description": "Fetch one task with deadlines, priority, children, dependencies, and dependents.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
            },
        },
    },
    {
        "type": "function",
        "name": "get_task_metadata",
        "description": "Fetch one task's metadata fields and aliases in a compact read-only shape.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
            },
        },
    },
    {
        "type": "function",
        "name": "get_task_notes",
        "description": "Read note text and note chunks for one task when the user references embedded details.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
            },
        },
    },
    {
        "type": "function",
        "name": "get_task_branch",
        "description": "Inspect a task or branch with nested children for restructure advice.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
                "depth": {"type": "integer"},
            },
        },
    },
    {
        "type": "function",
        "name": "get_parent_chain",
        "description": "Inspect the ancestor path for a task when context depends on parent branches.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
            },
        },
    },
    {
        "type": "function",
        "name": "get_children",
        "description": "Inspect direct children for a task or branch.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
                "depth": {"type": "integer"},
            },
        },
    },
    {
        "type": "function",
        "name": "get_siblings",
        "description": "Inspect siblings under the same parent for disambiguation and local context.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
            },
        },
    },
    {
        "type": "function",
        "name": "get_subtree",
        "description": "Inspect a task subtree with nested descendants for structural planning context.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
                "depth": {"type": "integer"},
            },
        },
    },
    {
        "type": "function",
        "name": "get_related_entities",
        "description": "Look up parents, children, blockers, and dependency relationships around a task.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
            },
        },
    },
    {
        "type": "function",
        "name": "get_session_working_set",
        "description": "Inspect the current session entity memory with salience ordering.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "get_recent_session_actions",
        "description": "Inspect recent session task actions and salient targets that may bias retrieval.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "get_workload_overview",
        "description": (
            "Scan a complete requested category/branch or the full workspace and return bounded "
            "ranked workload pressure: overdue, due-soon, high-priority, stale, active, and backlog "
            "signals plus explicit evidence coverage. Use this as the first tool for broad "
            "prioritization, workload review, or 'what should I do' questions. Pass a category or "
            "branch title for scoped review; pass category_name=null for the full workspace."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "category_name": {"type": ["string", "null"]},
                "days": {"type": "integer"},
            },
        },
    },
    {
        "type": "function",
        "name": "list_tasks_by_filter",
        "description": "List task nodes matching structured metadata filters such as deadline present/missing, estimate missing, status, priority, category, or text query.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "category_name": {"type": ["string", "null"]},
                "filters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "active_only": {"type": "boolean"},
                        "include_archived": {"type": "boolean"},
                        "include_backlog": {"type": "boolean"},
                        "include_branches": {"type": "boolean"},
                        "leaf_only": {"type": "boolean"},
                        "deadline": {"type": ["string", "null"]},
                        "deadline_start": {"type": ["string", "null"]},
                        "deadline_end": {"type": ["string", "null"]},
                        "estimate": {"type": ["string", "null"]},
                        "max_estimate_minutes": {"type": ["integer", "null"]},
                        "min_estimate_minutes": {"type": ["integer", "null"]},
                        "statuses": {"type": "array", "items": {"type": "string"}},
                        "priorities": {"type": "array", "items": {"type": "string"}},
                        "query": {"type": ["string", "null"]},
                        "days": {"type": ["integer", "null"]},
                        "sort": {
                            "type": ["string", "null"],
                            "enum": ["priority_desc", "priority_asc", "estimate_desc", "estimate_asc", None],
                        },
                    },
                },
                "limit": {"type": "integer"},
            },
            "required": ["category_name", "filters", "limit"],
        },
    },
    {
        "type": "function",
        "name": "analyze_tree_cleanup_candidates",
        "description": (
            "Return ranked cleanup signals plus an optional paged organization map for a broad "
            "task-tree cleanup request. Use the organization map when the user wants a complete "
            "workspace/category cleanup, including subtree-preserving moves across workspaces."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "category_name": {"type": ["string", "null"]},
                "limit": {"type": "integer"},
                "include_organization_map": {"type": "boolean"},
                "organization_offset": {"type": "integer"},
                "organization_limit": {"type": "integer"},
            },
            "required": ["category_name", "limit"],
        },
    },
]


def build_runtime_context(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]] | None = None,
    today_override: date | None = None,
    include_task_tree_outline: bool = True,
) -> dict[str, Any]:
    today_value = today_override or date.today()
    category_name_by_id = {
        category.id: category.name for category in categories if category.id is not None
    }
    node_by_id = {node.id: node for node in nodes if node.id is not None}
    staged_summaries = [
        serialize_task(node_by_id[node_id], category_name_by_id=category_name_by_id)
        for node_id in staged_node_ids
        if node_id in node_by_id
    ]
    payload = {
        "today": today_value.isoformat(),
        "active_category": (
            {
                "id": active_category.id,
                "name": active_category.name,
            }
            if active_category is not None
            else None
        ),
        "focus_task": (
            serialize_task(focus_node, category_name_by_id=category_name_by_id)
            if focus_node is not None and focus_node.id is not None
            else None
        ),
        "staged_tasks": staged_summaries,
        "working_set": working_set or [],
        "working_set_contract": {
            "purpose": "recently_touched_tasks_for_conversation_continuity",
            "complete_task_inventory": False,
            "fresh_workload_snapshot": False,
            "instruction": (
                "Do not use the working set alone to answer current deadline, urgency, "
                "workload, or next-task questions. Read the task graph during this turn."
            ),
        },
    }
    if include_task_tree_outline:
        payload["task_tree_outline"] = build_compact_tree_outline(
            categories=categories,
            nodes=nodes,
            active_category_id=active_category.id if active_category is not None else None,
        )
    else:
        payload["task_scope_index"] = build_tree_scope_index(
            categories=categories,
            nodes=nodes,
            active_category_id=active_category.id if active_category is not None else None,
        )
    return payload


def tool_search_entities(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 6), 12))
    return {
        "query": args.get("query"),
        "matches": search_entities(
            query=str(args.get("query") or ""),
            categories=context["categories"],
            nodes=context["nodes"],
            focus_node=context["focus_node"],
            active_category=context["active_category"],
            working_set=context["working_set"],
            limit=limit,
        ),
    }


def tool_search_notes(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 6), 12))
    return {
        "query": args.get("query"),
        "matches": search_notes(
            query=str(args.get("query") or ""),
            categories=context["categories"],
            nodes=context["nodes"],
            limit=limit,
        ),
    }


def tool_get_task_snapshot(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_task_snapshot(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_task_metadata(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_task_metadata(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_task_notes(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_task_notes(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_task_branch(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_task_branch(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        depth=max(1, min(int(args.get("depth") or 2), 4)),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_parent_chain(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_parent_chain(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_children(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_children(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        depth=max(1, min(int(args.get("depth") or 1), 4)),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_siblings(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_siblings(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_subtree(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_subtree(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        depth=max(1, min(int(args.get("depth") or 2), 4)),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_related_entities(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_related_entities(
        task_id=args.get("task_id"),
        task_title=args.get("task_title"),
        categories=context["categories"],
        nodes=context["nodes"],
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
    )


def tool_get_session_working_set(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_session_working_set(context["working_set"])


def tool_get_recent_session_actions(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_recent_session_actions(context["working_set"])


def tool_get_workload_overview(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return get_workload_overview(
        categories=context["categories"],
        nodes=context["nodes"],
        today_value=context["today"],
        category_name=args.get("category_name"),
        days=max(1, min(int(args.get("days") or 7), 30)),
    )


TASK_GRAPH_DEADLINE_RE = re.compile(
    r"\b(?:due|deadline|deadlines?|overdue|today|tomorrow|this\s+week|next\s+week|"
    r"this\s+month|next\s+month|upcoming|next\s+\d+\s+days?)\b",
    re.IGNORECASE,
)
TASK_GRAPH_STATUS_DONE_RE = re.compile(r"\b(?:done|completed|finished)\b", re.IGNORECASE)
TASK_GRAPH_STATUS_TODO_RE = re.compile(r"\b(?:todo|to\s+do|open|active)\b", re.IGNORECASE)
TASK_GRAPH_ESTIMATE_RE = re.compile(r"\b(?:estimate|estimated|time|duration)\b", re.IGNORECASE)
TASK_GRAPH_LIST_QUERY_RE = re.compile(
    r"\b(?:list|show|find|which|what\s+are|what\s+is|what's|give\s+me|any|"
    r"do\s+i\s+have|are\s+there)\b.{0,100}\b(?:tasks?|todos?|items?)\b"
    r"|\b(?:tasks?|todos?|items?)\b.{0,100}"
    r"\b(?:with|due|deadline|priority|important|critical|key|top|urgent|overdue|"
    r"missing|present|next\s+week|this\s+week|next\s+month|this\s+month|upcoming)\b",
    re.IGNORECASE,
)
TASK_GRAPH_SCOPE_QUERY_RE = re.compile(
    r"\b(?:prioriti[sz]e|priorities|workload|full\s+workspace|whole\s+workspace|"
    r"review\s+(?:my\s+)?(?:work|workspace|tasks?|todos?)|"
    r"what\s+should\s+i\s+(?:do|work\s+on|tackle)|"
    r"where\s+should\s+i\s+start|next\s+(?:tasks?|todos?|items?))\b",
    re.IGNORECASE,
)


def _safe_positive_int(value: Any, *, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _active_scope_name(context: dict[str, Any]) -> str | None:
    active_category = context.get("active_category")
    if active_category is None:
        return None
    return str(getattr(active_category, "name", "") or "").strip() or None


def _query_mentions_workspace_scope(query: str) -> bool:
    return bool(
        re.search(
            r"\b(?:whole|entire|all|every|everything|workspace|workspaces|across|"
            r"categories|category|tree)\b",
            query,
            re.IGNORECASE,
        )
    )


def _task_graph_scope_name(
    *,
    query: str,
    requested_scope: Any,
    context: dict[str, Any],
) -> str | None:
    value = str(requested_scope or "").strip()
    if value:
        return value
    if _query_mentions_workspace_scope(query):
        return None
    return _active_scope_name(context)


def _task_graph_scope_nodes(
    *,
    scope: str | None,
    context: dict[str, Any],
) -> list[StrategyNode]:
    """Apply the selected workspace as a hard retrieval boundary."""
    nodes = list(context["nodes"])
    normalized_scope = normalize_text(scope or "")
    if not normalized_scope:
        return nodes

    active_category = context.get("active_category")
    active_category_id = getattr(active_category, "id", None)
    active_category_name = normalize_text(
        str(getattr(active_category, "name", "") or "")
    )
    if (
        isinstance(active_category_id, int)
        and normalized_scope == active_category_name
    ):
        # An active workspace is an identity, not merely a same-name ranking
        # hint. This also prevents duplicate workspace names from widening the
        # lookup.
        allowed_category_ids = {active_category_id}
    else:
        allowed_category_ids = {
            int(category.id)
            for category in context["categories"]
            if isinstance(category.id, int)
            and normalize_text(category.name) == normalized_scope
        }
    return [
        node
        for node in nodes
        if isinstance(node.category_id, int)
        and node.category_id in allowed_category_ids
    ]


def _next_week_bounds(today_value: date) -> tuple[date, date]:
    days_until_monday = (7 - today_value.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    start = today_value + timedelta(days=days_until_monday)
    return start, start + timedelta(days=6)


def _this_week_bounds(today_value: date) -> tuple[date, date]:
    end = today_value + timedelta(days=6 - today_value.weekday())
    return today_value, end


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return start, next_month - timedelta(days=1)


def _this_month_bounds(today_value: date) -> tuple[date, date]:
    _, end = _month_bounds(today_value.year, today_value.month)
    return today_value, end


def _next_month_bounds(today_value: date) -> tuple[date, date]:
    year = today_value.year + (1 if today_value.month == 12 else 0)
    month = 1 if today_value.month == 12 else today_value.month + 1
    return _month_bounds(year, month)


def _infer_deadline_filter(query: str, today_value: date) -> tuple[dict[str, Any], str | None]:
    lowered = query.lower()
    if re.search(r"\b(?:no|missing|without)\s+(?:due|deadline)\b|\bdeadline\s+missing\b", lowered):
        return {"deadline": "missing"}, "missing deadline"
    if "overdue" in lowered:
        return {"deadline": "overdue"}, "overdue"
    if "next week" in lowered:
        start, end = _next_week_bounds(today_value)
        return {
            "deadline": "between",
            "deadline_start": start.isoformat(),
            "deadline_end": end.isoformat(),
        }, f"due next week ({start.isoformat()} to {end.isoformat()})"
    if "next month" in lowered:
        start, end = _next_month_bounds(today_value)
        return {
            "deadline": "between",
            "deadline_start": start.isoformat(),
            "deadline_end": end.isoformat(),
        }, f"due next month ({start.isoformat()} to {end.isoformat()})"
    if "this week" in lowered:
        start, end = _this_week_bounds(today_value)
        return {
            "deadline": "between",
            "deadline_start": start.isoformat(),
            "deadline_end": end.isoformat(),
        }, f"due this week ({start.isoformat()} to {end.isoformat()})"
    if "this month" in lowered:
        start, end = _this_month_bounds(today_value)
        return {
            "deadline": "between",
            "deadline_start": start.isoformat(),
            "deadline_end": end.isoformat(),
        }, f"due this month ({start.isoformat()} to {end.isoformat()})"
    if "tomorrow" in lowered:
        value = today_value + timedelta(days=1)
        return {
            "deadline": "between",
            "deadline_start": value.isoformat(),
            "deadline_end": value.isoformat(),
        }, f"due tomorrow ({value.isoformat()})"
    if "today" in lowered:
        return {
            "deadline": "between",
            "deadline_start": today_value.isoformat(),
            "deadline_end": today_value.isoformat(),
        }, f"due today ({today_value.isoformat()})"
    next_days = re.search(r"\bnext\s+(\d{1,2})\s+days?\b", lowered)
    if next_days:
        days = max(1, min(int(next_days.group(1)), 30))
        return {"deadline": "within", "days": days}, f"due within {days} days"
    if TASK_GRAPH_DEADLINE_RE.search(lowered):
        return {"deadline": "present"}, "has a deadline"
    return {}, None


def _infer_priorities(query: str) -> tuple[list[str], str | None]:
    lowered = query.lower()
    priorities: list[str] = []
    if re.search(
        r"\b(?:urgent|important|critical|key|major|high|red)\b"
        r"|\btop\s+(?:priority|priorities)\b",
        lowered,
    ):
        priorities.append("high")
    if re.search(r"\b(?:medium|orange)\b", lowered):
        priorities.append("medium")
    if re.search(r"\b(?:low|green)\b", lowered):
        priorities.append("low")
    return priorities, ", ".join(priorities) + " priority" if priorities else None


def _infer_statuses(query: str) -> tuple[list[str], str | None]:
    statuses: list[str] = []
    if TASK_GRAPH_STATUS_DONE_RE.search(query):
        statuses.append("done")
    if TASK_GRAPH_STATUS_TODO_RE.search(query):
        statuses.append("todo")
    if re.search(r"\bin[-\s]?progress\b", query, re.IGNORECASE):
        statuses.append("in-progress")
    seen: set[str] = set()
    deduped = [item for item in statuses if not (item in seen or seen.add(item))]
    return deduped, ", ".join(deduped) if deduped else None


def _merge_model_filters(
    *,
    inferred_filters: dict[str, Any],
    supplied_filters: Any,
    allowed_supplied_filter_keys: set[str] | None = None,
) -> dict[str, Any]:
    filters = dict(inferred_filters)
    if isinstance(supplied_filters, dict):
        for key, value in supplied_filters.items():
            if isinstance(key, str) and value not in (None, "", [], {}):
                if allowed_supplied_filter_keys is not None and key not in allowed_supplied_filter_keys:
                    continue
                if allowed_supplied_filter_keys is not None and key in filters:
                    # The raw user request is the authority for predicates it
                    # states explicitly. A model rewrite may add an omitted
                    # supported dimension, but it cannot replace "urgent" with
                    # a noncanonical priority value or shift a relative date
                    # window after the server has resolved it.
                    continue
                filters[key] = value
    return filters


def _allowed_model_filter_keys(
    *,
    source_query: str,
    deadline_label: str | None,
    priority_label: str | None,
    status_label: str | None,
) -> set[str]:
    allowed = {
        "active_only",
        "include_archived",
        "include_backlog",
        "include_branches",
        "leaf_only",
        "sort",
    }
    if deadline_label:
        allowed.update({"deadline", "deadline_start", "deadline_end", "days"})
    if priority_label:
        allowed.add("priorities")
    if status_label:
        allowed.add("statuses")
    if TASK_GRAPH_ESTIMATE_RE.search(source_query):
        allowed.add("estimate")
    return allowed


def _explicit_task_list_query(query: str) -> bool:
    return bool(TASK_GRAPH_LIST_QUERY_RE.search(query))


def _scope_wide_task_query(query: str) -> bool:
    return _explicit_task_list_query(query) or bool(TASK_GRAPH_SCOPE_QUERY_RE.search(query))


def _has_strong_named_task_match(entity_matches: list[dict[str, Any]]) -> bool:
    if not entity_matches or not isinstance(entity_matches[0], dict):
        return False
    score = entity_matches[0].get("score")
    return isinstance(score, (int, float)) and float(score) >= 0.62


CONTEXTUAL_TASK_REFERENCE_RE = re.compile(
    r"\b(?:it|its|that|this|them|they|their|those|one|ones|same)\b"
    r"|\bwhat\s+about\b"
    r"|\b(?:tighter|earlier|later|soonest|closest|highest|lowest)\b",
    re.IGNORECASE,
)


COMPARATIVE_CONTEXT_RE = re.compile(
    r"\b(?:tighter|earlier|later|soonest|closest|compare|between|which)\b",
    re.IGNORECASE,
)


MUTATION_REQUEST_RE = re.compile(
    r"\b(?:add|archive|assign|change|close|complete|create|delete|edit|make|mark|"
    r"move|pin|remove|rename|reopen|reschedule|schedule|set|unpin|update)\b",
    re.IGNORECASE,
)


def _query_needs_contextual_task(query: str) -> bool:
    return bool(CONTEXTUAL_TASK_REFERENCE_RE.search(query))


CONTEXT_QUERY_NON_ENTITY_TOKENS = {
    "about",
    "deadline",
    "due",
    "earlier",
    "estimate",
    "highest",
    "it",
    "its",
    "later",
    "lowest",
    "one",
    "ones",
    "priority",
    "same",
    "status",
    "task",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "those",
    "tighter",
    "what",
    "when",
    "which",
}


def _query_explicitly_names_entity_match(
    query: str,
    entity_matches: list[dict[str, Any]],
) -> bool:
    if not entity_matches:
        return False
    _, title = _entity_match_identity(entity_matches[0])
    query_tokens = {
        token
        for token in normalize_text(query).split()
        if len(token) >= 3 and token not in CONTEXT_QUERY_NON_ENTITY_TOKENS
    }
    title_tokens = set(title.split())
    return bool(query_tokens.intersection(title_tokens))


def _task_graph_contextual_targets(
    *,
    query: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not _query_needs_contextual_task(query):
        return []
    session_memory = context.get("session_memory")
    if not isinstance(session_memory, dict):
        session_memory = {}
    prefer_multiple = bool(COMPARATIVE_CONTEXT_RE.search(query))
    targets: list[dict[str, Any]] = []

    def add_target(raw: Any, source: str) -> None:
        if not isinstance(raw, dict):
            return
        title = str(raw.get("title") or raw.get("task_title") or "").strip()
        task_id = raw.get("task_id") if isinstance(raw.get("task_id"), int) else raw.get("node_id")
        if not isinstance(task_id, int) and not title:
            return
        targets.append(
            {
                "task_id": task_id if isinstance(task_id, int) else None,
                "title": title or None,
                "source": source,
            }
        )

    current_target = session_memory.get("current_target_branch")
    current_source = (
        str(current_target.get("source") or "")
        if isinstance(current_target, dict)
        else ""
    )
    # A UI focus is only a fallback.  A conversational pronoun should bind to
    # the task(s) mentioned in the preceding exchange, even when another task
    # happens to remain selected in the workspace.
    if current_source != "focus_task":
        add_target(current_target, "session_current_target")

    recent_targets = session_memory.get("recent_targets")
    if isinstance(recent_targets, list):
        iterable = recent_targets if prefer_multiple else list(reversed(recent_targets[-2:]))
        for item in iterable:
            add_target(item, "session_recent_target")

    if current_source == "focus_task":
        add_target(current_target, "session_focus_fallback")

    if not targets:
        for item in context.get("working_set") or []:
            add_target(item, "working_set")

    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, str]] = set()
    for target in targets:
        key = (target.get("task_id"), str(target.get("title") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
        if len(unique) >= (4 if prefer_multiple else 1):
            break
    return unique


def _task_graph_contextual_entity_matches(
    *,
    query: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    targets = _task_graph_contextual_targets(query=query, context=context)
    if not targets:
        return []
    categories = context["categories"]
    nodes = context["nodes"]
    category_name_by_id = {
        category.id: category.name
        for category in categories
        if category.id is not None
    }
    node_lookup = {node.id: node for node in nodes if node.id is not None}
    matches: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        node: StrategyNode | None = None
        task_id = target.get("task_id")
        if isinstance(task_id, int):
            node = node_lookup.get(task_id)
        if node is None:
            title = normalize_text(str(target.get("title") or ""))
            if title:
                for candidate in nodes:
                    if normalize_text(candidate.title) == title:
                        node = candidate
                        break
        if node is None:
            continue
        matches.append(
            {
                "title": node.title,
                "score": max(0.82, 0.98 - (index * 0.04)),
                "task": serialize_task(
                    node,
                    category_name_by_id=category_name_by_id,
                    node_lookup=node_lookup,
                ),
                "source": target.get("source") or "session_context",
                "matched_text": target.get("title") or node.title,
            }
        )
    return matches


def _task_graph_should_list(
    query: str,
    filters: dict[str, Any],
    entity_matches: list[dict[str, Any]],
    *,
    structured_filters_supplied: bool = False,
) -> bool:
    # A structured filter is an explicit scope decision made by the model. It
    # outranks phrase matching and session-memory entity salience.
    if structured_filters_supplied:
        return True
    explicit_list = _explicit_task_list_query(query)
    scope_wide_query = _scope_wide_task_query(query)
    strong_named_match = _has_strong_named_task_match(entity_matches)
    if strong_named_match and not scope_wide_query:
        return False
    # A weak or missing title match is not permission to widen a named lookup
    # into the full active scope. Only language that actually asks for a broad
    # scan may return unrelated tasks.
    if not strong_named_match:
        return scope_wide_query
    if (filters.get("deadline") or filters.get("priorities") or filters.get("statuses")) and explicit_list:
        return True
    if filters.get("estimate") or filters.get("query"):
        return True
    return bool(explicit_list)


def _query_task_graph_summary(
    *,
    intent: str,
    scope: str | None,
    count: int,
    entity_match_count: int,
    filter_labels: list[str],
) -> str:
    scope_label = scope or "all categories"
    if intent == "filtered_task_list":
        label = " and ".join(filter_labels) if filter_labels else "matching task"
        return (
            f"Queried the task graph for {label} in {scope_label} and found "
            f"{count} task{'s' if count != 1 else ''}."
        )
    if entity_match_count:
        return (
            f"Queried the task graph for named-task context in {scope_label} and found "
            f"{entity_match_count} candidate{'s' if entity_match_count != 1 else ''}."
        )
    return f"Queried the task graph in {scope_label}; no strong task evidence matched."


def _task_brief(task: dict[str, Any]) -> str:
    title = str(task.get("title") or task.get("task_title") or "").strip()
    if not title:
        return ""
    details: list[str] = []
    priority = str(task.get("priority_label") or task.get("priority") or "").strip()
    if priority:
        details.append(priority)
    status = str(task.get("status") or "").strip()
    if status:
        details.append(status.replace("-", " "))
    deadline = str(task.get("deadline") or task.get("deadline_at") or "").strip()
    if deadline and deadline.lower() != "no deadline":
        details.append(f"due {deadline}")
    estimate = str(task.get("estimate") or "").strip()
    if estimate and estimate.lower() != "no estimate":
        details.append(f"estimated {estimate}")
    return title + (f" - {', '.join(details)}" if details else "")


def _answer_candidate_from_named_snapshot(
    *,
    query: str,
    snapshots: list[dict[str, Any]],
    entity_matches: list[dict[str, Any]],
    requested_fact_field: str | None = None,
) -> dict[str, Any] | None:
    if not snapshots or not _has_strong_named_task_match(entity_matches):
        return None
    task = snapshots[0].get("task")
    if not isinstance(task, dict):
        return None
    title = str(task.get("title") or "").strip()
    if not title:
        return None
    lowered = query.lower()
    top_title = normalize_text(title)
    query_norm = normalize_text(query)
    if len(snapshots) > 1 and not (
        top_title and len(top_title.split()) >= 2 and top_title in query_norm
    ):
        rows: list[str] = []
        task_ids: list[Any] = []
        for snapshot in snapshots[:4]:
            candidate_task = snapshot.get("task") if isinstance(snapshot, dict) else None
            if not isinstance(candidate_task, dict):
                continue
            brief = _task_brief(candidate_task)
            if brief:
                rows.append(brief)
                task_ids.append(candidate_task.get("task_id") or candidate_task.get("id"))
        if rows:
            return {
                "summary": "I found multiple matching tasks: " + "; ".join(rows) + ".",
                "confidence": 0.9,
                "source": "query_task_graph_multi_named_snapshot",
                "task_ids": task_ids,
            }
    effective_fact_field = requested_fact_field or _task_graph_requested_fact_field(lowered)
    if effective_fact_field == "deadline":
        summary = f"{title} is due {task.get('deadline') or task.get('deadline_at') or 'No deadline'}."
    elif effective_fact_field == "priority":
        summary = f"{title} is {task.get('priority_label') or task.get('priority') or 'unspecified priority'}."
    elif effective_fact_field == "estimate":
        summary = f"{title} is estimated at {task.get('estimate') or 'No estimate'}."
    elif effective_fact_field == "status":
        summary = f"{title} is {task.get('status') or 'unknown status'}."
    else:
        summary = _task_brief(task) + "."
    return {
        "summary": summary,
        "confidence": 0.93,
        "source": "query_task_graph_named_snapshot",
        "task_ids": [task.get("task_id") or task.get("id")],
    }


def _task_graph_requested_fact_field(query: str) -> str | None:
    """Return the explicit fact predicate carried by one task question."""

    lowered = str(query or "").lower()
    if re.search(r"\b(?:due|deadline|when|tighter|earlier|soonest|closest)\b", lowered):
        return "deadline"
    if re.search(r"\b(?:priority|urgent|high|medium|low|red|orange|green)\b", lowered):
        return "priority"
    if re.search(r"\b(?:estimate|how long|duration|time)\b", lowered):
        return "estimate"
    if re.search(r"\bstatus\b", lowered):
        return "status"
    return None


def _contextual_task_graph_fact_field(
    *,
    source_query: str,
    context: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Conserve the predicate of the immediately preceding referential turn.

    A follow-up such as ``What about the Acme one?`` changes the task while
    retaining the fact being discussed.  Provider-authored lookup text is not
    allowed to silently replace that predicate.  Fresh named questions and
    follow-ups with an explicit predicate remain self-contained.
    """

    explicit = _task_graph_requested_fact_field(source_query)
    if explicit is not None or not _query_needs_contextual_task(source_query):
        return explicit, "current_user_request" if explicit is not None else None

    session_memory = context.get("session_memory")
    recent_requests = (
        session_memory.get("recent_user_requests")
        if isinstance(session_memory, dict)
        else None
    )
    if not isinstance(recent_requests, list):
        return None, None

    source_normalized = normalize_text(source_query)
    for item in reversed(recent_requests):
        if not isinstance(item, dict):
            continue
        prior_query = str(item.get("summary") or "").strip()
        if not prior_query or normalize_text(prior_query) == source_normalized:
            continue
        inherited = _task_graph_requested_fact_field(prior_query)
        return (
            (inherited, "immediately_preceding_user_request")
            if inherited is not None
            else (None, None)
        )
    return None, None


def _answer_candidate_from_task_list(
    *,
    tasks: list[dict[str, Any]],
    count: int,
    scope: str | None,
    filter_labels: list[str],
    truncated: bool,
) -> dict[str, Any] | None:
    if count < 0 or truncated:
        return None
    scope_label = scope or "all categories"
    label = " and ".join(filter_labels) if filter_labels else "matching"
    if count == 0:
        summary = f"I did not find any {label} tasks in {scope_label}."
        confidence = 0.88
    else:
        rows = [brief for brief in (_task_brief(task) for task in tasks[:8]) if brief]
        if not rows:
            return None
        task_word = "task" if count == 1 else "tasks"
        summary = f"I found {count} {label} {task_word} in {scope_label}: " + "; ".join(rows) + "."
        confidence = 0.92 if count <= len(rows) else 0.86
    return {
        "summary": summary,
        "confidence": confidence,
        "source": "query_task_graph_filtered_list",
        "task_ids": [
            task.get("task_id") or task.get("id")
            for task in tasks[:8]
            if isinstance(task, dict)
        ],
    }


def _multiple_named_matches_ambiguous(
    *,
    query: str,
    entity_matches: list[dict[str, Any]],
) -> bool:
    if len(entity_matches) <= 1:
        return False
    top = entity_matches[0]
    second = entity_matches[1]
    top_score = top.get("score")
    second_score = second.get("score")
    if not isinstance(top_score, (int, float)) or not isinstance(second_score, (int, float)):
        return False
    if max(float(top_score), float(second_score)) < 0.4:
        return False
    if float(top_score) - float(second_score) >= 0.16:
        return False
    top_title = normalize_text(str(top.get("title") or ""))
    query_norm = normalize_text(query)
    if top_title and len(top_title.split()) >= 2 and top_title in query_norm:
        return False
    return True


def _entity_match_identity(match: dict[str, Any] | None) -> tuple[Any, str]:
    if not isinstance(match, dict):
        return (None, "")
    task = match.get("task") if isinstance(match.get("task"), dict) else {}
    task_id = task.get("task_id", task.get("id"))
    title = normalize_text(str(match.get("title") or task.get("title") or ""))
    return (task_id if isinstance(task_id, int) else None, title)


def _model_named_match_conflicts_with_user_request(
    *,
    source_query: str,
    model_matches: list[dict[str, Any]],
    source_matches: list[dict[str, Any]],
) -> bool:
    """Detect a model-authored lookup that drifted to an unmentioned task.

    The model may narrow a compound request to one task that the user actually
    named.  It may not replace the user's named target with a different task.
    Keep that distinction structural by comparing retrieval identities and the
    raw request, rather than special-casing titles or prompt phrases.
    """

    if not (
        _has_strong_named_task_match(model_matches)
        and _has_strong_named_task_match(source_matches)
    ):
        return False
    model_id, model_title = _entity_match_identity(model_matches[0])
    source_id, source_title = _entity_match_identity(source_matches[0])
    if (model_id, model_title) == (source_id, source_title):
        return False
    source_norm = normalize_text(source_query)
    model_target_was_named = bool(model_title and model_title in source_norm)
    if model_target_was_named:
        return False
    source_score = source_matches[0].get("score")
    return isinstance(source_score, (int, float)) and float(source_score) >= 0.85


def tool_query_task_graph(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    original_user_request = str(context.get("original_user_request") or "").strip()
    source_query = original_user_request or query
    today_value = context["today"]
    limit = _safe_positive_int(args.get("limit"), default=12, maximum=30)
    scope = _task_graph_scope_name(
        query=source_query,
        requested_scope=args.get("category_name"),
        context=context,
    )
    scoped_nodes = _task_graph_scope_nodes(scope=scope, context=context)
    scoped_context = {**context, "nodes": scoped_nodes}
    deadline_filters, deadline_label = _infer_deadline_filter(source_query, today_value)
    priorities, priority_label = _infer_priorities(source_query)
    statuses, status_label = _infer_statuses(source_query)
    inferred_filters: dict[str, Any] = {
        "active_only": statuses != ["done"],
        "include_archived": False,
        "include_backlog": True,
        "include_branches": True,
        "leaf_only": False,
    }
    inferred_filters.update(deadline_filters)
    if priorities:
        inferred_filters["priorities"] = priorities
        inferred_filters["sort"] = "priority_desc"
    if statuses:
        inferred_filters["statuses"] = statuses
    if TASK_GRAPH_ESTIMATE_RE.search(query):
        lowered = query.lower()
        if re.search(r"\b(?:missing|no|without)\b", lowered):
            inferred_filters["estimate"] = "missing"
        elif re.search(r"\b(?:present|has|with)\b", lowered):
            inferred_filters["estimate"] = "present"
    filters = _merge_model_filters(
        inferred_filters=inferred_filters,
        supplied_filters=args.get("filters"),
        allowed_supplied_filter_keys=(
            _allowed_model_filter_keys(
                source_query=source_query,
                deadline_label=deadline_label,
                priority_label=priority_label,
                status_label=status_label,
            )
            if original_user_request
            else None
        ),
    )
    retrieval_query = query or source_query
    entity_matches = search_entities(
        query=retrieval_query,
        categories=context["categories"],
        nodes=scoped_nodes,
        focus_node=context["focus_node"],
        active_category=context["active_category"],
        working_set=context["working_set"],
        limit=min(6, limit),
    )
    source_request_resolution: dict[str, Any] | None = None
    if original_user_request and normalize_text(original_user_request) != normalize_text(retrieval_query):
        source_entity_matches = search_entities(
            query=source_query,
            categories=context["categories"],
            nodes=scoped_nodes,
            focus_node=context["focus_node"],
            active_category=context["active_category"],
            working_set=context["working_set"],
            limit=min(6, limit),
        )
        source_is_ambiguous = _multiple_named_matches_ambiguous(
            query=source_query,
            entity_matches=source_entity_matches,
        )
        model_drifted = _model_named_match_conflicts_with_user_request(
            source_query=source_query,
            model_matches=entity_matches,
            source_matches=source_entity_matches,
        )
        if source_is_ambiguous or model_drifted:
            entity_matches = source_entity_matches
            source_request_resolution = {
                "source": "original_user_request",
                "reason": (
                    "The model query targeted a task the user did not name; the raw "
                    "user request remains authoritative."
                    if model_drifted
                    else "The model query narrowed the target, but the raw user request "
                    "still has multiple plausible named-task matches."
                ),
                "model_query": retrieval_query,
                "source_user_request": source_query,
                "target_titles": [
                    match.get("title")
                    for match in source_entity_matches[:4]
                    if isinstance(match.get("title"), str)
                ],
            }
    structured_filters_supplied = isinstance(args.get("filters"), dict) and bool(args["filters"])
    if (
        source_request_resolution is not None
        and _has_strong_named_task_match(entity_matches)
        and not _scope_wide_task_query(source_query)
    ):
        structured_filters_supplied = False
    contextual_resolution: dict[str, Any] | None = None
    if (
        not structured_filters_supplied
        and _query_needs_contextual_task(query)
        and not _query_explicitly_names_entity_match(query, entity_matches)
    ):
        contextual_matches = _task_graph_contextual_entity_matches(
            query=query,
            context=scoped_context,
        )
        if contextual_matches:
            entity_matches = contextual_matches
            contextual_resolution = {
                "source": "session_memory",
                "reason": "Resolved contextual task reference from recent session targets.",
                "target_titles": [
                    match.get("title")
                    for match in contextual_matches
                    if isinstance(match.get("title"), str)
                ],
            }
    should_list = _task_graph_should_list(
        source_query,
        filters,
        entity_matches,
        structured_filters_supplied=structured_filters_supplied,
    )

    listed: dict[str, Any] | None = None
    tasks: list[dict[str, Any]] = []
    if should_list:
        listed = list_tasks_by_filter(
            categories=context["categories"],
            nodes=context["nodes"],
            today_value=today_value,
            category_name=scope,
            filters=filters,
            limit=limit,
        )
        raw_tasks = listed.get("tasks")
        tasks = raw_tasks if isinstance(raw_tasks, list) else []

    strong_entity_match = _has_strong_named_task_match(entity_matches)
    named_ambiguity = _multiple_named_matches_ambiguous(
        query=source_query,
        entity_matches=entity_matches,
    )
    snapshots: list[dict[str, Any]] = []
    if not should_list and (strong_entity_match or named_ambiguity):
        snapshot_limit = 4 if contextual_resolution is not None else 2
        for match in entity_matches[:snapshot_limit]:
            task = match.get("task") if isinstance(match, dict) else None
            task_id = task.get("id") if isinstance(task, dict) else None
            task_title = task.get("title") if isinstance(task, dict) else None
            if not isinstance(task_id, int) and not isinstance(task_title, str):
                continue
            snapshot = get_task_snapshot(
                task_id=task_id if isinstance(task_id, int) else None,
                task_title=task_title if isinstance(task_title, str) else None,
                categories=context["categories"],
                nodes=context["nodes"],
                focus_node=context["focus_node"],
                active_category=context["active_category"],
                working_set=context["working_set"],
            )
            if isinstance(snapshot.get("task"), dict):
                snapshots.append(snapshot)

    filter_labels = [
        label
        for label in (priority_label, status_label, deadline_label)
        if label
    ]
    count = int(listed.get("count") or len(tasks)) if isinstance(listed, dict) else len(snapshots)
    intent = "filtered_task_list" if should_list else "named_task_context"
    ambiguity_query = source_query if source_request_resolution is not None else query
    requested_fact_field, requested_fact_field_source = _contextual_task_graph_fact_field(
        source_query=source_query,
        context=context,
    )
    answer_candidate = (
        _answer_candidate_from_task_list(
            tasks=tasks,
            count=count,
            scope=scope,
            filter_labels=filter_labels,
            truncated=bool(listed.get("truncated")) if isinstance(listed, dict) else False,
        )
        if should_list
        else _answer_candidate_from_named_snapshot(
            query=ambiguity_query,
            snapshots=snapshots,
            entity_matches=entity_matches,
            requested_fact_field=requested_fact_field,
        )
    )
    ambiguity = {
        "multiple_named_matches": _multiple_named_matches_ambiguous(
            query=ambiguity_query,
            entity_matches=entity_matches,
        ),
        "filter_labels": filter_labels,
    }
    read_only_answer_request = not bool(MUTATION_REQUEST_RE.search(source_query)) and bool(
        re.search(
            r"\b(?:what|when|which|who|where|how|tell|show|list|find|compare|"
            r"review|give\s+me|do\s+i\s+have|are\s+there)\b",
            source_query,
            re.IGNORECASE,
        )
        or COMPARATIVE_CONTEXT_RE.search(source_query)
    )
    answer_resolves_ambiguity = (
        isinstance(answer_candidate, dict)
        and answer_candidate.get("source") == "query_task_graph_multi_named_snapshot"
        and read_only_answer_request
    )
    answer_ready = (
        isinstance(answer_candidate, dict)
        and float(answer_candidate.get("confidence") or 0.0) >= 0.9
        and (answer_resolves_ambiguity or not bool(ambiguity.get("multiple_named_matches")))
    )
    suggested_next_tools: list[dict[str, Any]] = []
    if tasks:
        suggested_next_tools.append(
            {
                "name": "get_task_snapshot",
                "reason": "Inspect one listed task before proposing a change or giving detailed advice.",
            }
        )
    if strong_entity_match:
        suggested_next_tools.append(
            {
                "name": "get_task_snapshot",
                "arguments": {
                    "task_title": entity_matches[0].get("title"),
                },
                "reason": "Read exact task fields for the top named-task candidate.",
            }
        )
    if not tasks and not strong_entity_match:
        suggested_next_tools.append(
            {
                "name": "search_tool_catalog",
                "arguments": {"query": query},
                "reason": "No graph evidence matched; choose a more specific retrieval tool if needed.",
            }
        )

    summary = _query_task_graph_summary(
        intent=intent,
        scope=scope,
        count=count,
        entity_match_count=len(entity_matches) if strong_entity_match else 0,
        filter_labels=filter_labels,
    )
    clarification_needed = bool(ambiguity.get("multiple_named_matches")) and not answer_resolves_ambiguity
    ambiguity_choices = [
        {
            "kind": "entity",
            "label": str(task.get("title") or "").strip(),
            "value": str(task.get("task_id") or task.get("id") or ""),
            "description": _task_brief(task),
            "target_node_id": task.get("task_id") or task.get("id"),
            "follow_up_prompt": f'Use the task named "{str(task.get("title") or "").strip()}"',
        }
        for snapshot in snapshots[:4]
        if isinstance(snapshot, dict)
        and isinstance((task := snapshot.get("task")), dict)
        and str(task.get("title") or "").strip()
        and isinstance(task.get("task_id") or task.get("id"), int)
    ]
    clarification = (
        {
            "message": "Which matching task did you mean?",
            "entity_choices": ambiguity_choices,
            "field_choices": [],
            "scoped_follow_up_prompts": [
                choice["follow_up_prompt"] for choice in ambiguity_choices
            ],
        }
        if clarification_needed and ambiguity_choices
        else None
    )
    return {
        "query": query,
        "source_user_request": original_user_request or None,
        "scope": scope or "all categories",
        "interpreted_intent": {
            "kind": intent,
            "read_only": True,
            "model_owns_final_answer": True,
        },
        "answer_ready": answer_ready,
        "evidence_coverage": {
            "kind": "filtered_scope_scan" if should_list else "candidate_lookup",
            "complete_scope_scan": bool(should_list),
            "adequate_for_scope_wide_prioritization": bool(should_list),
            "scope": scope or "all categories",
            "returned_task_count": len(tasks) if should_list else len(snapshots),
            "matched_task_count": count if should_list else len(snapshots),
            "bounded_result_truncated": (
                bool(listed.get("truncated"))
                if should_list and isinstance(listed, dict)
                else False
            ),
        },
        "answer_candidate": answer_candidate,
        "answer_contract": {
            "preferred_summary_field": "answer_candidate.summary",
            "preferred_task_ids_field": "answer_candidate.task_ids",
            "raw_candidates_are_secondary": True,
            "model_final_must_preserve_list_members": bool(should_list and tasks),
        },
        "inferred_filters": filters,
        "count": count,
        "truncated": bool(listed.get("truncated")) if isinstance(listed, dict) else False,
        "tasks": tasks,
        "entity_matches": entity_matches,
        "task_snapshots": snapshots,
        "contextual_resolution": contextual_resolution,
        "requested_fact_field": requested_fact_field,
        "requested_fact_field_source": requested_fact_field_source,
        "source_request_resolution": source_request_resolution,
        "lookup_status": (
            "scope_scan"
            if should_list
            else ("matched" if strong_entity_match else "not_found")
        ),
        "ambiguity": ambiguity,
        "clarification_needed": clarification_needed,
        "clarification": clarification,
        "suggested_next_tools": suggested_next_tools,
        "model_instruction": (
            "The requested mutation target is ambiguous. Return the supplied clarification "
            "and entity choices now; do not call a proposal or mutation-authoring tool until "
            "the user selects exactly one task."
            if clarification is not None
            else "This is a bounded filtered scope result; synthesize from answer_candidate and tasks."
            if should_list
            else (
                "No strong named task matched. Do not treat weak candidates as the user's target "
                "and do not claim that the task is ambiguous. State that the named task was not "
                "found in the current task graph, then ask the user to verify its name or scope."
                if not strong_entity_match
                else
                "This is candidate lookup evidence, not a workload snapshot. If the user's goal "
                "is scope-wide review or prioritization, do not finalize from these candidates; "
                "call get_workload_overview for the requested category or full workspace."
            )
        ),
        "observation_summary": summary,
    }


def tool_list_tasks_by_filter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    filters = args.get("filters")
    return list_tasks_by_filter(
        categories=context["categories"],
        nodes=context["nodes"],
        today_value=context["today"],
        category_name=args.get("category_name"),
        filters=filters if isinstance(filters, dict) else {},
        limit=max(1, min(int(args.get("limit") or 40), 100)),
    )


LOW_INFORMATION_TITLE_TOKENS = {
    "archive",
    "bucket",
    "cleanup",
    "draft",
    "follow",
    "misc",
    "notes",
    "old",
    "polish",
    "project",
    "projects",
    "research",
    "side",
    "todo",
    "update",
}


def _cleanup_scope_nodes(
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    active_category: Category | None,
    category_name: Any,
) -> tuple[str, list[StrategyNode]]:
    normalized_scope = normalize_text(str(category_name or ""))
    category_by_id = {category.id: category for category in categories if category.id is not None}
    selected_category: Category | None = None
    if normalized_scope:
        selected_category = next(
            (
                category
                for category in categories
                if normalize_text(category.name) == normalized_scope
            ),
            None,
        )
    if selected_category is None and active_category is not None:
        selected_category = active_category
    if selected_category is not None and selected_category.id is not None:
        return selected_category.name, [
            node for node in nodes if node.category_id == selected_category.id
        ]
    return "all categories", [
        node
        for node in nodes
        if node.category_id in category_by_id
    ]


def _cleanup_path(node: StrategyNode, node_lookup: dict[int, StrategyNode]) -> str:
    return " / ".join(
        [parent.title for parent in build_parent_chain(node, node_lookup)] + [node.title]
    )


def _title_signature(title: str) -> str:
    tokens = [
        token
        for token in normalize_text(title).split()
        if token not in {"a", "an", "and", "for", "in", "of", "the", "to"}
    ]
    return " ".join(tokens)


def _cleanup_candidate_signals(
    node: StrategyNode,
    *,
    duplicate_title_count: int,
    children_by_parent: dict[int, list[StrategyNode]],
) -> list[str]:
    signals: list[str] = []
    tags = {tag.lower() for tag in decode_tags(node.tags)}
    stale_marker = stale_days(node)
    if duplicate_title_count > 1:
        signals.append(f"title overlaps with {duplicate_title_count - 1} other active task")
    if stale_marker is not None and stale_marker >= 30:
        signals.append(f"stale for {stale_marker} days")
    if "state:deferred" in tags:
        signals.append("marked deferred")
    if node.is_backlog:
        signals.append("in backlog")
    if normalize_text(node.priority) == "low":
        signals.append("low priority")
    if not node.deadline_at:
        signals.append("no deadline")
    if node.time_estimate_minutes is None:
        signals.append("no estimate")
    if node.id is not None and not children_by_parent.get(node.id):
        title_tokens = set(_title_signature(node.title).split())
        if title_tokens.intersection(LOW_INFORMATION_TITLE_TOKENS):
            signals.append("low-information title")
    return signals


def _messy_branch_signals(
    node: StrategyNode,
    children: list[StrategyNode],
) -> list[str]:
    signals: list[str] = []
    if len(children) >= 6:
        signals.append(f"{len(children)} direct children")
    child_signatures: dict[str, int] = {}
    for child in children:
        first_token = (_title_signature(child.title).split() or [""])[0]
        if first_token:
            child_signatures[first_token] = child_signatures.get(first_token, 0) + 1
    repeated_prefixes = [
        token for token, count in child_signatures.items() if count >= 3
    ]
    if repeated_prefixes:
        signals.append("repeated child prefixes: " + ", ".join(sorted(repeated_prefixes)[:3]))
    empty_group_children = [
        child for child in children if child.id is not None and child.node_type != "habit"
    ]
    if node.node_type != "habit" and children and len(empty_group_children) == len(children):
        low_metadata_count = sum(
            1
            for child in children
            if not child.deadline_at and child.time_estimate_minutes is None
        )
        if low_metadata_count >= 4:
            signals.append(f"{low_metadata_count} children lack deadline and estimate")
    return signals


def tool_analyze_tree_cleanup_candidates(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    categories = context["categories"]
    nodes = context["nodes"]
    scope_label, scoped_nodes = _cleanup_scope_nodes(
        categories=categories,
        nodes=nodes,
        active_category=context["active_category"],
        category_name=args.get("category_name"),
    )
    safe_limit = max(1, min(int(args.get("limit") or 8), 20))
    node_lookup = build_node_lookup(nodes)
    category_name_by_id = build_category_name_map(categories)
    children_by_parent = build_children_map(nodes)
    active_nodes = [
        node
        for node in scoped_nodes
        if node.id is not None and not is_archived(node) and not is_completed(node)
    ]
    title_counts: dict[str, int] = {}
    for node in active_nodes:
        signature = _title_signature(node.title)
        if signature:
            title_counts[signature] = title_counts.get(signature, 0) + 1

    cleanup_candidates: list[dict[str, Any]] = []
    for node in active_nodes:
        signature = _title_signature(node.title)
        signals = _cleanup_candidate_signals(
            node,
            duplicate_title_count=title_counts.get(signature, 0),
            children_by_parent=children_by_parent,
        )
        if not signals:
            continue
        score = min(
            1.0,
            0.18 * len(signals)
            + (0.2 if any("stale" in signal for signal in signals) else 0.0)
            + (0.16 if any("deferred" in signal for signal in signals) else 0.0)
            + (0.12 if any("overlaps" in signal for signal in signals) else 0.0),
        )
        cleanup_candidates.append(
            {
                "task_id": int(node.id),
                "title": node.title,
                "path": _cleanup_path(node, node_lookup),
                "score": round(score, 2),
                "signals": signals,
                "suggested_action": (
                    "review_delete_or_archive"
                    if score >= 0.58
                    else "review_metadata_or_scope"
                ),
                "task": serialize_task(
                    node,
                    category_name_by_id=category_name_by_id,
                    node_lookup=node_lookup,
                ),
            }
        )
    cleanup_candidates.sort(
        key=lambda item: (-float(item["score"]), str(item["path"]).lower())
    )

    messy_branches: list[dict[str, Any]] = []
    for node in active_nodes:
        if node.id is None:
            continue
        children = children_by_parent.get(node.id, [])
        signals = _messy_branch_signals(node, children)
        if not signals:
            continue
        messy_branches.append(
            {
                "task_id": int(node.id),
                "title": node.title,
                "path": _cleanup_path(node, node_lookup),
                "child_count": len(children),
                "signals": signals,
                "child_sample": [child.title for child in children[:8]],
                "suggested_action": "inspect_branch_for_restructure",
            }
        )
    messy_branches.sort(key=lambda item: (-int(item["child_count"]), str(item["path"]).lower()))

    limited_candidates = cleanup_candidates[:safe_limit]
    limited_branches = messy_branches[:safe_limit]
    organization_map: list[dict[str, Any]] = []
    destination_catalog: list[dict[str, Any]] = []
    organization_offset = max(0, int(args.get("organization_offset") or 0))
    organization_limit = max(1, min(int(args.get("organization_limit") or 160), 250))
    organization_nodes = sorted(
        [node for node in scoped_nodes if node.id is not None and not is_archived(node)],
        key=lambda node: (_cleanup_path(node, node_lookup).lower(), int(node.id or 0)),
    )
    if bool(args.get("include_organization_map")):
        for node in organization_nodes[
            organization_offset : organization_offset + organization_limit
        ]:
            children = children_by_parent.get(int(node.id), [])
            organization_map.append(
                {
                    "task_id": int(node.id),
                    "title": node.title,
                    "category_id": int(node.category_id),
                    "category_name": category_name_by_id.get(node.category_id, "Unknown"),
                    "parent_task_id": int(node.parent_id) if node.parent_id is not None else None,
                    "path": _cleanup_path(node, node_lookup),
                    "child_count": len(children),
                    "child_sample": [child.title for child in children[:5]],
                    "status": node.status,
                    "priority": node.priority,
                    "deadline": node.deadline_at,
                    "estimate_minutes": node.time_estimate_minutes,
                    "is_backlog": bool(node.is_backlog),
                    "tags": decode_tags(node.tags),
                }
            )
        for category in sorted(categories, key=lambda item: (item.sort_order, item.name.lower())):
            if category.id is None:
                continue
            roots = [
                node.title
                for node in nodes
                if node.category_id == category.id
                and node.parent_id is None
                and not is_archived(node)
            ]
            destination_catalog.append(
                {
                    "category_id": int(category.id),
                    "name": category.name,
                    "root_samples": roots[:10],
                }
            )
    top_candidate_titles = [item["title"] for item in limited_candidates[:3]]
    top_branch_titles = [item["title"] for item in limited_branches[:3]]
    summary_parts: list[str] = []
    if top_candidate_titles:
        summary_parts.append("cleanup candidates: " + ", ".join(top_candidate_titles))
    if top_branch_titles:
        summary_parts.append("messy branches: " + ", ".join(top_branch_titles))
    if not summary_parts:
        summary_parts.append("no strong cleanup candidates found")
    observation_summary = (
        f"Analyzed {len(active_nodes)} active task"
        f"{'s' if len(active_nodes) != 1 else ''} in {scope_label}; "
        + "; ".join(summary_parts)
        + "."
    )
    return {
        "scope": scope_label,
        "task_count": len(scoped_nodes),
        "active_task_count": len(active_nodes),
        "cleanup_candidate_count": len(cleanup_candidates),
        "messy_branch_count": len(messy_branches),
        "cleanup_candidates": limited_candidates,
        "messy_branches": limited_branches,
        "organization_map": organization_map,
        "organization_map_count": len(organization_nodes),
        "organization_map_offset": organization_offset,
        "organization_map_complete": (
            not bool(args.get("include_organization_map"))
            or organization_offset + len(organization_map) >= len(organization_nodes)
        ),
        "next_organization_offset": (
            organization_offset + len(organization_map)
            if bool(args.get("include_organization_map"))
            and organization_offset + len(organization_map) < len(organization_nodes)
            else None
        ),
        "destination_catalog": destination_catalog,
        "recommended_next_step": (
            "For a requested full cleanup, classify the complete organization_map, preserve each "
            "moved subtree, and compile one validated batch proposal. Retrieve the next page first "
            "when organization_map_complete is false. Prefer archive over deletion when uncertainty "
            "remains. For a read-only review, summarize the strongest signals without proposing changes."
        ),
        "observation_summary": observation_summary,
    }


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "query_task_graph": tool_query_task_graph,
    "search_entities": tool_search_entities,
    "search_notes": tool_search_notes,
    "get_task_snapshot": tool_get_task_snapshot,
    "get_task_metadata": tool_get_task_metadata,
    "get_task_notes": tool_get_task_notes,
    "get_task_branch": tool_get_task_branch,
    "get_parent_chain": tool_get_parent_chain,
    "get_children": tool_get_children,
    "get_siblings": tool_get_siblings,
    "get_subtree": tool_get_subtree,
    "get_related_entities": tool_get_related_entities,
    "get_session_working_set": tool_get_session_working_set,
    "get_recent_session_actions": tool_get_recent_session_actions,
    "get_workload_overview": tool_get_workload_overview,
    "list_tasks_by_filter": tool_list_tasks_by_filter,
    "analyze_tree_cleanup_candidates": tool_analyze_tree_cleanup_candidates,
}


def execute_task_read(
    name: str,
    arguments: dict[str, Any],
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None = None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]] | None = None,
    session_memory: dict[str, Any] | None = None,
    today_override: date | None = None,
    original_user_request: str | None = None,
) -> dict[str, Any]:
    """Execute one deterministic task-read handler."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    return handler(
        arguments,
        {
            "categories": categories,
            "nodes": nodes,
            "focus_node": focus_node,
            "active_category": active_category,
            "staged_node_ids": staged_node_ids,
            "working_set": working_set or [],
            "session_memory": session_memory or {},
            "today": today_override or date.today(),
            "original_user_request": original_user_request,
        },
    )
