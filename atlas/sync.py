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


def _ensure_canonical(session: Session, cid: str) -> None:
    if session.get(Canonical, cid) is None:
        session.add(Canonical(id=cid, view={}, explain={}))
        session.flush()


def apply_entry(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> None:
    if kind == "obs":
        cid = body["canonical_id"]
        o = body["observation"]
        _ensure_canonical(session, cid)
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
              token: str | None) -> int:
    """Fetch and apply one page. Returns the number of entries applied."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    info = client.get(f"{upstream_url}/api/v2/sync/info", headers=headers, timeout=15)
    info.raise_for_status()
    meta = info.json()
    node_id = meta["node_id"]
    pub = ctx.settings.trusted_keys.get(node_id)
    if pub is None:
        raise SyncError(f"upstream key {node_id} is not in ATLAS_TRUSTED_KEYS")

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
             token: str | None, max_pages: int = 1000) -> int:
    total = 0
    for _ in range(max_pages):
        n = pull_once(maker, ctx, client, upstream_url, token)
        total += n
        if n < ctx.settings.sync_batch_size:
            break
    return total
