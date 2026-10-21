"""Admin-editable settings: a typed registry, validation, storage and live merging over the environment.

Environment variables stay the source of defaults and of anything secret or structural (database URL, keys,
role). Everything an operator reasonably wants to tune lives here and is changed from the admin UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEntry, Setting, now
from .settings import Settings

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}\Z")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    group: str
    kind: str  # int | bool | str | text | choice | color | links
    help: str = ""
    choices: tuple[str, ...] = ()
    lo: int | None = None
    hi: int | None = None
    max_len: int = 200
    nullable: bool = False  # empty input means "use the default"


def S(key, label, group, kind, help="", **kw) -> Spec:
    return Spec(key, label, group, kind, help, **kw)


REGISTRY: tuple[Spec, ...] = (
    S("app_name", "Site name", "Branding", "str", "Shown in the header, page titles and emails.", max_len=60),
    S("node_kind", "Kind of node", "Node", "choice", "Shown as a badge. 'official' is only a claim; people trust an official node "  # noqa: E501
      "by pinning its key from your published list. 'auto' follows the role.",
      choices=("auto", "official", "community", "mirror", "local")),
    S("node_operator", "Operator", "Node", "str", "Who runs this node (shown to visitors and to nodes that follow you).", max_len=80),  # noqa: E501
    S("tagline", "Tagline", "Branding", "str", "One line under the title on the home page.", max_len=200),
    S("footer_text", "Footer text", "Branding", "str", "Plain text shown in the footer.", max_len=300),
    S("accent_color", "Accent colour (dark theme)", "Branding", "color"),
    S("accent_color_light", "Accent colour (light theme)", "Branding", "color"),
    S("default_theme", "Theme", "Branding", "choice", "'auto' follows each visitor's system setting.",
      choices=("auto", "dark", "light")),
    S("nav_links", "Navigation links", "Branding", "links",
      "One per line: Label | address. Addresses may be /paths or https:// links."),
    S("registration", "Registration", "Community", "choice", "Whether new people can create accounts.",
      choices=("open", "closed")),
    S("email_verification", "Email verification", "Community", "choice",
      "Whether a verified address is needed to contribute. Empty = by node role (authorities require it).",
      choices=("off", "optional", "required"), nullable=True),
    S("auto_approve_reputation", "Auto-apply from reputation", "Community", "int",
      "Users at or above this reputation have their changes applied immediately. 0 disables.", lo=0, hi=1_000_000),
    S("curator_reputation", "Reviewer from reputation", "Community", "int",
      "Reputation that grants review rights. 0 = only people you promote.", lo=0, hi=1_000_000),
    S("max_pending_proposals", "Pending proposals per user", "Community", "int", lo=1, hi=1000),
    S("new_user_daily_proposals", "Daily proposals, new users", "Community", "int", lo=1, hi=10_000),
    S("trusted_daily_proposals", "Daily proposals, trusted users", "Community", "int", lo=1, hi=100_000),
    S("community_art_cap", "Community artwork per game", "Community", "int", lo=1, hi=200),
    S("oidc_provider_enabled", "Act as a sign-in provider (OIDC)", "Federation", "bool",
      "Lets other nodes you register under Admin → Sign-in sharing let your users sign in with their account here."),
    S("comments_enabled", "Comments", "Community", "bool", "Let verified users discuss games and proposals."),
    S("comment_daily_limit", "Comments per user per day", "Community", "int", lo=1, hi=10_000),
    S("comment_min_reputation", "Reputation needed to comment", "Community", "int", lo=0, hi=1_000_000),
    S("comment_autohide_reports", "Reports that auto-hide a comment", "Community", "int", lo=1, hi=100),
    S("lists_enabled", "Personal lists and follows", "Community", "bool"),
    S("max_lists_per_user", "Lists per user", "Community", "int", lo=1, hi=500),
    S("max_list_items", "Games per list", "Community", "int", lo=1, hi=100_000),
    S("max_follows", "Follows per user", "Community", "int", lo=1, hi=100_000),
    S("password_min_length", "Minimum password length", "Security", "int", lo=8, hi=64),
    S("session_ttl_days", "Sign-in lifetime (days)", "Security", "int", lo=1, hi=90),
    S("require_mfa_for_privileged", "Require two-factor sign-in for reviewers and administrators", "Security", "bool",
      "Includes reputation-based reviewers. Existing sessions must enrol or complete a second factor before continuing."),
    S("rate_limit_per_minute", "API requests per minute per token", "Security", "int", lo=1, hi=100_000),
    S("max_batch", "Largest observation batch", "Security", "int", lo=1, hi=5000),
    S("max_resolve", "Largest resolve batch", "Security", "int", lo=1, hi=5000),
    S("auto_match_threshold", "Auto-match confidence (percent)", "Matching", "int",
      "Matches at or above this are merged automatically.", lo=50, hi=100),
    S("review_threshold", "Review confidence (percent)", "Matching", "int",
      "Weaker matches below the auto threshold but above this open a duplicate review.", lo=0, hi=100),
    S("inherit_schema", "Adopt the upstream media types and fields", "Federation", "bool",
      "Mirrors and local nodes copy the authority's custom fields; yours always take precedence."),
    S("media_cache_enabled", "Download and cache artwork", "Artwork", "bool"),
    S("media_max_bytes", "Largest image (bytes)", "Artwork", "int", lo=1024, hi=50 * 1024 * 1024),
    S("mail_backend", "Email delivery", "Email", "choice",
      "'outbox' stores messages for admins to read and sends nothing (fine for testing).",
      choices=("smtp", "outbox")),
    S("smtp_host", "SMTP host", "Email", "str", max_len=200),
    S("smtp_port", "SMTP port", "Email", "int", lo=1, hi=65535),
    S("smtp_user", "SMTP user", "Email", "str", max_len=200),
    S("smtp_starttls", "Use STARTTLS", "Email", "bool"),
    S("mail_from", "From address", "Email", "str", max_len=200),
)
BY_KEY = {s.key: s for s in REGISTRY}
EXTRA = {"logo_hash"}  # stored like settings but edited by dedicated controls, not the form
PERCENT = {"auto_match_threshold", "review_threshold"}  # stored as 50..100, used as 0.5..1.0


def parse(spec: Spec, raw: Any) -> Any:
    """Convert form/JSON input to the stored value, or raise SettingError."""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if raw is None or (isinstance(raw, str) and not raw.strip() and spec.kind != "links"):
        if spec.nullable:
            return None
        if spec.kind in ("str", "text"):
            return ""
        raise SettingError(f"{spec.label}: a value is required")
    if spec.kind == "int":
        try:
            n = int(str(raw).strip())
        except ValueError as exc:
            raise SettingError(f"{spec.label}: must be a whole number") from exc
        if (spec.lo is not None and n < spec.lo) or (spec.hi is not None and n > spec.hi):
            raise SettingError(f"{spec.label}: must be between {spec.lo} and {spec.hi}")
        return n
    if spec.kind in ("str", "text"):
        text = str(raw).strip()
        if len(text) > spec.max_len or any(ord(c) < 32 for c in text):
            raise SettingError(f"{spec.label}: at most {spec.max_len} characters, no control characters")
        return text
    if spec.kind == "choice":
        value = str(raw).strip()
        if value not in spec.choices:
            raise SettingError(f"{spec.label}: choose one of {', '.join(spec.choices)}")
        return value
    if spec.kind == "color":
        value = str(raw).strip()
        if not _COLOR.match(value):
            raise SettingError(f"{spec.label}: use a colour like #3366ff")
        return value.lower()
    if spec.kind == "links":
        return parse_links(raw)
    raise SettingError(f"unknown setting kind {spec.kind}")  # pragma: no cover


def parse_links(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, list):
        pairs = [(str(i.get("label", "")), str(i.get("url", ""))) for i in raw if isinstance(i, dict)]
    else:
        pairs = []
        for line in str(raw or "").splitlines():
            if line.strip():
                label, _, url = line.partition("|")
                pairs.append((label, url))
    out = []
    for label, url in pairs[:12]:
        label, url = label.strip(), url.strip()
        ok = (url.startswith("/") and not url.startswith("//") and "\\" not in url) or url.startswith("https://")
        if not label or len(label) > 40 or not ok or len(url) > 300 or any(c.isspace() for c in url):
            raise SettingError("Navigation links: each line needs a short label and a /path or https:// address")
        out.append({"label": label, "url": url})
    return out


def display(spec: Spec, value: Any) -> str:
    if spec.kind == "links":
        return "\n".join(f"{i['label']} | {i['url']}" for i in (value or []))
    if spec.key in PERCENT:
        return str(round(float(value) * 100))
    return "" if value is None else str(value)


def current_value(spec: Spec, settings: Settings) -> Any:
    return getattr(settings, spec.key)


def to_attr(spec: Spec, stored: Any) -> Any:
    return stored / 100 if spec.key in PERCENT and stored is not None else stored


def from_attr(spec: Spec, value: Any) -> Any:
    return round(value * 100) if spec.key in PERCENT and value is not None else value


def effective(base: Settings, overrides: dict[str, Any]) -> Settings:
    """The environment settings with the admin's overrides applied (invalid leftovers are ignored)."""
    update = {k: to_attr(BY_KEY[k], v) if k in BY_KEY else v for k, v in overrides.items()
              if k in BY_KEY or k in EXTRA}
    if not update:
        return base
    merged = base.model_copy(update=update)
    try:
        return _check(merged)
    except ValueError:
        return base


def _check(s: Settings) -> Settings:
    if s.review_threshold > s.auto_match_threshold:
        raise ValueError("review threshold above auto-match threshold")
    return s


def load(session: Session) -> dict[str, Any]:
    return {r.key: r.value for r in session.scalars(select(Setting))}


def stamp(session: Session) -> str:
    """Cheap change marker for cache invalidation."""
    rows = session.execute(select(Setting.key, Setting.updated_at)).all()
    return str(sorted((k, str(t)) for k, t in rows))


def save(session: Session, base: Settings, actor_id: str | None, form: dict[str, Any]) -> list[str]:
    """Validate every submitted value, store those that differ from the current effective value."""
    current = effective(base, load(session))
    parsed: dict[str, Any] = {}
    for key, raw in form.items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue
        parsed[key] = parse(spec, raw)
    trial = current.model_copy(update={k: to_attr(BY_KEY[k], v) for k, v in parsed.items()})
    _check(trial)
    changed = []
    for key, value in parsed.items():
        spec = BY_KEY[key]
        if from_attr(spec, current_value(spec, current)) == value:
            continue
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value, updated_by=actor_id, updated_at=now()))
        else:
            row.value, row.updated_by, row.updated_at = value, actor_id, now()
        session.add(AuditEntry(actor_id=actor_id, action="setting", target=key,
                               detail={"from": from_attr(spec, current_value(spec, current)), "to": value}))
        changed.append(key)
    session.flush()
    return changed


def reset(session: Session, actor_id: str | None, key: str) -> bool:
    row = session.get(Setting, key)
    if row is None:
        return False
    session.delete(row)
    session.add(AuditEntry(actor_id=actor_id, action="setting_reset", target=key, detail=None))
    session.flush()
    return True


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.match(slug))
