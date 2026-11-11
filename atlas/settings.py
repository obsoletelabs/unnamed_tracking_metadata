"""Typed configuration. Read once; nothing else in the package touches os.environ."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["authority", "mirror", "local"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_file=".env", extra="ignore")

    role: Role = "local"
    database_url: str = "sqlite:///./atlas.db"
    public_url: str = "http://localhost:8000"
    environment: Literal["dev", "production"] = "dev"

    # Federation
    upstream_url: str | None = None
    upstream_token: str | None = None
    # key_id=base64 raw Ed25519 public key; entries from unlisted keys are rejected
    trusted_keys: dict[str, str] = Field(default_factory=dict)
    sync_interval_seconds: int = Field(default=60, ge=5)
    sync_batch_size: int = Field(default=500, ge=1, le=5000)
    # Entries a follower keeps downloading, verifying and relaying past the first parked one (C1.4, H10).
    sync_max_parked_backlog: int = Field(default=50_000, ge=1)
    client_changes_retain_days: int = Field(default=90, ge=1, le=3650)  # local client change log (C3.2)
    # Lowest follower protocol this node serves; followers below it refuse to sync (C1.3).
    min_follower_protocol: str = "1.0"
    # Highest protocol this node's own feed may use. Unset = min_follower_protocol. Raised in Admin → Peers.
    emit_protocol: str | None = None
    # CONTRACT-C13 (A19). Environment settings in this release, like A17/A18's (amendment A19).
    # H54: below protocol 1.5, introduce records with an ordinary 1.0 observation of the source `atlas-record`
    # (every follower applies it). Off by default: it publishes titles and store ids as an observation.
    federation_bridge_record_obs: bool = False
    # CONTRACT-C15 §4.1 (A20): the media types the bridge introduces. A 1.0 follower files every observation as a game
    # (its old fallback), so by default only games are bridged; a record of another type is introduced with a
    # `record` (which a 1.0 follower parks) when the emit protocol allows one, and otherwise not at all. Admin → Peers
    # counts the skipped records.
    federation_bridge_media_types: list[str] = Field(default_factory=lambda: ["game"])
    # H55: `referenced` introduces a record when a signed entry first names it; `all` introduces every live record,
    # provider imports included. The safe default is `referenced` (owner decision pending).
    federation_introduce: Literal["referenced", "all"] = "referenced"
    # H53: what a `record` (or bridge observation) carries besides the id and type: a subset of title, provider_ids.
    federation_snapshot_fields: list[Literal["title", "provider_ids"]] = Field(
        default_factory=lambda: ["title", "provider_ids"])
    # C13 §5: open `wait` + quarantined rows per feed before the follower stops applying it (backlog_full), and how
    # many waiting rows one introduction applies per transaction.
    sync_max_open_failures: int = Field(default=10_000, ge=1)
    sync_wake_batch: int = Field(default=500, ge=1, le=5000)
    # H56 (owner decision pending, safe default OFF; the contract recommends on): when a `record` arrives, fetch the
    # game from this node's OWN enabled providers (its own keys and quota) by the store ids the record carries, so
    # description, art and achievements are built here without anything being redistributed (C13 §5.1 step 4).
    # Only store ids no other record here holds, only ids the provider accepts, at most
    # `sync_enrich_per_peer_per_hour` fetches per upstream feed (the rest wait for the next hour).
    sync_enrich_from_own_providers: bool = False
    sync_enrich_per_peer_per_hour: int = Field(default=200, ge=1, le=10_000)
    # C13 §6.3: ask the feed's own signer for an image digest before the third-party origin is tried.
    sync_media_prefer_peer: bool = True
    # H58 (owner decision pending, safe default off): may `/api/v2/sync/media` hand a pinned peer the cached bytes of a
    # curated URL row whose licence is `unknown`? Off: only `cc0` and `owner_upload` bytes are served (C12 §12.5.5).
    federation_media_unknown_licence: bool = False

    # Resolution
    auto_match_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    review_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # Merge: higher wins. Unlisted sources get default_trust.
    source_trust: dict[str, int] = Field(
        default_factory=lambda: {"curator": 100, "manual": 90, "community": 85, "igdb": 70, "steam": 65,
                                 "gog": 60, "openlibrary": 55, "rawg": 50}
    )
    default_trust: int = 40
    # Sources whose observations may be published on the change feed.
    redistributable_sources: set[str] = Field(default_factory=lambda: {"curator", "manual", "community"})

    # Matching and provenance (CONTRACT-C11). Environment settings in this release; the admin screen that edits
    # them ships with the C11 UI (amendment A17 #9).
    # "Preferred source per field" (PART 6.4): {field: [source, ...]}. Reorders sources of the provider class only.
    field_source_preference: dict[str, list[str]] = Field(default_factory=dict)
    # Per-origin trust ceiling for replicated observations: {node_id: class}. Unlisted: the configured upstream
    # authority (a node in ATLAS_TRUSTED_KEYS) is `curator`, every other origin `provider` (C11 §11.7.1, H48).
    sync_origin_class_cap: dict[str, str] = Field(default_factory=dict)
    review_dismiss_days: int = Field(default=30, ge=0, le=3650)  # a dismissed pair stays closed this long
    match_decision_retain_days: int = Field(default=180, ge=1, le=3650)

    # API safety
    max_batch: int = Field(default=1000, ge=1, le=5000)
    max_resolve: int = Field(default=200, ge=1, le=5000)
    rate_limit_per_minute: int = Field(default=600, ge=1)
    max_body_bytes: int = Field(default=2_000_000, ge=1024)
    json_logs: bool = False
    metrics_enabled: bool = False
    # Several worker processes on one host: a directory they share, so any worker's /metrics reports all of them
    metrics_dir: str | None = None
    # How long the expensive collectors (catalogue aggregates, the media directory walk) are cached per process
    metrics_cache_seconds: int = Field(default=300, ge=0, le=86400)

    # Providers (all optional)
    steam_enabled: bool = True
    rawg_api_key: str | None = None
    igdb_client_id: str | None = None
    igdb_client_secret: str | None = None
    # TheGamesDB API v1 key (issue #54): keyed official API, requested from the TheGamesDB forum.
    tgdb_api_key: str | None = None
    # Open Library (books, CONTRACT-C15 §3): keyless. On by default (D15.7), so a fresh node finds and imports books
    # with no configuration; at most one request a second per process, never a bulk crawl.
    openlibrary_enabled: bool = True
    # C15 §3.2 / D15.7: an e-mail or https URL sent in the User-Agent to providers that ask clients to identify
    # themselves (Open Library). Empty by default: nothing about the operator leaves the node unless set here (or
    # in Admin -> Integrations). Never taken from node_operator.
    provider_contact: str = ""
    # Wikidata cross-store ids (issue #63): keyless, CC0. Off by default for the same reason.
    wikidata_enabled: bool = False
    # Price sources `atlas prices fetch` may query (issue #38), comma-separated, e.g. "steam". Empty: off.
    price_sources: str = Field(default="", max_length=200)
    provider_ttl_seconds: int = Field(default=7 * 86400, ge=60)
    provider_negative_ttl_seconds: int = Field(default=86400, ge=60)

    # Artwork cache
    media_dir: str = "./media"
    # Scheduled backups: set backup_dir to write a verified archive every backup_interval_hours, keeping backup_keep.
    backup_dir: str | None = None
    backup_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    backup_keep: int = Field(default=14, ge=1, le=1000)
    media_cache_enabled: bool = True
    media_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    media_max_pixels: int = Field(default=40_000_000, ge=1)
    media_cache_batch: int = Field(default=10, ge=1, le=200)
    media_allowed_hosts: set[str] = Field(default_factory=set)  # empty = any public host
    # Safe fetching (C12 §12.6, H40). Environment settings in this release (amendment A18 #9).
    media_https_only: bool = True  # new downloads need https; an https -> http redirect is always refused
    media_fetch_deadline_s: int = Field(default=20, ge=5, le=60)  # one download, redirects and body included
    media_host_concurrency: int = Field(default=2, ge=1, le=16)  # simultaneous downloads per host and process
    media_host_rpm: int = Field(default=60, ge=1, le=6000)  # downloads per host and minute, per process
    media_max_frames: int = Field(default=300, ge=1, le=10000)  # frames of an animated image
    # Per-source media policy overrides (§12.5.5): {"steam": {"fetch": "redirect"}}; see atlas/providers/media_policy.py
    media_policy: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Achievements (C12 §12.1)
    achievement_percent_max_age_days: int = Field(default=30, ge=1, le=3650)  # older rarity is shown as stale
    achievement_retired_retention_days: int = Field(default=180, ge=1, le=3650)  # H37
    steam_api_key: str | None = None

    app_name: str = "Atlas"
    # What kind of node this is, shown to visitors and peers. "auto" follows the role (authority = community).
    # "official" is a claim: it means something only to people who pin this node's key from your published list.
    node_kind: Literal["auto", "official", "community", "mirror", "local"] = "auto"
    node_operator: str = ""
    # Owner groups (CONTRACT-C10): the freshness gate for privileged actions (H28) and the member pull interval.
    group_view_max_age_hours: int = Field(default=24, ge=1, le=168)
    group_sync_interval: int = Field(default=60, ge=30, le=86400)  # C10 §3.8
    # Profile sync over the private group channel (C10 §4.3): the operator's switch and field allow-list (empty =
    # every registry field), and how long a version waits for its missing history before it is applied (§4.4).
    group_profile_sync: bool = True
    group_profile_fields: str = ""
    group_hold_timeout_days: int = Field(default=7, ge=1, le=90)
    # The revocation journal (C10 §C10.17 R1-3): every accepted CRL, succession and cancel, as signed objects, in a
    # directory outside the database, media_dir and backup_dir, so a restore cannot roll revocations back. Put it on
    # a persistent volume. Empty turns it off (a restore is then protected only by the resync pull).
    group_journal_dir: str | None = "./group-journal"
    # Delegation and federated sign-in (C10 §5.5, §6.3); the defaults are H15 and H27. Only a local admin
    # changes them (the /admin/group policy form); a delegated admin never can.
    group_max_delegated_role: Literal["none", "moderator", "curator", "admin"] = "none"
    group_federated_privilege: Literal["deny", "local_mfa", "allow"] = "deny"
    group_trust_remote_mfa: bool = False
    group_delegated_requires_mfa: bool = True
    group_auto_provision: bool = False
    group_signin: bool = True
    group_signin_public: bool = False
    group_denied_accounts: str = ""  # space-separated group account ids whose delegated rights are dropped here
    tagline: str = "Media metadata, merged and sourced"
    footer_text: str = "Community metadata. Contributions are CC0."
    accent_color: str = "#6aa8ff"  # dark theme accent
    accent_color_light: str = "#1a5fc2"  # 5.4:1 on the lightest surfaces (#1f6fe0 was 4.2:1 on --surface-2)
    default_theme: Literal["auto", "dark", "light"] = "auto"
    logo_hash: str | None = None
    inherit_schema: bool = True  # mirrors/locals adopt the upstream authority's media types and fields
    nav_links: list[dict[str, str]] = Field(default_factory=list)  # [{"label": ..., "url": ...}]
    # Email. The SMTP password is only ever read from ATLAS_SMTP_PASSWORD.
    mail_backend: Literal["smtp", "outbox"] = "outbox"  # outbox: stored for admins, nothing is sent
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = "atlas@localhost"
    # None = decided by role: authorities require a verified email, other nodes do not.
    email_verification: Literal["off", "optional", "required"] | None = None

    # Community
    registration: Literal["open", "closed"] = "open"
    session_ttl_days: int = Field(default=14, ge=1, le=90)
    require_mfa_for_privileged: bool = False
    auto_approve_reputation: int = Field(default=50, ge=0)  # 0 disables auto-approval
    curator_reputation: int = Field(default=500, ge=0)  # reputation that grants review rights; 0 = manual grants only
    max_pending_proposals: int = Field(default=20, ge=1)
    new_user_daily_proposals: int = Field(default=10, ge=1)
    password_min_length: int = Field(default=10, ge=8)
    trusted_daily_proposals: int = Field(default=100, ge=1)  # cap for auto-applied edits per user per day
    community_art_cap: int = Field(default=50, ge=1, le=200)  # community artwork per game
    oidc_provider_enabled: bool = False  # this node lets other nodes sign its users in
    # #84: members (not only administrators) may create personal tokens that can also read the change feed, so they
    # can follow this node from their own node or app. Read-only either way; turning it off stops those tokens'
    # sync access at once.
    self_service_sync_tokens: bool = False
    comments_enabled: bool = True
    lists_enabled: bool = True
    comment_daily_limit: int = Field(default=30, ge=1)
    comment_min_reputation: int = Field(default=0, ge=0)
    comment_autohide_reports: int = Field(default=3, ge=1)  # open reports from distinct users that hide a comment
    max_lists_per_user: int = Field(default=20, ge=1)
    max_list_items: int = Field(default=2000, ge=1)
    max_follows: int = Field(default=500, ge=1)
    initial_admin: str | None = None  # username that becomes administrator; unset = first account

    bootstrap_admin_token: str | None = None
    # Encrypts secrets stored in the database (peer tokens, OIDC client secrets). Generate: `atlas secret-key`.
    secret_key: str | None = None
    # Retired secrets (comma separated) that still decrypt during a rotation; `atlas secrets rewrap` moves
    # every stored secret to secret_key, after which this can be emptied.
    secret_key_previous: str | None = None
    # None = migrate automatically on SQLite only; set false on multi-replica Postgres and run `atlas migrate`.
    auto_migrate: bool | None = None

    @field_validator("public_url")
    @classmethod
    def strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("provider_contact")
    @classmethod
    def contact(cls, v: str) -> str:
        return check_contact(v)

    @field_validator("federation_bridge_media_types")
    @classmethod
    def bridge_media_types(cls, v: list[str]) -> list[str]:
        """C15 §4.1: an empty list would bridge nothing, not even games; refuse it rather than read it two ways."""
        cleaned = [t.strip() for t in v if isinstance(t, str) and t.strip()]
        if not cleaned:
            raise ValueError("federation_bridge_media_types must name at least one media type (default [\"game\"])")
        return cleaned

    @field_validator("min_follower_protocol", "emit_protocol")
    @classmethod
    def protocol_version(cls, v: str | None) -> str | None:
        from .feed_schema import PROTOCOL, parse_protocol

        if v is None or v == "":
            return None
        if parse_protocol(v) > parse_protocol(PROTOCOL):
            raise ValueError(f"this release speaks federation protocol {PROTOCOL} at most")
        return v

    @model_validator(mode="after")
    def check(self) -> Settings:
        if self.min_follower_protocol is None:
            self.min_follower_protocol = "1.0"
        if self.review_threshold > self.auto_match_threshold:
            raise ValueError("review_threshold must not exceed auto_match_threshold")
        from .merge import CLASS_RANK, LOCK_FIELDS

        bad = sorted(c for c in self.sync_origin_class_cap.values() if c not in CLASS_RANK or c == "derived")
        if bad:
            raise ValueError(f"sync_origin_class_cap: unknown class {bad[0][:20]!r} (curator, publisher, community, "
                             "provider)")
        unknown = sorted(f for f in self.field_source_preference if f not in (*LOCK_FIELDS, "provider_ids"))
        if unknown:
            raise ValueError(f"field_source_preference: unknown field {unknown[0][:40]!r}")
        if any(len(v) > 32 for v in self.field_source_preference.values()):
            raise ValueError("field_source_preference: at most 32 sources per field")
        from .providers.media_policy import check_overrides

        check_overrides(self.media_policy)
        from .group_journal import check_location

        check_location(self)
        if self.upstream_url and not self.trusted_keys:
            raise ValueError("ATLAS_TRUSTED_KEYS must list the upstream's key when ATLAS_UPSTREAM_URL is set")
        if self.environment == "production":
            if self.database_url.startswith("sqlite") and self.role == "authority":
                raise ValueError("an authority in production needs PostgreSQL")
            if not self.public_url.startswith("https://"):
                raise ValueError("ATLAS_PUBLIC_URL must be https in production")
            if self.registration == "open" and not self.initial_admin and not self.bootstrap_admin_token:
                raise ValueError("production with open registration needs ATLAS_INITIAL_ADMIN so nobody else can claim it")
            if self.bootstrap_admin_token and len(self.bootstrap_admin_token) < 32:
                raise ValueError("ATLAS_BOOTSTRAP_ADMIN_TOKEN must be at least 32 characters")
        return self

    def kind(self) -> str:
        if self.node_kind != "auto":
            return self.node_kind
        return "community" if self.role == "authority" else self.role

    def verification_mode(self) -> str:
        return self.email_verification or ("required" if self.role == "authority" else "optional")

    def trust(self, source: str) -> int:
        """`atlas-record` (C13 §3.3) ranks below every provider unless configured: its observation only carries a
        display title until real observations arrive."""
        return self.source_trust.get(source, RECORD_TRUST if source == "atlas-record" else self.default_trust)


_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_CONTACT_EMAIL = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@" + _LABEL + r"(?:\." + _LABEL + r")+"
_CONTACT_URL = r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'*+,=:@%/-]*)?"


def check_contact(value: str) -> str:
    """`provider_contact`: empty, an e-mail address or an https URL; visible ASCII only, without `"();<>` or spaces,
    at most 200 characters. It goes into an HTTP header, so anything else is refused, never cleaned."""
    import re

    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > 200 or not (re.fullmatch(_CONTACT_EMAIL, value, re.ASCII)
                                or re.fullmatch(_CONTACT_URL, value, re.ASCII)):
        raise ValueError("provider_contact must be an e-mail address or an https:// URL (visible ASCII, at most 200 "
                         "characters, no spaces, quotes, brackets or semicolons)")
    return value


#: The sources this node's own provider adapters write (`providers.registry.build_registry`, plus the Wikidata
#: cross-reference source). They are always reserved for curator and system ingests, whatever ATLAS_SOURCE_TRUST
#: lists: a contributor token must not write observations indistinguishable from the node's own provider imports.
PROVIDER_SOURCES = frozenset({"steam", "rawg", "igdb", "tgdb", "openlibrary", "wikidata"})

#: The trust of the `atlas-record` source (C13 §3.3), below every listed provider (`rawg` 50 is the lowest).
RECORD_TRUST = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()
