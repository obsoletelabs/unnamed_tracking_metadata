# Node roles, kinds and scopes

This page replaces an earlier draft that described an `AUTHORITY_MODE` setting (`local`, `central`, `hybrid`), an
`authority_mappings` table and `/api/v1/authority/*` endpoints. None of those exist. Atlas is configured
with the settings below. The operator guide covers them in more depth: see [Concepts](guide/concepts.md),
[Federation](guide/federation.md), [Node groups](guide/node-groups.md) and [Scopes and sharing](guide/scopes.md).

## Role: what the node does (`ATLAS_ROLE`)

| Role | What it does |
|---|---|
| `authority` | Keeps its own catalogue and **signs** a public change feed that others can follow. Records are introduced on that feed (C13). |
| `mirror` | Follows one or more authorities' feeds and serves a read-only copy. Its own edits stay on it. |
| `local` | A private catalogue for one person or household. It can follow authorities, but signs nothing for anyone else. |

All three roles run the same software and keep a complete catalogue. Canonical ids (`atlas:<ulid>`) are local and
stay stable. Provider ids (`steam:730`, `igdb:…`) are evidence attached to them. A record that is also known
elsewhere is linked by replicated observations and merges, not by a mapping table.

## Kind: how the node presents itself (`node_kind`)

`auto` follows the role (an authority shows as `community`). It can be set to `official`, `community`, `mirror` or
`local`. The kind is a label shown to visitors and peers. "Official" means something only to people who pinned that
node's key.

## Node groups

Nodes that belong to the same owner can form a **node group** (C10). Members share sign-in, profiles and, when
asked, data. See [Node groups](guide/node-groups.md).

## Scopes: how far an item may travel (C14)

Every item on a record has one **scope**: `public`, `group` (your nodes), `private` (one account) or `local` (this
node only). A record can also carry a **ceiling**: `local` ("Keep on this node only") or `group`. Nothing about a
ceiling'd record is ever signed into a public feed. Scope (how far an item travels) is separate from visibility
(who may see it on this node):

- Host and plugin library data is never shown to visitors, followers' tokens, or accounts without
  the "sees data from your nodes" right (`scope.detail`).
- Data received from your other nodes is shown to visitors only while **Show data from your nodes to visitors**
  is on, and then only in the field categories you allow.

See [Scopes and sharing](guide/scopes.md).

## API

- `GET /api/v2/games/{id}` and the other read routes apply the visibility rule. A record a caller may not see is
  a plain `404`. Callers with `scope.detail` (and the node's own app tokens) get an additional `scope` object.
- `PUT /api/v2/games/{id}/scope` (`curate`) sets `{"ceiling": "local"}` or lifts the ceiling with
  `{"ceiling": null}`. A record already introduced on the feed cannot be kept back (`409`).
