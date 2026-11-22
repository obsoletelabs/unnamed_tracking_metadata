"""Typed configuration. Read once; nothing else in the package touches os.environ."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # Resolution
    auto_match_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    review_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # Merge: higher wins. Unlisted sources get default_trust.
    source_trust: dict[str, int] = Field(
        default_factory=lambda: {"curator": 100, "manual": 90, "community": 85, "igdb": 70, "steam": 65,
                                 "gog": 60, "rawg": 50}
    )
    default_trust: int = 40
    # Sources whose observations may be published on the change feed.
    redistributable_sources: set[str] = Field(default_factory=lambda: {"curator", "manual", "community"})

    # API safety
    max_batch: int = Field(default=1000, ge=1, le=5000)
    max_resolve: int = Field(default=200, ge=1, le=5000)
    rate_limit_per_minute: int = Field(default=600, ge=1)
    max_body_bytes: int = Field(default=2_000_000, ge=1024)
    json_logs: bool = False
    metrics_enabled: bool = False

    # Providers (all optional)
    steam_enabled: bool = True
    rawg_api_key: str | None = None
    igdb_client_id: str | None = None
    igdb_client_secret: str | None = None
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
    accent_color_light: str = "#1f6fe0"
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
        return self.source_trust.get(source, self.default_trust)


@lru_cache
def get_settings() -> Settings:
    return Settings()
