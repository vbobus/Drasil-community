"""Evidence-backed inventory and architecture guards for legacy adapter retirement."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .rollout import OperationFamily, RolloutPolicy


@dataclass(frozen=True)
class AdapterSurface:
    adapter_id: str
    family: OperationFamily
    path: str
    responsibility: str


LEGACY_ADAPTER_SURFACES = (
    AdapterSurface("task-patch-runtime", OperationFamily.TASK,
                   "backend/app/services/agent_kernel/task_patch_runtime.py", "legacy_apply"),
    AdapterSurface("task-proposal-actions", OperationFamily.TASK,
                   "backend/app/services/strategist_graph.py", "legacy_review_apply"),
    AdapterSurface("schedule-proposal-actions", OperationFamily.CALENDAR,
                   "backend/app/services/schedule_proposal_actions.py", "legacy_review_apply"),
    AdapterSurface("schedule-patches", OperationFamily.CALENDAR,
                   "backend/app/services/schedule_patches.py", "legacy_apply"),
    AdapterSurface("recurrence-proposal-actions", OperationFamily.RECURRENCE,
                   "backend/app/services/schedule_proposal_actions.py", "legacy_review_apply"),
    AdapterSurface("preference-proposal-actions", OperationFamily.PREFERENCE,
                   "backend/app/services/strategist_graph.py", "legacy_review_apply"),
    AdapterSurface("preference-patches", OperationFamily.PREFERENCE,
                   "backend/app/services/preference_patches.py", "legacy_apply"),
    AdapterSurface("compatibility-change-set", OperationFamily.TASK,
                   "backend/app/services/change_sets.py", "legacy_projection"),
)


def build_retirement_inventory(
    policy: RolloutPolicy,
    *,
    root: Path,
) -> list[dict[str, Any]]:
    _ = policy
    inventory: list[dict[str, Any]] = []
    for surface in LEGACY_ADAPTER_SURFACES:
        present = (root / surface.path).exists()
        inventory.append({
            **asdict(surface),
            "family": surface.family.value,
            "present": present,
            "state": "retired_from_agent_authority",
            "routable": False,
            "gateReasonCode": "single_authority_cutover",
        })
    return inventory


def _function_starts_with_raise(source: str, function_name: str) -> bool:
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    return bool(function is not None and function.body and isinstance(function.body[0], ast.Raise))


def validate_retirement_guards(root: Path) -> list[str]:
    errors: list[str] = []
    for deleted in (
        "backend/app/services/strategist.py",
        "backend/app/services/strategist_compat.py",
    ):
        if (root / deleted).exists():
            errors.append(f"retired compatibility facade returned: {deleted}")

    review_source = (
        root / "backend/app/services/agent_kernel/review_actions.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "schedule_proposal_actions", "preference_patches", "task_patch_runtime",
        "strategist_graph",
    ):
        if forbidden in review_source:
            errors.append(f"native accepted-action boundary imports legacy surface: {forbidden}")

    router_source = (
        root / "backend/app/services/agent_kernel/tier_router.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("re.compile", "re.search", "re.match", "keyword"):
        if forbidden in router_source:
            errors.append(f"tier router contains semantic routing primitive: {forbidden}")

    graph_source = (root / "backend/app/services/strategist_graph.py").read_text(encoding="utf-8")
    if "status_code=410" not in graph_source:
        errors.append("retired Strategist graph no longer has an HTTP 410 sentinel")
    if graph_source.count("sync_response_proposals(") > 1:
        errors.append("live Strategist host can still persist compatibility proposals")

    controller_source = (
        root / "backend/app/services/agent_kernel/controller.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "change_set_shadow",
        "legacy_proposals",
        "RolloutPolicy",
        "selected_authority=\"legacy\"",
    ):
        if forbidden in controller_source:
            errors.append(
                f"review selection contains competing authority: {forbidden}"
            )

    from .mcp_policy import allowed_mcp_tool_names

    strategist_tools = allowed_mcp_tool_names("strategist")
    for forbidden in (
        "propose_metadata_update",
        "propose_tree_operation",
        "propose_preference_memory",
        "draft_change_set",
        "simulate_change_set",
        "update_goal_state",
    ):
        if forbidden in strategist_tools:
            errors.append(f"retired model authoring tool is routable: {forbidden}")

    worker_source = (root / "backend/app/api/ai.py").read_text(encoding="utf-8")
    if "handle_schedule_proposal_action(" in worker_source:
        errors.append("schedule proposal apply handler remains routable")
    endpoint_guard = worker_source.find(
        'str(body.ui_event.kind or "").strip().lower() == "proposal_action"'
    )
    endpoint_work = worker_source.find("principal = require_authenticated_principal()")
    if endpoint_guard < 0 or endpoint_work < 0 or endpoint_guard > endpoint_work:
        errors.append("retired proposal actions do not fail synchronously at the API boundary")

    planner_source = (root / "backend/app/api/planner.py").read_text(encoding="utf-8")
    if not _function_starts_with_raise(planner_source, "propose_schedule_patch"):
        errors.append("standalone Planner proposal authoring route remains routable")

    runtime_source = (
        root / "backend/app/services/agent_kernel/runtime.py"
    ).read_text(encoding="utf-8")
    if "native_shadow_sampling_enabled()" in runtime_source:
        errors.append("native shadow sampling remains routable")
    if "if False and (" in runtime_source:
        errors.append("historical shadow authoring remains embedded in the production runtime")

    app_server_source = (
        root / "backend/app/services/agent_kernel/codex_app_server.py"
    ).read_text(encoding="utf-8")
    if "TERMINAL_SEMANTIC_PLAN_TOOL_NAMES: frozenset[str] = frozenset()" not in app_server_source:
        errors.append("semantic-plan tools can still terminate a compound provider turn")

    for relative_path in (
        "frontend/src/components/planning/StrategistAiCard.tsx",
        "frontend/src/components/selected/SelectedTasksPanel.tsx",
    ):
        compatibility_ui_source = (root / relative_path).read_text(encoding="utf-8")
        if (
            "const RETIRED_COMPATIBILITY_ACTIONS_ENABLED = false;" not in compatibility_ui_source
            or "This proposal has expired" not in compatibility_ui_source
        ):
            errors.append(
                f"retired compatibility card controls are active: {relative_path}"
            )
    return errors
