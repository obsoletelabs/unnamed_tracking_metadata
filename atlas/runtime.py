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

from . import integrations
from .models import AuditEntry, Setting, now
from .settings import Settings

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}\Z")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_ACCOUNT_UID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")


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


GROUP_POLICY = "Node group"  # the specs only /admin/group edits (local administrators only)


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
    S("self_service_sync_tokens", "Members may create their own sync tokens", "Federation", "bool",
      "Lets signed-in members with a verified account create personal read-only tokens that can also follow this "
      "node's change feed (scope sync), for their own Atlas node or the Tracking App, without asking an "
      "administrator. Turning it off stops the sync access of those tokens at once."),
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
    # Owner-group local policy (C10 §8.1). Edited only on /admin/group by a local administrator, never from the
    # general settings form, so a delegated administrator can change none of them (§6.3 local_admin_only).
    S("group_profile_sync", "Sync user preferences in the owner group", GROUP_POLICY, "bool",
      "Linked accounts may share the preferences each user chose with the other nodes of your group. Off: nothing "
      "is sent or applied; local preferences keep working."),
    S("group_profile_fields", "Preferences that may be synced", GROUP_POLICY, "str",
      "Space-separated preference names, for example 'theme language a11y.font_scale'. Empty = every preference "
      "the user agrees to.", max_len=600),
    S("group_max_delegated_role", "Highest role a group grant may give here", GROUP_POLICY, "choice",
      "Grants signed by the group can add rights here up to this role. 'none' ignores every grant.",
      choices=("none", "moderator", "curator", "admin")),
    S("group_federated_privilege", "Rights in a session opened through another node", GROUP_POLICY, "choice",
      "'deny': ordinary user rights only. 'local_mfa': reviewer and administrator rights only after this node's "
      "own two-factor step. 'allow': as in a local session.", choices=("deny", "local_mfa", "allow")),
    S("group_trust_remote_mfa", "Accept the other node's two-factor step", GROUP_POLICY, "bool",
      "Off: people with a second factor here always enter this node's code after signing in through another node."),
    S("group_delegated_requires_mfa", "Delegated rights need this node's two-factor step", GROUP_POLICY, "bool",
      "Rights that come from a group grant apply only after this node's own second factor in that session."),
    S("group_auto_provision", "Create an account here on first group sign-in", GROUP_POLICY, "bool",
      "Only while registration is open. The new account has user rights and no password on this node."),
    S("group_signin", "Sign-in through another node of the group", GROUP_POLICY, "bool"),
    S("group_signin_public", "Show group sign-in buttons on the login page", GROUP_POLICY, "bool",
      "Shows every visitor which nodes are in your group and their addresses (often a home address). Off: people "
      "start from the node they are signed in on, with 'Open ... signed in'."),
    S("group_denied_accounts", "Group accounts whose grants are ignored here", GROUP_POLICY, "str",
      "Space-separated group account ids. Their grants add nothing on this node.", max_len=2000),
    S("group_view_max_age_hours", "Freshness limit for privileged group actions (hours)", GROUP_POLICY, "int",
      "Sign-ins and delegated rights need a view of the group from a third node newer than this.", lo=1, hi=168),
    S("group_hold_timeout_days", "Days a preference waits for its missing history", GROUP_POLICY, "int",
      lo=1, hi=90),
    # CONTRACT-C14 §3.8/§5.7 (H64, D1): data received from the group on this node's public pages. Off by default;
    # also edited on Admin → Sharing (local administrators only).
    S("group_data_public_pages", "Show data from your nodes to visitors", GROUP_POLICY, "bool",
      "Off: visitors who are not signed in (and accounts without the scope detail right) never see anything this "
      "node received from your other nodes. On: they see the public field categories below, without any badge, "
      "node name or count."),
    S("group_public_categories", "Field categories visitors may see", GROUP_POLICY, "str",
      "Space-separated category names (see Admin → Sharing). The rest is shown to signed-in accounts with the "
      "scope detail right only.", max_len=400),
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
EXTRA = {"logo_hash", "emit_protocol"}  # stored like settings but edited by dedicated controls, not the form
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
    merged = integrations.apply(base.model_copy(update=update) if update else base, overrides)
    if merged is base:
        return base
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
    if "group_denied_accounts" in parsed:
        ids = parsed["group_denied_accounts"].split()
        if any(not _ACCOUNT_UID.match(i) for i in ids):
            raise SettingError("Group accounts whose grants are ignored here: give group account ids (26 characters)")
        parsed["group_denied_accounts"] = " ".join(dict.fromkeys(ids))
    if "group_public_categories" in parsed:
        from .scope import clean_categories

        try:
            parsed["group_public_categories"] = clean_categories(parsed["group_public_categories"])
        except ValueError as exc:
            raise SettingError(f"Field categories visitors may see: {exc}") from exc
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
    if "group_public_categories" in changed:  # the anon_on projections hold the old categories (C14 §5.7)
        from .scope import schedule_reproject

        schedule_reproject(session)
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
