# Central and user-hosted authority modes

The project supports two deployment roles using the same metadata model.

## Central authority

The central service is the shared authority for canonical identities and curated metadata. It uses PostgreSQL, durable object storage, distributed workers, shared cache, moderation and operational tooling. Provider observations and community contributions are retained as evidence; reconciliation promotes selected evidence into canonical fields.

The central service does not need every external source to be available for a canonical read. Provider failures affect freshness and confidence rather than database availability.

## User-hosted authority

A user-hosted installation is a complete metadata authority for its own installation. It can run with SQLite or PostgreSQL and local media storage. No central account is required.

A local authority may combine:

- local applications such as Playnite and local library files;
- the user's own provider API credentials;
- imported metadata;
- cached central metadata;
- private user observations and overrides;
- optional community/central contributions.

The local authority remains functional when the central service is unreachable.

## Synchronization model

Synchronization is additive:

```text
local canonical ID  <->  central canonical ID
provider identity   <->  canonical mapping
local observation   ->    optional contribution
central observation ->    optional local cache
```

A local ID is never replaced merely because a central match is discovered. A mapping can be added later. Local-only entities remain valid without a central counterpart.

Central synchronization must be explicitly configured. Private observations, local paths, personal notes and user-only overrides are never implicitly published.

## Precedence

Local presentation can override central values without claiming that the shared canonical value changed. This distinction lets a user say “this is the title I want displayed here” while preserving the shared metadata authority.

The reconciliation layer therefore distinguishes:

1. source observation;
2. canonical shared selection;
3. local override;
4. presentation preference.

This prevents a personal customization from becoming a global data mutation.
