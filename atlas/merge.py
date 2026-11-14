"""Field-level merge of current observations into a canonical view.

Each field has a strategy; every winning value records where it came from.
Locks (curator-pinned values) always win.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from . import dates
from .normalize import norm_namespace

MAX_UNION = 100


@dataclass(frozen=True)
class Obs:
    id: str
    source: str
    source_kind: str
    external_id: str
    payload: dict[str, Any]
    trust: int
    stamp: int  # source_updated_at if known, else observed time (epoch seconds)


SCALAR_FIELDS = {
    # field -> (strategy, extractor)
    "title": "priority",
    "release_date": "vote",
    "description": "priority",
    "developer": "priority",
    "publisher": "priority",
    "series": "priority",
    "age_rating": "priority",
    "website": "priority",
    "critic_score": "priority",
    "user_score": "priority",
}
UNION_FIELDS = ("platforms", "genres", "themes", "tags", "features", "languages", "aliases")
#: C5.2: lock fields that are neither scalar nor a string list. A `releases` lock holds up to five entries.
RELEASE_LOCKS = ("releases",)
#: Every field a curator may lock (the `lock` feed kind's vocabulary). `release_date` locks the first release:
#: its value may be partial ("2011"); `releases` (protocol 1.3) pins the dated releases.
LOCK_FIELDS = (*SCALAR_FIELDS, *UNION_FIELDS, *RELEASE_LOCKS)
#: View keys derived from the release dates (C5.2). `release_date` stays day-only for older clients.
RELEASE_VIEW_KEYS = ("first_release", "release_year", "releases")


def _value(obs: Obs, field: str) -> Any:
    p = obs.payload
    if field in ("title", "release_date"):
        return p.get(field)
    if field == "platforms":
        return ([p["platform"]] if p.get("platform") else []) + list(p.get("platforms") or []) or None
    return (p.get("facts") or {}).get(field)


def _rank(obs: Obs) -> tuple[int, int, str]:
    return (obs.trust, obs.stamp, obs.id)


def merge(observations: list[Obs], locks: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    view: dict[str, Any] = {}
    explain: dict[str, Any] = {}
    ranked = sorted(observations, key=_rank, reverse=True)

    _merge_releases(ranked, locks, view, explain)
    for field, strategy in SCALAR_FIELDS.items():
        if field == "release_date":
            continue  # derived from the first release above
        if field in locks:
            view[field] = locks[field]
            explain[field] = {"strategy": "lock"}
            continue
        candidates = [(o, _value(o, field)) for o in ranked if _value(o, field) not in (None, "")]
        if not candidates:
            continue
        if strategy == "vote":
            weight: dict[Any, int] = defaultdict(int)
            for o, v in candidates:
                weight[v] += max(o.trust, 1)
            best = max(weight.values())
            winners = [v for v in weight if weight[v] == best]
            value = next(v for o, v in candidates if v in winners)  # top-trust among tied
            src = next(o for o, v in candidates if v == value)
            view[field] = value
            explain[field] = {"strategy": "vote", "observation_id": src.id, "source": src.source}
        else:
            src, value = candidates[0]
            view[field] = value
            explain[field] = {"strategy": "priority", "observation_id": src.id, "source": src.source}

    for field in UNION_FIELDS:
        if field in locks:
            view[field] = list(locks[field])
            explain[field] = {"strategy": "lock"}
            continue
        seen: dict[str, str] = {}
        contributors: list[str] = []
        for o in ranked:
            vals = _value(o, field) or []
            added = False
            for v in vals:
                key = v.strip().casefold()
                if key and key not in seen and len(seen) < MAX_UNION:
                    seen[key] = v.strip()
                    added = True
            if added and o.source not in contributors:
                contributors.append(o.source)
        if seen:
            view[field] = list(seen.values())
            explain[field] = {"strategy": "union", "sources": contributors}

    # provider_ids: each observation's own identity plus anything it cross-references.
    ids: dict[str, tuple[str, Obs]] = {}
    for o in ranked:  # highest trust first; first claim on a namespace wins
        claims = {norm_namespace(o.source): o.external_id}
        claims.update(o.payload.get("provider_ids") or {})
        for ns, ident in claims.items():
            ids.setdefault(ns, (ident, o))
    if ids:
        view["provider_ids"] = {ns: v for ns, (v, _) in sorted(ids.items())}
        explain["provider_ids"] = {"strategy": "claim", "sources": sorted({o.source for _, o in ids.values()})}

    view["sources"] = sorted({o.source for o in observations})
    return view, explain


def _merge_releases(ranked: list[Obs], locks: dict[str, Any], view: dict[str, Any], explain: dict[str, Any]) -> None:
    """C5.2: the first release by hierarchical vote, the dated releases by a per-key vote, and the legacy
    day-only `release_date`. Provenance for every value is in `explain`.

    Precedence of the first release: a `release_date` lock; else a `releases` lock that lists a full or
    early-access release (its earliest such entry, C5.1); else the vote over the observations. The vote is
    by trust, so a lower-trust source's earlier full entry may stay in `releases` while the first release
    is later: `explain.first_release.earlier_entry` then names it (amendment A14.13)."""
    locked: list[dict[str, Any]] | None = None
    if "releases" in locks:
        try:
            locked = dates.clean_entries(locks["releases"])
        except dates.DateError:
            locked = []

    first: dates.PartialDate | None = None
    if "release_date" in locks:
        value = locks["release_date"]
        first = dates.try_parse_first(value)
        if first is not None:
            explain["first_release"] = {"strategy": "lock"}
        else:
            # Before C5 a lock took any text ("TBA"). Such a lock still shows as it always did in the legacy
            # key; it is not a date, so there is no first release, year or sort column.
            explain["first_release"] = {"strategy": "lock", "invalid": True}
            if isinstance(value, str) and value:
                view["release_date"] = value
                explain["release_date"] = {"strategy": "lock", "invalid": True}
    elif locked and (from_lock := dates.first_of_entries(locked)) is not None:
        first = from_lock
        explain["first_release"] = {"strategy": "lock", "field": "releases"}
    else:
        candidates = [(f, o.trust, o) for o in ranked if (f := dates.observation_first(o.payload)) is not None]
        result = dates.vote([(f, t) for f, t, _ in candidates])
        if result is not None:
            first = result.value
            src = candidates[result.winner][2]
            explain["first_release"] = {
                "strategy": "vote", "observation_id": src.id, "source": src.source, "support": result.support,
                "sources": sorted({o.source for f, _, o in candidates if f.agrees_with(first)}),
                "disagree": sorted({o.source for f, _, o in candidates if not f.agrees_with(first)})}
            if result.disputed:  # no year has half the weight: a low-confidence plurality year (A14.12)
                explain["first_release"]["disputed"] = True
    if first is not None:
        view["first_release"] = first.as_dict()
        view["release_year"] = first.year
        if first.precision == "day":  # the legacy key keeps its meaning: a full date or nothing
            view["release_date"] = first.value
            explain["release_date"] = dict(explain["first_release"])

    if locked is not None:
        if locked:
            view["releases"] = sorted(locked, key=lambda e: (dates.from_dict(e).sort_key(), dates.entry_key(e)))
        explain["releases"] = {"strategy": "lock"}
        _note_earlier_entry(view, explain)
        return
    groups: dict[tuple[str, str, str, str], list[tuple[dict[str, Any], Obs]]] = {}
    for o in ranked:
        for e in dates.observation_entries(o.payload):
            groups.setdefault(dates.entry_key(e), []).append((e, o))
    if not groups:
        return
    merged: dict[tuple[str, str, str, str], tuple[int, dict[str, Any]]] = {}
    provenance: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for key, claims in groups.items():
        result = dates.vote([(dates.from_dict(e), o.trust) for e, o in claims])
        assert result is not None
        top, src = claims[result.winner]
        entry = {**{k: v for k, v in top.items() if k not in ("date", "precision")}, **result.value.as_dict()}
        merged[key] = (max(o.trust for _, o in claims), entry)
        provenance[key] = {"observation_id": src.id, "source": src.source, "support": result.support,
                           "sources": sorted({o.source for _, o in claims})}
        if result.disputed:
            provenance[key]["disputed"] = True
    kept, dropped = dates.select_entries(merged)
    view["releases"] = [merged[k][1] for k in kept]
    explain["releases"] = {"strategy": "vote", "entries": [provenance[k] for k in kept],
                           "dropped": len(dropped)}
    _note_earlier_entry(view, explain)


def _note_earlier_entry(view: dict[str, Any], explain: dict[str, Any]) -> None:
    """Flag a kept full or early-access entry that ends before the first release starts (they contradict)."""
    first = view.get("first_release")
    earliest = dates.first_of_entries(view.get("releases") or [])
    if not isinstance(first, dict) or earliest is None or "first_release" not in explain:
        return
    when = dates.try_parse(first.get("date"), first.get("precision"), any_year=True)
    if when is not None and earliest.end() < when.start():
        explain["first_release"]["earlier_entry"] = earliest.as_dict()
