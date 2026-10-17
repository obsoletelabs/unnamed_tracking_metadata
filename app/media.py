from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
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


@dataclass(frozen=True, slots=True)
class StoredImage:
    sha256: str
    content_type: str
    byte_size: int
    width: int
    height: int
    storage_key: str
    derivative_keys: dict[str, str]


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
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("image data is malformed or unreadable") from exc
    signatures = {
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/webp": (b"RIFF",),
    }
    if not any(data.startswith(signature) for signature in signatures[content_type]):
        raise ValueError("image bytes do not match the declared content type")
    if content_type == "image/webp" and data[8:12] != b"WEBP":
        raise ValueError("invalid WebP image")
    return hashlib.sha256(data).hexdigest(), content_type


def image_dimensions(data: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("image dimensions could not be decoded") from exc


def storage_key(sha256: str, content_type: str, derivative: str = "original") -> str:
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[content_type]
    suffix = "" if derivative == "original" else f"-{derivative}"
    return f"images/{sha256[:2]}/{sha256}{suffix}.{extension}"


class ObjectStore:
    def put(self, key: str, data: bytes, content_type: str) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3ObjectStore(ObjectStore):
    def __init__(self) -> None:
        import boto3

        self.bucket = os.environ["MEDIA_S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=os.getenv("MEDIA_S3_ENDPOINT") or None,
            region_name=os.getenv("MEDIA_S3_REGION") or None,
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()


def object_store() -> ObjectStore:
    if os.getenv("MEDIA_STORAGE", "local").lower() == "s3":
        return S3ObjectStore()
    return LocalObjectStore(os.getenv("MEDIA_LOCAL_ROOT", "/data/media"))


def _derivative(data: bytes, max_dimension: int) -> bytes:
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()


def ingest_image(data: bytes, content_type: str, store: ObjectStore | None = None) -> StoredImage:
    digest, normalized_type = validate_image_bytes(data, content_type)
    width, height = image_dimensions(data)
    if width > 50000 or height > 50000:
        raise ValueError("image dimensions exceed the configured limit")
    store = store or object_store()
    original_key = storage_key(digest, normalized_type)
    store.put(original_key, data, normalized_type)
    derivatives: dict[str, str] = {}
    for name, dimension in (("small", 640), ("medium", 1600)):
        derivative = _derivative(data, dimension)
        key = storage_key(digest, "image/jpeg", name)
        store.put(key, derivative, "image/jpeg")
        derivatives[name] = key
    return StoredImage(digest, normalized_type, len(data), width, height, original_key, derivatives)
