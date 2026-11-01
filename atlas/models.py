"""Storage model.

Observations are the source of truth; canonical records are derived from them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import achievement_key
from .ids import ulid


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Canonical(Base):
    __tablename__ = "canonical"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(32), default="game", server_default="game", index=True)
    norm_title: Mapped[str] = mapped_column(String(500), default="", index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    view: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explain: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Set when merged into another record; reads follow the redirect.
    merged_into: Mapped[str | None] = mapped_column(ForeignKey("canonical.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Observation(Base):
    __tablename__ = "observation"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    canonical_id: Mapped[str | None] = mapped_column(ForeignKey("canonical.id"), nullable=True, index=True)
    source_kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(128))
    source_scope: Mapped[str] = mapped_column(String(512))
    external_id: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_updated_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Only the newest observation per (source, scope, external_id) feeds the merge.
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    # False => stays on this node (provider terms, local overlay, private contribution).
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False)
    # Where it came from when received over the change feed.
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_obs_key", "source", "source_scope", "external_id", "is_current"),
        UniqueConstraint("origin_node", "origin_seq", name="uq_obs_origin"),
    )


class Crosswalk(Base):
    __tablename__ = "crosswalk"

    namespace: Mapped[str] = mapped_column(String(50), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), index=True)


class Lock(Base):
    """A curator-pinned field value; always beats merged values."""

    __tablename__ = "field_lock"

    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), primary_key=True)
    field: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    set_by: Mapped[str] = mapped_column(String(128), default="")
    local_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewItem(Base):
    __tablename__ = "review_item"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    kind: Mapped[str] = mapped_column(String(32))  # possible_duplicate | id_conflict
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), index=True)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|merged|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ChangeLog(Base):
    """This node's own append-only, signed feed."""

    __tablename__ = "change_log"

    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClientChange(Base):
    """This node's local, unsigned change log for its own API clients (C3).

    One row per client-visible version bump, written only by `service.bump_version`. It holds ids, versions
    and a cause, never record content, and it is never federated, relayed or copied into a recovery candidate:
    nothing that builds or serves a signed feed reads this table."""

    __tablename__ = "client_change"

    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    canonical_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    cause: Mapped[str] = mapped_column(String(16))  # feed | replicated | local | provider | merge | detach
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    # sqlite_autoincrement: SQLite never reuses a seq, even if the newest rows were ever deleted.
    __table_args__ = (Index("ix_client_change_cid", "canonical_id", "seq"), {"sqlite_autoincrement": True})


class SyncCursor(Base):
    __tablename__ = "sync_cursor"

    upstream: Mapped[str] = mapped_column(String(64), primary_key=True)  # upstream node id
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SyncFailure(Base):
    """A mirrored feed entry that could not be applied, kept so it can be retried and reported.

    The signed entry itself stays in `replicated_change`; this row only says it still needs applying."""

    __tablename__ = "sync_failure"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # parked: needs a newer release; blocks the applied head (C1.4). quarantined: a known entry that
    # failed validation or apply; does not block, retried automatically.
    disposition: Mapped[str] = mapped_column(String(16), default="quarantined", server_default="quarantined")
    # unsupported_kind|_version|_capability|_field|_value, or invalid|apply_error|skipped
    reason: Mapped[str] = mapped_column(String(32), default="apply_error", server_default="apply_error")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # {kind, _v, unknown, requires}

    __table_args__ = (Index("ix_sync_failure_open", "resolved_at", "node_id", "seq"),
                      Index("ix_sync_failure_parked", "node_id", "disposition", "seq"))


class ReplicatedFeed(Base):
    """Pinned original feed identity, retained so mirrors can relay without re-signing."""

    __tablename__ = "replicated_feed"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(100))


class ReplicatedChange(Base):
    __tablename__ = "replicated_change"

    node_id: Mapped[str] = mapped_column(ForeignKey("replicated_feed.node_id"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # wide enough to retain (and park) a newer release's kind
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NodeState(Base):
    """Key/value node metadata (node id, signing key)."""

    __tablename__ = "node_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Token(Base):
    __tablename__ = "api_token"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"), nullable=True, index=True)


class RateCounter(Base):
    __tablename__ = "rate_counter"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class ProviderCache(Base):
    __tablename__ = "provider_cache"

    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    payload: Mapped[Any] = mapped_column(JSON, nullable=True)
    negative: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Artwork(Base):
    """An image reference. Provider URLs are cached as bytes on demand; only curated art federates."""

    __tablename__ = "artwork"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), index=True)
    source: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(String(1000))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False)
    blob_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # the community's chosen default for its kind on this game (set through a reviewed proposal); local to this node
    preferred: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("canonical_id", "kind", "url", name="uq_artwork"),)


class ArtworkVote(Base):
    """A member's up or down vote on one image. Advisory: it never changes what is shown by itself."""

    __tablename__ = "artwork_vote"

    artwork_id: Mapped[str] = mapped_column(ForeignKey("artwork.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True, index=True)
    value: Mapped[int] = mapped_column(Integer)  # 1 or -1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Achievement(Base):
    __tablename__ = "achievement"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), index=True)
    source: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(200), default="")
    name: Mapped[str] = mapped_column(String(200))
    norm_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    icon_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    global_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # progression | missable | win_condition
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True)  # bronze | silver | gold
    points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locked_icon_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # cached, verified icon bytes (content-addressed, same store and rules as artwork)
    icon_blob_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    icon_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    icon_next_try_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_icon_blob_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    locked_icon_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_icon_next_try_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)  # order chosen by people (curator/community)
    # stable wire key (CONTRACTS-v1 C2): "ak1:" + 26 base32 characters of sha256(identity); stored, never re-derived
    key: Mapped[str] = mapped_column(String(30), default=achievement_key.default_key)

    __table_args__ = (UniqueConstraint("canonical_id", "source", "norm_name", name="uq_achievement"),
                      Index("uq_achievement_key", "canonical_id", "key", unique=True))


class User(Base):
    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | curator | admin
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | suspended
    reputation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bio: Mapped[str] = mapped_column(String(500), default="", server_default="")
    profile_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    email_notifications: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # opt-in
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oidc_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie secret
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserMfa(Base):
    __tablename__ = "user_mfa"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    secret_enc: Mapped[str] = mapped_column(Text)
    setup_session_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recovery_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Proposal(Base):
    """A community change awaiting review (or already applied by a trusted user, and revertable)."""

    __tablename__ = "proposal"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    kind: Mapped[str] = mapped_column(String(16))  # edit | new_game | artwork | achievements
    canonical_id: Mapped[str | None] = mapped_column(ForeignKey("canonical.id"), nullable=True, index=True)
    proposer_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    previous: Mapped[Any] = mapped_column(JSON, nullable=True)  # state before applying, for revert
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | approved | rejected | withdrawn | reverted
    auto_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReputationEvent(Base):
    __tablename__ = "reputation_event"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    proposal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditEntry(Base):
    __tablename__ = "audit_entry"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    actor_id: Mapped[str | None] = mapped_column(String(26), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Setting(Base):
    """Admin-editable overrides of the environment defaults (see atlas/runtime.py)."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Page(Base):
    """An admin-written Markdown page (About, Rules, Help...)."""

    __tablename__ = "page"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    in_nav: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=100)
    updated_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class EmailToken(Base):
    """One-time link secrets (stored hashed): address verification and password reset."""

    __tablename__ = "email_token"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the secret
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # verify | reset
    email: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Outbox(Base):
    """Every email Atlas sends, queued here so requests never wait on (or leak the timing of) SMTP."""

    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)  # cleared after delivery when really sent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class MediaType(Base):
    """A kind of thing Atlas describes (game now; movie, anime... by configuration)."""

    __tablename__ = "media_type"

    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    labels: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # core field -> label, e.g. developer: Director
    origin: Mapped[str] = mapped_column(String(8), default="local")  # local | upstream


class FieldDef(Base):
    __tablename__ = "field_def"

    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    media_type: Mapped[str] = mapped_column(ForeignKey("media_type.key"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(12))  # text | longtext | number | date | bool | choice | list | url
    choices: Mapped[list[str]] = mapped_column(JSON, default=list)
    help: Mapped[str] = mapped_column(String(300), default="")
    public: Mapped[bool] = mapped_column(Boolean, default=True)  # shown on the page and in the API
    editable: Mapped[bool] = mapped_column(Boolean, default=True)  # community may propose changes
    position: Mapped[int] = mapped_column(Integer, default=100)
    origin: Mapped[str] = mapped_column(String(8), default="local")

    __table_args__ = (UniqueConstraint("media_type", "key", name="uq_field_def"),)


class CustomValue(Base):
    __tablename__ = "custom_value"

    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class UserList(Base):
    __tablename__ = "user_list"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(12), default="custom")  # favourites | wishlist | custom
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_list_name"),)


class ListItem(Base):
    __tablename__ = "list_item"

    list_id: Mapped[str] = mapped_column(ForeignKey("user_list.id"), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), primary_key=True, index=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Follow(Base):
    __tablename__ = "follow"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(8), primary_key=True)  # game | user
    target_id: Mapped[str] = mapped_column(String(40), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    key: Mapped[str] = mapped_column(String(80), default="")  # collapses repeats about the same thing
    title: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(300), default="")
    count: Mapped[int] = mapped_column(Integer, default=1)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Comment(Base):
    __tablename__ = "comment"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    canonical_id: Mapped[str | None] = mapped_column(ForeignKey("canonical.id"), nullable=True, index=True)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposal.id"), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="visible")  # visible | hidden | deleted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Report(Base):
    __tablename__ = "report"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    comment_id: Mapped[str] = mapped_column(ForeignKey("comment.id"), index=True)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open | actioned | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (UniqueConstraint("comment_id", "reporter_id", name="uq_report"),)


class Peer(Base):
    """An upstream node this node replicates from, with its identity pinned by the administrator."""

    __tablename__ = "peer"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    name: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(300))
    node_id: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(String(100))
    token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    remote_protocol: Mapped[str | None] = mapped_column(String(16), nullable=True)  # last advertised by the peer


class FollowerSeen(Base):
    """Which federation protocol each follower credential last spoke (its `Atlas-Protocol` header).

    Lets an authority see who still needs an upgrade before raising its emit protocol. Holds the
    token id only: no address, user agent or other personal data."""

    __tablename__ = "follower_seen"

    token_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(16))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OidcClient(Base):
    """Provider side: a relying party (another node) allowed to sign users in through this node."""

    __tablename__ = "oidc_client"

    client_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    secret_hash: Mapped[str] = mapped_column(String(64))
    redirect_uris: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Confidential clients (which authenticate with a secret) may be allowed to skip PKCE for interoperability.
    require_pkce: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # RS256 works with nearly every application; EdDSA is for other Atlas nodes (which pin the node key).
    id_token_alg: Mapped[str] = mapped_column(String(8), default="RS256", server_default="RS256")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OidcCode(Base):
    __tablename__ = "oidc_code"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the code
    client_id: Mapped[str] = mapped_column(ForeignKey("oidc_client.client_id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    redirect_uri: Mapped[str] = mapped_column(String(500))
    challenge: Mapped[str] = mapped_column(String(64), default="")  # empty: the client did not use PKCE
    nonce: Mapped[str] = mapped_column(String(200), default="")
    scope: Mapped[str] = mapped_column(String(100), default="openid")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OidcAccess(Base):
    __tablename__ = "oidc_access"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the access token
    client_id: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    scope: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OidcIssuer(Base):
    """Relying-party side: an identity provider (another Atlas node) whose logins this node accepts."""

    __tablename__ = "oidc_issuer"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    name: Mapped[str] = mapped_column(String(80))
    issuer_url: Mapped[str] = mapped_column(String(300), unique=True)
    client_id: Mapped[str] = mapped_column(String(40))
    client_secret_enc: Mapped[str] = mapped_column(Text)
    node_id: Mapped[str] = mapped_column(String(64))  # pinned signing key of the issuer
    public_key: Mapped[str] = mapped_column(String(100))
    authorization_endpoint: Mapped[str] = mapped_column(String(400))
    token_endpoint: Mapped[str] = mapped_column(String(400))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_create: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_all_users: Mapped[bool] = mapped_column(Boolean, default=False)
    trust_email: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OidcState(Base):
    __tablename__ = "oidc_state"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the state value
    issuer_id: Mapped[str] = mapped_column(ForeignKey("oidc_issuer.id"))
    nonce: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(String(100))
    next_url: Mapped[str] = mapped_column(String(300), default="/")
    link_user_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    binding: Mapped[str] = mapped_column(String(64), default="", server_default="")  # hash of the browser cookie
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OidcLink(Base):
    __tablename__ = "oidc_link"

    issuer_id: Mapped[str] = mapped_column(ForeignKey("oidc_issuer.id"), primary_key=True)
    subject: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# ---- owner federation groups (CONTRACT-C10 §9.1, migration 0028) -----------------------------------------

class OwnerGroup(Base):
    """This node's owner group. One group per node in v1 (D10.5); keys and invite secrets are `enc:v1:`."""

    __tablename__ = "owner_group"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    genesis: Mapped[dict[str, Any]] = mapped_column(JSON)
    succession: Mapped[list[Any]] = mapped_column(JSON)  # active successions, in order
    root_key_id: Mapped[str] = mapped_column(String(16))
    recovery: Mapped[dict[str, Any]] = mapped_column(JSON)  # the currently pinned {threshold, keys}
    root_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)  # enc:v1:<fernet(passphrase-wrapped G)>
    box_sealed: Mapped[str] = mapped_column(Text)  # enc:v1: X25519 private key of this node for this group
    self_serial: Mapped[str | None] = mapped_column(String(40), nullable=True)
    root_state: Mapped[str] = mapped_column(String(10), default="none", server_default="none")  # holder|moved|none
    succession_pending: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # active|resyncing|left|revoked|halted
    state_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    # Additions to the C10 §9.1 sketch (CONTRACT-AMENDMENTS A10): the newest signed root_moved, recovery-quorum
    # cancels kept for relaying, and the box-key rekey window (§1.7: the old key opens responses for 15 minutes).
    root_moved: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    succession_cancels: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    box_previous_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)
    box_previous_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    box_pending_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)  # after rekey-box, until renewed


class GroupCert(Base):
    """Every membership and delegating certificate this node holds, all versions (signed envelopes)."""

    __tablename__ = "group_cert"

    group_id: Mapped[str] = mapped_column(ForeignKey("owner_group.group_id", ondelete="CASCADE"), primary_key=True)
    serial: Mapped[str] = mapped_column(String(40), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(16))
    serial_n: Mapped[int] = mapped_column(BigInteger)
    serial_r: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parent_serial: Mapped[str | None] = mapped_column(String(40), nullable=True)
    authorized_by_grant: Mapped[str | None] = mapped_column(String(40), nullable=True)
    node_id: Mapped[str] = mapped_column(String(16))
    member_rev: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    held_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (Index("ix_group_cert_node", "group_id", "node_id", "not_after"),)


class GroupBannedNode(Base):
    """Node identities banned by a `removed` or `key_compromise` entry; never deleted (§1.6)."""

    __tablename__ = "group_banned_node"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    root_public_key: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(24))
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    crl_serial: Mapped[str] = mapped_column(String(40))


class GroupView(Base):
    """The newest signed freshness view per member (§1.6)."""

    __tablename__ = "group_view"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupMember(Base):
    """Derived current view of each member, rebuilt from group_cert and the revocation index."""

    __tablename__ = "group_member"

    group_id: Mapped[str] = mapped_column(ForeignKey("owner_group.group_id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    root_public_key: Mapped[str] = mapped_column(String(100))
    box_public_key: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(80))
    serial: Mapped[str] = mapped_column(String(40))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    delegate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))  # active|expiring|expired|revoked|left|fork
    rotations: Mapped[list[Any]] = mapped_column(JSON)  # validated key_rotation proofs
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_protocol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class GroupCrl(Base):
    """Every signature-valid CRL seen, forks included, so `state` can serve them again (§1.6)."""

    __tablename__ = "group_crl"

    group_id: Mapped[str] = mapped_column(ForeignKey("owner_group.group_id", ondelete="CASCADE"), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(16), primary_key=True)
    crl_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupRevoked(Base):
    """Append-only revocation index (§1.6): rows are never deleted, only upgraded."""

    __tablename__ = "group_revoked"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    serial: Mapped[str] = mapped_column(String(40), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(24))
    node_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    root_public_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    suspect_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupEnrolment(Base):
    """One enrolment, on the inviter's side or on the joiner's side (§2)."""

    __tablename__ = "group_enrolment"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # enrolment_id (ULID)
    side: Mapped[str] = mapped_column(String(8))  # inviter|joiner
    group_id: Mapped[str] = mapped_column(String(16))
    invite_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256(invite_id)
    secret_enc: Mapped[str] = mapped_column(Text)  # enc:v1: k (joiner: k and its new box key)
    peer_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    peer_node_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON)
    delegate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    transcript: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    transcript_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_j: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nonce_j: Mapped[str | None] = mapped_column(String(44), nullable=True)
    url_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    state: Mapped[str] = mapped_column(String(16))
    # open|hello|revealed|confirmed|issued|activating|active|burned|expired|aborted
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cert_serial: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_group_enrolment_expiry", "expires_at"),)


class GroupNonce(Base):
    """Request nonces seen from members, kept 15 minutes (§3.2 step 8)."""

    __tablename__ = "group_nonce"

    node_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(32), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_group_nonce_seen", "seen_at"),)


# ---- owner-group private channel and profile sync (CONTRACT-C10 §3.4-§4.6, §9.2: migration 0029) ----------------

class GroupStream(Base):
    """One `(origin, epoch, scope)` stream, own streams included (own next seq = hwm + 1 under a row lock)."""

    __tablename__ = "group_stream"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope: Mapped[str] = mapped_column(String(26), primary_key=True)
    hwm: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    key_id: Mapped[str] = mapped_column(String(64))  # the key this epoch is bound to (§1.7)
    key_bound_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # previous.heads[scope] (rotation)
    advertised_head: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # for the expiry grace (§1.5)
    advertised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class GroupChange(Base):
    """Every change entry this node emitted or received, with its disposition (§3.4). `body` is NULL when the
    entry was elided or its body stripped (authorization refusals never keep a value)."""

    __tablename__ = "group_change"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope: Mapped[str] = mapped_column(String(26), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(48))  # the signed field name, or "-"
    body: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    sig: Mapped[str] = mapped_column(String(128))
    hlc: Mapped[str | None] = mapped_column(String(48), nullable=True)  # kept when the body is stripped (§4.4)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_from: Mapped[str | None] = mapped_column(String(16), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    disposition: Mapped[str] = mapped_column(String(12))  # applied|noop|parked|held|elided|rejected
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("ix_group_change_scope", "group_id", "scope", "issued_at"),)


class GroupAccountLink(Base):
    """This node's local user for a group account (§5.1): one each way."""

    __tablename__ = "group_account_link"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    account_uid: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    minted_here: Mapped[bool] = mapped_column(Boolean)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_account_link_user"),)


class GroupAccountNode(Base):
    """Which nodes hold an account, from `account.link` / `account.unlink` entries (§4.3)."""

    __tablename__ = "group_account_node"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    account_uid: Mapped[str] = mapped_column(String(26), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str] = mapped_column(String(10))  # linked|unlinked|suspect
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    entry_ref: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileField(Base):
    """The displayed value of one preference (the materialized head winner, §4.4). NULL value = node default."""

    __tablename__ = "profile_field"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    field: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    hlc: Mapped[str] = mapped_column(String(48))
    origin_node: Mapped[str] = mapped_column(String(16))
    cause: Mapped[str] = mapped_column(String(12))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileVersion(Base):
    """One version of one preference; the versions of a (user, field) form a DAG through `base` (§4.4)."""

    __tablename__ = "profile_version"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    field: Mapped[str] = mapped_column(String(48), primary_key=True)
    hlc: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    cleared: Mapped[bool] = mapped_column(Boolean)
    base: Mapped[list[str]] = mapped_column(JSON)
    cause: Mapped[str] = mapped_column(String(12))
    origin_node: Mapped[str] = mapped_column(String(16))
    entry_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)  # group_change PK; NULL = local only
    is_head: Mapped[bool] = mapped_column(Boolean)
    held_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileSuperseded(Base):
    __tablename__ = "profile_superseded"

    user_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    field: Mapped[str] = mapped_column(String(48), primary_key=True)
    hlc: Mapped[str] = mapped_column(String(48), primary_key=True)
    pruned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileConflict(Base):
    """A review-queue row (§4.6): concurrent|privacy_widen|suspect|suspect_link|missing_base|held."""

    __tablename__ = "profile_conflict"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(48))
    kind: Mapped[str] = mapped_column(String(16))
    winner_hlc: Mapped[str | None] = mapped_column(String(48), nullable=True)
    loser_hlc: Mapped[str | None] = mapped_column(String(48), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(10))  # open|resolved|merged|dismissed
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "field", "kind", "loser_hlc", name="uq_profile_conflict"),)


class ProfileSyncConsent(Base):
    """Per-node consent of one user for one group (§4.3): consent is given where the data lands, never synced."""

    __tablename__ = "profile_sync_consent"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    fields: Mapped[list[str]] = mapped_column(JSON)
    signout_propagation: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupSigninState(Base):
    """A pending link (A11) or sign-in (A12) at the relying party, bound to the `atlas_grp_bind` cookie (§5.3)."""

    __tablename__ = "group_signin_state"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(state) = the assertion's state_hash
    group_id: Mapped[str] = mapped_column(String(16))
    idp_node: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(8))
    nonce: Mapped[str] = mapped_column(String(64))
    binding: Mapped[str] = mapped_column(String(64))  # sha256(atlas_grp_bind)
    link_user_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    next_url: Mapped[str] = mapped_column(String(300))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupWebHandle(Base):
    """POST-parks-then-303-GET handles (§5.3): the decrypted, signature-checked payload, single use."""

    __tablename__ = "group_web_handle"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(handle)
    kind: Mapped[str] = mapped_column(String(10))  # authorize|callback|start (+ consent|linkform, A11 amendment)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupAssertionSeen(Base):
    __tablename__ = "group_assertion_seen"

    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    jti: Mapped[str] = mapped_column(String(26), primary_key=True)
    iss_node: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupIdpConsent(Base):
    """On the asserting node: this user agreed to link (or sign in to) `rp_node`."""

    __tablename__ = "group_idp_consent"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    rp_node: Mapped[str] = mapped_column(String(16), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
