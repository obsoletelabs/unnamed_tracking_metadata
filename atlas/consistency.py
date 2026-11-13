"""`atlas check`: read-only consistency checks for operators (issue #19).

Every check is a bounded query that changes nothing. Each reports a count and up to `sample` example ids, so the
output stays small on a large catalogue. A non-zero count is a finding to look at, not proof of corruption: the
`hint` says which existing command repairs it (or that it needs a curator).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from . import media
from .models import Achievement, Artwork, Canonical, Crosswalk, CustomValue, Relation, SyncFailure
from .settings import Settings

#: Upper bound on merge-chain hops followed per record (the resolver gives up earlier; this only reports).
MAX_HOPS = 64


def _ids(session: Session, query, sample: int) -> tuple[int, list[str]]:
    count = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.execute(query.limit(sample)).all() if count else []
    return count, [":".join(str(v) for v in row) for row in rows]


def _crosswalk_to_merged(session: Session, sample: int):
    q = select(Crosswalk.namespace, Crosswalk.external_id).join(Canonical, Canonical.id == Crosswalk.canonical_id) \
        .where(Canonical.merged_into.is_not(None)).order_by(Crosswalk.namespace, Crosswalk.external_id)
    return _ids(session, q, sample), "a merge normally moves crosswalk rows to the survivor; resolve still follows " \
                                     "the redirect, so ids keep working. Re-run the merge or ask a curator."


def _crosswalk_dangling(session: Session, sample: int):
    q = select(Crosswalk.namespace, Crosswalk.external_id).outerjoin(Canonical, Canonical.id == Crosswalk.canonical_id) \
        .where(Canonical.id.is_(None)).order_by(Crosswalk.namespace, Crosswalk.external_id)
    return _ids(session, q, sample), "the record is gone: restore it from a backup or remove the crosswalk row"


def _merge_target_missing(session: Session, sample: int):
    target = aliased(Canonical)
    q = select(Canonical.id).outerjoin(target, target.id == Canonical.merged_into) \
        .where(Canonical.merged_into.is_not(None), target.id.is_(None)).order_by(Canonical.id)
    return _ids(session, q, sample), "a redirect points nowhere; resolve answers `none` for it. Restore from backup."


def _merge_cycles(session: Session, sample: int):
    edges = dict(session.execute(select(Canonical.id, Canonical.merged_into)
                                 .where(Canonical.merged_into.is_not(None))).all())
    bad = []
    for start in sorted(edges):
        seen, cur = {start}, edges[start]
        for _ in range(MAX_HOPS):
            if cur is None or cur not in edges:
                break
            if cur in seen:
                bad.append(start)
                break
            seen.add(cur)
            cur = edges[cur]
        else:
            bad.append(start)
    return (len(bad), bad[:sample]), "a merge chain loops or is longer than 64 hops; unmerge one link (curator)"


def _values_on_merged(session: Session, sample: int):
    q = select(CustomValue.canonical_id, CustomValue.key).join(Canonical, Canonical.id == CustomValue.canonical_id) \
        .where(Canonical.merged_into.is_not(None)).order_by(CustomValue.canonical_id, CustomValue.key)
    return _ids(session, q, sample), "custom values left on a merged record are invisible; a merge moves them"


def _relations_on_merged(session: Session, sample: int):
    a, b = aliased(Canonical), aliased(Canonical)
    q = select(Relation.from_id, Relation.to_id, Relation.type).join(a, a.id == Relation.from_id) \
        .join(b, b.id == Relation.to_id) \
        .where(Relation.ended_at.is_(None), or_(a.merged_into.is_not(None), b.merged_into.is_not(None))) \
        .order_by(Relation.from_id, Relation.to_id)
    return _ids(session, q, sample), "an active relation names a merged record; a merge normally moves or ends it"


def _artwork_blob_missing(session: Session, settings: Settings, sample: int):
    missing, total = [], 0
    for aid, digest in session.execute(select(Artwork.id, Artwork.blob_hash).where(Artwork.blob_hash.is_not(None))
                                       .order_by(Artwork.id)):
        if media.find_blob(settings, digest) is None:
            total += 1
            if len(missing) < sample:
                missing.append(aid)
    return (total, missing), "cached bytes are gone from ATLAS_MEDIA_DIR: `atlas media` or the admin page's " \
                             "*Retry* re-downloads them; restore the media folder from a backup otherwise"


def _orphan_blobs(session: Session, settings: Settings, sample: int):
    root = Path(settings.media_dir)
    if not root.is_dir():
        return (0, []), ""
    used = set(session.scalars(select(Artwork.blob_hash).where(Artwork.blob_hash.is_not(None))))
    used |= set(session.scalars(select(Achievement.icon_blob_hash).where(Achievement.icon_blob_hash.is_not(None))))
    used |= set(session.scalars(select(Achievement.locked_icon_blob_hash)
                                .where(Achievement.locked_icon_blob_hash.is_not(None))))
    if getattr(settings, "logo_hash", None):
        used.add(settings.logo_hash)
    found = []
    for path in sorted(root.glob("??/*.*")):
        digest = path.stem
        if len(digest) == 64 and digest not in used:
            found.append(path.name)
    return (len(found), found[:sample]), ("files no row references. Some are uploads waiting in a proposal, so "
                                          "nothing deletes them automatically; review before removing by hand")


def _parked_or_quarantined(session: Session, sample: int):
    q = select(SyncFailure.node_id, SyncFailure.seq, SyncFailure.reason) \
        .where(SyncFailure.resolved_at.is_(None)).order_by(SyncFailure.node_id, SyncFailure.seq)
    return _ids(session, q, sample), "`atlas sync status` explains them; `atlas sync replay` re-applies"


def _achievements_on_merged(session: Session, sample: int):
    q = select(Achievement.canonical_id, Achievement.id).join(Canonical, Canonical.id == Achievement.canonical_id) \
        .where(and_(Canonical.merged_into.is_not(None))).order_by(Achievement.canonical_id, Achievement.id)
    return _ids(session, q, sample), "achievements left on a merged record are invisible; a merge moves them"


def _upgrade_unfinished(session: Session, sample: int):
    """C16 §6.3 item 6: an upgrade journal still unfinished after 24 hours."""
    from . import upgrade

    state = upgrade.status(session) if upgrade.overdue(session) else None
    found = (1, [f"{state['stage']}: {state['last_error'] or 'running'}"]) if state else (0, [])
    return found, "the upgrade to wire 1 has not finished; see `atlas status` (Admin) for the stage and its last error"


CHECKS: dict[str, Callable[..., Any]] = {
    "crosswalk_to_merged_record": _crosswalk_to_merged,
    "crosswalk_to_missing_record": _crosswalk_dangling,
    "merge_target_missing": _merge_target_missing,
    "merge_chain_cycle_or_too_long": _merge_cycles,
    "custom_values_on_merged_record": _values_on_merged,
    "achievements_on_merged_record": _achievements_on_merged,
    "active_relation_to_merged_record": _relations_on_merged,
    "artwork_cached_file_missing": _artwork_blob_missing,
    "media_file_unreferenced": _orphan_blobs,
    "sync_entries_parked_or_quarantined": _parked_or_quarantined,
    "upgrade_unfinished": _upgrade_unfinished,
}
#: Findings that are expected in normal operation and do not fail `atlas check`.
INFORMATIONAL = frozenset({"media_file_unreferenced", "sync_entries_parked_or_quarantined"})
_NEEDS_SETTINGS = frozenset({"artwork_cached_file_missing", "media_file_unreferenced"})


def run(session: Session, settings: Settings, *, sample: int = 10, only: list[str] | None = None) -> dict[str, Any]:
    """Run the checks (read-only). `ok` is false when a non-informational check found something."""
    results: dict[str, Any] = {}
    for name, fn in CHECKS.items():
        if only and name not in only:
            continue
        (count, examples), hint = fn(session, settings, sample) if name in _NEEDS_SETTINGS else fn(session, sample)
        results[name] = {"count": count, "examples": examples, **({"hint": hint} if count else {})}
    ok = all(r["count"] == 0 for n, r in results.items() if n not in INFORMATIONAL)
    return {"ok": ok, "checks": results}
