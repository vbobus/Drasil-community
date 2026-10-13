"""Account-scoped hosted settings, encrypted before reaching PostgreSQL."""
import json
import os

from cryptography.fernet import Fernet


def cipher() -> Fernet:
    key = os.getenv("DRASIL_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("Hosted Drasil requires DRASIL_ENCRYPTION_KEY.")
    return Fernet(key.encode("ascii"))


def load_settings(name: str) -> dict:
    from app.services.account_auth import active_account, _connect
    account = active_account()
    with _connect() as connection:
        row = connection.execute(
            "SELECT encrypted_value FROM private_settings WHERE account_id = ? AND name = ?",
            (account.id, name),
        ).fetchone()
    if row is None:
        return {}
    return json.loads(cipher().decrypt(row[0].encode()).decode())


def save_settings(name: str, value: dict) -> None:
    from app.services.account_auth import active_account, _connect
    account = active_account()
    encrypted = cipher().encrypt(json.dumps(value).encode()).decode()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO private_settings (account_id, name, encrypted_value) VALUES (?, ?, ?) "
            "ON CONFLICT (account_id, name) DO UPDATE SET encrypted_value = excluded.encrypted_value",
            (account.id, name, encrypted),
        )
