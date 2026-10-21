"""Order reads, replacement, cancellation and races against real PostgreSQL."""
from datetime import timedelta
from decimal import Decimal
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.security.tokens import get_token_service
from app.seed_orders import seed_order_states
from test_orders_postgres import assert_error, order_password_hash, race, world  # noqa: F401


pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                reason="Run the test-integration Compose service")
STATES = ("CREATED", "PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED", "CANCELED")


def headers(world, user="buyer"):
    token = get_token_service().issue_access(world.users[user]).access_token
    return {"Authorization": "Bearer " + token}


def put(world, order_id, items, *, user="buyer", **extra):
    payload = {"items": [{"product_id": str(product), "quantity": quantity}
                          for product, quantity in items], **extra}
    return world.client.put(f"/orders/{order_id}", json=payload, headers=headers(world, user))


def cancel(world, order_id, *, user="buyer"):
    return world.client.post(f"/orders/{order_id}/cancel", headers=headers(world, user))


def get(world, order_id, *, user="buyer"):
    return world.client.get(f"/orders/{order_id}", headers=headers(world, user))


def create(world, items, *, promo=None, user="buyer"):
    response = world.post(items, promo=promo, user=user)
    assert response.status_code == 201, response.text
    return response.json()


def set_state(world, order_id, status):
    # Trusted fixture data for states whose public transitions are unspecified.
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE orders SET status = :status WHERE id = :id"),
                           {"id": UUID(order_id), "status": status})


def stocks(world):
    return {row["id"]: row["stock"] for row in world.snapshot()["products"]}


def test_replace_items_returns_previous_stock_and_preserves_identity(world):
    old, retained, new = world.product(price="2.00"), world.product(price="3.00"), world.product(price="4.00")
    original = create(world, [(old, 3), (retained, 2)])
    response = put(world, original["id"], [(retained, 5), (new, 4)])
    assert response.status_code == 200, response.text
    updated = response.json()
    assert {key: updated[key] for key in ("id", "user_id", "created_at", "status")} == {
        key: original[key] for key in ("id", "user_id", "created_at", "status")}
    assert updated["updated_at"] > original["updated_at"]
    assert updated["total_amount"] == 31 and updated["discount_amount"] == 0
    assert stocks(world) == {old: 10, retained: 5, new: 6}
    saved = world.snapshot()
    assert {row["product_id"]: row["quantity"] for row in saved["items"]} == {retained: 5, new: 4}
    assert {row["operation_type"] for row in saved["operations"]} == {"CREATE_ORDER", "UPDATE_ORDER"}
    assert get(world, original["id"]).json() == {**updated, "items": sorted(updated["items"], key=lambda x: x["product_id"])}


def test_replacement_can_reuse_all_previously_reserved_stock(world):
    product = world.product(stock=3)
    order = create(world, [(product, 3)])
    assert stocks(world)[product] == 0
    response = put(world, order["id"], [(product, 3)])
    assert response.status_code == 200, response.text
    assert stocks(world)[product] == 0


def test_catalog_price_changes_only_apply_to_explicit_replacement(world):
    product = world.product(price="1.25")
    order = create(world, [(product, 2)])
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE products SET price = 7.77 WHERE id = :id"), {"id": product})
    assert get(world, order["id"]).json()["total_amount"] == 2.5
    assert get(world, order["id"]).json()["items"][0]["price_at_order"] == 1.25
    response = put(world, order["id"], [(product, 2)])
    assert response.status_code == 200, response.text
    assert response.json()["total_amount"] == 15.54
    assert response.json()["items"][0]["price_at_order"] == 7.77


@pytest.mark.parametrize("state", STATES)
def test_only_created_order_accepts_updates(world, state):
    product = world.product()
    order = create(world, [(product, 1)])
    set_state(world, order["id"], state)
    before = world.snapshot()
    response = put(world, order["id"], [(product, 2)])
    if state == "CREATED":
        assert response.status_code == 200, response.text
    else:
        assert_error(response, 409, "INVALID_STATE_TRANSITION")
        assert response.json()["details"]["current_status"] == state
        assert world.snapshot() == before


@pytest.mark.parametrize("state", STATES)
def test_cancellation_state_matrix(world, state):
    product = world.product()
    order = create(world, [(product, 2)])
    set_state(world, order["id"], state)
    before = world.snapshot()
    response = cancel(world, order["id"])
    if state in ("CREATED", "PAYMENT_PENDING"):
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELED"
        assert stocks(world)[product] == 10
        assert world.snapshot()["operations"] == before["operations"]
    else:
        assert_error(response, 409, "INVALID_STATE_TRANSITION")
        assert world.snapshot() == before


@pytest.mark.parametrize("method", ["get", "put", "cancel"])
@pytest.mark.parametrize("user", ["buyer", "other", "seller", "admin"])
def test_owner_and_role_matrix(world, method, user):
    product = world.product()
    order = create(world, [(product, 2)])
    before = world.snapshot()
    if method == "put":
        response = put(world, order["id"], [(product, 1)], user=user)
    else:
        response = globals()[method](world, order["id"], user=user)
    if user in ("buyer", "admin"):
        assert response.status_code == 200, response.text
        assert response.json()["user_id"] == str(world.users["buyer"].id)
    else:
        assert_error(response, 403, "ACCESS_DENIED" if user == "seller" else "ORDER_OWNERSHIP_VIOLATION")
        assert world.snapshot() == before
    if method == "put" and user == "admin":
        recorded = [row for row in world.snapshot()["operations"] if row["operation_type"] == "UPDATE_ORDER"]
        assert [row["user_id"] for row in recorded] == [world.users["admin"].id]


@pytest.mark.parametrize("method", ["get", "put", "cancel"])
@pytest.mark.parametrize("order_id, status, code", [
    ("invalid-uuid", 400, "VALIDATION_ERROR"), (str(uuid4()), 404, "ORDER_NOT_FOUND"),
])
def test_order_id_errors(world, method, order_id, status, code):
    before = world.snapshot()
    response = put(world, order_id, [(uuid4(), 1)]) if method == "put" else globals()[method](world, order_id)
    assert_error(response, status, code)
    assert world.snapshot() == before


def test_ownership_then_state_then_rate_precede_catalog(world):
    product = world.product()
    order = create(world, [(product, 1)])
    set_state(world, order["id"], "PAID")
    world.operation("UPDATE_ORDER")
    world.operation("UPDATE_ORDER", user="other")
    before = world.snapshot()
    assert_error(put(world, order["id"], [(uuid4(), 1)], user="other"), 403, "ORDER_OWNERSHIP_VIOLATION")
    assert_error(put(world, order["id"], [(uuid4(), 1)]), 409, "INVALID_STATE_TRANSITION")
    assert world.snapshot() == before
    set_state(world, order["id"], "CREATED")
    before = world.snapshot()
    limited = put(world, order["id"], [(uuid4(), 1)])
    assert_error(limited, 429, "ORDER_LIMIT_EXCEEDED")
    assert limited.json()["details"]["operation_type"] == "UPDATE_ORDER"
    assert 0 < int(limited.headers["Retry-After"]) <= 300
    assert world.snapshot() == before


def test_create_and_update_rates_are_independent_and_cancel_is_unlimited(world):
    product = world.product()
    order = create(world, [(product, 1)])
    assert put(world, order["id"], [(product, 2)]).status_code == 200
    before = world.snapshot()
    assert_error(put(world, order["id"], [(product, 3)]), 429, "ORDER_LIMIT_EXCEEDED")
    assert world.snapshot() == before
    assert cancel(world, order["id"]).status_code == 200
    assert stocks(world)[product] == 10


def test_update_rate_boundary_is_inclusive(world, monkeypatch):
    product = world.product()
    order = create(world, [(product, 1)])
    now = world.db_now()
    with world.engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO user_operations (user_id, operation_type, created_at)
            VALUES (:user, 'UPDATE_ORDER', :at)
        """), {"user": world.users["buyer"].id, "at": now - timedelta(minutes=5)})
    monkeypatch.setattr(world.service.repository, "now", lambda connection: now)
    response = put(world, order["id"], [(product, 2)])
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("failure", ["missing", "inactive", "shortage", "overflow"])
def test_failed_replacement_restores_entire_original_order(world, failure):
    old = world.product(stock=7)
    order = create(world, [(old, 3)])
    if failure == "missing":
        items, status, code = [(uuid4(), 1)], 404, "PRODUCT_NOT_FOUND"
    elif failure == "inactive":
        items, status, code = [(world.product(status="ARCHIVED"), 1)], 409, "PRODUCT_INACTIVE"
    elif failure == "shortage":
        items, status, code = [(old, 8), (world.product(stock=0), 1)], 409, "INSUFFICIENT_STOCK"
    else:
        items, status, code = [(world.product(price="9999999999.99"), 2)], 400, "VALIDATION_ERROR"
    before = world.snapshot()
    response = put(world, order["id"], items)
    assert_error(response, status, code)
    assert world.snapshot() == before
    if failure == "shortage":
        assert response.json()["details"]["products"] == [
            {"product_id": str(old), "requested": 8, "available": 7},
            {"product_id": str(items[1][0]), "requested": 1, "available": 0},
        ]


@pytest.mark.parametrize("extra", [{"promo_code": "SAVE_10"}, {"status": "PAID"}, {"user_id": str(uuid4())}])
def test_put_rejects_fields_outside_generated_contract(world, extra):
    product = world.product()
    order = create(world, [(product, 1)])
    before = world.snapshot()
    assert_error(put(world, order["id"], [(product, 2)], **extra), 400, "VALIDATION_ERROR")
    assert world.snapshot() == before


def test_update_keeps_one_promo_use_even_at_its_maximum(world):
    world.service.update_interval = timedelta(0)
    product = world.product(price="0.05")
    order = create(world, [(product, 1)], promo=world.promo(max_uses=1))
    for quantity, discount, total in [(3, 0.02, 0.13), (5, 0.03, 0.22), (2, 0.01, 0.09)]:
        response = put(world, order["id"], [(product, quantity)])
        assert response.status_code == 200, response.text
        assert response.json()["discount_amount"] == discount
        assert response.json()["total_amount"] == total
        assert response.json()["promo_code_id"] == order["promo_code_id"]
        assert world.snapshot()["promos"][0]["current_uses"] == 1


@pytest.mark.parametrize("discount_type,value,expected", [
    ("PERCENTAGE", "70", "7.00"), ("PERCENTAGE", "70.01", "0.00"),
    ("PERCENTAGE", "90", "0.00"), ("FIXED_AMOUNT", "15", "10.00"),
])
def test_replacement_recalculates_discount_and_limits(world, discount_type, value, expected):
    product = world.product(price="5.00")
    promo = world.promo(discount_type=discount_type, discount_value=Decimal(value))
    order = create(world, [(product, 1)], promo=promo)
    response = put(world, order["id"], [(product, 2)])
    assert response.status_code == 200, response.text
    assert Decimal(str(response.json()["discount_amount"])) == Decimal(expected)
    assert Decimal(str(response.json()["total_amount"])) == Decimal("10") - Decimal(expected)
    assert world.snapshot()["promos"][0]["current_uses"] == 1


def test_below_minimum_removes_promo_and_releases_exactly_one_use(world):
    world.service.update_interval = timedelta(0)
    product = world.product(price="10.00")
    promo = world.promo(min_order_amount=Decimal("20"), current_uses=1)
    order = create(world, [(product, 3)], promo=promo)
    assert world.snapshot()["promos"][0]["current_uses"] == 2
    response = put(world, order["id"], [(product, 1)])
    assert response.status_code == 200, response.text
    assert response.json()["promo_code_id"] is None
    assert response.json()["discount_amount"] == 0 and response.json()["total_amount"] == 10
    assert world.snapshot()["promos"][0]["current_uses"] == 1
    # Raising the total later does not reattach a removed promo.
    assert put(world, order["id"], [(product, 3)]).json()["discount_amount"] == 0
    assert cancel(world, order["id"]).status_code == 200
    assert world.snapshot()["promos"][0]["current_uses"] == 1
    assert stocks(world)[product] == 10


@pytest.mark.parametrize("change", ["expired", "inactive", "future"])
@pytest.mark.parametrize("quantity", [1, 3])
def test_invalid_promo_rolls_back_replacement_even_below_minimum(world, change, quantity):
    product = world.product(price="10")
    order = create(world, [(product, 2)], promo=world.promo(min_order_amount=Decimal("20")))
    now = world.db_now()
    with world.engine.begin() as connection:
        if change == "inactive":
            connection.execute(text("UPDATE promo_codes SET active = false WHERE id = :id"),
                               {"id": UUID(order["promo_code_id"])})
        else:
            start, end = ((now - timedelta(days=2), now - timedelta(days=1)) if change == "expired"
                          else (now + timedelta(days=1), now + timedelta(days=2)))
            connection.execute(text("UPDATE promo_codes SET valid_from = :start, valid_until = :end WHERE id = :id"),
                               {"id": UUID(order["promo_code_id"]), "start": start, "end": end})
    before = world.snapshot()
    assert_error(put(world, order["id"], [(product, quantity)]), 422, "PROMO_CODE_INVALID")
    assert world.snapshot() == before


def test_cancellation_restores_all_stock_but_preserves_historical_prices_and_discount(world):
    first, second = world.product(price="5.00"), world.product(price="7.00")
    order = create(world, [(first, 2), (second, 3)], promo=world.promo(max_uses=1))
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE products SET price = 99, status = 'ARCHIVED' WHERE id = :id"), {"id": first})
        connection.execute(text("UPDATE promo_codes SET active = false WHERE id = :id"),
                           {"id": UUID(order["promo_code_id"])})
    response = cancel(world, order["id"])
    assert response.status_code == 200, response.text
    canceled = response.json()
    assert canceled["status"] == "CANCELED"
    for key in ("id", "user_id", "created_at", "promo_code_id", "total_amount", "discount_amount"):
        assert canceled[key] == order[key]
    assert sorted(canceled["items"], key=lambda item: item["product_id"]) == sorted(order["items"], key=lambda item: item["product_id"])
    assert stocks(world) == {first: 10, second: 10}
    assert world.snapshot()["promos"][0]["current_uses"] == 0
    before = world.snapshot()
    assert_error(cancel(world, order["id"]), 409, "INVALID_STATE_TRANSITION")
    assert world.snapshot() == before
    assert get(world, order["id"]).json() == canceled


@pytest.mark.parametrize("method", ["put", "cancel"])
def test_late_database_failure_rolls_back_stock_promo_items_and_journal(world, monkeypatch, method):
    product = world.product(price="10")
    order = create(world, [(product, 2)], promo=world.promo(min_order_amount=Decimal("20")))
    before = world.snapshot()

    def fail(connection, *args):
        # A real SQL failure after inventory and promo counters changed.
        connection.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(world.service.repository, "record_operation" if method == "put" else "set_status", fail)
    response = put(world, order["id"], [(product, 1)]) if method == "put" else cancel(world, order["id"])
    assert_error(response, 500, "INTERNAL_ERROR")
    assert world.snapshot() == before


@pytest.mark.parametrize("method", ["put", "cancel"])
def test_invalid_server_response_rolls_back_transaction(world, monkeypatch, method):
    product = world.product()
    order = create(world, [(product, 1)], promo=world.promo())
    before = world.snapshot()
    name = "update_order" if method == "put" else "set_status"
    original = getattr(world.service.repository, name)

    def invalid_response(*args):
        return {**original(*args), "status": "BROKEN_SERVER_STATE"}

    monkeypatch.setattr(world.service.repository, name, invalid_response)
    response = put(world, order["id"], [(product, 2)]) if method == "put" else cancel(world, order["id"])
    assert_error(response, 500, "INTERNAL_ERROR")
    assert world.snapshot() == before


def test_competing_cancellations_restore_stock_and_promo_once(world):
    product = world.product(stock=3)
    order = create(world, [(product, 3)], promo=world.promo(max_uses=1))
    responses = race(lambda: cancel(world, order["id"]), lambda: cancel(world, order["id"], user="admin"))
    assert sorted(response.status_code for response in responses) == [200, 409], [r.text for r in responses]
    assert stocks(world)[product] == 3
    saved = world.snapshot()
    assert saved["promos"][0]["current_uses"] == 0
    assert saved["orders"][0]["status"] == "CANCELED"
    assert len(saved["operations"]) == 1


def test_competing_updates_for_same_actor_enforce_update_limit(world):
    product = world.product(stock=10)
    order = create(world, [(product, 1)], promo=world.promo(max_uses=1))
    responses = race(lambda: put(world, order["id"], [(product, 3)]),
                     lambda: put(world, order["id"], [(product, 5)]))
    assert sorted(response.status_code for response in responses) == [200, 429], [r.text for r in responses]
    saved = world.snapshot()
    assert stocks(world)[product] + saved["items"][0]["quantity"] == 10
    assert saved["promos"][0]["current_uses"] == 1
    assert [row["operation_type"] for row in saved["operations"]].count("UPDATE_ORDER") == 1


def test_owner_and_admin_updates_serialize_on_the_order(world):
    product = world.product(stock=10)
    order = create(world, [(product, 1)], promo=world.promo(max_uses=1))
    responses = race(lambda: put(world, order["id"], [(product, 3)]),
                     lambda: put(world, order["id"], [(product, 5)], user="admin"))
    assert [response.status_code for response in responses] == [200, 200], [r.text for r in responses]
    saved = world.snapshot()
    assert saved["items"][0]["quantity"] in (3, 5)
    assert stocks(world)[product] + saved["items"][0]["quantity"] == 10
    assert saved["promos"][0]["current_uses"] == 1
    assert [row["operation_type"] for row in saved["operations"]].count("UPDATE_ORDER") == 2


def test_update_racing_with_cancel_leaves_no_reservation(world):
    product = world.product(stock=10)
    order = create(world, [(product, 2)], promo=world.promo(max_uses=1))
    updated, canceled = race(lambda: put(world, order["id"], [(product, 4)]),
                             lambda: cancel(world, order["id"], user="admin"))
    assert canceled.status_code == 200, canceled.text
    assert updated.status_code in (200, 409), updated.text
    if updated.status_code == 409:
        assert_error(updated, 409, "INVALID_STATE_TRANSITION")
    saved = world.snapshot()
    assert stocks(world)[product] == 10
    assert saved["promos"][0]["current_uses"] == 0
    assert saved["orders"][0]["status"] == "CANCELED"


def test_cross_order_replacements_lock_old_and_new_products_without_deadlock(world):
    first, second = world.product(stock=5), world.product(stock=5)
    buyer = create(world, [(first, 2)])
    other = create(world, [(second, 2)], user="other")
    responses = race(lambda: put(world, buyer["id"], [(second, 3), (first, 1)]),
                     lambda: put(world, other["id"], [(first, 3), (second, 1)], user="other"))
    assert [response.status_code for response in responses] == [200, 200], [r.text for r in responses]
    saved = world.snapshot()
    for product in (first, second):
        reserved = sum(row["quantity"] for row in saved["items"] if row["product_id"] == product)
        assert stocks(world)[product] + reserved == 5


def test_demo_orders_cover_remaining_states_and_are_idempotent(world):
    args = (world.engine, world.users["buyer"].username, world.users["seller"].username)
    seeded = seed_order_states(*args)
    world.products.append(seeded[0]["product_id"])
    assert {row["status"] for row in seeded} == set(STATES) - {"CREATED"}
    assert stocks(world)[seeded[0]["product_id"]] == 96
    before = world.snapshot()
    again = seed_order_states(*args)
    assert all(not row["created"] for row in again)
    assert world.snapshot() == before
    for row in seeded:
        response = get(world, row["id"])
        assert response.status_code == 200, response.text
        assert response.json()["status"] == row["status"]
    pending = next(row for row in seeded if row["status"] == "PAYMENT_PENDING")
    assert cancel(world, pending["id"]).status_code == 200
    before = world.snapshot()
    seed_order_states(*args)
    assert world.snapshot() == before


def test_demo_seed_refuses_to_displace_existing_active_order(world):
    order = create(world, [(world.product(), 1)])
    before = world.snapshot()
    with pytest.raises(ValueError, match="active order"):
        seed_order_states(world.engine, world.users["buyer"].username, world.users["seller"].username)
    assert world.snapshot() == before
    assert get(world, order["id"]).json()["status"] == "CREATED"


def test_simultaneous_demo_seed_runs_do_not_duplicate_reservations(world):
    def seed():
        return seed_order_states(world.engine, world.users["buyer"].username, world.users["seller"].username)

    first, second = race(seed, seed)
    world.products.append(first[0]["product_id"])
    assert sum(row["created"] for rows in (first, second) for row in rows) == 5
    assert len(world.snapshot()["orders"]) == 5
    assert stocks(world)[first[0]["product_id"]] == 96
