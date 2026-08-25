"""Planner AI capability proposal API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, alias_generators, model_validator
from sqlmodel import Session

from app.db.database import get_session
from app.services.planner_tools import local_wall_time

router = APIRouter(prefix="/api/v1/ai/planner", tags=["Planner AI"])


class PlannerProposeRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    prompt: str = ""
    working_set_task_ids: List[int] = Field(default_factory=list)
    window_start: datetime
    window_end: datetime
    slot_minutes: int = 15
    fallback_duration_minutes: int = 30
    provider_override: Optional[str] = None
    model_override: Optional[str] = None
    provider_auth_preflight: bool = True
    operation_mode: Literal["create", "reschedule", "remove"] = "create"

    @model_validator(mode="after")
    def validate_request(self) -> "PlannerProposeRequest":
        if not self.working_set_task_ids:
            raise ValueError("workingSetTaskIds must contain at least one task id.")
        if self.slot_minutes <= 0:
            raise ValueError("slotMinutes must be positive.")
        if self.fallback_duration_minutes <= 0:
            raise ValueError("fallbackDurationMinutes must be positive.")
        if local_wall_time(self.window_end) <= local_wall_time(self.window_start):
            raise ValueError("windowEnd must be after windowStart.")
        return self


class PlannerContextCard(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    id: str
    label: str
    value: str
    detail: Optional[str] = None


class PlannerProposalResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    provider_label: str
    mode: str
    summary: str
    context_cards: List[PlannerContextCard] = Field(default_factory=list)
    suggested_prompts: List[str] = Field(default_factory=list)
    proposal: Optional[dict[str, Any]] = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


def _error(status_code: int, error_type: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "type": error_type,
                "detail": detail,
            }
        },
    )


@router.post("/propose", response_model=PlannerProposalResponse)
def propose_schedule_patch(
    body: PlannerProposeRequest,
    session: Session = Depends(get_session),
) -> PlannerProposalResponse:
    raise _error(
        status.HTTP_410_GONE,
        "planner_proposal_retired",
        (
            "Standalone Planner proposals have been retired. Submit the scheduling "
            "request through Strategist to create a canonical ChangeSet review."
        ),
    )
