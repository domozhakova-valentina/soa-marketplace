"""HTTP support shared by generated routes, without hand-written request DTOs."""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
import re
from typing import Any
from uuid import UUID

import simplejson
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import ResponseValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter, ValidationError


def _json_default(value: Any) -> Any:
    if isinstance(value, (UUID, date, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.to_dict()
    raise TypeError(f"Unsupported JSON type: {type(value).__name__}")


def dumps_contract(value: Any) -> str:
    # Decimal becomes an exact JSON number, without a float conversion.
    return simplejson.dumps(value, use_decimal=True, ensure_ascii=False,
                            allow_nan=False, default=_json_default)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def loads_contract(value: str | bytes) -> Any:
    return json.loads(value, parse_float=Decimal, parse_constant=_reject_constant)


def query_integer(value: Any) -> int:
    if type(value) is int:
        return value
    if not isinstance(value, str) or re.fullmatch(r"[+-]?[0-9]+", value) is None:
        raise ValueError("an integer query parameter is required")
    return int(value)


class ContractJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps_contract(content).encode("utf-8")


class ExactJSONRequest(Request):
    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            self._json = loads_contract(await self.body())
        return self._json


class ContractRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            return await original(ExactJSONRequest(request.scope, request.receive))

        return handle


def contract_response(value: Any, model: Any, status_code: int) -> Response:
    if status_code == 204:
        return Response(status_code=204)
    try:
        validated = TypeAdapter(model).validate_python(value)
    except ValidationError as error:
        # Response failures are server errors, never invalid client requests.
        raise ResponseValidationError(error.errors(), body=value) from error
    return ContractJSONResponse(validated, status_code=status_code)


def install_contract_handlers(app: FastAPI) -> None:
    from app.middleware.error_handlers import register_error_handlers
    from app.middleware.request_logging import RequestLoggingMiddleware

    register_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)

    original_openapi = app.openapi

    def contract_openapi():
        schema = original_openapi()
        # FastAPI otherwise documents its automatic 422 even though input
        # errors now use 400. Keep the explicit business 422 promo responses.
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses", {})
                automatic = responses.get("422", {}).get("content", {}).get("application/json", {}).get("schema", {})
                if automatic.get("$ref") == "#/components/schemas/HTTPValidationError":
                    del responses["422"]
        return schema

    app.openapi = contract_openapi
