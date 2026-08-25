"""Fast host-owned in-process client for the authoritative Drasil MCP server."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.services.drasil_mcp import call_drasil_mcp_tool

from .mcp_invocation import McpExecutionDependencies, McpInvocationContext


def invoke_drasil_mcp_tool(
    session: Session,
    *,
    invocation: McpInvocationContext,
    name: str,
    arguments: dict[str, Any],
    execution: McpExecutionDependencies | None = None,
) -> dict[str, Any]:
    """Invoke the same MCP contract as stdio without a JSON transport round trip."""

    response = call_drasil_mcp_tool(
        session,
        invocation=invocation,
        name=name,
        arguments=arguments,
        execution=execution,
    )
    structured = response.get("structuredContent")
    if not isinstance(structured, dict):
        raise RuntimeError("Drasil MCP returned no structured tool result.")
    return structured
