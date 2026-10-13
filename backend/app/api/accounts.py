"""Drasil account onboarding, login, switching, and administration."""

from __future__ import annotations

import sqlite3
import os

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from app.db.hosted import hosted_runtime
from pydantic import BaseModel, ConfigDict, Field, alias_generators

from app.services.account_auth import (
    account_count,
    active_account,
    assert_login_attempt_allowed,
    authenticate_account,
    bootstrap_request_allowed,
    change_account_password,
    create_account,
    create_session,
    list_accounts,
    record_login_result,
    request_token,
    revoke_session,
    SESSION_COOKIE,
)

router = APIRouter(prefix="/api/v1/accounts", tags=["Accounts"])


class AccountCredentials(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)


class BootstrapAccountRequest(AccountCredentials):
    display_name: str = Field(min_length=1, max_length=80)
    bootstrap_token: str | None = Field(default=None, max_length=512)


class CreateAccountRequest(AccountCredentials):
    display_name: str = Field(min_length=1, max_length=80)
    is_admin: bool = False


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=10, max_length=512)


class AccountSessionResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=alias_generators.to_camel,
        populate_by_name=True,
    )

    token: str
    account: dict[str, object]


def _session_response(account, response: Response) -> dict[str, object]:
    token = create_session(account)
    if hosted_runtime():
        response.set_cookie(SESSION_COOKIE, token, secure=True, httponly=True,
                            samesite="strict", path="/")
        # The browser never needs a readable bearer credential on the hosted site.
        return {"token": "", "account": account.public()}
    return {"token": token, "account": account.public()}


@router.get("/status")
def account_status() -> dict[str, object]:
    test_mode = os.getenv("DRASIL_TEST_ISOLATED_DB") == "1"
    return {
        "needsBootstrap": False if test_mode else account_count() == 0,
        "authenticationRequired": not test_mode,
        "supportsRemoteBackend": True,
        "cookieSessions": hosted_runtime(),
    }


@router.post("/bootstrap", response_model=AccountSessionResponse)
def bootstrap_account(body: BootstrapAccountRequest, request: Request, response: Response) -> dict[str, object]:
    assert_login_attempt_allowed(request, "bootstrap")
    if account_count() != 0:
        raise HTTPException(status_code=409, detail="Drasil has already been initialized.")
    if not bootstrap_request_allowed(request, body.bootstrap_token):
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote first-run setup requires DRASIL_BOOTSTRAP_TOKEN on the backend."
            ),
        )
    try:
        account = create_account(
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            is_admin=True,
            first_account_only=True,
        )
    except (sqlite3.IntegrityError, IntegrityError) as error:
        raise HTTPException(status_code=409, detail="An account with that username already exists.") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    # Import lazily so the registry remains independent of application data.
    from app.db.database import initialize_account_database

    initialize_account_database(account)
    return _session_response(account, response)


@router.post("/login", response_model=AccountSessionResponse)
def login(body: AccountCredentials, request: Request, response: Response) -> dict[str, object]:
    login_key = assert_login_attempt_allowed(request, body.username)
    account = authenticate_account(body.username, body.password)
    if account is None:
        record_login_result(login_key, succeeded=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username or password is incorrect.",
        )
    record_login_result(login_key, succeeded=True)
    from app.db.database import initialize_account_database
    initialize_account_database(account)
    return _session_response(account, response)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    revoke_session(getattr(request.state, "drasil_session_token", request_token(request)))
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


@router.get("/me")
def current_account() -> dict[str, object]:
    account = active_account()
    assert account is not None
    return account.public()


@router.post("/password", response_model=AccountSessionResponse)
def change_password(body: ChangePasswordRequest, request: Request, response: Response) -> dict[str, object]:
    account = active_account()
    assert account is not None
    assert_login_attempt_allowed(request, account.username)
    try:
        changed = change_account_password(
            account,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not changed:
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    return _session_response(account, response)


@router.get("")
def account_index() -> list[dict[str, object]]:
    actor = active_account()
    if actor is not None and not actor.is_admin:
        return [actor.public()]
    return [account.public() for account in list_accounts()]


@router.post("", response_model=dict[str, object])
def add_account(body: CreateAccountRequest) -> dict[str, object]:
    actor = active_account()
    if actor is None or not actor.is_admin:
        raise HTTPException(status_code=403, detail="Only a Drasil administrator can add accounts.")
    try:
        account = create_account(
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            is_admin=body.is_admin,
        )
    except (sqlite3.IntegrityError, IntegrityError) as error:
        raise HTTPException(status_code=409, detail="An account with that username already exists.") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    from app.db.database import initialize_account_database

    initialize_account_database(account)
    return account.public()
