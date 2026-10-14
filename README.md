# Atlas metadata catalogue — mockup 1

A small, runnable MVP of the proposed central game metadata service. This branch is for experimentation and review; it is not production-ready.

## Run with Docker

Requirements: Docker Engine and the Docker Compose plugin.

1. Copy `.env.example` to `.env`.
2. Set `SESSION_SECRET` to a long random value. Do not use the example value for an instance other people can access.
3. Set `ADMIN_EMAILS` to the email address that will own the initial administrator account. Separate multiple addresses with commas.
4. Start the app:

   ```sh
   docker compose up --build -d
   ```

5. Open [http://localhost:8000](http://localhost:8000). If the database has no users, the app opens the first-run setup screen; create the administrator using an email listed in `ADMIN_EMAILS`.

The default database is SQLite in a named Docker volume, so records survive container recreation. To stop the service, run `docker compose down`. This preserves the data volume; `docker compose down -v` deletes it.


### SMTP and self-registration

Self-registration is disabled by default. To enable it, configure these environment variables in \`.env\` and set \`ALLOW_SELF_REGISTRATION=true\`:

\`\`\`dotenv
PUBLIC_BASE_URL=https://metadata.example.com
ALLOW_SELF_REGISTRATION=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=metadata@example.com
SMTP_STARTTLS=true
\`\`\`

Use the externally reachable HTTPS URL for \`PUBLIC_BASE_URL\` in a deployed instance. The SMTP sender must be authorised by your mail provider. Existing accounts are treated as verified during the compatibility migration; self-registered accounts remain blocked until their one-time verification link is used. The first administrator is created through \`/setup\` while the database has no users.

### PostgreSQL option

A separate Compose overlay starts PostgreSQL 16 and waits for its health check before starting the API. Set a strong password in `.env` as `POSTGRES_PASSWORD=...`, then run:

```sh
docker compose -f compose.yaml -f compose.postgres.yaml up --build -d
```

The overlay configures the API to use PostgreSQL on the internal Compose network. Do not publish the PostgreSQL port to the internet. To switch an existing SQLite instance to PostgreSQL, export/import the data first; changing `DATABASE_URL` does not migrate records automatically.

## What works

- Responsive catalogue website backed by the live database
- Login-required website search by title, developer or canonical ID
- Dedicated sign-in, first-run administrator setup, dashboard, catalogue, game detail and achievements pages
- Admin-created user accounts; optional self-registration only when SMTP is configured and explicitly enabled
- SMTP email verification is required before self-registered users can sign in
- Sign in and sign out
- Generate API bearer tokens; the full token is displayed once
- List owned tokens and revoke a token
- Add games through a schema-driven form; canonical IDs are generated automatically
- Background provider ID discovery for exact title matches, with ambiguous candidates left for review
- Add achievements to games with descriptions, optional icons, provider IDs, and hidden/secret status
- API-key-required JSON reads and writes; health checks remain public
- Admin schema editor with JSON export and validated schema-driven game fields
- Metadata provider registry and per-provider external game IDs
- Built-in discovery adapters for Steam Store search, IGDB (optional credentials), and RAWG (optional API key)
- Relational tags, schema-validated custom fields, and image uploads (PNG/JPEG/WebP/GIF, 5 MiB max)
- Auto-generated interactive API docs at `/docs` and OpenAPI at `/openapi.json`
- Health check at `/health`
- Seed records added to an empty database on first startup

### API examples

The first two examples below require an `Authorization: Bearer YOUR_API_KEY` header. The only intentionally public API endpoint is `/health`.

Search with a key generated from the website:

```sh
curl 'http://localhost:8000/api/v1/games?q=hollow' \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

Fetch a canonical record:

```sh
curl 'http://localhost:8000/api/v1/games/gm_YOURID' \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

Create a record with a key generated from the website:

```sh
curl -X POST 'http://localhost:8000/api/v1/games' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Tunic","developer":"TUNIC Team","release_year":2022,"description":"An isometric adventure."}'
```

The actual canonical ID returned by the API should be used for subsequent lookups. Creating a game schedules background discovery against registered provider adapters. Exact title matches may be attached automatically; uncertain matches are not silently assigned.

List and create achievements for a game:

```sh
curl 'http://localhost:8000/api/v1/games/gm_YOURID/achievements' \
  -H 'Authorization: Bearer YOUR_API_KEY'

curl -X POST 'http://localhost:8000/api/v1/games/gm_YOURID/achievements' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"name":"First steps","description":"Complete the tutorial"}'
```

Run provider ID discovery manually:

```sh
curl -X POST 'http://localhost:8000/api/v1/games/gm_YOURID/discover-identifiers' \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

Upload an image (the returned URL also requires an API key):

```sh
curl -X POST 'http://localhost:8000/api/v1/games/gm_YOURID/images' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -F 'file=@cover.png'
```

## Important limitations

This is a **local MVP/mockup, not a secure public beta**. It now has separate user/admin pages, SMTP verification, admin account provisioning, schema-driven game entry, basic achievements, provider ID discovery and image uploads, but is still early-stage. Achievement unlock/progress tracking, field-level provenance, revisions, moderation, provider refresh schedules, sync cursors, quotas, rate limits, API scopes and multi-region routing are not implemented yet.

Before exposing this publicly, it needs security hardening, including CSRF protection for browser forms, secure deployment configuration and cookie settings behind HTTPS, abuse controls/rate limiting, password-reset/invitation workflows, scoped API keys, formal database migrations, broader automated tests, logging and backup/restore procedures. SMTP must be configured and `ALLOW_SELF_REGISTRATION=true` set explicitly to enable public self-registration. The schema JSON editor currently defines simple field contracts; it is not a general-purpose arbitrary schema execution system. The default session secret is for local development only. Do not use real passwords or production credentials in this prototype.

## Files

- `app/main.py` — FastAPI application, database models, web routes and JSON API
- `app/templates/auth.html` and `setup.html` — sign-in, registration, first-admin setup
- `app/templates/dashboard.html` — user dashboard and API key manager
- `app/templates/catalogue.html`, `game_form.html`, `game_detail.html`, `achievements.html` — catalogue and game workflows
- `app/templates/admin_home.html`, `admin.html`, `providers.html`, `users.html` — separate administration screens
- `app/static/style.css` — responsive styling
- `Dockerfile`, `compose.yaml`, `compose.postgres.yaml` — container setup
- `index.html` — earlier standalone visual concept retained for comparison

This branch is a foundation for trying the end-to-end workflow and deciding what should become part of the real service.
