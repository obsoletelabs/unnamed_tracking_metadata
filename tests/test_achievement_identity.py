"""Stable achievement identity (CONTRACTS-v1 C2): rows are reconciled in place, keys are stored and survive
refreshes, renames, reorders, merges, mirrors and paging, and hidden entries never leak through their keys."""

from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc, select, text

from atlas import facets, feed, main, service, sync
from atlas.achievement_key import ak1, identity, is_key
from atlas.models import Achievement, ChangeLog
from atlas.service import AtlasError, Ctx

from .conftest import batch, build_settings
from .test_postgres import pg, pg_url  # noqa: F401
from .test_sync import curate, pair  # noqa: F401
from .test_web import site  # noqa: F401


def seed(session, ctx, ext="620", title="Portal 2"):
    [r] = service.ingest(session, ctx, batch("steam", [{"external_id": ext, "title": title,
                                                        "release_date": "2011-04-19"}]), system=True)
    session.commit()
    return r.canonical_id


def stored(session, cid, source=None):
    """name -> (id, key), read straight from the database (not from the ORM identity map)."""
    session.expire_all()
    sql = "SELECT id, key, name FROM achievement WHERE canonical_id = :c" + (" AND source = :s" if source else "")
    return {r.name: (r.id, r.key) for r in session.execute(text(sql), {"c": cid, "s": source})}


STEAM = [{"name": "Winner", "external_id": "ACH_WIN", "description": "Win once"},
         {"name": "Collector", "external_id": "ACH_COLLECT"},
         {"name": "Secret Ending", "external_id": "ACH_SECRET", "hidden": True, "description": "The cake"}]


def test_key_is_the_contract_digest():
    ident = "steam:ACH_WIN_ONE_GAME"
    expected = "ak1:" + base64.b32encode(hashlib.sha256(ident.encode()).digest()).decode().lower()[:26]
    assert ak1(ident) == expected and is_key(expected) and len(expected) == 30
    assert identity("Steam", "ACH_1", "x") == "steam:ACH_1" and identity("curator", "", "big win") == "curator~big win"
    assert not is_key("ak1:" + "1" * 26) and not is_key("ak1:abc") and not is_key(None)


def test_refreshes_keep_ids_and_keys_in_the_database(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", STEAM)
    session.commit()
    first = stored(session, cid)
    assert first["Winner"][1] == ak1("steam:ACH_WIN")
    for _ in range(3):  # repeated identical refreshes change nothing
        facets.set_achievements(session, ctx, cid, "steam", STEAM)
        session.commit()
        assert stored(session, cid) == first

    # the provider renames one entry (same external id), reorders, drops one and adds one
    edited = [{"name": "Big Collector", "external_id": "ACH_COLLECT", "description": "now more"},
              {"name": "Newcomer", "external_id": "ACH_NEW"}, STEAM[0]]
    facets.set_achievements(session, ctx, cid, "steam", edited)
    session.commit()
    after = stored(session, cid)
    assert after["Big Collector"] == first["Collector"] and after["Winner"] == first["Winner"]
    assert "Secret Ending" not in after and after["Newcomer"][0] not in {v[0] for v in first.values()}
    assert after["Newcomer"][1] == ak1("steam:ACH_NEW")
    row = session.get(Achievement, first["Collector"][0])
    assert (row.description, row.norm_name) == ("now more", "big collector")  # updated in place

    # a removed entry that comes back gets its derived key again (stable across remove/add)
    facets.set_achievements(session, ctx, cid, "steam", STEAM)
    session.commit()
    again = stored(session, cid)
    assert again["Secret Ending"][1] == first["Secret Ending"][1] and again["Winner"] == first["Winner"]


def test_editor_round_trip_rename_keeps_id_and_key(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Escape"}, {"name": "Survive"}], ordered=True)
    session.commit()
    first = stored(session, cid, "curator")
    assert first["Escape"][1] == ak1("curator~escape")
    items = facets.achievement_items(list(session.scalars(select(Achievement).where(
        Achievement.canonical_id == cid).order_by(Achievement.position))))
    assert [i["key"] for i in items] == [first["Escape"][1], first["Survive"][1]]
    items[0]["name"] = "Escape the House"  # rename through the key, and move it to the end
    facets.set_achievements(session, ctx, cid, "curator", [items[1], items[0]], ordered=True)
    session.commit()
    after = stored(session, cid, "curator")
    assert after["Escape the House"] == first["Escape"] and after["Survive"] == first["Survive"]
    assert session.get(Achievement, first["Escape"][0]).position == 1
    # the editor may send the row id instead of the key
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Survive", "id": first["Survive"][0]},
                                                           {"name": "Escape!", "id": first["Escape"][0]}])
    session.commit()
    assert stored(session, cid, "curator") == {"Survive": first["Survive"], "Escape!": first["Escape"]}


def test_rename_swap_does_not_trip_the_unique_constraints(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Alpha"}, {"name": "Beta"}])
    session.commit()
    first = stored(session, cid)
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Beta", "key": first["Alpha"][1]},
                                                           {"name": "Alpha", "key": first["Beta"][1]}])
    session.commit()
    assert stored(session, cid) == {"Beta": first["Alpha"], "Alpha": first["Beta"]}


def test_hidden_entries_expose_keys_but_no_spoiler(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", STEAM)
    session.commit()
    secret_key = stored(session, cid)["Secret Ending"][1]
    hidden = next(m for m in facets.merged_achievements(session, ctx, cid) if m["hidden"])
    assert hidden["key"] == secret_key and hidden["keys"] == [secret_key] and hidden["key_identity"] is None
    assert "ACH_SECRET" not in str(hidden) and "Secret" not in str(hidden) and "cake" not in str(hidden)
    shown = next(m for m in facets.merged_achievements(session, ctx, cid) if m["name"] == "Winner")
    assert shown["key_identity"] == "steam:ACH_WIN"
    by_key = next(m for m in facets.merged_achievements(session, ctx, cid, reveal={secret_key}) if m["hidden"])
    assert by_key["revealed"] and by_key["name"] == "Secret Ending" and by_key["key_identity"] == "steam:ACH_SECRET"


def test_merged_anchor_is_min_of_keys_whatever_the_trust(session, ctx):
    def read(trust):
        s = build_settings(source_trust=trust)
        return {m["name"]: m for m in facets.merged_achievements(session, Ctx(s, ctx.identity), cid, reveal=True)}

    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Winner", "external_id": "ACH_WIN"}])
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "winner", "points": 5}])
    session.commit()
    keys = sorted([ak1("steam:ACH_WIN"), ak1("curator~winner")])
    a, b = read({"steam": 90, "curator": 10})["Winner"], read({"steam": 10, "curator": 90})["winner"]
    assert a["keys"] == b["keys"] == keys and a["key"] == b["key"] == keys[0]
    assert a["id"] != b["id"]  # `id` still follows trust; `key` does not


def test_merging_records_keeps_keys(session, ctx):
    one, two = seed(session, ctx), seed(session, ctx, ext="400", title="Portal")
    facets.set_achievements(session, ctx, one, "steam", [{"name": "Winner", "external_id": "ACH_WIN"}])
    facets.set_achievements(session, ctx, two, "steam", [{"name": "Winner", "external_id": "ACH_WIN"},
                                                         {"name": "Portal Only", "external_id": "ACH_P"}])
    facets.set_achievements(session, ctx, two, "curator", [{"name": "Speedrun"}])
    session.commit()
    before = {**stored(session, two), "Winner(dst)": stored(session, one)["Winner"]}
    service.merge_canonicals(session, ctx, two, one, actor="test")
    session.commit()
    after = stored(session, one)
    assert after["Winner"] == before["Winner(dst)"]  # duplicate: the survivor's row stays
    assert after["Portal Only"] == before["Portal Only"] and after["Speedrun"] == before["Speedrun"]
    assert stored(session, two) == {}
    facets.set_achievements(session, ctx, one, "steam", [{"name": "Winner", "external_id": "ACH_WIN"},
                                                         {"name": "Portal Only", "external_id": "ACH_P"}])
    session.commit()
    assert stored(session, one) == after  # and a refresh after the merge keeps them too


def test_unique_key_index_holds(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", STEAM[:2])
    session.commit()
    a, b = session.scalars(select(Achievement).order_by(Achievement.name)).all()
    b.key = a.key
    with pytest.raises(exc.IntegrityError):
        session.flush()
    session.rollback()


def test_repeated_external_id_and_foreign_keys_get_distinct_keys(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Winner", "external_id": "ACH_WIN"}])
    steam_key = stored(session, cid)["Winner"][1]
    facets.set_achievements(session, ctx, cid, "curator", [
        {"name": "One", "external_id": "X"}, {"name": "Two", "external_id": "X"},
        {"name": "Thief", "key": steam_key},  # another source's key is not taken over
        {"name": "Dup A", "key": "ak1:" + "a" * 26}, {"name": "Dup B", "key": "ak1:" + "a" * 26}])
    session.commit()
    got = stored(session, cid, "curator")
    assert got["One"][1] == ak1("curator:X") and got["Two"][1] == ak1("curator~two")
    assert got["Thief"][1] == ak1("curator~thief") and stored(session, cid)["Winner"][1] == steam_key
    assert got["Dup A"][1] == "ak1:" + "a" * 26 and got["Dup B"][1] == ak1("curator~dup b")
    with pytest.raises(AtlasError, match="key"):
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "Bad", "key": "steam:ACH_WIN"}])


def test_repeated_external_id_refreshes_keep_each_name_on_its_row(session, ctx):
    cid = seed(session, ctx)
    dup = [{"name": "Winner", "external_id": "X"}, {"name": "Winner Again", "external_id": "X"},
           {"name": "Other", "external_id": "Y"}]
    facets.set_achievements(session, ctx, cid, "steam", dup)
    session.commit()
    first = stored(session, cid)
    assert first["Winner"][1] == ak1("steam:X") and first["Winner Again"][1] == ak1("steam~winner again")
    for order in [dup, dup, dup[::-1], [dup[1], dup[2], dup[0]], dup]:  # same order and reordered
        facets.set_achievements(session, ctx, cid, "steam", order)
        session.commit()
        assert stored(session, cid) == first


def test_repeated_external_id_rows_backfilled_in_the_other_order_stay_put(session, ctx):
    # migration 0022 hands the ext-id key to the lower row id, which need not be the first name in the list
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Winner Again", "external_id": "X"},
                                                         {"name": "Winner", "external_id": "X"}])
    session.commit()
    first = stored(session, cid)
    assert first["Winner Again"][1] == ak1("steam:X")
    for _ in range(5):
        facets.set_achievements(session, ctx, cid, "steam", [{"name": "Winner", "external_id": "X"},
                                                             {"name": "Winner Again", "external_id": "X"}])
        session.commit()
        assert stored(session, cid) == first


def test_merge_keeps_a_curated_row_whose_key_another_source_holds(session, ctx):
    one, two = seed(session, ctx), seed(session, ctx, ext="400", title="Portal")
    facets.set_achievements(session, ctx, one, "steam", [{"name": "Winner", "external_id": "ACH_WIN"}])
    session.commit()
    steam_key = stored(session, one)["Winner"][1]
    facets.set_achievements(session, ctx, two, "curator", [{"name": "Curated Only", "key": steam_key}])
    facets.set_achievements(session, ctx, two, "steam", [{"name": "Winner", "external_id": "ACH_WIN"}])
    session.commit()
    curated_id = stored(session, two)["Curated Only"][0]
    assert stored(session, two)["Curated Only"][1] == steam_key
    service.merge_canonicals(session, ctx, two, one, actor="test")
    session.commit()
    after = stored(session, one)
    assert after["Winner"][1] == steam_key  # the survivor's steam row stays; the moving steam duplicate goes
    assert after["Curated Only"] == (curated_id, ak1("curator~curated only"))  # kept, re-keyed, same id
    assert len(after) == 2
    row = session.get(Achievement, curated_id)
    assert (row.source, row.canonical_id) == ("curator", one)


def test_detach_moves_rows_with_their_keys(session, ctx):
    cid = seed(session, ctx)
    service.ingest(session, ctx, batch("gog", [{"external_id": "g1", "title": "Portal 2",
                                               "release_date": "2011-04-19"}]), system=True)
    session.commit()
    facets.set_achievements(session, ctx, cid, "gog", [{"name": "Winner", "external_id": "G_WIN"}])
    session.commit()
    before = stored(session, cid, "gog")
    from atlas.models import Observation
    obs = session.scalars(select(Observation).where(Observation.source == "gog")).one()
    if obs.canonical_id != cid:
        pytest.skip("the gog observation did not join the same record")
    new = service.detach_observation(session, ctx, obs.id)
    session.commit()
    assert stored(session, new.id, "gog") == before


# ---- federation ------------------------------------------------------------------------------------------

def test_feed_carries_keys_and_mirror_agrees_with_authority(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [r] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Hades", "release_date": "2020-09-17"}])
    cid = r.canonical_id
    with a_maker() as s:
        facets.set_achievements(s, a_ctx, cid, "curator", [{"name": "Escape"}, {"name": "Fish"}],
                                redistributable=True, ordered=True)
        s.commit()
        authority = stored(s, cid)
        entry = s.scalars(select(ChangeLog).where(ChangeLog.kind == "achievements")).one()
        assert [i["key"] for i in entry.body["items"]] == [authority["Escape"][1], authority["Fish"][1]]
        assert all("id" not in i for i in entry.body["items"])
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        mirror = stored(s, cid)
        assert {k: v[1] for k, v in mirror.items()} == {k: v[1] for k, v in authority.items()}

    # an editor rename on the authority keeps a key the mirror could not derive from the new name
    with a_maker() as s:
        facets.set_achievements(s, a_ctx, cid, "curator", [{"name": "Escape the House", "key": authority["Escape"][1]},
                                                           {"name": "Fish"}], redistributable=True, ordered=True)
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert stored(s, cid) == {"Escape the House": mirror["Escape"], "Fish": mirror["Fish"]}  # ids kept too
        [m] = [x for x in facets.merged_achievements(s, m_ctx, cid) if x["name"] == "Escape the House"]
        assert m["key"] == authority["Escape"][1]


def test_mirror_adopts_the_authority_key_for_rows_it_derived_itself(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [r] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Hades", "release_date": "2020-09-17"}])
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    cid = r.canonical_id
    with a_maker() as s:  # a pre-key authority entry: items without key (as a 1.0 authority signs them)
        feed.append(s, a_ctx.identity, "achievements", {"canonical_id": cid, "source": "curator",
                                                        "items": [{"name": "Escape"}]})
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        [(rid, key)] = stored(s, cid).values()
        assert key == ak1("curator~escape")  # derived, identical to what the authority derives
    authority_key = "ak1:" + "b" * 26
    with a_maker() as s:
        feed.append(s, a_ctx.identity, "achievements", {"canonical_id": cid, "source": "curator",
                                                        "items": [{"name": "Escape", "key": authority_key}]})
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert stored(s, cid) == {"Escape": (rid, authority_key)}


def test_mirror_backfilled_in_another_order_adopts_the_authority_keys(pair):  # noqa: F811
    a_maker, a_ctx, m_maker, m_ctx, client = pair
    [r] = curate(a_maker, a_ctx, "curator", [{"external_id": "c1", "title": "Hades", "release_date": "2020-09-17"}])
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    cid = r.canonical_id
    k_ext, k_name = ak1("curator:X"), ak1("curator~winner again")
    with m_maker() as s:  # the mirror's own backfill gave the two duplicate-id rows the other keys
        facets.set_achievements(s, m_ctx, cid, "curator", [{"name": "Winner Again", "external_id": "X"},
                                                           {"name": "Winner", "external_id": "X"}])
        s.commit()
        before = stored(s, cid)
        assert before["Winner Again"][1] == k_ext
    with a_maker() as s:
        feed.append(s, a_ctx.identity, "achievements", {"canonical_id": cid, "source": "curator", "items": [
            {"name": "Winner", "external_id": "X", "key": k_ext},
            {"name": "Winner Again", "external_id": "X", "key": k_name}]})
        s.commit()
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:  # the authority's key is the identity (matched first): mirror and authority agree
        # again, and each mirror row id follows its key, as the mirror's own clients saw them paired
        assert stored(s, cid) == {"Winner": (before["Winner Again"][0], k_ext),
                                  "Winner Again": (before["Winner"][0], k_name)}
        merged = {m["name"]: m["key"] for m in facets.merged_achievements(s, m_ctx, cid)}
        assert merged == {"Winner": k_ext, "Winner Again": k_name}
    with m_maker() as s:  # and a repeat of the same signed entry changes nothing
        again = stored(s, cid)
    sync.pull_all(m_maker, m_ctx, client, "http://upstream", None)
    with m_maker() as s:
        assert stored(s, cid) == again


# ---- HTTP API ---------------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40,
                       media_cache_enabled=False)
    app = main.create_app(s)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer " + "m" * 40
        c.st = app.state.atlas
        body = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "ops"},
                "observations": [{"external_id": "1", "title": "Hades", "release_date": "2020-09-17"}]}
        c.cid = c.post("/api/v2/observations", json=body).json()["results"][0]["atlas"]["canonical_id"]
        yield c


def pages(api, limit=200, reveal=None):
    out, offset = [], 0
    while offset is not None:
        q = f"limit={limit}&offset={offset}" + (f"&reveal={reveal}" if reveal else "")
        page = api.get(f"/api/v2/games/{api.cid}/achievements?{q}").json()
        out += page["results"]
        offset = page["next_offset"]
    return out


def test_api_fields_and_ids_survive_repeated_puts_across_pages(api):
    items = [{"name": f"Achievement {n:04d}", "external_id": f"ACH_{n}"} for n in range(450)]
    items[7]["hidden"] = True
    url = f"/api/v2/games/{api.cid}/achievements"
    assert api.put(url, json={"items": items}).json() == {"count": 450}
    first = pages(api)
    assert len(first) == 450 and len({e["key"] for e in first}) == 450
    for e in first:
        assert is_key(e["key"]) and e["keys"] == [e["key"]]
    by_name = {e["name"]: e for e in first if not e["hidden"]}
    assert by_name["Achievement 0001"]["key_identity"] == "curator:ACH_1"
    [secret] = [e for e in first if e["hidden"]]
    assert secret["key_identity"] is None and "ACH_7" not in str(secret) and secret["key"] == ak1("curator:ACH_7")

    # edit repeatedly: rename (same external id), reorder, drop the last 50, add 10, change descriptions
    edited = [dict(i) for i in reversed(items[:400])]
    edited[0]["name"] = "Renamed 0399"
    edited += [{"name": f"Fresh {n}", "external_id": f"NEW_{n}"} for n in range(10)]
    for round_ in range(2):
        for i in edited:
            i["description"] = f"round {round_}"
        assert api.put(url, json={"items": edited}).json() == {"count": 410}
    after = pages(api, limit=150)
    assert len(after) == 410 and len({e["id"] for e in after}) == 410
    old = {e["key"]: e["id"] for e in first}
    kept = [e for e in after if e["key"] in old]
    assert len(kept) == 400 and all(old[e["key"]] == e["id"] for e in kept)  # same id for the same key
    renamed = next(e for e in after if e["name"] == "Renamed 0399")
    assert renamed["key"] == by_name["Achievement 0399"]["key"] and renamed["id"] == by_name["Achievement 0399"]["id"]
    with api.st.maker() as s:  # and the database agrees with the API
        rows = {r.key: r.id for r in s.scalars(select(Achievement).where(Achievement.canonical_id == api.cid))}
        assert rows == {e["key"]: e["id"] for e in after}
    # reveal accepts a key as well as an id
    shown = next(e for e in pages(api, reveal=secret["key"]) if e["hidden"])
    assert shown["revealed"] and shown["name"] == "Achievement 0007" and shown["key_identity"] == "curator:ACH_7"


def test_identity_longer_than_200_characters_still_has_a_30_character_key(api):
    long_ext = "X" * 200
    with api.st.maker() as s:  # the longest normalised source name plus the longest external id
        facets.set_achievements(s, api.st.ctx, api.cid, "p" * 60, [{"name": "Long", "external_id": long_ext}])
        s.commit()
    [e] = pages(api)
    assert e["key_identity"] == "p" * 50 + ":" + long_ext and len(e["key_identity"]) == 251
    assert len(e["key"]) == 30 and e["key"] == ak1(e["key_identity"])
    with api.st.maker() as s:
        assert s.scalars(select(Achievement.key)).one() == e["key"]


def test_api_rejects_malformed_keys(api):
    r = api.put(f"/api/v2/games/{api.cid}/achievements", json={"items": [{"name": "A", "key": "ak1:short"}]})
    assert r.status_code == 400 and "key" in r.text


# ---- website editor ---------------------------------------------------------------------------------------

def test_editor_rename_keeps_the_entry_and_shows_a_change(site):  # noqa: F811
    from html import unescape

    from .test_web import _ach_form, signup, token

    signup(site, "alice")  # administrator: applies at once
    url = f"/game/{site.gid}/achievements/edit"
    t = token(site, url)
    rows = {"a": {"name": "Escape", "description": "Leave"}, "b": {"name": "Secret", "hidden": "1"}}
    assert site.post(url, data=_ach_form(t, rows, action="submit")).status_code == 303
    with site.st.maker() as s:
        first = stored(s, site.gid)
    page = unescape(site.get(url).text)
    assert f'name="akey.r0" value="{first["Escape"][1]}"' in page  # the editor round-trips the stored key
    renamed = {"r0": {"name": "Escape the House", "description": "Leave", "akey": first["Escape"][1]},
               "r1": {"name": "Secret", "hidden": "1", "akey": first["Secret"][1]}}
    preview = unescape(site.post(url, data=_ach_form(t, renamed)).text)
    assert "Name: <del>Escape</del> → <ins>Escape the House</ins>" in preview
    assert "<ins>Add</ins>" not in preview and "<del>Remove</del>" not in preview
    assert site.post(url, data=_ach_form(t, renamed, action="submit")).status_code == 303
    with site.st.maker() as s:
        assert stored(s, site.gid) == {"Escape the House": first["Escape"], "Secret": first["Secret"]}
    hidden = site.get(f"/api/v2/games/{site.gid}/achievements", headers={"Authorization": "Bearer " + "b" * 40}
                      ).json()["results"][-1]
    assert hidden["hidden"] and hidden["key"] == first["Secret"][1] and hidden["key_identity"] is None


def test_reconcile_on_postgresql(pg):  # noqa: F811
    """Renames, swaps, removals and re-adds against PostgreSQL's immediate unique checks."""
    maker, ctx = pg
    with maker() as s:
        cid = seed(s, ctx)
        facets.set_achievements(s, ctx, cid, "curator", [{"name": "Alpha"}, {"name": "Beta"}, {"name": "Gamma"}])
        s.commit()
        first = stored(s, cid)
    with maker() as s:
        facets.set_achievements(s, ctx, cid, "curator", [{"name": "Beta", "key": first["Alpha"][1]},
                                                         {"name": "Alpha", "key": first["Beta"][1]},
                                                         {"name": "Delta"}])
        s.commit()
    with maker() as s:
        after = stored(s, cid)
        assert after["Beta"] == first["Alpha"] and after["Alpha"] == first["Beta"] and "Gamma" not in after
        facets.set_achievements(s, ctx, cid, "curator", [{"name": "Gamma"}, {"name": "Alpha"}])
        s.commit()
        assert stored(s, cid)["Gamma"][1] == first["Gamma"][1] and stored(s, cid)["Alpha"] == first["Beta"]
