"""Minimal Atlas v2 client (single file, depends only on httpx). Intended for the tracking-app plugin."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import httpx


class AtlasClientError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


class AtlasClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None, timeout: float = 15.0):
        self._base = base_url.rstrip("/") + "/api/v2"
        self._http = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._http.request(method, self._base + path, headers=self._headers, **kw)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message") or resp.json().get("detail")
            except ValueError:
                detail = resp.text[:200]
            raise AtlasClientError(resp.status_code, str(detail))
        return resp.json()

    def search(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        return self._req("GET", "/games", params={"q": query, "limit": limit})["results"]

    def game(self, atlas_id: str) -> dict[str, Any]:
        """Follows merge redirects; `redirected_from` is set when the id was merged away."""
        return self._req("GET", f"/games/{atlas_id}")

    def resolve(self, items: Iterable[dict[str, Any]], batch_size: int = 200) -> Iterator[dict[str, Any]]:
        """Resolve many library entries; yields one result per input item, in order."""
        buf: list[dict[str, Any]] = []
        for it in items:
            buf.append(it)
            if len(buf) >= batch_size:
                yield from self._req("POST", "/resolve", json={"items": buf})["results"]
                buf = []
        if buf:
            yield from self._req("POST", "/resolve", json={"items": buf})["results"]

    def provider_search(self, provider: str, query: str) -> list[dict[str, Any]]:
        return self._req("GET", f"/providers/{provider}/search", params={"q": query})["results"]

    def provider_fetch(self, provider: str, external_id: str) -> dict[str, Any]:
        return self._req("POST", f"/providers/{provider}/fetch", json={"external_id": external_id})

    def submit(
        self, source: str, scope: str, observations: list[dict[str, Any]], kind: str = "plugin"
    ) -> list[dict[str, Any]]:
        body = {"connection": {"source_kind": kind, "source": source, "source_scope": scope},
                "observations": observations}
        return self._req("POST", "/observations", json=body)["results"]
