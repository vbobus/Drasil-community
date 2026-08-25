"""Classify proposal-boundary failures without exposing validator internals."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def _messages(failures: list[dict[str, Any]]) -> list[str]:
    return [
        str(error).strip()
        for failure in failures
        for error in failure.get("errors", [])
        if isinstance(error, str) and error.strip()
    ]


def _recurring_conflicts(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for failure in failures:
        proposal = failure.get("proposal")
        if not isinstance(proposal, dict):
            continue
        plans = [
            plan
            for key in ("recurring_schedule_plan", "created_task_schedule_plan")
            for plan in [proposal.get(key)]
            if isinstance(plan, dict)
        ]
        raw_conflicts = [
            item
            for plan in plans
            for key in ("priority_conflicts", "preserved_blockers")
            for item in plan.get(key, [])
            if isinstance(item, dict)
        ]
        for conflict in raw_conflicts:
            blockers = [
                item
                for item in conflict.get("blockers", [])
                if isinstance(item, dict)
            ]
            for blocker in blockers:
                flattened = {
                    "date": conflict.get("date"),
                    "incomingTaskTitle": conflict.get("incomingTaskTitle"),
                    "incomingPriority": conflict.get("incomingPriority"),
                    "requestedStartAt": conflict.get("requestedStartAt"),
                    "requestedEndAt": conflict.get("requestedEndAt"),
                    "blockerTaskTitle": blocker.get("taskTitle"),
                    "blockerPriority": blocker.get("priority"),
                    "blockerStartAt": blocker.get("startAt"),
                    "blockerEndAt": blocker.get("endAt"),
                    "fixedReason": blocker.get("fixedReason"),
                    "decisionReason": conflict.get("decisionReason"),
                    "recommendedAction": conflict.get("recommendedAction"),
                    "conflictKind": conflict.get("conflictKind"),
                    "gapMinutes": conflict.get("gapMinutes"),
                    "availableGapMinutes": conflict.get("availableGapMinutes"),
                }
                identity = (
                    flattened["date"],
                    flattened["incomingTaskTitle"],
                    flattened["requestedStartAt"],
                    flattened["blockerTaskTitle"],
                    flattened["blockerStartAt"],
                )
                if identity in seen:
                    continue
                seen.add(identity)
                conflicts.append(flattened)
    return conflicts


def _clock(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return "the requested time"
    return parsed.strftime("%I:%M %p").lstrip("0")


def _calendar_day(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value or "the affected date")
    return parsed.strftime("%A, %B %d").replace(" 0", " ")


def classify_handoff_failures(failures: list[dict[str, Any]]) -> dict[str, Any]:
    messages = _messages(failures)
    lowered = "\n".join(messages).lower()
    recurring_conflicts = _recurring_conflicts(failures)

    if recurring_conflicts:
        incoming_titles = list(
            dict.fromkeys(
                str(item.get("incomingTaskTitle") or "the requested task")
                for item in recurring_conflicts
            )
        )
        incoming_label = incoming_titles[0] if len(incoming_titles) == 1 else "the requested tasks"
        requested_clock = _clock(recurring_conflicts[0].get("requestedStartAt"))
        blocker_titles = list(
            dict.fromkeys(
                str(item.get("blockerTaskTitle") or "calendar item")
                for item in recurring_conflicts
            )
        )
        blocker_label = ", ".join(blocker_titles)
        blocker_schedule_label = ", ".join(
            dict.fromkeys(
                (
                    f"{str(item.get('blockerTaskTitle') or 'calendar item')} "
                    f"({_clock(item.get('blockerStartAt'))}–{_clock(item.get('blockerEndAt'))})"
                )
                for item in recurring_conflicts
            )
        )
        has_hard_blocker = any(
            item.get("fixedReason") in {"protected", "pinned"}
            for item in recurring_conflicts
        )
        recommended_move = all(
            item.get("recommendedAction") == "move_existing"
            for item in recurring_conflicts
        )
        gap_conflicts = [
            item
            for item in recurring_conflicts
            if item.get("conflictKind") == "minimum_gap"
        ]
        if gap_conflicts:
            first_gap = gap_conflicts[0]
            gap_minutes = int(first_gap.get("gapMinutes") or 0)
            available_gap = int(first_gap.get("availableGapMinutes") or 0)
            message = (
                f"On {_calendar_day(first_gap.get('date'))}, {incoming_label} starts at "
                f"{_clock(first_gap.get('requestedStartAt'))}, only {available_gap} minutes "
                f"after {str(first_gap.get('blockerTaskTitle') or 'the existing item')} "
                f"ends at {_clock(first_gap.get('blockerEndAt'))}. Your schedule requires "
                f"a {gap_minutes}-minute transition gap. Choose whether to keep the call "
                "at its requested time and move the existing item, or keep the existing "
                "item and move the call."
            )
        elif has_hard_blocker:
            message = (
                f"I found {len(recurring_conflicts)} conflict"
                f"{'' if len(recurring_conflicts) == 1 else 's'} with protected or pinned "
                "calendar items. Review every affected time below."
            )
        elif recommended_move:
            message = (
                f"{incoming_label} has the clearer fixed-time claim at {requested_clock}, "
                f"but I could not safely relocate every conflict automatically. "
                f"Review the {len(recurring_conflicts)} affected time"
                f"{'' if len(recurring_conflicts) == 1 else 's'} below."
            )
        else:
            message = (
                f"{incoming_label} at {requested_clock} conflicts with "
                f"{blocker_schedule_label}. I found {len(recurring_conflicts)} time conflict"
                f"{'' if len(recurring_conflicts) == 1 else 's'} where priority or "
                "movability is not clear, so choose which schedule should take precedence."
            )
        choices = [
            {
                "kind": "field",
                "label": f"Keep {blocker_label}",
                "value": "preserve_existing",
                "description": f"Keep the existing blocks and move {incoming_label}.",
                "follow_up_prompt": (
                    f"Keep the existing calendar items. Move {incoming_label} to the "
                    "nearest suitable times, and show me the complete revised schedule."
                ),
            }
        ]
        if not has_hard_blocker:
            choices.insert(
                0,
                {
                    "kind": "field",
                    "label": f"Keep {incoming_label} at {requested_clock}",
                    "value": "move_existing",
                    "description": (
                        f"Move {blocker_label} to suitable nearby openings and show every change."
                    ),
                    "follow_up_prompt": (
                        f"Keep {incoming_label} at its requested recurring time. Move every "
                        "conflicting existing task to the nearest suitable opening. If any "
                        "cannot be moved, tell me exactly which task and date still needs a decision."
                    ),
                },
            )
        return {
            "status": "needs_user_input",
            "message": message,
            "field": "calendar_conflict_priority",
            "choices": choices,
            "conflicts": recurring_conflicts,
        }

    if "confirm which task should keep this time" in lowered:
        match = re.search(
            r"(?:The requested time conflicts with your calendar on \d{4}-\d{2}-\d{2}:\s*)?"
            r"(?P<incoming>[^:\n]+?) at (?P<requested>\d{4}-\d{2}-\d{2}T[^ ]+) is blocked by "
            r"(?P<blocker>.+?) \((?P<priority>low|medium|high) priority, "
            r"(?P<start>\d{4}-\d{2}-\d{2}T[^ ]+) to (?P<end>\d{4}-\d{2}-\d{2}T[^)]+)\)",
            "\n".join(messages),
            re.IGNORECASE,
        )
        if match:
            def clock(value: str) -> str:
                parsed = datetime.fromisoformat(value)
                return parsed.strftime("%I:%M %p").lstrip("0")

            incoming = match.group("incoming").strip()
            blocker = match.group("blocker").strip()
            priority = match.group("priority").lower()
            requested = clock(match.group("requested"))
            start = clock(match.group("start"))
            end = clock(match.group("end"))
            can_move_existing = not any(
                marker in lowered for marker in ("[protected]", "[pinned]")
            )
            choices = [
                {
                    "kind": "field",
                    "label": f"Keep {blocker} here",
                    "value": "preserve_existing",
                    "description": f"Find another time for {incoming}.",
                    "follow_up_prompt": (
                        f"Do not move the existing task. Find another time for {incoming}."
                    ),
                }
            ]
            if can_move_existing:
                choices.append(
                    {
                        "kind": "field",
                        "label": f"Use this time for {incoming}",
                        "value": "move_existing",
                        "description": f"Move {blocker} to a nearby opening.",
                        "follow_up_prompt": (
                            f"Move the existing task and keep the requested time for {incoming}."
                        ),
                    }
                )
            return {
                "status": "needs_user_input",
                "message": (
                    f"{blocker} ({priority} priority) is already scheduled from "
                    f"{start} to {end}, which blocks {incoming} at {requested}. "
                    "Which commitment should keep this time?"
                ),
                "field": "calendar_conflict_priority",
                "choices": choices,
            }

    if (
        "conflicts with saved scheduling rule" in lowered
        or "confirm whether to override this saved rule" in lowered
    ):
        matches = re.findall(
            r"conflicts with saved scheduling rule:\s*(.+?)\.\s*confirm whether",
            "\n".join(messages),
            re.IGNORECASE,
        )
        rules = list(dict.fromkeys(item.strip() for item in matches if item.strip()))
        rule_summary = "; ".join(rules) if rules else "a saved scheduling rule"
        return {
            "status": "needs_user_input",
            "message": (
                f"This time conflicts with your saved rule: {rule_summary}. "
                "Should I use the requested time anyway?"
            ),
            "field": "preference_override",
            "choices": [
                {
                    "kind": "field",
                    "label": "Use this time anyway",
                    "value": "override_saved_rule",
                    "description": "Keep this exact time for this recurring schedule.",
                    "follow_up_prompt": (
                        "Keep the requested exact recurring time and override the "
                        "conflicting saved scheduling rule for this schedule."
                    ),
                },
                {
                    "kind": "field",
                    "label": "Respect my saved rule",
                    "value": "respect_saved_rule",
                    "description": "Find a different time that follows the saved rule.",
                    "follow_up_prompt": (
                        "Respect my saved scheduling rule and find a different time "
                        "for this recurring task."
                    ),
                },
            ],
        }

    if "unsupported" in lowered or "choose a daily or weekly recurrence" in lowered:
        return {
            "status": "unsupported",
            "message": "That recurrence pattern is not supported yet.",
            "field": None,
            "choices": [],
        }
    if (
        "end date or number of days" in lowered
        or "end date, occurrence count, or an ongoing recurrence" in lowered
        or "recurrence_end" in lowered
    ):
        return {
            "status": "needs_user_input",
            "message": "How long should this repeat?",
            "field": "recurrence_end",
            "choices": [
                {
                    "kind": "field",
                    "label": "Keep it ongoing",
                    "value": "never",
                    "description": "Repeat every week until you stop it.",
                    "follow_up_prompt": "Keep this recurring schedule ongoing with no end date.",
                },
                {
                    "kind": "field",
                    "label": "Through this year",
                    "value": "end_of_year",
                    "description": "Stop after the final matching day this year.",
                    "follow_up_prompt": "Continue this recurring schedule through the end of this year.",
                },
            ],
        }
    if "first day" in lowered:
        return {
            "status": "needs_user_input",
            "message": "When should this recurring schedule begin?",
            "field": "recurrence_start",
            "choices": [],
        }
    if "duration" in lowered and "choose" in lowered:
        return {
            "status": "needs_user_input",
            "message": "How long should each occurrence last?",
            "field": "duration_minutes",
            "choices": [],
        }
    if (
        "no suitable opening" in lowered
        or "already occupied" in lowered
        or "conflicts with your calendar" in lowered
    ):
        return {
            "status": "schedule_conflict",
            "message": "Some requested times conflict with your calendar. Review the affected dates.",
            "field": None,
            "choices": [],
        }
    if (
        "multiple approval envelopes" in lowered
        or "canonicalization" in lowered
        or "projected into the standard ui contract" in lowered
        or "no calendar operations" in lowered
        or "no task operations" in lowered
    ):
        return {
            "status": "repairable_internally",
            "message": "I couldn’t finish preparing the review. I can resume from the saved draft.",
            "field": None,
            "choices": [],
        }
    return {
        "status": "repairable_internally",
        "message": "I couldn’t finish preparing the review. I can resume from the saved draft.",
        "field": None,
        "choices": [],
    }


def clarification_for_handoff_outcome(outcome: dict[str, Any]) -> dict[str, Any] | None:
    status = str(outcome.get("status") or "")
    if status not in {"needs_user_input", "schedule_conflict", "unsupported"}:
        return None
    choices = [item for item in outcome.get("choices", []) if isinstance(item, dict)]
    return {
        "message": str(outcome.get("message") or "Please clarify before I continue."),
        "entity_choices": [],
        "field_choices": choices,
        "scoped_follow_up_prompts": [],
        "conflicts": [
            item for item in outcome.get("conflicts", []) if isinstance(item, dict)
        ],
    }
