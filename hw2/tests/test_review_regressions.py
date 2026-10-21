"""Boundary cases found while checking the implementation against the homework."""
import json
import os
from uuid import UUID

import pytest
from sqlalchemy import text

import app.api_impl.products as products_api
from app.security.tokens import get_token_service
from app.services.products import ProductsService
from marketplace_api.models.error_response import ErrorResponse
from test_auth import auth_client, password_hash, signed_claims  # noqa: F401
from test_generated_contract import api, product_input  # noqa: F401
from test_orders_postgres import order_password_hash, world  # noqa: F401
from test_request_logging import log_capture, requests  # noqa: F401


@pytest.mark.parametrize("method,path,status,code", [
    ("GET", "/missing-route", 404, "NOT_FOUND"),
    ("POST", "/health", 405, "METHOD_NOT_ALLOWED"),
])
def test_routing_errors_use_the_same_error_contract(api, method, path, status, code):
    client, _ = api
    response = client.request(method, path)
    assert response.status_code == status
    assert response.json()["error_code"] == code
    ErrorResponse.model_validate(response.json())
    UUID(response.headers["X-Request-Id"])
    if status == 405:
        assert "GET" in response.headers["Allow"]


@pytest.mark.parametrize("query", ["page=2147483648", "size=2147483648", "category=%00"])
def test_unstorable_query_values_never_reach_repository(api, query):
    client, calls = api
    response = client.get("/products?" + query)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert not calls


@pytest.mark.parametrize("category", ["Tech", "Электроника", "😀", "a/b"])
def test_valid_category_query_reaches_implementation_unchanged(api, category):
    client, calls = api
    response = client.get("/products", params={"category": category})
    assert response.status_code == 200, response.text
    assert calls == [("list", 0, 20, None, category)]


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("field,value", [
    ("stock", 2147483648), ("name", "bad\x00name"),
    ("description", "bad\x00description"), ("category", "bad\x00category"),
    ("name", "\ud800"), ("description", "\udfff"),
])
def test_unstorable_product_values_are_client_errors(api, method, field, value):
    client, calls = api
    path = "/products" if method == "POST" else "/products/11111111-1111-4111-8111-111111111111"
    response = client.request(method, path, content=json.dumps(product_input(**{field: value})),
                              headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert any(item["field"] == field for item in response.json()["details"]["fields"])
    assert not calls


def test_largest_valid_unicode_product_is_accepted_and_its_body_logged(api, log_capture):
    client, _ = api
    body = product_input(name="😀" * 255, description="😀" * 4000, category="😀" * 100)
    response = client.post("/products", json=body)
    assert response.status_code == 201, response.text
    assert requests(log_capture)[0]["body"] == body


@pytest.mark.parametrize("claim", ["iat", "exp", "nbf"])
def test_non_finite_jwt_dates_are_invalid_tokens_not_server_errors(auth_client, claim):
    client, _, engine, _, _ = auth_client
    token = signed_claims(**{claim: float("inf")})
    response = client.get("/products", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401, response.text
    assert response.json()["error_code"] == "TOKEN_INVALID"
    assert not engine.calls


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="Run test-integration")
def test_int32_boundaries_are_supported_by_real_postgresql(world, monkeypatch):
    monkeypatch.setattr(products_api, "get_products_service", lambda: ProductsService(world.engine))
    token = get_token_service().issue_access(world.users["seller"]).access_token
    headers = {"Authorization": "Bearer " + token}
    response = world.client.post("/products", json=product_input(stock=2147483647), headers=headers)
    assert response.status_code == 201, response.text
    product_id = UUID(response.json()["id"])
    world.products.append(product_id)
    with world.engine.connect() as connection:
        assert connection.execute(text("SELECT stock FROM products WHERE id = :id"),
                                  {"id": product_id}).scalar_one() == 2147483647
    response = world.client.get("/products?page=2147483647&size=2147483647", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [] and response.json()["totalElements"] >= 1
