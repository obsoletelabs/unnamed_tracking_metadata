"""Pull replication from a trusted upstream's signed change feed."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import service
from .models import Canonical, Crosswalk, Lock, Observation, SyncCursor
from .service import AtlasError, Ctx, keys_of
from .signing import key_id_for, verify

log = logging.getLogger("atlas.sync")


class SyncError(Exception):
    pass


def _is_upstream_origin(session: Session, cid: str) -> bool:
    return session.scalars(
        select(Observation.id).where(Observation.canonical_id == cid, Observation.origin_node.is_not(None)).limit(1)
    ).first() is not None


def _ensure_canonical(session: Session, cid: str, media_type: str | None = None) -> None:
    from .models import MediaType

    if session.get(Canonical, cid) is None:
        known = media_type and session.get(MediaType, media_type) is not None
        session.add(Canonical(id=cid, view={}, explain={}, media_type=media_type if known else "game"))
        session.flush()


def apply_entry(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> None:
    if kind == "obs":
        cid = body["canonical_id"]
        o = body["observation"]
        _ensure_canonical(session, cid, body.get("media_type"))
        if session.get(Observation, o["id"]) is not None:
            return  # idempotent replay
        prior = session.scalars(
            select(Observation).where(
                Observation.source == o["source"], Observation.source_scope == o["source_scope"],
                Observation.external_id == o["external_id"], Observation.is_current.is_(True),
            )
        ).first()
        if prior:
            prior.is_current = False
        obs = Observation(
            id=o["id"], canonical_id=cid, source_kind=o["source_kind"], source=o["source"],
            source_scope=o["source_scope"], external_id=o["external_id"], payload=o["payload"],
            source_updated_at=o.get("source_updated_at"),
            observed_at=datetime.fromisoformat(o["observed_at"]), redistributable=True,
            origin_node=node_id, origin_seq=seq,
        )
        session.add(obs)
        session.flush()
        touched = {cid}
        for key in keys_of(obs):
            row = session.get(Crosswalk, key)
            if row is None:
                session.add(Crosswalk(namespace=key[0], external_id=key[1], canonical_id=cid))
            elif row.canonical_id != cid:
                other = service.follow(session, row.canonical_id)
                if other != cid and not _is_upstream_origin(session, other):
                    # a purely local record now has an authoritative twin: fold it in
                    service.merge_canonicals(session, ctx, other, cid, actor="sync", log=False)
                # else: both are authoritative; the authority left the key where it was (and opened a
                # review), so the mirror does too.
        session.flush()
        for t in touched:
            if session.get(Canonical, t) and session.get(Canonical, t).merged_into is None:  # type: ignore[union-attr]
                service.rebuild(session, ctx, t)
    elif kind in ("artwork", "achievements"):
        from . import facets

        try:
            fn = facets.set_artwork if kind == "artwork" else facets.set_achievements
            fn(session, ctx, body["canonical_id"], body["source"], body["items"], redistributable=True, log=False)
        except AtlasError:
            return
    elif kind == "schema":
        from . import custom

        if ctx.settings.inherit_schema:
            custom.apply_snapshot(session, body)
    elif kind == "custom":
        from . import custom

        try:
            custom.set_values(session, ctx, body["canonical_id"], body["source"], body.get("values") or {},
                              redistributable=True, log=False, clear=tuple(body.get("clear") or ()))
        except AtlasError:
            return
    elif kind == "merge":
        src, dst = body["src"], body["dst"]
        s, d = session.get(Canonical, src), session.get(Canonical, dst)
        if s is None or d is None or s.merged_into or d.merged_into:
            return
        service.merge_canonicals(session, ctx, src, dst, actor="sync", log=False)
    elif kind == "detach":
        try:
            service.detach_observation(
                session, ctx, body["observation_id"], new_id=body["new_canonical_id"], log=False
            )
        except AtlasError:
            return
    elif kind == "lock":
        existing = session.get(Lock, (body["canonical_id"], body["field"]))
        if existing is not None and existing.local_only:
            return
        try:
            service.set_lock(session, ctx, body["canonical_id"], body["field"], body["value"],
                             actor=node_id, log=False)
        except AtlasError:
            return
    elif kind == "unlock":
        try:
            service.clear_lock(session, ctx, body["canonical_id"], body["field"], log=False)
        except AtlasError:
            return
    else:
        log.warning("ignoring unknown feed entry kind %r", kind)


def pull_once(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client, upstream_url: str,
              token: str | None, trusted: dict[str, str] | None = None) -> int:
    """Fetch and apply one page. Returns the number of entries applied. `trusted` maps node id -> public key."""
    keys = ctx.settings.trusted_keys if trusted is None else trusted
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    info = client.get(f"{upstream_url}/api/v2/sync/info", headers=headers, timeout=15)
    info.raise_for_status()
    meta = info.json()
    node_id = meta["node_id"]
    pub = keys.get(node_id)
    if pub is None:
        raise SyncError(f"upstream key {node_id} is not trusted (pin it under Admin → Peers or in ATLAS_TRUSTED_KEYS)")

    import base64
    if key_id_for(base64.b64decode(pub)) != node_id:
        raise SyncError("configured trusted key does not match its key id")

    with maker() as session:
        cursor = session.get(SyncCursor, node_id)
        last = cursor.last_seq if cursor else 0

    resp = client.get(
        f"{upstream_url}/api/v2/sync/changes",
        params={"since": last, "limit": ctx.settings.sync_batch_size}, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    entries = resp.json()["entries"]
    applied = 0
    with maker() as session:
        try:
            service.write_lock(session)
            expected = last
            for e in entries:
                if e["key_id"] != node_id or e["node_id"] != node_id:
                    raise SyncError("entry signed by a different key than the one trusted")
                if e["seq"] != expected + 1:
                    raise SyncError(f"feed gap or rewind: expected {expected + 1}, got {e['seq']}")
                if not verify(pub, node_id, e["seq"], e["kind"], e["body"], e["body_hash"], e["signature"]):
                    raise SyncError(f"bad signature on entry {e['seq']}")
                apply_entry(session, ctx, node_id, e["seq"], e["kind"], e["body"])
                expected = e["seq"]
                applied += 1
            if applied:
                cur = session.get(SyncCursor, node_id)
                if cur is None:
                    session.add(SyncCursor(upstream=node_id, last_seq=expected))
                else:
                    cur.last_seq = expected
            session.commit()
        except Exception:
            session.rollback()
            raise
    return applied


def pull_all(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client, upstream_url: str,
             token: str | None, max_pages: int = 1000, trusted: dict[str, str] | None = None) -> int:
    total = 0
    for _ in range(max_pages):
        n = pull_once(maker, ctx, client, upstream_url, token, trusted)
        total += n
        if n < ctx.settings.sync_batch_size:
            break
    return total


def sync_peers(maker: sessionmaker[Session], ctx: Ctx, client: httpx.Client) -> dict[str, str]:
    """Replicate from every enabled peer (plus the environment-configured upstream). Failures are per peer."""
    from . import secrets_box
    from .models import Peer, now

    results: dict[str, str] = {}
    s = ctx.settings
    jobs: list[tuple[str, str, str | None, dict[str, str], str | None]] = []
    if s.upstream_url:
        jobs.append(("env", s.upstream_url, s.upstream_token, s.trusted_keys, None))
    with maker() as session:
        for peer in session.scalars(select(Peer).where(Peer.enabled.is_(True))):
            try:
                token = secrets_box.decrypt(s, peer.token_enc) if peer.token_enc else None
            except secrets_box.SecretsError as exc:
                peer.last_error = str(exc)[:300]
                results[peer.name] = peer.last_error
                continue
            jobs.append((peer.name, peer.url, token, {peer.node_id: peer.public_key}, peer.id))
        session.commit()
    for name, url, token, trusted, peer_id in jobs:
        try:
            n = pull_all(maker, ctx, client, url, token, trusted=trusted)
            results[name] = f"ok, {n} entries"
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            results[name] = error
            log.warning("sync with %s failed: %s", name, error)
        if peer_id:
            with maker() as session:
                row = session.get(Peer, peer_id)
                if row is not None:
                    row.last_error = error
                    if error is None:
                        row.last_ok_at = now()
                session.commit()
    return results
