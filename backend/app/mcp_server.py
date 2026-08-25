"""Minimal stdio MCP-compatible server for the Drasil Core contract.

This is a deliberately small adapter around ``app.services.drasil_mcp``. It
keeps the first MCP step dependency-free, while leaving room to swap in FastMCP
or another SDK once the contract proves useful.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from sqlmodel import Session

from app.db.database import create_db_and_tables, get_active_engine
from app.services.account_auth import find_account_by_username, list_accounts, use_account
from app.services.agent_kernel.mcp_invocation import local_development_mcp_context
from app.services.agent_kernel.mcp_invocation import McpInvocationContext
from app.services.agent_kernel.mcp_policy import allowed_mcp_tool_names
from app.services.drasil_mcp import (
    DRASIL_MCP_SERVER_INFO,
    call_drasil_mcp_tool,
    get_drasil_mcp_prompt,
    list_drasil_mcp_prompts,
    list_drasil_mcp_resource_templates,
    list_drasil_mcp_tools,
    read_drasil_mcp_resource,
)
JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-06-18"


def _local_invocation_context():
    return local_development_mcp_context(
        agent_id="strategist",
        allowed_tool_names=allowed_mcp_tool_names("strategist"),
    )


def _success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": result,
    }


def _error(
    request_id: Any,
    *,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        payload["error"]["data"] = data
    return payload


def handle_mcp_json_rpc(
    message: dict[str, Any],
    *,
    invocation: McpInvocationContext | None = None,
) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if not isinstance(method, str):
        return _error(request_id, code=-32600, message="Invalid JSON-RPC request.")
    if request_id is None and method.startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            protocol_version = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
            return _success(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                        "prompts": {},
                    },
                    "serverInfo": DRASIL_MCP_SERVER_INFO,
                },
            )
        if method == "tools/list":
            if invocation is None:
                raise PermissionError(
                    "tools/list requires a trusted MCP invocation context."
                )
            return _success(
                request_id,
                list_drasil_mcp_tools(invocation=invocation),
            )
        if method == "tools/call":
            if invocation is None:
                raise PermissionError(
                    "tools/call requires a trusted MCP invocation context."
                )
            name = params.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("tools/call requires a tool name.")
            raw_arguments = params.get("arguments", {})
            if not isinstance(raw_arguments, dict):
                raise ValueError("tools/call arguments must be an object.")
            arguments = raw_arguments
            with Session(get_active_engine()) as session:
                result = call_drasil_mcp_tool(
                    session,
                    invocation=invocation,
                    name=name,
                    arguments=arguments,
                )
            return _success(request_id, result)
        if method == "resources/templates/list":
            return _success(request_id, list_drasil_mcp_resource_templates())
        if method == "resources/read":
            if invocation is None:
                raise PermissionError(
                    "resources/read requires a trusted MCP invocation context."
                )
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                raise ValueError("resources/read requires a URI.")
            with Session(get_active_engine()) as session:
                result = read_drasil_mcp_resource(
                    session,
                    invocation=invocation,
                    uri=uri,
                )
            return _success(request_id, result)
        if method == "prompts/list":
            return _success(request_id, list_drasil_mcp_prompts())
        if method == "prompts/get":
            name = params.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("prompts/get requires a prompt name.")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return _success(request_id, get_drasil_mcp_prompt(name, arguments))
    except ValueError as error:
        return _error(request_id, code=-32602, message=str(error))
    except PermissionError as error:
        return _error(request_id, code=-32001, message=str(error))
    except Exception as error:  # pragma: no cover - defensive server boundary.
        return _error(
            request_id,
            code=-32603,
            message="Internal Drasil MCP server error.",
            data={"detail": str(error)},
        )
    return _error(request_id, code=-32601, message=f"Unknown MCP method: {method}")


def main() -> None:
    configured_username = os.getenv("DRASIL_MCP_ACCOUNT", "").strip()
    accounts = list_accounts()
    if configured_username:
        account = find_account_by_username(configured_username)
        if account is None:
            raise RuntimeError(f"DRASIL_MCP_ACCOUNT does not name an active account: {configured_username}")
    elif len(accounts) == 1:
        account = accounts[0]
    elif not accounts:
        raise RuntimeError("Initialize a Drasil account before starting the MCP server.")
    else:
        raise RuntimeError(
            "Set DRASIL_MCP_ACCOUNT to choose which account this local MCP process may access."
        )

    with use_account(account):
        create_db_and_tables()
        local_invocation = _local_invocation_context()
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                response = _error(None, code=-32700, message="Parse error.")
            else:
                response = (
                    handle_mcp_json_rpc(message, invocation=local_invocation)
                    if isinstance(message, dict)
                    else _error(None, code=-32600, message="Invalid JSON-RPC request.")
                )
            if response is not None:
                sys.stdout.write(
                    json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
                )
                sys.stdout.flush()


if __name__ == "__main__":
    main()
