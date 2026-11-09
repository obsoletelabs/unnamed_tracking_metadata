"""Mutually verified enrolment of a node into an owner group (CONTRACT-C10 §2).

Inviter side: `create_invite`, `hello`, `reveal`, `confirm_inviter`, `complete`, `abort`.
Joiner side: `start_join` (hello + reveal), `confirm_joiner` (complete, then activation), `ping`.

`http` is anything with `post(url, json=..., timeout=...)` returning an object with `status_code` and `json()`
(an `httpx.Client`, or a test dispatcher). Codes and `k` are never logged or put in a URL (§2.2).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import group_crypto as gc
from . import groups, key_rotation
from .groups import GroupError, aware, utcnow
from .models import GroupEnrolment, OwnerGroup, ulid
from .settings import Settings
from .signing import NodeIdentity, canonical_json, key_id_for

INVITE_TTL = timedelta(minutes=10)
PENDING_TTL = timedelta(minutes=15)
MAX_FAILURES = 5
MAX_OPEN_INVITES = 3
HELLO_CAP = 64 * 1024
SMALL_CAP = 4 * 1024
OPEN_STATES = ("open", "hello", "revealed", "confirmed")
ENROL_PATH = "/api/v2/group/enrol"


def _invite_hash(invite_id: str) -> str:
    return gc.sha256_hex(invite_id.encode())


def _secret(settings: Settings, row: GroupEnrolment) -> dict:
    return json.loads(groups.open_secret(settings, row.secret_enc))


def _seal(settings: Settings, data: dict) -> str:
    return groups.seal_secret(settings, json.dumps(data).encode())


def _not_found() -> GroupError:
    return GroupError("Not Found", "not_found")


# ---- inviter -----------------------------------------------------------------------------------------------

def may_invite(session: Session, group: OwnerGroup, identity: NodeIdentity, now: datetime | None = None) -> str:
    """'root' when this node holds G, 'delegate' with a valid group.enrol delegation, else a GroupError."""
    if group.status != "active":
        raise GroupError(f"the group is {group.status} here", "conflict")
    if group.root_state == "holder" and group.root_sealed:
        return "root"
    cert = groups.trust(session, group, now, own_node=identity.node_id).in_force(identity.node_id)
    if cert and cert["body"]["delegate"] and "group.enrol" in cert["body"]["scopes"]:
        return "delegate"
    raise GroupError("this node can neither use the group root nor enrol by delegation", "forbidden")


def create_invite(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity,
                  scopes: list[str] | None = None, delegate: dict | None = None, actor: str | None = None,
                  now: datetime | None = None, cli: bool = False) -> tuple[GroupEnrolment, str]:
    now = now or utcnow()
    kind = may_invite(session, group, identity, now)
    scopes = list(scopes if scopes is not None else groups.DEFAULT_SCOPES)
    if not set(scopes) <= set(groups.SCOPES) or len(set(scopes)) != len(scopes):
        raise GroupError("unknown scope")
    if bool(set(scopes) & set(groups.DELEGATE_SCOPES)) != (delegate is not None):
        raise GroupError("group.enrol and grant.issue come with a delegate object, and only with one")
    if delegate is not None:
        if kind != "root":
            raise GroupError("only the root may delegate (depth 1)", "forbidden")
        if not groups._delegate_ok(_with_self(delegate, None), group.genesis["body"]["policy"]):
            raise GroupError("the delegate limits are out of bounds")
        if ("grant.issue" in scopes) != bool(delegate["may_grant_roles"]) or (
                "grant.issue" in scopes and not delegate["may_grant_nodes"]):
            raise GroupError("grant.issue needs the roles and the nodes the new member may grant, and only it")
    groups.expire_enrolments(session, now)
    # Serialise invite creation per group (PostgreSQL READ COMMITTED would let two requests both see 2 open).
    session.scalars(select(OwnerGroup).where(OwnerGroup.group_id == group.group_id).with_for_update()).one()
    open_count = session.scalar(select(func.count()).select_from(GroupEnrolment).where(
        GroupEnrolment.side == "inviter", GroupEnrolment.group_id == group.group_id,
        GroupEnrolment.state.in_(OPEN_STATES)))
    if open_count >= MAX_OPEN_INVITES:
        raise GroupError(f"at most {MAX_OPEN_INVITES} invites can be open at once; cancel one first", "conflict")
    invite = gc.Invite(group.group_id, identity.node_id, secrets.token_hex(8), secrets.token_bytes(16))
    row = GroupEnrolment(id=ulid(), side="inviter", group_id=group.group_id, invite_hash=_invite_hash(invite.invite_id),
                         secret_enc=_seal(settings, {"k": gc.b64(invite.k), "kind": kind}), scopes=scopes,
                         delegate=delegate, state="open", failures=0, created_by=actor, created_at=now,
                         expires_at=now + INVITE_TTL)
    session.add(row)
    session.flush()
    groups.audit(session, actor, "group_invite_created", row.id, {"scopes": scopes, "delegate": bool(delegate)}, cli)
    return row, gc.encode_code(invite)


SELF = "self"  # in an invite's `may_grant_nodes`: the joining node itself, resolved when the certificate is issued


def _with_self(delegate: dict, node_id: str | None) -> dict:
    """The delegate with `self` replaced by the joiner's node id (or dropped, for checking the invite)."""
    nodes = [node_id if n == SELF else n for n in delegate.get("may_grant_nodes") or [] if n != SELF or node_id]
    return {**delegate, "may_grant_nodes": list(dict.fromkeys(nodes))}


def _fail(session: Session, row: GroupEnrolment, why: str) -> GroupError:
    """Count a MAC or PoP failure; the fifth burns the invite (§2.5). The caller commits before answering.

    Both writes are single atomic UPDATEs guarded by the current state, so concurrent failures are never lost
    and the invite is burned exactly once."""
    session.flush()
    session.execute(update(GroupEnrolment).where(GroupEnrolment.id == row.id)
                    .values(failures=func.coalesce(GroupEnrolment.failures, 0) + 1)
                    .execution_options(synchronize_session=False))
    burned = session.execute(update(GroupEnrolment).where(
        GroupEnrolment.id == row.id, GroupEnrolment.failures >= MAX_FAILURES,
        GroupEnrolment.state.in_(OPEN_STATES)).values(state="burned").execution_options(synchronize_session=False))
    session.refresh(row)
    if burned.rowcount:
        groups.audit(session, None, "group_invite_burned", row.id, {"failures": row.failures, "last": why})
    session.flush()
    return _not_found()


def _joiner_key(joiner: dict) -> tuple[str, str]:
    """(key_id, public) of the joiner's current signing key from its own validated history (§1.7)."""
    node_id, root_public = joiner["node_id"], joiner["root_public_key"]
    if key_id_for(gc.unb64(root_public, 32)) != node_id:
        raise GroupCryptoMismatch("node_id is not the key id of root_public_key")
    history = joiner.get("rotations") or []
    tip = joiner.get("signing_key_id") or node_id
    try:
        _, keys = key_rotation.validate_history(node_id, root_public, history, tip)
    except key_rotation.RotationError as exc:
        raise GroupCryptoMismatch(str(exc)) from None
    return tip, keys[tip]


class GroupCryptoMismatch(Exception):
    pass


def hello(session: Session, settings: Settings, identity: NodeIdentity, body: Any,
          now: datetime | None = None) -> dict:
    now = now or utcnow()
    if not isinstance(body, dict) or not isinstance(body.get("invite_id"), str):
        raise _not_found()
    row = session.scalars(select(GroupEnrolment).where(
        GroupEnrolment.invite_hash == _invite_hash(body["invite_id"]), GroupEnrolment.side == "inviter")
        .with_for_update()).first()  # a concurrent hello on the same invite waits, then sees it used
    if row is None or row.state != "open" or aware(row.expires_at) < now:  # type: ignore[operator]
        raise _not_found()  # unknown, used or expired invites look like a missing route (§2.5)
    group = session.get(OwnerGroup, row.group_id)
    if group is None or group.status != "active":
        raise _not_found()
    k = gc.unb64(_secret(settings, row)["k"], 16)
    t1_obj = {key: v for key, v in body.items() if key not in ("pop", "mac")}
    t1 = canonical_json(t1_obj)
    if not gc.mac_ok(k, "joiner-hello", t1, body.get("mac")):
        raise _fail(session, row, "hello_mac")
    try:
        joiner = body["joiner"]
        member, member_pop = joiner["member"], joiner["member_pop"]
        if body.get("v") != 1 or body.get("group_id") != group.group_id:
            raise GroupCryptoMismatch("wrong group")
        if abs((now - gc.parse_time(body["issued_at"])).total_seconds()) > gc.SKEW:
            raise GroupCryptoMismatch("issued_at outside the clock skew")
        key_id, public = _joiner_key(joiner)
        if not gc.verify_enrol_pop(public, "joiner-hello", body.get("pop", ""), gc.sha256_hex(t1)):
            raise GroupCryptoMismatch("joiner pop")
        if member.get("node_id") != joiner["node_id"] or member.get("root_public_key") != joiner["root_public_key"]:
            raise GroupCryptoMismatch("member object is not the joiner's")
        if not groups._member_ok(member) or "url_verified" in member:
            raise GroupCryptoMismatch("member object malformed")
        if member_pop.get("key_id") != key_id or not gc.verify_member_pop(public, group.group_id, member, member_pop):
            raise GroupCryptoMismatch("member pop")
        commit_j = body["commit_j"]
        if not isinstance(commit_j, str) or len(commit_j) != 64:
            raise GroupCryptoMismatch("commitment")
    except (GroupCryptoMismatch, KeyError, TypeError, AttributeError, gc.GroupCryptoError) as exc:
        raise _fail(session, row, f"hello:{str(exc)[:60]}") from None
    # §1.4 step 8 and the member rules, before anything is shown to the admin
    _admission(session, settings, group, member, identity.node_id, now)
    tr = groups.trust(session, group, now, own_node=identity.node_id)
    own = tr.in_force(identity.node_id)
    if own is None:
        raise _not_found()
    eid = ulid()
    reply = {"v": 1, "enrolment_id": eid,
             "group": {"genesis": group.genesis, "succession": list(group.succession or [])},
             "inviter": {"node_id": identity.node_id, "root_public_key": identity.trust_public_b64,
                         "signing_key_id": identity.key_id,
                         "rotations": key_rotation.own_history(session, identity.node_id),
                         "box_public_key": own["body"]["member"]["box_public_key"],
                         "url": own["body"]["member"]["url"], "name": own["body"]["member"]["name"]},
             "inviter_kind": _secret(settings, row).get("kind"), "inviter_cert": own,
             "offered_scopes": list(row.scopes), "nonce_i": gc.b64(secrets.token_bytes(32))}
    th = gc.transcript_hash(t1, canonical_json(reply))
    reply["transcript_hash"] = th
    reply["pop"] = gc.enrol_pop(identity.private, "inviter", th)
    reply["mac"] = gc.enrol_mac(k, "inviter", th.encode())
    # The invite row becomes the enrolment: invite_id is single use from here on.
    old_id = row.id
    row.id = eid
    row.state = "hello"
    row.transcript = {"hello": body, "reply": {k2: v for k2, v in reply.items() if k2 not in ("pop", "mac")}}
    row.transcript_hash = th
    row.commit_j = body["commit_j"]
    row.peer_node_id = joiner["node_id"]
    row.peer_url = member["url"]
    row.expires_at = now + PENDING_TTL
    groups.audit(session, None, "group_enrol_hello", eid, {"invite": old_id, "node_id": joiner["node_id"]})
    session.flush()
    return reply


def _admission(session: Session, settings: Settings, group: OwnerGroup, member: dict, own_node: str,
               now: datetime) -> None:
    tr = groups.trust(session, group, now, own_node=own_node)
    node = member["node_id"]
    if node == own_node:
        raise GroupError("a node cannot enrol itself", "conflict")
    if node in tr.banned:
        raise GroupError("that node was removed from the group; only a root-issued readmission can bring it back",
                         "forbidden")
    if any(c["body"]["member"]["root_public_key"] != member["root_public_key"] for c in tr.node_certs(node)):
        raise GroupError("a certificate with another root key exists for that node id", "conflict")
    held = [c for c in tr.node_certs(node) if tr.valid(c).ok]
    if held:
        raise GroupError("that node is already a member here. If it left the group, this node has not seen that yet: "
                         "use 'Sync with members now' on this node, then send a new code", "conflict")
    try:
        groups.clean_member_url(member["url"], settings.environment == "production")
    except GroupError:
        raise
    others = tr.members()
    if len(others) + 1 > group.genesis["body"]["policy"]["max_members"]:
        raise GroupError("the group already has its maximum number of members", "conflict")
    for other, cert in others.items():
        m = cert["body"]["member"]
        if groups.normal_origin(m["url"]) == groups.normal_origin(member["url"]):
            raise GroupError(f"another member ({groups.gc_fp(other)}) already uses that address", "conflict")
        if m["name"].casefold() == member["name"].casefold():
            raise GroupError(f"another member ({groups.gc_fp(other)}) already uses that name", "conflict")


def _row(session: Session, eid: Any, side: str) -> GroupEnrolment:
    if not isinstance(eid, str) or len(eid) != 26:
        raise _not_found()
    row = session.scalars(select(GroupEnrolment).where(GroupEnrolment.id == eid).with_for_update()).first()
    if row is None or row.side != side:
        raise _not_found()
    return row


def reveal(session: Session, settings: Settings, body: Any, now: datetime | None = None) -> dict:
    now = now or utcnow()
    row = _row(session, body.get("enrolment_id") if isinstance(body, dict) else None, "inviter")
    if row.state != "hello" or aware(row.expires_at) < now:  # type: ignore[operator]
        raise _not_found()
    k = gc.unb64(_secret(settings, row)["k"], 16)
    nonce_b64 = body.get("nonce_j")
    data = f"{row.transcript_hash}\n{row.id}\n{nonce_b64}".encode()
    if not gc.mac_ok(k, "joiner-reveal", data, body.get("mac")):
        raise _fail(session, row, "reveal_mac")
    try:
        nonce_j = gc.unb64(nonce_b64, 32)
    except (gc.GroupCryptoError, TypeError):
        raise _fail(session, row, "reveal_nonce") from None
    if gc.commitment(nonce_j) != row.commit_j:
        raise _fail(session, row, "commitment")  # counts as a MAC failure (§2.3)
    row.nonce_j = nonce_b64
    row.state = "revealed"
    session.flush()
    return {"v": 1, "status": "revealed"}


def sas_of(settings: Settings, row: GroupEnrolment) -> str | None:
    if not row.nonce_j or not row.transcript_hash:
        return None
    secret = _secret(settings, row)
    return gc.sas(gc.unb64(secret["k"], 16), row.transcript_hash, gc.unb64(row.nonce_j, 32))


def check_url(http: Any, row: GroupEnrolment, timeout: float = 5.0) -> bool:
    """Best-effort: the joiner answers a signed ping at its claimed url (H30). Failure is not fatal."""
    if http is None or not row.peer_url:
        return False
    try:
        member = row.transcript["hello"]["joiner"]
        _, public = _joiner_key(member)
        resp = http.get(f"{row.peer_url.rstrip('/')}{ENROL_PATH}/ping", params={"e": row.id}, timeout=timeout)
        if resp.status_code != 200:
            return False
        return gc.verify_enrol_pop(public, "joiner-url", resp.json().get("sig", ""), row.transcript_hash)
    except Exception:  # noqa: BLE001 - any failure is "address not verified"
        return False


def confirm_inviter(session: Session, settings: Settings, group: OwnerGroup, identity: NodeIdentity, eid: str,
                    root_passphrase: str | None, *, http: Any = None, actor: str | None = None,
                    now: datetime | None = None, cli: bool = False) -> dict:
    """The inviter's admin compared the SAS and confirms: the certificate is issued (§2.1)."""
    now = now or utcnow()
    row = _row(session, eid, "inviter")
    if row.state != "revealed" or aware(row.expires_at) < now:  # type: ignore[operator]
        raise GroupError("this enrolment is not waiting for a SAS confirmation", "conflict")
    kind = _secret(settings, row).get("kind")
    root = groups.unlock_root(session, settings, group, root_passphrase or "") if kind == "root" else None
    if kind == "delegate":
        fresh = groups.freshness(session, group, identity.node_id, None, settings.group_view_max_age_hours, now,
                                 action="enrol")
        if not fresh.ok:
            raise GroupError(f"a delegated enrolment needs a fresh view of the group: {fresh.reason}", "conflict")
    hello_body = row.transcript["hello"]
    member = hello_body["joiner"]["member"]
    _admission(session, settings, group, member, identity.node_id, now)
    row.url_verified = check_url(http, row)
    policy = group.genesis["body"]["policy"]
    days = policy["delegate_max_days"] if row.delegate else policy["cert_max_days"]
    delegate = _with_self(row.delegate, member["node_id"]) if row.delegate else None
    cert = groups.issue_cert(session, group, identity, member, hello_body["joiner"]["member_pop"], list(row.scopes),
                             delegate, signer_root=root, days=days,
                             enrolment={"id": row.id, "transcript_hash": row.transcript_hash},
                             url_verified=row.url_verified, now=now, actor=actor, cli=cli)
    del root
    row.state, row.cert_serial, row.confirmed_at = "issued", cert["body"]["serial"], now
    groups.audit(session, actor, "group_enrol_confirmed", row.id,
                 {"node_id": member["node_id"], "serial": cert["body"]["serial"], "url_verified": row.url_verified},
                 cli)
    session.flush()
    return cert


def _joiner_proof(session: Session, settings: Settings, row: GroupEnrolment, body: Any, role: str) -> None:
    k = gc.unb64(_secret(settings, row)["k"], 16)
    data = f"{row.transcript_hash}\n{row.id}".encode()
    if not isinstance(body, dict) or not gc.mac_ok(k, role, data, body.get("mac")):
        raise _fail(session, row, f"{role}_mac")
    try:
        _, public = _joiner_key(row.transcript["hello"]["joiner"])
    except GroupCryptoMismatch:
        raise _fail(session, row, f"{role}_key") from None
    if not gc.verify_enrol_pop(public, role, body.get("pop", ""), row.transcript_hash, row.id):
        raise _fail(session, row, f"{role}_pop")


def complete(session: Session, settings: Settings, identity: NodeIdentity, body: Any,
             now: datetime | None = None) -> tuple[int, dict]:
    """(202, pending) | (200, sealed {status, cert, state}) | (410, gone)."""
    now = now or utcnow()
    row = _row(session, body.get("enrolment_id") if isinstance(body, dict) else None, "inviter")
    if row.state in ("burned",):
        raise _not_found()
    _joiner_proof(session, settings, row, body, "joiner-complete")
    if row.state in ("aborted", "expired") or (row.state in OPEN_STATES and aware(row.expires_at) < now):  # type: ignore[operator]
        if row.state in OPEN_STATES:
            row.state = "expired"
        return 410, {"status": "gone"}
    if row.state != "issued" and row.state != "active":
        return 202, {"status": "pending"}
    group = session.get(OwnerGroup, row.group_id)
    cert = groups.trust(session, group, now).certs.get(row.cert_serial or "")  # type: ignore[arg-type]
    if group is None or cert is None:
        return 410, {"status": "gone"}
    plain = {"status": "issued", "cert": cert, "state": groups.export_state(session, group, identity, now)}
    sealed = gc.seal_response(cert["body"]["member"]["box_public_key"], gc.enrol_request_nonce(row.transcript_hash),
                              group.group_id, identity.node_id, row.peer_node_id, plain)  # type: ignore[arg-type]
    if row.state == "issued":
        row.state = "active"
        groups.audit(session, None, "group_enrol_delivered", row.id, {"serial": row.cert_serial})
    session.flush()
    return 200, sealed


def abort(session: Session, settings: Settings, identity: NodeIdentity, body: Any,
          now: datetime | None = None) -> dict:
    now = now or utcnow()
    row = _row(session, body.get("enrolment_id") if isinstance(body, dict) else None, "inviter")
    if row.state == "burned":
        raise _not_found()
    _joiner_proof(session, settings, row, body, "joiner-abort")
    _abort_inviter(session, settings, identity, row, now, actor=None, why="joiner")
    return {"v": 1, "status": "aborted"}


def _abort_inviter(session: Session, settings: Settings, identity: NodeIdentity, row: GroupEnrolment,
                   now: datetime, actor: str | None, why: str) -> None:
    issued = row.cert_serial if row.state in ("issued", "active") else None
    row.state = "aborted"
    revoked = False
    if issued:
        group = session.get(OwnerGroup, row.group_id)
        tr = groups.trust(session, group, now, own_node=identity.node_id)  # type: ignore[arg-type]
        cert = tr.certs.get(issued)
        if cert is not None and cert["body"]["issuer"] == identity.node_id:
            groups.sign_crl(session, group, [groups.crl_entry(cert, "enrolment_aborted", now)], root=None,  # type: ignore[arg-type]
                            identity=identity, now=now)
            groups.rebuild(session, group, identity.node_id, now)  # type: ignore[arg-type]
            revoked = True
        else:  # root-issued: G is passphrase-locked, so an admin revokes it (documented limitation)
            groups.notify_admins(session, f"group-enrol-abort:{row.id}",
                                 "An enrolment was aborted after its certificate was issued; revoke that node")
    groups.audit(session, actor, "group_enrol_aborted", row.id, {"by": why, "serial": issued, "revoked": revoked})
    session.flush()


def cancel(session: Session, settings: Settings, identity: NodeIdentity, eid: str, actor: str | None = None,
           now: datetime | None = None) -> None:
    now = now or utcnow()
    row = session.get(GroupEnrolment, eid)
    if row is None:
        raise GroupError("no such enrolment", "not_found")
    if row.side == "inviter":
        _abort_inviter(session, settings, identity, row, now, actor, "admin")
    else:
        row.state = "aborted"
        groups.audit(session, actor, "group_enrol_aborted", row.id, {"by": "admin", "side": "joiner"})


# ---- joiner ------------------------------------------------------------------------------------------------

def ping(session: Session, identity: NodeIdentity, eid: Any) -> dict:
    row = _row(session, eid, "joiner")
    if row.state not in ("revealed", "confirmed") or not row.transcript_hash:
        raise _not_found()
    return {"sig": gc.enrol_pop(identity.private, "joiner-url", row.transcript_hash)}


def _post(http: Any, url: str, path: str, payload: dict) -> tuple[int, Any]:
    try:
        resp = http.post(f"{url.rstrip('/')}{ENROL_PATH}/{path}", json=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        raise GroupError(f"the inviter could not be reached: {type(exc).__name__}", "unavailable") from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data


def _detail(reply: Any) -> str:
    """The inviter's reason (#122). It answers with one only after the code's MAC verified (§2.5), so showing it
    reveals nothing to someone without the code; it is cut and stripped of control characters."""
    detail = reply.get("detail") if isinstance(reply, dict) else None
    if not isinstance(detail, str) or not detail.strip():
        return ""
    return ": " + "".join(c for c in detail if c.isprintable())[:200]


def start_join(session: Session, settings: Settings, identity: NodeIdentity, url: str, code: str, http: Any, *,
               name: str | None = None, member_url: str | None = None, actor: str | None = None,
               now: datetime | None = None, cli: bool = False) -> tuple[GroupEnrolment, str, dict]:
    """hello + reveal. Returns the joiner row, the SAS to compare and the verified inviter summary."""
    now = now or utcnow()
    current = groups.load(session)
    if current is not None and current.status not in ("left", "revoked"):
        raise GroupError("this node is already in a group (one group per node, D10.5)", "conflict")
    try:
        invite = gc.decode_code(code)
    except gc.GroupCryptoError as exc:
        raise GroupError(str(exc)) from None
    inviter_url = groups.clean_member_url(url, settings.environment == "production")
    production = settings.environment == "production"
    own_url = groups.clean_member_url(member_url or settings.public_url, production)
    box_sealed, box_public = groups._box_new(settings)
    member = groups.own_member(settings, identity, box_public, name, own_url)
    nonce_j = secrets.token_bytes(32)
    hello_obj = {"v": 1, "invite_id": invite.invite_id, "group_id": invite.group_id, "issued_at": gc.stamp(now),
                 "joiner": {"node_id": identity.node_id, "root_public_key": identity.trust_public_b64,
                            "signing_key_id": identity.key_id,
                            "rotations": key_rotation.own_history(session, identity.node_id), "member": member,
                            "member_pop": gc.member_pop(identity.private, invite.group_id, member, identity.key_id),
                            "atlas_protocol": "1.1", "group_protocol": groups.GROUP_PROTOCOL},
                 "commit_j": gc.commitment(nonce_j)}
    t1 = canonical_json(hello_obj)
    payload = {**hello_obj, "pop": gc.enrol_pop(identity.private, "joiner-hello", gc.sha256_hex(t1)),
               "mac": gc.enrol_mac(invite.k, "joiner-hello", t1)}
    status, reply = _post(http, inviter_url, "hello", payload)
    if status == 404:
        raise GroupError("the inviter does not know this code: it is mistyped, used, expired or burned", "not_found")
    if status != 200 or not isinstance(reply, dict):
        raise GroupError(f"the inviter refused the hello ({status}){_detail(reply)}", "conflict")
    inviter = _verify_reply(invite, t1, reply)
    eid = reply["enrolment_id"]
    reveal_mac = gc.enrol_mac(invite.k, "joiner-reveal", f"{reply['transcript_hash']}\n{eid}\n{gc.b64(nonce_j)}".encode())
    status, _ = _post(http, inviter_url, "reveal", {"enrolment_id": eid, "nonce_j": gc.b64(nonce_j), "mac": reveal_mac})
    if status != 200:
        raise GroupError(f"the inviter refused the reveal ({status})", "conflict")
    th = reply["transcript_hash"]
    row = GroupEnrolment(id=eid, side="joiner", group_id=invite.group_id, invite_hash=_invite_hash(invite.invite_id),
                         secret_enc=_seal(settings, {"k": gc.b64(invite.k), "box": box_sealed}), peer_url=inviter_url,
                         peer_node_id=invite.inviter, scopes=list(reply.get("offered_scopes") or []), delegate=None,
                         transcript={"hello": payload, "reply": reply, "member": member}, transcript_hash=th,
                         commit_j=hello_obj["commit_j"], nonce_j=gc.b64(nonce_j), state="revealed", failures=0,
                         created_by=actor, created_at=now, expires_at=now + PENDING_TTL)
    session.add(row)
    session.flush()
    groups.audit(session, actor, "group_join_started", eid,
                 {"group_id": invite.group_id, "inviter": invite.inviter}, cli)
    return row, gc.sas(invite.k, th, nonce_j), inviter


def _verify_reply(invite: gc.Invite, t1: bytes, reply: dict) -> dict:
    """J's checks before showing anything (§2.3)."""
    try:
        if not gc.mac_ok(invite.k, "inviter", str(reply.get("transcript_hash", "")).encode(), reply.get("mac")):
            raise GroupError("the inviter's answer does not carry the code's MAC: it is not the inviter")
        t2 = canonical_json({k: v for k, v in reply.items() if k not in ("transcript_hash", "pop", "mac")})
        th = gc.transcript_hash(t1, t2)
        if th != reply["transcript_hash"]:
            raise GroupError("the transcript hash does not match")
        inviter, genesis = reply["inviter"], reply["group"]["genesis"]
        if inviter["node_id"] != invite.inviter or genesis.get("group_id") != invite.group_id:
            raise GroupError("the answer comes from another node or group than the code names")
        if not groups.verify_genesis(genesis, invite.group_id):
            raise GroupError("the group genesis does not verify")
        if gc.key_id_of(genesis["body"]["root_public_key"]) != invite.group_id:
            raise GroupError("the genesis root key is not the group id")
        shadow = OwnerGroup(group_id=invite.group_id, genesis=genesis, succession=[], recovery=genesis["body"]["recovery"])
        previous = invite.group_id
        for i, obj in enumerate(reply["group"]["succession"] or [], start=1):
            pinned = groups._pinned_at(shadow, i) if i > 1 else genesis["body"]["recovery"]
            if not groups.verify_succession(obj, shadow, i, previous, pinned):
                raise GroupError("a root succession in the answer does not verify")
            shadow.succession = [*shadow.succession, obj]
            previous = obj["body"]["new_root_key_id"]
        _, public = _joiner_key(inviter)
        if not gc.verify_enrol_pop(public, "inviter", reply["pop"], th):
            raise GroupError("the inviter's proof of possession does not verify")
        _inviter_cert_ok(shadow, inviter, reply)
    except (KeyError, TypeError, AttributeError, GroupCryptoMismatch, gc.GroupCryptoError) as exc:
        raise GroupError(f"the inviter's answer is malformed or does not verify ({type(exc).__name__})") from None
    return inviter


def _inviter_cert_ok(shadow: OwnerGroup, inviter: dict, reply: dict) -> None:
    """J's check (§2.3): the inviter's own certificate verifies from the pinned genesis and binds the inviter's
    keys, url and box key; a delegated inviter's certificate carries `group.enrol` with room in `delegate`."""
    cert = reply.get("inviter_cert")
    if not isinstance(cert, dict):
        raise GroupError("the inviter sent no certificate")
    rotations = {inviter["node_id"]: inviter.get("rotations") or []}
    tr = groups.Trust(shadow, {cert["body"]["serial"]: cert}, {}, {}, rotations, utcnow())
    kind = reply.get("inviter_kind")
    if kind == "delegate" and cert["signer"]["kind"] != "root":
        raise GroupError("a delegated inviter's certificate must be root-issued (depth 1)")
    if cert["signer"]["kind"] == "root":  # a root holder enrolled by a delegate is checked at activation instead
        verdict = tr.valid(cert)
        if not verdict.ok:
            raise GroupError(f"the inviter's certificate does not verify ({verdict.reason})")
    member = cert["body"]["member"]
    if member["node_id"] != inviter["node_id"] or member["root_public_key"] != inviter["root_public_key"] \
            or member["box_public_key"] != inviter["box_public_key"] or member["url"] != inviter["url"]:
        raise GroupError("the inviter's certificate is for another node or address")
    if reply.get("inviter_kind") == "delegate":
        delegate = cert["body"]["delegate"] or {}
        if "group.enrol" not in cert["body"]["scopes"] or not delegate.get("may_enrol") \
                or not set(reply.get("offered_scopes") or []) <= set(delegate.get("may_grant_scopes") or []):
            raise GroupError("the inviter is not delegated to enrol nodes with these scopes")
    elif reply.get("inviter_kind") != "root":
        raise GroupError("the inviter does not say whether it enrols as root or as a delegate")


def confirm_joiner(session: Session, settings: Settings, identity: NodeIdentity, eid: str, http: Any, *,
                   actor: str | None = None, now: datetime | None = None, cli: bool = False) -> str:
    """The joiner's admin compared the SAS. Ask the inviter to complete; returns 'pending', 'active' or 'aborted'."""
    now = now or utcnow()
    row = _row(session, eid, "joiner")
    if row.state == "revealed":
        row.state, row.confirmed_at = "confirmed", now
        groups.audit(session, actor, "group_join_confirmed", row.id, {}, cli)
    if row.state != "confirmed":
        raise GroupError(f"this enrolment is {row.state}", "conflict")
    secret = _secret(settings, row)
    k = gc.unb64(secret["k"], 16)
    payload = {"enrolment_id": row.id,
               "pop": gc.enrol_pop(identity.private, "joiner-complete", row.transcript_hash, row.id),
               "mac": gc.enrol_mac(k, "joiner-complete", f"{row.transcript_hash}\n{row.id}".encode())}
    status, data = _post(http, row.peer_url or "", "complete", payload)
    if status == 202:
        session.flush()
        return "pending"
    if status == 410:
        row.state = "aborted"
        return "aborted"
    if status != 200:
        raise GroupError(f"the inviter refused to complete ({status})", "conflict")
    box = gc.x_from_raw(groups.open_secret(settings, secret["box"]))
    try:
        plain = gc.open_response([box], gc.enrol_request_nonce(row.transcript_hash), row.group_id, row.peer_node_id,
                                 identity.node_id, data)
    except gc.GroupCryptoError:
        raise GroupError("the completion is not sealed to this node's key") from None
    _activate(session, settings, identity, row, plain, secret["box"], now, actor, cli)
    return "active"


def _activate(session: Session, settings: Settings, identity: NodeIdentity, row: GroupEnrolment, plain: Any,
              box_sealed: str, now: datetime, actor: str | None, cli: bool) -> OwnerGroup:
    try:
        cert, state = plain["cert"], plain["state"]
        genesis = state["genesis"]
        if plain.get("status") != "issued":
            raise GroupError("the completion is not an issued certificate")
        if genesis["body_hash"] != row.transcript["reply"]["group"]["genesis"]["body_hash"]:
            raise GroupError("the completion carries another genesis than the one compared with the SAS")
        mine = {k: v for k, v in cert["body"]["member"].items() if k != "url_verified"}
        if mine != row.transcript["member"]:
            raise GroupError("the certificate does not bind this node's member object")
        if cert["body"].get("enrolment", {}).get("transcript_hash") != row.transcript_hash:
            raise GroupError("the certificate is for another enrolment")
    except (KeyError, TypeError, AttributeError):
        raise GroupError("the completion is malformed") from None
    old = groups.load(session)
    if old is not None:
        session.delete(old)
        session.flush()
    group = OwnerGroup(group_id=row.group_id, name=genesis["body"]["name"], genesis=genesis, succession=[],
                       root_key_id=row.group_id, recovery=genesis["body"]["recovery"], root_sealed=None,
                       box_sealed=box_sealed, root_state="none", status="active", joined_at=now, updated_at=now)
    session.add(group)
    session.flush()
    groups.ingest_state(session, group, state, identity.node_id, now, settings)
    tr = groups.trust(session, group, now, own_node=identity.node_id)
    own = tr.in_force(identity.node_id)
    if own is None or own["body"]["serial"] != cert["body"]["serial"]:
        verdict = tr.valid(cert).reason if cert["body"]["serial"] in tr.certs else "missing"
        raise GroupError(f"the issued certificate does not verify here ({verdict})")
    row.state, row.cert_serial = "active", cert["body"]["serial"]
    groups.audit(session, actor, "group_joined", group.group_id,
                 {"serial": cert["body"]["serial"], "inviter": row.peer_node_id}, cli)
    session.flush()
    return group
