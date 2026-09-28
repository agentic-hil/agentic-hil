from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.container import cubeprogrammer_without_a_probe as smoke


def test_cubeprogrammer_smoke_accepts_only_the_observed_no_stlink_notice() -> None:
    fixture = json.loads(smoke.FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["recordings"]["list_no_probe"]["stdout"]
    observed_notice = "ST-LINK error (DEV_NO_STLINK)"
    observed = expected.replace("No ST-Link detected!", f"{observed_notice}\nNo ST-Link detected!", 1)

    assert smoke.output_matches_recording("list_no_probe", observed)
    assert not smoke.output_matches_recording("list_no_probe", expected.replace("No ST-Link detected!", f"{observed_notice}\n{observed_notice}\nNo ST-Link detected!", 1))
    assert not smoke.output_matches_recording("list_no_probe", f"{observed_notice}\n" + expected)
    assert not smoke.output_matches_recording("list_no_probe", observed.replace(observed_notice, "ST-LINK error (OTHER)", 1))
    assert not smoke.output_matches_recording("version", observed_notice + fixture["recordings"]["version"]["stdout"])


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
