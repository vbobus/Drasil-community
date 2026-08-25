"""Streaming Codex app-server harness for structured Strategist model calls.

The old Codex path used ``codex exec --output-schema`` with ``subprocess.run``:
one opaque child process, no incremental progress, and a wall-clock timeout that
looked like an agent failure even while the provider was still retrying.

This harness uses ``codex app-server --listen stdio://`` instead. The protocol
is line-delimited JSON-RPC. We start an ephemeral thread, run one schema-bound
turn, and watch the server notification stream for meaningful progress before
declaring the turn wedged.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Callable

from app.services.agent_kernel.core_contract import render_core_operating_instructions

from app.core.codex_cli import require_codex_cli_path


class CodexAppServerUnavailableError(RuntimeError):
    """Raised when the app-server transport cannot be started or negotiated."""

    def __init__(self, message: str, *, trace: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.trace = trace or {}


class CodexAppServerIdleTimeoutError(RuntimeError):
    """Raised when a turn stops emitting meaningful progress."""

    def __init__(self, message: str, *, trace: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.trace = trace or {}


class CodexAppServerMalformedStructuredOutputError(RuntimeError):
    """Raised when app-server completed but no emitted text parsed as schema JSON."""

    def __init__(
        self,
        message: str,
        *,
        trace: dict[str, Any],
        parse_error: str,
    ) -> None:
        super().__init__(message)
        self.trace = trace
        self.parse_error = parse_error


@dataclass
class CodexAppServerTrace:
    transport: str = "stdio"
    thread_id: str | None = None
    turn_id: str | None = None
    event_count: int = 0
    progress_event_count: int = 0
    server_request_count: int = 0
    stderr_preview: list[str] = field(default_factory=list)
    notification_methods: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    idle_timeout_seconds: float | None = None
    runtime_timeout_seconds: float | None = None
    final_text_chars: int = 0
    final_text_source: str | None = None
    final_text_preview: str = ""
    parse_error: str | None = None
    status: str = "unknown"
    dynamic_tool_call_count: int = 0
    charged_tool_call_count: int = 0
    dynamic_tool_repair_count: int = 0
    dynamic_tool_error_count: int = 0
    dynamic_tool_names: list[str] = field(default_factory=list)
    dynamic_tool_cache_hits: int = 0
    tool_budget_exhausted: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "event_count": self.event_count,
            "progress_event_count": self.progress_event_count,
            "server_request_count": self.server_request_count,
            "stderr_preview": self.stderr_preview[-8:],
            "notification_methods": self.notification_methods[-30:],
            "duration_ms": self.duration_ms,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "runtime_timeout_seconds": self.runtime_timeout_seconds,
            "final_text_chars": self.final_text_chars,
            "final_text_source": self.final_text_source,
            "final_text_preview": self.final_text_preview,
            "parse_error": self.parse_error,
            "status": self.status,
            "dynamic_tool_call_count": self.dynamic_tool_call_count,
            "charged_tool_call_count": self.charged_tool_call_count,
            "dynamic_tool_repair_count": self.dynamic_tool_repair_count,
            "dynamic_tool_error_count": self.dynamic_tool_error_count,
            "dynamic_tool_names": self.dynamic_tool_names[-20:],
            "dynamic_tool_cache_hits": self.dynamic_tool_cache_hits,
            "tool_budget_exhausted": self.tool_budget_exhausted,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def _bounded_float_env(name: str, *, default: float, minimum: float, maximum: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def codex_app_server_enabled() -> bool:
    raw_value = os.getenv("STRATEGIST_CODEX_APP_SERVER_ENABLED", "1").strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def codex_app_server_idle_timeout_seconds() -> float:
    return _bounded_float_env(
        "STRATEGIST_CODEX_APP_SERVER_IDLE_TIMEOUT_SECONDS",
        default=300.0,
        minimum=20.0,
        maximum=3600.0,
    )


def codex_app_server_control_timeout_seconds() -> float:
    return _bounded_float_env(
        "STRATEGIST_CODEX_APP_SERVER_CONTROL_TIMEOUT_SECONDS",
        default=45.0,
        minimum=5.0,
        maximum=300.0,
    )


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 1)


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n"


def _compact_preview(text: str, *, limit: int = 500) -> str:
    preview = " ".join(text.strip().split())
    if len(preview) <= limit:
        return preview
    return f"{preview[:limit].rstrip()}..."


def _iter_balanced_json_object_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    start_index: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character == "{":
            if depth == 0:
                start_index = index
            depth += 1
            continue
        if character == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start_index is not None:
                candidates.append(text[start_index : index + 1])
                start_index = None
    return candidates


def _parse_json_object_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty structured output")
    candidates = [stripped, *_iter_balanced_json_object_candidates(stripped)]
    last_error: Exception | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = ValueError("structured output was not a JSON object")
    if last_error is not None:
        raise last_error
    raise ValueError("structured output did not contain a JSON object")


def _append_text_candidate(
    candidates: list[tuple[str, str]],
    *,
    source: str,
    text: str,
) -> None:
    stripped = text.strip()
    if not stripped:
        return
    if any(existing_text == stripped for _, existing_text in candidates):
        return
    candidates.append((source, stripped))


def _structured_output_candidates(
    *,
    completed_text_parts: list[str],
    delta_text_parts: list[str],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    completed_joined = "".join(completed_text_parts)
    delta_joined = "".join(delta_text_parts)

    # Completed items are usually best, but app-server can emit partial
    # completed fragments while the full message only exists in deltas.
    _append_text_candidate(candidates, source="completed_joined", text=completed_joined)
    _append_text_candidate(candidates, source="delta_joined", text=delta_joined)

    for index, text in enumerate(reversed(completed_text_parts), start=1):
        _append_text_candidate(
            candidates,
            source=f"completed_part_-{index}",
            text=text,
        )
    for index, text in enumerate(reversed(delta_text_parts), start=1):
        _append_text_candidate(
            candidates,
            source=f"delta_part_-{index}",
            text=text,
        )
    return candidates


def _parse_first_structured_output_candidate(
    candidates: list[tuple[str, str]],
) -> tuple[dict[str, Any], str, str]:
    last_error = "no structured text candidates"
    for source, text in candidates:
        try:
            return _parse_json_object_text(text), source, text
        except Exception as error:
            last_error = f"{source}: {error}"
    raise ValueError(last_error)


def _thread_id_from_response(response: dict[str, Any]) -> str | None:
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return None
    thread_id = thread.get("id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _turn_id_from_response(response: dict[str, Any]) -> str | None:
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    turn = result.get("turn")
    if not isinstance(turn, dict):
        return None
    turn_id = turn.get("id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _extract_output_text_from_raw_response_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for content_item in content:
        if not isinstance(content_item, dict):
            continue
        if content_item.get("type") not in {"output_text", "text"}:
            continue
        text = content_item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _extract_agent_message_text(item: Any) -> str:
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return ""
    text = item.get("text")
    return text if isinstance(text, str) else ""


def _error_detail_from_notification(params: Any) -> str:
    if not isinstance(params, dict):
        return "Codex app-server emitted an error notification."
    error = params.get("error")
    if not isinstance(error, dict):
        return "Codex app-server emitted an error notification."
    message = error.get("message")
    details = error.get("additionalDetails")
    pieces = [piece for piece in (message, details) if isinstance(piece, str) and piece.strip()]
    return " ".join(pieces) or "Codex app-server emitted an error notification."


class CodexAppServerJsonRpcClient:
    def __init__(
        self,
        *,
        codex_path: str,
        cwd: Path,
        configured_model: str | None,
    ) -> None:
        self.codex_path = codex_path
        self.cwd = cwd
        self.configured_model = configured_model
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self._pending_messages: deque[tuple[str, dict[str, Any] | str]] = deque()
        self._next_request_id = 1

    def __enter__(self) -> "CodexAppServerJsonRpcClient":
        command = [
            self.codex_path,
            "app-server",
            "--listen",
            "stdio://",
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=account_process_environment(),
            errors="replace",
            cwd=str(self.cwd),
        )
        self._start_reader("stdout", self._process.stdout)
        self._start_reader("stderr", self._process.stderr)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self._process = None

    def _start_reader(self, name: str, stream: Any) -> None:
        def reader() -> None:
            if stream is None:
                return
            for line in stream:
                self._messages.put((name, line.rstrip("\n")))

        threading.Thread(target=reader, daemon=True).start()

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerUnavailableError("Codex app-server is not running.")
        process.stdin.write(_json_line(payload))
        process.stdin.flush()

    def request(self, method: str, params: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = monotonic() + timeout_seconds
        stderr_preview: list[str] = []
        while monotonic() < deadline:
            try:
                stream_name, line = self._messages.get(timeout=0.25)
            except queue.Empty:
                self._raise_if_exited(stderr_preview)
                continue
            if stream_name == "stderr":
                if line.strip():
                    stderr_preview.append(line)
                    self._pending_messages.append((stream_name, line))
                continue
            message = self._decode_stdout_line(line)
            if message.get("id") != request_id:
                # Notifications can race ahead of the response to initialize,
                # thread/start, or turn/start. Preserve them for the turn loop
                # instead of silently dropping provider progress/output events.
                self._pending_messages.append((stream_name, message))
                continue
            if "error" in message:
                raise CodexAppServerUnavailableError(str(message["error"]))
            return message
        raise CodexAppServerUnavailableError(
            f"Codex app-server request {method} did not respond within {timeout_seconds:.0f}s."
        )

    def read_message(self, *, timeout_seconds: float) -> tuple[str, dict[str, Any] | str] | None:
        if self._pending_messages:
            return self._pending_messages.popleft()
        try:
            stream_name, line = self._messages.get(timeout=timeout_seconds)
        except queue.Empty:
            self._raise_if_exited([])
            return None
        if stream_name == "stderr":
            return stream_name, line
        return stream_name, self._decode_stdout_line(line)

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def _decode_stdout_line(self, line: str) -> dict[str, Any]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise CodexAppServerUnavailableError(
                f"Codex app-server emitted invalid JSON-RPC: {line[:200]}"
            ) from error
        if not isinstance(parsed, dict):
            raise CodexAppServerUnavailableError("Codex app-server emitted a non-object message.")
        return parsed

    def _raise_if_exited(self, stderr_preview: list[str]) -> None:
        process = self._process
        if process is not None and process.poll() is not None:
            stderr = "\n".join(stderr_preview[-5:])
            raise CodexAppServerUnavailableError(
                f"Codex app-server exited with code {process.returncode}. {stderr}".strip()
            )


def _build_thread_start_params(
    *,
    repo_root: Path,
    configured_model: str | None,
    dynamic_tools: list[dict[str, Any]] | None = None,
    base_instructions: str | None = None,
    developer_instructions: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(repo_root),
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "ephemeral": True,
        "experimentalRawEvents": True,
        "baseInstructions": (
            base_instructions
            or (
                "You are being used as a schema-bound model step inside Drasil's "
                "native agent-kernel workflow. Return only JSON matching the requested "
                "output schema. Do not use repository-editing tools."
            )
        ),
        "developerInstructions": (
            developer_instructions
            or (
                "Honor the JSON output schema exactly. For intermediate workflow "
                "stages, return compact JSON in the schema envelope. For final stages, "
                "return the complete Strategist response object inside the requested "
                "schema envelope."
            )
        ),
    }
    if dynamic_tools:
        params["dynamicTools"] = dynamic_tools
    if configured_model:
        params["model"] = configured_model
    service_tier = os.getenv("STRATEGIST_CODEX_APP_SERVER_SERVICE_TIER")
    if service_tier:
        params["serviceTier"] = service_tier
    return params


def _dynamic_tool_spec_from_function_schema(
    definition: dict[str, Any],
    *,
    defer_loading: bool,
    namespace: str = "drasil",
) -> dict[str, Any] | None:
    name = str(definition.get("name") or "").strip()
    if not name:
        return None
    spec: dict[str, Any] = {
        "name": name,
        "description": str(definition.get("description") or ""),
        "inputSchema": definition.get("parameters") or {
            "type": "object",
            "properties": {},
        },
    }
    if defer_loading:
        spec["namespace"] = namespace
        spec["deferLoading"] = True
    return spec


DIRECT_DYNAMIC_TOOL_NAMES = {
    "query_task_graph",
    "submit_task_metadata_plan",
    "submit_task_tree_plan",
    "submit_preference_plan",
    "submit_preference_management_plan",
    "submit_preference_cleanup_plan",
}
COMPACT_SEMANTIC_ACK_TOOL_NAMES = {
    "submit_task_metadata_plan",
    "submit_task_tree_plan",
    "submit_preference_plan",
    "submit_preference_management_plan",
    "submit_preference_cleanup_plan",
    "handoff_to_planner",
    "respond_to_planner_decision",
    "draft_change_set",
    "simulate_change_set",
}
TERMINAL_SEMANTIC_PLAN_TOOL_NAMES: frozenset[str] = frozenset()


def _terminal_proposal_payload_from_tool_result(
    *,
    tool_name: str,
    result: dict[str, Any],
    terminal_semantic_plan_tool_names: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    terminal_response = result.get("terminal_response")
    if isinstance(terminal_response, dict):
        return terminal_response
    if (
        tool_name in terminal_semantic_plan_tool_names
        and result.get("compiled") is True
        and isinstance(result.get("semantic_plan_input"), dict)
    ):
        return {
            "decision": {
                "kind": "mutation",
                "reason": "The requested semantic plan compiled successfully.",
            },
            "summary": "I prepared the requested change for review.",
            "semantic_plan_input": None,
            "action_proposal": None,
            "metadata_proposal": None,
            "branch_proposal": None,
            "preference_memory_proposal": None,
            "clarification": None,
        }
    # High-level semantic inputs are accumulated across the complete turn and
    # compiled together by the host. They are deliberately non-terminal.
    return None


def _build_dynamic_tool_specs(tool_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for definition in tool_definitions:
        name = str(definition.get("name") or "").strip()
        spec = _dynamic_tool_spec_from_function_schema(
            definition,
            defer_loading=bool(name and name not in DIRECT_DYNAMIC_TOOL_NAMES),
        )
        if spec is not None:
            specs.append(spec)
    return specs


def _read_dynamic_tool_call(params: Any) -> dict[str, Any] | None:
    if not isinstance(params, dict):
        return None
    tool_name = params.get("tool")
    if not isinstance(tool_name, str) or not tool_name.strip():
        tool_name = params.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            parsed_arguments = {}
        arguments = parsed_arguments
    if not isinstance(arguments, dict):
        arguments = {}
    call_id = params.get("callId") or params.get("toolCallId") or params.get("id")
    return {
        "thread_id": str(params.get("threadId") or ""),
        "turn_id": str(params.get("turnId") or ""),
        "call_id": str(call_id or f"tool-call-{abs(hash(json.dumps(arguments, default=str))) % 1000000}"),
        "tool_name": tool_name.strip(),
        "arguments": arguments,
    }


def _compact_task_for_dynamic_tool_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    task = value.get("task") if isinstance(value.get("task"), dict) else value
    if not isinstance(task, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "id",
        "task_id",
        "title",
        "status",
        "priority",
        "priority_label",
        "deadline",
        "deadline_at",
        "estimate",
        "time_estimate_minutes",
        "estimate_minutes",
        "parent_task_id",
        "path",
        "child_count",
        "child_sample",
        "is_backlog",
        "tags",
        "category_id",
        "category",
        "category_name",
    ):
        if task.get(key) not in (None, "", [], {}):
            compact[key] = task.get(key)
    return compact or None


def _compact_dynamic_tool_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact: dict[str, Any] = {}
    for key in (
        "result_kind",
        "read_only",
        "compiled",
        "proposal_kind",
        "query",
        "scope",
        "interpreted_intent",
        "answer_ready",
        "answer_candidate",
        "answer_contract",
        "evidence_coverage",
        "observation_summary",
        "inferred_filters",
        "count",
        "truncated",
        "tool_result_handle",
        "duplicate_tool_call",
        "model_instruction",
        "planner_mode",
        "planner_executed",
        "proposal_ready",
        "needs_user_input",
        "summary",
        "task_count",
        "active_count",
        "backlog_count",
        "category_count",
        "cleanup_candidate_count",
        "messy_branch_count",
        "organization_map_count",
        "organization_map_offset",
        "organization_map_complete",
        "next_organization_offset",
        "due_soon_count",
        "overdue_count",
        "high_priority_count",
        "stale_count",
    ):
        if value.get(key) not in (None, "", [], {}):
            compact[key] = value.get(key)
    for object_key in ("tool_error", "planner_error", "clarification"):
        if isinstance(value.get(object_key), dict):
            compact[object_key] = value[object_key]
    for list_key, limit in (
        ("tasks", 12),
        ("children", 10),
        ("siblings", 10),
        ("parents", 8),
        ("matches", 6),
        ("entity_matches", 6),
        ("due_soon", 12),
        ("overdue", 12),
        ("high_priority", 12),
        ("stale", 8),
        ("cleanup_candidates", 12),
        ("messy_branches", 12),
        ("organization_map", 100),
    ):
        items = value.get(list_key)
        if not isinstance(items, list):
            continue
        compact_items = [
            task
            for task in (_compact_task_for_dynamic_tool_result(item) for item in items[:limit])
            if task is not None
        ]
        if compact_items:
            compact[list_key] = compact_items
    if isinstance(value.get("destination_catalog"), list):
        compact["destination_catalog"] = value["destination_catalog"][:20]
    snapshots = value.get("task_snapshots")
    if isinstance(snapshots, list):
        compact_snapshots: list[dict[str, Any]] = []
        for snapshot in snapshots[:6]:
            if not isinstance(snapshot, dict):
                continue
            task = _compact_task_for_dynamic_tool_result(snapshot.get("task"))
            if task is not None:
                compact_snapshots.append({"task": task})
        if compact_snapshots:
            compact["task_snapshots"] = compact_snapshots
    branch = _compact_task_for_dynamic_tool_result(value.get("branch"))
    if branch is not None:
        raw_branch = value.get("branch")
        raw_children = raw_branch.get("children") if isinstance(raw_branch, dict) else None
        if isinstance(raw_children, list):
            branch["children"] = [
                task
                for task in (
                    _compact_task_for_dynamic_tool_result(child)
                    for child in raw_children[:12]
                )
                if task is not None
            ]
        compact["branch"] = branch
    if "tool_result" in value:
        compact["tool_result"] = _compact_dynamic_tool_result(value.get("tool_result"))
    suggestions = value.get("suggested_next_tools")
    if isinstance(suggestions, list) and suggestions:
        compact["suggested_next_tools"] = suggestions[:4]
    return compact or value


def _model_facing_dynamic_tool_result(*, tool_name: str, value: Any) -> Any:
    """Return the smallest acknowledgement that preserves the next decision.

    Successful semantic submissions are already stored in full by the host and
    their arguments are present in the model's immediately preceding tool call.
    Echoing the compiled plan back into the transcript makes every subsequent
    model step pay for the same large payload again.  Validation failures stay
    unabridged because their field-level details are needed for repair.
    """

    if (
        tool_name in COMPACT_SEMANTIC_ACK_TOOL_NAMES
        and isinstance(value, dict)
        and not value.get("error")
        and value.get("compiled") is not False
    ):
        return _compact_dynamic_tool_result(value)
    return value


def _json_for_tool_result(value: Any, *, max_chars: int = 16000) -> str:
    serialized = json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return serialized
    compact_value = _compact_dynamic_tool_result(value)
    compact_serialized = json.dumps(compact_value, ensure_ascii=True, default=str, separators=(",", ":"))
    if len(compact_serialized) <= max_chars:
        return compact_serialized
    return json.dumps(
        {
            "truncated": True,
            "original_chars": len(serialized),
            "compact_preview": compact_serialized[: max(0, max_chars - 220)].rstrip(),
            "hint": (
                "The tool result was truncated for the model context. If a "
                "tool_result_handle is present, call get_tool_result_by_handle for the full result."
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _build_turn_start_params(
    *,
    thread_id: str,
    prompt: str,
    schema: dict[str, Any],
    configured_model: str | None,
    reasoning_effort: str | None = None,
    use_output_schema: bool = True,
    reasoning_summary: str | None = "auto",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
    }
    if reasoning_summary:
        params["summary"] = reasoning_summary
    if use_output_schema:
        params["outputSchema"] = schema
    if configured_model:
        params["model"] = configured_model
    selected_reasoning_effort = (
        reasoning_effort
        if reasoning_effort is not None
        else os.getenv("STRATEGIST_CODEX_APP_SERVER_REASONING_EFFORT", "")
    ).strip()
    if selected_reasoning_effort:
        params["effort"] = selected_reasoning_effort
    return params


def _is_meaningful_progress(method: str, params: Any, *, turn_id: str | None) -> bool:
    if method in {
        "agentMessage/delta",
        "item/agentMessage/delta",
        "reasoningSummaryText/delta",
        "item/reasoning/summaryTextDelta",
        "reasoningText/delta",
        "item/reasoning/textDelta",
        "rawResponseItem/completed",
        "item/started",
        "item/completed",
        "thread/status/changed",
        "turn/started",
        "turn/plan/updated",
        "turn/diff/updated",
    }:
        if not isinstance(params, dict) or turn_id is None:
            return True
        return params.get("turnId") in {turn_id, None}
    if method in {"warning", "error"}:
        return True
    return False


def _build_interrupt_params(thread_id: str, turn_id: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"threadId": thread_id}
    if turn_id:
        params["turnId"] = turn_id
    return params


def invoke_codex_app_server_json_schema(
    *,
    prompt: str,
    schema: dict[str, Any],
    configured_model: str | None,
    timeout_seconds: int,
    reasoning_effort: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one schema-bound Codex turn through app-server streaming."""

    codex_path = require_codex_cli_path("Strategist")
    repo_root = Path(__file__).resolve().parents[4]
    started_at = perf_counter()
    runtime_timeout = max(1.0, float(timeout_seconds))
    idle_timeout = min(codex_app_server_idle_timeout_seconds(), runtime_timeout)
    control_timeout = codex_app_server_control_timeout_seconds()
    trace = CodexAppServerTrace(
        idle_timeout_seconds=idle_timeout,
        runtime_timeout_seconds=runtime_timeout,
    )
    delta_text_parts: list[str] = []
    completed_text_parts: list[str] = []
    last_error_detail: str | None = None

    with CodexAppServerJsonRpcClient(
        codex_path=codex_path,
        cwd=repo_root,
        configured_model=configured_model,
    ) as client:
        client.request(
            "initialize",
            {
                "clientInfo": {"name": "drasil-strategist", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            timeout_seconds=control_timeout,
        )
        client.notify("initialized")
        thread_response = client.request(
            "thread/start",
            _build_thread_start_params(
                repo_root=repo_root,
                configured_model=configured_model,
            ),
            timeout_seconds=control_timeout,
        )
        thread_id = _thread_id_from_response(thread_response)
        if not thread_id:
            raise CodexAppServerUnavailableError("Codex app-server did not return a thread id.")
        trace.thread_id = thread_id
        turn_response = client.request(
            "turn/start",
            _build_turn_start_params(
                thread_id=thread_id,
                prompt=prompt,
                schema=schema,
                configured_model=configured_model,
                reasoning_effort=reasoning_effort,
            ),
            timeout_seconds=control_timeout,
        )
        turn_id = _turn_id_from_response(turn_response)
        trace.turn_id = turn_id

        deadline = monotonic() + runtime_timeout
        idle_deadline = monotonic() + idle_timeout
        while monotonic() < deadline:
            remaining_idle = idle_deadline - monotonic()
            remaining_runtime = deadline - monotonic()
            if remaining_idle <= 0 or remaining_runtime <= 0:
                try:
                    client.request(
                        "turn/interrupt",
                        _build_interrupt_params(thread_id, turn_id),
                        timeout_seconds=min(5.0, control_timeout),
                    )
                except Exception:
                    pass
                trace.status = "idle_timeout"
                trace.duration_ms = _elapsed_ms(started_at)
                text_candidates = _structured_output_candidates(
                    completed_text_parts=completed_text_parts,
                    delta_text_parts=delta_text_parts,
                )
                if text_candidates:
                    trace.final_text_source = text_candidates[0][0]
                    trace.final_text_chars = len(text_candidates[0][1])
                    trace.final_text_preview = _compact_preview(text_candidates[0][1])
                raise CodexAppServerIdleTimeoutError(
                    "Codex app-server turn stopped emitting meaningful progress before completion.",
                    trace=trace.as_dict(),
                )
            read_timeout = max(0.1, min(1.0, remaining_idle, remaining_runtime))

            event = client.read_message(timeout_seconds=read_timeout)
            if event is None:
                continue
            stream_name, payload = event
            if stream_name == "stderr":
                line = str(payload).strip()
                if line:
                    trace.stderr_preview.append(line)
                    trace.stderr_preview = trace.stderr_preview[-8:]
                continue
            if not isinstance(payload, dict):
                continue

            trace.event_count += 1
            if "id" in payload and "method" in payload:
                trace.server_request_count += 1
                method = str(payload.get("method") or "")
                if method == "item/tool/call":
                    client.respond(
                        payload["id"],
                        {
                            "success": False,
                            "contentItems": [
                                {
                                    "type": "inputText",
                                    "text": (
                                        "This schema-only model step does not expose "
                                        "dynamic Drasil tools."
                                    ),
                                }
                            ],
                        },
                    )
                else:
                    client.respond(payload["id"], {})
                continue

            method = str(payload.get("method") or "")
            params = payload.get("params")
            if method:
                trace.notification_methods.append(method)
                trace.notification_methods = trace.notification_methods[-30:]
            if _is_meaningful_progress(method, params, turn_id=turn_id):
                trace.progress_event_count += 1
                idle_deadline = monotonic() + idle_timeout

            if method in {"agentMessage/delta", "item/agentMessage/delta"} and isinstance(params, dict):
                delta = params.get("delta")
                if isinstance(delta, str):
                    delta_text_parts.append(delta)
                continue
            if method == "rawResponseItem/completed" and isinstance(params, dict):
                text = _extract_output_text_from_raw_response_item(params.get("item"))
                if text:
                    completed_text_parts.append(text)
                continue
            if method == "item/completed" and isinstance(params, dict):
                text = _extract_agent_message_text(params.get("item"))
                if text:
                    completed_text_parts.append(text)
                continue
            if method == "error":
                last_error_detail = _error_detail_from_notification(params)
                if isinstance(params, dict) and params.get("willRetry") is True:
                    continue
            if method == "turn/completed" and isinstance(params, dict):
                turn = params.get("turn")
                status = str(turn.get("status") if isinstance(turn, dict) else "")
                trace.status = status or "completed"
                break
        else:
            trace.status = "runtime_timeout"
            trace.duration_ms = _elapsed_ms(started_at)
            text_candidates = _structured_output_candidates(
                completed_text_parts=completed_text_parts,
                delta_text_parts=delta_text_parts,
            )
            if text_candidates:
                trace.final_text_source = text_candidates[0][0]
                trace.final_text_chars = len(text_candidates[0][1])
                trace.final_text_preview = _compact_preview(text_candidates[0][1])
            raise CodexAppServerIdleTimeoutError(
                "Codex app-server turn exceeded its runtime budget.",
                trace=trace.as_dict(),
            )

    trace.duration_ms = _elapsed_ms(started_at)
    text_candidates = _structured_output_candidates(
        completed_text_parts=completed_text_parts,
        delta_text_parts=delta_text_parts,
    )
    if text_candidates:
        trace.final_text_source = text_candidates[0][0]
        trace.final_text_chars = len(text_candidates[0][1])
        trace.final_text_preview = _compact_preview(text_candidates[0][1])
    if trace.status == "failed" and not text_candidates:
        raise CodexAppServerUnavailableError(
            last_error_detail or "Codex app-server turn failed before producing output.",
            trace=trace.as_dict(),
        )
    try:
        parsed, source, final_text = _parse_first_structured_output_candidate(text_candidates)
        stripped_final_text = final_text.lstrip()
        if not (
            stripped_final_text.startswith("{")
            or stripped_final_text.startswith("```json")
            or stripped_final_text.startswith("```")
        ):
            raise ValueError(
                "Native final output contained non-JSON text before the response object. "
                "Tool calls must use the dynamic tool protocol, not textual emulation."
            )
        trace.final_text_source = source
        trace.final_text_chars = len(final_text)
        trace.final_text_preview = _compact_preview(final_text)
        return parsed, trace.as_dict()
    except (ValueError, json.JSONDecodeError) as error:
        trace.status = "malformed_output"
        trace.parse_error = str(error)
        raise CodexAppServerMalformedStructuredOutputError(
            "Codex app-server completed but did not emit parseable structured JSON.",
            trace=trace.as_dict(),
            parse_error=str(error),
        ) from error


def invoke_codex_app_server_native_tool_loop(
    *,
    prompt: str,
    schema: dict[str, Any],
    configured_model: str | None,
    timeout_seconds: int,
    tool_definitions: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
    tool_trace: list[dict[str, Any]] | None = None,
    reasoning_effort: str | None = None,
    max_tool_calls: int | None = None,
    agent_label: str = "Strategist",
    client_name: str = "drasil-strategist",
    base_instructions: str | None = None,
    developer_instructions: str | None = None,
    allow_terminal_proposal_tools: bool = True,
    terminal_semantic_plan_tool_names: frozenset[str] = frozenset(),
    reasoning_summary: str | None = "auto",
    cancellation_check: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one Codex app-server turn with Drasil tools attached natively.

    This mirrors the production app-server harness shape used by OpenClaw:
    the model receives a dynamic tool surface, semantically chooses tools, and
    emits the final structured Strategist response in the same turn. Drasil
    owns tool execution and approval boundaries; Codex owns semantic tool use.
    """

    codex_path = require_codex_cli_path(agent_label)
    repo_root = Path(__file__).resolve().parents[4]
    started_at = perf_counter()
    runtime_timeout = max(1.0, float(timeout_seconds))
    idle_timeout = min(codex_app_server_idle_timeout_seconds(), runtime_timeout)
    control_timeout = codex_app_server_control_timeout_seconds()
    trace = CodexAppServerTrace(
        idle_timeout_seconds=idle_timeout,
        runtime_timeout_seconds=runtime_timeout,
    )
    delta_text_parts: list[str] = []
    completed_text_parts: list[str] = []
    last_error_detail: str | None = None
    dynamic_tools = _build_dynamic_tool_specs(tool_definitions)
    tool_call_budget = max(1, int(max_tool_calls or 8))
    # ``tool_call_budget`` is a budget for successful, useful work. Provider
    # argument repairs and exact cached reads are not semantic progress and
    # must not consume the capacity reserved for the final authoring call.
    # Keep a separate bounded attempt ceiling so repeated invalid calls still
    # terminate safely.
    tool_attempt_budget = tool_call_budget + max(4, tool_call_budget // 2)
    read_result_cache: dict[str, dict[str, Any]] = {}

    with CodexAppServerJsonRpcClient(
        codex_path=codex_path,
        cwd=repo_root,
        configured_model=configured_model,
    ) as client:
        client.request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            timeout_seconds=control_timeout,
        )
        client.notify("initialized")
        thread_response = client.request(
            "thread/start",
            _build_thread_start_params(
                repo_root=repo_root,
                configured_model=configured_model,
                dynamic_tools=dynamic_tools,
                base_instructions=base_instructions or render_core_operating_instructions("strategist"),
                developer_instructions=developer_instructions or (
                    "Use dynamic tools when the shared operating contract requires evidence. "
                    "Do not emulate tool calls in JSON. When enough evidence is available, "
                    "produce one valid Strategist response object."
                ),
            ),
            timeout_seconds=control_timeout,
        )
        thread_id = _thread_id_from_response(thread_response)
        if not thread_id:
            raise CodexAppServerUnavailableError("Codex app-server did not return a thread id.")
        trace.thread_id = thread_id
        turn_response = client.request(
            "turn/start",
            _build_turn_start_params(
                thread_id=thread_id,
                prompt=prompt,
                schema=schema,
                configured_model=configured_model,
                reasoning_effort=reasoning_effort,
                use_output_schema=False,
                reasoning_summary=reasoning_summary,
            ),
            timeout_seconds=control_timeout,
        )
        turn_id = _turn_id_from_response(turn_response)
        trace.turn_id = turn_id

        deadline = monotonic() + runtime_timeout
        idle_deadline = monotonic() + idle_timeout
        while monotonic() < deadline:
            if cancellation_check is not None and cancellation_check():
                try:
                    client.request(
                        "turn/interrupt",
                        _build_interrupt_params(thread_id, turn_id),
                        timeout_seconds=min(5.0, control_timeout),
                    )
                except Exception:
                    pass
                trace.status = "cancelled"
                trace.duration_ms = _elapsed_ms(started_at)
                raise CodexAppServerUnavailableError(
                    "Codex app-server native tool turn was cancelled by its host."
                )
            remaining_idle = idle_deadline - monotonic()
            remaining_runtime = deadline - monotonic()
            if remaining_idle <= 0 or remaining_runtime <= 0:
                try:
                    client.request(
                        "turn/interrupt",
                        _build_interrupt_params(thread_id, turn_id),
                        timeout_seconds=min(5.0, control_timeout),
                    )
                except Exception:
                    pass
                trace.status = "idle_timeout"
                trace.duration_ms = _elapsed_ms(started_at)
                text_candidates = _structured_output_candidates(
                    completed_text_parts=completed_text_parts,
                    delta_text_parts=delta_text_parts,
                )
                if text_candidates:
                    trace.final_text_source = text_candidates[0][0]
                    trace.final_text_chars = len(text_candidates[0][1])
                    trace.final_text_preview = _compact_preview(text_candidates[0][1])
                raise CodexAppServerIdleTimeoutError(
                    "Codex app-server native tool turn stopped emitting meaningful progress before completion.",
                    trace=trace.as_dict(),
                )
            read_timeout = max(0.1, min(1.0, remaining_idle, remaining_runtime))

            event = client.read_message(timeout_seconds=read_timeout)
            if event is None:
                continue
            stream_name, payload = event
            if stream_name == "stderr":
                line = str(payload).strip()
                if line:
                    trace.stderr_preview.append(line)
                    trace.stderr_preview = trace.stderr_preview[-8:]
                continue
            if not isinstance(payload, dict):
                continue
            raw_usage = payload.get("usage")
            if not isinstance(raw_usage, dict):
                params = payload.get("params")
                raw_usage = params.get("usage") if isinstance(params, dict) else None
            if isinstance(raw_usage, dict):
                input_tokens = raw_usage.get(
                    "input_tokens",
                    raw_usage.get("inputTokens"),
                )
                output_tokens = raw_usage.get(
                    "output_tokens",
                    raw_usage.get("outputTokens"),
                )
                if isinstance(input_tokens, int) and input_tokens >= 0:
                    trace.input_tokens = input_tokens
                if isinstance(output_tokens, int) and output_tokens >= 0:
                    trace.output_tokens = output_tokens

            trace.event_count += 1
            if "id" in payload and "method" in payload:
                trace.server_request_count += 1
                method = str(payload.get("method") or "")
                if method == "item/tool/call":
                    call = _read_dynamic_tool_call(payload.get("params"))
                    if call is None:
                        trace.dynamic_tool_error_count += 1
                        client.respond(
                            payload["id"],
                            {
                                "success": False,
                                "contentItems": [
                                    {
                                        "type": "inputText",
                                        "text": "Malformed Drasil dynamic tool call.",
                                    }
                                ],
                            },
                        )
                        continue
                    tool_name = str(call["tool_name"])
                    arguments = call["arguments"] if isinstance(call["arguments"], dict) else {}
                    trace.dynamic_tool_call_count += 1
                    trace.dynamic_tool_names.append(tool_name)
                    trace.dynamic_tool_names = trace.dynamic_tool_names[-20:]
                    if trace.dynamic_tool_call_count > tool_attempt_budget:
                        trace.tool_budget_exhausted = True
                        trace.status = "tool_budget_exhausted"
                        trace.duration_ms = _elapsed_ms(started_at)
                        try:
                            client.request(
                                "turn/interrupt",
                                _build_interrupt_params(thread_id, turn_id),
                                timeout_seconds=min(5.0, control_timeout),
                            )
                        except Exception:
                            pass
                        raise CodexAppServerIdleTimeoutError(
                            (
                                "Codex app-server native tool turn exceeded its bounded "
                                "tool-attempt budget before producing a final response."
                            ),
                            trace=trace.as_dict(),
                        )
                    cache_key = json.dumps(
                        {"name": tool_name, "arguments": arguments},
                        sort_keys=True,
                        ensure_ascii=True,
                        default=str,
                        separators=(",", ":"),
                    )
                    cached_result = read_result_cache.get(cache_key)
                    if cached_result is not None:
                        trace.dynamic_tool_cache_hits += 1
                        replay_result = dict(cached_result)
                        replay_result["duplicate_tool_call"] = True
                        replay_result["model_instruction"] = (
                            "This exact read result is already available in this turn. "
                            "Do not call it again; use the evidence, choose a different "
                            "tool only if necessary, or produce the final response/proposal."
                        )
                        client.respond(
                            payload["id"],
                            {
                                "success": True,
                                "contentItems": [
                                    {
                                        "type": "inputText",
                                        "text": _json_for_tool_result(replay_result),
                                    }
                                ],
                            },
                        )
                        continue
                    if trace.charged_tool_call_count >= tool_call_budget:
                        trace.tool_budget_exhausted = True
                        trace.status = "tool_budget_exhausted"
                        trace.duration_ms = _elapsed_ms(started_at)
                        try:
                            client.request(
                                "turn/interrupt",
                                _build_interrupt_params(thread_id, turn_id),
                                timeout_seconds=min(5.0, control_timeout),
                            )
                        except Exception:
                            pass
                        raise CodexAppServerIdleTimeoutError(
                            (
                                "Codex app-server native tool turn exhausted its "
                                "successful tool-call budget before producing a final response."
                            ),
                            trace=trace.as_dict(),
                        )
                    tool_started_at = perf_counter()
                    try:
                        result = tool_executor(tool_name, arguments)
                        if result.get("error") or result.get("compiled") is False:
                            trace.dynamic_tool_repair_count += 1
                        else:
                            trace.charged_tool_call_count += 1
                        duration_ms = _elapsed_ms(tool_started_at)
                        if tool_trace is not None:
                            tool_trace.append(
                                {
                                    "name": tool_name,
                                    "arguments": arguments,
                                    "result": result,
                                    "duration_ms": duration_ms,
                                }
                            )
                        client.respond(
                            payload["id"],
                            {
                                "success": True,
                                "contentItems": [
                                    {
                                        "type": "inputText",
                                        "text": _json_for_tool_result(
                                            _model_facing_dynamic_tool_result(
                                                tool_name=tool_name,
                                                value=result,
                                            )
                                        ),
                                    }
                                ],
                            },
                        )
                        if result.get("read_only") is True and not result.get("error"):
                            read_result_cache[cache_key] = dict(result)
                        terminal_payload = (
                            _terminal_proposal_payload_from_tool_result(
                                tool_name=tool_name,
                                result=result,
                                terminal_semantic_plan_tool_names=(
                                    terminal_semantic_plan_tool_names
                                ),
                            )
                            if allow_terminal_proposal_tools
                            else None
                        )
                        if terminal_payload is not None:
                            try:
                                client.request(
                                    "turn/interrupt",
                                    _build_interrupt_params(thread_id, turn_id),
                                    # The semantic tool result is already compiled and
                                    # captured. Do not put a blocking control-plane
                                    # acknowledgement on the user-visible latency path;
                                    # exiting the client context still closes the turn.
                                    timeout_seconds=min(0.25, control_timeout),
                                )
                            except Exception:
                                pass
                            trace.status = "terminal_proposal_tool"
                            trace.duration_ms = _elapsed_ms(started_at)
                            trace.final_text_source = "terminal_proposal_tool"
                            trace.final_text_chars = len(json.dumps(terminal_payload, ensure_ascii=True))
                            trace.final_text_preview = _compact_preview(terminal_payload["summary"])
                            return terminal_payload, trace.as_dict()
                    except Exception as error:
                        trace.dynamic_tool_error_count += 1
                        duration_ms = _elapsed_ms(tool_started_at)
                        error_payload = {
                            "error": str(error),
                            "tool": tool_name,
                            "duration_ms": duration_ms,
                        }
                        if tool_trace is not None:
                            tool_trace.append(
                                {
                                    "name": tool_name,
                                    "arguments": arguments,
                                    "result": error_payload,
                                    "duration_ms": duration_ms,
                                }
                            )
                        client.respond(
                            payload["id"],
                            {
                                "success": False,
                                "contentItems": [
                                    {
                                        "type": "inputText",
                                        "text": _json_for_tool_result(error_payload),
                                    }
                                ],
                            },
                        )
                    idle_deadline = monotonic() + idle_timeout
                else:
                    client.respond(payload["id"], {})
                continue

            method = str(payload.get("method") or "")
            params = payload.get("params")
            if method:
                trace.notification_methods.append(method)
                trace.notification_methods = trace.notification_methods[-30:]
            if _is_meaningful_progress(method, params, turn_id=turn_id):
                trace.progress_event_count += 1
                idle_deadline = monotonic() + idle_timeout

            if method in {"agentMessage/delta", "item/agentMessage/delta"} and isinstance(params, dict):
                delta = params.get("delta")
                if isinstance(delta, str):
                    delta_text_parts.append(delta)
                continue
            if method == "rawResponseItem/completed" and isinstance(params, dict):
                text = _extract_output_text_from_raw_response_item(params.get("item"))
                if text:
                    completed_text_parts.append(text)
                continue
            if method == "item/completed" and isinstance(params, dict):
                text = _extract_agent_message_text(params.get("item"))
                if text:
                    completed_text_parts.append(text)
                continue
            if method == "error":
                last_error_detail = _error_detail_from_notification(params)
                if isinstance(params, dict) and params.get("willRetry") is True:
                    continue
            if method == "turn/completed" and isinstance(params, dict):
                turn = params.get("turn")
                status = str(turn.get("status") if isinstance(turn, dict) else "")
                trace.status = status or "completed"
                break
        else:
            trace.status = "runtime_timeout"
            trace.duration_ms = _elapsed_ms(started_at)
            text_candidates = _structured_output_candidates(
                completed_text_parts=completed_text_parts,
                delta_text_parts=delta_text_parts,
            )
            if text_candidates:
                trace.final_text_source = text_candidates[0][0]
                trace.final_text_chars = len(text_candidates[0][1])
                trace.final_text_preview = _compact_preview(text_candidates[0][1])
            raise CodexAppServerIdleTimeoutError(
                "Codex app-server native tool turn exceeded its runtime budget.",
                trace=trace.as_dict(),
            )

    trace.duration_ms = _elapsed_ms(started_at)
    text_candidates = _structured_output_candidates(
        completed_text_parts=completed_text_parts,
        delta_text_parts=delta_text_parts,
    )
    if text_candidates:
        trace.final_text_source = text_candidates[0][0]
        trace.final_text_chars = len(text_candidates[0][1])
        trace.final_text_preview = _compact_preview(text_candidates[0][1])
    if trace.status == "failed" and not text_candidates:
        raise CodexAppServerUnavailableError(
            last_error_detail or "Codex app-server native tool turn failed before producing output.",
            trace=trace.as_dict(),
        )
    try:
        parsed, source, final_text = _parse_first_structured_output_candidate(text_candidates)
        trace.final_text_source = source
        trace.final_text_chars = len(final_text)
        trace.final_text_preview = _compact_preview(final_text)
        return parsed, trace.as_dict()
    except (ValueError, json.JSONDecodeError) as error:
        trace.status = "malformed_output"
        trace.parse_error = str(error)
        raise CodexAppServerMalformedStructuredOutputError(
            "Codex app-server native tool turn completed but did not emit parseable structured JSON.",
            trace=trace.as_dict(),
            parse_error=str(error),
        ) from error
from app.services.account_auth import account_process_environment
