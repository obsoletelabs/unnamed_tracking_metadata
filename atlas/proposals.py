"""Community proposals, review, reputation and revert."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import accounts, custom, facets, mail, social
from .contract import ObservationBatchV1
from .models import Achievement, Artwork, Canonical, Crosswalk, Observation, Proposal, ReputationEvent, User, now
from .resolve import follow
from .service import AtlasError, Ctx, ingest, write_lock

SOURCE = "community"
SCOPE = "atlas"
KINDS = ("edit", "new_game", "artwork", "achievements", "link_ids")
REWARD = {"edit": 5, "new_game": 10, "artwork": 3, "achievements": 5, "link_ids": 2}
REJECT_PENALTY = -2
REVERT_PENALTY = -10
SCALAR = {"title": 500, "description": 20000, "developer": 200, "publisher": 200, "series": 200, "age_rating": 50,
          "website": 500}
LISTS = ("genres", "themes", "tags", "features", "platforms", "languages", "aliases")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


def adjust_reputation(session: Session, user: User, delta: int, reason: str, proposal_id: str | None = None) -> None:
    if delta:
        user.reputation += delta
        session.add(ReputationEvent(user_id=user.id, delta=delta, reason=reason, proposal_id=proposal_id))


# ---- validation ---------------------------------------------------------------------------------------

def clean_edit(payload: dict[str, Any]) -> dict[str, Any]:
    """Only fields with a value; an empty value cannot clear data (omission never deletes)."""
    unknown = set(payload) - set(SCALAR) - set(LISTS) - {"release_date"}
    if unknown:
        raise AtlasError(f"cannot edit: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for key, limit in SCALAR.items():
        if payload.get(key) not in (None, ""):
            value = payload[key]
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise AtlasError(f"{key} must be text up to {limit} characters")
            if key == "website" and (not value.startswith("https://") or any(c.isspace() for c in value)):
                raise AtlasError("website must be an https:// address")
            out[key] = value.strip()
    if payload.get("release_date") not in (None, ""):
        value = payload["release_date"]
        try:
            if not isinstance(value, str) or not _STAMP.match(value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as exc:
            raise AtlasError("release_date must be YYYY-MM-DD") from exc
        out["release_date"] = value
    for key in LISTS:
        value = payload.get(key)
        if value:
            valid = isinstance(value, list) and len(value) <= 50 and all(
                isinstance(v, str) and 0 < len(v.strip()) <= 128 for v in value)
            if not valid:
                raise AtlasError(f"{key} must be a list of up to 50 short strings")
            out[key] = [v.strip() for v in value]
    if not out:
        raise AtlasError("nothing to change")
    return out


def _core_and_custom(session: Session, media_type: str, payload: dict[str, Any], skip: tuple[str, ...] = ()
                     ) -> dict[str, Any]:
    raw_custom = payload.get("custom") or {}
    core_in = {k: v for k, v in payload.items() if k not in ("custom", "media_type", *skip)}
    out: dict[str, Any] = {}
    if any(v not in (None, "", []) for v in core_in.values()):
        out.update(clean_edit(core_in))
    values = custom.clean_values(session, media_type, raw_custom, editable_only=True) if raw_custom else {}
    if values:
        out["custom"] = values
    if not out:
        raise AtlasError("nothing to change")
    return out


def _validate(session: Session, kind: str, canonical_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "edit":
        row = session.get(Canonical, canonical_id) if canonical_id else None
        if row is None or row.merged_into:
            raise AtlasError("unknown game", "not_found")
        return _core_and_custom(session, row.media_type, payload)
    if kind == "new_game":
        if not (payload.get("title") or "").strip():
            raise AtlasError("a title is required")
        mtype = payload.get("media_type") or "game"
        from .service import _valid_type

        mtype = _valid_type(session, mtype)
        body = _core_and_custom(session, mtype, payload, skip=("provider_ids",))
        body["media_type"] = mtype
        ids = payload.get("provider_ids") or {}
        if not isinstance(ids, dict) or len(ids) > 16 or not all(
                re.fullmatch(r"[a-z0-9._-]{1,50}", str(k)) and isinstance(v, str) and 0 < len(v) <= 256
                for k, v in ids.items()):
            raise AtlasError("provider_ids must map provider keys to ids")
        if ids:
            body["provider_ids"] = ids
        return body
    if kind == "artwork":
        return {"items": facets.clean_artwork(payload.get("items") or [])}
    if kind == "achievements":
        return {"items": facets.clean_achievements(payload.get("items") or [])}
    if kind == "link_ids":
        return {"provider_ids": _clean_links(session, canonical_id, payload.get("provider_ids"))}
    raise AtlasError(f"kind must be one of {KINDS}")


def _clean_links(session: Session, cid: str | None, ids: Any) -> dict[str, str]:
    """Store ids to link to an existing game; refuse what would contradict the records already there."""
    if not isinstance(ids, dict) or not 1 <= len(ids) <= 8:
        raise AtlasError("give between one and eight store ids, each as store and id")
    out: dict[str, str] = {}
    for key, value in ids.items():
        ns = str(key).strip().lower()
        ident = value.strip() if isinstance(value, str) else ""
        if not re.fullmatch(r"[a-z0-9._-]{1,50}", ns) or not 0 < len(ident) <= 256 or any(c.isspace() for c in ident):
            raise AtlasError("a store name uses a-z, 0-9, '.', '_' or '-'; an id has no spaces and at most 256 characters")
        mapped = session.get(Crosswalk, (ns, ident))
        if mapped is not None and follow(session, mapped.canonical_id) != cid:
            raise AtlasError(f"{ns}:{ident} is already linked to another game; ask a reviewer to merge the two games",
                             "conflict")
        row = session.get(Canonical, cid) if cid else None
        have = (row.view.get("provider_ids") or {}).get(ns) if row is not None else None
        if have and have != ident:
            raise AtlasError(f"this game already has {ns} id {have}; propose a correction through an edit instead",
                             "conflict")
        out[ns] = ident
    return out


# ---- applying -----------------------------------------------------------------------------------------

def _community_obs(session: Session, cid: str) -> Observation | None:
    return session.scalars(select(Observation).where(
        Observation.source == SOURCE, Observation.external_id == cid, Observation.is_current.is_(True))).first()


def _to_item(cid: str, reported: dict[str, Any]) -> dict[str, Any]:
    item = {k: v for k, v in reported.items() if k in ("title", "platform", "platforms", "edition", "release_date",
                                                       "provider_ids", "facts", "source_updated_at")}
    item["external_id"] = cid
    return item


def _ingest_community(session: Session, ctx: Ctx, cid: str, reported: dict[str, Any], external_id: str | None = None,
                      force: bool = True, media_type: str | None = None) -> str:
    item = _to_item(external_id or cid, reported)
    item.pop("source_updated_at", None)
    batch = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "metadata", "source": SOURCE, "source_scope": SCOPE},
        "observations": [item]})
    res = ingest(session, ctx, batch, curator=True, force_canonical=cid if force else None, media_type=media_type)[0]
    return res.canonical_id  # type: ignore[return-value]


def _merge_edit(current: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in current.items() if k != "source_updated_at"}
    facts = dict(out.get("facts") or {})
    for key, value in edit.items():
        if key in ("title", "release_date"):
            out[key] = value
        elif key == "platforms":
            out["platforms"] = value
        else:
            facts[key] = value
    if facts:
        out["facts"] = facts
    return out


def apply(session: Session, ctx: Ctx, p: Proposal) -> None:
    if p.kind == "edit":
        cid = p.canonical_id
        if cid is None or session.get(Canonical, cid) is None:
            raise AtlasError("the game no longer exists", "not_found")
        cur = _community_obs(session, cid)
        before = dict(cur.payload) if cur else None
        core = {k: v for k, v in p.payload.items() if k != "custom"}
        p.previous = {"payload": before, "custom": None}
        if core:
            _ingest_community(session, ctx, cid, _merge_edit(before or {}, core))
        if p.payload.get("custom"):
            old = custom.source_values(session, cid, SOURCE)
            p.previous = {**p.previous, "custom": {k: old.get(k) for k in p.payload["custom"]}}
            custom.set_values(session, ctx, cid, SOURCE, p.payload["custom"],
                              redistributable=ctx.settings.role == "authority")
    elif p.kind == "new_game":
        reported = _merge_edit({}, {k: v for k, v in p.payload.items() if k not in ("provider_ids", "custom", "media_type")})
        if p.payload.get("provider_ids"):
            reported["provider_ids"] = p.payload["provider_ids"]
        p.canonical_id = _ingest_community(session, ctx, "new-" + p.id, reported, external_id="new-" + p.id,
                                           force=False, media_type=p.payload.get("media_type"))
        if p.payload.get("custom"):
            custom.set_values(session, ctx, p.canonical_id, SOURCE, p.payload["custom"],
                              redistributable=ctx.settings.role == "authority")
        p.previous = {"created": True}
    elif p.kind in ("artwork", "achievements"):
        cid = p.canonical_id
        if cid is None or session.get(Canonical, cid) is None:
            raise AtlasError("the game no longer exists", "not_found")
        redistribute = ctx.settings.role == "authority"
        if p.kind == "artwork":
            rows = session.scalars(select(Artwork).where(Artwork.canonical_id == cid, Artwork.source == SOURCE)).all()
            old = [{"kind": a.kind, "url": a.url, "priority": a.priority, "width": a.width, "height": a.height}
                   for a in rows]
            old = [{k: v for k, v in i.items() if v is not None} for i in old]
            p.previous = {"items": old}
            facets.set_artwork(session, ctx, cid, SOURCE, old + p.payload["items"], redistributable=redistribute)
        else:
            rows = session.scalars(select(Achievement).where(
                Achievement.canonical_id == cid, Achievement.source == SOURCE)).all()
            old = [{"name": a.name, "description": a.description, "external_id": a.external_id, "hidden": a.hidden,
                    **({"icon_url": a.icon_url} if a.icon_url else {}),
                    **({"global_percent": a.global_percent} if a.global_percent is not None else {})} for a in rows]
            p.previous = {"items": old}
            facets.set_achievements(session, ctx, cid, SOURCE, old + p.payload["items"], redistributable=redistribute)
    elif p.kind == "link_ids":
        cid = p.canonical_id
        if cid is None or session.get(Canonical, cid) is None:
            raise AtlasError("the game no longer exists", "not_found")
        _clean_links(session, cid, p.payload["provider_ids"])  # re-check: the world may have changed since review
        cur = _community_obs(session, cid)
        before = dict(cur.payload) if cur else None
        new = {k: v for k, v in p.payload["provider_ids"].items()
               if session.get(Crosswalk, (k, v)) is None}  # only rows this proposal creates are undone on revert
        reported = dict(before or {})
        reported.setdefault("title", session.get(Canonical, cid).view.get("title") or "Untitled")
        reported["provider_ids"] = {**(reported.get("provider_ids") or {}), **p.payload["provider_ids"]}
        p.previous = {"payload": before, "added": [[k, v] for k, v in new.items()]}
        _ingest_community(session, ctx, cid, reported)
    else:  # pragma: no cover - validated earlier
        raise AtlasError("unknown proposal kind")


def revert_applied(session: Session, ctx: Ctx, p: Proposal) -> None:
    prev = p.previous or {}
    if p.kind == "edit":
        for key, old in (prev.get("custom") or {}).items():
            if old is None:
                custom.set_values(session, ctx, p.canonical_id, SOURCE, {}, clear=(key,),  # type: ignore[arg-type]
                                  redistributable=ctx.settings.role == "authority")
            else:
                custom.set_values(session, ctx, p.canonical_id, SOURCE, {key: old},  # type: ignore[arg-type]
                                  redistributable=ctx.settings.role == "authority")
        before = prev.get("payload")
        if not p.payload.keys() - {"custom"}:
            return
        if before:
            _ingest_community(session, ctx, p.canonical_id, before)  # type: ignore[arg-type]
        else:  # there was no community observation: neutralise it by superseding with an empty claim
            cur = _community_obs(session, p.canonical_id)  # type: ignore[arg-type]
            if cur:
                cur.is_current = False
                from .service import rebuild
                rebuild(session, ctx, p.canonical_id)  # type: ignore[arg-type]
    elif p.kind == "link_ids":
        for ns, ident in prev.get("added", []):
            row = session.get(Crosswalk, (ns, ident))
            if row is not None and follow(session, row.canonical_id) == p.canonical_id:
                session.delete(row)
        session.flush()
        before = prev.get("payload")
        if before:
            _ingest_community(session, ctx, p.canonical_id, before)  # type: ignore[arg-type]
        else:
            cur = _community_obs(session, p.canonical_id)  # type: ignore[arg-type]
            if cur:
                cur.is_current = False
            from .service import rebuild
            rebuild(session, ctx, p.canonical_id)  # type: ignore[arg-type]
    elif p.kind == "artwork":
        facets.set_artwork(session, ctx, p.canonical_id, SOURCE, prev.get("items", []),  # type: ignore[arg-type]
                           redistributable=ctx.settings.role == "authority")
    elif p.kind == "achievements":
        facets.set_achievements(session, ctx, p.canonical_id, SOURCE, prev.get("items", []),  # type: ignore[arg-type]
                                redistributable=ctx.settings.role == "authority")
    else:
        raise AtlasError("this kind of proposal cannot be reverted; merge or edit the game instead", "conflict")


# ---- workflow -----------------------------------------------------------------------------------------

def can_auto_apply(ctx: Ctx, user: User) -> bool:
    s = ctx.settings
    trusted = s.auto_approve_reputation > 0 and user.reputation >= s.auto_approve_reputation
    return accounts.can_review(s, user) or trusted


def _check_capacity(session: Session, settings, kind: str, cid: str, items: list[dict[str, Any]]) -> None:
    """Refuse up front what could never be approved later, and bound what the server must fetch."""
    if kind == "artwork":
        have = session.scalar(select(func.count()).select_from(Artwork).where(
            Artwork.canonical_id == cid, Artwork.source == SOURCE)) or 0
        if have + len(items) > settings.community_art_cap:
            raise AtlasError(f"community artwork for a game is limited to {settings.community_art_cap} images")
    else:
        have = session.scalar(select(func.count()).select_from(Achievement).where(
            Achievement.canonical_id == cid, Achievement.source == SOURCE)) or 0
        if have + len(items) > facets.MAX_ACH:
            raise AtlasError("this game already has the maximum number of community achievements")


def create(session: Session, ctx: Ctx, user: User, kind: str, canonical_id: str | None, payload: dict[str, Any],
           note: str = "") -> Proposal:
    s = ctx.settings
    write_lock(session)  # the limits below are counted in SQL; serialise with reviewers
    session.flush()
    if user.status != "active":
        raise AtlasError("account suspended", "forbidden")
    if not mail.is_verified(s, user):
        raise AtlasError("confirm your email address before contributing (see your profile)", "forbidden")
    payload = _validate(session, kind, canonical_id, payload)
    if kind != "new_game":
        row = session.get(Canonical, canonical_id) if canonical_id else None
        if row is None or row.merged_into:
            raise AtlasError("unknown game", "not_found")
    else:
        canonical_id = None
    note = (note or "").strip()[:2000]
    if kind in ("artwork", "achievements"):
        _check_capacity(session, s, kind, canonical_id, payload["items"])  # type: ignore[arg-type]
    pending = session.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.proposer_id == user.id, Proposal.status == "pending"))
    if pending >= s.max_pending_proposals:
        raise AtlasError("too many pending proposals; wait for review", "conflict")
    if not accounts.can_review(s, user):  # reviewers are bounded by the audit trail instead
        today = session.scalar(select(func.count()).select_from(Proposal).where(
            Proposal.proposer_id == user.id, Proposal.created_at >= now() - timedelta(days=1)))
        limit = s.trusted_daily_proposals if can_auto_apply(ctx, user) else s.new_user_daily_proposals
        if today >= limit:
            raise AtlasError("daily proposal limit reached", "conflict")
    p = Proposal(kind=kind, canonical_id=canonical_id, proposer_id=user.id, payload=payload, note=note)
    session.add(p)
    session.flush()
    if can_auto_apply(ctx, user):
        apply(session, ctx, p)
        # No reputation for self-approved changes: it is earned when a reviewer confirms work.
        p.status, p.auto_applied, p.decided_at = "approved", True, now()
        accounts.audit(session, user.id, "proposal_auto_applied", p.id, {"kind": kind})
        social.on_proposal_applied(session, ctx, p, user)
    return p


def _decidable(session: Session, ctx: Ctx, reviewer: User, pid: str, *, statuses: tuple[str, ...]) -> Proposal:
    write_lock(session)  # decisions are check-then-write; one at a time, so nothing is applied or paid twice
    session.flush()
    if not accounts.can_review(ctx.settings, reviewer):
        raise AtlasError("review rights required", "forbidden")
    p = session.get(Proposal, pid)
    if p is None:
        raise AtlasError("unknown proposal", "not_found")
    if p.status not in statuses:
        raise AtlasError(f"proposal is {p.status}", "conflict")
    if p.proposer_id == reviewer.id and reviewer.role != "admin":
        raise AtlasError("you cannot review your own proposal", "forbidden")
    return p


def review(session: Session, ctx: Ctx, reviewer: User, pid: str, *, approve: bool, note: str = "") -> Proposal:
    p = _decidable(session, ctx, reviewer, pid, statuses=("pending",))
    proposer = session.get(User, p.proposer_id)
    if approve:
        apply(session, ctx, p)
        p.status = "approved"
        if proposer:
            adjust_reputation(session, proposer, REWARD[p.kind], "approved:" + p.kind, p.id)
    else:
        p.status = "rejected"
        if proposer:
            adjust_reputation(session, proposer, REJECT_PENALTY, "rejected:" + p.kind, p.id)
    p.decided_by, p.decided_at, p.decision_note = reviewer.id, now(), (note or "").strip()[:2000]
    accounts.audit(session, reviewer.id, "proposal_" + p.status, p.id)
    social.on_proposal_decided(session, ctx, p, p.status, reviewer)
    if p.status == "approved":
        social.on_proposal_applied(session, ctx, p, proposer)
    return p


def revert(session: Session, ctx: Ctx, reviewer: User, pid: str, note: str = "") -> Proposal:
    p = _decidable(session, ctx, reviewer, pid, statuses=("approved",))
    if p.canonical_id and p.decided_at is not None:
        newer = session.scalars(select(Proposal.id).where(
            Proposal.canonical_id == p.canonical_id, Proposal.kind == p.kind, Proposal.status == "approved",
            Proposal.id != p.id, Proposal.decided_at > p.decided_at)).first()  # order of application
        if newer:  # a revert restores the state before this proposal, which would also erase newer ones
            raise AtlasError("revert the newer proposals for this game first", "conflict")
    revert_applied(session, ctx, p)
    p.status, p.decided_by, p.decided_at = "reverted", reviewer.id, now()
    p.decision_note = (note or "").strip()[:2000]
    proposer = session.get(User, p.proposer_id)
    if proposer:
        earned = session.scalar(select(func.coalesce(func.sum(ReputationEvent.delta), 0)).where(
            ReputationEvent.proposal_id == p.id, ReputationEvent.delta > 0)) or 0
        adjust_reputation(session, proposer, REVERT_PENALTY - earned, "reverted:" + p.kind, p.id)
    accounts.audit(session, reviewer.id, "proposal_reverted", p.id)
    social.on_proposal_decided(session, ctx, p, "reverted", reviewer)
    return p


def withdraw(session: Session, user: User, pid: str) -> Proposal:
    write_lock(session)
    session.flush()
    p = session.get(Proposal, pid)
    if p is None or p.proposer_id != user.id:
        raise AtlasError("unknown proposal", "not_found")
    if p.status != "pending":
        raise AtlasError(f"proposal is {p.status}", "conflict")
    p.status, p.decided_at = "withdrawn", now()
    return p
