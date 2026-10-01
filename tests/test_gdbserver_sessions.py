"""Typed debug sessions over each backend's own GDB server (#624).

The session layer was written against OpenOCD's GDB server, and three of its
steps spoke OpenOCD: the reset into halt, the guard that keeps the core halted
when GDB lets go, and the line and words a starting server is read by. Those
steps are now each backend's to answer. The first half of this file holds
OpenOCD to the exact command line and the exact GDB/MI commands it was sent
before that split, so moving the steps changed nothing on the one backend that
already had sessions. The rest holds the other servers to their own recordings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_debug_sessions import debug_service, start_debug_session

from agentic_hil.backends.common import invocation
from agentic_hil.config import resolve_work_path

IMAGE = "<image>"
PORT = "<port>"
OPENOCD_RESET_HALT = '-interpreter-exec console "monitor reset halt"'
OPENOCD_DETACH_GUARD = '-interpreter-exec console "monitor $_TARGETNAME configure -event gdb-detach {}; $_TARGETNAME configure -event gdb-end {}"'
OPENOCD_SESSION_PROLOGUE = [
    "-gdb-set pagination off",
    "-gdb-set confirm off",
    "-gdb-set mi-async on",
    f"-file-exec-and-symbols {IMAGE}",
    f"-target-select extended-remote localhost:{PORT}",
]


def session_log(service, started: dict) -> dict:
    return json.loads((Path(service.config.work_dir) / started["log_path"]).read_text(encoding="utf-8"))


def sent_commands(service, started: dict) -> list[str]:
    """The GDB/MI commands the session sent, in order, with the two values a run chooses replaced.

    The image is staged under a fresh directory and the port is reserved per
    start, so each is named by a placeholder; every other byte is compared.
    """
    port = str(started["gdb_port"])
    commands = []
    for entry in session_log(service, started)["gdb_commands"]:
        command = str(entry["command"])
        if command.startswith("-file-exec-and-symbols "):
            command = f"-file-exec-and-symbols {IMAGE}"
        commands.append(command.replace(f"localhost:{port}", f"localhost:{PORT}"))
    return commands


def run_breakpoint_cycle(service, mode: str) -> dict:
    started = start_debug_session(service, mode=mode)
    assert started["ok"] is True, started
    assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
    continued = service.call("debug_continue", {"timeout_s": 5})
    assert continued["stop_reason"] == "breakpoint_hit", continued
    assert service.call("debug_halt", {"timeout_s": 5})["ok"] is True
    stopped = service.call("debug_stop_session")
    assert stopped["ok"] is True, stopped
    assert stopped["safe_state_confirmed"] is True
    assert stopped["detach_resume_guard_confirmed"] is True
    return started


@pytest.mark.parametrize(
    ("mode", "after_connect"),
    [
        ("attach", []),
        ("reset_halt", [OPENOCD_RESET_HALT]),
        ("load", [OPENOCD_RESET_HALT, "-target-download", OPENOCD_RESET_HALT]),
    ],
)
def test_openocd_sessions_send_the_same_gdb_mi_commands_as_before_the_server_hooks(tmp_path: Path, mode: str, after_connect: list[str]) -> None:
    service = debug_service(tmp_path)
    try:
        started = run_breakpoint_cycle(service, mode)
    finally:
        service.close()

    assert sent_commands(service, started) == [
        *OPENOCD_SESSION_PROLOGUE,
        *after_connect,
        '-break-insert "test_done"',
        "-exec-continue",
        OPENOCD_DETACH_GUARD,
        "-gdb-exit",
    ]


def test_openocd_session_containment_and_teardown_send_the_same_commands_as_before_the_server_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A continue that runs out its time and an explicit halt on a running target, the two paths that interrupt."""
    monkeypatch.setenv("FAKE_GDB_BEHAVIOR", "bench_run_state")
    service = debug_service(tmp_path, fake_gdb_behavior="bench_run_state")
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        continued = service.call("debug_continue", {"timeout_s": 0.5})
        assert continued["ok"] is False, continued
        assert continued["stop_reason"] == "timeout", continued
        assert continued["halt_confirmed"] is True, continued
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
    finally:
        service.close()

    assert sent_commands(service, started) == [
        *OPENOCD_SESSION_PROLOGUE,
        "-exec-continue",
        "-exec-interrupt --all",
        OPENOCD_DETACH_GUARD,
        "-gdb-exit",
    ]


@pytest.mark.parametrize(("mode", "startup"), [("attach", "init; halt"), ("reset_halt", "init; reset halt"), ("load", "init; reset halt")])
def test_openocd_debug_server_command_line_is_unchanged_by_the_server_hooks(tmp_path: Path, mode: str, startup: str) -> None:
    service = debug_service(tmp_path)
    try:
        started = run_breakpoint_cycle(service, mode)
    finally:
        service.close()

    config = service.config
    executable = resolve_work_path(config, config.debugger.executable)
    assert session_log(service, started)["server_command"] == [
        *invocation(str(executable)),
        "-f",
        "interface/stlink.cfg",
        "-f",
        "target/stm32f4x.cfg",
        "-c",
        "bindto 127.0.0.1",
        "-c",
        f"gdb_port {started['gdb_port']}",
        "-c",
        "tcl_port disabled",
        "-c",
        "telnet_port disabled",
        "-c",
        startup,
    ]
