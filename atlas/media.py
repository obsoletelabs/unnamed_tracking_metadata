"""Artwork cache: SSRF-safe download, strict image validation, content-addressed storage.

C12 (A18) additions: every failure has a `media_error` code (§12.6.1) stored as `last_error`; https only for new
downloads and no https -> http redirect (H40); one deadline across redirects and body; a Content-Type check before
the body is read; per-host concurrency and rate limits; frame and total-pixel caps for animated images; measured
dimensions, aspect class and a difference hash on every cached image (§12.5.2-§12.5.4); per-source media policy
(§12.5.5); uploads re-encoded without metadata (§12.6.2, H41)."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import os
import re
import socket
import tempfile
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Achievement, Artwork, now
from .providers.media_policy import policy
from .settings import Settings

log = logging.getLogger("atlas.media")

FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp"),
           "GIF": ("gif", "image/gif")}
EXT_TYPES = {ext: ctype for ext, ctype in FORMATS.values()}
MAX_REDIRECTS = 3
MAX_SIDE = 12000
CONNECT_TIMEOUT, READ_TIMEOUT = 5.0, 10.0
#: Content types accepted before the body is read (the bytes are still decode-verified). A missing header is
#: accepted too: the decode decides (amendment A18 #6).
ACCEPTED_TYPES = ("image/", "application/octet-stream")


class MediaError(Exception):
    """An image that cannot be fetched or used. `code` is a `media_error` value (C12 §12.6.1)."""

    def __init__(self, message: str, code: str = "bad_image", *, deferred: bool = False) -> None:
        super().__init__(message)
        #: `deferred`: this node's own per-host limits said "not now"; not the image's fault, so not a failure
        self.code, self.deferred = code, deferred


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
        raise MediaError("invalid url", "invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise MediaError("invalid url", "invalid_url") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.fragment:
        raise MediaError("invalid url", "invalid_url")
    if port not in (80, 443):
        raise MediaError("port not allowed", "port_not_allowed")
    host = parts.hostname.lower()
    if settings.media_allowed_hosts and host not in settings.media_allowed_hosts:
        raise MediaError("host not allowed", "host_not_allowed")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaError("cannot resolve host", "dns_failed") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs or not all(_public(a) for a in addrs):
        raise MediaError("address is not publicly routable", "not_public")
    if parts.scheme != "https" and settings.media_https_only:
        raise MediaError("https required", "https_required")
    return parts.scheme, host, port, sorted(addrs)[0]


# ---- per-host limits (per process) ----------------------------------------------------------------------

_HOSTS_LOCK = threading.Lock()
_HOST_SLOTS: dict[str, threading.BoundedSemaphore] = {}
_HOST_TIMES: dict[str, deque[float]] = {}


def _slot(host: str, settings: Settings) -> threading.BoundedSemaphore:
    with _HOSTS_LOCK:
        slot = _HOST_SLOTS.get(host)
        if slot is None:
            if len(_HOST_SLOTS) > 4096:  # bounded: forget idle hosts
                _HOST_SLOTS.clear()
            slot = _HOST_SLOTS[host] = threading.BoundedSemaphore(settings.media_host_concurrency)
        return slot


def _take_rate(host: str, settings: Settings) -> bool:
    """A sliding one-minute window of at most `media_host_rpm` requests per host."""
    clock = time.monotonic()
    with _HOSTS_LOCK:
        if len(_HOST_TIMES) > 4096:
            _HOST_TIMES.clear()
        times = _HOST_TIMES.setdefault(host, deque())
        while times and clock - times[0] > 60:
            times.popleft()
        if len(times) >= settings.media_host_rpm:
            return False
        times.append(clock)
        return True


def reset_host_limits() -> None:
    """Forget the per-host counters (tests; a process restart does the same)."""
    with _HOSTS_LOCK:
        _HOST_SLOTS.clear()
        _HOST_TIMES.clear()


def _status_code(status: int) -> str:
    if status == 404:
        return "status_404"
    if status == 410:
        return "status_410"
    return "status_5xx" if status >= 500 else "status_4xx"


def fetch_options(settings: Settings, source: str) -> tuple[str, tuple[str, ...]]:
    """(User-Agent, follow_hosts) for downloading an image of `source`: a provider that asks clients to identify
    themselves (Open Library, C15 §3.2) gets the operator's `provider_contact`; the redirect hosts come from the
    source's media policy."""
    from .providers.base import user_agent

    contact = settings.provider_contact if source in CONTACT_SOURCES else None
    return user_agent(contact or None), policy(settings, source).follow_hosts


#: sources whose image downloads carry `provider_contact` in the User-Agent (C15 §3.2), with the hosts (and their
#: subdomains) that get it: the contact goes to the provider that asked for it, never to a redirect host of another
#: service (covers.openlibrary.org redirects to archive.org, which gets the plain versioned User-Agent)
CONTACT_HOSTS: dict[str, tuple[str, ...]] = {"openlibrary": ("openlibrary.org",)}
CONTACT_SOURCES = frozenset(CONTACT_HOSTS)


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _request(client: httpx.Client, url: str, settings: Settings, *, user_agent: str | None = None,
             follow_hosts: tuple[str, ...] = (), agent_hosts: tuple[str, ...] = ()) -> bytes:
    """Download one image. `follow_hosts`, when given, limits where a redirect may lead (each hop is still checked
    by `check_url`). `agent_hosts`, when given, are the only hosts `user_agent` is sent to; any other hop gets the
    plain versioned User-Agent."""
    from .providers.base import user_agent as default_agent

    plain = default_agent()
    custom = user_agent or plain
    deadline = time.monotonic() + settings.media_fetch_deadline_s
    first_scheme = None
    for hop in range(MAX_REDIRECTS + 1):
        if hop and follow_hosts and not _host_in((urlsplit(url).hostname or "").lower(), follow_hosts):
            raise MediaError("redirect to a host this source does not use", "host_not_allowed")
        scheme, host, port, ip = check_url(url, settings)
        if first_scheme == "https" and scheme != "https":
            raise MediaError("redirect from https to http refused", "https_required")
        first_scheme = first_scheme or scheme
        parts = urlsplit(url)
        target = f"{scheme}://{ip if ':' not in ip else '[' + ip + ']'}:{port}{parts.path or '/'}"
        if parts.query:
            target += "?" + parts.query
        agent = custom if not agent_hosts or _host_in(host.lower(), agent_hosts) else plain
        if not _take_rate(host, settings):
            raise MediaError("too many requests to this host; retried later", "timeout", deferred=True)
        slot = _slot(host, settings)
        if not slot.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise MediaError("timed out waiting for a download slot", "timeout", deferred=True)
        try:
            left = max(0.1, deadline - time.monotonic())
            timeout = httpx.Timeout(min(READ_TIMEOUT, left), connect=min(CONNECT_TIMEOUT, left))
            with client.stream("GET", target, headers={"Host": host, "Accept": "image/*", "User-Agent": agent},
                               extensions={"sni_hostname": host}, timeout=timeout, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise MediaError("redirect without location", "status_4xx")
                    url = urljoin(url, loc)
                    continue
                if resp.status_code != 200:
                    raise MediaError(f"upstream status {resp.status_code}", _status_code(resp.status_code))
                ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype and not ctype.startswith(ACCEPTED_TYPES):
                    raise MediaError(f"not an image ({ctype[:40]})", "bad_type")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > settings.media_max_bytes:
                    raise MediaError("image too large", "too_large")
                buf = bytearray()
                for chunk in resp.iter_bytes():  # decoded bytes: a gzip bomb is bounded by the same cap
                    buf += chunk
                    if len(buf) > settings.media_max_bytes:
                        raise MediaError("image too large", "too_large")
                    if time.monotonic() > deadline:
                        raise MediaError("download took too long", "timeout")
                return bytes(buf)
        except httpx.TimeoutException as exc:
            raise MediaError(f"fetch failed: {type(exc).__name__}", "timeout") from exc
        except httpx.HTTPError as exc:
            raise MediaError(f"fetch failed: {type(exc).__name__}", "dns_failed" if isinstance(
                exc, httpx.ConnectError) else "status_5xx") from exc
        finally:
            slot.release()
    raise MediaError("too many redirects", "too_many_redirects")


# ---- validation and measurement ---------------------------------------------------------------------------

def _check_frames(img: Image.Image, settings: Settings) -> None:
    """Animation limits, from the frame headers before any frame is decoded (§12.6.2): at most
    `media_max_frames` frames, and the frames together at most 4 x `media_max_pixels` pixels."""
    if not getattr(img, "is_animated", False):
        return
    frames = total = 0
    cap = settings.media_max_pixels * 4
    for frame in ImageSequence.Iterator(img):
        frames += 1
        w, h = frame.size
        total += w * h
        if frames > settings.media_max_frames or total > cap:
            raise MediaError("too many frames or pixels in an animated image", "dimensions")
    img.seek(0)


def validate_image(data: bytes, settings: Settings) -> tuple[str, str, int, int]:
    """Decode-verify the bytes; the format comes from the content, never from headers."""
    Image.MAX_IMAGE_PIXELS = settings.media_max_pixels
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            w, h = img.size
            if fmt not in FORMATS:
                raise MediaError("unsupported image type", "bad_type")
            if w < 1 or h < 1 or w > MAX_SIDE or h > MAX_SIDE or w * h > settings.media_max_pixels:
                raise MediaError("image dimensions out of range", "dimensions")
            _check_frames(img, settings)
            img.verify()
    except Image.DecompressionBombError as exc:
        raise MediaError("image dimensions out of range", "dimensions") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, ValueError) as exc:
        raise MediaError("not a valid image", "bad_image") from exc
    ext, ctype = FORMATS[fmt]
    return ext, ctype, w, h


def dhash(data: bytes) -> str | None:
    """64-bit difference hash of a 9x8 grayscale (first frame), as 16 hex digits (§12.5.4)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            px = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, ValueError):
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def aspect_class(width: int | None, height: int | None) -> str | None:
    """§12.5.3: the class of `width / height`, or None when the size is unknown."""
    if not width or not height:
        return None
    r = width / height
    if r < 0.60:
        return "tall"
    if r < 0.75:
        return "portrait"
    if 0.90 <= r <= 1.10:
        return "square"
    if 1.25 <= r < 1.90:
        return "landscape"
    if 1.90 <= r < 2.40:
        return "capsule"
    if r >= 2.40:
        return "wide"
    return "other"


def reencode_upload(data: bytes, settings: Settings) -> tuple[bytes, str, str, int, int]:
    """An uploaded image decoded and written again without EXIF, XMP, ICC or text chunks (§12.6.2, H41).

    PNG and WebP are written lossless, JPEG at quality 92, an animated GIF stays an animated GIF. SVG and other
    formats are refused by `validate_image` first. Returns (bytes, ext, content type, width, height)."""
    ext, ctype, w, h = validate_image(data, settings)
    out = io.BytesIO()
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            if fmt == "JPEG":
                # apply the EXIF orientation before the EXIF goes, or a phone photo comes out rotated
                clean = ImageOps.exif_transpose(img)
                clean = clean.convert("RGB") if clean.mode not in ("RGB", "L") else clean.copy()
                clean.info = {}
                clean.save(out, "JPEG", quality=92)
            else:
                # what is drawing, not metadata, is kept: transparency, frame durations and the loop count
                frames, durations = [], []
                for frame in ImageSequence.Iterator(img):
                    copy = frame.copy()  # loads the frame: WebP sets its duration only then
                    durations.append(frame.info.get("duration", 100))
                    copy.info = {k: frame.info[k] for k in ("transparency",) if k in frame.info}
                    frames.append(copy)
                first, animated = frames[0], len(frames) > 1
                anim: dict[str, Any] = {}
                if animated:
                    anim = {"save_all": True, "append_images": frames[1:], "duration": durations,
                            "loop": img.info.get("loop", 0)}
                if "transparency" in first.info:
                    anim["transparency"] = first.info["transparency"]
                if fmt == "GIF":
                    first.save(out, "GIF", optimize=False, **anim)
                elif fmt == "WEBP":
                    anim.pop("transparency", None)  # WebP carries alpha in the pixels (RGBA)
                    first.save(out, "WEBP", lossless=True, **anim)
                else:  # PNG, and APNG with all its frames
                    first.save(out, "PNG", **anim)
    except (OSError, ValueError, EOFError) as exc:
        raise MediaError("not a valid image", "bad_image") from exc
    data = out.getvalue()
    ext, ctype, w, h = validate_image(data, settings)
    return data, ext, ctype, w, h


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


def own_media_base(settings: Settings) -> str:
    return settings.public_url.rstrip("/") + "/media/"


def is_upload(settings: Settings, url: str | None) -> bool:
    """An image uploaded to this node: its address is this node's own /media/<sha256>."""
    return bool(url) and url.startswith(own_media_base(settings))  # type: ignore[union-attr]


def fetch_policy(settings: Settings, source: str, url: str | None) -> str:
    """`proxy`, `redirect` or `none` for one image. Uploads are always held here (proxy)."""
    return "proxy" if is_upload(settings, url) else policy(settings, source).fetch


def measure(art: Artwork, data: bytes, w: int, h: int) -> None:
    """Measured wins (§12.5.2): the decoded size replaces a differing declaration, which stays in `declared_*`."""
    if art.declared_width is None and art.declared_height is None and art.aspect is None:
        art.declared_width, art.declared_height = art.width, art.height
    art.width, art.height = w, h
    art.aspect = aspect_class(w, h)
    art.dhash = dhash(data)


# ---- from the peer that delivered the row (C13 §6) -----------------------------------------------------------

DIGEST_RE = re.compile(r"/(?:api/v2/)?media/([0-9a-f]{64})(?:\.[a-z]{3,4})?\Z")
SOURCES_KEPT = 5


def url_digest(url: str | None) -> str | None:
    """A peer's own content address in an image URL (`…/media/<sha256>`), the only part discovery keeps."""
    if not url:
        return None
    found = DIGEST_RE.search(urlsplit(url).path)
    return found.group(1) if found else None


def _tried(art: Artwork, source: str, error: str | None) -> None:
    """Keep why each source failed (C13 §6.3): the last few attempts, newest last, bounded."""
    entries = [e for e in (art.sources_tried or []) if isinstance(e, dict) and e.get("source") != source]
    entries.append({"source": source[:80], "error": error, "at": now().isoformat()})
    art.sources_tried = entries[-SOURCES_KEPT:]


def peer_for(session: Session, origin: str | None) -> Any:
    """The enabled peer this node replicates `origin`'s feed from (directly or through a relay)."""
    if not origin:
        return None
    from .models import Peer

    return session.scalars(select(Peer).where(Peer.node_id == origin, Peer.enabled.is_(True))
                           .order_by(Peer.created_at)).first()


def _peer_request(client: httpx.Client, base: str, digest: str, token: str | None, settings: Settings) -> bytes:
    """GET `<peer>/api/v2/sync/media/<sha256>`. The peer's address was pinned by the operator, so this exact
    origin is allowed even on a private network (the C12 public-address rule is for provider URLs); no redirects
    are followed, and the body is bounded and must hash to the digest asked for."""
    from . import wire

    headers = {"Accept": "image/*", "User-Agent": "atlas-metadata/2", wire.HEADER: wire.request_header()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with client.stream("GET", f"{base.rstrip('/')}/api/v2/sync/media/{digest}", headers=headers,
                           timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT), follow_redirects=False) as resp:
            if 300 <= resp.status_code < 400:  # C13 §6.2: never followed, so a peer cannot bounce us inward
                raise MediaError(f"peer redirected ({resp.status_code}); not followed", "too_many_redirects")
            if resp.status_code != 200:
                raise MediaError(f"peer status {resp.status_code}", _status_code(resp.status_code))
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > settings.media_max_bytes:
                    raise MediaError("image too large", "too_large")
    except httpx.TimeoutException as exc:
        raise MediaError(f"peer fetch failed: {type(exc).__name__}", "timeout") from exc
    except httpx.HTTPError as exc:
        raise MediaError(f"peer fetch failed: {type(exc).__name__}", "status_5xx") from exc
    if hashlib.sha256(buf).hexdigest() != digest:
        raise MediaError("peer sent bytes that do not match the digest", "bad_image")
    return bytes(buf)


def _answers_as_signer(session: Session, peer: Any) -> bool:
    """Whether `peer` serves its own feed (`follow = own`, or inferred so for a peer added before C13)."""
    from . import sync

    follow = peer.follow or (sync.inferred_follow(session, peer.id) or {}).get("follow")
    return follow == sync.FOLLOW_OWN


def discover_digest(session: Session, settings: Settings, art: Artwork, peer: Any, client: httpx.Client,
                    token: str | None) -> str | None:
    """The digest the peer stores this image under: from the URL when it is a content address, else from the
    peer's artwork list for the record (only `media_url` of the item with this kind and address is read)."""
    if art.peer_digest:
        return art.peer_digest
    digest, provenance = url_digest(art.url), "url"
    if digest is None and not _answers_as_signer(session, peer):
        raise MediaError("a relay is not asked for digests", "relay_unsigned")  # C13 §6.3: it did not sign the rows
    if digest is None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = client.get(f"{peer.url.rstrip('/')}/api/v2/games/{art.canonical_id}/artwork", headers=headers,
                              timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT), follow_redirects=False)
            items = resp.json() if resp.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            items = []
        items = items.get("results", []) if isinstance(items, dict) else []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("url") == art.url and item.get("kind") == art.kind:
                digest = url_digest(item.get("media_url") if isinstance(item.get("media_url"), str) else None)
                break
        provenance = "peer_api"
    if digest:
        art.peer_digest, art.digest_provenance = digest, provenance
    return digest


def _from_peer(session: Session, settings: Settings, art: Artwork, client: httpx.Client) -> bytes | None:
    """Try the peer that delivered the row first (C13 §6.2); None when there is no such peer or it failed."""
    if not getattr(settings, "sync_media_prefer_peer", True) or not art.origin_node:
        return None
    peer = peer_for(session, art.origin_node)
    if peer is None:
        return None
    from . import secrets_box

    label = f"peer:{peer.id}"
    try:
        token = secrets_box.decrypt(settings, peer.token_enc) if peer.token_enc else None
        digest = discover_digest(session, settings, art, peer, client, token)
        if digest is None:
            _tried(art, label, "not_on_peer")
            return None
        data = _peer_request(client, peer.url, digest, token, settings)
    except MediaError as exc:
        _tried(art, label, exc.code)
        return None
    except secrets_box.SecretsError:
        _tried(art, label, "peer_token")
        return None
    art.fetched_from = label
    return data


def _fetch_measured(kind: str, fetch: Any, settings: Settings) -> tuple[bytes, tuple[str, str, int, int]]:
    """Download and decode-verify one image, recording `atlas_media_operations_total{operation="fetch"}`."""
    from . import instruments as m

    started, outcome = time.monotonic(), "ok"
    try:
        data, source = fetch()
        m.media_bytes.inc(len(data), source=source)
        return data, validate_image(data, settings)
    except MediaError as exc:
        outcome = "deferred" if exc.deferred else exc.code
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        m.media_operations.inc(kind=kind, operation="fetch", outcome=outcome)
        m.media_duration.observe(time.monotonic() - started, kind=kind, operation="fetch")


def _store_measured(kind: str, settings: Settings, data: bytes, ext: str) -> str:
    from . import instruments as m

    started, outcome = time.monotonic(), "storage_error"
    try:
        digest = store(settings, data, ext)
        outcome = "ok"
        return digest
    finally:
        m.media_operations.inc(kind=kind, operation="store", outcome=outcome)
        m.media_duration.observe(time.monotonic() - started, kind=kind, operation="store")


def _from_group(session: Session, settings: Settings, art: Artwork, group_fetch: Any) -> bytes | None:
    """CONTRACT-C14 §3.6: the bytes from a member of this node's owner group (`assist.blob`), already checked
    against the row's digest; an answer that is not a valid image marks that member suspect."""
    if group_fetch is None:
        return None
    from . import assist

    try:
        data = group_fetch(session, art)
    except Exception as exc:  # noqa: BLE001 - the group is a fallback; a failure leaves the other sources
        log.info("artwork %s %s: no image from the group (%s)", art.canonical_id, art.kind, type(exc).__name__)
        return None
    if data is None:
        return None
    try:
        validate_image(data, settings)
    except MediaError as exc:
        helper = (art.fetched_from or "")[len(assist.GROUP_PREFIX):]
        if helper:
            assist.suspect(session, helper, "bad_image")
        _tried(art, "group", exc.code)
        return None
    return data


def cache_one(session: Session, settings: Settings, art: Artwork, client: httpx.Client,
              group_fetch: Any = None) -> bool:
    """Peer first (C13 §6.2), then the origin; the owner group (`group_fetch`, C14 §3.6) before the origin only
    with `group_assist_group_before_origin` (the full-trust preset), otherwise when the origin fails. A row that
    came from the group keeps its `group:<helper>` label wherever its bytes came from (it is group data)."""
    got_from: dict[str, str | None] = {"source": None}
    group_row = (art.fetched_from or "").startswith("group:")
    early = bool(getattr(settings, "group_assist_group_before_origin", False))

    def fetch() -> tuple[bytes, str]:
        got = _from_peer(session, settings, art, client)
        if got is not None:
            got_from["source"] = "peer"
            return got, "peer"
        if early and (got := _from_group(session, settings, art, group_fetch)) is not None:
            got_from["source"] = "group"
            return got, "group"
        agent, follow = fetch_options(settings, art.source)
        try:
            got = _request(client, art.url, settings, user_agent=agent, follow_hosts=follow,
                           agent_hosts=CONTACT_HOSTS.get(art.source, ()))
        except MediaError as exc:
            if exc.deferred or early or (fallback := _from_group(session, settings, art, group_fetch)) is None:
                raise
            got_from["source"] = "group"
            return fallback, "group"
        if not group_row:
            art.fetched_from = "origin"
        got_from["source"] = "origin"
        return got, "origin"

    try:
        data, (ext, ctype, w, h) = _fetch_measured("artwork", fetch, settings)
        digest = _store_measured("artwork", settings, data, ext)
        if got_from["source"] == "origin" and art.peer_digest and art.peer_digest != digest \
                and art.digest_provenance != "signed":
            # C13 §6.2 step 3: a digest a peer reported (unsigned) is a hint only. The origin's bytes differ (the
            # image was updated, or the peer is stale): keep them, and record that the digest is the origin's.
            log.info("artwork %s %s: origin bytes differ from the %s digest %s; stored as the origin's",
                     art.canonical_id, art.kind, art.digest_provenance, art.peer_digest[:12])
            art.peer_digest, art.digest_provenance = digest, "origin"
    except MediaError as exc:
        art.last_error = exc.code
        _tried(art, "origin", exc.code)
        if exc.deferred:  # the per-host limit: try again in a minute, without backing off
            art.next_try_at = now() + timedelta(minutes=1)
            return False
        art.fail_count += 1
        art.next_try_at = now() + timedelta(minutes=min(60 * 24 * 7, 5 * 2 ** min(art.fail_count, 12)))
        return False
    art.blob_hash, art.content_type, art.size_bytes = digest, ctype, len(data)
    measure(art, data, w, h)
    art.cached_at, art.fail_count, art.next_try_at, art.last_error = now(), 0, None, None
    from . import facets  # facets imports this module

    session.flush()
    facets.dedupe(session, settings, art.canonical_id)
    return True


def _proxied_sources(settings: Settings, sources: list[str]) -> list[str]:
    return [s for s in sources if policy(settings, s).fetch == "proxy"]


def cache_pending(maker: sessionmaker[Session], settings: Settings, client: httpx.Client | None = None,
                  group_fetch: Any = None) -> int:
    """Cache a bounded batch of uncached artwork; safe to call from several workers.

    Only rows whose source policy is `proxy` are downloaded (§12.5.5); uploads are already stored. Cached rows
    that were never measured (rows cached before migration 0035) are measured in the same pass."""
    done = 0
    own = client or httpx.Client(trust_env=False)  # never route through an environment proxy
    try:
        with maker() as session:
            sources = list(session.scalars(select(Artwork.source).distinct()))
            proxied = _proxied_sources(settings, sources)
            rows = session.scalars(
                select(Artwork).where(
                    Artwork.blob_hash.is_(None), Artwork.source.in_(proxied),
                    (Artwork.next_try_at.is_(None)) | (Artwork.next_try_at <= now()),
                ).order_by(Artwork.priority, Artwork.created_at).limit(settings.media_cache_batch)
            ).all() if proxied else []
            for art in rows:
                done += cache_one(session, settings, art, own, group_fetch)
                session.commit()
        done += cache_pending_achievement_icons(maker, settings, own)
        backfill(maker, settings, limit=settings.media_cache_batch)
    finally:
        if client is None:
            own.close()
    return done


def cache_achievement_icon(settings: Settings, ach: Achievement, which: str, client: httpx.Client) -> bool:
    """Fetch one achievement image ("icon" or "locked_icon") through the same SSRF-safe, validated store."""
    url = getattr(ach, f"{which}_url")
    try:
        data, (ext, _ctype, _w, _h) = _fetch_measured("achievement_icon",
                                                      lambda: (_request(client, url, settings), "origin"), settings)
        digest = _store_measured("achievement_icon", settings, data, ext)
    except MediaError as exc:
        if exc.deferred:
            setattr(ach, f"{which}_next_try_at", now() + timedelta(minutes=1))
            return False
        fails = getattr(ach, f"{which}_fail_count") + 1
        setattr(ach, f"{which}_fail_count", fails)
        setattr(ach, f"{which}_last_error", exc.code)
        setattr(ach, f"{which}_next_try_at", now() + timedelta(minutes=min(60 * 24 * 7, 5 * 2 ** min(fails, 12))))
        return False
    setattr(ach, f"{which}_blob_hash", digest)
    setattr(ach, f"{which}_fail_count", 0)
    setattr(ach, f"{which}_last_error", None)
    setattr(ach, f"{which}_next_try_at", None)
    return True


def cache_pending_achievement_icons(maker: sessionmaker[Session], settings: Settings,
                                    client: httpx.Client | None = None) -> int:
    """Achievement icons are media too (§12.5.5): only icons of `proxy` sources are downloaded."""
    done = 0
    own = client or httpx.Client(trust_env=False)
    try:
        with maker() as session:
            sources = list(session.scalars(select(Achievement.source).distinct()))
            proxied = _proxied_sources(settings, sources)
            if not proxied:
                return 0
            for which in ("icon", "locked_icon"):
                url, blob = getattr(Achievement, f"{which}_url"), getattr(Achievement, f"{which}_blob_hash")
                retry = getattr(Achievement, f"{which}_next_try_at")
                rows = session.scalars(select(Achievement).where(
                    url.is_not(None), blob.is_(None), (retry.is_(None)) | (retry <= now()),
                    Achievement.source.in_(proxied), Achievement.retired_at.is_(None),
                ).order_by(Achievement.id).limit(settings.media_cache_batch)).all()
                for ach in rows:
                    done += cache_achievement_icon(settings, ach, which, own)
                    session.commit()
    finally:
        if client is None:
            own.close()
    return done


def backfill(maker: sessionmaker[Session], settings: Settings, *, limit: int = 200,
             budget_s: float = 30.0) -> int:
    """Measure cached images that were stored before migration 0035 (size, aspect, dhash), then recompute the
    exact duplicates of their records. Bounded by `limit` rows and `budget_s` seconds per call; resumable because
    a measured row has `aspect` set. Rows whose file is gone are left for the cache to fetch again."""
    started, done = time.monotonic(), 0
    with maker() as session:
        rows = session.scalars(select(Artwork).where(Artwork.blob_hash.is_not(None), Artwork.aspect.is_(None),
                                                     Artwork.last_error.is_(None))
                               .order_by(Artwork.id).limit(limit)).all()
        touched: set[str] = set()
        for art in rows:
            if time.monotonic() - started > budget_s:
                break
            found = find_blob(settings, art.blob_hash or "")
            if found is None:
                if is_upload(settings, art.url):
                    # A local-only upload has no remote source to recover from: the file is simply gone.
                    art.last_error = "storage"
                else:
                    # Recoverable: the remote source may still be healthy. Clear the stale hash so
                    # `cache_pending` treats this row as uncached and fetches it again.
                    art.blob_hash = art.content_type = art.size_bytes = art.cached_at = None
                    art.fail_count, art.next_try_at, art.last_error = 0, None, None
                continue
            data = found[0].read_bytes()
            try:
                _ext, _ctype, w, h = validate_image(data, settings)
            except MediaError as exc:
                art.last_error = exc.code
                continue
            measure(art, data, w, h)
            touched.add(art.canonical_id)
            done += 1
        session.flush()
        from . import facets

        for cid in sorted(touched):
            facets.dedupe(session, settings, cid)
        session.commit()
    return done


def pending_backfill(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Artwork).where(
        Artwork.blob_hash.is_not(None), Artwork.aspect.is_(None), Artwork.last_error.is_(None))) or 0


def servable(session: Session, settings: Settings, digest: str) -> bool:
    """`/media/{digest}` serves a blob only while a row of a `proxy` source (or an upload, or the site logo)
    references it (§12.5.5): switching a provider to `redirect` or `none` stops serving its cached bytes."""
    if getattr(settings, "logo_hash", None) == digest:
        return True
    art = session.execute(select(Artwork.source, Artwork.url).where(Artwork.blob_hash == digest)).all()
    ach = session.execute(select(Achievement.source, Achievement.icon_url, Achievement.locked_icon_url,
                                 Achievement.icon_blob_hash).where(
        or_(Achievement.icon_blob_hash == digest, Achievement.locked_icon_blob_hash == digest))).all()
    if not art and not ach:
        # not referenced by any row: an upload awaiting review (proposal) or a file a curator just stored
        return True
    for source, url in art:
        if fetch_policy(settings, source, url) == "proxy":
            return True
    for source, icon_url, locked_url, icon_blob in ach:
        if fetch_policy(settings, source, icon_url if icon_blob == digest else locked_url) == "proxy":
            return True
    return False


def stats(session: Session) -> dict[str, int]:
    count = lambda *cond: session.scalar(select(func.count()).select_from(Artwork).where(*cond)) or 0  # noqa: E731
    return {"total": count(), "cached": count(Artwork.blob_hash.is_not(None)),
            "failing": count(Artwork.blob_hash.is_(None), Artwork.fail_count > 0)}


def retry_pending(session: Session) -> int:
    """Make every uncached image eligible for the next background pass immediately."""
    rows = session.scalars(select(Artwork).where(Artwork.blob_hash.is_(None))).all()
    for art in rows:
        art.fail_count, art.next_try_at = 0, None
    return len(rows)


def clear_cache(session: Session, settings: Settings) -> int:
    """Forget downloaded provider images and delete their files; uploads to this node are kept.

    Images stay listed and are cached again if caching is on. A file still used by an upload or the logo stays."""
    own = own_media_base(settings)
    cached = session.scalars(select(Artwork).where(Artwork.blob_hash.is_not(None), ~Artwork.url.startswith(own))).all()
    keep = {a.blob_hash for a in session.scalars(select(Artwork).where(Artwork.url.startswith(own)))}
    for ach in session.scalars(select(Achievement).where(
            Achievement.icon_blob_hash.is_not(None) | Achievement.locked_icon_blob_hash.is_not(None))):
        keep.update(h for h in (ach.icon_blob_hash, ach.locked_icon_blob_hash) if h)
    cleared = 0
    from . import instruments as m

    for art in cached:
        digest = art.blob_hash
        art.blob_hash = art.content_type = art.size_bytes = art.cached_at = None
        art.fail_count, art.next_try_at = 0, None
        art.duplicate_of = None
        cleared += 1
        if digest not in keep:
            keep.add(digest)  # one delete attempt per file
            found = find_blob(settings, digest)
            if found is not None:
                found[0].unlink(missing_ok=True)
    m.media_evictions.inc(cleared, reason="manual")
    return cleared


def storage_usage(settings: Settings, *, limit: int = 200_000) -> dict[str, Any]:
    """Bytes and files under ATLAS_MEDIA_DIR as this process sees it (a per-node directory, or a shared volume when
    several nodes mount the same one). Stops after `limit` files and says so instead of walking without bound."""
    root = Path(settings.media_dir)
    total = files = 0
    truncated = False
    if not root.is_dir():
        return {"bytes": 0, "files": 0, "truncated": False, "present": False}
    for path in root.rglob("*"):
        if files >= limit:
            truncated = True
            break
        try:
            if path.is_file() and not path.name.endswith(".part"):
                total += path.stat().st_size
                files += 1
        except OSError:
            continue
    return {"bytes": total, "files": files, "truncated": truncated, "present": True}


def describe_state(settings: Settings, art: Any) -> str:
    """The API's `state` for one artwork row (§12.6.1)."""
    fetch = fetch_policy(settings, art.source, art.url)
    if fetch == "none":
        return "blocked"
    if art.blob_hash:
        return "cached"
    if fetch == "redirect" or not settings.media_cache_enabled:
        return "not_fetched"
    return "failing" if art.fail_count else "pending"
