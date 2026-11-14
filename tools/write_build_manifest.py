"""Write `atlas/_build.json` from the running code (CONTRACT-C16 §10.4). Run after `uv build`, against the same
checkout the wheel was built from, so the wheel ships the snapshot inside it (`atlas/_build.json` is included by
the default package data rules: it sits next to `atlas/__init__.py`). A source checkout has no such file and
`atlas.build_info.compute()` is simply called at import instead; `atlas selfcheck` only compares when the file
is present.

    python tools/write_build_manifest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    from atlas import build_info

    manifest = build_info.write()
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
