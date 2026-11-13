# Provider fixtures

Trimmed answers used by `tests/test_providers_p7.py`. Open Library fixtures are synthetic and live in `../openlibrary/` (A22: Open Library answers are not committed). Lists are cut to a few entries; nothing else is edited.

| File | Source | How it was made |
| --- | --- | --- |
| `steam-price-overview-gb.json` | `GET https://store.steampowered.com/api/appdetails?appids=620,440,1&filters=price_overview&cc=gb` | Recorded live, 2026-11-07: a priced app (620), a free app (440, `data: []`) and an unknown app (1, `success: false`) |
| `wikidata-steam-292030.json` | Wikidata SPARQL, items with Steam application ID (P1733) 292030 | Recorded live, 2026-11-07 (CC0). Two GOG values: the game and its expansion pass |
| `wikidata-steam-1794680.json` | Wikidata SPARQL, P1733 = 1794680 | Recorded live, 2026-11-07. An itch.io URL (P7294) |
| `tgdb-bygameid-documented-shape.json` | TheGamesDB API v1 `Games/ByGameID` | **Not recorded**: written from TheGamesDB's published API documentation, because no TheGamesDB key was available. The opt-in live job (`tests/live/`) checks the real shape when `ATLAS_TGDB_API_KEY` is set. |
