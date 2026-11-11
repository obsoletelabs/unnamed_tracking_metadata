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
    "secret_key": ("Node", "Encrypts secrets stored in the database (the node signing key, peer tokens, OIDC client secrets, provider credentials entered in Admin → Integrations). Generate one with "
                   "`atlas secret-key`. Losing it makes those secrets unreadable, and a backup's node identity unrecoverable."),
    "secret_key_previous": ("Node", "Comma-separated retired secrets that can still decrypt while you rotate "
                            "`ATLAS_SECRET_KEY`. Run `atlas secrets rewrap`, then remove them."),
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
    "group_view_max_age_hours": ("Federation", "Node groups: how recent (hours, 1-168) a third member's view must be "
                                 "before delegated or privileged group actions."),
    "group_sync_interval": ("Federation", "Node groups: seconds between pulls of every member's state (30-86400)."),
    "group_profile_sync": ("Federation", "Node groups: sync the preferences linked users chose over the private "
                           "group channel. Off sends and applies nothing."),
    "group_profile_fields": ("Federation", "Node groups: space-separated preference names that may be synced; empty "
                             "means every registry field the user agrees to."),
    "group_hold_timeout_days": ("Federation", "Node groups: days a received preference waits for its missing history "
                                "before it is applied with a review entry (1-90)."),
    "group_journal_dir": ("Federation", "Node groups: the revocation journal directory. Every accepted CRL, "
                          "succession and cancel is appended here so a restore cannot roll revocations back; keep it "
                          "on persistent storage outside `media_dir` and `backup_dir`. Empty turns it off."),
    "group_max_delegated_role": ("Federation", "Node groups: the highest role a group grant may give here (`none`, "
                                 "`moderator`, `curator`, `admin`). The default `none` ignores every grant."),
    "group_federated_privilege": ("Federation", "Node groups: what a session opened through another node may do with "
                                  "local reviewer or administrator rights: `deny` (default), `local_mfa` (after this "
                                  "node's own second factor) or `allow`."),
    "group_trust_remote_mfa": ("Federation", "Node groups: accept the other node's fresh (under 10 minutes) second "
                               "factor in place of this node's own at a group sign-in. Off by default."),
    "group_delegated_requires_mfa": ("Federation", "Node groups: rights from a group grant apply only after this "
                                     "node's own second factor in the session. On by default."),
    "group_auto_provision": ("Federation", "Node groups: create a plain account (role user, no password, no email) "
                             "at a group sign-in when none is linked here. Needs open registration. Off by default."),
    "group_signin": ("Federation", "Node groups: accept sign-ins confirmed by another member. Turning it off ends "
                     "every session opened that way on the next request."),
    "group_signin_public": ("Federation", "Node groups: show a sign-in button per member on the login page (this "
                            "reveals group membership to visitors). Off by default."),
    "group_denied_accounts": ("Federation", "Node groups: space-separated group account ids whose grants are "
                              "ignored here."),
    "group_data_public_pages": ("Federation", "Scopes (C14): show data received from your other nodes to visitors, "
                                "read-only tokens and accounts without `scope.detail`, in the public field "
                                "categories only. Off by default. Also on Admin → Sharing."),
    "group_public_categories": ("Federation", "Scopes (C14): space-separated field categories visitors may see of "
                                "data from your nodes while `group_data_public_pages` is on (`identity release "
                                "platforms classification credits series external_ids cover_art` by default)."),
    "group_assist_ask": ("Federation", "Asking your nodes (C14): ask the other members of the owner group for "
                         "games this node waits for and images it cannot fetch. Also on Admin → Group."),
    "group_assist_answer": ("Federation", "Asking your nodes: what this node answers members with: `off`, "
                            "`public` (published data only, the default) or `group` (also its own provider data, "
                            "as `group_assist_share_provider_data` allows)."),
    "group_assist_share_provider_data": ("Federation", "Asking your nodes: which of this node's provider data it "
                                         "shares in the group with `group_assist_answer = group`: `none`, `fields`, "
                                         "`fields+media` or `fields+media+achievements`."),
    "group_assist_lookups": ("Federation", "Asking your nodes: provider lookups for members: `off`, `answer` (run "
                             "them from this node's keys) or `answer+ask` (also ask members)."),
    "group_assist_lookup_reserve": ("Federation", "Asking your nodes: percent of each provider budget kept for this "
                                    "node's own lookups (0-95, default 50)."),
    "group_assist_auto_adopt": ("Federation", "Asking your nodes: apply waiting changes a member reports it applied "
                                "without the \"Use what <member> has\" click (same-owner preset)."),
    "group_assist_group_before_origin": ("Federation", "Asking your nodes: fetch images from a member before their "
                                         "origin (same-owner preset); otherwise only when the origin fails."),
    "group_share_contribution_state": ("Federation", "Share the state of this node's proposals to a main Atlas with "
                                       "the other members of the owner group, and show theirs (on by default)."),
    "group_assist_max_jobs": ("Federation", "Asking your nodes: the most games queued at once (5000)."),
    "group_assist_run_limit": ("Federation", "Asking your nodes: games asked for per group round (50)."),
    "group_assist_give_up_days": ("Federation", "Asking your nodes: days after which a game nobody has is no longer "
                                  "asked for automatically (30)."),
    "group_assist_max_answer": ("Federation", "Asking your nodes: largest answer this node sends, in bytes "
                                "(512 KiB)."),
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
    "federation_bridge_record_obs": ("Federation", "Authority, emit protocol below 1.5: introduce a record its feed never "
                                     "signed (only provider data) with an ordinary observation of the source "
                                     "`atlas-record` (title and store ids), which every follower release applies. Off: "
                                     "such records reach followers only once the emit protocol is 1.5 (`record` "
                                     "entries). See [Changes waiting for their game]"
                                     "(federation.md#changes-waiting-for-their-game)."),
    "federation_introduce": ("Federation", "Authority: `referenced` introduces a record when a signed entry first names "
                             "it; `all` also introduces every record made from provider data alone as it is created."),
    "federation_snapshot_fields": ("Federation", "What a `record` (or bridge observation) carries besides the id and "
                                   "media type: a JSON list, a subset of `title` and `provider_ids`."),
    "sync_max_open_failures": ("Federation", "Follower: open waiting and quarantined entries per upstream feed before it "
                               "stops applying that feed and reports `backlog_full` (nothing is lost; the entries stay "
                               "retained)."),
    "sync_wake_batch": ("Federation", "Follower: waiting entries applied per transaction when their record arrives "
                        "(1-5000); the retry pass of the same pull applies the rest."),
    "sync_enrich_from_own_providers": ("Federation", "Follower (owner decision H56, off by default): when a `record` "
                                       "arrives, fetch the game from this node's own providers by the store ids it "
                                       "carries, so art and achievements arrive without redistribution. See "
                                       "[Changes waiting for their game](federation.md#changes-waiting-for-their-game)."),
    "sync_enrich_per_peer_per_hour": ("Federation", "Follower: at most this many own-provider fetches per upstream "
                                      "feed and hour (1-10000); the rest wait for the next hour."),
    "sync_media_prefer_peer": ("Federation", "Follower: fetch an image first from the peer whose feed delivered its "
                               "artwork row (`/api/v2/sync/media/{sha256}`, hash checked), then from the original "
                               "address."),
    "federation_media_unknown_licence": ("Federation", "Whether `/api/v2/sync/media` hands a pinned peer the cached bytes "
                                         "of a curated image whose licence is unknown (none stated). Off: only `cc0` "
                                         "and owner uploads are served; peers fetch the rest from the original "
                                         "address."),
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
    "field_source_preference": ("Matching", "JSON object of field → list of sources, best first, for example "
                                "`{\"description\": [\"igdb\", \"steam\"]}`. It reorders sources of the `provider` "
                                "trust class only: it never lifts a provider above community or curator data "
                                "(see Matching and provenance)."),
    "sync_origin_class_cap": ("Matching", "JSON object of node id → highest trust class (`curator`, `publisher`, "
                              "`community`, `provider`) its replicated observations may have. Unlisted nodes: a "
                              "configured upstream authority (a node in `ATLAS_TRUSTED_KEYS` or under Admin → Peers) "
                              "is `curator`, every other origin `provider`."),
    "review_dismiss_days": ("Matching", "Days a dismissed review pair stays closed: the same pair is not reopened "
                            "within this window unless its evidence gets stronger."),
    "match_decision_retain_days": ("Matching", "Days match decisions are kept. The newest decision of each source "
                                   "record and every decision an open review or a live merge refers to are always "
                                   "kept."),
    "max_batch": ("Limits", "Largest observation batch accepted by the API."),
    "max_resolve": ("Limits", "Largest `/resolve` batch."),
    "rate_limit_per_minute": ("Limits", "API requests per token per minute (unauthenticated callers get 60 per address)."),
    "max_body_bytes": ("Limits", "Largest request body accepted."),
    "steam_enabled": ("Providers", "Use Steam's public store API. Can also be switched in [Admin → Integrations](administering.md#integrations)."),
    "rawg_api_key": ("Providers", "Enables the RAWG provider. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "igdb_client_id": ("Providers", "Twitch client id; with the secret enables the IGDB provider. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "igdb_client_secret": ("Providers", "Twitch client secret for IGDB. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "steam_api_key": ("Providers", "Steam Web API key. Without it achievements have global percentages only (no names). Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value."),
    "tgdb_api_key": ("Providers", "TheGamesDB API v1 key (requested from TheGamesDB; it has a monthly allowance); enables the TheGamesDB provider. Its data is kept on this node only. Can also be set in [Admin → Integrations](administering.md#integrations) (stored encrypted; needs `ATLAS_SECRET_KEY`), which overrides this value. See [Metadata providers](providers.md)."),
    "openlibrary_enabled": ("Providers", "Use Open Library (keyless, CC0) to search and import books. Needs a `book` media type. Can also be switched in [Admin → Integrations](administering.md#integrations)."),
    "wikidata_enabled": ("Providers", "Suggest store links (GOG, Epic, itch.io, TheGamesDB, Steam) from the ids a game already has, using Wikidata (keyless, CC0). Can also be switched in [Admin → Integrations](administering.md#integrations)."),
    "price_sources": ("Providers", "Comma-separated price sources `atlas prices fetch` may query, e.g. `steam`. Empty: price fetching is off. Read the source's terms in [Prices](prices.md#fetching-prices-from-a-store) first."),
    "provider_ttl_seconds": ("Providers", "How long a provider answer is cached."),
    "provider_negative_ttl_seconds": ("Providers", "How long “not found” is cached."),
    "media_dir": ("Artwork", "Directory for cached images (content-addressed). Mount a volume here; it must be writable by the user Atlas runs as "
                  "(uid 10001 in the official image). See [Troubleshooting](troubleshooting.md#artwork-doesnt-show)."),
    "self_service_sync_tokens": ("Federation", "Let members create their own read-only tokens with the `sync` scope "
                                 "(Profile → API tokens), to follow this node from their own node or app. Off by "
                                 "default; turning it off stops those tokens' sync access at once. Editable in "
                                 "Admin → Settings."),
    "media_cache_enabled": ("Artwork", "Download and verify artwork in the background. Editable in Admin → Settings."),
    "media_max_bytes": ("Artwork", "Largest image accepted (bytes)."),
    "media_max_pixels": ("Artwork", "Largest image by pixel count (decompression-bomb guard)."),
    "media_cache_batch": ("Artwork", "Images processed per background pass."),
    "media_https_only": ("Artwork", "New downloads need `https` (C12 §12.6, H40). A redirect from https to http is "
                         "refused either way."),
    "media_fetch_deadline_s": ("Artwork", "Seconds one download may take in all, redirects and body included (5-60)."),
    "media_host_concurrency": ("Artwork", "Simultaneous downloads from one host, per process."),
    "media_host_rpm": ("Artwork", "Downloads from one host per minute, per process. Over the limit an image waits a "
                       "minute; it is not counted as a failure."),
    "media_max_frames": ("Artwork", "Most frames an animated image may have; the frames together may have at most 4 x "
                         "`ATLAS_MEDIA_MAX_PIXELS` pixels."),
    "media_policy": ("Artwork", "JSON overrides of the per-source media policy (C12 §12.5.5), for example "
                     "`{\"steam\": {\"fetch\": \"redirect\"}}`. Fields: `fetch` (`proxy`, `redirect`, `none`), "
                     "`federate`, `licence`, `attribution`, `redirect_hosts`. An unknown field or value stops the "
                     "node at start-up."),
    "achievement_percent_max_age_days": ("Limits", "Days after which an achievement's rarity is shown as stale and a "
                                         "fresher value from another source wins (C12 §12.1.6)."),
    "achievement_retired_retention_days": ("Limits", "Days a retired achievement row (one its source no longer lists) "
                                           "is kept, with its id and key, before it is deleted (C12 §12.1.3, H37)."),
    "media_allowed_hosts": ("Artwork", "If non-empty, only these hostnames may be downloaded from. Always public addresses only. A provider image not cached yet may also be redirected to these hosts by `/api/v2/artwork/{id}/image`."),
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
    "metrics_dir": ("Limits", "Several worker processes on one host: a private directory they share (writable by "
                    "Atlas only). Each worker writes its counters there and any worker's `/metrics` reports all of "
                    "them. Empty: every process reports only itself."),
    "metrics_cache_seconds": ("Limits", "How long the expensive `/metrics` collectors (catalogue aggregates, the "
                              "media directory walk) are cached in each process; 0 recomputes them on every scrape."),
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
    ("get", "/ready"): "Readiness: 503 when the database does not answer or migrations are pending; `degraded` (200) when only artwork storage is unwritable.",
    ("get", "/schema"): "Media types and custom fields this node describes (public ones only).",
    ("get", "/site"): "Name, theme, navigation, role, registration and email policy of this node.",
    ("get", "/games"): "Search records by title (`q`, optional `type`, `limit`, `offset`) and/or by first release (`released_from`, `released_to`: `YYYY`, `YYYY-MM` or `YYYY-MM-DD`; a record matches when its whole release period is inside the range). `sort`: `relevance`, `title`, `release` or `-release` (year before month before day, unknown last); `relevance` re-ranks exact and prefix matches within each page of the title order, so page with `title` or `release` for a stable order. `q` is required unless a release filter is given (a blank `q` lists recent records, as before). Includes `custom` values.",
    ("get", "/games/{cid}"): "One record. Follows merge redirects (`redirected_from`). ETag = id-version. Release dates: `first_release` (`{date, precision}`), `release_year`, `releases` (up to five `{date, precision, platform?, region?, edition?, category}`) and the unchanged day-only `release_date`, present only when the first release is known to the day. `relations`: `parents`, `children`, `members_of`, `members` (each `{id, type, title, media_type}`, ordered by type then id, at most 200) with `counts` and `truncated`; `editions` when known. See [Relationships](relations.md).",
    ("get", "/games/{cid}/relations"): "The relation graph around a record: `depth` hops (1-4) in both directions, at most `limit` (200) records, deterministic order, `truncated` when more exists; plus the record's `editions`. See [Relationships](relations.md).",
    ("put", "/games/{cid}/scope"): "Keep a record on this node only (`{ceiling: \"local\"}`) or publish it (`{ceiling: null}`), CONTRACT-C14 §5.3. A record this feed already introduced cannot be kept back (409 `conflict`). See [Scopes and sharing](scopes.md).",
    ("put", "/games/{cid}/relations"): "Replace the record's own outgoing relations (`{relations: [{to, type}]}`, at most 200). Self relations, cycles, a second parent of the same kind and memberships that do not point at a series or collection are refused and nothing changes. Published at `emit_protocol` 1.4, kept local below: the answer says `published: false` with `reason` (`emit_protocol` with `pending`, or `local_node`), and `changed.published` counts relations stored while gated that this call signed.",
    ("get", "/games/{cid}/prices"): "The newest observed price of every offer of a record, one per provider, product, package, edition, platform, region and currency (optional `region`, `currency`, `provider`). Integer minor units with the ISO 4217 `exponent`, exact decimal strings, `sale`, `stale`. Non-redistributable prices only for curator tokens. Not part of the record's view; never in the feed. See [Prices](prices.md).",
    ("get", "/games/{cid}/prices/history"): "Retained price observations of a record in one `region` (required), newest first, filtered by `currency`, `provider`, `product_id`, `edition`, `platform`, `since`, `until`; at most 1000 per page, follow `next_cursor`. With `currency`, `lowest` is the lowest retained price (the historical low within retention). See [Prices](prices.md).",
    ("post", "/prices"): "Store up to 500 price observations (integer minor units; floats, unknown currencies or regions and inconsistent discounts are a 422). Per item: `stored`, `duplicate`, `conflict`, `unknown_record`, `expired` or `invalid`. Nothing is published. See [Prices](prices.md).",
    ("get", "/admin/peers/probe"): "Look a node up before following it (read-only): its id and protocol, what the token can do, its record counts and the feeds it offers, with a recommended one. The peer's token goes in the `Atlas-Peer-Token` header. 400 bad address, 502 not verifiable, 504 no answer.",
    ("get", "/prices/status"):"Stored price observations, how many the next purge removes, retention bounds, and the price channel's state (protocol 1.6, disabled in this release).",
    ("get", "/sync/price-changes"): "The price channel (protocol 1.6, capability `price-channel-v1`). Designed but disabled in this release: always 404.",
    ("get", "/games/{cid}/achievements"): "Merged achievement list: name, description, kind, tier, points, icons, global percent, and a stable `key` (`ak1:…`) with `keys` and `key_identity`. Hidden ones are redacted unless `reveal=all` or `reveal=<id or key>,…` is sent with a signed-in user's token or a curator token. No per-user progress. See [Achievements](achievements.md).",
    ("get", "/games/{cid}/artwork"): ("Artwork in its long-standing order (community choice, trust, priority) with `primary`, the first image "
                                       "per kind in it, as before; `best`, the C12 quality pick per kind; `?order=quality` sorts the list (and "
                                       "`primary`) by the C12 ranking. `media_url` once cached, and `image_url` always."),
    ("get", "/artwork/{art_id}/image"): "One image: its cached bytes, or a redirect to the original public address while it is not cached.",
    ("get", "/games/{cid}/explain"): "Which source and rule produced each field (provenance).",
    ("get", "/media/{digest}"): "A cached, verified image. Immutable; `nosniff`.",
    ("post", "/resolve"): "Batch (max `max_resolve`): Atlas ids, provider ids and titles to canonical ids, with `reason` (id, redirect, crosswalk, conflict, type_mismatch, exact, fuzzy, ambiguous, none), `tier`, `matched_keys`, `conflict` and `redirected_from`. A conflict returns no id. Read-only. See [Tracking App](tracking-app.md#resolving-ids-post-resolve).",
    ("post", "/observations"): "Submit a batch of observations (the app's ingestion contract v1). Personal facts are discarded.",
    ("put", "/games/{cid}/achievements"): "Replace the curated achievement set for a record.",
    ("put", "/games/{cid}/artwork"): "Replace the curated artwork set for a record. Answers `count` (items given), `stored` (now curated) and `skipped`: each image another source of the record already lists (same kind and address), which stays that source's, as `{kind, url, held_by, reason: \"held_by_another_source\"}`.",
    ("put", "/games/{cid}/custom"): "Set or clear curated custom-field values.",
    ("delete", "/games/{cid}/locks/{field}"): "Remove a pinned field value.",
    ("put", "/games/{cid}/locks/{field}"): "Pin a field value; it beats every merged value. `release_date` takes `YYYY`, `YYYY-MM` or `YYYY-MM-DD`; `releases` takes a list of up to five release entries. A year/month `release_date` lock or a `releases` lock needs `emit_protocol` 1.3 on an authority (else 409 `emit_protocol`). `{\"clear\": true}` marks the field deliberately empty on this node.",
    ("post", "/games/{cid}/merge"): "Merge a record into another (leaves a redirect and a merge record that can be undone). `dry_run=1` previews the survivor's view and every field change; `override_distinct` merges records marked as different works.",
    ("get", "/games/{cid}/provenance"): "Why each field has its value: winning source, trust class, reason, runners-up, conflicts, plus recent decisions, merges and distinct marks. `detail=values` (curate) adds candidate values.",
    ("get", "/games/{cid}/provenance/observations"): "Every observation of a record, newest first, paged.",
    ("get", "/games/{cid}/decisions"): "Match decisions for a record and the records merged into it, newest first.",
    ("get", "/observations/{oid}/decision"): "The newest match decision recorded for one observation.",
    ("get", "/artwork/misfits"): "Measured images whose aspect does not suit their role (C12 §12.5.3), for curators.",
    ("get", "/conflicts"): "Records whose `field` has disagreeing sources in the winning trust class.",
    ("get", "/merges"): "Merge records of a record (`canonical_id`).",
    ("get", "/merges/{merge_id}"): "One merge record with its item counts.",
    ("post", "/merges/{merge_id}/unmerge"): "Undo a recorded merge (last in, first out; `If-Match` takes the survivor's ETag; `dry_run` previews). A merge this authority published needs `emit_protocol` 1.7 (409 `emit_protocol`).",
    ("get", "/distinct"): "Active \"different works\" decisions of a record.",
    ("post", "/distinct"): "Mark two records as different works: never merged or paired for review again (local to this node).",
    ("delete", "/distinct/{rejection_id}"): "End a \"different works\" decision.",
    ("post", "/review/{rid}/distinct"): "Resolve a review item as different works.",
    ("post", "/review/{rid}/detach"): "Resolve a review item by splitting one observation off its record.",
    ("get", "/games/{cid}/observations"): "Raw source records behind a record (current and superseded).",
    ("get", "/changes"): "Bounded client change page: canonical ids, current versions and merge redirects. `feed=local` pages this node's local change log (every change, provider imports included); the default is the signed feed.",
    ("post", "/observations/{oid}/detach"): "Split one source record off onto its own record.",
    ("get", "/review"): "Open possible-duplicate and id-conflict items with reason, evidence and priority (`order=priority`, `kind`).",
    ("post", "/review/{rid}/dismiss"): "Mark a review item as not a duplicate.",
    ("post", "/review/{rid}/merge"): "Resolve a review item by merging into one of its candidates.",
    ("get", "/providers"): "Providers configured on this node and their request budgets.",
    ("post", "/providers/{name}/fetch"): "Fetch full details from a provider (cached), merge, return the record.",
    ("get", "/group/assist"): "Counts of this node's questions to its owner group (CONTRACT-C14 §3): due, backing off, given up, dropped because the queue was full, and kept evidence items. See [Node groups](node-groups.md#asking-your-nodes).",
    ("post", "/group/assist/ask"): "Ask the owner group now for records this node lacks (`{ids: [...]}`, \"Ask my nodes\"), also ones it gave up on. Answers arrive in a later group round. 409 when the node is in no group or asking is off.",
    ("post", "/group/assist/lookup"): "A provider lookup through a member of the owner group (CONTRACT-C14 §4), for a provider this node has no key or budget for. With `record`, the result is kept on that record as group data (never public, never in this node's feed). Needs `group_assist_lookups = answer+ask`.",
    ("get", "/providers/{name}/search"): "Lightweight provider search; nothing is stored.",
    ("get", "/sync/changes"): "Signed entries after `since`; optional `node_id` selects a retained original authority feed.",
    ("get", "/sync/info"): "Stable trust root, current signer, signed successor proofs, available original feeds, and the federation protocol, feed kinds and capabilities this node speaks. Since protocol 1.5 also `own_head` (this node's own feed head), `introduced_through` (from that seq on, every game the feed names was introduced first; null until `atlas feed introduce` finished once), `media_withheld` and, per retained feed, `feeds[].empty`; an authority adds `records` (`live`, `introduced`, `not_federated`). Additive: older followers ignore them.",
    ("get", "/status"): "Role, feed head, replication cursors, the number of feed entries that failed to apply and the named heads per upstream.",
    ("get", "/sync/status"): "Protocol versions, named heads (observed, downloaded, verified, applied), parked, quarantined and waiting entries per upstream, the protocol each follower last spoke, and (additive) `peers[]`: each peer's id, name, what it follows, its status `{code, label, tone, detail, action}` and its waiting groups `waiting[]` (see [Peer status](federation.md#peer-status)).",
    ("get", "/sync/media/{digest}"): "The bytes of a cached image this node may hand to peers, by its lowercase SHA-256: an artwork row that is redistributable, not local-only, of a source that federates, licensed `cc0` or `owner_upload` (or a curated row of unknown licence when `ATLAS_FEDERATION_MEDIA_UNKNOWN_LICENCE` is on). Followers fetch replicated images here first and check the hash. 404 otherwise.",
    ("get", "/sync/digest"): "Digest of the replicated observations of this node's feed (or a retained one with `node_id`), for `atlas sync reconcile`. Observations only; cached up to 60 s (`cached_seconds`).",
    ("get", "/sync/failures"): "Mirrored feed entries that could not be applied and still need attention: parked (need a newer release), quarantined, or waiting for their game (`disposition` `wait`, `waiting_for`); `next_try_at` for quarantined ones.",
    ("post", "/sync/failures/retry"): "Retry failed mirrored entries now, whatever their backoff.",
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
