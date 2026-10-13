# Atlas metadata catalogue — mockup 1

This branch contains a **static UI concept** for the planned central game metadata and achievement catalogue.

## Preview

Open `index.html` directly in a browser, or enable GitHub Pages for the `mockup-1` branch to host the prototype. No build tooling is required.

## Included screens

- Catalogue landing page with sample metrics, search and filters
- Game detail pages with canonical-ID and external-identifier examples
- Shared achievement-definition examples
- Recent-change and incremental-sync concept
- Strict beta API access / client quota concept
- Contribution form and human-review workflow concept
- Responsive desktop, tablet and mobile layouts

## Explicitly a mockup

All catalogue records, identifiers, statistics, quotas and activity are fictional sample content for exploring the product experience. Forms only show local feedback; there is no backend, account system, API, key issuance, persistence, moderation queue or real rate limiting.

## Design direction

The concept assumes:
- Public website browsing, but controlled programmatic access through managed beta keys.
- Stable canonical IDs and explicit external identifier mappings.
- Achievement definitions as first-class catalogue records, distinct from a user's unlock state.
- Contributions as proposals that are reviewed before publication.
- Revision-based incremental sync and an eventual ability to serve read traffic from multiple regions behind a stable API domain.

This is a disposable starting point for discussion, not a commitment to the final stack or visual identity.