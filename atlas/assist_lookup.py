"""Provider lookups on behalf of another member, and shared headroom (CONTRACT-C14 §4, Review #1 C14-3/4/9/15).

Helper (`serve_lookup`): only a closed set of shipped adapters (`SHIPPED`), each also configured here, each an
instance of its built-in class (so a replaced or plugin provider is never driven by a member), and only the
operations of `CALLS`. The adapter builds the URL from its fixed `https` base; asker input is an id that passes
the adapter pattern, or a query of at most 200 characters passed as a parameter. The take comes from the assist
share of this node's per-credential budget (`provider_budget.take_assist`), atomically and before the provider
call, which runs after the call's nonce was committed (no write lock held across it). Results go through a closed
DTO (`_dto`): unknown keys are dropped, and every URL must be `https`, without userinfo and without a key-like query
name, or it is dropped. A typed attestation (`group.assist`, `kind = "lookup"`) signs the exact request and the
hash of the whole result. Each `(asker, op_id)` runs once (`assist_lookup_op`): a retry of a completed op gets the
stored result signed for its new nonce without a second take; a claimed one answers `throttled`; a reused op id
with another request is refused. Nothing is ingested on the helper; its only writes are its provider cache row,
counters and the op row.

Asker (`lookup_via_group`): picks the member with the most headroom whose credential is not this node's own
(`cred_group_fp`), verifies the attestation (signer = that member's current key, bound to this node, this nonce,
the exact request and the received result) and ingests the results as `<provider>` observations scoped
`group:<helper>` on the record it names, never into the public feed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import group_crypto as gc
from .assist import ID_RE, AssistError, Throttled, _safe_url, canon
from .ids import ulid
from .models import AssistLookupOp, Canonical, GroupAssistItem, NodeState, now

log = logging.getLogger("atlas.assist")

#: The closed provider set (§4.2, Review #1 C14-4): the adapters this release ships.
SHIPPED = ("steam", "rawg", "igdb", "tgdb", "openlibrary")
CALLS = ("fetch", "search", "achievements", "achievement_stats")
ERRORS = ("not_configured", "no_headroom", "provider_unavailable", "invalid_id", "forbidden", "throttled")
LOOKUP_PER_HOUR = 120
LOOKUP_PER_DAY = 1000
ACCOUNT_PER_HOUR = 60
OP_TTL = timedelta(minutes=15)
CLAIM_RELEASE = timedelta(seconds=60)
STATUS = "assist_status:"
STATUS_EVERY = 300
STATUS_FRESH = 600
#: §4.3 step 7: the public fields of ProviderResult that may travel, and of an achievement.
RESULT_FIELDS = ("external_id", "title", "platform", "release_date", "release", "provider_ids", "facts", "artwork")
FACT_URL_KEYS = ("url", "website", "homepage", "store_url")
ACH_FIELDS = ("api_name", "name", "description", "icon", "icon_gray", "hidden", "percent", "points")
ART_FIELDS = ("kind", "url", "width", "height", "licence")


def _built_in(p: Any, name: str) -> bool:
    from .providers.igdb import Igdb
    from .providers.openlibrary import OpenLibrary
    from .providers.rawg import Rawg
    from .providers.steam import Steam
    from .providers.tgdb import TheGamesDb

    classes = {"steam": Steam, "rawg": Rawg, "igdb": Igdb, "tgdb": TheGamesDb, "openlibrary": OpenLibrary}
    return name in classes and type(p) is classes[name]


def adapter(fetcher: Any, provider: Any) -> Any:
    """The trusted adapter for `provider`, or None: shipped, configured here, and the built-in class itself."""
    if provider not in SHIPPED:
        return None
    p = (getattr(fetcher, "providers", None) or {}).get(provider)
    return p if p is not None and _built_in(p, provider) else None


def mode(settings: Any) -> str:
    return str(getattr(settings, "group_assist_lookups", "off") or "off")


def reserve(settings: Any) -> int:
    return int(getattr(settings, "group_assist_lookup_reserve", 50))


# ---- the request, the result and the attestation ------------------------------------------------------------------

def request_of(params: dict) -> dict:
    """The canonical attested request: every key present, null when unused (§4.2 C14-15)."""
    return {k: params.get(k) for k in ("provider", "call", "external_id", "query", "id", "on_behalf", "op_id")}


def validate(params: Any) -> dict:
    from .providers.registry import _SAFE_ID

    if not isinstance(params, dict):
        raise AssistError("invalid_id")
    req = request_of(params)
    if req["provider"] not in SHIPPED or req["call"] not in CALLS:
        raise AssistError("not_configured")
    if not isinstance(req["op_id"], str) or not 10 <= len(req["op_id"]) <= 40:
        raise AssistError("invalid_id")
    if req["call"] == "search":
        if not isinstance(req["query"], str) or not 0 < len(req["query"].strip()) <= 200 or req["external_id"]:
            raise AssistError("invalid_id")
    elif not isinstance(req["external_id"], str) or not _SAFE_ID.match(req["external_id"]) or req["query"]:
        raise AssistError("invalid_id")
    if req["id"] is not None and (not isinstance(req["id"], str) or not ID_RE.match(req["id"])):
        raise AssistError("invalid_id")
    if req["on_behalf"] is not None and (not isinstance(req["on_behalf"], str) or len(req["on_behalf"]) > 40):
        raise AssistError("invalid_id")
    return req


def _clean_url(url: Any) -> str | None:
    return url if _safe_url(url) else None


def _art(items: Any) -> list[dict]:
    out = []
    for a in items if isinstance(items, list) else []:
        if isinstance(a, dict) and _clean_url(a.get("url")):
            out.append({k: a[k] for k in ART_FIELDS if k in a})
    return out[:64]


def _dto(result: Any) -> dict | None:
    """One ProviderResult reduced to the closed public DTO, every URL checked (dropped, never redacted)."""
    raw = result.__dict__ if hasattr(result, "__dict__") else result
    if not isinstance(raw, dict) or not isinstance(raw.get("external_id"), str) or not isinstance(raw.get("title"),
                                                                                                   str):
        return None
    out: dict[str, Any] = {k: raw[k] for k in RESULT_FIELDS if raw.get(k) not in (None, "", [], {})}
    facts = dict(out.get("facts") or {})
    for key in list(facts):
        if key in FACT_URL_KEYS and not _clean_url(facts[key]):
            facts.pop(key)
    if facts:
        out["facts"] = facts
    else:
        out.pop("facts", None)
    if "artwork" in out:
        out["artwork"] = _art(out["artwork"])
    return canon(out)


def _ach(items: Any) -> list[dict] | None:
    if not isinstance(items, list):
        return None
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        item = {k: a[k] for k in ACH_FIELDS if k in a}
        for k in ("icon", "icon_gray"):
            if k in item and not _clean_url(item[k]):
                item.pop(k)
        out.append(item)
    return canon(out[:2000])


def result_hash(answer: dict) -> str:
    from .signing import canonical_json

    whole = {k: answer.get(k) for k in ("results", "achievements", "cache", "fetched_at")}
    return hashlib.sha256(canonical_json(whole)).hexdigest()


def _attest(group: Any, identity: Any, asker: str, nonce: str, req: dict, answer: dict) -> dict:
    body = {"kind": "lookup", "requester": asker, "request_nonce": nonce, "request": canon(req),
            "result_hash": result_hash(answer), "cache": answer["cache"], "fetched_at": answer["fetched_at"]}
    return gc.make_object("group.assist", group.group_id, f"assist:{identity.node_id}/{ulid()}", body,
                          {"kind": "node", "id": identity.node_id, "key_id": identity.key_id}, identity.private)


# ---- helper: assist.lookup ------------------------------------------------------------------------------------------

def _limits(st: Any, asker: str, req: dict) -> None:
    from . import ratelimit

    windows = [(f"assist-lookup:{asker}:{req['provider']}", LOOKUP_PER_HOUR, 3600),
               (f"assist-lookup:{asker}:{req['provider']}", LOOKUP_PER_DAY, 86400)]
    if req["on_behalf"]:
        windows.append((f"assist-lookup-acct:{asker}:{req['on_behalf']}", ACCOUNT_PER_HOUR, 3600))
    ok, retry = ratelimit.take(st.maker, windows)
    if not ok:
        raise Throttled(retry)


def _request_hash(req: dict) -> str:
    from .signing import canonical_json

    return hashlib.sha256(canonical_json(canon(req))).hexdigest()


def _claim(st: Any, asker: str, req: dict) -> dict | None:
    """Insert the op row (claimed) in its own committed transaction. Returns the stored answer of a completed op
    (re-signed by the caller for the new nonce), raises for a claimed or mismatching one, None when claimed now."""
    rh = _request_hash(req)
    with st.maker() as s:
        s.execute(delete(AssistLookupOp).where(AssistLookupOp.expires_at < now()))
        row = s.get(AssistLookupOp, (asker, req["op_id"]))
        if row is not None:
            if row.request_hash != rh:
                s.commit()
                raise AssistError("invalid_id")
            if row.state == "completed" and isinstance(row.result, dict):
                s.commit()
                return row.result
            claimed = row.claimed_at.replace(tzinfo=now().tzinfo) if row.claimed_at.tzinfo is None \
                else row.claimed_at
            if now() - claimed < CLAIM_RELEASE:
                s.commit()
                raise Throttled(5)
            row.claimed_at = now()  # a crashed claim is released after 60 s (its take is not refunded)
            s.commit()
            return None
        s.add(AssistLookupOp(asker=asker, op_id=req["op_id"], request_hash=rh, state="claimed", claimed_at=now(),
                             expires_at=now() + OP_TTL))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise Throttled(5) from None
    return None


def _complete(st: Any, asker: str, op_id: str, answer: dict | None) -> None:
    with st.maker() as s:
        row = s.get(AssistLookupOp, (asker, op_id))
        if row is not None:
            if answer is None:
                s.delete(row)
            else:
                row.state, row.result = "completed", answer
        s.commit()


def serve_lookup(session: Session, st: Any, group: Any, identity: Any, asker: str, nonce: str, params: Any) -> dict:
    """`assist.lookup` (§4.3). `session` is the call's transaction: the nonce is committed here first, so the
    provider request runs without the write lock."""
    from . import provider_budget
    from .instruments import group_assist_lookups
    from .providers.base import ProviderError

    settings = st.ctx.settings
    if mode(settings) not in ("answer", "answer+ask"):
        raise AssistError("forbidden")
    req = validate(params)
    p = adapter(st.providers, req["provider"])
    if p is None:
        raise AssistError("not_configured")
    session.commit()  # the nonce (and nothing else) is durable before any provider work (§4.3 step 5)
    _limits(st, asker, req)
    stored = _claim(st, asker, req)
    if stored is None:
        ok, retry = provider_budget.take_assist(st.maker, settings, p, reserve(settings))
        if not ok:
            _complete(st, asker, req["op_id"], None)
            group_assist_lookups.inc(role="answered", outcome="throttled")
            exc = AssistError("no_headroom")
            exc.result = {"retry_after": max(1, int(retry))}  # type: ignore[attr-defined]
            raise exc
        token = provider_budget.TAKEN.set(True)
        try:
            with st.maker() as s:
                answer = _run(s, st.providers, p, req)
                s.commit()
        except ProviderError:
            _complete(st, asker, req["op_id"], None)
            group_assist_lookups.inc(role="answered", outcome="error")
            raise AssistError("provider_unavailable") from None
        finally:
            provider_budget.TAKEN.reset(token)
        _complete(st, asker, req["op_id"], answer)
        _audit(st, asker, req["provider"])
    else:
        answer = stored
    group_assist_lookups.inc(role="answered", outcome="ok" if answer.get("results") or answer.get("achievements")
                             else "not_found")
    with st.maker() as s:
        headroom = {req["provider"]: provider_budget.headroom(s, settings, p, reserve(settings))}
    return {**answer, "provider": req["provider"], "call": req["call"],
            "attestation": _attest(group, identity, asker, nonce, req, answer), "headroom": canon(
                {k: int(v * 100) for k, v in headroom.items()})}


def _run(session: Session, fetcher: Any, p: Any, req: dict) -> dict:
    """The provider call through this node's own Fetcher (cache first). The per-credential take was made by the
    caller (`provider_budget.TAKEN`), so the Fetcher does not count the request again; its per-process bucket stays
    as a local brake."""
    call = req["call"]
    achievements = None
    results: list[dict] = []
    if call == "fetch":
        results = [d for d in (_dto(r) for r in fetcher.lookup(session, p.name, external_id=req["external_id"])) if d]
    elif call == "search":
        results = [d for d in (_dto(r) for r in fetcher.lookup(session, p.name, query=req["query"])) if d][:20]
    elif call == "achievements":
        achievements = _ach(fetcher.achievements(session, p.name, req["external_id"]))
    else:
        stats = fetcher.achievement_stats(session, p.name, req["external_id"])
        achievements = [{"api_name": str(k)[:128], "percent": int(round(float(v) * 100))}
                        for k, v in (stats or {}).items()][:2000]
    return {"results": results, "achievements": achievements, "cache": "miss", "fetched_at": gc.stamp()}


def _audit(st: Any, asker: str, provider: str) -> None:
    """§4.3 step 4: one aggregated row per asker per hour."""
    from . import groups, ratelimit

    hour = int(time.time() // 3600)
    ok, _ = ratelimit.take(st.maker, [(f"assist-lookup-audit:{asker}:{hour}", 1, 3600)])
    if ok:
        with st.maker() as s:
            groups.audit(s, None, "group_assist_lookup", asker, {"provider": provider, "hour": hour})
            s.commit()


# ---- helper: assist.status ------------------------------------------------------------------------------------------

def serve_status(session: Session, settings: Any, group: Any, fetcher: Any = None) -> dict:
    from . import assist, provider_budget
    from .providers.registry import build_registry

    providers = getattr(fetcher, "providers", None) or build_registry(settings)
    out: dict[str, Any] = {}
    for name in SHIPPED:
        p = providers.get(name)
        if p is None or not _built_in(p, name):
            out[name] = {"configured": False}
            continue
        item: dict[str, Any] = {"configured": True,
                                "headroom": int(provider_budget.headroom(session, settings, p, reserve(settings))
                                                * 100)}
        fp = provider_budget.cred_group_fp(group.group_id, settings, name)
        if fp:
            item["cred_group_fp"] = fp
        out[name] = item
    return {"as_of": gc.stamp(), "answers": {"records": assist.answer_mode(settings),
                                             "media": "media" in assist.shares(settings),
                                             "lookups": mode(settings) in ("answer", "answer+ask")},
            "providers": out}


# ---- asker -------------------------------------------------------------------------------------------------------

def poll_status(session: Session, settings: Any, identity: Any, group: Any, tr: Any, http: Any, call: Any) -> int:
    """Every group round, at most every 5 minutes per member: `assist.status`, kept in `assist_status:<node>`;
    then the shared-credential split (§4.3 step 6(c))."""
    from . import assist, provider_budget

    asked = 0
    for node, cert in tr.members().items():
        if node == identity.node_id or not assist.supports(session, node, "assist.status"):
            continue
        prior = session.get(NodeState, STATUS + node)
        if prior is not None:
            try:
                if time.time() - float(json.loads(prior.value).get("_at", 0)) < STATUS_EVERY:
                    continue
            except (ValueError, AttributeError):
                pass
        try:
            got = call(session, settings, identity, group, cert["body"]["member"], "assist.status", {}, http)
        except Exception:  # noqa: BLE001 - a member that cannot answer simply is not routed to
            continue
        if isinstance(got, dict):
            assist._put(session, STATUS + node, {**got, "_at": time.time()})
            asked += 1
    members = set(tr.members())
    for name in SHIPPED:
        own = provider_budget.cred_group_fp(group.group_id, settings, name)
        n = 1
        if own:
            for node in members - {identity.node_id}:
                if (_status(session, node, fresh=False).get("providers") or {}).get(name, {}).get(
                        "cred_group_fp") == own:
                    n += 1
        provider_budget.set_share(session, name, n)
    session.flush()
    return asked


def _status(session: Session, node: str, fresh: bool = True) -> dict:
    row = session.get(NodeState, STATUS + node)
    if row is None:
        return {}
    try:
        value = json.loads(row.value)
    except ValueError:
        return {}
    if fresh and time.time() - float(value.get("_at", 0)) > STATUS_FRESH:
        return {}
    return value if isinstance(value, dict) else {}


def pick_helper(session: Session, settings: Any, identity: Any, group: Any, tr: Any, provider: str) -> list[str]:
    """Members by headroom for `provider`, never one that spends this node's own credential."""
    from . import assist, provider_budget

    own = provider_budget.cred_group_fp(group.group_id, settings, provider)
    ranked = []
    for node in tr.members():
        if node == identity.node_id or not assist.supports(session, node, "assist.lookup") \
                or assist._backed_off(session, node):
            continue
        entry = (_status(session, node).get("providers") or {}).get(provider) or {}
        if not entry.get("configured") or (own and entry.get("cred_group_fp") == own):
            continue
        head = entry.get("headroom") if type(entry.get("headroom")) is int else 0
        if head > 0:
            ranked.append((-head, node))
    return [n for _h, n in sorted(ranked)]


def verify_attestation(session: Session, identity: Any, tr: Any, helper: str, nonce: str, req: dict,
                       got: dict) -> dict:
    att = got.get("attestation")
    key = tr.current_key(helper)
    if not isinstance(att, dict) or att.get("t") != "group.assist" or key is None:
        raise AssistError("bad_signature")
    signer = att.get("signer") or {}
    if signer.get("kind") != "node" or signer.get("id") != helper or signer.get("key_id") != key[0] \
            or not gc.verify_object(att, key[1]):
        raise AssistError("bad_signature")
    body = att["body"]
    if body.get("kind") != "lookup" or body.get("requester") != identity.node_id or body.get("request_nonce") != nonce:
        raise AssistError("wrong_requester")
    if body.get("request") != canon(req) or body.get("result_hash") != result_hash(got) \
            or body.get("cache") != got.get("cache") or body.get("fetched_at") != got.get("fetched_at"):
        raise AssistError("hash_mismatch")
    return body


def lookup_via_group(maker: Any, ctx: Any, http: Any, provider: str, call_name: str, *, external_id: str | None = None,
                     query: str | None = None, cid: str | None = None, on_behalf: str | None = None,
                     call: Any = None) -> dict:
    """Ask the member with the most headroom for one provider lookup; verify; ingest the results as group-scope
    observations on `cid` (a fetch). Returns {"helper", "results", "achievements", "ingested"}."""
    from . import assist, groups
    from .api import group_routes
    from .instruments import group_assist_lookups

    call = call or group_routes.call
    settings, identity = ctx.settings, ctx.identity
    if mode(settings) != "answer+ask":
        raise AssistError("forbidden")
    req = validate({"provider": provider, "call": call_name, "external_id": external_id, "query": query, "id": cid,
                    "on_behalf": on_behalf, "op_id": ulid()})
    with maker() as s:
        group = groups.active(s)
        if group is None:
            raise AssistError("forbidden")
        tr = groups.trust(s, group, own_node=identity.node_id)
        for helper in pick_helper(s, settings, identity, group, tr, provider):
            member = tr.members()[helper]["body"]["member"]
            nonce = secrets.token_bytes(16)
            try:
                got = call(s, settings, identity, group, member, "assist.lookup", req, http, nonce=nonce)
            except Exception as exc:  # noqa: BLE001
                assist._note_call_error(s, helper, exc)
                group_assist_lookups.inc(role="asked", outcome="throttled" if str(exc) in ("throttled", "no_headroom")
                                         else "error")
                continue
            if not isinstance(got, dict):
                continue
            try:
                verify_attestation(s, identity, tr, helper, gc.b64(nonce), req, got)
            except AssistError as exc:
                assist.suspect(s, helper, exc.code)
                s.commit()
                group_assist_lookups.inc(role="asked", outcome="refused")
                continue
            results = [r for r in (_dto(r) for r in got.get("results") or []) if r]
            ingested = _ingest(s, ctx, helper, provider, results, cid, got) if call_name == "fetch" and cid else 0
            s.commit()
            group_assist_lookups.inc(role="asked", outcome="ok" if results or got.get("achievements")
                                     else "not_found")
            return {"helper": helper, "results": results, "achievements": got.get("achievements"),
                    "ingested": ingested}
        s.commit()
    raise AssistError("no_headroom")


def _ingest(session: Session, ctx: Any, helper: str, provider: str, results: list[dict], cid: str, got: dict) -> int:
    """§4.5: observations of `<provider>` scoped `group:<helper>` on `cid`, through the ingest step with
    `force_canonical`; the attestation is kept as a `lookup` item."""
    from . import service
    from .contract import ObservationBatchV1
    from .providers.base import ProviderResult

    if session.get(Canonical, cid) is None or not results:
        return 0
    service.write_lock(session)
    items = []
    for r in results:
        pr = ProviderResult(**{k: v for k, v in r.items() if k in ProviderResult.__dataclass_fields__})
        items.append(pr.to_item())
    batch = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": provider, "source_scope": "group:" + helper},
        "observations": items})
    out = service.ingest(session, ctx, batch, system=True, force_canonical=cid)
    att = got["attestation"]
    session.add(GroupAssistItem(id=ulid(), helper=helper, cid=cid, kind="lookup", body={"provider": provider},
                                body_hash=att["body_hash"], envelope=att, received_at=now(), applied_at=now()))
    session.flush()
    return len(out)


def purge(session: Session) -> int:
    return session.execute(delete(AssistLookupOp).where(AssistLookupOp.expires_at < now())).rowcount or 0

