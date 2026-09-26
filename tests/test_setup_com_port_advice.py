"""What `init` and `setup` tell a person about serial ports and the probe count (#549).

Both commands end their `config` step with advice, and on a stock Ubuntu 24.04
with one Nucleo attached that advice went wrong twice. The COM-port item asked
the reader to add the DUT UART under `com_ports` while the file the same run had
just written already bound `com_ports.dut_uart` to the debugger's
`/dev/serial/by-id/` path, and the five ports it named were the first five of
the host's 32 legacy `/dev/ttyS*` entries, so the one port that mattered was
counted off under "and 28 more". And the paragraph saying the probe was bound
off an inventory that cannot see a probe without a virtual COM port was printed
twice, as the step's headline and again as item 1 under it.

The inventory is the recorded one in
`fixtures/com_ports_ubuntu_24_04_recording.json`, and the documents rendered
are the ones the commands build on it.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_OPENOCD
from support import trusted_launcher

from agentic_hil import cli
from agentic_hil.adopt import PROJECT_CONFIG_ADOPT, _release_refusal
from agentic_hil.bootstrap import PROJECT_PROFILE, discover_attached_hardware
from agentic_hil.comports import list_available_com_ports
from agentic_hil.config import load_authoritative_config
from agentic_hil.humanize import render_result
from agentic_hil.types import JsonObject, com_port_is_unbound

RECORDING_PATH = Path(__file__).resolve().parent / "fixtures" / "com_ports_ubuntu_24_04_recording.json"
RECORDED_INVENTORY: JsonObject = json.loads(RECORDING_PATH.read_text(encoding="utf-8"))["recording"]

# The debugger's virtual COM port in that recording: the name the host gives it,
# and the stable path `init` binds `com_ports.dut_uart` to.
KERNEL_NAME = "/dev/ttyACM0"
BY_ID_PATH = "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066BFF505050505050505050-if02"

# The starter project's profile: it names the board and declares `dut_uart`, so
# the discovery below needs to say nothing to the board.
STARTER_PROFILE: JsonObject = {
    "target": {"name": "nucleo-f446re-starter", "controller": "stm32f446ret6"},
    "debuggers": {"dut": {"timeout_s": 60, "permissions": {}}},
    "com_ports": {"dut_uart": {"baudrate": 115200, "permissions": {}}},
}


def _flat(text: str) -> str:
    """The text with the wrapper's line breaks taken back out, so a sentence is
    counted and found at any terminal width."""
    return " ".join(text.split())


def _starter_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace


def _the_recorded_host(monkeypatch: pytest.MonkeyPatch, *, openocd: bool = True) -> None:
    """The host the recording was taken on, as these commands read it.

    Three readings are replaced and nothing else: the toolchain (OpenOCD on PATH
    and no STM32CubeProgrammer, which the suite already hides), the serial
    inventory, which discovery and the COM-port advice both read, and a spawn
    that must not happen, because the profile names the controller and a probe
    identified off the USB inventory is not talked to. With `openocd=False` the
    host has no toolchain at all: the placeholder path, with the board still
    plugged in."""

    def inventory(tool: str = "com_ports_available") -> JsonObject:
        return {**copy.deepcopy(RECORDED_INVENTORY), "tool": tool}

    def nothing_spawned(command: list[str], cwd: str, timeout_s: float) -> object:
        raise AssertionError(f"nothing should have been spawned: {command}")

    monkeypatch.setattr(
        "agentic_hil.bootstrap.find_openocd", (lambda: str(FAKE_OPENOCD)) if openocd else (lambda: None)
    )
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", nothing_spawned)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", inventory)
    monkeypatch.setattr("agentic_hil.cli.list_available_com_ports", inventory)


def _setup_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user-wide half of `setup`, installed into the sandboxed profile."""
    command = str(trusted_launcher())
    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", lambda: command)
    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", list)
    real_which = shutil.which
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: None if name == "claude" else real_which(name))


# ---------------------------------------------------------------------------
# The COM-port item.


def test_the_dut_uart_the_run_bound_is_confirmed_rather_than_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 10 of the reported run asked for work the same run had done.

    The file `init` writes on this host binds `com_ports.dut_uart` to the
    debugger's by-id path, so the advice says that, by entry and by device, and
    then names the other ports it saw. The debugger's port is the bound one even
    though the host lists it as `/dev/ttyACM0`: its `stable_device` is the bound
    path, so it is not named again among the others, and the others are the 32
    legacy ports, five of them named in the host's order and the rest counted."""
    workspace = _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch)

    result = cli.init_config()

    assert result["ok"] is True, result
    # What the file binds, read back rather than assumed: the advice is about it.
    assert load_authoritative_config(workspace).com_ports["dut_uart"].device == BY_ID_PATH
    next_steps = result["next_steps"]
    confirming = [step for step in next_steps if "com_ports.dut_uart" in step]
    assert len(confirming) == 1, next_steps
    assert f"com_ports.dut_uart is bound to {BY_ID_PATH}" in confirming[0], confirming[0]
    assert not any("Add the DUT UART" in step for step in next_steps), next_steps
    assert KERNEL_NAME not in confirming[0], confirming[0]
    others = "/dev/ttyS31, /dev/ttyS30, /dev/ttyS29, /dev/ttyS28, /dev/ttyS27, and 27 more"
    assert others in confirming[0], confirming[0]


def test_with_nothing_bound_the_usb_port_is_named_first_and_the_rest_are_counted(tmp_path: Path) -> None:
    """The five ports the reported run named were five of the legacy ones.

    With no COM port bound the advice still asks for the DUT UART, and the
    sample it names is taken with the ports that carry a USB identity first, so
    on the recorded host the debugger's virtual COM port leads. The legacy ports
    stay in the sample and in the count, in the host's own order: on some hosts
    one of them is a real UART."""
    steps = cli.init_next_steps(copy.deepcopy(RECORDED_INVENTORY), tmp_path / "config.yaml")

    asking = [step for step in steps if step.startswith("Detected COM ports: ")]
    assert len(asking) == 1, steps
    assert asking[0].startswith(
        f"Detected COM ports: {KERNEL_NAME}, /dev/ttyS31, /dev/ttyS30, /dev/ttyS29, /dev/ttyS28, and 28 more."
    ), asking[0]
    assert "Add the DUT UART under com_ports" in asking[0], asking[0]


def test_a_declared_port_with_no_device_is_asked_for_with_the_usb_port_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The placeholder on the same host, with the board plugged in.

    No toolchain was found, so nothing was bound: the profile's `dut_uart` is
    written as an entry with no device, which declares a port and binds none.
    The advice asks for it, and names the debugger's port first."""
    workspace = _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch, openocd=False)

    result = cli.init_config()

    assert result["ok"] is True, result
    assert com_port_is_unbound(load_authoritative_config(workspace).com_ports["dut_uart"])
    next_steps = result["next_steps"]
    assert not any("is bound to" in step for step in next_steps), next_steps
    asking = [step for step in next_steps if step.startswith("Detected COM ports: ")]
    assert len(asking) == 1, next_steps
    assert asking[0].startswith(
        f"Detected COM ports: {KERNEL_NAME}, /dev/ttyS31, /dev/ttyS30, /dev/ttyS29, /dev/ttyS28, and 28 more."
    ), asking[0]
    assert "Add the DUT UART under com_ports" in asking[0], asking[0]


# ---------------------------------------------------------------------------
# The bound port at its edges, driven through the entries the file binds.


def _confirming_item(steps: list[str]) -> str:
    """The one next step that says what the file binds, which then asks for nothing."""
    confirming = [step for step in steps if "is bound to" in step]
    assert len(confirming) == 1, steps
    assert not any("Add the DUT UART" in step for step in steps), steps
    return confirming[0]


def test_a_port_bound_by_its_kernel_name_is_confirmed_and_left_out_of_the_others(tmp_path: Path) -> None:
    """The debugger's port bound by the name the host lists it under rather
    than by its stable path: the same port, and the same answer."""
    steps = cli.init_next_steps(
        copy.deepcopy(RECORDED_INVENTORY), tmp_path / "config.yaml", bound_com_ports={"dut_uart": KERNEL_NAME}
    )

    assert _confirming_item(steps) == (
        f"com_ports.dut_uart is bound to {KERNEL_NAME}. "
        "Other COM ports detected: /dev/ttyS31, /dev/ttyS30, /dev/ttyS29, /dev/ttyS28, /dev/ttyS27, and 27 more."
    )


def test_two_bound_entries_are_confirmed_one_clause_each_and_both_left_out(tmp_path: Path) -> None:
    """A second entry bound to one of the legacy ports, which on some hosts is
    a real UART: each entry is named with its device, and neither port is
    counted among the others."""
    steps = cli.init_next_steps(
        copy.deepcopy(RECORDED_INVENTORY),
        tmp_path / "config.yaml",
        bound_com_ports={"dut_uart": BY_ID_PATH, "console": "/dev/ttyS0"},
    )

    assert _confirming_item(steps) == (
        f"com_ports.dut_uart is bound to {BY_ID_PATH}. com_ports.console is bound to /dev/ttyS0. "
        "Other COM ports detected: /dev/ttyS31, /dev/ttyS30, /dev/ttyS29, /dev/ttyS28, /dev/ttyS27, and 26 more."
    )


def test_a_bound_device_the_host_does_not_list_is_confirmed_without_claiming_it_is_there(tmp_path: Path) -> None:
    """The file names the device and the host does not list it.

    The entry is confirmed, because the file binds it, and nothing says the
    device is attached: every port the host lists is named or counted, none is
    set aside as the bound one, and none is called other than it."""
    steps = cli.init_next_steps(
        copy.deepcopy(RECORDED_INVENTORY), tmp_path / "config.yaml", bound_com_ports={"dut_uart": "/dev/ttyUSB0"}
    )

    assert _confirming_item(steps) == (
        "com_ports.dut_uart is bound to /dev/ttyUSB0. "
        f"Detected COM ports: {KERNEL_NAME}, /dev/ttyS31, /dev/ttyS30, /dev/ttyS29, /dev/ttyS28, and 28 more."
    )


def test_a_failed_or_empty_inventory_is_still_said_beside_the_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound entry does not stand in for what the host said about its ports.

    The failure is the product's own document for a pyserial it cannot
    import; the empty inventory is the recording with its ports taken out."""
    monkeypatch.setitem(sys.modules, "serial.tools", None)
    # What the import system raises for the listing's own import, which the
    # item names (#568).
    with pytest.raises(ImportError) as raised:
        __import__("serial.tools", fromlist=["list_ports"])
    failed = list_available_com_ports()
    assert failed["ok"] is False, failed
    empty = {**copy.deepcopy(RECORDED_INVENTORY), "ports": [], "summary": "0 available COM port(s)."}
    bound = {"dut_uart": BY_ID_PATH}

    assert _confirming_item(cli.init_next_steps(failed, tmp_path / "config.yaml", bound_com_ports=bound)) == (
        f"com_ports.dut_uart is bound to {BY_ID_PATH}. "
        f"COM port discovery failed: {type(raised.value).__name__}: {raised.value}. "
        "Run: agentic-hil com-ports after checking the pyserial installation."
    )
    assert _confirming_item(cli.init_next_steps(empty, tmp_path / "config.yaml", bound_com_ports=bound)) == (
        f"com_ports.dut_uart is bound to {BY_ID_PATH}. "
        "No host COM ports detected. Connect USB serial hardware and run: agentic-hil com-ports"
    )


def test_the_bound_port_alone_leaves_no_other_port_to_name(tmp_path: Path) -> None:
    """The recording with only the debugger's port left in it."""
    ports = [port for port in RECORDED_INVENTORY["ports"] if port["device"] == KERNEL_NAME]
    only_the_board = {
        **copy.deepcopy(RECORDED_INVENTORY),
        "ports": copy.deepcopy(ports),
        "summary": "1 available COM port(s).",
    }

    steps = cli.init_next_steps(only_the_board, tmp_path / "config.yaml", bound_com_ports={"dut_uart": BY_ID_PATH})

    assert _confirming_item(steps) == f"com_ports.dut_uart is bound to {BY_ID_PATH}. No other COM ports detected."


# ---------------------------------------------------------------------------
# The inventory paragraph, once.


def test_init_prints_the_inventory_paragraph_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same ninety words were the step's headline and its item 1.

    `init` on the recorded host binds the one probe the USB serial inventory
    shows, and the paragraph saying what that inventory cannot see is the one
    thing in the step a reader has to weigh. Printed twice it reads as two
    findings, or the second copy is skipped along with whatever follows it. The
    rest of what the step carries still reaches the screen, the sentence that
    points at the fields in the file included."""
    _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch)

    result = cli.init_project()

    config_step = result["steps"]["config"]
    note = config_step["hardware_discovery"]["probe_inventory_note"]
    out = _flat(render_result(result, "init"))

    assert out.count(_flat(note)) == 1, out
    for step in config_step["next_steps"][1:]:
        assert _flat(step) in out, step
    assert "`debuggers.dut.probe_inventory`" in out


def test_setup_prints_the_inventory_paragraph_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported run: `setup` renders the same `config` step `init` does."""
    _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch)
    _setup_harness(monkeypatch)

    result = cli.setup_project(agent="claude-code")

    config_step = result["steps"]["config"]
    note = config_step["hardware_discovery"]["probe_inventory_note"]
    out = _flat(render_result(result, "setup"))

    assert out.count(_flat(note)) == 1, out
    for step in config_step["next_steps"][1:]:
        assert _flat(step) in out, step
    assert "`debuggers.dut.probe_inventory`" in out


def test_the_document_keeps_the_paragraph_everywhere_it_carried_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the doubled rendering goes; the document is what a caller parses.

    The discovery keeps its three fields, and the step keeps the paragraph in
    its `summary` and in its first next step, so a caller reading either one
    alone still has it."""
    _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch)

    result = cli.init_project()

    config_step = result["steps"]["config"]
    discovery = config_step["hardware_discovery"]
    assert discovery["discovered_by"] == "usb_serial_inventory"
    assert discovery["probe_inventory"] == "incomplete"
    note = discovery["probe_inventory_note"]
    assert "'066BFF505050505050505050' is the one ST-Link this host's USB serial inventory shows" in note
    assert note in config_step["summary"]
    assert note in config_step["next_steps"][0]


def test_a_refusal_that_carries_the_discovery_prints_the_paragraph_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other way the same paragraph reaches a screen twice.

    A refusal that carries the discovery renders it as a nested result: its
    summary, which ends with the paragraph, and then its fields, one of which is
    `probe_inventory_note`, the paragraph again. `agentic-hil adopt-hardware`
    answers this way when the probe it read cannot be given back."""
    _the_recorded_host(monkeypatch)
    discovery = discover_attached_hardware(profile=STARTER_PROFILE)
    assert discovery["probe_inventory"] == "incomplete", discovery
    refusal = _release_refusal(discovery, {"lease_state": "quarantined"}, PROJECT_CONFIG_ADOPT)

    out = _flat(render_result(refusal, "adopt-hardware"))

    assert out.count(_flat(discovery["probe_inventory_note"])) == 1, out


# ---------------------------------------------------------------------------
# A failed listing, named on the screen.
#
# The COM-port item said that discovery failed and nothing about how. The error
# the serial backend raised is in the document, as `backend_error` under
# `available_com_ports`, and neither screen prints that field, so the one line
# that decides what to do next never reached the person reading them.

# What to do about a listing the serial backend could not finish. pyserial was
# imported to raise the error, so its installation is not the question; the
# causes the result names are the error itself and a USB serial device that
# changed state while the host listed it.
LISTING_ADVICE = "Run: agentic-hil com-ports again once the USB serial devices have settled."


def _device_gone() -> OSError:
    """What a USB serial device that went away mid-listing raises, in the
    platform's own words: the errno and the C library's text for it. No host has
    been recorded failing a listing."""
    return OSError(errno.ENODEV, os.strerror(errno.ENODEV))


def _a_failed_listing(monkeypatch: pytest.MonkeyPatch, error: OSError) -> JsonObject:
    """The product's own document for a host listing that raised `error`."""

    def comports(*args: object, **kwargs: object) -> list[object]:
        raise error

    with monkeypatch.context() as patch:
        patch.setattr("serial.tools.list_ports.comports", comports)
        failed = list_available_com_ports()
    assert failed["error_type"] == "com_port_discovery_failed", failed
    assert failed["backend_error"] == str(error), failed
    return failed


def _the_advice_listing_fails(monkeypatch: pytest.MonkeyPatch) -> JsonObject:
    """The listing the COM-port advice is built from fails, and discovery's own
    reading of the recorded host stands, so the file binds what it bound."""
    failed = _a_failed_listing(monkeypatch, _device_gone())
    monkeypatch.setattr(
        "agentic_hil.cli.list_available_com_ports", lambda tool="com_ports_available": copy.deepcopy(failed)
    )
    return failed


def _run(command: str, monkeypatch: pytest.MonkeyPatch) -> JsonObject:
    if command == "setup":
        _setup_harness(monkeypatch)
        return cli.setup_project(agent="claude-code")
    return cli.init_project()


def _screen_items(out: str) -> list[str]:
    """Every numbered item on a rendered screen, each as it was printed."""
    items: list[list[str]] = []
    hanging = ""
    for line in out.splitlines():
        head = re.match(r"\s*\d+\. ", line)
        if head:
            items.append([line])
            hanging = " " * head.end()
        elif items and hanging and line.startswith(hanging) and line[len(hanging) : len(hanging) + 1].strip():
            items[-1].append(line)
        else:
            hanging = ""
    return ["\n".join(item) for item in items]


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "nothing-bound"])
@pytest.mark.parametrize("command", ["init", "setup"])
def test_a_failed_listing_is_named_in_the_item_that_says_it_failed(
    command: str, bound: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screen names the error the discovery reported, whole, in the item
    that says what to do about it, after the bound entry's confirmation when the
    file binds one.

    With the toolchain found, the file binds `dut_uart` to the debugger's port;
    without it, the profile's `dut_uart` is declared with no device."""
    _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch, openocd=bound)
    failed = _the_advice_listing_fails(monkeypatch)

    out = render_result(_run(command, monkeypatch), command)

    printed = [item for item in _screen_items(out) if "COM port discovery failed" in _flat(item)]
    assert len(printed) == 1, out
    number, said = _flat(printed[0]).split(". ", 1)
    assert failed["backend_error"] in said, printed[0]
    expected = f"COM port discovery failed: {failed['backend_error']}. {LISTING_ADVICE}"
    assert number.isdigit()
    assert said == (f"com_ports.dut_uart is bound to {BY_ID_PATH}. {expected}" if bound else expected)


@pytest.mark.parametrize("command", ["init", "setup"])
def test_the_document_keeps_the_failed_listing_as_the_discovery_returned_it(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error was never missing from the document: `available_com_ports` is
    the discovery's own result, its `backend_error` and `likely_causes` in it."""
    _starter_workspace(tmp_path, monkeypatch)
    _the_recorded_host(monkeypatch)
    failed = _the_advice_listing_fails(monkeypatch)

    result = _run(command, monkeypatch)

    assert result["steps"]["config"]["available_com_ports"] == failed


@pytest.mark.skipif(
    sys.platform != "win32", reason="ctypes.WinError, which pyserial raises when a listing fails, is Windows-only"
)
def test_a_windows_error_ends_its_own_sentence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyserial fails a Windows listing with `ctypes.WinError()`, whose text is
    the system's own sentence with its full stop: here ERROR_GEN_FAILURE, a
    device attached to the system that is not functioning. The item names it
    whole and does not end it twice."""
    import ctypes

    error = ctypes.WinError(31)
    assert str(error).endswith("."), str(error)
    failed = _a_failed_listing(monkeypatch, error)

    steps = cli.init_next_steps(failed, tmp_path / "config.yaml")

    assert [step for step in steps if "COM port discovery failed" in step] == [
        f"COM port discovery failed: {error} {LISTING_ADVICE}"
    ]
