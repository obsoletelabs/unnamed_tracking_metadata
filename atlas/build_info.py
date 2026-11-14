"""The build manifest (CONTRACT-C16 §10.4, amended §22.8; Z2): what a running build actually implements, derived
from the code that is actually loaded, not just its version string. "2.0.0" alone never proves a build implements
2.0: a stale tracked `build/lib` copy or a partial install can ship older code under a newer version string.

`compute()` is the source of truth, read from the feed registry and the wire profile at call time (never hand
maintained). The wheel build writes a snapshot, `atlas/_build.json` (`tools/write_build_manifest.py`, run by
`package.yml` after `uv build`); a source checkout has no such file and simply computes the manifest at import, so
there is nothing to disagree with. `check()` compares the two and names both values on a mismatch: a different
`version`, a different `wire` range, or a capability set that does not match the running code (`atlas selfcheck`,
and every app start before the sync thread runs, §10.4). `GET /api/v2/health` exposes `build: {version, commit,
capabilities}` next to `wire` (already there); `/sync/info` keeps its own `capabilities` list (the feed's, not the
build's) and adds nothing here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .wire import WIRE_MAX, WIRE_MIN

#: Where the wheel build writes the snapshot (next to the package so it ships inside it); absent in a source
#: checkout, where `compute()` is simply called each time.
PATH = Path(__file__).resolve().parent / "_build.json"

#: Feature flags a Z2 (or later) build asserts, independent of a specific feed kind or token (CONTRACT-AMENDMENTS,
#: "C16-10 `_build.json`, selfcheck and `/health.build`"). Kept short and code-checked below, not hand-waved: each
#: one names an `atlas` module whose presence proves it, so the list cannot silently drift from what shipped.
#: Deliberately only `atlas.*` modules (never `tools.*`, a dev-only package the wheel does not ship): `atlas
#: selfcheck` must compute the same capability set from a source checkout and from an installed wheel, or every
#: packaged build would fail its own check (`tools/release_gates.py`'s gates are a repository concern, not a
#: build-manifest capability a peer negotiates on).
_FEATURE_PROOF: tuple[tuple[str, str], ...] = (
    ("upgrade-journal-v1", "atlas.upgrade"),
    ("feed-freeze-v1", "atlas.feed_resume"),
    ("fork-check-every-pull", "atlas.sync"),
    ("wire-negotiation-v1", "atlas.wire"),
)


def _commit() -> str:
    """The git sha this tree was built from, or "unknown" (a wheel installed with no .git directory)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                              capture_output=True, text=True, timeout=5, check=True)
        sha = out.stdout.strip()
        return sha if sha else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _features() -> list[str]:
    import importlib

    found = []
    for name, module in _FEATURE_PROOF:
        try:
            importlib.import_module(module)
        except ImportError:
            continue
        found.append(name)
    return found


def compute() -> dict[str, Any]:
    """The manifest for the code actually running: version, the wire range (`atlas.wire`), the commit (best
    effort) and `capabilities` (the feed's registered capabilities plus this release's feature flags, each proven
    present by import rather than listed by hand)."""
    from . import feed_schema

    capabilities = sorted({*feed_schema.CAPABILITIES, *_features()})
    return {"version": __version__, "wire": {"min": WIRE_MIN, "max": WIRE_MAX}, "commit": _commit(),
            "capabilities": capabilities}


def load(path: Path | None = None) -> dict[str, Any] | None:
    """The committed snapshot, or None (a source checkout, or a corrupt file: never fatal by itself)."""
    try:
        return json.loads((path or PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write(path: Path | None = None) -> dict[str, Any]:
    """Used by `tools/write_build_manifest.py` at wheel-build time, and by tests."""
    manifest = compute()
    target = path or PATH
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


class BuildMismatch(RuntimeError):
    """The running code disagrees with its own committed `_build.json` (§10.4): refuse to start."""


def check(path: Path | None = None) -> list[str]:
    """Problems with the running build versus its own `_build.json`, each naming both values; empty when there is
    no snapshot (nothing to disagree with) or it agrees."""
    stored = load(path)
    if stored is None:
        return []
    computed = compute()
    problems = []
    if stored.get("version") != computed["version"]:
        problems.append(f"_build.json version {stored.get('version')!r} does not match the running code's "
                        f"{computed['version']!r} (a stale build/lib copy or a partial install)")
    if stored.get("wire") != computed["wire"]:
        problems.append(f"_build.json wire {stored.get('wire')!r} does not match the running code's "
                        f"{computed['wire']!r}")
    stored_caps = sorted(stored.get("capabilities") or [])
    if stored_caps != computed["capabilities"]:
        problems.append(f"_build.json capabilities {stored_caps} do not match the running code's "
                        f"{computed['capabilities']} (a mixed or partial build)")
    return problems
