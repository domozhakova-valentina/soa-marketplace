"""Per-test JWT configuration; production never uses this signing key."""
import pytest

from app.security.tokens import get_token_service


@pytest.fixture(autouse=True)
def jwt_configuration(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-only-signing-key-never-use-outside-tests-64bytes")
    get_token_service.cache_clear()
    yield
    get_token_service.cache_clear()
