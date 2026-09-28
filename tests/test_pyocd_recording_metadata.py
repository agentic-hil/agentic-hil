from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.bench import pyocd_recordings


def test_pyocd_metadata_commands_use_the_explicit_bench_environment(monkeypatch) -> None:
    environment = {
        "HOME": "/bench-home",
        "PATH": "/work/.venv/bin:/usr/bin:/bin",
        "AGENTIC_HIL_CONFIG": "/bench-config/config.yaml",
    }
    target_document = {
        "pyocd_version": "0.45.1",
        "targets": [
            {
                "name": "stm32f446retx",
                "part_number": "STM32F446RETx",
                "source": "pack",
            }
        ],
    }
    responses = iter(
        (
            SimpleNamespace(returncode=0, stdout=json.dumps(target_document), stderr=""),
            SimpleNamespace(returncode=0, stdout="Keil.STM32F4xx_DFP 3.1.1\n", stderr=""),
        )
    )
    calls: list[dict] = []
    monkeypatch.setattr(pyocd_recordings.shutil, "which", lambda _name: "/work/.venv/bin/pyocd")
    monkeypatch.setattr(
        pyocd_recordings.subprocess,
        "run",
        lambda *args, **kwargs: (calls.append(kwargs) or next(responses)),
    )

    executable, pyocd_version, pack_version = pyocd_recordings.pyocd_provenance(environment)

    assert (executable, pyocd_version, pack_version) == ("/work/.venv/bin/pyocd", "0.45.1", "3.1.1")
    assert len(calls) == 2
    assert all(call.get("env") == environment for call in calls)


def test_pyocd_bench_environment_restores_the_image_pack_data_home() -> None:
    isolated_environment = {
        "HOME": "/bench-home",
        "XDG_DATA_HOME": "/tmp/pytest-home/.local/share",
        "XDG_CONFIG_HOME": "/bench-config",
        "XDG_STATE_HOME": "/bench-state",
    }
    bench = SimpleNamespace(environment=lambda **_overrides: isolated_environment.copy())

    environment = pyocd_recordings.pyocd_bench_environment(bench)
    server_environment = pyocd_recordings.PyOcdBench(bench).environment()
    expected_data_home = str(Path(isolated_environment["HOME"]) / ".local" / "share")

    assert environment["HOME"] == "/bench-home"
    assert environment["XDG_DATA_HOME"] == expected_data_home
    assert server_environment["XDG_DATA_HOME"] == expected_data_home
    assert environment["XDG_CONFIG_HOME"] == "/bench-config"
    assert environment["XDG_STATE_HOME"] == "/bench-state"
    assert isolated_environment["XDG_DATA_HOME"] == "/tmp/pytest-home/.local/share"


def test_pyocd_failure_evidence_keeps_redacted_diagnostics_and_real_transcript(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    action_log = state_root / "pyocd-probe.json"
    action_log.write_text(
        json.dumps(
            {
                "command": "pyocd commander --uid PYOCD123 --port /dev/ttyACM0",
                "returncode": 1,
                "timed_out": False,
                "stdout": "connecting to PYOCD123",
                "stderr": "SWD/JTAG communication failure (No ACK) at /dev/ttyACM0",
            }
        ),
        encoding="utf-8",
    )
    bench = SimpleNamespace(project=tmp_path, state_root=state_root)
    result = {
        "ok": False,
        "error_type": "target_not_detected",
        "backend_error_type": "target_not_detected",
        "summary": f"pyOCD could not connect to PYOCD123 at /dev/ttyACM0 in {tmp_path}",
        "programmer_output": {
            "stdout": "connecting to PYOCD123",
            "stderr": "SWD/JTAG communication failure (No ACK) at /dev/ttyACM0",
            "returncode": 1,
        },
        "log_path": str(action_log),
    }

    evidence = pyocd_recordings.pyocd_result_evidence(
        bench,
        result,
        ("PYOCD123", "/dev/ttyACM0", str(tmp_path), tmp_path.as_posix()),
    )
    serialized = json.dumps(evidence, sort_keys=True)

    assert evidence["result"]["summary"] == "pyOCD could not connect to [redacted] at [redacted] in [redacted]"
    assert evidence["result"]["backend_error_type"] == "target_not_detected"
    assert evidence["programmer_output"]["stderr"] == "SWD/JTAG communication failure (No ACK) at [redacted]"
    assert evidence["action_log"]["stderr"] == "SWD/JTAG communication failure (No ACK) at [redacted]"
    assert "PYOCD123" not in serialized
    assert "/dev/ttyACM0" not in serialized
    assert str(tmp_path) not in serialized


def test_pyocd_failure_evidence_keeps_diagnostics_when_action_log_shape_is_invalid(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    action_log = state_root / "pyocd-probe.json"
    action_log.write_text("[]", encoding="utf-8")
    bench = SimpleNamespace(project=tmp_path, state_root=state_root)
    result = {
        "ok": False,
        "error_type": "probe_discovery_failed",
        "backend_error_type": "probe_discovery_failed",
        "summary": "pyOCD could not enumerate probes",
        "programmer_output": {"stderr": "USB permission refused"},
        "log_path": str(action_log),
    }

    evidence = pyocd_recordings.pyocd_result_evidence(bench, result, ())

    assert evidence["result"]["summary"] == "pyOCD could not enumerate probes"
    assert evidence["result"]["backend_error_type"] == "probe_discovery_failed"
    assert evidence["programmer_output"] == {"stderr": "USB permission refused"}
    assert evidence["action_log"] is None
    assert evidence["transcript_error"]
