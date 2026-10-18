from __future__ import annotations

from .base import ProviderResult, get_json, parse_date, strip_html


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
        return ProviderResult(
            external_id=str(external_id), title=d["name"], platform="pc",
            release_date=None if rel.get("coming_soon") else parse_date(rel.get("date")),
            facts={
                "description": strip_html(d.get("short_description")),
                "developer": (d.get("developers") or [None])[0],
                "publisher": (d.get("publishers") or [None])[0],
                "tags": [g["description"] for g in d.get("genres", [])][:50],
                "features": [c["description"] for c in d.get("categories", [])][:50],
            },
        )
