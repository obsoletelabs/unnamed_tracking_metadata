# Production architecture

This document is the target architecture for the metadata service. The service is an online metadata authority and reconciliation layer for `unnamed_tracking_app` and future clients; it is not a proxy that blindly republishes third-party source data.

## Core invariants

1. **Canonical identity is ours.** Provider IDs are evidence and mappings, not canonical identity.
2. **Authoritative metadata is distinct from community preference.** Votes may influence presentation, but cannot silently rewrite authoritative fields.
3. **Provenance is retained.** Provider/source, observed time, confidence, and reconciliation decisions must remain inspectable.
4. **Application nodes are disposable.** No durable user, metadata, job, session, or image state may exist only on an API/worker filesystem.
5. **PostgreSQL is the production source of truth.** Object storage is authoritative for binary media; caches are disposable.
6. **External providers are dependencies, not availability requirements.** Provider outages degrade freshness/search coverage rather than taking down existing metadata.
7. **All expensive work is bounded and recoverable.** Provider calls, image processing, indexing, and reconciliation run through bounded synchronous work or durable jobs.
8. **Clients can safely retry.** Mutations that can be retried across network failures use idempotency semantics.
9. **Community content is moderated and attributable.** Uploaded media and proposed metadata retain contributor/provenance information and can be reported or removed without corrupting canonical records.
10. **Reads prefer availability; writes prefer correctness.** Stale cached metadata may be served when explicitly permitted, while authoritative mutations require a healthy transactional database.

## Logical topology

```text
Clients / unnamed_tracking_app
          |
      CDN / WAF
          |
   Load balancer / ingress
          |
   +------+----------------+
   | stateless API nodes   |
   | stateless admin UI    |
   +------+----------------+
          |
   +------+------+-------------------+
   |             |                   |
PostgreSQL    Redis/cache        Object storage
(authority)  (disposable)       (images/derivatives)
   |             |                   |
   +-------------+-------------------+
          |
     durable job queue
          |
   +------+------+----------------+
   | workers / provider adapters   |
   +------+-----------------------+
          |
   External metadata providers
```

The first production deployment may collapse these logical components onto fewer machines, but the interfaces must preserve the separation. Scaling API nodes must not require shared local storage or sticky sessions.

## Durable data ownership

| Data | Authority | Replication/failure behavior |
|---|---|---|
| Users, API keys, canonical games, mappings, tags, votes, reports, audit history | PostgreSQL | HA primary + replicas/backups; transactions are authoritative |
| Original images and generated derivatives | S3-compatible object storage | Versioning/replication where available; database stores durable references and hashes |
| Search indexes | Derived | Rebuildable from PostgreSQL; replicated or regenerated asynchronously |
| Provider response cache | Redis/cache | Disposable; provider outage must not erase canonical data |
| Job state | Durable queue / PostgreSQL-backed job store as selected | Retry/recovery after worker loss |
| API/session state | Database-backed or signed/rotatable session mechanism | No node-local session requirement |

## Trust boundaries

### Clients to API

Clients are untrusted. Authentication, authorization, input validation, request-size limits, rate limits, and CSRF protection for browser mutations occur at the application boundary. API tokens are scoped and never grant access merely because a route exists.

### API to providers

Providers are untrusted external dependencies. Secrets are server-side only. Responses are parsed into strict internal schemas, bounded in size, and never treated as trusted HTML or executable content. Provider latency, errors, quotas, and circuit state are isolated per provider.

### API/workers to media

Uploaded media is untrusted. The service validates file signatures and decoded content, imposes dimension/pixel/size limits, computes content hashes, strips or controls unsafe metadata, and stores binaries outside the application filesystem. Remote media retrieval, if supported, uses an explicit SSRF-safe fetcher rather than arbitrary server-side HTTP.

### Community to authority

Community submissions are proposals. They may become visible alternatives and may affect presentation ranking, but require the defined moderation/reconciliation rules before changing canonical metadata.

## Online/distributed behavior

### API nodes

API nodes are stateless and horizontally scalable. Health checks distinguish process liveness from dependency readiness. Rolling deployments must permit old and new application versions to overlap during migrations.

### PostgreSQL

Production uses a managed or otherwise HA PostgreSQL deployment with automated backups, tested restores, connection pooling, and a documented failover procedure. Read replicas may serve explicitly safe read workloads but never become accidental authorities.

### Redis/cache

Cache loss is safe. Cache entries carry schema/data versions and TTLs. Cross-node invalidation is event-driven or version-based; correctness never depends on every cache invalidation succeeding.

### Object storage

Images are addressed by immutable content/derivative identifiers. CDN URLs can be cached aggressively. Replacing metadata does not require replacing a binary. Missing derivatives can be regenerated from the durable original.

### Jobs

Workers are interchangeable. A worker crash leaves a recoverable job. Jobs have bounded retries, exponential backoff, dead-letter/failure state, and idempotency keys. Duplicate delivery must be harmless.

### Provider outages

Search can return local/cached results when providers are unavailable. Enrichment records provider failure without deleting existing metadata. Circuit breakers prevent a failing provider from consuming all request capacity.

## Consistency model

- **Canonical writes:** transactional and strongly consistent within PostgreSQL.
- **Community vote counts/rankings:** eventually consistent presentation data is acceptable, but a user's own vote should be reflected immediately in its response.
- **Search indexes:** eventually consistent and rebuildable.
- **Provider freshness:** explicitly timestamped and eventually refreshed.
- **CDN/cache:** eventually consistent, with immutable media URLs and versioned metadata responses where possible.

## Recovery targets

The initial production target is **RPO <= 15 minutes** for database data and **RTO <= 1 hour** for a complete service-region recovery. These are operational targets, not guarantees until backup/restore drills demonstrate them. Image/object storage should target materially lower data-loss risk through provider versioning/replication.

## Deployment progression

1. Local: SQLite + local object directory, single process.
2. Small production: PostgreSQL + S3-compatible storage + one Redis + two API instances + one worker pool.
3. Highly available production: PostgreSQL HA/replica topology + replicated object storage + redundant Redis + multiple API/worker instances behind an ingress/CDN.
4. Regional resilience: independent application stacks sharing replicated durable data, with explicit failover rather than pretending active-active writes are free.

The system should not require multi-region active-active writes initially. A carefully replicated primary with tested disaster recovery is safer than introducing distributed write conflicts before the data model and reconciliation semantics are mature.

## What is explicitly *not* the authority

The service must not treat Steam, IGDB, community votes, browser uploads, caches, search indexes, or a particular API node as the canonical authority. They are sources, evidence, derived views, or presentation inputs. This distinction is foundational to reliable reconciliation and long-term integration.
