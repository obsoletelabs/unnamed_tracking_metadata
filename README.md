# Atlas

A semi-distributed game metadata server. It merges what many providers say about a game into one canonical record, tells you where every field came from, and keeps working when its upstream is down.

Built for the [unnamed tracking app](https://github.com/obsoletelabs/unnamed_tracking_app_2), but the API is plain REST + OpenAPI.

> v2 is a rewrite. The demo-1 code lives in `legacy/`; `atlas import-legacy` migrates its data. Design notes: [`docs/v2/`](docs/v2).

## How it works

- **Observations** are immutable claims from one source (Steam, IGDB, a curator, a tracking-app plugin), in the tracking app's `SourceObservationV1` format. Personal facts (playtime, ownership) are stripped on arrival and never stored.
- **Canonical records** (`atlas:<id>`) are derived from observations by per-field merge rules (trust priority, vote, union, curator locks). `GET /games/{id}/explain` shows which source won each field.
- **Matching is conservative.** Shared store IDs match automatically; same title + same release year matches; anything weaker opens a review item and creates a separate record. Merges leave redirects, and a wrong merge can be split with `detach`.
- **Nodes** are one codebase with three roles: `authority` (PostgreSQL, publishes a signed change feed), `mirror` (replicates an authority), `local` (SQLite, caches providers, optionally replicates). Replication verifies an Ed25519 signature per entry against a key *you* pinned, and rejects gaps or rewinds. Provider-derived data is cached locally and never published.

## The website

Atlas ships its own site (server-rendered, no build step, dark/light automatic): public browsing of games, artwork, achievements and provenance; accounts and a proposal workflow; reviewer tools; admin. The tracking app only pulls metadata through the API.

- **Contribute:** anyone can register (`ATLAS_REGISTRATION=open|closed`) and propose edits, new games, artwork and achievements. The administrator is the account named by `ATLAS_INITIAL_ADMIN` (if unset, the first account to register; production with open registration refuses to start without it). `atlas make-admin <username>` promotes someone later.
- **Reputation:** proposals a reviewer approves earn reputation (self-applied changes earn none, and are capped per day); from `ATLAS_AUTO_APPROVE_REPUTATION` (default 50) changes apply immediately and can still be reverted; from `ATLAS_CURATOR_REPUTATION` (default 500) a user can review others. Rejections and reverts cost reputation. Per-user pending and daily limits apply.
- **Review:** diff view of each proposal, a possible-duplicates queue with one-click merge, split-off of wrongly merged source records, field pinning, ordered revert.
- **Artwork** is shown only after the server has downloaded and verified it (pinned public IP, redirect revalidation, size and pixel caps, decode check); pages never hotlink third-party images.
- Hardened by default: CSRF on every form, HttpOnly SameSite session cookies, a strict CSP with no inline script or style, login/registration throttling.

## Run it

```sh
cp .env.example .env        # set ATLAS_BOOTSTRAP_ADMIN_TOKEN (32+ random chars)
docker compose up -d        # local node, SQLite, http://127.0.0.1:8000
```

Authority with PostgreSQL: `docker compose -f compose.authority.yaml up -d` behind a TLS proxy.

Without Docker: `uv sync && uv run atlas serve`. CLI: `atlas migrate | token | node | sync | import-legacy`.

### Federation in three steps

1. On the authority: `atlas node` prints its node id and public key.
2. Create a token with scopes `read,sync` and give it to the mirror.
3. On the mirror set `ATLAS_ROLE=mirror`, `ATLAS_UPSTREAM_URL`, `ATLAS_UPSTREAM_TOKEN` and `ATLAS_TRUSTED_KEYS={"<node_id>":"<key>"}`.

## API (`/api/v2`)

| | |
|---|---|
| `POST /resolve` | batch (max `ATLAS_MAX_RESOLVE`, default 200): store IDs / titles → canonical ids (read-only) |
| `POST /observations` | submit a batch (contribute) |
| `GET /games?q=`, `GET /games/{id}`, `/explain` | read |
| `GET /providers/{p}/search`, `POST /providers/{p}/fetch` | pull from a provider through the cache |
| `POST /games/{id}/merge`, `/observations/{id}/detach`, `PUT /games/{id}/locks/{field}`, `/review` | curation |
| `GET /sync/info`, `/sync/changes` | federation |
| `/tokens` | admin |

Scopes: `read`, `contribute`, `curate`, `sync`, `admin`. Interactive docs at `/docs` in dev.

Trust model: source names that carry weight in the merge (`steam`, `igdb`, `curator`, ...) are reserved. Only a token with `curate` (or the node itself, via provider fetches) may use them; everyone else submits under their own name at default trust and cannot register IDs in a reserved namespace.

## Status and limits

Tested: merge/resolution, signed replication, API, auth, rate limits, migrations, concurrent writes on real PostgreSQL, the whole website (browser-level checks of CSRF, permissions, escaping, headers) and the Docker image. **Not yet verified in this repo:** live calls to Steam/RAWG/IGDB (adapters are tested against recorded response shapes only), and the tracking-app plugin (see [`docs/v2/app-integration.md`](docs/v2/app-integration.md)). Writes are serialised by a global lock, which is fine for a metadata service and the first thing to revisit under heavy ingest.

Code: MIT. Contributed metadata: CC0 ([DATA-LICENSE.md](DATA-LICENSE.md)).
