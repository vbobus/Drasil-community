import unittest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from app.services import codex_auth


class CodexBrowserAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.request.side_effect = [
            {"result": {}},
            {"result": {"loginId": "login-one", "authUrl": "https://auth.openai.com/oauth/authorize?state=test"}},
        ]
        self.patches = [
            patch.object(codex_auth, '_ACTIVE_SESSIONS', {}),
            patch.object(codex_auth, 'get_auth_status', return_value={"isAuthenticated": False}),
            patch.object(codex_auth, '_active_session_key', return_value='account-one'),
            patch.object(codex_auth, 'get_codex_path', return_value='codex'),
            patch.object(codex_auth, 'account_process_environment', return_value={"CODEX_HOME": ".tmp-test-home"}),
            patch('app.services.agent_kernel.codex_app_server.CodexAppServerJsonRpcClient', return_value=self.client),
            patch.object(codex_auth.threading, 'Thread'),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_browser_signin_uses_official_app_server_and_reuses_pending_attempt(self):
        status = codex_auth.start_browser_auth()
        self.assertEqual(status['pendingSession']['verificationUri'], 'https://auth.openai.com/oauth/authorize?state=test')
        self.assertIsNone(status['pendingSession']['userCode'])
        self.client.request.assert_any_call('account/login/start', {'type': 'chatgpt'}, timeout_seconds=15)
        self.assertEqual(codex_auth.start_browser_auth(), status)
        self.assertEqual(self.client.request.call_count, 2)

    def test_login_url_cannot_redirect_to_an_untrusted_site(self):
        self.client.request.side_effect = [{"result": {}}, {"result": {"loginId": "x", "authUrl": "https://attacker.example/auth"}}]
        with self.assertRaises(HTTPException):
            codex_auth.start_browser_auth()
        self.client.close.assert_called_once()
        self.assertEqual(codex_auth._ACTIVE_SESSIONS, {})

    def test_completion_notification_closes_the_login_process(self):
        thread = codex_auth.threading.Thread
        self.client.read_message.return_value = ('stdout', {
            'method': 'account/login/completed', 'params': {'loginId': 'login-one', 'success': True},
        })
        codex_auth.start_browser_auth()
        thread.call_args.kwargs['target']()
        self.assertEqual(codex_auth._ACTIVE_SESSIONS['account-one'].status, 'completed')
        self.client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
