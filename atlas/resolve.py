"""Entity resolution: which canonical record does an incoming claim belong to?

Never merges on weak evidence. Weak/ambiguous matches are reported, not assigned.

`find` is the ingest-time matcher. `classify` answers `POST /api/v2/resolve` (contract C4): it is read-only and
reports conflicting identity evidence instead of picking a side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Canonical, Crosswalk, ReviewItem
from .normalize import digit_tokens, norm_title, year_of
from .settings import Settings

FUZZY_CAP = 0.8  # fuzzy-only evidence never reaches the default auto-match threshold
MAX_REDIRECT_HOPS = 20


@dataclass
class Match:
    canonical_id: str | None = None
    method: str = "none"  # crosswalk | exact | fuzzy | none
    confidence: float = 0.0
    candidates: list[tuple[str, float]] = field(default_factory=list)
    conflict: list[str] = field(default_factory=list)  # distinct canonicals claimed by different ids

    @property
    def assigned(self) -> bool:
        return self.canonical_id is not None


def follow(session: Session, cid: str) -> str:
    """Resolve merge redirects (bounded)."""
    for _ in range(MAX_REDIRECT_HOPS):
        row = session.get(Canonical, cid)
        if row is None or row.merged_into is None:
            return cid
        cid = row.merged_into
    return cid


def lookup_keys(session: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for ns, ext in keys:
        row = session.get(Crosswalk, (ns, ext))
        if row:
            out[(ns, ext)] = follow(session, row.canonical_id)
    return out


def _score(settings: Settings, *, title_equal: bool, ratio: float, cand: Canonical, year: int | None,
           platform: str | None, developer: str | None) -> float:
    if title_equal:
        score = 0.7
    else:
        score = min(ratio * 0.7, FUZZY_CAP)
    cy = cand.year
    if year is not None and cy is not None:
        gap = abs(year - cy)
        score += 0.25 if gap == 0 else 0.1 if gap == 1 else -0.5
    view = cand.view or {}
    if platform and platform.casefold() in {p.casefold() for p in view.get("platforms", [])}:
        score += 0.03
    if developer and view.get("developer") and developer.casefold() == view["developer"].casefold():
        score += 0.1
    if not title_equal:
        score = min(score, FUZZY_CAP)
    return max(0.0, min(score, 0.99))


def _typed(session: Session, cids: list[str], media_type: str | None) -> list[str]:
    if not media_type:
        return cids
    return [cid for cid in cids if (row := session.get(Canonical, cid)) is not None and row.media_type == media_type]


def _title_candidates(session: Session, settings: Settings, *, title: str | None, release_date: str | None,
                      platform: str | None, developer: str | None, exclude: str | None,
                      media_type: str | None) -> list[tuple[str, float, bool]]:
    """Title evidence, best first: (canonical id, score, normalized titles equal), scores >= review_threshold."""
    norm = norm_title(title)
    if not norm:
        return []
    year = year_of(release_date)
    first = norm.split()[0]
    stmt = select(Canonical).where(
        Canonical.merged_into.is_(None),
        *([Canonical.media_type == media_type] if media_type else []),
        (Canonical.norm_title == norm) | Canonical.norm_title.like(f"{first}%"),
    ).limit(300)
    cands: list[tuple[str, float, bool]] = []
    for cand in session.scalars(stmt):
        if exclude and cand.id == exclude:
            continue
        equal = cand.norm_title == norm
        if equal:
            ratio = 1.0
        else:
            if digit_tokens(cand.norm_title) != digit_tokens(norm):
                continue
            ratio = SequenceMatcher(None, cand.norm_title, norm).ratio()
            if ratio < 0.9:
                continue
        s = _score(settings, title_equal=equal, ratio=ratio, cand=cand, year=year, platform=platform,
                   developer=developer)
        if s >= settings.review_threshold:
            cands.append((cand.id, s, equal))
    cands.sort(key=lambda c: c[1], reverse=True)
    return cands


def _assignable(settings: Settings, cands: list[tuple[str, float, bool]]) -> bool:
    """Strong, unambiguous title evidence: at or above the auto threshold, 0.1 clear of the runner-up."""
    top = cands[0][1]
    runner = cands[1][1] if len(cands) > 1 else 0.0
    return top >= settings.auto_match_threshold and top - runner >= 0.1


def find(session: Session, settings: Settings, *, namespace: str, external_id: str,
         provider_ids: dict[str, str] | None, title: str | None, release_date: str | None,
         platform: str | None, developer: str | None = None, exclude: str | None = None,
         media_type: str | None = None) -> Match:
    """Ingest-time matching. On an id conflict it prefers the source's own id (ingest then opens a review)."""
    # 1. identity keys
    keys = [(namespace, external_id)] + [(k, v) for k, v in (provider_ids or {}).items()]
    hits = lookup_keys(session, keys)
    distinct = sorted(set(hits.values()))
    if media_type:
        typed = _typed(session, distinct, media_type)
        if distinct and not typed:
            # A provider identity already assigned to another media type is a hard mismatch, not title evidence.
            return Match(None, "none", 0.0, [], conflict=distinct)
        distinct = typed
    if len(distinct) == 1:
        return Match(distinct[0], "crosswalk", 1.0, [(distinct[0], 1.0)])
    if len(distinct) > 1:
        # prefer the source's own identity; report the rest as a conflict
        own = hits.get((namespace, external_id))
        return Match(own, "crosswalk", 0.9 if own else 0.0, [(c, 1.0) for c in distinct], conflict=distinct)

    # 2. title evidence
    cands = _title_candidates(session, settings, title=title, release_date=release_date, platform=platform,
                              developer=developer, exclude=exclude, media_type=media_type)
    if not cands:
        return Match()
    out = [(c, round(s, 3)) for c, s, _ in cands]
    top_id, top, equal = cands[0]
    # assign only on strong, unambiguous evidence
    if _assignable(settings, cands):
        return Match(top_id, "exact" if equal else "fuzzy", round(top, 3), out)
    return Match(None, "none", round(top, 3), out)


# ---- /resolve (contract C4) ----------------------------------------------------------------------------

ATLAS_NAMESPACE = "atlas"
# Titles a client invents when it knows only a store id ("Steam app 12345", "GOG game 1207658924"). They are
# never title evidence: such a record resolves by its provider id or not at all. Matched on the normalized title.
_PLACEHOLDER = re.compile(
    r"^(?:steam|gog|epic|egs|epic games|itch|itch io|xbox|psn|playstation|nintendo|eshop|origin|ea|ea app|uplay|"
    r"ubisoft|battle net|battlenet|amazon|humble|lutris|heroic|rom|unknown) "
    r"(?:app|application|game|title|product|item)(?: id)? \d+$"
)
# reason -> tier (C4.1 table). Clients may auto-assign only SAFE_REASONS (C4.3).
TIERS = {"id": "id", "redirect": "id", "crosswalk": "id", "conflict": "id", "type_mismatch": "id",
         "exact": "strong", "fuzzy": "strong", "ambiguous": "weak", "none": "none"}
REASONS = tuple(TIERS)
SAFE_REASONS = frozenset({"id", "redirect", "crosswalk", "exact"})
_COMPAT_METHOD = {"id": "crosswalk", "redirect": "crosswalk", "crosswalk": "crosswalk", "exact": "exact",
                  "fuzzy": "fuzzy"}


def is_placeholder_title(title: str | None) -> bool:
    return bool(_PLACEHOLDER.match(norm_title(title)))


@dataclass
class Resolution:
    """One `/resolve` answer (C4.1). `canonical_id` is set only when the evidence allows assigning it."""

    reason: str = "none"
    canonical_id: str | None = None
    confidence: float = 0.0
    candidates: list[tuple[str, float]] = field(default_factory=list)
    matched_keys: list[tuple[str, str, str]] = field(default_factory=list)  # (namespace, external_id, live id)
    conflict: list[str] = field(default_factory=list)
    own: str | None = None
    redirected_from: str | None = None

    @property
    def tier(self) -> str:
        return TIERS[self.reason]

    @property
    def method(self) -> str:
        """The pre-C4 `method`, kept for old clients: `none` whenever nothing may be assigned (H2)."""
        return _COMPAT_METHOD.get(self.reason, "none") if self.canonical_id else "none"

    @property
    def assigned(self) -> bool:
        return self.canonical_id is not None


def _atlas_ids(namespace: str, external_id: str, provider_ids: dict[str, str] | None) -> list[str]:
    raw = [external_id] if namespace == ATLAS_NAMESPACE and external_id else []
    if provider_ids and provider_ids.get(ATLAS_NAMESPACE):
        raw.append(provider_ids[ATLAS_NAMESPACE])
    out: list[str] = []
    for value in raw:
        value = value.strip()
        cid = value if value.startswith("atlas:") else f"atlas:{value}"
        if cid not in out:
            out.append(cid)
    return out


def _is_live(session: Session, cid: str) -> bool:
    row = session.get(Canonical, cid)
    return row is not None and row.merged_into is None


def _contradicted(session: Session, cid: str, keys: list[tuple[str, str]]) -> bool:
    """True when the client holds an id in a namespace where the title candidate already has a different id.
    The client's key is not in the crosswalk (or identity evidence would have answered), so equal titles are then
    evidence of two games, not one."""
    namespaces = {ns for ns, ext in keys if ns and ext}
    if not namespaces:
        return False
    held = session.scalars(select(Crosswalk.namespace).where(
        Crosswalk.canonical_id == cid, Crosswalk.namespace.in_(sorted(namespaces))).limit(1)).first()
    return held is not None


def classify(session: Session, settings: Settings, *, namespace: str, external_id: str,
             provider_ids: dict[str, str] | None, title: str | None, release_date: str | None,
             platform: str | None, developer: str | None = None, media_type: str | None = None) -> Resolution:
    """Read-only resolution for clients (C4.1, C4.2).

    - Identity evidence (Atlas ids, followed through merges, and provider ids through the crosswalk) never depends
      on the title.
    - Identity evidence that points at more than one record is a `conflict`: no canonical id is returned.
    - Title evidence alone assigns only when it is exact or a capped fuzzy match, clearly ahead, not a placeholder
      title, not contradicted by an id the client holds in the same namespace, and the client sent no Atlas id
      that fails to resolve to a live record here.
    """
    matched: dict[tuple[str, str], str] = {}
    requested_atlas: dict[str, str] = {}  # live id -> the Atlas id the client sent
    own_atlas: str | None = None  # live id of the item's own external_id when namespace is "atlas"
    # An Atlas id the client holds but this node cannot resolve to a live record (unknown here, or a merge chain
    # longer than MAX_REDIRECT_HOPS) is not evidence for any record, but it is evidence against adopting another
    # record on title alone: every record here has an Atlas id, and it is not the one the client holds.
    unresolved_atlas = False
    own_cids = set(_atlas_ids(namespace, external_id, None))  # the item's own external_id, as an Atlas id
    for cid in _atlas_ids(namespace, external_id, provider_ids):
        if session.get(Canonical, cid) is None:
            unresolved_atlas = True
            continue
        live = follow(session, cid)
        if not _is_live(session, live):
            unresolved_atlas = True
            continue
        matched[(ATLAS_NAMESPACE, cid)] = live
        requested_atlas.setdefault(live, cid)
        if cid in own_cids:
            own_atlas = live
    keys = [(namespace, external_id)] if namespace != ATLAS_NAMESPACE else []
    keys += [(k, v) for k, v in (provider_ids or {}).items() if k != ATLAS_NAMESPACE]
    keys = [k for k in dict.fromkeys(keys) if k[0] and k[1]]
    for key, live in lookup_keys(session, keys).items():
        if not _is_live(session, live):
            continue  # a crosswalk row behind an over-long merge chain names no live record
        matched[key] = live
    matched_keys = [(ns, ext, cid) for (ns, ext), cid in matched.items()]
    distinct = sorted(set(matched.values()))

    if distinct:
        typed = _typed(session, distinct, media_type)
        if not typed:
            # a known identity of another media type is a hard mismatch, never title evidence
            return Resolution("type_mismatch", matched_keys=matched_keys)
        matched_keys = [k for k in matched_keys if k[2] in typed]
        if len(typed) == 1:
            live = typed[0]
            asked = requested_atlas.get(live)
            if asked is None:
                return Resolution("crosswalk", live, 1.0, [(live, 1.0)], matched_keys)
            if asked != live:
                return Resolution("redirect", live, 1.0, [(live, 1.0)], matched_keys, redirected_from=asked)
            return Resolution("id", live, 1.0, [(live, 1.0)], matched_keys)
        if namespace == ATLAS_NAMESPACE:
            own = own_atlas
        else:
            own = matched.get((namespace, external_id))
        own = own if own in typed else None
        return Resolution("conflict", None, 0.9 if own else 0.0, [(c, 1.0) for c in typed], matched_keys,
                          conflict=typed, own=own)

    if is_placeholder_title(title):
        return Resolution()
    cands = _title_candidates(session, settings, title=title, release_date=release_date, platform=platform,
                              developer=developer, exclude=None, media_type=media_type)
    if not cands:
        return Resolution()
    out = [(c, round(s, 3)) for c, s, _ in cands]
    top_id, top, equal = cands[0]
    if _assignable(settings, cands) and not unresolved_atlas and not _contradicted(session, top_id, keys):
        return Resolution("exact" if equal else "fuzzy", top_id, round(top, 3), out)
    return Resolution("ambiguous", None, round(top, 3), out)


def conflict_review(session: Session, cids: list[str]) -> str | None:
    """The open `id_conflict` review covering these records, if ingest opened one (read-only lookup)."""
    if not cids:
        return None
    wanted = set(cids)
    rows = session.scalars(select(ReviewItem).where(
        ReviewItem.kind == "id_conflict", ReviewItem.status == "open", ReviewItem.canonical_id.in_(sorted(wanted)),
    ).order_by(ReviewItem.created_at, ReviewItem.id)).all()
    for row in rows:
        if wanted & {c for c, _ in (row.candidates or [])}:
            return row.id
    return None
