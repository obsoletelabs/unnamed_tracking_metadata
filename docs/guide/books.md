# Books

Atlas knows **books** as a built-in media type (contract C15 §2). A book record is a **work**: "Dune", not "the
2005 Ace paperback of Dune". Its printings, translations and audiobooks are its **editions**, an entry list on the
record (`editions` in the API), never records of their own. This page describes the data model; the book pages,
ISBN search and the Open Library provider come in later releases of the same programme.

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
the observation contract (`docs/v2/observation-contract.md` in the repository)). Subjects travel as `facts.features` and the first
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
  authors or editions. An authority whose emit protocol is below the release version refuses book data and book
  locks (HTTP 409, `"code": "emit_protocol"`).
* The lock fields `authors`, `subtitle`, `page_count` and `original_language`, the vocabularies `book_format` and
  `book_creator_role` and the relation member `position` are registered under the same version.
* Book records travel on the public feed like games, and never on the private group channel. Reading history is not
  in Atlas at all.

## Upgrading and rolling back

Migration 0061 adds `relation.position` and the derived `book_creator` table, adopts an existing `book` type as
described above and recomputes book title keys. Rolling back drops the column and the table; the achievements flag
and the recomputed keys stay (the next rebuild of each record writes its key again).
