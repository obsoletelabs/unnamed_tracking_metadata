# Running Atlas in a Proxmox LXC

Three ways, from the newest to the most conventional. All give you a small, self-contained Atlas on your Proxmox host, ideal for a **local node**.

| Method | Needs | Notes |
| --- | --- | --- |
| **A. OCI image as an LXC** | Proxmox VE **9.1 or newer** | the new "application container" feature; runs the official image directly, no Docker inside |
| **B. Debian/Ubuntu LXC + Python package** | any Proxmox VE with a Debian 13 / Ubuntu 24.04 template | the most conventional; systemd service |
| **C. LXC with Docker + the compose files** | any Proxmox VE | familiar, a little heavier |

!!! warning "What has and has not been tested"
    The Atlas side of each method has been tested (the image runs and passes its health check; the Python package installs into a clean environment and serves the site). **I could not run Proxmox itself while writing this**, so the Proxmox steps below follow Proxmox's documented behaviour and are marked where I am less certain. Proxmox's OCI feature is labelled a **technology preview** and changes between releases: if a menu or option differs from what is written here, trust your Proxmox version's documentation and tell us so we can fix this page.

## Common preparation

* **Resources:** 1 core, 1 GB RAM, 4 GB disk is plenty for a personal node (the cache grows with artwork).
* **Network:** a bridge (`vmbr0`) with DHCP or a fixed address. You will reach Atlas at `http://<container-ip>:8000`.
* **Data lives in `/data`** (database and cached images). Keep it on a **mount point** so you can upgrade or rebuild the container without losing it, and so you can back it up.
* Decide a **public URL** (`http://<container-ip>:8000`, or the name you put in front of it with a proxy) and put it in `ATLAS_PUBLIC_URL`.

## A. OCI image as an LXC (Proxmox VE 9.1+)

Proxmox VE 9.1 can create an LXC container straight from an OCI/Docker image. It runs the image's own entry point as the container's main process (an *application container*): no init system, no Docker.

### 1. Pull the image

In the web UI: **Datacenter → your storage (one that allows *Container templates*, usually `local`) → CT Templates → Pull from OCI Registry**.

* **Reference:** `ghcr.io/obsoletelabs/unnamed_tracking_metadata:2.0.0` (pin a release; `v2` follows development)
* **File name:** `atlas-2.0.0` (anything you like)

!!! note "Private registry"
    While the package is private, anonymous pulls fail. Either make the package public, or create the template another way (for example `skopeo copy docker://ghcr.io/…:2.0.0 oci-archive:/var/lib/vz/template/cache/atlas-2.0.0.tar` after `skopeo login ghcr.io`), whichever your Proxmox version accepts as a container template. Confirm against your version's documentation.

### 2. Create the container

**Create CT** → on the *Template* step choose the image you pulled. Then:

| Setting | Value |
| --- | --- |
| Unprivileged container | **yes** (recommended) |
| CPU / memory | 1 core, 1024 MB |
| Root disk | 4 GB |
| Network | your bridge; DHCP or a static address |
| **Mount point** | add **mp0**: a volume of 4–10 GB (or a bind mount of a host directory), path **`/data`** |
| Start after creation | **no** (set the environment first) |

### 3. Environment variables

In the container's **Options → Environment** (application containers expose the image's environment and entry point there), add:

```dotenv
ATLAS_ROLE=local
ATLAS_PUBLIC_URL=http://192.168.1.50:8000
ATLAS_DATABASE_URL=sqlite:////data/atlas.db
ATLAS_MEDIA_DIR=/data/media
ATLAS_INITIAL_ADMIN=alice
ATLAS_BOOTSTRAP_ADMIN_TOKEN=<openssl rand -hex 24>
```

Leave the **entry point/command** at the image default (`atlas serve --host 0.0.0.0 --port 8000`).

### 4. Fix data permissions, then start

The image runs as the unprivileged user `atlas` (uid **10001**). In an unprivileged container Proxmox maps container uid *N* to host uid *100000 + N*, so on a **bind-mounted host directory** give that user ownership:

```sh
mkdir -p /srv/atlas-data && chown -R 110001:110001 /srv/atlas-data
pct set <ctid> -mp0 /srv/atlas-data,mp=/data
```

(For a storage-backed volume Proxmox sets ownership for you; if Atlas cannot write `/data`, this is the first thing to check. If your Proxmox runs the entry point as root, no change is needed.)

Start the container and open `http://<container-ip>:8000`. Register `alice`: she is the administrator.

### 5. Upgrading (Method A)

Application containers are not updated in place. To move to a new release: pull the new image tag as a template, **create a new container** from it with the same settings and the **same `/data` mount point** (detach it from the old container and attach it to the new one, or bind-mount the same host directory), start it, and remove the old container once you are happy. Take a backup first.

### 6. Backups (Method A)

* A **storage-backed** mount point with *Backup* ticked is included in Proxmox backups (`vzdump`) with the container.
* A **bind mount** is *not* part of `vzdump`: back it up yourself (`tar czf atlas-data.tgz -C /srv/atlas-data .` while the container is stopped).

## B. Debian/Ubuntu LXC with the Python package

Works on any Proxmox VE. Atlas needs **Python 3.12+**, so use the **Debian 13** or **Ubuntu 24.04** template (Debian 12 ships Python 3.11).

1. **Create CT** from a Debian 13 / Ubuntu 24.04 template: unprivileged, 1 core, 1 GB, 4 GB disk, your network. Start it and open its console (or SSH).
2. Install prerequisites:
   ```sh
   apt update && apt install -y python3 python3-venv python3-pip curl ca-certificates
   ```
3. Install Atlas and the service exactly as in [Ways to install → Python package](install.md#3-python-package-no-docker): the virtual environment in `/opt/atlas`, the environment file in `/etc/atlas`, data in `/var/lib/atlas`, and the `atlas.service` unit.
4. `systemctl enable --now atlas`, then browse to `http://<container-ip>:8000`.

**Data and backups:** `/var/lib/atlas` is on the container's root disk, so a normal Proxmox container backup covers it. **Upgrading:** `pip install --upgrade <wheel>` and `systemctl restart atlas`; snapshot the container first.

## C. LXC with Docker and the compose files

1. Create a Debian 13 / Ubuntu 24.04 container, **unprivileged**, and enable **Options → Features → Nesting** and **keyctl** (required for Docker inside LXC).
2. Install Docker (`curl -fsSL https://get.docker.com | sh`) and clone the repository.
3. Use the `deploy/local` folder: [Local node](local.md) (official image) or add `-f compose.build.yaml --build` to [build from source](install.md#2-build-the-image-from-source).

Docker inside LXC works well but adds a layer; prefer A or B unless you already standardise on compose.

## Reaching it from elsewhere

The container has its own address. For other machines, either browse to `http://<container-ip>:8000` directly (a trusted LAN), or put a reverse proxy in front (the Proxmox host, another container running Caddy or nginx with HTTPS). Set `ATLAS_PUBLIC_URL` to the address people actually use. Do not expose port 8000 to the internet without HTTPS.

## Connecting the rest

* Follow a central node: [Local node → Follow a central node](local.md#5-follow-a-central-node-optional).
* The Tracking App plugin: [Tracking App](../tracking-app.md).
* Check it's healthy: `curl http://<container-ip>:8000/api/v2/ready`.
