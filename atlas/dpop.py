"""DPoP proofs with Ed25519 keys (RFC 9449), for the node link (CONTRACT-C14 §6.3, §6.5).

A proof is a JWT whose header carries the public key (`{"typ": "dpop+jwt", "alg": "EdDSA", "jwk": OKP}`) and
whose claims are `{htm, htu, iat, jti}` plus `ath` (base64url SHA-256 of the access token) on resource requests.
The key's identity is its RFC 7638 thumbprint (`jkt`). The signing input of a JWT starts with `eyJ`, so a proof can
never collide with an `atlas-` signing domain (C10 §1.3).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SKEW = 300  # seconds: |now - iat| (§6.5)
MAX_LEN = 4096
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,64}\Z")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwk(public_b64: str) -> dict[str, str]:
    """The OKP JWK of a raw Ed25519 public key given in standard base64."""
    return {"kty": "OKP", "crv": "Ed25519", "x": b64u(base64.b64decode(public_b64, validate=True))}


def thumbprint(key: dict[str, Any]) -> str:
    """RFC 7638 thumbprint of an OKP key (RFC 8037 §2): SHA-256 over the required members in lexicographic order
    (`crv`, `kty`, `x`), no whitespace, base64url without padding."""
    if not isinstance(key, dict) or key.get("kty") != "OKP" or key.get("crv") != "Ed25519" \
            or not isinstance(key.get("x"), str):
        raise ValueError("an Ed25519 OKP key is required")
    canon = json.dumps({"crv": key["crv"], "kty": key["kty"], "x": key["x"]}, separators=(",", ":"),
                       sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


def jkt_of(public_b64: str) -> str:
    return thumbprint(jwk(public_b64))


def ath(access_token: str) -> str:
    return b64u(hashlib.sha256(access_token.encode()).digest())


def normal_htu(url: str) -> str:
    """The `htu` comparison form (RFC 9449 §4.3): scheme, host and path, without query or fragment."""
    p = urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path or '/'}"


def make_proof(private: Ed25519PrivateKey, htm: str, htu: str, *, access_token: str | None = None,
               iat: int | None = None, jti: str | None = None) -> str:
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    header = {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": {"kty": "OKP", "crv": "Ed25519", "x": b64u(raw)}}
    claims: dict[str, Any] = {"htm": htm.upper(), "htu": normal_htu(htu), "iat": int(time.time() if iat is None
                                                                                    else iat),
                              "jti": jti or secrets.token_urlsafe(24)}
    if access_token is not None:
        claims["ath"] = ath(access_token)
    h = b64u(json.dumps(header, separators=(",", ":")).encode())
    c = b64u(json.dumps(claims, separators=(",", ":")).encode())
    return f"{h}.{c}.{b64u(private.sign(f'{h}.{c}'.encode()))}"


class DpopError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Proof:
    jkt: str
    jti: str
    iat: int
    public_b64: str


def verify_proof(proof: str | None, *, htm: str, htu: str, access_token: str | None = None,
                 require_ath: bool = False, now: float | None = None) -> Proof:
    """Check one DPoP proof's form, signature, method, URL, time and `ath`. The caller checks the key (`jkt`)
    against the token and the link, and records `(jkt, jti)` once (a replay is a duplicate key)."""
    if not isinstance(proof, str) or not proof or len(proof) > MAX_LEN:
        raise DpopError("missing_proof")
    try:
        h, c, s = proof.split(".")
        header = json.loads(b64u_dec(h))
        claims = json.loads(b64u_dec(c))
        sig = b64u_dec(s)
    except (ValueError, TypeError) as exc:
        raise DpopError("malformed_proof") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise DpopError("malformed_proof")
    if header.get("typ") != "dpop+jwt" or header.get("alg") != "EdDSA" or set(header) != {"typ", "alg", "jwk"}:
        raise DpopError("bad_header")
    key = header.get("jwk")
    try:
        jkt = thumbprint(key)
        raw = b64u_dec(key["x"])
        if len(raw) != 32 or set(key) - {"kty", "crv", "x", "kid", "use", "alg"}:
            raise ValueError("bad key")
        Ed25519PublicKey.from_public_bytes(raw).verify(sig, f"{h}.{c}".encode())
    except (ValueError, TypeError, KeyError, InvalidSignature) as exc:
        raise DpopError("bad_signature") from exc
    if claims.get("htm") != htm.upper():
        raise DpopError("wrong_method")
    if not isinstance(claims.get("htu"), str) or normal_htu(claims["htu"]) != normal_htu(htu):
        raise DpopError("wrong_url")
    iat = claims.get("iat")
    t = time.time() if now is None else now
    if type(iat) is not int or abs(t - iat) > SKEW:
        raise DpopError("stale_proof")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not _JTI.match(jti):
        raise DpopError("bad_jti")
    if access_token is not None:
        if not isinstance(claims.get("ath"), str) or not secrets.compare_digest(claims["ath"], ath(access_token)):
            raise DpopError("bad_ath")
    elif require_ath or "ath" in claims:
        raise DpopError("bad_ath")
    return Proof(jkt=jkt, jti=jti, iat=iat, public_b64=base64.b64encode(raw).decode())
