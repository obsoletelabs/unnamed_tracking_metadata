# Authority modes

The metadata service supports the same conceptual model in three deployment modes. A local authority is not a reduced client: it owns a complete local catalogue and can reconcile multiple sources without an account or a central service.

## Local authority

`AUTHORITY_MODE=local`

- SQLite is the default durable database.
- Local object storage is the default media store.
- Provider credentials belong to the local installation.
- Canonical IDs are local and remain stable even when providers change.
- Provider mappings, aliases, observations, relationships, tags, images and provenance are retained locally.
- The service continues to search and serve metadata without internet access.
- No account is required merely to operate the authority.

## Central authority

`AUTHORITY_MODE=central`

- PostgreSQL is the production source of truth.
- S3-compatible object storage is the durable media authority.
- Redis is optional but recommended for shared cache state.
- Canonical identities and reconciled metadata are shared across clients.
- External providers are observations/evidence, never the canonical database.
- Community preference can influence presentation but cannot silently overwrite authoritative fields.

## Hybrid authority

`AUTHORITY_MODE=hybrid`

- The local authority remains usable when the central service is unavailable.
- Local canonical IDs are never destructively replaced by central IDs.
- `authority_mappings` records the optional local-to-central relationship.
- Local overrides remain local and can be excluded from upstream publication.
- Central synchronization is incremental and should be performed through durable jobs.
- Contributions are opt-in through `PUBLISH_CONTRIBUTIONS=true`.

## Identity boundary

A local entity may look like:

`local:game:01H...`

and later map to:

`central:game:12345`

while retaining provider identities such as `steam:730`, `igdb:98765`, and a Playnite identity. Mapping is evidence, not a destructive merge. A local-only entity remains valid when no central match exists.

## Reconciliation boundary

Provider observations, user-local overrides, community observations and curated canonical values are separate layers. Automated or LLM-assisted reconciliation may propose a match or canonical tag, but proposals must retain confidence and provenance and must not silently become authoritative.

This permits the central authority and user-hosted authorities to reconcile the same underlying game without requiring identical provider availability, credentials, or network access.
