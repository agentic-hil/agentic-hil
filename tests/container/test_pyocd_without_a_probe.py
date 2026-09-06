"""pyOCD with nothing on USB: the refusal the real tool gives, and the result the backend makes of it.

This image carries pyOCD and no probe, which is exactly the bench #480 was
measured on: `type: pyocd`, no `probe_id`, and `probe_target`, `reset_target`
and `flash_firmware` each waiting out `timeout_s`, being reaped, and opening a
quarantine for a board nothing had contacted. Two pyOCD facts decide the fix,
and both are pyOCD's to change between releases: that it waits for a probe
unless spawned with `-W`, and the exact sentence it prints when told not to.
The suite's fixture reproduces the recording taken here on 2026-09-06 from
pyOCD 0.45.1; this module is what notices the next time pyOCD rewords it.

Nothing here can reach a board. pyOCD is asked to find a probe, finds none, and
says so; the backend is then asked what it makes of that.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, coordination_record_states

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

# pyOCD 0.45.1, 2026-09-06, in this image, with nothing on USB.
RECORDED_NO_PROBE = "No connected debug probes\n"
RECORDED_NO_PROBE_FOR_UID = "No connected debug probe matches unique ID 'NOSUCHPROBE0001'\n"

# Long enough that the real pyOCD's own start-up (a Python interpreter loading
# its probe drivers) is nowhere near it, short enough that a run which waits it
# out is a failure and not a slow job. The bound the refusal is held to is half
# of it.
#
# Measured rather than guessed, and raised from 10 because the guess was too
# close: that start-up took 8.5 s in this image on a loaded machine, which is
# past the old half-of-it bound and near enough to the old timeout that the
# refusal would have been reaped as one. A timeout is the failure this file
# exists to tell apart from a refusal, so a machine's load must not be able to
# manufacture it here.
TIMEOUT_S = 40


def real_pyocd() -> str:
    found = shutil.which("pyocd")
    assert found is not None, "pyocd is not on PATH: this tier's image has to carry pyOCD, installed with pip, and no probe"
    return found


def pyocd_configuration(workspace: Path, config_path: Path, state_root: Path, *, probe_id: str | None = None) -> Path:
    """A project bound to the pyOCD this image installs, with the two effectful grants.

    Effectful on purpose: a flash and a reset are the two tools whose result
    declared the hardware state unknown, and a configuration that refused them
    for a missing grant would never reach the spawn under test.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""workspace_root: {str(workspace)!r}
state_root: {str(state_root)!r}
version: 3
permissions:
  allow_config_write: false
  allow_config_description_write: false
  allow_config_permissions_write: false
  allow_recover: false
  allow_upgrade: false
target:
  name: "container-fixture"
  controller: "stm32f446ret6"
debuggers:
  dut:
    type: pyocd
    executable: {real_pyocd()!r}
    probe_id: {json.dumps(probe_id)}
    timeout_s: {TIMEOUT_S}
    permissions:
      allow_flash: true
      allow_reset: true
      allow_debug_execution: false
      allow_raw_debugger_commands: false
      allow_mass_erase: false
debug:
  gdb_executable: null
  allowed_symbols: []
  allow_all_symbols: true
  max_dump_size_bytes: 1048576
artifacts:
  allowed_roots: ["build"]
  upload_directory: ".agentic-hil/artifacts"
  allowed_extensions: [".elf"]
  max_upload_size_mb: 1
  allow_upload: false
com_ports: {{}}
can_buses: {{}}
reports:
  directory: ".agentic-hil/reports"
logs:
  directory: ".agentic-hil/logs"
""",
        encoding="utf-8",
    )
    return config_path


def test_the_installed_pyocd_still_refuses_with_the_sentences_the_fixture_recorded(tmp_path: Path) -> None:
    """The premise of the suite's fixture, held against the pyOCD this image has.

    The three spawns the backend issues (`commander --command status`,
    `commander --command reset` and `flash --no-reset`), the `--uid` form of
    the first, and the `reset` subcommand the recording listed: five commands,
    two sentences, and the exit codes the fixture reproduces. A release that
    rewords the refusal, or starts exiting non-zero from the commander, fails
    here rather than quietly returning `unknown_debugger_error` on a bench
    again.
    """
    pyocd = real_pyocd()
    image = tmp_path / "x.elf"
    image.write_bytes(b"\x7fELFfake")

    commander = subprocess.run([pyocd, "commander", "-W", "--command", "status"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    assert (commander.stdout, commander.returncode) == (RECORDED_NO_PROBE, 0), commander

    by_uid = subprocess.run([pyocd, "commander", "-W", "--uid", "NOSUCHPROBE0001", "--command", "status"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    assert (by_uid.stdout, by_uid.returncode) == (RECORDED_NO_PROBE_FOR_UID, 0), by_uid

    # What `reset_target` spawns: the commander, which exits 0 over the sentence.
    reset_through_the_commander = subprocess.run([pyocd, "commander", "-W", "--command", "reset"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    assert (reset_through_the_commander.stdout, reset_through_the_commander.returncode) == (RECORDED_NO_PROBE, 0), reset_through_the_commander

    # What `flash_firmware` spawns: exit 1, with a second line of its own on stderr.
    flash = subprocess.run([pyocd, "flash", "-W", "--no-reset", str(image)], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    assert (flash.stdout, flash.returncode) == (RECORDED_NO_PROBE, 1), flash
    assert "No target device available" in flash.stderr, flash

    reset = subprocess.run([pyocd, "reset", "-W"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    assert (reset.stdout, reset.returncode) == (RECORDED_NO_PROBE, 1), reset
    assert "No target device available to reset" in reset.stderr, reset


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("probe_target", {}),
        ("reset_target", {"mode": "run"}),
        ("flash_firmware", {"image_path": "build/firmware.elf"}),
    ],
)
def test_a_tool_with_no_probe_attached_refuses_promptly_against_the_real_pyocd(tmp_path: Path, tool: str, arguments: dict) -> None:
    """The issue's expected result, produced by the real pyOCD rather than by a fake.

    `error_type: adapter_not_found`, `target_contacted: false`,
    `side_effect_status: not_started`, `hardware_state: unchanged`, no cleanup
    requirement, and all of it well inside `timeout_s`. The log the backend
    wrote is read back too: the command carried the flag and pyOCD's sentence is
    what came back, so the classification rests on the words pyOCD used.
    """
    project = tmp_path / "project"
    (project / "build").mkdir(parents=True)
    (project / "build" / "firmware.elf").write_bytes(b"\x7fELFfake")
    config = load_config(str(pyocd_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")))

    service = AgenticHILToolService(config)
    try:
        started = time.perf_counter()
        result = service.call(tool, arguments)
        elapsed_s = time.perf_counter() - started
    finally:
        service.close()

    assert result["error_type"] != "timeout", result
    assert result["error_type"] == "adapter_not_found", result
    assert result["target_contacted"] is False, result
    assert result["side_effect_status"] == "not_started", result
    assert result["hardware_state"] == "unchanged", result
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantine_id") is None, result
    states = coordination_record_states(config.state_root)
    assert states, "the run wrote no coordination record at all, so nothing here says a lease was taken and given back"
    assert set(states) == {"released"}, states
    assert elapsed_s < TIMEOUT_S / 2, (elapsed_s, result)
    log = json.loads((project / result["log_path"]).read_text(encoding="utf-8"))
    assert log["timed_out"] is False, log
    assert "-W" in log["command"].split() or "--no-wait" in log["command"].split(), log["command"]
    assert log["stdout"] == RECORDED_NO_PROBE, log


# ---------------------------------------------------------------------------
# #509: the wording behind `target_type_invalid`, on the one leg that has pyOCD.
#
# tests/test_pyocd_unknown_target_phrases.py holds the classifier's two markers
# against the installed pyOCD's own refusal, and it `importorskip`s pyOCD, so
# the hosted matrix has to install the extra for those markers to be pinned
# anywhere the matrix runs. What that file cannot say is what the sentence was
# when the fixture was written: it asserts the markers, not the words. That is
# what belongs here, where a real pyOCD is guaranteed, and it is the only thing
# added, because a second copy of the markers would be a second copy of a fact
# that has one owner. The refusal itself is made by that file's helper, imported
# rather than restated for the same reason. The `target_type_invalid`
# classification is what unlocks the `pyocd pack find` / `pyocd pack install`
# remediation; an earlier phrase list matched nothing pyOCD printed and every
# such failure fell to `unknown_debugger_error`.

# Not a plausible near-miss of a real part: unresolvable on any host, including
# one whose CMSIS-pack cache is full of vendor targets.
UNRESOLVABLE_TARGET_TYPE = "agentic_hil_no_such_target_type"
# pyOCD 0.45.1, 2026-09-06, in this image: `Board(Session(None, ...))` with
# this target override. The unit fixture (tests/fixtures/fake_pyocd_unknown_target.py)
# wraps the same sentence in the log prefix and suffix the command line adds.
RECORDED_UNKNOWN_TARGET = (
    f"Target type {UNRESOLVABLE_TARGET_TYPE} not recognized. Use 'pyocd list --targets' to see currently "
    "available target types. See <https://pyocd.io/docs/target_support.html> for how to install additional "
    "target support."
)
STALE_RECORDING = "the sentence in tests/fixtures/fake_pyocd_unknown_target.py is stale: take it again from this image and note the version and the date"


def test_the_installed_pyocd_refuses_an_unknown_target_with_the_sentence_the_fixture_reproduces(tmp_path: Path) -> None:
    """The words, the fixture's copy of them, and what the classifier makes of them.

    The markers themselves belong to tests/test_pyocd_unknown_target_phrases.py,
    which the hosted matrix runs against a real pyOCD; the helper that produces
    the refusal is that file's, imported here so the two tiers ask the tool the
    same question. What is added here is the recording: the sentence as it read
    when the unit fake was written, so a reworded release is a failure that
    names the file to re record rather than a fake drifting away from the tool
    it stands in for.
    """
    from fixtures.fake_pyocd_unknown_target import TARGET_NOT_RECOGNIZED
    from test_pyocd_unknown_target_phrases import real_pyocd_refusal

    from agentic_hil.backends.pyocd import PyOCDBackend

    message = real_pyocd_refusal(UNRESOLVABLE_TARGET_TYPE)

    assert message == RECORDED_UNKNOWN_TARGET, STALE_RECORDING
    assert TARGET_NOT_RECOGNIZED.format(target=UNRESOLVABLE_TARGET_TYPE) == f"0001042 C {message} [__main__]", STALE_RECORDING

    project = tmp_path / "project"
    project.mkdir()
    config = load_config(str(pyocd_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")))
    assert PyOCDBackend(config)._classify_output(f"0001042 C {message} [__main__]", "flash_firmware") == "target_type_invalid"
