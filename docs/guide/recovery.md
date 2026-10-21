# Recover an authority from a mirror

Restore a verified authority backup whenever possible: it preserves accounts, credentials,
private data and the original identity. This procedure is for a lost authority whose available
backup is too old, when a mirror retains its complete original signed public feed.

## Safety boundary

`atlas recover-mirror` builds an **offline candidate archive under a new identity**. It never
changes the live mirror, takes over the old key, starts a server, redirects followers or
contacts the lost authority. It uses a consistent read snapshot, verifies every sequence and
signature (including successor-key proofs), and refuses missing history or unknown event kinds.

Only one explicitly selected origin is recovered. Other authorities, local overlays, provider
caches, accounts, passwords, sessions, tokens, MFA, reputation, lists, comments, mail, site
configuration and peer credentials are excluded. Canonical IDs, original public payloads,
merges and schema history are replayed and re-signed under the fresh root. Original signed
entries remain separately as evidence. Recovered schema becomes locally administrable.
Malformed semantic payloads retain the follower's existing validation behavior; a valid
signature does not assert that every payload is useful metadata.

Artwork **references** recover; cached bytes do not. Run the normal cache job on the candidate.
Expired/unavailable URLs may need curator repair. Private/local data needs its own backup.
There is no account migration from federation.

## Fence, verify, build

1. **Fence the original writer.** Stop its service and prevent an old VM/container, scheduled
   restore or stale database replica from restarting it. Isolate its old write endpoint and
   credentials. Record who performed fencing and how it was verified. If it cannot be fenced,
   do not advertise the replacement as the same authoritative service.
2. Freeze changes on the selected mirror. Back it up, verify the archive and stop its
   sync/server processes for a repeatable operational snapshot. The command itself only reads
   its database. Record the independently published authority fingerprint, mirror fingerprint,
   last applied cursor and retained head.
3. Older mirrors may know a cursor but lack original signatures: backfill while an authentic
   source is available. An incomplete cache cannot manufacture missing entries after loss.
4. Build to a new private path with the mirror's usual database configuration:

    ```sh
    ATLAS_ROLE=mirror atlas recover-mirror \
      --from-authority <original-authority-node-id> \
      --confirm-mirror <source-mirror-node-id> \
      --old-authority-fenced \
      --output /private/replacement-candidate.tar.gz
    atlas backup verify /private/replacement-candidate.tar.gz
    ```

   The flag is an operator assertion; software on a partitioned mirror cannot prove a remote
   machine is off. The command prints the new public root, recovered head and final body hash.
   Keep them in the incident record. The archive contains a **new private signing key**, is
   owner-readable only from creation, and must be encrypted off-host. Existing output is
   refused. Original private signing keys are never required or copied.

## Validate and cut over

1. On a separate empty candidate database at the same revision, run `atlas migrate`, then
   `atlas backup restore replacement-candidate.tar.gz`. Configure it as an authority. Follow
   [Deployment](deployment.md) for HTTPS, PostgreSQL, encryption, email and first-admin bootstrap.
   Configure fresh credentials; none are inherited from the mirror.
2. In isolation, check `atlas node`, records/custom fields, redirects, signed feed continuity,
   ordinary-user registration/sign-in and a fresh follower pinned to the **new root**. Record
   gaps caused by original replication delay. Writes beyond the retained head cannot recover.
3. Publish the new fingerprint through the independently trusted initial-pinning channel. On
   each follower **disable/remove the old peer and environment pin**, then explicitly trust
   the new root and pull. Do not follow both as competing authorities during cutover. Compare
   catalogue records and verify advancement from the new feed.
4. Move routing after checks and maintain the old writer's fence. Back up/verify the replacement
   immediately. Retain original public history and incident evidence.

The command never produces two writers for the **same cryptographic identity**. It does not
implement leader election, a quorum or automatic network fencing. Two trusted roots can still
publish conflicting metadata; operational fencing and follower retirement are essential.

## Abort or roll back

Before cutover, discard the candidate; the source mirror is unchanged. After the replacement
accepts writes, freeze both sides before reversal, preserve both histories and reconcile
changes explicitly. Never put an old private key into a running replacement or start an
unfenced snapshot as a second writer.

See [Release verification](release-verification.md) for recovery regression coverage.
