"""Saved sessions must upgrade an account before its first workspace query."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.nodes import router as nodes_router
from app.db import database
from app.models.category import Category
from app.models.node import StrategyNode
from app.services.account_auth import AccountContextMiddleware, AccountRecord, SESSION_COOKIE, use_account


class HostedSessionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountRecord('saved-session-account', 'owner', 'Owner', True, Path('.'), Path('unused.db'))
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(Category(id=1, name='Work'))
            session.add(StrategyNode(id=1, category_id=1, title='Existing task'))
            session.commit()
        with self.engine.begin() as connection:
            connection.execute(text('ALTER TABLE strategy_nodes DROP COLUMN planned_date'))
        for patcher in (
            patch.dict(os.environ, {'DRASIL_TEST_ISOLATED_DB': '0'}),
            patch.object(database, 'hosted_storage', return_value=True),
            patch.object(database, 'engine_for_account', return_value=self.engine),
            patch.object(database, '_HOSTED_INITIALIZED', set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def migrate(self, account):
        self.assertEqual(account, self.account)
        with self.engine.begin() as connection:
            connection.execute(text('ALTER TABLE strategy_nodes ADD COLUMN planned_date VARCHAR(10)'))

    def test_existing_cookie_upgrades_before_task_query_without_login(self):
        app = FastAPI()
        app.include_router(nodes_router)
        app.add_middleware(AccountContextMiddleware)
        with patch('app.services.account_auth.hosted_runtime', return_value=True), \
                patch('app.services.account_auth.resolve_session', return_value=self.account) as resolve, \
                patch.object(database, '_initialize_postgres_account', side_effect=self.migrate) as migrate, \
                TestClient(app, base_url='https://testserver') as client:
            client.cookies.set(SESSION_COOKIE, 'existing-session-token')
            for _ in range(2):
                response = client.get('/api/v1/nodes?categoryId=1')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()[0]['title'], 'Existing task')
                self.assertIsNone(response.json()[0]['plannedDate'])
            resolve.assert_called_with('existing-session-token')
            migrate.assert_called_once_with(self.account)

    def test_failed_migration_is_retried_before_allowing_database_access(self):
        with use_account(self.account), patch.object(database, '_initialize_postgres_account', side_effect=RuntimeError('database temporarily unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
                database.get_active_engine()
            self.assertNotIn(self.account.id, database._HOSTED_INITIALIZED)
        with use_account(self.account), patch.object(database, '_initialize_postgres_account', side_effect=self.migrate):
            with Session(database.get_active_engine()) as session:
                self.assertEqual(session.get(StrategyNode, 1).title, 'Existing task')


if __name__ == '__main__':
    unittest.main()
