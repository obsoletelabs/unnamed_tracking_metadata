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

    # Node identity. Generated and persisted in the database on first start when unset.
    node_key_file: str | None = None

    # Federation
    upstream_url: str | None = None
    upstream_token: str | None = None
    # key_id=base64 raw Ed25519 public key; entries from unlisted keys are rejected
    trusted_keys: dict[str, str] = Field(default_factory=dict)
    sync_interval_seconds: int = Field(default=60, ge=5)
    sync_batch_size: int = Field(default=500, ge=1, le=5000)

    # Resolution
    auto_match_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    review_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # Merge: higher wins. Unlisted sources get default_trust.
    source_trust: dict[str, int] = Field(
        default_factory=lambda: {"curator": 100, "manual": 90, "igdb": 70, "steam": 65, "gog": 60, "rawg": 50}
    )
    default_trust: int = 40
    # Sources whose observations may be published on the change feed.
    redistributable_sources: set[str] = Field(default_factory=lambda: {"curator", "manual"})

    # API safety
    max_batch: int = Field(default=1000, ge=1, le=5000)
    max_resolve: int = Field(default=200, ge=1, le=5000)
    rate_limit_per_minute: int = Field(default=600, ge=1)
    max_body_bytes: int = Field(default=2_000_000, ge=1024)

    # Providers (all optional)
    steam_enabled: bool = True
    rawg_api_key: str | None = None
    igdb_client_id: str | None = None
    igdb_client_secret: str | None = None
    provider_ttl_seconds: int = Field(default=7 * 86400, ge=60)
    provider_negative_ttl_seconds: int = Field(default=86400, ge=60)

    bootstrap_admin_token: str | None = None
    # None = migrate automatically on SQLite only; set false on multi-replica Postgres and run `atlas migrate`.
    auto_migrate: bool | None = None

    @field_validator("public_url")
    @classmethod
    def strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def check(self) -> Settings:
        if self.review_threshold > self.auto_match_threshold:
            raise ValueError("review_threshold must not exceed auto_match_threshold")
        if self.role == "mirror" and not self.upstream_url:
            raise ValueError("a mirror needs ATLAS_UPSTREAM_URL")
        if self.upstream_url and not self.trusted_keys:
            raise ValueError("ATLAS_TRUSTED_KEYS must list the upstream's key when ATLAS_UPSTREAM_URL is set")
        if self.environment == "production":
            if self.database_url.startswith("sqlite") and self.role == "authority":
                raise ValueError("an authority in production needs PostgreSQL")
            if not self.public_url.startswith("https://"):
                raise ValueError("ATLAS_PUBLIC_URL must be https in production")
            if self.bootstrap_admin_token and len(self.bootstrap_admin_token) < 32:
                raise ValueError("ATLAS_BOOTSTRAP_ADMIN_TOKEN must be at least 32 characters")
        return self

    def trust(self, source: str) -> int:
        return self.source_trust.get(source, self.default_trust)


@lru_cache
def get_settings() -> Settings:
    return Settings()
