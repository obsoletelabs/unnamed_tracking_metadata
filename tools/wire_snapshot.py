"""Wire golden vectors and fingerprint (CONTRACT-C16 design §9.2; amendments §22.8). Renders, from the running
code, one vector per schema id (the feed registry: every kind's shape, `SINCE`, the reserved sets, the envelope
and the critical-member registry; the wire header grammar and the v0 tombstone) and a sha256 per schema id, so a
PR that changes any of them is visible as a diff in `contracts/wire/v1/schema.json` and
`contracts/wire/v1/fingerprint.json`, not just in the code that computes them.

    python -m tools.wire_snapshot generate   # rewrite the committed vectors and fingerprint from the code
    python -m tools.wire_snapshot check      # fail (and print the stale schema ids) when they disagree (G-W1)

Only wire 1 exists, so there is one vector set, not a pair; `tools/release_gates.py` reads this module's
`compute()` for G-W1 and G-W5. G-W2 (an adapter or a `contracts/wire/CHANGES.md` declaration for a changed schema
id), G-W3 (no further schema change once `WIRE_MAX` is released) and G-W4 (a released `contracts/wire/v{n}/` is
immutable) need a second wire version to mean anything -- nothing has shipped past wire 1 yet, so they are not
built (recorded in CONTRACT-AMENDMENTS.md, not silently dropped). G-W7 (`WIRE_MIN` only rises with a
`released.json` entry) is checked against `contracts/wire/released.json`, which starts empty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DIR = "contracts/wire/v1"
SCHEMA_FILE = f"{DIR}/schema.json"
FINGERPRINT_FILE = f"{DIR}/fingerprint.json"
RELEASED_FILE = "contracts/wire/released.json"
CHANGES_FILE = "contracts/wire/CHANGES.md"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=list)


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def compute_schemas() -> dict[str, Any]:
    """One vector per schema id, read from the modules that are the source of truth (never hand duplicated)."""
    sys.path.insert(0, str(ROOT))
    from atlas import feed_schema, wire

    out: dict[str, Any] = {}
    for name, spec in sorted(feed_schema.FEED_KINDS.items()):
        out[f"feed.kind.{name}"] = {
            "since": spec.since, "emit": spec.emit, "apply": spec.apply, "fields": sorted(spec.fields),
            "nested": {k: sorted(v) for k, v in sorted(spec.nested.items())},
            "values": dict(sorted(spec.values.items())), "optional": sorted(spec.optional)}
    out["feed.since"] = {f"{k[0]}:{k[1]!r}": v for k, v in sorted(feed_schema.SINCE.items(), key=lambda kv: str(kv[0]))}
    out["feed.reserved"] = {
        "kinds": sorted(feed_schema.RESERVED_KINDS), "tokens": sorted(feed_schema.RESERVED_TOKENS),
        "values": sorted(f"{k}:{v}" for k, v in feed_schema.RESERVED_VALUES),
        "bodies": sorted(f"{k}:{v}" for k, v in feed_schema.RESERVED_BODIES)}
    out["feed.envelope"] = list(feed_schema.ENVELOPE)
    out["feed.critical_members"] = {f"{k[0]}:{k[1]}": v for k, v in sorted(feed_schema.CRITICAL_MEMBERS.items())}
    out["feed.capabilities"] = sorted(feed_schema.CAPABILITIES)
    out["wire.header"] = {"header": wire.HEADER, "legacy_header": wire.LEGACY_HEADER, "min": wire.WIRE_MIN,
                          "max": wire.WIRE_MAX, "min_peer": wire.WIRE_MIN_PEER, "reasons": list(wire.REASONS),
                          "actions": list(wire.ACTIONS)}
    out["wire.tombstone"] = dict(wire.V0_TOMBSTONE)
    return out


def fingerprint(schemas: dict[str, Any]) -> dict[str, str]:
    return {name: digest(value) for name, value in schemas.items()}


def _render(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def cmd_generate(root: Path) -> int:
    schemas = compute_schemas()
    target = root / SCHEMA_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(schemas), encoding="utf-8", newline="\n")
    (root / FINGERPRINT_FILE).write_text(_render(fingerprint(schemas)), encoding="utf-8", newline="\n")
    released = root / RELEASED_FILE
    if not released.exists():
        released.parent.mkdir(parents=True, exist_ok=True)
        released.write_text(_render([]), encoding="utf-8", newline="\n")
    changes = root / CHANGES_FILE
    if not changes.exists():
        changes.write_text("# Wire changes\n\n## Unreleased\n\n(nothing declared)\n", encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_FILE} and {FINGERPRINT_FILE} ({len(schemas)} schema ids)")
    return 0


def cmd_check(root: Path) -> int:
    schemas = compute_schemas()
    want_schema, want_fp = _render(schemas), _render(fingerprint(schemas))
    problems = []
    try:
        have_schema = (root / SCHEMA_FILE).read_text(encoding="utf-8")
        have_fp = (root / FINGERPRINT_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read the committed wire snapshot: {exc}; run: python -m tools.wire_snapshot generate")
        return 1
    if have_schema.replace("\r\n", "\n") != want_schema:
        problems.append(f"{SCHEMA_FILE} does not match the running code")
    if have_fp.replace("\r\n", "\n") != want_fp:
        have_map, want_map = json.loads(have_fp), fingerprint(schemas)
        stale = sorted(set(have_map) ^ set(want_map)) or \
            sorted(k for k in want_map if have_map.get(k) != want_map[k])
        problems.append(f"{FINGERPRINT_FILE} is stale for: {', '.join(stale[:20])}")
    for p in problems:
        print(p)
    if problems:
        print("run: python -m tools.wire_snapshot generate")
        return 1
    print(f"wire snapshot matches the running code ({len(schemas)} schema ids)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wire_snapshot", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "check"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    return cmd_generate(args.root) if args.command == "generate" else cmd_check(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
