"""Encrypt hosted OAuth secrets while retaining local SQLite compatibility."""
from sqlalchemy import Text, TypeDecorator
from app.db.hosted import hosted_storage


class PrivateToken(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or not hosted_storage():
            return value
        from app.services.private_settings import cipher
        return cipher().encrypt(value.encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None or not hosted_storage():
            return value
        from app.services.private_settings import cipher
        return cipher().decrypt(value.encode()).decode()
