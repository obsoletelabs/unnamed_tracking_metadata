# Local node

A **local node** is your own Atlas: private, cheap to run, and able to cache provider data. It can run entirely on its own, or follow a central authority so you receive its curated records and still keep your own overrides. It runs fine on a Raspberry Pi (the image is built for arm64).

## 1. Get the files

```sh
mkdir -p ~/atlas && cd ~/atlas
for f in compose.yaml .env.example; do
  curl -fsSLO "https://raw.githubusercontent.com/obsoletelabs/unnamed_tracking_metadata/v2/deploy/local/$f"
done
cp .env.example .env
```

## 2. Configure

Edit `.env`:

```dotenv
ATLAS_PUBLIC_URL=http://localhost:8000
ATLAS_INITIAL_ADMIN=alice
ATLAS_BOOTSTRAP_ADMIN_TOKEN=<openssl rand -hex 24>
ATLAS_TAG=2.0.0        # or v2 while following development
```

Optional: `ATLAS_SECRET_KEY` (run `docker run --rm ghcr.io/obsoletelabs/unnamed_tracking_metadata:v2 atlas secret-key`) if you will store peer tokens or sign-in secrets in the admin UI; provider keys (`ATLAS_RAWG_API_KEY`, `ATLAS_STEAM_API_KEY`, `ATLAS_IGDB_*`).

## 3. Start

```sh
docker compose up -d
```

Open <http://localhost:8000>, **Create account** as `alice` (administrator). Email isn't required on a local node, and nothing is sent: messages appear under **Admin → Email**.

By default the port is bound to `127.0.0.1` (this machine only). To share on your network set `ATLAS_BIND=0.0.0.0` (or your LAN address) in `.env` and **put a firewall rule or reverse proxy in front** if others you don't trust share that network.

## 4. Use it

* **Add data:** *Add game* on the site, or ask a provider through the API (`POST /api/v2/providers/steam/fetch`). Provider data is cached on your node only.
* **Connect the Tracking App:** create a `read` token (Admin → Tokens) and use the [plugin](../tracking-app.md).
* **Make it yours:** [Administering](../administering.md): colours, pages, fields.

## 5. Follow a central node (optional)

You need from the authority's administrator: its **address**, its **node id**, and a token with scopes `read` + `sync`.

1. Set `ATLAS_SECRET_KEY` in `.env` and `docker compose up -d` (so the token can be stored encrypted).
2. **Admin → Peers → Add a peer:** address + token → **Look up node**.
3. **Compare the node id shown with the one you were given**, then **Trust this node and replicate from it**.
4. **Sync now**. From then on it follows automatically (every 60 s).

What you get: the authority's curated records, merges, pins, artwork, achievements, custom fields and media types, with the **same ids**. What you keep: provider data, private overrides and your accounts. The authority never receives anything from you. Details: [Federation](../federation.md).

## 6. Sign in with a central account (optional)

If the authority is a sign-in provider you can let your users sign in with their account there: [Sign-in sharing](../sso.md).

## Backups and upgrades

Everything lives in the `atlas-data` volume (database and cached images).

```sh
docker compose stop atlas
docker run --rm -v atlas-local_atlas-data:/d -v "$PWD":/out busybox tar czf /out/atlas-data.tgz -C /d .
docker compose start atlas
```

Upgrade: change `ATLAS_TAG`, then `docker compose pull && docker compose up -d` (migrations run automatically on a SQLite node).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Port already in use | set `ATLAS_PORT=8080` in `.env`. |
| `docker compose pull` says *denied* | sign in to the registry ([Official images](images.md#pulling)) or build your own image. |
| Peer says *not trusted* / bad signature | wrong node id pinned, or the authority's identity changed (it was restored from nothing). Confirm with its administrator and re-add the peer. |
| Artwork missing | artwork is downloaded in the background after the record exists; it needs outbound internet. |
