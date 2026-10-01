"""What OpenOCD 0.11 answers to the probe selection this backend sends, from the bench.

Ubuntu 22.04 packages OpenOCD 0.11. With `probe_id` set, a call the OpenOCD
backend makes on 0.12 and newer, or on an OpenOCD whose release it could not
read, puts `-c "adapter serial <serial>"` on OpenOCD's command line after the
interface script and ahead of the target script and of the call's own command
(test_openocd_0_11_probe_selection holds what a 0.11 that names its release
gets instead). The OpenOCD the default image carries runs it, on every green
default tier; 0.11 has no such subcommand. It prints the usage of the `adapter` group,
whose list names `hla_serial` for the job and no `serial`, then
`Error: invalid subcommand "serial <serial>"`, and exits 1 with no line from an
adapter or a target in between: OpenOCD evaluates
its `-f` and `-c` arguments in order at the configuration stage and stops at the
first one that fails, so the `init` in the call's own command, which is what
opens the probe, was never reached.

That is a refusal of a command this backend sent, before the probe was opened,
and the backend has a result for exactly that: `debugger_command_rejected`,
which leaves the bench in service. Read as an unconfirmed reset instead, it
quarantined a board nothing had touched, and the recovery the quarantine asked
for failed on the same refusal (fixtures/openocd_0_11_bench_recordings.json
says where and how the words were recorded).

The other direction holds beside it: the same words about a serial this backend
did not send are somebody else's script failing, and say nothing about where
this call stopped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.backends.common import NOT_CONTACTED
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_TRANSCRIPT = FIXTURES / "fake_debugger_transcript.py"
RECORDINGS = json.loads((FIXTURES / "openocd_0_11_bench_recordings.json").read_text(encoding="utf-8"))
# The serial the recording carries in place of the bench's own, which the tier's
# runner withholds: the backend has to be bound to the serial OpenOCD names.
SERIAL = RECORDINGS["probe_serial"]
RESET = RECORDINGS["recordings"]["reset_target_run"]


def play(monkeypatch: pytest.MonkeyPatch, recorded: dict) -> None:
    """Have the transcript fake play one recorded run, byte for byte."""
    monkeypatch.delenv("AGENTIC_HIL_FAKE_TRANSCRIPT_RECORDING", raising=False)
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_STDOUT", recorded["stdout"])
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_STDERR", recorded["stderr"])
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_EXIT", str(recorded["returncode"]))


def reset(workspace: Path, probe_id: str | None) -> tuple[dict, object]:
    config = load_config(str(write_config(workspace, debugger_executable=FAKE_TRANSCRIPT, probe_id=probe_id)))
    service = AgenticHILToolService(config)
    try:
        return service.call(RESET["tool"], RESET["arguments"]), config
    finally:
        service.close()


def blocking_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = {json.loads(path.read_text(encoding="utf-8")).get("state") for path in records.glob("*.json")}
    return {state for state in states if isinstance(state, str)} & {"cleanup_required", "quarantined", "recovery_pending"}


def test_the_recording_is_openocd_0_11_refusing_the_serial_this_backend_selects_the_probe_by() -> None:
    """The recording is the one this module is about, and says so in its own words."""
    assert RECORDINGS["tool_versions"]["openocd"] == "Open On-Chip Debugger 0.11.0"
    assert RESET["stderr"].startswith("Open On-Chip Debugger 0.11.0\n")
    assert f'Error: invalid subcommand "serial {SERIAL}"' in RESET["stderr"]
    assert RESET["returncode"] == 1
    assert RESET["stdout"] == ""


def test_openocd_0_11_refusing_the_probe_selection_is_a_rejected_command_and_the_bench_stays_in_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    play(monkeypatch, RESET)

    result, config = reset(tmp_path, SERIAL)

    assert result["ok"] is False, result
    assert result["error_type"] == "debugger_command_rejected", result
    assert result["backend_error_type"] == "command_rejected_before_init", result
    assert result["rejected_commands"] == ["adapter serial"], result
    for key, value in NOT_CONTACTED.items():
        assert result.get(key) == value, (key, result)
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantined") is not True, result
    assert "quarantine_guidance" not in result, result
    assert not blocking_record_states(config), blocking_record_states(config)
    # OpenOCD's own words travel with the result, whole.
    assert result["programmer_output"] == {"returncode": RESET["returncode"], "stdout": RESET["stdout"], "stderr": RESET["stderr"]}
    # And the words it refused are the ones this backend put on its command line.
    logged = json.loads((Path(config.workspace_root) / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert f"adapter serial {SERIAL}" in logged


def test_the_same_refusal_of_a_serial_this_backend_did_not_send_stays_an_unconfirmed_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `probe_id` the backend sends no `adapter serial`, so the words are another script's.

    Nothing then says the call stopped before the probe was opened, and the
    reset keeps the reading every reset that cannot prove its abort point gets.
    """
    play(monkeypatch, RESET)

    result, config = reset(tmp_path, None)

    logged = json.loads((Path(config.workspace_root) / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert "adapter serial" not in logged
    assert result["ok"] is False, result
    assert "rejected_commands" not in result, result
    assert result["side_effect_status"] == "unknown", result
    assert result["retry_safe"] is False, result
    assert result.get("hardware_state") != "unchanged", result
    assert result["cleanup_required"] is True, result
    assert result["cleanup_reasons"] == ["debugger_result_unconfirmed"], result


def session_log(config, started: dict) -> dict:
    """The session log the start's result names, where the server's own output is kept."""
    return json.loads((Path(config.work_dir) / started["log_path"]).read_text(encoding="utf-8"))


def test_a_debug_session_the_same_refusal_stops_is_read_as_the_rejected_command_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The debug server puts the same `-c` on its command line, so the same reading has to apply.

    A session start builds the server's argv with the probe selection the tool
    path uses, including the documented `adapter serial` fallback taken when the
    release could not be read, and 0.11 refuses it inside the interpreter at the
    first argument. The tool path answers `debugger_command_rejected` naming
    `adapter serial`; the start classified from the output's words alone and
    answered `error_type: debugger_error`, `backend_error_type:
    unknown_debugger_error` and "Debug server exited before the GDB port became
    ready." with no `rejected_commands`, for a server that provably stopped before
    `init`.

    The markers were already right and the decisive line was already in
    `server_stderr_tail`, so nothing about what this does to the bench changes:
    this is the classification catching up with the transcript. The bench stays in
    service either way, and that is asserted here so it cannot regress the other
    way.
    """
    from test_debug_sessions import debug_service, start_debug_session

    play(monkeypatch, RESET)
    service = debug_service(tmp_path, debugger_executable=FAKE_TRANSCRIPT, probe_id=SERIAL)
    try:
        # `attach`, because a `load` start that spawned a server cannot rule the
        # firmware load out on the coordination layer's own evidence and keeps the
        # unconfirmed reading it had: that reading is not what this is about, and
        # changing it would be a product decision rather than a classification.
        started = start_debug_session(service, "attach")
        status = service.call("debug_get_session_status")
    finally:
        service.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "debugger_command_rejected", started
    assert started["backend_error_type"] == "command_rejected_before_init", started
    assert started["rejected_commands"] == ["adapter serial"], started
    # The decisive line, still where it always was: the session log the result names.
    assert f'invalid subcommand "serial {SERIAL}"' in session_log(service.config, started)["server_stderr_tail"], started
    # And still not an incident: nothing opened the probe, so nothing has to be
    # inspected before the next call.
    assert started["side_effect_status"] == "not_started", started
    assert started["retry_safe"] is True, started
    assert started.get("quarantined") is not True, started
    assert not blocking_record_states(service.config), blocking_record_states(service.config)
    assert status.get("active") is not True, status


def test_the_start_names_the_configuration_stage_read_that_sent_it_to_adapter_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start is a result the probe selection feeds, so it carries the read that failed.

    `OpenOCDProbeSelection.read_failure` is what tells the two `adapter serial`
    fallbacks apart: the release really is 0.12.0 or newer, or nothing here could
    find out. The tool path puts it on every failure of the call it shaped and the
    `not_supported` refusal publishes it itself; the start built its result from
    `rejected_openocd_commands` alone, so on the debug path the wrapper that
    swallowed `--version`, an explicitly supported `debuggers.<name>.executable`,
    was named nowhere: the session log holds the server's output, not the release
    read's.

    The fake answers the read with the same non-zero exit it answers everything
    with, which is the whole of the scenario: the release read really did fail
    here, and `adapter serial` really was the fallback the server was started
    with.
    """
    from test_debug_sessions import debug_service, start_debug_session

    play(monkeypatch, RESET)
    service = debug_service(tmp_path, debugger_executable=FAKE_TRANSCRIPT, probe_id=SERIAL)
    try:
        started = start_debug_session(service, "attach")
    finally:
        service.close()

    assert started["ok"] is False, started
    read_failure = started["probe_selection_read_failure"]
    assert "OpenOCD release read" in read_failure, read_failure
    # First, because the release and the driver the generic causes talk about are
    # what could not be read: this is the repair.
    assert started["likely_causes"][0] == read_failure, started
    # And the rejected command is still there, unchanged.
    assert started["rejected_commands"] == ["adapter serial"], started


def test_a_server_that_died_for_another_reason_keeps_the_reading_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: only a command this backend sent may be read this way.

    A server whose output names no command of ours stopped somewhere this layer
    cannot place, and the generic classification is the honest answer for it.
    """
    from test_debug_sessions import debug_service, start_debug_session

    play(monkeypatch, {"stdout": "", "stderr": "Open On-Chip Debugger 0.11.0\nError: libusb_open() failed with LIBUSB_ERROR_ACCESS\n", "returncode": 1})
    service = debug_service(tmp_path, debugger_executable=FAKE_TRANSCRIPT, probe_id=SERIAL)
    try:
        started = start_debug_session(service, "attach")
    finally:
        service.close()

    assert started["ok"] is False, started
    assert "rejected_commands" not in started, started
    assert started["error_type"] != "debugger_command_rejected", started
    assert "LIBUSB_ERROR_ACCESS" in session_log(service.config, started)["server_stderr_tail"], started
