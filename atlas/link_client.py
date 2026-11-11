"""Node links, node side (CONTRACT-C14 Part C, slice C14-3n).

A local node links to an account on a main Atlas (Admin -> Main Atlas):

1. `probe` reads the main's discovery document and pins its root key (`atlas_node`), validating the main's signing
   key history from that root, as followers do for the feed.
2. `start` makes a per-main link key (C14-11: a fresh Ed25519 root for this main only, so two mains cannot
   correlate this node by key), fetches a one-use challenge, signs the proof of possession and opens the RFC 8628
   device flow with a DPoP proof by that key. The node shows the QR code of `verification_uri_complete`, the user
   code and its own **confirmation code** (C14-5), which the account owner types on the main.
3. `poll` waits for the approval and verifies what comes back: the link grant against the main's pinned key
   history, and the id token (`cnf.jkt` = this link key, pairwise `sub`).

Then contributions are signed by the link key (`propose`), sent in `seq` order, at most 20 per request (`send`),
with backoff on 429/5xx (`Retry-After` honoured) and a full stop on 401/403 (`link_revoked`). Decisions come back
through `pull`. `unlink` moves `revoked_locally -> pending_remote -> confirmed` (C14-10); `rotate_key` rotates the
link key append-only and re-signs what is still queued. Every state change is recorded for the owner group
(`contrib_state.record`, §7.7). Tokens, keys and the open flow's codes are sealed with ATLAS_SECRET_KEY.

An authority never links upward (`refusal`): it is a main itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import contrib_state, dpop, instruments, key_rotation, peer_probe, secrets_box
from . import link_crypto as lc
from .ids import ulid
from .models import Canonical, UpstreamAlias, UpstreamContribution, UpstreamLink, now
from .signing import ENC_PREFIX, canonical_json

log = logging.getLogger("atlas.link_client")

CLIENT_ID = "atlas-node"
SCOPE = "openid atlas.link atlas.contribute"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT = 15
BATCH = 20
KINDS = ("edit", "new_game", "link_ids", "artwork", "artwork_default", "achievements")
#: how a contribution kind appears in the owner group's shared state (contrib_state.KINDS)
STATE_KIND = {"edit": "edit", "new_game": "record", "link_ids": "relation", "artwork": "artwork",
              "artwork_default": "artwork", "achievements": "other"}
RETRYABLE = ("daily_limit", "rate_limited", "too_many_pending")
OPEN = ("queued", "sent")
DECIDED = ("approved", "rejected", "reverted", "withdrawn")
CONFIRM_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"


class LinkClientError(Exception):
    """Something the admin reads: the message is shown as it is."""


# ---- sealing (§6.6: the link key and tokens are secrets of this node) ------------------------------------------

def _seal(settings: Any, plain: str) -> str:
    return ENC_PREFIX + secrets_box.encrypt(settings, plain) if secrets_box.available(settings) else plain


def _open(settings: Any, value: str | None) -> str:
    if not value:
        return ""
    if value.startswith(ENC_PREFIX):
        return secrets_box.decrypt(settings, value[len(ENC_PREFIX):])
    return value


def _raw(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                  serialization.NoEncryption())).decode()


def _key(settings: Any, value: str | None) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(_open(settings, value)))


def link_node_id(row: UpstreamLink) -> str:
    """This node's id towards that main: the id of its per-main link root key."""
    return lc.key_id(row.link_root_public)


def current_key_id(row: UpstreamLink) -> str:
    return row.link_rotations[-1]["body"]["key_id"] if row.link_rotations else link_node_id(row)


def refusal(settings: Any) -> str | None:
    if getattr(settings, "role", "") == "authority":
        return "This node is an authority: it is a main Atlas itself and cannot link to another one."
    return None


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=now().tzinfo)


def _error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("reason") or err.get("code") or err.get("message"))[:200]
        if isinstance(err, str):
            return (f"{err}: {body['error_description']}" if body.get("error_description") else err)[:200]
    return f"HTTP {resp.status_code}"


# ---- discovery and the main's pinned keys (§6.2) ----------------------------------------------------------------

def probe(http: Any, url: str) -> dict[str, Any]:
    """Read and check the main's discovery document. Raises LinkClientError with what went wrong."""
    try:
        issuer = peer_probe.clean_url(url)
    except ValueError as exc:
        raise LinkClientError(str(exc)) from exc
    try:
        resp = http.get(f"{issuer}/.well-known/openid-configuration", timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise LinkClientError(f"{issuer} did not answer ({type(exc).__name__}).") from exc
    if resp.status_code != 200:
        raise LinkClientError(f"{issuer} is not a main Atlas that accepts node links (HTTP {resp.status_code}).")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise LinkClientError(f"{issuer} did not answer with a discovery document.") from exc
    if not isinstance(doc, dict) or doc.get("issuer") != issuer:
        raise LinkClientError("The discovery document names another issuer; use the main's exact public address.")
    if not isinstance(doc.get("atlas_link"), dict) or not doc.get("device_authorization_endpoint"):
        raise LinkClientError(f"{issuer} does not accept node links (links are off there, or it is not an "
                              "authority).")
    node = doc.get("atlas_node")
    if not isinstance(node, dict):
        raise LinkClientError("The main does not publish its node key.")
    try:
        _, keys = key_rotation.validate_history(node["node_id"], node["root_public_key"], node.get("rotations") or [],
                                                node["signing_key_id"])
    except (key_rotation.RotationError, KeyError, TypeError) as exc:
        raise LinkClientError(f"The main's key history does not verify: {exc}") from exc
    return {"issuer": issuer, "node_id": node["node_id"], "root_public_key": node["root_public_key"],
            "signing_key_id": node["signing_key_id"], "public_key": keys[node["signing_key_id"]],
            "fp": peer_probe.fingerprint(node["node_id"]),
            "name": str(doc["atlas_link"].get("name") or issuer.split("://", 1)[1])[:80]}


def main_key(http: Any, row: UpstreamLink) -> tuple[str, str]:
    """The main's current signing key `(key_id, public)`, validated from the root pinned at link time."""
    info = probe(http, row.issuer_url)
    if info["node_id"] != row.main_node_id or info["root_public_key"] != row.main_root_public_key:
        raise LinkClientError("The main's root key changed since this node linked to it; refusing it.")
    return info["signing_key_id"], info["public_key"]


# ---- §6.3: the device flow ---------------------------------------------------------------------------------------

def new_confirmation() -> str:
    import secrets

    return "".join(secrets.choice(CONFIRM_ALPHABET) for _ in range(8))


def show(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _fresh_key(row: UpstreamLink, settings: Any) -> None:
    root = Ed25519PrivateKey.generate()
    row.link_root_public, row.link_key, row.link_key_previous = lc.public_b64(root), _seal(settings, _raw(root)), None
    row.link_rotations, row.key_epoch, row.next_seq, row.status_cursor = [], 1, 1, None
    row.link_id = row.subject = row.display_name = row.grant = None
    row.access_enc = row.refresh_enc = row.access_expires_at = None


def start(session: Session, settings: Any, identity: Any, http: Any, url: str, *, relink: bool = False
          ) -> UpstreamLink:
    """Open a device flow at the main (§6.3 steps 0-1). A first link, or a new link after an unlink, uses a fresh
    per-main link key; a relink (`relink=True`, the same link after a freeze or a lost approval) first rotates the
    link key, so a key that may have leaked is never the one that relinks."""
    if (why := refusal(settings)) is not None:
        raise LinkClientError(why)
    info = probe(http, url)
    row = session.get(UpstreamLink, info["node_id"])
    if row is not None and row.main_root_public_key != info["root_public_key"]:
        raise LinkClientError("This main's root key differs from the one pinned here.")
    if row is None:
        row = UpstreamLink(main_node_id=info["node_id"], issuer_url=info["issuer"],
                           main_root_public_key=info["root_public_key"], main_name=info["name"], status="starting",
                           link_root_public="", link_key="", link_rotations=[], created_at=now())
        _fresh_key(row, settings)
        session.add(row)
    elif relink:
        if not row.link_id:
            raise LinkClientError("There is no link to repair yet; link first.")
        _local_rotation(row, settings)
    elif row.status in ("linked",) or (row.flow and row.status == "starting"):
        if row.status == "linked":
            raise LinkClientError("This node is already linked to that main Atlas.")
        _fresh_key(row, settings)
    else:  # after an unlink, a refusal or a failed flow: a new link with a new key (C14-11)
        _fresh_key(row, settings)
        row.status = "starting"
    row.issuer_url, row.main_name = info["issuer"], info["name"]
    session.flush()
    issuer = info["issuer"]
    private = _key(settings, row.link_key)
    public = lc.public_b64(private)
    confirm = new_confirmation()
    node: dict[str, Any] = {"node_id": link_node_id(row), "root_public_key": row.link_root_public,
                            "key_id": current_key_id(row), "public_key": public,
                            "name": (str(getattr(settings, "app_name", "") or "Atlas").strip() or "Atlas")[:80],
                            "public_url": None, "rotations": list(row.link_rotations or []),
                            "confirm_hash": hashlib.sha256(confirm.encode()).hexdigest(),
                            "role": getattr(settings, "role", "local")}
    if relink:
        node["relink"] = row.link_id
    try:
        ch = http.post(f"{issuer}/oidc/device/challenge", timeout=TIMEOUT)
        if ch.status_code != 200:
            raise LinkClientError(f"The main refused to start a link: {_error(ch)}")
        challenge = ch.json()["challenge"]
        raw_node = canonical_json(node).decode()
        pop = lc.sign_pop(private, lc.registration_parts(issuer, node, challenge))
        proof = dpop.make_proof(private, "POST", f"{issuer}/oidc/device")
        resp = http.post(f"{issuer}/oidc/device", timeout=TIMEOUT, headers={"DPoP": proof},
                         data={"client_id": CLIENT_ID, "scope": SCOPE, "atlas_node": raw_node,
                               "atlas_challenge": challenge, "atlas_node_pop": pop})
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise LinkClientError(f"The main did not answer the link request ({type(exc).__name__}).") from exc
    if resp.status_code != 200:
        raise LinkClientError(f"The main refused the link request: {_error(resp)}")
    out = resp.json()
    uri, complete = str(out.get("verification_uri") or ""), str(out.get("verification_uri_complete") or "")
    if not _own_link_page(issuer, uri) or not _own_link_page(issuer, complete):
        # the admin's phone is sent to the QR code's address: never another origin than the pinned issuer
        raise LinkClientError("The main's approval address is not its own /link page; nothing was started.")
    t = now()
    row.flow = {"device_code": _seal(settings, out["device_code"]), "user_code": str(out["user_code"])[:9],
                "verification_uri": str(out["verification_uri"])[:300],
                "verification_uri_complete": str(out["verification_uri_complete"])[:340],
                "confirm": _seal(settings, confirm), "interval": int(out.get("interval") or 5),
                "expires_at": (t + timedelta(seconds=int(out.get("expires_in") or 600))).isoformat(),
                "next_poll_at": t.isoformat(), "relink": relink, "started_at": t.isoformat()}
    row.last_error = None
    session.flush()
    return row


def flow_view(settings: Any, row: UpstreamLink) -> dict[str, Any] | None:
    """What the node's admin page shows while a flow is open (the confirmation code only here, §6.3 step 2)."""
    flow = row.flow
    if not flow:
        return None
    from . import qr

    return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"],
            "verification_uri_complete": flow["verification_uri_complete"],
            "confirm": show(_open(settings, flow["confirm"])), "expires_at": flow["expires_at"],
            "qr": qr.svg(flow["verification_uri_complete"], label="Scan to approve this link on the main Atlas"),
            "relink": bool(flow.get("relink"))}


def poll(session: Session, settings: Any, http: Any, row: UpstreamLink, *, force: bool = False) -> str:
    """One poll of the token endpoint (§6.3 step 4). Returns `waiting`, `linked` or `failed`."""
    flow = row.flow
    if not flow:
        return row.status
    t = now()
    expires = _when(flow.get("expires_at"))
    if expires is not None and expires <= t:
        return _flow_failed(session, row, "The code expired before it was approved. Start again.")
    nxt = _when(flow.get("next_poll_at"))
    if not force and nxt is not None and nxt > t:
        return "waiting"
    private = _key(settings, row.link_key)
    proof = dpop.make_proof(private, "POST", f"{row.issuer_url}/oidc/token")
    try:
        resp = http.post(f"{row.issuer_url}/oidc/token", timeout=TIMEOUT, headers={"DPoP": proof},
                         data={"grant_type": DEVICE_GRANT, "client_id": CLIENT_ID,
                               "device_code": _open(settings, flow["device_code"])})
    except httpx.HTTPError as exc:
        row.last_error = f"The main did not answer ({type(exc).__name__})."
        return "waiting"
    if resp.status_code != 200:
        err = _error(resp).split(":", 1)[0]
        if err in ("authorization_pending", "slow_down"):
            interval = int(flow.get("interval") or 5) + (5 if err == "slow_down" else 0)
            row.flow = {**flow, "interval": interval, "next_poll_at": (t + timedelta(seconds=interval)).isoformat()}
            session.flush()
            return "waiting"
        reason = {"access_denied": "The link was denied on the main Atlas.",
                  "expired_token": "The code expired before it was approved."}.get(err, f"The main refused: {err}")
        return _flow_failed(session, row, reason)
    try:
        _accept(session, settings, http, row, resp.json(), first=True)
    except LinkClientError as exc:
        return _flow_failed(session, row, str(exc))
    return "linked"


def _own_link_page(issuer: str, uri: str) -> bool:
    """`uri` is the issuer's own `/link` page (optionally with a query), on the issuer's exact origin."""
    base = f"{issuer.rstrip('/')}/link"
    return uri == base or (uri.startswith(base + "?") and not any(c in uri for c in "\\\r\n\t #@"))


def _flow_failed(session: Session, row: UpstreamLink, message: str) -> str:
    row.flow, row.last_error = None, message[:200]
    if row.status == "starting":
        row.status = "failed"
    session.flush()
    return "failed"


def _accept(session: Session, settings: Any, http: Any, row: UpstreamLink, body: dict[str, Any], *,
            first: bool) -> None:
    """Check and keep fresh tokens: the link grant must verify under the main's current key (pinned root), name
    this node's link root; on the first issue the id token must too, bound to this link key (`cnf.jkt`)."""
    from . import oidc

    kid, public = main_key(http, row)
    grant = body.get("atlas_link")
    if not lc.verify(grant, public, t="atlas.link") or grant.get("issuer") != row.main_node_id \
            or grant["signer"].get("id") != row.main_node_id:
        raise LinkClientError("The link grant from the main does not verify against its pinned key.")
    g = grant["body"]
    own = link_node_id(row)
    if g.get("node_id") != own or g.get("node_root_public_key") != row.link_root_public:
        raise LinkClientError("The link grant names another node key.")
    if row.link_id and g.get("link_id") != row.link_id and not first:
        raise LinkClientError("The main answered for another link.")
    jkt = dpop.jkt_of(lc.public_b64(_key(settings, row.link_key)))
    if first:
        token = body.get("id_token")
        try:
            claims = oidc.verify_jwt(token or "", public_key_b64=public, kid=kid, issuer=row.issuer_url,
                                     audience=CLIENT_ID, nonce=None)
        except oidc.TokenError as exc:
            raise LinkClientError(f"The main's id token does not verify: {exc}") from exc
        if (claims.get("cnf") or {}).get("jkt") != jkt or claims.get("sub") != g.get("subject") \
                or claims.get("atlas_link_id") != g.get("link_id"):
            raise LinkClientError("The main's id token is not bound to this link key.")
        name = claims.get("preferred_username")
        row.display_name = str(name)[:80] if isinstance(name, str) else None
    if body.get("token_type") != "DPoP" or not body.get("access_token") or not body.get("refresh_token"):
        raise LinkClientError("The main did not issue DPoP-bound tokens.")
    if row.link_id and row.link_id != g["link_id"]:
        row.status_cursor = None
    row.link_id, row.subject, row.grant, row.key_epoch = g["link_id"], g["subject"], grant, int(g["key_epoch"])
    row.access_enc, row.refresh_enc = _seal(settings, body["access_token"]), _seal(settings, body["refresh_token"])
    row.access_expires_at = now() + timedelta(seconds=int(body.get("expires_in") or 3600))
    if first:
        row.status, row.flow, row.linked_at, row.unlinked_at = "linked", None, now(), None
    row.last_error, row.attempts, row.retry_at = None, 0, None
    session.flush()


# ---- §6.5: DPoP-bound calls ------------------------------------------------------------------------------------

class LinkGone(LinkClientError):
    """The main refused the link itself (401/403): the queue stops (`link_revoked`)."""


def _refresh(session: Session, settings: Any, http: Any, row: UpstreamLink) -> None:
    private = _key(settings, row.link_key)
    proof = dpop.make_proof(private, "POST", f"{row.issuer_url}/oidc/token")
    resp = http.post(f"{row.issuer_url}/oidc/token", timeout=TIMEOUT, headers={"DPoP": proof},
                     data={"grant_type": "refresh_token", "client_id": CLIENT_ID,
                           "refresh_token": _open(settings, row.refresh_enc)})
    if resp.status_code == 200:
        _accept(session, settings, http, row, resp.json(), first=False)
        return
    if resp.status_code in (400, 401) and _error(resp).split(":", 1)[0] == "invalid_grant":
        if row.status != "linked":
            raise LinkGone("The main no longer accepts this link.")
        _gone(session, row, "The main no longer accepts this link (its tokens were refused).")
    raise LinkClientError(f"Refreshing the link failed: {_error(resp)}")


def _gone(session: Session, row: UpstreamLink, message: str) -> None:
    row.status, row.last_error = "remote_revoked", message[:200]
    row.access_enc = row.refresh_enc = row.access_expires_at = None
    for c in session.scalars(select(UpstreamContribution).where(UpstreamContribution.main_node_id ==
                                                                row.main_node_id,
                                                                UpstreamContribution.state.in_(OPEN))):
        c.state, c.updated_at = "link_revoked", now()
    session.flush()
    raise LinkGone(message)


# What a main's 401 `error_description` (or its own 403) means for the link. Only these end it; clock skew, a
# replayed or malformed proof and other transient refusals leave the link and its queue alone and are retried.
ENDED = ("link_revoked", "link_frozen", "link_banned", "account_unavailable")
STALE_TOKEN = ("invalid_token", "key_mismatch")  # refresh once; a refused refresh (`invalid_grant`) ends the link


def _reason(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("error_description") or "")[:64] if isinstance(body, dict) else ""


def _request(settings: Any, http: Any, row: UpstreamLink, method: str, path: str, body: Any,
             access: str) -> httpx.Response:
    url = f"{row.issuer_url}{path}"
    proof = dpop.make_proof(_key(settings, row.link_key), method, url, access_token=access)
    return http.request(method, url, json=body, timeout=TIMEOUT,
                        headers={"Authorization": f"DPoP {access}", "DPoP": proof})


def call(session: Session, settings: Any, http: Any, row: UpstreamLink, method: str, path: str,
         body: Any = None) -> httpx.Response:
    """One `/api/v2/link*` request with a fresh access token and a DPoP proof (with `ath`) by the current key.

    A 401/403 ends the link (`remote_revoked`, the open queue `link_revoked`) only when the main says the link or
    the account is gone (`ENDED`) or answers its own 403. An expired or superseded token is refreshed once and the
    request repeated; a refused refresh ends the link. Any other 401 (a stale or replayed proof after clock drift,
    a malformed proof, links switched off) raises a retryable `LinkClientError` and changes nothing."""
    if row.status not in ("linked", "revoked_locally", "pending_remote"):
        raise LinkClientError("This node is not linked.")
    if pending_rotation(row) is not None:
        raise LinkClientError("A key rotation is waiting for the main's answer; it is finished first.")
    expires = _utc(row.access_expires_at)
    if not row.access_enc or expires is None or expires <= now() + timedelta(seconds=60):
        _refresh(session, settings, http, row)
    resp = _request(settings, http, row, method, path, body, _open(settings, row.access_enc))
    row.last_used_at = now()
    if resp.status_code not in (401, 403) or row.status != "linked":
        return resp
    reason = _reason(resp)
    if resp.status_code == 403 or reason in ENDED:
        _gone(session, row, f"The main refused this link ({_error(resp)}).")
    if reason in STALE_TOKEN:
        _refresh(session, settings, http, row)  # ends the link itself when the main refuses the refresh
        resp = _request(settings, http, row, method, path, body, _open(settings, row.access_enc))
        reason = _reason(resp)
        if resp.status_code == 403 or (resp.status_code == 401 and reason in ENDED + STALE_TOKEN):
            _gone(session, row, f"The main refused this link ({_error(resp)}).")
        if resp.status_code not in (401, 403):
            return resp
    raise LinkClientError(f"The main did not accept this request for now ({_error(resp)}); it is retried.")


# ---- §7.1/§7.2: the queue -------------------------------------------------------------------------------------

def linked(session: Session) -> UpstreamLink | None:
    """The main this node proposes to (the first linked one)."""
    return session.scalars(select(UpstreamLink).where(UpstreamLink.status == "linked")
                           .order_by(UpstreamLink.linked_at)).first()


def _sign(settings: Any, row: UpstreamLink, c: UpstreamContribution) -> None:
    node_id = link_node_id(row)
    c.envelope = lc.make("atlas.contribution", node_id, f"contrib:{node_id}/{c.id}", c.body,
                         {"kind": "node", "id": node_id, "key_id": current_key_id(row)},
                         _key(settings, row.link_key))


def _share(session: Session, settings: Any, identity: Any, row: UpstreamLink, c: UpstreamContribution) -> None:
    """§7.7: this node's own contribution state, for the owner group (a no-op outside a group)."""
    try:
        contrib_state.record(session, settings, identity, main=row.main_node_id, subject=row.subject or "0000",
                             contribution_id=c.id, kind=STATE_KIND.get(c.kind, "other"),
                             state=c.state if c.state in contrib_state.STATES else "pending",
                             local_cid=c.local_cid if c.local_cid and contrib_state.CID_RE.match(c.local_cid)
                             else None,
                             main_cid=c.main_cid if c.main_cid and contrib_state.CID_RE.match(c.main_cid) else None,
                             fields=[f for f in c.fields or [] if contrib_state.FIELD_RE.match(f)],
                             decided_at=_utc(c.decided_at), feed_seq=c.feed_seq)
    except contrib_state.StateError as exc:  # never blocks the queue
        log.warning("contribution state not shared: %s", exc)


def base_of(session: Session, main_cid: str | None, fields: list[str]) -> dict[str, Any] | None:
    """§7.1 `base`: the record's version and a hash per touched field as this node last saw them (equal ids when
    this node follows the main's feed)."""
    row = session.get(Canonical, main_cid) if main_cid else None
    if row is None:
        return None
    view = row.view or {}
    return {"main_version": int(row.version or 0),
            "fields": {f: hashlib.sha256(canonical_json(view.get(f))).hexdigest() for f in fields}}


def main_id_for(session: Session, local_cid: str | None) -> str | None:
    if not local_cid:
        return None
    alias = session.get(UpstreamAlias, local_cid)
    return alias.main_cid if alias is not None else local_cid


def propose(session: Session, settings: Any, identity: Any, row: UpstreamLink, *, kind: str,
            local_cid: str | None, payload: dict[str, Any], note: str = "", user_id: str | None = None
            ) -> UpstreamContribution:
    """Queue one contribution (§7.2). `local_cid` is this node's record (None for `new_game`)."""
    if row.status != "linked" or not row.link_id:
        raise LinkClientError("This node is not linked to that main Atlas.")
    if kind not in KINDS or not isinstance(payload, dict) or not payload:
        raise LinkClientError("That change cannot be proposed to a main Atlas.")
    if kind in ("artwork", "artwork_default"):
        for item in payload.get("items") or []:
            url = item.get("url") if isinstance(item, dict) else None
            if not isinstance(url, str) or not url.startswith("https://"):
                raise LinkClientError("Upload images to a main Atlas is not available yet: only public https "
                                      "artwork addresses can be proposed.")
    main_cid = None if kind == "new_game" else main_id_for(session, local_cid)
    if kind != "new_game" and not main_cid:
        raise LinkClientError("Pick the record to propose a change to.")
    seq = row.next_seq
    row.next_seq = seq + 1
    fields = sorted(k for k in payload if isinstance(k, str))[:64] if kind == "edit" else []
    t = now()
    c = UpstreamContribution(id=ulid(), main_node_id=row.main_node_id, seq=seq, kind=kind, local_cid=local_cid,
                             main_cid=main_cid, fields=fields, state="queued", created_by=user_id, created_at=t,
                             updated_at=t)
    c.body = {"link_id": row.link_id, "node_id": link_node_id(row), "subject": row.subject, "seq": seq,
              "kind": kind, "canonical_id": main_cid, "local_canonical_id": local_cid, "payload": payload,
              "note": (note or "")[:2000], "base": base_of(session, main_cid, fields) if kind == "edit" else None,
              "created_at": lc_stamp(t)}
    _sign(settings, row, c)
    session.add(c)
    session.flush()
    _share(session, settings, identity, row, c)
    return c


def lc_stamp(t: datetime) -> str:
    from .group_crypto import stamp

    return stamp(t)


def _backoff(row: UpstreamLink, retry_after: int | None) -> None:
    row.attempts += 1
    wait = retry_after if retry_after else min(3600, 30 * 2 ** min(row.attempts, 7))
    row.retry_at = now() + timedelta(seconds=max(1, wait))


def send(session: Session, settings: Any, identity: Any, http: Any, row: UpstreamLink, *, force: bool = False
         ) -> dict[str, int]:
    """Send queued contributions in `seq` order, at most 20 per request (§7.2). A retryable refusal leaves the
    item (and every later one) queued until its `retry_after`; a final one moves it to `failed`."""
    out = {"sent": 0, "failed": 0, "waiting": 0}
    if row.status != "linked":
        return out
    t = now()
    if not force and row.retry_at is not None and _utc(row.retry_at) > t:  # type: ignore[operator]
        return out
    items = list(session.scalars(select(UpstreamContribution).where(
        UpstreamContribution.main_node_id == row.main_node_id, UpstreamContribution.state == "queued")
        .order_by(UpstreamContribution.seq).limit(BATCH)))
    if not items:
        return out
    if not force and items[0].next_try_at is not None and _utc(items[0].next_try_at) > t:  # type: ignore[operator]
        return out
    for c in items:
        c.state, c.attempts, c.updated_at = "sent", c.attempts + 1, t
    session.flush()
    try:
        resp = call(session, settings, http, row, "POST", "/api/v2/link/contributions",
                    {"items": [c.envelope for c in items]})
    except LinkGone:
        instruments.link_contributions_sent.inc(len(items), result="unauthorized")
        return out
    except (httpx.HTTPError, LinkClientError) as exc:
        instruments.link_contributions_sent.inc(len(items), result="error")
        for c in items:
            c.state = "queued"
        row.last_error = f"Sending failed ({type(exc).__name__}); will retry."
        _backoff(row, None)
        session.flush()
        return out
    if resp.status_code == 429 or resp.status_code >= 500:
        instruments.link_contributions_sent.inc(len(items), result="retry")
        for c in items:
            c.state = "queued"
        retry = resp.headers.get("Retry-After")
        _backoff(row, int(retry) if retry and retry.isdigit() else None)
        row.last_error = f"The main asked to wait (HTTP {resp.status_code})."
        session.flush()
        return out
    if resp.status_code != 200:
        for c in items:
            c.state = "queued"
        row.last_error = f"The main refused the batch: {_error(resp)}"
        _backoff(row, None)
        session.flush()
        return out
    by_id = {f"contrib:{link_node_id(row)}/{c.id}": c for c in items}
    for ans in resp.json().get("items") or []:
        c = by_id.get(ans.get("id")) if isinstance(ans, dict) else None
        if c is None:
            continue
        c.updated_at = now()
        result = ans.get("status") if ans.get("status") in ("accepted", "duplicate") else (
            "retry" if ans.get("reason") in RETRYABLE else "refused")
        instruments.link_contributions_sent.inc(result=result)
        if ans.get("status") in ("accepted", "duplicate"):
            c.proposal_id = str(ans.get("proposal_id") or "")[:26] or None
            state = ans.get("state")
            c.state = state if state in ("pending", "approved", "rejected", "reverted", "withdrawn") else "pending"
            if c.state != "pending":
                c.decided_at = now()
            out["sent"] += 1
        elif ans.get("reason") in RETRYABLE:
            c.state, c.last_error = "queued", str(ans["reason"])
            retry = ans.get("retry_after")
            c.next_try_at = now() + timedelta(seconds=int(retry) if type(retry) is int and retry > 0 else 60)
            out["waiting"] += 1
        else:
            c.state, c.last_error = "failed", str(ans.get("reason") or "refused")[:200]
            out["failed"] += 1
        _share(session, settings, identity, row, c)
    for c in items:  # anything the main did not answer goes back to the queue
        if c.state == "sent":
            c.state = "queued"
    row.last_error, row.attempts, row.retry_at = None, 0, None
    session.flush()
    return out


def pull(session: Session, settings: Any, identity: Any, http: Any, row: UpstreamLink) -> int:
    """Fetch the main's decisions since the stored cursor (§7.3) and update the queue. Returns items changed."""
    if row.status != "linked":
        return 0
    changed = 0
    own = link_node_id(row)
    for _ in range(10):
        path = "/api/v2/link/contributions"
        if row.status_cursor:
            from urllib.parse import quote

            path += f"?since={quote(row.status_cursor)}"
        try:
            resp = call(session, settings, http, row, "GET", path)
        except (LinkGone, httpx.HTTPError, LinkClientError):
            return changed
        if resp.status_code != 200:
            return changed
        data = resp.json()
        for item in data.get("items") or []:
            cid = item.get("id") or ""
            prefix = f"contrib:{own}/"
            if not isinstance(cid, str) or not cid.startswith(prefix):
                continue
            c = session.get(UpstreamContribution, cid[len(prefix):])
            if c is None or c.main_node_id != row.main_node_id:
                continue
            state = item.get("state")
            if state not in ("pending", *DECIDED) or c.state == state:
                continue
            c.state, c.proposal_id, c.updated_at = state, item.get("proposal_id") or c.proposal_id, now()
            c.decided_at = _when(item.get("decided_at")) or (now() if state != "pending" else None)
            note = item.get("reviewer_note")
            c.reviewer_note = note[:500] if isinstance(note, str) else None
            main_cid = item.get("main_canonical_id")
            if c.kind == "new_game" and state == "approved" and isinstance(main_cid, str) and c.local_cid:
                c.main_cid = main_cid  # §7.4 "New records": the local record is known under the main's id
                session.merge(UpstreamAlias(local_cid=c.local_cid, main_cid=main_cid, at=now()))
            _share(session, settings, identity, row, c)
            changed += 1
        nxt = data.get("next")
        if not nxt or nxt == row.status_cursor:
            break
        row.status_cursor = str(nxt)[:80]
    session.flush()
    return changed


def withdraw(session: Session, settings: Any, identity: Any, http: Any, row: UpstreamLink,
             c: UpstreamContribution) -> None:
    if c.state == "queued":
        c.state, c.updated_at = "unsent", now()
    elif c.state == "pending":
        resp = call(session, settings, http, row, "POST",
                    f"/api/v2/link/contributions/contrib:{link_node_id(row)}/{c.id}/withdraw")
        if resp.status_code != 200:
            raise LinkClientError(f"The main did not withdraw it: {_error(resp)}")
        c.state, c.decided_at, c.updated_at = "withdrawn", now(), now()
    else:
        raise LinkClientError("Only a queued or pending contribution can be withdrawn.")
    _share(session, settings, identity, row, c)
    session.flush()


# ---- §6.7 / C14-10: unlink, rotation, compromise ----------------------------------------------------------------

def unlink(session: Session, settings: Any, identity: Any, http: Any, row: UpstreamLink, *,
           compromised: bool = False) -> str:
    """Unlink from here: at once locally (`revoked_locally`, nothing more is sent, queued items become `unsent`),
    then at the main (`pending_remote` until it confirms, retried by the worker, then `confirmed`). With
    `compromised`, the main flags this link's pending proposals `link_compromised` for a careful review."""
    if row.status in ("linked", "remote_revoked", "starting", "failed"):
        row.status, row.unlinked_at = "revoked_locally", now()
        row.flow = {"compromised": True} if compromised else None
        for c in session.scalars(select(UpstreamContribution).where(
                UpstreamContribution.main_node_id == row.main_node_id, UpstreamContribution.state.in_(OPEN))):
            c.state, c.updated_at = "unsent", now()
            _share(session, settings, identity, row, c)
        session.flush()
    return confirm_unlink(session, settings, http, row)


def confirm_unlink(session: Session, settings: Any, http: Any, row: UpstreamLink) -> str:
    if row.status not in ("revoked_locally", "pending_remote"):
        return row.status
    if not row.refresh_enc and not row.access_enc:
        row.status = "confirmed"
        session.flush()
        return row.status
    compromised = bool((row.flow or {}).get("compromised"))
    try:
        resp = call(session, settings, http, row, "DELETE",
                    "/api/v2/link" + ("?reason=compromised" if compromised else ""))
        done = resp.status_code in (200, 401, 403, 404)  # a link the main already ended counts as confirmed
    except LinkGone:
        done = True  # the main already ended the link
    except (httpx.HTTPError, LinkClientError):
        done = False
    if done:
        try:
            http.post(f"{row.issuer_url}/oidc/revoke", timeout=TIMEOUT,
                      data={"client_id": CLIENT_ID, "token": _open(settings, row.refresh_enc)})
        except httpx.HTTPError:
            pass
        row.status, row.flow = "confirmed", None
        row.access_enc = row.refresh_enc = row.access_expires_at = None
    else:
        row.status = "pending_remote"
    session.flush()
    return row.status


def _local_rotation(row: UpstreamLink, settings: Any) -> tuple[Ed25519PrivateKey, Ed25519PrivateKey, list]:
    """Append a rotation certificate for a fresh link key (signed by the current one) and switch to it."""
    previous = _key(settings, row.link_key)
    successor = Ed25519PrivateKey.generate()
    seq = len(row.link_rotations or []) + 1
    cert = lc.rotation_certificate(previous, link_node_id(row), seq, lc.public_b64(successor))
    rotations = [*(row.link_rotations or []), cert]
    row.link_key_previous, row.link_key = row.link_key, _seal(settings, _raw(successor))
    row.link_rotations = rotations
    return previous, successor, rotations


def pending_rotation(row: UpstreamLink) -> dict[str, Any] | None:
    """The key rotation sent but not yet answered (kept in `flow` while linked), or None."""
    flow = row.flow or {}
    return flow.get("rotation") if row.status == "linked" and isinstance(flow.get("rotation"), dict) else None


def _drop_rotation(row: UpstreamLink) -> None:
    row.flow = {k: v for k, v in (row.flow or {}).items() if k != "rotation"} or None


def rotate_key(session: Session, settings: Any, identity: Any, http: Any, row: UpstreamLink) -> None:
    """Rotate the link key (§6.7): the request is made with the CURRENT key and carries the new history and a
    proof by the new key; on success the main issues tokens for the new key, and what is still queued is
    re-signed with it.

    The successor key, the history and the proof are stored (and committed) BEFORE the request, and kept until the
    main's answer is known. A timeout, a lost answer or a grant that does not check out leaves them in place; the
    next attempt (the admin's button again, or the worker, which finishes it before sending anything) repeats the
    IDENTICAL rotation with the old key and token, without refreshing. The main recognises the repeat and issues
    tokens for the new key (`link.rotation_retry`) instead of freezing the link. Only a definite answer drops the
    stored rotation."""
    if row.status != "linked" or not row.link_id:
        raise LinkClientError("This node is not linked.")
    pending = pending_rotation(row)
    if pending is None:
        expires = _utc(row.access_expires_at)
        if not row.access_enc or expires is None or expires <= now() + timedelta(seconds=60):
            _refresh(session, settings, http, row)  # nothing is sent yet: a refresh is still safe
        previous_private = _key(settings, row.link_key)
        successor = Ed25519PrivateKey.generate()
        rotations = [*(row.link_rotations or []), lc.rotation_certificate(
            previous_private, link_node_id(row), len(row.link_rotations or []) + 1, lc.public_b64(successor))]
        pop = lc.sign_pop(successor, lc.rotation_parts(row.issuer_url, row.link_id, link_node_id(row),
                                                        rotations[-1]["body"]["key_id"], row.key_epoch + 1))
        pending = {"key": _seal(settings, _raw(successor)), "rotations": rotations, "pop": pop,
                   "started_at": now().isoformat()}
        row.flow = {**(row.flow or {}), "rotation": pending}
        session.commit()  # durable before the main can act on it
    if not row.access_enc:
        _drop_rotation(row)
        _gone(session, row, "No token is left to repeat the key rotation with; relink this node.")
    try:
        resp = _request(settings, http, row, "POST", "/api/v2/link/rotation",
                        {"rotations": pending["rotations"], "new_key_pop": pending["pop"]},
                        _open(settings, row.access_enc))
    except httpx.HTTPError as exc:
        row.last_error = f"The main did not answer the key rotation ({type(exc).__name__}); it is retried."
        session.flush()
        raise LinkClientError(row.last_error) from exc
    row.last_used_at = now()
    if resp.status_code == 200:
        old_key, old_previous, old_rotations = row.link_key, row.link_key_previous, row.link_rotations
        row.link_key, row.link_key_previous, row.link_rotations = pending["key"], old_key, pending["rotations"]
        try:
            _accept(session, settings, http, row, resp.json(), first=False)
        except (LinkClientError, ValueError, httpx.HTTPError) as exc:
            # the main has most likely moved to the new key: keep the stored rotation and repeat it later
            row.link_key, row.link_key_previous, row.link_rotations = old_key, old_previous, old_rotations
            row.last_error = f"The main's answer to the key rotation did not check out ({exc}); it is retried."[:200]
            session.flush()
            raise LinkClientError(row.last_error) from exc
        _drop_rotation(row)
        for c in session.scalars(select(UpstreamContribution).where(
                UpstreamContribution.main_node_id == row.main_node_id, UpstreamContribution.state == "queued")):
            _sign(settings, row, c)
        session.flush()
        return
    reason = _reason(resp)
    if resp.status_code == 403 or (resp.status_code == 401 and reason in ENDED + STALE_TOKEN):
        # the link or account is gone, or the old token is no longer known (a repeat after more than an hour)
        _drop_rotation(row)
        _gone(session, row, f"The main refused this link during a key rotation ({_error(resp)}); relink this node.")
    if resp.status_code in (400, 409):  # a definite refusal (a fork also freezes the link on the main)
        _drop_rotation(row)
        session.flush()
        raise LinkClientError(f"The main refused the new link key: {_error(resp)}")
    row.last_error = f"The main did not take the key rotation yet ({_error(resp)}); it is retried."[:200]
    session.flush()
    raise LinkClientError(row.last_error)


# ---- §7.2: approved local proposals with `link_push = auto` --------------------------------------------------

def on_local_approved(session: Session, ctx: Any, proposal: Any) -> UpstreamContribution | None:
    """Queue a proposal approved on this node when `link_push = auto` (never a proposal that came through a link
    to this node, never a kind a main does not take)."""
    settings = ctx.settings
    if getattr(settings, "link_push", "manual") != "auto" or getattr(proposal, "via_link_id", None):
        return None
    if proposal.kind not in KINDS:
        return None
    row = linked(session)
    if row is None:
        return None
    try:
        return propose(session, settings, ctx.identity, row, kind=proposal.kind, local_cid=proposal.canonical_id,
                       payload=dict(proposal.payload or {}), note="", user_id=None)
    except LinkClientError as exc:
        log.info("approved proposal %s not queued for the main: %s", proposal.id, exc)
        return None


# ---- the worker -----------------------------------------------------------------------------------------------

def round_(maker: Any, ctx: Any, http: Any) -> dict[str, int]:
    """One worker round: poll open flows, confirm pending unlinks, send the queue, pull decisions."""
    done = {"polled": 0, "sent": 0, "pulled": 0}
    if refusal(ctx.settings) is not None:
        return done
    with maker() as session:
        ids = [r.main_node_id for r in session.scalars(select(UpstreamLink))]
    for main in ids:
        with maker() as session:
            row = session.get(UpstreamLink, main)
            if row is None:
                continue
            try:
                if row.flow and row.status in ("starting", "linked") and "device_code" in row.flow:
                    poll(session, ctx.settings, http, row)
                    done["polled"] += 1
                elif row.status in ("revoked_locally", "pending_remote"):
                    confirm_unlink(session, ctx.settings, http, row)
                elif row.status == "linked" and pending_rotation(row) is not None:
                    rotate_key(session, ctx.settings, ctx.identity, http, row)  # finished before anything else
                elif row.status == "linked":
                    done["sent"] += send(session, ctx.settings, ctx.identity, http, row)["sent"]
                    done["pulled"] += pull(session, ctx.settings, ctx.identity, http, row)
                session.commit()
            except Exception as exc:  # noqa: BLE001 - one main's trouble never stops the others
                session.rollback()
                log.warning("link round for %s failed: %s", main, type(exc).__name__)
    return done


def counts(session: Session, main: str) -> dict[str, int]:
    from sqlalchemy import func

    return dict(session.execute(select(UpstreamContribution.state, func.count()).where(
        UpstreamContribution.main_node_id == main).group_by(UpstreamContribution.state)).all())


_ = json
