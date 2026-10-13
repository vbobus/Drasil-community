"""Loading must stay account-scoped, concurrent, and free of eager AI imports."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app.api.workspace import router
from app.core.request_timing import RequestTimingMiddleware
from app.db.database import get_session
from app.models.category import Category
from app.models.node import StrategyNode
from app.models.calendar_block import CalendarBlock
from app.services.account_auth import AccountContextMiddleware, AccountRecord, active_account
from datetime import datetime


class AuthenticationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_auth_preserves_each_request_account(self):
        app = FastAPI()
        app.add_middleware(AccountContextMiddleware)
        app.add_middleware(RequestTimingMiddleware)
        current, maximum = 0, 0
        lock = threading.Lock()

        def resolve(token):
            nonlocal current, maximum
            with lock:
                current += 1
                maximum = max(maximum, current)
            time.sleep(.1)
            with lock:
                current -= 1
            return AccountRecord(token, token, token, False, Path('.'), Path('unused'))

        @app.get('/api/v1/protected')
        def protected():
            return {'account': active_account().id}

        with patch.dict(os.environ, {'DRASIL_TEST_ISOLATED_DB': '0'}), \
                patch('app.services.account_auth.resolve_session', side_effect=resolve):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                responses = await asyncio.gather(*[client.get('/api/v1/protected', headers={'Authorization': f'Bearer account-{i}'}) for i in range(5)])
        self.assertGreater(maximum, 1, 'Session lookup must not block the event loop serially.')
        self.assertEqual([r.json()['account'] for r in responses], [f'account-{i}' for i in range(5)])
        self.assertTrue(all('auth;dur=' in r.headers['server-timing'] for r in responses))
        self.assertEqual(len({r.headers['x-request-id'] for r in responses}), 5)


class WorkspaceBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all([Category(id=1, name='Work'), Category(id=2, name='Life')])
            session.add_all([StrategyNode(id=10, category_id=1, title='Parent', planned_date='2026-10-07'),
                            StrategyNode(id=11, category_id=1, parent_id=10, title='Child', planned_date='2026-10-08'),
                            StrategyNode(id=12, category_id=2, title='Future')])
            session.add_all([CalendarBlock(id=1, task_id=10, category_id=1, start_at=datetime(2026,10,7,9), end_at=datetime(2026,10,7,10)),
                            CalendarBlock(id=2, task_id=12, category_id=2, start_at=datetime(2027,1,1,9), end_at=datetime(2027,1,1,10))])
            session.commit()
        self.app = FastAPI()
        self.app.include_router(router)
        def session_override():
            with Session(self.engine) as session:
                yield session
        self.app.dependency_overrides[get_session] = session_override

    def test_bootstrap_shares_tasks_but_membership_is_global(self):
        with TestClient(self.app) as client:
            result = client.get('/api/v1/workspace?start=2026-10-01T00:00:00&end=2026-11-01T00:00:00')
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertEqual(len(data['categories']), 2)
        self.assertEqual([n['id'] for n in data['nodes']], [10, 11, 12])
        self.assertEqual(data['nodes'][1]['parentId'], 10)
        self.assertEqual(data['nodes'][1]['plannedDate'], '2026-10-08')
        self.assertEqual([b['taskId'] for b in data['blocks']], [10])
        self.assertEqual(data['scheduledTaskIds'], [10, 12])

    def test_invalid_range_and_unauthenticated_access_are_rejected(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get('/api/v1/workspace?start=2026-01-01&end=2027-01-01').status_code, 400)
        protected_app = FastAPI()
        protected_app.include_router(router)
        protected_app.dependency_overrides = self.app.dependency_overrides
        protected_app.add_middleware(AccountContextMiddleware)
        with patch.dict(os.environ, {'DRASIL_TEST_ISOLATED_DB': '0'}), patch('app.services.account_auth.resolve_session', return_value=None), TestClient(protected_app) as client:
            self.assertEqual(client.get('/api/v1/workspace?start=2026-10-01&end=2026-11-01').status_code, 401)

    def test_versioned_account_skips_ddl_on_a_fresh_instance(self):
        from app.db import database
        from app.db.schema_version import ACCOUNT_SCHEMA_VERSION
        account = AccountRecord('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'owner', 'Owner', False, Path('.'), Path('unused'))
        connection = MagicMock()
        connection.execute.side_effect = [MagicMock(scalar=lambda: 'drasil_schema_version'), MagicMock(scalar=lambda: ACCOUNT_SCHEMA_VERSION)]
        engine = MagicMock()
        engine.begin.return_value.__enter__.return_value = connection
        with patch.object(database, 'engine_for_account', return_value=engine), patch.object(SQLModel.metadata, 'create_all') as create_all:
            database._initialize_postgres_account(account)
        create_all.assert_not_called()
        self.assertEqual(connection.execute.call_count, 2)

    def test_production_import_does_not_load_heavy_dependencies(self):
        backend = Path(__file__).resolve().parents[1]
        script = "import sys;sys.modules['app.private_extensions']=None;import app.main;assert not any(m in sys.modules for m in ('numpy','pandas','ortools','langgraph'))"
        result = subprocess.run([sys.executable, '-c', script], cwd=backend, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
