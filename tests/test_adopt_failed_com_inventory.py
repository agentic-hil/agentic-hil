"""adopt-hardware tells a COM port inventory that failed from one with no matching port (#570).

When adoption found the probe and no host serial port carrying its serial
number, it listed `com_ports.<name>.device` as unavailable with the reason "No
host serial port carries this probe's serial number", and it gave that reason
whenever the match was missing, including when the host's serial ports were
never listed: pyserial would not import, or listing the ports raised an OS
error. With STM32CubeProgrammer installed the probe is enumerated through its
CLI, so discovery still finds it while the inventory has failed, and the reason
stated a finding about the board that nobody made, with the inventory's own
error nowhere in the answer. The entry now says the host's serial ports could
not be listed, carries the inventory's error line, and offers no host ports,
because none were read. The reason stays for an inventory that was read and
holds no matching port.

The probe is found the way such a host finds it: the STM32CubeProgrammer
fixture lists it and names its target, each time as a process of its own. The
inventory fails for real as well, through pyserial hidden from every finder or
through its port enumeration raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_STLINK
from test_config_adopt import placeholder_bench, service
from test_pyserial_import_error import LISTING_IMPORT, MISSING, _break_pyserial

from agentic_hil import cli
from agentic_hil.adopt import PROJECT_CONFIG_ADOPT, plan_adoption
from agentic_hil.bootstrap import DISCOVERED_BY_STLINK_CLI
from agentic_hil.comports import list_available_com_ports
from agentic_hil.types import JsonObject

# The serial the STM32CubeProgrammer fixture lists first and answers its HOTPLUG
# connect under. It lists a second probe beside it, so the call names this one,
# the way an operator with two boards attached does.
PROBE = "STLINK123"

# The entry this issue is about.
KEY = "com_ports.<name>.device"

# The keys the attached board answers whatever the host's serial ports say,
# which is how a test here knows the probe was found and its target read.
ANSWERED = ["debuggers.dut.executable", "debuggers.dut.probe_id", "target.controller"]

# What an inventory that was read and holds no matching port is told, unchanged
# by the fix.
NO_MATCH_REASON = (
    "No host serial port carries this probe's serial number, so discovery cannot say which device belongs "
    "to this board. A Nucleo exposes one over the probe; a board wired to a separate USB-serial adapter does not."
)
NO_MATCH_NEXT_STEP = (
    "Name the device yourself with `project_config_set` on `com_ports.<name>.device` if you know which of the "
    "host ports it is."
)
# Naming the device by hand, which both next steps offer.
BY_HAND = "`project_config_set` on `com_ports.<name>.device`"

# A port of another device, whose serial the probe's does not match.
OTHER_PORT = {"device": "COM4", "serial_number": "SOMETHINGELSE"}

# The two ways the issue says the inventory fails, and the line the second one
# raises, which is this test's own.
FAILURES = ("import", "os_error")
OS_ERROR = "the port enumeration's own error line"


# ---------------------------------------------------------------------------
# The host.


def _stm32cubeprogrammer_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with STM32CubeProgrammer, which the suite otherwise hides. Its
    fixture answers the probe listing and the HOTPLUG connect."""
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: FAKE_STLINK.as_posix())


def _fail_the_inventory(failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JsonObject:
    """Leave the host's serial ports unlistable the way `failure` names, and
    return the inventory as discovery takes it, checked to have failed that way
    before the product is asked anything."""
    if failure == "import":
        _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)
    else:

        def enumeration_raises() -> list[object]:
            raise OSError(OS_ERROR)

        monkeypatch.setattr("serial.tools.list_ports.comports", enumeration_raises)
    inventory = list_available_com_ports("bootstrap_com_ports")
    assert inventory["ok"] is False, inventory
    assert inventory["backend_error"] == (MISSING if failure == "import" else OS_ERROR), inventory
    return inventory


def _plan(workspace: Path) -> JsonObject:
    """The plan `project_config_adopt_hardware` answers for the named probe."""
    tools = service(workspace)
    try:
        return tools.call(PROJECT_CONFIG_ADOPT, {"probe_id": PROBE})
    finally:
        tools.close()


def _entry(result: JsonObject) -> JsonObject:
    entries = [item for item in result["unavailable"] if item.get("key") == KEY]
    assert len(entries) == 1, result["unavailable"]
    return entries[0]


def _flat(text: str) -> str:
    """The text with the wrapper's line breaks taken back out."""
    return " ".join(text.split())


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = cli.entrypoint(list(argv))
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# An inventory that failed.


@pytest.mark.parametrize("failure", FAILURES)
def test_a_failed_inventory_is_not_read_as_a_board_without_a_serial_port(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe is found by its CLI and the board names itself, and the host's
    serial ports were never listed. The COM device is unavailable because the
    listing failed: the entry says so, carries the listing's error line, and
    has no host ports to offer. Its next step sends the reader to that line and
    back here, or to naming the device by hand."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch)
    _stm32cubeprogrammer_host(monkeypatch)
    inventory = _fail_the_inventory(failure, tmp_path, monkeypatch)

    planned = _plan(workspace)

    assert planned["ok"] is True, planned
    discovery = planned["hardware_discovery"]
    assert discovery["discovered_by"] == DISCOVERED_BY_STLINK_CLI, discovery
    assert discovery["available_com_ports"]["backend_error"] == inventory["backend_error"], discovery
    assert sorted(item["key"] for item in planned["carried"]) == ANSWERED, planned["carried"]

    entry = _entry(planned)
    assert "serial ports could not be listed" in entry["reason"], entry
    assert "No host serial port carries" not in entry["reason"], entry
    assert entry.get("backend_error") == inventory["backend_error"], entry
    assert "host_com_ports" not in entry, entry
    next_step = entry["next_step"]
    assert "error" in next_step and "again" in next_step, entry
    assert BY_HAND in next_step, entry


def test_an_inventory_that_failed_without_an_error_line_is_named_by_its_summary() -> None:
    """Where the inventory carries no error line of its own, the entry carries
    its summary. Every failure the listing reports carries one since #568, so
    this is the planner on its own, handed the listing's earlier shape."""
    document = {
        "debuggers": {"dut": {"type": "stlink", "probe_id": None, "executable": None}},
        "target": {"name": "example-target", "controller": "unknown-controller"},
        "com_ports": {},
    }
    inventory = {
        "ok": False,
        "tool": "bootstrap_com_ports",
        "error_type": "serial_backend_not_available",
        "summary": "pyserial is not installed or could not be imported.",
        "likely_causes": ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"],
    }
    discovery = {
        "ok": True,
        "backend": "stlink",
        "probe_id": PROBE,
        "executable": FAKE_STLINK.as_posix(),
        "target": {"controller": "STM32F446RE"},
        "com_port": None,
        "available_com_ports": inventory,
    }

    entry = _entry(plan_adoption(document, discovery))

    assert "serial ports could not be listed" in entry["reason"], entry
    assert entry.get("backend_error") == inventory["summary"], entry
    assert "host_com_ports" not in entry, entry


@pytest.mark.parametrize("failure", FAILURES)
def test_the_adopt_hardware_screen_prints_the_inventory_error(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agentic-hil adopt-hardware` carries the entry in its document, and its
    screen prints the listing's error line once, where it said that no port
    carries the probe's serial."""
    placeholder_bench(tmp_path, monkeypatch)
    _stm32cubeprogrammer_host(monkeypatch)
    inventory = _fail_the_inventory(failure, tmp_path, monkeypatch)

    code, out = _cli(capsys, "adopt-hardware", "--probe-id", PROBE, "--dry-run", "--json")
    assert code == 0, out
    assert _entry(json.loads(out)).get("backend_error") == inventory["backend_error"], out

    code, out = _cli(capsys, "adopt-hardware", "--probe-id", PROBE, "--dry-run")
    assert code == 0, out
    screen = _flat(out)
    assert "No host serial port carries" not in screen, out
    assert screen.count(inventory["backend_error"]) == 1, out


# ---------------------------------------------------------------------------
# An inventory that was read.


def test_an_inventory_that_was_read_and_holds_no_matching_port_keeps_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding the reason states is this inventory's own: the host's ports
    were listed and none carries the probe's serial. The entry keeps its reason,
    its next step and the ports that were read, and has no error to carry."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch)
    _stm32cubeprogrammer_host(monkeypatch)
    monkeypatch.setattr(
        "agentic_hil.bootstrap.list_available_com_ports",
        lambda tool: {"ok": True, "tool": tool, "ports": [OTHER_PORT], "summary": "1 available COM port(s)."},
    )

    planned = _plan(workspace)

    assert planned["ok"] is True, planned
    assert sorted(item["key"] for item in planned["carried"]) == ANSWERED, planned["carried"]
    entry = _entry(planned)
    assert entry["reason"] == NO_MATCH_REASON, entry
    assert entry["next_step"] == NO_MATCH_NEXT_STEP, entry
    assert entry["host_com_ports"] == [OTHER_PORT], entry
    assert "backend_error" not in entry, entry
