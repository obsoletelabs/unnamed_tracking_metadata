"""Shared vocabulary file: generate it, check it, compare it across repositories (CONTRACTS-v1 C7.4, X2).

`contracts/vocab-v1.json` lists the closed vocabularies Atlas, the metadata-atlas plugin and the
Tracking App must agree on (achievement tier and kind, crossplay, and the values reserved for later
protocols). Each repository generates the file from its own registry; this script checks that the
overlapping keys agree. Only `generate` and `check` import Atlas. `verify` and `compare` use the
standard library alone, so another repository can vendor this file as it is:

    python -m tools.vocab_check generate              # Atlas: rewrite the file and its .sha256 from atlas/vocab.py
    python -m tools.vocab_check check                 # Atlas: fail when the file or digest is stale
    python tools/vocab_check.py verify FILE           # any repo: digest and shape of one file
    python tools/vocab_check.py compare A.json B.json # any repo: the shared keys of two or more files agree

Exit status: 0 when everything agrees, 1 when something differs (each difference is printed), 2 on a
usage error or an unreadable file. Vocabularies are sets: the order of values is not compared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FILE = "contracts/vocab-v1.json"
#: Keys only Atlas publishes; never compared across repositories (C7.4).
ATLAS_ONLY = frozenset({"feed_kinds"})
#: The keys every repository's file must carry for version 1.
REQUIRED = ("achievement_tier", "achievement_kind", "crossplay")
MAX_KEYS, MAX_VALUES, MAX_BYTES = 64, 256, 64 * 1024
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def render(data: dict[str, Any]) -> str:
    """Two-space indent, keys in the given order, LF, trailing newline (the same as `atlas.vocab.render`)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def digest_line(text: str, name: str) -> str:
    return f"{digest(text)}  {name}\n"


def shape_errors(data: Any) -> list[str]:
    """What is wrong with the structure of one file (empty when it is well formed)."""
    if not isinstance(data, dict):
        return ["the file is not a JSON object"]
    errors = []
    if type(data.get("version")) is not int or data["version"] < 1:
        errors.append("'version' must be a positive integer")
    if len(data) > MAX_KEYS:
        errors.append(f"more than {MAX_KEYS} keys")
    for key in REQUIRED:
        if key not in data:
            errors.append(f"missing key {key!r}")
    for key, values in data.items():
        if key == "version":
            continue
        if not isinstance(key, str) or not _KEY.match(key):
            errors.append(f"key {str(key)[:70]!r} is not a lowercase identifier")
            continue
        if not isinstance(values, list) or len(values) > MAX_VALUES:
            errors.append(f"{key}: must be a list of at most {MAX_VALUES} values")
            continue
        bad = [v for v in values if not isinstance(v, str) or not _TOKEN.match(v)]
        if bad:
            errors.append(f"{key}: values must be lowercase tokens of at most 40 characters: {bad[:5]!r}")
        if len(set(map(str, values))) != len(values):
            errors.append(f"{key}: repeated values")
    return errors


def load(path: Path) -> tuple[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{path}: larger than {MAX_BYTES} bytes")
    text = raw.decode("utf-8")
    return text, json.loads(text)


def compare(files: dict[str, dict[str, Any]]) -> list[str]:
    """Differences between the keys two or more files share (Atlas-only keys excluded)."""
    names = list(files)
    problems = []
    versions = {n: files[n].get("version") for n in names}
    if len(set(versions.values())) > 1:
        problems.append("version differs: " + ", ".join(f"{n}={v}" for n, v in versions.items()))
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = (set(files[a]) & set(files[b])) - ATLAS_ONLY - {"version"}
            for key in sorted(shared):
                left, right = set(files[a][key]), set(files[b][key])
                if left != right:
                    problems.append(f"{key}: only in {a}: {sorted(left - right)}; only in {b}: {sorted(right - left)}")
            for key in REQUIRED:
                if key not in files[a] or key not in files[b]:
                    problems.append(f"{key}: missing from {a if key not in files[a] else b}")
    return problems


# ---- commands --------------------------------------------------------------------------------------------

def _atlas_file() -> tuple[str, str]:
    sys.path.insert(0, str(ROOT))
    from atlas import vocab

    text = vocab.render(vocab.export())
    if text != render(vocab.export()):  # the two renderers must never drift
        raise SystemExit("atlas.vocab.render and tools/vocab_check.render disagree")
    return text, digest_line(text, Path(FILE).name)


def cmd_generate(root: Path) -> int:
    text, line = _atlas_file()
    target = root / FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    target.with_name(target.name + ".sha256").write_bytes(line.encode("utf-8"))
    print(f"wrote {FILE} ({digest(text)})")
    return 0


def cmd_check(root: Path) -> int:
    text, line = _atlas_file()
    target = root / FILE
    problems = []
    try:
        have = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
        have_digest = target.with_name(target.name + ".sha256").read_bytes().decode("utf-8").replace("\r\n", "\n")
    except OSError as exc:
        print(f"cannot read {FILE}: {exc}")
        return 1
    if have != text:
        problems.append(f"{FILE} does not match atlas/vocab.py; run: python -m tools.vocab_check generate")
    if have_digest != line:
        problems.append(f"{FILE}.sha256 is stale; run: python -m tools.vocab_check generate")
    for p in problems:
        print(p)
    if not problems:
        print(f"{FILE} matches atlas/vocab.py ({digest(text)})")
    return 1 if problems else 0


def cmd_verify(path: Path) -> int:
    try:
        text, data = load(path)
    except (OSError, ValueError) as exc:
        print(f"cannot read {path}: {exc}")
        return 2
    problems = shape_errors(data)
    side = path.with_name(path.name + ".sha256")
    if side.exists():
        want = side.read_text(encoding="utf-8").split()[0] if side.read_text(encoding="utf-8").split() else ""
        if want != digest(text):
            problems.append(f"{side.name} says {want[:16]}…, the file hashes to {digest(text)[:16]}…")
    else:
        problems.append(f"{side.name} is missing")
    for p in problems:
        print(f"{path}: {p}")
    if not problems:
        print(f"{path}: ok ({digest(text)})")
    return 1 if problems else 0


def cmd_compare(paths: list[Path]) -> int:
    files: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            _, data = load(path)
        except (OSError, ValueError) as exc:
            print(f"cannot read {path}: {exc}")
            return 2
        errors = shape_errors(data)
        if errors:
            for e in errors:
                print(f"{path}: {e}")
            return 1
        files[str(path)] = data
    problems = compare(files)
    for p in problems:
        print(p)
    if not problems:
        print(f"{len(files)} files agree on their shared vocabularies")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vocab_check", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "check"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    sub.add_parser("verify").add_argument("file", type=Path)
    sub.add_parser("compare").add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "generate":
        return cmd_generate(args.root)
    if args.command == "check":
        return cmd_check(args.root)
    if args.command == "verify":
        return cmd_verify(args.file)
    if len(args.files) < 2:
        parser.error("compare needs at least two files")
    return cmd_compare(args.files)


if __name__ == "__main__":
    raise SystemExit(main())
