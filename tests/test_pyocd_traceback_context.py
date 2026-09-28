"""Private pyOCD config context for early CLI exceptions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.bench.pyocd_recordings import temporary_pyocd_traceback_environment


def test_traceback_context_adds_only_private_logging_option_and_preserves_parent_environment(tmp_path: Path) -> None:
    project_dir = tmp_path / "pyocd-traceback"
    base_environment = {"HOME": "parent-home", "PYOCD_PROJECT_DIR": ""}
    original_environment = dict(base_environment)

    with temporary_pyocd_traceback_environment(base_environment, project_dir) as environment:
        assert environment == {"HOME": "parent-home", "PYOCD_PROJECT_DIR": str(project_dir)}
        assert (project_dir / "pyocd.yaml").read_text(encoding="utf-8") == "debug.traceback: true\n"
        assert base_environment == original_environment

    assert not project_dir.exists()


def test_traceback_context_refuses_to_replace_an_existing_pyocd_project(tmp_path: Path) -> None:
    existing_project = tmp_path / "operator-project"
    existing_project.mkdir()
    existing_config = existing_project / "pyocd.yaml"
    existing_config.write_text("target_override: operator-target\n", encoding="utf-8")
    base_environment = {"PYOCD_PROJECT_DIR": str(existing_project)}

    with pytest.raises(ValueError, match="PYOCD_PROJECT_DIR"), temporary_pyocd_traceback_environment(
        base_environment, tmp_path / "private"
    ):
        pytest.fail("an explicit pyOCD project must not be displaced")

    assert existing_config.read_text(encoding="utf-8") == "target_override: operator-target\n"
    assert not (tmp_path / "private").exists()


def test_traceback_context_refuses_to_overwrite_a_preexisting_private_path(tmp_path: Path) -> None:
    project_dir = tmp_path / "already-owned"
    project_dir.mkdir()
    existing_config = project_dir / "pyocd.yaml"
    existing_config.write_text("target_override: keep\n", encoding="utf-8")

    with pytest.raises((FileExistsError, ValueError)), temporary_pyocd_traceback_environment({}, project_dir):
        pytest.fail("the context must not adopt a preexisting path")

    assert existing_config.read_text(encoding="utf-8") == "target_override: keep\n"


def test_traceback_context_cleans_up_after_exception(tmp_path: Path) -> None:
    project_dir = tmp_path / "pyocd-traceback"

    with pytest.raises(RuntimeError, match="intentional"), temporary_pyocd_traceback_environment({}, project_dir):
        raise RuntimeError("intentional")

    assert not project_dir.exists()
