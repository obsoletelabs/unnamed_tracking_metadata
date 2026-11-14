"""Look a node up before following it (CONTRACT-C13 §7.1).

One probe makes at most two calls with the operator's token: `/api/v2/sync/info` (read scope) and
`/api/v2/sync/changes?since=<max>&limit=1` (sync scope), which is always made, so the own head and whether the token
can read the change feed are known rather than guessed. The result lists the node's own feed first and every
authority feed it relays, each with its head, its trust state and its warnings, and recommends one.

Used by the admin form (`POST /admin/peers/probe`, which adds a session-bound `probe_token` to each option) and by
`GET /api/v2/admin/peers/probe` (tests, scripts and the CLI; no token: adding a peer stays an admin-form action).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import feed_schema, sync
from .models import Peer, ReplicatedFeed
from .signing import key_id_for

#: §7.1: the probe asks for the entries after this sequence, so it learns the head (and proves the sync scope)
#: without downloading anything.
PROBE_SINCE = 2**53 - 1
#: §7.1: both probe calls, and the one-page pull after adding a peer (§7.3), use this timeout, per request (it
#: bounds each HTTP call, not the probe or the pull as a whole).
PROBE_TIMEOUT = 10


class ProbeError(Exception):
    """The node could not be verified. `unreachable`: it did not answer in time (never reported as empty)."""

    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


def clean_url(raw: str) -> str:
    """A plain http(s) base address, without credentials, query or fragment. Raises ValueError."""
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or len(url) > 300 or any(c.isspace() for c in url):
        raise ValueError("Use a plain http(s) address such as https://atlas.example.org")
    return url


def fingerprint(node_id: str) -> str:
    return " ".join(node_id[i:i + 4] for i in range(0, len(node_id), 4))


def node_kind(info: dict) -> str:
    k = str(info.get("kind", info.get("role", "")))
    return k if k in ("official", "community", "mirror", "local") else "community"


def verified(node_id: object, key: object) -> bool:
    try:
        return isinstance(node_id, str) and isinstance(key, str) and key_id_for(base64.b64decode(key)) == node_id
    except ValueError:
        return False


def trust(session: Session, node_id: str, public_key: str, peers_now: dict[str, str]) -> str:
    """§7.1 `trust`: `already_peer`, `key_mismatch` (the key differs from the one pinned or retained for that node
    id: never re-pinned from here) or `new`."""
    retained = session.get(ReplicatedFeed, node_id)
    pinned = peers_now.get(node_id)
    if (pinned is not None and pinned != public_key) or (retained is not None and retained.public_key != public_key):
        return "key_mismatch"
    return "already_peer" if pinned is not None else "new"


def feed_options(session: Session, info: dict, own_head: int | None = None, *,
                 token_for: Callable[[dict], str] | None = None) -> list[dict]:
    """The feeds a probed node offers (§7.1-§7.2): its own first, then each authority feed it relays, with how much
    of each it holds. `own_head` is what `/sync/changes` reported (an older upstream sends no `own_head` in
    `/sync/info`). A relayed feed is recommended only when the own feed is known to be empty (a pure mirror).
    `token_for(option)` adds each option's `probe_token` (the admin form)."""
    peers_now = {n: k for n, k in session.execute(select(Peer.node_id, Peer.public_key))}
    if own_head is None and type(info.get("own_head")) is int:
        own_head = info["own_head"]
    options = [{"follow": sync.FOLLOW_OWN, "node_id": str(info["node_id"]), "public_key": str(info["public_key"]),
                "via": None, "head": own_head, "empty": own_head == 0, "role": info.get("role", "?"),
                "kind": node_kind(info), "label": "This node's own feed", "origin": "own"}]
    for f in info.get("feeds", []) or []:
        if not isinstance(f, dict) or not verified(f.get("node_id"), f.get("public_key")):
            continue
        head = f.get("head") if type(f.get("head")) is int else None
        options.append({"follow": sync.FOLLOW_RELAYED, "node_id": f["node_id"], "public_key": f["public_key"],
                        "via": str(info["node_id"]), "head": head, "empty": bool(f.get("empty", head == 0)),
                        "role": "authority", "kind": "official", "label": "An authority feed it relays",
                        "origin": "relayed"})
    for o in options:
        o["fp"] = fingerprint(o["node_id"])
        o["trust"] = trust(session, o["node_id"], o["public_key"], peers_now)
        o["already"] = o["trust"] == "already_peer"
        o["disabled"] = o["trust"] != "new"
        if token_for is not None:
            o["probe_token"] = token_for(o)
        warnings = []
        if o["empty"]:
            warnings.append("empty_feed")
        if o["head"] is None:
            warnings.append("head_unknown")
        if o["follow"] == sync.FOLLOW_RELAYED and own_head is not None and o["head"] is not None \
                and o["head"] < own_head:
            warnings.append("smaller_than_own")
        o["warnings"] = warnings
    own = options[0]
    if own_head == 0 and any(o["head"] for o in options[1:]):
        own["warnings"].append("own_feed_empty_but_relayed_has_data")
    usable = [o for o in options if not o["disabled"]]
    if own_head == 0:  # a genuine pure mirror: the relayed feed that holds the most
        best = max((o for o in usable if o["follow"] == sync.FOLLOW_RELAYED and o["head"]),
                   key=lambda o: o["head"], default=None)
    else:  # the own feed whenever it is not known to be empty, even when its head is unknown (an older upstream)
        best = own if own in usable else None
    for o in options:
        o["recommended"] = o is best
    return options


def probe_head(http: httpx.Client, target: str, headers: dict) -> tuple[int | None, bool | None]:
    """`/sync/changes?since=<max>&limit=1`, always made: the own head, and whether the token has the sync scope.
    (None, False) for a 401/403; (None, None) when the call failed otherwise."""
    try:
        resp = http.get(f"{target}/api/v2/sync/changes", params={"since": PROBE_SINCE, "limit": 1},
                        headers={**headers, "Atlas-Protocol": feed_schema.PROTOCOL}, timeout=PROBE_TIMEOUT)
    except httpx.HTTPError:
        return None, None
    if resp.status_code in (401, 403):
        return None, False
    if resp.status_code != 200:
        return None, None
    try:
        head = resp.json().get("head")
    except (ValueError, AttributeError):
        return None, True
    return (head if type(head) is int and 0 <= head < 2**63 else None), True


def probe(session: Session, http: httpx.Client, target: str, token: str | None, *,
          token_for: Callable[[dict], str] | None = None) -> dict[str, Any]:
    """Probe `target` (already cleaned) and describe what it offers, in the shape of §7.1:
    `{node, token, records, feeds, default_feed}` (plus `info`, the raw `/sync/info`). Raises ProbeError."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = http.get(f"{target}/api/v2/sync/info", headers=headers, timeout=PROBE_TIMEOUT)
        resp.raise_for_status()
        info = resp.json()
        if not isinstance(info, dict) or not verified(info.get("node_id"), info.get("public_key")):
            raise ValueError("the node's id does not match its public key")
        own_head, can_sync = probe_head(http, target, headers)
        options = feed_options(session, info, own_head, token_for=token_for)
    except httpx.TimeoutException as exc:
        raise ProbeError("it did not answer in time (unreachable)", unreachable=True) from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ProbeError(str(exc)) from exc
    raw = info.get("protocol")
    try:
        sync.negotiate(info)
        protocol_ok = True
    except sync.SyncRefused:
        protocol_ok = False
    if not protocol_ok:
        for o in options:
            o["warnings"].append("protocol_refused")
    records = info.get("records")
    if not (isinstance(records, dict) and all(type(records.get(k)) is int for k in sync.RECORD_COUNTS)):
        records = None
    best = next((o for o in options if o["recommended"]), None)
    return {"node": {"node_id": str(info["node_id"]), "fp": fingerprint(str(info["node_id"])),
                     "name": str(info.get("name") or "")[:80], "role": info.get("role", "?"), "kind": node_kind(info),
                     "protocol": str(raw or feed_schema.LEGACY_PROTOCOL)[:16], "protocol_ok": protocol_ok},
            "token": {"read": True, "sync": can_sync},
            "records": {k: records[k] for k in sync.RECORD_COUNTS} if records else None,
            "feeds": options, "default_feed": best["node_id"] if best else None, "info": info}


def public(result: dict[str, Any]) -> dict[str, Any]:
    """The probe as the API returns it (§7.1): no raw `info`, no form internals."""
    keep = ("node_id", "public_key", "origin", "via", "head", "empty", "trust", "recommended", "warnings", "fp")
    return {"node": result["node"], "token": result["token"], "records": result["records"],
            "feeds": [{k: o[k] for k in keep} for o in result["feeds"]], "default_feed": result["default_feed"],
            "probe_token": None}
