# Observability and service objectives

## Endpoints

- `GET /healthz`: process liveness. It intentionally does not require the database to be reachable.
- `GET /readyz`: readiness probe; returns HTTP 503 when the database check fails.
- `GET /metrics`: Prometheus text format with process uptime, total HTTP requests and HTTP 5xx responses.

## Structured access logs

Each HTTP request emits a single JSON log event with request ID, method, path, status code and elapsed milliseconds. Query strings, request bodies, authorization headers, cookies, and API token values are deliberately excluded. API errors return the request ID so operators can correlate client reports with logs.

The current counters are process-local and reset when a process restarts. They are suitable for initial instrumentation, not yet a replacement for a shared metrics backend or histogram-based latency monitoring.

## Initial SLO targets

These are intended targets, not claims of measured production performance:

- **Availability:** 99.5% monthly for the central API, excluding announced maintenance.
- **Read latency:** p95 below 300 ms for cached/local catalogue reads.
- **Error rate:** less than 1% server-side 5xx responses over a rolling 30-minute window.
- **Recovery:** RPO at most 15 minutes and RTO at most 1 hour, pending successful restore/failover drills.
- **Provider independence:** local canonical reads remain available when external providers are unavailable.

## Suggested alerts

- Readiness failures lasting more than 2 minutes.
- 5xx rate above 1% for 10 minutes.
- p95 read latency above 300 ms for 15 minutes after latency histograms are deployed.
- Durable job queue age above 10 minutes or dead-letter count increasing.
- Provider adapter failure rate above its configured threshold.
- PostgreSQL connection saturation, replication lag, or backup age beyond the agreed recovery point.
- Object-store checksum failures or derivative generation failures.

## Gaps to close before public production

- Export metrics through a shared Prometheus-compatible backend and add request latency histograms.
- Add per-provider health, latency, quota, and circuit-breaker metrics.
- Add durable queue depth, retry, dead-letter, and oldest-job-age metrics.
- Add optional OpenTelemetry tracing with secret and personal-data redaction.
- Add dashboard/runbook links and verify alert thresholds with real traffic.
- Measure RPO/RTO through automated restore and failover drills before claiming the targets are achieved.
