from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.bench import pyocd_recordings, usb_reset_support


def test_pyocd_timeout_safety_accepts_the_recorded_null_optional_status_fields() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "pyocd_probe_discovery_usb_timeout_recording.json"
    recording = json.loads(fixture_path.read_text(encoding="utf-8"))["recording"]
    result = {
        **recording["result"],
        "tool": "debugger_probes_list",
        "backend": "pyocd",
        "target_contacted": False,
        "retry_safe": True,
        "target_ok": None,
        "cleanup_ok": None,
        "cleanup_required": False,
        "quarantined": False,
        "programmer_output": {
            "stdout": recording["stdout"],
            "stderr": recording["stderr"],
            "returncode": recording["returncode"],
        },
    }

    assert pyocd_recordings.safe_initial_usb_timeout(result) is True


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


class FakePyOcdServer:
    def __init__(self, results: list[dict], events: list[tuple]) -> None:
        self._results = iter(results)
        self.events = events
        self.pid = 4321
        self.process = SimpleNamespace(poll=lambda: None)

    def call(self, name: str, arguments: dict | None = None) -> tuple[dict, dict]:
        self.events.append(("mcp", name, arguments))
        result = next(self._results)
        return {}, result


def _pyocd_listing(probe_id: str, stdout: str = "") -> dict:
    return {
        "ok": True,
        "target_ok": True,
        "target_contacted": True,
        "retry_safe": False,
        "audit_ok": True,
        "cleanup_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "lease_state": "active",
        "side_effect_status": "unchanged",
        "hardware_state": "unchanged",
        "tool": "debugger_probes_list",
        "backend": "pyocd",
        "probes": [{"probe_id": probe_id}],
        "summary": "1 connected debugger probe(s) detected.",
        "programmer_output": {"stdout": stdout, "stderr": "", "returncode": 0},
    }


def _safe_pyocd_usb_timeout() -> dict:
    recording = json.loads(
        (Path(__file__).parent / "fixtures" / "pyocd_probe_discovery_usb_timeout_recording.json").read_text(
            encoding="utf-8"
        )
    )["recording"]
    return {
        **recording["result"],
        "ok": False,
        "tool": "debugger_probes_list",
        "backend": "pyocd",
        "error_type": "probe_discovery_failed",
        "target_contacted": False,
        "target_ok": None,
        "retry_safe": True,
        "cleanup_ok": None,
        "cleanup_required": False,
        "quarantined": False,
        "programmer_output": {
            "stdout": recording["stdout"],
            "stderr": recording["stderr"],
            "returncode": recording["returncode"],
        },
    }


def _run_pyocd_discovery_reset(monkeypatch, results: list[dict]):
    events: list[tuple] = []
    server = FakePyOcdServer(results, events)
    bench = SimpleNamespace(project=Path("."), state_root=Path("."))
    properties: dict[str, str] = {}
    identity = SimpleNamespace(serial_number="SERIAL-7", vid="0483", pid="374b")
    monkeypatch.setattr(
        usb_reset_support,
        "reset_usb_device",
        lambda actual: events.append(("reset", actual.serial_number, actual.vid, actual.pid)),
    )
    monkeypatch.setattr(
        usb_reset_support,
        "wait_for_usb_device",
        lambda **kwargs: events.append(("wait", kwargs["expected_serial"], kwargs["expected_vid"], kwargs["expected_pid"])),
    )

    recording = pyocd_recordings.pyocd_discovery_after_usb_reset(
        server,
        bench,
        expected_serial="SERIAL-7",
        usb_identity=identity,
        private_values=("SERIAL-7",),
        record_property=lambda name, value: properties.__setitem__(name, value),
    )
    return events, properties, recording


def test_pyocd_discovery_reset_records_initial_timeout_and_same_process_recovery(monkeypatch) -> None:
    initial = _safe_pyocd_usb_timeout()
    recovered = _pyocd_listing("SERIAL-7", '{"boards":[{"unique_id":"SERIAL-7"}]}')

    events, properties, recording = _run_pyocd_discovery_reset(monkeypatch, [initial, recovered])

    assert [event[0] for event in events] == ["mcp", "reset", "wait", "mcp"]
    assert [event[1] for event in events if event[0] == "mcp"] == ["debugger_probes_list"] * 2
    assert recording["mcp_process_same"] is True
    assert recording["initial"]["result"]["target_contacted"] is False
    assert recording["initial"]["result"]["target_ok"] is None
    assert recording["initial"]["result"]["retry_safe"] is True
    assert recording["initial"]["result"]["cleanup_ok"] is None
    assert "USB Error: [Errno 110] Operation timed out" in recording["initial"]["programmer_output"]["stdout"]
    assert recording["after"]["result"]["ok"] is True
    assert properties["pyocd_discovery_before_usb_reset_v1"]
    assert properties["pyocd_discovery_after_usb_reset_v1"]
    assert "SERIAL-7" not in json.dumps(properties)


def test_pyocd_discovery_reset_is_still_performed_after_an_initial_success(monkeypatch) -> None:
    events, _, recording = _run_pyocd_discovery_reset(
        monkeypatch,
        [_pyocd_listing("OTHER-PROBE"), _pyocd_listing("SERIAL-7")],
    )

    assert [event[0] for event in events] == ["mcp", "reset", "wait", "mcp"]
    assert recording["initial"]["result"]["ok"] is True
    assert recording["after"]["result"]["ok"] is True


def test_pyocd_discovery_reset_retains_post_reset_failure_evidence(monkeypatch) -> None:
    after = _safe_pyocd_usb_timeout()
    after["summary"] = "post-reset discovery still timed out"
    events: list[tuple] = []
    server = FakePyOcdServer([_safe_pyocd_usb_timeout(), after], events)
    properties: dict[str, str] = {}
    identity = SimpleNamespace(serial_number="SERIAL-7", vid="0483", pid="374b")
    monkeypatch.setattr(usb_reset_support, "reset_usb_device", lambda _identity: events.append(("reset",)))
    monkeypatch.setattr(usb_reset_support, "wait_for_usb_device", lambda **_kwargs: events.append(("wait",)))

    with pytest.raises(pytest.fail.Exception, match="post-reset discovery still timed out"):
        pyocd_recordings.pyocd_discovery_after_usb_reset(
            server,
            SimpleNamespace(project=Path("."), state_root=Path(".")),
            expected_serial="SERIAL-7",
            usb_identity=identity,
            private_values=("SERIAL-7",),
            record_property=lambda name, value: properties.__setitem__(name, value),
        )

    assert [event[0] for event in events] == ["mcp", "reset", "wait", "mcp"]
    assert "post-reset discovery still timed out" in properties["pyocd_discovery_after_usb_reset_v1"]


@pytest.mark.parametrize("failure", ["pid_changed", "process_exited"])
def test_pyocd_discovery_reset_rejects_a_replaced_or_dead_mcp_process(monkeypatch, failure) -> None:
    events: list[tuple] = []
    server = FakePyOcdServer([_safe_pyocd_usb_timeout()], events)
    properties: dict[str, str] = {}
    identity = SimpleNamespace(serial_number="SERIAL-7", vid="0483", pid="374b")

    def reset(_identity) -> None:
        events.append(("reset",))
        if failure == "pid_changed":
            server.pid += 1
        else:
            server.process.poll = lambda: 1

    monkeypatch.setattr(usb_reset_support, "reset_usb_device", reset)
    monkeypatch.setattr(usb_reset_support, "wait_for_usb_device", lambda **_kwargs: events.append(("wait",)))

    with pytest.raises(pytest.fail.Exception, match="same pyOCD MCP process"):
        pyocd_recordings.pyocd_discovery_after_usb_reset(
            server,
            SimpleNamespace(project=Path("."), state_root=Path(".")),
            expected_serial="SERIAL-7",
            usb_identity=identity,
            private_values=("SERIAL-7",),
            record_property=lambda name, value: properties.__setitem__(name, value),
        )

    assert [event[0] for event in events] == ["mcp", "reset", "wait"]
    assert "pyocd_discovery_before_usb_reset_v1" in properties
    assert "pyocd_discovery_after_usb_reset_v1" not in properties


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_contacted", True),
        ("target_ok", False),
        ("retry_safe", False),
        ("side_effect_status", "partial"),
        ("hardware_state", "unknown"),
        ("audit_ok", False),
        ("cleanup_ok", False),
        ("cleanup_required", True),
        ("quarantined", True),
        ("lease_state", "stale"),
        ("error_type", "backend_timeout"),
        ("programmer_output", {"stdout": "not JSON", "stderr": "", "returncode": 1}),
        (
            "programmer_output",
            {
                "stdout": json.dumps({"status": 1, "error": "pyocd.core.exceptions.ProbeError: permission denied"}),
                "stderr": "",
                "returncode": 1,
            },
        ),
    ],
)
def test_pyocd_discovery_reset_refuses_unsafe_failure_before_any_reset(monkeypatch, field, value) -> None:
    unsafe = _safe_pyocd_usb_timeout()
    unsafe[field] = value
    events: list[tuple] = []
    server = FakePyOcdServer([unsafe], events)
    properties: dict[str, str] = {}
    identity = SimpleNamespace(serial_number="SERIAL-7", vid="0483", pid="374b")
    monkeypatch.setattr(
        usb_reset_support,
        "reset_usb_device",
        lambda _identity: events.append(("reset",)),
    )
    monkeypatch.setattr(
        usb_reset_support,
        "wait_for_usb_device",
        lambda **_kwargs: events.append(("wait",)),
    )

    with pytest.raises(pytest.fail.Exception, match="initial debugger_probes_list"):
        pyocd_recordings.pyocd_discovery_after_usb_reset(
            server,
            SimpleNamespace(project=Path("."), state_root=Path(".")),
            expected_serial="SERIAL-7",
            usb_identity=identity,
            private_values=("SERIAL-7",),
            record_property=lambda name, value: properties.__setitem__(name, value),
        )

    assert [event[0] for event in events] == ["mcp"]
    assert "pyocd_discovery_before_usb_reset_v1" in properties
    assert "pyocd_discovery_after_usb_reset_v1" not in properties
