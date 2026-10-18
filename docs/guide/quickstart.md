# Quick start

You need Docker with the Compose plugin. Five minutes, no accounts elsewhere.

## 1. Start a node

```sh
git clone https://github.com/obsoletelabs/unnamed_tracking_metadata.git
cd unnamed_tracking_metadata && git checkout v2
cp .env.example .env
```

Edit `.env`:

```dotenv
ATLAS_ROLE=local
ATLAS_PUBLIC_URL=http://localhost:8000
ATLAS_INITIAL_ADMIN=alice            # the username that will administer this node
ATLAS_BOOTSTRAP_ADMIN_TOKEN=         # 32+ random characters, e.g. `openssl rand -hex 24`
```

```sh
docker compose up -d
```

Open <http://localhost:8000>. A local node keeps its data in a Docker volume (SQLite) and sends no email (messages are listed under **Admin → Email** instead).

## 2. Become the administrator

Choose **Create account** and register with the username you put in `ATLAS_INITIAL_ADMIN`. That account is the administrator. (If you leave the setting empty, the first account to register is.)

## 3. Add some games

* **From the site:** *Add game* (your proposal; the administrator's applies at once).
* **From a provider:** `POST /api/v2/providers/steam/fetch {"external_id": "620"}` with a token, which downloads, merges and stores the record.

```sh
TOKEN=$ATLAS_BOOTSTRAP_ADMIN_TOKEN
curl -s localhost:8000/api/v2/providers/steam/fetch -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"external_id":"620"}'
```

## 4. Create a token for other programs

**Admin → Tokens** → name it, tick `read` → copy the secret (shown once). Or from the command line: `docker compose exec atlas atlas token --name tracker --scopes read`.

```sh
curl -s 'localhost:8000/api/v2/games?q=portal' -H "Authorization: Bearer <token>"
```

## 5. Make it yours

**Admin → Settings** changes the name, colours, logo, menu links, registration, email and limits live. **Admin → Pages** edits About/Rules/Help. **Admin → Fields** adds extra fields (for example *Engine* or *Age rating*). See [Administering](administering.md).

## Next

* A shared, public node? Read [Deployment](deployment.md) (PostgreSQL, HTTPS, email) and set `ATLAS_ROLE=authority`.
* Follow a central node from home? [Federation](federation.md).
* Use it from the Tracking App: [Tracking App](tracking-app.md).

Command-line tools (`atlas <command>`): `serve`, `migrate`, `token`, `node`, `secret-key`, `sync`, `cache-media`, `make-admin`, `import-legacy`.
