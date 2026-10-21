"""User queries shared by authentication and trusted initial-data creation."""
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping


class UsersRepository:
    def create(self, connection: Connection, username: str, password_hash: str,
               role: str = "USER") -> RowMapping | None:
        # Only a username conflict is a known business error; DB failures propagate.
        return connection.execute(text("""
            INSERT INTO users (username, password_hash, role)
            VALUES (:username, :password_hash, :role)
            ON CONFLICT (username) DO NOTHING
            RETURNING id, username, role
        """), {"username": username, "password_hash": password_hash, "role": role}).mappings().one_or_none()

    def get_by_username(self, connection: Connection, username: str) -> RowMapping | None:
        return connection.execute(text("""
            SELECT id, username, password_hash, role FROM users
            WHERE username = :username FOR SHARE
        """), {"username": username}).mappings().one_or_none()

    def get_by_id(self, connection: Connection, user_id: UUID) -> RowMapping | None:
        return connection.execute(text("""
            SELECT id, username, role FROM users WHERE id = :id FOR SHARE
        """), {"id": user_id}).mappings().one_or_none()
