import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from atlas import main, runtime
from atlas.models import AuditEntry, Page, Setting

from .conftest import build_settings
from .test_web import fresh, login, signup, site, token  # noqa: F401


def png(w=64, h=64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 60)).save(buf, "PNG")
    return buf.getvalue()


def spec(key):
    return runtime.BY_KEY[key]


def test_parse_validates_each_kind():
    assert runtime.parse(spec("auto_approve_reputation"), "75") == 75
    for bad in ("x", "-1", "99999999"):
        with pytest.raises(runtime.SettingError):
            runtime.parse(spec("auto_approve_reputation"), bad)
    assert runtime.parse(spec("registration"), "closed") == "closed"
    with pytest.raises(runtime.SettingError):
        runtime.parse(spec("registration"), "invite-only")
    assert runtime.parse(spec("accent_color"), "#FF8800") == "#ff8800"
    for bad in ("red", "#fff", "#gggggg", "url(x)"):
        with pytest.raises(runtime.SettingError):
            runtime.parse(spec("accent_color"), bad)
    assert runtime.parse(spec("email_verification"), "") is None  # nullable = by role
    assert runtime.parse(spec("smtp_starttls"), "on") is True and runtime.parse(spec("smtp_starttls"), False) is False
    with pytest.raises(runtime.SettingError):
        runtime.parse(spec("app_name"), "x" * 100)
    with pytest.raises(runtime.SettingError):
        runtime.parse(spec("app_name"), "bad\x00name")


def test_nav_links_accept_only_safe_addresses():
    ok = runtime.parse_links("Docs | /p/help\nSource | https://github.com/x/y")
    assert ok == [{"label": "Docs", "url": "/p/help"}, {"label": "Source", "url": "https://github.com/x/y"}]
    for bad in ("x | javascript:alert(1)", "x | //evil.test", "x | http://plain.test", "| /p/x", "x | /a b",
                "x | data:text/html,hi", "x | /p\\evil"):
        with pytest.raises(runtime.SettingError):
            runtime.parse_links(bad)
    assert len(runtime.parse_links("\n".join(f"L{i} | /p/{i}" for i in range(30)))) == 12


def test_thresholds_are_cross_checked_and_stored_as_percent(session):
    base = build_settings()
    with pytest.raises(ValueError):
        runtime.save(session, base, None, {"auto_match_threshold": "70", "review_threshold": "90"})
    runtime.save(session, base, None, {"auto_match_threshold": "92", "review_threshold": "65"})
    eff = runtime.effective(base, runtime.load(session))
    assert eff.auto_match_threshold == 0.92 and eff.review_threshold == 0.65
    assert runtime.display(spec("auto_match_threshold"), 0.92) == "92"


def test_save_records_audit_and_ignores_unknown_and_unchanged(session):
    base = build_settings()
    changed = runtime.save(session, base, "u1", {"app_name": "Vault", "nonsense": "x", "tagline": base.tagline})
    assert changed == ["app_name"]
    entry = session.scalars(select(AuditEntry).where(AuditEntry.action == "setting")).one()
    assert entry.target == "app_name" and entry.detail == {"from": "Atlas", "to": "Vault"}
    assert runtime.reset(session, "u1", "app_name") and not runtime.reset(session, "u1", "app_name")


def login_admin(site):  # noqa: F811
    signup(site, "alice")
    return site


def post_settings(site, **values):  # noqa: F811
    t = token(site, "/admin/settings")
    return site.post("/admin/settings", data={"csrf": t, **values})


def all_defaults(site):  # noqa: F811
    html = site.get("/admin/settings").text
    vals = {}
    for m in re.finditer(r'<input id="s_(\w+)" type="(?:text|color)"(?: inputmode="numeric")? name="\1" value="([^"]*)"', html):  # noqa: E501
        vals[m.group(1)] = m.group(2)
    for m in re.finditer(r'<textarea id="s_(\w+)" name="\1" rows="3">([^<]*)</textarea>', html):
        vals[m.group(1)] = m.group(2)
    return vals


def test_admin_changes_branding_and_it_appears_everywhere(site):  # noqa: F811
    login_admin(site)
    vals = all_defaults(site)
    vals.update(app_name="Vault", tagline="Everything about games", footer_text="Run by friends",
                accent_color="#ff8800", nav_links="Discord | https://discord.gg/x\nRules | /p/rules", default_theme="dark",
                registration="open", mail_backend="outbox", email_verification="", smtp_starttls="on")
    r = post_settings(site, **vals)
    assert r.status_code == 303, r.text
    home = site.get("/").text
    assert "Vault" in home and "Everything about games" in home and "Run by friends" in home
    assert 'data-theme="dark"' in home and 'href="https://discord.gg/x"' in home and 'href="/p/rules"' in home
    css = site.get("/theme.css")
    assert css.status_code == 200 and "--accent:#ff8800" in css.text and "text/css" in css.headers["content-type"]
    assert site.get("/theme.css", headers={"If-None-Match": css.headers["etag"]}).status_code == 304
    info = site.get("/api/v2/site").json()
    assert info["name"] == "Vault" and info["theme"]["accent"] == "#ff8800" and info["role"] == "authority"
    assert info["nav_links"][0]["label"] == "Discord"


def test_invalid_settings_are_rejected_and_nothing_is_saved(site):  # noqa: F811
    login_admin(site)
    vals = all_defaults(site)
    vals["app_name"] = "Should not stick"
    vals["accent_color"] = "javascript:alert(1)"
    r = post_settings(site, **vals)
    assert r.status_code == 400 and "colour" in r.text
    assert "Should not stick" not in site.get("/").text
    with site.st.maker() as s:
        assert s.get(Setting, "app_name") is None


def test_only_admins_can_change_settings_and_csrf_is_enforced(site):  # noqa: F811
    login_admin(site)
    bob = fresh(site)
    signup(bob, "bob")
    assert bob.get("/admin/settings").status_code == 403
    t = token(bob, "/me")
    assert bob.post("/admin/settings", data={"csrf": t, "app_name": "Hacked"}).status_code == 403
    assert site.post("/admin/settings", data={"app_name": "NoToken"}).status_code in (403, 422)
    assert bob.get("/admin/pages").status_code == 403 and bob.post("/admin/pages", data={"csrf": t}).status_code == 403


def test_reset_to_default(site):  # noqa: F811
    login_admin(site)
    vals = all_defaults(site)
    vals["app_name"] = "Vault"
    post_settings(site, **vals)
    assert "Vault" in site.get("/").text
    t = token(site, "/admin/settings")
    assert site.post("/admin/settings/reset/app_name", data={"csrf": t}).status_code == 303
    assert "Vault" not in site.get("/").text
    assert site.post("/admin/settings/reset/not_a_setting", data={"csrf": t}).status_code == 404


def test_settings_apply_live_across_processes_and_drive_behaviour(tmp_path):
    url = f"sqlite:///{tmp_path}/shared.db"
    a = main.create_app(build_settings(database_url=url, bootstrap_admin_token="a" * 40))
    b = main.create_app(build_settings(database_url=url, bootstrap_admin_token="a" * 40))
    with TestClient(a, follow_redirects=False) as ca, TestClient(b, follow_redirects=False) as cb:
        signup(ca, "alice")
        t = token(ca, "/admin/settings")
        vals = all_defaults(ca)
        vals.update(registration="closed", auto_approve_reputation="7")
        assert ca.post("/admin/settings", data={"csrf": t, **vals}).status_code == 303
        assert a.state.atlas.ctx.settings.auto_approve_reputation == 7
        b.state.atlas._checked = 0  # the other process re-checks within seconds; skip the wait
        assert cb.get("/register").status_code == 403
        assert b.state.atlas.ctx.settings.registration == "closed"


def test_logo_upload_validation_and_display(site):  # noqa: F811
    login_admin(site)
    t = token(site, "/admin/settings")
    ok = site.post("/admin/branding/logo", data={"csrf": t}, files={"file": ("logo.png", png(), "image/png")})
    assert ok.status_code == 303
    html = site.get("/").text
    digest = re.search(r'<img src="/media/([0-9a-f]{64})" alt="">', html).group(1)
    served = site.get(f"/media/{digest}")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    for name, data in (("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>"),
                       ("x.png", b"not an image at all"), ("big.png", png(2000, 10)),
                       ("huge.png", b"\x89PNG\r\n\x1a\n" + b"0" * 600_000)):
        r = site.post("/admin/branding/logo", data={"csrf": t}, files={"file": (name, data, "image/png")})
        assert r.status_code == 400, name
    assert site.post("/admin/branding/logo/remove", data={"csrf": t}).status_code == 303
    assert "/media/" not in site.get("/").text.split("<main>")[0]  # the logo is gone from the header


def test_default_pages_exist_and_markdown_is_safe(site):  # noqa: F811
    assert "Contribution rules" in site.get("/p/rules").text
    login_admin(site)
    t = token(site, "/admin/pages/new")
    body = "# Hello\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1)) [good](https://example.org)\n\n![x](https://evil.test/a.png)"
    r = site.post("/admin/pages", data={"csrf": t, "slug": "welcome", "title": "Welcome", "body": body, "published": "on",
                                        "in_nav": "on", "position": "5"})
    assert r.status_code == 303
    page = site.get("/p/welcome").text
    assert "<h1>Hello</h1>" in page and "<script>alert(1)" not in page and "&lt;script&gt;" in page
    assert 'href="javascript:' not in page and "<img src=\"https://evil" not in page
    assert 'href="https://example.org" rel="noopener noreferrer nofollow"' in page
    assert 'href="/p/welcome"' in site.get("/").text  # in the menu


def test_draft_pages_are_hidden_and_slugs_validated(site):  # noqa: F811
    login_admin(site)
    t = token(site, "/admin/pages/new")
    site.post("/admin/pages", data={"csrf": t, "slug": "secret", "title": "Secret", "body": "hidden"})  # unpublished
    assert fresh(site).get("/p/secret").status_code == 404
    for slug in ("Bad Slug", "../etc", "new", "a/b", ""):
        r = site.post("/admin/pages", data={"csrf": t, "slug": slug, "title": "x"})
        assert r.status_code in (400, 422), slug
    assert site.post("/admin/pages/secret/delete", data={"csrf": t}).status_code == 303
    with site.st.maker() as s:
        assert s.get(Page, "secret") is None


def test_email_verification_default_depends_on_role():
    assert build_settings(role="authority", email_verification=None).verification_mode() == "required"
    assert build_settings(role="local", email_verification=None).verification_mode() == "optional"
    assert build_settings(role="authority", email_verification="off").verification_mode() == "off"



def test_saving_the_form_unchanged_does_not_drift_percent_settings(site):  # noqa: F811
    """Regression: the form once displayed 85 as 8500 after a save, so each save rewrote the thresholds."""
    login_admin(site)
    vals = all_defaults(site)
    assert vals["auto_match_threshold"] == "85" and vals["review_threshold"] == "60"
    vals.update(auto_match_threshold="90", registration="open", mail_backend="outbox", smtp_starttls="on",
                email_verification="")
    assert post_settings(site, **vals).status_code == 303
    again = all_defaults(site)
    assert again["auto_match_threshold"] == "90" and again["review_threshold"] == "60"
    again.update(registration="open", mail_backend="outbox", smtp_starttls="on", email_verification="")
    assert post_settings(site, **again).status_code == 303
    assert all_defaults(site)["auto_match_threshold"] == "90"
