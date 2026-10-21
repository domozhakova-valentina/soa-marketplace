"""Order persistence and row locks; the service owns the transaction."""
from uuid import UUID

from sqlalchemy import Uuid, bindparam, text
from sqlalchemy.engine import Connection


ORDER_COLUMNS = "id, user_id, status, promo_code_id, total_amount, discount_amount, created_at, updated_at"
PROMO_COLUMNS = "id, discount_type, discount_value, min_order_amount, max_uses, current_uses, active, valid_from, valid_until"


class OrdersRepository:
    """Acquire locks only in this order: actor -> order -> sorted products -> promo.

    Creation skips the order lock because its order does not exist yet. Update
    and cancellation must lock the union of old/new products before the promo.
    Every method must use the connection owned by the service transaction.
    """

    def lock_user(self, connection: Connection, user_id: UUID):
        # Also serializes concurrent order operations for the same user.
        return connection.execute(text(
            "SELECT id, username, role FROM users WHERE id = :id FOR UPDATE"
        ), {"id": user_id}).mappings().one_or_none()

    def lock_order(self, connection: Connection, order_id: UUID):
        return connection.execute(text(
            f"SELECT {ORDER_COLUMNS} FROM orders WHERE id = :id FOR UPDATE"
        ), {"id": order_id}).mappings().one_or_none()

    def get_items(self, connection: Connection, order_id: UUID):
        # Call after locking the parent order when preparing a mutation.
        return connection.execute(text("""
            SELECT id, product_id, quantity, price_at_order FROM order_items
            WHERE order_id = :id ORDER BY product_id
        """), {"id": order_id}).mappings().all()

    def now(self, connection: Connection):
        # CURRENT_TIMESTAMP is the transaction start, possibly before a lock wait.
        return connection.execute(text("SELECT clock_timestamp()")).scalar_one()

    def last_operation(self, connection: Connection, user_id: UUID, operation_type: str):
        return connection.execute(text("""
            SELECT created_at FROM user_operations
            WHERE user_id = :user_id AND operation_type = :operation_type
            ORDER BY created_at DESC, id DESC LIMIT 1
        """), {"user_id": user_id, "operation_type": operation_type}).scalar_one_or_none()

    def has_active_order(self, connection: Connection, user_id: UUID) -> bool:
        return connection.execute(text("""
            SELECT EXISTS(SELECT 1 FROM orders
                WHERE user_id = :user_id AND status IN ('CREATED', 'PAYMENT_PENDING'))
        """), {"user_id": user_id}).scalar_one()

    def lock_products(self, connection: Connection, product_ids: list[UUID]):
        statement = text("""
            SELECT id, status, stock, price FROM products WHERE id IN :ids
            ORDER BY id FOR UPDATE
        """).bindparams(bindparam("ids", expanding=True, type_=Uuid()))
        return connection.execute(statement, {"ids": sorted(set(product_ids))}).mappings().all()

    def reserve_products(self, connection: Connection, items: list[dict]) -> None:
        result = connection.execute(text("""
            UPDATE products SET stock = stock - :quantity
            WHERE id = :product_id AND stock >= :quantity
        """), [{"product_id": item["product_id"], "quantity": item["quantity"]}
               for item in sorted(items, key=lambda item: item["product_id"])])
        if result.rowcount != len(items):
            # Under the service's locks/checks this is a programming error.
            # Force rollback even if a future caller forgets to take the locks.
            raise RuntimeError("Stock reservation invariant violated")

    def restore_products(self, connection: Connection, items: list) -> None:
        result = connection.execute(text("""
            UPDATE products SET stock = stock + :quantity WHERE id = :product_id
        """), [{"product_id": item["product_id"], "quantity": item["quantity"]}
               for item in sorted(items, key=lambda item: item["product_id"])])
        if result.rowcount != len(items):
            raise RuntimeError("Stock restoration invariant violated")

    def lock_promo(self, connection: Connection, code: str):
        return connection.execute(text(
            f"SELECT {PROMO_COLUMNS} FROM promo_codes WHERE code = :code FOR UPDATE"
        ), {"code": code}).mappings().one_or_none()

    def lock_promo_by_id(self, connection: Connection, promo_id: UUID):
        return connection.execute(text(
            f"SELECT {PROMO_COLUMNS} FROM promo_codes WHERE id = :id FOR UPDATE"
        ), {"id": promo_id}).mappings().one_or_none()

    def use_promo(self, connection: Connection, promo_id: UUID) -> None:
        result = connection.execute(text("""
            UPDATE promo_codes SET current_uses = current_uses + 1
            WHERE id = :id AND current_uses < max_uses AND active
        """), {"id": promo_id})
        if result.rowcount != 1:
            raise RuntimeError("Promo usage invariant violated")

    def release_promo(self, connection: Connection, promo_id: UUID) -> None:
        result = connection.execute(text("""
            UPDATE promo_codes SET current_uses = current_uses - 1
            WHERE id = :id AND current_uses > 0
        """), {"id": promo_id})
        if result.rowcount != 1:
            raise RuntimeError("Promo release invariant violated")

    def create_order(self, connection: Connection, user_id: UUID, promo_id: UUID | None,
                     total_amount, discount_amount):
        return connection.execute(text(f"""
            INSERT INTO orders (user_id, status, promo_code_id, total_amount, discount_amount)
            VALUES (:user_id, 'CREATED', :promo_id, :total_amount, :discount_amount)
            RETURNING {ORDER_COLUMNS}
        """), {"user_id": user_id, "promo_id": promo_id,
               "total_amount": total_amount, "discount_amount": discount_amount}).mappings().one()

    def create_items(self, connection: Connection, order_id: UUID, items: list[dict]):
        result = []
        for item in items:
            result.append(connection.execute(text("""
                INSERT INTO order_items (order_id, product_id, quantity, price_at_order)
                VALUES (:order_id, :product_id, :quantity, :price_at_order)
                RETURNING id, product_id, quantity, price_at_order
            """), {**item, "order_id": order_id}).mappings().one())
        return result

    def replace_items(self, connection: Connection, order_id: UUID, items: list[dict]):
        connection.execute(text("DELETE FROM order_items WHERE order_id = :id"), {"id": order_id})
        return self.create_items(connection, order_id, items)

    def update_order(self, connection: Connection, order_id: UUID, promo_id: UUID | None,
                     total_amount, discount_amount):
        return connection.execute(text(f"""
            UPDATE orders SET promo_code_id = :promo_id, total_amount = :total_amount,
                discount_amount = :discount_amount
            WHERE id = :id RETURNING {ORDER_COLUMNS}
        """), {"id": order_id, "promo_id": promo_id,
               "total_amount": total_amount, "discount_amount": discount_amount}).mappings().one()

    def set_status(self, connection: Connection, order_id: UUID, status: str):
        return connection.execute(text(f"""
            UPDATE orders SET status = :status WHERE id = :id RETURNING {ORDER_COLUMNS}
        """), {"id": order_id, "status": status}).mappings().one()

    def record_operation(self, connection: Connection, user_id: UUID, operation_type: str) -> None:
        connection.execute(text("""
            INSERT INTO user_operations (user_id, operation_type, created_at)
            VALUES (:user_id, :operation_type, clock_timestamp())
        """), {"user_id": user_id, "operation_type": operation_type})
