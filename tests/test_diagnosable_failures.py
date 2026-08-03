"""A refusal has to name what is wrong, not only that something is.

Two findings drive these tests, and both are about a caller who was told "no"
and could not act on it.

* A Windows path refusal named an opaque ``S-1-15-3-...`` principal, so nobody
  could tell who held the right or whether to care (hardci-hq #64).
* A pyOCD target type that only a CMSIS pack provides failed at flash time with
  no mention of packs, and an agent hunting for the value escalated into pyOCD's
  own sources and hand-downloaded a vendor ``.pdsc`` twice (hardci-hq #66).

The tests that matter most here are the negative ones: an unresolvable SID must
still produce a refusal rather than an exception, and a host that cannot answer
the target-support question must report "undetermined" rather than "broken".
A diagnostic that lies is worse than one that abstains.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import FAKE_PYOCD, FAKE_PYOCD_UNKNOWN_TARGET, write_authoritative_config, write_config

from agentic_hil import windows_principals
from agentic_hil.backends.pyocd import (
    PyOCDBackend,
    normalise_target_type,
    pack_install_commands,
    parse_pyocd_targets,
)
from agentic_hil.cli import doctor
from agentic_hil.config import ConfigError, load_config
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.windows_principals import (
    describe_principal,
    package_sid_for_capability,
    principal_label,
    untrusted_principal_details,
)

CAPABILITY_SID = "S-1-15-3-3557520199-3666692283-3112367039-3524159787-2791857073-3163583606-3692855932"
PACKAGE_SID = "S-1-15-2-3557520199-3666692283-3112367039-3524159787-2791857073-3163583606-3692855932"
PACKAGE_FULL_NAME = "Example_1.2.3.0_x64__pzs8sxrjxfjjc"

# Verbatim from pyOCD 0.45.1 stderr, recorded in a real Nucleo-F446RE session.
# It is here as a literal on purpose: the shipped classifier matched none of it,
# so the whole pack remediation was unreachable on the one message that must
# produce it. Paraphrasing this string would let that happen again.
PYOCD_TARGET_NOT_RECOGNIZED = (
    "0001042 C Target type stm32f446retx not recognized. Use 'pyocd list --targets' to see currently "
    "available target types. See <https://pyocd.io/docs/target_support.html> for how to install "
    "additional target support. [__main__]"
)


@pytest.fixture
def resolvable_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry that answers, without needing this machine to have the package."""
    monkeypatch.setattr(windows_principals, "_on_windows", lambda: True)
    monkeypatch.setattr(windows_principals, "_account_name", lambda sid: None)
    monkeypatch.setattr(windows_principals, "_appcontainer_mapping", lambda sid: {"Moniker": "example_pzs8sxrjxfjjc", "DisplayName": "Example"})
    monkeypatch.setattr(windows_principals, "_package_full_name", lambda sid: PACKAGE_FULL_NAME)


def test_capability_sid_maps_onto_the_package_sid_that_shares_its_sub_authorities() -> None:
    assert package_sid_for_capability(CAPABILITY_SID) == PACKAGE_SID
    assert package_sid_for_capability("S-1-5-32-544") is None


def test_a_resolvable_capability_sid_reports_the_package_that_holds_the_right(resolvable_package: None) -> None:
    described = describe_principal(CAPABILITY_SID)

    assert described["sid"] == CAPABILITY_SID
    assert described["kind"] == "app_capability"
    assert described["package_sid"] == PACKAGE_SID
    assert described["package"] == PACKAGE_FULL_NAME
    assert described["package_family"] == "example_pzs8sxrjxfjjc"
    assert described["display_name"] == "Example"
    assert principal_label(described) == f"package {PACKAGE_FULL_NAME} (Example)"


def test_the_refusal_names_the_package_and_offers_the_choice_without_touching_acls(resolvable_package: None) -> None:
    details = untrusted_principal_details([CAPABILITY_SID])

    assert details["untrusted_principals"] == [describe_principal(CAPABILITY_SID)]
    summary = details["untrusted_principals_summary"]
    assert PACKAGE_FULL_NAME in summary
    assert "permitted location" in summary
    assert "the application that holds it" in summary
    assert "Do not edit the ACL" in summary


def test_an_unresolvable_capability_sid_still_reports_the_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SID nobody can name is still reported, and the refusal is unchanged."""
    monkeypatch.setattr(windows_principals, "_on_windows", lambda: True)
    monkeypatch.setattr(windows_principals, "_account_name", lambda sid: None)
    monkeypatch.setattr(windows_principals, "_appcontainer_mapping", lambda sid: {})
    monkeypatch.setattr(windows_principals, "_package_full_name", lambda sid: None)

    described = describe_principal(CAPABILITY_SID)

    assert described["sid"] == CAPABILITY_SID
    assert "package" not in described
    assert "display_name" not in described
    assert principal_label(described) == CAPABILITY_SID
    assert CAPABILITY_SID in untrusted_principal_details([CAPABILITY_SID])["untrusted_principals_summary"]


def test_a_lookup_that_raises_never_becomes_the_reported_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming the holder is a courtesy; refusing the path is the duty.

    A registry the process may not read must not turn `unsafe_configured_path`
    into an unhandled exception from the code that was trying to be helpful.
    """

    def explode(sid: str):
        raise OSError("registry unavailable")

    monkeypatch.setattr(windows_principals, "_on_windows", lambda: True)
    monkeypatch.setattr(windows_principals, "_account_name", explode)
    monkeypatch.setattr(windows_principals, "_appcontainer_mapping", explode)
    monkeypatch.setattr(windows_principals, "_package_full_name", explode)

    assert describe_principal(CAPABILITY_SID) == {"sid": CAPABILITY_SID}
    assert untrusted_principal_details([CAPABILITY_SID])["untrusted_principals"] == [{"sid": CAPABILITY_SID}]


def test_nothing_is_reported_when_no_principal_could_be_named() -> None:
    """A refusal with no readable SID stays a refusal and grows no empty fields."""
    assert untrusted_principal_details([]) == {}
    assert untrusted_principal_details([""]) == {}


def test_posix_never_reaches_the_windows_only_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(sid: str):
        raise AssertionError("Windows-only registry lookup reached off Windows")

    monkeypatch.setattr(windows_principals, "_on_windows", lambda: False)
    monkeypatch.setattr(windows_principals, "_account_name", forbidden)
    monkeypatch.setattr(windows_principals, "_appcontainer_mapping", forbidden)
    monkeypatch.setattr(windows_principals, "_package_full_name", forbidden)

    assert describe_principal(CAPABILITY_SID) == {"sid": CAPABILITY_SID}
    assert untrusted_principal_details([CAPABILITY_SID])["untrusted_principals"] == [{"sid": CAPABILITY_SID}]


def test_no_module_imports_winreg_at_import_time() -> None:
    """`import agentic_hil` must not pull in a Windows-only module.

    Asserted structurally rather than by watching sys.modules, because a POSIX
    run cannot import winreg at all and a Windows run has it loaded already, so
    neither platform would notice the mistake at runtime.
    """
    def imports_winreg(node: ast.AST) -> bool:
        if isinstance(node, ast.Import):
            return any(alias.name.split(".")[0] == "winreg" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").split(".")[0] == "winreg"
        return False

    package = Path(windows_principals.__file__).parent
    offenders: list[str] = []
    found_any = False
    for source in sorted(package.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        deferred = {
            id(node)
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
        }
        for node in ast.walk(tree):
            if not imports_winreg(node):
                continue
            found_any = True
            if id(node) not in deferred:
                offenders.append(f"{source.name}:{node.lineno}")

    assert found_any, "no winreg import found at all: this guard would pass vacuously"
    assert offenders == [], f"winreg imported at module level in: {', '.join(offenders)}"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL and SID semantics")
def test_a_real_windows_refusal_names_the_principal_holding_the_right(tmp_path: Path) -> None:
    """End to end against the real ACL walker, with a SID Windows can name.

    Everyone (S-1-1-0) stands in for the capability SID: it is a principal the
    check rejects and the local security authority can name, so this exercises
    the SID stringification and the account lookup without depending on which
    packages happen to be installed on the runner.
    """
    path = write_config(tmp_path)
    config = load_config(str(path))
    root = Path(config.state_root)
    grant = subprocess.run(["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)M"], capture_output=True, text=True, check=False)
    if grant.returncode != 0:
        pytest.skip(f"could not set temporary test ACL: {grant.stderr}")
    try:
        with pytest.raises(ConfigError) as refused:
            load_config(str(path))
        details = refused.value.to_dict()
        assert details["error_type"] == "unsafe_configured_path"
        assert [entry["sid"] for entry in details["untrusted_principals"]] == ["S-1-1-0"]
        assert "S-1-1-0" in details["untrusted_principals_summary"]
    finally:
        subprocess.run(["icacls", str(root), "/remove:g", "*S-1-1-0"], capture_output=True, check=False)


@pytest.mark.skipif(os.name != "nt", reason="the package registry is a Windows construct")
def test_a_capability_sid_registered_on_this_machine_resolves_to_its_package() -> None:
    """The real registry route, against whatever package this host has.

    Skipped rather than faked when the host registers no packaged application,
    so a machine that cannot answer the question does not fail the suite for it.
    """
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, windows_principals._CAP_AUTHZ_APPLICATIONS) as root:
            package_name = winreg.EnumKey(root, 0)
            with winreg.OpenKey(root, package_name) as package_key:
                package_sid, _ = winreg.QueryValueEx(package_key, "PackageSid")
    except OSError:
        pytest.skip("this host registers no packaged application capability SIDs")
    if not isinstance(package_sid, str) or not package_sid.startswith(windows_principals.PACKAGE_SID_PREFIX):
        pytest.skip(f"the first registered package carries no package SID to invert: {package_sid!r}")

    capability_sid = windows_principals.CAPABILITY_SID_PREFIX + package_sid[len(windows_principals.PACKAGE_SID_PREFIX) :]
    described = describe_principal(capability_sid)

    assert described["package_sid"] == package_sid
    assert described["package"] == package_name


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
