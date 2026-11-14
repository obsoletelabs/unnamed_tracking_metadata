"""Entity resolution: which canonical record does an incoming claim belong to?

Never merges on weak evidence. Weak/ambiguous matches are reported, not assigned.

Both entry points are thin wrappers over the one matcher, `atlas.matching` (CONTRACT-C11 §11.2, D11.2), so ingest
and `/resolve` can never disagree about the same evidence:

* `find` is the ingest-time view (kept for callers that want the pre-C11 `Match` shape);
* `classify` answers `POST /api/v2/resolve` (contract C4): it is read-only and reports conflicting identity
  evidence instead of picking a side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import matching
from .matching import ATLAS_NAMESPACE, FUZZY_CAP, MAX_REDIRECT_HOPS, Facts, follow  # noqa: F401  (re-exported)
from .models import Crosswalk, ReviewItem
from .normalize import norm_title
from .settings import Settings


@dataclass
class Match:
    canonical_id: str | None = None
    method: str = "none"  # crosswalk | exact | fuzzy | forced | none
    confidence: float = 0.0
    candidates: list[tuple[str, float]] = field(default_factory=list)
    conflict: list[str] = field(default_factory=list)  # distinct canonicals claimed by different ids

    @property
    def assigned(self) -> bool:
        return self.canonical_id is not None


def lookup_keys(session: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for ns, ext in keys:
        row = session.get(Crosswalk, (ns, ext))
        if row:
            out[(ns, ext)] = follow(session, row.canonical_id)
    return out


_METHOD = {"id": "crosswalk", "redirect": "crosswalk", "crosswalk": "crosswalk", "conflict": "crosswalk",
           "exact": "exact", "fuzzy": "fuzzy"}


def find(session: Session, settings: Settings, *, namespace: str, external_id: str,
         provider_ids: dict[str, str] | None, title: str | None, release_date: str | None,
         platform: str | None, developer: str | None = None, exclude: str | None = None,
         media_type: str | None = None) -> Match:
    """Ingest-time matching. On an id conflict it prefers the source's own id (ingest then opens a review)."""
    facts = Facts(namespace=namespace, external_id=external_id, provider_ids=dict(provider_ids or {}), title=title,
                  release_date=release_date, platform=platform, developer=developer, media_type=media_type)
    return as_match(matching.match(session, settings, facts, exclude=exclude))


def as_match(d: matching.Decision) -> Match:
    method = _METHOD.get(d.reason, "none") if d.canonical_id else "none"
    return Match(d.canonical_id, method, round(d.score, 3), d.pairs(), conflict=list(d.conflict))


# ---- /resolve (contract C4) ----------------------------------------------------------------------------

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


def classify(session: Session, settings: Settings, *, namespace: str, external_id: str,
             provider_ids: dict[str, str] | None, title: str | None, release_date: str | None,
             platform: str | None, developer: str | None = None, media_type: str | None = None) -> Resolution:
    """Read-only resolution for clients (C4.1, C4.2), through the same matcher as ingest.

    - Identity evidence (Atlas ids, followed through merges, and provider ids through the crosswalk) never depends
      on the title.
    - Identity evidence that points at more than one record, or that contradicts the one record it names, is a
      `conflict`: no canonical id is returned. This is decided across media types, before `media_type` is
      applied; a single record of another type is `type_mismatch`.
    - Title evidence alone assigns only when it is exact or a capped fuzzy match, clearly ahead, not a placeholder
      title, not contradicted by an id the client holds in the same namespace, and the client sent no Atlas id
      that fails to resolve to a live record here.

    The matcher's finer reasons (`contradicted`, `unresolved_atlas`, `placeholder`) are reported with their
    resolve-contract-v1 values (`ambiguous`, `none`) until the v2 fixture ships (amendment A17 #6)."""
    facts = Facts(namespace=namespace, external_id=external_id, provider_ids=dict(provider_ids or {}), title=title,
                  release_date=release_date, platform=platform, developer=developer, media_type=media_type)
    d = matching.match(session, settings, facts)
    reason = matching.RESOLVE_REASON.get(d.reason, d.reason)
    if d.reason == "type_mismatch":
        return Resolution("type_mismatch", matched_keys=d.matched_keys)
    if d.reason in ("crosswalk", "id", "redirect"):
        live = d.canonical_id
        keys = [k for k in d.matched_keys if k[2] == live]
        return Resolution(d.reason, live, 1.0, [(live, 1.0)], keys, redirected_from=d.redirected_from)  # type: ignore[list-item]
    if d.reason == "conflict":
        named = sorted({cid for _ns, _ext, cid in d.matched_keys})
        typed = [c for c in named if c in set(d.conflict)] or named
        keys = [k for k in d.matched_keys if k[2] in typed]
        own = d.own if d.own in typed else None
        return Resolution("conflict", None, 0.9 if own else 0.0, [(c, 1.0) for c in typed], keys,
                          conflict=typed, own=own)
    out = d.pairs()
    if d.canonical_id:
        return Resolution(reason, d.canonical_id, round(d.score, 3), out)
    return Resolution(reason, None, round(d.score, 3) if out else 0.0, out)


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
