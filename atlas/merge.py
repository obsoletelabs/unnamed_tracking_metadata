"""Field-level merge of current observations into a canonical view.

Each field has a strategy; every winning value records where it came from and why it won (explain v2,
CONTRACT-C11 §11.7.2). Locks (curator-pinned or explicitly cleared values) always win.

Ranking (C11 §11.7.1): `(effective class, per-field source preference, numeric trust, freshness, observation id)`.
The class comes first, so numeric `source_trust` only orders sources within a class (H34), and the per-field
preference only reorders providers. The result is a pure function of the observations, the locks and the trust
configuration: the order in which observations arrived never matters (property P1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import dates
from .normalize import norm_namespace

MAX_UNION = 100
#: Explain is at most this large per record; union `members` are dropped first when it would be larger.
MAX_EXPLAIN_BYTES = 64 * 1024
#: At most this many runner-up observation ids per field.
MAX_RUNNERS_UP = 5
EXPLAIN_VERSION = 2

# ---- trust classes (C11 §11.7.1; normative mapping in code, never configuration, D11.6) ---------------------

CLASS_RANK = {"curator": 4, "publisher": 3, "community": 2, "provider": 1, "derived": 0}
CLASS_NAME = {rank: name for name, rank in CLASS_RANK.items()}
#: Sources of the `curator` class: reserved namespaces that need the curate scope to ingest.
CURATOR_SOURCES = frozenset({"curator", "manual"})
COMMUNITY_SOURCES = frozenset({"community"})
# `publisher` (rank 3) is reserved for verified publisher identities (#36); no source maps to it yet.


def source_class(source: str) -> str:
    ns = norm_namespace(source)
    if ns in CURATOR_SOURCES:
        return "curator"
    if ns in COMMUNITY_SOURCES:
        return "community"
    return "provider"


def effective_class(source: str, ceiling: str = "curator") -> str:
    """`min(source class, origin ceiling)`: a ceiling can only lower a class, never raise one."""
    own = source_class(source)
    return own if CLASS_RANK[own] <= CLASS_RANK.get(ceiling, CLASS_RANK["provider"]) else ceiling


class _Cleared:
    """A lock that clears its field (C11 §11.8): the field is deliberately empty."""

    def __repr__(self) -> str:
        return "CLEARED"


CLEARED: Any = _Cleared()


@dataclass(frozen=True)
class Obs:
    id: str
    source: str
    source_kind: str
    external_id: str
    payload: dict[str, Any]
    trust: int
    stamp: int  # source_updated_at if known, else observed time (epoch seconds)
    cls: int = CLASS_RANK["provider"]  # effective class rank (C11 §11.7.1)
    source_cls: int = CLASS_RANK["provider"]
    origin: str | None = None  # origin node of a replicated observation


SCALAR_FIELDS = {
    # field -> strategy
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
#: CONTRACT-C15 §2.3: book fields read from `ext.book`, each merged by priority (the highest-ranked observation that
#: states it wins as a whole; an author list is never interleaved) and lockable. They are not `SCALAR_FIELDS`: a
#: scalar lock must be a string, and `authors` is a list of objects and `page_count` a number (`service.clean_lock`
#: holds them to the `ext.book` rules). Registered under the unified release version (`feed_schema.BOOK_LOCK_FIELDS`).
BOOK_LOCKS = ("authors", "subtitle", "page_count", "original_language")
#: Every field a curator may lock (the `lock` feed kind's vocabulary). `release_date` locks the first release:
#: its value may be partial ("2011"); `releases` (protocol 1.3) pins the dated releases.
LOCK_FIELDS = (*SCALAR_FIELDS, *UNION_FIELDS, *RELEASE_LOCKS, *BOOK_LOCKS)
#: View keys derived from the release dates (C5.2). `release_date` stays day-only for older clients.
RELEASE_VIEW_KEYS = ("first_release", "release_year", "releases")


def book_of(payload: dict[str, Any]) -> dict[str, Any]:
    """An observation payload's `ext.book` member, or {} (C15 §2.3)."""
    ext = payload.get("ext")
    book = ext.get("book") if isinstance(ext, dict) else None
    return book if isinstance(book, dict) else {}


def _value(obs: Obs, field: str) -> Any:
    p = obs.payload
    if field in BOOK_LOCKS:
        return book_of(p).get(field)
    if field in ("title", "release_date"):
        return p.get(field)
    if field == "platforms":
        return ([p["platform"]] if p.get("platform") else []) + list(p.get("platforms") or []) or None
    return (p.get("facts") or {}).get(field)


_COMPONENTS = ("class", "preference", "trust", "freshness", "tie_break")


def _pref(obs: Obs, field: str, prefs: dict[str, list[str]] | None) -> int:
    """`field_pref_rank` (C11 §11.7.1): earlier in the field's preference list ranks higher. It only reorders
    sources of the `provider` class: a preference never lifts a provider above community or curator."""
    if not prefs or obs.cls != CLASS_RANK["provider"]:
        return 0
    order = prefs.get(field) or []
    ns = norm_namespace(obs.source)
    return len(order) - order.index(ns) if ns in order else 0


def _rank(obs: Obs, field: str = "", prefs: dict[str, list[str]] | None = None,
          cls: int | None = None) -> tuple[int, int, int, int, str]:
    return (obs.cls if cls is None else cls, _pref(obs, field, prefs) if field else 0, obs.trust, obs.stamp, obs.id)


def _separator(win: tuple, other: tuple) -> str:
    """The first ranking component that separates the winner from the best runner-up (explain `reason`)."""
    for name, a, b in zip(_COMPONENTS, win, other, strict=True):
        if a != b:
            return name
    return "tie_break"  # pragma: no cover - observation ids are unique


def _is_placeholder(value: Any) -> bool:
    from .resolve import is_placeholder_title

    return isinstance(value, str) and is_placeholder_title(value)


def _counts(rows: list[tuple[Obs, Any]], value: Any) -> tuple[int, int]:
    """Agreeing and disagreeing reports. Host and plugin observations (one chain per host scope) count once per
    source, so a thousand hosts do not outweigh one provider in the numbers shown."""
    agree: set[str] = set()
    disagree: set[str] = set()
    for o, v in rows:
        who = f"{o.source_kind}:{o.source}" if o.source_kind in ("host", "plugin") else o.id
        (agree if v == value else disagree).add(who)
    return len(agree), len(disagree - agree)


def _priority(field: str, observations: list[Obs],
              prefs: dict[str, list[str]] | None) -> tuple[Any, dict[str, Any]] | None:
    keyed = []
    for o in observations:
        v = _value(o, field)
        if v in (None, ""):
            continue
        # A placeholder title ("Steam app 12345") ranks as `derived`: any real title beats it (§11.8).
        cls = CLASS_RANK["derived"] if field == "title" and _is_placeholder(v) else None
        keyed.append((_rank(o, field, prefs, cls), o, v))
    if not keyed:
        return None
    keyed.sort(key=lambda t: t[0], reverse=True)
    top_rank, src, value = keyed[0]
    others = [(r, o) for r, o, v in keyed[1:] if v != value]
    explain: dict[str, Any] = {
        "strategy": "priority", "observation_id": src.id, "source": src.source,
        "reason": _separator(top_rank, others[0][0]) if others else "only_value",
        "class": CLASS_NAME[top_rank[0]], "source_class": CLASS_NAME[src.source_cls],
        "trust": src.trust, "stamp": src.stamp,
        "runners_up": [o.id for _, o in others[:MAX_RUNNERS_UP]],
        "conflict": any(o.cls >= CLASS_RANK["provider"] and o.source_kind == "metadata" for _, o in others)}
    if src.origin:
        explain["origin_node"] = src.origin
    explain["agree"], explain["disagree"] = _counts([(o, v) for _, o, v in keyed], value)
    return value, explain


def merge(observations: list[Obs], locks: dict[str, Any], *, prefs: dict[str, list[str]] | None = None,
          meta: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """`locks` maps a field to its pinned value, or to `CLEARED`. `prefs` is `field_source_preference`; `meta`
    becomes `explain._meta` (explain version, matcher, trust digest)."""
    view: dict[str, Any] = {}
    explain: dict[str, Any] = {}
    ranked = sorted(observations, key=_rank, reverse=True)

    _merge_releases(ranked, locks, view, explain)
    for field in SCALAR_FIELDS:
        if field == "release_date":
            continue  # derived from the first release above
        if field in locks:
            if locks[field] is CLEARED:
                explain[field] = {"strategy": "cleared", "reason": "lock"}
            else:
                view[field] = locks[field]
                explain[field] = {"strategy": "lock", "reason": "lock"}
            continue
        won = _priority(field, ranked, prefs)
        if won is not None:
            view[field], explain[field] = won

    for field in BOOK_LOCKS:  # C15 §2.3: present only when some observation (or a lock) states it
        if field in locks:
            if locks[field] is CLEARED:
                explain[field] = {"strategy": "cleared", "reason": "lock"}
            else:
                view[field] = locks[field]
                explain[field] = {"strategy": "lock", "reason": "lock"}
            continue
        won = _priority(field, ranked, prefs)
        if won is not None:
            view[field], explain[field] = won
    _derive_developer(view, explain, locks)

    for field in UNION_FIELDS:
        if field in locks:
            if locks[field] is CLEARED:
                explain[field] = {"strategy": "cleared", "reason": "lock"}
            else:
                view[field] = list(locks[field])
                explain[field] = {"strategy": "lock", "reason": "lock"}
            continue
        seen: dict[str, str] = {}
        members: dict[str, list[str]] = {}
        contributors: list[str] = []
        for o in sorted(observations, key=lambda o, f=field: _rank(o, f, prefs), reverse=True):
            added = False
            for v in _value(o, field) or []:
                key = v.strip().casefold()
                if not key:
                    continue
                if key not in seen and len(seen) < MAX_UNION:
                    seen[key] = v.strip()
                    added = True
                if key in seen and o.source not in members.setdefault(key, []):
                    members[key].append(o.source)
            if added and o.source not in contributors:
                contributors.append(o.source)
        if seen:
            view[field] = list(seen.values())
            explain[field] = {"strategy": "union", "sources": contributors, "reason": "union",
                              "members": {k: sorted(v) for k, v in members.items()}, "truncated": False}

    # provider_ids: each observation's own identity plus anything it cross-references. The first claim per
    # namespace by rank wins; losing claims are listed so a wrong cross-reference is visible (§11.7.2).
    claims_by_ns: dict[str, list[tuple[str, Obs]]] = {}
    for o in sorted(observations, key=lambda o: _rank(o, "provider_ids", prefs), reverse=True):
        claims = {norm_namespace(o.source): o.external_id}
        claims.update(o.payload.get("provider_ids") or {})
        for ns, ident in claims.items():
            claims_by_ns.setdefault(ns, []).append((ident, o))
    if claims_by_ns:
        view["provider_ids"] = {ns: rows[0][0] for ns, rows in sorted(claims_by_ns.items())}
        info: dict[str, Any] = {"strategy": "claim", "reason": "claim",
                                "sources": sorted({rows[0][1].source for rows in claims_by_ns.values()})}
        conflicts: dict[str, list[dict[str, Any]]] = {}
        for ns, rows in sorted(claims_by_ns.items()):
            values: dict[str, list[str]] = {}
            for ident, o in rows:
                srcs = values.setdefault(ident, [])
                if o.source not in srcs:
                    srcs.append(o.source)
            if len(values) > 1:
                conflicts[ns] = [{"value": v, "sources": sorted(srcs), "won": v == rows[0][0]}
                                 for v, srcs in values.items()][:MAX_RUNNERS_UP + 1]
        if conflicts:
            # Two claims for one namespace: indexed in `field_conflict`, so `/conflicts?field=provider_ids` lists a
            # record whose observation named an id it does not hold (§11.2.1 step 4 with only one record involved,
            # where there is no other record for an `id_conflict` item to name).
            info["conflicts"], info["conflict"] = conflicts, True
        explain["provider_ids"] = info

    editions = _editions(ranked)
    if editions:
        view["editions"] = editions
        explain["editions"] = {"strategy": "group", "sources": sorted({s for e in editions for s in e["sources"]})}

    view["sources"] = sorted({o.source for o in observations})
    if meta is not None:
        explain["_meta"] = dict(meta)
    _bound(explain)
    return view, explain


def _derive_developer(view: dict[str, Any], explain: dict[str, Any], locks: dict[str, Any]) -> None:
    """C15 §2.2: `developer` stays the single "primary creator" string. When no observation states one (and no lock
    pins or clears it), it is the first author's name, so consumers that read `developer` (older host plugins,
    search tiles) show an author."""
    if "developer" in view or "developer" in locks:
        return
    authors = view.get("authors")
    if isinstance(authors, list) and authors and isinstance(authors[0], dict) and authors[0].get("name"):
        view["developer"] = authors[0]["name"]
        explain["developer"] = {"strategy": "derived", "reason": "derived", "from": "authors"}


def _bound(explain: dict[str, Any]) -> None:
    """Keep explain within MAX_EXPLAIN_BYTES: union members go first (§11.7.2)."""
    if len(json.dumps(explain, separators=(",", ":"), default=str)) <= MAX_EXPLAIN_BYTES:
        return
    for entry in explain.values():
        if isinstance(entry, dict) and "members" in entry:
            entry["members"], entry["truncated"] = {}, True


def conflict_fields(explain: dict[str, Any]) -> list[str]:
    """The fields whose explain says `conflict: true` (the `field_conflict` index, §11.7.2)."""
    return sorted(f for f, e in explain.items() if isinstance(e, dict) and e.get("conflict") is True)


#: At most this many editions per record in the view.
MAX_EDITIONS = 50


#: C15 §2.6: the members a book edition adds to an `editions[]` entry, in display order (present only when known).
EDITION_DETAIL = ("isbn_13", "isbn_10", "format", "publisher", "published", "pages", "language")


def _edition_name(entry: dict[str, Any]) -> str:
    """The source's edition label, or "<Format>, <publisher> <year>" synthesised from what it states (C15 §2.6)."""
    name = " ".join(str(entry.get("name") or "").split())
    if name:
        return name
    published = entry.get("published")
    year = str(published.get("date", ""))[:4] if isinstance(published, dict) else ""
    tail = " ".join(p for p in (str(entry.get("publisher") or ""), year) if p)
    head = str(entry.get("format") or "").capitalize()
    return ", ".join(p for p in (head, tail) if p) or (f"ISBN {entry['isbn_13']}" if entry.get("isbn_13") else "")


def _edition_claims(o: Obs) -> list[tuple[str, str | None, dict[str, Any], dict[str, str]]]:
    """(name, isbn_13, details, provider_ids) for each edition an observation names: its own `edition` (the C6.1
    path, the observation's own id) and each `ext.book.editions[]` entry (C15 §2.6)."""
    out: list[tuple[str, str | None, dict[str, Any], dict[str, str]]] = []
    name = " ".join(str(o.payload.get("edition") or "").split())
    if name:
        out.append((name, None, {}, {norm_namespace(o.source): o.external_id}))
    for entry in book_of(o.payload).get("editions") or []:
        if not isinstance(entry, dict):
            continue
        label = _edition_name(entry)
        isbn = entry.get("isbn_13") if isinstance(entry.get("isbn_13"), str) else None
        if not label and not isbn:
            continue
        ids = {k: v for k, v in (entry.get("provider_ids") or {}).items() if isinstance(v, str)}
        if isinstance(entry.get("key"), str):
            ids.setdefault(f"{norm_namespace(o.source)}_edition"[:50], entry["key"])
        details = {k: entry[k] for k in EDITION_DETAIL if entry.get(k) not in (None, "")}
        out.append((label, isbn, details, ids))
    return out


def _editions(ranked: list[Obs]) -> list[dict[str, Any]]:
    """C6.1 / C15 §2.6: the record's editions. An edition is never a separate record or a relation target;
    per-edition dates are `releases[].edition` (C5.1).

    ISBN first: an edition with an `isbn_13` groups by that ISBN only (two sources naming one ISBN differently are one
    edition; two printings both called "Paperback" with different ISBNs stay two). An edition without one groups by
    its name (casefold, whitespace collapsed), as before, and joins an ISBN group only when exactly one ISBN group
    carries that name. Within a group the most trusted observation's spelling and details win; `provider_ids` maps
    each namespace to *that edition's* id there (the most trusted wins), never ids an observation only
    cross-references. At most `MAX_EDITIONS`, sorted by name."""
    groups: dict[str, dict[str, Any]] = {}  # "isbn:<13>" or "name:<casefold>"
    named_isbn: dict[str, set[str]] = {}  # casefold name -> ISBN group keys carrying it

    def claim(key: str, name: str, details: dict[str, Any], ids: dict[str, str], source: str) -> None:
        found = groups.get(key)
        if found is None:
            if len(groups) >= MAX_EDITIONS:
                return
            found = groups[key] = {"name": name, "provider_ids": {}, "sources": []}
        for k, v in details.items():
            found.setdefault(k, v)
        for ns, ident in ids.items():
            found["provider_ids"].setdefault(ns, ident)
        if source not in found["sources"]:
            found["sources"].append(source)

    names_only: list[tuple[str, str, dict[str, Any], dict[str, str], str]] = []
    for o in ranked:  # highest rank first
        for name, isbn, details, ids in _edition_claims(o):
            if isbn:
                claim(f"isbn:{isbn}", name, details, ids, o.source)
                named_isbn.setdefault(name.casefold(), set()).add(f"isbn:{isbn}")
            else:
                names_only.append((name, name.casefold(), details, ids, o.source))
    for name, folded, details, ids, source in names_only:  # rank order kept
        targets = named_isbn.get(folded, set())
        claim(next(iter(targets)) if len(targets) == 1 else f"name:{folded}", name, details, ids, source)
    out = []
    for item in sorted(groups.values(), key=lambda g: (g["name"].casefold(), g["name"], g.get("isbn_13") or "")):
        entry = {"name": item["name"], "provider_ids": dict(sorted(item["provider_ids"].items())),
                 "sources": sorted(item["sources"])}
        entry.update({k: item[k] for k in EDITION_DETAIL if k in item})
        out.append(entry)
    return out


def _top_class(rows: list[tuple[Any, int, Obs]]) -> list[tuple[Any, int, Obs]]:
    """C11 §11.8: votes count within the highest class present; lower classes vote only when it has no value."""
    if not rows:
        return rows
    top = max(o.cls for _, _, o in rows)
    return [r for r in rows if r[2].cls == top]


def _merge_releases(ranked: list[Obs], locks: dict[str, Any], view: dict[str, Any], explain: dict[str, Any]) -> None:
    """C5.2: the first release by hierarchical vote, the dated releases by a per-key vote, and the legacy
    day-only `release_date`. Provenance for every value is in `explain`.

    Precedence of the first release: a `release_date` lock; else a `releases` lock that lists a full or
    early-access release (its earliest such entry, C5.1); else the vote over the observations of the highest
    class that states one (C11 §11.8). The vote is by trust within that class, so a lower-trust source's earlier
    full entry may stay in `releases` while the first release is later: `explain.first_release.earlier_entry`
    then names it (amendment A14.13). A cleared lock (C11 §11.8) empties the field."""
    locked: list[dict[str, Any]] | None = None
    if locks.get("releases") is CLEARED:
        locked = []
    elif "releases" in locks:
        try:
            locked = dates.clean_entries(locks["releases"])
        except dates.DateError:
            locked = []

    first: dates.PartialDate | None = None
    if locks.get("release_date") is CLEARED:
        explain["first_release"] = {"strategy": "cleared", "reason": "lock"}
    elif "release_date" in locks:
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
        candidates = _top_class([(f, o.trust, o) for o in ranked
                                 if (f := dates.observation_first(o.payload)) is not None])
        result = dates.vote([(f, t) for f, t, _ in candidates])
        if result is not None:
            first = result.value
            src = candidates[result.winner][2]
            explain["first_release"] = {
                "strategy": "vote", "observation_id": src.id, "source": src.source, "class": CLASS_NAME[src.cls],
                "support": result.support,
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
        explain["releases"] = ({"strategy": "cleared", "reason": "lock"} if locks.get("releases") is CLEARED
                               else {"strategy": "lock"})
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
    for key, all_claims in groups.items():
        claims = [(e, o) for e, _t, o in _top_class([(e, o.trust, o) for e, o in all_claims])]
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
