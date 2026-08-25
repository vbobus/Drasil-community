"""Durable agent-run plan helpers for bounded Strategist workflows.

The plan is intentionally plain JSON so it can live in the existing kernel
event log. That gives us checkpointing without introducing a second persistence
surface before the workflow shape has settled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _string_list(value: Any, *, limit: int = 4) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _risk_level(item: dict[str, Any], *, unit_kind: str) -> str:
    if unit_kind in {"branch_restructure", "workload_overdue"}:
        return "high"
    if unit_kind == "workload_due_soon":
        return "medium"
    score = item.get("score")
    if isinstance(score, (float, int)) and float(score) >= 0.8:
        return "medium"
    return "low"


def _task_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("task_title") or "").strip()


def _task_id(item: dict[str, Any]) -> int | None:
    return _as_int(item.get("task_id", item.get("id")))


def _task_evidence(item: dict[str, Any], *, extras: list[str] | None = None) -> list[str]:
    evidence: list[str] = []
    for key in ("deadline", "priority_label", "estimate"):
        value = str(item.get(key) or "").strip()
        if value and value.lower() not in {"no deadline", "no estimate"}:
            evidence.append(value)
    if extras:
        evidence.extend(extras)
    return evidence[:4]


def _base_plan(
    *,
    kind: str,
    objective: str,
    scope: str,
    work_units: list[dict[str, Any]],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    current_work_unit_id = (
        str(work_units[0]["work_unit_id"])
        if work_units and isinstance(work_units[0].get("work_unit_id"), str)
        else None
    )
    return {
        "plan_id": str(uuid4()),
        "kind": kind,
        "status": "awaiting_work_unit_selection" if work_units else "complete",
        "objective": objective,
        "scope": scope,
        "strategy": "reconnaissance_then_bounded_patch",
        "current_work_unit_id": current_work_unit_id,
        "work_unit_count": len(work_units),
        "work_units": work_units,
        "checkpoint": checkpoint,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _work_unit_from_cleanup_candidate(
    item: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any] | None:
    task_id = _as_int(item.get("task_id"))
    title = str(item.get("title") or "").strip()
    if task_id is None or not title:
        return None
    signals = _string_list(item.get("signals"))
    return {
        "work_unit_id": f"cleanup-{task_id}",
        "kind": "cleanup_candidate",
        "status": "pending",
        "rank": index + 1,
        "title": f"Review cleanup for {title}",
        "target_task_id": task_id,
        "target_task_title": title,
        "path": str(item.get("path") or "").strip() or title,
        "risk": _risk_level(item, unit_kind="cleanup_candidate"),
        "evidence": signals,
        "suggested_action": str(item.get("suggested_action") or "review_cleanup").strip(),
        "expected_output": "one reviewable delete/archive/metadata proposal or a no-op explanation",
        "follow_up_prompt": f"Turn this cleanup candidate into a reviewable proposal: {title}",
    }


def _work_unit_from_messy_branch(
    item: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any] | None:
    task_id = _as_int(item.get("task_id"))
    title = str(item.get("title") or "").strip()
    if task_id is None or not title:
        return None
    signals = _string_list(item.get("signals"))
    child_sample = _string_list(item.get("child_sample"), limit=5)
    return {
        "work_unit_id": f"restructure-{task_id}",
        "kind": "branch_restructure",
        "status": "pending",
        "rank": index + 1,
        "title": f"Restructure {title}",
        "target_task_id": task_id,
        "target_task_title": title,
        "path": str(item.get("path") or "").strip() or title,
        "risk": _risk_level(item, unit_kind="branch_restructure"),
        "evidence": signals,
        "child_sample": child_sample,
        "suggested_action": "inspect_branch_for_restructure",
        "expected_output": "one focused branch restructure proposal",
        "follow_up_prompt": f"Inspect and propose a focused restructure for {title}",
    }


def build_cleanup_agent_run_plan(
    *,
    objective: str,
    analysis_result: dict[str, Any],
    max_work_units: int = 7,
) -> dict[str, Any]:
    """Create a bounded plan from cleanup analysis.

    This is the "map first, patch one slice next" checkpoint that keeps broad
    cleanup prompts from becoming one large model synthesis step.
    """
    cleanup_candidates = [
        item for item in analysis_result.get("cleanup_candidates", []) if isinstance(item, dict)
    ]
    messy_branches = [
        item for item in analysis_result.get("messy_branches", []) if isinstance(item, dict)
    ]
    work_units: list[dict[str, Any]] = []
    for index, item in enumerate(cleanup_candidates):
        unit = _work_unit_from_cleanup_candidate(item, index=index)
        if unit is not None:
            work_units.append(unit)
        if len(work_units) >= max_work_units:
            break
    if len(work_units) < max_work_units:
        for index, item in enumerate(messy_branches):
            unit = _work_unit_from_messy_branch(item, index=index)
            if unit is not None:
                work_units.append(unit)
            if len(work_units) >= max_work_units:
                break

    return _base_plan(
        kind="tree_cleanup",
        objective=objective,
        scope=str(analysis_result.get("scope") or "task tree").strip(),
        work_units=work_units,
        checkpoint={
            "phase": "reconnaissance",
            "next_action": (
                "select_one_work_unit"
                if work_units
                else "no_cleanup_needed"
            ),
            "cleanup_candidate_count": analysis_result.get("cleanup_candidate_count"),
            "messy_branch_count": analysis_result.get("messy_branch_count"),
            "active_task_count": analysis_result.get("active_task_count"),
        },
    )


def _work_unit_from_workload_item(
    item: dict[str, Any],
    *,
    bucket: str,
    index: int,
) -> dict[str, Any] | None:
    task_id = _task_id(item)
    title = _task_title(item)
    if task_id is None or not title:
        return None
    if bucket == "overdue":
        unit_title = f"Resolve overdue task: {title}"
        follow_up = f"Turn this overdue workload item into one focused next-step proposal: {title}"
        expected_output = "one next-step recommendation or metadata proposal to unblock the overdue task"
        unit_kind = "workload_overdue"
        extras = ["overdue"]
    elif bucket == "due_soon":
        unit_title = f"Plan due-soon task: {title}"
        follow_up = f"Draft a focused plan for this due-soon task: {title}"
        expected_output = "one focused plan or metadata proposal for the due-soon task"
        unit_kind = "workload_due_soon"
        extras = ["due soon"]
    else:
        unit_title = f"Review stale task: {title}"
        follow_up = f"Decide whether to update, archive, or ignore this stale task: {title}"
        expected_output = "one reviewable cleanup/metadata proposal or a no-op explanation"
        unit_kind = "workload_stale"
        extras = ["stale"]
    return {
        "work_unit_id": f"{bucket}-{task_id}",
        "kind": unit_kind,
        "status": "pending",
        "rank": index + 1,
        "title": unit_title,
        "target_task_id": task_id,
        "target_task_title": title,
        "path": title,
        "risk": _risk_level(item, unit_kind=unit_kind),
        "evidence": _task_evidence(item, extras=extras),
        "suggested_action": bucket,
        "expected_output": expected_output,
        "follow_up_prompt": follow_up,
    }


def build_workload_agent_run_plan(
    *,
    objective: str,
    workload_result: dict[str, Any],
    max_work_units: int = 7,
) -> dict[str, Any]:
    work_units: list[dict[str, Any]] = []
    for bucket in ("overdue", "due_soon", "stale"):
        raw_items = workload_result.get(bucket)
        if not isinstance(raw_items, list):
            continue
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            unit = _work_unit_from_workload_item(item, bucket=bucket, index=index)
            if unit is not None:
                work_units.append(unit)
            if len(work_units) >= max_work_units:
                break
        if len(work_units) >= max_work_units:
            break
    return _base_plan(
        kind="workload_triage",
        objective=objective,
        scope=str(workload_result.get("scope") or "workload").strip(),
        work_units=work_units,
        checkpoint={
            "phase": "reconnaissance",
            "next_action": "select_one_work_unit" if work_units else "no_workload_pressure_found",
            "overdue_count": len(workload_result.get("overdue") or []),
            "due_soon_count": len(workload_result.get("due_soon") or []),
            "stale_count": len(workload_result.get("stale") or []),
            "active_task_count": workload_result.get("active_count"),
        },
    )


def _branch_children(branch_result: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    branch = branch_result.get("branch")
    if not isinstance(branch, dict):
        return {}, []
    children = [item for item in branch.get("children", []) if isinstance(item, dict)]
    return branch, children


def _work_unit_from_branch_child(
    item: dict[str, Any],
    *,
    index: int,
    plan_kind: str,
) -> dict[str, Any] | None:
    task_id = _task_id(item)
    title = _task_title(item)
    if task_id is None or not title:
        return None
    action = "Restructure" if plan_kind == "branch_restructure" else "Break down"
    unit_kind = "branch_restructure" if plan_kind == "branch_restructure" else "branch_breakdown"
    return {
        "work_unit_id": f"{unit_kind}-{task_id}",
        "kind": unit_kind,
        "status": "pending",
        "rank": index + 1,
        "title": f"{action} {title}",
        "target_task_id": task_id,
        "target_task_title": title,
        "path": title,
        "risk": _risk_level(item, unit_kind=unit_kind),
        "evidence": _task_evidence(item),
        "suggested_action": unit_kind,
        "expected_output": (
            "one focused branch restructure proposal"
            if plan_kind == "branch_restructure"
            else "one bounded child-task breakdown proposal"
        ),
        "follow_up_prompt": (
            f"Inspect and propose a focused restructure for {title}"
            if plan_kind == "branch_restructure"
            else f"Break down this branch slice into concrete subtasks: {title}"
        ),
    }


def build_branch_agent_run_plan(
    *,
    objective: str,
    branch_result: dict[str, Any],
    plan_kind: str = "branch_breakdown",
    max_work_units: int = 7,
) -> dict[str, Any]:
    branch, children = _branch_children(branch_result)
    work_units: list[dict[str, Any]] = []
    for index, child in enumerate(children):
        unit = _work_unit_from_branch_child(child, index=index, plan_kind=plan_kind)
        if unit is not None:
            work_units.append(unit)
        if len(work_units) >= max_work_units:
            break
    branch_title = str(branch.get("title") or "branch").strip()
    return _base_plan(
        kind=plan_kind,
        objective=objective,
        scope=branch_title,
        work_units=work_units,
        checkpoint={
            "phase": "reconnaissance",
            "next_action": "select_one_work_unit" if work_units else "draft_single_branch_proposal",
            "branch_task_id": branch.get("task_id"),
            "branch_title": branch_title,
            "child_count": len(children),
        },
    )


def compact_agent_run_plan_for_model(
    plan: dict[str, Any] | None,
    *,
    max_work_units: int = 5,
) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    work_units = [
        unit for unit in plan.get("work_units", []) if isinstance(unit, dict)
    ][:max_work_units]
    return {
        "plan_id": plan.get("plan_id"),
        "kind": plan.get("kind"),
        "status": plan.get("status"),
        "objective": plan.get("objective"),
        "scope": plan.get("scope"),
        "strategy": plan.get("strategy"),
        "current_work_unit_id": plan.get("current_work_unit_id"),
        "work_unit_count": plan.get("work_unit_count"),
        "work_units": [
            {
                "work_unit_id": unit.get("work_unit_id"),
                "kind": unit.get("kind"),
                "status": unit.get("status"),
                "rank": unit.get("rank"),
                "title": unit.get("title"),
                "target_task_id": unit.get("target_task_id"),
                "target_task_title": unit.get("target_task_title"),
                "path": unit.get("path"),
                "risk": unit.get("risk"),
                "evidence": unit.get("evidence", []),
                "expected_output": unit.get("expected_output"),
                "follow_up_prompt": unit.get("follow_up_prompt"),
            }
            for unit in work_units
        ],
    }


def clarification_from_agent_run_plan(plan: dict[str, Any]) -> dict[str, Any]:
    work_units = [unit for unit in plan.get("work_units", []) if isinstance(unit, dict)]
    choices: list[dict[str, Any]] = []
    plan_kind = str(plan.get("kind") or "").strip()
    for unit in work_units[:7]:
        label = str(unit.get("title") or "").strip()
        if not label:
            continue
        target_task_id = _as_int(unit.get("target_task_id"))
        evidence = _string_list(unit.get("evidence"), limit=3)
        path = str(unit.get("path") or "").strip()
        description_parts = [part for part in (path, ", ".join(evidence)) if part]
        choices.append(
            {
                "kind": f"{plan_kind or 'agent'}_work_unit",
                "label": label,
                "value": str(unit.get("work_unit_id") or target_task_id or label),
                "target_node_id": target_task_id,
                "description": " | ".join(description_parts) or None,
                "follow_up_prompt": str(unit.get("follow_up_prompt") or label),
            }
        )
    message_by_kind = {
        "tree_cleanup": "Which cleanup work unit should I turn into a reviewable proposal first?",
        "workload_triage": "Which workload item should I tackle first?",
        "branch_breakdown": "Which branch slice should I break down first?",
        "branch_restructure": "Which branch slice should I restructure first?",
    }
    return {
        "message": message_by_kind.get(
            plan_kind,
            "Which work unit should I tackle first?",
        ),
        "entity_choices": choices,
        "scoped_follow_up_prompts": [
            choice["follow_up_prompt"]
            for choice in choices
            if isinstance(choice.get("follow_up_prompt"), str)
        ],
    }
