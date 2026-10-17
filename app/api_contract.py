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
MAX_BODY_BYTES = int(os.getenv("API_MAX_BODY_BYTES", "1048576"))
IDEMPOTENCY_TTL = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "86400"))
IDEMPOTENCY_MAX_RESPONSE = int(os.getenv("IDEMPOTENCY_MAX_RESPONSE_BYTES", "1048576"))
API_RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "120"))
API_RATE_WINDOW = int(os.getenv("API_RATE_WINDOW_SECONDS", "60"))
if MAX_BODY_BYTES < 1024 or MAX_BODY_BYTES > 20 * 1024 * 1024:
    raise RuntimeError("API_MAX_BODY_BYTES must be between 1024 and 20971520")
if IDEMPOTENCY_TTL < 60 or IDEMPOTENCY_TTL > 30 * 86400:
    raise RuntimeError("IDEMPOTENCY_TTL_SECONDS must be between 60 and 2592000")
if IDEMPOTENCY_MAX_RESPONSE < 1024 or IDEMPOTENCY_MAX_RESPONSE > 20 * 1024 * 1024:
    raise RuntimeError("IDEMPOTENCY_MAX_RESPONSE_BYTES must be between 1024 and 20971520")
if API_RATE_LIMIT < 1 or API_RATE_LIMIT > 100000:
    raise RuntimeError("API_RATE_LIMIT must be between 1 and 100000")
if API_RATE_WINDOW < 1 or API_RATE_WINDOW > 86400:
    raise RuntimeError("API_RATE_WINDOW_SECONDS must be between 1 and 86400")

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
    async for chunk in response.body_iterator:
        data = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        chunks.append(data)
    body = b"".join(chunks)
    response = Response(content=body, status_code=response.status_code, headers=dict(response.headers), media_type=None)
    return response, body


def _audit_mutation(engine, request: Request, response: Response) -> None:
    try:
        from .security_audit import record_api_mutation

        record_api_mutation(
            engine,
            request_id=_request_id(request),
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            authorization=request.headers.get("authorization", ""),
            client_address=request.client.host if request.client else "unknown",
        )
    except Exception:
        # Auditing must never turn a successful user mutation into a 500 response.
        pass


def install_api_contract(app: FastAPI, engine) -> None:
    @app.middleware("http")
    async def api_contract_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        supplied_request_id = request.headers.get("X-Request-ID", "").strip()
        if supplied_request_id and len(supplied_request_id) <= 100 and all(char.isalnum() or char in "-_." for char in supplied_request_id):
            request.state.request_id = supplied_request_id
        else:
            request.state.request_id = uuid.uuid4().hex
        is_api = request.url.path.startswith("/api/")
        mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if is_api and mutating:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_BODY_BYTES:
                        response = _error_response(request, 413, "body_too_large", "Request body exceeds the configured limit.")
                        response.headers["X-Request-ID"] = request.state.request_id
                        return response
                except ValueError:
                    response = _error_response(request, 400, "invalid_content_length", "Invalid Content-Length header.")
                    response.headers["X-Request-ID"] = request.state.request_id
                    return response
        if is_api:
            limited, count, retry_after = _rate_limited(_rate_key(request))
            if limited:
                response = _error_response(request, 429, "rate_limited", "Too many API requests.")
                response.headers["Retry-After"] = str(retry_after)
                _audit_mutation(engine, request, response) if request.method in {"POST", "PUT", "PATCH", "DELETE"} else None
                return response
            request.state.rate_count = count

        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if idempotency_key and (len(idempotency_key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key)):
            response = _error_response(request, 400, "invalid_idempotency_key", "Invalid Idempotency-Key header.")
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        body = await request.body() if is_api and mutating else b""
        if len(body) > MAX_BODY_BYTES:
            response = _error_response(request, 413, "body_too_large", "Request body exceeds the configured limit.")
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        request_hash = _body_hash(body)
        scope = _idempotency_scope(request, idempotency_key) if idempotency_key and is_api and mutating else ""

        if scope:
            existing = _load_idempotency(engine, scope)
            if existing:
                if existing["request_hash"] != request_hash:
                    response = _error_response(request, 409, "idempotency_key_reused", "Idempotency-Key was already used with a different request body.")
                    _audit_mutation(engine, request, response)
                    return response
                replay = Response(
                    content=existing["response_body"],
                    status_code=existing["status_code"],
                    media_type=existing["content_type"],
                    headers={"X-Request-ID": request.state.request_id, "X-Idempotent-Replay": "true", "Cache-Control": "no-store"},
                )
                replay.headers["X-API-Version"] = API_VERSION
                _audit_mutation(engine, request, replay)
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
        if is_api and mutating:
            _audit_mutation(engine, request, response)
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
