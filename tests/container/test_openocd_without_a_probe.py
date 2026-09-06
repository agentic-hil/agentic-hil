"""What the two read-only commands answer on a host that has OpenOCD and no probe.

This is the shape of every CI container and of a developer's laptop, and it is
the shape the exit codes were wrong for. `agentic-hil debugger-probes` used to
exit non-zero whenever its listing disclaimed its own completeness, and the
OpenOCD path can never claim completeness: OpenOCD has no probe listing of its
own, so the ids come from the host's USB serial inventory, which reaches an
ST-Link only through the virtual COM port a V2-1 or a V3 publishes. Every
`set -e` script over a working bench broke on that, and an agent read it as a
fault.

The counterpart is `doctor`, which does exit non-zero, and for a different
reason: a configuration whose devices name no hardware cannot have a test plan
run against it, so it says so and names the command that binds them. An
installed toolchain with no probe attached is not that case and is not a
failure, which is the pair these tests hold apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]


def agentic_hil(*arguments: str, cwd: Path, config: Path | None = None) -> subprocess.CompletedProcess[str]:
    """The CLI, through this interpreter, so no PATH lookup decides what ran."""
    environment = {**os.environ}
    environment.pop("AGENTIC_HIL_CONFIG", None)
    if config is not None:
        environment["AGENTIC_HIL_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-m", "agentic_hil", *arguments],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def test_probe_discovery_answers_through_bootstrap_and_exits_zero_with_no_configuration(tmp_path: Path) -> None:
    """The one moment an operator wants a probe listing with nothing else in place.

    Before the first `setup`: is the board visible, is there one of it, which
    serial. Refusing that for a missing configuration withheld an answer the tool
    could already give, and the answer says what it is rather than pretending to
    be the configured bench speaking.
    """
    answered = agentic_hil("debugger-probes", "--json", cwd=tmp_path)

    assert answered.returncode == 0, answered.stderr
    result = json.loads(answered.stdout)
    assert result["ok"] is True, result
    assert result["source"] == "bootstrap", result
    assert result["backend"] == "openocd", result
    assert result["probes"] == [], result
    assert result["complete"] is False, result
    assert "not an authoritative count" in result["summary"], result["summary"]


def test_probe_discovery_through_the_configured_backend_reports_no_probe_and_still_exits_zero(tmp_path: Path) -> None:
    """A listing that can never be complete is not a run that went wrong.

    The count is zero, the listing says in words that zero is not proof, and the
    exit status says the discovery ran. All three at once is the behaviour: a
    script may act on the status, and a person or an agent has to read the
    sentence before acting on the number.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")

    answered = agentic_hil("debugger-probes", "--json", cwd=project, config=config)

    assert answered.returncode == 0, answered.stderr
    result = json.loads(answered.stdout)
    assert result["ok"] is True, result
    assert result["backend"] == "openocd", result
    assert result["complete"] is False, result
    assert result["probes"] == [], result
    assert "not proof no probe is connected" in result["summary"], result["summary"]


def test_doctor_refuses_a_configuration_whose_devices_name_no_hardware(tmp_path: Path) -> None:
    """The placeholder file `init` writes where discovery found no bench.

    Nothing is wrong with the file; what is wrong is that no plan can run against
    it, and an operator who reads a green doctor over it is an operator who finds
    that out at the first step of a run instead.
    """
    project = tmp_path / "project"
    project.mkdir()
    written = agentic_hil("init", cwd=project)
    assert written.returncode == 0, written.stdout + written.stderr

    answered = agentic_hil("doctor", cwd=project)

    assert answered.returncode == 1, answered.stdout
    assert "not bound to hardware" in answered.stdout, answered.stdout
    assert "adopt-hardware" in answered.stdout, answered.stdout


def test_doctor_accepts_a_bound_configuration_whose_openocd_is_installed(tmp_path: Path) -> None:
    """A toolchain that is there and a probe that is not is not a failure.

    Doctor spawns the configured debugger to establish that it is the thing the
    file names and that it runs. It does not go to a board, so a bench with no
    probe attached passes this and is refused later, by the command that needs
    the probe, with the error that names the probe.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")

    answered = agentic_hil("doctor", cwd=project, config=config)

    assert answered.returncode == 0, answered.stdout + answered.stderr
    assert "OpenOCD is available." in answered.stdout, answered.stdout
    assert str(shutil.which("openocd")) in config.read_text(encoding="utf-8")
