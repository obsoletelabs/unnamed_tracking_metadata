# Official images

## Where they are

```
ghcr.io/obsoletelabs/unnamed_tracking_metadata
```

Built by the repository's **Publish container** workflow (multi-architecture `linux/amd64` + `linux/arm64`, with build provenance and an SBOM attached by BuildKit), smoke-tested by starting the *published* image and calling its health endpoint, and scanned with Trivy (High/Critical, fixed vulnerabilities fail the build).

## Tags

| Tag | Meaning | Use it for |
| --- | --- | --- |
| `2.0.0`, `2.0` | a release (git tag `v2.0.0`); `2.0` follows patch releases | **production**: pin an exact version |
| `latest` | the newest build of the `main` branch | trying things out |
| `v2` | the newest build of the `v2` branch (the current development line, until it is merged to `main`) | following development |
| `sha-<short>` | one exact commit | reproducing a build |

!!! tip "Pin in production"
    Put `ATLAS_TAG=2.0.0` (or whichever release you tested) in `.env`. Upgrade deliberately, after a backup, by changing the number. Never run an authority on a moving tag.

You can also pin by **digest** (`ghcr.io/…@sha256:…`), which cannot change underneath you.

## Pulling

If the repository/package is **public**, `docker pull ghcr.io/obsoletelabs/unnamed_tracking_metadata:2.0.0` just works.

While it is **private**, sign in first with a GitHub personal access token that has the `read:packages` scope:

```sh
echo "$GITHUB_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin
```

(The deploy folders use whichever image your login allows.)

## Using your own build instead

Every `deploy/` folder has a `compose.build.yaml`: `docker compose -f compose.yaml -f compose.build.yaml up -d --build`. Details and the plain `docker build` route: [Ways to install](install.md#2-build-the-image-from-source). There is also a [Python package](install.md#3-python-package-no-docker).

## What's inside

* Python 3.12 slim base, a pre-built virtual environment (no compilers in the final image), no shell history or build tools.
* User `atlas` (uid 10001); working directory and default SQLite database in `/data` (a volume).
* `CMD ["atlas", "serve", "--host", "0.0.0.0", "--port", "8000"]`; the `atlas` CLI can run one-off jobs: `docker compose exec atlas atlas <command>` ([commands](../quickstart.md)).
* `HEALTHCHECK` calls `/api/v2/health`.

## The documentation image

Each build of the documentation site is also published, so you can read these docs offline or host them yourself:

```sh
docker run --rm -p 8080:8080 ghcr.io/obsoletelabs/unnamed_tracking_metadata-docs:v2   # http://localhost:8080
```
