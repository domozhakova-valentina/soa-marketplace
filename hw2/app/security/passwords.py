"""Password storage uses Argon2id with a fresh random salt per hash."""
from functools import lru_cache
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache
def dummy_password_hash() -> str:
    # Unknown usernames still incur password verification work.
    return hash_password(secrets.token_urlsafe(32))
