"""What `tools/withhold.py` leaves of the probe's serial and the machine's names.

A workflow's log and its artifacts can be read by anyone who can read the
repository. `tools/bench_in_container.py` withholds the probe's serial numbers
and this machine's host name, home directory and user name from everything the
bench tier prints and hands back, which covers the gate and the nightly's
distributions. The nightly's demo job drives the release installed on the
machine itself instead, so that runner is not there to withhold what doctor,
the plan, the pytest plugin and the evidence print, or what the job uploads.
This is the same withholding as a command a workflow step runs, and these tests
hold it to what the runner's own withholding promises: whole values replaced
with [withheld], line by line, no line removed, every attached probe's serial
found the same way, and nothing run or changed when there is no probe to learn
a serial from.

Nothing here needs a probe. The sysfs the probes are found through is a
directory tree laid out the way the kernel documents it, as in the runner's own
tests, and the commands are this interpreter.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from support import scaled_time_bound

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import bench_in_container  # noqa: E402
import withhold  # noqa: E402

SCRIPT = TOOLS / "withhold.py"
PYTHON = sys.executable

# A probe serial, a host name, a home and a user that belong to no machine,
# the runner's tests' own. What they stand for is exactly what must never reach
# a log somebody else can read.
PROBE_SERIAL = "PROBESERIAL0001"
SECOND_SERIAL = "PROBESERIAL0002"
HOST_NAME = "workstation-7"
HOME = "/srv/mhuber"
USER = "mhuber"
BY_ID = f"/dev/serial/by-id/usb-Vendor_Probe_{PROBE_SERIAL}-if02"


def a_probe(sysfs: Path, name: str, *, serial: str | None, device: int = 5) -> None:
    """One in-circuit debugger the way /sys/bus/usb/devices shows it, by the runner's vendor and product ids."""
    directory = sysfs / "bus" / "usb" / "devices" / name
    directory.mkdir(parents=True)
    for attribute, value in (("idVendor", "0483"), ("idProduct", "374b"), ("busnum", "1"), ("devnum", str(device))):
        (directory / attribute).write_text(f"{value}\n", encoding="utf-8")
    if serial is not None:
        (directory / "serial").write_text(f"{serial}\n", encoding="utf-8")


@pytest.fixture
def sysfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This machine as the tool sees it: one probe attached, and names of its own."""
    root = tmp_path / "sys"
    a_probe(root, "1-2", serial=PROBE_SERIAL)
    monkeypatch.setattr(bench_in_container, "SYSFS", root)
    monkeypatch.setattr(withhold, "host_identities", lambda: [HOST_NAME, HOME, USER])
    return root


def a_command(source: str) -> list[str]:
    return [PYTHON, "-c", textwrap.dedent(source)]


def test_what_a_command_prints_reaches_its_own_stream_withheld_line_by_line(
    sysfs: Path, capfd: pytest.CaptureFixture
) -> None:
    """Standard output to standard output, standard error to standard error, and no line removed."""
    status = withhold.main(
        [
            "run",
            "--",
            *a_command(
                f"""
                import sys
                print("Debuggers")
                print("    probe_id       {PROBE_SERIAL.lower()}")
                print("    device         {BY_ID}")
                print("")
                print("config_path {HOME}/.config/agentic-hil/config.yaml", file=sys.stderr)
                print("on {HOST_NAME} as {USER}", file=sys.stderr)
                """
            ),
        ]
    )

    printed = capfd.readouterr()
    assert status == 0
    assert printed.out.splitlines() == [
        "Debuggers",
        "    probe_id       [withheld]",
        "    device         /dev/serial/by-id/usb-Vendor_Probe_[withheld]-if02",
        "",
    ]
    assert printed.err.splitlines() == [
        "config_path [withheld]/.config/agentic-hil/config.yaml",
        "on [withheld] as [withheld]",
    ]


def test_the_commands_exit_status_is_the_status_of_the_step(sysfs: Path, capfd: pytest.CaptureFixture) -> None:
    """A red plan stays a red step: the command's own status comes back unchanged."""
    status = withhold.main(["run", "--", *a_command(f"import sys; print('{PROBE_SERIAL}'); sys.exit(3)")])

    assert status == 3
    assert capfd.readouterr().out.splitlines() == ["[withheld]"]


def test_a_command_that_cannot_start_is_said_so_and_is_127(sysfs: Path, tmp_path: Path, capfd: pytest.CaptureFixture) -> None:
    status = withhold.main(["run", "--", str(tmp_path / "no-such-command")])

    assert status == withhold.EXIT_CANNOT_START == 127
    assert "could not be started" in capfd.readouterr().err


def test_the_serial_of_every_attached_probe_is_withheld(sysfs: Path, capfd: pytest.CaptureFixture) -> None:
    """Every probe the runner's discovery finds, not only the first: which one a configuration names is not known here."""
    a_probe(sysfs, "1-3", serial=SECOND_SERIAL, device=6)

    assert withhold.main(["run", "--", *a_command(f"print('{PROBE_SERIAL} and {SECOND_SERIAL}')")]) == 0

    assert capfd.readouterr().out.splitlines() == ["[withheld] and [withheld]"]


def test_a_report_the_next_step_reads_is_written_as_the_command_wrote_it(
    sysfs: Path, tmp_path: Path, capfd: pytest.CaptureFixture
) -> None:
    """`--stdout-file` is for the plan's report, which the evidence is built from by its paths.

    Withheld there, the report's paths under the home directory would no longer
    lead to the plan the evidence reads. It goes to the file as the command
    wrote it, its standard error is withheld as always, and the step that
    withholds the evidence before it is uploaded covers the file.
    """
    report = tmp_path / "artifacts" / "reports" / "testconfig.json"
    report.parent.mkdir(parents=True)
    written = f'{{"test_config_path": "{HOME}/demo/testconfig.yaml", "probe_id": "{PROBE_SERIAL}"}}\n'

    status = withhold.main(
        [
            "run",
            "--stdout-file",
            str(report),
            "--",
            *a_command(
                f"""
                import sys
                sys.stdout.write({written!r})
                print("plan refused on probe {PROBE_SERIAL}", file=sys.stderr)
                """
            ),
        ]
    )

    printed = capfd.readouterr()
    assert status == 0
    assert report.read_text(encoding="utf-8") == written
    assert printed.out == ""
    assert printed.err.splitlines() == ["plan refused on probe [withheld]"]


def test_files_are_withheld_in_place_and_only_where_a_value_stood(
    sysfs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
) -> None:
    """The evidence before it is uploaded: every regular file under each path the upload names.

    A file with nothing to withhold is left byte for byte, a byte that is not
    UTF-8 survives in a file that had a value replaced, and a path a run that
    stopped early never wrote is passed over. Each file that changed is named,
    by the path it was reached through.
    """
    demo = tmp_path / "demo"
    report = demo / "artifacts" / "reports" / "testconfig.json"
    port_log = demo / ".agentic-hil" / "logs" / "com-dut_uart.jsonl"
    untouched = demo / "artifacts" / "evidence" / "run-summary.json"
    last = demo / ".agentic-hil" / "reports" / "last-report.json"
    for path in (report, port_log, untouched, last):
        path.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f'{{"probe_id": "{PROBE_SERIAL}", "device": "{BY_ID}"}}\n', encoding="utf-8")
    port_log.write_bytes(f'{{"line": "adapter serial {PROBE_SERIAL}"}}\n'.encode() + b"\xff\xfe raw bytes\n")
    untouched.write_bytes(b'{"outcome": "success"}\r\n\xff')
    last.write_text(f"written on {HOST_NAME} in {HOME}/work\n", encoding="utf-8")
    monkeypatch.chdir(demo)

    status = withhold.main(["files", "artifacts", ".agentic-hil/reports", ".agentic-hil/logs", "never-written"])

    assert status == 0
    assert report.read_text(encoding="utf-8") == (
        '{"probe_id": "[withheld]", "device": "/dev/serial/by-id/usb-Vendor_Probe_[withheld]-if02"}\n'
    )
    assert port_log.read_bytes() == b'{"line": "adapter serial [withheld]"}\n\xff\xfe raw bytes\n'
    assert untouched.read_bytes() == b'{"outcome": "success"}\r\n\xff'
    assert last.read_text(encoding="utf-8") == "written on [withheld] in [withheld]/work\n"
    named = capfd.readouterr().err
    for changed in ("artifacts/reports/testconfig.json", ".agentic-hil/logs/com-dut_uart.jsonl", ".agentic-hil/reports/last-report.json"):
        assert changed in named.replace(os.sep, "/"), named
    assert "run-summary.json" not in named


@pytest.mark.parametrize("serial", [None, ""], ids=["no-probe", "a-probe-without-a-serial"])
def test_without_a_serial_to_withhold_nothing_is_run_and_nothing_is_changed(
    sysfs: Path, tmp_path: Path, serial: str | None, capfd: pytest.CaptureFixture
) -> None:
    """No attached probe carries a serial, so what the configuration names is not known here.

    Doctor would print it unwithheld, and so would every file the job uploads,
    so both commands refuse before they run or change anything, and say why.
    """
    for entry in (sysfs / "bus" / "usb" / "devices").iterdir():
        (entry / "serial").unlink()
        if serial is not None:
            (entry / "serial").write_text(f"{serial}\n", encoding="utf-8")
    ran = tmp_path / "ran"
    report = tmp_path / "testconfig.json"
    evidence = tmp_path / "artifacts" / "report.json"
    evidence.parent.mkdir()
    evidence.write_text(f'{{"probe_id": "{PROBE_SERIAL}"}}\n', encoding="utf-8")

    run_status = withhold.main(
        ["run", "--stdout-file", str(report), "--", *a_command(f"open({str(ran)!r}, 'w').close()")]
    )
    files_status = withhold.main(["files", str(evidence.parent)])

    refusals = capfd.readouterr().err
    assert run_status == files_status == withhold.EXIT_REFUSED == 2
    assert not ran.exists()
    assert not report.exists()
    assert evidence.read_text(encoding="utf-8") == f'{{"probe_id": "{PROBE_SERIAL}"}}\n'
    assert refusals.count("no in-circuit debugger or programmer attached") == 2, refusals


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="a stop signal reaches a process this way on POSIX alone")
def test_a_stop_signal_reaches_the_command_and_what_it_says_then_is_withheld(tmp_path: Path) -> None:
    """A cancelled step's signal is handed to the command, and its last words are still relayed.

    The plan closes its sessions on the way out, so the command has to hear the
    signal rather than lose the process it prints through.
    """
    root = tmp_path / "sys"
    a_probe(root, "1-2", serial=PROBE_SERIAL)
    command = a_command(
        f"""
        import signal, sys, time
        def stop(number, frame):
            print("stopping the plan on probe {PROBE_SERIAL}", flush=True)
            sys.exit(3)
        signal.signal(signal.SIGTERM, stop)
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    driver = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(TOOLS)!r})
        import bench_in_container, withhold
        bench_in_container.SYSFS = Path({str(root)!r})
        withhold.host_identities = lambda: []
        sys.exit(withhold.main(["run", "--", *{command!r}]))
        """
    )
    wrapper = subprocess.Popen([PYTHON, "-c", driver], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert wrapper.stdout is not None
        assert wrapper.stdout.readline() == "ready\n"
        wrapper.send_signal(signal.SIGTERM)
        out, err = wrapper.communicate(timeout=scaled_time_bound(30))
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()

    assert wrapper.returncode == 3, err
    assert out == "stopping the plan on probe [withheld]\n"
