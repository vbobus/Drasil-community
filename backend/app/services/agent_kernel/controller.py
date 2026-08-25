"""Single-authority review projection for compiled ChangeSets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import ChangeSet, ChangeSetSimulationResult, GoalState, ReviewDocument
from .presentation import ReviewDocumentProjector, ReviewProjectionError


@dataclass(frozen=True)
class NativeAuthoritySelection:
    review_document: ReviewDocument | None
    selected_authority: str
    reason_code: str

    def to_safe_trace_payload(self) -> dict[str, Any]:
        return {
            "selectedAuthority": self.selected_authority,
            "reasonCode": self.reason_code,
            "reviewProjected": self.review_document is not None,
        }


class NativeAuthorityController:
    """Project the one canonical review; never select between semantic paths."""

    def __init__(
        self,
        *,
        projector: ReviewDocumentProjector | None = None,
    ) -> None:
        self.projector = projector or ReviewDocumentProjector()

    def select(
        self,
        *,
        session_id: str,
        goal: GoalState,
        change_set: ChangeSet,
        simulation: ChangeSetSimulationResult | None,
        review_entity_titles: Mapping[str, str] | None = None,
        display_time_zone: str | None = None,
    ) -> NativeAuthoritySelection:
        if simulation is None:
            return NativeAuthoritySelection(
                review_document=None,
                selected_authority="none",
                reason_code="simulation_unavailable",
            )
        simulation_status = getattr(simulation, "status", None)
        if (
            simulation_status is not None
            and simulation_status not in {"ready_for_review", "needs_user_decision"}
        ):
            return NativeAuthoritySelection(
                review_document=None,
                selected_authority="none",
                reason_code="simulation_not_reviewable",
            )
        try:
            review = self.projector.project(
                session_id=session_id,
                goal=goal,
                change_set=change_set,
                simulation=simulation,
                entity_titles=review_entity_titles,
                display_time_zone=display_time_zone,
            )
        except ReviewProjectionError:
            return NativeAuthoritySelection(
                review_document=None,
                selected_authority="none",
                reason_code="review_projection_failed",
            )
        return NativeAuthoritySelection(
            review_document=review,
            selected_authority="server_changeset",
            reason_code="canonical_review_projected",
        )
