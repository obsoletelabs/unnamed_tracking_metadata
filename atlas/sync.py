"""Pull replication from a trusted upstream's signed change feed."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from . import key_rotation, service
from .contract import SourceObservationV1
from .models import (
    Canonical,
    Crosswalk,
    Lock,
    NodeState,
    Observation,
    ReplicatedChange,
    ReplicatedFeed,
    SyncCursor,
    SyncFailure,
)
from .service import AtlasError, Ctx, keys_of
from .signing import key_id_for

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


_OBS_FIELDS = ("title", "platform", "platforms", "edition", "release_date", "provider_ids", "facts", "source_updated_at",
               "available")
_ID = re.compile(r"[0-9A-Za-z]{26}\Z")


def _valid_observation(o: Any) -> bool:
    """What an upstream may send: the same shape and limits as an observation submitted through the API."""
    try:
        if not isinstance(o, dict) or not _ID.match(str(o.get("id", ""))) or len(json.dumps(o.get("payload"))) > 100_000:
            return False
        for key, limit in (("source", 128), ("source_scope", 512), ("external_id", 256)):
            if not isinstance(o.get(key), str) or not 0 < len(o[key]) <= limit:
                return False
        if o.get("source_kind") not in ("host", "plugin", "manual", "metadata"):
            return False
        payload = o["payload"]
        if not isinstance(payload, dict) or set(payload) - set(_OBS_FIELDS):
            return False
        SourceObservationV1.model_validate({"external_id": o["external_id"], **payload})
        datetime.fromisoformat(o["observed_at"])
        return True
    except (ValidationError, ValueError, TypeError, KeyError):
        return False


# Automatic retries stop here so a permanently broken entry is not re-run on every pull; an
# administrator can still retry it by hand.
AUTO_RETRY_LIMIT = 20


def apply_entry(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> bool:
    """Apply one entry; False when it failed. A failure is recorded, not silently dropped, and the
    entry's partial changes are rolled back, so one bad entry neither stalls replication nor leaves
    half-applied state."""
    try:
        with session.begin_nested():
            _apply(session, ctx, node_id, seq, kind, body)
    except (AtlasError, KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("could not apply feed entry %s (%s) from %s: %s", seq, kind, node_id, exc)
        _record_failure(session, node_id, seq, kind, exc)
        return False
    failure = session.get(SyncFailure, (node_id, seq))
    if failure is not None and failure.resolved_at is None:
        failure.resolved_at = datetime.now(UTC)
        failure.last_attempt_at = failure.resolved_at
    return True


def _record_failure(session: Session, node_id: str, seq: int, kind: str, exc: Exception) -> None:
    message = f"{type(exc).__name__}: {exc}"[:2000]
    now = datetime.now(UTC)
    failure = session.get(SyncFailure, (node_id, seq))
    if failure is None:
        session.add(SyncFailure(node_id=node_id, seq=seq, kind=kind[:32], error=message, attempts=1,
                                first_failed_at=now, last_attempt_at=now))
    else:
        failure.error, failure.attempts, failure.last_attempt_at = message, failure.attempts + 1, now
        failure.resolved_at = None
    session.flush()


def retry_failures(session: Session, ctx: Ctx, node_id: str | None = None, *, limit: int = 100,
                   max_attempts: int | None = AUTO_RETRY_LIMIT) -> dict[str, int]:
    """Re-apply retained entries that failed, oldest first. Application is idempotent, so a retry
    that succeeds converges without duplicating anything and healthy entries are untouched."""
    query = select(SyncFailure).where(SyncFailure.resolved_at.is_(None))
    if node_id is not None:
        query = query.where(SyncFailure.node_id == node_id)
    if max_attempts is not None:
        query = query.where(SyncFailure.attempts < max_attempts)
    rows = session.scalars(query.order_by(SyncFailure.node_id, SyncFailure.seq).limit(limit)).all()
    resolved = 0
    for row in rows:
        entry = session.get(ReplicatedChange, (row.node_id, row.seq))
        if entry is None:
            _record_failure(session, row.node_id, row.seq, row.kind, KeyError("retained entry is missing"))
        elif apply_entry(session, ctx, row.node_id, row.seq, entry.kind, entry.body):
            resolved += 1
    return {"retried": len(rows), "resolved": resolved, "still_failing": len(rows) - resolved}


def open_failures(session: Session) -> int:
    from sqlalchemy import func

    return session.scalar(select(func.count()).select_from(SyncFailure).where(SyncFailure.resolved_at.is_(None))) or 0


def _apply(session: Session, ctx: Ctx, node_id: str, seq: int, kind: str, body: dict[str, Any]) -> None:
    if kind == "obs":
        cid = body["canonical_id"]
        o = body["observation"]
        if not isinstance(cid, str) or not cid.startswith("atlas:") or len(cid) > 40 or not _valid_observation(o):
            return
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
            custom.apply_snapshot(session, body, node_id=node_id)
    elif kind == "custom":
        from . import custom
        from .custom_compat import key_for

        game = session.get(Canonical, body["canonical_id"])
        if game is None:
            return
        values = custom.clean_values(session, game.media_type, body.get("values") or {}, editable_only=False)
        clear = tuple(key_for(session, game.media_type, str(k)[:40]) for k in (body.get("clear") or [])[:60])
        custom.set_values(session, ctx, body["canonical_id"], str(body["source"])[:128], values,
                          redistributable=True, log=False, clear=clear)
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
    elif kind == "key_rotation":
        pass  # Verified with the complete key history before any entry is applied.
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
    relay = False
    if node_id not in keys:
        available = {f.get("node_id") for f in meta.get("feeds", []) if isinstance(f, dict)}
        choices = available & keys.keys()
        if len(choices) == 1:
            node_id = choices.pop()
            meta = next(f for f in meta["feeds"] if f.get("node_id") == node_id)
            relay = True
        elif len(choices) > 1:
            raise SyncError("relay has several trusted feeds; configure a separate peer for each authority")
    pub = keys.get(node_id)
    if pub is None:
        raise SyncError(f"upstream key {node_id} is not trusted (pin it under Admin → Peers or in ATLAS_TRUSTED_KEYS)")

    import base64
    if key_id_for(base64.b64decode(pub)) != node_id:
        raise SyncError("configured trusted key does not match its key id")
    try:
        history, signing_keys = key_rotation.validate_history(node_id, pub, meta.get("rotations", []),
                                                              meta.get("signing_key_id", node_id))
    except key_rotation.RotationError as exc:
        raise SyncError(str(exc)) from exc

    with maker() as session:
        cursor = session.get(SyncCursor, node_id)
        last = cursor.last_seq if cursor else 0
        # Upgraded mirrors backfill original signatures without re-applying old state.
        from sqlalchemy import func

        retained = session.scalar(select(func.max(ReplicatedChange.seq)).where(ReplicatedChange.node_id == node_id)) or 0
        since = min(last, retained)

    params = {"since": since, "limit": ctx.settings.sync_batch_size}
    if relay:
        params["node_id"] = node_id
    resp = client.get(
        f"{upstream_url}/api/v2/sync/changes",
        params=params, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    page = resp.json()
    entries = page["entries"]
    advertised_head = page.get("head")
    applied = 0
    with maker() as session:
        try:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            service.write_lock(session)
            # A second worker may have advanced while this request was in flight.
            cursor = session.get(SyncCursor, node_id, populate_existing=True)
            current = cursor.last_seq if cursor else 0
            expected = since
            try:
                key_rotation.remember(session, node_id, history)
            except key_rotation.RotationError as exc:
                raise SyncError(str(exc)) from exc
            retained_feed = session.get(ReplicatedFeed, node_id)
            if retained_feed is None:
                session.add(ReplicatedFeed(node_id=node_id, public_key=pub))
                session.flush()
            elif retained_feed.public_key != pub:
                raise SyncError("retained authority key differs from the pinned key")
            for e in entries:
                if e["seq"] != expected + 1:
                    raise SyncError(f"feed gap or rewind: expected {expected + 1}, got {e['seq']}")
                if not key_rotation.check_entry(node_id, history, signing_keys, e):
                    raise SyncError(f"bad signature on entry {e['seq']}")
                retained_entry = session.get(ReplicatedChange, (node_id, e["seq"]))
                if retained_entry is None:
                    session.add(ReplicatedChange(node_id=node_id, seq=e["seq"], kind=e["kind"], body=e["body"],
                                                body_hash=e["body_hash"], signature=e["signature"], key_id=e["key_id"]))
                elif (retained_entry.body_hash, retained_entry.signature) != (e["body_hash"], e["signature"]):
                    raise SyncError("authority feed fork: an already retained entry changed")
                if e["seq"] > current:
                    apply_entry(session, ctx, node_id, e["seq"], e["kind"], e["body"])
                expected = e["seq"]
                applied += 1
            retry_failures(session, ctx, node_id)
            if applied:
                cur = session.get(SyncCursor, node_id)
                if cur is None:
                    session.add(SyncCursor(upstream=node_id, last_seq=expected))
                else:
                    cur.last_seq = max(current, expected)
            if type(advertised_head) is int and expected <= advertised_head < 2**63:
                # Operational watermark only: this transport-reported head is not signed
                # and never authorizes an entry or advances the applied cursor.
                watermark = session.get(NodeState, "sync_head:" + node_id)
                if watermark is None:
                    session.add(NodeState(key="sync_head:" + node_id, value=str(advertised_head)))
                else:
                    watermark.value = str(max(int(watermark.value), advertised_head))
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
