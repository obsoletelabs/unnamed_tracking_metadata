"""TheGamesDB (issue #54): the official, keyed API v1 (https://api.thegamesdb.net/).

Access: a key is requested from TheGamesDB (forum); each key has a monthly request allowance that the API reports
back as `remaining_monthly_allowance`. Atlas keeps well under it with a low per-minute budget and the shared
provider cache (a game is fetched once per `provider_ttl_seconds`). Licence: the data is community contributed;
TheGamesDB's terms do not state that it may be redistributed, so its observations are local-only
(`redistributable = False`), like RAWG's and IGDB's. See docs/guide/providers.md.

Genre, developer and publisher come back as numeric ids that need extra lookup calls; this adapter keeps the
allowance for games and leaves those out.
"""

from __future__ import annotations

from typing import Any

from .base import ProviderError, ProviderResult, get_json, parse_release, release_ext, strip_html

API = "https://api.thegamesdb.net/v1"
# TheGamesDB platform ids for the platforms Atlas names (https://api.thegamesdb.net/v1/Platforms)
_PLATFORM = {1: "pc", 4912: "nintendo", 7: "nintendo", 8: "nintendo", 4971: "nintendo", 10: "playstation",
             11: "playstation", 12: "playstation", 4919: "playstation", 4980: "playstation", 13: "playstation",
             14: "xbox", 15: "xbox", 4920: "xbox", 4981: "xbox", 37: "macos", 4914: "linux", 4915: "ios",
             4916: "android"}
_FIELDS = "players,publishers,genres,overview,rating,platform,coop,alternates"


class TheGamesDb:
    name = "tgdb"
    redistributable = False
    requests_per_minute = 20  # a monthly allowance, not a per-second limit: stay low
    media_types = ("game",)

    def __init__(self, api_key: str) -> None:
        self.key = api_key

    def _get(self, client, path: str, **params: Any) -> dict[str, Any]:
        data = get_json(client, "GET", f"{API}/{path}", params={"apikey": self.key, **params})
        if not isinstance(data, dict) or data.get("code") not in (200, None):
            raise ProviderError(f"thegamesdb: {str((data or {}).get('status', 'bad response'))[:200]}")
        return data

    def search(self, client, query):
        data = self._get(client, "Games/ByGameName", name=query, fields="platform")
        games = ((data.get("data") or {}).get("games")) or []
        return [self._result(g, {}) for g in games if isinstance(g, dict) and g.get("game_title")][:10]

    def fetch(self, client, external_id):
        data = self._get(client, "Games/ByGameID", id=external_id, fields=_FIELDS, include="boxart")
        games = ((data.get("data") or {}).get("games")) or []
        game = next((g for g in games if isinstance(g, dict) and str(g.get("id")) == str(external_id)), None)
        if game is None or not game.get("game_title"):
            return None
        return self._result(game, (data.get("include") or {}).get("boxart") or {})

    @staticmethod
    def _result(g: dict[str, Any], boxart: dict[str, Any]) -> ProviderResult:
        gid = str(g["id"])
        day, release = release_ext(parse_release(g.get("release_date")))
        aliases = [a for a in (g.get("alternates") or []) if isinstance(a, str) and a.strip()][:30]
        rating = g.get("rating")
        custom: dict[str, Any] = {}
        if isinstance(g.get("players"), int) and 0 < g["players"] <= 64:
            custom["players_max"] = g["players"]
        return ProviderResult(
            external_id=gid, title=str(g["game_title"])[:500], platform=_PLATFORM.get(g.get("platform")),
            release_date=day, release=release,
            facts={"description": strip_html(g.get("overview")),
                   "aliases": aliases,
                   "age_rating": rating if isinstance(rating, str) and rating and rating != "Not Rated" else None,
                   "features": ["Co-op"] if g.get("coop") == "Yes" else []},
            custom=custom,
            artwork=_boxart(gid, boxart),
        )


def _boxart(gid: str, boxart: dict[str, Any]) -> list[dict[str, Any]]:
    base = str(((boxart.get("base_url") or {}).get("original")) or "")
    if not base.startswith("https://"):
        return []
    art = []
    for i, b in enumerate(((boxart.get("data") or {}).get(gid) or [])[:6]):
        name = str(b.get("filename") or "")
        if b.get("type") == "boxart" and b.get("side") == "front" and name and ".." not in name:
            art.append({"kind": "key_art", "url": base + name, "priority": 40 + i})
    return art
