# Metadata providers

Atlas imports metadata from a provider only through that provider's own API, under its published terms. It never
scrapes a store's web pages, and it never works around a login, a key, a rate limit or a robots rule. This page
lists every source considered (issue #54): what each needs, what its terms allow, and where it lives.

Switch providers on and enter their keys in **Admin → Integrations**. Environment variables are in
[Environment variables](reference-env.md#providers).

## Access matrix

This summary of each provider's public documentation was written in November 2026. No lawyer has reviewed it, and terms change. Read a source's current terms before you enable it on a public node. The Open Library, Wikidata and Steam storefront API shapes were checked against the live services; TheGamesDB's comes from its published API documentation, because no key was available.

| Source | Access | Licence / redistribution | Fields | In Atlas |
| --- | --- | --- | --- | --- |
| Steam storefront | Public, no key (`storesearch`, `appdetails`). Valve throttles `appdetails` to about 200 requests per 5 minutes per address. | The Steam Web API terms allow use but grant no redistribution of store data. | Description, developer, publisher, genres, features, languages, Metacritic score, crossplay, artwork, global achievement percentages; prices (see [Prices](prices.md)) | Built in, on by default (`ATLAS_STEAM_ENABLED`). Kept on this node only. |
| Steam Web API | Key (`ATLAS_STEAM_API_KEY`) | As above | Achievement names, descriptions and icons | Built in |
| IGDB | Twitch client id and secret; 4 requests a second | IGDB/Twitch terms: no redistribution | Description, genres, themes, scores, aliases, release dates per platform and region, cross-store ids | Built in. Kept on this node only. |
| RAWG | Key; attribution required | RAWG terms: no redistribution of the database | Description, genres, tags, scores, aliases | Built in. Kept on this node only. |
| **TheGamesDB** | Official API v1 with a key requested from TheGamesDB. Each key has a **monthly allowance**; answers report what is left. | Community data. The terms do not grant redistribution, so Atlas treats it as not redistributable. | Overview, alternate titles, player count, co-op, rating, platform, front box art | **Built in** (`ATLAS_TGDB_API_KEY`, `tgdb`). Budget 20 requests a minute, cached for `ATLAS_PROVIDER_TTL_SECONDS`. Genre, developer and publisher come back as ids that need extra lookups, so the adapter leaves them out. |
| **Open Library** (books) | Official JSON API, no key. Send an identifying User-Agent and stay around 1 request a second. Use the monthly dumps for bulk loads. | Catalogue data is **CC0**: redistributable. Covers by cover id from covers.openlibrary.org. | Title, first publication year, author, publisher, subjects, description, cover | **Built in**, off by default (`ATLAS_OPENLIBRARY_ENABLED`). Imports **works** (`OL…W`) as records of the `book` media type, which an administrator must create first (Admin → Media types). Offered only for `book` records. |
| **Wikidata** | Public SPARQL endpoint, no key. Send a User-Agent with contact details. 60 s of query time per minute and 5 parallel queries per client. | **CC0**: redistributable. | Cross-store ids only: Steam (P1733), GOG (P2725), Epic (P6278), TheGamesDB (P7622), itch.io (P7294), Open Library (P648) | **Built in**, off by default (`ATLAS_WIKIDATA_ENABLED`). Used **only to suggest** store links, never as a metadata source (see below). |
| GOG | No published API terms. The catalogue endpoints the GOG Galaxy client uses (`api.gog.com`, `catalog.gog.com`) are public but undocumented, and GOG does not grant their use to third parties. | Unknown | — | **No adapter.** Ids (`gog:<slug>`) are suggested through Wikidata, linked by members, or sent by plugins. **Owner decision:** an adapter on the undocumented endpoints needs GOG's written permission, or an owner decision to accept the risk. If that happens, it should be a default-off adapter here. |
| Epic Games Store | No public catalogue API. The store's GraphQL endpoint is internal and behind bot protection. Epic's terms forbid scraping and automated access to the store. | — | — | **No adapter, and none planned.** Ids (`epic:<slug>`) are suggested through Wikidata or linked by hand. This changes only if Epic publishes a catalogue API. |
| itch.io | An official server-side API with a key, but it covers only the key holder's own games and purchases. There is no catalogue search. | — | — | **No adapter.** A per-user library import belongs in a Tracking App plugin, which holds the user's own key. Ids are `itch:<creator>/<game>` and link to `https://<creator>.itch.io/<game>`. |
| Google Books | Official API; a key is recommended. The terms restrict caching and storing results and require attribution. | Not redistributable; limited storage | Books | **Not built.** Open Library covers books without these limits. Revisit only if Open Library coverage is not enough. |

Where a provider lives:

* **Atlas** holds sources that are shared, cached and deduplicated for every client: Steam, IGDB, RAWG,
  TheGamesDB, Open Library, and Wikidata for link suggestions.
* **A Tracking App plugin** (plugin API v1) holds anything tied to one user's own account or key, such as an
  itch.io or GOG library import.

### Follow-ups

* **GOG adapter:** needs GOG's permission or an owner decision (see the GOG row).
* **TheGamesDB genres, developers and publishers:** the `/Genres`, `/Developers` and `/Publishers` lookups are
  cacheable lists. Fetch them once a day if the monthly allowance allows it.
* **A built-in `book` media type:** today an administrator creates it. Making it built in changes the
  vocabulary every node and the Tracking App share, so it is an owner decision.

## Store link suggestions (Wikidata)

With Wikidata switched on, **Link a store** on a game page first looks up the ids the game already has (at most
two of them per request) and suggests the GOG, Epic, itch.io, TheGamesDB, Steam or Open Library ids that
Wikidata lists for the same item. Each suggestion shows where it came from and links to the store page, so a
member can check it before proposing the link.

A store with exactly one candidate is marked *Likely match*. When Wikidata lists several (for example, a game and
its expansion pass both carry the same Steam id), none of them is marked. Every link is still a reviewed
proposal, and an id already used by another game is refused.

Name search then adds matches from every provider configured for the record's media type. A book is never
searched in a game store, and a game is never searched in Open Library.

IGDB and RAWG ids are not taken from Wikidata. Wikidata stores their slugs, while Atlas stores their numeric
ids.

## Live checks

`tests/live/` checks every adapter against the real APIs. It is opt-in and never runs on pull requests. See
[Testing](testing.md#live-provider-checks).
