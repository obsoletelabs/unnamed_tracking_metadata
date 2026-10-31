"""HTTP telemetry (#69): in-flight, error classes, bounded route labels, readiness gauges, collector isolation."""

import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from atlas import auth, main, readiness
from atlas.models import Outbox
from atlas.observability import ERROR_CLASSES, MAX_ROUTE_LABELS, RequestTelemetry, Stats

from .conftest import build_settings

ADMIN = "a" * 40


def make_app(tmp_path, **extra):
    settings = build_settings(database_url=f"sqlite:///{tmp_path}/t.db", media_dir=str(tmp_path / "media"),
                              media_cache_enabled=False, metrics_enabled=True, bootstrap_admin_token=ADMIN, **extra)
    return main.create_app(settings)


def scraper(app):
    with app.state.atlas.maker() as session:
        _, token = auth.create_token(session, "scraper", ["metrics"])
        session.commit()
    return {"Authorization": f"Bearer {token}"}


def series(body, name):
    """{label-string: float} for one metric name."""
    out = {}
    for line in body.splitlines():
        match = re.fullmatch(rf"{re.escape(name)}(\{{.*\}})? (\S+)", line)
        if match:
            out[(match.group(1) or "")] = float(match.group(2))
    return out


def by_label(body, name, label):
    return {re.search(rf'{label}="(\w+)"', k).group(1): v for k, v in series(body, name).items()}


# ---- ASGI-level: in-flight and error classes ---------------------------------------------------------------

def run_asgi(app, receive=None):
    stats = Stats()
    middleware = RequestTelemetry(app, SimpleNamespace(ctx=SimpleNamespace(identity=SimpleNamespace(node_id="n"))),
                                  stats, json_logs=False)
    scope = {"type": "http", "method": "GET", "headers": [], "path": "/x"}

    async def send(message):
        pass

    async def default_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def go():
        await middleware(scope, receive or default_receive, send)

    return stats, go


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_success_counts_nothing_as_an_error_and_in_flight_returns_to_zero():
    stats, go = run_asgi(ok_app)
    asyncio.run(go())
    assert stats.in_flight == 0 and sum(stats.errors.values()) == 0
    assert stats.counts[("GET", "unmatched", "200")] == 1


def test_a_cancelled_request_is_a_disconnect_not_a_500_and_in_flight_is_released():
    seen = []

    async def cancelled(scope, receive, send):
        seen.append(stats.in_flight)
        raise asyncio.CancelledError

    stats, go = run_asgi(cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert seen == [1] and stats.in_flight == 0  # decremented even though the task was cancelled
    assert stats.errors["client_disconnect"] == 1 and stats.errors["server_error"] == 0
    assert stats.counts[("GET", "unmatched", "499")] == 1 and ("GET", "unmatched", "500") not in stats.counts


def test_a_client_that_hangs_up_before_the_response_is_a_disconnect():
    async def waits(scope, receive, send):
        while (await receive())["type"] != "http.disconnect":
            pass  # a handler giving up after the peer went away; no response is ever started

    async def receive():
        return {"type": "http.disconnect"}

    stats, go = run_asgi(waits, receive)
    asyncio.run(go())
    assert stats.errors["client_disconnect"] == 1 and stats.counts[("GET", "unmatched", "499")] == 1
    assert stats.in_flight == 0


def test_a_disconnect_after_the_response_completed_is_not_counted():
    async def finishes_then_sees_disconnect(scope, receive, send):
        await ok_app(scope, receive, send)
        await receive()

    async def receive():
        return {"type": "http.disconnect"}

    stats, go = run_asgi(finishes_then_sees_disconnect, receive)
    asyncio.run(go())
    assert sum(stats.errors.values()) == 0 and stats.counts[("GET", "unmatched", "200")] == 1


def test_an_exception_that_escapes_is_an_unhandled_exception_and_a_500():
    async def boom(scope, receive, send):
        raise RuntimeError("secret-exception-message")

    stats, go = run_asgi(boom)
    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert stats.errors["unhandled_exception"] == 1 and stats.counts[("GET", "unmatched", "500")] == 1
    assert stats.in_flight == 0


# ---- real app --------------------------------------------------------------------------------------------

def test_in_flight_is_visible_during_a_request_and_error_classes_are_always_exported(tmp_path):
    app = make_app(tmp_path)
    headers = scraper(app)
    stats = app.state.observability

    @app.get("/probe-in-flight")
    def probe():
        return {"in_flight": stats.in_flight}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/probe-in-flight").json() == {"in_flight": 1}
    client.get("/nothing-here")
    body = client.get("/metrics", headers=headers).text
    assert series(body, "atlas_http_requests_in_flight") == {"": 1.0}  # the scrape itself is in flight
    errors = by_label(body, "atlas_http_request_errors_total", "class")
    assert set(errors) == set(ERROR_CLASSES)  # zero-filled, fixed enum
    assert errors["client_error"] == 1 and errors["server_error"] == 0
    assert stats.in_flight == 0


def test_route_label_cardinality_is_bounded_and_carries_no_request_data(tmp_path):
    app = make_app(tmp_path)
    headers = scraper(app)
    client = TestClient(app, raise_server_exceptions=False)
    for i in range(150):
        client.get(f"/game/user-supplied-id-{i}?token=secret-{i}")
        client.get(f"/api/v2/games/atlas:{i:040d}")
        client.get(f"/no/such/path/{i}/{'x' * 50}")
        client.get(f"/static/missing-{i}.css")
    client.request("BREW", "/game/x")
    body = client.get("/metrics", headers=headers).text
    routes = set(re.findall(r'atlas_http_requests_total\{[^}]*route="([^"]*)"', body))
    assert len(routes) <= 12, routes
    assert 'route="/game/{cid}"' in body and 'route="unmatched"' in body
    for leaked in ("user-supplied-id", "secret-", "no/such/path", "missing-", "x" * 20):
        assert leaked not in body
    assert set(re.findall(r'method="([^"]*)"', body)) <= {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
                                                         "HEAD", "OTHER"}
    statuses = set(re.findall(r'status="([^"]*)"', body))
    assert len(statuses) <= 8 and all(re.fullmatch(r"[1-5]\d\d", s) for s in statuses), statuses


def test_the_route_label_budget_is_enforced_even_if_templates_were_unbounded():
    stats = Stats()
    for i in range(MAX_ROUTE_LABELS + 200):
        stats.observe("GET", f"/dynamic/{i}", 200, 0.001)
    labels = {route for (_, route, _) in stats.counts}
    assert len(labels) == MAX_ROUTE_LABELS + 1 and "other" in labels
    assert sum(stats.counts.values()) == MAX_ROUTE_LABELS + 200  # nothing is dropped, only folded
    stats.observe("GET", "/dynamic/0", 99999, 0.001)  # an out-of-range status cannot create a free-form label
    assert ("GET", "/dynamic/0", "0") in stats.counts


# ---- readiness --------------------------------------------------------------------------------------------

def ready_state(client, headers):
    body = client.get("/metrics", headers=headers).text
    return {"ready": series(body, "atlas_ready")[""], "checks": by_label(body, "atlas_ready_check", "check"),
            "schema": by_label(body, "atlas_schema_state", "state")}


def test_readiness_is_distinct_from_liveness_and_the_metrics_agree_with_it(tmp_path):
    app = make_app(tmp_path)
    headers = scraper(app)
    with TestClient(app) as c:
        assert c.get("/api/v2/health").json()["status"] == "ok"
        ready = c.get("/api/v2/ready")
        assert ready.status_code == 200 and ready.json() == {"status": "ready"}
        assert ready.headers["cache-control"] == "no-store"
        assert ready_state(c, headers) == {"ready": 1.0, "checks": {"database": 1.0, "schema": 1.0, "media": 1.0},
                                           "schema": {"current": 1.0, "behind": 0.0, "unknown": 0.0}}


def test_pending_migrations_make_it_not_ready_and_it_recovers_when_applied(tmp_path):
    app = make_app(tmp_path)
    headers = scraper(app)
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    with TestClient(app) as c:
        with engine.begin() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            conn.execute(text("UPDATE alembic_version SET version_num = '0001'"))
        behind = c.get("/api/v2/ready")
        assert behind.status_code == 503 and behind.json() == {"status": "not_ready"}
        assert behind.headers["cache-control"] == "no-store"
        state = ready_state(c, headers)
        assert state["ready"] == 0.0 and state["checks"]["schema"] == 0.0 and state["schema"]["behind"] == 1.0
        assert state["checks"]["database"] == 1.0  # which dependency failed is visible
        assert c.get("/api/v2/health").status_code == 200  # liveness is unaffected
        detail = c.get("/api/v2/status", headers={"Authorization": f"Bearer {ADMIN}"}).json()["checks"]
        assert detail["schema"] == "behind" and detail["readiness"] == "not_ready"
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": current})
        assert c.get("/api/v2/ready").status_code == 200
        assert ready_state(c, headers)["ready"] == 1.0
    engine.dispose()


def test_unwritable_media_degrades_but_keeps_serving(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the media directory should be")
    settings = build_settings(database_url=f"sqlite:///{tmp_path}/m.db", media_dir=str(blocker / "media"),
                              media_cache_enabled=False, metrics_enabled=True, bootstrap_admin_token=ADMIN)
    app = main.create_app(settings)
    headers = scraper(app)
    with TestClient(app) as c:
        response = c.get("/api/v2/ready")
        assert response.status_code == 200 and response.json() == {"status": "degraded"}
        assert c.get("/api/v2/health").json()["media"] == "unwritable"
        state = ready_state(c, headers)
        assert state["ready"] == 1.0 and state["checks"] == {"database": 1.0, "schema": 1.0, "media": 0.0}
        status = c.get("/api/v2/status", headers={"Authorization": f"Bearer {ADMIN}"}).json()["checks"]
        assert status["media"] == "unwritable" and status["readiness"] == "degraded"
        # the directory path never reaches the public readiness response or the metrics
        assert "blocker" not in response.text and "blocker" not in c.get("/metrics", headers=headers).text


def test_an_unreachable_database_is_not_ready_instead_of_a_500(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        # a real database error from the readiness query (the table does not exist)
        monkeypatch.setattr(readiness, "select", lambda *a: text("SELECT * FROM table_that_does_not_exist"))
        response = c.get("/api/v2/ready")
        assert response.status_code == 503 and response.json() == {"status": "not_ready"}
        assert c.get("/api/v2/health").status_code == 200
        with app.state.atlas.maker() as session:
            result = readiness.check(session, app.state.atlas.ctx.settings)
        assert (result.database, result.schema, result.status) == (False, "unknown", "not_ready")


# ---- collector isolation ----------------------------------------------------------------------------------

def test_one_failing_collector_does_not_fail_the_scrape(tmp_path):
    app = make_app(tmp_path)
    headers = scraper(app)
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    table = Outbox.__tablename__
    with TestClient(app) as c:
        before = c.get("/metrics", headers=headers).text
        assert set(series(before, "atlas_collector_success").values()) == {1.0}
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} RENAME TO outbox_gone"))
        response = c.get("/metrics", headers=headers)
        body = response.text
        assert response.status_code == 200
        success = by_label(body, "atlas_collector_success", "collector")
        assert success.pop("outbox") == 0.0 and set(success.values()) == {1.0}
        assert "atlas_outbox_messages{" not in body  # unavailable, not reported as zero
        assert "atlas_feed_head 0" in body and "atlas_ready 1" in body and "atlas_http_requests_total{" in body
        assert "outbox_gone" not in body and "OperationalError" not in body  # no error text in the output
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE outbox_gone RENAME TO {table}"))
        recovered = c.get("/metrics", headers=headers).text
        assert 'atlas_collector_success{collector="outbox"} 1' in recovered
        assert 'atlas_outbox_messages{state="pending"} 0' in recovered
    engine.dispose()


def test_a_collector_that_raises_something_unexpected_is_isolated_and_timed(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    headers = scraper(app)

    def broken(self, state, session):
        raise ValueError("secret-collector-detail https://user:pw@host/")

    monkeypatch.setattr(Stats, "_backup", broken)
    with TestClient(app) as c:
        body = c.get("/metrics", headers=headers).text
        assert 'atlas_collector_success{collector="backup"} 0' in body and "atlas_backup_enabled" not in body
        assert "secret-collector-detail" not in body
        durations = series(body, "atlas_collector_duration_seconds")
        assert len(durations) == 7 and all(v >= 0 for v in durations.values())


def test_scope_and_opt_in_are_unchanged(tmp_path):
    off = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/o.db", media_dir=str(tmp_path / "m"),
                                         media_cache_enabled=False))
    with TestClient(off) as c:
        assert c.get("/metrics").status_code == 404
        assert c.get("/api/v2/ready").json() == {"status": "ready"}  # readiness works without telemetry
    with TestClient(make_app(tmp_path)) as c:
        assert c.get("/metrics").status_code == 401
