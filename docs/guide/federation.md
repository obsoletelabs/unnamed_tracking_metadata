# Federation

Atlas nodes can follow each other. There is **no central server in the code**: any node can be an authority for its own community, and others can follow it.

## Roles

| Role | Writes | Replicates from | Notes |
| --- | --- | --- | --- |
| `authority` | accepts contributions and curation; publishes the signed change feed | nobody | PostgreSQL in production |
| `mirror` | provider cache and local-only overlays | an authority | can't rewrite records the upstream owns |
| `local` | everything on its own data | optionally an authority | SQLite; private overlays win locally |

## Telling nodes apart

Every node shows a **kind** badge next to its name, in `/api/v2/site`, and on the peer page when you add it. Set it in
**Admin → Settings → Node** (with an optional *Operator* name shown in the footer):

| Kind | Meaning |
| --- | --- |
| **official** | Run by the project or by an operator the project lists. Treat it as the reference copy. |
| **community** | An authority run by someone else. Fine to follow if you trust them. This is what an authority shows by default. |
| **mirror** | A read-only copy of another node, usually run to spread load or give regional access. |
| **local** | A private node: a household or one person, caching providers and following an upstream. Never meant to be followed. |

!!! warning "The badge is a claim, not proof"
    Anyone can type "official". What makes a node official is that **you pin its key** from a list you trust: the node id
    printed by `atlas node` and shown under *Peers* before you confirm. Publish your official list (names, URLs, node ids) in one
    place, and have people copy ids from there, never from the node itself.

### Running several official nodes

The simplest and safest layout for 3–4 official nodes is **one authority and the rest mirrors**:

1. Run the authority (see [Authoritative server](hosting/authority.md)). It alone accepts curation and publishes the signed feed.
2. Run each mirror with `ATLAS_ROLE=mirror`, `ATLAS_UPSTREAM_URL` pointing at the authority (or at another mirror, to make a tree)
   and the authority's key pinned under **Admin → Peers**. Mirrors serve reads, so put them in different places or behind a load
   balancer. Set their kind to *official* if you operate them.
3. Point apps at the nearest official node. If the authority is down, mirrors keep serving everything they have; curation resumes
   when it returns.
4. Back up the authority (see [Backup and restore](backup.md)) and publish its **public key** so a replacement can be recognised.

Several independent authorities that all accept edits are possible too (each follower can pin more than one), but then two
copies of the same game can be curated differently; prefer one authority plus mirrors unless you have a reason.

Local nodes are different: they follow one of the above, add their own overlays on top (never sent back), and are for one person
or household. See [Local node](hosting/local.md).

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
* Every node keeps a **local client change log** for its own API clients (`GET /api/v2/changes?feed=local`, see [Incremental client updates](tracking-app.md#incremental-client-updates)). It records which records changed and why, including provider imports that never enter the signed feed. It is unsigned and holds ids and versions only. It is never served by `/sync/changes`, never relayed and never copied into a recovery candidate, so nothing in it reaches a follower.

## Operating it

* **Position/lag:** Admin → Peers shows each peer's position; `GET /api/v2/status` (admin) shows the authority's feed head.
* **Entries that fail to apply:** a mirror never drops one silently. An entry this release does not understand is *parked* and holds later entries back until an upgrade (see [Compatibility and upgrades](#compatibility-and-upgrades)). If a known feed entry cannot be applied it is rolled back, recorded as *quarantined*, and the rest of the page still applies. Each pull retries the recorded entries from the signed copy the mirror retains (up to 20 automatic attempts each; retries are idempotent, so they converge without duplicates). A retry never overwrites newer state: when a later entry of the same feed has already replaced what the failed entry would set (a source's artwork or achievement list, a lock, a relation that a later entry added or ended, the schema), the failed entry is resolved as superseded (`superseded_by` in its detail) without being applied, and a failed custom-values entry applies only the keys no later entry set. A failed merge is applied and then the later entries that already applied to either record (locks, artwork, achievements, custom values, relations) are re-applied in feed order (`replayed` in its detail); if a later merge of the same records already applied, the merge stays quarantined with reason `out_of_order` for an administrator, because the order of two merges decides what survives. `GET /api/v2/status` shows the open count as `sync_failures`, `GET /api/v2/sync/failures` lists them with the error and attempt count, and `POST /api/v2/sync/failures/retry` retries them immediately, including ones past the automatic limit.
* **Add a mirror:** nothing to do on the authority except issue a token.
* **Remove a peer:** *Remove*; data already received stays.
* **Disaster recovery:** a current verified database backup preserves identity. If that is unavailable, a mirror with complete retained signatures can build an [offline recovery candidate](recovery.md) under a fresh root; followers explicitly retire the old origin and pin the new one.
* **Several workers:** every worker process runs the sync loop; replays are idempotent, so extra pulls are wasteful but harmless.
* **One writer at a time:** writes are serialised by a database lock. This is fine for metadata; it is the first thing to revisit under very heavy ingestion.

## Compatibility and upgrades

Nodes of different releases can follow each other. Each release speaks a federation **protocol**
(`MAJOR.MINOR`); this release speaks **1.5**. Releases before protocol negotiation did not
advertise one and count as **1.0**.

| Protocol | Adds | A 1.0 follower | A 1.1, 1.2 or 1.3 follower |
| --- | --- | --- | --- |
| 1.1 | negotiation, parking, named heads, `items[].key` on achievements (reserved-optional) | applies what it knows; drops what it does not | — |
| 1.2 | the achievement tier `platinum` (an entry with it also lists the `_requires` token `tier-platinum`) | **drops the whole achievement list** of that game; after upgrading, `atlas sync replay --rescan` records it as `dropped_by_previous_release` and `--historical` applies it | a 1.1 follower **parks** it (`unsupported_capability`) and applies it after upgrading |
| **1.3** | partial and regional release dates: `ext.release` on observations, a `release_date` lock that is a year or a month, and the `releases` lock field (each signed with the `_requires` token `dates-v1`, also advertised in `capabilities`) | **drops the observation or lock**; after upgrading, `--rescan` records both and `--historical` applies them | **parks** it (`unsupported_capability`) and applies it after upgrading; the record keeps its previous dates meanwhile, never a stripped or day-only copy |
| **1.4** | the feed kind `relation`: relationships between works (DLC, expansions, ports, remakes, mods, series and collection membership; see [Relationships](relations.md)) | **drops the entry** (an unknown kind); after upgrading, `atlas sync replay --rescan` records it as `dropped_by_previous_release` and `--historical` applies it | **parks** it (`unsupported_kind`) and applies it after upgrading; the records keep their other data meanwhile |
| **1.5** | the feed kind `record`: a game introduced by identity only (id, type, display title, store ids), signed before any artwork, achievements, custom values, locks, merges or relations that name it, so no follower receives changes for a game it has never heard of (see [Changes waiting for their game](#changes-waiting-for-their-game)). Below 1.5 an authority can instead publish an ordinary observation of the source `atlas-record` (the bridge, off by default) | **drops the entry** and the changes behind it; keep the emit protocol at 1.1 or lower while a 1.0 follower remains, or turn the bridge on. After upgrading, `--rescan` recovers both | **parks** it (`unsupported_kind`), which holds that follower's applied head until it upgrades; with the bridge on, the authority sends the bridge observation instead while `min_follower_protocol` is below 1.5 |
| 1.6 (reserved) | the price channel: a separate signed channel `/sync/price-changes` (capability `price-channel-v1`); see [Prices](prices.md). **Designed, not enabled in this release**: no capability, the endpoint answers 404, nothing is signed | never sees it (separate endpoint, not the feed) | never sees it; a `price` entry in the main feed would be **parked** (`unsupported_kind`) |
| 1.7 (reserved) | undoing merges across the federation: the feed kinds `unmerge` and `distinct` ("these are different works"), and lock body version 2 for an explicit clear (`field-clear-v1`) and the merge guard (`merge-guard-v1`); see [Matching and provenance](matching.md). **Not signed by this release**: unmerges, distinct marks and clears stay on the node that made them; a merge is published as before, so an authority cannot undo a published merge until it emits 1.7 | would **drop the entry** (an unknown kind) | **parks** it (`unsupported_kind` or `unsupported_version`) and applies it after upgrading |
| 1.8, 1.9 (reserved) | achievement body version 2 (`ach-v2`), artwork body version 2 (`art-v2`, artwork kind `trailer`) | — | **parks** it |

### What a follower does with entries it does not understand

A follower never silently skips a signed entry. It sorts every verified entry into one of:

| Outcome | When | Blocks later entries? |
| --- | --- | --- |
| applied | the kind, body version, required capabilities, every field and every vocabulary value are known | no |
| **parked** | an unknown kind (`unsupported_kind`), a newer or malformed body version (`unsupported_version`), an unknown or malformed `_requires` (`unsupported_capability`), an unknown field or `ext` shape (`unsupported_field`) or an unknown closed value, or one that is not text, such as an achievement tier (`unsupported_value`) | **yes**: nothing after it is applied until you upgrade |
| **quarantined** | a known entry that fails validation (`invalid`) or fails to apply (`apply_error`) | no; it is retried automatically |
| no-op | an idempotent replay (an observation already present, a merge already done) | no |

Parking blocks because entries are causally ordered (a merge can depend on something written
before it). While parked, the follower **keeps downloading, verifying and retaining** later entries,
so a mirror still relays a newer authority's feed byte for byte, up to
`ATLAS_SYNC_MAX_PARKED_BACKLOG` entries (50 000 by default). Above that it pauses downloading and
reports `backlog_full`. After you upgrade, the next pull (and a one-time replay at startup) applies
the parked entries in order from the applied head; `atlas sync replay` does the same on demand.
Entries recorded by `atlas sync replay --rescan` (below the applied head, see below) are different:
they apply only through `atlas sync replay --historical`.

### Heads

Admin → Peers, `GET /api/v2/status` (field `upstreams`) and `GET /api/v2/sync/status` (admin) show,
per upstream feed:

| Head | Meaning |
| --- | --- |
| `observed_head` | what the upstream claims (unsigned; never trusted on its own) |
| `downloaded_head` | highest entry fetched, also when a page failed to verify |
| `verified_head` | highest signed entry verified and retained; relayable |
| `applied_head` | what the local view reflects: every entry up to it was applied, a no-op or quarantined |
| `parked` | first parked entry, its reason and kind, how many entries are held back, `backlog_full` |
| `quarantined` | how many entries are quarantined and the oldest one |

`applied <= verified <= downloaded <= observed` always holds. `state` is `in_sync` only when the
upstream reported a head, every entry up to it is verified and applied, and nothing is parked,
quarantined, refused or rewound. A node never reports itself synchronized from a head it has not verified.

If an authority advertises fewer signed entries than this node already verified, or serves a
different entry under a sequence number this node retains, the pull fails with "authority feed fork
or rewind", nothing is applied, and the upstream's `state` becomes `rewound` (field `rewound`, metric
`atlas_sync_rewound`, `atlas sync status` exit 5). It stays `rewound` until the authority serves the
last entry this node verified again, byte for byte. Recovering from a real fork is not automated yet.

### Negotiation and refusal

`/api/v2/sync/info` advertises `protocol`, `min_follower_protocol`, `emit_protocol`,
`observation_schema`, `kinds` (per kind, the highest body version emitted and applied) and
`capabilities`, next to the unchanged `contract: 1`. Followers send an `Atlas-Protocol` header.
A follower refuses an upstream, before downloading anything and without moving its cursor, when
the upstream needs a newer follower (`min_follower_protocol`) or when the upstream is more than
one major version older (N-1 support). The reason is shown on the peer and in `sync/status`.

An authority records which protocol each follower token last sent (token id only) and shows it on
Admin → Peers. A follower that sends no header is a 1.0 release.

### Emitting newer features

`emit_protocol` (default: `ATLAS_MIN_FOLLOWER_PROTOCOL`, itself 1.0 by default) is the newest
protocol this node's own feed may use. Constructs that need more are not signed: a reserved kind is
refused (kept local), a platinum achievement tier is rejected with "Raise federation emit protocol
to 1.2 first", and an extended release date is stored locally but left out of the signed
observation. Raise it under **Admin → Peers → Federation protocol** only after every follower is
upgraded: it asks for your password and a confirmation, like a key rotation, and is audited.
Followers of releases before 1.1 cannot park and would silently drop newer constructs, so keep
`emit_protocol` at 1.1 or lower until none remain. Raising it to **1.2** lets curators, the editor,
proposals and bulk imports publish platinum achievements; until then those writes are refused with
HTTP 409 (`"code": "emit_protocol"` in the API) and nothing is stored or signed. Mirrors and local
nodes sign nothing, so they accept platinum whatever their own setting, and a mirror applies it from
an upstream that emits 1.2.

Raising it to **1.3** publishes partial and regional release dates. Below 1.3 an authority still stores
them from providers, the API and the editor, and shows them locally, but signs observations without
`ext.release` (followers see the day-only `release_date`, as before), and refuses a `release_date` lock
that is a year or a month, a `releases` lock and removing a `releases` lock with HTTP 409
(`"code": "emit_protocol"`), storing nothing. A day lock is unaffected. After raising it, run
`atlas dates rebuild` on followers that upgraded with existing data, or simply let new entries arrive:
migration 0031 already derived the first release from every record's existing date.

Raising the emit protocol does **not** re-publish what was signed before. An observation stored while
the authority emitted below 1.3 was signed without its `ext.release`, and that signed entry stays as it
is: followers keep only its day-only `release_date`, so their first release can differ from the
authority's until the same source sends the record again (a new import or sync signs a new observation,
now with `ext.release`) or a curator edits or locks the date. `atlas dates rebuild` re-derives views on
one node only and signs nothing. Raise the emit protocol before importing partial dates you want followers
to have.

A `release_date` lock written by a release before 1.3 could hold any text ("TBA"). A follower of this
release applies such a signed lock exactly as the authority stored it and shows it in `release_date` as
before; only new locks must be a date. A day keeps any year (a 1937 film keeps its date); a year or a month
must be between 1950 and 2100.

Raising it to **1.4** publishes relationships between works (`relation` entries). Below 1.4 an authority
stores relations made in the editor or with `PUT /api/v2/games/{id}/relations`, shows them locally and
appends nothing to its feed. A relation that was already published is not removed while the protocol is
below 1.4 (HTTP 409 `emit_protocol`): its followers would keep it. As with dates, raising the protocol does
**not** publish relations made before; re-save them (remove and add) to publish them.

### Upgrading a network

1. Upgrade followers (mirrors first, then local nodes). They keep following older authorities.
2. Upgrade authorities. They keep emitting the old protocol.
3. When Admin → Peers shows every follower on the new protocol, raise `emit_protocol`.

After upgrading a follower from a release before 1.1, run `atlas sync replay --rescan` once: it
records retained entries that the old release skipped without applying (unknown kinds, unknown
fields, invalid observations) as parked or quarantined, without moving the cursor back and without
applying anything. An observation this release accepts but the old one rejected is parked as
`dropped_by_previous_release`, and so are a `relation` entry, an achievement list (for example one with
a `platinum` tier) and a lock or unlock (for example a `releases` lock) whose effect is missing here. For
lists and locks only the newest entry per record and source (or per record and field) counts: an
older one that a later entry replaced is not recovered. One whose record the feed merged since is judged
on the survivor, where the authority's merge carried it (the survivor's own duplicates and own lock
win), and `--historical` lands it there. A list is compared on the fields it stores for each item
(identity, text, icons, kind, tier, points, percent, set, status, locale, icon licence), not on
translations or order. These rows sit below the applied head, so they no longer block, but the upstream stays
`parked` (and promotion is refused) until they are dealt with. Rescanning again after the replay
records nothing: the state then holds. `python -m tools.skew_drill --scenario rescan` runs this
recovery against the real 1.0 release.

Then run `atlas sync replay --historical` (try `--dry-run` first). It re-applies **every** retained
entry from the oldest recovered one up to the applied head, in feed order, so a skipped entry lands in
its causal position and the merges, detaches and artwork after it run again on top of it. Entries
already reflected are no-ops; an observation that a later entry superseded is stored as history, not
made current. Values set by those entries are applied again, so a local change since to the same
upstream-owned field is overwritten by the authority's value, as on any pull. A plain `atlas sync
replay`, and the replay at startup, never apply these rows: entries are never applied out of order.

Rolling the code back after an upgrade loses nothing that is retained, but it is not free: a release
before 1.1 cannot park. It treats parked rows as ordinary failed entries, retries them, passes over
the kinds it does not know, marks those rows resolved and moves its cursor past them, which is the
old silent skip. After upgrading again, run `atlas sync replay --rescan` (it re-classifies rows an
older release resolved) and then `atlas sync replay --historical`. Downgrading the database
(`alembic downgrade`) removes the parked/quarantined distinction; see the migration notes.

### Operator commands

```
atlas sync status [--node N] [--json]          # heads per upstream; exit 3 parked, 4 refused, 5 rewound
atlas sync replay [--node N] [--dry-run] [--limit K]   # apply parked entries in order until the next park
atlas sync replay --rescan [--node N]          # one-time recovery after upgrading from 1.0 (records only)
atlas sync replay --historical [--node N] [--dry-run]  # re-apply retained entries from the oldest recovered one
atlas sync skip --node N --seq S --reason TEXT # give up on one parked entry (quarantined, audited)
atlas sync reconcile --node N --dry-run        # compare this node's view with the upstream's /sync/digest
```

`replay` exits 0 when converged, 3 while entries are still parked (the reason is printed), 4 when
an upstream refused this node and 5 when an upstream rewound or forked its feed.

`reconcile` compares a digest (`atlas-view-v1`) of the observations the authority published and the
live record each sits on (so merges and detaches count). It does not cover artwork, achievements,
custom values, locks or the schema: a match means equal replicated observations, not an identical
node. The upstream caches its answer for up to 60 seconds (`cached_seconds`). Plain `atlas sync` still pulls from `ATLAS_UPSTREAM_URL`.

### Feed kinds in this release

| Kind | Since | Emits / applies body version | Fields |
| --- | --- | --- | --- |
| `obs` | 1.0 | 1 / 1 | `canonical_id`, `media_type`, `observation` |
| `artwork` | 1.0 | 1 / 1 | `canonical_id`, `items`, `source` |
| `achievements` | 1.0 | 1 / 1 | `canonical_id`, `items`, `source` |
| `custom` | 1.0 | 1 / 1 | `canonical_id`, `clear`, `source`, `values` |
| `schema` | 1.0 | 1 / 1 | `fields`, `types` |
| `merge` | 1.0 | 1 / 1 | `dst`, `src` |
| `detach` | 1.0 | 1 / 1 | `canonical_id`, `new_canonical_id`, `observation_id` |
| `lock` | 1.0 | 1 / 1 | `canonical_id`, `field`, `value` |
| `unlock` | 1.0 | 1 / 1 | `canonical_id`, `field` |
| `key_rotation` | 1.0 | 1 / 1 | `key_id`, `public_key` |
| `relation` | 1.4 | 1 / 1 | `from`, `op`, `source`, `to`, `type` |
| `record` | 1.5 | 1 / 1 | `because`, `canonical_id`, `media_type`, `provider_ids`, `title` |
| `price` | 1.6 (reserved) | not in this release | parked by this release |
| `unmerge` | 1.7 (reserved) | not in this release | parked by this release |
| `distinct` | 1.7 (reserved) | not in this release | parked by this release |

Each entry body may carry `_v` (body version; absent means 1) and `_requires` (capability or
vocabulary tokens a follower must know, at most 16 of up to 40 characters). Both are inside the
signed body, so they cannot be stripped; keys starting with `_` are reserved for this envelope.
Observation payloads may carry `ext` members: one listed in `_requires` must be understood, any
other is ignored by a follower that does not know it. The only field that a later minor may add
without parking 1.1 followers is `achievements.items[].key` (reserved-optional). Since migration 0022
an authority puts each row's stored key there (see [Achievements](achievements.md#stable-keys)), at
every emit protocol: 1.1 followers accept it as reserved-optional, and 1.0 followers read only the
fields they know, so they ignore it. A follower stores the authority's key, so mirror and authority
name every entry alike; an entry without a key (from an older authority) gets the derived key, which
is the same except for entries renamed in the authority's editor. Version-1 bodies are unchanged on
the wire, so existing signed feeds and signatures stay valid.

## Sharing sign-ins between nodes

Separate from replication: [Sign-in sharing](sso.md).

## Relays and several authorities

Mirrors retain verified original entries, independently of their applied catalogue. A relay
advertises available origins under `/api/v2/sync/info` → `feeds`; request an origin with
`/api/v2/sync/changes?node_id=<authority-id>`. Its body, sequence, key ID and signature remain
the original authority's. Followers pin that authority, not a replacement mirror signature.
The relay's own default feed remains separate. Replication needs `read` for info and `sync` for changes.

Admin → Peers → Look up node lists relayed origins. Select one and compare its fingerprint
through an independent channel. Add a separate peer for each origin, even when URLs match.
Environment configuration can select one pinned relayed origin; several matching origins
are refused as ambiguous. The relay operator may withhold updates, but cannot forge a
valid entry for an independently pinned origin.

Upgraded old mirrors backfill original history from sequence zero without re-applying old
entries to their current catalogue. Until backfill completes they cannot provide a complete
recovery source. Origin-scoped schema snapshots preserve other authorities' definitions;
local definitions win and the first upstream owner retains conflicting names. Coordinate
namespaces and curation policy across independent authorities: this is not leader election
or automatic conflict consensus.

For signing-key rotation, upgrade followers first and follow [Security](security.md#rotate-a-node-signing-key).
See [Release verification](release-verification.md) for the four-node HTTP acceptance drill.

### Feed and successor-proof wire contract

The existing default feed remains compatible until an operator explicitly rotates a signer.
`sync/info` keeps `node_id` and `public_key` as the stable root fingerprint/public key, and adds
`signing_key_id` (current signer), `rotations` (ordered proofs) and `feeds` (available relayed
origins, each with its own root, signer, proofs and retained `head`). Node IDs are the first
16 hexadecimal SHA-256 characters of the original raw Ed25519 public key.

Every entry carries `node_id` (original root), `key_id` (signer for that sequence), `seq`,
`kind`, `body`, `body_hash` and `signature`. The signature covers the existing domain-separated
message `atlas-feed-v2\n<node_id>\n<seq>\n<kind>\n<body_hash>`. The hash is SHA-256 of UTF-8 JSON
with sorted keys, no extra separators/spaces and Unicode preserved. Entries are independently
signed and required to be contiguous; this is not a previous-entry hash chain.

A `key_rotation` body is exactly `{"key_id": "<successor fingerprint>", "public_key": "<base64 raw key>"}`.
Its proof contains all entry fields above. The old key signs the transition entry itself;
the successor signs only later sequences. At most 128 ordered transitions are accepted, with
unique 32-byte successor keys and a final signer matching `signing_key_id`. Followers verify
from their independently pinned root and retain the full prefix to reject later rollback or
fork. The root is never replaced by an unverified key received from `sync/info`.
