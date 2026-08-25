import unittest

from app.services.agent_kernel.recurrence_authoring import (
    NativeRecurrenceAuthoringError,
    compile_native_recurrence_update,
)
from app.services.agent_kernel.runtime import (
    _hydrate_series_recurrence_handoff,
    _planner_handoff_target_scope,
)


class NativeRecurrenceAuthoringTests(unittest.TestCase):
    def _trace(self, *, series_id: str = "series-team-call") -> list[dict[str, object]]:
        return [
            {
                "name": "get_task_schedule",
                "arguments": {"task_ids": [188]},
                "result": {
                    "result_kind": "task_schedule_context",
                    "task_ids": [188],
                    "blocks": [
                        {
                            "block_id": 96,
                            "task_id": 188,
                            "task_title": "Team Call",
                            "recurrence_series_id": series_id,
                            "recurrence_series_revision": 4,
                            "recurrence_key": "2026-07-21",
                            "recurrence_rule": {
                                "frequency": "weekly",
                                "days_of_week": [
                                    "monday",
                                    "tuesday",
                                    "wednesday",
                                    "thursday",
                                ],
                                "start_date": "2026-07-21",
                                "end_date": None,
                                "occurrence_count": None,
                                "preferred_start_time": "20:00",
                                "duration_minutes": 60,
                                "timezone": "America/Vancouver",
                            },
                        }
                    ],
                },
            }
        ]

    def test_clock_time_follow_up_preserves_series_cadence_and_duration(self) -> None:
        draft = compile_native_recurrence_update(
            handoff={
                "task_ids": [188],
                "operation_mode": "reschedule",
                "recurrence": {
                    "frequency": "weekly",
                    "days_of_week": [
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                    ],
                    "start_date": "2026-07-21",
                    "end_mode": "never",
                    "duration_minutes": 60,
                    "preferred_start_time": "19:00",
                    "timezone": "America/Vancouver",
                    "strict": True,
                },
            },
            tool_trace=self._trace(),
            source_ref="turn:move-team-call",
            source_text="move the team call to 7 pm instead",
        )

        self.assertEqual(len(draft.operations), 1)
        operation = draft.operations[0]
        self.assertEqual(operation.kind, "recurrence.update_series")
        self.assertEqual(
            operation.series_ref.entity_id,
            "recurrence_series:series-team-call",
        )
        self.assertEqual(operation.series_ref.revision, 4)
        self.assertEqual(
            operation.updates.model_dump(exclude_unset=True, exclude_none=True),
            {"preferred_start_time": "19:00"},
        )
        self.assertEqual(draft.clause["scope"], "series")
        self.assertEqual(draft.clause["cardinality"], "exactly_one")
        self.assertEqual(
            draft.preconditions.expected_entity_revisions[0].entity_id,
            "recurrence_series:series-team-call",
        )

    def test_same_session_target_must_resolve_to_one_authoritative_series(self) -> None:
        trace = self._trace(series_id="series-one")
        trace[0]["result"]["blocks"].append(  # type: ignore[index,union-attr]
            {
                "block_id": 97,
                "task_id": 188,
                "task_title": "Team Call",
                "recurrence_series_id": "series-two",
                "recurrence_series_revision": 1,
                "recurrence_rule": {
                    "frequency": "weekly",
                    "days_of_week": ["monday"],
                    "start_date": "2026-07-21",
                    "preferred_start_time": "20:00",
                    "duration_minutes": 60,
                },
            }
        )

        with self.assertRaisesRegex(
            NativeRecurrenceAuthoringError,
            "exactly one series",
        ):
            compile_native_recurrence_update(
                handoff={
                    "task_ids": [188],
                    "operation_mode": "reschedule",
                    "recurrence": {
                        "frequency": "weekly",
                        "start_date": "2026-07-21",
                        "duration_minutes": 60,
                        "preferred_start_time": "19:00",
                    },
                },
                tool_trace=trace,  # type: ignore[arg-type]
                source_ref="turn:ambiguous-series",
                source_text="move the team call to 7 pm",
            )

    def test_every_occurrence_scope_repairs_missing_recurrence_from_tool_evidence(self) -> None:
        handoff = {
            "task_ids": [188],
            "instruction": (
                "Move every occurrence of Team Call to 7:00 PM local time, "
                "preserving its existing recurrence pattern and duration."
            ),
            "window_start": "2026-07-21T19:00:00",
            "window_end": "2027-07-21T20:00:00",
            "operation_mode": "reschedule",
            "scope_source": "explicit_named_task",
            "recurrence": None,
            "request_constraints": [
                {
                    "kind": "exact_start",
                    "hard": True,
                    "label": "7 PM for every occurrence",
                    "source_text": "every occurence",
                    "task_ids": [188],
                    "start_at": "2026-07-21T19:00:00",
                }
            ],
        }

        repaired = _hydrate_series_recurrence_handoff(
            handoff,
            tool_trace=self._trace(),  # type: ignore[arg-type]
            request_context_text="move the team call to 7 pm\nevery occurence",
        )

        self.assertTrue(repaired)
        self.assertEqual(handoff["target_scope"], "series")
        self.assertTrue(handoff["series_scope_repaired"])
        self.assertEqual(
            handoff["recurrence"],
            {
                "frequency": "weekly",
                "days_of_week": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                ],
                "start_date": "2026-07-21",
                "duration_minutes": 60,
                "preferred_start_time": "19:00",
                "timezone": "America/Vancouver",
                "strict": True,
                "end_mode": "never",
            },
        )
        self.assertEqual(handoff["request_constraints"], [])

    def test_explicit_user_series_scope_overrides_provider_occurrence_scope(self) -> None:
        handoff = {
            "task_ids": [188],
            "instruction": "Move the call to 7 PM.",
            "operation_mode": "reschedule",
            "target_scope": "occurrence",
            "recurrence": None,
            "request_constraints": [
                {"kind": "exact_start", "start_at": "19:00"}
            ],
        }

        repaired = _hydrate_series_recurrence_handoff(
            handoff,
            tool_trace=self._trace(),  # type: ignore[arg-type]
            request_context_text="move every occurrence of the team call to 7 pm",
        )

        self.assertTrue(repaired)
        self.assertEqual(handoff["target_scope"], "series")
        self.assertEqual(handoff["recurrence"]["preferred_start_time"], "19:00")
        self.assertTrue(handoff["recurrence"]["strict"])
        self.assertEqual(handoff["request_constraints"], [])

    def test_one_occurrence_scope_never_hydrates_a_series_contract(self) -> None:
        handoff = {
            "task_ids": [188],
            "instruction": "Move the next scheduled call only to 7 PM.",
            "operation_mode": "reschedule",
            "target_scope": "occurrence",
            "recurrence": None,
            "request_constraints": [],
        }

        self.assertEqual(
            _planner_handoff_target_scope(
                handoff,
                request_context_text="the next occurrence only",
            ),
            "occurrence",
        )
        self.assertFalse(
            _hydrate_series_recurrence_handoff(
                handoff,
                tool_trace=self._trace(),  # type: ignore[arg-type]
                request_context_text="the next occurrence only",
            )
        )
        self.assertIsNone(handoff["recurrence"])


if __name__ == "__main__":
    unittest.main()
