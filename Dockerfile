# Base pinned by multi-arch index digest (python:3.12-slim, Debian 13.7, Python 3.12.16, image built 2026-11-05).
# Both stages name the same digest literally so Dependabot's docker updater can bump them together
# (see docs/guide/release-verification.md, "Container base image").
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY atlas ./atlas
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime
# Apply Debian security updates published after the base image was built. The newest python:3.12-slim
# (checked 2026-11-08) still ships zlib1g 1:1.3.dfsg+really1.3.1-1+b1 (CVE-2026-76844,
# CVE-2026-85091, HIGH); trixie-security has 1:1.3.dfsg+really1.3.1-1+deb13u1. The publish workflow
# builds this stage with no-cache-filters, so every rebuild re-runs the upgrade instead of reusing a cached layer.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home /data atlas && mkdir -p /data /media && chown atlas /data /media
# same path as the build stage: venv scripts embed absolute interpreter paths
COPY --from=build /src/.venv /src/.venv
ENV PATH=/src/.venv/bin:$PATH PYTHONUNBUFFERED=1 ATLAS_DATABASE_URL=sqlite:////data/atlas.db
USER atlas
WORKDIR /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v2/health', timeout=4)"
CMD ["atlas", "serve", "--host", "0.0.0.0", "--port", "8000"]
