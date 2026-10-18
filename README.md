# Atlas

A semi-distributed game metadata server. It merges what many providers say about a game into one canonical record, tells you where every field came from, and keeps working when its upstream is down.

Built for the [unnamed tracking app](https://github.com/obsoletelabs/unnamed_tracking_app_2), but the API is plain REST + OpenAPI.

> v2 is a rewrite. The demo-1 code lives in `legacy/`; `atlas import-legacy` migrates its data. Design notes: [`docs/v2/`](docs/v2).

## How it works

- **Observations** are immutable claims from one source (Steam, IGDB, a curator, a tracking-app plugin), in the tracking app's `SourceObservationV1` format. Personal facts (playtime, ownership) are stripped on arrival and never stored.
- **Canonical records** (`atlas:<id>`) are derived from observations by per-field merge rules (trust priority, vote, union, curator locks). `GET /games/{id}/explain` shows which source won each field.
- **Matching is conservative.** Shared store IDs match automatically; same title + same release year matches; anything weaker opens a review item and creates a separate record. Merges leave redirects, and a wrong merge can be split with `detach`.
- **Nodes** are one codebase with three roles: `authority` (PostgreSQL, publishes a signed change feed), `mirror` (replicates an authority), `local` (SQLite, caches providers, optionally replicates). Replication verifies an Ed25519 signature per entry against a key *you* pinned, and rejects gaps or rewinds. Provider-derived data is cached locally and never published.

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
| `POST /resolve` | batch: store IDs / titles → canonical ids (read-only) |
| `POST /observations` | submit a batch (contribute) |
| `GET /games?q=`, `GET /games/{id}`, `/explain` | read |
| `GET /providers/{p}/search`, `POST /providers/{p}/fetch` | pull from a provider through the cache |
| `POST /games/{id}/merge`, `/observations/{id}/detach`, `PUT /games/{id}/locks/{field}`, `/review` | curation |
| `GET /sync/info`, `/sync/changes` | federation |
| `/tokens` | admin |

Scopes: `read`, `contribute`, `curate`, `sync`, `admin`. Interactive docs at `/docs` in dev.

## Status and limits

Tested: merge/resolution, signed replication, API, auth, rate limits, migrations, concurrent writes on real PostgreSQL. **Not yet verified in this repo:** the Dockerfile/compose files, live calls to Steam/RAWG/IGDB (adapters are tested against recorded response shapes only), and the tracking-app plugin (see [`docs/v2/app-integration.md`](docs/v2/app-integration.md)). Writes are serialised by a global lock, which is fine for a metadata service and the first thing to revisit under heavy ingest.

Code: MIT. Contributed metadata: CC0 ([DATA-LICENSE.md](DATA-LICENSE.md)).
