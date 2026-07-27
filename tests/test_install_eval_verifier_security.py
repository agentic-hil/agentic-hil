from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.install import verifier
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
