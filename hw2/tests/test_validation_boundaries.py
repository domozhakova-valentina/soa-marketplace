"""Accepted boundaries and rejected neighbours from the homework requirements."""
from uuid import uuid4

from pydantic import ValidationError
import pytest

from marketplace_api.models.order_create import OrderCreate
from marketplace_api.models.order_update import OrderUpdate
from test_generated_contract import api, product_input  # noqa: F401


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("changes", [
    {"name": "x"}, {"name": "x" * 255}, {"description": "x" * 4000},
    {"description": None}, {"price": 0.01}, {"stock": 0},
    {"category": "x"}, {"category": "x" * 100},
])
def test_product_boundary_is_accepted_by_generated_http_route(api, method, changes):
    client, calls = api
    path = "/products" if method == "POST" else "/products/11111111-1111-4111-8111-111111111111"
    response = client.request(method, path, json=product_input(**changes))
    assert response.status_code == (201 if method == "POST" else 200), response.text
    assert len(calls) == 1


@pytest.mark.parametrize("model", [OrderCreate, OrderUpdate])
@pytest.mark.parametrize("count", [1, 50])
@pytest.mark.parametrize("quantity", [1, 999])
def test_order_accepts_both_item_count_and_quantity_boundaries(model, count, quantity):
    payload = model(items=[{"product_id": str(uuid4()), "quantity": quantity} for _ in range(count)])
    assert len(payload.items) == count
    assert all(item.quantity == quantity for item in payload.items)


@pytest.mark.parametrize("model", [OrderCreate, OrderUpdate])
@pytest.mark.parametrize("count,quantity", [(0, 1), (51, 1), (1, 0), (1, 1000)])
def test_order_rejects_values_immediately_outside_boundaries(model, count, quantity):
    with pytest.raises(ValidationError):
        model(items=[{"product_id": str(uuid4()), "quantity": quantity} for _ in range(count)])


@pytest.mark.parametrize("code", ["AB_1", "A" * 20])
def test_promo_code_accepts_length_boundaries(code):
    payload = OrderCreate(items=[{"product_id": str(uuid4()), "quantity": 1}], promo_code=code)
    assert payload.promo_code == code


@pytest.mark.parametrize("code", ["ABC", "A" * 21, "lowercase", "SAVE-10", "SAVE 10", "СКИДКА"])
def test_promo_code_rejects_bad_length_or_alphabet(code):
    with pytest.raises(ValidationError):
        OrderCreate(items=[{"product_id": str(uuid4()), "quantity": 1}], promo_code=code)
