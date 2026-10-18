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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .ids import ulid


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Canonical(Base):
    __tablename__ = "canonical"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
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


class SyncCursor(Base):
    __tablename__ = "sync_cursor"

    upstream: Mapped[str] = mapped_column(String(64), primary_key=True)  # upstream node id
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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
