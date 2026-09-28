from __future__ import annotations

import sys
from pathlib import Path

from conftest import write_config

from tests.bench.test_bench_faults import resolve_bench_gdb_executable


def test_bench_gdb_resolution_uses_explicit_fixture_config_despite_parent_override(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    config_path = write_config(project, gdb_executable=Path(sys.executable))
    unrelated_config = write_config(tmp_path / "unrelated", gdb_executable=Path("C:/unrelated/gdb.exe"))
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(unrelated_config))

    executable = resolve_bench_gdb_executable(config_path, project, "openocd")

    assert Path(executable) == Path(sys.executable)
