"""Cheap metadata intent extraction for the Strategist harness.

This module is deliberately deterministic and narrow: it extracts explicit
field edits from prompts so the kernel can build bounded TaskPatch proposals
without importing the legacy Strategist orchestrator.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.models.node import StrategyNode
from app.services.strategist_proposals import build_metadata_patch_proposal


def parse_prompt_date(prompt: str, today_value: date) -> str | None:
    lowered = prompt.strip().lower()
    if lowered in {"today"}:
        return today_value.isoformat()
    if lowered in {"tomorrow"}:
        return (today_value + timedelta(days=1)).isoformat()
    if re.search(r"\bend\s+of\s+next\s+week\b", lowered):
        days_until_this_sunday = (6 - today_value.weekday()) % 7
        return (today_value + timedelta(days=days_until_this_sunday + 7)).isoformat()
    if lowered in {"next week"}:
        return (today_value + timedelta(days=7)).isoformat()
    weekday_match = re.search(
        r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lowered,
    )
    if weekday_match:
        weekday_lookup = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        target_weekday = weekday_lookup[weekday_match.group(1)]
        days_until_target = (target_weekday - today_value.weekday()) % 7
        if days_until_target == 0:
            days_until_target = 7
        return (today_value + timedelta(days=days_until_target)).isoformat()
    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", prompt)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1)).isoformat()
        except ValueError:
            return None
    text_match = re.search(
        r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b",
        prompt,
        re.IGNORECASE,
    )
    if not text_match:
        return None
    month_lookup = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    year = int(text_match.group(3) or today_value.year)
    try:
        return date(
            year,
            month_lookup[text_match.group(1).lower()],
            int(text_match.group(2)),
        ).isoformat()
    except ValueError:
        return None


def parse_prompt_estimate_minutes(prompt: str) -> int | None:
    hour_match = re.search(
        r"\b(\d{1,3}(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)\b",
        prompt,
        re.IGNORECASE,
    )
    minute_match = re.search(r"\b(\d{1,3})\s*(m|min|mins|minute|minutes)\b", prompt, re.IGNORECASE)
    total = 0
    if hour_match:
        total += round(float(hour_match.group(1)) * 60)
    if minute_match:
        total += int(minute_match.group(1))
    return total if total > 0 else None


def parse_prompt_priority(prompt: str) -> str | None:
    if re.search(r"\b(?:high\s+priority|priority\s+high|urgent|red)\b", prompt, re.IGNORECASE):
        return "high"
    if re.search(r"\b(?:medium\s+priority|priority\s+medium|orange)\b", prompt, re.IGNORECASE):
        return "medium"
    if re.search(r"\b(?:low\s+priority|priority\s+low|green)\b", prompt, re.IGNORECASE):
        return "low"
    return None


def parse_prompt_status(prompt: str) -> str | None:
    lowered = prompt.lower()
    normalized_words = set(re.findall(r"[a-z]+", lowered))
    if any(token in lowered for token in ("mark", "set", "change", "update", "complete", "completed", "done", "doen")):
        if re.search(
            r"\b(?:not|isn't|isnt|wasn't|wasnt)\s+(?:yet\s+|really\s+)?(?:done|doen|complete|completed|finished)\b"
            r"|\b(?:incomplete|unfinished)\b",
            lowered,
            re.IGNORECASE,
        ):
            return "todo"
        if any(token in lowered for token in (" todo", "to do", " back to todo", " reopen", "not done")):
            return "todo"
        if any(token in lowered for token in (" in progress", "in-progress", " active", " started", " start ")):
            return "in-progress"
        if (
            any(token in lowered for token in (" as done", " to done", " done", " doen", " complete", " completed", " finished"))
            or normalized_words.intersection({"done", "doen", "don", "dnoe", "finished", "complete", "completed"})
        ):
            return "done"
    return None


def build_metadata_updates_from_prompt(prompt: str, today_value: date) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    deadline_at = parse_prompt_date(prompt, today_value)
    if deadline_at is not None and ("deadline" in prompt.lower() or "due" in prompt.lower()):
        updates["deadline_at"] = deadline_at

    priority = parse_prompt_priority(prompt)
    if priority is not None:
        updates["priority"] = priority

    status = parse_prompt_status(prompt)
    if status is not None:
        updates["status"] = status

    estimate_minutes = parse_prompt_estimate_minutes(prompt)
    if estimate_minutes is not None and re.search(
        r"\b(?:estimate|how\s+long|duration|allocate|make\s+it|"
        r"set(?:\s+the)?\s+duration|change(?:\s+it)?\s+to)\b",
        prompt,
        re.IGNORECASE,
    ):
        updates["time_estimate_minutes"] = estimate_minutes

    if re.search(r"\bunpin\b", prompt, re.IGNORECASE):
        updates["is_pinned"] = False
    elif re.search(r"\bpin\b", prompt, re.IGNORECASE):
        updates["is_pinned"] = True

    rename_match = re.search(r"\b(?:rename|retitle)\b.*?\bto\b\s+['\"]?(.+?)['\"]?$", prompt, re.IGNORECASE)
    if rename_match:
        renamed = rename_match.group(1).strip().strip(".")
        if renamed:
            updates["title"] = renamed
    return updates


def build_direct_metadata_proposal(
    *,
    prompt: str,
    target_node: StrategyNode | None,
    today_value: date,
    evidence_bundle: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if target_node is None or target_node.id is None:
        return None

    updates = build_metadata_updates_from_prompt(prompt, today_value)
    if not updates:
        return None

    return build_metadata_patch_proposal(
        target_node=target_node,
        updates=updates,
        rationale="This patch comes from the explicit metadata change you asked for.",
        evidence_bundle=evidence_bundle,
        summary=f"Update {target_node.title}",
    )


__all__ = [
    "build_direct_metadata_proposal",
    "build_metadata_updates_from_prompt",
    "parse_prompt_date",
    "parse_prompt_estimate_minutes",
    "parse_prompt_priority",
    "parse_prompt_status",
]
