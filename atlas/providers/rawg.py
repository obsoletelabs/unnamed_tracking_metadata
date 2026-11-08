from __future__ import annotations

from .base import ProviderResult, get_json

_PLATFORM = {"pc": "pc", "playstation": "playstation", "xbox": "xbox", "nintendo": "nintendo", "ios": "ios",
             "android": "android", "linux": "linux", "macos": "macos"}


class Rawg:
    name = "rawg"
    redistributable = False
    media_types = ("game",)
    requests_per_minute = 60

    def __init__(self, api_key: str) -> None:
        self.key = api_key

    def search(self, client, query):
        data = get_json(client, "GET", "https://api.rawg.io/api/games",
                        params={"key": self.key, "search": query, "page_size": 10, "search_precise": "true"})
        return [self._result(g) for g in data.get("results", []) if g.get("name")]

    def fetch(self, client, external_id):
        g = get_json(client, "GET", f"https://api.rawg.io/api/games/{external_id}", params={"key": self.key})
        return self._result(g) if g and g.get("name") else None

    @staticmethod
    def _result(g):
        return ProviderResult(
            external_id=str(g["id"]), title=g["name"], release_date=g.get("released") or None,
            facts={
                "description": g.get("description_raw"),
                "developer": ((g.get("developers") or [{}])[0]).get("name"),
                "publisher": ((g.get("publishers") or [{}])[0]).get("name"),
                "genres": [x["name"] for x in g.get("genres", [])][:50],
                "tags": [x["name"] for x in g.get("tags", [])][:50],
                "aliases": [a for a in (g.get("alternative_names") or []) if isinstance(a, str)][:30],
                "age_rating": ((g.get("esrb_rating") or {}).get("name")),
                "critic_score": g.get("metacritic") if isinstance(g.get("metacritic"), int) else None,
                "user_score": round(g["rating"] * 20) if isinstance(g.get("rating"), (int, float)) and g["rating"] > 0 else None,  # noqa: E501
                "website": g["website"] if str(g.get("website") or "").startswith("https://") else None,
            },
            artwork=[{"kind": "hero", "url": g["background_image"], "priority": 30}]
            if str(g.get("background_image") or "").startswith("https://") else [],
        )
