"""MCP-compatible Drasil tool/resource contract.

This module intentionally does not depend on an MCP SDK. It keeps the first
MCP migration slice app-native and testable while exposing shapes that map
directly to MCP tools, resources, and prompts.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, select

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.agent_profiles import get_agent_profile
from app.services.agent_kernel.capabilities import (
    compact_capability_catalog,
    load_skill,
)
from app.services.agent_kernel.compact_goal_context import (
    build_compact_goal_context,
    resolve_evidence_handle,
)
from app.services.agent_kernel.mcp_invocation import (
    McpExecutionDependencies,
    McpInvocationContext,
    reject_model_authored_authority,
)
from app.services.agent_kernel.mcp_policy import (
    filter_mcp_specs_for_invocation,
    registered_mcp_surface_specs,
)
from app.services.agent_kernel.calendar_service import SQL_CALENDAR_READ_SERVICE
from app.services.agent_kernel.state_store import GoalStateStore
from app.services.agent_kernel.task_read_service import execute_native_task_read
from app.services.agent_kernel.tool_registry import (
    execute_kernel_tool,
)
from app.services.preference_memory import (
    PreferenceRetrievalScope,
    SQL_PREFERENCE_APPLICATION_SERVICE,
)
from app.services.preference_admin import (
    analyze_remembered_preference_cleanup,
    list_remembered_preference_inventory,
)
from app.services.task_read_handlers import (
    build_tree_scope_index,
)

DRASIL_MCP_SERVER_INFO = {
    "name": "drasil-core",
    "version": "0.3.0",
    "description": (
        "Authoritative high-level Drasil capability boundary with fail-closed agent toolsets."
    ),
}

DRASIL_MCP_SURFACE = "drasil_mcp"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":"))


def _mcp_content_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": _json_text(value),
            }
        ],
        "structuredContent": value,
    }


def _load_world(
    session: Session,
    *,
    active_category_id: int | None = None,
    focus_node_id: int | None = None,
) -> tuple[list[Category], list[StrategyNode], Category | None, StrategyNode | None]:
    categories = list(session.exec(select(Category)).all())
    nodes = list(session.exec(select(StrategyNode)).all())
    active_category = (
        next((category for category in categories if category.id == active_category_id), None)
        if active_category_id is not None
        else None
    )
    if active_category is None:
        active_category = next(
            (category for category in categories if category.name.lower() == "work"),
            None,
        )
    if active_category is None and categories:
        active_category = sorted(categories, key=lambda item: (item.sort_order, item.name.lower()))[0]
    focus_node = (
        next((node for node in nodes if node.id == focus_node_id), None)
        if focus_node_id is not None
        else None
    )
    return categories, nodes, active_category, focus_node


def _category_by_id(categories: list[Category], category_id: int) -> Category | None:
    return next((category for category in categories if category.id == category_id), None)


def list_drasil_mcp_tools(
    *,
    invocation: McpInvocationContext | None = None,
) -> dict[str, Any]:
    if invocation is None:
        return {"tools": []}
    return {
        "tools": [
            spec.mcp_tool_schema()
            for spec in filter_mcp_specs_for_invocation(
                agent_id=invocation.agent_id,
                invocation_allowed_tool_names=invocation.allowed_tool_names,
            )
        ]
    }


def _parse_local_datetime(
    value: object,
    *,
    field_name: str,
    timezone_name: str,
) -> datetime:
    """Normalize provider ISO values into the app's local-wall-time contract.

    Models legitimately emit either a naive local value or an ISO value with
    an offset.  The application services intentionally operate on naive local
    wall times, so the trusted invocation timezone is the authoritative bridge
    between those two transport representations.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO local date-time.")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO local date-time.") from error
    if parsed.tzinfo is not None:
        if timezone_name.strip().casefold() in {"", "local"}:
            # A local-development invocation may not have an IANA name. The
            # provider's explicit offset still makes its wall-clock fields
            # unambiguous, so preserve them rather than rejecting the value.
            return parsed.replace(tzinfo=None)
        try:
            local_timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("The trusted account timezone is invalid.") from error
        parsed = parsed.astimezone(local_timezone).replace(tzinfo=None)
    return parsed


def _query_preferences(
    session: Session,
    *,
    invocation: McpInvocationContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not invocation.application_user_id:
        raise PermissionError(
            "Preference retrieval requires trusted application-user authority."
        )
    window_start = _parse_local_datetime(
        arguments.get("window_start"),
        field_name="window_start",
        timezone_name=invocation.timezone,
    )
    window_end = _parse_local_datetime(
        arguments.get("window_end"),
        field_name="window_end",
        timezone_name=invocation.timezone,
    )
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start.")
    return SQL_PREFERENCE_APPLICATION_SERVICE.query_context(
        session,
        scope=PreferenceRetrievalScope(
            task_ids=list(arguments.get("task_ids") or []),
            category_ids=list(arguments.get("category_ids") or []),
            task_titles=list(arguments.get("task_titles") or []),
            category_names=list(arguments.get("category_names") or []),
            window_start=window_start,
            window_end=window_end,
            prompt=str(arguments.get("query") or ""),
            user_id=invocation.application_user_id,
        ),
        max_soft_preferences=int(arguments.get("max_soft_preferences", 8)),
        max_observations=int(arguments.get("max_observations", 5)),
    )


def _list_remembered_preferences(
    session: Session,
    *,
    invocation: McpInvocationContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not invocation.application_user_id:
        raise PermissionError(
            "Preference administration requires trusted application-user authority."
        )
    return list_remembered_preference_inventory(
        session,
        user_id=invocation.application_user_id,
        include_inactive=bool(arguments.get("include_inactive", True)),
        query=str(arguments.get("query") or ""),
        limit=int(arguments.get("limit", 50)),
    )


def _analyze_preference_cleanup_candidates(
    session: Session,
    *,
    invocation: McpInvocationContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not invocation.application_user_id:
        raise PermissionError(
            "Preference cleanup requires trusted application-user authority."
        )
    return analyze_remembered_preference_cleanup(
        session,
        user_id=invocation.application_user_id,
        cleanup_scope=str(arguments.get("cleanup_scope") or "all"),
    )


def _planner_scoped_arguments(
    invocation: McpInvocationContext,
    *,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Enforce the host-delegated entity and time window for owned Planner calls."""

    scoped = dict(arguments)
    range_required = not (
        name == "list_calendar_blocks"
        and scoped.get("include_all_for_tasks") is True
        and isinstance(scoped.get("task_ids"), list)
        and bool(scoped["task_ids"])
    )
    if name in {"list_calendar_blocks", "query_preferences"} and range_required:
        start_field = "start_at" if name == "list_calendar_blocks" else "window_start"
        end_field = "end_at" if name == "list_calendar_blocks" else "window_end"
        start = _parse_local_datetime(
            scoped.get(start_field),
            field_name=start_field,
            timezone_name=invocation.timezone,
        )
        end = _parse_local_datetime(
            scoped.get(end_field),
            field_name=end_field,
            timezone_name=invocation.timezone,
        )
        # Calendar and preference application services intentionally accept
        # local wall time, irrespective of which agent made the MCP call.
        scoped[start_field] = start.isoformat()
        scoped[end_field] = end.isoformat()

    if invocation.agent_id != "planner":
        return scoped
    allowed_task_ids = set(invocation.selected_task_ids)
    task_scoped_tools = {
        "get_task_snapshot",
        "get_task_metadata",
        "get_task_schedule",
        "query_preferences",
        "evaluate_schedule_plan",
    }
    if name in task_scoped_tools and not allowed_task_ids:
        raise PermissionError("Owned Planner invocation has no delegated task scope.")

    explicit_ids: set[int] = set()
    raw_task_id = scoped.get("task_id")
    if isinstance(raw_task_id, int) and not isinstance(raw_task_id, bool):
        explicit_ids.add(raw_task_id)
    raw_task_ids = scoped.get("task_ids")
    if isinstance(raw_task_ids, list):
        explicit_ids.update(
            item
            for item in raw_task_ids
            if isinstance(item, int) and not isinstance(item, bool)
        )
    directives = scoped.get("task_directives")
    if isinstance(directives, list):
        explicit_ids.update(
            item["task_id"]
            for item in directives
            if isinstance(item, dict)
            and isinstance(item.get("task_id"), int)
            and not isinstance(item.get("task_id"), bool)
        )
    outside = sorted(explicit_ids.difference(allowed_task_ids))
    if outside:
        raise PermissionError(
            f"Planner tool request exceeds delegated task scope: {outside}"
        )
    if (
        name in {"get_task_snapshot", "get_task_metadata"}
        and not explicit_ids
        and scoped.get("task_title")
    ):
        raise PermissionError(
            "Owned Planner task reads must use a host-delegated task ID."
        )

    if name in {"list_calendar_blocks", "query_preferences"} and range_required:
        start_field = "start_at" if name == "list_calendar_blocks" else "window_start"
        end_field = "end_at" if name == "list_calendar_blocks" else "window_end"
        start = _parse_local_datetime(
            scoped.get("start_at", scoped.get("window_start")),
            field_name=start_field,
            timezone_name=invocation.timezone,
        )
        end = _parse_local_datetime(
            scoped.get("end_at", scoped.get("window_end")),
            field_name=end_field,
            timezone_name=invocation.timezone,
        )
        # Downstream application services intentionally accept only local wall
        # time.  Validation alone is insufficient: forward the canonicalized
        # values so an offset-bearing provider argument cannot be rejected a
        # second time by the local-time boundary.
        scoped[start_field] = start.isoformat()
        scoped[end_field] = end.isoformat()
        if (
            invocation.scope_window_start is not None
            and start < invocation.scope_window_start
        ) or (
            invocation.scope_window_end is not None
            and end > invocation.scope_window_end
        ):
            raise PermissionError(
                "Planner tool request exceeds the delegated scheduling window."
            )
    return scoped


def call_drasil_mcp_tool(
    session: Session,
    *,
    invocation: McpInvocationContext,
    name: str,
    arguments: dict[str, Any] | None = None,
    execution: McpExecutionDependencies | None = None,
) -> dict[str, Any]:
    execution = execution or McpExecutionDependencies()
    surface_specs = {
        spec.name: spec
        for spec in registered_mcp_surface_specs()
    }
    if name not in surface_specs:
        raise ValueError(f"Unknown or unavailable Drasil MCP tool: {name}")
    invocation.require_tool(name)
    reject_model_authored_authority(arguments or {})
    arguments = _planner_scoped_arguments(
        invocation,
        name=name,
        arguments=dict(arguments or {}),
    )
    categories, nodes, active_category, focus_node = _load_world(
        session,
        active_category_id=invocation.active_category_id,
        focus_node_id=invocation.focus_node_id,
    )
    result = execute_kernel_tool(
        name,
        arguments,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=list(invocation.selected_task_ids),
        working_set=list(execution.working_set or []),
        today_override=invocation.local_date,
        strategist_session_id=(
            execution.strategist_session_id or invocation.chat_session_id
        ),
        pending_proposal=execution.pending_proposal,
        recent_events=execution.recent_events,
        session_memory=execution.session_memory,
        tool_result_store=execution.tool_result_store,
        original_user_request=execution.original_user_request,
        calendar_service=execution.calendar_service or SQL_CALENDAR_READ_SERVICE,
        scheduling_service=execution.scheduling_service,
        calendar_reader=execution.calendar_reader,
        planner_executor=execution.planner_executor,
        planner_context_reader=execution.planner_context_reader,
        planner_plan_evaluator=execution.planner_plan_evaluator,
        planner_decision_responder=execution.planner_decision_responder,
        goal_state_updater=execution.goal_state_updater,
        evidence_resolver=(
            execution.evidence_resolver
            or (
                lambda handle: resolve_evidence_handle(
                    session,
                    session_id=str(invocation.chat_session_id),
                    goal_id=str(invocation.active_goal_id),
                    handle=handle,
                )
                if invocation.chat_session_id and invocation.active_goal_id
                else None
            )
        ),
        change_set_authorer=execution.change_set_authorer,
        change_set_simulator=execution.change_set_simulator,
        preference_reader=lambda preference_arguments: _query_preferences(
            session,
            invocation=invocation,
            arguments=preference_arguments,
        ),
        preference_inventory_reader=lambda preference_arguments: (
            _list_remembered_preferences(
                session,
                invocation=invocation,
                arguments=preference_arguments,
            )
        ),
        preference_cleanup_analyzer=lambda cleanup_arguments: (
            _analyze_preference_cleanup_candidates(
                session,
                invocation=invocation,
                arguments=cleanup_arguments,
            )
        ),
    )
    response = _mcp_content_result(result)
    if result.get("error"):
        response["isError"] = True
    return response


def list_drasil_mcp_resource_templates() -> dict[str, Any]:
    return {
        "resourceTemplates": [
            {
                "uriTemplate": "drasil://agent/{agent_id}/profile",
                "name": "runtime-agent-profile",
                "title": "Drasil Runtime Agent Profile",
                "description": "Versioned identity, duties, boundaries, and available capabilities for a Drasil agent.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://capability/{capability_id}/skill",
                "name": "runtime-skill",
                "title": "Drasil Runtime Skill",
                "description": "Versioned model-readable guidance from Drasil's capability catalog.",
                "mimeType": "text/markdown",
            },
            {
                "uriTemplate": "drasil://capabilities/catalog",
                "name": "capability-catalog",
                "title": "Drasil Capability Catalog",
                "description": "Compact catalog of model-selectable Drasil runtime capabilities.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://category/{category_id}/scope-index",
                "name": "category-scope-index",
                "title": "Category Scope Index",
                "description": "Small category-level routing index for progressive task-tree retrieval.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://category/{category_id}/cleanup-candidates",
                "name": "category-cleanup-candidates",
                "title": "Category Cleanup Candidates",
                "description": "Ranked cleanup and messy-branch signals for a category.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://task/{task_id}/branch",
                "name": "task-branch",
                "title": "Task Branch",
                "description": "Nested task branch snapshot. Optional query parameter: depth.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://task/{task_id}/snapshot",
                "name": "task-snapshot",
                "title": "Task Snapshot",
                "description": "Single task metadata, dependency, and child summary.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://goals/current",
                "name": "current-goal",
                "title": "Current Goal Context",
                "description": "Bounded current GoalState projection for the authenticated session.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "drasil://evidence/{handle}",
                "name": "durable-evidence",
                "title": "Durable Goal Evidence",
                "description": "Bounded tool observation authorized to the current session and goal.",
                "mimeType": "application/json",
            },
        ]
    }


def _parse_positive_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {field_name}: {value!r}") from error
    if parsed <= 0:
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return parsed


def read_drasil_mcp_resource(
    session: Session,
    *,
    invocation: McpInvocationContext,
    uri: str,
) -> dict[str, Any]:
    invocation.require_resource(uri)
    parsed = urlparse(uri)
    if parsed.scheme != "drasil":
        raise ValueError(f"Unsupported Drasil MCP resource URI: {uri}")
    path_parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if parsed.netloc == "capabilities" and path_parts == ["catalog"]:
        payload = compact_capability_catalog()
        return {
            "contents": [{"uri": uri, "mimeType": "application/json", "text": _json_text(payload)}],
            "structuredContent": payload,
        }
    if parsed.netloc == "agent" and len(path_parts) == 2 and path_parts[1] == "profile":
        payload = get_agent_profile(path_parts[0], include_instructions=True)
        return {
            "contents": [{"uri": uri, "mimeType": "application/json", "text": _json_text(payload)}],
            "structuredContent": payload,
        }
    if parsed.netloc == "capability" and len(path_parts) == 2 and path_parts[1] == "skill":
        payload = load_skill({"capability_id": path_parts[0]})
        if payload.get("error"):
            raise ValueError(str(payload["error"]))
        return {
            "contents": [{"uri": uri, "mimeType": "text/markdown", "text": str(payload["skill"])}],
            "structuredContent": payload,
        }
    if parsed.netloc == "goals" and path_parts == ["current"]:
        if not invocation.chat_session_id or not invocation.active_goal_id:
            raise PermissionError("Current-goal resource requires session and goal identity.")
        goal = GoalStateStore(session).load(
            invocation.chat_session_id,
            invocation.active_goal_id,
        )
        if (
            invocation.active_goal_revision is not None
            and goal.revision != invocation.active_goal_revision
        ):
            raise PermissionError("Current-goal resource revision is stale.")
        payload = build_compact_goal_context(
            goal_state=goal,
            latest_user_turn="",
            runtime_context={},
            pending_proposal=None,
            recent_events=[],
        )
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": _json_text(payload),
                }
            ],
            "structuredContent": payload,
        }
    if parsed.netloc == "evidence" and len(path_parts) == 1:
        if not invocation.chat_session_id or not invocation.active_goal_id:
            raise PermissionError("Evidence resource requires session and goal identity.")
        handle = unquote(path_parts[0])
        payload = resolve_evidence_handle(
            session,
            session_id=invocation.chat_session_id,
            goal_id=invocation.active_goal_id,
            handle=handle,
        )
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": _json_text(payload),
                }
            ],
            "structuredContent": payload,
        }

    categories, nodes, active_category, focus_node = _load_world(session)

    if parsed.netloc == "category" and len(path_parts) == 2:
        category_id = _parse_positive_int(path_parts[0], field_name="category_id")
        category = _category_by_id(categories, category_id)
        if category is None:
            raise ValueError(f"Unknown category_id: {category_id}")
        if path_parts[1] == "scope-index":
            payload = build_tree_scope_index(
                categories=categories,
                nodes=nodes,
                active_category_id=category_id,
            )
        elif path_parts[1] == "cleanup-candidates":
            payload = execute_native_task_read(
                "analyze_tree_cleanup_candidates",
                {"category_name": category.name, "limit": 12},
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=category,
                staged_node_ids=[],
                working_set=[],
                today_override=invocation.local_date,
            )
        else:
            raise ValueError(f"Unsupported category resource: {uri}")
    elif parsed.netloc == "task" and len(path_parts) == 2:
        task_id = _parse_positive_int(path_parts[0], field_name="task_id")
        if path_parts[1] == "branch":
            raw_depth = query.get("depth", ["2"])[0]
            depth = max(1, min(_parse_positive_int(raw_depth, field_name="depth"), 4))
            payload = execute_native_task_read(
                "get_task_branch",
                {"task_id": task_id, "task_title": None, "depth": depth},
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                staged_node_ids=[],
                working_set=[],
                today_override=invocation.local_date,
            )
        elif path_parts[1] == "snapshot":
            payload = execute_native_task_read(
                "get_task_snapshot",
                {"task_id": task_id, "task_title": None},
                categories=categories,
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                staged_node_ids=[],
                working_set=[],
                today_override=invocation.local_date,
            )
        else:
            raise ValueError(f"Unsupported task resource: {uri}")
    else:
        raise ValueError(f"Unsupported Drasil MCP resource URI: {uri}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": _json_text(payload),
            }
        ],
        "structuredContent": payload,
    }


def list_drasil_mcp_prompts() -> dict[str, Any]:
    return {
        "prompts": [
            {
                "name": "strategist-cleanup-review",
                "description": "Review a category for redundant tasks or messy branches before proposing cleanup.",
                "arguments": [
                    {
                        "name": "category_name",
                        "description": "Category/workspace to inspect, such as Work.",
                        "required": False,
                    }
                ],
            },
            {
                "name": "strategist-create-task-branch",
                "description": "Create a reviewable task branch while preserving user-provided details.",
                "arguments": [
                    {
                        "name": "request",
                        "description": "The user's task creation request.",
                        "required": True,
                    }
                ],
            },
        ]
    }


def get_drasil_mcp_prompt(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    if name == "strategist-cleanup-review":
        category_name = str(arguments.get("category_name") or "the active category").strip()
        text = (
            f"Review {category_name} for cleanup. First call analyze_tree_cleanup_candidates. "
            "For a full cleanup, retrieve every organization-map page and use the destination catalog. "
            "Preserve coherent subtrees, then return one validated approval-gated batch of moves and "
            "archives. Hard deletion requires strong evidence. Clarify only when destructive scope or "
            "a destination remains materially ambiguous."
        )
    elif name == "strategist-create-task-branch":
        request = str(arguments.get("request") or "").strip()
        text = (
            "Create a reviewable top-level task branch from the request. Preserve concrete details "
            "such as dates, people, blockers, and prerequisites in notes, deadlines, or child tasks. "
            f"Request: {request}"
        )
    else:
        raise ValueError(f"Unknown Drasil MCP prompt: {name}")
    return {
        "description": name,
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": text,
                },
            }
        ],
    }
