# Atlas metadata catalogue — mockup 1

A small, runnable MVP of the proposed central game metadata service. This branch is for experimentation and review; it is not production-ready.

## Run with Docker

Requirements: Docker Engine and the Docker Compose plugin.

1. Copy `.env.example` to `.env`.
2. Set `SESSION_SECRET` to a long random value. Do not use the example value for an instance other people can access.
3. Start the app:

   ```sh
   docker compose up --build -d
   ```

4. Open [http://localhost:8000](http://localhost:8000).

The default database is SQLite in a named Docker volume, so records survive container recreation. To stop the service, run `docker compose down`. This preserves the data volume; `docker compose down -v` deletes it.

### PostgreSQL option

A separate Compose overlay starts PostgreSQL 16 and waits for its health check before starting the API. Set a strong password in `.env` as `POSTGRES_PASSWORD=...`, then run:

```sh
docker compose -f compose.yaml -f compose.postgres.yaml up --build -d
```

The overlay configures the API to use PostgreSQL on the internal Compose network. Do not publish the PostgreSQL port to the internet. To switch an existing SQLite instance to PostgreSQL, export/import the data first; changing `DATABASE_URL` does not migrate records automatically.

## What works

- Responsive catalogue website backed by the live database
- Search games by title, developer or canonical ID
- Register an account, sign in and sign out
- Generate API bearer tokens; the full token is displayed once
- List owned tokens and revoke a token
- Add a game through the website after signing in
- Public JSON reads and token-authenticated JSON creation
- Auto-generated interactive API docs at `/docs` and OpenAPI at `/openapi.json`
- Health check at `/health`
- Seed records added to an empty database on first startup

### API examples

Search without a key:

```sh
curl 'http://localhost:8000/api/v1/games?q=hollow'
```

Fetch a canonical record:

```sh
curl 'http://localhost:8000/api/v1/games/gm_YOURID'
```

Create a record with a key generated from the website:

```sh
curl -X POST 'http://localhost:8000/api/v1/games' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Tunic","developer":"TUNIC Team","release_year":2022,"description":"An isometric adventure."}'
```

The actual canonical ID returned by the API should be used for subsequent lookups.

## Important limitations

This is a **local MVP/mockup, not a secure public beta**. It intentionally keeps the data model small: game title, developer, release year and description. Achievement sets, external identifier mappings, source provenance, revisions, moderation, sync cursors, quotas, rate limits and multi-region routing are not implemented yet.

Before exposing this publicly, it needs security hardening, including CSRF protection for browser forms, secure deployment configuration and cookie settings behind HTTPS, abuse controls/rate limiting, account recovery and verification, stronger API key policy/scopes, database migrations, automated tests, logging and backup/restore procedures. The default session secret is for local development only. Do not use real passwords or production credentials in this prototype.

## Files

- `app/main.py` — FastAPI application, database models, web routes and JSON API
- `app/templates/index.html` — website and account dashboard
- `app/static/style.css` — responsive styling
- `Dockerfile`, `compose.yaml`, `compose.postgres.yaml` — container setup
- `index.html` — earlier standalone visual concept retained for comparison

This branch is a foundation for trying the end-to-end workflow and deciding what should become part of the real service.
