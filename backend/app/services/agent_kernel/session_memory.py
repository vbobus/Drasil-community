"""Event-log-derived continuity memory for the Strategist agent kernel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.agent_kernel.context_contract import compact_pending_proposal_for_model
from app.services.agent_kernel.plan import compact_agent_run_plan_for_model

if TYPE_CHECKING:
    from app.services.agent_kernel.contracts import GoalState


ACTIVE_REQUEST_STATUSES = {"awaiting_user_input", "in_progress", "pending_approval"}

TOOL_CONTEXT_RESULT_KEYS = (
    "result_kind",
    "observation_summary",
    "summary",
    "error",
    "tool_error",
    "planner_error",
    "model_instruction",
    "planner_mode",
    "planner_executed",
    "proposal_ready",
    "needs_user_input",
    "clarification",
    "answer_candidate",
    "evidence_coverage",
    "task",
    "tasks",
    "task_ids",
    "blocks",
    "calendar_blocks",
    "range_start",
    "range_end",
    "recurrence",
    "recurrence_series",
    "occurrences",
    "planner_result",
    "metadata_proposal",
    "branch_proposal",
    "preference_memory_proposal",
    "count",
)


def _bounded_tool_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[nested value omitted]"
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_bounded_tool_value(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for index, (raw_key, child) in enumerate(value.items()):
            if index >= 40:
                break
            bounded[str(raw_key)] = _bounded_tool_value(child, depth=depth + 1)
        return bounded
    return str(value)[:1000]


def _compact_calendar_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact: list[dict[str, Any]] = []
    for block in value[:40]:
        if not isinstance(block, dict):
            continue
        item = {
            key: block[key]
            for key in (
                "block_id",
                "task_id",
                "task_title",
                "start_at",
                "end_at",
                "is_pinned",
                "is_protected",
                "is_recurring",
                "recurrence_series_ref",
                "recurrence_occurrence_ref",
            )
            if key in block
        }
        rule = block.get("recurrence_rule")
        if isinstance(rule, dict):
            item["recurrence_rule"] = {
                key: rule[key]
                for key in (
                    "frequency",
                    "interval",
                    "days_of_week",
                    "start_date",
                    "end_date",
                    "occurrence_count",
                    "preferred_start_time",
                    "duration_minutes",
                    "timezone",
                    "strict",
                    "gap_minutes",
                )
                if key in rule
            }
        compact.append(item)
    return compact


def _compact_branch_proposal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("semantic_contract"), dict):
        return _bounded_tool_value(value)
    return compact_pending_proposal_for_model(
        {
            "proposal_id": value.get("proposal_id"),
            "proposal_kind": "branch",
            "status": "draft",
            "target_node_id": value.get("target_node_id"),
            "target_node_ids": value.get("target_node_ids", []),
            "target_node_title": value.get("target_node_title"),
            "summary": value.get("summary"),
            "rationale": value.get("rationale"),
            "payload": value,
        }
    )


def build_tool_context_entry(
    *,
    name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build model-usable tool continuity without turning it into control state."""

    normalized_name = str(name or "tool").strip() or "tool"
    compact_result = {
        key: _bounded_tool_value(result[key])
        for key in TOOL_CONTEXT_RESULT_KEYS
        if key in result and result[key] not in (None, "", [], {})
    }
    if normalized_name == "list_calendar_blocks":
        blocks = result.get("blocks", result.get("calendar_blocks"))
        compact_blocks = _compact_calendar_blocks(blocks)
        compact_result.pop("calendar_blocks", None)
        if compact_blocks:
            compact_result["blocks"] = compact_blocks
    branch_proposal = _compact_branch_proposal(result.get("branch_proposal"))
    if branch_proposal is not None:
        compact_result["branch_proposal"] = branch_proposal
    if not compact_result:
        compact_result = _bounded_tool_value(result)
    return {
        "tool_name": normalized_name,
        "arguments": _bounded_tool_value(arguments),
        "result": compact_result,
    }


def merge_tool_context(
    *contexts: list[dict[str, Any]] | None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for context in contexts:
        for entry in context or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("tool_name") or "").strip()
            arguments = entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {}
            key = f"{name}:{repr(arguments)}"
            if key in seen:
                merged = [item for item in merged if item is not entry and f"{item.get('tool_name')}:{repr(item.get('arguments') or {})}" != key]
            seen.add(key)
            merged.append(entry)
    return merged[-max(1, limit):]


def _bounded_request_prompts(value: Any, *, limit: int = 16) -> list[str]:
    prompts = [
        str(item).strip()[:4000]
        for item in value
        if isinstance(item, str) and item.strip()
    ] if isinstance(value, list) else []
    if len(prompts) <= limit:
        return prompts
    # The first turn defines the objective. Keep it plus the most recent
    # corrections rather than letting a long clarification chain evict it.
    return [prompts[0], *prompts[-(limit - 1):]]


def compact_continuation_state(value: Any) -> dict[str, Any] | None:
    """Bound durable continuation memory without dropping semantic evidence."""

    if not isinstance(value, dict):
        return None
    compact: dict[str, Any] = {
        key: value[key]
        for key in (
            "status",
            "goal_id",
            "goal_revision",
            "original_request",
            "latest_user_input",
            "target_task_ids",
            "semantic_slots",
        )
        if key in value
    }
    compact["request_prompts"] = _bounded_request_prompts(
        value.get("request_prompts", [])
    )
    clarification = value.get("clarification")
    if isinstance(clarification, dict):
        compact["clarification"] = _bounded_tool_value(clarification)
    proposal_revision = value.get("proposal_revision")
    if isinstance(proposal_revision, dict):
        compact["proposal_revision"] = _bounded_tool_value(proposal_revision)
    raw_tool_context = value.get("tool_context")
    if isinstance(raw_tool_context, list):
        compacted_tools: list[dict[str, Any]] = []
        for entry in raw_tool_context[-8:]:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result")
            compacted_tools.append(
                build_tool_context_entry(
                    name=str(entry.get("tool_name") or "tool"),
                    arguments=(
                        entry.get("arguments")
                        if isinstance(entry.get("arguments"), dict)
                        else {}
                    ),
                    result=result if isinstance(result, dict) else {},
                )
            )
        if compacted_tools:
            compact["tool_context"] = merge_tool_context(compacted_tools, limit=8)
    return compact


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event, dict) else None
    return payload if isinstance(payload, dict) else {}


def _task_item_ref(raw_item: Any) -> tuple[int | None, str | None]:
    if not isinstance(raw_item, dict):
        return None, None
    task_item = raw_item.get("task") if isinstance(raw_item.get("task"), dict) else raw_item
    if not isinstance(task_item, dict):
        return None, None
    task_id = task_item.get("task_id", task_item.get("id", task_item.get("node_id")))
    title = task_item.get("title")
    return (
        task_id if isinstance(task_id, int) else None,
        title if isinstance(title, str) and title.strip() else None,
    )


def _answer_candidate_task_refs(result: Any) -> list[tuple[int | None, str | None]]:
    if not isinstance(result, dict):
        return []
    answer_candidate = result.get("answer_candidate")
    task_ids = answer_candidate.get("task_ids") if isinstance(answer_candidate, dict) else None
    if not isinstance(task_ids, list):
        return []
    title_by_id: dict[int, str] = {}
    for key in ("tasks", "children", "siblings", "parents", "matches", "entity_matches"):
        values = result.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            task_id, title = _task_item_ref(item)
            if task_id is not None and title:
                title_by_id[task_id] = title
    snapshots = result.get("task_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            task = snapshot.get("task") if isinstance(snapshot, dict) else None
            task_id, title = _task_item_ref(task)
            if task_id is not None and title:
                title_by_id[task_id] = title
    refs: list[tuple[int | None, str | None]] = []
    for raw_task_id in task_ids:
        if isinstance(raw_task_id, int):
            refs.append((raw_task_id, title_by_id.get(raw_task_id)))
    return refs


def _latest_event(
    recent_events: list[dict[str, Any]],
    *event_types: str,
) -> dict[str, Any] | None:
    allowed = {event_type for event_type in event_types if event_type}
    for event in reversed(recent_events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type in allowed:
            return event
    return None


def _compact_working_set(working_set: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in working_set or []:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        title = str(item.get("title") or "").strip()
        if not isinstance(node_id, int) or not title:
            continue
        compact_item: dict[str, Any] = {
                "task_id": node_id,
                "title": title,
                "role": item.get("role"),
                "confidence": item.get("confidence"),
                "source": item.get("source"),
            }
        recurrence_series = item.get("recurrence_series")
        if isinstance(recurrence_series, list):
            compact_item["recurrence_series"] = [
                {
                    key: series[key]
                    for key in (
                        "series_ref",
                        "revision",
                        "frequency",
                        "interval",
                        "days_of_week",
                        "month_days",
                        "start_date",
                        "end_date",
                        "occurrence_count",
                        "preferred_start_time",
                        "duration_minutes",
                        "timezone",
                    )
                    if key in series
                }
                for series in recurrence_series[:4]
                if isinstance(series, dict)
            ]
        compacted.append(compact_item)
    return compacted[:8]


def _compact_pending_proposal(
    pending_proposal: dict[str, Any] | None,
) -> dict[str, Any] | None:
    return compact_pending_proposal_for_model(pending_proposal)


def _build_active_objective(
    *,
    recent_events: list[dict[str, Any]],
    pending_proposal: dict[str, Any] | None,
    active_category: Category | None,
) -> dict[str, Any] | None:
    if isinstance(pending_proposal, dict):
        target_title = str(pending_proposal.get("target_node_title") or "").strip()
        proposal_kind = str(pending_proposal.get("proposal_kind") or "proposal").strip()
        summary = str(pending_proposal.get("summary") or "").strip()
        if summary:
            objective = summary
        elif target_title:
            objective = f"Review pending {proposal_kind} proposal for {target_title}."
        else:
            objective = f"Review pending {proposal_kind} proposal."
        return {
            "summary": objective,
            "source": "pending_proposal",
        }

    latest_user_turn = _latest_event(recent_events, "user_turn")
    if latest_user_turn is not None:
        prompt = str(_event_payload(latest_user_turn).get("prompt") or "").strip()
        if prompt:
            return {
                "summary": prompt,
                "source": "latest_user_turn",
            }

    latest_answer = _latest_event(recent_events, "final_answer")
    if latest_answer is not None:
        summary = str(_event_payload(latest_answer).get("summary") or "").strip()
        if summary:
            return {
                "summary": summary,
                "source": "latest_final_answer",
            }

    if active_category is not None:
        return {
            "summary": f"Continue planning in {active_category.name}.",
            "source": "active_category",
        }
    return None


def _build_current_target_branch(
    *,
    recent_events: list[dict[str, Any]],
    pending_proposal: dict[str, Any] | None,
    focus_node: StrategyNode | None,
) -> dict[str, Any] | None:
    if isinstance(pending_proposal, dict):
        target_node_id = pending_proposal.get("target_node_id")
        target_title = str(pending_proposal.get("target_node_title") or "").strip()
        if isinstance(target_node_id, int) or target_title:
            return {
                "task_id": target_node_id if isinstance(target_node_id, int) else None,
                "title": target_title or None,
                "source": "pending_proposal",
            }

    latest_draft = _latest_event(recent_events, "draft_patch")
    if latest_draft is not None:
        payload = _event_payload(latest_draft)
        target_node_id = payload.get("target_task_id", payload.get("target_node_id"))
        target_node_ids = payload.get("target_task_ids", payload.get("target_node_ids"))
        if isinstance(target_node_id, int) or isinstance(target_node_ids, list):
            return {
                "task_id": target_node_id if isinstance(target_node_id, int) else None,
                "task_ids": [
                    node_id
                    for node_id in (target_node_ids or [])
                    if isinstance(node_id, int)
                ],
                "proposal_kind": payload.get("proposal_kind"),
                "source": "latest_draft_patch",
            }

    latest_tool = _latest_event(recent_events, "tool_observation")
    if latest_tool is not None:
        payload = _event_payload(latest_tool)
        tool_name = payload.get("tool_name") or payload.get("name")
        result = payload.get("result")
        for task_id, title in _answer_candidate_task_refs(result):
            return {
                "task_id": task_id,
                "title": title,
                "source": "latest_tool_answer_candidate",
                "tool_name": tool_name,
            }
        task = payload.get("task")
        if isinstance(task, dict):
            task_id = task.get("task_id", task.get("id"))
            title = task.get("title")
            if isinstance(task_id, int) or isinstance(title, str):
                return {
                    "task_id": task_id if isinstance(task_id, int) else None,
                    "title": title if isinstance(title, str) else None,
                    "source": "latest_tool_observation",
                    "tool_name": tool_name,
                }
        observed_tasks = payload.get("observed_tasks", payload.get("observed_nodes"))
        if isinstance(observed_tasks, list):
            for item in observed_tasks:
                if not isinstance(item, dict):
                    continue
                node_id = item.get("task_id", item.get("id"))
                title = item.get("title")
                if isinstance(node_id, int) or isinstance(title, str):
                    return {
                        "task_id": node_id if isinstance(node_id, int) else None,
                        "title": title if isinstance(title, str) else None,
                        "source": "latest_tool_observation",
                        "tool_name": tool_name,
                    }
        result_task = result.get("task") if isinstance(result, dict) else None
        if isinstance(result_task, dict):
            node_id = result_task.get("task_id", result_task.get("id"))
            title = result_task.get("title")
            if isinstance(node_id, int) or isinstance(title, str):
                return {
                    "task_id": node_id if isinstance(node_id, int) else None,
                    "title": title if isinstance(title, str) else None,
                    "source": "latest_tool_observation",
                    "tool_name": tool_name,
                }
        matches = result.get("matches") if isinstance(result, dict) else None
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                task = match.get("task")
                if not isinstance(task, dict):
                    continue
                node_id = task.get("task_id", task.get("id"))
                title = task.get("title")
                if isinstance(node_id, int) or isinstance(title, str):
                    return {
                        "task_id": node_id if isinstance(node_id, int) else None,
                        "title": title if isinstance(title, str) else None,
                        "source": "latest_tool_observation",
                        "tool_name": tool_name,
                    }
        entity_matches = result.get("entity_matches") if isinstance(result, dict) else None
        if isinstance(entity_matches, list):
            for match in entity_matches:
                if not isinstance(match, dict):
                    continue
                task = match.get("task")
                if not isinstance(task, dict):
                    continue
                node_id = task.get("task_id", task.get("id"))
                title = task.get("title")
                if isinstance(node_id, int) or isinstance(title, str):
                    return {
                        "task_id": node_id if isinstance(node_id, int) else None,
                        "title": title if isinstance(title, str) else None,
                        "source": "latest_tool_observation",
                        "tool_name": tool_name,
                    }
        snapshots = result.get("task_snapshots") if isinstance(result, dict) else None
        if isinstance(snapshots, list):
            for snapshot in snapshots:
                if not isinstance(snapshot, dict):
                    continue
                task = snapshot.get("task")
                if not isinstance(task, dict):
                    continue
                node_id = task.get("task_id", task.get("id"))
                title = task.get("title")
                if isinstance(node_id, int) or isinstance(title, str):
                    return {
                        "task_id": node_id if isinstance(node_id, int) else None,
                        "title": title if isinstance(title, str) else None,
                        "source": "latest_tool_observation",
                        "tool_name": tool_name,
                    }
        arguments = payload.get("arguments")
        if isinstance(arguments, dict) and isinstance(arguments.get("task_id"), int):
            return {
                "task_id": arguments["task_id"],
                "source": "latest_tool_observation",
                "tool_name": tool_name,
            }

    if focus_node is not None:
        return {
            "task_id": focus_node.id,
            "title": focus_node.title,
            "source": "focus_task",
        }
    return None


def _build_recent_targets(
    recent_events: list[dict[str, Any]],
    current_target_branch: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []

    def add_target(task_id: Any, title: Any, *, source: str, tool_name: Any = None) -> None:
        if not isinstance(task_id, int) and not isinstance(title, str):
            return
        targets.append(
            {
                "task_id": task_id if isinstance(task_id, int) else None,
                "title": title if isinstance(title, str) and title.strip() else None,
                "source": source,
                "tool_name": tool_name if isinstance(tool_name, str) else None,
            }
        )

    for event in recent_events:
        if not isinstance(event, dict):
            continue
        payload = _event_payload(event)
        event_type = str(event.get("event_type") or "")
        if event_type == "tool_answer_completion":
            ids = payload.get("target_node_ids")
            titles = payload.get("target_node_titles")
            if isinstance(ids, list):
                for index, task_id in enumerate(ids):
                    title = titles[index] if isinstance(titles, list) and index < len(titles) else None
                    add_target(task_id, title, source=event_type)
            else:
                add_target(
                    payload.get("target_node_id"),
                    payload.get("target_node_title"),
                    source=event_type,
                )
            continue
        if event_type != "tool_observation":
            continue
        task = payload.get("task")
        tool_name = payload.get("tool_name") or payload.get("name")
        result = payload.get("result")
        for task_id, title in _answer_candidate_task_refs(result):
            add_target(
                task_id,
                title,
                source="tool_answer_candidate",
                tool_name=tool_name,
            )
        if isinstance(task, dict):
            add_target(
                task.get("task_id", task.get("id")),
                task.get("title"),
                source=event_type,
                tool_name=tool_name,
            )
        observed_tasks = payload.get("observed_tasks", payload.get("observed_nodes"))
        if isinstance(observed_tasks, list):
            answer_targets = [
                item
                for item in observed_tasks
                if isinstance(item, dict)
                and str(item.get("source") or "") == "answer_candidate"
            ]
            # Search tools may expose incidental retrieval candidates alongside
            # the smaller set actually used in their answer. Conversation
            # continuity follows the answer targets, not every candidate that
            # happened to score in the same lookup.
            for item in answer_targets or observed_tasks:
                if not isinstance(item, dict):
                    continue
                add_target(
                    item.get("task_id", item.get("id")),
                    item.get("title"),
                    source=event_type,
                    tool_name=tool_name,
                )
        result_task = result.get("task") if isinstance(result, dict) else None
        if isinstance(result_task, dict):
            add_target(
                result_task.get("task_id", result_task.get("id")),
                result_task.get("title"),
                source=event_type,
                tool_name=tool_name,
            )
        matches = result.get("matches") if isinstance(result, dict) else None
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                task = match.get("task")
                if isinstance(task, dict):
                    add_target(
                        task.get("task_id", task.get("id")),
                        task.get("title"),
                        source=event_type,
                        tool_name=tool_name,
                    )
        entity_matches = result.get("entity_matches") if isinstance(result, dict) else None
        if isinstance(entity_matches, list):
            for match in entity_matches:
                if not isinstance(match, dict):
                    continue
                task = match.get("task")
                if isinstance(task, dict):
                    add_target(
                        task.get("task_id", task.get("id")),
                        task.get("title"),
                        source=event_type,
                        tool_name=tool_name,
                    )
        snapshots = result.get("task_snapshots") if isinstance(result, dict) else None
        if isinstance(snapshots, list):
            for snapshot in snapshots:
                if not isinstance(snapshot, dict):
                    continue
                task = snapshot.get("task")
                if isinstance(task, dict):
                    add_target(
                        task.get("task_id", task.get("id")),
                        task.get("title"),
                        source=event_type,
                        tool_name=tool_name,
                    )

    if isinstance(current_target_branch, dict) and (
        str(current_target_branch.get("source") or "") != "focus_task"
        or not targets
    ):
        # UI focus is a fallback, not a conversational mention. Appending a
        # stale selected task after real tool-answer targets makes it look like
        # the most recent referent and breaks ordinary pronouns such as "its".
        add_target(
            current_target_branch.get("task_id"),
            current_target_branch.get("title"),
            source=str(current_target_branch.get("source") or "current_target_branch"),
            tool_name=current_target_branch.get("tool_name"),
        )

    unique_reversed: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for target in reversed(targets):
        key = (target.get("task_id"), str(target.get("title") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        unique_reversed.append(target)
        if len(unique_reversed) >= 6:
            break
    return list(reversed(unique_reversed))


def _build_recent_decisions(recent_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for event in recent_events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type not in {"approval_decision", "applied_patch", "final_answer"}:
            continue
        payload = _event_payload(event)
        decisions.append(
            {
                "event_type": event_type,
                "created_at": event.get("created_at"),
                "policy": payload.get("policy"),
                "mode": payload.get("mode"),
                "action": payload.get("action"),
                "status": payload.get("status"),
                "proposal_id": payload.get("proposal_id"),
                "proposal_kind": payload.get("proposal_kind"),
                "summary": payload.get("summary"),
            }
        )
    return decisions[-6:]


def _latest_user_request(recent_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest_user_turn = _latest_event(recent_events, "user_turn")
    if latest_user_turn is None:
        return None
    prompt = str(_event_payload(latest_user_turn).get("prompt") or "").strip()
    if not prompt:
        return None
    return {
        "summary": prompt,
        "source": "latest_user_turn",
        "created_at": latest_user_turn.get("created_at"),
    }


def _recent_user_requests(
    recent_events: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for event in recent_events:
        if not isinstance(event, dict) or event.get("event_type") != "user_turn":
            continue
        prompt = str(_event_payload(event).get("prompt") or "").strip()
        if not prompt:
            continue
        requests.append(
            {
                "summary": prompt,
                "source": "user_turn",
                "created_at": event.get("created_at"),
            }
        )
    return requests[-limit:]


def _latest_assistant_answer(recent_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest_answer = _latest_event(recent_events, "final_answer")
    if latest_answer is None:
        return None
    payload = _event_payload(latest_answer)
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return None
    return {
        "summary": summary,
        "mode": payload.get("mode"),
        "policy": payload.get("policy"),
        "has_proposal": bool(payload.get("has_proposal")),
        "has_clarification": bool(payload.get("has_clarification")),
        "source": "latest_final_answer",
        "created_at": latest_answer.get("created_at"),
    }


def _recent_assistant_answers(
    recent_events: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    answers: list[dict[str, Any]] = []
    for event in recent_events:
        if not isinstance(event, dict) or event.get("event_type") != "final_answer":
            continue
        payload = _event_payload(event)
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            continue
        answers.append(
            {
                "summary": summary,
                "mode": payload.get("mode"),
                "policy": payload.get("policy"),
                "has_proposal": bool(payload.get("has_proposal")),
                "has_clarification": bool(payload.get("has_clarification")),
                "source": "final_answer",
                "created_at": event.get("created_at"),
            }
        )
    return answers[-limit:]


def _build_active_agent_run_plan(
    recent_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    latest_plan_event = _latest_event(recent_events, "agent_run_plan_created")
    if latest_plan_event is None:
        return None
    payload = _event_payload(latest_plan_event)
    plan = payload.get("agent_run_plan") if isinstance(payload.get("agent_run_plan"), dict) else payload
    compact_plan = compact_agent_run_plan_for_model(plan)
    if compact_plan is None:
        return None
    status = str(compact_plan.get("status") or "").strip().lower()
    if status in {"complete", "completed", "cancelled", "failed"}:
        return None
    return compact_plan


def _build_unresolved_asks(
    *,
    recent_events: list[dict[str, Any]],
    pending_proposal: dict[str, Any] | None,
    agent_run_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    compact_pending = _compact_pending_proposal(pending_proposal)
    if compact_pending is not None:
        unresolved.append(
            {
                "kind": "pending_proposal",
                "summary": "User has not approved, edited, or rejected this proposal yet.",
                "proposal": compact_pending,
            }
        )
    if agent_run_plan is not None:
        unresolved.append(
            {
                "kind": "agent_run_plan",
                "summary": "A broad Strategist task has been decomposed into bounded work units.",
                "plan": agent_run_plan,
            }
        )

    latest_answer = _latest_event(recent_events, "final_answer")
    if latest_answer is not None:
        payload = _event_payload(latest_answer)
        if payload.get("has_clarification") or payload.get("policy") == "clarify":
            unresolved.append(
                {
                    "kind": "clarification",
                    "summary": payload.get("summary") or "Strategist asked for clarification.",
                    "mode": payload.get("mode"),
                    "source": (
                        "structured_clarification"
                        if payload.get("has_clarification")
                        else "clarify_policy"
                    ),
                }
            )
    return unresolved[:4]


def _build_active_request(
    recent_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the latest still-open request envelope from the durable event log."""

    for event in reversed(recent_events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "").strip()
        payload = _event_payload(event)
        if event_type == "applied_patch":
            return None
        if event_type == "approval_decision":
            action = str(payload.get("action") or "").strip().lower()
            if action in {"approve", "reject"}:
                return None
            # Edit opens a revision of the same request. Keep scanning for the
            # latest continuation envelope instead of erasing the original
            # target, cadence, duration, and constraints.
            continue
        if event_type != "final_answer":
            continue
        state = payload.get("continuation_state")
        if not isinstance(state, dict):
            return None
        status = str(state.get("status") or "").strip().lower()
        if status not in ACTIVE_REQUEST_STATUSES:
            return None
        prompts = [
            str(item).strip()
            for item in state.get("request_prompts", [])
            if isinstance(item, str) and item.strip()
        ]
        if not prompts:
            return None
        return {
            **state,
            "status": status,
            "request_prompts": prompts[-8:],
        }
    return None


def _build_recent_tool_context(
    recent_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for event in recent_events:
        if not isinstance(event, dict) or event.get("event_type") != "tool_observation":
            continue
        payload = _event_payload(event)
        entry = payload.get("tool_context")
        if isinstance(entry, dict):
            context.append(entry)
            continue
        name = payload.get("tool_name") or payload.get("name")
        arguments = payload.get("arguments")
        result = payload.get("result")
        if isinstance(name, str) and isinstance(arguments, dict) and isinstance(result, dict):
            context.append(
                build_tool_context_entry(
                    name=name,
                    arguments=arguments,
                    result=result,
                )
            )
    return merge_tool_context(context)


def build_request_prompt_chain(
    session_memory: dict[str, Any] | None,
    current_prompt: str,
) -> list[str]:
    """Combine an open request with its latest answer without pulling stale turns."""

    prompts: list[str] = []
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory, dict)
        else None
    )
    if isinstance(active_request, dict) and str(
        active_request.get("status") or ""
    ).strip().lower() in ACTIVE_REQUEST_STATUSES:
        prompts.extend(
            str(item).strip()
            for item in active_request.get("request_prompts", [])
            if isinstance(item, str) and item.strip()
        )
    normalized_current = str(current_prompt or "").strip()
    if normalized_current and (not prompts or prompts[-1] != normalized_current):
        prompts.append(normalized_current)
    return _bounded_request_prompts(prompts)


def reconstruct_active_request_prompts(
    session_memory: dict[str, Any] | None,
    current_prompt: str,
    conversation_history: list[dict[str, str]] | None,
    *,
    canonical_original_request: str | None = None,
) -> list[str]:
    """Backfill one open objective from canonical goal truth and visible turns.

    ``active_request`` is a compatibility projection and can be absent after an
    architecture migration or a malformed provider clarification.  The
    canonical GoalState original request therefore remains the authoritative
    root of a continuation chain.
    """

    prompts = build_request_prompt_chain(session_memory, current_prompt)
    active_request = (
        session_memory.get("active_request")
        if isinstance(session_memory, dict)
        and isinstance(session_memory.get("active_request"), dict)
        else None
    )
    projected_original = (
        str(active_request.get("original_request") or "").strip()
        if isinstance(active_request, dict)
        and str(active_request.get("status") or "").strip().lower()
        in ACTIVE_REQUEST_STATUSES
        else ""
    )
    canonical_original = str(canonical_original_request or "").strip()
    original = canonical_original or projected_original
    if not original:
        return prompts
    user_turns = [
        str(message.get("content") or "").strip()
        for message in conversation_history or []
        if isinstance(message, dict)
        and str(message.get("role") or "").strip().lower() == "user"
        and str(message.get("content") or "").strip()
    ]
    matching_indexes = [
        index
        for index, value in enumerate(user_turns)
        if value.casefold() == original.casefold()
    ]
    relevant_turns = (
        user_turns[matching_indexes[-1]:]
        if matching_indexes
        else [original]
    )
    reconstructed: list[str] = []
    for value in [*relevant_turns, *prompts]:
        if value and value not in reconstructed:
            reconstructed.append(value)
    normalized_current = str(current_prompt or "").strip()
    if normalized_current:
        reconstructed = [
            value for value in reconstructed if value != normalized_current
        ]
        reconstructed.append(normalized_current)
    return _bounded_request_prompts(reconstructed)


def build_continuation_state(
    *,
    session_memory: dict[str, Any] | None,
    current_prompt: str,
    clarification: dict[str, Any] | None,
    has_proposal: bool,
    target_task_ids: list[int] | None = None,
    proposal_revision: dict[str, Any] | None = None,
    tool_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist the clauses that remain live across clarification/proposal turns."""

    request_prompts = build_request_prompt_chain(session_memory, current_prompt)
    prior = (
        session_memory.get("active_request")
        if isinstance(session_memory, dict)
        and isinstance(session_memory.get("active_request"), dict)
        else None
    )
    goal_state = (
        session_memory.get("goal_state")
        if isinstance(session_memory, dict)
        and isinstance(session_memory.get("goal_state"), dict)
        else {}
    )
    if isinstance(clarification, dict):
        status = "awaiting_user_input"
    elif has_proposal:
        status = "pending_approval"
    elif isinstance(prior, dict) and str(prior.get("status") or "").strip().lower() in {
        "awaiting_user_input",
        "in_progress",
    }:
        # A failed or incomplete continuation must not erase the original goal.
        status = "in_progress"
    else:
        status = "completed"
    compact_clarification = None
    if isinstance(clarification, dict):
        compact_clarification = {}
        for key in ("message", "question"):
            value = clarification.get(key)
            if isinstance(value, str) and value.strip():
                compact_clarification[key] = value.strip()[:2000]
        for key in (
            "task_choices",
            "field_choices",
            "missing_fields",
            "conflicts",
            "scoped_follow_up_prompts",
        ):
            value = clarification.get(key)
            if isinstance(value, list):
                compact_clarification[key] = value[:12]
    state = {
        "status": status,
        "goal_id": (
            prior.get("goal_id")
            if isinstance(prior, dict) and prior.get("goal_id")
            else goal_state.get("goalId", goal_state.get("goal_id"))
        ),
        "goal_revision": (
            prior.get("goal_revision")
            if isinstance(prior, dict) and prior.get("goal_revision")
            else goal_state.get("revision")
        ),
        "original_request": request_prompts[0] if request_prompts else None,
        "latest_user_input": str(current_prompt or "").strip() or None,
        "request_prompts": request_prompts,
        "clarification": compact_clarification,
        "target_task_ids": _unique_ints(list(target_task_ids or [])),
        "semantic_slots": (
            goal_state.get("semanticSlots", goal_state.get("semantic_slots", []))
            or (
                prior.get("semantic_slots", [])
                if isinstance(prior, dict)
                else []
            )
        ),
    }
    prior_tool_context = (
        prior.get("tool_context")
        if isinstance(prior, dict) and isinstance(prior.get("tool_context"), list)
        else []
    )
    current_tool_context = (
        session_memory.get("recent_tool_context")
        if isinstance(session_memory, dict)
        and isinstance(session_memory.get("recent_tool_context"), list)
        else []
    )
    merged_tool_context = merge_tool_context(
        prior_tool_context,
        current_tool_context,
        tool_context,
    )
    if merged_tool_context:
        state["tool_context"] = merged_tool_context
    active_revision = proposal_revision
    if active_revision is None and isinstance(prior, dict):
        prior_revision = prior.get("proposal_revision")
        if isinstance(prior_revision, dict):
            active_revision = prior_revision
    if isinstance(active_revision, dict) and active_revision:
        # The proposal id plus its compact semantic contract is the durable
        # revision basis. It survives clarifications so the model changes only
        # the field the user corrected instead of rebuilding the request from
        # prose and accidentally retargeting a conflict blocker.
        state["proposal_revision"] = active_revision
    return state


def _unique_ints(values: list[Any]) -> list[int]:
    unique: list[int] = []
    for value in values:
        if isinstance(value, int) and value not in unique:
            unique.append(value)
    return unique


def build_session_memory(
    *,
    working_set: list[dict[str, Any]] | None,
    pending_proposal: dict[str, Any] | None,
    recent_events: list[dict[str, Any]],
    recent_session_events: list[dict[str, Any]] | None = None,
    focus_node: StrategyNode | None,
    active_category: Category | None,
    staged_node_ids: list[int],
    active_agent_run_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build compact continuity memory from durable kernel events and UI state."""
    compact_working_set = _compact_working_set(working_set)
    active_plan = (
        compact_agent_run_plan_for_model(active_agent_run_plan)
        if isinstance(active_agent_run_plan, dict)
        else _build_active_agent_run_plan(recent_events)
    )
    current_target_branch = _build_current_target_branch(
        recent_events=recent_events,
        pending_proposal=pending_proposal,
        focus_node=focus_node,
    )
    target_candidates: list[Any] = list(staged_node_ids)
    if focus_node is not None:
        target_candidates.append(focus_node.id)
    if current_target_branch is not None:
        target_candidates.append(current_target_branch.get("task_id"))
        task_ids = current_target_branch.get("task_ids")
        if isinstance(task_ids, list):
            target_candidates.extend(task_ids)
    for item in compact_working_set:
        target_candidates.append(item.get("task_id"))

    active_request = _build_active_request(recent_events)
    prior_tool_context = (
        active_request.get("tool_context")
        if isinstance(active_request, dict) and isinstance(active_request.get("tool_context"), list)
        else []
    )
    recent_tool_context = merge_tool_context(
        prior_tool_context,
        _build_recent_tool_context(recent_events),
    )
    if isinstance(active_request, dict) and "tool_context" in active_request:
        # Expose one canonical tool-context surface. Keeping the same evidence
        # nested under active_request and again under recent_tool_context grows
        # every continuation packet without adding information.
        active_request = dict(active_request)
        active_request.pop("tool_context", None)
    return {
        "source": "agent_kernel_event_log",
        "active_objective": _build_active_objective(
            recent_events=recent_events,
            pending_proposal=pending_proposal,
            active_category=active_category,
        ),
        "latest_user_request": _latest_user_request(recent_events),
        "latest_assistant_answer": _latest_assistant_answer(recent_events),
        "recent_user_requests": _recent_user_requests(recent_events),
        "recent_assistant_answers": _recent_assistant_answers(recent_events),
        "working_set": compact_working_set,
        "working_set_contract": {
            "purpose": "conversation_continuity_only",
            "complete_task_inventory": False,
            "fresh_workload_snapshot": False,
        },
        "current_target_branch": current_target_branch,
        # Goal-scoped events own active-request state, while conversational
        # references may legitimately compare tasks mentioned under successive
        # read-only goals. Keep those lifetimes separate so a new fact lookup
        # does not erase the preceding comparison target.
        "recent_targets": _build_recent_targets(
            recent_session_events if recent_session_events is not None else recent_events,
            current_target_branch,
        ),
        "pending_proposal": _compact_pending_proposal(pending_proposal),
        "agent_run_plan": active_plan,
        "recent_decisions": _build_recent_decisions(recent_events),
        "unresolved_asks": _build_unresolved_asks(
            recent_events=recent_events,
            pending_proposal=pending_proposal,
            agent_run_plan=active_plan,
        ),
        "active_request": active_request,
        "recent_tool_context": recent_tool_context,
        "continuity_hints": {
            "target_task_ids": _unique_ints(target_candidates),
            "staged_task_ids": _unique_ints(list(staged_node_ids)),
            "has_pending_proposal": isinstance(pending_proposal, dict),
        },
    }


def project_goal_state_memory(
    session_memory: dict[str, Any],
    goal_state: "GoalState",
) -> dict[str, Any]:
    """Make canonical GoalState authoritative while retaining legacy memory keys."""

    from app.services.agent_kernel.contracts import GoalState
    from app.services.agent_kernel.goal_state_reducer import TERMINAL_GOAL_STATUSES

    state = GoalState.model_validate(goal_state.model_dump(mode="python"))
    projected = dict(session_memory)
    projected["source"] = "durable_goal_state"
    bounded_goal = state.model_dump(mode="json", by_alias=True)
    bounded_goal["correctionDeltas"] = bounded_goal.get("correctionDeltas", [])[-8:]
    bounded_goal["resolvedDecisions"] = bounded_goal.get("resolvedDecisions", [])[-12:]
    bounded_goal["unresolvedDecisions"] = bounded_goal.get("unresolvedDecisions", [])[:6]
    projected["goal_state"] = bounded_goal
    if state.status in TERMINAL_GOAL_STATUSES:
        projected["active_request"] = None
        return projected
    active_request_candidate = projected.get("active_request")
    candidate_goal_id = (
        active_request_candidate.get("goal_id")
        if isinstance(active_request_candidate, dict)
        else None
    )
    candidate_original_request = (
        str(active_request_candidate.get("original_request") or "").strip()
        if isinstance(active_request_candidate, dict)
        else ""
    )
    prior_active_request = (
        active_request_candidate
        if isinstance(active_request_candidate, dict)
        and (
            candidate_goal_id == state.goal_id
            or (
                not candidate_goal_id
                and candidate_original_request == state.original_request.strip()
            )
        )
        else {}
    )
    correction_prompts: list[str] = [
        str(item).strip()
        for item in prior_active_request.get("request_prompts", [])
        if isinstance(item, str)
        and str(item).strip()
        and str(item).strip() != state.original_request
    ]
    for delta in state.correction_deltas:
        for patch in delta.patches:
            source_text = patch.provenance.source_text
            if source_text and source_text not in correction_prompts:
                correction_prompts.append(source_text)
    request_prompts = [state.original_request, *correction_prompts[-15:]]
    compatibility_projection: dict[str, Any] = {}
    pending_proposal = projected.get("pending_proposal")
    if isinstance(pending_proposal, dict) and pending_proposal.get("proposal_id"):
        compatibility_projection["proposal_revision"] = pending_proposal
    current_target = projected.get("current_target_branch")
    if isinstance(current_target, dict):
        target_ids = _unique_ints(
            [current_target.get("task_id"), *(current_target.get("task_ids") or [])]
        )
        if target_ids:
            compatibility_projection["target_task_ids"] = target_ids
    projected["active_request"] = {
        **{
            key: value
            for key, value in prior_active_request.items()
            if key in {
                "proposal_revision",
                "target_task_ids",
                "clarification",
                "tool_context",
            }
        },
        **compatibility_projection,
        "status": (
            "awaiting_user_input"
            if state.status == "needs_user_decision"
            else "pending_approval"
            if state.status in {"ready_for_review", "approved", "committing"}
            else "in_progress"
        ),
        "goal_id": state.goal_id,
        "goal_revision": state.revision,
        "original_request": state.original_request,
        "request_prompts": request_prompts,
        "semantic_slots": [slot.model_dump(mode="json", by_alias=True) for slot in state.semantic_slots],
    }
    return projected
