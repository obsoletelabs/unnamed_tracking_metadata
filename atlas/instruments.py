"""Every process-recorded metric family, with its label allowlist (issues #69-#76).

Label values come only from the tuples below or, for `provider`, from the configured registry with a cap. Nothing
here is labelled with an account, client, subject, email, IP, token, URL, query, title, path or exception text.
docs/guide/observability.md documents each family; tests/test_metrics_catalogue.py checks that the two agree.
"""

from __future__ import annotations

from .auth import SCOPES
from .metrics import (
    COUNT_BUCKETS,
    DB_BUCKETS,
    DELAY_BUCKETS,
    LATENCY_BUCKETS,
    REGISTRY,
    SLOW_BUCKETS,
)
from .vocab import MEDIA_ERRORS

#: Configured provider names kept as label values (a node configures a handful; later ones fold into `other`).
PROVIDER_CAP = 16

# ---- scrape (#69) -------------------------------------------------------------------------------------------
COLLECTOR_NAMES = ("readiness", "feed", "outbox", "peers", "sync_upstreams", "providers", "backup", "group",
                   "database", "catalog", "media_storage")
collector_refresh = REGISTRY.histogram(
    "atlas_collector_refresh_duration_seconds",
    "Time spent computing a scrape collector, by outcome (cached collectors only when they refresh)",
    {"collector": COLLECTOR_NAMES, "outcome": ("ok", "error")}, SLOW_BUCKETS)

# ---- providers (#71) ----------------------------------------------------------------------------------------
PROVIDER_OPERATIONS = ("search", "fetch", "achievements", "achievement_stats")
provider_calls = REGISTRY.counter(
    "atlas_provider_calls_total",
    "Logical provider lookups by terminal outcome (one per lookup, however it was answered)",
    {"provider": PROVIDER_CAP, "operation": PROVIDER_OPERATIONS,
     "outcome": ("ok", "empty", "cache_hit", "negative_cache_hit", "stale_served", "error", "unavailable")})
provider_cache = REGISTRY.counter(
    "atlas_provider_cache_requests_total",
    "Provider cache lookups: hit (fresh), negative_hit (fresh 'no such record'), miss, stale (expired row served "
    "because the provider failed or was held back)",
    {"provider": PROVIDER_CAP, "result": ("hit", "negative_hit", "miss", "stale")})
provider_backoff = REGISTRY.counter(
    "atlas_provider_backoff_total",
    "Calls held back by this process: circuit_open (5 failures in a row open it for 60 s), budget_exhausted "
    "(per-minute budget), circuit_tripped (the failure that opened it)",
    {"provider": PROVIDER_CAP, "reason": ("circuit_open", "budget_exhausted", "circuit_tripped")})
provider_requests = REGISTRY.counter(
    "atlas_provider_requests_total",
    "Upstream HTTP requests (attempts, including token requests) by outcome class",
    {"provider": PROVIDER_CAP,
     "outcome": ("ok", "http_429", "http_4xx", "http_5xx", "timeout", "transport", "invalid_payload")})
provider_request_duration = REGISTRY.histogram(
    "atlas_provider_request_duration_seconds", "Upstream HTTP request duration by outcome class",
    {"provider": PROVIDER_CAP,
     "outcome": ("ok", "http_429", "http_4xx", "http_5xx", "timeout", "transport", "invalid_payload")},
    LATENCY_BUCKETS)
provider_quota_remaining = REGISTRY.gauge(
    "atlas_provider_rate_limit_remaining",
    "Requests left in the provider's window as its last response reported (X-RateLimit-Remaining / "
    "RateLimit-Remaining); absent when the provider never reported one", {"provider": PROVIDER_CAP}, merge="min",
    live=True)
provider_quota_reset = REGISTRY.gauge(
    "atlas_provider_rate_limit_reset_timestamp_seconds",
    "When the provider said its window resets (Unix time, from X-RateLimit-Reset / RateLimit-Reset / Retry-After); "
    "absent when never reported", {"provider": PROVIDER_CAP}, merge="max", live=True)

# ---- federation (#70) ---------------------------------------------------------------------------------------
FEDERATION_OUTCOMES = ("ok", "empty", "timeout", "transport_error", "http_error", "untrusted", "refused",
                       "rewound", "fork", "verification_failed", "backlog_full", "error")
federation_batches = REGISTRY.counter(
    "atlas_federation_batches_total", "Feed pages pulled from peers, by outcome",
    {"follow": ("own", "relayed"), "outcome": FEDERATION_OUTCOMES})
federation_batch_duration = REGISTRY.histogram(
    "atlas_federation_batch_duration_seconds",
    "Time per pulled page and stage: fetch (info and changes requests), verify (signatures and sequence), "
    "apply (store, apply in order, retry quarantined)", {"stage": ("fetch", "verify", "apply")}, SLOW_BUCKETS)
FEDERATION_RESULTS = ("received", "retained", "duplicate", "applied", "noop", "parked", "quarantined", "waiting")
federation_entries = REGISTRY.counter(
    "atlas_federation_entries_total",
    "Signed feed entries: received in a page, retained (new), duplicate (already retained, identical), and the "
    "result of applying them in order", {"result": FEDERATION_RESULTS})
federation_bytes = REGISTRY.counter(
    "atlas_federation_received_bytes_total",
    "Response bytes of the sync info and changes requests this node made", {"request": ("info", "changes")})
VERIFICATION_REASONS = ("signature", "untrusted_key", "key_mismatch", "rotation", "sequence", "kind", "fork",
                        "rewind", "protocol")
federation_verification = REGISTRY.counter(
    "atlas_federation_verification_failures_total",
    "Pages refused before anything was applied, by class", {"reason": VERIFICATION_REASONS})

# ---- catalogue enrichment (#72) -----------------------------------------------------------------------------
enrichment = REGISTRY.counter(
    "atlas_enrichment_operations_total", "Catalogue enrichment runs by operation and outcome",
    {"operation": ("provider_import",), "outcome": ("created", "matched", "review", "not_found", "failed")})

# ---- media (#73) --------------------------------------------------------------------------------------------
MEDIA_KINDS = ("artwork", "achievement_icon")
MEDIA_OUTCOMES = ("ok", "deferred", "storage_error", "error", *MEDIA_ERRORS)
media_operations = REGISTRY.counter(
    "atlas_media_operations_total",
    "Image cache work: fetch (download and validate) and store (write to ATLAS_MEDIA_DIR); outcome is `ok`, "
    "`deferred` (this node's per-host limit said not now), `storage_error`, `error` (an unexpected exception) or "
    "a C12 media_error code",
    {"kind": MEDIA_KINDS, "operation": ("fetch", "store"), "outcome": MEDIA_OUTCOMES})
media_duration = REGISTRY.histogram(
    "atlas_media_operation_duration_seconds", "Image fetch and store duration",
    {"kind": MEDIA_KINDS, "operation": ("fetch", "store")}, SLOW_BUCKETS)
media_bytes = REGISTRY.counter(
    "atlas_media_transfer_bytes_total", "Image bytes downloaded (validated or not), by where they came from",
    {"source": ("origin", "peer")})
media_cache = REGISTRY.counter(
    "atlas_media_cache_requests_total",
    "Image requests: hit (served from this node's cache), redirect (sent to the provider host), miss (not cached "
    "yet), refused (policy: not served here)", {"route": ("image", "media"), "result": ("hit", "redirect", "miss",
                                                                                         "refused")})
media_evictions = REGISTRY.counter(
    "atlas_media_cache_evictions_total", "Cached images forgotten, by reason (Atlas has no automatic expiry)",
    {"reason": ("manual",)})

# ---- database and search (#74) ------------------------------------------------------------------------------
DB_OPERATIONS = ("select", "insert", "update", "delete", "other")
db_query = REGISTRY.histogram(
    "atlas_database_query_duration_seconds", "Statement execution time by statement class (never SQL text)",
    {"operation": DB_OPERATIONS, "outcome": ("ok", "error")}, DB_BUCKETS)
db_transactions = REGISTRY.counter(
    "atlas_database_transactions_total", "Transactions ended, by outcome", {"outcome": ("commit", "rollback")})
DB_ERRORS = ("lock_timeout", "deadlock", "serialization", "connection", "integrity", "other")
db_errors = REGISTRY.counter(
    "atlas_database_errors_total", "Statement errors by class (contention: lock_timeout, deadlock, serialization)",
    {"class": DB_ERRORS})
SEARCH_MODES = ("title", "filter", "browse")
search_duration = REGISTRY.histogram(
    "atlas_search_duration_seconds",
    "Catalogue search time: title (a title query), filter (release-date filters only), browse (neither)",
    {"mode": SEARCH_MODES, "outcome": ("ok", "invalid", "error")}, LATENCY_BUCKETS)
search_results = REGISTRY.histogram(
    "atlas_search_results", "Records returned per successful search page (never the query or the records)",
    {"mode": SEARCH_MODES}, COUNT_BUCKETS)

# ---- background work, mail and recovery (#75) ---------------------------------------------------------------
BACKGROUND_OPERATIONS = ("housekeeping", "price_purge", "decision_prune", "retired_purge", "media_cache",
                         "mail_delivery", "mail_purge", "backup", "sync_round", "scheduled_introduce", "group_round")
background_ops = REGISTRY.counter(
    "atlas_background_operations_total", "Background task runs by outcome (skipped: not due or not configured)",
    {"operation": BACKGROUND_OPERATIONS, "outcome": ("ok", "error", "skipped")})
background_duration = REGISTRY.histogram(
    "atlas_background_operation_duration_seconds", "Background task run time",
    {"operation": BACKGROUND_OPERATIONS}, SLOW_BUCKETS)
BACKGROUND_LOOPS = ("housekeeping", "media_cache", "mail", "backup", "sync")
background_delay = REGISTRY.histogram(
    "atlas_background_schedule_delay_seconds",
    "How late a loop woke up compared with when it planned to (scheduler and thread contention)",
    {"loop": BACKGROUND_LOOPS}, DELAY_BUCKETS)
MAIL_OUTCOMES = ("sent", "failed")
MAIL_CLASSES = ("none", "transient", "permanent", "auth", "connection", "protocol", "invalid")
mail_deliveries = REGISTRY.counter(
    "atlas_mail_deliveries_total",
    "SMTP delivery attempts: class is `transient` (4xx reply), `permanent` (5xx reply or refused sender or "
    "recipients), `auth`, `connection`, `protocol` (another SMTP error) or `invalid` (a message the library refused "
    "to build); `none` for a sent message",
    {"outcome": MAIL_OUTCOMES, "class": MAIL_CLASSES})

# ---- identity, consent, tokens and authorization (#76) ------------------------------------------------------
IDENTITY_FLOWS = ("password", "oidc_signin", "oidc_link")
IDENTITY_OUTCOMES = ("ok", "bad_credentials", "rate_limited", "provider_cancelled", "state_invalid",
                     "state_expired", "browser_mismatch", "provider_refused", "verification_failed",
                     "link_conflict", "not_allowed", "no_account", "registration_closed", "account_unavailable")
identity_flows = REGISTRY.counter(
    "atlas_identity_flows_total", "Sign-in and account-link flows by terminal outcome (one per attempt)",
    {"flow": IDENTITY_FLOWS, "outcome": IDENTITY_OUTCOMES})
oidc_consent = REGISTRY.counter(
    "atlas_oidc_consent_decisions_total",
    "Atlas as OpenID provider: consent screen decisions (allowed, denied by the user, invalid_request: an "
    "authorization request refused before any consent was shown or recorded)",
    {"decision": ("allowed", "denied", "invalid_request")})
oidc_stage = REGISTRY.histogram(
    "atlas_oidc_stage_duration_seconds",
    "OpenID stage duration: authorize, token and userinfo (Atlas as provider), callback (Atlas as relying party: "
    "state check, code exchange and id-token verification)",
    {"stage": ("authorize", "token", "userinfo", "callback"), "outcome": ("ok", "error")}, LATENCY_BUCKETS)
TOKEN_OUTCOMES = ("ok", "invalid", "expired", "reused", "redirect_mismatch", "pkce_failed", "account_unavailable",
                  "client_disabled", "client_auth_failed", "unsupported_grant")
token_operations = REGISTRY.counter(
    "atlas_token_operations_total", "OpenID provider token lifecycle (never a token value)",
    {"token": ("authorization_code", "access_token"), "operation": ("issue", "exchange", "validate", "revoke"),
     "outcome": TOKEN_OUTCOMES})
authorization = REGISTRY.counter(
    "atlas_authorization_decisions_total",
    "API token checks by required scope: allowed, or denied with reason missing_token, insufficient_scope or "
    "rate_limited", {"scope": SCOPES, "decision": ("allowed", "denied"),
                     "reason": ("ok", "missing_token", "insufficient_scope", "rate_limited")})
RATE_LIMIT_OPERATIONS = ("api", "login", "register", "password_reset", "email_verification", "mfa", "oidc_token",
                         "oidc_callback", "group", "import", "account")
auth_rate_limit = REGISTRY.counter(
    "atlas_auth_rate_limit_decisions_total", "Rate-limiter decisions by fixed operation class",
    {"operation": RATE_LIMIT_OPERATIONS, "decision": ("allowed", "limited")})


def rate_limit_operation(key: str) -> str:
    """Map a limiter key (which holds addresses and ids) to a fixed operation class; the key is never exported."""
    head = key.split(":", 1)[0]
    if head in ("t", "ip"):
        return "api"
    if head == "oidc":
        return "oidc_callback" if key.startswith("oidc:cb:") else "oidc_token"
    mapping = {"login": "login", "register": "register", "forgot": "password_reset", "reset": "password_reset",
               "verify": "email_verification", "email": "email_verification", "mfa": "mfa", "import": "import",
               "import-search": "import"}
    if head in mapping:
        return mapping[head]
    if head.startswith("group"):
        return "group"
    return "account"
