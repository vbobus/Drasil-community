"""Provider-neutral application boundary for task-graph reads.

The current native implementation reuses the proven deterministic read
handlers while removing production Agent Kernel execution from the legacy
``execute_strategist_tool`` dispatcher. A later local MCP client can implement
the same protocol without changing the kernel's semantic tool contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.task_read_handlers import TOOL_HANDLERS


class TaskReadToolUnavailable(ValueError):
    """The requested name is not part of the native task-read boundary."""


@dataclass(frozen=True)
class TaskReadInvocation:
    categories: list[Category]
    nodes: list[StrategyNode]
    focus_node: StrategyNode | None
    active_category: Category | None
    staged_node_ids: tuple[int, ...]
    working_set: tuple[dict[str, Any], ...]
    session_memory: dict[str, Any]
    local_date: date
    original_user_request: str | None = None

    def handler_context(self) -> dict[str, Any]:
        return {
            "categories": self.categories,
            "nodes": self.nodes,
            "focus_node": self.focus_node,
            "active_category": self.active_category,
            "staged_node_ids": list(self.staged_node_ids),
            "working_set": [dict(item) for item in self.working_set],
            "session_memory": dict(self.session_memory),
            "today": self.local_date,
            "original_user_request": self.original_user_request,
        }


class TaskReadApplicationService(Protocol):
    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invocation: TaskReadInvocation,
    ) -> dict[str, Any]: ...


class NativeTaskReadApplicationService:
    """Execute deterministic task reads without the legacy dispatcher."""

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invocation: TaskReadInvocation,
    ) -> dict[str, Any]:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            raise TaskReadToolUnavailable(
                f"Unknown native task-read tool: {name}"
            )
        return handler(dict(arguments), invocation.handler_context())


NATIVE_TASK_READ_SERVICE: TaskReadApplicationService = (
    NativeTaskReadApplicationService()
)


def native_task_read_tool_names() -> frozenset[str]:
    return frozenset(TOOL_HANDLERS)


def execute_native_task_read(
    name: str,
    arguments: dict[str, Any],
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]] | None = None,
    session_memory: dict[str, Any] | None = None,
    today_override: date | None = None,
    original_user_request: str | None = None,
) -> dict[str, Any]:
    invocation = TaskReadInvocation(
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=tuple(staged_node_ids),
        working_set=tuple(working_set or []),
        session_memory=dict(session_memory or {}),
        local_date=today_override or date.today(),
        original_user_request=original_user_request,
    )
    return NATIVE_TASK_READ_SERVICE.execute(
        name,
        arguments,
        invocation=invocation,
    )
