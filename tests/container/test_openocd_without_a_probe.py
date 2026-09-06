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
import re
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

    # What doctor reports back, not what the fixture wrote. Reading the
    # executable out of the file three lines after writing it there was a line
    # that read as a check and could not go red.
    reported = json.loads(agentic_hil("doctor", "--json", cwd=project, config=config).stdout)
    checked = reported["debuggers"]["dut"].get("check") or reported["debugger"]
    assert checked["executable"] == shutil.which("openocd"), checked


# ---------------------------------------------------------------------------
# #509: the probe-unplugged transcript, from the binary rather than from a fake.
#
# The first failure every newcomer sees is the probe not plugged in, and no
# test had ever seen what OpenOCD says about it: the unit fixture for a failed
# open prints `unable to connect to the target`, which is a different bucket,
# and the `libusb_open` line sat in one test as a carried warning beside a
# printed marker. What the installed build words its refusal as is OpenOCD's
# to change, and a rewording drops the run to `unknown_debugger_error`, where
# the abort-point proof is withheld, the lease quarantines and the operator is
# sent to `recover` over a bench nothing touched.

# OpenOCD 0.12.0, 2026-09-06, in this image, with nothing on USB. The unit
# tier's fake (tests/fixtures/fake_openocd_no_probe.py) prints this recording
# verbatim, so the two tiers are held to one transcript.
RECORDED_OPEN_FAILED = "Error: open failed"
# A libusb refusal is not recordable here: the container has no USB bus, so
# libusb has nothing to refuse and the transcript carries the open failure
# alone. On a host without a udev rule the same run prints
# `Error: libusb_open() failed with LIBUSB_ERROR_ACCESS` ahead of it.

THE_THREE_TOOLS = [
    ("probe_target", {}),
    ("reset_target", {"mode": "run"}),
    ("flash_firmware", {"image_path": "build/firmware.elf"}),
]


def effectful_project(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose debugger may flash and reset, with an image to flash."""
    project = tmp_path / "project"
    (project / "build").mkdir(parents=True)
    (project / "build" / "firmware.elf").write_bytes(b"\x7fELFfake")
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", grant_flash_and_reset=True)
    return project, config


def blocking_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = {json.loads(path.read_text(encoding="utf-8")).get("state") for path in records.glob("*.json")}
    return {state for state in states if isinstance(state, str)} & {"cleanup_required", "quarantined", "recovery_pending"}


def test_the_installed_openocd_refuses_a_missing_probe_before_init_with_the_recorded_line() -> None:
    """The premise: what this OpenOCD prints with no probe, and where it stops.

    The stage marker is asked for after `init` the way the backend asks for it,
    and it must not print: the refusal is OpenOCD's own, before any target was
    addressed, which is what lets the backend say the board was never touched.
    """
    from agentic_hil.backends.openocd import OPENOCD_INIT_STAGE_MARKER

    refused = subprocess.run(
        [shutil.which("openocd"), "-f", "interface/stlink.cfg", "-f", "target/stm32f4x.cfg", "-c", "init", "-c", f"echo {OPENOCD_INIT_STAGE_MARKER}", "-c", "shutdown"],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )

    assert refused.returncode == 1, refused
    assert RECORDED_OPEN_FAILED in refused.stderr.splitlines(), refused.stderr
    assert OPENOCD_INIT_STAGE_MARKER not in refused.stdout + refused.stderr, refused
    assert "Open On-Chip Debugger 0.12.0" in refused.stderr, refused.stderr


@pytest.mark.parametrize(("tool", "arguments"), THE_THREE_TOOLS)
def test_a_tool_with_no_probe_attached_is_adapter_not_found_and_never_contacted(tmp_path: Path, tool: str, arguments: dict) -> None:
    """The issue's expected result, produced by the real OpenOCD rather than by a fake.

    `adapter_not_found` with the abort-point proof (`target_contacted` false,
    `retry_safe` true, `hardware_state` unchanged, `side_effect_status`
    not_started), no quarantine and no cleanup demand in the coordination
    records, the classified line on the result and in the log, and the remedy
    that names the probe rather than `recover`.
    """
    from agentic_hil.config import load_config
    from agentic_hil.tools import AgenticHILToolService

    project, config_path = effectful_project(tmp_path)
    config = load_config(str(config_path))
    service = AgenticHILToolService(config)
    try:
        result = service.call(tool, arguments)
    finally:
        service.close()

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "adapter_not_found", json.dumps(result)
    assert result["backend_error_type"] == "adapter_not_found", json.dumps(result)
    assert result["target_contacted"] is False, json.dumps(result)
    assert result["retry_safe"] is True, json.dumps(result)
    assert result["hardware_state"] == "unchanged", json.dumps(result)
    assert result["side_effect_status"] == "not_started", json.dumps(result)
    assert result.get("cleanup_required") is not True, json.dumps(result)
    assert result.get("quarantine_id") is None, json.dumps(result)
    assert result.get("quarantined") is not True, json.dumps(result)
    assert not blocking_record_states(config), blocking_record_states(config)
    assert RECORDED_OPEN_FAILED in result["programmer_output"]["stderr"].splitlines(), result["programmer_output"]
    assert result["programmer_output"]["returncode"] == 1, result["programmer_output"]
    assert any("Connect the probe" in step for step in result["remediation"]), result["remediation"]
    assert not any("recover" in step.lower() for step in result["remediation"]), result["remediation"]
    log = json.loads((project / result["log_path"]).read_text(encoding="utf-8"))
    assert log["returncode"] == 1, log
    assert log["timed_out"] is False, log
    assert RECORDED_OPEN_FAILED in log["stderr"].splitlines(), log


def test_the_console_script_shows_the_operator_the_line_and_the_remedy(tmp_path: Path) -> None:
    """`agentic-hil test-reactor` over a plan whose one step is a reset, with no probe.

    The operator's surface for the same refusal: exit 1, the heading naming
    `adapter_not_found`, OpenOCD's own line printed as OpenOCD wrote it, the
    step's abort-point proof, and the remedy in the vocabulary a shell has.
    The JSON document beside it carries the same result under the step.
    """
    project, config = effectful_project(tmp_path)
    plan = project / ".agentic-hil" / "testconfig.yaml"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("version: 4\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")

    rendered = agentic_hil("test-reactor", cwd=project, config=config)
    document = agentic_hil("test-reactor", "--json", cwd=project, config=config)

    assert rendered.returncode == 1, rendered.stdout + rendered.stderr
    assert rendered.stdout.startswith("Failed: adapter_not_found"), rendered.stdout
    assert RECORDED_OPEN_FAILED in rendered.stdout, rendered.stdout
    assert re.search(r"target_contacted\s+no\b", rendered.stdout), rendered.stdout
    assert re.search(r"quarantined\s+no\b", rendered.stdout), rendered.stdout
    assert re.search(r"hardware_state\s+unchanged\b", rendered.stdout), rendered.stdout
    assert "Call `agentic-hil debugger-probes`" in rendered.stdout, rendered.stdout
    assert "Connect the probe" in rendered.stdout, rendered.stdout
    assert "agentic-hil recover" not in rendered.stdout, rendered.stdout

    assert document.returncode == 1, document.stdout + document.stderr
    result = json.loads(document.stdout)
    assert result["error_type"] == "adapter_not_found", result
    step = result["steps"][0]["result"]
    assert step["tool"] == "reset_target", step
    assert step["error_type"] == "adapter_not_found", step
    assert step["target_contacted"] is False, step
    assert step["hardware_state"] == "unchanged", step
    assert step["quarantined"] is False, step
    assert RECORDED_OPEN_FAILED in step["programmer_output"]["stderr"].splitlines(), step["programmer_output"]
