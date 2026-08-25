"""Authoritative, user-scoped remembered-preference administration reads."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlmodel import Session, select

from app.models.preference_memory import SchedulingConstraint, UserPreference
from app.services.preference_values import (
    decode_preference_value,
    preference_semantic_payload,
    preference_semantic_similarity,
    preference_subject,
)


PREFERENCE_CLEANUP_SCOPES = {
    "contradictions",
    "overlaps",
    "all",
}
CONTRADICTION_REASON_CODES = {
    "superseded_by_newer_value",
    "contradicts_conditional_gap_rule",
    "lower_provenance_conflict",
}
OVERLAP_REASON_CODES = {
    "duplicate_older_copy",
    "semantic_duplicate_older_copy",
    "redundant_scoped_copy",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _integer_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _preference_provenance_rank(
    item: UserPreference,
    value: dict[str, Any],
) -> int:
    """Rank direct user evidence above unlinked legacy/test-shaped rows."""

    if str(item.source or "").strip().lower() == "test_artifact":
        return 0
    if str(value.get("sourceText") or "").strip():
        return 3
    if str(value.get("extractionKind") or "").strip() == "agent_tool":
        return 2
    return 1


def _canonical_preference_order(
    item: UserPreference,
    values: dict[str, dict[str, Any]],
) -> tuple[int, Any, Any, str]:
    return (
        _preference_provenance_rank(item, values[item.id]),
        item.updated_at,
        item.created_at,
        item.id,
    )


def _preference_subject(item: UserPreference, value: dict[str, Any]) -> str | None:
    semantic_subject = preference_subject(
        item.preference_type,
        value,
        description=item.description or "",
        domain=item.domain,
    )
    if semantic_subject == "wake_time":
        return semantic_subject
    if semantic_subject == "inter_task_gap":
        return semantic_subject
    if semantic_subject == "earliest_schedule_time":
        return "earliest_start"
    if semantic_subject == "latest_schedule_time":
        return "latest_end"
    if semantic_subject == "deep_work_window":
        return semantic_subject

    text = " ".join(
        [item.description or "", str(value.get("preferenceText") or "")]
    ).casefold()
    if re.search(r"\bwake(?:\s|-)?up\b|\bwake\s+time\b", text):
        return "wake_time"
    if item.preference_type == "inter_task_gap" or any(
        key in value
        for key in (
            "interTaskGapMinutes",
            "preferredInterTaskGapMinutes",
            "gapMinutes",
        )
    ):
        return "inter_task_gap"
    if item.preference_type == "availability":
        rule = str(value.get("rule") or "").casefold()
        if (
            "before" in rule
            or value.get("preferredEarliestStartTime") is not None
            or (
                value.get("startTime") == "00:00"
                and value.get("endTime") not in {None, "23:59"}
            )
        ):
            return "earliest_start"
        if "after" in rule or value.get("startTime") not in {None, "00:00"}:
            return "latest_end"
    if item.domain == "deep_work" or "deep work" in text:
        return "deep_work_window"
    return None


def _semantic_value(
    item: UserPreference,
    value: dict[str, Any],
    *,
    subject: str | None,
) -> dict[str, Any]:
    semantic = preference_semantic_payload(
        item.preference_type,
        value,
        description=item.description or "",
        domain=item.domain,
    )
    if subject == "wake_time":
        return {"time": semantic.get("time")}
    if subject == "inter_task_gap":
        return {
            "gap_minutes": _integer_or_zero(semantic.get("gap_minutes")),
            "minimum_task_duration_minutes": semantic.get(
                "minimum_task_duration_minutes"
            ),
            "maximum_task_duration_minutes": semantic.get(
                "maximum_task_duration_minutes"
            ),
            "shorter_task_gap_minutes": semantic.get(
                "shorter_task_gap_minutes"
            ),
        }
    if subject == "earliest_start":
        return {"time": semantic.get("time")}
    if subject == "latest_end":
        return {"time": semantic.get("time")}
    return semantic


def find_semantic_duplicate_matches(
    session: Session,
    *,
    user_id: str,
    preference_type: str,
    domain: str,
    scope_type: str,
    scope_id: str | None,
    description: str,
    value: dict[str, Any],
    exclude_preference_id: str | None = None,
    minimum_score: float = 0.9,
) -> list[dict[str, Any]]:
    """Find high-confidence active paraphrases in the same authority scope."""

    candidates = session.exec(
        select(UserPreference)
        .where(UserPreference.user_id == user_id)
        .where(UserPreference.active == True)  # noqa: E712
        .where(UserPreference.scope_type == scope_type)
        .order_by(UserPreference.updated_at.desc(), UserPreference.id)
    ).all()
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.id == exclude_preference_id or candidate.scope_id != scope_id:
            continue
        score = preference_semantic_similarity(
            preference_type,
            value,
            left_description=description,
            left_domain=domain,
            right_key=candidate.preference_type,
            right_value=decode_preference_value(candidate.value_json),
            right_description=candidate.description or "",
            right_domain=candidate.domain,
        )
        if score < minimum_score:
            continue
        matches.append(
            {
                "id": candidate.id,
                "entity_ref": f"preference:{candidate.id}",
                "revision": candidate.revision,
                "description": candidate.description,
                "score": round(score, 4),
            }
        )
    return matches


def _candidate(
    item: UserPreference,
    *,
    reason_code: str,
    reason: str,
    conflicts_with: list[UserPreference],
) -> dict[str, Any]:
    return {
        "id": item.id,
        "entity_ref": f"preference:{item.id}",
        "revision": item.revision,
        "description": item.description,
        "preference_type": item.preference_type,
        "scope_type": item.scope_type,
        "scope_id": item.scope_id,
        "recommended_action": "disable",
        "reason_code": reason_code,
        "reason": reason,
        "conflicts_with": [
            {
                "id": other.id,
                "entity_ref": f"preference:{other.id}",
                "revision": other.revision,
                "description": other.description,
            }
            for other in conflicts_with
        ],
    }


def analyze_remembered_preference_cleanup(
    session: Session,
    *,
    user_id: str,
    cleanup_scope: str = "all",
) -> dict[str, Any]:
    """Classify the complete active preference inventory for safe batch cleanup."""

    normalized_scope = cleanup_scope.strip().lower()
    if normalized_scope not in PREFERENCE_CLEANUP_SCOPES:
        normalized_scope = "all"
    active = list(
        session.exec(
            select(UserPreference)
            .where(UserPreference.user_id == user_id)
            .where(UserPreference.active == True)  # noqa: E712
            .order_by(UserPreference.updated_at.desc(), UserPreference.id)
        ).all()
    )
    values = {item.id: decode_preference_value(item.value_json) for item in active}
    subjects = {
        item.id: _preference_subject(item, values[item.id]) for item in active
    }
    semantic_values = {
        item.id: _semantic_value(
            item,
            values[item.id],
            subject=subjects[item.id],
        )
        for item in active
    }
    candidate_by_id: dict[str, dict[str, Any]] = {}
    kept_ids: set[str] = {item.id for item in active}

    # Compare canonical meaning, not raw wording. Keep the strongest/newest
    # record and identify older paraphrases without crossing scope boundaries.
    ordered_active = sorted(
        active,
        key=lambda item: _canonical_preference_order(item, values),
        reverse=True,
    )
    duplicate_keepers: list[UserPreference] = []
    for item in ordered_active:
        duplicate_match: tuple[UserPreference, float] | None = None
        for keeper in duplicate_keepers:
            if (
                item.scope_type != keeper.scope_type
                or item.scope_id != keeper.scope_id
            ):
                continue
            score = preference_semantic_similarity(
                item.preference_type,
                values[item.id],
                left_description=item.description or "",
                left_domain=item.domain,
                right_key=keeper.preference_type,
                right_value=values[keeper.id],
                right_description=keeper.description or "",
                right_domain=keeper.domain,
            )
            if score >= 0.84:
                duplicate_match = (keeper, score)
                break
        if duplicate_match is None:
            duplicate_keepers.append(item)
            continue
        keeper, score = duplicate_match
        exact = semantic_values[item.id] == semantic_values[keeper.id]
        candidate_by_id[item.id] = _candidate(
            item,
            reason_code=(
                "duplicate_older_copy"
                if exact
                else "semantic_duplicate_older_copy"
            ),
            reason=(
                "An equivalent preference exists in the same scope and is newer."
                if exact
                else (
                    "A newer preference expresses the same meaning in different wording "
                    f"(semantic match {score:.0%})."
                )
            ),
            conflicts_with=[keeper],
        )
        kept_ids.discard(item.id)

    # Mutually exclusive values for the same semantic subject and exact scope
    # are supersessions. This covers wake-time and availability updates without
    # treating unrelated schedule-style preferences as conflicts.
    subject_groups: dict[
        tuple[str, str, str | None],
        list[UserPreference],
    ] = {}
    for item in active:
        subject = subjects[item.id]
        if subject in {"wake_time", "earliest_start", "latest_end"}:
            subject_groups.setdefault(
                (subject, item.scope_type, item.scope_id),
                [],
            ).append(item)
    for group in subject_groups.values():
        distinct = {
            _canonical_json(semantic_values[item.id]) for item in group
        }
        if len(distinct) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda item: _canonical_preference_order(item, values),
            reverse=True,
        )
        keeper = ordered[0]
        for older in ordered[1:]:
            older_rank = _preference_provenance_rank(older, values[older.id])
            keeper_rank = _preference_provenance_rank(
                keeper,
                values[keeper.id],
            )
            candidate_by_id[older.id] = _candidate(
                older,
                reason_code=(
                    "lower_provenance_conflict"
                    if older_rank < keeper_rank
                    else "superseded_by_newer_value"
                ),
                reason=(
                    "This record conflicts with a directly sourced user preference but "
                    "does not retain equivalent user-source provenance."
                    if older_rank < keeper_rank
                    else "A newer preference sets a different value for the same subject and scope."
                ),
                conflicts_with=[keeper],
            )
            kept_ids.discard(older.id)

    # A global duration-aware gap rule is a refinement: it permits short tasks
    # to remain back-to-back. Broad gap rules contradict that behavior, while
    # scoped copies of the same conditional rule add no scheduling information.
    conditional_global_gaps = [
        item
        for item in active
        if subjects[item.id] == "inter_task_gap"
        and item.scope_type == "global"
        and semantic_values[item.id].get("minimum_task_duration_minutes") is not None
    ]
    if conditional_global_gaps:
        canonical_gap = sorted(
            conditional_global_gaps,
            key=lambda item: _canonical_preference_order(item, values),
            reverse=True,
        )[0]
        canonical_policy = semantic_values[canonical_gap.id]
        for item in active:
            if item.id == canonical_gap.id or subjects[item.id] != "inter_task_gap":
                continue
            policy = semantic_values[item.id]
            if policy == canonical_policy:
                candidate_by_id[item.id] = _candidate(
                    item,
                    reason_code="redundant_scoped_copy",
                    reason="The active global conditional gap preference already covers this scope.",
                    conflicts_with=[canonical_gap],
                )
                kept_ids.discard(item.id)
            elif (
                _integer_or_zero(policy.get("gap_minutes")) > 0
                and policy.get("minimum_task_duration_minutes") is None
            ):
                candidate_by_id[item.id] = _candidate(
                    item,
                    reason_code="contradicts_conditional_gap_rule",
                    reason=(
                        "This blanket gap also affects short tasks, while the active "
                        "conditional rule explicitly permits short tasks back-to-back."
                    ),
                    conflicts_with=[canonical_gap],
                )
                kept_ids.discard(item.id)

    ambiguous: list[dict[str, Any]] = []
    deep_work_items = [
        item for item in active if subjects[item.id] == "deep_work_window"
    ]
    hard_windows = [
        item
        for item in active
        if item.strength == "hard"
        and subjects[item.id] in {"earliest_start", "latest_end"}
    ]
    for item in deep_work_items:
        if hard_windows:
            ambiguous.append(
                {
                    "id": item.id,
                    "entity_ref": f"preference:{item.id}",
                    "revision": item.revision,
                    "description": item.description,
                    "reason_code": "partially_constrained_preferred_window",
                    "reason": (
                        "Some preferred deep-work windows may be unavailable under hard "
                        "start/end rules, but the preference can still be satisfied at "
                        "other times and is not clearly superseded."
                    ),
                    "related_preferences": [
                        {
                            "id": other.id,
                            "entity_ref": f"preference:{other.id}",
                            "revision": other.revision,
                            "description": other.description,
                        }
                        for other in hard_windows
                    ],
                }
            )

    all_candidates = sorted(
        candidate_by_id.values(),
        key=lambda item: (
            item["reason_code"],
            item["description"] or "",
            item["id"],
        ),
    )
    reason_codes = (
        CONTRADICTION_REASON_CODES
        if normalized_scope == "contradictions"
        else OVERLAP_REASON_CODES
        if normalized_scope == "overlaps"
        else CONTRADICTION_REASON_CODES | OVERLAP_REASON_CODES
    )
    candidates = [
        item for item in all_candidates if item["reason_code"] in reason_codes
    ]
    other_cleanup_candidates = [
        item for item in all_candidates if item["reason_code"] not in reason_codes
    ]
    fingerprint_payload = {
        "cleanup_scope": normalized_scope,
        "candidates": [
        {
            "id": item["id"],
            "revision": item["revision"],
            "recommended_action": item["recommended_action"],
            "reason_code": item["reason_code"],
        }
        for item in candidates
        ],
    }
    fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    return {
        "candidates": candidates,
        "candidate_count": len(candidates),
        "candidate_set_fingerprint": f"sha256:{fingerprint}",
        "cleanup_scope": normalized_scope,
        "other_cleanup_candidates": other_cleanup_candidates,
        "other_cleanup_candidate_count": len(other_cleanup_candidates),
        "ambiguous": ambiguous,
        "ambiguous_count": len(ambiguous),
        "kept_preferences": [
            {
                "id": item.id,
                "entity_ref": f"preference:{item.id}",
                "revision": item.revision,
                "description": item.description,
                "reason": "No high-confidence supersession, contradiction, or redundancy was found.",
            }
            for item in active
            if item.id in kept_ids
        ],
        "active_inventory_count": len(active),
        "complete": True,
        "classification_version": "preference-cleanup-v2",
    }


def list_remembered_preference_inventory(
    session: Session,
    *,
    user_id: str,
    include_inactive: bool = True,
    query: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Return exact IDs and revisions needed for safe preference administration."""

    bounded_limit = max(1, min(int(limit), 100))
    preferences = list(
        session.exec(
            select(UserPreference)
            .where(UserPreference.user_id == user_id)
            .order_by(
                UserPreference.active.desc(),
                UserPreference.updated_at.desc(),
                UserPreference.id,
            )
        ).all()
    )
    if not include_inactive:
        preferences = [item for item in preferences if item.active]
    normalized_query = query.strip().casefold()
    if normalized_query:
        preferences = [
            item
            for item in preferences
            if normalized_query
            in " ".join(
                [
                    item.description or "",
                    item.preference_type,
                    item.domain,
                    item.scope_type,
                    item.scope_id or "",
                    item.value_json or "",
                ]
            ).casefold()
        ]
    preferences = preferences[:bounded_limit]
    preference_ids = [item.id for item in preferences]
    constraints_by_preference_id: dict[str, list[SchedulingConstraint]] = {}
    if preference_ids:
        constraints = session.exec(
            select(SchedulingConstraint)
            .where(SchedulingConstraint.user_id == user_id)
            .where(SchedulingConstraint.source_preference_id.in_(preference_ids))
            .order_by(SchedulingConstraint.created_at, SchedulingConstraint.id)
        ).all()
        for constraint in constraints:
            if constraint.source_preference_id:
                constraints_by_preference_id.setdefault(
                    constraint.source_preference_id,
                    [],
                ).append(constraint)

    active_count = sum(1 for item in preferences if item.active)
    return {
        "preferences": [
            {
                "id": item.id,
                "entity_ref": f"preference:{item.id}",
                "revision": item.revision,
                "active": item.active,
                "description": item.description,
                "domain": item.domain,
                "preference_type": item.preference_type,
                "scope_type": item.scope_type,
                "scope_id": item.scope_id,
                "strength": item.strength,
                "source": item.source,
                "confidence": item.confidence,
                "value": decode_preference_value(item.value_json),
                "constraints": [
                    {
                        "id": constraint.id,
                        "entity_ref": f"scheduling_constraint:{constraint.id}",
                        "revision": constraint.revision,
                        "active": constraint.active,
                        "constraint_type": constraint.constraint_type,
                        "target_type": constraint.target_type,
                        "target_id": constraint.target_id,
                        "hard": constraint.hard,
                        "description": constraint.description,
                        "value": decode_preference_value(constraint.value_json),
                    }
                    for constraint in constraints_by_preference_id.get(item.id, [])
                ],
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in preferences
        ],
        "count": len(preferences),
        "active_count": active_count,
        "inactive_count": len(preferences) - active_count,
        "include_inactive": include_inactive,
        "query": query.strip(),
    }
