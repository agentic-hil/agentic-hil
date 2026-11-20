"""A reader that holds a file open must not fail the atomic write that replaces it.

On Windows a rename onto a name that another handle holds open is refused, and so
is a rename of a source another handle holds without delete sharing. Recorded on
Windows 11 with CPython 3.14 against NTFS:

* the target open for reading, under any share mode (none, read, read and write,
  read, write and delete): `PermissionError`, `winerror` 5, errno 13;
* the source open for reading without delete sharing: `PermissionError`,
  `winerror` 32, errno 13.

Holders like that are ordinary on a bench: another agentic-hil process reading
`last-report.json` while a run writes it, an agent's own file read, a virus
scanner or the search indexer looking at a file that just changed. Each holds the
file for milliseconds. The atomic writer went straight from the first refusal to
a failed write, and a failed report write is an audit failure that quarantines the
bench: an observation run that sent nothing to the board was quarantined that way
after four minutes of rewriting its report once a second.

So the Windows writer retries those two refusals for a bounded time and then
raises the one Windows gave. A refusal that persists past the budget is still the
failure it always was, with Windows' own words, and every other error is raised
at once.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import json

import pytest
from conftest import write_config

from agentic_hil import config as config_module
from agentic_hil.config import atomic_write_bytes, load_config
from agentic_hil.report import last_report_path, write_report

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the refusal is the Windows rename's own; POSIX renames over an open file")


def _strays(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if entry.name.startswith(".agentic-hil-write-"))


def test_a_reader_holding_the_target_open_does_not_fail_the_write(tmp_path: Path) -> None:
    target = tmp_path / "last-report.json"
    target.write_bytes(b"old")
    reader = open(target, "rb")  # noqa: SIM115 - held across the write on purpose, closed by the timer
    letting_go = threading.Timer(0.2, reader.close)
    letting_go.start()
    try:
        atomic_write_bytes(target, b"new")
    finally:
        letting_go.cancel()
        reader.close()

    assert target.read_bytes() == b"new"
    assert _strays(tmp_path) == []


def test_a_report_written_while_someone_reads_the_last_one_stays_audited(tmp_path: Path) -> None:
    """The incident itself: a COM step's report landing while another process
    has `last-report.json` open. It used to come back `audit_ok: false` with
    `[WinError 5]` and quarantine the bench."""
    config = load_config(str(write_config(tmp_path)))
    first = write_report(config, {"ok": True, "tool": "com_read", "port_id": "dut_uart", "bytes_read": 45})
    assert first.get("audit_ok") is not False
    mirror = Path(last_report_path(config))
    reader = open(mirror, "rb")  # noqa: SIM115 - held across the write on purpose, closed by the timer
    letting_go = threading.Timer(0.2, reader.close)
    letting_go.start()
    try:
        second = write_report(config, {"ok": True, "tool": "com_read", "port_id": "dut_uart", "bytes_read": 41})
    finally:
        letting_go.cancel()
        reader.close()

    assert second.get("audit_ok") is not False, second
    assert "audit_errors" not in second
    assert json.loads(mirror.read_text(encoding="utf-8"))["bytes_read"] == 41


def test_a_reader_that_never_lets_go_still_fails_the_write_with_windows_own_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "WINDOWS_REPLACE_RETRY_BUDGET_S", 0.05)
    target = tmp_path / "last-report.json"
    target.write_bytes(b"old")
    with open(target, "rb"), pytest.raises(PermissionError) as raised:
        atomic_write_bytes(target, b"new")

    assert raised.value.winerror == 5
    assert target.read_bytes() == b"old"
    assert _strays(tmp_path) == []


def test_a_held_source_is_retried_until_its_holder_lets_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scanner opening the freshly written temporary file without delete
    sharing: the recorded refusal for that is `winerror` 32."""
    target = tmp_path / "last-report.json"
    real_replace = os.replace
    calls: list[str] = []

    def held_twice(source: object, destination: object) -> None:
        calls.append(str(destination))
        if len(calls) <= 2:
            error = PermissionError(13, "Der Prozess kann nicht auf die Datei zugreifen, da sie von einem anderen Prozess verwendet wird")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(config_module.os, "replace", held_twice)
    atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert len(calls) == 3
    assert _strays(tmp_path) == []


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(FileNotFoundError(2, "Das System kann die angegebene Datei nicht finden"), id="missing"),
        pytest.param(OSError(28, "Auf dem Datenträger ist nicht genug Speicherplatz vorhanden"), id="disk-full"),
    ],
)
def test_any_other_refusal_is_raised_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    target = tmp_path / "last-report.json"
    target.write_bytes(b"old")
    calls: list[str] = []

    def refuse(source: object, destination: object) -> None:
        calls.append(str(destination))
        raise error

    monkeypatch.setattr(config_module.os, "replace", refuse)
    with pytest.raises(OSError) as raised:
        atomic_write_bytes(target, b"new")

    assert raised.value is error
    assert len(calls) == 1
    assert target.read_bytes() == b"old"
    assert _strays(tmp_path) == []
