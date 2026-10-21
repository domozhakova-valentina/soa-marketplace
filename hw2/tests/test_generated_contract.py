"""Exercise generated routes with test implementations; no CRUD/DB/auth bypass in production."""
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import json
import re
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
import yaml

from app.contract_http import loads_contract
from app.main import app
from marketplace_api.apis.auth_api_base import BaseAuthApi
from marketplace_api.apis.products_api_base import BaseProductsApi
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.models.product_create import ProductCreate
from marketplace_api.models.product_page import ProductPage
from marketplace_api.models.product_response import ProductResponse
from marketplace_api.models.product_update import ProductUpdate
from marketplace_api.models.user_response import UserResponse
from marketplace_api.security_api import get_token_BearerAuth


PRODUCT = "11111111-1111-4111-8111-111111111111"
SELLER = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
SPEC = yaml.safe_load((Path(__file__).parents[1] / "openapi/marketplace.yaml").read_text(encoding="utf-8"))


def product_input(**updates):
    return {"name": "Mouse", "price": 1499.90, "stock": 15,
            "category": "ELECTRONICS", "status": "ACTIVE", **updates}


def product_response(payload=None):
    values = product_input(price=Decimal("1499.90")) if payload is None else payload.model_dump()
    return ProductResponse(
        id=PRODUCT, name=values["name"], description=values.get("description"),
        price=values["price"], stock=values["stock"], category=values["category"],
        status=values["status"], seller_id=values.get("seller_id") or SELLER,
        created_at=NOW, updated_at=NOW,
    )


@pytest.fixture
def api(monkeypatch):
    calls = []
    # Restore the original registries on teardown, including class registration.
    monkeypatch.setattr(BaseProductsApi, "subclasses", ())
    monkeypatch.setattr(BaseAuthApi, "subclasses", ())

    class ProductsProbe(BaseProductsApi):
        async def create_product(self, product_create):
            calls.append(("create", product_create))
            return product_response(product_create)

        async def update_product(self, id, product_update):
            calls.append(("update", id, product_update))
            return product_response(product_update)

        async def get_product(self, id):
            calls.append(("get", id))
            return product_response()

        async def list_products(self, page, size, status, category):
            calls.append(("list", page, size, status, category))
            return ProductPage(items=[], total_elements=0, page=page, size=size)

        async def archive_product(self, id):
            calls.append(("archive", id))

    class AuthProbe(BaseAuthApi):
        async def register_user(self, register_request):
            calls.append(("register", register_request))
            return UserResponse(id=SELLER, username=register_request.username, role="USER")

    monkeypatch.setitem(app.dependency_overrides, get_token_BearerAuth,
                        lambda: TokenModel(sub=SELLER, role="SELLER"))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, calls


@pytest.mark.parametrize("price", ["0.01", "1", "1499.90", "9999999999.99"])
def test_exact_money_reaches_handler_and_returns_json_number(api, price):
    client, calls = api
    body = json.dumps(product_input()).replace("1499.9", price)
    response = client.post("/products", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 201, response.text
    assert calls[0][1].price == Decimal(price)
    assert isinstance(calls[0][1].price, Decimal)
    data = json.loads(response.text, parse_float=Decimal)
    assert not isinstance(data["price"], str)
    assert Decimal(data["price"]) == Decimal(price)
    assert data["id"] == PRODUCT
    assert data["description"] is None
    assert "created_at" in data
    UUID(response.headers["X-Request-Id"])


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", "x" * 256), ("description", "x" * 4001),
    ("price", 0), ("price", -1), ("price", 0.001), ("price", 10000000000),
    ("price", "1.23"), ("price", True), ("price", None),
    ("stock", -1), ("stock", 1.5), ("stock", "1"), ("stock", True),
    ("category", ""), ("category", "x" * 101), ("status", "UNKNOWN"),
    ("seller_id", "bad-uuid"), ("seller_id", 123), ("seller_id", None),
    ("unexpected", "value"), ("id", PRODUCT),
])
def test_invalid_body_never_reaches_implementation(api, field, value):
    client, calls = api
    response = client.post("/products", json=product_input(**{field: value}))
    assert response.status_code == 400, response.text
    data = response.json()
    assert data["error_code"] == "VALIDATION_ERROR"
    assert data["details"]["fields"]
    assert not calls


@pytest.mark.parametrize("missing", ["name", "price", "stock", "category", "status"])
def test_required_properties(api, missing):
    client, calls = api
    body = product_input()
    del body[missing]
    response = client.post("/products", json=body)
    assert response.status_code == 400
    assert not calls


@pytest.mark.parametrize("method,url", [("post", "/products"), ("put", f"/products/{PRODUCT}")])
@pytest.mark.parametrize("body", [None, "null", "{}", '{"price":', "[]"])
def test_body_is_required_and_json_must_be_valid(api, method, url, body):
    client, calls = api
    response = client.request(method, url, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert not calls


@pytest.mark.parametrize("price", ["9999999999.9800001", "NaN", "Infinity", "-Infinity"])
def test_money_is_not_rounded_by_json_parser_before_validation(api, price):
    client, calls = api
    body = json.dumps(product_input()).replace("1499.9", price)
    response = client.post("/products", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert not calls


def test_uuid_fields_are_uuid_objects(api):
    client, calls = api
    assert client.post("/products", json=product_input(seller_id=SELLER)).status_code == 201
    assert calls[0][1].seller_id == UUID(SELLER)
    assert client.get(f"/products/{PRODUCT}").status_code == 200
    assert calls[1][1] == UUID(PRODUCT)


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_invalid_path_uuid_never_reaches_implementation(api, method):
    client, calls = api
    kwargs = {"json": product_input()} if method == "put" else {}
    response = client.request(method, "/products/not-a-uuid", **kwargs)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert not calls


@pytest.mark.parametrize("query,page,size", [("", 0, 20), ("?page=0&size=20", 0, 20), ("?page=2&size=5", 2, 5)])
def test_query_integer_conversion(api, query, page, size):
    client, calls = api
    response = client.get("/products" + query)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "totalElements": 0, "page": page, "size": size}
    assert calls[0][1:3] == (page, size)
    assert type(calls[0][1]) is int


@pytest.mark.parametrize("query", ["page=-1", "size=0", "page=abc", "page=1.5", "page=1.0",
                                  "size=1e2", "status=UNKNOWN", "category="])
def test_bad_query_is_validation_error(api, query):
    client, calls = api
    response = client.get("/products?" + query)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert not calls


def test_success_statuses_and_delete_has_no_body(api):
    client, calls = api
    assert client.post("/products", json=product_input()).status_code == 201
    assert client.put(f"/products/{PRODUCT}", json=product_input()).status_code == 200
    response = client.delete(f"/products/{PRODUCT}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.post("/auth/register", json={"username": "buyer_01", "password": "ExamplePass_123"}).status_code == 201


def test_response_validation_failure_remains_server_error(api, monkeypatch):
    client, calls = api

    async def broken(self, id):
        return {"price": "invalid"}

    monkeypatch.setattr(BaseProductsApi.subclasses[0], "get_product", broken)
    response = client.get(f"/products/{PRODUCT}")
    assert response.status_code == 500
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    UUID(response.headers["X-Request-Id"])


def test_nullable_description_and_exact_dto_serialization():
    dto = ProductCreate(**product_input(price=Decimal("9999999999.99"), description=None))
    assert loads_contract(dto.to_json())["price"] == Decimal("9999999999.99")
    assert loads_contract(dto.to_json())["description"] is None
    assert "seller_id" not in loads_contract(dto.to_json())
    response = product_response(dto)
    assert loads_contract(response.to_json())["id"] == PRODUCT
    assert loads_contract(response.to_json())["description"] is None


def test_updates_use_generated_dto_with_same_money_rules():
    dto = ProductUpdate(**product_input(price=Decimal("0.01")))
    assert dto.price == Decimal("0.01")
    with pytest.raises(ValidationError):
        ProductUpdate(**product_input(price=Decimal("0.001")))


def test_openapi_money_schema_remains_number():
    for mode in ("validation", "serialization"):
        price = ProductCreate.model_json_schema(mode=mode)["properties"]["price"]
        assert price["type"] == "number"
        assert price["minimum"] == 0.01


def test_all_generated_object_models_forbid_extra_properties():
    for name, schema in SPEC["components"]["schemas"].items():
        if schema.get("type") != "object":
            continue
        module_name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        model = getattr(importlib.import_module(f"marketplace_api.models.{module_name}"), name)
        if schema.get("additionalProperties") is False:
            assert model.model_config["extra"] == "forbid", name


def test_generated_statuses_match_source_contract():
    routes = {(route.path, method): route for route in app.routes if hasattr(route, "methods") for method in route.methods}
    for path, path_item in SPEC["paths"].items():
        for method, operation in path_item.items():
            if method not in ("get", "post", "put", "delete"):
                continue
            successes = [int(code) for code in operation["responses"] if str(code).startswith("2")]
            assert len(successes) == 1, "Templates require one primary successful response per operation"
            assert routes[(path, method.upper())].status_code == successes[0]


def test_documentation_has_400_validation_and_keeps_business_422():
    schema = app.openapi()
    assert "400" in schema["paths"]["/products"]["post"]["responses"]
    assert "422" not in schema["paths"]["/products"]["post"]["responses"]
    assert schema["paths"]["/orders"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")


def test_health_still_works(api):
    client, calls = api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("schema_name", ["ProductResponse", "ProductPage", "OrderCreate", "OrderUpdate", "OrderResponse", "PromoCodeResponse", "UserResponse"])
def test_schema_examples_still_validate_with_custom_templates(schema_name):
    schema = SPEC["components"]["schemas"][schema_name]
    module_name = re.sub(r"(?<!^)(?=[A-Z])", "_", schema_name).lower()
    model = getattr(importlib.import_module(f"marketplace_api.models.{module_name}"), schema_name)
    value = model.model_validate(schema["example"])
    exported = loads_contract(value.to_json())
    assert set(schema["required"]) <= set(exported)
    assert model.model_validate(exported) == value
