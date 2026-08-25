"""Provider-neutral boundary for Planner scheduling execution."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from pydantic import Field, RootModel

from app.services.agent_kernel.contracts.common import ContractModel


class ScheduleTaskUpdate(ContractModel):
    task_id: int
    fields: dict[str, Any]


class SchedulePlanningRequest(ContractModel):
    """Validated handoff from the Strategist host to Planner."""

    task_ids: list[int] = Field(min_length=1)
    instruction: str = Field(min_length=1)
    window_start: str = Field(min_length=1)
    window_end: str = Field(min_length=1)
    operation_mode: Literal["create", "reschedule", "remove"]
    scope_source: Literal[
        "attached_working_set",
        "explicit_named_task",
        "calendar_query",
    ]
    target_scope: Literal["task", "occurrence", "series"] | None = None
    task_updates: list[ScheduleTaskUpdate] | None = None
    recurrence: dict[str, Any] | None = None
    request_constraints: list[dict[str, Any]] = Field(default_factory=list)
    # Host-owned marker for a deterministic repair path. This is not part of
    # the model-facing handoff schema.
    deterministic_semantics_complete: bool = False


class SchedulePlanningResult(RootModel[dict[str, Any]]):
    """Typed envelope for one Planner application-service outcome."""


def schedule_planning_result_payload(
    result: SchedulePlanningResult | dict[str, Any],
) -> dict[str, Any]:
    """Return an owned canonical payload from the typed Planner boundary."""

    return copy.deepcopy(SchedulePlanningResult.model_validate(result).root)


class SchedulingApplicationService(Protocol):
    def plan(
        self,
        request: SchedulePlanningRequest | dict[str, Any],
    ) -> SchedulePlanningResult: ...


@dataclass(frozen=True)
class PlannerSchedulingApplicationService:
    """Typed application service backed by the current Planner implementation."""

    executor: Callable[[dict[str, Any]], dict[str, Any]]

    def plan(
        self,
        request: SchedulePlanningRequest | dict[str, Any],
    ) -> SchedulePlanningResult:
        validated = SchedulePlanningRequest.model_validate(request)
        payload = validated.model_dump(mode="python", exclude_none=False)
        outcome = self.executor(copy.deepcopy(payload))
        return SchedulePlanningResult.model_validate(outcome)


# Import compatibility for callers that have not yet adopted the explicit name.
CallableSchedulingApplicationService = PlannerSchedulingApplicationService
