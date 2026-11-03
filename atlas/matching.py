"""One matcher for ingest and `/resolve` (CONTRACT-C11 §11.2), its decision records (§11.4), the review queue
rules (§11.5) and rejection memory (§11.6).

Identity evidence (Atlas ids, crosswalk keys) never depends on the title. Title evidence is scored from signals
(`title_equal`, `title_fuzzy`, `alias_equal`, year, platform, developer, publisher) and vetoed when it contradicts
something stronger: a differing store id in the same namespace (V4), a placeholder title (V3), an unresolvable
Atlas id (V5), a remembered rejection (V6), too small a margin (V7). Every signal and veto that was considered is
returned as an evidence item, so a decision can always say why.

Everything here is local to this node: decisions, review items and rejections quote provider data and depend on
this node's records and settings; none of it is federated in this release.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .models import (
    AuditEntry,
    Canonical,
    Crosswalk,
    MatchDecision,
    MatchRejection,
    RecordLive,
    ReviewCandidate,
    ReviewItem,
    now,
)
from .normalize import digit_tokens, norm_namespace, norm_title, split_edition, year_of
from .settings import Settings

MATCHER = "m2"
FUZZY_CAP = 0.8  # fuzzy-only (and alias) evidence never reaches the default auto-match threshold
MAX_REDIRECT_HOPS = 20
MAX_TITLE_ROWS = 300  # §11.9.2: title candidate rows read per item
MAX_CANDIDATES = 5
MAX_EVIDENCE = 24
MAX_EVIDENCE_VALUE = 200  # characters of an evidence value as JSON
MAX_KEYS_PER_RECORD = 200  # crosswalk rows read per record for a consistency test
MARGIN = 0.1
ATLAS_NAMESPACE = "atlas"
#: Store namespaces whose ids name one platform's version of a work (§11.2.4, H51).
PLATFORM_SCOPED_NAMESPACES = frozenset({"psn", "xbox", "nintendo"})

#: reason -> tier for ingest decisions (§11.1). `forced`, `stale` are bookkeeping.
TIERS = {"id": "id", "redirect": "id", "crosswalk": "id", "conflict": "id", "type_mismatch": "id",
         "exact": "strong", "fuzzy": "strong", "ambiguous": "weak", "contradicted": "weak",
         "unresolved_atlas": "weak", "none": "none", "rejected": "none", "placeholder": "none",
         "forced": "id", "stale": "id"}
#: What `/resolve` (C4.1, `resolve-contract-v1`) reports for the reasons it does not know yet (amendment A17 #6).
RESOLVE_REASON = {"contradicted": "ambiguous", "unresolved_atlas": "ambiguous", "rejected": "ambiguous",
                  "placeholder": "none"}

#: §11.5 rule 5. `possible_duplicate` adds round(score x 10).
REVIEW_PRIORITY = {"id_conflict": 30, "bridge_deferred": 25, "title_contradicted": 20, "unmerge_check": 15,
                   "possible_duplicate": 10}
REVIEW_KINDS = tuple(REVIEW_PRIORITY)
#: Kinds a "merge" resolution does not apply to: an `unmerge_check` names the survivor and the record an unmerge just
#: split off it. Its resolutions in this release are `detach` and `dismiss` (move_chain / move_row are deferred).
UNMERGEABLE_KINDS = frozenset({"unmerge_check"})
REVIEW_STATUSES = ("open", "merged", "dismissed", "distinct", "superseded", "resolved")

_COMPANY_SUFFIX = frozenset({"inc", "ltd", "llc", "gmbh", "co", "corp", "corporation", "sa", "srl", "kk", "bv", "ab"})
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)


def norm_company(name: Any) -> str:
    """Developer/publisher comparison form: casefold, NFKD without marks, punctuation removed, legal-form words
    dropped ("Valve Corporation" == "valve")."""
    if not isinstance(name, str):
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    tokens = [t for t in _NONWORD.sub(" ", text).split() if t not in _COMPANY_SUFFIX]
    return " ".join(tokens)


# ---- the claim being matched -----------------------------------------------------------------------------

@dataclass
class Facts:
    """What the matcher knows about one incoming observation (or one `/resolve` item)."""

    namespace: str
    external_id: str
    provider_ids: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    release_date: str | None = None
    platform: str | None = None
    platforms: list[str] = field(default_factory=list)
    developer: str | None = None
    publisher: str | None = None
    aliases: list[str] = field(default_factory=list)
    edition: str | None = None
    media_type: str | None = None

    @classmethod
    def of_payload(cls, namespace: str, external_id: str, payload: dict[str, Any],
                   media_type: str | None) -> Facts:
        facts = payload.get("facts") or {}
        return cls(namespace=namespace, external_id=external_id,
                   provider_ids=dict(payload.get("provider_ids") or {}), title=payload.get("title"),
                   release_date=payload.get("release_date"), platform=payload.get("platform"),
                   platforms=list(payload.get("platforms") or []), developer=facts.get("developer"),
                   publisher=facts.get("publisher"), aliases=list(facts.get("aliases") or [])[:20],
                   edition=payload.get("edition"), media_type=media_type)

    def own_variant(self) -> str:
        return variant_of(self.namespace, self.title, self.edition, self.platform, self.platforms)

    def keys(self) -> list[tuple[str, str, str]]:
        """Identity keys with their variant: the own key first, then `provider_ids` (work keys), Atlas excluded."""
        out: list[tuple[str, str, str]] = []
        if self.namespace and self.external_id and self.namespace != ATLAS_NAMESPACE:
            out.append((self.namespace, self.external_id, self.own_variant()))
        for ns, ext in self.provider_ids.items():
            if ns and ext and ns != ATLAS_NAMESPACE and (ns, ext) not in {(k[0], k[1]) for k in out}:
                out.append((ns, ext, ""))
        return out


def variant_of(namespace: str, title: str | None, edition: str | None, platform: str | None,
               platforms: list[str] | None = None) -> str:
    """§11.2.4: the crosswalk variant of an observation's own key. An edition marker (`edition`, or an edition
    suffix `split_edition` strips from the title) makes it `edition:<name>`; a platform-scoped store namespace
    with exactly one stated platform makes it `platform:<slug>`; otherwise '' (a work key)."""
    name = " ".join(str(edition or "").split())
    if not name and title:
        name = split_edition(title)[1] or ""
    if name:
        return "edition:" + " ".join(name.casefold().split())[:100]
    if norm_namespace(namespace) in PLATFORM_SCOPED_NAMESPACES:
        stated = {p.strip().casefold() for p in ([platform] if platform else []) + list(platforms or []) if p}
        if len(stated) == 1:
            slug = re.sub(r"[^a-z0-9]+", "-", stated.pop()).strip("-")[:60]
            if slug:
                return "platform:" + slug
    return ""


# ---- results ---------------------------------------------------------------------------------------------

def ev(signal: str, value: Any = None, effect: float = 0.0, *, veto: bool = False) -> dict[str, Any]:
    """One evidence item (§11.4): `value` is cut to MAX_EVIDENCE_VALUE characters of JSON."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > MAX_EVIDENCE_VALUE:
        value = text[:MAX_EVIDENCE_VALUE]
    item: dict[str, Any] = {"signal": signal, "value": value, "effect": round(effect, 3)}
    if veto:
        item["veto"] = True
    return item


@dataclass
class Candidate:
    id: str
    score: float
    base: str  # title_equal | title_fuzzy | alias_equal
    evidence: list[dict[str, Any]] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)

    @property
    def signals(self) -> list[str]:
        return [e["signal"] for e in self.evidence if not e.get("veto") and e["signal"] not in ("edition_key",
                                                                                                "platform_key")]

    def out(self) -> dict[str, Any]:
        item: dict[str, Any] = {"id": self.id, "score": round(self.score, 3), "signals": self.signals}
        if self.vetoes:
            item["vetoes"] = list(self.vetoes)
        return item


@dataclass
class Decision:
    """The matcher's answer for one claim. `canonical_id` is set only when the evidence allows assigning it."""

    tier: str = "none"
    reason: str = "none"
    canonical_id: str | None = None
    score: float = 0.0
    margin: float | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    review: str | None = None  # review kind to open
    own: str | None = None  # record the observation's own key names (conflict fallback)
    conflict: list[str] = field(default_factory=list)  # live records named by identity evidence
    blocked: set[tuple[str, str]] = field(default_factory=set)  # contradicting keys never registered
    matched_keys: list[tuple[str, str, str]] = field(default_factory=list)
    redirected_from: str | None = None
    consistent: bool | None = None

    @property
    def assigned(self) -> bool:
        return self.canonical_id is not None

    def pairs(self) -> list[tuple[str, float]]:
        if self.conflict:
            return [(c, 1.0) for c in self.conflict]
        return [(c.id, round(c.score, 3)) for c in self.candidates]


def _decide(reason: str, **kw: Any) -> Decision:
    return Decision(tier=TIERS[reason], reason=reason, **kw)


# ---- live records and identity evidence ------------------------------------------------------------------

def follow(session: Session, cid: str) -> str:
    """Resolve merge redirects (bounded)."""
    for _ in range(MAX_REDIRECT_HOPS):
        row = session.get(Canonical, cid)
        if row is None or row.merged_into is None:
            return cid
        cid = row.merged_into
    return cid


def is_live(session: Session, cid: str) -> bool:
    row = session.get(Canonical, cid)
    return row is not None and row.merged_into is None


def _typed(session: Session, cids: list[str], media_type: str | None) -> list[str]:
    if not media_type:
        return cids
    return [c for c in cids if (row := session.get(Canonical, c)) is not None and row.media_type == media_type]


def atlas_ids(namespace: str, external_id: str, provider_ids: dict[str, str] | None) -> list[str]:
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


@dataclass
class Identity:
    matched: dict[tuple[str, str], str] = field(default_factory=dict)  # key -> live record
    requested_atlas: dict[str, str] = field(default_factory=dict)  # live id -> Atlas id the client sent
    own_atlas: str | None = None
    unresolved_atlas: bool = False


def identity(session: Session, facts: Facts) -> Identity:
    """§11.2.1 steps 1-2: Atlas ids (followed through merges) and crosswalk keys, as live records."""
    out = Identity()
    own_cids = set(atlas_ids(facts.namespace, facts.external_id, None))
    for cid in atlas_ids(facts.namespace, facts.external_id, facts.provider_ids):
        if session.get(Canonical, cid) is None:
            out.unresolved_atlas = True
            continue
        live = follow(session, cid)
        if not is_live(session, live):
            out.unresolved_atlas = True
            continue
        out.matched[(ATLAS_NAMESPACE, cid)] = live
        out.requested_atlas.setdefault(live, cid)
        if cid in own_cids:
            out.own_atlas = live
    for ns, ext, _variant in facts.keys():
        row = session.get(Crosswalk, (ns, ext))
        if row is None:
            continue
        live = follow(session, row.canonical_id)
        if is_live(session, live):  # a row behind an over-long merge chain names no live record
            out.matched[(ns, ext)] = live
    return out


def record_keys(session: Session, cid: str, namespaces: set[str] | None = None) -> dict[tuple[str, str], set[str]]:
    """A record's crosswalk keys as {(namespace, variant): {external ids}} (bounded)."""
    stmt = select(Crosswalk.namespace, Crosswalk.external_id, Crosswalk.variant).where(Crosswalk.canonical_id == cid)
    if namespaces is not None:
        if not namespaces:
            return {}
        stmt = stmt.where(Crosswalk.namespace.in_(sorted(namespaces)))
    out: dict[tuple[str, str], set[str]] = {}
    for ns, ext, variant in session.execute(stmt.order_by(Crosswalk.namespace, Crosswalk.external_id)
                                            .limit(MAX_KEYS_PER_RECORD)):
        out.setdefault((ns, variant or ""), set()).add(ext)
    return out


def contradictions(held: dict[tuple[str, str], set[str]], keys: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The claim's keys that contradict what a record holds: same namespace and variant, value not held (§11.2.4
    rule 1: work keys compare with work keys, variant keys only with the same variant)."""
    out = []
    for ns, ext, variant in keys:
        have = held.get((ns, variant))
        if have and ext not in have:
            out.append((ns, ext))
    return out


def consistent(parties: list[dict[tuple[str, str], set[str]]]) -> bool:
    """§11.3 "consistent": for every namespace/variant, every two parties holding values there share one."""
    for i, a in enumerate(parties):
        for b in parties[i + 1:]:
            for slot, values in a.items():
                other = b.get(slot)
                if other and values and not (values & other):
                    return False
    return True


def _facts_keys_map(keys: list[tuple[str, str, str]]) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for ns, ext, variant in keys:
        out.setdefault((ns, variant), set()).add(ext)
    return out


# ---- rejection memory (§11.6) ----------------------------------------------------------------------------

def chain_hash(source: str, scope: str, external_id: str) -> str:
    return "chain:" + hashlib.sha256(f"{source}\n{scope}\n{external_id}".encode()).hexdigest()[:32]


def alias_ids(session: Session, cid: str) -> set[str]:
    """Every id whose live record is `cid` (itself included), through `record_live`."""
    return {cid, *session.scalars(select(RecordLive.id).where(RecordLive.live_id == cid))}


def active_rejection(session: Session, x: str, y: str) -> MatchRejection | None:
    """An active `record` rejection between the live records `x` and `y` (any of their merged-in ids)."""
    if x == y:
        return None
    xs, ys = sorted(alias_ids(session, x)), sorted(alias_ids(session, y))
    return session.scalars(select(MatchRejection).where(
        MatchRejection.scope == "record", MatchRejection.ended_at.is_(None),
        or_((MatchRejection.a.in_(xs)) & (MatchRejection.b.in_(ys)),
            (MatchRejection.a.in_(ys)) & (MatchRejection.b.in_(xs)))).limit(1)).first()


def chain_rejected(session: Session, chain: str, x: str) -> bool:
    return session.scalars(select(MatchRejection.id).where(
        MatchRejection.scope == "chain", MatchRejection.ended_at.is_(None), MatchRejection.a == chain,
        MatchRejection.b.in_(sorted(alias_ids(session, x)))).limit(1)).first() is not None


def add_rejection(session: Session, scope: str, a: str, b: str, *, reason: str, actor: str, note: str = "",
                  local_only: bool = True) -> tuple[MatchRejection, bool]:
    """Remember a rejection; `(row, created)`. Adding one that is already active is a no-op (§11.6)."""
    if scope == "record":
        if a == b:
            raise ValueError("a record cannot be distinct from itself")
        a, b = sorted((a, b))
    existing = session.scalars(select(MatchRejection).where(
        MatchRejection.scope == scope, MatchRejection.a == a, MatchRejection.b == b,
        MatchRejection.ended_at.is_(None))).first()
    if existing is not None:
        return existing, False
    row = MatchRejection(scope=scope, a=a, b=b, reason=reason[:32], actor=actor[:128], note=note[:2000],
                         local_only=local_only)
    session.add(row)
    session.add(AuditEntry(actor_id=None, action="match_distinct_add", target=f"{a} {b}"[:128],
                           detail={"actor": actor[:128], "scope": scope, "reason": reason[:32]}))
    session.flush()
    return row, True


def end_rejection(session: Session, row: MatchRejection, *, reason: str, actor: str) -> None:
    if row.ended_at is not None:
        return
    row.ended_at, row.end_reason = now(), reason[:32]
    session.add(AuditEntry(actor_id=None, action="match_distinct_end", target=f"{row.a} {row.b}"[:128],
                           detail={"actor": actor[:128], "reason": reason[:32], "id": row.id}))
    session.flush()


def rejection_out(row: MatchRejection) -> dict[str, Any]:
    return {"id": row.id, "scope": row.scope, "a": row.a, "b": row.b, "reason": row.reason, "note": row.note,
            "actor": row.actor, "local_only": row.local_only, "created_at": _iso(row.created_at),
            "ended_at": _iso(row.ended_at), "end_reason": row.end_reason}


def _iso(dt: Any) -> str | None:
    from datetime import UTC

    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


# ---- title evidence (§11.2.2) ----------------------------------------------------------------------------

def _aliases_of(view: dict[str, Any]) -> set[str]:
    return {n for a in (view.get("aliases") or [])[:100] if isinstance(a, str) and (n := norm_title(a))}


def title_candidates(session: Session, settings: Settings, facts: Facts, *, exclude: str | None = None) -> list[Candidate]:
    """Scored title candidates, best first (ties by id), each with its evidence items; at most MAX_TITLE_ROWS rows
    are read and only scores >= review_threshold are kept. Vetoes are applied by the caller."""
    norm = norm_title(facts.title)
    if not norm:
        return []
    year = year_of(facts.release_date)
    first = norm.split()[0]
    obs_aliases = {n for a in facts.aliases if isinstance(a, str) and (n := norm_title(a))} - {norm}
    cond = (Canonical.norm_title == norm) | Canonical.norm_title.like(f"{first}%")
    if obs_aliases:
        cond = cond | Canonical.norm_title.in_(sorted(obs_aliases)[:20])
    stmt = select(Canonical).where(
        Canonical.merged_into.is_(None), Canonical.norm_title != "",
        *([Canonical.media_type == facts.media_type] if facts.media_type else []), cond,
    ).order_by(Canonical.id).limit(MAX_TITLE_ROWS)
    platforms = {p.casefold() for p in ([facts.platform] if facts.platform else []) + facts.platforms if p}
    dev, pub = norm_company(facts.developer), norm_company(facts.publisher)
    out: list[Candidate] = []
    for cand in session.scalars(stmt):
        if exclude and cand.id == exclude:
            continue
        view = cand.view or {}
        items: list[dict[str, Any]] = []
        if cand.norm_title == norm:
            base, score = "title_equal", 0.70
            items.append(ev("title_equal", norm, 0.70))
        elif norm in _aliases_of(view) or cand.norm_title in obs_aliases:
            base, score = "alias_equal", 0.65
            items.append(ev("alias_equal", cand.norm_title, 0.65))
        else:
            if digit_tokens(cand.norm_title) != digit_tokens(norm):
                continue  # V2 sequel guard: never a candidate
            if not 0.7 * len(norm) <= len(cand.norm_title) <= 1.3 * len(norm):
                continue
            matcher = SequenceMatcher(None, cand.norm_title, norm)
            if matcher.quick_ratio() < 0.9:
                continue
            ratio = matcher.ratio()
            if ratio < 0.9:
                continue
            base, score = "title_fuzzy", min(ratio * 0.7, FUZZY_CAP)
            items.append(ev("title_fuzzy", round(ratio, 3), score))
        cy = cand.year
        if year is not None and cy is not None:
            gap = abs(year - cy)
            name, effect = ("year_same", 0.25) if gap == 0 else ("year_adjacent", 0.10) if gap == 1 else ("year_far", -0.5)
            score += effect
            items.append(ev(name, [year, cy], effect))
        if platforms and platforms & {p.casefold() for p in view.get("platforms") or [] if isinstance(p, str)}:
            score += 0.03
            items.append(ev("platform_overlap", sorted(platforms)[:5], 0.03))
        cdev = norm_company(view.get("developer"))
        if dev and cdev:
            if dev == cdev:
                score += 0.10
                items.append(ev("developer_equal", dev, 0.10))
            else:
                score -= 0.15
                items.append(ev("developer_mismatch", [dev, cdev], -0.15))
        cpub = norm_company(view.get("publisher"))
        if pub and cpub and pub == cpub:
            score += 0.05
            items.append(ev("publisher_equal", pub, 0.05))
        if base != "title_equal":
            score = min(score, FUZZY_CAP)
        score = max(0.0, min(score, 0.99))
        if score >= settings.review_threshold:
            out.append(Candidate(cand.id, score, base, items))
    out.sort(key=lambda c: (-c.score, c.id))
    return out


def apply_vetoes(session: Session, settings: Settings, facts: Facts, cands: list[Candidate], *,
                 unresolved_atlas: bool, chain: str | None = None, chain_record: str | None = None) -> Decision:
    """V4-V7 over scored candidates, and the resulting title-tier decision (§11.2.3, lines after the identity
    part). V6 removes candidates; V4 and V5 keep them listed with their vetoes."""
    keys = facts.keys()
    namespaces = {ns for ns, _e, _v in keys}
    kept: list[Candidate] = []
    rejected_any = False
    for c in cands:
        if (chain_record and chain_record != c.id and active_rejection(session, c.id, chain_record) is not None) \
                or (chain and chain_rejected(session, chain, c.id)):
            rejected_any = True
            continue
        held = record_keys(session, c.id, namespaces)
        bad = contradictions(held, keys)
        if bad:
            c.vetoes.append("contradiction")
            c.evidence.append(ev("contradiction", [f"{ns}:{ext}" for ns, ext in bad][:5], 0.0, veto=True))
        else:
            for ns, ext, variant in keys:
                if variant and held.get((ns, "")) and ext not in held[(ns, "")]:
                    kind = "edition_key" if variant.startswith("edition:") else "platform_key"
                    c.evidence.append(ev(kind, f"{ns}:{ext} {variant}", 0.0))
        if unresolved_atlas:
            c.vetoes.append("unresolved_atlas")
            c.evidence.append(ev("unresolved_atlas", None, 0.0, veto=True))
        kept.append(c)
    if not kept:
        return _decide("rejected" if rejected_any else "none")
    kept = kept[:MAX_CANDIDATES]
    best = kept[0]
    clean = [c for c in kept if not c.vetoes]
    runner = next((c.score for c in kept[1:] if not c.vetoes), 0.0) if not best.vetoes else \
        (kept[1].score if len(kept) > 1 else 0.0)
    margin = round(best.score - runner, 3)
    evidence = list(best.evidence)
    strong = best.score >= settings.auto_match_threshold and best.base in ("title_equal", "title_fuzzy")
    if margin < MARGIN:
        evidence.append(ev("margin", margin, 0.0, veto=True))
    evidence = evidence[:MAX_EVIDENCE]
    common = {"score": round(best.score, 3), "margin": margin, "evidence": evidence, "candidates": kept}
    if not best.vetoes and strong and margin >= MARGIN:
        reason = "exact" if best.base == "title_equal" else "fuzzy"
        return _decide(reason, canonical_id=best.id, **common)
    if "contradiction" in best.vetoes and strong and margin >= MARGIN:
        return _decide("contradicted", review="title_contradicted", **common)
    if "unresolved_atlas" in best.vetoes:
        return _decide("unresolved_atlas", review="possible_duplicate", **common)
    if not clean and "contradiction" in best.vetoes:
        return _decide("contradicted", review="title_contradicted", **common)
    return _decide("ambiguous", review="possible_duplicate", **common)


# ---- the matcher (§11.2.3) -------------------------------------------------------------------------------

def match(session: Session, settings: Settings, facts: Facts, *, chain: str | None = None,
          chain_record: str | None = None, exclude: str | None = None) -> Decision:
    """Ingest matching. On identity evidence that is not consistent with one record it assigns the record the
    observation's own key names (or none: a new record) and asks for review."""
    ident = identity(session, facts)
    live = sorted(set(ident.matched.values()))
    keys = facts.keys()
    id_items = [ev("atlas_id" if ns == ATLAS_NAMESPACE else "crosswalk", f"{ns}:{ext}", 1.0)
                for (ns, ext), _cid in sorted(ident.matched.items())][:8]
    matched_keys = [(ns, ext, cid) for (ns, ext), cid in ident.matched.items()]
    if live:
        typed = _typed(session, live, facts.media_type)
        if not typed:
            return _decide("type_mismatch", conflict=live, evidence=[*id_items, ev("type_mismatch", None, 0.0,
                                                                                    veto=True)][:MAX_EVIDENCE],
                           matched_keys=matched_keys)
        own_key = (facts.namespace, facts.external_id)
        own = ident.own_atlas if facts.namespace == ATLAS_NAMESPACE else ident.matched.get(own_key)
        own = own if own in typed else None
        if len(typed) == 1:
            target = typed[0]
            held = record_keys(session, target)
            bad = contradictions(held, keys)
            if not bad:
                asked = ident.requested_atlas.get(target)
                reason = "crosswalk" if asked is None else ("redirect" if asked != target else "id")
                return _decide(reason, canonical_id=target, score=1.0, evidence=id_items, matched_keys=matched_keys,
                               redirected_from=asked if reason == "redirect" else None, consistent=True,
                               candidates=[Candidate(target, 1.0, "id")])
            # §11.2.1 step 4: inconsistent with the one record it hits. The contradicting keys are not
            # registered; the observation goes to its own key's record (or a new one).
            items = [*id_items, ev("namespace_conflict", [f"{ns}:{ext}" for ns, ext in bad][:5], 0.0, veto=True)]
            return _decide("conflict", canonical_id=own, score=0.9 if own else 0.0, evidence=items[:MAX_EVIDENCE],
                           review="id_conflict", own=own, conflict=typed if own != target else [],
                           blocked=set(bad), matched_keys=matched_keys, consistent=False)
        parties = [record_keys(session, cid) for cid in typed] + [_facts_keys_map(keys)]
        ok = consistent(parties)
        # Several records: a bridge (§11.3) would join them when the evidence is consistent and corroborated.
        # Bridges ship with federated unmerge (C11-4 after C11-5, H33), so this release always asks a curator.
        items = [*id_items, ev("consistent", ok, 0.0), ev("bridge_blocked", ["not_available"], 0.0, veto=True)]
        blocked = set()
        if own is not None:
            blocked = set(contradictions(record_keys(session, own), keys))
        return _decide("conflict", canonical_id=own, score=0.9 if own else 0.0, evidence=items[:MAX_EVIDENCE],
                       review="id_conflict", own=own, conflict=typed, blocked=blocked, matched_keys=matched_keys,
                       consistent=ok)

    from .resolve import is_placeholder_title

    if is_placeholder_title(facts.title):
        return _decide("placeholder", evidence=[ev("placeholder", norm_title(facts.title), 0.0, veto=True)])
    cands = title_candidates(session, settings, facts, exclude=exclude)
    if not cands:
        return _decide("none")
    return apply_vetoes(session, settings, facts, cands, unresolved_atlas=ident.unresolved_atlas, chain=chain,
                        chain_record=chain_record)


# ---- review queue (§11.5) --------------------------------------------------------------------------------

def pair_key(kind: str, ids: list[str]) -> str:
    return hashlib.sha256((kind + "\n" + "\n".join(sorted(set(ids)))).encode()).hexdigest()[:32]


def priority(kind: str, score: float) -> int:
    base = REVIEW_PRIORITY.get(kind, 10)
    return base + round(score * 10) if kind == "possible_duplicate" else base


def open_review(session: Session, settings: Settings, kind: str, cid: str, candidates: list[tuple[str, float]],
                confidence: float, *, reason: str | None = None, evidence: list[dict[str, Any]] | None = None,
                decision_id: str | None = None, tier: str | None = None) -> ReviewItem | None:
    """Open (or refresh) the one open item for this pair. Not opened when an active rejection covers every
    candidate pair (rule 2), or when the same pair was dismissed within `review_dismiss_days` and the new
    evidence is not stronger (rule 3)."""
    cands = [[c, s] for c, s in candidates if c != cid]
    if not cands:
        return None
    if all(active_rejection(session, cid, c) is not None for c, _s in cands):
        return None
    key = pair_key(kind, [cid, *(c for c, _s in cands)])
    session.flush()  # sessions do not autoflush: one ingest item may open the same review twice
    existing = session.scalars(select(ReviewItem).where(ReviewItem.pair_key == key,
                                                        ReviewItem.status == "open")).first()
    if existing is not None:
        existing.candidates, existing.confidence = cands, confidence
        existing.reason, existing.evidence = reason, evidence
        existing.decision_id = decision_id or existing.decision_id
        existing.priority = priority(kind, confidence)
        return existing
    since = now() - timedelta(days=settings.review_dismiss_days)
    dismissed = session.scalars(select(ReviewItem).where(
        ReviewItem.pair_key == key, ReviewItem.status == "dismissed", ReviewItem.resolved_at.is_not(None),
        ReviewItem.resolved_at >= since).order_by(ReviewItem.resolved_at.desc()).limit(1)).first()
    if dismissed is not None:
        stronger = (tier == "id" and kind != "id_conflict") or confidence >= (dismissed.confidence or 0) + 0.1
        if not stronger:
            return None
    item = ReviewItem(kind=kind, canonical_id=cid, candidates=cands, confidence=confidence, reason=reason,
                      evidence=evidence, decision_id=decision_id, pair_key=key, priority=priority(kind, confidence))
    session.add(item)
    session.flush()
    return item


def close_review(session: Session, item: ReviewItem, status: str, actor: str) -> None:
    item.status, item.decided_by, item.resolved_at = status, actor[:128] or None, now()
    session.add(AuditEntry(actor_id=None, action="review_resolve", target=item.id,
                           detail={"actor": actor[:128], "status": status, "kind": item.kind}))
    session.flush()


#: §11.9.2: review items one merge, unmerge or "different works" decision may touch (the work stays bounded by the
#: records involved, never by the size of the queue; a merge snapshot is bounded by MERGE_MAX_ITEMS as well).
MAX_TOUCHED_REVIEWS = 10_000


def touched_ids(session: Session, ids: set[str]) -> set[str]:
    """`ids` and every merged-in id that redirects to one of them (`record_live`, indexed)."""
    out = {i for i in ids if i}
    if out:
        out |= set(session.scalars(select(RecordLive.id).where(RecordLive.live_id.in_(sorted(out)))))
    return out


def reviews_touching(session: Session, ids: set[str]) -> list[ReviewItem]:
    """Open review items whose record or one of whose candidates is in `ids` (or redirects to one of them), oldest
    first, through the `review_item.canonical_id` and `review_candidate` indexes."""
    wanted = sorted(touched_ids(session, ids))
    if not wanted:
        return []
    session.flush()
    by_candidate = select(ReviewCandidate.review_id).where(ReviewCandidate.canonical_id.in_(wanted))
    return list(session.scalars(select(ReviewItem).where(
        ReviewItem.status == "open", or_(ReviewItem.canonical_id.in_(wanted), ReviewItem.id.in_(by_candidate)))
        .order_by(ReviewItem.created_at, ReviewItem.id).limit(MAX_TOUCHED_REVIEWS)))


def reevaluate_reviews(session: Session, ids: set[str], *, actor: str = "") -> None:
    """After a merge or unmerge of `ids`: refresh open items naming them; an item whose records collapse to one
    live record is `superseded` (§11.5 rule 7), and of two items that now describe the same pair the newer one
    is too. Only the items naming `ids` are read (`reviews_touching`)."""
    if not ids:
        return
    for item in reviews_touching(session, ids):
        cid = follow(session, item.canonical_id)
        cands: list[list[Any]] = []
        for c, s in item.candidates or []:
            live = follow(session, c)
            if live != cid and live not in {x for x, _ in cands}:
                cands.append([live, s])
        if not cands:
            item.status, item.resolved_at = "superseded", now()
            continue
        key = pair_key(item.kind, [cid, *(c for c, _s in cands)])
        if key == item.pair_key and cid == item.canonical_id and cands == item.candidates:
            continue
        clash = session.scalars(select(ReviewItem.id).where(ReviewItem.pair_key == key, ReviewItem.status == "open",
                                                            ReviewItem.id != item.id)).first()
        if clash is not None:
            item.status, item.resolved_at = "superseded", now()
            continue
        item.canonical_id, item.candidates, item.pair_key = cid, cands, key
        session.flush()


def review_out(item: ReviewItem) -> dict[str, Any]:
    return {"id": item.id, "kind": item.kind, "canonical_id": item.canonical_id, "candidates": item.candidates,
            "confidence": item.confidence, "status": item.status, "reason": item.reason,
            "evidence": item.evidence or [], "decision_id": item.decision_id, "priority": item.priority,
            "pair_key": item.pair_key, "created_at": _iso(item.created_at), "resolved_at": _iso(item.resolved_at),
            "decided_by": item.decided_by}


# ---- decision records (§11.4) ----------------------------------------------------------------------------

def record_decision(session: Session, *, source: str, scope: str, external_id: str, observation_id: str | None,
                    canonical_id: str | None, outcome: str, decision: Decision | None = None, tier: str = "id",
                    reason: str = "forced", score: float = 1.0, actor: str = "auto",
                    review_id: str | None = None) -> MatchDecision:
    d = decision
    row = MatchDecision(
        observation_id=observation_id, source=source[:128], source_scope=scope[:512], external_id=external_id[:256],
        canonical_id=canonical_id, outcome=outcome, tier=d.tier if d else tier, reason=d.reason if d else reason,
        score=float(d.score if d else score), margin=d.margin if d else None,
        evidence=(d.evidence if d else [])[:MAX_EVIDENCE],
        candidates=[c.out() for c in (d.candidates if d else [])][:MAX_CANDIDATES],
        merges=None, matcher=MATCHER, actor=actor[:128], review_id=review_id)
    session.add(row)
    return row


def decision_out(row: MatchDecision, *, full: bool = True) -> dict[str, Any]:
    out = {"id": row.id, "matcher": row.matcher, "at": _iso(row.created_at), "actor": row.actor,
           "chain": {"source": row.source, "source_scope": row.source_scope, "external_id": row.external_id},
           "observation_id": row.observation_id, "outcome": row.outcome, "canonical_id": row.canonical_id,
           "tier": row.tier, "reason": row.reason, "score": row.score, "margin": row.margin}
    if full:
        out.update({"evidence": row.evidence, "candidates": row.candidates, "merges": row.merges or [],
                    "review_id": row.review_id})
    return out


def prune_decisions(session: Session, settings: Settings, *, batch: int = 1000, max_rows: int = 200_000) -> int:
    """Retention (§11.4): keep the newest decision per chain, every decision an open review or a live merge record
    references, and everything younger than `match_decision_retain_days`. The database finds the deletable rows
    (an indexed anti-join on the chain index: an older decision with a newer one in its chain), so old rows that
    must stay never hide the deletable ones behind them. Deletes in batches of `batch`, at most `max_rows` rows per
    call (the daily housekeeping run catches up on the rest)."""
    from sqlalchemy import and_, exists
    from sqlalchemy.orm import aliased

    from .models import MergeRecord

    cutoff = now() - timedelta(days=settings.match_decision_retain_days)
    newer = aliased(MatchDecision)
    has_newer = exists().where(
        newer.source == MatchDecision.source, newer.source_scope == MatchDecision.source_scope,
        newer.external_id == MatchDecision.external_id,
        or_(newer.created_at > MatchDecision.created_at,
            and_(newer.created_at == MatchDecision.created_at, newer.id > MatchDecision.id)))
    by_review = select(ReviewItem.decision_id).where(ReviewItem.status == "open", ReviewItem.decision_id.is_not(None))
    by_merge = select(MergeRecord.decision_id).where(MergeRecord.reverted_at.is_(None),
                                                     MergeRecord.decision_id.is_not(None))
    total = 0
    while total < max_rows:
        doomed = list(session.scalars(select(MatchDecision.id).where(
            MatchDecision.created_at < cutoff, has_newer, MatchDecision.id.not_in(by_review),
            MatchDecision.id.not_in(by_merge)).order_by(MatchDecision.created_at, MatchDecision.id)
            .limit(min(batch, max_rows - total))))
        if not doomed:
            break
        session.execute(delete(MatchDecision).where(MatchDecision.id.in_(doomed)))
        total += len(doomed)
        if len(doomed) < batch:
            break
    return total
