"""Request correlation, secret masking and original-body delivery through ASGI."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import json
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError

from app.contract_http import install_contract_handlers
from app.middleware.request_logging import MAX_BODY_BYTES, RequestLoggingMiddleware, error_logger, logger
from app.security.tokens import get_token_service
from marketplace_api.models.user_response import UserResponse
from test_auth import PASSWORD, auth_client, bearer, login, password_hash  # noqa: F401


@pytest.fixture
def log_capture(caplog):
    for target in (logger, error_logger):
        target.addHandler(caplog.handler)
    yield caplog
    for target in (logger, error_logger):
        target.removeHandler(caplog.handler)


def requests(capture):
    return [json.loads(record.getMessage()) for record in capture.records if record.name == logger.name]


def assert_request(capture, response, method, endpoint, *, user_id=None):
    events = requests(capture)
    assert len(events) == 1, events
    event = events[0]
    assert event["request_id"] == response.headers["X-Request-Id"]
    assert UUID(event["request_id"]).version == 4
    assert event["method"] == method and event["endpoint"] == endpoint
    assert event["status_code"] == response.status_code
    assert event["duration_ms"] >= 0
    assert event["user_id"] == user_id
    assert datetime.fromisoformat(event["timestamp"]).utcoffset().total_seconds() == 0
    return event


@pytest.fixture
def probe_client():
    app = FastAPI()
    install_contract_handlers(app)

    @app.api_route("/echo", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def echo(request: Request):
        first, second = await request.body(), await request.body()
        assert first == second
        return {"received": first.decode("utf-8")}

    @app.get("/status/{status}")
    async def status(status: int):
        if status >= 400:
            raise HTTPException(status, "private detail")
        return {"ok": True}

    @app.post("/failure")
    async def failure(request: Request):
        await request.body()
        raise OperationalError("private SQL", {"password": "SQL_PASSWORD_SECRET"},
                               Exception("DATABASE_CONNECTION_SECRET"))

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_requests_preserve_body_and_log_it(probe_client, log_capture, method):
    payload = {"items": [{"product_id": str(uuid4()), "quantity": 2}], "name": "Русский текст\nстрока"}
    response = probe_client.request(method, "/echo", json=payload)
    assert response.status_code == 200
    assert json.loads(response.json()["received"]) == payload
    event = assert_request(log_capture, response, method, "/echo")
    assert event["body"] == payload


def test_secrets_are_redacted_at_any_nesting_level_without_changing_request(probe_client, log_capture):
    payload = {"password": "LOGIN_PASSWORD_SECRET", "profile": {
        "PaSsWoRd_HaSh": "HASH_SECRET", "api-key": "API_KEY_SECRET", "clientSecret": "CLIENT_SECRET",
        "Authorization": "Bearer AUTHORIZATION_SECRET", "Cookie": "SESSION_COOKIE_SECRET",
        "children": [{"refresh_token": "REFRESH_SECRET", "accessToken": "ACCESS_SECRET"}],
    }, "description": "Bearer INLINE_SECRET", "name": "visible"}
    response = probe_client.post("/echo", json=payload)
    assert json.loads(response.json()["received"]) == payload
    event = assert_request(log_capture, response, "POST", "/echo")
    assert event["body"]["name"] == "visible"
    assert event["body"]["password"] == "***"
    assert event["body"]["profile"]["children"] == [{"refresh_token": "***", "accessToken": "***"}]
    assert "SECRET" not in log_capture.text


def test_jwt_in_an_ordinary_string_is_also_redacted(probe_client, log_capture):
    token = get_token_service().issue_access(UserResponse(id=uuid4(), username="buyer", role="USER")).access_token
    response = probe_client.post("/echo", json={"description": "token: " + token})
    assert response.status_code == 200
    assert token not in log_capture.text
    assert requests(log_capture)[0]["body"]["description"] == "token: ***"


@pytest.mark.parametrize("content, media_type, reason", [
    (b'{"password":"BROKEN_JSON_SECRET",', "application/json", "invalid_json"),
    (b'{"password":"BAD_UTF8_SECRET", "x":"\xff"}', "application/json", "invalid_json"),
    (b"password=FORM_PASSWORD_SECRET", "application/x-www-form-urlencoded", "unsupported_media_type"),
    (b"BINARY_SECRET", "application/octet-stream", "unsupported_media_type"),
    (b'{"price": NaN}', "application/json", "invalid_json"),
])
def test_unparseable_or_non_json_bodies_do_not_leak_raw_bytes(probe_client, log_capture, content, media_type, reason):
    # Invalid UTF-8 cannot be echoed by the probe; use an early 404 instead.
    response = probe_client.post("/missing", content=content, headers={"Content-Type": media_type})
    event = assert_request(log_capture, response, "POST", "/missing")
    assert event["body"]["omitted"] == reason
    assert "SECRET" not in log_capture.text


def test_large_body_is_omitted_but_delivered_to_handler_in_full(probe_client, log_capture):
    payload = {"description": "X" * (MAX_BODY_BYTES * 2), "password": "LARGE_BODY_SECRET"}
    response = probe_client.put("/echo", json=payload)
    assert response.status_code == 200, response.text
    assert json.loads(response.json()["received"]) == payload
    event = assert_request(log_capture, response, "PUT", "/echo")
    assert event["body"] == {"omitted": "body_too_large", "limit_bytes": MAX_BODY_BYTES}
    assert "LARGE_BODY_SECRET" not in log_capture.text


def test_json_log_keeps_exact_decimals_and_large_exponents(probe_client, log_capture):
    content = b'{"price": 1234567890.12, "large": 1e999}'
    response = probe_client.post("/echo", content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    raw = next(record.getMessage() for record in log_capture.records if record.name == logger.name)
    event = json.loads(raw, parse_float=Decimal)
    assert event["body"] == {"price": Decimal("1234567890.12"), "large": Decimal("1e999")}


def test_unrepresentable_decimal_is_omitted_without_changing_response(probe_client, log_capture):
    content = b'{"value": 1e9999999999999999999999999999999999}'
    response = probe_client.post("/echo", content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.json()["received"].encode() == content
    assert assert_request(log_capture, response, "POST", "/echo")["body"]["omitted"] == "invalid_json"


def test_unpaired_unicode_escape_still_produces_valid_json_log(probe_client, log_capture, capsys):
    response = probe_client.post("/echo", content=b'{"name":"\\ud800"}',
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    event = json.loads(capsys.readouterr().out)
    assert event["body"] == {"name": "\ud800"}
    assert event["request_id"] == response.headers["X-Request-Id"]


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 500])
def test_statuses_and_query_secrets_are_logged_correctly(probe_client, log_capture, status):
    path = f"/status/{status}"
    response = probe_client.get(path + "?access_token=QUERY_SECRET",
                                headers={"Authorization": "Bearer HEADER_SECRET", "Cookie": "COOKIE_SECRET"})
    assert response.status_code == status
    event = assert_request(log_capture, response, "GET", path)
    assert "body" not in event
    assert "SECRET" not in log_capture.text


def test_request_id_is_generated_instead_of_trusting_client_header(probe_client, log_capture):
    supplied = str(uuid4())
    response = probe_client.get("/status/200", headers={"X-Request-Id": supplied})
    assert response.headers["X-Request-Id"] != supplied
    assert_request(log_capture, response, "GET", "/status/200")


def test_unexpected_failure_has_same_id_and_no_sql_secrets(probe_client, log_capture, capsys):
    response = probe_client.post("/failure", json={"password": "REQUEST_PASSWORD_SECRET"})
    assert response.status_code == 500
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    event = assert_request(log_capture, response, "POST", "/failure")
    errors = [json.loads(record.getMessage()) for record in log_capture.records if record.name == error_logger.name]
    assert len(errors) == 1 and errors[0]["request_id"] == event["request_id"]
    assert errors[0]["exception_type"] == "OperationalError"
    assert errors[0]["stack"]
    output = capsys.readouterr().out
    assert "SECRET" not in output and "private SQL" not in output
    assert "SECRET" not in log_capture.text and "private SQL" not in log_capture.text
    # stdout consists of individually parseable JSON lines, without prefixes
    # or a multiline traceback that would break a log collector.
    lines = [json.loads(line) for line in output.splitlines()]
    assert len(lines) == 2


def test_empty_body_is_explicit_null(probe_client, log_capture):
    response = probe_client.delete("/echo")
    assert response.status_code == 200
    assert assert_request(log_capture, response, "DELETE", "/echo")["body"] is None


def test_parallel_requests_have_separate_ids(probe_client, log_capture):
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: probe_client.post("/echo", json={"quantity": 1}), range(12)))
    events = requests(log_capture)
    assert len(events) == 12
    assert {event["request_id"] for event in events} == {response.headers["X-Request-Id"] for response in responses}
    assert len({event["request_id"] for event in events}) == 12


@pytest.mark.parametrize("large", [False, True])
def test_chunked_bodies_are_replayed_exactly_once(log_capture, large):
    original = b'{"description":"' + b"x" * (MAX_BODY_BYTES * 2 if large else 10) + b'"}'
    chunks = [original[offset:offset + 127] for offset in range(0, len(original), 127)]
    inbox = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
             for index, chunk in enumerate(chunks)]
    delivered, sent = [], []

    async def receive():
        assert inbox, "Middleware or handler consumed a request chunk twice"
        return inbox.pop(0)

    async def send(message):
        sent.append(message)

    async def application(scope, receive, send):
        while True:
            message = await receive()
            delivered.append(message["body"])
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {"type": "http", "method": "PUT", "path": "/chunked", "headers": [(b"content-type", b"application/json")]}
    asyncio.run(RequestLoggingMiddleware(application)(scope, receive, send))
    assert b"".join(delivered) == original and not inbox
    event = requests(log_capture)[0]
    assert event["status_code"] == 204
    assert dict(sent[0]["headers"])[b"x-request-id"].decode() == event["request_id"]
    assert event["body"] == ({"omitted": "body_too_large", "limit_bytes": MAX_BODY_BYTES}
                             if large else json.loads(original))


def test_public_login_masks_password_and_refresh_masks_token(auth_client, log_capture):
    client, _, _, _, _ = auth_client
    pair = login(client)
    login_event = requests(log_capture)[0]
    assert login_event["body"] == {"username": "buyer", "password": "***"}
    assert login_event["user_id"] is None
    log_capture.clear()
    response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 200
    assert assert_request(log_capture, response, "POST", "/auth/refresh")["body"] == {"refresh_token": "***"}
    assert pair["refresh_token"] not in log_capture.text and PASSWORD not in log_capture.text


@pytest.mark.parametrize("role, status", [("buyer", 403), ("seller", 201)])
def test_verified_user_is_logged_for_success_and_role_denial(auth_client, log_capture, role, status):
    client, _, _, users, _ = auth_client
    token = login(client, role)["access_token"]
    log_capture.clear()
    response = client.post("/products", json={"name": "Logged product", "price": 1.23, "stock": 2,
                           "category": "TEST", "status": "ACTIVE"}, headers=bearer(token))
    assert response.status_code == status, response.text
    user_id = str(users.get_by_username(None, role)["id"])
    assert_request(log_capture, response, "POST", "/products", user_id=user_id)
    assert token not in log_capture.text


def test_invalid_and_stale_tokens_are_never_used_as_log_identity(auth_client, log_capture):
    client, store, _, users, _ = auth_client
    token = login(client)["access_token"]
    user = users.get_by_username(None, "buyer")
    store.users[user["id"]]["role"] = "ADMIN"
    log_capture.clear()
    response = client.get("/products", headers=bearer(token))
    assert response.status_code == 401
    assert_request(log_capture, response, "GET", "/products")
    log_capture.clear()
    response = client.get("/products", headers=bearer("INVALID_TOKEN_SECRET"))
    assert response.status_code == 401
    assert_request(log_capture, response, "GET", "/products")
    assert "INVALID_TOKEN_SECRET" not in log_capture.text


def test_generated_validation_error_is_logged_before_service_call(auth_client, log_capture):
    client, _, _, _, _ = auth_client
    response = client.post("/auth/register", json={"username": "x", "password": "SHORT_SECRET"})
    assert response.status_code == 400
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert assert_request(log_capture, response, "POST", "/auth/register")["body"]["password"] == "***"
    assert "SHORT_SECRET" not in log_capture.text
