# `unnamed_tracking_app` integration contract

The metadata authority is a source of canonical identity and reconciled metadata, not a provider proxy. The client must remain functional when the authority or an external provider is unavailable.

## Search vs enrichment

`GET /api/v1/search` is a bounded discovery operation. It returns canonical IDs, titles, a match score and whether the match came from a canonical title or alias. It must not trigger expensive provider enrichment.

A future enrichment operation should accept a selected canonical ID and return provider observations plus provenance. Enrichment should be bounded to the top three ranked providers and must be safe to retry.

## Identity

The client stores the metadata authority's canonical ID separately from provider IDs. Provider IDs are mappings/evidence and must never replace the canonical identity when a provider changes.

A self-hosted authority may return a local canonical ID. The client must accept that ID without requiring a central account. Hybrid instances may later expose a central mapping without requiring a destructive client-side merge.

## Images

Images are selected through role and aspect-ratio-aware presentation endpoints. The client should prefer the authority's content-addressed media URL and may cache it indefinitely because the URL contains the source SHA-256.

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
