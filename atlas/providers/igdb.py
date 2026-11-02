"""IGDB adapter. Shapes follow IGDB's public v4 docs; not exercised against the live API in CI."""

from __future__ import annotations

import time

from .. import dates
from .base import ProviderResult, epoch_to_date, get_json, release_ext

# external_games.category values documented by IGDB
_EXTERNAL = {1: "steam", 5: "gog", 26: "epic", 11: "microsoft"}
_FIELDS = (
    "cover.image_id,screenshots.image_id,artworks.image_id,name,first_release_date,summary,genres.name,themes.name,platforms.name,"
    "game_modes.name,alternative_names.name,aggregated_rating,rating,"
    "involved_companies.developer,involved_companies.publisher,involved_companies.company.name,"
    "external_games.category,external_games.uid,"
    "release_dates.date,release_dates.y,release_dates.m,release_dates.platform.name,"
    "release_dates.date_format.format,release_dates.release_region.region,release_dates.status.name,"
    "release_dates.category,release_dates.region"
)
# IGDB v4 release_dates (api-docs.igdb.com, checked 2026-11-02): `category` and `region` are deprecated enums,
# replaced by the references `date_format` (date_formats.format) and `release_region`
# (release_date_regions.region); `status` references release_date_statuses (name). The references are read
# first; the deprecated enums are a fallback for rows that still carry only them.
# How precise the date is. Quarters (YYYYQ1-4, enum 3-6) are reserved in C5.1 and TBD (7) is unknown.
_PRECISION = {0: "day", 1: "month", 2: "year"}
_FORMAT = {"YYYYMMMMDD": "day", "YYYYMMMM": "month", "YYYY": "year"}
# region -> C5.1 region (the enum ids and the release_date_regions names list the same regions)
_REGION = {1: "eu", 2: "na", 3: "au", 4: "nz", 5: "jp", 6: "cn", 7: "asia", 8: "ww", 9: "kr", 10: "br"}
_REGION_NAME = {"europe": "eu", "north_america": "na", "australia": "au", "new_zealand": "nz", "japan": "jp",
                "china": "cn", "asia": "asia", "worldwide": "ww", "korea": "kr", "brazil": "br"}


def _precision(row):
    fmt = row.get("date_format")
    if isinstance(fmt, dict) and isinstance(fmt.get("format"), str):
        return _FORMAT.get(fmt["format"].strip().upper())
    return _PRECISION.get(row.get("category"))


def _region(row):
    ref = row.get("release_region")
    if isinstance(ref, dict) and isinstance(ref.get("region"), str):
        return _REGION_NAME.get(ref["region"].strip().casefold().replace(" ", "_"))
    return _REGION.get(row.get("region"))


def _category(row):
    """The C5.1 category from the row's release status: early access and alpha/beta builds must not become
    a "full" first release. A cancelled release is no release (None). No status, or any other status
    ("Full Release", "Offline"), is a full release. The status names are matched loosely (case, spaces and
    hyphens ignored) because IGDB documents the entity, not its values."""
    status = row.get("status")
    name = status.get("name") if isinstance(status, dict) else None
    key = "".join(c for c in name.casefold() if c.isalnum()) if isinstance(name, str) else ""
    if "earlyaccess" in key:
        return "early_access"
    if "alpha" in key or "beta" in key:
        return "beta"
    if "cancel" in key:
        return None
    return "full"


class Igdb:
    name = "igdb"
    redistributable = False
    requests_per_minute = 240  # IGDB allows 4 req/s

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self._token: tuple[str, float] | None = None

    def _auth(self, client) -> dict[str, str]:
        if not self._token or self._token[1] < time.time() + 60:
            data = get_json(client, "POST", "https://id.twitch.tv/oauth2/token", params={
                "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"})
            self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return {"Client-ID": self.client_id, "Authorization": f"Bearer {self._token[0]}"}

    def _query(self, client, body):
        return get_json(client, "POST", "https://api.igdb.com/v4/games", headers=self._auth(client), content=body)

    def search(self, client, query):
        safe = query.replace('"', " ").replace("\\", " ")
        return [self._result(g) for g in self._query(client, f'search "{safe}"; fields {_FIELDS}; limit 10;')]

    def fetch(self, client, external_id):
        if not str(external_id).isdigit():
            return None
        rows = self._query(client, f"fields {_FIELDS}; where id = {int(external_id)};")
        return self._result(rows[0]) if rows else None

    @staticmethod
    def _result(g):
        companies = g.get("involved_companies", [])
        pick = lambda flag: next((c["company"]["name"] for c in companies if c.get(flag) and c.get("company")), None)  # noqa: E731
        ids = {}
        for ext in g.get("external_games", []):
            ns = _EXTERNAL.get(ext.get("category"))
            if ns and ext.get("uid"):
                ids.setdefault(ns, str(ext["uid"]))
        entries = _releases(g.get("release_dates") or [])
        if entries:
            first = dates.first_of_entries(entries)
            day, release = release_ext((first.value, first.precision) if first else None, entries)
        else:  # no per-release rows: the summary date, which IGDB only gives as a day
            day, release = epoch_to_date(g.get("first_release_date")), None
        return ProviderResult(
            external_id=str(g["id"]), title=g["name"], release_date=day, release=release,
            provider_ids=ids,
            facts={
                "description": g.get("summary"), "developer": pick("developer"), "publisher": pick("publisher"),
                "genres": [x["name"] for x in g.get("genres", [])][:50],
                "themes": [x["name"] for x in g.get("themes", [])][:50],
                "features": [x["name"] for x in g.get("game_modes", [])][:50],
                "aliases": [x["name"] for x in g.get("alternative_names", []) if x.get("name")][:30],
                "critic_score": round(g["aggregated_rating"]) if isinstance(g.get("aggregated_rating"), (int, float)) else None,  # noqa: E501
                "user_score": round(g["rating"]) if isinstance(g.get("rating"), (int, float)) else None,
            },
            artwork=_artwork(g),
        )


def _img(image_id, size):
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg"


def _artwork(g):
    art = []
    cover = (g.get("cover") or {}).get("image_id")
    if cover:
        art.append({"kind": "key_art", "url": _img(cover, "cover_big"), "priority": 20})
    art += [{"kind": "hero", "url": _img(a["image_id"], "1080p"), "priority": 20 + i}
            for i, a in enumerate((g.get("artworks") or [])[:3]) if a.get("image_id")]
    art += [{"kind": "screenshot", "url": _img(s["image_id"], "screenshot_big"), "priority": 30 + i}
            for i, s in enumerate((g.get("screenshots") or [])[:12]) if s.get("image_id")]
    return art


def _releases(rows):
    """IGDB release_dates as C5.1 entries: precision from the date format, never invented (a year-only row
    is "2011", not the 1 January its timestamp points at), category from the release status. One entry per
    (platform, region, category), the earliest kept; at most five, full and early-access releases kept
    before betas so the first release is never cut; earliest first."""
    best = {}
    for r in rows:
        precision, category, year = _precision(r), _category(r), r.get("y")
        if category is None or precision is None or not isinstance(year, int):
            continue
        if precision == "day":
            value = epoch_to_date(r.get("date"))
        elif precision == "month" and isinstance(r.get("m"), int):
            value = f"{year:04d}-{r['m']:02d}"
        else:
            value = f"{year:04d}" if precision == "year" else None
        if value is None or dates.try_parse(value, precision) is None:
            continue
        entry = {"date": value, "precision": precision, "category": category}
        platform = ((r.get("platform") or {}).get("name") or "").strip()[:dates.MAX_PLATFORM]
        if platform:
            entry["platform"] = platform
        region = _region(r)
        if region:
            entry["region"] = region
        key = dates.entry_key(entry)
        when = dates.from_dict(entry).sort_key()
        if key not in best or when < dates.from_dict(best[key]).sort_key():
            best[key] = entry
    def chronological(e):
        return dates.from_dict(e).sort_key(), dates.entry_key(e)

    kept = sorted(best.values(), key=lambda e: (e["category"] not in dates.FIRST_CATEGORIES, chronological(e)))
    return sorted(kept[:dates.MAX_RELEASES], key=chronological)
