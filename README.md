# Atlas

A semi-distributed **game metadata server**. It merges what many providers and a community say about a game into one canonical record, tells you where every field came from, and keeps working when its upstream is down. It has its own website, an API for apps (notably the [Unnamed Tracking App](https://github.com/obsoletelabs/unnamed_tracking_app_2)), and can sign people in to other software over OpenID Connect.

![A game page](docs/guide/img/02-game.png)

**→ Read the [guide](docs/guide/index.md).** Quick links: [Quick start](docs/guide/quickstart.md) · [Concepts](docs/guide/concepts.md) · [Administering](docs/guide/administering.md) · [Federation](docs/guide/federation.md) · [Sign-in sharing (OIDC)](docs/guide/sso.md) · [Tracking App](docs/guide/tracking-app.md) · [API](docs/guide/reference-api.md) · [Security](docs/guide/security.md)

## What it does

* **Merged records with provenance.** Observations from Steam, IGDB, RAWG, curators and the community are merged per field by trust; every value can be explained, pinned, split or reverted. Matching is conservative: uncertain matches become a reviewable duplicate, never a silent merge.
* **A website** to browse, contribute (reputation-gated proposals with review), discuss, keep lists and follow games, with public profiles and data export/deletion.
* **Admin-configurable without code:** name, colours, logo, menus, Markdown pages, extra typed fields per media type, limits, email, who may sign in how, peers.
* **Distributed:** one program as `authority`, `mirror` or `local` node; a signed change feed; pinned trust; per-node accounts; OpenID Connect between nodes.
* **Artwork and achievements:** downloaded only after safety checks and served from the node; achievement *lists* (never anyone's progress).
* **Email:** verification and password reset, with SMTP delivery or a readable outbox.

## Run it

```sh
cp .env.example .env     # set ATLAS_INITIAL_ADMIN and a bootstrap token
docker compose up -d     # http://127.0.0.1:8000
```

Public authority with PostgreSQL: `docker compose -f compose.authority.yaml up -d` behind a TLS proxy ([Deployment](docs/guide/deployment.md)). Without Docker: `pip install .` → `atlas migrate` → `atlas serve`.

## Status

Tested: merge/resolution, signed replication, the API, auth and sessions, rate limits, migrations (including upgrade from every revision and on PostgreSQL), concurrent writes on real PostgreSQL, the whole website at browser level (CSRF, permissions, escaping, headers), email flows, custom fields, social features, peers, and OIDC (31 tests including forged tokens and algorithm confusion), plus a live check with the Tracking App's own OIDC client stack. Two independent security reviews were done and their findings fixed. CI runs tests (with PostgreSQL), a Docker build and smoke test, and a dependency audit.

**Not verified in this repository:** live calls to RAWG and IGDB (adapters are tested against recorded response shapes; Steam was checked live), and installing the plugin inside a running Tracking App (see [Tracking App](docs/guide/tracking-app.md)). **Not built:** two-factor authentication, structured logs/metrics, key-rotation UI, mirror-to-authority promotion. See [Security](docs/guide/security.md#known-limits-be-aware).

Code: MIT. Contributed metadata: CC0 ([DATA-LICENSE.md](DATA-LICENSE.md)). The earlier demo lives in `legacy/` (`atlas import-legacy` migrates its data).
