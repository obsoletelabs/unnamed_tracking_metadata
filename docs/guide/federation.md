# Federation

Atlas nodes can follow each other. There is **no central server in the code**: any node can be an authority for its own community, and others can follow it.

## Roles

| Role | Writes | Replicates from | Notes |
| --- | --- | --- | --- |
| `authority` | accepts contributions and curation; publishes the signed change feed | nobody | PostgreSQL in production |
| `mirror` | provider cache and local-only overlays | an authority | can't rewrite records the upstream owns |
| `local` | everything on its own data | optionally an authority | SQLite; private overlays win locally |

## What replicates

Only things that are safe to republish:

* curated and community **observations** (sources `curator`, `manual`, `community` by default),
* **merges**, **splits** and **pinned fields** made on the authority,
* curated **artwork** and **achievements**, and curated **custom-field values**,
* **media types and custom-field definitions** (if the receiver adopts them).

What does **not** replicate: provider data (Steam, IGDB…; each node fetches its own under the provider's terms), accounts, passwords, reputation, comments, lists, follows, notifications, settings and pages. Mirrors and local nodes keep everything they cache themselves.

## How trust works

* Every node has an **Ed25519 identity** (its *node id* is a short fingerprint of the key). Look at **Admin → Peers → This node**.
* Each feed entry is **signed** (covering its sequence number, kind and content). A receiver accepts entries only from keys the **administrator pinned**.
* Entries must arrive **in order with no gaps**. A gap, a rewind, a bad signature, a different key or an altered kind is refused and nothing from that batch is applied.
* If the upstream is unreachable, the node keeps serving what it has and retries with backoff.

## Setting up a mirror or local node that follows an authority

On the **authority** (administrator):

1. **Admin → Tokens**: create a token with scopes **`read`** and **`sync`** for the follower.
2. Tell the follower's administrator the authority's address and its **node id** (Admin → Peers → This node) by a channel you trust (not only the same web page).

On the **follower** (set `ATLAS_ROLE=mirror` or `local`, and `ATLAS_SECRET_KEY` so the token can be stored encrypted):

1. **Admin → Peers → Add a peer:** address + token → **Look up node**.
2. Atlas shows the node's id. **Compare it with what you were told.** Then **Trust this node and replicate from it**.
3. **Sync now** to pull immediately; afterwards it follows every `ATLAS_SYNC_INTERVAL_SECONDS` (60 by default). The peer list shows position, last success and any error.

You can also configure a single upstream with environment variables (`ATLAS_UPSTREAM_URL`, `ATLAS_UPSTREAM_TOKEN`, `ATLAS_TRUSTED_KEYS`; see [Environment variables](reference-env.md)); use whichever you prefer, and you can follow several peers from the UI.

## Local changes on a follower

* A follower cannot merge, split or unpin records the upstream owns (those decisions belong to the authority). Pins a follower sets are **local-only**, never override upstream ones, and an upstream pin never overwrites a local override. Edits proposed on a follower are local overlays and are never sent upstream.
* A record the follower created from a provider **folds into** the authoritative record when an upstream observation claims the same identifiers (authority wins; no duplicates).
* Local nodes can add their own media types and fields; those stay local and win over an upstream definition with the same name.

## Operating it

* **Position/lag:** Admin → Peers shows each peer's position; `GET /api/v2/status` (admin) shows the authority's feed head.
* **Add a mirror:** nothing to do on the authority except issue a token.
* **Remove a peer:** *Remove*; data already received stays.
* **Disaster recovery:** a node's identity (signing key) lives in its **database**; restoring the database restores the identity and followers keep working. **Promoting a mirror to authority is not supported yet**, because a mirror has a different identity; plan recovery around database backups ([Deployment](deployment.md#backups)).
* **Several workers:** every worker process runs the sync loop; replays are idempotent, so extra pulls are wasteful but harmless.
* **One writer at a time:** writes are serialised by a database lock. This is fine for metadata; it is the first thing to revisit under very heavy ingestion.

## Sharing sign-ins between nodes

Separate from replication: [Sign-in sharing](sso.md).
