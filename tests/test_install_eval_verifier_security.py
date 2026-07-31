from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.install import verifier
from evals.install.fixtures import agent_config_path, fixture_content
from evals.install.source import create_source_snapshot, source_digest


def make_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")


def test_root_owned_executable_rejects_eval_owned_file() -> None:
    class EvalOwnedExecutable:
        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=1000)

        def __str__(self) -> str:
            return "/home/eval/tool/bin/python"

    ok, detail = verifier.root_owned_executable(EvalOwnedExecutable())  # type: ignore[arg-type]

    assert not ok
    assert "not root-owned" in detail


def test_launcher_interpreter_must_resolve_to_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed-python"
    candidate = tmp_path / "other-python"
    allowed.write_bytes(b"allowed")
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(verifier, "LAUNCHER_PYTHON_ALLOWLIST", (allowed,))
    monkeypatch.setattr(
        verifier,
        "root_owned_executable",
        lambda path: (True, str(path)),
    )

    ok, detail = verifier.trusted_launcher_interpreter(candidate)

    assert not ok
    assert "not allowlisted" in detail


def test_safe_launcher_rejects_untrusted_interpreter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "agentic-hil"
    interpreter = tmp_path / "python"
    launcher.write_text(
        f"#!{interpreter}\n"
        "import re\n"
        "import sys\n"
        "from agentic_hil.cli import entrypoint\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script.pyw|.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(entrypoint())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verifier,
        "trusted_launcher_interpreter",
        lambda _path: (False, "executable is not root-owned"),
    )

    ok, detail = verifier.safe_launcher_script(launcher)

    assert not ok
    assert "not root-owned" in detail


def test_source_digest_rejects_directory_symlink(tmp_path: Path) -> None:
    package = tmp_path / "agentic_hil"
    package.mkdir()
    (package / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.py").write_text("PAYLOAD = True\n", encoding="utf-8")
    make_symlink(package / "linked", outside, directory=True)

    with pytest.raises(ValueError, match="may not contain symlinks"):
        source_digest(package)


def test_snapshot_rejects_package_symlink_before_staging(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    package = source / "src" / "agentic_hil"
    package.mkdir(parents=True)
    for name in ("AI_AGENT_QUICKSTART.md", "README.md", "pyproject.toml"):
        (source / name).write_text(f"{name}\n", encoding="utf-8")
    (package / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("PAYLOAD = True\n", encoding="utf-8")
    make_symlink(package / "linked.py", outside)
    destination = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="may not contain symlinks"):
        create_source_snapshot(source, destination)

    assert not destination.exists()


def test_trusted_staging_never_follows_ignored_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "agentic_hil"
    package.mkdir()
    (package / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pyc").write_bytes(b"must not be staged")
    make_symlink(package / "__pycache__", outside, directory=True)
    trusted_root = tmp_path / "trusted"
    monkeypatch.setattr(verifier, "TRUSTED_PACKAGE_ROOT", trusted_root)

    with pytest.raises(ValueError, match="may not contain symlinks"):
        verifier.prepare_trusted_package(package)

    assert not trusted_root.exists()


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_unmanaged_entry_check_ignores_the_agent_cli_bookkeeping(
    agent: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(verifier, "HOME", tmp_path)
    path = agent_config_path(agent, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seeded = fixture_content(agent, "unsafe-existing-config")
    assert seeded is not None
    path.write_text(seeded, encoding="utf-8")
    assert verifier.unmanaged_entry_untouched(agent)[0]

    # The agent CLI owns this file too and records its own state while running.
    if agent == "codex":
        path.write_text(seeded + '\n[projects."/workspace"]\ntrust_level = "trusted"\n', encoding="utf-8")
    else:
        document = json.loads(seeded)
        document["agent_cli_bookkeeping"] = {"lastSession": "abc"}
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    assert verifier.unmanaged_entry_untouched(agent)[0]


def _doctor_saying(installation: object) -> object:
    payload = json.dumps({"ok": True, "installation": installation} if installation is not None else {"ok": True})
    return SimpleNamespace(returncode=0, stdout=payload, stderr="")


def test_an_editable_installation_is_caught_by_asking_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two models reached for an editable install after pip refused.

    The metadata check refuses one already, but only as the absence of a copied
    package. This makes the product itself the thing that has to notice.
    """
    monkeypatch.setattr(
        verifier,
        "run_trusted",
        lambda arguments, **kwargs: _doctor_saying({"editable": True, "package_path": "/workspace/source/src/agentic_hil"}),
    )

    ok, detail = verifier.doctor_reports_a_copied_installation()

    assert not ok
    assert "/workspace/source" in detail


def test_a_copied_installation_passes_the_doctor_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verifier,
        "run_trusted",
        lambda arguments, **kwargs: _doctor_saying({"editable": False, "package_path": "/home/eval/.local/share/uv/x"}),
    )

    ok, detail = verifier.doctor_reports_a_copied_installation()

    assert ok
    assert "copied installation" in detail


def test_a_doctor_that_says_nothing_about_the_installation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An older build that predates the field must not pass by omission."""
    monkeypatch.setattr(verifier, "run_trusted", lambda arguments, **kwargs: _doctor_saying(None))

    ok, detail = verifier.doctor_reports_a_copied_installation()

    assert not ok
    assert "nothing about the installation" in detail


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_a_clean_case_has_no_operator_fixture_to_lose(
    agent: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The clean fixture seeds nothing, so a missing config is not a loss.

    Requiring the file first reported "agent config missing" as a preservation
    failure in 37 runs whose install never got far enough to write one.
    """
    monkeypatch.setattr(verifier, "HOME", tmp_path)
    assert fixture_content(agent, "clean") is None

    ok, detail = verifier.fixture_preserved(agent, "clean")

    assert ok
    assert "nothing" in detail or "no operator fixture" in detail


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_a_seeded_operator_fixture_that_vanished_is_a_failure(
    agent: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(verifier, "HOME", tmp_path)

    ok, detail = verifier.fixture_preserved(agent, "preserve-user-config")

    assert not ok
    assert detail == "agent config missing"


@pytest.mark.parametrize("agent", ["claude-code", "opencode"])
def test_skill_is_not_discoverable_when_nothing_was_installed(
    agent: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """These CLIs discover a skill by its location, so the file has to be there.

    The check compared the expected path against itself, which is true whatever
    the disk holds. Measured: fourteen runs whose install never produced a
    launcher were still reported as having a discoverable skill.
    """
    monkeypatch.setattr(verifier, "HOME", tmp_path)
    monkeypatch.setattr(verifier, "safe_owned_path", lambda path, root, **kwargs: (True, str(path)))
    installed = verifier.skill_path(agent)

    ok, detail = verifier.skill_registered(agent, installed)

    assert not ok
    assert "nothing installed" in detail


@pytest.mark.parametrize("agent", ["claude-code", "opencode"])
def test_skill_is_discoverable_once_it_is_where_the_cli_looks(
    agent: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(verifier, "HOME", tmp_path)
    monkeypatch.setattr(verifier, "safe_owned_path", lambda path, root, **kwargs: (True, str(path)))
    installed = verifier.skill_path(agent)
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text("skill", encoding="utf-8")

    ok, detail = verifier.skill_registered(agent, installed)

    assert ok, detail


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_unmanaged_entry_check_rejects_a_rewritten_operator_entry(
    agent: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(verifier, "HOME", tmp_path)
    path = agent_config_path(agent, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seeded = fixture_content(agent, "unsafe-existing-config")
    assert seeded is not None

    path.write_text(seeded.replace("/operator/agentic-hil", "/home/eval/.local/bin/agentic-hil"), encoding="utf-8")
    accepted, detail = verifier.unmanaged_entry_untouched(agent)
    assert not accepted, detail

    if agent == "codex":
        path.write_text(f"{verifier.MANAGED_MCP_MARKERS[0]}\n{seeded}", encoding="utf-8")
        accepted, detail = verifier.unmanaged_entry_untouched(agent)
        assert not accepted, detail
        assert "managed marker" in detail


def test_mcp_probe_reports_why_the_server_refused_to_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    refusal = "import sys; sys.stderr.write('state_root refused\\n'); raise SystemExit(3)"
    monkeypatch.setattr(
        verifier,
        "trusted_command",
        lambda arguments: [sys.executable, "-c", refusal],
    )
    monkeypatch.setattr(verifier, "trusted_environment", lambda: dict(os.environ))

    with pytest.raises(RuntimeError) as excinfo:
        verifier.mcp_probe(["mcp-stdio"], tmp_path)

    message = str(excinfo.value)
    assert "state_root refused" in message, message
    assert "exit=3" in message, message
