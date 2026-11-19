"""Serve startup boundaries and the server-only cross-account lock warning."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import write_authoritative_config
from httpserver_fakes import ScriptedJobService, patch_seam, short_settings

from agentic_hil import bench, cli, httpserver
from agentic_hil.httpapi import JobService, ServerSettings
from agentic_hil.types import AgenticHILConfig, JsonObject


def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    path = write_authoritative_config(workspace, monkeypatch, probe_id="TESTPROBE-HTTP", config_version=2)
    monkeypatch.chdir(workspace)
    return path


def observe_no_start(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    factory = Mock(side_effect=AssertionError("Service must not open on a refused start"))
    constructor = Mock(side_effect=AssertionError("Listener must not bind on a refused start"))
    patch_seam(monkeypatch, "open_job_service", factory)
    monkeypatch.setattr(httpserver, "create_http_server", constructor)
    if hasattr(cli, "create_http_server"):
        monkeypatch.setattr(cli, "create_http_server", constructor)
    return factory, constructor


def test_serve_refuses_without_server_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = isolated_config(tmp_path, monkeypatch)
    factory, constructor = observe_no_start(monkeypatch)
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))

    exit_code = cli.entrypoint(["--json", "serve"])

    assert exit_code != 0
    output = capsys.readouterr()
    result = json.loads(output.out or output.err)
    assert result["ok"] is False
    assert result["error_type"] == "server_not_configured"
    assert result.get("remediation")
    assert str(path) in result["summary"] or result.get("config_path") == str(path)
    summary = result["summary"].lower()
    assert "server" in summary and "block" in summary and "job" in summary
    factory.assert_not_called()
    constructor.assert_not_called()
    assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*")) == before


def test_serve_refuses_non_loopback_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    isolated_config(tmp_path, monkeypatch)
    settings = ServerSettings(host="0.0.0.0")
    patch_seam(monkeypatch, "load_server_settings", lambda config: settings)
    factory, constructor = observe_no_start(monkeypatch)

    exit_code = cli.entrypoint(["--json", "serve"])

    assert exit_code != 0
    output = capsys.readouterr()
    result = json.loads(output.out or output.err)
    assert result["ok"] is False
    assert result["error_type"] == "server_listen_refused"
    assert result.get("remediation")
    assert "#891" in json.dumps(result)
    factory.assert_not_called()
    constructor.assert_not_called()


def recorded_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated doctor observations deterministic and contact no device."""
    monkeypatch.setattr(cli, "_doctor_state_root", lambda config: {"ok": True, "summary": "Recorded state root."})
    monkeypatch.setattr(cli, "_doctor_report_state", lambda config: {"ok": True, "summary": "Recorded report state."})
    monkeypatch.setattr(cli, "_doctor_bench_binding", lambda config: {"ok": True, "summary": "Recorded binding."})
    monkeypatch.setattr(cli, "_doctor_probe_check", lambda config, name: (
        {"ok": True, "summary": "Recorded probe ready."}, {"ok": True, "status": "supported"},
    ))
    monkeypatch.setattr(cli, "probe_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "port_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "standing_foreign_incidents", lambda *args: [])
    monkeypatch.setattr(cli, "config_status", lambda config: {"state": "unchanged", "summary": "Recorded configuration unchanged."})
    monkeypatch.setattr(cli, "config_devices", lambda config: SimpleNamespace(warnings=lambda: []))
    monkeypatch.setattr(cli, "missing_configured_extras", lambda config: None)
    monkeypatch.setattr(cli, "_doctor_installation_report", lambda: {"recorded": True})
    monkeypatch.setattr(cli, "_doctor_mcp_report", lambda: {"recorded": True})
    monkeypatch.setattr(cli, "_doctor_debugger_scripts", lambda entry: {"ok": True, "summary": "Recorded scripts."})


def normalize_doctor(value: object, config_path: Path, home_lock_root: Path) -> object:
    """Replace only the fixture's config and home-lock paths, including in text."""
    if isinstance(value, str):
        return value.replace(str(config_path), "<config>").replace(str(home_lock_root), "<home-locks>")
    if isinstance(value, dict):
        return {key: normalize_doctor(item, config_path, home_lock_root) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_doctor(item, config_path, home_lock_root) for item in value]
    return value


# Frozen from doctor's current return structure for recorded_doctor's observations.
# Only the two explicitly normalized fixture paths vary across hosts.
DOCTOR_WITHOUT_SERVER: JsonObject = {
    "ok": True,
    "tool": "agentic_hil_doctor",
    "summary": "Agentic HIL configuration loaded and 1 debugger(s) checked.",
    "config_status": {
        "state": "unchanged", "summary": "Recorded configuration unchanged.",
        "compare_with_running_server": cli.RUNNING_SERVER_COMPARISON,
    },
    "config_path": "<config>",
    "state_root": {"ok": True, "summary": "Recorded state root."},
    "report_state": {"ok": True, "summary": "Recorded report state."},
    "bench_binding": {"ok": True, "summary": "Recorded binding."},
    "device_locks": {
        "ok": True, "root": "home", "path": "<home-locks>",
        "summary": "Device locks are taken in this account's <home-locks>; another account on this machine does not see them.",
    },
    "unhealthy": [], "standing_incidents": [],
    "installation": {"recorded": True}, "mcp": {"recorded": True},
    "target": {"name": "example-target", "controller": "stm32f4"},
    "debuggers": {
        "dut": {
            "type": "openocd", "probe_id": "TESTPROBE-HTTP", "bound": True,
            "permissions": {
                "allow_flash": True, "allow_reset": True, "allow_debug_execution": True,
                "allow_raw_debugger_commands": False, "allow_mass_erase": False,
            },
            "scripts": {"ok": True, "summary": "Recorded scripts."},
            "check": {"ok": True, "summary": "Recorded probe ready."},
            "target_support": {"ok": True, "status": "supported"},
        },
    },
    "com_ports": {}, "can_buses": {},
    "debugger": {"ok": True, "summary": "Recorded probe ready."},
}


def warning_text(value: object) -> str:
    return value.lower() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).lower()


def assert_mutual_lock_warning(text: str) -> None:
    """Assert the warning's meaning without prescribing its prose or placement."""
    assert "other account" in text and "server" in text and "lock" in text
    assert "does not see" in text or "do not see" in text or "not visible" in text
    assert "server's" in text or "server’s" in text
    assert "theirs" in text or "their locks" in text or "other accounts' locks" in text or "other accounts’ locks" in text
    assert bench.MACHINE_DEVICE_LOCK_SETUP.lower() in text


def test_server_lock_warning_at_startup_and_in_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = isolated_config(tmp_path, monkeypatch)
    settings = short_settings()
    service = ScriptedJobService()
    original_constructor = httpserver.create_http_server
    original_serve_forever = ThreadingHTTPServer.serve_forever
    opened: list[ThreadingHTTPServer] = []
    lifecycle: list[str] = []

    @contextmanager
    def factory(config: AgenticHILConfig, received: ServerSettings) -> Iterator[JobService]:
        assert received == settings
        lifecycle.append("entered")
        try:
            yield service
        finally:
            lifecycle.append("exited")

    def construct(received: ServerSettings, backend: JobService) -> ThreadingHTTPServer:
        assert received == settings and backend is service
        server = original_constructor(received, backend)
        opened.append(server)
        return server

    def interrupt(server: ThreadingHTTPServer, poll_interval: float = 0.5) -> None:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_port != 0
        # Enter the real loop once so its shutdown event has the same state as
        # after a real Ctrl+C. No client or timed sleep decides the interruption.
        server.service_actions = Mock(side_effect=KeyboardInterrupt)
        original_serve_forever(server, poll_interval=0)

    patch_seam(monkeypatch, "load_server_settings", lambda config: settings)
    patch_seam(monkeypatch, "open_job_service", factory)
    monkeypatch.setattr(httpserver, "create_http_server", construct)
    if hasattr(cli, "create_http_server"):
        monkeypatch.setattr(cli, "create_http_server", construct)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", interrupt)
    monkeypatch.setattr(bench, "machine_lock_root_applies", lambda: True)
    monkeypatch.setattr(bench, "machine_device_lock_root", lambda: None)
    monkeypatch.setattr(cli, "machine_device_lock_root", lambda: None)

    try:
        exit_code = cli.entrypoint(["--json", "serve"])
        assert exit_code == 0
        output = capsys.readouterr()
        assert_mutual_lock_warning((output.out + output.err).lower())
        assert lifecycle == ["entered", "exited"]
        assert len(opened) == 1 and opened[0].socket.fileno() == -1
        assert service.calls == []

        recorded_doctor(monkeypatch)
        report = cli.doctor()
        assert_mutual_lock_warning(warning_text(report))

        machine = tmp_path / "shared-device-locks"
        monkeypatch.setattr(bench, "machine_device_lock_root", lambda: machine)
        monkeypatch.setattr(cli, "machine_device_lock_root", lambda: machine)
        exit_code = cli.entrypoint(["--json", "serve"])
        assert exit_code == 0
        output = capsys.readouterr()
        assert bench.MACHINE_DEVICE_LOCK_SETUP not in output.out + output.err
        assert lifecycle == ["entered", "exited", "entered", "exited"]
        assert len(opened) == 2 and all(server.socket.fileno() == -1 for server in opened)
        report = cli.doctor()
        assert bench.MACHINE_DEVICE_LOCK_SETUP not in json.dumps(report)
        assert report.get("warnings", []) == []

        patch_seam(monkeypatch, "load_server_settings", lambda config: None)
        monkeypatch.setattr(bench, "machine_device_lock_root", lambda: None)
        monkeypatch.setattr(cli, "machine_device_lock_root", lambda: None)
        home_locks = Path.home() / bench.BENCH_ROOT_NAME / bench.DEVICE_LOCK_DIRECTORY_NAME
        assert normalize_doctor(cli.doctor(), config_path, home_locks) == deepcopy(DOCTOR_WITHOUT_SERVER)
    finally:
        for server in opened:
            server.server_close()
