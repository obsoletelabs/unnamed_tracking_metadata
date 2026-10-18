# Extending Atlas

Most customisation needs no code ([Administering](administering.md)). For the rest:

## A client of the API

Any language can call the [REST API](reference-api.md). A single-file Python client lives in `clients/python/atlas_client.py` (`search`, `game`, `resolve`, `provider_search/fetch`, `submit`). The ingestion format is the Tracking App's **observation contract v1** (`atlas/contract.py`); Atlas adds only optional fields (`platforms`) and never removes any, so clients written against the contract keep working.

## A new metadata provider

A provider is a small class in `atlas/providers/`:

```python
class MyProvider:
    name = "mysource"            # becomes the source name and namespace
    redistributable = False       # provider data is never published on the feed
    requests_per_minute = 60      # shared budget; searches get a third of it

    def search(self, client, query) -> list[ProviderResult]: ...
    def fetch(self, client, external_id) -> ProviderResult | None: ...
    def achievements(self, client, external_id) -> list[dict]: ...   # optional
```

`ProviderResult` carries `external_id`, `title`, optional `release_date` (ISO), `platform`, `provider_ids` (cross-references to other providers: these are what merge records), `facts` (description, developer, publisher, tags, features), `artwork` and `achievements`. Register it in `build_registry` (`atlas/providers/registry.py`) behind a setting. The **Fetcher** adds caching, negative caching, per-provider budgets, a circuit breaker and serve-stale-on-error for you. Give the source a trust weight in `source_trust` and add tests with recorded response shapes (see `tests/test_providers.py`).

## A new field type or merge rule

Merge rules are in `atlas/merge.py` (one function, rules per field). Custom field kinds are in `atlas/custom.py` (`validate_value` plus the form input in `_fields.html`). Keep every rule **deterministic** and **explainable**: record which observation won.

## Keeping docs honest

`python tools/gen_docs.py` regenerates the three reference pages from the code; the test suite fails if they are stale or if a setting or endpoint is undocumented.
