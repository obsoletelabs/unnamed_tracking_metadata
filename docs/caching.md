# Cache and distribution semantics

The metadata authority treats caches as derived state. PostgreSQL and durable object storage remain authoritative.

## Layers

- **Process fallback:** bounded in-memory TTL cache for standalone/offline operation.
- **Redis:** optional shared cache for horizontally scaled API nodes.
- **CDN/browser:** content-addressed image responses may be cached for one year because the URL contains the SHA-256 of the original asset.
- **Database:** never treated as a cache; canonical metadata and provenance are durable source data.

## Search

Search responses use a 30-second TTL. Cache keys include the normalized query and requested result count. Search results are therefore allowed to be briefly stale, while writes remain immediately visible to direct database reads.

When Redis is unavailable, the process falls back to local memory rather than making the metadata API unavailable. A multi-node deployment should provide Redis when consistent cache sharing is required.

## Invalidation

Canonical mutations should invalidate affected search/metadata keys rather than relying only on short TTLs. Cache invalidation is advisory: every cached response has a bounded TTL and can be rebuilt from PostgreSQL.

## Images

Original images and generated derivatives are content-addressed. Responses use `Cache-Control: public, max-age=31536000, immutable` and an ETag derived from the original SHA-256. Replacing an asset therefore creates a new URL rather than mutating a cached URL.

`/api/v2/media/<sha256>` names the bytes and is immutable. `/api/v2/artwork/<id>/image` names a row whose image can
change: it answers `Cache-Control: public, max-age=300` with the digest as `ETag` (C12 §12.5.5). A source whose media
policy is `redirect` or `none` stops being served from `/media` at once, even when bytes are still on disk.

## Consistency

- Canonical writes: strongly consistent in PostgreSQL.
- Search/cache: eventual consistency, bounded by invalidation plus TTL.
- Image bytes: immutable once addressed by content hash.
- Redis and process memory: disposable and rebuildable.
