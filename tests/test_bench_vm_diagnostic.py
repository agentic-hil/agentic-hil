"""Contracts for the manually invoked, read-only bench VM diagnostic."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "bench-gate.yml"
HELPER = ROOT / "tools" / "bench_vm_diagnostic.py"
EXPECTED_ARCHIVE_SHA256 = "6a9e60a5a048c45eb3241f9bb66bdc2e6cbd0119fb2e42568dc059fc6167442a"


def _load_helper():
    assert HELPER.is_file(), "diagnostic helper has not been implemented"
    spec = importlib.util.spec_from_file_location("bench_vm_diagnostic", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_has_explicit_read_only_dispatch_mode_and_optional_asset_transfer():
    source = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    dispatch = workflow["on"]["workflow_dispatch"]
    inputs = dispatch["inputs"]

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert inputs["diagnose_only"]["type"] == "boolean"
    assert inputs["diagnose_only"]["default"] == "false"
    assert inputs["cubeprogrammer_asset_id"]["required"] == "false"
    assert "nucleo-f446re-bench" in source
    assert "cancel-in-progress: false" in source
    assert "contents: read" in source
    assert "agentic-hil/agentic-hil" in source
    assert "bench_vm_diagnostic.py" in source
    assert "github.token" in source


def test_usb_diagnostic_matches_one_allowlisted_probe_without_emitting_identity_or_writing():
    helper = _load_helper()
    probe_id = "SERIAL-MUST-NOT-LEAK"
    probes = [SimpleNamespace(where="bus 1 device 2 (sysfs 1-2)", serial_numbers=(probe_id,))]

    result = helper.match_stlink_sysfs(probes)

    assert result["status"] == "matched"
    assert result["sysfs_name"] == "1-2"
    assert probe_id not in json.dumps(result)
    assert result["writes_performed"] is False


def test_usb_diagnostic_refuses_ambiguous_identity_and_sanitizes_discovery_errors():
    helper = _load_helper()
    probes = [
        SimpleNamespace(where="bus 1 device 2 (sysfs 1-2)", serial_numbers=("same",)),
        SimpleNamespace(where="bus 1 device 3 (sysfs 1-3)", serial_numbers=("other",)),
    ]

    result = helper.match_stlink_sysfs(probes)

    assert result["status"] == "missing_or_ambiguous"
    assert "same" not in json.dumps(result)
    assert "sysfs_name" not in result
    assert result["writes_performed"] is False


def test_private_archive_is_hash_checked_and_replaced_atomically(tmp_path, monkeypatch):
    helper = _load_helper()
    expected = b"pinned archive bytes"
    digest = hashlib.sha256(expected).hexdigest()
    target = tmp_path / "toolchains" / "cubeprogrammer-2.23.0.zip"
    target.parent.mkdir()
    target.write_bytes(b"previous valid cache")

    def fake_download(asset_id: str, token: str, destination: Path) -> None:
        assert asset_id == "595488871"
        assert token == "not-for-logs"
        destination.write_bytes(expected)

    monkeypatch.setattr(helper, "_download_release_asset", fake_download)
    result = helper.cache_cubeprogrammer_archive(
        "595488871", "not-for-logs", target, expected_sha256=digest, expected_size=len(expected)
    )

    assert target.read_bytes() == expected
    assert result["sha256"] == digest
    assert "not-for-logs" not in json.dumps(result)
    assert list(target.parent.iterdir()) == [target]


def test_private_archive_hash_failure_preserves_cache_and_removes_partial(tmp_path, monkeypatch):
    helper = _load_helper()
    target = tmp_path / "toolchains" / "cubeprogrammer-2.23.0.zip"
    target.parent.mkdir()
    target.write_bytes(b"previous valid cache")

    def fake_download(asset_id: str, token: str, destination: Path) -> None:
        destination.write_bytes(b"wrong archive")

    monkeypatch.setattr(helper, "_download_release_asset", fake_download)
    try:
        helper.cache_cubeprogrammer_archive(
            "595488871", "not-for-logs", target,
            expected_sha256=EXPECTED_ARCHIVE_SHA256,
            expected_size=None,
        )
    except ValueError as exc:
        assert "SHA-256" in str(exc)
    else:
        raise AssertionError("wrong digest must be rejected")

    assert target.read_bytes() == b"previous valid cache"
    assert list(target.parent.iterdir()) == [target]


def test_asset_id_must_be_a_positive_decimal_number():
    helper = _load_helper()

    for invalid in ("", "0", "-1", "1/../../other", "595488871?token=x"):
        try:
            helper.validate_asset_id(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid asset id accepted: {invalid!r}")


def test_vm_diagnostic_reports_only_sanitized_capability_results(monkeypatch):
    helper = _load_helper()
    monkeypatch.setattr(helper, "discover_bench_probes", lambda: [SimpleNamespace(where="bus 1 device 2 (sysfs 1-2)", serial_numbers=("SECRET-SERIAL",))])
    monkeypatch.setattr(
        helper,
        "inspect_kernel_permissions",
        lambda name: {"authorized": "1", "authorized_readable": True, "authorized_user_writable": False, "authorized_sudo_writable": True, "driver_controls": []},
    )
    monkeypatch.setattr(helper, "probe_sudo_noprompt", lambda: {"available": True, "writes_performed": False})
    monkeypatch.setattr(helper, "detect_rootless_runtimes", lambda: {"podman_rootless": True, "docker_rootless": False})

    result = helper.collect_read_only_diagnostic()
    rendered = json.dumps(result)

    assert "SECRET-SERIAL" not in rendered
    assert "1-2" not in rendered
    assert result["usb"]["status"] == "matched"
    assert result["usb"]["authorized_user_writable"] is False
    assert result["sudo"]["available"] is True
    assert result["writes_performed"] is False


def test_release_asset_redirect_drops_bearer_token_on_cross_origin_redirect(monkeypatch):
    helper = _load_helper()
    captured = {}

    class FakeOpener:
        def __init__(self, handler):
            self.handler = handler() if isinstance(handler, type) else handler

        def open(self, request, timeout):
            captured["handler"] = self.handler
            captured["request"] = request
            return object()

    monkeypatch.setattr(helper, "build_opener", lambda handler: FakeOpener(handler))
    request = helper.Request(
        "https://api.github.com/repos/agentic-hil/agentic-hil/releases/assets/595488871",
        headers={"Authorization": "Bearer secret"},
    )
    helper._safe_urlopen(request)
    handler = captured["handler"]
    redirected = handler.redirect_request(
        captured["request"], None, 302, "Found", {}, "https://release-assets.githubusercontent.com/file"
    )

    assert redirected.get_header("Authorization") is None


def test_write_scoped_private_asset_job_is_manual_conditional_and_separate_from_bench():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]

    assert set(jobs) == {"bench-tier", "provision-cubeprogrammer"}
    provision = jobs["provision-cubeprogrammer"]
    bench = jobs["bench-tier"]
    assert provision["permissions"] == {"contents": "write"}
    assert "github.repository == 'agentic-hil/agentic-hil'" in provision["if"]
    assert "inputs.diagnose_only" in provision["if"]
    assert "inputs.cubeprogrammer_asset_id" in provision["if"]
    assert "cubeprogrammer_asset_id" in bench["if"]
    assert "diagnose_only" in bench["if"]
    assert "permissions" not in bench
    assert workflow["permissions"] == {"contents": "read"}
    checkouts = [step for step in provision["steps"] if "actions/checkout" in str(step.get("uses", ""))]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["persist-credentials"] == "false"


def test_cubeprogrammer_recordings_are_an_opt_in_step_after_the_standard_gates():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    bench_steps = workflow["jobs"]["bench-tier"]["steps"]
    cube_steps = [step for step in bench_steps if "cubeprogrammer_recordings.py" in step.get("run", "")]

    assert inputs["run_cubeprogrammer_recordings"]["type"] == "boolean"
    assert inputs["run_cubeprogrammer_recordings"]["default"] == "false"
    assert len(cube_steps) == 1, cube_steps
    cube = cube_steps[0]
    assert "success()" in cube["if"]
    assert "!inputs.diagnose_only" in cube["if"]
    assert "inputs.run_cubeprogrammer_recordings" in cube["if"]
    assert cube["working-directory"] == "under-test"
    assert "--cubeprogrammer-archive" in cube["run"]
    assert "$HOME/.cache/agentic-hil/toolchains/cubeprogrammer-2.23.0.zip" in cube["run"]
    assert '--output "$BENCH_RESULTS/cubeprogrammer"' in cube["run"]
    assert "-- tests/bench/cubeprogrammer_recordings.py" in cube["run"]

    standard = [step for step in bench_steps if step.get("name") in {"Run the bench tier in its container", "Run the stage without the probe's device group"}]
    assert len(standard) == 2
    assert all("run_cubeprogrammer_recordings" not in step.get("if", "") for step in standard)


def test_diagnostic_only_run_does_not_upload_stale_bench_results():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    upload = [step for step in workflow["jobs"]["bench-tier"]["steps"] if "upload-artifact" in str(step.get("uses", ""))]

    assert len(upload) == 1
    assert "always()" in upload[0]["if"]
    assert "!inputs.diagnose_only" in upload[0]["if"]


def test_usb_reset_reenumeration_is_an_opt_in_stage_after_all_other_gates():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["bench-tier"]
    steps = job["steps"]
    usb_steps = [step for step in steps if "tests/bench/usb_reset_reenumeration.py" in step.get("run", "")]

    assert inputs["run_usb_reset_reenumeration"]["type"] == "boolean"
    assert inputs["run_usb_reset_reenumeration"]["default"] == "false"
    assert len(usb_steps) == 1, usb_steps
    usb = usb_steps[0]
    assert "success()" in usb["if"]
    assert "!inputs.diagnose_only" in usb["if"]
    assert "inputs.run_usb_reset_reenumeration" in usb["if"]
    assert "run_cubeprogrammer_recordings" not in usb["if"]
    assert usb["working-directory"] == "under-test"
    assert "bench_in_container.py" in usb["run"]
    assert "--source ../under-test" in usb["run"]
    assert "--expected-commit" in usb["run"]
    assert '--output "$BENCH_RESULTS/usb-reset"' in usb["run"]
    assert "--runtime podman" in usb["run"]
    assert "--live-device-tree" in usb["run"]
    assert usb["run"].endswith("-- tests/bench/usb_reset_reenumeration.py")
    assert "--cubeprogrammer-archive" not in usb["run"]

    live_tree_runs = [
        step
        for step in steps
        if "bench_in_container.py" in step.get("run", "")
        and any(
            module in step.get("run", "")
            for module in (
                "tests/bench/usb_reset_reenumeration.py",
                "tests/bench/pyocd_recordings.py",
                "tests/bench/recovery_check.py",
            )
        )
    ]
    assert len(live_tree_runs) == 3
    assert all("--live-device-tree" in step["run"] for step in live_tree_runs)
    other_bench_runs = [step for step in steps if "bench_in_container.py" in step.get("run", "") and step not in live_tree_runs]
    assert other_bench_runs
    assert all("--live-device-tree" not in step["run"] for step in other_bench_runs)

    step_names = [step.get("name") for step in steps]
    assert step_names.index("Run CubeProgrammer hardware recordings") < step_names.index(usb["name"])
    assert step_names.index("Run the bench tier in its container") < step_names.index(usb["name"])
    assert step_names.index("Run the stage without the probe's device group") < step_names.index(usb["name"])
    assert job["runs-on"] == ["self-hosted", "agentic-hil", "nucleo-f446re"]
    assert "permissions" not in job
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "nucleo-f446re-bench", "cancel-in-progress": "false"}
    assert set(workflow["jobs"]) == {"bench-tier", "provision-cubeprogrammer"}


def test_pyocd_recordings_are_an_independent_opt_in_stage_before_cube_and_usb():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["bench-tier"]
    steps = job["steps"]
    pyocd_steps = [step for step in steps if "tests/bench/pyocd_recordings.py" in step.get("run", "")]

    assert inputs["run_pyocd_recordings"]["type"] == "boolean"
    assert inputs["run_pyocd_recordings"]["default"] == "false"
    assert len(pyocd_steps) == 1, pyocd_steps
    pyocd = pyocd_steps[0]
    assert "success()" in pyocd["if"]
    assert "!inputs.diagnose_only" in pyocd["if"]
    assert "inputs.run_pyocd_recordings" in pyocd["if"]
    assert "run_cubeprogrammer_recordings" not in pyocd["if"]
    assert "run_usb_reset_reenumeration" not in pyocd["if"]
    assert pyocd["working-directory"] == "under-test"
    assert "bench_in_container.py" in pyocd["run"]
    assert "--source ../under-test" in pyocd["run"]
    assert "--expected-commit" in pyocd["run"]
    assert '--output "$BENCH_RESULTS/pyocd"' in pyocd["run"]
    assert "--runtime podman" in pyocd["run"]
    assert "--live-device-tree" in pyocd["run"]
    assert pyocd["run"].endswith("-- tests/bench/pyocd_recordings.py")
    assert "--cubeprogrammer-archive" not in pyocd["run"]

    standard = [step for step in steps if step.get("name") in {"Run the bench tier in its container", "Run the stage without the probe's device group"}]
    assert len(standard) == 2
    assert all("run_pyocd_recordings" not in step.get("if", "") for step in standard)
    step_names = [step.get("name") for step in steps]
    assert step_names.index("Run the bench tier in its container") < step_names.index(pyocd["name"])
    assert step_names.index("Run the stage without the probe's device group") < step_names.index(pyocd["name"])
    assert step_names.index(pyocd["name"]) < step_names.index("Run CubeProgrammer hardware recordings")
    assert step_names.index(pyocd["name"]) < step_names.index("Run USB reset and re-enumeration recording")


def test_incident_recovery_check_is_an_explicit_preflight_before_the_standard_gates():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["bench-tier"]
    steps = job["steps"]

    assert inputs["run_recovery_check"]["type"] == "boolean"
    assert inputs["run_recovery_check"]["default"] == "false"
    checks = [step for step in steps if "tests/bench/recovery_check.py" in step.get("run", "")]
    assert len(checks) == 1, checks
    check = checks[0]
    assert check["id"] == "recovery_check"
    assert "success()" in check["if"]
    assert "!inputs.diagnose_only" in check["if"]
    assert "inputs.run_recovery_check" in check["if"]
    assert check["working-directory"] == "under-test"
    assert "--live-device-tree" in check["run"]
    assert "--cubeprogrammer-archive" not in check["run"]
    assert check["run"].endswith("-- tests/bench/recovery_check.py")

    standard = [
        step
        for step in steps
        if step.get("name") in {"Run the bench tier in its container", "Run the stage without the probe's device group"}
    ]
    assert len(standard) == 2
    names = [step.get("name") for step in steps]
    assert names.index(check["name"]) < names.index("Run the bench tier in its container")
    assert names.index(check["name"]) < names.index("Run the stage without the probe's device group")
    tier = next(step for step in standard if step.get("name") == "Run the bench tier in its container")
    assert "success()" in tier["if"]
    withheld = next(step for step in standard if step.get("name") == "Run the stage without the probe's device group")
    assert "!cancelled()" in withheld["if"]
    assert "!inputs.diagnose_only" in withheld["if"]
    assert "!inputs.run_recovery_check" in withheld["if"]
    assert "steps.recovery_check.outcome == 'success'" in withheld["if"]
    assert "success()" not in withheld["if"]
