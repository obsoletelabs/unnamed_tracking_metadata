"""C16 section 10.2 (T-U20): every release gate fails on a planted hit, passes on a clean tree and under an excluded
path, and an allowlist entry that allows nothing is itself a failure. A gate that cannot fail would fail the build."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import release_gates as rg  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.org")
    git(tmp_path, "config", "user.name", "t")
    put(tmp_path, "atlas/ok.py", "x = 1\n")
    return tmp_path


def put(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "-f", rel)


def verdict(repo: Path, gates=rg.GATES) -> tuple[int, str]:
    out = io.StringIO()
    code = rg.run(repo, gates, out=out)
    return code, out.getvalue()


def test_a_clean_tree_passes(repo):
    assert verdict(repo) == (0, "")


@pytest.mark.parametrize(("gate", "rel", "text"), [
    ("G-U1", "build/lib/atlas/x.py", "x = 1\n"),
    ("G-U1", "atlas_metadata.egg-info/PKG-INFO", "Name: x\n"),
    ("G-W6", "atlas/sender.py", 'headers = {"Atlas-Protocol": "1.0"}\n'),
    ("G-W6", "atlas/sender2.py", 'request.headers["Atlas-Protocol"] = "1.0"\n'),
])
def test_a_planted_hit_fails_with_the_gate_id(repo, gate, rel, text):
    put(repo, rel, text)
    code, out = verdict(repo)
    assert code == 1 and out.count(gate) >= 1 and rel.split("/")[-1] in out


def test_the_same_hit_under_an_excluded_or_unscanned_path_passes(repo):
    put(repo, "atlas/migrations/versions/x.py", 'headers = {"Atlas-Protocol": "1.0"}\n')  # excluded
    put(repo, "tests/test_x.py", 'headers = {"Atlas-Protocol": "1.0"}\n')  # a test plays a v0 caller: not scanned
    put(repo, "atlas/wire.py", 'LEGACY_HEADER = "Atlas-Protocol"\n')  # the one place that defines it (skipped)
    assert verdict(repo) == (0, "")


def test_reading_the_legacy_header_is_not_sending_it(repo):
    put(repo, "atlas/api/routes.py", 'Legacy = Annotated[str | None, Header(alias=wire.LEGACY_HEADER)]\n')
    assert verdict(repo) == (0, "")


def test_a_hit_in_an_allowlisted_file_must_match_the_allowlist_text(repo):
    gate = rg.Gate("G-T", r"FORBIDDEN", paths=("atlas",), allow=(("atlas/ok.py", "FORBIDDEN # fine"),))
    put(repo, "atlas/ok.py", "x = FORBIDDEN # fine\n")
    assert verdict(repo, (gate,)) == (0, "")
    put(repo, "atlas/ok.py", "x = FORBIDDEN # fine\ny = FORBIDDEN\n")
    code, out = verdict(repo, (gate,))
    assert code == 1 and "G-T atlas/ok.py:2" in out and "atlas/ok.py:1" not in out


def test_an_allowlist_entry_with_no_matching_line_is_stale_and_fails(repo):
    gate = rg.Gate("G-T", r"FORBIDDEN", paths=("atlas",), allow=(("atlas/ok.py", "FORBIDDEN # fine"),))
    code, out = verdict(repo, (gate,))
    assert code == 1 and "stale allowlist entry" in out


def test_the_repository_itself_passes_every_gate():
    """What CI runs: the real tree, no tracked build output, no module sending the v0 header."""
    code, out = verdict(ROOT)
    assert code == 0, out
