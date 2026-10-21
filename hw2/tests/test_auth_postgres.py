"""Real SQL, Argon2, JWT and HTTP, with no authentication dependency overrides."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import bindparam, create_engine, text

import app.api_impl.auth as auth_api
import app.api_impl.products as products_api
from app.main import app
from app.repositories.users import UsersRepository
from app.security.passwords import hash_password, verify_password
from app.security.tokens import get_token_service
from app.services.auth import AuthService
from app.services.products import ProductsService


DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
PASSWORD = "IntegrationPassword_123"
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="Run the test-integration Compose service")


@pytest.fixture
def database_auth(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    names = {role: "auth_" + uuid4().hex for role in ("USER", "SELLER", "ADMIN")}
    usernames = list(names.values())
    users = UsersRepository()
    try:
        with engine.begin() as connection:
            for role in ("SELLER", "ADMIN"):
                users.create(connection, names[role], hash_password(PASSWORD), role)
        service = AuthService(engine, get_token_service(), users)
        monkeypatch.setattr(auth_api, "get_auth_service", lambda: service)
        monkeypatch.setattr(products_api, "get_products_service", lambda: ProductsService(engine))
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, engine, names
    finally:
        with engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM products WHERE seller_id IN
                    (SELECT id FROM users WHERE username IN :names)
            """).bindparams(bindparam("names", expanding=True)), {"names": usernames})
            connection.execute(text("DELETE FROM users WHERE username IN :names")
                               .bindparams(bindparam("names", expanding=True)), {"names": usernames})
        engine.dispose()


def login(client, username):
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token):
    return {"Authorization": "Bearer " + token}


def product_input(**changes):
    return {"name": "JWT product", "price": 12.34, "stock": 5,
            "category": "Tech", "status": "ACTIVE", **changes}


def test_postgres_registration_login_refresh_and_hash(database_auth):
    client, engine, names = database_auth
    body = {"username": names["USER"], "password": PASSWORD}
    response = client.post("/auth/register", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "USER" and "password_hash" not in response.text
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT * FROM users WHERE username = :name"),
                                    {"name": names["USER"]}).mappings().one()
    assert stored["password_hash"].startswith("$argon2id$")
    assert verify_password(PASSWORD, stored["password_hash"])
    assert client.post("/auth/register", json=body).status_code == 409
    assert client.post("/auth/register", json={**body, "role": "ADMIN"}).status_code == 400
    response = client.post("/auth/login", json={**body, "password": "wrong"})
    assert response.status_code == 401 and response.json()["error_code"] == "INVALID_CREDENTIALS"
    pair = login(client, names["USER"])
    assert client.get("/products", headers=bearer(pair["access_token"])).status_code == 200
    assert client.get("/products", headers=bearer(pair["refresh_token"])).status_code == 401
    response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 200, response.text
    assert response.json()["access_token"] != pair["access_token"]
    assert client.get("/products", headers=bearer(response.json()["access_token"])).status_code == 200


def test_postgres_role_matrix_and_role_change(database_auth):
    client, engine, names = database_auth
    assert client.post("/auth/register", json={"username": names["USER"], "password": PASSWORD}).status_code == 201
    user_pair = login(client, names["USER"])
    seller_pair = login(client, names["SELLER"])
    admin_pair = login(client, names["ADMIN"])
    response = client.post("/products", json=product_input(), headers=bearer(user_pair["access_token"]))
    assert response.status_code == 403 and response.json()["error_code"] == "ACCESS_DENIED"
    response = client.post("/products", json=product_input(), headers=bearer(seller_pair["access_token"]))
    assert response.status_code == 201, response.text
    created = response.json()
    response = client.post("/products", json=product_input(seller_id=created["seller_id"]),
                           headers=bearer(admin_pair["access_token"]))
    assert response.status_code == 201, response.text
    assert response.json()["seller_id"] == created["seller_id"]
    response = client.get(f"/orders/{uuid4()}", headers=bearer(seller_pair["access_token"]))
    assert response.status_code == 403 and response.json()["error_code"] == "ACCESS_DENIED"
    with engine.begin() as connection:
        connection.execute(text("UPDATE users SET role = 'USER' WHERE username = :name"),
                           {"name": names["SELLER"]})
    response = client.get("/products", headers=bearer(seller_pair["access_token"]))
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_INVALID"
    refreshed = client.post("/auth/refresh", json={"refresh_token": seller_pair["refresh_token"]})
    assert refreshed.status_code == 200
    response = client.post("/products", json=product_input(), headers=bearer(refreshed.json()["access_token"]))
    assert response.status_code == 403


def test_concurrent_registration_maps_only_username_conflicts_to_409(database_auth):
    client, engine, names = database_auth
    body = {"username": names["USER"], "password": PASSWORD}
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: client.post("/auth/register", json=body), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM users WHERE username = :name"),
                                  {"name": names["USER"]}).scalar_one() == 1
