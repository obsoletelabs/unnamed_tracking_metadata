"""Node group polish (SPEC-UI-POLISH-PASS §4-§5, CONTRACT-C14 H59-H73 defaults): the invite QR code and pasted
invites (T-Q1-Q6), the "Same owner, full trust" preset (T-P1-P9) and the first-class group page (T-F4, T-F5).
Two in-process nodes, synthetic names only."""

from __future__ import annotations

import json
import re
from html import unescape

import pytest
import segno
from sqlalchemy import select

from atlas import group_presets, groups, qr, runtime
from atlas.models import AuditEntry, GroupEnrolment
from atlas.web import CSP
from atlas.web import group as group_web

from .group_harness import ROOT_PW
from .test_group_ui import create_via_ui, csrf, ui  # noqa: F401  (fixture)
from .test_web import PW

POLICY = ("group_auto_provision", "group_trust_remote_mfa", "group_federated_privilege", "group_max_delegated_role")


def policy(node) -> dict:
    with node.session() as s:
        eff = runtime.effective(node.settings, runtime.load(s))
    return {k: getattr(eff, k) for k in POLICY}


CAUTIOUS = {"group_auto_provision": False, "group_trust_remote_mfa": False, "group_federated_privilege": "deny",
            "group_max_delegated_role": "none"}
FULL = {**CAUTIOUS, "group_auto_provision": True, "group_trust_remote_mfa": True}
FULL_ADMIN = {**FULL, "group_federated_privilege": "local_mfa", "group_max_delegated_role": "admin"}


def invite(ca, **form):
    r = ca.post("/admin/group/invite", data={"csrf": csrf(ca, "/admin/group"), **form})
    return r


def code_of(page: str) -> str:
    return re.search(r"data-invite-code>(AG1-[^<]+)<", page).group(1)


def payload_of(page: str) -> str:
    return unescape(re.search(r'<pre class="small" id="invite-payload">([^<]+)</pre>', page).group(1))


def join(cb, url="http://a.test", code="", **form):
    return cb.post("/admin/group/join", data={"csrf": csrf(cb, "/admin/group/join"), "url": url, "code": code,
                                              "name": "Laptop", **form})


def finish(a, ca, cb):
    """Both administrators compare and confirm; returns the enrolment id."""
    with a.session() as s:
        eid = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")
                        .order_by(GroupEnrolment.created_at.desc())).first().id
    ok = ca.post(f"/admin/group/enrolment/{eid}/confirm", data={
        "csrf": csrf(ca, f"/admin/group/enrolment/{eid}"), "matches": "on", "root_passphrase": ROOT_PW,
        "password": PW})
    assert ok.status_code == 303, ok.text
    done = cb.post(f"/admin/group/enrolment/{eid}/confirm",
                   data={"csrf": csrf(cb, f"/admin/group/enrolment/{eid}"), "matches": "on"})
    assert done.status_code == 303, done.text
    return eid


# ---- QR code (§5) ------------------------------------------------------------------------------------------

def test_invite_renders_qr_svg(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    r = invite(ca, preset="same_owner")
    assert r.status_code == 200
    payload = payload_of(r.text)
    data = json.loads(payload)
    assert data["url"] == "http://a.test" and data["code"] == code_of(r.text)
    figure = re.search(r"<figure class=\"qr-plate\" data-invite-qr>(.*?)</figure>", r.text, re.S).group(1)
    assert "<svg" in figure
    # the drawn matrix is exactly the payload's (segno has no decoder: compare with a fresh encoding)
    assert qr.svg(payload, label="QR code with this node's address and the invite code") in r.text
    assert segno.make(payload, error="m").designator  # encodable at the level qr.svg uses


def test_qr_svg_is_csp_safe(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    r = invite(ca, preset="cautious")
    svg = re.search(r"<svg.*?</svg>", r.text, re.S).group(0)
    for banned in ("style", "<script", "href", "foreignObject"):
        assert banned not in svg, banned
    assert not re.search(r"\son\w+=", svg)
    assert r.headers["Content-Security-Policy"] == CSP


def test_qr_fallback_when_unencodable(ui, monkeypatch):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)

    def overflow(*_a, **_k):
        raise ValueError("data too large")

    monkeypatch.setattr(group_web.qr, "svg", overflow)
    r = invite(ca, preset="cautious")
    assert r.status_code == 200 and "data-invite-qr-missing" in r.text and 'role="status"' in r.text
    assert "data-invite-qr>" not in r.text and code_of(r.text) and "http://a.test" in r.text


def test_join_accepts_pasted_payload(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    page = invite(ca, preset="cautious").text
    r = join(cb, url=payload_of(page))
    assert r.status_code == 303, r.text
    with b.session() as s:
        row = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "joiner")).one()
        assert row.peer_url == "http://a.test"


def test_qr_page_not_cacheable(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    assert invite(ca, preset="cautious").headers["Cache-Control"] == "private, no-store"


def test_join_payload_length_and_url_policy(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    code = code_of(invite(ca, preset="cautious").text)
    padded = json.dumps({"v": 1, "type": "atlas-group-invite", "url": "http://a.test", "code": code, "pad": "x" * 380})
    assert 400 < len(padded) <= 600
    assert join(cb, url=padded).status_code == 303
    long_url = json.dumps({"v": 1, "type": "atlas-group-invite", "url": "http://a.test/" + "p" * 300, "code": code})
    assert join(cb, url=long_url).status_code == 400
    typed = join(cb, url="http://user:pw@a.test", code=code)
    pasted = join(cb, url=json.dumps({"v": 1, "type": "atlas-group-invite", "url": "http://user:pw@a.test", "code": code}))
    assert typed.status_code == pasted.status_code == 400


# ---- the preset (§4) ---------------------------------------------------------------------------------------

def test_preset_same_owner_sets_table(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    code = code_of(invite(ca, preset="same_owner").text)
    assert join(cb, code=code, preset="cautious").status_code == 303
    with a.session() as s:
        eid = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one().id
    sas = ca.get(f"/admin/group/enrolment/{eid}").text
    assert "data-preset-changes" in sas and "Same owner, full trust" in sas
    assert policy(a) == CAUTIOUS  # nothing until the join completes
    finish(a, ca, cb)
    assert policy(a) == FULL  # the inviter chose full trust
    assert policy(b) == CAUTIOUS  # the joiner chose cautious: each side sets only itself
    done = ca.get(f"/admin/group/enrolment/{eid}").text
    assert 'data-enrol-step="done"' in done and 'data-preset-applied="same_owner"' in done
    assert 'data-enrol-link="offer"' in done and "/admin/group/enrolment/" + eid + "/link" in done
    with a.session() as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "group_policy_preset")).first() is not None


def test_preset_never_grants_admin_without_checkbox(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    invite(ca, preset="same_owner", grant_roles="admin", delegate_enrol="3")
    with a.session() as s:
        row = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one()
        assert "grant.issue" not in row.scopes and "group.enrol" not in row.scopes and not row.preset_admin
    assert policy(a)["group_max_delegated_role"] == "none"


def test_preset_admin_requires_typed_confirmation(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    r = invite(ca, preset="same_owner", with_admin="on")
    assert r.status_code == 400
    ok = invite(ca, preset="same_owner", with_admin="on", confirm_admin=group_web.ADMIN_CONFIRM)
    assert ok.status_code == 200 and "+ administrators" in ok.text


def test_with_admin_never_delegates_grant_issue(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    page = invite(ca, preset="same_owner", with_admin="on", confirm_admin=group_web.ADMIN_CONFIRM).text
    with a.session() as s:
        row = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one()
        assert list(row.scopes) == list(groups.DEFAULT_SCOPES) and row.preset_admin
    assert join(cb, code=code_of(page), preset="custom").status_code == 303
    finish(a, ca, cb)
    assert policy(a) == FULL_ADMIN
    with a.session() as s:  # no grant until the grants form is used
        assert s.scalars(select(AuditEntry).where(AuditEntry.action.like("%grant_issue%"))).first() is None


def test_cancelled_enrolment_changes_no_policy(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    code = code_of(invite(ca, preset="same_owner").text)
    assert join(cb, code=code, preset="same_owner").status_code == 303
    with a.session() as s:
        eid = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one().id
    r = ca.post(f"/admin/group/enrolment/{eid}/cancel", data={"csrf": csrf(ca, f"/admin/group/enrolment/{eid}")})
    assert r.status_code == 303
    assert policy(a) == CAUTIOUS and policy(b) == CAUTIOUS


def test_custom_preset_is_todays_behaviour(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    invite(ca, delegate_enrol="0")  # an old form post: no preset field
    with a.session() as s:
        row = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")).one()
        assert list(row.scopes) == list(groups.DEFAULT_SCOPES) and row.preset is None
    invite(ca, delegate_enrol="2", preset="custom")
    with a.session() as s:
        rows = s.scalars(select(GroupEnrolment).where(GroupEnrolment.side == "inviter")
                         .order_by(GroupEnrolment.created_at)).all()
        assert "group.enrol" in rows[-1].scopes and rows[-1].preset is None


def test_policy_page_names_current_preset(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    page = ca.get("/admin/group").text
    assert re.search(r'data-preset-now="cautious"', page) and "Cautious" in page
    r = ca.post("/admin/group/policy", data={"csrf": csrf(ca, "/admin/group"), "preset": "same_owner"})
    assert r.status_code == 303
    assert 'data-preset-now="same_owner"' in ca.get("/admin/group").text
    assert policy(a) == FULL


def test_preset_values_table():
    for preset, expected in (("cautious", CAUTIOUS), ("same_owner", FULL)):
        got = group_presets.values(preset)
        assert {k: got[k] for k in POLICY} == expected
        assert got["group_signin_public"] is False and got["group_delegated_requires_mfa"] is True
    got = group_presets.values("same_owner", with_admin=True)
    assert {k: got[k] for k in POLICY} == FULL_ADMIN


# ---- the group page (§3.3-3.5) -----------------------------------------------------------------------------

def test_group_page_sections_and_danger_zone(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    page = ca.get("/admin/group").text
    for anchor in ('id="overview"', 'id="add-node"', 'id="policy"', "data-danger-zone"):
        assert anchor in page, anchor
    zone = page[page.index("data-danger-zone"):]
    before = page[:page.index("data-danger-zone")]
    for action in ("/revoke", "/admin/group/leave", "/admin/group/renew"):
        assert action not in before, action
    assert "/admin/group/leave" in zone or "/admin/group/renew" in zone


def test_group_health_counts(ui):  # noqa: F811
    a, b, ca, cb = ui
    create_via_ui(a, ca)
    code = code_of(invite(ca, preset="cautious").text)
    join(cb, code=code)
    finish(a, ca, cb)
    page = ca.get("/admin/group").text
    m = re.search(r'data-group-health="(\w+)"', page)
    assert m and m.group(1) in ("ok", "warn", "bad")
    assert re.search(r'data-group-health-status="active"[^>]*>\s*(?:<a[^>]*>)?<span class="stat-value">2<', page)


@pytest.mark.parametrize("path", ["/admin/group", "/admin/group/join"])
def test_group_pages_render_for_admin(ui, path):  # noqa: F811
    a, b, ca, cb = ui
    assert ca.get(path).status_code == 200
