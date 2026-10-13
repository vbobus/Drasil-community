import unittest
import os
from unittest.mock import patch
from datetime import datetime, timedelta
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from app.main import app as _app
from app.models.companion import CompanionRun
from app.models.node import StrategyNode
from app.services import companion as service
from app.api.companion import complete, Completion, unpair, update_settings, SettingsUpdate
from starlette.requests import Request
from app.services.account_auth import AccountContextMiddleware


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.token = service.issue_worker_token(self.session, str(uuid4()))
        value = service.settings(self.session)
        value.enabled = True
        value.check_in_hours = ''
        self.session.add(value)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def claim(self, **kwargs):
        return service.claim(self.session, worker_status='ready', memory_status='ready', **kwargs)['run']

    def test_scope_secrets_and_idempotent_completion(self):
        self.assertNotIn(self.token, str(service.public_settings(service.settings(self.session))))
        service.enqueue(self.session, 'Help me prepare')
        run = self.claim()
        self.assertIsNone(self.claim())
        body = Completion(lease=run['lease'], status='completed', message='What is the smallest useful step?')
        first = complete(run['id'], body, self.session)
        second = complete(run['id'], Completion(lease=run['lease'], status='completed', message='Overwrite'), self.session)
        self.assertEqual(first, second)
        self.assertNotIn('lease_hash', first)
        self.assertFalse(self.session.exec(select(StrategyNode)).all())

    def test_pause_rejects_inflight_result(self):
        service.enqueue(self.session, 'Hi')
        run = self.claim()
        update_settings(SettingsUpdate(enabled=False), self.session)
        result = complete(run['id'], Completion(lease=run['lease'], status='completed', message='Late'), self.session)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['result'])

    def test_unpair_cancels_work_and_prevents_enqueue(self):
        service.enqueue(self.session, 'Hi')
        unpair(self.session)
        self.assertEqual(self.session.exec(select(CompanionRun)).one().status, 'failed')
        with self.assertRaises(HTTPException): service.enqueue(self.session, 'Hi again')
        self.assertFalse(service.settings(self.session).worker_token_hash)

    def test_bad_lease_and_quiet_direct_message_rejected(self):
        service.enqueue(self.session, 'Hi')
        run = self.claim()
        with self.assertRaises(HTTPException): service.validate_lease(self.session, run['id'], 'wrong')
        with self.assertRaises(HTTPException):
            complete(run['id'], Completion(lease=run['lease'], status='completed', quiet=True), self.session)

    def test_memory_unavailable_does_not_start_work(self):
        service.enqueue(self.session, 'Hi')
        self.assertIsNone(service.claim(self.session, worker_status='ready', memory_status='unavailable')['run'])
        self.assertEqual(self.session.exec(select(CompanionRun)).one().status, 'queued')

    def test_expired_lease_is_failed_and_daily_limit_holds(self):
        value = service.settings(self.session); value.daily_run_limit = 1
        self.session.add(value); self.session.commit()
        service.enqueue(self.session, 'Hi')
        run = self.claim()
        row = self.session.get(CompanionRun, run['id']); row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        self.session.add(row); self.session.commit()
        service.enqueue(self.session, 'Next')
        self.assertIsNone(self.claim())
        self.assertEqual(self.session.get(CompanionRun, run['id']).status, 'failed')

    def test_scheduled_check_in_is_once_per_window(self):
        value = service.settings(self.session); value.check_in_hours = '9'; value.time_zone = 'UTC'
        self.session.add(value); self.session.commit()
        run = self.claim(now=datetime(2026, 10, 2, 9, 10))
        row = self.session.get(CompanionRun, run['id']); row.status = 'completed'
        self.session.add(row); self.session.commit()
        self.assertIsNone(self.claim(now=datetime(2026, 10, 2, 10)))
        self.assertIsNone(self.claim(now=datetime(2026, 10, 3, 12)))
        self.assertIsNotNone(self.claim(now=datetime(2026, 10, 4, 9)))

    def test_context_schema_and_account_data_are_serializable(self):
        import json
        context = service.context(self.session)
        json.dumps(context, default=str)
        self.assertEqual(context['tasks'], [])
        self.assertNotIn(self.token, str(context))


if __name__ == '__main__': unittest.main()


class WorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_token_cannot_reach_task_mutations(self):
        middleware = AccountContextMiddleware(_app)
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/v1/nodes',
            'headers': [(b'authorization', b'Bearer dcw.invalid.secret')], 'query_string': b''})
        async def unreachable(_):
            self.fail('Worker reached general task API')
        with patch.dict(os.environ, {'DRASIL_TEST_ISOLATED_DB': '0'}), patch('app.services.account_auth.hosted_runtime', return_value=False):
            response = await middleware.dispatch(request, unreachable)
        self.assertEqual(response.status_code, 403)

    async def test_invalid_worker_is_rejected(self):
        middleware = AccountContextMiddleware(_app)
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/v1/companion/worker/claim',
            'headers': [(b'authorization', b'Bearer dcw.invalid.secret')], 'query_string': b''})
        async def unreachable(_): self.fail('Invalid worker reached claim')
        with patch.dict(os.environ, {'DRASIL_TEST_ISOLATED_DB': '0'}), patch('app.services.account_auth.hosted_runtime', return_value=False):
            response = await middleware.dispatch(request, unreachable)
        self.assertEqual(response.status_code, 401)
