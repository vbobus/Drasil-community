"""Fail-closed agent policy for the authoritative ``drasil-core`` surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from .tool_registry import KernelToolSpec, registered_kernel_tool_specs


class McpAgentPolicyError(PermissionError):
    """Raised when an unknown agent or invalid capability widening is requested."""


TASK_TOOLS = frozenset(
    {
        "query_task_graph",
        "search_entities",
        "search_notes",
        "get_task_snapshot",
        "get_task_metadata",
        "get_task_notes",
        "get_task_branch",
        "get_parent_chain",
        "get_children",
        "get_siblings",
        "get_subtree",
        "get_related_entities",
        "get_session_working_set",
        "get_recent_session_actions",
        "get_workload_overview",
        "list_tasks_by_filter",
        "analyze_tree_cleanup_candidates",
    }
)
CALENDAR_TOOLS = frozenset({"list_calendar_blocks", "get_task_schedule"})
PREFERENCE_READ_TOOLS = frozenset({"query_preferences"})
PREFERENCE_ADMIN_READ_TOOLS = frozenset(
    {
        "list_remembered_preferences",
        "analyze_preference_cleanup_candidates",
    }
)
CONTEXT_TOOLS = frozenset({"get_tool_result_by_handle"})
CAPABILITY_TOOLS = frozenset(
    {"search_capability_catalog", "load_skill", "search_tool_catalog"}
)
SCHEDULING_TOOLS = frozenset(
    {"handoff_to_planner", "respond_to_planner_decision"}
)
PLANNER_EXECUTION_TOOLS = frozenset(
    {"read_planner_context", "evaluate_schedule_plan"}
)
GOAL_TOOLS = frozenset({"set_goal_transition"})
SEMANTIC_PLAN_TOOLS = frozenset(
    {
        "submit_task_metadata_plan",
        "submit_task_tree_plan",
        "submit_preference_plan",
        "submit_preference_management_plan",
        "submit_preference_cleanup_plan",
    }
)

INTERNAL_ONLY_TOOL_NAMES = frozenset(
    {
        "draft_change_set",
        "simulate_change_set",
        "update_goal_state",
        "get_pending_proposal",
        "get_recent_kernel_events",
        "get_session_memory",
        "get_context_surfaces",
    }
)


@dataclass(frozen=True)
class McpAgentPolicy:
    agent_id: str
    allowed_tool_names: frozenset[str]
    logical_modules: tuple[str, ...]


_SHARED_READS = (
    TASK_TOOLS
    | CALENDAR_TOOLS
    | PREFERENCE_READ_TOOLS
    | CONTEXT_TOOLS
    | CAPABILITY_TOOLS
)
MCP_AGENT_POLICIES: dict[str, McpAgentPolicy] = {
    "strategist": McpAgentPolicy(
        agent_id="strategist",
        allowed_tool_names=(
            _SHARED_READS
            | PREFERENCE_ADMIN_READ_TOOLS
            | SCHEDULING_TOOLS
            | GOAL_TOOLS
            | SEMANTIC_PLAN_TOOLS
        ),
        logical_modules=(
            "context",
            "task",
            "calendar",
            "scheduling",
            "preference",
            "goal",
            "capability",
            "semantic-plan",
        ),
    ),
    "planner": McpAgentPolicy(
        agent_id="planner",
        allowed_tool_names=_SHARED_READS | PLANNER_EXECUTION_TOOLS,
        logical_modules=(
            "context",
            "task",
            "calendar",
            "preference",
            "capability",
        ),
    ),
}


def _policy(agent_id: str) -> McpAgentPolicy:
    normalized = str(agent_id or "").strip().lower()
    policy = MCP_AGENT_POLICIES.get(normalized)
    if policy is None:
        raise McpAgentPolicyError(f"Unknown Drasil MCP agent: {normalized!r}.")
    return policy


def allowed_mcp_tool_names(agent_id: str) -> frozenset[str]:
    """Return the maximum server policy for one known agent."""

    return _policy(agent_id).allowed_tool_names


def resource_allowed_for_agent(agent_id: str, uri: str) -> bool:
    """Authorize one resource kind independently of caller-supplied prefixes."""

    policy = _policy(agent_id)
    parsed = urlparse(uri)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "drasil":
        return False
    if parsed.netloc == "capabilities" and path_parts == ["catalog"]:
        return True
    if parsed.netloc == "capability" and len(path_parts) == 2:
        return path_parts[1] == "skill"
    if parsed.netloc == "agent" and len(path_parts) == 2:
        return path_parts == [policy.agent_id, "profile"]
    if parsed.netloc == "evidence" and len(path_parts) == 1:
        return True
    if parsed.netloc == "goals" and path_parts == ["current"]:
        return policy.agent_id == "strategist"
    if parsed.netloc in {"category", "task"}:
        return True
    return False


def registered_agent_mcp_specs(agent_id: str) -> list[KernelToolSpec]:
    """Return canonical specs visible to an agent in stable registry order."""

    allowed = allowed_mcp_tool_names(agent_id)
    specs = [
        item
        for item in registered_kernel_tool_specs()
        if item.name in allowed
    ]
    missing = sorted(allowed.difference(item.name for item in specs))
    if missing:
        raise RuntimeError(
            f"MCP policy references unregistered canonical tools: {missing}"
        )
    return specs


def registered_mcp_surface_specs() -> list[KernelToolSpec]:
    """Return the union exposed by the physical server, once per canonical spec."""

    exposed = set().union(
        *(policy.allowed_tool_names for policy in MCP_AGENT_POLICIES.values())
    )
    return [
        item
        for item in registered_kernel_tool_specs()
        if item.name in exposed
    ]


def filter_mcp_specs_for_invocation(
    *,
    agent_id: str,
    invocation_allowed_tool_names: Iterable[str],
) -> list[KernelToolSpec]:
    """Intersect host-granted tools with the immutable server agent policy."""

    granted = {
        str(item).strip()
        for item in invocation_allowed_tool_names
        if str(item).strip()
    }
    policy_names = allowed_mcp_tool_names(agent_id)
    effective = granted.intersection(policy_names)
    return [
        item
        for item in registered_agent_mcp_specs(agent_id)
        if item.name in effective
    ]


def registered_agent_provider_tool_schemas(agent_id: str) -> list[dict]:
    """Project provider schemas from the same canonical specs used by MCP."""

    return [item.v2_tool_schema() for item in registered_agent_mcp_specs(agent_id)]


def logical_module_for_tool(name: str) -> str:
    if name in TASK_TOOLS:
        return "task"
    if name in CALENDAR_TOOLS:
        return "calendar"
    if name in PREFERENCE_READ_TOOLS or name in PREFERENCE_ADMIN_READ_TOOLS or name in {
        "submit_preference_plan",
        "submit_preference_management_plan",
        "submit_preference_cleanup_plan",
    }:
        return "preference"
    if name in CONTEXT_TOOLS:
        return "context"
    if name in CAPABILITY_TOOLS:
        return "capability"
    if name in SCHEDULING_TOOLS or name in PLANNER_EXECUTION_TOOLS:
        return "scheduling"
    if name in GOAL_TOOLS:
        return "goal"
    if name in SEMANTIC_PLAN_TOOLS:
        return "semantic-plan"
    return "internal"
