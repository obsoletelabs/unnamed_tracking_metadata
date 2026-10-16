# API contract

The metadata API is versioned under `/api/v1`. Once a version is published, its response and identifier semantics are compatibility commitments; additive fields are preferred over breaking changes.

## Request identity

API responses include `X-Request-ID` and `X-API-Version`. Clients may supply `X-Request-ID` for tracing, but operators should treat it as an opaque diagnostic value rather than an authentication credential.

## Errors

API errors use one envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "request_id": "..."
  }
}
```

Clients should branch on the HTTP status and stable `error.code`, not on human-readable messages.

## Idempotency

Mutating `/api/` requests may send `Idempotency-Key`. The key is scoped to the caller, HTTP method, path and request body. Reusing a key with a different body returns `409 idempotency_key_reused`.

Successful JSON responses up to the configured storage limit are retained for replay. The database-backed store means replay can work across API processes; cleanup and operational retention are covered by the database lifecycle tooling.

Clients should use a fresh random key for each logical mutation and retry the same key after connection failures.

## Rate limiting

API responses expose `X-RateLimit-Limit` and `X-RateLimit-Remaining`. A rejected request returns `429`, `Retry-After`, and the normal error envelope.

The initial limiter is deliberately conservative and process-local. It is a first safety boundary, not the final distributed quota system. The caching/distribution and authentication issues replace this with shared Redis-backed/token-aware limits while retaining the same response contract.

## Pagination

New collection endpoints must use bounded pagination and must never accept an unbounded `limit`. Cursor pagination is the preferred public contract:

```json
{
  "items": [],
  "next_cursor": "..."
}
```

Cursors are opaque. Clients must not parse or manufacture them. A missing/null cursor means the collection is exhausted. Legacy endpoints may retain their existing shape until migrated explicitly.

## Versioning and deprecation

- `/api/v1` remains stable while compatible clients exist.
- Breaking changes require a new `/api/vN` version.
- Additive fields and new endpoints do not require a new major API version.
- Deprecated endpoints must advertise their replacement and receive a documented removal window.
- Canonical metadata IDs are data identifiers, not API versions; changing an API version must never silently change an entity's identity.

## Client retry policy

Retry only operations documented as retryable. For mutating requests, use `Idempotency-Key` before retrying after an ambiguous network failure. Respect `Retry-After` for `429` and future upstream/dependency throttling responses.

## Local authority compatibility

A user-hosted authority implements the same API shape where practical. Local-only entities can be returned with a local identity and no central mapping. When a local entity is mapped to the central authority, the mapping is additive and does not invalidate the local identifier.
