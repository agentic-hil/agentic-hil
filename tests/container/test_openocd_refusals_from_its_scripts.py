"""The real OpenOCD refusing its own scripts, and its words held against the recording.

Two of #506's gaps need OpenOCD itself and no probe. A target script that is
not in the script tree is refused by OpenOCD at its configuration stage,
before any adapter is opened, so the refusal is reachable in this image with
nothing on USB; the same is true of a file a configured script sources and of
the GDB server OpenOCD is asked to start with no adapter behind it. The unit
tier replays the recordings of those runs (``tests/fixtures/
debugger_refusal_recordings.json``); this module is what notices when the
installed OpenOCD, or the installed pyOCD, rewords them.

Two kinds of test. The first drives the backend against the real OpenOCD
through the service, for the one refusal whose classification depends on a
configured field: a missing ``target_cfg`` has to come back as
``target_config_not_found`` naming that field, with the bench untouched. The
second re-runs each recorded command and compares the decisive lines to the
recording, so a rewording turns a test red here rather than on a bench.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_hil.backends.common import NOT_CONTACTED
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, REPOSITORY_ROOT, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

RECORDINGS_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "debugger_refusal_recordings.json"
RECORDINGS = json.loads(RECORDINGS_PATH.read_text(encoding="utf-8"))
# pyOCD prefixes its log lines with a millisecond counter that differs between
# runs; everything after it is the sentence under test.
PYOCD_LOG_PREFIX = re.compile(r"^\d{7} ", re.MULTILINE)


def run_recorded(name: str) -> tuple[dict, subprocess.CompletedProcess[str]]:
    """One recorded command, re-run exactly as it was recorded.

    The program name is left as the recording spells it rather than resolved to
    a path: getopt puts `argv[0]` in front of `unrecognized option`, so an
    absolute path here would change the very line the comparison is about.
    Both programs are on PATH in this image, which the version test asserts.
    """
    recorded = RECORDINGS["recordings"][name]
    argv = list(recorded["argv"])
    if "/tmp/x.bin" in argv:
        Path("/tmp/x.bin").write_bytes(b"x")
    return recorded, subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)


def decisive_lines(text: str) -> list[str]:
    """The lines a classifier reads: the errors, the refusals, and the version line."""
    return [PYOCD_LOG_PREFIX.sub("", line) for line in text.splitlines() if line.startswith(("Error", "embedded:", "openocd:", "pyocd:", "No connected", "Open On-Chip", "**")) or re.match(r"^\d{7} [EC] ", line)]


def blocking_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = {json.loads(path.read_text(encoding="utf-8")).get("state") for path in records.glob("*.json")}
    return {state for state in states if isinstance(state, str)} & {"cleanup_required", "quarantined", "recovery_pending"}


def test_the_installed_tools_are_the_ones_the_recording_names() -> None:
    """The premise of every comparison below, asserted rather than noted."""
    versions = RECORDINGS["tool_versions"]
    openocd = subprocess.run([shutil.which("openocd"), "--version"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
    pyocd = subprocess.run([shutil.which("pyocd"), "--version"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)

    assert versions["openocd"].startswith(openocd.stderr.splitlines()[0]), (versions["openocd"], openocd.stderr)
    assert versions["pyocd"] == pyocd.stdout.strip(), (versions["pyocd"], pyocd.stdout)


def test_a_missing_target_cfg_is_target_config_not_found(tmp_path: Path) -> None:
    """The real OpenOCD, `interface/stlink.cfg` it has, `target/does-not-exist.cfg` it has not.

    OpenOCD fails at its configuration stage, before opening the adapter, so no
    probe is needed for the refusal and none is needed for what the backend
    makes of it: `target_config_not_found`, a summary that names `target_cfg`,
    and a call that provably never reached a board.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(str(fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", target_cfg="target/does-not-exist.cfg")))
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["backend_error_type"] == "target_config_not_found", result
    assert result["error_type"] == "debugger_config_not_found", result
    assert "target_cfg" in result["summary"], result["summary"]
    assert "Can't find target/does-not-exist.cfg" in result["programmer_output"]["stderr"], result["programmer_output"]
    for key, value in NOT_CONTACTED.items():
        assert result.get(key) == value, (key, result)
    assert result.get("quarantined") is not True, result
    assert result.get("cleanup_required") is not True, result
    assert not blocking_record_states(config), blocking_record_states(config)


def test_a_present_target_cfg_is_still_refused_for_the_adapter_and_not_for_the_script(tmp_path: Path) -> None:
    """The neighbour: with both scripts resolving, the same probeless OpenOCD fails one stage later.

    `open failed` is the adapter, not a script, and it must stay
    `adapter_not_found` so the target case above is a classification and not
    a coincidence of the fixture's configuration.
    """
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(str(fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")))
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["backend_error_type"] == "adapter_not_found", result
    assert result["error_type"] == "adapter_not_found", result
    assert "target_cfg" not in result["summary"], result["summary"]


@pytest.mark.parametrize(
    "name",
    [
        "openocd_version",
        "openocd_unknown_option",
        "openocd_missing_target_cfg",
        "openocd_missing_include",
        "openocd_server_stlink_no_probe",
        "openocd_server_ftdi_no_device",
        "openocd_program_no_probe",
        "pyocd_version",
        "pyocd_unknown_option",
        "pyocd_commander_status_no_probe",
        "pyocd_flash_no_probe",
    ],
)
def test_the_installed_tool_still_words_its_refusal_as_recorded(name: str) -> None:
    """Each recorded command, re-run: same exit status, same decisive lines.

    Whole-stream equality is deliberately not asserted. OpenOCD's `Info` lines
    and pyOCD's millisecond prefixes vary with the host and the run; what the
    classifier reads, and what the fixture therefore has to keep, are the
    error and refusal lines, and those have to be byte for byte the recording.
    """
    recorded, rerun = run_recorded(name)

    assert rerun.returncode == recorded["returncode"], (rerun.stdout, rerun.stderr)
    assert decisive_lines(rerun.stdout) == decisive_lines(recorded["stdout"]), (rerun.stdout, recorded["stdout"])
    assert decisive_lines(rerun.stderr) == decisive_lines(recorded["stderr"]), (rerun.stderr, recorded["stderr"])


def test_the_program_procs_phrases_are_still_in_the_binary() -> None:
    """`** Programming Failed **` and `** Verify Failed **` are what a failed flash prints.

    Neither can be produced without a probe, so the unit tier plays them from
    the recording's list; this is the check that the installed OpenOCD still
    carries them, so the list cannot go stale silently.
    """
    binary = Path(shutil.which("openocd")).read_bytes()

    for phrase in RECORDINGS["phrases_in_the_openocd_binary"]["phrases"]:
        assert phrase.encode() in binary, phrase
