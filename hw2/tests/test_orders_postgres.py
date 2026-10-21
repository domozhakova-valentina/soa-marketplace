"""Order creation through real HTTP, JWT and PostgreSQL, including races/rollback."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import os
from threading import Barrier
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import Uuid, bindparam, create_engine, text

import app.api_impl.auth as auth_api
import app.api_impl.orders as orders_api
from app.main import app
from app.repositories.users import UsersRepository
from app.security.passwords import hash_password
from app.security.tokens import get_token_service
from app.services.auth import AuthService
from app.services.orders import OrdersService
from marketplace_api.models.user_response import UserResponse


DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="Run the test-integration Compose service")


def expanded(query, name):
    return text(query).bindparams(bindparam(name, expanding=True, type_=Uuid()))


class OrderWorld:
    def __init__(self, engine, password_hash):
        self.engine = engine
        self.users = {}
        self.products = []
        self.promos = []
        repository = UsersRepository()
        with engine.begin() as connection:
            for label, role in (("buyer", "USER"), ("other", "USER"), ("seller", "SELLER"), ("admin", "ADMIN")):
                row = repository.create(connection, "order_" + uuid4().hex, password_hash, role)
                self.users[label] = UserResponse.model_validate(row)
        self.service = OrdersService(engine)
        self.client = None

    def product(self, *, price="12.34", stock=10, status="ACTIVE"):
        with self.engine.begin() as connection:
            row = connection.execute(text("""
                INSERT INTO products (name, price, stock, category, status, seller_id)
                VALUES ('Test order product', :price, :stock, 'ORDER_TEST', :status, :seller)
                RETURNING id
            """), {"price": Decimal(price), "stock": stock, "status": status,
                   "seller": self.users["seller"].id}).scalar_one()
        self.products.append(row)
        return row

    def promo(self, **changes):
        now = self.db_now()
        values = {"code": "P" + uuid4().hex[:15].upper(), "discount_type": "PERCENTAGE",
                  "discount_value": Decimal("10"), "min_order_amount": Decimal("0"),
                  "max_uses": 10, "current_uses": 0, "active": True,
                  "valid_from": now - timedelta(days=1), "valid_until": now + timedelta(days=1), **changes}
        with self.engine.begin() as connection:
            promo_id = connection.execute(text("""
                INSERT INTO promo_codes (code, discount_type, discount_value, min_order_amount,
                    max_uses, current_uses, active, valid_from, valid_until)
                VALUES (:code, :discount_type, :discount_value, :min_order_amount,
                    :max_uses, :current_uses, :active, :valid_from, :valid_until)
                RETURNING id
            """), values).scalar_one()
        self.promos.append(promo_id)
        return values["code"]

    def db_now(self):
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT clock_timestamp()")).scalar_one()

    def operation(self, operation_type="CREATE_ORDER", *, ago=timedelta(), user="buyer"):
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO user_operations (user_id, operation_type, created_at)
                VALUES (:user_id, :operation_type, :created_at)
            """), {"user_id": self.users[user].id, "operation_type": operation_type,
                   "created_at": self.db_now() - ago})

    def active_order(self, status="CREATED", user="buyer"):
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO orders (user_id, status, total_amount) VALUES (:user_id, :status, 0)
            """), {"user_id": self.users[user].id, "status": status})

    def post(self, items, *, promo=None, user="buyer", **extra):
        payload = {"items": [{"product_id": str(product), "quantity": quantity} for product, quantity in items], **extra}
        if promo is not None:
            payload["promo_code"] = promo
        token = get_token_service().issue_access(self.users[user]).access_token
        return self.client.post("/orders", json=payload, headers={"Authorization": "Bearer " + token})

    def snapshot(self):
        user_ids = [user.id for user in self.users.values()]
        with self.engine.connect() as connection:
            result = {}
            queries = {
                "products": ("SELECT * FROM products WHERE id IN :ids ORDER BY id", self.products),
                "promos": ("SELECT * FROM promo_codes WHERE id IN :ids ORDER BY id", self.promos),
                "orders": ("SELECT * FROM orders WHERE user_id IN :ids ORDER BY id", user_ids),
                "operations": ("SELECT * FROM user_operations WHERE user_id IN :ids ORDER BY id", user_ids),
                "items": ("SELECT * FROM order_items WHERE order_id IN "
                          "(SELECT id FROM orders WHERE user_id IN :ids) ORDER BY id", user_ids),
            }
            for label, (query, ids) in queries.items():
                result[label] = [dict(row) for row in connection.execute(expanded(query, "ids"), {"ids": ids}).mappings()]
            return result

    def cleanup(self):
        user_ids = [user.id for user in self.users.values()]
        with self.engine.begin() as connection:
            connection.execute(expanded("DELETE FROM order_items WHERE order_id IN "
                "(SELECT id FROM orders WHERE user_id IN :ids)", "ids"), {"ids": user_ids})
            connection.execute(expanded("DELETE FROM orders WHERE user_id IN :ids", "ids"), {"ids": user_ids})
            connection.execute(expanded("DELETE FROM user_operations WHERE user_id IN :ids", "ids"), {"ids": user_ids})
            connection.execute(expanded("DELETE FROM products WHERE id IN :ids", "ids"), {"ids": self.products})
            connection.execute(expanded("DELETE FROM promo_codes WHERE id IN :ids", "ids"), {"ids": self.promos})
            connection.execute(expanded("DELETE FROM users WHERE id IN :ids", "ids"), {"ids": user_ids})


@pytest.fixture(scope="module")
def order_password_hash():
    return hash_password("OrderTestPassword_123")


@pytest.fixture
def world(monkeypatch, order_password_hash):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=8,
                           connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=15000"})
    state = OrderWorld(engine, order_password_hash)
    monkeypatch.setattr(auth_api, "get_auth_service", lambda: AuthService(engine, get_token_service()))
    monkeypatch.setattr(orders_api, "get_orders_service", lambda: state.service)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            state.client = client
            yield state
    finally:
        state.cleanup()
        engine.dispose()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error_code"] == code, response.text


def test_order_creation_commits_stock_snapshots_order_and_operation(world):
    first, second = world.product(price="1.23", stock=10), world.product(price="2.50", stock=5)
    response = world.post([(first, 3), (second, 2)])
    assert response.status_code == 201, response.text
    data = response.json()
    assert response.headers["Location"] == f"/orders/{data['id']}"
    assert data["user_id"] == str(world.users["buyer"].id)
    assert data["status"] == "CREATED" and data["promo_code_id"] is None
    assert data["total_amount"] == 8.69 and data["discount_amount"] == 0
    assert [item["product_id"] for item in data["items"]] == [str(first), str(second)]
    saved = world.snapshot()
    assert {row["id"]: row["stock"] for row in saved["products"]} == {first: 7, second: 3}
    assert len(saved["orders"]) == len(saved["operations"]) == 1
    assert len(saved["items"]) == 2
    assert saved["operations"][0]["operation_type"] == "CREATE_ORDER"
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE products SET price = 99 WHERE id = :id"), {"id": first})
    assert {row["product_id"]: row["price_at_order"] for row in world.snapshot()["items"]} == {
        first: Decimal("1.23"), second: Decimal("2.50"),
    }


@pytest.mark.parametrize("user", ["buyer", "admin"])
def test_order_owner_is_the_authenticated_subject(world, user):
    product = world.product()
    response = world.post([(product, 1)], user=user)
    assert response.status_code == 201, response.text
    assert response.json()["user_id"] == str(world.users[user].id)


def test_seller_cannot_create_orders(world):
    product = world.product()
    before = world.snapshot()
    assert_error(world.post([(product, 1)], user="seller"), 403, "ACCESS_DENIED")
    assert world.snapshot() == before


def test_rate_limit_precedes_active_order_catalog_stock_and_promo(world):
    product = world.product(stock=0, status="INACTIVE")
    world.operation(ago=timedelta(seconds=1))
    world.active_order()
    before = world.snapshot()
    response = world.post([(product, 999), (uuid4(), 1)], promo="MISSING")
    assert_error(response, 429, "ORDER_LIMIT_EXCEEDED")
    assert 0 < int(response.headers["Retry-After"]) <= 300
    assert world.snapshot() == before


@pytest.mark.parametrize("status", ["CREATED", "PAYMENT_PENDING"])
def test_active_order_precedes_catalog_checks(world, status):
    world.active_order(status)
    before = world.snapshot()
    assert_error(world.post([(uuid4(), 1)]), 409, "ORDER_HAS_ACTIVE")
    assert world.snapshot() == before


def test_recent_update_does_not_block_create_and_old_create_is_outside_limit(world):
    product = world.product()
    world.operation(ago=timedelta(minutes=6))
    world.operation("UPDATE_ORDER")
    response = world.post([(product, 1)])
    assert response.status_code == 201, response.text
    operations = world.snapshot()["operations"]
    assert [row["operation_type"] for row in operations].count("CREATE_ORDER") == 2


def test_rate_limit_boundary_is_inclusive(world, monkeypatch):
    product = world.product()
    now = world.db_now()
    with world.engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO user_operations (user_id, operation_type, created_at)
            VALUES (:user_id, 'CREATE_ORDER', :created_at)
        """), {"user_id": world.users["buyer"].id, "created_at": now - timedelta(minutes=5)})
    monkeypatch.setattr(world.service.repository, "now", lambda connection: now)
    response = world.post([(product, 1)])
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("kind,code,status", [("missing", "PRODUCT_NOT_FOUND", 404),
                                             ("INACTIVE", "PRODUCT_INACTIVE", 409),
                                             ("ARCHIVED", "PRODUCT_INACTIVE", 409)])
def test_catalog_validation_precedes_stock(world, kind, code, status):
    short = world.product(stock=0)
    other = uuid4() if kind == "missing" else world.product(status=kind)
    before = world.snapshot()
    assert_error(world.post([(short, 1), (other, 1)]), status, code)
    assert world.snapshot() == before


def test_all_shortages_are_reported_without_reserving_any_products(world):
    enough, first, second = world.product(stock=20), world.product(stock=0), world.product(stock=2)
    before = world.snapshot()
    response = world.post([(enough, 1), (first, 3), (second, 4)])
    assert_error(response, 409, "INSUFFICIENT_STOCK")
    assert response.json()["details"]["products"] == [
        {"product_id": str(first), "requested": 3, "available": 0},
        {"product_id": str(second), "requested": 4, "available": 2},
    ]
    assert world.snapshot() == before


@pytest.mark.parametrize("condition", ["missing", "inactive", "expired", "future", "exhausted", "minimum"])
def test_promo_failure_after_reservation_rolls_back_everything(world, condition):
    product = world.product(price="10.00", stock=5)
    changes = {}
    now = world.db_now()
    if condition == "inactive":
        changes["active"] = False
    elif condition == "expired":
        changes.update(valid_from=now - timedelta(days=2), valid_until=now - timedelta(days=1))
    elif condition == "future":
        changes.update(valid_from=now + timedelta(days=1), valid_until=now + timedelta(days=2))
    elif condition == "exhausted":
        changes.update(max_uses=1, current_uses=1)
    elif condition == "minimum":
        changes["min_order_amount"] = Decimal("100")
    code = "MISSING" if condition == "missing" else world.promo(**changes)
    before = world.snapshot()
    response = world.post([(product, 2)], promo=code)
    assert_error(response, 422, "PROMO_CODE_MIN_AMOUNT" if condition == "minimum" else "PROMO_CODE_INVALID")
    assert world.snapshot() == before
    # Failed attempts don't create a cooldown or active order.
    response = world.post([(product, 2)])
    assert response.status_code == 201, response.text
    assert len(world.snapshot()["operations"]) == 1


@pytest.mark.parametrize("price,kind,value,discount,total", [
    ("0.05", "PERCENTAGE", "10", "0.01", "0.04"),
    ("100", "PERCENTAGE", "70", "70.00", "30.00"),
    ("100", "PERCENTAGE", "70.01", "0.00", "100.00"),
    ("100", "PERCENTAGE", "90", "0.00", "100.00"),
    ("12.34", "FIXED_AMOUNT", "100", "12.34", "0.00"),
    ("12.34", "FIXED_AMOUNT", "2.34", "2.34", "10.00"),
])
def test_discount_rounding_limits_and_exact_database_money(world, price, kind, value, discount, total):
    product = world.product(price=price)
    promo = world.promo(discount_type=kind, discount_value=Decimal(value))
    response = world.post([(product, 1)], promo=promo)
    assert response.status_code == 201, response.text
    saved = world.snapshot()
    assert saved["orders"][0]["discount_amount"] == Decimal(discount)
    assert saved["orders"][0]["total_amount"] == Decimal(total)
    assert saved["promos"][0]["current_uses"] == 1


def test_minimum_is_checked_against_subtotal_before_discount(world):
    product = world.product(price="50")
    promo = world.promo(min_order_amount=Decimal("100"), discount_value=Decimal("50"))
    response = world.post([(product, 2)], promo=promo)
    assert response.status_code == 201, response.text
    assert response.json()["total_amount"] == 50


@pytest.mark.parametrize("boundary", ["start", "end"])
def test_promo_date_boundaries_are_inclusive(world, monkeypatch, boundary):
    now = world.db_now()
    product = world.product()
    dates = {"valid_from": now} if boundary == "start" else {"valid_until": now}
    promo = world.promo(**dates)
    monkeypatch.setattr(world.service.repository, "now", lambda connection: now)
    response = world.post([(product, 1)], promo=promo)
    assert response.status_code == 201, response.text


def test_amount_overflow_rolls_back_reservation(world):
    product = world.product(price="9999999999.99", stock=10)
    before = world.snapshot()
    assert_error(world.post([(product, 2)]), 400, "VALIDATION_ERROR")
    assert world.snapshot() == before
    assert world.post([(product, 1)]).status_code == 201


@pytest.mark.parametrize("failure", ["database", "response"])
def test_late_failures_rollback_stock_promo_order_items_and_journal(world, monkeypatch, failure):
    product = world.product()
    promo = world.promo()
    before = world.snapshot()
    repository = world.service.repository
    if failure == "database":
        original = repository.record_operation

        def broken(connection, user_id, operation_type):
            original(connection, user_id, operation_type)
            connection.execute(text("SELECT 1 / 0"))

        monkeypatch.setattr(repository, "record_operation", broken)
    else:
        original = repository.create_order

        def broken(*args):
            row = original(*args)
            return {**row, "total_amount": "invalid"}

        monkeypatch.setattr(repository, "create_order", broken)
    assert_error(world.post([(product, 1)], promo=promo), 500, "INTERNAL_ERROR")
    assert world.snapshot() == before


def test_duplicate_products_are_rejected_before_service(world, monkeypatch):
    product = world.product()
    before = world.snapshot()
    called = []
    monkeypatch.setattr(world.service, "create_order", lambda *args: called.append(True))
    response = world.post([(product, 1), (product, 2)])
    assert_error(response, 400, "VALIDATION_ERROR")
    assert response.json()["details"]["fields"][0]["field"] == "items"
    assert not called and world.snapshot() == before


@pytest.mark.parametrize("case", ["empty", "too_many", "zero", "over_999", "uuid", "user_id"])
def test_invalid_request_does_not_change_database(world, case):
    product = world.product()
    before = world.snapshot()
    items, extra = [(product, 1)], {}
    if case == "empty":
        items = []
    elif case == "too_many":
        items = [(uuid4(), 1) for _ in range(51)]
    elif case == "zero":
        items = [(product, 0)]
    elif case == "over_999":
        items = [(product, 1000)]
    elif case == "uuid":
        items = [("not-a-uuid", 1)]
    elif case == "user_id":
        extra["user_id"] = str(world.users["other"].id)
    assert_error(world.post(items, **extra), 400, "VALIDATION_ERROR")
    assert world.snapshot() == before


def race(*calls):
    barrier = Barrier(len(calls))

    def invoke(call):
        barrier.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as workers:
        futures = [workers.submit(invoke, call) for call in calls]
        return [future.result(timeout=20) for future in futures]


@pytest.mark.parametrize("interval,loser_code", [(5, "ORDER_LIMIT_EXCEEDED"), (0, "ORDER_HAS_ACTIVE")])
def test_concurrent_orders_for_same_user_are_serialized(world, interval, loser_code):
    product = world.product(stock=5)
    world.service.create_interval = timedelta(minutes=interval)
    responses = race(lambda: world.post([(product, 1)]), lambda: world.post([(product, 1)]))
    assert sum(response.status_code == 201 for response in responses) == 1, [r.text for r in responses]
    loser = next(response for response in responses if response.status_code != 201)
    assert loser.json()["error_code"] == loser_code
    saved = world.snapshot()
    assert len(saved["orders"]) == len(saved["operations"]) == 1
    assert saved["products"][0]["stock"] == 4


def test_concurrent_buyers_cannot_oversell(world):
    product = world.product(stock=1)
    responses = race(lambda: world.post([(product, 1)]), lambda: world.post([(product, 1)], user="other"))
    assert sorted(response.status_code for response in responses) == [201, 409], [r.text for r in responses]
    assert next(response for response in responses if response.status_code == 409).json()["error_code"] == "INSUFFICIENT_STOCK"
    saved = world.snapshot()
    assert len(saved["orders"]) == len(saved["operations"]) == 1 and saved["products"][0]["stock"] == 0


def test_concurrent_buyers_cannot_exceed_promo_limit(world):
    first, second = world.product(stock=2), world.product(stock=2)
    promo = world.promo(max_uses=1)
    responses = race(lambda: world.post([(first, 1)], promo=promo),
                     lambda: world.post([(second, 1)], promo=promo, user="other"))
    assert sorted(response.status_code for response in responses) == [201, 422], [r.text for r in responses]
    assert next(response for response in responses if response.status_code == 422).json()["error_code"] == "PROMO_CODE_INVALID"
    saved = world.snapshot()
    assert saved["promos"][0]["current_uses"] == 1
    assert sum(row["stock"] for row in saved["products"]) == 3
    assert len(saved["orders"]) == len(saved["operations"]) == 1


def test_reversed_product_order_does_not_deadlock(world):
    first, second = world.product(stock=2), world.product(stock=2)
    responses = race(lambda: world.post([(first, 1), (second, 1)]),
                     lambda: world.post([(second, 1), (first, 1)], user="other"))
    assert [response.status_code for response in responses] == [201, 201], [r.text for r in responses]
    saved = world.snapshot()
    assert all(row["stock"] == 0 for row in saved["products"])
    assert len(saved["orders"]) == len(saved["operations"]) == 2
