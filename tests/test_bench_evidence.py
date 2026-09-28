from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from conftest import write_config

from tests.bench.test_bench_faults import record_gdb_stop_evidence, resolve_bench_gdb_executable


def test_bench_gdb_resolution_uses_explicit_fixture_config_despite_parent_override(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    config_path = write_config(project, gdb_executable=Path(sys.executable))
    unrelated_config = write_config(tmp_path / "unrelated", gdb_executable=Path("C:/unrelated/gdb.exe"))
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(unrelated_config))

    executable = resolve_bench_gdb_executable(config_path, project, "openocd")

    assert Path(executable).resolve(strict=True) == Path(sys.executable).resolve(strict=True)


def test_bench_evidence_export_redacts_absolute_file_and_fullname_fields(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    config_path = write_config(project, gdb_executable=Path(sys.executable))
    absolute = "/tmp/pytest-of-root/bench image/Src/main.c"
    log_path = project / "session.json"
    log_path.write_text(
        json.dumps({"gdb_stop_records": [f'*stopped,frame={{func="HardFault_Handler",file="{absolute}",fullname="{absolute}"}}']}),
        encoding="utf-8",
    )
    bench = SimpleNamespace(
        project=project,
        config=config_path,
        configuration=lambda: {"debuggers": {"dut": {"type": "openocd"}}},
        debugger_name=lambda: "dut",
    )
    monkeypatch.setenv("AGENTIC_HIL_BENCH_COMMIT", "1" * 40)
    monkeypatch.setenv("AGENTIC_HIL_BENCH_RUN_ID", "local-test-1")
    properties = {}

    record_gdb_stop_evidence(bench, "session.json", "attach_hardfault", lambda name, value: properties.__setitem__(name, value))

    recording = json.loads(properties["gdb_mi_stop_recording_v1"])
    assert recording["records"] == ['*stopped,frame={func="HardFault_Handler",file="<path redacted>",fullname="<path redacted>"}']
