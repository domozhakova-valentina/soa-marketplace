"""Real HTTP authentication/JWT and business services; only DB storage is replaced."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import os
from threading import current_thread
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import jwt
import pytest

import app.api_impl.auth as auth_api
import app.api_impl.products as products_api
from app.errors import AppError
from app.main import app
from app.security.authorization import require_order_owner
from app.security.passwords import hash_password, verify_password
from app.security.tokens import ACCESS_SECONDS, AUDIENCE, ISSUER, REFRESH_SECONDS, TokenService, get_token_service
from app.seed_users import seed_demo_users
from app.services.auth import AuthService
from app.services.products import ProductsService
from marketplace_api.models.extra_models import TokenModel
from test_products import MemoryRepository, product_input


PASSWORD = "CorrectPassword_123"


class MemoryUsers:
    def __init__(self, store):
        self.store = store

    def create(self, connection, username, password_hash, role="USER"):
        if self.get_by_username(connection, username) is not None:
            return None
        row = {"id": uuid4(), "username": username, "password_hash": password_hash, "role": role}
        self.store.users[row["id"]] = row
        return {key: row[key] for key in ("id", "username", "role")}

    def get_by_username(self, connection, username):
        return next((row for row in self.store.users.values() if row["username"] == username), None)

    def get_by_id(self, connection, user_id):
        row = self.store.users.get(user_id)
        return {key: row[key] for key in ("id", "username", "role")} if row else None


class AuthStore(MemoryRepository):
    def get_user(self, connection, user_id):
        return MemoryUsers(self).get_by_id(connection, user_id)


class AuthEngine:
    def __init__(self, store):
        self.store = store
        self.calls = []

    @contextmanager
    def begin(self):
        self.calls.append(current_thread().name)
        saved_users, saved_products = deepcopy(self.store.users), deepcopy(self.store.products)
        try:
            yield object()
        except Exception:
            self.store.users, self.store.products = saved_users, saved_products
            raise


@pytest.fixture(scope="module")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def auth_client(monkeypatch, password_hash):
    store = AuthStore()
    engine = AuthEngine(store)
    users = MemoryUsers(store)
    for username, role in (("buyer", "USER"), ("seller", "SELLER"),
                           ("other_seller", "SELLER"), ("admin", "ADMIN")):
        users.create(None, username, password_hash, role)
    service = AuthService(engine, get_token_service(), users)
    monkeypatch.setattr(auth_api, "get_auth_service", lambda: service)
    monkeypatch.setattr(products_api, "get_products_service", lambda: ProductsService(engine, store))
    # No override for get_token_BearerAuth: every protected request verifies JWT.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, store, engine, users, service


def login(client, username="buyer"):
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token):
    return {"Authorization": "Bearer " + token}


def signed_claims(user=None, *, purpose="access", **changes):
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {"sub": str(user.id if user else uuid4()), "role": user.role.value if user else "USER",
               "token_type": purpose, "iat": now - 10, "exp": now + 60,
               "iss": ISSUER, "aud": AUDIENCE, "jti": str(uuid4()), **changes}
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")


def test_register_login_refresh_and_protected_request(auth_client):
    client, store, engine, users, _ = auth_client
    response = client.post("/auth/register", json={"username": "new_user", "password": PASSWORD})
    assert response.status_code == 201, response.text
    registered = response.json()
    assert set(registered) == {"id", "username", "role"} and registered["role"] == "USER"
    saved = users.get_by_username(None, "new_user")
    assert saved["password_hash"] != PASSWORD
    assert saved["password_hash"].startswith("$argon2id$")
    assert verify_password(PASSWORD, saved["password_hash"])
    pair = login(client, "new_user")
    assert pair["token_type"] == "bearer"
    assert pair["expires_in"] == ACCESS_SECONDS == 1800
    assert pair["refresh_expires_in"] == REFRESH_SECONDS == 1209600
    for name, purpose, lifetime in (("access_token", "access", ACCESS_SECONDS),
                                     ("refresh_token", "refresh", REFRESH_SECONDS)):
        payload = jwt.decode(pair[name], os.environ["JWT_SECRET"], algorithms=["HS256"],
                             issuer=ISSUER, audience=AUDIENCE)
        assert payload["sub"] == registered["id"] and payload["role"] == "USER"
        assert payload["token_type"] == purpose and payload["exp"] - payload["iat"] == lifetime
        assert "password" not in payload and "password_hash" not in payload
    assert client.get("/products", headers=bearer(pair["access_token"])).status_code == 200
    response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 200, response.text
    refreshed = response.json()
    assert set(refreshed) == {"access_token", "token_type", "expires_in"}
    assert refreshed["access_token"] != pair["access_token"]
    assert client.get("/products", headers=bearer(refreshed["access_token"])).status_code == 200
    assert all("worker" in name.lower() for name in engine.calls)


@pytest.mark.parametrize("extra", [{"role": "ADMIN"}, {"role": "SELLER"},
                                  {"id": str(uuid4())}, {"password_hash": "raw"}])
def test_registration_cannot_choose_privileges(auth_client, extra):
    client, _, engine, users, _ = auth_client
    response = client.post("/auth/register", json={"username": "attacker", "password": PASSWORD, **extra})
    assert response.status_code == 400
    assert users.get_by_username(None, "attacker") is None and not engine.calls


def test_duplicate_registration_returns_409(auth_client):
    client, _, _, users, _ = auth_client
    before = deepcopy(users.get_by_username(None, "buyer"))
    response = client.post("/auth/register", json={"username": "buyer", "password": "AnotherPassword_123"})
    assert response.status_code == 409
    assert response.json()["error_code"] == "USER_ALREADY_EXISTS"
    assert users.get_by_username(None, "buyer") == before


@pytest.mark.parametrize("username,password", [("buyer", "WrongPassword"), ("missing", PASSWORD)])
def test_bad_credentials_have_same_public_error(auth_client, username, password):
    client, _, _, _, _ = auth_client
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.json() == {"error_code": "INVALID_CREDENTIALS", "message": "Неверное имя пользователя или пароль"}
    assert PASSWORD not in response.text and "password_hash" not in response.text


def test_token_purposes_are_not_interchangeable(auth_client):
    client, _, _, _, _ = auth_client
    pair = login(client)
    response = client.get("/products", headers=bearer(pair["refresh_token"]))
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_INVALID"
    response = client.post("/auth/refresh", json={"refresh_token": pair["access_token"]})
    assert response.status_code == 401 and response.json()["error_code"] == "REFRESH_TOKEN_INVALID"


def test_access_rejected_after_account_deletion(auth_client):
    client, store, _, users, _ = auth_client
    pair = login(client)
    user_id = users.get_by_username(None, "buyer")["id"]
    del store.users[user_id]
    response = client.get("/products", headers=bearer(pair["access_token"]))
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_INVALID"
    response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 401 and response.json()["error_code"] == "REFRESH_TOKEN_INVALID"


def test_changed_role_invalidates_old_access_and_refresh_picks_current_role(auth_client):
    client, _, _, users, _ = auth_client
    pair = login(client, "seller")
    users.get_by_username(None, "seller")["role"] = "USER"
    response = client.get("/products", headers=bearer(pair["access_token"]))
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_INVALID"
    refreshed = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert refreshed.status_code == 200
    token = refreshed.json()["access_token"]
    assert get_token_service().decode_access(token).role.value == "USER"
    response = client.post("/products", json=product_input(), headers=bearer(token))
    assert response.status_code == 403 and response.json()["error_code"] == "ACCESS_DENIED"


def test_product_ownership_with_real_tokens(auth_client):
    client, _, _, _, _ = auth_client
    seller = login(client, "seller")["access_token"]
    other = login(client, "other_seller")["access_token"]
    admin = login(client, "admin")["access_token"]
    buyer = login(client)["access_token"]
    created = client.post("/products", json=product_input(), headers=bearer(seller))
    assert created.status_code == 201, created.text
    product = created.json()
    for method in ("put", "delete"):
        arguments = {"json": product_input()} if method == "put" else {}
        for token in (other, buyer):
            response = client.request(method, f"/products/{product['id']}", headers=bearer(token), **arguments)
            assert response.status_code == 403
    response = client.put(f"/products/{product['id']}", json=product_input(name="Admin"), headers=bearer(admin))
    assert response.status_code == 200
    assert response.json()["seller_id"] == product["seller_id"]
    assert client.delete(f"/products/{product['id']}", headers=bearer(admin)).status_code == 204
    assert client.get(f"/products/{product['id']}", headers=bearer(buyer)).json()["status"] == "ARCHIVED"


@pytest.mark.parametrize("method,path", [("post", "/orders"), ("get", "/orders/{id}"),
                                       ("put", "/orders/{id}"), ("post", "/orders/{id}/cancel")])
def test_seller_is_denied_all_order_routes(auth_client, method, path):
    client, _, _, _, _ = auth_client
    token = login(client, "seller")["access_token"]
    arguments = {"json": {"items": [{"product_id": str(uuid4()), "quantity": 1}]}} if method in ("put", "post") and not path.endswith("cancel") else {}
    response = client.request(method, path.format(id=uuid4()), headers=bearer(token), **arguments)
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "ACCESS_DENIED"


def test_user_cannot_create_promo_codes(auth_client):
    client, _, _, _, _ = auth_client
    token = login(client)["access_token"]
    response = client.post("/promo-codes", headers=bearer(token), json={
        "code": "SAVE10", "discount_type": "PERCENTAGE", "discount_value": 10,
        "min_order_amount": 0, "max_uses": 100, "active": True,
        "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
    })
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("purpose,code", [("access", "TOKEN_EXPIRED"), ("refresh", "TOKEN_INVALID")])
def test_expiry_and_wrong_purpose_are_distinct_before_db(auth_client, purpose, code):
    client, _, engine, _, _ = auth_client
    now = int(datetime.now(timezone.utc).timestamp())
    token = signed_claims(purpose=purpose, iat=now - 3600, exp=now - 1)
    response = client.get("/products", headers=bearer(token))
    assert response.status_code == 401 and response.json()["error_code"] == code
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert not engine.calls


def test_expired_refresh_is_refresh_invalid(auth_client):
    client, _, engine, _, _ = auth_client
    now = int(datetime.now(timezone.utc).timestamp())
    response = client.post("/auth/refresh", json={
        "refresh_token": signed_claims(purpose="refresh", iat=now - 3600, exp=now - 1),
    })
    assert response.status_code == 401 and response.json()["error_code"] == "REFRESH_TOKEN_INVALID"
    assert not engine.calls


@pytest.mark.parametrize("token", ["invalid", "", "a.b.c"])
def test_malformed_refresh_is_rejected(auth_client, token):
    client, _, engine, _, _ = auth_client
    response = client.post("/auth/refresh", json={"refresh_token": token})
    if not token:
        assert response.status_code == 400
    else:
        assert response.status_code == 401 and response.json()["error_code"] == "REFRESH_TOKEN_INVALID"
    assert not engine.calls


@pytest.mark.parametrize("field", ["sub", "role", "exp", "iat", "jti", "iss", "aud", "token_type"])
def test_every_required_jwt_claim_is_required(field):
    raw = signed_claims()
    payload = jwt.decode(raw, os.environ["JWT_SECRET"], algorithms=["HS256"], audience=AUDIENCE)
    del payload[field]
    token = jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")
    with pytest.raises(AppError) as caught:
        get_token_service().decode_access(token)
    assert caught.value.error_code == "TOKEN_INVALID"


@pytest.mark.parametrize("changes", [{"sub": "invalid"}, {"role": "ROOT"}, {"jti": "invalid"},
                                     {"iss": "other"}, {"aud": "other"}, {"role": None},
                                     {"exp": True}, {"iat": True}, {"exp": "9999999999"},
                                     {"iat": 9999999999}])
def test_bad_claims_are_rejected(changes):
    with pytest.raises(AppError) as caught:
        get_token_service().decode_access(signed_claims(**changes))
    assert caught.value.error_code == "TOKEN_INVALID"


@pytest.mark.parametrize("algorithm,key", [("HS256", "other-signing-key-which-is-long-enough"),
                                          ("HS384", None), ("none", "")])
def test_wrong_key_or_algorithm_is_rejected(algorithm, key):
    payload = jwt.decode(signed_claims(), os.environ["JWT_SECRET"], algorithms=["HS256"], audience=AUDIENCE)
    token = jwt.encode(payload, os.environ["JWT_SECRET"] if key is None else key, algorithm=algorithm)
    with pytest.raises(AppError) as caught:
        get_token_service().decode_access(token)
    assert caught.value.error_code == "TOKEN_INVALID"


def test_weak_signing_secret_is_rejected():
    with pytest.raises(ValueError):
        TokenService("short")


def test_argon2_hashes_use_unique_salts_and_verify():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and first.startswith("$argon2id$")
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong", first)
    assert not verify_password(PASSWORD, "not-a-password-hash")


@pytest.mark.parametrize("role,own,allowed,code", [
    ("USER", True, True, None), ("USER", False, False, "ORDER_OWNERSHIP_VIOLATION"),
    ("SELLER", True, False, "ACCESS_DENIED"), ("SELLER", False, False, "ACCESS_DENIED"),
    ("ADMIN", True, True, None), ("ADMIN", False, True, None),
])
def test_order_owner_helper_preserves_admin_exception(role, own, allowed, code):
    subject = uuid4()
    user = TokenModel(sub=str(subject), role=role)
    owner = subject if own else uuid4()
    if allowed:
        require_order_owner(user, owner)
    else:
        with pytest.raises(AppError) as caught:
            require_order_owner(user, owner)
        assert caught.value.error_code == code and caught.value.status_code == 403


def test_initial_data_is_explicit_idempotent_and_does_not_elevate_existing_accounts(auth_client):
    _, store, engine, users, _ = auth_client
    assert len(seed_demo_users(engine, PASSWORD, users)) == 3
    hashes = [users.get_by_username(None, name)["password_hash"] for name in ("demo_user", "demo_seller", "demo_admin")]
    assert len(set(hashes)) == 3
    before = deepcopy(store.users)
    seed_demo_users(engine, "AnotherPassword_123", users)
    assert store.users == before
    users.get_by_username(None, "demo_admin")["role"] = "USER"
    with pytest.raises(RuntimeError):
        seed_demo_users(engine, PASSWORD, users)
    assert users.get_by_username(None, "demo_admin")["role"] == "USER"
