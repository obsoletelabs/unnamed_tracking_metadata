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
from datetime import UTC, date, datetime
from typing import Any

from . import vocab

#: C5.1: the range a release year may take (the host candidate allows wider years; releases use this one).
YEAR_MIN, YEAR_MAX = 1950, 2100
#: The legacy day-only `release_date` (`SourceObservationV1.release_date: date`) accepts any calendar day, and
#: C5.2 says it stays as it is. A day from that key (or a day lock on it) keeps any year a `date` can hold:
#: a 1937 film keeps its date. Partial dates and `ext.release` values use the C5.1 range above.
LEGACY_YEAR_MIN, LEGACY_YEAR_MAX = 1, 9999
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


def parse(value: Any, precision: Any = None, *, any_year: bool = False) -> PartialDate:
    """Validate a PartialDate (and, when given, its precision). Raises DateError; never guesses.

    `any_year` lifts the C5.1 1950-2100 range (to 1-9999). Only values that already existed before C5 use
    it: the legacy day-only `release_date` and the views, columns and filters derived from it."""
    if not isinstance(value, str) or not PARTIAL_RE.fullmatch(value):
        raise DateError("a release date is YYYY, YYYY-MM or YYYY-MM-DD")
    shape = precision_of(value)
    if precision is not None and precision != shape:
        if precision not in PRECISIONS:
            raise DateError(f"precision must be one of {', '.join(PRECISIONS)}")
        raise DateError(f"precision {precision!r} does not match the date {value!r} (that is {shape!r} precision)")
    year = int(value[:4])
    low, high = (LEGACY_YEAR_MIN, LEGACY_YEAR_MAX) if any_year else (YEAR_MIN, YEAR_MAX)
    if not low <= year <= high:
        raise DateError(f"a release year is between {YEAR_MIN} and {YEAR_MAX}")
    if shape == "day":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise DateError(f"{value} is not a calendar date") from exc
    return PartialDate(value, shape)


def try_parse(value: Any, precision: Any = None, *, any_year: bool = False) -> PartialDate | None:
    try:
        return parse(value, precision, any_year=any_year)
    except DateError:
        return None


def parse_first(value: Any) -> PartialDate:
    """A first release given as one string (a `release_date` lock or edit): a day keeps the legacy
    `release_date` meaning and may have any year; a year or a month is new (C5) and uses the C5.1 range."""
    return parse(value, any_year=isinstance(value, str) and precision_of(value) == "day")


def try_parse_first(value: Any) -> PartialDate | None:
    try:
        return parse_first(value)
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
    legacy = try_parse(payload.get("release_date"), "day", any_year=True)  # C5.2: stays as it is
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
    #: True when no year has at least half of the weight (C5.2's 50% rule fails at the top level). The
    #: value is then the plurality year (as the pre-C5 `vote` strategy chose), flagged as low confidence.
    disputed: bool = False


def vote(candidates: Sequence[tuple[PartialDate, int]]) -> Vote | None:
    """Vote over (date, weight) candidates given in rank order (highest trust first).

    1. vote on the year;
    2. among candidates that agree on that year *and state a month*, vote on the month;
    3. then, among those agreeing on the month and stating a day, on the day.

    Each level weighs `max(trust, 1)`. The vote stops at the deepest level whose winner has at least half of
    the weight seen at that level, so a less precise candidate never outvotes a more precise one it agrees
    with ("2011" and "2011-04-19" agree: the day wins). Ties go to the higher-ranked candidate.

    When even the year has no such winner (three or more sources each name a different year), the result
    is the plurality year only, never a month or a day, and `disputed` is set: the merge shows it as a
    low-confidence value instead of dropping a date the record had before C5 (amendment A14.12)."""
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
    disputed = year[1] * 2 < year[2]
    pool = [i for i in pool if candidates[i][0].value[:4] == result]
    for level, part, need in (("month", slice(0, 7), "month"), ("day", slice(0, 10), "day")):
        stating = [i for i in pool if candidates[i][0].rank >= RANK[need]]
        if not stating or disputed:
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
    return Vote(chosen, winner, support, disputed)


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
        when = try_parse(first.get("date"), first.get("precision"), any_year=True)
    if when is None:
        return {"release_start": None, "release_end": None, "release_precision": None, "year": None}
    return {"release_start": when.start(), "release_end": when.end(), "release_precision": when.precision,
            "year": when.year}


def parse_bound(value: Any, name: str) -> PartialDate | None:
    """A search filter bound: empty means none; otherwise any PartialDate ("2011" means the whole year). Any
    year is allowed, since a record's legacy day date may be older than 1950."""
    if value in (None, ""):
        return None
    try:
        return parse(value, any_year=True)
    except DateError as exc:
        raise DateError(f"{name}: {exc}") from exc


# ---- display: the one formatter (SPEC-UI-POLISH-PASS §6) ---------------------------------------------------

#: The viewer's date styles (profile field `date_format`). `dmy` is the default for everyone who has not chosen.
STYLES: tuple[str, ...] = ("dmy", "long", "mdy", "iso")
DEFAULT_STYLE = "dmy"
#: Values older releases stored, mapped where they are read (no data migration: the profile history is signed, §6.4).
LEGACY_STYLES: dict[str, str | None] = {"locale": None, "ymd": "iso"}
STYLE_LABELS = {"dmy": "31/12/2026 (day/month/year)", "long": "31 December 2026", "mdy": "12/31/2026 (month/day/year)",
                "iso": "2026-12-31 (ISO)"}
TIME_FORMATS: tuple[str, ...] = ("24h", "12h")
TIME_LABELS = {"24h": "14:05 (24-hour)", "12h": "2:05 PM (12-hour)"}
MONTHS: tuple[str, ...] = tuple(calendar.month_name)[1:]  # English until the interface is translated (H18)

# str.format, never strftime: strftime pads (or does not pad) years before 1000 differently per platform.
_DAY = {"dmy": "{d:02d}/{m:02d}/{y:04d}", "mdy": "{m:02d}/{d:02d}/{y:04d}", "iso": "{y:04d}-{m:02d}-{d:02d}",
        "long": "{d} {month} {y:04d}"}
_PLACEHOLDER = {"dmy": ("dd/mm/yyyy, mm/yyyy or yyyy", "dd/mm/yyyy"),
                "long": ("17 September 2020, September 2020 or 2020", "17 September 2020"),
                "mdy": ("mm/dd/yyyy, mm/yyyy or yyyy", "mm/dd/yyyy"),
                "iso": ("yyyy-mm-dd, yyyy-mm or yyyy", "yyyy-mm-dd")}


def style_of(prefs: Any = None) -> str:
    """The viewer's date style: unset, unknown or legacy `locale` -> `dmy`; legacy `ymd` -> `iso`."""
    value = prefs.get("date_format") if isinstance(prefs, dict) else None
    if value in LEGACY_STYLES:
        value = LEGACY_STYLES[value]
    return value if value in STYLES else DEFAULT_STYLE


def style_chosen(prefs: Any = None) -> bool:
    """True when the viewer explicitly chose a date style (a legacy `locale` counts as not chosen, H-UI5)."""
    value = prefs.get("date_format") if isinstance(prefs, dict) else None
    return value in STYLES or value == "ymd"


def _clock(prefs: dict, style: str) -> str:
    fmt = prefs.get("time_format")
    fmt = fmt if fmt in TIME_FORMATS else None  # legacy "locale" means unset
    return fmt or ("12h" if style == "mdy" else "24h")


def _day_text(y: int, m: int, d: int, style: str) -> str:
    return _DAY[style].format(y=y, m=m, d=d, month=MONTHS[m - 1])


def _zone(prefs: dict):
    """The viewer's time zone, or None for UTC (Atlas then says so, H-UI9)."""
    name = prefs.get("time_zone")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone or no tz database on this platform: UTC
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def format_day(value: Any, precision: Any = None, prefs: Any = None) -> str:
    """A calendar date as text, showing only what is known (C5 part 7.3): a year is the year; a month is
    "September 2020" (ISO: "2020-09"); a day follows the viewer's style (`dmy` by default). A datetime is shown as
    the day it is in the viewer's time zone. Anything that is not a date is shown unchanged."""
    if value is None or value == "":
        return ""
    prefs = prefs if isinstance(prefs, dict) else {}
    style = style_of(prefs)
    if isinstance(value, datetime):
        local = _aware(value).astimezone(_zone(prefs) or UTC)
        return _day_text(local.year, local.month, local.day, style)
    if isinstance(value, date):
        return _day_text(value.year, value.month, value.day, style)
    when = value if isinstance(value, PartialDate) else try_parse(value, precision, any_year=True)
    if when is None:
        return str(value)
    d = when.start()
    if when.precision == "year":
        return when.value
    if when.precision == "month":
        return when.value if style == "iso" else f"{MONTHS[d.month - 1]} {d.year:04d}"
    return _day_text(d.year, d.month, d.day, style)


def display(value: Any, precision: Any = None, prefs: dict[str, Any] | None = None) -> str:
    """Deprecated alias of `format_day`, kept for one release (callers outside the web layer may import it)."""
    return format_day(value, precision, prefs)


def _relative(dt: datetime, now: datetime, zone: Any) -> str | None:
    seconds = (now - dt).total_seconds()
    ahead = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "in under a minute" if ahead else "just now"
    if seconds < 3600:
        n = int(seconds // 60)
        return f"in {n} min" if ahead else f"{n} min ago"
    if seconds < 86400:
        n = int(seconds // 3600)
        return f"in {n} h" if ahead else f"{n} h ago"
    if not ahead and (now.astimezone(zone).date() - dt.astimezone(zone).date()).days == 1:
        return "yesterday"
    return None


def format_instant(dt: Any, prefs: Any = None, *, relative: bool = False, now: datetime | None = None) -> str:
    """A moment for display in the viewer's style, time format and time zone; without a time zone it is UTC and
    says so ("17/09/2020 14:05 UTC"). `relative` gives "5 min ago", "3 h ago", "yesterday", then the absolute
    form (callers keep the absolute value in `title` and `<time datetime>`)."""
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    if not isinstance(dt, datetime):
        return format_day(dt, None, prefs)
    prefs = prefs if isinstance(prefs, dict) else {}
    style = style_of(prefs)
    zone = _zone(prefs)
    local = _aware(dt).astimezone(zone or UTC)
    if relative:
        text = _relative(_aware(dt), _aware(now or datetime.now(UTC)), zone or UTC)
        if text:
            return text
    if _clock(prefs, style) == "12h":
        hour = local.hour % 12 or 12
        clock = f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    else:
        clock = f"{local.hour:02d}:{local.minute:02d}"
    day = _day_text(local.year, local.month, local.day, style)
    text = f"{day}, {clock}" if style == "long" else f"{day} {clock}"
    return text if zone is not None else f"{text} UTC"


def iso_of(value: Any) -> str:
    """The machine value for `<time datetime>`: ISO 8601 (UTC for moments), or the partial date as stored."""
    if isinstance(value, datetime):
        return _aware(value).astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def placeholder(prefs: Any = None, *, partial: bool = True) -> str:
    """The entry hint for a date field in the viewer's style (§7.1)."""
    pair = _PLACEHOLDER[style_of(prefs)]
    return pair[0] if partial else pair[1]


# ---- entry: what a person may type into a date field (§7.2) ---------------------------------------------

@dataclass(frozen=True)
class EntryResult:
    ok: PartialDate | None = None
    ambiguous: tuple[PartialDate, PartialDate] | None = None
    error: str | None = None


_SEP = r"[/.\-]"
_NUMERIC = re.compile(r"(\d{1,2})" + _SEP + r"(\d{1,2})" + _SEP + r"(\d{4})\Z", re.ASCII)
_YEAR_FIRST = re.compile(r"(\d{4})" + _SEP + r"(\d{1,2})(?:" + _SEP + r"(\d{1,2}))?\Z", re.ASCII)
_MONTH_YEAR = re.compile(r"(\d{1,2})" + _SEP + r"(\d{4})\Z", re.ASCII)
_D_MON_Y = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})\Z", re.ASCII)
_MON_D_Y = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\Z", re.ASCII)
_MON_Y = re.compile(r"([A-Za-z]+)\.?,?\s+(\d{4})\Z", re.ASCII)


def _month_name(word: str) -> int | None:
    """A month from its English name or any prefix of at least three letters ("Sep", "Sept", "September")."""
    word = word.lower()
    if len(word) < 3:
        return None
    for i, name in enumerate(MONTHS, start=1):
        if name.lower().startswith(word):
            return i
    return None


def _build(y: int, m: int | None, d: int | None, any_year: bool) -> PartialDate:
    value = f"{y:04d}" + (f"-{m:02d}" if m else "") + (f"-{d:02d}" if d else "")
    return parse(value, any_year=any_year)


def _readable(y: int, m: int, d: int, any_year: bool) -> PartialDate | None:
    if not 1 <= m <= 12:
        return None
    try:
        return _build(y, m, d, any_year)
    except DateError:
        return None


def parse_entry(text: Any, prefs: Any = None, *, any_year: bool = False) -> EntryResult:
    """Read a typed date in any accepted form. ISO is always unambiguous; `a/b/yyyy` is read in the viewer's
    style (`mdy` as month/day, every other style as day/month) and is ambiguous when both readings are dates,
    unless the viewer chose a style explicitly. Release dates keep C5.1's year range unless `any_year`."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return EntryResult(error="enter a date")
    try:
        if PARTIAL_RE.fullmatch(raw):
            return EntryResult(ok=parse(raw, any_year=any_year))
        if m := _YEAR_FIRST.fullmatch(raw):
            y, mo, d = int(m[1]), int(m[2]), int(m[3]) if m[3] else None
            if not 1 <= mo <= 12:
                return EntryResult(error=f"{raw} has no month {mo}")
            return EntryResult(ok=_build(y, mo, d, any_year))
        if m := _MONTH_YEAR.fullmatch(raw):
            mo, y = int(m[1]), int(m[2])
            if not 1 <= mo <= 12:
                return EntryResult(error=f"{raw} has no month {mo}")
            return EntryResult(ok=_build(y, mo, None, any_year))
        if m := _NUMERIC.fullmatch(raw):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            low, high = (LEGACY_YEAR_MIN, LEGACY_YEAR_MAX) if any_year else (YEAR_MIN, YEAR_MAX)
            if not low <= y <= high:
                return EntryResult(error=f"a release year is between {YEAR_MIN} and {YEAR_MAX}")
            month_first = style_of(prefs) == "mdy"
            first = _readable(y, a, b, any_year) if month_first else _readable(y, b, a, any_year)
            second = _readable(y, b, a, any_year) if month_first else _readable(y, a, b, any_year)
            if first is None and second is None:
                return EntryResult(error=f"{raw} is not a calendar date")
            if first is None or second is None or first == second or style_chosen(prefs):
                return EntryResult(ok=first or second)
            return EntryResult(ambiguous=(first, second))
        for pattern, order in ((_D_MON_Y, "dmy"), (_MON_D_Y, "mdy")):
            if m := pattern.fullmatch(raw):
                d, word = (int(m[1]), m[2]) if order == "dmy" else (int(m[2]), m[1])
                mo = _month_name(word)
                if mo is None:
                    return EntryResult(error=f"{word} is not a month")
                return EntryResult(ok=_build(int(m[3]), mo, d, any_year))
        if m := _MON_Y.fullmatch(raw):
            mo = _month_name(m[1])
            if mo is None:
                return EntryResult(error=f"{m[1]} is not a month")
            return EntryResult(ok=_build(int(m[2]), mo, None, any_year))
    except DateError as exc:
        return EntryResult(error=str(exc))
    return EntryResult(error=f"{raw} is not a date this field accepts ({placeholder(prefs)})")


_LOOSE_PREFIX = re.compile(r"^(?:circa|ca\.|c\.|c|copyright|cop\.|©|\(c\)|p)\s*(?=\d)", re.IGNORECASE)
_LOOSE_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)", re.ASCII)


def parse_loose(text: Any, *, any_year: bool = False) -> PartialDate | None:
    """A bibliographic date as free text ("1965", "August 1965", "Aug 01, 1965", "c1965", "[1965?]", "Spring 1965")
    at the precision it proves (C15 §3.3), or None. Nothing is invented: a copyright or circa year is that year, an
    `a/b/yyyy` date that reads as two different days is only its year, a range or two years is dropped. Release
    dates keep the C5.1 year range unless `any_year`."""
    if not isinstance(text, str):
        return None
    raw = " ".join(text.split()).strip(" .;,:[]()?")
    raw = _LOOSE_PREFIX.sub("", raw).strip(" .;,:[]()?")
    if not raw or len(raw) > 64:
        return None
    got = parse_entry(raw, any_year=any_year)
    if got.ok is not None:
        return got.ok
    if got.ambiguous is not None and got.ambiguous[0].value[:4] == got.ambiguous[1].value[:4]:
        return try_parse(got.ambiguous[0].value[:4], "year", any_year=any_year)
    years = set(_LOOSE_YEAR.findall(raw))
    if len(years) != 1:
        return None
    return try_parse(years.pop(), "year", any_year=any_year)


def region_name(code: str | None) -> str:
    if not code:
        return ""
    return REGION_NAMES.get(code, code.upper())


def category_name(category: str | None) -> str:
    return CATEGORY_NAMES.get(category or "", category or "")
