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
