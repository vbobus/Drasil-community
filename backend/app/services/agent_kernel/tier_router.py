"""Execution-envelope routing for the Strategist agent kernel.

Natural-language meaning and scope belong to the model.  This module only
separates typed UI actions, which already carry exact identifiers, from model
turns.  It intentionally does not inspect prompt wording.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

from app.models.category import Category
from app.models.node import StrategyNode

TierName = Literal["tier_0", "tier_1"]


@dataclass(frozen=True)
class TierBudget:
    max_model_calls: int
    max_tool_calls: int
    max_prompt_chars: int
    foreground_timeout_ms: int
    model_step_timeout_seconds: int
    loop_timeout_seconds: int

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TierRouteDecision:
    tier: TierName
    route_kind: str
    reason: str
    budget: TierBudget
    signals: dict[str, Any]
    allows_model: bool
    continuation: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "route_kind": self.route_kind,
            "reason": self.reason,
            "budget": self.budget.to_payload(),
            "signals": self.signals,
            "allows_model": self.allows_model,
            "continuation": self.continuation,
        }


TIER_BUDGETS: dict[TierName, TierBudget] = {
    "tier_0": TierBudget(
        max_model_calls=0,
        max_tool_calls=0,
        max_prompt_chars=0,
        foreground_timeout_ms=1000,
        model_step_timeout_seconds=0,
        loop_timeout_seconds=0,
    ),
    # One common, capable envelope for every natural-language turn.  These are
    # safety ceilings for the durable agent loop, not foreground HTTP wait
    # limits or an intent classification.  Keep them large enough for broad
    # workspace operations that require several evidence and proposal calls;
    # provider progress is guarded separately by the meaningful-progress idle
    # watchdog.
    "tier_1": TierBudget(
        # Covers evidence reads plus a compound proposal and bounded typed
        # argument repairs in one controller run. Stateful recurrence revisions
        # regularly need more than ten calls without indicating an idle loop;
        # the meaningful-progress watchdog remains the transport backstop.
        max_model_calls=7,
        max_tool_calls=14,
        max_prompt_chars=18000,
        foreground_timeout_ms=90000,
        model_step_timeout_seconds=1800,
        loop_timeout_seconds=7200,
    ),
}


def _has_pending_proposal(session_memory: dict[str, Any]) -> bool:
    pending = session_memory.get("pending_proposal") if isinstance(session_memory, dict) else None
    return isinstance(pending, dict)


def _has_agent_run_plan(session_memory: dict[str, Any]) -> bool:
    plan = session_memory.get("agent_run_plan") if isinstance(session_memory, dict) else None
    return isinstance(plan, dict)


def _has_unresolved_ask(session_memory: dict[str, Any]) -> bool:
    asks = session_memory.get("unresolved_asks") if isinstance(session_memory, dict) else None
    return isinstance(asks, list) and bool(asks)


def route_strategist_turn(
    *,
    prompt: str,
    ui_event_kind: str | None,
    session_memory: dict[str, Any],
    active_category: Category | None,
    focus_node: StrategyNode | None,
    staged_node_ids: list[int],
    today_value: date,
) -> TierRouteDecision:
    """Choose only between an exact typed UI action and the model controller.

    ``prompt`` and ``today_value`` remain in the signature for compatibility
    with callers, but neither is used to infer intent, scope, or complexity.
    """

    _ = (prompt, today_value)
    signals: dict[str, Any] = {
        "ui_event_kind": ui_event_kind,
        "has_active_category": active_category is not None,
        "active_category_name": active_category.name if active_category is not None else None,
        "has_focus_task": focus_node is not None,
        "focus_task_id": focus_node.id if focus_node is not None else None,
        "staged_node_count": len(staged_node_ids),
        "has_pending_proposal": _has_pending_proposal(session_memory),
        "has_agent_run_plan": _has_agent_run_plan(session_memory),
        "has_unresolved_ask": _has_unresolved_ask(session_memory),
        "semantic_routing_disabled": True,
    }

    if ui_event_kind == "proposal_action":
        return TierRouteDecision(
            tier="tier_0",
            route_kind="proposal_ui_event",
            reason="The typed proposal UI event carries an exact proposal id and action.",
            budget=TIER_BUDGETS["tier_0"],
            signals=signals,
            allows_model=False,
        )

    return TierRouteDecision(
        tier="tier_1",
        route_kind="model_owned_tool_loop",
        reason=(
            "Natural-language intent, relational scope, continuation, and tool selection "
            "are owned by the model."
        ),
        budget=TIER_BUDGETS["tier_1"],
        signals=signals,
        allows_model=True,
        continuation=ui_event_kind == "proposal_revision",
    )
