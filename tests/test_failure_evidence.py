"""A classified failure carries the tool's own words, on every backend.

#327 attached `programmer_output` to the erase refusal, because the line that
justified the classification survived nowhere else in the result: the transcript
sat in the log file the result names by path, and an agent relaying the result to
a person relayed every fact except the decisive one. Every other classified
failure still shipped without it (#334). A failed download, a verify mismatch, an
internal command error and a reset failure all carried a classification, a
summary and a list of likely causes, and left the sentence all three were derived
from on disk.

Driven through the real service and the real fake CLIs rather than asserted on a
constructed result, because the question is whether the capture survives the path
a caller actually takes: the backend, the log write, the report write and the
redaction pass.

The two negative tests carry as much weight as the three positive ones. A
timeout returns before anything is classified, because a killed process says
nothing about where it stopped, and a success has no diagnosis to carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    FAKE_OPENOCD,
    FAKE_OPENOCD_NO_TARGET,
    FAKE_PYOCD,
    FAKE_PYOCD_UNKNOWN_TARGET,
    FAKE_STLINK,
    FAKE_STLINK_NO_TARGET,
    write_config,
)

from agentic_hil.backends.common import CompletedCommand
from agentic_hil.config import load_config
from agentic_hil.knowledge import ERROR_CATALOGUE
from agentic_hil.tools import AgenticHILToolService

PROBE_ID = {"openocd": "STLINK123", "pyocd": "PYOCD123", "stlink": "STLINK123"}


def probe_through_the_service(workspace: Path, backend_name: str, executable: Path) -> dict:
    target_type = "stm32f446retx" if backend_name == "pyocd" else None
    config = load_config(str(write_config(workspace, debugger_type=backend_name, debugger_executable=executable, probe_id=PROBE_ID[backend_name], target_type=target_type)))
    service = AgenticHILToolService(config)
    try:
        return service.call("probe_target")
    finally:
        service.close()


@pytest.mark.parametrize(
    ("backend_name", "executable", "error_type", "decisive_line"),
    [
        ("stlink", FAKE_STLINK_NO_TARGET, "target_not_detected", "Error: No STM32 target found!"),
        ("openocd", FAKE_OPENOCD_NO_TARGET, "target_not_detected", "Error: unable to connect to the target"),
        ("pyocd", FAKE_PYOCD_UNKNOWN_TARGET, "target_type_invalid", "not recognized"),
    ],
    ids=["stlink", "openocd", "pyocd"],
)
def test_a_classified_failure_carries_the_tools_own_words(
    backend_name: str, executable: Path, error_type: str, decisive_line: str, tmp_path: Path
) -> None:
    """One arbitrary classified failure per backend, none of them the erase refusal.

    Arbitrary on purpose: the policy is that a failure carries its diagnosis, not
    that the one failure somebody wrote a fixture for does.
    """
    refused = probe_through_the_service(tmp_path, backend_name, executable)
    captured = refused["programmer_output"]

    assert refused["ok"] is False
    assert refused["error_type"] == error_type
    assert decisive_line in f"{captured['stdout']}{captured['stderr']}"
    # The shape #327 chose and #317 redacts by key name at any depth, unchanged:
    # the two streams apart, with the process's own return code beside them.
    assert set(captured) == {"returncode", "stdout", "stderr"}
    assert captured["returncode"] != 0
    # And the log file the result names still holds the same capture, so the two
    # accounts of one run cannot disagree.
    assert refused["log_path"]


@pytest.mark.parametrize(
    ("backend_name", "executable"),
    [("stlink", FAKE_STLINK), ("openocd", FAKE_OPENOCD), ("pyocd", FAKE_PYOCD)],
    ids=["stlink", "openocd", "pyocd"],
)
def test_a_success_does_not_grow_the_field(backend_name: str, executable: Path, tmp_path: Path) -> None:
    """The capture is a failure's diagnosis, and a success has none to give.

    Pinned rather than assumed: attaching it everywhere would put a copy of every
    transcript this bench ever produces into every report it ever writes, which
    is a different policy from the one #334 asked for.
    """
    probed = probe_through_the_service(tmp_path, backend_name, executable)

    assert probed["ok"] is True
    assert "programmer_output" not in probed


def test_a_timeout_carries_no_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberately exempt, and the exemption is the honest answer.

    A timeout is the deadline killing the process before it could say where it
    stopped, so what it wrote up to that point is not an account of the failure.
    It returns before anything is classified, and the log file still holds
    whatever the process managed to print.
    """
    from agentic_hil.backends import stlink

    monkeypatch.setattr(
        stlink,
        "spawn_command",
        lambda *args, **kwargs: CompletedCommand(stdout="Memory Programming ...\n", stderr="", returncode=None, timed_out=True, not_found=False),
    )
    timed_out = probe_through_the_service(tmp_path, "stlink", FAKE_STLINK)

    assert timed_out["ok"] is False
    assert timed_out["error_type"] == "timeout"
    assert "programmer_output" not in timed_out
    assert timed_out["log_path"]


@pytest.mark.parametrize("scope", ["stlink", "openocd", "pyocd"])
def test_the_erase_remedies_send_the_reader_to_the_field_the_result_now_carries(scope: str) -> None:
    """The remedy has to name where the transcript is, and it moved.

    The two backend entries #333 added pointed at `log_path`, because when they
    were written that was the only place a caller could read OpenOCD's or
    pyOCD's own account of the run. It is on the result now, and a remedy that
    still sent a reader to a file path would be sending them past the answer.
    """
    steps = " ".join(ERROR_CATALOGUE[f"flash_erase_failed:{scope}"].remediation)

    assert "programmer_output" in steps
