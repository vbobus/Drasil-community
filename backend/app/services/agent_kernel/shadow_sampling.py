"""Provider-facing projections for isolated native-v2 shadow sampling."""

from __future__ import annotations

import copy
import os
from typing import Literal

from pydantic import Field

from .contracts import RequestedOperationClause, strict_provider_schema
from .contracts.common import ContractId, ContractModel, Revision
from .contracts.references import Provenance


NATIVE_SHADOW_REASONING_ENV = "AGENT_KERNEL_V2_SHADOW_REASONING_EFFORT"
NATIVE_SHADOW_TIMEOUT_ENV = "AGENT_KERNEL_V2_SHADOW_TIMEOUT_SECONDS"
_SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
DEFAULT_NATIVE_SHADOW_TIMEOUT_SECONDS = 90

TASK_STRUCTURAL_AUTHORING_INSTRUCTIONS = (
    "For structural task intent, preserve the canonical operation kind instead of encoding "
    "structure as metadata: task.move uses taskRefs plus destinationParentRef and/or "
    "destinationWorkspaceRef; task.archive uses taskRefs; task.delete uses taskRefs. "
    "Archiving is never a task.update tags surrogate. Bind every existing task as a canonical "
    "EntityRef with a string entityId. For a bounded cleanup request, operate only on exact "
    "targets justified by the supplied foreground evidence. The organization_map is classification "
    "context, never an automatic mutation list: do not archive or move every mapped task. Treat "
    "cleanup_candidates with suggested_action=review_delete_or_archive as reversible archive "
    "candidates when their signals justify it; review_metadata_or_scope is not automatic archive "
    "authority. Explicit structural imperatives are hard semantic clauses: every task named in a "
    "Move clause must appear only in task.move with the stated destination; every task named in an "
    "Archive clause must appear only in task.archive; and every task named in a Delete clause must "
    "appear only in task.delete. Cleanup analysis may bind those names to exact IDs and validate the "
    "request, but it must never omit, substitute, add, or change the operation kind of an explicitly "
    "named target. Use task.move only for the highest coherent subtree root whose supplied path and "
    "destination_catalog establish a better workspace, so its descendants remain together. Never "
    "omit an independently justified archive or move, never add a merely mapped task, and group "
    "same-kind targets only when their target set and destination meaning stay exact. "
    "A recurring task request requires a separate recurrence.create_series operation in the "
    "same atomic group as task.create; its taskRef must consume the created task tempRef and "
    "it must depend on the task.create operation. Cadence, weekdays, range, timezone, preferred "
    "start time, and duration belong in the canonical recurrence rule, never in TaskFields. "
    "Durable preference creation uses preference.upsert with a stable semantic preferenceKey, "
    "a canonical scalar/string-set/time-window value, and explicit domain, strength, and scope "
    "semantics; do not encode it as task metadata or free-form assumptions. Use preferenceKey "
    "schedule_style for a soft statement about preferred working periods. Preserve a qualitative "
    "statement as a normalized scalar string; do not invent exact time-window boundaries. Scalar "
    "preference wording is semantic data, not assistant prose: copy the user's preference content "
    "exactly after removing only request framing such as 'remember that I prefer' or 'I prefer' "
    "and terminal punctuation. Never paraphrase, shorten, expand, or reorder that content. For "
    "example, 'Remember that I prefer focused work in the morning.' becomes exactly "
    "value:{kind:'scalar',value:'focused work in the morning'}, semantics:{domain:'scheduling',"
    "strength:'soft',scope:'global'}; domain, "
    "strength, and scope never belong inside value. "
    "An exact one-time placement of existing task N uses exactly {kind:'calendar.create_block', "
    "operationId, dependsOn:[], atomicGroupId:null, fulfillsClauseIds:[clauseId], provenance, "
    "taskRef:{kind:'task',entityId:'task:N'}, startAt:'...-07:00', endAt:'...-07:00', "
    "tempRef:null}. The calendar operation has no timezone property: encode the supplied IANA-zone "
    "offset in startAt/endAt. Existing one-digit database ids still require the task: prefix because "
    "every ContractId has at least three characters. Use the identical operation shape in the "
    "requested clause and draft. Do not turn a one-time placement into task metadata or recurrence. "
)


def native_shadow_reasoning_effort(environment: dict[str, str] | None = None) -> str:
    """Use a bounded sampler-specific level without changing foreground selection."""

    source = os.environ if environment is None else environment
    configured = str(source.get(NATIVE_SHADOW_REASONING_ENV) or "medium").strip().lower()
    return configured if configured in _SUPPORTED_REASONING_EFFORTS else "medium"


def native_shadow_timeout_seconds(environment: dict[str, str] | None = None) -> int:
    """Keep isolated sampling separate from foreground and bounded for complex drafts."""

    source = os.environ if environment is None else environment
    raw = source.get(NATIVE_SHADOW_TIMEOUT_ENV)
    try:
        configured = int(raw) if raw is not None else DEFAULT_NATIVE_SHADOW_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        configured = DEFAULT_NATIVE_SHADOW_TIMEOUT_SECONDS
    return max(15, min(300, configured))


class NativeShadowRequestedOperationsPatch(ContractModel):
    """Typed projection of the one semantic patch used by shadow authoring."""

    path: Literal["operations.requested"]
    operation: Literal["set"]
    value: list[RequestedOperationClause] = Field(min_length=1, max_length=128)
    provenance: Provenance
    supersedes_ref: ContractId | None = None


class NativeShadowGoalStateUpdateCommand(ContractModel):
    """Minimal provider input accepted by canonical GoalStateUpdateCommand."""

    command_id: ContractId
    disposition: Literal["correct"]
    goal_id: ContractId
    expected_revision: Revision
    patches: list[NativeShadowRequestedOperationsPatch] = Field(min_length=1, max_length=1)


class NativeShadowDecisionSelectionCommand(ContractModel):
    """Select one immutable native simulation option in the isolated shadow branch."""

    goal_id: ContractId
    expected_goal_revision: Revision
    decision_id: ContractId
    option_id: ContractId


def native_shadow_tool_definitions(
    definitions: list[dict],
    *,
    include_task_graph_query: bool,
    include_conflict_resolution: bool = False,
) -> list[dict]:
    """Return a compact typed surface without changing canonical tool validation."""

    allowed = {"update_goal_state", "draft_change_set"}
    if include_task_graph_query:
        allowed.add("query_task_graph")
    if include_conflict_resolution:
        allowed.add("simulate_change_set")
    projected: list[dict] = []
    for definition in definitions:
        name = str(definition.get("name") or "")
        if name not in allowed:
            continue
        item = copy.deepcopy(definition)
        if name == "update_goal_state":
            item["description"] = (
                "Record exactly one operations.requested patch using typed requested-operation "
                "clauses before drafting the isolated native ChangeSet."
            )
            item["parameters"] = strict_provider_schema(
                NativeShadowGoalStateUpdateCommand
            )
        projected.append(item)
    if not any(item.get("name") == "update_goal_state" for item in projected):
        projected.append(
            {
                "name": "update_goal_state",
                "description": (
                    "Offline compatibility-only shadow command for one "
                    "operations.requested patch."
                ),
                "parameters": strict_provider_schema(
                    NativeShadowGoalStateUpdateCommand
                ),
                "strict": True,
            }
        )
    if include_conflict_resolution:
        projected.append(
            {
                "name": "select_change_set_decision",
                "description": (
                    "Select one exact open ChangeSet decision option after simulation. "
                    "The server applies only the immutable stored delta and never mutates "
                    "product-domain state."
                ),
                "parameters": strict_provider_schema(
                    NativeShadowDecisionSelectionCommand
                ),
                "strict": True,
            }
        )
    return projected
