"""Exercise error handlers without a database or production API implementations."""
import json
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.contract_http import install_contract_handlers
from app.errors import AppError
from app.middleware.request_logging import error_logger
from marketplace_api.models.error_response import ErrorResponse


@pytest.fixture(autouse=True)
def capture_error_logs(caplog):
    error_logger.addHandler(caplog.handler)
    yield
    error_logger.removeHandler(caplog.handler)


def error_client(error):
    app = FastAPI()
    install_contract_handlers(app)

    @app.get("/probe")
    async def probe():
        raise error

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("code,status,message", [
    ("VALIDATION_ERROR", 400, "Некорректные входные данные"),
    ("PRODUCT_NOT_FOUND", 404, "Товар не найден"),
    ("ACCESS_DENIED", 403, "Недостаточно прав"),
    ("TOKEN_INVALID", 401, "Невалидный токен доступа"),
    ("TOKEN_EXPIRED", 401, "Срок действия токена истёк"),
    ("INTERNAL_ERROR", 500, "Внутренняя ошибка сервера"),
])
def test_application_error_uses_generated_response(code, status, message):
    error = AppError(code, message, status)
    assert str(error) == message
    with error_client(error) as client:
        response = client.get("/probe")
    assert response.status_code == status
    assert response.json() == {"error_code": code, "message": message}
    ErrorResponse.model_validate(response.json())
    UUID(response.headers["X-Request-Id"])


def test_application_error_keeps_structured_details():
    details = {"fields": [{"field": "items[0].quantity", "message": "От 1 до 999"}]}
    with error_client(AppError("VALIDATION_ERROR", "Ошибка", 400, details)) as client:
        response = client.get("/probe")
    assert response.status_code == 400
    assert response.json()["details"] == details
    ErrorResponse.model_validate(response.json())


@pytest.mark.parametrize("error", [
    RuntimeError("private application data"),
    OperationalError("private SQL", {"password": "secret"}, Exception("connection failed")),
    IntegrityError("private SQL", {"password": "secret"}, Exception("constraint failed")),
])
def test_unknown_errors_are_internal_and_do_not_leak_details(error, caplog):
    with error_client(error) as client:
        response = client.get("/probe")
    assert response.status_code == 500
    assert response.json() == {
        "error_code": "INTERNAL_ERROR", "message": "Внутренняя ошибка сервера",
    }
    UUID(response.headers["X-Request-Id"])
    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == error_logger.name]
    assert any(event["exception_type"] == type(error).__name__ for event in events)
    assert "private SQL" not in caplog.text and "private application data" not in caplog.text
    assert '"password"' not in caplog.text


@pytest.mark.parametrize("status,code", [(400, "VALIDATION_ERROR"),
                                         (401, "TOKEN_INVALID"),
                                         (403, "ACCESS_DENIED"),
                                         (418, "HTTP_ERROR"),
                                         (500, "INTERNAL_ERROR")])
def test_http_exceptions_keep_contract_and_headers(status, code):
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    with error_client(HTTPException(status, "private detail", headers)) as client:
        response = client.get("/probe")
    assert response.status_code == status
    assert response.json()["error_code"] == code
    ErrorResponse.model_validate(response.json())
    if headers:
        assert response.headers["WWW-Authenticate"] == "Bearer"
