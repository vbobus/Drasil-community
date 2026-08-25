"""Canonical encoding for durable preference values.

The agent contract uses tagged values for transport. Persistence and scheduling
use the logical object so typed fields remain queryable and enforceable.
"""

from __future__ import annotations

import json
import re
from typing import Any

INTER_TASK_GAP_VALUE_KEYS = (
    "interTaskGapMinutes",
    "inter_task_gap_minutes",
    "preferredInterTaskGapMinutes",
    "preferred_inter_task_gap_minutes",
    "gapMinutes",
    "gap_minutes",
    "bufferMinutes",
    "buffer_minutes",
)

_INTER_TASK_GAP_TEXT_PATTERNS = (
    re.compile(
        r"\b(?P<minutes>\d{1,4})\s*[- ]?\s*(?:minutes?|mins?|min)\s+"
        r"(?:gap|buffer|break|transition)(?:\s+time)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:gap|buffer|break|transition)(?:\s+time)?(?:\s+of)?\s+"
        r"(?P<minutes>\d{1,4})\s*[- ]?\s*(?:minutes?|mins?|min)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<minutes>\d{1,4})\s*[- ]?\s*(?:minutes?|mins?|min)\s+"
        r"(?:in\s+)?between\s+(?:scheduled\s+)?tasks?\b",
        re.IGNORECASE,
    ),
)
_INTER_TASK_GAP_SUBJECT_RE = re.compile(
    r"\b(?:gap|buffer|break|transition)(?:\s+time)?\s+"
    r"(?:in\s+)?between\s+(?:scheduled\s+)?tasks?\b"
    r"|\bbetween\s+(?:scheduled\s+)?tasks?\b",
    re.IGNORECASE,
)
_ALL_TASKS_APPLICABILITY_RE = re.compile(
    r"\b(?:between\s+)?(?:all|every|any)\s+(?:scheduled\s+)?tasks?\b"
    r"|\bregardless\s+of\s+(?:the\s+)?(?:task(?:'s)?\s+)?duration\b"
    r"|\bno\s+matter\s+(?:the\s+)?(?:task(?:'s)?\s+)?duration\b",
    re.IGNORECASE,
)

_NUMBER_WORD_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_WORD_PATTERN = "|".join(
    sorted(_NUMBER_WORD_VALUES, key=len, reverse=True)
)
_INTER_TASK_GAP_WORD_PATTERNS = (
    re.compile(
        rf"\b(?P<minutes>(?:{_NUMBER_WORD_PATTERN})(?:[- ](?:{_NUMBER_WORD_PATTERN}))?)\s+"
        r"(?:minutes?|mins?|min)\s+(?:of\s+)?(?:gap|buffer|break|transition)(?:\s+time)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:gap|buffer|break|transition)(?:\s+time)?(?:\s+of)?\s+"
        rf"(?P<minutes>(?:{_NUMBER_WORD_PATTERN})(?:[- ](?:{_NUMBER_WORD_PATTERN}))?)\s+"
        r"(?:minutes?|mins?|min)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?P<minutes>(?:{_NUMBER_WORD_PATTERN})(?:[- ](?:{_NUMBER_WORD_PATTERN}))?)\s+"
        r"(?:minutes?|mins?|min)\s+(?:in\s+)?between\s+(?:scheduled\s+)?tasks?\b",
        re.IGNORECASE,
    ),
)
_SEMANTIC_TEXT_IGNORED_KEYS = {
    "extractionKind",
    "normalizedText",
    "preferenceText",
    "sourceText",
}
_SEMANTIC_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "i",
    "id",
    "like",
    "me",
    "my",
    "of",
    "please",
    "prefer",
    "preference",
    "the",
    "to",
    "want",
    "would",
}
_SEMANTIC_SYNONYMS = {
    "answers": "response",
    "brief": "concise",
    "buffer": "gap",
    "break": "gap",
    "focused": "focus",
    "mornings": "morning",
    "responses": "response",
    "tasks": "task",
    "transition": "gap",
}


def inter_task_gap_minutes_from_text(text: str) -> int | None:
    """Extract a gap duration only when the number is tied to gap semantics."""

    for pattern in _INTER_TASK_GAP_TEXT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        minutes = int(match.group("minutes"))
        if 0 <= minutes <= 1440:
            return minutes
    for pattern in _INTER_TASK_GAP_WORD_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        minutes = _number_words_value(match.group("minutes"))
        if minutes is not None and 0 <= minutes <= 1440:
            return minutes
    return None


def _number_words_value(value: str) -> int | None:
    parts = re.split(r"[-\s]+", value.strip().casefold())
    if not parts or any(part not in _NUMBER_WORD_VALUES for part in parts):
        return None
    numbers = [_NUMBER_WORD_VALUES[part] for part in parts]
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2 and numbers[0] >= 20 and numbers[1] < 10:
        return numbers[0] + numbers[1]
    return None


def text_mentions_inter_task_gap(text: str) -> bool:
    """Return whether prose addresses the semantic setting between-task gap."""

    return (
        inter_task_gap_minutes_from_text(text) is not None
        or _INTER_TASK_GAP_SUBJECT_RE.search(text) is not None
    )


def text_explicitly_applies_to_all_tasks(text: str) -> bool:
    """Return whether prose explicitly removes duration-based applicability."""

    return _ALL_TASKS_APPLICABILITY_RE.search(text) is not None


def decode_preference_value(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError:
            return {}
    else:
        parsed = value
    if not isinstance(parsed, dict):
        return {}

    kind = parsed.get("kind")
    if kind == "scalar":
        scalar = parsed.get("value")
        if isinstance(scalar, str):
            try:
                decoded = json.loads(scalar)
            except json.JSONDecodeError:
                return {"preferenceText": scalar}
            return decoded if isinstance(decoded, dict) else {"value": decoded}
        if isinstance(scalar, dict):
            return scalar
        return {"value": scalar}
    if kind == "string_set":
        return {"values": list(parsed.get("values") or [])}
    if kind == "time_window":
        return {
            "startTime": parsed.get("startLocal", parsed.get("start_local")),
            "endTime": parsed.get("endLocal", parsed.get("end_local")),
            "timezone": parsed.get("timezone"),
        }
    return parsed


def encode_preference_value(value: Any) -> str:
    payload = (
        value.model_dump(mode="json", by_alias=True)
        if hasattr(value, "model_dump")
        else value
    )
    logical = decode_preference_value(payload)
    return json.dumps(
        logical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def preference_family(preference_key: str, value: Any) -> str:
    logical = decode_preference_value(value if isinstance(value, (str, dict)) else None)
    if any(key in logical for key in INTER_TASK_GAP_VALUE_KEYS):
        return "inter_task_gap"
    normalized = preference_key.strip().lower()
    if normalized.startswith("constraint."):
        normalized = normalized.removeprefix("constraint.")
    return {
        "minimum_inter_task_gap": "inter_task_gap",
        "task_buffer": "inter_task_gap",
    }.get(normalized, normalized)


def preference_subject(
    preference_key: str,
    value: Any,
    *,
    description: str = "",
    domain: str = "",
) -> str:
    """Return the semantic setting controlled by a preference.

    ``preference_type`` alone is not an identity: many unrelated preferences
    use ``schedule_style``.  This subject is deliberately based on structured
    values first, then bounded domain/text cues for user-authored settings that
    do not yet have a dedicated typed field.
    """

    logical = decode_preference_value(
        value if isinstance(value, (str, dict)) else None
    )
    family = preference_family(preference_key, logical)
    if family == "inter_task_gap":
        return family

    normalized_domain = domain.strip().lower()
    text = " ".join(
        [
            description,
            str(logical.get("preferenceText") or ""),
            str(logical.get("normalizedText") or ""),
            str(logical.get("sourceText") or ""),
        ]
    ).casefold()
    rule = str(logical.get("rule") or "").strip().casefold()
    if text_mentions_inter_task_gap(text):
        return "inter_task_gap"
    if (
        normalized_domain == "sleep"
        and re.search(r"\bwake(?:\s|-)?up\b|\bwake\s+time\b", text)
    ):
        return "wake_time"
    if (
        logical.get("preferredEarliestStartTime") is not None
        or rule == "do_not_schedule_before"
        or "schedule before" in text
    ):
        return "earliest_schedule_time"
    if (
        logical.get("preferredLatestEndTime") is not None
        or rule == "do_not_schedule_after"
        or "schedule after" in text
    ):
        return "latest_schedule_time"
    if normalized_domain == "deep_work" or "deep work" in text:
        return "deep_work_window"

    normalized_key = preference_key.strip().lower() or "general"
    if normalized_key.startswith("constraint."):
        normalized_key = normalized_key.removeprefix("constraint.")
    return f"{normalized_domain or 'general'}:{normalized_key}"


def _text_time(text: str) -> str | None:
    match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    marker = str(match.group(3) or "").casefold()
    if not 0 <= minute <= 59:
        return None
    if marker:
        if not 1 <= hour <= 12:
            return None
        if marker.startswith("p") and hour < 12:
            hour += 12
        elif marker.startswith("a") and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _semantic_tokens(text: str) -> tuple[str, ...]:
    normalized = text.casefold().replace("-", " ")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    canonical = {
        _SEMANTIC_SYNONYMS.get(token, token)
        for token in tokens
        if token not in _SEMANTIC_STOP_WORDS
    }
    return tuple(sorted(canonical))


def _integer_value(value: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            parsed = int(raw)
            if float(raw) == float(parsed) and 0 <= parsed <= 1440:
                return parsed
    return None


def preference_semantic_payload(
    preference_key: str,
    value: Any,
    *,
    description: str = "",
    domain: str = "",
) -> dict[str, Any]:
    """Build a stable semantic representation for duplicate comparison."""

    logical = decode_preference_value(
        value if isinstance(value, (str, dict)) else None
    )
    subject = preference_subject(
        preference_key,
        logical,
        description=description,
        domain=domain,
    )
    text = " ".join(
        item
        for item in (
            description,
            str(logical.get("preferenceText") or ""),
            str(logical.get("normalizedText") or ""),
            str(logical.get("sourceText") or ""),
        )
        if item
    )
    payload: dict[str, Any] = {"subject": subject}
    if subject == "inter_task_gap":
        structured_gap = _integer_value(logical, INTER_TASK_GAP_VALUE_KEYS)
        payload.update(
            {
                "gap_minutes": (
                    structured_gap
                    if structured_gap is not None
                    else inter_task_gap_minutes_from_text(text)
                ),
                "minimum_task_duration_minutes": _integer_value(
                    logical,
                    (
                        "minimumTaskDurationMinutes",
                        "minimum_task_duration_minutes",
                        "taskDurationAtLeastMinutes",
                        "task_duration_at_least_minutes",
                    ),
                ),
                "maximum_task_duration_minutes": _integer_value(
                    logical,
                    (
                        "maximumTaskDurationMinutes",
                        "maximum_task_duration_minutes",
                        "taskDurationAtMostMinutes",
                        "task_duration_at_most_minutes",
                    ),
                ),
                "shorter_task_gap_minutes": _integer_value(
                    logical,
                    ("shorterTaskGapMinutes", "shorter_task_gap_minutes"),
                ),
            }
        )
        return payload
    if subject in {"wake_time", "earliest_schedule_time", "latest_schedule_time"}:
        payload["time"] = (
            logical.get("preferredEarliestStartTime")
            or logical.get("preferredLatestEndTime")
            or logical.get("startTime")
            or logical.get("endTime")
            or _text_time(text)
        )
        return payload
    if subject == "deep_work_window":
        lowered = text.casefold()
        windows: set[str] = set()
        if "morning" in lowered or "before lunch" in lowered:
            windows.add("morning")
        if "afternoon" in lowered or "after lunch" in lowered:
            windows.add("afternoon")
        if "evening" in lowered:
            windows.add("evening")
        if "late night" in lowered or "nighttime" in lowered:
            windows.add("late_night")
        payload["windows"] = sorted(windows)
        start = logical.get("startTime")
        end = logical.get("endTime")
        if start is not None or end is not None:
            payload["time_window"] = [start, end]
        return payload

    structured = {
        key: child
        for key, child in logical.items()
        if key not in _SEMANTIC_TEXT_IGNORED_KEYS
    }
    if structured:
        payload["value"] = structured
    payload["tokens"] = _semantic_tokens(text)
    return payload


def preference_semantic_similarity(
    left_key: str,
    left_value: Any,
    *,
    left_description: str = "",
    left_domain: str = "",
    right_key: str,
    right_value: Any,
    right_description: str = "",
    right_domain: str = "",
) -> float:
    """Return a conservative semantic-equivalence score from zero to one."""

    left = preference_semantic_payload(
        left_key,
        left_value,
        description=left_description,
        domain=left_domain,
    )
    right = preference_semantic_payload(
        right_key,
        right_value,
        description=right_description,
        domain=right_domain,
    )
    if left.get("subject") != right.get("subject"):
        return 0.0
    if left == right:
        return 1.0

    subject = left.get("subject")
    if subject in {
        "inter_task_gap",
        "wake_time",
        "earliest_schedule_time",
        "latest_schedule_time",
        "deep_work_window",
    }:
        left_values = {key: value for key, value in left.items() if key != "subject"}
        right_values = {key: value for key, value in right.items() if key != "subject"}
        comparable_keys = {
            key
            for key in left_values.keys() & right_values.keys()
            if left_values[key] is not None and right_values[key] is not None
        }
        if comparable_keys and all(
            left_values[key] == right_values[key] for key in comparable_keys
        ):
            missing_left = {
                key for key, value in left_values.items() if value not in (None, [], ())
            } - comparable_keys
            missing_right = {
                key for key, value in right_values.items() if value not in (None, [], ())
            } - comparable_keys
            return 0.9 if not missing_left and not missing_right else 0.84
        return 0.0

    left_tokens = set(left.get("tokens") or ())
    right_tokens = set(right.get("tokens") or ())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return overlap / union if union else 0.0


def enforcement_constraint_type(
    preference_key: str,
    value: Any,
) -> str | None:
    """Return the enforceable constraint represented by a hard preference."""
    logical = decode_preference_value(
        value if isinstance(value, (str, dict)) else None
    )
    explicit = str(
        logical.get("enforcementConstraintType")
        or logical.get("enforcement_constraint_type")
        or logical.get("constraintType")
        or logical.get("constraint_type")
        or ""
    ).strip()
    if explicit:
        return explicit[:64]
    if preference_family(preference_key, logical) == "inter_task_gap":
        return "inter_task_gap"
    if logical.get("startTime") is not None and logical.get("endTime") is not None:
        return "forbidden_window"
    return None


def preference_rules_overlap(family: str, left: Any, right: Any) -> bool:
    """Return whether two same-scope typed rules can govern the same task."""

    if family != "inter_task_gap":
        return True

    def duration_range(value: Any) -> tuple[int, int]:
        logical = decode_preference_value(
            value if isinstance(value, (str, dict)) else None
        )
        minimum = 0
        maximum = 1440
        for key in (
            "minimumTaskDurationMinutes",
            "minimum_task_duration_minutes",
            "taskDurationAtLeastMinutes",
            "task_duration_at_least_minutes",
        ):
            raw = logical.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                minimum = max(0, min(1440, int(raw)))
                break
        for key in (
            "maximumTaskDurationMinutes",
            "maximum_task_duration_minutes",
            "taskDurationAtMostMinutes",
            "task_duration_at_most_minutes",
        ):
            raw = logical.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                maximum = max(0, min(1440, int(raw)))
                break
        return minimum, maximum

    left_minimum, left_maximum = duration_range(left)
    right_minimum, right_maximum = duration_range(right)
    return max(left_minimum, right_minimum) <= min(left_maximum, right_maximum)
