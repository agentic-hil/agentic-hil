from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.container import cubeprogrammer_without_a_probe as smoke

VM_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stm32cubeprogrammer_2_23_0_vm_36440193292_recordings.json"


def _recordings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["recordings"]


def test_cubeprogrammer_vm_recording_has_run_and_source_provenance() -> None:
    fixture = json.loads(VM_FIXTURE.read_text(encoding="utf-8"))

    assert fixture["provenance"]["workflow_run_id"] == 36440193292
    assert fixture["provenance"]["source_commit"] == "cc96acb2a7a65211c75f65c09343d97f9f45f753"
    assert fixture["provenance"]["installer_archive_sha256"] == "6a9e60a5a048c45eb3241f9bb66bdc2e6cbd0119fb2e42568dc059fc6167442a"
    assert "no USB device nodes passed through" in fixture["provenance"]["environment"]
    assert set(fixture["recordings"]) == {"version", "list_no_probe", "connect_no_probe"}
    assert fixture["recordings"]["list_no_probe"]["returncode"] == 0
    assert fixture["recordings"]["connect_no_probe"]["returncode"] == 1
    assert "/dev/bus/usb/002/002, errno=2" in fixture["recordings"]["list_no_probe"]["stderr"]
    assert fixture["recordings"]["list_no_probe"]["stderr"] == fixture["recordings"]["connect_no_probe"]["stderr"]


@pytest.mark.parametrize("name", ["version", "list_no_probe", "connect_no_probe"])
def test_cubeprogrammer_smoke_matches_only_complete_recorded_stdout_stderr_pairs(name: str) -> None:
    original = _recordings(smoke.FIXTURE)[name]
    vm = _recordings(VM_FIXTURE)[name]

    assert smoke.output_matches_recording(name, original["stdout"], original["stderr"])
    assert smoke.output_matches_recording(name, vm["stdout"], vm["stderr"])
    if name != "version":
        assert not smoke.output_matches_recording(name, original["stdout"], vm["stderr"])
        assert not smoke.output_matches_recording(name, vm["stdout"], original["stderr"])
        wrong_stderr = vm["stderr"].replace("errno=2", "errno=3")
        assert not smoke.output_matches_recording(name, vm["stdout"], wrong_stderr)


def test_cubeprogrammer_smoke_allows_only_usb_bus_device_renumbering_in_recorded_libusb_stderr() -> None:
    vm = _recordings(VM_FIXTURE)["list_no_probe"]
    renumbered = vm["stderr"].replace("/dev/bus/usb/002/002", "/dev/bus/usb/123/456")

    assert smoke.output_matches_recording("list_no_probe", vm["stdout"], renumbered)
    assert not smoke.output_matches_recording("list_no_probe", vm["stdout"], renumbered.replace("errno=2", "errno=13"))
    assert not smoke.output_matches_recording("list_no_probe", vm["stdout"], renumbered.replace("/dev/bus/usb/123/456", "/tmp/usb-device"))
    assert not smoke.output_matches_recording("list_no_probe", vm["stdout"], renumbered + "unexpected diagnostic\n")
    connect = _recordings(VM_FIXTURE)["connect_no_probe"]
    assert not smoke.output_matches_recording(
        "connect_no_probe",
        connect["stdout"].replace("DEV_NO_STLINK", "DEV_OTHER"),
        connect["stderr"],
    )


def test_cubeprogrammer_smoke_replays_both_vm_stdout_and_stderr_for_all_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    records = _recordings(smoke.FIXTURE)
    vm_records = _recordings(VM_FIXTURE)
    executable = tmp_path / "STM32_Programmer_CLI"
    executable.write_text("test double", encoding="utf-8")
    monkeypatch.setattr(smoke, "CUBE_CLI", executable)
    responses = [
        SimpleNamespace(returncode=record["returncode"], stdout=record["stdout"], stderr=record["stderr"])
        for record in vm_records.values()
    ]
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return responses[len(calls) - 1]

    monkeypatch.setattr(smoke.subprocess, "run", run)

    observed = smoke.run_cli_smoke(records)

    assert len(calls) == 3, calls
    assert observed["list_no_probe"]["stderr"] == vm_records["list_no_probe"]["stderr"]
    assert observed["connect_no_probe"]["stdout"] == vm_records["connect_no_probe"]["stdout"]
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["recordings"] == observed


def test_cubeprogrammer_smoke_accepts_only_the_observed_no_stlink_notice() -> None:
    fixture = json.loads(smoke.FIXTURE.read_text(encoding="utf-8"))
    vm_fixture = json.loads(VM_FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["recordings"]["list_no_probe"]["stdout"]
    observed_notice = "ST-LINK error (DEV_NO_STLINK)"
    observed = expected.replace("No ST-Link detected!", f"{observed_notice}\nNo ST-Link detected!", 1)

    assert smoke.output_matches_recording("list_no_probe", observed, vm_fixture["recordings"]["list_no_probe"]["stderr"])
    assert not smoke.output_matches_recording("list_no_probe", expected.replace("No ST-Link detected!", f"{observed_notice}\n{observed_notice}\nNo ST-Link detected!", 1), "")
    assert not smoke.output_matches_recording("list_no_probe", f"{observed_notice}\n" + expected, "")
    assert not smoke.output_matches_recording("list_no_probe", observed.replace(observed_notice, "ST-LINK error (OTHER)", 1), vm_fixture["recordings"]["list_no_probe"]["stderr"])
    assert not smoke.output_matches_recording("version", observed_notice + fixture["recordings"]["version"]["stdout"], "")


def test_cubeprogrammer_smoke_emits_all_command_results_before_a_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A VM-only transcript difference must still expose every actual command result."""
    fixture = {
        "provenance": {"version": "2.23.0"},
        "recordings": {
            "version": {"argv": ["--version"], "returncode": 0, "stdout": "version output", "stderr": ""},
            "list_no_probe": {"argv": ["-q", "-l", "st-link-only"], "returncode": 0, "stdout": "listing output", "stderr": ""},
            "connect_no_probe": {"argv": ["-q", "-c", "port=SWD"], "returncode": 1, "stdout": "connect output", "stderr": ""},
        }
    }
    fixture_path = tmp_path / "recordings.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    executable = tmp_path / "STM32_Programmer_CLI"
    executable.write_text("test double", encoding="utf-8")
    monkeypatch.setattr(smoke, "FIXTURE", fixture_path)
    monkeypatch.setattr(smoke, "CUBE_CLI", executable)
    responses = [
        SimpleNamespace(returncode=0, stdout="version output", stderr=""),
        SimpleNamespace(returncode=0, stdout="listing output differs", stderr=""),
        SimpleNamespace(returncode=1, stdout="connect output", stderr=""),
    ]
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return responses[len(calls) - 1]

    monkeypatch.setattr(smoke.subprocess, "run", run)

    with pytest.raises(AssertionError):
        smoke.test_installed_cubeprogrammer_matches_recorded_usb_free_results_and_product_refusals(tmp_path)

    assert len(calls) == 3, calls
    emitted = json.loads(capsys.readouterr().out)
    assert [record["argv"] for record in emitted["recordings"].values()] == [record["argv"] for record in fixture["recordings"].values()]
    assert emitted["recordings"]["list_no_probe"]["stdout"] == "listing output differs"
    assert emitted["recordings"]["connect_no_probe"]["stdout"] == "connect output"
