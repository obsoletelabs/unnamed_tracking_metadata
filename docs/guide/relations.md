# Relationships and editions

Atlas records how works relate: a DLC and the game it extends, a port or remake and its original, a mod and
its base game, the games in a series, the contents of a bundle. Since federation protocol **1.4** these
relations replicate like the rest of a record (contract C6, issue #86).

## Works, not editions

A relation joins two **records** (works). Editions are not separate records and are never a relation target:

* a record's **editions** (`editions` in the API) come from the `edition` its sources name, for example
  "Game of the Year Edition", each with the store ids of *that* edition (`provider_ids`) and the sources that
  named it;
* dated releases of an edition are rows of the **Release dates** table (`releases[].edition`).

So "Deluxe Edition" is an edition of its game, while a DLC, a remaster or a port is its own record with a
relation to the original.

## Relation types

A relation points **from** the record that depends **to** its parent, base work, series or collection.

| Type | Meaning | Parents per record |
| --- | --- | --- |
| `dlc_of` | downloadable content for the parent | one *add-on* parent (DLC, expansion and standalone expansion share this limit) |
| `expansion_of` | an expansion that needs the parent | one add-on parent |
| `standalone_expansion_of` | an expansion that runs on its own | one add-on parent |
| `port_of` | the same game on another platform | one |
| `remake_of` | rebuilt from scratch | one |
| `remaster_of` | the original, improved | one |
| `version_of` | another version of the same game | one |
| `mod_of` | a mod of the base game | one |
| `modpack_of` | a collection of mods for the base game | one |
| `total_conversion_of` | a mod that replaces the base game's content | one |
| `series_member` | part of a series; points at a record of type **Series** | any number |
| `collection_member` | included in a collection or bundle; points at a record of type **Collection** | any number |

The host's `same_game` is a merge in Atlas, not a relation; Atlas never sends `custom` or `related_to`.
The **Series** and **Collection** media types exist on every node for these targets. A bundle is a
Collection record: each game in it is a `collection_member` of the bundle.

Atlas refuses, with a reason and without changing anything:

* a relation from a record to itself (also after following merge redirects);
* a second parent of the same kind (remove the old one first, or replace both at once with the API);
* a **cycle**: a relation whose parent already leads back to the record through DLC, port, remake, mod
  ... relations (or, for memberships, through series and collection memberships). The check walks at most 32
  steps and 10 000 records; a graph too large to check is refused;
* a membership that does not point at a Series or Collection record, or a DLC, port... relation that does;
* more than 200 relations *from* one record. A record may have any number of relations *to* it (a large
  bundle); pages and the API show the first 200 of each kind, with the full counts.

## On the site

A record page shows a **Related** section: what the record belongs to (for example "DLC for", "Part of the
series"), what belongs to it ("DLC", "Ports", "Contents", "Games in this series") and its editions.

Reviewers open **Edit relations** (on the record page, and under **Curation**). The editor lists the record's
own relations with **Remove** buttons, and adds one by title search: choose what this record is, search for
the other record, and press **Add** next to it. Refused additions are explained on the page. Relations from an
upstream authority are marked and can only be changed there. Ended relations, with why they ended, are listed
at the bottom.

## In the API

* `GET /api/v2/games/{id}` adds `relations`: `parents`, `children`, `members_of` and `members`, each a list of
  `{id, type, title, media_type}` ordered by type then id and holding at most 200, plus `counts` (full sizes)
  and `truncated`. The record's `editions` are in the record when known. Both keys are additive; old clients
  ignore them.
* `GET /api/v2/games/{id}/relations?depth=2` returns the graph around a record (`nodes`, `edges`), up to 4
  hops in both directions and 200 records, in a fixed order, with `truncated` when there is more. `edges` are
  all active relations among the returned records. The walk reads the database in pages and stops at the
  first record past the limit, so a bundle with thousands of members costs no more than a small one. Use it to
  navigate from a DLC to its game and on to the game's other DLC, ports and series.
* `PUT /api/v2/games/{id}/relations` (curate scope) replaces the record's own relations:
  `{"relations": [{"to": "atlas:…", "type": "dlc_of"}]}`. Missing ones are ended, new ones added, all or
  nothing. On a mirror or local node the relations are local to that node (an *overlay*), allowed on any
  record, including one the upstream manages. Overlays are never published, and the upstream's relations always
  win over them: when a relation from the upstream conflicts with an overlay (a second parent, a cycle), the
  overlay is ended with reason `superseded`; an overlay identical to an upstream relation is taken over by it.

Ids follow merge redirects everywhere.

## Merges and splits

Merging a record moves its relations to the record it was merged into. A relation that would then point at
itself, duplicate one the survivor has, give the survivor a second parent of the same kind, form a cycle, or
make a membership point at a record that is not a series or collection is **ended** instead; the survivor's
own relations always win. An active relation therefore never names a merged record.

Every node makes the same decision for the same merge, so followers stay identical without extra feed
entries. That holds because the decision for a *replicated* relation (one the authority published, or one a
follower applied from its feed) looks only at other replicated relations. Relations that exist on one node
only (a mirror's or local node's overlays) are decided after
them, and one that then conflicts is ended (`superseded`).

Splitting a source record off (**Split off**) creates a new record without relations; the relations stay
with the original record, which keeps its id.

## Federation

Each change is one signed `relation` entry: `{"op": "add", "from", "to", "type", "source"}` or
`{"op": "end", "from", "to", "type"}`. Followers apply them in feed order and follow merge redirects.

**Series position (C15 §2.4).** A `series_member` relation may carry `position` (1-16 printable characters, such
as `"1"`, `"2.5"` or `"0"`): the member's place in its series, shown in `members`/`members_of` and in the graph
edges. Only `series_member` takes one; an `add` of another type with a position is refused here and quarantined
`invalid` on a follower, and an `end` never carries one. Changing a position ends the relation and adds it again
with the new position (no new operation), so the history shows both. A relation without a position is signed
exactly as before. `position` is registered under the one release version, like the rest of the book model
([Books](books.md)).

* An authority signs every relation it stores (wire 1). `PUT /games/{id}/relations` answers `published: true`
  when every relation the record holds on its own is in the signed feed; relations an earlier release kept local
  (below its emit protocol 1.4) are signed when the record's relations are put again, once each
  (`changed.published` counts them), and until then the answer is `published: false` with `reason: "pending"` and
  `pending` (how many still wait). A mirror or local node answers `published: false`, `reason: "local_node"`. A
  relation that was never signed and is removed is ended on the authority only; no `end` is signed for it, since
  followers never had it.
* A follower of 1.1, 1.2 or 1.3 **parks** the first `relation` entry (`unsupported_kind`) and holds back what
  follows, then applies everything in order after it is upgraded (see
  [Compatibility and upgrades](federation.md#compatibility-and-upgrades)).
* A 1.0 follower cannot park and passes over relations. After upgrading it, `atlas sync replay --rescan`
  records them (`dropped_by_previous_release`) and `atlas sync replay --historical` applies them.
* A replicated relation is checked against the relations this node holds from the feed, as the authority
  checked it. One that breaks a rule there (a cycle, a second parent, a self relation) is quarantined
  (`invalid`), not parked: it does not block later entries and is listed under Admin → Peers. This node's own
  overlays never cause that: an overlay that conflicts is ended (`superseded`) instead. A feed `end` ends only
  a relation that came from the feed. One that names a
  record this node does not have yet is quarantined and retried.

## Upgrading and rolling back

Migration 0032 creates the `relation` table; no existing data changes. To show editions on records that
existed before this release, run `atlas dates rebuild --restart` once (it re-derives every record's view,
batch by batch, and resumes if interrupted); records also pick them up the next time a source updates them.

A database downgrade keeps the relations aside (`relation_0032_kept`) and an upgrade brings them back. Records
may have been merged while the older release ran (by this node, or by its upstream), and that release does not
move relations. So after the schema step, `atlas migrate` (and every start, until it is done) replays those
merges with the same rules a 1.4 follower applied when it received them, in the same order (this node's own
feed, then its upstreams' retained feeds), and advances the versions of the records whose relations changed.
The authority and its followers therefore hold the same relations afterwards, with no extra feed entries.
