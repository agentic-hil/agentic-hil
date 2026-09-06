"""The probe, the reset it performs, and the port the board answers on.

Three things a fake cannot establish. Whether a probe is enumerated at all is a
question about this host's USB inventory; whether OpenOCD's own words after a
successful reset are read as evidence or as a verdict is a question about the
OpenOCD on this machine and the board in front of it; and whether the port
listing stays readable is a question about a host that has thirty-odd serial
devices of which one is the board.

Nothing here asserts a value that identifies hardware. A serial is asserted to
be present and to be a non-empty string, never to be a particular string: these
files are public and the bench is not, and a test written the other way passes
on one machine in the world.
"""

from __future__ import annotations

import pytest

from .conftest import BENCH_ONLY, Bench, debugger_capture, failure_worded_lines

pytestmark = [pytest.mark.bench, BENCH_ONLY]


def test_the_attached_probe_is_listed_with_a_serial_and_the_count_is_not_claimed_authoritative(bench: Bench) -> None:
    """What discovery may say about a bench it can only see through a USB inventory.

    Two claims at once, and they pull in opposite directions on purpose. The
    probe is there and is named, which is what makes a multi-board configuration
    writable at all; and the listing states that its count is not authoritative,
    because OpenOCD has no probe listing of its own and a probe with no virtual
    COM port would not appear. Exiting non-zero over that second fact broke every
    `set -e` script over a working bench, so the status is asserted too.
    """
    status, result = bench.document("debugger-probes")

    assert status == 0, result
    assert result["ok"] is True, result
    probes = result["probes"]
    assert probes, "no probe was enumerated, and this tier runs only where one is attached"
    for probe in probes:
        assert isinstance(probe["probe_id"], str) and probe["probe_id"].strip(), probe
    assert result["complete"] is False, result
    assert "authoritative" in result["summary"], result["summary"]


def test_a_real_reset_over_the_probe_is_a_success_and_carries_whatever_openocd_said(bench: Bench) -> None:
    """The backend's own success marker outranks the words on the way to it.

    OpenOCD stops evaluating its command string at the first command that fails,
    so a marker in the output is OpenOCD's own statement that the reset returned
    success. Reading a failure-worded line printed alongside it as a second,
    contradicting verdict refused a step on an Ubuntu bench whose board had
    already restarted, while the same plan passed on a bench whose OpenOCD build
    never writes the line.

    Both directions are held here, against whatever this bench's OpenOCD prints:
    the step is a success even where its capture carries the words a failure is
    read out of, and every line the result kept as evidence is a line that was
    really in the capture. What is deliberately not asserted is that the two
    sets are the same set. The predicate below is written here rather than
    imported, because asking the backend which lines they are would be asking
    the code under test to grade itself, and two independently written rules
    that disagree in either direction make a strict equality a test that goes
    red for a defect that is not there and green for one that is.
    """
    plan = bench.project / "reset-only.yaml"
    plan.write_text(
        f"""version: 3
name: reset-only
steps:
  - device: {bench.debugger_name()}
    action: reset
    mode: run
""",
        encoding="utf-8",
    )

    status, report = bench.document("test-reactor", "--test-config", "reset-only.yaml")

    assert status == 0, report
    assert report["ok"] is True, report
    reset = report["steps"][0]["result"]
    assert reset["ok"] is True, reset
    assert reset["success_confirmed"] is True, reset
    assert "error_type" not in reset, reset

    capture = debugger_capture(bench, reset["log_path"])
    carried = reset.get("backend_warnings") or []
    assert all(warning in capture.splitlines() for warning in carried), (carried, capture)
    if carried:
        assert "backend_warnings" in reset["summary"], reset["summary"]
    # The whole claim, in one line: a capture carrying the words a failure is
    # read out of did not turn a confirmed success into a refusal.
    assert not failure_worded_lines(capture) or reset["success_confirmed"] is True, (capture, reset)
    assert reset["summary"].startswith("Target reset with mode 'run'."), reset["summary"]


def test_com_ports_lists_the_board_in_full_and_collapses_the_ports_with_no_usb_identity(bench: Bench) -> None:
    """A host with thirty legacy stubs and one board, printed so the board is findable.

    The listing used to print every one of them at the same length, so the entry
    an operator needed was somewhere inside a page of `/dev/ttyS*` rows carrying
    no information at all. What carries a USB identity is printed whole, down to
    the stable by-id path a configuration should name, and the rest collapse to
    one line that still says how many they were and what they run between.
    """
    listed = bench.run("com-ports")
    assert listed.returncode == 0, listed.stderr
    printed = listed.stdout

    status, result = bench.document("com-ports")
    assert status == 0, result
    identified = [port for port in result["ports"] if port.get("serial_number") or port.get("vid") or port.get("product")]
    assert identified, "this bench published no port with a USB identity, and the board's is one"

    for port in identified:
        assert port["device"] in printed, printed
        if port.get("stable_device"):
            assert port["stable_device"] in printed, printed
        if port.get("serial_number"):
            assert str(port["serial_number"]) in printed, "the serial the configuration names is missing from the rendering"

    anonymous = [port for port in result["ports"] if port not in identified]
    if anonymous:
        assert f"{len(anonymous)} legacy serial ports without a USB identity" in printed, printed
        for port in anonymous:
            # Collapsed means not given a bullet of its own. The count and the
            # names the range runs between are still there, which is what keeps
            # the line an account rather than an omission.
            assert f"- {port['device']}\n" not in printed, f"{port['device']} was printed as an entry of its own"
