"""Server jobs keep the bench policy and run unmodified, untrusted plans.

The first positive load in each proof deliberately exposes the current v4
configuration gap. The assertions after it describe the complete job contract.
All hardware replies below are invented, deterministic boundary doubles.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_authoritative_config
from support import scaled_time_bound
from test_hardening import _link_directory

from agentic_hil.config import (
    ConfigError,
    atomic_write_bytes,
    configured_work_path,
    load_authoritative_config,
    project_state_directory,
    validate_config_document,
)
from agentic_hil.configreload import reload_description, restart_required_changes
from agentic_hil.coordination import HardwareCoordinator, project_resource
from agentic_hil.report import canonical_audit_evidence, overall_success
from agentic_hil.types import CURRENT_CONFIG_VERSION

REPO = Path(__file__).resolve().parents[1]
PLAN_NAME = "plans/flash.yaml"
CLAIM = "client-supplied-name"
JOB_FIELDS = ("job_id", "submitted_by", "submitted_by_source")
IMAGE_A = b"\x7fELFserver-job-image-A"
IMAGE_B = b"\x7fELFserver-job-image-B"
LIMIT_DEFAULTS = {
    "max_upload_mb": 64,
    "max_artifact_wait_s": 600,
    "max_job_runtime_s": 900,
    "max_queue_s": 86400,
    "reserve_after_s": 600,
    "max_queued_jobs_per_user": 5,
}


def _bench(tmp_path, monkeypatch):
    workspace = (tmp_path / "bench").resolve()
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=tmp_path / "configuration",
        config_version=4,
        state_root=tmp_path / "trusted-state",
        probe_id="SERVERJOBS-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16],
        com_ports_yaml='com_ports:\n  feedback:\n    device: "COM_SERVERJOBS"\n    resource_id: "serverjobs-feedback"\n',
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["server"] = {"job_root": str((tmp_path / "jobs").resolve())}
    document["artifacts"]["allow_upload"] = False
    _write_document(path, document)
    return workspace, path


def _write_document(path, document):
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _plan(image="build/app.elf", debugger="dut"):
    return yaml.safe_dump({
        "version": 2,
        "name": "server job parity",
        "steps": [{"debugger": debugger, "action": "flash", "image_path": image}],
    }, sort_keys=False).encode()


def _prepare(config, job_id, plan=None, artifacts=None):
    # Lazy import keeps collection independent of the unimplemented interface.
    from agentic_hil.serverjobs import prepare_job

    return prepare_job(
        config,
        job_id=job_id,
        submitted_by=CLAIM,
        plan_name=PLAN_NAME,
        plan_bytes=_plan() if plan is None else plan,
        artifacts={"build/app.elf": IMAGE_A} if artifacts is None else artifacts,
    )


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Backend:
    """Only the debugger transport is replaced; staging and leases stay real."""

    def __init__(self, config):
        self.config = config

    def _record(self, event):
        with Path(os.environ["SERVERJOBS_CAPTURE"]).open("a", encoding="utf-8") as output:
            output.write(json.dumps(event) + "\n")

    def flash_firmware(self, artifact, reset_after_flash=False):
        staged = Path(artifact["resolved_path"])
        data = staged.read_bytes()
        self._record({
            "action": "flash", "hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest(),
            "path": str(staged), "staged": artifact.get("staged"),
            "executable": self.config.debugger.executable,
        })
        if os.environ.get("SERVERJOBS_MODE") in ("attribution", "local-attribution"):
            _exercise_writers(self.config)
        if os.environ.get("SERVERJOBS_MODE") == "unconfirmed":
            return {
                "ok": False, "tool": "flash_firmware", "error_type": "flash_failed",
                "side_effect_status": "unknown", "summary": "Scripted incomplete flash confirmation.",
            }
        return {
            "ok": True, "tool": "flash_firmware", "success_confirmed": True,
            "reset_after_flash": reset_after_flash, "side_effect_committed": True,
            "side_effect_status": "committed", "retry_safe": False,
            "artifact": {key: artifact[key] for key in ("sha256", "size_bytes", "validation")},
        }

    def reset_target(self, mode="run"):
        self._record({"action": "reset", "mode": mode})
        return {"ok": True, "tool": "reset_target", "mode": mode, "success_confirmed": True}

    def probe_target(self):
        self._record({"action": "probe"})
        return {"ok": True, "tool": "probe_target", "target_detected": True}

    def sessionless_debug_tools(self):
        return frozenset()

    def opens_debug_sessions(self):
        return False

    def close(self):
        pass


def _exercise_writers(config):
    """Call existing writers while the worker's runtime context is active."""
    from agentic_hil.comports import ComPortSession
    from agentic_hil.report import append_jsonl_audited, logs_directory, write_report

    logs = Path(logs_directory(config))
    spoof = {} if os.environ.get("SERVERJOBS_MODE") == "local-attribution" else {
        "job_id": "spoof", "submitted_by": "spoof", "submitted_by_source": "spoof",
    }
    assert append_jsonl_audited(config, str(logs / "claimed-event.jsonl"), {"event": "claim", **spoof}) is None
    written = write_report(config, {"ok": True, "tool": "writer-observation", "run": "writer-observation", **spoof})
    assert written["audit_ok"] is True
    session = ComPortSession(
        "feedback", config.com_ports["feedback"], SimpleNamespace(),
        str(logs / "passive-feedback.jsonl"), start_reader=False,
    )
    session.received.append(b"scripted feedback\n")
    assert session.log_received()
    # Feedback keeps its existing workspace-only logging behavior.
    from agentic_hil.report import canonical_audit_log_path

    assert not canonical_audit_log_path(config, session.log_path).exists()


def _install_child_fakes():
    """Loaded only by trusted sitecustomize, never from submitted content."""
    import agentic_hil.report as report
    import agentic_hil.test_reactor as reactor
    import agentic_hil.tools as tools

    tools.create_debugger_backend = _Backend
    # These plans have no wait steps. Freeze just the reactor's elapsed clock,
    # leaving coordination/process liveness and real timeout clocks untouched.
    reactor.time = SimpleNamespace(monotonic=lambda: 0.0)
    if os.environ.get("SERVERJOBS_MODE") == "audit-fault":
        original = report.safe_write_text
        failed = False

        def fail_one_report(config, path, content):
            nonlocal failed
            if not failed and Path(os.environ["SERVERJOBS_CAPTURE"]).exists() and '"tool": "flash_firmware"' in content:
                failed = True
                raise OSError("scripted job report mirror failure")
            return original(config, path, content)

        report.safe_write_text = fail_one_report


def _child_environment(tmp_path, capture, mode):
    bootstrap = tmp_path / "trusted-bootstrap"
    bootstrap.mkdir(exist_ok=True)
    (bootstrap / "sitecustomize.py").write_text(
        "from test_server_jobs import _install_child_fakes\n_install_child_fakes()\n", encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({
        "PYTHONPATH": os.pathsep.join(map(str, (bootstrap, REPO / "tests", REPO / "src", REPO))),
        "SERVERJOBS_CAPTURE": str(capture), "SERVERJOBS_MODE": mode,
        "PYTHONIOENCODING": "utf-8",
    })
    return environment


def _child(tmp_path, argv, cwd, *, label, mode="normal"):
    capture = tmp_path / f"{label}-backend.jsonl"
    process = subprocess.Popen(
        [sys.executable, *argv], cwd=cwd, env=_child_environment(tmp_path, capture, mode),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        stdout, stderr = process.communicate(timeout=scaled_time_bound(30))
        assert "Error in sitecustomize" not in stderr, stderr
        assert process.returncode in (0, 1), (process.returncode, stdout, stderr)
        result = json.loads(stdout)
        assert isinstance(result, dict), (stdout, stderr)
        assert (process.returncode == 0) == overall_success(result), result
        return result, _rows(capture)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=scaled_time_bound(10))


def _worker(tmp_path, bench, job, *, label, mode="normal", plan_name=PLAN_NAME):
    return _child(tmp_path, [
        "-m", "agentic_hil.serverjobs", "--bench-config", bench.config_path,
        "--bench-workspace", bench.work_dir, "--job-id", label,
        "--submitted-by", CLAIM, "--plan", plan_name,
    ], job.work_dir, label=label, mode=mode)


def _local(tmp_path, bench, plan, *, label="local", mode="normal"):
    target = Path(bench.work_dir) / PLAN_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plan)
    return _child(tmp_path, ["-m", "agentic_hil", "test-reactor", "--test-config", PLAN_NAME, "--json"],
                  bench.work_dir, label=label, mode=mode)


# Exact field paths permitted to vary in the reports of these single-step plans.
# '*' denotes a list index, never an arbitrary dictionary key.
_VARIABLE_PATHS = {
    ("run",), ("steps", "*", "result", "run", "id"),
    ("steps", "*", "result", "run", "started_at"),
    ("steps", "*", "result", "lease_id"),
    ("config_in_force", "checked_at"),
    ("config_status", "loaded_at"), ("config_status", "checked_at"),
    ("steps", "*", "result", "config_in_force", "checked_at"),
    ("steps", "*", "result", "config_status", "loaded_at"),
    ("steps", "*", "result", "config_status", "checked_at"),
}


def _normal_report(report, config):
    roots = [(str(project_state_directory(config)), "<project-state>"), (config.work_dir, "<workspace>")]
    run = report.get("run")
    if isinstance(run, str):
        # Run identifiers may also occur in the canonical report filename.
        roots.insert(0, (run, "<run>"))

    def visit(value, path=()):
        if path in _VARIABLE_PATHS and value is not None:
            return "<variable>"
        if isinstance(value, dict):
            return {key: visit(item, (*path, key)) for key, item in value.items() if key not in JOB_FIELDS}
        if isinstance(value, list):
            return [visit(item, (*path, "*")) for item in value]
        if isinstance(value, str):
            for root, token in roots:
                value = value.replace(root, token).replace(root.replace("\\", "/"), token)
        return value

    return visit(report)


def _assert_flash(calls, image, workspace):
    flashes = [call for call in calls if call["action"] == "flash"]
    assert len(flashes) == 1, calls
    call = flashes[0]
    assert bytes.fromhex(call["hex"]) == image
    assert call["sha256"] == hashlib.sha256(image).hexdigest()
    assert call["staged"] is True
    assert not Path(call["path"]).is_relative_to(Path(workspace))
    assert not Path(call["path"]).exists(), "private staging must be released"


def _assert_no_job_fields(value):
    if isinstance(value, dict):
        assert not set(value).intersection(JOB_FIELDS), value
        for item in value.values():
            _assert_no_job_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_job_fields(item)


def _assert_attributed(row, job_id):
    assert {key: row[key] for key in JOB_FIELDS} == {
        "job_id": job_id, "submitted_by": CLAIM, "submitted_by_source": "client",
    }, row


def _assert_job_files(job, result, job_id, *, require_report=True):
    _assert_attributed(result, job_id)
    state = project_state_directory(job)
    reports = list((state / "reports" / "runs").glob("*.json"))
    reports += list(Path(job.reports.directory).glob("*.json"))
    if require_report:
        assert reports, result
    for path in reports:
        _assert_attributed(json.loads(path.read_text(encoding="utf-8")), job_id)
    logs = list(Path(job.logs.directory).glob("*.jsonl")) + list((state / "audit-logs").glob("*.jsonl"))
    for path in logs:
        for row in _rows(path):
            _assert_attributed(row, job_id)


def test_server_configuration_v4(tmp_path, monkeypatch):
    workspace, path = _bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    legacy = copy.deepcopy(raw)
    legacy["version"] = 3
    with pytest.raises(ConfigError) as legacy_validation:
        validate_config_document(legacy, str(path))
    assert legacy_validation.value.error_type == "config_invalid"
    assert legacy_validation.value.details["rejected_fields"] == ["server"]
    assert legacy_validation.value.summary == "Unknown Agentic HIL configuration field `server` at the top level of the file."
    try:
        _write_document(path, legacy)
        with pytest.raises(ConfigError) as legacy_load:
            load_authoritative_config(workspace)
        assert legacy_load.value.error_type == "config_invalid"
        assert legacy_load.value.details["rejected_fields"] == ["server"]
        assert legacy_load.value.summary == "Unknown Agentic HIL configuration field `server` at the top level of the file."
    finally:
        _write_document(path, raw)
    server = config.server
    assert server.listen == "127.0.0.1:8765"
    assert tuple(server.hostnames) == tuple(server.allowed_origins) == ()
    assert Path(server.job_root) == tmp_path / "jobs"
    assert asdict(server.limits) == LIMIT_DEFAULTS
    assert server.retention_days == 14
    with pytest.raises(FrozenInstanceError):
        server.retention_days = 1
    with pytest.raises(FrozenInstanceError):
        server.limits.max_queue_s = 1
    absent = copy.deepcopy(raw)
    absent.pop("server")
    _write_document(path, absent)
    assert load_authoritative_config(workspace).server is None
    assert CURRENT_CONFIG_VERSION == 3

    # Validation here is pure, and must not discover a host or open a listener.
    def no_network(*args, **kwargs):
        raise AssertionError("server validation attempted network access")

    with monkeypatch.context() as network:
        import socket

        network.setattr(socket, "getaddrinfo", no_network)
        network.setattr(socket, "socket", no_network)
        for listen in ("example.invalid:1", "127.0.0.1:65535", "[::1]:8765"):
            accepted = copy.deepcopy(raw)
            accepted["server"].update({
                "listen": listen, "hostnames": ["bench.invalid"],
                "allowed_origins": ["https://client.invalid"], "retention_days": 1,
                "limits": {key: index + 1 for index, key in enumerate(LIMIT_DEFAULTS)},
            })
            accepted["server"]["limits"]["reserve_after_s"] = 0
            validate_config_document(accepted, str(path))
            _write_document(path, accepted)
            parsed = load_authoritative_config(workspace).server
            assert parsed.listen == listen
            assert tuple(parsed.hostnames) == ("bench.invalid",)
            assert tuple(parsed.allowed_origins) == ("https://client.invalid",)
            assert parsed.retention_days == 1
            assert asdict(parsed.limits) == accepted["server"]["limits"]

    invalid = []
    invalid += [{key: value for key, value in raw["server"].items() if key != "job_root"}]
    invalid += [{**raw["server"], "job_root": "relative/jobs"}]
    invalid += [{**raw["server"], "unknown": True}, {**raw["server"], "limits": {"unknown": 1}}]
    invalid += [{**raw["server"], "listen": value} for value in ("localhost", "a:0", "a:65536", "a:abc", "[::1]", "a:-1")]
    for field in (*LIMIT_DEFAULTS, "retention_days"):
        for value in (True, 1.5, -1, *(() if field == "reserve_after_s" else (0,))):
            section = copy.deepcopy(raw["server"])
            if field == "retention_days":
                section[field] = value
            else:
                section["limits"] = {field: value}
            invalid.append(section)
    for section in invalid:
        candidate = {**raw, "server": section}
        with pytest.raises(ConfigError) as refused:
            validate_config_document(candidate, str(path))
        assert refused.value.error_type == "config_invalid"
    # Unknown nested keys use the same schema refusal form as other unknowns.
    for container, section in (
        ("server", {**raw["server"], "unknown": 1}),
        ("server.limits", {**raw["server"], "limits": {"unknown": 1}}),
    ):
        with pytest.raises(ConfigError) as refused:
            validate_config_document({**raw, "server": section}, str(path))
        assert refused.value.error_type == "config_invalid"
        assert refused.value.summary == f"Unknown Agentic HIL configuration field `unknown` under {container}."
        assert refused.value.details["rejected_fields"] == ["unknown"]

    changed = copy.deepcopy(raw)
    changed["server"]["retention_days"] = 2
    _write_document(path, changed)
    disk = load_authoritative_config(workspace)
    assert "server.retention_days" in restart_required_changes(config, disk)
    reloaded, result = reload_description(config)
    assert result["ok"] is True, result
    assert "server.retention_days" in result["restart_required_for"]
    assert reloaded.server == config.server


def test_job_worker_matches_local_plan(tmp_path, monkeypatch):
    workspace, path = _bench(tmp_path, monkeypatch)
    bench = load_authoritative_config(workspace)
    plan = _plan()
    job = _prepare(bench, "parity", plan)
    assert Path(job.work_dir) == Path(bench.server.job_root) / "parity" / "workspace"
    assert job.workspace_root == job.work_dir
    for name in ("debuggers", "debugger", "debug", "permissions", "validation", "state_root", "config_path", "config_digest", "permissions_digest"):
        assert getattr(job, name) == getattr(bench, name), name
    assert job.artifacts.allow_upload is bench.artifacts.allow_upload is False
    assert job.artifacts.allowed_roots == [str(Path(job.work_dir) / "build")]
    assert Path(job.artifacts.upload_directory) == Path(job.work_dir) / ".agentic-hil/artifacts"
    assert Path(job.reports.directory) == Path(job.work_dir) / ".agentic-hil/reports"
    assert Path(job.logs.directory) == Path(job.work_dir) / ".agentic-hil/logs"
    # This would fail to load and would grant a different probe if treated as policy.
    embedded_config = Path(job.work_dir) / ".agentic-hil/config.yaml"
    embedded_config.parent.mkdir(parents=True, exist_ok=True)
    embedded_config.write_text("version: invalid\npermissions: {allow_flash: true}\n", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build/app.elf").write_bytes(IMAGE_A)
    remote, calls = _worker(tmp_path, bench, job, label="parity")
    local, local_calls = _local(tmp_path, bench, plan)
    assert overall_success(remote), remote
    assert overall_success(local), local
    assert _normal_report(remote, job) == _normal_report(local, bench)
    _assert_flash(calls, IMAGE_A, job.work_dir)
    _assert_flash(local_calls, IMAGE_A, workspace)
    assert calls[0]["executable"] == bench.debugger.executable
    assert (Path(job.work_dir) / PLAN_NAME).read_bytes() == plan
    assert remote["test_config_sha256"] == hashlib.sha256(plan).hexdigest()

    for label, candidate in (("unknown-device", _plan(debugger="undeclared")), ("permission", plan)):
        if label == "permission":
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            raw["debuggers"]["dut"]["permissions"]["allow_flash"] = False
            _write_document(path, raw)
            bench = load_authoritative_config(workspace)
        prepared = _prepare(bench, label, candidate)
        refused, calls = _worker(tmp_path, bench, prepared, label=label)
        local_refused, local_calls = _local(tmp_path, bench, candidate, label="local-" + label)
        assert not refused["ok"] and not local_refused["ok"]
        assert not calls and not local_calls
        assert _normal_report(refused, prepared) == _normal_report(local_refused, bench)

    from agentic_hil.serverjobs import prepare_job

    # Test the derived workspace, without imposing an extra job_root overlap rule.
    for label, bad_root in (("state-overlap", Path(bench.state_root)), ("config-inside", path.parent)):
        invalid_bench = replace(bench, server=replace(bench.server, job_root=str(bad_root)))
        if label == "config-inside":
            # The named authoritative file is inside this prospective workspace.
            target = bad_root / label / "workspace"
            invalid_bench = replace(invalid_bench, config_path=str(target / "config.yaml"))
        target = bad_root / label / "workspace"
        assert not target.exists()
        with pytest.raises(ConfigError):
            prepare_job(invalid_bench, job_id=label, submitted_by=CLAIM, plan_name=PLAN_NAME,
                        plan_bytes=plan, artifacts={"build/app.elf": IMAGE_A})
        assert not target.exists()


@pytest.mark.parametrize("case", ["traversal", "absolute_outside", "link"], ids=["traversal", "absolute_outside", "link"])
def test_job_artifact_refusals_match_local(tmp_path, monkeypatch, case):
    workspace, _ = _bench(tmp_path, monkeypatch)
    bench = load_authoritative_config(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.elf").write_bytes(IMAGE_A)
    image = {"traversal": "../outside/app.elf", "absolute_outside": str(outside / "app.elf"), "link": "build/link/app.elf"}[case]
    plan = _plan(image)
    job = _prepare(bench, case, plan)
    if case == "link":
        (workspace / "build").mkdir(exist_ok=True)
        _link_directory(workspace / "build/link", outside)
        _link_directory(Path(job.work_dir) / "build/link", outside)
    remote, calls = _worker(tmp_path, bench, job, label=case)
    local, local_calls = _local(tmp_path, bench, plan)
    assert not remote["ok"] and not local["ok"]
    assert not calls and not local_calls
    assert _normal_report(remote, job) == _normal_report(local, bench)
    if case in ("traversal", "absolute_outside"):
        # Use absent targets, separate from the files used by plan preflight.
        placement_name = "../outside/placement.elf" if case == "traversal" else str(outside / "placement.elf")
        placement_id = "placement-" + case
        placement_workspace = Path(bench.server.job_root) / placement_id / "workspace"
        local_target = configured_work_path(bench, placement_name)
        job_target = Path(os.path.abspath(placement_workspace / placement_name))
        assert not local_target.exists() and not job_target.exists()
        with pytest.raises(ConfigError) as local_placement:
            atomic_write_bytes(local_target, IMAGE_A, workspace=workspace)
        with pytest.raises(ConfigError) as job_placement:
            _prepare(bench, placement_id, artifacts={placement_name: IMAGE_A})
        assert not local_target.exists() and not job_target.exists()
        local_refusal = local_placement.value.to_dict()
        job_refusal = job_placement.value.to_dict()
        # Check each absolute target before normalizing only this workspace path
        # back to the submitted name. Every other refusal field stays intact.
        assert local_refusal["path"] == str(local_target)
        assert job_refusal["path"] == str(job_target)
        local_refusal["path"] = job_refusal["path"] = placement_name
        assert job_refusal == local_refusal
    if case == "absolute_outside":
        inside_id = "absolute-inside"
        inside = Path(bench.server.job_root) / inside_id / "workspace/build/app.elf"
        accepted = _prepare(bench, inside_id, _plan(str(inside)))
        success, accepted_calls = _worker(tmp_path, bench, accepted, label=inside_id)
        (workspace / "build").mkdir(exist_ok=True)
        local_image = workspace / "build/app.elf"
        local_image.write_bytes(IMAGE_A)
        local_success, accepted_local_calls = _local(tmp_path, bench, _plan(str(local_image)), label="local-inside")
        assert overall_success(success) and overall_success(local_success)
        _assert_flash(accepted_calls, IMAGE_A, accepted.work_dir)
        _assert_flash(accepted_local_calls, IMAGE_A, workspace)


def test_job_workspaces_isolate_same_relative_artifact(tmp_path, monkeypatch):
    workspace, _ = _bench(tmp_path, monkeypatch)
    bench = load_authoritative_config(workspace)
    first = _prepare(bench, "first")
    second = _prepare(bench, "second", artifacts={"build/app.elf": IMAGE_B})
    first_result, first_calls = _worker(tmp_path, bench, first, label="first")
    second_result, second_calls = _worker(tmp_path, bench, second, label="second")
    assert overall_success(first_result) and overall_success(second_result)
    _assert_flash(first_calls, IMAGE_A, first.work_dir)
    _assert_flash(second_calls, IMAGE_B, second.work_dir)
    assert first.work_dir != second.work_dir
    assert project_state_directory(first) != project_state_directory(second)
    assert first_result["canonical_report_path"] != second_result["canonical_report_path"]
    assert first.reports.directory != second.reports.directory
    assert (Path(first.work_dir) / "build/app.elf").read_bytes() == IMAGE_A
    assert (Path(second.work_dir) / "build/app.elf").read_bytes() == IMAGE_B
    _assert_job_files(first, first_result, "first")
    _assert_job_files(second, second_result, "second")
    cross = _prepare(bench, "cross", _plan(str(Path(first.work_dir) / "build/app.elf")))
    refused, calls = _worker(tmp_path, bench, cross, label="cross")
    assert not refused["ok"] and not calls
    assert refused["validation_error"]["validation"]["error_type"] == "artifact_validation_failed"
    with pytest.raises((ConfigError, FileExistsError)):
        _prepare(bench, "first")
    assert (Path(first.work_dir) / "build/app.elf").read_bytes() == IMAGE_A


def _incident_scenario(tmp_path, bench):
    broken_job = _prepare(bench, "broken")
    broken, calls = _worker(tmp_path, bench, broken_job, label="broken", mode="audit-fault")
    assert calls and not overall_success(broken)
    assert broken["audit_ok"] is False or broken.get("quarantined") is True
    coordinator = HardwareCoordinator(bench, "test-reader")
    status = coordinator.status()
    assert status["incident_stands"] is True, status
    records = [json.loads(p.read_text(encoding="utf-8")) for p in coordinator.record_directory.glob("*.json")]
    standing = [record for record in records if record.get("state") in ("quarantined", "cleanup_required")]
    assert standing, records
    for record in standing:
        assert record["project_resource"] == project_resource(bench)
        assert record["workspace"] == str(Path(bench.work_dir).resolve())
        assert record["config_path"] == str(Path(bench.config_path).resolve())
        _assert_no_job_fields(record)
    blocked_job = _prepare(bench, "blocked")
    blocked, blocked_calls = _worker(tmp_path, bench, blocked_job, label="blocked")
    assert blocked["error_type"] == "resource_quarantined", blocked
    assert not blocked_calls
    quarantine_id = next(record["quarantine_id"] for record in standing)
    assert blocked.get("quarantine_id") == quarantine_id or quarantine_id in json.dumps(blocked)
    ledger = coordinator.root / "recovery.jsonl"
    before = _rows(ledger)
    recovered, recovery_calls = _child(tmp_path, [
        "-m", "agentic_hil", "recover", "--confirm-safe-state", "--quarantine-id", quarantine_id, "--json",
    ], bench.work_dir, label="bench-recover")
    assert overall_success(recovered), recovered
    assert not recovery_calls
    recovery_lines = _rows(ledger)[len(before):]
    assert recovery_lines
    _assert_no_job_fields(recovered)
    _assert_no_job_fields(recovery_lines)
    after_job = _prepare(bench, "after")
    after, after_calls = _worker(tmp_path, bench, after_job, label="after")
    assert overall_success(after), after
    _assert_flash(after_calls, IMAGE_A, after_job.work_dir)
    return [(broken_job, broken, "broken"), (blocked_job, blocked, "blocked"), (after_job, after, "after")]


def test_job_incident_is_recovered_from_bench(tmp_path, monkeypatch):
    workspace, _ = _bench(tmp_path, monkeypatch)
    bench = load_authoritative_config(workspace)
    _incident_scenario(tmp_path, bench)


def test_job_worker_attribution_is_scoped(tmp_path, monkeypatch):
    workspace, _ = _bench(tmp_path, monkeypatch)
    bench = load_authoritative_config(workspace)
    job = _prepare(bench, "attribution")
    result, calls = _worker(tmp_path, bench, job, label="attribution", mode="attribution")
    assert overall_success(result), result
    _assert_flash(calls, IMAGE_A, job.work_dir)
    _assert_job_files(job, result, "attribution")
    event_log = Path(job.logs.directory) / "claimed-event.jsonl"
    event_rows = _rows(event_log)
    assert len(event_rows) == 1
    _assert_attributed(event_rows[0], "attribution")
    feedback = _rows(Path(job.logs.directory) / "passive-feedback.jsonl")
    assert len(feedback) == 1 and feedback[0]["direction"] == "rx"
    _assert_attributed(feedback[0], "attribution")
    evidence = canonical_audit_evidence(job, event_log)
    assert evidence["canonical_log_present"] is evidence["workspace_log_verified"] is True
    chain = "0" * 64
    for line in event_log.read_bytes().splitlines(keepends=True):
        chain = hashlib.sha256(chain.encode() + line).hexdigest()
    assert evidence["log_chain_sha256"] == chain
    assert evidence["log_sequence"] == len(event_rows)

    failed_job = _prepare(bench, "preflight", _plan("../outside.elf"))
    refused, failed_calls = _worker(tmp_path, bench, failed_job, label="preflight")
    assert not refused["ok"] and not failed_calls
    _assert_job_files(failed_job, refused, "preflight")
    startup_job = _prepare(bench, "startup", b"invalid: [\n")
    startup, startup_calls = _worker(tmp_path, bench, startup_job, label="startup")
    assert not startup["ok"] and not startup_calls
    _assert_job_files(startup_job, startup, "startup", require_report=False)

    recovery_job = _prepare(bench, "worker-recovery")
    coordinator = HardwareCoordinator(bench, "test-reader")
    ledger = coordinator.root / "recovery.jsonl"
    before = len(_rows(ledger))
    recovered, recovery_calls = _worker(tmp_path, bench, recovery_job, label="worker-recovery", mode="unconfirmed")
    assert not recovered["ok"]
    assert {call["action"] for call in recovery_calls} >= {"flash", "reset", "probe"}
    recovery_rows = _rows(ledger)[before:]
    assert recovery_rows
    for row in recovery_rows:
        _assert_attributed(row, "worker-recovery")
    _assert_job_files(recovery_job, recovered, "worker-recovery")
    for prepared, report, job_id in _incident_scenario(tmp_path, bench):
        _assert_job_files(prepared, report, job_id)

    (workspace / "build").mkdir(exist_ok=True)
    (workspace / "build/app.elf").write_bytes(IMAGE_A)
    local, _ = _local(tmp_path, bench, _plan(), mode="local-attribution")
    assert overall_success(local), local
    _assert_no_job_fields(local)
    for path in (project_state_directory(bench) / "reports/runs").glob("*.json"):
        _assert_no_job_fields(json.loads(path.read_text(encoding="utf-8")))
    for path in Path(bench.logs.directory).glob("*.jsonl"):
        _assert_no_job_fields(_rows(path))
