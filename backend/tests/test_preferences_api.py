import json
import unittest

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db.database import create_db_and_tables, engine
from app.main import app
from app.models.preference_memory import (
    PreferenceChangeLog,
    PreferenceLink,
    PreferenceObservation,
    SchedulingConstraint,
    UserPreference,
)


class PreferencesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        self.client = TestClient(app)

    def _create_memory(self) -> tuple[str, str]:
        with Session(engine) as session:
            preference = UserPreference(
                domain="scheduling",
                preference_type="availability",
                scope_type="global",
                strength="hard",
                description="Do not schedule work before 10 AM.",
                value_json=json.dumps({"rule": "do_not_schedule_before", "endTime": "10:00"}),
            )
            session.add(preference)
            session.commit()
            session.refresh(preference)
            constraint = SchedulingConstraint(
                constraint_type="forbidden_window",
                target_type="global",
                hard=True,
                source_preference_id=preference.id,
                description="Do not schedule work before 10 AM.",
                value_json=json.dumps(
                    {
                        "recurrence": "weekly",
                        "daysOfWeek": [0, 1, 2, 3, 4, 5, 6],
                        "startTime": "00:00",
                        "endTime": "10:00",
                    }
                ),
            )
            session.add(constraint)
            session.commit()
            session.refresh(constraint)
            return preference.id, constraint.id

    def test_list_remembered_preferences_includes_constraints_and_summary(self) -> None:
        preference_id, constraint_id = self._create_memory()

        response = self.client.get("/api/v1/preferences/remembered?includeInactive=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        preference = next(
            item for item in payload["preferences"] if item["id"] == preference_id
        )
        self.assertEqual(preference["description"], "Do not schedule work before 10 AM.")
        self.assertTrue(preference["active"])
        self.assertEqual(preference["value"]["endTime"], "10:00")
        self.assertEqual(preference["constraints"][0]["id"], constraint_id)
        self.assertEqual(preference["constraints"][0]["value"]["endTime"], "10:00")
        self.assertGreaterEqual(payload["summary"]["activeCount"], 1)
        self.assertGreaterEqual(payload["summary"]["hardConstraintCount"], 1)

    def test_update_remembered_preference_disables_linked_constraints_and_audits(self) -> None:
        preference_id, constraint_id = self._create_memory()
        with Session(engine) as session:
            link = PreferenceLink(
                preference_id=preference_id,
                target_type="workspace",
                target_id="test-workspace",
            )
            session.add(link)
            session.commit()
            link_id = link.id

        response = self.client.patch(
            f"/api/v1/preferences/remembered/{preference_id}",
            json={"active": False},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["active"])
        self.assertFalse(payload["constraints"][0]["active"])

        with Session(engine) as session:
            preference = session.get(UserPreference, preference_id)
            constraint = session.get(SchedulingConstraint, constraint_id)
            link = session.get(PreferenceLink, link_id)
            change_logs = session.exec(
                select(PreferenceChangeLog).where(
                    PreferenceChangeLog.record_id.in_([preference_id, constraint_id])
                )
            ).all()

        self.assertFalse(preference.active)
        self.assertFalse(constraint.active)
        self.assertFalse(link.active)
        self.assertEqual(len(change_logs), 2)
        self.assertEqual({log.change_type for log in change_logs}, {"deactivate"})

        restored = self.client.patch(
            f"/api/v1/preferences/remembered/{preference_id}",
            json={"active": True},
        )
        self.assertEqual(restored.status_code, 200)
        with Session(engine) as session:
            self.assertTrue(session.get(UserPreference, preference_id).active)
            self.assertTrue(session.get(SchedulingConstraint, constraint_id).active)
            self.assertTrue(session.get(PreferenceLink, link_id).active)

    def test_delete_remembered_preference_removes_linked_memory_and_audits(self) -> None:
        preference_id, constraint_id = self._create_memory()
        with Session(engine) as session:
            observation = PreferenceObservation(
                preference_id=preference_id,
                event_type="test",
            )
            link = PreferenceLink(
                preference_id=preference_id,
                target_type="workspace",
                target_id="test-workspace",
            )
            session.add(observation)
            session.add(link)
            session.commit()
            observation_id = observation.id
            link_id = link.id

        response = self.client.delete(
            f"/api/v1/preferences/remembered/{preference_id}"
        )

        self.assertEqual(response.status_code, 204)
        with Session(engine) as session:
            self.assertIsNone(session.get(UserPreference, preference_id))
            self.assertIsNone(session.get(SchedulingConstraint, constraint_id))
            self.assertIsNone(session.get(PreferenceObservation, observation_id))
            self.assertIsNone(session.get(PreferenceLink, link_id))
            audit = session.exec(
                select(PreferenceChangeLog)
                .where(PreferenceChangeLog.record_id == preference_id)
                .where(PreferenceChangeLog.change_type == "delete")
            ).first()
        self.assertIsNotNone(audit)

    def test_list_decodes_legacy_tagged_structured_preference_value(self) -> None:
        with Session(engine) as session:
            preference = UserPreference(
                domain="scheduling",
                preference_type="inter_task_gap",
                scope_type="global",
                description="Leave a gap around longer tasks.",
                value_json=json.dumps(
                    {
                        "kind": "scalar",
                        "value": json.dumps(
                            {
                                "interTaskGapMinutes": 15,
                                "minimumTaskDurationMinutes": 45,
                            }
                        ),
                    }
                ),
            )
            session.add(preference)
            session.commit()
            preference_id = preference.id

        payload = self.client.get(
            "/api/v1/preferences/remembered?includeInactive=true"
        ).json()
        item = next(
            value for value in payload["preferences"] if value["id"] == preference_id
        )
        self.assertEqual(item["value"]["interTaskGapMinutes"], 15)
        self.assertEqual(item["value"]["minimumTaskDurationMinutes"], 45)

    def test_settings_crud_rejects_semantic_paraphrase_duplicate(self) -> None:
        created = self.client.post(
            "/api/v1/preferences/remembered",
            json={
                "domain": "scheduling",
                "preferenceType": "inter_task_gap",
                "scopeType": "global",
                "strength": "medium",
                "description": "Keep a 17-minute gap between scheduled tasks.",
                "value": {"interTaskGapMinutes": 17},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        created_payload = created.json()
        preference_id = created_payload["id"]
        self.assertEqual(created_payload["revision"], 1)

        listed = self.client.get(
            "/api/v1/preferences/remembered?includeInactive=true"
        )
        self.assertEqual(listed.status_code, 200)
        listed_item = next(
            item
            for item in listed.json()["preferences"]
            if item["id"] == preference_id
        )
        self.assertEqual(listed_item["value"]["interTaskGapMinutes"], 17)

        updated = self.client.patch(
            f"/api/v1/preferences/remembered/{preference_id}",
            json={
                "revision": listed_item["revision"],
                "description": "Leave a nineteen minute buffer between tasks.",
                "preferenceType": "schedule_style",
                "value": {
                    "preferenceText": "Leave a nineteen minute buffer between tasks."
                },
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        updated_payload = updated.json()
        self.assertEqual(updated_payload["revision"], listed_item["revision"] + 1)

        duplicate = self.client.post(
            "/api/v1/preferences/remembered",
            json={
                "domain": "scheduling",
                "preferenceType": "inter_task_gap",
                "scopeType": "global",
                "strength": "strong",
                "description": "Use a 19-minute transition between every task.",
                "value": {"interTaskGapMinutes": 19},
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        duplicate_error = duplicate.json()["detail"]["error"]
        self.assertEqual(duplicate_error["type"], "DuplicatePreference")
        self.assertEqual(duplicate_error["existingPreferenceId"], preference_id)

        stale = self.client.patch(
            f"/api/v1/preferences/remembered/{preference_id}",
            json={
                "revision": listed_item["revision"],
                "description": "This edit was based on a stale revision.",
            },
        )
        self.assertEqual(stale.status_code, 409)

        deleted = self.client.delete(
            f"/api/v1/preferences/remembered/{preference_id}"
        )
        self.assertEqual(deleted.status_code, 204)
        with Session(engine) as session:
            self.assertIsNone(session.get(UserPreference, preference_id))
            change_types = set(
                session.exec(
                    select(PreferenceChangeLog.change_type).where(
                        PreferenceChangeLog.record_id == preference_id
                    )
                ).all()
            )
        self.assertTrue({"create", "update", "delete"}.issubset(change_types))

    def test_create_hard_memory_compiles_linked_constraint(self) -> None:
        response = self.client.post(
            "/api/v1/preferences/remembered",
            json={
                "domain": "scheduling",
                "preferenceType": "unavailable_window",
                "scopeType": "category",
                "scopeId": "987654",
                "strength": "hard",
                "description": "Keep 13:00 to 14:00 unavailable for this category.",
                "value": {"startTime": "13:00", "endTime": "14:00"},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(len(payload["constraints"]), 1)
        constraint = payload["constraints"][0]
        self.assertEqual(constraint["constraintType"], "forbidden_window")
        self.assertEqual(constraint["targetType"], "category")
        self.assertEqual(constraint["targetId"], "987654")
        self.assertTrue(constraint["hard"])
        self.client.delete(f"/api/v1/preferences/remembered/{payload['id']}")


if __name__ == "__main__":
    unittest.main()
