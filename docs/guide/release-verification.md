# Release verification

The tools run **their own disposable nodes and databases**, without provider keys or deployed
production services. Reports contain checks and public node IDs, never passwords, tokens,
recovery codes or private signing keys.

## Repeatable commands

From a source checkout with locked development dependencies:

```sh
uv sync --frozen --extra dev
uv run ruff check atlas tests tools
uv run pytest -q
uv run python -m tools.multinode_drill --output topology-sqlite.json
uv run python -m tools.restore_drill --output restore-sqlite.json
```

For real PostgreSQL, supply a **disposable development cluster** administrator URL:

```sh
uv run python -m tools.multinode_drill \
  --postgres-admin-url "$ATLAS_TEST_PG_URL" --output topology-postgres.json
uv run python -m tools.restore_drill \
  --postgres-admin-url "$ATLAS_TEST_PG_URL" --output restore-postgres.json
```

The tools create random `atlas_drill_*` databases and drop exactly those afterwards, including
on failure. They do not clear the database named in the URL. The account needs create/drop
database permissions. **Do not use production credentials.** Servers use ephemeral loopback
ports. Temporary SQLite/media folders and subprocesses are removed on exit. JSON success
reports are written only after every assertion passes.

## Topology and ordinary users

The topology starts four real HTTP servers: two authorities → mirror → local node. The
PostgreSQL variant uses PostgreSQL for both authorities and SQLite for followers. It tests:

* identities, liveness/readiness and independent origin pins;
* both catalogues/custom schemas downstream, original signatures, relay feed selection and
  repeated sync without duplication;
* signing-key rotation after initial sync without changing the root pin;
* ordinary-user browse, profile, export and read-only personal API tokens;
* private-list create/add/remove/delete, ownership denial, follow/unfollow, escaped comments,
  owner-only edits, reporting, moderator denial, pending proposals, approval and notifications;
* administrator/reviewer denial, CSRF rejection, read-token write denial and cross-node
  cookie/token rejection;
* accounts, sessions and private profile information staying local;
* one authority's outage/restart while the other updates and cached relay reads continue;
* protected metrics and account deletion revoking credentials while games remain.

Comment redirects must place success queries before fragment anchors; the live drill caught
and now covers this regression. Approved public metadata reaches followers, while list notes,
profile data, comments and private proposal notes stay outside the signed feed.

Regressions also cover malformed signatures, ambiguous/unpinned relays, history rewind,
retired keys, concurrent replay and MFA recovery-code consumption. Schema snapshots belong
to their origin, so one cannot delete another's definitions. Independent authorities still
need agreed namespaces and conflict policy; this is not a multi-writer consensus test.

## Restore and recovery

The restore tool creates metadata, synthetic cached artwork, account/password/token, encrypted
MFA and a rotated signing key. It creates/verifies an archive, restores an empty database,
compares **every table** checksum and exact image bytes, verifies password/token authentication,
decrypts MFA and validates signing history/feed. A second restore must refuse occupied storage
without changing it.

`tests/test_promotion.py` reconstructs a candidate from one mirror origin, verifies its fresh
root/feed, excludes local/private/other-authority data, checks source immutability, and refuses
gaps, tampering, unknown kinds, wrong source/role and existing output. See [Recovery](recovery.md).

## Evidence limits

Loopback drills use development HTTP. They do not verify production TLS/proxy headers, SMTP
delivery, public OIDC-provider setup, live RAWG/IGDB quotas, real network partitions,
multi-process load or a Proxmox host. Run the tools in a throwaway LXC to test its actual
storage/runtime; workstation success is **not** an LXC deployment claim. No RPO/RTO target
is established by these functional drills.

Record commit, date, database versions, commands and sanitized reports per release. Keep UI
screenshots with the review. Live-provider checks remain separate; the operator-run package
acceptance below covers installation in the Tracking App.

## Tracking App package acceptance

Provision a fresh disposable Tracking App backend, PostgreSQL and plugin runtime, plus an
Atlas node reachable both by this tool and by the runtime. Start the host with a synthetic
administrator. No host or plugin source is imported by this tool, and the original signed
package is not modified. Set `TRACKING_APP_TEST_PASSWORD` and `ATLAS_TEST_ADMIN_TOKEN` through
private environment configuration, then run:

```sh
uv run python -m tools.tracking_app_drill --disposable \
  --host-url http://127.0.0.1:18764 --username atlas-host-admin \
  --atlas-url http://127.0.0.1:18767 --atlas-peer-url http://atlas:8000 \
  --package /path/to/official.metadata-atlas-0.2.0.utp \
  --output tracking-app.json
```

The command requires an empty plugin inventory and explicit `--disposable` confirmation. It
creates a user/game, installs a package, changes provider configuration and leaves that test
state for inspection. Remove **only your own stack and volumes** afterwards. Never point it
at an existing deployment. Production credentials are neither needed nor appropriate.

The check exercises a real signed installation/worker, ordinary-user configuration inheritance
and secret redaction, search/enrichment/selection, session ownership, library refresh, manual
field locks and failure while Atlas is unreachable. It does not verify browser layout,
production sandboxing, public artwork delivery or achievements. The package SHA-256 and public
publisher key ID appear in the report. This cross-repository drill is operator-run; the four-node
and restore drills run in CI without needing the Tracking App checkout.
