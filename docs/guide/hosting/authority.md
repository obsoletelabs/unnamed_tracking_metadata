# Authoritative server

An **authority** is the public source of truth for a community: it accepts contributions and curation, publishes the signed change feed that mirrors and local nodes follow, and can sign people in to other software. This guide takes you from an empty server to a hardened, backed-up, federated deployment using the **official images**.

!!! info "What you need"
    * A Linux server (1 vCPU / 2 GB RAM is a reasonable starting point; no load testing has been done, so size for your own traffic) with **Docker + Compose** and ports **80** and **443** open.
    * A **DNS name** pointing at it (for example `atlas.example.org`).
    * An **SMTP account** for email (verification and password reset). Authorities require confirmed addresses before people can contribute.
    * About 30 minutes.

## 1. Get the deployment files

```sh
mkdir -p /opt/atlas && cd /opt/atlas
for f in compose.yaml Caddyfile .env.example restore.sh; do
  curl -fsSLO "https://raw.githubusercontent.com/obsoletelabs/unnamed_tracking_metadata/v2/deploy/authority/$f"
done
chmod +x restore.sh
cp .env.example .env
```

(Private repository? Use `git clone` with your credentials and copy `deploy/authority/` instead.)

## 2. Fill in `.env`

Generate the secrets:

```sh
openssl rand -hex 24                                    # ATLAS_BOOTSTRAP_ADMIN_TOKEN
openssl rand -hex 24                                    # POSTGRES_PASSWORD
docker run --rm ghcr.io/obsoletelabs/unnamed_tracking_metadata:v2 atlas secret-key   # ATLAS_SECRET_KEY
```

Set at least:

```dotenv
ATLAS_DOMAIN=atlas.example.org
ATLAS_PUBLIC_URL=https://atlas.example.org
ATLAS_INITIAL_ADMIN=alice
ATLAS_BOOTSTRAP_ADMIN_TOKEN=…
ATLAS_SECRET_KEY=…
POSTGRES_PASSWORD=…
ATLAS_MAIL_BACKEND=smtp
ATLAS_SMTP_HOST=… ATLAS_SMTP_USER=… ATLAS_SMTP_PASSWORD=… ATLAS_MAIL_FROM=Atlas <noreply@example.org>
ATLAS_TAG=2.0.0        # pin a release
```

!!! warning "Keep `.env` safe and backed up"
    `ATLAS_SECRET_KEY` encrypts stored peer tokens and OIDC secrets; without it they cannot be read again. `chmod 600 .env`.

Production mode **refuses to start** unless the public URL is `https://`, the database is PostgreSQL, and (with open registration) an initial admin or bootstrap token is set. That is deliberate.

## 3. Start it

```sh
docker compose up -d
docker compose ps          # db healthy, migrate exited 0, atlas + caddy running
docker compose logs -f atlas
```

What runs: **PostgreSQL**, a one-shot **migrate** job (`atlas migrate`), **Atlas** (not published on the host; only the proxy can reach it, which is what makes trusting its forwarded headers safe), **Caddy** (automatic Let's Encrypt certificates for your domain), and a **backup** container (daily `pg_dump`, 14 days kept, in `./backups`).

Check: <https://atlas.example.org/api/v2/health> returns `{"status":"ok"…}`, and <https://atlas.example.org/api/v2/ready> too.

## 4. First sign-in and basic setup

1. Open the site and **Create account** with the username in `ATLAS_INITIAL_ADMIN` and an email address. You are the administrator.
2. Confirm your email (Admin → Email shows messages if SMTP isn't working yet; fix SMTP, then use *Mark confirmed* in the user list only as a stopgap).
3. **Admin → Settings:** name, colours, logo, registration (`open`/`closed`), thresholds. Start with registration **closed** if you want to invite people first.
4. **Admin → Pages:** edit *About* and *Contribution rules*.
5. **Admin → Fields:** add media types or custom fields if you want them.
6. Optionally add provider keys to `.env` (`ATLAS_RAWG_API_KEY`, `ATLAS_IGDB_*`, `ATLAS_STEAM_API_KEY`) and `docker compose up -d`.

## 5. Make it useful to others

| Goal | How |
| --- | --- |
| Let local nodes and mirrors follow you | **Admin → Tokens:** create a token with scopes `read` + `sync` per follower; give them the address and your **node id** (Admin → Peers → This node) over a trusted channel. See [Federation](../federation.md). |
| Let apps read metadata | A token with only `read`. The [Tracking App plugin](../tracking-app.md) uses this. |
| Let people sign in to other software with their account | Settings → *Act as a sign-in provider*, then **Admin → Sign-in sharing**. See [Sign-in sharing](../sso.md). |
| Curators | Promote trusted people (user list) or let reputation grant review rights. |

## 6. Backups, and a restore drill

The `backup` container writes `./backups/atlas-YYYYmmdd-HHMMSS.dump` daily. Copy them **off the server** (rsync, object storage) and back up the media volume too:

```sh
docker run --rm -v atlas_media:/m -v "$PWD":/out busybox tar czf /out/media.tgz -C /m .
```

Practise a restore on a scratch machine (same `.env`, same files):

```sh
./restore.sh backups/atlas-20261018-030000.dump     # replaces the database contents and restarts
```

The node's **identity** (the key followers pin) lives in the database, so restoring the database restores the identity.

## 7. Upgrading

```sh
# 1. back up (above)  2. read the release notes  3. change the tag
sed -i 's/^ATLAS_TAG=.*/ATLAS_TAG=2.1.0/' .env
docker compose pull && docker compose up -d     # migrate runs first, then Atlas restarts
docker compose logs --tail=50 migrate atlas
```

Migrations only move forward. If something is wrong, restore the backup and put the old tag back.

## 8. Hardening checklist

* [ ] `.env` is mode 600 and not in a repository.
* [ ] Only ports 80/443 (and SSH) are reachable from outside; PostgreSQL is **not** published.
* [ ] SMTP works (send yourself a password reset) and SPF/DKIM are set for your sending domain.
* [ ] Off-site backups and a tested restore.
* [ ] `ATLAS_TAG` pinned; you know how you'll learn about security updates.
* [ ] Registration policy decided (open/closed) and the Rules page written.
* [ ] At least two administrators (so one can recover the other's account).
* [ ] An uptime check on `/api/v2/ready`.

## Scaling out

More replicas of the `atlas` service work behind the same proxy: sessions, tokens and rate limits live in the database. Keep `ATLAS_AUTO_MIGRATE=false` (the compose file does) so only the migrate job changes the schema. Writes are serialised by a database lock, which is fine for metadata and the first limit you would meet under very heavy ingestion. See [Operations](../deployment.md).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Caddy can't get a certificate | DNS doesn't point here yet, or 80/443 are blocked. |
| `atlas` keeps restarting | `docker compose logs atlas`: usually one of the production checks (HTTPS URL, PostgreSQL, initial admin). |
| People never get email | SMTP settings (Admin → Email shows the error per message). |
| Followers report *not trusted* / *gap* | They pinned a different node id, or you restored an older backup. See [Federation](../federation.md#operating-it). |
