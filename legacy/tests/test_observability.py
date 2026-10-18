import json
import logging

from fastapi.testclient import TestClient

from app.asgi import app


def test_liveness_and_readiness_are_distinct_endpoints():
    with TestClient(app) as client:
        live = client.get("/healthz")
        ready = client.get("/readyz")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_access_logs_are_structured_and_omit_query_strings(caplog):
    caplog.set_level(logging.INFO, logger="atlas.http")
    with TestClient(app) as client:
        response = client.get("/healthz?private-search-term=do-not-log")
    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "atlas.http" and record.message.startswith("{")
    ]
    assert events
    event = events[-1]
    assert event["event"] == "http_request"
    assert event["path"] == "/healthz"
    assert "private-search-term" not in json.dumps(event)
    assert event["status_code"] == 200
