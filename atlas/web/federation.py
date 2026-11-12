"""Admin: peers (replication) and API tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from .. import __version__, accounts, auth, feed, feed_schema, key_rotation, peer_probe, secrets_box, service, sync
from ..models import FollowerWire, NodeState, Peer, ReplicatedFeed, SyncCursor, SyncFailure, Token, User
from ..service import AtlasError
from ..signing import id_matches, load_identity, signing_key_at_rest
from .deps import LOCAL_ADMIN_ONLY, admin_form, admin_page, go, local_admin_form, throttle
from .deps import Page as Ctx
from .routes import render

router = APIRouter()


def _keys(p: Ctx, error=None, status=200):
    identity = load_identity(p.session, p.st.ctx.settings)
    return render(p, "admin_keys.html", status=status, identity=identity, at_rest=signing_key_at_rest(p.session),
                  history=key_rotation.own_history(p.session, identity.node_id), error=error,
                  has_password=p.me.password_hash != "!")


@router.get("/admin/keys", response_class=HTMLResponse)
def signing_keys(p: Annotated[Ctx, Depends(admin_page)]):
    return _keys(p)


@router.post("/admin/keys/rotate", response_class=HTMLResponse)
def rotate_signing_key(p: Annotated[Ctx, Depends(local_admin_form)], confirm: Annotated[str, Form(max_length=64)],
                       password: Annotated[str, Form(max_length=256)] = "",
                       followers_upgraded: Annotated[str, Form()] = ""):
    throttle(p, f"key-rotation:{p.me.id}", 3, 3600)
    if followers_upgraded != "on":
        return _keys(p, "Upgrade all followers before rotating this key.", status=400)
    if not accounts.reauthenticated(p.me, password, p.login):
        return _keys(p, "Confirm your current password or sign in again with your linked provider.", status=400)
    try:
        successor = key_rotation.rotate(p.session, confirm.strip(), p.st.ctx.settings)
    except AtlasError as exc:
        return _keys(p, str(exc), status=409)
    accounts.audit(p.session, p.me.id, "signing_key_rotated", successor.node_id,
                   {"previous": confirm.strip(), "current": successor.key_id})
    p.st.ctx.identity = successor
    from .. import group_channel, groups

    group = groups.active(p.session)
    opened = group is not None and group_channel.current_epoch(p.session, group, successor.node_id) is not None
    if opened and group_channel.may_emit(p.session, group, successor):
        group_channel.ensure_epoch(p.session, group, successor)  # the rotation epoch (§1.7)
    return go("/admin/keys", "saved")


def clean_url(raw: str) -> str:
    try:
        return peer_probe.clean_url(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


fingerprint = peer_probe.fingerprint


FAILURES_SHOWN = 50


def _protocol(p: Ctx) -> dict:
    """This node's wire range and, per follower token, the wire it was served at, what was omitted for it and why it
    was refused (design §4.6: "Follower {name} on wire {use}: features {Y, Z} not available to it (since seq N)")."""
    from .. import wire
    from ..models import FollowerWire

    names = {t.id: t.name for t in p.session.scalars(select(Token))}
    followers = []
    for row in p.session.scalars(select(FollowerWire).order_by(FollowerWire.last_seen_at.desc()).limit(100)):
        followers.append({"name": names.get(row.token_id, "(deleted token)"), "use": row.use,
                          "range": f"{row.wire_min}-{row.wire_max}" if row.wire_min is not None else None,
                          "omitted": row.omitted or [], "omitted_since": row.omitted_since, "refused": row.refused,
                          "legacy": row.legacy_protocol, "seen": row.last_seen_at,
                          "behind": bool(row.refused or row.omitted)})
    prof = wire.profile(p.st)
    return {"wire": prof.advertised(), "followers": followers}


STATUS_LABELS = sync.PEER_STATUS_LABELS
#: C13 §8.2: what each kind of open entry is called on Admin → Peers, its one action and how it is retried.
CAUSES = {("parked", None): ("needs_upgrade", "Needs a newer Atlas", "Upgrade Atlas; they apply on their own after"),
          ("quarantined", "invalid"): ("malformed", "Sent something Atlas can't accept",
                                       "Nothing to retry: the signed entry stays retained (atlas sync skip)"),
          ("quarantined", "skipped"): ("skipped", "Skipped by an administrator", "Undo with atlas sync replay"),
          ("quarantined", "media_type_not_adopted"): ("media_type_not_adopted",
                                                      "Uses a media type this node does not adopt",
                                                      "Create the type locally, or dismiss"),
          ("quarantined", None): ("apply_failed", "Couldn't be applied (likely a bug)", "Retry now")}


def _cause(disposition: str, reason: str | None) -> tuple[str, str, str]:
    return CAUSES.get((disposition, reason)) or CAUSES.get((disposition, None)) or CAUSES[("quarantined", None)]


def _failure_groups(p: Ctx) -> list[dict]:
    """C13 §8.1-§8.2: the open parked and quarantined entries grouped by peer feed and cause, each with its count,
    heading and one action (the rows themselves stay in the table under it)."""
    rows = p.session.execute(select(SyncFailure.node_id, SyncFailure.disposition, SyncFailure.reason, func.count(),
                                    func.min(SyncFailure.seq), func.max(SyncFailure.seq))
                             .where(SyncFailure.resolved_at.is_(None), SyncFailure.disposition != sync.WAIT)
                             .group_by(SyncFailure.node_id, SyncFailure.disposition, SyncFailure.reason)).all()
    names = {n: name for n, name in p.session.execute(select(Peer.node_id, Peer.name))}
    groups: dict[tuple[str, str], dict] = {}
    for node, disposition, reason, n, first, last in rows:
        code, heading, action = _cause(disposition, reason)
        g = groups.setdefault((node, code), {"node_id": node, "peer": names.get(node), "cause": code,
                                             "heading": heading, "action": action, "count": 0, "first": first,
                                             "last": last})
        g["count"] += n
        g["first"], g["last"] = min(g["first"], first), max(g["last"], last)
    return sorted(groups.values(), key=lambda g: (g["node_id"], g["first"]))


def _waiting(p: Ctx) -> dict:
    """C13 §8.1: entries waiting for a record, grouped by peer feed and by the record they wait for, each with its
    sub-reason and the note to copy for the upstream's operator."""
    from .. import assist, scope
    from .. import groups as owner_groups

    names = {n: name for n, name in p.session.execute(select(Peer.node_id, Peer.name))}
    group = owner_groups.active(p.session) if scope.in_group(p.session) else None
    groups = []
    for g in sync.waiting_groups(p.session):
        g["peer"] = names.get(g["node_id"])
        g["since_dt"] = datetime.fromisoformat(g["since"]) if g["since"] else None
        if g["subreason"] == sync.UPSTREAM_DEFECT and g["peer"]:
            g["label"] = f"Waiting for its game: {g['peer']}'s feed is missing it"
        # C14 §3.2/§3.5: what the owner group said about it (admin page: member names are fine here)
        g["assist"] = (assist.waiting_line(p.session, g["canonical_id"], g["node_id"], group.group_id)
                       if group is not None else None)
        groups.append(g)
    return {**sync.open_waiting(p.session), "groups": groups, "in_group": group is not None,
            "asking": bool(getattr(p.st.ctx.settings, "group_assist_ask", True))}


def _media_line(p: Ctx, peer: Peer) -> dict | None:
    """C13 §6.5: images of the rows this peer's feed delivered: how many are here, fetched from the peer or from
    their origin, and how many could not be fetched (the reasons are on each game's artwork page)."""
    from ..models import Artwork

    rows = p.session.execute(select(Artwork.blob_hash.is_not(None), Artwork.fetched_from, Artwork.fail_count)
                             .where(Artwork.origin_node == peer.node_id)).all()
    if not rows:
        return None
    label = f"peer:{peer.id}"
    withheld = p.session.get(NodeState, "sync_media_withheld:" + peer.node_id)
    return {"withheld": int(withheld.value) if withheld is not None and withheld.value.isdigit() else None,
            "total": len(rows), "cached": sum(1 for c, _, _ in rows if c),
            "from_peer": sum(1 for c, f, _ in rows if c and f == label),
            "from_origin": sum(1 for c, f, _ in rows if c and f == "origin"),
            "failing": sum(1 for c, _, n in rows if not c and n)}


def _records(p: Ctx) -> dict | None:
    """C13 §4.2 on an authority: live records its feed names nowhere, and whether it can publish them now."""
    if p.st.ctx.settings.role != "authority":
        return None
    out = feed.records_summary(p.session)
    out.update(missing=feed.unintroduced_count(p.session, "missing"), mode=feed.introduction_mode(p.st.ctx.settings),
               pending=p.session.get(NodeState, feed.INTRODUCE_PENDING) is not None,
               introduced_through=feed.introduced_through(p.session))
    return out


@router.post("/admin/peers/assist/ask")
def assist_ask(p: Annotated[Ctx, Depends(admin_form)], cid: Annotated[str, Form(max_length=40)]):
    """"Ask my nodes" (C14 §3.2): (re)open the record's job now, also after it gave up. Reading a record is the
    right this needs (§3.9); the page is for administrators."""
    from .. import assist, group_channel

    if not assist.ID_RE.match(cid):
        raise HTTPException(400, "Unknown record.")
    if not assist.note_missing(p.session, p.st.ctx.settings, cid, "user", force=True):
        return _peers_page(p, error="This node is not asking its group: it is in no group, or asking is off on "
                                    "Admin → Group.", status=409)
    accounts.audit(p.session, p.me.id, "group_assist_ask", cid, {})
    group_channel.WAKE.set()
    return go("/admin/peers#waiting-entries", "saved")


@router.post("/admin/peers/assist/adopt")
def assist_adopt(p: Annotated[Ctx, Depends(admin_form)], item: Annotated[str, Form(max_length=40)]):
    """"Use what <member> has" (C14 §3.5 class 2): wake the entries that wait, reversibly (`forget`)."""
    from .. import assist

    try:
        assist.adopt(p.session, p.st.ctx, item, p.me.id)
    except assist.AssistError:
        return _peers_page(p, error="That answer is no longer on offer.", status=409)
    return go("/admin/peers#waiting-entries", "saved")


@router.post("/admin/peers/introduce")
def introduce_records(p: Annotated[Ctx, Depends(admin_form)], scope: Annotated[str, Form(max_length=8)] = "missing"):
    """"Publish them": schedule `atlas feed introduce` for the sync worker (C13 §4.2). Audited."""
    if scope not in ("missing", "all"):
        raise HTTPException(400, "Choose missing or all.")
    if p.st.ctx.settings.role != "authority" or feed.introduction_mode(p.st.ctx.settings) == feed.VIA_LEGACY:
        return _peers_page(p, error="Could not publish records: followers on older releases cannot receive them; "
                                    "turn on the compatibility bridge (ATLAS_FEDERATION_BRIDGE_RECORD_OBS) or ask "
                                    "them to upgrade.", status=409)
    feed.schedule_introduce(p.session, scope)
    accounts.audit(p.session, p.me.id, "feed_introduce", "", {"scope": scope})
    return go("/admin/peers", "saved")


@router.get("/admin/peers", response_class=HTMLResponse)
def peers(p: Annotated[Ctx, Depends(admin_page)], probe: dict | None = None, error: str | None = None,
          tone: str | None = None):
    return _peers_page(p, probe, error, tone=tone)


def _flash_tone(error: str | None, status: int) -> str:
    """PE7: the tone of the message line; routes pass it, and old call sites fall back to the status."""
    if not error:
        return "ok"
    if status >= 400 or error.lower().startswith(("could not", "sync failed")):
        return "error"
    return "ok"


def _peer_badge(p: Ctx, peer: Peer, member: bool) -> dict[str, str]:
    """C14 §5.5: what this peer's data is here. A node of your own group is "Your nodes"; any other peer's feed is
    public, with the kind it states (official/community) or Mirror when relayed or unknown."""
    from .. import scope

    if member:
        return scope.badge("group")
    return scope.badge("public", scope.public_kind(p.st.ctx.settings, peer.node_id, p.session))


def _peers_page(p: Ctx, probe: dict | None = None, error: str | None = None, status: int = 200,
                tone: str | None = None):
    rows = []
    from .. import groups

    members: set[str] = set()
    group = groups.active(p.session)
    if group is not None:  # PE5: a peer that is also a member of this node's group
        from ..models import GroupMember

        members = set(p.session.scalars(select(GroupMember.node_id).where(GroupMember.group_id == group.group_id)))
    cursors = {c.upstream: c for c in p.session.scalars(select(SyncCursor))}
    heads = sync.status(p.session, p.st.ctx)["upstreams"]
    for peer in p.session.scalars(select(Peer).order_by(Peer.created_at)):
        cur = cursors.get(peer.node_id)
        h = heads.get(peer.node_id)
        described = sync.describe_peer(p.session, p.st.ctx.settings, peer, h)
        rows.append({"peer": peer, "seq": cur.last_seq if cur else 0, "fp": fingerprint(peer.node_id),
                     "group_member": peer.node_id in members,
                     "scope_badge": _peer_badge(p, peer, peer.node_id in members),
                     "heads": h, "status": described["code"], "badge": described,
                     "inferred": sync.inferred_follow(p.session, peer.id) if peer.follow is None else None,
                     "via_fp": fingerprint(peer.via_node_id) if peer.via_node_id else None,
                     "media": _media_line(p, peer)})
    s = p.st.ctx.settings
    failures = list(p.session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None),
                                                                SyncFailure.disposition != sync.WAIT)
                                      .order_by(SyncFailure.node_id, SyncFailure.seq).limit(FAILURES_SHOWN)))
    return render(p, "admin_peers.html", status=status, rows=rows, failures=failures,
                  failures_open=sync.open_failures(p.session, feed_schema.QUARANTINE)
                  + sync.open_failures(p.session, feed_schema.PARK),
                  parked_open=sync.open_failures(p.session, feed_schema.PARK),
                  waiting=_waiting(p), failure_groups=_failure_groups(p), status_labels=STATUS_LABELS,
                  records=_records(p),
                  next_retry=sync.next_retry_at, own=p.st.ctx.identity, fp=fingerprint(p.st.ctx.identity.node_id),
                  can_store=secrets_box.available(s), env_upstream=s.upstream_url, probe=probe, error=error,
                  flash_tone=tone or _flash_tone(error, status), release=__version__,
                  standalone=not rows and not p.session.scalar(select(func.count()).select_from(FollowerWire))
                  and not probe and not s.upstream_url,
                  inherit=s.inherit_schema, protocol=_protocol(p))


PROBE_DOMAIN = "atlas-peer-probe-v1\n"
FOLLOWS = (sync.FOLLOW_OWN, sync.FOLLOW_RELAYED)
#: C13 §7.1: a probe's choices are valid for ten minutes.
PROBE_TTL = 600
PROBE_SINCE = peer_probe.PROBE_SINCE


def probe_token(p: Ctx, url: str, node_id: str, public_key: str, follow: str, via: str | None, empty: bool,
                expires: int | None = None) -> str:
    """C13 §7.1-§7.3: binds a choice on the choose-feed screen to what the probe saw, in this session (keyed by the
    session's form secret, so a token from another admin session is refused), for ten minutes. It covers the URL,
    the feed's node id and key, the follow mode, the relay and whether the probe found the feed empty, so the add
    form cannot be edited into another feed, mode or acknowledgement rule. `<expiry>.<hex HMAC-SHA256>`."""
    expires = int(datetime.now(UTC).timestamp()) + PROBE_TTL if expires is None else expires
    message = PROBE_DOMAIN + "\n".join((url, node_id, public_key, follow, via or "", "empty" if empty else "",
                                        str(expires)))
    return f"{expires}." + hmac.new(p.csrf.encode(), message.encode(), hashlib.sha256).hexdigest()


def check_probe_token(p: Ctx, token: str, url: str, node_id: str, public_key: str, follow: str, via: str | None,
                      empty: bool) -> bool:
    raw_expiry, _, _mac = token.partition(".")
    try:
        expires = int(raw_expiry)
    except ValueError:
        return False
    if expires < int(datetime.now(UTC).timestamp()):
        return False
    expected = probe_token(p, url, node_id, public_key, follow, via, empty, expires)
    return hmac.compare_digest(expected, token)


def feed_options(p: Ctx, url: str, info: dict, own_head: int | None = None) -> list[dict]:
    """`peer_probe.feed_options` with each option's session-bound `probe_token` (C13 §7.1-§7.3)."""
    return peer_probe.feed_options(p.session, info, own_head, token_for=lambda o: probe_token(
        p, url, o["node_id"], o["public_key"], o["follow"], o["via"], o["empty"]))


@router.post("/admin/peers/probe", response_class=HTMLResponse)
def probe(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
          token: Annotated[str, Form(max_length=200)] = "", name: Annotated[str, Form(max_length=80)] = "",
          feed_node_id: Annotated[str, Form(max_length=64)] = ""):
    target = clean_url(url)
    try:
        result = peer_probe.probe(p.session, p.st.http, target, token.strip() or None, token_for=lambda o: probe_token(
            p, target, o["node_id"], o["public_key"], o["follow"], o["via"], o["empty"]))
        options = result["feeds"]
        if feed_node_id:  # the previous release's relay form: one chosen authority feed
            options = [o for o in options if o["node_id"] == feed_node_id and o["follow"] == sync.FOLLOW_RELAYED]
            if not options:
                raise peer_probe.ProbeError("That authority feed is not available from this relay.")
            for o in options:
                o["recommended"] = not o["disabled"]
    except peer_probe.ProbeError as exc:
        return peers(p, error=f"Could not verify that node: {exc}")  # type: ignore[arg-type]
    if all(o["already"] for o in options):
        return peers(p, error="That node is already a peer.")  # type: ignore[arg-type]
    node = result["node"]
    return peers(p, probe={"url": target, "token": token.strip(), "name": name.strip() or node["name"],  # type: ignore[arg-type]
                           "role": node["role"], "kind": node["kind"], "fp": node["fp"], "node_id": node["node_id"],
                           "options": options, "can_sync": result["token"]["sync"],
                           "https": target.startswith("https://"), "protocol": node["protocol"],
                           "protocol_ok": node["protocol_ok"], "records": result["records"]})


@router.post("/admin/peers")
def add_peer(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
             node_id: Annotated[str, Form(max_length=64)], public_key: Annotated[str, Form(max_length=100)],
             name: Annotated[str, Form(max_length=80)], token: Annotated[str, Form(max_length=200)] = "",
             follow: Annotated[str, Form(max_length=8)] = "", via_node_id: Annotated[str, Form(max_length=64)] = "",
             probe_token_: Annotated[str, Form(alias="probe_token", max_length=128)] = "",
             empty: Annotated[str, Form(max_length=8)] = "",
             ack_relayed_fingerprint: Annotated[str, Form(max_length=8)] = "",
             ack_empty: Annotated[str, Form(max_length=8)] = ""):
    """Pin a peer from the choose-feed screen (C13 §7.3). The form must carry the chosen `follow` mode and the
    `probe_token` the probe issued for it: the node id, key, relay and emptiness it pins are exactly the ones the
    token covers (an edited field fails the HMAC), and the token expires after ten minutes. A relayed feed, or any
    feed reached over plain http, needs `ack_relayed_fingerprint` (the operator compared the fingerprint out of
    band); an empty feed needs `ack_empty`. Anything missing answers 400."""
    target = clean_url(url)
    via = via_node_id.strip() or None
    is_empty = empty == "1"
    if follow not in FOLLOWS:
        raise HTTPException(400, "Choose which feed to follow: look the node up and pick one.")
    if not probe_token_ or not check_probe_token(p, probe_token_, target, node_id, public_key, follow, via, is_empty):
        raise HTTPException(400, "That choice does not match what the node offered, or the lookup expired. "
                                 "Look the node up again.")
    try:
        ok = id_matches(node_id, base64.b64decode(public_key))
    except ValueError:
        ok = False
    if not ok:  # unreachable through a valid token (the probe verified it); kept as a second check
        raise HTTPException(400, "The node id does not match the public key.")
    if (follow == sync.FOLLOW_RELAYED or not target.startswith("https://")) and ack_relayed_fingerprint != "on":
        raise HTTPException(400, f"Confirm that you compared the fingerprint {fingerprint(node_id)} with that "
                                 "node's operator.")
    if is_empty and ack_empty != "on":
        raise HTTPException(400, "This feed has no entries yet. Confirm that you want to follow it anyway.")
    s = p.st.ctx.settings
    enc = None
    if token.strip():
        try:
            enc = secrets_box.encrypt(s, token.strip())
        except secrets_box.SecretsError as exc:
            raise HTTPException(400, str(exc)) from exc
    if p.session.scalars(select(Peer.id).where(Peer.node_id == node_id)).first():
        raise HTTPException(409, "That node is already a peer.")
    retained = p.session.get(ReplicatedFeed, node_id)
    if retained is not None and retained.public_key != public_key:
        raise HTTPException(409, "This feed's key differs from the one this node already retains for it.")
    row = Peer(name=name.strip() or target, url=target, node_id=node_id, public_key=public_key, token_enc=enc,
               follow=follow, via_node_id=via if follow == sync.FOLLOW_RELAYED else None)
    p.session.add(row)
    accounts.audit(p.session, p.me.id, "peer_add", node_id, {"url": target, "follow": row.follow,
                                                            "via": row.via_node_id, "empty": is_empty})
    p.session.commit()  # the peer is saved whatever the first pull does
    return go("/admin/peers#peer-" + row.id, first_pull(p, row, token.strip() or None))


def first_pull(p: Ctx, row: Peer, token: str | None) -> str:
    """C13 §7.3 "Then": pull one page right after adding, so the operator sees the real
    badge at once ("Copying: 23 of 23", "Caught up · 4 changes waiting for their game") instead of "Not contacted
    yet" until the next interval. The probe timeout applies to each request (`/sync/info`, then `/sync/changes`),
    not to the whole call: applying the page and waking waiting entries run inside this request, so it can take
    longer than PROBE_TIMEOUT. A timeout leaves the peer saved and not contacted; any other error is recorded on
    the peer as a sync failure is."""
    try:
        sync.pull_once(p.st.maker, p.st.ctx, p.st.http, row.url, token, {row.node_id: row.public_key},
                       follow=row.follow, peer_id=row.id, timeout=peer_probe.PROBE_TIMEOUT)
        row.last_error, row.last_ok_at = None, datetime.now(UTC)
        note = "peer_following"
    except httpx.TimeoutException:
        note = "peer_not_contacted"
    except Exception as exc:  # recorded on the peer, as the sync loop does
        row.last_error = f"{type(exc).__name__}: {exc}"[:300]
        note = "peer_failed"
    p.session.commit()
    return note


@router.post("/admin/peers/{pid}/follow")
def confirm_follow(pid: str, p: Annotated[Ctx, Depends(admin_form)], follow: Annotated[str, Form(max_length=8)]):
    """C13 §7.4: the operator confirms which feed a peer added before A19 follows (what the pulls inferred, or the
    other mode). Audited; from then on the pull never infers again."""
    row = p.session.get(Peer, pid)
    if row is None:
        raise HTTPException(404, "No such peer.")
    if follow not in FOLLOWS:
        raise HTTPException(400, "Choose own or relayed.")
    inferred = sync.inferred_follow(p.session, pid) or {}
    row.follow = follow
    row.via_node_id = inferred.get("via") if follow == sync.FOLLOW_RELAYED else None
    accounts.audit(p.session, p.me.id, "peer_follow", row.node_id,
                   {"follow": follow, "via": row.via_node_id, "inferred": inferred.get("follow")})
    return go("/admin/peers", "saved")


@router.post("/admin/peers/{pid}/toggle")
def toggle_peer(pid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(Peer, pid)
    if row is None:
        raise HTTPException(404, "No such peer.")
    row.enabled = not row.enabled
    accounts.audit(p.session, p.me.id, "peer_toggle", row.node_id, {"enabled": row.enabled})
    return go("/admin/peers", "saved")


@router.post("/admin/peers/{pid}/delete")
def delete_peer(pid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(Peer, pid)
    if row is None:
        raise HTTPException(404, "No such peer.")
    others = p.session.scalars(select(Peer.id).where(Peer.node_id == row.node_id, Peer.id != row.id)).first()
    abandoned = 0 if others else sync.abandon_waiting(p.session, row.node_id)
    accounts.audit(p.session, p.me.id, "peer_delete", row.node_id, {"abandoned": abandoned} if abandoned else None)
    p.session.delete(row)
    return go("/admin/peers", "saved")


def sync_note(n: int, before: dict | None, after: dict | None) -> str | None:
    """C13 §7.6: what "Sync now" did, honestly: entries fetched, and how many apply, wait or failed."""
    if not after:
        return f"Synced {n} entries." if n else None
    applied = after["applied_head"] - (before or {}).get("applied_head", 0)
    waiting, failing = after["waiting"]["count"], after["quarantined"]["count"]
    if n == 0 and applied == 0:
        if waiting:
            return (f"Nothing new. {waiting} entr{'y waits' if waiting == 1 else 'ies wait'} for records this feed "
                    "has not published yet.")
        return None
    parts = [f"Synced {n} entries", f"{applied} applied"]
    if waiting:
        parts.append(f"{waiting} waiting for records the feed has not published")
    if failing:
        parts.append(f"{failing} could not be applied")
    return ", ".join(parts) + "."


@router.post("/admin/peers/{pid}/sync", response_class=HTMLResponse)
def sync_now(pid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(Peer, pid)
    if row is None:
        raise HTTPException(404, "No such peer.")
    s = p.st.ctx.settings
    before = sync.upstream_status(p.session, p.st.ctx, row.node_id)
    try:
        token = secrets_box.decrypt(s, row.token_enc) if row.token_enc else None
        n = sync.pull_all(p.st.maker, p.st.ctx, p.st.http, row.url, token, max_pages=20,
                          trusted={row.node_id: row.public_key}, follow=row.follow, peer_id=row.id)
        row.last_error, row.last_ok_at = None, datetime.now(UTC)
        p.session.commit()
        after = sync.upstream_status(p.session, p.st.ctx, row.node_id)
        return peers(p, error=sync_note(n, before, after))  # type: ignore[arg-type]
    except Exception as exc:
        row.last_error = f"{type(exc).__name__}: {exc}"[:300]
        p.session.commit()
        return peers(p, error=f"Sync failed: {row.last_error}", tone="error")  # type: ignore[arg-type]


@router.post("/admin/peers/failures/create-type", response_class=HTMLResponse)
def create_type_for_failure(p: Annotated[Ctx, Depends(admin_form)], key: Annotated[str, Form(max_length=40)]):
    """C15 §2.2 "Create type locally": an upstream's entries name a media type this node does not adopt. Create it
    here (an ordinary local type, which an administrator can rename or delete later), then retry: they apply."""
    from .. import custom
    from ..models import MediaType

    key = key.strip().lower()
    if p.session.get(MediaType, key) is None:
        try:
            custom.save_type(p.session, p.st.ctx, key, key.replace("_", " ").capitalize())
        except AtlasError as exc:
            p.session.rollback()
            return peers(p, error=f"Could not create the media type: {exc}", tone="error")  # type: ignore[arg-type]
        accounts.audit(p.session, p.me.id, "schema_type_save", key[:64], {"via": "media_type_not_adopted"})
    result = sync.retry_failures(p.session, p.st.ctx, limit=100, due_only=False)
    accounts.audit(p.session, p.me.id, "sync_failures_retry", "", result)
    p.session.flush()
    return peers(p, error=f"Created the media type {key}; retried {result['retried']} entr"  # type: ignore[arg-type]
                 f"{'y' if result['retried'] == 1 else 'ies'}: {result['resolved']} applied.", tone="ok")


@router.post("/admin/peers/failures/dismiss", response_class=HTMLResponse)
def dismiss_failure(p: Annotated[Ctx, Depends(admin_form)], node_id: Annotated[str, Form(max_length=64)],
                    seq: Annotated[int, Form(ge=1)]):
    """C15 §2.2 "Dismiss": stop reporting an entry whose media type this node does not adopt (audited)."""
    try:
        sync.dismiss_not_adopted(p.session, node_id, seq, actor_id=p.me.id)
    except ValueError as exc:
        return peers(p, error=f"Could not dismiss: {exc}", tone="error")  # type: ignore[arg-type]
    return go("/admin/peers", "saved")


@router.post("/admin/peers/failures/retry", response_class=HTMLResponse)
def retry_failed_entries(p: Annotated[Ctx, Depends(admin_form)]):
    """Re-apply the mirrored entries that failed, whatever their automatic attempt count."""
    service.write_lock(p.session)
    result = sync.retry_failures(p.session, p.st.ctx, limit=100, due_only=False)
    accounts.audit(p.session, p.me.id, "sync_failures_retry", "", result)
    p.session.flush()
    if not result["retried"]:
        return peers(p, error="Nothing to retry.", tone="ok")  # type: ignore[arg-type]
    note = f"Retried {result['retried']} entr{'y' if result['retried'] == 1 else 'ies'}: {result['resolved']} applied"
    if result["still_failing"]:
        note += f", {result['still_failing']} still failing (see the reasons below)"
    return peers(p, error=note + ".", tone="warn" if result["still_failing"] else "ok")  # type: ignore[arg-type]


# ---- tokens -------------------------------------------------------------------------------------------

@router.get("/admin/tokens", response_class=HTMLResponse)
def tokens(p: Annotated[Ctx, Depends(admin_page)], new_token: str | None = None):
    return _tokens_page(p, None)


def _tokens_page(p: Ctx, new_token: str | None, status: int = 200, error: str | None = None):
    rows = []
    for t in p.session.scalars(select(Token).order_by(Token.created_at.desc()).limit(200)):
        owner = p.session.get(User, t.user_id) if t.user_id else None
        rows.append({"t": t, "owner": owner.username if owner else None})
    return render(p, "admin_tokens.html", status=status, rows=rows, new_token=new_token, scopes=auth.SCOPES,
                  self_service=p.st.ctx.settings.self_service_sync_tokens,
                  error=error)


@router.post("/admin/tokens", response_class=HTMLResponse)
def token_create(p: Annotated[Ctx, Depends(admin_form)], name: Annotated[str, Form(max_length=128)],
                 scopes: Annotated[list[str], Form()], days: Annotated[str, Form()] = ""):
    try:
        expires = int(days) if days.strip() else None
        if expires is not None and not 1 <= expires <= 3650:
            raise ValueError
    except ValueError:
        return _tokens_page(p, None, 400, "Expiry must be a number of days between 1 and 3650.")
    if not p.is_local_admin:  # C10 §6.3, §6.4: a token is a standing credential that would outlive a group grant
        return _tokens_page(p, None, 403, LOCAL_ADMIN_ONLY)
    try:
        row, secret = auth.create_token(p.session, name.strip() or "token", scopes, expires)
    except ValueError as exc:
        return _tokens_page(p, None, 400, str(exc))
    accounts.audit(p.session, p.me.id, "token_create", row.id, {"scopes": row.scopes})
    return _tokens_page(p, secret)


@router.post("/admin/tokens/{tid}/revoke")
def token_revoke(tid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(Token, tid)
    if row is None:
        raise HTTPException(404, "No such token.")
    row.revoked_at = row.revoked_at or datetime.now(UTC)
    accounts.audit(p.session, p.me.id, "token_revoke", tid, None)
    return go("/admin/tokens", "revoked")
