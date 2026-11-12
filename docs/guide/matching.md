# Matching and provenance

Atlas joins what many sources say about one work into one record. This page explains how it decides that two
source records are the same work, how it picks each field's value, how you can see why, and how a curator undoes a
wrong merge. It describes contract C11 as shipped in this release; the parts that need a newer federation
protocol are marked.

## How an observation finds its record

Every observation that arrives (from a provider import, a client, a plugin or the upstream feed) goes through one
matcher (`m2`). The same matcher answers `POST /api/v2/resolve`, so a client asking "which record is this?" and the
server ingesting it can never disagree.

The matcher looks at the evidence in this order:

1. **Identity evidence.** An Atlas id the source holds, or a provider id (Steam app id, GOG id, IGDB id...) already in
   the crosswalk. One record named this way is an `id` match. Two different records named by one observation are a
   **conflict**: nothing is merged, both records stay and an `id_conflict` review item is opened. This release never
   joins two records automatically because one observation names both (no automatic "bridge"; a later release may
   add it behind a setting). When the observation's own key names the record but another id it sends contradicts
   that record, the contradicting id is never registered; with no second record to pair, no review item is opened:
   the decision's evidence names the id (`namespace_conflict`) and the record is listed under
   `GET /api/v2/conflicts?field=provider_ids`.
2. **Title evidence.** The normalized title, aliases and a capped fuzzy score, adjusted by the release year
   (same, adjacent, far apart), the platform, the developer (company suffixes such as "Inc" or "Corporation" are
   ignored) and the publisher.

Some evidence vetoes a title match outright:

| Veto | Example |
| --- | --- |
| Sequel guard | "Portal" never matches "Portal 2": the numbers differ |
| Placeholder title | "Steam App 12345" is never title evidence |
| Contradicting id | the record already holds another id of the same store (and the same platform, for PlayStation, Xbox and Nintendo stores) |
| Unknown Atlas id | the source names an Atlas record this node does not have |
| "Different works" | a curator said these two records are different, or split this source record off that one |
| Margin | two records score almost the same: the matcher does not guess |

A title match is assigned only when it is strong enough and clearly ahead; a weaker one opens a `possible_duplicate`
review item instead.

### Decisions are stored

Each matched observation leaves a **match decision**: the tier (`id`, `strong`, `weak`, `none`), the reason
(`crosswalk`, `exact`, `fuzzy`, `ambiguous`, `contradicted`, `rejected`, `placeholder`...), the score, the margin to
the runner-up and every piece of evidence with its effect. Curators read them with
`GET /api/v2/games/{id}/decisions` and `GET /api/v2/observations/{id}/decision`. Decisions are kept for
`ATLAS_MATCH_DECISION_RETAIN_DAYS` (180 by default); the newest decision of each source record, and every decision a
review item or a live merge refers to, are always kept. `atlas match prune` applies the retention now (the server does
it daily, up to 200,000 rows per run in batches of 1,000).

### Re-sending the same data

An observation whose payload is byte-for-byte what the source record already says is **unchanged**: no new row, no
rebuild, no version bump, no feed entry. Clients that want to see this pass `?report_unchanged=1` to
`POST /api/v2/observations`; without it the answer says `updated` as before. A batch rebuilds each record it touched
once, at its end.

## How a field gets its value

Each field is chosen by rank, compared in this order:

1. **Trust class**: `curator` (curator and manual entries) > `publisher` (reserved) > `community` > `provider`.
   A replicated observation can never rank above its origin. The upstream authorities you configured (every node
   in `ATLAS_TRUSTED_KEYS` and every node under **Admin → Peers**: each is a feed whose merges and pinned fields this
   node applies) may carry curator data; any other origin counts as `provider`. `ATLAS_SYNC_ORIGIN_CLASS_CAP` sets a
   different ceiling per node.
2. **Field preference** (`ATLAS_FIELD_SOURCE_PREFERENCE`, for example `{"description": ["igdb", "steam"]}`). It only
   reorders sources of the `provider` class: a preference never lifts a provider over community or curator data.
3. **Source trust** (`ATLAS_SOURCE_TRUST`).
4. **Freshness**: the source's own update time, else when Atlas saw it.
5. A fixed tie-break, so the result never depends on the order observations arrived in.

List fields (genres, tags, platforms...) are the union of every source, best source first. A **pinned field** beats
everything. A curator can also **clear** a field (`PUT /api/v2/games/{id}/locks/{field}` with `{"clear": true}`):
it stays empty whatever the sources say. In this release a clear is kept on the node that made it; federating it
needs protocol 1.7.

!!! note "Changing the trust settings"
    When you change trust classes or preferences, run `atlas match explain-backfill` to recompute every record. It
    works in batches, can be interrupted and resumed, and only bumps the version of records whose data actually
    changed.

## Why does this field have this value?

`GET /api/v2/games/{id}/provenance` answers per field: the value, the winning source and its trust class, the reason
it won (`class`, `preference`, `trust`, `freshness`, `only_value`, `lock`...), the runners-up and whether sources of
the winning class disagree (`conflict`). With the `curate` scope, `?detail=values` adds every candidate value grouped
by value. Host and plugin observations (one per installation) are counted, never listed one by one, without the
`curate` scope.

`GET /api/v2/conflicts?field=description` lists the records whose sources disagree on a field, for a curator to work
through. The record's `explain` (`GET /api/v2/games/{id}/explain`) carries the same information in its stored form,
with `_meta` naming the explain version, the matcher and a digest of this node's trust settings (two nodes with
different settings can see why their views differ).

## The review queue

Review items now carry the matcher's `reason`, its `evidence`, the `decision_id` and a `priority`
(`GET /api/v2/review?order=priority`): identity conflicts first, then likely duplicates by score. There is at most
one open item per pair of records. A curator resolves an item by

* **merging** it (`POST /api/v2/review/{id}/merge`),
* marking the records as **different works** (`POST /api/v2/review/{id}/distinct`): they are never merged or paired
  again,
* **splitting** one wrongly attached source record off (`POST /api/v2/review/{id}/detach`), or
* **dismissing** it: the same pair is not reopened for `ATLAS_REVIEW_DISMISS_DAYS` (30 by default).

A merge closes the items it makes pointless and re-points the others at the surviving record; an unmerge gives each
of them back as it was, unless a curator decided it in between.

An **`unmerge_check`** item is different: it names the surviving record and the record an unmerge split off it, and
lists the rows that stayed because they were edited after the merge. It is never merged (that would redo the merge
just undone; the API answers `409 conflict` and the web queue has no merge button). Check the rows, detach what
belongs to the other record (`POST /api/v2/review/{id}/detach`, or on the game page), then dismiss it. Moving a
whole source record or a single row between the two records from the item itself is not in this release.

## Undoing a merge

Every merge leaves a **merge record**: what moved (source records, keys, pinned fields, artwork and votes,
achievements, custom values, list entries, follows, prices, relations, review items) and what the merged record looked
like. `POST /api/v2/games/{id}/merge?dry_run=1` previews a merge and changes nothing.

`POST /api/v2/merges/{merge_id}/unmerge` brings the merged record back with everything it brought. Rows a curator
edited after the merge are reported and left as they are (a `unmerge_check` review item lists them). Merges are undone
last in, first out. A source record that arrived after the merge goes back only when all its other identity evidence
points at the restored record. Pass the survivor's ETag as `If-Match` to be sure nothing changed in between;
`{"dry_run": true}` previews. By default the two records are then remembered as different works
(`remember_distinct`), so matching never merges them again; `POST /api/v2/games/{id}/merge` with
`"override_distinct": true` merges them anyway and ends that decision.

What cannot be undone in this release:

* a merge this **authority published** in its signed feed (it is on every mirror): undoing it needs the reserved
  `unmerge` feed kind, which this release does not sign (`409 reserved_feature`);
* a merge a **mirror applied from its upstream** (`409 upstream_owned`): only the upstream can undo it;
* a merge made before this release (`409 legacy_merge`): split its source records off instead.

## Federation

Nothing in this page changes what an authority signs in this release. The constructs a later release will publish
are reserved for protocol 1.7 (the feed kinds `unmerge` and `distinct`, lock body version 2 for clears and the merge
guard); followers of this and earlier releases park such entries instead of dropping them. See
[Federation](federation.md#compatibility-and-upgrades).
