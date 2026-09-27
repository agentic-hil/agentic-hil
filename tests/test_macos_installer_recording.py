"""The macOS installer facts are from a real hosted runner, not a Linux stand-in."""

from __future__ import annotations

import json
import re
import sys
import sysconfig
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from record_macos_installer_facts import capture, output_has_digest  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RECORDING = REPOSITORY_ROOT / "tests" / "fixtures" / "macos_installer_environment_recording.json"


def test_macos_installer_recording_carries_real_framework_and_hash_tool_evidence() -> None:
    """The report must preserve raw answers that can be checked against the wheel bytes."""
    recording = json.loads(RECORDING.read_text(encoding="utf-8"))

    assert recording["platform"] == "macOS"
    assert recording["github_actions"]["repository"]
    assert recording["github_actions"]["run_id"]
    assert recording["github_actions"]["commit"]
    assert recording["github_actions"]["run_url"].endswith(
        f"/actions/runs/{recording['github_actions']['run_id']}"
    )
    assert recording["python"]["version"]
    assert recording["python"]["executable"]
    assert recording["python"]["prefix"]
    assert recording["python"]["base_prefix"]
    assert recording["python"]["prefix"] == recording["python"]["base_prefix"]
    assert recording["python"]["framework"]
    assert recording["python"]["preferred_user_scheme"] == "osx_framework_user"

    scheme = recording["python"]["schemes"]["osx_framework_user"]
    assert scheme["purelib"]
    assert PurePosixPath(scheme["purelib"]["path"]).is_absolute()
    assert scheme["purelib"]["realpath"]
    assert isinstance(scheme["purelib"]["is_symlink"], bool)
    assert recording["python"]["executable_path"]["realpath"]
    assert isinstance(recording["python"]["executable_path"]["is_symlink"], bool)

    digest = recording["wheel"]["sha256"]
    assert re.fullmatch(r"[a-f0-9]{64}", digest)
    assert recording["wheel"]["size"] > 0
    assert recording["tools"]["shasum"]["version"]
    assert recording["tools"]["openssl"]["version"]
    assert recording["tools"]["shasum"]["executable"]["realpath"]
    assert recording["tools"]["openssl"]["executable"]["realpath"]

    shasum = recording["tools"]["shasum"]["sha256"]
    assert re.search(rf"\b{digest}\b", shasum["stdout"], re.IGNORECASE)
    assert shasum["returncode"] == 0
    assert recording["tools"]["openssl"]["sha256"]["returncode"] == 0
    openssl = recording["tools"]["openssl"]["sha256"]["stdout"]
    assert re.search(rf"\b{digest}\b", openssl, re.IGNORECASE)


def test_capture_workflow_is_scoped_and_uploads_the_raw_recording() -> None:
    workflow = REPOSITORY_ROOT / ".github" / "workflows" / "record-macos-installer-facts.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in source
    assert "pull_request:" in source
    assert "macos-latest" in source
    assert "tools/record_macos_installer_facts.py" in source
    assert "macos-installer-recording" in source
    assert "actions/upload-artifact@" in source


@pytest.mark.parametrize(
    "output",
    (
        "0123456789abcdef" * 4 + "  agentic_hil.whl",
        "SHA2-256(agentic_hil.whl)= " + "0123456789abcdef" * 4,
    ),
)
def test_hash_tool_output_parsers_accept_real_shasum_and_openssl_lines(output: str) -> None:
    digest = "0123456789abcdef" * 4

    assert output_has_digest(output, digest)
    assert not output_has_digest(output, "f" * 64)


def test_capture_refuses_a_non_macos_host_before_reading_any_recording_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("record_macos_installer_facts.platform.system", lambda: "Windows")

    with pytest.raises(RuntimeError, match="must be captured on macOS"):
        capture(Path("not-a-wheel.whl"))


def test_capture_refuses_a_non_framework_python_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("record_macos_installer_facts.platform.system", lambda: "Darwin")
    monkeypatch.setenv("GITHUB_RUN_ID", "1234")
    monkeypatch.setenv("GITHUB_REPOSITORY", "agentic-hil/agentic-hil")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: None if name == "PYTHONFRAMEWORK" else None)
    monkeypatch.setattr(sysconfig, "get_preferred_scheme", lambda _key: "posix_user")

    with pytest.raises(RuntimeError, match="not a macOS framework installation"):
        capture(Path("not-a-wheel.whl"))
