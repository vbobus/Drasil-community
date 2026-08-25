"""
App-owned Strategist intent scoring.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.node import StrategyNode

DELETE_RE = re.compile(r"\b(delete|remove)\b", re.IGNORECASE)
COMPARE_RE = re.compile(
    r"\b(compare|versus|vs\.?|tighter deadline|which one|which has|earlier due|sooner due|factor in)\b",
    re.IGNORECASE,
)
WORKLOAD_RE = re.compile(
    r"\b(workload|busy|this week|next week|this month|next month|upcoming|due soon|deadlines?|stale|sitting around|next \d+ days?)\b",
    re.IGNORECASE,
)
RECOMMENDATION_ACTION_PATTERN = (
    r"(?:rec+om+m?end(?:ation)?s?|suggest(?:ions?)?|focus(?:\s+on)?|"
    r"prioriti[sz]e|what\s+should\s+i\s+do|what\s+would\s+you(?:\s+do)?|"
    r"what\s+should\s+come\s+first|where\s+should\s+i\s+start|what\s+next|"
    r"highest\s+leverage|high\s+leverage|biggest\s+growth|move\s+my\s+life\s+forward|"
    r"what\s+should\s+i\s+(?:ignore|pause|defer)|ignore\s+for\s+now|pause\s+for\s+now|"
    r"urgent(?:ly)?|important|5x)"
)
PLANNING_SCOPE_PATTERN = (
    r"(?:today|tomorrow|this\s+week|next\s+week|week|this\s+month|next\s+month|"
    r"next\s+\d+\s+(?:days?|weeks?|months?)|quarter|next\s+quarter|upcoming|"
    r"workload|task\s+tree|tasks?|life|goals?|career)"
)
BROAD_RECOMMENDATION_RE = re.compile(
    rf"\b{RECOMMENDATION_ACTION_PATTERN}\b.*\b{PLANNING_SCOPE_PATTERN}\b"
    rf"|\b{PLANNING_SCOPE_PATTERN}\b.*\b{RECOMMENDATION_ACTION_PATTERN}\b",
    re.IGNORECASE,
)
BROAD_GRAPH_RE = re.compile(
    r"\b(?:all\s+tasks?|task\s+tree|tree|hierarchy|hierarchical|ascii|workload|everything|overall|across\s+categories)\b",
    re.IGNORECASE,
)
BROAD_REVIEW_RE = re.compile(
    r"\b(?:can|could|should|show|tell|review|look|map|draw|print|sketch|represent|visuali[sz]e|organize|organise|structure|restructure|restructured|rework|clean\s+up|cleanup|tidy)\b",
    re.IGNORECASE,
)
METADATA_GAP_RE = re.compile(
    r"\b(?:all\s+tasks?|tasks?|everything|workload)\b.*\b(?:missing\s+(?:info|information|metadata|fields?)|incomplete\s+(?:info|metadata)|fill\s+.*missing)\b"
    r"|\b(?:missing\s+(?:info|information|metadata|fields?)|incomplete\s+(?:info|metadata)|fill\s+.*missing)\b.*\b(?:all\s+tasks?|tasks?|everything|urgent|urgently)\b",
    re.IGNORECASE,
)
LIST_RE = re.compile(r"\b(show|list|what are|any other|which|display|sort|order|rank)\b", re.IGNORECASE)
BROAD_TASK_LIST_RE = re.compile(
    r"\b(?:show|list|find|which|what\s+are|display|sort|order|rank)\b.*\b(?:all\s+)?(?:tasks|taks|items?|nodes?)\b"
    r"|\b(?:all\s+)?(?:tasks|taks|items?|nodes?)\b.*\b(?:by|with|without|missing|present|priority|deadline|estimate|status|due|finish|finished|done|sort|order|rank)\b",
    re.IGNORECASE,
)
METADATA_RE = re.compile(
    r"\b(update|set|change|rename|retitle|pin|unpin|mark|complete|completed|done|pass|passed|start|reopen|deadline|priority|estimate|notes?|tags?|status)\b",
    re.IGNORECASE,
)
METADATA_REFINEMENT_RE = re.compile(
    r"\b(?:tighten|improve|clean\s+up|cleanup|polish|refine|fix|fill|complete|review|audit)\b.*\bmetadata\b"
    r"|\bmetadata\b.*\b(?:tighten|improve|clean\s+up|cleanup|polish|refine|fix|fill|complete|review|audit)\b",
    re.IGNORECASE,
)
BRANCH_RE = re.compile(
    r"\b(break down|breakdown|split|decompose|subtasks?|children|concrete steps?|concrete next actions?|restructure|organize|chaotic|branch)\b",
    re.IGNORECASE,
)
CREATE_RE = re.compile(
    r"\b(add|create|make)\b.*\b(new|task|subtask|wildcard|branch)\b|\bnew\s+(task|subtask|wildcard|branch)\b",
    re.IGNORECASE,
)
FACT_RE = re.compile(
    r"\b(when|due|deadline|priority|status|estimate|how long|details?|notes?|what about|finish|finished|blocking)\b",
    re.IGNORECASE,
)
CORRECTION_RE = re.compile(
    r"\b(not that one|i mean|actually|instead)\b|^no[, ]",
    re.IGNORECASE,
)
CONFIRM_RE = re.compile(r"\b(approve|approved|yes|confirm|do it|apply it)\b", re.IGNORECASE)
OPEN_STRATEGY_RE = re.compile(
    r"\b(strategize|starategize|strategy session|brainstorm|think through|talk through|freeform)\b",
    re.IGNORECASE,
)
CONVERSATION_DIRECTIVE_RE = re.compile(
    r"\b(?:normal|regular|freeform|just(?:\s+a)?|plain)\s+conversation\b"
    r"|\bconversation\s+with\s+you\b"
    r"|\bnot\s+(?:a\s+)?(?:task\s+lookup|planning\s+action|tree\s+lookup)\b"
    r"|\b(?:chat|talk)\s+with\s+you\b"
    r"|\bdo\s+you\s+understand\b",
    re.IGNORECASE,
)
GENERAL_ADVICE_RE = re.compile(
    r"\b(?:what|how|why|should|could|can|would)\b.*\b(?:life|meaning|purpose|values|philosophy|advice)\b"
    r"|\b(?:best|good)\s+life\s+strateg(?:y|ies)\b"
    r"|\bstrateg(?:y|ies)\s+in\s+life\b",
    re.IGNORECASE,
)
EXTERNAL_PLANNING_RE = re.compile(
    r"\b(?:what|how|where|when|which|should|could|can|would)\b.*\b(?:prepare|prep|pack|bring|plan|research|visit|travel|trip|vacation|itinerary|ferry|flight|hotel|restaurant|reservation|weather|visa|passport|ticket|packing)\b"
    r"|\b(?:prepare|prep|pack|bring|plan|research)\b.*\b(?:for|before|about)\b.*\b(?:trip|travel|visit|vacation|itinerary|ferry|flight|hotel|restaurant|reservation|weather|visa|passport|ticket|packing)\b"
    r"|\b(?:prepare|prep|pack|bring|plan|research)\b.*\b(?:for|before|about)\b.*\b(?:going|visiting|travelling|traveling)\b"
    r"|\b(?:trip|travel|vacation|itinerary|ferry|flight|hotel|restaurant|reservation|weather|visa|passport|ticket|packing)\b.*\b(?:prepare|prep|pack|bring|plan|research|should|need)\b",
    re.IGNORECASE,
)
TASK_DATA_RE = re.compile(
    r"\b(?:task|tasks|node|nodes|tree|branch|metadata|deadline|due|priority|status|estimate|subtask|subtasks|workload|staged|calendar|application|resume|portfolio|project|writeup|homework|assignment|exam|category|categories)\b",
    re.IGNORECASE,
)

FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "deadline_at": re.compile(r"\b(due|deadline|when('| i)?s)\b", re.IGNORECASE),
    "priority": re.compile(r"\b(priority|urgent|red|orange|green|high|medium|low)\b", re.IGNORECASE),
    "time_estimate_minutes": re.compile(r"\b(estimate|how long|hours?|hrs?|minutes?|mins?)\b", re.IGNORECASE),
    "notes": re.compile(r"\b(notes?|description|details?)\b", re.IGNORECASE),
    "tags": re.compile(r"\b(tags?)\b", re.IGNORECASE),
    "is_pinned": re.compile(r"\b(pin|pinned|unpin)\b", re.IGNORECASE),
    "title": re.compile(r"\b(rename|retitle|title)\b", re.IGNORECASE),
    "status": re.compile(r"\b(status|done|complete|completed|finish|finished|pass|passed|in progress|todo|start|reopen)\b", re.IGNORECASE),
}

INTENT_TO_MODE = {
    "delete": "action",
    "edit_metadata": "metadata",
    "strategic_planning": "branch",
    "create_task": "branch",
    "retrieve_fact": "review",
    "compare_entities": "review",
    "workload_review": "review",
    "strategic_synthesis": "review",
    "graph_inspection": "review",
    "metadata_gap_review": "review",
    "list_query": "review",
    "open_strategy_chat": "review",
    "confirm_proposal": "review",
    "unknown": "review",
}


def is_broad_graph_review_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.strip().lower().split())
    if not normalized:
        return False
    return bool(BROAD_GRAPH_RE.search(normalized)) and (
        bool(BROAD_REVIEW_RE.search(normalized))
        or normalized.endswith("?")
        or bool(re.search(r"\b(what|why|how|where|which)\b", normalized))
    )


def is_broad_recommendation_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.strip().lower().split())
    if not normalized:
        return False
    return bool(BROAD_RECOMMENDATION_RE.search(normalized))


def is_task_list_query_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.strip().lower().split())
    if not normalized:
        return False
    return bool(BROAD_TASK_LIST_RE.search(normalized))


def is_external_planning_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.strip().lower().split())
    if not normalized:
        return False
    return bool(EXTERNAL_PLANNING_RE.search(normalized)) and not bool(TASK_DATA_RE.search(normalized))


def _recent_conversation_directive(
    conversation_history: list[dict[str, str]] | None,
) -> bool:
    if not conversation_history:
        return False
    for message in conversation_history[-4:]:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and CONVERSATION_DIRECTIVE_RE.search(content):
            return True
    return False


def is_open_conversation_prompt(
    prompt: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> bool:
    normalized = " ".join(prompt.strip().lower().split())
    if not normalized:
        return False
    if CONVERSATION_DIRECTIVE_RE.search(normalized):
        return True
    if GENERAL_ADVICE_RE.search(normalized) and not TASK_DATA_RE.search(normalized):
        return True
    if is_external_planning_prompt(normalized):
        return True
    if _recent_conversation_directive(conversation_history) and not TASK_DATA_RE.search(normalized):
        return True
    return False


def detect_requested_fields(prompt: str) -> list[str]:
    requested: list[str] = []
    for field_name, pattern in FIELD_PATTERNS.items():
        if pattern.search(prompt):
            requested.append(field_name)
    return requested


def score_intent_candidates(
    *,
    prompt: str,
    focus_node: StrategyNode | None,
) -> list[dict[str, Any]]:
    normalized = " ".join(prompt.strip().lower().split())
    candidates: list[dict[str, Any]] = []
    is_task_list_query = is_task_list_query_prompt(normalized)

    def add(intent: str, score: float, reasons: list[str]) -> None:
        candidates.append(
            {
                "intent": intent,
                "mode": INTENT_TO_MODE.get(intent, "review"),
                "score": round(min(1.0, max(0.0, score)), 4),
                "reasons": reasons,
            }
        )

    if CONFIRM_RE.search(normalized):
        add("confirm_proposal", 0.96, ["confirmation keyword"])
    if is_open_conversation_prompt(normalized):
        reasons = (
            ["external-world planning or research frame"]
            if is_external_planning_prompt(normalized)
            else ["conversation-first problem frame"]
        )
        add("open_strategy_chat", 0.95, reasons)
    if OPEN_STRATEGY_RE.search(normalized):
        add("open_strategy_chat", 0.93, ["open strategy conversation phrasing"])
    if METADATA_GAP_RE.search(normalized):
        add("metadata_gap_review", 0.95, ["broad missing-metadata audit phrasing"])
    if is_broad_graph_review_prompt(normalized):
        add("graph_inspection", 0.91, ["broad read-only graph inspection"])
    contextual_choice = bool(
        re.search(r"\b(which\s+one|which\s+of|between\s+.+\s+and\s+)\b", normalized)
    )
    broad_recommendation = (
        is_broad_recommendation_prompt(normalized)
        and not METADATA_GAP_RE.search(normalized)
        and not contextual_choice
    )
    if is_task_list_query and not broad_recommendation:
        add("list_query", 0.92, ["broad read-only task list or sort request"])
    if DELETE_RE.search(normalized):
        add("delete", 0.97, ["delete verb"])
    if CREATE_RE.search(normalized):
        add("create_task", 0.94, ["create/add task phrasing"])
    if BRANCH_RE.search(normalized):
        add("strategic_planning", 0.88, ["branch planning keyword"])
    if METADATA_REFINEMENT_RE.search(normalized):
        add("edit_metadata", 0.9, ["metadata refinement request"])
    if METADATA_RE.search(normalized) and not is_task_list_query:
        score = 0.82
        if normalized.endswith("?") and not any(
            verb in normalized for verb in ("update", "set", "change", "rename", "retitle", "pin", "unpin")
        ):
            score = 0.64
        if detect_requested_fields(prompt):
            score += 0.08
        add("edit_metadata", score, ["metadata verb or field keyword"])
    if COMPARE_RE.search(normalized):
        add("compare_entities", 0.85, ["comparison phrasing"])
    if broad_recommendation:
        add("strategic_synthesis", 0.96, ["read-only decision synthesis or high-leverage focus request"])
        add("workload_review", 0.9, ["broad recommendation or time-horizon planning phrasing"])
    if WORKLOAD_RE.search(normalized):
        workload_score = 0.9 if any(token in normalized for token in ("workload", "this week", "next week")) else 0.83
        add("workload_review", workload_score, ["workload or date-range phrasing"])
    if LIST_RE.search(normalized) and not is_task_list_query and not broad_recommendation:
        list_score = 0.86 if normalized.startswith(("what are", "show me", "list ")) else 0.74
        add("list_query", list_score, ["listing phrasing"])
    if CORRECTION_RE.search(normalized):
        add("retrieve_fact", 0.84, ["corrective follow-up phrasing"])
    if FACT_RE.search(normalized) or normalized.endswith("?"):
        add("retrieve_fact", 0.76, ["fact lookup phrasing"])

    if not candidates:
        add(
            "unknown",
            0.5 if focus_node is None else 0.58,
            ["fallback intent"],
        )

    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        prior = deduped.get(candidate["intent"])
        if prior is None or float(candidate["score"]) > float(prior["score"]):
            deduped[candidate["intent"]] = candidate

    ordered = sorted(
        deduped.values(),
        key=lambda item: (float(item["score"]), item["intent"] == "retrieve_fact"),
        reverse=True,
    )
    return ordered


def choose_mode_from_candidates(
    *,
    prompt: str,
    focus_node: StrategyNode | None,
    intent_candidates: list[dict[str, Any]],
) -> str:
    if is_broad_graph_review_prompt(prompt):
        return "review"
    if intent_candidates:
        mode = intent_candidates[0].get("mode")
        if isinstance(mode, str) and mode:
            return mode
    return "branch" if focus_node is not None and prompt.strip() else "review"
