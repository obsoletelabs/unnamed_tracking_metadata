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
| `atlas_collector_success` | One per scrape collector (`collector` is `readiness`, `feed`, `outbox`, `peers`, `sync_upstreams`, `providers` or `backup`); zero means that collector failed and its series are absent from this scrape (unavailable, not zero) |
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
| `atlas_peer_status` | One for the peer's current status (`status` is `paused`, `not_contacted`, `needs_you`, `unreachable`, `empty`, `catching_up`, `caught_up_waiting` or `caught_up`), zero for the others, per peer |
| `atlas_feed_records` | Authority only: live games by `state`: `introduced` to followers, `not_federated` (never introduced), `missing` (named by a signed entry but never introduced: `atlas feed introduce --missing` would publish them) |
| `atlas_outbox_messages` | Pending, failed and sent email counts; failed deliveries may be retried |
| `atlas_provider_failures_total` | Provider fetch failures in this process |
| `atlas_backup_enabled` | Whether scheduled archive storage is configured |
| `atlas_backup_last_success_timestamp_seconds` | Newest archive timestamp in the configured directory; zero if absent |

Request/provider counters and `atlas_http_requests_in_flight` reset on restart and are **per process**.
Scrape each worker or replica on its own address (a Kubernetes `PodMonitor`, or one static target per
instance); a load balancer choosing a random worker returns a different process's counters each time, which
makes `rate()` meaningless. Aggregate afterwards with `sum by (...)` over the **request and error counters and
`in_flight`** (they are additive across instances, and `rate()` handles the reset on restart). Do **not** add up the
**database-derived gauges** (`atlas_feed_head`, `atlas_outbox_messages`, `atlas_sync_*`,
`atlas_peer_*`, `atlas_schema_state`, backup timestamps): every instance reads the same database, so
use `max by (...)` or `avg by (...)`. `atlas_ready` and `atlas_ready_check` are per instance on purpose. A scrape request appears
in request counters on the following scrape.

### Label budget and scrape cost

Every label value comes from a fixed set in code, never from a request:

| Label | Values | Bound |
| --- | --- | --- |
| `method` | `GET POST PUT PATCH DELETE OPTIONS HEAD OTHER` | 8 |
| `route` | the matched route **template** (`/game/{cid}`), `unmatched` for anything that matched no route (including static files and scanners), or `other` | at most 512 distinct, then folded into `other` |
| `status` | the response status (100-599), `499` for a client that left first, `0` for an invalid value | what the application can return |
| `class`, `check`, `state`, `collector`, `head` | fixed names listed above | constant |
| `peer_id`, `node_id`, `provider` | rows an administrator configured and nodes this node pins | one series set per configured peer, origin or provider; keep these lists small |

No label ever carries an account, client, email, IP, token, URL, query text, title, error message or plugin
name. Do not add a label called `job`: Prometheus reserves it for the scrape job.

A scrape runs one `SELECT 1`, one small read of the migration table, one temporary-file create-and-delete in the
media directory, a few indexed or grouped reads (feed head, outbox states, peers, cursors) and a directory
listing for the backup timestamp. The migration scripts are parsed once per process. There is no
background cache yet: scrape every 15 to 60 seconds, not faster, and watch `atlas_collector_duration_seconds`.
Each collector is isolated: if one fails, the rest of the scrape is still returned, the failed collector's
series are **absent** (not zero) and `atlas_collector_success{collector=...}` is 0. If the database itself is
down the token check fails first, the scrape returns an error and Prometheus records `up == 0`; use
`/api/v2/ready` or `atlas_ready` from the last good scrape to see why.

Backup timestamps describe files present, **not proof that a restore succeeded**. Scheduled
backups are verified after creation, but an archive copied into that directory still needs
verification. Filesystem names and error messages are not exported. There are no measured
provider quotas, provider latency histograms, distributed traces or consensus-lag metrics.

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

See [Release verification](release-verification.md) for repeatable topology/restore drills.
