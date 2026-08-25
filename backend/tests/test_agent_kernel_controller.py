from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.services.agent_kernel.controller import NativeAuthorityController
from app.services.agent_kernel.presentation import ReviewProjectionError


class NativeAuthorityControllerTests(unittest.TestCase):
    def test_missing_simulation_fails_closed_without_an_alternate_authority(self) -> None:
        projector = Mock()
        selected = NativeAuthorityController(projector=projector).select(
            session_id="session:one",
            goal=SimpleNamespace(),
            change_set=SimpleNamespace(),
            simulation=None,
        )

        self.assertEqual(selected.selected_authority, "none")
        self.assertEqual(selected.reason_code, "simulation_unavailable")
        self.assertIsNone(selected.review_document)
        projector.project.assert_not_called()

    def test_exact_compiled_lineage_projects_the_single_server_authority(self) -> None:
        expected_review = SimpleNamespace(review_id="review:one")
        projector = Mock()
        projector.project.return_value = expected_review
        selected = NativeAuthorityController(projector=projector).select(
            session_id="session:one",
            goal=SimpleNamespace(),
            change_set=SimpleNamespace(),
            simulation=SimpleNamespace(),
            review_entity_titles={"task:1": "Write launch brief"},
        )

        self.assertEqual(selected.selected_authority, "server_changeset")
        self.assertEqual(selected.reason_code, "canonical_review_projected")
        self.assertIs(selected.review_document, expected_review)
        self.assertEqual(
            projector.project.call_args.kwargs["entity_titles"],
            {"task:1": "Write launch brief"},
        )

    def test_projection_failure_does_not_select_a_fallback(self) -> None:
        projector = Mock()
        projector.project.side_effect = ReviewProjectionError("stale")
        selected = NativeAuthorityController(projector=projector).select(
            session_id="session:one",
            goal=SimpleNamespace(),
            change_set=SimpleNamespace(),
            simulation=SimpleNamespace(),
        )

        self.assertEqual(selected.selected_authority, "none")
        self.assertEqual(selected.reason_code, "review_projection_failed")
        self.assertIsNone(selected.review_document)


if __name__ == "__main__":
    unittest.main()
