"""Scopes: how far an item may travel, and who may see it here (CONTRACT-C14 Part B, §5; Review #1 §5.7).

Two separate axes:

* **Travel scope** (§5.1/§5.2): `public` (signed into a public feed), `group` (shared with the owner's own nodes),
  `private` (one account), `local` (this node only). Derived from flags that already exist (`derive_*` below), plus
  one column, the record ceiling (`canonical.scope_ceiling`, §5.3). It drives the badges, the filters and the
  feed gate (`ScopeCeiling`).
* **Visibility** (§5.7): what a viewer sees on this node. It follows *origin*, not travel scope: this node's own
  provider data stays on its public pages exactly as before, while host/plugin library data ("operator-private")
  and data *received from the group* are left out for viewers without `scope.detail`. Group data comes back for
  them only while `group_data_public_pages` is on, and then only the field categories the administrator made
  public (owner decision D1, the partial public projection), never with a badge, a helper name or a count.

The visibility rule is applied in SQL: a viewer without `scope.detail` reads a record through its
`scope_projection` row (when it has one), so search, sort, pagination, counts and existence checks all see the same
projected data, and a record that exists only through hidden data is the same 404 as an unknown id.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, delete, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from . import dates, feed_schema
from .merge import CLEARED, Obs, merge, source_class
from .models import (
    Achievement,
    Artwork,
    Canonical,
    CustomValue,
    FeedIntroduced,
    Lock,
    NodeState,
    Observation,
    Relation,
    ScopeProjection,
    User,
    UserPermission,
    now,
)
from .normalize import norm_title

log = logging.getLogger("atlas.scope")

SCOPES = ("public", "group", "private", "local")
CEILINGS = ("group", "private", "local")
#: widest first: the record header shows the widest scope among its items
WIDTH = {"public": 0, "group": 1, "private": 2, "local": 3}
GROUP_SOURCE = "atlas-group"
GROUP_SCOPE_PREFIX = "group:"
HOST_KINDS = ("host", "plugin")
#: The `user_permission` values this release knows (C14 §9). `feed.access` follows with C14-3m.
DETAIL = "scope.detail"
PERMISSIONS = (DETAIL,)
#: Non-personal API tokens with one of these scopes act for the operator and see what an administrator sees.
OPERATOR_TOKEN_SCOPES = frozenset({"contribute", "curate", "admin"})

ANON_OFF, ANON_ON = "anon_off", "anon_on"
TIERS = (ANON_OFF, ANON_ON)
PENDING = "scope_projection_pending"
CURSOR = "scope_projection_cursor"

# ---- badges (§5.1, exact text) ------------------------------------------------------------------------------------

BADGES = {"official": "Official", "community": "Community", "mirror": "Mirror", "group": "Your nodes",
          "private": "Only you", "local": "This node only"}
BADGE_HELP = {
    "official": "Signed into a public feed by a node that calls itself official. That claim means something only "
                "to people who pinned that node's key.",
    "community": "Signed into a public feed.", "mirror": "A relayed copy of a public feed.",
    "group": "Shared among your own nodes. Never public, never sent upstream.",
    "private": "Belongs to one account.", "local": "Stays on this node."}
EDIT_GOES = {"public": "Public: signed into your feed", "group": "Your nodes", "private": "Only you",
             "local": "This node only"}

# ---- field categories of the partial public projection (owner decision D1) ----------------------------------------

#: category -> (label, view fields it covers). Artwork kinds and achievements have their own categories below.
CATEGORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "identity": ("Title and other names", ("title", "aliases")),
    "release": ("Release dates", ("release_date", "releases", "first_release", "release_year")),
    "platforms": ("Platforms", ("platforms",)),
    "classification": ("Genres, themes, tags and features", ("genres", "themes", "tags", "features")),
    "credits": ("Developer and publisher", ("developer", "publisher")),
    "series": ("Series and relations", ("series",)),
    "external_ids": ("Store and database ids", ("provider_ids",)),
    "cover_art": ("Cover and key art", ()),
    "descriptions": ("Descriptions and reviews", ("description",)),
    "scores": ("Critic and user scores", ("critic_score", "user_score")),
    "details": ("Age rating, website, languages and custom fields", ("age_rating", "website", "languages")),
    "screenshots": ("Screenshots and other images", ()),
    "achievements": ("Achievement lists and icons", ()),
    "pricing": ("Prices", ()),
}
PUBLIC_DEFAULT = ("identity", "release", "platforms", "classification", "credits", "series", "external_ids",
                  "cover_art")
COVER_KINDS = ("key_art", "poster", "banner", "hero", "logo", "icon")


def art_category(kind: str) -> str:
    return "cover_art" if kind in COVER_KINDS else "screenshots"


def public_categories(settings: Any) -> frozenset[str]:
    raw = getattr(settings, "group_public_categories", None)
    if raw is None:
        return frozenset(PUBLIC_DEFAULT)
    return frozenset(c for c in str(raw).split() if c in CATEGORIES)


def clean_categories(raw: Any) -> str:
    """The stored form of the category setting: known names, space separated, in the table's order."""
    wanted = set(str(raw or "").replace(",", " ").split())
    unknown = sorted(wanted - set(CATEGORIES))
    if unknown:
        raise ValueError(f"unknown field categories: {', '.join(unknown)}")
    return " ".join(c for c in CATEGORIES if c in wanted)


# ---- the feed gate (§5.4) -----------------------------------------------------------------------------------------

class ScopeCeiling(feed_schema.EmitGateError):
    """An entry names a record whose ceiling keeps it out of this node's public feed (CONTRACT-C14 §5.4).

    Its own class on purpose (Review #1, cross-ref C16-6): no rename of the feed-gate base may merge it with another
    error. `local_ok`: every record the entry names has a ceiling, so the edit may be kept on this node without
    signing; false for an entry that also names a public record (a `merge`, a `relation`), which fails closed."""

    def __init__(self, cid: str, ceiling: str, *, local_ok: bool):
        self.canonical_id, self.ceiling, self.local_ok = cid, ceiling, local_ok
        self.feature, self.needed, self.current = "scope ceiling", "publish", ceiling
        where = "shared with your nodes only" if ceiling == "group" else (
            "kept for one account" if ceiling == "private" else "kept on this node only")
        ValueError.__init__(self, f"This game is {where}. Publish it to sign it into your feed ({cid}).")


def ceiling_of(session: Session, cid: str) -> str | None:
    row = session.get(Canonical, cid)
    return row.scope_ceiling if row is not None else None


# ---- viewers (§5.7) -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Viewer:
    """Who is looking. `kind`: `anonymous`, `token` (an API principal without a user that does not act for the
    operator), `user` (signed in, without `scope.detail`) or `detail`. `switch` is `group_data_public_pages`."""

    kind: str
    switch: bool = False
    user_id: str | None = None
    admin: bool = False

    @property
    def detail(self) -> bool:
        return self.kind == "detail"

    @property
    def tier(self) -> str | None:
        """The projection this viewer reads, or None for the full record."""
        if self.detail:
            return None
        return ANON_ON if self.switch else ANON_OFF


#: The full view, for internal callers and operator tools: no projection, no filter.
OPERATOR = Viewer("detail", admin=True)


def _switch(settings: Any) -> bool:
    return bool(getattr(settings, "group_data_public_pages", False))


def holds_detail(session: Session, settings: Any, user: User | None, login: Any = None, rights: Any = None) -> bool:
    """§5.7 (critic of Review #1): administrators and curators (local role, or review rights from a valid grant),
    every active account while registration is closed, and accounts an administrator granted `scope.detail`. A
    self-registered account on an open node does not hold it.

    A session opened through the owner group holds it only "per its C10 caps": through the rights this request was
    given (`effective_rights`, which caps the local role and adds valid grants) or an explicit grant to the linked
    account. Signing in through the group never adds it by itself, or anyone who self-registers on an open sibling
    (or here) and links the account would get the operator's view."""
    if user is None or user.status != "active":
        return False
    federated = login is not None and getattr(login, "via_group", None) is not None
    if rights is not None and (getattr(rights, "review", False) or getattr(rights, "admin", False)):
        return True
    if not federated and user.role in ("admin", "curator"):
        return True
    if getattr(settings, "registration", "open") == "closed":
        return True
    return session.get(UserPermission, (user.id, DETAIL)) is not None


def viewer_for_user(session: Session, settings: Any, user: User | None, login: Any = None,
                    rights: Any = None) -> Viewer:
    switch = _switch(settings)
    if user is None:
        return Viewer("anonymous", switch)
    if holds_detail(session, settings, user, login, rights):
        return Viewer("detail", switch, user.id, admin=user.role == "admin")
    return Viewer("user", switch, user.id)


def viewer_for_principal(session: Session, settings: Any, principal: Any) -> Viewer:
    """An API caller. A personal token is its user. A token an administrator minted for this node's own apps (the
    Playnite plugin, the Tracking App: `read`, `contribute`, `curate`, `admin`) acts for the operator and sees what
    an administrator sees, so the app integration contracts keep resolving the operator's own library. A token that
    holds `sync` without an operator scope is a follower's: a `token` viewer, which sees what an anonymous visitor
    sees (see CONTRACT-AMENDMENTS "C14-1")."""
    from .models import Token

    switch = _switch(settings)
    if principal is None:
        return Viewer("anonymous", switch)
    if getattr(principal, "personal", False):
        row = session.get(Token, principal.token_id)
        user = session.get(User, row.user_id) if row is not None and row.user_id else None
        return viewer_for_user(session, settings, user)
    if principal.scopes & OPERATOR_TOKEN_SCOPES or "sync" not in principal.scopes:
        return Viewer("detail", switch, admin="admin" in principal.scopes)
    return Viewer("token", switch)


def grant(session: Session, user: User, permission: str, actor: str | None) -> bool:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")
    if session.get(UserPermission, (user.id, permission)) is not None:
        return False
    session.add(UserPermission(user_id=user.id, permission=permission, granted_by=actor, granted_at=now()))
    session.flush()
    return True


def revoke(session: Session, user: User, permission: str) -> bool:
    row = session.get(UserPermission, (user.id, permission))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def granted(session: Session, user_ids: list[str], permission: str = DETAIL) -> set[str]:
    if not user_ids:
        return set()
    return set(session.scalars(select(UserPermission.user_id).where(
        UserPermission.user_id.in_(user_ids), UserPermission.permission == permission)))


def granted_all(session: Session, permission: str = DETAIL) -> list[str]:
    return list(session.scalars(select(UserPermission.user_id).where(UserPermission.permission == permission)))


# ---- origin classes (visibility follows origin, §5.7 critic) --------------------------------------------------------

def is_group_obs(o: Any, ceiling: str | None = None) -> bool:
    """Received from the group: an `atlas-group` observation, one stored under a `group:<member>` scope, or any
    observation of a record whose ceiling is `group`."""
    return ceiling == "group" or o.source == GROUP_SOURCE or (o.source_scope or "").startswith(GROUP_SCOPE_PREFIX)


def is_host_obs(o: Any) -> bool:
    return o.source_kind in HOST_KINDS


def group_obs_clause() -> Any:
    return or_(Observation.source == GROUP_SOURCE, Observation.source_scope.like(GROUP_SCOPE_PREFIX + "%"))


def hidden_obs_clause() -> Any:
    return or_(Observation.source_kind.in_(HOST_KINDS), group_obs_clause())


def is_group_art(a: Artwork, ceiling: str | None = None) -> bool:
    return ceiling == "group" or (a.fetched_from or "").startswith(GROUP_SCOPE_PREFIX)


def _group_source(source: str | None) -> bool:
    return bool(source) and (source == GROUP_SOURCE or source.startswith(GROUP_SCOPE_PREFIX))


# ---- travel scope derivation (§5.2, first match wins) ---------------------------------------------------------------

def in_group(session: Session) -> bool:
    from . import groups

    try:
        group = groups.load(session)
    except Exception:  # pragma: no cover - a broken group row never breaks a page
        return False
    return group is not None and group.status in ("active", "resyncing")


def _provider_rule(settings: Any, grouped: bool, what: str = "fields") -> str:
    share = str(getattr(settings, "group_assist_share_provider_data", "none") or "none")
    return "group" if grouped and share != "none" and what in share else "local"


def _narrow(scope: str, ceiling: str | None) -> str:
    if ceiling in CEILINGS and WIDTH[ceiling] > WIDTH[scope]:
        return ceiling
    return scope


def of_observation(settings: Any, o: Observation, ceiling: str | None = None, *, grouped: bool = False) -> str:
    if ceiling in CEILINGS:
        return ceiling
    if o.origin_node:
        return "public"
    if is_group_obs(o):
        return "group"
    if is_host_obs(o):
        return "local"
    if o.redistributable and getattr(settings, "role", "") == "authority":
        return "public"
    if o.source_kind == "metadata" and source_class(o.source) == "provider":
        return _provider_rule(settings, grouped)
    return "local"


def of_artwork(settings: Any, a: Artwork, ceiling: str | None = None, *, grouped: bool = False) -> str:
    if ceiling in CEILINGS:
        return ceiling
    if a.origin_node:
        return "public"
    if is_group_art(a):
        return "group"
    if a.local_only:
        return "local"
    if a.redistributable and getattr(settings, "role", "") == "authority":
        return "public"
    if source_class(a.source) == "provider":
        return _provider_rule(settings, grouped, "media")
    return "local"


def of_achievement(settings: Any, a: Achievement, ceiling: str | None = None, *, grouped: bool = False) -> str:
    if ceiling in CEILINGS:
        return ceiling
    if a.redistributable and getattr(settings, "role", "") == "authority":
        return "public"
    if source_class(a.source) == "provider":
        return _provider_rule(settings, grouped, "achievements")
    return "local"


def of_custom(settings: Any, cv: CustomValue, public_field: bool, ceiling: str | None = None) -> str:
    if ceiling in CEILINGS:
        return ceiling
    if not public_field:
        return "local"
    if _group_source(cv.source):
        return "group"
    if cv.redistributable and getattr(settings, "role", "") == "authority":
        return "public"
    return "local"


def of_relation(settings: Any, r: Relation, ceiling: str | None = None) -> str:
    if ceiling in CEILINGS:
        return ceiling
    if r.origin_node or (r.redistributable and getattr(settings, "role", "") == "authority"):
        return "public"
    if _group_source(r.source):
        return "group"
    return "local"


def of_lock(settings: Any, lk: Lock, ceiling: str | None = None) -> str:
    if ceiling in CEILINGS:
        return ceiling
    return "local" if lk.local_only or getattr(settings, "role", "") != "authority" else "public"


def public_kind(settings: Any, origin_node: str | None = None, session: Session | None = None) -> str:
    """`official`, `community` or `mirror` for a `public` item: this node's own `node_kind` for what it signs, the
    remembered kind of the origin peer for replicated data (`mirror` when it is unknown or relayed)."""
    if origin_node is None:
        kind = settings.kind() if hasattr(settings, "kind") else "community"
        return kind if kind in ("official", "community", "mirror") else "community"
    if session is not None:
        from .models import Peer

        peer = session.scalars(select(Peer).where(Peer.node_id == origin_node).limit(1)).first()
        kind = getattr(peer, "kind", None) if peer is not None else None
        if kind in ("official", "community"):
            return kind
    return "mirror"


def badge(scope: str, kind: str | None = None) -> dict[str, str]:
    key = (kind or "community") if scope == "public" else scope
    return {"scope": scope, "key": key, "label": BADGES[key], "help": BADGE_HELP[key]}


def record_summary(session: Session, settings: Any, row: Canonical) -> dict[str, Any]:
    """The record header for `scope.detail` viewers (§5.5): the widest scope, its badge, item counts per scope and
    the composition line, e.g. "Community · 3 items from your nodes · 1 on this node only". Never computed for
    other viewers (§5.7: no counts)."""
    grouped = in_group(session)
    ceiling = row.scope_ceiling
    counts = {s: 0 for s in SCOPES}
    public_origin: str | None = None
    for o in session.scalars(select(Observation).where(Observation.canonical_id == row.id,
                                                       Observation.is_current.is_(True))):
        s = of_observation(settings, o, ceiling, grouped=grouped)
        counts[s] += 1
        if s == "public" and public_origin is None:
            public_origin = o.origin_node or ""
    for a in session.scalars(select(Artwork).where(Artwork.canonical_id == row.id)):
        counts[of_artwork(settings, a, ceiling, grouped=grouped)] += 1
    for a in session.scalars(select(Achievement).where(Achievement.canonical_id == row.id,
                                                       Achievement.retired_at.is_(None))):
        counts[of_achievement(settings, a, ceiling, grouped=grouped)] += 1
    present = [s for s in SCOPES if counts[s]]
    widest = min(present, key=lambda s: WIDTH[s]) if present else (ceiling or "local")
    kind = public_kind(settings, public_origin or None, session) if widest == "public" else None
    parts = [badge(widest, kind)["label"]]
    if counts["group"] and widest != "group":
        parts.append(f"{counts['group']} item{'s' if counts['group'] != 1 else ''} from your nodes")
    if counts["local"] and widest != "local":
        parts.append(f"{counts['local']} on this node only")
    if counts["private"] and widest != "private":
        parts.append(f"{counts['private']} only for one account")
    return {"ceiling": ceiling, "ceiling_by": row.scope_ceiling_by, "widest": widest, "counts": counts,
            "badge": badge(widest, kind), "line": " · ".join(parts),
            "edit_goes": EDIT_GOES["local" if ceiling in CEILINGS else
                                    ("public" if getattr(settings, "role", "") == "authority" else "local")]}


# ---- the projection (§5.7) ----------------------------------------------------------------------------------------

def _strip_payload(payload: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """A group observation's payload limited to the public field categories (D1)."""
    out: dict[str, Any] = {}
    fields = {f for c in allowed for f in CATEGORIES[c][1]}
    if "title" in fields and payload.get("title"):
        out["title"] = payload["title"]
    if "release" in allowed:
        for key in ("release_date", "ext", "edition"):
            if key in payload:
                out[key] = payload[key]
    if "platforms" in allowed:
        for key in ("platform", "platforms"):
            if key in payload:
                out[key] = payload[key]
    if "external_ids" in allowed and payload.get("provider_ids"):
        out["provider_ids"] = payload["provider_ids"]
    facts = payload.get("facts") or {}
    kept = {k: v for k, v in facts.items() if k in fields}
    if kept:
        out["facts"] = kept
    return out


def _scrub(explain: dict[str, Any], hidden_ids: set[str]) -> dict[str, Any]:
    """Explain for a projection: no id of a group observation (no helper can be named or counted)."""
    out: dict[str, Any] = {}
    for name, info in explain.items():
        if not isinstance(info, dict):
            out[name] = info
            continue
        info = dict(info)
        if info.get("observation_id") in hidden_ids:
            info.pop("observation_id")
            info.pop("origin_node", None)
        if isinstance(info.get("runners_up"), list):
            info["runners_up"] = [i for i in info["runners_up"] if i not in hidden_ids]
        info.pop("agree", None)
        info.pop("disagree", None)
        if "members" in info and isinstance(info["members"], dict):
            info.pop("members")
        out[name] = info
    return out


def needs_projection(session: Session, row: Canonical) -> bool:
    if row.scope_ceiling in ("group", "private"):
        return True
    return session.scalars(select(Observation.id).where(
        Observation.canonical_id == row.id, Observation.is_current.is_(True), hidden_obs_clause()).limit(1)
    ).first() is not None


def reproject(session: Session, ctx: Any, cid: str) -> None:
    """Recompute the projection rows of one record (called by every `service.rebuild`, by a ceiling change and by
    the category setting). A record without hidden items keeps no row."""
    from . import provenance

    session.flush()
    session.execute(delete(ScopeProjection).where(ScopeProjection.canonical_id == cid))
    row = session.get(Canonical, cid)
    if row is None or row.merged_into is not None or not needs_projection(session, row):
        session.flush()
        return
    ceiling = row.scope_ceiling
    if ceiling == "private":  # no writer yet (H70, C14-5): never shown to anyone but its account
        for tier in TIERS:
            session.add(ScopeProjection(canonical_id=cid, tier=tier, present=False, view={}, explain={}))
        session.flush()
        return
    settings = ctx.settings
    rows = session.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                     Observation.is_current.is_(True))).all()
    authorities = provenance.authority_origins(session, settings)
    locks = {lk.field: (CLEARED if lk.cleared else lk.value)
             for lk in session.scalars(select(Lock).where(Lock.canonical_id == cid))}
    base: list[Obs] = []
    grouped: list[Obs] = []
    for o in rows:
        if is_host_obs(o):
            continue
        (grouped if is_group_obs(o, ceiling) else base).append(provenance.obs_of(settings, o, authorities))
    hidden_ids = {o.id for o in grouped}
    allowed = public_categories(settings)
    for tier in TIERS:
        visible = list(base)
        if tier == ANON_ON:
            visible += [dataclasses.replace(o, payload=_strip_payload(o.payload, allowed)) for o in grouped]
            visible = [o for o in visible if o.payload or o.id not in hidden_ids]
        present = bool(visible)
        view: dict[str, Any] = {}
        explain: dict[str, Any] = {}
        if present:
            view, explain = merge(visible, locks, prefs=settings.field_source_preference,
                                  meta=provenance.meta(settings, authorities))
            explain = _scrub(explain, hidden_ids)
            if tier == ANON_ON and grouped and "external_ids" not in allowed:
                # a group observation's own identity claim is an external id too: keep only the base claims
                own = merge(base, {}, prefs=settings.field_source_preference)[0].get("provider_ids") if base else None
                if own:
                    view["provider_ids"] = own
                else:
                    view.pop("provider_ids", None)
        cols = dates.columns(view.get("first_release"))
        session.add(ScopeProjection(canonical_id=cid, tier=tier, present=present, view=view, explain=explain,
                                    norm_title=norm_title(view.get("title")) if present else "", **cols))
    session.flush()


def schedule_reproject(session: Session) -> None:
    if session.get(NodeState, PENDING) is None:
        session.add(NodeState(key=PENDING, value=""))
    session.execute(delete(NodeState).where(NodeState.key == CURSOR))
    session.flush()


def _candidates(session: Session, after: str, limit: int) -> list[str]:
    hidden = select(Observation.canonical_id).where(Observation.is_current.is_(True), hidden_obs_clause())
    with_rows = select(ScopeProjection.canonical_id)
    return list(session.scalars(select(Canonical.id).where(
        Canonical.id > after, Canonical.merged_into.is_(None),
        or_(Canonical.scope_ceiling.is_not(None), Canonical.id.in_(hidden), Canonical.id.in_(with_rows)))
        .order_by(Canonical.id).limit(limit)))


def reproject_step(session: Session, ctx: Any, *, limit: int = 200) -> dict[str, Any] | None:
    """One resumable batch of the projection rebuild, or None when none is pending."""
    if session.get(NodeState, PENDING) is None:
        return None
    from .service import write_lock

    write_lock(session)  # `service.rebuild` rewrites the same rows inside ingest/edit transactions (same lock)
    state = session.get(NodeState, CURSOR)
    after = state.value if state is not None else ""
    ids = _candidates(session, after, limit)
    for cid in ids:
        reproject(session, ctx, cid)
    if len(ids) < limit:
        session.execute(delete(NodeState).where(NodeState.key.in_((PENDING, CURSOR))))
        done = True
    else:
        if state is None:
            session.add(NodeState(key=CURSOR, value=ids[-1]))
        else:
            state.value = ids[-1]
        done = False
    session.flush()
    return {"examined": len(ids), "finished": done}


def reproject_pending(maker: Any, ctx: Any, *, max_batches: int = 50) -> int:
    """Run pending batches (the housekeeping loop and `atlas scope reproject`). Returns the records examined."""
    total = 0
    for _ in range(max_batches):
        with maker() as s:
            out = reproject_step(s, ctx)
            s.commit()
        if out is None:
            break
        total += out["examined"]
        if out["finished"]:
            break
    return total


def pending(session: Session) -> bool:
    return session.get(NodeState, PENDING) is not None


# ---- reading through the projection (the SQL-level rule) ----------------------------------------------------------

class Projected:
    """A record as a viewer without `scope.detail` sees it: the projection's view and search columns over the
    record's other attributes. Read-only; never added to a session."""

    __slots__ = ("_row", "view", "explain", "norm_title", "year", "release_start", "release_end", "release_precision")

    def __init__(self, row: Canonical, p: ScopeProjection):
        self._row = row
        self.view, self.explain, self.norm_title = p.view or {}, p.explain or {}, p.norm_title
        self.year, self.release_start, self.release_end = p.year, p.release_start, p.release_end
        self.release_precision = p.release_precision

    def __getattr__(self, name: str) -> Any:
        return getattr(self._row, name)

    @property
    def record(self) -> Canonical:
        return self._row


def unwrap(row: Any) -> Canonical:
    return row.record if isinstance(row, Projected) else row


class Columns:
    """The search columns a query should read for a viewer: the record's own, or the projection's."""

    def __init__(self, proj: Any = None):
        self.proj = proj
        c = Canonical
        if proj is None:
            self.norm_title, self.release_start, self.release_end = c.norm_title, c.release_start, c.release_end
            self.release_precision, self.year = c.release_precision, c.year
        else:
            self.norm_title = _pick(proj, proj.norm_title, c.norm_title)
            self.release_start = _pick(proj, proj.release_start, c.release_start)
            self.release_end = _pick(proj, proj.release_end, c.release_end)
            self.release_precision = _pick(proj, proj.release_precision, c.release_precision)
            self.year = _pick(proj, proj.year, c.year)


def _pick(proj: Any, projected: Any, own: Any) -> Any:
    """The projection's column where the record has a projection row (even when that column is NULL), else the
    record's own: COALESCE would wrongly fall back to the full record for an unknown projected date."""
    return case((proj.canonical_id.is_not(None), projected), else_=own)


def project(stmt: Any, viewer: Viewer | None, *, with_row: bool = True) -> tuple[Any, Columns]:
    """Apply the visibility rule to a `select(Canonical, ...)` statement, in SQL, before any filter, order, limit
    or count the caller adds. Returns the statement (which then also selects the projection row, possibly NULL)
    and the columns to filter and sort on."""
    if viewer is None:
        return stmt, Columns()
    if viewer.detail:
        if not viewer.admin:
            stmt = stmt.where(or_(Canonical.scope_ceiling.is_(None), Canonical.scope_ceiling != "private"))
        return stmt, Columns()
    proj = aliased(ScopeProjection)
    if with_row:  # `fetch` needs the projection row; a count or group-by query does not
        stmt = stmt.add_columns(proj)
    stmt = stmt.outerjoin(proj, and_(proj.canonical_id == Canonical.id, proj.tier == viewer.tier))
    stmt = stmt.where(or_(proj.canonical_id.is_(None), proj.present.is_(True)))
    return stmt, Columns(proj)


def fetch(session: Session, stmt: Any, viewer: Viewer | None) -> list[Any]:
    """Run a `project`ed `select(Canonical)` and return the records as the viewer sees them."""
    result = session.execute(stmt)
    if viewer is None or viewer.detail:
        return [r[0] for r in result]
    return [Projected(r[0], r[-1]) if r[-1] is not None else r[0] for r in result]


def hidden_ids(viewer: Viewer | None) -> Any:
    """A subquery of the record ids this viewer may not see (for relations, lists and media), or None."""
    if viewer is None or (viewer.detail and viewer.admin):
        return None
    if viewer.detail:
        return select(Canonical.id).where(Canonical.scope_ceiling == "private")
    return select(ScopeProjection.canonical_id).where(ScopeProjection.tier == viewer.tier,
                                                      ScopeProjection.present.is_(False))


def visible(session: Session, viewer: Viewer | None, row: Canonical) -> Any:
    """The record as `viewer` sees it, or None when it does not exist for them."""
    if viewer is None or (viewer.detail and viewer.admin):
        return row
    if viewer.detail:
        return None if row.scope_ceiling == "private" else row
    p = session.get(ScopeProjection, (row.id, viewer.tier))
    if p is None:
        return row
    return Projected(row, p) if p.present else None


def get_visible(session: Session, viewer: Viewer | None, cid: str) -> tuple[Any, str | None]:
    """`service.get_canonical` under the visibility rule: a refused record raises exactly the error of an unknown
    one (same status, same text), so a 404 never tells that something exists."""
    from .service import AtlasError, get_canonical

    row, redirected = get_canonical(session, cid)
    shown = visible(session, viewer, row)
    if shown is None:
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    return shown, redirected


def titles(session: Session, viewer: Viewer | None, ids: list[str]) -> dict[str, dict[str, Any]]:
    """`{id: {"title", "media_type"}}` of the records the viewer may see (relations, lists)."""
    out: dict[str, dict[str, Any]] = {}
    if not ids:
        return out
    stmt, _cols = project(select(Canonical).where(Canonical.id.in_(sorted(set(ids)))), viewer)
    for row in fetch(session, stmt, viewer):
        out[row.id] = {"title": (row.view or {}).get("title"), "media_type": row.media_type}
    return out


# ---- facets: artwork, achievements, custom values, relations --------------------------------------------------------

def artwork_clause(viewer: Viewer | None, settings: Any) -> Any:
    """A SQL condition on `Artwork` for the rows this viewer may see, or None for all of them."""
    if viewer is None or viewer.detail:
        return None
    group_row = or_(func.coalesce(Artwork.fetched_from, "").like(GROUP_SCOPE_PREFIX + "%"),  # NULL: own fetch
                    Artwork.canonical_id.in_(select(Canonical.id).where(Canonical.scope_ceiling == "group")))
    hidden = hidden_ids(viewer)
    base = Artwork.canonical_id.not_in(hidden) if hidden is not None else None
    if viewer.switch:
        allowed = public_categories(settings)
        kinds = [k for k in (*COVER_KINDS, "screenshot") if art_category(k) in allowed]
        rule = or_(not_(group_row), Artwork.kind.in_(kinds)) if kinds else not_(group_row)
    else:
        rule = not_(group_row)
    return and_(base, rule) if base is not None else rule


def achievements_visible(session: Session, viewer: Viewer | None, settings: Any, cid: str) -> bool:
    """Achievement rows are received from the group only on a `group`-ceiling record in this release (C14-2 adds
    the per-row evidence); such a list is shown to viewers without `scope.detail` only while the switch is on and
    `achievements` is a public category (withheld by default, D1)."""
    if viewer is None or viewer.detail:
        return True
    if ceiling_of(session, cid) != "group":
        return True
    return viewer.switch and "achievements" in public_categories(settings)


def achievement_clause(viewer: Viewer | None, settings: Any) -> Any:
    """A SQL condition on `Achievement` for the rows this viewer may see, or None for all of them: a row stored
    under a group source (`atlas-group`, `group:<member>`) is withheld like the record-level rule above (second
    review; C14-2 adds the per-row evidence for rows received under a provider's name)."""
    if viewer is None or viewer.detail or (viewer.switch and "achievements" in public_categories(settings)):
        return None
    return not_(or_(Achievement.source == GROUP_SOURCE, Achievement.source.like(GROUP_SCOPE_PREFIX + "%")))


def custom_visible(viewer: Viewer | None, settings: Any, source: str) -> bool:
    if viewer is None or viewer.detail or not _group_source(source):
        return True
    return viewer.switch and "details" in public_categories(settings)


def relation_clause(viewer: Viewer | None, settings: Any) -> Any:
    """A SQL condition on `Relation`: both ends visible, and a relation received from the group only while the
    switch is on and `series` is a public category."""
    if viewer is None or (viewer.detail and viewer.admin):
        return None
    hidden = hidden_ids(viewer)
    cond = and_(Relation.from_id.not_in(hidden), Relation.to_id.not_in(hidden)) if hidden is not None else None
    if not viewer.detail and not (viewer.switch and "series" in public_categories(settings)):
        group_rel = or_(Relation.source == GROUP_SOURCE, Relation.source.like(GROUP_SCOPE_PREFIX + "%"))
        cond = and_(cond, not_(group_rel)) if cond is not None else not_(group_rel)
    return cond


def media_access(session: Session, viewer: Viewer | None, settings: Any, digest: str) -> str | None:
    """Who may fetch the bytes `/media/<digest>` (§5.7): `public` (today's headers), `group` (only group rows
    reference it and the viewer may see one: `private` headers, never `immutable`), or None (refused: the same 404
    as an unknown digest). Bytes no artwork or achievement row references (the site logo, an upload) keep
    today's rule."""
    art = list(session.scalars(select(Artwork).where(Artwork.blob_hash == digest)))
    icons = list(session.scalars(select(Achievement).where(
        or_(Achievement.icon_blob_hash == digest, Achievement.locked_icon_blob_hash == digest))))
    if not art and not icons:
        return "public"
    clause = artwork_clause(viewer, settings)
    ceilings = {cid: ceiling_of(session, cid) for cid in {a.canonical_id for a in art} | {i.canonical_id for i in icons}}
    hidden = set(session.scalars(hidden_ids(viewer))) if hidden_ids(viewer) is not None else set()
    shown_art = art if clause is None else list(session.scalars(select(Artwork).where(Artwork.blob_hash == digest,
                                                                                         clause)))
    # Bytes this node fetched itself are content-addressed and carry no host data: they stay public even when the
    # record they belong to is hidden (the Tracking App host downloads them without a token, C4 artwork flow).
    if any(not is_group_art(a, ceilings.get(a.canonical_id)) for a in art):
        return "public"
    if any(ceilings.get(i.canonical_id) not in ("group", "private") for i in icons):
        return "public"
    row_ok = achievement_clause(viewer, settings) is None
    group_icons = [i for i in icons if i.canonical_id not in hidden and ceilings.get(i.canonical_id) == "group"
                   and achievements_visible(session, viewer, settings, i.canonical_id)
                   and (row_ok or not _group_source(i.source))]
    if shown_art or group_icons:
        return "group"
    return None


# ---- ceilings (§5.3) ----------------------------------------------------------------------------------------------

class CeilingRefused(ValueError):
    pass


def set_ceiling(session: Session, ctx: Any, cid: str, ceiling: str, *, by: str = "operator",
                actor: str | None = None) -> Canonical:
    """Narrow a record (`Keep on this node only` sets `local`). A ceiling cannot un-publish: a record this feed
    already introduced is refused."""
    from .accounts import audit
    from .service import AtlasError

    if ceiling not in ("group", "local"):  # `private` has no writer before C14-5 (H70)
        raise CeilingRefused("the ceiling must be group or local")
    row = session.get(Canonical, cid)
    if row is None or row.merged_into is not None:
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    if session.get(FeedIntroduced, cid) is not None:  # introduced, or (legacy) already named by a signed entry
        raise CeilingRefused("This game is already in your public feed; followers keep what they have.")
    if row.scope_ceiling is not None and WIDTH[row.scope_ceiling] >= WIDTH[ceiling]:
        return row  # a ceiling only narrows
    row.scope_ceiling, row.scope_ceiling_by = ceiling, by
    session.flush()
    reproject(session, ctx, cid)
    audit(session, actor, "scope_ceiling_set", cid, {"ceiling": ceiling, "by": by})
    return row


def publish(session: Session, ctx: Any, cid: str, *, actor: str | None = None) -> int | None:
    """Lift the ceiling (the only way to widen, §5.3) and, on an authority, introduce the record on the feed."""
    from . import feed
    from .accounts import audit
    from .service import AtlasError

    row = session.get(Canonical, cid)
    if row is None or row.merged_into is not None:
        raise AtlasError(f"unknown canonical {cid}", "not_found")
    if row.scope_ceiling is None:
        return None
    was = row.scope_ceiling
    row.scope_ceiling, row.scope_ceiling_by = None, None
    session.flush()
    reproject(session, ctx, cid)
    audit(session, actor, "scope_published", cid, {"was": was})
    if getattr(ctx.settings, "role", "") == "authority":
        return feed.ensure_introduced(session, ctx, cid, "publish")
    return None


def ceiling_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Canonical.scope_ceiling, func.count()).where(
        Canonical.merged_into.is_(None), Canonical.scope_ceiling.is_not(None)).group_by(Canonical.scope_ceiling))
    return {c: n for c, n in rows}


def has_filter(viewer: Viewer | None, column: Any, has: str | None, scope_filter: str | None) -> list[Any]:
    """`has=group|local` and `scope=public|group|local|private` (§5.5) as SQL conditions on `Canonical.id`; only
    for viewers with `scope.detail`. For anyone else an unknown or refused filter is ignored (no effect, no
    error), so the answer cannot tell that group data exists."""
    if viewer is None or not viewer.detail:
        return []
    conds: list[Any] = []
    group_cids = select(Observation.canonical_id).where(Observation.is_current.is_(True), group_obs_clause())
    host_cids = select(Observation.canonical_id).where(Observation.is_current.is_(True),
                                                       Observation.source_kind.in_(HOST_KINDS))
    if has == "group":
        conds.append(or_(Canonical.scope_ceiling == "group", column.in_(group_cids)))
    elif has == "local":
        conds.append(or_(Canonical.scope_ceiling == "local", column.in_(host_cids)))
    if scope_filter in ("group", "local", "private"):
        conds.append(Canonical.scope_ceiling == scope_filter)
    elif scope_filter == "public":
        public = select(Observation.canonical_id).where(Observation.is_current.is_(True), or_(
            Observation.origin_node.is_not(None), Observation.redistributable.is_(True)))
        conds += [Canonical.scope_ceiling.is_(None), column.in_(public)]
    return conds
