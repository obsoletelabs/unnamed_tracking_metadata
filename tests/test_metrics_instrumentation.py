"""What each instrumented service records (#69-#76): outcomes, not identities, and nothing changes the behaviour."""

from __future__ import annotations

import base64
import json
import smtplib
import threading
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from atlas import (
    auth,
    background,
    backup,
    catalog_metrics,
    db,
    importer,
    instruments,
    mail,
    main,
    media,
    metrics,
    ratelimit,
    service,
    sync,
)
from atlas.metrics import REGISTRY
from atlas.models import OidcCode, Outbox, ProviderCache, now
from atlas.providers import Fetcher, ProviderError, build_registry

from .conftest import batch, build_settings
from .test_oidc import World, exchange, follow_callback, issue
from .test_providers import make_client
from .test_relay import node, publish, pull, transport
from .test_web import fresh, login, signup

ADMIN = "a" * 40


@pytest.fixture(autouse=True)
def recording():
    was = metrics.ENABLED
    metrics.enable()
    REGISTRY.reset()
    yield
    REGISTRY.reset()
    metrics.enable(was)


def series(family) -> dict[tuple[str, ...], object]:
    return {tuple(k): v for k, v in family.snapshot()["series"]}


def count(family, **labels) -> float:
    got = family.value(**labels)
    return got[2] if family.kind == "histogram" else got


def scrape(app) -> str:
    with app.state.atlas.maker() as s:
        _, secret = auth.create_token(s, "scraper", ["metrics"])
        s.commit()
    with TestClient(app) as c:
        r = c.get("/metrics", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200
    return r.text


def make_app(tmp_path, **extra):
    return main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/t.db", media_dir=str(tmp_path / "media"),
                                          media_cache_enabled=False, metrics_enabled=True,
                                          bootstrap_admin_token=ADMIN, **extra))


# ---- providers (#71) ----------------------------------------------------------------------------------------

def test_provider_logical_calls_cache_and_upstream_attempts_are_counted_separately(session):
    calls: list[str] = []
    s = build_settings(provider_ttl_seconds=60)
    f = Fetcher(s, build_registry(s), make_client(calls))
    f.lookup(session, "steam", external_id="620")  # miss, one upstream request
    f.lookup(session, "steam", external_id="620")  # fresh cache hit, no request
    assert count(instruments.provider_calls, provider="steam", operation="fetch", outcome="ok") == 1
    assert count(instruments.provider_calls, provider="steam", operation="fetch", outcome="cache_hit") == 1
    assert count(instruments.provider_cache, provider="steam", result="miss") == 1
    assert count(instruments.provider_cache, provider="steam", result="hit") == 1
    assert count(instruments.provider_requests, provider="steam", outcome="ok") == len(calls) == 1
    assert count(instruments.provider_request_duration, provider="steam", outcome="ok") == 1

    row = session.get(ProviderCache, ("steam", "fetch:620"))
    row.expires_at = row.expires_at - timedelta(days=1)
    session.flush()
    f.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert f.lookup(session, "steam", external_id="620")[0].title == "Portal 2"  # behaviour unchanged: stale served
    assert count(instruments.provider_calls, provider="steam", operation="fetch", outcome="stale_served") == 1
    assert count(instruments.provider_cache, provider="steam", result="stale") == 1
    assert count(instruments.provider_requests, provider="steam", outcome="http_5xx") == 1


def test_provider_timeouts_circuit_and_budget_are_distinguished(session):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    s = build_settings()
    f = Fetcher(s, build_registry(s), httpx.Client(transport=httpx.MockTransport(slow)))
    for i in range(5):
        with pytest.raises(ProviderError):
            f.lookup(session, "steam", external_id=str(100 + i))
    assert count(instruments.provider_requests, provider="steam", outcome="timeout") == 5
    assert count(instruments.provider_backoff, provider="steam", reason="circuit_tripped") == 1
    with pytest.raises(ProviderError, match="circuit open"):
        f.lookup(session, "steam", external_id="999")
    assert count(instruments.provider_calls, provider="steam", operation="fetch", outcome="unavailable") == 1
    assert count(instruments.provider_backoff, provider="steam", reason="circuit_open") == 1
    assert count(instruments.provider_calls, provider="steam", operation="fetch", outcome="error") == 5


def test_quota_gauges_come_only_from_headers_the_provider_sent(session):
    def handler(request):
        if "appdetails" in request.url.path:
            return httpx.Response(200, json={"1": {"success": False}},
                                  headers={"X-RateLimit-Remaining": "41", "X-RateLimit-Reset": "1900000000"})
        return httpx.Response(200, json={"items": []})

    s = build_settings()
    f = Fetcher(s, build_registry(s), httpx.Client(transport=httpx.MockTransport(handler)))
    f.lookup(session, "steam", external_id="1")
    assert instruments.provider_quota_remaining.value(provider="steam") == 41
    assert instruments.provider_quota_reset.value(provider="steam") == 1_900_000_000
    f.lookup(session, "steam", query="portal")  # a response without quota headers changes nothing
    assert instruments.provider_quota_remaining.value(provider="steam") == 41
    assert count(instruments.provider_calls, provider="steam", operation="search", outcome="empty") == 1
    # never invented: a provider that never reported a quota has no series at all (unknown, not zero)
    assert all(k[0] == "steam" for k in series(instruments.provider_quota_remaining))


def test_provider_errors_never_put_a_url_or_query_in_a_label(session):
    s = build_settings()
    f = Fetcher(s, build_registry(s), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with pytest.raises(ProviderError):
        f.lookup(session, "steam", query="secret search terms")
    text_ = json.dumps(REGISTRY.snapshot())
    assert "secret" not in text_ and "steampowered" not in text_


# ---- federation (#70) ---------------------------------------------------------------------------------------

def test_federation_pulls_stages_entries_and_bytes_across_two_authorities_and_a_relay(tmp_path):
    a, b = node(tmp_path, "a", "authority"), node(tmp_path, "b", "authority")
    mirror, local = node(tmp_path, "mirror", "mirror"), node(tmp_path, "local", "local")
    publish(a[2], "Authority Alpha", "alpha_note")
    publish(b[2], "Authority Beta", "beta_note")
    assert pull(mirror[2], a) > 0 and pull(mirror[2], b) > 0
    own_ok = count(instruments.federation_batches, follow="own", outcome="ok")
    assert own_ok >= 2
    received = count(instruments.federation_entries, result="received")
    assert received == count(instruments.federation_entries, result="retained") > 0
    assert count(instruments.federation_entries, result="applied") > 0
    assert count(instruments.federation_bytes, request="info") > 0 and count(instruments.federation_bytes,
                                                                                request="changes") > 0
    for stage in ("fetch", "verify", "apply"):
        assert count(instruments.federation_batch_duration, stage=stage) >= 2

    assert pull(local[2], mirror, a[2].ctx.identity) > 0 and pull(local[2], mirror, b[2].ctx.identity) > 0
    assert count(instruments.federation_batches, follow="relayed", outcome="ok") >= 2
    assert pull(local[2], mirror, a[2].ctx.identity) == 0  # caught up: an empty page is not a failure
    assert count(instruments.federation_batches, follow="relayed", outcome="empty") >= 1
    assert count(instruments.federation_verification, reason="signature") == 0
    peers = {k for k in series(instruments.federation_batches)}
    assert all(set(k) <= {"own", "relayed", *instruments.FEDERATION_OUTCOMES} for k in peers)  # no node ids


def _tampering(client, change):
    def handler(request):
        response = client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers))
        body = response.json()
        if "entries" in body:
            change(body)
        return httpx.Response(response.status_code, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_bad_signatures_replays_pin_mismatch_and_timeouts_are_classified_without_changing_trust(tmp_path):
    a, mirror = node(tmp_path, "a", "authority"), node(tmp_path, "mirror", "mirror")
    publish(a[2], "One", "f1")
    publish(a[2], "Two", "f2")
    ident, token = a[2].ctx.identity, a[3]
    trusted = {ident.node_id: ident.public_b64}

    def forged(body):
        body["entries"][0]["body"] = {**body["entries"][0]["body"], "forged": True}

    with pytest.raises(sync.SyncError, match="bad signature"):
        sync.pull_once(mirror[2].maker, mirror[2].ctx, _tampering(a[1], forged), "http://upstream", token,
                       trusted=trusted)
    assert count(instruments.federation_verification, reason="signature") == 1
    assert count(instruments.federation_batches, follow="own", outcome="verification_failed") == 1

    def replayed(body):  # the same entries sent again from the start: a replay, refused as out of sequence
        body["entries"] = body["entries"][1:] + body["entries"]

    with pytest.raises(sync.SyncError, match="gap or rewind"):
        sync.pull_once(mirror[2].maker, mirror[2].ctx, _tampering(a[1], replayed), "http://upstream", token,
                       trusted=trusted)
    assert count(instruments.federation_verification, reason="sequence") == 1

    other = node(tmp_path, "other", "authority")[2].ctx.identity
    with transport(a[1]) as http, pytest.raises(sync.SyncError, match="does not match"):
        sync.pull_once(mirror[2].maker, mirror[2].ctx, http, "http://upstream", token,
                       trusted={ident.node_id: other.public_b64})  # pinned key is not the authority's
    assert count(instruments.federation_verification, reason="key_mismatch") == 1
    with transport(a[1]) as http, pytest.raises(sync.SyncError, match="not trusted"):
        sync.pull_once(mirror[2].maker, mirror[2].ctx, http, "http://upstream", token, trusted={})
    assert count(instruments.federation_batches, follow="own", outcome="untrusted") == 2

    def offline(request):
        raise httpx.ConnectTimeout("offline", request=request)

    with pytest.raises(httpx.TimeoutException):
        sync.pull_once(mirror[2].maker, mirror[2].ctx, httpx.Client(transport=httpx.MockTransport(offline)),
                       "http://upstream", token, trusted=trusted)
    assert count(instruments.federation_batches, follow="own", outcome="timeout") == 1
    assert count(instruments.federation_entries, result="applied") == 0  # nothing above was applied
    # after every refusal the genuine feed still applies normally: metrics never changed a trust decision
    fetched = pull(mirror[2], a)
    assert fetched > 0 and count(instruments.federation_entries, result="applied") == fetched


# ---- catalogue (#72) ----------------------------------------------------------------------------------------

def parse(lines):
    out = {}
    for line in lines:
        if line.startswith("#"):
            continue
        name_labels, value = line.rsplit(" ", 1)
        out[name_labels] = float(value)
    return out


def test_catalogue_coverage_denominators_add_up_and_not_applicable_fields_are_absent(session, ctx):
    service.ingest(session, ctx, batch("curator", [
        {"external_id": "1", "title": "Dated", "release_date": "2011-04-19", "facts": {"developer": "Valve"}},
        {"external_id": "2", "title": "Undated"},
    ]), curator=True)
    from atlas import custom

    custom.seed(session)
    custom.save_type(session, ctx, "movie", "Movie")
    service.ingest(session, ctx, batch("tmdb", [{"external_id": "3", "title": "A Film"}]), system=True,
                   media_type="movie")
    session.commit()
    got = parse(catalog_metrics.lines(session))
    live = {k: v for k, v in got.items() if k.startswith("atlas_catalog_entities") and 'state="live"' in k}
    for key, total in live.items():
        group = key.split('media_type="')[1].split('"')[0]
        for field in ("release_date", "artwork", "external_ids", "developer", "publisher"):
            have = got[f'atlas_catalog_field_coverage{{media_type="{group}",field="{field}",state="present"}}']
            miss = got[f'atlas_catalog_field_coverage{{media_type="{group}",field="{field}",state="missing"}}']
            assert have + miss == total
    assert got['atlas_catalog_field_coverage{media_type="game",field="release_date",state="present"}'] == 1
    assert not any('field="achievements"' in k and 'media_type="game"' not in k for k in got)
    assert 'atlas_catalog_achievement_definitions{state="active"}' in got
    assert all("Dated" not in k and "Valve" not in k for k in got)  # aggregates only
    assert catalog_metrics.media_group("my_custom_board_games") == "custom"


def test_enrichment_outcomes_are_counted_once_per_import(monkeypatch):
    st = SimpleNamespace()
    monkeypatch.setattr(importer, "_import", lambda *a: SimpleNamespace(review=False, method="none"))
    importer.import_from_provider(st, _session_stub(), "steam", "1")
    monkeypatch.setattr(importer, "_import", lambda *a: SimpleNamespace(review=True, method="title"))
    importer.import_from_provider(st, _session_stub(), "steam", "2")

    def missing(*a):
        raise service.AtlasError("nope", code="not_found")

    monkeypatch.setattr(importer, "_import", missing)
    with pytest.raises(service.AtlasError):
        importer.import_from_provider(st, _session_stub(), "steam", "3")
    assert series(instruments.enrichment) == {("provider_import", "created"): 1, ("provider_import", "review"): 1,
                                             ("provider_import", "not_found"): 1}


def _session_stub():
    return SimpleNamespace(info={})


# ---- media (#73) --------------------------------------------------------------------------------------------

def test_media_fetch_and_store_outcomes(tmp_path):
    settings = build_settings(media_dir=str(tmp_path / "media"))
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, "PNG")
    png = buffer.getvalue()
    data, (ext, *_rest) = media._fetch_measured("artwork", lambda: (png, "origin"), settings)
    media._store_measured("artwork", settings, data, ext)
    with pytest.raises(media.MediaError):
        media._fetch_measured("artwork", lambda: (b"not an image", "peer"), settings)

    def later():
        raise media.MediaError("busy", "fetch_failed", deferred=True)

    with pytest.raises(media.MediaError):
        media._fetch_measured("achievement_icon", later, settings)
    ops = series(instruments.media_operations)
    assert ops[("artwork", "fetch", "ok")] == 1 and ops[("artwork", "store", "ok")] == 1
    assert ops[("achievement_icon", "fetch", "deferred")] == 1
    assert sum(v for (k, o, outcome), v in ops.items() if outcome not in ("ok", "deferred")) == 1
    assert count(instruments.media_bytes, source="origin") == len(png)
    assert count(instruments.media_bytes, source="peer") == len(b"not an image")

    broken = build_settings(media_dir=str(tmp_path / "file-not-dir"))
    (tmp_path / "file-not-dir").write_text("x")
    with pytest.raises(Exception):  # noqa: B017 - whichever storage error the platform raises
        media._store_measured("artwork", broken, data, ext)
    assert series(instruments.media_operations)[("artwork", "store", "storage_error")] == 1


def test_storage_usage_is_unknown_without_a_directory_and_bounded_with_one(tmp_path):
    missing = media.storage_usage(build_settings(media_dir=str(tmp_path / "absent")))
    assert missing["present"] is False
    root = tmp_path / "m"
    (root / "ab").mkdir(parents=True)
    for i in range(5):
        (root / "ab" / f"{i}.png").write_bytes(b"x" * 10)
    (root / "ab" / "partial.png.part").write_bytes(b"y" * 99)
    got = media.storage_usage(build_settings(media_dir=str(root)))
    assert got == {"bytes": 50, "files": 5, "truncated": False, "present": True}
    assert media.storage_usage(build_settings(media_dir=str(root)), limit=2)["truncated"] is True


def test_media_requests_count_refusals_and_misses_without_ids(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/media/" + "0" * 64).status_code == 404
        with app.state.atlas.maker() as s:
            _, read = auth.create_token(s, "reader", ["read"])
            s.commit()
        r = c.get("/api/v2/artwork/nope/image", headers={"Authorization": f"Bearer {read}"})
        assert r.status_code == 404
    got = series(instruments.media_cache)
    assert got[("media", "miss")] == 1 and got[("image", "miss")] == 1  # an unreferenced digest: servable, absent
    assert "0" * 64 not in json.dumps(REGISTRY.snapshot())


# ---- database and search (#74) ------------------------------------------------------------------------------

def test_statements_transactions_and_errors_are_classified_without_sql(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/d.db")
    db.instrument(engine)
    db.instrument(engine)  # idempotent: listeners are not attached twice
    with engine.connect() as c:
        c.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, secret_column TEXT)"))
        c.execute(text("INSERT INTO t VALUES (1, 'x')"))
        c.commit()
        c.execute(text("SELECT secret_column FROM t WHERE id = 1")).all()
        with pytest.raises(Exception):  # noqa: B017
            c.execute(text("INSERT INTO t VALUES (1, 'dup')"))
        c.rollback()
    assert count(instruments.db_query, operation="select", outcome="ok") == 1
    assert count(instruments.db_query, operation="insert", outcome="ok") == 1
    assert count(instruments.db_query, operation="insert", outcome="error") == 1
    assert count(instruments.db_query, operation="other", outcome="ok") == 1  # CREATE
    assert count(instruments.db_errors, **{"class": "integrity"}) == 1
    assert count(instruments.db_transactions, outcome="commit") >= 1
    assert count(instruments.db_transactions, outcome="rollback") >= 1
    assert "secret_column" not in json.dumps(REGISTRY.snapshot())


@pytest.mark.parametrize(("orig", "expected"), [
    (SimpleNamespace(sqlstate="40P01"), "deadlock"), (SimpleNamespace(sqlstate="40001"), "serialization"),
    (SimpleNamespace(pgcode="55P03"), "lock_timeout"), (Exception("database is locked"), "lock_timeout"),
    (Exception("something else"), "other")])
def test_database_error_classes(orig, expected):
    assert db.error_class(SimpleNamespace(orig=orig)) == expected


def test_pool_status_is_absent_for_pools_without_numbers(tmp_path):
    from sqlalchemy.pool import QueuePool, StaticPool

    assert db.pool_status(create_engine("sqlite://", poolclass=StaticPool)) is None
    pooled = create_engine(f"sqlite:///{tmp_path}/p.db", poolclass=QueuePool, pool_size=3)
    with pooled.connect():
        got = db.pool_status(pooled)
    assert got == {"checked_out": 1, "idle": 0, "overflow": 0, "size": 3}


def test_search_is_timed_by_mode_and_counts_results_never_the_query(session, ctx):
    service.ingest(session, ctx, batch("curator", [{"external_id": "1", "title": "Hades",
                                                    "release_date": "2020-09-17"}]), curator=True)
    session.commit()
    assert len(service.search(session, "hades")) == 1
    assert service.search(session, "zzz nothing") == []
    service.search(session, "", released_from="2020")
    service.search(session, "")
    with pytest.raises(service.AtlasError):
        service.search(session, "", sort="bogus")
    assert count(instruments.search_duration, mode="title", outcome="ok") == 2
    assert count(instruments.search_duration, mode="filter", outcome="ok") == 1
    assert count(instruments.search_duration, mode="browse", outcome="ok") == 1
    assert count(instruments.search_duration, mode="browse", outcome="invalid") == 1
    zero_bucket = instruments.search_results.value(mode="title")[0][0]
    assert zero_bucket == 1  # le="0": one zero-result search
    assert "hades" not in json.dumps(REGISTRY.snapshot()).lower()


# ---- background work, mail and recovery (#75) ---------------------------------------------------------------

def test_background_operations_count_ok_error_skip_and_wake_delay():
    with background.operation("backup"):
        pass
    with pytest.raises(RuntimeError), background.operation("backup"):
        raise RuntimeError("boom")
    background.skipped("media_cache")
    stop = threading.Event()
    assert background.wait(stop, "sync", 0) is False
    stop.set()
    assert background.wait(stop, "sync", 5) is True  # stopping is not a late wake-up
    assert series(instruments.background_ops) == {("backup", "ok"): 1, ("backup", "error"): 1,
                                                 ("media_cache", "skipped"): 1}
    assert count(instruments.background_delay, loop="sync") == 1


@pytest.mark.parametrize(("exc", "expected"), [
    (smtplib.SMTPAuthenticationError(535, b"no"), "auth"), (smtplib.SMTPResponseException(451, b"later"), "transient"),
    (smtplib.SMTPResponseException(550, b"no such user"), "permanent"),
    (smtplib.SMTPSenderRefused(553, b"no", "a@b"), "permanent"), (smtplib.SMTPRecipientsRefused({}), "permanent"),
    (smtplib.SMTPServerDisconnected("gone"), "connection"), (smtplib.SMTPException("odd"), "protocol"),
    (ConnectionRefusedError(), "connection"), (ValueError("bad header"), "invalid")])
def test_mail_failure_classes(exc, expected):
    assert mail.failure_class(exc) == expected


def test_mail_attempts_and_backlog_age_without_sending_anything(tmp_path, monkeypatch):
    app = make_app(tmp_path, mail_backend="smtp", smtp_host="smtp.invalid")
    st = app.state.atlas
    assert "atlas_mail_oldest_pending_timestamp_seconds" not in scrape(app)  # empty queue: absent, not zero
    with st.maker() as s:
        mail.queue(s, "person@example.com", "Hi", "body")
        s.commit()
    assert "atlas_mail_oldest_pending_timestamp_seconds " in scrape(app)
    sent: list[str] = []

    def refuse(settings, row):
        raise smtplib.SMTPResponseException(451, b"try later")

    monkeypatch.setattr(mail, "_send_smtp", refuse)
    mail.deliver_pending(st.maker, st.ctx.settings)
    with st.maker() as s:
        s.execute(Outbox.__table__.update().values(next_attempt_at=None))
        s.commit()
    monkeypatch.setattr(mail, "_send_smtp", lambda settings, row: sent.append(row.id))
    mail.deliver_pending(st.maker, st.ctx.settings)
    assert len(sent) == 1
    assert series(instruments.mail_deliveries) == {("failed", "transient"): 1, ("sent", "none"): 1}
    assert "person@example.com" not in json.dumps(REGISTRY.snapshot())
    assert "atlas_mail_oldest_pending_timestamp_seconds " not in scrape(app)


def test_archive_verification_and_restore_drill_are_separate_evidence(tmp_path):
    app = make_app(tmp_path, backup_dir=str(tmp_path / "backups"))
    st = app.state.atlas
    with st.maker() as s:
        service.ingest(s, st.ctx, batch("curator", [{"external_id": "c1", "title": "Kept"}]), curator=True)
        s.commit()
    engine = st.maker.kw["bind"]
    body = scrape(app)
    assert "atlas_backup_last_verified_timestamp_seconds 0" in body
    assert "atlas_backup_last_restore_drill_timestamp_seconds 0" in body
    archive = tmp_path / "backups" / "a.tar.gz"
    archive.parent.mkdir()
    backup.create(st.ctx.settings, engine, archive)
    assert "atlas_backup_last_verified_timestamp_seconds 0" in scrape(app)  # an archive alone proves nothing

    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(archive.read_bytes()[:200])
    with pytest.raises(backup.BackupError):
        backup.drill(st.ctx.settings, broken, record_to=engine)
    assert "atlas_backup_last_restore_drill_timestamp_seconds 0" in scrape(app)

    with engine.connect() as c:
        before = c.execute(text("SELECT count(*) FROM canonical")).scalar()
    got = backup.drill(st.ctx.settings, archive, record_to=engine)
    assert got["rows"] > 0
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM canonical")).scalar() == before  # the live database is untouched
    body = scrape(app)
    assert "atlas_backup_last_restore_drill_timestamp_seconds 0" not in body
    assert "atlas_backup_last_verified_timestamp_seconds 0" not in body
    assert str(tmp_path) not in body and "a.tar.gz" not in body


# ---- identity, consent, tokens and authorization (#76) ------------------------------------------------------

def test_token_endpoint_outcomes_are_terminal_and_bounded(tmp_path):
    with World(tmp_path) as w:
        cid, secret, code = issue(w)
        assert exchange(w, cid, secret, code, "https://app.test/cb", "wrong" * 10).status_code == 400
        assert exchange(w, cid, secret, code, "https://app.test/cb", "v" * 50).status_code == 400  # reused
        cid, secret, code = issue(w)
        assert exchange(w, cid, "bad secret", code, "https://app.test/cb", "v" * 50).status_code == 401
        assert exchange(w, cid, secret, code, "https://app.test/other", "v" * 50).status_code == 400
        cid, secret, code = issue(w)
        with w.p_app.state.atlas.maker() as s:
            for row in s.scalars(select(OidcCode)):
                row.expires_at = now() - timedelta(seconds=1)
            s.commit()
        assert exchange(w, cid, secret, code, "https://app.test/cb", "v" * 50).status_code == 400
        cid, secret, code = issue(w)
        ok = exchange(w, cid, secret, code, "https://app.test/cb", "v" * 50)
        assert ok.status_code == 200
        access = ok.json()["access_token"]
        assert w.provider.get("/oidc/userinfo", headers={"Authorization": "Bearer " + access}).status_code == 200
        assert w.provider.get("/oidc/userinfo", headers={"Authorization": "Bearer nope"}).status_code == 401
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        assert w.provider.post("/oidc/token", data={"grant_type": "password"},
                               headers={"Authorization": "Basic " + basic}).status_code == 400
    got = series(instruments.token_operations)
    exchanges = {k[2]: v for k, v in got.items() if k[:2] == ("authorization_code", "exchange")}
    assert exchanges == {"pkce_failed": 1, "reused": 1, "client_auth_failed": 1, "redirect_mismatch": 1,
                         "expired": 1, "ok": 1, "unsupported_grant": 1}
    assert got[("access_token", "issue", "ok")] == 1
    assert got[("access_token", "validate", "ok")] == 1 and got[("access_token", "validate", "invalid")] == 1
    snapshot = json.dumps(REGISTRY.snapshot())
    assert cid not in snapshot and access not in snapshot and "app.test" not in snapshot


def test_consent_and_sign_in_flow_outcomes(tmp_path):
    with World(tmp_path) as w:
        iid, _, _ = w.add_issuer(auto_create="on")
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        loc, _ = w.sign_in_via(browser, iid, decision="deny")
        assert follow_callback(browser, loc).status_code == 400  # the provider says access_denied
        browser = fresh(type("S", (), {"st_app": w.n_app}))
        loc, _ = w.sign_in_via(browser, iid)
        assert follow_callback(browser, loc).status_code == 303
        assert follow_callback(fresh(type("S", (), {"st_app": w.n_app})), loc).status_code == 400  # replayed state
        assert w.provider.get("/oidc/authorize?client_id=unknown&redirect_uri=x&response_type=code"
                              "&scope=openid").status_code == 400
    consent = series(instruments.oidc_consent)
    assert consent == {("denied",): 1, ("allowed",): 1, ("invalid_request",): 1}
    flows = series(instruments.identity_flows)
    assert flows[("oidc_signin", "provider_cancelled")] == 1
    assert flows[("oidc_signin", "ok")] == 1
    assert flows[("oidc_signin", "state_invalid")] == 1
    assert count(instruments.oidc_stage, stage="callback", outcome="ok") == 1
    assert count(instruments.oidc_stage, stage="authorize", outcome="error") == 1


def test_password_sign_in_and_api_authorization_decisions(tmp_path):
    app = make_app(tmp_path, public_url="http://localhost:8000")
    site = SimpleNamespace(st_app=app)
    with fresh(site) as browser:
        signup(browser, "erin")
    with fresh(site) as browser:
        assert login(browser, "erin", "wrong password").status_code == 400
        assert login(browser, "erin").status_code == 303
    flows = series(instruments.identity_flows)
    assert flows[("password", "bad_credentials")] == 1 and flows[("password", "ok")] == 1
    with app.state.atlas.maker() as s:
        _, read = auth.create_token(s, "reader", ["read"])
        _, scraper = auth.create_token(s, "scraper", ["metrics"])
        s.commit()
    with TestClient(app) as c:
        assert c.get("/api/v2/games?q=x").status_code == 401
        assert c.get("/api/v2/games?q=x", headers={"Authorization": f"Bearer {scraper}"}).status_code == 403
        assert c.get("/api/v2/games?q=x", headers={"Authorization": f"Bearer {read}"}).status_code == 200
    got = series(instruments.authorization)
    assert got[("read", "denied", "missing_token")] == 1
    assert got[("read", "denied", "insufficient_scope")] == 1
    assert got[("read", "allowed", "ok")] == 1
    labels = [k for fam in REGISTRY.snapshot().values() for k, _ in fam["series"]]
    assert labels and not any("erin" in value for key in labels for value in key)


@pytest.mark.parametrize(("key", "operation"), [
    ("t:abc", "api"), ("ip:203.0.113.9", "api"), ("login:pair:203.0.113.9:erin", "login"),
    ("forgot:erin@example.com", "password_reset"), ("oidc:cb:203.0.113.9", "oidc_callback"),
    ("oidc:token:atlas_x", "oidc_token"), ("group-enrol:1", "group"), ("whatever:else", "account")])
def test_rate_limit_keys_map_to_fixed_operations(key, operation):
    assert instruments.rate_limit_operation(key) == operation


def test_rate_limiter_decisions_are_counted_by_operation(maker):
    for _ in range(3):
        ratelimit.hit(maker, "login:ip:203.0.113.9", 2, 60)
    assert series(instruments.auth_rate_limit) == {("login", "allowed"): 2, ("login", "limited"): 1}


# ---- the scrape (#69) ---------------------------------------------------------------------------------------

def test_scrape_exports_new_families_and_caches_the_expensive_collectors(tmp_path):
    app = make_app(tmp_path, metrics_cache_seconds=3600)
    first = scrape(app)
    for name in ("atlas_http_response_bytes_total", "atlas_catalog_entities", "atlas_media_storage_present",
                 "atlas_collector_refresh_duration_seconds_bucket", 'atlas_collector_cache_age_seconds{collector="catalog"}',
                 "atlas_database_transactions_total"):
        assert name in first, name
    refreshed = count(instruments.collector_refresh, collector="catalog", outcome="ok")
    scrape(app)
    assert count(instruments.collector_refresh, collector="catalog", outcome="ok") == refreshed  # served from cache


def test_a_failing_cached_collector_is_isolated(tmp_path, monkeypatch):
    def broken(session):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(catalog_metrics, "lines", broken)
    body = scrape(make_app(tmp_path))
    assert 'atlas_collector_success{collector="catalog"} 0' in body
    assert 'atlas_collector_success{collector="readiness"} 1' in body and "atlas_ready " in body
    assert "atlas_catalog_entities" not in body and "secret detail" not in body
    assert count(instruments.collector_refresh, collector="catalog", outcome="error") == 1


def test_multi_worker_scrape_sums_another_workers_counters(tmp_path):
    shared = tmp_path / "metrics"
    app = make_app(tmp_path, metrics_dir=str(shared))
    other = {"atlas_enrichment_operations_total": {
        "type": "counter", "help": "h", "labels": ["operation", "outcome"], "buckets": [], "merge": "sum", "live": False,
        "series": [[["provider_import", "created"], 40]]}}
    metrics.write_snapshot(shared, other, token="other-worker")
    instruments.enrichment.inc(2, operation="provider_import", outcome="created")
    body = scrape(app)
    assert 'atlas_enrichment_operations_total{operation="provider_import",outcome="created"} 42' in body
    assert "atlas_metrics_workers 2" in body
