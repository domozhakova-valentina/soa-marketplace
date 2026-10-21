"""Explicit CLI for demonstration accounts; never invoked by application startup."""
from getpass import getpass

from sqlalchemy.engine import Engine

from app.db.connection import get_engine
from app.repositories.users import UsersRepository
from app.security.passwords import hash_password


DEMO_USERS = (("demo_user", "USER"), ("demo_seller", "SELLER"), ("demo_admin", "ADMIN"))


def seed_demo_users(engine: Engine, password: str,
                    repository: UsersRepository | None = None) -> list[str]:
    if not 8 <= len(password) <= 128:
        raise ValueError("Password must contain 8 to 128 characters")
    repository = repository if repository is not None else UsersRepository()
    result = []
    with engine.begin() as connection:
        for username, role in DEMO_USERS:
            existing = repository.get_by_username(connection, username)
            if existing is None:
                created = repository.create(connection, username, hash_password(password), role)
                if created is not None:
                    result.append(f"Created {username} ({role})")
                    continue
                existing = repository.get_by_username(connection, username)
            if existing["role"] != role:
                raise RuntimeError(f"Existing account {username} has a different role; no accounts were changed")
            result.append(f"Already exists: {username} ({role}); password unchanged")
    return result


def main() -> None:
    password = getpass("Password for new demo accounts (8-128 characters): ")
    try:
        for message in seed_demo_users(get_engine(), password):
            print(message)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
