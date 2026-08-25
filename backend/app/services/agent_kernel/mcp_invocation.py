"""Server-injected invocation identity for Drasil MCP boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class McpInvocationContext:
    """Trusted request context that is never accepted from model tool arguments."""

    principal_id: str
    local_date: date
    timezone: str
    allowed_tool_names: frozenset[str]
    agent_id: str = "strategist"
    application_user_id: str | None = None
    allowed_resource_prefixes: tuple[str, ...] = ("drasil://",)
    chat_session_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    active_goal_id: str | None = None
    active_goal_revision: int | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    active_category_id: int | None = None
    focus_node_id: int | None = None
    selected_task_ids: tuple[int, ...] = ()
    scope_window_start: datetime | None = None
    scope_window_end: datetime | None = None

    def require_tool(self, name: str) -> None:
        from .mcp_policy import allowed_mcp_tool_names

        if name not in allowed_mcp_tool_names(self.agent_id):
            raise PermissionError(
                f"Drasil MCP agent {self.agent_id!r} is not authorized for tool {name!r}."
            )
        if name not in self.allowed_tool_names:
            raise PermissionError(
                f"Drasil MCP principal {self.principal_id!r} is not authorized for tool {name!r}."
            )

    def require_resource(self, uri: str) -> None:
        from .mcp_policy import resource_allowed_for_agent

        if not resource_allowed_for_agent(self.agent_id, uri):
            raise PermissionError(
                f"Drasil MCP agent {self.agent_id!r} is not authorized for resource {uri!r}."
            )
        if not any(uri.startswith(prefix) for prefix in self.allowed_resource_prefixes):
            raise PermissionError(
                f"Drasil MCP principal {self.principal_id!r} is not authorized for resource {uri!r}."
            )


@dataclass(frozen=True)
class McpExecutionDependencies:
    """Trusted in-process services; never serialized into MCP arguments."""

    strategist_session_id: str | None = None
    pending_proposal: dict[str, Any] | None = None
    recent_events: list[dict[str, Any]] | None = None
    session_memory: dict[str, Any] | None = None
    tool_result_store: dict[str, Any] | None = None
    original_user_request: str | None = None
    working_set: list[dict[str, Any]] | None = None
    calendar_service: Any = None
    scheduling_service: Any = None
    calendar_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    planner_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    planner_context_reader: Callable[[], dict[str, Any]] | None = None
    planner_plan_evaluator: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    planner_decision_responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    goal_state_updater: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    evidence_resolver: Callable[[str], dict[str, Any]] | None = None
    change_set_authorer: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    change_set_simulator: Callable[[dict[str, Any]], dict[str, Any]] | None = None


_RESERVED_AUTHORITY_KEYS = frozenset(
    {
        "principalid",
        "agentid",
        "userid",
        "applicationuserid",
        "sessionid",
        "chatsessionid",
        "turnid",
        "runid",
        "goalid",
        "activegoalid",
        "activegoalrevision",
        "expectedrevision",
        "commandid",
        "correlationid",
        "causationid",
        "approvalid",
        "allowedtoolnames",
        "allowedresourceprefixes",
        "activecategoryid",
        "focusnodeid",
        "selectedtaskids",
        "scopewindowstart",
        "scopewindowend",
        "rolloutpolicy",
    }
)


def reject_model_authored_authority(arguments: object, *, path: str = "arguments") -> None:
    """Reject attempts to smuggle host identity or authorization through input."""

    if isinstance(arguments, dict):
        for key, value in arguments.items():
            normalized = "".join(
                character
                for character in str(key).lower()
                if character.isalnum()
            )
            if normalized in _RESERVED_AUTHORITY_KEYS:
                raise PermissionError(
                    f"Drasil MCP authority field {path}.{key} is host-supplied."
                )
            reject_model_authored_authority(value, path=f"{path}.{key}")
    elif isinstance(arguments, list):
        for index, value in enumerate(arguments):
            reject_model_authored_authority(value, path=f"{path}[{index}]")


def local_development_mcp_context(
    *,
    allowed_tool_names: Iterable[str],
    principal_id: str = "local-development-user",
    agent_id: str = "strategist",
) -> McpInvocationContext:
    """Build the explicit local-only context used by the inactive stdio adapter."""
    now = datetime.now().astimezone()
    return McpInvocationContext(
        principal_id=principal_id,
        local_date=now.date(),
        timezone=str(now.tzinfo or "local"),
        allowed_tool_names=frozenset(allowed_tool_names),
        agent_id=agent_id,
        application_user_id="local",
    )
