# Provider architecture

Providers are evidence sources, not canonical authorities. An adapter may search or enrich, but the returned observation is stored with provider identity, timestamp, provenance, licence metadata where available, and confidence before reconciliation.

## Provider lifecycle

```text
not_configured -> healthy -> unhealthy
                     \-> rate_limited
```

A provider outage must degrade metadata freshness, not make canonical reads unavailable. Provider calls are bounded by query/result limits, per-provider quotas, timeouts/retries, and a failure budget.

## Search versus enrichment

Search is deliberately cheap and bounded. It returns a small ranked candidate set. Enrichment happens only after an entity is selected and may retrieve richer fields/media.

This prevents a search request from becoming an uncontrolled fan-out across providers.

## Trust

Provider results are observations. Source trust and observation confidence contribute to reconciliation, but neither one directly rewrites the canonical record.

Provider IDs are namespaced by provider and entity type. The same external ID can never silently become a canonical ID.

## Copyright and terms safety

The service stores provenance alongside imported observations. Adapters should use official APIs or permitted feeds where available and must not bypass access controls, robots restrictions, authentication, rate limits, or licensing terms. Raw provider payloads should not be retained merely because an API returned them; store only the fields needed for the metadata contract and retain attribution/licence information when supplied.

Images use the separate media pipeline and must not be treated as ordinary provider JSON.

## Central authority

The central deployment owns the shared provider registry and can curate source trust, provider health, quotas and canonical selections.

## User-hosted authority

A local deployment may configure its own provider credentials and sources. It can continue enriching while offline from central services and can retain provider observations locally. When connected to the central authority it may reconcile local identities against central IDs without uploading private observations unless the user explicitly enables contribution/sync.
