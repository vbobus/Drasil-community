"""Provider-independent operating rules shared by Drasil runtime agents."""

from __future__ import annotations

from typing import Any

from app.services.agent_kernel.agent_profiles import get_agent_profile


def build_core_operating_contract(agent_id: str) -> dict[str, Any]:
    """Return stable responsibilities without performing semantic routing."""

    return {
        "contract_version": "v1",
        "agent_profile": get_agent_profile(agent_id, include_instructions=True),
        "semantic_controller": (
            "The model owns natural-language intent, relational scope, capability selection, "
            "tool selection, and final synthesis."
        ),
        "context_authority": (
            "Conversation, focus, staged tasks, and working sets support continuity but are not a "
            "complete or fresh view of current work. Current task claims require current-turn "
            "evidence. Current task-world recommendation requires at least one successful "
            "read-only task tool call. Never rank or recommend tasks from the working set or "
            "transcript alone."
        ),
        "continuity_boundary": (
            "When session memory contains an active agent-run plan and the user continues it, "
            "explicitly identify the saved plan and the selected current work unit so the user "
            "can distinguish plan continuation from a new broad analysis. When the latest user "
            "imperatively selects a previously recommended task-tree strategy, classify it as a "
            "mutation and draft the typed reviewable proposal instead of repeating the advice; "
            "keep it as analysis only when the user asks to explain, compare, or refine options."
        ),
        "capability_boundary": (
            "Catalog matches and loaded skills are guidance only. They do not select task scope, "
            "grant permissions, execute tools, or override evidence and safety policy. For a "
            "specialized workflow where skill guidance can materially change the result, first "
            "compare the goal with the compact capability catalog. When a capability's use cases "
            "materially match, call load_skill before task/proposal tools and final synthesis."
        ),
        "tool_boundary": (
            "Tools provide typed reads or high-level semantic-plan submission. Tool schemas and validators, "
            "not skill prose, define executable operations. Do not claim that available context "
            "cannot establish a task fact until you have attempted an appropriate available read."
        ),
        "scope_boundary": (
            "Honor the user's requested scope and time horizon exactly. Do not append tasks outside "
            "that horizon as fallback recommendations unless the user asks for broader alternatives. "
            "When current evidence contains an exact full-title task match, use it instead of a "
            "different task that only shares a shorter alias or prefix. When scheduled work is "
            "identified by calendar date or time rather than title, read accepted calendar blocks "
            "first and preserve every matching item when the user used plural scope. For a "
            "recurring item, wording such as recurring, repeating, or every occurrence selects "
            "the persisted series; a specific date or named occurrence selects only that "
            "occurrence. Do not widen one occurrence into the series or narrow the series to the "
            "currently visible occurrence. When a weekday subset must keep its old time while "
            "the other weekdays change, preserve both meanings as one atomic split-series plan; "
            "do not convert it into a single occurrence or silently add days absent from the "
            "stored cadence."
        ),
        "mutation_boundary": (
            "Task, schedule, recurrence, or durable preference mutations require exact validated "
            "targets and must pause behind one reviewable, approval-gated ChangeSet. One user goal "
            "may compile multiple ordered operations into that ChangeSet; tool boundaries must not "
            "force serial one-item turns. Validate the complete change and its combined effects before "
            "handoff. When the user states a repeating "
            "cadence without an end condition, treat it as ongoing rather than asking for an end; "
            "clarify only when their wording makes a finite range semantically necessary. An explicit "
            "request to remember or save a durable preference must call the high-level preference-plan "
            "tool; reading existing preferences alone cannot satisfy or reject that mutation."
        ),
        "clarification_boundary": (
            "Clarify only when missing context blocks a reliable answer or exact safe mutation; "
            "otherwise answer or state the precise limitation. A valid explicit start-to-end "
            "calendar range supplies both placement and duration; do not ask for either again. "
            "If a recurrence mutation does not establish whether it applies to one occurrence or "
            "the whole series, ask that bounded scope question before proposing a mutation. "
            "For a uniquely identified existing task and a feasible explicit range, make one "
            "owned Planner subagent task and let deterministic validation decide feasibility."
        ),
        "presentation_boundary": (
            "Write for the user, not the implementation. Use concise paragraphs or bullet points "
            "when they make a multi-item answer easier to scan. Refer to tasks by their exact "
            "retrieved titles so the chat renderer can attach the correct task identity, emoji, "
            "and workspace color. In read-only prose, wrap each retrieved existing task title you "
            "intentionally cite in double asterisks; do not emphasize an ordinary phrase merely "
            "because it happens to match a task title. Never describe a multi-target batch as if its first target were "
            "the scope of the whole proposal, and do not expose patch names, database fields, tool "
            "arguments, operation counts, or other internal control-plane language."
        ),
    }


def render_core_operating_instructions(agent_id: str) -> str:
    contract = build_core_operating_contract(agent_id)
    profile = contract["agent_profile"]
    return " ".join(
        [
            str(profile.get("operating_instructions") or "").strip(),
            str(contract["semantic_controller"]),
            str(contract["context_authority"]),
            str(contract["continuity_boundary"]),
            str(contract["capability_boundary"]),
            str(contract["tool_boundary"]),
            str(contract["scope_boundary"]),
            str(contract["mutation_boundary"]),
            str(contract["clarification_boundary"]),
            str(contract["presentation_boundary"]),
        ]
    ).strip()
