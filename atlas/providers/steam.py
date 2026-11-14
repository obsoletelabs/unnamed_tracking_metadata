from __future__ import annotations

from .base import ProviderResult, get_json, parse_release, release_ext, strip_html


class Steam:
    name = "steam"
    redistributable = False
    requests_per_minute = 40  # appdetails is aggressively throttled by Valve

    def search(self, client, query):
        data = get_json(client, "GET", "https://store.steampowered.com/api/storesearch/",
                        params={"term": query, "cc": "us", "l": "en"})
        return [
            ProviderResult(external_id=str(i["id"]), title=i["name"], platform="pc")
            for i in data.get("items", []) if i.get("type", "app") == "app" and i.get("name")
        ][:10]

    def fetch(self, client, external_id):
        data = get_json(client, "GET", "https://store.steampowered.com/api/appdetails",
                        params={"appids": external_id, "cc": "us", "l": "en"})
        entry = (data or {}).get(str(external_id)) or {}
        if not entry.get("success"):
            return None
        d = entry["data"]
        if d.get("type") not in (None, "game", "dlc", "demo"):
            return None
        rel = d.get("release_date") or {}
        # "19 Apr, 2011", "Apr 2011" or "2011" (C5.2 keeps the precision); an unreleased game's text is a plan
        # ("Coming soon", "Q3 2025") and is left unknown, as before.
        day, release = release_ext(None if rel.get("coming_soon") else parse_release(rel.get("date")))
        return ProviderResult(
            external_id=str(external_id), title=d["name"], platform="pc", release_date=day, release=release,
            facts={
                "description": strip_html(d.get("short_description")),
                "developer": (d.get("developers") or [None])[0],
                "publisher": (d.get("publishers") or [None])[0],
                "genres": [g["description"] for g in d.get("genres", [])][:50],
                "features": [c["description"] for c in d.get("categories", [])][:50],
                "languages": _languages(d.get("supported_languages")),
                "critic_score": _score((d.get("metacritic") or {}).get("score")),
                "website": _https(d.get("website")),
            },
            custom=_multiplayer(d),
            artwork=_artwork(external_id, d),
        )

    def achievement_stats(self, client, external_id):
        """Global unlock percentages by achievement API name. Public: no Web API key needed (C12 §12.1.6)."""
        pct = {}
        data = get_json(client, "GET", "https://api.steampowered.com/ISteamUserStats/"
                        "GetGlobalAchievementPercentagesForApp/v2/", params={"gameid": external_id})
        for a in ((data.get("achievementpercentages") or {}).get("achievements", []))[:5000]:
            try:
                name, value = str(a["name"])[:200], float(a["percent"])
            except (KeyError, TypeError, ValueError):
                continue
            if name and 0 <= value <= 100:
                pct[name] = value
        return pct

    def achievements(self, client, external_id, api_key=None):
        """Names/descriptions need a Steam Web API key; global percentages do not (see `achievement_stats`, which
        the importer stores on their own when there is no key)."""
        if not api_key:
            return []
        pct = self.achievement_stats(client, external_id)
        schema = get_json(client, "GET", "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/",
                          params={"key": api_key, "appid": external_id, "l": "english"})
        out = []
        for a in ((schema.get("game") or {}).get("availableGameStats") or {}).get("achievements", []):
            if not a.get("displayName"):
                continue
            row = {"name": a["displayName"], "external_id": a.get("name", ""), "hidden": bool(a.get("hidden")),
                   "description": a.get("description", "")}
            for key, field in (("icon", "icon_url"), ("icongray", "locked_icon_url")):
                if str(a.get(key, "")).startswith("https://"):
                    row[field] = a[key]
            if a.get("name") in pct:
                row["global_percent"] = min(100.0, max(0.0, pct[a["name"]]))
            out.append(row)
        return out


def _multiplayer(d) -> dict:
    """Steam only states crossplay when it exists; absence is not evidence of 'no'."""
    categories = {c.get("description") for c in d.get("categories", [])}
    return {"crossplay": "yes"} if "Cross-Platform Multiplayer" in categories else {}


def _languages(raw) -> list[str]:
    """Steam sends an HTML string such as 'English<strong>*</strong>, French'."""
    text = strip_html(raw) or ""
    return [x.strip().rstrip("*").strip() for x in text.replace("languages with full audio support", "").split(",")
            if x.strip().rstrip("*").strip()][:60]


def _score(value):
    return int(value) if isinstance(value, (int, float)) and 0 <= value <= 100 else None


def _https(value):
    return value if isinstance(value, str) and value.startswith("https://") and len(value) <= 500 else None


def _artwork(app_id, d):
    base = f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}"
    art = []
    if str(d.get("header_image", "")).startswith("https://"):
        art.append({"kind": "banner", "url": d["header_image"], "priority": 10})
    art.append({"kind": "key_art", "url": f"{base}/library_600x900.jpg", "priority": 10, "width": 600, "height": 900})
    art.append({"kind": "hero", "url": f"{base}/library_hero.jpg", "priority": 10})
    art.append({"kind": "logo", "url": f"{base}/logo.png", "priority": 10})
    for i, s in enumerate((d.get("screenshots") or [])[:12]):
        if str(s.get("path_full", "")).startswith("https://"):
            art.append({"kind": "screenshot", "url": s["path_full"], "priority": 20 + i})
    return art
