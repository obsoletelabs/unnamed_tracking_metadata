"""Artwork cache: SSRF-safe download, strict image validation, content-addressed storage."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import socket
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import Artwork, now
from .settings import Settings

FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp"),
           "GIF": ("gif", "image/gif")}
EXT_TYPES = {ext: ctype for ext, ctype in FORMATS.values()}
MAX_REDIRECTS = 3
MAX_SIDE = 12000


class MediaError(Exception):
    pass


class MediaStorageError(Exception):
    """The node cannot write to its own media directory. This is an operator problem, not a bad image."""


_TUNNELS = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")]


def _public(address: str) -> bool:
    """Globally routable, and not an IPv6 form that tunnels or translates to an IPv4 address."""
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and (ip.ipv4_mapped or any(ip in net for net in _TUNNELS)):
        return False
    return ip.is_global


def check_url(url: str, settings: Settings) -> tuple[str, str, int, str]:
    """Validate a URL for fetching. Returns (scheme, host, port, ip) with the address pinned."""
    if len(url) > 1000 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        raise MediaError("invalid url")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise MediaError("invalid url") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.fragment:
        raise MediaError("invalid url")
    if port not in (80, 443):
        raise MediaError("port not allowed")
    host = parts.hostname.lower()
    if settings.media_allowed_hosts and host not in settings.media_allowed_hosts:
        raise MediaError("host not allowed")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaError("cannot resolve host") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs or not all(_public(a) for a in addrs):
        raise MediaError("address is not publicly routable")
    return parts.scheme, host, port, sorted(addrs)[0]


def _request(client: httpx.Client, url: str, settings: Settings) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, ip = check_url(url, settings)
        parts = urlsplit(url)
        target = f"{scheme}://{ip if ':' not in ip else '[' + ip + ']'}:{port}{parts.path or '/'}"
        if parts.query:
            target += "?" + parts.query
        try:
            with client.stream("GET", target, headers={"Host": host, "Accept": "image/*",
                                                       "User-Agent": "atlas-metadata/2"},
                               extensions={"sni_hostname": host}, timeout=10, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise MediaError("redirect without location")
                    url = urljoin(url, loc)
                    continue
                if resp.status_code != 200:
                    raise MediaError(f"upstream status {resp.status_code}")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > settings.media_max_bytes:
                    raise MediaError("image too large")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > settings.media_max_bytes:
                        raise MediaError("image too large")
                return bytes(buf)
        except httpx.HTTPError as exc:
            raise MediaError(f"fetch failed: {type(exc).__name__}") from exc
    raise MediaError("too many redirects")


def validate_image(data: bytes, settings: Settings) -> tuple[str, str, int, int]:
    """Decode-verify the bytes; the format comes from the content, never from headers."""
    Image.MAX_IMAGE_PIXELS = settings.media_max_pixels
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            w, h = img.size
            if fmt not in FORMATS:
                raise MediaError("unsupported image type")
            if w < 1 or h < 1 or w > MAX_SIDE or h > MAX_SIDE or w * h > settings.media_max_pixels:
                raise MediaError("image dimensions out of range")
            img.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise MediaError("not a valid image") from exc
    ext, ctype = FORMATS[fmt]
    return ext, ctype, w, h


def blob_path(settings: Settings, digest: str, ext: str) -> Path:
    return Path(settings.media_dir) / digest[:2] / f"{digest}.{ext}"


def storage_problem(settings: Settings) -> str | None:
    """None when the media directory exists (or can be created) and is writable, else an actionable message."""
    root = Path(settings.media_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=root, suffix=".probe")
        os.close(fd)
        os.unlink(tmp)
    except OSError as exc:
        return (f"cannot write to ATLAS_MEDIA_DIR ({settings.media_dir}): {type(exc).__name__}. "
                "The directory must be writable by the user Atlas runs as (uid 10001 in the official image).")
    return None


def store(settings: Settings, data: bytes, ext: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = blob_path(settings, digest, ext)
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    except OSError as exc:
        raise MediaStorageError(
            f"cannot write to ATLAS_MEDIA_DIR ({settings.media_dir}): {type(exc).__name__}. "
            "The directory must be writable by the user Atlas runs as (uid 10001 in the official image).") from exc
    return digest


def find_blob(settings: Settings, digest: str) -> tuple[Path, str] | None:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    for ext, ctype in EXT_TYPES.items():
        path = blob_path(settings, digest, ext)
        if path.is_file():
            return path, ctype
    return None


def cache_one(session: Session, settings: Settings, art: Artwork, client: httpx.Client) -> bool:
    try:
        data = _request(client, art.url, settings)
        ext, ctype, w, h = validate_image(data, settings)
        digest = store(settings, data, ext)
    except MediaError:
        art.fail_count += 1
        art.next_try_at = now() + timedelta(minutes=min(60 * 24 * 7, 5 * 2 ** min(art.fail_count, 12)))
        return False
    art.blob_hash, art.content_type, art.size_bytes = digest, ctype, len(data)
    art.width, art.height = art.width or w, art.height or h
    art.cached_at, art.fail_count, art.next_try_at = now(), 0, None
    return True


def cache_pending(maker: sessionmaker[Session], settings: Settings, client: httpx.Client | None = None) -> int:
    """Cache a bounded batch of uncached artwork; safe to call from several workers."""
    done = 0
    own = client or httpx.Client(trust_env=False)  # never route through an environment proxy
    try:
        with maker() as session:
            rows = session.scalars(
                select(Artwork).where(
                    Artwork.blob_hash.is_(None),
                    (Artwork.next_try_at.is_(None)) | (Artwork.next_try_at <= now()),
                ).order_by(Artwork.priority, Artwork.created_at).limit(settings.media_cache_batch)
            ).all()
            for art in rows:
                done += cache_one(session, settings, art, own)
                session.commit()
    finally:
        if client is None:
            own.close()
    return done
