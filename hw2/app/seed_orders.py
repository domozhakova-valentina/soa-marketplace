"""Explicit demonstration data for states without public transition endpoints."""
import argparse
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.connection import get_engine
from app.repositories.orders import OrdersRepository


DEMO_STATES = ("PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED", "CANCELED")


def seed_order_states(engine: Engine, buyer: str = "demo_user", seller: str = "demo_seller") -> list[dict]:
    """Insert once; never reset existing orders or reserve their stock twice."""
    orders = OrdersRepository()
    with engine.begin() as connection:
        # Look up identities without shared locks: two simultaneous seed runs
        # must not both hold FOR SHARE and then try upgrading the buyer lock.
        lookup = text("SELECT id, role FROM users WHERE username = :username")
        owner = connection.execute(lookup, {"username": buyer}).mappings().one_or_none()
        merchant = connection.execute(lookup, {"username": seller}).mappings().one_or_none()
        if owner is None or merchant is None:
            raise ValueError("Create demo accounts first: python -m app.seed_users")
        owner = orders.lock_user(connection, owner["id"])
        if owner["role"] not in ("USER", "ADMIN") or merchant["role"] not in ("SELLER", "ADMIN"):
            raise ValueError("Buyer must be USER/ADMIN and seller must be SELLER/ADMIN")

        product_id = uuid5(NAMESPACE_URL, f"marketplace/demo-states/product/{owner['id']}/{merchant['id']}")
        order_ids = {state: uuid5(NAMESPACE_URL, f"marketplace/demo-states/order/{owner['id']}/{state}")
                     for state in DEMO_STATES}
        existing = {state: connection.execute(text("SELECT id, status FROM orders WHERE id = :id"),
                    {"id": order_id}).mappings().one_or_none() for state, order_id in order_ids.items()}
        if existing["PAYMENT_PENDING"] is None and orders.has_active_order(connection, owner["id"]):
            raise ValueError("Buyer already has an active order; cancel it before seeding demo states")

        connection.execute(text("""
            INSERT INTO products (id, name, price, stock, category, status, seller_id)
            VALUES (:id, 'Order state demonstration', 12.34, 100, 'DEMO_STATES', 'ACTIVE', :seller)
            ON CONFLICT (id) DO NOTHING
        """), {"id": product_id, "seller": merchant["id"]})
        product = orders.lock_products(connection, [product_id])[0]
        result = []
        for state, order_id in order_ids.items():
            if existing[state] is not None:
                result.append({"id": order_id, "status": existing[state]["status"],
                               "product_id": product_id, "created": False})
                continue
            # Terminal CANCELED already returned its inventory. Other states
            # represent a purchased/reserved unit and consume one unit once.
            item = {"product_id": product_id, "quantity": 1, "price_at_order": product["price"]}
            if state != "CANCELED":
                orders.reserve_products(connection, [item])
            connection.execute(text("""
                INSERT INTO orders (id, user_id, status, total_amount)
                VALUES (:id, :user_id, :status, :total)
            """), {"id": order_id, "user_id": owner["id"], "status": state, "total": product["price"]})
            orders.create_items(connection, order_id, [item])
            result.append({"id": order_id, "status": state, "product_id": product_id, "created": True})
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buyer", default="demo_user")
    parser.add_argument("--seller", default="demo_seller")
    args = parser.parse_args()
    try:
        for order in seed_order_states(get_engine(), args.buyer, args.seller):
            action = "Created" if order["created"] else "Already exists"
            print(f"{action}: {order['id']} ({order['status']})")
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
