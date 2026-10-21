# Импорт регистрирует реализацию сгенерированного API.
from app.api_impl.health import HealthApi  # noqa: F401

# Приложение со сгенерированными маршрутами.
from marketplace_api.main import app  # noqa: F401