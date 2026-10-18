# Backup, restore and upgrades

Atlas is meant to be deployed once and left alone. That only works if you can always get your data back. This page is the
whole story: what to back up, how to restore, how to rehearse it, and how to upgrade safely.

## What a backup contains

`atlas backup create` writes **one archive** (`.tar.gz`) holding:

* every database table (accounts, records, observations, the signed change feed, settings, pages, comments, lists…),
* the cached artwork files,
* a manifest with a SHA-256 for every member, the Atlas version and the database revision.

!!! warning "Archives are secrets"
    They contain password hashes, session records and **this node's signing key**. Anyone holding the key can publish a feed
    that mirrors will accept. Files are written readable by their owner only. Encrypt them before they leave the machine
    (for example `age -p`, `gpg -c` or your backup tool's encryption) and never put one in a public place.

The archive is taken from a single consistent snapshot while Atlas keeps serving. It restores on **either** database, so it is
also how you move a local SQLite node to PostgreSQL.

## Scheduled backups (recommended)

Set a folder on a persistent volume and Atlas writes a verified archive on a schedule, keeping the newest few:

```dotenv
ATLAS_BACKUP_DIR=/backups          # mount a volume here
ATLAS_BACKUP_INTERVAL_HOURS=24     # default
ATLAS_BACKUP_KEEP=14               # default
```

In Docker add `volumes: ["./backups:/backups"]` to the `atlas` service. **Admin → Backups** lists what exists and has a
*Back up now* button. Archives are deliberately not downloadable from the website; copy the folder off the machine with your
usual tool (rsync, restic, Proxmox Backup Server, a NAS share).

Back up the folder **off the machine**. A backup on the same disk dies with the disk.

## Backing up by hand

=== "Docker"

    ```sh
    docker compose exec atlas atlas backup create /data/manual.tar.gz
    docker compose cp atlas:/data/manual.tar.gz .
    ```

=== "From source"

    ```sh
    atlas backup create manual.tar.gz        # add --no-media to leave out artwork (it can be downloaded again)
    ```

An authority running PostgreSQL can also keep using `pg_dump -Fc`; the compose stack's `backup` service does exactly that daily.
Use both if you like. A `pg_dump` restores PostgreSQL only; an Atlas archive restores anywhere.

## Check a backup (do this regularly)

```sh
atlas backup verify manual.tar.gz
```

This re-reads the whole archive and compares every checksum. A backup you have never verified is a hope, not a backup.

## Restore

Restore into a **fresh, empty** database of the same Atlas version:

1. Start with the same software version that made the backup (the manifest shows it; `atlas backup verify` prints it). Point
   `ATLAS_DATABASE_URL` at the new, empty database and set `ATLAS_MEDIA_DIR`.
2. Bring the schema to the backup's revision, then restore, then bring it to the latest:

    ```sh
    atlas migrate --revision <revision printed by verify>
    atlas backup restore manual.tar.gz
    atlas migrate
    ```

3. Start Atlas. Your accounts, records, tokens, settings, pages and **node identity** are back. Mirrors that follow you carry on
   from the same feed position with the same key; nobody has to re-pin anything.

`restore` refuses a database that already has data. `--force` replaces it (it deletes everything first), so use it only when
you mean it. It runs in one transaction: if anything fails the database is left as it was.

!!! tip "Rehearse it"
    Once, on a spare machine or a throwaway LXC: restore your latest archive, sign in, open a few records, and confirm the
    node id (`atlas node`) matches production. That rehearsal is worth more than any amount of reading.

## When the node is lost entirely

* **A mirror or local node** can be rebuilt from its upstream: install, point `ATLAS_UPSTREAM_URL` at it, and it replicates the
  feed again. You lose only local-only things (local overlays, local accounts) so back those up anyway.
* **An authority** can only be rebuilt from a backup, because it is the source of truth. If several official authorities exist
  and one is lost, restore the archive on a new host. Never run two copies of the same authority identity at once; if you restore
  while the old one might still be alive, the feed would fork.
* Keep the **node signing key** safe independently: it is in every archive, but `atlas node` shows the public id you published.

## Upgrading

Updates should be rare. When you do one:

1. `atlas backup create` and `atlas backup verify` first. Copy it off the machine.
2. Pin versions. Use a version tag (for example `ghcr.io/obsoletelabs/unnamed_tracking_metadata:2.0.0`), never `latest`, in
   production.
3. On an authority run migrations explicitly (`docker compose run --rm migrate`, or `atlas migrate`), then restart. The compose
   file already does this in the `migrate` service and keeps `ATLAS_AUTO_MIGRATE=false` on the app.
4. Migrations only go forward. To go back you restore the pre-upgrade backup into a database at the old version, which is why
   step 1 matters.
5. Upgrade mirrors after the authority, never before: a mirror understands older feeds but an old mirror may not understand a
   newer authority.
