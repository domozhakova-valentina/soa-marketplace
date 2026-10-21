"""HTTP and business rules with an in-memory repository; no production auth bypass."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import current_thread
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

import app.api_impl.products as products_api
from app.main import app
from app.services.products import ProductsService
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.security_api import get_token_BearerAuth


def product_input(**changes):
    return {"name": "Mouse", "description": "Original", "price": 12.34,
            "stock": 4, "category": "Tech", "status": "ACTIVE", **changes}


class MemoryRepository:
    """Test double only. PostgreSQL SQL is covered by test_products_postgres.py."""
    def __init__(self):
        self.users = {}
        self.products = {}
        self.clock = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def get_user(self, connection, user_id):
        return self.users.get(user_id)

    def create(self, connection, values):
        product_id = uuid4()
        row = {**values, "id": product_id, "created_at": self.clock, "updated_at": self.clock}
        self.products[product_id] = row
        return row

    def get(self, connection, product_id, *, for_update=False):
        return self.products.get(product_id)

    def update(self, connection, product_id, values):
        self.products[product_id].update(values, updated_at=self.clock + timedelta(seconds=1))
        return self.products[product_id]

    def archive(self, connection, product_id):
        self.products[product_id].update(status="ARCHIVED", updated_at=self.clock + timedelta(seconds=1))

    def list(self, connection, page, size, status, category):
        rows = [row for row in self.products.values()
                if (status is None or row["status"] == status)
                and (category is None or row["category"] == category)]
        rows.sort(key=lambda row: (row["created_at"], row["id"]))
        return rows[page * size:(page + 1) * size], len(rows)


class MemoryEngine:
    def __init__(self, repository):
        self.repository = repository
        self.calls = []

    @contextmanager
    def begin(self):
        self.calls.append(current_thread().name)
        saved = deepcopy(self.repository.products)
        try:
            yield object()
        except Exception:
            self.repository.products = saved
            raise


@pytest.fixture
def catalog(monkeypatch):
    repository = MemoryRepository()
    users = {role: uuid4() for role in ("USER", "SELLER", "OTHER_SELLER", "ADMIN")}
    for label, user_id in users.items():
        repository.users[user_id] = {"id": user_id, "username": label.lower(),
                                     "role": "SELLER" if label == "OTHER_SELLER" else label}
    engine = MemoryEngine(repository)
    service = ProductsService(engine, repository)
    monkeypatch.setattr(products_api, "get_products_service", lambda: service)
    identity = {"sub": str(users["SELLER"])}
    monkeypatch.setitem(app.dependency_overrides, get_token_BearerAuth,
                        lambda: TokenModel(sub=identity["sub"], role=next(
                            (user["role"] for user in repository.users.values()
                             if str(user["id"]) == identity["sub"]), "SELLER")))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, repository, engine, users, identity


def create_product(client, **changes):
    response = client.post("/products", json=product_input(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def test_complete_lifecycle_and_worker_transaction(catalog):
    client, repository, engine, users, identity = catalog
    response = client.post("/products", content='{"name":"Mouse","description":"Original",'
                           '"price":9999999999.99,"stock":4,"category":"Tech","status":"ACTIVE"}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 201, response.text
    created = response.json()
    product_id = created["id"]
    assert response.headers["Location"] == f"/products/{product_id}"
    assert created["seller_id"] == str(users["SELLER"])
    assert repository.products[UUID(product_id)]["price"] == Decimal("9999999999.99")
    assert client.get(f"/products/{product_id}").json() == created

    body = product_input(name="New", price=0.01, stock=0, status="INACTIVE")
    del body["description"]
    response = client.put(f"/products/{product_id}", json=body)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["description"] is None
    assert updated["name"] == "New" and updated["stock"] == 0
    assert updated["id"] == created["id"]
    assert updated["seller_id"] == created["seller_id"]
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] > created["updated_at"]

    for _ in range(2):
        response = client.delete(f"/products/{product_id}")
        assert response.status_code == 204 and response.content == b""
    assert repository.products[UUID(product_id)]["status"] == "ARCHIVED"
    assert client.get(f"/products/{product_id}").json()["status"] == "ARCHIVED"
    assert client.get("/products").json()["totalElements"] == 1
    assert engine.calls and all("worker" in name.lower() for name in engine.calls)


@pytest.mark.parametrize("role", ["USER", "SELLER", "ADMIN"])
def test_all_roles_can_read_archived_products(catalog, role):
    client, _, _, users, identity = catalog
    product = create_product(client, status="ARCHIVED")
    identity["sub"] = str(users[role])
    assert client.get(f"/products/{product['id']}").status_code == 200
    assert client.get("/products").json()["totalElements"] == 1


@pytest.mark.parametrize("role", ["USER", "OTHER_SELLER"])
@pytest.mark.parametrize("method", ["put", "delete"])
def test_non_owner_cannot_change_product(catalog, role, method):
    client, repository, _, users, identity = catalog
    product = create_product(client)
    before = deepcopy(repository.products)
    identity["sub"] = str(users[role])
    arguments = {"json": product_input(name="Hacked")} if method == "put" else {}
    response = client.request(method, f"/products/{product['id']}", **arguments)
    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCESS_DENIED"
    assert repository.products == before


def test_admin_can_choose_seller_update_and_archive_any_product(catalog):
    client, _, _, users, identity = catalog
    identity["sub"] = str(users["ADMIN"])
    product = create_product(client, seller_id=str(users["SELLER"]))
    assert product["seller_id"] == str(users["SELLER"])
    assert client.put(f"/products/{product['id']}", json=product_input()).status_code == 200
    assert client.delete(f"/products/{product['id']}").status_code == 204


def test_seller_cannot_impersonate_another_seller(catalog):
    client, repository, _, users, _ = catalog
    response = client.post("/products", json=product_input(seller_id=str(users["OTHER_SELLER"])))
    assert response.status_code == 403
    assert not repository.products


def test_user_cannot_create_even_with_an_existing_seller_id(catalog):
    client, repository, _, users, identity = catalog
    identity["sub"] = str(users["USER"])
    response = client.post("/products", json=product_input(seller_id=str(users["SELLER"])))
    assert response.status_code == 403
    assert not repository.products


@pytest.mark.parametrize("target", ["missing", "USER"])
def test_admin_must_choose_an_existing_seller_or_admin(catalog, target):
    client, repository, _, users, identity = catalog
    identity["sub"] = str(users["ADMIN"])
    seller_id = users["USER"] if target == "USER" else uuid4()
    response = client.post("/products", json=product_input(seller_id=str(seller_id)))
    assert response.status_code == 400
    assert response.json()["details"]["fields"][0]["field"] == "seller_id"
    assert not repository.products


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_valid_uuid_missing_product_is_404(catalog, method):
    client, _, _, _, _ = catalog
    arguments = {"json": product_input()} if method == "put" else {}
    response = client.request(method, f"/products/{uuid4()}", **arguments)
    assert response.status_code == 404
    assert response.json()["error_code"] == "PRODUCT_NOT_FOUND"


def test_filters_pagination_case_and_stable_tie_order(catalog):
    client, _, _, _, _ = catalog
    ids = [create_product(client)["id"] for _ in range(3)]
    create_product(client, status="INACTIVE")
    create_product(client, category="tech")
    query = "/products?status=ACTIVE&category=Tech"
    first = client.get(query + "&page=0&size=2").json()
    second = client.get(query + "&page=1&size=2").json()
    assert first["totalElements"] == second["totalElements"] == 3
    assert [item["id"] for item in first["items"] + second["items"]] == sorted(ids)
    assert first["page"] == 0 and first["size"] == 2
    empty = client.get(query + "&page=50&size=2").json()
    assert empty == {"items": [], "totalElements": 3, "page": 50, "size": 2}
    assert client.get("/products?category=TECH").json()["totalElements"] == 0
    assert client.get("/products").json()["totalElements"] == 5


@pytest.mark.parametrize("method,url,arguments", [
    ("post", "/products", {"json": product_input(price=0)}),
    ("post", "/products", {"content": '{"price":'}),
    ("post", "/products", {}),
    ("put", "/products/not-a-uuid", {"json": product_input()}),
    ("get", "/products?size=0", {}),
    ("get", "/products?page=-1", {}),
    ("get", "/products?status=UNKNOWN", {}),
    ("put", "/products/11111111-1111-4111-8111-111111111111",
     {"json": product_input(seller_id="33333333-3333-4333-8333-333333333333")}),
])
def test_invalid_input_never_starts_a_database_transaction(catalog, method, url, arguments):
    client, repository, engine, _, _ = catalog
    response = client.request(method, url, **arguments)
    assert response.status_code == 400, response.text
    data = response.json()
    assert data["error_code"] == "VALIDATION_ERROR"
    assert all(set(item) == {"field", "message"} for item in data["details"]["fields"])
    assert not repository.products and not engine.calls


def test_nested_field_paths_and_no_input_echo(catalog):
    client, _, engine, _, _ = catalog
    response = client.post("/orders", json={"items": [{"product_id": str(uuid4()), "quantity": 0}]})
    assert response.status_code == 400
    assert any(item["field"] == "items[0].quantity"
               for item in response.json()["details"]["fields"])
    assert "input" not in response.text and not engine.calls


@pytest.mark.parametrize("subject", ["not-a-uuid", "missing"])
def test_invalid_or_deleted_token_subject_is_401(catalog, subject):
    client, _, _, _, identity = catalog
    identity["sub"] = str(uuid4()) if subject == "missing" else subject
    response = client.get("/products")
    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_INVALID"


def test_invalid_server_data_rolls_back_the_insert(catalog, monkeypatch):
    client, repository, _, _, _ = catalog
    original = repository.create

    def broken(connection, values):
        row = original(connection, values)
        return {**row, "price": "broken"}

    monkeypatch.setattr(repository, "create", broken)
    response = client.post("/products", json=product_input())
    assert response.status_code == 500
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    assert not repository.products


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer unverified"},
                                    {"Authorization": "Basic abc"}])
def test_unverified_http_identity_is_rejected(headers):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/products", headers=headers)
    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_INVALID"
