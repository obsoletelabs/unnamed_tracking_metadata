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
"""

from __future__ import annotations

import argparse
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
)


def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def tracked_build_output(paths: list[str]) -> list[tuple[str, int, str]]:
    return [(p, 0, "tracked build output") for p in paths if p.startswith("build/") or ".egg-info/" in p
            or p.endswith(".egg-info")]


def _in(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(prefix in (".", "") or path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def run_gate(root: Path, gate: Gate, paths: list[str]) -> tuple[list[tuple[str, int, str]], list[str]]:
    """`(hits, stale allowlist entries)`."""
    if gate.check:
        return {"tracked_build_output": tracked_build_output}[gate.check](paths), []
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
