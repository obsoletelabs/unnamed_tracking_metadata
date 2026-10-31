"""Generate the reference pages of the guide from the code, so they cannot drift.

    python tools/gen_docs.py          # rewrite docs/guide/reference-*.md
    python tools/gen_docs.py --check  # exit 1 if they are out of date (used by the test suite)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas import runtime  # noqa: E402
from atlas.main import create_app  # noqa: E402
from atlas.settings import Settings  # noqa: E402

# One sentence per environment variable. A test fails if a setting is added without an entry here.
ENV_HELP: dict[str, tuple[str, str]] = {
    # name: (group, text)
    "role": ("Node", "`authority` (the source of truth; publishes the signed change feed), `mirror` (replicates an authority) "
             "or `local` (a private node that caches providers and may replicate)."),
    "database_url": ("Node", "SQLAlchemy URL. `sqlite:///./atlas.db` for local nodes; `postgresql+psycopg://user:pw@host/db` "
                     "for authorities (required in production)."),
    "public_url": ("Node", "The address people use to reach this node. Used in emails, OIDC and links. Must be `https://` "
                   "in production."),
    "environment": ("Node", "`dev` shows API docs at `/docs` and relaxes production checks; `production` enforces HTTPS, "
                    "PostgreSQL for authorities and an explicit initial administrator."),
    "auto_migrate": ("Node", "Run database migrations on start. Default: on for SQLite, off otherwise. With several replicas "
                     "set `false` and run `atlas migrate` as a job."),
    "secret_key": ("Node", "Encrypts secrets stored in the database (peer tokens, OIDC client secrets, provider credentials entered in Admin → Integrations). Generate one with "
                   "`atlas secret-key`. Losing it makes those secrets unreadable."),
    "bootstrap_admin_token": ("Node", "32+ random characters. Becomes an admin API token while set; removing it revokes it. "
                              "Use it to create the first real tokens."),
    "initial_admin": ("Node", "Username that becomes the website administrator when it registers. If unset, the first account "
                      "to register is the administrator (production with open registration refuses to start without it)."),
    "upstream_url": ("Federation", "Address of an upstream node to replicate from (simple single-upstream setups; more can be "
                     "added under Admin → Peers)."),
    "upstream_token": ("Federation", "API token (scopes `read`, `sync`) for `upstream_url`."),
    "trusted_keys": ("Federation", "JSON object `{\"<node id>\": \"<base64 public key>\"}` of upstream nodes whose signed feed "
                     "you trust. Required when `upstream_url` is set. Get the values from `atlas node` on the upstream."),
    "sync_interval_seconds": ("Federation", "How often to pull from peers (minimum 5)."),
    "sync_batch_size": ("Federation", "Feed entries fetched per request."),
    "sync_max_parked_backlog": ("Federation", "Entries a follower keeps downloading, verifying and relaying after the "
                                "first entry it had to park (it needs a newer release). Above this it pauses downloading "
                                "and reports `backlog_full`."),
    "client_changes_retain_days": ("Limits", "Days the local client change log (`GET /api/v2/changes?feed=local`) keeps "
                                   "superseded rows. A daily job then deletes every row that has a newer row for the "
                                   "same record; the newest row of each record is always kept. A client whose cursor "
                                   "is older than the oldest kept row gets `\"reset\": true` and must re-read."),
    "min_follower_protocol": ("Federation", "Lowest federation protocol a follower must speak to sync from this node "
                              "(followers below it refuse). Default: the previous release's protocol."),
    "emit_protocol": ("Federation", "Newest federation protocol this node's own feed may use. Unset = "
                      "`min_follower_protocol`. Raise it in Admin → Peers once every follower is upgraded."),
    "inherit_schema": ("Federation", "Adopt the upstream authority's media types and custom fields (yours always take "
                       "precedence). Editable in Admin → Settings."),
    "oidc_provider_enabled": ("Federation", "Let other nodes and the Tracking App sign users in with accounts on this node "
                              "(OpenID Connect). Editable in Admin → Settings."),
    "auto_match_threshold": ("Matching", "Confidence (0.5–1.0) at or above which an incoming record is merged into an "
                             "existing one automatically. Editable as a percentage in Admin → Settings."),
    "review_threshold": ("Matching", "Confidence below the auto threshold but above this opens a possible-duplicate review."),
    "source_trust": ("Matching", "JSON object of source name → trust weight used to choose between conflicting values "
                     "(higher wins). Reserved names need the `curate` scope to submit."),
    "default_trust": ("Matching", "Trust for sources not listed in `source_trust` (apps and plugins submitting data)."),
    "redistributable_sources": ("Matching", "Sources whose curated observations are published on the change feed. Provider "
                                "data (Steam, IGDB…) is never federated."),
    "max_batch": ("Limits", "Largest observation batch accepted by the API."),
    "max_resolve": ("Limits", "Largest `/resolve` batch."),
    "rate_limit_per_minute": ("Limits", "API requests per token per minute (unauthenticated callers get 60 per address)."),
    "max_body_bytes": ("Limits", "Largest request body accepted."),
    "steam_enabled": ("Providers", "Use Steam's public store API. Can also be switched in [Admin → Integrations](administering.md#integrations)."),
    "rawg_api_key": ("Providers", "Enables the RAWG provider. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "igdb_client_id": ("Providers", "Twitch client id; with the secret enables the IGDB provider. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "igdb_client_secret": ("Providers", "Twitch client secret for IGDB. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "steam_api_key": ("Providers", "Steam Web API key. Without it achievements have global percentages only (no names). Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "provider_ttl_seconds": ("Providers", "How long a provider answer is cached."),
    "provider_negative_ttl_seconds": ("Providers", "How long “not found” is cached."),
    "media_dir": ("Artwork", "Directory for cached images (content-addressed). Mount a volume here; it must be writable by the user Atlas runs as "
                  "(uid 10001 in the official image). See [Troubleshooting](troubleshooting.md#artwork-doesnt-show)."),
    "media_cache_enabled": ("Artwork", "Download and verify artwork in the background. Editable in Admin → Settings."),
    "media_max_bytes": ("Artwork", "Largest image accepted (bytes)."),
    "media_max_pixels": ("Artwork", "Largest image by pixel count (decompression-bomb guard)."),
    "media_cache_batch": ("Artwork", "Images processed per background pass."),
    "media_allowed_hosts": ("Artwork", "If non-empty, only these hostnames may be downloaded from. Always public addresses only."),
    "backup_dir": ("Backups", "Folder for scheduled backups (use a persistent volume). Unset = no scheduled backups."),
    "backup_interval_hours": ("Backups", "Hours between scheduled backups (default 24)."),
    "backup_keep": ("Backups", "How many scheduled backups to keep (default 14)."),
    "node_kind": ("Node", "Badge shown to visitors and peers: `auto` (follows the role), `official`, `community`, `mirror` or `local`. "
                  "`official` is only a claim; people trust it by pinning your key. Editable in Admin → Settings."),
    "node_operator": ("Node", "Who runs this node. Editable in Admin → Settings."),
    "app_name": ("Branding", "Site name. Editable in Admin → Settings."),
    "tagline": ("Branding", "Home page line. Editable in Admin → Settings."),
    "footer_text": ("Branding", "Footer text. Editable in Admin → Settings."),
    "accent_color": ("Branding", "Accent colour, dark theme. Editable in Admin → Settings."),
    "accent_color_light": ("Branding", "Accent colour, light theme. Editable in Admin → Settings."),
    "default_theme": ("Branding", "`auto`, `dark` or `light`. Editable in Admin → Settings."),
    "logo_hash": ("Branding", "Set by uploading a logo in Admin → Settings; not meant to be set by hand."),
    "nav_links": ("Branding", "Extra menu links. Editable in Admin → Settings."),
    "mail_backend": ("Email", "`outbox` stores messages for admins to read and sends nothing; `smtp` delivers."),
    "smtp_host": ("Email", "SMTP server."),
    "smtp_port": ("Email", "587 (STARTTLS) or 465 (implicit TLS)."),
    "smtp_user": ("Email", "SMTP username (empty for none)."),
    "smtp_password": ("Email", "SMTP password. Only ever read from the environment, never stored in the database."),
    "smtp_starttls": ("Email", "Upgrade the connection with STARTTLS."),
    "mail_from": ("Email", "From address, e.g. `Atlas <noreply@example.org>`."),
    "email_verification": ("Community", "`off`, `optional` or `required`. Unset = by role: authorities require a confirmed "
                           "address to contribute, other nodes do not."),
    "registration": ("Community", "`open` or `closed`."),
    "session_ttl_days": ("Community", "Sign-in lifetime."),
    "require_mfa_for_privileged": ("Community", "Require TOTP enrolment and verified sessions for reviewers and admins."),
    "json_logs": ("Limits", "Opt-in structured request/application logs; no query, body or credential logging."),
    "metrics_enabled": ("Limits", "Enable `/metrics`; a `metrics`-scoped bearer token is required."),
    "auto_approve_reputation": ("Community", "Reputation from which a user's changes apply immediately (0 disables)."),
    "curator_reputation": ("Community", "Reputation that grants review rights (0 = only people you promote)."),
    "max_pending_proposals": ("Community", "Pending proposals per user."),
    "new_user_daily_proposals": ("Community", "Proposals per day for users who are not trusted yet."),
    "trusted_daily_proposals": ("Community", "Proposals per day for trusted users (reviewers are not capped)."),
    "password_min_length": ("Community", "Minimum password length (8–64)."),
    "community_art_cap": ("Community", "Community-contributed images per game."),
    "comments_enabled": ("Community", "Comments on games and proposals."),
    "lists_enabled": ("Community", "Personal lists and follows."),
    "comment_daily_limit": ("Community", "Comments per user per day."),
    "comment_min_reputation": ("Community", "Reputation needed to comment."),
    "comment_autohide_reports": ("Community", "Open reports from distinct users that hide a comment pending review."),
    "max_lists_per_user": ("Community", "Lists per user."),
    "max_list_items": ("Community", "Games per list."),
    "max_follows": ("Community", "Follows per user."),
}


# One line per endpoint, kept here so the reference reads well. A test fails when an endpoint has no entry.
SUMMARIES: dict[tuple[str, str], str] = {
    ("get", "/health"): "Liveness: the process is up.",
    ("get", "/ready"): "Readiness: the database answers and its schema is current (503 otherwise).",
    ("get", "/schema"): "Media types and custom fields this node describes (public ones only).",
    ("get", "/site"): "Name, theme, navigation, role, registration and email policy of this node.",
    ("get", "/games"): "Search records by title (`q`, optional `type`, `limit`). Includes `custom` values.",
    ("get", "/games/{cid}"): "One record. Follows merge redirects (`redirected_from`). ETag = id-version.",
    ("get", "/games/{cid}/achievements"): "Merged achievement list: name, description, kind, tier, points, icons, global percent, and a stable `key` (`ak1:…`) with `keys` and `key_identity`. Hidden ones are redacted unless `reveal=all` or `reveal=<id or key>,…` is sent with a signed-in user's token or a curator token. No per-user progress. See [Achievements](achievements.md).",
    ("get", "/games/{cid}/artwork"): "Artwork by trust and priority, plus the best image per kind; `media_url` once cached, and `image_url` always.",
    ("get", "/artwork/{art_id}/image"): "One image: its cached bytes, or a redirect to the original public address while it is not cached.",
    ("get", "/games/{cid}/explain"): "Which source and rule produced each field (provenance).",
    ("get", "/media/{digest}"): "A cached, verified image. Immutable; `nosniff`.",
    ("post", "/resolve"): "Batch (max `max_resolve`): store ids / titles to canonical ids with method and confidence. Read-only.",
    ("post", "/observations"): "Submit a batch of observations (the app's ingestion contract v1). Personal facts are discarded.",
    ("put", "/games/{cid}/achievements"): "Replace the curated achievement set for a record.",
    ("put", "/games/{cid}/artwork"): "Replace the curated artwork set for a record.",
    ("put", "/games/{cid}/custom"): "Set or clear curated custom-field values.",
    ("delete", "/games/{cid}/locks/{field}"): "Remove a pinned field value.",
    ("put", "/games/{cid}/locks/{field}"): "Pin a field value; it beats every merged value.",
    ("post", "/games/{cid}/merge"): "Merge a record into another (leaves a redirect).",
    ("get", "/games/{cid}/observations"): "Raw source records behind a record (current and superseded).",
    ("get", "/changes"): "Bounded client change page: canonical ids, current versions and merge redirects. `feed=local` pages this node's local change log (every change, provider imports included); the default is the signed feed.",
    ("post", "/observations/{oid}/detach"): "Split one source record off onto its own record.",
    ("get", "/review"): "Open possible-duplicate and id-conflict items.",
    ("post", "/review/{rid}/dismiss"): "Mark a review item as not a duplicate.",
    ("post", "/review/{rid}/merge"): "Resolve a review item by merging into one of its candidates.",
    ("get", "/providers"): "Providers configured on this node and their request budgets.",
    ("post", "/providers/{name}/fetch"): "Fetch full details from a provider (cached), merge, return the record.",
    ("get", "/providers/{name}/search"): "Lightweight provider search; nothing is stored.",
    ("get", "/sync/changes"): "Signed entries after `since`; optional `node_id` selects a retained original authority feed.",
    ("get", "/sync/info"): "Stable trust root, current signer, signed successor proofs, available original feeds, and the federation protocol, feed kinds and capabilities this node speaks.",
    ("get", "/status"): "Role, feed head, replication cursors, the number of feed entries that failed to apply and the named heads per upstream.",
    ("get", "/sync/status"): "Protocol versions, named heads (observed, downloaded, verified, applied), parked and quarantined entries per upstream, and the protocol each follower last spoke.",
    ("get", "/sync/digest"): "Digest of the replicated observations of this node's feed (or a retained one with `node_id`), for `atlas sync reconcile`. Observations only; cached up to 60 s (`cached_seconds`).",
    ("get", "/sync/failures"): "Mirrored feed entries that could not be applied and still need attention: parked (need a newer release) or quarantined.",
    ("post", "/sync/failures/retry"): "Retry failed mirrored entries now, whatever the automatic attempt count.",
    ("get", "/tokens"): "List API tokens.",
    ("post", "/tokens"): "Create a token; the secret is returned once.",
    ("delete", "/tokens/{tid}"): "Revoke a token.",
}


def env_name(field: str) -> str:
    return "ATLAS_" + field.upper()


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def fmt_default(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, (set, dict, list)):
        return "`" + md_escape(str(sorted(value) if isinstance(value, set) else value)) + "`" if value else "—"
    return f"`{md_escape(str(value))}`" if str(value) else "—"


def reference_env() -> str:
    lines = ["# Environment variables", "",
             "_Generated by `tools/gen_docs.py` from the code. Do not edit by hand._", "",
             "Every setting is read from an environment variable named `ATLAS_<NAME>` (or from a `.env` file). Settings marked "
             "**live** can also be changed at runtime in **Admin → Settings**; the environment value is then only the default.",
             ""]
    live = {s.key for s in runtime.REGISTRY} | runtime.EXTRA
    groups: dict[str, list[str]] = {}
    for name, field in Settings.model_fields.items():
        group, text = ENV_HELP[name]
        default = field.default if field.default_factory is None else field.default_factory()
        row = (f"| `{env_name(name)}` | {fmt_default(default)} | {md_escape(text)}"
               f"{' **live**' if name in live else ''} |")
        groups.setdefault(group, []).append(row)
    for group in ("Node", "Backups", "Federation", "Matching", "Limits", "Providers", "Artwork", "Branding", "Email", "Community"):
        lines += [f"## {group}", "", "| Variable | Default | Meaning |", "| --- | --- | --- |", *groups.get(group, []), ""]
    return "\n".join(lines)


def reference_settings() -> str:
    lines = ["# Admin settings", "",
             "_Generated by `tools/gen_docs.py` from the code. Do not edit by hand._", "",
             "These can be changed live in **Admin → Settings** without restarting. Each change is recorded in the audit "
             "log, and **Use default** returns a setting to its environment value.", ""]
    groups: dict[str, list[runtime.Spec]] = {}
    for spec in runtime.REGISTRY:
        groups.setdefault(spec.group, []).append(spec)
    base = Settings(_env_file=None)
    for group, specs in groups.items():
        lines += [f"## {group}", "", "| Setting | Type | Default | Notes |", "| --- | --- | --- | --- |"]
        for sp in specs:
            kind = {"int": "number", "bool": "on/off", "str": "text", "text": "text", "color": "colour",
                    "links": "list of links", "choice": " / ".join(sp.choices) or "choice"}[sp.kind]
            if sp.kind == "int" and sp.lo is not None:
                kind += f" ({sp.lo}–{sp.hi})"
            default = runtime.display(sp, runtime.from_attr(sp, getattr(base, sp.key)))
            lines.append(f"| **{sp.label}** (`{sp.key}`) | {md_escape(kind)} | "
                         f"{('`' + md_escape(default) + '`') if default else '—'} | {md_escape(sp.help)} |")
        lines.append("")
    return "\n".join(lines)


def reference_api() -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(_env_file=None, database_url=f"sqlite:///{tmp}/docs.db", role="authority",
                                  media_dir=f"{tmp}/media"))
        try:
            spec = app.openapi()
        finally:  # release the SQLite file before the temp dir is removed (Windows cannot delete an open file)
            app.state.atlas.maker.kw["bind"].dispose()
            app.state.atlas.http.close()
    scope_of: dict[tuple[str, str], str] = {}
    from atlas.api.routes import router as api_router

    for route in api_router.routes:  # app.routes only holds lazily included routers in recent FastAPI
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            scope = getattr(dep.call, "required_scope", None)
            if scope:
                for method in getattr(route, "methods", ()):
                    scope_of[(method.lower(), route.path)] = scope
    lines = ["# API reference", "",
             "_Generated by `tools/gen_docs.py` from the code. Do not edit by hand._", "",
             "Base path `/api/v2`. Send `Authorization: Bearer <token>`. Scopes: `read`, `contribute`, `curate`, `sync`, "
             "`admin` (`admin` implies all). Endpoints marked **public** need no token. Errors are "
             "`{\"error\": {\"code\", \"message\"}}`; validation errors are `422`; limits return `429` with `Retry-After`.", ""]
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, ops in sorted(spec["paths"].items()):
        if not path.startswith("/api/v2"):
            continue
        for method, op in ops.items():
            by_tag.setdefault((op.get("tags") or ["other"])[0], []).append((method, path, op))
    order = ["ops", "read", "write", "curate", "providers", "federation", "admin"]
    for tag in sorted(by_tag, key=lambda t: order.index(t) if t in order else 99):
        lines += [f"## {tag}", "", "| Method | Path | Scope | What it does |", "| --- | --- | --- | --- |"]
        for method, path, op in sorted(by_tag[tag], key=lambda x: (x[1], x[0])):
            scope = scope_of.get((method, path), "public")
            what = md_escape(SUMMARIES.get((method, path[7:]), "(undocumented)"))
            lines.append(f"| `{method.upper()}` | `{path[7:] or '/'}` | {scope} | {what} |")
        lines.append("")
    return "\n".join(lines)


PAGES = {
    "reference-env.md": reference_env,
    "reference-settings.md": reference_settings,
    "reference-api.md": reference_api,
}


def main(argv: list[str]) -> int:
    out = ROOT / "docs" / "guide"
    out.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, build in PAGES.items():
        text = build().rstrip() + "\n"
        path = out / name
        if "--check" in argv:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
    if stale:
        print("out of date: " + ", ".join(stale) + " (run python tools/gen_docs.py)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
