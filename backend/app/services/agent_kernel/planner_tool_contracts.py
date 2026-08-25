"""Canonical Planner model-frame and tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from app.services.agent_kernel.contracts import ContractModel, strict_provider_schema
from app.services.agent_kernel.contracts.common import JsonValue


class PlannerTaskDirective(ContractModel):
    task_id: int
    action: Literal["schedule", "skip"]
    allocated_minutes: int = Field(ge=1, le=1440)
    gap_after_minutes: int | None = Field(default=None, ge=0, le=1440)
    rationale: str


class PlannerStrategicDecision(ContractModel):
    question: str = Field(min_length=1, max_length=1_000)
    options: list[str] = Field(min_length=2, max_length=8)


class PlannerFrame(ContractModel):
    intent_summary: str
    planning_rationale: str
    constraints_considered: list[str]
    limitations: list[str]
    selected_capability_ids: list[str]
    decision: Literal["plan", "cannot_plan", "needs_strategic_decision"]
    task_directives: list[PlannerTaskDirective]
    strategic_decision: PlannerStrategicDecision | None = None


class ReadPlannerContextInput(ContractModel):
    pass


class SearchCapabilityCatalogInput(ContractModel):
    goal: str = Field(min_length=1)


class EvaluateSchedulePlanInput(ContractModel):
    task_directives: list[PlannerTaskDirective] = Field(min_length=1)


class LoadSkillInput(ContractModel):
    capability_id: str = Field(min_length=1)


class PlannerContextData(ContractModel):
    read_only: Literal[True]
    working_set: JsonValue
    calendar: JsonValue
    preferences: JsonValue
    default_scheduling_policy: JsonValue
    operation_mode: str


class CapabilityCatalogData(ContractModel):
    read_only: Literal[True]
    goal: str
    agent: str
    desired_output: str | None = None
    count: int = Field(ge=0)
    capabilities: list[JsonValue] = Field(default_factory=list)
    retrieval_strategy: str
    catalog_truncated: bool
    selection_note: str


class ScheduleEvaluationData(ContractModel):
    read_only: Literal[True]
    normalization: JsonValue
    solver_status: str
    placements: list[JsonValue]
    conflicts: list[JsonValue]
    model_instruction: str


class LoadedSkillData(ContractModel):
    read_only: Literal[True]
    capability: JsonValue = None
    skill: str | None = None
    recommended_tools: list[str] = Field(default_factory=list)
    evidence_requirements: JsonValue = None
    dependencies: list[str] = Field(default_factory=list)
    truncated: bool | None = None
    authority_boundary: str | None = None
    error: str | None = None
    available_capability_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class PlannerToolContract:
    name: str
    description: str
    input_model: type[ContractModel]
    output_model: type[ContractModel]
    result_kind: str
    contract_version: str = "2.0"
    side_effect: str = "none"
    approval_requirement: str = "none"
    idempotency_behavior: str = "read_only"

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": strict_provider_schema(self.input_model),
            "strict": True,
        }

    def validate_input(self, value: object) -> dict[str, Any]:
        return self.input_model.model_validate(value).model_dump(exclude_unset=True)

    def validate_output(self, value: object) -> dict[str, Any]:
        return self.output_model.model_validate(value).model_dump(exclude_unset=True)


PLANNER_MODEL_TOOL_CONTRACTS: dict[str, PlannerToolContract] = {
    "read_planner_context": PlannerToolContract(
        "read_planner_context",
        "Read the submitted task set, calendar window, commitments, and preferences.",
        ReadPlannerContextInput,
        PlannerContextData,
        "planner_context",
    ),
    "search_capability_catalog": PlannerToolContract(
        "search_capability_catalog",
        "Semantically search the bounded Planner capability catalog.",
        SearchCapabilityCatalogInput,
        CapabilityCatalogData,
        "capability_catalog",
    ),
    "evaluate_schedule_plan": PlannerToolContract(
        "evaluate_schedule_plan",
        "Evaluate an ordered semantic task plan with the deterministic scheduler.",
        EvaluateSchedulePlanInput,
        ScheduleEvaluationData,
        "schedule_evaluation",
    ),
    "load_skill": PlannerToolContract(
        "load_skill",
        "Load one selected Planner capability by stable id.",
        LoadSkillInput,
        LoadedSkillData,
        "loaded_skill",
    ),
}


def planner_model_tool_definitions() -> list[dict[str, Any]]:
    return [contract.definition() for contract in PLANNER_MODEL_TOOL_CONTRACTS.values()]


def validate_planner_tool_input(name: str, value: object) -> dict[str, Any]:
    contract = PLANNER_MODEL_TOOL_CONTRACTS.get(name)
    if contract is None:
        raise ValueError(f"Unknown Planner tool: {name}")
    return contract.validate_input(value)


def validate_planner_tool_output(name: str, value: object) -> dict[str, Any]:
    contract = PLANNER_MODEL_TOOL_CONTRACTS.get(name)
    if contract is None:
        raise ValueError(f"Unknown Planner tool: {name}")
    return contract.validate_output(value)


PLANNER_FRAME_SCHEMA = strict_provider_schema(PlannerFrame)
