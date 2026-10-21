"""Order operations: one transaction, prescribed validation order, exact money."""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil
from uuid import UUID

from sqlalchemy.engine import Engine

from app.errors import AppError
from app.repositories.orders import OrdersRepository
from app.security.authorization import require_order_owner, require_roles
from app.services.order_states import require_editable, require_transition
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.models.order_create import OrderCreate
from marketplace_api.models.order_response import OrderResponse
from marketplace_api.models.order_status import OrderStatus
from marketplace_api.models.order_update import OrderUpdate


CENT = Decimal("0.01")
MAX_MONEY = Decimal("9999999999.99")


def money(value: Decimal) -> Decimal:
    rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
    if not rounded.is_finite() or not Decimal("0") <= rounded <= MAX_MONEY:
        raise AppError("VALIDATION_ERROR", "Сумма заказа превышает допустимый диапазон", 400, {
            "fields": [{"field": "items", "message": "Денежные суммы должны помещаться в DECIMAL(12,2)"}]
        })
    return rounded


class OrdersService:
    def __init__(self, engine: Engine, create_interval: timedelta = timedelta(minutes=5),
                 repository: OrdersRepository | None = None, *,
                 update_interval: timedelta = timedelta(minutes=5)):
        if create_interval < timedelta(0) or update_interval < timedelta(0):
            raise ValueError("Order operation intervals cannot be negative")
        self.engine = engine
        self.create_interval = create_interval
        self.update_interval = update_interval
        self.repository = repository if repository is not None else OrdersRepository()

    @contextmanager
    def locked_transaction(self, subject: UUID, order_id: UUID | None = None):
        """Synchronous transaction boundary shared by all order operations.

        For existing orders: lock actor, lock order, verify owner; the caller
        then locks all involved products by UUID and finally the promo row.
        Keep DTO validation inside the context so any failure rolls back.
        """
        with self.engine.begin() as connection:
            user = self.repository.lock_user(connection, subject)
            if user is None:
                raise AppError("TOKEN_INVALID", "Пользователь токена не найден", 401)
            principal = TokenModel(sub=str(user["id"]), role=user["role"])
            require_roles(principal, ("USER", "ADMIN"))
            order = None
            if order_id is not None:
                order = self.repository.lock_order(connection, order_id)
                if order is None:
                    raise AppError("ORDER_NOT_FOUND", "Заказ не найден", 404)
                require_order_owner(principal, order["user_id"])
            yield connection, order

    @staticmethod
    def _unique_products(payload: OrderCreate | OrderUpdate) -> None:
        # The generated model rejects duplicates at the HTTP boundary. Preserve
        # the invariant if an internal caller mutates the DTO's list afterward.
        product_ids = [item.product_id for item in payload.items]
        if len(product_ids) != len(set(product_ids)):
            raise AppError("VALIDATION_ERROR", "Некорректные входные данные", 400, {
                "fields": [{"field": "items", "message": "product_id не должен повторяться"}]
            })

    def _check_rate(self, connection, subject: UUID, operation: str, interval: timedelta) -> None:
        now = self.repository.now(connection)
        last = self.repository.last_operation(connection, subject, operation)
        if last is not None and now - last < interval:
            retry_after = ceil((last + interval - now).total_seconds())
            raise AppError("ORDER_LIMIT_EXCEEDED", "Превышен лимит частоты операций с заказом", 429,
                           {"operation_type": operation, "retry_after_seconds": retry_after})

    def _reserve(self, connection, items, products):
        # Catalog checks precede all inventory checks, including on update.
        for item in items:
            product = products.get(item.product_id)
            if product is None:
                raise AppError("PRODUCT_NOT_FOUND", "Товар не найден", 404,
                               {"product_id": str(item.product_id)})
            if product["status"] != "ACTIVE":
                raise AppError("PRODUCT_INACTIVE", "Товар не находится в состоянии ACTIVE", 409,
                               {"product_id": str(item.product_id)})
        shortages = [{"product_id": str(item.product_id), "requested": item.quantity,
                      "available": products[item.product_id]["stock"]}
                     for item in items if products[item.product_id]["stock"] < item.quantity]
        if shortages:
            raise AppError("INSUFFICIENT_STOCK", "Недостаточно остатков товаров", 409,
                           {"products": shortages})
        snapshots = [{"product_id": item.product_id, "quantity": item.quantity,
                      "price_at_order": products[item.product_id]["price"]} for item in items]
        self.repository.reserve_products(connection, snapshots)
        subtotal = money(sum((item["price_at_order"] * item["quantity"] for item in snapshots), Decimal("0")))
        return snapshots, subtotal

    def _validate_promo(self, connection, promo, *, own_use=False) -> None:
        now = self.repository.now(connection)
        if (promo is None or not promo["active"]
                or promo["current_uses"] - int(own_use) >= promo["max_uses"]
                or not promo["valid_from"] <= now <= promo["valid_until"]):
            raise AppError("PROMO_CODE_INVALID", "Промокод недействителен", 422)
        if own_use and promo["current_uses"] < 1:
            raise RuntimeError("Order's promo usage is missing")

    @staticmethod
    def _discount(subtotal: Decimal, promo) -> Decimal:
        if promo["discount_type"] == "PERCENTAGE":
            # Homework: above 70%, "price is 100%", i.e. no discount.
            if promo["discount_value"] > Decimal("70"):
                return Decimal("0.00")
            return money(subtotal * promo["discount_value"] / Decimal("100"))
        return money(min(promo["discount_value"], subtotal))

    def create_order(self, subject: UUID, payload: OrderCreate) -> OrderResponse:
        self._unique_products(payload)
        with self.locked_transaction(subject) as (connection, _):
            # 1. The journal is scoped by operation type, not simply by user.
            self._check_rate(connection, subject, "CREATE_ORDER", self.create_interval)

            # 2. Checked after the rate limit, including PAYMENT_PENDING.
            if self.repository.has_active_order(connection, subject):
                raise AppError("ORDER_HAS_ACTIVE", "У пользователя уже есть активный заказ", 409)

            # 3. Lock in UUID order to avoid reversed-item-order deadlocks.
            products = {row["id"]: row for row in self.repository.lock_products(
                connection, [item.product_id for item in payload.items])}
            # 4-6. Validate all stock, reserve it and capture locked prices.
            snapshots, subtotal = self._reserve(connection, payload.items, products)

            # 7. A promo failure after reservation still rolls everything back.
            promo_id = None
            discount = Decimal("0.00")
            if payload.promo_code is not None:
                promo = self.repository.lock_promo(connection, payload.promo_code)
                self._validate_promo(connection, promo)
                if subtotal < promo["min_order_amount"]:
                    raise AppError("PROMO_CODE_MIN_AMOUNT", "Сумма заказа ниже минимальной для промокода", 422,
                                   {"order_amount": subtotal, "min_order_amount": promo["min_order_amount"]})
                discount = self._discount(subtotal, promo)
                promo_id = promo["id"]
                self.repository.use_promo(connection, promo_id)
            total = money(subtotal - discount)

            # 8. All inserts and the generated response validation precede commit.
            order = self.repository.create_order(connection, subject, promo_id, total, discount)
            items = self.repository.create_items(connection, order["id"], snapshots)
            self.repository.record_operation(connection, subject, "CREATE_ORDER")
            return OrderResponse(**order, items=items)

    def get_order(self, subject: UUID, order_id: UUID) -> OrderResponse:
        # Parent lock keeps the order and its items a consistent snapshot.
        with self.locked_transaction(subject, order_id) as (connection, order):
            return OrderResponse(**order, items=self.repository.get_items(connection, order_id))

    def update_order(self, subject: UUID, order_id: UUID, payload: OrderUpdate) -> OrderResponse:
        self._unique_products(payload)
        with self.locked_transaction(subject, order_id) as (connection, order):
            # Ownership is checked by locked_transaction before state and rate.
            require_editable(order["status"])
            self._check_rate(connection, subject, "UPDATE_ORDER", self.update_interval)
            old_items = self.repository.get_items(connection, order_id)
            involved = {item["product_id"] for item in old_items} | {item.product_id for item in payload.items}
            products = {row["id"]: dict(row) for row in self.repository.lock_products(connection, list(involved))}

            self.repository.restore_products(connection, old_items)
            for item in old_items:
                products[item["product_id"]]["stock"] += item["quantity"]
            snapshots, subtotal = self._reserve(connection, payload.items, products)

            promo_id = order["promo_code_id"]
            discount = Decimal("0.00")
            if promo_id is not None:
                promo = self.repository.lock_promo_by_id(connection, promo_id)
                self._validate_promo(connection, promo, own_use=True)
                if subtotal < promo["min_order_amount"]:
                    self.repository.release_promo(connection, promo_id)
                    promo_id = None
                else:
                    # This order already owns one use, even at max_uses.
                    discount = self._discount(subtotal, promo)
            total = money(subtotal - discount)
            updated = self.repository.update_order(connection, order_id, promo_id, total, discount)
            items = self.repository.replace_items(connection, order_id, snapshots)
            self.repository.record_operation(connection, subject, "UPDATE_ORDER")
            return OrderResponse(**updated, items=items)

    def cancel_order(self, subject: UUID, order_id: UUID) -> OrderResponse:
        with self.locked_transaction(subject, order_id) as (connection, order):
            require_transition(order["status"], OrderStatus.CANCELED)
            items = self.repository.get_items(connection, order_id)
            self.repository.lock_products(connection, [item["product_id"] for item in items])
            self.repository.restore_products(connection, items)
            if order["promo_code_id"] is not None:
                self.repository.lock_promo_by_id(connection, order["promo_code_id"])
                # Cancellation releases a use even if the promo has expired.
                self.repository.release_promo(connection, order["promo_code_id"])
            canceled = self.repository.set_status(connection, order_id, OrderStatus.CANCELED.value)
            # Keep historical items, promo link, total and discount unchanged.
            return OrderResponse(**canceled, items=items)
