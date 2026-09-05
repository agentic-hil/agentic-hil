from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, FAKE_GDB, FAKE_OPENOCD, write_authoritative_config, write_config

from agentic_hil.backends.gdbdebug import decode_symbol_value
from agentic_hil.cli import build_parser, entrypoint
from agentic_hil.config import ConfigError, load_config
from agentic_hil.knowledge import LISTEN_ONLY_MODE_ERROR
from agentic_hil.test_reactor import DebuggerRunner, TestReactor, UartRunner, load_test_config, merge_result_status
from agentic_hil.tools import AgenticHILToolService


class FakeComPortService:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.closed = False

    def reconfigure(self, config) -> None:
        pass

    def session_start(self, port_id: str, clear_buffer: bool = True) -> dict:
        already_active = port_id in self.active
        self.active.add(port_id)
        return {"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": already_active}

    def session_stop(self, port_id: str) -> dict:
        was_active = port_id in self.active
        self.active.discard(port_id)
        return {"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": was_active}

    def close(self) -> None:
        self.closed = True
        self.active.clear()


class RecordingService:
    def __init__(
        self,
        *,
        fail_cleanup: bool = False,
        raise_flash: bool = False,
        audit_flash_failure: bool = False,
        audit_failure_call: str | None = None,
        fail_call: str | None = None,
        uart_reads: list[bytes] | None = None,
        uart_encoding: str = "utf-8",
        can_reads: list[list[dict]] | None = None,
        reset_result: dict | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_cleanup = fail_cleanup
        self.raise_flash = raise_flash
        self.audit_flash_failure = audit_flash_failure
        self.audit_failure_call = audit_failure_call
        self.fail_call = fail_call
        # What the line says, one entry per com_read, in the order it says it. An
        # exhausted list is a silent port, which is exactly what a board that
        # never printed its banner looks like from here.
        self.uart_reads = list(uart_reads or [])
        self.uart_encoding = uart_encoding
        # What the bus carries, one entry per can_read, in the order it carries
        # it. An exhausted list is a quiet bus, which is what a node that never
        # sent the frame a plan is waiting for looks like from here.
        self.can_reads = list(can_reads or [])
        self.reset_result = reset_result
        self.breakpoint_id = 0
        self.recovery_calls: list[list[str]] = []

    def recover_after_failed_run(self, devices: list[str] | None = None) -> dict:
        # Part of the service contract the reactor drives, so the double carries
        # it: every abort calls this, and a double that lacked it would only
        # prove the seam is never reached.
        self.recovery_calls.append(sorted(devices or []))
        return {"attempted": True, "actions": ["reap_processes", "reset_halt", "probe_target"], "outcome": "recovered", "incident_resolved": False}

    def call(self, name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == self.fail_call:
            return {"ok": False, "tool": name, "error_type": "can_read_failed", "summary": "adapter would not read"}
        if name == "can_session_start":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "already_active": False}
        if name == "can_session_stop":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "was_active": True}
        if name == "can_read":
            frames = self.can_reads.pop(0) if self.can_reads else []
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "frames": frames, "frames_read": len(frames)}
        if name == "can_send":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "frame": {"id": arguments.get("frame_id")}}
        if name == "flash_firmware" and self.raise_flash:
            raise OSError("flash transport failed")
        if name == "flash_firmware" and self.audit_flash_failure:
            return {
                "ok": True,
                "audit_ok": False,
                "audit_error": {"error_type": "unsafe_configured_path"},
                "side_effect_committed": True,
                "retry_safe": False,
            }
        if name == self.audit_failure_call:
            result = {"ok": True, "audit_ok": False, "audit_error": {"error_type": "audit_failed"}, "side_effect_committed": True, "retry_safe": False}
            if name == "debug_set_breakpoint":
                self.breakpoint_id += 1
                result["breakpoint"] = {"id": self.breakpoint_id}
            return result
        if name == "com_session_start":
            return {"ok": True, "already_active": False}
        if name == "com_session_stop":
            return {"ok": True}
        if name == "com_read":
            chunk = self.uart_reads.pop(0) if self.uart_reads else b""
            # The shape com_read really returns: raw bytes as hex beside the
            # decoded text, so a caller that reassembles across reads has
            # something to reassemble that a split character cannot corrupt.
            return {
                "ok": True,
                "tool": name,
                "port_id": arguments.get("port_id"),
                "bytes_read": len(chunk),
                "buffer_remaining_bytes": 0,
                "data": {"hex": chunk.hex(), "text": chunk.decode(self.uart_encoding, errors="replace"), "encoding": self.uart_encoding},
            }
        if name == "reset_target":
            return self.reset_result if self.reset_result is not None else {"ok": True, "tool": name, "mode": arguments.get("mode")}
        if name == "debug_start_session":
            return {"ok": True, "target_ok": True}
        if name == "debug_stop_session":
            if self.fail_cleanup:
                raise OSError("debugger would not stop")
            return {"ok": True}
        if name == "debug_dump_symbol_ihex":
            return {"ok": False, "error_type": "memory_read_failed", "summary": "read failed"}
        if name == "debug_set_breakpoint":
            self.breakpoint_id += 1
            return {"ok": True, "breakpoint": {"id": self.breakpoint_id}}
        if name == "debug_continue":
            return {"ok": True, "stop_reason": "breakpoint_hit", "stop": {"breakpoint_id": self.breakpoint_id}}
        if name == "debug_clear_breakpoints":
            return {"ok": True, "cleared": 1}
        return {"ok": True}


def reactor_service(tmp_path: Path, behavior: str | None = None) -> tuple[AgenticHILToolService, FakeComPortService]:
    config_path = write_config(
        tmp_path,
        gdb_executable=FAKE_GDB,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_data = b"\x7fELF" + b"\x00" * 12
    if behavior is not None:
        elf_data += f"\nFAKE_GDB_BEHAVIOR={behavior}\n".encode()
    elf_path.write_bytes(elf_data)
    com_ports = FakeComPortService()
    service = AgenticHILToolService(load_config(str(config_path)), com_ports=com_ports)  # type: ignore[arg-type]
    return service, com_ports


def write_test_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def adopt_argv(next_step: str) -> list[str]:
    """The `agentic-hil adopt-hardware ...` command a next step names, as argv.

    The advertised repair has to be one the CLI actually accepts: a reader who
    copies it must not land on `unrecognized arguments` and exit 2, which is what
    `adopt-hardware --apply` did (round 1, finding 2). So the tests parse the
    emitted command against the real parser rather than only matching substrings.
    """
    match = re.search(r"`agentic-hil (adopt-hardware[^`]*)`", next_step)
    assert match is not None, f"no adopt-hardware command found in: {next_step}"
    return match.group(1).split()


def test_reactor_flattens_and_deduplicates_all_audit_errors() -> None:
    first = {"audit_ok": False, "audit_error": {"error_type": "first"}, "audit_errors": [{"error_type": "first"}, {"error_type": "second"}]}
    cleanup = {"audit_ok": False, "audit_error": {"error_type": "cleanup"}, "audit_errors": [{"error_type": "second"}, {"error_type": "cleanup"}]}

    result = merge_result_status({"ok": False}, first, cleanup)

    assert result["audit_error"] == {"error_type": "first"}
    assert result["audit_errors"] == [{"error_type": "first"}, {"error_type": "second"}, {"error_type": "cleanup"}]
    assert result["retry_safe"] is False


def test_reactor_runs_flash_uart_breakpoint_and_memory_dump(tmp_path: Path) -> None:
    service, com_ports = reactor_service(tmp_path)
    test_path = write_test_config(
        tmp_path,
        """version: 2
name: capture-state
steps:
  - debugger: dut
    action: flash
    image_path: build/app.elf
  - port_id: dut_uart
    action: uart_open
  - debugger: dut
    action: debug_start
    image_path: build/app.elf
    mode: attach
  - debugger: dut
    action: run_until_breakpoint
    location:
      symbol: test_done
    timeout_s: 5
  - debugger: dut
    action: dump_memory
    symbol: CTC_array
    output_path: build/new/nested/memory.hex
  - debugger: dut
    action: debug_stop
  - port_id: dut_uart
    action: uart_close
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(test_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == [
        "flash",
        "uart_open",
        "debug_start",
        "run_until_breakpoint",
        "dump_memory",
        "debug_stop",
        "uart_close",
    ]
    assert result["cleanup"] == []
    assert com_ports.active == set()
    hex_lines = (tmp_path / "build" / "new" / "nested" / "memory.hex").read_text(encoding="ascii").splitlines()
    assert hex_lines[0] == ":020000042000DA"
    assert hex_lines[-1] == ":00000001FF"


def test_reactor_stops_debug_and_uart_after_failed_step(tmp_path: Path) -> None:
    service, com_ports = reactor_service(tmp_path, "unexpected_breakpoint")
    test_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: test_done, timeout_s: 5}
  - {debugger: dut, action: dump_memory, symbol: CTC_array, output_path: build/should-not-exist.hex}
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(test_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["failed_step"] == 3
    assert result["error_type"] == "unexpected_breakpoint"
    assert sorted(item["action"] for item in result["cleanup"]) == ["debug_stop", "uart_close"]
    assert com_ports.active == set()
    assert not (tmp_path / "build" / "should-not-exist.hex").exists()


def test_test_config_accepts_json_and_rejects_unknown_actions(tmp_path: Path) -> None:
    json_path = tmp_path / "testconfig.json"
    json_path.write_text(
        json.dumps({"version": 2, "steps": [{"port_id": "dut_uart", "action": "uart_open"}]}),
        encoding="utf-8",
    )
    assert load_test_config(str(json_path), str(tmp_path)).steps[0].action == "uart_open"

    invalid_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: shell}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(invalid_path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"
    assert excinfo.value.details["field"] == "steps[0].action"
    assert excinfo.value.details["value"] == "shell"


def test_load_test_config_records_the_digest_of_the_bytes_it_read(tmp_path: Path) -> None:
    """Review round 0, finding 4: the plan's provenance is captured at load, from
    the exact bytes parsed, so evidence never has to re-hash a file that may have
    changed since. The digest is the SHA-256 of the file on disk, and it is the
    one the run report carries."""
    import hashlib

    from agentic_hil.test_reactor import TEST_CONFIG_SHA256_KEY

    text = "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n"
    path = write_test_config(tmp_path, text)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    loaded = load_test_config(str(path), str(tmp_path))
    assert loaded.sha256 == expected

    service, _com_ports = reactor_service(tmp_path)
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(path), str(tmp_path)))
    finally:
        service.close()
    # The report carries the load-time digest, and it is the file's own bytes.
    assert result[TEST_CONFIG_SHA256_KEY] == expected


def test_test_config_rejects_duplicate_keys_with_location(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - port_id: dut_uart\n    action: uart_open\n    action: uart_close\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"
    assert excinfo.value.details["line"] == 5
    assert "duplicate key 'action'" in excinfo.value.details["backend_error"]


def test_test_config_schema_error_does_not_echo_structured_input(tmp_path: Path) -> None:
    secret = "operator-secret-must-not-leak"
    path = write_test_config(
        tmp_path,
        f"version: 2\nname:\n  secret: {secret}\nsteps:\n  - {{port_id: dut_uart, action: uart_open}}\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))

    assert excinfo.value.error_type == "test_config_invalid"
    assert secret not in json.dumps(excinfo.value.details)
    assert "value" not in excinfo.value.details


def test_test_plan_must_remain_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = tmp_path / "outside.yaml"
    plan.write_text("version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_test_config(str(plan), str(workspace))

    assert rejected.value.error_type == "test_config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())


def test_reactor_schema_rejects_traversal_breakpoint_file(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: run_until_breakpoint, location: {file: ../evil.c, line: 10}}\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"


def test_reactor_schema_accepts_windows_drive_breakpoint_file(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: run_until_breakpoint, location: {file: 'C:/src/main.c', line: 10}}\n",
    )
    loaded = load_test_config(str(path), str(tmp_path))
    assert loaded.steps[0].arguments["location"] == {"file": "C:/src/main.c", "line": 10}


def test_preflight_contract_rejects_bad_step_before_any_backend_call(tmp_path: Path) -> None:
    from agentic_hil.test_reactor import TestConfig, TestStep

    config = load_config(str(write_config(tmp_path, allow_all_symbols=True)))
    service = RecordingService()
    # Bypass the schema loader to prove the preflight contract validators are an
    # independent gate: a traversal breakpoint file must fail before debug_start.
    plan = TestConfig(
        path=str(tmp_path / "plan.yaml"),
        name="direct",
        steps=[
            TestStep("debug_start", {"image_path": "build/app.elf"}, debugger="dut"),
            TestStep("run_until_breakpoint", {"location": {"file": "../evil.c", "line": 10}}, debugger="dut"),
        ],
    )

    result = TestReactor(config, service).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 2
    assert service.calls == []


def test_preflight_rejects_late_unknown_debugger_before_flash(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {debugger: typo, action: flash, image_path: build/app.elf}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 2
    assert result["steps"] == []
    assert service.calls == []


def test_a_step_naming_a_port_this_bench_declares_none_of_says_what_to_do(tmp_path: Path) -> None:
    """The refusal a first plan run lands on, with somewhere to go (#431).

    `Test step references a COM port that is not in the authoritative config` was
    the whole of it: true, and no help. Two entirely different things produce it,
    and a reader who has got as far as running a plan cannot tell them apart from
    that sentence. Here the project declares no `com_ports` at all, so there is
    no name to correct the step to and the configuration is what has to be
    written; the refusal says so and names the command that writes it.
    """
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    validation = result["validation_error"]
    assert validation["configured_com_ports"] == []
    next_step = validation["next_step"]
    assert "`com_ports`" in next_step
    # Emptiness is not read as proof the configuration is what is wrong: the more
    # common reading, that the plan is for another bench, is offered first and
    # regeneration only where the project profile is meant to carry the kind.
    assert "another bench" in next_step
    assert "agentic-hil init --force" in next_step
    # And it says what that command costs, because it replaces the file: a reset,
    # not a repair.
    assert "narrowed permission" in next_step
    assert "reset rather than a repair" in next_step
    assert service.calls == []


def test_a_step_naming_a_port_this_bench_does_not_have_points_at_the_ones_it_does(tmp_path: Path) -> None:
    """The other half of the same refusal: the plan is what gets corrected.

    This project declares a port, and the step names another one. The bench is
    not missing anything, so telling the reader to regenerate the configuration
    would be the wrong instruction; the next step names the declared entries the
    step can be corrected to, and the command that fills a declared entry's
    hardware in.
    """
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  console:\n    device: "COM7"\n    baudrate: 115200\n')))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    validation = result["validation_error"]
    assert validation["configured_com_ports"] == ["console"]
    next_step = validation["next_step"]
    assert "console" in next_step
    assert "`com_ports.dut_uart`" in next_step
    # The advertised fill command names the entry it fills and parses on the real
    # CLI. A bare `adopt-hardware` could not pick a freshly declared COM entry
    # sitting beside `console`, and `--apply` is not a flag at all (round 1,
    # finding 2), so the emitted argv is exercised rather than substring-matched.
    parsed = build_parser().parse_args(adopt_argv(next_step))
    assert parsed.command == "adopt-hardware"
    assert parsed.com_port == "dut_uart"
    assert "--apply" not in next_step
    # The empty-section instruction is the wrong one here and must not appear.
    assert "init --force" not in next_step
    assert service.calls == []


def test_a_step_naming_a_debugger_this_bench_does_not_have_answers_the_same_way(tmp_path: Path) -> None:
    """The rule is the device kind's, not the COM port's.

    Every kind routes through one `name_refusal`, so a debugger name nobody
    configured has to arrive with the same next step, spelled for `debuggers`.
    """
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: typo, action: flash, image_path: build/app.elf}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    next_step = result["validation_error"]["next_step"]
    assert "`debuggers`" in next_step
    assert "`debuggers.typo`" in next_step
    assert "dut" in next_step
    # The debugger fill command names the entry with `--debugger` and parses on
    # the real CLI: with `dut` already declared, a bare `adopt-hardware` refuses
    # an unnamed debugger (round 1, finding 2).
    parsed = build_parser().parse_args(adopt_argv(next_step))
    assert parsed.command == "adopt-hardware"
    assert parsed.debugger == "typo"
    assert "--apply" not in next_step
    assert service.calls == []


def test_the_declared_debugger_fill_command_selects_the_new_entry_on_a_multi_entry_bench() -> None:
    """The emitted debugger command performs the fill it advertises (round 1, finding 2).

    `entry_fill_step` runs in the branch where the bench already declares entries
    and the operator has just declared the missing one beside them. Adoption then
    faces several, so its selection refuses an unnamed debugger. This parses the
    argv the debugger kind emits and runs adoption's own selection on the
    multi-entry document, to prove the named flag lands on the freshly declared
    entry where the bare command could not. The COM kind's own selection, which
    also runs through the debugger step first, is covered by the three
    `_com_adopt_instruction` tests below.
    """
    from agentic_hil.adopt import _choose_debugger

    config = SimpleNamespace(debuggers={"dut": {"type": "openocd"}, "newprobe": {"type": "openocd"}})
    # A debugger that already had `dut`, with `newprobe` just declared beside it.
    debugger_doc = {"debuggers": {"dut": {"type": "openocd"}, "newprobe": {"type": "openocd"}}}
    debugger_argv = adopt_argv(DebuggerRunner.entry_fill_step(config, "newprobe"))
    debugger_parsed = build_parser().parse_args(debugger_argv)
    assert debugger_parsed.debugger == "newprobe"
    # The flag lands adoption on exactly the declared entry...
    assert _choose_debugger(debugger_doc, debugger_parsed.debugger) == ("newprobe", debugger_doc["debuggers"]["newprobe"])
    # ...where the bare command (no --debugger) is refused as ambiguous.
    bare_debugger = _choose_debugger(debugger_doc, None)
    assert isinstance(bare_debugger, dict) and bare_debugger["ok"] is False


def test_com_fill_command_names_the_only_debugger_so_adoption_selects_it() -> None:
    """A COM fill runs through adoption's debugger-first selection (round 2, finding 2).

    `_adopt` calls `_choose_debugger` before `_choose_com_port`, so the COM-only
    fill the guidance advertises has to survive the debugger step. With one
    debugger configured the command names it -- so a second one added later does
    not silently start refusing it -- and this parses the emitted argv and runs
    adoption's two selections in order, proving the command lands on both the
    debugger and the freshly declared port.
    """
    from agentic_hil.adopt import _choose_com_port, _choose_debugger

    config = SimpleNamespace(debuggers={"dut": {"type": "openocd"}})
    document = {"debuggers": {"dut": {"type": "openocd"}}, "com_ports": {"console": {"device": "COM7"}, "new_uart": {}}}

    argv = adopt_argv(UartRunner.entry_fill_step(config, "new_uart"))
    parsed = build_parser().parse_args(argv)
    assert parsed.command == "adopt-hardware"
    assert parsed.debugger == "dut"
    assert parsed.com_port == "new_uart"
    assert "--apply" not in " ".join(argv)
    # Adoption's real order: the debugger first, then the port it can now reach.
    assert _choose_debugger(document, parsed.debugger) == ("dut", document["debuggers"]["dut"])
    assert _choose_com_port(document, parsed.com_port, ()) == ("new_uart", document["com_ports"]["new_uart"])


def test_com_fill_on_a_debugger_free_bench_does_not_promise_adoption() -> None:
    """A UART-only bench has no debugger, so adoption cannot run there (round 2, finding 2).

    `_choose_debugger` refuses a configuration with no debugger before any port is
    reached, so the previous `adopt-hardware --com-port <name>` sent the reader to
    a command that could never fill the port. The guidance names the manual path
    instead and advertises no COM adoption command.
    """
    from agentic_hil.adopt import _choose_debugger

    config = SimpleNamespace(debuggers={})
    document = {"debuggers": {}, "com_ports": {"new_uart": {}}}

    guidance = UartRunner.entry_fill_step(config, "new_uart")
    # Adoption really cannot select an entry here...
    refusal = _choose_debugger(document, None)
    assert isinstance(refusal, dict) and refusal["ok"] is False
    # ...so no `adopt-hardware ... --com-port` command is advertised...
    assert re.search(r"`agentic-hil adopt-hardware[^`]*--com-port", guidance) is None
    # ...and the reader is told to fill the device by hand.
    assert "com_ports.new_uart.device" in guidance
    assert "agentic-hil com-ports" in guidance


def test_the_debugger_free_com_repair_names_a_version_3_bench_s_identity(tmp_path: Path) -> None:
    """The only repair a UART-only version-3 bench has must load (round 0, finding 5).

    Generated configurations are version 3, where a `com_ports` entry with a bare
    `device` -- a `COM7` or a `/dev/ttyACM0` with no `serial_number`,
    `resource_id`, `/dev/serial/by-id/...` name or `identity_source` -- is refused
    on the next load. On a debugger-free bench adoption cannot run, so the manual
    path is the whole repair, and telling the operator to set only `.device` there
    left the configuration invalid, on Windows in particular. The guidance now
    names the identity a bind needs, and following it produces a file that loads.
    """
    config_path = write_config(
        tmp_path / "bench",
        config_version=3,
        com_ports_yaml="com_ports:\n  dut_uart:\n    baudrate: 115200\n",
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    config = load_config(str(config_path))
    assert config.config_version == 3
    assert config.debuggers == {}

    guidance = UartRunner.entry_fill_step(config, "dut_uart")
    # No debugger, so adoption cannot run and no COM adoption command is advertised.
    assert re.search(r"`agentic-hil adopt-hardware[^`]*--com-port", guidance) is None
    # The repair names the device AND the identity a bare version-3 device lacks,
    # not only `.device` as it did before (round 0, finding 5).
    assert "com_ports.dut_uart.device" in guidance
    assert "agentic-hil com-ports" in guidance
    assert "serial_number" in guidance
    assert "identity_source" in guidance

    # The repair, followed: a version-3 bound device with no identity is refused,
    # and the same device carrying the serial the guidance names loads. Following
    # the old "set only `.device`" advice is the first of these.
    bare = write_config(
        tmp_path / "bare",
        config_version=3,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM7"\n    baudrate: 115200\n',
    )
    with pytest.raises(ConfigError) as refused:
        load_config(str(bare))
    assert "identified" in str(refused.value)
    identified = write_config(
        tmp_path / "identified",
        config_version=3,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM7"\n    serial_number: "066AFF303435"\n    baudrate: 115200\n',
    )
    assert load_config(str(identified)).com_ports["dut_uart"].device == "COM7"


def test_the_debugger_free_com_repair_s_vid_pid_route_also_loads(tmp_path: Path) -> None:
    """The vid/pid alternative the repair offers must load too (round 2, finding 2).

    An adapter that publishes USB ids but no serial number is the second half of
    the version-3 repair, and version 3 refuses a `device` carrying `vid`/`pid`
    and nothing else exactly as it refuses a bare one: USB ids name a kind of
    adapter, not a unit, so the entry must also declare `identity_source: vid_pid`.
    The earlier guidance named only the `vid` and `pid`, so an operator with a
    serial-less adapter followed the repair into a file that fails its next load.
    The guidance now names that `identity_source`, and following it produces a
    file that loads (round 1, finding 2).
    """
    config_path = write_config(
        tmp_path / "bench",
        config_version=3,
        com_ports_yaml="com_ports:\n  dut_uart:\n    baudrate: 115200\n",
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    config = load_config(str(config_path))
    assert config.debuggers == {}

    guidance = UartRunner.entry_fill_step(config, "dut_uart")
    # The vid/pid alternative names the `identity_source` a bare vid/pid lacks, not
    # only the ids as it did before (round 1, finding 2).
    assert "vid" in guidance and "pid" in guidance
    assert "identity_source: vid_pid" in guidance

    # The repair followed literally on the vid/pid route: `device` + `vid` + `pid`
    # with no `identity_source` is refused, exactly as a bare device is, because
    # USB ids name a kind of adapter rather than a unit...
    bare_ids = write_config(
        tmp_path / "bare-ids",
        config_version=3,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "/dev/ttyUSB0"\n    vid: 6790\n    pid: 29987\n    baudrate: 115200\n',
    )
    with pytest.raises(ConfigError) as refused:
        load_config(str(bare_ids))
    assert refused.value.to_dict()["actual_identity_source"] == "vid_pid"
    # ...and the same entry carrying the `identity_source` the guidance now names
    # loads, which the old advice never produced.
    identified = write_config(
        tmp_path / "typed",
        config_version=3,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "/dev/ttyUSB0"\n    vid: 6790\n    pid: 29987\n    identity_source: "vid_pid"\n    baudrate: 115200\n',
    )
    loaded = load_config(str(identified))
    assert loaded.com_ports["dut_uart"].device == "/dev/ttyUSB0"
    assert loaded.com_ports["dut_uart"].identity_source == "vid_pid"


def test_com_fill_on_a_multi_debugger_bench_asks_which_debugger() -> None:
    """Several debuggers, and none can be picked for the operator (round 2, finding 2).

    Adoption would refuse an unnamed debugger before it reached the port, so the
    guidance names every debugger and asks for `--debugger` rather than
    advertising the bare command that refuses. Once the operator names one, the
    two selections run in order and land on the debugger and then the port.
    """
    from agentic_hil.adopt import _choose_com_port, _choose_debugger

    config = SimpleNamespace(debuggers={"dut": {"type": "openocd"}, "spare": {"type": "openocd"}})
    document = {"debuggers": {"dut": {"type": "openocd"}, "spare": {"type": "openocd"}}, "com_ports": {"new_uart": {}}}

    guidance = UartRunner.entry_fill_step(config, "new_uart")
    # Both debuggers are named and the operator is asked to choose one.
    assert "dut" in guidance and "spare" in guidance
    assert "--debugger" in guidance
    assert "--com-port new_uart" in guidance
    # The bare command (no --debugger) is exactly what adoption refuses here, which
    # is why the guidance cannot advertise it.
    bare = _choose_debugger(document, None)
    assert isinstance(bare, dict) and bare["ok"] is False
    # Once the operator names one, the two selections run in order and land.
    assert _choose_debugger(document, "spare") == ("spare", document["debuggers"]["spare"])
    assert _choose_com_port(document, "new_uart", ()) == ("new_uart", document["com_ports"]["new_uart"])


def test_unbound_com_refusal_names_the_debugger_dependency_end_to_end(tmp_path: Path) -> None:
    """The cited path (`UartRunner.unbound_refusal`) emits the same debugger-aware
    guidance a real plan reaches (round 2, finding 2).

    A plan opening a declared-but-unbound port on a single-debugger bench is
    refused `com_port_not_bound`, and the `next_step` now carries the `--debugger`
    the COM-only fill needs, parseable on the real CLI and selecting through
    adoption's own order.
    """
    from agentic_hil.adopt import _choose_com_port, _choose_debugger

    config_path = write_config(tmp_path, com_ports_yaml="com_ports:\n  dut_uart:\n    device: null\n")
    config = load_config(str(config_path))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {action: uart_open, port_id: dut_uart}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["error_type"] == "com_port_not_bound"
    next_step = result["validation_error"]["next_step"]
    argv = adopt_argv(next_step)
    parsed = build_parser().parse_args(argv)
    assert parsed.debugger == "dut"
    assert parsed.com_port == "dut_uart"
    document = {"debuggers": {"dut": {"type": "openocd"}}, "com_ports": {"dut_uart": {"device": ""}}}
    assert _choose_debugger(document, parsed.debugger) == ("dut", document["debuggers"]["dut"])
    assert _choose_com_port(document, parsed.com_port, ()) == ("dut_uart", document["com_ports"]["dut_uart"])
    assert service.calls == []


def test_troubleshooting_has_a_section_for_the_refusal_a_first_plan_hits() -> None:
    """The page a refused reader is sent to had nothing under this error type.

    `test_config_invalid` is where every plan that does not run lands, and
    TROUBLESHOOTING.md went from `com_port_not_bound` to CAN buses without ever
    naming it. The section has to separate the two documents a plan refusal can
    be about and carry the same commands the refusal itself names, or the page
    and the result are two different answers to one question.
    """
    page = (Path(__file__).resolve().parents[1] / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    heading = next((line for line in page.splitlines() if line.startswith("## ") and "test_config_invalid" in line), None)

    assert heading is not None, "TROUBLESHOOTING.md has no section for test_config_invalid"
    section = page.split(heading, 1)[1].split("\n## ", 1)[0]
    # The three routes out, the same three the refusal's own `next_step` names.
    assert "agentic-hil init --force" in section
    assert "agentic-hil adopt-hardware" in section
    # The CLI has no --apply flag; adoption applies by default. The section must
    # not send a reader to a command that exits 2 at argument parsing.
    assert "adopt-hardware --apply" not in section
    assert "configured_com_ports" in section
    # And the field a reader is told to read first, which is where the concrete
    # answer for their case is.
    assert "`next_step`" in section
    assert "`validation_error`" in section


def test_a_refused_plan_carries_the_error_catalogues_own_fix(tmp_path: Path) -> None:
    """The refusal a plan author reads is the one surface that carried nothing.

    Every other refusing path in this package merges `remediation_fields`, so the
    result says what the MCP error reference says. A plan refused at preflight
    did not, and a plan is exactly where a reader has the least idea what to try
    next: the catalogue's steps and its `do_not` now travel on the result.
    """
    from agentic_hil.knowledge import remediation_fields

    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    catalogue = remediation_fields("test_config_invalid")
    assert catalogue["remediation"], "the catalogue has to carry an entry for this error type"
    assert result["remediation"] == catalogue["remediation"]
    assert result["do_not"] == catalogue["do_not"]


def test_preflight_rejects_closing_a_uart_the_plan_never_opened(tmp_path: Path) -> None:
    # A plan may only close the session it opened, so a close on a second
    # configured port fails before any step runs.
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n  other_uart:\n    device: "COM_OTHER"\n',
            )
        )
    )
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n  - {port_id: other_uart, action: uart_close}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["steps"] == []
    assert service.calls == []


def test_preflight_does_not_create_dump_output_directories(tmp_path: Path) -> None:
    service, _ = reactor_service(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: dump_memory, symbol: CTC_array, output_path: build/new/nested/memory.hex}
  - {debugger: missing, action: flash, image_path: build/app.elf}
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(plan_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["steps"] == []
    assert not (tmp_path / "build" / "new").exists()


def test_preflight_enforces_symbol_allowlist_before_hardware_actions(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                allowed_symbols=["capture_buffer"],
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: run_until_breakpoint, location: test_done}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[2].location"
    assert service.calls == []


def test_reactor_cleanup_continues_after_debug_stop_exception(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
""",
    )
    service = RecordingService(fail_cleanup=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "cleanup_failed"
    assert result["step_error_type"] == "memory_read_failed"
    assert result["cleanup_ok"] is False
    assert [item["action"] for item in result["cleanup"]] == ["debug_stop", "uart_close"]
    assert ("com_session_stop", {"port_id": "dut_uart"}) in service.calls


def test_reactor_converts_step_exception_to_structured_failure(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path)),
    )
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n")
    service = RecordingService(raise_flash=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "step_exception"
    assert result["steps"][0]["result"]["exception_type"] == "OSError"


def test_reactor_treats_audit_failure_as_failed_step(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')),
    )
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n  - {port_id: dut_uart, action: uart_open}\n",
    )
    service = RecordingService(audit_flash_failure=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert result["error_type"] == "audit_failed"
    assert result["audit_ok"] is False
    assert result["retry_safe"] is False
    assert service.calls == [("flash_firmware", {"image_path": "build/app.elf"})]


@pytest.mark.parametrize(
    ("failed_call", "expected_calls"),
    [
        ("debug_set_breakpoint", ["debug_start_session", "debug_set_breakpoint", "debug_clear_breakpoints", "debug_stop_session"]),
        ("debug_continue", ["debug_start_session", "debug_set_breakpoint", "debug_continue", "debug_clear_breakpoints", "debug_stop_session"]),
        ("debug_clear_breakpoints", ["debug_start_session", "debug_set_breakpoint", "debug_continue", "debug_clear_breakpoints", "debug_stop_session"]),
    ],
)
def test_run_until_breakpoint_propagates_internal_audit_failures(tmp_path: Path, failed_call: str, expected_calls: list[str]) -> None:
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: debug_start, image_path: build/app.elf}\n  - {debugger: dut, action: run_until_breakpoint, location: test_done}\n",
    )
    service = RecordingService(audit_failure_call=failed_call)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["audit_ok"] is False
    assert result["retry_safe"] is False
    assert [name for name, _ in service.calls] == expected_calls


def test_run_until_breakpoint_removes_each_owned_breakpoint(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path)),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: run_until_breakpoint, location: first_stop}
  - {debugger: dut, action: run_until_breakpoint, location: second_stop}
  - {debugger: dut, action: debug_stop}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [name for name, _ in service.calls].count("debug_clear_breakpoints") == 2


def test_plan_rejects_a_com_port_the_config_does_not_declare(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: missing_uart, action: uart_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].port_id"
    assert result["validation_error"]["configured_com_ports"] == ["dut_uart"]
    assert service.calls == []


def test_single_configured_debugger_is_bound_and_needs_no_name(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))

    assert config.debugger_id == "dut"
    assert config.debugger is config.debuggers["dut"]


def test_several_configured_debuggers_leave_the_config_unbound(tmp_path: Path) -> None:
    # With more than one probe there is no board a bare call could mean, so
    # nothing is bound and the plan must name the probe itself.
    config = load_config(
        str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))
    )

    assert config.debugger_id is None
    assert config.debugger is None
    assert sorted(config.debuggers) == ["dut", "probe_b"]


def test_plan_must_name_the_debugger_when_several_are_configured(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))
    )
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {action: flash, image_path: build/app.elf}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].debugger"
    assert result["validation_error"]["configured_debuggers"] == ["dut", "probe_b"]
    assert service.calls == []


def test_debugger_target_override_inherits_unspecified_project_fields(tmp_path: Path) -> None:
    # A per-debugger `target` overrides only the fields it names; the rest fall
    # back to the project target so a board cannot silently lose the controller.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml=(
                    "debuggers:\n  probe_t:\n    type: openocd\n    resource_id: rt\n"
                    '    target:\n      name: "renamed-controller"\n'
                ),
            )
        )
    )

    assert config.debuggers["probe_t"].target is not None
    assert config.debuggers["probe_t"].target.name == "renamed-controller"
    assert config.debuggers["probe_t"].target.controller == config.target.controller


def test_cli_parses_test_reactor_command() -> None:
    args = build_parser().parse_args(["test-reactor", "--test-config", "test.json"])
    assert args.command == "test-reactor"
    assert args.test_config == "test.json"


def test_cli_uses_authoritative_config_and_repository_local_test_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    test_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: shell}\n")
    monkeypatch.chdir(tmp_path)

    exit_code = entrypoint(["test-reactor", "--test-config", str(test_path), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == "test_config_invalid"


def test_cli_returns_failure_for_audit_failed_result(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("agentic_hil.cli.run_test_reactor", lambda _path, **_kwargs: {"ok": True, "audit_ok": False})

    exit_code = entrypoint(["test-reactor", "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["audit_ok"] is False


def test_check_plan_accepts_a_loadable_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """The hosted simulator's happy path: a plan the reactor can load passes,
    with no configuration and no hardware."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "nominal.testconfig.yaml"
    plan.write_text("version: 3\nname: nominal\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")

    exit_code = entrypoint(["check-plan", str(plan), "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["ok"] is True
    assert result["plans"][0]["name"] == "nominal"
    assert result["plans"][0]["steps"] == 1


def test_check_plan_refuses_what_a_schema_only_reader_would_pass(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """The two failures the schema-only reader the examples used to run would miss.

    `yaml.safe_load` silently collapses duplicate keys the reactor's
    `UniqueKeyLoader` refuses, and `jsonschema` ignores the `x-since-version`
    gates, so a plan using a key from a later plan version passes it and is then
    refused on the bench. `check-plan` loads through `load_test_config`, so both
    are caught here, one run names every red plan rather than the first, and the
    command exits nonzero.
    """
    monkeypatch.chdir(tmp_path)
    good = tmp_path / "good.testconfig.yaml"
    good.write_text("version: 3\nname: good\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")
    duplicate_key = tmp_path / "dupe.testconfig.yaml"
    duplicate_key.write_text("version: 3\nname: one\nname: two\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")
    too_new_for_its_version = tmp_path / "too_new.testconfig.yaml"
    too_new_for_its_version.write_text("version: 2\nname: too-new\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")

    exit_code = entrypoint(["check-plan", str(good), str(duplicate_key), str(too_new_for_its_version), "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    verdicts = {Path(entry["plan"]).name: entry for entry in result["plans"]}
    assert verdicts["good.testconfig.yaml"]["ok"] is True
    assert verdicts["dupe.testconfig.yaml"]["ok"] is False
    assert verdicts["too_new.testconfig.yaml"]["ok"] is False
    # The feature gate jsonschema ignores is what catches the version mismatch.
    assert verdicts["too_new.testconfig.yaml"]["error_type"] == "test_config_invalid"


def test_check_plan_renders_a_refusal_for_a_person(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Without `--json` the operator reading the CI log gets prose, and it names
    which plan would be refused rather than a machine document."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "v1.testconfig.yaml"
    plan.write_text("version: 1\nname: old\nsteps:\n  - {debugger: dut, action: reset}\n", encoding="utf-8")

    exit_code = entrypoint(["check-plan", str(plan)])

    assert exit_code == 1
    printed = capsys.readouterr().out
    assert "v1.testconfig.yaml" in printed


def test_a_run_names_the_per_run_report_copy_the_next_run_will_not_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three runs must leave three reports, and the run must say where its own is.

    `last-report.json` is the newest run and nothing else, so a bench collecting
    the evidence of three runs kept one of them and found out when the third had
    already overwritten the first. The per-run copy under the state root is what
    the earlier ones survive in, and it is only useful if the run names it: both
    in the document, for whatever parses it, and in the one line the run prints,
    for whoever is still standing at the terminal."""
    import hashlib

    from agentic_hil.cli import run_test_reactor

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Its own copy of the fake backend, because the device lock is machine-wide
    # and keyed on the hardware: a shared executable path would make this test
    # declare the same board as its neighbours.
    probe = tmp_path / "fake_openocd.py"
    probe.write_bytes(FAKE_OPENOCD.read_bytes())
    write_authoritative_config(workspace, monkeypatch, debugger_executable=probe)
    monkeypatch.chdir(workspace)
    plan = write_test_config(workspace, "version: 3\nname: keeps-evidence\nsteps:\n  - {device: dut, action: delay, duration_ms: 5}\n")

    first = run_test_reactor(str(plan))

    assert first["ok"] is True, first
    workspace_report = workspace / ".agentic-hil" / "reports" / "last-report.json"
    canonical = Path(first["canonical_report_path"])
    assert first["summary"] == f"Test reactor sequence completed. This run's own report is kept at {canonical}; later runs do not overwrite it."
    # The mirror in the workspace carries the same field, so a reader who finds
    # only that file still learns where the earlier runs went.
    assert json.loads(workspace_report.read_text(encoding="utf-8"))["canonical_report_path"] == str(canonical)
    # It is a real file, outside the workspace, and the same document byte for
    # byte as the mirror rather than a differently shaped summary of it.
    assert canonical.is_file()
    assert workspace.resolve() not in canonical.resolve().parents
    assert hashlib.sha256(canonical.read_bytes()).hexdigest() == hashlib.sha256(workspace_report.read_bytes()).hexdigest()

    second = run_test_reactor(str(plan))

    assert second["run"] != first["run"]
    assert Path(second["canonical_report_path"]) != canonical
    # The workspace file is the second run's now; the first run's own copy is
    # still the first run's, which is the whole of what this buys.
    assert json.loads(workspace_report.read_text(encoding="utf-8"))["run"] == second["run"]
    assert json.loads(canonical.read_text(encoding="utf-8"))["run"] == first["run"]

    exit_code = entrypoint(["test-reactor", "--test-config", str(plan)])

    # Printed, not merely returned: the summary is the line an operator reads.
    printed = " ".join(capsys.readouterr().out.split())
    assert exit_code == 0
    assert f"This run's own report is kept at {canonical.parent}" in printed
    assert "later runs do not overwrite it." in printed


def test_reactor_builds_one_service_per_named_debugger(tmp_path: Path) -> None:
    # Multi-board: the bound probe shares the base service; every other named
    # probe drives its own service built for that debugger, sharing the base
    # coordinator for project-level exclusivity.
    from agentic_hil.config import bind_debugger
    from agentic_hil.test_reactor import TestReactor

    config = bind_debugger(
        load_config(str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))),
        "dut",
    )

    class ClosableRecordingService(RecordingService):
        def close(self) -> None:
            self.closed = True

    base = ClosableRecordingService()
    built: list[ClosableRecordingService] = []

    def factory(bound_config) -> ClosableRecordingService:
        svc = ClosableRecordingService()
        svc.built_for = bound_config.debugger
        built.append(svc)
        return svc

    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        assert reactor.runner("dut").service is base
        assert reactor.runner("dut").debugger is config.debuggers["dut"]
        assert reactor.runner("probe_b").service is not base
        assert reactor.runner("probe_b").debugger.resource_id == "rb"
        assert len(built) == 1
        assert built[0].built_for.resource_id == "rb"
        assert built[0].built_for.type == "openocd"
    finally:
        reactor.close()
    assert built[0].closed is True


def test_a_failed_multi_debugger_run_recovers_through_the_touched_probes(tmp_path: Path) -> None:
    # The base service is unbound in the multi-probe CLI (`config.debugger` is
    # None), and debugger steps run on per-probe services. Recovery has to follow
    # them: recovering through the unbound base withholds reset as
    # `allow_reset_missing` while the probes the plan drove are left as the failed
    # run put them, and it must never reach for a probe the run never named.
    from agentic_hil.test_reactor import TestConfig, TestReactor, TestStep

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n  probe_c:\n    type: openocd\n    resource_id: rc\n",
            )
        )
    )
    assert config.debugger_id is None, "a config with several probes leaves the base service unbound"

    class ClosableRecordingService(RecordingService):
        def close(self) -> None:
            self.closed = True

    base = ClosableRecordingService()
    built: dict[str, ClosableRecordingService] = {}

    def factory(bound_config) -> ClosableRecordingService:
        svc = ClosableRecordingService(fail_call="reset_target" if bound_config.debugger_id == "probe_b" else None)
        built[bound_config.debugger_id] = svc
        return svc

    plan = TestConfig(
        path=str(tmp_path / "plan.yaml"),
        name="multi-probe-abort",
        steps=[
            TestStep("reset", {"mode": "run"}, debugger="dut"),
            TestStep("reset", {"mode": "run"}, debugger="probe_b"),
        ],
    )
    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        result = reactor.run(plan)
    finally:
        reactor.close()

    assert result["ok"] is False, result
    assert result["failed_step"] == 2

    # Each touched probe recovered through its own bound service; the base
    # service (bound to no probe) was never asked to.
    assert built["dut"].recovery_calls == [["dut"]]
    assert built["probe_b"].recovery_calls == [["probe_b"]]
    assert base.recovery_calls == [], "the unbound base service must not be the one recovered"
    # A probe the plan never named is neither built nor touched.
    assert "probe_c" not in built

    recovery = result["recovery"]
    assert recovery["outcome"] == "recovered", recovery
    assert recovery["devices"] == ["dut", "probe_b"]
    assert recovery["recovered_debuggers"] == ["dut", "probe_b"]
    assert set(recovery["recoveries"]) == {"dut", "probe_b"}


def test_a_probe_used_only_as_a_delay_route_is_not_recovered(tmp_path: Path) -> None:
    # `delay` is declared `touches_device=False`: it routes to a named probe and
    # is recorded as a step, but drives no hardware. A run that delays on one probe
    # and then fails a real reset on another must recover only the probe it drove:
    # resetting and halting a board no hardware action in the plan ever reached is
    # exactly what recovery must not do.
    from agentic_hil.test_reactor import TestConfig, TestReactor, TestStep

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n  probe_c:\n    type: openocd\n    resource_id: rc\n",
            )
        )
    )
    assert config.debugger_id is None

    class ClosableRecordingService(RecordingService):
        def close(self) -> None:
            self.closed = True

    base = ClosableRecordingService()
    built: dict[str, ClosableRecordingService] = {}

    def factory(bound_config) -> ClosableRecordingService:
        svc = ClosableRecordingService(fail_call="reset_target" if bound_config.debugger_id == "probe_b" else None)
        built[bound_config.debugger_id] = svc
        return svc

    plan = TestConfig(
        path=str(tmp_path / "plan.yaml"),
        name="delay-then-abort",
        steps=[
            TestStep("delay", {"duration_ms": 1}, debugger="dut"),
            TestStep("reset", {"mode": "run"}, debugger="probe_b"),
        ],
    )
    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        result = reactor.run(plan)
    finally:
        reactor.close()

    assert result["ok"] is False, result
    assert result["failed_step"] == 2

    # The delay step still built dut's device and per-probe service, so absence
    # from `devices` is not why it is spared: it is spared because it drove nothing.
    assert "dut" in built, "the delay step builds dut's device and service"
    assert built["dut"].recovery_calls == [], "a delay-only probe must not be recovered"
    assert built["probe_b"].recovery_calls == [["probe_b"]]
    assert base.recovery_calls == []
    assert "probe_c" not in built

    recovery = result["recovery"]
    assert recovery["outcome"] == "recovered", recovery
    # Exactly one probe was driven, so recovery took the single-probe path: there
    # is no per-probe aggregate that could name the delay-only dut.
    assert "recoveries" not in recovery


def test_config_rejects_named_debuggers_that_share_a_physical_probe(tmp_path: Path) -> None:
    # Two names, one probe serial: both entries would drive the same board while
    # a plan believes it addressed two.
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_name="probe_x",
                    probe_id="0669FF505153",
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n    probe_id: "0669FF505153"\n',
                )
            )
        )
    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["other_debugger"] == "probe_x"


def test_multi_probe_config_without_probe_ids_still_loads(tmp_path: Path) -> None:
    # Discovering the ids is exactly what an operator does before they can write
    # one down, and loading must work with no hardware attached, so the demand
    # for a probe_id cannot live at config load.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debugger_name="probe_x",
                auto_probe_ids=False,
                debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n',
            )
        )
    )

    assert sorted(config.debuggers) == ["probe_x", "probe_y"]
    assert config.debuggers["probe_y"].probe_id is None


def test_driving_an_unnamed_probe_is_refused_when_several_are_configured(tmp_path: Path) -> None:
    # The demand belongs at the hardware boundary, where picking the wrong board
    # is the failure that matters.
    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_name="probe_x",
                    auto_probe_ids=False,
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n',
                )
            )
        ),
        "probe_x",
    )
    service = AgenticHILToolService(config)
    try:
        driven = service.call("reset_target", {"mode": "run"})
        listed = service.call("debugger_probes_list")
    finally:
        service.close()

    assert driven["ok"] is False
    assert driven["error_type"] == "not_supported"
    assert driven["side_effect_committed"] is False
    assert "debugger-probes" in driven["summary"]
    # Discovery is the way out of that state, so the unnamed-probe guard must
    # not swallow it. (OpenOCD answers not_supported on its own: it cannot
    # enumerate, but the refusal is the backend's, not the guard's.)
    assert "configured_debuggers" not in listed
    assert listed["backend"] == "openocd"


def test_config_rejects_a_pyocd_probe_id_contained_in_another(tmp_path: Path) -> None:
    # pyOCD matches --uid as a case-insensitive substring, so the shorter
    # selector also selects the board the longer one names.
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_type="pyocd",
                    debugger_name="probe_x",
                    probe_id="0669FF505153",
                    auto_probe_ids=False,
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "0669FF"\n',
                )
            )
        )

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["other_debugger"] == "probe_x"


def test_config_rejects_a_pyocd_probe_id_that_only_differs_by_its_type_prefix(tmp_path: Path) -> None:
    # pyOCD strips an optional `<type>:` prefix before matching, so these two
    # spellings are one selector.
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_type="pyocd",
                    debugger_name="probe_x",
                    probe_id="0669FF505153",
                    auto_probe_ids=False,
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "stlink:0669ff505153"\n',
                )
            )
        )

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["other_debugger"] == "probe_x"


def test_single_debugger_needs_no_probe_id(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))

    assert config.debuggers["dut"].probe_id is None
    assert config.debugger_id == "dut"


def test_reactor_requires_factory_for_an_unbound_debugger(tmp_path: Path) -> None:
    from agentic_hil.config import bind_debugger
    from agentic_hil.test_reactor import TestReactor

    config = bind_debugger(
        load_config(str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))),
        "dut",
    )
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: probe_b, action: flash, image_path: build/app.elf}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].debugger"
    assert result["validation_error"]["bound_debugger"] == "dut"
    assert service.calls == []


def test_preflight_gates_debug_actions_on_the_named_debuggers_type(tmp_path: Path) -> None:
    # probe_b is pyocd while the other configured probe is openocd: typed debug
    # steps must be judged by the debugger the STEP names.
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: pyocd\n    resource_id: rb\n",
            )
        )
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: probe_b, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: probe_b, action: debug_stop}
""",
    )

    class ClosableService(RecordingService):
        def close(self) -> None:
            pass

    reactor = TestReactor(config, RecordingService(), service_factory=lambda bound_config: ClosableService())  # type: ignore[arg-type]
    try:
        result = reactor.run(load_test_config(str(plan_path), str(tmp_path)))
    finally:
        reactor.close()

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["validation_error"]["debugger_type"] == "pyocd"


def test_debug_steps_execute_on_the_named_debugger_service(tmp_path: Path) -> None:
    # Inverse direction: the bound probe is pyocd (no typed debug), but the probe
    # the step NAMES is openocd: preflight must pass and the steps must run on
    # that probe's own service, not the base service.
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debugger_type="pyocd",
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n",
            )
        )
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: probe_b, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: probe_b, action: debug_stop}
""",
    )

    class ClosableService(RecordingService):
        def close(self) -> None:
            pass

    base = RecordingService()
    built: list[ClosableService] = []

    def factory(bound_config) -> ClosableService:
        svc = ClosableService()
        built.append(svc)
        return svc

    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        result = reactor.run(load_test_config(str(plan_path), str(tmp_path)))
    finally:
        reactor.close()

    assert result["ok"] is True, result
    assert base.calls == []
    assert [name for name, _ in built[0].calls] == ["debug_start_session", "debug_stop_session"]


def test_a_registered_run_reports_reactor_construction_failure_and_closes_service(monkeypatch: pytest.MonkeyPatch) -> None:
    # If building the per-device services fails inside TestReactor.__init__, the
    # run must still write a structured report, close the base service, and only
    # then re-raise the constructor error.
    # Asked of `run_registered_plan` rather than of the entry points above it: a
    # run handle is real coordination state and this test's config double is one
    # field wide on purpose. What is pinned is unchanged, and it is still the
    # whole of what a frontend does with a plan once a handle exists.
    from types import SimpleNamespace

    from agentic_hil.reactorrun import run_registered_plan

    class FakeRegistration:
        """A run handle that records nothing. What a registration does has its
        own tests; here it only has to be there."""

        handle = "run-00000000000000ff"

        def running(self) -> None:
            return None

        def progress(self, progress: dict) -> None:
            return None

        def stop_requested(self) -> bool:
            return False

    class FakeService:
        def __init__(self, config, frontend: str) -> None:
            assert frontend == "reactor"
            self.config = config
            self.coordinator = object()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services: list[FakeService] = []

    def make_service(config, frontend: str = "", **kwargs) -> FakeService:
        svc = FakeService(config, frontend=frontend or kwargs.get("frontend", ""))
        services.append(svc)
        return svc

    written: list[dict] = []

    def fake_reactor(config, service, service_factory=None, **kwargs):
        raise RuntimeError("device service failed to build")

    monkeypatch.setattr("agentic_hil.reactorrun.AgenticHILToolService", lambda config, frontend: make_service(config, frontend=frontend))
    monkeypatch.setattr("agentic_hil.reactorrun.TestReactor", fake_reactor)
    monkeypatch.setattr("agentic_hil.reactorrun.write_report", lambda config, result: written.append(result) or result)

    plan = SimpleNamespace(name="plan", path="plan.yaml", steps=[])
    with pytest.raises(RuntimeError, match="device service failed to build"):
        run_registered_plan(SimpleNamespace(work_dir="."), plan, wait_s=0.0, registration=FakeRegistration())

    assert len(services) == 1
    assert services[0].closed is True
    assert written and written[0]["error_type"] == "reactor_exception"
    assert written[0]["ok"] is False
    assert written[0]["run"] == "run-00000000000000ff"


def test_reactor_close_releases_services_built_before_a_factory_failure(tmp_path: Path) -> None:
    # Runners are built on first use, so a later probe's service can fail after
    # an earlier one already exists. close() must still release what was built.
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n  probe_c:\n    type: openocd\n    resource_id: rc\n",
            )
        )
    )

    class ClosableService(RecordingService):
        closed = False

        def close(self) -> None:
            self.closed = True

    built: list[ClosableService] = []

    def factory(bound_config) -> ClosableService:
        if built:
            raise RuntimeError("second probe service failed to build")
        svc = ClosableService()
        built.append(svc)
        return svc

    reactor = TestReactor(config, RecordingService(), service_factory=factory)  # type: ignore[arg-type]
    reactor.runner("probe_b")
    with pytest.raises(RuntimeError, match="second probe service failed to build"):
        reactor.runner("probe_c")

    reactor.close()

    assert len(built) == 1
    assert built[0].closed is True


def test_reactor_close_aggregates_all_per_debugger_errors(tmp_path: Path) -> None:
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n  probe_c:\n    type: openocd\n    resource_id: rc\n",
            )
        )
    )

    class FailingCloseService(RecordingService):
        def __init__(self, message: str) -> None:
            super().__init__()
            self.message = message

        def close(self) -> None:
            raise OSError(self.message)

    names = iter(["close-b failed", "close-c failed"])

    def factory(bound_config) -> FailingCloseService:
        return FailingCloseService(next(names))

    reactor = TestReactor(config, RecordingService(), service_factory=factory)  # type: ignore[arg-type]
    reactor.runner("probe_b")
    reactor.runner("probe_c")
    with pytest.raises(RuntimeError) as excinfo:
        reactor.close()
    assert "close-b failed" in str(excinfo.value)
    assert "close-c failed" in str(excinfo.value)
    # close() drained the owned services; a second close must not re-raise.
    reactor.close()


def test_single_probe_target_override_is_applied_on_the_bound_config(tmp_path: Path) -> None:
    # The override is documented as moving from devices.<name>.target to
    # debuggers.<name>.target. Auto-binding the only configured probe has to
    # honour it, or it would work on multi-probe projects and be silently
    # dropped on the supported single-probe path.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml='debuggers:\n  dut:\n    target:\n      name: "sensor-node"\n',
            )
        )
    )

    assert config.debugger_id == "dut"
    assert config.target.name == "sensor-node"
    # Unnamed fields still fall back to the project target.
    assert config.target.controller == "stm32f4"


def test_bind_debugger_applies_the_probes_own_target(tmp_path: Path) -> None:
    from agentic_hil.config import bind_debugger

    config = load_config(
        str(
            write_config(
                tmp_path,
                debugger_name="board_a",
                probe_id="PROBE-A",
                debuggers_yaml=(
                    'debuggers:\n  board_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
                    f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
                    '    target:\n      name: "sensor-node"\n'
                ),
            )
        )
    )

    assert bind_debugger(config, "board_b").target.name == "sensor-node"
    assert bind_debugger(config, "board_a").target.name == config.target.name


def test_version_1_plan_is_refused_with_the_key_that_replaced_device(tmp_path: Path) -> None:
    # Version 1 steps addressed a `device:` the config model no longer has.
    # Without a version boundary every old plan would fail on a bare const
    # mismatch with nothing pointing at the replacement.
    plan_path = write_test_config(
        tmp_path,
        "version: 1\nsteps:\n  - {device: dut, action: flash, image_path: build/app.elf}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_test_config(str(plan_path), str(tmp_path))

    assert rejected.value.error_type == "test_config_invalid"
    assert rejected.value.details["field"] == "version"
    migration = rejected.value.details["migration"]
    assert any("debugger:" in value for value in migration.values())
    assert any("port_id:" in value for value in migration.values())


CAN_BUS_YAML = """can_buses:
  dut_can:
    adapter: "socketcan"
    channel: "can0"
    bitrate: 500000
"""


def can_config(tmp_path: Path, *, listen_only: bool = False, allow_write: bool = True, allow_read: bool = True):
    """A project with one configured CAN bus, and the flags that decide what a
    plan may do with it."""
    permissions = {**DEFAULT_TEST_PERMISSIONS, "allow_can_write": allow_write, "allow_can_read": allow_read}
    bus = CAN_BUS_YAML + ("    listen_only: true\n" if listen_only else "")
    return load_config(str(write_config(tmp_path, can_buses_yaml=bus, permissions=permissions)))


def test_reactor_runs_a_can_read_and_send_plan_end_to_end(tmp_path: Path) -> None:
    # A CAN bus is a device a plan drives like any other: it opens, it is read
    # from and written to, and it closes.
    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
name: bus-traffic
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_read, max_frames: 4, wait_timeout_s: 0.5}
  - {bus_id: dut_can, action: can_send, frame_id: "0x123", data_hex: "01ff"}
  - {bus_id: dut_can, action: can_close}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["can_open", "can_read", "can_send", "can_close"]
    assert [step["route"] for step in result["steps"]] == ["dut_can"] * 4
    # Every call names the entry the step routed to, supplied by the device
    # rather than taken from the plan's own arguments.
    assert service.calls == [
        ("can_session_start", {"clear_rx_queue": True, "bus_id": "dut_can"}),
        ("can_read", {"max_frames": 4, "wait_timeout_s": 0.5, "bus_id": "dut_can"}),
        ("can_send", {"frame_id": "0x123", "data_hex": "01ff", "bus_id": "dut_can"}),
        ("can_session_stop", {"bus_id": "dut_can"}),
    ]
    # The plan closed what it opened, so cleanup has nothing left to do.
    assert result["cleanup"] == []


def test_preflight_rejects_a_can_bus_the_config_does_not_declare(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {bus_id: missing_can, action: can_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    validation = result["validation_error"]
    assert validation["field"] == "steps[0].bus_id"
    assert validation["configured_can_buses"] == ["dut_can"]
    # Adoption has no CAN half, so a CAN refusal must not send the reader to
    # `adopt-hardware` for a fill it cannot perform; the adapter and channel are
    # written by hand instead (round 0, finding 1).
    next_step = validation["next_step"]
    assert "adopt-hardware" not in next_step
    assert "adapter and channel" in next_step
    assert service.calls == []


def test_preflight_refuses_a_can_send_on_a_bus_that_may_not_be_written(tmp_path: Path) -> None:
    # The permission the backend would raise, moved to where the plan can still
    # be corrected: nothing is opened for a send that was never going to happen.
    config = can_config(tmp_path, allow_write=False)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_send, frame_id: 291}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["validation_error"]["field"] == "steps[1].action"
    assert result["validation_error"]["permission"] == "allow_write"
    assert result["steps"] == []
    assert service.calls == []


def test_preflight_refuses_a_can_send_on_a_listen_only_bus(tmp_path: Path) -> None:
    # Writing is permitted here; the bus itself is what refuses. `listen_only`
    # is the claim that observing this bus sends nothing, so a plan that also
    # sends on it contradicts the bus it declared, and that is a fault in the
    # plan, findable before anything reaches the bench.
    config = can_config(tmp_path, listen_only=True, allow_write=True)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_send, frame_id: "0x7ff"}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    validation = result["validation_error"]
    assert validation["field"] == "steps[1].action"
    assert validation["listen_only"] is True
    assert validation["config_field"] == "can_buses.dut_can.listen_only"
    assert "listen_only" in validation["summary"]
    # The same name the tool's own gate answers with, so this is one refusal with
    # one catalogue entry rather than a plan-shaped rhyme of it. A reader who met
    # `can_listen_only_mode` at the bench meets it here too.
    assert validation["error_type"] == LISTEN_ONLY_MODE_ERROR
    assert validation["error_type"] != "permission_denied", "the mode is not a permission, on either route"
    assert service.calls == []


def test_a_can_send_step_is_refused_by_the_tool_even_if_preflight_is_bypassed(tmp_path: Path) -> None:
    """The plan route's second line of defence, and the reason it is a second one.

    Preflight is the better refusal (it names the plan's own contradiction
    before a session exists), but it is a check the reactor performs, and a check
    can be got round. The tool the step routes to answers the same way for a call
    that arrives at it anyway, so the guarantee does not rest on preflight having
    run.
    """
    from agentic_hil.can import CanBusService

    config = can_config(tmp_path, listen_only=True, allow_write=True)
    service = CanBusService(config)
    try:
        result = service.send("dut_can", {"frame_id": "0x7ff", "data_hex": "01"})
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_MODE_ERROR


def test_reactor_closes_an_open_can_session_after_a_failed_step(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_read}
  - {bus_id: dut_can, action: can_close}
""",
    )
    service = RecordingService(fail_call="can_read")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "can_read_failed"
    # The plan never reached its own can_close, so the run closes the session it
    # opened, exactly as it does for a UART.
    assert [item["action"] for item in result["cleanup"]] == ["can_close"]
    assert result["cleanup"][0]["bus_id"] == "dut_can"
    assert result["cleanup_ok"] is True
    assert [name for name, _ in service.calls] == ["can_session_start", "can_read", "can_session_stop"]


def test_preflight_refuses_can_traffic_before_the_bus_is_open(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {bus_id: dut_can, action: can_read}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].action"
    assert result["validation_error"]["summary"] == "CAN bus session must be opened before this action."
    assert service.calls == []


def test_a_can_session_may_only_be_closed_by_the_plan_that_opened_it(tmp_path: Path) -> None:
    # Preflight refuses this plan, so the ownership guard underneath it is
    # reached only by a step that got past preflight, which is exactly what it
    # is there for.
    from agentic_hil.test_reactor import TestStep

    config = can_config(tmp_path)
    service = RecordingService()

    result = TestReactor(config, service).execute_step(TestStep("can_close", {}, bus_id="dut_can"))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "can_session_not_owned"
    assert result["bus_id"] == "dut_can"
    assert service.calls == []


def test_a_plan_declares_and_locks_the_can_bus_it_drives(tmp_path: Path) -> None:
    # A device kind the reactor can drive is a device kind the run holds: the
    # bus is in what the mutex takes, or an observation from outside could reach
    # it between two of this plan's steps.
    from agentic_hil.devices import can_device
    from agentic_hil.test_reactor import declared_devices

    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_close}
""",
    )

    devices = declared_devices(config, load_test_config(str(plan_path), str(tmp_path)))

    assert can_device(config, "dut_can").lock_key in devices
    # One entry per physical unit, not one per step, where a unit is every key
    # its device holds, not one: an executable debugger carries its legacy
    # `probe:<path>` alias beside `probe-exe:` so old and new processes collide
    # during an upgrade window, and counting keys would break each time a
    # device gains an alias. The set equality is the real claim.
    from agentic_hil.devices import debugger_device

    expected = {can_device(config, "dut_can").lock_key}
    expected.update(debugger_device(config).lock_keys)
    assert set(devices) == expected


def test_every_device_kind_answers_for_its_own_actions() -> None:
    # The one authority: a step action exists because a method declared it, and
    # the schema map, the routing keys and the tools are all read back off those
    # declarations. A kind naming a tool its device does not own, or a schema
    # entry the bundled schema does not define, would be a second answer, and
    # so would an action nothing implements.
    from importlib import resources

    from agentic_hil.test_reactor import (
        ACTION_SCHEMAS,
        REACTOR_ACTION_SCHEMAS,
        ROUTE_FIELDS,
        STEP_ACTION_DECLARATIONS,
        STEP_DEVICE_CLASSES,
        step_device_classes,
    )

    schema = json.loads(resources.files("agentic_hil").joinpath("schemas/testconfig.schema.json").read_text(encoding="utf-8"))
    claimed: dict[str, object] = {}
    for device_class in STEP_DEVICE_CLASSES:
        assert device_class.route_field in ROUTE_FIELDS
        assert device_class.step_actions == {name: spec.schema for name, spec in device_class.step_action_specs.items()}
        for action, spec in device_class.step_action_specs.items():
            # An action claimed by two kinds is only ever one declaration two
            # kinds inherited: `delay`, declared once on the base. Two kinds
            # declaring the same name separately is the drift this forbids.
            assert claimed.get(action, spec) == spec, f"{action} is declared twice, by two different methods"
            claimed[action] = spec
            assert spec.schema in schema["$defs"], f"{action} names a schema $def that does not exist"
            assert device_class in step_device_classes(action)
            # The declaration is on the method that runs it, and nowhere else.
            method = getattr(device_class, spec.method)
            assert spec in getattr(method, STEP_ACTION_DECLARATIONS, ()), f"{action} names a method that does not declare it"
            if spec.tool is not None:
                assert spec.tool in device_class.device_class.tools, f"{spec.tool} is not a tool {device_class.device_class.__name__} owns"

    # Every action, less the ones the reactor serves itself. `repeat` routes to
    # no device and drives no hardware, so no kind can declare it and the old
    # equality would now demand a device class for a step about the sequence.
    # The two halves are still required to cover the whole vocabulary and to
    # stay disjoint, which is the property the equality was really pinning.
    assert set(claimed) == set(ACTION_SCHEMAS) - set(REACTOR_ACTION_SCHEMAS)
    assert not set(claimed) & set(REACTOR_ACTION_SCHEMAS)
    assert {"can_open", "can_close", "can_send", "can_read"} <= set(claimed)
    assert {"uart_write", "uart_read", "delay"} <= set(claimed)
    for action, schema_name in REACTOR_ACTION_SCHEMAS.items():
        # A reactor action answers for itself exactly as a declared one does: a
        # shape in the bundled schema, and no device kind claiming it.
        assert schema_name in schema["$defs"], f"{action} names a schema $def that does not exist"
        assert step_device_classes(action) == ()
    # Every step shape the schema offers is served, and every action has a
    # shape. A `$def` nobody dispatches is a step a plan can be written against
    # and nothing will run.
    offered = {str(branch["$ref"]).rsplit("/", 1)[-1] for branch in schema["$defs"]["steps"]["items"]["oneOf"]}
    assert offered == set(ACTION_SCHEMAS.values())


def test_the_schema_is_exported_without_building_a_single_device() -> None:
    # The property declaration-on-the-method exists to keep: the plan schema is
    # read off the classes, so `agentic-hil` can answer what a plan may contain
    # on a machine with no bench attached and no config loaded at all.
    from agentic_hil.test_reactor import ACTION_SCHEMAS, STEP_DEVICE_CLASSES

    for device_class in STEP_DEVICE_CLASSES:
        assert device_class.step_action_specs, f"{device_class.__name__} declares no actions"
        for spec in device_class.step_action_specs.values():
            # Reading the declaration touches the class, never an instance.
            assert ACTION_SCHEMAS[spec.name] == spec.schema


def test_delay_is_declared_once_and_served_by_every_device_kind() -> None:
    from agentic_hil.test_reactor import STEP_DEVICE_CLASSES, StepDevice, step_device_classes

    assert "delay" in StepDevice.step_action_specs
    # One declaration, three claimants: the kinds inherit it rather than repeat it.
    assert set(step_device_classes("delay")) == set(STEP_DEVICE_CLASSES)
    # The very same declaration object reaches all three, not three copies of it.
    assert all(device_class.step_action_specs["delay"] is StepDevice.step_action_specs["delay"] for device_class in STEP_DEVICE_CLASSES)
    assert StepDevice.step_action_specs["delay"].touches_device is False


def test_reset_and_uart_expect_reproduce_the_demo_flow(tmp_path: Path) -> None:
    # The demo loop, declared: flash, open the line, reset into the firmware,
    # wait for its banner, close. The banner arrives in three pieces and no
    # single read contains it, so this only passes if the step matches against
    # everything read so far.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
name: hello-world
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: reset}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(uart_reads=[b"Hel", b"lo Wo", b"rld\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["flash", "uart_open", "reset", "uart_expect", "uart_close"]
    assert result["cleanup"] == []
    # The mode the schema documents reaches the tool rather than being left to it.
    assert ("reset_target", {"mode": "run"}) in service.calls
    expected = result["steps"][3]["result"]
    assert expected["ok"] is True
    assert expected["reads"] == 3
    assert expected["bytes_received"] == 13
    assert [arguments["port_id"] for name, arguments in service.calls if name == "com_read"] == ["dut_uart"] * 3


def test_uart_expect_reassembles_a_character_split_across_two_reads(tmp_path: Path) -> None:
    # The seam a decoded-text buffer cannot survive: one code point arrives half
    # in one read and half in the next. Decoding each read on its own turns both
    # halves into replacement characters, and text that did arrive would never
    # be found.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Grüße", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    banner = "Grüße\r\n".encode()
    service = RecordingService(uart_reads=[banner[:3], banner[3:]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["reads"] == 2
    assert result["steps"][1]["result"]["bytes_received"] == len(banner)


def test_uart_open_hands_the_plans_clean_buffer_choice_to_the_session(tmp_path: Path) -> None:
    # What the plan says about starting clean must reach `com_session_start`,
    # because that is the only thing standing between "the banner I matched came
    # from this boot" and "it was already in the buffer". Both the schema default
    # and an explicit `false` are pinned: a default that silently stopped being
    # applied would leave the first plan below passing for the wrong reason.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n  aux_uart:\n    device: "COM_AUX"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: aux_uart, action: uart_open, clear_buffer: false}
  - {port_id: aux_uart, action: uart_close}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))

    assert result["ok"] is True, result
    opens = [arguments for name, arguments in service.calls if name == "com_session_start"]
    assert opens == [{"clear_buffer": True, "port_id": "dut_uart"}, {"clear_buffer": False, "port_id": "aux_uart"}]


def test_uart_open_refuses_a_non_boolean_clean_buffer(tmp_path: Path) -> None:
    path = write_test_config(tmp_path, 'version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open, clear_buffer: "yes"}\n')

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == "steps[0].clear_buffer"
    assert refused.value.details["validator"] == "type"


def test_uart_expect_pattern_matches_a_value_split_across_two_reads(tmp_path: Path) -> None:
    # The reason a pattern exists at all: checking the board answered with the
    # *right* value, not merely that it answered. The reading is split mid-number
    # across two reads, so this only passes if the pattern is searched against the
    # decoded rolling buffer rather than against the chunk that arrived last:
    # a per-chunk search sees "temp=23" and "7C" and matches neither.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, pattern: "temp=\\\\d+C\\\\b", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(uart_reads=[b"boot ok\r\ntemp=23", b"7C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))

    assert result["ok"] is True, result
    matched = result["steps"][1]["result"]
    assert matched["ok"] is True
    assert matched["reads"] == 2
    assert matched["bytes_received"] == 20
    # A pattern reports under its own key, so a result cannot be read as a
    # substring expectation nobody wrote.
    assert matched["expected_pattern"] == "temp=\\d+C\\b"
    assert "expected_text" not in matched


def test_uart_expect_pattern_is_anchored_only_where_it_is_written(tmp_path: Path) -> None:
    # `re.search` semantics: the same output matches an unanchored pattern and
    # fails a `^`-anchored one, so a plan that means "the line starts this way"
    # gets to say it and a plan that does not is not silently given it.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))

    def run(pattern: str) -> dict:
        plan_path = write_test_config(
            tmp_path,
            f"""version: 2
steps:
  - {{port_id: dut_uart, action: uart_open}}
  - {{port_id: dut_uart, action: uart_expect, pattern: "{pattern}", timeout_s: 0.2}}
  - {{port_id: dut_uart, action: uart_close}}
""",
        )
        service = RecordingService(uart_reads=[b"noise\r\nversion 1.4.2\r\n"])
        return TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))

    assert run("version \\\\d+\\\\.\\\\d+\\\\.\\\\d+")["ok"] is True
    anchored = run("^version \\\\d+\\\\.\\\\d+\\\\.\\\\d+")
    # Named, so this cannot pass on a pattern that merely failed to compile.
    assert anchored["error_type"] == "uart_expect_timeout"


def test_uart_expect_pattern_timeout_reports_the_tail_like_a_text_step(tmp_path: Path) -> None:
    # A pattern that never matches fails the same named way a text step does and
    # answers with the same bounded tail: a red result stays readable without
    # reconnecting to the board, whichever kind of claim went unmet.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, pattern: "temp=\\\\d+C", timeout_s: 0.2}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(uart_reads=[b"boot: stage 1\r\n", b"sensor timeout, temp=??C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "uart_expect_timeout"
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]

    failed = result["steps"][1]["result"]
    assert failed["expected_pattern"] == "temp=\\d+C"
    assert "expected_text" not in failed
    assert "temp=??C" in failed["received_tail"]["text"]
    assert failed["received_tail"]["hex"] == b"boot: stage 1\r\nsensor timeout, temp=??C\r\n".hex()
    assert failed["received_tail_truncated"] is False
    assert failed["bytes_received"] == 41


def test_uart_expect_timeout_fails_the_run_and_reports_what_the_port_said(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 0.2}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(uart_reads=[b"boot: stage 1\r\n", b"HardFault at 0x08000abc\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "uart_expect_timeout"
    # A failed step aborts the run like any other: the close never executed, and
    # the session the plan opened is closed by cleanup instead.
    assert [step["action"] for step in result["steps"]] == ["uart_open", "uart_expect"]
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]
    assert service.recovery_calls, "a failed run must reach the recovery seam"

    failed = result["steps"][1]["result"]
    assert failed["error_type"] == "uart_expect_timeout"
    assert failed["expected_text"] == "Hello World"
    # The point of the named error: a red result says what the port actually
    # said, so nobody has to reconnect to the board to find out.
    assert "HardFault at 0x08000abc" in failed["received_tail"]["text"]
    assert failed["received_tail"]["hex"] == b"boot: stage 1\r\nHardFault at 0x08000abc\r\n".hex()
    assert failed["received_tail_truncated"] is False
    assert failed["bytes_received"] == 40


# A short expectation and one long enough that finding it needs more bytes kept
# than the report is allowed to show. The cap is on what a red step reports, not
# on what it matched against, so both must come back the same size.
@pytest.mark.parametrize("expected_text", ["Hello World", "Hello World " * 20])
def test_uart_expect_timeout_caps_the_reported_tail(tmp_path: Path, expected_text: str) -> None:
    # A chatty port must not turn one red step into an unbounded result, and the
    # tail it does report is the end of the traffic, the part nearest whatever
    # went wrong.
    from agentic_hil.test_reactor import UART_EXPECT_TAIL_BYTES

    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        f"""version: 2
steps:
  - {{port_id: dut_uart, action: uart_open}}
  - {{port_id: dut_uart, action: uart_expect, text: "{expected_text}", timeout_s: 0.2}}
  - {{port_id: dut_uart, action: uart_close}}
""",
    )
    noise = bytes((index % 10) + 48 for index in range(4000))
    service = RecordingService(uart_reads=[noise[:1500], noise[1500:]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    failed = result["steps"][1]["result"]
    assert failed["error_type"] == "uart_expect_timeout"
    assert failed["bytes_received"] == 4000
    assert len(bytes.fromhex(failed["received_tail"]["hex"])) == UART_EXPECT_TAIL_BYTES
    assert failed["received_tail"]["hex"] == noise[-UART_EXPECT_TAIL_BYTES:].hex()
    assert failed["received_tail_truncated"] is True


def test_uart_expect_reports_a_failed_read_as_the_read_refused_it(tmp_path: Path) -> None:
    # A read that was refused is not an expectation that went unmet: answering it
    # with uart_expect_timeout would send an operator to look at firmware that
    # never got the chance to print anything.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(fail_call="com_read")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "can_read_failed"
    assert result["steps"][1]["result"]["tool"] == "com_read"
    assert len([name for name, _ in service.calls if name == "com_read"]) == 1


def test_reset_step_reports_a_backend_mode_refusal_unchanged(tmp_path: Path) -> None:
    # `init` is OpenOCD's reset-init event and nothing else runs it. A backend
    # that says so must have that answer reach the plan, not a reworded one.
    from agentic_hil.backends.common import reset_init_unsupported

    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: reset, mode: init}\n")
    service = RecordingService(reset_result=reset_init_unsupported("pyocd", "pyOCD has no such event"))

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["steps"][0]["result"]["supported_modes"] == ["run", "halt"]
    assert ("reset_target", {"mode": "init"}) in service.calls


def test_reset_step_is_refused_without_the_reset_permission(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False})))
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n  - {debugger: dut, action: reset}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["validation_error"]["field"] == "steps[1].action"
    # Named, so the operator is sent to the grant and not to the bench.
    assert result["validation_error"]["summary"] == "Target reset is disabled for this debugger by the authoritative config."
    # Refused before the run, so the flash the plan would have done never happened.
    assert result["steps"] == []
    assert service.calls == []


def test_reset_step_is_refused_while_a_debug_session_is_open(tmp_path: Path) -> None:
    # `reset_target` is a one-shot and cannot take the lease a live debug session
    # holds, so this plan could only ever come back busy half way through.
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: reset}
  - {debugger: dut, action: debug_stop}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[1].action"
    assert result["validation_error"]["debug_session_debugger"] == "dut"
    assert service.calls == []


def test_new_steps_are_refused_when_they_name_a_device_the_config_does_not_declare(tmp_path: Path) -> None:
    # A route field naming nothing configured is answered by the name, not by a
    # lock failure about a device nobody declared, for the new steps as for the
    # old ones, because each is answered by the class that serves it.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    expect_plan = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: typo_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
""",
    )
    service = RecordingService()

    refused = TestReactor(config, service).run(load_test_config(str(expect_plan), str(tmp_path)))  # type: ignore[arg-type]

    assert refused["ok"] is False
    assert refused["validation_error"]["field"] == "steps[1].port_id"
    assert refused["validation_error"]["configured_com_ports"] == ["dut_uart"]
    assert service.calls == []

    reset_plan = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: typo, action: reset}\n")
    reset_service = RecordingService()

    reset_refused = TestReactor(config, reset_service).run(load_test_config(str(reset_plan), str(tmp_path)))  # type: ignore[arg-type]

    assert reset_refused["validation_error"]["field"] == "steps[0].debugger"
    assert reset_refused["validation_error"]["configured_debuggers"] == ["dut"]
    assert reset_service.calls == []


@pytest.mark.parametrize(
    ("step", "field"),
    [
        # Neither expectation: the step names nothing to wait for, which is a
        # fault in the step rather than in one key, so the field is the step.
        ("{port_id: dut_uart, action: uart_expect, timeout_s: 5}", "steps[0]"),
        ('{port_id: dut_uart, action: uart_expect, text: "", timeout_s: 5}', "steps[0].text"),
        ('{port_id: dut_uart, action: uart_expect, pattern: "", timeout_s: 5}', "steps[0].pattern"),
        ('{port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 0}', "steps[0].timeout_s"),
        ('{port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: -1}', "steps[0].timeout_s"),
        ('{port_id: dut_uart, action: uart_expect, text: "Hello World"}', "steps[0].timeout_s"),
        ('{port_id: dut_uart, debugger: dut, action: uart_expect, text: "x", timeout_s: 5}', "steps[0].debugger"),
        ("{port_id: dut_uart, action: reset}", "steps[0].port_id"),
        ("{debugger: dut, action: reset, mode: warm}", "steps[0].mode"),
    ],
)
def test_new_steps_are_refused_by_the_bundled_schema(tmp_path: Path, step: str, field: str) -> None:
    # Both new steps answer to the same schema pass every other step does: a
    # missing expectation, a timeout that waits for no time at all, a route field
    # belonging to another device kind, and a reset mode no backend has.
    path = write_test_config(tmp_path, f"version: 2\nsteps:\n  - {step}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == field


@pytest.mark.parametrize(
    "expectation",
    [
        # Both: two claims where the schema allows one, so there is no answer to
        # "what did this step wait for".
        'text: "Hello World", pattern: "Hello"',
        # Neither: nothing to wait for at all.
        "",
    ],
)
def test_uart_expect_refuses_a_step_that_is_not_exactly_one_expectation(tmp_path: Path, expectation: str) -> None:
    step = f"{{port_id: dut_uart, action: uart_expect, {expectation + ', ' if expectation else ''}timeout_s: 5}}"
    path = write_test_config(tmp_path, f"version: 2\nsteps:\n  - {step}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == "steps[0]"
    # The refusal says which two keys it means, so the plan can be corrected
    # without reading the bundled schema.
    assert refused.value.details["exactly_one_of"] == ["text", "pattern"]


def test_uart_expect_refuses_a_pattern_that_does_not_compile(tmp_path: Path) -> None:
    # An uncompilable regex is a named refusal before the run starts, not a
    # traceback out of the middle of one: the plan is refused with nothing
    # opened, nothing flashed and no session to clean up.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, pattern: "temp=(\\\\d+", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert result["failed_step"] == 2
    assert result["steps"] == []
    refusal = result["validation_error"]
    assert refusal["field"] == "steps[1].pattern"
    assert refusal["value"] == "temp=(\\d+"
    # The position re gave up at, which is the whole point of catching this.
    assert "missing )" in refusal["regex_error"]
    # Refused before the bench was touched at all.
    assert service.calls == []


def test_unknown_reactor_action_still_names_the_two_new_ones(tmp_path: Path) -> None:
    # The allowed set is read off the device classes, so an action nobody serves
    # is refused with a list that grew by exactly the two steps that were added.
    path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_wait, text: hi}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].action"
    assert {"reset", "uart_expect"} <= set(refused.value.details["allowed_values"])
    assert "uart_wait" not in refused.value.details["allowed_values"]


# The four `version: 2` plans this repository ships, frozen as text at the point
# format v3 was introduced. The files themselves move on to the v3 idiom; these
# copies do not, because what has to keep holding is that a plan already written
# against v2 (on someone's bench, in someone's CI) parses to the same steps and
# runs to the same result after v3 lands. A pin against the live files would only
# ever prove that the files agree with themselves.
V2_EXAMPLE_PLAN = """version: 2
name: capture-state
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open, clear_buffer: true}
  - {bus_id: dut_can, action: can_open, clear_rx_queue: true}
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {bus_id: dut_can, action: can_read, max_frames: 64, wait_timeout_s: 1}
  - {debugger: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {debugger: dut, action: debug_stop}
  - {bus_id: dut_can, action: can_close}
  - {port_id: dut_uart, action: uart_close}
"""

V2_DEMO_PLAN = """version: 2
name: nucleo-f446re-hello-world
steps:
  - {debugger: dut, action: flash, image_path: build/Debug/nucleo-f446re_demo.elf}
  - {port_id: dut_uart, action: uart_open, clear_buffer: true}
  - {debugger: dut, action: reset, mode: run}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
"""

V2_OPENOCD_PLAN = """version: 2
name: nucleo-f446re-openocd
steps:
  - {debugger: dut, action: flash, image_path: build/Debug/demo.elf, reset_after_flash: true}
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/Debug/demo.elf, mode: reset_halt}
  - {debugger: dut, action: run_until_breakpoint, location: delay, timeout_s: 5}
  - {debugger: dut, action: debug_stop}
  - {port_id: dut_uart, action: uart_close}
"""

V2_STLINK_PLAN = """version: 2
name: nucleo-f446re-stlink
steps:
  - {debugger: dut, action: flash, image_path: build/Debug/demo.elf, reset_after_flash: true}
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_close}
"""

# Every step of all four, as `load_test_config` resolves it: the action, the
# routing key each kind reads, and the flat arguments left over. Written out
# rather than derived, so a change to how a v2 plan parses has to be made here
# in full before the suite goes green again.
V2_PLAN_STEPS: dict[str, list[tuple[str, str | None, str | None, str | None, dict]]] = {
    "example": [
        ("flash", "dut", None, None, {"image_path": "build/app.elf"}),
        ("uart_open", None, "dut_uart", None, {"clear_buffer": True}),
        ("can_open", None, None, "dut_can", {"clear_rx_queue": True}),
        ("debug_start", "dut", None, None, {"image_path": "build/app.elf", "mode": "attach"}),
        ("run_until_breakpoint", "dut", None, None, {"location": "capture_done", "timeout_s": 5}),
        ("can_read", None, None, "dut_can", {"max_frames": 64, "wait_timeout_s": 1}),
        ("dump_memory", "dut", None, None, {"symbol": "capture_buffer", "output_path": "build/capture.hex"}),
        ("debug_stop", "dut", None, None, {}),
        ("can_close", None, None, "dut_can", {}),
        ("uart_close", None, "dut_uart", None, {}),
    ],
    "demo": [
        ("flash", "dut", None, None, {"image_path": "build/Debug/nucleo-f446re_demo.elf"}),
        ("uart_open", None, "dut_uart", None, {"clear_buffer": True}),
        ("reset", "dut", None, None, {"mode": "run"}),
        ("uart_expect", None, "dut_uart", None, {"text": "Hello World", "timeout_s": 5}),
        ("uart_close", None, "dut_uart", None, {}),
    ],
    "openocd": [
        ("flash", "dut", None, None, {"image_path": "build/Debug/demo.elf", "reset_after_flash": True}),
        ("uart_open", None, "dut_uart", None, {}),
        ("debug_start", "dut", None, None, {"image_path": "build/Debug/demo.elf", "mode": "reset_halt"}),
        ("run_until_breakpoint", "dut", None, None, {"location": "delay", "timeout_s": 5}),
        ("debug_stop", "dut", None, None, {}),
        ("uart_close", None, "dut_uart", None, {}),
    ],
    "stlink": [
        ("flash", "dut", None, None, {"image_path": "build/Debug/demo.elf", "reset_after_flash": True}),
        ("uart_open", None, "dut_uart", None, {}),
        ("uart_close", None, "dut_uart", None, {}),
    ],
}


@pytest.mark.parametrize(
    ("name", "text"),
    [("example", V2_EXAMPLE_PLAN), ("demo", V2_DEMO_PLAN), ("openocd", V2_OPENOCD_PLAN), ("stlink", V2_STLINK_PLAN)],
)
def test_version_2_plans_parse_to_exactly_the_steps_they_always_did(tmp_path: Path, name: str, text: str) -> None:
    loaded = load_test_config(str(write_test_config(tmp_path, text)), str(tmp_path))

    assert [(step.action, step.debugger, step.port_id, step.bus_id, step.arguments) for step in loaded.steps] == V2_PLAN_STEPS[name]


def test_version_2_demo_plan_runs_to_exactly_the_result_it_always_did(tmp_path: Path) -> None:
    # The whole run, key for key: the banner arrives in three pieces, the step
    # result carries the same fields under the same names, the tool calls are
    # the same calls in the same order, and nothing is left for cleanup. This is
    # the pin format v3 is not allowed to move.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(tmp_path, V2_DEMO_PLAN)
    service = RecordingService(uart_reads=[b"Hel", b"lo Wo", b"rld\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["cleanup"] == []
    assert result["cleanup_ok"] is True
    assert [(step["index"], step["route"], step["action"]) for step in result["steps"]] == [
        (1, "dut", "flash"),
        (2, "dut_uart", "uart_open"),
        (3, "dut", "reset"),
        (4, "dut_uart", "uart_expect"),
        (5, "dut_uart", "uart_close"),
    ]
    assert result["steps"][3]["result"] == {
        "ok": True,
        "tool": "test_reactor",
        "summary": "Expected text appeared on the COM port.",
        "port_id": "dut_uart",
        "expected_text": "Hello World",
        "timeout_s": 5.0,
        "bytes_received": 13,
        "reads": 3,
    }
    assert [name for name, _ in service.calls] == [
        "flash_firmware",
        "com_session_start",
        "reset_target",
        "com_read",
        "com_read",
        "com_read",
        "com_session_stop",
    ]
    assert service.calls[0][1] == {"image_path": "build/Debug/nucleo-f446re_demo.elf"}
    assert service.calls[1][1] == {"clear_buffer": True, "port_id": "dut_uart"}
    assert service.calls[2][1] == {"mode": "run"}
    assert service.calls[6][1] == {"port_id": "dut_uart"}
    assert [sorted(arguments) for name, arguments in service.calls if name == "com_read"] == [["port_id", "wait_timeout_s"]] * 3


def test_version_2_uart_expect_timeout_reports_exactly_what_it_always_did(tmp_path: Path) -> None:
    # The red half of the same pin: a banner that never arrives fails with the
    # tail of what the port did say, under the names a v2 report already uses.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
name: silent
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 0.05}
""",
    )
    service = RecordingService(uart_reads=[b"Goodbye\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "uart_expect_timeout"
    failed = result["steps"][1]["result"]
    assert failed["error_type"] == "uart_expect_timeout"
    assert failed["summary"] == "Expected text did not appear on the COM port before this step's timeout."
    assert failed["expected_text"] == "Hello World"
    assert failed["port_id"] == "dut_uart"
    assert failed["received_tail"]["text"] == "Goodbye\r\n"
    assert failed["received_tail_truncated"] is False
    assert failed["bytes_received"] == 9
    # The plan never closed the port, so cleanup did.
    assert [record["action"] for record in result["cleanup"]] == ["uart_close"]


# --- format v3 ------------------------------------------------------------


def uart_config(tmp_path: Path, *, allow_write: bool = True):
    permissions = {**DEFAULT_TEST_PERMISSIONS, "allow_com_write": allow_write}
    return load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n', permissions=permissions)))


def test_v3_routes_every_kind_by_one_device_key(tmp_path: Path) -> None:
    # The whole point of v3's routing: one key, three sections, and the config
    # is what knows which section a name is in.
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
                can_buses_yaml=CAN_BUS_YAML,
            )
        )
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: one-key
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut_uart, action: uart_open}
  - {device: dut_can, action: can_open}
  - {device: dut, action: reset, mode: run}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["route"] for step in result["steps"]] == ["dut", "dut_uart", "dut_can", "dut"]
    # Each call reached the entry the one key named, in that entry's own scope.
    assert service.calls[1] == ("com_session_start", {"clear_buffer": True, "port_id": "dut_uart"})
    assert service.calls[2] == ("can_session_start", {"clear_rx_queue": True, "bus_id": "dut_can"})
    # Nothing was closed by the plan, so cleanup closed both sessions.
    assert sorted(record["action"] for record in result["cleanup"]) == ["can_close", "uart_close"]


def test_v3_accepts_the_version_2_keys_as_aliases(tmp_path: Path) -> None:
    # `debugger:` / `port_id:` stay readable in a v3 plan: they say the same
    # thing `device:` says, and a plan may mix them.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: aliases
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_close}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["route"] for step in result["steps"]] == ["dut", "dut_uart", "dut_uart"]


def test_a_step_naming_its_device_twice_is_refused(tmp_path: Path) -> None:
    # `device:` and its alias mean one entry, so a step carrying both has not
    # decided which board it drives. Refused rather than resolved by precedence.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: two-keys
steps:
  - {device: dut_uart, port_id: other_uart, action: uart_open}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].port_id"
    assert result["validation_error"]["route_keys"] == ["device", "port_id"]
    assert service.calls == []


def test_v3_plans_need_no_close_because_cleanup_closes(tmp_path: Path) -> None:
    # The idiom v3 makes explicit: a plan that never closes is complete, and the
    # session is provably shut by the run's own cleanup rather than left open.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: no-close
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello World"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"Hello World\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["uart_open", "uart_read"]
    assert [record["action"] for record in result["cleanup"]] == ["uart_close"]
    assert result["cleanup"][0]["result"]["ok"] is True
    assert result["cleanup_ok"] is True
    assert ("com_session_stop", {"port_id": "dut_uart"}) in service.calls


def test_comparator_equals_matches_a_value_split_across_reads(tmp_path: Path) -> None:
    # `equals` is the whole value, not a substring, and the value still arrives
    # in pieces, so the claim is judged against everything read so far.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: equals
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello World"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"Hel", b"lo Wo", b"rld\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["ok"] is True
    assert read["summary"] == "The COM port output equalled the expected value."
    assert read["comparator"] == {"equals": "Hello World"}
    assert read["reads"] == 3
    assert read["bytes_received"] == 13


def test_comparator_equals_matches_a_banner_printed_in_a_loop(tmp_path: Path) -> None:
    # What the demo firmware actually does: prints its banner over and over, so
    # the whole buffer is never one value. Any one complete line of it is, which
    # is why `equals` reads both ways.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: looping-banner
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello World"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"Hello World\nHello World\nHello Wo"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["reads"] == 1


def test_comparator_equals_waits_for_a_line_to_be_complete(tmp_path: Path) -> None:
    # The other half of the same rule: a fragment that starts with the value is
    # not the value, so a longer line still to come cannot pass early.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: partial-line
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello"}, timeout_s: 0.05}
""",
    )
    service = RecordingService(uart_reads=[b"Hello", b" World\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["steps"][1]["result"]["error_type"] == "comparator_unmet"


def test_comparator_equals_judges_unterminated_output_when_time_runs_out(tmp_path: Path) -> None:
    # A board that answers once and stops sends no terminator, so the whole of
    # what was received is judged on the last pass, and only there, which is
    # what the test above holds.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: unterminated
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "READY"}, timeout_s: 0.05}
""",
    )
    service = RecordingService(uart_reads=[b"READY"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["comparator"] == {"equals": "READY"}


def test_comparator_equals_refuses_a_line_that_merely_contains_the_value(tmp_path: Path) -> None:
    # The difference between `equals` and the v2 `text:`: a banner that says more
    # than the expected value does not equal it, and the step says so with the
    # tail of what the port did say.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: equals-red
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello World"}, timeout_s: 0.05}
""",
    )
    service = RecordingService(uart_reads=[b"Hello World and then some\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "comparator_unmet"
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    assert read["summary"] == "The COM port output never equalled the expected value before this step's timeout."
    assert read["comparator"] == {"equals": "Hello World"}
    assert read["received_tail"]["text"] == "Hello World and then some\r\n"
    assert read["received_tail_truncated"] is False


def test_comparator_range_passes_a_captured_value_inside_the_bounds(tmp_path: Path) -> None:
    # The reading, not merely the fact that the board answered: the pattern says
    # where the number is and the range says which numbers are acceptable.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: range-green
steps:
  - {device: dut_uart, action: uart_open}
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "temp=(\\\\d+)C"
      range: {min: 20, max: 30}
    timeout_s: 5
""",
    )
    service = RecordingService(uart_reads=[b"temp=", b"24C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["summary"] == "A value captured from the COM port output fell inside the expected range."
    assert read["captured_value"] == 24.0
    assert read["captured_text"] == "24"
    assert read["comparator"] == {"pattern": "temp=(\\d+)C", "range": {"min": 20, "max": 30}}


def test_comparator_range_fails_naming_the_value_it_did_capture(tmp_path: Path) -> None:
    # A reading that was merely out of range and a board that said nothing are
    # different failures, so the red result carries the number it did read.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: range-red
steps:
  - {device: dut_uart, action: uart_open}
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "temp=(\\\\d+)C"
      range: {min: 20, max: 30}
    timeout_s: 0.05
""",
    )
    service = RecordingService(uart_reads=[b"temp=91C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    assert read["summary"] == "No value captured from the COM port output fell inside the expected range before this step's timeout."
    assert read["captured_value"] == 91.0
    assert read["comparator"]["range"] == {"min": 20, "max": 30}
    assert read["received_tail"]["text"] == "temp=91C\r\n"


def test_comparator_range_takes_the_later_reading_when_the_bench_settles(tmp_path: Path) -> None:
    # An out-of-range reading followed by an in-range one is a bench that
    # settled, so the step passes on the later value rather than answering
    # forever with the earliest occurrence in the window.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: settles
steps:
  - {device: dut_uart, action: uart_open}
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "temp=(\\\\d+)C"
      range: {min: 20, max: 30}
    timeout_s: 5
""",
    )
    service = RecordingService(uart_reads=[b"temp=91C\r\n", b"temp=25C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["captured_value"] == 25.0
    # The reading that settled is also the text the step reports having matched,
    # so a green record cannot show the value the bench had already left behind.
    assert result["steps"][1]["result"]["matched_text"]["text"] == "temp=25C"


def test_a_met_equals_comparator_records_the_line_it_matched(tmp_path: Path) -> None:
    # The green half of `received_tail`. A red step quotes what the port did say;
    # a green one used to quote nothing, so a report could not show what the
    # board answered and the evidence lived only in the COM log.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: equals-green
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {equals: "DIAG ON"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"boot: stage 1\r\n", b"DIAG ON\r\nidle\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["summary"] == "The COM port output equalled the expected value."
    # The line that met the claim, not the whole rolling window: the banner
    # before it is what the port said, not what the step passed on.
    assert read["matched_text"] == {"hex": b"DIAG ON".hex(), "text": "DIAG ON", "encoding": "utf-8"}
    assert read["matched_text_truncated"] is False
    # And the red path's field is not borrowed for it: a met claim is not a tail.
    assert "received_tail" not in read


def test_a_met_pattern_comparator_records_the_slice_that_satisfied_it(tmp_path: Path) -> None:
    # A pattern matches somewhere inside the window, so what a green step has to
    # show is the span it matched rather than everything that had arrived.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: pattern-green
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "READY v[0-9.]+"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"boot: stage 1\r\nREADY v2.4 (dut)\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["matched_text"]["text"] == "READY v2.4"
    assert read["matched_text"]["hex"] == b"READY v2.4".hex()
    assert read["matched_text_truncated"] is False


def test_a_met_pattern_over_invalid_utf8_reports_the_bytes_that_were_on_the_wire(tmp_path: Path) -> None:
    # `matched_text.hex` is sliced out of the receive buffer, not re-encoded from
    # the decoded string: a `replace` decode turns the invalid byte 0xff into `�`,
    # and re-encoding that character would put `efbfbd` in the report as though it
    # had arrived. A green record must never attest to bytes the port never sent.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: invalid-utf8
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "val=.Z"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"val=\xffZ\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    # The raw bytes the pattern matched, 0xff and all; not the codec's rendering
    # of the replacement character it decoded to.
    assert read["matched_text"]["hex"] == b"val=\xffZ".hex()
    assert "efbfbd" not in read["matched_text"]["hex"]
    assert bytes.fromhex(read["matched_text"]["hex"]) in b"val=\xffZ\r\n"


def test_a_met_pattern_under_a_stateful_encoding_does_not_fabricate_a_bom(tmp_path: Path) -> None:
    # A stateful codec is the other way a re-encoding invents bytes: `utf-8-sig`
    # decodes a bufferless payload cleanly but re-encoding the match prepends a
    # BOM (`efbbbf`) that was never on the wire. The raw slice carries only what
    # arrived.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: stateful-encoding
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "READY v[0-9.]+"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"READY v2.4\r\n"], uart_encoding="utf-8-sig")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["matched_text"]["hex"] == b"READY v2.4".hex()
    assert not read["matched_text"]["hex"].startswith("efbbbf")
    assert bytes.fromhex(read["matched_text"]["hex"]) in b"READY v2.4\r\n"


def test_matched_span_bytes_omits_the_byte_field_for_an_interior_stateful_match() -> None:
    """Round-1 finding 4: an interior character of a multi-character emission.

    A stateful codec such as UTF-7 buffers a run and emits several characters
    only when the byte closing the run arrives, so where inside the run each of
    them began is not recoverable. A span that matches only an interior character
    -- ``本`` inside ``日本語`` -- has no honest byte boundary, and must return
    ``None`` (which the caller renders as "byte field unavailable") rather than
    the empty slice it used to hand back and dress up as ``{"hex": "", ...}``. The
    run's outer edges are still sound, so matching the whole run, or the ASCII
    characters framing it, still yields the exact wire bytes."""
    from agentic_hil.comports import decode_bytes, matched_span_bytes

    raw = "x日本語y".encode("utf-7")
    assert decode_bytes(raw, "utf-7") == "x日本語y"
    # The interior character has no recoverable byte boundary: None, not b"".
    assert matched_span_bytes(raw, "utf-7", (2, 3)) is None
    # The whole run does, and it round-trips to the characters it stands for.
    whole_run = matched_span_bytes(raw, "utf-7", (1, 4))
    assert whole_run is not None and whole_run.decode("utf-7") == "日本語"
    # The ASCII characters on either side of the run are unambiguous too.
    assert matched_span_bytes(raw, "utf-7", (0, 1)) == b"x"
    assert matched_span_bytes(raw, "utf-7", (4, 5)) == b"y"


def test_a_met_pattern_on_an_interior_stateful_char_reports_honest_text_not_empty_bytes(tmp_path: Path) -> None:
    # The reactor-level face of round-1 finding 4: a comparator that matches only
    # `本` inside a UTF-7 `日本語` run has no honest byte slice, so the report
    # quotes the matched text and marks the byte field unavailable rather than
    # attesting to `{"hex": "", "text": "", ...}` -- an empty slice dressed up as
    # evidence of a match that really happened.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: interior-stateful
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "本"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=["x日本語y\r\n".encode("utf-7")], uart_encoding="utf-7")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    # The match is real and its text is quoted, but no fabricated byte field: the
    # honest fallback rather than the empty-slice `{"hex": ""}` the bug produced.
    assert read["matched_text"]["text"] == "本"
    assert "hex" not in read["matched_text"]
    assert read["matched_text_bytes_unavailable"] is True


def test_matched_span_bytes_omits_the_byte_field_for_a_shift_state_dependent_match() -> None:
    """Round-2 finding 2: a character whose byte slice is only itself in the
    decoder state earlier bytes established.

    ISO-2022-JP switches into two-byte mode with an escape that arrives before
    the run, so the interior ``本`` is emitted on its own -- not as part of a
    multi-character emission the ``ambiguous`` set catches -- yet its two bytes
    ``4b 5c`` decode to the ASCII ``K`` and a backslash from the initial state,
    not to ``本``. That isolated slice is false wire evidence, so the byte field
    must be omitted (``None``) and the caller left with the honest text-only
    match. The whole run, escape included, still slices honestly because it
    carries the state it needs, as do the ASCII characters framing it."""
    from agentic_hil.comports import decode_bytes, matched_span_bytes

    raw = "x日本語y".encode("iso2022_jp")
    assert decode_bytes(raw, "iso2022_jp") == "x日本語y"
    # The interior character's byte slice only decodes to it inside the shifted
    # run, so on its own it is not honest evidence: None, not b"K\\".
    assert matched_span_bytes(raw, "iso2022_jp", (2, 3)) is None
    # The whole run carries the escape that establishes its state, so it slices
    # honestly and round-trips to the characters it stands for.
    whole_run = matched_span_bytes(raw, "iso2022_jp", (1, 4))
    assert whole_run is not None and whole_run.decode("iso2022_jp") == "日本語"
    # The leading ASCII character needs no shift state and slices to its one byte.
    assert matched_span_bytes(raw, "iso2022_jp", (0, 1)) == b"x"


def test_a_met_pattern_on_a_shifted_interior_char_reports_honest_text_not_false_bytes(tmp_path: Path) -> None:
    # The reactor-level face of round-2 finding 2: a comparator that matches only
    # `本` inside an ISO-2022-JP `日本語` run has no honest byte slice, because the
    # two bytes it decoded from mean the ASCII `K` and a backslash outside the
    # shift the earlier escape set up. The report quotes the matched text and
    # marks the byte field unavailable rather than attesting to `K\`, a slice that
    # decodes to something the port never matched.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: shifted-interior
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "本"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=["x日本語y\r\n".encode("iso2022_jp")], uart_encoding="iso2022_jp")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    # The match is real and its text is quoted, but no false byte field: the
    # honest fallback rather than the state-dependent `4b5c` slice.
    assert read["matched_text"]["text"] == "本"
    assert "hex" not in read["matched_text"]
    assert read["matched_text_bytes_unavailable"] is True


def test_an_unmet_comparator_still_answers_with_the_tail_and_claims_no_match(tmp_path: Path) -> None:
    # The other direction of the same field: a step that matched nothing must not
    # carry a matched text at all, because absence is what says nothing met it.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: pattern-red
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "READY v[0-9.]+"}, timeout_s: 0.05}
""",
    )
    service = RecordingService(uart_reads=[b"boot: stage 1\r\nHardFault\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    assert read["received_tail"]["text"] == "boot: stage 1\r\nHardFault\r\n"
    assert read["received_tail_truncated"] is False
    assert "matched_text" not in read
    assert "matched_text_truncated" not in read


def test_a_match_longer_than_the_cap_is_bounded_and_says_so(tmp_path: Path) -> None:
    # The cap the red path has, on the green one and for the same reason: a claim
    # met by a value longer than the report should carry must not put all of it
    # in the report, and a bounded quote that did not say it was bounded would
    # read as the whole of what matched. A long unterminated answer is the case
    # that reaches it, because the read window widens with what the plan wrote.
    from agentic_hil.test_reactor import UART_EXPECT_TAIL_BYTES

    long_value = "A" * (UART_EXPECT_TAIL_BYTES + 88)
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        f"""version: 3
name: long-match
steps:
  - {{device: dut_uart, action: uart_open}}
  - {{device: dut_uart, action: uart_read, comparator: {{equals: "{long_value}"}}, timeout_s: 0.05}}
""",
    )
    service = RecordingService(uart_reads=[long_value.encode()])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert len(bytes.fromhex(read["matched_text"]["hex"])) == UART_EXPECT_TAIL_BYTES
    # Kept from the front, because a match begins where it begins.
    assert read["matched_text"]["hex"] == long_value.encode()[:UART_EXPECT_TAIL_BYTES].hex()
    assert read["matched_text_truncated"] is True


def test_a_met_range_comparator_records_the_reading_around_the_number(tmp_path: Path) -> None:
    # The claim a `range` really makes: a reading inside the bounds, quoted whole
    # rather than only as the number pulled out of it, so a green record shows
    # the unit and the surrounding text the plan wrote the pattern against.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: range-green-text
steps:
  - {device: dut_uart, action: uart_open}
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "temp=(\\\\d+)C"
      range: {min: 20, max: 30}
    timeout_s: 5
""",
    )
    service = RecordingService(uart_reads=[b"temp=", b"24C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["captured_text"] == "24"
    assert read["matched_text"]["text"] == "temp=24C"
    assert read["matched_text_truncated"] is False


def test_a_met_range_quotes_the_reading_that_met_it_not_a_later_one(tmp_path: Path) -> None:
    # An in-range reading followed by an out-of-range one in the same window: the
    # step passes, and what it says it matched must be the reading that met the
    # bounds. `captured_value` is the last reading the pattern found, which is a
    # different question and keeps its own answer.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: range-then-drift
steps:
  - {device: dut_uart, action: uart_open}
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "temp=(\\\\d+)C"
      range: {min: 20, max: 30}
    timeout_s: 5
""",
    )
    service = RecordingService(uart_reads=[b"temp=24C\r\ntemp=91C\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["matched_text"]["text"] == "temp=24C"
    assert read["captured_value"] == 91.0


def test_a_range_without_a_capture_group_is_refused_before_the_run(tmp_path: Path) -> None:
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: no-capture
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "temp=\\\\d+C", range: {min: 20, max: 30}}, timeout_s: 5}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    refusal = result["validation_error"]
    assert refusal["field"] == "steps[1].comparator.pattern"
    assert refusal["capture_groups"] == 0
    # Refused before the bench was touched at all.
    assert service.calls == []


def test_a_range_without_a_pattern_is_refused_by_the_schema(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        'version: 3\nsteps:\n  - {device: dut_uart, action: uart_read, comparator: {equals: "hi", range: {min: 1, max: 2}}, timeout_s: 5}\n',
    )

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"


def test_a_comparator_saying_two_things_at_once_is_refused(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        'version: 3\nsteps:\n  - {device: dut_uart, action: uart_read, comparator: {equals: "hi", pattern: "hi"}, timeout_s: 5}\n',
    )

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["exactly_one_of"] == ["equals", "pattern"]


def test_uart_read_without_a_comparator_is_a_plain_read(tmp_path: Path) -> None:
    # No claim, no waiting for one: the step is `com_read` and answers exactly
    # as `com_read` answers.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: plain
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_read, timeout_s: 0.5}
""",
    )
    service = RecordingService(uart_reads=[b"whatever\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["tool"] == "com_read"
    assert read["data"]["text"] == "whatever\r\n"
    # Exactly one read, and the plan's own deadline is what it waited.
    assert [arguments for name, arguments in service.calls if name == "com_read"] == [{"port_id": "dut_uart", "wait_timeout_s": 0.5}]


def test_uart_write_sends_stimulus_through_the_com_write_tool(tmp_path: Path) -> None:
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: stimulus
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_write, text: "ping\\n"}
  - {device: dut_uart, action: uart_read, comparator: {equals: "pong"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"pong\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["uart_open", "uart_write", "uart_read"]
    # The same tool an agent driving the line by hand would call, addressed to
    # the port the step routed to rather than to whatever the plan wrote.
    assert ("com_write", {"text": "ping\n", "port_id": "dut_uart"}) in service.calls


def test_uart_write_is_refused_by_name_when_the_port_denies_writing(tmp_path: Path) -> None:
    # The permission is the config's, and the refusal is the plan's: named
    # before the run, so the line is never half driven.
    config = uart_config(tmp_path, allow_write=False)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: denied
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_write, text: "ping"}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    refusal = result["validation_error"]
    assert refusal["field"] == "steps[1].action"
    assert refusal["summary"] == "Writing to this COM port is disabled by the authoritative config."
    assert refusal["permission"] == "allow_write"
    assert service.calls == []


def can_frame(frame_id: int, data_hex: str = "", *, extended: bool = False) -> dict:
    """One received frame, in the shape `can_read` normalizes every adapter's
    answer to, so a test that passes here is a test the real read path feeds."""
    data = bytes.fromhex(data_hex)
    return {"id": frame_id, "id_hex": f"0x{frame_id:x}", "extended": extended, "rtr": False, "data_hex": data.hex(), "dlc": len(data)}


def can_comparator_plan(tmp_path: Path, comparator: str, *, timeout_s: str = "5") -> Path:
    return write_test_config(
        tmp_path,
        f"""version: 3
name: bus-expectation
steps:
  - {{device: dut_can, action: can_open}}
  - {{device: dut_can, action: can_read, comparator: {comparator}, timeout_s: {timeout_s}}}
""",
    )


def test_can_comparator_matches_the_frame_the_plan_named(tmp_path: Path) -> None:
    # The identifier says which frame, the payload says what it must carry, and
    # neither alone is the claim: the first read here carries the expected
    # payload under another id and the expected id with another payload, and
    # both are passed over.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x123", equals: "01 ff"}')
    service = RecordingService(can_reads=[[can_frame(0x120, "01ff"), can_frame(0x123, "0000")], [can_frame(0x123, "01ff")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["summary"] == "A frame on the CAN bus carried the expected payload."
    assert read["frame"]["id_hex"] == "0x123"
    assert read["frame"]["data_hex"] == "01ff"
    # The plan's own spelling is echoed back; the whitespace in it was a spelling
    # of the bytes, not part of the claim.
    assert read["comparator"] == {"id": "0x123", "equals": "01 ff"}
    # The frame was not in the queue the first read drained, so the step read
    # again, which is the whole difference from a plain read.
    assert read["reads"] == 2
    assert read["frames_read"] == 3


def test_can_comparator_ignores_a_frame_carrying_the_payload_under_another_id(tmp_path: Path) -> None:
    # A bus carries every node's traffic. The payload this plan is waiting for is
    # on the wire twice and never under the identifier it named, so the step is
    # red, and says which frames it did see, because "the id is wrong in the
    # plan" and "the board never sent it" are different repairs.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x123", equals: "01ff"}', timeout_s="0.05")
    service = RecordingService(can_reads=[[can_frame(0x120, "01ff"), can_frame(0x7FF, "01ff")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "comparator_unmet"
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    assert read["summary"] == "No frame on the CAN bus carried the expected payload before this step's timeout."
    assert read["frames_tail"] == [{"id_hex": "0x120", "data_hex": "01ff", "extended": False}, {"id_hex": "0x7ff", "data_hex": "01ff", "extended": False}]
    assert read["frames_tail_truncated"] is False
    assert read["frames_read"] == 2


def test_can_comparator_reads_an_identifier_family_through_a_mask(tmp_path: Path) -> None:
    # `id` plus `id_mask` is a family rather than one value, which is how a plan
    # waits for any frame of a protocol that encodes a node in the low bits.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x120", id_mask: "0x7f0", pattern: "^dead"}')
    service = RecordingService(can_reads=[[can_frame(0x100, "deadbeef"), can_frame(0x12F, "deadbeef")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["summary"] == "Expected pattern matched the payload of a frame on the CAN bus."
    # 0x100 is outside the family the mask describes; 0x12f is inside it, and
    # both carry the payload the pattern would otherwise match.
    assert read["frame"]["id_hex"] == "0x12f"


def test_a_can_comparator_selects_by_frame_type_not_identifier_alone(tmp_path: Path) -> None:
    # A standard 0x123 and an extended 0x123 are two different frames on the wire.
    # A comparator that does not say `extended` waits for the standard one, so the
    # extended twin carrying the expected payload is passed over and the standard
    # twin behind it is the match: the same distinction the broker's participant
    # filters draw. Without the discriminator the extended twin was a false accept.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x123", equals: "01ff"}')
    service = RecordingService(can_reads=[[can_frame(0x123, "01ff", extended=True)], [can_frame(0x123, "01ff")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["frame"]["extended"] is False
    assert read["reads"] == 2, "the extended twin was passed over; the standard frame in the next read is the match"


def test_a_can_comparator_marked_extended_waits_for_the_extended_frame(tmp_path: Path) -> None:
    # The mirror: `extended: true` matches the extended frame of that number and
    # passes over its standard twin, so the type match is a match and not a
    # blanket refusal.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x123", extended: true, equals: "01ff"}')
    service = RecordingService(can_reads=[[can_frame(0x123, "01ff")], [can_frame(0x123, "01ff", extended=True)]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["frame"]["extended"] is True
    assert read["comparator"]["extended"] is True
    assert read["reads"] == 2, "the standard twin was passed over; the extended frame in the next read is the match"


def test_a_masked_can_comparator_still_separates_the_frame_types(tmp_path: Path) -> None:
    # The type match holds under a mask too: an id_mask widens which numbers a
    # comparator selects, never which frame namespace, so an extended frame inside
    # a standard-typed family is still not the frame the plan asked about.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x120", id_mask: "0x7f0", extended: false, pattern: "^dead"}', timeout_s="0.05")
    service = RecordingService(can_reads=[[can_frame(0x12F, "deadbeef", extended=True)]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    # The rejected frame was the extended twin inside a standard-typed family, and
    # the tail keeps `extended` so the failure shows the frame type rather than a
    # frame that reads as identical to the one the plan asked for.
    assert read["frames_tail"] == [{"id_hex": "0x12f", "data_hex": "deadbeef", "extended": True}]


def test_a_standard_comparator_shows_the_frame_type_of_the_extended_twin_it_rejected(tmp_path: Path) -> None:
    # The reproduction the review named: an extended 0x123 carrying the expected
    # payload, judged by a standard comparator. It is rejected on frame type, and
    # its `frames_tail` entry must carry `extended: true`, otherwise the failure
    # shows an id and a payload identical to the requested frame and omits the only
    # field that explains why it did not match.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x123", equals: "01ff"}', timeout_s="0.05")
    service = RecordingService(can_reads=[[can_frame(0x123, "01ff", extended=True)]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    assert read["frames_tail"] == [{"id_hex": "0x123", "data_hex": "01ff", "extended": True}]


def test_can_comparator_range_reads_its_capture_in_the_base_the_payload_is_written_in(tmp_path: Path) -> None:
    # The signal, not merely the frame: the pattern says where in the payload the
    # value is and the range says which values are acceptable. The capture comes
    # out of hexadecimal, so `19` here is 25: read as decimal it would be 19 and
    # this bench would report a settled reading as out of range.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: 513, pattern: "^02(..)$", range: {min: 20, max: 30}}')
    service = RecordingService(can_reads=[[can_frame(0x201, "0291")], [can_frame(0x201, "0219")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["summary"] == "A value captured from a frame's payload fell inside the expected range."
    assert read["captured_text"] == "19"
    assert read["captured_value"] == 25.0
    assert read["reads"] == 2


def test_can_comparator_range_fails_naming_the_value_it_did_capture(tmp_path: Path) -> None:
    # A signal that was merely out of range and a bus that never carried the
    # frame are different failures, so the red result carries the number it read.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x201", pattern: "^02(..)$", range: {min: 20, max: 30}}', timeout_s="0.05")
    service = RecordingService(can_reads=[[can_frame(0x201, "0291")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "comparator_unmet"
    assert read["summary"] == "No value captured from a frame's payload fell inside the expected range before this step's timeout."
    assert read["captured_value"] == 145.0
    assert read["comparator"]["range"] == {"min": 20, "max": 30}
    assert read["frames_tail"] == [{"id_hex": "0x201", "data_hex": "0291", "extended": False}]


def test_can_comparator_caps_the_frames_it_reports(tmp_path: Path) -> None:
    # The `received_tail` honesty in this medium's unit: a busy bus must not turn
    # one red step into an unbounded result, and a truncated tail says so rather
    # than reading as the whole of what the bus carried.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x7ff", equals: "ff"}', timeout_s="0.05")
    service = RecordingService(can_reads=[[can_frame(0x300 + number, "00") for number in range(20)]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["frames_read"] == 20
    assert len(read["frames_tail"]) == 16
    assert read["frames_tail_truncated"] is True
    # The last frames, not the first: what the bus was doing when the step gave
    # up is what an operator reads next.
    assert read["frames_tail"][-1] == {"id_hex": "0x313", "data_hex": "00", "extended": False}


def test_a_can_comparator_saying_two_things_at_once_is_refused(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        'version: 3\nsteps:\n  - {device: dut_can, action: can_read, comparator: {id: 1, equals: "01", pattern: "01"}, timeout_s: 5}\n',
    )

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["exactly_one_of"] == ["equals", "pattern"]


def test_a_can_range_without_a_capture_group_is_refused_before_the_run(tmp_path: Path) -> None:
    # The same refusal the serial comparator gives, in the same shape and under
    # the same field name, because what a pattern must satisfy is a property of
    # the claim rather than of the medium it is judged against.
    config = can_config(tmp_path)
    plan_path = can_comparator_plan(tmp_path, '{id: "0x201", pattern: "^02..$", range: {min: 20, max: 30}}')
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    refusal = result["validation_error"]
    assert refusal["field"] == "steps[1].comparator.pattern"
    assert refusal["capture_groups"] == 0
    # Refused before the bus was opened at all.
    assert service.calls == []


def test_a_can_comparator_beside_a_per_read_wait_is_refused(tmp_path: Path) -> None:
    # Two deadlines with different owners. Honouring one and ignoring the other
    # is how a plan comes to carry a number nothing reads.
    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
steps:
  - {device: dut_can, action: can_open}
  - {device: dut_can, action: can_read, comparator: {id: 1, equals: "01"}, timeout_s: 5, wait_timeout_s: 1}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[1].wait_timeout_s"
    assert "timeout_s" in result["validation_error"]["summary"]
    assert service.calls == []


def test_can_read_without_a_comparator_is_the_version_2_step_unchanged(tmp_path: Path) -> None:
    # No claim, no reading again: the step is one `can_read` with the plan's own
    # arguments, and it answers exactly as `can_read` answers.
    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
name: plain
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_read, max_frames: 4, wait_timeout_s: 0.5}
""",
    )
    service = RecordingService(can_reads=[[can_frame(0x123, "01ff")]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["tool"] == "can_read"
    assert read["frames"] == [can_frame(0x123, "01ff")]
    assert "comparator" not in read
    assert [arguments for name, arguments in service.calls if name == "can_read"] == [{"max_frames": 4, "wait_timeout_s": 0.5, "bus_id": "dut_can"}]


def test_an_action_a_kind_does_not_declare_answers_not_supported(tmp_path: Path) -> None:
    # By construction, not by a forgotten special case: dispatch looks the string
    # up among the declarations and there is nothing else to look in.
    from agentic_hil.test_reactor import CanRunner, TestStep

    config = can_config(tmp_path)
    reactor = TestReactor(config, RecordingService())  # type: ignore[arg-type]
    device = reactor.device_for(CanRunner, "dut_can")

    result = device.execute(TestStep("uart_write", {"text": "ping"}, device="dut_can"))

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["summary"] == "The can device kind does not serve this test reactor action."
    assert result["bus_id"] == "dut_can"
    assert result["action"] == "uart_write"
    assert "can_send" in result["supported_actions"]


def test_dispatch_holds_a_step_to_its_own_actions_schema(tmp_path: Path) -> None:
    # The second half of dispatch: a step some other caller assembled cannot
    # reach a device method carrying arguments the format never allowed.
    from agentic_hil.test_reactor import CanRunner, TestStep

    config = can_config(tmp_path)
    reactor = TestReactor(config, RecordingService())  # type: ignore[arg-type]
    device = reactor.device_for(CanRunner, "dut_can")

    result = device.execute(TestStep("can_send", {"frame_id": -1}, device="dut_can"))

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert result["summary"] == "Test step does not satisfy its action's schema."
    assert result["field"] == "frame_id"


def test_delay_waits_on_two_different_device_kinds_and_is_in_the_result(tmp_path: Path) -> None:
    # One declaration on the base, so both kinds serve it; and it is a step, so
    # it appears in the record of what the run did.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: waiting
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut, action: delay, duration_ms: 1}
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: delay, duration_ms: 1}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["flash", "delay", "uart_open", "delay"]
    assert [step["route"] for step in result["steps"]] == ["dut", "dut", "dut_uart", "dut_uart"]
    assert result["steps"][1]["result"] == {
        "ok": True,
        "tool": "test_reactor",
        "summary": "Test plan waited.",
        "debugger": "dut",
        "action": "delay",
        "duration_ms": 1,
    }
    assert result["steps"][3]["result"]["port_id"] == "dut_uart"
    # It drove nothing: no tool call sits between the flash and the open.
    assert [name for name, _ in service.calls] == ["flash_firmware", "com_session_start", "com_session_stop"]


def test_delay_before_the_session_it_waits_on_is_opened(tmp_path: Path) -> None:
    # An action that drives no hardware is in no session, so the session-order
    # refusals of the kind it routes to are about a step this one is not.
    config = uart_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 3
name: wait-first
steps:
  - {device: dut_uart, action: delay, duration_ms: 1}
  - {device: dut_uart, action: uart_open}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result


def test_a_delay_naming_no_device_is_refused_by_the_schema(tmp_path: Path) -> None:
    path = write_test_config(tmp_path, "version: 3\nsteps:\n  - {action: delay, duration_ms: 10}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == "steps[0]"


def test_a_delay_longer_than_the_cap_is_refused(tmp_path: Path) -> None:
    from agentic_hil.test_reactor import DELAY_MAX_MS

    path = write_test_config(tmp_path, f"version: 3\nsteps:\n  - {{device: dut, action: delay, duration_ms: {DELAY_MAX_MS + 1}}}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].duration_ms"


@pytest.mark.parametrize(
    ("step", "named"),
    [
        ("{device: dut_uart, action: uart_open}", "device"),
        ("{port_id: dut_uart, action: uart_read, timeout_s: 1}", "uart_read"),
        ("{port_id: dut_uart, action: uart_write, text: hi}", "uart_write"),
        ("{port_id: dut_uart, action: delay, duration_ms: 5}", "delay"),
        # `can_read` is a version 2 action; what is new on it is the claim, so
        # the key is what the gate names rather than the step.
        ('{bus_id: dut_can, action: can_read, comparator: {id: 1, equals: "01"}, timeout_s: 1}', "comparator"),
    ],
)
def test_a_version_2_plan_cannot_reach_for_version_3(tmp_path: Path, step: str, named: str) -> None:
    # The version a plan declares is what tells another install whether it can
    # run it, so a v2 plan using a v3 action or key is refused by name rather
    # than working here and failing there for no stated reason.
    path = write_test_config(tmp_path, f"version: 2\nsteps:\n  - {step}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["value"] == named
    assert refused.value.details["plan_version"] == 2
    assert refused.value.details["requires_plan_version"] == 3
    assert "version: 3" in refused.value.details["migration"]


def test_every_plan_this_repository_ships_loads(tmp_path: Path) -> None:
    # A reader copies these. A plan that does not load is documentation of a
    # format that does not exist, and a migration is exactly when that happens.
    repository_root = Path(__file__).resolve().parents[1]
    shipped = sorted(repository_root.glob("examples/**/testconfig*.yaml")) + sorted(repository_root.glob("evals/**/testconfig*.yaml"))

    assert len(shipped) >= 4, shipped
    for plan_path in shipped:
        loaded = load_test_config(str(plan_path), str(repository_root))
        assert loaded.steps, plan_path
    # The demo plan is the one a reader runs first: v3 idiom, no close step, and
    # the read makes a claim rather than merely reading.
    demo = load_test_config(str(repository_root / "examples" / "nucleo-f446re_demo" / "testconfig.yaml"), str(repository_root))
    assert [step.action for step in demo.steps] == ["flash", "uart_open", "reset", "uart_read"]
    assert [step.device for step in demo.steps] == ["dut", "dut_uart", "dut", "dut_uart"]
    assert demo.steps[3].arguments["comparator"] == {"equals": "Hello World"}


DEMO_PLAN_STEP_COUNTS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}

# A step count a document states about the demo plan: the written number in
# front of "steps", close enough behind the plan's own file name to be a claim
# about that plan rather than about steps in general. The prose is read with its
# line breaks collapsed first, so a sentence that reflows still reads as one.
DEMO_PLAN_COUNT = re.compile(rf"testconfig\.yaml.{{0,240}}?\b({'|'.join(DEMO_PLAN_STEP_COUNTS.values())}) steps\b")


@pytest.mark.parametrize("document", ["docs/test-plan-contract.md", "examples/nucleo-f446re_demo/tests/test_firmware.py"])
def test_a_document_that_counts_the_demo_plan_counts_the_plan_it_names(document: str) -> None:
    """Two documents introduce the demo plan by its size, and neither sits next
    to it. The contract page invited a reader to review the plan by eye and said
    it was five steps; the plan has four, and the pytest docstring repeated the
    five to the audience most likely to open it next. The count is read off the
    plan here, so a step added or dropped fails a test rather than a reader.
    """
    repository_root = Path(__file__).resolve().parents[1]
    demo = load_test_config(str(repository_root / "examples" / "nucleo-f446re_demo" / "testconfig.yaml"), str(repository_root))
    written = DEMO_PLAN_STEP_COUNTS.get(len(demo.steps))
    assert written is not None, f"the demo plan has {len(demo.steps)} steps, more than this test can spell"

    prose = re.sub(r"\s+", " ", (repository_root / document).read_text(encoding="utf-8"))
    counted = DEMO_PLAN_COUNT.findall(prose)

    assert counted, f"{document} no longer counts the demo plan; drop it from this test rather than leaving it unchecked"
    assert set(counted) == {written}, f"{document} counts the demo plan as {sorted(set(counted))}; it has {len(demo.steps)} steps"


# --- the repeat block step, plan format version 4 --------------------------


def repeat_plan(tmp_path: Path, block: str, *, version: int = 4) -> Path:
    """A plan whose only step is the block `block`."""
    return write_test_config(tmp_path, f"version: {version}\nsteps:\n{block}")


class SessionTrackingService(RecordingService):
    """A service that remembers which serial sessions are open, the way the real
    tool does: a second `com_session_start` on a live session answers
    `already_active`, which is exactly what a plan opening a port inside a loop
    meets on every iteration after the first."""

    def __init__(self) -> None:
        super().__init__()
        self.open_ports: set[str] = set()

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        port = str((arguments or {}).get("port_id"))
        if name == "com_session_start":
            already_active = port in self.open_ports
            self.open_ports.add(port)
            return {**result, "already_active": already_active}
        if name == "com_session_stop":
            self.open_ports.discard(port)
        return result


class ResetFailsOnAttempt(RecordingService):
    """A probe whose Nth reset comes back unconfirmed. Everything else answers
    exactly as `RecordingService` does, so what a test using this observes is
    one failure at a place it chose."""

    def __init__(self, attempt: int) -> None:
        super().__init__()
        self.attempt = attempt
        self.resets = 0

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "reset_target":
            self.resets += 1
            if self.resets == self.attempt:
                return {"ok": False, "tool": name, "error_type": "reset_unconfirmed", "summary": "The probe did not confirm the reset."}
        return result


def test_a_repeat_step_without_a_bound_is_refused(tmp_path: Path) -> None:
    # A plan may not loop unbounded, and the schema is where that is settled, so
    # nothing downstream has to decide what to do with a loop that never ends.
    path = repeat_plan(tmp_path, "  - {action: repeat, steps: [{device: dut, action: reset}]}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == "steps[0]"
    assert refused.value.details["validator"] == "anyOf"


def test_a_repeat_count_below_one_is_refused(tmp_path: Path) -> None:
    path = repeat_plan(tmp_path, "  - {action: repeat, count: 0, steps: [{device: dut, action: reset}]}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].count"
    assert refused.value.details["value"] == 0


def test_a_repeat_duration_of_zero_is_refused(tmp_path: Path) -> None:
    # Zero is not "do not wait" here either: a loop bounded by no time at all
    # has no reading a plan could have meant.
    path = repeat_plan(tmp_path, "  - {action: repeat, duration_s: 0, steps: [{device: dut, action: reset}]}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].duration_s"
    assert refused.value.details["value"] == 0


def test_a_repeat_step_may_not_name_a_device(tmp_path: Path) -> None:
    # The block is a statement about the sequence, not about the bench. Routing
    # it somewhere would make the reactor look like it drives a device it never
    # touches, and would put a board in the run's lock declaration for a step
    # that does nothing to it.
    path = repeat_plan(tmp_path, "  - {device: dut, action: repeat, count: 2, steps: [{device: dut, action: reset}]}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].device"


def test_a_step_nested_in_a_repeat_is_refused_at_the_path_that_names_its_nesting(tmp_path: Path) -> None:
    # A reader counts down the file to find the step a refusal names. Reporting
    # a nested step at the top level would send them to a different step.
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut, action: reset}
  - action: repeat
    count: 2
    steps:
      - {device: dut, action: reset}
      - {device: dut, action: delay, duration_ms: 0}
""",
    )

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[2].steps[1].duration_ms"


def test_a_version_3_plan_cannot_reach_for_the_repeat_step(tmp_path: Path) -> None:
    # The version a plan declares is what tells another install whether it can
    # run it, so a v3 plan using the v4 block step is refused by name here
    # rather than failing on an older install for no stated reason.
    path = repeat_plan(tmp_path, "  - {action: repeat, count: 2, steps: [{device: dut, action: reset}]}\n", version=3)

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].action"
    assert refused.value.details["value"] == "repeat"
    assert refused.value.details["plan_version"] == 3
    assert refused.value.details["requires_plan_version"] == 4
    assert "version: 4" in refused.value.details["migration"]


def test_the_version_gate_descends_into_a_repeat_blocks_own_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The marker is moved onto `reset` for the length of this test, so what is
    # under test stays the walk rather than whichever action happens to be the
    # newest one shipped: a step nested inside a block is held to the plan's
    # version exactly as a top-level step is, or a block would be the way around
    # the gate.
    from agentic_hil import test_reactor

    schema = json.loads(json.dumps(test_reactor.test_config_schema()))
    schema["$defs"]["reset"]["x-since-version"] = 5
    schema["properties"]["version"]["enum"] = [2, 3, 4, 5]
    monkeypatch.setattr(test_reactor, "test_config_schema", lambda: schema)
    path = repeat_plan(tmp_path, "  - {action: repeat, count: 2, steps: [{device: dut, action: reset}]}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].steps[0].action"
    assert refused.value.details["requires_plan_version"] == 5


def test_a_repeat_inside_a_repeat_loads_as_a_nested_block(tmp_path: Path) -> None:
    # The nested list is the same shape as the plan's own, which is the whole of
    # why a block nests. Loading it as steps rather than as raw mappings is what
    # lets preflight, the lock declaration and the executor see it.
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - action: repeat
    count: 2
    steps:
      - {device: dut, action: reset}
      - action: repeat
        duration_s: 1.5
        steps:
          - {device: dut, action: delay, duration_ms: 1}
""",
    )

    loaded = load_test_config(str(path), str(tmp_path))

    outer = loaded.steps[0]
    assert outer.action == "repeat"
    assert outer.route == "-"
    assert outer.arguments == {"count": 2}
    assert [step.action for step in outer.steps] == ["reset", "repeat"]
    inner = outer.steps[1]
    assert inner.arguments == {"duration_s": 1.5}
    assert [step.action for step in inner.steps] == ["delay"]


def test_a_repeat_count_runs_exactly_that_many_iterations(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    path = repeat_plan(tmp_path, "  - {action: repeat, count: 4, steps: [{device: dut, action: reset}]}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert sum(1 for name, _ in service.calls if name == "reset_target") == 4
    block = result["steps"][0]
    assert block["action"] == "repeat"
    assert block["route"] == "-"
    assert [iteration["iteration"] for iteration in block["iterations"]] == [1, 2, 3, 4]
    assert block["result"]["exit_reason"] == "count"
    assert block["result"]["iterations_run"] == 4


def test_a_repeat_bounded_only_by_duration_still_runs_one_whole_iteration(tmp_path: Path) -> None:
    # The bound is asked between iterations, so a cycle is never cut in half,
    # and a duration already spent when the block is reached does not turn the
    # block into a step that runs nothing.
    config = load_config(str(write_config(tmp_path)))
    path = repeat_plan(
        tmp_path,
        "  - action: repeat\n    duration_s: 0.001\n    steps:\n      - {device: dut, action: delay, duration_ms: 50}\n      - {device: dut, action: reset}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    block = result["steps"][0]
    assert block["result"]["iterations_run"] == 1
    assert block["result"]["exit_reason"] == "duration_s"
    assert [step["action"] for step in block["iterations"][0]["steps"]] == ["delay", "reset"]


def test_a_repeat_bounded_by_duration_exits_green_between_iterations(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    path = repeat_plan(
        tmp_path,
        "  - action: repeat\n    duration_s: 0.25\n    steps:\n      - {device: dut, action: delay, duration_ms: 50}\n      - {device: dut, action: reset}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    block = result["steps"][0]
    assert block["result"]["exit_reason"] == "duration_s"
    assert block["result"]["iterations_run"] >= 2
    assert block["result"]["elapsed_s"] >= 0.25
    # Every iteration that started also finished: the loop ends between cycles,
    # never inside one.
    assert all([step["action"] for step in iteration["steps"]] == ["delay", "reset"] for iteration in block["iterations"])
    assert sum(1 for name, _ in service.calls if name == "reset_target") == block["result"]["iterations_run"]


def test_a_repeat_with_both_bounds_ends_on_the_count_when_the_count_is_reached_first(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    path = repeat_plan(tmp_path, "  - {action: repeat, count: 3, duration_s: 3600, steps: [{device: dut, action: reset}]}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][0]["result"]["exit_reason"] == "count"
    assert result["steps"][0]["result"]["iterations_run"] == 3
    assert sum(1 for name, _ in service.calls if name == "reset_target") == 3


def test_a_repeat_with_both_bounds_ends_on_the_duration_when_the_duration_is_reached_first(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    path = repeat_plan(
        tmp_path,
        "  - action: repeat\n    count: 10000\n    duration_s: 0.001\n    steps:\n      - {device: dut, action: delay, duration_ms: 50}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][0]["result"]["exit_reason"] == "duration_s"
    assert result["steps"][0]["result"]["iterations_run"] == 1


def test_a_failing_step_inside_a_repeat_aborts_the_run_naming_the_iteration(tmp_path: Path) -> None:
    # A failure inside a loop is a failed run like any other: it stops there,
    # cleanup closes what the plan opened, recovery runs after the result
    # exists, and the record says which iteration and which step of the subset
    # it stopped at so the failure can be found without replaying the plan.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - {device: dut_uart, action: uart_open}
  - action: repeat
    count: 5
    steps:
      - {device: dut, action: delay, duration_ms: 1}
      - {device: dut, action: reset}
""",
    )
    service = ResetFailsOnAttempt(3)

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    # The top-level step the run stopped at is the block, and what went wrong
    # there is the inner step's own error.
    assert result["failed_step"] == 2
    assert result["step_error_type"] == "reset_unconfirmed"
    assert result["error_type"] == "reset_unconfirmed"
    block = result["steps"][1]
    assert block["result"]["ok"] is False
    assert block["result"]["error_type"] == "reset_unconfirmed"
    assert block["result"]["exit_reason"] == "step_failed"
    assert block["result"]["failed_iteration"] == 3
    assert block["result"]["failed_nested_step"] == 2
    assert block["result"]["failed_nested_action"] == "reset"
    assert block["result"]["iterations_run"] == 3
    assert len(block["iterations"]) == 3
    assert block["iterations"][2]["steps"][1]["result"]["error_type"] == "reset_unconfirmed"
    # The loop stopped where it failed rather than running its remaining count.
    assert service.resets == 3
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]
    assert service.recovery_calls == [["dut"]]


def test_a_repeat_step_propagates_a_nested_audit_failure_to_the_run(tmp_path: Path) -> None:
    # A run's status is propagated from the results its steps produced, and a
    # block step's results are its iterations'. A walk that stopped at the top
    # level would lose every audit failure a loop's steps reported, which is the
    # one class of failure a green `ok` must never sit beside.
    config = load_config(str(write_config(tmp_path)))
    path = repeat_plan(tmp_path, "  - {action: repeat, count: 2, steps: [{device: dut, action: reset}]}\n")
    service = RecordingService(audit_failure_call="reset_target")

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["audit_ok"] is False
    assert result["audit_error"] == {"error_type": "audit_failed"}
    assert result["retry_safe"] is False
    assert result["steps"][0]["result"]["iterations_run"] == 1


def test_a_port_opened_before_a_repeat_stays_open_across_its_iterations(tmp_path: Path) -> None:
    # The loop opens nothing, closes nothing and resets nothing between
    # iterations: a session the plan holds is still held on the next one.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - {device: dut_uart, action: uart_open}
  - action: repeat
    count: 3
    steps:
      - {device: dut_uart, action: uart_read, comparator: {equals: "cycle done"}, timeout_s: 1}
""",
    )
    service = RecordingService(uart_reads=[b"cycle done\r\n", b"cycle done\r\n", b"cycle done\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert sum(1 for name, _ in service.calls if name == "com_session_start") == 1
    assert sum(1 for name, _ in service.calls if name == "com_read") == 3
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]


def test_a_port_opened_inside_a_repeat_is_still_closed_by_cleanup(tmp_path: Path) -> None:
    # Every open after the first answers `already_active`, because the first one
    # is still in force. A run that re-decided ownership on each open would hand
    # it back on the second iteration and leave the line open behind it.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    path = repeat_plan(
        tmp_path,
        "  - action: repeat\n    count: 2\n    steps:\n      - {device: dut_uart, action: uart_open}\n      - {device: dut, action: reset}\n",
    )
    service = SessionTrackingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert sum(1 for name, _ in service.calls if name == "com_session_start") == 2
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]
    assert sum(1 for name, _ in service.calls if name == "com_session_stop") == 1
    assert service.open_ports == set()


def test_a_repeat_inside_a_repeat_runs_the_inner_block_every_outer_iteration(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - action: repeat
    count: 2
    steps:
      - action: repeat
        count: 3
        steps:
          - {device: dut, action: reset}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert sum(1 for name, _ in service.calls if name == "reset_target") == 6
    outer = result["steps"][0]
    assert outer["result"]["iterations_run"] == 2
    inner_records = [iteration["steps"][0] for iteration in outer["iterations"]]
    assert [record["action"] for record in inner_records] == ["repeat", "repeat"]
    assert [record["result"]["iterations_run"] for record in inner_records] == [3, 3]
    assert all(len(record["iterations"]) == 3 for record in inner_records)


def test_a_run_locks_a_device_a_plan_only_touches_inside_a_repeat(tmp_path: Path) -> None:
    # What the run locks is read off the plan, so a device named only inside a
    # block must be in it: a board reached by a step nobody counted is a board
    # another run can take while this one is using it.
    from agentic_hil.test_reactor import declared_devices

    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    path = repeat_plan(
        tmp_path,
        "  - action: repeat\n    count: 2\n    steps:\n      - {device: dut_uart, action: uart_open}\n",
    )

    devices = declared_devices(config, load_test_config(str(path), str(tmp_path)))

    assert sum(1 for item in devices if item.startswith("com:")) == 1


def test_a_repeat_whose_nested_step_names_no_configured_device_is_refused_before_the_run(tmp_path: Path) -> None:
    # Preflight descends into the block, so a plan that could only fail on its
    # first iteration fails before anything is opened at all.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    path = repeat_plan(
        tmp_path,
        "  - action: repeat\n    count: 2\n    steps:\n      - {device: dut_uart, action: uart_open}\n      - {device: not_configured, action: uart_read, timeout_s: 1}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["steps"] == []
    # The name is refused under the key the kind that would have served the
    # step routes by, and the path says which step inside the block it was.
    assert result["validation_error"]["field"] == "steps[0].steps[1].port_id"
    # The top-level step a refusal reports is still the block, which is what
    # `failed_step` has always meant.
    assert result["validation_error"]["step"] == 1
    assert result["failed_step"] == 1
    assert service.calls == []


# --- read_symbol, plan format version 5 --------------------------------------
#
# The other half of #174. `debug_symbol_value` made a symbol's value readable
# over the tool surface, and the plan format still had no way to ask for one:
# no action that reads a symbol, and no comparator that judges a decoded
# number, every comparator it carried judging text and reading a `range` out of
# a regular expression's capture group.

# The fake GDB's own memory rule: one byte per address, the low byte of the
# address itself. `boot_counter` is the typed four-byte symbol it describes, at
# an address whose top byte makes the two integer readings disagree.
BOOT_COUNTER_ADDRESS = 0x20000080
BOOT_COUNTER_BYTES = bytes((BOOT_COUNTER_ADDRESS + index) & 0xFF for index in range(4))
BOOT_COUNTER_VALUE = int.from_bytes(BOOT_COUNTER_BYTES, "little")


def symbol_plan(tmp_path: Path, step: str, *, version: int = 5) -> Path:
    """A plan that opens a debug session and then runs `step` inside it."""
    return write_test_config(
        tmp_path,
        f"version: {version}\nsteps:\n  - {{device: dut, action: debug_start, image_path: build/app.elf}}\n  - {step}\n",
    )


def symbol_value_result(data: bytes, symbol: str) -> dict:
    """What `debug_symbol_value` answers for one symbol.

    Decoded by the backend's own function rather than by a copy of it here, so a
    double cannot promise a reading the real tool does not give: which widths
    carry an integer, and what the two readings are at those widths, is the
    backend's answer in these tests as well. The double flashes no image, so
    the order is the backend's own stated default, exactly what the real tool
    answers for an image that declares none."""
    return {
        "ok": True,
        "tool": "debug_symbol_value",
        "symbol": symbol,
        "address": hex(BOOT_COUNTER_ADDRESS),
        "size_bytes": len(data),
        "resolved_from": "debug_info",
        **decode_symbol_value(data, {"byte_order": "little", "byte_order_from": "default"}),
    }


class SymbolService(RecordingService):
    """A bench whose symbols all hold `data`."""

    def __init__(self, data: bytes, **kwargs) -> None:
        super().__init__(**kwargs)
        self.data = data

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "debug_symbol_value":
            return symbol_value_result(self.data, str((arguments or {}).get("symbol")))
        return result


def run_symbol_plan(tmp_path: Path, path: Path, service: RecordingService, **config_kwargs) -> dict:
    config = load_config(str(write_config(tmp_path, **config_kwargs)))
    return TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]


def test_read_symbol_asserts_a_value_in_target_memory_end_to_end(tmp_path: Path) -> None:
    # The whole path with no double in it: the reactor's own step, the real
    # OpenOCD backend, a live typed session against the fake GDB, and the value
    # the target answered with judged against what the plan claimed.
    service, _ = reactor_service(tmp_path)
    test_path = write_test_config(
        tmp_path,
        f"""version: 5
name: assert-a-counter
steps:
  - {{device: dut, action: debug_start, image_path: build/app.elf, mode: attach}}
  - {{device: dut, action: read_symbol, symbol: boot_counter, size_bytes: 4, comparator: {{equals: {BOOT_COUNTER_VALUE}}}}}
  - {{device: dut, action: debug_stop}}
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(test_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["debug_start", "read_symbol", "debug_stop"]
    read = result["steps"][1]["result"]
    assert read["summary"] == "The symbol held the expected value."
    assert read["symbol"] == "boot_counter"
    assert read["address"] == hex(BOOT_COUNTER_ADDRESS)
    assert read["size_bytes"] == 4
    # The same resolution the tool uses, which is what makes an assembly-defined
    # object readable here too: the field says which route answered.
    assert read["resolved_from"] == "debug_info"
    assert read["hex"] == BOOT_COUNTER_BYTES.hex()
    assert read["captured_value"] == BOOT_COUNTER_VALUE
    assert read["reading"] == "value_unsigned"


def test_read_symbol_without_a_comparator_records_the_value_unchanged(tmp_path: Path) -> None:
    # The plain read every feedback action has: no claim, and the tool's own
    # answer in the report, so a plan can record a value it does not assert.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter}")
    service = SymbolService(b"\x2a\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is True, result
    read = result["steps"][1]["result"]
    assert read["tool"] == "debug_symbol_value"
    assert read["hex"] == "2a000000"
    assert read["value_unsigned"] == 42
    assert ("debug_symbol_value", {"symbol": "boot_counter"}) in service.calls


def test_a_symbol_comparator_that_goes_unmet_fails_the_step_and_ends_the_run(tmp_path: Path) -> None:
    # Abort-on-failure, unchanged: the step after it never runs, and the session
    # the plan opened is closed by the run's own cleanup rather than by a step
    # that was never reached.
    path = write_test_config(
        tmp_path,
        """version: 5
steps:
  - {device: dut, action: debug_start, image_path: build/app.elf}
  - {device: dut, action: read_symbol, symbol: boot_counter, comparator: {equals: 42}}
  - {device: dut, action: read_symbol, symbol: boot_counter}
""",
    )
    service = SymbolService(b"\x07\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "comparator_unmet"
    read = result["steps"][1]["result"]
    assert read["summary"] == "The symbol did not hold the expected value."
    assert read["comparator"] == {"equals": 42}
    # What it did read, so a red step is readable without reconnecting to the
    # board: the number that was judged, and the bytes it was decoded from.
    assert read["captured_value"] == 7
    assert read["hex"] == "07000000"
    assert [step["action"] for step in result["steps"]] == ["debug_start", "read_symbol"]
    assert [record["action"] for record in result["cleanup"]] == ["debug_stop"]


@pytest.mark.parametrize(("value", "met"), [(20, True), (25, True), (30, True), (19, False), (31, False)], ids=str)
def test_a_symbol_range_holds_the_value_to_inclusive_bounds(tmp_path: Path, value: int, met: bool) -> None:
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter, comparator: {range: {min: 20, max: 30}}}")
    service = SymbolService(value.to_bytes(4, "little"))

    result = run_symbol_plan(tmp_path, path, service)

    read = result["steps"][1]["result"]
    assert read["ok"] is met, read
    assert read["captured_value"] == value
    assert read["summary"] == ("The symbol's value fell inside the expected range." if met else "The symbol's value fell outside the expected range.")


@pytest.mark.parametrize(("data", "met"), [(b"\xf5\x00\x00\x00", True), (b"\xf6\x00\x00\x00", False)], ids=["bits_match", "bits_differ"])
def test_a_symbol_mask_judges_the_bits_it_names(tmp_path: Path, data: bytes, met: bool) -> None:
    # One flag of a status word, asserted without stating every other bit of it.
    # The report carries the whole word beside the bits, so a red step says both
    # what was read and what was compared. `0x0f` is a YAML number like any
    # other, which the echoed comparator shows.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: status_word, comparator: {mask: 0x0f, equals: 5}}")
    service = SymbolService(data)

    result = run_symbol_plan(tmp_path, path, service)

    read = result["steps"][1]["result"]
    assert read["ok"] is met, read
    assert read["comparator"] == {"mask": 15, "equals": 5}
    assert read["captured_value"] == data[0]
    assert read["masked_value"] == data[0] & 0x0F
    assert read["summary"] == ("The symbol's masked bits held the expected value." if met else "The symbol's masked bits did not hold the expected value.")


@pytest.mark.parametrize(
    ("claim", "met", "reading"),
    [("{equals: -3, signed: true}", True, "value_signed"), ("{equals: -3}", False, "value_unsigned")],
    ids=["signed", "unsigned_by_default"],
)
def test_signed_picks_the_reading_the_claim_is_about(tmp_path: Path, claim: str, met: bool, reading: str) -> None:
    # The same four bytes are honestly both readings and above the top bit the
    # two disagree, which is why the tool returns both. A plan asserting a
    # counter that went negative says so here rather than writing the two's
    # complement of the number it means.
    path = symbol_plan(tmp_path, f"{{device: dut, action: read_symbol, symbol: drift, comparator: {claim}}}")
    service = SymbolService((-3).to_bytes(4, "little", signed=True))

    result = run_symbol_plan(tmp_path, path, service)

    read = result["steps"][1]["result"]
    assert read["ok"] is met, read
    assert read["reading"] == reading
    assert read["value_signed"] == -3
    assert read["value_unsigned"] == 2**32 - 3
    assert read["captured_value"] == (-3 if met else 2**32 - 3)


def test_read_symbol_needs_the_debug_session_the_plan_opened(tmp_path: Path) -> None:
    # The lifecycle every other typed debug action is held to: the session is
    # the plan's own, and a step outside one is refused before the run.
    path = write_test_config(tmp_path, "version: 5\nsteps:\n  - {device: dut, action: read_symbol, symbol: boot_counter}\n")
    service = SymbolService(b"\x2a\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is False
    assert result["validation_error"]["summary"] == "A debug session must be started before this action."
    assert service.calls == []


def test_read_symbol_is_held_to_the_symbol_allowlist(tmp_path: Path) -> None:
    # The operator's statement about what may be read off this bench, and a new
    # way of reading it is not a way around it. Refused before the run, so the
    # board is never asked.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter}")
    service = SymbolService(b"\x2a\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service, allowed_symbols=["CTC_array"])

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[1].symbol"
    assert result["validation_error"]["summary"] == "Symbol is not allowed by the authoritative debug config."
    assert service.calls == []


def test_a_declared_width_with_no_integer_reading_is_refused_before_the_run(tmp_path: Path) -> None:
    # The decoded integer exists at four widths, so a numeric claim on a 3-byte
    # object is a claim the format cannot make. The plan stated the width, so it
    # is answered at plan time rather than an hour into a run.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: rgb, size_bytes: 3, comparator: {equals: 1}}")
    service = SymbolService(b"\x01\x00\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is False
    assert result["steps"] == []
    assert result["validation_error"]["field"] == "steps[1].size_bytes"
    assert result["validation_error"]["summary"] == "A comparator judges the symbol's decoded integer, which exists only at 1, 2, 4 and 8 bytes."
    assert result["validation_error"]["integer_widths"] == [1, 2, 4, 8]
    assert service.calls == []


def test_a_declared_width_above_the_benchs_ceiling_is_refused_before_the_run(tmp_path: Path) -> None:
    # `debug.max_dump_size_bytes` bounds what may be read at all, comparator or
    # not, and a width the plan states is one the ceiling can be applied to here
    # instead of after the session is open.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: CTC_array, size_bytes: 408}")
    service = SymbolService(bytes(408))

    result = run_symbol_plan(tmp_path, path, service, max_dump_size_bytes=16)

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[1].size_bytes"
    assert result["validation_error"]["summary"] == "Symbol read exceeds debug.max_dump_size_bytes."
    assert result["validation_error"]["max_dump_size_bytes"] == 16
    assert service.calls == []


def test_a_width_with_no_integer_reading_is_refused_at_runtime_when_the_plan_declared_none(tmp_path: Path) -> None:
    # The other half of the width rule. Preflight builds nothing and reaches no
    # board, so a symbol whose size only the image describes is judged here,
    # with a stated reason and the bytes it did read rather than a green step or
    # an invented number.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: CTC_array, comparator: {equals: 1}}")
    service = SymbolService(bytes(408))

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "symbol_width_not_numeric"
    assert read["summary"] == "The symbol's width carries no integer reading, so this step's comparator could not be judged."
    assert read["size_bytes"] == 408
    assert read["integer_widths"] == [1, 2, 4, 8]
    assert read["hex"] == bytes(408).hex()
    assert read["value_not_decoded"].startswith("size_bytes is not 1, 2, 4 or 8")
    assert "captured_value" not in read


def test_a_declared_width_the_target_does_not_have_fails_the_step(tmp_path: Path) -> None:
    # A firmware that turned a `uint32_t` into a `uint16_t` is a test whose value
    # means something else now, so a stated width is asserted rather than
    # assumed, and the value is not judged at all.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter, size_bytes: 4, comparator: {equals: 7}}")
    service = SymbolService(b"\x07\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is False
    read = result["steps"][1]["result"]
    assert read["error_type"] == "symbol_size_mismatch"
    assert read["summary"] == "The symbol is not the width this step declared."
    assert read["expected_size_bytes"] == 4
    assert read["size_bytes"] == 2
    assert "captured_value" not in read


def test_a_plain_read_may_name_any_width_the_bench_allows(tmp_path: Path) -> None:
    # A width without a claim is not a claim about a number: reading a structure
    # and recording its bytes is exactly what a plain `read_symbol` is for.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: CTC_array, size_bytes: 408}")
    service = SymbolService(bytes(408))

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["hex"] == bytes(408).hex()


def test_read_symbol_takes_no_timeout_because_it_does_not_wait(tmp_path: Path) -> None:
    # A halted target's memory does not change under the step, so there is no
    # deadline to give it. The key is refused rather than accepted and ignored.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter, timeout_s: 5}")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[1].timeout_s"


@pytest.mark.parametrize(
    ("comparator", "field"),
    [
        ("{}", "steps[1].comparator"),
        ("{equals: 1, range: {min: 0, max: 2}}", "steps[1].comparator"),
        ("{mask: 15}", "steps[1].comparator"),
        ("{mask: 15, equals: 1, signed: true}", "steps[1].comparator"),
        ("{equals: 1, pattern: abc}", "steps[1].comparator.pattern"),
    ],
    ids=["neither", "both", "mask_without_equals", "mask_with_signed", "text_vocabulary"],
)
def test_a_symbol_comparator_the_format_cannot_mean_is_refused_at_load(tmp_path: Path, comparator: str, field: str) -> None:
    # One claim per comparator, `mask` only as a claim about a value, and no sign
    # beside a mask because a mask selects bits while a sign reads the whole
    # width. The text vocabulary is not this comparator's at all.
    path = symbol_plan(tmp_path, f"{{device: dut, action: read_symbol, symbol: boot_counter, comparator: {comparator}}}")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == field


def test_a_version_4_plan_cannot_reach_for_read_symbol(tmp_path: Path) -> None:
    # The version a plan declares is what tells another install whether it can
    # run it, so a v4 plan using the v5 action is refused by name rather than
    # working here and failing on an older install for no stated reason.
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter}", version=4)

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[1].action"
    assert refused.value.details["value"] == "read_symbol"
    assert refused.value.details["plan_version"] == 4
    assert refused.value.details["requires_plan_version"] == 5
    assert "version: 5" in refused.value.details["migration"]


def test_a_version_4_plan_loads_and_behaves_exactly_as_it_did(tmp_path: Path) -> None:
    # The format grew a version; a plan written against the old one is held to
    # the old one and runs unchanged, block step, comparator and all.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - {device: dut_uart, action: uart_open}
  - action: repeat
    count: 2
    steps:
      - {device: dut_uart, action: uart_read, comparator: {equals: "cycle done"}, timeout_s: 5}
""",
    )
    service = RecordingService(uart_reads=[b"cycle done\r\n", b"cycle done\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["iterations_run"] == 2
    assert [record["action"] for record in result["cleanup"]] == ["uart_close"]


# --- a plan carries the reads where the backend serves them with no session ---
#
# The reads are `dump_memory` and `read_symbol`, and on a backend that answers
# them standalone (ST-Link since #342) a plan needs no `debug_start` before
# them: there is no session on that bench for the step to be inside. Which
# backends those are is never a list here; it is what the backend implements,
# read through `configured_sessionless_debug_reads`. Inside a session nothing
# below changes, and the tests that pin that say so.


SESSIONLESS_READ_PLAN = """version: 5
name: read-without-a-session
steps:
  - {device: dut, action: read_symbol, symbol: boot_counter, size_bytes: 4, comparator: {equals: 42}}
  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
"""


class SessionlessReadService(SymbolService):
    """A bench whose two symbol reads both answer, with nothing opened first.

    The dump answers `ok` here where `RecordingService` fails it, because this
    double stands in for a backend that serves the read standalone: a plan that
    was admitted and then failed on the double's own refusal would prove nothing
    about the gate under test."""

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "debug_dump_symbol_ihex":
            return {
                "ok": True,
                "tool": name,
                "symbol": str((arguments or {}).get("symbol")),
                "size_bytes": len(self.data),
                "output": {"path": str((arguments or {}).get("output_path"))},
                "summary": "Symbol memory dumped as Intel HEX.",
            }
        return result


def test_a_plan_reads_without_a_debug_start_where_the_backend_serves_it(tmp_path: Path) -> None:
    # The workflow #342 opened and #345 finishes: dump a RAM-resident
    # measurement on an ST-Link bench, as a plan step rather than only by hand.
    # There is no `debug_start` in the plan and none is inserted; both steps go
    # straight to the reads, which is what the backend serves.
    path = write_test_config(tmp_path, SESSIONLESS_READ_PLAN)
    service = SessionlessReadService((42).to_bytes(4, "little"))

    result = run_symbol_plan(tmp_path, path, service, debugger_type="stlink")

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["read_symbol", "dump_memory"]
    assert [name for name, _ in service.calls] == ["debug_symbol_value", "debug_dump_symbol_ihex"]
    assert result["steps"][0]["result"]["summary"] == "The symbol held the expected value."
    assert result["steps"][0]["result"]["captured_value"] == 42
    # Nothing was opened, so there is nothing for cleanup to close.
    assert result["cleanup"] == []


def test_a_read_step_is_still_held_to_the_symbol_allowlist_without_a_session(tmp_path: Path) -> None:
    # The operator's statement about what may be read off this bench does not
    # depend on how the read reaches the board. Refused before the run, so the
    # probe is never opened.
    path = write_test_config(tmp_path, SESSIONLESS_READ_PLAN)
    service = SessionlessReadService((42).to_bytes(4, "little"))

    result = run_symbol_plan(tmp_path, path, service, debugger_type="stlink", allowed_symbols=["CTC_array"])

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].symbol"
    assert result["validation_error"]["summary"] == "Symbol is not allowed by the authoritative debug config."
    assert service.calls == []


def test_a_read_step_on_a_backend_that_serves_it_neither_way_is_refused_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The refusal for a backend that serves the read neither way: it names the
    # backend, the step and its number, says which reads it does serve
    # standalone (none), and ends at the configuration change that would run
    # it. Every shipped backend now serves the reads one way or the other, so
    # the shape is pinned through a pyocd stripped of the ability, which is
    # also what a future partial backend looks like.
    from agentic_hil.backends.pyocd import PyOCDBackend

    monkeypatch.setattr(PyOCDBackend, "sessionless_debug_tools", lambda self: frozenset())
    path = write_test_config(tmp_path, SESSIONLESS_READ_PLAN)
    service = SessionlessReadService((42).to_bytes(4, "little"))

    result = run_symbol_plan(tmp_path, path, service, debugger_type="pyocd")

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    refusal = result["validation_error"]
    assert refusal["step"] == 1
    assert refusal["field"] == "steps[0].action"
    assert refusal["action"] == "read_symbol"
    assert refusal["debugger"] == "dut"
    assert refusal["debugger_type"] == "pyocd"
    assert refusal["sessionless_debug_reads"] == []
    assert refusal["summary"] == (
        "Step 1's 'read_symbol' reads target memory, and the 'pyocd' backend on debugger 'dut' serves that read "
        "neither without a debug session nor inside one. To run it on this bench, the same probe runs under "
        "`type: openocd` with the `interface_cfg` for it (`interface/stlink.cfg` for an ST-Link, "
        "`interface/cmsis-dap.cfg` for a CMSIS-DAP probe) and the `target_cfg` for this part."
    )
    assert service.calls == []


def test_a_session_step_on_a_backend_that_opens_none_is_refused_by_name(tmp_path: Path) -> None:
    # The other half of the same gate, on the same bench that now runs reads: a
    # session is what ST-Link has not got, and the refusal says so about the
    # session rather than about "typed debug actions", which no longer names one
    # thing.
    path = write_test_config(
        tmp_path,
        "version: 5\nsteps:\n  - {device: dut, action: debug_start, image_path: build/app.elf}\n",
    )
    (tmp_path / "build").mkdir(parents=True, exist_ok=True)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = SessionlessReadService(b"\x00\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service, debugger_type="stlink")

    assert result["ok"] is False
    refusal = result["validation_error"]
    assert refusal["action"] == "debug_start"
    assert refusal["debugger_type"] == "stlink"
    # The reads this backend does serve are named beside the refusal, because a
    # plan author reading it is one step away from a plan that runs here.
    assert refusal["sessionless_debug_reads"] == ["debug_dump_symbol_ihex", "debug_symbol_value"]
    assert refusal["summary"].startswith(
        "Step 1's 'debug_start' needs a typed debug session, and debugger 'dut' runs the 'stlink' backend, "
        "which opens none."
    )
    assert service.calls == []


def test_the_in_session_openocd_path_is_unchanged_by_the_sessionless_route(tmp_path: Path) -> None:
    # The pin: on the backend that has a session, both reads still run inside the
    # one the plan opened, in the order the plan wrote them, on that session's
    # lease. Nothing about the sessionless route reaches this bench.
    path = write_test_config(
        tmp_path,
        """version: 5
steps:
  - {device: dut, action: debug_start, image_path: build/app.elf}
  - {device: dut, action: read_symbol, symbol: boot_counter, size_bytes: 4, comparator: {equals: 42}}
  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
  - {device: dut, action: debug_stop}
""",
    )
    (tmp_path / "build").mkdir(parents=True, exist_ok=True)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = SessionlessReadService((42).to_bytes(4, "little"))

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is True, result
    assert [name for name, _ in service.calls] == [
        "debug_start_session",
        "debug_symbol_value",
        "debug_dump_symbol_ihex",
        "debug_stop_session",
    ]


def test_a_dump_memory_step_without_a_session_on_openocd_is_still_refused(tmp_path: Path) -> None:
    # The lifecycle rule for the backend that has a session is untouched: a read
    # outside one is refused before the run, with the sentence it has always
    # been refused with, because there `debug_start` is what would fix it.
    path = write_test_config(
        tmp_path,
        "version: 5\nsteps:\n  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}\n",
    )
    service = SessionlessReadService(b"\x00\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is False
    assert result["validation_error"]["summary"] == "A debug session must be started before this action."
    assert service.calls == []


def test_a_backend_that_gains_sessionless_reads_admits_the_plan_with_no_reactor_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The point of deriving the capability instead of listing types: this is the
    # OpenOCD bench of the test above, whose backend now names the two reads as
    # ones it serves standalone. The same plan that is refused there is admitted
    # here, and nothing in the reactor was told about it.
    from agentic_hil.backends.openocd import OpenOCDBackend

    monkeypatch.setattr(
        OpenOCDBackend,
        "sessionless_debug_tools",
        lambda self: frozenset({"debug_symbol_value", "debug_dump_symbol_ihex"}),
    )
    path = write_test_config(
        tmp_path,
        "version: 5\nsteps:\n  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}\n",
    )
    service = SessionlessReadService(b"\x00\x00\x00\x00")

    result = run_symbol_plan(tmp_path, path, service)

    assert result["ok"] is True, result
    assert [name for name, _ in service.calls] == ["debug_dump_symbol_ihex"]


def test_a_read_step_is_judged_by_the_backend_of_the_probe_it_names(tmp_path: Path) -> None:
    # Multi-probe benches ask the same question per probe: the bound probe is
    # OpenOCD, which would need a session for this read, and the probe the step
    # names is an ST-Link, which does not. The step names it, so the ST-Link's
    # answer is the one that decides, and the read runs on that probe's own
    # service.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: stlink\n    resource_id: rb\n",
            )
        )
    )
    path = write_test_config(
        tmp_path,
        "version: 5\nsteps:\n  - {device: probe_b, action: read_symbol, symbol: boot_counter}\n",
    )

    class ClosableService(SessionlessReadService):
        def close(self) -> None:
            pass

    base = SessionlessReadService((42).to_bytes(4, "little"))
    built: list[ClosableService] = []

    def factory(bound_config) -> ClosableService:
        service = ClosableService((42).to_bytes(4, "little"))
        built.append(service)
        return service

    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        result = reactor.run(load_test_config(str(path), str(tmp_path)))
    finally:
        reactor.close()

    assert result["ok"] is True, result
    assert base.calls == []
    assert [name for name, _ in built[0].calls] == ["debug_symbol_value"]


# --- a sessionless read needs its allow_probe checked at preflight, like a session --
#
# `debug_start` is refused before the run on a version-1 bench that withholds
# allow_probe, so the whole plan is rejected before it touches the board. A
# sessionless read still opens a probe onto a live core and needs the same
# grant, and until it was checked here the reactor admitted the plan and let the
# backend refuse the read at its own turn, after any earlier effectful step had
# already run. The gate is `probe_allowed()`, so a read-free (version 2) bench,
# which grants reads by exclusivity and carries no allow_probe to set, is still
# admitted.


def test_a_sessionless_read_after_flash_is_refused_before_the_flash_runs(tmp_path: Path) -> None:
    # A flash ahead of a read the config plainly forbids: the permission is known
    # before the run, so the whole plan is refused at preflight and the flash
    # never lands. Were the read admitted, the flash would have mutated the board
    # before the read's own permission_denied arrived, the hardware-touching a
    # refusable plan must never reach.
    path = write_test_config(
        tmp_path,
        """version: 5
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut, action: read_symbol, symbol: boot_counter}
""",
    )
    service = SessionlessReadService((42).to_bytes(4, "little"))

    result = run_symbol_plan(
        tmp_path,
        path,
        service,
        debugger_type="stlink",
        permissions={**DEFAULT_TEST_PERMISSIONS, "allow_probe": False},
    )

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 2
    refusal = result["validation_error"]
    assert refusal["field"] == "steps[1].action"
    assert refusal["action"] == "read_symbol"
    assert refusal["summary"] == "Reading target memory requires allow_probe on this debugger."
    assert refusal["permission"] == "allow_probe"
    # No step ran: the flash the plan led with never reached the service.
    assert result["steps"] == []
    assert service.calls == []


def test_a_sessionless_dump_after_flash_is_refused_before_the_flash_runs(tmp_path: Path) -> None:
    # The dump half of the same gate. It is refused ahead of the output-path
    # validation the dump would otherwise run, so a denied plan makes no service
    # call of any kind.
    path = write_test_config(
        tmp_path,
        """version: 5
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
""",
    )
    service = SessionlessReadService(b"\x00" * 8)

    result = run_symbol_plan(
        tmp_path,
        path,
        service,
        debugger_type="stlink",
        permissions={**DEFAULT_TEST_PERMISSIONS, "allow_probe": False},
    )

    assert result["ok"] is False
    assert result["failed_step"] == 2
    refusal = result["validation_error"]
    assert refusal["field"] == "steps[1].action"
    assert refusal["action"] == "dump_memory"
    assert refusal["summary"] == "Reading target memory requires allow_probe on this debugger."
    assert refusal["permission"] == "allow_probe"
    assert result["steps"] == []
    assert service.calls == []


def test_a_read_free_bench_admits_a_sessionless_read_with_no_allow_probe(tmp_path: Path) -> None:
    # The other side of the gate: a version-2 bench grants reads by exclusivity
    # and never names allow_probe, so `probe_allowed()`, not the raw flag, is
    # what keeps the read admitted here.
    path = write_test_config(
        tmp_path,
        "version: 5\nsteps:\n  - {device: dut, action: read_symbol, symbol: boot_counter, size_bytes: 4, comparator: {equals: 42}}\n",
    )
    service = SessionlessReadService((42).to_bytes(4, "little"))

    result = run_symbol_plan(tmp_path, path, service, debugger_type="stlink", config_version=2)

    assert result["ok"] is True, result
    assert [name for name, _ in service.calls] == ["debug_symbol_value"]
