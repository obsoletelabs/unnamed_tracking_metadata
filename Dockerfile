FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY atlas ./atlas
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /data atlas && mkdir /data && chown atlas /data
COPY --from=build /src/.venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 ATLAS_DATABASE_URL=sqlite:////data/atlas.db
USER atlas
WORKDIR /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v2/health', timeout=4)"
CMD ["atlas", "serve", "--host", "0.0.0.0", "--port", "8000"]
