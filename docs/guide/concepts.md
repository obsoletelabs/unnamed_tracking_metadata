# Concepts

## Observations and canonical records

Atlas never edits a record directly. Everything it learns is an **observation**: one source's claim about one thing ("Steam says app 620 is *Portal 2*, released 2011-04-19, by Valve"). Observations are kept, not overwritten; a newer one from the same source supersedes the older for merging, and the old one stays as history.

A **canonical record** (`atlas:01m5…`) is *derived* from the current observations of everything that was matched to it. Because it is derived, it can always be rebuilt, explained, split or merged again.

## How fields are merged

Each field has a rule, and Atlas records which observation won (see **Where this came from** on every game page and `GET /games/{id}/explain`):

| Field | Rule |
| --- | --- |
| title, description, developer, publisher, series | highest **trust** wins; ties go to the newest |
| release date | trust-weighted vote across sources |
| platforms, genres/tags, features | union (case-insensitive, up to 100) |
| identifiers | each source's own id plus the ids it cross-references; the most trusted claim wins a namespace |
| custom fields | the most trusted source's value |
| anything | a curator can **pin** a value; a pin beats everything |

Trust is a number per source (`curator` 100, `manual` 90, `community` 85, `igdb` 70, `steam` 65, `gog` 60, `rawg` 50, others 40). Names that carry weight are **reserved**: only a token with the `curate` scope (or the node itself) may submit under them, so nobody can impersonate a trusted source.

## Matching: which record does a claim belong to?

1. **Identifiers.** If the source's own id, or an id it cross-references, is already known, that settles it.
2. **Title evidence.** Same normalised title (accents, ™, editions and numerals ignored; `Mega Man X` ≠ `Mega Man 10`) and same release year scores high enough to merge automatically. A missing year, a different year, or only a fuzzy title does **not**.
3. **Anything uncertain** creates a separate record **and** opens a *possible duplicate* for a person to decide. Atlas prefers a duplicate someone can merge over a wrong merge nobody notices.

Merging leaves a **redirect**, so ids held by other programs keep working. A wrong merge can be **split** (detach a source record).

## What is *not* stored

Playtime, ownership, wishlist state, favourites and similar personal facts are stripped from submissions on arrival. Accounts, lists and follows exist on the website for people who choose to use them, and can be exported or deleted (see [Using the site](using-the-site.md)).

## Media types and custom fields

A record has a **media type** (`game` is built in). An admin can add types (movie, anime…) and extra **typed fields** per type, and can relabel core fields per type (a movie's *Developer* reads *Director*). They appear in forms, pages and the API, and contributors' changes to them are reviewed like any other. See [Administering](administering.md#media-types-and-custom-fields).

## Artwork and achievements

* **Artwork** is stored as references (kind, address, size). The server downloads an image only after safety checks and then serves its own copy by content hash; pages never hotlink a provider.
* **Achievements** are the *list* for a game: name, description, icon, hidden flag, global unlock percentage. Never anyone's progress.

## Nodes and roles

One program, three roles. See [Federation](federation.md).

| Role | Typical use | Database |
| --- | --- | --- |
| `authority` | the public source of truth; publishes a signed change feed | PostgreSQL |
| `mirror` | a regional or read-heavy copy of an authority | PostgreSQL or SQLite |
| `local` | one household or team; caches providers; may follow an authority | SQLite |

## Accounts and reputation

Accounts belong to a node. Contributors propose changes; reviewers approve them. Approved work earns **reputation**; reputation unlocks faster paths. Details in [Using the site](using-the-site.md) and [Reviewing](reviewing.md).
