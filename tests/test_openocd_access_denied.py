"""The first failure a newcomer on Linux sees with the probe plugged in: OpenOCD may not open it.

The probe is on the bus and the device node's mode refuses the user OpenOCD
runs as, because no udev rule is installed or the user is not in the group it
names. OpenOCD 0.12's stlink driver says so in as many words,
`libusb_open() failed with LIBUSB_ERROR_ACCESS`, and then prints the same
`Error: open failed` it prints with nothing on USB. Read as the second line,
this is `adapter_not_found` with the causes of an absent probe, and the
operator is told to connect a probe that is connected. Read as the first, the
public `error_type` stays `adapter_not_found`, whose meaning is "could not be
found or opened", and `backend_error_type`, the summary and the causes say it
was the opening, and what to change about this user so it succeeds.

The number is read this way only off Windows. There libusb gives the same
answer for a device another program holds, which the causes `adapter_not_found`
already carries cover, the way the serial path reads `EACCES`.

The transcript the fake prints is the recording the bench took from the real
binary with the probe's group withheld (tests/bench/test_bench_without_device_group.py,
OpenOCD 0.12.0, 2026-09-27). This is the unit tier of the same fact; the bench
stage is the truth beside it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD_ACCESS_DENIED, write_config
from fixtures.fake_openocd_access_denied import RECORDED_ACCESS_DENIED_STDERR, THE_LINE_THE_CLASSIFIER_READS

import agentic_hil.backends.openocd as openocd_backend
from agentic_hil.backends.openocd import OPENOCD_INIT_STAGE_MARKER
from agentic_hil.config import load_config
from agentic_hil.humanize import render_result
from agentic_hil.tools import AgenticHILToolService

THE_THREE_TOOLS = [
    ("probe_target", {}),
    ("reset_target", {"mode": "run"}),
    ("flash_firmware", {"image_path": "build/firmware.elf"}),
]


class HostOs:
    """The `os` module as the backend sees it on one host: `name` is fixed, the rest is the real module."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attribute: str):
        return getattr(os, attribute)


def a_project_with_an_image(tmp_path: Path):
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    return load_config(str(write_config(tmp_path, debugger_executable=FAKE_OPENOCD_ACCESS_DENIED)))


def refused_on(host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str = "probe_target", arguments: dict | None = None) -> dict:
    """One call against the recording, with the backend seeing host `host`."""
    monkeypatch.setattr(openocd_backend, "os", HostOs(host), raising=False)
    service = AgenticHILToolService(a_project_with_an_image(tmp_path))
    try:
        return service.call(tool, arguments or {})
    finally:
        service.close()


def test_the_recording_is_a_refusal_before_init_and_carries_the_line() -> None:
    """The recording is internally consistent, which is all this can say.

    A guard against a one sided edit of the fixture module, not a pinned
    behaviour: both names come out of that module, so no change to the product
    can make this fail. What holds the recording to the tool is the bench stage
    that withholds the probe's group.
    """
    assert THE_LINE_THE_CLASSIFIER_READS in RECORDED_ACCESS_DENIED_STDERR
    assert "Error: open failed" in RECORDED_ACCESS_DENIED_STDERR
    assert OPENOCD_INIT_STAGE_MARKER not in RECORDED_ACCESS_DENIED_STDERR
    assert "AGENTIC_HIL_RESULT" not in RECORDED_ACCESS_DENIED_STDERR


@pytest.mark.parametrize(("tool", "arguments"), THE_THREE_TOOLS)
def test_a_probe_this_user_may_not_open_is_named_as_such_and_never_contacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict) -> None:
    result = refused_on("posix", tmp_path, monkeypatch, tool, arguments)

    assert result["ok"] is False, result
    assert result["error_type"] == "adapter_not_found", result
    assert result["backend_error_type"] == "adapter_access_denied", result
    assert "LIBUSB_ERROR_ACCESS" in result["summary"], result["summary"]
    assert "may not open" in result["summary"], result["summary"]
    assert re.search(r"\bgroup\b", result["likely_causes"][0]), result["likely_causes"]
    assert any("udev" in cause for cause in result["likely_causes"]), result["likely_causes"]
    assert not any("not connected" in cause for cause in result["likely_causes"]), result["likely_causes"]
    assert any("udev rule" in step for step in result["remediation"]), result["remediation"]
    assert result["target_contacted"] is False, result
    assert result["retry_safe"] is True, result
    assert result["hardware_state"] == "unchanged", result
    assert result["side_effect_status"] == "not_started", result
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantine_id") is None, result
    assert result.get("quarantined") is not True, result
    assert THE_LINE_THE_CLASSIFIER_READS in result["programmer_output"]["stderr"], result["programmer_output"]
    assert result["programmer_output"]["returncode"] == 1, result["programmer_output"]


def test_on_windows_the_same_answer_keeps_the_causes_of_an_adapter_that_did_not_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: on Windows libusb answers a device another program holds with the same error.

    So there the transcript is the `adapter_not_found` it always was, with the
    holder among its causes, and nobody is told to join a group Windows does
    not have.
    """
    result = refused_on("nt", tmp_path, monkeypatch)

    assert result["error_type"] == "adapter_not_found", result
    assert result["backend_error_type"] == "adapter_not_found", result
    assert "debug probe is already in use" in result["likely_causes"], result["likely_causes"]
    assert not any("udev" in cause for cause in result["likely_causes"]), result["likely_causes"]
    assert result["target_contacted"] is False, result


def test_the_rendering_an_operator_reads_names_the_permission_and_carries_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The document a person sees keeps the public name, prints OpenOCD's own line and says what to change."""
    result = refused_on("posix", tmp_path, monkeypatch)

    rendered = render_result(result, "test-reactor")

    assert rendered.splitlines()[0].endswith(": adapter_not_found"), rendered.splitlines()[0]
    assert THE_LINE_THE_CLASSIFIER_READS in rendered, rendered
    assert "may not open" in rendered, rendered
    assert "udev rule" in rendered, rendered
    # And nobody is sent to `recover`: this run provably never reached the bench.
    assert "agentic-hil recover" not in rendered, rendered
