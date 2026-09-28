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


class FakeLeaseStatusServer:
    def __init__(self, results: list[object]) -> None:
        self.results = iter(results)
        self.calls: list[str] = []

    def try_call(self, name: str, arguments: dict | None = None) -> object:
        self.calls.append(name)
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


def test_pyocd_run_stop_captures_sanitized_statuses_and_product_recovery() -> None:
    server = FakeLeaseStatusServer(
        [
            {
                "ok": True,
                "audit_ok": True,
                "cleanup_required": True,
                "quarantined": True,
                "lease_state": "cleanup_required",
                "blocked": True,
                "incident_stands": False,
                "standing_incidents": [{"probe_id": "STLINK123", "summary": "incident at /dev/ttyACM0"}],
                "cleanup_reasons": ["target_probe_timeout"],
                "quarantine_guidance": [{"summary": "inspect STLINK123 at /dev/ttyACM0"}],
                "auto_recoverable": True,
                "auto_recover_policy": "reset_halt",
                "quarantine_id": "STLINK123-incident",
                "private_extra": "must not be copied",
            },
            {
                "ok": True,
                "released_devices": ["debugger:dut", "uart:uart"],
                "recovery": {
                    "attempted": True,
                    "actions": ["reap_processes", "reset_halt", "probe_target"],
                    "outcome": "recovered",
                    "auto_recover_policy": "reset_halt",
                    "failed_action": None,
                    "incident_resolved": True,
                    "incident_open": False,
                    "summary": "recovered STLINK123 at /dev/ttyACM0",
                    "private_extra": "must not be copied",
                },
            },
            {
                "ok": True,
                "audit_ok": True,
                "cleanup_required": False,
                "quarantined": False,
                "lease_state": "released",
                "blocked": False,
                "auto_recoverable": False,
                "auto_recover_policy": "reset_halt",
            },
        ]
    )

    evidence = pyocd_recordings.capture_run_stop_with_lease_evidence(
        server,
        ("STLINK123", "/dev/ttyACM0"),
    )

    assert server.calls == ["hardware_lease_status", "bench_run_stop", "hardware_lease_status"]
    assert evidence["lease_status_before_stop"]["available"] is True
    assert evidence["lease_status_before_stop"]["auto_recover_policy"] == "reset_halt"
    assert evidence["lease_status_before_stop"]["quarantine_guidance"] == [
        {"summary": "inspect [redacted] at [redacted]"}
    ]
    assert evidence["lease_status_after_stop"]["lease_state"] == "released"
    assert evidence["run_stop"]["recovery"] == {
        "attempted": True,
        "actions": ["reap_processes", "reset_halt", "probe_target"],
        "outcome": "recovered",
        "auto_recover_policy": "reset_halt",
        "failed_action": None,
        "incident_resolved": True,
        "incident_open": False,
        "summary": "recovered [redacted] at [redacted]",
    }
    assert pyocd_recordings.run_stop_succeeded(evidence["run_stop"]) is True
    assert "STLINK123" not in json.dumps(evidence)
    assert "/dev/ttyACM0" not in json.dumps(evidence)
    assert "private_extra" not in json.dumps(evidence)


def test_pyocd_run_stop_keeps_a_failed_recovery_verdict_even_if_stop_closed_the_run() -> None:
    server = FakeLeaseStatusServer(
        [
            {"ok": True, "blocked": True, "auto_recoverable": True},
            {
                "ok": True,
                "released_devices": ["debugger:dut"],
                "recovery": {
                    "audit_ok": False,
                    "cleanup_ok": True,
                    "cleanup_required": True,
                    "quarantined": True,
                    "lease_state": "cleanup_required",
                    "side_effect_status": "partial",
                    "hardware_state": "unknown",
                    "attempted": True,
                    "actions": ["reap_processes", "reset_halt"],
                    "outcome": "failed",
                    "failed_action": "reset_halt",
                    "failed_check": "audit_ok",
                    "incident_open": True,
                    "summary": "reset could not be confirmed",
                },
            },
            {"ok": True, "blocked": True, "incident_stands": True, "quarantined": True},
        ]
    )

    evidence = pyocd_recordings.capture_run_stop_with_lease_evidence(server, ())

    assert evidence["run_stop"]["ok"] is True
    assert evidence["run_stop"]["recovery"]["outcome"] == "failed"
    assert evidence["run_stop"]["recovery"]["failed_action"] == "reset_halt"
    assert evidence["run_stop"]["recovery"]["failed_check"] == "audit_ok"
    assert evidence["run_stop"]["recovery"]["audit_ok"] is False
    assert evidence["run_stop"]["recovery"]["side_effect_status"] == "partial"
    assert evidence["lease_status_after_stop"]["incident_stands"] is True
    assert pyocd_recordings.run_stop_succeeded(evidence["run_stop"]) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ok", False),
        ("target_ok", False),
        ("audit_ok", False),
        ("cleanup_ok", False),
        ("cleanup_required", True),
        ("quarantined", True),
        ("lease_state", "stale"),
        ("side_effect_status", "unknown"),
        ("side_effect_status", "partial"),
        ("hardware_state", "unknown"),
    ],
)
def test_pyocd_run_stop_uses_the_product_continue_predicate(field: str, value: object) -> None:
    result = {
        "ok": True,
        "target_ok": None,
        "audit_ok": True,
        "cleanup_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "lease_state": "released",
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
    }
    result[field] = value

    evidence = pyocd_recordings.run_stop_evidence(result, ())

    assert pyocd_recordings.run_stop_succeeded(evidence) is False


def test_pyocd_run_stop_accepts_recovered_outcome_when_incident_is_closed() -> None:
    result = {
        "ok": True,
        "recovery": {
            "outcome": "recovered",
            "incident_resolved": False,
            "incident_open": False,
        },
    }

    assert pyocd_recordings.run_stop_succeeded(pyocd_recordings.run_stop_evidence(result, ())) is True


def test_pyocd_run_stop_capture_keeps_after_status_and_original_failure_if_stop_raises() -> None:
    server = FakeLeaseStatusServer(
        [
            {"ok": True, "lease_state": "active"},
            RuntimeError("stop call failed for /dev/ttyACM0"),
            {"ok": True, "lease_state": "released"},
        ]
    )
    captured = None

    with pytest.raises(AssertionError, match="original test failure"):
        try:
            raise AssertionError("original test failure")
        finally:
            captured = pyocd_recordings.capture_run_stop_with_lease_evidence(server, ("/dev/ttyACM0",))

    assert server.calls == ["hardware_lease_status", "bench_run_stop", "hardware_lease_status"]
    assert captured["lease_status_after_stop"]["lease_state"] == "released"
    assert captured["run_stop"] == {
        "available": False,
        "unavailable_reason": "run_stop_call_failed",
        "error": "RuntimeError: stop call failed for [redacted]",
        "recovery": None,
    }
    assert pyocd_recordings.run_stop_succeeded(captured["run_stop"]) is False


def _clean_run_closure_evidence() -> dict:
    return {
        "run_stop": {
            "available": True,
            "ok": True,
            "target_ok": None,
            "audit_ok": True,
            "cleanup_ok": True,
            "cleanup_required": False,
            "quarantined": False,
            "lease_state": "released",
            "side_effect_status": "unchanged",
            "hardware_state": "unchanged",
            "released_devices": ["debugger:dut", "uart:uart"],
            "recovery": None,
        },
        "lease_status_after_stop": {
            "available": True,
            "ok": True,
            "target_ok": None,
            "audit_ok": True,
            "cleanup_ok": True,
            "cleanup_required": False,
            "quarantined": False,
            "lease_state": "released",
            "side_effect_status": "unchanged",
            "hardware_state": "unchanged",
            "blocked": False,
            "incident_stands": False,
            "standing_incidents": [],
            "cleanup_reasons": [],
            "auto_recoverable": False,
            "auto_recover_policy": "reset_halt",
        },
    }


def test_pyocd_run_closure_accepts_clean_after_status_and_successful_stop() -> None:
    evidence = _clean_run_closure_evidence()

    assert pyocd_recordings.run_closure_succeeded(evidence) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available", False),
        ("ok", False),
        ("target_ok", False),
        ("audit_ok", False),
        ("cleanup_ok", False),
        ("cleanup_required", True),
        ("quarantined", True),
        ("lease_state", "stale"),
        ("side_effect_status", "unknown"),
        ("side_effect_status", "partial"),
        ("hardware_state", "unknown"),
        ("blocked", True),
        ("incident_stands", True),
        ("standing_incidents", [{"summary": "unresolved incident"}]),
    ],
)
def test_pyocd_run_closure_rejects_unclean_after_status(field: str, value: object) -> None:
    evidence = _clean_run_closure_evidence()
    evidence["lease_status_after_stop"][field] = value

    assert pyocd_recordings.run_closure_succeeded(evidence) is False


@pytest.mark.parametrize(
    "failed_status",
    [None, {"ok": False, "error_type": "backend_error", "summary": "status unavailable"}, OSError("private path /dev/ttyACM0")],
)
def test_pyocd_run_stop_marks_failed_status_unavailable_without_skipping_stop(failed_status) -> None:
    server = FakeLeaseStatusServer(
        [
            failed_status,
            {"ok": True, "released_devices": [], "recovery": {"attempted": False, "outcome": "skipped"}},
            failed_status,
        ]
    )

    evidence = pyocd_recordings.capture_run_stop_with_lease_evidence(server, ("/dev/ttyACM0",))

    assert server.calls == ["hardware_lease_status", "bench_run_stop", "hardware_lease_status"]
    assert evidence["lease_status_before_stop"]["available"] is False
    assert evidence["run_stop"]["ok"] is True
    assert evidence["lease_status_after_stop"]["available"] is False
    assert "/dev/ttyACM0" not in json.dumps(evidence)


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
