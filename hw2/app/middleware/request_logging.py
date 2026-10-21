"""One JSON access event per HTTP request, with bounded, redacted body capture."""
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import re
import sys
from time import perf_counter
import traceback
from uuid import uuid4

import simplejson
from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Covers even the largest valid Product request with four-byte Unicode text.
MAX_BODY_BYTES = 64 * 1024
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REDACTED = "***"
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT = re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class JSONLineHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # getMessage intentionally omits raw exception messages/SQL parameters.
        # Resolve stdout at emission time, including under redirected test output.
        try:
            sys.stdout.write(record.getMessage() + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def json_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(handler, JSONLineHandler) for handler in logger.handlers):
        logger.addHandler(JSONLineHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


logger = json_logger("marketplace.requests")
error_logger = json_logger("marketplace.errors")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _secret_key(key: str) -> bool:
    normalized = "".join(char for char in key.casefold() if char.isalnum())
    return (any(part in normalized for part in ("password", "passwd", "token", "secret", "credential",
                                               "apikey", "privatekey", "passphrase"))
            or normalized in {"pwd", "authorization", "cookie", "setcookie", "session", "sessionid",
                              "key", "accesskey", "cvv", "cvc", "pin", "cardnumber"})


def redact(value):
    if isinstance(value, dict):
        return {key: REDACTED if _secret_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _JWT.sub(REDACTED, _BEARER.sub("Bearer " + REDACTED, value))
    return value


def body_for_log(body: bytes, content_type: str, *, complete: bool):
    if not complete or len(body) > MAX_BODY_BYTES:
        return {"omitted": "body_too_large", "limit_bytes": MAX_BODY_BYTES}
    if not body:
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return {"omitted": "unsupported_media_type", "size_bytes": len(body)}
    try:
        # Values are for logging only. The handler still parses the original
        # bytes using the contract's exact Decimal-aware JSON implementation.
        def reject_constant(value):
            raise ValueError("Non-finite JSON value")

        return redact(json.loads(body, parse_float=Decimal, parse_constant=reject_constant))
    except (ValueError, ArithmeticError, RecursionError):
        return {"omitted": "invalid_json", "size_bytes": len(body)}


def log_server_error(request, error: Exception, status_code: int = 500) -> None:
    # Traceback locations aid debugging without printing exception text, SQL,
    # bound parameters or local variables, any of which may contain secrets.
    event = {"event": "server_error", "request_id": getattr(request.state, "request_id", None),
             "timestamp": timestamp(), "status_code": status_code, "exception_type": type(error).__name__,
             "stack": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                       for frame in traceback.extract_tb(error.__traceback__)]}
    error_logger.error(json.dumps(event, ensure_ascii=True, allow_nan=False))


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started, received_at = perf_counter(), timestamp()
        state = scope.setdefault("state", {})
        request_id = state["request_id"] = str(uuid4())
        state["user_id"] = None
        method = scope["method"]
        status_code = 500
        saved_messages = deque()
        body = bytearray()
        complete = False

        async def replay() -> Message:
            if saved_messages:
                return saved_messages.popleft()
            return await receive()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii"))]}
            await send(message)

        try:
            if method in MUTATING_METHODS:
                # Prefetch only a bounded prefix, preserving message boundaries.
                # The handler receives every original byte, including any suffix
                # beyond the capture limit, and can read request.body() repeatedly.
                while len(body) <= MAX_BODY_BYTES:
                    message = await receive()
                    saved_messages.append(message)
                    if message["type"] != "http.request":
                        break
                    chunk = message.get("body", b"")
                    body.extend(chunk[:MAX_BODY_BYTES + 1 - len(body)])
                    if not message.get("more_body", False):
                        complete = True
                        break
            await self.app(scope, replay, send_with_request_id)
        finally:
            event = {"request_id": request_id, "method": method, "endpoint": scope["path"],
                     "status_code": status_code, "duration_ms": round((perf_counter() - started) * 1000, 3),
                     "user_id": state.get("user_id"), "timestamp": received_at}
            if method in MUTATING_METHODS:
                content_type = next((value.decode("latin-1") for key, value in scope.get("headers", [])
                                     if key.lower() == b"content-type"), "")
                event["body"] = body_for_log(bytes(body), content_type, complete=complete)
            logger.info(simplejson.dumps(event, ensure_ascii=True, allow_nan=False, use_decimal=True))
