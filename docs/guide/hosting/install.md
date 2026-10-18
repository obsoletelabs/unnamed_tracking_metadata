# Ways to install Atlas

Pick one. **The official Docker image is the preferred route**; the others exist for people who want to build it themselves or do not use Docker.

| | When to choose it | Tested here |
| --- | --- | --- |
| **1. Official Docker image** (recommended) | almost always; the same artifact CI built, smoke-tested and scanned | yes: published by CI, started and checked in CI |
| **2. Docker image built from source** | you want to audit or modify the code, or can't reach the registry | yes |
| **3. Python package** (wheel) | no Docker (a VM, a bare LXC, a Raspberry Pi), or you prefer systemd | yes: clean-environment install, migrations, web UI |
| **4. Proxmox LXC** | you run Proxmox | method B/C below on any recent Proxmox; the OCI method needs Proxmox VE 9.1+ — see [Proxmox LXC](proxmox.md) |

Every example in this guide (the `deploy/` folders) works with routes 1 and 2 by changing one command.

## 1. Official Docker image (recommended)

```sh
git clone -b v2 https://github.com/obsoletelabs/unnamed_tracking_metadata.git
cd unnamed_tracking_metadata/deploy/local        # or authority/ or lab/
cp .env.example .env                             # edit it
docker compose up -d
```

The compose files pull `ghcr.io/obsoletelabs/unnamed_tracking_metadata:${ATLAS_TAG}`. Pin `ATLAS_TAG` to a release. If the package is private you must `docker login ghcr.io` first ([Official images](images.md#pulling)).

## 2. Build the image from source

Same folders, one extra file:

```sh
cd unnamed_tracking_metadata/deploy/local
cp .env.example .env
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

`compose.build.yaml` points the service at the repository's `Dockerfile` (two stages, non-root, health check) and tags it `atlas:local`. Behind a restrictive network, build once by hand: `docker build --network=host -t atlas:local .` and then start with the same two `-f` flags (without `--build`). To update: `git pull` then rerun the command.

Or build and run the image yourself:

```sh
docker build -t atlas:local .
docker run -d --name atlas -p 127.0.0.1:8000:8000 -v atlas-data:/data \
  -e ATLAS_ROLE=local -e ATLAS_INITIAL_ADMIN=alice atlas:local
```

## 3. Python package (no Docker)

Needs **Python 3.12 or newer** (Debian 13 or Ubuntu 24.04 have it; Debian 12 does not).

Install the wheel from a release or CI artifact, or directly from the repository:

```sh
python3 -m venv /opt/atlas/venv
/opt/atlas/venv/bin/pip install atlas_metadata-2.0.0-py3-none-any.whl
# or, from git (a private repository needs a token in the URL):
/opt/atlas/venv/bin/pip install "atlas-metadata @ git+https://github.com/obsoletelabs/unnamed_tracking_metadata@v2"
```

Releases publish the wheel and source archive on the GitHub release page; every build also uploads them as the `python-package` artifact of the *Python package* workflow. The wheel contains the web site, migrations and CLI (`atlas`); CI installs it into a clean environment and starts it on every build.

Configure with environment variables (a file is easiest) and run:

```sh
mkdir -p /etc/atlas /var/lib/atlas
cat > /etc/atlas/atlas.env <<'ENV'
ATLAS_ROLE=local
ATLAS_PUBLIC_URL=http://localhost:8000
ATLAS_DATABASE_URL=sqlite:////var/lib/atlas/atlas.db
ATLAS_MEDIA_DIR=/var/lib/atlas/media
ATLAS_INITIAL_ADMIN=alice
ATLAS_BOOTSTRAP_ADMIN_TOKEN=change-me-to-32+-random-characters
ENV
chmod 600 /etc/atlas/atlas.env
set -a; . /etc/atlas/atlas.env; set +a
/opt/atlas/venv/bin/atlas migrate
/opt/atlas/venv/bin/atlas serve --host 127.0.0.1 --port 8000
```

### Keep it running with systemd

```ini
# /etc/systemd/system/atlas.service
[Unit]
Description=Atlas metadata server
After=network-online.target
Wants=network-online.target

[Service]
User=atlas
Group=atlas
EnvironmentFile=/etc/atlas/atlas.env
WorkingDirectory=/var/lib/atlas
ExecStartPre=/opt/atlas/venv/bin/atlas migrate
ExecStart=/opt/atlas/venv/bin/atlas serve --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/atlas

[Install]
WantedBy=multi-user.target
```

```sh
useradd --system --home /var/lib/atlas --shell /usr/sbin/nologin atlas
chown -R atlas:atlas /var/lib/atlas
systemctl daemon-reload && systemctl enable --now atlas
journalctl -u atlas -f
```

Upgrade: `pip install --upgrade <new wheel>` then `systemctl restart atlas` (migrations run first). Back up `/var/lib/atlas` and `/etc/atlas`.

!!! note "An authority from the Python package?"
    Possible (PostgreSQL, a reverse proxy, `ATLAS_ENVIRONMENT=production`), but the supported, tested production path is the container deployment in [Authoritative server](authority.md).
