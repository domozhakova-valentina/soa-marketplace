"""Convert expected and unexpected exceptions to the generated error DTO."""
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.contract_http import ContractJSONResponse
from app.errors import AppError
from app.middleware.request_logging import log_server_error


def error_response(code: str, message: str, status: int,
                   details: dict | None = None, headers: dict | None = None) -> Response:
    # Generated models import the JSON helpers; avoid a module import cycle.
    from marketplace_api.models.error_response import ErrorResponse

    values = {"error_code": code, "message": message}
    if details is not None:
        values["details"] = details
    return ContractJSONResponse(ErrorResponse(**values), status_code=status, headers=headers)


def _field_path(location: tuple) -> str:
    parts = list(location)
    if parts and parts[0] in {"body", "path", "query", "header", "cookie"}:
        parts.pop(0)
    path = ""
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else ("." if path else "") + str(part)
    return path or "body"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def application_error(request: Request, error: AppError):
        headers = {"WWW-Authenticate": "Bearer"} if error.error_code in {"TOKEN_INVALID", "TOKEN_EXPIRED"} else None
        if error.error_code == "ORDER_LIMIT_EXCEEDED" and error.details is not None and "retry_after_seconds" in error.details:
            headers = {"Retry-After": str(error.details["retry_after_seconds"])}
        return error_response(error.error_code, error.message, error.status_code, error.details, headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return error_response("VALIDATION_ERROR", "Некорректные входные данные", 400, {
            "fields": [{"field": _field_path(item["loc"]), "message": item["msg"]}
                       for item in error.errors()]
        })

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        if error.status_code == 400:
            return error_response("VALIDATION_ERROR", "Некорректные входные данные", 400,
                                  {"fields": [{"field": "body", "message": str(error.detail)}]}, error.headers)
        if error.status_code == 401:
            return error_response("TOKEN_INVALID", "Невалидный токен доступа", 401, headers=error.headers)
        if error.status_code == 403:
            return error_response("ACCESS_DENIED", "Недостаточно прав", 403, headers=error.headers)
        if error.status_code >= 500:
            log_server_error(request, error, error.status_code)
            return error_response("INTERNAL_ERROR", "Внутренняя ошибка сервера", error.status_code,
                                  headers=error.headers)
        code, message = {
            404: ("NOT_FOUND", "Маршрут не найден"),
            405: ("METHOD_NOT_ALLOWED", "Метод не разрешён для этого маршрута"),
        }.get(error.status_code, ("HTTP_ERROR", "Ошибка HTTP-запроса"))
        return error_response(code, message, error.status_code, headers=error.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        log_server_error(request, error)
        response = error_response("INTERNAL_ERROR", "Внутренняя ошибка сервера", 500)
        # Starlette's server error handler is outside user middleware.
        response.headers["X-Request-Id"] = getattr(request.state, "request_id", str(uuid4()))
        return response
