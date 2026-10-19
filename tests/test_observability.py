import json
import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from atlas import auth, main
from atlas.observability import JsonFormatter

from .conftest import build_settings


def app_for(tmp_path, **extra):
    return main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/metrics.db",
                                         media_dir=str(tmp_path / "media"), media_cache_enabled=False, **extra))


def test_observability_is_opt_in(tmp_path):
    client = TestClient(app_for(tmp_path))
    assert client.get("/metrics").status_code == 404
    assert "x-request-id" not in client.get("/api/v2/health").headers


def test_metrics_have_a_dedicated_scope_and_no_request_secrets(tmp_path):
    app = app_for(tmp_path, metrics_enabled=True)
    with app.state.atlas.maker() as session:
        _, metrics = auth.create_token(session, "scraper", ["metrics"])
        _, reader = auth.create_token(session, "reader", ["read"])
        session.commit()
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": f"Bearer {reader}"}).status_code == 403
    client.get("/game/secret-user-value?token=secret-query", headers={"Authorization": "Bearer secret-token"})
    headers = {"Authorization": f"Bearer {metrics}"}
    response = client.get("/metrics", headers=headers)
    assert response.status_code == 200 and "version=0.0.4" in response.headers["content-type"]
    assert 'route="/game/{cid}"' in response.text and "atlas_feed_head 0" in response.text
    assert 'atlas_outbox_messages{state="pending"} 0' in response.text
    assert 'le="+Inf"' in response.text and "atlas_backup_enabled 0" in response.text
    for secret in ("secret-user-value", "secret-query", "secret-token", metrics, reader):
        assert secret not in response.text
    assert client.get("/api/v2/games?q=Alpha", headers=headers).status_code == 403


def test_request_ids_json_logs_and_failures_are_redacted(tmp_path, capsys):
    app = app_for(tmp_path, json_logs=True)

    @app.get("/expected-error")
    def expected():
        raise HTTPException(403, "Forbidden")

    @app.get("/unexpected-error")
    def unexpected():
        raise RuntimeError("secret-exception-message")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/expected-error?token=secret-query", headers={"X-Request-ID": "release-check-1",
                                                                       "Authorization": "Bearer secret-header"})
    assert response.status_code == 403 and response.headers["x-request-id"] == "release-check-1"
    error = client.get("/unexpected-error")
    assert error.status_code == 500 and len(error.headers["x-request-id"]) == 32
    invalid = client.get("/api/v2/health", headers={"X-Request-ID": "bad id " * 100})
    assert len(invalid.headers["x-request-id"]) == 32
    non_ascii = client.get("/api/v2/health", headers=[(b"X-Request-ID", b"\xffdiscarded-prefix")])
    assert len(non_ascii.headers["x-request-id"]) == 32
    output = capsys.readouterr().err
    entries = [json.loads(line) for line in output.splitlines()]
    assert any(e.get("event") == "http.error" and e["error_type"] == "RuntimeError" for e in entries)
    request = next(e for e in entries if e.get("request_id") == "release-check-1")
    assert request["route"] == "/expected-error" and request["node_id"] == app.state.atlas.ctx.identity.node_id
    for secret in ("secret-query", "secret-header", "secret-exception-message"):
        assert secret not in output and secret not in error.text


@pytest.mark.parametrize("secret", ["https://name:password@example.test/?token=private", "Bearer private"])
def test_application_exception_arguments_are_not_serialized(secret):
    record = logging.LogRecord("atlas", logging.ERROR, "module", 1, "worker failed: %s", (RuntimeError(secret),), None)
    payload = JsonFormatter().format(record)
    assert secret not in payload and "RuntimeError" in payload


def test_inactive_exception_logging_cannot_break_redaction():
    # logging.exception outside an except block supplies this tuple. A formatter error
    # would make logging's own diagnostics echo the otherwise-redacted message/arguments.
    record = logging.LogRecord("atlas", logging.ERROR, "module", 1, "secret: %s", ("private-token",),
                               (None, None, None))
    payload = JsonFormatter().format(record)
    assert "private-token" not in payload and "secret" not in payload
