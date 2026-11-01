"""Admin: peers (replication) and API tokens."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .. import accounts, auth, feed_schema, key_rotation, runtime, secrets_box, service, sync
from ..models import FollowerSeen, Peer, Setting, SyncCursor, SyncFailure, Token, User
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


@router.get("/admin/peers", response_class=HTMLResponse)
def peers(p: Annotated[Ctx, Depends(admin_page)], probe: dict | None = None, error: str | None = None):
    return _peers_page(p, probe, error)


def _peers_page(p: Ctx, probe: dict | None = None, error: str | None = None, status: int = 200):
    rows = []
    cursors = {c.upstream: c for c in p.session.scalars(select(SyncCursor))}
    heads = sync.status(p.session, p.st.ctx)["upstreams"]
    for peer in p.session.scalars(select(Peer).order_by(Peer.created_at)):
        cur = cursors.get(peer.node_id)
        rows.append({"peer": peer, "seq": cur.last_seq if cur else 0, "fp": fingerprint(peer.node_id),
                     "heads": heads.get(peer.node_id)})
    s = p.st.ctx.settings
    failures = list(p.session.scalars(select(SyncFailure).where(SyncFailure.resolved_at.is_(None))
                                      .order_by(SyncFailure.node_id, SyncFailure.seq).limit(FAILURES_SHOWN)))
    return render(p, "admin_peers.html", status=status, rows=rows, failures=failures,
                  failures_open=sync.open_failures(p.session),
                  parked_open=sync.open_failures(p.session, feed_schema.PARK),
                  auto_limit=sync.AUTO_RETRY_LIMIT, own=p.st.ctx.identity, fp=fingerprint(p.st.ctx.identity.node_id),
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
    return go("/admin/peers", "saved")


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
        available = [f for f in info.get("feeds", []) if isinstance(f, dict)
                     and isinstance(f.get("node_id"), str) and isinstance(f.get("public_key"), str)]
        if feed_node_id:
            selected = next((f for f in available if f["node_id"] == feed_node_id), None)
            if selected is None:
                raise ValueError("That authority feed is not available from this relay.")
            info = {**info, "node_id": selected["node_id"], "public_key": selected["public_key"]}
        node_id, key = str(info["node_id"]), str(info["public_key"])
        if key_id_for(base64.b64decode(key)) != node_id:
            raise ValueError("the node's id does not match its public key")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return peers(p, error=f"Could not verify that node: {exc}")  # type: ignore[arg-type]
    if p.session.scalars(select(Peer.id).where(Peer.node_id == node_id)).first() and (feed_node_id or not available):
        return peers(p, error="That node is already a peer.")  # type: ignore[arg-type]
    return peers(p, probe={"url": target, "token": token.strip(), "node_id": node_id, "fp": fingerprint(node_id),
                           "public_key": key, "role": info.get("role", "?"), "kind": (k if (k := str(info.get("kind", info.get("role", "")))) in ("official", "community", "mirror", "local") else "community"), "name": name.strip() or info.get("name", ""), "feeds": available})  # type: ignore[arg-type]  # noqa: E501


@router.post("/admin/peers")
def add_peer(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
             node_id: Annotated[str, Form(max_length=64)], public_key: Annotated[str, Form(max_length=100)],
             name: Annotated[str, Form(max_length=80)], token: Annotated[str, Form(max_length=200)] = ""):
    target = clean_url(url)
    try:
        ok = key_id_for(base64.b64decode(public_key)) == node_id
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(400, "The node id does not match the public key.")
    s = p.st.ctx.settings
    enc = None
    if token.strip():
        try:
            enc = secrets_box.encrypt(s, token.strip())
        except secrets_box.SecretsError as exc:
            raise HTTPException(400, str(exc)) from exc
    if p.session.scalars(select(Peer.id).where(Peer.node_id == node_id)).first():
        raise HTTPException(409, "That node is already a peer.")
    row = Peer(name=name.strip() or target, url=target, node_id=node_id, public_key=public_key, token_enc=enc)
    p.session.add(row)
    accounts.audit(p.session, p.me.id, "peer_add", node_id, {"url": target})
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


@router.post("/admin/peers/{pid}/sync", response_class=HTMLResponse)
def sync_now(pid: str, p: Annotated[Ctx, Depends(admin_form)]):
    row = p.session.get(Peer, pid)
    if row is None:
        raise HTTPException(404, "No such peer.")
    s = p.st.ctx.settings
    try:
        token = secrets_box.decrypt(s, row.token_enc) if row.token_enc else None
        n = sync.pull_all(p.st.maker, p.st.ctx, p.st.http, row.url, token, max_pages=20,
                          trusted={row.node_id: row.public_key})
        row.last_error, row.last_ok_at = None, datetime.now(UTC)
        p.session.commit()
        return peers(p, error=None) if n == 0 else peers(p, error=f"Synced {n} entries.")  # type: ignore[arg-type]
    except Exception as exc:
        row.last_error = f"{type(exc).__name__}: {exc}"[:300]
        p.session.commit()
        return peers(p, error=f"Sync failed: {row.last_error}")  # type: ignore[arg-type]


@router.post("/admin/peers/failures/retry", response_class=HTMLResponse)
def retry_failed_entries(p: Annotated[Ctx, Depends(admin_form)]):
    """Re-apply the mirrored entries that failed, whatever their automatic attempt count."""
    service.write_lock(p.session)
    result = sync.retry_failures(p.session, p.st.ctx, limit=100, max_attempts=None)
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
    if "admin" in scopes and not p.is_local_admin:  # C10 §6.3: never through a group grant
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
