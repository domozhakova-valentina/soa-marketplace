"""Check every possible state pair, including terminal and same-state moves."""
import pytest

from app.errors import AppError
from app.services.order_states import require_editable, require_transition
from marketplace_api.models.order_status import OrderStatus


@pytest.mark.parametrize("current", list(OrderStatus))
@pytest.mark.parametrize("target", list(OrderStatus))
def test_order_state_machine(current, target):
    allowed = {
        ("CREATED", "PAYMENT_PENDING"), ("CREATED", "CANCELED"),
        ("PAYMENT_PENDING", "PAID"), ("PAYMENT_PENDING", "CANCELED"),
        ("PAID", "SHIPPED"), ("SHIPPED", "COMPLETED"),
    }
    if (current.value, target.value) in allowed:
        require_transition(current, target)
    else:
        with pytest.raises(AppError) as caught:
            require_transition(current, target)
        assert caught.value.status_code == 409
        assert caught.value.error_code == "INVALID_STATE_TRANSITION"
        assert caught.value.details["current_status"] == current.value
        assert caught.value.details["target_status"] == target.value


@pytest.mark.parametrize("current", list(OrderStatus))
def test_order_is_editable_only_in_created(current):
    if current == OrderStatus.CREATED:
        require_editable(current.value)
    else:
        with pytest.raises(AppError) as caught:
            require_editable(current)
        assert caught.value.error_code == "INVALID_STATE_TRANSITION"
        assert caught.value.details == {"current_status": current.value, "allowed_statuses": ["CREATED"]}
