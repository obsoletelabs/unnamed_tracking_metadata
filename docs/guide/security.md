# Security

## What is protected, and how

| Area | Controls |
| --- | --- |
| Accounts | scrypt password hashing; server-side sessions stored hashed; `HttpOnly`, `SameSite=Lax` (and `Secure` over HTTPS) cookies; per-address and per-account throttling on sign-in, registration, reset and verification; identical answers for known and unknown accounts |
| Forms | a CSRF token on every state-changing form; strict Content-Security-Policy with **no inline script or style**; clickjacking and sniffing headers; user text is always escaped; Markdown pages cannot contain HTML |
| API | tokens stored only as hashes, scoped, expiring, revocable, fail-closed; personal tokens are read-only and die with the account; rate limits; body size caps; bounded batches |
| Trust model | reserved source names need `curate`; uncurated claims can match but cannot register identifiers in someone else's namespace; far-future timestamps ignored; personal facts discarded on arrival |
| Artwork | downloads only from public addresses (pinned address, every redirect re-checked, tunnelled IPv6 refused, environment proxies ignored), size and pixel limits, full image decode, content-hash storage, served with `nosniff` and a restrictive CSP |
| Federation | Ed25519-signed feed (sequence, kind, content), pinned trust, gap/rewind/tamper rejection, provider data never republished |
| OIDC | exact redirect match, consent, PKCE, single-use burned codes, pinned issuer keys, algorithm never negotiated, email never used to take over an account |
| Secrets at rest | peer tokens and client secrets encrypted with `ATLAS_SECRET_KEY`; SMTP password only in the environment; sent email bodies deleted from the outbox |
| Concurrency | writers serialised; reviews, reverts and withdrawals cannot double-apply; responses are sent only after commit |
| Audit | admin and moderation actions are logged with the actor |

Two independent code reviews were carried out during development; every confirmed finding was fixed with a regression test (the test suite runs the attack paths, not just the happy paths), and the database-level races are exercised on real PostgreSQL.

## Known limits (be aware)

* **No two-factor authentication** yet.
* **No breached-password check**; passwords are length-checked only.
* **Signing keys** (the node's Ed25519 identity and the OIDC RSA key) are stored in the database; there is no in-app rotation yet.
* Observability (structured logs, metrics, alerting) is minimal.
* The SMTP server and the reverse proxy are yours to harden.
* Admin-supplied URLs (peers, OIDC providers) are fetched by the server; only administrators can set them, and private addresses are allowed on purpose (LAN mirrors).
* Account **deletion** removes personal data but keeps accepted (CC0) contributions, detached from the person.

## Reporting a problem

Open a private security advisory on the repository, or contact the repository owners directly. Please include steps to reproduce.
