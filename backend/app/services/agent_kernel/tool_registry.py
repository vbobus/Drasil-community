"""Canonical high-level capability contracts for Drasil agents and MCP."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ValidationError

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.context_contract import (
    compact_pending_proposal_for_model,
    task_contract_surface,
)
from app.services.task_read_handlers import (
    STRATEGIST_TOOL_DEFINITIONS,
)
from app.services.agent_kernel.task_read_service import execute_native_task_read
from app.services.agent_kernel.calendar_service import (
    CalendarReadApplicationService,
    CalendarReadInvocation,
)
from app.services.agent_kernel.scheduling_service import (
    SchedulePlanningRequest,
    SchedulingApplicationService,
    schedule_planning_result_payload,
)
from app.services.strategist_metadata_intent import (
    build_metadata_updates_from_prompt,
    parse_prompt_date,
    parse_prompt_estimate_minutes,
)
from app.services.agent_kernel.task_patch import (
    build_task_patch_draft,
    simulate_task_patch,
    validate_task_patch,
)
from app.services.agent_kernel.capabilities import (
    load_skill,
    search_capabilities,
    validate_capability_tool_references,
)
from app.services.preference_patches import (
    PREFERENCE_RESPONSE_KEY,
    build_preference_management_proposal_from_tool_arguments,
    build_preference_memory_proposal_from_tool_arguments,
)
from app.services.strategist_proposal_normalizer import (
    normalize_branch_proposal,
    normalize_metadata_updates,
    normalize_metadata_proposal,
    normalize_structural_operation_kind,
)
from app.services.agent_kernel.contracts import (
    DraftChangeSetCommand,
    DraftChangeSetResult,
    ChangeSetSimulationResult,
    GoalTurnDecision,
    GoalStateUpdateCommand,
    SimulateChangeSetCommand,
    ToolError,
    ToolErrorCategory,
    ToolResult,
    strict_provider_schema,
)
from app.services.agent_kernel.contracts.tool_models import model_from_object_schema


GENERIC_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}

V2_TOOL_ERROR_CODES: tuple[str, ...] = (
    "TOOL_NOT_FOUND",
    "TOOL_ARGUMENTS_INVALID",
    "TOOL_OUTPUT_INVALID",
    "TOOL_CONTEXT_UNAVAILABLE",
    "TOOL_AUTHORIZATION_DENIED",
    "TOOL_RETRYABLE_SYSTEM_FAILURE",
    "TOOL_VALIDATION_FAILED",
    "TOOL_INVARIANT_FAILED",
    "CONFLICT_REQUIRES_DECISION",
)


class ToolSemanticValidationError(ValueError):
    """Repairable semantic problem in one otherwise well-shaped tool call."""

    def __init__(
        self,
        message: str,
        *,
        affected_paths: list[str] | None = None,
        repair_guidance: str | None = None,
    ) -> None:
        super().__init__(message)
        self.affected_paths = list(affected_paths or [])
        self.repair_guidance = repair_guidance


@dataclass(frozen=True)
class KernelToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool = True
    result_kind: str = "task_context"
    contract_version: str = "2.0"
    side_effect: str | None = None
    approval_requirement: str | None = None
    idempotency_behavior: str | None = None
    error_codes: tuple[str, ...] = V2_TOOL_ERROR_CODES
    external_surfaces: tuple[str, ...] = ()
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] = ToolResult
    semantic_validator: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.input_model is None:
            object.__setattr__(
                self,
                "input_model",
                model_from_object_schema(f"{self.name}_v2", self.input_schema),
            )
        if self.side_effect is None:
            object.__setattr__(self, "side_effect", "none" if self.read_only else "draft")
        if self.approval_requirement is None:
            object.__setattr__(
                self,
                "approval_requirement",
                "none" if self.read_only else "required_before_commit",
            )
        if self.idempotency_behavior is None:
            object.__setattr__(
                self,
                "idempotency_behavior",
                "read_only" if self.read_only else "draft_only",
            )

    def tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": copy.deepcopy(self.input_schema),
        }

    def v2_tool_schema(self) -> dict[str, Any]:
        assert self.input_model is not None
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": strict_provider_schema(self.input_model),
            "strict": True,
        }

    def mcp_tool_schema(self) -> dict[str, Any]:
        """Project the canonical contract into the MCP tools/list shape."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": copy.deepcopy(self.input_schema),
            "outputSchema": copy.deepcopy(self.output_schema),
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.side_effect not in {None, "none", "draft"},
                "idempotentHint": self.read_only
                or self.idempotency_behavior
                in {"read_only", "draft_only", "command_id_replay_safe"},
            },
        }

    def validate_v2_input(self, value: object) -> dict[str, Any]:
        assert self.input_model is not None
        validated = self.input_model.model_validate(value).model_dump(
            by_alias=True,
            exclude_unset=True,
            exclude_none=True,
        )
        if self.semantic_validator is not None:
            validated = self.semantic_validator(validated)
        return validated

    def validate_v2_output(self, value: object) -> dict[str, Any]:
        return self.output_model.model_validate(value).model_dump(by_alias=True)

    def context_summary(self) -> dict[str, Any]:
        properties = self.input_schema.get("properties")
        required = self.input_schema.get("required")
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "result_kind": self.result_kind,
            "contract_version": self.contract_version,
            "side_effect": self.side_effect,
            "approval_requirement": self.approval_requirement,
            "idempotency_behavior": self.idempotency_behavior,
            "error_codes": list(self.error_codes),
            "external_surfaces": list(self.external_surfaces),
            "input_keys": sorted(properties.keys()) if isinstance(properties, dict) else [],
            "required": required if isinstance(required, list) else [],
        }


def _output_schema(*, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    schema = copy.deepcopy(GENERIC_OBJECT_SCHEMA)
    if properties:
        schema["properties"] = properties
    return schema


def _kernel_tool_description(definition: dict[str, Any]) -> str:
    name = str(definition.get("name") or "").strip()
    description = str(definition["description"])
    if name == "search_entities":
        return (
            description
            + " Pass a short task title, alias, or noun phrase; do not pass the whole user prompt."
        )
    if name == "search_notes":
        return (
            description
            + " Pass the smallest note phrase or title clue needed; do not pass the whole user prompt."
        )
    if name == "list_tasks_by_filter":
        return (
            description
            + " Prefer structured filters over text query when filtering by status, priority, deadline, estimate, or category."
        )
    if name == "get_workload_overview":
        return (
            description
            + " This is the authoritative bounded scope-wide evidence surface for prioritization; "
            "candidate search results are not a substitute."
        )
    if name == "query_task_graph":
        return (
            description
            + " Prefer this as the first tool for ambiguous read-only task-world questions; it can suggest narrower follow-up tools. "
            "Do not use it for a cleanup or restructure mutation; the cleanup analyzer is the complete bounded evidence surface."
        )
    if name == "analyze_tree_cleanup_candidates":
        return (
            description
            + " Use this as the first task-world tool for cleanup or restructure requests. "
            "For an explicit full cleanup, request include_organization_map=true with the full "
            "supported page, then use its cleanup_candidates, organization_map, and "
            "destination_catalog directly; do not precede or repeat it with query_task_graph."
        )
    return description


def _spec_from_retrieval_tool(definition: dict[str, Any], *, result_kind: str) -> KernelToolSpec:
    return KernelToolSpec(
        name=str(definition["name"]),
        description=_kernel_tool_description(definition),
        input_schema=copy.deepcopy(definition.get("parameters", GENERIC_OBJECT_SCHEMA)),
        output_schema=_output_schema(),
        result_kind=result_kind,
        external_surfaces=("drasil_mcp",),
    )


def _retrieval_specs() -> list[KernelToolSpec]:
    result_kinds = {
        "search_entities": "search_results",
        "search_notes": "note_search_results",
        "get_task_snapshot": "task_snapshot",
        "get_task_metadata": "task_metadata",
        "get_task_notes": "task_notes",
        "get_task_branch": "task_branch",
        "get_parent_chain": "parent_chain",
        "get_children": "children",
        "get_siblings": "siblings",
        "get_subtree": "subtree",
        "get_related_entities": "related_entities",
        "get_session_working_set": "working_set",
        "get_recent_session_actions": "recent_session_actions",
        "get_workload_overview": "workload_overview",
        "query_task_graph": "task_graph_query",
        "list_tasks_by_filter": "filtered_tasks",
        "analyze_tree_cleanup_candidates": "cleanup_analysis",
    }
    specs: list[KernelToolSpec] = []
    for definition in STRATEGIST_TOOL_DEFINITIONS:
        name = str(definition.get("name") or "").strip()
        if not name:
            continue
        specs.append(
            _spec_from_retrieval_tool(
                definition,
                result_kind=result_kinds.get(name, "task_context"),
            )
        )
    return specs


STRUCTURAL_OPERATION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": ["string", "null"],
            "description": (
                "Operation kind, such as create_node, metadata_update, move_subtree, "
                "archive_node, delete_node, reorder_node, or rename_node."
            ),
        },
        "operation": {
            "type": ["string", "null"],
            "description": "Alias for kind; normalized by the harness.",
        },
        "op": {
            "type": ["string", "null"],
            "description": "Alias for kind; normalized by the harness.",
        },
        "type": {
            "type": ["string", "null"],
            "description": "Alias for kind; normalized by the harness.",
        },
        "action": {
            "type": ["string", "null"],
            "description": "Alias for kind; normalized by the harness.",
        },
        "node_id": {"type": ["integer", "null"]},
        "task_id": {"type": ["integer", "null"]},
        "source_task_id": {"type": ["integer", "null"]},
        "source_task_title": {"type": ["string", "null"]},
        "title": {
            "type": ["string", "null"],
            "description": "Title for create_node, rename, or title updates.",
        },
        "node": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "title": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
                "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
                "deadline_at": {"type": ["string", "null"]},
                "priority": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"]},
                "time_estimate_minutes": {"type": ["integer", "null"]},
                "estimate_minutes": {"type": ["integer", "null"]},
                "estimated_duration_minutes": {"type": ["integer", "null"]},
                "duration_minutes": {"type": ["integer", "null"]},
                "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                "category_id": {"type": ["integer", "null"]},
                "category_name": {"type": ["string", "null"]},
                "node_type": {"type": ["string", "null"]},
                "is_pinned": {"type": ["boolean", "null"]},
                "is_backlog": {"type": ["boolean", "null"]},
            },
            "description": (
                "Optional nested create-node payload. The harness flattens title, "
                "notes, deadline_at, priority, status, estimate, tags, and category."
            ),
        },
        "category_id": {"type": ["integer", "null"]},
        "category_name": {"type": ["string", "null"]},
        "parent_id": {"type": ["integer", "null"]},
        "parent_task_id": {"type": ["integer", "null"]},
        "parent_title": {"type": ["string", "null"]},
        "parent_task_title": {"type": ["string", "null"]},
        "parent_temp_id": {"type": ["string", "null"]},
        "destination_parent_id": {"type": ["integer", "null"]},
        "destination_parent_task_id": {"type": ["integer", "null"]},
        "destination_parent_title": {"type": ["string", "null"]},
        "destination_parent_task_title": {"type": ["string", "null"]},
        "destination_parent_temp_id": {"type": ["string", "null"]},
        "destination_category_id": {
            "type": ["integer", "null"],
            "description": (
                "Destination workspace/category for move_subtree. Supply this for cross-workspace "
                "moves; descendants stay attached and move with the subtree."
            ),
        },
        "destination_category_name": {"type": ["string", "null"]},
        "temp_id": {"type": ["string", "null"]},
        "target_index": {"type": ["integer", "null"]},
        "sequence": {
            "type": ["integer", "null"],
            "description": "One-based ordering alias for target_index.",
        },
        "order": {
            "type": ["integer", "null"],
            "description": "One-based ordering alias for target_index.",
        },
        "position": {
            "type": ["integer", "null"],
            "description": "One-based ordering alias for target_index.",
        },
        "notes": {"type": ["string", "null"]},
        "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
        "deadline_at": {
            "type": ["string", "null"],
            "description": "ISO date YYYY-MM-DD for create_node or metadata_update.",
        },
        "time_estimate_minutes": {
            "type": ["integer", "null"],
            "description": (
                "A grounded common-sense estimate for the new task. Supply this even "
                "when the user omitted a duration: brief atomic checks, bookings, "
                "confirmations, or messages are normally 5-10 minutes, while ordinary "
                "focused execution is commonly 30-90 minutes. Do not assign independent "
                "effort to a grouping/outcome parent whose children represent its work."
            ),
        },
        "estimate_minutes": {
            "type": ["integer", "null"],
            "description": "Alias for time_estimate_minutes; normalized by the harness.",
        },
        "duration_minutes": {
            "type": ["integer", "null"],
            "description": "Alias for time_estimate_minutes on a task create operation.",
        },
        "estimated_duration_minutes": {
            "type": ["integer", "null"],
            "description": "Alias for time_estimate_minutes on a task create operation.",
        },
        "status": {"type": ["string", "null"]},
        "priority": {"type": ["string", "null"]},
        "node_type": {"type": ["string", "null"]},
        "emoji": {"type": ["string", "null"]},
        "tags": {"type": ["array", "null"], "items": {"type": "string"}},
        "is_pinned": {"type": ["boolean", "null"]},
        "is_backlog": {"type": ["boolean", "null"]},
        "updates": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "title": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
                "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
                "deadline_at": {"type": ["string", "null"]},
                "priority": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"]},
                "time_estimate_minutes": {"type": ["integer", "null"]},
                "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                "node_type": {"type": ["string", "null"]},
                "is_pinned": {"type": ["boolean", "null"]},
                "is_backlog": {"type": ["boolean", "null"]},
            },
            "description": "Metadata update fields for metadata_update operations.",
        },
    },
}


CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "unavailable_interval",
                "allowed_window",
                "exact_start",
                "task_order",
                "minimum_gap",
                "contiguous",
            ],
        },
        "hard": {"type": "boolean"},
        "label": {"type": "string"},
        "source_text": {"type": "string"},
        "task_ids": {"type": "array", "items": {"type": "integer"}},
        "target_temp_ids": {"type": "array", "items": {"type": "string"}},
        "start_at": {"type": ["string", "null"]},
        "end_at": {"type": ["string", "null"]},
        "before_task_id": {"type": ["integer", "null"]},
        "after_task_id": {"type": ["integer", "null"]},
        "before_temp_id": {"type": ["string", "null"]},
        "after_temp_id": {"type": ["string", "null"]},
        "minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
        "recurrence": {"type": ["string", "null"], "enum": ["once", "daily", None]},
        "days_of_week": {
            "type": ["array", "null"],
            "items": {
                "type": "string",
                "enum": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                ],
            },
        },
    },
    "required": ["kind", "hard", "label", "source_text"],
}


CREATED_TASK_SCHEDULE_GROUP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "target_temp_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "Created task temp_ids that share this placement window and scheduling order."
            ),
        },
        "target_task_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Existing task IDs that must be solved in the same placement pass as the "
                "created targets. Use this for mixed existing-and-new requests instead of "
                "a separate Planner handoff."
            ),
        },
        "instruction": {
            "type": "string",
            "description": "Only the scheduling constraints for this group.",
        },
        "window_start": {
            "type": "string",
            "description": "Offset-free ISO local wall time.",
        },
        "window_end": {
            "type": "string",
            "description": "Offset-free ISO local wall time.",
        },
        "slot_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
        "request_constraints": {
            "type": "array",
            "items": CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA,
            "description": (
                "Typed hard or soft constraints for this group. New tasks are addressed by "
                "target_temp_ids; existing tasks are addressed by task_ids."
            ),
        },
    },
    # A group may contain only persisted tasks, and the planner deliberately
    # treats an omitted target_temp_ids list as all created tasks when there are
    # no persisted targets. Requiring this transport field rejected both valid
    # meanings before the semantic planner could resolve them.
    "required": ["instruction", "window_start", "window_end"],
}


REQUEST_ITEM_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_text": {
            "type": "string",
            "description": "Smallest current-request fragment that establishes this item.",
        },
        "changes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string",
                "enum": ["create", "schedule", "update", "move", "archive", "delete"],
            },
        },
        "target_temp_ids": {"type": "array", "items": {"type": "string"}},
        "target_task_ids": {"type": "array", "items": {"type": "integer"}},
        "schedule_group_index": {"type": ["integer", "null"], "minimum": 0},
    },
    "required": ["source_text", "changes"],
}


def _canonical_request_changes(value: Any) -> list[str]:
    """Classify provider action vocabulary into the coarse intent ledger.

    The ledger records user-visible result families, not one provider's exact
    spelling. A compound label can therefore yield more than one family; an
    unknown fine-grained label is ignored because executable operations later
    populate the canonical ledger.
    """

    raw = str(value or "").strip().casefold()
    if not raw:
        return []
    # An explicit decision to leave a task off the calendar is scheduling
    # metadata, but it is not a request to schedule that task.  Treating the
    # word "scheduling" in values such as ``leave_unscheduled`` as a positive
    # action silently broadened otherwise precise schedule target sets.
    compact = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if compact in {
        "unscheduled",
        "leave_unscheduled",
        "keep_unscheduled",
        "schedule_later",
        "scheduling_later",
        "defer_schedule",
        "defer_scheduling",
        "do_not_schedule",
        "dont_schedule",
        "not_scheduled",
    }:
        return []
    canonical = {"create", "schedule", "update", "move", "archive", "delete"}
    if raw in canonical:
        return [raw]
    exact_aliases = {
        "task.create": ("create",),
        "create_node": ("create",),
        "task_creation": ("create",),
        "create_task": ("create",),
        "calendar.create_block": ("schedule",),
        "create_block": ("schedule",),
        "calendar_schedule": ("schedule",),
        "calendar_scheduling": ("schedule",),
        "schedule_task": ("schedule",),
        "schedule_once": ("schedule",),
        "scheduling": ("schedule",),
        "recurrence.create_series": ("schedule",),
        "create_series": ("schedule",),
        "task.update": ("update",),
        "metadata_update": ("update",),
        "task.move": ("move",),
        "move_subtree": ("move",),
        "task.archive": ("archive",),
        "archive_node": ("archive",),
        "task.delete": ("delete",),
        "delete_node": ("delete",),
    }
    if raw in exact_aliases:
        return list(exact_aliases[raw])

    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", raw)
        if token
    }
    changes: list[str] = []
    schedule_tokens = {
        "schedule", "scheduling", "calendar", "block", "placement",
        "recurrence", "recurring", "series", "slot",
    }
    update_tokens = {
        "update", "edit", "modify", "metadata", "deadline", "due",
        "status", "priority", "estimate", "duration", "rename", "title", "notes",
    }
    if tokens & {"create", "add", "new"} and (
        "task" in tokens or not tokens & schedule_tokens
    ):
        changes.append("create")
    if tokens & schedule_tokens or ({"set", "time"} <= tokens):
        changes.append("schedule")
    if tokens & update_tokens:
        changes.append("update")
    if tokens & {"move", "reparent", "reorder", "relocate"}:
        changes.append("move")
    if tokens & {"archive", "deactivate"}:
        changes.append("archive")
    if tokens & {"delete", "erase", "purge"}:
        changes.append("delete")
    # The request ledger intentionally has only six user-visible result
    # families. Provider-authored planning labels outside that vocabulary are
    # annotations, not new executable actions. Keeping an unknown label made a
    # semantically valid plan fail strict schema validation before its actual
    # operations could populate the ledger below.
    return list(dict.fromkeys(changes))


_TASK_PLAN_COVERAGE_STORE_KEY = "__task_plan_intent_coverage__"
_MISSING_TASK_PLAN_COVERAGE = object()


def _task_plan_intent_coverage(arguments: Mapping[str, Any]) -> dict[str, int]:
    operations = arguments.get("structural_operations")
    structural_count = len(operations) if isinstance(operations, list) else 0
    request_items = arguments.get("request_items")
    request_atoms: set[tuple[str, str, str]] = set()
    if isinstance(request_items, list):
        for item in request_items:
            if not isinstance(item, dict):
                continue
            targets = [
                *(f"temporary:{value}" for value in item.get("target_temp_ids") or []),
                *(f"task:{value}" for value in item.get("target_task_ids") or []),
            ]
            if not targets:
                targets = [f"clause:{(_clean_text(item.get('source_text')) or '').casefold()}"]
            for change in item.get("changes") or []:
                for target in targets:
                    request_atoms.add((str(change), target.partition(":")[0], target))
    request_item_count = len(request_atoms)

    schedule = arguments.get("created_task_schedule")
    owners: set[tuple[str, str]] = set()

    def collect_owner_refs(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for temp_id in value.get("target_temp_ids") or []:
            cleaned = _clean_text(temp_id)
            if cleaned:
                owners.add(("temporary", cleaned))
        for task_id in value.get("target_task_ids") or []:
            if isinstance(task_id, int) and not isinstance(task_id, bool):
                owners.add(("task", str(task_id)))

    if isinstance(schedule, dict):
        collect_owner_refs(schedule)
        groups = schedule.get("schedule_groups")
        if isinstance(groups, list):
            for group in groups:
                collect_owner_refs(group)
    recurrence = arguments.get("recurring_schedule")
    unaddressed_recurrence_count = 0
    if isinstance(recurrence, dict):
        recurrence_temp_id = _clean_text(
            _first_present(recurrence, "target_temp_id", "targetTempId")
        )
        if recurrence_temp_id:
            owners.add(("temporary", recurrence_temp_id))
        else:
            unaddressed_recurrence_count = 1
    return {
        "structural_operations": structural_count,
        "request_items": request_item_count,
        "schedule_owners": len(owners) + unaddressed_recurrence_count,
    }


def _record_task_plan_coverage(
    arguments: Mapping[str, Any],
    *,
    tool_result_store: dict[str, Any] | None,
    original_user_request: str | None,
) -> str | None:
    """Conserve intent across every schema-valid same-request authoring attempt.

    Repair attempts are where clause loss most often occurs: the provider fixes
    one invalid window by deleting an inconvenient task.  Waiting until a plan
    compiles before retaining its coverage makes that destructive repair look
    like the first source of truth.  Keep the maximum typed ledger from every
    validated submission; sparse proposal revisions remain explicit deltas.
    """

    if not isinstance(tool_result_store, dict):
        return None
    coverage = _task_plan_intent_coverage(arguments)
    if not (coverage["structural_operations"] or coverage["schedule_owners"]):
        return None
    request_key = (_clean_text(original_user_request) or "").casefold()
    coverage_record = tool_result_store.get(_TASK_PLAN_COVERAGE_STORE_KEY)
    prior_coverage = (
        coverage_record.get("coverage")
        if isinstance(coverage_record, dict)
        and coverage_record.get("request_key") == request_key
        and isinstance(coverage_record.get("coverage"), dict)
        else None
    )
    is_sparse_revision = bool(
        _clean_text(arguments.get("revision_of_proposal_id"))
        or isinstance(arguments.get("recurring_schedule_updates"), dict)
    )
    if isinstance(prior_coverage, dict) and not is_sparse_revision:
        regressions = {
            key: (int(prior_coverage.get(key, 0)), int(coverage.get(key, 0)))
            for key in coverage
            if int(coverage.get(key, 0)) < int(prior_coverage.get(key, 0))
        }
        if regressions:
            return "; ".join(
                f"{key} fell from {before} to {after}"
                for key, (before, after) in regressions.items()
            )
    tool_result_store[_TASK_PLAN_COVERAGE_STORE_KEY] = {
        "request_key": request_key,
        "coverage": {
            key: max(
                int(coverage.get(key, 0)),
                int(prior_coverage.get(key, 0))
                if isinstance(prior_coverage, dict) and not is_sparse_revision
                else 0,
            )
            for key in coverage
        },
    }
    return None


def task_plan_coverage_checkpoint(
    tool_result_store: dict[str, Any] | None,
) -> object:
    """Capture the transactional intent ledger before one authoring attempt."""

    if not isinstance(tool_result_store, dict):
        return None
    if _TASK_PLAN_COVERAGE_STORE_KEY not in tool_result_store:
        return _MISSING_TASK_PLAN_COVERAGE
    return copy.deepcopy(tool_result_store[_TASK_PLAN_COVERAGE_STORE_KEY])


def restore_task_plan_coverage(
    tool_result_store: dict[str, Any] | None,
    checkpoint: object,
) -> None:
    """Roll back coverage when downstream scheduling compilation rejects a draft."""

    if not isinstance(tool_result_store, dict):
        return
    if checkpoint is _MISSING_TASK_PLAN_COVERAGE:
        tool_result_store.pop(_TASK_PLAN_COVERAGE_STORE_KEY, None)
        return
    if checkpoint is None:
        return
    tool_result_store[_TASK_PLAN_COVERAGE_STORE_KEY] = copy.deepcopy(checkpoint)


PREFERENCE_VALUE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "preferenceText": {"type": ["string", "null"]},
        "sourceText": {"type": ["string", "null"]},
        "extractionKind": {"type": ["string", "null"]},
        "constraintEligible": {"type": ["boolean", "null"]},
        "rule": {"type": ["string", "null"]},
        "recurrence": {"type": ["string", "null"]},
        "daysOfWeek": {
            "type": ["array", "null"],
            "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        },
        "startTime": {"type": ["string", "null"]},
        "endTime": {"type": ["string", "null"]},
        "timezone": {"type": ["string", "null"]},
        "interTaskGapMinutes": {"type": ["integer", "null"]},
        "preferredInterTaskGapMinutes": {"type": ["integer", "null"]},
        "minimumTaskDurationMinutes": {"type": ["integer", "null"]},
        "shorterTaskGapMinutes": {"type": ["integer", "null"]},
        "appliesToAllTasks": {
            "type": ["boolean", "null"],
            "description": (
                "True only when the user explicitly says the gap applies to every "
                "task or regardless of task duration."
            ),
        },
    },
}


REQUEST_SCHEDULING_CONSTRAINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "unavailable_interval",
                "allowed_window",
                "exact_start",
                "task_order",
                "minimum_gap",
                "contiguous",
            ],
            "description": (
                "Executable constraint kind: a blocked interval, an allowed task window, "
                "an exact task start, a before/after dependency, a required gap, or a "
                "requirement that one task remain contiguous."
            ),
        },
        "hard": {
            "type": "boolean",
            "description": "True when violating this instruction would make the proposal invalid.",
        },
        "label": {"type": "string", "description": "Short user-facing label."},
        "source_text": {
            "type": "string",
            "description": "Smallest user-request fragment supporting the constraint.",
        },
        "task_ids": {"type": "array", "items": {"type": "integer"}},
        "target_temp_ids": {"type": "array", "items": {"type": "string"}},
        "start_at": {"type": ["string", "null"]},
        "end_at": {"type": ["string", "null"]},
        "before_task_id": {"type": ["integer", "null"]},
        "after_task_id": {"type": ["integer", "null"]},
        "before_temp_id": {"type": ["string", "null"]},
        "after_temp_id": {"type": ["string", "null"]},
        "minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
        "recurrence": {
            "type": ["string", "null"],
            "enum": ["once", "daily", None],
            "description": (
                "Use daily when an unavailable interval or allowed window repeats on each "
                "applicable day in the scheduling window."
            ),
        },
        "days_of_week": {
            "type": ["array", "null"],
            "items": {
                "type": "string",
                "enum": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                ],
            },
        },
    },
    "required": ["kind", "hard", "label", "source_text"],
}


def _validate_planner_handoff_semantics(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Validate Planner transport invariants without controlling agent behavior."""

    parsed: dict[str, datetime] = {}
    for field_name in ("window_start", "window_end"):
        raw_value = str(arguments.get(field_name) or "").strip()
        try:
            value = datetime.fromisoformat(raw_value)
        except ValueError as error:
            raise ToolSemanticValidationError(
                f"{field_name} must be an ISO local date-time.",
                affected_paths=[field_name],
                repair_guidance=(
                    "Retry this tool call with an explicit naive local date-time such as "
                    "2026-07-21T20:00:00. Do not add Z or a numeric UTC offset."
                ),
            ) from error
        if value.tzinfo is not None:
            raise ToolSemanticValidationError(
                f"{field_name} must use naive local wall time.",
                affected_paths=[field_name],
                repair_guidance=(
                    "Remove the timezone suffix from the tool argument. The server supplies "
                    "the authoritative workspace timezone separately."
                ),
            )
        parsed[field_name] = value

    if parsed["window_end"] <= parsed["window_start"]:
        raise ToolSemanticValidationError(
            "Planner window_end must be after window_start.",
            affected_paths=["window_start", "window_end"],
            repair_guidance=(
                "Repair the tool arguments and retry in this same conversation. If the user "
                "gave one desired start time for an existing scheduled task, first use "
                "get_task_schedule to preserve its duration, and give Planner a positive "
                "search window rather than equal start/end values."
            ),
        )
    return arguments


KERNEL_ONLY_TOOL_SPECS: list[KernelToolSpec] = [
    KernelToolSpec(
        name="list_calendar_blocks",
        description=(
            "Read the accepted calendar blocks that overlap a local date/time range. "
            "Use this before task search when the user refers to scheduled work by date, "
            "time, or calendar position rather than by task title. The returned task IDs "
            "may be passed together to Planner with scope_source=calendar_query. Returned "
            "calendar-block, recurrence-series, and recurrence-occurrence references are "
            "authoritative mutation targets. Use the series reference only when the request "
            "clearly applies to the repeating series, and the occurrence reference only when "
            "it clearly applies to one dated occurrence; ask for clarification when scope is "
            "not established."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start_at": {"type": "string"},
                "end_at": {"type": "string"},
                "task_ids": {"type": "array", "items": {"type": "integer"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["start_at", "end_at"],
        },
        output_schema=_output_schema(
            properties={
                "blocks": {"type": "array", "items": {"type": "object"}},
                "task_ids": {"type": "array", "items": {"type": "integer"}},
                "count": {"type": "integer"},
            }
        ),
        read_only=True,
        result_kind="calendar_context",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="get_task_schedule",
        description=(
            "Read all accepted schedule blocks and persisted recurrence identity for exact "
            "task IDs. Use this before changing an existing task's schedule or recurrence when "
            "the request is not limited to one known date. The returned block, recurrence-series, "
            "recurrence-occurrence, revision, and rule fields are authoritative. Preserve the "
            "existing cadence and omitted rule fields unless the user explicitly changes them."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "maxItems": 100,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["task_ids"],
        },
        output_schema=_output_schema(
            properties={
                "blocks": {"type": "array", "items": {"type": "object"}},
                "task_ids": {"type": "array", "items": {"type": "integer"}},
                "recurrence_series_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "recurrence_occurrence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "count": {"type": "integer"},
            }
        ),
        read_only=True,
        result_kind="task_schedule_context",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="query_preferences",
        description=(
            "Read only effective hard constraints and soft preferences relevant to a "
            "bounded task/category and local date-time window. Explicit current-turn "
            "constraints outrank remembered preferences. This never writes preference memory."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "maxItems": 100,
                },
                "category_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "maxItems": 32,
                },
                "task_titles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 100,
                },
                "category_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 32,
                },
                "window_start": {"type": "string"},
                "window_end": {"type": "string"},
                "query": {"type": "string"},
                "max_soft_preferences": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 16,
                },
                "max_observations": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 12,
                },
            },
            "required": ["window_start", "window_end"],
        },
        output_schema=_output_schema(
            properties={
                "hardConstraints": {"type": "array", "items": {"type": "object"}},
                "softPreferences": {"type": "array", "items": {"type": "object"}},
                "observations": {"type": "array", "items": {"type": "object"}},
                "contextVersion": {"type": "string"},
            }
        ),
        read_only=True,
        result_kind="preference_context",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="list_remembered_preferences",
        description=(
            "List the current user's exact remembered preference records for management. "
            "Returns stable preference IDs, revisions, active status, meaning, and linked "
            "constraints. Use this immediately before proposing disable, restore, delete, "
            "or replacement actions. This is read-only and includes inactive records by "
            "default."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "include_inactive": {"type": "boolean"},
                "query": {"type": "string", "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
        output_schema=_output_schema(
            properties={
                "preferences": {"type": "array", "items": {"type": "object"}},
                "count": {"type": "integer"},
                "active_count": {"type": "integer"},
                "inactive_count": {"type": "integer"},
            }
        ),
        read_only=True,
        result_kind="remembered_preference_inventory",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="analyze_preference_cleanup_candidates",
        description=(
            "Analyze the complete active saved-preference inventory for high-confidence "
            "superseded, contradictory, duplicate, and redundant records. Set cleanup_scope "
            "to contradictions for conflicting/superseded values, overlaps for duplicate or "
            "redundant copies, or all only when the user requests both. Returns a complete "
            "scope-specific batch, fingerprint, canonical keepers, and ambiguous tensions. "
            "Never broaden a contradictions-only request into duplicate cleanup."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cleanup_scope": {
                    "type": "string",
                    "enum": ["contradictions", "overlaps", "all"],
                },
            },
            "required": [],
        },
        output_schema=_output_schema(
            properties={
                "candidates": {"type": "array", "items": {"type": "object"}},
                "candidate_count": {"type": "integer"},
                "candidate_set_fingerprint": {"type": "string"},
                "cleanup_scope": {"type": "string"},
                "other_cleanup_candidates": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "ambiguous": {"type": "array", "items": {"type": "object"}},
                "ambiguous_count": {"type": "integer"},
                "kept_preferences": {"type": "array", "items": {"type": "object"}},
                "active_inventory_count": {"type": "integer"},
                "complete": {"type": "boolean"},
            }
        ),
        read_only=True,
        result_kind="preference_cleanup_candidates",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="read_planner_context",
        description=(
            "Read the compact scheduling task, calendar window, commitments, "
            "preferences, and policy submitted to this owned Planner run."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        output_schema=_output_schema(
            properties={
                "working_set": {"type": "object"},
                "calendar": {"type": "object"},
                "preferences": {"type": "object"},
                "default_scheduling_policy": {"type": "object"},
                "operation_mode": {"type": "string"},
            }
        ),
        read_only=True,
        result_kind="planner_context",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="evaluate_schedule_plan",
        description=(
            "Evaluate the owned Planner's ordered task directives with the "
            "deterministic scheduling solver and return placements and conflicts."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_directives": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "task_id": {"type": "integer"},
                            "action": {
                                "type": "string",
                                "enum": ["schedule", "skip"],
                            },
                            "allocated_minutes": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1440,
                            },
                            "gap_after_minutes": {
                                "type": ["integer", "null"],
                                "minimum": 0,
                                "maximum": 1440,
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "task_id",
                            "action",
                            "allocated_minutes",
                            "rationale",
                        ],
                    },
                }
            },
            "required": ["task_directives"],
        },
        output_schema=_output_schema(
            properties={
                "normalization": {"type": "object"},
                "solver_status": {"type": "string"},
                "placements": {"type": "array", "items": {"type": "object"}},
                "conflicts": {"type": "array", "items": {"type": "object"}},
                "model_instruction": {"type": "string"},
            }
        ),
        read_only=True,
        result_kind="schedule_evaluation",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="handoff_to_planner",
        description=(
            "Ask Planner to evaluate a scheduling request and return its result to this same "
            "free-form tool loop. Planner success, repair guidance, or a genuine missing user "
            "decision is an observation for you to handle; this call does not end your turn. "
            "This handoff accepts persisted existing task IDs only. For tasks being created in "
            "the current proposal, do not call this tool; put their temp_ids and scheduling "
            "windows in submit_task_tree_plan.created_task_schedule so the server invokes "
            "Planner during compilation. If one request contains both unscheduled persisted and "
            "new tasks, include the existing IDs as target_task_ids in those same schedule groups "
            "so one solver pass coordinates all placements. Use this handoff separately only when "
            "an existing accepted block must be rescheduled or removed. "
            "Use only when the user asks to schedule. Translate relative time using the supplied "
            "authoritative local date/time and return an explicit naive local-wall-time window. "
            "Every start/end timestamp in the handoff and request_constraints must be naive local "
            "wall time with no Z suffix or numeric offset. "
            "A user-supplied start-to-end range already supplies duration and exact placement; "
            "preserve that range and do not ask for duration or broader availability. "
            "When the same request also changes metadata on a scheduled task, include task_updates "
            "so one review and approval can apply the task and calendar changes together. "
            "Translate every explicit hard instruction into request_constraints; never leave "
            "breaks, unavailable times, exact anchors, ordering, required gaps, allowed windows, "
            "or contiguity requirements only in the prose instruction. Constraints do not need "
            "a corresponding task: omit task_ids for request-wide commitments, and use daily "
            "recurrence with optional weekdays for repeating clock windows. For an existing "
            "recurring task mutation, target_scope is mandatory: use series for every/all "
            "occurrences and occurrence for one dated/next occurrence. Series scope also requires "
            "the persisted recurrence returned by get_task_schedule; never represent a series "
            "edit as one calendar-block update."
            " Conflict relocation for a newly-created recurrence belongs in "
            "submit_task_tree_plan.recurring_schedule.conflict_relocation; do not encode "
            "a move/relocate directive as a request_constraint or hand off its blockers "
            "separately."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "integer"}},
                "instruction": {"type": "string"},
                "window_start": {"type": "string"},
                "window_end": {"type": "string"},
                "operation_mode": {"type": "string", "enum": ["create", "reschedule", "remove"]},
                "scope_source": {
                    "type": "string",
                    "enum": ["attached_working_set", "explicit_named_task", "calendar_query"],
                },
                "target_scope": {
                    "type": ["string", "null"],
                    "enum": ["task", "occurrence", "series", None],
                    "description": (
                        "Mutation cardinality. For an existing recurring task, use series for "
                        "every/all occurrences and occurrence for one specific dated occurrence."
                    ),
                },
                "task_updates": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "task_id": {"type": "integer"},
                            "fields": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": ["string", "null"]},
                                    "notes": {"type": ["string", "null"]},
                                    "status": {"type": ["string", "null"]},
                                    "priority": {"type": ["string", "null"]},
                                    "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
                                    "deadline_at": {"type": ["string", "null"]},
                                    "time_estimate_minutes": {"type": ["integer", "null"]},
                                    "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                                    "is_pinned": {"type": ["boolean", "null"]},
                                    "node_type": {"type": ["string", "null"]},
                                },
                            },
                        },
                        "required": ["task_id", "fields"],
                    },
                },
                "request_constraints": {
                    "type": "array",
                    "items": REQUEST_SCHEDULING_CONSTRAINT_SCHEMA,
                    "description": (
                        "Typed constraints for this request. They outrank saved preferences and "
                        "are enforced and validated before a proposal can be approved."
                    ),
                },
                "recurrence": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "frequency": {"type": "string", "enum": ["daily", "weekly"]},
                        "days_of_week": {
                            "type": ["array", "null"],
                            "items": {
                                "type": "string",
                                "enum": [
                                    "monday",
                                    "tuesday",
                                    "wednesday",
                                    "thursday",
                                    "friday",
                                    "saturday",
                                    "sunday",
                                ],
                            },
                            "description": "Required for weekly recurrence unless start_date supplies the intended weekday.",
                        },
                        "start_date": {"type": "string"},
                        "end_date": {"type": ["string", "null"]},
                        "occurrence_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 366},
                        "end_mode": {
                            "type": ["string", "null"],
                            "enum": ["never", "until", "count", None],
                            "description": "Use never for an ongoing or indefinite recurrence.",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1440,
                            "description": (
                                "Exact duration of each recurrence occurrence. When the current "
                                "request states a duration such as 'exactly 60 minutes', copy that "
                                "integer here and do not ask for duration again."
                            ),
                        },
                        "preferred_start_time": {
                            "type": ["string", "null"],
                            "description": (
                                "Optional user-requested local anchor. Leave null when the user "
                                "did not name a time; Planner will choose using preferences, "
                                "calendar context, and common-sense scheduling heuristics."
                            ),
                        },
                        "allowed_start_time": {"type": ["string", "null"]},
                        "allowed_end_time": {"type": ["string", "null"]},
                        "gap_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
                        "timezone": {"type": ["string", "null"]},
                        "strict": {
                            "type": ["boolean", "null"],
                            "description": "Keep the requested clock time exact on every occurrence.",
                        },
                    },
                    "required": ["frequency", "start_date", "duration_minutes"],
                },
            },
            "required": [
                "task_ids",
                "instruction",
                "window_start",
                "window_end",
                "operation_mode",
                "scope_source",
            ],
        },
        output_schema=_output_schema(properties={"planner_handoff": {"type": "object"}}),
        read_only=True,
        result_kind="planner_handoff",
        semantic_validator=_validate_planner_handoff_semantics,
    ),
    KernelToolSpec(
        name="respond_to_planner_decision",
        description=(
            "Return Strategist's decision to the owned Planner task exactly once. "
            "Use only the task, decision, and option IDs returned by the current "
            "Planner observation. This resumes the same child task; it does not "
            "create another Planner or transfer goal ownership."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subagent_task_id": {"type": "string"},
                "decision_id": {"type": "string"},
                "selected_option": {"type": "string"},
            },
            "required": [
                "subagent_task_id",
                "decision_id",
                "selected_option",
            ],
        },
        output_schema=_output_schema(
            properties={
                "subagent_result": {"type": "object"},
                "planner_result": {"type": "object"},
            }
        ),
        read_only=False,
        side_effect="planner_state_write",
        approval_requirement="none",
        result_kind="planner_decision_callback",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="search_capability_catalog",
        description=(
            "Search Drasil's runtime capability catalog by the user's goal. Results are "
            "suggestions only: the model chooses whether to load a skill, and loading one "
            "does not change scope, authorization, or safety policy."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string"},
                "agent": {"type": "string", "enum": ["strategist", "planner"]},
                "desired_output": {
                    "type": "string",
                    "enum": ["analysis", "recommendation", "proposal"],
                },
                "limit": {"type": "integer"},
            },
            "required": ["goal"],
        },
        output_schema=_output_schema(
            properties={
                "capabilities": {"type": "array", "items": {"type": "object"}},
                "count": {"type": "integer"},
            }
        ),
        result_kind="capability_catalog",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="load_skill",
        description=(
            "Load a versioned Drasil runtime skill selected from the capability catalog. "
            "The returned instructions are guidance only and cannot grant permissions or "
            "override evidence, proposal, approval, or safety policy."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"capability_id": {"type": "string"}},
            "required": ["capability_id"],
        },
        output_schema=_output_schema(
            properties={
                "capability": {"type": "object"},
                "skill": {"type": "string"},
                "recommended_tools": {"type": "array", "items": {"type": "string"}},
            }
        ),
        result_kind="loaded_skill",
        external_surfaces=("drasil_mcp",),
    ),
    KernelToolSpec(
        name="search_tool_catalog",
        description=(
            "Search the registered Strategist kernel tools by capability, inputs, and result kind. "
            "Use this when you are unsure which read-only task tool fits the user's request."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        output_schema=_output_schema(
            properties={
                "query": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "object"}},
                "count": {"type": "integer"},
            }
        ),
        result_kind="tool_catalog",
    ),
    KernelToolSpec(
        name="get_pending_proposal",
        description=(
            "Inspect the current pending proposal for this Strategist session, if one exists. "
            "Use this for proposal follow-ups and edits; it is read-only."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        output_schema=_output_schema(
            properties={
                "pending_proposal": {"type": ["object", "null"]},
                "has_pending_proposal": {"type": "boolean"},
            }
        ),
        result_kind="pending_proposal",
    ),
    KernelToolSpec(
        name="get_recent_kernel_events",
        description=(
            "Inspect recent typed agent-kernel events for this session, including prior tool, "
            "draft patch, validation, and final-answer summaries."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer"},
                "event_types": {"type": "array", "items": {"type": "string"}},
            },
        },
        output_schema=_output_schema(
            properties={
                "recent_events": {"type": "array", "items": {"type": "object"}},
                "count": {"type": "integer"},
            }
        ),
        result_kind="recent_kernel_events",
    ),
    KernelToolSpec(
        name="get_session_memory",
        description=(
            "Inspect compact event-log-derived session memory, including active objective, "
            "working set, target branch, pending proposal, recent decisions, and unresolved asks."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        output_schema=_output_schema(
            properties={
                "session_memory": {"type": "object"},
            }
        ),
        result_kind="session_memory",
    ),
    KernelToolSpec(
        name="get_context_surfaces",
        description=(
            "Inspect the current UI/session context surfaces: active category, selected task, "
            "staged task ids, working set, pending proposal summary, session memory, and recent event summary."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        output_schema=_output_schema(
            properties={
                "active_category": {"type": ["object", "null"]},
                "focus_task": {"type": ["object", "null"]},
                "staged_task_ids": {"type": "array", "items": {"type": "integer"}},
                "working_set": {"type": "array", "items": {"type": "object"}},
                "pending_proposal": {"type": ["object", "null"]},
                "session_memory": {"type": "object"},
                "recent_events": {"type": "array", "items": {"type": "object"}},
            }
        ),
        result_kind="context_surfaces",
    ),
    KernelToolSpec(
        name="get_tool_result_by_handle",
        description=(
            "Read a bounded same-turn result or durable current-goal evidence observation "
            "from an opaque handle. Handles are session/goal scoped and read-only. For native "
            "simulation results, select summary, conflicts, alternatives, or final_facts."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tool_result_handle": {"type": "string"},
                "section": {
                    "type": "string",
                    "enum": ["summary", "conflicts", "alternatives", "final_facts"],
                },
                "offset": {"type": "integer", "minimum": 0, "maximum": 2048},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["tool_result_handle"],
        },
        output_schema=_output_schema(
            properties={
                "tool_result_handle": {"type": "string"},
                "tool_result": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                "found": {"type": "boolean"},
                "simulation_page": {"type": ["object", "null"]},
            }
        ),
        result_kind="tool_result",
    ),
    KernelToolSpec(
        name="propose_metadata_update",
        description=(
            "Compile a typed reviewable metadata proposal draft for existing task metadata. "
            "Use this for semantic task edits such as deadline, priority, status, estimate, "
            "title, notes, tags, pin state, or node type after you have identified the task. "
            "For different updates across multiple existing tasks, pass target_updates with "
            "an exact target_task_id and updates object for each task; this produces one "
            "reviewable bulk metadata proposal without using tree operations. "
            "Also use it when the user selects or accepts a concrete metadata value from "
            "the prior task-edit discussion. "
            "Do not use this for recommendation-only requests that merely ask you to find "
            "or compare a possible value; answer those directly until the user selects one. "
            "This does not apply changes; it returns proposal arguments for harness validation."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target_task_id": {"type": ["integer", "null"]},
                "target_task_title": {"type": ["string", "null"]},
                "updates": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": ["string", "null"]},
                        "notes": {"type": ["string", "null"]},
                        "status": {"type": ["string", "null"]},
                        "priority": {"type": ["string", "null"]},
                        "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
                        "deadline_at": {
                            "type": ["string", "null"],
                            "description": (
                                "Task due date as YYYY-MM-DD. A supplied ISO date-time is "
                                "coerced to its calendar date."
                            ),
                        },
                        "time_estimate_minutes": {"type": ["integer", "null"]},
                        "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                        "is_pinned": {"type": ["boolean", "null"]},
                        "node_type": {"type": ["string", "null"]},
                    },
                },
                "target_updates": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "target_task_id": {"type": "integer"},
                            "target_task_title": {"type": ["string", "null"]},
                            "updates": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": ["string", "null"]},
                                    "notes": {"type": ["string", "null"]},
                                    "status": {"type": ["string", "null"]},
                                    "priority": {"type": ["string", "null"]},
                                    "planned_date": {"type": ["string", "null"], "description": "Planned calendar day YYYY-MM-DD, no set time; independent of the deadline."},
                                    "deadline_at": {
                                        "type": ["string", "null"],
                                        "description": (
                                            "Task due date as YYYY-MM-DD. A supplied ISO "
                                            "date-time is coerced to its calendar date."
                                        ),
                                    },
                                    "time_estimate_minutes": {"type": ["integer", "null"]},
                                    "tags": {
                                        "type": ["array", "null"],
                                        "items": {"type": "string"},
                                    },
                                    "is_pinned": {"type": ["boolean", "null"]},
                                    "node_type": {"type": ["string", "null"]},
                                },
                            },
                        },
                        "required": ["target_task_id", "updates"],
                    },
                },
                "rationale": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
            },
        },
        output_schema=_output_schema(
            properties={
                "proposal_kind": {"type": "string"},
                "metadata_proposal": {"type": "object"},
                "compiled": {"type": "boolean"},
            }
        ),
        read_only=False,
        result_kind="proposal_draft",
    ),
    KernelToolSpec(
        name="propose_tree_operation",
        description=(
            "Compile typed reviewable tree operation proposal arguments. Use this for task "
            "creation, deletion, move/reparent/reorder, restructuring, grouping, or mixed "
            "tree-plus-metadata work, including when the user selects or accepts a concrete "
            "tree operation from the prior task-edit discussion. This does not apply changes; "
            "it returns proposal arguments for typed batch validation. Keep user-visible summary "
            "and rationale free of internal contract or implementation names. For a top-level "
            "task create, choose the best workspace from the available category catalog unless "
            "the user named one; the active workspace is only a tie-breaker. Pass the chosen "
            "target_category_id or exact target_category_name and one create_node "
            "operation with title plus optional deadline_at, notes, priority, status, estimate, "
            "or tags. Omitting target_task_id/title and parent fields is an explicit top-level "
            "choice; current UI focus or an earlier conversation goal must not become its parent. "
            "For a new top-level parent plus new child subtasks, pass temp_id on the "
            "parent create_node and parent_temp_id on each child create_node. For direct-parent "
            "subtasks, pass parent_id or parent_task_title on each "
            "create_node. For corrections, pass move_subtree operations with source_task_title "
            "or node_id and the most specific destination_parent_task_title/id. Do not use this "
            "for recommendation-only requests until the user selects or accepts one concrete change. "
            "When one request creates a task and schedules it as a daily or weekly recurrence, "
            "include recurring_schedule in this same proposal. Give the created task a temp_id, "
            "reference it with target_temp_id, preserve the stated duration, and use a broad "
            "time window when the user chose morning/afternoon/evening so individual conflicting "
            "days can move within that period. When one request creates one or more tasks and "
            "schedules them once, include created_task_schedule in this same proposal. Reference "
            "each create_node by temp_id and decide tree parentage independently from calendar "
            "placement. When the user asks to schedule multiple items, keep every item in the "
            "schedule ownership set even when its exact duration or slot was omitted; Planner uses "
            "saved history, preferences, occupancy, and bounded estimates. Fixed commitments must "
            "be represented as tasks with calendar ownership, not only as free-floating constraints. "
            "Targets that share one placement window may use the legacy top-level fields; "
            "targets with different windows must use schedule_groups. Use offset-free ISO local "
            "wall time. Put existing unscheduled task IDs in target_task_ids whether or not new "
            "tasks are present; an existing-only schedule may use no structural operations. "
            "Represent fixed appointments with an exact_start "
            "request_constraint targeting the relevant temp ID; keep the surrounding group "
            "window broad enough for its flexible siblings."
            " To revise a pending branch proposal, pass revision_of_proposal_id and only the "
            "changed recurring fields in recurring_schedule_updates; the harness preserves "
            "the proposal's existing task operations and unaffected recurrence fields. "
            "Conflict blockers are affected existing blocks, not new recurrence targets."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "proposal_kind": {"type": ["string", "null"]},
                "revision_of_proposal_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Stable pending proposal id when revising an existing branch proposal."
                    ),
                },
                "replace_structural_operations": {
                    "type": ["boolean", "null"],
                    "description": (
                        "For a revision only: explicitly replace the pending proposal's task "
                        "operations. Defaults to false so unrelated operations cannot disappear."
                    ),
                },
                "replace_recurring_schedule": {
                    "type": ["boolean", "null"],
                    "description": (
                        "For a revision only: explicitly replace the full recurrence contract. "
                        "Defaults to false; use recurring_schedule_updates for a narrow edit."
                    ),
                },
                "target_task_id": {"type": ["integer", "null"]},
                "target_task_title": {"type": ["string", "null"]},
                "target_category_id": {"type": ["integer", "null"]},
                "target_category_name": {"type": ["string", "null"]},
                "request_items": {
                    "type": "array",
                    "minItems": 1,
                    "items": REQUEST_ITEM_INPUT_SCHEMA,
                    "description": (
                        "Clause inventory for the complete current request. Add one item per "
                        "explicit requested action and bind it to temp IDs or existing task IDs. "
                        "For compound scheduling, include schedule in changes for every item the "
                        "user expects placed on the calendar, including fixed commitments. Use "
                        "schedule_group_index when multiple windows make ownership ambiguous."
                    ),
                },
                "structural_operations": {
                    "type": "array",
                    "items": STRUCTURAL_OPERATION_INPUT_SCHEMA,
                },
                "recurring_schedule": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "target_temp_id": {"type": ["string", "null"]},
                        "frequency": {"type": "string", "enum": ["daily", "weekly"]},
                        "days_of_week": {
                            "type": ["array", "null"],
                            "items": {
                                "type": "string",
                                "enum": [
                                    "monday",
                                    "tuesday",
                                    "wednesday",
                                    "thursday",
                                    "friday",
                                    "saturday",
                                    "sunday",
                                ],
                            },
                            "description": "Weekdays included by a weekly recurrence.",
                        },
                        "start_date": {"type": "string"},
                        "end_date": {"type": ["string", "null"]},
                        "occurrence_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 366},
                        "end_mode": {
                            "type": ["string", "null"],
                            "enum": ["never", "until", "count", None],
                            "description": (
                                "Use never for an open-ended recurrence when the user gives no "
                                "end date or occurrence count."
                            ),
                        },
                        "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                        "preferred_start_time": {
                            "type": ["string", "null"],
                            "description": (
                                "Optional user-requested local anchor. Leave null when the user "
                                "did not name a time; Planner chooses a suitable opening."
                            ),
                        },
                        "allowed_start_time": {
                            "type": ["string", "null"],
                            "description": "Earliest acceptable local time for per-day conflict resolution.",
                        },
                        "allowed_end_time": {
                            "type": ["string", "null"],
                            "description": "Latest acceptable local time for per-day conflict resolution.",
                        },
                        "gap_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
                        "timezone": {"type": ["string", "null"]},
                        "strict": {
                            "type": ["boolean", "null"],
                            "description": "Keep the requested clock time exact on every occurrence.",
                        },
                        "conflict_relocation": {
                            "type": ["string", "null"],
                            "enum": ["before_incoming", "after_incoming", None],
                            "description": (
                                "Where flexible existing conflict blockers should move relative "
                                "to the incoming fixed-time occurrence."
                            ),
                        },
                    },
                    "required": [
                        "frequency",
                        "start_date",
                        "duration_minutes"
                    ],
                },
                "recurring_schedule_updates": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "description": (
                        "Changed recurrence fields for revision_of_proposal_id. Unmentioned "
                        "fields and the existing structural operations are preserved."
                    ),
                    "properties": {
                        "frequency": {"type": ["string", "null"], "enum": ["daily", "weekly", None]},
                        "days_of_week": {
                            "type": ["array", "null"],
                            "items": {
                                "type": "string",
                                "enum": [
                                    "monday",
                                    "tuesday",
                                    "wednesday",
                                    "thursday",
                                    "friday",
                                    "saturday",
                                    "sunday",
                                ],
                            },
                        },
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "occurrence_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 366},
                        "end_mode": {"type": ["string", "null"], "enum": ["never", "until", "count", None]},
                        "duration_minutes": {"type": ["integer", "null"], "minimum": 1, "maximum": 1440},
                        "preferred_start_time": {"type": ["string", "null"]},
                        "allowed_start_time": {"type": ["string", "null"]},
                        "allowed_end_time": {"type": ["string", "null"]},
                        "gap_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
                        "timezone": {"type": ["string", "null"]},
                        "strict": {"type": ["boolean", "null"]},
                        "conflict_relocation": {
                            "type": ["string", "null"],
                            "enum": ["before_incoming", "after_incoming", None],
                        },
                    },
                },
                "created_task_schedule": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "description": (
                        "One approval-level collection of independent one-time scheduling groups. "
                        "Use the direct fields for one shared window or schedule_groups for "
                        "multiple windows. Unscheduled created nodes remain only in structural_operations."
                    ),
                    "properties": {
                        "target_temp_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Created task temp_ids in the requested scheduling order.",
                        },
                        "target_task_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Existing task IDs solved with created targets in one pass."
                            ),
                        },
                        "instruction": {"type": "string"},
                        "window_start": {
                            "type": "string",
                            "description": "Offset-free ISO local wall time.",
                        },
                        "window_end": {
                            "type": "string",
                            "description": "Offset-free ISO local wall time.",
                        },
                        "slot_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
                        "request_constraints": {
                            "type": "array",
                            "items": CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA,
                        },
                        "schedule_groups": {
                            "type": "array",
                            "minItems": 1,
                            "items": CREATED_TASK_SCHEDULE_GROUP_INPUT_SCHEMA,
                        },
                    },
                    "required": [],
                },
                "rationale": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
            },
            "required": ["request_items"],
        },
        output_schema=_output_schema(
            properties={
                "proposal_kind": {"type": "string"},
                "branch_proposal": {"type": "object"},
                "compiled": {"type": "boolean"},
            }
        ),
        read_only=False,
        result_kind="proposal_draft",
    ),
    KernelToolSpec(
        name="propose_preference_memory",
        description=(
            "Compile an approval-gated durable scheduling preference memory proposal. "
            "Use this when the user asks you to remember a preference, or when the "
            "conversation clearly establishes a durable scheduling preference that should "
            "affect future planning. The model owns semantic judgment: infer the preference "
            "from natural language, but pass structured preference and constraint fields. "
            "Do not use this for one-off scheduling commands or temporary statements. "
            "If a hard rule lacks an exact enforceable window, ask a clarification instead "
            "or save only a soft preference. This does not save memory; it returns a "
            "reviewable proposal for user approval."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "preference": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": (
                                "Canonical preference content for review. For a qualitative "
                                "preference, preserve every user-supplied content word and its "
                                "order; remove only request framing and terminal punctuation. "
                                "Do not paraphrase, shorten, broaden, or add rationale."
                            ),
                        },
                        "domain": {
                            "type": ["string", "null"],
                            "description": "Usually scheduling, calendar, deep_work, energy, sleep, or focus.",
                        },
                        "preferenceType": {
                            "type": ["string", "null"],
                            "description": (
                                "An open-ended semantic key, not a fixed category list. "
                                "Use inter_task_gap for a gap between tasks, availability "
                                "for blocked time, or a concise stable key for other meanings. "
                                "Do not collapse a known executable meaning into schedule_style."
                            ),
                        },
                        "strength": {
                            "type": ["string", "null"],
                            "description": (
                                "soft, medium, strong, or hard. Use hard for explicit always, "
                                "must, never, or at-least rules."
                            ),
                        },
                        "enforcement": {
                            "type": ["string", "null"],
                            "enum": ["soft_default", "hard_minimum", None],
                            "description": (
                                "For inter-task gaps, use soft_default for usually/prefer and "
                                "hard_minimum for always/must/at-least language."
                            ),
                        },
                        "scopeType": {
                            "type": ["string", "null"],
                            "description": "global, category, task, or project.",
                        },
                        "scopeId": {"type": ["string", "null"]},
                        "confidence": {"type": ["number", "null"]},
                        "value": {
                            "type": ["object", "null"],
                            "additionalProperties": False,
                            "properties": copy.deepcopy(
                                PREFERENCE_VALUE_INPUT_SCHEMA["properties"]
                            ),
                            "description": (
                                "Structured preference value. Include preferenceText/sourceText "
                                "for soft preferences. For a usual gap between scheduled tasks, "
                                "include interTaskGapMinutes and enforcement=soft_default. For a "
                                "duration-dependent gap, also include minimumTaskDurationMinutes "
                                "and shorterTaskGapMinutes (usually 0), so shorter tasks can remain "
                                "back-to-back. Set appliesToAllTasks=true only when the user "
                                "explicitly removes any duration condition. If they only change "
                                "the gap amount, omit applicability fields so the server preserves "
                                "compatible conditions from the active preference. "
                                "always/minimum gap, use enforcement=hard_minimum; the compiler will "
                                "create the enforceable inter_task_gap constraint. For hard availability preferences, "
                                "include recurrence/startTime/endTime/daysOfWeek. For qualitative "
                                "preferenceText, copy every content word from the user in the same "
                                "order and omit only request framing and terminal punctuation."
                            ),
                        },
                    },
                    "required": ["description"],
                },
                "constraints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "description": {"type": ["string", "null"]},
                            "constraintType": {
                                "type": ["string", "null"],
                                "description": (
                                    "Use forbidden_window for unavailable time, or inter_task_gap "
                                    "for a hard minimum gap between scheduled tasks."
                                ),
                            },
                            "targetType": {
                                "type": ["string", "null"],
                                "description": "Usually global, category, or task.",
                            },
                            "targetId": {"type": ["string", "null"]},
                            "hard": {"type": ["boolean", "null"]},
                            "penaltyWeight": {"type": ["number", "null"]},
                            "confidence": {"type": ["number", "null"]},
                            "value": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": copy.deepcopy(
                                    PREFERENCE_VALUE_INPUT_SCHEMA["properties"]
                                ),
                                "description": (
                                    "Enforceable constraint payload, such as recurrence weekly, "
                                    "daysOfWeek, startTime HH:MM, endTime HH:MM, timezone local, and rule."
                                    " For inter_task_gap, include interTaskGapMinutes."
                                ),
                            },
                        },
                        "required": ["value"],
                    },
                },
                "memory_intent": {
                    "type": ["string", "null"],
                    "description": "explicit_request or inferred_preference.",
                },
                "source_text": {"type": ["string", "null"]},
                "rationale": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
                "validation_warnings": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "assumption_warnings": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
            },
            "required": ["preference"],
        },
        output_schema=_output_schema(
            properties={
                "proposal_kind": {"type": "string"},
                PREFERENCE_RESPONSE_KEY: {"type": "object"},
                "compiled": {"type": "boolean"},
            }
        ),
        read_only=False,
        result_kind="proposal_draft",
    ),
    KernelToolSpec(
        name="draft_change_set",
        description=(
            "Create or revise the current goal's canonical native ChangeSet draft. Supply only "
            "typed operations, constraints, preconditions, assumptions, and expected revisions. "
            "The server owns ChangeSet identity, trace identity, idempotency, status, simulation, "
            "approval, and commit authority. This tool never applies domain mutations."
        ),
        input_schema=strict_provider_schema(DraftChangeSetCommand),
        output_schema=strict_provider_schema(DraftChangeSetResult),
        input_model=DraftChangeSetCommand,
        output_model=DraftChangeSetResult,
        read_only=False,
        result_kind="native_change_set_draft",
        side_effect="draft_state_write",
        approval_requirement="required_before_commit",
        idempotency_behavior="server_tool_call_replay_safe",
    ),
    KernelToolSpec(
        name="simulate_change_set",
        description=(
            "Simulate the current native ChangeSet revision against fresh cross-domain facts. "
            "Supply only expected GoalState and ChangeSet revisions. Returns typed conflicts "
            "and exact alternatives without applying product-domain mutations."
        ),
        input_schema=strict_provider_schema(SimulateChangeSetCommand),
        output_schema=strict_provider_schema(ChangeSetSimulationResult),
        input_model=SimulateChangeSetCommand,
        output_model=ChangeSetSimulationResult,
        read_only=False,
        result_kind="native_change_set_simulation",
        side_effect="simulation_state_write",
        approval_requirement="none",
        idempotency_behavior="server_tool_call_replay_safe",
    ),
    KernelToolSpec(
        name="set_goal_transition",
        description=(
            "Classify the current user turn as new, continue, or correct. Supply only sparse "
            "semantic changes. For a new or corrected scheduling goal, the model owns whether "
            "the schedule is one-time or repeating; record repeating meaning in recurrence.rule "
            "instead of relying on prompt labels. The Agent Host binds goal identity, revisions, "
            "provenance, trace, and idempotency authority. This never mutates product-domain state."
        ),
        input_schema=strict_provider_schema(GoalTurnDecision),
        output_schema=_output_schema(
            properties={
                "goal_state": {"type": "object"},
                "persisted": {"type": "boolean"},
                "disposition": {
                    "type": "string",
                    "enum": ["new", "continue", "correct"],
                },
                "contextReset": {"type": "boolean"},
            }
        ),
        input_model=GoalTurnDecision,
        read_only=False,
        result_kind="goal_state",
        side_effect="goal_state_write",
        approval_requirement="none",
        idempotency_behavior="host_turn_replay_safe",
    ),
]


def _semantic_plan_tool_spec(
    *,
    source_name: str,
    name: str,
    description: str,
) -> KernelToolSpec:
    source = next(
        spec for spec in KERNEL_ONLY_TOOL_SPECS if spec.name == source_name
    )
    return replace(
        source,
        name=name,
        description=description,
        output_schema=_output_schema(
            properties={
                "compiled": {"type": "boolean"},
                "semantic_plan_input": {"type": "object"},
                "clarification": {"type": ["object", "null"]},
            }
        ),
        result_kind="semantic_plan_submission",
    )


SEMANTIC_PLAN_TOOL_SPECS: tuple[KernelToolSpec, ...] = (
    _semantic_plan_tool_spec(
        source_name="propose_metadata_update",
        name="submit_task_metadata_plan",
        description=(
            "Submit complete high-level metadata intent for one or more exact tasks. "
            "Use concrete task identity and changed values only. The server validates "
            "and compiles this semantic input into the goal's single ChangeSet; this "
            "tool never creates an approval identity or applies product data."
        ),
    ),
    _semantic_plan_tool_spec(
        source_name="propose_tree_operation",
        name="submit_task_tree_plan",
        description=(
            "Submit one complete high-level task-tree plan for create, move, archive, "
            "delete, decomposition, metadata, and related recurrence meaning. Preserve "
            "explicit temporary parent links and independent roots. The server compiles "
            "the plan into one ChangeSet and never applies it from this tool call. When "
            "new tasks are scheduled and the user supplied no duration, make reasonable "
            "low-risk estimates from task meaning and available user context instead of "
            "asking by default. Planner validates placement against calendar state and "
            "saved preferences and supplies a bounded estimate if one is omitted. For any "
            "initial schedule of unscheduled persisted tasks, with or without new tasks, put "
            "their IDs in created_task_schedule.target_task_ids; put new temp IDs in "
            "target_temp_ids so one solver pass coordinates the complete request. A "
            "schedule-only plan for persisted task IDs may omit structural_operations. Use a "
            "stored task estimate when present and otherwise let the compiler choose a bounded "
            "low-risk estimate instead of asking for duration by default. Use a "
            "clause-by-clause inventory before submission: every requested new task, named existing "
            "task, fixed commitment, date/window, and ordering relation must be represented in this "
            "one plan. Do not converge on only one convenient target from a compound request. Use a "
            "parent outcome with separate prerequisite and activity children when a future activity "
            "depends on a short availability check, reservation, or booking. Put only the explicitly "
            "timed prerequisite in created_task_schedule; keep a dependent activity unscheduled when "
            "the user says its placement will be chosen later, while preserving its stated deadline. "
            "Give an ordinary short prerequisite a reasonable 5–10 minute estimate. Use a "
            "fixed commitment as an unavailable interval only when it is already represented by an "
            "existing calendar item or the user clearly describes it as external availability. When "
            "the user asks to create or schedule the complete request, a fixed activity must own a "
            "task/create operation and calendar block just like the flexible activities. Use a "
            "separate Planner handoff only "
            "when an existing block must be rescheduled or removed."
        ),
    ),
    _semantic_plan_tool_spec(
        source_name="propose_preference_memory",
        name="submit_preference_plan",
        description=(
            "Submit one high-level durable-preference plan when the user explicitly "
            "wants a rule remembered. One-off reminders are task intent. The server "
            "compiles validated preference meaning into the goal's single ChangeSet; "
            "this tool does not save or approve the preference."
        ),
    ),
    KernelToolSpec(
        name="submit_preference_management_plan",
        description=(
            "Submit an approval-gated plan to disable, restore, or permanently delete exact "
            "remembered preferences. First call list_remembered_preferences and copy each "
            "target's exact id, revision, and description. Use disable for reversible removal "
            "from future planning, restore to reactivate a disabled preference, and delete only "
            "when the user clearly requests permanent deletion. This tool drafts reviewable "
            "changes; it never applies them."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["disable", "restore", "delete"],
                            },
                            "preference_id": {"type": "string", "minLength": 1},
                            "revision": {"type": "integer", "minimum": 1},
                            "description": {"type": "string"},
                        },
                        "required": [
                            "action",
                            "preference_id",
                            "revision",
                            "description",
                        ],
                    },
                },
                "source_text": {"type": ["string", "null"]},
                "rationale": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
            },
            "required": ["actions"],
        },
        output_schema=_output_schema(
            properties={
                "compiled": {"type": "boolean"},
                "semantic_plan_input": {"type": "object"},
                "clarification": {"type": ["object", "null"]},
            }
        ),
        read_only=False,
        result_kind="semantic_plan_submission",
    ),
    KernelToolSpec(
        name="submit_preference_cleanup_plan",
        description=(
            "Submit one complete approval-gated batch for the exact cleanup_scope returned "
            "by analyze_preference_cleanup_candidates. The server recomputes that scoped "
            "candidate set and rejects a stale fingerprint or partial selection. Use "
            "disable for reversible cleanup or delete only after the user explicitly chooses "
            "permanent deletion. Ambiguous tensions are never included automatically."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["disable", "delete"],
                },
                "candidate_set_fingerprint": {
                    "type": "string",
                    "minLength": 8,
                },
                "cleanup_scope": {
                    "type": "string",
                    "enum": ["contradictions", "overlaps", "all"],
                },
                "source_text": {"type": ["string", "null"]},
                "rationale": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
            },
            "required": [
                "action",
                "cleanup_scope",
                "candidate_set_fingerprint"
            ],
        },
        output_schema=_output_schema(
            properties={
                "compiled": {"type": "boolean"},
                "semantic_plan_input": {"type": "object"},
                "candidate_count": {"type": "integer"},
                "candidate_set_fingerprint": {"type": "string"},
                "cleanup_scope": {"type": "string"},
                "complete_batch": {"type": "boolean"},
                "clarification": {"type": ["object", "null"]},
            }
        ),
        read_only=False,
        result_kind="semantic_plan_submission",
    ),
)


LEGACY_UPDATE_GOAL_STATE_SPEC = KernelToolSpec(
    name="update_goal_state",
    description="Internal compatibility GoalState command; not provider-visible.",
    input_schema=strict_provider_schema(GoalStateUpdateCommand),
    output_schema=_output_schema(
        properties={
            "goal_state": {"type": "object"},
            "persisted": {"type": "boolean"},
        }
    ),
    input_model=GoalStateUpdateCommand,
    read_only=False,
    result_kind="goal_state",
    side_effect="goal_state_write",
    approval_requirement="none",
    idempotency_behavior="command_id_replay_safe",
)


KERNEL_TOOL_SPECS: tuple[KernelToolSpec, ...] = tuple(
    [*_retrieval_specs(), *KERNEL_ONLY_TOOL_SPECS, *SEMANTIC_PLAN_TOOL_SPECS]
)
KERNEL_TOOL_SPECS_BY_NAME: dict[str, KernelToolSpec] = {
    spec.name: spec for spec in KERNEL_TOOL_SPECS
}
validate_capability_tool_references(set(KERNEL_TOOL_SPECS_BY_NAME))


def registered_kernel_tool_specs() -> list[KernelToolSpec]:
    return list(KERNEL_TOOL_SPECS)


def registered_kernel_tool_schemas() -> list[dict[str, Any]]:
    return [spec.v2_tool_schema() for spec in KERNEL_TOOL_SPECS]


def registered_external_tool_specs(surface: str) -> list[KernelToolSpec]:
    """Return canonical tools deliberately exposed on one external surface."""
    normalized_surface = surface.strip().lower()
    if not normalized_surface:
        return []
    if normalized_surface == "drasil_mcp":
        from .mcp_policy import registered_mcp_surface_specs

        return registered_mcp_surface_specs()
    return [
        spec
        for spec in KERNEL_TOOL_SPECS
        if normalized_surface in {item.lower() for item in spec.external_surfaces}
    ]


def compact_simulation_result_for_model(
    result: dict[str, Any], *, tool_result_handle: str
) -> dict[str, Any]:
    graph = result.get("conflictGraph")
    conflicts = graph.get("conflicts") if isinstance(graph, dict) else []
    issues = result.get("issues") if isinstance(result.get("issues"), list) else []
    decision = graph.get("decisionRequest") if isinstance(graph, dict) else None
    preview = []
    for conflict in conflicts[:3] if isinstance(conflicts, list) else []:
        if not isinstance(conflict, dict):
            continue
        alternatives = conflict.get("alternatives")
        preview.append(
            {
                "conflictId": conflict.get("conflictId"),
                "kind": conflict.get("kind"),
                "incomingOperationIds": conflict.get("incomingOperationIds") or [],
                "policyRecommendation": conflict.get("policyRecommendation"),
                "resolutionDisposition": conflict.get("resolutionDisposition"),
                "alternativeIds": [
                    item.get("alternativeId")
                    for item in alternatives
                    if isinstance(item, dict) and item.get("alternativeId")
                ] if isinstance(alternatives, list) else [],
            }
        )
    return {
        "read_only": False,
        "result_kind": "native_change_set_simulation",
        "observation_summary": (
            f"Simulated ChangeSet revision {result.get('changeSetRevision')}; "
            f"status {result.get('status')} with {len(conflicts) if isinstance(conflicts, list) else 0} conflicts."
        ),
        "simulation_summary": {
            "simulationId": result.get("simulationId"),
            "commandId": result.get("commandId"),
            "goalId": result.get("goalId"),
            "goalRevision": result.get("goalRevision"),
            "resultingGoalRevision": result.get("resultingGoalRevision"),
            "changeSetId": result.get("changeSetId"),
            "changeSetRevision": result.get("changeSetRevision"),
            "status": result.get("status"),
            "inputSnapshotFingerprint": (
                result.get("inputSnapshot", {}).get("fingerprint")
                if isinstance(result.get("inputSnapshot"), dict)
                else None
            ),
            "outputFingerprint": result.get("outputFingerprint"),
            "conflictCount": len(conflicts) if isinstance(conflicts, list) else 0,
            "conflictPreview": preview,
            "issues": [
                {
                    "code": item.get("code"),
                    "category": item.get("category"),
                    "affectedOperationIds": item.get("affectedOperationIds") or [],
                    "summary": str(item.get("summary") or "")[:500],
                }
                for item in issues
                if isinstance(item, dict)
            ],
            "decision": (
                {
                    "decisionId": decision.get("decisionId"),
                    "question": decision.get("question"),
                    "options": [
                        {
                            "optionId": item.get("optionId"),
                            "label": item.get("label"),
                            "alternativeId": item.get("alternativeId"),
                            "placementRelation": item.get("placementRelation"),
                        }
                        for item in decision.get("options", [])
                        if isinstance(item, dict) and item.get("optionId")
                    ],
                }
                if isinstance(decision, dict)
                else None
            ),
            "detailAvailable": True,
        },
        "model_instruction": (
            "Use get_tool_result_by_handle with this handle and section=alternatives to load "
            "executable deltas, or section=conflicts/final_facts for more evidence."
        ),
        "tool_result_handle": tool_result_handle,
    }


def _simulation_result_page(
    stored_result: dict[str, Any], *, section: str, offset: int, limit: int
) -> dict[str, Any] | None:
    if stored_result.get("result_kind") != "native_change_set_simulation":
        return None
    graph = stored_result.get("conflictGraph")
    conflicts = graph.get("conflicts") if isinstance(graph, dict) else []
    if section == "summary":
        return compact_simulation_result_for_model(
            stored_result, tool_result_handle=""
        )["simulation_summary"]
    if section == "conflicts":
        values = conflicts if isinstance(conflicts, list) else []
    elif section == "alternatives":
        values = [
            {"conflictId": conflict.get("conflictId"), "alternative": alternative}
            for conflict in conflicts if isinstance(conflict, dict)
            for alternative in conflict.get("alternatives", [])
            if isinstance(alternative, dict)
        ] if isinstance(conflicts, list) else []
    else:
        values = (
            stored_result.get("projectedFinalFacts")
            if isinstance(stored_result.get("projectedFinalFacts"), list)
            else []
        )
    page = values[offset : offset + limit]
    return {
        "section": section,
        "offset": offset,
        "limit": limit,
        "total": len(values),
        "truncated": offset + len(page) < len(values),
        "nextOffset": offset + len(page) if offset + len(page) < len(values) else None,
        "items": task_contract_surface(page),
    }


def build_kernel_tool_registry_context(*, compact: bool = False) -> dict[str, Any]:
    if compact:
        return {
            "name": "agent-kernel-tools",
            "version": "v2",
            "read_only": False,
            "tool_count": len(KERNEL_TOOL_SPECS),
            "tools": [
                {
                    "name": spec.name,
                    "read_only": spec.read_only,
                    **(
                        {"result_kind": spec.result_kind}
                        if not spec.read_only
                        else {}
                    ),
                }
                for spec in KERNEL_TOOL_SPECS
            ],
            "selection_policy": (
                "The model owns semantic action selection. Use query_task_graph, GoalState, "
                "submit_preference_plan, and get_tool_result_by_handle."
            ),
        }
    return {
        "name": "strategist-agent-kernel-tool-registry",
        "version": "v2",
        "read_only": False,
        "tool_count": len(KERNEL_TOOL_SPECS),
        "tools": [spec.context_summary() for spec in KERNEL_TOOL_SPECS],
    }


def _required_argument_errors(spec: KernelToolSpec, arguments: dict[str, Any]) -> list[str]:
    required = spec.input_schema.get("required")
    if not isinstance(required, list):
        return []
    return [
        f"Missing required tool argument: {key}."
        for key in required
        if isinstance(key, str) and key not in arguments
    ]


TASK_SCOPED_TOOL_NAMES = {
    "get_task_snapshot",
    "get_task_metadata",
    "get_task_notes",
    "get_task_branch",
    "get_parent_chain",
    "get_children",
    "get_siblings",
    "get_subtree",
    "get_related_entities",
}


def _unknown_argument_errors(spec: KernelToolSpec, arguments: dict[str, Any]) -> list[str]:
    if spec.input_schema.get("additionalProperties") is not False:
        return []
    properties = spec.input_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    allowed = {key for key in properties if isinstance(key, str)}
    unknown = sorted(key for key in arguments if key not in allowed)
    if not unknown:
        return []
    return [
        "Unsupported tool argument"
        f"{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}."
    ]


def _canonical_task_argument_errors(name: str, arguments: dict[str, Any]) -> list[str]:
    if name not in TASK_SCOPED_TOOL_NAMES:
        return []
    old_keys = [
        key
        for key in ("node_id", "node_title", "id", "title")
        if key in arguments
    ]
    if not old_keys:
        return []
    return [
        "Task lookup tools use task_id and task_title only; "
        f"unsupported argument{'s' if len(old_keys) != 1 else ''}: {', '.join(old_keys)}."
    ]


def _tool_argument_error(
    spec: KernelToolSpec,
    errors: list[str],
    *,
    repair_hint: str | None = None,
    affected_paths: list[str] | None = None,
) -> dict[str, Any]:
    message = " ".join(errors)
    typed_error = ToolError(
        code="TOOL_ARGUMENTS_INVALID",
        category=ToolErrorCategory.MODEL_REPAIRABLE,
        retryable=False,
        message_for_model=message,
        affected_paths=list(dict.fromkeys(affected_paths or []))[:16],
        repair_guidance=repair_hint,
    )
    payload = {
        "error": message,
        "tool_error": typed_error.model_dump(by_alias=True),
        "read_only": spec.read_only,
        "result_kind": "error",
    }
    if repair_hint:
        payload["repair_hint"] = repair_hint
    return payload


def _bounded_limit(value: Any, *, default: int = 8, maximum: int = 30) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(1, min(value, maximum))
    return default


def _event_type_allowed(event: dict[str, Any], event_types: set[str]) -> bool:
    if not event_types:
        return True
    event_type = str(event.get("event_type") or "").strip()
    return event_type in event_types


def _count_result_items(result: dict[str, Any]) -> int | None:
    for key in (
        "matches",
        "tasks",
        "children",
        "siblings",
        "parents",
        "related",
        "note_chunks",
        "working_set",
        "recent_actions",
        "recent_events",
        "capabilities",
        "tools",
        "due_soon",
        "overdue",
        "stale",
    ):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    task = result.get("task")
    if isinstance(task, dict):
        return 1
    return None


def summarize_kernel_tool_observation(tool_call: dict[str, Any]) -> str:
    name = str(tool_call.get("name") or "tool").strip()
    args = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
    embedded_summary = result.get("observation_summary")
    if isinstance(embedded_summary, str) and embedded_summary.strip():
        return embedded_summary.strip()
    count = _count_result_items(result)
    if name in {"search_entities", "search_notes"}:
        query = str(args.get("query") or "").strip()
        suffix = f" and found {count} result{'s' if count != 1 else ''}" if count is not None else ""
        return f"Searched task context for {query!r}{suffix}."
    if name == "get_pending_proposal":
        return "Checked whether this session has a pending proposal."
    if name == "get_recent_kernel_events":
        suffix = f" and returned {count} event{'s' if count != 1 else ''}" if count is not None else ""
        return f"Read recent Strategist kernel events{suffix}."
    if name == "get_session_memory":
        return "Read event-log-derived Strategist session memory."
    if name == "get_context_surfaces":
        return "Read current UI and session context surfaces."
    if name in {
        "submit_task_metadata_plan",
        "submit_task_tree_plan",
        "submit_preference_plan",
        "submit_preference_management_plan",
        "submit_preference_cleanup_plan",
    }:
        return "Submitted complete semantic change meaning for server compilation."
    if name == "propose_metadata_update":
        return "Compiled typed metadata update arguments into a reviewable proposal draft."
    if name in {"submit_task_tree_plan", "propose_tree_operation"}:
        return "Compiled typed tree operation arguments into a reviewable proposal draft."
    if name == "propose_preference_memory":
        return "Compiled typed preference-memory arguments into a reviewable proposal draft."
    if name == "analyze_preference_cleanup_candidates":
        return "Analyzed the complete saved-preference inventory for cleanup candidates."
    if name == "get_workload_overview":
        scope = str(args.get("category_name") or "all categories").strip()
        return f"Read workload overview for {scope}."
    if name == "query_task_graph":
        count_suffix = f" with {count} task{'s' if count != 1 else ''}" if count is not None else ""
        return f"Queried task graph evidence{count_suffix}."
    if name == "search_capability_catalog":
        goal = str(args.get("goal") or "").strip()
        suffix = f" and found {count} capabilit{'y' if count == 1 else 'ies'}" if count is not None else ""
        return f"Searched Drasil capabilities for {goal!r}{suffix}."
    if name == "load_skill":
        capability_id = str(args.get("capability_id") or "").strip()
        return f"Loaded the versioned {capability_id!r} Drasil skill."
    if name == "search_tool_catalog":
        query = str(args.get("query") or "").strip()
        suffix = f" and found {count} tool{'s' if count != 1 else ''}" if count is not None else ""
        return f"Searched the tool catalog for {query!r}{suffix}."
    if name == "list_tasks_by_filter":
        suffix = f" and returned {count} task{'s' if count != 1 else ''}" if count is not None else ""
        return f"Listed tasks matching structured filters{suffix}."
    if name == "analyze_tree_cleanup_candidates":
        scope = str(result.get("scope") or args.get("category_name") or "the task tree").strip()
        candidate_count = result.get("cleanup_candidate_count")
        branch_count = result.get("messy_branch_count")
        if isinstance(candidate_count, int) and isinstance(branch_count, int):
            return (
                f"Analyzed cleanup signals in {scope}: {candidate_count} cleanup "
                f"candidate{'s' if candidate_count != 1 else ''}, {branch_count} messy "
                f"branch{'es' if branch_count != 1 else ''}."
            )
        return f"Analyzed cleanup signals in {scope}."
    if name.startswith("get_"):
        suffix = f" with {count} item{'s' if count != 1 else ''}" if count is not None else ""
        return f"Read {name.replace('_', ' ')}{suffix}."
    return f"Called {name}."


def _with_tool_metadata(
    *,
    name: str,
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any],
) -> dict[str, Any]:
    spec = (
        LEGACY_UPDATE_GOAL_STATE_SPEC
        if name == "update_goal_state"
        else KERNEL_TOOL_SPECS_BY_NAME[name]
    )
    payload = dict(result)
    if payload.get("error") and "tool_error" not in payload:
        code = (
            "TOOL_CONTEXT_UNAVAILABLE"
            if name in {
                "list_calendar_blocks",
                "get_task_schedule",
                "handoff_to_planner",
                "draft_change_set",
                "simulate_change_set",
            }
            else "TOOL_VALIDATION_FAILED"
        )
        typed_error = ToolError(
            code=code,
            category=ToolErrorCategory.MODEL_REPAIRABLE,
            retryable=False,
            message_for_model=str(payload["error"]),
            repair_guidance=(
                str(payload.get("repair_hint"))
                if payload.get("repair_hint")
                else None
            ),
        )
        payload["tool_error"] = typed_error.model_dump(by_alias=True)
    if name == "simulate_change_set" and not payload.get("error"):
        # Keep the strict canonical simulation output untouched at the tool
        # boundary. Runtime transport metadata is projected separately.
        return payload
    tool_call = {
        "name": name,
        "arguments": arguments or {},
        "result": payload,
    }
    payload["read_only"] = spec.read_only
    payload["result_kind"] = spec.result_kind
    payload["observation_summary"] = summarize_kernel_tool_observation(tool_call)
    return payload


TOOL_SEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "i",
    "in",
    "is",
    "me",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "tool",
    "tools",
    "use",
    "what",
    "which",
    "with",
}
def _tool_search_tokens(value: Any) -> set[str]:
    text = str(value or "").lower()
    return {
        token
        for token in re.findall(r"[a-z0-9_]+", text)
        if len(token) >= 2 and token not in TOOL_SEARCH_STOPWORDS
    }


def _tool_catalog_score(spec: KernelToolSpec, query_tokens: set[str], query_text: str) -> tuple[float, list[str]]:
    name_tokens = _tool_search_tokens(spec.name.replace("_", " "))
    description_tokens = _tool_search_tokens(spec.description)
    input_keys = set(spec.context_summary().get("input_keys") or [])
    input_tokens = _tool_search_tokens(" ".join(input_keys))
    result_tokens = _tool_search_tokens(spec.result_kind)
    reasons: list[str] = []
    score = 0.0
    name_hits = query_tokens.intersection(name_tokens)
    if name_hits:
        score += 4.0 * len(name_hits)
        reasons.append("matched tool name")
    result_hits = query_tokens.intersection(result_tokens)
    if result_hits:
        score += 2.5 * len(result_hits)
        reasons.append("matched result kind")
    input_hits = query_tokens.intersection(input_tokens)
    if input_hits:
        score += 2.0 * len(input_hits)
        reasons.append("matched input keys")
    description_hits = query_tokens.intersection(description_tokens)
    if description_hits:
        score += min(4.0, 0.8 * len(description_hits))
        reasons.append("matched description")
    searchable = " ".join(
        [
            spec.name.replace("_", " "),
            spec.description,
            spec.result_kind.replace("_", " "),
            " ".join(input_keys),
        ]
    ).lower()
    if query_text and query_text in searchable:
        score += 3.0
        reasons.append("matched phrase")
    if spec.name == "query_task_graph" and query_tokens.intersection(
        {"task", "tasks", "urgent", "due", "deadline", "priority", "status", "question", "query"}
    ):
        score += 3.0
        reasons.append("general task-graph entry point")
    return score, reasons


def _search_tool_catalog(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    limit = _bounded_limit(arguments.get("limit"), default=6, maximum=12)
    query_tokens = _tool_search_tokens(query)
    ranked: list[tuple[float, str, KernelToolSpec, list[str]]] = []
    for spec in KERNEL_TOOL_SPECS:
        score, reasons = _tool_catalog_score(spec, query_tokens, query.lower())
        if score <= 0:
            continue
        ranked.append((score, spec.name, spec, reasons))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    tools: list[dict[str, Any]] = []
    for score, _, spec, reasons in ranked[:limit]:
        summary = spec.context_summary()
        tools.append(
            {
                **summary,
                "score": round(score, 3),
                "match_reasons": reasons,
                "tool_schema": spec.tool_schema(),
            }
        )
    return {
        "query": query,
        "count": len(tools),
        "tools": tools,
        "selection_hint": (
            "Use query_task_graph first for broad task-world reads; use a specialized "
            "tool when the needed entity, branch, metadata, or proposal/session surface is known."
        ),
    }


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(value.strip().split())
    return None


_EXPLICIT_CLOCK_FRAGMENT_RE = re.compile(
    r"\b(?:[01]?\d(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)|[0-2]?\d:[0-5]\d)\b",
    re.IGNORECASE,
)
_DEFERRED_SCHEDULING_FRAGMENT_RE = re.compile(
    r"\b(?:schedule(?:d|ing)?\s+(?:it\s+|this\s+|that\s+)?later|"
    r"leave\s+(?:it\s+|this\s+|that\s+)?unscheduled|"
    r"keep\s+(?:it\s+|this\s+|that\s+)?unscheduled|"
    r"do\s+not\s+schedule(?:\s+(?:it|this|that))?(?:\s+yet)?|"
    r"don['’]?t\s+schedule(?:\s+(?:it|this|that))?(?:\s+yet)?|"
    r"not\s+schedule(?:d)?\s+yet|defer\s+(?:its?\s+)?scheduling)\b",
    re.IGNORECASE,
)
_OWNERSHIP_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "create",
        "for",
        "in",
        "of",
        "on",
        "task",
        "the",
        "to",
    }
)


def _inferred_deferred_schedule_ownership(
    *,
    original_user_request: str | None,
    operations: list[dict[str, Any]],
) -> tuple[str, str, str, str] | None:
    """Resolve one timed leaf and one explicitly deferred leaf from request clauses.

    This conservative conservation guard only returns ownership when distinct
    request fragments uniquely identify both an explicitly clock-timed task and
    an activity whose scheduling is deferred.
    """

    request = _clean_text(original_user_request)
    if not request:
        return None
    fragments = [
        fragment.strip()
        for fragment in re.split(r"[.!?;\n]+", request)
        if fragment.strip()
    ]
    timed_fragments = [
        fragment for fragment in fragments if _EXPLICIT_CLOCK_FRAGMENT_RE.search(fragment)
    ]
    deferred_indexes = [
        index
        for index, fragment in enumerate(fragments)
        if _DEFERRED_SCHEDULING_FRAGMENT_RE.search(fragment)
    ]
    if not timed_fragments or not deferred_indexes:
        return None
    deferred_fragments = [
        " ".join(fragments[max(0, index - 2) : index + 1])
        for index in deferred_indexes
    ]
    parent_temp_ids = {
        parent
        for operation in operations
        if (parent := _clean_text(operation.get("parent_temp_id")))
    }
    title_tokens_by_leaf: dict[str, set[str]] = {}
    for operation in operations:
        if str(operation.get("kind") or "") != "create_node":
            continue
        temp_id = _clean_text(operation.get("temp_id"))
        if not temp_id or temp_id in parent_temp_ids:
            continue
        title_tokens_by_leaf[temp_id] = {
            token
            for token in re.findall(
                r"[a-z0-9]+", str(operation.get("title") or "").casefold()
            )
            if token not in _OWNERSHIP_STOP_WORDS
        }
    if len(title_tokens_by_leaf) < 2:
        return None
    shared_tokens = (
        set.intersection(*title_tokens_by_leaf.values())
        if all(title_tokens_by_leaf.values())
        else set()
    )

    def unique_owner(
        fragments_to_score: list[str],
        *,
        minimum_score: int,
    ) -> tuple[str, str] | None:
        scored: list[tuple[int, str]] = []
        source_by_target: dict[str, str] = {}
        for target, title_tokens in title_tokens_by_leaf.items():
            best_score = 0
            best_source = ""
            for fragment in fragments_to_score:
                fragment_tokens = {
                    token
                    for token in re.findall(r"[a-z0-9]+", fragment.casefold())
                    if token not in _OWNERSHIP_STOP_WORDS
                }
                score = sum(
                    1
                    for title_token in title_tokens - shared_tokens
                    if any(
                        title_token == fragment_token
                        or (
                            len(title_token) >= 4
                            and len(fragment_token) >= 4
                            and title_token[:3] == fragment_token[:3]
                            and abs(len(title_token) - len(fragment_token)) <= 4
                        )
                        for fragment_token in fragment_tokens
                    )
                )
                if score > best_score:
                    best_score = score
                    best_source = fragment
            scored.append((best_score, target))
            source_by_target[target] = best_source
        scored.sort(reverse=True)
        if not scored or scored[0][0] < minimum_score:
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1], source_by_target[scored[0][1]]

    scheduled = unique_owner(timed_fragments, minimum_score=2)
    deferred = unique_owner(deferred_fragments, minimum_score=1)
    if scheduled is None:
        bounded_atomic_leaf_ids: list[str] = []
        for operation in operations:
            temp_id = _clean_text(operation.get("temp_id"))
            if not temp_id or temp_id not in title_tokens_by_leaf:
                continue
            fields = (
                operation.get("fields")
                if isinstance(operation.get("fields"), dict)
                else {}
            )
            estimate = _first_present(
                operation,
                "time_estimate_minutes",
                "estimate_minutes",
            )
            if estimate in (None, ""):
                estimate = _first_present(
                    fields,
                    "time_estimate_minutes",
                    "estimate_minutes",
                )
            if (
                isinstance(estimate, int)
                and not isinstance(estimate, bool)
                and 0 < estimate <= 30
            ):
                bounded_atomic_leaf_ids.append(temp_id)
        if len(bounded_atomic_leaf_ids) == 1:
            scheduled = bounded_atomic_leaf_ids[0], timed_fragments[0]
    if deferred is None and scheduled is not None:
        remaining_leaf_ids = [
            target for target in title_tokens_by_leaf if target != scheduled[0]
        ]
        if len(remaining_leaf_ids) == 1:
            deferred = remaining_leaf_ids[0], deferred_fragments[-1]
    if scheduled is None or deferred is None or scheduled[0] == deferred[0]:
        return None
    return scheduled[0], scheduled[1], deferred[0], deferred[1]


def _proposal_target_fields(arguments: dict[str, Any]) -> dict[str, Any]:
    target_task_id = _first_present(
        arguments,
        "target_task_id",
        "targetTaskId",
        "target_node_id",
        "targetNodeId",
        "task_id",
        "taskId",
    )
    target_task_title = _clean_text(
        _first_present(
            arguments,
            "target_task_title",
            "targetTaskTitle",
            "target_node_title",
            "targetNodeTitle",
            "task_title",
            "taskTitle",
        )
    )
    fields: dict[str, Any] = {}
    if isinstance(target_task_id, int) and not isinstance(target_task_id, bool):
        fields["target_node_id"] = target_task_id
    if target_task_title:
        fields["target_node_title"] = target_task_title
    return fields


def _compile_metadata_proposal_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    target_fields = _proposal_target_fields(arguments)
    raw_target_updates = arguments.get("target_updates")
    target_updates: list[dict[str, Any]] = []
    if isinstance(raw_target_updates, list):
        for raw_item in raw_target_updates:
            if not isinstance(raw_item, dict):
                continue
            target_task_id = _first_present(
                raw_item,
                "target_task_id",
                "targetTaskId",
                "target_node_id",
                "targetNodeId",
            )
            item_updates = normalize_metadata_updates(raw_item.get("updates"))
            if (
                not isinstance(target_task_id, int)
                or isinstance(target_task_id, bool)
                or not isinstance(item_updates, dict)
                or not item_updates
            ):
                continue
            item: dict[str, Any] = {
                "target_node_id": target_task_id,
                "updates": copy.deepcopy(item_updates),
            }
            target_title = _clean_text(
                _first_present(raw_item, "target_task_title", "targetTaskTitle")
            )
            if target_title:
                item["target_node_title"] = target_title
            target_updates.append(item)
    if not target_fields and not target_updates:
        return {
            "error": (
                "propose_metadata_update requires one target task or target_updates "
                "with exact target_task_id values."
            ),
            "proposal_kind": "metadata",
            "compiled": False,
        }
    raw_updates = arguments.get("updates")
    updates = normalize_metadata_updates(raw_updates)
    if isinstance(raw_updates, dict) and raw_updates and not updates and not target_updates:
        return {
            "error": (
                "propose_metadata_update received no valid metadata values. Use a "
                "date-only YYYY-MM-DD deadline or another supported metadata field."
            ),
            "proposal_kind": "metadata",
            "compiled": False,
        }
    if (not isinstance(updates, dict) or not updates) and not target_updates:
        return {
            "error": "propose_metadata_update requires updates or non-empty target_updates.",
            "proposal_kind": "metadata",
            "compiled": False,
        }
    proposal = {
        "proposal_kind": "metadata",
        **target_fields,
        "rationale": (
            _clean_text(arguments.get("rationale"))
            or "The model selected this task metadata change; review before applying."
        ),
        "summary": _clean_text(arguments.get("summary")) or "Review metadata changes",
    }
    if isinstance(updates, dict) and updates:
        proposal["updates"] = copy.deepcopy(updates)
    if target_updates:
        proposal["target_updates"] = target_updates
    return {
        "proposal_kind": "metadata",
        "metadata_proposal": proposal,
        "compiled": True,
    }


def _structural_operations_from_task_patch(
    task_patch: Any,
) -> list[dict[str, Any]]:
    """Recover semantic tree operations from a stored canonical TaskPatch."""

    raw_operations = (
        task_patch.get("operations")
        if isinstance(task_patch, dict)
        and isinstance(task_patch.get("operations"), list)
        else []
    )
    structural: list[dict[str, Any]] = []
    for raw in raw_operations:
        if not isinstance(raw, dict):
            continue
        operation = copy.deepcopy(raw)
        kind = str(operation.pop("op", "") or "").strip().lower()
        if kind == "create":
            operation["kind"] = "create_node"
        elif kind == "update":
            fields = operation.pop("fields", None)
            operation["kind"] = "metadata_update"
            if isinstance(fields, dict):
                operation["updates"] = fields
            if "task_id" in operation and "node_id" not in operation:
                operation["node_id"] = operation.pop("task_id")
        elif kind == "move":
            operation["kind"] = "move_subtree"
            if "task_id" in operation and "node_id" not in operation:
                operation["node_id"] = operation.pop("task_id")
        elif kind == "delete":
            operation["kind"] = "delete_node"
            if "task_id" in operation and "node_id" not in operation:
                operation["node_id"] = operation.pop("task_id")
        else:
            continue
        structural.append(operation)
    return structural


def _duplicate_existing_task_matches(
    structural_operations: Any,
    *,
    nodes: list[StrategyNode],
    default_category_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return bounded high-confidence active-task identity collisions."""

    if not isinstance(structural_operations, list):
        return []
    deleted_ids = {
        int(node_id)
        for operation in structural_operations
        if isinstance(operation, dict)
        and str(operation.get("kind") or operation.get("op") or "").strip().lower()
        in {"delete_node", "delete"}
        and isinstance(
            node_id := _first_present(
                operation,
                "node_id",
                "nodeId",
                "task_id",
                "taskId",
            ),
            int,
        )
        and not isinstance(node_id, bool)
    }
    proposed_identities = [
        (
            int(category_id)
            if isinstance(category_id, int) and not isinstance(category_id, bool)
            else default_category_id,
            " ".join(str(operation.get("title") or "").split()).casefold(),
            (
                int(parent_id)
                if isinstance(parent_id, int) and not isinstance(parent_id, bool)
                else None
            ),
        )
        for operation in structural_operations
        if isinstance(operation, dict)
        and str(operation.get("kind") or operation.get("op") or "").strip().lower()
        in {"create_node", "create", "task.create"}
        and str(operation.get("title") or "").strip()
        for category_id in [
            _first_present(
                operation,
                "category_id",
                "categoryId",
                "target_category_id",
                "targetCategoryId",
            )
        ]
        for parent_id in [
            _first_present(
                operation,
                "parent_task_id",
                "parentTaskId",
                "parent_node_id",
                "parentNodeId",
            )
        ]
    ]
    if not proposed_identities:
        return []

    generic_action_tokens = {
        "a",
        "an",
        "the",
        "to",
        "for",
        "of",
        "and",
        "send",
        "write",
        "create",
        "add",
        "make",
        "do",
        "complete",
    }

    def identity_tokens(title: str) -> frozenset[str]:
        normalized = "".join(
            character.casefold() if character.isalnum() else " "
            for character in title
        )
        return frozenset(
            token
            for token in normalized.split()
            if token and token not in generic_action_tokens
        )

    def proposed_identity_matches(node: StrategyNode) -> bool:
        existing_title = " ".join(str(node.title or "").split()).casefold()
        existing_tokens = identity_tokens(existing_title)
        for category_id, proposed_title, proposed_parent_id in proposed_identities:
            if category_id != int(node.category_id):
                continue
            if proposed_title == existing_title:
                return True
            proposed_tokens = identity_tokens(proposed_title)
            # Containment after removing generic action words is a stable
            # entity-identity signal (for example, "send a message to X" and
            # "message X"). Requiring at least two significant tokens avoids
            # collapsing short, genuinely distinct tasks.
            if (
                proposed_parent_id != int(node.id)
                and
                min(len(existing_tokens), len(proposed_tokens)) >= 2
                and (
                    existing_tokens.issubset(proposed_tokens)
                    or proposed_tokens.issubset(existing_tokens)
                )
                and abs(len(existing_tokens) - len(proposed_tokens)) <= 2
            ):
                return True
        return False

    matches = [
        {
            "task_id": int(node.id),
            "title": node.title,
            "category_id": int(node.category_id),
        }
        for node in nodes
        if node.id is not None
        and int(node.id) not in deleted_ids
        and str(node.status or "").strip().casefold()
        not in {"done", "completed", "archived"}
        and proposed_identity_matches(node)
    ]
    return sorted(matches, key=lambda item: (item["title"].casefold(), item["task_id"]))[
        :8
    ]


def _reuse_existing_tasks_in_compound_plan(
    arguments: dict[str, Any],
    *,
    nodes: list[StrategyNode],
    default_category_id: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rebind unambiguous duplicate creates when the request also uses the task.

    A compound request often names a mixture of new work and an already-existing
    task.  If a provider represents every item as ``create_node``, rejecting the
    whole plan forces another expensive reasoning pass.  Reuse is safe only when
    the collided temporary task participates in scheduling or parents another
    requested node; a standalone create still returns the normal duplicate result.
    """

    structural = arguments.get("structural_operations")
    if not isinstance(structural, list):
        return arguments, []
    schedule = arguments.get("created_task_schedule")
    schedule_owners = []
    if isinstance(schedule, dict):
        raw_groups = schedule.get("schedule_groups")
        schedule_owners = (
            [group for group in raw_groups if isinstance(group, dict)]
            if isinstance(raw_groups, list)
            else [schedule]
        )
    scheduled_temp_ids = {
        str(value).strip()
        for owner in schedule_owners
        for value in (owner.get("target_temp_ids") or [])
        if isinstance(value, str) and value.strip()
    }
    parent_temp_ids = {
        str(parent).strip()
        for operation in structural
        if isinstance(operation, dict)
        for parent in [
            _first_present(
                operation,
                "parent_temp_id",
                "parentTempId",
                "parent_node_temp_id",
            )
        ]
        if isinstance(parent, str) and parent.strip()
    }

    bindings: dict[str, dict[str, Any]] = {}
    for operation in structural:
        if not isinstance(operation, dict):
            continue
        kind = str(operation.get("kind") or operation.get("op") or "").strip().lower()
        if kind not in {"create_node", "create", "task.create"}:
            continue
        temp_id = _clean_text(
            _first_present(operation, "temp_id", "tempId", "temporary_id")
        )
        if not temp_id or (
            temp_id not in scheduled_temp_ids and temp_id not in parent_temp_ids
        ):
            continue
        matches = _duplicate_existing_task_matches(
            [operation],
            nodes=nodes,
            default_category_id=default_category_id,
        )
        if len(matches) != 1:
            continue
        bindings[temp_id] = matches[0]

    if not bindings:
        return arguments, []

    normalized = copy.deepcopy(arguments)
    normalized_structural = [
        operation
        for operation in normalized.get("structural_operations", [])
        if not (
            isinstance(operation, dict)
            and _clean_text(
                _first_present(operation, "temp_id", "tempId", "temporary_id")
            )
            in bindings
            and str(operation.get("kind") or operation.get("op") or "")
            .strip()
            .lower()
            in {"create_node", "create", "task.create"}
        )
    ]
    for operation in normalized_structural:
        if not isinstance(operation, dict):
            continue
        parent_temp_id = _clean_text(
            _first_present(operation, "parent_temp_id", "parentTempId")
        )
        if parent_temp_id in bindings:
            operation["parent_task_id"] = bindings[parent_temp_id]["task_id"]
            operation.pop("parent_temp_id", None)
            operation.pop("parentTempId", None)
    normalized["structural_operations"] = normalized_structural

    def replace_targets(
        owner: dict[str, Any],
        *,
        task_id_key: str = "target_task_ids",
    ) -> None:
        raw_temp_ids = owner.get("target_temp_ids")
        if not isinstance(raw_temp_ids, list):
            return
        retained = [
            value
            for value in raw_temp_ids
            if not (isinstance(value, str) and value.strip() in bindings)
        ]
        rebound_ids = [
            bindings[value.strip()]["task_id"]
            for value in raw_temp_ids
            if isinstance(value, str) and value.strip() in bindings
        ]
        owner["target_temp_ids"] = list(dict.fromkeys(retained))
        owner[task_id_key] = list(
            dict.fromkeys([*(owner.get(task_id_key) or []), *rebound_ids])
        )
        constraints = owner.get("request_constraints")
        if not isinstance(constraints, list):
            return
        for constraint in constraints:
            if not isinstance(constraint, dict):
                continue
            replace_targets(constraint, task_id_key="task_ids")
            for temp_key, task_key in (
                ("before_temp_id", "before_task_id"),
                ("after_temp_id", "after_task_id"),
            ):
                ref = _clean_text(constraint.get(temp_key))
                if ref in bindings:
                    constraint[task_key] = bindings[ref]["task_id"]
                    constraint.pop(temp_key, None)

    normalized_schedule = normalized.get("created_task_schedule")
    if isinstance(normalized_schedule, dict):
        raw_groups = normalized_schedule.get("schedule_groups")
        if isinstance(raw_groups, list):
            for group in raw_groups:
                if isinstance(group, dict):
                    replace_targets(group)
        else:
            replace_targets(normalized_schedule)

    raw_items = normalized.get("request_items")
    if isinstance(raw_items, list):
        retained_items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            target_temp_ids = item.get("target_temp_ids")
            rebound_ids = [
                bindings[value.strip()]["task_id"]
                for value in (target_temp_ids or [])
                if isinstance(value, str) and value.strip() in bindings
            ]
            if rebound_ids:
                item["target_temp_ids"] = [
                    value
                    for value in (target_temp_ids or [])
                    if not (isinstance(value, str) and value.strip() in bindings)
                ]
                item["target_task_ids"] = list(
                    dict.fromkeys([*(item.get("target_task_ids") or []), *rebound_ids])
                )
                changes = item.get("changes")
                if isinstance(changes, list):
                    item["changes"] = [change for change in changes if change != "create"]
            if item.get("changes"):
                retained_items.append(item)
        normalized["request_items"] = retained_items

    return normalized, list(bindings.values())


def _revision_semantic_payload(
    value: Any,
    *,
    depth: int = 0,
) -> dict[str, Any] | None:
    """Find a stored semantic/task-patch payload through bounded wrappers."""

    if not isinstance(value, dict) or depth > 4:
        return None
    operations = value.get(
        "structural_operations",
        value.get("structuralOperations"),
    )
    task_patch = value.get("task_patch", value.get("taskPatch"))
    if (
        isinstance(operations, list)
        and operations
    ) or (
        isinstance(task_patch, dict)
        and isinstance(task_patch.get("operations"), list)
        and task_patch["operations"]
    ):
        return value
    for key in (
        "payload",
        "proposal",
        "branch_proposal",
        "branchProposal",
        "semantic_plan_input",
        "semanticPlanInput",
    ):
        nested = _revision_semantic_payload(value.get(key), depth=depth + 1)
        if nested is not None:
            return nested
    return None


def _normalize_semantic_plan_container_aliases(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Canonicalize provider-equivalent top-level plan field names.

    Nested semantic normalization must not depend on whether a provider emits
    the JSON-schema spelling or its camelCase serialization.  Pydantic accepts
    both spellings, but our richer boundary normalizers run before Pydantic;
    canonicalizing the container first guarantees every nested value travels
    through the same semantic validation path.
    """

    normalized = copy.deepcopy(arguments)
    aliases_by_field = {
        "structural_operations": ("structuralOperations", "operations"),
        "created_task_schedule": ("createdTaskSchedule",),
        "recurring_schedule": ("recurringSchedule",),
        "recurring_schedule_updates": ("recurringScheduleUpdates",),
        "request_items": ("requestItems",),
        "revision_of_proposal_id": ("revisionOfProposalId",),
        "replace_structural_operations": ("replaceStructuralOperations",),
        "replace_recurring_schedule": ("replaceRecurringSchedule",),
        "target_category_id": ("targetCategoryId",),
        "target_category_name": ("targetCategoryName",),
        "target_task_id": ("targetTaskId",),
        "target_task_title": ("targetTaskTitle",),
    }
    for canonical, aliases in aliases_by_field.items():
        if normalized.get(canonical) in (None, "", [], {}):
            for alias in aliases:
                value = normalized.get(alias)
                if value not in (None, "", [], {}):
                    normalized[canonical] = value
                    break
        for alias in aliases:
            normalized.pop(alias, None)

    # Some providers place the documented schedule_groups collection beside
    # created_task_schedule instead of inside it. Both shapes express the same
    # typed plan; canonicalize the container rather than spending a model turn
    # on a lossless nesting repair.
    top_level_groups = _first_present(
        normalized,
        "schedule_groups",
        "scheduleGroups",
    )
    if isinstance(top_level_groups, list) and top_level_groups:
        schedule = normalized.get("created_task_schedule")
        if not isinstance(schedule, dict):
            schedule = {}
            normalized["created_task_schedule"] = schedule
        if schedule.get("schedule_groups") in (None, "", [], {}):
            schedule["schedule_groups"] = copy.deepcopy(top_level_groups)
    normalized.pop("schedule_groups", None)
    normalized.pop("scheduleGroups", None)
    return normalized


def _compile_tree_operation_tool(
    arguments: dict[str, Any],
    *,
    original_user_request: str | None = None,
    session_memory: dict[str, Any] | None = None,
    today_override: date | None = None,
    pending_proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    revision_of_proposal_id = _clean_text(arguments.get("revision_of_proposal_id"))
    pending_proposal_id = _clean_text(
        pending_proposal.get("proposal_id") if isinstance(pending_proposal, dict) else None
    )
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory, dict)
        and isinstance(session_memory.get("active_request"), dict)
        else None
    )
    active_revision = (
        active_request.get("proposal_revision")
        if isinstance(active_request, dict)
        and isinstance(active_request.get("proposal_revision"), dict)
        else None
    )
    if (
        revision_of_proposal_id is None
        and pending_proposal_id
        and isinstance(active_revision, dict)
        and _clean_text(active_revision.get("proposal_id")) == pending_proposal_id
    ):
        revision_of_proposal_id = pending_proposal_id
    revision_payload: dict[str, Any] | None = None
    if revision_of_proposal_id:
        if revision_of_proposal_id != pending_proposal_id:
            return {
                "error": "revision_of_proposal_id must reference the current pending proposal.",
                "proposal_kind": "branch",
                "compiled": False,
            }
        if str(pending_proposal.get("proposal_kind") or "").strip() != "branch":
            return {
                "error": "propose_tree_operation can only revise a pending branch proposal.",
                "proposal_kind": "branch",
                "compiled": False,
            }
        pending_payload = pending_proposal.get("payload")
        if not isinstance(pending_payload, dict):
            pending_payload = pending_proposal.get("semantic_contract")
        if isinstance(pending_payload, dict):
            revision_payload = (
                _revision_semantic_payload(pending_payload)
                or pending_payload
            )
    raw_operations = arguments.get("structural_operations")
    created_task_schedule = arguments.get("created_task_schedule")

    def scheduled_existing_task_ids(value: Any) -> list[int]:
        if not isinstance(value, dict):
            return []
        raw_ids = value.get("target_task_ids")
        candidates = list(raw_ids) if isinstance(raw_ids, list) else []
        groups = value.get("schedule_groups")
        if isinstance(groups, list):
            candidates.extend(
                task_id
                for group in groups
                if isinstance(group, dict)
                for task_id in (
                    group.get("target_task_ids")
                    if isinstance(group.get("target_task_ids"), list)
                    else []
                )
            )
        return list(
            dict.fromkeys(
                task_id
                for task_id in candidates
                if isinstance(task_id, int) and not isinstance(task_id, bool)
            )
        )

    existing_schedule_task_ids = scheduled_existing_task_ids(
        created_task_schedule
    )
    if (
        isinstance(revision_payload, dict)
        and arguments.get("replace_structural_operations") is not True
    ):
        raw_operations = revision_payload.get(
            "structural_operations",
            revision_payload.get("structuralOperations"),
        )
        if not isinstance(raw_operations, list) or not raw_operations:
            raw_operations = _structural_operations_from_task_patch(
                revision_payload.get(
                    "task_patch",
                    revision_payload.get("taskPatch"),
                )
            )
    if (
        (not isinstance(raw_operations, list) or not raw_operations)
        and not existing_schedule_task_ids
    ):
        return {
            "error": (
                "propose_tree_operation requires task operations or an existing-task "
                "created_task_schedule target."
            ),
            "proposal_kind": "branch",
            "compiled": False,
        }
    request_text = str(original_user_request or "").strip()
    structural_operations = [
        _normalize_tree_operation_tool_argument(item)
        for item in (raw_operations if isinstance(raw_operations, list) else [])
        if isinstance(item, dict)
    ]
    if not structural_operations and not existing_schedule_task_ids:
        return {
            "error": "propose_tree_operation structural_operations must contain objects.",
            "proposal_kind": "branch",
            "compiled": False,
        }
    pending_updates = _pending_metadata_updates_from_session_memory(
        session_memory=session_memory,
        today_override=today_override,
        current_user_request=request_text,
    )
    current_turn_updates = (
        build_metadata_updates_from_prompt(request_text, today_override or date.today())
        if request_text
        else {}
    )
    has_metadata_update = any(
        str(operation.get("kind") or operation.get("op") or "").strip().lower()
        in {"metadata_update", "update_metadata"}
        for operation in structural_operations
    )
    if pending_updates and not current_turn_updates:
        for operation in structural_operations:
            operation_kind = str(operation.get("kind") or operation.get("op") or "").strip().lower()
            if operation_kind not in {"metadata_update", "update_metadata"}:
                continue
            updates = operation.get("updates")
            if not isinstance(updates, dict):
                updates = {}
                operation["updates"] = updates
            updates.update(pending_updates)
    if not has_metadata_update:
        revision_updates = (
            current_turn_updates
            if isinstance(revision_payload, dict) and current_turn_updates
            else pending_updates
        )
        if revision_updates:
            existing_target_task_id = _first_present(
                arguments,
                "target_task_id",
                "targetTaskId",
                "target_node_id",
                "targetNodeId",
            )
            existing_target_task_title = _clean_text(
                _first_present(
                    arguments,
                    "target_task_title",
                    "targetTaskTitle",
                    "target_node_title",
                    "targetNodeTitle",
                )
            )
            has_existing_target = (
                isinstance(existing_target_task_id, int)
                and not isinstance(existing_target_task_id, bool)
            ) or bool(existing_target_task_title)
            recurring_schedule = arguments.get("recurring_schedule")
            created_task_schedule = arguments.get("created_task_schedule")
            has_explicit_created_schedule_target = bool(
                isinstance(recurring_schedule, dict)
                and _clean_text(recurring_schedule.get("target_temp_id"))
            ) or bool(
                isinstance(created_task_schedule, dict)
                and created_task_schedule.get("target_temp_ids")
            )
            bound_to_created_node = bool(
                (not has_existing_target or has_explicit_created_schedule_target)
                and _bind_pending_updates_to_created_node(
                    structural_operations=structural_operations,
                    pending_updates=revision_updates,
                    recurring_schedule=recurring_schedule,
                    created_task_schedule=created_task_schedule,
                )
            )
            if not bound_to_created_node:
                target_task_id = existing_target_task_id
                target_task_title = existing_target_task_title
                # A clarification answer about a task that is being created belongs on
                # that create operation. Never manufacture an orphan metadata update:
                # normalization would drop it and make a valid composed proposal fail
                # operation-conservation validation on every retry.
                if (
                    isinstance(target_task_id, int)
                    and not isinstance(target_task_id, bool)
                ) or target_task_title:
                    metadata_operation: dict[str, Any] = {
                        "kind": "metadata_update",
                        "updates": revision_updates,
                    }
                    if isinstance(target_task_id, int) and not isinstance(
                        target_task_id, bool
                    ):
                        metadata_operation["node_id"] = target_task_id
                    elif target_task_title:
                        metadata_operation["target_task_title"] = target_task_title
                    structural_operations.insert(0, metadata_operation)
    preserve_revision_structure = bool(
        isinstance(revision_payload, dict)
        and arguments.get("replace_structural_operations") is not True
    )
    proposal: dict[str, Any] = {
        "proposal_kind": (
            _clean_text(revision_payload.get("proposal_kind"))
            if preserve_revision_structure and isinstance(revision_payload, dict)
            else _clean_text(arguments.get("proposal_kind"))
        )
        or "restructure",
        "structural_operations": structural_operations,
        "rationale": (
            _clean_text(arguments.get("rationale"))
            or (
                _clean_text(revision_payload.get("rationale"))
                if isinstance(revision_payload, dict)
                else None
            )
            or "The model selected these task-tree operations; review before applying."
        ),
        "summary": (
            _clean_text(arguments.get("summary"))
            or (
                _clean_text(revision_payload.get("summary"))
                if isinstance(revision_payload, dict)
                else None
            )
            or "Review task-tree changes"
        ),
    }
    if revision_of_proposal_id:
        proposal["revision_of_proposal_id"] = revision_of_proposal_id
    if not preserve_revision_structure:
        proposal.update(_proposal_target_fields(arguments))
        if existing_schedule_task_ids:
            proposal["target_node_ids"] = existing_schedule_task_ids
            # An existing task scheduled alongside newly-created tree nodes is an
            # affected calendar target, not the structural root of the proposal.
            # Binding it as target_node_id makes branch normalization re-parent
            # every create beneath that unrelated task (and can cross workspaces).
            if len(existing_schedule_task_ids) == 1 and not structural_operations:
                proposal.setdefault(
                    "target_node_id",
                    existing_schedule_task_ids[0],
                )
    if isinstance(revision_payload, dict):
        for target_key in (
            "target_node_id",
            "target_node_title",
            "target_node_ids",
        ):
            if (
                (preserve_revision_structure or target_key not in proposal)
                and target_key in revision_payload
            ):
                proposal[target_key] = copy.deepcopy(revision_payload[target_key])
    target_category_id = _first_present(
        arguments,
        "target_category_id",
        "targetCategoryId",
        "category_id",
        "categoryId",
    )
    if (
        not preserve_revision_structure
        and isinstance(target_category_id, int)
        and not isinstance(target_category_id, bool)
    ):
        proposal["target_category_id"] = target_category_id
    elif isinstance(revision_payload, dict) and isinstance(
        revision_payload.get("target_category_id"), int
    ):
        proposal["target_category_id"] = revision_payload["target_category_id"]
    target_category_name = _clean_text(
        _first_present(
            arguments,
            "target_category_name",
            "targetCategoryName",
            "category_name",
            "categoryName",
        )
    )
    if target_category_name and not preserve_revision_structure:
        proposal["target_category_name"] = target_category_name
    elif isinstance(revision_payload, dict) and _clean_text(
        revision_payload.get("target_category_name")
    ):
        proposal["target_category_name"] = _clean_text(
            revision_payload.get("target_category_name")
        )
    recurring_schedule = arguments.get("recurring_schedule")
    recurring_schedule_updates = arguments.get("recurring_schedule_updates")
    base_recurring_schedule: dict[str, Any] | None = None
    if isinstance(revision_payload, dict):
        raw_base_recurring_schedule = revision_payload.get(
            "recurring_schedule",
            revision_payload.get("recurringSchedule"),
        )
        base_recurring_schedule = (
            raw_base_recurring_schedule
            if isinstance(raw_base_recurring_schedule, dict)
            else None
        )
        if isinstance(base_recurring_schedule, dict):
            if (
                isinstance(recurring_schedule, dict)
                and arguments.get("replace_recurring_schedule") is not True
                and not isinstance(recurring_schedule_updates, dict)
            ):
                return {
                    "error": (
                        "A proposal revision must put changed recurrence fields in "
                        "recurring_schedule_updates; the full recurring_schedule is preserved "
                        "unless replace_recurring_schedule is explicitly true."
                    ),
                    "proposal_kind": "branch",
                    "compiled": False,
                }
            merged_recurring_schedule = (
                {}
                if arguments.get("replace_recurring_schedule") is True
                else copy.deepcopy(base_recurring_schedule)
            )
            if (
                isinstance(recurring_schedule, dict)
                and arguments.get("replace_recurring_schedule") is True
            ):
                merged_recurring_schedule.update(copy.deepcopy(recurring_schedule))
            if isinstance(recurring_schedule_updates, dict):
                merged_recurring_schedule.update(copy.deepcopy(recurring_schedule_updates))
            recurring_schedule = merged_recurring_schedule
    if isinstance(recurring_schedule, dict):
        if isinstance(base_recurring_schedule, dict):
            base_target_temp_id = _clean_text(
                base_recurring_schedule.get(
                    "target_temp_id",
                    base_recurring_schedule.get("targetTempId"),
                )
            )
            revised_target_temp_id = _clean_text(
                recurring_schedule.get(
                    "target_temp_id",
                    recurring_schedule.get("targetTempId"),
                )
            )
            if (
                base_target_temp_id
                and revised_target_temp_id != base_target_temp_id
            ):
                return {
                    "error": (
                        "A proposal revision cannot replace its primary recurring task. "
                        "Preserve target_temp_id and revise only the requested schedule "
                        "fields; start a separate proposal for a different task."
                    ),
                    "proposal_kind": "branch",
                    "compiled": False,
                }

            # A conflict blocker is an affected existing resource, never the
            # recurrence target.  Even if a model requests the low-level
            # replacement escape hatch, do not allow it to reuse the target
            # temp id for a blocker and thereby clone that blocker's cadence
            # across every incoming occurrence.
            base_operations = revision_payload.get(
                "structural_operations",
                revision_payload.get("structuralOperations"),
            )

            def created_title_for_temp_id(
                operations: Any,
                temp_id: str | None,
            ) -> str | None:
                if not temp_id or not isinstance(operations, list):
                    return None
                for operation in operations:
                    if not isinstance(operation, dict):
                        continue
                    if _clean_text(operation.get("temp_id")) != temp_id:
                        continue
                    return _clean_text(operation.get("title"))
                return None

            base_target_title = created_title_for_temp_id(
                base_operations,
                base_target_temp_id,
            )
            revised_target_title = created_title_for_temp_id(
                structural_operations,
                base_target_temp_id,
            )
            recurring_plan = revision_payload.get(
                "recurring_schedule_plan",
                revision_payload.get("recurringSchedulePlan"),
            )
            displaced_titles: set[str] = set()
            if isinstance(recurring_plan, dict):
                conflict_resolutions = recurring_plan.get(
                    "conflict_resolutions",
                    recurring_plan.get("conflictResolutions"),
                )
                if isinstance(conflict_resolutions, list):
                    for resolution in conflict_resolutions:
                        blockers = (
                            resolution.get("blockers")
                            if isinstance(resolution, dict)
                            else None
                        )
                        if not isinstance(blockers, list):
                            continue
                        for blocker in blockers:
                            blocker_title = _clean_text(
                                blocker.get(
                                    "taskTitle",
                                    blocker.get("task_title"),
                                )
                                if isinstance(blocker, dict)
                                else None
                            )
                            if blocker_title:
                                displaced_titles.add(blocker_title.casefold())
            if (
                base_target_title
                and revised_target_title
                and revised_target_title.casefold() != base_target_title.casefold()
                and revised_target_title.casefold() in displaced_titles
            ):
                return {
                    "error": (
                        "A conflict blocker cannot become the recurring target of a "
                        "proposal revision. Preserve the incoming task and move only its "
                        "listed conflicting calendar blocks."
                    ),
                    "proposal_kind": "branch",
                    "compiled": False,
                }
        recurrence_days = recurring_schedule.get(
            "days_of_week",
            recurring_schedule.get("daysOfWeek"),
        )
        if isinstance(recurrence_days, list) and recurrence_days:
            recurring_schedule["frequency"] = "weekly"
        proposal["recurring_schedule"] = copy.deepcopy(recurring_schedule)
    if isinstance(revision_payload, dict) and not isinstance(created_task_schedule, dict):
        base_created_task_schedule = revision_payload.get(
            "created_task_schedule",
            revision_payload.get("createdTaskSchedule"),
        )
        if isinstance(base_created_task_schedule, dict):
            created_task_schedule = copy.deepcopy(base_created_task_schedule)
    if isinstance(created_task_schedule, dict):
        proposal["created_task_schedule"] = copy.deepcopy(created_task_schedule)
    if "target_node_id" not in proposal and "target_node_title" not in proposal:
        inferred_target = _infer_tree_proposal_target_from_operations(structural_operations)
        if "target_node_id" in inferred_target or "target_node_title" in inferred_target:
            proposal.update(inferred_target)
    if (
        "target_node_id" not in proposal
        and "target_node_title" not in proposal
        and "target_node_ids" not in proposal
        and "target_category_id" not in proposal
        and "target_category_name" not in proposal
    ):
        proposal.update(_infer_tree_proposal_target_from_operations(structural_operations))
    if (
        "target_node_id" not in proposal
        and "target_node_title" not in proposal
        and "target_node_ids" not in proposal
        and "target_category_id" not in proposal
        and "target_category_name" not in proposal
    ):
        return {
            "error": "propose_tree_operation requires a target task or target category.",
            "proposal_kind": "branch",
            "compiled": False,
        }
    return {
        "proposal_kind": "branch",
        "branch_proposal": proposal,
        "compiled": True,
    }


_DEFAULT_CATEGORY_AFFINITY_PATTERNS: dict[str, tuple[tuple[str, int], ...]] = {
    "home": (
        (r"\bconcierge\b", 5),
        (r"\b(?:cook|cooking|kitchen|grocer(?:y|ies)|laundry)\b", 4),
        (r"\b(?:home|house|household|apartment|furniture|cleaning)\b", 3),
    ),
    "personal growth": (
        (r"\b(?:learn|study|course|training|tutorial|certification)\b", 4),
        (r"\b(?:docker|kubernetes|skill|practice)\b", 3),
    ),
    "work": (
        (r"\b(?:client|applicant|application|interview|resume|office)\b", 4),
        (r"\b(?:job|report|presentation|spreadsheet|investor)\b", 3),
    ),
    "health": (
        (r"\b(?:doctor|dentist|workout|exercise|gym|medication)\b", 4),
        (r"\b(?:health|medical|therapy|appointment)\b", 3),
    ),
    "finance": (
        (r"\b(?:budget|invoice|tax|bank|payment|insurance|mortgage)\b", 4),
        (r"\b(?:finance|financial|investing|expense)\b", 3),
    ),
    "social": (
        (r"\b(?:friend|family|party|coffee chat|social)\b", 3),
    ),
    "leisure": (
        (r"\b(?:movie|game|hobby|vacation|travel|leisure)\b", 3),
    ),
}


def _proposal_category_affinity_text(proposal: dict[str, Any]) -> str:
    parts = [
        str(proposal.get(key) or "")
        for key in ("summary", "rationale")
    ]
    raw_operations = proposal.get("structural_operations")
    if isinstance(raw_operations, list):
        for operation in raw_operations:
            if not isinstance(operation, dict):
                continue
            parts.extend(
                str(operation.get(key) or "")
                for key in ("title", "notes")
            )
    return " ".join(parts).casefold()


def _default_category_affinity_score(category_name: str, text: str) -> int:
    patterns = _DEFAULT_CATEGORY_AFFINITY_PATTERNS.get(
        category_name.strip().casefold(),
        (),
    )
    return sum(weight for pattern, weight in patterns if re.search(pattern, text))


def _align_high_confidence_active_category(
    raw_proposal: dict[str, Any],
    *,
    active_category: Category | None,
    categories: list[Category],
) -> dict[str, Any]:
    """Correct an obvious active-workspace match without defaulting all creates."""

    if active_category is None or active_category.id is None:
        return raw_proposal
    raw_operations = raw_proposal.get("structural_operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        return raw_proposal
    create_operations = [
        operation
        for operation in raw_operations
        if isinstance(operation, dict)
        and normalize_structural_operation_kind(
            _first_present(operation, "kind", "operation", "op", "type", "action")
        )
        == "create_node"
    ]
    if not create_operations or len(create_operations) != len(raw_operations):
        return raw_proposal

    selected_id = _first_present(
        raw_proposal,
        "target_category_id",
        "targetCategoryId",
        "category_id",
        "categoryId",
    )
    selected_name = _clean_text(
        _first_present(
            raw_proposal,
            "target_category_name",
            "targetCategoryName",
            "category_name",
            "categoryName",
        )
    )
    selected_category = next(
        (
            category
            for category in categories
            if (
                isinstance(selected_id, int)
                and not isinstance(selected_id, bool)
                and category.id == selected_id
            )
            or (
                selected_name
                and category.name.strip().casefold() == selected_name.casefold()
            )
        ),
        None,
    )
    if selected_category is None or selected_category.id == active_category.id:
        return raw_proposal

    affinity_text = _proposal_category_affinity_text(raw_proposal)
    active_score = _default_category_affinity_score(
        active_category.name,
        affinity_text,
    )
    selected_score = _default_category_affinity_score(
        selected_category.name,
        affinity_text,
    )
    if active_score < 4 or selected_score > 0:
        return raw_proposal

    aligned = copy.deepcopy(raw_proposal)
    aligned["target_category_id"] = int(active_category.id)
    aligned["target_category_name"] = active_category.name
    for operation in aligned["structural_operations"]:
        if isinstance(operation, dict):
            operation["category_id"] = int(active_category.id)
    return aligned


def _canonicalize_compiled_task_proposal(
    result: dict[str, Any],
    *,
    proposal_kind: str,
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None,
    categories: list[Category],
) -> dict[str, Any]:
    if result.get("compiled") is not True:
        return result
    raw_structural_operations: list[dict[str, Any]] = []
    raw_branch_proposal: dict[str, Any] | None = None
    if proposal_kind == "branch":
        raw_proposal = result.get("branch_proposal")
        if isinstance(raw_proposal, dict):
            raw_branch_proposal = raw_proposal
            raw_operations = raw_proposal.get("structural_operations")
            if isinstance(raw_operations, list):
                raw_structural_operations = [
                    operation for operation in raw_operations if isinstance(operation, dict)
                ]
    evidence_bundle = {
        "source": "typed_proposal_tool_validation",
        "active_category": (
            {"category_id": active_category.id, "name": active_category.name}
            if active_category is not None
            else None
        ),
        "available_categories": [
            {"category_id": category.id, "name": category.name}
            for category in categories
            if category.id is not None
        ],
    }
    raw_schedule_contract = (
        raw_branch_proposal.get(
            "created_task_schedule",
            raw_branch_proposal.get("createdTaskSchedule"),
        )
        if raw_branch_proposal is not None
        else None
    )
    raw_schedule_target_ids = (
        raw_branch_proposal.get(
            "target_node_ids",
            raw_branch_proposal.get("targetNodeIds"),
        )
        if raw_branch_proposal is not None
        else None
    )
    existing_schedule_only = bool(
        proposal_kind == "branch"
        and not raw_structural_operations
        and isinstance(raw_schedule_contract, dict)
        and isinstance(raw_schedule_target_ids, list)
        and raw_schedule_target_ids
    )
    if existing_schedule_only:
        valid_node_ids = {
            int(node.id)
            for node in nodes
            if isinstance(node.id, int) and not isinstance(node.id, bool)
        }
        requested_ids = {
            task_id
            for task_id in raw_schedule_target_ids
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        }
        if not requested_ids or requested_ids != set(raw_schedule_target_ids):
            return {
                "error": "Existing-task schedule targets must be canonical integer task ids.",
                "proposal_kind": proposal_kind,
                "compiled": False,
            }
        if not requested_ids.issubset(valid_node_ids):
            return {
                "error": "One or more existing-task schedule targets no longer exist.",
                "proposal_kind": proposal_kind,
                "compiled": False,
            }
        canonical = copy.deepcopy(raw_branch_proposal)
        response_key = "branch_proposal"
    elif proposal_kind == "metadata":
        canonical = normalize_metadata_proposal(
            result.get("metadata_proposal"),
            nodes,
            focus_node,
            evidence_bundle=evidence_bundle,
        )
        response_key = "metadata_proposal"
    else:
        canonical = normalize_branch_proposal(
            raw_branch_proposal,
            nodes,
            focus_node,
            evidence_bundle=evidence_bundle,
        )
        response_key = "branch_proposal"
    if canonical is None:
        return {
            "error": (
                "The typed proposal could not be resolved to exact existing task ids. "
                "Inspect or clarify the target, then call the proposal tool again."
            ),
            "proposal_kind": proposal_kind,
            "compiled": False,
        }
    if proposal_kind == "branch" and isinstance(raw_branch_proposal, dict):
        revision_of_proposal_id = _clean_text(
            raw_branch_proposal.get("revision_of_proposal_id")
        )
        if revision_of_proposal_id:
            # Normalization deliberately rebuilds the user-facing proposal, so
            # reattach revision lineage after it. Persistence and regression
            # checks can then prove the new card superseded the intended base.
            canonical["revision_of_proposal_id"] = revision_of_proposal_id
    if proposal_kind == "branch" and raw_structural_operations:
        canonical_operations = canonical.get("structural_operations")
        canonical_operation_count = (
            len(canonical_operations) if isinstance(canonical_operations, list) else 0
        )
        if canonical_operation_count != len(raw_structural_operations):
            return {
                "error": (
                    "One or more requested tree operations were lost while compiling the "
                    "proposal. Revise or normalize the unsupported operations, then submit "
                    "the complete batch again."
                ),
                "proposal_kind": proposal_kind,
                "compiled": False,
                "operation_conservation": {
                    "status": "invalid",
                    "input_operation_count": len(raw_structural_operations),
                    "compiled_operation_count": canonical_operation_count,
                    "input_operation_kinds": [
                        str(
                            operation.get("kind")
                            or operation.get("operation")
                            or operation.get("op")
                            or "unknown"
                        )
                        for operation in raw_structural_operations
                    ],
                },
                "model_instruction": (
                    "Do not hand an incomplete proposal to the user. Preserve every operation "
                    "from the requested batch, use a supported typed operation, and submit the "
                    "complete batch by calling the "
                    "proposal tool again in this turn."
                ),
            }
    task_patch = build_task_patch_draft(
        proposal_kind=proposal_kind,
        proposal=canonical,
    )
    validation = (
        {
            "status": "valid",
            "errors": [],
            "warnings": [],
            "operation_count": 0,
        }
        if existing_schedule_only
        else validate_task_patch(
            task_patch,
            nodes,
            category_ids={
                int(category.id)
                for category in categories
                if isinstance(category.id, int)
            },
        )
    )
    simulation = simulate_task_patch(task_patch, nodes)
    if validation.get("status") == "invalid":
        validation_errors = [
            str(item).strip()
            for item in validation.get("errors", [])
            if str(item).strip()
        ]
        return {
            "error": (
                "The complete proposed batch did not pass TaskPatch validation. "
                "Revise the operations and call the proposal tool again."
                + (
                    " Validation errors: " + "; ".join(validation_errors[:6])
                    if validation_errors
                    else ""
                )
            ),
            "proposal_kind": proposal_kind,
            "compiled": False,
            "task_patch": task_patch,
            "task_patch_validation": validation,
            "task_patch_simulation": simulation,
            "model_instruction": (
                "Do not hand this proposal to the user. Fix every validation error, then "
                "submit the complete batch again in this turn."
            ),
        }
    canonical["task_patch"] = task_patch
    canonical["task_patch_validation"] = validation
    canonical["task_patch_simulation"] = simulation
    canonical["task_patch_status"] = validation.get("status")
    if proposal_kind == "branch":
        canonical["operation_conservation"] = {
            "status": "valid",
            "input_operation_count": len(raw_structural_operations),
            "compiled_operation_count": len(task_patch.get("operations") or []),
        }
    canonical_result = dict(result)
    canonical_result[response_key] = canonical
    canonical_result["canonical"] = True
    return canonical_result


def _infer_tree_proposal_target_from_operations(
    structural_operations: list[dict[str, Any]],
) -> dict[str, Any]:
    for operation in structural_operations:
        target_node_id = _first_present(
            operation,
            "parent_id",
            "parentId",
            "parent_task_id",
            "parentTaskId",
            "destination_parent_id",
            "destinationParentId",
            "destination_parent_task_id",
            "destinationParentTaskId",
            "node_id",
            "nodeId",
            "task_id",
            "taskId",
            "source_task_id",
            "sourceTaskId",
        )
        if isinstance(target_node_id, int) and not isinstance(target_node_id, bool):
            return {"target_node_id": target_node_id}
        target_node_title = _clean_text(
            _first_present(
                operation,
                "parent_task_title",
                "parentTaskTitle",
                "parent_title",
                "parentTitle",
                "destination_parent_task_title",
                "destinationParentTaskTitle",
                "destination_parent_title",
                "destinationParentTitle",
                "target_task_title",
                "targetTaskTitle",
                "node_title",
                "nodeTitle",
                "source_task_title",
                "sourceTaskTitle",
            )
        )
        if target_node_title:
            return {"target_node_title": target_node_title}
        target_category_id = _first_present(
            operation,
            "category_id",
            "categoryId",
            "target_category_id",
            "targetCategoryId",
        )
        if isinstance(target_category_id, int) and not isinstance(target_category_id, bool):
            return {"target_category_id": target_category_id}
        target_category_name = _clean_text(
            _first_present(
                operation,
                "category_name",
                "categoryName",
                "target_category_name",
                "targetCategoryName",
            )
        )
        if target_category_name:
            return {"target_category_name": target_category_name}
    return {}


def _normalize_tree_operation_tool_argument(
    raw_operation: dict[str, Any],
    *,
    created_temp_positions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Normalize model-friendly operation arguments at the tool boundary."""

    operation = copy.deepcopy(raw_operation)
    # ``task`` is a natural provider spelling for the payload of task.create.
    # Normalize it to the single stored ``node`` representation before strict
    # validation so semantic intent does not require another model round trip.
    if not isinstance(operation.get("node"), dict) and isinstance(
        operation.get("task"),
        dict,
    ):
        operation["node"] = copy.deepcopy(operation["task"])
    operation.pop("task", None)
    raw_task_fields = operation.pop(
        "task_fields",
        operation.pop("taskFields", None),
    )
    if isinstance(raw_task_fields, dict):
        existing_fields = (
            copy.deepcopy(operation["fields"])
            if isinstance(operation.get("fields"), dict)
            else {}
        )
        operation["fields"] = {
            **copy.deepcopy(raw_task_fields),
            **existing_fields,
        }
    nested_node = operation.get("node")
    if isinstance(nested_node, dict):
        for key in (
            "title",
            "category_id",
            "categoryId",
            "status",
            "priority",
            "planned_date",
            "deadline_at",
            "deadlineAt",
            "notes",
            "node_type",
            "nodeType",
            "time_estimate_minutes",
            "timeEstimateMinutes",
            "estimate_minutes",
            "estimated_duration_minutes",
            "duration_minutes",
            "tags",
        ):
            if operation.get(key) in (None, "", [], {}) and nested_node.get(key) not in (None, "", [], {}):
                operation[key] = copy.deepcopy(nested_node[key])
        operation.pop("node", None)
    if "kind" not in operation and isinstance(operation.get("operation"), str):
        operation["kind"] = operation["operation"]
    if "kind" not in operation and isinstance(operation.get("op"), str):
        operation["kind"] = operation["op"]
    if "kind" not in operation and isinstance(operation.get("type"), str):
        operation["kind"] = operation["type"]
    if "kind" not in operation and isinstance(operation.get("action"), str):
        operation["kind"] = operation["action"]
    operation.pop("type", None)
    operation.pop("action", None)
    raw_kind = operation.get("kind")
    canonical_kind = normalize_structural_operation_kind(raw_kind)
    if canonical_kind:
        operation["kind"] = canonical_kind
    # JSON providers sometimes serialize identifiers as digit strings. The
    # strict models coerce them later, but semantic normalization (especially
    # parent/category alignment) must see the same identifier type first.
    for id_key in (
        "node_id",
        "task_id",
        "source_task_id",
        "category_id",
        "parent_id",
        "parent_task_id",
        "destination_parent_id",
        "destination_parent_task_id",
        "destination_category_id",
    ):
        raw_id = operation.get(id_key)
        if isinstance(raw_id, str) and raw_id.strip().isdigit():
            operation[id_key] = int(raw_id.strip())
    target_task_id = operation.pop(
        "target_task_id",
        operation.pop("targetTaskId", operation.pop("target_node_id", None)),
    )
    if (
        canonical_kind != "create_node"
        and operation.get("node_id") in (None, "")
        and operation.get("task_id") in (None, "")
        and operation.get("source_task_id") in (None, "")
        and isinstance(target_task_id, int)
        and not isinstance(target_task_id, bool)
    ):
        operation["node_id"] = target_task_id
    if operation.get("node_type") in (None, ""):
        task_type = operation.pop("task_type", operation.pop("taskType", None))
        if task_type not in (None, ""):
            operation["node_type"] = task_type
    else:
        operation.pop("task_type", None)
        operation.pop("taskType", None)
    # Provider-side planning annotations are useful while constructing a plan,
    # but are not stored operation fields. Preserve an explicit expected parent
    # as the canonical parent when no stronger parent reference was supplied.
    expected_parent_id = operation.pop(
        "expected_parent_task_id",
        operation.pop("expectedParentTaskId", None),
    )
    if (
        canonical_kind == "create_node"
        and operation.get("parent_id") in (None, "")
        and operation.get("parent_task_id") in (None, "")
        and isinstance(expected_parent_id, int)
        and not isinstance(expected_parent_id, bool)
    ):
        operation["parent_id"] = expected_parent_id
    new_parent_temp_id = operation.pop(
        "new_parent_temp_id",
        operation.pop("newParentTempId", None),
    )
    if (
        canonical_kind == "move_subtree"
        and operation.get("destination_parent_temp_id") in (None, "")
        and _clean_text(new_parent_temp_id)
    ):
        operation["destination_parent_temp_id"] = _clean_text(new_parent_temp_id)
    new_parent_id = operation.pop(
        "new_parent_task_id",
        operation.pop("newParentTaskId", operation.pop("new_parent_id", None)),
    )
    if (
        canonical_kind == "move_subtree"
        and operation.get("destination_parent_id") in (None, "")
        and isinstance(new_parent_id, int)
        and not isinstance(new_parent_id, bool)
    ):
        operation["destination_parent_id"] = new_parent_id
    operation.pop("scope_note", None)
    operation.pop("scopeNote", None)
    independent_root = operation.pop(
        "independent_root",
        operation.pop("independentRoot", None),
    )
    if canonical_kind == "create_node" and independent_root is True:
        # Top-level is represented by the absence of a parent reference.
        operation.pop("parent_id", None)
        operation.pop("parent_task_id", None)
        operation.pop("parent_temp_id", None)
    # Relative sibling placement is a natural way for a model to express an
    # ordered batch of new children.  The canonical operation uses a zero-based
    # target_index, so resolve that relationship while the complete batch is
    # still available instead of rejecting or silently dropping the ordering.
    after_temp_id = _clean_text(
        operation.pop("after_temp_id", operation.pop("afterTempId", None))
    )
    if (
        canonical_kind == "create_node"
        and operation.get("target_index") in (None, "")
        and after_temp_id
        and created_temp_positions is not None
        and after_temp_id in created_temp_positions
    ):
        operation["target_index"] = created_temp_positions[after_temp_id] + 1
    # Dependencies between newly-created tasks are translated into typed
    # scheduling order constraints by execute_kernel_tool, where the complete
    # plan and its schedule are available. They must not leak into strict task
    # mutation validation.
    operation.pop("depends_on_temp_ids", None)
    operation.pop("dependsOnTempIds", None)
    if operation.get("deadline_at") in (None, ""):
        raw_deadline = operation.pop(
            "due_date",
            operation.pop(
                "dueDate",
                operation.pop(
                    "due_at",
                    operation.pop("dueAt", operation.pop("deadline", None)),
                ),
            ),
        )
        if raw_deadline not in (None, ""):
            operation["deadline_at"] = raw_deadline
    raw_schedule_hint = operation.pop("schedule", None)
    if canonical_kind == "create_node" and isinstance(raw_schedule_hint, dict):
        for key in (
            "time_estimate_minutes",
            "estimate_minutes",
            "estimated_duration_minutes",
            "duration_minutes",
        ):
            if (
                operation.get(key) in (None, "")
                and raw_schedule_hint.get(key) not in (None, "")
            ):
                operation[key] = copy.deepcopy(raw_schedule_hint[key])
                break
    nested_fields = operation.pop("fields", None)
    if canonical_kind == "create_node" and isinstance(nested_fields, dict):
        for key in (
            "title",
            "category_id",
            "categoryId",
            "category_name",
            "categoryName",
            "status",
            "priority",
            "planned_date",
            "deadline_at",
            "deadlineAt",
            "notes",
            "node_type",
            "nodeType",
            "time_estimate_minutes",
            "timeEstimateMinutes",
            "estimate_minutes",
            "estimateMinutes",
            "estimated_duration_minutes",
            "estimatedDurationMinutes",
            "duration_minutes",
            "durationMinutes",
            "tags",
        ):
            if (
                operation.get(key) in (None, "", [], {})
                and nested_fields.get(key) not in (None, "", [], {})
            ):
                operation[key] = copy.deepcopy(nested_fields[key])
    raw_estimate = operation.pop("estimate", None)
    raw_estimate_minutes = operation.pop(
        "estimate_minutes",
        operation.pop("estimateMinutes", None),
    )
    raw_estimated_duration_minutes = operation.pop(
        "estimated_duration_minutes",
        operation.pop("estimatedDurationMinutes", None),
    )
    raw_estimated_minutes = operation.pop(
        "estimated_minutes",
        operation.pop("estimatedMinutes", None),
    )
    raw_duration_minutes = operation.pop(
        "duration_minutes",
        operation.pop("durationMinutes", None),
    )
    raw_time_estimate_minutes = operation.pop("timeEstimateMinutes", None)
    if operation.get("time_estimate_minutes") in (None, ""):
        estimate_value = (
            raw_time_estimate_minutes
            if raw_time_estimate_minutes not in (None, "")
            else raw_estimate_minutes
            if raw_estimate_minutes not in (None, "")
            else raw_estimated_duration_minutes
            if raw_estimated_duration_minutes not in (None, "")
            else raw_estimated_minutes
            if raw_estimated_minutes not in (None, "")
            else raw_duration_minutes
            if raw_duration_minutes not in (None, "")
            else raw_estimate
        )
        estimate_minutes = _duration_value_minutes(estimate_value)
        if estimate_minutes is not None:
            operation["time_estimate_minutes"] = estimate_minutes
    raw_ordering = operation.pop("ordering", None)
    sequence = operation.pop(
        "sequence",
        operation.pop(
            "order",
            operation.pop(
                "position",
                _first_present(raw_ordering, "index", "position", "order", "sequence")
                if isinstance(raw_ordering, dict)
                else raw_ordering,
            ),
        ),
    )
    if operation.get("target_index") in (None, "") and isinstance(
        sequence,
        int,
    ) and not isinstance(sequence, bool):
        operation["target_index"] = max(0, sequence - 1)
    if "source_task_title" not in operation:
        source_title = _clean_text(
            _first_present(
                operation,
                "task_title",
                "taskTitle",
                "target_task_title",
                "targetTaskTitle",
                "target_node_title",
                "targetNodeTitle",
                "node_title",
                "nodeTitle",
            )
        )
        if source_title:
            operation["source_task_title"] = source_title
    for legacy_key in (
        "task_title",
        "taskTitle",
        "target_task_title",
        "targetTaskTitle",
        "target_node_title",
        "targetNodeTitle",
        "node_title",
        "nodeTitle",
    ):
        operation.pop(legacy_key, None)
    if canonical_kind == "metadata_update":
        updates = (
            copy.deepcopy(operation.get("updates"))
            if isinstance(operation.get("updates"), dict)
            else {}
        )
        for field_name in (
            "title",
            "notes",
            "planned_date",
            "deadline_at",
            "priority",
            "status",
            "time_estimate_minutes",
            "tags",
            "node_type",
            "is_pinned",
            "is_backlog",
        ):
            value = operation.get(field_name)
            if value not in (None, "", [], {}):
                updates.setdefault(field_name, copy.deepcopy(value))
        if updates:
            if updates.get("deadline_at") in (None, ""):
                raw_nested_deadline = updates.pop(
                    "deadline",
                    updates.pop("due_date", updates.pop("dueDate", None)),
                )
                if raw_nested_deadline not in (None, ""):
                    updates["deadline_at"] = raw_nested_deadline
            operation["updates"] = updates
    # These are provider explanations of semantics already encoded by the
    # canonical operation kind. A move is always subtree-preserving and task
    # qualifications do not change archive/delete execution.
    operation.pop("preserve_descendants", None)
    operation.pop("preserveDescendants", None)
    operation.pop("preserve_subtree", None)
    operation.pop("preserveSubtree", None)
    operation.pop("qualification", None)
    operation.pop("context", None)
    # Planning annotations describe why/how the provider selected an operation;
    # they are not executable task fields.  The typed kind and canonical fields
    # retain the action itself, while top-level rationale retains explanations.
    for annotation_key in (
        "mode",
        "reason",
        "disambiguation",
        "explanation",
        "rationale",
    ):
        operation.pop(annotation_key, None)
    permanent = operation.pop("permanent", None)
    if canonical_kind == "delete_node" and permanent is False:
        operation["kind"] = "archive_node"
    # The provider may attach arbitrary planning metadata to an otherwise
    # complete operation. Once all supported semantic aliases have been
    # extracted, project to the executable mutation contract. Required target
    # and payload fields still fail strict validation; annotations no longer
    # manufacture repair turns.
    allowed_operation_keys = set(STRUCTURAL_OPERATION_INPUT_SCHEMA["properties"])
    return {
        key: value
        for key, value in operation.items()
        if key in allowed_operation_keys
    }


_SCHEDULING_CONSTRAINT_KIND_ALIASES = {
    "time_window": "allowed_window",
    "availability_window": "allowed_window",
    "date_range": "allowed_window",
    "available_window": "allowed_window",
    "blocked_time": "unavailable_interval",
    "busy_interval": "unavailable_interval",
    "unavailable_window": "unavailable_interval",
    "blocked_window": "unavailable_interval",
    "blocked_interval": "unavailable_interval",
    "unavailable": "unavailable_interval",
    "protected_time": "unavailable_interval",
    "protected_interval": "unavailable_interval",
    "fixed_start": "exact_start",
    "exact_time": "exact_start",
    "fixed_time": "exact_start",
    "must_start_at": "exact_start",
    "ordering": "task_order",
    "order": "task_order",
    "sequence": "task_order",
    "dependency": "task_order",
    "depends_on": "task_order",
    "gap": "minimum_gap",
    "buffer": "minimum_gap",
    "no_split": "contiguous",
    "unsplittable": "contiguous",
}
_SCHEDULING_CONSTRAINT_KINDS = {
    "unavailable_interval",
    "allowed_window",
    "exact_start",
    "task_order",
    "minimum_gap",
    "contiguous",
}


def _normalize_request_constraint_tool_argument(
    raw_constraint: dict[str, Any],
    *,
    default_source_text: str | None = None,
) -> dict[str, Any]:
    """Project equivalent provider shapes into the typed scheduling contract."""

    constraint = copy.deepcopy(raw_constraint)

    def move(target: str, *aliases: str) -> None:
        if constraint.get(target) not in (None, "", [], {}):
            for alias in aliases:
                constraint.pop(alias, None)
            return
        for alias in aliases:
            value = constraint.pop(alias, None)
            if value not in (None, "", [], {}):
                constraint[target] = value
                break

    move("kind", "type", "constraint_type", "constraintType", "name")
    move("source_text", "sourceText", "reason", "description")
    move("task_ids", "taskIds")
    move("target_temp_ids", "targetTempIds", "temp_ids", "tempIds")
    move(
        "start_at",
        "startAt",
        "start",
        "start_time",
        "startTime",
        "local_start_time",
        "localStartTime",
        "window_start",
        "windowStart",
        "exact_start",
        "exactStart",
        "time",
    )
    move(
        "end_at",
        "endAt",
        "end",
        "end_time",
        "endTime",
        "local_end_time",
        "localEndTime",
        "window_end",
        "windowEnd",
    )
    move("before_task_id", "beforeTaskId")
    move("after_task_id", "afterTaskId")
    move("before_temp_id", "beforeTempId")
    move("after_temp_id", "afterTempId")
    move("days_of_week", "daysOfWeek")
    move("minutes", "gap_minutes", "gapMinutes", "duration_minutes", "durationMinutes")

    raw_kind = str(constraint.get("kind") or "").strip().casefold().replace("-", "_")
    canonical_kind = _SCHEDULING_CONSTRAINT_KIND_ALIASES.get(raw_kind, raw_kind)
    if canonical_kind not in _SCHEDULING_CONSTRAINT_KINDS:
        kind_tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", raw_kind)
            if token
        }
        if kind_tokens & {
            "unavailable", "unavailability", "blocked", "busy", "avoid",
            "conflict", "protected", "nooverlap",
        }:
            canonical_kind = "unavailable_interval"
        elif kind_tokens & {
            "allowed", "available", "within", "between", "window", "fit",
            "preferred", "preference", "deadline", "notbefore", "notafter",
        } or {"not", "before"} <= kind_tokens or {"not", "after"} <= kind_tokens:
            canonical_kind = "allowed_window"
        elif kind_tokens & {"exact", "fixed", "anchored"} and kind_tokens & {
            "start", "time", "appointment", "commitment", "event",
        }:
            canonical_kind = "exact_start"
        elif kind_tokens & {"order", "ordering", "before", "after", "dependency", "sequence"}:
            canonical_kind = "task_order"
        elif kind_tokens & {"gap", "buffer", "spacing"}:
            canonical_kind = "minimum_gap"
        elif kind_tokens & {"contiguous", "unsplittable", "continuous"} or {
            "no", "split",
        } <= kind_tokens:
            canonical_kind = "contiguous"
    if canonical_kind not in _SCHEDULING_CONSTRAINT_KINDS:
        has_order_refs = any(
            constraint.get(key) not in (None, "")
            for key in (
                "before_task_id",
                "after_task_id",
                "before_temp_id",
                "after_temp_id",
            )
        )
        if has_order_refs:
            canonical_kind = "task_order"
        elif isinstance(constraint.get("minutes"), int):
            canonical_kind = "minimum_gap"
        elif constraint.get("start_at") not in (None, ""):
            canonical_kind = (
                "allowed_window"
                if constraint.get("end_at") not in (None, "")
                else "exact_start"
            )
    if canonical_kind not in _SCHEDULING_CONSTRAINT_KINDS:
        # This object is a planner directive or explanation, not an executable
        # scheduling constraint. Its prose remains in the handoff instruction;
        # returning an empty projection prevents it from masquerading as a
        # malformed hard constraint and blocking the whole plan.
        return {}
    if canonical_kind:
        constraint["kind"] = canonical_kind
    if canonical_kind in _SCHEDULING_CONSTRAINT_KINDS:
        constraint.setdefault("hard", True)
        if not _clean_text(constraint.get("label")):
            constraint["label"] = canonical_kind.replace("_", " ").title()
        if not _clean_text(constraint.get("source_text")):
            constraint["source_text"] = (
                _clean_text(default_source_text)
                or constraint["label"]
            )
    recurrence = str(constraint.get("recurrence") or "").strip().casefold()
    if "week" in recurrence:
        # Constraint recurrence describes a daily clock window filtered by
        # days_of_week; it is not the task-series cadence contract.
        constraint["recurrence"] = "daily"

    allowed_keys = set(CREATED_TASK_REQUEST_CONSTRAINT_SCHEMA["properties"])
    return {
        key: value
        for key, value in constraint.items()
        if key in allowed_keys
    }


def _normalize_created_task_schedule_tool_argument(
    raw_schedule: dict[str, Any],
    *,
    default_instruction: str | None = None,
) -> dict[str, Any]:
    """Normalize common model spellings before strict schedule validation."""

    schedule = copy.deepcopy(raw_schedule)

    def move(source: dict[str, Any], target: str, *aliases: str) -> None:
        if source.get(target) not in (None, "", [], {}):
            for alias in aliases:
                source.pop(alias, None)
            return
        selected: Any = None
        for alias in aliases:
            value = source.pop(alias, None)
            if selected in (None, "", [], {}) and value not in (
                None,
                "",
                [],
                {},
            ):
                selected = value
        if selected not in (None, "", [], {}):
            source[target] = selected

    def normalize_group(raw_group: dict[str, Any]) -> dict[str, Any]:
        group = copy.deepcopy(raw_group)
        raw_placement = copy.deepcopy(group.get("placement"))
        raw_strict = group.get("strict") is True
        if isinstance(raw_placement, dict):
            raw_placement_mode = str(
                _first_present(
                    raw_placement,
                    "mode",
                    "scheduling_mode",
                    "schedulingMode",
                )
                or ""
            ).strip().casefold()
            raw_strict = raw_strict or raw_placement_mode in {
                "fixed",
                "exact",
                "strict",
                "anchored",
            }
        raw_semantic_constraints = group.get("constraints")
        if (
            group.get("request_constraints") in (None, "", [], {})
            and group.get("requestConstraints") in (None, "", [], {})
            and (
                isinstance(raw_semantic_constraints, dict)
                or (
                    isinstance(raw_semantic_constraints, list)
                    and raw_semantic_constraints
                    and all(
                        isinstance(item, dict)
                        for item in raw_semantic_constraints
                    )
                )
            )
        ):
            group["request_constraints"] = copy.deepcopy(
                raw_semantic_constraints
            )
            group.pop("constraints", None)
        move(
            group,
            "target_temp_ids",
            "targetTempIds",
            "task_temp_ids",
            "taskTempIds",
            "temp_ids",
            "tempIds",
            "targets",
            "tasks",
            "items",
            "task_ref",
            "taskRef",
            "task_refs",
            "taskRefs",
            "ordered_temp_ids",
            "orderedTempIds",
            "ordered_task_temp_ids",
            "orderedTaskTempIds",
        )
        singular_target = _first_present(
            group,
            "target_temp_id",
            "targetTempId",
            "task_temp_id",
            "taskTempId",
            "task_ref",
            "taskRef",
        )
        for alias in (
            "target_temp_id",
            "targetTempId",
            "task_temp_id",
            "taskTempId",
            "task_ref",
            "taskRef",
        ):
            group.pop(alias, None)
        if group.get("target_temp_ids") in (None, "", [], {}) and singular_target not in (
            None,
            "",
        ):
            group["target_temp_ids"] = [singular_target]

        raw_targets = group.get("target_temp_ids")
        if isinstance(raw_targets, str):
            group["target_temp_ids"] = [raw_targets]
        elif isinstance(raw_targets, list):
            normalized_targets: list[str] = []
            for target in raw_targets:
                if isinstance(target, str) and target.strip():
                    normalized_targets.append(target.strip())
                    continue
                if not isinstance(target, dict):
                    continue
                target_id = _clean_text(
                    _first_present(
                        target,
                        "target_temp_id",
                        "targetTempId",
                        "task_temp_id",
                        "taskTempId",
                        "temp_id",
                        "tempId",
                        "id",
                    )
                )
                if target_id:
                    normalized_targets.append(target_id)
            group["target_temp_ids"] = normalized_targets
        elif raw_targets in (None, "", [], {}):
            group.pop("target_temp_ids", None)

        move(
            group,
            "target_task_ids",
            "targetTaskIds",
            "existing_task_ids",
            "existingTaskIds",
            "persisted_task_ids",
            "persistedTaskIds",
        )
        raw_task_ids = group.get("target_task_ids")
        if isinstance(raw_task_ids, int) and not isinstance(raw_task_ids, bool):
            group["target_task_ids"] = [raw_task_ids]
        elif isinstance(raw_task_ids, list):
            group["target_task_ids"] = list(
                dict.fromkeys(
                    int(task_id)
                    for task_id in raw_task_ids
                    if isinstance(task_id, int) and not isinstance(task_id, bool)
                )
            )
        elif raw_task_ids in (None, "", [], {}):
            group.pop("target_task_ids", None)

        move(group, "request_constraints", "requestConstraints")
        raw_constraints = group.get("request_constraints")
        if isinstance(raw_constraints, dict):
            raw_constraints = [raw_constraints]
        if isinstance(raw_constraints, list):
            normalized_constraints: list[dict[str, Any]] = []
            for raw_constraint in raw_constraints:
                if not isinstance(raw_constraint, dict):
                    continue
                constraint = _normalize_request_constraint_tool_argument(
                    raw_constraint,
                    default_source_text=(
                        _clean_text(group.get("instruction"))
                        or default_instruction
                    ),
                )
                group_temp_targets = group.get("target_temp_ids")
                group_task_targets = group.get("target_task_ids")
                group_target_count = (
                    len(group_temp_targets)
                    if isinstance(group_temp_targets, list)
                    else 0
                ) + (
                    len(group_task_targets)
                    if isinstance(group_task_targets, list)
                    else 0
                )
                has_single_created_owner = (
                    isinstance(group_temp_targets, list)
                    and len(group_temp_targets) == 1
                )
                if (
                    constraint.get("kind") == "unavailable_interval"
                    and constraint.get("start_at") not in (None, "")
                    and constraint.get("end_at") in (None, "")
                    and (group_target_count == 1 or has_single_created_owner)
                ):
                    # A one-bound interval cannot describe a blocker. Inside a
                    # single-owner schedule group it does, however, carry the
                    # complete shape of an exact start for that owner. Repair
                    # that provider kind mismatch while leaving genuinely
                    # incomplete external blockers (zero/multiple owners)
                    # invalid and visible to the validator.
                    constraint["kind"] = "exact_start"
                    constraint["label"] = "Exact Start"
                if (
                    constraint.get("kind") == "exact_start"
                    and (
                        len(constraint.get("target_temp_ids") or [])
                        + len(constraint.get("task_ids") or [])
                    )
                    > 1
                    and _first_present(
                        group,
                        "window_start",
                        "windowStart",
                        "start_at",
                        "startAt",
                    )
                    not in (None, "")
                    and _first_present(
                        group,
                        "window_end",
                        "windowEnd",
                        "end_at",
                        "endAt",
                    )
                    not in (None, "")
                ):
                    # The group window already owns placement for this task
                    # set. An exact-start constraint cannot legally target
                    # several tasks, so omit only that redundant artifact.
                    continue
                if (
                    str(constraint.get("kind") or "").strip()
                    and isinstance(constraint.get("hard"), bool)
                    and str(constraint.get("label") or "").strip()
                    and str(constraint.get("source_text") or "").strip()
                ):
                    normalized_constraints.append(constraint)
            if normalized_constraints:
                group["request_constraints"] = normalized_constraints
            else:
                group.pop("request_constraints", None)
        else:
            group.pop("request_constraints", None)

        move(
            group,
            "instruction",
            "scheduling_instruction",
            "schedulingInstruction",
            "description",
            "constraint",
            "constraints",
            "hard_constraints",
            "hardConstraints",
            "soft_constraints",
            "softConstraints",
            "prompt",
            "label",
        )
        raw_instruction = group.get("instruction")
        if not isinstance(raw_instruction, str):
            instruction_parts: list[str] = []

            def collect_instruction_parts(value: Any) -> None:
                if isinstance(value, str) and value.strip():
                    instruction_parts.append(" ".join(value.strip().split()))
                    return
                if isinstance(value, list):
                    for item in value:
                        collect_instruction_parts(item)
                    return
                if isinstance(value, dict):
                    preferred_keys = (
                        "text",
                        "summary",
                        "instruction",
                        "description",
                        "constraints",
                        "prompt",
                        "order",
                    )
                    preferred_values = [
                        value[key] for key in preferred_keys if key in value
                    ]
                    for item in preferred_values or list(value.values()):
                        collect_instruction_parts(item)

            collect_instruction_parts(raw_instruction)
            if instruction_parts:
                group["instruction"] = "; ".join(
                    dict.fromkeys(instruction_parts)
                )
            else:
                group.pop("instruction", None)
        raw_exact_start = _first_present(group, "exact_start", "exactStart")
        group.pop("exact_start", None)
        group.pop("exactStart", None)
        move(group, "window_start", "windowStart", "start_at", "startAt")
        if (
            group.get("window_start") in (None, "")
            and isinstance(raw_exact_start, str)
            and raw_exact_start.strip()
        ):
            group["window_start"] = raw_exact_start.strip()
        move(group, "window_end", "windowEnd", "end_at", "endAt")
        move(group, "slot_minutes", "slotMinutes")
        raw_slot_minutes = group.get("slot_minutes")
        if isinstance(raw_slot_minutes, bool) or not isinstance(
            raw_slot_minutes, int
        ):
            group.pop("slot_minutes", None)
        elif raw_slot_minutes < 1 or raw_slot_minutes > 60:
            # Slot size is solver resolution, not task duration. Providers
            # commonly copy a 90-minute meeting duration here. Preserve the
            # requested duration on the task and use the planner's safe grid.
            group["slot_minutes"] = 5
        raw_date = _clean_text(
            _first_present(group, "date", "local_date", "localDate")
        ) or _clean_text(
            _first_present(schedule, "date", "local_date", "localDate")
        ) or _clean_text(
            _first_present(
                raw_placement,
                "date",
                "local_date",
                "localDate",
            )
            if isinstance(raw_placement, dict)
            else None
        )
        raw_start_time = _clean_text(
            _first_present(
                group,
                "start_time",
                "startTime",
                "local_start_time",
                "localStartTime",
            )
        )
        raw_end_time = _clean_text(
            _first_present(
                group,
                "end_time",
                "endTime",
                "local_end_time",
                "localEndTime",
            )
        )
        if raw_date and raw_start_time and group.get("window_start") in (None, ""):
            group["window_start"] = f"{raw_date}T{raw_start_time}"
        if raw_date and raw_end_time and group.get("window_end") in (None, ""):
            group["window_end"] = f"{raw_date}T{raw_end_time}"
        for alias in (
            "local_date",
            "localDate",
            "start_time",
            "startTime",
            "local_start_time",
            "localStartTime",
            "end_time",
            "endTime",
            "local_end_time",
            "localEndTime",
        ):
            group.pop(alias, None)
        # Created-task estimates own scheduled duration. A group-level duration
        # is redundant and is not allowed to override those task estimates.
        group.pop("duration_minutes", None)
        group.pop("durationMinutes", None)
        group.pop("total_duration_minutes", None)
        group.pop("totalDurationMinutes", None)
        group.pop("durations_minutes", None)
        group.pop("durationsMinutes", None)
        for non_contract_hint in (
            "allow_gaps",
            "allowGaps",
            "anchored",
            "is_anchored",
            "isAnchored",
            "flexible",
            "is_flexible",
            "isFlexible",
            "flexibility",
            "strict",
            "contiguous",
            "date",
            "group_id",
            "groupId",
            "ordered",
            "order",
            "ordering",
            "placement",
            "placement_intent",
            "placementIntent",
            "scheduling_mode",
            "schedulingMode",
            "timezone",
            "time_zone",
            "timeZone",
        ):
            group.pop(non_contract_hint, None)

        raw_window = _first_present(group, "window", "time_window", "timeWindow")
        if not isinstance(raw_window, dict) and isinstance(raw_placement, dict):
            raw_window = raw_placement
        for alias in ("window", "time_window", "timeWindow"):
            group.pop(alias, None)
        if isinstance(raw_window, dict):
            if group.get("window_start") in (None, ""):
                group["window_start"] = _first_present(
                    raw_window,
                    "start",
                    "start_at",
                    "startAt",
                    "start_time",
                    "startTime",
                    "window_start",
                    "windowStart",
                )
            if group.get("window_end") in (None, ""):
                group["window_end"] = _first_present(
                    raw_window,
                    "end",
                    "end_at",
                    "endAt",
                    "end_time",
                    "endTime",
                    "window_end",
                    "windowEnd",
                )

        def normalize_boundary(value: Any) -> str | None:
            if isinstance(value, str) and value.strip():
                cleaned = value.strip()
                if (
                    raw_date
                    and "T" not in cleaned
                    and (
                        ":" in cleaned
                        or cleaned.upper().endswith(" AM")
                        or cleaned.upper().endswith(" PM")
                    )
                ):
                    cleaned = f"{raw_date}T{cleaned}"
                parsed_human_clock = False
                try:
                    parsed_boundary = datetime.fromisoformat(cleaned)
                except ValueError:
                    parsed_boundary = None
                    if "T" in cleaned:
                        boundary_date_text, boundary_clock_text = cleaned.split(
                            "T", 1
                        )
                        try:
                            boundary_date = date.fromisoformat(boundary_date_text)
                        except ValueError:
                            boundary_date = None
                        if boundary_date is not None:
                            for clock_format in (
                                "%I:%M %p",
                                "%I:%M:%S %p",
                                "%I %p",
                                "%H:%M",
                                "%H:%M:%S",
                            ):
                                try:
                                    parsed_clock = datetime.strptime(
                                        boundary_clock_text.strip().upper(),
                                        clock_format,
                                    ).time()
                                except ValueError:
                                    continue
                                parsed_boundary = datetime.combine(
                                    boundary_date,
                                    parsed_clock,
                                )
                                parsed_human_clock = True
                                break
                    if parsed_boundary is None:
                        return cleaned
                if (
                    parsed_boundary.tzinfo is not None
                    and parsed_boundary.utcoffset() is not None
                ):
                    # Drasil's scheduling contract is expressed in workspace
                    # local wall time. Provider offsets are transport metadata;
                    # normalize them before any comparison with persisted local
                    # calendar values or offset-free constraint aliases.
                    return parsed_boundary.replace(tzinfo=None).isoformat()
                return parsed_boundary.isoformat() if parsed_human_clock else cleaned
            if not isinstance(value, dict):
                return None
            direct = _clean_text(
                _first_present(
                    value,
                    "date_time",
                    "dateTime",
                    "datetime",
                    "local_date_time",
                    "localDateTime",
                    "iso",
                    "value",
                )
            )
            if direct:
                return direct
            boundary_date = _clean_text(
                _first_present(
                    value,
                    "date",
                    "local_date",
                    "localDate",
                )
            ) or raw_date
            boundary_time = _clean_text(
                _first_present(
                    value,
                    "time",
                    "local_time",
                    "localTime",
                    "start_time",
                    "startTime",
                    "end_time",
                    "endTime",
                )
            )
            if boundary_date and boundary_time:
                return f"{boundary_date}T{boundary_time}"
            return None

        for boundary_key in ("window_start", "window_end"):
            normalized_boundary = normalize_boundary(group.get(boundary_key))
            if normalized_boundary:
                group[boundary_key] = normalized_boundary
            elif group.get(boundary_key) not in (None, ""):
                group.pop(boundary_key, None)
        # A model-authored local date without clock bounds means flexible
        # placement on that date. The deterministic planner applies current
        # time, preferences, occupancy, ordering, and duration afterward.
        if raw_date and group.get("window_start") in (None, ""):
            group["window_start"] = f"{raw_date}T00:00"
        if raw_date and group.get("window_end") in (None, ""):
            group["window_end"] = f"{raw_date}T23:59"
        try:
            group_window_start = datetime.fromisoformat(
                str(group.get("window_start") or "")
            )
            group_window_end = datetime.fromisoformat(
                str(group.get("window_end") or "")
            )
        except ValueError:
            group_window_start = None
            group_window_end = None

        def normalize_constraint_boundary(value: Any) -> Any:
            if isinstance(value, dict):
                normalized_nested = normalize_boundary(value)
                return normalized_nested if normalized_nested is not None else value
            if not isinstance(value, str) or not value.strip():
                return value
            cleaned = value.strip()
            if (
                group_window_start is not None
                and "T" not in cleaned
                and ":" in cleaned
            ):
                cleaned = f"{group_window_start.date().isoformat()}T{cleaned}"
            normalized = normalize_boundary(cleaned)
            return normalized if normalized is not None else cleaned

        for constraint in group.get("request_constraints") or []:
            if not isinstance(constraint, dict):
                continue
            for boundary_key in ("start_at", "end_at"):
                if constraint.get(boundary_key) not in (None, ""):
                    constraint[boundary_key] = normalize_constraint_boundary(
                        constraint[boundary_key]
                    )
            if (
                constraint.get("kind") == "exact_start"
                and group_window_start is not None
                and group_window_end is not None
                and isinstance(constraint.get("start_at"), str)
            ):
                try:
                    exact_start = datetime.fromisoformat(
                        constraint["start_at"]
                    )
                except ValueError:
                    exact_start = None
                if exact_start is not None and not (
                    group_window_start <= exact_start < group_window_end
                ):
                    rebased_start = datetime.combine(
                        group_window_start.date(),
                        exact_start.time(),
                    )
                    if group_window_start <= rebased_start < group_window_end:
                        constraint["start_at"] = rebased_start.isoformat()
        owned_temp_ids = group.get("target_temp_ids")
        owned_task_ids = group.get("target_task_ids")
        owned_target_count = len(owned_temp_ids) if isinstance(
            owned_temp_ids, list
        ) else 0
        owned_target_count += len(owned_task_ids) if isinstance(
            owned_task_ids, list
        ) else 0
        if (
            raw_strict
            and owned_target_count == 1
            and group.get("window_start") not in (None, "")
        ):
            constraints = group.get("request_constraints")
            if not isinstance(constraints, list):
                constraints = []
                group["request_constraints"] = constraints
            exact_constraints = [
                constraint
                for constraint in constraints
                if isinstance(constraint, dict)
                and constraint.get("kind") == "exact_start"
            ]
            if not exact_constraints:
                exact_constraints = [
                    {
                        "kind": "exact_start",
                        "hard": True,
                        "label": "Exact start",
                        "source_text": (
                            _clean_text(default_instruction)
                            or "Preserve the fixed start for this schedule group."
                        ),
                    }
                ]
                constraints.extend(exact_constraints)
            for constraint in exact_constraints:
                # For a single strict group, the group window is the canonical
                # fixed-time contract. Keeping a conflicting duplicated start
                # would make an internally authored plan reject itself.
                constraint["start_at"] = group["window_start"]
                if not constraint.get("target_temp_ids") and isinstance(
                    owned_temp_ids, list
                ):
                    constraint["target_temp_ids"] = list(owned_temp_ids)
                if not constraint.get("task_ids") and isinstance(
                    owned_task_ids, list
                ):
                    constraint["task_ids"] = list(owned_task_ids)
        for constraint in group.get("request_constraints") or []:
            if not isinstance(constraint, dict):
                continue
            if constraint.get("kind") == "exact_start":
                has_explicit_target = bool(
                    constraint.get("target_temp_ids")
                    or constraint.get("task_ids")
                )
                if not has_explicit_target:
                    if isinstance(owned_temp_ids, list) and len(owned_temp_ids) == 1:
                        # In a mixed group, the sole new task is the incoming
                        # fixed event; existing task ids are conflict context.
                        constraint["target_temp_ids"] = list(owned_temp_ids)
                    elif (
                        not owned_temp_ids
                        and isinstance(owned_task_ids, list)
                        and len(owned_task_ids) == 1
                    ):
                        constraint["task_ids"] = list(owned_task_ids)
            if (
                constraint.get("kind") == "exact_start"
                and constraint.get("start_at") in (None, "")
                and group.get("window_start") not in (None, "")
            ):
                constraint["start_at"] = group["window_start"]
            if constraint.get("kind") == "allowed_window":
                if constraint.get("start_at") in (None, ""):
                    constraint["start_at"] = group.get("window_start")
                if constraint.get("end_at") in (None, ""):
                    constraint["end_at"] = group.get("window_end")
        if group.get("instruction") in (None, ""):
            group["instruction"] = (
                " ".join(default_instruction.strip().split())
                if isinstance(default_instruction, str)
                and default_instruction.strip()
                else "Schedule the referenced created tasks within this window."
            )
        # Provider annotations are not part of the semantic schedule contract.
        # Project to one canonical shape after extracting targets, instruction,
        # window, and slot size. This prevents harmless planning metadata from
        # forcing another model turn while required semantic fields still fail
        # closed in strict validation below.
        canonical_keys = {
            "target_temp_ids",
            "target_task_ids",
            "instruction",
            "window_start",
            "window_end",
            "slot_minutes",
            "request_constraints",
            "schedule_groups",
        }
        return {
            key: value
            for key, value in group.items()
            if key in canonical_keys
        }

    move(schedule, "schedule_groups", "scheduleGroups")
    direct_group = normalize_group(schedule)
    raw_groups = direct_group.get("schedule_groups")
    if isinstance(raw_groups, list):
        direct_group["schedule_groups"] = [
            normalize_group(group) if isinstance(group, dict) else group
            for group in raw_groups
        ]
        normalized_groups = [
            group
            for group in direct_group["schedule_groups"]
            if isinstance(group, dict)
        ]
        if len(normalized_groups) == 1:
            # Providers commonly use top-level targets as the complete ownership
            # set and one schedule_group for its shared placement window. The
            # planner previously ignored those top-level targets whenever groups
            # existed, silently dropping calendar operations. With one group the
            # ownership is unambiguous, so merge the complete set into it.
            sole_group = normalized_groups[0]
            for target_key in ("target_temp_ids", "target_task_ids"):
                combined = [
                    *(
                        direct_group.get(target_key)
                        if isinstance(direct_group.get(target_key), list)
                        else []
                    ),
                    *(
                        sole_group.get(target_key)
                        if isinstance(sole_group.get(target_key), list)
                        else []
                    ),
                ]
                if combined:
                    sole_group[target_key] = list(dict.fromkeys(combined))
            top_constraints = direct_group.get("request_constraints")
            if isinstance(top_constraints, list) and top_constraints:
                group_constraints = sole_group.get("request_constraints")
                if not isinstance(group_constraints, list):
                    group_constraints = []
                    sole_group["request_constraints"] = group_constraints
                for constraint in top_constraints:
                    if constraint not in group_constraints:
                        group_constraints.append(constraint)
            direct_group.pop("target_temp_ids", None)
            direct_group.pop("target_task_ids", None)
            direct_group.pop("request_constraints", None)
        if not direct_group.get("target_temp_ids"):
            direct_group.pop("instruction", None)
        for non_contract_hint in (
            "anchored",
            "is_anchored",
            "isAnchored",
            "date",
            "ordering",
            "placement",
            "scheduling_mode",
            "schedulingMode",
            "timezone",
            "time_zone",
            "timeZone",
        ):
            direct_group.pop(non_contract_hint, None)
    elif raw_groups in (None, "", [], {}):
        direct_group.pop("schedule_groups", None)
    return direct_group


def _normalize_goal_transition_tool_argument(
    raw_arguments: dict[str, Any],
) -> dict[str, Any]:
    """Accept common semantic goal shapes while preserving a strict stored contract."""

    arguments = copy.deepcopy(raw_arguments)
    disposition_aliases = {
        "new_goal": "new",
        "new-topic": "new",
        "new_topic": "new",
        "same_goal": "continue",
        "continuation": "continue",
        "correction": "correct",
    }
    raw_disposition = str(arguments.get("disposition") or "").strip().casefold()
    arguments["disposition"] = disposition_aliases.get(
        raw_disposition,
        raw_disposition,
    )
    raw_patches = arguments.get("patches")
    if isinstance(raw_patches, dict):
        raw_patches = [raw_patches]
    if not isinstance(raw_patches, list):
        raw_patches = []

    path_aliases = {
        "goal": "outcomes.desired",
        "outcome": "outcomes.desired",
        "outcomes": "outcomes.desired",
        "operations": "operations.requested",
        "requested_operations": "operations.requested",
        "workspace": "scope.workspace",
        "parent": "scope.parent",
        "duration": "temporal.duration_minutes",
        "duration_minutes": "temporal.duration_minutes",
        "date": "temporal.requested_date",
        "requested_date": "temporal.requested_date",
        "time": "temporal.requested_time",
        "requested_time": "temporal.requested_time",
        "window": "temporal.requested_window",
        "requested_window": "temporal.requested_window",
        "ordering": "temporal.ordering",
        "constraints": "temporal.requested_window",
    }

    normalized_patches: list[dict[str, Any]] = []

    def append_patch(path: str, value: Any, operation: str = "set") -> None:
        canonical_path = path_aliases.get(path, path)
        normalized_value = copy.deepcopy(value)
        if canonical_path == "outcomes.desired":
            if isinstance(normalized_value, str) and normalized_value.strip():
                normalized_value = [{"description": normalized_value.strip()}]
            elif isinstance(normalized_value, dict):
                normalized_value = [normalized_value]
            elif isinstance(normalized_value, list):
                normalized_value = [
                    item
                    if isinstance(item, dict)
                    else {"description": str(item).strip()}
                    for item in normalized_value
                    if isinstance(item, dict)
                    or (isinstance(item, str) and item.strip())
                ]
            if arguments.get("disposition") == "new" and not all(
                isinstance(item, dict)
                and bool(item.get("outcome_id") or item.get("outcomeId"))
                and isinstance(item.get("provenance"), dict)
                for item in normalized_value
            ):
                # The host already persists the complete current request as the
                # new goal. A descriptive provider shorthand is not a canonical
                # DesiredOutcome and must not create a reducer-time retry.
                return
        elif canonical_path == "operations.requested":
            if not isinstance(normalized_value, list):
                normalized_value = [normalized_value]
            operation_values: list[Any] = []
            for item in normalized_value:
                if not isinstance(item, dict):
                    operation_values.append(item)
                    continue
                operation_label = _first_present(
                    item,
                    "operation_kind",
                    "operationKind",
                    "kind",
                    "operation",
                    "type",
                    "action",
                )
                operation_values.append(
                    operation_label if operation_label is not None else str(item)
                )
            normalized_value = operation_values
        elif canonical_path == "temporal.requested_window":
            if isinstance(normalized_value, str) and normalized_value.strip():
                normalized_value = {"description": normalized_value.strip()}
            elif isinstance(normalized_value, list):
                normalized_value = {"constraints": normalized_value}
        normalized_patches.append(
            {
                "path": canonical_path,
                "operation": operation,
                "value": normalized_value,
            }
        )

    for raw_patch in raw_patches:
        if not isinstance(raw_patch, dict):
            continue
        if "path" in raw_patch:
            path = str(raw_patch.get("path") or "").strip()
            operation = str(
                _first_present(raw_patch, "operation", "op", "action") or "set"
            ).strip()
            append_patch(path, raw_patch.get("value"), operation)
            continue
        for legacy_key in path_aliases:
            if legacy_key in raw_patch and raw_patch[legacy_key] not in (
                None,
                "",
                [],
                {},
            ):
                append_patch(legacy_key, raw_patch[legacy_key])

    arguments["patches"] = normalized_patches
    if arguments.get("disposition") == "new":
        try:
            GoalTurnDecision.model_validate(arguments)
        except ValidationError:
            # The host retains the complete current user request for a new goal.
            # Falling back to an empty sparse delta loses no request text and
            # prevents a harmless provider-shape retry from blocking planning.
            arguments["patches"] = []
    return arguments


_WEEKDAY_ALIASES = {
    "mo": "monday",
    "mon": "monday",
    "monday": "monday",
    "tu": "tuesday",
    "tue": "tuesday",
    "tues": "tuesday",
    "tuesday": "tuesday",
    "we": "wednesday",
    "wed": "wednesday",
    "wednesday": "wednesday",
    "th": "thursday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "thursday": "thursday",
    "fr": "friday",
    "fri": "friday",
    "friday": "friday",
    "sa": "saturday",
    "sat": "saturday",
    "saturday": "saturday",
    "su": "sunday",
    "sun": "sunday",
    "sunday": "sunday",
}


def _duration_value_minutes(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    iso_match = re.fullmatch(
        r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?",
        text,
        re.IGNORECASE,
    )
    if iso_match:
        minutes = int(iso_match.group(1) or 0) * 60 + int(iso_match.group(2) or 0)
        return minutes if minutes > 0 else None
    return parse_prompt_estimate_minutes(text)


def _normalize_recurring_schedule_tool_argument(
    raw_schedule: dict[str, Any],
) -> dict[str, Any]:
    """Repair common model spellings without narrowing recurrence semantics."""

    schedule = copy.deepcopy(raw_schedule)
    raw_placement = schedule.pop("placement", None)
    if isinstance(raw_placement, dict):
        placement_aliases = {
            "preferred_start_time": (
                "preferred_start_time", "preferredStartTime", "local_time",
                "localTime", "start_time", "startTime", "time",
            ),
            "allowed_start_time": (
                "allowed_start_time", "allowedStartTime", "window_start_local",
                "windowStartLocal", "window_start", "windowStart",
            ),
            "allowed_end_time": (
                "allowed_end_time", "allowedEndTime", "window_end_local",
                "windowEndLocal", "window_end", "windowEnd",
            ),
        }
        for canonical, aliases in placement_aliases.items():
            value = _first_present(raw_placement, *aliases)
            if schedule.get(canonical) in (None, "") and value not in (None, ""):
                schedule[canonical] = value
        placement_mode = str(
            _first_present(raw_placement, "mode", "scheduling_mode", "schedulingMode")
            or ""
        ).strip().casefold()
        if placement_mode in {"fixed", "exact", "strict", "anchored"}:
            schedule.setdefault("strict", True)
        elif placement_mode in {"flexible", "preferred", "best_fit"}:
            schedule.setdefault("strict", False)

    raw_conflict_adjustments = schedule.pop(
        "conflict_adjustments", schedule.pop("conflictAdjustments", None)
    )
    conflict_values = (
        raw_conflict_adjustments
        if isinstance(raw_conflict_adjustments, list)
        else [raw_conflict_adjustments]
        if raw_conflict_adjustments not in (None, "", {})
        else []
    )
    for adjustment in conflict_values:
        candidate = (
            " ".join(str(value) for value in adjustment.values())
            if isinstance(adjustment, dict)
            else str(adjustment or "")
        ).casefold()
        if "before" in candidate:
            schedule.setdefault("conflict_relocation", "before_incoming")
            break
        if "after" in candidate:
            schedule.setdefault("conflict_relocation", "after_incoming")
            break

    def move(target: str, *aliases: str) -> None:
        if schedule.get(target) not in (None, "", [], {}):
            for alias in aliases:
                schedule.pop(alias, None)
            return
        for alias in aliases:
            value = schedule.pop(alias, None)
            if value not in (None, "", [], {}):
                schedule[target] = value
                break

    move("target_temp_id", "targetTempId", "task_temp_id", "taskTempId")
    raw_target_temp_ids = schedule.pop(
        "target_temp_ids", schedule.pop("targetTempIds", None)
    )
    if (
        schedule.get("target_temp_id") in (None, "")
        and isinstance(raw_target_temp_ids, list)
        and len(raw_target_temp_ids) == 1
        and isinstance(raw_target_temp_ids[0], str)
    ):
        schedule["target_temp_id"] = raw_target_temp_ids[0]
    move(
        "frequency",
        "recurrence_type",
        "recurrenceType",
        "rule",
        "recurrence",
        "cadence",
        "recurrence_rule",
        "recurrenceRule",
    )
    move("days_of_week", "weekdays", "daysOfWeek", "by_weekday", "byWeekday")
    move(
        "start_date",
        "startDate",
        "starts_on",
        "startsOn",
        "starts_on_or_after",
        "startsOnOrAfter",
        "date",
    )
    move("end_date", "endDate", "until", "ends_on", "endsOn")
    move("occurrence_count", "occurrenceCount")
    move("end_mode", "end_condition", "endCondition", "endMode")
    move("duration_minutes", "duration", "durationMinutes")
    move(
        "preferred_start_time",
        "start_time",
        "startTime",
        "preferredStartTime",
        "local_start_time",
        "localStartTime",
        "start_time_local",
        "startTimeLocal",
        "local_time",
        "localTime",
        "time",
    )
    move(
        "allowed_start_time",
        "allowedStartTime",
        "time_window_start",
        "timeWindowStart",
        "window_start",
        "windowStart",
        "window_start_local",
        "windowStartLocal",
    )
    move(
        "allowed_end_time",
        "allowedEndTime",
        "time_window_end",
        "timeWindowEnd",
        "window_end",
        "windowEnd",
        "window_end_local",
        "windowEndLocal",
        "end_time",
        "endTime",
    )
    move("gap_minutes", "gapMinutes")
    move("timezone", "time_zone", "timeZone")
    move("conflict_relocation", "conflictRelocation")
    raw_conflict_relocation = str(
        schedule.get("conflict_relocation") or ""
    ).strip().casefold().replace("-", "_")
    if "before" in raw_conflict_relocation:
        schedule["conflict_relocation"] = "before_incoming"
    elif "after" in raw_conflict_relocation:
        schedule["conflict_relocation"] = "after_incoming"
    elif raw_conflict_relocation:
        # This field is an optional placement-policy hint, not user-authored
        # content. Providers commonly emit labels such as ``auto``,
        # ``relocate_existing`` or a boolean here. They carry no directional
        # meaning and must not invalidate an otherwise complete recurrence.
        # Omitting the hint delegates to the scheduling service's canonical
        # after-incoming default and keeps one provider spelling from consuming
        # several repair turns.
        schedule.pop("conflict_relocation", None)

    raw_time_of_day = schedule.pop(
        "time_of_day",
        schedule.pop(
            "timeOfDay",
            schedule.pop("preferred_period", schedule.pop("preferredPeriod", None)),
        ),
    )
    if isinstance(raw_time_of_day, str) and raw_time_of_day.strip():
        time_of_day = raw_time_of_day.strip().casefold().replace("-", "_")
        qualitative_windows = {
            "early_morning": ("05:00", "09:00", "07:00"),
            "morning": ("06:00", "12:00", "09:00"),
            "midday": ("11:00", "14:00", "12:00"),
            "noon": ("11:00", "14:00", "12:00"),
            "afternoon": ("12:00", "17:00", "14:00"),
            "evening": ("17:00", "21:00", "18:00"),
            "night": ("19:00", "23:59", "20:00"),
        }
        window = qualitative_windows.get(time_of_day)
        if window is not None:
            schedule.setdefault("allowed_start_time", window[0])
            schedule.setdefault("allowed_end_time", window[1])
            schedule.setdefault("preferred_start_time", window[2])
        elif re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time_of_day.strip()):
            schedule.setdefault("preferred_start_time", raw_time_of_day.strip())

    scheduling_mode = str(
        schedule.pop("scheduling_mode", schedule.pop("schedulingMode", "")) or ""
    ).strip().casefold().replace("-", "_")
    if scheduling_mode in {"fixed", "exact", "strict", "anchored"}:
        schedule.setdefault("strict", True)
    elif scheduling_mode in {"flexible", "preferred", "best_fit"}:
        schedule.setdefault("strict", False)
    flexibility = schedule.pop("flexibility", None)
    if flexibility is True:
        schedule.setdefault("strict", False)
    elif flexibility is False:
        schedule.setdefault("strict", True)
    elif isinstance(flexibility, str):
        normalized_flexibility = flexibility.strip().casefold().replace("-", "_")
        if normalized_flexibility in {"flexible", "preferred", "best_fit"}:
            schedule.setdefault("strict", False)
        elif normalized_flexibility in {"fixed", "exact", "strict", "anchored"}:
            schedule.setdefault("strict", True)
    start_policy = str(
        schedule.pop("start_policy", schedule.pop("startPolicy", "")) or ""
    ).strip().casefold().replace("-", "_")
    if start_policy in {"fixed", "exact", "strict", "anchored"}:
        schedule.setdefault("strict", True)
    elif start_policy in {"flexible", "preferred", "best_fit"}:
        schedule.setdefault("strict", False)

    raw_end = schedule.pop("end", None)
    if isinstance(raw_end, str) and raw_end.strip():
        lowered_end = raw_end.strip().casefold()
        if lowered_end in {"never", "ongoing", "forever", "indefinite"}:
            schedule.setdefault("end_mode", "never")
        else:
            schedule.setdefault("end_date", raw_end.strip())
    elif isinstance(raw_end, dict):
        end_kind = str(
            _first_present(raw_end, "mode", "kind", "type") or ""
        ).strip()
        if end_kind:
            schedule.setdefault("end_mode", end_kind)
        end_date = _first_present(raw_end, "date", "end_date", "endDate", "until")
        if end_date not in (None, ""):
            schedule.setdefault("end_date", end_date)
        count = _first_present(raw_end, "count", "occurrence_count", "occurrenceCount")
        if count not in (None, ""):
            schedule.setdefault("occurrence_count", count)

    raw_start = schedule.pop("start", None)
    if isinstance(raw_start, str) and raw_start.strip():
        try:
            parsed_start = datetime.fromisoformat(raw_start.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed_start = None
        if parsed_start is not None:
            schedule.setdefault("start_date", parsed_start.date().isoformat())
            schedule.setdefault("preferred_start_time", parsed_start.strftime("%H:%M"))

    raw_starts = schedule.pop("starts", None)
    if isinstance(raw_starts, str) and raw_starts.strip():
        try:
            parsed_starts = datetime.fromisoformat(
                raw_starts.strip().replace("Z", "+00:00")
            )
        except ValueError:
            parsed_starts = None
        if parsed_starts is not None:
            schedule.setdefault("start_date", parsed_starts.date().isoformat())
            if "T" in raw_starts:
                schedule.setdefault(
                    "preferred_start_time",
                    parsed_starts.strftime("%H:%M"),
                )
        else:
            try:
                schedule.setdefault(
                    "start_date",
                    date.fromisoformat(raw_starts.strip()).isoformat(),
                )
            except ValueError:
                pass
    elif isinstance(raw_starts, dict):
        starts_date = _first_present(
            raw_starts, "date", "start_date", "startDate", "on"
        )
        starts_time = _first_present(
            raw_starts, "time", "start_time", "startTime", "at"
        )
        if starts_date not in (None, ""):
            schedule.setdefault("start_date", starts_date)
        if starts_time not in (None, ""):
            schedule.setdefault("preferred_start_time", starts_time)

    start_at_local = schedule.pop("start_at_local", schedule.pop("startAtLocal", None))
    if isinstance(start_at_local, str) and start_at_local.strip():
        try:
            parsed_start = datetime.fromisoformat(start_at_local.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed_start = None
        if parsed_start is not None:
            schedule.setdefault("start_date", parsed_start.date().isoformat())
            schedule.setdefault("preferred_start_time", parsed_start.strftime("%H:%M"))

    raw_days = schedule.get("days_of_week")
    if isinstance(raw_days, list):
        normalized_days = [
            _WEEKDAY_ALIASES.get(str(day).strip().casefold())
            for day in raw_days
        ]
        schedule["days_of_week"] = [day for day in normalized_days if day]
        if schedule["days_of_week"]:
            schedule["frequency"] = "weekly"

    raw_frequency_value = schedule.get("frequency")
    if isinstance(raw_frequency_value, dict):
        nested_frequency = _first_present(
            raw_frequency_value,
            "frequency",
            "cadence",
            "unit",
            "kind",
            "type",
            "value",
        )
        if nested_frequency not in (None, ""):
            schedule["frequency"] = nested_frequency
    raw_frequency = str(schedule.get("frequency") or "").strip().casefold()
    frequency_aliases = {
        "day": "daily",
        "every_day": "daily",
        "every day": "daily",
        "daily": "daily",
        "week": "weekly",
        "every_week": "weekly",
        "every week": "weekly",
        "weekly": "weekly",
        "weekday": "weekly",
        "weekdays": "weekly",
        "every weekday": "weekly",
    }
    if raw_frequency in {"weekday", "weekdays", "every weekday"} and not schedule.get(
        "days_of_week"
    ):
        schedule["days_of_week"] = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
        ]
    frequency_tokens = raw_frequency.replace("_", " ").replace("-", " ").split()
    frequency_weekdays = list(
        dict.fromkeys(
            weekday
            for token in frequency_tokens
            if (
                weekday := _WEEKDAY_ALIASES.get(
                    token[:-1] if token.endswith("s") else token
                )
            )
        )
    )
    if frequency_weekdays and not schedule.get("days_of_week"):
        schedule["days_of_week"] = frequency_weekdays
        schedule["frequency"] = "weekly"
    if frequency_weekdays:
        schedule["frequency"] = "weekly"
    elif raw_frequency in frequency_aliases:
        schedule["frequency"] = frequency_aliases[raw_frequency]
    elif "week" in raw_frequency:
        schedule["frequency"] = "weekly"
    elif "day" in raw_frequency:
        schedule["frequency"] = "daily"

    duration_minutes = _duration_value_minutes(schedule.get("duration_minutes"))
    if duration_minutes is not None:
        schedule["duration_minutes"] = duration_minutes

    time_window = schedule.pop("time_window", schedule.pop("timeWindow", None))
    if isinstance(time_window, dict):
        window_start = time_window.get("start", time_window.get("start_time"))
        window_end = time_window.get("end", time_window.get("end_time"))
        if window_start not in (None, ""):
            schedule.setdefault("preferred_start_time", window_start)
            schedule.setdefault("allowed_start_time", window_start)
        if window_end not in (None, ""):
            schedule.setdefault("allowed_end_time", window_end)

    preferred_window = schedule.pop(
        "preferred_time_window",
        schedule.pop("preferredTimeWindow", None),
    )
    if isinstance(preferred_window, str):
        window_match = re.fullmatch(
            r"\s*(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\s*",
            preferred_window,
        )
        if window_match:
            schedule.setdefault("preferred_start_time", window_match.group(1))
            schedule.setdefault("allowed_start_time", window_match.group(1))
            schedule.setdefault("allowed_end_time", window_match.group(2))

    # Some providers express recurrence placement through the same typed
    # constraint vocabulary used by one-time scheduling. Translate the
    # executable subset instead of rejecting the wrapper or silently losing
    # its time semantics.
    raw_constraints: Any = []
    for constraint_key in (
        "constraints",
        "hard_constraints",
        "hardConstraints",
        "soft_constraints",
        "softConstraints",
    ):
        value = schedule.pop(constraint_key, None)
        if isinstance(value, list):
            raw_constraints.extend(value)
        elif value not in (None, "", {}):
            raw_constraints.append(value)
    if isinstance(raw_constraints, dict):
        raw_constraints = [raw_constraints]
    if isinstance(raw_constraints, list):
        for raw_constraint in raw_constraints:
            if not isinstance(raw_constraint, dict):
                continue
            constraint = _normalize_request_constraint_tool_argument(raw_constraint)
            kind = constraint.get("kind")
            start_at = str(constraint.get("start_at") or "").strip()
            end_at = str(constraint.get("end_at") or "").strip()
            start_time = start_at[11:16] if len(start_at) >= 16 and "T" in start_at else start_at
            end_time = end_at[11:16] if len(end_at) >= 16 and "T" in end_at else end_at
            if kind == "exact_start" and start_time:
                schedule.setdefault("preferred_start_time", start_time)
                schedule.setdefault("strict", True)
            elif kind == "allowed_window":
                if start_time:
                    schedule.setdefault("allowed_start_time", start_time)
                if end_time:
                    schedule.setdefault("allowed_end_time", end_time)
            elif kind == "minimum_gap" and isinstance(constraint.get("minutes"), int):
                schedule.setdefault("gap_minutes", constraint["minutes"])

    conflict_resolution = schedule.pop(
        "conflict_resolution", schedule.pop("conflictResolution", None)
    )
    conflict_text = (
        " ".join(str(value) for value in conflict_resolution.values())
        if isinstance(conflict_resolution, dict)
        else str(conflict_resolution or "")
    ).casefold()
    if "before" in conflict_text:
        schedule.setdefault("conflict_relocation", "before_incoming")
    elif "after" in conflict_text:
        schedule.setdefault("conflict_relocation", "after_incoming")

    end_mode = str(schedule.get("end_mode") or "").strip().casefold()
    if end_mode in {
        "ongoing", "indefinite", "forever", "open_ended", "open-ended",
        "no_end", "no-end", "none", "unbounded",
    }:
        schedule["end_mode"] = "never"
    ongoing = schedule.pop("ongoing", None)
    if ongoing is True:
        schedule["end_mode"] = "never"
    elif not end_mode and not schedule.get("end_date") and not schedule.get(
        "occurrence_count"
    ):
        # The presence of a recurrence contract already establishes repetition.
        # With no user-supplied bound, preserve the ordinary open-ended meaning.
        schedule["end_mode"] = "never"

    interval = schedule.pop("interval", None)
    if interval not in (None, 1, "1"):
        # The current product contract supports daily/weekly cadence only. Keep
        # an unsupported interval visible to validation rather than discarding it.
        schedule["interval"] = interval
    for annotation_key in (
        "anchored",
        "is_anchored",
        "isAnchored",
        "flexible",
        "source_text",
        "sourceText",
        "description",
        "recurrence_scope",
        "recurrenceScope",
        "existing_task_id",
        "existingTaskId",
        "existing_task_title",
        "existingTaskTitle",
        "target_task_id",
        "targetTaskId",
        "target_task_title",
        "targetTaskTitle",
        "task_id",
        "taskId",
        "title",
        "task_title",
        "taskTitle",
        "operation",
        "op",
        "action",
        "movable",
        "is_movable",
        "isMovable",
    ):
        schedule.pop(annotation_key, None)
    if schedule.get("frequency") == "weekly" and not schedule.get("days_of_week"):
        try:
            anchor_date = date.fromisoformat(str(schedule.get("start_date") or ""))
        except ValueError:
            anchor_date = None
        if anchor_date is not None:
            schedule["days_of_week"] = [
                (
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                )[anchor_date.weekday()]
            ]
    canonical_recurrence_keys = {
        "target_temp_id",
        "frequency",
        "days_of_week",
        "start_date",
        "end_date",
        "occurrence_count",
        "end_mode",
        "duration_minutes",
        "preferred_start_time",
        "allowed_start_time",
        "allowed_end_time",
        "gap_minutes",
        "timezone",
        "strict",
        "conflict_relocation",
        # Keep an explicitly unsupported executable interval visible so the
        # strict contract can reject it rather than silently changing cadence.
        "interval",
    }
    return {
        key: value
        for key, value in schedule.items()
        if key in canonical_recurrence_keys
    }


def _normalize_preference_plan_tool_argument(
    raw_arguments: dict[str, Any],
    *,
    default_source_text: str | None = None,
) -> dict[str, Any]:
    """Canonicalize preference meaning without discarding provider-authored content."""

    arguments = copy.deepcopy(raw_arguments)
    preference = (
        copy.deepcopy(arguments.get("preference"))
        if isinstance(arguments.get("preference"), dict)
        else {}
    )
    subject = _clean_text(preference.pop("subject", None))
    preferred_period = _clean_text(
        preference.pop(
            "preferred_time_of_day",
            preference.pop("preferred_period", None),
        )
    )
    raw_hard = preference.pop("is_hard_constraint", None)
    if preferred_period and preference.get("preferenceType") in (None, ""):
        preference["preferenceType"] = "preferred_time_of_day"
    if isinstance(raw_hard, bool) and preference.get("strength") in (None, ""):
        preference["strength"] = "hard" if raw_hard else "soft"
    if not _clean_text(preference.get("description")) and subject and preferred_period:
        preference["description"] = f"Prefer {subject} in the {preferred_period}."

    value_aliases = {
        "preference_text": "preferenceText",
        "source_text": "sourceText",
        "extraction_kind": "extractionKind",
        "constraint_eligible": "constraintEligible",
        "days_of_week": "daysOfWeek",
        "start_time": "startTime",
        "end_time": "endTime",
        "time_zone": "timezone",
        "inter_task_gap_minutes": "interTaskGapMinutes",
        "preferred_inter_task_gap_minutes": "preferredInterTaskGapMinutes",
        "minimum_task_duration_minutes": "minimumTaskDurationMinutes",
        "shorter_task_gap_minutes": "shorterTaskGapMinutes",
        "applies_to_all_tasks": "appliesToAllTasks",
    }

    def normalize_value(raw_value: Any, *, constraint_type: str = "") -> dict[str, Any]:
        if isinstance(raw_value, dict):
            value = copy.deepcopy(raw_value)
        elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            value = (
                {"interTaskGapMinutes": int(raw_value)}
                if constraint_type == "inter_task_gap"
                else {"rule": str(raw_value)}
            )
        elif isinstance(raw_value, str) and raw_value.strip():
            value = {"rule": raw_value.strip()}
        else:
            value = {}
        for alias, canonical in value_aliases.items():
            if value.get(canonical) in (None, "") and value.get(alias) not in (None, ""):
                value[canonical] = value[alias]
            value.pop(alias, None)
        allowed = set(PREFERENCE_VALUE_INPUT_SCHEMA["properties"])
        return {key: item for key, item in value.items() if key in allowed}

    preference_value = normalize_value(preference.get("value"))
    if subject and preferred_period:
        preference_value.setdefault(
            "rule",
            f"Prefer {subject} in the {preferred_period}.",
        )
        preference_value.setdefault(
            "preferenceText",
            _clean_text(default_source_text)
            or f"prefer {subject} in the {preferred_period}",
        )
    for raw_key in tuple(PREFERENCE_VALUE_INPUT_SCHEMA["properties"]):
        if preference.get(raw_key) not in (None, ""):
            preference_value.setdefault(raw_key, preference.pop(raw_key))
    for alias, canonical in value_aliases.items():
        if preference.get(alias) not in (None, ""):
            preference_value.setdefault(canonical, preference.pop(alias))
    if preference_value:
        preference["value"] = preference_value

    raw_scope = preference.pop("scope", None)
    if isinstance(raw_scope, str) and raw_scope.strip():
        preference.setdefault("scopeType", raw_scope.strip())
    elif isinstance(raw_scope, dict):
        preference.setdefault(
            "scopeType",
            _first_present(raw_scope, "scopeType", "scope_type", "type", "kind"),
        )
        preference.setdefault(
            "scopeId",
            _first_present(raw_scope, "scopeId", "scope_id", "id", "target_id"),
        )
    raw_applies_to = preference.pop(
        "applies_to", preference.pop("appliesTo", None)
    )
    if isinstance(raw_applies_to, str) and raw_applies_to.strip():
        preference.setdefault("scopeType", raw_applies_to.strip())
    elif isinstance(raw_applies_to, dict):
        preference.setdefault(
            "scopeType",
            _first_present(
                raw_applies_to, "scopeType", "scope_type", "type", "kind"
            ),
        )
        preference.setdefault(
            "scopeId",
            _first_present(
                raw_applies_to,
                "scopeId",
                "scope_id",
                "targetId",
                "target_id",
                "id",
            ),
        )
    for alias, canonical in {
        "preference_type": "preferenceType",
        "scope_type": "scopeType",
        "scope_id": "scopeId",
    }.items():
        if preference.get(canonical) in (None, "") and preference.get(alias) not in (None, ""):
            preference[canonical] = preference[alias]
        preference.pop(alias, None)
    if preference.get("preferenceType") in (None, "") and preference.get("type") not in (
        None,
        "",
    ):
        preference["preferenceType"] = preference["type"]
    preference.pop("type", None)

    if not _clean_text(preference.get("description")):
        preference["description"] = (
            _clean_text(preference_value.get("preferenceText"))
            or _clean_text(preference_value.get("rule"))
            or _clean_text(arguments.get("source_text"))
            or _clean_text(default_source_text)
        )
    arguments["preference"] = preference

    raw_constraints = arguments.get("constraints")
    if isinstance(raw_constraints, dict):
        raw_constraints = [raw_constraints]
    if isinstance(raw_constraints, list):
        constraints: list[Any] = []
        for raw_constraint in raw_constraints:
            if not isinstance(raw_constraint, dict):
                constraints.append(raw_constraint)
                continue
            constraint = copy.deepcopy(raw_constraint)
            raw_type = _first_present(
                constraint,
                "constraintType",
                "constraint_type",
                "type",
                "kind",
            )
            constraint_type = str(raw_type or "").strip()
            if constraint_type:
                constraint["constraintType"] = constraint_type
            raw_applies_to = constraint.pop(
                "applies_to",
                constraint.pop("appliesTo", None),
            )
            if isinstance(raw_applies_to, str) and raw_applies_to.strip():
                constraint.setdefault("targetType", raw_applies_to.strip())
            elif isinstance(raw_applies_to, dict):
                constraint.setdefault(
                    "targetType",
                    _first_present(raw_applies_to, "targetType", "target_type", "type", "kind"),
                )
                constraint.setdefault(
                    "targetId",
                    _first_present(raw_applies_to, "targetId", "target_id", "id"),
                )
            for alias, canonical in {
                "constraint_type": "constraintType",
                "target_type": "targetType",
                "target_id": "targetId",
                "penalty_weight": "penaltyWeight",
            }.items():
                if constraint.get(canonical) in (None, "") and constraint.get(alias) not in (None, ""):
                    constraint[canonical] = constraint[alias]
                constraint.pop(alias, None)
            constraint.pop("type", None)
            constraint.pop("kind", None)
            constraint["value"] = normalize_value(
                constraint.get("value"),
                constraint_type=constraint_type,
            )
            if not constraint["value"]:
                embedded_value = normalize_value(constraint, constraint_type=constraint_type)
                if embedded_value:
                    constraint["value"] = embedded_value
            allowed = {
                "description",
                "constraintType",
                "targetType",
                "targetId",
                "hard",
                "penaltyWeight",
                "confidence",
                "value",
            }
            constraints.append(
                {key: item for key, item in constraint.items() if key in allowed}
            )
        arguments["constraints"] = constraints
    return arguments


def _bind_pending_updates_to_created_node(
    *,
    structural_operations: list[dict[str, Any]],
    pending_updates: dict[str, Any],
    recurring_schedule: Any,
    created_task_schedule: Any,
) -> bool:
    """Attach a clarification answer to the new task it describes.

    A new task has no database id yet, so emitting a separate metadata operation
    cannot be validated. The scheduling contracts already provide a stable temp id;
    use that identity to keep creation, clarified metadata, and scheduling atomic.
    """

    create_operations = [
        operation
        for operation in structural_operations
        if str(operation.get("kind") or operation.get("op") or "").strip().lower()
        == "create_node"
    ]
    if not create_operations:
        return False

    target_temp_ids: list[str] = []
    if isinstance(recurring_schedule, dict):
        target_temp_id = _clean_text(recurring_schedule.get("target_temp_id"))
        if target_temp_id:
            target_temp_ids.append(target_temp_id)
    if isinstance(created_task_schedule, dict):
        raw_target_temp_ids = created_task_schedule.get("target_temp_ids")
        if isinstance(raw_target_temp_ids, list) and len(raw_target_temp_ids) == 1:
            target_temp_id = _clean_text(raw_target_temp_ids[0])
            if target_temp_id:
                target_temp_ids.append(target_temp_id)
        raw_schedule_groups = created_task_schedule.get("schedule_groups")
        grouped_target_temp_ids = [
            _clean_text(item)
            for group in raw_schedule_groups
            if isinstance(group, dict)
            for item in (group.get("target_temp_ids") or [])
        ] if isinstance(raw_schedule_groups, list) else []
        grouped_target_temp_ids = [
            item for item in grouped_target_temp_ids if item
        ]
        if len(grouped_target_temp_ids) == 1:
            target_temp_ids.append(grouped_target_temp_ids[0])

    matched_operations = [
        operation
        for operation in create_operations
        if _clean_text(operation.get("temp_id")) in target_temp_ids
    ]
    if len(matched_operations) == 1:
        target_operation = matched_operations[0]
    elif not target_temp_ids and len(create_operations) == 1:
        target_operation = create_operations[0]
    else:
        return False

    for key, value in pending_updates.items():
        if value is not None:
            target_operation[key] = copy.deepcopy(value)
    return True


def _pending_metadata_updates_from_session_memory(
    *,
    session_memory: dict[str, Any] | None,
    today_override: date | None,
    current_user_request: str | None = None,
) -> dict[str, Any]:
    if not isinstance(session_memory, dict):
        return {}
    today_value = today_override or date.today()

    # Resolve short answers such as "1 hr" against the actual pending question.
    # Parsing the answer by itself loses the field name, while parsing old assistant
    # prose as metadata can accidentally turn a recurrence start into a deadline.
    active_request = session_memory.get("active_request")
    if isinstance(active_request, dict):
        clarification = active_request.get("clarification")
        if isinstance(clarification, dict):
            clarification_text = " ".join(
                str(clarification.get(key) or "").strip()
                for key in ("question", "message")
                if str(clarification.get(key) or "").strip()
            )
            if re.search(
                r"\b(?:duration|how\s+long|how\s+much\s+time|time\s+should|minutes?|hours?)\b",
                clarification_text,
                re.IGNORECASE,
            ):
                estimate_minutes = parse_prompt_estimate_minutes(
                    str(current_user_request or "")
                )
                if estimate_minutes is not None:
                    return {"time_estimate_minutes": estimate_minutes}

    unresolved_asks = session_memory.get("unresolved_asks")
    if isinstance(unresolved_asks, list):
        for item in reversed(unresolved_asks[-4:]):
            summary = str(item.get("summary") or "") if isinstance(item, dict) else ""
            if re.search(
                r"\b(?:duration|how\s+long|how\s+much\s+time|minutes?|hours?)\b",
                summary,
                re.IGNORECASE,
            ):
                estimate_minutes = parse_prompt_estimate_minutes(
                    str(current_user_request or "")
                )
                if estimate_minutes is not None:
                    return {"time_estimate_minutes": estimate_minutes}

    text_parts: list[str] = []
    if isinstance(unresolved_asks, list):
        for item in unresolved_asks[-4:]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if summary:
                text_parts.append(summary)
    latest_answer = session_memory.get("latest_assistant_answer")
    if isinstance(latest_answer, dict):
        summary = str(latest_answer.get("summary") or "").strip()
        if summary:
            text_parts.append(summary)
    latest_user_request = session_memory.get("latest_user_request")
    if isinstance(latest_user_request, dict):
        summary = str(latest_user_request.get("summary") or "").strip()
        if summary:
            text_parts.append(summary)
    recent_user_requests = session_memory.get("recent_user_requests")
    if isinstance(recent_user_requests, list):
        for item in recent_user_requests[-4:]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if summary:
                text_parts.append(summary)
    for text in reversed(text_parts):
        updates = build_metadata_updates_from_prompt(text, today_value)
        deadline_language = re.search(
            r"\b(?:deadline|due)\b|\bset\b.{0,160}\b(?:to|as|for)\b",
            text,
            re.IGNORECASE,
        )
        recurrence_language = re.search(
            r"\b(?:schedule|scheduled|starting|starts?|repeat|recurring|every)\b",
            text,
            re.IGNORECASE,
        )
        if (
            "deadline_at" not in updates
            and deadline_language
            and not recurrence_language
        ):
            parsed_date = parse_prompt_date(text, today_value)
            if parsed_date:
                updates["deadline_at"] = parsed_date
        if updates:
            return updates
    return {}


def execute_kernel_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    categories: list[Category],
    nodes: list[StrategyNode],
    focus_node: StrategyNode | None,
    active_category: Category | None = None,
    staged_node_ids: list[int],
    working_set: list[dict[str, Any]] | None = None,
    today_override: date | None = None,
    strategist_session_id: str | None = None,
    pending_proposal: dict[str, Any] | None = None,
    recent_events: list[dict[str, Any]] | None = None,
    session_memory: dict[str, Any] | None = None,
    tool_result_store: dict[str, Any] | None = None,
    original_user_request: str | None = None,
    calendar_service: CalendarReadApplicationService | None = None,
    scheduling_service: SchedulingApplicationService | None = None,
    calendar_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    planner_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    planner_context_reader: Callable[[], dict[str, Any]] | None = None,
    planner_plan_evaluator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    planner_decision_responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    goal_state_updater: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    evidence_resolver: Callable[[str], dict[str, Any]] | None = None,
    change_set_authorer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    change_set_simulator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    preference_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    preference_inventory_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    preference_cleanup_analyzer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and dispatch one canonical capability to injected services."""
    spec = KERNEL_TOOL_SPECS_BY_NAME.get(name)
    if spec is None and name == "update_goal_state":
        spec = LEGACY_UPDATE_GOAL_STATE_SPEC
    if spec is None:
        typed_error = ToolError(
            code="TOOL_NOT_FOUND",
            category=ToolErrorCategory.FATAL_INVARIANT,
            retryable=False,
            message_for_model=f"Unknown agent-kernel tool: {name}",
        )
        return {
            "error": f"Unknown agent-kernel tool: {name}",
            "tool_error": typed_error.model_dump(by_alias=True),
            "read_only": True,
            "result_kind": "error",
        }
    if not isinstance(arguments, dict):
        return _tool_argument_error(spec, ["Tool arguments must be an object."])
    if name == "query_task_graph":
        # Normalize common provider representations at the typed read boundary.
        # These are field-shape equivalents, not natural-language intent rules.
        arguments = copy.deepcopy(arguments)
        if not str(arguments.get("query") or "").strip():
            for alias in ("text", "request", "search_query"):
                if str(arguments.get(alias) or "").strip():
                    arguments["query"] = arguments[alias]
                    break
        for alias in ("text", "request", "search_query"):
            arguments.pop(alias, None)
        if arguments.get("category_name") in (None, ""):
            for alias in ("category", "workspace", "workspace_name"):
                if arguments.get(alias) not in (None, ""):
                    arguments["category_name"] = arguments[alias]
                    break
        for alias in ("category", "workspace", "workspace_name"):
            arguments.pop(alias, None)
        filters = arguments.get("filters")
        if isinstance(filters, dict):
            filters = copy.deepcopy(filters)
            for singular, plural in (("status", "statuses"), ("priority", "priorities")):
                if filters.get(plural) is None and filters.get(singular) is not None:
                    value = filters.pop(singular)
                    filters[plural] = value if isinstance(value, list) else [value]
                else:
                    filters.pop(singular, None)
            for alias, canonical in (
                ("date_from", "deadline_start"),
                ("start_date", "deadline_start"),
                ("date_to", "deadline_end"),
                ("end_date", "deadline_end"),
                ("include_statuses", "statuses"),
            ):
                if filters.get(canonical) is None and filters.get(alias) is not None:
                    filters[canonical] = filters[alias]
            allowed_filter_keys = {
                "active_only",
                "include_archived",
                "include_backlog",
                "include_branches",
                "leaf_only",
                "deadline",
                "deadline_start",
                "deadline_end",
                "estimate",
                "max_estimate_minutes",
                "min_estimate_minutes",
                "statuses",
                "priorities",
                "query",
                "days",
                "sort",
            }
            raw_sort = str(filters.get("sort") or "").strip().casefold()
            sort_aliases = {
                "deadline": "deadline_asc",
                "due": "deadline_asc",
                "due_date": "deadline_asc",
                "earliest_deadline": "deadline_asc",
                "latest_deadline": "deadline_desc",
                "priority": "priority_desc",
                "highest_priority": "priority_desc",
                "lowest_priority": "priority_asc",
                "estimate": "estimate_desc",
                "longest": "estimate_desc",
                "shortest": "estimate_asc",
            }
            if raw_sort in sort_aliases:
                filters["sort"] = sort_aliases[raw_sort]
            # Structured filters are optional query hints. Preserve every
            # canonical hint and let the natural-language query remain the
            # authority when a provider adds unsupported filter annotations.
            arguments["filters"] = {
                key: value
                for key, value in filters.items()
                if key in allowed_filter_keys and value is not None
            }
    if name == "list_tasks_by_filter":
        arguments = copy.deepcopy(arguments)
        filters = arguments.get("filters")
        if isinstance(filters, dict):
            raw_sort = str(filters.get("sort") or "").strip().casefold()
            sort_aliases = {
                "priority": "priority_desc",
                "highest_priority": "priority_desc",
                "lowest_priority": "priority_asc",
                "estimate": "estimate_desc",
                "longest": "estimate_desc",
                "shortest": "estimate_asc",
            }
            if raw_sort in sort_aliases:
                filters["sort"] = sort_aliases[raw_sort]
            arguments["filters"] = {
                key: value for key, value in filters.items() if value is not None
            }
    if name == "query_preferences":
        # Read limits are caller budgets, not semantic inputs. Clamp oversized
        # provider requests to the registered bounded surface instead of
        # spending another model turn repairing an equivalent query.
        arguments = copy.deepcopy(arguments)
        for key, maximum in (
            ("max_soft_preferences", 16),
            ("max_observations", 12),
        ):
            value = arguments.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                arguments[key] = max(0, min(maximum, value))
        for key, maximum in (
            ("task_ids", 100),
            ("task_titles", 100),
            ("category_ids", 32),
            ("category_names", 32),
        ):
            value = arguments.get(key)
            if isinstance(value, list):
                arguments[key] = value[:maximum]
    if name == "get_task_schedule":
        # Accept equivalent singular/camel provider shapes, then bind an exact
        # title only when it resolves unambiguously in authoritative context.
        arguments = copy.deepcopy(arguments)
        raw_task_ids = _first_present(
            arguments,
            "task_ids",
            "taskIds",
            "task_id",
            "taskId",
        )
        if isinstance(raw_task_ids, int) and not isinstance(raw_task_ids, bool):
            arguments["task_ids"] = [raw_task_ids]
        elif isinstance(raw_task_ids, list):
            arguments["task_ids"] = list(
                dict.fromkeys(
                    task_id
                    for task_id in raw_task_ids
                    if isinstance(task_id, int) and not isinstance(task_id, bool)
                )
            )
        if not arguments.get("task_ids"):
            task_title = _clean_text(
                _first_present(arguments, "task_title", "taskTitle", "title")
            )
            title_matches = [
                int(node.id)
                for node in nodes
                if node.id is not None
                and _clean_text(node.title)
                and _clean_text(node.title).casefold() == (task_title or "").casefold()
            ]
            if len(title_matches) == 1:
                arguments["task_ids"] = title_matches
        for alias in (
            "taskIds",
            "task_id",
            "taskId",
            "task_title",
            "taskTitle",
            "title",
        ):
            arguments.pop(alias, None)
    if name == "get_tool_result_by_handle":
        # Pagination is a read budget, not user meaning. Keep equivalent
        # oversized provider requests inside the registered safe page bounds.
        arguments = copy.deepcopy(arguments)
        if arguments.get("tool_result_handle") in (None, ""):
            arguments["tool_result_handle"] = _first_present(
                arguments,
                "handle",
                "toolResultHandle",
                "result_handle",
                "resultHandle",
            )
        for alias in (
            "handle",
            "toolResultHandle",
            "result_handle",
            "resultHandle",
        ):
            arguments.pop(alias, None)
        for key, lower, upper in (("offset", 0, 2048), ("limit", 1, 20)):
            value = arguments.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                arguments[key] = max(lower, min(upper, value))
    if name == "set_goal_transition":
        arguments = _normalize_goal_transition_tool_argument(arguments)
    if name in {"propose_metadata_update", "submit_task_metadata_plan"}:
        arguments = copy.deepcopy(arguments)

        def normalize_due_date(updates: Any) -> Any:
            if not isinstance(updates, dict):
                return updates
            normalized = copy.deepcopy(updates)
            if (
                normalized.get("deadline_at") in (None, "")
                and normalized.get("due_date") not in (None, "")
            ):
                normalized["deadline_at"] = normalized["due_date"]
            if (
                normalized.get("deadline_at") in (None, "")
                and normalized.get("dueDate") not in (None, "")
            ):
                normalized["deadline_at"] = normalized["dueDate"]
            if (
                normalized.get("deadline_at") in (None, "")
                and normalized.get("deadline") not in (None, "")
            ):
                normalized["deadline_at"] = normalized["deadline"]
            normalized.pop("due_date", None)
            normalized.pop("dueDate", None)
            normalized.pop("deadline", None)
            return normalized

        arguments["updates"] = normalize_due_date(arguments.get("updates"))
        raw_target_updates = arguments.get("target_updates")
        if isinstance(raw_target_updates, list):
            normalized_target_updates: list[Any] = []
            for raw_item in raw_target_updates:
                if not isinstance(raw_item, dict):
                    normalized_target_updates.append(raw_item)
                    continue
                item = copy.deepcopy(raw_item)
                if (
                    item.get("target_task_id") is None
                    and item.get("task_id") is not None
                ):
                    item["target_task_id"] = item["task_id"]
                item.pop("task_id", None)
                nested_updates = item.get("updates")
                if not isinstance(nested_updates, dict):
                    nested_updates = {
                        key: item.pop(key)
                        for key in ("due_date", "dueDate", "deadline", "deadline_at")
                        if key in item
                    }
                item["updates"] = normalize_due_date(nested_updates)
                normalized_target_updates.append(item)
            arguments["target_updates"] = normalized_target_updates
    canonical_errors = _canonical_task_argument_errors(name, arguments)
    if canonical_errors:
        return _tool_argument_error(
            spec,
            canonical_errors,
            repair_hint=(
                "Retry with task_id or task_title."
                if name in TASK_SCOPED_TOOL_NAMES
                else None
            ),
        )
    if name == "handoff_to_planner":
        arguments = copy.deepcopy(arguments)
        handoff_task_ids = list(dict.fromkeys(
            int(task_id)
            for task_id in (arguments.get("task_ids") or [])
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        ))
        raw_constraints = arguments.get("request_constraints")
        if isinstance(raw_constraints, dict):
            raw_constraints = [raw_constraints]
        if isinstance(raw_constraints, list):
            normalized_constraints = [
                _normalize_request_constraint_tool_argument(
                    item,
                    default_source_text=(
                        _clean_text(arguments.get("instruction"))
                        or original_user_request
                    ),
                )
                if isinstance(item, dict)
                else item
                for item in raw_constraints
            ]
            arguments["request_constraints"] = [
                constraint
                for constraint in normalized_constraints
                if isinstance(constraint, dict)
                and constraint.get("kind") in _SCHEDULING_CONSTRAINT_KINDS
            ]
            for constraint in arguments["request_constraints"]:
                if constraint.get("kind") != "allowed_window":
                    continue
                # A one-sided availability constraint is complete when paired
                # with the handoff's bounded planning window: ``not_before``
                # inherits its upper bound and ``not_after`` its lower bound.
                # This preserves the requested inequality without asking the
                # user to invent a redundant second timestamp.
                if constraint.get("start_at") in (None, ""):
                    constraint["start_at"] = arguments.get("window_start")
                if constraint.get("end_at") in (None, ""):
                    constraint["end_at"] = arguments.get("window_end")
            if len(handoff_task_ids) == 1:
                for constraint in arguments["request_constraints"]:
                    if constraint.get("kind") in {"exact_start", "contiguous"}:
                        constraint["task_ids"] = list(handoff_task_ids)
        normalized_recurrence = (
            _normalize_recurring_schedule_tool_argument(arguments["recurrence"])
            if isinstance(arguments.get("recurrence"), dict)
            else {}
        )
        task_ids_for_recurrence = [
            task_id
            for task_id in arguments.get("task_ids") or []
            if isinstance(task_id, int) and not isinstance(task_id, bool)
        ]
        authoritative_recurrence: dict[str, Any] | None = None
        if len(task_ids_for_recurrence) == 1 and isinstance(tool_result_store, dict):
            target_task_id = task_ids_for_recurrence[0]
            for stored_result in reversed(list(tool_result_store.values())):
                blocks = (
                    stored_result.get("blocks")
                    if isinstance(stored_result, dict)
                    else None
                )
                if not isinstance(blocks, list):
                    continue
                matching_rules = [
                    block.get("recurrence_rule")
                    for block in blocks
                    if isinstance(block, dict)
                    and block.get("task_id") == target_task_id
                    and isinstance(block.get("recurrence_rule"), dict)
                ]
                if matching_rules:
                    authoritative_recurrence = _normalize_recurring_schedule_tool_argument(
                        matching_rules[0]
                    )
                    break
        if authoritative_recurrence is not None and (
            normalized_recurrence
            or arguments.get("target_scope") == "series"
        ):
            normalized_recurrence = {
                **authoritative_recurrence,
                **normalized_recurrence,
            }
        if normalized_recurrence:
            retained_constraints: list[dict[str, Any]] = []
            for constraint in arguments.get("request_constraints", []):
                clock_value = str(constraint.get("start_at") or "").strip()
                clock_match = re.fullmatch(
                    r"((?:[01]\d|2[0-3]):[0-5]\d)(?::[0-5]\d)?",
                    clock_value,
                )
                if constraint.get("kind") == "exact_start" and clock_match:
                    # A recurrence owns its per-occurrence clock anchor. Some
                    # providers additionally emit that anchor as a one-time
                    # exact_start constraint (for example ``19:00``), whose
                    # contract correctly requires a complete local date-time.
                    # Conserve the meaning in the recurrence and remove only
                    # the redundant, structurally inapplicable copy.
                    normalized_recurrence["preferred_start_time"] = (
                        clock_match.group(1)[:5]
                    )
                    normalized_recurrence["strict"] = True
                    continue
                retained_constraints.append(constraint)
            arguments["request_constraints"] = retained_constraints
            recurrence_schema = (
                spec.input_schema.get("properties", {})
                .get("recurrence", {})
                .get("properties", {})
            )
            arguments["recurrence"] = {
                key: value
                for key, value in normalized_recurrence.items()
                if key in recurrence_schema
            }
    if name in {"propose_preference_memory", "submit_preference_plan"}:
        arguments = _normalize_preference_plan_tool_argument(
            arguments,
            default_source_text=original_user_request,
        )
    if name in {"propose_tree_operation", "submit_task_tree_plan"}:
        arguments = _normalize_semantic_plan_container_aliases(arguments)
        created_schedule = arguments.get("created_task_schedule")
        if isinstance(created_schedule, dict):
            top_target_id = arguments.get("target_task_id")
            target_ids = created_schedule.get("target_task_ids")
            if (
                isinstance(top_target_id, int)
                and not isinstance(top_target_id, bool)
                and not isinstance(target_ids, list)
            ):
                created_schedule["target_task_ids"] = [top_target_id]
            if not created_schedule.get("target_task_ids"):
                top_target_title = _clean_text(arguments.get("target_task_title"))
                exact_matches = [
                    node
                    for node in nodes
                    if node.id is not None
                    and top_target_title
                    and str(node.title or "").strip().casefold()
                    == top_target_title.casefold()
                ]
                if len(exact_matches) == 1:
                    created_schedule["target_task_ids"] = [
                        int(exact_matches[0].id)
                    ]
        if (
            _clean_text(arguments.get("revision_of_proposal_id"))
            and not isinstance(pending_proposal, dict)
            and (
                isinstance(arguments.get("structural_operations"), list)
                or isinstance(arguments.get("created_task_schedule"), dict)
                or isinstance(arguments.get("recurring_schedule"), dict)
            )
        ):
            # A provider can echo a conversational/review identifier into the
            # revision field on an otherwise complete fresh plan. With no
            # authoritative pending proposal there is nothing to revise; treat
            # the complete payload as fresh instead of failing on a phantom ID.
            arguments.pop("revision_of_proposal_id", None)
            arguments.pop("replace_structural_operations", None)
            arguments.pop("replace_recurring_schedule", None)
        raw_operations = arguments.get("structural_operations")
        dependency_pairs: list[tuple[str, str]] = []
        if isinstance(raw_operations, list):
            for item in raw_operations:
                if not isinstance(item, dict):
                    continue
                after_temp_id = _clean_text(
                    _first_present(item, "temp_id", "tempId")
                )
                raw_dependencies = _first_present(
                    item,
                    "depends_on_temp_ids",
                    "dependsOnTempIds",
                )
                if isinstance(raw_dependencies, str):
                    raw_dependencies = [raw_dependencies]
                if not after_temp_id or not isinstance(raw_dependencies, list):
                    continue
                dependency_pairs.extend(
                    (before_temp_id, after_temp_id)
                    for value in raw_dependencies
                    if (before_temp_id := _clean_text(value))
                    and before_temp_id != after_temp_id
                )
        has_canonical_schedule = isinstance(
            arguments.get("created_task_schedule"),
            dict,
        )
        if isinstance(raw_operations, list) and not has_canonical_schedule:
            nested_schedule_indexes = [
                index
                for index, item in enumerate(raw_operations)
                if isinstance(item, dict)
                and isinstance(item.get("schedule"), dict)
                and bool(item["schedule"])
            ]
            if nested_schedule_indexes:
                return _tool_argument_error(
                    spec,
                    [
                        (
                            "structural_operations"
                            f"[{nested_schedule_indexes[0]}].schedule cannot be "
                            "the only scheduling contract; move its targets and "
                            "window into created_task_schedule."
                        )
                    ],
                    repair_hint=(
                        "Preserve the requested window in created_task_schedule "
                        "and retry the same semantic plan."
                    ),
                )
        if isinstance(raw_operations, list):
            exact_nodes_by_title: dict[str, list[StrategyNode]] = {}
            for node in nodes:
                if node.id is None:
                    continue
                exact_nodes_by_title.setdefault(
                    str(node.title or "").strip().casefold(), []
                ).append(node)
            exact_categories_by_name: dict[str, list[Category]] = {}
            for category in categories:
                if category.id is None:
                    continue
                exact_categories_by_name.setdefault(
                    str(category.name or "").strip().casefold(), []
                ).append(category)
            anchored_raw_operations: list[Any] = []
            for raw_item in raw_operations:
                if not isinstance(raw_item, dict):
                    anchored_raw_operations.append(raw_item)
                    continue
                item = copy.deepcopy(raw_item)
                source_title = _clean_text(
                    _first_present(
                        item,
                        "source_task_title",
                        "sourceTaskTitle",
                        "task_title",
                        "taskTitle",
                        "target_task_title",
                        "targetTaskTitle",
                    )
                )
                source_matches = exact_nodes_by_title.get(
                    source_title.casefold() if source_title else "", []
                )
                if len(source_matches) == 1:
                    item["source_task_id"] = int(source_matches[0].id)
                destination_title = _clean_text(
                    _first_present(
                        item,
                        "destination_parent_task_title",
                        "destinationParentTaskTitle",
                        "destination_parent_title",
                        "destinationParentTitle",
                    )
                )
                destination_matches = exact_nodes_by_title.get(
                    destination_title.casefold() if destination_title else "", []
                )
                if len(destination_matches) == 1:
                    item["destination_parent_task_id"] = int(
                        destination_matches[0].id
                    )
                category_name = _clean_text(
                    _first_present(
                        item,
                        "destination_category_name",
                        "destinationCategoryName",
                    )
                )
                category_matches = exact_categories_by_name.get(
                    category_name.casefold() if category_name else "", []
                )
                if len(category_matches) == 1:
                    item["destination_category_id"] = int(category_matches[0].id)
                anchored_raw_operations.append(item)
            raw_operations = anchored_raw_operations
            created_temp_positions = {
                temp_id: position
                for position, item in enumerate(
                    raw_item
                    for raw_item in raw_operations
                    if isinstance(raw_item, dict)
                    and normalize_structural_operation_kind(
                        _first_present(raw_item, "kind", "operation", "op", "type", "action")
                    ) == "create_node"
                )
                if (temp_id := _clean_text(_first_present(item, "temp_id", "tempId")))
            }
            arguments["structural_operations"] = [
                _normalize_tree_operation_tool_argument(
                    item,
                    created_temp_positions=created_temp_positions,
                )
                if isinstance(item, dict)
                else item
                for item in raw_operations
            ]
            # A task's explicit parent owns its workspace. Resolve that closed
            # invariant at the shared boundary so a harmless duplicate category
            # field cannot force a retry or tempt a later attempt to drop the
            # operation. This applies equally to persisted and newly-created
            # parents and does not infer parentage from prose.
            parent_category_by_task_id = {
                int(node.id): int(node.category_id)
                for node in nodes
                if isinstance(node.id, int)
                and not isinstance(node.id, bool)
                and isinstance(node.category_id, int)
                and not isinstance(node.category_id, bool)
            }
            creates_by_temp_id = {
                temp_id: operation
                for operation in arguments["structural_operations"]
                if isinstance(operation, dict)
                and operation.get("kind") == "create_node"
                and (temp_id := _clean_text(operation.get("temp_id")))
            }
            categories_by_id = {
                int(category.id): category
                for category in categories
                if isinstance(category.id, int) and not isinstance(category.id, bool)
            }
            nodes_by_id = {
                int(node.id): node
                for node in nodes
                if isinstance(node.id, int) and not isinstance(node.id, bool)
            }
            categories_by_name = {
                str(category.name or "").strip().casefold(): int(category.id)
                for category in categories
                if isinstance(category.id, int)
                and not isinstance(category.id, bool)
                and str(category.name or "").strip()
            }
            for operation in creates_by_temp_id.values():
                category_name = _clean_text(operation.get("category_name"))
                category_id = categories_by_name.get(
                    category_name.casefold() if category_name else ""
                )
                if category_id is not None:
                    operation["category_id"] = category_id
            for operation in creates_by_temp_id.values():
                parent_task_id = _first_present(
                    operation, "parent_id", "parent_task_id"
                )
                parent_title = _clean_text(
                    _first_present(
                        operation, "parent_title", "parent_task_title"
                    )
                )
                category = (
                    categories_by_id.get(parent_task_id)
                    if isinstance(parent_task_id, int)
                    else None
                )
                actual_parent = (
                    nodes_by_id.get(parent_task_id)
                    if isinstance(parent_task_id, int)
                    else None
                )
                if (
                    category is not None
                    and parent_title
                    and parent_title.casefold()
                    == str(category.name).strip().casefold()
                    and (
                        actual_parent is None
                        or parent_title.casefold()
                        != str(actual_parent.title).strip().casefold()
                    )
                ):
                    # The provider used the workspace/category namespace as a
                    # task-parent namespace. Preserve the workspace selection
                    # and represent the requested task as top-level.
                    operation["category_id"] = int(category.id)
                    for key in (
                        "parent_id",
                        "parent_task_id",
                        "parent_title",
                        "parent_task_title",
                    ):
                        operation.pop(key, None)
            for operation in creates_by_temp_id.values():
                parent_task_id = _first_present(
                    operation, "parent_id", "parent_task_id"
                )
                if (
                    isinstance(parent_task_id, int)
                    and not isinstance(parent_task_id, bool)
                    and parent_task_id in parent_category_by_task_id
                ):
                    operation["category_id"] = parent_category_by_task_id[
                        parent_task_id
                    ]
            # Propagate created-parent workspaces through arbitrarily deep but
            # bounded submitted trees without depending on operation order.
            for _ in range(len(creates_by_temp_id)):
                changed = False
                for operation in creates_by_temp_id.values():
                    parent_temp_id = _clean_text(operation.get("parent_temp_id"))
                    parent = creates_by_temp_id.get(parent_temp_id or "")
                    if not isinstance(parent, dict):
                        continue
                    parent_category_id = parent.get("category_id")
                    if (
                        isinstance(parent_category_id, int)
                        and not isinstance(parent_category_id, bool)
                        and operation.get("category_id") != parent_category_id
                    ):
                        operation["category_id"] = parent_category_id
                        changed = True
                if not changed:
                    break
        recurring_schedule = arguments.get("recurring_schedule")
        if isinstance(recurring_schedule, dict):
            normalized_recurrence = _normalize_recurring_schedule_tool_argument(
                recurring_schedule
            )
            normalized_operations = arguments.get("structural_operations")
            create_operations = [
                operation
                for operation in normalized_operations
                if isinstance(operation, dict)
                and operation.get("kind") == "create_node"
            ] if isinstance(normalized_operations, list) else []
            target_temp_id = _clean_text(normalized_recurrence.get("target_temp_id"))
            matching_creates = [
                operation
                for operation in create_operations
                if not target_temp_id
                or _clean_text(operation.get("temp_id")) == target_temp_id
            ]
            if not target_temp_id and len(matching_creates) == 1:
                target_temp_id = _clean_text(matching_creates[0].get("temp_id"))
                if target_temp_id:
                    normalized_recurrence["target_temp_id"] = target_temp_id
            if normalized_recurrence.get("duration_minutes") in (None, ""):
                if len(matching_creates) == 1:
                    duration = matching_creates[0].get("time_estimate_minutes")
                    if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
                        normalized_recurrence["duration_minutes"] = duration
            if normalized_recurrence.get("duration_minutes") in (None, ""):
                # Recurrence needs a concrete occurrence duration before it can
                # compile. Match the one-time planner's neutral bounded fallback
                # instead of forcing a user clarification for ordinary tasks.
                normalized_recurrence["duration_minutes"] = 45
            if normalized_recurrence.get("start_date") in (None, ""):
                # The server's authoritative local turn date is the recurrence
                # series anchor. days_of_week still determines actual weekly
                # occurrence dates, so this does not invent a requested weekday.
                normalized_recurrence["start_date"] = (
                    today_override or date.today()
                ).isoformat()
            arguments["recurring_schedule"] = normalized_recurrence
        recurring_updates = arguments.get("recurring_schedule_updates")
        if (
            isinstance(recurring_updates, list)
            and len(recurring_updates) == 1
            and isinstance(recurring_updates[0], dict)
        ):
            recurring_updates = recurring_updates[0]
        if isinstance(recurring_updates, dict):
            arguments["recurring_schedule_updates"] = (
                _normalize_recurring_schedule_tool_argument(recurring_updates)
            )
        created_task_schedule = arguments.get("created_task_schedule")
        if isinstance(created_task_schedule, dict):
            normalized_created_schedule = _normalize_created_task_schedule_tool_argument(
                created_task_schedule,
                default_instruction=original_user_request,
            )
            create_temp_ids = [
                temp_id
                for operation in (arguments.get("structural_operations") or [])
                if isinstance(operation, dict)
                and operation.get("kind") == "create_node"
                and (temp_id := _clean_text(operation.get("temp_id")))
            ]
            raw_groups = normalized_created_schedule.get("schedule_groups")
            schedule_targets: list[dict[str, Any]]
            if isinstance(raw_groups, list):
                schedule_targets = [group for group in raw_groups if isinstance(group, dict)]
                claimed = {
                    temp_id
                    for group in schedule_targets
                    for value in (group.get("target_temp_ids") or [])
                    if (temp_id := _clean_text(value))
                }
                missing_groups = [
                    group
                    for group in schedule_targets
                    if not group.get("target_temp_ids")
                    and not group.get("target_task_ids")
                ]
                remaining = [temp_id for temp_id in create_temp_ids if temp_id not in claimed]
                if len(missing_groups) == 1 and remaining:
                    missing_groups[0]["target_temp_ids"] = remaining
                elif len(missing_groups) == len(remaining) and missing_groups:
                    for group, temp_id in zip(missing_groups, remaining, strict=True):
                        group["target_temp_ids"] = [temp_id]
            else:
                schedule_targets = [normalized_created_schedule]

            for before_temp_id, after_temp_id in dict.fromkeys(dependency_pairs):
                owner = next(
                    (
                        group
                        for group in schedule_targets
                        if after_temp_id in (group.get("target_temp_ids") or [])
                        and before_temp_id in (group.get("target_temp_ids") or [])
                    ),
                    None,
                )
                if owner is None and len(schedule_targets) == 1:
                    owner = schedule_targets[0]
                if owner is None:
                    continue
                owned_temp_ids = owner.get("target_temp_ids")
                if not isinstance(owned_temp_ids, list):
                    owned_temp_ids = []
                    owner["target_temp_ids"] = owned_temp_ids
                for dependency_temp_id in (before_temp_id, after_temp_id):
                    if (
                        dependency_temp_id in create_temp_ids
                        and dependency_temp_id not in owned_temp_ids
                    ):
                        owned_temp_ids.append(dependency_temp_id)
                constraints = owner.setdefault("request_constraints", [])
                candidate = {
                    "kind": "task_order",
                    "hard": True,
                    "label": "Task order",
                    "source_text": (
                        _clean_text(original_user_request)
                        or "Preserve the requested order between created tasks."
                    ),
                    "before_temp_id": before_temp_id,
                    "after_temp_id": after_temp_id,
                }
                if candidate not in constraints:
                    constraints.append(candidate)
            arguments["created_task_schedule"] = normalized_created_schedule
        raw_request_items = arguments.get("request_items")
        if isinstance(raw_request_items, dict):
            raw_request_items = [raw_request_items]
        normalized_request_items: list[dict[str, Any]] = []
        if isinstance(raw_request_items, list):
            for raw_item in raw_request_items:
                if not isinstance(raw_item, dict):
                    continue
                item = copy.deepcopy(raw_item)
                if item.get("source_text") in (None, ""):
                    item["source_text"] = _first_present(
                        item, "sourceText", "text", "clause", "description"
                    )
                raw_changes = _first_present(
                    item, "changes", "actions", "intents", "change"
                )
                if isinstance(raw_changes, str):
                    raw_changes = [raw_changes]
                if isinstance(raw_changes, dict):
                    # Providers often use an expressive ledger object, e.g.
                    # {"create": true, "scheduling": "leave_unscheduled"}.
                    # Classify known action keys, but let an explicit negative
                    # scheduling value veto only that key.  Unknown wrapper keys
                    # (such as "action") are classified from their values.
                    expanded_changes: list[Any] = []
                    for raw_key, raw_value in raw_changes.items():
                        key_changes = _canonical_request_changes(raw_key)
                        value_changes = _canonical_request_changes(raw_value)
                        key_is_schedule = "schedule" in key_changes
                        value_is_explicit_unscheduled = (
                            key_is_schedule
                            and isinstance(raw_value, str)
                            and not value_changes
                            and any(
                                token in re.sub(
                                    r"[^a-z0-9]+",
                                    "_",
                                    raw_value.casefold(),
                                ).strip("_").split("_")
                                for token in ("unscheduled", "later", "defer", "not", "dont")
                            )
                        )
                        if key_changes and not value_is_explicit_unscheduled:
                            expanded_changes.append(raw_key)
                        elif not key_changes and value_changes:
                            expanded_changes.append(raw_value)
                    raw_changes = expanded_changes
                item["changes"] = list(
                    dict.fromkeys(
                        canonical_change
                        for change in (raw_changes or [])
                        for canonical_change in _canonical_request_changes(change)
                    )
                )
                for target, aliases in {
                    "target_temp_ids": ("targetTempIds", "temp_ids", "tempIds"),
                    "target_task_ids": ("targetTaskIds", "task_ids", "taskIds"),
                    "schedule_group_index": ("scheduleGroupIndex", "group_index", "groupIndex"),
                }.items():
                    if item.get(target) in (None, "", [], {}):
                        item[target] = _first_present(item, *aliases)
                for alias in (
                    "sourceText", "text", "clause", "description", "actions", "intents",
                    "change", "targetTempIds", "temp_ids", "tempIds", "targetTaskIds",
                    "task_ids", "taskIds", "scheduleGroupIndex", "group_index", "groupIndex",
                ):
                    item.pop(alias, None)
                item = {
                    key: value
                    for key, value in item.items()
                    if key in REQUEST_ITEM_INPUT_SCHEMA["properties"]
                    and value not in (None, "", [], {})
                }
                if item.get("source_text") and item.get("changes"):
                    normalized_request_items.append(item)

        operation_change = {
            "create_node": "create",
            "metadata_update": "update",
            "rename_node": "update",
            "move_subtree": "move",
            "archive_node": "archive",
            "delete_node": "delete",
        }
        operations = [
            operation
            for operation in (arguments.get("structural_operations") or [])
            if isinstance(operation, dict)
        ]
        inferred_ownership: tuple[str, str, str, str] | None = None
        if arguments.get("created_task_schedule"):
            inferred_ownership = _inferred_deferred_schedule_ownership(
                original_user_request=original_user_request,
                operations=operations,
            )
            if inferred_ownership is not None:
                (
                    scheduled_temp_id,
                    scheduled_source,
                    deferred_temp_id,
                    deferred_source,
                ) = inferred_ownership
                conserved_items: list[dict[str, Any]] = []
                for item in normalized_request_items:
                    targets = list(item.get("target_temp_ids") or [])
                    changes = list(item.get("changes") or [])
                    if deferred_temp_id not in targets or "schedule" not in changes:
                        conserved_items.append(item)
                        continue
                    retained_targets = [
                        target for target in targets if target != deferred_temp_id
                    ]
                    if retained_targets:
                        conserved_items.append(
                            {**item, "target_temp_ids": retained_targets}
                        )
                    deferred_changes = [
                        change for change in changes if change != "schedule"
                    ]
                    if "create" not in deferred_changes:
                        deferred_changes.insert(0, "create")
                    conserved_items.append(
                        {
                            **item,
                            "source_text": deferred_source,
                            "changes": deferred_changes,
                            "target_temp_ids": [deferred_temp_id],
                        }
                    )
                normalized_request_items = conserved_items
                scheduled_item = next(
                    (
                        item
                        for item in normalized_request_items
                        if scheduled_temp_id in (item.get("target_temp_ids") or [])
                    ),
                    None,
                )
                if scheduled_item is not None:
                    scheduled_item["changes"] = list(
                        dict.fromkeys(
                            [*scheduled_item.get("changes", []), "create", "schedule"]
                        )
                    )
                deferred_item = next(
                    (
                        item
                        for item in normalized_request_items
                        if deferred_temp_id in (item.get("target_temp_ids") or [])
                        and "schedule" not in (item.get("changes") or [])
                    ),
                    None,
                )
                normalized_request_items.extend(
                    [
                        *(
                            []
                            if scheduled_item is not None
                            else [
                                {
                                    "source_text": scheduled_source,
                                    "changes": ["create", "schedule"],
                                    "target_temp_ids": [scheduled_temp_id],
                                }
                            ]
                        ),
                        *(
                            []
                            if deferred_item is not None
                            else [
                                {
                                    "source_text": deferred_source,
                                    "changes": ["create"],
                                    "target_temp_ids": [deferred_temp_id],
                                }
                            ]
                        ),
                    ]
                )
        request_source_by_temp_id: dict[str, list[str]] = {}
        for item in normalized_request_items:
            source_text = _clean_text(item.get("source_text"))
            if not source_text:
                continue
            for target in item.get("target_temp_ids") or []:
                if isinstance(target, str) and target.strip():
                    request_source_by_temp_id.setdefault(target.strip(), []).append(
                        source_text
                    )
        child_count_by_parent_temp_id: dict[str, int] = {}
        for operation in operations:
            parent_temp_id = _clean_text(operation.get("parent_temp_id"))
            if parent_temp_id:
                child_count_by_parent_temp_id[parent_temp_id] = (
                    child_count_by_parent_temp_id.get(parent_temp_id, 0) + 1
                )
        for operation in operations:
            temp_id = _clean_text(operation.get("temp_id"))
            parent_temp_id = _clean_text(operation.get("parent_temp_id"))
            estimate = operation.get("time_estimate_minutes")
            source_fragments = request_source_by_temp_id.get(temp_id, [])
            if (
                temp_id
                and parent_temp_id
                and isinstance(estimate, int)
                and not isinstance(estimate, bool)
                and estimate > 60
                and child_count_by_parent_temp_id.get(parent_temp_id, 0) >= 2
                and source_fragments
                and not any(
                    parse_prompt_estimate_minutes(fragment) is not None
                    for fragment in source_fragments
                )
            ):
                # Sibling operations are already a first level of executable
                # decomposition. Keep model-inferred drafts small enough to be
                # independently placeable; a longer child should be decomposed
                # again instead of silently consuming an oversized block.
                # Preserve explicit user durations, only children, and every
                # standalone substantive task unchanged.
                operation["time_estimate_minutes"] = 60
        for operation in operations:
            temp_id = _clean_text(operation.get("temp_id"))
            task_id = _first_present(operation, "node_id", "task_id", "source_task_id")
            change = operation_change.get(str(operation.get("kind") or ""))
            already_covered = any(
                (temp_id and temp_id in (item.get("target_temp_ids") or []))
                or (
                    isinstance(task_id, int)
                    and task_id in (item.get("target_task_ids") or [])
                )
                for item in normalized_request_items
            )
            if change and not already_covered:
                normalized_request_items.append(
                    {
                        "source_text": (
                            _clean_text(operation.get("title"))
                            or _clean_text(operation.get("source_task_title"))
                            or _clean_text(original_user_request)
                            or "Requested task change"
                        ),
                        "changes": [change],
                        **({"target_temp_ids": [temp_id]} if temp_id else {}),
                        **(
                            {"target_task_ids": [task_id]}
                            if isinstance(task_id, int) and not isinstance(task_id, bool)
                            else {}
                        ),
                    }
                )

        normalized_created_schedule = arguments.get("created_task_schedule")
        schedule_groups = (
            normalized_created_schedule.get("schedule_groups")
            if isinstance(normalized_created_schedule, dict)
            else None
        )
        schedule_owners = (
            [group for group in schedule_groups if isinstance(group, dict)]
            if isinstance(schedule_groups, list)
            else [normalized_created_schedule]
            if isinstance(normalized_created_schedule, dict)
            else []
        )
        create_by_temp_id = {
            temp_id: operation
            for operation in operations
            if str(operation.get("kind") or "") == "create_node"
            and (temp_id := _clean_text(operation.get("temp_id")))
        }
        children_by_parent_temp_id: dict[str, set[str]] = {}
        for child_temp_id, operation in create_by_temp_id.items():
            parent_temp_id = _clean_text(operation.get("parent_temp_id"))
            if parent_temp_id:
                children_by_parent_temp_id.setdefault(parent_temp_id, set()).add(
                    child_temp_id
                )
        all_owned_temp_ids = {
            target
            for owner in schedule_owners
            if isinstance(owner, dict)
            for target in (owner.get("target_temp_ids") or [])
            if isinstance(target, str) and target.strip()
        }
        pruned_summary_parent_ids: set[str] = set()
        for parent_temp_id, child_temp_ids in children_by_parent_temp_id.items():
            parent_operation = create_by_temp_id.get(parent_temp_id, {})
            parent_fields = (
                parent_operation.get("fields")
                if isinstance(parent_operation.get("fields"), dict)
                else {}
            )
            parent_estimate = _first_present(
                parent_operation,
                "time_estimate_minutes",
                "estimate_minutes",
            )
            if parent_estimate in (None, ""):
                parent_estimate = _first_present(
                    parent_fields,
                    "time_estimate_minutes",
                    "estimate_minutes",
                )
            if (
                parent_temp_id in all_owned_temp_ids
                and child_temp_ids.intersection(all_owned_temp_ids)
                and (
                    len(child_temp_ids) >= 2
                    or not (
                        isinstance(parent_estimate, int)
                        and not isinstance(parent_estimate, bool)
                        and parent_estimate > 0
                    )
                )
            ):
                pruned_summary_parent_ids.add(parent_temp_id)
        for owner in schedule_owners:
            if not isinstance(owner, dict) or not isinstance(
                owner.get("target_temp_ids"), list
            ):
                continue
            if pruned_summary_parent_ids:
                # A zero-effort outcome/grouping node is represented by its
                # executable children. Scheduling both double-counts work and
                # can make an otherwise feasible compound plan overflow, even
                # when the provider put them in separate overlapping groups.
                owner["target_temp_ids"] = [
                    target
                    for target in owner["target_temp_ids"]
                    if target not in pruned_summary_parent_ids
                ]
            remaining_temp_ids = [
                target
                for target in owner.get("target_temp_ids", [])
                if isinstance(target, str) and target.strip()
            ]
            try:
                short_window_start = datetime.fromisoformat(
                    str(owner.get("window_start") or "")
                ).replace(tzinfo=None)
                short_window_end = datetime.fromisoformat(
                    str(owner.get("window_end") or "")
                ).replace(tzinfo=None)
                short_window_minutes = int(
                    (short_window_end - short_window_start).total_seconds() // 60
                )
            except (TypeError, ValueError):
                short_window_minutes = 0
            if 5 <= short_window_minutes <= 30 and len(remaining_temp_ids) > 1:
                exact_constraint_temp_ids = list(
                    dict.fromkeys(
                        target
                        for constraint in (owner.get("request_constraints") or [])
                        if isinstance(constraint, dict)
                        and constraint.get("kind") == "exact_start"
                        for target in (constraint.get("target_temp_ids") or [])
                        if target in remaining_temp_ids
                    )
                )
                ledger_scheduled_temp_ids = {
                    target
                    for item in normalized_request_items
                    if "schedule" in (item.get("changes") or [])
                    for target in (item.get("target_temp_ids") or [])
                    if target in remaining_temp_ids
                }
                ledger_unscheduled_temp_ids = {
                    target
                    for item in normalized_request_items
                    if "schedule" not in (item.get("changes") or [])
                    for target in (item.get("target_temp_ids") or [])
                    if target in remaining_temp_ids
                }
                ledger_only_scheduled = sorted(
                    ledger_scheduled_temp_ids - ledger_unscheduled_temp_ids
                )
                leaf_ids = [
                    target
                    for target in remaining_temp_ids
                    if target not in children_by_parent_temp_id
                ]
                title_tokens_by_leaf: dict[str, set[str]] = {
                    target: set(
                        re.findall(
                            r"[a-z0-9]+",
                            str(
                                create_by_temp_id.get(target, {}).get("title")
                                or ""
                            ).casefold(),
                        )
                    )
                    for target in leaf_ids
                }
                shared_title_tokens = (
                    set.intersection(*title_tokens_by_leaf.values())
                    if title_tokens_by_leaf
                    and all(title_tokens_by_leaf.values())
                    else set()
                )
                instruction_tokens = set(
                    re.findall(
                        r"[a-z0-9]+",
                        str(owner.get("instruction") or "").casefold(),
                    )
                )
                instruction_scores = {
                    target: len(
                        (tokens - shared_title_tokens) & instruction_tokens
                    )
                    for target, tokens in title_tokens_by_leaf.items()
                }
                best_instruction_score = max(
                    instruction_scores.values(), default=0
                )
                instruction_owned_leaf_ids = [
                    target
                    for target, score in instruction_scores.items()
                    if score == best_instruction_score and score >= 3
                ]
                fitting_leaf_ids: list[str] = []
                bounded_atomic_leaf_ids: list[str] = []
                all_others_structurally_infeasible = True
                for target in remaining_temp_ids:
                    operation = create_by_temp_id.get(target, {})
                    fields = (
                        operation.get("fields")
                        if isinstance(operation.get("fields"), dict)
                        else {}
                    )
                    estimate = _first_present(
                        operation,
                        "time_estimate_minutes",
                        "estimate_minutes",
                    )
                    if estimate in (None, ""):
                        estimate = _first_present(
                            fields,
                            "time_estimate_minutes",
                            "estimate_minutes",
                        )
                    is_leaf = target not in children_by_parent_temp_id
                    fits = (
                        is_leaf
                        and isinstance(estimate, int)
                        and not isinstance(estimate, bool)
                        and 0 < estimate <= short_window_minutes
                    )
                    if fits:
                        fitting_leaf_ids.append(target)
                    if (
                        is_leaf
                        and isinstance(estimate, int)
                        and not isinstance(estimate, bool)
                        and 0 < estimate <= 30
                    ):
                        bounded_atomic_leaf_ids.append(target)
                    elif not (
                        target in children_by_parent_temp_id
                        or estimate in (None, "")
                        or (
                            isinstance(estimate, int)
                            and not isinstance(estimate, bool)
                            and estimate > short_window_minutes
                        )
                    ):
                        all_others_structurally_infeasible = False
                selected = (
                    exact_constraint_temp_ids[0]
                    if len(exact_constraint_temp_ids) == 1
                    and exact_constraint_temp_ids[0]
                    not in children_by_parent_temp_id
                    else ledger_only_scheduled[0]
                    if len(ledger_only_scheduled) == 1
                    and ledger_only_scheduled[0] not in children_by_parent_temp_id
                    else instruction_owned_leaf_ids[0]
                    if len(instruction_owned_leaf_ids) == 1
                    else fitting_leaf_ids[0]
                    if len(fitting_leaf_ids) == 1
                    and all_others_structurally_infeasible
                    else bounded_atomic_leaf_ids[0]
                    if not fitting_leaf_ids
                    and len(bounded_atomic_leaf_ids) == 1
                    and all_others_structurally_infeasible
                    else None
                )
                if selected is not None:
                    selected_operation = create_by_temp_id.get(selected, {})
                    selected_estimate = selected_operation.get(
                        "time_estimate_minutes"
                    )
                    if (
                        isinstance(selected_estimate, int)
                        and not isinstance(selected_estimate, bool)
                        and selected_estimate > short_window_minutes
                    ):
                        # A user-specified bounded window is authoritative for
                        # this one atomic action. This is the same duration
                        # inference used by the single-target planner path.
                        selected_operation["time_estimate_minutes"] = (
                            short_window_minutes
                        )
                    pruned_summary_parent_ids.update(
                        target for target in remaining_temp_ids if target != selected
                    )
                    owner["target_temp_ids"] = [selected]
        if isinstance(schedule_groups, list):
            schedule_groups[:] = [
                owner
                for owner in schedule_owners
                if (owner.get("target_temp_ids") or owner.get("target_task_ids"))
            ]
            schedule_owners = schedule_groups
        if pruned_summary_parent_ids:
            for item in normalized_request_items:
                targets = set(item.get("target_temp_ids") or [])
                if targets.intersection(pruned_summary_parent_ids):
                    item["changes"] = [
                        change
                        for change in item.get("changes", [])
                        if change != "schedule"
                    ]
        scheduled_temp_ids = {
            target
            for item in normalized_request_items
            if "schedule" in item.get("changes", [])
            for target in (item.get("target_temp_ids") or [])
            if isinstance(target, str) and target.strip()
        }
        scheduled_task_ids = {
            target
            for item in normalized_request_items
            if "schedule" in item.get("changes", [])
            for target in (item.get("target_task_ids") or [])
            if isinstance(target, int) and not isinstance(target, bool)
        }
        explicitly_unscheduled_temp_ids = {
            target
            for item in normalized_request_items
            if "schedule" not in item.get("changes", [])
            for target in (item.get("target_temp_ids") or [])
            if isinstance(target, str) and target.strip()
        } - scheduled_temp_ids
        explicitly_unscheduled_task_ids = {
            target
            for item in normalized_request_items
            if "schedule" not in item.get("changes", [])
            for target in (item.get("target_task_ids") or [])
            if isinstance(target, int) and not isinstance(target, bool)
        } - scheduled_task_ids
        # The request-item ledger is the model's per-clause ownership contract.
        # A task explicitly marked create/update-only must not be swept into a
        # broad schedule group merely because it shares a task tree with timed
        # work. This keeps tree parentage independent from calendar ownership.
        if scheduled_temp_ids or scheduled_task_ids:
            for owner in schedule_owners:
                if not isinstance(owner, dict):
                    continue
                if isinstance(owner.get("target_temp_ids"), list):
                    owner["target_temp_ids"] = [
                        target
                        for target in owner["target_temp_ids"]
                        if target not in explicitly_unscheduled_temp_ids
                    ]
                if isinstance(owner.get("target_task_ids"), list):
                    owner["target_task_ids"] = [
                        target
                        for target in owner["target_task_ids"]
                        if target not in explicitly_unscheduled_task_ids
                    ]
                owner_temp_ids = {
                    target
                    for target in (owner.get("target_temp_ids") or [])
                    if isinstance(target, str) and target.strip()
                }
                owner_task_ids = {
                    target
                    for target in (owner.get("target_task_ids") or [])
                    if isinstance(target, int) and not isinstance(target, bool)
                }
                constraints = owner.get("request_constraints")
                if not isinstance(constraints, list):
                    continue
                valid_constraints: list[dict[str, Any]] = []
                for raw_constraint in constraints:
                    if not isinstance(raw_constraint, dict):
                        continue
                    constraint = copy.deepcopy(raw_constraint)
                    if isinstance(constraint.get("target_temp_ids"), list):
                        constraint["target_temp_ids"] = [
                            target
                            for target in constraint["target_temp_ids"]
                            if target in owner_temp_ids
                        ]
                    if isinstance(constraint.get("task_ids"), list):
                        constraint["task_ids"] = [
                            target
                            for target in constraint["task_ids"]
                            if target in owner_task_ids
                        ]
                    kind = str(constraint.get("kind") or "").strip()
                    if kind in {"exact_start", "contiguous"}:
                        references = [
                            *(constraint.get("target_temp_ids") or []),
                            *(constraint.get("task_ids") or []),
                        ]
                        if not references and len(owner_temp_ids) + len(owner_task_ids) == 1:
                            if owner_temp_ids:
                                constraint["target_temp_ids"] = [next(iter(owner_temp_ids))]
                            else:
                                constraint["task_ids"] = [next(iter(owner_task_ids))]
                            references = [True]
                        if len(references) != 1:
                            continue
                    if kind == "task_order":
                        before_temp = constraint.get("before_temp_id")
                        after_temp = constraint.get("after_temp_id")
                        before_task = constraint.get("before_task_id")
                        after_task = constraint.get("after_task_id")
                        temp_order_valid = (
                            before_temp in owner_temp_ids
                            and after_temp in owner_temp_ids
                            and before_temp != after_temp
                        )
                        task_order_valid = (
                            before_task in owner_task_ids
                            and after_task in owner_task_ids
                            and before_task != after_task
                        )
                        if not (temp_order_valid or task_order_valid):
                            continue
                    if kind == "minimum_gap":
                        references = [
                            *(constraint.get("target_temp_ids") or []),
                            *(constraint.get("task_ids") or []),
                        ]
                        if references and len(references) != 2:
                            continue
                    valid_constraints.append(constraint)
                if valid_constraints:
                    owner["request_constraints"] = valid_constraints
                else:
                    owner.pop("request_constraints", None)
        for item in normalized_request_items:
            if "schedule" not in item.get("changes", []) or not schedule_owners:
                continue
            group_index = item.get("schedule_group_index")
            owner = (
                schedule_owners[group_index]
                if isinstance(group_index, int) and 0 <= group_index < len(schedule_owners)
                else schedule_owners[0]
                if len(schedule_owners) == 1
                else None
            )
            if owner is None:
                continue
            for target_key in ("target_temp_ids", "target_task_ids"):
                owned = owner.get(target_key)
                if not isinstance(owned, list):
                    owned = []
                    owner[target_key] = owned
                for target in (item.get(target_key) or []):
                    if target not in owned:
                        owned.append(target)
        if inferred_ownership is not None:
            scheduled_temp_id, _, deferred_temp_id, _ = inferred_ownership
            excluded_temp_ids = {
                deferred_temp_id,
                *pruned_summary_parent_ids,
                *(
                    parent_temp_id
                    for parent_temp_id, child_temp_ids in children_by_parent_temp_id.items()
                    if len(child_temp_ids) >= 2
                ),
            }
            for owner in schedule_owners:
                if not isinstance(owner, dict):
                    continue
                targets = owner.get("target_temp_ids")
                if (
                    not isinstance(targets, list)
                    or scheduled_temp_id not in targets
                ):
                    continue
                owner["target_temp_ids"] = [
                    target
                    for target in targets
                    if target not in excluded_temp_ids
                ]
                owned_temp_ids = set(owner["target_temp_ids"])
                constraints = owner.get("request_constraints")
                if not isinstance(constraints, list):
                    continue
                conserved_constraints: list[dict[str, Any]] = []
                for raw_constraint in constraints:
                    if not isinstance(raw_constraint, dict):
                        continue
                    constraint = copy.deepcopy(raw_constraint)
                    if isinstance(constraint.get("target_temp_ids"), list):
                        constraint["target_temp_ids"] = [
                            target
                            for target in constraint["target_temp_ids"]
                            if target in owned_temp_ids
                        ]
                    if constraint.get("kind") in {"exact_start", "contiguous"}:
                        references = constraint.get("target_temp_ids") or []
                        if not references and len(owned_temp_ids) == 1:
                            constraint["target_temp_ids"] = [
                                next(iter(owned_temp_ids))
                            ]
                            references = constraint["target_temp_ids"]
                        if len(references) != 1:
                            continue
                    conserved_constraints.append(constraint)
                if conserved_constraints:
                    owner["request_constraints"] = conserved_constraints
                else:
                    owner.pop("request_constraints", None)
        arguments["request_items"] = normalized_request_items or [
            {
                "source_text": _clean_text(original_user_request) or "Requested task plan",
                "changes": ["schedule"] if arguments.get("created_task_schedule") else ["create"],
            }
        ]
    try:
        arguments = spec.validate_v2_input(arguments)
    except ToolSemanticValidationError as error:
        return _tool_argument_error(
            spec,
            [str(error)],
            repair_hint=error.repair_guidance,
            affected_paths=error.affected_paths,
        )
    except ValidationError as error:
        validation_items = error.errors(include_url=False)
        return _tool_argument_error(
            spec,
            [
                "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in validation_items
                )
            ],
            affected_paths=[
                ".".join(str(part) for part in item["loc"])[:256]
                for item in validation_items
            ],
        )
    validation_errors = (
        []
        if spec.input_model is not None
        else [
            *_unknown_argument_errors(spec, arguments),
            *_required_argument_errors(spec, arguments),
        ]
    )
    if validation_errors:
        return _tool_argument_error(
            spec,
            validation_errors,
            repair_hint=(
                "Retry with task_id or task_title."
                if name in TASK_SCOPED_TOOL_NAMES
                else None
            ),
        )

    if name == "draft_change_set":
        if change_set_authorer is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "native_changeset_unavailable"},
            )
        try:
            result = change_set_authorer(arguments)
        except ValidationError as error:
            typed_error = ToolError(
                code="TOOL_VALIDATION_FAILED",
                category=ToolErrorCategory.MODEL_REPAIRABLE,
                retryable=False,
                message_for_model="Native ChangeSet input failed graph or contract validation.",
                repair_guidance="Repair the operation graph, constraints, or payload budget and retry.",
                details={"validationErrorCount": len(error.errors(include_url=False))},
            )
            result = {
                "error": "native_changeset_validation_failed",
                "tool_error": typed_error.model_dump(by_alias=True),
            }
        except Exception as error:
            from app.services.agent_kernel.change_set_authoring import (
                ChangeSetAuthorizationError,
                ChangeSetAuthoringContextError,
                ChangeSetConservationError,
                ChangeSetSchedulingEvidenceError,
                ChangeSetResolutionError,
                NativeChangeSetAuthoringError,
            )
            from app.services.agent_kernel.change_set_store import (
                ActiveChangeSetConflict,
                ChangeSetCommandConflict,
                ChangeSetRevisionConflict,
                NativeChangeSetStoreError,
            )

            if not isinstance(error, (NativeChangeSetAuthoringError, NativeChangeSetStoreError)):
                raise
            authorization = isinstance(error, ChangeSetAuthorizationError)
            context_unavailable = isinstance(error, ChangeSetAuthoringContextError)
            repairable = isinstance(
                error,
                (
                    ChangeSetConservationError,
                    ChangeSetSchedulingEvidenceError,
                    ChangeSetResolutionError,
                    ChangeSetRevisionConflict,
                    ChangeSetCommandConflict,
                    ActiveChangeSetConflict,
                ),
            )
            typed_error = ToolError(
                code=(
                    "TOOL_AUTHORIZATION_DENIED"
                    if authorization
                    else "TOOL_CONTEXT_UNAVAILABLE"
                    if context_unavailable
                    else "TOOL_VALIDATION_FAILED"
                ),
                category=(
                    ToolErrorCategory.AUTHORIZATION
                    if authorization
                    else ToolErrorCategory.MODEL_REPAIRABLE
                    if repairable
                    else ToolErrorCategory.RETRYABLE_SYSTEM
                ),
                retryable=context_unavailable,
                message_for_model=str(error),
                repair_guidance=(
                    "Reload current goal and ChangeSet revisions, then repair and resubmit the draft."
                    if repairable
                    else None
                ),
            )
            result = {
                "error": "native_changeset_authoring_failed",
                "tool_error": typed_error.model_dump(by_alias=True),
            }
        else:
            try:
                result = spec.validate_v2_output(result)
            except ValidationError as error:
                typed_error = ToolError(
                    code="TOOL_OUTPUT_INVALID",
                    category=ToolErrorCategory.FATAL_INVARIANT,
                    retryable=False,
                    message_for_model="Native ChangeSet authoring returned an invalid canonical result.",
                    details={"validationErrorCount": len(error.errors(include_url=False))},
                )
                result = {
                    "error": "native_changeset_output_invalid",
                    "tool_error": typed_error.model_dump(by_alias=True),
                }
        return _with_tool_metadata(name=name, arguments=arguments, result=result)
    if name == "simulate_change_set":
        if change_set_simulator is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "native_changeset_simulation_unavailable"},
            )
        try:
            result = change_set_simulator(arguments)
        except ValidationError as error:
            typed_error = ToolError(
                code="TOOL_VALIDATION_FAILED",
                category=ToolErrorCategory.MODEL_REPAIRABLE,
                retryable=False,
                message_for_model="Native ChangeSet simulation input failed contract validation.",
                details={"validationErrorCount": len(error.errors(include_url=False))},
            )
            result = {
                "error": "native_changeset_simulation_validation_failed",
                "tool_error": typed_error.model_dump(by_alias=True),
            }
        except Exception as error:
            from app.services.agent_kernel.change_set_authoring import ChangeSetAuthorizationError
            from app.services.agent_kernel.simulation import SimulationProjectionError
            from app.services.agent_kernel.simulation_coordinator import (
                ChangeSetSimulationError,
                SimulationCommandInProgress,
                SimulationInvariantError,
                SimulationRevisionConflict,
            )

            authorization = isinstance(error, ChangeSetAuthorizationError)
            stale = isinstance(error, SimulationRevisionConflict)
            invariant = isinstance(error, SimulationInvariantError)
            in_progress = isinstance(error, SimulationCommandInProgress)
            projection = isinstance(error, SimulationProjectionError)
            known = isinstance(
                error,
                (ChangeSetSimulationError, ChangeSetAuthorizationError, SimulationProjectionError),
            )
            typed_error = ToolError(
                code=(
                    "TOOL_AUTHORIZATION_DENIED"
                    if authorization
                    else "TOOL_RETRYABLE_SYSTEM_FAILURE"
                    if stale or in_progress or not known
                    else "TOOL_INVARIANT_FAILED"
                    if invariant
                    else "TOOL_VALIDATION_FAILED"
                ),
                category=(
                    ToolErrorCategory.AUTHORIZATION
                    if authorization
                    else ToolErrorCategory.RETRYABLE_SYSTEM
                    if stale or in_progress or not known
                    else ToolErrorCategory.FATAL_INVARIANT
                    if invariant
                    else ToolErrorCategory.MODEL_REPAIRABLE
                ),
                retryable=stale or in_progress or not known,
                message_for_model=(
                    str(error)
                    if known
                    else "Native ChangeSet simulation failed unexpectedly; reload current revisions and retry."
                ),
                repair_guidance=(
                    "Reload the active GoalState and ChangeSet revisions, revise if needed, then simulate again."
                    if stale or in_progress or projection or not known
                    else None
                ),
            )
            result = {
                "error": "native_changeset_simulation_failed",
                "tool_error": typed_error.model_dump(by_alias=True),
            }
        else:
            try:
                result = spec.validate_v2_output(result)
            except ValidationError as error:
                typed_error = ToolError(
                    code="TOOL_OUTPUT_INVALID",
                    category=ToolErrorCategory.FATAL_INVARIANT,
                    retryable=False,
                    message_for_model="Native ChangeSet simulation returned an invalid canonical result.",
                    details={"validationErrorCount": len(error.errors(include_url=False))},
                )
                result = {
                    "error": "native_changeset_simulation_output_invalid",
                    "tool_error": typed_error.model_dump(by_alias=True),
                }
        return _with_tool_metadata(name=name, arguments=arguments, result=result)
    if name in {"set_goal_transition", "update_goal_state"}:
        if goal_state_updater is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "Durable GoalState is unavailable in this runtime."},
            )
        try:
            result = goal_state_updater(arguments)
        except Exception as error:
            from app.services.agent_kernel.goal_state_reducer import (
                GoalStateDecisionConflict,
                GoalStateReducerError,
                GoalStateTransitionError,
            )
            from app.services.agent_kernel.state_store import (
                GoalStateAuthorizationError,
                GoalStateNotFound,
                GoalStateRevisionConflict,
                GoalStateSessionMismatch,
                GoalStateStoreError,
            )

            if not isinstance(error, (GoalStateReducerError, GoalStateStoreError)):
                raise
            replay_conflict = isinstance(error, GoalStateRevisionConflict) and (
                "idempotency" in str(error).lower()
                or "replay" in str(error).lower()
            )
            stale = isinstance(error, (GoalStateRevisionConflict, GoalStateNotFound))
            authorization = isinstance(
                error,
                (GoalStateAuthorizationError, GoalStateSessionMismatch),
            )
            invariant = isinstance(error, GoalStateTransitionError)
            validation = isinstance(error, GoalStateDecisionConflict)
            error_kind = (
                "goal_state_replay_conflict"
                if replay_conflict
                else "goal_state_stale"
                if stale
                else "goal_state_authorization_denied"
                if authorization
                else "goal_state_invariant_failed"
                if invariant
                else "goal_state_validation_failed"
            )
            typed_error = ToolError(
                code=(
                    "TOOL_AUTHORIZATION_DENIED"
                    if authorization
                    else "TOOL_INVARIANT_FAILED"
                    if invariant
                    else "TOOL_VALIDATION_FAILED"
                ),
                category=(
                    ToolErrorCategory.AUTHORIZATION
                    if authorization
                    else ToolErrorCategory.FATAL_INVARIANT
                    if invariant
                    else ToolErrorCategory.MODEL_REPAIRABLE
                ),
                retryable=stale and not replay_conflict,
                message_for_model=str(error),
                repair_guidance=(
                    "Reload the active goal and submit one fresh transition."
                    if stale and not replay_conflict
                    else None
                ),
                details={
                    "conflictKind": error_kind,
                    "validationFailure": validation,
                },
            )
            result = {
                "error": error_kind,
                "message": str(error),
                "retryable": typed_error.retryable,
                "tool_error": typed_error.model_dump(by_alias=True),
            }
        return _with_tool_metadata(name=name, arguments=arguments, result=result)
    if name in {"list_calendar_blocks", "get_task_schedule"}:
        if calendar_service is None and calendar_reader is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "Calendar context is unavailable in this runtime."},
            )
        calendar_arguments = {
            **arguments,
            **(
                {"include_all_for_tasks": True}
                if name == "get_task_schedule"
                else {}
            ),
        }
        calendar_result = (
            calendar_service.read(
                calendar_arguments,
                invocation=CalendarReadInvocation(
                    nodes=nodes,
                    categories=categories,
                ),
            )
            if calendar_service is not None
            else calendar_reader(calendar_arguments)
            if calendar_reader is not None
            else {"error": "Calendar context is unavailable in this runtime."}
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=calendar_result,
        )
    if name == "query_preferences":
        if preference_reader is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "preference_context_unavailable"},
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=preference_reader(arguments),
        )
    if name == "list_remembered_preferences":
        if preference_inventory_reader is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "preference_inventory_unavailable"},
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=preference_inventory_reader(arguments),
        )
    if name == "analyze_preference_cleanup_candidates":
        if preference_cleanup_analyzer is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "preference_cleanup_analysis_unavailable"},
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=preference_cleanup_analyzer(arguments),
        )
    if name == "read_planner_context":
        if planner_context_reader is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "planner_context_unavailable"},
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=planner_context_reader(),
        )
    if name == "evaluate_schedule_plan":
        if planner_plan_evaluator is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "planner_evaluation_unavailable"},
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=planner_plan_evaluator(arguments),
        )
    if name == "respond_to_planner_decision":
        if planner_decision_responder is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "planner_decision_callback_unavailable"},
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=planner_decision_responder(arguments),
        )
    if name == "handoff_to_planner":
        task_ids = list(dict.fromkeys(
            int(task_id) for task_id in (arguments.get("task_ids") or []) if isinstance(task_id, int)
        ))
        live_ids = {int(node.id) for node in nodes if node.id is not None}
        invalid_ids = [task_id for task_id in task_ids if task_id not in live_ids]
        scope_source = str(arguments.get("scope_source") or "").strip()
        outside_ids = (
            []
            if scope_source in {"explicit_named_task", "calendar_query"}
            else [task_id for task_id in task_ids if task_id not in set(staged_node_ids)]
        )
        if not task_ids or invalid_ids or outside_ids:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={
                    "error": "Planner handoff requires live task IDs from the attached working set.",
                    "invalid_task_ids": invalid_ids,
                    "outside_working_set_ids": outside_ids,
                },
            )
        planner_handoff = {
                "task_ids": task_ids,
                "instruction": str(arguments.get("instruction") or original_user_request or "").strip(),
                "window_start": str(arguments.get("window_start") or "").strip(),
                "window_end": str(arguments.get("window_end") or "").strip(),
                "operation_mode": str(arguments.get("operation_mode") or "create").strip(),
                "scope_source": scope_source,
                "target_scope": (
                    str(arguments.get("target_scope") or "").strip() or None
                ),
                "task_updates": (
                    copy.deepcopy(arguments.get("task_updates"))
                    if isinstance(arguments.get("task_updates"), list)
                    else None
                ),
                "recurrence": (
                    copy.deepcopy(arguments.get("recurrence"))
                    if isinstance(arguments.get("recurrence"), dict)
                    else None
                ),
                "request_constraints": (
                    copy.deepcopy(arguments.get("request_constraints"))
                    if isinstance(arguments.get("request_constraints"), list)
                    else []
                ),
            }
        if scheduling_service is None and planner_executor is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={
                    "planner_handoff": planner_handoff,
                    "model_instruction": (
                        "The Planner command is prepared but has not executed in this runtime. "
                        "Do not treat this as a completed scheduling result."
                    ),
                },
            )
        planner_outcome = (
            schedule_planning_result_payload(
                scheduling_service.plan(
                    SchedulePlanningRequest.model_validate(planner_handoff)
                )
            )
            if scheduling_service is not None
            else planner_executor(copy.deepcopy(planner_handoff))
            if planner_executor is not None
            else {"error": "planner_execution_unavailable"}
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "planner_handoff": planner_handoff,
                **planner_outcome,
            },
        )
    if name == "search_capability_catalog":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=search_capabilities(arguments),
        )
    if name == "load_skill":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=load_skill(arguments),
        )
    if name == "search_tool_catalog":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=_search_tool_catalog(arguments),
        )
    if name == "get_pending_proposal":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "strategist_session_id": strategist_session_id,
                "has_pending_proposal": pending_proposal is not None,
                "pending_proposal": compact_pending_proposal_for_model(pending_proposal),
            },
        )
    if name == "get_recent_kernel_events":
        limit = _bounded_limit(arguments.get("limit"), default=8, maximum=30)
        raw_event_types = arguments.get("event_types")
        event_types = {
            str(item).strip()
            for item in raw_event_types
            if isinstance(item, str) and str(item).strip()
        } if isinstance(raw_event_types, list) else set()
        events = [
            event
            for event in (recent_events or [])
            if isinstance(event, dict) and _event_type_allowed(event, event_types)
        ][-limit:]
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "strategist_session_id": strategist_session_id,
                "count": len(events),
                "recent_events": task_contract_surface(events),
            },
        )
    if name == "get_session_memory":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "strategist_session_id": strategist_session_id,
                "session_memory": task_contract_surface(session_memory or {}),
            },
        )
    if name == "get_context_surfaces":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "strategist_session_id": strategist_session_id,
                "active_category": (
                    {"category_id": active_category.id, "name": active_category.name}
                    if active_category is not None
                    else None
                ),
                "focus_task": (
                    {
                        "task_id": focus_node.id,
                        "title": focus_node.title,
                        "status": focus_node.status,
                        "priority": focus_node.priority,
                        "parent_task_id": focus_node.parent_id,
                    }
                    if focus_node is not None
                    else None
                ),
                "staged_task_ids": staged_node_ids,
                "working_set": task_contract_surface(working_set or []),
                "pending_proposal": compact_pending_proposal_for_model(pending_proposal),
                "session_memory": task_contract_surface(session_memory or {}),
                "recent_events": task_contract_surface(recent_events or []),
            },
        )
    if name == "get_tool_result_by_handle":
        handle = str(arguments.get("tool_result_handle") or "").strip()
        section = str(arguments.get("section") or "summary").strip()
        offset = int(arguments.get("offset") or 0)
        limit = int(arguments.get("limit") or 10)
        stored_result = (
            tool_result_store.get(handle)
            if isinstance(tool_result_store, dict) and handle
            else None
        )
        if (
            stored_result is None
            and handle.startswith("evidence:")
            and evidence_resolver is not None
        ):
            try:
                stored_result = evidence_resolver(handle)
            except (KeyError, PermissionError, ValueError):
                stored_result = None
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "tool_result_handle": handle,
                "found": stored_result is not None,
                "simulation_page": (
                    _simulation_result_page(
                        stored_result,
                        section=section,
                        offset=offset,
                        limit=limit,
                    )
                    if isinstance(stored_result, dict)
                    else None
                ),
                "tool_result": (
                    None
                    if isinstance(stored_result, dict)
                    and stored_result.get("result_kind") == "native_change_set_simulation"
                    else task_contract_surface(stored_result)
                ),
            },
        )
    if name == "submit_task_metadata_plan":
        compiled_result = _canonicalize_compiled_task_proposal(
            _compile_metadata_proposal_tool(arguments),
            proposal_kind="metadata",
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "compiled": compiled_result.get("compiled") is True,
                "semantic_plan_input": {
                    "input_kind": "task",
                    "plan_kind": "metadata",
                    "payload": compiled_result.get("metadata_proposal"),
                },
                "clarification": compiled_result.get("clarification"),
            },
        )
    if name == "submit_task_tree_plan":
        default_category_id = (
            int(category_id)
            if isinstance(
                category_id := _first_present(
                    arguments,
                    "target_category_id",
                    "targetCategoryId",
                ),
                int,
            )
            and not isinstance(category_id, bool)
            else int(active_category.id)
            if active_category is not None and active_category.id is not None
            else None
        )
        arguments, _reused_existing_tasks = _reuse_existing_tasks_in_compound_plan(
            arguments,
            nodes=nodes,
            default_category_id=default_category_id,
        )
        duplicate_matches = _duplicate_existing_task_matches(
            arguments.get("structural_operations"),
            nodes=nodes,
            default_category_id=default_category_id,
        )
        if duplicate_matches:
            exact = duplicate_matches[0]
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={
                    "compiled": False,
                    "semantic_plan_input": {
                        "input_kind": "task",
                        "plan_kind": "branch",
                        "payload": None,
                    },
                    "clarification": None,
                    "error": "duplicate_existing_task",
                    "existing_task_matches": duplicate_matches,
                    "model_instruction": (
                        f'An active task named "{exact["title"]}" already exists as '
                        f'task ID {exact["task_id"]}. Use that exact task for an '
                        "update, breakdown, or schedule request. If the user explicitly "
                        "needs separate work, ask them to distinguish its title before "
                        "submitting another create operation."
                    ),
                },
            )
        compiled_result = _canonicalize_compiled_task_proposal(
            _compile_tree_operation_tool(
                arguments,
                original_user_request=original_user_request,
                session_memory=session_memory,
                today_override=today_override,
                pending_proposal=pending_proposal,
            ),
            proposal_kind="branch",
            nodes=nodes,
            focus_node=focus_node,
            active_category=active_category,
            categories=categories,
        )
        compiled = compiled_result.get("compiled") is True
        coverage_regression = _record_task_plan_coverage(
            arguments,
            tool_result_store=tool_result_store,
            original_user_request=original_user_request,
        )
        if coverage_regression:
            return _tool_argument_error(
                spec,
                [
                    "A successful authoring retry cannot silently shrink the current "
                    f"request's intent coverage: {coverage_regression}."
                ],
                repair_hint=(
                    "Preserve every clause, task operation, and calendar owner from the "
                    "last successfully compiled plan while applying the requested revision."
                ),
            )
        branch_payload = compiled_result.get("branch_proposal")
        if isinstance(branch_payload, dict):
            branch_payload["request_items"] = copy.deepcopy(
                arguments.get("request_items", [])
            )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "compiled": compiled,
                "semantic_plan_input": {
                    "input_kind": "task",
                    "plan_kind": "branch",
                    "payload": branch_payload,
                },
                "clarification": compiled_result.get("clarification"),
                # Semantic submission tools are the model-facing MCP boundary.
                # Preserve bounded validator feedback when compilation fails so
                # providers can repair a portable argument shape instead of
                # mistaking a silent ``compiled: false`` for a submitted plan.
                **(
                    {
                        "error": compiled_result.get("error"),
                        "model_instruction": compiled_result.get(
                            "model_instruction"
                        ),
                        "operation_conservation": compiled_result.get(
                            "operation_conservation"
                        ),
                        "task_patch_validation": compiled_result.get(
                            "task_patch_validation"
                        ),
                    }
                    if not compiled
                    else {}
                ),
            },
        )
    if name == "submit_preference_plan":
        compiled_result = build_preference_memory_proposal_from_tool_arguments(
            arguments,
            original_user_request=original_user_request,
            today_value=today_override,
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "compiled": compiled_result.get("compiled") is True,
                "semantic_plan_input": {
                    "input_kind": "preference",
                    "plan_kind": "preference",
                    "payload": (
                        compiled_result.get(PREFERENCE_RESPONSE_KEY)
                        or compiled_result.get("preferenceMemoryProposal")
                    ),
                },
                "clarification": compiled_result.get("clarification"),
            },
        )
    if name == "submit_preference_management_plan":
        compiled_result = build_preference_management_proposal_from_tool_arguments(
            arguments,
            original_user_request=original_user_request,
            today_value=today_override,
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "compiled": compiled_result.get("compiled") is True,
                "semantic_plan_input": {
                    "input_kind": "preference",
                    "plan_kind": "preference",
                    "payload": (
                        compiled_result.get(PREFERENCE_RESPONSE_KEY)
                        or compiled_result.get("preference_memory_proposal")
                    ),
                },
                "clarification": compiled_result.get("clarification"),
                **(
                    {"error": compiled_result.get("error")}
                    if compiled_result.get("compiled") is not True
                    else {}
                ),
            },
        )
    if name == "submit_preference_cleanup_plan":
        if preference_cleanup_analyzer is None:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={"error": "preference_cleanup_analysis_unavailable"},
            )
        cleanup_scope = str(arguments.get("cleanup_scope") or "all").strip().lower()
        analysis = preference_cleanup_analyzer(
            {"cleanup_scope": cleanup_scope}
        )
        expected_fingerprint = str(
            analysis.get("candidate_set_fingerprint") or ""
        ).strip()
        supplied_fingerprint = str(
            arguments.get("candidate_set_fingerprint") or ""
        ).strip()
        if supplied_fingerprint != expected_fingerprint:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={
                    "compiled": False,
                    "error": "preference_cleanup_candidate_set_stale",
                    "model_instruction": (
                        "Call analyze_preference_cleanup_candidates again and submit "
                        "the returned candidate_set_fingerprint."
                    ),
                    "candidate_set_fingerprint": expected_fingerprint,
                    "complete_batch": False,
                },
            )
        candidates = [
            item
            for item in analysis.get("candidates", [])
            if isinstance(item, dict)
        ]
        if not candidates:
            return _with_tool_metadata(
                name=name,
                arguments=arguments,
                result={
                    "compiled": False,
                    "error": "no_preference_cleanup_candidates",
                    "candidate_count": 0,
                    "candidate_set_fingerprint": expected_fingerprint,
                    "complete_batch": True,
                    "clarification": None,
                },
            )
        action = str(arguments.get("action") or "").strip().lower()
        compiled_result = build_preference_management_proposal_from_tool_arguments(
            {
                "actions": [
                    {
                        "action": action,
                        "preference_id": item.get("id"),
                        "revision": item.get("revision"),
                        "description": item.get("description") or "",
                        "reason_code": item.get("reason_code"),
                        "reason": item.get("reason"),
                        "replacement": (
                            item.get("conflicts_with", [None])[0]
                            if isinstance(item.get("conflicts_with"), list)
                            and item.get("conflicts_with")
                            else None
                        ),
                    }
                    for item in candidates
                ],
                "source_text": arguments.get("source_text"),
                "rationale": arguments.get("rationale")
                or (
                    "Apply the complete server-classified saved-preference cleanup "
                    "batch without including ambiguous preferences."
                ),
                "summary": arguments.get("summary")
                or (
                    f"{'Disable' if action == 'disable' else 'Permanently delete'} "
                    f"all {len(candidates)} high-confidence conflicting or outdated "
                    "saved preferences."
                ),
            },
            original_user_request=original_user_request,
            today_value=today_override,
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={
                "compiled": compiled_result.get("compiled") is True,
                "semantic_plan_input": {
                    "input_kind": "preference",
                    "plan_kind": "preference",
                    "payload": (
                        compiled_result.get(PREFERENCE_RESPONSE_KEY)
                        or compiled_result.get("preference_memory_proposal")
                    ),
                },
                "candidate_count": len(candidates),
                "candidate_set_fingerprint": expected_fingerprint,
                "cleanup_scope": cleanup_scope,
                "complete_batch": True,
                "clarification": compiled_result.get("clarification"),
                **(
                    {"error": compiled_result.get("error")}
                    if compiled_result.get("compiled") is not True
                    else {}
                ),
            },
        )
    if name == "propose_metadata_update":
        compiled_result = _compile_metadata_proposal_tool(arguments)
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=_canonicalize_compiled_task_proposal(
                compiled_result,
                proposal_kind="metadata",
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                categories=categories,
            ),
        )
    if name == "propose_tree_operation":
        compiled_result = _compile_tree_operation_tool(
            arguments,
            original_user_request=original_user_request,
            session_memory=session_memory,
            today_override=today_override,
            pending_proposal=pending_proposal,
        )
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=_canonicalize_compiled_task_proposal(
                compiled_result,
                proposal_kind="branch",
                nodes=nodes,
                focus_node=focus_node,
                active_category=active_category,
                categories=categories,
            ),
        )
    if name == "propose_preference_memory":
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result=build_preference_memory_proposal_from_tool_arguments(
                arguments,
                original_user_request=original_user_request,
                today_value=today_override,
            ),
        )

    result = execute_native_task_read(
        name,
        arguments,
        categories=categories,
        nodes=nodes,
        focus_node=focus_node,
        active_category=active_category,
        staged_node_ids=staged_node_ids,
        working_set=working_set,
        session_memory=session_memory,
        today_override=today_override,
        original_user_request=original_user_request,
    )
    if not isinstance(result, dict):
        return _with_tool_metadata(
            name=name,
            arguments=arguments,
            result={"result": task_contract_surface(result)},
        )
    return _with_tool_metadata(
        name=name,
        arguments=arguments,
        result=task_contract_surface(result),
    )
