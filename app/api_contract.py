from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException


API_VERSION = "v1"
IDEMPOTENCY_TTL = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "86400"))
IDEMPOTENCY_MAX_RESPONSE = int(os.getenv("IDEMPOTENCY_MAX_RESPONSE_BYTES", "1048576"))
API_RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "120"))
API_RATE_WINDOW = int(os.getenv("API_RATE_WINDOW_SECONDS", "60"))

_RATE_EVENTS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = Lock()


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": _request_id(request)}},
        headers={"Cache-Control": "no-store"},
    )


def _rate_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    identity = hashlib.sha256(authorization.encode("utf-8")).hexdigest() if authorization else "anonymous"
    host = request.client.host if request.client else "unknown"
    return f"{host}:{identity}"


def _rate_limited(key: str) -> tuple[bool, int, int]:
    now = time.monotonic()
    with _RATE_LOCK:
        events = _RATE_EVENTS[key]
        cutoff = now - API_RATE_WINDOW
        while events and events[0] <= cutoff:
            events.popleft()
        count = len(events)
        if count >= API_RATE_LIMIT:
            retry_after = max(1, int(events[0] + API_RATE_WINDOW - now))
            return True, count, retry_after
        events.append(now)
        if len(_RATE_EVENTS) > 10000:
            for old_key in list(_RATE_EVENTS):
                if not _RATE_EVENTS[old_key] or _RATE_EVENTS[old_key][-1] <= now - API_RATE_WINDOW * 2:
                    _RATE_EVENTS.pop(old_key, None)
        return False, count + 1, 0


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _idempotency_scope(request: Request, key: str) -> str:
    authorization = request.headers.get("authorization", "")
    principal = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    raw = f"{principal}|{request.client.host if request.client else 'unknown'}|{request.method}|{request.url.path}|{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_idempotency(engine, scope: str):
    from sqlalchemy import text

    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT request_hash, status_code, content_type, response_body "
                "FROM api_idempotency_keys "
                "WHERE scope_key = :scope AND expires_at > CURRENT_TIMESTAMP"
            ),
            {"scope": scope},
        ).mappings().first()


def _store_idempotency(engine, scope: str, request_hash: str, response: Response, body: bytes) -> bool:
    from sqlalchemy import text

    if len(body) > IDEMPOTENCY_MAX_RESPONSE:
        return False
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        return False
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            result = connection.execute(
                text(
                    "INSERT OR IGNORE INTO api_idempotency_keys "
                    "(scope_key, request_hash, status_code, content_type, response_body, created_at, expires_at) "
                    "VALUES (:scope, :request_hash, :status_code, :content_type, :response_body, "
                    "CURRENT_TIMESTAMP, datetime('now', '+' || :ttl || ' seconds'))"
                ),
                {"scope": scope, "request_hash": request_hash, "status_code": response.status_code,
                 "content_type": content_type, "response_body": body.decode("utf-8"), "ttl": str(IDEMPOTENCY_TTL)},
            )
        else:
            result = connection.execute(
                text(
                    "INSERT INTO api_idempotency_keys "
                    "(scope_key, request_hash, status_code, content_type, response_body, created_at, expires_at) "
                    "VALUES (:scope, :request_hash, :status_code, :content_type, :response_body, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + (:ttl * INTERVAL '1 second')) "
                    "ON CONFLICT (scope_key) DO NOTHING"
                ),
                {"scope": scope, "request_hash": request_hash, "status_code": response.status_code,
                 "content_type": content_type, "response_body": body.decode("utf-8"), "ttl": IDEMPOTENCY_TTL},
            )
    return result.rowcount == 1


async def _capture_response(response: Response) -> tuple[Response, bytes]:
    if not hasattr(response, "body_iterator"):
        return response, getattr(response, "body", b"") or b""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.body_iterator:
        data = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        chunks.append(data)
        total += len(data)
    body = b"".join(chunks)
    # Rebuild the response so enabling idempotency never truncates the original response.
    response = Response(content=body, status_code=response.status_code, headers=dict(response.headers), media_type=None)
    return response, body


def install_api_contract(app: FastAPI, engine) -> None:
    @app.middleware("http")
    async def api_contract_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        is_api = request.url.path.startswith("/api/")
        if is_api:
            limited, count, retry_after = _rate_limited(_rate_key(request))
            if limited:
                response = _error_response(request, 429, "rate_limited", "Too many API requests.")
                response.headers["Retry-After"] = str(retry_after)
                return response
            request.state.rate_count = count

        mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        body = await request.body() if is_api and mutating else b""
        request_hash = _body_hash(body)
        scope = _idempotency_scope(request, idempotency_key) if idempotency_key and is_api and mutating else ""

        if scope:
            existing = _load_idempotency(engine, scope)
            if existing:
                if existing["request_hash"] != request_hash:
                    return _error_response(request, 409, "idempotency_key_reused", "Idempotency-Key was already used with a different request body.")
                replay = Response(
                    content=existing["response_body"],
                    status_code=existing["status_code"],
                    media_type=existing["content_type"],
                    headers={"X-Request-ID": request.state.request_id, "X-Idempotent-Replay": "true", "Cache-Control": "no-store"},
                )
                replay.headers["X-API-Version"] = API_VERSION
                return replay

        response = await call_next(request)
        if scope:
            response, response_body = await _capture_response(response)
            if response_body:
                _store_idempotency(engine, scope, request_hash, response, response_body)

        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-API-Version"] = API_VERSION
        if is_api:
            response.headers["X-RateLimit-Limit"] = str(API_RATE_LIMIT)
            response.headers["X-RateLimit-Remaining"] = str(max(0, API_RATE_LIMIT - getattr(request.state, "rate_count", 0)))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            return _error_response(request, 422, "validation_error", "Request validation failed.")
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
            return _error_response(request, exc.status_code, "http_error", detail)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
