"""Write the Open Library adapter fixtures (CONTRACT-C15 §3.4) into `tests/fixtures/openlibrary/`.

The fixtures are **synthetic**: invented works, authors, ids and ISBNs laid out in the response shapes Open Library's
JSON API uses (search, work, editions, edition, ISBN redirect, work redirect, 429). Open Library observations are not
redistributable here (D15.2) and its descriptions can be publisher text, so no real response is ever committed.

    python -m tools.record_openlibrary_fixtures            # rewrite the fixtures from the catalogue below
    python -m tools.record_openlibrary_fixtures --check    # exit 1 when the committed files differ (no writes)
    python -m tools.record_openlibrary_fixtures --compare-live [--contact you@example.org]
        # manual only: fetch five live answers and report which keys the adapter reads are missing from them or
        # from the synthetic files. Prints key names only; nothing live is written. Never run in CI.

`tests/test_a22_openlibrary.py` fails when the committed files drift from what this script writes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "openlibrary"
API = "https://openlibrary.org"


def _isbn13(body12: str) -> str:
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def _isbn10(body9: str) -> str:
    total = sum(int(c) * (10 - i) for i, c in enumerate(body9))
    check = (11 - total % 11) % 11
    return body9 + ("X" if check == 10 else str(check))


# Invented ISBNs with correct check digits (978-0-00-…: no title is meant).
ISBN_PAPERBACK = _isbn13("978000990011")
ISBN10_HARDCOVER = _isbn10("009900120")
ISBN_OLD = _isbn13("978000990015")
OTHER_ISBN10 = _isbn10("123456789")
BAD_ISBN = ISBN_PAPERBACK[:-1] + str((int(ISBN_PAPERBACK[-1]) + 1) % 10)  # wrong check digit

W1, W2, W3, W4, W5 = (f"OL990000000{n}W" for n in range(1, 6))


def _work(key: str, title: str, authors: list[str], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "key": f"/works/{key}", "title": title, "type": {"key": "/type/work"},
        "authors": [{"author": {"key": f"/authors/{a}"}, "type": {"key": "/type/author_role"}} for a in authors],
        "latest_revision": 3, "revision": 3,
        "created": {"type": "/type/datetime", "value": "2024-01-02T03:04:05.000000"},
        "last_modified": {"type": "/type/datetime", "value": "2025-06-07T08:09:10.000000"},
    }
    body.update(extra)
    return body


def _edition(key: str, work: str, title: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"key": f"/books/{key}", "title": title, "type": {"key": "/type/edition"},
                            "works": [{"key": f"/works/{work}"}], "latest_revision": 2, "revision": 2}
    body.update(extra)
    return body


def _lang(code: str) -> list[dict[str, str]]:
    return [{"key": f"/languages/{code}"}]


def _doc(key: str, title: str, authors: list[tuple[str, str]], **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {"key": f"/works/{key}", "title": title,
                           "author_name": [a[0] for a in authors], "author_key": [a[1] for a in authors]}
    doc.update(extra)
    return doc


def _search(docs: list[dict[str, Any]], q: str) -> dict[str, Any]:
    return {"numFound": len(docs), "start": 0, "numFoundExact": True, "num_found": len(docs), "q": q,
            "offset": None, "docs": docs}


SALT_AUTHORS = [("Ione Marsh", "OL9900000001A"), ("Pell Arden", "OL9900000002A")]
MANY = [(f"Contributor Number {n:02d}", f"OL99000001{n:02d}A") for n in range(1, 32)]  # 31: one over the cap

SALT_DOC = _doc(W1, "The Salt Cartographer", SALT_AUTHORS, subtitle="Maps of a Drowned Coast",
                first_publish_year=1987, cover_i=9900001, edition_count=5, language=["eng", "fre"])
LEDGER_DOC = _doc(W3, "An Undated Ledger", [("Oswin Fell", "OL9900000003A")], edition_count=0)
WEATHER_DOC = _doc(W4, "Weather of Small Towns", [("Tamsin Rowe", "OL9900000004A")], first_publish_year=1972,
                   cover_i=9900004, edition_count=2, language=["eng"])
VOICES_DOC = _doc(W5, "Collected Voices", MANY, first_publish_year=2003, edition_count=1, language=["eng"])

EDITIONS_W1 = [
    _edition("OL9900000011M", W1, "The Salt Cartographer", physical_format="Mass Market Paperback",
             isbn_13=[ISBN_PAPERBACK], isbn_10=[OTHER_ISBN10],  # a valid ISBN-10 of another book: dropped
             publish_date="Aug 01, 2005", number_of_pages=412, languages=_lang("eng"), publishers=["Gullwing Press"],
             covers=[9900001]),
    _edition("OL9900000012M", W1, "The Salt Cartographer", physical_format="Hardcover", isbn_10=[ISBN10_HARDCOVER],
             publish_date="1987", number_of_pages=398, languages=_lang("eng"), publishers=["Harrow & Vane"],
             edition_name="First edition"),
    _edition("OL9900000013M", W1, "The Salt Cartographer", physical_format="Audio CD", isbn_13=[BAD_ISBN],
             publish_date="2019-05", languages=_lang("eng"), publishers=["Gullwing Audio"]),
    _edition("OL9900000014M", W1, "La Cartographe du sel", physical_format="E-book", publish_date="c2011",
             number_of_pages=0, languages=_lang("fre"), publishers=["Éditions du Phare"]),
    _edition("OL9900000015M", W1, "The Salt Cartographer", physical_format="paperback", isbn_13=[ISBN_OLD],
             publish_date="1948", number_of_pages=380, languages=_lang("eng"), publishers=["Gullwing Press"]),
]
EDITIONS_W4 = [
    _edition("OL9900000041M", W4, "Weather of Small Towns", physical_format="Paperback", publish_date="Spring 1999",
             languages=_lang("eng"), publishers=["Lowland Books"]),
    _edition("OL9900000042M", W4, "Weather of Small Towns", physical_format="Large print", publish_date="1/2/1999",
             languages=_lang("eng"), publishers=["Lowland Books"]),
]


def _editions(work: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"links": {"self": f"/works/{work}/editions.json", "work": f"/works/{work}"}, "size": len(entries),
            "entries": entries}


def build() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(files, routes): every fixture file by name, and how the fake transport answers each request."""
    files: dict[str, Any] = {
        "search_salt_cartographer.json": _search([SALT_DOC, LEDGER_DOC], "salt cartographer"),
        "search_empty.json": _search([], "no such book anywhere"),
        "http_429.json": {"error": "Too Many Requests", "note": "synthetic 429 answer; the route sends Retry-After"},
        f"search_key_{W1}.json": _search([SALT_DOC], f'key:"/works/{W1}"'),
        f"search_key_{W3}.json": _search([LEDGER_DOC], f'key:"/works/{W3}"'),
        f"search_key_{W4}.json": _search([WEATHER_DOC], f'key:"/works/{W4}"'),
        f"search_key_{W5}.json": _search([VOICES_DOC], f'key:"/works/{W5}"'),
        f"work_{W1}.json": _work(W1, "The Salt Cartographer", ["OL9900000001A", "OL9900000002A"],
                                 subtitle="Maps of a Drowned Coast", first_publish_date="March 1987",
                                 description={"type": "/type/text",
                                              "value": "An invented novel about a surveyor charting a coast "
                                                       "that the sea takes back each winter."},
                                 subjects=["Maps", "Coasts", "Fiction", "Surveyors"], covers=[9900001, 9900002]),
        "work_redirect.json": {"key": f"/works/{W2}", "type": {"key": "/type/redirect"}, "location": f"/works/{W1}",
                               "latest_revision": 2, "revision": 2},
        "work_no_dates.json": _work(W3, "An Undated Ledger", ["OL9900000003A"], description="Plain text description."),
        "work_free_text_dates.json": _work(W4, "Weather of Small Towns", ["OL9900000004A"], first_publish_date="c1972",
                                           subjects=["Weather"]),
        "work_many_authors.json": _work(W5, "Collected Voices", [a[1] for a in MANY], first_publish_date="2003"),
        f"editions_{W1}.json": _editions(W1, EDITIONS_W1),
        "editions_empty.json": _editions(W3, []),
        f"editions_{W4}.json": _editions(W4, EDITIONS_W4),
        f"isbn_{ISBN_PAPERBACK}.json": EDITIONS_W1[0],
        "edition_isbn10_only.json": EDITIONS_W1[1],
        "edition_bad_isbn.json": EDITIONS_W1[2],
    }
    routes: list[dict[str, Any]] = [
        {"path": "/search.json", "q": "salt cartographer", "file": "search_salt_cartographer.json"},
        {"path": "/search.json", "q": "no such book anywhere", "file": "search_empty.json"},
        {"path": "/search.json", "q": "busy", "status": 429, "headers": {"Retry-After": "30"}, "file": "http_429.json"},
        *({"path": "/search.json", "q": f'key:"/works/{w}"', "file": f"search_key_{w}.json"} for w in (W1, W3, W4, W5)),
        {"path": f"/works/{W1}.json", "file": f"work_{W1}.json"},
        {"path": f"/works/{W2}.json", "file": "work_redirect.json"},
        {"path": f"/works/{W3}.json", "file": "work_no_dates.json"},
        {"path": f"/works/{W4}.json", "file": "work_free_text_dates.json"},
        {"path": f"/works/{W5}.json", "file": "work_many_authors.json"},
        {"path": f"/works/{W1}/editions.json", "file": f"editions_{W1}.json"},
        {"path": f"/works/{W3}/editions.json", "file": "editions_empty.json"},
        {"path": f"/works/{W4}/editions.json", "file": f"editions_{W4}.json"},
        {"path": f"/isbn/{ISBN_PAPERBACK}.json", "status": 302,
         "headers": {"Location": f"{API}/books/OL9900000011M.json"}},
        {"path": "/books/OL9900000011M.json", "file": f"isbn_{ISBN_PAPERBACK}.json"},
        {"path": "/books/OL9900000012M.json", "file": "edition_isbn10_only.json"},
        {"path": "/books/OL9900000013M.json", "file": "edition_bad_isbn.json"},
    ]
    files["routes.json"] = {"note": "how tests/test_a22_openlibrary.py's fake transport answers; written by "
                                    "tools/record_openlibrary_fixtures.py", "isbn_paperback": ISBN_PAPERBACK,
                            "isbn10_hardcover": ISBN10_HARDCOVER, "isbn_old": ISBN_OLD, "bad_isbn": BAD_ISBN,
                            "routes": routes}
    return files, routes


def render(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def write() -> list[str]:
    files, _ = build()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, obj in files.items():
        (OUT / name).write_bytes(render(obj).encode())
    return sorted(files)


def drift() -> list[str]:
    """Names of fixture files that differ from (or are missing against) what `write` produces."""
    files, _ = build()
    names = {p.name for p in OUT.glob("*.json")}
    bad = sorted(names ^ set(files))
    for name, obj in files.items():
        path = OUT / name
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != obj:
            bad.append(name)
    return sorted(set(bad))


#: keys the adapter reads, per answer kind (the live comparison reports these names only)
READS = {
    "search": {"docs"},
    "search_doc": {"key", "title", "author_name", "author_key", "first_publish_year", "cover_i"},
    "work": {"key", "title", "type", "description", "subjects", "covers", "first_publish_date", "authors"},
    "editions": {"entries", "size"},
    "edition": {"key", "works", "isbn_13", "isbn_10", "physical_format", "publish_date", "number_of_pages",
                "languages", "publishers"},
}


def compare_live(contact: str | None) -> int:  # pragma: no cover - manual, needs the network
    import httpx

    from atlas.providers.base import user_agent

    files, _ = build()
    synthetic = {"search": files["search_salt_cartographer.json"],
                 "search_doc": files["search_salt_cartographer.json"]["docs"][0],
                 "work": files[f"work_{W1}.json"], "editions": files[f"editions_{W1}.json"],
                 "edition": files[f"isbn_{ISBN_PAPERBACK}.json"]}
    headers = {"User-Agent": user_agent(contact)}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=False) as client:
        def get(url: str, **params: Any) -> Any:
            time.sleep(1.1)  # Open Library's unidentified rate is one request a second
            resp = client.get(url, params=params or None)
            return resp
        live: dict[str, Any] = {}
        live["search"] = get(f"{API}/search.json", q="the hobbit", limit=1,
                             fields="key,title,author_name,author_key,first_publish_year,cover_i").json()
        live["search_doc"] = (live["search"].get("docs") or [{}])[0]
        work = str(live["search_doc"].get("key", "/works/OL0W")).rsplit("/", 1)[-1]
        live["work"] = get(f"{API}/works/{work}.json").json()
        live["editions"] = get(f"{API}/works/{work}/editions.json", limit=5).json()
        redirect = get(f"{API}/isbn/9780261103344.json")
        print(f"isbn lookup: HTTP {redirect.status_code}, redirects to "
              f"{httpx.URL(redirect.headers.get('location', '')).host or '-'}")
        entries = live["editions"].get("entries") or [{}]
        live["edition"] = next((e for e in entries if e.get("isbn_13") or e.get("isbn_10")), entries[0])
    required = {"search": {"docs"}, "search_doc": {"key", "title"}, "work": {"key", "title", "type"},
                "editions": {"entries"}, "edition": {"key", "works"}}
    problems = 0
    for kind, keys in READS.items():
        missing_live = sorted(keys - set(live.get(kind) or {}))
        missing_syn = sorted(keys - set(synthetic[kind]))
        print(f"{kind}: live lacks {missing_live or 'nothing'}; synthetic lacks {missing_syn or 'nothing'}")
        problems += bool(required[kind] - set(live.get(kind) or {}))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    ap.add_argument("--compare-live", action="store_true", help="manual: compare key names with live answers")
    ap.add_argument("--contact", default=None, help="contact for the User-Agent (e-mail or https URL)")
    args = ap.parse_args(argv)
    if args.compare_live:
        return compare_live(args.contact)
    if args.check:
        bad = drift()
        for name in bad:
            print(f"drift: {name}")
        return 1 if bad else 0
    for name in write():
        print(f"wrote tests/fixtures/openlibrary/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
