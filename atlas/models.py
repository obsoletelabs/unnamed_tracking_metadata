"""Storage model.

Observations are the source of truth; canonical records are derived from them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    select,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, attributes, mapped_column

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
    # The first release (C5.2) as a period, for sorting and filtering: "2011" is 2011-01-01..2011-12-31 with
    # precision "year". NULL when unknown. Derived from `view["first_release"]` on every rebuild (migration 0031).
    release_start: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    release_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    release_precision: Mapped[str | None] = mapped_column(String(5), nullable=True)
    view: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explain: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Set when merged into another record; reads follow the redirect.
    merged_into: Mapped[str | None] = mapped_column(ForeignKey("canonical.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    # CONTRACT-C14 §5.3 (migration 0038): how far this record may travel at most. NULL: derived from its items;
    # `group`, `private` or `local`. A ceiling only narrows. `scope_ceiling_by`: who set it (`assist`, `operator`).
    scope_ceiling: Mapped[str | None] = mapped_column(String(8), nullable=True)
    scope_ceiling_by: Mapped[str | None] = mapped_column(String(8), nullable=True)


class ScopeProjection(Base):
    """CONTRACT-C14 §5.7 (migration 0038): what a viewer without `scope.detail` sees of one record, kept only for
    records that hold something such a viewer may not see (host/plugin library data, data received from the
    group, a `private` ceiling). A record without a row here looks the same to every viewer.

    `tier` is `anon_off` (`group_data_public_pages` off) or `anon_on` (on: group items limited to the public field
    categories, owner decision D1). `present = false` means the record does not exist for that viewer: it is left
    out of every list, search and count, and its page is the same 404 as an unknown id. The columns mirror the
    `canonical` columns that search, sort and filter read, so the visibility rule is applied inside the SQL query,
    before ranking, pagination and counts."""

    __tablename__ = "scope_projection"

    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id", ondelete="CASCADE"), primary_key=True)
    tier: Mapped[str] = mapped_column(String(8), primary_key=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    view: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explain: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    norm_title: Mapped[str] = mapped_column(String(500), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    release_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    release_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    release_precision: Mapped[str | None] = mapped_column(String(5), nullable=True)

    __table_args__ = (Index("ix_scope_projection_tier", "tier", "present"),)


class UserPermission(Base):
    """A right an administrator granted one account (CONTRACT-C14 §9, migration 0038). Values: `scope.detail`
    (C14-1 §5.7: sees scope badges, counts, filters and group data on this node's pages); `feed.access` follows
    with C14-3m."""

    __tablename__ = "user_permission"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(String(32), primary_key=True)
    granted_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Relation(Base):
    """A directed relation between two works (CONTRACTS-v1 C6.3): `from_id` is the DLC, port, remake or member,
    `to_id` the parent, base work, series or collection. Types are `vocab.RELATION_TYPES`.

    Rows are never deleted: removing a relation sets `ended_at` (and `end_reason`), so history and the feed's
    `end` entries stay explainable. An *active* row (`ended_at IS NULL`) always joins two live records: a merge
    moves it to the survivor or ends it (`atlas.relations.move`)."""

    __tablename__ = "relation"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    from_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), nullable=False)
    to_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(128))
    # True when it may enter this node's signed feed (a curator relation on an authority) or came from one.
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Additions to C6.3 (CONTRACT-AMENDMENTS A15): who added it, why it ended, where it came from over the feed,
    # and the seq of this node's own `add` entry when it was published.
    created_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    feed_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # CONTRACT-C15 §2.4 (A21, migration 0061): a member's place in its series ("1", "2.5", "0"). Only a
    # `series_member` relation carries one; a change of position ends the row and adds a new one (no new op).
    position: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint("from_id <> to_id", name="ck_relation_not_self"),
        Index("uq_relation_active", "from_id", "to_id", "type", unique=True,
              sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),
        Index("ix_relation_from", "from_id", "ended_at"),
        Index("ix_relation_to", "to_id", "ended_at"),
        Index("ix_relation_origin", "origin_node", "origin_seq"),
    )


class PriceObservation(Base):
    """One observed price of one offer in one storefront region (CONTRACTS-v1 C8, `atlas/prices.py`).

    Not an `Observation` and never merged into `Canonical.view`: prices churn, are regional and expire. Money
    is an integer in the currency's ISO 4217 minor units (`amount_minor`, the price a buyer pays now;
    `base_amount_minor`, the list price). Rows are deleted once `purge_after` (`observed_at + retention_days`)
    has passed. `dedup_key` is the offer plus `observed_at`, so a re-sent observation is stored once."""

    __tablename__ = "price_observation"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32))
    product_id: Mapped[str] = mapped_column(String(128))
    package_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    edition: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    region: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discount_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(16))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    license_terms: Mapped[str] = mapped_column(String(64), default="", server_default="")
    retention_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dedup_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_by: Mapped[str] = mapped_column(String(128), default="", server_default="")

    __table_args__ = (
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="ck_price_amount"),
        CheckConstraint("base_amount_minor IS NULL OR base_amount_minor >= 0", name="ck_price_base"),
        CheckConstraint("discount_pct IS NULL OR (discount_pct >= 0 AND discount_pct <= 100)",
                        name="ck_price_discount"),
        CheckConstraint("retention_days >= 1 AND retention_days <= 365", name="ck_price_retention"),
        Index("uq_price_dedup", "dedup_key", unique=True),
        Index("ix_price_latest", "canonical_id", "region", "currency", "observed_at"),
        Index("ix_price_offer", "provider", "product_id", "region"),
        Index("ix_price_purge", "purge_after"),
    )


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
    # C11 §11.9.1: sha256 of the canonical JSON of `payload`; NULL on rows written before migration 0034 (computed
    # lazily on the next re-ingest of the chain). An identical re-ingest only refreshes `last_seen_at`.
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # CONTRACT-C14 §3.4 (migration 0039): the `group_assist_item` (helper-signed bundle or lookup attestation) this
    # observation was written from; NULL for everything else.
    assist_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (
        Index("ix_obs_key", "source", "source_scope", "external_id", "is_current"),
        UniqueConstraint("origin_node", "origin_seq", name="uq_obs_origin"),
    )


class BookCreator(Base):
    """CONTRACT-C15 §5 (A21, migration 0061): a book's ordered creators, derived from `view["authors"]` on every
    rebuild (like `release_start`), so `?author=` can prefix-match `norm_name` (casefold, no diacritics). At most 30
    rows per record; only records of the `book` type have rows."""

    __tablename__ = "book_creator"

    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    norm_name: Mapped[str] = mapped_column(String(200), index=True)
    role: Mapped[str] = mapped_column(String(16), default="author")


class Crosswalk(Base):
    __tablename__ = "crosswalk"

    namespace: Mapped[str] = mapped_column(String(50), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), index=True)
    # C11 §11.2.4: '' for a work key, `edition:<norm edition>` or `platform:<slug>` for a key that names one
    # edition or one platform version of the work. Variant keys never contradict the work's own key.
    variant: Mapped[str] = mapped_column(String(120), default="", server_default="")


class Lock(Base):
    """A curator-pinned field value; always beats merged values."""

    __tablename__ = "field_lock"

    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), primary_key=True)
    field: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    set_by: Mapped[str] = mapped_column(String(128), default="")
    local_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # C11 §11.8 explicit clear: the field is deliberately empty (`value` is NULL). Local only in this release.
    cleared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ReviewItem(Base):
    __tablename__ = "review_item"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    kind: Mapped[str] = mapped_column(String(32))  # possible_duplicate | id_conflict
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), index=True)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # open | merged | dismissed | distinct | superseded | resolved (C11 §11.5)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # C11 §11.5: why it was opened, the decision's evidence, one open item per pair, queue order.
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    pair_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_review_queue", "status", "priority", "created_at"),
        Index("uq_review_open_pair", "pair_key", unique=True,
              sqlite_where=text("status = 'open' AND pair_key IS NOT NULL"),
              postgresql_where=text("status = 'open' AND pair_key IS NOT NULL")),
    )


class ReviewCandidate(Base):
    """C11 §11.9.2: which records a review item names as candidates (`review_item.candidates`, as rows), so a merge,
    unmerge or "different works" decision finds the items it touches with an indexed lookup instead of reading the
    whole open queue. Derived: kept in step by `_sync_review_candidates` whenever an item's candidates change."""

    __tablename__ = "review_candidate"

    review_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(40), primary_key=True)

    __table_args__ = (Index("ix_review_candidate_cid", "canonical_id", "review_id"),)


def candidate_ids(candidates: Any) -> set[str]:
    """The record ids of a `review_item.candidates` value ([[id, score], ...]), bounded."""
    out: set[str] = set()
    for c in (candidates or [])[:100] if isinstance(candidates, list) else []:
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], str) and c[0]:
            out.add(c[0][:40])
    return out


class MatchDecision(Base):
    """C11 §11.4: why one ingested observation landed where it did. Local only: it depends on this node's trust
    configuration and records and quotes provider data, so it is never federated."""

    __tablename__ = "match_decision"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("observation.id", ondelete="SET NULL"),
                                                       nullable=True)
    source: Mapped[str] = mapped_column(String(128))
    source_scope: Mapped[str] = mapped_column(String(512))
    external_id: Mapped[str] = mapped_column(String(256))
    canonical_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))
    tier: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(32))
    score: Mapped[float] = mapped_column(Float)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    merges: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    matcher: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(128), default="auto", server_default="auto")
    review_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (
        Index("ix_match_decision_cid", "canonical_id", "created_at"),
        Index("ix_match_decision_chain", "source", "source_scope", "external_id", "created_at"),
    )


class MatchRejection(Base):
    """C11 §11.6: "these are different works" (`record`: a < b, both canonical ids) or "this source record is not
    that work" (`chain`: a = "chain:" + 32 hex, b = canonical id). Applies to the live records of the stored ids."""

    __tablename__ = "match_rejection"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    scope: Mapped[str] = mapped_column(String(8))
    a: Mapped[str] = mapped_column(String(80))
    b: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    actor: Mapped[str] = mapped_column(String(128))
    local_only: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("uq_match_rejection_active", "scope", "a", "b", unique=True,
              sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),
        Index("ix_match_rejection_b", "b", sqlite_where=text("ended_at IS NULL"),
              postgresql_where=text("ended_at IS NULL")),
    )


class RecordLive(Base):
    """C11 §11.6 alias closure: a merged record id and the live record it redirects to (indexed on `live_id`), so
    rejections, decisions and merge history of merged-in ids are found with one indexed lookup. A record without a
    row is its own live record (amendment A17)."""

    __tablename__ = "record_live"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    live_id: Mapped[str] = mapped_column(String(40), index=True)


class FieldConflict(Base):
    """C11 §11.7.2 conflict index: a row exists while the field's explain says `conflict: true`."""

    __tablename__ = "field_conflict"

    canonical_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    field: Mapped[str] = mapped_column(String(48), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (Index("ix_field_conflict_field", "field", "canonical_id"),)


class MergeRecord(Base):
    """C11 §11.10.1: one merge of `src` into `dst`, written before anything moves, with the snapshot items needed
    to undo it. `feed_seq` is the signed `merge` entry's sequence when this node published the merge."""

    __tablename__ = "merge_record"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    src: Mapped[str] = mapped_column(ForeignKey("canonical.id"))
    dst: Mapped[str] = mapped_column(ForeignKey("canonical.id"))
    cause: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(128), default="")
    decision_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    review_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    feed_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    src_view: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    src_explain: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    late_chains: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("uq_merge_record_live", "src", unique=True, sqlite_where=text("reverted_at IS NULL"),
              postgresql_where=text("reverted_at IS NULL")),
        Index("ix_merge_record_dst", "dst", "created_at"),
    )


class MergeRecordItem(Base):
    """One snapshot item of a merge (§11.10.1 table): what moved (`chain`, `xwalk`, `lock`, `art`, ...) or what was
    deleted (`*_dropped`, with the full row in `data`). `data_hash` detects rows edited on `dst` after the merge."""

    __tablename__ = "merge_record_item"

    merge_id: Mapped[str] = mapped_column(ForeignKey("merge_record.id", ondelete="CASCADE"), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    ref: Mapped[str] = mapped_column(String(900))
    data: Mapped[Any] = mapped_column(JSON, nullable=True)
    data_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_merge_record_item_kind", "kind", "merge_id"),)


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
    # failed validation or apply; does not block, retried automatically with backoff. wait (C13 §5.2): the entry
    # names a record this node has not received yet; it does not block, is not retried on the pull clock, and is
    # applied when that record arrives on the same feed (`sync.wake_waiting`).
    disposition: Mapped[str] = mapped_column(String(16), default="quarantined", server_default="quarantined")
    # unsupported_kind|_version|_capability|_field|_value, or invalid|apply_error|skipped, or (wait)
    # waiting_for_record|dropped_by_previous_release
    reason: Mapped[str] = mapped_column(String(32), default="apply_error", server_default="apply_error")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # {kind, _v, unknown, requires}
    # C13 §5.2: the canonical id a `wait` row waits for (migration 0036)
    waiting_for: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (Index("ix_sync_failure_open", "resolved_at", "node_id", "seq"),
                      Index("ix_sync_failure_parked", "node_id", "disposition", "seq"),
                      Index("ix_sync_failure_waiting", "waiting_for", "resolved_at"))


class FeedIntroduced(Base):
    """C13 §4.1: which records this node's own signed feed has introduced, and by which entry (migration 0036).

    `via` is `obs` (an observation), `record` (the 1.5 kind), `bridge` (an `atlas-record` observation), `detach`
    (the record a detach created) or `legacy` (`legacy_unintroduced`: named by a signed entry while neither the
    bridge nor protocol 1.5 was available, so nothing could introduce it; `seq` is NULL). Authority-side only."""

    __tablename__ = "feed_introduced"

    canonical_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    via: Mapped[str] = mapped_column(String(8))
    because: Mapped[str | None] = mapped_column(String(16), nullable=True)
    introduced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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

    key: Mapped[str] = mapped_column(String(160), primary_key=True)  # C16 D5: some keys embed a 64-hex node id
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
    # C12 §12.5 (migration 0035). `width`/`height` are the measured size once cached (a differing declaration is
    # kept in `declared_*`); `aspect` and `dhash` come from the decoded image; `duplicate_of` names the visible row
    # of a group of rows of one record and kind holding the same bytes (§12.5.4).
    declared_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    declared_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    aspect: Mapped[str | None] = mapped_column(String(12), nullable=True)
    dhash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(26), nullable=True)
    region: Mapped[str | None] = mapped_column(String(8), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(35), nullable=True)
    licence: Mapped[str | None] = mapped_column(String(24), nullable=True)
    attribution: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_page_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # an upload whose licence cannot enter the CC0 feed stays on this node (§12.5.5, H47)
    local_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # C13 §6 (migration 0036). `origin_node`: the feed whose signed `artwork` entry delivered the row (NULL for rows
    # this node wrote or imported itself; only feed rows are ever asked of a peer). `fetched_from`: `local`,
    # `peer:<node_id>`, `origin` or `upload`. `sources_tried`: the sources of the last attempt, in order.
    # `peer_digest` / `digest_provenance`: a sha256 learned for the row and how (`signed`, `peer_reported`, `origin`).
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_from: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sources_tried: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    peer_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    digest_provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (UniqueConstraint("canonical_id", "kind", "url", name="uq_artwork"),
                      Index("ix_artwork_dup", "canonical_id", "kind", "blob_hash"))


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
    # C12 §12.1 (migration 0035). The set (group) the row belongs to inside its source's list; not part of the key.
    set_key: Mapped[str] = mapped_column(String(64), default="base", server_default="base")
    # the item's index in its source's list, always stored (§12.1.3); `position` stays the people-chosen order
    source_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # active | unobtainable (absent: unknown)
    # set when the source stopped listing the row: retired rows keep id and key and revive when the item returns
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    text_locale: Mapped[str | None] = mapped_column(String(35), nullable=True)  # language of name/description
    percent_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)  # when global_percent was measured
    icon_licence: Mapped[str | None] = mapped_column(String(24), nullable=True)
    icon_attribution: Mapped[str | None] = mapped_column(String(300), nullable=True)
    icon_last_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locked_icon_last_error: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # F1 (C12 §12.1.4, amended A18): a row is identified inside its source by external id *and* name, so two
    # achievements that share a name but not an external id are both kept.
    __table_args__ = (Index("uq_achievement_ident", "canonical_id", "source", "external_id", "norm_name", unique=True),
                      Index("uq_achievement_key", "canonical_id", "key", unique=True))


class AchievementSet(Base):
    """A named group of one source's achievements on one record: base game, DLC, edition, platform... (§12.1.1)."""

    __tablename__ = "achievement_set"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"))
    source: Mapped[str] = mapped_column(String(128))
    set_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # no foreign key: the DLC's record may be one this node does not hold (§12.4); resolved at read time
    dlc_canonical_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    edition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    platforms: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    __table_args__ = (Index("uq_achievement_set", "canonical_id", "source", "set_key", unique=True),
                      Index("ix_achievement_set_dlc", "dlc_canonical_id"))


class AchievementText(Base):
    """A translation of one achievement row's name and description (§12.1.7). Never part of its identity."""

    __tablename__ = "achievement_text"

    achievement_id: Mapped[str] = mapped_column(ForeignKey("achievement.id", ondelete="CASCADE"), primary_key=True)
    locale: Mapped[str] = mapped_column(String(35), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")


class AchievementStat(Base):
    """A provider's global unlock percentage fetched apart from its definitions (§12.1.6). Local only, never
    federated; matched to rows by (source, external_id) at read time and never creates a row."""

    __tablename__ = "achievement_stat"

    canonical_id: Mapped[str] = mapped_column(ForeignKey("canonical.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    percent: Mapped[float] = mapped_column(Float)
    as_of: Mapped[date] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (CheckConstraint("percent >= 0 AND percent <= 100", name="ck_achievement_stat_percent"),)


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
    # A12 (CONTRACT-C10 §5.4, §9.3): set only on a session opened by a group sign-in assertion.
    via_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assertion_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    amr: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_user_session_origin", "origin_node"),)


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
    # CONTRACT-C14 §6.6/§7.4 (migration 0040): a proposal that arrived as a node-signed contribution through a link.
    # The node-link columns (migration 0040) are deferred and have no client default, so code that writes or reads
    # proposals on an older schema (a downgrade, an upgrade test seeding old rows) never names them.
    via_link_id: Mapped[str | None] = mapped_column(String(26), nullable=True, index=True, deferred=True)
    contribution_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True, deferred=True)
    stale_base: Mapped[bool] = mapped_column(Boolean, server_default=false(), deferred=True)
    # `link_revoked` / `link_compromised` (§6.7, Review #1 C14-10): never auto-applied; a compromised one needs the
    # reviewer's "I checked this change".
    link_flag: Mapped[str | None] = mapped_column(String(20), nullable=True, deferred=True)


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
    # CONTRACT-C15 §2.7 (A20, migration 0060): whether achievements apply to records of this type. True for every
    # existing type; an administrator may turn it off. False: the curator API, imports and followers refuse
    # achievements for such records, and existing rows are kept but hidden (`applicable: false`).
    achievements: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


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
    key: Mapped[str] = mapped_column(String(160), default="")  # collapses repeats about the same thing (C16 D5: 64-hex ids)
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
    # C13 §7.4 (migration 0036): which feed the operator chose to follow. `own`: the answering node's own feed
    # (`node_id` must answer; never a relay fallback). `relayed`: the pinned feed through this relay (`via_node_id`
    # is the relay that answered when it was chosen). NULL: a peer added before A19, whose mode is still inferred.
    follow: Mapped[str | None] = mapped_column(String(8), nullable=True)
    via_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # C16 Z1 (migration 0063): what a wire peer advertised, `{min, max, use, high}` (`high`: the highest `max` ever
    # seen from it, the sticky floor of design §4.10). Never holds an `M.m` v0 value (design §2.3).
    remote_wire: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class FollowerSeen(Base):
    """Which v0 protocol a v0 follower credential last sent (its `Atlas-Protocol` header), kept for the Admin list
    of refused v0 followers. Wire followers are in `FollowerWire`. Holds the token id only: no address, user agent
    or other personal data."""

    __tablename__ = "follower_seen"

    token_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(16))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FollowerWire(Base):
    """Per follower token (C16 Z1, design §4.6): the wire range it sent, the negotiated `use`, what was omitted for
    it (rendition `omitted` lists, unioned) and why it was refused, if it was. Token id only."""

    __tablename__ = "follower_wire"

    token_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    wire_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wire_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use: Mapped[int | None] = mapped_column(Integer, nullable=True)
    omitted: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    omitted_since: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    refused: Mapped[str | None] = mapped_column(String(24), nullable=True)  # a `wire_unsupported` reason
    legacy_protocol: Mapped[str | None] = mapped_column(String(16), nullable=True)  # what a refused v0 caller sent
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FeedRendition(Base):
    """Cache of signed renditions (C16 §22.2, design §4.3), origin and mirror. Keyed with the canonical body hash and
    the signer key so a resumed or forked canonical, or a rotated key, never serves a stale rendition. Empty in
    wire 1: nothing renders yet."""

    __tablename__ = "feed_rendition"

    origin_node: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    rendition_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    signer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signer_key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    adapter_version: Mapped[int] = mapped_column(Integer)
    object: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RenditionReceived(Base):
    """A rendition this follower applied for an upstream entry it could not read (design §4.3); the canonical row
    stays in `replicated_change` and is applied by replay after an upgrade."""

    __tablename__ = "rendition_received"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rendition_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    signer_kind: Mapped[str] = mapped_column(String(8))
    signer_id: Mapped[str] = mapped_column(String(64))
    object: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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
    # CONTRACT-C14 §6.6 (migration 0040): a node link's DPoP-bound access token. `jkt` is the token's `cnf.jkt`.
    link_id: Mapped[str | None] = mapped_column(String(26), nullable=True, index=True)
    key_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jkt: Mapped[str | None] = mapped_column(String(64), nullable=True)


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

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    genesis: Mapped[dict[str, Any]] = mapped_column(JSON)
    succession: Mapped[list[Any]] = mapped_column(JSON)  # active successions, in order
    root_key_id: Mapped[str] = mapped_column(String(64))
    recovery: Mapped[dict[str, Any]] = mapped_column(JSON)  # the currently pinned {threshold, keys}
    root_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)  # enc:v1:<fernet(passphrase-wrapped G)>
    box_sealed: Mapped[str] = mapped_column(Text)  # enc:v1: X25519 private key of this node for this group
    self_serial: Mapped[str | None] = mapped_column(String(96), nullable=True)
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
    serial: Mapped[str] = mapped_column(String(96), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(64))
    serial_n: Mapped[int] = mapped_column(BigInteger)
    serial_r: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_serial: Mapped[str | None] = mapped_column(String(96), nullable=True)
    authorized_by_grant: Mapped[str | None] = mapped_column(String(40), nullable=True)
    node_id: Mapped[str] = mapped_column(String(64))
    member_rev: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    held_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (Index("ix_group_cert_node", "group_id", "node_id", "not_after"),)


class GroupGrant(Base):
    """Every signed administrative delegation grant this node holds (CONTRACT-C10 §6.1, §9.3)."""

    __tablename__ = "group_grant"

    group_id: Mapped[str] = mapped_column(ForeignKey("owner_group.group_id", ondelete="CASCADE"), primary_key=True)
    serial: Mapped[str] = mapped_column(String(96), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(64))
    serial_n: Mapped[int] = mapped_column(BigInteger)
    parent_serial: Mapped[str | None] = mapped_column(String(96), nullable=True)
    authorized_by_grant: Mapped[str | None] = mapped_column(String(40), nullable=True)
    account_uid: Mapped[str] = mapped_column(String(26))
    role: Mapped[str] = mapped_column(String(16))
    nodes: Mapped[list[str]] = mapped_column(JSON)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body: Mapped[dict[str, Any]] = mapped_column(JSON)  # the whole signed envelope
    body_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    held_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)  # collision|succession_pending

    __table_args__ = (Index("ix_group_grant_account", "group_id", "account_uid", "not_after"),)


class GroupBannedNode(Base):
    """Node identities banned by a `removed` or `key_compromise` entry; never deleted (§1.6)."""

    __tablename__ = "group_banned_node"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_public_key: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(24))
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    crl_serial: Mapped[str] = mapped_column(String(96))


class GroupView(Base):
    """The newest signed freshness view per member (§1.6)."""

    __tablename__ = "group_view"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupMember(Base):
    """Derived current view of each member, rebuilt from group_cert and the revocation index."""

    __tablename__ = "group_member"

    group_id: Mapped[str] = mapped_column(ForeignKey("owner_group.group_id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_public_key: Mapped[str] = mapped_column(String(100))
    box_public_key: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(80))
    serial: Mapped[str] = mapped_column(String(96))
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
    issuer: Mapped[str] = mapped_column(String(64), primary_key=True)
    crl_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupRevoked(Base):
    """Append-only revocation index (§1.6): rows are never deleted, only upgraded."""

    __tablename__ = "group_revoked"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial: Mapped[str] = mapped_column(String(96), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(24))
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    root_public_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    suspect_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupEnrolment(Base):
    """One enrolment, on the inviter's side or on the joiner's side (§2)."""

    __tablename__ = "group_enrolment"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # enrolment_id (ULID)
    side: Mapped[str] = mapped_column(String(8))  # inviter|joiner
    group_id: Mapped[str] = mapped_column(String(64))
    invite_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256(invite_id)
    secret_enc: Mapped[str] = mapped_column(Text)  # enc:v1: k (joiner: k and its new box key)
    peer_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    peer_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    cert_serial: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # U3 (migration 0037): the trust preset this side chose, applied to its own local policy on activation.
    preset: Mapped[str | None] = mapped_column(String(16), nullable=True)  # same_owner|cautious|NULL (custom)
    preset_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    __table_args__ = (Index("ix_group_enrolment_expiry", "expires_at"),)


class GroupNonce(Base):
    """Request nonces seen from members, kept 15 minutes (§3.2 step 8)."""

    __tablename__ = "group_nonce"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(32), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_group_nonce_seen", "seen_at"),)


# ---- owner-group private channel and profile sync (CONTRACT-C10 §3.4-§4.6, §9.2: migration 0029) ----------------

class GroupStream(Base):
    """One `(origin, epoch, scope)` stream, own streams included (own next seq = hwm + 1 under a row lock)."""

    __tablename__ = "group_stream"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    origin: Mapped[str] = mapped_column(String(64), primary_key=True)
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

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    origin: Mapped[str] = mapped_column(String(64), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope: Mapped[str] = mapped_column(String(26), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(48))  # the signed field name, or "-"
    body: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    sig: Mapped[str] = mapped_column(String(128))
    hlc: Mapped[str | None] = mapped_column(String(96), nullable=True)  # kept when the body is stripped (§4.4)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    disposition: Mapped[str] = mapped_column(String(12))  # applied|noop|parked|held|elided|rejected
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("ix_group_change_scope", "group_id", "scope", "issued_at"),)


class GroupAccountLink(Base):
    """This node's local user for a group account (§5.1): one each way."""

    __tablename__ = "group_account_link"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_uid: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    minted_here: Mapped[bool] = mapped_column(Boolean)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_account_link_user"),)


class GroupAccountNode(Base):
    """Which nodes hold an account, from `account.link` / `account.unlink` entries (§4.3)."""

    __tablename__ = "group_account_node"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_uid: Mapped[str] = mapped_column(String(26), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    hlc: Mapped[str] = mapped_column(String(96))
    origin_node: Mapped[str] = mapped_column(String(64))
    cause: Mapped[str] = mapped_column(String(12))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileVersion(Base):
    """One version of one preference; the versions of a (user, field) form a DAG through `base` (§4.4)."""

    __tablename__ = "profile_version"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    field: Mapped[str] = mapped_column(String(48), primary_key=True)
    hlc: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    cleared: Mapped[bool] = mapped_column(Boolean)
    base: Mapped[list[str]] = mapped_column(JSON)
    cause: Mapped[str] = mapped_column(String(12))
    origin_node: Mapped[str] = mapped_column(String(64))
    entry_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)  # group_change PK; NULL = local only
    is_head: Mapped[bool] = mapped_column(Boolean)
    held_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileSuperseded(Base):
    __tablename__ = "profile_superseded"

    user_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    field: Mapped[str] = mapped_column(String(48), primary_key=True)
    hlc: Mapped[str] = mapped_column(String(96), primary_key=True)
    pruned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileConflict(Base):
    """A review-queue row (§4.6): concurrent|privacy_widen|suspect|suspect_link|missing_base|held."""

    __tablename__ = "profile_conflict"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(48))
    kind: Mapped[str] = mapped_column(String(16))
    winner_hlc: Mapped[str | None] = mapped_column(String(96), nullable=True)
    loser_hlc: Mapped[str | None] = mapped_column(String(96), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(10))  # open|resolved|merged|dismissed
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "field", "kind", "loser_hlc", name="uq_profile_conflict"),)


class ProfileSyncConsent(Base):
    """Per-node consent of one user for one group (§4.3): consent is given where the data lands, never synced."""

    __tablename__ = "profile_sync_consent"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fields: Mapped[list[str]] = mapped_column(JSON)
    signout_propagation: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupSigninState(Base):
    """A pending link (A11) or sign-in (A12) at the relying party, bound to the `atlas_grp_bind` cookie (§5.3)."""

    __tablename__ = "group_signin_state"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(state) = the assertion's state_hash
    group_id: Mapped[str] = mapped_column(String(64))
    idp_node: Mapped[str] = mapped_column(String(64))
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
    kind: Mapped[str] = mapped_column(String(10))  # authorize|callback|start (+ consent|linkform A11, linkpick U3)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupAssertionSeen(Base):
    __tablename__ = "group_assertion_seen"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    jti: Mapped[str] = mapped_column(String(26), primary_key=True)
    iss_node: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupIdpConsent(Base):
    """On the asserting node: this user agreed to link (or sign in to) `rp_node`."""

    __tablename__ = "group_idp_consent"

    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rp_node: Mapped[str] = mapped_column(String(64), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


@event.listens_for(Session, "before_flush")
def _sync_review_candidates(session: Session, _ctx: Any, _instances: Any) -> None:
    """Keep `review_candidate` in step with `review_item.candidates` for every item added or changed in this flush."""
    changed = [obj for obj in session.new if isinstance(obj, ReviewItem)]
    changed += [obj for obj in session.dirty if isinstance(obj, ReviewItem)
                and attributes.get_history(obj, "candidates").has_changes()]
    if not changed:
        return
    with session.no_autoflush:
        for item in changed:
            if item.id is None:
                item.id = ulid()
            want = candidate_ids(item.candidates)
            have: dict[str, ReviewCandidate] = {}
            if item not in session.new:
                have = {r.canonical_id: r for r in session.scalars(
                    select(ReviewCandidate).where(ReviewCandidate.review_id == item.id))}
            for cid, row in have.items():
                if cid not in want:
                    session.delete(row)
            for cid in sorted(want - set(have)):
                session.add(ReviewCandidate(review_id=item.id, canonical_id=cid))


# ---- asking the group (CONTRACT-C14 Part A, migration 0039) ---------------------------------------------------

class GroupAssistItem(Base):
    """Evidence kept for everything an answer from another member of the owner group changed here (§3.4, §3.5).

    `kind`: `bundle` (a helper-signed record bundle), `entry` (a feed entry signed by feed F that a helper carried,
    verified against this node's own pin for F: class 1(a)), `vouch` (a bundle signed by F's own pinned feed key,
    class 1(b)), `wake` (rows of feed F woken on a helper's word that it applied F: class 2, reversible),
    `offer` (a class 2 wake waiting for the "Use what <helper> has" click), `merge_hint` (a helper says the record
    was merged; never applied as a redirect) or `lookup` (a provider lookup attestation, §4.5). `body` holds the
    small facts the item stands for (feed, seqs, survivor); `envelope` the signed object; `signed_entries` the
    entries a bundle carried. Kept 90 days after `superseded_at` (D14.9)."""

    __tablename__ = "group_assist_item"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=ulid)
    helper: Mapped[str] = mapped_column(String(64))
    cid: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))
    body_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    signed_entries: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_group_assist_item_cid", "cid", "superseded_at"),
                      Index("ix_group_assist_item_helper", "helper", "kind"))


class AssistLookupOp(Base):
    """On a helper: one provider lookup an asker requested, by its stable `op_id` (§4.3 step 8, Review #1 C14-9).
    A retry of a completed op gets the stored result re-signed for the new nonce without a second budget take; a
    reuse of the op id for a different request (`request_hash`) is refused."""

    __tablename__ = "assist_lookup_op"

    asker: Mapped[str] = mapped_column(String(64), primary_key=True)
    op_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(10))  # claimed | completed
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupContribState(Base):
    """Contribution state shared inside the owner group (owner decision H67, §7.7). A row whose `origin_node` is
    this node is its own item (served to members); every other row was received from that member, signed by it,
    and is shown only. No payload values, reviewer notes or account data are ever stored here."""

    __tablename__ = "group_contrib_state"

    origin_node: Mapped[str] = mapped_column(String(64), primary_key=True)
    contribution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# ---- the super group (C14-4, migration 0041) ----------------------------------------------------------------------
# One user write made on any member of the owner group, signed by that member and replicated to every member;
# the last-writer-wins state of each subject; and the conflicts (a real overwrite of a concurrent write) admins see.

class GroupWrite(Base):
    """One signed user write (`group.write` object). `id` is this node's own receive order, which is what members
    page through (`since`); `op_id` (`<origin>:<seq>`) is global and idempotent. `status`: `applied` (counted in the
    subject's state), `held` (kept but not counted: its origin's certificate expired (D7), or a later release's kind
    this one cannot read) or `refused`. `author_user_id` is set only on the origin, for the "syncing" state."""

    __tablename__ = "group_write"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    op_id: Mapped[str] = mapped_column(String(96), unique=True)
    origin_node: Mapped[str] = mapped_column(String(96), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    subject: Mapped[str] = mapped_column(String(200), index=True)
    hlc: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(12), default="applied")
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(String(26), nullable=True, index=True)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GroupWriteState(Base):
    """The winning write of one subject (highest hybrid clock among applied writes), identical on every member once
    they hold the same writes. `waiting`: why it is not shown here yet (`no_account`, `no_record`, `no_list`,
    `media_type`, `lists_off`, `limit`, `invalid`), retried every group round, least recently tried first
    (`tried_at`); NULL once materialised."""

    __tablename__ = "group_write_state"

    subject: Mapped[str] = mapped_column(String(200), primary_key=True)
    op_id: Mapped[str] = mapped_column(String(96))
    hlc: Mapped[str] = mapped_column(String(96))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    waiting: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    tried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # last retry


class GroupWriteConflict(Base):
    """A real overwrite: `winner_op` won over a concurrent `loser_op` (its writer had not seen the loser) with a
    different value. `id` is a hash of (subject, winner, loser), so every member records the same entries. Shown to
    administrators only; `seen_at`/`seen_by` record an administrator's acknowledgement on this node."""

    __tablename__ = "group_write_conflict"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject: Mapped[str] = mapped_column(String(200), index=True)
    winner_op: Mapped[str] = mapped_column(String(96))
    loser_op: Mapped[str] = mapped_column(String(96))
    winner_node: Mapped[str] = mapped_column(String(96))
    loser_node: Mapped[str] = mapped_column(String(96))
    winner_hlc: Mapped[str] = mapped_column(String(96))
    loser_hlc: Mapped[str] = mapped_column(String(96))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_by: Mapped[str | None] = mapped_column(String(26), nullable=True)


# ---- node links (CONTRACT-C14 Part C, migration 0040) -----------------------------------------------------------
# Main side: one `node_link` per (account, node link key); the device flow's challenge and device rows; DPoP-bound
# refresh tokens and proof ids; banned keys; the stored answer of every contribution (idempotent execution).

class NodeLink(Base):
    """A node's link key registered at this (main) node, owned by the account that approved it (§6.6).

    `node_id` is the id of the link's root key: a per-main key the node made for this main only (C14-11), so two
    mains cannot correlate the node by key. `current_key_id`/`current_jkt` are the link's one active key and
    `key_epoch` counts its rotations (C14-6). `trust` is the link's own (`probation` until its `reputation` reaches
    the main's auto-approve threshold, C14-8, H73). `status`: `active`, `frozen` (a fork or a reported theft: every
    token refused), `revoked`, `banned`."""

    __tablename__ = "node_link"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(64), index=True)
    root_public_key: Mapped[str] = mapped_column(String(64))
    current_key_id: Mapped[str] = mapped_column(String(64))
    current_jkt: Mapped[str] = mapped_column(String(64))
    key_epoch: Mapped[int] = mapped_column(Integer, default=1)
    rotations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(80), default="")
    public_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review: Mapped[str] = mapped_column(String(8), default="standard")
    trust: Mapped[str] = mapped_column(String(10), default="probation")
    reputation: Mapped[int] = mapped_column(Integer, default=0)
    show_name: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "node_id", name="uq_node_link_user_node"),
                      UniqueConstraint("subject", name="uq_node_link_subject"))


class LinkChallenge(Base):
    """§6.3 step 0 (Review #1 C14-7): a registration challenge, stored as its hash, consumed once."""

    __tablename__ = "link_challenge"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LinkDevice(Base):
    """One device authorization (§6.3). Codes are stored only as a hash (`device_code`) and an HMAC (`user_code`);
    `confirm_hash` is the node's per-flow confirmation code, compared, never shown; `jkt` binds every poll to the
    key that registered. `status`: `pending`, `approved`, `denied`, `used`."""

    __tablename__ = "link_device"

    device_code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_code_hmac: Mapped[str] = mapped_column(String(64), unique=True)
    confirm_hash: Mapped[str] = mapped_column(String(64))
    jkt: Mapped[str] = mapped_column(String(64))
    node: Mapped[dict[str, Any]] = mapped_column(JSON)
    scopes: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="pending")
    user_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    link_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    show_name: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    confirm_attempts: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(64), default="")
    interval: Mapped[int] = mapped_column(Integer, default=5)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LinkRefresh(Base):
    """A link's refresh token (sha256), bound to the key (`jkt`) and `key_epoch` it was issued for (§6.5)."""

    __tablename__ = "link_refresh"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    link_id: Mapped[str] = mapped_column(String(26), index=True)
    jkt: Mapped[str] = mapped_column(String(64))
    key_epoch: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LinkNonce(Base):
    """A DPoP proof id seen (§6.5): the primary key makes `(jkt, jti)` unique, so a replay is a duplicate key."""

    __tablename__ = "link_nonce"

    jkt: Mapped[str] = mapped_column(String(64), primary_key=True)
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class LinkBannedKey(Base):
    """A node link key (a link root id or one key of a rotation history) that may never link again (§6.7)."""

    __tablename__ = "link_banned_key"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), default="")
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    by: Mapped[str | None] = mapped_column(String(26), nullable=True)


class LinkContributionAnswer(Base):
    """The stored per-item answer to one contribution (§7.4 step 1, Review #1 C14-9): a retry gets it back without
    being counted again (a refused item until its `retry_until`)."""

    __tablename__ = "link_contribution_answer"

    contribution_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    link_id: Mapped[str] = mapped_column(String(26), index=True)
    answer: Mapped[dict[str, Any]] = mapped_column(JSON)
    retry_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# Node side (C14-3n): the link to one main, the contribution queue, and local -> main id aliases.

class UpstreamLink(Base):
    """This node's link to one main Atlas (§6, node side). One row per main (its pinned root `main_node_id`).

    The link key is a per-main Ed25519 key made for this main only (C14-11): `link_key` holds the current private
    key, sealed like the node signing key; `link_rotations` its history from `link_root_public`. Tokens are sealed.
    `status`: `starting` (a device flow is open; `flow` holds it, its confirmation code sealed), `linked`,
    `revoked_locally` -> `pending_remote` -> `confirmed` (an unlink from here, C14-10), `remote_revoked` (the main
    refused the link)."""

    __tablename__ = "upstream_link"

    main_node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer_url: Mapped[str] = mapped_column(String(300))
    main_root_public_key: Mapped[str] = mapped_column(String(64))
    main_name: Mapped[str] = mapped_column(String(80), default="")
    link_root_public: Mapped[str] = mapped_column(String(64))
    link_key: Mapped[str] = mapped_column(Text)
    link_key_previous: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_rotations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="starting")
    flow: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    link_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(36), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    grant: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    key_epoch: Mapped[int] = mapped_column(Integer, default=1)
    access_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_seq: Mapped[int] = mapped_column(BigInteger, default=1)
    status_cursor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UpstreamContribution(Base):
    """One node-signed contribution to a main (§7.1, §7.2). `body` is what gets signed (re-signed after a link key
    rotation); `envelope` the last signed form. States (§7.2): `queued`, `sent`, `pending`, `approved`, `rejected`,
    `reverted`, `withdrawn`, `failed`, `link_revoked`, `unsent`."""

    __tablename__ = "upstream_contribution"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=ulid)
    main_node_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    local_cid: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    main_cid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(14), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    feed_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (UniqueConstraint("main_node_id", "seq", name="uq_upstream_contribution_seq"),)


class UpstreamAlias(Base):
    """A local record a main approved as a new record under its own id (§7.4 "New records")."""

    __tablename__ = "upstream_alias"

    local_cid: Mapped[str] = mapped_column(String(40), primary_key=True)
    main_cid: Mapped[str] = mapped_column(String(40), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
