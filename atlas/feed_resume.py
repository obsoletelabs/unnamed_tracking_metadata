"""Resuming a feed-frozen authority (C16 §6.4 rule 2).

An authority restored from a backup is behind every follower that pulled one entry after the backup was taken, and
signing on from it would reuse a seq with other bytes: an undetectable fork. `backup.restore` therefore writes
`NodeState["feed_restored"]` and `feed.append_unchecked` refuses to sign (409 `feed_frozen`) until one of:

* `resume_from_nodes`: fetch this node's own signed entries above the restored head from nodes that retained them,
  verify every signature and contiguity, take the longest verified tail, insert it into `change_log` verbatim (the
  same bytes, never re-signed), move `feed_seq`, apply each entry to local state, then unfreeze. A source can only
  withhold, which the longest-tail rule and the recorded follower positions expose.
* `confirm_no_follower_ahead`: the operator's statement that no follower pulled above the restored head. Refused
  while any recorded follower position exceeds the head, unless that token is revoked in the same call.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import accounts, feed, feed_schema, secrets_box, signing, sync, wire
from .models import ChangeLog, NodeState, Peer, Token
from .service import AtlasError

log = logging.getLogger("atlas")

PAGE = 1000
MAX_TAIL = 200_000
FOLLOWER_SINCE = "follower_since:"
RESUMED = "feed_resumed"


class ResumeError(ValueError):
    pass


@dataclass
class Source:
    label: str
    url: str
    token: str | None


def _head(session: Session) -> int:
    return int(session.scalar(select(func.max(ChangeLog.seq))) or 0)


def follower_positions(session: Session) -> dict[str, int]:
    """`{token id: highest since it asked for}`, the authority's evidence of how far each follower got."""
    out: dict[str, int] = {}
    for row in session.scalars(select(NodeState).where(NodeState.key.like(FOLLOWER_SINCE + "%"))):
        if str(row.value).isdigit():
            out[row.key.removeprefix(FOLLOWER_SINCE)] = int(row.value)
    return out


def _unfreeze(session: Session, how: str, head: int, **detail: Any) -> None:
    session.execute(NodeState.__table__.delete().where(NodeState.key == feed.FEED_RESTORED))
    record = {"at": datetime.now(UTC).isoformat(), "how": how, "head": head, **detail}
    row = session.get(NodeState, RESUMED)
    if row is None:
        session.add(NodeState(key=RESUMED, value=json.dumps(record, sort_keys=True)))
    else:
        row.value = json.dumps(record, sort_keys=True)
    accounts.audit(session, None, "feed_resume", how, record)
    session.flush()


def confirm_no_follower_ahead(session: Session, *, revoke: list[str] | None = None) -> dict[str, Any]:
    """Unfreeze on the operator's statement (audited). Refuses while a recorded follower position exceeds the
    restored head, naming the token, unless `revoke` lists that token."""
    state = feed.frozen(session)
    if state is None:
        raise ResumeError("the feed is not frozen")
    head = _head(session)
    revoke = revoke or []
    ahead = {t: n for t, n in follower_positions(session).items() if n > head}
    blocking = {t: n for t, n in ahead.items() if t not in revoke}
    if blocking:
        token, since = sorted(blocking.items())[0]
        raise ResumeError(f"token {token} already pulled up to seq {since}, above the restored head {head}: a follower "
                          "is ahead of this feed. Use `atlas feed resume --from-node`, or revoke that token with "
                          f"`--revoke-follower {token}` if it is lost")
    now = datetime.now(UTC)
    for token in revoke:
        row = session.get(Token, token)
        if row is None:
            raise ResumeError(f"no token {token}")
        row.revoked_at = row.revoked_at or now
    _unfreeze(session, "confirm_no_follower_ahead", head, revoked=sorted(revoke), ahead=ahead)
    return {"resumed": "confirm_no_follower_ahead", "head": head, "revoked": sorted(revoke)}


# ---- --from-node -----------------------------------------------------------------------------------------------

def sources(maker: sessionmaker[Session], settings: Any, names: list[str] | None, token: str | None = None) -> list[Source]:
    """`--from-node` arguments to sources: a URL (the peer with that URL supplies the token), or a peer's name or node
    id. Without any, every enabled peer and the environment upstream."""
    found: list[Source] = []
    with maker() as session:
        peers = list(session.scalars(select(Peer).where(Peer.enabled.is_(True))))

        def secret(peer: Peer) -> str | None:
            return secrets_box.decrypt(settings, peer.token_enc) if peer.token_enc else None

        if not names:
            if settings.upstream_url:
                found.append(Source("env", settings.upstream_url.rstrip("/"), settings.upstream_token))
            found.extend(Source(p.name, p.url.rstrip("/"), secret(p)) for p in peers)
            return found
        for name in names:
            if name.startswith(("http://", "https://")):
                peer = next((p for p in peers if p.url.rstrip("/") == name.rstrip("/")), None)
                found.append(Source(name, name.rstrip("/"), token or (secret(peer) if peer else None)))
                continue
            peer = next((p for p in peers if name in (p.node_id, p.name)), None)
            if peer is None:
                raise ResumeError(f"no peer named or numbered {name!r}; pass its address instead")
            found.append(Source(peer.name, peer.url.rstrip("/"), token or secret(peer)))
    return found


def _fetch_tail(client: httpx.Client, source: Source, node_id: str, head: int) -> list[dict[str, Any]]:
    headers = {wire.HEADER: wire.request_header()}
    if source.token:
        headers["Authorization"] = f"Bearer {source.token}"
    out: list[dict[str, Any]] = []
    since = head
    while len(out) < MAX_TAIL:
        resp = client.get(f"{source.url}/api/v2/sync/changes", params={"since": since, "limit": PAGE,
                                                                      "node_id": node_id}, headers=headers, timeout=30)
        if resp.status_code == 404:  # this node does not retain this feed
            return out
        resp.raise_for_status()
        entries = resp.json().get("entries", [])
        if not entries:
            break
        out.extend(entries)
        since = entries[-1]["seq"]
        if len(entries) < PAGE:
            break
    return out


def _verified(session: Session, identity: Any, head: int, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The longest prefix of `entries` that is contiguous from `head + 1` and correctly signed. Keys: this node's
    current key, the keys of its recorded rotations, and a rotation inside the tail vouched for by the key before it.
    Anything else stops the prefix (a source can only withhold, not forge)."""
    keys = {identity.key_id: identity.trust_public_b64}
    for row in session.scalars(select(ChangeLog).where(ChangeLog.kind == "key_rotation")):
        body = row.body if isinstance(row.body, dict) else {}
        if isinstance(body.get("key_id"), str) and isinstance(body.get("public_key"), str):
            keys[body["key_id"]] = body["public_key"]
    good: list[dict[str, Any]] = []
    expected = head + 1
    for e in entries:
        if (not isinstance(e, dict) or type(e.get("seq")) is not int or e["seq"] != expected
                or not isinstance(e.get("kind"), str) or e.get("node_id") != identity.node_id
                or e.get("key_id") not in keys):
            break
        if not signing.verify(keys[e["key_id"]], identity.node_id, e["seq"], e["kind"], e.get("body"),
                              e.get("body_hash", ""), e.get("signature", "")):
            break
        if e["kind"] == "key_rotation" and isinstance(e.get("body"), dict):
            body = e["body"]
            if isinstance(body.get("key_id"), str) and isinstance(body.get("public_key"), str):
                keys[body["key_id"]] = body["public_key"]
        good.append(e)
        expected += 1
    return good


def resume_from_nodes(maker: sessionmaker[Session], ctx: Any, srcs: list[Source],
                      client: httpx.Client | None = None) -> dict[str, Any]:
    """Re-insert the longest verified tail from the sources, apply it, and unfreeze. Raises `ResumeError` with
    nothing changed when it cannot."""
    own = client or httpx.Client()
    identity = ctx.identity
    with maker() as session:
        if feed.frozen(session) is None:
            raise ResumeError("the feed is not frozen")
        head = _head(session)
    if not srcs:
        raise ResumeError("no node to resume from: pass --from-node, or add peers that retain this feed")
    tails: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for source in srcs:
        try:
            raw = _fetch_tail(own, source, identity.node_id, head)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors[source.label] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        with maker() as session:
            tails[source.label] = _verified(session, identity, head, raw)
    if not tails:
        raise ResumeError("no source answered: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    best_label = max(tails, key=lambda k: len(tails[k]))
    tail = tails[best_label]
    for label, other in tails.items():  # two sources holding different bytes for one seq: a fork, never resolved here
        for a, b in zip(tail, other, strict=False):
            if (a["body_hash"], a["signature"]) != (b["body_hash"], b["signature"]):
                raise ResumeError(f"{best_label} and {label} hold different signed bytes for seq {a['seq']}: the feed "
                                  "has forked. Do not resume; compare the histories first")
    with maker() as session:
        from .service import write_lock

        write_lock(session)
        if feed.frozen(session) is None or _head(session) != head:
            raise ResumeError("the feed changed while resuming; run the command again")
        # positions recorded before the restore or during the freeze: the tail must reach the highest known one
        behind = {t: n for t, n in follower_positions(session).items() if n > (tail[-1]["seq"] if tail else head)}
        if behind:
            token, since = sorted(behind.items())[0]
            raise ResumeError(f"the longest retained tail ends at seq {tail[-1]['seq'] if tail else head}, but token "
                              f"{token} pulled up to seq {since}: no source retains what that follower holds. Try "
                              "more nodes (--from-node), or revoke that token if the follower is lost")
        last_key = identity.key_id
        for e in tail:
            if e["kind"] == "key_rotation":
                last_key = e["body"]["key_id"]
        if last_key != identity.key_id:
            raise ResumeError("the retained tail contains a key rotation, and this node does not hold the rotated "
                              f"private key ({last_key}). Restore a backup taken after the rotation, then resume")
        have_start = feed.wire_start_seq(session)
        for e in tail:
            if e["kind"] == "wire_start":
                if have_start is not None and have_start != e["seq"]:
                    raise ResumeError(f"the retained wire_start is at seq {e['seq']}, this node recorded {have_start}")
        for e in tail:
            session.add(ChangeLog(seq=e["seq"], kind=e["kind"], body=e["body"], body_hash=e["body_hash"],
                                  signature=e["signature"], key_id=e["key_id"],
                                  created_at=sync.origin_time(e.get("created_at"))))
        session.flush()
        end = tail[-1]["seq"] if tail else head
        row = session.get(NodeState, "feed_seq")
        if row is None:
            session.add(NodeState(key="feed_seq", value=str(end)))
        else:
            row.value = str(end)
        for e in tail:
            if e["kind"] == "wire_start" and have_start is None:
                session.merge(NodeState(key=feed.WIRE_START, value=str(e["seq"])))
        session.flush()
        applied, unapplied = _apply_tail(session, ctx, tail)
        feed.rebuild_introduced(session)
        _unfreeze(session, "from_node", end, source=best_label, inserted=len(tail), applied=applied,
                  unapplied=unapplied)
        session.commit()
    return {"resumed": "from_node", "source": best_label, "head": head, "new_head": end, "inserted": len(tail),
            "applied": applied, "unapplied": unapplied, "sources": {k: len(v) for k, v in tails.items()},
            "unreachable": errors}


def _apply_tail(session: Session, ctx: Any, tail: list[dict[str, Any]]) -> tuple[int, int]:
    """Apply each entry as `promotion.py` does (re-inserting the log alone would leave the authority's own records
    behind its own feed). An entry this release cannot apply stays unapplied and is counted, as on a follower."""
    applied = unapplied = 0
    for e in tail:
        if e["kind"] in ("key_rotation", "wire_start"):
            continue
        outcome = feed_schema.classify(e["kind"], e["body"])
        if outcome.action != feed_schema.APPLY:
            unapplied += 1
            continue
        try:
            with session.begin_nested():
                with sync.entry_at(sync.origin_time(e.get("created_at"))):
                    sync._apply(session, ctx, ctx.identity.node_id, e["seq"], e["kind"], outcome.body)
            applied += 1
        except (sync.InvalidEntry, AtlasError):
            unapplied += 1
    return applied, unapplied
