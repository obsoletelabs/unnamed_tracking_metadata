from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base


class AuthorityMapping(Base):
    """Local-to-central identity mapping. Local IDs remain valid independently."""

    __tablename__ = "authority_mappings"
    __table_args__ = (UniqueConstraint("local_canonical_id", name="uq_authority_local_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_canonical_id: Mapped[str] = mapped_column(String(32), index=True)
    central_canonical_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="unmapped", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(80), default="local")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etag: Mapped[str] = mapped_column(String(200), default="")


@dataclass(frozen=True, slots=True)
class SyncEnvelope:
    canonical_id: str
    revision: int
    fields: dict
    provider_mappings: list[dict]
    aliases: list[dict]
    relationships: list[dict]
    source: str = "central"
    generated_at: datetime = datetime.now(timezone.utc)


def merge_authority_payload(central: dict, local_overrides: dict | None = None) -> dict:
    """Merge shared metadata with private presentation overrides without mutating the shared copy."""
    merged = dict(central)
    if local_overrides:
        for field, value in local_overrides.items():
            merged[field] = value
    return merged


def publishable_fields(fields: dict, private_fields: set[str] | None = None) -> dict:
    private_fields = private_fields or set()
    return {key: value for key, value in fields.items() if key not in private_fields}
