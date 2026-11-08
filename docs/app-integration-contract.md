# `unnamed_tracking_app` integration contract

The metadata authority is a source of canonical identity and reconciled metadata, not a provider proxy. The client must remain functional when the authority or an external provider is unavailable.

## Search vs enrichment

`GET /api/v1/search` is a bounded discovery operation. It returns canonical IDs, titles, a match score and whether the match came from a canonical title or alias. It must not trigger expensive provider enrichment.

A future enrichment operation should accept a selected canonical ID and return provider observations plus provenance. Enrichment should be bounded to the top three ranked providers and must be safe to retry.

## Identity

The client stores the metadata authority's canonical ID separately from provider IDs. Provider IDs are mappings/evidence and must never replace the canonical identity when a provider changes.

### Resolution by established identity (contract C4, #96)

`POST /api/v2/resolve` reports the evidence behind every answer, so a client never mistakes weak or conflicting evidence for a safe refresh. The full table of `reason` and `tier` values is in the [Tracking App guide](guide/tracking-app.md#resolving-ids-post-resolve). The guarantees, and the tests that pin them:

| Guarantee | Evidence |
| --- | --- |
| A known Atlas id or provider id resolves to the same canonical id whatever title the client sends (renamed records, placeholder titles such as `Steam app 12345`) | `tests/test_resolve_contract.py`: rename, placeholder and fixture cases; `tests/test_tracking_app_contract.py::test_candidate_from_another_provider_resolves_by_crosswalk` |
| A merged Atlas id resolves to the live record (`reason: redirect`, `redirected_from`), through a chain of merges; the merged record's provider ids follow the merge | `test_merge_chain_redirects_by_id_and_keeps_old_ids_resolving`, fixture cases `merged atlas id, chain of 2` and `provider id of a merged record follows the merge` |
| Ids that point at more than one record are a `conflict` with `canonical_id: null` and `method: none`; the candidates, the matched keys, the source's own record and the open curator review are reported | `test_conflict_reports_keys_and_the_review_ingest_opened_without_writing`, fixture cases `two sources claim the same id` and `atlas id contradicted by a provider id` |
| The conflict is decided across media types, before `media_type` is applied: an id naming a record of another type still makes it a `conflict`, and its key is reported | `test_ids_pointing_at_records_of_different_types_are_a_conflict_not_a_crosswalk` |
| Ids that all name one record of another media type are a `type_mismatch` with no id, never title evidence | fixture case `identity of another media type` |
| Title-only evidence assigns only when exact, clearly ahead of the next candidate and not contradicted by a different id the client holds in the same namespace; equal titles on different games are `ambiguous` | fixture cases `identical titles …`; `test_identical_titles_list_both_candidates_and_assign_neither` |
| `/resolve` writes nothing: no review, crosswalk row or version changes | `test_conflict_reports_keys_and_the_review_ingest_opened_without_writing` |

Failure semantics for clients: assign only `reason` ∈ {`id`, `redirect`, `crosswalk`, `exact`}. Treat `conflict`, `type_mismatch` and `ambiguous` as "do not apply" and keep the current local record and its metadata. On `redirect`, replace the stored Atlas id with `canonical_id`. Atlas does not merge or overwrite anything on the client's behalf.

Host and plugin compatibility: the cases live as data in `tests/fixtures/resolve-contract-v1.json`, with its SHA-256 (over LF line endings) in `resolve-contract-v1.json.sha256`. The metadata-atlas plugin vendors that file and replays it against its own gateway. Nothing in it is host or user-library state: every record is synthetic. An older plugin that reads only `method` stops adopting conflicting ids, because a conflict now has `method: none` (decision H2).

## Shared vocabularies

Atlas, the metadata-atlas plugin and the Tracking App agree on the closed value lists in
[`contracts/vocab-v1.json`](https://github.com/obsoletelabs/unnamed_tracking_metadata/blob/v2/contracts/vocab-v1.json)
(contract C7.4): achievement `tier` (`bronze`, `silver`, `gold`, `platinum`) and `kind`, `crossplay`
(`yes`, `partial`, `no`), and the values reserved for later protocols (`relation_type`,
`date_precision`, `release_category`). `feed_kinds` is Atlas-only. Its SHA-256 (over LF line endings)
is in `vocab-v1.json.sha256`.

* Atlas generates the file from `atlas/vocab.py` (`python -m tools.vocab_check generate`), and
  `tests/test_vocab_platinum.py` fails when the file, the digest or the code disagree.
* `tools/vocab_check.py` needs only the Python standard library for `verify` and `compare`, so the
  other repositories can vendor it: `python vocab_check.py verify vocab-v1.json` checks a file and its
  digest, and `python vocab_check.py compare atlas.json plugin.json host.json` fails (exit 1) when the
  lists they share differ. Value order does not matter; keys only one side has are not compared.
* Absent means unknown in every list. `not_applicable` is never a stored or sent value; crossplay
  derives it for display from `players_max == 1` (C7.2, C7.3).
* A new value is a protocol minor (platinum is 1.2): a client or plugin must be ready to ignore a value
  it does not know rather than reject the whole response.

A self-hosted authority may return a local canonical ID. The client must accept that ID without requiring a central account. Hybrid instances may later expose a central mapping without requiring a destructive client-side merge.

## Images

Images are selected through role and aspect-ratio-aware presentation endpoints. The client should prefer the authority's content-addressed media URL and may cache it indefinitely because the URL contains the source SHA-256.

Each artwork entry has two paths to the same cached bytes (issue #46):

| Field | Path | Token | Use it for |
|---|---|---|---|
| `media_url` | `/api/v2/media/<sha256>` | `read` scope (Authorization header) | server-side downloads that already send the token |
| `public_url` | `/media/<sha256>` | none | `<img>` tags, browsers and hosts that cannot send a header |

`public_url` is `null` until the image is cached, and whenever the source's media policy is not `proxy` (a provider
switched to `redirect` or `none` stops being served). An `<img src>` pointing at `media_url` fails with 401 because a
browser sends no Authorization header; that was the "assigned images do not show" symptom in #46. Achievement
`icon_url` / `locked_icon_url` use the `/api/v2/media/<sha256>` form; the same digest is served token-free at
`/media/<sha256>` under the same rule.

## Tags

Provider tags are observations. Canonical tags are reconciled identities. Client display should use canonical tags while retaining provider provenance where needed for debugging or future reconciliation.

LLM-assisted reconciliation is advisory: a proposed `race` → `racing` mapping needs provenance/confidence and must not silently rewrite canonical data.

## Achievements

Achievement identities remain attached to their game/provider mapping. A provider achievement may be imported without assuming that another provider's achievement with the same name is identical.

## Failure and offline behavior

- Search failure: use the client's existing local/cache data.
- Enrichment failure: retain the selected game and existing metadata; retry asynchronously.
- Image failure: use previously cached image or omit the image; never block game identity.
- Authority unavailable: do not delete or merge local game identities.
- Provider unavailable: keep the last successful observation and its freshness timestamp.

## Authentication

Client credentials use scoped API tokens. Read-only client operations should use `read`; mutation or contribution features require additional explicit scopes. Tokens must never be embedded in image URLs or logged in plaintext.

## Caching

Canonical identity mappings can be cached longer than provider observations. Search is short-TTL and rebuildable. Media is content-addressed and immutable. Client caches must not be treated as authoritative over a successful canonical response.

## Compatibility testing

The integration suite should exercise:

1. Search with punctuation/case differences.
2. Canonical ID lookup after provider ID changes.
3. Local-only authority entities.
4. Central-to-local and local-to-central mappings.
5. Offline reads from cached canonical data.
6. Provider enrichment retry after a timeout.
7. Image role/aspect-ratio selection.
8. Canonical tags versus provider tag observations.
9. Scoped token rejection.
10. Idempotent mutation retries.
