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

from .. import accounts, auth, secrets_box, sync
from ..models import Peer, SyncCursor, Token, User
from ..signing import key_id_for
from .deps import Page as Ctx
from .deps import admin_form, admin_page, go
from .routes import render, when

router = APIRouter()


def clean_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or len(url) > 300 or any(c.isspace() for c in url):
        raise HTTPException(400, "Use a plain http(s) address such as https://atlas.example.org")
    return url


def fingerprint(node_id: str) -> str:
    return " ".join(node_id[i:i + 4] for i in range(0, len(node_id), 4))


@router.get("/admin/peers", response_class=HTMLResponse)
def peers(p: Annotated[Ctx, Depends(admin_page)], probe: dict | None = None, error: str | None = None):
    rows = []
    cursors = {c.upstream: c for c in p.session.scalars(select(SyncCursor))}
    for peer in p.session.scalars(select(Peer).order_by(Peer.created_at)):
        cur = cursors.get(peer.node_id)
        rows.append({"peer": peer, "seq": cur.last_seq if cur else 0, "fp": fingerprint(peer.node_id)})
    s = p.st.ctx.settings
    return render(p, "admin_peers.html", rows=rows, own=p.st.ctx.identity, fp=fingerprint(p.st.ctx.identity.node_id),
                  can_store=secrets_box.available(s), env_upstream=s.upstream_url, probe=probe, error=error, when=when,
                  inherit=s.inherit_schema)


@router.post("/admin/peers/probe", response_class=HTMLResponse)
def probe(p: Annotated[Ctx, Depends(admin_form)], url: Annotated[str, Form(max_length=300)],
          token: Annotated[str, Form(max_length=200)] = "", name: Annotated[str, Form(max_length=80)] = ""):
    target = clean_url(url)
    headers = {"Authorization": f"Bearer {token.strip()}"} if token.strip() else {}
    try:
        resp = p.st.http.get(f"{target}/api/v2/sync/info", headers=headers)
        resp.raise_for_status()
        info = resp.json()
        node_id, key = str(info["node_id"]), str(info["public_key"])
        if key_id_for(base64.b64decode(key)) != node_id:
            raise ValueError("the node's id does not match its public key")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return peers(p, error=f"Could not verify that node: {exc}")  # type: ignore[arg-type]
    if p.session.scalars(select(Peer.id).where(Peer.node_id == node_id)).first():
        return peers(p, error="That node is already a peer.")  # type: ignore[arg-type]
    return peers(p, probe={"url": target, "token": token.strip(), "node_id": node_id, "fp": fingerprint(node_id),
                           "public_key": key, "role": info.get("role", "?"), "kind": (k if (k := str(info.get("kind", info.get("role", "")))) in ("official", "community", "mirror", "local") else "community"), "name": name.strip() or info.get("name", "")})  # type: ignore[arg-type]  # noqa: E501


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
