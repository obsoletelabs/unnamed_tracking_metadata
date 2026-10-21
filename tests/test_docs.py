"""The guide must stay true to the code: generated pages current, everything documented, links and images real."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from atlas import runtime
from atlas.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide"
sys.path.insert(0, str(ROOT))


def test_generated_reference_pages_are_current():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_docs.py"), "--check"], capture_output=True, text=True,
                            cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_setting_has_documentation():
    from tools.gen_docs import ENV_HELP

    assert set(Settings.model_fields) == set(ENV_HELP), "document new settings in tools/gen_docs.py ENV_HELP"
    text = (GUIDE / "reference-env.md").read_text()
    for name in Settings.model_fields:
        assert f"`ATLAS_{name.upper()}`" in text
    settings_page = (GUIDE / "reference-settings.md").read_text()
    for spec in runtime.REGISTRY:
        assert f"`{spec.key}`" in settings_page


def test_every_api_endpoint_is_described():
    text = (GUIDE / "reference-api.md").read_text()
    assert "(undocumented)" not in text
    assert text.count("| `") > 25


def markdown_files():
    return sorted([*GUIDE.rglob("*.md"), ROOT / "README.md"])


@pytest.mark.parametrize("path", markdown_files(), ids=lambda p: p.name)
def test_relative_links_and_images_resolve(path):
    text = path.read_text()
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        file_part = target.split("#")[0]
        assert (path.parent / file_part).resolve().exists(), f"{path.name}: broken link {target}"
    for target in re.findall(r"\]\(([^)\s]+#[^)\s]+)\)", text):
        file_part, anchor = target.split("#", 1)
        if file_part.startswith(("http://", "https://")):
            continue
        dest = (path.parent / file_part).resolve() if file_part else path
        heads = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-") for h in re.findall(r"^#+\s+(.+)$", dest.read_text(), re.M)}
        assert anchor in heads, f"{path.name}: no heading for #{anchor} in {dest.name}"


def test_guide_covers_the_documented_pages():
    names = {p.name for p in GUIDE.glob("*.md")}
    for expected in ("index.md", "quickstart.md", "concepts.md", "using-the-site.md", "reviewing.md", "administering.md",
                     "federation.md", "sso.md", "tracking-app.md", "deployment.md", "security.md", "extending.md",
                     "troubleshooting.md", "reference-api.md", "reference-env.md", "reference-settings.md"):
        assert expected in names


def test_cli_commands_in_the_guide_exist():
    from atlas import cli

    text = (GUIDE / "quickstart.md").read_text() + (GUIDE / "deployment.md").read_text()
    commands = set(re.findall(r"`atlas ([a-z-]+)", text))
    parser_commands = {"serve", "migrate", "backup", "recover-mirror", "token", "node", "secret-key", "sync",
                       "cache-media", "make-admin", "import-legacy"}
    assert commands <= parser_commands
    assert parser_commands <= {name for name in dir(cli) if name.startswith("cmd_")} | {
        n.removeprefix("cmd_").replace("_", "-") for n in dir(cli) if n.startswith("cmd_")}


def test_every_page_is_in_the_mkdocs_navigation():
    nav = (ROOT / "mkdocs.yml").read_text()
    for page in GUIDE.rglob("*.md"):
        assert page.relative_to(GUIDE).as_posix() in nav, f"{page.name} is not in mkdocs.yml nav"


def test_deploy_folders_are_complete_and_offer_a_source_build():
    for folder in ("authority", "local", "lab"):
        base = ROOT / "deploy" / folder
        assert (base / "compose.yaml").exists() and (base / "compose.build.yaml").exists(), folder
        text = (base / "compose.yaml").read_text()
        assert "ghcr.io/obsoletelabs/unnamed_tracking_metadata" in text and "${ATLAS_TAG" in text
        assert "context: ../.." in (base / "compose.build.yaml").read_text()
    assert (ROOT / "deploy" / "authority" / ".env.example").exists() and (ROOT / "deploy" / "local" / ".env.example").exists()
