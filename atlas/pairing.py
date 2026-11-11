"""Device pairing for clients (CONTRACT-C15 §10.1 item 1, D15.15, A24): the OAuth device-grant shape, without OAuth.

1. A connector calls `POST /api/v2/client/pair` (anonymous) with a label ("Jellyfin on nas"). It gets a short user
   code, the approval address (also as a QR code) and a poll token.
2. The person opens `/pair` on Atlas, signed in, types (or scans) the code, sees which client asks (label,
   User-Agent, requesting address) and approves or denies with a form POST.
3. The connector polls `POST /api/v2/client/pair/poll`. After approval the first poll mints a **personal** token
   for the approving user through the same rules as `/me/tokens`: scope `read` only, counted in the 10-token cap,
   listed and revocable on `/me`, dead with the account. The secret exists only in that one answer.

Security rules (§10.1 critic, T-B18): the user code is 8 Crockford base32 characters (40 bits; no I, L, O, U),
expires in 600 s and is single use; the poll token has 256 bits and is stored hashed, as is the code; polling
faster than every 5 s answers `slow_down` and widens the interval; at most 20 pairings are open per node; 5 wrong
codes in one sign-in session lock that session's approval form for 10 minutes; `pair` and `poll` count on the
anonymous per-IP budget; approval is a CSRF-checked POST, never a link. The QR encodes the approval address and the
code only.

State lives in `node_state` rows (`client_pair:<id>`, `client_pair_lock:<session hash>`), like the enrichment queue:
a handful of short-lived rows, so no table and no migration.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ids import ulid
from .models import NodeState

PREFIX = "client_pair:"
LOCK_PREFIX = "client_pair_lock:"
#: Crockford base32: no I, L, O or U, so a code read aloud or typed from a TV screen is unambiguous.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
CODE_BITS = CODE_LENGTH * 5
POLL_TOKEN_BYTES = 32
EXPIRES_IN = 600
INTERVAL = 5
MAX_OPEN = 20
MAX_OPEN_PER_IP = 3  # so one address cannot hold every node-wide slot (A24 review)
WRONG_LIMIT = 5
LOCK_SECONDS = 600
LABEL_MAX = 64
TOKEN_CAP = 10  # the /me/tokens cap (web/routes.py)


def clock() -> float:
    """Seconds since the epoch; replaced in tests."""
    return time.time()


class PairingError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class Pending:
    id: str
    label: str
    user_agent: str
    ip: str
    created: float
    expires: float
    status: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalise_code(raw: str | None) -> str | None:
    """Upper-case, without spaces and hyphens, with the Crockford look-alikes folded (O→0, I/L→1). None when it
    cannot be a code at all."""
    text = "".join((raw or "").split()).replace("-", "").upper()
    text = text.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    if len(text) != CODE_LENGTH or any(ch not in ALPHABET for ch in text):
        return None
    return text


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _rows(session: Session) -> list[NodeState]:
    return list(session.scalars(select(NodeState).where(NodeState.key.like(PREFIX + "%")).limit(500)))


def _load(row: NodeState) -> dict[str, Any]:
    try:
        return json.loads(row.value)
    except ValueError:
        return {}


def _save(row: NodeState, data: dict[str, Any]) -> None:
    row.value = json.dumps(data, sort_keys=True)


def purge(session: Session, now: float | None = None) -> None:
    """Drop expired pairings and lapsed locks."""
    t = clock() if now is None else now
    for row in _rows(session):
        if _load(row).get("expires", 0) <= t:
            session.delete(row)
    for row in session.scalars(select(NodeState).where(NodeState.key.like(LOCK_PREFIX + "%")).limit(500)):
        if _load(row).get("until", 0) <= t and _load(row).get("window_end", 0) <= t:
            session.delete(row)
    session.flush()


def start(session: Session, label: str, user_agent: str, ip: str) -> dict[str, Any]:
    """Open a pairing. Returns the code and the poll token (the only time either exists in clear)."""
    t = clock()
    purge(session, t)
    rows = _rows(session)
    if len(rows) >= MAX_OPEN:
        raise PairingError("too many pairings are open on this node; try again in a few minutes", "too_many")
    if sum(1 for r in rows if (d := _load(r)).get("ip") == ip[:64] and d.get("status") == "pending") >= MAX_OPEN_PER_IP:
        raise PairingError("too many pairings are open from this address; finish or wait for one to expire",
                           "too_many_from_address")
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    poll_token = secrets.token_urlsafe(POLL_TOKEN_BYTES)
    pid = ulid()
    data = {"code": _digest(code), "poll": _digest(poll_token), "label": label, "user_agent": user_agent[:200],
            "ip": ip[:64], "created": t, "expires": t + EXPIRES_IN, "status": "pending", "user_id": None,
            "last_poll": None, "interval": INTERVAL}
    row = NodeState(key=PREFIX + pid)
    _save(row, data)
    session.add(row)
    session.flush()
    return {"id": pid, "code": display_code(code), "poll_token": poll_token, "expires_in": EXPIRES_IN,
            "interval": INTERVAL}


def find(session: Session, raw_code: str | None) -> Pending | None:
    """The open, unexpired, still pending pairing a code names, or None."""
    code = normalise_code(raw_code)
    if code is None:
        return None
    want, t = _digest(code), clock()
    for row in _rows(session):
        data = _load(row)
        if data.get("code") == want and data.get("expires", 0) > t and data.get("status") == "pending":
            return Pending(row.key[len(PREFIX):], data["label"], data["user_agent"], data["ip"], data["created"],
                           data["expires"], data["status"])
    return None


def decide(session: Session, pending: Pending, user_id: str, approve: bool) -> None:
    row = session.get(NodeState, PREFIX + pending.id)
    if row is None:
        raise PairingError("this pairing is no longer open", "not_found")
    data = _load(row)
    data["status"] = "approved" if approve else "denied"
    data["user_id"] = user_id
    _save(row, data)
    session.flush()


def poll(session: Session, poll_token: str) -> dict[str, Any]:
    """`pending` | `slow_down` | `denied` | `expired` | `approved` (with the new token, once)."""
    from . import accounts, auth
    from .models import Token, User

    t = clock()
    want = _digest(poll_token or "")
    row = next((r for r in _rows(session) if _load(r).get("poll") == want), None)
    if row is None:
        return {"status": "expired"}
    data = _load(row)
    if data.get("expires", 0) <= t:
        session.delete(row)
        return {"status": "expired"}
    if data["status"] == "denied":
        session.delete(row)
        return {"status": "denied"}
    if data["status"] == "approved":
        session.delete(row)  # single use, whatever happens next
        user = session.get(User, data["user_id"])
        if user is None or user.status != "active":
            return {"status": "denied"}
        active = session.scalar(select(func.count()).select_from(Token).where(
            Token.user_id == user.id, Token.revoked_at.is_(None))) or 0
        if active >= TOKEN_CAP:
            return {"status": "denied", "reason": "token_limit"}
        token, secret = auth.create_token(session, data["label"], ["read"])
        token.user_id = user.id
        accounts.audit(session, user.id, "token_create", token.id,
                       {"scopes": ["read"], "via": "paired", "client": data["label"], "user_agent": data["user_agent"]})
        return {"status": "approved", "token": secret, "token_id": token.id, "name": token.name, "scopes": ["read"]}
    last = data.get("last_poll")
    interval = int(data.get("interval") or INTERVAL)
    if last is not None and t - last < interval:
        interval += INTERVAL  # RFC 8628 §3.5: each slow_down adds 5 seconds
        data.update(interval=interval, last_poll=t)
        _save(row, data)
        return {"status": "slow_down", "interval": interval, "expires_in": int(data["expires"] - t)}
    data["last_poll"] = t
    _save(row, data)
    return {"status": "pending", "interval": interval, "expires_in": int(data["expires"] - t)}


# ---- wrong-code lock (per sign-in session) -------------------------------------------------------------------------

def _lock_key(session_id: str) -> str:
    return LOCK_PREFIX + _digest(session_id)[:40]


def locked_for(session: Session, session_id: str) -> int:
    """Seconds this sign-in session's approval form stays locked (0 = open)."""
    row = session.get(NodeState, _lock_key(session_id))
    if row is None:
        return 0
    left = _load(row).get("until", 0) - clock()
    return max(0, int(left + 0.999))


def wrong_code(session: Session, session_id: str) -> int:
    """Count a wrong code. Returns the seconds of lock this started (0 while under the limit). The count runs in a
    window of LOCK_SECONDS from the first wrong code; the fifth inside it locks the form for LOCK_SECONDS."""
    t = clock()
    key = _lock_key(session_id)
    row = session.get(NodeState, key)
    data = _load(row) if row is not None else {}
    if data.get("window_end", 0) <= t:
        data = {"wrong": 0, "window_end": t + LOCK_SECONDS, "until": 0}
    data["wrong"] = int(data.get("wrong", 0)) + 1
    if data["wrong"] >= WRONG_LIMIT:
        data.update(until=t + LOCK_SECONDS, window_end=t + LOCK_SECONDS, wrong=0)
    if row is None:
        row = NodeState(key=key)
        session.add(row)
    _save(row, data)
    session.flush()
    return LOCK_SECONDS if data.get("until", 0) > t else 0


def clean_label(raw: str) -> str:
    text = (raw or "").strip()
    if not text or len(text) > LABEL_MAX or not text.isprintable():
        raise ValueError(f"label must be 1-{LABEL_MAX} printable characters")
    return text
