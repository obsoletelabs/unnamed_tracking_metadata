"""Executable release gates (CONTRACT-C16 §10.1, review #1 R1-9).

Plain Python, so the gates behave the same on Windows, Linux CI and macOS: files come from `git ls-files -z` (tracked
files only, deterministic), patterns are Python `re` (one dialect), every gate prints `GATE path:line: text` for each
hit and the run exits non-zero when any gate fails. `tests/test_release_gates.py` plants a hit for every gate in a
throwaway repository, so a gate that cannot fail fails the build.

    python tools/release_gates.py            # run every gate over the repository at the working directory
    python tools/release_gates.py --baseline # print the hit counts and exit 0 (an inventory, never a verdict)

Gates here:

* G-U1  no tracked build output: `build/` and `*.egg-info/` describe an older tree than the source and have shipped a
        stale `atlas/` before. They are untracked and ignored.
* G-W6  `Atlas-Protocol` (the v0 protocol header) is read in `atlas/wire.py` and by the one route dependency that
        names a refused v0 caller (`alias=`); a wire build never *sends* it, and no module writes it as a header key.
* G-W1  the committed wire snapshot (`contracts/wire/v1/schema.json` and `fingerprint.json`,
        `tools/wire_snapshot.py`) matches what the running code computes.
* G-W5  every schema id the running code computes has a golden vector in the committed snapshot (and vice versa):
        `tools/wire_snapshot.py generate` was run after the registry changed, not only after the fingerprint.
* G-W7  `WIRE_MIN` (`atlas/wire.py`) only differs from the last entry in `contracts/wire/released.json` when that
        entry (or a newer one) carries a non-empty `min_raised` note.
* G-C1  every member `atlas/feed_schema.py` marks critical (`CRITICAL_MEMBERS`) names a token that is itself
        registered (`SINCE[("token", name)]` and in `CAPABILITIES`): a critical mark can never ship without the
        `_requires` plumbing an old reader needs to park on it correctly.

G-W2 (an adapter or a `contracts/wire/CHANGES.md` declaration for a schema id that changed since the merge base),
G-W3 (no further schema change once `WIRE_MAX` equals the last released wire) and G-W4 (a released
`contracts/wire/v{n}/` is immutable) need a second wire version to mean anything: wire 1 is the only one that
exists, so they are not built yet (recorded in CONTRACT-AMENDMENTS.md, not silently dropped).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Never scanned by a regex gate (generated or historical text).
EXCLUDE = ("atlas/migrations/", "docs/pr-evidence/", "build/", "atlas_metadata.egg-info/")


@dataclass(frozen=True)
class Gate:
    id: str
    pattern: str = ""
    paths: tuple[str, ...] = (".",)
    note: str = ""
    #: `(path, text)`: a hit in `path` whose line contains `text` is allowed. An entry that allows nothing is stale.
    allow: tuple[tuple[str, str], ...] = ()
    #: A function gate: `(tracked paths) -> list[(path, line, text)]`, instead of a regex over file contents.
    check: str = ""
    skip: tuple[str, ...] = field(default_factory=tuple)


GATES: tuple[Gate, ...] = (
    Gate("G-U1", check="tracked_build_output",
         note="build output is tracked: `git rm -r --cached build '*.egg-info'` and keep them in .gitignore"),
    Gate("G-W6", r"""["']Atlas-Protocol["']\s*:|\bLEGACY_HEADER\b\s*[\]:=]|\bheaders\s*\[\s*["']Atlas-Protocol["']""",
         paths=("atlas",), skip=("atlas/wire.py",),
         note="a wire build never sends Atlas-Protocol; only atlas/wire.py defines it and one route dependency reads it"),
    Gate("G-W1", check="wire_fingerprint_matches",
         note="the wire snapshot is stale: run `python -m tools.wire_snapshot generate`"),
    Gate("G-W5", check="wire_vectors_present",
         note="a schema id is missing its golden vector or vice versa: run `python -m tools.wire_snapshot generate`"),
    Gate("G-W7", check="wire_min_change_announced",
         note="WIRE_MIN changed with no released.json entry carrying a non-empty min_raised note"),
    Gate("G-C1", check="critical_members_are_registered",
         note="a feed_schema.CRITICAL_MEMBERS token is not registered in SINCE/CAPABILITIES"),
)


def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def tracked_build_output(root: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    return [(p, 0, "tracked build output") for p in paths if p.startswith("build/") or ".egg-info/" in p
            or p.endswith(".egg-info")]


def _is_atlas_tree(root: Path) -> bool:
    """The wire/critical-member gates below read `atlas/feed_schema.py`'s own registry: meaningless (and not
    importable) outside the real Atlas tree. A throwaway test repository (or another project reusing this same
    gate runner) has no such file, so these gates say nothing about it rather than failing to import it."""
    return (root / "atlas" / "feed_schema.py").is_file()


def _wire_snapshot(root: Path):
    """`tools.wire_snapshot`: always this process's own module (there is one `atlas`/`tools` per process), so its
    `compute_schemas()` reflects whatever `atlas.feed_schema`/`atlas.wire` hold right now, monkeypatches included.
    `root` only ever picks which committed snapshot files the caller compares that against."""
    import tools.wire_snapshot as mod

    return mod


def wire_fingerprint_matches(root: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    """G-W1: the committed schema vectors and fingerprint equal what the running registry computes now."""
    if not _is_atlas_tree(root):
        return []
    snap = _wire_snapshot(root)
    schemas = snap.compute_schemas()
    want_schema, want_fp = snap._render(schemas), snap._render(snap.fingerprint(schemas))
    hits = []
    try:
        have_schema = (root / snap.SCHEMA_FILE).read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        have_schema = None
    if have_schema != want_schema:
        hits.append((snap.SCHEMA_FILE, 0, "does not match the running code: run `python -m tools.wire_snapshot "
                                          "generate`"))
    try:
        have_fp = (root / snap.FINGERPRINT_FILE).read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        have_fp = None
    if have_fp != want_fp:
        hits.append((snap.FINGERPRINT_FILE, 0, "is stale: run `python -m tools.wire_snapshot generate`"))
    return hits


def wire_vectors_present(root: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    """G-W5: `schema.json` and `fingerprint.json` name exactly the same schema ids (catches a hand edit, or one
    file regenerated without the other, even when G-W1 is not run)."""
    if not _is_atlas_tree(root):
        return []
    snap = _wire_snapshot(root)
    try:
        schema = json.loads((root / snap.SCHEMA_FILE).read_text(encoding="utf-8"))
        fp = json.loads((root / snap.FINGERPRINT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [(snap.FINGERPRINT_FILE, 0, "missing or unreadable: run `python -m tools.wire_snapshot generate`")]
    missing = sorted(set(schema) - set(fp))
    extra = sorted(set(fp) - set(schema))
    hits = [(snap.FINGERPRINT_FILE, 0, f"no fingerprint for schema id {sid}") for sid in missing]
    hits += [(snap.SCHEMA_FILE, 0, f"fingerprint names {sid}, which has no golden vector") for sid in extra]
    return hits


def wire_min_change_announced(root: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    """G-W7: `atlas.wire.WIRE_MIN` only differs from `contracts/wire/released.json`'s last entry when that entry
    (or a newer one) carries a non-empty `min_raised` note. An empty or absent `released.json` means nothing has
    shipped under this scheme yet, so there is nothing for `WIRE_MIN` to disagree with."""
    if not _is_atlas_tree(root):
        return []
    from atlas import wire

    try:
        released = json.loads((root / "contracts/wire/released.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        released = []
    if not isinstance(released, list) or not released or not isinstance(released[-1], dict):
        return []
    last = released[-1]
    if last.get("min") == wire.WIRE_MIN or str(last.get("min_raised") or "").strip():
        return []
    return [("contracts/wire/released.json", 0,
             f"WIRE_MIN is {wire.WIRE_MIN} but the last released entry ({last.get('min')!r}) has no min_raised "
             "note")]


def critical_members_are_registered(root: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    """G-C1: every `feed_schema.CRITICAL_MEMBERS` token is itself a registered `("token", name)` in `SINCE` and
    in `CAPABILITIES` -- a critical mark can never ship without the `_requires` plumbing an old reader needs to
    park on it correctly."""
    if not _is_atlas_tree(root):
        return []
    from atlas import feed_schema

    hits = []
    for (kind, path), token in sorted(feed_schema.CRITICAL_MEMBERS.items()):
        where = f"{kind}:{path}"
        if ("token", token) not in feed_schema.SINCE:
            hits.append(("atlas/feed_schema.py", 0,
                        f'{where} marks {token!r} critical but SINCE has no ("token", {token!r}) entry'))
        if token not in feed_schema.CAPABILITIES:
            hits.append(("atlas/feed_schema.py", 0, f"{where} marks {token!r} critical but it is not in "
                                                     "CAPABILITIES"))
    return hits


def _in(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(prefix in (".", "") or path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


_CHECKS = {"tracked_build_output": tracked_build_output, "wire_fingerprint_matches": wire_fingerprint_matches,
          "wire_vectors_present": wire_vectors_present, "wire_min_change_announced": wire_min_change_announced,
          "critical_members_are_registered": critical_members_are_registered}


def run_gate(root: Path, gate: Gate, paths: list[str]) -> tuple[list[tuple[str, int, str]], list[str]]:
    """`(hits, stale allowlist entries)`."""
    if gate.check:
        return _CHECKS[gate.check](root, paths), []
    rx = re.compile(gate.pattern)
    hits: list[tuple[str, int, str]] = []
    used: set[tuple[str, str]] = set()
    for path in paths:
        if not _in(path, gate.paths) or path in gate.skip or any(path.startswith(e) for e in EXCLUDE):
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if not rx.search(line):
                continue
            allowed = next(((p, t) for p, t in gate.allow if p == path and t in line), None)
            if allowed:
                used.add(allowed)
            else:
                hits.append((path, number, line.strip()[:160]))
    stale = [f"{p}: {t}" for p, t in gate.allow if (p, t) not in used]
    return hits, stale


def run(root: Path, gates: tuple[Gate, ...] = GATES, *, baseline: bool = False, out=None) -> int:
    out = out or sys.stdout
    paths = tracked(root)
    failed = 0
    for gate in gates:
        hits, stale = run_gate(root, gate, paths)
        if baseline:
            print(f"{gate.id}: {len(hits)} hits, {len(stale)} stale allowlist entries", file=out)
            continue
        for path, number, text in hits:
            print(f"{gate.id} {path}:{number}: {text}", file=out)
        for entry in stale:
            print(f"{gate.id} stale allowlist entry (nothing matches it any more): {entry}", file=out)
        if hits or stale:
            failed += 1
            if gate.note:
                print(f"{gate.id}: {gate.note}", file=out)
    return 0 if baseline or not failed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--baseline", action="store_true", help="print counts and exit 0")
    ap.add_argument("--root", default=".", help="repository root (default: the working directory)")
    args = ap.parse_args(argv)
    return run(Path(args.root).resolve(), baseline=args.baseline)


if __name__ == "__main__":
    sys.exit(main())
