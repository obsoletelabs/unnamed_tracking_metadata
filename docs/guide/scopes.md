# Scopes and sharing

Every item Atlas holds about a game (a field value, an image, an achievement list, a custom value, a relation)
has a **scope**: how far it may travel.

| Scope | Badge | Travels to |
|---|---|---|
| `public` | Official / Community / Mirror | Your public feed (on an authority) and everyone who follows it |
| `group` | Your nodes | Your own nodes when they ask. Never public, never sent upstream |
| `private` | Only you | One account (no writer yet) |
| `local` | This node only | Nowhere: it stays on this node |

Badges and the scope filters are shown only to accounts that **see data from your nodes** (see below).
Visitors never see a badge, a count or a filter.

## Keeping a record on this node

On a game's page, under **Curation → Where this record goes**, curators and administrators can:

- **Keep on this node only**: sets a `local` ceiling. From then on, nothing about the record is signed into a
  feed or shared with your nodes. Edits still work; they stay here.
- **Publish it**: lifts the ceiling. On an authority, the record is then introduced on your feed.

A ceiling cannot take back what was already published. A record your feed has already introduced cannot be kept
back, because followers keep what they received.

The same is available to API clients with the `curate` scope:

```http
PUT /api/v2/games/{id}/scope
{"ceiling": "local"}      # keep on this node only
{"ceiling": null}         # publish
```

## Who sees what

Scope says how far an item travels. Visibility says who may see it on this node. Atlas applies one rule inside
every read query, so search, browsing, totals, suggestions, relations, lists, media and the API agree:

| Item | Visitors, followers' tokens, accounts without the right | Accounts that see data from your nodes |
|---|---|---|
| Host/plugin library data (Playnite and similar) | Never. A record known only through it is not found | Yes |
| This node's own provider data, artwork and curator values | Yes, as before | Yes |
| Data received from your other nodes | Only while **Show data from your nodes to visitors** is on, and only the allowed categories, with no badge or node name | Yes, with badge |
| Public feed data | Yes | Yes |

A record a viewer may not see answers exactly like an unknown record (`404`, same text). The same rule applies
to its relations, lists, achievements, the proposal and edit pages, and the local change feed
(`GET /api/v2/changes?feed=local` leaves such records out; the cursor still moves past their entries).

A merge or relation between a record kept back and a public record is refused (`409`, code `scope_ceiling`):
publish the record first.

### Who sees data from your nodes (`scope.detail`)

- Administrators and curators, and anyone with review rights through your node group.
- Every account while registration is **closed**.
- A session opened through your node group only when that session has review or administrator rights here
  (after the `group_federated_privilege` limit, see [node groups](node-groups.md)) or the account was granted the
  right below. Signing in through the group does not give it by itself.
- Accounts a **local** administrator ticks **sees data from your nodes** for on **Admin → Users**. It is a
  standing right, so an administrator who holds that role only through a node-group grant cannot change it.

An account that registered itself on an open node does not see it unless you grant it. API tokens an
administrator mints for this node's own apps (the Playnite plugin, the Tracking App) act for the operator and see
everything. A token that holds `sync` without `contribute`, `curate` or `admin` (a follower's) sees what a visitor
sees. A personal token sees what its account sees.

## Admin → Sharing

Local administrators only. The page shows:

- **Where each scope goes**: what each scope accepts, contributes upstream, shares with your nodes and signs into
  the public feed. The upstream column is greyed: contributing to a main Atlas is not available yet. A node that
  is not in a group and not linked to a main Atlas says so: *Not in a group · Not linked to a main Atlas.
  Everything stays here.*
- **Records kept back**: how many records have a ceiling.
- **What visitors see**: the **Show data from your nodes to visitors** switch (off by default) and the field
  categories visitors may see while it is on. Public by default: title and other names, release dates,
  platforms, genres/themes/tags/features, developer and publisher, series and relations, store and database ids,
  cover art. Withheld by default (signed-in accounts that see data from your nodes only): descriptions, scores,
  other details, screenshots, achievements, prices.

Changing the categories recalculates what visitors see in the background. While that runs, the page says so.
`atlas scope status` shows progress and `atlas scope reproject` runs it at once.

## Upgrading

Migration `0038` adds the ceiling columns and starts that background recalculation. Until it has reached a
record, that record is shown as the previous release showed it. The public feed does not wait: a record is
recalculated on the spot before it is signed, so `atlas feed introduce` never signs a value from your libraries
or your nodes and never introduces a record that exists only through them. A downgrade is refused while any record has a
ceiling. With `ATLAS_DOWNGRADE_FORCE=1` the ceilings are kept aside and restored by the next upgrade.
