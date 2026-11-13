# Observation contract

Atlas v2 accepts and emits the tracking app's versioned source-observation contract unchanged, so the app plugin needs no translation layer.

Reference: `unnamed_tracking_app` `src/backend/src/features/game_ingestion/contract.py` (`CONTRACT_VERSION = 1`).

- Batch: `{contract_version: 1, connection: {source_kind, source, source_scope, label?}, media_type?, observations: [1..5000]}`. `media_type` is one enabled Atlas type (`game`, `anime`, `movie`, `tv_show`, or a locally configured type) and defaults to `game` for existing clients.
- Observation: `external_id`, optional `title`, `platform`, `edition`, `release_date`, `provider_ids{namespace: id}`, `facts{...}`, `source_updated_at`, `available`.
- Omitted or `null` = unknown. Never deletes or clears a stored value.
- `source_kind` is one of `host | plugin | manual | metadata`. Atlas responses use `metadata`.
- `media_type` applies to every observation in the batch. Keep external IDs unique within a source scope and media type; send movie/anime/show observations in their own batches so Atlas never joins records across types.

The same media type is available on `POST /api/v2/resolve` items. Typed resolution filters both provider-ID crosswalks and title/year candidates. If an identity already belongs only to another media type, Atlas reports a conflict and does not fall back to a title match.

## Release dates (`ext.release`, protocol 1.3)

`release_date` keeps its meaning: a full day (`YYYY-MM-DD`) or nothing. A source that knows only a year or
a month, or dates per platform and region, sends them in the optional `ext.release` member instead:

```json
{"external_id": "620", "title": "Portal 2", "release_date": "2011-04-19",
 "ext": {"release": {
   "first": {"date": "2011-04-19", "precision": "day"},
   "entries": [
     {"date": "2011-04-19", "precision": "day", "platform": "pc", "region": "ww", "category": "full"},
     {"date": "2011-06", "precision": "month", "platform": "ps3", "region": "eu", "category": "full"},
     {"date": "2010", "precision": "year", "platform": "pc", "region": "ww", "category": "early_access"}]}}}
```

- A date is `YYYY`, `YYYY-MM` or `YYYY-MM-DD` with `precision` `year`, `month` or `day` matching its shape,
  a year between 1950 and 2100, and a real calendar day (`2024-02-29` yes, `2023-02-29` no). Nothing is
  ever padded: `"2011"` is never `2011-01-01`. `quarter` is reserved and refused.
- `first` is the source's first release. When `release_date` is also sent, `first` must be the same day.
- `entries`: at most **5**, each unique on (`platform`, `region`, `edition`, `category`); platform and
  edition compare case-insensitively. `region` is `ww`, `eu`, `na`, `asia`, `oce`, `latam` or an ISO
  3166-1 alpha-2 code in lower case; `category` is `full`, `early_access`, `beta`, `rerelease`, `port` or
  `demo`. An entry's `edition` must equal the observation's `edition` when both are given. A sixth entry
  is refused (HTTP 422), never silently truncated.
- Merge (C5.2): the first release is a hierarchical vote (year, then month, then day, each weighted by
  source trust; a level is decided only with at least half the weight that states it), so a source that
  says `2011` never outvotes one that says `2011-04-19`. Entries are voted per key; a record keeps the five
  most trusted. `explain.first_release` and `explain.releases` name the winning observation, the
  agreeing and disagreeing sources and the vote support; `explain.releases.dropped` counts entries over five.
- Records expose `first_release`, `release_year` and `releases`; `release_date` is present only when the
  first release is known to the day. Search filters and sorts by the first release.

## Books (`ext.book`, the unified release version)

A book observation (`media_type: "book"`) adds an optional `ext.book` member: an ordered `authors` list
(`{name, role, ids}`, at most 30), `subtitle`, `original_language`, `page_count`, a `series` hint
(`{name, position}`) and `editions` (at most 50: `key`, `name`, `isbn_13`, `isbn_10`, `format`, `publisher`,
`published` as a partial date, `pages`, `language`, `provider_ids`). Subjects travel as `facts.features` and the
first publication as `ext.release.first`. ISBNs are normalised and check-digit validated; an invalid one is
dropped, never guessed. The rules, the merge and the ISBN crosswalk are in [Books](../guide/books.md).

## Atlas extensions (additive, namespaced under `atlas`)
- Response includes `atlas.canonical_id`, `atlas.confidence` (0-1), `atlas.match` (`crosswalk | exact | fuzzy | ambiguous`), and `atlas.redirected_from` when a canonical was merged.
- Ambiguous matches return candidates and are never auto-assigned.
- Fields the server derived carry `atlas.explain[field] = {observation_id, source, strategy}`.

## Compatibility rule
Extensions only add keys under `atlas`; a v1 client that ignores them must still behave correctly. Any breaking change bumps the contract version and is served on a new path.
