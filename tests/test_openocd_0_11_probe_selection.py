"""OpenOCD 0.11 selects the probe with its adapter driver's own selector, from the bench.

The OpenOCD backend opens the probe a configuration binds by serial with
`adapter serial <serial>`, and OpenOCD 0.11, which Ubuntu 22.04 packages, has no
such command: the generic selector came with 0.12. What 0.11 has is one selector
per adapter driver, and the bench recorded which ones it takes
(fixtures/openocd_0_11_bench_recordings.json): `hla_serial` for the hla driver
`interface/stlink.cfg` loads, `st-link serial` for the st-link driver of
`interface/stlink-dap.cfg`, `cmsis_dap_serial` for cmsis-dap. `jlink serial`
refuses any serial that is not a number, and other drivers have no selector.

So the backend asks the installed OpenOCD which release it is and, on 0.11,
which driver the interface script loads, both at OpenOCD's configuration stage
where no adapter is opened, and selects the probe by that driver's selector.
Where no selector applies, the call is refused before OpenOCD is started for
it, naming the release and the driver: the one thing selection may never do is
leave the serial out and let OpenOCD open whichever probe it finds first. On
0.12 and newer nothing changes, down to the byte.

Every run is answered by fake_openocd_recorded.py with the bench's own
recording of exactly that run, and a run with any other command line is refused
by the fake, so each command line below is the one the bench ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config
from test_bootstrap import NUCLEO_VCP
from test_debug_sessions import debug_service, start_debug_session
from test_openocd_0_11_refusals import blocking_record_states

from agentic_hil.backends import openocd as openocd_backend
from agentic_hil.backends.common import NOT_CONTACTED, CompletedCommand, command_for_log, invocation
from agentic_hil.bootstrap import discover_attached_hardware
from agentic_hil.config import load_config, resolve_work_path
from agentic_hil.tools import AgenticHILToolService

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_RECORDED = FIXTURES / "fake_openocd_recorded.py"
RECORDINGS = json.loads((FIXTURES / "openocd_0_11_bench_recordings.json").read_text(encoding="utf-8"))
RECORDED = RECORDINGS["recordings"]
# The suite's synthetic serial, which the recordings carry in place of the
# bench's own and which the recorded selectors were given.
SERIAL = RECORDINGS["probe_serial"]
DRIVER_RUNS = {
    "hla": "adapter_driver_hla",
    "st-link": "adapter_driver_st_link",
    "cmsis-dap": "adapter_driver_cmsis_dap",
    "jlink": "adapter_driver_jlink",
    "dummy": "adapter_driver_dummy",
    "remote_bitbang": "adapter_driver_remote_bitbang",
}
SELECTOR_RUNS = {"hla": "selector_hla_serial", "st-link": "selector_st_link_serial", "cmsis-dap": "selector_cmsis_dap_serial"}
SELECTOR_ACCEPTED = "AGENTIC_HIL_SELECTOR:accepted"


def replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    version: str | None = "version",
    adapter_driver: str | None = None,
    call: str | None = None,
    scratch: str | None = None,
) -> Path:
    """Name the recording each kind of run is answered with, and where the runs are logged."""
    argv_log = tmp_path / "openocd-runs.jsonl"
    monkeypatch.setenv("AGENTIC_HIL_FAKE_OPENOCD_ARGV_LOG", str(argv_log))
    for variable, name in (("VERSION", version), ("ADAPTER_DRIVER", adapter_driver), ("CALL", call), ("SCRATCH", scratch)):
        if name is None:
            monkeypatch.delenv(f"AGENTIC_HIL_FAKE_OPENOCD_{variable}", raising=False)
        else:
            monkeypatch.setenv(f"AGENTIC_HIL_FAKE_OPENOCD_{variable}", name)
    return argv_log


def runs(argv_log: Path) -> list[list[str]]:
    """Each OpenOCD run the fake was started for, in order, without the program name."""
    if not argv_log.is_file():
        return []
    return [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]


def recorded_argv(name: str, scratch: str | None = None) -> list[str]:
    argv = RECORDED[name]["argv"][1:]
    return [argument.replace("<scratch>", scratch) for argument in argv] if scratch is not None else list(argv)


def call_service(workspace: Path, tool: str, arguments: dict | None = None, **config_kwargs) -> tuple[dict, object]:
    config = load_config(str(write_config(workspace, debugger_executable=FAKE_RECORDED, probe_id=SERIAL, **config_kwargs)))
    service = AgenticHILToolService(config)
    try:
        return service.call(tool, arguments or {}), config
    finally:
        service.close()


def logged_command(config, result: dict) -> str:
    return json.loads((Path(config.workspace_root) / result["log_path"]).read_text(encoding="utf-8"))["command"]


def assert_not_contacted(result: dict) -> None:
    for key, value in NOT_CONTACTED.items():
        assert result.get(key) == value, (key, result)
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantined") is not True, result
    assert "quarantine_guidance" not in result, result


def test_the_recordings_are_openocd_0_11_naming_each_driver_and_taking_its_selector() -> None:
    """What the rest of this module stands on, in the recordings' own words."""
    assert RECORDED["version"]["returncode"] == 0
    assert RECORDED["version"]["stderr"].startswith("Open On-Chip Debugger 0.11.0\n")
    for driver, name in DRIVER_RUNS.items():
        assert RECORDED[name]["returncode"] == 0, name
        assert f"AGENTIC_HIL_ADAPTER_DRIVER:{driver}\n" in RECORDED[name]["stderr"], name
    for driver, name in SELECTOR_RUNS.items():
        assert RECORDED[name]["returncode"] == 0, name
        assert f"{SELECTOR_ACCEPTED}\n" in RECORDED[name]["stderr"], name
        assert RECORDED[name]["argv"][2] == RECORDED[DRIVER_RUNS[driver]]["argv"][2], name
    assert RECORDED["selector_jlink_serial"]["returncode"] == 1
    assert SELECTOR_ACCEPTED not in RECORDED["selector_jlink_serial"]["stderr"]


def test_the_release_and_the_driver_are_read_out_of_openocds_own_words() -> None:
    assert openocd_backend.parse_openocd_version(RECORDED["version"]["stderr"]) == ("0.11.0", (0, 11, 0))
    assert openocd_backend.parse_openocd_version("Open On-Chip Debugger 0.12.0\n") == ("0.12.0", (0, 12, 0))
    assert openocd_backend.parse_openocd_version("Licensed under GNU GPL v2\n") is None
    for driver, name in DRIVER_RUNS.items():
        assert openocd_backend.parse_openocd_adapter_driver(RECORDED[name]["stdout"] + RECORDED[name]["stderr"]) == driver, name
    # An interface script that loads no driver: OpenOCD answers in a word of its own.
    undefined = RECORDED["adapter_driver_undefined"]
    assert openocd_backend.parse_openocd_adapter_driver(undefined["stdout"] + undefined["stderr"]) == "undefined"
    # A script OpenOCD could not find: no answer at all, rather than a guess.
    missing = RECORDED["adapter_driver_missing_script"]
    assert openocd_backend.parse_openocd_adapter_driver(missing["stdout"] + missing["stderr"]) is None


@pytest.mark.parametrize("driver", sorted(SELECTOR_RUNS))
def test_on_openocd_0_11_the_probe_is_selected_by_the_drivers_own_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str) -> None:
    """The selector is the one the bench gave the synthetic serial and OpenOCD 0.11 took."""
    replay(monkeypatch, tmp_path, adapter_driver=DRIVER_RUNS[driver])
    interface_cfg = RECORDED[DRIVER_RUNS[driver]]["argv"][2]

    selection = openocd_backend.openocd_probe_selection(str(FAKE_RECORDED), interface_cfg, SERIAL, 5)

    accepted = RECORDED[SELECTOR_RUNS[driver]]["argv"]
    assert selection.supported is True, selection
    assert list(selection.commands) == ["-c", accepted[accepted.index("-c") + 1]], selection
    assert selection.version == "0.11.0"
    assert selection.adapter_driver == driver


def test_probe_target_on_openocd_0_11_selects_by_hla_serial_and_reads_the_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_hla", call="probe_target_hla_serial")

    result, config = call_service(tmp_path, "probe_target")

    assert result["ok"] is True, result
    assert result["target_detected"] is True, result
    assert runs(argv_log) == [["--version"], recorded_argv("adapter_driver_hla"), recorded_argv("probe_target_hla_serial")]
    assert f"hla_serial {SERIAL}" in logged_command(config, result)
    assert "adapter serial" not in logged_command(config, result)


def test_reset_target_on_openocd_0_11_selects_by_hla_serial_and_resets_the_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_hla", call="reset_target_run_hla_serial")

    result, config = call_service(tmp_path, "reset_target", {"mode": "run"})

    assert result["ok"] is True, result
    assert result["success_confirmed"] is True, result
    assert result["mode"] == "run"
    assert runs(argv_log)[-1] == recorded_argv("reset_target_run_hla_serial")
    assert not blocking_record_states(config), blocking_record_states(config)


def test_a_serial_no_attached_probe_carries_is_a_probe_that_was_not_found_and_nothing_else_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hla_serial` is a filter, not a preference: OpenOCD 0.11 opens nothing when nothing matches."""
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_hla", call="probe_target_hla_serial_no_such_probe")

    result, config = call_service(tmp_path, "probe_target")

    assert runs(argv_log)[-1] == recorded_argv("probe_target_hla_serial_no_such_probe")
    assert result["ok"] is False, result
    assert result["error_type"] == "adapter_not_found", result
    assert "rejected_commands" not in result, result
    assert_not_contacted(result)
    assert not blocking_record_states(config), blocking_record_states(config)


@pytest.mark.parametrize(("tool", "arguments"), [("probe_target", {}), ("reset_target", {"mode": "run"})])
def test_a_driver_with_no_selector_on_openocd_0_11_is_refused_before_openocd_is_started_for_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict
) -> None:
    """J-Link on 0.11: `jlink serial` takes numbers only, so the configured serial cannot be passed.

    Refused rather than sent without a selector, and refused before the call's
    own OpenOCD run: the two reads before it are configuration-stage runs that
    open no adapter, so the board is exactly as the last call left it.
    """
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_jlink")

    result, config = call_service(tmp_path, tool, arguments, interface_cfg="interface/jlink.cfg")

    assert runs(argv_log) == [["--version"], recorded_argv("adapter_driver_jlink")]
    assert result["ok"] is False, result
    assert result["error_type"] == "not_supported", result
    assert result["backend_error_type"] == "probe_selection_not_supported", result
    assert result["openocd_version"] == "0.11.0", result
    assert result["adapter_driver"] == "jlink", result
    assert "0.11.0" in result["summary"] and "jlink" in result["summary"], result["summary"]
    assert result["remediation"], result
    assert_not_contacted(result)
    assert not blocking_record_states(config), blocking_record_states(config)


@pytest.mark.parametrize("driver", ["dummy", "remote_bitbang"])
def test_drivers_with_no_selector_of_their_own_select_nothing_on_openocd_0_11(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str) -> None:
    scratch = tmp_path.as_posix()
    replay(monkeypatch, tmp_path, adapter_driver=DRIVER_RUNS[driver], scratch=scratch)
    interface_cfg = recorded_argv(DRIVER_RUNS[driver], scratch)[1]

    selection = openocd_backend.openocd_probe_selection(str(FAKE_RECORDED), interface_cfg, SERIAL, 5)

    assert selection.supported is False, selection
    assert selection.commands == (), selection
    assert selection.version == "0.11.0"
    assert selection.adapter_driver == driver


def test_an_openocd_whose_release_cannot_be_read_is_still_given_a_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapter serial`, which 0.12 and newer take and 0.11 refuses before it opens the probe.

    Either way no probe is opened that the serial does not name. The call's own
    run is answered by no recording here, so only its command line is held.
    """
    argv_log = replay(monkeypatch, tmp_path, version=None)

    call_service(tmp_path, "probe_target")

    started = runs(argv_log)
    assert started[0] == ["--version"]
    assert started[-1][:6] == ["-f", "interface/stlink.cfg", "-c", f"adapter serial {SERIAL}", "-f", "target/stm32f4x.cfg"], started


def test_an_interface_script_openocd_0_11_cannot_find_is_still_given_a_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The driver read fails on the missing script, and the call names the script's own failure.

    OpenOCD evaluates `-f` before the `-c` behind it, so the call stops at the
    script, before any selector is read and before any probe is opened.
    """
    missing = RECORDED["adapter_driver_missing_script"]["argv"][2]
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_missing_script")

    call_service(tmp_path, "probe_target", interface_cfg=missing)

    started = runs(argv_log)
    assert started[:2] == [["--version"], recorded_argv("adapter_driver_missing_script")]
    assert started[-1][:4] == ["-f", missing, "-c", f"adapter serial {SERIAL}"], started


def test_a_selector_openocd_refuses_is_a_command_rejected_before_init() -> None:
    """The bench's run of `hla_serial` against the jlink driver, read the way a call's output is."""
    refused = RECORDED["selector_hla_serial_with_jlink"]
    output = refused["stdout"] + refused["stderr"]
    call = f'{openocd_backend.OPENOCD_INIT_PREFIX}targets; echo "AGENTIC_HIL_RESULT:probe_target:ok"; shutdown'

    assert openocd_backend.rejected_openocd_commands(call, output, (f"hla_serial {SERIAL}",)) == ["hla_serial"]


@pytest.mark.parametrize(
    ("tool", "arguments", "command"),
    [
        ("probe_target", {}, 'targets; echo "AGENTIC_HIL_RESULT:probe_target:ok"; shutdown'),
        ("reset_target", {"mode": "run"}, 'reset run; echo "AGENTIC_HIL_RESULT:reset_target:ok"; shutdown'),
    ],
)
def test_on_openocd_0_12_the_command_line_is_byte_for_byte_the_one_it_always_was(tmp_path: Path, tool: str, arguments: dict, command: str) -> None:
    """fake_openocd.py answers `--version` the way 0.12 does, and nothing about the call moves."""
    config = load_config(str(write_config(tmp_path, probe_id=SERIAL)))
    service = AgenticHILToolService(config)
    try:
        result = service.call(tool, arguments)
    finally:
        service.close()

    assert result["ok"] is True, result
    executable = resolve_work_path(config, config.debugger.executable)
    assert logged_command(config, result) == command_for_log(
        [
            *invocation(str(executable)),
            "-f",
            "interface/stlink.cfg",
            "-c",
            f"adapter serial {SERIAL}",
            "-f",
            "target/stm32f4x.cfg",
            "-c",
            "gdb_port disabled",
            "-c",
            "tcl_port disabled",
            "-c",
            "telnet_port disabled",
            "-c",
            f'init; echo "AGENTIC_HIL_STAGE:init:ok"; {command}',
        ]
    )


def test_on_openocd_0_12_the_debug_server_is_started_exactly_as_it_always_was(tmp_path: Path) -> None:
    service = debug_service(tmp_path, probe_id=SERIAL)
    try:
        started = start_debug_session(service)
        assert started["ok"] is True, started
        service.call("debug_stop_session")
    finally:
        service.close()

    config = service.config
    executable = resolve_work_path(config, config.debugger.executable)
    server_command = json.loads((Path(config.work_dir) / started["log_path"]).read_text(encoding="utf-8"))["server_command"]
    assert server_command == [
        *invocation(str(executable)),
        "-f",
        "interface/stlink.cfg",
        "-c",
        f"adapter serial {SERIAL}",
        "-f",
        "target/stm32f4x.cfg",
        "-c",
        "bindto 127.0.0.1",
        "-c",
        f"gdb_port {started['session']['gdb_port']}",
        "-c",
        "tcl_port disabled",
        "-c",
        "telnet_port disabled",
        "-c",
        "init; reset halt",
    ]


def test_the_debug_server_on_openocd_0_11_is_started_with_the_drivers_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command line a session start hands the server, read off the backend before any server runs.

    No recording holds a debug server's run on 0.11, so the server is not
    started here; the bench tier starts one. What is held is that the server is
    given the selector the two configuration-stage reads chose, in the place the
    interface script's selector goes.
    """
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_hla")
    config = load_config(str(write_config(tmp_path, debugger_executable=FAKE_RECORDED, probe_id=SERIAL)))
    backend = openocd_backend.OpenOCDBackend(config)
    try:
        resolved = backend._resolve_debug_server()
        assert resolved["ok"] is True, resolved
        server_args = backend._debug_server_args(str(resolved["executable_path"]), 3333, False)
    finally:
        backend.close()

    assert runs(argv_log) == [["--version"], recorded_argv("adapter_driver_hla")]
    assert server_args == [
        *invocation(str(resolved["executable_path"])),
        "-f",
        "interface/stlink.cfg",
        "-c",
        f"hla_serial {SERIAL}",
        "-f",
        "target/stm32f4x.cfg",
        "-c",
        "bindto 127.0.0.1",
        "-c",
        "gdb_port 3333",
        "-c",
        "tcl_port disabled",
        "-c",
        "telnet_port disabled",
        "-c",
        "init; halt",
    ]


def test_a_debug_session_on_openocd_0_11_with_no_selector_for_the_driver_is_refused_before_the_server_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_jlink")
    service = debug_service(tmp_path, debugger_executable=FAKE_RECORDED, probe_id=SERIAL, interface_cfg="interface/jlink.cfg")
    try:
        started = start_debug_session(service)
        status = service.call("debug_get_session_status")
    finally:
        service.close()

    assert runs(argv_log) == [["--version"], recorded_argv("adapter_driver_jlink")]
    assert started["ok"] is False, started
    assert started["error_type"] == "not_supported", started
    assert started["backend_error_type"] == "probe_selection_not_supported", started
    assert "0.11.0" in started["summary"] and "jlink" in started["summary"], started["summary"]
    assert_not_contacted(started)
    assert status.get("active") is not True, status


def discovery_on_a_host_with_openocd_0_11(monkeypatch: pytest.MonkeyPatch, profile: dict) -> tuple[dict, list[list[str]]]:
    """Bootstrap's target read, with OpenOCD resolving to the recorded 0.11 and the read itself captured.

    The read's answer is not what is under test, so it answers with nothing; the
    two configuration-stage reads before it go to the recorded OpenOCD.
    """
    target_reads: list[list[str]] = []

    def capture(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        target_reads.append(command)
        return CompletedCommand("", "", 0, False, False)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: str(FAKE_RECORDED))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", capture)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "tool": tool, "ports": [NUCLEO_VCP]})
    return discover_attached_hardware(probe_id=SERIAL, profile=profile), target_reads


def test_bootstrap_reads_the_target_on_openocd_0_11_through_hla_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_hla")

    _, target_reads = discovery_on_a_host_with_openocd_0_11(monkeypatch, {"target": {"name": "demo"}})

    assert runs(argv_log) == [["--version"], recorded_argv("adapter_driver_hla")]
    assert target_reads == [
        [
            *invocation(str(FAKE_RECORDED)),
            "-f",
            "interface/stlink.cfg",
            "-c",
            f"hla_serial {SERIAL}",
            "-f",
            "target/stm32f4x.cfg",
            "-c",
            "init",
            "-c",
            "targets",
            "-c",
            "shutdown",
        ]
    ]


def test_bootstrap_on_openocd_0_11_says_nothing_to_a_board_whose_driver_has_no_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_jlink")
    profile = {"target": {"name": "demo"}, "debuggers": {"dut": {"interface_cfg": "interface/jlink.cfg"}}}

    result, target_reads = discovery_on_a_host_with_openocd_0_11(monkeypatch, profile)

    assert runs(argv_log) == [["--version"], recorded_argv("adapter_driver_jlink")]
    assert target_reads == []
    # The probe is still written down, with the controller left for a person to name.
    assert result["ok"] is True, result
    assert result["target"] is None, result
    assert result["hardware_state"] == "unchanged", result
    summary = result["target_discovery"]["summary"]
    assert "0.11.0" in summary and "jlink" in summary, result["target_discovery"]


def test_a_release_read_that_failed_says_why_and_the_refusal_it_causes_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two reads collapsed five outcomes into a bare None, and nothing recorded any of them.

    `openocd_version` and `adapter_driver` are published only on the
    `not_supported` refusal, so no success path and no other failure path said
    the reads had happened or failed, and `_write_log` keeps only the call's own
    argv and streams. This repository explicitly supports
    `debuggers.<name>.executable` being a wrapper, and names one as a
    `likely_cause` elsewhere: put one that swallows `--version` in front of
    OpenOCD 0.11 and the release read answers nothing, the backend falls back to
    `adapter serial`, and 0.11 refuses it. The result named `adapter serial` and
    three generic causes with no `openocd_version` field at all, so nothing
    pointed at the repair, which is to make the wrapper pass `--version` through.
    """
    reads: list[tuple[str, float]] = []

    def a_wrapper_that_swallows_the_banner(executable_path: str, timeout_s: float) -> tuple[object, str | None]:
        reads.append((executable_path, timeout_s))
        return None, openocd_backend.read_failure_reason(
            CompletedCommand("", "", 0, False, False), "OpenOCD release read (`--version`)", timeout_s, "printing a version banner"
        )

    selection = openocd_backend.openocd_probe_selection(
        str(FAKE_RECORDED), "interface/stlink.cfg", SERIAL, 5, read_release=a_wrapper_that_swallows_the_banner
    )

    assert reads, "the release read was never asked"
    assert list(selection.commands) == ["-c", f"adapter serial {SERIAL}"], selection
    assert selection.supported is True, selection
    assert selection.version is None and selection.adapter_driver is None, selection
    # The decisive line, which is the whole of what used to be dropped.
    assert selection.read_failure is not None
    assert "exited 0 without printing a version banner" in selection.read_failure, selection.read_failure


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        (CompletedCommand("", "", 1, False, True), "could not be started: the file is not there"),
        (CompletedCommand("", "", 1, False, False, "not executable"), "could not be started: the file is not executable"),
        (CompletedCommand("", "", 1, True, False), "did not answer within 5s"),
        (CompletedCommand("", "wrapper: cannot exec openocd\n", 127, False, False), "exited 127: wrapper: cannot exec openocd"),
        (CompletedCommand("", "", 2, False, False), "exited 2 and wrote nothing"),
        (CompletedCommand("quiet wrapper\n", "", 0, False, False), "exited 0 without naming a release; its last line was: quiet wrapper"),
    ],
)
def test_each_way_a_read_can_fail_says_which_one_it_was(completed: CompletedCommand, expected: str) -> None:
    """Five outcomes, five sentences, and OpenOCD's own last line where it wrote one.

    The path is deliberately not in any of them: every result that carries this
    carries `executable` beside it, and these travel into `likely_causes`."""
    reason = openocd_backend.read_failure_reason(completed, "OpenOCD release read (`--version`)", 5, "naming a release")

    assert expected in reason, reason
    assert "OpenOCD release read" in reason, reason


def test_a_refusal_caused_by_a_failed_driver_read_carries_that_reads_own_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 0.11 whose interface script cannot be found: the driver read fails and the call is refused.

    The release reads fine, so `openocd_version` is on the result; what is missing
    without this is any statement that the driver could not be read, and that is
    what sent the call to `adapter serial`. The refusal the call then gets names
    the read first, because the release and the driver below it are what could not
    be learned.
    """
    missing = RECORDED["adapter_driver_missing_script"]["argv"][2]
    replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_missing_script")

    result, _ = call_service(tmp_path, "probe_target", interface_cfg=missing)

    assert result["ok"] is False, result
    read_failure = result["probe_selection_read_failure"]
    assert "adapter driver read" in read_failure, read_failure
    assert missing in read_failure, read_failure
    assert result["likely_causes"][0] == read_failure, result["likely_causes"]


def test_a_read_that_answered_keeps_the_result_free_of_a_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: a 0.11 that named its release and its driver reports no failed read.

    This is what tells the two `adapter serial` fallbacks apart, so the field must
    be absent where nothing failed rather than carrying an empty string.
    """
    replay(monkeypatch, tmp_path, adapter_driver="adapter_driver_hla", call="probe_target_hla_serial_no_such_probe")

    result, _ = call_service(tmp_path, "probe_target")

    assert result["ok"] is False, result
    assert "probe_selection_read_failure" not in result, result
