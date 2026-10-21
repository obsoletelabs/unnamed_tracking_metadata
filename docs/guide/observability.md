# Logs, metrics and service objectives

## Enable telemetry

Both features are opt-in and require a restart:

```dotenv
ATLAS_JSON_LOGS=true
ATLAS_METRICS_ENABLED=true
```

`/api/v2/health` checks process liveness (and reports `media: ok|unwritable`); `/api/v2/ready` checks
that the database answers **and its schema is at the newest migration**, and returns 503
(`{"status":"not_ready"}`) otherwise, which is what an orchestrator should use to hold traffic during a
rolling upgrade with `ATLAS_AUTO_MIGRATE=false`. Neither depends on external providers, and the public
responses stay minimal on purpose. An administrator token gets the detail from `GET /api/v2/status`:
`checks.database`, `checks.schema` (`current`, `behind` or `unknown`), `checks.media` and
`checks.provider_failures`.

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
| `atlas_http_requests_total` | Requests by bounded method, route template and status |
| `atlas_http_request_duration_seconds` | Request duration histogram, sum and count, in seconds |
| `atlas_feed_head` | This node's own signed feed head |
| `atlas_sync_cursor` | Applied position per independently trusted origin |
| `atlas_sync_head` | Highest transport-reported head observed for an origin |
| `atlas_peer_lag_entries` | Entries between that origin's observed head and applied cursor, labeled per peer |
| `atlas_peer_last_success_timestamp_seconds` | Last successful pull per enabled peer; zero before first success |
| `atlas_peer_error` | One when the last peer pull failed |
| `atlas_outbox_messages` | Pending, failed and sent email counts; failed deliveries may be retried |
| `atlas_provider_failures_total` | Provider fetch failures in this process |
| `atlas_backup_enabled` | Whether scheduled archive storage is configured |
| `atlas_backup_last_success_timestamp_seconds` | Newest archive timestamp in the configured directory; zero if absent |

Request/provider counters reset on restart and are **per process**. Scrape each worker
separately; a load balancer choosing a random worker does not provide reliable counters.
Database gauges are shared and should not be summed across workers. A scrape request appears
in request counters on the following scrape.

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
`/metrics` itself. No dashboard or alert delivery service is bundled.

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
      - alert: AtlasServerErrors
        expr: sum by(job, instance) (rate(atlas_http_requests_total{status=~"5.."}[30m])) / sum by(job, instance) (rate(atlas_http_requests_total[30m])) > 0.01
        for: 5m
```

See [Release verification](release-verification.md) for repeatable topology/restore drills.
