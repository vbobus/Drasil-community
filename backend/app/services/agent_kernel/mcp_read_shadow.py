"""Host-owned local MCP client and non-authoritative read comparison."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from time import perf_counter
from typing import Any, Mapping, Protocol
from uuid import uuid4

from app.mcp_server import handle_mcp_json_rpc
from app.services.agent_kernel.mcp_invocation import McpInvocationContext


MCP_READ_SHADOW_ENABLED_ENV = "DRASIL_MCP_READ_SHADOW_ENABLED"
DRASIL_BUILD_ID_ENV = "DRASIL_BUILD_ID"
LEGACY_BUILD_ID_ENV = "AGENT_KERNEL_V2_BUILD_ID"
_TRUTHY = {"1", "true", "yes", "on"}

MCP_READ_SHADOW_TOOL_NAMES = frozenset(
    {
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
        "get_workload_overview",
        "list_tasks_by_filter",
        "analyze_tree_cleanup_candidates",
        "list_calendar_blocks",
        "get_task_schedule",
    }
)


def mcp_read_shadow_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environment is None else environment
    return (
        str(source.get(MCP_READ_SHADOW_ENABLED_ENV) or "")
        .strip()
        .lower()
        in _TRUTHY
    )


def mcp_read_build_id(
    environment: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environment is None else environment
    value = str(
        source.get(DRASIL_BUILD_ID_ENV)
        or source.get(LEGACY_BUILD_ID_ENV)
        or ""
    ).strip()
    if not value or len(value) > 160:
        return "missing_build_identity"
    return value


class LocalMcpClientError(RuntimeError):
    """The local MCP transport or server contract failed."""


class DrasilCoreMcpClient(Protocol):
    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invocation: McpInvocationContext,
    ) -> dict[str, Any]: ...


class LocalDrasilCoreMcpClient:
    """Call drasil-core through its JSON-RPC boundary in the host process."""

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invocation: McpInvocationContext,
    ) -> dict[str, Any]:
        response = handle_mcp_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": f"mcp-shadow-{uuid4()}",
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": dict(arguments),
                },
            },
            invocation=invocation,
        )
        if not isinstance(response, dict):
            raise LocalMcpClientError("drasil-core returned no JSON-RPC response")
        error = response.get("error")
        if isinstance(error, dict):
            raise LocalMcpClientError(
                str(error.get("message") or "drasil-core JSON-RPC error")
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise LocalMcpClientError(
                "drasil-core returned an invalid tools/call result"
            )
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise LocalMcpClientError(
                "drasil-core returned no structuredContent"
            )
        return structured


LOCAL_DRASIL_CORE_MCP_CLIENT: DrasilCoreMcpClient = (
    LocalDrasilCoreMcpClient()
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _diff_paths(
    primary: Any,
    candidate: Any,
    *,
    path: str = "$",
    limit: int = 20,
) -> list[str]:
    differences: list[str] = []

    def visit(left: Any, right: Any, current: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append(current)
            return
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right), key=str):
                child = f"{current}.{key}"
                if key not in left or key not in right:
                    differences.append(child)
                else:
                    visit(left[key], right[key], child)
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append(f"{current}.length")
            for index, (left_item, right_item) in enumerate(
                zip(left, right)
            ):
                visit(left_item, right_item, f"{current}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if left != right:
            differences.append(current)

    visit(primary, candidate, path)
    return differences


@dataclass(frozen=True)
class McpReadShadowComparison:
    build_id: str
    tool_name: str
    attempted: bool
    status: str
    matched: bool | None
    duration_ms: int
    primary_digest: str | None = None
    candidate_digest: str | None = None
    difference_paths: tuple[str, ...] = ()
    error_type: str | None = None

    def event_payload(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "tool_name": self.tool_name,
            "attempted": self.attempted,
            "status": self.status,
            "matched": self.matched,
            "duration_ms": self.duration_ms,
            "primary_digest": self.primary_digest,
            "candidate_digest": self.candidate_digest,
            "difference_paths": list(self.difference_paths),
            "error_type": self.error_type,
            "authoritative_result": "native",
            "candidate_result_used": False,
        }


def compare_native_read_with_local_mcp(
    *,
    name: str,
    arguments: dict[str, Any],
    primary_result: dict[str, Any],
    invocation: McpInvocationContext,
    client: DrasilCoreMcpClient = LOCAL_DRASIL_CORE_MCP_CLIENT,
    enabled: bool | None = None,
    build_id: str | None = None,
) -> McpReadShadowComparison:
    effective_build_id = build_id or mcp_read_build_id()
    if enabled is None:
        enabled = mcp_read_shadow_enabled()
    if not enabled:
        return McpReadShadowComparison(
            build_id=effective_build_id,
            tool_name=name,
            attempted=False,
            status="disabled",
            matched=None,
            duration_ms=0,
        )
    if name not in MCP_READ_SHADOW_TOOL_NAMES:
        return McpReadShadowComparison(
            build_id=effective_build_id,
            tool_name=name,
            attempted=False,
            status="ineligible",
            matched=None,
            duration_ms=0,
        )

    started = perf_counter()
    primary_digest = _digest(primary_result)
    try:
        candidate = client.call_tool(
            name,
            arguments,
            invocation=invocation,
        )
    except Exception as error:
        return McpReadShadowComparison(
            build_id=effective_build_id,
            tool_name=name,
            attempted=True,
            status="candidate_failure",
            matched=False,
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
            primary_digest=primary_digest,
            error_type=type(error).__name__,
        )

    candidate_digest = _digest(candidate)
    if candidate.get("error"):
        return McpReadShadowComparison(
            build_id=effective_build_id,
            tool_name=name,
            attempted=True,
            status="candidate_error",
            matched=False,
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
            primary_digest=primary_digest,
            candidate_digest=candidate_digest,
            difference_paths=tuple(
                _diff_paths(primary_result, candidate)
            ),
        )
    differences = tuple(_diff_paths(primary_result, candidate))
    return McpReadShadowComparison(
        build_id=effective_build_id,
        tool_name=name,
        attempted=True,
        status="matched" if not differences else "mismatched",
        matched=not differences,
        duration_ms=max(0, int((perf_counter() - started) * 1000)),
        primary_digest=primary_digest,
        candidate_digest=candidate_digest,
        difference_paths=differences,
    )
