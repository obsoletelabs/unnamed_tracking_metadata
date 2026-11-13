# Books

Atlas knows **books** as a built-in media type (contract C15 §2). A book record is a **work**: "Dune", not "the
2005 Ace paperback of Dune". Its printings, translations and audiobooks are its **editions**, an entry list on the
record (`editions` in the API), never records of their own. This page describes the data model, the Open
Library provider, finding books by ISBN or author, and the book pages curators work with.

| Concept | In Atlas |
| --- | --- |
| Work | a record with `media_type = "book"`: title, authors, description, subjects, first publication |
| Edition | an entry of the record's `editions[]`: ISBN, format, publisher, date, pages, language |
| Author | an ordered list `authors` on the record; no person records |
| Series | a record of the type `series` and a `series_member` relation **with a position** |
| Omnibus, box set | a `collection` and `collection_member` relations, as for games |

## The `book` media type

Every node seeds `book` (label "Book") next to the other built-in types. Its field labels read as a book's:
`developer` is shown as **Author**, `release_date` as **First published**, `platforms` as **Formats** and
`features` as **Subjects**. It has no custom fields: page count and original language are book fields of the
record (below), with one source of truth each. Achievements do not apply to books, so the type is seeded with its
achievements flag off (see [Achievements](achievements.md)).

**A node that already had a `book` type** (one an administrator created, or one inherited from an upstream) keeps
it: its name, labels and custom fields are unchanged. The upgrade only turns its achievements flag off and shows a
notice on **Admin → Media types** ("Media types that became built-in"), also written to the audit log
(`media_type_builtin`). A custom field whose key is also a book field (`authors`, `subtitle`, `page_count`,
`original_language`, `editions`) keeps its values under `custom` and is shown under its own label.

## What a book observation carries (`ext.book`)

A provider, curator or client sends book data in the optional `ext.book` member of an observation (see
the observation contract (`docs/v2/observation-contract.md` in the repository). Subjects travel as `facts.features` and the first
publication as `ext.release.first`, exactly as for any other record.

```json
{"external_id": "W-17", "title": "The Lantern Keeper", "facts": {"features": ["Lighthouses", "Fantasy"]},
 "ext": {"release": {"first": {"date": "1971", "precision": "year"}},
         "book": {
           "authors": [{"name": "Ione Marsh", "role": "author"},
                       {"name": "Pell Arden", "role": "translator"}],
           "subtitle": "A Novel of the Cold Coast",
           "original_language": "en",
           "page_count": 412,
           "series": {"name": "Cold Coast", "position": "1"},
           "editions": [
             {"key": "E-1", "name": "Paperback, Harbour Press 2005", "isbn_13": "978-1-234-56789-7",
              "format": "paperback", "publisher": "Harbour Press",
              "published": {"date": "2005-08", "precision": "month"}, "pages": 528, "language": "en"}]}}}
```

| Member | Rule |
| --- | --- |
| `authors` | at most 30, in order. `name` 1-200 printable characters; `role` is `author`, `editor`, `translator`, `illustrator`, `narrator` or `contributor` (default `author`); `ids` maps a provider key to that source's author id (never `atlas`, `curator`, `manual` or `community`) |
| `subtitle` | 1-300 printable characters |
| `original_language` | a language code such as `en`, `fra` or `pt-BR` |
| `page_count` | 1-100 000 |
| `series` | a hint: `name` (1-200) and `position` (1-16 characters, such as `"1"` or `"2.5"`). The series itself is a relation, below |
| `editions` | at most 50. `format` is `paperback`, `hardcover`, `ebook`, `audiobook` or `other`; `published` is a partial date like `ext.release.first`; `pages` 1-100 000; `provider_ids` holds that edition's ids |

The whole member is at most 64 KiB. An unknown member or value is refused (HTTP 422).

**ISBNs.** `isbn_13` and `isbn_10` accept spaces, hyphens and a lower-case `x`, and are checked against their check
digit. An ISBN-10 is converted to its ISBN-13 (`978` + nine digits + a new check digit); a missing ISBN-10 is
derived from a `978` ISBN-13; a `979` ISBN has no ISBN-10. An invalid ISBN, or an ISBN-10 that names another book
than the ISBN-13 next to it, is **dropped** (the edition is kept) and counted in
`atlas_book_isbn_rejected_total`. Atlas never guesses or corrects a digit.

## How a book record is merged

* **Authors** are merged by priority: the whole list of the most trusted observation that states one wins. Two
  sources' lists are never interleaved. `subtitle`, `page_count` and `original_language` follow the same rule.
* All four can be **locked** by a curator (`PUT /games/{id}/locks/{field}`). A lock is checked against the rules
  above: `authors` takes a list of `{name, role, ids}`, `page_count` a whole number.
* **`developer`** stays one "primary creator" string. When no source states one, it is the first author's name
  (`explain.developer.strategy = "derived"`), so clients that read only `developer` show an author.
* **Editions are grouped by ISBN first.** An edition with an ISBN-13 is one entry per ISBN, whatever each source
  calls it; two printings both called "Paperback" with different ISBNs stay two entries. An edition without an
  ISBN groups by its name, and joins an ISBN entry only when exactly one ISBN entry has that name. A missing name is
  written as "Format, publisher year". Each entry shows the most trusted source's details. At most 50.

## ISBNs find the work

Each ISBN-13 of an edition of a current **provider or curator** observation is a crosswalk key (namespace `isbn`)
of the work, so many ISBNs point at one record and `POST /api/v2/resolve` with `provider_ids: {"isbn": "…"}`
answers the work. A user's report from the tracking app may match by ISBN but never registers one. When the
observation is replaced, its ISBNs follow; when one is split onto its own record, they move with it.

The **same ISBN claimed by two works** is never merged automatically (publishers reuse ISBNs, and an omnibus has its
own ISBN): the key stays where it was and a review opens with reason **`isbn_conflict`** in the review queue. Inside
one work, a duplicate ISBN is one edition.

A `record` entry (federation) can carry only one ISBN, the ISBN-13 of the record's first edition; followers get the
others from their own providers.

## Finding books

* **By ISBN.** Type an ISBN-10 or ISBN-13 into any search box (hyphens and spaces are fine, `x` or `X` as the last
  digit of an ISBN-10). Atlas looks it up among the ISBNs of every edition it knows and shows that one book. An ISBN
  with a wrong check digit is not an ISBN, so it is searched as ordinary text. The API does the same:
  `GET /api/v2/games?q=978-1-234-56789-7`. Other filters still apply to that book (type, author, release dates), and a
  bad parameter is refused as in any search.
* **By author.** On the **Book** tab of the catalogue an **Author** box finds books by anyone credited (authors,
  editors, translators, illustrators, narrators): it matches the start of any part of a name, ignoring case and
  accents, so `herb` finds "Maren Hérbertsdóttir" and `maren h` finds her too, while `erb` does not. In the API,
  `GET /api/v2/games?type=book&author=herb`; `author` without `type` means books, and with another type it is refused
  (400). On the Book tab a plain search also matches authors' names.
* Author search reads a small table Atlas rebuilds whenever a book changes (at most 30 people per book), so a new
  author list, a curator's list or a pinned one is searchable at once.

## The book page

A book's page shows the title and subtitle, the people credited in order ("by Maren Hérbertsdóttir and Ilse Varn;
translated by Teo Varga"), and its series with the book's position, linked to the series' own page. **First
published**, the editions' dates and every other date go through the same date formatter as the rest of the site, so
they follow each viewer's choice (31/12/2026 by default, "31 December 2026", 12/31/2026 or ISO; a year alone is
just the year, a month is "February 2006"). Pages, the original language and the subjects are listed with the other
facts.

The **Editions** table lists each edition's format, publisher, date, pages, language and ISBN-13, with a **Copy**
button for the ISBN. Books have no achievements section. When the record holds Open Library data, the page says
**Book data from Open Library** and links to the work on openlibrary.org.

## Curating books

Curators (and administrators) see two links on a book's page:

* **Edit authors** opens an ordered list of names and roles. Change the numbers in *Order* to reorder, leave a name
  empty to remove it, and add people in the empty rows. Saving stores the list as the curators' own observation, so
  it is used instead of a provider's list; ticking **Pin this list** also locks it (the `authors` lock), so later
  imports cannot change it. Unticking it removes the lock. An Open Library author id stays with its name.
* **Set the series** links the book to a series record with its position ("1", "2.5", "0" for a prequel). Pick an
  existing series from the search, or type a name: a series of that name is linked if there is one, otherwise it is
  created. When a provider says the book is part of a series, its name and number fill the form, but nothing is
  linked until a curator saves it. To change a position, save the same series with the new number.

On an authority both are signed into its feed like any other change; on local nodes and mirrors they stay on the
node.

**Importing books.** On **Import** choose Open Library: the search box takes a title, an author or an ISBN, and an
**ISBN** box imports that book straight away (an ISBN goes straight to Open Library's book lookup, with no search in
between). An invalid ISBN is refused before anything is sent.

**A new personal node.** While a local node's catalogue is empty, its home page offers **Add a game** and **Add a book
by ISBN** side by side. Open Library needs no key and is on by default, so a node with no upstream and no provider
keys can add its first book from an ISBN.

## Matching books

For books, the title key keeps "Edition" words and trailing brackets: "The Second Edition" is a real title. The
soft evidence is the title, the **first author** and the first publication year; the format is never evidence that
two books are one. The upgrade recomputes the stored key of existing book records.

## Series and their order

A book's place in its series is the `position` of its `series_member` relation (`PUT /games/{id}/relations`,
`{"to": "<series id>", "type": "series_member", "position": "2"}`). Only `series_member` takes a position; any
other type is refused. Changing the position ends the relation and adds a new one, so the record's relation history
shows both. See [Relationships](relations.md).

## Federation

Book data is registered under the **one release version** of this release, not under a protocol minor of its own.

* An `obs` that carries `ext.book` is always signed with the `_requires` token **`book-v1`** (advertised in
  `capabilities`). A follower that cannot apply it **parks** the entry instead of storing a bare record without
  authors or editions. Every authority of this release (wire 1) signs book data and book locks.
* The lock fields `authors`, `subtitle`, `page_count` and `original_language`, the vocabularies `book_format` and
  `book_creator_role` and the relation member `position` are registered under the same version.
* Book records travel on the public feed like games, and never on the private group channel. Reading history is not
  in Atlas at all.

## Open Library

Open Library is the built-in, keyless book provider (contract C15 §3). It is **on by default**, so a fresh node,
even a local one with no upstream and no keys, can import a book: **Admin → Import**, provider *Open Library*, then
a title, an Open Library work id (`OL…W`), an edition id (`OL…M`) or an ISBN-10/13 (hyphens allowed). An ISBN is
always looked up as an identifier, never searched as a title.

What an import brings: the title and subtitle; the **authors** in order (at most 30, each with its Open Library id;
Open Library states no roles, so each is an `author`); the first publication as `ext.release.first`, at the
precision the text proves ("March 1987" is a month, "c1972" or "Spring 1999" a year, a date before 1950 is left
out, a day is never invented); subjects as **Subjects**; the description; up to 50 **editions** (editions with an
ISBN first, then newest first) with ISBNs, format, publisher, date, pages and language; and the cover as a `poster`
image, by cover id. A work Open Library has merged into another is followed once: the record gets the surviving id
and keeps the old one as an alias, so a link or a job that still names it lands on the same record.

**Being a good client.** Atlas sends `User-Agent: atlas-metadata/<version> (+<project URL>[; <contact>])` with every
request to Open Library and covers.openlibrary.org. The contact is **empty unless you set it**: Admin →
Integrations → Open Library, or `ATLAS_PROVIDER_CONTACT` (an e-mail address or an `https://` URL). Your
`ATLAS_NODE_OPERATOR` is never sent. When a cover download is redirected from covers.openlibrary.org to the
Internet Archive's storage hosts (archive.org), those hops get the same User-Agent **without** the contact. Atlas makes at most one request a second per process whatever the contact
(with several workers each paces itself), caches every answer for `ATLAS_PROVIDER_TTL_SECONDS`, and never walks the
catalogue: imports, searches and follower enrichment are always about one book a person or a record asked for. A
work costs three or four requests (the work, its author list, its editions, and a redirect when there is one); an
ISBN two more.

**Open Library data stays on this node** (decision D15.2). A book imported from Open Library reaches followers as a
`record` (its id, title, Open Library id and one ISBN). A follower with `ATLAS_SYNC_ENRICH_FROM_OWN_PROVIDERS=true`
then fetches the work from its own Open Library access and builds the same editions, authors and cover itself;
only Open Library work, edition and ISBN ids are fetched (an `isbn` key alone is not a provider). Covers are cached
on each node and attributed to Open Library. The download follows covers.openlibrary.org's redirect to the
archive.org storage hosts and no other host.

**Checking it against the live service.** CI never calls Open Library: the tests use synthetic fixtures
(`tests/fixtures/openlibrary/`, written by `python -m tools.record_openlibrary_fixtures`). By hand,
`python -m tools.openlibrary_live_smoke [--contact you@example.org]` makes about six requests and prints counts and
ids only; the *Live Open Library* workflow runs it on manual dispatch.

## Upgrading and rolling back

Migration 0061 adds `relation.position` and the derived `book_creator` table, adopts an existing `book` type as
described above and recomputes book title keys. Rolling back drops the column and the table; the achievements flag
and the recomputed keys stay (the next rebuild of each record writes its key again).

**Open Library is on after the upgrade** (D15.7). A node that never set `ATLAS_OPENLIBRARY_ENABLED` starts making
outbound requests to Open Library once it runs this release: when someone searches or imports a book, when follower
enrichment (H56) meets an `openlibrary` key, and when it caches an Open Library cover. Nothing is fetched on its own
at start-up. To keep the node from calling Open Library, set `ATLAS_OPENLIBRARY_ENABLED=false` (or switch it off in
Admin → Integrations) before upgrading.
