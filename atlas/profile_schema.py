"""The profile field registry (CONTRACT-C10 §4.2): a closed allow-list of preferences that may be synced.

Every field is its own register (§4.4). A value that fails validation is `quarantine:invalid`; an unknown field is
`held` (§3.5). Nothing outside `REGISTRY` is ever emitted or applied, and `DENIED` (auth material, local ids,
email, roles) is asserted disjoint from it.
"""

from __future__ import annotations

import re
import unicodedata
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1


class FieldError(ValueError):
    pass


def _no_controls(text: str, allow_newlines: bool = False) -> bool:
    return not any(unicodedata.category(c) == "Cc" and not (allow_newlines and c in "\n\r") for c in text)


def _string(lo: int, hi: int, allow_newlines: bool = False) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str):
            raise FieldError("must be text")
        text = unicodedata.normalize("NFC", value.strip())
        if not lo <= len(text) <= hi or not _no_controls(text, allow_newlines):
            raise FieldError(f"must be {lo}-{hi} characters without control characters")
        return text
    return check


def _choice(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in options:
            raise FieldError(f"must be one of {', '.join(options)}")
        return value
    return check


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        raise FieldError("must be true or false")
    return value


_COLOR = re.compile(r"^#[0-9a-f]{6}\Z")
_BCP47 = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*\Z")
_TZ = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})\Z")


def _color(value: Any) -> str:
    if not isinstance(value, str) or not _COLOR.match(value.lower()):
        raise FieldError("must be a colour like #3366ff")
    return value.lower()


def _bcp47(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 35 or not _BCP47.match(value):
        raise FieldError("must be a BCP 47 language tag such as en or pt-BR")
    return value


@lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    try:
        return frozenset(zoneinfo.available_timezones())
    except Exception:  # noqa: BLE001 - no tz database on this platform
        return frozenset()


def _time_zone(value: Any) -> str:
    """An IANA name `zoneinfo.ZoneInfo` accepts. Where the platform has no tz database (Windows without the
    tzdata package), well-formed Area/Location names are accepted unverified (CONTRACT-AMENDMENTS A11)."""
    if not isinstance(value, str) or len(value) > 64:
        raise FieldError("must be an IANA time zone such as Europe/Berlin")
    zones = _zones()
    if zones:
        if value not in zones:
            raise FieldError("must be an IANA time zone such as Europe/Berlin")
        return value
    if not _TZ.match(value):
        raise FieldError("must be an IANA time zone such as Europe/Berlin")
    return value


def _font_scale(value: Any) -> float:
    if type(value) not in (int, float):
        raise FieldError("must be a number")
    steps = round((float(value) - 0.8) / 0.05)
    if not 0.8 - 1e-9 <= float(value) <= 2.0 + 1e-9 or abs(0.8 + steps * 0.05 - float(value)) > 1e-6:
        raise FieldError("must be between 0.8 and 2.0 in steps of 0.05")
    return round(0.8 + steps * 0.05, 2)


def _identity_refs(value: Any) -> list[dict[str, str]]:
    """Informational only: issuer url, issuer name, when linked. Never a subject; never creates an OidcLink."""
    if not isinstance(value, list) or len(value) > 8:
        raise FieldError("must be a list of at most 8 references")
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"issuer_url", "issuer_name", "linked_at"}:
            raise FieldError("each reference has exactly issuer_url, issuer_name and linked_at")
        url = item["issuer_url"]
        parts = urlsplit(url) if isinstance(url, str) else None
        if parts is None or parts.scheme not in ("https", "http") or not parts.hostname or len(url) > 300:
            raise FieldError("issuer_url must be an http(s) address")
        out.append({"issuer_url": url, "issuer_name": _string(1, 80)(item["issuer_name"]),
                    "linked_at": _string(1, 30)(item["linked_at"])})
    return out


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    check: Callable[[Any], Any]
    consent: bool  # default user consent (§4.2)
    kind: str  # choice | bool | text | number | list (for the settings form)
    choices: tuple[str, ...] = ()
    help: str = ""


def F(name: str, label: str, check, consent: bool = True, kind: str = "text", choices: tuple[str, ...] = (),
      help: str = "") -> Field:
    return Field(name, label, check, consent, kind, choices, help)


_DATE = ("locale", "iso", "dmy", "mdy", "ymd")
_TIME = ("locale", "24h", "12h")
_THEME = ("auto", "dark", "light")
_AVATAR = ("none", "initials", "identicon")
_DENSITY = ("comfortable", "compact")
_SORT = ("added", "title", "release")

REGISTRY: dict[str, Field] = {f.name: f for f in (
    F("display_name", "Display name", _string(1, 64), help="Shown instead of your username where a name appears."),
    F("avatar.style", "Avatar", _choice(*_AVATAR), kind="choice", choices=_AVATAR),
    F("avatar.color", "Avatar colour", _color, kind="color"),
    F("language", "Language", _bcp47, help="Stored now; applied once the interface is translated."),
    F("locale", "Locale", _bcp47, help="Number and date conventions; applied once the interface is translated."),
    F("date_format", "Date format", _choice(*_DATE), kind="choice", choices=_DATE),
    F("time_format", "Time format", _choice(*_TIME), kind="choice", choices=_TIME),
    F("theme", "Theme", _choice(*_THEME), kind="choice", choices=_THEME, help="Overrides the site theme for you."),
    F("time_zone", "Time zone", _time_zone, help="IANA name, for example Europe/Berlin."),
    F("a11y.reduced_motion", "Reduce motion", _bool, kind="bool"),
    F("a11y.high_contrast", "High contrast", _bool, kind="bool"),
    F("a11y.font_scale", "Text size", _font_scale, kind="number", help="0.8 to 2.0."),
    F("a11y.underline_links", "Underline links", _bool, kind="bool"),
    F("ui.density", "Density", _choice(*_DENSITY), kind="choice", choices=_DENSITY),
    F("ui.show_hidden_achievements", "Show hidden achievements", _bool, kind="bool"),
    F("ui.default_list_sort", "Default list order", _choice(*_SORT), kind="choice", choices=_SORT),
    F("bio", "About you", _string(0, 500, allow_newlines=True), consent=False, kind="textarea"),
    F("privacy.profile_public", "Public profile", _bool, consent=False, kind="bool",
      help="A change to public that arrives from another node waits for your review."),
    F("identity.refs", "Sign-in references", _identity_refs, consent=False, kind="list",
      help="Informational only; never links a sign-in."),
)}

# Never synced, whatever a peer sends (§4.2 deny-list). Names are app_user columns, auth tables and local ids.
DENIED = frozenset({
    "password_hash", "user_mfa.secret_enc", "user_mfa.recovery_hashes", "user_mfa.last_step", "secret_enc",
    "recovery_hashes", "last_step", "user_session", "api_token", "token_hash", "oidc_access", "oidc_code",
    "oidc_state", "oidc_link.subject", "subject", "email", "email_verified_at", "email_notifications", "username",
    "role", "status", "reputation", "oidc_allowed", "deleted_at", "id", "app_user.id", "user_id", "user_list.id",
    "follows", "notifications", "comments", "proposals", "reports", "lists", "csrf", "session", "mfa_verified_at",
})
# Column names of auth tables that must never appear as a key anywhere in a synced body (test A11-t).
AUTH_COLUMNS = frozenset({"password_hash", "secret_enc", "recovery_hashes", "last_step", "token_hash", "csrf",
                          "mfa_verified_at", "email", "subject", "setup_session_id"})

assert not DENIED & set(REGISTRY), "the deny-list overlaps the registry"


def known(name: str) -> bool:
    return name in REGISTRY


def validate(name: str, value: Any) -> Any:
    """The normalized value, or FieldError. Unknown fields raise KeyError (they are held, not invalid)."""
    return REGISTRY[name].check(value)


def default_consent() -> list[str]:
    return [n for n, f in REGISTRY.items() if f.consent]


def fields() -> list[str]:
    return list(REGISTRY)
