"""Owner-group HTTP endpoints (CONTRACT-C10 §2.3, §3.2): enrolment and the single sealed member call.

Every node mounts these routes, member or not. Anything that does not prove an open invite or membership gets
the same 404 body as an unknown route (`{"detail": "Not Found"}`), so the routes reveal nothing.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.concurrency import run_in_threadpool

from .. import group_crypto as gc
from .. import group_enrol, groups, key_rotation, ratelimit
from ..models import GroupNonce, OwnerGroup
from ..signing import NodeIdentity, canonical_json
from .deps import AppState

router = APIRouter(include_in_schema=False)
CALL_CAP = 256 * 1024
CALL_OPS = ("hello", "state", "rotations")  # A10; A11 adds heads/changes/snapshot/notify
NOT_FOUND = {"detail": "Not Found"}


def not_found() -> JSONResponse:
    return JSONResponse(NOT_FOUND, status_code=404)


def too_large() -> JSONResponse:
    return JSONResponse({"detail": "request body too large"}, status_code=413)


async def _body(request: Request, cap: int) -> bytes | None:
    """Read at most `cap` bytes, checking Content-Length first and then while streaming (§2.5)."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > cap:
        return None
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > cap:
            return None
    return bytes(data)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _throttled(st: AppState, key: str, limit: int, window: int) -> JSONResponse | None:
    ok, retry = ratelimit.hit(st.maker, key, limit, window)
    if ok:
        return None
    return JSONResponse({"detail": "Too many requests"}, status_code=429, headers={"Retry-After": str(retry)})


def _run_enrol(st: AppState, step: str, payload: Any) -> JSONResponse:
    settings, identity = st.ctx.settings, st.ctx.identity
    with st.maker() as session:
        try:
            if step == "hello":
                result: tuple[int, Any] = (200, group_enrol.hello(session, settings, identity, payload))
            elif step == "reveal":
                result = (200, group_enrol.reveal(session, settings, payload))
            elif step == "complete":
                result = group_enrol.complete(session, settings, identity, payload)
            else:
                result = (200, group_enrol.abort(session, settings, identity, payload))
            session.commit()
        except groups.GroupError as exc:
            session.commit()  # failure counters and burned invites persist (§2.5)
            if exc.code == "not_found":
                return not_found()
            return JSONResponse({"detail": str(exc)}, status_code=409 if exc.code == "conflict" else 400)
        except StaleDataError:  # a concurrent request consumed the same invite or enrolment first
            session.rollback()
            return not_found()
    return JSONResponse(result[1], status_code=result[0])


async def _enrol(request: Request, step: str, cap: int) -> JSONResponse:
    st: AppState = request.app.state.atlas
    if (limited := await run_in_threadpool(_throttled, st, f"group-enrol-ip:{_ip(request)}", 10, 600)):
        return limited
    raw = await _body(request, cap)
    if raw is None:
        return too_large()
    try:
        payload = json.loads(raw)
    except ValueError:
        return not_found()
    if not isinstance(payload, dict):
        return not_found()
    return await run_in_threadpool(_run_enrol, st, step, payload)


@router.post("/api/v2/group/enrol/hello")
async def enrol_hello(request: Request):
    return await _enrol(request, "hello", group_enrol.HELLO_CAP)


@router.post("/api/v2/group/enrol/reveal")
async def enrol_reveal(request: Request):
    return await _enrol(request, "reveal", group_enrol.SMALL_CAP)


@router.post("/api/v2/group/enrol/complete")
async def enrol_complete(request: Request):
    return await _enrol(request, "complete", group_enrol.SMALL_CAP)


@router.post("/api/v2/group/enrol/abort")
async def enrol_abort(request: Request):
    return await _enrol(request, "abort", group_enrol.SMALL_CAP)


def _ping(st: AppState, eid: str | None) -> JSONResponse:
    with st.maker() as session:
        try:
            return JSONResponse(group_enrol.ping(session, st.ctx.identity, eid))
        except groups.GroupError:
            return not_found()


@router.get("/api/v2/group/enrol/ping")
async def enrol_ping(request: Request):
    st: AppState = request.app.state.atlas
    if (limited := await run_in_threadpool(_throttled, st, f"group-enrol-ip:{_ip(request)}", 10, 600)):
        return limited
    return await run_in_threadpool(_ping, st, request.query_params.get("e"))


# ---- the sealed member call (§3.2) ---------------------------------------------------------------------------

class _Reject(Exception):
    """Any failure up to and including the signature check: the uniform 404."""


def _auth_failed(st: AppState, why: str) -> None:
    """Aggregated audit (one row per 10 minutes per failure step) so probing cannot flood the audit log; the
    body is never recorded."""
    window = int(time.time() // 600)
    ok, _ = ratelimit.hit(st.maker, f"group-auth-audit:{why}:{window}", 1, 600)
    if ok:
        with st.maker() as session:
            groups.audit(session, None, "group_request_auth_failed", why, {"window": window})
            session.commit()


def serve_call(st: AppState, body: Any, now: float | None = None) -> JSONResponse:
    settings, identity = st.ctx.settings, st.ctx.identity
    now = now if now is not None else time.time()
    with st.maker() as session:
        group = groups.active(session)
        if group is None:
            gc.dummy_open(body)
            return not_found()
        try:
            try:
                inner = gc.open_request(groups.box_keys(settings, group), group.group_id, identity.node_id, body)
            except (gc.GroupCryptoError, groups.GroupError):
                raise _Reject("decrypt") from None
            fields = ("v", "g", "n", "aud", "k", "t", "r", "proto", "op", "params", "s")
            if set(inner) != set(fields) or inner["v"] != 1 or type(inner["t"]) is not int \
                    or not isinstance(inner["params"], dict):
                raise _Reject("shape")
            if not all(isinstance(inner[f], str) for f in ("g", "n", "aud", "k", "r", "proto", "op", "s")):
                raise _Reject("shape")
            if inner["g"] != group.group_id or inner["aud"] != identity.node_id:
                raise _Reject("audience")
            nowdt = groups.utcnow()
            tr = groups.trust(session, group, nowdt, own_node=identity.node_id)
            cert = tr.in_force(inner["n"])
            if cert is None or inner["n"] == identity.node_id:
                raise _Reject("not_member")
            current = tr.current_key(inner["n"])
            if current is None or current[0] != inner["k"]:
                raise _Reject("key")
            if not gc.verify_request(current[1], inner):
                raise _Reject("signature")
        except _Reject as exc:
            session.rollback()
            _auth_failed(st, str(exc))
            return not_found()
        error, result = None, None
        # The per-node throttle (step 11) is counted in its own transaction before this one writes, so the
        # SQLite write lock is never held across two sessions.
        throttled = not ratelimit.hit(st.maker, f"group-node:{inner['n']}", 600, 60)[0]
        if abs(now - inner["t"]) > gc.SKEW:
            error = "clock_skew"
        else:
            try:
                session.add(GroupNonce(node_id=inner["n"], nonce=inner["r"][:32], seen_at=nowdt))
                session.flush()
            except IntegrityError:
                session.rollback()
                error = "replay"
        if error is None and inner["proto"].split(".")[0] != groups.GROUP_PROTOCOL.split(".")[0]:
            error = "protocol"
        if error is None and inner["op"] not in CALL_OPS:
            error = "not_found"
        if error is None and throttled:
            error = "throttled"
        if error is None:
            result = _op(session, st, group, identity, inner, cert)
        server_time = gc.stamp(nowdt)
        outcome = {"result": result, "error": error, "server_time": server_time}
        parts = gc.response_parts(group.group_id, identity.node_id, identity.key_id, inner["n"], inner["r"],
                                  inner["op"], inner["params"], outcome)
        rs = {"key_id": identity.key_id, "sig": gc.sign_message(identity.private, gc.DOMAINS["response"], parts)}
        sealed = gc.seal_response(cert["body"]["member"]["box_public_key"], gc.unb64(inner["r"], 16), group.group_id,
                                  identity.node_id, inner["n"], {"v": 1, **outcome, "rs": rs})
        groups.purge_nonces(session)
        member = session.get(groups.GroupMember, (group.group_id, inner["n"]))
        if member is not None:
            member.last_seen_at, member.last_protocol = nowdt, inner["proto"][:16]
        session.commit()
        return JSONResponse(sealed)


def _op(session, st: AppState, group: OwnerGroup, identity: NodeIdentity, inner: dict, cert: dict) -> Any:
    op, params = inner["op"], inner["params"]
    if op == "hello":
        return {"group_protocol": groups.GROUP_PROTOCOL, "min_group_protocol": groups.MIN_GROUP_PROTOCOL,
                "ops": list(CALL_OPS), "node_id": identity.node_id}
    if op == "state":
        groups.refresh(session, group, identity.node_id, settings=st.ctx.settings)
        state = groups.export_state(session, group, identity)
        if params.get("have") and params["have"] == state["state_hash"]:
            return {"unchanged": True, "state_hash": state["state_hash"], "views": state["views"]}
        return state
    if op == "rotations":
        node = params.get("node_id")
        if node == identity.node_id:
            return key_rotation.own_history(session, identity.node_id)
        row = session.get(groups.GroupMember, (group.group_id, node)) if isinstance(node, str) else None
        return row.rotations if row is not None else []
    return None


@router.post("/api/v2/group/call")
async def group_call(request: Request):
    st: AppState = request.app.state.atlas
    if (limited := await run_in_threadpool(_throttled, st, f"group-call-ip:{_ip(request)}", 1200, 60)):
        return limited
    raw = await _body(request, CALL_CAP)
    if raw is None:
        return not_found()
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    return await run_in_threadpool(serve_call, st, body)


# ---- client ------------------------------------------------------------------------------------------------

class CallError(Exception):
    pass


def call(session, settings, identity: NodeIdentity, group: OwnerGroup, member: dict, op: str, params: dict,
         http: Any, now: float | None = None) -> Any:
    """One sealed, signed request to a member (its in-force certificate's `member`), verifying the signed answer."""
    nonce = secrets.token_bytes(16)
    inner = {"v": 1, "g": group.group_id, "n": identity.node_id, "aud": member["node_id"], "k": identity.key_id,
             "t": int(now if now is not None else time.time()), "r": gc.b64(nonce), "proto": groups.GROUP_PROTOCOL,
             "op": op, "params": params}
    sealed = gc.seal_request(member["box_public_key"], group.group_id, member["node_id"],
                             gc.sign_request(identity.private, inner))
    try:
        resp = http.post(member["url"].rstrip("/") + gc.CALL_PATH, content=canonical_json(sealed),
                         headers={"Content-Type": "application/json"}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        raise CallError(f"unreachable: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise CallError(f"HTTP {resp.status_code}")
    try:
        plain = gc.open_response(groups.box_keys(settings, group), nonce, group.group_id, member["node_id"],
                                 identity.node_id, resp.json())
    except (gc.GroupCryptoError, ValueError) as exc:
        raise CallError(f"the answer is not sealed to this node: {exc}") from None
    tr = groups.trust(session, group, own_node=identity.node_id)
    key = tr.current_key(member["node_id"])
    outcome = {"result": plain.get("result"), "error": plain.get("error"), "server_time": plain.get("server_time")}
    rs = plain.get("rs") if isinstance(plain.get("rs"), dict) else {}
    if key is not None and isinstance(rs.get("key_id"), str) and rs["key_id"] != key[0]:
        key = _learn_rotation(session, settings, identity, group, member, op, params, plain, http) or key
    if key is None or rs.get("key_id") != key[0] or not gc.verify_message(
            key[1], gc.DOMAINS["response"], gc.response_parts(group.group_id, member["node_id"], key[0],
                                                              identity.node_id, inner["r"], op, params, outcome),
            rs.get("sig", "")):
        raise CallError("the answer is not signed by that member's current key")
    if plain.get("error"):
        raise CallError(plain["error"])
    return plain.get("result")


def _learn_rotation(session, settings, identity: NodeIdentity, group: OwnerGroup, member: dict, op: str,
                    params: dict, plain: dict, http: Any) -> tuple[str, str] | None:
    """The member answered under a key this node does not know: it rotated (§1.7). Fetch its `rotations`
    history (or use this answer when it is that history), validate it from the root key in its certificate
    (fork and key-uniqueness checks included) and return the member's current key. The history authenticates
    itself, so it can be taken from an answer whose signature is not yet verifiable."""
    node = member["node_id"]
    if op == "rotations" and params.get("node_id") == node:
        history = plain.get("result")
    else:
        try:
            call(session, settings, identity, group, member, "rotations", {"node_id": node}, http)
        except CallError:
            return None
        history = None
    if history is not None:
        outcome = groups.ingest_rotations(session, group, node, history, groups.utcnow())
        if outcome not in ("stored", "known"):
            return None
        groups.rebuild(session, group, identity.node_id, settings=settings)
    return groups.trust(session, group, own_node=identity.node_id).current_key(node)


def pull(session, settings, identity: NodeIdentity, group: OwnerGroup, http: Any) -> dict[str, str]:
    """Pull `state` from every reachable in-force member and ingest it (§3.8). Returns node -> outcome."""
    outcomes: dict[str, str] = {}
    tr = groups.trust(session, group, own_node=identity.node_id)
    for node, cert in tr.members().items():
        if node == identity.node_id:
            continue
        row = session.get(groups.GroupMember, (group.group_id, node))
        try:
            state = call(session, settings, identity, group, cert["body"]["member"], "state",
                         {"have": group.state_hash or ""}, http)
            if state.get("unchanged"):
                for view in state.get("views") or []:
                    groups.ingest_view(session, group, view, groups.utcnow())
            else:
                groups.ingest_state(session, group, state, identity.node_id, settings=settings)
            outcomes[node] = "ok"
            if row is not None:
                row.last_error = None
        except (CallError, groups.GroupError) as exc:
            outcomes[node] = str(exc)[:300]
            if row is not None:
                row.last_error = str(exc)[:300]
    if group.status == "resyncing" and outcomes and all(v == "ok" for v in outcomes.values()):
        group.status = "active"
        groups.audit(session, None, "group_resync_done", group.group_id, {"members": len(outcomes)})
    groups.make_view(session, group, identity, force=True) if group.self_serial else None
    session.flush()
    return outcomes
