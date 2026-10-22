"""Entity resolution: which canonical record does an incoming claim belong to?

Never merges on weak evidence. Weak/ambiguous matches are reported, not assigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Canonical, Crosswalk
from .normalize import digit_tokens, norm_title, year_of
from .settings import Settings

FUZZY_CAP = 0.8  # fuzzy-only evidence never reaches the default auto-match threshold


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
    for _ in range(20):
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


def find(session: Session, settings: Settings, *, namespace: str, external_id: str,
         provider_ids: dict[str, str] | None, title: str | None, release_date: str | None,
         platform: str | None, developer: str | None = None, exclude: str | None = None,
         media_type: str | None = None) -> Match:
    # 1. identity keys
    keys = [(namespace, external_id)] + [(k, v) for k, v in (provider_ids or {}).items()]
    hits = lookup_keys(session, keys)
    distinct = sorted(set(hits.values()))
    if media_type:
        typed = [cid for cid in distinct if (row := session.get(Canonical, cid)) is not None
                 and row.media_type == media_type]
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
    norm = norm_title(title)
    if not norm:
        return Match()
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
    if not cands:
        return Match()
    cands.sort(key=lambda c: c[1], reverse=True)
    out = [(c, round(s, 3)) for c, s, _ in cands]
    top_id, top, equal = cands[0]
    runner = cands[1][1] if len(cands) > 1 else 0.0
    # assign only on strong, unambiguous evidence
    if top >= settings.auto_match_threshold and top - runner >= 0.1:
        return Match(top_id, "exact" if equal else "fuzzy", round(top, 3), out)
    return Match(None, "none", round(top, 3), out)
