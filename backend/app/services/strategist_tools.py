"""Compatibility facade for the retired Strategist task-read dispatcher.

New application and transport code imports ``task_read_handlers`` or
``agent_kernel.task_read_service``. This module remains only as a stable parity
surface until the compatibility retirement gate is complete.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.task_read_handlers import (
    STRATEGIST_TOOL_DEFINITIONS,
    TOOL_HANDLERS,
    build_compact_tree_outline,
    build_runtime_context,
    build_tree_scope_index,
    execute_task_read,
)

__all__ = [
    "STRATEGIST_TOOL_DEFINITIONS",
    "TOOL_HANDLERS",
    "build_compact_tree_outline",
    "build_runtime_context",
    "build_tree_scope_index",
    "execute_strategist_tool",
]


def execute_strategist_tool(
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
    return execute_task_read(
        name,
        arguments,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        session_memory=session_memory,
        today_override=today_override,
        original_user_request=original_user_request,
    )
