from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base


class ImageAsset(Base):
    """Durable media provenance independent of the public image presentation record."""

    __tablename__ = "image_assets"
    __table_args__ = (UniqueConstraint("sha256", name="uq_image_asset_sha256"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("game_images.id", ondelete="CASCADE"), unique=True, index=True)
    storage_key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(80))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    licence: Mapped[str] = mapped_column(String(200), default="")
    attribution: Mapped[str] = mapped_column(String(500), default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    source_type: Mapped[str] = mapped_column(String(40), default="community")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class RemoteFetchPolicy:
    max_bytes: int = 5 * 1024 * 1024
    allowed_content_types: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


def validate_remote_url(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("remote media URLs must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("remote media URLs may not contain credentials")
    host = parsed.hostname.rstrip(".").lower()
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("remote media host could not be resolved") from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise ValueError("remote media host resolves to a non-public address")
    return value


def validate_image_bytes(data: bytes, content_type: str, policy: RemoteFetchPolicy | None = None) -> tuple[str, str]:
    policy = policy or RemoteFetchPolicy()
    if not data or len(data) > policy.max_bytes:
        raise ValueError("image exceeds the configured size limit")
    content_type = content_type.lower().split(";", 1)[0].strip()
    if content_type not in policy.allowed_content_types:
        raise ValueError("unsupported image content type")
    signatures = {
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/webp": (b"RIFF",),
    }
    if not any(data.startswith(signature) for signature in signatures[content_type]):
        raise ValueError("image bytes do not match the declared content type")
    return hashlib.sha256(data).hexdigest(), content_type


def storage_key(sha256: str, content_type: str) -> str:
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[content_type]
    return f"images/{sha256[:2]}/{sha256}.{extension}"
