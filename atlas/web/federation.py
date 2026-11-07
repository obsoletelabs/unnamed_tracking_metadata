"""Admin: peers (replication) and API tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from .. import accounts, auth, feed, feed_schema, key_rotation, runtime, secrets_box, service, sync
from ..models import FollowerSeen, NodeState, Peer, Setting, SyncCursor, SyncFailure, Token, User
from ..service import AtlasError
from ..signing import key_id_for, load_identity, signing_key_at_rest
from .deps import LOCAL_ADMIN_ONLY, admin_form, admin_page, go, local_admin_form, throttle
from .deps import Page as Ctx
from .routes import render, when

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
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or len(url) > 300 or any(c.isspace() for c in url):
        raise HTTPException(400, "Use a plain http(s) address such as https://atlas.example.org")
    return url


def fingerprint(node_id: str) -> str:
    return " ".join(node_id[i:i + 4] for i in range(0, len(node_id), 4))


FAILURES_SHOWN = 50


def _protocol(p: Ctx) -> dict:
    """This node's federation protocol and the protocols its followers were last seen speaking (C1.6)."""
    s = p.st.ctx.settings
    emit = feed_schema.emit_protocol(s)
    names = {t.id: t.name for t in p.session.scalars(select(Token))}
    followers = []
    for row in p.session.scalars(select(FollowerSeen).order_by(FollowerSeen.last_seen_at.desc()).limit(100)):
        try:
            behind = feed_schema.parse_protocol(row.protocol) < feed_schema.parse_protocol(emit)
        except ValueError:
            behind = True
        followers.append({"name": names.get(row.token_id, "(deleted token)"), "protocol": row.protocol,
                          "seen": row.last_seen_at, "behind": behind})
    return {"own": feed_schema.PROTOCOL, "min": s.min_follower_protocol, "emit": emit, "followers": followers,
            "choices": feed_schema.protocol_choices(s.min_follower_protocol, feed_schema.PROTOCOL),
            "has_password": p.me.password_hash != "!"}


STATUS_LABELS = {"paused": ("Paused", "warn"), "not_contacted": ("Not contacted yet", "warn"),
                 "needs_you": ("Needs you", "bad"), "unreachable": ("Unreachable", "bad"),
                 "empty": ("Empty feed", "warn"), "catching_up": ("Catching up", "warn"),
                 "caught_up_waiting": ("Caught up · some entries waiting", "warn"), "caught_up": ("Caught up", "ok")}
WAITING_SHOWN = 50


def _waiting(p: Ctx) -> dict:
    """C13 §7.5: entries waiting for a record, grouped by the record they wait for."""
    total = sync.open_waiting(p.session)
    groups = p.session.execute(
        select(SyncFailure.waiting_for, func.count(), func.min(SyncFailure.seq), func.min(SyncFailure.first_failed_at),
               func.min(SyncFailure.node_id))
        .where(SyncFailure.disposition == sync.WAIT, SyncFailure.resolved_at.is_(None))
        .group_by(SyncFailure.waiting_for).order_by(func.min(SyncFailure.seq)).limit(WAITING_SHOWN)).all()
    kinds: dict[str, set[str]] = {}
    for cid, kind in p.session.execute(select(SyncFailure.waiting_for, SyncFailure.kind).where(
            SyncFailure.disposition == sync.WAIT, SyncFailure.resolved_at.is_(None),
            SyncFailure.waiting_for.in_([g[0] for g in groups])).distinct()):
        kinds.setdefault(cid, set()).add(kind)
    return {**total, "groups": [{"record": cid, "entries": n, "first_seq": first, "since": since,
                                 "node_id": node, "kinds": sorted(kinds.get(cid, ()))}
                                for cid, n, first, since, node in groups]}


def _media_line(p: Ctx, peer: Peer) -> dict | None:
    """C13 §6.5: images of the rows this peer's feed delivered: how many are here, fetched from the peer or from
    their origin, and how many could not be fetched (the reasons are on each game's artwork page)."""
    from ..models import Artwork

    rows = p.session.execute(select(Artwork.blob_hash.is_not(None), Artwork.fetched_from, Artwork.fail_count)
                             .where(Artwork.origin_node == peer.node_id)).all()
    if not rows:
        return None
    label = f"peer:{peer.id}"
    return {"total": len(rows), "cached": sum(1 for c, _, _ in rows if c),
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


@router.post("/admin/peers/introduce")
def introduce_records(p: Annotated[Ctx, Depends(admin_form)], scope: Annotated[str, Form(max_length=8)] = "missing"):
    """"Publish them": schedule `atlas feed introduce` for the sync worker (C13 §4.2). Audited."""
    if scope not in ("missing", "all"):
        raise HTTPException(400, "Choose missing or all.")
    if p.st.ctx.settings.role != "authority" or feed.introduction_mode(p.st.ctx.settings) == feed.VIA_LEGACY:
        return _peers_page(p, error="Could not publish records: raise the emit protocol to 1.5 first (or enable the "
                                    "bridge, ATLAS_FEDERATION_BRIDGE_RECORD_OBS).", status=409)
    feed.schedule_introduce(p.session, scope)
    accounts.audit(p.session, p.me.id, "feed_introduce", "", {"scope": scope})
    return go("/admin/peers", "saved")


@router.get("/admin/peers", response_class=HTMLResponse)
def peers(p: Annotated[Ctx, Depends(admin_page)], probe: dict | None = None, error: str | None = None):
    return _peers_page(p, probe, error)


def _peers_page(p: Ctx, probe: dict | None = None, error: str | None = None, status: int = 200):
    rows = []
    cursors = {c.upstream: c for c in p.session.scalars(select(SyncCursor))}
    heads = sync.status(p.session, p.st.ctx)["upstreams"]
    for peer in p.session.scalars(select(Peer).order_by(Peer.created_at)):
        cur = cursors.get(peer.node_id)
        h = heads.get(peer.node_id)
        rows.append({"peer": peer, "seq": cur.last_seq if cur else 0, "fp": fingerprint(peer.node_id),
                     "heads": h, "status": sync.peer_status(peer, h),
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
                  waiting=_waiting(p), status_labels=STATUS_LABELS, records=_records(p),
                  next_retry=sync.next_retry_at, own=p.st.ctx.identity, fp=fingerprint(p.st.ctx.identity.node_id),
                  can_store=secrets_box.available(s), env_upstream=s.upstream_url, probe=probe, error=error, when=when,
                  inherit=s.inherit_schema, protocol=_protocol(p))


@router.post("/admin/peers/protocol", response_class=HTMLResponse)
def set_emit_protocol(p: Annotated[Ctx, Depends(admin_form)], emit_protocol: Annotated[str, Form(max_length=16)],
                      password: Annotated[str, Form(max_length=256)] = "",
                      followers_upgraded: Annotated[str, Form()] = ""):
    """Change the protocol this node's own feed may use. Raising it lets newer constructs reach followers,
    so it asks for the same confirmation as a key rotation (re-authentication + followers upgraded)."""
    throttle(p, f"emit-protocol:{p.me.id}", 5, 3600)
    s = p.st.ctx.settings
    current = feed_schema.emit_protocol(s)
    choices = feed_schema.protocol_choices(s.min_follower_protocol, feed_schema.PROTOCOL)
    if emit_protocol not in choices:
        return _peers_page(p, error=f"Choose one of {', '.join(choices)}.", status=400)
    if emit_protocol == current:
        return _peers_page(p, error=f"This node already emits protocol {current}.")
    raising = feed_schema.parse_protocol(emit_protocol) > feed_schema.parse_protocol(current)
    if raising and followers_upgraded != "on":
        return _peers_page(p, error="Could not raise the emit protocol: upgrade every follower first and confirm it.",
                           status=400)
    if not accounts.reauthenticated(p.me, password, p.login):
        return _peers_page(p, error="Could not change the emit protocol: confirm your current password or sign in "
                           "again with your linked provider.", status=400)
    row = p.session.get(Setting, "emit_protocol")
    if row is None:
        p.session.add(Setting(key="emit_protocol", value=emit_protocol, updated_by=p.me.id))
    else:
        row.value, row.updated_by = emit_protocol, p.me.id
    behind = [f["name"] for f in _protocol(p)["followers"]
              if feed_schema.parse_protocol(f["protocol"]) < feed_schema.parse_protocol(emit_protocol)]
    accounts.audit(p.session, p.me.id, "emit_protocol", "", {"from": current, "to": emit_protocol,
                                                              "followers_behind": behind[:20]})
    p.session.flush()
    p.st.ctx.settings = runtime.effective(p.st.base or s, runtime.load(p.session))
    record = feed_schema.parse_protocol(feed_schema.RECORD_PROTOCOL)
    if raising and feed_schema.parse_protocol(current) < record <= feed_schema.parse_protocol(emit_protocol) \
            and p.st.ctx.settings.role == "authority" and feed.unintroduced_count(p.session, "missing"):
        feed.schedule_introduce(p.session, "missing")  # C13 §4.2: the sync worker publishes them in batches
    return go("/admin/peers", "saved")


PROBE_DOMAIN = "atlas-peer-probe-v1\n"
FOLLOWS = (sync.FOLLOW_OWN, sync.FOLLOW_RELAYED)


def probe_token(p: Ctx, url: str, node_id: str, public_key: str, follow: str, via: str | None) -> str:
    """C13 §7.2: binds a choice on the choose-feed screen to what the probe saw, in this session (keyed by the
    session's form secret), so the add form cannot be edited into another feed or mode."""
    message = PROBE_DOMAIN + "\n".join((url, node_id, public_key, follow, via or ""))
    return hmac.new(p.csrf.encode(), message.encode(), hashlib.sha256).hexdigest()


def _kind(info: dict) -> str:
    k = str(info.get("kind", info.get("role", "")))
    return k if k in ("official", "community", "mirror", "local") else "community"


def _verified(node_id: object, key: object) -> bool:
    try:
        return isinstance(node_id, str) and isinstance(key, str) and key_id_for(base64.b64decode(key)) == node_id
    except ValueError:
        return False


def feed_options(p: Ctx, url: str, info: dict) -> list[dict]:
    """The feeds a probed node offers (C13 §7.2): its own, and each authority feed it relays, with how much of each
    it holds. A node that never signed anything (`own_head` 0) is not a useful source of its own feed."""
    peers_now = set(p.session.scalars(select(Peer.node_id)))
    own_head = info.get("own_head") if type(info.get("own_head")) is int else None
    options = [{"follow": sync.FOLLOW_OWN, "node_id": str(info["node_id"]), "public_key": str(info["public_key"]),
                "via": None, "head": own_head, "empty": own_head == 0, "role": info.get("role", "?"),
                "kind": _kind(info), "label": "This node's own feed"}]
    for f in info.get("feeds", []) or []:
        if not isinstance(f, dict) or not _verified(f.get("node_id"), f.get("public_key")):
            continue
        head = f.get("head") if type(f.get("head")) is int else None
        options.append({"follow": sync.FOLLOW_RELAYED, "node_id": f["node_id"], "public_key": f["public_key"],
                        "via": str(info["node_id"]), "head": head, "empty": bool(f.get("empty", head == 0)),
                        "role": "authority", "kind": "official", "label": "An authority feed it relays"})
    for o in options:
        o["fp"], o["already"] = fingerprint(o["node_id"]), o["node_id"] in peers_now
        o["probe_token"] = probe_token(p, url, o["node_id"], o["public_key"], o["follow"], o["via"])
    usable = [o for o in options if not o["already"]]
    # the live failure: a mirror's own feed is empty while the authority feed it relays holds everything
    best = next((o for o in usable if not o["empty"] and (o["follow"] == sync.FOLLOW_RELAYED or o["head"])), None)
    for o in options:
        o["recommended"] = o is (best or (usable[0] if usable else None))
    return options


@router.post("/admin/peers/probe", response_class=HTMLResponse)
def probe(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
          token: Annotated[str, Form(max_length=200)] = "", name: Annotated[str, Form(max_length=80)] = "",
          feed_node_id: Annotated[str, Form(max_length=64)] = ""):
    target = clean_url(url)
    headers = {"Authorization": f"Bearer {token.strip()}"} if token.strip() else {}
    try:
        resp = p.st.http.get(f"{target}/api/v2/sync/info", headers=headers)
        resp.raise_for_status()
        info = resp.json()
        if not _verified(info.get("node_id"), info.get("public_key")):
            raise ValueError("the node's id does not match its public key")
        options = feed_options(p, target, info)
        if feed_node_id:  # the previous release's relay form: one chosen authority feed
            options = [o for o in options if o["node_id"] == feed_node_id and o["follow"] == sync.FOLLOW_RELAYED]
            if not options:
                raise ValueError("That authority feed is not available from this relay.")
            options[0]["recommended"] = True
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return peers(p, error=f"Could not verify that node: {exc}")  # type: ignore[arg-type]
    if all(o["already"] for o in options):
        return peers(p, error="That node is already a peer.")  # type: ignore[arg-type]
    return peers(p, probe={"url": target, "token": token.strip(), "name": name.strip() or info.get("name", ""),
                           "role": info.get("role", "?"), "kind": _kind(info), "fp": fingerprint(str(info["node_id"])),
                           "node_id": str(info["node_id"]), "options": options})  # type: ignore[arg-type]


@router.post("/admin/peers")
def add_peer(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
             node_id: Annotated[str, Form(max_length=64)], public_key: Annotated[str, Form(max_length=100)],
             name: Annotated[str, Form(max_length=80)], token: Annotated[str, Form(max_length=200)] = "",
             follow: Annotated[str, Form(max_length=8)] = "", via_node_id: Annotated[str, Form(max_length=64)] = "",
             probe_token_: Annotated[str, Form(alias="probe_token", max_length=64)] = ""):
    """Pin a peer. From the choose-feed screen (C13 §7.2) the form carries the chosen `follow` mode and a
    `probe_token` that proves it is what the probe offered; a form without `follow` (an older client) adds the
    peer as before, its mode inferred on each pull until the operator confirms it."""
    target = clean_url(url)
    try:
        ok = key_id_for(base64.b64decode(public_key)) == node_id
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(400, "The node id does not match the public key.")
    via = via_node_id.strip() or None
    if follow:
        expected = probe_token(p, target, node_id, public_key, follow, via)
        if follow not in FOLLOWS or not hmac.compare_digest(expected, probe_token_):
            raise HTTPException(400, "That choice does not match what the node offered. Look the node up again.")
    s = p.st.ctx.settings
    enc = None
    if token.strip():
        try:
            enc = secrets_box.encrypt(s, token.strip())
        except secrets_box.SecretsError as exc:
            raise HTTPException(400, str(exc)) from exc
    if p.session.scalars(select(Peer.id).where(Peer.node_id == node_id)).first():
        raise HTTPException(409, "That node is already a peer.")
    row = Peer(name=name.strip() or target, url=target, node_id=node_id, public_key=public_key, token_enc=enc,
               follow=follow or None, via_node_id=via if follow == sync.FOLLOW_RELAYED else None)
    p.session.add(row)
    accounts.audit(p.session, p.me.id, "peer_add", node_id, {"url": target, "follow": row.follow,
                                                            "via": row.via_node_id})
    return go("/admin/peers", "saved")


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
    accounts.audit(p.session, p.me.id, "peer_delete", row.node_id, None)
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
        return peers(p, error=f"Sync failed: {row.last_error}")  # type: ignore[arg-type]


@router.post("/admin/peers/failures/retry", response_class=HTMLResponse)
def retry_failed_entries(p: Annotated[Ctx, Depends(admin_form)]):
    """Re-apply the mirrored entries that failed, whatever their automatic attempt count."""
    service.write_lock(p.session)
    result = sync.retry_failures(p.session, p.st.ctx, limit=100, due_only=False)
    accounts.audit(p.session, p.me.id, "sync_failures_retry", "", result)
    p.session.flush()
    if not result["retried"]:
        return peers(p, error="Nothing to retry.")  # type: ignore[arg-type]
    note = f"Retried {result['retried']} entr{'y' if result['retried'] == 1 else 'ies'}: {result['resolved']} applied"
    if result["still_failing"]:
        note += f", {result['still_failing']} still failing (see the reasons below)"
    return peers(p, error=note + ".")  # type: ignore[arg-type]


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
                  when=when, error=error)


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
