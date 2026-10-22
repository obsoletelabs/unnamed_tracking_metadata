# Observation contract

Atlas v2 accepts and emits the tracking app's versioned source-observation contract unchanged, so the app plugin needs no translation layer.

Reference: `unnamed_tracking_app` `src/backend/src/features/game_ingestion/contract.py` (`CONTRACT_VERSION = 1`).

- Batch: `{contract_version: 1, connection: {source_kind, source, source_scope, label?}, media_type?, observations: [1..5000]}`. `media_type` is one enabled Atlas type (`game`, `anime`, `movie`, `tv_show`, or a locally configured type) and defaults to `game` for existing clients.
- Observation: `external_id`, optional `title`, `platform`, `edition`, `release_date`, `provider_ids{namespace: id}`, `facts{...}`, `source_updated_at`, `available`.
- Omitted or `null` = unknown. Never deletes or clears a stored value.
- `source_kind` is one of `host | plugin | manual | metadata`. Atlas responses use `metadata`.
- `media_type` applies to every observation in the batch. Keep external IDs unique within a source scope and media type; send movie/anime/show observations in their own batches so Atlas never joins records across types.

The same media type is available on `POST /api/v2/resolve` items. Typed resolution filters both provider-ID crosswalks and title/year candidates. If an identity already belongs only to another media type, Atlas reports a conflict and does not fall back to a title match.

## Atlas extensions (additive, namespaced under `atlas`)
- Response includes `atlas.canonical_id`, `atlas.confidence` (0-1), `atlas.match` (`crosswalk | exact | fuzzy | ambiguous`), and `atlas.redirected_from` when a canonical was merged.
- Ambiguous matches return candidates and are never auto-assigned.
- Fields the server derived carry `atlas.explain[field] = {observation_id, source, strategy}`.

## Compatibility rule
Extensions only add keys under `atlas`; a v1 client that ignores them must still behave correctly. Any breaking change bumps the contract version and is served on a new path.
