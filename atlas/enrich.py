"""Enrich introduced records from this node's own providers (CONTRACT-C13 §5.1 step 4; owner decision H56).

**Off by default** (`ATLAS_SYNC_ENRICH_FROM_OWN_PROVIDERS=false`): H56 is an open owner decision, so nothing here
runs unless the operator turns it on. The contract recommends on.

When a follower applies a `record`, the record carries only an id, a title and store
ids. With enrichment on, the follower fetches the game itself from its *own* enabled providers, with its own keys and
quota, by those store ids: description, genres, art and achievements are built here, and nothing is redistributed
(the fetched data is this node's provider data, never signed into a feed, and its images are fetched from their
origin only, never asked of a peer: `media._from_peer` skips rows that did not arrive through a feed).

Limits (§5.1 step 4):
* **Only free keys.** A job is queued only when the store id is unclaimed here, or already points at the record. A
  store id another live record holds (a cross-origin duplicate under review, or a record kept apart) is skipped:
  `ingest(force_canonical=…)` would move that record's observation.
* **Valid ids only.** Each id must be one the provider accepts (the Fetcher's id rule, and digits for Steam) before
  any request; an invalid id is dropped and counted.
* **Rate-bounded.** At most `sync_enrich_per_peer_per_hour` fetches per upstream feed per hour; the rest stay queued
  for the next hour, counted with a compare-and-set write so concurrent workers cannot overrun it. Every fetch also
  goes through the provider's own budget and circuit breaker (`Fetcher`).

Only a `record` queues a job. A bridge observation (an `obs` signed for an old follower) is an ordinary observation
and is not enriched; the `record` of the same game, signed alongside it, is.

Jobs are node-state rows (`sync_enrich:<hash>`), so a restart keeps them and every worker sees one queue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import service
from .models import Canonical, Crosswalk, NodeState

log = logging.getLogger("atlas.enrich")

JOB = "sync_enrich:"
HOUR = "sync_enrich_hour:"
INVALID = "sync_enrich_invalid"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}\Z")
#: per-provider id rules on top of the Fetcher's (the adapter would refuse anything else)
_PROVIDER_ID = {"steam": re.compile(r"^[0-9]{1,12}\Z"), "igdb": re.compile(r"^[0-9]{1,12}\Z")}
RUN_LIMIT = 50


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "sync_enrich_from_own_providers", False))


_here: tuple[object, frozenset[str]] | None = None


def providers_here(settings: Any) -> frozenset[str]:
    """The built-in providers this node has enabled (its own keys). Built once per settings object, not once per
    applied `record`: a large first sync applies thousands of them."""
    global _here
    cached = _here
    if cached is not None and cached[0] is settings:
        return cached[1]
    from .providers.registry import build_registry

    names = frozenset(build_registry(settings))
    _here = (settings, names)
    return names


def valid_id(namespace: str, external_id: str) -> bool:
    rule = _PROVIDER_ID.get(namespace)
    if namespace == "openlibrary":  # a work, an edition or an ISBN (C15 §3.3); never an author id
        from .providers.openlibrary import OpenLibrary

        return bool(_SAFE_ID.match(external_id)) and OpenLibrary.valid_id(external_id)
    return bool(_SAFE_ID.match(external_id)) and (rule is None or bool(rule.match(external_id)))


def _key(namespace: str, external_id: str) -> str:
    return JOB + hashlib.sha256(f"{namespace}\n{external_id}".encode()).hexdigest()[:40]


def _free_for(session: Session, namespace: str, external_id: str, cid: str) -> bool:
    row = session.get(Crosswalk, (namespace, external_id))
    return row is None or service.follow(session, row.canonical_id) == service.follow(session, cid)


def queue(session: Session, settings: Any, node_id: str, cid: str, ids: dict[str, Any]) -> dict[str, int]:
    """Queue a fetch of `cid` from this node's providers for each store id it can resolve (called when a `record`
    from `node_id`'s feed applies). Returns what happened to each id."""
    out = {"queued": 0, "claimed": 0, "invalid": 0, "unsupported": 0}
    if not enabled(settings) or not ids:
        return out
    here = providers_here(settings)
    for namespace, external_id in sorted(ids.items())[:32]:
        external_id = str(external_id)
        if namespace not in here:
            out["unsupported"] += 1
            continue
        if not valid_id(namespace, external_id):
            out["invalid"] += 1
            log.info("record %s from %s: %s id %r is not one the provider accepts; not fetched", cid, node_id,
                     namespace, external_id[:80])
            continue
        if not _free_for(session, namespace, external_id, cid):
            out["claimed"] += 1  # another record holds this store id: fetching would move its observation
            continue
        key = _key(namespace, external_id)
        if session.get(NodeState, key) is None:
            session.add(NodeState(key=key, value=json.dumps(
                {"ns": namespace, "id": external_id, "cid": cid, "node": node_id,
                 "at": datetime.now(UTC).isoformat()}, sort_keys=True)))
            out["queued"] += 1
    if out["invalid"]:
        row = session.get(NodeState, INVALID)
        count = int(row.value) if row is not None and row.value.isdigit() else 0
        if row is None:
            session.add(NodeState(key=INVALID, value=str(out["invalid"])))
        else:
            row.value = str(count + out["invalid"])
    session.flush()
    return out


def pending(session: Session) -> list[tuple[str, dict[str, Any]]]:
    jobs = []
    for row in session.scalars(select(NodeState).where(NodeState.key.like(JOB + "%")).limit(5000)):
        try:
            job = json.loads(row.value)
        except ValueError:
            continue
        if isinstance(job, dict):
            jobs.append((row.key, job))
    return sorted(jobs, key=lambda j: (str(j[1].get("at")), j[0]))


def _take(session: Session, settings: Any, node_id: str, now: datetime) -> bool:
    """One fetch from `node_id`'s hourly allowance, or False when it is used up."""
    key, hour = HOUR + node_id, now.strftime("%Y%m%d%H")
    row = session.get(NodeState, key)
    stamp, _, used = (row.value if row is not None else "").partition(":")
    count = int(used) if stamp == hour and used.isdigit() else 0
    if count >= settings.sync_enrich_per_peer_per_hour:
        return False
    new = f"{hour}:{count + 1}"
    if row is None:
        try:
            with session.begin_nested():
                session.add(NodeState(key=key, value=new))
        except IntegrityError:  # another worker took the first fetch of this hour: retry on the next run
            return False
        return True
    # compare-and-set: only if no other worker counted a fetch since this one read the allowance
    done = session.execute(update(NodeState).where(NodeState.key == key, NodeState.value == row.value)
                           .values(value=new).execution_options(synchronize_session=False))
    session.expire(row)
    return done.rowcount == 1


def run(maker: sessionmaker[Session], st: Any, *, limit: int = RUN_LIMIT, now: datetime | None = None
        ) -> dict[str, int]:
    """Work through queued jobs, oldest first, each in its own transaction. A job whose record is gone, or whose
    store id another record took meanwhile, is dropped; one over its feed's hourly allowance stays queued; one whose
    provider failed stays queued (the provider's own circuit breaker spaces the retries)."""
    from . import importer
    from .providers import ProviderError

    out = {"fetched": 0, "dropped": 0, "deferred": 0, "failed": 0}
    settings = st.ctx.settings
    if not enabled(settings):
        return out
    now = now or datetime.now(UTC)
    with maker() as session:
        jobs = pending(session)[: limit * 4]
    done = 0
    for key, job in jobs:
        if done >= limit:
            break
        namespace, external_id, cid, node = (str(job.get(k) or "") for k in ("ns", "id", "cid", "node"))
        with maker() as session:
            row = session.get(NodeState, key)
            if row is None:
                continue
            record = session.get(Canonical, cid)
            if record is None or namespace not in st.providers.providers or not valid_id(namespace, external_id) \
                    or not _free_for(session, namespace, external_id, cid):
                session.delete(row)
                session.commit()
                out["dropped"] += 1
                continue
            if not _take(session, settings, node, now):
                session.commit()
                out["deferred"] += 1
                continue
            # the allowance taken stays taken; commit it now so the write lock is not held while the provider is
            # fetched (Open Library's pacer may wait several seconds before each request; review of #139)
            session.commit()
            done += 1
            try:
                with session.begin_nested():
                    importer.import_from_provider(st, session, namespace, external_id,
                                                  force_canonical=service.follow(session, cid))
            except ProviderError as exc:
                log.info("enrich %s from %s:%s failed: %s (kept queued)", cid, namespace, external_id, exc)
                out["failed"] += 1
                session.commit()  # the allowance taken stays taken
                continue
            except service.AtlasError as exc:
                log.info("enrich %s from %s:%s: %s (dropped)", cid, namespace, external_id, exc)
                out["dropped"] += 1
            else:
                out["fetched"] += 1
            session.delete(row)
            session.commit()
    return out
