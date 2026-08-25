"""PreferencePatch proposal, validation, and approval runtime."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.models.preference_memory import (
    PreferenceChangeLog,
    SchedulingConstraint,
    UserPreference,
)
from app.services.preference_values import (
    decode_preference_value,
    inter_task_gap_minutes_from_text,
    preference_rules_overlap,
    text_explicitly_applies_to_all_tasks,
)

PREFERENCE_PROPOSAL_KIND = "preference_memory"
PREFERENCE_RESPONSE_KEY = "preference_memory_proposal"
PREFERENCE_PATCH_VERSION = "preference-patch-v1"
DEFAULT_USER_ID = "local"
ALL_DAYS_OF_WEEK = [0, 1, 2, 3, 4, 5, 6]
WEEKDAY_DAYS_OF_WEEK = [0, 1, 2, 3, 4]
WEEKEND_DAYS_OF_WEEK = [5, 6]
DAY_NAME_TO_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
DEFAULT_SLEEP_DURATION_MINUTES = 8 * 60
EARLY_MORNING_AFTER_BOUNDARY_CONTEXT_LIMIT_MINUTES = 5 * 60
NEGATION_TERMS = r"(?:do not|don't|dont|never|avoid|no|please\s+don't|please\s+dont)"
SCHEDULE_ACTION_TERMS = (
    r"(?:schedule|put|place|book|assign|plan|slot|scheduling|assigning|"
    r"tasks?|work|meetings?)"
)
BEFORE_BOUNDARY_TERMS = r"(?:before|earlier\s+than|prior\s+to)"
AFTER_BOUNDARY_TERMS = r"(?:after|past|beyond|later\s+than)"
BOUNDARY_RELATION_PATTERN = rf"(?P<direction>{BEFORE_BOUNDARY_TERMS}|{AFTER_BOUNDARY_TERMS})"
REFERENCE_BOUNDARY_TERMS = (
    r"(?:that|then|it|bedtime|sleep(?:\s+time)?|my\s+bedtime|my\s+sleep(?:\s+time)?)"
)

MEMORY_TRIGGER_RE = re.compile(
    r"\b(?:remember|save|keep in mind|my preference|i prefer|prefer|"
    r"do not schedule|don't schedule|dont schedule|never schedule|avoid scheduling|no scheduling|"
    r"don't put|dont put|do not put|never put|avoid putting|"
    r"do not assign|don't assign|dont assign|never assign|avoid assigning|"
    r"sleep|asleep|bedtime|unavailable|not available|"
    r"energy|fresh|tired|drained|lighter tasks?|heavier tasks?|"
    r"usually|typically|normally|always|every day|weekdays?|weekends?|"
    r"best time|mornings? are best|afternoons? are best|evenings? are best|"
    r"nights? are best)\b",
    re.IGNORECASE,
)
EXPLICIT_MEMORY_REQUEST_RE = re.compile(
    r"\b(?:remember|save|keep in mind)\b",
    re.IGNORECASE,
)
ONE_OFF_SCHEDULING_REQUEST_RE = re.compile(
    r"\b(?:schedule|put|place|book|assign|move|reschedule)\s+"
    r"(?:this|that|it|these|those|the\s+selected|selected\s+tasks?|the\s+task)\b",
    re.IGNORECASE,
)
SCHEDULING_RE = re.compile(
    r"\b(?:schedule|calendar|time|slot|block|task|tasks|work|meeting|meetings|deep work|focus|"
    r"assign|assignment|"
    r"sleep|asleep|bedtime|availability|available|unavailable|morning|mornings|"
    r"afternoon|afternoons|evening|evenings|night|nights|weekday|weekdays|"
    r"weekend|weekends|energy|fresh|tired|drained|lighter|heavier)\b",
    re.IGNORECASE,
)
TIME_PATTERN = r"(?:midnight|noon|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)?)"
TIME_RE = re.compile(TIME_PATTERN, re.IGNORECASE)
TIME_BOUNDARY_RE = re.compile(
    rf"\b{NEGATION_TERMS}\s+{SCHEDULE_ACTION_TERMS}\b.*?"
    rf"\b{BOUNDARY_RELATION_PATTERN}\s+(?P<time>{TIME_PATTERN})",
    re.IGNORECASE,
)
REFERRED_TIME_BOUNDARY_RE = re.compile(
    rf"\b{NEGATION_TERMS}\s+{SCHEDULE_ACTION_TERMS}\b.*?"
    rf"\b{BOUNDARY_RELATION_PATTERN}\s+{REFERENCE_BOUNDARY_TERMS}\b",
    re.IGNORECASE,
)
BETWEEN_RE = re.compile(
    rf"\b{NEGATION_TERMS}\s+{SCHEDULE_ACTION_TERMS}\b.*?"
    rf"\b(?:between|from)\s+(?P<start>{TIME_PATTERN})\s+(?:and|to|-)\s+(?P<end>{TIME_PATTERN})",
    re.IGNORECASE,
)
SLEEP_RE = re.compile(
    rf"\b(?:sleep|asleep|bed|bedtime)\b.*?"
    rf"\b(?:between|from)\s+(?P<start>{TIME_PATTERN})\s+(?:and|to|-)\s+(?P<end>{TIME_PATTERN})",
    re.IGNORECASE,
)
SLEEP_CUTOFF_RE = re.compile(
    rf"\b(?:sleep|asleep|bed|bedtime|sleep\s+time)\b.*?"
    rf"\b(?:is|starts?|begins?|at|by|around)\s+(?P<time>{TIME_PATTERN})"
    rf"|\b(?:go\s+to\s+bed|head\s+to\s+bed|turn\s+in)\b.*?"
    rf"\b(?:at|by|around)\s+(?P<time_alt>{TIME_PATTERN})",
    re.IGNORECASE,
)
STABLE_PREFERENCE_CONTEXT_RE = re.compile(
    r"\b(?:usually|typically|normally|often|these days|nowadays|always|"
    r"every day|weeknights?|weekdays?|weekends?|my\s+(?:bedtime|routine|schedule|availability))\b",
    re.IGNORECASE,
)
TEMPORARY_SLEEP_CONTEXT_RE = re.compile(
    r"\b(?:might|maybe|probably|tonight|today|tomorrow|this\s+one\s+time|just\s+today)\b",
    re.IGNORECASE,
)
TEMPORARY_OR_UNCERTAIN_CONTEXT_RE = re.compile(
    r"\b(?:might|maybe|probably|possibly|not\s+sure|tonight|today|tomorrow|"
    r"this\s+one\s+time|just\s+today|this\s+week|next\s+week|temporarily|for\s+now)\b",
    re.IGNORECASE,
)
UNAVAILABLE_RE = re.compile(
    rf"\b(?:unavailable|not\s+available)\b.*?"
    rf"\b(?:between|from)\s+(?P<start>{TIME_PATTERN})\s+(?:and|to|-)\s+(?P<end>{TIME_PATTERN})",
    re.IGNORECASE,
)
SOFT_PREFERENCE_RE = re.compile(
    r"(?:\b(?:i|we)\s+(?:(?:always|generally|usually|typically|normally)\s+)?prefer\b|"
    r"\bmy preference is\b|^\s*prefer\b)(?P<body>.+)",
    re.IGNORECASE,
)
DESIRE_STYLE_RE = re.compile(
    r"\b(?:i(?:'d)?|we(?:'d)?)\s+"
    r"(?:(?:would\s+)?like|want|need)\s+"
    r"(?P<body>(?:to\s+)?(?:do|have|keep|use|work|meet|schedule|plan|prefer|avoid|"
    r"focus|code|write|take|start|finish).+)",
    re.IGNORECASE,
)
USUAL_STYLE_RE = re.compile(
    r"\b(?:i\s+)?(?:usually|typically|normally)\s+"
    r"(?P<body>(?:do|take|schedule|plan|prefer|use|keep|work|meet|avoid|need).+)",
    re.IGNORECASE,
)
BEST_TIME_RE = re.compile(
    r"\b(?P<body>"
    r"(?:(?:mornings?|afternoons?|evenings?|nights?|weekdays?|weekends?)\s+"
    r"(?:are|is)\s+(?:best|better|ideal|good).{0,80}"
    r"(?:deep work|focus|focused work|meetings?|tasks?|work))|"
    r"(?:(?:deep work|focus|focused work|meetings?|tasks?|work).{0,80}"
    r"(?:best|better|ideal|good).{0,80}"
    r"(?:mornings?|afternoons?|evenings?|nights?|weekdays?|weekends?))"
    r")\b",
    re.IGNORECASE,
)
TIME_PREPOSITION_RE = re.compile(
    rf"\b(?:at|by|before|after|past|beyond|from|between|until|around|"
    rf"earlier\s+than|later\s+than)\s+(?P<time>{TIME_PATTERN})",
    re.IGNORECASE,
)
VAGUE_TIME_ANCHOR_RE = re.compile(
    r"\b(?:lunch|dinner|breakfast|school\s+pickup|pickup|dropoff|commute|"
    r"family\s+time|personal\s+time|work\s+hours?|office\s+hours?|"
    r"morning|afternoon|evening|night)\b",
    re.IGNORECASE,
)
HARD_SCHEDULING_INTENT_RE = re.compile(
    rf"\b{NEGATION_TERMS}\s+{SCHEDULE_ACTION_TERMS}\b",
    re.IGNORECASE,
)
HARD_SCHEDULING_RELATION_RE = re.compile(
    rf"\b(?:{BEFORE_BOUNDARY_TERMS}|{AFTER_BOUNDARY_TERMS}|during|around|while|when|until)\b",
    re.IGNORECASE,
)
UNAVAILABLE_INTENT_RE = re.compile(
    r"\b(?:unavailable|not\s+available|busy|blocked)\b",
    re.IGNORECASE,
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _clean_text(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _edit_distance_at_most(value: str, target: str, limit: int) -> bool:
    if abs(len(value) - len(target)) > limit:
        return False
    previous = list(range(len(target) + 1))
    for value_index, value_char in enumerate(value, start=1):
        current = [value_index]
        row_min = current[0]
        for target_index, target_char in enumerate(target, start=1):
            cost = 0 if value_char == target_char else 1
            current_value = min(
                previous[target_index] + 1,
                current[target_index - 1] + 1,
                previous[target_index - 1] + cost,
            )
            current.append(current_value)
            row_min = min(row_min, current_value)
        if row_min > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _has_explicit_memory_request(normalized_prompt: str) -> bool:
    if EXPLICIT_MEMORY_REQUEST_RE.search(normalized_prompt):
        return True
    tokens = re.findall(r"[a-zA-Z']+", normalized_prompt.lower())
    for token in tokens[:6]:
        if len(token) >= 6 and _edit_distance_at_most(token, "remember", 2):
            return True
    return False


def _strip_explicit_memory_lead_in(prompt: str) -> str:
    cleaned = _clean_text(prompt)
    if not cleaned:
        return cleaned
    stripped = re.sub(
        r"^\s*(?:remember|save|keep\s+in\s+mind)\b\s*(?:that|this|:|-|,)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if stripped != cleaned:
        return stripped
    tokens = cleaned.split(maxsplit=1)
    if tokens and _has_explicit_memory_request(tokens[0]):
        return tokens[1] if len(tokens) > 1 else ""
    return cleaned


def _time_minutes(value: str) -> int | None:
    normalized = value.strip().lower().replace(".", "")
    if normalized == "midnight":
        return 0
    if normalized == "noon":
        return 12 * 60
    match = re.fullmatch(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>am|pm)?", normalized)
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or "0")
    period = match.group("period")
    if hour > 23 or minute > 59:
        return None
    if period == "am":
        if hour == 12:
            hour = 0
    elif period == "pm":
        if hour != 12:
            hour += 12
    return hour * 60 + minute


def _time_label(minutes: int) -> str:
    normalized = minutes % (24 * 60)
    return f"{normalized // 60:02d}:{normalized % 60:02d}"


def _time_spoken(minutes: int) -> str:
    normalized = minutes % (24 * 60)
    hour = normalized // 60
    minute = normalized % 60
    period = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    if minute == 0:
        return f"{display_hour} {period}"
    return f"{display_hour}:{minute:02d} {period}"


def _time_text_is_ambiguous(value: str | None) -> bool:
    normalized = (value or "").strip().lower().replace(".", "")
    if not normalized or normalized in {"midnight", "noon"}:
        return False
    match = re.fullmatch(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>am|pm)?", normalized)
    if match is None:
        return False
    if match.group("period"):
        return False
    hour = int(match.group("hour"))
    return 1 <= hour <= 12


def _ambiguous_time_texts(normalized_prompt: str) -> list[str]:
    ambiguous: list[str] = []
    for match in TIME_PREPOSITION_RE.finditer(normalized_prompt):
        time_text = _clean_text(match.group("time"))
        if _time_text_is_ambiguous(time_text):
            ambiguous.append(time_text)
    return list(dict.fromkeys(ambiguous))


def _match_time_minutes(match: re.Match[str], *group_names: str) -> int | None:
    for group_name in group_names:
        value = match.groupdict().get(group_name)
        if isinstance(value, str) and value.strip():
            return _time_minutes(value)
    return None


def _boundary_direction(value: str | None) -> str | None:
    normalized = _clean_text(value).lower()
    if not normalized:
        return None
    if re.fullmatch(BEFORE_BOUNDARY_TERMS, normalized, re.IGNORECASE):
        return "before"
    if re.fullmatch(AFTER_BOUNDARY_TERMS, normalized, re.IGNORECASE):
        return "after"
    return None


def _personal_cutoff_minutes(normalized_prompt: str) -> int | None:
    cutoff_match = SLEEP_CUTOFF_RE.search(normalized_prompt)
    if cutoff_match is None:
        return None
    return _match_time_minutes(cutoff_match, "time", "time_alt")


def _is_temporary_sleep_context(normalized_prompt: str) -> bool:
    return bool(TEMPORARY_SLEEP_CONTEXT_RE.search(normalized_prompt))


def _is_temporary_or_uncertain_context(normalized_prompt: str) -> bool:
    return bool(TEMPORARY_OR_UNCERTAIN_CONTEXT_RE.search(normalized_prompt))


def _is_stable_sleep_cutoff_prompt(normalized_prompt: str) -> bool:
    if _is_temporary_sleep_context(normalized_prompt):
        return False
    if STABLE_PREFERENCE_CONTEXT_RE.search(normalized_prompt):
        return True
    return bool(re.search(r"\b(?:i|we)\s+(?:sleep|go\s+to\s+bed|head\s+to\s+bed|turn\s+in)\b", normalized_prompt, re.IGNORECASE))


def _sleep_cutoff_needs_time_clarification(normalized_prompt: str) -> bool:
    time_text = _sleep_cutoff_time_text(normalized_prompt)
    if time_text is None:
        return False
    normalized_time = time_text.strip().lower().replace(".", "")
    return normalized_time in {"noon", "12pm", "12 pm"} or _time_text_is_ambiguous(normalized_time)


def _sleep_cutoff_time_text(normalized_prompt: str) -> str | None:
    cutoff_match = SLEEP_CUTOFF_RE.search(normalized_prompt)
    if cutoff_match is None:
        return None
    for group_name in ("time", "time_alt"):
        candidate = cutoff_match.groupdict().get(group_name)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _literal_after_boundary_needs_context(boundary_minutes: int | None) -> bool:
    return (
        boundary_minutes is not None
        and 0 <= boundary_minutes < EARLY_MORNING_AFTER_BOUNDARY_CONTEXT_LIMIT_MINUTES
    )


def _days_of_week_from_prompt(prompt: str) -> list[int]:
    normalized = _clean_text(prompt).lower()
    if re.search(r"\bweekdays?\b", normalized):
        return WEEKDAY_DAYS_OF_WEEK
    if re.search(r"\bweekends?\b", normalized):
        return WEEKEND_DAYS_OF_WEEK
    selected: list[int] = []
    for day_name, day_index in DAY_NAME_TO_INDEX.items():
        if re.search(rf"\b{day_name}s?\b", normalized):
            selected.append(day_index)
    if selected:
        return sorted(set(selected))
    return ALL_DAYS_OF_WEEK


def _domain_from_preference_text(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(?:deep work|focus|focused|coding|writing)\b", normalized):
        return "deep_work"
    if re.search(r"\b(?:meeting|meetings|call|calls|appointment|calendar|event)\b", normalized):
        return "calendar"
    if re.search(r"\b(?:sleep|asleep|bed|bedtime|wake)\b", normalized):
        return "sleep"
    if re.search(r"\b(?:energy|tired|fresh|drained)\b", normalized):
        return "energy"
    return "scheduling"


def _soft_strength_from_prompt(prompt: str) -> str:
    normalized = prompt.lower()
    if re.search(r"\b(?:always|never)\b", normalized):
        return "strong"
    if re.search(r"\b(?:ideally|when possible|if possible|prefer|usually|typically|normally)\b", normalized):
        return "medium"
    return "medium"


def _has_memory_candidate_shape(normalized_prompt: str) -> bool:
    if ONE_OFF_SCHEDULING_REQUEST_RE.search(normalized_prompt):
        return False
    has_memory_intent = _has_explicit_memory_request(normalized_prompt) or MEMORY_TRIGGER_RE.search(normalized_prompt)
    return bool(has_memory_intent and SCHEDULING_RE.search(normalized_prompt))


def _hard_intent_lacks_concrete_window(normalized_prompt: str) -> bool:
    if not (
        HARD_SCHEDULING_INTENT_RE.search(normalized_prompt)
        or UNAVAILABLE_INTENT_RE.search(normalized_prompt)
    ):
        return False
    has_window = bool(
        TIME_BOUNDARY_RE.search(normalized_prompt)
        or REFERRED_TIME_BOUNDARY_RE.search(normalized_prompt)
        or BETWEEN_RE.search(normalized_prompt)
        or SLEEP_RE.search(normalized_prompt)
        or UNAVAILABLE_RE.search(normalized_prompt)
    )
    if has_window:
        return False
    return bool(
        HARD_SCHEDULING_RELATION_RE.search(normalized_prompt)
        or VAGUE_TIME_ANCHOR_RE.search(normalized_prompt)
    )


def _sleep_window_patch(
    *,
    prompt: str,
    start_minutes: int,
    end_minutes: int,
    rule: str,
    inferred_wake_time: bool = False,
) -> dict[str, Any]:
    description = f"Treat {_time_spoken(start_minutes)} to {_time_spoken(end_minutes)} as unavailable sleep time."
    value_extra = (
        {
            "assumption": "wake_time_inferred",
            "inferredWakeTime": _time_label(end_minutes),
            "inferredSleepDurationMinutes": DEFAULT_SLEEP_DURATION_MINUTES,
        }
        if inferred_wake_time
        else None
    )
    assumption_warnings = (
        [
            (
                f"Assumed an 8-hour sleep window ending at {_time_spoken(end_minutes)} "
                "because no wake time was specified."
            )
        ]
        if inferred_wake_time
        else None
    )
    return _hard_window_patch(
        prompt=prompt,
        start_minutes=start_minutes,
        end_minutes=end_minutes,
        description=description,
        rule=rule,
        value_extra=value_extra,
        assumption_warnings=assumption_warnings,
    )


def _hard_boundary_patch(
    *,
    prompt: str,
    direction: str,
    boundary_minutes: int,
    rule: str,
) -> dict[str, Any] | None:
    if direction == "before":
        description = f"Do not schedule work before {_time_spoken(boundary_minutes)}."
        return _hard_window_patch(
            prompt=prompt,
            start_minutes=0,
            end_minutes=boundary_minutes,
            description=description,
            rule=rule,
        )
    if direction == "after":
        description = f"Do not schedule work after {_time_spoken(boundary_minutes)}."
        return _hard_window_patch(
            prompt=prompt,
            start_minutes=boundary_minutes,
            end_minutes=(24 * 60) - 1,
            description=description,
            rule=rule,
        )
    return None


def _time_boundary_patch(prompt: str, normalized_prompt: str) -> dict[str, Any] | None:
    boundary_minutes = _personal_cutoff_minutes(normalized_prompt)
    if boundary_minutes is not None and _is_stable_sleep_cutoff_prompt(normalized_prompt):
        inferred_end = (boundary_minutes + DEFAULT_SLEEP_DURATION_MINUTES) % (24 * 60)
        return _sleep_window_patch(
            prompt=prompt,
            start_minutes=boundary_minutes,
            end_minutes=inferred_end,
            rule="sleep_window_inferred_wake",
            inferred_wake_time=True,
        )

    direct_match = TIME_BOUNDARY_RE.search(normalized_prompt)
    if direct_match is not None:
        direction = _boundary_direction(direct_match.group("direction"))
        direct_boundary_minutes = _match_time_minutes(direct_match, "time")
        if direction == "after" and _literal_after_boundary_needs_context(direct_boundary_minutes):
            return None
        if direction is not None and direct_boundary_minutes is not None:
            return _hard_boundary_patch(
                prompt=prompt,
                direction=direction,
                boundary_minutes=direct_boundary_minutes,
                rule=f"do_not_schedule_{direction}",
            )

    referenced_match = REFERRED_TIME_BOUNDARY_RE.search(normalized_prompt)
    if referenced_match is not None and boundary_minutes is not None:
        direction = _boundary_direction(referenced_match.group("direction"))
        if direction == "after":
            inferred_end = (boundary_minutes + DEFAULT_SLEEP_DURATION_MINUTES) % (24 * 60)
            return _sleep_window_patch(
                prompt=prompt,
                start_minutes=boundary_minutes,
                end_minutes=inferred_end,
                rule="sleep_window_inferred_wake",
                inferred_wake_time=True,
            )
        if direction is not None:
            return _hard_boundary_patch(
                prompt=prompt,
                direction=direction,
                boundary_minutes=boundary_minutes,
                rule="personal_time_boundary",
            )

    return None


def _first_time_text(value: str) -> str | None:
    match = TIME_RE.search(value)
    return match.group(0) if match is not None else None


def _latest_sleep_context_prompt(conversation_history: list[dict[str, str]] | None) -> str | None:
    if not isinstance(conversation_history, list):
        return None
    for item in reversed(conversation_history[-12:]):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = _clean_text(item.get("content"))
        if not content:
            continue
        if SLEEP_CUTOFF_RE.search(content) or re.search(r"\b(?:sleep|bedtime|go\s+to\s+bed)\b", content, re.IGNORECASE):
            return content
    return None


def contextualize_preference_prompt(
    prompt: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    """Carry a short clarification answer into the prior durable preference frame."""

    normalized = _clean_text(prompt)
    time_text = _first_time_text(normalized)
    if not normalized or time_text is None:
        return prompt
    prior_sleep_prompt = _latest_sleep_context_prompt(conversation_history)
    if prior_sleep_prompt is None:
        return prompt
    looks_like_time_answer = bool(
        re.search(r"\b(?:mean|meant|am|pm|midnight|noon|yes|yeah|yep)\b", normalized, re.IGNORECASE)
    ) and len(normalized.split()) <= 8
    looks_like_boundary_followup = bool(TIME_BOUNDARY_RE.search(normalized))
    if not looks_like_time_answer and not looks_like_boundary_followup:
        return prompt
    return f"I sleep at {time_text}. {normalized}"


def build_preference_memory_clarification(
    prompt: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    normalized = _clean_text(contextualize_preference_prompt(prompt, conversation_history))
    if not normalized:
        return None
    if not _has_memory_candidate_shape(normalized):
        return None
    if _is_temporary_or_uncertain_context(normalized):
        return None
    if _sleep_cutoff_needs_time_clarification(normalized):
        sleep_time_text = _sleep_cutoff_time_text(normalized) or "that time"
        normalized_sleep_time = sleep_time_text.strip().lower().replace(".", "")
        if normalized_sleep_time in {"noon", "12pm", "12 pm", "12"}:
            message = (
                "Did you mean 12:00 AM (midnight) or 12:00 PM (noon)? "
                "I need the nighttime sleep start before I can save a useful scheduling preference."
            )
            follow_ups = [
                "I mean 12:00 AM",
                "I mean 12:00 PM",
                "Don't save this preference",
            ]
        else:
            message = (
                f"Did you mean {sleep_time_text} AM or {sleep_time_text} PM? "
                "I need the nighttime sleep start before I can save a useful scheduling preference."
            )
            follow_ups = [
                f"I mean {sleep_time_text} AM",
                f"I mean {sleep_time_text} PM",
                "Don't save this preference",
            ]
        return {
            "message": message,
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": follow_ups,
        }
    ambiguous_times = _ambiguous_time_texts(normalized)
    if ambiguous_times:
        time_text = ambiguous_times[0]
        return {
            "message": (
                f"I can treat this as a scheduling preference, but '{time_text}' is ambiguous. "
                "Please specify AM/PM or a 24-hour time before I save it."
            ),
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": [
                f"I mean {time_text} AM",
                f"I mean {time_text} PM",
                "Don't save this preference",
            ],
        }
    if _hard_intent_lacks_concrete_window(normalized):
        return {
            "message": (
                "That sounds like a hard scheduling rule, but the time window is not concrete enough "
                "for me to save safely. Tell me the exact start/end time or say to keep it only as a soft preference."
            ),
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": [
                "Use a specific time window",
                "Save it only as a soft preference",
                "Don't save this preference",
            ],
        }

    direct_match = TIME_BOUNDARY_RE.search(normalized)
    direct_boundary_minutes = _match_time_minutes(direct_match, "time") if direct_match else None
    direction = _boundary_direction(direct_match.group("direction")) if direct_match else None
    has_sleep_context = _personal_cutoff_minutes(normalized) is not None or _latest_sleep_context_prompt(conversation_history) is not None
    if (
        direction == "after"
        and _literal_after_boundary_needs_context(direct_boundary_minutes)
        and not has_sleep_context
    ):
        boundary_text = _time_spoken(direct_boundary_minutes or 0)
        blocked_scope = "the whole day" if direct_boundary_minutes == 0 else "almost the entire day"
        return {
            "message": (
                f"{boundary_text} is very early in the day, so saving 'after {boundary_text}' literally would "
                f"block {blocked_scope}. "
                "Should I save a sleep window instead? If so, tell me the wake time or say to use an 8-hour sleep window."
            ),
            "entity_choices": [],
            "field_choices": [],
            "scoped_follow_up_prompts": [
                f"Use {boundary_text} to {_time_spoken((direct_boundary_minutes or 0) + DEFAULT_SLEEP_DURATION_MINUTES)} as sleep time",
                "I wake up at 7:00 AM",
                "Don't save this preference",
            ],
        }

    return None


def _base_patch(prompt: str) -> dict[str, Any]:
    return {
        "patchId": f"preference-patch-{uuid4()}",
        "patchVersion": PREFERENCE_PATCH_VERSION,
        "intentSummary": _clean_text(prompt),
        "userId": DEFAULT_USER_ID,
        "operations": [],
    }


def _preference_operation(
    *,
    temp_id: str,
    description: str,
    value: dict[str, Any],
    strength: str,
    preference_type: str,
    domain: str = "scheduling",
) -> dict[str, Any]:
    return {
        "op": "create_preference",
        "tempId": temp_id,
        "preference": {
            "userId": DEFAULT_USER_ID,
            "domain": domain,
            "preferenceType": preference_type,
            "scopeType": "global",
            "scopeId": None,
            "strength": strength,
            "source": "user_explicit",
            "confidence": 1.0,
            "active": True,
            "description": description,
            "value": value,
        },
    }


def _constraint_operation(
    *,
    description: str,
    value: dict[str, Any],
    source_preference_temp_id: str,
) -> dict[str, Any]:
    return {
        "op": "create_constraint",
        "sourcePreferenceTempId": source_preference_temp_id,
        "constraint": {
            "userId": DEFAULT_USER_ID,
            "constraintType": "forbidden_window",
            "targetType": "global",
            "targetId": None,
            "hard": True,
            "penaltyWeight": 1.0,
            "source": "user_explicit",
            "confidence": 1.0,
            "active": True,
            "description": description,
            "value": value,
        },
    }


def _recurring_window_value(
    start_minutes: int,
    end_minutes: int,
    rule: str,
    *,
    days_of_week: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "recurrence": "weekly",
        "daysOfWeek": days_of_week or ALL_DAYS_OF_WEEK,
        "startTime": _time_label(start_minutes),
        "endTime": _time_label(end_minutes),
        "timezone": "local",
        "rule": rule,
    }


def _hard_window_patch(
    *,
    prompt: str,
    start_minutes: int,
    end_minutes: int,
    description: str,
    rule: str,
    value_extra: dict[str, Any] | None = None,
    assumption_warnings: list[str] | None = None,
) -> dict[str, Any]:
    patch = _base_patch(prompt)
    temp_id = "preference-1"
    days_of_week = _days_of_week_from_prompt(prompt)
    value = _recurring_window_value(
        start_minutes,
        end_minutes,
        rule,
        days_of_week=days_of_week,
    )
    if value_extra:
        value.update(value_extra)
    preference_value = {
        "rule": rule,
        "recurrence": "weekly",
        "daysOfWeek": days_of_week,
        "startTime": _time_label(start_minutes),
        "endTime": _time_label(end_minutes),
        "sourceText": _clean_text(prompt),
        "extractionKind": "hard_constraint",
    }
    if value_extra:
        preference_value.update(value_extra)
    patch["operations"] = [
        _preference_operation(
            temp_id=temp_id,
            description=description,
            value=preference_value,
            strength="hard",
            preference_type="availability",
        ),
        _constraint_operation(
            description=description,
            value=value,
            source_preference_temp_id=temp_id,
        ),
    ]
    if assumption_warnings:
        patch["assumptionWarnings"] = assumption_warnings
    return patch


def _soft_preference_patch(prompt: str, body: str) -> dict[str, Any]:
    patch = _base_patch(prompt)
    body_text = _clean_text(body).rstrip(".")
    description = f"Preference: {body_text}."
    domain = _domain_from_preference_text(body_text)
    patch["operations"] = [
        _preference_operation(
            temp_id="preference-1",
            description=description,
            value={
                "preferenceText": body_text,
                "normalizedText": body_text.lower(),
                "sourceText": _clean_text(prompt),
                "extractionKind": "soft_preference",
                "constraintEligible": False,
            },
            strength=_soft_strength_from_prompt(prompt),
            preference_type="schedule_style",
            domain=domain,
        )
    ]
    return patch


def _memory_intent_from_prompt(prompt: str) -> str:
    normalized = _clean_text(prompt)
    if _has_explicit_memory_request(normalized):
        return "explicit_request"
    return "inferred_preference"


def build_preference_patch_from_prompt(prompt: str) -> dict[str, Any] | None:
    """Build a reviewable PreferencePatch from explicit or inferable memory language."""

    normalized = _clean_text(prompt)
    if not normalized:
        return None
    if not _has_memory_candidate_shape(normalized):
        return None
    if _is_temporary_or_uncertain_context(normalized):
        return None

    if _sleep_cutoff_needs_time_clarification(normalized) or _ambiguous_time_texts(normalized):
        return None
    if _hard_intent_lacks_concrete_window(normalized):
        return None

    sleep_match = SLEEP_RE.search(normalized)
    if sleep_match is not None:
        start = _time_minutes(sleep_match.group("start"))
        end = _time_minutes(sleep_match.group("end"))
        if start is not None and end is not None:
            description = (
                f"Treat {_time_spoken(start)} to {_time_spoken(end)} as unavailable sleep time."
            )
            return _hard_window_patch(
                prompt=prompt,
                start_minutes=start,
                end_minutes=end,
                description=description,
                rule="sleep_window",
            )

    unavailable_match = UNAVAILABLE_RE.search(normalized)
    if unavailable_match is not None:
        start = _time_minutes(unavailable_match.group("start"))
        end = _time_minutes(unavailable_match.group("end"))
        if start is not None and end is not None:
            description = f"Treat {_time_spoken(start)} to {_time_spoken(end)} as unavailable time."
            return _hard_window_patch(
                prompt=prompt,
                start_minutes=start,
                end_minutes=end,
                description=description,
                rule="unavailable_window",
            )

    between_match = BETWEEN_RE.search(normalized)
    if between_match is not None:
        start = _time_minutes(between_match.group("start"))
        end = _time_minutes(between_match.group("end"))
        if start is not None and end is not None:
            description = (
                f"Do not schedule work between {_time_spoken(start)} and {_time_spoken(end)}."
            )
            return _hard_window_patch(
                prompt=prompt,
                start_minutes=start,
                end_minutes=end,
                description=description,
                rule="do_not_schedule_between",
            )

    boundary_patch = _time_boundary_patch(prompt, normalized)
    if boundary_patch is not None:
        return boundary_patch

    soft_source = _strip_explicit_memory_lead_in(normalized)
    soft_match = SOFT_PREFERENCE_RE.search(normalized) or SOFT_PREFERENCE_RE.search(soft_source)
    if soft_match is not None:
        body = _clean_text(soft_match.group("body"))
        if body:
            return _soft_preference_patch(prompt, body)

    desire_match = DESIRE_STYLE_RE.search(normalized) or DESIRE_STYLE_RE.search(soft_source)
    if desire_match is not None:
        body = _clean_text(desire_match.group("body"))
        if body.lower().startswith("to "):
            body = _clean_text(body[3:])
        if body:
            return _soft_preference_patch(prompt, body)

    usual_match = USUAL_STYLE_RE.search(normalized) or USUAL_STYLE_RE.search(soft_source)
    if usual_match is not None:
        body = _clean_text(usual_match.group("body"))
        if body:
            return _soft_preference_patch(prompt, body)

    best_time_match = BEST_TIME_RE.search(normalized) or BEST_TIME_RE.search(soft_source)
    if best_time_match is not None:
        body = _clean_text(best_time_match.group("body"))
        if body:
            return _soft_preference_patch(prompt, body)

    return None


def validate_preference_patch(preference_patch: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    operations = preference_patch.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append("PreferencePatch must contain at least one operation.")
        operations = []

    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            errors.append(f"Operation {index + 1} must be an object.")
            continue
        op = str(operation.get("op") or "").strip()
        if op == "create_preference":
            preference = operation.get("preference")
            if not isinstance(preference, dict):
                errors.append(f"Operation {index + 1} is missing preference details.")
                continue
            if not _clean_text(str(preference.get("description") or "")):
                errors.append(f"Operation {index + 1} preference needs a description.")
            value = preference.get("value")
            if not isinstance(value, dict):
                errors.append(f"Operation {index + 1} preference value must be an object.")
        elif op == "create_constraint":
            constraint = operation.get("constraint")
            if not isinstance(constraint, dict):
                errors.append(f"Operation {index + 1} is missing constraint details.")
                continue
            value = constraint.get("value")
            if not isinstance(value, dict):
                errors.append(f"Operation {index + 1} constraint value must be an object.")
                continue
            has_absolute_window = bool(value.get("startAt") and value.get("endAt"))
            has_recurring_window = bool(value.get("startTime") and value.get("endTime"))
            if not has_absolute_window and not has_recurring_window:
                warnings.append(
                    f"Operation {index + 1} creates a constraint that retrieval can store, "
                    "but the scheduler cannot enforce yet."
                )
        else:
            errors.append(f"Unsupported PreferencePatch operation: {op or 'unknown'}.")

    status = "invalid" if errors else "warning" if warnings else "valid"
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "operationCount": len(operations),
    }


def simulate_preference_patch(preference_patch: dict[str, Any]) -> dict[str, Any]:
    operations = preference_patch.get("operations")
    if not isinstance(operations, list):
        operations = []
    create_preferences = 0
    create_constraints = 0
    preview: list[str] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if operation.get("op") == "create_preference":
            create_preferences += 1
            preference = operation.get("preference") if isinstance(operation.get("preference"), dict) else {}
            description = _clean_text(str(preference.get("description") or "Save preference."))
            preview.append(description)
        elif operation.get("op") == "create_constraint":
            create_constraints += 1
            constraint = operation.get("constraint") if isinstance(operation.get("constraint"), dict) else {}
            description = _clean_text(str(constraint.get("description") or "Save scheduling constraint."))
            if description not in preview:
                preview.append(description)
    return {
        "operationCount": len(operations),
        "createPreferences": create_preferences,
        "createConstraints": create_constraints,
        "preview": preview,
    }


def build_preference_memory_proposal(
    *,
    prompt: str,
    today_value: date | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    proposal_prompt = contextualize_preference_prompt(prompt, conversation_history)
    preference_patch = build_preference_patch_from_prompt(proposal_prompt)
    if preference_patch is None:
        return None
    memory_intent = _memory_intent_from_prompt(proposal_prompt)
    validation = validate_preference_patch(preference_patch)
    simulation = simulate_preference_patch(preference_patch)
    proposal_id = f"preference-proposal-{uuid4()}"
    preview = [
        item for item in simulation["preview"] if isinstance(item, str) and item.strip()
    ]
    validation_warnings = [
        item
        for item in [
            *validation["warnings"],
            *(
                preference_patch.get("assumptionWarnings")
                if isinstance(preference_patch.get("assumptionWarnings"), list)
                else []
            ),
        ]
        if isinstance(item, str) and item.strip()
    ]
    return {
        "proposal_id": proposal_id,
        "proposal_kind": PREFERENCE_PROPOSAL_KIND,
        "operation": "save_memory",
        "target_node_title": "Preferences",
        "summary": "Review this preference before I save it.",
        "rationale": "This will guide future schedules.",
        "preview": preview,
        "memory_intent": memory_intent,
        "validation_warnings": validation_warnings,
        "preference_patch": preference_patch,
        "preference_patch_validation": validation,
        "preference_patch_simulation": simulation,
        "preference_patch_status": validation["status"],
        "today": today_value.isoformat() if today_value is not None else None,
    }


def _tool_argument_text(value: Any) -> str:
    return _clean_text(str(value or ""))


def _tool_argument_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tool_argument_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _structured_inter_task_gap_minutes(value: dict[str, Any]) -> int | None:
    for key in (
        "interTaskGapMinutes",
        "inter_task_gap_minutes",
        "preferredInterTaskGapMinutes",
        "preferred_inter_task_gap_minutes",
        "gapMinutes",
        "gap_minutes",
        "bufferMinutes",
        "buffer_minutes",
    ):
        raw_minutes = value.get(key)
        if isinstance(raw_minutes, bool) or not isinstance(raw_minutes, (int, float)):
            continue
        minutes = int(raw_minutes)
        if float(raw_minutes) == float(minutes) and 0 <= minutes <= 1440:
            return minutes
    return None


def _structured_gap_duration_threshold(
    value: dict[str, Any],
    *,
    description: str,
) -> int | None:
    for key in (
        "minimumTaskDurationMinutes",
        "minimum_task_duration_minutes",
        "taskDurationAtLeastMinutes",
        "task_duration_at_least_minutes",
    ):
        raw_minutes = value.get(key)
        if isinstance(raw_minutes, bool) or not isinstance(raw_minutes, (int, float)):
            continue
        minutes = int(raw_minutes)
        if float(raw_minutes) == float(minutes) and 1 <= minutes <= 1440:
            return minutes
    for pattern in (
        r"\b(?:tasks?\s+)?(?:shorter|less)\s+than\s+(\d+)\s*(?:minutes?|mins?)\b",
        r"\b(\d+)\s*(?:minutes?|mins?)\s*(?:or\s+longer|and\s+longer|or\s+more|\+)\b",
    ):
        match = re.search(pattern, description, re.IGNORECASE)
        if match is not None:
            return max(1, min(1440, int(match.group(1))))
    return None


def build_preference_memory_proposal_from_tool_arguments(
    arguments: dict[str, Any],
    *,
    original_user_request: str | None = None,
    today_value: date | None = None,
) -> dict[str, Any]:
    """Compile model-owned preference-memory tool arguments into a reviewable proposal."""

    if not isinstance(arguments, dict):
        arguments = {}
    preference_payload = _tool_argument_object(arguments.get("preference"))
    description = _tool_argument_text(
        preference_payload.get("description")
        or arguments.get("description")
        or arguments.get("preference_text")
        or arguments.get("preferenceText")
    )
    if not description:
        return {
            "compiled": False,
            "proposal_kind": PREFERENCE_PROPOSAL_KIND,
            "error": "propose_preference_memory requires a preference description.",
        }

    source_text = _tool_argument_text(
        arguments.get("source_text")
        or arguments.get("sourceText")
        or original_user_request
        or description
    )
    patch = _base_patch(source_text)
    temp_id = "preference-1"
    preference_value = _tool_argument_object(preference_payload.get("value"))
    if not preference_value:
        preference_value = {
            "preferenceText": description,
            "normalizedText": description.lower(),
        }
    preference_value.setdefault("sourceText", source_text)
    preference_value.setdefault("extractionKind", "agent_tool")
    semantic_text = " ".join(
        item for item in (description, source_text) if item
    )
    gap_minutes = _structured_inter_task_gap_minutes(preference_value)
    if gap_minutes is None:
        gap_minutes = inter_task_gap_minutes_from_text(semantic_text)
    if gap_minutes is not None:
        preference_value.setdefault("interTaskGapMinutes", gap_minutes)
    gap_duration_threshold = _structured_gap_duration_threshold(
        preference_value,
        description=description,
    )
    if gap_minutes is not None and gap_duration_threshold is not None:
        preference_value.setdefault(
            "minimumTaskDurationMinutes",
            gap_duration_threshold,
        )
        preference_value.setdefault("shorterTaskGapMinutes", 0)
    elif gap_minutes is not None and text_explicitly_applies_to_all_tasks(
        semantic_text
    ):
        # This marker distinguishes an explicit blanket rule from an
        # amount-only update whose existing applicability should be preserved.
        preference_value["appliesToAllTasks"] = True
    enforcement = _tool_argument_text(
        preference_payload.get("enforcement") or arguments.get("enforcement")
    ).lower()
    requested_strength = _tool_argument_text(
        preference_payload.get("strength") or arguments.get("strength")
    ) or "medium"
    if enforcement == "hard_minimum":
        requested_strength = "hard"
    constraints = [
        item
        for item in _tool_argument_list(arguments.get("constraints"))
        if isinstance(item, dict)
    ]
    if gap_minutes is not None and requested_strength == "hard" and not any(
        str(item.get("constraintType") or item.get("constraint_type") or "").strip().lower()
        in {"inter_task_gap", "minimum_inter_task_gap", "task_buffer"}
        for item in constraints
    ):
        constraints.append(
            {
                "description": description,
                "constraintType": "inter_task_gap",
                "targetType": _tool_argument_text(
                    preference_payload.get("scopeType")
                    or preference_payload.get("scope_type")
                    or arguments.get("scopeType")
                    or arguments.get("scope_type")
                )
                or "global",
                "targetId": preference_payload.get(
                    "scopeId",
                    preference_payload.get("scope_id", arguments.get("scopeId")),
                ),
                "hard": True,
                "penaltyWeight": 1.0,
                "value": {"interTaskGapMinutes": gap_minutes},
            }
        )
    if not constraints:
        preference_value.setdefault("constraintEligible", False)
    else:
        preference_value["constraintEligible"] = True

    patch["operations"] = [
        _preference_operation(
            temp_id=temp_id,
            description=description,
            value=preference_value,
            strength=requested_strength,
            preference_type=(
                "inter_task_gap"
                if gap_minutes is not None
                else _tool_argument_text(
                    preference_payload.get("preferenceType")
                    or preference_payload.get("preference_type")
                    or arguments.get("preference_type")
                    or arguments.get("preferenceType")
                )
                or "schedule_style"
            ),
            domain=(
                "scheduling"
                if gap_minutes is not None
                else _tool_argument_text(
                    preference_payload.get("domain") or arguments.get("domain")
                )
                or _domain_from_preference_text(description)
            ),
        )
    ]
    patch_preference = patch["operations"][0]["preference"]
    patch_preference["scopeType"] = _tool_argument_text(
        preference_payload.get("scopeType")
        or preference_payload.get("scope_type")
        or arguments.get("scope_type")
        or arguments.get("scopeType")
    ) or "global"
    patch_preference["scopeId"] = (
        preference_payload.get("scopeId")
        if "scopeId" in preference_payload
        else preference_payload.get("scope_id", arguments.get("scope_id", arguments.get("scopeId")))
    )
    confidence = preference_payload.get("confidence", arguments.get("confidence"))
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        patch_preference["confidence"] = max(0.0, min(1.0, float(confidence)))

    for raw_constraint in constraints:
        constraint_value = _tool_argument_object(raw_constraint.get("value"))
        if not constraint_value:
            continue
        constraint_description = _tool_argument_text(raw_constraint.get("description")) or description
        constraint_operation = _constraint_operation(
            description=constraint_description,
            value=constraint_value,
            source_preference_temp_id=temp_id,
        )
        constraint = constraint_operation["constraint"]
        constraint["constraintType"] = _tool_argument_text(
            raw_constraint.get("constraintType")
            or raw_constraint.get("constraint_type")
        ) or "forbidden_window"
        constraint["targetType"] = _tool_argument_text(
            raw_constraint.get("targetType")
            or raw_constraint.get("target_type")
        ) or "global"
        constraint["targetId"] = raw_constraint.get("targetId", raw_constraint.get("target_id"))
        if isinstance(raw_constraint.get("hard"), bool):
            constraint["hard"] = raw_constraint["hard"]
        penalty_weight = raw_constraint.get("penaltyWeight", raw_constraint.get("penalty_weight"))
        if isinstance(penalty_weight, (int, float)) and not isinstance(penalty_weight, bool):
            constraint["penaltyWeight"] = float(penalty_weight)
        constraint_confidence = raw_constraint.get("confidence")
        if isinstance(constraint_confidence, (int, float)) and not isinstance(constraint_confidence, bool):
            constraint["confidence"] = max(0.0, min(1.0, float(constraint_confidence)))
        patch["operations"].append(constraint_operation)

    validation = validate_preference_patch(patch)
    simulation = simulate_preference_patch(patch)
    validation_warnings = [
        item
        for item in [
            *validation["warnings"],
            *[
                str(item)
                for item in _tool_argument_list(arguments.get("validation_warnings"))
                if str(item).strip()
            ],
            *[
                str(item)
                for item in _tool_argument_list(arguments.get("assumption_warnings"))
                if str(item).strip()
            ],
        ]
        if isinstance(item, str) and item.strip()
    ]
    memory_intent = _tool_argument_text(arguments.get("memory_intent")) or "explicit_request"
    proposal = {
        "proposal_id": f"preference-proposal-{uuid4()}",
        "proposal_kind": PREFERENCE_PROPOSAL_KIND,
        "operation": "save_memory",
        "target_node_title": "Preferences",
        "summary": "Review this preference before I save it.",
        "rationale": _tool_argument_text(arguments.get("rationale"))
        or "This will guide future schedules.",
        "preview": [
            item for item in simulation["preview"] if isinstance(item, str) and item.strip()
        ],
        "memory_intent": memory_intent,
        "validation_warnings": validation_warnings,
        "preference_patch": patch,
        "preference_patch_validation": validation,
        "preference_patch_simulation": simulation,
        "preference_patch_status": validation["status"],
        "today": today_value.isoformat() if today_value is not None else None,
    }
    return {
        "compiled": validation["status"] != "invalid",
        "proposal_kind": PREFERENCE_PROPOSAL_KIND,
        PREFERENCE_RESPONSE_KEY: proposal,
        "preference_memory_proposal": proposal,
        "validation": validation,
        "simulation": simulation,
        "error": "; ".join(validation["errors"]) if validation["status"] == "invalid" else None,
    }


def build_preference_management_proposal_from_tool_arguments(
    arguments: dict[str, Any],
    *,
    original_user_request: str | None = None,
    today_value: date | None = None,
) -> dict[str, Any]:
    """Compile exact saved-preference controls into an approval-gated proposal."""

    actions = arguments.get("actions") if isinstance(arguments, dict) else None
    if not isinstance(actions, list) or not actions:
        return {
            "compiled": False,
            "proposal_kind": PREFERENCE_PROPOSAL_KIND,
            "error": "Preference management requires at least one exact action.",
        }
    if len(actions) > 100:
        return {
            "compiled": False,
            "proposal_kind": PREFERENCE_PROPOSAL_KIND,
            "error": "Preference management is limited to 100 actions per review.",
        }
    operations: list[dict[str, Any]] = []
    preview: list[str] = []
    labels = {
        "disable": ("retire_preference", "Disable"),
        "restore": ("restore_preference", "Restore"),
        "delete": ("delete_preference", "Permanently delete"),
    }
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, dict):
            return {
                "compiled": False,
                "proposal_kind": PREFERENCE_PROPOSAL_KIND,
                "error": f"Preference action {index + 1} must be an object.",
            }
        action = str(raw_action.get("action") or "").strip().lower()
        preference_id = str(
            raw_action.get("preference_id")
            or raw_action.get("preferenceId")
            or ""
        ).strip().removeprefix("preference:")
        revision = raw_action.get("revision")
        description = str(raw_action.get("description") or "").strip()
        if action not in labels:
            return {
                "compiled": False,
                "proposal_kind": PREFERENCE_PROPOSAL_KIND,
                "error": f"Preference action {index + 1} has an unsupported action.",
            }
        if (
            not preference_id
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            return {
                "compiled": False,
                "proposal_kind": PREFERENCE_PROPOSAL_KIND,
                "error": (
                    f"Preference action {index + 1} requires the exact id and "
                    "revision returned by list_remembered_preferences."
                ),
            }
        operation_kind, label = labels[action]
        replacement = (
            raw_action.get("replacement")
            if isinstance(raw_action.get("replacement"), dict)
            else None
        )
        preference_payload = {
            "id": preference_id,
            "revision": revision,
            "description": description or None,
        }
        if replacement is not None:
            preference_payload["replacement"] = {
                "id": str(replacement.get("id") or "").strip(),
                "revision": replacement.get("revision"),
                "description": str(replacement.get("description") or "").strip()
                or None,
            }
        reason_code = str(raw_action.get("reason_code") or "").strip()
        if reason_code:
            preference_payload["reasonCode"] = reason_code
        reason = str(raw_action.get("reason") or "").strip()
        if reason:
            preference_payload["reason"] = reason
        operations.append(
            {
                "op": operation_kind,
                "preference": preference_payload,
            }
        )
        preview.append(
            (
                f"{label} older rule: {description or f'saved preference {preference_id}'}; "
                f"keep newer rule: {replacement.get('description') or replacement.get('id')}."
                if replacement is not None
                else f"{label}: {description or f'saved preference {preference_id}'}."
            )
        )

    source_text = str(
        arguments.get("source_text")
        or arguments.get("sourceText")
        or original_user_request
        or ""
    ).strip()
    patch = {
        "version": 1,
        "sourceText": source_text,
        "operations": operations,
    }
    proposal = {
        "proposal_id": f"preference-proposal-{uuid4()}",
        "proposal_kind": PREFERENCE_PROPOSAL_KIND,
        "operation": "manage_memory",
        "target_node_title": "Preferences",
        "summary": str(arguments.get("summary") or "").strip()
        or "Review these remembered preference changes before applying them.",
        "rationale": str(arguments.get("rationale") or "").strip()
        or "These changes affect what future planning remembers and enforces.",
        "preview": preview,
        "memory_intent": "explicit_management_request",
        "validation_warnings": [],
        "preference_patch": patch,
        "preference_patch_validation": {"status": "valid", "errors": [], "warnings": []},
        "preference_patch_simulation": {"status": "valid", "preview": preview},
        "preference_patch_status": "valid",
        "today": today_value.isoformat() if today_value is not None else None,
    }
    return {
        "compiled": True,
        "proposal_kind": PREFERENCE_PROPOSAL_KIND,
        PREFERENCE_RESPONSE_KEY: proposal,
        "preference_memory_proposal": proposal,
        "validation": proposal["preference_patch_validation"],
        "simulation": proposal["preference_patch_simulation"],
        "error": None,
    }


def _payload_value(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def _create_preference(session: Session, payload: dict[str, Any]) -> UserPreference:
    preference = UserPreference(
        user_id=str(_payload_value(payload, "userId", "user_id", default=DEFAULT_USER_ID)),
        domain=str(_payload_value(payload, "domain", default="scheduling")),
        preference_type=str(_payload_value(payload, "preferenceType", "preference_type", default="general")),
        scope_type=str(_payload_value(payload, "scopeType", "scope_type", default="global")),
        scope_id=_payload_value(payload, "scopeId", "scope_id"),
        strength=str(_payload_value(payload, "strength", default="medium")),
        source=str(_payload_value(payload, "source", default="user_explicit")),
        confidence=float(_payload_value(payload, "confidence", default=1.0)),
        active=bool(_payload_value(payload, "active", default=True)),
        description=_payload_value(payload, "description"),
        value_json=_json_dumps(_payload_value(payload, "value", default={})),
    )
    session.add(preference)
    session.flush()
    return preference


def _create_constraint(
    session: Session,
    payload: dict[str, Any],
    *,
    source_preference_id: str | None,
) -> SchedulingConstraint:
    constraint = SchedulingConstraint(
        user_id=str(_payload_value(payload, "userId", "user_id", default=DEFAULT_USER_ID)),
        constraint_type=str(_payload_value(payload, "constraintType", "constraint_type", default="forbidden_window")),
        target_type=str(_payload_value(payload, "targetType", "target_type", default="global")),
        target_id=_payload_value(payload, "targetId", "target_id"),
        hard=bool(_payload_value(payload, "hard", default=True)),
        penalty_weight=float(_payload_value(payload, "penaltyWeight", "penalty_weight", default=1.0)),
        source_preference_id=source_preference_id,
        source=str(_payload_value(payload, "source", default="user_explicit")),
        confidence=float(_payload_value(payload, "confidence", default=1.0)),
        active=bool(_payload_value(payload, "active", default=True)),
        description=_payload_value(payload, "description"),
        value_json=_json_dumps(_payload_value(payload, "value", default={})),
    )
    session.add(constraint)
    session.flush()
    return constraint


def reconcile_superseded_gap_preferences(
    session: Session,
    *,
    user_id: str | None = None,
) -> dict[str, int]:
    """Canonicalize gap memory and deactivate older conflicting same-scope rules."""

    statement = (
        select(UserPreference)
        .where(UserPreference.active == True)
        .order_by(UserPreference.updated_at.desc(), UserPreference.created_at.desc())
    )
    if user_id is not None:
        statement = statement.where(UserPreference.user_id == user_id)
    active_preferences = list(session.exec(statement).all())
    candidates_by_scope: dict[
        tuple[str, str, str | None],
        list[tuple[UserPreference, dict[str, Any], int]],
    ] = {}
    for preference in active_preferences:
        value = decode_preference_value(preference.value_json)
        if not isinstance(value, dict):
            continue
        gap_minutes = _structured_inter_task_gap_minutes(value)
        if gap_minutes is None:
            continue
        key = (preference.user_id, preference.scope_type, preference.scope_id)
        candidates_by_scope.setdefault(key, []).append((preference, value, gap_minutes))

    canonicalized = 0
    deactivated_preferences = 0
    deactivated_constraints = 0
    preferences_by_id = {preference.id: preference for preference in active_preferences}
    latest_by_scope: dict[tuple[str, str, str | None], UserPreference] = {}
    for key, candidates in candidates_by_scope.items():
        latest, latest_value, gap_minutes = candidates[0]
        latest_by_scope[key] = latest
        changed = False
        if latest.preference_type.strip().lower() != "inter_task_gap":
            latest.preference_type = "inter_task_gap"
            changed = True
        if latest_value.get("interTaskGapMinutes") != gap_minutes:
            latest_value["interTaskGapMinutes"] = gap_minutes
            changed = True
        threshold = _structured_gap_duration_threshold(
            latest_value,
            description=str(latest.description or ""),
        )
        if threshold is not None:
            if latest_value.get("minimumTaskDurationMinutes") != threshold:
                latest_value["minimumTaskDurationMinutes"] = threshold
                changed = True
            if "shorterTaskGapMinutes" not in latest_value:
                latest_value["shorterTaskGapMinutes"] = 0
                changed = True
        if changed:
            latest.value_json = _json_dumps(latest_value)
            session.add(latest)
            canonicalized += 1

        for older, older_value, _older_gap in candidates[1:]:
            if not preference_rules_overlap(
                "inter_task_gap",
                latest_value,
                older_value,
            ):
                continue
            older.active = False
            session.add(older)
            deactivated_preferences += 1

    constraint_statement = (
        select(SchedulingConstraint)
        .where(SchedulingConstraint.active == True)
    )
    if user_id is not None:
        constraint_statement = constraint_statement.where(
            SchedulingConstraint.user_id == user_id
        )
    for constraint in session.exec(constraint_statement).all():
        if constraint.constraint_type.strip().lower() not in {
            "inter_task_gap",
            "minimum_inter_task_gap",
            "task_buffer",
        }:
            continue
        key = (constraint.user_id, constraint.target_type, constraint.target_id)
        latest = latest_by_scope.get(key)
        if latest is None:
            continue
        linked_preference = (
            preferences_by_id.get(constraint.source_preference_id)
            if constraint.source_preference_id
            else None
        )
        if linked_preference is not None and linked_preference.active:
            continue
        if (
            constraint.source_preference_id == latest.id
            or (
                constraint.source_preference_id is None
                and constraint.updated_at > latest.updated_at
            )
        ):
            continue
        constraint.active = False
        session.add(constraint)
        deactivated_constraints += 1

    return {
        "canonicalizedPreferences": canonicalized,
        "deactivatedPreferences": deactivated_preferences,
        "deactivatedConstraints": deactivated_constraints,
    }


def _deactivate_superseded_gap_memory(
    session: Session,
    *,
    preference: UserPreference,
) -> None:
    reconcile_superseded_gap_preferences(session, user_id=preference.user_id)


def _add_change_log(
    session: Session,
    *,
    user_id: str,
    record_type: str,
    record_id: str,
    after: dict[str, Any],
    actor: str,
    reason: str,
) -> None:
    session.add(
        PreferenceChangeLog(
            user_id=user_id,
            record_type=record_type,
            record_id=record_id,
            change_type="create",
            actor=actor,
            reason=reason,
            before_json=None,
            after_json=_json_dumps(after),
        )
    )


def apply_preference_patch(
    session: Session,
    *,
    preference_patch: dict[str, Any],
    actor: str = "user_approved",
) -> dict[str, Any]:
    validation = validate_preference_patch(preference_patch)
    if validation["status"] == "invalid":
        raise ValueError("PreferencePatch is invalid and cannot be applied.")

    operations = preference_patch.get("operations")
    if not isinstance(operations, list):
        operations = []

    temp_preference_ids: dict[str, str] = {}
    created_preference_ids: list[str] = []
    created_constraint_ids: list[str] = []
    reason = str(preference_patch.get("intentSummary") or "Approved preference memory update.")

    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if operation.get("op") == "create_preference":
            payload = operation.get("preference") if isinstance(operation.get("preference"), dict) else {}
            preference = _create_preference(session, payload)
            _deactivate_superseded_gap_memory(session, preference=preference)
            temp_id = str(operation.get("tempId") or operation.get("temp_id") or "")
            if temp_id:
                temp_preference_ids[temp_id] = preference.id
            created_preference_ids.append(preference.id)
            _add_change_log(
                session,
                user_id=preference.user_id,
                record_type="user_preference",
                record_id=preference.id,
                after={
                    "id": preference.id,
                    "domain": preference.domain,
                    "preferenceType": preference.preference_type,
                    "scopeType": preference.scope_type,
                    "scopeId": preference.scope_id,
                    "strength": preference.strength,
                    "description": preference.description,
                    "value": json.loads(preference.value_json),
                },
                actor=actor,
                reason=reason,
            )
        elif operation.get("op") == "create_constraint":
            payload = operation.get("constraint") if isinstance(operation.get("constraint"), dict) else {}
            source_temp_id = str(
                operation.get("sourcePreferenceTempId")
                or operation.get("source_preference_temp_id")
                or ""
            )
            source_preference_id = temp_preference_ids.get(source_temp_id)
            constraint = _create_constraint(
                session,
                payload,
                source_preference_id=source_preference_id,
            )
            created_constraint_ids.append(constraint.id)
            _add_change_log(
                session,
                user_id=constraint.user_id,
                record_type="scheduling_constraint",
                record_id=constraint.id,
                after={
                    "id": constraint.id,
                    "constraintType": constraint.constraint_type,
                    "targetType": constraint.target_type,
                    "targetId": constraint.target_id,
                    "hard": constraint.hard,
                    "sourcePreferenceId": constraint.source_preference_id,
                    "description": constraint.description,
                    "value": json.loads(constraint.value_json),
                },
                actor=actor,
                reason=reason,
            )

    session.commit()
    return {
        "preferencePatchValidation": validation,
        "createdPreferenceIds": created_preference_ids,
        "createdConstraintIds": created_constraint_ids,
    }
