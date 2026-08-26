from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from app.api.accounts import router as accounts_router
from app.db import database as database_module
from app.db.database import get_active_engine, initialize_account_database
from app.models.category import Category
from app.services.account_auth import (
    AccountContextMiddleware,
    account_private_path,
    authenticate_account,
    create_account,
    create_session,
    resolve_session,
    use_account,
)


class AccountIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "DRASIL_TEST_ISOLATED_DB": "0",
                "DRASIL_AUTH_DATABASE_PATH": str(self.root / "auth.db"),
                "DRASIL_ACCOUNT_DATA_ROOT": str(self.root / "accounts"),
                "DRASIL_LEGACY_DATABASE_PATH": str(self.root / "missing-legacy.db"),
                "DRASIL_BOOTSTRAP_TOKEN": "remote-first-run-token",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        for account_engine in database_module._ACCOUNT_ENGINES.values():
            account_engine.dispose()
        database_module._ACCOUNT_ENGINES.clear()
        self.environment.stop()
        self.temp_directory.cleanup()

    def test_accounts_use_separate_databases_and_private_directories(self) -> None:
        alex = create_account(
            username="alex",
            display_name="Alex",
            password="correct-horse-alex",
        )
        blair = create_account(
            username="blair",
            display_name="Blair",
            password="correct-horse-blair",
        )
        initialize_account_database(alex)
        initialize_account_database(blair)

        with use_account(alex):
            with Session(get_active_engine()) as session:
                session.add(
                    Category(
                        name="Alex private category",
                        accent_color="#112233",
                        icon_slug="circle",
                        sort_order=99,
                    )
                )
                session.commit()
            alex_private_path = account_private_path(".strategist-provider.json")
            alex_private_path.write_text('{"owner":"alex"}', encoding="utf-8")

        with use_account(blair):
            with Session(get_active_engine()) as session:
                leaked = session.exec(
                    select(Category).where(Category.name == "Alex private category")
                ).first()
            blair_private_path = account_private_path(".strategist-provider.json")

        self.assertIsNone(leaked)
        self.assertNotEqual(alex.database_path, blair.database_path)
        self.assertNotEqual(alex_private_path, blair_private_path)
        self.assertFalse(blair_private_path.exists())

    def test_new_accounts_receive_the_default_workspaces(self) -> None:
        expected_workspaces = [
            "Work",
            "Health",
            "Home",
            "Finance",
            "Social",
            "Personal Growth",
            "Leisure",
        ]
        account = create_account(
            username="community-user",
            display_name="Community User",
            password="community-password-123",
        )

        initialize_account_database(account)

        with use_account(account):
            with Session(get_active_engine()) as session:
                workspaces = session.exec(
                    select(Category).order_by(Category.sort_order)
                ).all()

        self.assertEqual(
            [workspace.name for workspace in workspaces],
            expected_workspaces,
        )

    def test_passwords_are_hashed_and_sessions_are_account_scoped(self) -> None:
        account = create_account(
            username="casey",
            display_name="Casey",
            password="a-long-private-password",
        )
        self.assertIsNone(authenticate_account("casey", "wrong-password"))
        self.assertEqual(
            authenticate_account("CASEY", "a-long-private-password"), account
        )
        token = create_session(account)
        self.assertEqual(resolve_session(token), account)
        self.assertIsNone(resolve_session(f"{token}-tampered"))
        self.assertNotIn(b"a-long-private-password", (self.root / "auth.db").read_bytes())

    def test_account_api_bootstrap_login_add_and_switch(self) -> None:
        application = FastAPI()
        application.include_router(accounts_router)
        application.add_middleware(AccountContextMiddleware)

        with TestClient(application) as client:
            self.assertTrue(client.get("/api/v1/accounts/status").json()["needsBootstrap"])
            bootstrap = client.post(
                "/api/v1/accounts/bootstrap",
                json={
                    "username": "owner",
                    "displayName": "Owner",
                    "password": "owner-password-123",
                    "bootstrapToken": "remote-first-run-token",
                },
            )
            self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
            owner_session = bootstrap.json()
            owner_headers = {"Authorization": f"Bearer {owner_session['token']}"}
            me = client.get("/api/v1/accounts/me", headers=owner_headers)
            self.assertEqual(me.json()["username"], "owner")

            created = client.post(
                "/api/v1/accounts",
                headers=owner_headers,
                json={
                    "username": "member",
                    "displayName": "Member",
                    "password": "member-password-123",
                    "isAdmin": False,
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            member_login = client.post(
                "/api/v1/accounts/login",
                json={"username": "member", "password": "member-password-123"},
            )
            self.assertEqual(member_login.status_code, 200, member_login.text)
            member_headers = {
                "Authorization": f"Bearer {member_login.json()['token']}"
            }
            member = client.get("/api/v1/accounts/me", headers=member_headers)
            self.assertEqual(member.json()["displayName"], "Member")

            changed_password = client.post(
                "/api/v1/accounts/password",
                headers=member_headers,
                json={
                    "currentPassword": "member-password-123",
                    "newPassword": "member-password-456",
                },
            )
            self.assertEqual(changed_password.status_code, 200, changed_password.text)
            self.assertEqual(
                client.get("/api/v1/accounts/me", headers=member_headers).status_code,
                401,
            )
            member_headers = {
                "Authorization": f"Bearer {changed_password.json()['token']}"
            }

            forbidden = client.post(
                "/api/v1/accounts",
                headers=member_headers,
                json={
                    "username": "third",
                    "displayName": "Third",
                    "password": "third-password-123",
                },
            )
            self.assertEqual(forbidden.status_code, 403)

    def test_remote_bootstrap_rejects_a_wrong_setup_token(self) -> None:
        application = FastAPI()
        application.include_router(accounts_router)
        application.add_middleware(AccountContextMiddleware)
        with TestClient(application) as client:
            response = client.post(
                "/api/v1/accounts/bootstrap",
                json={
                    "username": "owner",
                    "displayName": "Owner",
                    "password": "owner-password-123",
                    "bootstrapToken": "wrong",
                },
            )
        self.assertEqual(response.status_code, 403)

    def test_remote_frontend_receives_authentication_errors_through_cors(self) -> None:
        application = FastAPI()
        application.include_router(accounts_router)
        application.add_middleware(AccountContextMiddleware)
        application.add_middleware(
            CORSMiddleware,
            allow_origins=["https://frontend.example"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        with TestClient(application) as client:
            response = client.get(
                "/api/v1/accounts/me",
                headers={"Origin": "https://frontend.example"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://frontend.example",
        )


if __name__ == "__main__":
    unittest.main()
