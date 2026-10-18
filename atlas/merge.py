"""Field-level merge of current observations into a canonical view.

Each field has a strategy; every winning value records where it came from.
Locks (curator-pinned values) always win.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

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
}
UNION_FIELDS = ("platforms", "tags", "features")


def _value(obs: Obs, field: str) -> Any:
    p = obs.payload
    if field in ("title", "release_date"):
        return p.get(field)
    if field == "platforms":
        return [p["platform"]] if p.get("platform") else None
    return (p.get("facts") or {}).get(field)


def _rank(obs: Obs) -> tuple[int, int, str]:
    return (obs.trust, obs.stamp, obs.id)


def merge(observations: list[Obs], locks: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    view: dict[str, Any] = {}
    explain: dict[str, Any] = {}
    ranked = sorted(observations, key=_rank, reverse=True)

    for field, strategy in SCALAR_FIELDS.items():
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
