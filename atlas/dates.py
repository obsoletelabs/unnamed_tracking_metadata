"""Partial and regional release dates (CONTRACTS-v1 C5, master requirements part 7).

A release date is a `PartialDate` plus an explicit precision: `"2011"` (year), `"2011-04"` (month) or
`"2011-04-19"` (day). The precision always equals the shape of the string; nothing here invents a month or
a day ("2011" is never "2011-01-01"). Dates are calendar dates without a time or a time zone.

A record has at most `MAX_RELEASES` release entries, each keyed by `(platform, region, edition, category)`,
and one *first release* (C5.1): the earliest `full` or `early_access` release, compared by the start of its
period, ties going to the more precise date. The legacy `release_date` key stays day-only: it is set only
when the first release is known to the day, and it then equals it.

This module holds the value type, validation, the hierarchical vote the merge uses (C5.2), the primary-date
columns used for sorting and filtering, and display formatting. It has no database access.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import vocab

#: C5.1: the range a release year may take (the host candidate allows wider years; releases use this one).
YEAR_MIN, YEAR_MAX = 1950, 2100
#: C5.1: at most this many release entries per record (per canonical in Atlas).
MAX_RELEASES = 5
PRECISIONS: tuple[str, ...] = vocab.DATE_PRECISIONS  # ("year", "month", "day"); "quarter" is reserved, not v1
CATEGORIES: tuple[str, ...] = vocab.RELEASE_CATEGORIES
#: Categories that can be a record's first release (C5.1).
FIRST_CATEGORIES = ("full", "early_access")
#: C5.1 region groups; any other region is an ISO 3166-1 alpha-2 code in lower case.
REGION_GROUPS: tuple[str, ...] = ("ww", "eu", "na", "asia", "oce", "latam")
REGION_RE = re.compile(r"(ww|eu|na|asia|oce|latam|[a-z]{2})\Z")
PARTIAL_RE = re.compile(r"\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?\Z", re.ASCII)  # ASCII digits only
MAX_PLATFORM, MAX_EDITION = 50, 100
ENTRY_FIELDS = ("date", "precision", "platform", "region", "edition", "category")
RANK = {p: i for i, p in enumerate(PRECISIONS)}

REGION_NAMES = {"ww": "Worldwide", "eu": "Europe", "na": "North America", "asia": "Asia", "oce": "Oceania",
                "latam": "Latin America", "us": "United States", "gb": "United Kingdom", "jp": "Japan",
                "au": "Australia", "nz": "New Zealand", "kr": "South Korea", "cn": "China", "de": "Germany",
                "fr": "France", "br": "Brazil", "ca": "Canada"}
CATEGORY_NAMES = {"full": "Full release", "early_access": "Early access", "beta": "Beta", "rerelease": "Re-release",
                  "port": "Port", "demo": "Demo"}


class DateError(ValueError):
    """A release date or entry that breaks the C5.1 rules. The message is safe to show to a contributor."""


@dataclass(frozen=True)
class PartialDate:
    value: str  # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    precision: str  # "year" | "month" | "day", always the shape of `value`

    @property
    def year(self) -> int:
        return int(self.value[:4])

    @property
    def month(self) -> int | None:
        return int(self.value[5:7]) if self.precision != "year" else None

    @property
    def day(self) -> int | None:
        return int(self.value[8:10]) if self.precision == "day" else None

    @property
    def rank(self) -> int:
        return RANK[self.precision]

    def start(self) -> date:
        """First day of the period: how the date sorts (C5.5: "2011" sorts as the start of 2011)."""
        return date(self.year, self.month or 1, self.day or 1)

    def end(self) -> date:
        """Last day of the period: a filter that asks for dates inside a range uses it."""
        if self.precision == "day":
            return self.start()
        if self.precision == "month":
            return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])  # type: ignore[arg-type]
        return date(self.year, 12, 31)

    def sort_key(self) -> tuple[date, int]:
        return self.start(), self.rank

    def agrees_with(self, other: PartialDate) -> bool:
        """True when neither contradicts the other: "2011" and "2011-04-19" agree, "2010" and "2011" do not."""
        n = min(len(self.value), len(other.value))
        return self.value[:n] == other.value[:n]

    def as_dict(self) -> dict[str, str]:
        return {"date": self.value, "precision": self.precision}

    def __str__(self) -> str:
        return self.value


def precision_of(value: str) -> str:
    return {4: "year", 7: "month", 10: "day"}.get(len(value), "")


def parse(value: Any, precision: Any = None) -> PartialDate:
    """Validate a PartialDate (and, when given, its precision). Raises DateError; never guesses."""
    if not isinstance(value, str) or not PARTIAL_RE.fullmatch(value):
        raise DateError("a release date is YYYY, YYYY-MM or YYYY-MM-DD")
    shape = precision_of(value)
    if precision is not None and precision != shape:
        if precision not in PRECISIONS:
            raise DateError(f"precision must be one of {', '.join(PRECISIONS)}")
        raise DateError(f"precision {precision!r} does not match the date {value!r} (that is {shape!r} precision)")
    year = int(value[:4])
    if not YEAR_MIN <= year <= YEAR_MAX:
        raise DateError(f"a release year is between {YEAR_MIN} and {YEAR_MAX}")
    if shape == "day":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise DateError(f"{value} is not a calendar date") from exc
    return PartialDate(value, shape)


def try_parse(value: Any, precision: Any = None) -> PartialDate | None:
    try:
        return parse(value, precision)
    except DateError:
        return None


def from_dict(d: Any) -> PartialDate:
    if not isinstance(d, dict):
        raise DateError("a release date is an object with date and precision")
    return parse(d.get("date"), d.get("precision"))


# ---- release entries ------------------------------------------------------------------------------------

def _one_line(value: Any, name: str, limit: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DateError(f"{name} must be text")
    cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in value).split())
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise DateError(f"{name} is at most {limit} characters")
    return cleaned


def clean_entry(raw: Any) -> dict[str, str]:
    """One ReleaseEntry (C5.1), validated and normalised: unknown keys, a bad region or category, a precision
    that does not match the date and an impossible day are all refused."""
    if not isinstance(raw, dict):
        raise DateError("a release entry is an object")
    unknown = sorted(set(raw) - set(ENTRY_FIELDS))
    if unknown:
        raise DateError(f"unknown release entry field: {', '.join(map(str, unknown))[:100]}")
    when = parse(raw.get("date"), raw.get("precision"))
    out: dict[str, str] = when.as_dict()
    platform = _one_line(raw.get("platform"), "platform", MAX_PLATFORM)
    if platform:
        out["platform"] = platform
    region = raw.get("region")
    if region not in (None, ""):
        if not isinstance(region, str) or not REGION_RE.fullmatch(region):
            raise DateError("region is ww, eu, na, asia, oce, latam or a two-letter country code in lower case")
        out["region"] = region
    edition = _one_line(raw.get("edition"), "edition", MAX_EDITION)
    if edition:
        out["edition"] = edition
    category = raw.get("category")
    if category not in CATEGORIES:
        raise DateError(f"category must be one of {', '.join(CATEGORIES)}")
    out["category"] = category
    return out


def entry_key(e: dict[str, Any]) -> tuple[str, str, str, str]:
    """C5.1 uniqueness key. Platform and edition compare case-insensitively ("PS5" is "ps5")."""
    return ((e.get("platform") or "").casefold(), e.get("region") or "", (e.get("edition") or "").casefold(),
            e.get("category") or "full")


def clean_entries(raw: Any, *, edition: str | None = None, limit: int = MAX_RELEASES) -> list[dict[str, str]]:
    """A list of entries: at most `limit` (more are refused, not truncated), unique on the C5.1 key, and each
    entry's edition equal to the observation's `edition` when both are given."""
    if not isinstance(raw, list):
        raise DateError("releases must be a list")
    if len(raw) > limit:
        raise DateError(f"at most {limit} release dates per game (got {len(raw)})")
    out, seen = [], set()
    for i, item in enumerate(raw):
        try:
            entry = clean_entry(item)
        except DateError as exc:
            raise DateError(f"release {i + 1}: {exc}") from exc
        if edition and entry.get("edition") and entry["edition"] != edition:
            raise DateError(f"release {i + 1}: edition {entry['edition']!r} is not this observation's edition "
                            f"{edition!r}")
        key = entry_key(entry)
        if key in seen:
            raise DateError(f"release {i + 1} repeats platform, region, edition and category of an earlier one")
        seen.add(key)
        out.append(entry)
    return out


def first_of_entries(entries: Iterable[dict[str, Any]]) -> PartialDate | None:
    """C5.1 first release: the earliest full or early-access entry by the start of its period; ties go to the
    higher precision."""
    best: PartialDate | None = None
    for e in entries:
        if e.get("category") not in FIRST_CATEGORIES:
            continue
        when = try_parse(e.get("date"), e.get("precision"))
        if when is None:
            continue
        if best is None or (when.start(), -when.rank) < (best.start(), -best.rank):
            best = when
    return best


def observation_first(payload: dict[str, Any]) -> PartialDate | None:
    """What one observation says about the first release: `ext.release.first`, else the legacy day-only
    `release_date`, else the first release its own entries imply."""
    release = ((payload.get("ext") or {}).get("release") or {}) if isinstance(payload.get("ext"), dict) else {}
    first = release.get("first") if isinstance(release, dict) else None
    if isinstance(first, dict):
        got = try_parse(first.get("date"), first.get("precision"))
        if got is not None:
            return got
    legacy = try_parse(payload.get("release_date"), "day")
    if legacy is not None:
        return legacy
    entries = release.get("entries") if isinstance(release, dict) else None
    return first_of_entries(entries) if isinstance(entries, list) else None


def observation_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ext = payload.get("ext")
    release = ext.get("release") if isinstance(ext, dict) else None
    entries = release.get("entries") if isinstance(release, dict) else None
    out = []
    for e in entries if isinstance(entries, list) else []:
        try:
            out.append(clean_entry(e))
        except DateError:
            continue  # stored payloads were validated on the way in; never fail a merge on one bad entry
    return out


# ---- the hierarchical vote (C5.2) ----------------------------------------------------------------------

@dataclass(frozen=True)
class Vote:
    value: PartialDate
    winner: int  # index of the candidate that best represents the result (for provenance)
    support: dict[str, list[int]]  # level -> [winning weight, weight seen at that level]


def vote(candidates: Sequence[tuple[PartialDate, int]]) -> Vote | None:
    """Vote over (date, weight) candidates given in rank order (highest trust first).

    1. vote on the year;
    2. among candidates that agree on that year *and state a month*, vote on the month;
    3. then, among those agreeing on the month and stating a day, on the day.

    Each level weighs `max(trust, 1)`. The vote stops at the deepest level whose winner has at least half of
    the weight seen at that level, so a less precise candidate never outvotes a more precise one it agrees
    with ("2011" and "2011-04-19" agree: the day wins). Ties go to the higher-ranked candidate."""
    if not candidates:
        return None
    support: dict[str, list[int]] = {}

    def pick(pool: list[int], part: slice) -> tuple[str, int, int]:
        weight: dict[str, int] = defaultdict(int)
        for i in pool:
            weight[candidates[i][0].value[part]] += max(candidates[i][1], 1)
        best = max(weight.values())
        value = next(candidates[i][0].value[part] for i in pool if weight[candidates[i][0].value[part]] == best)
        return value, best, sum(weight.values())

    pool = list(range(len(candidates)))
    result, _, _ = year = pick(pool, slice(0, 4))
    support["year"] = [year[1], year[2]]
    pool = [i for i in pool if candidates[i][0].value[:4] == result]
    for level, part, need in (("month", slice(0, 7), "month"), ("day", slice(0, 10), "day")):
        stating = [i for i in pool if candidates[i][0].rank >= RANK[need]]
        if not stating:
            break
        value, best, seen = pick(stating, part)
        support[level] = [best, seen]
        if best * 2 < seen:
            break
        result = value
        pool = [i for i in stating if candidates[i][0].value[part] == value]
    chosen = PartialDate(result, precision_of(result))
    winner = next(i for i in range(len(candidates))
                  if candidates[i][0].value[:len(result)] == result and candidates[i][0].rank >= chosen.rank)
    return Vote(chosen, winner, support)


Key = tuple[str, str, str, str]


def select_entries(groups: dict[Key, tuple[int, dict[str, Any]]], limit: int = MAX_RELEASES
                   ) -> tuple[list[Key], list[Key]]:
    """Which entry keys a record keeps (C5.2): the top `limit` by trust, then category order (full first),
    then the earliest date. `groups` maps key -> (best trust, merged entry). Returns (kept, dropped); the kept
    keys are in display order: chronological, then category order, then key."""
    order = {c: i for i, c in enumerate(CATEGORIES)}

    def category(key: Key) -> int:
        return order.get(groups[key][1]["category"], len(order))

    def when(key: Key) -> tuple[date, int]:
        return from_dict(groups[key][1]).sort_key()

    ranked = sorted(groups, key=lambda k: (-groups[k][0], category(k), when(k), k))
    kept, dropped = ranked[:limit], ranked[limit:]
    return sorted(kept, key=lambda k: (when(k), category(k), k)), dropped


# ---- the primary date: display, sort and filter --------------------------------------------------------

def columns(first: dict[str, Any] | None) -> dict[str, Any]:
    """The values of `canonical.release_start`, `release_end`, `release_precision` and `year` for a view's
    `first_release` (all None when it is unknown)."""
    when = None
    if isinstance(first, dict):
        when = try_parse(first.get("date"), first.get("precision"))
    if when is None:
        return {"release_start": None, "release_end": None, "release_precision": None, "year": None}
    return {"release_start": when.start(), "release_end": when.end(), "release_precision": when.precision,
            "year": when.year}


def parse_bound(value: Any, name: str) -> PartialDate | None:
    """A search filter bound: empty means none; otherwise any PartialDate ("2011" means the whole year)."""
    if value in (None, ""):
        return None
    try:
        return parse(value)
    except DateError as exc:
        raise DateError(f"{name}: {exc}") from exc


# ---- display ------------------------------------------------------------------------------------------

_DAY_FORMATS = {"iso": "%Y-%m-%d", "dmy": "%d.%m.%Y", "mdy": "%m/%d/%Y", "ymd": "%Y/%m/%d"}
_MONTH_FORMATS = {"iso": "%Y-%m", "dmy": "%m.%Y", "mdy": "%m/%Y", "ymd": "%Y/%m"}


def display(value: Any, precision: Any = None, prefs: dict[str, Any] | None = None) -> str:
    """A release date as text, showing only what is known (part 7.3). A year is always just the year; a
    month is "April 2011" unless the viewer chose a numeric date format; a day follows the viewer's date
    format (ISO by default, as before). Anything invalid is shown unchanged."""
    when = try_parse(value, precision)
    if when is None:
        return "" if value is None else str(value)
    style = (prefs or {}).get("date_format") or ""
    d = when.start()
    if when.precision == "year":
        return when.value
    if when.precision == "month":
        fmt = _MONTH_FORMATS.get(style)
        return d.strftime(fmt) if fmt else f"{calendar.month_name[d.month]} {d.year}"
    return d.strftime(_DAY_FORMATS.get(style, "%Y-%m-%d"))


def region_name(code: str | None) -> str:
    if not code:
        return ""
    return REGION_NAMES.get(code, code.upper())


def category_name(category: str | None) -> str:
    return CATEGORY_NAMES.get(category or "", category or "")
