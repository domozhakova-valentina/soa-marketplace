"""Real PostgreSQL queries, migrations, timestamps and HTTP; run test-integration."""
from decimal import Decimal
import os
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text

import app.api_impl.products as products_api
from app.main import app
from app.services.products import ProductsService
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.security_api import get_token_BearerAuth


DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="Run the test-integration Compose service")


def product_input(**changes):
    return {"name": "Mouse", "description": "Original", "price": 12.34,
            "stock": 4, "category": "Tech", "status": "ACTIVE", **changes}


@pytest.fixture
def database_api(monkeypatch):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    users = {role: uuid4() for role in ("USER", "SELLER", "OTHER_SELLER", "ADMIN")}
    try:
        with engine.begin() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM flyway_schema_history WHERE success"
            )).scalar_one() >= 3
            for label, user_id in users.items():
                connection.execute(text("""
                    INSERT INTO users (id, username, password_hash, role)
                    VALUES (:id, :username, 'test-only', :role)
                """), {"id": user_id, "username": "test_" + user_id.hex,
                       "role": "SELLER" if label == "OTHER_SELLER" else label})
        identity = {"sub": str(users["SELLER"])}
        service = ProductsService(engine)
        monkeypatch.setattr(products_api, "get_products_service", lambda: service)
        monkeypatch.setitem(app.dependency_overrides, get_token_BearerAuth,
                            lambda: TokenModel(sub=identity["sub"], role=next(
                                ("SELLER" if label == "OTHER_SELLER" else label
                                 for label, user_id in users.items() if str(user_id) == identity["sub"]), "USER")))
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, engine, users, identity, service
    finally:
        # Delete only this test's unique data, leaving other database rows alone.
        with engine.begin() as connection:
            for user_id in users.values():
                connection.execute(text("DELETE FROM products WHERE seller_id = :id"), {"id": user_id})
            for user_id in users.values():
                connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        engine.dispose()


def test_real_database_lifecycle_and_soft_delete(database_api):
    client, engine, users, identity, _ = database_api
    response = client.post("/products", content='{"name":"Mouse","description":"Original",'
                           '"price":9999999999.99,"stock":4,"category":"Tech","status":"ACTIVE"}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 201, response.text
    created = response.json()
    product_id = UUID(created["id"])
    assert response.headers["Location"] == f"/products/{product_id}"
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT * FROM products WHERE id = :id"),
                                    {"id": product_id}).mappings().one()
    assert stored["price"] == Decimal("9999999999.99")
    assert stored["seller_id"] == users["SELLER"]
    assert stored["created_at"].isoformat().replace("+00:00", "Z") == created["created_at"].replace("+00:00", "Z")
    assert client.get(f"/products/{product_id}").json() == created

    replacement = product_input(name="Replacement", price=0.01, stock=0)
    del replacement["description"]
    response = client.put(f"/products/{product_id}", json=replacement)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["description"] is None and updated["price"] == 0.01
    assert updated["seller_id"] == created["seller_id"]
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] > created["updated_at"]
    for _ in range(2):
        response = client.delete(f"/products/{product_id}")
        assert response.status_code == 204 and response.content == b""
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT * FROM products WHERE id = :id"),
                                    {"id": product_id}).mappings().one()
    assert stored["status"] == "ARCHIVED"
    identity["sub"] = str(users["USER"])
    assert client.get(f"/products/{product_id}").json()["status"] == "ARCHIVED"


def test_real_query_filters_count_empty_page_and_tie_sort(database_api):
    client, engine, users, _, _ = database_api
    category = "category_" + uuid4().hex
    ids = []
    for changes in ({}, {}, {}, {"status": "INACTIVE"}, {"category": category.upper()}):
        response = client.post("/products", json=product_input(**{"category": category, **changes}))
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE products SET created_at = '2026-01-01T00:00:00Z' WHERE seller_id = :id"
        ), {"id": users["SELLER"]})
    query = f"/products?status=ACTIVE&category={category}"
    first = client.get(query + "&page=0&size=2")
    second = client.get(query + "&page=1&size=2")
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["totalElements"] == second.json()["totalElements"] == 3
    assert [item["id"] for item in first.json()["items"] + second.json()["items"]] == sorted(ids[:3])
    assert client.get(query + "&page=99&size=2").json() == {
        "items": [], "totalElements": 3, "page": 99, "size": 2,
    }
    assert client.get("/products?category=missing_" + uuid4().hex).json()["items"] == []
    # Category is bound as a parameter even when it resembles SQL.
    assert client.get("/products", params={"category": "' OR TRUE --"}).json()["totalElements"] == 0


def test_real_database_permissions_and_admin_seller_validation(database_api):
    client, engine, users, identity, _ = database_api
    created = client.post("/products", json=product_input()).json()
    identity["sub"] = str(users["OTHER_SELLER"])
    assert client.put(f"/products/{created['id']}", json=product_input(name="Hacked")).status_code == 403
    assert client.delete(f"/products/{created['id']}").status_code == 403
    identity["sub"] = str(users["USER"])
    assert client.post("/products", json=product_input()).status_code == 403
    identity["sub"] = str(users["ADMIN"])
    for seller_id in (users["USER"], uuid4()):
        response = client.post("/products", json=product_input(seller_id=str(seller_id)))
        assert response.status_code == 400, response.text
    response = client.post("/products", json=product_input(seller_id=str(users["SELLER"])))
    assert response.status_code == 201, response.text
    assert response.json()["seller_id"] == str(users["SELLER"])
    response = client.put(f"/products/{created['id']}", json=product_input(name="Admin change"))
    assert response.status_code == 200, response.text
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM products WHERE seller_id = :id"),
                                  {"id": users["SELLER"]}).scalar_one() == 2


def test_actual_insert_rolls_back_on_server_response_validation_error(database_api, monkeypatch):
    client, engine, users, _, service = database_api
    original = service.repository.create

    def broken(connection, values):
        row = original(connection, values)
        return {**row, "price": "broken"}

    monkeypatch.setattr(service.repository, "create", broken)
    response = client.post("/products", json=product_input())
    assert response.status_code == 500
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM products WHERE seller_id = :id"),
                                  {"id": users["SELLER"]}).scalar_one() == 0


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_uuid_validation_and_absent_product_in_postgres(database_api, method):
    client, _, _, _, _ = database_api
    arguments = {"json": product_input()} if method == "put" else {}
    assert client.request(method, "/products/not-a-uuid", **arguments).status_code == 400
    response = client.request(method, f"/products/{uuid4()}", **arguments)
    assert response.status_code == 404
    assert response.json()["error_code"] == "PRODUCT_NOT_FOUND"
