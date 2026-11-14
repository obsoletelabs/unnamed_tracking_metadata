"""OpenID Connect between Atlas nodes: provider (authorization-code + PKCE, EdDSA id tokens) and relying party."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import accounts, secrets_box
from .models import (
    NodeState,
    OidcAccess,
    OidcClient,
    OidcCode,
    OidcIssuer,
    OidcLink,
    OidcState,
    User,
    now,
)
from .service import AtlasError, Ctx
from .signing import NodeIdentity, key_id_for

CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
ID_TTL = timedelta(minutes=10)
STATE_TTL = timedelta(minutes=10)
SCOPES = {"openid", "profile", "email"}
_USERNAME_CLEAN = re.compile(r"[^a-z0-9_-]")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_challenge(verifier: str) -> str:
    return b64u(hashlib.sha256(verifier.encode()).digest())


# ---- JWT (EdDSA only) ---------------------------------------------------------------------------------

def _refusal(message: str, reason: str) -> AtlasError:
    """An AtlasError carrying a bounded metrics outcome (instruments.IDENTITY_OUTCOMES / TOKEN_OUTCOMES)."""
    exc = AtlasError(message)
    exc.metric_reason = reason  # type: ignore[attr-defined]
    return exc


def sign_jwt(identity: NodeIdentity, claims: dict[str, Any]) -> str:
    header = b64u(json.dumps({"alg": "EdDSA", "typ": "JWT", "kid": identity.node_id}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":"), default=str).encode())
    sig = identity.private.sign(f"{header}.{body}".encode())
    return f"{header}.{body}.{b64u(sig)}"


class TokenError(Exception):
    pass


def verify_jwt(token: str, *, public_key_b64: str, kid: str, issuer: str, audience: str, nonce: str | None,
               clock=None) -> dict[str, Any]:
    """Verify an id token against a pinned key. Anything unexpected is an error; `alg` is never negotiated."""
    try:
        header_b, body_b, sig_b = token.split(".")
        header = json.loads(b64u_dec(header_b))
        claims = json.loads(b64u_dec(body_b))
        sig = b64u_dec(sig_b)
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed token") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError("malformed token")
    if header.get("alg") != "EdDSA" or header.get("kid") != kid:
        raise TokenError("unexpected algorithm or key id")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(sig, f"{header_b}.{body_b}".encode())
    except (InvalidSignature, ValueError) as exc:
        raise TokenError("bad signature") from exc
    t = (clock or now)().timestamp()
    if claims.get("iss") != issuer:
        raise TokenError("wrong issuer")
    aud = claims.get("aud")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise TokenError("wrong audience")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= t:
        raise TokenError("token expired")
    if not isinstance(claims.get("iat"), (int, float)) or claims["iat"] > t + 60:
        raise TokenError("token issued in the future")
    if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise TokenError("nonce mismatch")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise TokenError("missing subject")
    return claims


# ---- RS256 key (for applications that only accept RSA) ---------------------------------------------

def rsa_key(session: Session, ctx: Ctx):
    """This node's RSA signing key, created on first use and kept (as PEM) beside the Ed25519 node key."""
    cached = getattr(ctx, "_rsa", None)
    if cached is not None:
        return cached
    row = session.get(NodeState, "oidc_rsa_key")
    if row is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        row = NodeState(key="oidc_rsa_key", value=pem)
        session.add(row)
        session.flush()
    key = serialization.load_pem_private_key(row.value.encode(), password=None)
    numbers = key.public_key().public_numbers()
    n = b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big"))
    e = b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big"))
    kid = hashlib.sha256(f"{n}.{e}".encode()).hexdigest()[:16]
    ctx._rsa = (key, kid, {"kty": "RSA", "n": n, "e": e, "kid": kid, "use": "sig", "alg": "RS256"})  # type: ignore[attr-defined]
    return ctx._rsa  # type: ignore[attr-defined]


def sign_jwt_rs256(session: Session, ctx: Ctx, claims: dict[str, Any]) -> str:
    key, kid, _ = rsa_key(session, ctx)
    header = b64u(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":"), default=str).encode())
    sig = key.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{b64u(sig)}"


# ---- provider -----------------------------------------------------------------------------------------

def discovery(ctx: Ctx) -> dict[str, Any]:
    base = ctx.settings.public_url
    return {
        "issuer": base, "authorization_endpoint": f"{base}/oidc/authorize", "token_endpoint": f"{base}/oidc/token",
        "userinfo_endpoint": f"{base}/oidc/userinfo", "jwks_uri": f"{base}/oidc/jwks",
        "response_types_supported": ["code"], "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256", "EdDSA"], "scopes_supported": sorted(SCOPES),
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "grant_types_supported": ["authorization_code"],
        "claims_supported": ["sub", "preferred_username", "email", "email_verified", "atlas_reputation"],
    }


def jwks(session: Session, ctx: Ctx) -> dict[str, Any]:
    raw = base64.b64decode(ctx.identity.public_b64)
    ed = {"kty": "OKP", "crv": "Ed25519", "x": b64u(raw), "kid": ctx.identity.node_id, "use": "sig", "alg": "EdDSA"}
    return {"keys": [rsa_key(session, ctx)[2], ed]}


def valid_redirect(uri: str) -> bool:
    p = urlsplit(uri)
    if p.fragment or p.username or p.password or not p.hostname or len(uri) > 500:
        return False
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"))


def create_client(session: Session, name: str, redirect_uris: list[str], require_pkce: bool = True,
                  id_token_alg: str = "RS256") -> tuple[OidcClient, str]:
    uris = [u.strip() for u in redirect_uris if u.strip()]
    if not name.strip() or not uris or len(uris) > 10 or not all(valid_redirect(u) for u in uris):
        raise AtlasError("give a name and 1-10 redirect addresses (https, or http://localhost for testing)")
    if id_token_alg not in ("RS256", "EdDSA"):
        raise AtlasError("id_token_alg must be RS256 or EdDSA")
    secret = secrets.token_urlsafe(32)
    row = OidcClient(client_id="atlas_" + secrets.token_hex(8), name=name.strip()[:80], secret_hash=sha(secret),
                     redirect_uris=uris, require_pkce=require_pkce, id_token_alg=id_token_alg)
    session.add(row)
    session.flush()
    return row, secret


def authenticate_client(session: Session, client_id: str, secret: str) -> OidcClient | None:
    row = session.get(OidcClient, client_id or "")
    if row is None or not row.enabled or not hmac.compare_digest(row.secret_hash, sha(secret or "")):
        return None
    return row


def check_authorize(session: Session, params: dict[str, str]) -> tuple[OidcClient, str]:
    """Validate an authorization request. Raises AtlasError for problems that must NOT redirect back."""
    client = session.get(OidcClient, params.get("client_id", ""))
    uri = params.get("redirect_uri", "")
    if client is None or not client.enabled or uri not in (client.redirect_uris or []):
        raise AtlasError("unknown application or redirect address")
    if params.get("response_type") != "code":
        raise AtlasError("only response_type=code is supported")
    challenge, method = params.get("code_challenge", ""), params.get("code_challenge_method", "")
    if challenge or client.require_pkce:
        if method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise AtlasError("PKCE with S256 is required")
    scopes = set(params.get("scope", "").split())
    if "openid" not in scopes or not scopes <= SCOPES:
        raise AtlasError("scope must include openid and only openid, profile, email")
    return client, " ".join(sorted(scopes))


def issue_code(session: Session, client: OidcClient, user: User, params: dict[str, str], scope: str) -> str:
    code = secrets.token_urlsafe(32)
    session.add(OidcCode(id=sha(code), client_id=client.client_id, user_id=user.id, redirect_uri=params["redirect_uri"],
                         challenge=params.get("code_challenge", ""), nonce=params.get("nonce", "")[:200], scope=scope,
                         expires_at=now() + CODE_TTL))
    session.flush()
    return code


def claims_for(user: User, scope: str) -> dict[str, Any]:
    out: dict[str, Any] = {"sub": user.id}
    if "profile" in scope.split():
        out.update(preferred_username=user.username, atlas_reputation=user.reputation)
    if "email" in scope.split() and user.email:
        out.update(email=user.email, email_verified=user.email_verified_at is not None)
    return out


def exchange_code(session: Session, ctx: Ctx, client: OidcClient, code: str, redirect_uri: str,
                  verifier: str) -> dict[str, Any]:
    row = session.get(OidcCode, sha(code or ""))
    if row is None or row.client_id != client.client_id:
        raise _refusal("invalid_grant", "invalid")
    if row.used_at is not None:  # reuse: revoke what this code produced
        session.execute(delete(OidcAccess).where(OidcAccess.client_id == client.client_id,
                                                 OidcAccess.user_id == row.user_id))
        session.commit()
        raise _refusal("invalid_grant", "reused")
    row.used_at = now()
    session.commit()  # a code is burned by any attempt, successful or not
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=now().tzinfo)
    if exp <= now():
        raise _refusal("invalid_grant", "expired")
    if row.redirect_uri != redirect_uri:
        raise _refusal("invalid_grant", "redirect_mismatch")
    if row.challenge:
        if not hmac.compare_digest(pkce_challenge(verifier or ""), row.challenge):
            raise _refusal("invalid_grant", "pkce_failed")
    elif client.require_pkce:
        raise _refusal("invalid_grant", "pkce_failed")
    user = session.get(User, row.user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        raise _refusal("invalid_grant", "account_unavailable")
    access = secrets.token_urlsafe(32)
    session.add(OidcAccess(id=sha(access), client_id=client.client_id, user_id=user.id, scope=row.scope,
                           expires_at=now() + ACCESS_TTL))
    t = now()
    claims = {"iss": ctx.settings.public_url, "aud": client.client_id, "iat": int(t.timestamp()),
              "exp": int((t + ID_TTL).timestamp()), **claims_for(user, row.scope)}
    if row.nonce:
        claims["nonce"] = row.nonce
    id_token = sign_jwt_rs256(session, ctx, claims) if client.id_token_alg == "RS256" else sign_jwt(ctx.identity, claims)
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
            "scope": row.scope, "id_token": id_token}


def userinfo(session: Session, access_token: str) -> dict[str, Any] | None:
    from . import instruments

    claims, outcome = _userinfo(session, access_token)
    instruments.token_operations.inc(token="access_token", operation="validate", outcome=outcome)
    return claims


def _userinfo(session: Session, access_token: str) -> tuple[dict[str, Any] | None, str]:
    row = session.get(OidcAccess, sha(access_token or ""))
    if row is None:
        return None, "invalid"
    client = session.get(OidcClient, row.client_id)
    if client is None or not client.enabled:  # disabling an application ends its access immediately
        return None, "client_disabled"
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=now().tzinfo)
    user = session.get(User, row.user_id)
    if exp <= now():
        return None, "expired"
    if user is None or user.status != "active" or user.deleted_at is not None:
        return None, "account_unavailable"
    return claims_for(user, row.scope), "ok"


def purge(session: Session) -> None:
    cutoff = now() - timedelta(hours=1)
    session.execute(delete(OidcCode).where(OidcCode.expires_at < cutoff))
    session.execute(delete(OidcAccess).where(OidcAccess.expires_at < cutoff))
    session.execute(delete(OidcState).where(OidcState.expires_at < cutoff))


# ---- relying party ------------------------------------------------------------------------------------

def probe_issuer(http: Any, issuer_url: str) -> dict[str, Any]:
    """Fetch discovery + keys of a prospective provider and describe the identity to be pinned."""
    base = issuer_url.rstrip("/")
    try:
        disc = http.get(f"{base}/.well-known/openid-configuration").json()
        if not isinstance(disc, dict):
            raise AtlasError("that address did not return an OpenID configuration")
        if disc.get("issuer") != base:
            raise AtlasError("the provider's issuer does not match the address you entered")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not str(disc.get(k, "")).startswith(base + "/"):
                raise AtlasError(f"{k} must live under the issuer address")
        doc = http.get(disc["jwks_uri"]).json()
        keys = doc.get("keys", []) if isinstance(doc, dict) else []
        key = next(k for k in keys if isinstance(k, dict) and k.get("kty") == "OKP" and k.get("crv") == "Ed25519")
        raw = b64u_dec(key["x"])
        pub = base64.b64encode(raw).decode()
        if key_id_for(raw) != key.get("kid"):
            raise AtlasError("the key id does not match the key")
    except (httpx.HTTPError, ValueError, KeyError, StopIteration, TypeError, AttributeError) as exc:
        raise AtlasError(f"could not read that provider: {type(exc).__name__}") from exc
    return {"issuer_url": base, "node_id": key["kid"], "public_key": pub,
            "authorization_endpoint": disc["authorization_endpoint"], "token_endpoint": disc["token_endpoint"]}


def start_login(session: Session, ctx: Ctx, issuer: OidcIssuer, next_url: str, link_user: User | None
                ) -> tuple[str, str]:
    """Returns (authorization URL, browser binding). The binding goes in a cookie and is required at the callback, so
    a callback URL carried to a different browser (login CSRF) is useless."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    binding = secrets.token_urlsafe(24)
    session.add(OidcState(id=sha(state), issuer_id=issuer.id, nonce=nonce, verifier=verifier, next_url=next_url,
                          link_user_id=link_user.id if link_user else None, binding=sha(binding),
                          expires_at=now() + STATE_TTL))
    session.flush()
    redirect = f"{ctx.settings.public_url}/oidc/callback/{issuer.id}"
    return issuer.authorization_endpoint + "?" + urlencode({
        "response_type": "code", "client_id": issuer.client_id, "redirect_uri": redirect,
        "scope": "openid profile email", "state": state, "nonce": nonce, "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256"}), binding


def finish_login(session: Session, ctx: Ctx, http: Any, issuer: OidcIssuer, code: str, state: str,
                 binding: str = "", current_user_id: str | None = None) -> tuple[User, str, bool]:
    """Complete the callback. Returns (user, next_url, linked_now). Raises AtlasError with a user-safe message."""
    row = session.get(OidcState, sha(state or ""))
    if row is None or row.issuer_id != issuer.id:
        raise _refusal("This sign-in attempt is unknown or was already used. Start again.", "state_invalid")
    session.delete(row)  # single use, even if the rest fails
    session.commit()
    if not row.binding or not hmac.compare_digest(row.binding, sha(binding or "")):
        raise _refusal("This sign-in was started in a different browser. Start again here.", "browser_mismatch")
    if row.link_user_id and current_user_id != row.link_user_id:
        raise _refusal("This link request belongs to another signed-in user. Start again.", "browser_mismatch")
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=now().tzinfo)
    if exp <= now():
        raise _refusal("This sign-in attempt expired. Start again.", "state_expired")
    try:
        secret = secrets_box.decrypt(ctx.settings, issuer.client_secret_enc)
        resp = http.post(issuer.token_endpoint, data={
            "grant_type": "authorization_code", "code": code, "client_id": issuer.client_id, "client_secret": secret,
            "redirect_uri": f"{ctx.settings.public_url}/oidc/callback/{issuer.id}", "code_verifier": row.verifier})
        if resp.status_code != 200:
            raise _refusal("The provider refused the sign-in.", "provider_refused")
        id_token = resp.json()["id_token"]
        claims = verify_jwt(id_token, public_key_b64=issuer.public_key, kid=issuer.node_id, issuer=issuer.issuer_url,
                            audience=issuer.client_id, nonce=row.nonce)
    except (secrets_box.SecretsError, httpx.HTTPError, KeyError, ValueError, TokenError) as exc:
        raise _refusal(f"Could not verify the provider's answer ({type(exc).__name__}).",
                       "verification_failed") from exc
    subject = claims["sub"][:200]
    link = session.get(OidcLink, (issuer.id, subject))
    if row.link_user_id:  # an explicit "link this to my account" flow
        user = session.get(User, row.link_user_id)
        if user is None or user.status != "active" or user.deleted_at is not None:
            raise _refusal("Your session ended. Sign in and try again.", "account_unavailable")
        if link is not None and link.user_id != user.id:
            raise _refusal("That external account is already linked to someone else.", "link_conflict")
        _require_allowed(issuer, user)
        if link is None:
            session.add(OidcLink(issuer_id=issuer.id, subject=subject, user_id=user.id))
        return user, row.next_url, True
    if link is not None:
        user = session.get(User, link.user_id)
        if user is None or user.status != "active" or user.deleted_at is not None:
            raise _refusal("This account is not available.", "account_unavailable")
        _require_allowed(issuer, user)
        return user, row.next_url, False
    if not issuer.auto_create:
        raise _refusal("No account here is linked to that identity. Sign in with your password, then link it "
                       "from your profile settings.", "no_account")
    if ctx.settings.registration != "open":
        raise _refusal("This server is not accepting new accounts.", "registration_closed")
    user = _create_account(session, issuer, claims)
    session.add(OidcLink(issuer_id=issuer.id, subject=subject, user_id=user.id))
    return user, row.next_url, True


def _require_allowed(issuer: OidcIssuer, user: User) -> None:
    if not (issuer.allow_all_users or user.oidc_allowed):
        raise _refusal("External sign-in is not enabled for this account. Ask an administrator.", "not_allowed")


def _create_account(session: Session, issuer: OidcIssuer, claims: dict[str, Any]) -> User:
    base = _USERNAME_CLEAN.sub("", str(claims.get("preferred_username") or "user").lower())[:24].strip("-_") or "user"
    if not accounts.USERNAME.match(base):
        base = "user" + base
    name = base
    while session.scalars(select(User.id).where(User.username == name)).first():
        name = f"{base[:24]}-{secrets.token_hex(2)}"
    email = None
    verified = False
    claimed = str(claims.get("email") or "").lower() or None
    if issuer.trust_email and claimed and claims.get("email_verified") is True and accounts.EMAIL.match(claimed) \
            and not session.scalars(select(User.id).where(User.email == claimed)).first():
        email, verified = claimed, True
    user = User(username=name, email=email, password_hash="!", role="user", oidc_allowed=True,
                email_verified_at=now() if verified else None)
    session.add(user)
    session.flush()
    accounts.audit(session, user.id, "register_oidc", issuer.id)
    return user
