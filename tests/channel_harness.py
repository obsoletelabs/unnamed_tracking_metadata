"""Helpers for the private group channel tests (CONTRACT-C10 §3.3-§4.6): a group of real Atlas apps, accounts
linked through a signed link assertion, and sync rounds. Every key and password is a synthetic runtime value."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from atlas import group_channel, groups, main, profile
from atlas.accounts import register
from atlas.api import group_routes
from atlas.models import (
    GroupAccountLink,
    GroupChange,
    GroupIdpConsent,
    ProfileConflict,
    ProfileField,
    ProfileVersion,
    User,
)
from atlas.service import Ctx

from .group_harness import create, enrol, finish_creation

PASSWORD = "synthetic-Pass-for-tests-4821"


def group_of(*nodes, scopes=None):
    """The first node creates the group; every other node enrols through it."""
    first = nodes[0]
    create(first)
    finish_creation(first)
    for n in nodes[1:]:
        enrol(first, n, scopes)
    for n in nodes:
        group_routes_pull(n)
    return nodes


def group_routes_pull(node) -> dict:
    with node.session() as s:
        out = group_routes.pull(s, node.settings, node.identity, groups.load(s), node.net)
        s.commit()
        return out


def user(node, name: str) -> str:
    with node.session() as s:
        u = register(s, node.settings, name, PASSWORD, f"{name}@{node.name}.example.invalid")
        s.commit()
        return u.id


def uid_of(node, user_id: str) -> str | None:
    with node.session() as s:
        row = s.scalars(select(GroupAccountLink).where(GroupAccountLink.user_id == user_id)).first()
        return row.account_uid if row else None


def mint(node, user_id: str) -> str:
    with node.session() as s:
        link = group_channel.mint_account(s, node.settings, groups.load(s), node.identity, s.get(User, user_id))
        s.commit()
        return link.account_uid


def assertion(idp, idp_user: str, rp, now: datetime | None = None, **over) -> dict:
    """A link assertion signed by `idp` for `rp` (what §5.3 step 7 seals to the relying party)."""
    with idp.session() as s:
        group = groups.load(s)
        uid = uid_of(idp, idp_user) or mint(idp, idp_user)
        s.merge(GroupIdpConsent(user_id=idp_user, group_id=group.group_id, rp_node=rp.node_id,
                                granted_at=now or groups.utcnow()))
        a = group_channel.sign_assertion(s, group, idp.identity, aud_node=rp.node_id, uid=over.pop("uid", uid),
                                         nonce="n" * 32, state_hash="s" * 43, auth_time=now or groups.utcnow(),
                                         amr=["pwd"], username_hint="tester", now=now)
        s.commit()
        return a


def link(idp, idp_user: str, rp, rp_user: str, now: datetime | None = None) -> str:
    """Link `rp_user` on `rp` to the group account of `idp_user` on `idp`. Returns the account_uid."""
    a = assertion(idp, idp_user, rp, now)
    with rp.session() as s:
        group = groups.load(s)
        group_channel.complete_link(s, rp.settings, group, rp.identity, s.get(User, rp_user),
                                    a["body"]["account_uid"], a, now=now)
        s.commit()
    return a["body"]["account_uid"]


def sync(node, now: datetime | None = None) -> dict:
    with node.session() as s:
        out = group_channel.sync(s, node.settings, node.identity, groups.load(s), node.net, group_routes.call, now=now)
        s.commit()
        return out


def rounds(*nodes, n: int = 2) -> None:
    for _ in range(n):
        for node in nodes:
            sync(node)


def full_round(node) -> dict:
    return main.group_round(node.st.maker, Ctx(node.settings, node.identity), node.net)


def edit(node, user_id: str, field: str, value, now: datetime | None = None):
    with node.session() as s:
        row = profile.set_local(s, node.settings, node.identity, s.get(User, user_id), field,
                                profile.CLEAR if value is profile.CLEAR else value, now=now)
        s.commit()
        return row.hlc, row.entry_ref


def shown(node, user_id: str, field: str):
    with node.session() as s:
        row = s.get(ProfileField, (user_id, field))
        return None if row is None else row.value


def heads(node, user_id: str, field: str) -> list[str]:
    with node.session() as s:
        return [h.hlc for h in profile.heads(s, user_id, field)]


def conflicts(node, user_id: str, status: str = "open") -> list[tuple]:
    with node.session() as s:
        return sorted((r.field, r.kind, r.winner_hlc, r.loser_hlc) for r in s.scalars(select(ProfileConflict).where(
            ProfileConflict.user_id == user_id, ProfileConflict.status == status)))


def changes(node, **where) -> list[GroupChange]:
    with node.session() as s:
        q = select(GroupChange)
        for k, v in where.items():
            q = q.where(getattr(GroupChange, k) == v)
        rows = list(s.scalars(q.order_by(GroupChange.origin, GroupChange.epoch, GroupChange.scope, GroupChange.seq)))
        s.expunge_all()
        return rows


def versions(node, user_id: str, field: str) -> list[ProfileVersion]:
    with node.session() as s:
        rows = list(s.scalars(select(ProfileVersion).where(ProfileVersion.user_id == user_id,
                                                           ProfileVersion.field == field)))
        s.expunge_all()
        return rows


def entries_of(src, origin: str, scope: str) -> list[dict]:
    """`src`'s stored entries of one stream, as `changes` would serve them unelided."""
    with src.session() as s:
        return [group_channel.row_entry(r) for r in s.scalars(select(GroupChange).where(
            GroupChange.origin == origin, GroupChange.scope == scope).order_by(GroupChange.epoch, GroupChange.seq))]


def deliver(dst, entries: list[dict], accept=None, now: datetime | None = None, served_by: str | None = None):
    """Run `dst`'s receiver over the entries above its high-water mark, at `now` (trust evaluated then too)."""
    from atlas import profile_schema

    now = now or groups.utcnow()
    with dst.session() as s:
        group = groups.load(s)
        rx = group_channel.Rx(s, dst.settings, dst.identity, group,
                              groups.trust(s, group, now, own_node=dst.node_id), now, served_by)
        outs = []
        for e in entries:
            stream = s.get(group_channel.GroupStream, (group.group_id, e["origin"], e["epoch"], e["scope"]))
            if stream is not None and e["seq"] <= stream.hwm:
                continue
            out = group_channel.receive(rx, e, set(profile_schema.fields() if accept is None else accept))
            outs.append((e["seq"], out.disposition, out.reason))
            if out.stop:
                break
        s.commit()
        return outs
