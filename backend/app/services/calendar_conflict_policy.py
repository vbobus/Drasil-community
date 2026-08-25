"""Shared, deterministic policy for calendar collisions.

The model may explain why a task matters, but accepted calendar mutations must
be decided from typed task/block evidence.  This module keeps one-off and
recurring scheduling aligned on what can move, what must stay, and when the
user needs to choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable

from app.models.calendar_block import CalendarBlock
from app.models.node import StrategyNode


PRIORITY_SCORES = {"low": 1, "medium": 2, "high": 3}
SHORT_EXACT_ACTION_MAX_MINUTES = 15

LIKELY_FIXED_TITLE_RE = re.compile(
    r"\b(?:appointment|class|course|exam|interview|meeting|stand[ -]?up|"
    r"call|doctor|dentist|therapy|flight|train|lunch|dinner|break|pickup|drop[ -]?off)\b",
    re.IGNORECASE,
)
EXPLICIT_CLOCK_TIME_RE = re.compile(
    r"\b(?:[01]?\d(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)|[01]?\d:[0-5]\d)\b",
    re.IGNORECASE,
)
INCOMING_IMPORTANCE_RE = re.compile(
    r"\b(?:important|urgent|critical|top priority|highest priority|more important|higher priority|"
    r"must happen|must do|cannot miss|can't miss)\b",
    re.IGNORECASE,
)
INCOMING_LOW_IMPORTANCE_RE = re.compile(
    r"\b(?:not (?:that )?important|this (?:task|event) is (?:low|lower) priority|"
    r"optional|this (?:task|event|time) can move)\b",
    re.IGNORECASE,
)
INCOMING_FIXED_TIME_RE = re.compile(
    r"\b(?:strict|fixed|non[- ]?negotiable|cannot change (?:this|the) time|"
    r"can't change (?:this|the) time|this time cannot move|this time can't move)\b",
    re.IGNORECASE,
)
EXPLICIT_DISPLACE_RE = re.compile(
    r"\b(?:make way|move the (?:other|existing|current|lower[- ]priority)|"
    r"reschedule the (?:other|existing|current|lower[- ]priority)|"
    r"bump the (?:other|existing|current|lower[- ]priority))\b",
    re.IGNORECASE,
)
EXPLICIT_PRESERVE_RE = re.compile(
    r"\b(?:do not|don't|cannot|can't)\s+(?:move|change|reschedule)\s+"
    r"(?:the\s+)?(?:other|existing|current)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CalendarBlocker:
    block: CalendarBlock
    task: StrategyNode
    fixed_reason: str | None

    @property
    def priority(self) -> str:
        return normalized_priority(self.task.priority)

    @property
    def score(self) -> int:
        return priority_score(self.task.priority)


@dataclass(frozen=True)
class CollisionDecision:
    action: str
    incoming_priority: str
    blockers: tuple[CalendarBlocker, ...]
    reason: str


def normalized_priority(value: str | None) -> str:
    priority = str(value or "medium").strip().lower()
    return priority if priority in PRIORITY_SCORES else "medium"


def priority_score(value: str | None) -> int:
    return PRIORITY_SCORES[normalized_priority(value)]


def _deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def effective_incoming_priority(task: StrategyNode, prompt: str) -> tuple[str, int]:
    priority = normalized_priority(task.priority)
    score = priority_score(priority)
    if INCOMING_LOW_IMPORTANCE_RE.search(prompt):
        return "low", PRIORITY_SCORES["low"]
    if INCOMING_IMPORTANCE_RE.search(prompt):
        return "high", max(score, PRIORITY_SCORES["high"])
    return priority, score


def prompt_has_explicit_clock_time(prompt: str) -> bool:
    return bool(EXPLICIT_CLOCK_TIME_RE.search(str(prompt or "")))


def fixed_reason_for_incoming(
    task: StrategyNode,
    *,
    prompt: str,
    exact_time: bool,
) -> str | None:
    if task.is_pinned:
        return "pinned"
    if INCOMING_FIXED_TIME_RE.search(prompt):
        return "explicitly fixed time"
    if exact_time and LIKELY_FIXED_TITLE_RE.search(str(task.title or "")):
        return "exact-time commitment"
    return None


def fixed_reason_for_block(block: CalendarBlock, task: StrategyNode) -> str | None:
    if block.is_protected:
        return "protected"
    if block.is_pinned or task.is_pinned:
        return "pinned"
    if LIKELY_FIXED_TITLE_RE.search(str(task.title or "")):
        return "likely fixed commitment"
    return None


def overlapping_blockers(
    blocks: Iterable[CalendarBlock],
    *,
    tasks_by_id: dict[int, StrategyNode],
    start_at: datetime,
    end_at: datetime,
) -> list[CalendarBlocker]:
    blockers: list[CalendarBlocker] = []
    for block in blocks:
        if not (start_at < block.end_at and block.start_at < end_at):
            continue
        task = tasks_by_id.get(int(block.task_id))
        if task is None:
            continue
        blockers.append(
            CalendarBlocker(
                block=block,
                task=task,
                fixed_reason=fixed_reason_for_block(block, task),
            )
        )
    return blockers


def decide_calendar_collision(
    *,
    incoming_task: StrategyNode,
    blockers: list[CalendarBlocker],
    prompt: str,
    exact_time: bool,
) -> CollisionDecision:
    incoming_priority, incoming_score = effective_incoming_priority(incoming_task, prompt)
    incoming_fixed_reason = fixed_reason_for_incoming(
        incoming_task,
        prompt=prompt,
        exact_time=exact_time,
    )
    ordered = tuple(
        sorted(
            blockers,
            key=lambda item: (item.block.start_at, item.block.id or 0),
        )
    )
    if not ordered:
        return CollisionDecision("none", incoming_priority, ordered, "No calendar blocker overlaps.")
    hard_fixed = [
        item for item in ordered if item.fixed_reason in {"protected", "pinned"}
    ]
    if hard_fixed:
        labels = ", ".join(item.task.title for item in hard_fixed or ordered)
        if incoming_fixed_reason:
            return CollisionDecision(
                "clarify",
                incoming_priority,
                ordered,
                f"{labels} and {incoming_task.title} are both fixed commitments.",
            )
        return CollisionDecision(
            "preserve_existing",
            incoming_priority,
            ordered,
            f"{labels} is protected or pinned.",
        )
    if EXPLICIT_PRESERVE_RE.search(prompt):
        labels = ", ".join(item.task.title for item in ordered)
        return CollisionDecision(
            "preserve_existing",
            incoming_priority,
            ordered,
            f"{labels} is explicitly preserved.",
        )
    highest_blocker_score = max(item.score for item in ordered)
    if EXPLICIT_DISPLACE_RE.search(prompt):
        return CollisionDecision(
            "displace_existing",
            incoming_priority,
            ordered,
            "The user explicitly chose the incoming time and every blocker is flexible.",
        )
    likely_fixed = [item for item in ordered if item.fixed_reason]
    if incoming_fixed_reason and likely_fixed:
        labels = ", ".join(item.task.title for item in likely_fixed)
        return CollisionDecision(
            "clarify",
            incoming_priority,
            ordered,
            f"{incoming_task.title} and {labels} both look like fixed commitments.",
        )
    if incoming_fixed_reason:
        return CollisionDecision(
            "displace_existing",
            incoming_priority,
            ordered,
            (
                f"{incoming_task.title} is an {incoming_fixed_reason} and every "
                "existing blocker is flexible."
            ),
        )
    if likely_fixed:
        labels = ", ".join(item.task.title for item in likely_fixed)
        return CollisionDecision(
            "preserve_existing",
            incoming_priority,
            ordered,
            f"{labels} looks like a fixed commitment.",
        )
    incoming_duration = incoming_task.time_estimate_minutes
    if (
        exact_time
        and isinstance(incoming_duration, int)
        and not isinstance(incoming_duration, bool)
        and 0 < incoming_duration <= SHORT_EXACT_ACTION_MAX_MINUTES
        and incoming_score >= highest_blocker_score
    ):
        return CollisionDecision(
            "displace_existing",
            incoming_priority,
            ordered,
            (
                f"{incoming_task.title} is a short exact-time action and every "
                "existing blocker is flexible, so a minimal pushback preserves all work."
            ),
        )
    if (
        incoming_score > highest_blocker_score
        or (
            exact_time
            and INCOMING_FIXED_TIME_RE.search(prompt)
            and incoming_score >= highest_blocker_score
        )
    ):
        return CollisionDecision(
            "displace_existing",
            incoming_priority,
            ordered,
            "The incoming task has higher priority and every blocker is flexible.",
        )
    if incoming_score < highest_blocker_score:
        return CollisionDecision(
            "preserve_existing",
            incoming_priority,
            ordered,
            "An existing task has higher priority.",
        )
    incoming_deadline = _deadline(incoming_task.deadline_at)
    blocker_deadlines = [
        deadline
        for item in ordered
        if item.score == highest_blocker_score
        and (deadline := _deadline(item.task.deadline_at)) is not None
    ]
    earliest_blocker_deadline = min(blocker_deadlines) if blocker_deadlines else None
    if incoming_score == highest_blocker_score:
        if incoming_deadline is not None and (
            earliest_blocker_deadline is None
            or incoming_deadline < earliest_blocker_deadline
        ):
            return CollisionDecision(
                "displace_existing",
                incoming_priority,
                ordered,
                "The incoming task has the earlier deadline and every blocker is flexible.",
            )
        if earliest_blocker_deadline is not None and (
            incoming_deadline is None
            or earliest_blocker_deadline < incoming_deadline
        ):
            return CollisionDecision(
                "preserve_existing",
                incoming_priority,
                ordered,
                "An existing task has the earlier deadline.",
            )
    if exact_time:
        return CollisionDecision(
            "clarify",
            incoming_priority,
            ordered,
            "The tasks have equal priority and the requested time is exact.",
        )
    return CollisionDecision(
        "preserve_existing",
        incoming_priority,
        ordered,
        "The least disruptive option is to keep accepted blocks and move the new task.",
    )


def blocker_payload(blocker: CalendarBlocker) -> dict[str, object]:
    block = blocker.block
    return {
        "blockId": block.id,
        "taskId": blocker.task.id,
        "taskTitle": blocker.task.title,
        "priority": blocker.priority,
        "startAt": block.start_at.isoformat(timespec="minutes"),
        "endAt": block.end_at.isoformat(timespec="minutes"),
        "isRecurring": bool(block.is_recurring),
        "fixedReason": blocker.fixed_reason,
    }
