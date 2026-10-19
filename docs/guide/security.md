# Security

## What is protected, and how

| Area | Controls |
| --- | --- |
| Accounts | scrypt password hashing; server-side sessions stored hashed; `HttpOnly`, `SameSite=Lax` (and `Secure` over HTTPS) cookies; per-address and per-account throttling on sign-in, registration, reset and verification; identical answers for known and unknown accounts |
| Forms | a CSRF token on every state-changing form; strict Content-Security-Policy with **no inline script or style**; clickjacking and sniffing headers; user text is always escaped; Markdown pages cannot contain HTML |
| API | tokens stored only as hashes, scoped, expiring, revocable, fail-closed; personal tokens are read-only and die with the account; rate limits; body size caps; bounded batches |
| Trust model | reserved source names need `curate`; uncurated claims can match but cannot register identifiers in someone else's namespace; far-future timestamps ignored; personal facts discarded on arrival |
| Artwork | downloads only from public addresses (pinned address, every redirect re-checked, tunnelled IPv6 refused, environment proxies ignored), size and pixel limits, full image decode, content-hash storage, served with `nosniff` and a restrictive CSP |
| Federation | Ed25519-signed feed (sequence, kind, content), pinned trust, gap/rewind/tamper rejection, provider data never republished; even from a trusted peer, observations, schema and values are validated like local input and a malformed entry is skipped instead of stalling replication |
| OIDC | the sign-in is bound to the browser that started it (login-CSRF safe), exact redirect match, consent, PKCE, single-use burned codes, pinned issuer keys, algorithm never negotiated, email never used to take over an account |
| Secrets at rest | peer tokens and client secrets encrypted with `ATLAS_SECRET_KEY`; SMTP password only in the environment; sent email bodies deleted from the outbox |
| Concurrency | writers serialised; reviews, reverts and withdrawals cannot double-apply; responses are sent only after commit |
| Audit | admin and moderation actions are logged with the actor |

Two independent code reviews were carried out during development; every confirmed finding was fixed with a regression test (the test suite runs the attack paths, not just the happy paths), and the database-level races are exercised on real PostgreSQL.

## Known limits (be aware)

* **No breached-password check**; passwords are length-checked only.
* **Signing keys** remain in the database. Ed25519 feed keys support signed successor rotation; OIDC RSA-key rotation is not implemented.
* [Logs and metrics](observability.md) are opt-in. Monitoring, alert delivery and production thresholds remain operator responsibilities.
* The SMTP server and the reverse proxy are yours to harden.
* Admin-supplied URLs (peers, OIDC providers) are fetched by the server; only administrators can set them, and private addresses are allowed on purpose (LAN mirrors).
* **Change-email** answers "already used" for taken addresses (rate-limited), so a signed-in user can test whether an address is registered.
* Account **deletion** removes personal data but keeps accepted (CC0) contributions, detached from the person.

## Reporting a problem

Open a private security advisory on the repository, or contact the repository owners directly. Please include steps to reproduce.

## Two-factor sign-in

Account → **Manage two-factor sign-in** lets each user enrol a standard TOTP authenticator
(SHA-1, six digits, 30 seconds). Setup requires the current password, or a linked-provider sign-in
within the last 15 minutes for an account without a password. The setup key is encrypted with
`ATLAS_SECRET_KEY`, is visible only to the initiating session, and expires after ten minutes.
The authenticator must produce a valid code before protection is enabled.

Ten random recovery codes are shown once, stored only as hashes and accepted once each.
Store them separately from the authenticator. Generating new codes invalidates the old set.
Enabling or disabling protection signs out other sessions. Authenticator time steps cannot be
reused, including across concurrent requests. A one-step clock tolerance is allowed.

Enrolled accounts must complete a second factor after either password or OIDC sign-in.
Primary authentication alone cannot access account exports, token creation, contributions,
OIDC consent or administration. An incomplete sign-in expires after ten minutes; successful
verification rotates the session and CSRF token. Verification is rate-limited by account and IP.
Password reset and email verification never disable MFA.

Administrators can enable **Require two-factor sign-in for reviewers and administrators** under
Admin → Settings → Security (`ATLAS_REQUIRE_MFA_FOR_PRIVILEGED=true` sets the default).
This includes reputation-based reviewers. Configure and preserve `ATLAS_SECRET_KEY`, enrol your
own authenticator and save recovery codes before requiring MFA. Accounts needing enrolment can
only set up MFA or sign out. Existing sessions are checked against the current policy.

There is deliberately no email-only or administrator-click bypass. If a user loses both their
authenticator and recovery codes, an operator must independently verify their identity before
any manual database recovery. Preserve an encrypted backup of the database **and** the external
encryption key; changing that key makes stored authenticators unreadable. A missing/wrong key
fails closed and is reported as a configuration problem. Downgrading past the MFA migration is
refused while accounts remain enrolled.

## Rotate a node signing key

First upgrade **every follower and relay** to software supporting successor proofs. Older
followers cannot verify events signed by the new key. Take and verify a private backup.
Under **Admin → Signing keys**, compare the current signer fingerprint, confirm your password
(or a fresh linked-provider sign-in), check the follower-upgrade acknowledgement and submit.
The form is CSRF-protected, rate-limited and subject to the same MFA policy as administration.

Rotation appends a transition signed by the old key, then atomically switches the private
signer. The original node ID and public trust root stay stable. Followers verify each
transition from that independently pinned root, enforce the key appropriate for every
sequence and remember the verified prefix, refusing rewind, tampering or re-use of retired
keys. Relays retain original signatures and proofs. Stale workers load the current signer
under the feed lock. The rotation limit is 128 transitions per root.

Take a new verified backup after rotation. Never restore a stale archive as a live second
writer: it would bring back an old signer/feed head. A missing signing key fails startup
rather than silently replacing the identity. Restore a verified current backup or use the
[fresh-identity recovery runbook](recovery.md).

This is routine key hygiene, not a complete response to a compromised trust root. Someone
holding an old root key can forge an alternative history for a new follower that has no
remembered prefix. Use an independently published new root and explicitly retire the old pin
after compromise. Rotation does not rotate OIDC keys, API tokens or `ATLAS_SECRET_KEY`.
