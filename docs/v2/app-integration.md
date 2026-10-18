# Tracking app integration

The tracking app has no Atlas client yet. This is the contract the planned `official/metadata-atlas` plugin should implement, using the existing `metadata_providers.*` capabilities and `network.outbound`.

| Plugin hook | Atlas call |
|---|---|
| `search` | `GET /providers/{p}/search?q=` for a chosen provider, or `GET /games?q=` for Atlas itself |
| `metadata(id)` | `GET /games/{atlas id}` (follows redirects; `redirected_from` says when the id was merged) |
| library import | `POST /resolve` with `[{namespace, external_id, title, platform, release_date}]` up to `ATLAS_MAX_BATCH` per call |
| unknown game | `POST /providers/{p}/fetch` then use the returned id |
| report what the host knows | `POST /observations` with `source_kind: "plugin"` (no personal facts are kept) |

Mapping: store the Atlas id in the app's `provider_ids` under namespace `atlas`; `locked_fields` always wins client-side, and omitted fields never clear data (same rule as the ingestion contract).

Open items on the app side: a canonical-id namespace in `GameIdentity`, mapping Atlas `tags`/`features` onto the app's flat tag arrays, and media (Atlas does not serve artwork yet).
