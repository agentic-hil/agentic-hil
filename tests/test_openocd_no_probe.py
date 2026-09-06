"""The first failure every newcomer sees: OpenOCD with the probe not plugged in.

OpenOCD 0.12's stlink driver prints `Error: open failed` and exits 1 before
`init` completes. Read right, that is `adapter_not_found`, and it proves the
run never reached the bench: `target_contacted` false, `retry_safe` true,
`hardware_state` unchanged, nothing quarantined, and the line itself carried on
the result, because it is what the classification, the summary and the causes
were read out of. Read wrong, it is `unknown_debugger_error`, the abort-point
proof is withheld, the lease quarantines and the operator is sent to `recover`
over a board nothing touched: the #425 class of defect one bucket over.

The transcript the fake prints is the recording the container tier took from
the real binary (tests/container/test_openocd_without_a_probe.py, OpenOCD
0.12.0, 2026-09-06). This is the unit tier of the same fact; the container
tier is the truth beside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD_NO_PROBE, write_config
from fixtures.fake_openocd_no_probe import RECORDED_NO_PROBE_STDERR, THE_LINE_THE_CLASSIFIER_READS

from agentic_hil.backends.openocd import OPENOCD_INIT_STAGE_MARKER
from agentic_hil.config import load_config
from agentic_hil.humanize import render_result
from agentic_hil.tools import AgenticHILToolService

THE_THREE_TOOLS = [
    ("probe_target", {}),
    ("reset_target", {"mode": "run"}),
    ("flash_firmware", {"image_path": "build/firmware.elf"}),
]


def a_project_with_an_image(tmp_path: Path):
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    return load_config(str(write_config(tmp_path, debugger_executable=FAKE_OPENOCD_NO_PROBE)))


def test_the_recording_is_a_refusal_before_init_and_carries_the_line() -> None:
    """The premise of every test below, held against the recording itself."""
    assert THE_LINE_THE_CLASSIFIER_READS in RECORDED_NO_PROBE_STDERR
    assert OPENOCD_INIT_STAGE_MARKER not in RECORDED_NO_PROBE_STDERR
    assert "AGENTIC_HIL_RESULT" not in RECORDED_NO_PROBE_STDERR


@pytest.mark.parametrize(("tool", "arguments"), THE_THREE_TOOLS)
def test_an_openocd_with_no_probe_attached_is_adapter_not_found_and_never_contacted(tmp_path: Path, tool: str, arguments: dict) -> None:
    config = a_project_with_an_image(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call(tool, arguments)
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["error_type"] == "adapter_not_found", result
    assert result["backend_error_type"] == "adapter_not_found", result
    assert result["target_contacted"] is False, result
    assert result["retry_safe"] is True, result
    assert result["hardware_state"] == "unchanged", result
    assert result["side_effect_status"] == "not_started", result
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantine_id") is None, result
    assert result.get("quarantined") is not True, result
    assert THE_LINE_THE_CLASSIFIER_READS in result["programmer_output"]["stderr"], result["programmer_output"]
    assert result["programmer_output"]["returncode"] == 1, result["programmer_output"]
    assert any("Connect the probe" in step for step in result["remediation"]), result["remediation"]


def test_the_rendering_an_operator_reads_carries_the_line_and_the_remedy(tmp_path: Path) -> None:
    """The document a person sees names the failure, prints OpenOCD's own line and says what to do."""
    config = a_project_with_an_image(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target", {})
    finally:
        service.close()

    rendered = render_result(result, "test-reactor")

    assert rendered.splitlines()[0].endswith(": adapter_not_found"), rendered.splitlines()[0]
    assert THE_LINE_THE_CLASSIFIER_READS in rendered, rendered
    assert "Connect the probe" in rendered, rendered
    assert "Call `agentic-hil debugger-probes`" in rendered, rendered
    # And nobody is sent to `recover`: this run provably never reached the bench.
    assert "agentic-hil recover" not in rendered, rendered
