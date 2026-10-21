# Импорт регистрирует реализацию сгенерированного API.
from app.api_impl.auth import AuthApi  # noqa: F401
from app.api_impl.health import HealthApi  # noqa: F401
from app.api_impl.products import ProductsApi  # noqa: F401
from app.api_impl.orders import OrdersApi  # noqa: F401
from app.api_impl.promo_codes import PromoCodesApi  # noqa: F401

# Приложение со сгенерированными маршрутами.
from marketplace_api.main import app  # noqa: F401

from app.contract_http import install_contract_handlers

install_contract_handlers(app)
