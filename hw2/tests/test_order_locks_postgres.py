"""Observe real PostgreSQL lock waits and business invariants under contention."""
from concurrent.futures import ThreadPoolExecutor
import os
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.repositories.users import UsersRepository
from marketplace_api.models.order_create import OrderCreate
from marketplace_api.models.user_response import UserResponse
from test_orders_postgres import order_password_hash, race, world  # noqa: F401


pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                reason="Run the test-integration Compose service")


def wait_for_block(engine, blocker_pid, table):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        # A new transaction refreshes pg_stat_activity's cached snapshot.
        with engine.connect() as observer:
            queries = observer.execute(text("""
                SELECT query FROM pg_stat_activity
                WHERE datname = current_database() AND wait_event_type = 'Lock'
                  AND :blocker = ANY(pg_blocking_pids(pid))
            """), {"blocker": blocker_pid}).scalars().all()
        for query in queries:
            if table in query.lower():
                assert "FOR UPDATE" in query.upper()
                return
        time.sleep(0.01)
    pytest.fail(f"No blocked FOR UPDATE query observed for {table}")


def request(product):
    return OrderCreate(items=[{"product_id": product, "quantity": 1}])


def test_actor_lock_serializes_service_calls(world):
    product = world.product(stock=2)
    buyer = world.users["buyer"].id
    with ThreadPoolExecutor(max_workers=1) as workers:
        with world.service.locked_transaction(buyer) as (connection, _):
            blocker = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            future = workers.submit(world.service.create_order, buyer, request(product))
            wait_for_block(world.engine, blocker, "users")
            assert not future.done()
            assert not world.snapshot()["orders"]
        result = future.result(timeout=10)
    assert result.user_id == buyer
    assert world.snapshot()["products"][0]["stock"] == 1


def test_product_lock_prevents_using_stale_stock(world):
    product = world.product(stock=2)
    buyer = world.users["buyer"].id
    with ThreadPoolExecutor(max_workers=1) as workers:
        with world.engine.begin() as connection:
            world.service.repository.lock_products(connection, [product])
            blocker = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            future = workers.submit(world.service.create_order, buyer, request(product))
            wait_for_block(world.engine, blocker, "products")
            # Simulate another operation consuming the final inventory.
            connection.execute(text("UPDATE products SET stock = 0 WHERE id = :id"), {"id": product})
        with pytest.raises(AppError) as caught:
            future.result(timeout=10)
    assert caught.value.error_code == "INSUFFICIENT_STOCK"
    assert caught.value.details["products"][0]["available"] == 0
    saved = world.snapshot()
    assert not saved["orders"] and not saved["operations"]
    assert saved["products"][0]["stock"] == 0


def test_promo_lock_rechecks_counter_after_wait_and_rolls_back_stock(world):
    product = world.product(stock=2)
    code = world.promo(max_uses=1)
    payload = OrderCreate(items=[{"product_id": product, "quantity": 1}], promo_code=code)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with world.engine.begin() as connection:
            promo = world.service.repository.lock_promo(connection, code)
            blocker = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            future = workers.submit(world.service.create_order, world.users["buyer"].id, payload)
            wait_for_block(world.engine, blocker, "promo_codes")
            world.service.repository.use_promo(connection, promo["id"])
        with pytest.raises(AppError) as caught:
            future.result(timeout=10)
    assert caught.value.error_code == "PROMO_CODE_INVALID"
    saved = world.snapshot()
    assert saved["products"][0]["stock"] == 2
    assert saved["promos"][0]["current_uses"] == 1
    assert not saved["orders"] and not saved["operations"]


def test_existing_order_lock_serializes_different_actors_and_reads_current_state(world):
    product = world.product()
    created = world.post([(product, 1)])
    assert created.status_code == 201, created.text
    order_id = UUID(created.json()["id"])

    def observe_as_admin():
        with world.service.locked_transaction(world.users["admin"].id, order_id) as (_, row):
            return row["status"]

    with ThreadPoolExecutor(max_workers=1) as workers:
        with world.service.locked_transaction(world.users["buyer"].id, order_id) as (connection, row):
            assert row["status"] == "CREATED"
            blocker = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            future = workers.submit(observe_as_admin)
            wait_for_block(world.engine, blocker, "orders")
            connection.execute(text("UPDATE orders SET status = 'PAYMENT_PENDING' WHERE id = :id"),
                               {"id": order_id})
        assert future.result(timeout=10) == "PAYMENT_PENDING"


@pytest.mark.parametrize("actor,code,status", [("other", "ORDER_OWNERSHIP_VIOLATION", 403),
                                              ("seller", "ACCESS_DENIED", 403)])
def test_mutation_transaction_checks_role_and_owner(world, actor, code, status):
    product = world.product()
    created = world.post([(product, 1)])
    assert created.status_code == 201, created.text
    before = world.snapshot()
    with pytest.raises(AppError) as caught:
        with world.service.locked_transaction(world.users[actor].id, UUID(created.json()["id"])):
            pytest.fail("A forbidden caller entered the mutation transaction")
    assert caught.value.error_code == code and caught.value.status_code == status
    assert world.snapshot() == before


def test_mutation_transaction_reports_missing_order(world):
    with pytest.raises(AppError) as caught:
        with world.service.locked_transaction(world.users["buyer"].id, uuid4()):
            pytest.fail("A missing order entered the mutation transaction")
    assert caught.value.error_code == "ORDER_NOT_FOUND" and caught.value.status_code == 404


def test_mutation_failure_releases_locks_and_rolls_back_all_changes(world):
    product = world.product(stock=5)
    code = world.promo(max_uses=5)
    created = world.post([(product, 1)], promo=code)
    assert created.status_code == 201, created.text
    order_id = UUID(created.json()["id"])
    before = world.snapshot()
    repository = world.service.repository
    with pytest.raises(RuntimeError, match="Rollback test"):
        with world.service.locked_transaction(world.users["buyer"].id, order_id) as (connection, order):
            items = repository.get_items(connection, order_id)
            repository.lock_products(connection, [item["product_id"] for item in items])
            promo = repository.lock_promo_by_id(connection, order["promo_code_id"])
            repository.reserve_products(connection, items)
            repository.use_promo(connection, promo["id"])
            raise RuntimeError("Rollback test")
    assert world.snapshot() == before
    # Another actor can acquire the order lock after the failed transaction.
    with world.service.locked_transaction(world.users["admin"].id, order_id) as (_, order):
        assert order["id"] == order_id


def test_stock_predicate_prevents_overdraw_even_if_a_caller_skips_prechecks(world):
    first, second = sorted([world.product(stock=2), world.product(stock=2)])
    before = world.snapshot()
    with pytest.raises(RuntimeError):
        with world.engine.begin() as connection:
            world.service.repository.reserve_products(connection, [
                {"product_id": first, "quantity": 1}, {"product_id": second, "quantity": 3},
            ])
    assert world.snapshot() == before


def test_promo_predicate_prevents_overuse_even_if_a_caller_skips_prechecks(world):
    world.promo(max_uses=1, current_uses=1)
    before = world.snapshot()
    with pytest.raises(RuntimeError):
        with world.engine.begin() as connection:
            world.service.repository.use_promo(connection, world.promos[0])
    assert world.snapshot() == before


@pytest.mark.parametrize("resource,successes,error_code", [("stock", 3, "INSUFFICIENT_STOCK"),
                                                         ("promo", 2, "PROMO_CODE_INVALID")])
def test_eight_parallel_buyers_preserve_stock_and_promo_limits(world, order_password_hash, resource, successes, error_code):
    users = UsersRepository()
    labels = ["buyer", "other"] + ["extra_" + str(index) for index in range(6)]
    with world.engine.begin() as connection:
        for label in labels[2:]:
            row = users.create(connection, "parallel_" + uuid4().hex, order_password_hash, "USER")
            world.users[label] = UserResponse.model_validate(row)
    if resource == "stock":
        products = [world.product(stock=successes)] * len(labels)
        promo = None
    else:
        products = [world.product(stock=1) for _ in labels]
        promo = world.promo(max_uses=successes)
    responses = race(*(lambda label=label, product=product: world.post([(product, 1)], user=label, promo=promo)
                       for label, product in zip(labels, products)))
    assert sum(response.status_code == 201 for response in responses) == successes, [r.text for r in responses]
    for response in responses:
        if response.status_code != 201:
            assert response.json()["error_code"] == error_code, response.text
    saved = world.snapshot()
    assert len(saved["orders"]) == len(saved["operations"]) == successes
    assert all(row["stock"] >= 0 for row in saved["products"])
    if resource == "stock":
        assert saved["products"][0]["stock"] == 0
    else:
        assert saved["promos"][0]["current_uses"] == successes
        assert sum(row["stock"] for row in saved["products"]) == len(labels) - successes


def test_unique_index_rejects_second_active_order_even_outside_service(world):
    product = world.product()
    assert world.post([(product, 1)]).status_code == 201
    before = world.snapshot()
    with pytest.raises(IntegrityError) as caught:
        with world.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO orders (user_id, status, total_amount)
                VALUES (:id, 'PAYMENT_PENDING', 0)
            """), {"id": world.users["buyer"].id})
    assert caught.value.orig.diag.constraint_name == "uq_orders_one_active_per_user"
    assert world.snapshot() == before
