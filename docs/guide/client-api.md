# Client API (for tools such as Jellyfin and Playnite)

Atlas can be a metadata source inside other tools: the Atlas connectors for **Playnite** and **Jellyfin** (later
Emby and Plex) live in their own repository and talk to Atlas over HTTP only. They are written against a small,
documented, versioned part of the API: **client API v1**. This page is that contract. Everything here is also in the
generated [API reference](reference-api.md).

## What the contract promises

The client API has its own version, separate from the federation protocol and the plugin API. It starts at **1.0**.

1. **Additive only within v1.** New fields, endpoints and query parameters may appear at any time. A client must
   ignore members it does not know.
2. **Vocabularies may grow.** A new media type, artwork kind, edition format or creator role is not a breaking
   change. A client maps a value it does not know to "unknown/other" and never fails.
3. **Ids are permanent.** An Atlas id never changes meaning. A merged id keeps working: the answer is the surviving
   record with `redirected_from` set to the id you asked for. Store the new id.
4. **Breaking changes need `/api/v3`.** `/api/v2` stays served for at least 12 months after v3 ships. A deprecated
   endpoint sends `Deprecation` and `Sunset` headers and a `Link` to its replacement, and `/site` lists it under
   `client_api.deprecations`.
5. **Errors and limits** follow the general [API conventions](reference-api.md): `{"detail": ...}` bodies, `422`
   for invalid input, `429` with `Retry-After` when you are over a limit (600 requests a minute per token by
   default).

Atlas's own test suite enforces item 1 and 2: the operations of the client API (tagged `client` in the OpenAPI
document) are written to `contracts/openapi-client-v1.json`, and a change that removes or narrows anything in it (a
path, a field, an enum value, a type, or a new required parameter) fails the build.

## Discover the node: `GET /api/v2/site`

No token is needed. The `client_api` member says what this node offers:

```json
"client_api": {"version": "1.0", "min_supported": "1.0",
               "capabilities": ["books", "pairing", "etag", "batch"], "deprecations": [],
               "limits": {"batch_items": 200, "anonymous_items": 25}}
```

Read it once per session. Refuse a node whose `version` has a different major number than yours, or that lacks a
capability you need, with a clear message. The separate `contract: 1` member is the general `/api/v2` marker;
connectors do not read it. `limits` (absent on older nodes) gives the ids per batch call and the lower cap on
batch ids and resolve items for a call made without a token.

| Capability | Meaning |
|---|---|
| `books` | The node has the built-in book type (Open Library data, authors, editions, ISBN search). |
| `pairing` | A person can connect your tool with a code ([Pairing](#pairing)). |
| `etag` | `GET /games/{id}` answers `304` to a matching `If-None-Match`. |
| `batch` | `POST /games/batch` reads up to 200 records at once. |
| `public_read` | The client API can be read without a token (the administrator chose so). |

## Authenticate

Send `Authorization: Bearer <token>` with a token that has the `read` scope. Never put a token in a URL. There are
three ways to get one:

* **Pairing** (recommended for tools that run on a server or a TV): see below.
* **A personal token**: the person creates one under **Profile → API tokens** and pastes it into your tool.
* **An administrator's token** (`atlas token --name jellyfin --scopes read`).

When the node lists `public_read`, the client API can also be read anonymously, at 60 requests a minute per
address. Send no `Authorization` header at all in that case: a wrong token is always refused. Without a token a
batch or resolve call carries at most 25 items (`limits.anonymous_items`; `413` beyond).

## Pairing

The device-code flow, without OAuth. The token it produces is a **personal read token** of the person who approved:
it is listed under their **Profile → API tokens**, counts toward their limit of 10 tokens, can be revoked there and
stops working if their account is closed.

1. `POST /api/v2/client/pair` with `{"label": "Jellyfin on nas"}` (no token). The answer:

    ```json
    {"code": "7K3Q-M9PX", "verify_url": "https://atlas.example.org/pair",
     "verify_url_complete": "https://atlas.example.org/pair?code=7K3Q-M9PX",
     "qr_svg": "<svg ...>", "poll_token": "…", "expires_in": 600, "interval": 5}
    ```

    Show the code and the QR code (it encodes `verify_url_complete`) next to each other. Keep `poll_token` secret.
2. The person opens the address while signed in, checks that the device name, software and address are theirs,
   and chooses **Approve**.
3. Poll `POST /api/v2/client/pair/poll` with `{"poll_token": "…"}` every `interval` seconds. `status` is
   `pending`, `slow_down` (you polled too fast: wait the new, longer `interval`), `approved` (with `token`, shown
   once), `denied` or `expired` (start again).

Limits: codes expire after 10 minutes and work once; at most 20 pairings are open on a node at a time, and 3 from one address; five wrong
codes lock the person's approval form for 10 minutes. Both calls count on the anonymous per-address budget.

## Find and read records

| Need | Call |
|---|---|
| Map the ids your host already knows to Atlas | `POST /api/v2/resolve` with `provider_ids`, at most 200 items ([Tracking App](tracking-app.md#resolving-ids-post-resolve) explains the answers) |
| Search | `GET /api/v2/games?q=…&type=…`; for books, `q` may be an ISBN-10 or ISBN-13 and `author=` filters by creator |
| One record | `GET /api/v2/games/{id}` |
| Many records | `POST /api/v2/games/batch` with `{"ids": [...]}` (1-200) |
| Series, DLC, editions | `GET /api/v2/games/{id}/relations` (a book's place in a series is `position`) |
| Images | `GET /api/v2/games/{id}/artwork` |
| Achievements | `GET /api/v2/games/{id}/achievements` (`applicable: false` for books) |
| What changed | `GET /api/v2/changes?feed=local&since=…&epoch=…` |

Every `/games…` path is also served as **`/records…`** (`/api/v2/records`, `/api/v2/records/{id}`,
`/api/v2/records/batch`, …): same handler, same answer. `/games` is not deprecated; use whichever reads better.

**Caching.** `GET /games/{id}` sends `ETag: "<id>-<version>"`. Send it back as `If-None-Match` and an unchanged
record answers `304 Not Modified` with no body. The version moves with every change of the record's own data. The
titles shown in `relations` belong to the related records: refresh those from the related records themselves.

**Batches.** `POST /games/batch` answers one item per id, in your order: `{"requested", "status": "ok" |
"not_found", "game"}`, where `game` is exactly what `GET /games/{id}` returns (merges followed).

**Images.** `primary_by_role` in the artwork answer names the image to show per role, already ranked:
`{"cover": id, "background": id, "icon": id, "logo": id}` (null when the record has none; a book's cover is its
`poster`). Download it from the item's `media_url` when present (immutable), otherwise from `image_url` with your
token. `image_url` may answer `307` to the provider's own host: follow it at most once, **without** the
`Authorization` header, only to `https`, and accept only an `image/*` answer.

**Dates** are partial (`first_release: {"date": "2005-08", "precision": "month"}`). Never invent a day: map a
month or year date to your host's month or year field.

## Identify your tool

Send `User-Agent: atlas-connector-<host>/<version>` (for example `atlas-connector-jellyfin/1.0.0`). Atlas counts
client API requests per connector in `atlas_client_requests_total{client}` (`playnite`, `jellyfin`, `emby`, `plex`,
anything else `other`), so operators can see the load each kind of tool causes.

## Fixtures and test catalogue

Atlas generates the fixtures connectors test against; they are never written by hand.

* `contracts/openapi-client-v1.json` (+ `.sha256`): the client subset of the OpenAPI document,
  `python tools/gen_docs.py --client-openapi`.
* `contracts/client-fixtures/`: recorded answers over a synthetic catalogue (3 games, 3 books, 1 series, 1 merge
  redirect, 1 record of a media type clients do not know, plus a derived record with unknown members), with a
  `MANIFEST.json` and `SHA256SUMS`, `python tools/export_client_fixtures.py`.
* Each release attaches both as `client-fixtures-<version>.tar.gz` (+ `.sha256`), laid out like the connectors'
  `contract/` folder.
* `atlas seed --fixture connectors-v1 [--token]` seeds the same synthetic catalogue into a throwaway node
  (idempotent) and prints the ids as JSON, for integration tests against a running Atlas. The records are invented
  and enter the change log, so it refuses an authority node or a database that already holds records unless you
  pass `--force`. The `--token` it mints is audited (`token_create`, `via: cli_seed`).
* `contracts/client-api-baselines/openapi-client-<version>.json`: a frozen copy of the OpenAPI file of each
  released client-api version, pinned by sha256 in `atlas/client_openapi.py`. The generated file must stay
  additive against every baseline of the current major: `gen_docs.py --client-openapi` refuses to write a break,
  and the tests fail on one, so regenerating cannot hide it. A break needs a new client-api major.

The fixtures and the OpenAPI file are synthetic data and an interface description: CC0, like the rest of the data
Atlas publishes.
