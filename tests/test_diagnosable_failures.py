"""A refusal has to name what is wrong, not only that something is.

The finding behind these tests: a pyOCD target type that only a CMSIS pack
provides failed at flash time with no mention of packs, and an agent hunting for
the value escalated into pyOCD's own sources and hand-downloaded a vendor
``.pdsc`` twice.

The tests that matter most here are the negative ones: a host that cannot answer
the target-support question must report "undetermined" rather than "broken". A
diagnostic that lies is worse than one that abstains.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_PYOCD, FAKE_PYOCD_UNKNOWN_TARGET, write_authoritative_config, write_config

from agentic_hil.backends.pyocd import (
    PyOCDBackend,
    normalise_target_type,
    pack_install_commands,
    parse_pyocd_targets,
)
from agentic_hil.cli import doctor
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

# Verbatim from pyOCD 0.45.1 stderr, recorded in a real Nucleo-F446RE session.
# It is here as a literal on purpose: the shipped classifier matched none of it,
# so the whole pack remediation was unreachable on the one message that must
# produce it. Paraphrasing this string would let that happen again.
PYOCD_TARGET_NOT_RECOGNIZED = (
    "0001042 C Target type stm32f446retx not recognized. Use 'pyocd list --targets' to see currently "
    "available target types. See <https://pyocd.io/docs/target_support.html> for how to install "
    "additional target support. [__main__]"
)


def test_pyocd_target_type_names_are_compared_the_way_pyocd_normalises_them() -> None:
    assert normalise_target_type("STM32F446RETx") == "stm32f446retx"
    assert normalise_target_type("stm32f446--retx") == "stm32f446_retx"
    assert normalise_target_type("foo--bar") == "foo_bar"


def test_the_real_pyocd_refusal_is_classified_as_a_target_type_problem(tmp_path: Path) -> None:
    """The regression this whole strand exists for.

    pyOCD 0.45.1's message matched none of the shipped phrases, so the pack
    remediation never reached the caller who needed it.
    """
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32f446retx")))
    backend = PyOCDBackend(config)

    assert backend._classify_output(PYOCD_TARGET_NOT_RECOGNIZED, "flash_firmware") == "target_type_invalid"


def test_an_unrelated_pyocd_warning_is_not_read_as_a_target_type_problem(tmp_path: Path) -> None:
    """pyOCD prints `pyocd list --targets` in a warning about the cortex_m default.

    Matching on that phrase would call a successful run a failure, which is the
    mirror image of the bug being fixed.
    """
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")))
    backend = PyOCDBackend(config)
    warning = (
        "Generic 'cortex_m' target type is selected by default; is this intentional? You will be able to debug "
        "most devices, but not program flash. To set the target type use the '--target' argument or "
        "'target_override' option. Use 'pyocd list --targets' to see available targets types."
    )

    assert backend._classify_output(warning) == "unknown_debugger_error"


def test_a_failure_pyocd_did_not_explain_is_checked_against_the_target_list(tmp_path: Path) -> None:
    """Prose changes between releases; the enumerated list does not.

    An unexplained failure with a target type the backend provably cannot
    resolve is a target-type failure whatever pyOCD wrote about it.
    """
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32h743zi")))
    backend = PyOCDBackend(config)

    assert backend._confirm_target_support("unknown_debugger_error") == "target_type_invalid"


def test_a_failure_is_left_alone_when_the_target_list_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32h743zi")))
    backend = PyOCDBackend(config)
    monkeypatch.setattr(backend, "_enumerate_target_types", lambda: {"ok": False, "reason": "no toolchain here."})

    assert backend._confirm_target_support("unknown_debugger_error") == "unknown_debugger_error"


def test_a_classified_failure_is_never_overwritten_by_the_target_list(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32h743zi")))
    backend = PyOCDBackend(config)

    assert backend._confirm_target_support("probe_not_found") == "probe_not_found"


def test_a_resolvable_target_type_reports_where_it_came_from(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32f446retx")))
    support = PyOCDBackend(config).target_support()

    assert support["ok"] is True
    assert support["status"] == "supported"
    assert support["source"] == "pack"
    assert "CMSIS pack" in support["summary"]


def test_an_unresolvable_target_type_names_the_command_that_installs_the_pack(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32h743zi")))
    support = PyOCDBackend(config).target_support()

    assert support["ok"] is False
    assert support["status"] == "unsupported"
    assert support["error_type"] == "target_type_invalid"
    assert support["install_commands"] == ["pyocd pack find stm32h743zi", "pyocd pack install stm32h743zi", "pyocd pack show"]
    assert any("pyocd pack install" in step for step in support["remediation"])


def test_a_host_that_cannot_answer_reports_undetermined_rather_than_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type="stm32f446retx")))
    backend = PyOCDBackend(config)
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": False})
    support = backend.target_support()

    assert support["ok"] is True
    assert support["status"] == "undetermined"
    assert "could not be found" in support["undetermined_reason"]


def test_an_unset_target_type_is_reported_without_being_called_a_fault(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")))
    support = PyOCDBackend(config).target_support()

    assert support["ok"] is True
    assert support["status"] == "not_configured"


def test_backends_without_a_target_type_answer_the_question_too(tmp_path: Path) -> None:
    from agentic_hil.backends.openocd import OpenOCDBackend
    from agentic_hil.backends.stlink import STLinkBackend

    openocd = OpenOCDBackend(load_config(str(write_config(tmp_path / "a")))).target_support()
    stlink = STLinkBackend(load_config(str(write_config(tmp_path / "b", debugger_type="stlink")))).target_support()

    assert openocd["status"] == "not_applicable"
    assert openocd["ok"] is True
    assert "target_cfg" in openocd["summary"]
    assert stlink["status"] == "not_applicable"
    assert stlink["ok"] is True


@pytest.mark.parametrize(
    "output",
    [
        "not json at all",
        json.dumps({"status": 1, "targets": []}),
        json.dumps({"status": 0}),
        json.dumps({"status": 0, "targets": [{"vendor": "STMicroelectronics"}]}),
        json.dumps({"status": 0, "targets": []}),
    ],
)
def test_unreadable_target_output_is_a_reason_not_an_empty_target_list(output: str) -> None:
    """An empty list would read as "nothing resolves" and condemn a working config."""
    parsed = parse_pyocd_targets(output)

    assert parsed["ok"] is False
    assert parsed["reason"]


def test_pack_install_commands_never_run_anything() -> None:
    commands = pack_install_commands("STM32F446RETx")

    assert commands[0].startswith("pyocd pack find ")
    assert commands == ["pyocd pack find stm32f446retx", "pyocd pack install stm32f446retx", "pyocd pack show"]


def test_the_probe_refusal_carries_the_pack_command(tmp_path: Path) -> None:
    """#66 stage 2: the fix belongs in the answer, not only in `doctor`.

    Driven through the real backend against a pyOCD that emits the real message,
    so the classifier, the catalogue lookup and the substituted command are
    exercised together rather than asserted in isolation.
    """
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", debugger_executable=FAKE_PYOCD_UNKNOWN_TARGET, probe_id="PYOCD123", target_type="stm32f446retx")))
    service = AgenticHILToolService(config)
    try:
        refused = service.call("probe_target")
    finally:
        service.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "target_type_invalid"
    assert refused["install_commands"] == ["pyocd pack find stm32f446retx", "pyocd pack install stm32f446retx", "pyocd pack show"]
    assert any("pyocd pack install" in step for step in refused["remediation"])
    assert any(".pdsc" in step for step in refused["do_not"])


def test_the_flash_refusal_carries_the_pack_command(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", debugger_executable=FAKE_PYOCD_UNKNOWN_TARGET, probe_id="PYOCD123", target_type="stm32f446retx")))
    service = AgenticHILToolService(config)
    try:
        refused = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "target_type_invalid"
    assert "pyocd pack install stm32f446retx" in refused["install_commands"]


def test_doctor_reports_target_support_beside_the_probe_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_type="pyocd", debugger_executable=FAKE_PYOCD, probe_id="PYOCD123", target_type="stm32f446retx")
    monkeypatch.chdir(workspace)

    report = doctor()

    assert report["ok"] is True
    assert report["debuggers"]["dut"]["target_support"]["status"] == "supported"
    assert report["debuggers"]["dut"]["target_support"]["source"] == "pack"


def test_doctor_fails_on_a_target_type_the_backend_cannot_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_type="pyocd", debugger_executable=FAKE_PYOCD, probe_id="PYOCD123", target_type="stm32h743zi")
    monkeypatch.chdir(workspace)

    report = doctor()

    support = report["debuggers"]["dut"]["target_support"]
    assert report["ok"] is False
    assert "target_type is not resolvable" in report["summary"]
    assert support["status"] == "unsupported"
    assert "pyocd pack install stm32h743zi" in support["install_commands"]


def test_doctor_stays_green_when_target_support_cannot_be_determined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check must not punish a host that simply has no pack cache yet.

    `setup` rolls back on a red `doctor`, so an undeterminable answer reported
    as a failure would break installation on exactly the fresh machine the check
    is meant to help. The doctor summary still says the question went
    unanswered, so nobody reads a green run as proof the target resolves.
    """
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_type="pyocd", debugger_executable=FAKE_PYOCD, probe_id="PYOCD123", target_type="stm32f446retx")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        PyOCDBackend,
        "_enumerate_target_types",
        lambda self: {"ok": False, "reason": "no CMSIS pack cache exists on this host."},
    )

    report = doctor()

    support = report["debuggers"]["dut"]["target_support"]
    assert report["ok"] is True
    assert support["ok"] is True
    assert support["status"] == "undetermined"
    assert "no CMSIS pack cache" in support["undetermined_reason"]
    assert "could not be determined here" in report["summary"]
