# Choose your setup

Atlas runs in two common shapes. Both use the **same official container image**; only the configuration differs.

| | **Authoritative server** | **Local node** |
| --- | --- | --- |
| Who it's for | a community or organisation running the shared, public source of truth | one person, household or team; a private cache; optionally following a central node |
| Role | `authority` | `local` |
| People | many accounts; open or closed registration | usually just you |
| Database | PostgreSQL 16 | SQLite in a volume |
| Network | public, HTTPS, domain name | localhost or your LAN |
| Email | real SMTP (confirmed addresses are required to contribute) | not needed |
| Backups | daily database dumps + restore drill | copy the volume |
| Publishes a change feed | yes | no |
| Follows other nodes | no | yes (optional) |
| Guide | [Authoritative server](authority.md) | [Local node](local.md) |

Not sure? Start with a [local node](local.md); it takes five minutes and can follow a central node later. To see both working together before you commit to anything, [try federation on one machine](lab.md).

## What you get from the official image

* `ghcr.io/obsoletelabs/unnamed_tracking_metadata` for **linux/amd64** and **linux/arm64** (servers, laptops, Raspberry Pi, Apple silicon).
* Runs as a **non-root** user, exposes port 8000, has a health check.
* Contains the web site, API, migrations and the `atlas` command-line tool.
* Built and **smoke-tested by CI on every release**, then scanned for known vulnerabilities.

See [Ways to install](install.md) (image, build from source, Python package), [Official images](images.md) for tags and pinning, and [Proxmox LXC](proxmox.md).

## The three files you need

For each setup there is a ready-made folder in the repository under `deploy/`:

| Folder | Contents |
| --- | --- |
| `deploy/authority/` | `compose.yaml` (Atlas, PostgreSQL, Caddy with automatic HTTPS, daily backups), `Caddyfile`, `.env.example`, `restore.sh` |
| `deploy/local/` | `compose.yaml`, `.env.example` |
| `deploy/lab/` | `compose.yaml`: an authority and a local node on one machine |
| each folder | `compose.build.yaml`: add `-f compose.build.yaml --build` to build the image from source instead of pulling it |

Each guide starts with `git clone`; the folder is all you use afterwards.

## After it's running

* Configure it from the browser: [Administering](../administering.md).
* Connect your apps: [Tracking App](../tracking-app.md).
* Keep it healthy: [Operations](../deployment.md).
