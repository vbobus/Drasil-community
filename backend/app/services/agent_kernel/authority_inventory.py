"""Machine-readable ownership map for the active capability migration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .rollout import OperationFamily, RolloutPolicy


@dataclass(frozen=True)
class CapabilityAuthority:
    capability: str
    primary_path: str
    application_service: str
    compatibility_path: str | None
    rollout_flag: str | None
    flag_enabled: bool | None
    promotion_eligible: bool | None
    promotion_reason: str | None
    missing_gates: tuple[str, ...]
    evidence_builds: tuple[str, ...]
    required_build_id: str | None
    retirement_gate: str

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        return {
            "capability": payload["capability"],
            "primaryPath": payload["primary_path"],
            "applicationService": payload["application_service"],
            "compatibilityPath": payload["compatibility_path"],
            "rolloutFlag": payload["rollout_flag"],
            "flagEnabled": payload["flag_enabled"],
            "promotionEligible": payload["promotion_eligible"],
            "promotionReason": payload["promotion_reason"],
            "missingGates": list(payload["missing_gates"]),
            "evidenceBuilds": list(payload["evidence_builds"]),
            "requiredBuildId": payload["required_build_id"],
            "retirementGate": payload["retirement_gate"],
        }


def _fixed_capabilities() -> list[CapabilityAuthority]:
    return [
        CapabilityAuthority(
            capability="task_reads",
            primary_path="drasil_core_mcp",
            application_service="TaskReadApplicationService",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="authoritative_mcp_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate=(
                "The live agent host has no second task-read adapter; historical "
                "parity fixtures are offline only."
            ),
        ),
        CapabilityAuthority(
            capability="calendar_reads",
            primary_path="drasil_core_mcp",
            application_service="CalendarReadApplicationService",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="authoritative_mcp_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate=(
                "The live agent host has no calendar-read shadow or compatibility "
                "reader after bounded cutover."
            ),
        ),
        CapabilityAuthority(
            capability="preference_reads",
            primary_path="drasil_core_mcp",
            application_service="PreferenceApplicationService",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="authoritative_mcp_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate="No separate preference-read compatibility authority remains.",
        ),
        CapabilityAuthority(
            capability="scheduling",
            primary_path="drasil_core_mcp",
            application_service=(
                "PlannerSubagentCoordinator + PlannerSchedulingApplicationService"
            ),
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="owned_planner_subagent_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate=(
                "Flexible model-backed scheduling crosses one Strategist-owned "
                "Planner task; exact deterministic UI commands remain a separate "
                "typed application-service entry point."
            ),
        ),
        CapabilityAuthority(
            capability="planner_delegation",
            primary_path="strategist_owned_planner_subagent",
            application_service="PlannerSubagentCoordinator",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="bounded_hierarchical_a2a",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate=(
                "Only Strategist may own Planner; stable task/decision IDs, "
                "budgets, cancellation, and one callback fail closed."
            ),
        ),
        CapabilityAuthority(
            capability="change_compilation",
            primary_path="server_change_compilation_coordinator",
            application_service=(
                "ServerChangeCompilationCoordinator + "
                "NativeChangeSetAuthoringCoordinator"
            ),
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="server_owned_cross_agent_composition",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate="One compiler is the only live mutation-authoring authority.",
        ),
        CapabilityAuthority(
            capability="review_actions",
            primary_path="review_action_coordinator",
            application_service="ReviewActionCoordinator",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="single_authority_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate="Legacy proposal-card actions return a retired response.",
        ),
        CapabilityAuthority(
            capability="transaction_execution",
            primary_path="native_changeset_transaction_runtime",
            application_service="AgentChangeSetTransactionRuntime",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=None,
            promotion_eligible=True,
            promotion_reason="single_authority_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate="No accepted review can call a proposal-family apply path.",
        ),
        CapabilityAuthority(
            capability="mcp_reads",
            primary_path="drasil_core_mcp",
            application_service="drasil-core read adapter over shared services",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=True,
            promotion_eligible=True,
            promotion_reason="authoritative_mcp_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate="Historical read-shadow fixtures remain offline only.",
        ),
        CapabilityAuthority(
            capability="mcp_commands",
            primary_path="drasil_core_mcp_high_level_draft",
            application_service="goal, semantic-plan, and scheduling application services",
            compatibility_path=None,
            rollout_flag=None,
            flag_enabled=True,
            promotion_eligible=True,
            promotion_reason="authoritative_mcp_cutover",
            missing_gates=(),
            evidence_builds=(),
            required_build_id=None,
            retirement_gate="Only high-level semantic-plan tools are agent-facing.",
        ),
    ]


def build_authority_inventory(
    policy: RolloutPolicy | None = None,
) -> dict[str, Any]:
    rollout = policy or RolloutPolicy.from_environment()
    capabilities = _fixed_capabilities()
    for family in OperationFamily:
        capabilities.append(
            CapabilityAuthority(
                capability=f"{family.value}_writes",
                primary_path="server_compiled_changeset",
                application_service=(
                    "ServerChangeCompilationCoordinator + "
                    "AgentChangeSetTransactionRuntime"
                ),
                compatibility_path=None,
                rollout_flag=None,
                flag_enabled=None,
                promotion_eligible=True,
                promotion_reason="single_authority_cutover",
                missing_gates=(),
                evidence_builds=(),
                required_build_id=None,
                retirement_gate=(
                    "Rollback deploys the previous known-good build; a request never "
                    "selects a proposal-family fallback."
                ),
            )
        )
    return {
        "schemaVersion": "1.0",
        "nativeAcceptedFallbackPolicy": "prohibited",
        "agentFacingAuthority": "drasil_core_mcp",
        "mcpProductionAuthority": "authoritative",
        "capabilities": [
            item.to_payload()
            for item in sorted(capabilities, key=lambda item: item.capability)
        ],
    }
