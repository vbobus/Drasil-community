import unittest
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.habits import router
from app.db.database import get_session
from app.models.habit import Habit, HabitCheckIn
from app.services.habits import local_today, set_schedule, snapshot, streaks
from app.db import database
from app.services.account_auth import AccountContextMiddleware, create_account, create_session


class HabitTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.today = date(2026, 10, 9)
        self.clock = patch("app.api.habits.local_today", return_value=self.today)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.client.close()
        self.session.close()
        self.engine.dispose()

    def create(self, **kwargs):
        result = self.client.post("/api/v1/habits", json={"title": "Read ten pages", **kwargs})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["habits"][-1]["id"]

    def check(self, habit_id, day=None, completed=True):
        return self.client.put(f"/api/v1/habits/{habit_id}/check-ins/{day or self.today}", json={"completed": completed})

    def test_completion_is_idempotent_and_undo_reverses_rewards(self):
        habit_id = self.create()
        for _ in range(3):
            result = self.check(habit_id)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["totalXp"], 10)
            self.assertEqual(result.json()["habits"][0]["currentStreak"], 1)
        self.assertEqual(len(self.session.exec(select(HabitCheckIn)).all()), 1)
        self.assertTrue(result.json()["badges"][0]["earned"])
        for _ in range(2):
            result = self.check(habit_id, completed=False)
            self.assertEqual(result.json()["totalXp"], 0)
            self.assertFalse(result.json()["badges"][0]["earned"])

    def test_weekday_streak_ignores_rest_days_and_pending_today(self):
        habit = Habit(title="Walk", created_on=date(2026, 10, 2), weekdays="0,2,4")
        completed = {date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 7)}
        self.assertEqual(streaks(habit, completed, self.today), (3, 3))
        completed.add(self.today)
        self.assertEqual(streaks(habit, completed, self.today + timedelta(days=1)), (4, 4))
        completed.remove(date(2026, 10, 7))
        self.assertEqual(streaks(habit, completed, self.today), (1, 2))

    def test_schedule_edit_preserves_past_schedule_and_history(self):
        habit = Habit(title="Walk", created_on=date(2026, 10, 2), weekdays="0,2,4")
        set_schedule(habit, [1], self.today)
        self.session.add(habit)
        self.session.commit()
        for day in [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 7)]:
            self.assertEqual(self.check(habit.id, day).status_code, 200)
        result = snapshot(self.session, self.today)
        self.assertEqual(result["habits"][0]["currentStreak"], 3)
        self.assertFalse(result["habits"][0]["week"][-1]["scheduled"])
        self.assertEqual(self.check(habit.id).status_code, 422)

    def test_future_pre_creation_and_rest_day_checkins_are_rejected(self):
        habit_id = self.create(weekdays=[0])
        for day in [self.today, self.today - timedelta(days=1), self.today + timedelta(days=3)]:
            self.assertEqual(self.check(habit_id, day).status_code, 422)
        self.assertFalse(self.session.exec(select(HabitCheckIn)).all())

    def test_archive_preserves_xp_and_restore_allows_checkins(self):
        habit_id = self.create()
        self.check(habit_id)
        result = self.client.patch(f"/api/v1/habits/{habit_id}/archive", json={"archived": True})
        self.assertEqual(result.json()["totalXp"], 10)
        self.assertTrue(result.json()["habits"][0]["archived"])
        self.assertEqual(self.check(habit_id, completed=False).status_code, 409)
        self.client.patch(f"/api/v1/habits/{habit_id}/archive", json={"archived": False})
        self.assertEqual(self.check(habit_id, completed=False).json()["totalXp"], 0)

    def test_validation_and_missing_habit(self):
        for body in [{"title": "  "}, {"title": "Walk", "weekdays": []}, {"title": "Walk", "weekdays": [7]},
                     {"title": "Walk", "weekdays": [1, 1]}, {"title": "Walk", "icon": "unknown"}]:
            self.assertEqual(self.client.post("/api/v1/habits", json=body).status_code, 422)
        self.assertEqual(self.check("missing").status_code, 404)
        with self.assertRaises(Exception) as error:
            local_today("Not/AZone")
        self.assertEqual(error.exception.status_code, 422)

    def test_reward_level_and_milestone_are_derived_from_real_history(self):
        habit = Habit(title="Daily reading", created_on=self.today - timedelta(days=9))
        self.session.add(habit)
        self.session.commit()
        for offset in range(10):
            self.check(habit.id, self.today - timedelta(days=offset))
        result = self.client.get("/api/v1/habits").json()
        self.assertEqual((result["level"], result["totalXp"], result["levelXp"]), (2, 100, 0))
        self.assertTrue(result["badges"][1]["earned"])
        self.assertTrue(result["badges"][2]["earned"])
        self.assertEqual(result["habits"][0]["bestStreak"], 10)

    def test_habits_and_checkins_stay_in_the_account_session(self):
        habit_id = self.create()
        self.check(habit_id)
        other_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(other_engine)
        with Session(other_engine) as other:
            self.assertEqual(snapshot(other, self.today)["habits"], [])
            self.assertIsNone(other.get(Habit, habit_id))
            self.assertEqual(snapshot(other, self.today)["totalXp"], 0)
        other_engine.dispose()


class AuthenticatedHabitTests(unittest.TestCase):
    def test_real_account_api_rejects_anonymous_and_other_account_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {
                "DRASIL_TEST_ISOLATED_DB": "0",
                "DRASIL_AUTH_DATABASE_PATH": str(root / "auth.db"),
                "DRASIL_ACCOUNT_DATA_ROOT": str(root / "accounts"),
                "DRASIL_LEGACY_DATABASE_PATH": str(root / "no-legacy.db"),
            }):
                try:
                    alex = create_account(username="alex", display_name="Alex", password="private-alex-password")
                    blair = create_account(username="blair", display_name="Blair", password="private-blair-password")
                    database.initialize_account_database(alex)
                    database.initialize_account_database(blair)
                    app = FastAPI()
                    app.include_router(router)
                    app.add_middleware(AccountContextMiddleware)
                    with TestClient(app) as client:
                        self.assertEqual(client.get("/api/v1/habits").status_code, 401)
                        alex_headers = {"Authorization": f"Bearer {create_session(alex)}"}
                        blair_headers = {"Authorization": f"Bearer {create_session(blair)}"}
                        result = client.post("/api/v1/habits", json={"title": "Private reading"}, headers=alex_headers)
                        self.assertEqual(result.status_code, 201, result.text)
                        habit_id = result.json()["habits"][0]["id"]
                        today = result.json()["today"]
                        check_path = f"/api/v1/habits/{habit_id}/check-ins/{today}"
                        self.assertEqual(client.put(check_path, json={"completed": True}, headers=alex_headers).status_code, 200)
                        self.assertEqual(client.get("/api/v1/habits", headers=blair_headers).json()["habits"], [])
                        self.assertEqual(client.put(check_path, json={"completed": False}, headers=blair_headers).status_code, 404)
                        self.assertEqual(client.get("/api/v1/habits", headers=alex_headers).json()["totalXp"], 10)
                finally:
                    for engine in database._ACCOUNT_ENGINES.values():
                        engine.dispose()
                    database._ACCOUNT_ENGINES.clear()


if __name__ == "__main__":
    unittest.main()
