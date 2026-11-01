"""Per-user preferences: local storage first, then a multi-value register per field (CONTRACT-C10 §4.1-§4.6).

Every edit is a version with a hybrid logical clock (HLC) and the list of head HLCs the editor saw (`base`). The
versions of one (user, field) form a DAG. Heads, the displayed value and the open conflicts are a pure function of
the version set (`compute`), so every node that holds the same versions shows the same value and the same
conflicts, whatever the arrival order. Nothing is silently discarded: a losing concurrent user edit opens a row in
the review queue (`/me/conflicts`) until the user decides.

Profiles work without any group. Group sync (`group_channel`) only adds remote versions through `ingest_remote`
and sends local ones out through the emit hook in `set_local`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import profile_schema
from .models import (
    AuditEntry,
    Follow,
    NodeState,
    ProfileConflict,
    ProfileField,
    ProfileSuperseded,
    ProfileSyncConsent,
    ProfileVersion,
    User,
)
from .signing import canonical_json

HLC_STATE = "profile_hlc"
SKEW_MS = 300_000
COUNTER_MAX = 0xFFFF
SUPERSEDED_KEEP = timedelta(days=180)
TOMBSTONE_KEEP = timedelta(days=400)
CLEAR = object()  # sentinel: reset the field to the node default
OPEN = "open"


class ProfileError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


# ---- HLC (§4.4) -------------------------------------------------------------------------------------------------

def fmt_hlc(ms: int, counter: int, node: str) -> str:
    return f"{ms:013d}-{counter:04x}-{node}"


def parse_hlc(hlc: str) -> tuple[int, int, str]:
    ms, counter, node = hlc.split("-")
    return int(ms), int(counter, 16), node


def hlc_time(hlc: str) -> datetime:
    return datetime.fromtimestamp(parse_hlc(hlc)[0] / 1000, UTC)


def _wall_ms(now: datetime | None = None) -> int:
    return int((now or utcnow()).timestamp() * 1000)


def tick(last: str | None, node: str, wall_ms: int) -> str:
    """The next HLC after `last` at wall time `wall_ms`. The counter never exceeds 0xffff: on overflow the
    millisecond part is bumped instead (§4.4)."""
    if last is None:
        return fmt_hlc(wall_ms, 0, node)
    ms, counter, _ = parse_hlc(last)
    if wall_ms > ms:
        return fmt_hlc(wall_ms, 0, node)
    if counter >= COUNTER_MAX:
        return fmt_hlc(ms + 1, 0, node)
    return fmt_hlc(ms, counter + 1, node)


def _clock(session: Session) -> NodeState:
    row = session.get(NodeState, HLC_STATE, with_for_update=True)
    if row is None:
        row = NodeState(key=HLC_STATE, value="")
        session.add(row)
        session.flush()
    return row


def next_hlc(session: Session, node: str, now: datetime | None = None) -> str:
    row = _clock(session)
    hlc = tick(row.value or None, node, _wall_ms(now))
    row.value = hlc
    return hlc


def observe(session: Session, hlc: str, now: datetime | None = None) -> bool:
    """Advance the local clock past a received HLC. False (and no change) when it is more than SKEW ahead of
    this node's wall clock: such a version is `retry:future`, so a fast clock cannot win forever (T14)."""
    ms = parse_hlc(hlc)[0]
    if ms > _wall_ms(now) + SKEW_MS:
        return False
    row = _clock(session)
    ms, counter, node = parse_hlc(hlc)
    if not row.value or parse_hlc(row.value)[:2] < (ms, counter):
        row.value = fmt_hlc(ms, counter, node)
    return True


# ---- the pure register (§4.4) ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class V:
    hlc: str
    value: Any
    cleared: bool
    base: tuple[str, ...]
    cause: str
    origin: str


def canon(v: V) -> str:
    return "\x00clear" if v.cleared else canonical_json(v.value).decode()


@dataclass(frozen=True)
class State:
    heads: tuple[V, ...]
    winner: V | None
    conflicts: tuple[tuple[str, str], ...]  # (winner hlc, loser hlc), sorted


def compute(versions: list[V]) -> State:
    """Heads, displayed value and open conflicts of one register: a pure function of the version set.

    Heads are versions in no other version's base. The displayed value is the head with the highest HLC, where
    user edits outrank imports, defaults and migration seeds ("they lose quietly to user versions"). A conflict is
    open when at least two user heads disagree; every user head whose value differs from the winner's is a loser."""
    based: set[str] = set()
    for v in versions:
        based.update(v.base)
    heads = tuple(sorted((v for v in versions if v.hlc not in based), key=lambda v: v.hlc))
    if not heads:
        return State((), None, ())
    winner = max(heads, key=lambda v: (v.cause == "user", v.hlc))
    users = [h for h in heads if h.cause == "user"]
    conflicts: tuple[tuple[str, str], ...] = ()
    if len(users) >= 2 and len({canon(h) for h in users}) >= 2:
        conflicts = tuple(sorted((winner.hlc, h.hlc) for h in users if h is not winner and canon(h) != canon(winner)))
    return State(heads, winner, conflicts)


# ---- storage ----------------------------------------------------------------------------------------------------

def _v(row: ProfileVersion) -> V:
    return V(row.hlc, row.value, row.cleared, tuple(row.base or ()), row.cause, row.origin_node)


def live_versions(session: Session, user_id: str, field: str) -> list[ProfileVersion]:
    return list(session.scalars(select(ProfileVersion).where(
        ProfileVersion.user_id == user_id, ProfileVersion.field == field, ProfileVersion.held_since.is_(None))))


def heads(session: Session, user_id: str, field: str) -> list[ProfileVersion]:
    rows = live_versions(session, user_id, field)
    state = compute([_v(r) for r in rows])
    keep = {h.hlc for h in state.heads}
    return sorted((r for r in rows if r.hlc in keep), key=lambda r: r.hlc)


def state_of(session: Session, user_id: str, field: str) -> State:
    return compute([_v(r) for r in live_versions(session, user_id, field)])


def _superseded(session: Session, user_id: str, field: str) -> set[str]:
    return set(session.scalars(select(ProfileSuperseded.hlc).where(
        ProfileSuperseded.user_id == user_id, ProfileSuperseded.field == field)))


def node_default(settings: Any, user: User | None, field: str) -> Any:
    if field == "theme":
        return getattr(settings, "default_theme", "auto")
    if field == "privacy.profile_public":
        return True
    if field == "bio":
        return ""
    return None


def recompute(session: Session, settings: Any, own_node: str, user: User, field: str,
              now: datetime | None = None) -> State:
    """Materialize the displayed value, the head flags and the review rows of one register."""
    now = now or utcnow()
    rows = live_versions(session, user.id, field)
    state = compute([_v(r) for r in rows])
    keep = {h.hlc for h in state.heads}
    for r in rows:
        r.is_head = r.hlc in keep
    shown = session.get(ProfileField, (user.id, field))
    winner = state.winner
    if winner is not None:
        value = None if winner.cleared else winner.value
        widen = field == "privacy.profile_public" and _privacy_widening(session, settings, own_node, user, winner,
                                                                       shown)
        if widen:
            _open_review(session, settings, user, field, "privacy_widen", None, winner.hlc,
                         {"origin": winner.origin}, now)
        elif shown is None:
            session.add(ProfileField(user_id=user.id, field=field, value=value, hlc=winner.hlc,
                                     origin_node=winner.origin, cause=winner.cause, updated_at=now))
        elif shown.hlc != winner.hlc:
            shown.value, shown.hlc, shown.origin_node, shown.cause, shown.updated_at = (
                value, winner.hlc, winner.origin, winner.cause, now)
        if not widen:
            _dual_write(session, settings, user, field, value)
    _sync_conflicts(session, settings, user, field, state, now)
    session.flush()
    return state


def _privacy_widening(session: Session, settings: Any, own_node: str, user: User, winner: V,
                      shown: ProfileField | None) -> bool:
    """`privacy.profile_public` only narrows automatically (D10.17): a remote `true` (or a clear, whose default is
    public) arriving while this node shows `false` waits for the user's review."""
    new = True if winner.cleared else bool(winner.value)
    if not new or winner.origin == own_node:
        return False
    if shown is not None and shown.hlc == winner.hlc:
        return False
    current = shown.value if shown is not None and shown.value is not None else user.profile_public
    if current is False:
        return True
    row = session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == user.id, ProfileConflict.field == "privacy.profile_public",
        ProfileConflict.kind == "privacy_widen", ProfileConflict.loser_hlc == winner.hlc)).first()
    return row is not None and row.status in (OPEN, "dismissed")


def _dual_write(session: Session, settings: Any, user: User, field: str, value: Any) -> None:
    """`User.bio` and `User.profile_public` stay the source of truth for older code (D10.23)."""
    if field == "bio":
        user.bio = value if isinstance(value, str) else ""
    elif field == "privacy.profile_public":
        public = True if value is None else bool(value)
        if user.profile_public and not public:  # as /me/settings: a private profile has no followers
            session.execute(delete(Follow).where(Follow.target_type == "user", Follow.target_id == user.id))
        user.profile_public = public


def _open_review(session: Session, settings: Any, user: User, field: str, kind: str, winner_hlc: str | None,
                 loser_hlc: str | None, detail: dict | None, now: datetime) -> ProfileConflict | None:
    row = session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == user.id, ProfileConflict.field == field, ProfileConflict.kind == kind,
        ProfileConflict.loser_hlc == loser_hlc if loser_hlc is not None else ProfileConflict.loser_hlc.is_(None))
    ).first()
    if row is not None:
        if row.status == "merged":
            row.status, row.resolved_at, row.winner_hlc = OPEN, None, winner_hlc
        return None
    row = ProfileConflict(user_id=user.id, field=field, kind=kind, winner_hlc=winner_hlc, loser_hlc=loser_hlc,
                          detail=detail, status=OPEN, detected_at=now)
    session.add(row)
    session.flush()
    action = "profile_privacy_review" if kind == "privacy_widen" else "profile_conflict_opened"
    session.add(AuditEntry(actor_id=None, action=action, target=user.id,
                           detail={"field": field, "kind": kind, "winner_hlc": winner_hlc, "loser_hlc": loser_hlc}))
    _notify(session, settings, user, field, kind)
    return row


TITLES = {"concurrent": "Your {label} was changed on two nodes at once",
          "privacy_widen": "Another node wants to make your profile public",
          "suspect": "Your {label} was last set by a node that was revoked as compromised",
          "suspect_link": "A revoked node was linked to your account",
          "missing_base": "Your {label} arrived without its history"}


def _notify(session: Session, settings: Any, user: User, field: str, kind: str) -> None:
    from . import social
    from .service import Ctx

    label = profile_schema.REGISTRY[field].label.lower() if field in profile_schema.REGISTRY else field
    title = TITLES.get(kind, "Your profile needs a decision").format(label=label)
    if settings is None:
        return
    social.notify(session, Ctx(settings, None), user.id, "profile_conflict", title, "/me/conflicts",  # type: ignore[arg-type]
                  key=f"{kind}:{field}")


def _sync_conflicts(session: Session, settings: Any, user: User, field: str, state: State, now: datetime) -> None:
    want = {loser: winner for winner, loser in state.conflicts}
    existing = {r.loser_hlc: r for r in session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == user.id, ProfileConflict.field == field, ProfileConflict.kind == "concurrent"))}
    for loser, row in existing.items():
        if row.status == OPEN and loser not in want:
            row.status, row.resolved_at = "merged", now
            session.add(AuditEntry(actor_id=None, action="profile_conflict_resolved", target=user.id,
                                   detail={"field": field, "loser_hlc": loser, "how": "merged"}))
        elif loser in want and row.status == OPEN:
            row.winner_hlc = want[loser]
    for loser, winner in want.items():
        row = existing.get(loser)
        if row is None or row.status == "merged":
            v = next(h for h in state.heads if h.hlc == loser)
            w = next(h for h in state.heads if h.hlc == winner)
            _open_review(session, settings, user, field, "concurrent", winner, loser,
                         {"winner_origin": w.origin, "loser_origin": v.origin}, now)


# ---- edits ------------------------------------------------------------------------------------------------------

def validate(field: str, value: Any) -> Any:
    if not profile_schema.known(field):
        raise ProfileError(f"unknown preference {field!r}")
    if value is CLEAR:
        return CLEAR
    try:
        return profile_schema.validate(field, value)
    except profile_schema.FieldError as exc:
        raise ProfileError(f"{profile_schema.REGISTRY[field].label}: {exc}") from None


def set_local(session: Session, settings: Any, identity: Any, user: User, field: str, value: Any, *,
              cause: str = "user", base: list[str] | None = None, now: datetime | None = None,
              emit: bool = True) -> ProfileVersion:
    """A local edit: a new version whose base is every current head (so an edit made while a conflict shows also
    resolves it, §4.4), then emitted to the group when the user, the operator and the link allow it."""
    value = validate(field, value)
    now = now or utcnow()
    current = [h.hlc for h in heads(session, user.id, field)]
    chosen = sorted(set(current) | set(base or ()))[-profile_schema_max_base():]
    hlc = next_hlc(session, identity.node_id, now)
    row = ProfileVersion(user_id=user.id, field=field, hlc=hlc, value=None if value is CLEAR else value,
                         cleared=value is CLEAR, base=chosen, cause=cause, origin_node=identity.node_id,
                         entry_ref=None, is_head=True, held_since=None, created_at=now)
    session.add(row)
    session.flush()
    recompute(session, settings, identity.node_id, user, field, now)
    if emit:
        from . import group_channel

        row.entry_ref = group_channel.emit_version(session, settings, identity, user, row, now=now)
    return row


def profile_schema_max_base() -> int:
    from .group_schema import MAX_BASE

    return MAX_BASE


def ingest_remote(session: Session, settings: Any, own_node: str, user: User, field: str, *, hlc: str, value: Any,
                  cleared: bool, base: list[str], cause: str, origin: str, entry_ref: str | None,
                  extra_known: set[str] | None = None, now: datetime | None = None) -> str:
    """A version from another node: noop | bad_base | future | held | applied (§4.4 causal delivery)."""
    now = now or utcnow()
    if session.get(ProfileVersion, (user.id, field, hlc)) is not None or hlc in _superseded(session, user.id, field):
        return "noop"
    if any(b >= hlc for b in base):
        return "bad_base"
    if not observe(session, hlc, now):
        return "future"
    known = {r.hlc for r in live_versions(session, user.id, field)} | _superseded(session, user.id, field)
    missing = set(base) - known - (extra_known or set())
    row = ProfileVersion(user_id=user.id, field=field, hlc=hlc, value=None if cleared else value, cleared=cleared,
                         base=list(base), cause=cause, origin_node=origin, entry_ref=entry_ref, is_head=False,
                         held_since=now if missing else None, created_at=now)
    session.add(row)
    session.flush()
    if missing:
        return "held"
    recompute(session, settings, own_node, user, field, now)
    release_held(session, settings, own_node, user, field, now=now, extra_known=extra_known)
    return "applied"


def release_held(session: Session, settings: Any, own_node: str, user: User, field: str, *,
                 timeout: timedelta | None = None, now: datetime | None = None,
                 extra_known: set[str] | None = None) -> list[str]:
    """Apply held versions whose base has arrived; after `timeout`, apply them with the missing references
    ignored and open a `missing_base` review row (liveness, §4.4). Returns the released HLCs."""
    now = now or utcnow()
    released: list[str] = []
    while True:
        held = list(session.scalars(select(ProfileVersion).where(
            ProfileVersion.user_id == user.id, ProfileVersion.field == field, ProfileVersion.held_since.isnot(None))
            .order_by(ProfileVersion.hlc)))
        if not held:
            break
        known = {r.hlc for r in live_versions(session, user.id, field)} | _superseded(session, user.id, field) \
            | (extra_known or set())
        progress = False
        for row in held:
            missing = set(row.base or ()) - known
            expired = timeout is not None and _aware(row.held_since) <= now - timeout  # type: ignore[operator]
            if missing and not expired:
                continue
            row.held_since = None
            released.append(row.hlc)
            if missing:
                _open_review(session, settings, user, field, "missing_base", None, row.hlc,
                             {"origin": row.origin_node, "missing": sorted(missing)}, now)
            progress = True
            known.add(row.hlc)
        session.flush()
        if not progress:
            break
    if released:
        recompute(session, settings, own_node, user, field, now)
    return released


def held_fields(session: Session, user_id: str) -> list[ProfileVersion]:
    return list(session.scalars(select(ProfileVersion).where(ProfileVersion.user_id == user_id,
                                                             ProfileVersion.held_since.isnot(None))))


# ---- reading ----------------------------------------------------------------------------------------------------

def values(session: Session, settings: Any, user: User | None) -> dict[str, Any]:
    """The displayed value of every registry field (node default where unset or cleared)."""
    out = {name: node_default(settings, user, name) for name in profile_schema.REGISTRY}
    if user is None:
        return out
    for row in session.scalars(select(ProfileField).where(ProfileField.user_id == user.id)):
        if row.field in out and row.value is not None:
            out[row.field] = row.value
    out["bio"] = user.bio or ""
    out["privacy.profile_public"] = bool(user.profile_public)
    return out


def open_conflicts(session: Session, user_id: str) -> list[ProfileConflict]:
    return list(session.scalars(select(ProfileConflict).where(
        ProfileConflict.user_id == user_id, ProfileConflict.status == OPEN).order_by(ProfileConflict.detected_at)))


def open_count(session: Session, user_id: str) -> int:
    return len(open_conflicts(session, user_id))


def version(session: Session, user_id: str, field: str, hlc: str | None) -> ProfileVersion | None:
    return session.get(ProfileVersion, (user_id, field, hlc)) if hlc else None


# ---- resolution (§4.4, §4.6) ------------------------------------------------------------------------------------

def resolve(session: Session, settings: Any, identity: Any, user: User, conflict_id: str, choice: str,
            custom: Any = None, now: datetime | None = None) -> ProfileConflict:
    """Keep the winner, keep the loser, or enter a new value. A value decision is a new version whose base is
    every current head (merging the DAG) plus a `profile.conflict.resolve` record, so the queue empties on every
    node. Review rows without competing versions (privacy widening, suspect values) take keep/accept/dismiss."""
    now = now or utcnow()
    row = session.get(ProfileConflict, conflict_id)
    if row is None or row.user_id != user.id or row.status != OPEN:
        raise ProfileError("no such open conflict")
    field = row.field
    if row.kind == "suspect_link":
        from . import group_channel

        group_channel.resolve_suspect_link(session, settings, identity, user, row, choice, now)
    elif row.kind in ("concurrent", "missing_base", "suspect"):
        if choice == "dismiss" and row.kind != "concurrent":
            pass
        else:
            if choice == "custom":
                value = custom
            else:
                pick = row.winner_hlc if choice == "winner" else row.loser_hlc
                if row.kind != "concurrent" and choice == "loser":
                    value = CLEAR  # suspect / missing_base: "reset to the default"
                else:
                    v = version(session, user.id, field, pick)
                    if v is None:
                        raise ProfileError("that value is no longer available")
                    value = CLEAR if v.cleared else v.value
            discarded = [h.hlc for h in heads(session, user.id, field)]
            new = set_local(session, settings, identity, user, field, value, now=now)
            from . import group_channel

            group_channel.emit_resolve(session, settings, identity, user, field, new.hlc,
                                       [h for h in discarded if h != new.hlc], now=now)
    elif row.kind == "privacy_widen":
        if choice in ("accept", "loser"):
            set_local(session, settings, identity, user, field, True, now=now)
        # "dismiss": this node keeps the profile private; the competing version stays recorded
    if row.status in (OPEN, "merged"):  # the merging version above already marked it "merged"
        row.status = "resolved" if choice != "dismiss" else "dismissed"
        row.resolved_at = now
        session.add(AuditEntry(actor_id=user.id, action="profile_conflict_resolved", target=user.id,
                               detail={"field": field, "kind": row.kind, "choice": choice}))
    session.flush()
    return row


# ---- consent and operator policy (§4.3) -------------------------------------------------------------------------

def policy_fields(settings: Any) -> list[str]:
    if not getattr(settings, "group_profile_sync", True):
        return []
    raw = (getattr(settings, "group_profile_fields", "") or "").replace(",", " ").split()
    allowed = [f for f in raw if f in profile_schema.REGISTRY]
    return allowed if raw else profile_schema.fields()


def consent(session: Session, user_id: str, group_id: str) -> ProfileSyncConsent | None:
    return session.get(ProfileSyncConsent, (user_id, group_id))


def consent_fields(session: Session, user_id: str, group_id: str) -> list[str]:
    row = consent(session, user_id, group_id)
    return list(row.fields) if row is not None else profile_schema.default_consent()


def set_consent(session: Session, user_id: str, group_id: str, fields: list[str],
                signout_propagation: bool = True) -> ProfileSyncConsent:
    clean = [f for f in profile_schema.REGISTRY if f in set(fields)]
    row = consent(session, user_id, group_id)
    if row is None:
        row = ProfileSyncConsent(user_id=user_id, group_id=group_id, fields=clean,
                                 signout_propagation=signout_propagation, updated_at=utcnow())
        session.add(row)
    else:
        row.fields, row.signout_propagation, row.updated_at = clean, signout_propagation, utcnow()
    session.flush()
    return row


def accept(session: Session, settings: Any, user_id: str, group_id: str) -> list[str]:
    """registry ∩ operator policy ∩ this user's consent on this node: what this node sends and receives."""
    allowed = set(policy_fields(settings)) & set(consent_fields(session, user_id, group_id))
    return [f for f in profile_schema.REGISTRY if f in allowed]


# ---- lifecycle --------------------------------------------------------------------------------------------------

def delete_user(session: Session, user_id: str) -> None:
    """Account deletion removes every profile row of the user (§4.5)."""
    for model in (ProfileConflict, ProfileVersion, ProfileField, ProfileSyncConsent):
        session.execute(delete(model).where(model.user_id == user_id))
    session.execute(delete(ProfileSuperseded).where(ProfileSuperseded.user_id == user_id))


def prune(session: Session, now: datetime | None = None) -> int:
    """Retention (H23): superseded versions after 180 days (their HLCs kept 400 days in `profile_superseded`)."""
    now = now or utcnow()
    n = 0
    for row in session.scalars(select(ProfileVersion).where(
            ProfileVersion.is_head.is_(False), ProfileVersion.held_since.is_(None),
            ProfileVersion.created_at < now - SUPERSEDED_KEEP)).all():
        session.merge(ProfileSuperseded(user_id=row.user_id, field=row.field, hlc=row.hlc, pruned_at=now))
        session.delete(row)
        n += 1
    session.execute(delete(ProfileSuperseded).where(ProfileSuperseded.pruned_at < now - TOMBSTONE_KEEP))
    session.flush()
    return n
