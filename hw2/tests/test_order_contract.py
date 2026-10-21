"""Order input restrictions and configuration without a running database."""
from datetime import timedelta
from uuid import uuid4

from pydantic import ValidationError
import pytest

import app.api_impl.orders as orders_api
from app.errors import AppError
from app.services.orders import OrdersService
from marketplace_api.models.order_create import OrderCreate
from marketplace_api.models.order_update import OrderUpdate


@pytest.mark.parametrize("model", [OrderCreate, OrderUpdate])
def test_product_uuid_uniqueness_uses_uuid_value_not_string_case(model):
    product = str(uuid4())
    with pytest.raises(ValidationError):
        model(items=[{"product_id": product, "quantity": 1},
                     {"product_id": product.upper(), "quantity": 2}])


def test_order_item_does_not_accept_client_price():
    with pytest.raises(ValidationError):
        OrderCreate(items=[{"product_id": str(uuid4()), "quantity": 1, "price_at_order": 0.01}])


@pytest.mark.parametrize("model", [OrderCreate, OrderUpdate])
def test_service_rejects_duplicates_after_internal_dto_list_mutation(model):
    payload = model(items=[{"product_id": str(uuid4()), "quantity": 1}])
    payload.items.append(payload.items[0])
    # No engine.begin is available: rejection must precede any transaction.
    with pytest.raises(AppError) as caught:
        service = OrdersService(object())
        if model is OrderCreate:
            service.create_order(uuid4(), payload)
        else:
            service.update_order(uuid4(), uuid4(), payload)
    assert caught.value.error_code == "VALIDATION_ERROR" and caught.value.status_code == 400


@pytest.mark.parametrize("minutes", ["0", "2.5", "10"])
@pytest.mark.parametrize("operation", ["CREATE", "UPDATE"])
def test_order_interval_is_configurable(monkeypatch, minutes, operation):
    monkeypatch.setenv(f"ORDER_{operation}_INTERVAL_MINUTES", minutes)
    monkeypatch.setattr(orders_api, "get_engine", lambda: object())
    service = orders_api.get_orders_service()
    assert getattr(service, operation.lower() + "_interval") == timedelta(minutes=float(minutes))


@pytest.mark.parametrize("minutes", ["-1", "NaN", "infinity", "invalid"])
@pytest.mark.parametrize("operation", ["CREATE", "UPDATE"])
def test_invalid_order_interval_is_rejected(monkeypatch, minutes, operation):
    monkeypatch.setenv(f"ORDER_{operation}_INTERVAL_MINUTES", minutes)
    monkeypatch.setattr(orders_api, "get_engine", lambda: object())
    with pytest.raises((ValueError, OverflowError)):
        orders_api.get_orders_service()
