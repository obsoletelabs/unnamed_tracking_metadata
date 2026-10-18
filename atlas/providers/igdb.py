"""IGDB adapter. Shapes follow IGDB's public v4 docs; not exercised against the live API in CI."""

from __future__ import annotations

import time

from .base import ProviderResult, epoch_to_date, get_json

# external_games.category values documented by IGDB
_EXTERNAL = {1: "steam", 5: "gog", 26: "epic", 11: "microsoft"}
_FIELDS = (
    "cover.image_id,screenshots.image_id,artworks.image_id,name,first_release_date,summary,genres.name,themes.name,platforms.name,"
    "game_modes.name,alternative_names.name,aggregated_rating,rating,"
    "involved_companies.developer,involved_companies.publisher,involved_companies.company.name,"
    "external_games.category,external_games.uid"
)


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
        return ProviderResult(
            external_id=str(g["id"]), title=g["name"], release_date=epoch_to_date(g.get("first_release_date")),
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
