# Atlas v2 — design overview

Status: draft (Phase 0). Source of truth for decisions before code lands.

## Goals
1. Merge data from many providers into one canonical game record, with provenance for every field.
2. Work seamlessly with the unnamed tracking app (plugin first, built-in later).
3. Semi-distributed: authority, mirror and local node roles from one codebase; local nodes cache third-party providers and keep serving when upstream is down.

## Core model
- **Observation** — immutable, provenance-stamped claim from one source about one record. Wire format is the tracking app's `SourceObservationV1` (see `observation-contract.md`) with `source_kind: "metadata"` for server-originated data.
- **Canonical** — derived view over observations. ID `atlas:<ulid>`. Never edited directly; edits are new observations or locks.
- **Crosswalk** — `(namespace, external_id) -> canonical_id`. Merges/splits leave redirects so client IDs never break.
- **Merge strategy** — per-field rule (priority, newest, union, vote, longest, locked) stored as data. Every winning value records its observation id (`explain`).
- **Change feed** — append-only, per-node monotonic sequence, Ed25519-signed entries; pull with `GET /api/v2/sync/changes?since=<seq>`.

## Roles
| Role | Store | Upstream | Writes |
|------|-------|----------|--------|
| authority | Postgres | none | review queue, publishes feed |
| mirror | Postgres/SQLite | authority feed | provider cache only |
| local | SQLite | optional mirror/authority | local overlays + provider cache |

## Federation rules
- Only IDs/crosswalk and user-contributed fields federate. Provider-derived values stay in the node that fetched them (provider ToS), unless the provider license allows redistribution.
- Authority wins for canonical fields; local overlays never leave the local node.
- Outage = keep last known data; never clear by omission (same semantics as the app contract).

## Decisions pending (blockers for later phases)
- Who may run authorities (single vs trust list).
- Contribution data license (CC0 vs CC-BY-SA).
- Fresh repo vs this branch (currently: branch `v2`, port selectively).

## Phases
See the approved plan; Phase 1 starts with package layout, typed settings, Alembic and DB/Redis-backed state.
