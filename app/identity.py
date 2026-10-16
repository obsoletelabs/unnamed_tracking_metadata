from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderMapping(Base):
    """Namespaced external identity evidence; never the canonical primary key."""

    __tablename__ = "provider_mappings"
    __table_args__ = (UniqueConstraint("provider", "entity_type", "external_id", name="uq_provider_entity_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="game")
    external_id: Mapped[str] = mapped_column(String(200), index=True)
    confidence: Mapped[float] = mapped_column(default=1.0)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GameAlias(Base):
    """Searchable alternate identity without mutating the canonical title."""

    __tablename__ = "game_aliases"
    __table_args__ = (UniqueConstraint("game_id", "normalized_alias", name="uq_game_alias_normalized"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(300))
    normalized_alias: Mapped[str] = mapped_column(String(300), index=True)
    locale: Mapped[str] = mapped_column(String(20), default="")
    alias_type: Mapped[str] = mapped_column(String(40), default="alternate")
    source: Mapped[str] = mapped_column(String(80), default="curated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetadataObservation(Base):
    """An immutable observation from a source, retained separately from the chosen value."""

    __tablename__ = "metadata_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(120), index=True)
    value_json: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    source_label: Mapped[str] = mapped_column(String(200), default="")
    source_external_id: Mapped[str] = mapped_column(String(200), default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    licence: Mapped[str] = mapped_column(String(200), default="")
    confidence: Mapped[float] = mapped_column(default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("metadata_observations.id", ondelete="SET NULL"), nullable=True)


class CanonicalField(Base):
    """The currently selected value, pointing back to its evidence."""

    __tablename__ = "canonical_fields"
    __table_args__ = (UniqueConstraint("game_id", "field_name", name="uq_canonical_game_field"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(120))
    value_json: Mapped[str] = mapped_column(Text)
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("metadata_observations.id", ondelete="SET NULL"), nullable=True)
    selection_reason: Mapped[str] = mapped_column(String(500), default="")
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameRelationship(Base):
    """Explicit relationship between canonical games; no relationship is inferred from title alone."""

    __tablename__ = "game_relationships"
    __table_args__ = (UniqueConstraint("from_game_id", "to_game_id", "relationship_type", name="uq_game_relationship"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    to_game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(40), index=True)
    confidence: Mapped[float] = mapped_column(default=1.0)
    source: Mapped[str] = mapped_column(String(80), default="curated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameIdentityRedirect(Base):
    """Permanent redirect for merged canonical identities without deleting their history."""

    __tablename__ = "game_identity_redirects"
    __table_args__ = (UniqueConstraint("old_canonical_id", name="uq_old_canonical_redirect"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    old_canonical_id: Mapped[str] = mapped_column(String(32), index=True)
    new_game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="RESTRICT"), index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
