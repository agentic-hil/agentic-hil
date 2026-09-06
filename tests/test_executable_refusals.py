"""A configured debugger executable that is there and will not run is a refusal.

The condition behind #479: `debuggers.<name>.executable` names a file that
exists, is a regular file, and cannot be executed. A toolchain unpacked out of an
archive that did not carry the execute bit is the ordinary way to get there; a
script without a shebang and a binary built for another architecture are the
other two, and each of them fails at the same call with a different operating
system error.

`spawn_command` caught `FileNotFoundError` and nothing else, so the refusal
travelled out of the backend as an exception, past `entrypoint`, which catches
`ConfigError` and `CoordinationError`. `agentic-hil doctor` ended in a traceback
with zero bytes on stdout, `doctor --json` wrote no document at all, and over MCP
the same call became an internal protocol error instead of a refusal an agent can
read.

The neighbours matter as much as the new case. A missing file keeps the refusal
and the wording it has always had, a healthy executable is untouched, and a spawn
failure that is not an exec refusal (a host out of file descriptors, say) still
raises rather than being reported as a broken toolchain.

Two of the three failing modes are real on any host: a file that is not an
executable image is refused by Linux with ENOEXEC and by Windows with
ERROR_BAD_EXE_FORMAT, which CPython reports as the same errno. The third, a file
whose mode withholds the execute bit, is a POSIX condition Windows cannot
express, so it is exercised for real where the modes are real and through a
patched raise at the product's own spawn boundary everywhere else.
"""

from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path

import pytest
from conftest import (
    FAKE_OPENOCD,
    write_authoritative_config,
    write_config,
)

from agentic_hil import cli
from agentic_hil.backends import common as backend_common
from agentic_hil.config import load_config
from agentic_hil.knowledge import remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="file modes are a POSIX condition; Windows cannot withhold the execute bit")

# The error the operating system raises when the file is there and the caller may
# not run it. Errno 13 on both platforms: POSIX raises it out of `execve`, and
# Windows maps ERROR_ACCESS_DENIED onto the same value.
PERMISSION_REFUSED = PermissionError(errno.EACCES, "Permission denied")
# A spawn failure that says nothing about the configured file. It must keep
# raising: reporting a host out of file descriptors as a broken toolchain would
# send an operator to fix a file that is fine.
HOST_EXHAUSTED = OSError(errno.EMFILE, "Too many open files")


def unrunnable_tool(directory: Path, name: str = "openocd-broken") -> Path:
    """A regular file, present, that no host will execute as a program.

    The execute bit is set on purpose. Without it POSIX refuses with EACCES,
    which is the other failing mode; with it, the kernel gets as far as reading
    the file, finds neither an ELF header nor a shebang, and refuses with
    ENOEXEC. Windows ignores the mode and refuses the same file for its content,
    so one fixture reaches the exec-format refusal on either host.

    No `.py` suffix, because `invocation` hands a `.py` path to this
    interpreter rather than to the operating system, and that path spawns a
    Python that runs fine.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("this file is not a program\n", encoding="utf-8")
    os.chmod(tool, 0o755)
    return tool


def unreadable_mode_tool(directory: Path, name: str = "openocd-no-exec-bit") -> Path:
    """The archive case: a real toolchain file that arrived mode 0644."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_bytes(FAKE_OPENOCD.read_bytes())
    os.chmod(tool, 0o644)
    return tool


def refuse_the_spawn(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    """Raise at the product's own spawn boundary, as the platform would."""

    def refuse(args, **kwargs):
        raise error

    monkeypatch.setattr(backend_common, "spawn_managed_process", refuse)


def service_for(workspace: Path, tool: Path, **kwargs) -> AgenticHILToolService:
    return AgenticHILToolService(load_config(str(write_config(workspace, debugger_executable=tool, **kwargs))))


# ---------------------------------------------------------------------------
# The refusal itself, on the tool surface.


@pytest.mark.parametrize("tool_name", ["probe_target", "debugger_info"])
def test_a_debugger_executable_that_will_not_run_is_a_structured_refusal(tmp_path: Path, tool_name: str) -> None:
    """The whole of #479 in one call: an answer, not an exception."""
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        result = service.call(tool_name)
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "debugger_not_executable"
    # The configured path, in the result and in the sentence, because the
    # operator has to be told which file to look at and a bench has more than one.
    assert Path(result["executable"]) == tmp_path / "toolchain" / "openocd-broken"
    assert result["executable"] in result["summary"]
    # The two halves of the sentence the issue asks for: the file is there, and
    # this host will not run it.
    assert "will not run" in result["summary"]
    # The next step an operator can act on, out of the one catalogue, exactly as
    # its neighbours carry theirs.
    assert result["remediation"] == remediation_fields("debugger_not_executable")["remediation"]
    assert result["remediation"]


def test_the_refusal_says_which_of_the_two_failing_modes_this_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mode to fix and an image to replace are different repairs."""
    refuse_the_spawn(monkeypatch, PERMISSION_REFUSED)
    permission_service = service_for(tmp_path, unreadable_mode_tool(tmp_path / "toolchain"))
    try:
        refused = permission_service.call("probe_target")
    finally:
        permission_service.close()
    monkeypatch.undo()
    format_service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        malformed = format_service.call("probe_target")
    finally:
        format_service.close()

    assert refused["error_type"] == malformed["error_type"] == "debugger_not_executable"
    assert refused["not_executable_reason"] == "permission_denied"
    assert malformed["not_executable_reason"] == "not_an_executable_image"
    assert refused["summary"] != malformed["summary"]
    # The operating system's own words for the refusal, kept verbatim: the
    # classification is this project's reading of it and the line is the
    # evidence behind the reading.
    assert "Permission denied" in refused["spawn_error"]


@POSIX_ONLY
def test_the_real_mode_is_refused_the_same_way(tmp_path: Path) -> None:
    """The archive case, with no patch anywhere: mode 0644 on a real file."""
    service = service_for(tmp_path, unreadable_mode_tool(tmp_path / "toolchain"))
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "debugger_not_executable"
    assert result["not_executable_reason"] == "permission_denied"


@pytest.mark.parametrize("debugger_type", ["openocd", "pyocd", "stlink"])
def test_every_backend_answers_the_same_way(tmp_path: Path, debugger_type: str) -> None:
    """The classification belongs where the process is spawned, so it is one
    answer for the three backends rather than three that can drift."""
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"), debugger_type=debugger_type)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["error_type"] == "debugger_not_executable"
    assert result["backend"] == debugger_type


def test_a_reset_that_cannot_spawn_its_debugger_never_reached_the_bench(tmp_path: Path) -> None:
    """The effectful tools answer it too, and prove their abort point.

    Nothing was spawned, so nothing can have touched the board: the refusal
    carries the not-contacted markers its missing-file neighbour carries, and
    the service must not quarantine hardware over a call that never started.
    """
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        result = service.call("reset_target", {"mode": "run"})
    finally:
        service.close()

    assert result["error_type"] == "debugger_not_executable"
    assert result["target_contacted"] is False
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["hardware_state"] == "unchanged"
    assert result["retry_safe"] is True
    assert result.get("quarantined") is not True


def test_a_flash_that_cannot_spawn_its_debugger_never_reached_the_bench(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert result["error_type"] == "debugger_not_executable"
    assert result["side_effect_status"] == "not_started"
    assert result.get("quarantined") is not True


# ---------------------------------------------------------------------------
# The two frontends the issue names.


def test_doctor_reports_it_instead_of_ending_in_a_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    tool = unrunnable_tool(tmp_path / "toolchain")
    write_authoritative_config(workspace, monkeypatch, debugger_executable=tool, probe_id="ST-LINK-1")
    monkeypatch.chdir(workspace)

    report = cli.doctor()

    assert report["ok"] is False
    assert "debuggers" in report["unhealthy"]
    check = report["debuggers"]["dut"]["check"]
    assert check["ok"] is False
    assert check["error_type"] == "debugger_not_executable"
    assert Path(check["executable"]) == tool
    assert check["executable"] in check["summary"]
    assert check["remediation"]


def test_the_command_line_exits_one_with_the_document_on_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`doctor --json` wrote zero bytes and a traceback. It writes a document."""
    workspace = tmp_path / "workspace"
    tool = unrunnable_tool(tmp_path / "toolchain")
    write_authoritative_config(workspace, monkeypatch, debugger_executable=tool, probe_id="ST-LINK-1")
    monkeypatch.chdir(workspace)
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    code = cli.entrypoint(["doctor", "--json"])

    written = stdout.getvalue()
    assert code == 1
    assert written.strip(), "doctor --json wrote nothing"
    document = json.loads(written)
    assert document["tool"] == "agentic_hil_doctor"
    assert document["ok"] is False
    assert "debuggers" in document["unhealthy"]
    assert document["debuggers"]["dut"]["check"]["error_type"] == "debugger_not_executable"


@pytest.mark.parametrize("tool_name", ["debugger_info", "probe_target"])
def test_the_mcp_call_answers_a_refusal_rather_than_a_protocol_error(tmp_path: Path, tool_name: str) -> None:
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        response = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": {}}},
            service,
        )
    finally:
        service.close()

    assert isinstance(response, dict)
    # An agent reads the tool result. An `error` member is the transport saying
    # the call could not be made at all, which is what the raised OSError turned
    # `debugger_info` into: JSON-RPC -32603, with nothing in it an agent can act
    # on.
    assert "error" not in response, response.get("error")
    payload = response["result"]
    assert payload["isError"] is True
    assert payload["structuredContent"]["error_type"] == "debugger_not_executable"
    assert "will not run" in payload["content"][0]["text"]


# ---------------------------------------------------------------------------
# The neighbours, pinned unchanged.


def test_a_missing_executable_keeps_its_refusal_and_its_wording(tmp_path: Path) -> None:
    service = service_for(tmp_path, tmp_path / "toolchain" / "openocd-absent")
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["error_type"] == "debugger_not_found"
    assert result["backend_error_type"] == "openocd_not_found"
    assert result["summary"] == "Debugger executable could not be found."
    assert result["likely_causes"] == [
        "debuggers.<name>.executable is not configured",
        "debugger executable is not installed",
        "debugger executable is not in PATH",
    ]


def test_an_executable_that_disappears_before_the_spawn_keeps_its_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn-time half of the missing-file refusal, which is the branch this
    change sits beside: the file resolved and was gone a moment later."""
    refuse_the_spawn(monkeypatch, FileNotFoundError(errno.ENOENT, "No such file or directory"))
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["error_type"] == "debugger_not_found"
    assert result["backend_error_type"] == "openocd_not_found"
    assert result["summary"] == "Debugger executable could not be found."


def test_a_debugger_that_runs_is_untouched(tmp_path: Path) -> None:
    service = service_for(tmp_path, FAKE_OPENOCD)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True, json.dumps(result)
    assert "not_executable_reason" not in result


def test_a_spawn_failure_that_is_not_an_exec_refusal_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Widening the except clause to every OSError would report a host that ran
    out of file descriptors as a toolchain the operator has to repair."""
    refuse_the_spawn(monkeypatch, HOST_EXHAUSTED)
    service = service_for(tmp_path, unrunnable_tool(tmp_path / "toolchain"))
    try:
        with pytest.raises(OSError) as raised:
            service.backend.info()
    finally:
        service.close()

    assert raised.value.errno == errno.EMFILE
