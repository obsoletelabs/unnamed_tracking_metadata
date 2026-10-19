# Deployment

## Choose a shape

| | Local node | Public authority |
| --- | --- | --- |
| Compose file | `compose.yaml` | `compose.authority.yaml` |
| Database | SQLite in a volume | PostgreSQL 16 |
| Role | `local` | `authority` |
| TLS | usually none (localhost/LAN) | required, via a reverse proxy |
| Email | `outbox` is fine | real SMTP recommended (verification and reset are on by default) |

Verified: the container image builds, runs as a non-root user, keeps data across restarts and reports healthy; the authority stack (Atlas + PostgreSQL + a one-shot migration job) starts in production mode.

## Production checklist (authority)

1. A host with Docker Compose and a DNS name, e.g. `atlas.example.org`.
2. `.env` (see `.env.example`):
   ```dotenv
   ATLAS_ROLE=authority
   ATLAS_ENVIRONMENT=production
   ATLAS_PUBLIC_URL=https://atlas.example.org
   ATLAS_INITIAL_ADMIN=alice
   ATLAS_BOOTSTRAP_ADMIN_TOKEN=<32+ random characters>
   ATLAS_SECRET_KEY=<atlas secret-key>
   POSTGRES_PASSWORD=<strong>
   ATLAS_MAIL_BACKEND=smtp
   ATLAS_SMTP_HOST=smtp.example.org
   ATLAS_SMTP_USER=…
   ATLAS_SMTP_PASSWORD=…
   ATLAS_MAIL_FROM=Atlas <noreply@example.org>
   ```
   Production mode **refuses to start** unless: HTTPS public URL, PostgreSQL for an authority, and (with open registration) an initial admin or bootstrap token.
3. `docker compose -f compose.authority.yaml up -d`. It runs `atlas migrate` first, then the server (bound to `127.0.0.1:8000`; **never publish PostgreSQL**).
4. Put a TLS-terminating reverse proxy in front.

### Reverse proxy

The app must see the visitor's address for rate limits, so the proxy must send `X-Forwarded-For`/`X-Forwarded-Proto`, and the server must trust only the proxy (the authority compose file uses `--forwarded-allow-ips "*"`, which is safe **only** because the port is bound to localhost behind your proxy).

Caddy:
```
atlas.example.org {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```
nginx:
```nginx
server {
    server_name atlas.example.org;
    client_max_body_size 4m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
(plus your usual TLS configuration).

## Backups

What to back up:

1. **The database** (`pg_dump -Fc` for PostgreSQL; the volume or `sqlite3 .backup` for SQLite). It contains the records, accounts, settings **and the node's signing keys** (identity), so followers keep trusting a restored node.
2. **The media volume** (`ATLAS_MEDIA_DIR`). It can be re-downloaded (provider artwork) but community-uploaded logos cannot.
3. **Your `.env`**, especially `ATLAS_SECRET_KEY`: without it stored peer tokens and OIDC client secrets cannot be decrypted (you would re-enter them).

Restore: new host, same `.env`, restore the database and media, `docker compose up -d`. Test a restore before you need it.

## Upgrading

1. Back up (above).
2. `git pull`, then `docker compose -f compose.authority.yaml up -d --build` (the migration job runs first). With several replicas set `ATLAS_AUTO_MIGRATE=false` and run `atlas migrate` once as a job.
3. Migrations are forward-only; to roll back, restore the backup.

## Operating

* **Health:** `GET /api/v2/health` (liveness), `/api/v2/ready` (database), `/api/v2/status` (admin: feed head, replication).
* **Logs/metrics:** enable `ATLAS_JSON_LOGS` and `ATLAS_METRICS_ENABLED` as needed; [Logs and metrics](observability.md) explains redaction and scoped scraping.
* **Scaling:** more replicas are fine (rate limits, sessions and tokens live in the database); one writer holds a lock at a time. No load figures have been measured; plan capacity by testing your own workload.
* **Housekeeping:** background threads in each process send email, cache artwork, purge expired counters/tokens and pull from peers. `atlas cache-media` runs a caching pass by hand.

## Command line

`atlas serve | migrate | backup | recover-mirror | token | node | secret-key | sync | cache-media | make-admin | import-legacy` (`docker compose exec atlas atlas <command>`). `import-legacy <demo-1.db>` migrates data from the earlier demo version (release dates were year-only there and are dropped). Use `recover-mirror` only with the [recovery runbook](recovery.md).

For every environment variable: [Environment variables](reference-env.md).
