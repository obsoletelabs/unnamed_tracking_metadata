"""Owner-group HTTP endpoints (CONTRACT-C10 §2.3, §3.2): enrolment and the single sealed member call.

Every node mounts these routes, member or not. Anything that does not prove an open invite or membership gets
the same 404 body as an unknown route (`{"detail": "Not Found"}`), so the routes reveal nothing.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.concurrency import run_in_threadpool

from .. import assist, group_channel, group_enrol, group_schema, groups, key_rotation, ratelimit, wire
from .. import group_crypto as gc
from ..models import GroupNonce, OwnerGroup
from ..signing import NodeIdentity, canonical_json
from .deps import AppState

log = logging.getLogger("atlas.groups")

router = APIRouter(include_in_schema=False)
CALL_CAP = 256 * 1024
CALL_OPS = ("hello", "state", "rotations", "heads", "changes", "snapshot", "notify",  # A10 + A11 (§3.3)
            "push", "member_update",  # P7 (#122): push signed state to a member; send this node's member update
            "assist.find", "assist.fetch", "assist.blob",  # C14 §3.3, §3.6: ask the group for records and images
            "assist.lookup", "assist.status",  # C14 §4.2, §4.4: provider lookups on behalf, shared headroom
            "link.state",  # C14 §7.7 (H67): shared contribution state
            "group.writes")  # C14-4 (super group): user writes replicated to every member
#: Ops whose answer depends on this node's local policy; a member with the policy off answers `forbidden`.
ASSIST_OPS = ("assist.find", "assist.fetch", "assist.blob", "assist.lookup", "assist.status")
#: Client-side caps on a member's sealed answer (CONTRACT-C14 §3.10, fixes V2: `call()` used to read any answer
#: whole). Per op: a full `changes` page (500 signed entries) or the `state`/`snapshot` of a large group must
#: still fit, so the existing ops keep a generous cap until real sizes are measured; the C14 `assist.*` ops get
#: 1 MiB. A larger answer is `CallError("answer too large")`, and reading stops at the cap.
RESPONSE_CAP = 8 * 1024 * 1024
ASSIST_RESPONSE_CAP = 1024 * 1024
LEFT_OPS = ("hello", "state")  # a node that left keeps answering these, so members still learn its `left` CRL
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
        left = False
        if group is None:
            gone = groups.load(session)
            if gone is not None and gone.status == "left":
                group, left = gone, True
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
        retry_after = None
        fresh = abs(now - inner["t"]) <= gc.SKEW and \
            session.get(GroupNonce, (inner["n"], inner["r"][:32])) is None
        if not throttled and fresh and inner["op"] in ASSIST_OPS:
            # C14 §3.10: the per-asker assist limits, counted before this transaction writes (a refused take
            # counts nothing); `throttled` carries `retry_after` so the asker backs off this helper only. Only a
            # fresh request (in the skew window, nonce not seen; read without a write lock) takes from the asker's
            # budget, so an observer replaying a captured sealed request cannot spend it.
            ok, retry = assist.limit_call(st.maker, inner["n"], inner["op"])
            if not ok:
                throttled, retry_after = True, retry
        if abs(now - inner["t"]) > gc.SKEW:
            error = "clock_skew"
        else:
            try:
                session.add(GroupNonce(node_id=inner["n"], nonce=inner["r"][:32], seen_at=nowdt))
                session.flush()
            except IntegrityError:
                session.rollback()
                error = "replay"
        if error is None and group_schema.proto_use(inner["proto"]) is None:
            error = "wire_unsupported"  # design §5.2: a v0 member ("1.0") or a wire this build does not speak
        if error is None and (inner["op"] not in CALL_OPS or (left and inner["op"] not in LEFT_OPS)):
            error = "not_found"
        if error is None and throttled:
            error = "throttled"
            if retry_after is not None:
                result = {"retry_after": max(1, int(retry_after))}
        if error is None:
            try:
                result = _op(session, st, group, identity, inner, cert)
            except _OpError as exc:
                error = str(exc)
                result = exc.result
        server_time = gc.stamp(nowdt)
        outcome = {"result": result, "error": error, "server_time": server_time}
        spoken = group_schema.proto_use(inner["proto"])
        if spoken is not None:
            outcome["wire"] = spoken  # §4.10 rule 4 / T-W27: the signed outcome echoes the wire it was served at
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
        return {**group_channel.hello_result(identity), "ops": list(CALL_OPS), "node_id": identity.node_id}
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
    tr = groups.trust(session, group, own_node=identity.node_id)
    if op == "heads":
        return group_channel.serve_heads(session, group, identity.node_id, inner["n"], tr)
    if op == "changes":
        return _or_error(group_channel.serve_changes(session, group, identity.node_id, inner["n"], tr, params))
    if op == "snapshot":
        return _or_error(group_channel.serve_snapshot(session, group, identity.node_id, inner["n"], params))
    if op == "notify":
        group_channel.WAKE.set()  # a hint only: the next round pulls (§3.8)
        return {"ok": True}
    if op == "push":
        # The same objects a `state` pull would fetch from this member, sent by it (a leave, a re-issued
        # certificate). `ingest_state` verifies every object; nothing is trusted because of who pushed it, but a
        # succession that first arrives here is attributed to the pusher, whose view never confirms it (R1-5).
        try:
            counts = groups.ingest_state(session, group, params.get("state"), identity.node_id,
                                         settings=st.ctx.settings, served_by=inner["n"])
        except groups.GroupError as exc:
            raise _OpError(str(exc)[:200]) from None
        return {"ok": True, "counts": counts}
    if op == "member_update":
        try:
            return {"status": groups.hold_member_update(session, st.ctx.settings, group, identity.node_id,
                                                        params.get("update"), inner["n"])}
        except groups.GroupError as exc:
            raise _OpError(str(exc)[:200]) from None
    if op in ASSIST_OPS:
        return _assist_op(session, st, group, identity, inner, tr)
    if op == "link.state":
        from .. import contrib_state

        try:
            return contrib_state.serve(session, st.ctx.settings, identity, params)
        except contrib_state.StateError as exc:
            raise _OpError(exc.code) from None
    if op == "group.writes":
        from .. import super_group

        try:
            return super_group.serve(session, st.ctx.settings, identity, tr, inner["n"], params)
        except super_group.WriteError as exc:
            raise _OpError(exc.code) from None
    return None


def _assist_op(session, st: AppState, group: OwnerGroup, identity: NodeIdentity, inner: dict, tr: Any) -> Any:
    """C14 Part A, answered only from what this node holds; the only writes are rate counters (and, for a lookup,
    its provider cache row and idempotency row, §4.3)."""
    from .. import assist_lookup

    op, params, asker = inner["op"], inner["params"], inner["n"]
    settings = st.ctx.settings
    try:
        if op == "assist.find":
            return assist.serve_find(session, settings, params)
        if op == "assist.fetch":
            return assist.serve_fetch(session, settings, identity, group, asker, inner["r"], params)
        if op == "assist.blob":
            return assist.serve_blob(session, settings, asker, params)
        if op == "assist.status":
            return assist_lookup.serve_status(session, settings, group, st.providers)
        return assist_lookup.serve_lookup(session, st, group, identity, asker, inner["r"], params)
    except assist.Throttled as exc:
        raise _OpError("throttled", {"retry_after": exc.retry_after}) from None
    except assist.AssistError as exc:
        raise _OpError(exc.code, getattr(exc, "result", None)) from None


class _OpError(Exception):
    def __init__(self, message: str, result: Any = None):
        super().__init__(message)
        self.result = result


def _or_error(result: Any) -> Any:
    if isinstance(result, str):
        raise _OpError(result)
    return result


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
    """A failed member call. `result` is the signed `result` that came with a signed `error` (for `throttled`,
    `{"retry_after": seconds}`), None otherwise."""

    def __init__(self, message: str, result: Any = None):
        super().__init__(message)
        self.result = result


def response_cap(op: str) -> int:
    """The most bytes `call()` reads of the answer to `op`."""
    return ASSIST_RESPONSE_CAP if op.startswith("assist.") else RESPONSE_CAP


def _post_capped(http: Any, url: str, content: bytes, cap: int) -> tuple[int, bytes]:
    """POST and read the answer by streaming, refusing one longer than `cap` bytes (declared or actual)."""
    headers = {"Content-Type": "application/json"}
    stream = getattr(http, "stream", None)
    if stream is None:  # a transport without streaming (a test double): read, then check
        resp = http.post(url, content=content, headers=headers, timeout=15)
        data = resp.content
        if len(data) > cap:
            raise CallError("answer too large")
        return resp.status_code, data
    with stream("POST", url, content=content, headers=headers, timeout=15) as resp:
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise CallError("answer too large")
        data = bytearray()
        for chunk in resp.iter_bytes():
            data.extend(chunk)
            if len(data) > cap:
                raise CallError("answer too large")
        return resp.status_code, bytes(data)


def call(session, settings, identity: NodeIdentity, group: OwnerGroup, member: dict, op: str, params: dict,
         http: Any, now: float | None = None, nonce: bytes | None = None) -> Any:
    """One sealed, signed request to a member (its in-force certificate's `member`), verifying the signed answer.
    `nonce` (16 bytes) lets a caller bind what the answer signs to the request (an `assist.fetch` bundle names it)."""
    from .. import group_channel

    nonce = nonce if nonce is not None and len(nonce) == 16 else secrets.token_bytes(16)
    # Design §5.2: speak the wire negotiated from the member's cached `hello` (its highest common wire with this
    # build); before any hello, this build's highest.
    cached = group_channel._HELLO.get(member["node_id"])
    use = (group_schema.negotiate(cached[1]) if cached else None) or wire.WIRE_MAX
    inner = {"v": 1, "g": group.group_id, "n": identity.node_id, "aud": member["node_id"], "k": identity.key_id,
             "t": int(now if now is not None else time.time()), "r": gc.b64(nonce),
             "proto": group_schema.proto_of(use), "op": op, "params": params}
    sealed = gc.seal_request(member["box_public_key"], group.group_id, member["node_id"],
                             gc.sign_request(identity.private, inner))
    try:
        status, answer = _post_capped(http, member["url"].rstrip("/") + gc.CALL_PATH, canonical_json(sealed),
                                      response_cap(op))
    except CallError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CallError(f"unreachable: {type(exc).__name__}") from None
    if status != 200:
        raise CallError(f"HTTP {status}")
    try:
        plain = gc.open_response(groups.box_keys(settings, group), nonce, group.group_id, member["node_id"],
                                 identity.node_id, json.loads(answer))
    except (gc.GroupCryptoError, ValueError) as exc:
        raise CallError(f"the answer is not sealed to this node: {exc}") from None
    tr = groups.trust(session, group, own_node=identity.node_id)
    key = tr.current_key(member["node_id"])
    outcome = {"result": plain.get("result"), "error": plain.get("error"), "server_time": plain.get("server_time")}
    if "wire" in plain:
        outcome["wire"] = plain["wire"]
    rs = plain.get("rs") if isinstance(plain.get("rs"), dict) else {}
    if key is not None and isinstance(rs.get("key_id"), str) and rs["key_id"] != key[0]:
        key = _learn_rotation(session, settings, identity, group, member, op, params, plain, http) or key
    if key is None or rs.get("key_id") != key[0] or not gc.verify_message(
            key[1], gc.DOMAINS["response"], gc.response_parts(group.group_id, member["node_id"], key[0],
                                                              identity.node_id, inner["r"], op, params, outcome),
            rs.get("sig", "")):
        raise CallError("the answer is not signed by that member's current key")
    if plain.get("error") == "wire_unsupported":
        group_channel._HELLO.pop(member["node_id"], None)  # §4.10 rule 4: ask its hello again next round
    if plain.get("error"):
        raise CallError(plain["error"], plain.get("result"))
    if "wire" in plain and plain["wire"] != use:
        raise CallError(f"the member answered at wire {plain['wire']} where wire {use} was asked")
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
    from .. import group_journal

    outcomes: dict[str, str] = {}
    try:  # R1-3: a restore's journal replay (or objects only the journal holds) come before anything else
        group_journal.replay_if_needed(session, group, identity.node_id, settings)
    except Exception as exc:  # noqa: BLE001 - a failed replay leaves REPLAY_DUE set, so the resync cannot finish
        log.warning("group journal replay failed: %s", exc)
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
                groups.ingest_state(session, group, state, identity.node_id, settings=settings, served_by=node)
                groups.check_view_backed(session, group, node)  # R1-2: a view its own state does not back
            outcomes[node] = "ok"
            if row is not None:
                row.last_error = None
        except (CallError, groups.GroupError) as exc:
            outcomes[node] = str(exc)[:300]
            if row is not None:
                row.last_error = str(exc)[:300]
    _finish_resync(session, settings, identity, group, outcomes)
    groups.make_view(session, group, identity, force=True) if group.self_serial else None
    session.flush()
    return outcomes


def _finish_resync(session, settings, identity: NodeIdentity, group: OwnerGroup, outcomes: dict[str, str]) -> None:
    """R1-3: `resyncing` ends only after the journal replay, and only when every in-force member answered with at
    least one answering member that is still in force after the union (a revoked-only peer never completes a
    resync). With no other in-force member left after replay and union, the journal alone completes it. A skipped
    trust epoch ends on a full pull, except in a two-member group whose journal holds nothing for the group (then
    only `resync --confirm-root`)."""
    from .. import group_journal

    if session.get(groups.NodeState, group_journal.REPLAY_DUE) is not None:
        return
    others = set(groups.trust(session, group, own_node=identity.node_id).members()) - {identity.node_id}
    full = bool(outcomes) and all(v == "ok" for v in outcomes.values())
    if group.status == "resyncing":
        if not others or (full and any(n in others for n in outcomes)):
            group.status = "active"
            groups.audit(session, None, "group_resync_done", group.group_id,
                         {"members": len(outcomes), "journal_only": not others})
        return
    epoch = session.get(groups.NodeState, groups.TRUST_EPOCH)
    if epoch is None or group.status != "active" or not full:
        return
    if len(others) == 1 and not group_journal.read(settings, group.group_id):
        return  # nothing proves the one remaining member was not the one revoked after the backup
    session.delete(epoch)
    groups.audit(session, None, "group_trust_epoch_closed", group.group_id, {"members": len(outcomes)})


def push_state(session, settings, identity: NodeIdentity, group: OwnerGroup, http: Any,
               only: set[str] | None = None) -> dict[str, str]:
    """Send this node's signed group state to every in-force member (or `only` those), so a leave or a re-issued
    certificate reaches them now instead of on their next pull. Returns node -> outcome; failures are not fatal
    (members also pull, and a node that left keeps answering `state`)."""
    state = groups.export_state(session, group, identity)
    outcomes: dict[str, str] = {}
    for node, cert in groups.trust(session, group, own_node=identity.node_id).members().items():
        if node == identity.node_id or (only is not None and node not in only):
            continue
        try:
            call(session, settings, identity, group, cert["body"]["member"], "push", {"state": state}, http)
            outcomes[node] = "ok"
        except CallError as exc:
            outcomes[node] = str(exc)[:300]
    return outcomes


def send_member_update(session, settings, identity: NodeIdentity, group: OwnerGroup, update: dict,
                       http: Any) -> dict[str, str]:
    """Offer this node's signed member update to every member; only a root holder keeps it ('held' or 'known'),
    the others answer `not_root`. Returns node -> outcome."""
    outcomes: dict[str, str] = {}
    for node, cert in groups.trust(session, group, own_node=identity.node_id).members().items():
        if node == identity.node_id:
            continue
        try:
            result = call(session, settings, identity, group, cert["body"]["member"], "member_update",
                          {"update": update}, http)
            outcomes[node] = str((result or {}).get("status") or "ok")
        except CallError as exc:
            outcomes[node] = str(exc)[:300]
    return outcomes
