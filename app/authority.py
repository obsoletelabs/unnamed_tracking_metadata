from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


VALID_MODES = {"central", "local", "hybrid"}


@dataclass(frozen=True, slots=True)
class AuthorityConfig:
    mode: str
    instance_id: str
    central_url: str
    sync_interval_seconds: int
    publish_contributions: bool

    @property
    def is_central(self) -> bool:
        return self.mode == "central"

    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    @property
    def can_sync(self) -> bool:
        return self.mode == "hybrid" and bool(self.central_url)


def load_authority_config() -> AuthorityConfig:
    mode = os.getenv("AUTHORITY_MODE", "local").strip().lower()
    if mode not in VALID_MODES:
        raise RuntimeError(f"AUTHORITY_MODE must be one of: {', '.join(sorted(VALID_MODES))}")
    instance_id = os.getenv("AUTHORITY_INSTANCE_ID", "").strip() or secrets.token_hex(12)
    central_url = os.getenv("CENTRAL_AUTHORITY_URL", "").strip().rstrip("/")
    sync_interval = int(os.getenv("AUTHORITY_SYNC_INTERVAL_SECONDS", "900"))
    if sync_interval < 60 or sync_interval > 86400:
        raise RuntimeError("AUTHORITY_SYNC_INTERVAL_SECONDS must be between 60 and 86400")
    publish = os.getenv("PUBLISH_CONTRIBUTIONS", "false").strip().lower() == "true"
    if mode == "central" and central_url:
        raise RuntimeError("CENTRAL_AUTHORITY_URL must not be set in central authority mode")
    if mode == "hybrid" and not central_url:
        raise RuntimeError("CENTRAL_AUTHORITY_URL is required in hybrid authority mode")
    return AuthorityConfig(mode, instance_id, central_url, sync_interval, publish)


config = load_authority_config()
