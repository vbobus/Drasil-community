"""Fact-level evidence assessment shared across agent runtimes.

This module summarizes tool observations. It deliberately does not infer user
intent, choose scope, or rewrite model answers.
"""

from __future__ import annotations

from typing import Any


NON_TASK_EVIDENCE_TOOLS = {
    "search_capability_catalog",
    "load_skill",
    "search_tool_catalog",
    "get_pending_proposal",
    "get_recent_kernel_events",
    "get_session_memory",
    "get_context_surfaces",
    "submit_task_metadata_plan",
    "submit_task_tree_plan",
    "submit_preference_plan",
    "submit_preference_management_plan",
    "submit_preference_cleanup_plan",
    "list_remembered_preferences",
    "analyze_preference_cleanup_candidates",
}


def _is_truncated(coverage: dict[str, Any], result: dict[str, Any]) -> bool:
    values = [
        coverage.get("bounded_result_truncated"),
        coverage.get("truncated"),
        result.get("truncated"),
    ]
    for value in values:
        if value is True:
            return True
        if isinstance(value, dict) and any(child is True for child in value.values()):
            return True
    return False


def assess_tool_evidence(tool_trace: list[dict[str, Any]]) -> dict[str, Any]:
    loaded_capabilities: list[dict[str, Any]] = []
    evidence_observations: list[dict[str, Any]] = []
    for call in tool_trace:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        if name == "load_skill":
            capability = result.get("capability")
            if isinstance(capability, dict) and capability.get("id"):
                loaded_capabilities.append(
                    {
                        "id": capability.get("id"),
                        "version": capability.get("version"),
                        "truncated": bool(result.get("truncated")),
                        "evidence_requirements": result.get("evidence_requirements", {}),
                        "load_duration_ms": call.get("duration_ms"),
                        "failed": bool(result.get("error")),
                    }
                )
            elif result.get("error"):
                loaded_capabilities.append(
                    {
                        "id": (call.get("arguments") or {}).get("capability_id")
                        if isinstance(call.get("arguments"), dict)
                        else None,
                        "version": None,
                        "load_duration_ms": call.get("duration_ms"),
                        "failed": True,
                        "error": result.get("error"),
                    }
                )
        if name in NON_TASK_EVIDENCE_TOOLS or result.get("error"):
            continue
        coverage = result.get("evidence_coverage")
        coverage = coverage if isinstance(coverage, dict) else {}
        kind = str(coverage.get("kind") or result.get("result_kind") or "tool_observation")
        evidence_observations.append(
            {
                "tool": name,
                "kind": kind,
                "scope": coverage.get("scope") or result.get("scope"),
                "complete_scope_scan": bool(coverage.get("complete_scope_scan")),
                "candidate_only": kind in {"candidate_lookup", "search_results", "note_search_results"},
                "truncated": _is_truncated(coverage, result),
            }
        )

    has_current_task_evidence = bool(evidence_observations)
    has_complete_scope_scan = any(item["complete_scope_scan"] for item in evidence_observations)
    candidate_only = bool(evidence_observations) and all(
        item["candidate_only"] for item in evidence_observations
    )
    has_truncated_evidence = any(item["truncated"] for item in evidence_observations)
    skill_checks: list[dict[str, Any]] = []
    for loaded in loaded_capabilities:
        requirements = loaded.get("evidence_requirements")
        requirements = requirements if isinstance(requirements, dict) else {}
        unmet: list[str] = []
        if requirements.get("scope_freshness") == "current_turn" and not has_current_task_evidence:
            unmet.append("current_turn_task_evidence")
        if requirements.get("candidate_search_is_sufficient") is False and candidate_only:
            unmet.append("evidence_beyond_candidate_search")
        if requirements.get("exact_targets_required_for_proposal") and not has_current_task_evidence:
            unmet.append("exact_target_evidence")
        skill_checks.append(
            {
                "capability_id": loaded.get("id"),
                "capability_version": loaded.get("version"),
                "requirements_satisfied": not unmet,
                "unmet_requirements": unmet,
            }
        )
    return {
        "contract_version": "v1",
        "loaded_capabilities": loaded_capabilities,
        "capability_metrics": {
            "load_count": len(loaded_capabilities),
            "failure_count": sum(1 for item in loaded_capabilities if item.get("failed")),
            "total_load_duration_ms": round(
                sum(
                    float(item.get("load_duration_ms") or 0)
                    for item in loaded_capabilities
                ),
                1,
            ),
            "versions": {
                str(item.get("id")): item.get("version")
                for item in loaded_capabilities
                if item.get("id")
            },
        },
        "task_evidence_observation_count": len(evidence_observations),
        "has_current_task_evidence": has_current_task_evidence,
        "has_complete_scope_scan": has_complete_scope_scan,
        "candidate_only": candidate_only,
        "has_truncated_evidence": has_truncated_evidence,
        "observations": evidence_observations,
        "skill_evidence_checks": skill_checks,
        "semantic_decision_deferred_to_model": True,
    }
