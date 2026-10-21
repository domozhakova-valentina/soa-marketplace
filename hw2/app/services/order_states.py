"""The order state machine and permissions for operations that retain state."""
from app.errors import AppError
from marketplace_api.models.order_status import OrderStatus


TRANSITIONS = {
    OrderStatus.CREATED: frozenset({OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELED}),
    OrderStatus.PAYMENT_PENDING: frozenset({OrderStatus.PAID, OrderStatus.CANCELED}),
    OrderStatus.PAID: frozenset({OrderStatus.SHIPPED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.COMPLETED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
}
EDITABLE_STATES = frozenset({OrderStatus.CREATED})


def require_editable(current: str | OrderStatus) -> None:
    current = OrderStatus(current)
    if current not in EDITABLE_STATES:
        raise AppError("INVALID_STATE_TRANSITION", "Изменение заказа разрешено только в CREATED", 409, {
            "current_status": current.value, "allowed_statuses": [state.value for state in EDITABLE_STATES],
        })


def require_transition(current: str | OrderStatus, target: str | OrderStatus) -> None:
    current, target = OrderStatus(current), OrderStatus(target)
    if target not in TRANSITIONS[current]:
        allowed_sources = [state.value for state in OrderStatus if target in TRANSITIONS[state]]
        raise AppError("INVALID_STATE_TRANSITION", "Переход состояния заказа запрещён", 409, {
            "current_status": current.value, "target_status": target.value,
            "allowed_statuses": allowed_sources,
        })
