"""This node's append-only signed change feed.

CONTRACT-C13 §4 (A19), "no orphans by construction": every entry that names a record is signed only after this feed
has introduced that record (`ensure_introduced`), by an observation, a `record` entry (protocol 1.5), a bridge
observation of the source `atlas-record` (below 1.5, opt-in), or the detach that created it. `emit` makes the call
itself for every kind that names records, so no emitter can forget it, and refuses (fails closed) an entry naming a
record that is still not introduced. Below 1.5 without the bridge nothing can introduce a record: it is marked
`legacy_unintroduced` and the entry is signed as before (the behaviour old sources keep), and
`atlas feed introduce --missing` publishes it once 1.5 or the bridge is available."""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import feed_schema
from .models import Canonical, ChangeLog, Crosswalk, FeedIntroduced, NodeState, Observation
from .signing import NodeIdentity, body_hash, load_identity

log = logging.getLogger("atlas.feed")

#: The body fields of each kind that name a record (C13 §4.1), in the order a follower checks them. A detach names
#: its old record here; the record it creates is introduced by the detach itself, or by a `record` right after it.
NAMES: dict[str, tuple[str, ...]] = {"artwork": ("canonical_id",), "achievements": ("canonical_id",),
                                     "custom": ("canonical_id",), "lock": ("canonical_id",),
                                     "unlock": ("canonical_id",), "merge": ("src", "dst"), "relation": ("from", "to"),
                                     "detach": ("canonical_id",)}
BECAUSE = {"merge": "merge", "relation": "relation", "detach": "detach"}  # every other naming kind: `sidecar`
#: How a record was introduced (`feed_introduced.via`).
VIA_OBS, VIA_RECORD, VIA_BRIDGE, VIA_DETACH, VIA_LEGACY = "obs", "record", "bridge", "detach", "legacy"
RECORD_DOMAIN = b"atlas-record-obs\n"


class UnintroducedCanonical(feed_schema.EmitGateError):
    """An entry names a record this feed never introduced (C13 §4.1 fail-closed gate)."""

    def __init__(self, cid: str, current: str):
        self.feature, self.needed, self.current, self.canonical_id = "unintroduced canonical", "1.5", current, cid
        ValueError.__init__(self, f"unintroduced canonical {cid}: introduce it (raise the emit protocol to 1.5 or "
                                  "enable the bridge) before signing entries that name it")


def emit(session: Session, ctx: Any, kind: str, body: dict[str, Any]) -> int:
    """Append an entry under this node's configured emit protocol (C1.6). Raises
    `feed_schema.EmitGateError` for a feature its followers may not understand yet.

    Every record the entry names is introduced first (C13 §4.1), in the same transaction and therefore at a lower
    sequence. An observation introduces its own record."""
    protocol = feed_schema.emit_protocol(ctx.settings)
    names = named(kind, body)
    scope_gate(session, kind, body, names)
    for cid in names:
        ensure_introduced(session, ctx, cid, BECAUSE.get(kind, "sidecar"))
    for cid in names:
        row = session.get(FeedIntroduced, cid)
        record = session.get(Canonical, cid)
        mode = introduction_mode(ctx.settings, record.media_type if record is not None else None)
        if row is None or (row.via == VIA_LEGACY and mode != VIA_LEGACY):
            raise UnintroducedCanonical(cid, protocol)
    seq = append(session, ctx.identity, kind, body, protocol=protocol)
    if kind == "obs" and isinstance(body.get("canonical_id"), str):
        mark(session, body["canonical_id"], seq, VIA_OBS)
    return seq


def scope_gate(session: Session, kind: str, body: dict[str, Any], names: list[str]) -> None:
    """CONTRACT-C14 §5.4: an entry naming a record with a scope ceiling is never signed, in every introduction
    mode. Only ceiling'd records named: `ScopeCeiling(local_ok=True)`, so the caller may keep the edit on this node
    (`emit_or_local`). A ceiling'd and a public record (a `merge`, a `relation`): fails closed. An `obs` names its
    own record, and an observation received from the group is never signed at all."""
    from .scope import ScopeCeiling, ceiling_of

    cids = list(names)
    if kind == "obs":
        if isinstance(body.get("canonical_id"), str) and body["canonical_id"] not in cids:
            cids.append(body["canonical_id"])
        o = body.get("observation") or {}
        if isinstance(o, dict) and (o.get("source") == "atlas-group"
                                    or str(o.get("source_scope") or "").startswith("group:")):
            raise ScopeCeiling(str(body.get("canonical_id")), "group", local_ok=True)
    ceilings = {cid: ceiling_of(session, cid) for cid in cids}
    capped = [cid for cid, c in ceilings.items() if c]
    if capped:
        raise ScopeCeiling(capped[0], ceilings[capped[0]], local_ok=len(capped) == len(cids))


def emit_or_local(session: Session, ctx: Any, kind: str, body: dict[str, Any]) -> int | None:
    """`emit`, except that an edit of records whose ceiling keeps them off the feed is kept on this node without
    signing (§5.4: "Where this edit goes: This node only"). Returns None then."""
    from .scope import ScopeCeiling

    try:
        return emit(session, ctx, kind, body)
    except ScopeCeiling as exc:
        if not exc.local_ok:  # §5.4: a mixed entry fails closed with the "Publish it" refusal (409), not a 500
            from .service import AtlasError

            raise AtlasError(str(exc), "scope_ceiling", canonical_id=exc.canonical_id,
                             ceiling=exc.ceiling) from exc
        log.info("not signed: %s has the scope ceiling %s", exc.canonical_id, exc.ceiling)
        return None


def append(session: Session, identity: NodeIdentity, kind: str, body: dict[str, Any], *,
           protocol: str = feed_schema.PROTOCOL) -> int:
    """Append an entry. Holds a row lock so seq order equals commit order (gap-free).

    Fails closed: a kind missing from `atlas/feed_schema.py`, or a construct newer than `protocol`, is
    never signed. Version-1 bodies are signed exactly as before (no envelope keys are added)."""
    body = feed_schema.check_emit(kind, body, protocol)
    return append_unchecked(session, identity, kind, body)


def append_unchecked(session: Session, identity: NodeIdentity, kind: str, body: dict[str, Any]) -> int:
    """Sign and append without the emission gate. Only for tests and the federation lab, which use it
    to play a newer release that emits entries this release does not know."""
    head = session.execute(
        select(NodeState).where(NodeState.key == "feed_seq").with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if head is None:
        head = NodeState(key="feed_seq", value="0")
        session.add(head)
        session.flush()
    seq = int(head.value) + 1
    head.value = str(seq)
    # A different worker may have rotated the key. The database, under the feed lock,
    # decides which key signs this sequence; a long-lived Ctx must not keep using the old one.
    transaction = session.get_transaction()
    cached = session.info.get("atlas_feed_signer")
    if cached is None or cached[0] is not transaction:
        cached = (transaction, load_identity(session))
        session.info["atlas_feed_signer"] = cached
    signer = cached[1]
    if signer.node_id != identity.node_id:
        raise ValueError("cannot append a different node's identity to this feed")
    digest = body_hash(body)
    session.add(
        ChangeLog(
            seq=seq, kind=kind, body=body, body_hash=digest,
            signature=signer.sign(seq, kind, digest), key_id=signer.key_id,
        )
    )
    session.flush()
    return seq


def read(session: Session, since: int, limit: int) -> list[ChangeLog]:
    return list(
        session.scalars(
            select(ChangeLog).where(ChangeLog.seq > since).order_by(ChangeLog.seq).limit(limit)
        )
    )


def head_seq(session: Session) -> int:
    row = session.get(NodeState, "feed_seq")
    return int(row.value) if row else 0


# ---- introducing records (C13 §3.2-§4.2) ------------------------------------------------------------------

def named(kind: str, body: dict[str, Any]) -> list[str]:
    """The record ids an entry of `kind` names, without duplicates (a merge or relation names two)."""
    out: list[str] = []
    for field in NAMES.get(kind, ()):
        value = body.get(field)
        if isinstance(value, str) and value.startswith("atlas:") and value not in out:
            out.append(value)
    return out


def bridged_types(settings: Any) -> list[str]:
    """CONTRACT-C15 §4.1: the media types the bridge carries (`federation_bridge_media_types`). Unset or empty means
    the default, games only: every caller (`bridges`, `introduce`, Admin → Peers) reads it through here so they
    cannot disagree (Settings also refuses an empty list)."""
    allowed = [t for t in (getattr(settings, "federation_bridge_media_types", None) or []) if t]
    return allowed or ["game"]


def bridges(settings: Any, media_type: str | None) -> bool:
    """CONTRACT-C15 §4.1: whether the bridge may introduce a record of `media_type` (`federation_bridge_media_types`,
    games only by default: a 1.0 follower would file any bridged observation as a game). None = the node-wide
    question (is the bridge on at all), used where no record is in hand."""
    if media_type is None:
        return True
    return (media_type or "game") in bridged_types(settings)


def introduction_mode(settings: Any, media_type: str | None = None) -> str:
    """What `ensure_introduced` emits (C13 §4.1, first matching row wins): the bridge observation while it is on, some
    follower may be below 1.5 and the record's type is bridged (C15 §4.1); else a `record` at emit protocol 1.5 or
    above; else nothing (`legacy`). Without `media_type` it answers for the node as a whole."""
    low = feed_schema.parse_protocol(getattr(settings, "min_follower_protocol", None) or "1.0")
    record = feed_schema.parse_protocol(feed_schema.RECORD_PROTOCOL)
    if getattr(settings, "federation_bridge_record_obs", False) and low < record and bridges(settings, media_type):
        return VIA_BRIDGE
    if feed_schema.parse_protocol(feed_schema.emit_protocol(settings)) >= record:
        return VIA_RECORD
    return VIA_LEGACY


def record_obs_id(feed_node_id: str, cid: str) -> str:
    """The deterministic observation id of a record's identity observation (C13 §3.2), shared by the bridge and the
    follower's synthetic observation, so both converge on one row. Keyed on the feed signer (never a relay) under
    a prefix-free domain. 26 base32 characters, the shape every release accepts as an observation id."""
    digest = hashlib.sha256(RECORD_DOMAIN + feed_node_id.encode() + b"\n" + cid.encode()).digest()
    return base64.b32encode(digest).decode()[:26]


def strip_ids(provider_ids: Any, limit: int = 32) -> dict[str, str]:
    """`provider_ids` as a `record` may carry them: valid provider keys and ids only, never `atlas` or another
    reserved namespace (C13 §3.2, C11 §11.2), at most `limit` of them."""
    from .contract import SourceObservationV1

    out: dict[str, str] = {}
    if not isinstance(provider_ids, dict):
        return out
    for key, value in sorted(provider_ids.items()):
        if len(out) >= limit or not isinstance(key, str) or not isinstance(value, str):
            continue
        if key in feed_schema.RECORD_RESERVED_NAMESPACES:
            continue
        try:
            SourceObservationV1.model_validate({"external_id": "x", "provider_ids": {key: value}})
        except ValueError:
            continue
        out[key] = value
    return out


def _signed_key(session: Session, cid: str, namespace: str, ids: list[str]) -> str:
    """The one work key of `namespace` a record signs (`ids` in lexical order). When it holds several, the ids the
    provider itself currently answers for this record (a current observation from that source) come first, so a
    merged-away id kept as an alias (C15 §3.3, `importer._keep_alias`) is never signed in place of the live id."""
    if len(ids) == 1:
        return ids[0]
    answered = set(session.scalars(select(Observation.external_id).where(
        Observation.canonical_id == cid, Observation.source == namespace, Observation.is_current.is_(True),
        Observation.external_id.in_(ids))))
    return next((i for i in ids if i in answered), ids[0])


def record_body(session: Session, settings: Any, row: Canonical, because: str) -> dict[str, Any]:
    """The identity of a record as this node signs it (C13 §3.2): its id and type, and, as
    `federation_snapshot_fields` allows, its display title and store ids. No provider content."""
    from .models import ScopeProjection
    from .scope import ANON_OFF

    fields = set(getattr(settings, "federation_snapshot_fields", ["title", "provider_ids"]) or [])
    # CONTRACT-C14 §5.7 (per-field public payloads): the title and ids a follower sees come from the record without
    # host/plugin library data and without anything received from the group (the `anon_off` projection), never
    # from those values; a field only they hold is left out.
    proj = session.get(ScopeProjection, (row.id, ANON_OFF))
    view = (proj.view if proj is not None else row.view) or {}
    title = view.get("title") if "title" in fields else ""
    allowed_ids = set((view.get("provider_ids") or {}).items()) if proj is not None else None
    keys: dict[str, str] = {}
    if "provider_ids" in fields:
        held_ids: dict[str, list[str]] = {}
        for cw in session.scalars(select(Crosswalk).where(Crosswalk.canonical_id == row.id, Crosswalk.variant == "")
                                  .order_by(Crosswalk.namespace, Crosswalk.external_id)):
            if allowed_ids is not None and (cw.namespace, cw.external_id) not in allowed_ids:
                continue  # C14 §5.7: only ids the public projection holds
            held_ids.setdefault(cw.namespace, []).append(cw.external_id)
        for ns, ids in held_ids.items():
            keys[ns] = _signed_key(session, row.id, ns, ids)
        if "isbn" not in keys:
            # C15 §2.5: `provider_ids` holds one value per namespace, so a book's `record` carries at most one ISBN:
            # one this record itself holds as an `isbn` edition row, the first in `editions[]` order. An ISBN the
            # view shows but this node never registered (a user's report) or refused (held by another work, an
            # `isbn_conflict`) is never signed. Followers get the others from their own enrichment (H56).
            held = set(session.scalars(select(Crosswalk.external_id).where(
                Crosswalk.canonical_id == row.id, Crosswalk.namespace == "isbn",
                Crosswalk.variant.like("edition:%"))))
            if allowed_ids is not None:  # C14 §5.7: only an ISBN the public projection holds
                held = {i for i in held if ("isbn", i) in allowed_ids}
            if held:
                from .normalize import isbn13

                ordered = [isbn13(e.get("isbn_13")) for e in (row.view or {}).get("editions") or []
                           if isinstance(e, dict)]
                keys["isbn"] = next((i for i in ordered if i in held), min(held))
    return {"canonical_id": row.id, "media_type": row.media_type or "game",
            "title": title[:feed_schema.RECORD_TITLE_MAX] if isinstance(title, str) else "",
            "provider_ids": strip_ids(keys), "because": because}


def mark(session: Session, cid: str, seq: int | None, via: str, because: str | None = None) -> None:
    """Record that this feed introduced `cid` (a legacy mark is replaced by a real introduction, never the reverse)."""
    row = session.get(FeedIntroduced, cid)
    if row is None:
        session.add(FeedIntroduced(canonical_id=cid, seq=seq, via=via, because=because,
                                   introduced_at=datetime.now(UTC)))
    elif row.via == VIA_LEGACY and via != VIA_LEGACY:
        row.seq, row.via, row.because, row.introduced_at = seq, via, because, datetime.now(UTC)
    else:
        return
    if via == VIA_LEGACY:
        # rule 2 no longer holds for this feed from here on: followers must not call a missing record a bug
        session.execute(NodeState.__table__.delete().where(NodeState.key == INTRODUCED_THROUGH))
    session.flush()


def _identity_observation(session: Session, ctx: Any, body: dict[str, Any]) -> Observation:
    """The `atlas-record` observation this node keeps for an introduced record, the same row (id, payload) its
    followers store, so `/sync/digest` agrees on both sides. It ranks below every provider (`settings.trust`)."""
    oid = record_obs_id(ctx.identity.node_id, body["canonical_id"])
    payload: dict[str, Any] = {}
    if body.get("title"):
        payload["title"] = body["title"]
    if body.get("provider_ids"):
        payload["provider_ids"] = body["provider_ids"]
    obs = session.get(Observation, oid)
    if obs is None:
        obs = Observation(id=oid, canonical_id=body["canonical_id"], source_kind="metadata",
                          source=feed_schema.RECORD_SOURCE, source_scope=ctx.identity.node_id,
                          external_id=body["canonical_id"], payload=payload, redistributable=True, is_current=True)
        session.add(obs)
        session.flush()
    return obs


def project_now(session: Session, ctx: Any, record: Canonical) -> bool:
    """C14 §5.7 (T-S11) while the post-upgrade reprojection is still running: a record that needs a projection but
    has none yet gets it now, so `record_body` never signs a host/plugin or group value and `introduce` never
    publishes a record a visitor may not see. Returns whether the record exists for a visitor."""
    from . import scope
    from .models import ScopeProjection

    proj = session.get(ScopeProjection, (record.id, scope.ANON_OFF))
    if proj is None and scope.needs_projection(session, record):
        scope.reproject(session, ctx, record.id)
        proj = session.get(ScopeProjection, (record.id, scope.ANON_OFF))
    return proj is None or bool(proj.present)


def ensure_introduced(session: Session, ctx: Any, cid: str, because: str) -> int | None:
    """Introduce `cid` on this feed unless it already is (C13 §4.1). Returns the seq signed, or None.

    The bridge observation or `record` is signed in the caller's transaction, before the entry that needs it."""
    row = session.get(FeedIntroduced, cid)
    if row is not None and row.via != VIA_LEGACY:
        return None
    record = session.get(Canonical, cid)
    if record is None:
        return None  # nothing to introduce; the gate in `emit` refuses the entry
    if record.scope_ceiling:
        return None  # CONTRACT-C14 §5.4: never introduced, never marked, in any mode (Publish lifts it first)
    mode = introduction_mode(ctx.settings, record.media_type)  # C15 §4.1: the bridge carries only some types
    if mode == VIA_LEGACY:
        mark(session, cid, None, VIA_LEGACY, because)
        return None
    project_now(session, ctx, record)
    body = record_body(session, ctx.settings, record, because)
    obs = _identity_observation(session, ctx, body)
    if mode == VIA_BRIDGE:
        seq = append(session, ctx.identity, "obs", {
            "canonical_id": cid, "media_type": body["media_type"],
            "observation": {"id": obs.id, "source_kind": obs.source_kind, "source": obs.source,
                            "source_scope": obs.source_scope, "external_id": obs.external_id,
                            "payload": obs.payload, "source_updated_at": None,
                            "observed_at": obs.observed_at.isoformat()}},
            protocol=feed_schema.emit_protocol(ctx.settings))
    else:
        seq = append(session, ctx.identity, "record", body, protocol=feed_schema.emit_protocol(ctx.settings))
    mark(session, cid, seq, mode, because)
    log.info("introduced %s on the feed at seq %s (%s, because %s)", cid, seq, mode, because)
    return seq


def bridge_skipped(session: Session, settings: Any) -> int:
    """C15 §4.1 (Admin → Peers): while the bridge is on, the live records it does not carry because their media type
    is not in `federation_bridge_media_types` (they get a `record` instead when the emit protocol allows it). 0 when
    the bridge is not in use."""
    if introduction_mode(settings) != VIA_BRIDGE:
        return 0
    return session.scalar(select(func.count()).select_from(Canonical).where(
        Canonical.merged_into.is_(None), Canonical.media_type.not_in(bridged_types(settings)))) or 0


# ---- `atlas feed introduce` (C13 §4.2) ---------------------------------------------------------------------

INTRODUCED_THROUGH = "introduced_through"
INTRODUCE_CURSOR = "introduce_cursor"
INTRODUCE_PENDING = "introduce_pending"
INTRODUCE_BATCH = 500


class IntroduceRefused(ValueError):
    pass


def _unbridgeable(settings: Any) -> list[str] | None:
    """C15 §4.1: the bridged media types when records of other types cannot be introduced at all (the bridge is the
    node's mode and the emit protocol is below 1.5, so they stay `legacy`); None otherwise. `introduce` leaves those
    records out, or it would never finish."""
    if settings is None or introduction_mode(settings) != VIA_BRIDGE:
        return None
    record = feed_schema.parse_protocol(feed_schema.RECORD_PROTOCOL)
    if feed_schema.parse_protocol(feed_schema.emit_protocol(settings)) >= record:  # other types get a `record`
        return None
    return bridged_types(settings)


def candidates(session: Session, scope: str, after: str = "", limit: int | None = None,
               settings: Any = None) -> list[Canonical]:
    """Records `introduce` would publish, in id order after `after`. `missing`: named by a signed entry while
    nothing could introduce them (`legacy_unintroduced`), merged since or not: a follower's entries that name a
    merged-away record (its artwork, the merge itself) wait for it, and its `record` lets them apply in feed order
    before the merge moves them. `all`: every live record not introduced yet."""
    query = select(Canonical).where(Canonical.id > after)
    introduced = select(FeedIntroduced.canonical_id).where(FeedIntroduced.via != VIA_LEGACY)
    if scope == "missing":
        query = query.where(Canonical.id.in_(select(FeedIntroduced.canonical_id)
                                             .where(FeedIntroduced.via == VIA_LEGACY)))
    else:
        query = query.where(Canonical.merged_into.is_(None), Canonical.id.not_in(introduced),
                            Canonical.id.not_in(publicly_absent()))
    if (bridged := _unbridgeable(settings)) is not None:
        query = query.where(Canonical.media_type.in_(bridged))
    query = query.where(Canonical.scope_ceiling.is_(None)).order_by(Canonical.id)  # C14 §5.4: never introduced
    return list(session.scalars(query.limit(limit) if limit else query))


def unintroduced_count(session: Session, scope: str = "all", settings: Any = None) -> int:
    query = select(func.count()).select_from(Canonical).where(Canonical.scope_ceiling.is_(None))
    if (bridged := _unbridgeable(settings)) is not None:
        query = query.where(Canonical.media_type.in_(bridged))
    if scope == "missing":  # merged ones too (see `candidates`)
        query = query.where(Canonical.id.in_(select(FeedIntroduced.canonical_id)
                                             .where(FeedIntroduced.via == VIA_LEGACY)))
    else:
        query = query.where(Canonical.merged_into.is_(None), Canonical.id.not_in(
            select(FeedIntroduced.canonical_id).where(FeedIntroduced.via != VIA_LEGACY)),
            Canonical.id.not_in(publicly_absent()))
    return session.scalar(query) or 0


def publicly_absent() -> Any:
    """Records that do not exist for a visitor (C14 §5.7: only host/plugin library data or data received from the
    group, a `private` ceiling): `federation_introduce = all` never introduces them, or the feed would disclose a
    record a follower's token may not see. An entry that names one still introduces it (`emit`)."""
    from .models import ScopeProjection
    from .scope import ANON_OFF

    return select(ScopeProjection.canonical_id).where(ScopeProjection.tier == ANON_OFF,
                                                      ScopeProjection.present.is_(False))


def introduce_batch(session: Session, ctx: Any, scope: str, *, limit: int = INTRODUCE_BATCH) -> dict[str, Any]:
    """One resumable step of `atlas feed introduce` (C13 §4.2): at most `limit` records, in id order from the
    cursor, in the caller's transaction. When nothing is left, `introduced_through` is set to the feed head: from
    that seq on every record this feed names was introduced at or below it."""
    if scope not in ("missing", "all"):
        raise ValueError("scope is missing or all")
    if ctx.settings.role != "authority":
        raise IntroduceRefused("only an authority signs a feed; this node's role is " + ctx.settings.role)
    if introduction_mode(ctx.settings) == VIA_LEGACY:
        raise IntroduceRefused("nothing can introduce a record at emit protocol "
                               f"{feed_schema.emit_protocol(ctx.settings)}: raise the emit protocol to 1.5 or enable "
                               "the bridge (ATLAS_FEDERATION_BRIDGE_RECORD_OBS=true)")
    from .service import write_lock

    write_lock(session)
    cursor_key = f"{INTRODUCE_CURSOR}:{scope}"
    state = session.get(NodeState, cursor_key)
    after = state.value if state is not None else ""
    rows = candidates(session, scope, after, limit, ctx.settings)
    signed = 0
    for row in rows:
        if scope == "all" and not project_now(session, ctx, row):
            after = row.id  # not projected yet when the batch was chosen: a record a visitor may not see
            continue
        if ensure_introduced(session, ctx, row.id, "backfill") is not None:
            signed += 1
        after = row.id
    left = unintroduced_count(session, scope, ctx.settings)
    done = left == 0
    if done:
        session.execute(NodeState.__table__.delete().where(NodeState.key == cursor_key))
        _set(session, INTRODUCED_THROUGH, str(head_seq(session)))
        session.execute(NodeState.__table__.delete().where(NodeState.key == INTRODUCE_PENDING))
    elif rows:
        _set(session, cursor_key, after)
    else:  # the cursor passed the end while records were added before it: start over
        session.execute(NodeState.__table__.delete().where(NodeState.key == cursor_key))
    session.flush()
    return {"scope": scope, "introduced": signed, "examined": len(rows), "left": left, "finished": done,
            "introduced_through": introduced_through(session)}


def introduced_through(session: Session) -> int | None:
    row = session.get(NodeState, INTRODUCED_THROUGH)
    try:
        return int(row.value) if row is not None else None
    except ValueError:
        return None


def schedule_introduce(session: Session, scope: str = "missing") -> None:
    """Ask the background worker to run `introduce` (C13 §4.2: when the emit protocol is raised into 1.5)."""
    _set(session, INTRODUCE_PENDING, scope)


def run_pending(maker: Any, ctx: Any, *, max_batches: int = 20) -> dict[str, Any] | None:
    """The scheduled `introduce`, in batches of their own transactions; None when nothing is scheduled."""
    result = None
    for _ in range(max_batches):
        with maker() as session:
            pending = session.get(NodeState, INTRODUCE_PENDING)
            if pending is None:
                return result
            try:
                result = introduce_batch(session, ctx, pending.value if pending.value in ("missing", "all")
                                         else "missing")
            except IntroduceRefused as exc:
                log.warning("scheduled `atlas feed introduce` cannot run: %s", exc)
                return None
            session.commit()
            if result["finished"]:
                return result
    return result


def records_summary(session: Session) -> dict[str, int]:
    """`/sync/info.records` (C13 §3.4): live records, how many this feed introduced, and how many it did not."""
    live = session.scalar(select(func.count()).select_from(Canonical).where(Canonical.merged_into.is_(None))) or 0
    introduced = session.scalar(select(func.count()).select_from(Canonical).where(
        Canonical.merged_into.is_(None), Canonical.id.in_(select(FeedIntroduced.canonical_id)
                                                          .where(FeedIntroduced.via != VIA_LEGACY)))) or 0
    return {"live": live, "introduced": introduced, "not_federated": live - introduced}


def rebuild_introduced(session: Session) -> int:
    """Fill `feed_introduced` from this node's own `change_log` (as migration 0036 does): every `obs` and `record`
    introduces its record, a `detach` its new record, and a live record that an entry names while nothing
    introduced it is marked legacy. Used by promotion, which re-signs a retained feed under a new identity."""
    introduced: dict[str, tuple[int, str]] = {}
    seen: set[str] = set()
    for row in session.scalars(select(ChangeLog).order_by(ChangeLog.seq)).yield_per(500):
        body = row.body if isinstance(row.body, dict) else {}
        cid = body.get("canonical_id")
        if row.kind in ("obs", "record") and isinstance(cid, str):
            introduced.setdefault(cid, (row.seq, VIA_OBS if row.kind == "obs" else VIA_RECORD))
        elif row.kind == "detach" and isinstance(body.get("new_canonical_id"), str):
            introduced.setdefault(body["new_canonical_id"], (row.seq, VIA_DETACH))
        seen.update(named(row.kind, body))
    count = 0
    for cid, (seq, via) in introduced.items():
        if session.get(FeedIntroduced, cid) is None:
            session.add(FeedIntroduced(canonical_id=cid, seq=seq, via=via, introduced_at=datetime.now(UTC)))
            count += 1
    for cid in sorted(seen - set(introduced)):
        live = session.get(Canonical, cid)
        if live is not None and live.merged_into is None and session.get(FeedIntroduced, cid) is None:
            session.add(FeedIntroduced(canonical_id=cid, seq=None, via=VIA_LEGACY, introduced_at=datetime.now(UTC)))
            count += 1
    session.flush()
    return count


def _set(session: Session, key: str, value: str) -> None:
    row = session.get(NodeState, key)
    if row is None:
        session.add(NodeState(key=key, value=value))
    else:
        row.value = value
