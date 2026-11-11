"""Cryptography of owner federation groups (CONTRACT-C10 §1.3, §1.2, §1.9, §2.2-2.3, §3.2, §3.7).

Everything here is a pure function of its inputs: no database, no settings. It uses only `cryptography`
and `hashlib` (already dependencies). `signing.DOMAIN` and the feed message are not touched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .signing import DOMAIN as FEED_DOMAIN
from .signing import PRICE_DOMAIN, body_hash, canonical_json, key_id_for, message_parts_ok, sign_message, verify_message

# ---- domains (§1.3) ---------------------------------------------------------------------------------------

DOMAINS: dict[str, bytes] = {
    "group.genesis": b"atlas-grp-genesis-v1\n",
    "group.succession": b"atlas-grp-succession-v1\n",
    "group.succession_cancel": b"atlas-grp-succession-cancel-v1\n",
    "group.root_moved": b"atlas-grp-rootmove-v1\n",
    "group.cert": b"atlas-grp-cert-v1\n",
    "member": b"atlas-grp-member-v1\n",
    "group.crl": b"atlas-grp-crl-v1\n",
    "group.grant": b"atlas-grp-grant-v1\n",
    "group.view": b"atlas-grp-view-v1\n",
    "change": b"atlas-grp-change-v1\n",
    "group.assertion": b"atlas-grp-assert-v1\n",
    "group.authreq": b"atlas-grp-authreq-v1\n",
    "group.start": b"atlas-grp-start-v1\n",
    "enrol": b"atlas-grp-enrol-v1\n",
    "request": b"atlas-grp-req-v1\n",
    "response": b"atlas-grp-resp-v1\n",
    "enrol_mac": b"atlas-grp-enrol-mac-v1\n",
    "seal": b"atlas-grp-seal-v1\n",
    "sealreq": b"atlas-grp-sealreq-v1\n",
    "root_wrap": b"atlas-grp-root-v1\n",
    "recovery_file": b"atlas-grp-recovery-v1\n",
    # CONTRACT-C14 §1.4: helper-signed bundles and lookup attestations (C14-2), and contribution state shared in the
    # owner group (H67, §7.7). Both use the C10 envelope; no consumer of group state accepts either type.
    "group.assist": b"atlas-grp-assist-v1\n",
    "group.contrib_state": b"atlas-grp-contrib-v1\n",
    # C14-4 (super group): one user write made on a member, replicated to every member. No consumer of group state
    # accepts this type; only `atlas.super_group` reads it.
    "group.write": b"atlas-grp-write-v1\n",
}
SAS_INFO = b"atlas-grp-sas-v1"  # HKDF info only, never signed
OBJECT_TYPES = ("group.genesis", "group.succession", "group.succession_cancel", "group.root_moved", "group.cert",
                "group.crl", "group.grant", "group.view", "group.assist", "group.contrib_state", "group.write")
SEAL_SUITE = "x25519-chacha20poly1305-v1"
SKEW = 300  # seconds (§1.3)
SUPPORTED_VERSION = 1


def all_domains() -> list[bytes]:
    from .link_crypto import domains as link_domains  # CONTRACT-C14 §1.4: the node link's own domains

    return [FEED_DOMAIN, PRICE_DOMAIN, *DOMAINS.values(), *link_domains()]


def prefix_free(domains: list[bytes]) -> bool:
    return not any(a != b and b.startswith(a) for a in domains for b in domains) and len(set(domains)) == len(domains)


class GroupCryptoError(ValueError):
    """A group object, file or message is malformed or does not verify."""


class WrongPassphrase(GroupCryptoError):
    pass


class WeakPassphrase(GroupCryptoError):
    pass


# ---- small helpers -----------------------------------------------------------------------------------------

def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def unb64(text: str, length: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(text, validate=True)
    except (TypeError, ValueError) as exc:
        raise GroupCryptoError("invalid base64") from exc
    if length is not None and len(raw) != length:
        raise GroupCryptoError("value has the wrong length")
    return raw


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamp(dt: datetime | None = None) -> str:
    """RFC 3339 UTC with a Z suffix and millisecond precision at most (§1.3)."""
    dt = (dt or datetime.now(UTC)).astimezone(UTC)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    millis = dt.microsecond // 1000
    return f"{text}.{millis:03d}Z" if millis else f"{text}Z"


_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z\Z")


def parse_time(text: Any) -> datetime:
    if not isinstance(text, str) or not _TIME.match(text):
        raise GroupCryptoError("times are RFC 3339 UTC with a Z suffix")
    return datetime.fromisoformat(text[:-1]).replace(tzinfo=UTC)


def ed_public_b64(private: Ed25519PrivateKey) -> str:
    return b64(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def ed_private_raw(private: Ed25519PrivateKey) -> bytes:
    return private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())


def ed_from_raw(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def key_id_of(public_b64: str) -> str:
    return key_id_for(unb64(public_b64, 32))


def x_public_b64(private: X25519PrivateKey) -> str:
    return b64(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def x_private_raw(private: X25519PrivateKey) -> bytes:
    return private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())


def x_from_raw(raw: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(raw)


# ---- envelope (§1.3) ---------------------------------------------------------------------------------------

MAX_SAFE_INT = 2 ** 53


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def canonical_ok(value: Any) -> bool:
    """R1-10: signed group bodies hold no floats, only integers within +/-2^53 and NFC strings (keys included), so
    every implementation hashes the same bytes. `canonical_json` itself is shared with the feed and unchanged."""
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, int):
        return -MAX_SAFE_INT <= value <= MAX_SAFE_INT
    if isinstance(value, float):
        return False
    if isinstance(value, str):
        return unicodedata.is_normalized("NFC", value)
    if isinstance(value, list):
        return all(canonical_ok(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and canonical_ok(k) and canonical_ok(v) for k, v in value.items())
    return False


def object_parts(obj: dict) -> list[str]:
    signer = obj["signer"]
    return [obj["group_id"], signer["kind"], signer["id"], signer["key_id"], obj["id"], obj["body_hash"]]


def domain_for(t: str, v: Any) -> bytes:
    """The signing domain of an object of type `t` at envelope version `v` (C10 §C10.17 R1-10).

    The `...-v1` domains are the `_v: 1` domains. Any other version has no domain until a v2 table exists, so a
    v2 envelope is never verified (or signed) under a v1 domain. The v1 bytes are unchanged."""
    if type(v) is not int or v != 1:
        raise GroupCryptoError(f"no signing domain for {t} at envelope version {v!r}")
    return DOMAINS[t]


def make_object(t: str, group_id: str, oid: str, body: dict, signer: dict, private: Ed25519PrivateKey) -> dict:
    if not canonical_ok(body):
        raise GroupCryptoError("a signed group body holds no floats, only safe integers and NFC strings")
    obj = {"t": t, "_v": 1, "group_id": group_id, "id": oid, "body": body,
           "signer": {"kind": signer["kind"], "id": signer["id"], "key_id": signer["key_id"]},
           "body_hash": body_hash(body)}
    obj["sig"] = sign_message(private, domain_for(t, obj["_v"]), object_parts(obj))
    return obj


def co_sign(obj: dict, private: Ed25519PrivateKey) -> str:
    """Another key's signature over exactly the message of `obj` (genesis PoPs, succession co-signatures)."""
    return sign_message(private, domain_for(obj["t"], obj.get("_v")), object_parts(obj))


def well_formed(obj: Any, group_id: str | None = None) -> bool:
    """Envelope shape, known type, version, printable signed parts and a body that matches its hash."""
    try:
        if not isinstance(obj, dict) or obj.get("t") not in OBJECT_TYPES or type(obj.get("_v")) is not int:
            return False
        if not 1 <= obj["_v"] <= SUPPORTED_VERSION or not isinstance(obj.get("body"), dict):
            return False
        signer = obj["signer"]
        if not isinstance(signer, dict) or signer.get("kind") not in ("root", "recovery", "node"):
            return False
        if group_id is not None and obj["group_id"] != group_id:
            return False
        if not message_parts_ok(object_parts(obj)) or not isinstance(obj.get("sig"), str):
            return False
        if not canonical_ok(obj["body"]):
            return False
        return body_hash(obj["body"]) == obj["body_hash"]
    except (KeyError, TypeError):
        return False


def verify_object(obj: dict, public_b64: str, signature: str | None = None) -> bool:
    if not well_formed(obj):
        return False
    try:
        domain = domain_for(obj["t"], obj["_v"])
    except GroupCryptoError:
        return False
    return verify_message(public_b64, domain, object_parts(obj), signature or obj["sig"])


# ---- member proof of possession (§1.3) ---------------------------------------------------------------------

MEMBER_FIELDS = ("node_id", "root_public_key", "box_public_key", "url", "name", "rev")


def member_digest(member: dict) -> str:
    signed = {k: v for k, v in member.items() if k != "url_verified"}
    return sha256_hex(canonical_json(signed))


def member_pop(private: Ed25519PrivateKey, group_id: str, member: dict, key_id: str) -> dict:
    parts = [group_id, member["node_id"], key_id, member_digest(member)]
    return {"key_id": key_id, "sig": sign_message(private, DOMAINS["member"], parts)}


def verify_member_pop(public_b64: str, group_id: str, member: dict, pop: dict) -> bool:
    try:
        parts = [group_id, member["node_id"], pop["key_id"], member_digest(member)]
        return verify_message(public_b64, DOMAINS["member"], parts, pop["sig"])
    except (KeyError, TypeError):
        return False


# ---- passphrase policy (§1.2) -------------------------------------------------------------------------------

PASSPHRASE_MIN = 14
_DATA = Path(__file__).resolve().parent / "data" / "common_passwords.txt"
_WALKS = ("qwertyuiopasdfghjklzxcvbnm", "1234567890", "abcdefghijklmnopqrstuvwxyz", "qazwsxedcrfvtgbyhnujmikolp",
          "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p", "zxcvbnmasdfghjklqwertyuiop", "0987654321")


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    words = set()
    for line in _DATA.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            words.add(line)
    return frozenset(words)


def _classes(text: str) -> int:
    return sum(bool(re.search(pattern, text)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))


def _core(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _is_common(text: str) -> bool:
    words = common_passwords()
    lowered = text.lower()
    core = _core(text)
    if lowered in words or core in words:
        return True
    stripped = re.sub(r"[0-9]+$", "", core)  # "password1234567" style
    if stripped and stripped in words:
        return True
    for size in range(1, len(core) // 2 + 1):  # a repeated common word or pattern ("passwordpassword")
        if len(core) % size == 0 and core[:size] * (len(core) // size) == core and (
                size < 4 or core[:size] in words):
            return True
    for walk in _WALKS:  # keyboard and alphabet walks, forwards or backwards
        if len(core) >= 8 and (core in walk * 2 or core in (walk * 2)[::-1]):
            return True
    return False


def check_passphrase(passphrase: str, names: list[str] | tuple[str, ...] = ()) -> None:
    """The root and recovery-file passphrase policy (§1.2). Raises WeakPassphrase with the reason."""
    if not isinstance(passphrase, str) or len(passphrase) < PASSPHRASE_MIN:
        raise WeakPassphrase(f"use at least {PASSPHRASE_MIN} characters")
    if len(passphrase) > 1024:
        raise WeakPassphrase("use at most 1024 characters")
    if _classes(passphrase) < 2 and len(passphrase) < 20:
        raise WeakPassphrase("use at least 20 characters, or mix letters with digits or symbols")
    lowered = passphrase.lower()
    for name in names:
        name = (name or "").strip().lower()
        if len(name) >= 3 and (name in lowered or _core(name) and _core(name) in _core(passphrase)):
            raise WeakPassphrase("do not use the user, node, group or host name in the passphrase")
    if _is_common(passphrase):
        raise WeakPassphrase("that passphrase is on the list of common passwords or patterns")


# ---- passphrase wrapping (§1.2, §1.9) -----------------------------------------------------------------------

SCRYPT_N = 2 ** 17  # tests may lower this for speed; the stored parameters are what unwrap uses
SCRYPT_R, SCRYPT_P = 8, 1
SCRYPT_MIN_N = 2 ** 14  # the weakest stored parameters unwrap accepts (tests lower it with SCRYPT_N)


def _kdf(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=2 * 128 * n * r + 2 ** 20,
                          dklen=32)


def wrap(secret: bytes, passphrase: str, aad: bytes) -> dict:
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = _kdf(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ct = AESGCM(key).encrypt(nonce, secret, aad)
    return {"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": b64(salt), "aead": "aes-256-gcm",
            "nonce": b64(nonce), "ct": b64(ct)}


def unwrap(wrapped: dict, passphrase: str, aad: bytes) -> bytes:
    try:
        if wrapped["kdf"] != "scrypt" or wrapped["aead"] != "aes-256-gcm":
            raise GroupCryptoError("unknown key wrapping")
        n, r, p = int(wrapped["n"]), int(wrapped["r"]), int(wrapped["p"])
        if n < SCRYPT_MIN_N or n > 2 ** 20 or r > 16 or p > 4:
            raise GroupCryptoError("unsupported key-wrapping parameters")
        key = _kdf(passphrase, unb64(wrapped["salt"], 16), n, r, p)
        return AESGCM(key).decrypt(unb64(wrapped["nonce"], 12), unb64(wrapped["ct"]), aad)
    except InvalidTag:
        raise WrongPassphrase("wrong passphrase") from None
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GroupCryptoError):
            raise
        raise GroupCryptoError("malformed wrapped key") from exc


def root_aad(group_id: str) -> bytes:
    return DOMAINS["root_wrap"] + group_id.encode()


def wrap_root(root: Ed25519PrivateKey, passphrase: str, group_id: str) -> dict:
    return wrap(ed_private_raw(root), passphrase, root_aad(group_id))


def unwrap_root(wrapped: dict, passphrase: str, group_id: str) -> Ed25519PrivateKey:
    raw = unwrap(wrapped, passphrase, root_aad(group_id))
    if len(raw) != 32:
        raise GroupCryptoError("malformed wrapped root key")
    return ed_from_raw(raw)


# ---- recovery files (§1.9) ----------------------------------------------------------------------------------

RECOVERY_FORMAT = "atlas-group-recovery"


def recovery_aad(group_id: str, recovery_key_id: str) -> bytes:
    return DOMAINS["recovery_file"] + group_id.encode() + b"\n" + recovery_key_id.encode()


RECOVERY_VERSION = 2  # v2 (C10 §C10.17 R1-5): G is wrapped under the root passphrase inside the file


def make_recovery_file(genesis: dict, succession: list[dict], recovery: Ed25519PrivateKey | None,
                       root: Ed25519PrivateKey | None, passphrase: str, created_at: str | None = None, *,
                       root_passphrase: str | None = None, root_wrapped: dict | None = None) -> dict:
    """A passphrase-wrapped file holding a recovery key, the root key G, or both (`export-recovery` without an
    existing recovery file holds G only; its key id is then "root").

    Format v2 (R1-5): G is never stored raw. It is carried as `root_wrapped`, the same `wrap_root` structure as
    the node's sealed root (scrypt + AES-GCM under the **root** passphrase, AAD the root domain + group id),
    so cracking the file passphrase yields R only. Pass `root` with `root_passphrase`, or an already wrapped G
    (`root_wrapped`, one wrap and two copies at creation)."""
    group_id = genesis["group_id"]
    if root is not None and root_wrapped is None:
        if not root_passphrase:
            raise GroupCryptoError("a recovery file holding the root key needs the root passphrase to wrap it (v2)")
        root_wrapped = wrap_root(root, root_passphrase, group_id)
    if recovery is None and root_wrapped is None:
        raise GroupCryptoError("a recovery file holds a recovery key, the root key, or both")
    rid = key_id_of(ed_public_b64(recovery)) if recovery is not None else "root"
    plain = canonical_json({"recovery_private": b64(ed_private_raw(recovery)) if recovery is not None else None,
                            "root_wrapped": root_wrapped})
    contains = (["recovery_key"] if recovery is not None else []) + (["root_key_wrapped"] if root_wrapped else [])
    return {"format": RECOVERY_FORMAT, "v": RECOVERY_VERSION, "group_id": group_id, "name": genesis["body"]["name"],
            "created_at": created_at or stamp(), "genesis": genesis, "succession": succession,
            "recovery_key_id": rid, "contains": contains, "wrapped": wrap(plain, passphrase, recovery_aad(group_id, rid))}


@dataclass
class RecoveryKeys:
    group_id: str
    recovery: Ed25519PrivateKey | None
    root: Ed25519PrivateKey | None  # raw G: only from a v1 file (the UI asks for a re-export)
    genesis: dict
    succession: list[dict]
    root_wrapped: dict | None = None  # v2: G wrapped under the root passphrase
    version: int = RECOVERY_VERSION

    @property
    def holds_root(self) -> bool:
        return self.root is not None or self.root_wrapped is not None

    @property
    def legacy_raw_root(self) -> bool:
        """A v1 file that holds G unprotected: a cracked file passphrase is a full, delay-free takeover."""
        return self.root is not None


def recovery_root(keys: RecoveryKeys, root_passphrase: str | None) -> Ed25519PrivateKey:
    """G from an opened recovery file: raw from a v1 file, unwrapped with the root passphrase from a v2 file."""
    if keys.root is not None:
        return keys.root
    if keys.root_wrapped is None:
        raise GroupCryptoError("this recovery file holds no root key")
    if not root_passphrase:
        raise WrongPassphrase("this recovery file holds the root key wrapped under the root passphrase")
    return unwrap_root(keys.root_wrapped, root_passphrase, keys.group_id)


def open_recovery_file(data: dict, passphrase: str) -> RecoveryKeys:
    try:
        version = data.get("v")
        if data.get("format") != RECOVERY_FORMAT or version not in (1, 2):
            raise GroupCryptoError("not an Atlas group recovery file")
        group_id, rid = data["group_id"], data["recovery_key_id"]
        plain = json.loads(unwrap(data["wrapped"], passphrase, recovery_aad(group_id, rid)))
        recovery = ed_from_raw(unb64(plain["recovery_private"], 32)) if plain.get("recovery_private") else None
        root = root_wrapped = None
        if version == 1:
            root = ed_from_raw(unb64(plain["root_private"], 32)) if plain.get("root_private") else None
        else:
            if plain.get("root_private"):
                raise GroupCryptoError("a v2 recovery file never holds the raw root key")
            root_wrapped = plain.get("root_wrapped")
            if root_wrapped is not None and not isinstance(root_wrapped, dict):
                raise GroupCryptoError("malformed recovery file")
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise GroupCryptoError("malformed recovery file") from exc
    if recovery is None and ((root is None and root_wrapped is None) or rid != "root"):
        raise GroupCryptoError("malformed recovery file")
    if recovery is not None and key_id_of(ed_public_b64(recovery)) != rid:
        raise GroupCryptoError("the recovery file's key does not match its key id")
    return RecoveryKeys(group_id, recovery, root, data["genesis"], list(data.get("succession") or []),
                        root_wrapped=root_wrapped, version=version)


# ---- enrolment: invite code, transcript, SAS (§2.2, §2.3) ---------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_PREFIX = "AG1-"
CODE_BYTES = 43


def _b32(raw: bytes) -> str:
    bits = len(raw) * 8
    chars = -(-bits // 5)
    value = int.from_bytes(raw, "big") << (chars * 5 - bits)
    return "".join(_CROCKFORD[(value >> (5 * (chars - 1 - i))) & 31] for i in range(chars))


def _unb32(text: str, nbytes: int) -> bytes:
    chars = -(-nbytes * 8 // 5)
    if len(text) != chars:
        raise GroupCryptoError("the code has the wrong length")
    value = 0
    for ch in text:
        idx = _CROCKFORD.find(ch)
        if idx < 0:
            raise GroupCryptoError("the code holds a character that is not in the code alphabet")
        value = value * 32 + idx
    extra = chars * 5 - nbytes * 8
    if value & ((1 << extra) - 1):
        raise GroupCryptoError("the code is mistyped")
    return (value >> extra).to_bytes(nbytes, "big")


@dataclass(frozen=True)
class Invite:
    group_id: str
    inviter: str
    invite_id: str
    k: bytes


def encode_code(invite: Invite) -> str:
    payload = (b"\x01" + bytes.fromhex(invite.group_id) + bytes.fromhex(invite.inviter)
               + bytes.fromhex(invite.invite_id) + invite.k)
    raw = payload + hashlib.sha256(payload).digest()[:2]
    text = _b32(raw)
    return CODE_PREFIX + "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def decode_code(code: str) -> Invite:
    """Parse and checksum an invite code before any network call (§2.2)."""
    text = (code or "").strip().upper()
    if not text.startswith(CODE_PREFIX):
        raise GroupCryptoError("an invite code starts with AG1-")
    body = re.sub(r"[\s-]", "", text[len(CODE_PREFIX):]).replace("O", "0").replace("I", "1").replace("L", "1")
    raw = _unb32(body, CODE_BYTES)
    payload, check = raw[:-2], raw[-2:]
    if not hmac.compare_digest(hashlib.sha256(payload).digest()[:2], check) or payload[0] != 1:
        raise GroupCryptoError("the code is mistyped (checksum mismatch)")
    return Invite(payload[1:9].hex(), payload[9:17].hex(), payload[17:25].hex(), payload[25:41])


def qr_payload(url: str, code: str) -> dict:
    return {"v": 1, "type": "atlas-group-invite", "url": url, "code": code}


def lp(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def transcript_hash(t1: bytes, t2: bytes) -> str:
    return sha256_hex(lp(t1) + lp(t2))


def commitment(nonce_j: bytes) -> str:
    return sha256_hex(DOMAINS["enrol"] + b"commit\n" + nonce_j)


def enrol_pop(private: Ed25519PrivateKey, role: str, *parts: str) -> str:
    return sign_message(private, DOMAINS["enrol"], [role, *parts])


def verify_enrol_pop(public_b64: str, role: str, signature: str, *parts: str) -> bool:
    return verify_message(public_b64, DOMAINS["enrol"], [role, *parts], signature)


def enrol_mac(k: bytes, role: str, data: bytes) -> str:
    return hmac.new(k, DOMAINS["enrol_mac"] + role.encode() + b"\n" + data, hashlib.sha256).hexdigest()


def mac_ok(k: bytes, role: str, data: bytes, mac: Any) -> bool:
    return isinstance(mac, str) and hmac.compare_digest(enrol_mac(k, role, data), mac)


def sas(k: bytes, transcript: str, nonce_j: bytes) -> str:
    """12 decimal digits from a 40-bit HKDF output (commit-then-reveal, §2.3)."""
    salt = hashlib.sha256(bytes.fromhex(transcript) + nonce_j).digest()
    raw = HKDF(algorithm=hashes.SHA256(), length=5, salt=salt, info=SAS_INFO).derive(k)
    digits = f"{int.from_bytes(raw, 'big') % 10 ** 12:012d}"
    return f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"


# ---- sealing (§3.2, §3.7) -----------------------------------------------------------------------------------

def _hkdf(shared: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(shared)


def _request_info(group_id: str, server_node: str, epk: str) -> bytes:
    return DOMAINS["sealreq"] + f"{group_id}\n{server_node}\n{epk}".encode()


def seal_request(server_box_public: str, group_id: str, server_node: str, inner: dict) -> dict:
    eph = X25519PrivateKey.generate()
    epk = x_public_b64(eph)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(unb64(server_box_public, 32)))
    info = _request_info(group_id, server_node, epk)
    nonce = secrets.token_bytes(12)
    ct = ChaCha20Poly1305(_hkdf(shared, b"\0" * 32, info)).encrypt(nonce, canonical_json(inner), info)
    return {"sealed_req": SEAL_SUITE, "epk": epk, "nonce": b64(nonce), "ct": b64(ct)}


def open_request(box_keys: list[X25519PrivateKey], group_id: str, node_id: str, body: Any) -> dict:
    """Decrypt a sealed request with this node's box key (and the previous one during a rekey window)."""
    if not isinstance(body, dict) or body.get("sealed_req") != SEAL_SUITE:
        raise GroupCryptoError("not a sealed request")
    try:
        epk_raw = unb64(body["epk"], 32)
        nonce, ct = unb64(body["nonce"], 12), unb64(body["ct"])
    except (KeyError, TypeError) as exc:
        raise GroupCryptoError("malformed sealed request") from exc
    info = _request_info(group_id, node_id, body["epk"])
    for key in box_keys:
        try:
            shared = key.exchange(X25519PublicKey.from_public_bytes(epk_raw))
            plain = ChaCha20Poly1305(_hkdf(shared, b"\0" * 32, info)).decrypt(nonce, ct, info)
            inner = json.loads(plain)
            if not isinstance(inner, dict):
                raise GroupCryptoError("sealed request is not an object")
            return inner
        except (InvalidTag, ValueError):
            continue
    raise GroupCryptoError("sealed request does not decrypt")


def dummy_open(body: Any) -> None:
    """The same X25519 exchange and AEAD attempt a member makes, for nodes in no group (uniform 404, §3.2)."""
    key = X25519PrivateKey.generate()
    try:
        epk = unb64(body.get("epk", ""), 32) if isinstance(body, dict) else secrets.token_bytes(32)
        shared = key.exchange(X25519PublicKey.from_public_bytes(epk))
        ChaCha20Poly1305(_hkdf(shared, b"\0" * 32, b"atlas-grp-sealreq-v1\n")).decrypt(
            secrets.token_bytes(12), unb64(body.get("ct", "")) if isinstance(body, dict) else b"\0" * 16, None)
    except Exception:
        pass


def _response_info(group_id: str, server_node: str, requester_node: str, epk: str) -> bytes:
    return DOMAINS["seal"] + f"{group_id}\n{server_node}\n{requester_node}\n{epk}".encode()


def seal_response(requester_box_public: str, request_nonce: bytes, group_id: str, server_node: str,
                  requester_node: str, response: dict) -> dict:
    eph = X25519PrivateKey.generate()
    epk = x_public_b64(eph)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(unb64(requester_box_public, 32)))
    info = _response_info(group_id, server_node, requester_node, epk)
    nonce = secrets.token_bytes(12)
    ct = ChaCha20Poly1305(_hkdf(shared, request_nonce, info)).encrypt(nonce, canonical_json(response), info)
    return {"sealed": SEAL_SUITE, "epk": epk, "nonce": b64(nonce), "ct": b64(ct)}


def open_response(box_keys: list[X25519PrivateKey], request_nonce: bytes, group_id: str, server_node: str,
                  requester_node: str, body: Any) -> dict:
    if not isinstance(body, dict) or body.get("sealed") != SEAL_SUITE:
        raise GroupCryptoError("the response is not sealed")
    try:
        epk_raw, nonce, ct = unb64(body["epk"], 32), unb64(body["nonce"], 12), unb64(body["ct"])
    except (KeyError, TypeError) as exc:
        raise GroupCryptoError("malformed sealed response") from exc
    info = _response_info(group_id, server_node, requester_node, body["epk"])
    for key in box_keys:
        try:
            shared = key.exchange(X25519PublicKey.from_public_bytes(epk_raw))
            plain = ChaCha20Poly1305(_hkdf(shared, request_nonce, info)).decrypt(nonce, ct, info)
            return json.loads(plain)
        except (InvalidTag, ValueError):
            continue
    raise GroupCryptoError("sealed response does not decrypt")


# ---- request and response signatures (§3.2) ----------------------------------------------------------------

CALL_PATH = "/api/v2/group/call"


def request_parts(inner: dict) -> list[str]:
    return [inner["g"], inner["n"], inner["aud"], inner["k"], str(inner["t"]), inner["r"], inner["proto"], "POST",
            CALL_PATH, inner["op"], sha256_hex(canonical_json(inner["params"]))]


def sign_request(private: Ed25519PrivateKey, inner: dict) -> dict:
    return {**inner, "s": sign_message(private, DOMAINS["request"], request_parts(inner))}


def verify_request(public_b64: str, inner: dict) -> bool:
    try:
        return verify_message(public_b64, DOMAINS["request"], request_parts(inner), inner["s"])
    except (KeyError, TypeError):
        return False


def response_parts(group_id: str, server_node: str, key_id: str, requester: str, nonce: str, op: str,
                   params: dict, outcome: dict) -> list[str]:
    return [group_id, server_node, key_id, requester, nonce, "POST", CALL_PATH, op,
            sha256_hex(canonical_json(params)), sha256_hex(canonical_json(outcome))]


def enrol_request_nonce(transcript: str) -> bytes:
    return hashlib.sha256(bytes.fromhex(transcript)).digest()[:16]
