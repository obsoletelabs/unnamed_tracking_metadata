# Distributed resilience

The service is designed so API instances and workers are disposable. Durable state lives outside the process.

## Stateless API nodes

- No canonical metadata is stored only in process memory.
- SQLite is intended for a single local authority; production central deployments use PostgreSQL.
- Redis is disposable cache state, never canonical state.
- Object storage holds durable media and is addressed by content hash.
- Request IDs are generated per request and returned to clients.

## Worker recovery

Async jobs are durable rows with idempotency keys, attempt counts, leases, retry backoff and dead-letter state. A worker crash leaves a running lease that can be reclaimed after the stale-job threshold.

Workers must be safe to run concurrently. Job handlers should use the job's idempotency key when mutating canonical state.

## Database

Central production deployments should use PostgreSQL with automated backups and a tested failover/restore path. SQLite WAL is intentionally retained for standalone/user-hosted authorities and should not be used as a shared network database.

## Media

S3-compatible object storage is preferred for multi-node deployments. Local filesystem media is for standalone authorities. Media URLs are immutable content-addressed objects, so CDN and node caches can be safely reused across rolling deployments.

## Cache failure

Redis loss must degrade to cache misses, not metadata loss. The application has a bounded in-process fallback for standalone operation. Canonical reads must always remain possible from PostgreSQL.

## Rolling deployments

Deploy the database migrations before starting new application nodes. New nodes must be able to read the previous schema during a rolling transition. Remove old code only after all nodes have moved past the compatibility window.

## Recovery targets

The initial architecture target is **RPO <= 15 minutes** and **RTO <= 1 hour** for the central service. These are targets, not claims of achieved performance, until backup restoration and failover drills are automated and recorded.

## Regional outage

A second region should use replicated PostgreSQL/object storage and a shared cache/queue strategy. During a regional outage, read traffic may be served from a promoted replica according to the deployment's database failover policy. Local/user-hosted authorities remain independently usable and can later reconcile with the central authority.

## Required operational drills

1. Restore the newest PostgreSQL backup into an isolated environment.
2. Verify schema migrations and row counts.
3. Verify object-store media checksums against database records.
4. Rebuild disposable caches and search indexes.
5. Requeue stale jobs and verify idempotent completion.
6. Measure actual RPO/RTO and update the published targets if necessary.
