"""Provider-neutral ownership and loop guards for hierarchical subagents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from pydantic import Field

from app.services.agent_kernel.contracts.common import ContractModel
from app.services.agent_kernel.mcp_policy import allowed_mcp_tool_names


PlannerSubagentStatus = Literal[
    "completed",
    "needs_strategic_decision",
    "needs_user_decision",
    "infeasible",
    "retryable_failure",
    "failed",
]


class SubagentDelegationError(ValueError):
    """Base failure for rejected hierarchical agent coordination."""


class SubagentOwnershipError(SubagentDelegationError):
    """The requested parent/child relationship is not authorized."""


class SubagentLoopGuardError(SubagentDelegationError):
    """A task or decision identity would create a repeated agent exchange."""


class SubagentBudget(ContractModel):
    timeout_seconds: int = Field(ge=1, le=300)
    max_exchanges: int = Field(ge=1, le=4)
    max_tool_calls: int = Field(ge=0, le=32)
    max_tokens: int = Field(ge=1, le=100_000)


class SubagentUsage(ContractModel):
    exchanges: int = Field(default=1, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class SubagentParentContext(ContractModel):
    parent_agent_id: str
    parent_run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    correlation_id: str = Field(min_length=1, max_length=128)


class StrategicDecisionRequest(ContractModel):
    decision_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=1_000)
    options: list[str] = Field(min_length=2, max_length=8)


class PlannerSubagentTask(ContractModel):
    task_id: str = Field(min_length=1, max_length=128)
    cancellation_id: str = Field(min_length=1, max_length=128)
    content_fingerprint: str = Field(min_length=16, max_length=64)
    parent: SubagentParentContext
    assigned_agent_id: str
    objective: str = Field(min_length=1, max_length=4_000)
    task_ids: list[int] = Field(min_length=1, max_length=100)
    evidence_handles: list[str] = Field(default_factory=list, max_length=32)
    allowed_tool_names: list[str] = Field(min_length=1, max_length=64)
    budget: SubagentBudget
    created_at: datetime
    deadline_at: datetime


class PlannerSubagentResult(ContractModel):
    task_id: str
    content_fingerprint: str = Field(min_length=16, max_length=64)
    parent_run_id: str
    goal_id: str
    goal_revision: int = Field(ge=1)
    status: PlannerSubagentStatus
    artifact: dict[str, Any] | None = None
    decision_request: StrategicDecisionRequest | None = None
    usage: SubagentUsage = Field(default_factory=SubagentUsage)
    reason_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z0-9_]+$",
    )
    retryable: bool = False


def _task_content(
    *,
    parent: SubagentParentContext,
    objective: str,
    task_ids: list[int],
    evidence_handles: list[str],
    allowed_tool_names: list[str],
    budget: SubagentBudget,
) -> dict[str, Any]:
    return {
        "parent": parent.model_dump(mode="json", by_alias=True),
        "assignedAgentId": "planner",
        "objective": objective.strip(),
        "taskIds": sorted(dict.fromkeys(task_ids)),
        "evidenceHandles": sorted(dict.fromkeys(evidence_handles)),
        "allowedToolNames": sorted(dict.fromkeys(allowed_tool_names)),
        "budget": budget.model_dump(mode="json", by_alias=True),
    }


def _task_identity_content(
    *,
    parent: SubagentParentContext,
    objective: str,
    task_ids: list[int],
) -> dict[str, Any]:
    """Return semantic identity unaffected by elapsed-time budget shrinkage."""

    return {
        "parentAgentId": parent.parent_agent_id,
        "parentRunId": parent.parent_run_id,
        "sessionId": parent.session_id,
        "goalId": parent.goal_id,
        "goalRevision": parent.goal_revision,
        "assignedAgentId": "planner",
        "objective": objective.strip(),
        "taskIds": sorted(dict.fromkeys(task_ids)),
    }


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def create_planner_subagent_task(
    *,
    parent: SubagentParentContext,
    objective: str,
    task_ids: list[int],
    evidence_handles: list[str],
    allowed_tool_names: list[str],
    budget: SubagentBudget,
    now: datetime,
    task_id: str | None = None,
) -> PlannerSubagentTask:
    """Build one stable task from trusted Strategist-owned context."""

    if parent.parent_agent_id != "strategist":
        raise SubagentOwnershipError(
            "Planner tasks must be owned by the Strategist agent."
        )
    unique_task_ids = list(dict.fromkeys(int(item) for item in task_ids))
    if not unique_task_ids:
        raise SubagentOwnershipError("Planner tasks require a bounded task scope.")
    unique_tools = list(dict.fromkeys(str(item).strip() for item in allowed_tool_names))
    unique_tools = [item for item in unique_tools if item]
    planner_policy = allowed_mcp_tool_names("planner")
    forbidden = sorted(set(unique_tools).difference(planner_policy))
    if forbidden:
        raise SubagentOwnershipError(
            f"Planner task requested capabilities outside its policy: {forbidden}"
        )
    content = _task_content(
        parent=parent,
        objective=objective,
        task_ids=unique_task_ids,
        evidence_handles=evidence_handles,
        allowed_tool_names=unique_tools,
        budget=budget,
    )
    content_fingerprint = _fingerprint(content)
    stable_suffix = _fingerprint(
        _task_identity_content(
            parent=parent,
            objective=objective,
            task_ids=unique_task_ids,
        )
    )[:24]
    return PlannerSubagentTask(
        task_id=task_id or f"planner-task:{stable_suffix}",
        cancellation_id=f"planner-cancel:{stable_suffix}",
        content_fingerprint=content_fingerprint,
        parent=parent,
        assigned_agent_id="planner",
        objective=objective,
        task_ids=unique_task_ids,
        evidence_handles=list(dict.fromkeys(evidence_handles)),
        allowed_tool_names=unique_tools,
        budget=budget,
        created_at=now,
        deadline_at=now + timedelta(seconds=budget.timeout_seconds),
    )


@dataclass
class _TaskRecord:
    task: PlannerSubagentTask
    started_at: datetime
    result: PlannerSubagentResult | None = None
    callback_decision_ids: set[str] = field(default_factory=set)
    callback_consumed: bool = False


@dataclass
class PlannerSubagentCoordinator:
    """Own one-level Strategist-to-Planner execution and decision callbacks."""

    now: Callable[[], datetime] = datetime.now
    event_sink: Callable[[str, dict[str, Any]], None] | None = None
    _records: dict[str, _TaskRecord] = field(default_factory=dict, init=False)
    _cancelled_ids: set[str] = field(default_factory=set, init=False)
    _failure_diagnostics: dict[str, dict[str, str]] = field(
        default_factory=dict,
        init=False,
    )

    def cancel(self, cancellation_id: str) -> None:
        self._cancelled_ids.add(str(cancellation_id))

    def is_cancelled(self, cancellation_id: str) -> bool:
        return str(cancellation_id) in self._cancelled_ids

    def run(
        self,
        task: PlannerSubagentTask,
        executor: Callable[[PlannerSubagentTask], dict[str, Any]],
    ) -> PlannerSubagentResult:
        self._require_owned_task(task)
        if task.task_id in self._records:
            raise SubagentLoopGuardError(
                f"Planner subagent task {task.task_id!r} has already executed."
            )
        record = _TaskRecord(task=task, started_at=self.now())
        self._records[task.task_id] = record
        self._emit(
            "planner_subagent_started",
            task=task,
            status="started",
            usage=SubagentUsage(exchanges=0),
        )
        preflight = self._preflight_result(task)
        if preflight is not None:
            record.result = preflight
            self._emit_result(preflight)
            return preflight
        try:
            payload = executor(task)
            postflight = self._preflight_result(task)
            result = (
                postflight
                if postflight is not None
                else self._normalize_result(task, payload)
            )
        except Exception as error:
            self._failure_diagnostics[task.task_id] = {
                "failure_stage": "executor_or_result_normalization",
                "exception_type": type(error).__name__[:128],
                "exception_detail": str(error)[:1_000],
            }
            result = self._result(
                task,
                status="failed",
                reason_code="subagent_result_invalid",
            )
        record.result = result
        self._emit_result(result)
        return result

    def respond_to_strategic_decision(
        self,
        *,
        task_id: str,
        decision_id: str,
        selected_option: str,
        executor: Callable[
            [PlannerSubagentTask, StrategicDecisionRequest, str],
            dict[str, Any],
        ],
    ) -> PlannerSubagentResult:
        record = self._records.get(task_id)
        if record is None or record.result is None:
            raise SubagentLoopGuardError("The Planner task is not awaiting a decision.")
        request = record.result.decision_request
        if (
            record.result.status != "needs_strategic_decision"
            or request is None
            or request.decision_id != decision_id
        ):
            raise SubagentLoopGuardError(
                "The strategic decision does not belong to the active Planner task."
            )
        if decision_id in record.callback_decision_ids:
            raise SubagentLoopGuardError(
                f"Strategic decision {decision_id!r} already received its callback."
            )
        if record.callback_consumed:
            raise SubagentLoopGuardError(
                "The Planner task already consumed its one strategic callback."
            )
        if selected_option not in request.options:
            raise SubagentLoopGuardError(
                "The strategic response is not one of the offered options."
            )
        if record.result.usage.exchanges >= record.task.budget.max_exchanges:
            raise SubagentLoopGuardError("The Planner exchange budget is exhausted.")
        preflight = self._preflight_result(record.task)
        if preflight is not None:
            record.result = preflight
            self._emit_result(preflight)
            return preflight
        remaining_task = self._remaining_budget_task(record)
        if remaining_task is None:
            result = self._result(
                record.task,
                status="failed",
                reason_code="subagent_budget_exhausted",
                usage=record.result.usage,
            )
            record.result = result
            self._emit_result(result)
            return result
        record.callback_consumed = True
        record.callback_decision_ids.add(decision_id)
        try:
            payload = executor(remaining_task, request, selected_option)
            incremental = self._usage(payload)
            combined = SubagentUsage(
                exchanges=record.result.usage.exchanges
                + max(1, incremental.exchanges),
                tool_calls=record.result.usage.tool_calls + incremental.tool_calls,
                tokens=record.result.usage.tokens + incremental.tokens,
            )
            postflight = self._preflight_result(record.task)
            result = (
                postflight
                if postflight is not None
                else self._normalize_result(
                    record.task,
                    payload,
                    usage_override=combined,
                )
            )
        except Exception as error:
            self._failure_diagnostics[record.task.task_id] = {
                "failure_stage": "decision_callback_or_result_normalization",
                "exception_type": type(error).__name__[:128],
                "exception_detail": str(error)[:1_000],
            }
            result = self._result(
                record.task,
                status="failed",
                reason_code="subagent_result_invalid",
                usage=record.result.usage,
            )
        record.result = result
        self._emit_result(result)
        return result

    def _require_owned_task(self, task: PlannerSubagentTask) -> None:
        if (
            task.parent.parent_agent_id != "strategist"
            or task.assigned_agent_id != "planner"
        ):
            raise SubagentOwnershipError(
                "Only Strategist may create its Planner child task."
            )
        policy = allowed_mcp_tool_names("planner")
        if not set(task.allowed_tool_names).issubset(policy):
            raise SubagentOwnershipError(
                "Planner task capabilities exceed the immutable server policy."
            )
        expected_content = _task_content(
            parent=task.parent,
            objective=task.objective,
            task_ids=task.task_ids,
            evidence_handles=task.evidence_handles,
            allowed_tool_names=task.allowed_tool_names,
            budget=task.budget,
        )
        if _fingerprint(expected_content) != task.content_fingerprint:
            raise SubagentLoopGuardError(
                "Planner task content does not match its host-owned fingerprint."
            )

    def _preflight_result(
        self, task: PlannerSubagentTask
    ) -> PlannerSubagentResult | None:
        if task.cancellation_id in self._cancelled_ids:
            return self._result(
                task,
                status="failed",
                reason_code="subagent_cancelled",
            )
        if self.now() >= task.deadline_at:
            return self._result(
                task,
                status="retryable_failure",
                reason_code="subagent_deadline_exceeded",
                retryable=True,
            )
        return None

    def _remaining_budget_task(
        self,
        record: _TaskRecord,
    ) -> PlannerSubagentTask | None:
        if record.result is None:
            return record.task
        remaining_exchanges = (
            record.task.budget.max_exchanges - record.result.usage.exchanges
        )
        remaining_tools = (
            record.task.budget.max_tool_calls - record.result.usage.tool_calls
        )
        remaining_tokens = (
            record.task.budget.max_tokens - record.result.usage.tokens
        )
        if remaining_exchanges <= 0 or remaining_tools < 0 or remaining_tokens <= 0:
            return None
        budget = SubagentBudget(
            timeout_seconds=max(
                1,
                min(
                    record.task.budget.timeout_seconds,
                    int((record.task.deadline_at - self.now()).total_seconds()),
                ),
            ),
            max_exchanges=remaining_exchanges,
            max_tool_calls=remaining_tools,
            max_tokens=remaining_tokens,
        )
        content = _task_content(
            parent=record.task.parent,
            objective=record.task.objective,
            task_ids=record.task.task_ids,
            evidence_handles=record.task.evidence_handles,
            allowed_tool_names=record.task.allowed_tool_names,
            budget=budget,
        )
        return record.task.model_copy(
            update={
                "budget": budget,
                "content_fingerprint": _fingerprint(content),
            }
        )

    @staticmethod
    def _usage(payload: dict[str, Any]) -> SubagentUsage:
        raw = payload.get("subagent_usage")
        if not isinstance(raw, dict):
            return SubagentUsage()
        return SubagentUsage.model_validate(raw)

    def _normalize_result(
        self,
        task: PlannerSubagentTask,
        payload: dict[str, Any],
        *,
        usage_override: SubagentUsage | None = None,
    ) -> PlannerSubagentResult:
        if not isinstance(payload, dict):
            raise TypeError("Planner subagent result must be an object.")
        usage = usage_override or self._usage(payload)
        if usage.exchanges > task.budget.max_exchanges:
            return self._result(
                task,
                status="failed",
                reason_code="subagent_exchange_budget_exceeded",
                usage=usage,
            )
        if usage.tool_calls > task.budget.max_tool_calls:
            return self._result(
                task,
                status="failed",
                reason_code="subagent_tool_budget_exceeded",
                usage=usage,
            )
        if usage.tokens > task.budget.max_tokens:
            return self._result(
                task,
                status="failed",
                reason_code="subagent_token_budget_exceeded",
                usage=usage,
            )
        explicit_status = str(payload.get("status") or "").strip()
        allowed_statuses = {
            "completed",
            "needs_strategic_decision",
            "needs_user_decision",
            "infeasible",
            "retryable_failure",
            "failed",
        }
        decision_request = None
        if explicit_status == "needs_strategic_decision":
            raw_decision = payload.get("strategic_decision")
            if not isinstance(raw_decision, dict):
                raise ValueError("Strategic decision payload is required.")
            decision_identity = _fingerprint(
                {
                    "taskId": task.task_id,
                    "question": str(raw_decision.get("question") or "").strip(),
                    "options": raw_decision.get("options"),
                }
            )[:24]
            decision_request = StrategicDecisionRequest.model_validate(
                {
                    **raw_decision,
                    "decision_id": f"planner-decision:{decision_identity}",
                }
            )
        if explicit_status in allowed_statuses:
            status: PlannerSubagentStatus = explicit_status  # type: ignore[assignment]
        elif payload.get("needs_user_input") is True or str(
            payload.get("planner_mode") or ""
        ) == "clarification":
            status = "needs_user_decision"
        elif payload.get("error"):
            planner_error = payload.get("planner_error")
            retryable = (
                isinstance(planner_error, dict)
                and planner_error.get("retryable") is True
            )
            status = "retryable_failure" if retryable else "failed"
        elif payload.get("proposal_ready") is True:
            status = "completed"
        else:
            status = "failed"
        if explicit_status == "completed" and (
            payload.get("error")
            or payload.get("planner_error")
            or decision_request is not None
        ):
            return self._result(
                task,
                status="failed",
                usage=usage,
                reason_code="subagent_result_contradictory",
            )
        if status == "completed" and self._artifact_exceeds_task_scope(task, payload):
            return self._result(
                task,
                status="failed",
                usage=usage,
                reason_code="subagent_artifact_scope_violation",
            )
        raw_reason = str(payload.get("reason_code") or "").strip()
        reason_code = self._safe_reason_code(raw_reason, status=status)
        return self._result(
            task,
            status=status,
            artifact=dict(payload),
            decision_request=decision_request,
            usage=usage,
            reason_code=reason_code,
            retryable=status == "retryable_failure",
        )

    @staticmethod
    def _artifact_exceeds_task_scope(
        task: PlannerSubagentTask,
        payload: dict[str, Any],
    ) -> bool:
        allowed = set(task.task_ids)

        def visit(value: Any, *, authoritative: bool = False) -> bool:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = str(key).replace("-", "_").lower()
                    child_authoritative = authoritative or normalized in {
                        "operations",
                        "task_directives",
                        "target_node_ids",
                        "workingsettaskids",
                        "working_set_task_ids",
                    }
                    if child_authoritative and normalized in {
                        "task_id",
                        "taskid",
                    }:
                        if isinstance(child, int) and not isinstance(child, bool):
                            if child not in allowed:
                                return True
                    if child_authoritative and normalized in {
                        "task_ids",
                        "taskids",
                        "target_node_ids",
                        "targetnodeids",
                        "workingsettaskids",
                        "working_set_task_ids",
                    }:
                        if isinstance(child, list) and any(
                            isinstance(item, int)
                            and not isinstance(item, bool)
                            and item not in allowed
                            for item in child
                        ):
                            return True
                    if visit(child, authoritative=child_authoritative):
                        return True
            elif isinstance(value, list):
                return any(visit(item, authoritative=authoritative) for item in value)
            return False

        return visit(payload)

    @staticmethod
    def _safe_reason_code(
        raw_reason: str,
        *,
        status: PlannerSubagentStatus,
    ) -> str | None:
        known = {
            "subagent_artifact_scope_violation",
            "subagent_budget_exhausted",
            "subagent_cancelled",
            "subagent_deadline_exceeded",
            "subagent_exchange_budget_exceeded",
            "subagent_execution_failed",
            "subagent_result_contradictory",
            "subagent_result_invalid",
            "subagent_token_budget_exceeded",
            "subagent_tool_budget_exceeded",
        }
        if raw_reason in known:
            return raw_reason
        if raw_reason and status in {"failed", "retryable_failure"}:
            return "subagent_reported_failure"
        return None

    @staticmethod
    def _result(
        task: PlannerSubagentTask,
        *,
        status: PlannerSubagentStatus,
        artifact: dict[str, Any] | None = None,
        decision_request: StrategicDecisionRequest | None = None,
        usage: SubagentUsage | None = None,
        reason_code: str | None = None,
        retryable: bool = False,
    ) -> PlannerSubagentResult:
        safe_reason = PlannerSubagentCoordinator._safe_reason_code(
            str(reason_code or ""),
            status=status,
        )
        return PlannerSubagentResult(
            task_id=task.task_id,
            content_fingerprint=task.content_fingerprint,
            parent_run_id=task.parent.parent_run_id,
            goal_id=task.parent.goal_id,
            goal_revision=task.parent.goal_revision,
            status=status,
            artifact=artifact,
            decision_request=decision_request,
            usage=usage or SubagentUsage(exchanges=0),
            reason_code=safe_reason,
            retryable=retryable,
        )

    def _emit_result(self, result: PlannerSubagentResult) -> None:
        record = self._records[result.task_id]
        failure_diagnostic = self._failure_diagnostics.get(result.task_id, {})
        self._emit(
            "planner_subagent_completed",
            task=record.task,
            status=result.status,
            usage=result.usage,
            reason_code=result.reason_code,
            duration_ms=max(
                0.0,
                (self.now() - record.started_at).total_seconds() * 1000,
            ),
            failure_diagnostic=failure_diagnostic,
        )

    def _emit(
        self,
        event_name: str,
        *,
        task: PlannerSubagentTask,
        status: str,
        usage: SubagentUsage,
        reason_code: str | None = None,
        duration_ms: float | None = None,
        failure_diagnostic: dict[str, str] | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        event_payload: dict[str, Any] = {
                "task_id": task.task_id,
                "parent_run_id": task.parent.parent_run_id,
                "goal_id": task.parent.goal_id,
                "goal_revision": task.parent.goal_revision,
                "assigned_agent_id": task.assigned_agent_id,
                "status": status,
                "reason_code": self._safe_reason_code(
                    str(reason_code or ""),
                    status=status,  # type: ignore[arg-type]
                ),
                "exchange_count": min(usage.exchanges, task.budget.max_exchanges),
                "tool_call_count": min(usage.tool_calls, task.budget.max_tool_calls),
                "token_count": min(usage.tokens, task.budget.max_tokens),
                "token_budget": task.budget.max_tokens,
                "token_over_budget_by": max(
                    0,
                    usage.tokens - task.budget.max_tokens,
                ),
                "duration_ms": (
                    min(max(0.0, duration_ms), 300_000.0)
                    if duration_ms is not None
                    else None
                ),
            }
        if failure_diagnostic:
            event_payload.update(failure_diagnostic)
        self.event_sink(event_name, event_payload)
