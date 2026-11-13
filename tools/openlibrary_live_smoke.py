"""Manual live smoke check of the Open Library adapter (CONTRACT-C15 §3.4). Never run in CI on a push or a pull
request: the `Live Open Library` workflow runs it on manual dispatch only, and you can run it by hand.

    python -m tools.openlibrary_live_smoke [--contact you@example.org] [--json out.json]

About six requests, one a second (the adapter's own pacing), with the identifying User-Agent: one title search, one
ISBN lookup (a redirect to its edition), and one work fetch (the work, its author list and its editions). It asserts
the shapes the adapter reads and prints counts and ids only; no description, title list or other Open Library data is
written anywhere (D15.2). Exit 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

#: a long-lived, well-known work and one of its ISBNs (ids only; nothing else is kept)
WORK, ISBN, QUERY = "OL893414W", "9780441013593", "dune herbert"


def run(contact: str | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    from atlas.providers.base import user_agent
    from atlas.providers.openlibrary import OpenLibrary

    p = OpenLibrary(contact=contact)
    own = client is None
    client = client or httpx.Client(timeout=20)
    report: dict[str, Any] = {"user_agent": user_agent(contact), "checks": {}}
    checks = report["checks"]
    try:
        found = p.search(client, QUERY)
        checks["search"] = {"ok": any(r.external_id == WORK for r in found), "results": len(found),
                            "first_has_author": bool(found and found[0].facts.get("developer"))}
        by_isbn = p.fetch(client, ISBN)
        checks["isbn"] = {"ok": by_isbn is not None and by_isbn.external_id == WORK,
                          "work": by_isbn.external_id if by_isbn else None}
        got = by_isbn
        book = (got.book or {}) if got else {}
        editions = book.get("editions") or []
        checks["work"] = {
            "ok": bool(got and got.title and book.get("authors") and editions
                       and any(e.get("isbn_13") == ISBN for e in editions) and got.release),
            "authors": len(book.get("authors") or []), "editions": len(editions),
            "editions_with_isbn": sum(1 for e in editions if e.get("isbn_13")),
            "release_precision": ((got.release or {}).get("first") or {}).get("precision") if got else None,
            "cover": bool(got and got.artwork), "subjects": len(got.facts.get("features") or []) if got else 0,
        }
    finally:
        if own:
            client.close()
    report["ok"] = all(c["ok"] for c in checks.values())
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--contact", default=None, help="provider_contact for the User-Agent (e-mail or https URL)")
    ap.add_argument("--json", type=Path, default=None, help="also write the report (counts and ids only)")
    args = ap.parse_args(argv)
    if args.contact:
        from atlas.settings import check_contact

        check_contact(args.contact)
    report = run(args.contact)
    text = json.dumps(report, indent=1, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
