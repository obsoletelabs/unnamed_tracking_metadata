"""SPEC-UI-POLISH-PASS: the date offer (§7.4), partial-date entry (§7.2), the getting-started checklist (§3.7),
standalone peers page (§3.1), no protocol numbers in the interface (§3.6) and the achievement editor and
filters (§8). All names and data are synthetic."""
import re
from datetime import UTC, datetime, timedelta
from html import unescape

from sqlalchemy import select

from atlas import facets, profile, proposals
from atlas.models import NodeState, Peer, Proposal, User, UserMfa
from atlas.web import setup_checklist

from .test_web import _ach_form, fresh, login, signup, site, token  # noqa: F401  (fixture)

# ---- the one-time date offer (§7.4) ------------------------------------------------------------------------


def test_date_offer_markup_hidden_until_js(site):  # noqa: F811
    page = site.get("/").text
    assert re.search(r"<aside class=\"offer\" data-date-offer hidden", page)  # app.js decides; no-JS sees nothing
    signup(site, "alice")
    assert "data-date-offer" in site.get("/").text
    r = site.post("/prefs/date-format", data={"csrf": token(site, "/"), "value": "dmy", "next": "/"})
    assert r.status_code == 303
    assert "data-date-offer" not in site.get("/").text  # "Keep" is an explicit choice: never asked again


def test_offer_keep_writes_explicit_dmy(site):  # noqa: F811
    signup(site, "alice")
    site.post("/prefs/date-format", data={"csrf": token(site, "/"), "value": "dmy", "next": "/"})
    with site.st.maker() as s:
        user = s.scalars(select(User).where(User.username == "alice")).one()
        assert profile.values(s, site.st.ctx.settings, user)["date_format"] == "dmy"
    anon = fresh(site)
    first = anon.get("/")
    assert first.headers["Cache-Control"] == "public, max-age=30" and first.headers["Vary"] == "Cookie"
    r = anon.post("/prefs/date-format", data={"value": "mdy", "next": "/"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303 and "atlas_date=mdy" in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    page = anon.get(f"/game/{site.gid}")
    assert page.headers["Cache-Control"] == "private, no-store" and "09/17/2020" in page.text
    assert "data-date-offer" not in page.text


def test_cookie_adopted_on_signin_once(site):  # noqa: F811
    signup(site, "alice")
    site.post("/logout", data={"csrf": token(site, "/")})
    anon = fresh(site)
    anon.post("/prefs/date-format", data={"value": "iso", "next": "/"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert login(anon, "alice").status_code == 303
    with site.st.maker() as s:
        user = s.scalars(select(User).where(User.username == "alice")).one()
        assert profile.values(s, site.st.ctx.settings, user)["date_format"] == "iso"
    # a later cookie never overwrites a profile choice
    other = fresh(site)
    other.post("/prefs/date-format", data={"value": "mdy", "next": "/"}, headers={"Sec-Fetch-Site": "same-origin"})
    login(other, "alice")
    with site.st.maker() as s:
        user = s.scalars(select(User).where(User.username == "alice")).one()
        assert profile.values(s, site.st.ctx.settings, user)["date_format"] == "iso"


def test_date_offer_post_is_safe(site):  # noqa: F811
    anon = fresh(site)
    cross = anon.post("/prefs/date-format", data={"value": "mdy", "next": "/"},
                      headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and "set-cookie" not in cross.headers
    evil = anon.post("/prefs/date-format", data={"value": "mdy", "next": "//evil.example"},
                     headers={"Sec-Fetch-Site": "same-origin"})
    assert evil.status_code == 303 and evil.headers["location"] == "/"
    bad = anon.post("/prefs/date-format", data={"value": "xyz"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert bad.status_code == 400
    clean = fresh(site).get("/")
    assert clean.headers["Cache-Control"] == "public, max-age=30" and 'name="csrf"' not in clean.text
    signup(site, "alice")
    no_token = site.post("/prefs/date-format", data={"value": "mdy", "next": "/"})
    assert no_token.status_code in (400, 403)


# ---- partial-date entry (§7.2) ------------------------------------------------------------------------------

def test_ambiguous_entry_prompts_then_saves(site):  # noqa: F811
    signup(site, "alice")
    signup(bob := fresh(site), "bob")
    url = f"/game/{site.gid}/edit"
    t = token(bob, url)
    first = bob.post(url, data={"csrf": t, "release_date": "03/04/2021"})
    text = unescape(first.text)
    assert first.status_code == 400 and "can be read two ways" in text
    assert 'name="release_date.read" value="2021-04-03"' in text and 'value="2021-03-04"' in text
    assert 'value="03/04/2021"' in text  # what was typed comes back
    again = bob.post(url, data={"csrf": t, "release_date": "03/04/2021", "release_date.read": "2021-03-04"})
    assert again.status_code == 303, again.text
    with site.st.maker() as s:
        pr = s.scalars(select(Proposal)).one()
        assert pr.payload["release_date"] == "2021-03-04"


def test_placeholders_follow_style(site):  # noqa: F811
    signup(site, "alice")
    url = f"/game/{site.gid}/edit"
    assert 'placeholder="dd/mm/yyyy, mm/yyyy or yyyy"' in site.get(url).text
    site.post("/prefs/date-format", data={"csrf": token(site, "/"), "value": "mdy", "next": "/"})
    assert 'placeholder="mm/dd/yyyy, mm/yyyy or yyyy"' in site.get(url).text
    assert 'placeholder="mm/dd/yyyy, mm/yyyy or yyyy"' in site.get("/search").text


# ---- getting started (§3.7) ---------------------------------------------------------------------------------

def _items(client, **kw):
    with client.st.maker() as s:
        return {i.key: i.done for i in setup_checklist.items(s, client.st.ctx.settings, **kw)}


def _local(client, **update):
    client.st.ctx.settings = client.st.ctx.settings.model_copy(update={"role": "local", **update})


def test_setup_checklist_items(site, tmp_path):  # noqa: F811
    signup(site, "alice")
    assert "registration" not in _items(site)  # a public (authority) node takes sign-ups
    _local(site)
    items = _items(site)
    assert "public_url" not in items  # only once a peer or group exists
    assert items["catalogue"] and not items["secret_key"] and not items["backups"] and not items["mfa"]
    assert not items["registration"]  # open registration on a local node
    assert not _items(site, providers=0)["providers"] and _items(site, providers=1)["providers"]
    with site.st.maker() as s:
        user = s.scalars(select(User).where(User.username == "alice")).one()
        s.add(UserMfa(user_id=user.id, secret_enc="x", setup_session_id="s", enabled_at=datetime.now(UTC)))
        s.add(Peer(name="Synthetic", url="https://peer.example.net", node_id="p" * 16, public_key="k"))
        s.commit()
        uid = user.id
    items = _items(site, user_id=uid)
    assert items["mfa"] and items["public_url"] is False  # still http://localhost:8000
    old = site.st.ctx.settings
    try:
        site.st.ctx.settings = old.model_copy(update={"registration": "closed", "secret_key": "s" * 44,
                                                      "public_url": "https://atlas.example.net",
                                                      "backup_dir": str(tmp_path)})
        (tmp_path / "atlas-20200917.tar.gz").write_bytes(b"x")
        items = _items(site, user_id=uid)
        assert items["registration"] and items["secret_key"] and items["public_url"] and items["backups"]
        later = datetime.now(UTC) + timedelta(days=8)
        with site.st.maker() as s:
            assert not {i.key: i.done for i in setup_checklist.items(s, site.st.ctx.settings, now=later)}["backups"]
    finally:
        site.st.ctx.settings = old


def test_setup_card_hides_and_shows(site):  # noqa: F811
    signup(site, "alice")
    _local(site)
    page = site.get("/admin").text
    assert "data-setup-checklist" in page and 'data-setup-item="mfa"' in page
    assert "Works on its own (no peers)" in page and "Not following any other node." in page
    assert "data-nav-optional" in page
    r = site.post("/admin/setup/hide", data={"csrf": token(site, "/admin")})
    assert r.status_code == 303
    with site.st.maker() as s:
        assert s.get(NodeState, setup_checklist.HIDDEN_KEY) is not None
    page = site.get("/admin").text
    assert "data-setup-checklist" not in page and "data-setup-show" in page
    site.post("/admin/setup/show", data={"csrf": token(site, "/admin")})
    assert "data-setup-checklist" in site.get("/admin").text
    assert site.post("/admin/setup/delete", data={"csrf": token(site, "/admin")}).status_code == 404
    signup(bob := fresh(site), "bob")
    assert bob.post("/admin/setup/hide", data={"csrf": token(bob, "/")}).status_code == 403


# ---- peers standalone and no protocol numbers (§3.1, §3.6) ------------------------------------------------

PROTOCOL_MINOR = re.compile(r"(?i)protocol\s*\d+\.\d+")
NEAR = re.compile(r"(?i)(?:protocol|release).{0,30}\b1\.[0-9]\b|\b1\.[0-9]\b.{0,30}(?:protocol|release)")


def _visible(html):
    html = re.sub(r"<details[^>]*data-protocol-internal.*?</details>", " ", html, flags=re.S)
    html = re.sub(r"<code[^>]*>.*?</code>", " ", html, flags=re.S)
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def test_no_protocol_minors_in_copy(site):  # noqa: F811
    signup(site, "alice")
    for path in ("/admin", "/admin/peers", "/admin/group", "/admin/settings", "/admin/backup",
                 f"/game/{site.gid}/edit", f"/game/{site.gid}/achievements/edit", f"/game/{site.gid}/relations"):
        r = site.get(path)
        if r.status_code != 200:
            continue
        text = _visible(r.text)
        assert not PROTOCOL_MINOR.search(text), (path, PROTOCOL_MINOR.search(text).group(0))
        assert not NEAR.search(text), (path, NEAR.search(text).group(0))


def test_peers_standalone_empty_state(site):  # noqa: F811
    signup(site, "alice")
    page = site.get("/admin/peers").text
    assert "data-peers-standalone" in page and "No peers yet" in page
    assert '<details class="own-feed" id="this-node">' in page  # own feed collapsed
    assert 'data-flash-tone="error"' not in page and 'data-flash-tone="warn"' not in page
    assert "No failed entries" in page


# ---- achievements (§8) --------------------------------------------------------------------------------------

def _seed_achievements(client, n=3, source="curator"):
    items = [{"name": "Lamp Lit", "external_id": "A1", "tier": "gold", "points": 30, "global_percent": 2.5},
             {"name": "Tide Turner", "external_id": "T1", "set": "tides", "global_percent": 60.0},
             {"name": "Secret Shore", "external_id": "S1", "hidden": True, "status": "unobtainable"}]
    items += [{"name": f"Filler {i}", "external_id": f"F{i}"} for i in range(max(0, n - 3))]
    with client.st.maker() as s:
        facets.set_achievements(s, client.st.ctx, client.gid, source, items,
                                sets=[{"key": "tides", "kind": "dlc", "name": "Tides"}], ordered=True)
        s.commit()


def test_game_filter_links(site):  # noqa: F811
    _seed_achievements(site)
    url = f"/game/{site.gid}"
    page = unescape(site.get(url).text)
    for key in ("all", "rare", "hidden", "unobtainable"):
        assert f'data-ach-filter-link="{key}"' in page, key
    assert 'data-ach-filter-link="set:tides"' in page
    rare = unescape(site.get(url, params={"ach": "rare"}).text)
    assert "Lamp Lit" in rare and "Tide Turner" not in rare and "Showing 1 of 3" in rare
    hidden = unescape(site.get(url, params={"ach": "hidden"}).text)
    assert "Hidden achievement" in hidden and "Secret Shore" not in hidden and "Lamp Lit" not in hidden
    gone = unescape(site.get(url, params={"ach": "unobtainable"}).text)
    assert "Showing 1 of 3" in gone and "Lamp Lit" not in gone
    tides = unescape(site.get(url, params={"ach": "set:tides"}).text)
    assert "Tide Turner" in tides and "Lamp Lit" not in tides
    plat = unescape(site.get(url, params={"ach": "platinum"}).text)
    assert "None match this filter." in plat
    unknown = unescape(site.get(url, params={"ach": "<script>"}).text)
    assert "Lamp Lit" in unknown and "Tide Turner" in unknown and "data-ach-filtered" not in unknown
    assert 'class="achievements data-cards"' in page


def test_rows_collapsed_errors_open(site):  # noqa: F811
    signup(site, "alice")
    _seed_achievements(site, source=proposals.SOURCE)
    url = f"/game/{site.gid}/achievements/edit"
    short = site.get(url).text
    assert len(re.findall(r'<details class="ach-row" data-row data-row-key="r\d+" id="[^"]+" open>', short)) == 3
    _seed_achievements(site, n=10, source=proposals.SOURCE)
    page = site.get(url).text
    assert re.search(r'<details class="ach-row" data-row data-row-key="[^"]+" id="[^"]+">', page)  # collapsed
    assert "data-ach-toolbar hidden" in page and "data-ach-count" in page and "data-ach-others" in page
    # a row with an error opens, whatever the length
    keys = re.findall(r'name="row" value="([^"]+)"', page)
    rows = {k: {"name": re.search(rf'name="name.{re.escape(k)}" value="([^"]*)"', page).group(1)} for k in keys}
    rows[keys[0]]["points"] = "lots"
    data = _ach_form(token(site, url), rows)
    r = site.post(url, data=data)
    assert r.status_code == 400
    assert re.search(r'<details class="ach-row has-errors" data-row data-row-key="[^"]+" id="[^"]+" open>', r.text)


def test_field_names_unchanged(site):  # noqa: F811
    signup(site, "alice")
    _seed_achievements(site, source=proposals.SOURCE)
    page = site.get(f"/game/{site.gid}/achievements/edit").text
    key = re.search(r'name="row" value="([^"]+)"', page).group(1)
    for field in ("name", "description", "kind", "tier", "points", "global_percent", "status", "hidden", "icon_url",
                  "locked_icon_url", "external_id", "akey", "extra", "remove"):
        assert f'name="{field}.{key}"' in page, field


# ---- AE6 set select and the §8.2 detail page (U3 review) -----------------------------------------------------

def test_set_select_bound_to_extra(site):  # noqa: F811
    signup(site, "alice")  # administrator: an edit applies at once
    url = f"/game/{site.gid}/achievements/edit"
    with site.st.maker() as s:  # only the base set: no select
        facets.set_achievements(s, site.st.ctx, site.gid, proposals.SOURCE, [{"name": "Lamp Lit"}], ordered=True)
        s.commit()
    assert "data-ach-set" not in site.get(url).text
    _seed_achievements(site, source=proposals.SOURCE)
    page = unescape(site.get(url).text)
    assert page.count("data-ach-set") == 4  # three rows and the new-row template
    keys = re.findall(r'name="row" value="([^"]+)"', page)
    tide = next(k for k in keys if re.search(rf'name="name.{re.escape(k)}" value="Tide Turner"', page))
    assert re.search(rf'name="set.{re.escape(tide)}" data-ach-set><option value="base" >Base game</option>'
                     rf'<option value="tides" selected>Tides</option>', page)
    # move "Lamp Lit" into the Tides set and "Tide Turner" back to the base set; the wire stays `extra.set`
    rows = {}
    for k in keys:
        name = re.search(rf'name="name.{re.escape(k)}" value="([^"]*)"', page).group(1)
        extra = unescape(re.search(rf'name="extra.{re.escape(k)}" value="([^"]*)"', page).group(1))
        rows[k] = {"name": name, "extra": extra, "external_id": re.search(
            rf'name="external_id.{re.escape(k)}" value="([^"]*)"', page).group(1),
            "set": {"Lamp Lit": "tides", "Tide Turner": "base"}.get(name, "")}
    r = site.post(url, data=_ach_form(token(site, url), rows, action="submit"))
    assert r.status_code == 303, r.text
    with site.st.maker() as s:
        items = {i["name"]: i for i in facets.achievement_items(
            list(s.scalars(select(facets.Achievement).where(facets.Achievement.canonical_id == site.gid,
                                                           facets.Achievement.source == proposals.SOURCE))), s)}
    assert items["Lamp Lit"].get("set") == "tides" and items["Tide Turner"].get("set", "base") == "base"


def test_achievement_detail_page(site):  # noqa: F811
    signup(site, "alice")
    with site.st.maker() as s:
        facets.set_achievements(s, site.st.ctx, site.gid, "curator", [
            {"name": "Lamp Lit", "external_id": "A1", "tier": "gold", "points": 30, "global_percent": 2.5},
            {"name": "Tide Turner", "external_id": "T1", "set": "tides", "global_percent": 60.0},
            {"name": "Secret Shore", "description": "The ending", "external_id": "S1", "hidden": True}],
            sets=[{"key": "tides", "kind": "dlc", "name": "Tides"}], ordered=True)
        s.commit()
        ids = {e["name"]: e["id"] for e in facets.merged_achievements(s, site.st.ctx, site.gid, reveal=True)}
    page = unescape(site.get(f"/game/{site.gid}/achievements/{ids['Lamp Lit']}").text)
    assert 'data-ach-detail="' in page and "ach-icon-pair" in page and "unlocked" in page and "locked" in page
    assert "data-ach-rarity" in page and "2.5%" in page and "ultra rare" in page
    assert re.search(r"data-ach-measured>measured (<time[^>]*>[^<]+</time>|at an unknown date)", page)
    assert 'data-ach-sources>curator' in page and "data-ach-set>Base game" not in page
    assert 'data-nav="next"' in page and "Position" in page
    # the neighbour that is hidden is never named in the link
    tide = unescape(site.get(f"/game/{site.gid}/achievements/{ids['Tide Turner']}").text)
    assert "data-ach-set>Tides" in tide and 'data-nav="prev"' in tide
    nxt = re.search(r'data-nav="next" rel="next">([^<]+)<', tide)
    assert nxt is None or "Secret Shore" not in nxt.group(1)
    # the hidden one itself: behind the spoiler guard, not in the title or heading
    secret = site.get(f"/game/{site.gid}/achievements/{ids['Secret Shore']}")
    text = unescape(secret.text)
    assert "<h1>Hidden achievement</h1>" in text and "data-ach-spoiler" in text
    assert "Secret Shore" not in text.split("data-ach-spoiler")[0] and "no-store" in secret.headers["cache-control"]
