import json
import queue
import unittest
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.services import codex_sandbox as cs


class CodexSandboxTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {"DRASIL_CODEX_SANDBOX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_status_never_exposes_tokens_or_worker_identity(self):
        status = cs.public_status(
            {
                "isAuthenticated": True,
                "access_token": "secret",
                "name": "private-worker",
                "pendingSession": {
                    "status": "pending",
                    "userCode": "TEST-CODE",
                    "refresh_token": "secret",
                },
            }
        )
        self.assertNotIn("secret", json.dumps(status))
        self.assertNotIn("private-worker", json.dumps(status))
        self.assertEqual(status["pendingSession"]["userCode"], "TEST-CODE")

    def test_rejects_untrusted_login_url(self):
        with self.assertRaises(ValueError):
            cs.public_status({"pendingSession": {"verificationUri": "https://evil.example/"}})

    def test_connected_status_does_not_start_or_contact_sandbox(self):
        with (
            patch.object(cs, "load_settings", return_value={"status": {"isAuthenticated": True}}),
            patch.object(cs, "new_client") as client,
        ):
            self.assertTrue(cs.get_status()["isAuthenticated"])
            client.assert_not_called()

    def test_disconnected_status_does_not_allocate_sandbox(self):
        with (
            patch.object(cs, "load_settings", return_value={}),
            patch.object(cs, "new_client") as client,
        ):
            self.assertFalse(cs.get_status()["isAuthenticated"])
            client.assert_not_called()

    def test_pending_status_does_not_wake_stopped_worker(self):
        state = {"name": "private-worker", "status": {"pendingSession": {"status": "pending"}}}
        client = MagicMock()
        client.get_sandbox.return_value.status = "stopped"
        with (
            patch.object(cs, "account_lock", return_value=nullcontext()),
            patch.object(cs, "load_settings", return_value=state),
            patch.object(cs, "save_settings"),
            patch.object(cs, "new_client", return_value=client),
        ):
            self.assertFalse(cs.get_status()["isAuthenticated"])
            client.get_sandbox.return_value.fs.read_text.assert_not_called()
            client.get_or_create_sandbox.assert_not_called()

    def test_deadline_tracks_last_activity_not_constant_extensions(self):
        box = MagicMock()
        session = box.current_session
        session.started_at = 1000
        session.execution_time_limit = timedelta(seconds=300)
        with patch.object(cs.time, "time", return_value=1060):
            cs.keep_awake(box)
        session.extend_execution_time_limit.assert_called_once_with(60)

    def test_deadline_never_exceeds_hobby_session_cap(self):
        box = MagicMock()
        box.current_session.started_at = 1000
        box.current_session.execution_time_limit = timedelta(seconds=2600)
        with patch.object(cs.time, "time", return_value=3599):
            cs.keep_awake(box)
        box.current_session.extend_execution_time_limit.assert_called_once_with(100)

    def test_transport_fails_closed_before_allocating_for_disconnected_account(self):
        with (
            patch.object(cs, "account_lock", return_value=nullcontext()),
            patch.object(cs, "load_settings", return_value={}),
            patch.object(cs, "new_client") as client,
        ):
            transport = cs.SandboxTransport(queue.Queue())
            with self.assertRaises(HTTPException) as error:
                transport.start()
            self.assertEqual(error.exception.status_code, 401)
            client.assert_not_called()
            self.assertFalse(transport.locked)

    def test_cancel_terminates_worker_and_releases_lock(self):
        transport = cs.SandboxTransport(queue.Queue())
        transport.process = MagicMock()
        transport.client = MagicMock()
        transport.box = MagicMock()
        transport.lock = MagicMock()
        transport.locked = True
        with patch.object(cs, "keep_awake"):
            transport.close()
        transport.process.terminate.assert_called_once()
        transport.client.close.assert_called_once()
        transport.lock.__exit__.assert_called_once()

    def test_logout_deletes_credentials_and_snapshots_before_clearing_mapping(self):
        client = MagicMock()
        with (
            patch.object(cs, "account_lock", return_value=nullcontext()),
            patch.object(cs, "load_settings", return_value={"name": "owned-worker"}),
            patch.object(cs, "new_client", return_value=client),
            patch.object(cs, "save_settings") as save,
        ):
            self.assertFalse(cs.logout()["isAuthenticated"])
            client.get_sandbox.assert_called_once_with(name="owned-worker")
            client.get_sandbox.return_value.destroy.assert_called_once_with(
                delete_orphan_snapshots=True
            )
            save.assert_called_once_with(cs.KEY, {})

    def test_hosted_preflight_uses_subscription_state_without_local_cli(self):
        from app.services.agent_kernel.provider_transport import provider_transport_readiness

        with (
            patch.object(cs, "get_status", return_value={"isAuthenticated": True}),
            patch(
                "app.services.agent_kernel.provider_transport.find_codex_cli_path",
                return_value=None,
            ),
        ):
            result = provider_transport_readiness({"provider": "codex-cli"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["source"], "sandbox_subscription_preflight")


if __name__ == "__main__":
    unittest.main()
