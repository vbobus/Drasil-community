import unittest
from datetime import date, datetime, timedelta, timezone

from app.models.category import Category
from app.models.node import StrategyNode
from app.services.deadline_values import parse_deadline_date, serialize_deadline_date
from app.services.retrieval import format_deadline
from app.services.strategist_presentation import build_context_cards


class DeadlineValueTests(unittest.TestCase):
    def test_offset_datetime_is_read_and_persisted_as_a_deadline_date(self) -> None:
        value = "2026-09-10T09:00:00-07:00"

        self.assertEqual(parse_deadline_date(value), date(2026, 9, 10))
        self.assertEqual(format_deadline(value), "Sep 10, 2026")
        self.assertEqual(
            serialize_deadline_date(
                datetime(2026, 9, 10, 9, 0, tzinfo=timezone(timedelta(hours=-7)))
            ),
            "2026-09-10",
        )

    def test_context_cards_ignore_invalid_legacy_deadlines_without_crashing(self) -> None:
        category = Category(id=1, name="Work", accent_color="#2563eb")
        nodes = [
            StrategyNode(
                id=1,
                category_id=1,
                title="Offset deadline",
                deadline_at="2026-09-10T09:00:00-07:00",
            ),
            StrategyNode(
                id=2,
                category_id=1,
                title="Malformed legacy deadline",
                deadline_at="not-a-date",
            ),
            StrategyNode(
                id=3,
                category_id=1,
                title="Completed deadline",
                deadline_at="2026-09-10",
                status="done",
            ),
        ]

        cards = build_context_cards(
            categories=[category],
            nodes=nodes,
            focus_node=None,
            active_category=category,
            staged_node_count=1,
            today_override=date(2026, 9, 5),
        )

        current_load = next(card for card in cards if card["id"] == "current_load")
        self.assertEqual(current_load["detail"], "1 due within 7 days")


if __name__ == "__main__":
    unittest.main()
