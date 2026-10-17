# Data lifecycle and community rights

The service separates durable canonical metadata from personal/account data and disposable derived data.

## Personal data

- Account email, display name and authentication records are retained while the account exists.
- API tokens are stored only as hashes; plaintext tokens are never recoverable from the database.
- Mutation audit events retain a principal hash and request metadata, not bearer credentials or request bodies.
- Account deletion should revoke authentication material immediately and anonymize or detach attributable community records where doing so does not break moderation/audit requirements.

## User export and deletion

A production account workflow must provide an authenticated export of user-owned records (profile, tokens metadata, submissions, votes and other attributable content) and a deletion operation with explicit confirmation. Authentication/security records may be retained in a minimized form where required for abuse prevention or legal obligations.

The implementation must not delete canonical games merely because their creator account was deleted. Canonical records are shared authority data and provenance must remain meaningful without retaining unnecessary personal information.

## Community content

Uploaded media must carry source/licence/attribution metadata where known. A contribution is not automatically treated as public-domain or freely redistributable merely because it was uploaded by a user. Moderation can remove content from presentation while preserving the minimum audit trail needed to explain the action.

Images should be garbage-collected only after no active canonical/presentation/reference record requires the underlying object. Content-addressed duplicates may share one stored object.

## Provider observations

Provider observations are replaceable evidence, not personal user data. They retain freshness and source provenance. A provider may be temporarily unavailable without deleting the last known observation.

## Derived data

Search indexes, Redis entries, generated image derivatives and metrics are disposable. They may be rebuilt from canonical data and durable media. Cache eviction is not data deletion.

## Retention targets

- Expired idempotency records: purge after their configured TTL.
- Stale authentication/session artifacts: purge according to configured security policy.
- Audit/security records: retain for the minimum operational/security period selected by the deployment.
- Provider observations: retain while useful for provenance/history; allow deployment-specific archival/pruning.
- Derived caches/indexes: no contractual retention; rebuild as required.

Exact legal retention requirements are deployment-specific and must be configured by the operator rather than inferred by the application.
