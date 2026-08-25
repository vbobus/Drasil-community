"""Read-only admin introspection for Drasil's agent architecture.

The endpoint projects runtime-owned registries and persisted traces. It does not
modify configuration, execute agent tools, or expose provider credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, inspect, text
from sqlmodel import Session, select

from app.db.database import get_active_engine, get_session
from app.models.strategist_agent import StrategistAgentEvent, StrategistRun
from app.models.strategist_chat import StrategistChatSession, StrategistChatTurn
from app.services.agent_kernel.agent_profiles import agent_profiles
from app.services.agent_kernel.authority_inventory import (
    build_authority_inventory,
)
from app.services.agent_kernel.capabilities import capability_catalog
from app.services.agent_kernel.provider_transport import codex_native_tool_loop_enabled
from app.services.agent_kernel.mcp_read_shadow import (
    MCP_READ_SHADOW_ENABLED_ENV,
    mcp_read_shadow_enabled,
)
from app.services.agent_kernel.mcp_read_rollout import (
    evaluate_mcp_read_promotion,
)
from app.services.agent_kernel.rollout import (
    ENV_FLAGS,
    FAMILY_OPERATION_KINDS,
    OperationFamily,
    RolloutPolicy,
    native_review_required_enabled,
    native_shadow_model_tools_enabled,
    native_shadow_sampling_enabled,
)
from app.services.agent_kernel.tool_registry import registered_kernel_tool_specs
from app.services.strategist_provider import get_effective_provider_settings


router = APIRouter(prefix="/api/v1/admin/agent-architecture", tags=["Admin"])

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "[nested value omitted]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:60]:
            normalized = str(key).lower()
            result[str(key)] = (
                "[redacted]"
                if any(part in normalized for part in _SENSITIVE_KEY_PARTS)
                else _safe_value(item, depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return value if len(value) <= 2000 else f"{value[:2000]}…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:800]


def _iter_application_routes(routes: list[Any]):
    """Yield concrete routes across FastAPI's flat and grouped route layouts."""
    for route in routes:
        if getattr(route, "path", None):
            yield route
            continue

        original_router = getattr(route, "original_router", None)
        nested_routes = getattr(original_router, "routes", None)
        if nested_routes:
            yield from _iter_application_routes(nested_routes)


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_projection(event: StrategistAgentEvent) -> dict[str, Any]:
    payload = _json_object(event.payload_json)
    details: dict[str, Any] = {}
    for key in (
        "prompt",
        "ui_event_kind",
        "route_kind",
        "tier",
        "reason_code",
        "status",
        "kind",
        "phase",
        "duration_ms",
        "name",
        "mode",
        "proposal_family",
        "validation_status",
        "solver_status",
        "selected_authority",
        "summary",
        "policy",
        "has_proposal",
        "has_clarification",
        "provider",
        "model_name",
        "dynamic_tool_names",
        "error_type",
        "error_detail",
        "reasonCode",
    ):
        if key in payload:
            details[key] = payload[key]
    if event.event_type == "tool_observation":
        details.update(
            {
                "tool": payload.get("name"),
                "arguments": payload.get("arguments"),
                "result": {
                    key: payload.get("result", {}).get(key)
                    for key in (
                        "result_kind",
                        "observation_summary",
                        "error",
                        "planner_mode",
                        "proposal_ready",
                        "needs_user_input",
                        "tool_route_repaired",
                    )
                    if isinstance(payload.get("result"), dict)
                    and payload["result"].get(key) is not None
                },
            }
        )
    details["captured"] = payload
    return {
        "id": event.id,
        "sequence": event.sequence,
        "eventType": event.event_type,
        "source": event.source,
        "turnId": event.turn_id,
        "runId": event.run_id,
        "goalId": event.goal_id,
        "goalRevision": event.goal_revision,
        "correlationId": event.correlation_id,
        "createdAt": event.created_at.isoformat(),
        "details": _safe_value(details),
    }


def _proposal_kind(response: dict[str, Any]) -> str | None:
    if isinstance(response.get("review_document"), dict):
        return "review_document"
    for key in (
        "schedule_proposal",
        "branch_proposal",
        "metadata_proposal",
        "preference_memory_proposal",
        "action_proposal",
    ):
        if isinstance(response.get(key), dict):
            return key.removesuffix("_proposal")
    return None


def _tool_payload() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for spec in registered_kernel_tool_specs():
        properties = spec.input_schema.get("properties", {})
        tools.append(
            {
                "name": spec.name,
                "description": spec.description,
                "contractVersion": spec.contract_version,
                "readOnly": spec.read_only,
                "sideEffect": spec.side_effect,
                "approvalRequirement": spec.approval_requirement,
                "idempotencyBehavior": spec.idempotency_behavior,
                "externalSurfaces": list(spec.external_surfaces),
                "resultKind": spec.result_kind,
                "inputFields": [
                    {
                        "name": name,
                        "required": name in spec.input_schema.get("required", []),
                        "description": (
                            value.get("description")
                            if isinstance(value, dict)
                            else None
                        ),
                        "schema": _safe_value(value),
                    }
                    for name, value in properties.items()
                ],
                "outputSchema": _safe_value(spec.output_schema),
                "errorCodes": list(spec.error_codes),
                "semanticValidator": (
                    spec.semantic_validator.__name__
                    if spec.semantic_validator is not None
                    else None
                ),
            }
        )
    return tools


def _database_payload() -> list[dict[str, Any]]:
    engine = get_active_engine()
    inspector = inspect(engine)
    tables: list[dict[str, Any]] = []
    with engine.connect() as connection:
        for table_name in sorted(inspector.get_table_names()):
            count = connection.execute(
                text(f'SELECT COUNT(*) FROM "{table_name}"')
            ).scalar_one()
            tables.append(
                {
                    "name": table_name,
                    "rowCount": int(count),
                    "columns": [
                        {
                            "name": column["name"],
                            "type": str(column["type"]),
                            "nullable": bool(column.get("nullable", True)),
                            "primaryKey": bool(column.get("primary_key", False)),
                        }
                        for column in inspector.get_columns(table_name)
                    ],
                    "foreignKeys": [
                        {
                            "columns": item.get("constrained_columns", []),
                            "targetTable": item.get("referred_table"),
                            "targetColumns": item.get("referred_columns", []),
                        }
                        for item in inspector.get_foreign_keys(table_name)
                    ],
                }
            )
    return tables


@router.get("")
def get_agent_architecture(request: Request) -> dict[str, Any]:
    tools = _tool_payload()
    profiles = [profile.compact() for profile in agent_profiles()]
    capabilities = [
        {
            **manifest.compact(),
            "notFor": list(manifest.not_for),
            "recommendedTools": list(manifest.recommended_tools),
            "dependencies": list(manifest.dependencies),
            "evidenceRequirements": manifest.evidence_requirements,
        }
        for manifest in capability_catalog()
    ]
    provider = get_effective_provider_settings()
    rollout = RolloutPolicy.from_environment()
    with Session(get_active_engine()) as architecture_session:
        mcp_read_promotion = evaluate_mcp_read_promotion(
            architecture_session
        )
    api_routes = [
        {
            "path": route.path,
            "methods": sorted(route.methods or []),
            "name": route.name,
            "tags": list(getattr(route, "tags", []) or []),
        }
        for route in _iter_application_routes(request.app.routes)
        if getattr(route, "path", "").startswith("/api/")
    ]
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "definition": {
            "toolContractBoundary": (
                "The typed boundary between model-selected intent and host execution: tool "
                "identity, input schema, semantic validation, target/scope conservation, "
                "side-effect policy, approval requirement, result schema, and typed errors."
            ),
            "authorityRule": (
                "The model selects meaning and tools. Host validators preserve identity, scope, "
                "cardinality, authorization, and approval before executable code can run."
            ),
            "mcpInvocationBoundary": (
                "Principal, session, run, goal revision, local time, selected tasks, and allowed "
                "tools are trusted server-injected context; they are never model-authored tool arguments."
            ),
        },
        "pipeline": [
            {"id": "ui", "label": "Chat UI", "kind": "interface", "description": "Captures prompt, selected tasks, proposal actions, local time, and timezone."},
            {"id": "api", "label": "AI API", "kind": "boundary", "description": "Validates the HTTP request, creates the durable turn/run, and streams progress."},
            {"id": "orchestrator", "label": "Agent Kernel orchestrator", "kind": "controller", "description": "Reconstructs context, selects the bounded route, exposes tools, and conserves the objective."},
            {"id": "strategist", "label": "Strategist agent", "kind": "agent", "description": "Owns free-form intent, scope, tool choice, clarification, and final synthesis."},
            {"id": "tools", "label": "Tool-contract boundary", "kind": "contract", "description": "Pydantic shape plus semantic invariants turn reasoning into safe typed calls."},
            {"id": "planner", "label": "Planner agent/service", "kind": "agent", "description": "Evaluates temporal placement, conflicts, preferences, recurrence, and alternatives."},
            {"id": "changeset", "label": "GoalState → ChangeSet", "kind": "contract", "description": "Conserves requested operations, simulates combined effects, and prepares review."},
            {"id": "approval", "label": "Human approval", "kind": "interaction", "description": "Approve, edit, reject, retry, or resolve a typed conflict decision."},
            {"id": "transaction", "label": "Transaction runtime", "kind": "execution", "description": "Revalidates revisions, commits atomically, verifies results, and queues external sync."},
            {"id": "database", "label": "SQLite + external calendar", "kind": "data", "description": "Durable local truth plus idempotent external synchronization."},
        ],
        "agents": profiles,
        "skills": capabilities,
        "tools": tools,
        "connections": [
            {
                "id": "codex-app-server",
                "label": "Codex app-server",
                "protocol": "JSON-RPC over stdio",
                "kind": "provider_transport",
                "enabled": codex_native_tool_loop_enabled(),
                "description": "Uses cached ChatGPT/Codex OAuth and exposes Drasil tools as host-owned dynamic functions.",
            },
            {
                "id": "dynamic-tool-bridge",
                "label": "Drasil dynamic-tool bridge",
                "protocol": "Codex dynamicTools",
                "kind": "tool_bridge",
                "enabled": True,
                "description": "Dispatches only registered Drasil tools; validators and application authorization remain server-owned.",
            },
            {
                "id": "local-mcp-read-shadow",
                "label": "Local drasil-core read shadow",
                "protocol": "MCP JSON-RPC in-process",
                "kind": "shadow_transport",
                "enabled": mcp_read_shadow_enabled(),
                "description": (
                    "Replays eligible read-only calls and records digest/diff telemetry. "
                    "Native results remain authoritative and MCP results are never returned to users."
                ),
            },
        ],
        "mcpServers": [],
        "mcpBoundaryNote": (
            "The read-only drasil-core contract supports an optional host-owned local shadow "
            "client, but no MCP server is connected as an authoritative application capability. "
            "Shadow results never replace native results. Any provider-side connectors in the "
            "surrounding Codex environment are neither enumerated nor authorized as Drasil tools."
        ),
        "executionMigration": {
            "taskReadAuthority": "native_application_service",
            "legacyReadDispatcher": "compatibility_parity_only",
            "calendarReadAuthority": "native_sql_application_service",
            "legacyCalendarReader": "compatibility_parity_only",
            "plannerExecutionBoundary": "scheduling_application_service",
            "plannerImplementation": "existing_orchestration_behind_compatibility_adapter",
            "mcpReadClient": "implemented_shadow_only",
            "mcpReadShadowEnabled": mcp_read_shadow_enabled(),
            "mcpReadShadowFlag": MCP_READ_SHADOW_ENABLED_ENV,
            "mcpReadAuthority": "native",
            "mcpReadPromotionEligible": mcp_read_promotion.eligible,
            "writeAuthority": "current_agent_kernel_and_compatibility_proposals",
            "retirementAllowed": False,
        },
        "authorityInventory": build_authority_inventory(rollout),
        "mcpReadPromotion": mcp_read_promotion.to_payload(),
        "provider": {
            "activeProvider": provider.get("activeProvider"),
            "model": provider.get("codexModel") if provider.get("activeProvider") == "codex-cli" else provider.get("openAiModel"),
            "reasoningEffort": provider.get("codexReasoningEffort"),
            "transport": "codex_app_server" if provider.get("activeProvider") == "codex-cli" else "openai_api",
        },
        "rollout": {
            "nativeReviewRequired": native_review_required_enabled(),
            "shadowSampling": native_shadow_sampling_enabled(),
            "shadowModelTools": native_shadow_model_tools_enabled(),
            "families": [
                {
                    "family": family.value,
                    "flag": ENV_FLAGS[family],
                    "enabled": rollout.enabled.get(family) is True,
                    "operationKinds": sorted(FAMILY_OPERATION_KINDS[family]),
                    "evidenceCount": len([item for item in rollout.evidence if item.family is family]),
                }
                for family in OperationFamily
            ],
        },
        "apiRoutes": sorted(api_routes, key=lambda item: (item["path"], item["methods"])),
        "database": {
            "engine": "SQLite",
            "tables": _database_payload(),
        },
    }


@router.get("/sessions")
def list_agent_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    records = list(
        session.exec(
            select(StrategistChatSession)
            .order_by(StrategistChatSession.updated_at.desc())
            .limit(limit)
        ).all()
    )
    items: list[dict[str, Any]] = []
    for record in records:
        turn_count = session.exec(
            select(func.count(StrategistChatTurn.id)).where(
                StrategistChatTurn.session_id == record.id
            )
        ).one()
        event_count = session.exec(
            select(func.count(StrategistAgentEvent.id)).where(
                StrategistAgentEvent.session_id == record.id
            )
        ).one()
        latest_turn = session.exec(
            select(StrategistChatTurn)
            .where(StrategistChatTurn.session_id == record.id)
            .order_by(StrategistChatTurn.created_at.desc())
            .limit(1)
        ).first()
        items.append(
            {
                "id": record.id,
                "title": record.title,
                "provider": record.provider_label,
                "model": record.model_name,
                "updatedAt": record.updated_at.isoformat(),
                "turnCount": int(turn_count),
                "eventCount": int(event_count),
                "latestPrompt": latest_turn.prompt if latest_turn else None,
            }
        )
    return {"sessions": items}


@router.get("/sessions/{session_id}")
def get_agent_session_trace(
    session_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    chat = session.get(StrategistChatSession, session_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Strategist session not found.")
    turns = list(
        session.exec(
            select(StrategistChatTurn)
            .where(StrategistChatTurn.session_id == session_id)
            .order_by(StrategistChatTurn.created_at.asc())
        ).all()
    )
    runs = list(
        session.exec(
            select(StrategistRun)
            .where(StrategistRun.session_id == session_id)
            .order_by(StrategistRun.created_at.asc())
        ).all()
    )
    events = list(
        session.exec(
            select(StrategistAgentEvent)
            .where(StrategistAgentEvent.session_id == session_id)
            .order_by(
                StrategistAgentEvent.created_at.asc(),
                StrategistAgentEvent.sequence.asc(),
            )
        ).all()
    )
    turn_id_by_correlation = {
        run.correlation_id: run.turn_id for run in runs if run.correlation_id
    }
    projected_events = []
    for event in events:
        projected = _event_projection(event)
        if projected["turnId"] is None:
            projected["turnId"] = turn_id_by_correlation.get(event.correlation_id)
        projected_events.append(projected)
    return {
        "session": {
            "id": chat.id,
            "title": chat.title,
            "provider": chat.provider_label,
            "model": chat.model_name,
            "createdAt": chat.created_at.isoformat(),
            "updatedAt": chat.updated_at.isoformat(),
        },
        "turns": [
            {
                "id": turn.id,
                "prompt": turn.prompt,
                "createdAt": turn.created_at.isoformat(),
                "error": turn.error_text,
                "response": {
                    "mode": response.get("mode"),
                    "summary": response.get("summary"),
                    "proposalKind": _proposal_kind(response),
                    "hasClarification": isinstance(response.get("clarification"), dict),
                    "handoffOutcome": _safe_value(response.get("handoff_outcome")),
                },
            }
            for turn in turns
            for response in [_json_object(turn.response_json)]
        ],
        "runs": [
            {
                "id": run.id,
                "turnId": run.turn_id,
                "status": run.status,
                "controlPlane": run.control_plane,
                "goalId": run.goal_id,
                "goalRevision": run.goal_revision,
                "correlationId": run.correlation_id,
                "prompt": run.prompt,
                "error": run.error_text,
                "request": _safe_value(_json_object(run.request_json)),
                "createdAt": run.created_at.isoformat(),
                "startedAt": run.started_at.isoformat() if run.started_at else None,
                "completedAt": run.completed_at.isoformat() if run.completed_at else None,
            }
            for run in runs
        ],
        "events": projected_events,
    }
