# Try federation on one machine

The **lab** runs an authority and a local node side by side on your computer so you can watch replication, overrides, shared fields and sign-in sharing work, before deploying anything. Everything here was exercised end to end against the official image.

!!! warning "Lab only"
    Plain HTTP, development mode, well-known tokens. Both ports bind to `127.0.0.1`. Do not expose it.

## Start it

```sh
git clone -b v2 https://github.com/obsoletelabs/unnamed_tracking_metadata.git
cd unnamed_tracking_metadata/deploy/lab
docker compose up -d
```

| | Address | Administrator |
| --- | --- | --- |
| Authority | <http://authority.lab.localhost:8001> | register **alice** |
| Local node | <http://localhost:8002> | register **bob** |

(`*.localhost` names resolve to your own machine in browsers and `curl`. The local node reaches the authority under the same name through a fixed lab address, so one address works for both the browser and the server.)

Lab tokens: authority `lab-bootstrap-token-0123456789abcdef0123456789`, local `lab-local-token-0123456789abcdef0123456789abcd`.

!!! tip "No registry access?"
    Build the image instead: `docker compose -f compose.yaml -f compose.build.yaml up -d --build` from `deploy/lab` in a clone of the repository.

## 1. Put curated data on the authority

Register **alice** at the authority. Then add records through the API as a curator:

```sh
AUTH=http://authority.lab.localhost:8001; T=lab-bootstrap-token-0123456789abcdef0123456789
curl -s $AUTH/api/v2/observations -H "Authorization: Bearer $T" -H 'content-type: application/json' -d '{
  "connection": {"source_kind":"metadata","source":"curator","source_scope":"lab"},
  "observations": [{"external_id":"1","title":"Hades","release_date":"2020-09-17","facts":{"developer":"Supergiant"}}]}'
```

Also add one **non-curated** record (source `playnite`, kind `plugin`) to see that it does *not* replicate.

## 2. Let the local node follow it

1. On the authority create a token: **Admin → Tokens**, scopes `read` + `sync` (or `POST /api/v2/tokens`).
2. On the local node (register **bob**): **Admin → Peers → Add a peer**, address `http://authority.lab.localhost:8001`, paste the token → **Look up node**.
3. Compare the **node id** shown with the authority's (**Admin → Peers → This node** there). Choose **Trust this node and replicate from it**.
4. Wait a few seconds (the lab syncs every 5 s) or press **Sync now**.

Check: <http://localhost:8002> now lists *Hades* with the **same id** as the authority; the non-curated record is absent.

## 3. Watch changes flow

* On the authority, pin a value (a game page → *Curation* → *Pinned fields*) or edit as the administrator: the local node updates within seconds.
* On the authority, **Admin → Fields**: add a custom field *Engine*. It appears on the local node (`/api/v2/schema`, edit forms).

## 4. Overrides stay local

On the local node pin a different title on the same game. The local page shows yours; the authority is unchanged, and a later upstream change does not overwrite your override.

## 5. Sign in to the local node with an authority account

1. Authority: Settings already has the provider on. Register **dave** (with an email) as an ordinary user.
2. Local node: **Admin → Sign-in sharing → Look up provider**, address `http://authority.lab.localhost:8001`. It shows the **redirect address** to register.
3. Authority: **Admin → Sign-in sharing → Register application**: that redirect address, signature **EdDSA**. Copy the client id and secret.
4. Local node: enter the client id and secret in the confirmation form, tick *Create an account on first sign-in*, **Add provider**.
5. In a private window open the local node, choose the provider button, sign in as **dave** on the authority, **Allow**. You land on the local node signed in as dave.

## Reset or remove

```sh
docker compose down        # stop, keep data
docker compose down -v     # stop and delete everything
```
