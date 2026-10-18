# Database operations

## Production authority

PostgreSQL is the production source of truth. API and worker processes are disposable and must not keep authoritative state in local files, process memory, or a node-local SQLite database.

Production PostgreSQL should use:

- automated backups with point-in-time recovery where the hosting platform supports it;
- a tested restore procedure, not merely a configured backup job;
- connection pooling sized for the database rather than per-process maximums;
- `pool_pre_ping`/connection health checks and bounded connection lifetime in long-running deployments;
- a managed HA/replication configuration where the availability target requires it;
- migrations run as an explicit deployment step or controlled startup gate, never opportunistically by arbitrary application code.

The application's migration history is append-only. A revision that has been applied must not be edited in place; create a new revision for every schema change.

Every revision is written to be safe to repeat. If `schema_migrations` records revisions but model tables are missing (tables dropped, or a partial restore that kept the revision table), `run_migrations` replays the recorded revisions instead of serving a broken schema; an intact schema is left alone. After migrating, `app.asgi` runs `seed_defaults()`, which creates the default `game` schema definition, the default metadata providers (Steam Store, IGDB, RAWG) and, on an empty catalogue, the demo games, once.

## SQLite local mode

SQLite remains a supported local/self-hosted mode for installations that do not need multiple writers or PostgreSQL's HA capabilities.

The application enables WAL mode, foreign-key enforcement, and a busy timeout for SQLite. A local deployment should keep the database on durable local storage and should back it up as a single consistent database artifact.

SQLite is appropriate for:

- local development;
- a single-user or low-write home installation;
- offline metadata authorities;
- test fixtures and deterministic integration environments.

SQLite is not the recommended shared central database for a horizontally scaled service.

## Central versus user-hosted storage

Both deployment modes use the same logical metadata model:

```text
central authority
  PostgreSQL + object storage + shared workers

user-hosted authority
  SQLite or PostgreSQL + local object storage
```

A local authority may additionally retain private source observations and user-defined overrides. Those records are not automatically uploaded to the central authority.

If a local authority synchronizes with the central service, canonical IDs and mappings are additive. A local database must remain usable when the central service is unavailable.

## Migration procedure

1. Back up the database before a production migration.
2. Deploy the application version containing the migration.
3. Run the versioned migration runner once as the deployment gate.
4. Verify the recorded schema revision.
5. Start/roll the API and workers.
6. Run smoke checks against health, authentication, reads and representative writes.
7. Keep the previous application artifact available for rollback. If a schema change is irreversible, use a forward-fix migration rather than pretending an application rollback is safe.

## Backup/restore acceptance

A production environment is not considered backed up until a restore has been performed against an isolated database and representative API reads have been verified. Restore drills should be repeated at a cadence appropriate to the service's RPO/RTO target.

The initial architecture target is RPO <= 15 minutes and RTO <= 1 hour; operators may tighten these targets after measuring the actual deployment.
