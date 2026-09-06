"""Which stream a protocol command's startup refusal is on, asked of the real server.

`mcp-stdio` and `com-stdio` own stdout for framed messages, so a configuration
refused before either can start is written to stderr and stdout stays empty;
that is #458 and the behaviour the release after it shipped. The install eval's wrong-workspace
arm reads that refusal off stdout, where the pre-#458 release wrote it, and both
of its unit tests were typed from that older world, so the arm fails a sound
install with `refused for the wrong reason: error_type=<no document>` while the
suite stays green (#481, #490).

These tests are where the two accounts meet the product. The first asks the
server itself which stream carries the refusal, and holds the recording under
tests/fixtures/protocol_startup_refusal_recording.json to the same answer, so
the unit tests that replay the recording and the eval that runs the real server
are tested against one account. The second runs the eval's own arm against the
real server, which is the failure #481 observed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.install import verifier

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, REPOSITORY_ROOT, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

RECORDING = REPOSITORY_ROOT / "tests" / "fixtures" / "protocol_startup_refusal_recording.json"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "wrong-workspace-probe", "version": "0"}},
}


def start_in(other: Path, *arguments: str, config: Path) -> subprocess.CompletedProcess[str]:
    """The protocol command, started in a directory its configuration does not bind."""
    environment = {**os.environ, "AGENTIC_HIL_CONFIG": str(config)}
    return subprocess.run(
        [sys.executable, "-m", "agentic_hil", *arguments],
        cwd=str(other),
        env=environment,
        input=json.dumps(INITIALIZE) + "\n",
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def bound_elsewhere(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A project, a configuration bound to it, and a directory that is not it."""
    project = tmp_path / "project"
    other = tmp_path / "other-project"
    project.mkdir()
    other.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")
    return project, other, config


@pytest.mark.parametrize(
    ("command", "recorded_as"),
    [(("mcp-stdio",), "mcp-stdio"), (("com-stdio", "--port", "dut"), "com-stdio --port dut")],
)
def test_a_startup_refusal_is_on_stderr_with_stdout_empty_and_the_recording_says_the_same(
    tmp_path: Path, command: tuple[str, ...], recorded_as: str
) -> None:
    """The stream contract, read off the server and off the recording in one test.

    Exit 1, nothing on stdout, and the whole refusal on stderr: `config_invalid`,
    the binding sentence, and both roots. The recording is held to the same
    facts field for field, paths excepted, so a release that moved the refusal
    again would fail here before the unit tests that replay the recording could
    go on passing against a stream the product no longer writes.
    """
    project, other, config = bound_elsewhere(tmp_path)

    started = start_in(other, *command, config=config)

    assert started.returncode == 1, started.stdout + started.stderr
    assert started.stdout == "", started.stdout
    refusal = json.loads(started.stderr)
    assert refusal["ok"] is False
    assert refusal["error_type"] == "config_invalid", refusal
    assert refusal["summary"] == "The authoritative config is bound to a different workspace.", refusal
    assert refusal["workspace_root"] == str(project), refusal
    assert refusal["expected_workspace"] == str(other), refusal
    assert refusal["path"] == str(config), refusal

    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))["runs"][recorded_as]
    assert recorded["returncode"] == started.returncode
    assert recorded["stdout"] == started.stdout
    recorded_refusal = json.loads(recorded["stderr"])
    assert sorted(recorded_refusal) == sorted(refusal), "the recording and the server disagree about the refusal's fields"
    for field in ("ok", "error_type", "summary", "remediation", "do_not"):
        assert recorded_refusal[field] == refusal[field], field


def test_the_install_evals_named_arm_passes_against_the_real_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check the release fails today, run the way the eval runs it (#481).

    Everything the arm asks of the refusal is there on the other stream:
    `config_invalid`, exit 1, no server initialized, both roots named. Only the
    container's fixed interpreter and PATH are replaced, by this interpreter and
    this environment, so the arm's own reading of the streams is what is under
    test. The discovered arm runs too, against a configuration home that holds
    nothing, which is how the eval reaches it.
    """
    project, other, config = bound_elsewhere(tmp_path)
    monkeypatch.setattr(verifier, "OTHER_WORKSPACE", other)
    monkeypatch.setattr(verifier, "PROBE_CONFIG_ROOT", tmp_path / "probe-config")

    def environment(named: Path | None = None, *, config_home: Path | None = None) -> dict[str, str]:
        built = {**os.environ}
        built.pop("AGENTIC_HIL_CONFIG", None)
        if named is not None:
            built["AGENTIC_HIL_CONFIG"] = str(named)
        if config_home is not None:
            built["XDG_CONFIG_HOME"] = str(config_home)
        return built

    monkeypatch.setattr(verifier, "trusted_environment", environment)
    monkeypatch.setattr(verifier, "trusted_command", lambda arguments: [sys.executable, "-m", "agentic_hil", *arguments])

    ok, detail = verifier.wrong_workspace_fails(["mcp-stdio"], config)

    assert ok, detail
    assert "config_invalid" in detail
    assert f"expected_workspace={other}" in detail, detail
    assert "config_file_not_found" in detail
