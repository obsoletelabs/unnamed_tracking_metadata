# Troubleshooting and FAQ

## The server won't start

| Message | Fix |
| --- | --- |
| `ATLAS_PUBLIC_URL must be https in production` | Use the real `https://` address, or `ATLAS_ENVIRONMENT=dev` while testing. |
| `an authority in production needs PostgreSQL` | Set `ATLAS_DATABASE_URL=postgresql+psycopg://…` (see `compose.authority.yaml`). |
| `production with open registration needs ATLAS_INITIAL_ADMIN` | Set the username that should administer the node (or close registration). |
| `ATLAS_TRUSTED_KEYS must list the upstream's key` | Pin the upstream with Admin → Peers instead, or set the key (from `atlas node` upstream). |

## People can't sign up or contribute

* **“an email address is required”**: this node requires verified email; give one.
* **“confirm your email address before contributing”**: open the link in the confirmation email (Profile shows a *send again* button). With `outbox` delivery nothing is sent; an administrator reads **Admin → Email** or marks the address confirmed.
* **“too many …”**: a throttle; wait the number of seconds shown.
* **Their changes don't appear**: they are waiting for review (new accounts) or were rejected (see **My account → My proposals**).

## Artwork is missing

The game's **Artwork** section shows how many images are known and cached. Pages and
the Tracking App plugin use only images downloaded and checked by this node. An
artwork suggestion awaiting review is not yet part of that download queue; check
**My account → My proposals** first.

Check **Admin → Settings → Artwork → Download and cache artwork**. The background
worker checks an idle queue every five seconds and continues immediately after a
full successful batch. Downloads run separately from account housekeeping and
never inside a page or API request. This polling interval is not a download-time
guarantee: upstream responses and the queue's size still matter. Disabling caching
stops new batches; an already running batch may finish.

Only public HTTP/HTTPS image addresses on ports 80/443 are eligible. DNS addresses
and redirects are checked, and any configured `ATLAS_MEDIA_ALLOWED_HOSTS` list must
include the image host. PNG, JPEG, WebP and GIF must pass the byte/pixel limits and
image validation. Failed downloads wait ten minutes before the first retry, then
back off further, up to seven days. A malformed URL fails that image without
blocking other queued images. `atlas cache-media` drains eligible batches until a
batch has no successful downloads; it does not bypass retry delays or safety checks.

If Atlas shows cached artwork but the Tracking App does not, check the Atlas
plugin's server address and read token, and whether the plugin runtime and host
can reach the node. The artwork API requires a read token; verified cached bytes
are served from the node's public `/media/<sha256>` path.

## Replication problems

Admin → Peers shows each peer's last error. Common ones: *not trusted* (the node id changed or was never pinned), *feed gap or rewind* (the upstream was restored from an older backup; re-pin after confirming with its administrator), *bad signature*, token missing the `sync` scope, unreachable upstream (the node keeps serving and retries).

## Artwork doesn't show

Images appear only after Atlas has downloaded and checked them (a background pass about once a minute, or `atlas cache-media`). Failures back off automatically. The source must be a public address; sizes above `ATLAS_MEDIA_MAX_BYTES`, non-images and SVG are refused.

## The API says 401/403/429

401: missing or invalid token (or the owner is suspended). 403: the token lacks the scope (see [API reference](reference-api.md)). 429: rate limit; obey `Retry-After`.

## FAQ

**Does Atlas store my playtime or library?** No. Personal facts in submissions are discarded.

**Can it track achievement completion?** No: only the list and global percentages.

**Can I use another database?** SQLite and PostgreSQL only.

**Can I run it without Docker?** Yes: `pip install .` then `atlas migrate` and `atlas serve` (Python 3.12+).

**How do I move from the old demo?** `atlas import-legacy path/to/demo-1.db`.

**Where are the screenshots from?** The demo data in `docs/guide/img`, produced by the project's own browser tour.
