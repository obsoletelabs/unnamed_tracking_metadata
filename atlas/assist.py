"""Asking the group: records and images one member lacks, answered by another (CONTRACT-C14 Part A, C14-2).

Everything travels inside the C10 sealed member call (`api.group_routes`), as the ops `assist.find`, `assist.fetch`
and `assist.blob` (provider lookups and shared quota, `assist.lookup` / `assist.status`, are in `assist_lookup`).

Helper side (`serve_*`): answers only from what it holds, under this node's local policy (§3.8). It never writes
its catalogue for an asker, never forwards a question (no transitive assist: data it got from the group is never
served again), and never returns private or local data: such a record answers exactly like an unknown id (§3.7).
The only writes an answer makes on a helper are rate counters.

Asker side: a record this node waits for (an A19 `wait` row, §3.1) becomes a job in `node_state`
(`assist_job:<hash>`). The runner (`run`, end of every group round) asks the members that advertise the ops, takes
the first helper-signed bundle, verifies it (signature under the helper's current key, bound to this node and this
request's nonce, fresh) and applies it (§3.4) as observations of `atlas-group` (trust 5) and of `<provider>`
scoped `group:<helper>`, artwork rows and crosswalk keys that were free, on a record created with the `group`
ceiling, so nothing of it is ever signed into this node's public feed until an operator publishes it (§5.4).

What a helper says is an observation, not proof (Review #1, C14-2). Waiting entries of feed F wake only on:
* class 1(a): an entry signed by F that verifies against this node's own pin and rotation history for F;
* class 1(b): a bundle carrying an explicit vouch for F, signed by the key this node pins for F's feed;
* class 2: the helper reports it applied F's entries to this record, only for a record created here from a bundle,
  automatically with `group_assist_auto_adopt` (the full-trust preset, H61) or by one click; recorded as `wake`
  evidence, reversible with `forget`, paused while the helper is suspect, forgotten when the helper is removed.
A helper's `merged` answer never creates a redirect here: it is kept as a hint and the survivor is asked for as its
own record (§3.4 step 3). Only this node's own pull of F's feed supersedes the group's identity observation and
lifts the ceiling the assist set (§3.4 step 9, `on_feed_introduced`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from . import group_crypto as gc
from .ids import ulid
from .merge import source_class
from .models import (
    Achievement,
    Artwork,
    Canonical,
    Crosswalk,
    CustomValue,
    FeedIntroduced,
    GroupAssistItem,
    ListItem,
    Lock,
    MediaType,
    NodeState,
    Observation,
    Relation,
    ReplicatedChange,
    ReplicatedFeed,
    ScopeProjection,
    SyncFailure,
    now,
)
from .settings import GROUP_SOURCE, PROVIDER_SOURCES

log = logging.getLogger("atlas.assist")

#: The record and media ops (§3.3, §3.6). `assist.lookup` and `assist.status` are `assist_lookup.OPS`.
OPS = ("assist.find", "assist.fetch", "assist.blob")
REASONS = ("waiting_for_record", "media_missing", "user", "merge_hint")
WANTS = ("record", "artwork", "achievements")
ID_RE = re.compile(r"^atlas:[0-9a-z]{26}\Z")
SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")
NODE_RE = re.compile(r"^[0-9a-f]{16}\Z")
GROUP_PREFIX = "group:"
FIND_MAX = 100
BLOB_CHUNK = 192 * 1024
MAX_OBSERVATIONS = 20
MAX_ARTWORK = 64
TRUNCATED_ARTWORK = 24
MAX_ENTRIES = 10
INTRODUCING = ("obs", "record", "detach")
SUSPECT_FOR = timedelta(hours=1)
#: §3.2: back off 1 h, 6 h, 24 h, then daily.
BACKOFF = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
EVIDENCE_KINDS = ("entry", "vouch", "wake")
ITEM_KINDS = ("bundle", "entry", "vouch", "wake", "offer", "merge_hint", "lookup")
BACKFILL_SCAN = 500

JOB = "assist_job:"
DONE = "assist_done:"
BACKFILL = "assist_backfill:"
SUSPECT = "assist_suspect:"
HELPER_BACKOFF = "assist_backoff:"
LAST_OK = "assist_ok:"
NO_OPS = "assist_noops:"
DROPPED = "assist_jobs_dropped"

#: §3.10 helper-side limits (database-counted, safe across workers)
ANSWER_PER_MIN = 120
FETCH_PER_HOUR = 600
BYTES_PER_HOUR = 512 * 1024 * 1024


class AssistError(Exception):
    """A refused or malformed assist answer (the code is a closed vocabulary, counted, never the content)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ---- small helpers ----------------------------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _hash(cid: str) -> str:
    return hashlib.sha256(cid.encode()).hexdigest()[:16]


def _state(session: Session, key: str) -> NodeState | None:
    return session.get(NodeState, key)


def _json(session: Session, key: str) -> dict | None:
    row = session.get(NodeState, key)
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _put(session: Session, key: str, value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    row = session.get(NodeState, key)
    if row is None:
        session.add(NodeState(key=key, value=text))
    else:
        row.value = text
    session.flush()


def _drop(session: Session, key: str) -> None:
    session.execute(delete(NodeState).where(NodeState.key == key))


def _iso(dt: datetime) -> str:
    return _aware(dt).astimezone(UTC).isoformat()


def _parse(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def shares(settings: Any) -> set[str]:
    """What this node shares of its provider data (`group_assist_share_provider_data`, H59)."""
    raw = str(getattr(settings, "group_assist_share_provider_data", "none") or "none")
    return set() if raw == "none" else set(raw.split("+"))


def answer_mode(settings: Any) -> str:
    return str(getattr(settings, "group_assist_answer", "public") or "off")


def canon(value: Any) -> Any:
    """A value made safe for a signed group body (`gc.canonical_ok`): floats are dropped (a signed group body holds
    none, R1-10) and strings are NFC. Used on everything a bundle carries."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if -gc.MAX_SAFE_INT <= value <= gc.MAX_SAFE_INT else None
    if isinstance(value, float):
        return None
    if isinstance(value, str):
        return gc.nfc(value)
    if isinstance(value, list):
        return [v for v in (canon(x) for x in value) if v is not None]
    if isinstance(value, dict):
        return {gc.nfc(str(k)): v for k, v in ((k, canon(x)) for k, x in value.items()) if v is not None}
    return None


def member_name(session: Session, group_id: str, node: str) -> str:
    from . import groups

    row = session.get(groups.GroupMember, (group_id, node))
    return (row.name if row is not None and row.name else node)[:80]


def _group_and_trust(session: Session, identity: Any):
    from . import groups

    group = groups.active(session)
    if group is None:
        return None, None
    return group, groups.trust(session, group, own_node=identity.node_id)


# ---- what a helper holds and may share (§3.3, §3.7, §3.8) ------------------------------------------------------

def _public_obs(settings: Any, o: Observation) -> bool:
    return bool(o.origin_node) or (bool(o.redistributable) and getattr(settings, "role", "") == "authority")


def _shareable(session: Session, settings: Any, row: Canonical) -> tuple[list[Observation], list[Observation]]:
    """(public observations, provider observations this node shares in the group) of a record it may answer for.
    Never host/plugin data, never anything received from the group (hops = 0), never a local or private record."""
    mode = answer_mode(settings)
    if mode == "off" or row.merged_into is not None or row.scope_ceiling is not None:
        return [], []
    public: list[Observation] = []
    provider: list[Observation] = []
    share_fields = mode == "group" and "fields" in shares(settings)
    for o in session.scalars(select(Observation).where(Observation.canonical_id == row.id,
                                                       Observation.is_current.is_(True))
                             .order_by(Observation.observed_at, Observation.id)):
        if o.source_kind in ("host", "plugin") or o.source == GROUP_SOURCE \
                or (o.source_scope or "").startswith(GROUP_PREFIX) or o.assist_item_id is not None:
            continue
        if _public_obs(settings, o):
            public.append(o)
        elif share_fields and o.source_kind == "metadata" and o.source in PROVIDER_SOURCES \
                and source_class(o.source) == "provider":
            provider.append(o)
    return public, provider


def _art_rows(session: Session, settings: Any, cid: str, *, bytes_only: bool = False) -> list[tuple[Artwork, str]]:
    """Artwork rows of `cid` this node may tell a member about, with their scope (`public` or `group`)."""
    from . import media

    mode = answer_mode(settings)
    share_media = mode == "group" and "media" in shares(settings)
    out = []
    for a in session.scalars(select(Artwork).where(Artwork.canonical_id == cid).order_by(Artwork.priority,
                                                                                         Artwork.created_at)):
        if a.local_only or (a.fetched_from or "").startswith(GROUP_PREFIX) or a.source == "upload":
            continue
        if a.origin_node or (a.redistributable and getattr(settings, "role", "") == "authority"):
            scope_ = "public"
        elif share_media and source_class(a.source) == "provider" and a.source in PROVIDER_SOURCES:
            scope_ = "group"
        else:
            continue
        if bytes_only and media.policy(settings, a.source).fetch != "proxy":
            continue  # §3.6 (critic): a source whose terms forbid caching never travels as bytes
        out.append((a, scope_))
    return out


def _holding(session: Session, settings: Any, row: Canonical) -> dict | None:
    """What this node can answer for `row`, or None when it answers `unknown` (§3.3)."""
    public, provider = _shareable(session, settings, row)
    if not public and not provider:
        return None
    art = _art_rows(session, settings, row.id)
    return {"public": public, "provider": provider, "artwork": art}


def _introducing(session: Session, cid: str, limit: int = MAX_ENTRIES) -> list[ReplicatedChange]:
    """This node's retained, unaltered feed entries that introduce `cid` (`obs`, `record`, bridge `obs`, `detach`)."""
    pairs = session.execute(select(Observation.origin_node, Observation.origin_seq).where(
        Observation.canonical_id == cid, Observation.origin_node.is_not(None), Observation.origin_seq.is_not(None))
        .order_by(Observation.origin_seq).limit(limit * 4)).all()
    out: list[ReplicatedChange] = []
    for node, seq in pairs:
        entry = session.get(ReplicatedChange, (node, seq))
        if entry is None or entry.kind not in INTRODUCING:
            continue
        body = entry.body or {}
        named = body.get("new_canonical_id") if entry.kind == "detach" else body.get("canonical_id")
        if named == cid and entry not in out:
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def _applied_from(session: Session, cid: str) -> list[str]:
    """The feeds whose signed entries this node applied to `cid` (§3.3 `applied_from`)."""
    feeds = set(session.scalars(select(Observation.origin_node).where(
        Observation.canonical_id == cid, Observation.origin_node.is_not(None)).distinct()))
    feeds |= set(session.scalars(select(Artwork.origin_node).where(
        Artwork.canonical_id == cid, Artwork.origin_node.is_not(None)).distinct()))
    return sorted(f for f in feeds if f)[:32]


def _record_hash(record: dict) -> str:
    from .signing import canonical_json

    return hashlib.sha256(canonical_json(record)).hexdigest()


# ---- helper: assist.find ------------------------------------------------------------------------------------------

def _validate_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= FIND_MAX:
        raise AssistError("invalid", "ids: 1 to 100 record ids")
    if not all(isinstance(i, str) and ID_RE.match(i) for i in raw):
        raise AssistError("invalid", "ids: atlas record ids only")
    return list(dict.fromkeys(raw))


def _validate_want(raw: Any) -> list[str]:
    if raw is None:
        return ["record", "artwork"]
    if not isinstance(raw, list) or not all(w in WANTS for w in raw):
        raise AssistError("invalid", "want: a subset of record, artwork, achievements")
    return list(dict.fromkeys(raw))


def _unknown(cid: str) -> dict:
    return {"id": cid, "state": "unknown", "survivor": None, "bundle_hash": None,
            "has": {"record": False, "artwork": 0, "achievements": 0}, "introduced": [], "applied_from": []}


def find_one(session: Session, settings: Any, cid: str) -> dict:
    """One `assist.find` item. A record with a `private` or `local` ceiling, or one this node holds only from the
    group, answers exactly like an absent id (§3.7)."""
    from .resolve import follow

    row = session.get(Canonical, cid)
    if row is None:
        return _unknown(cid)
    if row.merged_into is not None:
        survivor = follow(session, cid)
        target = session.get(Canonical, survivor)
        if target is None or survivor == cid or _holding(session, settings, target) is None:
            return _unknown(cid)
        return {**_unknown(cid), "state": "merged", "survivor": survivor}
    held = _holding(session, settings, row)
    if held is None:
        return _unknown(cid)
    record = _record_body(session, settings, row, held, ["record", "artwork"])
    return {"id": cid, "state": "live", "survivor": None, "bundle_hash": _record_hash(record),
            "has": {"record": True, "artwork": len(held["artwork"]), "achievements": 0},
            "introduced": [{"feed": e.node_id, "seq": e.seq, "kind": e.kind} for e in _introducing(session, cid)],
            "applied_from": _applied_from(session, cid)}


def serve_find(session: Session, settings: Any, params: dict) -> dict:
    if answer_mode(settings) == "off":
        raise AssistError("forbidden")
    ids = _validate_ids(params.get("ids"))
    _validate_want(params.get("want"))
    if params.get("reason") not in REASONS:
        raise AssistError("invalid", "reason")
    return {"items": [find_one(session, settings, cid) for cid in ids], "as_of": gc.stamp()}


# ---- helper: assist.fetch, the bundle -----------------------------------------------------------------------------

def _record_body(session: Session, settings: Any, row: Canonical, held: dict, want: list[str]) -> dict:
    view = row.view or {}
    from . import feed

    observations: list[dict] = []
    title = view.get("title") if isinstance(view.get("title"), str) else None
    ids = feed.strip_ids(view.get("provider_ids"))
    identity: dict[str, Any] = {}
    if title:
        identity["title"] = title[:500]
    if ids:
        identity["provider_ids"] = ids
    if identity:
        observations.append({"source": GROUP_SOURCE, "external_id": row.id, "scope": "public",
                             "payload": canon(identity)})
    for o in held["public"]:
        payload = canon(o.payload or {})
        if payload:
            observations.append({"source": GROUP_SOURCE, "external_id": f"{row.id}#{o.id}"[:256], "scope": "public",
                                 "payload": payload})
    for o in held["provider"]:
        payload = canon(o.payload or {})
        if payload:
            observations.append({"source": o.source, "external_id": o.external_id[:256], "scope": "group",
                                 "payload": payload})
    artwork = []
    if "artwork" in want:
        for a, scope_ in held["artwork"][:MAX_ARTWORK]:
            item = {"kind": a.kind, "url": a.url, "source": a.source, "licence": a.licence or "provider_terms",
                    "scope": scope_, "width": a.width, "height": a.height,
                    "sha256": a.blob_hash if a.blob_hash and _bytes_shareable(settings, a, scope_) else None}
            artwork.append(canon({k: v for k, v in item.items() if v is not None}))
    return {"id": row.id, "media_type": row.media_type, "survivor": None, "title": title, "provider_ids": ids,
            "observations": observations[:MAX_OBSERVATIONS], "artwork": artwork, "achievements": None}


def _bytes_shareable(settings: Any, a: Artwork, scope_: str) -> bool:
    from . import media

    return scope_ == "public" or media.policy(settings, a.source).fetch == "proxy"


def _signed_entry(e: ReplicatedChange) -> dict:
    return {"node_id": e.node_id, "seq": e.seq, "kind": e.kind, "body": e.body, "body_hash": e.body_hash,
            "key_id": e.key_id, "signature": e.signature,
            "created_at": _iso(e.created_at) if e.created_at else None}


def serve_fetch(session: Session, settings: Any, identity: Any, group: Any, asker: str, request_nonce: str,
                params: dict) -> dict:
    """`assist.fetch`: a bundle signed by this node over what it holds of one record, bound to the asker and the
    request nonce, plus at most 10 retained introducing entries (signed by their own feeds, unaltered)."""
    from . import feed
    from .signing import canonical_json

    if answer_mode(settings) == "off":
        raise AssistError("forbidden")
    cid = params.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise AssistError("invalid", "id")
    want = _validate_want(params.get("want"))
    row = session.get(Canonical, cid)
    held = _holding(session, settings, row) if row is not None else None
    if held is None:
        raise AssistError("not_found")  # identical to an unknown id (§3.7)
    record = _record_body(session, settings, row, held, want)
    body: dict[str, Any] = {"kind": "bundle", "requester": asker, "request_nonce": request_nonce,
                            "issued_at": gc.stamp(), "record": record, "applied_from": _applied_from(session, cid),
                            "helper_heads": {}}
    if getattr(settings, "role", "") == "authority" and session.get(FeedIntroduced, cid) is not None:
        # class 1(b): this node's own feed vouches for the record outside the feed (signed by its feed key)
        body["vouch"] = {"feed": identity.node_id, "cid": cid, "head": feed.head_seq(session)}
    entries = [_signed_entry(e) for e in _introducing(session, cid)] if params.get("signed_entries") else []
    limit = int(getattr(settings, "group_assist_max_answer", 512 * 1024))
    truncated = False

    def size() -> int:
        return len(canonical_json({"body": body, "signed_entries": entries}))

    if size() > limit:  # §3.3: descriptions first, then artwork beyond 24, then the carried entries
        truncated = True
        for o in record["observations"]:
            facts = o["payload"].get("facts")
            if isinstance(facts, dict):
                facts.pop("description", None)
        if size() > limit:
            record["artwork"] = record["artwork"][:TRUNCATED_ARTWORK]
        if size() > limit:
            entries = []
        if size() > limit:
            raise AssistError("too_large")
    bundle = gc.make_object("group.assist", group.group_id, f"assist:{identity.node_id}/{ulid()}", canon(body),
                            {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)
    return {"bundle": bundle, "signed_entries": entries, "truncated": truncated}


# ---- helper: assist.blob ------------------------------------------------------------------------------------------

def blob_shareable(session: Session, settings: Any, digest: str) -> bool:
    """§3.6 serving rule: a blob is served only if a row here that references it may travel (public, or a provider
    row while this node shares media in the group) and its source's policy here is `proxy`."""
    if answer_mode(settings) == "off":
        return False
    for a in session.scalars(select(Artwork).where(Artwork.blob_hash == digest)):
        rec = session.get(Canonical, a.canonical_id)
        if rec is None or rec.scope_ceiling is not None:
            continue
        for row, _scope in _art_rows(session, settings, a.canonical_id, bytes_only=True):
            if row.id == a.id:
                return True
    return False


def serve_blob(session: Session, settings: Any, asker: str, params: dict) -> dict:
    from . import media, ratelimit

    digest = params.get("sha256")
    offset, length = params.get("offset", 0), params.get("length", BLOB_CHUNK)
    if not isinstance(digest, str) or not SHA_RE.match(digest) or type(offset) is not int or offset < 0 \
            or type(length) is not int or not 1 <= length <= BLOB_CHUNK:
        raise AssistError("invalid")
    if not blob_shareable(session, settings, digest):
        raise AssistError("not_found")  # identical to an unknown digest
    found = media.find_blob(settings, digest)
    if found is None:
        raise AssistError("not_found")
    path, ext = found
    size = path.stat().st_size
    if offset > size:
        raise AssistError("invalid")
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
    ok, retry = ratelimit.take(session, [(f"assist-bytes:{asker}", BYTES_PER_HOUR, 3600)], amount=max(1, len(data)))
    if not ok:
        raise Throttled(retry)
    from .instruments import group_assist_bytes

    group_assist_bytes.inc(len(data), role="served")
    return {"sha256": digest, "size": size, "content_type": media.EXT_TYPES.get(ext, "application/octet-stream"),
            "offset": offset, "data": base64.b64encode(data).decode(), "eof": offset + len(data) >= size}


class Throttled(AssistError):
    def __init__(self, retry_after: int):
        super().__init__("throttled")
        self.retry_after = max(1, int(retry_after))


def limit_call(maker: Any, asker: str, op: str) -> tuple[bool, int]:
    """§3.10 per-asker limits for one call, counted in their own committed transaction before the call writes
    (all windows or none; a refused take counts nothing). Returns (allowed, retry_after)."""
    from . import ratelimit

    windows = [(f"assist:{asker}", ANSWER_PER_MIN, 60)]
    if op == "assist.fetch":
        windows.append((f"assist-fetch:{asker}", FETCH_PER_HOUR, 3600))
    return ratelimit.take(maker, windows)


# ---- asker: the job queue (§3.1) ------------------------------------------------------------------------------------

def _open_jobs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(NodeState).where(NodeState.key.like(JOB + "%"))) or 0


def job_of(session: Session, cid: str) -> dict | None:
    return _json(session, JOB + _hash(cid))


def done_of(session: Session, cid: str) -> dict | None:
    return _json(session, DONE + _hash(cid))


def note_missing(session: Session, settings: Any, cid: str, reason: str = "waiting_for_record",
                 feed: str | None = None, *, force: bool = False) -> bool:
    """Enqueue one job per record this node lacks (§3.1). Does nothing outside an active group or with
    `group_assist_ask` off. A second wait row for the same record only adds its feed. Above
    `group_assist_max_jobs` new jobs are dropped and counted; their waiting rows still wake through the feed.
    `force` (the **Ask my nodes** button) reopens a job that gave up and makes it due now."""
    from . import scope

    if not getattr(settings, "group_assist_ask", True) or reason not in REASONS or not ID_RE.match(str(cid)):
        return False
    if not scope.in_group(session):
        return False
    key = JOB + _hash(cid)
    job = _json(session, key)
    stamp = _iso(now())
    if job is not None:
        changed = False
        if feed and feed not in job["feeds"]:
            job["feeds"] = (job["feeds"] + [feed])[:32]
            changed = True
        if reason not in job["reasons"]:
            job["reasons"].append(reason)
            changed = True
        if force:
            job["next_at"], changed = stamp, True
        if changed:
            _put(session, key, job)
        return True
    done = done_of(session, cid)
    if done is not None and not force and done.get("outcome") in ("none_has_it", "media_type_not_adopted"):
        return False  # it gave up; only the button asks again (§3.2)
    if _open_jobs(session) >= int(getattr(settings, "group_assist_max_jobs", 5000)):
        row = _state(session, DROPPED)
        _put(session, DROPPED, str(int(row.value) + 1 if row is not None and row.value.isdigit() else 1))
        return False
    if done is not None:
        _drop(session, DONE + _hash(cid))
    _put(session, key, {"cid": cid, "reasons": [reason], "feeds": [feed] if feed else [], "first_at": stamp,
                        "next_at": stamp, "tries": 0, "asked": []})
    return True


def _due_jobs(session: Session, limit: int) -> list[dict]:
    t = now()
    out = []
    for row in session.scalars(select(NodeState).where(NodeState.key.like(JOB + "%")).order_by(NodeState.key)):
        try:
            job = json.loads(row.value)
        except ValueError:
            continue
        if (_parse(job.get("next_at")) or t) <= t:
            out.append(job)
    out.sort(key=lambda j: j.get("next_at") or "")
    return out[:limit]


def _close(session: Session, job: dict, outcome: str, **extra: Any) -> None:
    _drop(session, JOB + _hash(job["cid"]))
    _put(session, DONE + _hash(job["cid"]), {"cid": job["cid"], "outcome": outcome, "at": _iso(now()),
                                             "asked": job.get("asked") or [], **extra})


def _backoff(session: Session, settings: Any, job: dict) -> None:
    tries = int(job.get("tries") or 0) + 1
    first = _parse(job.get("first_at")) or now()
    if now() - first >= timedelta(days=int(getattr(settings, "group_assist_give_up_days", 30))):
        _close(session, job, "none_has_it")
        return
    delay = BACKOFF[tries - 1] if tries <= len(BACKOFF) else timedelta(days=1)
    job.update(tries=tries, next_at=_iso(now() + delay), last_asked=_iso(now()))
    _put(session, JOB + _hash(job["cid"]), job)


# ---- asker: the per-group backfill cursor (§3.1, Review #1 C14-14) ---------------------------------------------------

def backfill_step(session: Session, settings: Any, group_id: str, limit: int = BACKFILL_SCAN) -> dict:
    """Scan at most `limit` further open waiting rows (oldest first by (feed, seq)) and enqueue jobs while the queue
    has room. The cursor advances only past rows that got (or already had) a job: a full queue pauses it, it never
    skips. One cursor per group: a new group starts its own; leaving a group drops its cursor."""
    key = BACKFILL + group_id
    for stale in session.scalars(select(NodeState.key).where(NodeState.key.like(BACKFILL + "%"),
                                                             NodeState.key != key)).all():
        _drop(session, stale)
    cur = _json(session, key) or {"after": None, "done": False, "started_at": _iso(now())}
    if cur.get("done"):
        return {"scanned": 0, "queued": 0, "done": True}
    query = select(SyncFailure.node_id, SyncFailure.seq, SyncFailure.waiting_for).where(
        SyncFailure.disposition == "wait", SyncFailure.resolved_at.is_(None), SyncFailure.waiting_for.like("atlas:%"))
    after = cur.get("after")
    if isinstance(after, list) and len(after) == 2:
        query = query.where(or_(SyncFailure.node_id > after[0],
                                and_(SyncFailure.node_id == after[0], SyncFailure.seq > after[1])))
    rows = session.execute(query.order_by(SyncFailure.node_id, SyncFailure.seq).limit(limit)).all()
    queued = scanned = 0
    room = int(getattr(settings, "group_assist_max_jobs", 5000)) - _open_jobs(session)
    paused = False
    for node, seq, cid in rows:
        if not ID_RE.match(str(cid)):
            cur["after"] = [node, seq]
            scanned += 1
            continue
        if job_of(session, cid) is None and done_of(session, cid) is None:
            if room <= 0:
                paused = True
                break
            if note_missing(session, settings, cid, "waiting_for_record", node):
                queued += 1
                room -= 1
            else:
                paused = True
                break
        elif (job := job_of(session, cid)) is not None and node not in job["feeds"]:
            note_missing(session, settings, cid, "waiting_for_record", node)
        cur["after"] = [node, seq]
        scanned += 1
    if not paused and len(rows) < limit:
        cur["done"] = True
    _put(session, key, cur)
    return {"scanned": scanned, "queued": queued, "done": bool(cur.get("done")), "paused": paused}


# ---- asker: verifying and applying a bundle (§3.4) ------------------------------------------------------------------

def is_suspect(session: Session, helper: str) -> bool:
    until = _parse((_state(session, SUSPECT + helper) or NodeState(value="")).value)
    return until is not None and until > now()


def suspect(session: Session, helper: str, reason: str) -> None:
    """§3.10: a helper that sent a bad signature, a wrong binding, a hash that does not match or an invalid image
    is suspect for one hour: its class 2 wakes pause (it is not forgotten, critic of Review #1)."""
    from . import groups
    from .instruments import group_assist_rejected

    _put(session, SUSPECT + helper, _iso(now() + SUSPECT_FOR))
    group_assist_rejected.inc(reason=reason if reason in REJECT_REASONS else "bad_signature")
    groups.audit(session, None, "group_assist_suspect", helper, {"reason": reason})


REJECT_REASONS = ("bad_signature", "wrong_requester", "hash_mismatch", "bad_image", "too_large")


def verify_bundle(session: Session, identity: Any, group: Any, tr: Any, helper: str, bundle: Any, asked: str,
                  nonce_b64: str) -> dict:
    """§3.4 step 1. Returns the body or raises AssistError(<closed reason>)."""
    if not isinstance(bundle, dict) or bundle.get("t") != "group.assist" or bundle.get("group_id") != group.group_id:
        raise AssistError("bad_signature")
    signer = bundle.get("signer") or {}
    key = tr.current_key(helper)
    if key is None or signer.get("kind") != "node" or signer.get("id") != helper or signer.get("key_id") != key[0]:
        raise AssistError("bad_signature")
    if not gc.verify_object(bundle, key[1]):
        raise AssistError("bad_signature")
    body = bundle["body"]
    if body.get("kind") != "bundle" or body.get("requester") != identity.node_id \
            or body.get("request_nonce") != nonce_b64:
        raise AssistError("wrong_requester")
    issued = _parse(body.get("issued_at"))
    if issued is None or abs((now() - issued).total_seconds()) > gc.SKEW:
        raise AssistError("wrong_requester", "stale bundle")
    record = body.get("record")
    if not isinstance(record, dict) or record.get("id") != asked:
        raise AssistError("hash_mismatch", "the bundle describes another record")
    return body


def _valid_payload(external_id: str, payload: Any) -> bool:
    from pydantic import ValidationError

    from .contract import SourceObservationV1
    from .feed_schema import OBS_PAYLOAD

    try:
        if not isinstance(payload, dict) or set(payload) - set(OBS_PAYLOAD) or len(json.dumps(payload)) > 100_000:
            return False
        SourceObservationV1.model_validate({"external_id": external_id, **payload})
        return True
    except (ValidationError, ValueError, TypeError):
        return False


def _safe_url(url: Any) -> bool:
    """Bundle and lookup URLs are untrusted (§4.3 step 7): https, no userinfo, no key-like query names. The bytes are
    only ever fetched through `media._request` (public-address check on every resolved address, pinned IP, every
    redirect re-validated)."""
    from urllib.parse import parse_qsl, urlsplit

    if not isinstance(url, str) or len(url) > 1000 or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or port not in (None, 443):
        return False
    return not any(KEYLIKE.search(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True))


KEYLIKE = re.compile(r"key|token|sig|signature|secret|auth|client_id|access", re.IGNORECASE)


def _media_type_adopted(session: Session, media_type: Any) -> bool:
    return isinstance(media_type, str) and session.get(MediaType, media_type) is not None


def apply_bundle(session: Session, ctx: Any, helper: str, body: dict, bundle: dict, entries: list[dict]) -> dict:
    """§3.4 steps 2-8 in the caller's transaction (which holds the write lock). Returns
    {"outcome": applied|exists|media_type_not_adopted, "item": id, "woken": {feed: n}}."""
    from . import service
    from .facets import KINDS

    record = body["record"]
    cid = record["id"]
    if not _media_type_adopted(session, record.get("media_type")):
        return {"outcome": "media_type_not_adopted"}  # T-A18: never a `game` fallback
    row = session.get(Canonical, cid)
    if row is not None and row.scope_ceiling_by != "assist":
        return {"outcome": "exists"}  # it arrived some other way meanwhile
    item = GroupAssistItem(id=ulid(), helper=helper, cid=cid, kind="bundle", body_hash=bundle["body_hash"],
                           body={"helper": helper}, envelope=bundle, signed_entries=entries[:MAX_ENTRIES] or None,
                           received_at=now(), applied_at=now())
    session.add(item)
    if row is None:
        row = Canonical(id=cid, view={}, explain={}, media_type=record["media_type"], scope_ceiling="group",
                        scope_ceiling_by="assist")
        session.add(row)
    session.flush()
    for prior in session.scalars(select(GroupAssistItem).where(
            GroupAssistItem.cid == cid, GroupAssistItem.helper == helper, GroupAssistItem.kind == "bundle",
            GroupAssistItem.id != item.id, GroupAssistItem.superseded_at.is_(None))):
        prior.superseded_at = now()
    written: list[Observation] = []
    for o in (record.get("observations") or [])[:MAX_OBSERVATIONS]:
        if not isinstance(o, dict):
            continue
        source, external_id, payload = o.get("source"), o.get("external_id"), o.get("payload")
        if source == GROUP_SOURCE:
            source_scope = helper
        elif source in PROVIDER_SOURCES:
            source_scope = GROUP_PREFIX + helper
        else:
            continue  # a closed set: the group source, or a provider this release ships
        if not isinstance(external_id, str) or not 0 < len(external_id) <= 256 \
                or not _valid_payload(external_id, payload):
            continue
        for prior in session.scalars(select(Observation).where(
                Observation.source == source, Observation.source_scope == source_scope,
                Observation.external_id == external_id, Observation.is_current.is_(True))):
            prior.is_current = False
        obs = Observation(id=ulid(), canonical_id=cid, source_kind="metadata", source=source,
                          source_scope=source_scope, external_id=external_id, payload=payload,
                          redistributable=False, origin_node=None, is_current=True, assist_item_id=item.id)
        session.add(obs)
        written.append(obs)
    session.flush()
    taken: set[tuple[str, str]] = set()
    for obs in written:  # §3.4 step 5: only free crosswalk keys; never a merge or fold-in
        for key in service.keys_of(obs):
            if key not in taken and session.get(Crosswalk, key) is None:
                taken.add(key)
                session.add(Crosswalk(namespace=key[0], external_id=key[1], canonical_id=cid))
    for a in (record.get("artwork") or [])[:MAX_ARTWORK]:
        if not isinstance(a, dict) or a.get("kind") not in KINDS or not _safe_url(a.get("url")):
            continue
        if not isinstance(a.get("source"), str) or a["source"] not in PROVIDER_SOURCES | {"curator", "community"}:
            continue
        if session.scalars(select(Artwork.id).where(Artwork.canonical_id == cid, Artwork.kind == a["kind"],
                                                    Artwork.url == a["url"])).first() is not None:
            continue
        sha = a.get("sha256") if isinstance(a.get("sha256"), str) and SHA_RE.match(a["sha256"]) else None
        dims = {k: a[k] for k in ("width", "height") if type(a.get(k)) is int and 0 < a[k] <= 20000}
        session.add(Artwork(canonical_id=cid, source=a["source"], kind=a["kind"], url=a["url"],
                            licence=str(a.get("licence") or "provider_terms")[:24], redistributable=False,
                            fetched_from=GROUP_PREFIX + helper, peer_digest=sha,
                            digest_provenance="group_reported" if sha else None, **dims))
    session.flush()
    service.rebuild(session, ctx, cid, cause="local")
    return {"outcome": "applied", "item": item.id}


# ---- asker: when an answer may wake waiting entries (§3.5) ----------------------------------------------------------

def _waiting_rows(session: Session, feed_id: str | None, cid: str) -> list[SyncFailure]:
    q = select(SyncFailure).where(SyncFailure.disposition == "wait", SyncFailure.resolved_at.is_(None),
                                  SyncFailure.waiting_for == cid)
    if feed_id is not None:
        q = q.where(SyncFailure.node_id == feed_id)
    return list(session.scalars(q.order_by(SyncFailure.node_id, SyncFailure.seq)))


def pinned_keys(session: Session, feed_id: str) -> tuple[list[dict], dict[str, str]] | None:
    """This node's own pin and validated rotation history for feed F, or None when it does not pin F."""
    from . import key_rotation

    pin = session.get(ReplicatedFeed, feed_id)
    if pin is None:
        return None
    history = key_rotation.trusted_history(session, feed_id)
    current = history[-1]["body"]["key_id"] if history else feed_id
    try:
        return key_rotation.validate_history(feed_id, pin.public_key, history, current)
    except key_rotation.RotationError:
        return None


def verify_entry(session: Session, entry: Any, cid: str) -> str | None:
    """Class 1(a): the feed id of an introducing entry for `cid` signed by that feed and verified against this
    node's own pin and rotation history for it, or None."""
    from . import key_rotation

    if not isinstance(entry, dict) or entry.get("kind") not in INTRODUCING or not isinstance(entry.get("body"), dict):
        return None
    try:
        e = {f: entry[f] for f in ("node_id", "seq", "kind", "body", "body_hash", "signature", "key_id")}
    except KeyError:
        return None
    named = e["body"].get("new_canonical_id") if e["kind"] == "detach" else e["body"].get("canonical_id")
    if named != cid or not isinstance(e["node_id"], str):
        return None
    pinned = pinned_keys(session, e["node_id"])
    if pinned is None:
        return None
    history, keys = pinned
    try:
        ok = key_rotation.check_entry(e["node_id"], history, keys, e)
    except (KeyError, TypeError, ValueError):
        ok = False
    return e["node_id"] if ok else None


def verify_vouch(session: Session, bundle: dict, body: dict, cid: str) -> str | None:
    """Class 1(b): the bundle carries `vouch = {feed: F, cid, head}` and its signature verifies under the key this
    node's own pin and rotation history give as F's current feed key (the member certificate is not enough)."""
    vouch = body.get("vouch")
    if not isinstance(vouch, dict) or vouch.get("cid") != cid or not isinstance(vouch.get("feed"), str):
        return None
    feed_id = vouch["feed"]
    pinned = pinned_keys(session, feed_id)
    if pinned is None:
        return None
    history, keys = pinned
    current = history[-1]["body"]["key_id"] if history else feed_id
    if bundle.get("signer", {}).get("key_id") != current or not gc.verify_object(bundle, keys[current]):
        return None
    return feed_id


def evidence(session: Session, feed_id: str, cid: str) -> list[GroupAssistItem]:
    rows = session.scalars(select(GroupAssistItem).where(GroupAssistItem.cid == cid,
                                                         GroupAssistItem.kind.in_(EVIDENCE_KINDS),
                                                         GroupAssistItem.superseded_at.is_(None)))
    return [r for r in rows if (r.body or {}).get("feed") == feed_id]


def feed_may_apply(session: Session, feed_id: str, cid: str) -> bool:
    """§3.5: feed F counts as having introduced `cid` through the group (class 1 evidence or an accepted class 2
    wake). Used by `sync.introduced_by` and by `sync.missing_record` for records created from a bundle."""
    return bool(evidence(session, feed_id, cid))


def _wake(session: Session, ctx: Any, feed_id: str, cid: str) -> int:
    from . import sync

    return sync.wake_waiting(session, ctx, feed_id, cid, trigger="group", late=True)


def settle_wakes(session: Session, ctx: Any, helper: str, cid: str, bundle: dict, body: dict,
                 entries: list[dict]) -> dict[str, str]:
    """§3.5 for every feed with rows waiting for `cid`. Returns feed -> how it was handled."""
    from . import groups

    out: dict[str, str] = {}
    verified: dict[str, list[dict]] = {}
    for e in entries[:MAX_ENTRIES]:
        feed_id = verify_entry(session, e, cid)
        if feed_id is not None:
            verified.setdefault(feed_id, []).append(e)
    vouched = verify_vouch(session, bundle, body, cid)
    applied_from = {f for f in (body.get("applied_from") or []) if isinstance(f, str)}
    row = session.get(Canonical, cid)
    for feed_id in sorted({r.node_id for r in _waiting_rows(session, None, cid)} | set(verified)):
        if feed_id in verified:
            for e in verified[feed_id]:
                session.add(GroupAssistItem(id=ulid(), helper=helper, cid=cid, kind="entry",
                                            body={"feed": feed_id, "seq": e["seq"], "kind": e["kind"]},
                                            body_hash=str(e.get("body_hash"))[:64], envelope=e, received_at=now(),
                                            applied_at=now()))
            session.flush()
            out[feed_id] = "class1a"
        elif vouched == feed_id:
            session.add(GroupAssistItem(id=ulid(), helper=helper, cid=cid, kind="vouch", body={"feed": feed_id},
                                        body_hash=bundle["body_hash"], envelope=bundle, received_at=now(),
                                        applied_at=now()))
            session.flush()
            out[feed_id] = "class1b"
        elif feed_id in applied_from and row is not None and row.scope_ceiling_by == "assist":
            seqs = [r.seq for r in _waiting_rows(session, feed_id, cid)]
            if not seqs:
                continue
            if is_suspect(session, helper):
                out[feed_id] = "paused"
                continue
            if getattr(ctx.settings, "group_assist_auto_adopt", False):
                session.add(GroupAssistItem(id=ulid(), helper=helper, cid=cid, kind="wake",
                                            body={"feed": feed_id, "seqs": seqs[:5000], "helper": helper},
                                            body_hash=bundle["body_hash"], received_at=now(), applied_at=now()))
                session.flush()
                out[feed_id] = "class2"
            else:
                _offer(session, helper, cid, feed_id, bundle)
                out[feed_id] = "offer"
                continue
        else:
            out[feed_id] = "not_from_feed"
            continue
        n = _wake(session, ctx, feed_id, cid)
        if out[feed_id] == "class2":
            groups.audit(session, None, "group_assist_adopt", cid, {"feed": feed_id, "helper": helper, "woken": n,
                                                                   "auto": True})
    return out


def _offer(session: Session, helper: str, cid: str, feed_id: str, bundle: dict) -> None:
    for old in session.scalars(select(GroupAssistItem).where(
            GroupAssistItem.cid == cid, GroupAssistItem.kind == "offer", GroupAssistItem.superseded_at.is_(None))):
        if (old.body or {}).get("feed") == feed_id:
            old.superseded_at = now()
    session.add(GroupAssistItem(id=ulid(), helper=helper, cid=cid, kind="offer",
                                body={"feed": feed_id, "helper": helper}, body_hash=bundle["body_hash"],
                                received_at=now()))
    session.flush()


def adopt(session: Session, ctx: Any, item_id: str, actor: str | None) -> int:
    """The "Use what <helper> has" click (§3.5 class 2 without the preset). Returns the entries woken."""
    from . import groups

    offer = session.get(GroupAssistItem, item_id)
    if offer is None or offer.kind != "offer" or offer.superseded_at is not None:
        raise AssistError("not_found")
    feed_id = (offer.body or {}).get("feed")
    row = session.get(Canonical, offer.cid)
    if not isinstance(feed_id, str) or row is None or row.scope_ceiling_by != "assist":
        raise AssistError("not_found")
    seqs = [r.seq for r in _waiting_rows(session, feed_id, offer.cid)]
    offer.superseded_at = now()
    session.add(GroupAssistItem(id=ulid(), helper=offer.helper, cid=offer.cid, kind="wake",
                                body={"feed": feed_id, "seqs": seqs[:5000], "helper": offer.helper},
                                body_hash=offer.body_hash, received_at=now(), applied_at=now()))
    session.flush()
    n = _wake(session, ctx, feed_id, offer.cid)
    groups.audit(session, actor, "group_assist_adopt", offer.cid, {"feed": feed_id, "helper": offer.helper,
                                                                    "woken": n, "auto": False})
    return n


def on_feed_introduced(session: Session, ctx: Any, feed_id: str, cid: str) -> bool:
    """§3.4 step 9: this node's own pull of feed F applied an introducing entry for a record created from a
    bundle. The group's identity observation retires, the `group` ceiling the assist set is lifted (an operator's
    ceiling never is), class 2 wakes of F for it are confirmed. Returns True when anything changed."""
    from . import scope, service

    row = session.get(Canonical, cid)
    if row is None or row.scope_ceiling_by != "assist":
        return False
    for o in session.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                       Observation.source == GROUP_SOURCE,
                                                       Observation.is_current.is_(True))):
        o.is_current = False
    own = {(o.source, o.external_id) for o in session.scalars(select(Observation).where(
        Observation.canonical_id == cid, Observation.is_current.is_(True), Observation.assist_item_id.is_(None),
        ~Observation.source_scope.like(GROUP_PREFIX + "%")))}
    for o in session.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                       Observation.is_current.is_(True),
                                                       Observation.source_scope.like(GROUP_PREFIX + "%"))):
        if (o.source, o.external_id) in own:
            o.is_current = False
    for item in session.scalars(select(GroupAssistItem).where(GroupAssistItem.cid == cid,
                                                              GroupAssistItem.kind.in_(("wake", "entry", "vouch")),
                                                              GroupAssistItem.superseded_at.is_(None))):
        if (item.body or {}).get("feed") == feed_id:
            item.superseded_at = now()  # confirmed by the feed itself: no longer the reason anything applied
    row.scope_ceiling, row.scope_ceiling_by = None, None
    session.flush()
    service.rebuild(session, ctx, cid, cause="local")
    scope.reproject(session, ctx, cid)
    return True


# ---- forgetting a helper (§14, T-A19, Review #1 C14-2) -------------------------------------------------------------

def forget(session: Session, ctx: Any, helper: str, *, dry_run: bool = False, reason: str = "operator",
           actor: str | None = None) -> dict[str, int]:
    """`atlas group assist forget <node>`: drop everything that helper's answers changed here. Class 2 wakes not
    confirmed by the feed itself go back to waiting (their applied observations and artwork are removed), the
    helper's observations, artwork rows and evidence are retired, and records that only the helper's answer had
    created are removed (their waiting entries wait again). `dry_run` counts without changing anything."""
    from . import groups, scope, service

    counts = {"rewaited": 0, "observations": 0, "artwork": 0, "records": 0, "items": 0, "offers": 0}
    items = list(session.scalars(select(GroupAssistItem).where(GroupAssistItem.helper == helper,
                                                               GroupAssistItem.superseded_at.is_(None))))
    touched: set[str] = set()
    for item in items:
        counts["items"] += 1
        touched.add(item.cid)
        if item.kind == "offer":
            counts["offers"] += 1
        if item.kind != "wake":
            continue
        feed_id, seqs = (item.body or {}).get("feed"), [s for s in (item.body or {}).get("seqs") or []
                                                         if type(s) is int]
        if not isinstance(feed_id, str) or not seqs:
            continue
        rows = list(session.scalars(select(SyncFailure).where(SyncFailure.node_id == feed_id,
                                                               SyncFailure.seq.in_(seqs))))
        counts["rewaited"] += len(rows)
        if dry_run:
            continue
        for r in rows:
            r.disposition, r.reason, r.waiting_for, r.resolved_at = "wait", "waiting_for_record", item.cid, None
        session.execute(delete(Observation).where(Observation.origin_node == feed_id,
                                                  Observation.origin_seq.in_(seqs)))
        counts["artwork"] += session.execute(delete(Artwork).where(Artwork.canonical_id == item.cid,
                                                                   Artwork.origin_node == feed_id)).rowcount or 0
    own_obs = select(Observation.id).where(or_(Observation.source_scope == helper,
                                               Observation.source_scope == GROUP_PREFIX + helper),
                                           Observation.assist_item_id.is_not(None))
    obs_ids = list(session.scalars(own_obs))
    counts["observations"] = len(obs_ids)
    art_q = select(Artwork).where(Artwork.fetched_from == GROUP_PREFIX + helper)
    art_rows = list(session.scalars(art_q))
    counts["artwork"] += len(art_rows)
    touched |= {cid for cid in session.scalars(select(Observation.canonical_id).where(Observation.id.in_(obs_ids)))}
    touched |= {a.canonical_id for a in art_rows}
    if dry_run:
        counts["records"] = sum(1 for cid in touched if _only_from_group(session, cid, set(obs_ids)))
        return counts
    session.execute(delete(Observation).where(Observation.id.in_(obs_ids)))
    for a in art_rows:
        session.delete(a)
    for item in items:
        item.superseded_at = now()
    session.flush()
    for cid in sorted(touched):
        row = session.get(Canonical, cid)
        if row is None or row.merged_into is not None:
            continue
        if row.scope_ceiling_by == "assist" and _removable(session, cid):
            session.execute(delete(Crosswalk).where(Crosswalk.canonical_id == cid))
            session.execute(delete(ScopeProjection).where(ScopeProjection.canonical_id == cid))
            session.delete(row)
            counts["records"] += 1
            continue
        service.rebuild(session, ctx, cid, cause="local")
        scope.reproject(session, ctx, cid)
    session.flush()
    groups.audit(session, actor, "group_assist_forget", helper, {"reason": reason, **counts})
    return counts


def _only_from_group(session: Session, cid: str, gone: set[str]) -> bool:
    row = session.get(Canonical, cid)
    if row is None or row.scope_ceiling_by != "assist":
        return False
    left = session.scalars(select(Observation.id).where(Observation.canonical_id == cid)).all()
    return all(i in gone for i in left)


def _removable(session: Session, cid: str) -> bool:
    for model, column in ((Observation, Observation.canonical_id), (Artwork, Artwork.canonical_id),
                          (Achievement, Achievement.canonical_id), (CustomValue, CustomValue.canonical_id),
                          (Lock, Lock.canonical_id), (ListItem, ListItem.canonical_id)):
        if session.scalars(select(func.count()).select_from(model).where(column == cid)).first():
            return False
    return not session.scalars(select(Relation.id).where(or_(Relation.from_id == cid, Relation.to_id == cid))
                               .limit(1)).first()


def forget_removed(session: Session, ctx: Any, tr: Any) -> list[str]:
    """§3.5 (iv): a helper removed from the group (C10 CRL) is forgotten automatically."""
    out = []
    helpers = set(session.scalars(select(GroupAssistItem.helper).where(GroupAssistItem.superseded_at.is_(None))
                                  .distinct()))
    for helper in sorted(helpers):
        if helper in tr.banned or tr.status_of(helper) in ("revoked", "left"):
            forget(session, ctx, helper, reason="removed")
            out.append(helper)
    return out


# ---- asker: media bytes from the group (§3.6) ------------------------------------------------------------------------

def _decode(data: Any) -> bytes:
    try:
        return base64.b64decode(str(data), validate=True)
    except (binascii.Error, ValueError):
        raise AssistError("hash_mismatch", "chunk is not base64") from None


def fetch_blob(session: Session, settings: Any, identity: Any, http: Any, art: Artwork,
               call: Callable[..., Any] | None = None) -> bytes | None:
    """Assemble one image from a member, chunk by chunk, checking the total size against `media_max_bytes` and the
    sha256 of the whole (the caller validates the image). Only rows with a digest are asked for (the bundle's
    `group_reported` one, or a peer/signed one)."""
    from .api import group_routes

    digest = art.peer_digest
    if not digest or not SHA_RE.match(digest):
        return None
    group, tr = _group_and_trust(session, identity)
    if group is None or not getattr(settings, "group_assist_ask", True):
        return None
    call = call or group_routes.call
    members = tr.members()
    preferred = (art.fetched_from or "")[len(GROUP_PREFIX):] if (art.fetched_from or "").startswith(GROUP_PREFIX) \
        else None
    order = sorted((n for n in members if n != identity.node_id and supports(session, n, "assist.blob")),
                   key=lambda n: (n != preferred, n))
    for node in order:
        if _backed_off(session, node):
            continue
        member = members[node]["body"]["member"]
        try:
            data = _assemble(session, settings, identity, group, member, digest, http, call)
        except AssistError as exc:
            if exc.code in REJECT_REASONS:
                suspect(session, node, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 - one member failing never stops the others
            _note_call_error(session, node, exc)
            continue
        if data is not None:
            art.fetched_from = GROUP_PREFIX + node
            from .instruments import group_assist_bytes

            group_assist_bytes.inc(len(data), role="asked")
            return data
    return None


def _assemble(session: Session, settings: Any, identity: Any, group: Any, member: dict, digest: str, http: Any,
              call: Callable[..., Any]) -> bytes | None:
    from .api import group_routes

    out = bytearray()
    offset = 0
    total = None
    while True:
        try:
            got = call(session, settings, identity, group, member, "assist.blob",
                       {"sha256": digest, "offset": offset, "length": BLOB_CHUNK}, http)
        except group_routes.CallError as exc:
            if str(exc) == "not_found":
                return None
            raise
        if not isinstance(got, dict) or got.get("sha256") != digest or got.get("offset") != offset:
            raise AssistError("hash_mismatch", "a chunk for another blob or offset")
        size = got.get("size")
        if type(size) is not int or size < 1 or (total is not None and size != total):
            raise AssistError("hash_mismatch", "inconsistent size")
        total = size
        if size > int(getattr(settings, "media_max_bytes", 10 * 1024 * 1024)):
            raise AssistError("too_large")
        chunk = _decode(got.get("data"))
        if not chunk and not got.get("eof"):
            raise AssistError("hash_mismatch", "empty chunk")
        out.extend(chunk)
        offset += len(chunk)
        if len(out) > size:
            raise AssistError("hash_mismatch", "more bytes than announced")
        if got.get("eof"):
            break
    if len(out) != total or hashlib.sha256(bytes(out)).hexdigest() != digest:
        raise AssistError("hash_mismatch")
    return bytes(out)


def blob_fetcher(ctx: Any, http: Any) -> Callable[[Session, Artwork], bytes | None]:
    """What the media worker passes to `media.cache_pending`: fetch one row's bytes from the group."""

    def fetch(session: Session, art: Artwork) -> bytes | None:
        return fetch_blob(session, ctx.settings, ctx.identity, http, art)

    return fetch


# ---- asker: which members can help ---------------------------------------------------------------------------------

def supports(session: Session, node: str, op: str) -> bool:
    """Whether a member advertised `op` in its last `hello` (the channel's cached answer, kept in node state)."""
    from . import group_channel

    if _state(session, NO_OPS + node) is not None:
        until = _parse(session.get(NodeState, NO_OPS + node).value)
        if until is not None and until > now():
            return False
    cached = group_channel._HELLO.get(node)
    ops = (cached[1].get("ops") if cached else None) or (_json(session, "assist_hello:" + node) or {}).get("ops")
    return isinstance(ops, list) and op in ops


def learn_ops(session: Session, settings: Any, identity: Any, group: Any, tr: Any, http: Any,
              call: Callable[..., Any]) -> None:
    """Ask `hello` of members whose ops are not known yet (cached for an hour, like the channel's)."""
    import time

    from . import group_channel

    for node, cert in tr.members().items():
        if node == identity.node_id:
            continue
        cached = group_channel._HELLO.get(node)
        if cached is not None and time.monotonic() - cached[0] <= group_channel.HELLO_TTL:
            _put(session, "assist_hello:" + node, {"ops": cached[1].get("ops") or []})
            continue
        try:
            hello = call(session, settings, identity, group, cert["body"]["member"], "hello", {}, http)
        except Exception:  # noqa: BLE001 - an unreachable member simply cannot help this round
            continue
        if isinstance(hello, dict):
            group_channel._HELLO[node] = (time.monotonic(), hello)
            _put(session, "assist_hello:" + node, {"ops": [o for o in hello.get("ops") or [] if isinstance(o, str)]})


def _backed_off(session: Session, node: str) -> bool:
    until = _parse((_state(session, HELPER_BACKOFF + node) or NodeState(value="")).value)
    return until is not None and until > now()


def _note_call_error(session: Session, node: str, exc: Exception) -> None:
    """`throttled` backs off that helper only, for `retry_after`; an op an older member does not know (`not_found`
    for the op itself) means "cannot help" for an hour, with no retry storm (T-A15)."""
    text = str(exc)
    if text == "throttled":
        retry = (getattr(exc, "result", None) or {}).get("retry_after") if isinstance(
            getattr(exc, "result", None), dict) else None
        seconds = retry if type(retry) is int and 0 < retry <= 86400 else 60
        _put(session, HELPER_BACKOFF + node, _iso(now() + timedelta(seconds=seconds)))
    elif text == "protocol":
        _put(session, NO_OPS + node, _iso(now() + timedelta(hours=1)))


def _order(session: Session, helpers: list[str], job: dict) -> list[str]:
    feeds = set(job.get("feeds") or [])

    def last_ok(node: str) -> str:
        row = _state(session, LAST_OK + node)
        return row.value if row is not None else ""

    return sorted(helpers, key=lambda n: (n not in feeds, n not in (job.get("positive") or []),
                                          "".join(chr(0x10FFFF - ord(c)) for c in last_ok(n)), n))


# ---- asker: the runner (§3.2) ----------------------------------------------------------------------------------------

def run(maker: Any, ctx: Any, http: Any, call: Callable[..., Any] | None = None) -> dict[str, Any]:
    """One assist round, at the end of every group round: backfill, then at most `group_assist_run_limit` due jobs.
    Calls happen first (no write lock held); every bundle is then applied in its own transaction."""
    from . import groups, service
    from .api import group_routes
    from .instruments import group_assist_jobs_total

    call = call or group_routes.call
    settings, identity = ctx.settings, ctx.identity
    summary: dict[str, Any] = {"asked": 0, "applied": 0, "none": 0, "hints": 0, "rejected": 0}
    with maker() as s:
        group = groups.active(s)
        if group is None:
            s.commit()
            return summary
        tr = groups.trust(s, group, own_node=identity.node_id)
        forgotten = forget_removed(s, ctx, tr)
        if forgotten:
            summary["forgotten"] = forgotten
        if not getattr(settings, "group_assist_ask", True):
            s.commit()
            return summary
        summary["backfill"] = backfill_step(s, settings, group.group_id)
        jobs = _due_jobs(s, int(getattr(settings, "group_assist_run_limit", 50)))
        if not jobs:
            s.commit()
            return summary
        learn_ops(s, settings, identity, group, tr, http, call)
        helpers = [n for n in tr.members() if n != identity.node_id and supports(s, n, "assist.find")
                   and not _backed_off(s, n)]
        answers: dict[str, tuple[str, dict]] = {}  # cid -> (helper, find item)
        pending = {j["cid"]: j for j in jobs}
        for job in jobs:
            job.setdefault("asked", [])
        # one find per helper carries up to 100 ids, helpers in the order of the first job they are best for
        order: list[str] = []
        for job in jobs:
            for node in _order(s, helpers, job):
                if node not in order:
                    order.append(node)
        for node in order:
            ids = [cid for cid in pending if cid not in answers]
            if not ids:
                break
            member = tr.members()[node]["body"]["member"]
            for start in range(0, len(ids), FIND_MAX):
                batch = ids[start:start + FIND_MAX]
                try:
                    got = call(s, settings, identity, group, member, "assist.find",
                               {"ids": batch, "want": ["record", "artwork"], "reason": "waiting_for_record",
                                "on_behalf": None}, http)
                except Exception as exc:  # noqa: BLE001
                    _note_call_error(s, node, exc)
                    group_assist_jobs_total.inc(result="error")
                    break
                summary["asked"] += len(batch)
                for cid in batch:
                    pending[cid]["asked"] = sorted(set(pending[cid]["asked"]) | {node})[:32]
                for it in (got or {}).get("items") or []:
                    if isinstance(it, dict) and it.get("id") in pending and it.get("state") in ("live", "merged"):
                        answers.setdefault(it["id"], (node, it))
        fetched: list[tuple[dict, str, str, dict]] = []  # job, helper, nonce, result
        for cid, (node, it) in answers.items():
            job = pending[cid]
            if it["state"] == "merged":
                survivor = it.get("survivor")
                if isinstance(survivor, str) and ID_RE.match(survivor) and survivor != cid:
                    s.add(GroupAssistItem(id=ulid(), helper=node, cid=cid, kind="merge_hint",
                                          body={"survivor": survivor, "helper": node}, received_at=now()))
                    if s.get(Canonical, survivor) is None:
                        note_missing(s, settings, survivor, "merge_hint")
                    summary["hints"] += 1
                _close(s, job, "merge_hint", helper=node, survivor=survivor)
                continue
            member = tr.members()[node]["body"]["member"]
            nonce = secrets.token_bytes(16)
            try:
                result = call(s, settings, identity, group, member, "assist.fetch",
                              {"id": cid, "want": ["record", "artwork"], "signed_entries": True,
                               "on_behalf": None}, http, nonce=nonce)
            except Exception as exc:  # noqa: BLE001
                _note_call_error(s, node, exc)
                continue
            fetched.append((job, node, gc.b64(nonce), result if isinstance(result, dict) else {}))
        for cid, job in pending.items():
            if cid not in answers:
                _backoff(s, settings, job)
                summary["none"] += 1
        s.commit()
    for job, node, nonce, result in fetched:
        with maker() as s:
            service.write_lock(s)
            group = groups.active(s)
            if group is None:
                break
            tr = groups.trust(s, group, own_node=identity.node_id)
            try:
                body = verify_bundle(s, identity, group, tr, node, result.get("bundle"), job["cid"], nonce)
                entries = [e for e in (result.get("signed_entries") or []) if isinstance(e, dict)][:MAX_ENTRIES]
                out = apply_bundle(s, ctx, node, body, result["bundle"], entries)
            except AssistError as exc:
                s.rollback()
                with maker() as s2:
                    suspect(s2, node, exc.code)
                    s2.commit()
                summary["rejected"] += 1
                continue
            if out["outcome"] == "applied":
                out["wakes"] = settle_wakes(s, ctx, node, job["cid"], result["bundle"], body, entries)
                _put(s, LAST_OK + node, _iso(now()))
                summary["applied"] += 1
                group_assist_jobs_total.inc(result="applied")
            _close(s, job, out["outcome"], helper=node, wakes=out.get("wakes") or {})
            s.commit()
    return summary


# ---- what the waiting list shows (§3.2, §3.5) -------------------------------------------------------------------------

def waiting_line(session: Session, cid: str, feed_id: str, group_id: str | None) -> dict | None:
    """The line under one waiting group on Admin → Peers: asked, nobody has it, a merge hint, or an offer."""
    job, done = job_of(session, cid), done_of(session, cid)
    hint = session.scalars(select(GroupAssistItem).where(GroupAssistItem.cid == cid,
                                                         GroupAssistItem.kind == "merge_hint",
                                                         GroupAssistItem.superseded_at.is_(None))
                           .order_by(GroupAssistItem.received_at.desc()).limit(1)).first()
    offers = [o for o in session.scalars(select(GroupAssistItem).where(
        GroupAssistItem.cid == cid, GroupAssistItem.kind == "offer", GroupAssistItem.superseded_at.is_(None)))
        if (o.body or {}).get("feed") == feed_id]

    def name(n: str) -> str:
        return member_name(session, group_id, n) if group_id else n

    if offers:
        o = offers[0]
        return {"state": "offer", "helper": name(o.helper), "item": o.id, "at": o.received_at}
    if hint is not None:
        survivor = (hint.body or {}).get("survivor")
        target = session.get(Canonical, survivor) if isinstance(survivor, str) else None
        title = (target.view or {}).get("title") if target is not None else None
        return {"state": "merge_hint", "helper": name(hint.helper), "survivor": survivor, "title": title,
                "at": hint.received_at}
    held = session.get(Canonical, cid)
    if held is not None and held.scope_ceiling_by == "assist":
        helper = session.scalars(select(GroupAssistItem.helper).where(
            GroupAssistItem.cid == cid, GroupAssistItem.kind == "bundle")
            .order_by(GroupAssistItem.received_at.desc()).limit(1)).first()
        return {"state": "not_from_feed", "helper": name(helper) if helper else None}
    if done is not None:
        return {"state": done.get("outcome"), "at": _parse(done.get("at"))}
    if job is not None:
        return {"state": "asked" if job.get("tries") else "queued", "at": _parse(job.get("last_asked"))}
    return None


def counts(session: Session) -> dict[str, int]:
    """Job counts for `atlas group assist status` and the metrics gauge."""
    t = now()
    due = backoff = 0
    for value in session.scalars(select(NodeState.value).where(NodeState.key.like(JOB + "%"))):
        try:
            job = json.loads(value)
        except ValueError:
            continue
        if (_parse(job.get("next_at")) or t) <= t:
            due += 1
        else:
            backoff += 1
    given_up = 0
    for value in session.scalars(select(NodeState.value).where(NodeState.key.like(DONE + "%"))):
        if '"none_has_it"' in value:
            given_up += 1
    dropped = _state(session, DROPPED)
    return {"due": due, "backoff": backoff, "given_up": given_up,
            "dropped": int(dropped.value) if dropped is not None and dropped.value.isdigit() else 0,
            "items": session.scalar(select(func.count()).select_from(GroupAssistItem).where(
                GroupAssistItem.superseded_at.is_(None))) or 0}


def purge(session: Session, days: int = 90) -> int:
    """D14.9: evidence is kept 90 days after it was superseded."""
    cutoff = now() - timedelta(days=days)
    return session.execute(delete(GroupAssistItem).where(GroupAssistItem.superseded_at.is_not(None),
                                                         GroupAssistItem.superseded_at < cutoff)).rowcount or 0
