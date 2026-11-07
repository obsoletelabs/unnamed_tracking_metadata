"""Price observations (CONTRACTS-v1 C8, issue #38): the model, the API, retention and the price channel that is
designed (protocol 1.5) but disabled in this release.

Covers integer money with ISO 4217 exponents (JPY 0, KWD 3, USD 2, CLF 4) and the rejection of floats, currency
and region validation, duplicate and conflicting observations, the latest-per-offer query, regional history
order and paging, retention and purge, merges, signature domain separation, and the proof that nothing
price-related reaches the signed feed or `/sync/info` while followers of this and earlier releases (1.4 before
this change, 1.3, 1.1) are unaffected and park a stray `price` entry instead of dropping it. Every scenario
asserts the rows in the database, not only the answers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from atlas import auth, cli, feed, feed_schema, money, prices, service, signing, sync
from atlas.models import Canonical, ChangeLog, PriceObservation

from .conftest import batch
from .test_sync_compat import Node, pull

# C11 (A17) reserved the merge-undo kinds for protocol 1.6.
RESERVED_C11 = {"price": "1.6", "unmerge": "1.7", "distinct": "1.7"}  # after the H52 shift (C13)

NOW = datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def price(cid: str, **kw) -> dict:
    """A valid observation: USD 9.99 down from 19.99 in the US, one hour ago."""
    base = {"canonical_id": cid, "provider": "steam", "product_id": "620", "region": "us", "currency": "USD",
            "amount_minor": 999, "base_amount_minor": 1999, "availability": "available",
            "observed_at": iso(NOW - timedelta(hours=1)), "source_kind": "provider",
            "provenance": {"method": "api", "fetch_id": "run-1"},
            "license": {"redistributable": True, "terms": "test-terms"}}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not ...}


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path, "authority", "authority", emit_protocol=feed_schema.PROTOCOL)
    with n.st.maker() as s:
        _, n.curate = auth.create_token(s, "curator", ["read", "curate"])
        _, n.admin = auth.create_token(s, "admin", ["admin"])
        s.commit()
    return n


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def post(node, *items, token=None):
    return node.client.post("/api/v2/prices", json={"prices": list(items)}, headers=h(token or node.curate))


def db_rows(node, **where):
    with node.st.maker() as s:
        q = select(PriceObservation)
        for k, v in where.items():
            q = q.where(getattr(PriceObservation, k) == v)
        return [prices.channel_body(r) | {"purge_after": prices.utc(r.purge_after)} for r in
                s.scalars(q.order_by(PriceObservation.observed_at, PriceObservation.id))]


def count(node) -> int:
    with node.st.maker() as s:
        return s.scalar(select(func.count(PriceObservation.id)))


# ---- registry: the 1.6 channel is designed, registered and off --------------------------------------------

def test_price_channel_is_registered_reserved_and_disabled():
    channel = feed_schema.PRICE_CHANNEL
    assert (channel.since, channel.capability, channel.endpoint) == ("1.6", "price-channel-v1",
                                                                     "/api/v2/sync/price-changes")
    assert channel.domain == signing.PRICE_DOMAIN == b"atlas-price-v1\n" and channel.domain != signing.DOMAIN
    assert (channel.sequence, channel.cursor_suffix) == ("price_feed_seq", "#price")
    assert feed_schema.CHANNEL_ENABLED is False
    assert feed_schema.PROTOCOL == "1.5"  # 1.6 ships with the release that enables the channel (1.5 is C13's record)
    assert feed_schema.RESERVED_KINDS == RESERVED_C11 and "price" not in feed_schema.FEED_KINDS
    assert feed_schema.RESERVED_TOKENS["price-channel-v1"] == "1.6"
    assert "price-channel-v1" not in feed_schema.capabilities()
    assert "price-channel-v1" not in feed_schema.known_tokens()
    vocabularies = feed_schema.vocabularies()
    assert vocabularies["price_availability"] == {"available", "unavailable", "preorder", "free", "unreleased",
                                                  "delisted", "unknown"}
    assert vocabularies["price_source_kind"] == {"provider", "manual"}
    assert "| `price` | 1.6 (reserved) | not in this release | parked by this release |" in feed_schema.markdown_table()


def test_the_channel_body_is_exactly_the_registered_c8_shape(node):
    cid = node.publish("Shape Game")
    assert post(node, price(cid)).status_code == 200
    with node.st.maker() as s:
        row = s.scalars(select(PriceObservation)).one()
        body = prices.channel_body(row)
    assert set(body) == feed_schema.PRICE_CHANNEL.fields
    assert set(body["provenance"]) <= feed_schema.PRICE_CHANNEL.nested["provenance"]
    assert set(body["license"]) == feed_schema.PRICE_CHANNEL.nested["license"]
    assert body["availability"] in feed_schema.vocabularies()["price_availability"]


@pytest.mark.parametrize("protocol", feed_schema.protocol_choices())
def test_no_emit_protocol_signs_a_price_into_the_main_feed(protocol):
    with pytest.raises(feed_schema.EmitGateError) as err:
        feed_schema.check_emit("price", {"canonical_id": "atlas:x", "amount_minor": 1}, protocol)
    assert err.value.needed == "1.6"


# ---- money ------------------------------------------------------------------------------------------------

def test_iso_4217_exponents_and_exact_decimals():
    assert {c: money.exponent(c) for c in ("USD", "EUR", "JPY", "KRW", "KWD", "BHD", "TND", "CLF")} == {
        "USD": 2, "EUR": 2, "JPY": 0, "KRW": 0, "KWD": 3, "BHD": 3, "TND": 3, "CLF": 4}
    assert money.decimal(999, "USD") == "9.99" and money.decimal(1980, "JPY") == "1980"
    assert money.decimal(1500, "KWD") == "1.500" and money.decimal(5, "KWD") == "0.005"
    assert money.decimal(0, "JPY") == "0" and money.decimal(12345, "CLF") == "1.2345"
    assert money.decimal(money.MAX_MINOR, "USD") == "10000000000000.00"  # exact, no float rounding
    assert money.decimal(None, "USD") is None
    for code in ("XAU", "XDR", "XTS", "XXX", "HRK", "usd", "US", "", "EURO"):
        assert not money.valid_currency(code), code
    assert len(money.REGIONS) == 249 and {"us", "jp", "kw", "gb"} <= money.REGIONS
    for region in ("ww", "uk", "eu", "US", "usa", "", "xk"):
        assert not money.valid_region(region), region
    assert money.discount_pct(999, 1999) == 50 and money.discount_pct(1339, 1999) == 33
    assert money.discount_pct(0, 1999) == 100 and money.discount_pct(1999, 1999) == 0
    assert money.discount_pct(0, 0) == 0


def valid(**kw):
    return prices.PriceIn.model_validate(price("atlas:x", **kw))


@pytest.mark.parametrize(("change", "message"), [
    ({"amount_minor": 999.0}, "integer"),  # a float, even a whole one
    ({"amount_minor": 9.99}, "integer"),
    ({"amount_minor": "999"}, "integer"),
    ({"amount_minor": True}, "integer"),
    ({"base_amount_minor": 1999.0}, "integer"),
    ({"amount_minor": -1}, "greater than or equal"),
    ({"amount_minor": money.MAX_MINOR + 1, "base_amount_minor": ...}, "less than or equal"),
    ({"discount_pct": 50.0}, "integer"),
    ({"retention_days": 30.0}, "integer"),
    ({"currency": "usd"}, "ISO 4217"),
    ({"currency": "XAU"}, "ISO 4217"),
    ({"currency": "HRK"}, "ISO 4217"),
    ({"region": "US"}, "ISO 3166-1"),
    ({"region": "uk"}, "ISO 3166-1"),
    ({"region": "ww"}, "always regional"),
    ({"region": "usa"}, "at most 2"),
    ({"amount_minor": ...}, "amount_minor is required"),
    ({"availability": "preorder", "amount_minor": ...}, "amount_minor is required"),
    ({"availability": "free", "amount_minor": 1}, "free offer"),
    ({"amount_minor": 2000}, "cannot be above"),
    ({"discount_pct": 40}, "does not match"),
    ({"base_amount_minor": ..., "discount_pct": 50}, "needs both"),
    ({"availability": "on_sale"}, "availability"),
    ({"source_kind": "scraper"}, "source_kind"),
    ({"observed_at": "2026-10-31T12:00:00"}, "timezone"),
    ({"valid_until": iso(NOW - timedelta(hours=2))}, "valid_until must be after"),
    ({"retention_days": 0}, "greater than or equal"),
    ({"retention_days": 366}, "less than or equal"),
    ({"provider": "Steam Store"}, "pattern"),
    ({"product_id": "has space"}, "pattern"),
    ({"license": {"redistributable": "yes"}}, "boolean"),
    ({"provenance": {"node": "n", "token": "secret"}}, "Extra inputs"),
    ({"id": "01JD0000000000000000000000"}, "Extra inputs"),
    ({"price": 9.99}, "Extra inputs"),
])
def test_invalid_observations_are_refused(change, message):
    with pytest.raises(ValidationError, match=message):
        valid(**change)


def test_valid_edge_cases_and_derived_fields():
    assert valid(discount_pct=49).discount_pct == 49  # within one point of the derived 50
    assert valid(discount_pct=51).discount_pct == 51
    assert valid().discount_pct == 50  # derived when not sent
    assert valid(base_amount_minor=..., amount_minor=999).discount_pct is None
    free = valid(availability="free", amount_minor=..., base_amount_minor=...)
    assert free.amount_minor == 0
    assert valid(availability="delisted", amount_minor=..., base_amount_minor=...).amount_minor is None
    assert valid(currency="JPY", amount_minor=1980, base_amount_minor=1980).discount_pct == 0
    assert valid(amount_minor=money.MAX_MINOR, base_amount_minor=money.MAX_MINOR).amount_minor == money.MAX_MINOR
    assert valid(observed_at="2026-10-31T21:00:00+09:00").observed_at.utcoffset() == timedelta(hours=9)


# ---- API: storing and reading, JPY and KWD ------------------------------------------------------------------

def test_jpy_and_kwd_round_trip_as_integers(node):
    cid = node.publish("Yen Game")
    jpy = price(cid, region="jp", currency="JPY", amount_minor=1980, base_amount_minor=3960, product_id="9")
    kwd = price(cid, region="kw", currency="KWD", amount_minor=1500, base_amount_minor=1500, product_id="9")
    resp = post(node, jpy, kwd)
    assert resp.status_code == 200, resp.text
    assert [r["status"] for r in resp.json()["results"]] == ["stored", "stored"]
    assert resp.json()["counts"] == {"stored": 2}
    stored = {r["currency"]: r for r in db_rows(node)}
    assert (stored["JPY"]["amount_minor"], stored["JPY"]["base_amount_minor"], stored["JPY"]["discount_pct"]) == (
        1980, 3960, 50)
    assert (stored["KWD"]["amount_minor"], stored["KWD"]["discount_pct"]) == (1500, 0)
    assert all(type(r["amount_minor"]) is int for r in stored.values())
    with node.st.maker() as s:  # the database column holds an integer, not a decimal or a float
        raw = s.execute(select(PriceObservation.amount_minor, PriceObservation.currency)).all()
    assert sorted(raw, key=lambda r: r[1]) == [(1980, "JPY"), (1500, "KWD")]

    got = node.client.get(f"/api/v2/games/{cid}/prices", headers=h(node.token)).json()
    by = {r["currency"]: r for r in got["results"]}
    assert (by["JPY"]["amount"], by["JPY"]["base_amount"], by["JPY"]["exponent"], by["JPY"]["sale"]) == (
        "1980", "3960", 0, True)
    assert (by["KWD"]["amount"], by["KWD"]["exponent"], by["KWD"]["sale"]) == ("1.500", 3, False)
    assert by["KWD"]["amount_minor"] == 1500 and isinstance(by["KWD"]["amount_minor"], int)


def test_a_float_anywhere_rejects_the_whole_batch(node):
    cid = node.publish("Float Game")
    body = json.dumps({"prices": [price(cid), price(cid, product_id="2", amount_minor=999.0)]})
    resp = node.client.post("/api/v2/prices", content=body, headers={**h(node.curate),
                                                                      "content-type": "application/json"})
    assert resp.status_code == 422 and "integer" in resp.text
    assert count(node) == 0  # not even the valid first item


def test_scopes_and_batch_bounds(node):
    cid = node.publish("Scope Game")
    assert post(node, price(cid), token=node.token).status_code == 403  # read/sync token cannot write
    resp = post(node, *[price(cid, product_id=str(i)) for i in range(prices.MAX_BATCH + 1)])
    assert resp.status_code == 422 and count(node) == 0
    resp = post(node, *[price(cid, product_id=str(i)) for i in range(prices.MAX_BATCH)])
    assert resp.status_code == 200 and resp.json()["counts"] == {"stored": prices.MAX_BATCH}
    assert count(node) == prices.MAX_BATCH
    assert node.client.get(f"/api/v2/games/{cid}/prices/history?region=us&limit=1001",
                           headers=h(node.token)).status_code == 422
    assert node.client.get("/api/v2/prices/status", headers=h(node.curate)).status_code == 403
    assert node.client.get(f"/api/v2/games/{cid}/prices?region=ww", headers=h(node.token)).status_code == 422
    assert node.client.get(f"/api/v2/games/{cid}/prices?currency=XAU", headers=h(node.token)).status_code == 422


# ---- duplicates ---------------------------------------------------------------------------------------------

def test_duplicate_and_conflicting_observations(node):
    cid = node.publish("Dup Game")
    first = post(node, price(cid), price(cid, region="gb", currency="GBP", amount_minor=799, base_amount_minor=1599))
    assert [r["status"] for r in first.json()["results"]] == ["stored", "stored"]
    before = db_rows(node)

    again = post(node, price(cid, provenance={"method": "api", "fetch_id": "run-2"}),  # another fetch, same answer
                 price(cid, region="gb", currency="GBP", amount_minor=799, base_amount_minor=1599))
    assert [r["status"] for r in again.json()["results"]] == ["duplicate", "duplicate"]
    assert [r["id"] for r in again.json()["results"]] == [r["id"] for r in first.json()["results"]]
    assert db_rows(node) == before  # nothing changed, provenance included

    same_moment = iso(NOW - timedelta(hours=1))
    conflict = post(node, price(cid, amount_minor=499, observed_at=same_moment))
    assert conflict.json()["results"][0]["status"] == "conflict"
    assert db_rows(node) == before  # the stored observation is kept

    # The same instant written in another time zone is the same observation.
    tokyo = (NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=9))).isoformat()
    assert post(node, price(cid, observed_at=tokyo)).json()["results"][0]["status"] == "duplicate"

    # Within one batch: the second copy is a duplicate of the first, a different amount a conflict.
    later = iso(NOW - timedelta(minutes=10))
    batch = post(node, price(cid, observed_at=later), price(cid, observed_at=later),
                 price(cid, observed_at=later, amount_minor=1999))
    assert [r["status"] for r in batch.json()["results"]] == ["stored", "duplicate", "conflict"]
    assert len(db_rows(node)) == len(before) + 1
    assert len({r["id"] for r in batch.json()["results"]}) == 1


def test_per_item_statuses_for_unknown_expired_and_future(node):
    cid = node.publish("Status Game")
    resp = post(node, price("atlas:does-not-exist"),
                price(cid, observed_at=iso(NOW - timedelta(days=31))),  # past the default 30 days
                price(cid, observed_at=iso(NOW - timedelta(days=31)), retention_days=60, product_id="old"),
                price(cid, observed_at=iso(NOW + timedelta(hours=1)), product_id="future"))
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["unknown_record", "expired", "stored", "invalid"]
    assert [r["product_id"] for r in db_rows(node)] == ["old"]
    assert db_rows(node)[0]["retention_days"] == 60


# ---- latest per offer ---------------------------------------------------------------------------------------

def test_latest_per_offer_region_and_currency(node):
    cid = node.publish("Latest Game")
    t = [iso(NOW - timedelta(hours=n)) for n in range(6)]
    post(node,
         price(cid, observed_at=t[5], amount_minor=1999),
         price(cid, observed_at=t[1], amount_minor=999),  # newest US/USD for the base offer
         price(cid, observed_at=t[3], amount_minor=1499),
         price(cid, observed_at=t[2], edition="Deluxe", product_id="620", package_id="sub-2", amount_minor=2999,
               base_amount_minor=2999),
         price(cid, observed_at=t[4], region="de", currency="EUR", amount_minor=899, base_amount_minor=1799),
         price(cid, observed_at=t[1], region="de", currency="EUR", amount_minor=1799, base_amount_minor=1799),
         price(cid, observed_at=t[2], provider="gog", product_id="g1", region="us", amount_minor=1299,
               base_amount_minor=1999),
         price(cid, observed_at=t[0], region="jp", currency="JPY", amount_minor=1980, base_amount_minor=1980,
               license={"redistributable": False, "terms": "steam-web-api"}))
    assert count(node) == 8

    curated = node.client.get(f"/api/v2/games/{cid}/prices", headers=h(node.curate)).json()
    got = [(r["provider"], r["region"], r["currency"], r["edition"], r["amount_minor"]) for r in curated["results"]]
    assert got == [("gog", "us", "USD", None, 1299),
                   ("steam", "de", "EUR", None, 1799),
                   ("steam", "jp", "JPY", None, 1980),
                   ("steam", "us", "USD", None, 999),
                   ("steam", "us", "USD", "Deluxe", 2999)]
    assert curated["truncated"] is False and curated["redirected_from"] is None

    public = node.client.get(f"/api/v2/games/{cid}/prices", headers=h(node.token)).json()
    assert "jp" not in {r["region"] for r in public["results"]}  # not redistributable: curators only
    assert len(public["results"]) == 4

    us = node.client.get(f"/api/v2/games/{cid}/prices?region=us&provider=steam", headers=h(node.token)).json()
    assert [(r["edition"], r["amount_minor"]) for r in us["results"]] == [(None, 999), ("Deluxe", 2999)]
    eur = node.client.get(f"/api/v2/games/{cid}/prices?currency=EUR", headers=h(node.token)).json()
    assert [r["amount_minor"] for r in eur["results"]] == [1799]

    with node.st.maker() as s:
        bounded = prices.latest(s, cid, restricted=True, limit=2)
    assert len(bounded["results"]) == 2 and bounded["truncated"] is True


def test_prices_do_not_touch_the_record(node):
    cid = node.publish("Untouched Game")
    with node.st.maker() as s:
        row = s.get(Canonical, cid)
        before = (row.version, json.dumps(row.view, sort_keys=True), json.dumps(row.explain, sort_keys=True))
    post(node, price(cid))
    with node.st.maker() as s:
        row = s.get(Canonical, cid)
        assert (row.version, json.dumps(row.view, sort_keys=True), json.dumps(row.explain, sort_keys=True)) == before
    game = node.client.get(f"/api/v2/games/{cid}", headers=h(node.token)).json()
    assert "prices" not in game and "amount_minor" not in json.dumps(game)


# ---- regional history ---------------------------------------------------------------------------------------

def test_history_is_regional_newest_first_and_pages_without_gaps(node):
    cid = node.publish("History Game")
    hours = [7, 1, 5, 3, 6, 2, 4]  # sent out of order
    post(node, *[price(cid, observed_at=iso(NOW - timedelta(hours=n)), amount_minor=1000 + n,
                       base_amount_minor=2000) for n in hours],
         price(cid, region="ca", currency="CAD", observed_at=iso(NOW - timedelta(minutes=30))),
         price(cid, provider="gog", product_id="g1", observed_at=iso(NOW - timedelta(hours=1))))
    url = f"/api/v2/games/{cid}/prices/history"

    full = node.client.get(url, params={"region": "us", "provider": "steam"}, headers=h(node.token)).json()
    assert [r["amount_minor"] for r in full["results"]] == [1001, 1002, 1003, 1004, 1005, 1006, 1007]
    assert full["next_cursor"] is None and {r["region"] for r in full["results"]} == {"us"}

    both = node.client.get(url, params={"region": "us"}, headers=h(node.token)).json()["results"]
    stamps = [(r["observed_at"], r["id"]) for r in both]
    assert stamps == sorted(stamps, reverse=True) and len(both) == 8  # gog and steam at the same hour: by id

    seen, cursor = [], None
    while True:
        params = {"region": "us", "limit": 3, **({"cursor": cursor} if cursor else {})}
        page = node.client.get(url, params=params, headers=h(node.token)).json()
        seen += [r["id"] for r in page["results"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [r["id"] for r in both]  # no overlap, no gap

    window = node.client.get(url, params={"region": "us", "provider": "steam",
                                          "since": (NOW - timedelta(hours=5)).isoformat(),
                                          "until": (NOW - timedelta(hours=2)).isoformat()},
                             headers=h(node.token)).json()
    assert [r["amount_minor"] for r in window["results"]] == [1003, 1004, 1005]
    assert full["lowest"] is None  # no currency given: amounts of different currencies do not compare
    low = node.client.get(url, params={"region": "us", "currency": "USD", "provider": "steam", "limit": 2},
                          headers=h(node.token)).json()
    assert low["lowest"]["amount_minor"] == 1001 and low["lowest"]["amount"] == "10.01"  # across all pages
    assert low["lowest"]["observed_at"] == (NOW - timedelta(hours=1)).isoformat()
    windowed = node.client.get(url, params={"region": "us", "currency": "USD", "provider": "steam",
                                            "since": (NOW - timedelta(hours=7)).isoformat(),
                                            "until": (NOW - timedelta(hours=4)).isoformat()},
                               headers=h(node.token)).json()
    assert windowed["lowest"]["amount_minor"] == 1005
    canada = node.client.get(url, params={"region": "ca"}, headers=h(node.token)).json()["results"]
    assert [r["currency"] for r in canada] == ["CAD"]
    assert node.client.get(url, params={"region": "us", "cursor": "not-a-cursor"},
                           headers=h(node.token)).status_code == 400
    assert node.client.get(url, headers=h(node.token)).status_code == 422  # region is required


# ---- retention and purge ------------------------------------------------------------------------------------

def test_retention_hides_then_purge_deletes(node):
    cid = node.publish("Retention Game")
    post(node, price(cid, observed_at=iso(NOW - timedelta(days=29)), amount_minor=1, product_id="a"),
         price(cid, observed_at=iso(NOW - timedelta(days=1)), amount_minor=2, product_id="b", retention_days=2),
         price(cid, observed_at=iso(NOW - timedelta(days=200)), amount_minor=3, product_id="c", retention_days=365))
    rows = {r["product_id"]: r for r in db_rows(node)}
    assert rows["a"]["purge_after"] == NOW - timedelta(days=29) + timedelta(days=30)
    assert rows["c"]["purge_after"] == NOW - timedelta(days=200) + timedelta(days=365)

    with node.st.maker() as s:  # "a" passes its retention before the purge job runs: reads hide it at once
        s.execute(update(PriceObservation).where(PriceObservation.product_id == "a")
                  .values(purge_after=NOW - timedelta(seconds=1)))
        s.commit()
    visible = node.client.get(f"/api/v2/games/{cid}/prices", headers=h(node.token)).json()["results"]
    assert sorted(r["product_id"] for r in visible) == ["b", "c"]
    status = node.client.get("/api/v2/prices/status", headers=h(node.admin)).json()
    assert (status["observations"], status["due_for_purge"], status["records"]) == (3, 1, 1)
    assert status["channel"] == {"protocol": "1.6", "capability": "price-channel-v1",
                                 "endpoint": "/api/v2/sync/price-changes", "enabled": False}
    assert status["retention_days"] == {"default": 30, "max": 365}

    with node.st.maker() as s:
        assert prices.purge(s) == 1
        s.commit()
    assert sorted(r["product_id"] for r in db_rows(node)) == ["b", "c"]
    with node.st.maker() as s:  # three days on, "b" (two days) goes; "c" (a year) stays
        assert prices.purge(s, now=NOW + timedelta(days=3)) == 1
        s.commit()
    assert [r["product_id"] for r in db_rows(node)] == ["c"]


def test_purge_works_in_bounded_batches(node):
    cid = node.publish("Batch Purge Game")
    post(node, *[price(cid, product_id=f"p{i}", retention_days=1) for i in range(7)],
         price(cid, product_id="keep", retention_days=10))
    with node.st.maker() as s:
        assert prices.purge(s, now=NOW + timedelta(days=2), batch=3) == 7
        s.commit()
    assert [r["product_id"] for r in db_rows(node)] == ["keep"]


def test_cli_purge_and_status(tmp_path, monkeypatch, capsys):
    from atlas.settings import get_settings

    monkeypatch.setenv("ATLAS_DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("ATLAS_ROLE", "authority")
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        settings, maker = cli._maker()
        with maker() as s:
            identity = signing.load_identity(s, settings)
            ctx = service.Ctx(settings, identity)
            [r] = service.ingest(s, ctx, batch("manual", [{"external_id": "c", "title": "CLI Game"}], kind="manual"),
                                 curator=True)
            cid = r.canonical_id
            prices.ingest(s, [prices.PriceIn.model_validate(price(cid, retention_days=1,
                                                                   observed_at=iso(NOW - timedelta(hours=23))))])
            prices.ingest(s, [prices.PriceIn.model_validate(price(cid, product_id="kept"))])
            # a day later: the first one's retention has passed
            s.execute(update(PriceObservation).where(PriceObservation.product_id == "620")
                      .values(purge_after=NOW - timedelta(hours=1)))
            s.commit()
        capsys.readouterr()
        assert cli.main(["prices", "status"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert (status["observations"], status["due_for_purge"]) == (2, 1)
        assert cli.main(["prices", "purge"]) == 0
        assert json.loads(capsys.readouterr().out) == {"purged": 1}
        with maker() as s:
            assert list(s.scalars(select(PriceObservation.product_id))) == ["kept"]
    finally:
        get_settings.cache_clear()


# ---- merges -------------------------------------------------------------------------------------------------

def test_prices_follow_a_merge(node):
    keep, dup = node.publish("Merge Keep"), node.publish("Merge Dup")
    post(node, price(dup, product_id="d"), price(keep, product_id="k"))
    with node.st.maker() as s:
        service.merge_canonicals(s, node.st.ctx, dup, keep)
        s.commit()
    assert {r["canonical_id"] for r in db_rows(node)} == {keep}
    got = node.client.get(f"/api/v2/games/{dup}/prices", headers=h(node.token)).json()
    assert got["id"] == keep and got["redirected_from"] == dup
    assert sorted(r["product_id"] for r in got["results"]) == ["d", "k"]
    # A new observation sent for the merged id lands on the survivor.
    resp = post(node, price(dup, product_id="late"))
    assert resp.json()["results"][0]["canonical_id"] == keep


# ---- signatures ---------------------------------------------------------------------------------------------

def test_price_and_feed_signatures_do_not_cross_verify(node):
    identity = node.st.ctx.identity
    body = {"canonical_id": "atlas:x", "amount_minor": 999, "currency": "USD"}
    digest = signing.body_hash(body)
    price_sig = identity.sign_price(7, digest)
    feed_sig = identity.sign(7, "price", digest)
    public = identity.public_b64
    assert signing.verify_price(public, identity.node_id, 7, body, digest, price_sig)
    assert not signing.verify(public, identity.node_id, 7, "price", body, digest, price_sig)  # as a feed entry
    assert not signing.verify_price(public, identity.node_id, 7, body, digest, feed_sig)  # a feed sig as a price
    assert not signing.verify_price(public, identity.node_id, 8, body, digest, price_sig)  # another seq
    from atlas import group_crypto

    assert group_crypto.prefix_free(group_crypto.all_domains()) and signing.PRICE_DOMAIN in group_crypto.all_domains()


# ---- emission disabled: nothing price-related leaves the node ----------------------------------------------

PRICE_WORDS = ("price", "amount_minor", "currency", "base_amount_minor", "availability")


def test_nothing_price_related_is_emitted_by_default(node):
    """The authority runs at the highest emit protocol this release has. Storing, reading, merging and purging
    prices signs nothing, advertises nothing, and the channel endpoint is a 404."""
    a, b = node.publish("Emit A"), node.publish("Emit B")
    head = node.head()
    with node.st.maker() as s:
        before_changes = s.scalar(select(func.count(ChangeLog.seq)))
    resp = post(node, price(a), price(a, region="jp", currency="JPY", amount_minor=1980, base_amount_minor=1980,
                                      license={"redistributable": True, "terms": "x"}),
                price(b, observed_at=iso(NOW - timedelta(days=40)), retention_days=60))
    assert resp.json()["counts"] == {"stored": 3}
    with node.st.maker() as s:
        prices.purge(s, now=NOW + timedelta(days=25))
        s.commit()
    assert node.head() == head  # not one feed entry for any of it
    with node.st.maker() as s:
        assert s.scalar(select(func.count(ChangeLog.seq))) == before_changes
        assert s.scalar(select(func.count(ChangeLog.seq)).where(ChangeLog.kind == "price")) == 0
    changes = node.client.get("/api/v2/sync/changes?since=0&limit=5000", headers=h(node.token)).json()
    wire = json.dumps(changes["entries"])
    assert not any(word in wire for word in PRICE_WORDS)
    assert {e["kind"] for e in changes["entries"]} <= set(feed_schema.FEED_KINDS)

    info = node.client.get("/api/v2/sync/info", headers=h(node.token)).json()
    assert "price-channel-v1" not in info["capabilities"] and "price" not in info["kinds"]
    assert info["protocol"] == "1.5" and info["emit_protocol"] == "1.5"
    channel = node.client.get("/api/v2/sync/price-changes?since=0", headers=h(node.token))
    assert channel.status_code == 404 and "price" not in json.dumps(node.client.get(
        "/api/v2/sync/digest", headers=h(node.token)).json())
    with node.st.maker() as s, pytest.raises(feed_schema.EmitGateError):
        feed.emit(s, node.st.ctx, "price", prices.channel_body(s.scalars(select(PriceObservation)).first()))
    assert node.head() == head


# ---- followers: unaffected, and a stray `price` entry parks -------------------------------------------------

def play(monkeypatch, release: str) -> None:
    """Play a follower of an earlier release in this process. "a15" is the 1.4 release before this change
    (no reserved kinds); "1.3" and "1.1" also do not know `relation`."""
    if release == "this":
        return
    monkeypatch.setattr(feed_schema, "RESERVED_KINDS", {})
    if release in ("1.3", "1.1"):
        kinds = {k: v for k, v in feed_schema.FEED_KINDS.items() if k != "relation"}
        monkeypatch.setattr(feed_schema, "FEED_KINDS", kinds)
        monkeypatch.setattr(feed_schema, "RESERVED_KINDS", {"relation": "1.4"})
        monkeypatch.setattr(feed_schema, "PROTOCOL", release)
        monkeypatch.setattr(sync, "PROTOCOL", release)


@pytest.fixture
def fed(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol=feed_schema.PROTOCOL)
    with authority.st.maker() as s:
        _, authority.curate = auth.create_token(s, "curator", ["read", "curate"])
        s.commit()
    return authority, Node(tmp_path, "mirror", "mirror")


@pytest.mark.parametrize("release", ["this", "a15", "1.3", "1.1"])
def test_followers_see_no_price_and_converge(fed, monkeypatch, release):
    authority, mirror = fed
    game = authority.publish("Fed Price Game")
    post(authority, price(game), price(game, region="kw", currency="KWD", amount_minor=1500, base_amount_minor=1500))
    authority.publish("Fed After")
    with monkeypatch.context() as m:
        play(m, release)
        pull(mirror, authority)
        assert mirror.failures(authority) == []
        heads = mirror.heads(authority)
        assert heads["applied_head"] == heads["verified_head"] == authority.head()
    assert {"Fed Price Game", "Fed After"} <= mirror.titles()
    assert count(mirror) == 0 and count(authority) == 2  # prices stay on the authority
    assert mirror.retained(authority) == authority.own_feed()


@pytest.mark.parametrize("release", ["this", "a15", "1.3", "1.1"])
def test_a_price_entry_in_the_main_feed_parks_instead_of_dropping(fed, monkeypatch, release):
    """Prices never enter the main feed. If a (buggy or newer) authority signed one there anyway, followers of
    this release and of every 1.1+ release park it, hold later entries, and keep it for a release that knows."""
    authority, mirror = fed
    authority.publish("Before Price")
    seq = authority.emit("price", {"canonical_id": "atlas:x", "provider": "steam", "region": "us",
                                   "currency": "USD", "amount_minor": 999})
    authority.publish("After Price")
    with monkeypatch.context() as m:
        play(m, release)
        pull(mirror, authority)
        assert mirror.failures(authority) == [(seq, "price", "parked", "unsupported_kind")]
        heads = mirror.heads(authority)
        assert heads["applied_head"] == seq - 1 and heads["verified_head"] == authority.head()
        assert "After Price" not in mirror.titles() and seq in mirror.retained(authority)
    assert count(mirror) == 0


def test_capability_appears_only_when_enabled(node, monkeypatch):
    """The capability is tied to the switch: a release that implements the channel flips one constant."""
    info = node.client.get("/api/v2/sync/info", headers=h(node.token)).json()
    assert info["capabilities"] == list(feed_schema.CAPABILITIES)
    monkeypatch.setattr(feed_schema, "CHANNEL_ENABLED", True)
    assert "price-channel-v1" in feed_schema.capabilities()
