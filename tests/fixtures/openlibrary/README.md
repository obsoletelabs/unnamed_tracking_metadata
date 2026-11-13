# Open Library fixtures (synthetic)

Used by `tests/test_a22_openlibrary.py` and `tests/test_a22_book_enrichment.py` (CONTRACT-C15 §3.4).

**Nothing here was recorded from Open Library.** Every work, author, edition, ISBN, cover id and description is
invented and laid out in the response shapes of Open Library's JSON API (checked against the live service on
2026-11-11 with `python -m tools.record_openlibrary_fixtures --compare-live`, which prints key names only). Open
Library observations are not redistributed by Atlas (D15.2) and descriptions can be publisher text, so no real
answer is committed. Licence of these files: CC0 (`DATA-LICENSE.md`).

The files are **generated**: edit the catalogue in `tools/record_openlibrary_fixtures.py` and run
`python -m tools.record_openlibrary_fixtures`. The test suite fails when a file differs from what the script writes.

| Contract case (§3.4) | File |
| --- | --- |
| search | `search_salt_cartographer.json`, `search_key_<work>.json` (author names and keys of one work) |
| work | `work_OL9900000001W.json` (month-precision first publication, subtitle, subjects, covers) |
| editions of a work | `editions_OL9900000001W.json`: ISBN-13 + a mismatched ISBN-10, ISBN-10 only, a bad check digit, no ISBN with `c2011` and 0 pages, a 1948 date (outside C5.1) |
| ISBN lookup (redirect target) | `isbn_9780009900112.json`; the 302 itself is in `routes.json` |
| work redirect | `work_redirect.json` (`/type/redirect` to work 1) |
| no dates | `work_no_dates.json`, `editions_empty.json` |
| free-text dates | `work_free_text_dates.json` (`c1972`), `editions_OL9900000004W.json` (`Spring 1999`, `1/2/1999`) |
| 31 authors | `work_many_authors.json`, `search_key_OL9900000005W.json` |
| ISBN-10 only / bad ISBN editions | `edition_isbn10_only.json`, `edition_bad_isbn.json` |
| empty search | `search_empty.json` |
| HTTP 429 | `http_429.json` (the status and `Retry-After` are in `routes.json`) |

`routes.json` says which file answers which request path (and `q`), with its status and headers.
