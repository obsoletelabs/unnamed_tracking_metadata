"""Per-source media policy (C12 §12.5.5): how images of a source are fetched, served, credited and federated.

The table is code; an administrator overrides it per node with `ATLAS_MEDIA_POLICY` (JSON), for example
`{"steam": {"fetch": "redirect"}}` when a provider's terms forbid caching. The provider defaults are decision
H39 (approve pending a licence review of each provider's terms).

* `fetch`: `proxy` (download, validate, cache and serve from this node), `redirect` (never download; the image
  route answers 307 to the origin, only for `redirect_hosts`) or `none` (listed for curators, never served).
* `federate`: whether rows of the source may enter the signed feed at all. Provider rows never do (their data is
  not redistributable); curated rows do unless their licence keeps them out (§12.5.5, H47).
* `licence`, `attribution`: defaults for rows that state none.
* `redirect_hosts`: the only hosts `/api/v2/artwork/{id}/image` may redirect to for this source (H50).
* `follow_hosts`: when set, the only hosts (or their subdomains) this node's own download may be redirected to for
  this source, each hop still through the SSRF guard (C15 §2.7: Open Library covers redirect to archive.org storage).
  Empty: any public host, as before. It never widens `redirect_hosts` for browsers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

FETCH = ("proxy", "redirect", "none")


@dataclass(frozen=True)
class MediaPolicy:
    fetch: str = "proxy"
    federate: bool = True
    licence: str | None = None
    attribution: str | None = None
    redirect_hosts: tuple[str, ...] = ()
    follow_hosts: tuple[str, ...] = ()


_STEAM_HOSTS = ("cdn.akamai.steamstatic.com", "shared.akamai.steamstatic.com", "cdn.cloudflare.steamstatic.com",
                "shared.cloudflare.steamstatic.com", "cdn.fastly.steamstatic.com", "shared.fastly.steamstatic.com",
                "steamcdn-a.akamaihd.net", "media.steampowered.com")

#: Code defaults (H39). Every other source (curator, community, manual, uploads) gets `MediaPolicy()`.
POLICIES: dict[str, MediaPolicy] = {
    "steam": MediaPolicy(federate=False, licence="provider_terms", attribution="Steam", redirect_hosts=_STEAM_HOSTS),
    "igdb": MediaPolicy(federate=False, licence="provider_terms", attribution="IGDB",
                        redirect_hosts=("images.igdb.com",)),
    "rawg": MediaPolicy(federate=False, licence="provider_terms", attribution="RAWG",
                        redirect_hosts=("media.rawg.io",)),
    # C15 §2.7: covers belong to their publishers; cached here, never federated. covers.openlibrary.org answers
    # 302 -> archive.org -> <node>.us.archive.org (checked live 2026-11-11), so the download follows those hosts only.
    "openlibrary": MediaPolicy(federate=False, licence="provider_terms", attribution="Open Library",
                               redirect_hosts=("covers.openlibrary.org",),
                               follow_hosts=("covers.openlibrary.org", "archive.org")),
}
_FIELDS = {"fetch", "federate", "licence", "attribution", "redirect_hosts", "follow_hosts"}


def check_overrides(overrides: dict[str, Any]) -> None:
    """Validate `ATLAS_MEDIA_POLICY` at startup; a typo must not silently keep the default."""
    from ..vocab import LICENCES

    if len(overrides) > 64:
        raise ValueError("media_policy: at most 64 sources")
    for source, fields in overrides.items():
        if not isinstance(fields, dict) or set(fields) - _FIELDS:
            raise ValueError(f"media_policy[{source[:40]!r}]: an object with {sorted(_FIELDS)}")
        if "fetch" in fields and fields["fetch"] not in FETCH:
            raise ValueError(f"media_policy[{source[:40]!r}].fetch must be one of {FETCH}")
        if "federate" in fields and not isinstance(fields["federate"], bool):
            raise ValueError(f"media_policy[{source[:40]!r}].federate must be true or false")
        if fields.get("licence") is not None and fields["licence"] not in LICENCES:
            raise ValueError(f"media_policy[{source[:40]!r}].licence must be one of {LICENCES}")
        for name in ("redirect_hosts", "follow_hosts"):
            hosts = fields.get(name, [])
            if not isinstance(hosts, list) or len(hosts) > 32 or not all(isinstance(h, str) and 0 < len(h) <= 253
                                                                       for h in hosts):
                raise ValueError(f"media_policy[{source[:40]!r}].{name} must be a list of host names")


def policy(settings: Any, source: str) -> MediaPolicy:
    """The effective policy of a source on this node: the code default with the admin's overrides applied."""
    base = POLICIES.get(source, MediaPolicy())
    override = (getattr(settings, "media_policy", None) or {}).get(source)
    if not override:
        return base
    fields = dict(override)
    for name in ("redirect_hosts", "follow_hosts"):
        if name in fields:
            fields[name] = tuple(h.lower() for h in fields[name])
    return replace(base, **fields)
