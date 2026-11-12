"""C12 achievements (A18): sets and DLC groups, same-name achievements (F1), retirement, source order, translations,
rarity with source and freshness, the spoiler rules, and the signed v1 copy. Every assertion that matters reads
the database or the public API; all names are invented."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from atlas import facets, main, service
from atlas.achievement_key import ak1
from atlas.models import Achievement, AchievementSet, AchievementStat, AchievementText, ChangeLog
from atlas.service import AtlasError

from .conftest import batch, build_settings


def seed(session, ctx, ext="900", title="Lantern Bay"):
    [r] = service.ingest(session, ctx, batch("steam", [{"external_id": ext, "title": title}]), system=True)
    session.commit()
    return r.canonical_id


def rows(session, cid, source=None):
    session.expire_all()
    q = select(Achievement).where(Achievement.canonical_id == cid)
    if source:
        q = q.where(Achievement.source == source)
    return list(session.scalars(q.order_by(Achievement.id)))


TODAY = date.today()


# ---- sets, DLC and platforms (§12.1.1) ---------------------------------------------------------------------

def test_sets_group_entries_and_name_the_dlc_record(session, ctx):
    cid = seed(session, ctx)
    dlc = seed(session, ctx, "901", "Lantern Bay: Tides")
    facets.set_achievements(session, ctx, cid, "curator", [
        {"name": "Lamp Lit", "external_id": "A1"},
        {"name": "Tide Turner", "external_id": "T1", "set": "tides"},
        {"name": "Console Only", "external_id": "P1", "set": "ps"}],
        sets=[{"key": "tides", "kind": "dlc", "name": "Tides", "dlc": dlc},
              {"key": "ps", "kind": "platform", "name": "PlayStation", "platforms": ["ps5", "ps4"]}], ordered=True)
    session.commit()
    stored = {(s.set_key, s.kind, s.dlc_canonical_id, tuple(s.platforms or [])) for s in session.scalars(
        select(AchievementSet).where(AchievementSet.canonical_id == cid))}
    assert stored == {("tides", "dlc", dlc, ()), ("ps", "platform", None, ("ps4", "ps5"))}
    assert {a.name: a.set_key for a in rows(session, cid)} == {"Lamp Lit": "base", "Tide Turner": "tides",
                                                               "Console Only": "ps"}
    listing = facets.achievement_listing(session, ctx, cid)
    assert [s["id"] for s in listing["sets"]] == ["curator/base", "curator/tides", "curator/ps"]  # base, dlc, platform
    assert listing["sets"][0]["name"] == "Lantern Bay" and listing["sets"][1]["dlc"] == dlc
    assert [e["set"] for e in listing["results"]] == ["curator/base", "curator/tides", "curator/ps"]
    only = facets.achievement_listing(session, ctx, cid, set_filter="tides")["results"]
    assert [e["name"] for e in only] == ["Tide Turner"]
    pc = facets.achievement_listing(session, ctx, cid, platform="pc")["results"]
    assert "Console Only" not in {e["name"] for e in pc} and "Lamp Lit" in {e["name"] for e in pc}
    # the DLC's own record points back at the set that lists its achievements
    assert facets.sets_elsewhere(session, ctx, dlc) == [{"canonical_id": cid, "set": "curator/tides", "name": "Tides"}]


def test_same_set_from_two_sources_is_one_merged_set_and_a_merged_dlc_is_followed(session, ctx):
    cid = seed(session, ctx)
    dlc = seed(session, ctx, "901", "Tides")
    other = seed(session, ctx, "902", "Tides (duplicate)")
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Tide Turner", "set": "t"}],
                            sets=[{"key": "t", "kind": "dlc", "name": "Tides", "dlc": other}])
    facets.set_achievements(session, ctx, cid, "community", [{"name": "Tide Turner", "set": "tides-dlc"}],
                            sets=[{"key": "tides-dlc", "kind": "dlc", "name": "The Tides", "dlc": dlc}])
    session.commit()
    service.merge_canonicals(session, ctx, other, dlc, actor="cur")  # the duplicate DLC record is merged away
    session.commit()
    listing = facets.achievement_listing(session, ctx, cid)
    [tides] = [s for s in listing["sets"] if s["kind"] == "dlc"]
    assert tides["id"] == "community/tides-dlc" and tides["dlc"] == dlc  # smallest member id; the dlc followed
    assert tides["sources"] == ["community", "curator"] and tides["name"] == "Tides"  # name of the curator
    [entry] = listing["results"]
    assert sorted(entry["sources"]) == ["community", "curator"]  # one entry: the same name in the same merged set


def test_unknown_dlc_is_absent_and_invalid_sets_are_refused(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "A", "set": "d"}],
                            sets=[{"key": "d", "kind": "dlc", "name": "Elsewhere", "dlc": "atlas:unknownrecord"}])
    session.commit()
    [s] = [s for s in facets.achievement_listing(session, ctx, cid)["sets"] if s["kind"] == "dlc"]
    assert s["dlc"] is None  # a record this node does not know is never invented
    for sets, items, msg in (
            ([], [{"name": "A", "set": "nope"}], "unknown set"),
            ([{"key": "Bad Key", "kind": "dlc"}], [], "set key"),
            ([{"key": "x", "kind": "edition", "dlc": "atlas:1"}], [], "only a dlc"),
            ([{"key": "base", "kind": "dlc"}], [], "only the set `base`"),
            ([{"key": "x", "kind": "platform", "platforms": ["PS 5"]}], [], "platforms"),
            ([{"key": "x", "kind": "other"}] * 2, [], "listed twice")):
        with pytest.raises(AtlasError, match=msg):
            facets.set_achievements(session, ctx, cid, "curator", items, sets=sets)
    with pytest.raises(AtlasError, match="at most 50"):
        facets.clean_sets([{"key": f"s{n}", "kind": "other"} for n in range(51)])


# ---- same-name achievements (F1, §12.1.4) ------------------------------------------------------------------

def test_same_name_with_different_external_ids_is_two_entries_and_an_exact_repeat_is_dropped(session, ctx, caplog):
    cid = seed(session, ctx)
    items = [{"name": "Collector", "external_id": "COL_1", "description": "base shells"},
             {"name": "Collector", "external_id": "COL_2", "description": "tide shells"},
             {"name": "Collector", "external_id": "COL_1", "description": "repeat"}]
    with caplog.at_level("INFO"):
        assert facets.set_achievements(session, ctx, cid, "steam", items) == 2
    session.commit()
    stored = {(a.external_id, a.description) for a in rows(session, cid)}
    assert stored == {("COL_1", "base shells"), ("COL_2", "tide shells")} and "ach_dup_identity" in caplog.text
    keys = {a.external_id: a.key for a in rows(session, cid)}
    assert keys == {"COL_1": ak1("steam:COL_1"), "COL_2": ak1("steam:COL_2")}
    entries = facets.merged_achievements(session, ctx, cid)
    assert sorted(e["description"] for e in entries) == ["base shells", "tide shells"]  # never merged together
    # another source naming one "Collector" does not merge into either: the name is not unique in steam
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Collector", "description": "curated"}])
    session.commit()
    assert len(facets.merged_achievements(session, ctx, cid)) == 3
    # a refresh keeps each row on its own identity (a rename of one keeps its id)
    before = {a.external_id: a.id for a in rows(session, cid, "steam")}
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Collector", "external_id": "COL_2"},
                                                         {"name": "Shell Collector", "external_id": "COL_1"}])
    session.commit()
    assert {a.external_id: a.id for a in rows(session, cid, "steam")} == before


def test_a_unique_name_merges_across_sources(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Lamp Lit", "external_id": "L", "points": 10}])
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "lamp lit!", "tier": "gold"}])
    session.commit()
    [entry] = facets.merged_achievements(session, ctx, cid)
    assert entry["tier"] == "gold" and entry["points"] == 10 and len(entry["keys"]) == 2


# ---- retirement and order (§12.1.3) ------------------------------------------------------------------------

def test_dropped_entries_retire_return_with_their_key_and_are_purged_after_the_retention(session, ctx):
    cid = seed(session, ctx)
    full = [{"name": "One", "external_id": "1"}, {"name": "Two", "external_id": "2"}]
    facets.set_achievements(session, ctx, cid, "steam", full)
    session.commit()
    two = next(a for a in rows(session, cid) if a.name == "Two")
    two_id, two_key = two.id, two.key
    facets.set_achievements(session, ctx, cid, "steam", full[:1])
    session.commit()
    gone = session.get(Achievement, two_id)
    assert gone.retired_at is not None and gone.key == two_key
    assert [e["name"] for e in facets.merged_achievements(session, ctx, cid)] == ["One"]
    shown = facets.merged_achievements(session, ctx, cid, include_retired=True)
    assert {e["name"]: e["status"] for e in shown}["Two"] == "retired"
    facets.set_achievements(session, ctx, cid, "steam", full)  # it comes back: same id, same key
    session.commit()
    back = session.get(Achievement, two_id)
    assert back.retired_at is None and back.key == two_key
    facets.set_achievements(session, ctx, cid, "steam", full[:1])
    session.commit()
    assert facets.purge_retired(session, ctx.settings) == 0  # inside the retention
    session.get(Achievement, two_id).retired_at -= timedelta(days=ctx.settings.achievement_retired_retention_days + 1)
    session.commit()
    assert facets.purge_retired(session, ctx.settings) == 1
    session.commit()
    assert session.get(Achievement, two_id) is None and [a.name for a in rows(session, cid)] == ["One"]


def test_curated_positions_order_a_set_and_the_order_options(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "steam", [
        {"name": "Zulu", "external_id": "Z", "global_percent": 90.0},
        {"name": "Alpha", "external_id": "A", "global_percent": 10.0},
        {"name": "Mike", "external_id": "M", "global_percent": 50.0}], measured_at=TODAY)
    session.commit()
    assert [e["name"] for e in facets.merged_achievements(session, ctx, cid)] == ["Zulu", "Mike", "Alpha"]  # rarity
    assert [e["name"] for e in facets.merged_achievements(session, ctx, cid, order="source")] == [
        "Zulu", "Alpha", "Mike"]  # the provider's list order
    assert [e["name"] for e in facets.merged_achievements(session, ctx, cid, order="name")] == ["Alpha", "Mike", "Zulu"]
    assert {a.name: a.source_order for a in rows(session, cid)} == {"Zulu": 0, "Alpha": 1, "Mike": 2}
    # a curator arranges the list: positions of the highest class win over rarity
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Mike"}, {"name": "Alpha"}, {"name": "Zulu"}],
                            ordered=True)
    session.commit()
    assert [e["name"] for e in facets.merged_achievements(session, ctx, cid)] == ["Mike", "Alpha", "Zulu"]
    with pytest.raises(AtlasError, match="order must be"):
        facets.merged_achievements(session, ctx, cid, order="random")


# ---- spoilers (§12.1.8) ------------------------------------------------------------------------------------

def test_hidden_entries_leak_nothing_and_sort_by_key_only(session, ctx):
    cid = seed(session, ctx)
    secrets = [{"name": f"Secret {c}", "external_id": f"S_{c}", "hidden": True, "description": f"spoiler {c}",
                "icon_url": f"https://img.test/{c}.png", "global_percent": p, "locale": "en",
                "i18n": {"de": {"name": f"Geheim {c}", "description": f"Spoiler {c}"}}}
               for c, p in (("a", 1.0), ("b", 99.0), ("c", 50.0))]
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Open", "external_id": "O"}, *secrets])
    session.commit()
    listing = facets.achievement_listing(session, ctx, cid, lang="de")
    hidden = [e for e in listing["results"] if e["hidden"]]
    assert len(hidden) == 3 and listing["results"][0]["name"] == "Open"  # concealed entries last in their set
    assert [e["key"] for e in hidden] == sorted(e["key"] for e in hidden)  # by key, not by name or rarity
    text = str(hidden)
    for leak in ("Secret", "Geheim", "spoiler", "Spoiler", "S_a", "img.test", "steam:S_"):
        assert leak not in text, leak
    assert all(e["name"] == facets.HIDDEN_NAME and e["lang"] is None and e["icon_url"] is None
               and e["sources"] == [] for e in hidden)
    assert listing["revealed_any"] is False
    revealed = facets.achievement_listing(session, ctx, cid, reveal={hidden[0]["key"]}, lang="de")
    shown = next(e for e in revealed["results"] if e["key"] == hidden[0]["key"])
    assert shown["revealed"] and shown["name"].startswith("Geheim") and revealed["revealed_any"] is True


def test_concealed_entries_carry_no_curated_position_in_any_listing(session, ctx):
    """§12.1.3, §12.1.8: a curated position on a hidden entry would leak the order of the real names, so a
    concealed entry carries none, for every order=, set= and include_retired combination."""
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "curator", [
        {"name": "Zeta Secret", "hidden": True}, {"name": "Open"}, {"name": "Alpha Secret", "hidden": True},
        {"name": "Gone Secret", "hidden": True}], ordered=True)
    facets.set_achievements(session, ctx, cid, "curator", [  # retires "Gone Secret"
        {"name": "Zeta Secret", "hidden": True}, {"name": "Open"}, {"name": "Alpha Secret", "hidden": True}],
        ordered=True)
    session.commit()
    assert {r.name: r.position for r in rows(session, cid, "curator") if r.retired_at is None} == {
        "Zeta Secret": 0, "Open": 1, "Alpha Secret": 2}  # the positions are stored...
    for order in (None, *facets.ORDERS):
        for set_filter in (None, "base", "curator/base"):
            for include_retired in (False, True):
                listing = facets.achievement_listing(session, ctx, cid, order=order, set_filter=set_filter,
                                                     include_retired=include_retired)
                hidden = [e for e in listing["results"] if e["hidden"]]
                assert len(hidden) == (3 if include_retired else 2)
                assert all(e["position"] is None for e in hidden), (order, set_filter, include_retired)
                assert [e["key"] for e in hidden] == sorted(e["key"] for e in hidden)
                assert next(e for e in listing["results"] if not e["hidden"])["position"] == 1
    revealed = facets.achievement_listing(session, ctx, cid, reveal=True)
    assert [e["position"] for e in revealed["results"]] == [0, 1, 2]  # ...and shown once revealed


def test_api_reveal_is_never_cached_publicly_and_new_query_options_validate(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/a.db", bootstrap_admin_token="m" * 40,
                       media_dir=str(tmp_path / "media"), media_cache_enabled=False, role="local")
    with TestClient(main.create_app(s)) as api:
        api.headers["Authorization"] = "Bearer " + "m" * 40
        body = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "ops"},
                "observations": [{"external_id": "1", "title": "Lantern Bay"}]}
        cid = api.post("/api/v2/observations", json=body).json()["results"][0]["atlas"]["canonical_id"]
        r = api.put(f"/api/v2/games/{cid}/achievements", json={
            "items": [{"name": "Open", "global_percent": 40, "percent_as_of": TODAY.isoformat()},
                      {"name": "Secret Cove", "hidden": True, "set": "dlc1"}],
            "sets": [{"key": "dlc1", "kind": "dlc", "name": "Cove Pack"}]})
        assert r.status_code == 200, r.text
        plain = api.get(f"/api/v2/games/{cid}/achievements")
        assert plain.status_code == 200 and "no-store" not in plain.headers.get("cache-control", "")
        out = plain.json()
        assert [s["id"] for s in out["sets"]] == ["curator/base", "curator/dlc1"] and out["sets_elsewhere"] == []
        assert out["results"][0]["rarity"] == {"percent": 40.0, "source": "curator", "as_of": TODAY.isoformat(),
                                               "stale": False, "band": "uncommon"}
        assert "Secret Cove" not in plain.text
        revealed = api.get(f"/api/v2/games/{cid}/achievements?reveal=all")
        assert "Secret Cove" in revealed.text and revealed.headers["cache-control"] == "private, no-store"
        assert api.get(f"/api/v2/games/{cid}/achievements?set=dlc1").json()["total"] == 1
        assert api.get(f"/api/v2/games/{cid}/achievements?order=random").status_code in (400, 422)
        assert api.get(f"/api/v2/games/{cid}/achievements?lang=not_a_tag").status_code in (400, 422)


# ---- translations (§12.1.7) --------------------------------------------------------------------------------

def test_translations_are_served_by_tag_then_language_and_kept_apart_from_identity(session, ctx):
    cid = seed(session, ctx)
    item = {"name": "Lamp Lit", "external_id": "L", "description": "Light the lamp.", "locale": "en",
            "i18n": {"de": {"name": "Lampe an", "description": "Zünde die Lampe an."},
                     "pt-BR": {"name": "Lâmpada acesa", "description": ""}}}
    facets.set_achievements(session, ctx, cid, "steam", [item])
    session.commit()
    [row] = rows(session, cid)
    assert {t.locale for t in session.scalars(select(AchievementText))} == {"de", "pt-br"}
    assert row.key == ak1("steam:L") and row.text_locale == "en"
    by = lambda lang: facets.merged_achievements(session, ctx, cid, lang=lang)[0]  # noqa: E731
    assert (by("de")["name"], by("de")["lang"]) == ("Lampe an", "de")
    assert (by("de-AT")["name"], by("de-AT")["lang"]) == ("Lampe an", "de")  # the language subtag
    assert (by("pt-br")["name"], by("pt-br")["lang"]) == ("Lâmpada acesa", "pt-br")
    assert (by("fr")["name"], by("fr")["lang"]) == ("Lamp Lit", "en")  # the default text, in its own language
    # removing a translation removes its row; a translation never changes the key
    facets.set_achievements(session, ctx, cid, "steam", [{**item, "i18n": {"de": item["i18n"]["de"]}}])
    session.commit()
    assert {t.locale for t in session.scalars(select(AchievementText))} == {"de"} and rows(session, cid)[0].id == row.id
    with pytest.raises(AtlasError, match="language tag"):
        facets.set_achievements(session, ctx, cid, "steam", [{**item, "i18n": {"english!": {"name": "x"}}}])


# ---- rarity (§12.1.6) --------------------------------------------------------------------------------------

def test_rarity_prefers_a_fresh_measurement_and_marks_stale_values(session, ctx):
    cid = seed(session, ctx)
    old = TODAY - timedelta(days=ctx.settings.achievement_percent_max_age_days + 5)
    facets.set_achievements(session, ctx, cid, "curator", [
        {"name": "Lamp Lit", "external_id": "L", "global_percent": 3.0, "percent_as_of": TODAY.isoformat()}])
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Lamp Lit", "external_id": "L",
                                                          "global_percent": 62.0}], measured_at=old)
    session.commit()
    [e] = facets.merged_achievements(session, ctx, cid)
    assert e["rarity"] == {"percent": 3.0, "source": "curator", "as_of": TODAY.isoformat(), "stale": False,
                           "band": "very_rare"}  # the provider's measurement is too old: a fresh curated value wins
    facets.set_achievements(session, ctx, cid, "steam", [{"name": "Lamp Lit", "external_id": "L",
                                                          "global_percent": 61.0}], measured_at=TODAY)
    session.commit()
    [e] = facets.merged_achievements(session, ctx, cid)
    assert (e["rarity"]["source"], e["rarity"]["percent"], e["rarity"]["stale"]) == ("steam", 61.0, False)
    assert e["global_percent"] == 61.0  # the old field carries the same merged value
    # nothing fresh: the newest value, marked stale; a legacy row without a date counts as stale
    for a in rows(session, cid):
        a.percent_as_of = None if a.source == "steam" else old
    session.commit()
    [e] = facets.merged_achievements(session, ctx, cid)
    assert (e["rarity"]["source"], e["rarity"]["stale"], e["rarity"]["as_of"]) == ("curator", True, old.isoformat())


def test_an_unchanged_percent_keeps_its_date_and_stored_stats_join_rows_by_external_id(session, ctx):
    cid = seed(session, ctx)
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Lamp", "external_id": "ACH_LAMP",
                                                            "global_percent": 12.0}])
    session.commit()
    [row] = rows(session, cid)
    row.percent_as_of = date(2026, 10, 1)
    session.commit()
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Lamp", "external_id": "ACH_LAMP",
                                                            "global_percent": 12.0, "description": "edited"}])
    session.commit()
    assert rows(session, cid)[0].percent_as_of == date(2026, 10, 1)  # the value did not change
    facets.set_achievements(session, ctx, cid, "curator", [{"name": "Lamp", "external_id": "ACH_LAMP",
                                                            "global_percent": 13.0}])
    session.commit()
    assert rows(session, cid)[0].percent_as_of == TODAY  # a new value is dated when it was stored
    # keyless Steam: only percentages, stored apart, matched to the rows that name the external id
    assert facets.store_stats(session, cid, "steam", {"ACH_LAMP": 4.5, "UNKNOWN": 9.0, "BAD": 140}, as_of=TODAY) == 2
    session.commit()
    assert {s.external_id for s in session.scalars(select(AchievementStat))} == {"ACH_LAMP", "UNKNOWN"}
    [e] = facets.merged_achievements(session, ctx, cid)
    assert (e["rarity"]["source"], e["rarity"]["percent"], e["rarity"]["band"]) == ("steam", 4.5, "very_rare")
    assert len(rows(session, cid)) == 1  # a stat never creates an entry


def test_authority_and_a_late_mirror_date_an_undated_percent_alike(tmp_path):
    """§12.1.6: a percent without a date takes the date of the signed entry's origin timestamp, so a mirror that
    bootstraps weeks later stores (and calls stale) the same date as the authority, never its own clock."""
    from atlas.models import ReplicatedChange

    from .test_sync_compat import Node, pull

    authority = Node(tmp_path, "authority", "authority")
    mirror = Node(tmp_path, "mirror", "mirror")
    gid = authority.publish("Dated Bay")
    old = date.today() - timedelta(days=45)
    with authority.st.maker() as s:
        facets.set_achievements(s, authority.st.ctx, gid, "curator",
                                [{"name": "Lamp", "external_id": "L", "global_percent": 12.0}], redistributable=True)
        s.commit()
        [row] = s.scalars(select(Achievement)).all()
        assert row.percent_as_of == date.today()  # the authority dates it by the write
        entry = s.scalars(select(ChangeLog).where(ChangeLog.kind == "achievements")).one()
        assert "percent_as_of" not in entry.body["items"][0]  # a v1 body carries no date
        # as if the write (and so its entry) happened 45 days ago
        row.percent_as_of = old
        entry.created_at = entry.created_at - timedelta(days=45)
        s.commit()
        authority_stamp = entry.created_at
    pull(mirror, authority)
    assert mirror.failures(authority) == []
    with mirror.st.maker() as s:
        [mrow] = s.scalars(select(Achievement)).all()
        assert mrow.percent_as_of == old  # not the mirror's today
        kept = s.scalars(select(ReplicatedChange).where(ReplicatedChange.kind == "achievements")).one()
        assert sync_aware(kept.created_at) == sync_aware(authority_stamp)  # the relay serves the origin time
        [m_entry] = facets.merged_achievements(s, mirror.st.ctx, gid)
    with authority.st.maker() as s:
        [a_entry] = facets.merged_achievements(s, authority.st.ctx, gid)
    assert m_entry["rarity"] == a_entry["rarity"] and a_entry["rarity"]["stale"] is True
    assert a_entry["rarity"]["as_of"] == old.isoformat()


def sync_aware(at):
    from atlas import sync

    return sync._aware(at)


@pytest.mark.parametrize(("value", "replaced"), [("2999-01-01T00:00:00+00:00", True), ("garbage", True),
                                               (None, True), ("2026-01-02T03:04:05+00:00", False),
                                               ("2026-01-02T03:04:05", False)])
def test_origin_time_is_kept_unless_it_is_unusable(value, replaced):
    from datetime import UTC, datetime

    from atlas import sync

    at = sync.origin_time(value)
    if replaced:  # replaced by this node's clock
        assert abs((at - datetime.now(UTC)).total_seconds()) < 60
    else:
        assert at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_the_keyless_steam_import_stores_percentages_only(monkeypatch, tmp_path):
    from atlas.providers.steam import Steam

    calls = []

    def fake_get_json(_client, _method, url, params=None, **_kw):
        calls.append(url)
        return {"achievementpercentages": {"achievements": [{"name": "ACH_LAMP", "percent": "4.25"},
                                                            {"name": "ACH_BAD", "percent": "nan%"}]}}

    monkeypatch.setattr("atlas.providers.steam.get_json", fake_get_json)
    assert Steam().achievements(None, "900") == []  # no key: no definitions
    assert Steam().achievement_stats(None, "900") == {"ACH_LAMP": 4.25}
    assert all("GetGlobalAchievementPercentagesForApp" in u for u in calls)


# ---- the signed copy (§12.4: downgrade below 1.7) ----------------------------------------------------------

def test_the_signed_list_is_the_v1_body_without_new_fields_or_later_same_names(session, ctx, caplog):
    cid = seed(session, ctx)
    with caplog.at_level("INFO"):
        facets.set_achievements(session, ctx, cid, "curator", [
            {"name": "Collector", "external_id": "C1", "set": "t", "status": "unobtainable", "locale": "en",
             "i18n": {"de": {"name": "Sammler"}}, "global_percent": 5.0, "percent_as_of": TODAY.isoformat(),
             "icon_url": "https://img.test/c.png", "icon_licence": "cc-by", "icon_attribution": "A. Artist"},
            {"name": "Collector", "external_id": "C2"}],
            sets=[{"key": "t", "kind": "dlc", "name": "Tides"}], redistributable=True)
    session.commit()
    [entry] = [c for c in session.scalars(select(ChangeLog).where(ChangeLog.kind == "achievements"))]
    [item] = entry.body["items"]
    assert set(item) <= set(facets.V1_ITEM_FIELDS) and "_v" not in entry.body
    assert item["external_id"] == "C1" and "icon_url" not in item  # cc-by needs attribution: not in the CC0 feed
    assert item["global_percent"] == 5.0 and item["key"] == ak1("curator:C1")
    assert "ach_downgrade" in caplog.text
    assert len(rows(session, cid)) == 2  # both are kept locally


@pytest.mark.parametrize("release", ["this", "1.1"])
def test_followers_park_an_achievements_v2_body(tmp_path, monkeypatch, release):
    from atlas import auth

    from .test_prices import play
    from .test_sync_compat import Node, pull

    authority = Node(tmp_path, "authority", "authority", emit_protocol="1.5")
    mirror = Node(tmp_path, "mirror", "mirror")
    with authority.st.maker() as s:
        auth.create_token(s, "curator", ["read", "curate"])
        s.commit()
    gid = authority.publish("Park Two")
    seq = authority.emit("achievements", {"_v": 2, "canonical_id": gid, "source": "curator", "_requires": ["ach-v2"],
                                          "sets": [{"key": "t", "kind": "dlc"}],
                                          "items": [{"name": "A", "set": "t", "external_id": "1"}]})
    with monkeypatch.context() as m:
        play(m, release)
        pull(mirror, authority)
        [(fseq, kind, disposition, _reason)] = mirror.failures(authority)
        assert (fseq, kind, disposition) == (seq, "achievements", "parked")
    assert seq in mirror.retained(authority)  # kept for a later release to apply, never dropped
    with mirror.st.maker() as s:
        assert s.scalars(select(Achievement)).all() == []


# ---- the editor (#97, #79) -----------------------------------------------------------------------------------

def test_the_editor_sets_rarity_and_status_and_keeps_fields_it_does_not_show(site):  # noqa: F811
    from html import unescape

    from atlas.models import Proposal

    from .test_web import _ach_form, signup, token

    signup(site, "alice")  # administrator: applies at once
    with site.st.maker() as s:  # a community list with a set and a translation the editor has no control for
        facets.set_achievements(s, site.st.ctx, site.gid, "community", [
            {"name": "Lamp", "external_id": "L", "set": "t", "i18n": {"de": {"name": "Lampe", "description": ""}}}],
            sets=[{"key": "t", "kind": "dlc", "name": "Tides"}])
        s.commit()
    url = f"/game/{site.gid}/achievements/edit"
    page = site.get(url).text
    assert "Rarity (%)" in page and 'name="status.r0"' in page
    t = token(site, url)
    extra = unescape(re.search(r'name="extra\.r0" value="([^"]*)"', page).group(1))
    assert json.loads(extra) == {"i18n": {"de": {"description": "", "name": "Lampe"}}, "set": "t"}
    bad = site.post(url, data=_ach_form(t, {"r0": {"name": "Lamp", "external_id": "L", "global_percent": "140",
                                                   "extra": extra}}))
    assert bad.status_code == 400 and "Rarity must be a number from 0 to 100." in unescape(bad.text)
    rows_form = {"r0": {"name": "Lamp", "external_id": "L", "global_percent": "8,5", "status": "unobtainable",
                        "extra": extra},
                 "r1": {"name": "Collector", "external_id": "C1"}, "r2": {"name": "Collector", "external_id": "C2"}}
    preview = site.post(url, data=_ach_form(t, rows_form))
    assert preview.status_code == 200, unescape(preview.text)[:500]  # same name, different external ids: allowed
    assert "Rarity (%)" in unescape(preview.text) and "unobtainable" in preview.text
    done = site.post(url, data=_ach_form(t, rows_form, action="submit"))
    assert done.status_code == 303
    with site.st.maker() as s:
        lamp = s.scalars(select(Achievement).where(Achievement.source == "community", Achievement.name == "Lamp")).one()
        assert (lamp.global_percent, lamp.status, lamp.set_key) == (8.5, "unobtainable", "t")
        assert [t.name for t in s.scalars(select(AchievementText).where(AchievementText.achievement_id == lamp.id))] \
            == ["Lampe"]
        assert {a.external_id for a in s.scalars(select(Achievement).where(Achievement.name == "Collector"))} == {
            "C1", "C2"}
        assert s.scalars(select(AchievementSet).where(AchievementSet.source == "community")).one().set_key == "t"
        assert s.scalars(select(Proposal)).first().previous["sets"] == [{"key": "t", "kind": "dlc", "name": "Tides"}]


# ---- validation of the new fields ----------------------------------------------------------------------------

def test_self_dlc_future_rarity_dates_and_control_characters_in_translations_are_refused(session, ctx):
    cid = seed(session, ctx)
    other = seed(session, ctx, ext="901", title="Lantern Bay: Tides")
    with pytest.raises(AtlasError, match="its own dlc"):
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "A", "set": "t"}],
                                sets=[{"key": "t", "kind": "dlc", "dlc": cid}])
    future = (TODAY + timedelta(days=3)).isoformat()
    with pytest.raises(AtlasError, match="future"):
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "A", "global_percent": 3.0,
                                                                "percent_as_of": future}])
    with pytest.raises(AtlasError, match="future"):
        facets.set_achievements(session, ctx, cid, "curator", [{"name": "A", "rarity": {"percent": 3.0,
                                                                                         "as_of": "2999-01-01"}}])
    for bad in ({"de": {"name": "Weck\x00ruf"}}, {"de": {"name": "Weckruf", "description": "a\x1bb"}},
                {"de": {"name": 12}}):
        with pytest.raises(AtlasError):
            facets.set_achievements(session, ctx, cid, "curator", [{"name": "A", "i18n": bad}])
    session.rollback()
    assert rows(session, cid) == []  # nothing was written
    # still accepted: another record as the dlc, today's date, and prose with line breaks
    facets.set_achievements(session, ctx, cid, "curator", [
        {"name": "A", "set": "t", "global_percent": 3.0, "percent_as_of": TODAY.isoformat(),
         "i18n": {"de": {"name": "Weckruf", "description": "Zeile eins\nZeile zwei"}}}],
        sets=[{"key": "t", "kind": "dlc", "dlc": other}])
    session.commit()
    assert session.scalars(select(AchievementSet)).one().dlc_canonical_id == other
    assert session.scalars(select(AchievementText)).one().description == "Zeile eins\nZeile zwei"


from .test_web import site  # noqa: E402, F401  (fixture)
