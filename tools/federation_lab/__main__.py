"""Run the federation lab scenarios: `python -m tools.federation_lab [options] [-- pytest args]`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="Extra arguments after -- go to pytest, for example -k rotation.")
    parser.add_argument("--postgres-admin-url",
                        help="run both authorities on disposable databases created on this test PostgreSQL server")
    parser.add_argument("--logs", type=Path, help="copy every node's server log into this folder at the end")
    parser.add_argument("--junit", type=Path, help="write a JUnit XML report here")
    args, rest = parser.parse_known_args()
    env = dict(os.environ, ATLAS_FEDERATION_LAB="1")
    if args.postgres_admin_url:
        env["ATLAS_LAB_PG_URL"] = args.postgres_admin_url
    if args.logs:
        env["ATLAS_LAB_LOG_DIR"] = str(args.logs.resolve())
    command = [sys.executable, "-m", "pytest", "tests/federation_lab", "-ra", "-p", "no:cacheprovider"]
    if args.junit:
        command.append(f"--junitxml={args.junit.resolve()}")
    command += [a for a in rest if a != "--"]
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
