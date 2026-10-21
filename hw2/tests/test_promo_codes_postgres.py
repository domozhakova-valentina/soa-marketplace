"""Promo creation through the generated HTTP route, real JWT and PostgreSQL."""
from datetime import datetime
from decimal import Decimal
import json
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import bindparam, text

import app.api_impl.promo_codes as promo_api
from app.errors import AppError
from app.security.tokens import get_token_service
from app.services.promo_codes import PromoCodesService
from marketplace_api.models.promo_code_create import PromoCodeCreate
from marketplace_api.models.promo_code_response import PromoCodeResponse
from test_orders_postgres import assert_error, order_password_hash, race, world  # noqa: F401


pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                reason="Run the test-integration Compose service")


def payload(**changes):
    return {"code": "P" + uuid4().hex[:15].upper(), "discount_type": "PERCENTAGE",
            "discount_value": 10, "min_order_amount": 0, "max_uses": 10,
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z", **changes}


@pytest.fixture
def promos(world, monkeypatch):
    world.promo_service = PromoCodesService(world.engine)
    world.promo_codes = set()
    monkeypatch.setattr(promo_api, "get_promo_codes_service", lambda: world.promo_service)
    try:
        yield world
    finally:
        if world.promo_codes:
            with world.engine.connect() as connection:
                ids = connection.execute(text("SELECT id FROM promo_codes WHERE code IN :codes")
                    .bindparams(bindparam("codes", expanding=True)), {"codes": sorted(world.promo_codes)}).scalars().all()
            world.promos.extend(promo_id for promo_id in ids if promo_id not in world.promos)


def post(world, body, *, user="seller", token=None):
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        world.promo_codes.add(body["code"])
    if token is None:
        token = get_token_service().issue_access(world.users[user]).access_token
    response = world.client.post("/promo-codes", json=body, headers={"Authorization": "Bearer " + token})
    if response.status_code == 201:
        world.promos.append(UUID(response.json()["id"]))
    return response


def stored(world, code):
    with world.engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM promo_codes WHERE code = :code"),
                                 {"code": code}).mappings().one_or_none()
        return dict(row) if row is not None else None


@pytest.mark.parametrize("user", ["seller", "admin"])
@pytest.mark.parametrize("kind", ["PERCENTAGE", "FIXED_AMOUNT"])
def test_roles_create_both_promo_types_and_database_assigns_id_and_counter(promos, user, kind):
    body = payload(discount_type=kind, discount_value=12.34, min_order_amount=100.01)
    response = post(promos, body, user=user)
    assert response.status_code == 201, response.text
    data = response.json()
    UUID(data["id"])
    UUID(response.headers["X-Request-Id"])
    assert data["code"] == body["code"] and data["discount_type"] == kind
    assert data["active"] is True and data["current_uses"] == 0
    assert set(data) == {"id", "code", "discount_type", "discount_value", "min_order_amount",
                         "max_uses", "current_uses", "valid_from", "valid_until", "active"}
    PromoCodeResponse.model_validate(data)
    row = stored(promos, body["code"])
    assert row["id"] == UUID(data["id"])
    assert row["discount_value"] == Decimal("12.34")
    assert row["min_order_amount"] == Decimal("100.01")
    assert row["created_at"] is not None and row["updated_at"] is not None


def test_user_is_denied_and_no_row_is_inserted(promos):
    body = payload()
    assert_error(post(promos, body, user="buyer"), 403, "ACCESS_DENIED")
    assert stored(promos, body["code"]) is None


@pytest.mark.parametrize("token", ["", "not-a-token"])
def test_missing_or_invalid_access_is_401(promos, token):
    body = payload()
    response = promos.client.post("/promo-codes", json=body,
                                  headers={"Authorization": "Bearer " + token} if token else {})
    assert_error(response, 401, "TOKEN_INVALID")
    assert stored(promos, body["code"]) is None


def test_refresh_token_is_not_accepted_for_creation(promos):
    body = payload()
    token = get_token_service().issue_pair(promos.users["seller"]).refresh_token
    assert_error(post(promos, body, token=token), 401, "TOKEN_INVALID")
    assert stored(promos, body["code"]) is None


@pytest.mark.parametrize("field,value", [
    ("code", "ABC"), ("code", "A" * 21), ("code", "lowercase"), ("code", "SAVE-10"),
    ("code", "SAVE 10"), ("code", "СКИДКА"), ("code", "SAVE_10\n"),
    ("discount_type", "UNKNOWN"), ("discount_value", 0), ("discount_value", -1),
    ("discount_value", 0.001), ("discount_value", "10"), ("discount_value", True),
    ("discount_value", 10000000000), ("min_order_amount", -0.01), ("min_order_amount", "0"),
    ("max_uses", 0), ("max_uses", 1.5), ("max_uses", "10"), ("max_uses", True),
    ("max_uses", 2147483648), ("active", None), ("active", "true"),
    ("valid_from", "bad-date"), ("valid_until", None),
    ("current_uses", 99), ("id", str(uuid4())), ("seller_id", str(uuid4())),
])
def test_invalid_input_is_400_before_any_insert(promos, monkeypatch, field, value):
    body = payload(**{field: value})
    called = []
    monkeypatch.setattr(promos.promo_service.repository, "create", lambda *args: called.append(True))
    response = post(promos, body)
    assert_error(response, 400, "VALIDATION_ERROR")
    assert any(item["field"] == field for item in response.json()["details"]["fields"])
    assert not called
    assert stored(promos, body["code"]) is None


@pytest.mark.parametrize("missing", ["code", "discount_type", "discount_value", "min_order_amount",
                                     "max_uses", "valid_from", "valid_until"])
def test_required_fields_are_enforced(promos, missing):
    body = payload()
    del body[missing]
    assert_error(post(promos, body), 400, "VALIDATION_ERROR")


@pytest.mark.parametrize("dates,field", [
    ({"valid_until": "2026-01-01T00:00:00Z"}, "valid_until"),
    ({"valid_until": "2025-01-01T00:00:00Z"}, "valid_until"),
    ({"valid_from": "2026-01-01T00:00:00"}, "valid_from"),
    ({"valid_until": "2027-01-01T00:00:00"}, "valid_until"),
    ({"valid_from": "2026-01-01T00:00:00", "valid_until": "2027-01-01T00:00:00"}, "valid_from"),
    ({"valid_from": "2026-01-01T10:00:00+03:00", "valid_until": "2026-01-01T07:00:00Z"}, "valid_until"),
])
def test_date_order_and_timezone_are_validated_before_insert(promos, monkeypatch, dates, field):
    body = payload(**dates)
    called = []
    monkeypatch.setattr(promos.promo_service.repository, "create", lambda *args: called.append(True))
    response = post(promos, body)
    assert_error(response, 400, "VALIDATION_ERROR")
    assert field in {item["field"] for item in response.json()["details"]["fields"]}
    assert not called and stored(promos, body["code"]) is None


def test_offsets_are_compared_as_instants_and_stored_in_utc(promos):
    body = payload(valid_from="2026-01-01T10:00:00+03:00", valid_until="2026-01-01T07:00:01Z")
    response = post(promos, body)
    assert response.status_code == 201, response.text
    row = stored(promos, body["code"])
    assert row["valid_from"] == datetime.fromisoformat("2026-01-01T07:00:00+00:00")
    assert row["valid_until"] > row["valid_from"]


@pytest.mark.parametrize("changes", [
    {"code": "AB_1", "discount_value": 0.01, "max_uses": 1},
    {"code": "Z" * 20, "discount_value": 9999999999.99,
     "min_order_amount": 9999999999.99, "max_uses": 2147483647},
    {"active": False},
    {"valid_from": "2020-01-01T00:00:00Z", "valid_until": "2021-01-01T00:00:00Z"},
    {"valid_from": "2030-01-01T00:00:00Z", "valid_until": "2031-01-01T00:00:00Z"},
])
def test_valid_boundaries_and_inactive_or_scheduled_codes_can_be_saved(promos, changes):
    body = payload(**changes)
    response = post(promos, body)
    assert response.status_code == 201, response.text
    data = json.loads(response.text, parse_float=Decimal)
    assert Decimal(data["discount_value"]) == Decimal(str(body["discount_value"]))
    assert data["active"] == body.get("active", True)


def test_duplicate_does_not_reset_an_existing_used_promo(promos):
    body = payload(max_uses=1)
    created = post(promos, body)
    assert created.status_code == 201
    product = promos.product(price="10")
    assert promos.post([(product, 1)], promo=body["code"]).status_code == 201
    before = stored(promos, body["code"])
    assert before["current_uses"] == 1
    duplicate = post(promos, {**body, "discount_value": 99, "max_uses": 100}, user="admin")
    assert_error(duplicate, 409, "PROMO_CODE_ALREADY_EXISTS")
    assert stored(promos, body["code"]) == before


def test_simultaneous_seller_and_admin_requests_create_one_row(promos):
    body = payload()
    responses = race(lambda: post(promos, body), lambda: post(promos, body, user="admin"))
    assert sorted(response.status_code for response in responses) == [201, 409], [r.text for r in responses]
    loser = next(response for response in responses if response.status_code == 409)
    assert_error(loser, 409, "PROMO_CODE_ALREADY_EXISTS")
    assert stored(promos, body["code"])["current_uses"] == 0


@pytest.mark.parametrize("failure", ["database", "response"])
def test_late_failure_rolls_back_promo_creation(promos, monkeypatch, failure):
    body = payload()
    original = promos.promo_service.repository.create

    def broken(connection, values):
        row = original(connection, values)
        if failure == "database":
            connection.execute(text("SELECT 1 / 0"))
        return {**row, "current_uses": -1}

    monkeypatch.setattr(promos.promo_service.repository, "create", broken)
    assert_error(post(promos, body), 500, "INTERNAL_ERROR")
    assert stored(promos, body["code"]) is None


@pytest.mark.parametrize("user,code", [("buyer", "ACCESS_DENIED"), (None, "TOKEN_INVALID")])
def test_service_rechecks_user_and_role_for_internal_callers(promos, user, code):
    body = payload()
    subject = promos.users[user].id if user else uuid4()
    with pytest.raises(AppError) as caught:
        promos.promo_service.create_promo_code(subject, PromoCodeCreate(**body))
    assert caught.value.error_code == code
    assert stored(promos, body["code"]) is None
