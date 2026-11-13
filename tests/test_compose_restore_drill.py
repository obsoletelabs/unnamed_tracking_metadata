"""Unit coverage for the Windows-specific fixes in `tools.compose_restore_drill` (review on issue #27 backlog):

1. `restore.sh` must be LF before a POSIX shell runs it, whatever line endings the checkout gave the copy.
2. The project name must cross the shell boundary on the command line, not only through `env=`, because on
   Windows the resolved shell can be the WSL launcher, which starts a separate Linux environment that does not
   inherit the parent process's environment.

The full drill itself needs Docker (and, to reproduce the original bug, WSL) and is not run here."""

from __future__ import annotations

from tools import compose_restore_drill as drill


def test_to_lf_normalizes_crlf():
    assert drill.to_lf(b"#!/bin/sh\r\nset -eu\r\necho hi\r\n") == b"#!/bin/sh\nset -eu\necho hi\n"
    assert drill.to_lf(b"#!/bin/sh\nset -eu\necho hi\n") == b"#!/bin/sh\nset -eu\necho hi\n"  # already LF: unchanged


def test_prepare_writes_restore_sh_as_lf(tmp_path, monkeypatch):
    fake_authority = tmp_path / "authority"
    fake_authority.mkdir()
    (fake_authority / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (fake_authority / "Caddyfile").write_text("", encoding="utf-8")
    (fake_authority / "restore.sh").write_bytes(b"#!/bin/sh\r\nset -eu\r\necho hi\r\n")
    monkeypatch.setattr(drill, "AUTHORITY", fake_authority)

    out = tmp_path / "drill"
    out.mkdir()
    drill.prepare(out, "atlas-drill:local")
    assert b"\r" not in (out / "restore.sh").read_bytes()


def test_project_probe_and_restore_cmd_force_the_project_across_the_shell_boundary():
    project = "atlasdrill1234ab"
    probe = drill.project_probe_cmd(project)
    restore = drill.restore_cmd(project, "backups/drill.dump")
    # the project name travels on the command line (crosses a WSL launcher boundary that env= does not),
    # ahead of the actual command, so it is set before docker compose ever runs
    assert probe.startswith(f"COMPOSE_PROJECT_NAME={project} ") and probe.endswith("docker compose config --format json")
    assert restore.startswith(f"COMPOSE_PROJECT_NAME={project} ") and restore.endswith("sh restore.sh backups/drill.dump")
    # restore.sh itself is never rewritten or given a project argument: it stays exactly what ships to operators
    assert "restore.sh" in restore and "--project" not in restore and "-p " not in restore


def test_project_probe_and_restore_cmd_quote_an_unusual_project_name():
    import shlex

    # a project name is always our own "atlasdrill" + token_hex(3) in practice, but the quoting must still be
    # safe: nothing after COMPOSE_PROJECT_NAME= may break out of the single assignment
    project = "atlasdrill one; rm -rf /"
    probe = drill.project_probe_cmd(project)
    assignment = probe.rsplit(" docker compose", 1)[0]
    name, _, quoted = assignment.partition("=")
    assert name == "COMPOSE_PROJECT_NAME"
    assert shlex.split(quoted) == [project]
