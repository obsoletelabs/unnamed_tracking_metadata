"""C14-4 template lint (owner refinement 2026-11-10, "invisible topology"): no user-facing template shows which node
holds or served anything. "Served by", "also on <node>", "available on <sibling>", node ids, node names and key
fingerprints appear only on admin pages and in the few account-security ceremonies listed below, each with the
reason it must name a node. A new template, or a new hit in an existing one, fails this test until it is moved to
an admin page or (with a reason a reviewer accepts) added to an allowlist.

The rendered-page counterpart (the same markers absent from real pages of a three-node super group) is
`tests/test_c14_super_group.py::test_user_pages_show_no_node_identity` and the federation lab scenario."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "atlas" / "web" / "templates"

#: What counts as node identity in a template's source (Jinja comments are ignored).
IDENTITY = re.compile(
    r"served[\s_-]*by|also[\s_-]+on\b|available\s+on\s+(?:your|another|other|sibling)|\bnode_id\b|fingerprint|"
    r"\bfp\b|gc_fp|\bmember\.name\b|\bm\.node\b|\bmember_names?\b|\borigin_node\b|\bsource_scope\b|\bhelper\b|"
    r"\.node\s*\}\}|\bnode_name\b|data-sync-member|\bsigner\b|data-sg-member|data-group-bar", re.I)

#: Admin partials: no `admin` prefix for history's sake, included only by admin pages (checked below).
ADMIN_PARTIALS = {
    "_group_grants.html": "the group grants table of Admin -> Node group and Admin -> Group grants",
    "_group_sync.html": "the per-member sync strip; people see `_sync_status.html` instead (C14-4)",
}

#: User-facing templates that must name a node, and why. Nothing here is group topology shown in passing: each is
#: a step where the person decides to trust a specific node, or reviews where their account is signed in.
CEREMONIES = {
    "login.html": "group sign-in buttons (C10 §5.5): the person picks the node that vouches for them and checks its "
                  "fingerprint; shown only when the operator turned `group_signin_public` on (off by default)",
    "group_link_consent.html": "account-link consent (C10 §5.3): the fingerprint is the anti-phishing check",
    "group_signin_consent.html": "group sign-in consent (C10 §6): the fingerprint is the anti-phishing check",
    "me_linked_nodes.html": "the account-link ceremony itself (C10 §5.1: one account by explicit link, never email "
                            "matching); its sync summary is the identity-free `_sync_status.html`",
    "security.html": "the person's own sessions, including those opened through another node, so a stolen "
                     "session can be recognised and ended (C10 §6.3)",
    "link_approve.html": "a main Atlas approving an outside node's link (C14 §6.3): the full fingerprint and the "
                         "node's confirmation code are the anti-phishing control; not group topology",
    "me_links.html": "the person's own outside nodes linked to this main (C14 §6.6), with fingerprints to revoke "
                     "the right one; not group topology",
    "review.html": "reviewers only (review queue): `via node` names the outside node a contribution came through, "
                   "which the reviewer must judge (C14 §7.4); not group topology",
    "relations_edit.html": "reviewers only: `origin_node` is tested to show 'from upstream'; no node is named",
}


def _source(name: str) -> str:
    return re.sub(r"\{#.*?#\}", "", (TEMPLATES / name).read_text(encoding="utf-8"), flags=re.S)


def _admin(name: str) -> bool:
    return name.startswith(("admin", "_admin")) or name in ADMIN_PARTIALS


def _all() -> list[str]:
    return sorted(p.name for p in TEMPLATES.glob("*.html"))


@pytest.mark.parametrize("name", [n for n in _all() if not _admin(n) and n not in CEREMONIES])
def test_user_facing_template_names_no_node(name):
    hits = sorted({m.group(0) for m in IDENTITY.finditer(_source(name))})
    assert not hits, f"{name} shows node identity ({', '.join(hits)}): move it to an admin page (C14-4)"


def test_allowlists_are_current_and_justified():
    names = set(_all())
    for allow in (ADMIN_PARTIALS, CEREMONIES):
        for name, why in allow.items():
            assert name in names, f"{name} is allowlisted but no longer exists"
            assert len(why) > 30, f"{name} needs a real justification"
    for name in CEREMONIES:  # a ceremony that no longer names a node leaves the list
        assert IDENTITY.search(_source(name)), f"{name} no longer names a node: drop it from CEREMONIES"


def test_admin_partials_are_only_included_by_admin_pages_or_behind_the_admin_guard():
    include = re.compile(r'\{%\s*include\s+"([^"]+)"')
    for name in _all():
        if _admin(name):
            continue
        for line in _source(name).splitlines():
            for target in include.findall(line):
                if _admin(target):
                    assert re.search(r"\{%\s*if\s+p\.is_admin\b[^%]*%\}\s*\{%\s*include\s+\"" + re.escape(target),
                                     line), f"{name} includes admin partial {target} outside an `if p.is_admin`"


def test_user_sync_status_and_base_stay_clean():
    """The two places a person meets the group: the sync summary and the generic syncing line in the frame."""
    for name in ("_sync_status.html", "base.html"):
        assert not IDENTITY.search(_source(name).replace('{% include "_admin_group_bar.html" %}', ""))
    assert "data-syncing" in _source("base.html")
