"""Partial and regional release dates (CONTRACTS-v1 C5, master prompt part 7).

Covers the PartialDate type (every precision, leap days, invalid dates), up to five release entries, the
hierarchical merge vote with provenance, the derived primary date (display, search, sort and filter), locks
and the emission gate, federation (a 1.3 follower applies; 1.1 and 1.2 followers park, keep their state and
converge after upgrade), providers, legacy import, the editor and the resumable rebuild. Every scenario that
writes asserts the persistent state in the node's database, not only the HTTP answer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from html import unescape

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from atlas import auth, cli, dates, dates_rebuild, feed_schema, main, service, sync
from atlas.contract import SourceObservationV1
from atlas.legacy_import import import_legacy
from atlas.merge import Obs, merge
from atlas.models import Canonical, ChangeLog, Lock, NodeState, Observation, Proposal, ReplicatedChange, SyncFailure
from atlas.providers.base import parse_date, parse_release, release_ext
from atlas.providers.igdb import Igdb
from atlas.providers.steam import Steam
from atlas.service import AtlasError, Ctx
from atlas.signing import body_hash, load_identity

from .conftest import batch, build_settings
from .test_sync_compat import Node, _admin, observation, pull
from .test_web import _wait_for_startup_replay, signup, token


def ingest(session, ctx, source, items, **kw):
    kw.setdefault("system", True)
    res = service.ingest(session, ctx, batch(source, items, kind=kw.pop("kind", "metadata")), **kw)
    session.commit()
    return res


def release(first=None, entries=None):
    out = {}
    if first:
        out["first"] = {"date": first, "precision": dates.precision_of(first)}
    if entries:
        out["entries"] = entries
    return {"release": out}


def entry(when, platform=None, region=None, category="full", edition=None):
    e = {"date": when, "precision": dates.precision_of(when), "category": category}
    for k, v in (("platform", platform), ("region", region), ("edition", edition)):
        if v:
            e[k] = v
    return e


# ---- C5.1 PartialDate ------------------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "precision", "start", "end"), [
    ("2011", "year", date(2011, 1, 1), date(2011, 12, 31)),
    ("2011-04", "month", date(2011, 4, 1), date(2011, 4, 30)),
    ("2011-04-19", "day", date(2011, 4, 19), date(2011, 4, 19)),
    ("2024-02", "month", date(2024, 2, 1), date(2024, 2, 29)),  # leap February ends on the 29th
    ("2023-02", "month", date(2023, 2, 1), date(2023, 2, 28)),
    ("1950", "year", date(1950, 1, 1), date(1950, 12, 31)),
    ("2100-12-31", "day", date(2100, 12, 31), date(2100, 12, 31)),
])
def test_every_precision_is_a_period(value, precision, start, end):
    when = dates.parse(value, precision)
    assert (when.precision, when.start(), when.end()) == (precision, start, end)
    assert dates.parse(value).precision == precision  # the shape decides when no precision is given
    assert when.as_dict() == {"date": value, "precision": precision}


@pytest.mark.parametrize(("value", "ok"), [
    ("2024-02-29", True), ("2000-02-29", True),  # leap years (2000: divisible by 400)
    ("2023-02-29", False), ("2100-02-29", False), ("1900-02-29", False),  # not leap (2100, 1900: by 100)
    ("2011-04-31", False), ("2011-06-31", False), ("2011-12-31", True),
])
def test_leap_days_and_impossible_days(value, ok):
    assert (dates.try_parse(value) is not None) is ok
    if not ok:
        with pytest.raises(dates.DateError, match="calendar date|between 1950 and 2100"):
            dates.parse(value)


@pytest.mark.parametrize("value", [
    "", "11", "20111", "2011-4", "2011-13", "2011-00", "2011-04-1", "2011-04-00", "2011-04-32", "2011/04/19",
    "19.04.2011", "2011-04-19T00:00", " 2011", "2011 ", "Q3 2025", "2025-Q3", "TBA", None, 2011, 2011.0,
    ["2011"], "２０１１",
])
def test_invalid_formats_are_refused(value):
    with pytest.raises(dates.DateError):
        dates.parse(value)


def test_precision_must_match_and_year_must_be_in_range():
    with pytest.raises(dates.DateError, match="does not match"):
        dates.parse("2011-04", "day")
    with pytest.raises(dates.DateError, match="does not match"):
        dates.parse("2011", "month")
    with pytest.raises(dates.DateError, match="precision must be one of"):
        dates.parse("2011", "quarter")  # reserved, not v1
    for year in ("1949", "2101", "0000", "9999"):
        with pytest.raises(dates.DateError, match="between 1950 and 2100"):
            dates.parse(year)
    assert dates.PRECISIONS == ("year", "month", "day")
    # The legacy day key keeps any year a date can hold; partial values never widen.
    assert dates.parse("1937-12-21", "day", any_year=True).year == 1937
    assert dates.parse_first("1937-12-21").value == "1937-12-21"
    for partial in ("1948", "1948-05"):
        with pytest.raises(dates.DateError, match="between 1950 and 2100"):
            dates.parse_first(partial)
    with pytest.raises(dates.DateError):
        dates.parse("0000-01-01", any_year=True)
    assert dates.display("0999-03-04", "day", {"date_format": "dmy"}) == "04/03/0999"


def test_agreement_and_sort_key():
    year, month, day = dates.parse("2011"), dates.parse("2011-04"), dates.parse("2011-04-19")
    assert year.agrees_with(day) and day.agrees_with(year) and month.agrees_with(day)
    assert not dates.parse("2011-05").agrees_with(day) and not dates.parse("2012").agrees_with(month)
    # sort: by start, then year before month before day
    assert sorted([day, month, year], key=lambda d: d.sort_key()) == [year, month, day]


@pytest.mark.parametrize(("value", "precision", "prefs", "shown"), [
    ("2011", "year", None, "2011"),
    ("2011", "year", {"date_format": "dmy"}, "2011"),
    ("2011-04", "month", None, "April 2011"),
    # SPEC-UI-POLISH-PASS §6.2: a month is never shown as numbers that could pass for a day
    ("2011-04", "month", {"date_format": "dmy"}, "April 2011"),
    ("2011-04", "month", {"date_format": "mdy"}, "April 2011"),
    ("2011-04", "month", {"date_format": "iso"}, "2011-04"),
    ("2011-04-19", "day", None, "19/04/2011"),
    ("2011-04-19", "day", {"date_format": "dmy"}, "19/04/2011"),
    ("2011-04-19", "day", {"date_format": "long"}, "19 April 2011"),
    ("2011-04-19", "day", {"date_format": "iso"}, "2011-04-19"),
    ("2011-04-19", "day", {"date_format": "locale"}, "19/04/2011"),  # legacy value maps to the default
    ("2011-04-19", "day", {"date_format": "ymd"}, "2011-04-19"),
    ("2011-04-19", "day", {"date_format": "mdy"}, "04/19/2011"),
    ("soon", None, None, "soon"),  # invalid stays as it is (an old lock value), never a guess
    (None, None, None, ""),
])
def test_display_shows_only_what_is_known(value, precision, prefs, shown):
    assert dates.display(value, precision, prefs) == shown


# ---- C5.1 entries and the contract ------------------------------------------------------------------------

def test_entries_are_validated_normalised_and_keyed():
    [e] = dates.clean_entries([{"date": "2011-04", "precision": "month", "platform": "  PS5\t", "region": "eu",
                                "edition": "Deluxe", "category": "early_access"}])
    assert e == {"date": "2011-04", "precision": "month", "platform": "PS5", "region": "eu", "edition": "Deluxe",
                 "category": "early_access"}
    assert dates.entry_key(e) == ("ps5", "eu", "deluxe", "early_access")
    bad = [({"date": "2011", "precision": "year", "category": "launch"}, "category must be one of"),
           ({"date": "2011", "precision": "year", "category": "full", "region": "EU"}, "region is"),
           ({"date": "2011", "precision": "year", "category": "full", "region": "europe"}, "region is"),
           ({"date": "2011", "precision": "year", "category": "full", "store": "x"}, "unknown release entry field"),
           ({"date": "2011", "precision": "year", "category": "full", "platform": "p" * 51}, "at most 50"),
           ({"date": "2023-02-29", "precision": "day", "category": "full"}, "calendar date"),
           ({"date": "2011", "precision": "day", "category": "full"}, "does not match")]
    for raw, message in bad:
        with pytest.raises(dates.DateError, match=message):
            dates.clean_entries([raw])


def test_more_than_five_entries_and_duplicate_keys_are_refused():
    six = [entry("2011", platform=f"p{i}") for i in range(6)]
    with pytest.raises(dates.DateError, match="at most 5 release dates per game"):
        dates.clean_entries(six)
    assert len(dates.clean_entries(six[:5])) == 5
    with pytest.raises(dates.DateError, match="repeats platform, region, edition and category"):
        dates.clean_entries([entry("2011", platform="PS5"), entry("2012", platform="ps5")])  # case-insensitive
    # a different category is a different entry
    assert len(dates.clean_entries([entry("2011", platform="pc"), entry("2010", platform="pc",
                                                                        category="early_access")])) == 2


def _obs(**payload):
    return {"external_id": "x", "title": "T", **payload}


def test_contract_accepts_ext_release_and_keeps_release_date_day_only():
    ok = SourceObservationV1.model_validate(_obs(ext=release("2011", [entry("2011-04", "pc", "ww")])))
    assert ok.reported()["ext"]["release"]["first"] == {"date": "2011", "precision": "year"}
    same = SourceObservationV1.model_validate(_obs(release_date="2011-04-19", ext=release("2011-04-19")))
    assert same.reported()["release_date"] == "2011-04-19"
    for payload, message in [
        (_obs(release_date="2011"), "release_date"),  # the legacy key never takes a partial date
        (_obs(release_date="2011-04-19", ext=release("2011")), "release_date"),  # must equal a day first
        (_obs(release_date="2011-04-19", ext=release("2011-04-20")), "release_date"),
        (_obs(ext={"release": {}}), "first release or at least one entry"),
        (_obs(ext={"release": {"first": {"date": "2011", "precision": "day"}}}), "does not match"),
        (_obs(ext=release(entries=[entry("2011", f"p{i}") for i in range(6)])), "at most 5|too_long"),
        (_obs(ext=release(entries=[entry("2011", "pc"), entry("2012", "PC")])), "must differ"),
        (_obs(edition="Standard", ext=release(entries=[entry("2011", "pc", edition="Deluxe")])), "edition"),
        (_obs(ext={"release": {"first": {"date": "2011", "precision": "year"}, "label": "x"}}), "extra"),
        (_obs(ext={"price": {}}), "extra"),
        # the first release cannot start after the observation's own full or early-access release (C5.1)
        (_obs(ext=release("2015", [entry("2011-01-01", "pc")])), "after this observation's own"),
        (_obs(release_date="2015-01-01", ext=release(entries=[entry("2011-01-01", "pc", category="early_access")])),
         "after this observation's own"),
        (_obs(ext={"release": {"first": {"date": "1937-12-21", "precision": "day"}}}), "between 1950 and 2100"),
    ]:
        with pytest.raises(ValidationError, match=message):
            SourceObservationV1.model_validate(payload)
    # earlier is fine (only five entries fit), as is a beta before the first release, or a legacy pre-1950 day
    SourceObservationV1.model_validate(_obs(ext=release("2010", [entry("2011-01-01", "pc")])))
    SourceObservationV1.model_validate(_obs(ext=release("2015", [entry("2011", "pc", category="beta")])))
    old = SourceObservationV1.model_validate(_obs(release_date="1937-12-21"))
    assert old.reported()["release_date"] == "1937-12-21"


# ---- C5.2 merge: the hierarchical vote and provenance -----------------------------------------------------

def o(oid, source, trust, *, first=None, release_date=None, entries=None, stamp=0):
    payload = {"title": "T"}
    if release_date:
        payload["release_date"] = release_date
    if first or entries:
        payload["ext"] = release(first, entries)
    return Obs(oid, source, "metadata", oid, payload, trust, stamp)


@pytest.mark.parametrize(("candidates", "value", "precision"), [
    # a year and a day that agree: the day wins (a less precise source never outvotes a precise one)
    ([("2011", 90), ("2011-04-19", 50)], "2011-04-19", "day"),
    # conflicting years: the heavier year wins; the day of the losing year never leaks in
    ([("2011-04-19", 50), ("2012", 90)], "2012", "year"),
    ([("2011-04-19", 50), ("2011-04-19", 50), ("2012", 90)], "2011-04-19", "day"),
    # the month is split evenly: ties go to the higher-ranked (first) candidate
    ([("2011-04", 50), ("2011-05", 50)], "2011-04", "month"),
    # a month split where the winner has less than half: stop at the year
    ([("2011-04", 40), ("2011-05", 40), ("2011-06", 40)], "2011", "year"),
    # days split below 50%: stop at the agreed month
    ([("2011-04-19", 40), ("2011-04-20", 40), ("2011-04-21", 40)], "2011-04", "month"),
    # a year-only source agreeing with two different days does not count against either
    ([("2011", 100), ("2011-04-19", 60), ("2011-04-19", 40), ("2011-04-20", 30)], "2011-04-19", "day"),
    # trust 0 still counts as 1
    ([("2011", 0)], "2011", "year"),
])
def test_hierarchical_vote(candidates, value, precision):
    result = dates.vote([(dates.parse(v), t) for v, t in candidates])
    assert (result.value.value, result.value.precision) == (value, precision)
    assert candidates[result.winner][0].startswith(value)
    assert result.disputed is False


def test_no_year_with_half_the_weight_is_a_disputed_year_only():
    # C5.2: three sources, three years, no majority: never a month or a day, and flagged low confidence
    result = dates.vote([(dates.parse(v), 50) for v in ("2010-03-01", "2011-04-02", "2012-05-03")])
    assert (result.value.value, result.disputed, result.support) == ("2010", True, {"year": [50, 150]})
    view, explain = merge([o("A", "igdb", 50, release_date="2010-03-01"), o("B", "steam", 50, release_date="2011-04-02"),
                           o("C", "rawg", 50, release_date="2012-05-03")], {})
    # equal trust and age: the merge ranks C first (ties go to the higher-ranked observation)
    assert view["first_release"] == {"date": "2012", "precision": "year"} and "release_date" not in view
    assert explain["first_release"]["disputed"] is True and explain["first_release"]["disagree"] == ["igdb", "steam"]
    # an exact half is a majority (ties go to the higher-ranked source)
    assert dates.vote([(dates.parse("2010"), 50), (dates.parse("2011"), 50)]).disputed is False


def test_a_legacy_day_before_1950_survives_the_merge():
    """Films and TV predate the C5.1 release range: `release_date` stays exactly as it was (C5.2)."""
    view, explain = merge([o("A", "tmdb", 60, release_date="1937-12-21")], {})
    assert view["release_date"] == "1937-12-21" and view["release_year"] == 1937
    assert view["first_release"] == {"date": "1937-12-21", "precision": "day"}
    assert explain["release_date"]["observation_id"] == "A"
    assert dates.columns(view["first_release"])["year"] == 1937
    view, _ = merge([o("A", "tmdb", 60, release_date="1937-12-21")], {"release_date": "1938-02-04"})
    assert view["release_date"] == "1938-02-04"  # a curator can still pin a pre-1950 day


def test_merge_derives_first_release_and_records_provenance():
    view, explain = merge([o("A", "igdb", 70, first="2011"), o("B", "steam", 65, release_date="2011-04-19"),
                           o("C", "rawg", 50, release_date="2012-01-01")], {})
    assert view["first_release"] == {"date": "2011-04-19", "precision": "day"}
    assert view["release_date"] == "2011-04-19" and view["release_year"] == 2011
    assert explain["first_release"]["strategy"] == "vote" and explain["first_release"]["observation_id"] == "B"
    assert explain["first_release"]["sources"] == ["igdb", "steam"] and explain["first_release"]["disagree"] == ["rawg"]
    assert explain["first_release"]["support"]["year"] == [135, 185]
    assert explain["release_date"]["observation_id"] == "B"


def test_a_partial_first_release_never_fills_the_legacy_day_key():
    view, explain = merge([o("A", "igdb", 70, first="2011-04")], {})
    assert view["first_release"] == {"date": "2011-04", "precision": "month"} and view["release_year"] == 2011
    assert "release_date" not in view and "release_date" not in explain


def test_conflicting_providers_entries_vote_per_key_and_union_across_keys():
    a = o("A", "igdb", 70, entries=[entry("2011-04-19", "pc", "ww"), entry("2011-06", "ps3", "eu")])
    b = o("B", "steam", 65, entries=[entry("2011-04-18", "PC", "ww"), entry("2012", "switch", "jp", "port")])
    c = o("C", "rawg", 50, entries=[entry("2011-04-18", "pc", "ww")])
    view, explain = merge([a, b, c], {})
    rel = view["releases"]
    assert [(r["platform"].casefold(), r["date"]) for r in rel] == [("pc", "2011-04-18"), ("ps3", "2011-06"),
                                                                   ("switch", "2012")]  # chronological
    # pc/ww: 2011-04-18 has steam 65 + rawg 50 = 115 against igdb 70
    pc = explain["releases"]["entries"][0]
    assert pc["observation_id"] == "B" and pc["sources"] == ["igdb", "rawg", "steam"]
    assert pc["support"]["day"] == [115, 185]
    assert explain["releases"]["dropped"] == 0
    # the first release comes from the full/early-access entries when no source states one
    assert view["first_release"] == {"date": "2011-04-18", "precision": "day"}


def test_more_than_five_merged_entries_keep_five_by_trust_and_report_the_rest():
    obs = [o(f"O{i}", f"src{i}", 90 - i, entries=[entry(f"20{10 + i}", f"p{i}")]) for i in range(7)]
    view, explain = merge(obs, {})
    assert len(view["releases"]) == 5 and explain["releases"]["dropped"] == 2
    assert {r["platform"] for r in view["releases"]} == {f"p{i}" for i in range(5)}  # the most trusted five


def test_locks_win_and_an_invalid_old_lock_is_marked():
    obs = [o("A", "igdb", 70, release_date="2011-04-19", entries=[entry("2011-04-19", "pc")])]
    view, explain = merge(obs, {"release_date": "2010", "releases": [entry("2010-12", "pc", "ww")]})
    assert view["first_release"] == {"date": "2010", "precision": "year"} and "release_date" not in view
    assert explain["first_release"] == {"strategy": "lock"} and explain["releases"] == {"strategy": "lock"}
    assert view["releases"] == [entry("2010-12", "pc", "ww")]
    view, explain = merge(obs, {"release_date": "soon"})  # an earlier release accepted any text
    assert "first_release" not in view and explain["first_release"] == {"strategy": "lock", "invalid": True}
    assert view["release_date"] == "soon"  # shown in the legacy key, as the previous release did
    assert "release_year" not in view and dates.columns(view.get("first_release"))["year"] is None


def test_a_releases_lock_sets_the_first_release_and_earlier_entries_are_flagged():
    obs = [o("A", "igdb", 70, release_date="2015-06-01")]
    view, explain = merge(obs, {"releases": [entry("2013-05", "pc", category="early_access"), entry("2014", "ps4")]})
    # C5.1: the first release is the earliest full or early-access entry of the pinned list
    assert view["first_release"] == {"date": "2013-05", "precision": "month"} and "release_date" not in view
    assert explain["first_release"] == {"strategy": "lock", "field": "releases"}
    view, _ = merge(obs, {"releases": [entry("2013", "pc", category="beta")]})  # no full entry: the vote stands
    assert view["first_release"] == {"date": "2015-06-01", "precision": "day"}
    # a lower-trust source's earlier full entry stays in releases; the trust vote picks the first release and
    # the contradiction is visible in explain
    view, explain = merge([o("A", "igdb", 90, release_date="2015-06-01"),
                           o("B", "rawg", 10, entries=[entry("2012-03", "pc")])], {})
    assert view["first_release"]["date"] == "2015-06-01"
    assert explain["first_release"]["earlier_entry"] == {"date": "2012-03", "precision": "month"}


# ---- service: canonical columns, sort and filter ----------------------------------------------------------

@pytest.fixture
def catalogue(session, ctx):
    games = {"Year Only": release("2011"), "April": release("2011-04"), "Day": release("2011-04-19"),
             "Late": release("2011-12-31"), "Earlier": release("2010-06"), "Unknown": None}
    ids = {}
    for title, ext in games.items():
        item = {"external_id": title, "title": title}
        if ext:
            item["ext"] = ext
        [r] = ingest(session, ctx, "igdb", [item])
        ids[title] = r.canonical_id
    return ids


def test_columns_follow_the_first_release(session, catalogue):
    row = session.get(Canonical, catalogue["Year Only"])
    assert (row.release_start, row.release_end, row.release_precision, row.year) == (
        date(2011, 1, 1), date(2011, 12, 31), "year", 2011)
    row = session.get(Canonical, catalogue["April"])
    assert (row.release_start, row.release_end, row.release_precision) == (date(2011, 4, 1), date(2011, 4, 30),
                                                                           "month")
    row = session.get(Canonical, catalogue["Unknown"])
    assert (row.release_start, row.release_end, row.release_precision, row.year) == (None, None, None, None)


def titles(rows):
    return [r.view["title"] for r in rows]


def test_sort_by_release_is_deterministic(session, catalogue):
    assert titles(service.search(session, "", sort="release")) == [
        "Earlier", "Year Only", "April", "Day", "Late", "Unknown"]
    assert titles(service.search(session, "", sort="-release")) == [
        "Late", "Day", "April", "Year Only", "Earlier", "Unknown"]  # unknown stays last


@pytest.mark.parametrize(("low", "high", "expected"), [
    ("2011", "2011", {"Year Only", "April", "Day", "Late"}),
    ("2011-04", "2011-04", {"April", "Day"}),  # "2011" might be any month: not inside April
    ("2011-04-19", "2011-04-19", {"Day"}),
    ("2011-04-20", None, {"Late"}),
    (None, "2010", {"Earlier"}),
    ("2010-06", "2011-04", {"Earlier", "April", "Day"}),
    ("2012", None, set()),
])
def test_filter_matches_a_release_inside_the_range(session, catalogue, low, high, expected):
    assert set(titles(service.search(session, "", released_from=low, released_to=high))) == expected


def test_filter_errors(session, catalogue):
    with pytest.raises(AtlasError, match="released_from: a release date is"):
        service.search(session, "", released_from="2011-4")
    with pytest.raises(AtlasError, match="after released_to"):
        service.search(session, "", released_from="2012", released_to="2011")
    with pytest.raises(AtlasError, match="sort must be one of"):
        service.search(session, "", sort="year")


# ---- API: additive keys, filters and locks ----------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    app = main.create_app(build_settings(database_url=f"sqlite:///{tmp_path}/api.db", media_dir=str(tmp_path / "m"),
                                         media_cache_enabled=False, emit_protocol="1.3"))
    st = app.state.atlas
    with st.maker() as s:
        _, secret = auth.create_token(s, "dates", ["read", "contribute", "curate", "admin"])
        s.commit()
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {secret}"
    client.st = st
    return client


def post(api, items, source="manual", kind="manual"):
    r = api.post("/api/v2/observations", json={"connection": {"source_kind": kind, "source": source,
                                                              "source_scope": "dates"}, "observations": items})
    return r


def test_api_exposes_first_release_and_keeps_release_date_day_only(api):
    r = post(api, [{"external_id": "a", "title": "Partial", "ext": release("2011-04", [entry("2011-04", "pc")])},
                   {"external_id": "b", "title": "Day", "release_date": "2012-01-02"}])
    assert r.status_code == 200, r.text
    partial, day = (x["atlas"]["canonical_id"] for x in r.json()["results"])
    g = api.get(f"/api/v2/games/{partial}").json()
    assert g["first_release"] == {"date": "2011-04", "precision": "month"} and g["release_year"] == 2011
    assert "release_date" not in g  # an old client never sees a partial date in the day key
    assert g["releases"] == [entry("2011-04", "pc")]
    g = api.get(f"/api/v2/games/{day}").json()
    assert g["release_date"] == "2012-01-02" and g["first_release"] == {"date": "2012-01-02", "precision": "day"}
    with api.st.maker() as s:
        row = s.get(Canonical, partial)
        assert row.release_precision == "month" and row.view["releases"][0]["platform"] == "pc"
        stored = s.scalars(select(Observation).where(Observation.canonical_id == partial)).one()
        assert stored.payload["ext"]["release"]["first"] == {"date": "2011-04", "precision": "month"}
    explain = api.get(f"/api/v2/games/{partial}/explain").json()["explain"]
    assert explain["first_release"]["source"] == "manual"


def test_api_refuses_six_entries_and_a_partial_release_date(api):
    six = [entry("2011", f"p{i}") for i in range(6)]
    r = post(api, [{"external_id": "a", "title": "Too Many", "ext": release(entries=six)}])
    assert r.status_code == 422
    r = post(api, [{"external_id": "b", "title": "Old Key", "release_date": "2011"}])
    assert r.status_code == 422
    with api.st.maker() as s:
        assert s.scalars(select(Observation)).first() is None  # nothing stored


def test_api_list_filters_sort_and_old_clients(api):
    for i, (title, when) in enumerate([("B Year", "2011"), ("A Month", "2011-04"), ("C Day", "2011-04-19")]):
        assert post(api, [{"external_id": str(i), "title": title, "ext": release(when)}]).status_code == 200
    r = api.get("/api/v2/games", params={"released_from": "2011", "released_to": "2011", "sort": "release"})
    assert [g["title"] for g in r.json()["results"]] == ["B Year", "A Month", "C Day"]
    r = api.get("/api/v2/games", params={"released_from": "2011-04", "released_to": "2011-04"})
    assert {g["title"] for g in r.json()["results"]} == {"A Month", "C Day"}
    assert api.get("/api/v2/games", params={"q": "Day"}).json()["results"][0]["title"] == "C Day"  # unchanged
    assert api.get("/api/v2/games").status_code == 422  # neither q nor a filter
    blank = api.get("/api/v2/games", params={"q": " "})  # as before: a blank q lists recent records
    assert blank.status_code == 200 and len(blank.json()["results"]) == 3
    assert api.get("/api/v2/games", params={"released_from": "2011-13"}).status_code == 400
    assert api.get("/api/v2/games", params={"q": "x", "sort": "year"}).status_code == 422


def test_api_locks_partial_and_releases_signed_with_the_token(api):
    [r] = post(api, [{"external_id": "a", "title": "Lockable", "release_date": "2011-04-19"}]).json()["results"]
    cid = r["atlas"]["canonical_id"]
    g = api.put(f"/api/v2/games/{cid}/locks/release_date", json={"value": "2011"}).json()
    assert g["first_release"] == {"date": "2011", "precision": "year"} and "release_date" not in g
    rows = [entry("2011-04-19", "pc", "ww"), entry("2011-06", "ps3", "eu")]
    g = api.put(f"/api/v2/games/{cid}/locks/releases", json={"value": rows}).json()
    assert g["releases"] == rows
    bad = api.put(f"/api/v2/games/{cid}/locks/releases", json={"value": [entry("2011", f"p{i}") for i in range(6)]})
    assert bad.status_code in (400, 422)
    assert api.put(f"/api/v2/games/{cid}/locks/release_date", json={"value": "2023-02-29"}).status_code == 400
    with api.st.maker() as s:
        assert s.get(Lock, (cid, "release_date")).value == "2011"
        assert s.get(Lock, (cid, "releases")).value == rows
        locks = [c for c in s.scalars(select(ChangeLog).where(ChangeLog.kind == "lock").order_by(ChangeLog.seq))]
        assert [c.body["field"] for c in locks] == ["release_date", "releases"]
        assert all(c.body["_requires"] == ["dates-v1"] and c.body_hash == body_hash(c.body) for c in locks)
    assert api.delete(f"/api/v2/games/{cid}/locks/releases").status_code == 200
    with api.st.maker() as s:
        unlock = s.scalars(select(ChangeLog).where(ChangeLog.kind == "unlock")).one()
        assert unlock.body["_requires"] == ["dates-v1"]


def test_a_gated_authority_refuses_partial_locks_and_keeps_observations_local(tmp_path):
    authority = Node(tmp_path, "gated", "authority", emit_protocol="1.2")
    admin = {"Authorization": f"Bearer {_admin(authority)}"}
    r = authority.client.post("/api/v2/observations", headers=admin, json={
        "connection": {"source_kind": "manual", "source": "manual", "source_scope": "g"},
        "observations": [{"external_id": "a", "title": "Gated", "release_date": "2011-04-19",
                          "ext": release("2011-04-19", [entry("2011-04-19", "pc", "jp")])}]})
    assert r.status_code == 200, r.text
    cid = r.json()["results"][0]["atlas"]["canonical_id"]
    before = authority.head()
    refused = authority.client.put(f"/api/v2/games/{cid}/locks/release_date", headers=admin, json={"value": "2011"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "emit_protocol"
    assert "needs federation protocol 1.3; this node emits 1.2" in refused.json()["error"]["message"]
    refused = authority.client.put(f"/api/v2/games/{cid}/locks/releases", headers=admin,
                                   json={"value": [entry("2011", "pc")]})
    assert refused.status_code == 409
    ok = authority.client.put(f"/api/v2/games/{cid}/locks/release_date", headers=admin, json={"value": "2011-04-20"})
    assert ok.status_code == 200  # a day lock is what a 1.2 follower already understands
    with authority.st.maker() as s:
        assert s.get(Lock, (cid, "releases")) is None and s.get(Lock, (cid, "release_date")).value == "2011-04-20"
        [obs_entry] = [c for c in s.scalars(select(ChangeLog).where(ChangeLog.kind == "obs"))]
        signed = obs_entry.body["observation"]["payload"]
        assert "ext" not in signed and "_requires" not in obs_entry.body  # the signed copy omits ext.release
        stored = s.scalars(select(Observation).where(Observation.canonical_id == cid)).one()
        assert stored.payload["ext"]["release"]["entries"][0]["region"] == "jp"  # the local copy is complete
        lock_entry = s.scalars(select(ChangeLog).where(ChangeLog.kind == "lock")).one()
        assert "_requires" not in lock_entry.body
    assert authority.head() == before + 1  # only the day lock was signed
    assert authority.client.get(f"/api/v2/games/{cid}", headers=admin).json()["releases"][0]["region"] == "jp"


def test_check_emit_for_dates():
    obs_body = observation("X", release_date="2011-04-19", ext=release("2011-04-19", [entry("2011-04-19", "pc")]))
    low = feed_schema.check_emit("obs", obs_body, "1.2")
    assert "ext" not in low["observation"]["payload"] and "ext" in obs_body["observation"]["payload"]
    assert "_requires" not in low
    high = feed_schema.check_emit("obs", obs_body, "1.3")
    assert high["_requires"] == ["dates-v1"] and high["observation"]["payload"]["ext"]
    again = feed_schema.check_emit("obs", high, "1.3")
    assert again["_requires"] == ["dates-v1"]  # never twice
    for body in ({"canonical_id": "atlas:x", "field": "releases", "value": []},
                 {"canonical_id": "atlas:x", "field": "release_date", "value": "2011"},
                 {"canonical_id": "atlas:x", "field": "releases"}):
        kind = "unlock" if "value" not in body else "lock"
        with pytest.raises(feed_schema.EmitGateError, match="Raise federation emit protocol to 1.3 first"):
            feed_schema.check_emit(kind, body, "1.2")
        assert feed_schema.check_emit(kind, body, "1.3")["_requires"] == ["dates-v1"]
    day = {"canonical_id": "atlas:x", "field": "release_date", "value": "2011-04-19"}
    assert feed_schema.check_emit("lock", day, "1.0") is day


# ---- federation (C5.6): 1.3 applies, 1.1 and 1.2 park, upgrade converges ----------------------------------

@pytest.fixture
def dates_net(tmp_path):
    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.3")
    mirror = Node(tmp_path, "mirror", "mirror")
    admin = {"Authorization": f"Bearer {_admin(authority)}"}

    def publish(title, ext=None, **extra):
        item = {"external_id": title, "title": title, **extra}
        if ext:
            item["ext"] = ext
        r = authority.client.post("/api/v2/observations", headers=admin, json={
            "connection": {"source_kind": "manual", "source": "manual", "source_scope": "net"},
            "observations": [item]})
        assert r.status_code == 200, r.text
        return r.json()["results"][0]["atlas"]["canonical_id"], authority.head()
    return authority, mirror, publish, admin


def view_of(node, cid):
    with node.st.maker() as s:
        row = s.get(Canonical, cid)
        return dict(row.view), (row.release_start, row.release_end, row.release_precision, row.year)


REGIONAL = release("2010", [entry("2011", "pc", "ww"), entry("2010-11", "pc", "ww", "early_access"),
                            entry("2011-03-01", "Switch", "jp", "port")])


def test_a_1_3_follower_applies_dates_and_matches_the_authority(dates_net):
    authority, mirror, publish, _ = dates_net
    cid, seq = publish("Regional", REGIONAL)
    with authority.st.maker() as s:
        assert s.get(ChangeLog, seq).body["_requires"] == ["dates-v1"]
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    assert view_of(mirror, cid) == view_of(authority, cid)
    view, cols = view_of(mirror, cid)
    # the source's own "first" (2010) agrees with its early-access entry (November 2010)
    assert view["first_release"] == {"date": "2010", "precision": "year"} and "release_date" not in view
    assert len(view["releases"]) == 3 and cols == (date(2010, 1, 1), date(2010, 12, 31), "year", 2010)


def test_a_1_3_follower_applies_a_pre_c5_free_text_lock(dates_net):
    """Before C5 a `release_date` lock took any text, and authorities signed it. A 1.3 follower that applies
    or replays such an entry stores it as the authority did (no quarantine, no divergence)."""
    authority, mirror, publish, _ = dates_net
    cid, _ = publish("Undated", release_date="2019-05-01")
    with authority.st.maker() as s:  # what a pre-C5 authority stored and signed
        s.add(Lock(canonical_id=cid, field="release_date", value="TBA", set_by="curator"))
        service.rebuild(s, authority.st.ctx, cid)
        s.commit()
    seq = authority.emit("lock", {"canonical_id": cid, "field": "release_date", "value": "TBA"})
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    assert mirror.heads(authority)["applied_head"] == seq
    with mirror.st.maker() as s:
        assert s.get(Lock, (cid, "release_date")).value == "TBA"
    assert view_of(mirror, cid) == view_of(authority, cid)
    view, cols = view_of(mirror, cid)
    assert view["release_date"] == "TBA" and "first_release" not in view and cols == (None, None, None, None)
    with mirror.st.maker() as s:  # a new local lock is still held to the date rules
        with pytest.raises(AtlasError, match="release_date"):
            service.set_lock(s, mirror.st.ctx, cid, "release_date", "soon", local_only=True)
        assert service.set_lock(s, mirror.st.ctx, cid, "release_date", "1937-12-21",
                                local_only=True).view["release_date"] == "1937-12-21"


def play_release(monkeypatch, protocol):
    """Play a follower of an older release for this node only: it does not know `dates-v1`, applies no `ext`
    member, has no date vocabularies and cannot lock `releases`. Everything else is this release's code,
    which is what 1.1 and 1.2 had for these paths (classify, park, replay)."""
    gone = ("date_precision", "release_category", "release_region")
    old = {k: v for k, v in feed_schema.vocabularies().items() if k not in gone}
    old["lock_field"] = frozenset(old["lock_field"]) - {"releases"}
    if protocol == "1.1":
        old["achievement_tier"] = frozenset(old["achievement_tier"]) - {"platinum"}
        monkeypatch.setattr(feed_schema, "VOCAB_TOKENS", ())
    monkeypatch.setattr(feed_schema, "vocabularies", lambda: old)
    monkeypatch.setattr(feed_schema, "CAPABILITIES", ("ext-v1",))
    monkeypatch.setattr(feed_schema, "EXT_APPLY", frozenset())
    monkeypatch.setattr(feed_schema, "PROTOCOL", protocol)
    monkeypatch.setattr(sync, "PROTOCOL", protocol)


@pytest.mark.parametrize("protocol", ["1.2", "1.1"])
def test_an_older_follower_parks_dates_keeps_its_record_and_converges_after_upgrade(dates_net, monkeypatch,
                                                                                    protocol):
    authority, mirror, publish, _ = dates_net
    cid, _ = publish("Regional", release_date="2011-04-19")
    pull(mirror, authority)
    before = view_of(mirror, cid)
    assert before[0]["release_date"] == "2011-04-19"
    _, dated_seq = publish("Regional", REGIONAL)  # the same record: a newer observation with partial dates
    later, _ = publish("Later Record")

    play_release(monkeypatch, protocol)
    pull(mirror, authority)
    assert mirror.failures(authority) == [(dated_seq, "obs", "parked", "unsupported_capability")]
    with mirror.st.maker() as s:
        assert s.get(SyncFailure, (authority.node_id, dated_seq)).detail["requires"] == ["dates-v1"]
        retained = {r.seq for r in s.scalars(select(ReplicatedChange))}
    assert retained == set(range(1, authority.head() + 1))  # verified and retained, not dropped
    status = mirror.heads(authority)
    assert status["state"] == "parked" and status["applied_head"] == dated_seq - 1
    assert view_of(mirror, cid) == before  # not stripped, not partial: the record is as it was
    assert "Later Record" not in mirror.titles()  # causally later: held back

    monkeypatch.undo()  # upgraded in place to this release
    with mirror.st.maker() as s:
        result = sync.replay(s, mirror.st.ctx)
        s.commit()
    assert result[authority.node_id]["state"] == "in_sync", result
    assert mirror.failures(authority) == []
    assert view_of(mirror, cid) == view_of(authority, cid)
    assert view_of(mirror, cid)[0]["first_release"] == {"date": "2010", "precision": "year"}
    assert view_of(mirror, later)[0]["title"] == "Later Record"


def test_an_older_follower_parks_a_partial_lock_and_a_releases_lock(dates_net, monkeypatch):
    authority, mirror, publish, admin = dates_net
    cid, _ = publish("Locked", release_date="2011-04-19")
    pull(mirror, authority)
    assert authority.client.put(f"/api/v2/games/{cid}/locks/release_date", headers=admin,
                                json={"value": "2011"}).status_code == 200
    first_lock = authority.head()
    assert authority.client.put(f"/api/v2/games/{cid}/locks/releases", headers=admin,
                                json={"value": [entry("2011", "pc")]}).status_code == 200
    play_release(monkeypatch, "1.2")
    pull(mirror, authority)
    # The first parked entry blocks the head; the releases lock behind it is verified, retained and held back.
    assert mirror.failures(authority) == [(first_lock, "lock", "parked", "unsupported_capability")]
    with mirror.st.maker() as s:
        assert s.get(SyncFailure, (authority.node_id, first_lock)).detail["requires"] == ["dates-v1"]
        assert {r.seq for r in s.scalars(select(ReplicatedChange))} == set(range(1, authority.head() + 1))
        assert s.get(Lock, (cid, "release_date")) is None  # never stored as a day-only "2011"
        assert s.get(Lock, (cid, "releases")) is None
    assert view_of(mirror, cid)[0]["release_date"] == "2011-04-19"
    monkeypatch.undo()
    with mirror.st.maker() as s:
        sync.replay(s, mirror.st.ctx)
        s.commit()
    assert view_of(mirror, cid) == view_of(authority, cid)
    with mirror.st.maker() as s:
        assert s.get(Lock, (cid, "releases")).value == [entry("2011", "pc")]


def test_an_unknown_region_or_release_member_parks_on_this_release(dates_net):
    authority, mirror, _, _ = dates_net
    body = observation("Future", ext=release("2011", [entry("2011", "pc", "ww")]))
    body["_requires"] = ["dates-v1"]
    body["observation"]["payload"]["ext"]["release"]["entries"][0]["region"] = "mena"  # a future group
    seq = authority.emit("obs", body)
    body2 = observation("Future 2", ext=release("2011"))
    body2["_requires"] = ["dates-v1"]
    body2["observation"]["payload"]["ext"]["release"]["label"] = "x"  # a future member
    pull(mirror, authority)
    assert mirror.failures(authority) == [(seq, "obs", "parked", "unsupported_value")]
    with mirror.st.maker() as s:
        assert s.get(SyncFailure, (authority.node_id, seq)).detail["unknown"] == [
            "observation.payload.ext.release.entries[0].region=mena"]
    outcome = feed_schema.classify("obs", body2)
    assert (outcome.action, outcome.reason) == ("parked", "unsupported_field")
    assert outcome.detail["unknown"] == ["observation.payload.ext.release.label"]


# ---- providers --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "parsed"), [
    ("19 Apr, 2011", ("2011-04-19", "day")), ("Apr 19, 2011", ("2011-04-19", "day")),
    ("Apr 2011", ("2011-04", "month")), ("April 2011", ("2011-04", "month")), ("2011", ("2011", "year")),
    ("Q3 2025", None), ("Coming soon", None), ("TBA", None), ("", None), (None, None), ("1900", None),
])
def test_store_release_text(text, parsed):
    assert parse_release(text) == parsed
    assert parse_date(text) == (parsed[0] if parsed and parsed[1] == "day" else None)  # unchanged contract


def _steam(text, coming_soon=False):
    details = {"620": {"success": True, "data": {"type": "game", "name": "Portal 2",
                                                 "release_date": {"coming_soon": coming_soon, "date": text}}}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=details)))
    return Steam().fetch(client, "620").to_item()


def test_steam_keeps_the_precision_the_store_gives():
    assert _steam("19 Apr, 2011")["release_date"] == "2011-04-19" and "ext" not in _steam("19 Apr, 2011")
    month = _steam("Apr 2011")
    assert "release_date" not in month and month["ext"] == release("2011-04")
    assert _steam("2011")["ext"] == release("2011")
    assert "ext" not in _steam("Q3 2025") and "release_date" not in _steam("Q3 2025")
    assert "ext" not in _steam("Apr 2011", coming_soon=True)
    SourceObservationV1.model_validate(month)  # what the provider produces is a valid observation


IGDB_GAME = {"id": 72, "name": "Portal 2", "first_release_date": 1303171200, "release_dates": [
    {"category": 0, "date": 1303171200, "y": 2011, "m": 4, "region": 8, "platform": {"name": "PC"}},
    {"category": 0, "date": 1303257600, "y": 2011, "m": 4, "region": 8, "platform": {"name": "PC"}},  # later dup
    {"category": 1, "date": 1301616000, "y": 2011, "m": 4, "region": 1, "platform": {"name": "PlayStation 3"}},
    {"category": 2, "date": 1293840000, "y": 2011, "region": 5, "platform": {"name": "Xbox 360"}},
    {"category": 7, "date": 1293840000, "y": 2025, "region": 8, "platform": {"name": "Switch"}},  # a quarter
    {"category": 0, "date": 1303171200, "y": 2011, "m": 4, "region": 99, "platform": {"name": "Mac"}},
]}


def test_igdb_release_dates_become_entries_without_invented_precision():
    item = Igdb._result(IGDB_GAME).to_item()
    entries = item["ext"]["release"]["entries"]
    assert entries == [
        {"date": "2011", "precision": "year", "category": "full", "platform": "Xbox 360", "region": "jp"},
        {"date": "2011-04", "precision": "month", "category": "full", "platform": "PlayStation 3", "region": "eu"},
        {"date": "2011-04-19", "precision": "day", "category": "full", "platform": "Mac"},  # unknown region: none
        {"date": "2011-04-19", "precision": "day", "category": "full", "platform": "PC", "region": "ww"},
    ]
    assert item["ext"]["release"]["first"] == {"date": "2011", "precision": "year"}
    assert "release_date" not in item  # the earliest full release is year-only
    SourceObservationV1.model_validate(item)
    plain = Igdb._result({"id": 1, "name": "Old", "first_release_date": 1303171200}).to_item()
    assert plain["release_date"] == "2011-04-19" and "ext" not in plain


def test_igdb_reads_the_current_reference_fields_and_the_release_status():
    """IGDB v4 deprecated `category`/`region` for the `date_format`/`release_region` references; `status`
    tells early access and betas apart from the full release, so a beta never becomes the first release."""
    rows = [
        {"date_format": {"format": "YYYYMMMM"}, "y": 2019, "m": 6, "release_region": {"region": "worldwide"},
         "status": {"name": "Alpha"}, "platform": {"name": "PC"}},
        {"date_format": {"format": "YYYYMMMMDD"}, "date": 1575158400, "y": 2019, "m": 12,
         "release_region": {"region": "worldwide"}, "status": {"name": "Early Access"}, "platform": {"name": "PC"}},
        {"date_format": {"format": "YYYY"}, "y": 2021, "release_region": {"region": "north_america"},
         "status": {"name": "Full Release"}, "platform": {"name": "PC"}},
        {"date_format": {"format": "YYYYQ3"}, "y": 2025, "status": {"name": "Full Release"}},  # a quarter: unknown
        {"date_format": {"format": "YYYY"}, "y": 2022, "status": {"name": "Cancelled"}, "platform": {"name": "Wii U"}},
        # a row that still has only the deprecated enums
        {"category": 1, "y": 2021, "m": 3, "region": 5, "platform": {"name": "Switch"}},
    ]
    item = Igdb._result({"id": 9, "name": "Status Test", "first_release_date": 1559347200,
                         "release_dates": rows}).to_item()
    release = item["ext"]["release"]
    assert release["entries"] == [
        {"date": "2019-06", "precision": "month", "category": "beta", "platform": "PC", "region": "ww"},
        {"date": "2019-12-01", "precision": "day", "category": "early_access", "platform": "PC", "region": "ww"},
        {"date": "2021", "precision": "year", "category": "full", "platform": "PC", "region": "na"},
        {"date": "2021-03", "precision": "month", "category": "full", "platform": "Switch", "region": "jp"},
    ]
    # the first release is the early access, not the earlier alpha (C5.1); it is a day, so release_date has it
    assert item["release_date"] == "2019-12-01" and release["first"] == {"date": "2019-12-01", "precision": "day"}
    SourceObservationV1.model_validate(item)
    # five betas never push the full release out
    many = [{"date_format": {"format": "YYYY"}, "y": 2010 + i, "status": {"name": "Beta"},
             "platform": {"name": f"P{i}"}} for i in range(6)]
    many.append({"date_format": {"format": "YYYY"}, "y": 2020, "platform": {"name": "PC"}})
    got = Igdb._result({"id": 10, "name": "Many", "release_dates": many}).to_item()
    entries = got["ext"]["release"]["entries"]
    assert len(entries) == 5 and entries[-1] == {"date": "2020", "precision": "year", "category": "full",
                                                  "platform": "PC"}
    assert got["ext"]["release"]["first"] == {"date": "2020", "precision": "year"}


def test_release_ext_only_adds_what_the_day_key_cannot_say():
    assert parse_release("21 Dec, 1937") == ("1937-12-21", "day")  # a legacy day keeps any year
    assert parse_release("1937") is None  # a partial date uses the C5.1 range
    assert release_ext(("1937-12-21", "day")) == ("1937-12-21", None)
    assert release_ext(("2011-04-19", "day")) == ("2011-04-19", None)
    assert release_ext(("2011", "year")) == (None, release("2011")["release"])
    assert release_ext(None) == (None, None)
    day, ext = release_ext(None, [entry("2011-04", "pc")])
    assert day is None and ext == release("2011-04", [entry("2011-04", "pc")])["release"]


# ---- legacy import ----------------------------------------------------------------------------------------

def test_legacy_import_keeps_a_release_year_as_a_year(tmp_path, session):
    legacy = tmp_path / "legacy.db"
    c = sqlite3.connect(legacy)
    c.executescript("""
      CREATE TABLE games (id INTEGER PRIMARY KEY, canonical_id TEXT, title TEXT, developer TEXT, description TEXT,
                          release_year INTEGER);
      CREATE TABLE metadata_providers (id INTEGER PRIMARY KEY, slug TEXT);
      CREATE TABLE game_external_ids (id INTEGER PRIMARY KEY, game_id INT, provider_id INT, external_id TEXT);
      CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
      CREATE TABLE game_tags (id INTEGER PRIMARY KEY, game_id INT, tag_id INT);
      INSERT INTO games VALUES (1,'gm_a','Hades','Supergiant','',2020),(2,'gm_b','Undated','x','',NULL),
                               (3,'gm_c','Ancient','x','',1890);
    """)
    c.commit()
    c.close()
    ctx = Ctx(build_settings(), load_identity(session))
    import_legacy(session, ctx, str(legacy))
    session.commit()
    rows = {r.view["title"]: r for r in session.scalars(select(Canonical))}
    assert rows["Hades"].view["first_release"] == {"date": "2020", "precision": "year"}
    assert "release_date" not in rows["Hades"].view and rows["Hades"].release_precision == "year"
    assert "first_release" not in rows["Undated"].view
    assert "first_release" not in rows["Ancient"].view  # outside the release year range: not invented


# ---- website: the editor, the record page and the search filter -------------------------------------------

def _site(tmp_path, role, **settings):
    s = build_settings(role=role, database_url=f"sqlite:///{tmp_path}/{role}.db", media_dir=str(tmp_path / "media"),
                       media_cache_enabled=False, public_url="http://localhost:8000", **settings)
    app = main.create_app(s)
    st = app.state.atlas
    with st.maker() as sess:
        [r] = service.ingest(sess, st.ctx, batch("igdb", [{"external_id": "9", "title": "Lighthouse",
                                                           "release_date": "2011-04-19"}]), system=True)
        sess.commit()
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        _wait_for_startup_replay(st)
        c.gid, c.st = r.canonical_id, st
        yield c


@pytest.fixture
def local_site(tmp_path):
    yield from _site(tmp_path, "local")


@pytest.fixture
def gated_site(tmp_path):
    yield from _site(tmp_path, "authority")


def _form(t, view, rows=(), **extra):
    data = {"csrf": t, "title": view.get("title", ""), "release_date": "", "developer": "", "publisher": "",
            "series": "", "description": "", "platforms": "", "tags": "", "features": "", "note": ""}
    data.update(extra)
    for i, row in enumerate(rows):
        for k, v in row.items():
            data[f"rel_{k}.{i}"] = v
    return data


def test_editor_saves_a_partial_date_and_release_rows(local_site):
    site = local_site
    signup(site, "alice")  # the first account administers this node: its edits apply at once
    url = f"/game/{site.gid}/edit"
    page = site.get(url).text
    assert 'name="rel_date.4"' in page and 'name="rel_date.5"' not in page  # exactly five rows
    assert 'value="2011-04-19"' in page and "This node keeps partial" not in page
    rows = [{"date": "2011-04", "platform": "PS5", "region": "eu", "category": "full"},
            {"date": "2010", "platform": "pc", "region": "ww", "category": "early_access"}]
    # A different year from the provider's 2011-04-19: community (85) outweighs igdb (70) on the year, and no
    # source states a month of 2010, so the first release is the year. (A contributed "2011" would agree with
    # the provider's day and the day would stay: C5.2 never lets a less precise date outvote one it agrees with.)
    done = site.post(url, data=_form(token(site, url), {"title": "Lighthouse"}, rows, release_date="2010"))
    assert done.status_code == 303 and "m=applied" in done.headers["location"], done.text
    with site.st.maker() as s:
        row = s.get(Canonical, site.gid)
        assert row.view["first_release"] == {"date": "2010", "precision": "year"}
        assert "release_date" not in row.view and row.release_precision == "year"
        assert [r["date"] for r in row.view["releases"]] == ["2010", "2011-04"]
        community = s.scalars(select(Observation).where(Observation.source == "community")).one()
        assert "release_date" not in community.payload
        assert community.payload["ext"]["release"]["first"] == {"date": "2010", "precision": "year"}
    page = unescape(site.get(f"/game/{site.gid}").text)
    assert '<time datetime="2010" data-precision="year">2010</time>' in page
    assert "<h3>Release dates</h3>" in page and "April 2011" in page and "Early access" in page
    assert "Europe" in page and "Worldwide" in page
    again = site.get(url).text
    assert 'name="release_date" value="2010"' in again and 'value="PS5"' in again  # rows come back


@pytest.mark.parametrize(("rows", "release_date", "message"), [
    ([{"date": "2023-02-29", "platform": "pc", "category": "full"}], "", "2023-02-29 is not a calendar date"),
    ([{"date": "2011", "region": "Europe", "category": "full"}], "", "region is"),
    ([{"date": "", "platform": "pc", "category": "full"}], "", "a release date is"),
    ([{"date": "2011", "platform": "pc", "category": "full"}, {"date": "2012", "platform": "PC", "category": "full"}],
     "", "repeats platform"),
    ([], "2011-13", "2011-13 has no month 13"),  # §7.2: "2011-4" is now read as April 2011
])
def test_editor_reports_invalid_rows_and_keeps_the_input(local_site, rows, release_date, message):
    site = local_site
    signup(site, "alice")
    url = f"/game/{site.gid}/edit"
    r = site.post(url, data=_form(token(site, url), {"title": "Lighthouse"}, rows, release_date=release_date))
    text = unescape(r.text)
    assert r.status_code == 400 and message in text, text[:2000]
    for row in rows:
        if row.get("date"):
            assert f'value="{row["date"]}"' in text
    with site.st.maker() as s:
        assert s.scalars(select(Proposal)).first() is None
        assert s.get(Canonical, site.gid).view["release_date"] == "2011-04-19"


def test_editor_on_a_gated_authority_says_dates_stay_local(gated_site):
    site = gated_site
    signup(site, "alice")
    page = site.get(f"/game/{site.gid}/edit").text
    # SPEC-UI-POLISH-PASS §3.6 (D5): the gate is unchanged; the copy names the release, not a protocol number
    assert "does not publish them to its followers until every follower runs this Atlas release" in page
    assert "dates saved before then are not published later" in page


def test_lock_form_accepts_release_lines(local_site):
    site = local_site
    signup(site, "alice")
    lines = "2011-04-19 | pc | ww | | full\n2011 | Switch | jp | Deluxe | port"
    r = site.post(f"/game/{site.gid}/lock", data={"csrf": token(site, f"/game/{site.gid}"), "field": "releases",
                                                  "value": lines})
    assert r.status_code == 303, r.text
    with site.st.maker() as s:
        assert s.get(Lock, (site.gid, "releases")).value == [
            entry("2011-04-19", "pc", "ww"), entry("2011", "Switch", "jp", "port", "Deluxe")]


def test_search_page_filters_and_sorts(local_site):
    site = local_site
    with site.st.maker() as s:
        for title, ext in (("Alpha Year", release("2011")), ("Beta Month", release("2011-04")),
                           ("Gamma Later", release("2013-01"))):
            service.ingest(s, site.st.ctx, batch("igdb", [{"external_id": title, "title": title, "ext": ext}]),
                           system=True)
        s.commit()
    page = unescape(site.get("/search", params={"released_from": "2011", "released_to": "2011",
                                                "sort": "release"}).text)
    order = [page.index(t) for t in ("Alpha Year", "Beta Month", "Lighthouse")]
    assert order == sorted(order) and "Gamma Later" not in page
    assert 'name="released_from" value="2011"' in page and 'value="release" selected' in page
    month = unescape(site.get("/search", params={"released_from": "2011-04", "released_to": "2011-04"}).text)
    assert "Beta Month" in month and "Lighthouse" in month and "Alpha Year" not in month
    loose = unescape(site.get("/search", params={"released_from": "2011-4", "released_to": "2011-4"}).text)
    assert "Beta Month" in loose and "Alpha Year" not in loose  # §7.2: read as April 2011
    bad = site.get("/search", params={"released_from": "13/2011"})
    assert bad.status_code == 400 and "released_from: " in unescape(bad.text)


# ---- the resumable rebuild --------------------------------------------------------------------------------

def test_rebuild_is_resumable_and_only_bumps_changed_records(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path}/rebuild.db"
    monkeypatch.setenv("ATLAS_DATABASE_URL", url)
    monkeypatch.setenv("ATLAS_ENVIRONMENT", "dev")
    app = main.create_app(build_settings(database_url=url))
    st = app.state.atlas
    with st.maker() as s:
        for i in range(5):
            service.ingest(s, st.ctx, batch("igdb", [{"external_id": str(i), "title": f"Game {i}",
                                                      "release_date": f"201{i}-01-02"}]), system=True)
        s.commit()
        ids = sorted(s.scalars(select(Canonical.id)))
        # play a record derived by the previous release: a stale view the new merge would change
        stale = s.get(Canonical, ids[1])
        stale.view = {k: v for k, v in stale.view.items() if k not in ("first_release", "release_year")}
        stale.release_start = stale.release_end = stale.release_precision = None
        s.commit()
        versions = {cid: s.get(Canonical, cid).version for cid in ids}
    first = dates_rebuild.run(st.maker, st.ctx, batch=2, limit=2)
    assert first == {"rebuilt": 2, "changed": 1, "finished": False, "cursor": ids[1]}
    with st.maker() as s:
        assert s.get(NodeState, dates_rebuild.CURSOR).value == ids[1]
        assert s.get(Canonical, ids[1]).release_precision == "day"
        assert s.get(Canonical, ids[1]).version == versions[ids[1]] + 1
        assert s.get(Canonical, ids[0]).version == versions[ids[0]]  # unchanged records are not bumped
    assert cli.main(["dates", "rebuild", "--batch", "2"]) == 0
    assert json.loads(capsys.readouterr().out) == {"rebuilt": 3, "changed": 0, "finished": True}
    with st.maker() as s:
        assert s.get(NodeState, dates_rebuild.CURSOR) is None  # finished: the next run starts over
        assert {cid: s.get(Canonical, cid).version for cid in ids} == {**versions, ids[1]: versions[ids[1]] + 1}


# ---- backups carry the date columns -----------------------------------------------------------------------

def test_backup_and_restore_keep_the_release_columns(tmp_path):
    from atlas import backup, db

    def node(name):
        url = f"sqlite:///{tmp_path}/{name}.db"
        db.upgrade(url)
        settings = build_settings(database_url=url, media_dir=str(tmp_path / f"{name}-media"), media_cache_enabled=False)
        return settings, db.make_engine(url)

    settings, engine = node("source")
    maker = db.make_sessionmaker(engine)
    with maker() as s:
        ctx = Ctx(settings, load_identity(s))
        s.commit()
        [r] = ingest(s, ctx, "igdb", [{"external_id": "1", "title": "Backed Up", "ext": release("2024-02")}])
    archive = tmp_path / "a.tar.gz"
    backup.create(settings, engine, archive)
    target_settings, target = node("target")
    backup.restore(target_settings, target, archive, force=True)
    with db.make_sessionmaker(target)() as s:
        row = s.get(Canonical, r.canonical_id)
        assert (row.release_start, row.release_end, row.release_precision) == (date(2024, 2, 1), date(2024, 2, 29),
                                                                               "month")
        assert row.view["first_release"] == {"date": "2024-02", "precision": "month"}
