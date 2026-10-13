"""Configurable Langfuse tracing for the Strategist agent kernel.

Aggregate mode exports operational facts only. Full mode additionally exports
agent prompts/responses and tool inputs/outputs. API credentials and raw exception
messages are never passed to the tracing SDK in either mode.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import Parameter, signature
from threading import Lock
from typing import Any, Callable, Iterator, Mapping, TypeVar, cast


logger = logging.getLogger("drasil.agent_observability")

_F = TypeVar("_F", bound=Callable[..., Any])
_CLIENT_UNSET = object()
_client_instance: Any = _CLIENT_UNSET
_client_lock = Lock()
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", "none"}
_SUPPORTED_MODES = {"auto", "langfuse"}
_FULL_CONTENT_VALUES = {"1", "true", "yes", "on", "full"}
_ACTIVE_DRASIL_TRACE: ContextVar[bool] = ContextVar(
    "drasil_agent_observability_trace_active",
    default=False,
)


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        return None
    return normalized


def _hash_private_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return hashlib.sha256(f"drasil-observability:{value}".encode("utf-8")).hexdigest()[:32]


def _is_drasil_langfuse_span(span: Any) -> bool:
    """Reject third-party OTEL spans that could contain captured model content."""

    scope = getattr(span, "instrumentation_scope", None)
    return getattr(scope, "name", None) == "langfuse-sdk"


def _content_mode(environment: Mapping[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    configured = (
        source.get(
            "DRASIL_AGENT_OBSERVABILITY_CONTENT",
            "aggregate",
        )
        .strip()
        .lower()
    )
    return "full" if configured in _FULL_CONTENT_VALUES else "aggregate"


def _captures_content(environment: Mapping[str, str] | None = None) -> bool:
    return _content_mode(environment) == "full"


def _current_user_id() -> str | None:
    """Return a stable backend-local pseudonym for the active Drasil principal."""

    try:
        from app.services.principal_auth import resolve_authenticated_principal

        principal = resolve_authenticated_principal()
    except Exception as error:
        logger.warning("Agent user correlation failed (%s)", type(error).__name__)
        return None
    if principal is None:
        return None
    digest = _hash_private_identifier(principal.principal_id)
    return f"user-{digest}" if digest is not None else None


def _enabled_from_environment(environment: Mapping[str, str]) -> tuple[bool, str]:
    mode = environment.get("DRASIL_AGENT_OBSERVABILITY", "auto").strip().lower()
    if mode in _FALSE_VALUES:
        return False, "disabled_by_configuration"
    if mode not in _SUPPORTED_MODES:
        return False, "unsupported_backend"
    tracing_enabled = environment.get("LANGFUSE_TRACING_ENABLED", "true").strip().lower()
    if tracing_enabled in _FALSE_VALUES:
        return False, "disabled_by_langfuse_configuration"
    if not environment.get("LANGFUSE_PUBLIC_KEY", "").strip():
        return False, "missing_public_key"
    if not environment.get("LANGFUSE_SECRET_KEY", "").strip():
        return False, "missing_secret_key"
    return True, "configured"


def agent_observability_status(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a credential-free health summary suitable for the public health API."""

    source = os.environ if environment is None else environment
    enabled, reason = _enabled_from_environment(source)
    sdk_available = importlib.util.find_spec("langfuse") is not None
    content_mode = _content_mode(source)
    return {
        "backend": "langfuse",
        "enabled": enabled and sdk_available,
        "configured": enabled,
        "sdk_available": sdk_available,
        "reason": reason if not enabled else "ready" if sdk_available else "sdk_unavailable",
        "privacy_mode": "full_content" if content_mode == "full" else "aggregate_only",
        "content_mode": content_mode,
        "captures_content": content_mode == "full",
        "user_tracking": "pseudonymous_principal",
    }


def _get_client() -> Any | None:
    """Lazily construct the SDK after Drasil's local environment has been loaded."""

    global _client_instance
    enabled, _ = _enabled_from_environment(os.environ)
    if not enabled:
        return None
    if _client_instance is not _CLIENT_UNSET:
        return _client_instance
    with _client_lock:
        if _client_instance is not _CLIENT_UNSET:
            return _client_instance
        try:
            from langfuse import Langfuse

            _client_instance = Langfuse(should_export_span=_is_drasil_langfuse_span)
        except Exception as error:  # Observability must never break the agent runtime.
            logger.warning(
                "Langfuse initialization failed; agent tracing is disabled (%s)",
                type(error).__name__,
            )
            _client_instance = None
    return _client_instance


@contextmanager
def _propagate_attributes_context(
    *,
    user_id: str | None,
    session_id: str | None,
    metadata: dict[str, str],
    tags: list[str],
) -> Iterator[None]:
    try:
        from langfuse import propagate_attributes

        manager = propagate_attributes(
            trace_name="drasil-strategist-turn",
            user_id=user_id,
            session_id=session_id,
            metadata=metadata,
            tags=tags,
        )
        manager.__enter__()
    except Exception as error:
        logger.warning("Langfuse attribute propagation failed (%s)", type(error).__name__)
        yield
        return
    try:
        yield
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception as error:
            logger.warning("Langfuse attribute cleanup failed (%s)", type(error).__name__)


@contextmanager
def _observation_context(client: Any, **values: Any) -> Iterator[Any | None]:
    """Start/end an SDK observation without allowing exporter failures to escape."""

    try:
        manager = client.start_as_current_observation(**values)
        observation = manager.__enter__()
    except Exception as error:
        logger.warning("Langfuse observation start failed (%s)", type(error).__name__)
        yield None
        return
    try:
        yield observation
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception as error:
            logger.warning("Langfuse observation cleanup failed (%s)", type(error).__name__)


def _bound_arguments(
    function: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        function_signature = signature(function)
        bound = dict(function_signature.bind_partial(*args, **kwargs).arguments)
        for parameter_name, parameter in function_signature.parameters.items():
            if parameter.kind is not Parameter.VAR_KEYWORD:
                continue
            extra = bound.pop(parameter_name, None)
            if isinstance(extra, dict):
                bound.update(extra)
        return bound
    except (TypeError, ValueError):
        return dict(kwargs)


def _safe_update(observation: Any, **values: Any) -> None:
    if observation is None:
        return
    try:
        observation.update(**values)
    except Exception as error:
        logger.warning("Langfuse observation update failed (%s)", type(error).__name__)


def _has_active_trace(_client: Any) -> bool:
    return _ACTIVE_DRASIL_TRACE.get()


@contextmanager
def _active_trace_context(active: bool) -> Iterator[None]:
    token = _ACTIVE_DRASIL_TRACE.set(active)
    try:
        yield
    finally:
        _ACTIVE_DRASIL_TRACE.reset(token)


def _raise_preserving_traceback(error: Exception, traceback: Any) -> None:
    raise error.with_traceback(traceback)


def _turn_start_metadata(arguments: Mapping[str, Any]) -> dict[str, Any]:
    ui_event = arguments.get("ui_event")
    ui_event_kind = _safe_identifier(ui_event.get("kind")) if isinstance(ui_event, dict) else None
    requested_capabilities = arguments.get("requested_capability_ids")
    staged_nodes = arguments.get("staged_node_ids")
    metadata: dict[str, Any] = {
        "controlPlane": "agent-kernel",
        "captureMode": _content_mode(),
        "hasWorkspaceContext": arguments.get("workspace_context") is not None,
        "requestedCapabilityCount": (
            len(requested_capabilities) if isinstance(requested_capabilities, list) else 0
        ),
        "stagedNodeCount": len(staged_nodes) if isinstance(staged_nodes, list) else 0,
    }
    if ui_event_kind:
        metadata["uiEventKind"] = ui_event_kind
    provider = _safe_identifier(arguments.get("provider_override"))
    model = _safe_identifier(arguments.get("model_override"))
    if provider:
        metadata["providerOverride"] = provider
    if model:
        metadata["modelOverride"] = model
    return metadata


def _turn_input(arguments: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "prompt",
        "ui_event",
        "active_category_id",
        "focus_node_id",
        "staged_node_ids",
        "conversation_history",
        "requested_capability_ids",
        "workspace_context",
    ):
        value = arguments.get(key)
        if value is not None:
            payload[key] = value
    today_override = arguments.get("today_override")
    if today_override is not None:
        payload["today_override"] = str(today_override)
    return payload


def _turn_output(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    return result.get("response")


def _turn_result_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"outcome": "invalid-result"}
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    tool_calls = diagnostics.get("tool_calls")
    metadata: dict[str, Any] = {
        "outcome": "success",
        "hasProposal": any(
            response.get(key) is not None
            for key in (
                "action_proposal",
                "metadata_proposal",
                "branch_proposal",
                "preference_memory_proposal",
            )
        ),
        "hasClarification": response.get("clarification") is not None,
        "toolCallCount": len(tool_calls) if isinstance(tool_calls, list) else 0,
    }
    for source_key, target_key in (
        ("provider", "provider"),
        ("model_name", "model"),
    ):
        value = _safe_identifier(result.get(source_key))
        if value:
            metadata[target_key] = value
    for source_key, target_key in (("mode", "mode"), ("policy", "policy")):
        value = _safe_identifier(response.get(source_key))
        if value:
            metadata[target_key] = value
    return metadata


def observe_agent_turn(function: _F) -> _F:
    """Trace one complete kernel turn with configurable content capture."""

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        client = _get_client()
        if client is None:
            return function(*args, **kwargs)

        arguments = _bound_arguments(function, args, kwargs)
        turn_id = arguments.get("strategist_turn_id")
        session_hash = _hash_private_identifier(arguments.get("strategist_session_id"))
        trace_context = None
        if isinstance(turn_id, str) and turn_id.strip():
            try:
                trace_context = {"trace_id": client.create_trace_id(seed=f"drasil-turn:{turn_id}")}
            except Exception as error:
                logger.warning("Langfuse trace ID creation failed (%s)", type(error).__name__)
        start_metadata = _turn_start_metadata(arguments)
        capture_content = _captures_content()
        observation_kwargs: dict[str, Any] = {
            "as_type": "agent",
            "name": "drasil-strategist-turn",
            "metadata": start_metadata,
        }
        if capture_content:
            observation_kwargs["input"] = _turn_input(arguments)
        if trace_context is not None:
            observation_kwargs["trace_context"] = trace_context

        captured_error: Exception | None = None
        captured_traceback: Any = None
        result: Any = None
        with _observation_context(client, **observation_kwargs) as observation:
            with _active_trace_context(observation is not None):
                with _propagate_attributes_context(
                    user_id=_current_user_id(),
                    session_id=session_hash,
                    metadata={"controlPlane": "agent-kernel", "captureMode": _content_mode()},
                    tags=["drasil", "agent-kernel"],
                ):
                    try:
                        result = function(*args, **kwargs)
                    except Exception as error:
                        captured_error = error
                        captured_traceback = error.__traceback__
                        _safe_update(
                            observation,
                            level="ERROR",
                            status_message=f"agent turn failed: {type(error).__name__}",
                            metadata={**start_metadata, "outcome": "error"},
                        )
                    else:
                        update: dict[str, Any] = {
                            "metadata": _turn_result_metadata(result),
                        }
                        if capture_content:
                            update["output"] = _turn_output(result)
                        _safe_update(observation, **update)
                    if observation is not None:
                        try:
                            trace_id = client.get_current_trace_id()
                        except Exception:
                            trace_id = None
                        if isinstance(trace_id, str):
                            logger.info(
                                "agent turn trace created",
                                extra={"agent_trace_id": trace_id},
                            )
        # Serverless instances can freeze immediately after a response; graceful
        # process shutdown is not a reliable opportunity to export this turn.
        if os.getenv("VERCEL") == "1":
            try:
                client.flush()
            except Exception as error:
                logger.warning("Langfuse turn flush failed (%s)", type(error).__name__)
        if captured_error is not None:
            _raise_preserving_traceback(captured_error, captured_traceback)
        return result

    return cast(_F, wrapper)


def _content_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_content_size(item) for item in value)
    if isinstance(value, dict):
        return sum(_content_size(item) for item in value.values())
    return 0


def _usage_from_payload(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else value
    input_tokens = usage.get("input_tokens", usage.get("inputTokens"))
    output_tokens = usage.get("output_tokens", usage.get("outputTokens"))
    result: dict[str, int] = {}
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
        result["input_tokens"] = input_tokens
    if (
        isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    ):
        result["output_tokens"] = output_tokens
    return result


def _find_usage(value: Any) -> dict[str, int]:
    direct = _usage_from_payload(value)
    if direct:
        return direct
    if isinstance(value, dict):
        for child in value.values():
            found = _find_usage(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in reversed(value):
            found = _find_usage(child)
            if found:
                return found
    return {}


def _generation_input(
    arguments: Mapping[str, Any],
    *,
    prompt_argument: str | None,
    request_body_argument: str | None,
) -> Any:
    if request_body_argument is not None:
        return arguments.get(request_body_argument)
    if prompt_argument is not None:
        return arguments.get(prompt_argument)
    return None


def _generation_output(result: Any) -> Any:
    if isinstance(result, tuple) and result:
        return result[0]
    return result


def observe_generation(
    *,
    name: str,
    provider: str,
    model_argument: str | None = None,
    prompt_argument: str | None = None,
    request_body_argument: str | None = None,
) -> Callable[[_F], _F]:
    """Trace a provider call, optionally including its model input and output."""

    def decorator(function: _F) -> _F:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            client = _get_client()
            if client is None or not _has_active_trace(client):
                return function(*args, **kwargs)
            arguments = _bound_arguments(function, args, kwargs)
            request_body = (
                arguments.get(request_body_argument) if request_body_argument is not None else None
            )
            model_value = arguments.get(model_argument) if model_argument else None
            if model_value is None and isinstance(request_body, dict):
                model_value = request_body.get("model")
            model = _safe_identifier(model_value)
            prompt_chars = 0
            if prompt_argument:
                prompt_chars = _content_size(arguments.get(prompt_argument))
            elif isinstance(request_body, dict):
                prompt_chars = _content_size(request_body.get("input")) + _content_size(
                    request_body.get("instructions")
                )
            metadata: dict[str, Any] = {
                "provider": provider,
                "promptChars": prompt_chars,
                "captureMode": _content_mode(),
            }
            timeout = arguments.get("timeout_seconds")
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                metadata["timeoutSeconds"] = max(0, int(timeout))

            start_kwargs: dict[str, Any] = {
                "as_type": "generation",
                "name": name,
                "metadata": metadata,
            }
            if model:
                start_kwargs["model"] = model
            capture_content = _captures_content()
            if capture_content:
                start_kwargs["input"] = _generation_input(
                    arguments,
                    prompt_argument=prompt_argument,
                    request_body_argument=request_body_argument,
                )
            captured_error: Exception | None = None
            captured_traceback: Any = None
            result: Any = None
            with _observation_context(client, **start_kwargs) as observation:
                try:
                    result = function(*args, **kwargs)
                except Exception as error:
                    captured_error = error
                    captured_traceback = error.__traceback__
                    _safe_update(
                        observation,
                        level="ERROR",
                        status_message=f"provider call failed: {type(error).__name__}",
                        metadata={**metadata, "outcome": "error"},
                    )
                else:
                    usage = _find_usage(result)
                    if not usage:
                        for candidate in (
                            arguments.get("trace_collector"),
                            arguments.get("model_step_trace"),
                        ):
                            usage = _find_usage(candidate)
                            if usage:
                                break
                    update: dict[str, Any] = {"metadata": {**metadata, "outcome": "success"}}
                    if usage:
                        update["usage_details"] = usage
                    if capture_content:
                        update["output"] = _generation_output(result)
                    _safe_update(observation, **update)
            if captured_error is not None:
                _raise_preserving_traceback(captured_error, captured_traceback)
            return result

        return cast(_F, wrapper)

    return decorator


def observe_agent_tool(function: _F) -> _F:
    """Trace an authoritative tool call with configurable payload capture."""

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        client = _get_client()
        if client is None or not _has_active_trace(client):
            return function(*args, **kwargs)
        arguments = _bound_arguments(function, args, kwargs)
        tool_name = _safe_identifier(arguments.get("name")) or "unknown-tool"
        tool_arguments = arguments.get("arguments")
        metadata: dict[str, Any] = {
            "toolName": tool_name,
            "argumentKeyCount": len(tool_arguments) if isinstance(tool_arguments, dict) else 0,
            "captureMode": _content_mode(),
        }
        capture_content = _captures_content()
        captured_error: Exception | None = None
        captured_traceback: Any = None
        result: Any = None
        with _observation_context(
            client,
            as_type="tool",
            name=f"tool.{tool_name}",
            metadata=metadata,
            **({"input": tool_arguments} if capture_content else {}),
        ) as observation:
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                captured_error = error
                captured_traceback = error.__traceback__
                _safe_update(
                    observation,
                    level="ERROR",
                    status_message=f"tool call failed: {type(error).__name__}",
                    metadata={**metadata, "outcome": "error"},
                )
            else:
                result_metadata = {**metadata, "outcome": "success"}
                if isinstance(result, dict):
                    result_metadata["resultKeyCount"] = len(result)
                    result_metadata["readOnly"] = bool(result.get("read_only"))
                    error_value = result.get("error")
                    if error_value is not None:
                        result_metadata["outcome"] = "tool-error"
                        result_metadata["errorFingerprint"] = hashlib.sha256(
                            str(error_value).encode("utf-8")
                        ).hexdigest()[:16]
                update: dict[str, Any] = {"metadata": result_metadata}
                if capture_content:
                    update["output"] = result
                _safe_update(observation, **update)
        if captured_error is not None:
            _raise_preserving_traceback(captured_error, captured_traceback)
        return result

    return cast(_F, wrapper)


def record_agent_event(name: str, metadata: Mapping[str, Any]) -> None:
    """Attach one allow-listed categorical event to the current agent trace."""

    client = _get_client()
    safe_name = _safe_identifier(name)
    if client is None or safe_name is None or not _has_active_trace(client):
        return
    safe_metadata: dict[str, Any] = {}
    for key, value in metadata.items():
        safe_key = _safe_identifier(str(key))
        if safe_key is None:
            continue
        if isinstance(value, bool) or (
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
        ):
            safe_metadata[safe_key] = value
            continue
        safe_value = _safe_identifier(value)
        if safe_value is not None:
            safe_metadata[safe_key] = safe_value
    try:
        client.create_event(name=safe_name, metadata=safe_metadata)
    except Exception as error:
        logger.warning("Langfuse event export failed (%s)", type(error).__name__)


def shutdown_agent_observability() -> None:
    """Flush queued traces during graceful API shutdown."""

    global _client_instance
    client = _client_instance
    _client_instance = _CLIENT_UNSET
    if client is _CLIENT_UNSET or client is None:
        return
    try:
        client.shutdown()
    except Exception as error:
        logger.warning("Langfuse shutdown failed (%s)", type(error).__name__)


__all__ = [
    "agent_observability_status",
    "observe_agent_tool",
    "observe_agent_turn",
    "observe_generation",
    "record_agent_event",
    "shutdown_agent_observability",
]
