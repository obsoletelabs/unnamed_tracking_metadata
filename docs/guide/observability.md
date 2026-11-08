# Logs, metrics and service objectives

## Enable telemetry

Both features are opt-in and require a restart:

```dotenv
ATLAS_JSON_LOGS=true
ATLAS_METRICS_ENABLED=true
```

## Liveness and readiness

The two probes answer different questions; point each orchestrator check at the right one.

| Endpoint | Question | Touches | Result |
| --- | --- | --- | --- |
| `GET /api/v2/health` | Is the process serving at all? Restart it if not. | nothing external | always `200 {"status":"ok"}` plus `media: ok\|unwritable` |
| `GET /api/v2/ready` | Should this instance receive traffic now? Hold traffic if not. | database, migration table, media directory | `200 {"status":"ready"}`, `200 {"status":"degraded"}` or `503 {"status":"not_ready"}` |

`/ready` returns **503 `not_ready`** when the database does not answer a trivial query, or when migrations are pending
(for example during a rolling upgrade with `ATLAS_AUTO_MIGRATE=false`). It returns **200 `degraded`** when
the database and schema are fine but the artwork directory (`ATLAS_MEDIA_DIR`) is not writable: the instance
still serves catalogue reads and writes, so it stays in rotation, but artwork cannot be cached and an
operator should look (`atlas_ready_check{check="media"}` is 0). If the migration state cannot be determined
(`unknown`) the instance stays ready, exactly as before, and `atlas_schema_state{state="unknown"}` shows it.
Neither probe depends on external providers, responses carry `Cache-Control: no-store`, and the public bodies
stay minimal on purpose (no paths, versions or error text). An administrator token gets the detail from
`GET /api/v2/status`: `checks.database` (`ok\|unavailable`), `checks.schema` (`current`, `behind`
or `unknown`), `checks.media`, `checks.readiness` and `checks.provider_failures`. Use the liveness probe
for restarts only: wiring `/ready` to a restart policy would restart healthy pods during a slow migration.

With metrics enabled, `GET /metrics` accepts a dedicated token with the **metrics** scope
or an administrator API token. A read, sync or personal token cannot scrape it. When disabled,
the endpoint returns 404. Create a credential with `atlas token --name prometheus --scopes metrics`
and store its output in your secret manager. Do not put it in a scrape URL or repository.

Example Prometheus job (the file contains only the token, readable by Prometheus):

```yaml
scrape_configs:
  - job_name: atlas
    scheme: https
    static_configs:
      - targets: [atlas.example.org]
    authorization:
      type: Bearer
      credentials_file: /run/secrets/atlas-metrics-token
```

## Exported measurements

| Metric | Meaning |
| --- | --- |
| `atlas_uptime_seconds` | Current process uptime |
| `atlas_http_requests_total` | Requests by bounded method, route template and status. Status `499` is a client that left before any response was started; it is never counted as a 500 |
| `atlas_http_request_duration_seconds` | Request duration histogram, sum and count, in seconds. Buckets (`le`): 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, +Inf |
| `atlas_http_requests_in_flight` | Requests running now, including the scrape that reads it (an idle server reads 1). Released in a `finally`, so a cancelled or failed request is not leaked |
| `atlas_http_request_errors_total` | Requests that did not succeed, by `class`: `client_error` (4xx), `server_error` (a 5xx a handler returned), `unhandled_exception` (an exception escaped; the client sees a generic 500) or `client_disconnect`. One class per request, never more: a disconnect takes precedence over a status or exception the same request also produced. All four are always exported |
| `atlas_ready` | One when `/api/v2/ready` would answer 200 (`ready` or `degraded`), zero when it would answer 503 |
| `atlas_ready_check` | One series per dependency, `check` is `database`, `schema` or `media`; zero is the failing one. `media` at zero is `degraded`, not an outage |
| `atlas_schema_state` | One-hot over `state` = `current`, `behind`, `unknown` |
| `atlas_collector_success` | One per scrape collector (`collector` is `readiness`, `feed`, `outbox`, `peers`, `sync_upstreams`, `providers`, `backup`, `database`, `catalog` or `media_storage`, plus `group` on a node in an owner group); zero means that collector failed and its series are absent from this scrape (unavailable, not zero) |
| `atlas_collector_duration_seconds` | How long that collector took in this scrape (a gauge, not a histogram) |
| `atlas_feed_head` | This node's own signed feed head |
| `atlas_sync_cursor` | Applied position per independently trusted origin |
| `atlas_sync_head` | Highest transport-reported head observed for an origin |
| `atlas_peer_lag_entries` | Entries between that origin's observed head and applied cursor, labeled per peer |
| `atlas_sync_upstream_head` | Named heads per origin: `head` is `observed`, `downloaded`, `verified` or `applied` |
| `atlas_sync_parked_entries` | Entries held back behind a parked one (they need a newer release) per origin |
| `atlas_sync_quarantined_entries` | Known entries that failed to apply and are retried, per origin |
| `atlas_sync_refused` | One when this node refused the origin's protocol |
| `atlas_sync_rewound` | One when the origin advertised fewer signed entries than this node verified, or changed one (rewind or fork); never reported in sync while set |
| `atlas_sync_backlog_full` | One when parked backlog reached `ATLAS_SYNC_MAX_PARKED_BACKLOG` and downloading paused |
| `atlas_sync_in_sync` | One only when every head the origin reported is verified and applied, with nothing parked, quarantined or refused |
| `atlas_peer_last_success_timestamp_seconds` | Last successful pull per enabled peer; zero before first success |
| `atlas_peer_error` | One when the last peer pull failed |
| `atlas_sync_waiting_entries` | Entries waiting for a game this node does not hold yet, per origin (not failures; applied when the game arrives) |
| `atlas_sync_waiting_records` | Games those waiting entries wait for, per origin |
| `atlas_sync_open_failures` | Open entries per origin that are not applied: waiting plus quarantined |
| `atlas_sync_woken_total` | Arrivals of a game that applied waiting entries, per origin and `trigger` (`record`, `obs`, `bridge` for an `atlas-record` observation, `detach`) |
| `atlas_peer_status` | One for the peer's current status (`status` is `paused`, `not_contacted`, `needs_you`, `unreachable`, `empty`, `catching_up`, `caught_up_waiting` or `caught_up`), zero for the others, per peer |
| `atlas_feed_records` | Authority only: live games by `state`: `introduced` to followers, `not_federated` (never introduced), `missing` (named by a signed entry but never introduced: `atlas feed introduce --missing` would publish them) |
| `atlas_outbox_messages` | Pending, failed and sent email counts; failed deliveries may be retried |
| `atlas_provider_failures_total` | Provider fetch failures in this process |
| `atlas_backup_enabled` | Whether scheduled archive storage is configured |
| `atlas_backup_last_success_timestamp_seconds` | Newest archive timestamp in the configured directory; zero if absent |

### More measurements (#69-#76)

Every family below is recorded by the process that did the work (except where it says *collector*: those are read at
scrape time). Types, units and label values are fixed in `atlas/instruments.py`; each family also carries a `# HELP`
line. Counters reset when the process restarts (unless `ATLAS_METRICS_DIR` is set, see below); `rate()` and
`increase()` handle that.

**Service and scrape (#69)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_http_response_bytes_total` | counter (bytes) | `route` (same budget as requests), `kind` = `html json image text other` | Response body bytes the application sent, measured at the ASGI layer (before any proxy compression). A client that leaves early counts what was sent |
| `atlas_collector_refresh_duration_seconds` | histogram (s) | `collector`, `outcome` = `ok error` | Time each collector took; for the cached collectors only when they actually refresh. Buckets 0.05 … 900 |
| `atlas_collector_cache_age_seconds` | gauge (s, collector) | `collector` = `catalog media_storage` | Age of the cached result this scrape served |
| `atlas_metrics_workers` | gauge (collector) | none | Only with `ATLAS_METRICS_DIR`: worker snapshots with a heartbeat in the last 120 s |

Client disconnects are `atlas_http_request_errors_total{class="client_disconnect"}` (and status `499`), never a 500.

**Providers (#71)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_provider_calls_total` | counter | `provider`, `operation` = `search fetch achievements achievement_stats`, `outcome` = `ok empty cache_hit negative_cache_hit stale_served error unavailable` | One per **logical** lookup, however it was answered. `unavailable`: held back (circuit open or budget exhausted) with nothing cached |
| `atlas_provider_cache_requests_total` | counter | `provider`, `result` = `hit negative_hit miss stale` | The provider cache (`ATLAS_PROVIDER_TTL_SECONDS`, `…_NEGATIVE_TTL_SECONDS`). `stale`: an expired row served because the provider failed or was held back |
| `atlas_provider_requests_total` | counter | `provider`, `outcome` = `ok http_429 http_4xx http_5xx timeout transport invalid_payload` | Upstream HTTP **attempts**, including IGDB's token request; one lookup can make several |
| `atlas_provider_request_duration_seconds` | histogram (s) | `provider`, `outcome` | Upstream latency, timeouts included. Buckets as for HTTP requests |
| `atlas_provider_backoff_total` | counter | `provider`, `reason` = `circuit_open budget_exhausted circuit_tripped` | Calls this process held back, and the failures that opened the circuit (5 in a row open it for 60 s) |
| `atlas_provider_rate_limit_remaining` | gauge | `provider` | Only when the provider's last response carried `X-RateLimit-Remaining` or `RateLimit-Remaining`. Absent means unknown, not zero |
| `atlas_provider_rate_limit_reset_timestamp_seconds` | gauge (Unix s) | `provider` | From `X-RateLimit-Reset` (a Unix time, or seconds when small), `RateLimit-Reset` or `Retry-After` (seconds). Absent when never reported |

Atlas does **not retry** provider calls: a failure is answered from the stale cache when there is one, otherwise it
fails, and repeated failures open the circuit. That is why there is no retry counter; `atlas_provider_backoff_total`
shows the backoff decisions instead. Steam, RAWG and IGDB do not document quota headers today, so the quota gauges stay
absent for them unless an upstream starts sending them. `provider` values are the configured provider names (at most
16; any further one is folded into `other`). `atlas_provider_failures_total` is kept for compatibility.

**Federation (#70)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_federation_batches_total` | counter | `follow` = `own relayed`, `outcome` = `ok empty timeout transport_error http_error untrusted refused rewound fork verification_failed backlog_full error` | One per page pulled from a peer. `refused`: protocol negotiation failed; `untrusted`: unpinned or mismatching key |
| `atlas_federation_batch_duration_seconds` | histogram (s) | `stage` = `fetch verify apply` | `fetch`: the info and changes requests; `verify`: signatures, sequence and retention; `apply`: in-order apply and due retries. `verify`/`apply` only for pages that committed |
| `atlas_federation_entries_total` | counter | `result` = `received retained duplicate applied noop parked quarantined waiting` | Entries in committed pages: received, retained (new to this node), duplicate (already retained, identical: replay), and what applying them did |
| `atlas_federation_received_bytes_total` | counter (bytes) | `request` = `info changes` | Response bytes of this node's sync requests |
| `atlas_federation_verification_failures_total` | counter | `reason` = `signature untrusted_key key_mismatch rotation sequence kind fork rewind protocol` | Pages refused before anything was applied |

These counters carry **no peer or origin label**: per-peer state stays in the existing `atlas_peer_*` and
`atlas_sync_*` gauges (one series set per configured peer or pinned origin). Outgoing feed bytes are
`atlas_http_response_bytes_total{route="/api/v2/sync/changes"}` on the serving node. Pending work is the existing
`atlas_sync_parked_entries`, `atlas_sync_quarantined_entries` and `atlas_sync_waiting_entries`. Nothing here changes a
trust decision; observed heads remain unsigned hints, and `ok` pulls with zero lag still do not prove an unreachable
authority has nothing newer.

**Catalogue quality (#72)** — *collector `catalog`, cached for `ATLAS_METRICS_CACHE_SECONDS` (default 300 s)*

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_catalog_entities` | gauge | `media_type` = built-in types (`game anime movie …`) or `custom`, `state` = `live merged` | Records |
| `atlas_catalog_field_coverage` | gauge | `media_type`, `field` = `release_date artwork external_ids developer publisher achievements`, `state` = `present missing` | Live records with and without the field. `present + missing` = live records of that type. `achievements` is exported for games only (not applicable elsewhere) |
| `atlas_catalog_relationships` | gauge | `type` = the C6 relation types or `other`, `state` = `active ended` | Relations |
| `atlas_catalog_achievement_definitions` | gauge | `state` = `active unobtainable unknown retired` | Catalogue achievement definitions (never unlocks or progress) |
| `atlas_catalog_review_pending` | gauge | `queue` = `match_review proposal field_conflict` | Open match reviews, pending proposals and fields with conflicting sources |
| `atlas_enrichment_operations_total` | counter | `operation` = `provider_import`, `outcome` = `created matched review not_found failed` | Imports of a provider record into the catalogue |

Admin-defined media types are one `custom` group; their names are never label values. A description field is not
measured: this release has no structured description to count. No player identity or personal progress is exported.

**Media and image cache (#73)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_media_operations_total` | counter | `kind` = `artwork achievement_icon`, `operation` = `fetch store`, `outcome` = `ok deferred storage_error error` or a C12 `media_error` code | `fetch` downloads (from the delivering peer first, then the origin) and decode-validates; `deferred` is this node's own per-host limit, not a failure |
| `atlas_media_operation_duration_seconds` | histogram (s) | `kind`, `operation` | Fetch and store latency |
| `atlas_media_transfer_bytes_total` | counter (bytes) | `source` = `origin peer` | Image bytes downloaded, valid or not |
| `atlas_media_cache_requests_total` | counter | `route` = `image media`, `result` = `hit redirect miss refused` | `image`: `/api/v2/artwork/{id}/image`; `media`: `/media/{digest}` on the API and the site. `refused`: the source's policy does not serve it here |
| `atlas_media_cache_evictions_total` | counter | `reason` = `manual` | Images forgotten by *Clear cache*; Atlas has no automatic expiry or size-based eviction |
| `atlas_media_storage_present` | gauge (collector) | none | 1 when `ATLAS_MEDIA_DIR` exists; when 0 the usage series below are absent (unknown, not zero) |
| `atlas_media_storage_bytes`, `atlas_media_storage_files` | gauge (collector, cached) | none | Bytes and files under `ATLAS_MEDIA_DIR` as **this instance** sees it: per node, or the shared total when replicas mount one volume (then use `max`, not `sum`) |
| `atlas_media_storage_scan_truncated` | gauge (collector) | none | 1 when the walk stopped at 200 000 files (the byte count is then a lower bound) |
| `atlas_media_artwork` | gauge (collector, cached) | `state` = `total cached failing` | Artwork rows |

Served image bytes are `atlas_http_response_bytes_total{kind="image"}`. Atlas answers image requests with the full file
(no 304 handling of its own), so a CDN or proxy cache in front is where conditional revalidation shows up. There is no
storage budget setting, so no budget series is exported.

**Database and search (#74)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_database_query_duration_seconds` | histogram (s) | `operation` = `select insert update delete other`, `outcome` = `ok error` | Statement execution time from the first SQL keyword only (never SQL text or values). Buckets 0.0005 … 5 |
| `atlas_database_transactions_total` | counter | `outcome` = `commit rollback` | Transactions ended (savepoints are not counted) |
| `atlas_database_errors_total` | counter | `class` = `lock_timeout deadlock serialization connection integrity other` | Statement errors. SQLite's `database is locked` is `lock_timeout`; PostgreSQL classes come from SQLSTATE (40P01, 40001, 55P03/57014, 08xxx) |
| `atlas_database_pool_connections` | gauge (collector) | `state` = `checked_out idle overflow` | The SQLAlchemy queue pool of **this process**; absent for a pool without such numbers |
| `atlas_database_pool_size` | gauge (collector) | none | Configured pool size |
| `atlas_search_duration_seconds` | histogram (s) | `mode` = `title filter browse`, `outcome` = `ok invalid error` | Catalogue search (`/api/v2/games`, the site search): `title` with a title query, `filter` with release-date filters only, `browse` with neither |
| `atlas_search_results` | histogram (records) | `mode` | Records returned per page; buckets 0, 1, 5, 10, 25, 50, 100 (`le="0"` counts zero-result searches) |

Connection **wait** time is not measured: SQLAlchemy has no event before a pool checkout, and timing it would mean
replacing the pool. Watch `atlas_database_pool_connections{state="checked_out"}` against `atlas_database_pool_size`
instead. There is no search index lifecycle (search runs on indexed columns), so there is no index-operation counter.
Measured overhead: see the pull request for this change (a representative catalogue workload; the per-statement cost is
two `perf_counter()` calls and one histogram update).

**Background work, mail and recovery (#75)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_background_operations_total` | counter | `operation` = `housekeeping price_purge decision_prune retired_purge media_cache mail_delivery mail_purge backup sync_round scheduled_introduce group_round`, `outcome` = `ok error skipped` | Runs of each task in this process. `skipped`: `backup` not due yet, `media_cache` with caching off |
| `atlas_background_operation_duration_seconds` | histogram (s) | `operation` | Run time. Buckets 0.05 … 900 |
| `atlas_background_schedule_delay_seconds` | histogram (s) | `loop` = `housekeeping media_cache mail backup sync` | How much later than planned a loop woke up (thread and CPU contention). The owner-group loop wakes on events and is not measured |
| `atlas_mail_deliveries_total` | counter | `outcome` = `sent failed`, `class` = `none transient permanent auth connection protocol invalid` | SMTP attempts. Retries are separate attempts; `atlas_outbox_messages` stays the durable state |
| `atlas_mail_oldest_pending_timestamp_seconds` | gauge (Unix s, collector) | none | Creation time of the oldest unsent message (including failed ones still retried). **Absent when the queue is empty** |
| `atlas_backup_last_verified_timestamp_seconds` | gauge (Unix s, collector) | none | Last archive verified against its manifest (scheduled backups, `atlas backup drill`); zero if never. Stored in the database, so every worker reports it |
| `atlas_backup_last_restore_drill_timestamp_seconds` | gauge (Unix s, collector) | none | Last passing `atlas backup drill` (an isolated restore with row counts checked); zero if never |

Archive present (`atlas_backup_last_success_timestamp_seconds`), archive verified and restore drill passed are three
different facts and are exported separately. None of them proves the recovery time objective; only a timed full
rehearsal does. Metric tests never send mail or restore into a live database.

**Identity, consent, tokens and authorization (#76)**

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `atlas_identity_flows_total` | counter | `flow` = `password oidc_signin oidc_link`, `outcome` = `ok bad_credentials rate_limited provider_cancelled state_invalid state_expired browser_mismatch provider_refused verification_failed link_conflict not_allowed no_account registration_closed account_unavailable` | One terminal outcome per sign-in or link attempt. `provider_cancelled` (the user or provider stopped) is separate from failures. `verification_failed` includes DNS errors and timeouts reaching the provider |
| `atlas_oidc_consent_decisions_total` | counter | `decision` = `allowed denied invalid_request` | Atlas as OpenID provider: the consent screen. `invalid_request`: refused before consent (unknown client, redirect, PKCE or scope) |
| `atlas_oidc_stage_duration_seconds` | histogram (s) | `stage` = `authorize token userinfo callback`, `outcome` = `ok error` | Stage latency |
| `atlas_token_operations_total` | counter | `token` = `authorization_code access_token`, `operation` = `issue exchange validate revoke`, `outcome` = `ok invalid expired reused redirect_mismatch pkce_failed account_unavailable client_disabled client_auth_failed unsupported_grant` | OpenID provider token lifecycle |
| `atlas_authorization_decisions_total` | counter | `scope` = `read contribute curate sync metrics admin`, `decision` = `allowed denied`, `reason` = `ok missing_token insufficient_scope rate_limited` | API token checks, by the scope the endpoint requires |
| `atlas_auth_rate_limit_decisions_total` | counter | `operation` = `api login register password_reset email_verification mfa oidc_token oidc_callback group import account`, `decision` = `allowed limited` | Every rate-limiter decision, by the operation class its key belongs to (the key, which holds addresses and ids, is never exported) |

There is no "stale consent" outcome: Atlas asks for consent on every authorization and stores none to go stale. No
subject, email, claim, client id or redirect address is ever a label.

### Workers and replicas

Request, provider and every other process-recorded counter above are **per process** unless you set
`ATLAS_METRICS_DIR`.

* **One process per instance** (the container default): scrape each instance on its own address (a Kubernetes
  `PodMonitor`, or one static target per instance). Never scrape through a load balancer that picks a random instance:
  each scrape would see a different process's counters and `rate()` becomes meaningless.
* **Several worker processes behind one address** (for example `uvicorn --workers 4`): set `ATLAS_METRICS_DIR` to a
  private directory the workers share (same host, writable by Atlas only). Each worker writes its counters and histograms
  there every 5 seconds and on every scrape; whichever worker answers `/metrics` sums them all. Counters stay monotonic
  across worker restarts: a worker silent for 120 s is folded into `retired.json` instead of dropped.
  `atlas_http_requests_in_flight` and the provider quota gauges count live workers only. `atlas_metrics_workers` shows
  how many are live. The directory holds only metric names, the bounded label values above and numbers. A worker's
  last 5 seconds of counts can be lost if it is killed without shutting down.

Aggregate across instances with `sum by (...)` over **counters, histograms and `in_flight`**. Do **not** add up the
**database-derived gauges** (`atlas_feed_head`, `atlas_outbox_messages`, `atlas_mail_oldest_pending_*`,
`atlas_sync_*`, `atlas_peer_*`, `atlas_schema_state`, `atlas_catalog_*`, backup timestamps, and
`atlas_media_storage_*` on a shared volume): every instance reads the same data, so use `max by (...)`.
`atlas_ready`, `atlas_ready_check` and `atlas_database_pool_*` are per instance on purpose. A scrape request appears in
request counters on the following scrape.

### Label budget and scrape cost

Every label value comes from a fixed set in code, never from a request:

| Label | Values | Bound |
| --- | --- | --- |
| `method` | `GET POST PUT PATCH DELETE OPTIONS HEAD OTHER` | 8 |
| `route` | the matched route **template** (`/game/{cid}`), `unmatched` for anything that matched no route (including static files and scanners), or `other` | at most 512 distinct, then folded into `other` |
| `status` | the response status (100-599), `499` for a client that left first, `0` for an invalid value | what the application can return |
| `class`, `check`, `state`, `collector`, `head`, `kind`, `operation`, `outcome`, `result`, `reason`, `stage`, `mode`, `follow`, `source`, `request`, `media_type`, `field`, `type`, `queue`, `loop`, `flow`, `decision`, `token`, `scope`, `route` (media) | fixed names listed above | constant; `tests/test_metrics_catalogue.py` computes the worst-case series count of every family |
| `peer_id`, `node_id` | rows an administrator configured and nodes this node pins | one series set per configured peer or origin; keep these lists small |
| `provider` | configured provider names | at most 16 per process, then `other` |

No label ever carries an account, client, email, IP, token, URL, query text, title, error message or plugin
name. Do not add a label called `job`: Prometheus reserves it for the scrape job.

A scrape runs one `SELECT 1`, one small read of the migration table, one temporary-file create-and-delete in the
media directory, a few indexed or grouped reads (feed head, outbox states and oldest pending message, peers, cursors,
the two backup evidence rows) and a directory listing for the backup timestamp. The migration scripts are parsed once
per process. The two expensive collectors are cached per process for `ATLAS_METRICS_CACHE_SECONDS` (default 300):
`catalog` (about a dozen grouped `COUNT` queries) and `media_storage` (a walk of `ATLAS_MEDIA_DIR`, stopped at 200 000
files, plus three artwork counts). While one scrape refreshes a cached collector, a concurrent scrape is served the
previous result rather than starting a second refresh. Scrape every 15 to 60 seconds, not faster, and watch
`atlas_collector_duration_seconds` and `atlas_collector_refresh_duration_seconds`.

Measured cost of the recording itself (SQLite, one Windows development host, so treat the numbers as an order of
magnitude): statement timing adds about 6 to 9 microseconds to each statement (14-16 µs to 21-24 µs for `SELECT 1`);
a title search is still one statement; the `catalog` collector runs 12 grouped queries and took 25 to 40 ms on a
2 000-record catalogue. Every other family costs a dictionary update under a lock, and nothing at all while
`ATLAS_METRICS_ENABLED` is off.
Each collector is isolated: if one fails, the rest of the scrape is still returned, the failed collector's
series are **absent** (not zero) and `atlas_collector_success{collector=...}` is 0. If the database itself is
down the token check fails first, the scrape returns an error and Prometheus records `up == 0`; use
`/api/v2/ready` or `atlas_ready` from the last good scrape to see why.

`atlas_backup_last_success_timestamp_seconds` describes files present, **not proof that a restore succeeded**;
see the verified and drill timestamps above. Filesystem names and error messages are not exported. There are no
distributed traces or consensus-lag metrics.

Entry lag is relative to the highest head observed for a pinned origin (shared when several
peer URLs relay the same origin). It is omitted until a valid head is observed. Heads are
unsigned operational hints; they never authorize feed entries or advance cursors. A cached
relay cannot reveal writes it has not seen, and zero lag is not proof that an unreachable
authority has no newer writes. Alert on stale successful pulls as well as entry lag.

## Safe request logs

JSON request events include node ID, request ID, bounded method, route **template**, HTTP status
and duration. A password-reset URL is logged as `/reset/{token}`, never its token or query.
Bodies, cookies, authorization headers, client addresses and user IDs are omitted. Unmatched
paths share one label, preventing arbitrary URL/cardinality growth.

`X-Request-ID` accepts 1–64 ASCII letters, digits, underscores or hyphens; other values are
replaced with a generated ID. Responses include the ID, including handled errors. Treat it
as correlation data, not authenticated identity. Unexpected errors return a generic message.

Atlas and uvicorn error formatters record source and exception **types**, not raw messages,
arguments or traceback text that might contain credentials. The usual uvicorn access logger
is disabled when JSON logging is enabled. This reduces diagnostic detail deliberately.
Reverse-proxy logs and third-party libraries have separate redaction policies.

## Initial objectives and alerts

These are **targets**, not measured guarantees: 99.5% monthly availability, cached-read p95
below 300 ms, less than 1% 5xx over 30 minutes, recovery point within 15 minutes and recovery
within one hour. The default daily backup schedule cannot meet a 15-minute RPO. Set a suitable
database backup/replication policy and rehearse it before claiming that target.

Start with readiness failing for two minutes, stale/failed peer pulls for several sync
intervals, increasing provider failures, growing email backlog and old backup timestamps.
Tune against real traffic. Histograms can drive p95 alerts; filter the route and exclude
`/metrics` itself. No dashboard or alert delivery service is bundled; example panels follow the rules.

Example Prometheus rule group; adjust thresholds to the configured sync interval and backup
schedule. The peer rules include peers that have never completed a pull:

```yaml
groups:
  - name: atlas
    rules:
      - alert: AtlasPeerStale
        expr: time() - atlas_peer_last_success_timestamp_seconds > 600
        for: 5m
      - alert: AtlasPeerBehind
        expr: atlas_peer_lag_entries > 100
        for: 10m
      - alert: AtlasBackupOverdue
        expr: (time() - atlas_backup_last_success_timestamp_seconds > 90000) and on(job, instance) (atlas_backup_enabled == 1)
        for: 15m
      - alert: AtlasNotReady
        expr: atlas_ready == 0
        for: 2m
        annotations:
          summary: "{{ $labels.instance }} fails readiness"
          description: "Look at atlas_ready_check: database, schema (pending migrations) or media."
      - alert: AtlasMediaDegraded
        expr: atlas_ready_check{check="media"} == 0
        for: 10m
      - alert: AtlasCollectorFailing
        expr: atlas_collector_success == 0
        for: 10m
      - alert: AtlasScrapeDown
        expr: up{job="atlas"} == 0
        for: 5m
      - alert: AtlasServerErrors
        expr: sum by(job, instance) (rate(atlas_http_requests_total{status=~"5.."}[30m])) / sum by(job, instance) (rate(atlas_http_requests_total[30m])) > 0.01
        for: 5m
```

## Example queries and dashboard

These queries use only the series above. Replace `job="atlas"` with your scrape job name; the application
never sets a label called `job`. They are examples to tune against real traffic, not an availability or recovery
guarantee: counters show what this process saw, not what clients experienced.

```promql
# Instances that fail readiness, and which dependency
atlas_ready == 0
atlas_ready_check == 0

# Request rate, and the 5xx ratio over 5 minutes, excluding the scrape and the health probes (a failing readiness probe answers 503 on every poll; `atlas_ready` and `AtlasNotReady` cover that state)
sum by (instance) (rate(atlas_http_requests_total{route!~"/metrics|/api/v2/ready|/api/v2/health"}[5m]))
sum(rate(atlas_http_requests_total{status=~"5..",route!~"/metrics|/api/v2/ready|/api/v2/health"}[5m]))
  / sum(rate(atlas_http_requests_total{route!~"/metrics|/api/v2/ready|/api/v2/health"}[5m]))

# p95 latency of one route (always aggregate by le)
histogram_quantile(0.95, sum by (le) (rate(atlas_http_request_duration_seconds_bucket{route="/game/{cid}"}[5m])))

# Concurrency: requests running, minus the one scrape reading each gauge
sum(atlas_http_requests_in_flight) - count(atlas_http_requests_in_flight)

# Exceptions that escaped a handler (the client saw a generic 500), and clients that left early
sum(rate(atlas_http_request_errors_total{class="unhandled_exception"}[5m]))
sum(rate(atlas_http_request_errors_total{class="client_disconnect"}[5m]))

# Scrape overhead, and any collector that is currently failing
sum by (instance) (atlas_collector_duration_seconds)
atlas_collector_success == 0
```

A minimal Grafana dashboard (import it, then pick your Prometheus data source for `DS_PROMETHEUS`). It
shows ready instances, failing readiness checks, request rate by status, p95 latency by route, in-flight requests,
error classes and scrape cost:

```json
{
  "title": "Atlas service overview",
  "schemaVersion": 39,
  "refresh": "30s",
  "time": {
    "from": "now-6h",
    "to": "now"
  },
  "templating": {
    "list": [
      {
        "name": "instance",
        "type": "query",
        "datasource": "${DS_PROMETHEUS}",
        "query": "label_values(atlas_ready, instance)",
        "includeAll": true,
        "multi": true
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "stat",
      "title": "Ready instances",
      "gridPos": {
        "x": 0,
        "y": 0,
        "w": 6,
        "h": 4
      },
      "targets": [
        {
          "expr": "sum(atlas_ready{instance=~\"$instance\"})",
          "legendFormat": "ready",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    },
    {
      "id": 2,
      "type": "table",
      "title": "Failing readiness checks",
      "gridPos": {
        "x": 6,
        "y": 0,
        "w": 6,
        "h": 4
      },
      "targets": [
        {
          "expr": "atlas_ready_check{instance=~\"$instance\"} == 0",
          "instant": true,
          "format": "table",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    },
    {
      "id": 3,
      "type": "timeseries",
      "title": "Requests per second by status",
      "gridPos": {
        "x": 0,
        "y": 4,
        "w": 12,
        "h": 8
      },
      "targets": [
        {
          "expr": "sum by (status) (rate(atlas_http_requests_total{instance=~\"$instance\",route!~\"/metrics|/api/v2/ready|/api/v2/health\"}[5m]))",
          "legendFormat": "{{status}}",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    },
    {
      "id": 4,
      "type": "timeseries",
      "title": "p95 latency by route (seconds)",
      "gridPos": {
        "x": 12,
        "y": 4,
        "w": 12,
        "h": 8
      },
      "targets": [
        {
          "expr": "histogram_quantile(0.95, sum by (le, route) (rate(atlas_http_request_duration_seconds_bucket{instance=~\"$instance\",route!~\"/metrics|/api/v2/ready|/api/v2/health\"}[5m])))",
          "legendFormat": "{{route}}",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    },
    {
      "id": 5,
      "type": "timeseries",
      "title": "Requests in flight (excluding the scrape)",
      "gridPos": {
        "x": 0,
        "y": 12,
        "w": 12,
        "h": 6
      },
      "targets": [
        {
          "expr": "sum by (instance) (atlas_http_requests_in_flight{instance=~\"$instance\"}) - 1",
          "legendFormat": "{{instance}}",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    },
    {
      "id": 6,
      "type": "timeseries",
      "title": "Error classes per second",
      "gridPos": {
        "x": 12,
        "y": 12,
        "w": 12,
        "h": 6
      },
      "targets": [
        {
          "expr": "sum by (class) (rate(atlas_http_request_errors_total{instance=~\"$instance\"}[5m]))",
          "legendFormat": "{{class}}",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    },
    {
      "id": 7,
      "type": "timeseries",
      "title": "Scrape cost by collector (seconds)",
      "gridPos": {
        "x": 0,
        "y": 18,
        "w": 24,
        "h": 6
      },
      "targets": [
        {
          "expr": "atlas_collector_duration_seconds{instance=~\"$instance\"}",
          "legendFormat": "{{collector}}",
          "datasource": "${DS_PROMETHEUS}"
        }
      ],
      "datasource": "${DS_PROMETHEUS}"
    }
  ]
}
```

### Queries and alerts for the service measurements (#70-#76)

Examples using only the families above; tune thresholds against real traffic. Counters are per process unless
`ATLAS_METRICS_DIR` is set, so aggregate with `sum by (...)` over instances before dividing.

```promql
# Federation: pulled pages by outcome, refused pages by class, useful throughput, stage p95, bytes
sum by (outcome) (rate(atlas_federation_batches_total[15m]))
sum by (reason) (increase(atlas_federation_verification_failures_total[1h]))
sum by (result) (rate(atlas_federation_entries_total{result=~"retained|applied|duplicate"}[15m]))
histogram_quantile(0.95, sum by (le, stage) (rate(atlas_federation_batch_duration_seconds_bucket[30m])))
sum(rate(atlas_federation_received_bytes_total[15m]))

# Providers: logical lookups by outcome, upstream p95, cache hit ratio, calls held back, quota left
sum by (provider, outcome) (rate(atlas_provider_calls_total[15m]))
histogram_quantile(0.95, sum by (le, provider) (rate(atlas_provider_request_duration_seconds_bucket[15m])))
sum by (provider) (rate(atlas_provider_cache_requests_total{result=~"hit|negative_hit"}[1h]))
  / sum by (provider) (rate(atlas_provider_cache_requests_total[1h]))
sum by (provider, reason) (increase(atlas_provider_backoff_total[1h]))
min by (provider) (atlas_provider_rate_limit_remaining)

# Catalogue: share of live games with each field, review backlog, enrichment outcomes
# (every instance reports the same rows, so the ratio is right however many are summed)
sum by (field) (atlas_catalog_field_coverage{media_type="game",state="present"})
  / sum by (field) (atlas_catalog_field_coverage{media_type="game"})
max by (queue) (atlas_catalog_review_pending)
sum by (outcome) (increase(atlas_enrichment_operations_total[1d]))

# Media: download failures by code, cache efficiency of /media, storage (a shared volume: max, not sum)
sum by (outcome) (increase(atlas_media_operations_total{operation="fetch",outcome!="ok"}[1h]))
sum(rate(atlas_media_cache_requests_total{route="media",result="hit"}[1h]))
  / sum(rate(atlas_media_cache_requests_total{route="media"}[1h]))
max(atlas_media_storage_bytes)

# Database and search: statement p95 by class, contention, search p95 and the zero-result share
histogram_quantile(0.95, sum by (le, operation) (rate(atlas_database_query_duration_seconds_bucket[5m])))
sum by (class) (rate(atlas_database_errors_total{class=~"lock_timeout|deadlock|serialization"}[5m]))
histogram_quantile(0.95, sum by (le, mode) (rate(atlas_search_duration_seconds_bucket{outcome="ok"}[5m])))
sum(rate(atlas_search_results_bucket{le="0"}[1h])) / sum(rate(atlas_search_results_count[1h]))

# Background work and mail: failures, late wake-ups, mail backlog age, delivery failure classes
sum by (operation) (increase(atlas_background_operations_total{outcome="error"}[1h]))
histogram_quantile(0.95, sum by (le, loop) (rate(atlas_background_schedule_delay_seconds_bucket[30m])))
time() - max(atlas_mail_oldest_pending_timestamp_seconds)
sum by (class) (increase(atlas_mail_deliveries_total{outcome="failed"}[1h]))

# Identity: sign-in funnel (cancellations apart from failures), consent, token failures, denied scopes, throttling
sum by (flow, outcome) (increase(atlas_identity_flows_total[1h]))
sum by (decision) (increase(atlas_oidc_consent_decisions_total[1d]))
sum by (operation, outcome) (increase(atlas_token_operations_total{outcome!="ok"}[1h]))
sum by (scope, reason) (rate(atlas_authorization_decisions_total{decision="denied"}[15m]))
sum by (operation) (rate(atlas_auth_rate_limit_decisions_total{decision="limited"}[15m]))
```

`atlas_mail_oldest_pending_timestamp_seconds` is absent when nothing is waiting, so `time() - max(...)` returns no
data (not zero) for an empty queue, and the mail alert below fires only while something is pending.

```yaml
groups:
  - name: atlas-operations
    rules:
      - alert: AtlasFederationRefusingPages
        expr: sum by (instance, reason) (increase(atlas_federation_verification_failures_total[30m])) > 0
        annotations:
          description: "Signed pages were refused ({{ $labels.reason }}). Check the peer's pinned key and history before trusting it again."
      - alert: AtlasFederationNotPulling
        expr: (sum by (instance) (rate(atlas_federation_batches_total{outcome=~"ok|empty"}[30m])) == 0) and on (instance) (sum by (instance) (rate(atlas_federation_batches_total[30m])) > 0)
        for: 30m
      - alert: AtlasProviderFailing
        expr: sum by (instance, provider) (rate(atlas_provider_calls_total{outcome=~"error|unavailable"}[15m])) / sum by (instance, provider) (rate(atlas_provider_calls_total[15m])) > 0.2
        for: 15m
      - alert: AtlasProviderQuotaLow
        expr: atlas_provider_rate_limit_remaining < 10
        for: 5m
      - alert: AtlasDatabaseContention
        expr: sum by (instance) (rate(atlas_database_errors_total{class=~"lock_timeout|deadlock|serialization"}[5m])) > 0.1
        for: 10m
      - alert: AtlasSearchSlow
        expr: histogram_quantile(0.95, sum by (le, instance) (rate(atlas_search_duration_seconds_bucket{outcome="ok"}[10m]))) > 1
        for: 15m
      - alert: AtlasBackgroundTaskFailing
        expr: sum by (instance, operation) (increase(atlas_background_operations_total{outcome="error"}[1h])) >= 3
      - alert: AtlasMailBacklogOld
        expr: time() - max by (instance) (atlas_mail_oldest_pending_timestamp_seconds) > 3600
        for: 15m
      - alert: AtlasRestoreDrillOverdue
        expr: (time() - max by (instance) (atlas_backup_last_restore_drill_timestamp_seconds) > 2678400) and on (instance) (atlas_backup_enabled == 1)
        annotations:
          description: "No isolated restore drill passed in 31 days: run `atlas backup drill <archive>`."
      - alert: AtlasSignInFailuresHigh
        expr: sum by (instance) (rate(atlas_identity_flows_total{outcome!~"ok|bad_credentials|provider_cancelled"}[15m])) > 0.1
        for: 15m
```

An operations dashboard over the same series (import it next to the service overview above):

```json
{
  "title": "Atlas operations",
  "schemaVersion": 39,
  "refresh": "1m",
  "time": {"from": "now-24h", "to": "now"},
  "panels": [
    {"id": 1, "type": "timeseries", "title": "Federation pages by outcome", "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "sum by (outcome) (rate(atlas_federation_batches_total[15m]))", "legendFormat": "{{outcome}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 2, "type": "timeseries", "title": "Federation stage p95 (s)", "gridPos": {"x": 12, "y": 0, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "histogram_quantile(0.95, sum by (le, stage) (rate(atlas_federation_batch_duration_seconds_bucket[30m])))", "legendFormat": "{{stage}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 3, "type": "timeseries", "title": "Provider lookups by outcome", "gridPos": {"x": 0, "y": 7, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "sum by (provider, outcome) (rate(atlas_provider_calls_total[15m]))", "legendFormat": "{{provider}} {{outcome}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 4, "type": "timeseries", "title": "Provider upstream p95 (s)", "gridPos": {"x": 12, "y": 7, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "histogram_quantile(0.95, sum by (le, provider) (rate(atlas_provider_request_duration_seconds_bucket[15m])))", "legendFormat": "{{provider}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 5, "type": "table", "title": "Catalogue coverage (games)", "gridPos": {"x": 0, "y": 14, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "max by (field, state) (atlas_catalog_field_coverage{media_type=\"game\"})", "instant": true, "format": "table", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 6, "type": "timeseries", "title": "Media fetch failures", "gridPos": {"x": 12, "y": 14, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "sum by (outcome) (rate(atlas_media_operations_total{operation=\"fetch\",outcome!=\"ok\"}[1h]))", "legendFormat": "{{outcome}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 7, "type": "timeseries", "title": "Statement and search p95 (s)", "gridPos": {"x": 0, "y": 21, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "histogram_quantile(0.95, sum by (le, operation) (rate(atlas_database_query_duration_seconds_bucket[5m])))", "legendFormat": "db {{operation}}", "datasource": "${DS_PROMETHEUS}"},
                 {"expr": "histogram_quantile(0.95, sum by (le, mode) (rate(atlas_search_duration_seconds_bucket{outcome=\"ok\"}[5m])))", "legendFormat": "search {{mode}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 8, "type": "timeseries", "title": "Background failures and mail backlog age", "gridPos": {"x": 12, "y": 21, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "sum by (operation) (increase(atlas_background_operations_total{outcome=\"error\"}[1h]))", "legendFormat": "{{operation}}", "datasource": "${DS_PROMETHEUS}"},
                 {"expr": "time() - max(atlas_mail_oldest_pending_timestamp_seconds)", "legendFormat": "oldest mail (s)", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 9, "type": "timeseries", "title": "Sign-in outcomes", "gridPos": {"x": 0, "y": 28, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "sum by (flow, outcome) (increase(atlas_identity_flows_total[1h]))", "legendFormat": "{{flow}} {{outcome}}", "datasource": "${DS_PROMETHEUS}"}]},
    {"id": 10, "type": "timeseries", "title": "Denied API requests by scope", "gridPos": {"x": 12, "y": 28, "w": 12, "h": 7},
     "datasource": "${DS_PROMETHEUS}",
     "targets": [{"expr": "sum by (scope, reason) (rate(atlas_authorization_decisions_total{decision=\"denied\"}[15m]))", "legendFormat": "{{scope}} {{reason}}", "datasource": "${DS_PROMETHEUS}"}]}
  ]
}
```

These are monitoring aids and prove no objective: a counter shows what one process observed, a drill timestamp shows
that one isolated restore passed, and neither is a measured recovery time.

See [Release verification](release-verification.md) for repeatable topology/restore drills.
