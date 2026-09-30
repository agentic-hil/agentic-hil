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
