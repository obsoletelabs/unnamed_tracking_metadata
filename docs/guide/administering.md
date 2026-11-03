# Administering a node

Everything here is in the **Admin** area (administrators only). Almost nothing needs a restart or a code change.

![Admin settings](img/04-admin-settings.png)

## Overview

**Admin → Overview** shows the node's role and id, replication position, providers, users (role, status, email state, external sign-in permission) and a **recent activity log**: settings changed, proposals decided, roles changed, tokens created, peers added. Every moderation and admin action is recorded with who did it.

## Settings

Changes apply within seconds on every process, are validated, are recorded in the log, and can be undone with **Use default**. The full list with defaults is in [Admin settings](reference-settings.md). Groups:

* **Branding:** site name, tagline, footer, accent colours (dark/light), theme (auto/dark/light), navigation links, logo.
* **Community:** registration open/closed, email policy, reputation thresholds, proposal limits, comments and lists on/off with limits.
* **Security:** password length, sign-in lifetime, API rate limit, batch sizes.
* **Matching:** automatic-merge and review confidence.
* **Artwork:** caching on/off and size limit.
* **Email:** delivery mode, SMTP host/port/user/STARTTLS, from address.
* **Federation:** adopt upstream media types/fields; act as a sign-in provider.

Things deliberately **not** editable here (environment only): database URL, role, public URL, secret key, SMTP password, bootstrap token. See [Environment variables](reference-env.md).

### Logo and navigation

Upload a PNG/JPEG/WebP/GIF (≤512 KB, ≤1024 px; checked and re-served from the node). Navigation links are `Label | address` lines; addresses are `/paths` or `https://` only.

## Pages

**Admin → Pages** writes About, Rules, Help or anything else in **Markdown** (headings, lists, emphasis, links; raw HTML and images are ignored for safety). Pages can be drafts, ordered, and shown in the top menu. Three starter pages exist.

## Integrations

**Admin → Integrations** lists the metadata providers Atlas can import from (Steam, IGDB, RAWG): whether each is active, recent failures, and a form for its credentials. Credentials entered there are stored **encrypted in the database** (this needs `ATLAS_SECRET_KEY`, see [Environment variables](reference-env.md)), take effect immediately and override the environment values; leave a box empty to keep the current value, or tick *Remove* to fall back to the environment. Saved values are never displayed again, and the audit log records only which fields changed. Steam's public store API can be switched on or off there too.

**Linking a store id to an existing game.** Signed-in members use *Link a store page or id* on a game page (pick Steam, GOG, Epic, TheGamesDB, itch.io, IGDB or RAWG, or type another store's short name, and its id), or open **Find it by name**, which **suggests matches automatically**: it searches Steam, IGDB and RAWG (whichever the node has configured) for the game's own name, skips stores the game is already linked to, and lists the results with likely matches first (same title and, where known, the same year), each one click to propose. You can also search by hand. This searches Steam, IGDB or RAWG (whichever the node has configured) by the game's name and links the match you pick, with no id to look up. It is a reviewed contribution like any other (trusted accounts apply immediately, reviewers can revert). An id already used by another game is refused (the games should be merged instead), and so is replacing a different id the game already has for that store (propose an edit).

**Artwork cache.** The same page shows how many images are cached or failing, with *Retry uncached images now* (clears backoff so the next pass tries them) and *Clear cached images* (deletes downloaded provider images; uploads are kept; type `clear` to confirm). Turn automatic caching on or off in **Admin → Settings → Artwork**. Members can **upload an image file** from any record page instead of linking one: it is checked and stored by this node, then follows the normal review rules (trusted accounts apply immediately). Clients can request `GET /api/v2/artwork/{id}/image`: it serves the cached bytes. Otherwise a provider image (Steam, IGDB, RAWG, ...) is redirected to its original, but only when the host is one of that source's allow-listed `redirect_hosts` (or listed in `ATLAS_MEDIA_ALLOWED_HOSTS`), so the link works even with caching off; any other host answers 404, and a curated, community or uploaded image answers 404 until it is cached (C12, H50). See [Artwork quality](#artwork-quality-c12).

The page also lists the store namespaces Atlas links on game pages (`steam`, `gog`, `epic`, `tgdb`, `itch`, `igdb`, `rawg`): add ids as `namespace:id` lines when adding a game, or let a plugin submit them. GOG, Epic and TheGamesDB have no import adapter yet, so their ids are stored and linked but Atlas does not fetch data from them.

## Media types and custom fields

**Admin → Fields.** A **media type** says what a record is. Atlas starts with `game`, `anime`, `movie` and `tv_show`; each is seeded once with useful default labels and fields. You can add types such as *book* or *tool*. For each type you can:

* **rename core fields**, so a movie's *Developer* reads *Director* and *Publisher* reads *Studio*;
* add **custom fields**: text, long text, number, date, yes/no, choice (with options), list, link (`https://`). Each has a label, help text, order, **shown** (page and API) and **editable by contributors** switches.

The built-in anime fields are episode count, format, release status and source material. Movies start with runtime, writer, production countries and tagline. TV shows start with episode and season counts, episode runtime and release status. These are ordinary editable fields: admins can rename or remove them, and Atlas will not recreate a field after an admin removes it.

![Fields](img/05-admin-fields.png)

Custom values appear on the page, in the contributor form and the API (`custom`), are merged by source trust like core fields, can be proposed, reviewed and reverted, and move with records when merged. A field's *kind* cannot change after creation (create a new field instead). Deleting a field deletes its values. A type that has records can only be **disabled**, not deleted.

### Standard multiplayer fields

Every authority and local node starts with three fields on `game`: `players_min` and `players_max` (numbers) and `crossplay` (`yes`, `partial` or `no`; empty means **unknown**, never "no"). They are added once, on first start after upgrading; delete or relabel them freely and they will not come back. Mirrors receive them from their upstream. Steam imports set `crossplay` to `yes` when the store lists *Cross-Platform Multiplayer* (Steam never states `no`, so nothing is guessed). Curators set the rest with `PUT /games/{cid}/custom`; clients such as a game-night plugin read them from `custom` in `GET /games` and `GET /games/{id}`. Other providers can fill these fields by returning `custom` values from their adapter; undefined fields are skipped. A game whose maximum players is 1 and whose crossplay is empty shows **Single-player** on its page; that answer is worked out when the page is shown (contract C7.2), so it is never stored, never sent in the API or the feed, and never counted as "no". `not_applicable` cannot be stored in `crossplay`, even if you add it as a choice.


Authorities **replicate** their types and fields to mirrors and local nodes (Settings → *Adopt the upstream media types and fields*); a node's own definitions always win over an upstream one of the same name.

## Email

Set **Email → Delivery** to `smtp` and fill host, port, user and from; put the password in `ATLAS_SMTP_PASSWORD`. Messages are queued and sent by a background worker with retries and backoff, so a slow mail server never slows a page. **Admin → Email** lists every message and its state. With delivery `outbox` (the default) **nothing is sent**; the admin can read the messages, which is enough to try sign-up and password reset or to run a private node.

**Email verification policy:** `required` (default on authorities) means people confirm an address before they can contribute or comment; `optional` (default on other nodes) asks but doesn't block; `off` doesn't ask. Administrators are never blocked. **Mark confirmed** in the user list helps when mail isn't configured.

## Users

**Admin → Overview → Users:** set a role (*user*, *curator*, *admin*) and status (*active*, *suspended*). Suspending signs the person out everywhere and revokes their tokens. You cannot demote or suspend yourself. The first administrator is `ATLAS_INITIAL_ADMIN` (or the first account). That name is a one-time bootstrap: once an administrator exists, or any account has been deleted, registering the name again creates an ordinary user; `atlas make-admin <username>` promotes someone from the command line.

## Tokens

**Admin → Tokens** creates **service tokens** (for apps, mirrors, scripts) with scopes and optional expiry, shows the secret once, lists last use, and revokes. Scopes: `read`, `contribute`, `curate`, `sync` (replication), `admin`. People's own personal tokens appear here too (read-only, with their owner).

![Admin → Tokens](img/14-admin-tokens.png)

## Peers and sign-in sharing

* **Admin → Peers:** nodes this one replicates from. See [Federation](federation.md).
* **Admin → Sign-in sharing:** OpenID Connect both ways. See [Sign-in sharing](sso.md).

## Moderation

Reviewers handle proposals, duplicates and reported comments ([Reviewing](reviewing.md)). Administrators can also change anyone's role.

## Routine care

* Back up the database **and** the media volume ([Deployment](deployment.md#backups)).
* After upgrades, check **Admin → Overview** shows the feed head and no peer errors.
* After upgrading to the release with partial and regional release dates (migration 0031), run
  `atlas dates rebuild` once to re-derive every record with the new merge. It works in batches
  (`--batch`, default 500), can stop after `--limit` records, resumes where it stopped, and starts over
  with `--restart`. Records whose result does not change keep their version.
* Review the activity log occasionally; it is your audit trail.

## Artwork quality (C12)

* **Measured size and aspect.** Every cached image is measured; the measurement replaces the size a source declared
  (kept as `declared_width` / `declared_height`). The API reports the `aspect` class (`tall`, `portrait`, `square`,
  `landscape`, `capsule`, `wide`, `other`) and `role_fit` (whether it suits its kind: a cover is portrait, a banner
  capsule or wide, an icon square). Curators can list misfits with `GET /api/v2/artwork/misfits`. Images cached
  before this release are measured in the background, or at once with `atlas media backfill` (`atlas media stats`
  shows how many are left).
* **Duplicates.** Images of one record and kind with the same bytes are exact duplicates: the best-ranked one is
  shown, the others carry `duplicate_of` and are left out of the API list (unless `?include_duplicates=1`) and
  of the website.
  Perceptual near-duplicates are only reported to curators (`near_duplicate_of`), never hidden.
* **Ranking.** Curator images first, then the community's chosen default, then non-duplicates, role fit, source class
  and trust, region and language (`?region=jp&lang=ja`), priority and measured size. The website and the API's
  `best` (one image per kind) use it. The API list keeps its long-standing order by default, so an older client that
  shows the first image is not surprised; `?order=quality` sorts the list by this ranking.
* **Licence and attribution.** Each image may state `licence` (`provider_terms`, `publisher_press`, `owner_upload`,
  `cc0`, `cc-by`, `cc-by-sa`, `unknown`), `attribution` and `source_page_url`; provider images default to the
  provider's terms and name. Images under `cc-by`, `cc-by-sa`, `publisher_press` or `provider_terms`, and images
  marked `local_only`, are kept and served here but never signed into the feed (`licence_withheld` in the log). An
  upload is `owner_upload`; an upload under a licence that cannot enter the CC0 feed must be marked `local_only`.
* **Media policy per source.** `ATLAS_MEDIA_POLICY` overrides, per source, whether images are downloaded and served
  here (`proxy`, the default), only redirected to (`redirect`, with `redirect_hosts`), or never served (`none`).
* **Safe downloads.** New downloads need https (`ATLAS_MEDIA_HTTPS_ONLY`); a redirect from https to http is always
  refused. One download has `ATLAS_MEDIA_FETCH_DEADLINE_S` seconds in all; a response that says it is not an image
  is refused before its body is read; animated images are capped by frames and total pixels; each host gets at most
  `ATLAS_MEDIA_HOST_CONCURRENCY` downloads at a time and `ATLAS_MEDIA_HOST_RPM` per minute. Every failure is
  recorded as a code (`last_error`: `not_public`, `https_required`, `status_404`, `bad_type`, `too_large`,
  `timeout`, ...) and the API reports each image's `state` (`cached`, `pending`, `failing`, `not_fetched`, `blocked`).
* **Uploads** are decoded and written again without EXIF, XMP, ICC or text metadata (PNG and WebP losslessly, JPEG at
  quality 92, animated GIFs stay animated).
