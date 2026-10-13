import os
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db.database import create_db_and_tables, engine
from app.db.seed import seed_default_categories
from app.main import app
from app.models.calendar_block import CalendarBlock
from app.models.calendar_integration import CalendarIntegration
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.google_calendar import (
    GOOGLE_REAUTHORIZATION_MESSAGE,
    GoogleCalendarAuthorizationError,
    GoogleCalendarError,
    _google_request,
    delete_google_event_by_id,
    google_event_payload,
)


class GoogleCalendarIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        create_db_and_tables()
        seed_default_categories()
        self.client = TestClient(app)

    def _create_category(self, session: Session, name: str = "Google Calendar Work") -> Category:
        category = Category(
            name=name,
            accent_color="#1f6feb",
            icon_slug="briefcase",
            sort_order=922,
        )
        session.add(category)
        session.commit()
        session.refresh(category)
        return category

    def _cleanup_category_fixture(self, category_id: int) -> None:
        with Session(engine) as session:
            for block in session.exec(
                select(CalendarBlock).where(CalendarBlock.category_id == category_id)
            ).all():
                session.delete(block)
            for node in session.exec(
                select(StrategyNode).where(StrategyNode.category_id == category_id)
            ).all():
                session.delete(node)
            category = session.get(Category, category_id)
            if category:
                session.delete(category)
            session.commit()

    def _create_task(
        self,
        session: Session,
        category: Category,
        title: str,
        *,
        status: str = "todo",
        parent_id: int | None = None,
        sort_order: int = 0,
    ) -> StrategyNode:
        node = StrategyNode(
            category_id=category.id,
            title=title,
            node_type="task",
            status=status,
            priority="medium",
            parent_id=parent_id,
            time_estimate_minutes=30,
            emoji="T",
            sort_order=sort_order,
        )
        session.add(node)
        session.commit()
        session.refresh(node)
        return node

    def _create_connected_google_integration(self, session: Session) -> CalendarIntegration:
        integration = session.exec(
            select(CalendarIntegration).where(CalendarIntegration.provider == "google")
        ).first()
        if not integration:
            integration = CalendarIntegration(provider="google")
        integration.is_connected = True
        integration.google_calendar_id = "drasil-calendar@example.com"
        integration.google_calendar_summary = "Drasil"
        integration.time_zone = "America/Vancouver"
        integration.access_token = "access-token"
        integration.refresh_token = "refresh-token"
        integration.expires_at = datetime.utcnow() + timedelta(hours=1)
        integration.last_sync_error = None
        session.add(integration)
        session.commit()
        session.refresh(integration)
        return integration

    def test_google_token_invalid_grant_is_a_reauthorization_error(self) -> None:
        error = HTTPError(
            url="https://oauth2.googleapis.com/token",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error":"invalid_grant","error_description":"Bad Request"}',
            ),
        )
        self.addCleanup(error.close)

        with patch("app.services.google_calendar.urlopen", side_effect=error):
            with self.assertRaises(GoogleCalendarAuthorizationError) as raised:
                _google_request(
                    "POST",
                    "https://oauth2.googleapis.com/token",
                    form={"grant_type": "refresh_token"},
                )

        self.assertEqual(str(raised.exception), GOOGLE_REAUTHORIZATION_MESSAGE)
        self.assertEqual(raised.exception.status_code, 401)

    def test_sync_invalid_grant_marks_google_connection_for_reconnect(self) -> None:
        with Session(engine) as session:
            integration = self._create_connected_google_integration(session)
            integration.expires_at = datetime.utcnow() - timedelta(minutes=5)
            session.add(integration)
            session.commit()

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CALENDAR_CLIENT_ID": "test-client-id",
                "GOOGLE_CALENDAR_CLIENT_SECRET": "test-client-secret",
            },
        ), patch(
            "app.services.google_calendar._google_request",
            side_effect=GoogleCalendarAuthorizationError(
                GOOGLE_REAUTHORIZATION_MESSAGE,
                status_code=401,
            ),
        ):
            response = self.client.post(
                "/api/v1/calendar-integrations/google/sync",
                json={"direction": "pull"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], GOOGLE_REAUTHORIZATION_MESSAGE)
        status_response = self.client.get("/api/v1/calendar-integrations/google/status")
        self.assertEqual(status_response.status_code, 200)
        status_payload = status_response.json()
        self.assertFalse(status_payload["isConnected"])
        self.assertTrue(status_payload["requiresReconnect"])
        self.assertEqual(status_payload["lastSyncError"], GOOGLE_REAUTHORIZATION_MESSAGE)
        with Session(engine) as session:
            integration = session.exec(
                select(CalendarIntegration).where(CalendarIntegration.provider == "google")
            ).first()
            self.assertFalse(integration.is_connected)
            self.assertIsNone(integration.access_token)
            self.assertIsNone(integration.refresh_token)

    def test_replayed_delete_treats_missing_google_event_as_success(self) -> None:
        with Session(engine) as session:
            self._create_connected_google_integration(session)
            with patch(
                "app.services.google_calendar._google_request",
                side_effect=GoogleCalendarError("already deleted", status_code=410),
            ):
                error = delete_google_event_by_id(session, "already-removed-event")

            self.assertIsNone(error)

    def test_google_event_payload_uses_done_prefix_and_private_drasil_metadata(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session)
            parent = self._create_task(session, category, "Portfolio polish", status="done")
            child = self._create_task(
                session,
                category,
                "Archive old projects",
                parent_id=parent.id,
                sort_order=1,
            )
            block = CalendarBlock(
                id=9991,
                task_id=parent.id,
                category_id=category.id,
                start_at=datetime(2026, 6, 5, 9, 0),
                end_at=datetime(2026, 6, 5, 10, 0),
                variant="scheduled",
            )

            payload = google_event_payload(
                block=block,
                task=parent,
                category=category,
                all_category_nodes=[parent, child],
                time_zone="America/Vancouver",
            )

        self.assertTrue(payload["summary"].startswith("[Done]"))
        self.assertIn("Portfolio polish", payload["summary"])
        self.assertIn("Archive old projects", payload["description"])
        private = payload["extendedProperties"]["private"]
        self.assertEqual(private["drasilSource"], "drasil")
        self.assertEqual(private["drasilBlockId"], "9991")
        self.assertEqual(private["drasilTaskId"], str(parent.id))
        self.assertEqual(payload["start"]["timeZone"], "America/Vancouver")

    def test_google_connect_returns_authorization_url_for_dedicated_calendar_scope(self) -> None:
        env = {
            "GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
            "GOOGLE_CALENDAR_REDIRECT_URI": "http://localhost:8000/google/callback",
            "DRASIL_TIMEZONE": "America/Vancouver",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self.client.post("/api/v1/calendar-integrations/google/connect", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("authorizationUrl", payload)
        self.assertIn("access_type=offline", payload["authorizationUrl"])
        self.assertIn("calendar.app.created", payload["authorizationUrl"])
        self.assertNotIn("calendar.events", payload["authorizationUrl"])

    def test_google_connect_ignores_caller_supplied_redirect_uri(self) -> None:
        env = {
            "GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
            "GOOGLE_CALENDAR_REDIRECT_URI": "http://localhost:8000/google/callback",
            "DRASIL_TIMEZONE": "America/Vancouver",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self.client.post(
                "/api/v1/calendar-integrations/google/connect",
                json={"redirectUri": "https://attacker.example/oauth-callback"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["redirectUri"], "http://localhost:8000/google/callback")
        self.assertIn("localhost%3A8000%2Fgoogle%2Fcallback", payload["authorizationUrl"])
        self.assertNotIn("attacker.example", payload["authorizationUrl"])

    def test_google_status_does_not_expose_oauth_client_credentials(self) -> None:
        env = {
            "GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
            "GOOGLE_CALENDAR_REDIRECT_URI": "http://localhost:8000/google/callback",
            "DRASIL_TIMEZONE": "America/Vancouver",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self.client.get("/api/v1/calendar-integrations/google/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["isConfigured"])
        self.assertNotIn("oauthSettings", payload)
        self.assertNotIn("client-id", response.text)
        self.assertNotIn("client-secret", response.text)

    def test_google_connect_can_read_downloaded_web_client_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "google-client.json"
            config_path.write_text(
                """
{
  "web": {
    "client_id": "json-client-id",
    "client_secret": "json-client-secret",
    "redirect_uris": ["http://localhost:8000/google/callback"]
  }
}
""".strip(),
                encoding="utf-8",
            )
            env = {
                "GOOGLE_CALENDAR_CLIENT_JSON": str(config_path),
                "DRASIL_TIMEZONE": "America/Vancouver",
                "DRASIL_TEST_ISOLATED_DB": "1",
            }
            with patch.dict(os.environ, env, clear=True):
                response = self.client.post("/api/v1/calendar-integrations/google/connect", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("json-client-id", payload["authorizationUrl"])
        self.assertEqual(payload["redirectUri"], "http://localhost:8000/google/callback")

    def test_google_oauth_callback_returns_auto_closing_popup_html(self) -> None:
        env = {
            "GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
            "GOOGLE_CALENDAR_REDIRECT_URI": "http://localhost:8000/google/callback",
            "DRASIL_FRONTEND_ORIGIN": "http://localhost:5173",
        }
        with Session(engine) as session:
            integration = session.exec(
                select(CalendarIntegration).where(CalendarIntegration.provider == "google")
            ).first()
            if not integration:
                integration = CalendarIntegration(provider="google")
            integration.oauth_state = "pending-state"
            integration.oauth_redirect_uri = "http://localhost:8000/google/callback"
            integration.time_zone = "America/Vancouver"
            session.add(integration)
            session.commit()

        def fake_google_request(method: str, url: str, **_kwargs: object) -> dict:
            if "oauth2.googleapis.com/token" in url:
                self.assertEqual(method, "POST")
                return {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            if url.endswith("/calendars"):
                self.assertEqual(method, "POST")
                return {"id": "drasil-calendar@example.com", "summary": "Drasil"}
            if "/events" in url:
                self.assertIn(method, {"POST", "PATCH"})
                return {"id": "google-event-from-connect", "etag": "etag-from-connect"}
            self.fail(f"Unexpected Google request: {method} {url}")

        with patch.dict(os.environ, env, clear=False), patch(
            "app.services.google_calendar._google_request",
            side_effect=fake_google_request,
        ):
            response = self.client.get(
                "/api/v1/calendar-integrations/google/callback?code=oauth-code&state=pending-state"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("drasil:google-calendar:oauth", response.text)
        self.assertIn('src="http://localhost:5173/oauth-popup.js"', response.text)
        self.assertIn('type="application/json" id="oauth-result"', response.text)
        self.assertNotIn('onclick=', response.text)
        popup_script = (Path(__file__).resolve().parents[2] / "frontend/public/oauth-popup.js").read_text()
        self.assertIn("window.opener.postMessage", popup_script)
        self.assertIn("window.close()", popup_script)

    def test_google_oauth_callback_pushes_existing_calendar_blocks(self) -> None:
        env = {
            "GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
            "GOOGLE_CALENDAR_REDIRECT_URI": "http://localhost:8000/google/callback",
            "DRASIL_FRONTEND_ORIGIN": "http://localhost:5173",
        }
        with Session(engine) as session:
            category = self._create_category(session, "Initial Push Work")
            task = self._create_task(session, category, "Push after connect")
            block = CalendarBlock(
                task_id=task.id,
                category_id=category.id,
                start_at=datetime(2026, 6, 5, 13, 0),
                end_at=datetime(2026, 6, 5, 14, 0),
                variant="scheduled",
            )
            session.add(block)
            integration = session.exec(
                select(CalendarIntegration).where(CalendarIntegration.provider == "google")
            ).first()
            if not integration:
                integration = CalendarIntegration(provider="google")
            integration.oauth_state = "push-state"
            integration.oauth_redirect_uri = "http://localhost:8000/google/callback"
            integration.time_zone = "America/Vancouver"
            session.add(integration)
            session.commit()
            session.refresh(block)
            block_id = block.id

        event_posts: list[str] = []

        def fake_google_request(method: str, url: str, **_kwargs: object) -> dict:
            if "oauth2.googleapis.com/token" in url:
                return {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            if url.endswith("/calendars"):
                return {"id": "drasil-calendar@example.com", "summary": "Drasil"}
            if "/events" in url:
                self.assertIn(method, {"POST", "PATCH"})
                event_posts.append(url)
                return {"id": f"google-event-{len(event_posts)}", "etag": "etag-pushed"}
            self.fail(f"Unexpected Google request: {method} {url}")

        with patch.dict(os.environ, env, clear=False), patch(
            "app.services.google_calendar._google_request",
            side_effect=fake_google_request,
        ):
            response = self.client.get(
                "/api/v1/calendar-integrations/google/callback?code=oauth-code&state=push-state"
            )

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(event_posts), 1)
        with Session(engine) as session:
            synced_block = session.get(CalendarBlock, block_id)
            self.assertTrue(synced_block.google_event_id)
            self.assertIsNone(synced_block.google_sync_error)

    def test_google_oauth_callback_error_returns_popup_html(self) -> None:
        response = self.client.get(
            "/api/v1/calendar-integrations/google/callback?error=access_denied&error_description=User%20cancelled"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Google Calendar connection failed", response.text)
        self.assertIn("User cancelled", response.text)

    def test_calendar_block_create_mirrors_to_google_when_connected(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Google Mirror Work")
            task = self._create_task(session, category, "Mirror me")
            self._create_connected_google_integration(session)
            category_id = category.id
            task_id = task.id

        def fake_google_request(method: str, url: str, **_kwargs: object) -> dict:
            self.assertEqual(method, "POST")
            self.assertIn("/events", url)
            return {"id": "google-event-123", "etag": "etag-123"}

        with patch("app.services.google_calendar._google_request", side_effect=fake_google_request):
            response = self.client.post(
                "/api/v1/calendar-blocks",
                json={
                    "taskId": task_id,
                    "categoryId": category_id,
                    "startAt": "2026-06-05T09:00:00-07:00",
                    "endAt": "2026-06-05T10:00:00-07:00",
                },
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["googleEventId"], "google-event-123")
        self.assertIsNone(payload["googleSyncError"])

    def test_cross_date_push_updates_one_linked_google_event_with_exact_endpoints(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Google Multi Day Push")
            task = self._create_task(session, category, "One canonical overnight event")
            self._create_connected_google_integration(session)
            category_id = category.id
            task_id = task.id
        self.addCleanup(self._cleanup_category_fixture, category_id)

        requests: list[tuple[str, str, dict]] = []

        def fake_google_request(method: str, url: str, **kwargs: object) -> dict:
            body = kwargs.get("body")
            self.assertIsInstance(body, dict)
            requests.append((method, url, body))
            return {"id": "google-multi-day-event", "etag": f"etag-{len(requests)}"}

        with patch("app.services.google_calendar._google_request", side_effect=fake_google_request):
            create_response = self.client.post(
                "/api/v1/calendar-blocks",
                json={
                    "taskId": task_id,
                    "categoryId": category_id,
                    "startAt": "2026-07-13T23:30:00",
                    "endAt": "2026-07-15T01:00:00",
                },
            )
            self.assertEqual(create_response.status_code, 201)
            block_id = create_response.json()["id"]

            update_payload = {
                "startAt": "2026-07-14T22:45:00",
                "endAt": "2026-07-17T00:15:00",
            }
            for _ in range(2):
                update_response = self.client.patch(
                    f"/api/v1/calendar-blocks/{block_id}",
                    json=update_payload,
                )
                self.assertEqual(update_response.status_code, 200)
                self.assertEqual(update_response.json()["id"], block_id)
                self.assertEqual(update_response.json()["googleEventId"], "google-multi-day-event")

        self.assertEqual([method for method, _, _ in requests], ["POST", "PATCH", "PATCH"])
        self.assertTrue(requests[0][1].endswith("/events"))
        self.assertTrue(requests[1][1].endswith("/events/google-multi-day-event"))
        self.assertEqual(requests[1][1], requests[2][1])
        first_body = requests[0][2]
        self.assertEqual(first_body["start"], {"dateTime": "2026-07-13T23:30:00", "timeZone": "America/Vancouver"})
        self.assertEqual(first_body["end"], {"dateTime": "2026-07-15T01:00:00", "timeZone": "America/Vancouver"})
        self.assertEqual(first_body["extendedProperties"]["private"]["drasilBlockId"], str(block_id))
        self.assertEqual(requests[1][2]["start"]["dateTime"], update_payload["startAt"])
        self.assertEqual(requests[1][2]["end"]["dateTime"], update_payload["endAt"])
        with Session(engine) as session:
            self.assertEqual(
                len(session.exec(select(CalendarBlock).where(CalendarBlock.id == block_id)).all()),
                1,
            )

    def test_google_pull_sync_updates_only_block_time(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Google Pull Work")
            task = self._create_task(session, category, "Keep Drasil title")
            self._create_connected_google_integration(session)
            block = CalendarBlock(
                task_id=task.id,
                category_id=category.id,
                start_at=datetime(2026, 6, 5, 9, 0),
                end_at=datetime(2026, 6, 5, 10, 0),
                variant="scheduled",
                google_event_id="google-event-pull",
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            block_id = block.id
            task_id = task.id

        def fake_google_request(method: str, _url: str, **_kwargs: object) -> dict:
            self.assertEqual(method, "GET")
            return {
                "items": [
                    {
                        "id": "google-event-pull",
                        "status": "confirmed",
                        "summary": "User edited this title but Drasil ignores it",
                        "etag": "etag-pull",
                        "start": {"dateTime": "2026-06-05T11:15:00-07:00"},
                        "end": {"dateTime": "2026-06-05T12:00:00-07:00"},
                        "extendedProperties": {
                            "private": {
                                "drasilSource": "drasil",
                                "drasilBlockId": str(block_id),
                            }
                        },
                    }
                ]
            }

        with patch("app.services.google_calendar._google_request", side_effect=fake_google_request):
            response = self.client.post(
                "/api/v1/calendar-integrations/google/sync",
                json={"direction": "pull"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pull"], {"updated": 1, "scanned": 1})
        with Session(engine) as session:
            updated_block = session.get(CalendarBlock, block_id)
            updated_task = session.get(StrategyNode, task_id)
            self.assertEqual(updated_block.start_at, datetime(2026, 6, 5, 11, 15))
            self.assertEqual(updated_block.end_at, datetime(2026, 6, 5, 12, 0))
            self.assertEqual(updated_task.title, "Keep Drasil title")

    def test_google_pull_sync_converts_utc_event_time_to_integration_timezone(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Google Pull Timezone Work")
            task = self._create_task(session, category, "Keep local wall time")
            self._create_connected_google_integration(session)
            block = CalendarBlock(
                task_id=task.id,
                category_id=category.id,
                start_at=datetime(2026, 6, 5, 9, 0),
                end_at=datetime(2026, 6, 5, 10, 0),
                variant="scheduled",
                google_event_id="google-event-utc-pull",
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            block_id = block.id

        def fake_google_request(method: str, _url: str, **_kwargs: object) -> dict:
            self.assertEqual(method, "GET")
            return {
                "items": [
                    {
                        "id": "google-event-utc-pull",
                        "status": "confirmed",
                        "start": {"dateTime": "2026-06-05T18:15:00Z"},
                        "end": {"dateTime": "2026-06-05T19:00:00Z"},
                        "extendedProperties": {
                            "private": {
                                "drasilSource": "drasil",
                                "drasilBlockId": str(block_id),
                            }
                        },
                    }
                ]
            }

        with patch("app.services.google_calendar._google_request", side_effect=fake_google_request):
            response = self.client.post(
                "/api/v1/calendar-integrations/google/sync",
                json={"direction": "pull"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pull"], {"updated": 1, "scanned": 1})
        with Session(engine) as session:
            updated_block = session.get(CalendarBlock, block_id)
            self.assertEqual(updated_block.start_at, datetime(2026, 6, 5, 11, 15))
            self.assertEqual(updated_block.end_at, datetime(2026, 6, 5, 12, 0))

    def test_repeated_utc_pull_preserves_one_cross_date_linked_block(self) -> None:
        with Session(engine) as session:
            category = self._create_category(session, "Google Multi Day Pull")
            task = self._create_task(session, category, "UTC overnight pull")
            self._create_connected_google_integration(session)
            block = CalendarBlock(
                task_id=task.id,
                category_id=category.id,
                start_at=datetime(2026, 7, 13, 9, 0),
                end_at=datetime(2026, 7, 13, 10, 0),
                variant="scheduled",
                google_event_id="google-cross-date-pull",
            )
            session.add(block)
            session.commit()
            session.refresh(block)
            block_id = block.id
            category_id = category.id
        self.addCleanup(self._cleanup_category_fixture, category_id)

        def fake_google_request(method: str, _url: str, **_kwargs: object) -> dict:
            self.assertEqual(method, "GET")
            return {
                "items": [
                    {
                        "id": "google-cross-date-pull",
                        "status": "confirmed",
                        "etag": "cross-date-etag",
                        "start": {"dateTime": "2026-07-14T06:30:00Z"},
                        "end": {"dateTime": "2026-07-16T08:00:00Z"},
                        "extendedProperties": {
                            "private": {
                                "drasilSource": "drasil",
                                "drasilBlockId": str(block_id),
                            }
                        },
                    }
                ]
            }

        with patch("app.services.google_calendar._google_request", side_effect=fake_google_request):
            first = self.client.post(
                "/api/v1/calendar-integrations/google/sync",
                json={"direction": "pull"},
            )
            second = self.client.post(
                "/api/v1/calendar-integrations/google/sync",
                json={"direction": "pull"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["pull"], {"updated": 1, "scanned": 1})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["pull"], {"updated": 0, "scanned": 1})
        with Session(engine) as session:
            rows = session.exec(select(CalendarBlock).where(CalendarBlock.id == block_id)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].google_event_id, "google-cross-date-pull")
            self.assertEqual(rows[0].start_at, datetime(2026, 7, 13, 23, 30))
            self.assertEqual(rows[0].end_at, datetime(2026, 7, 16, 1, 0))


if __name__ == "__main__":
    unittest.main()
