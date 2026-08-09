"""The hardware evaluation's MCP probe, driven without hardware.

`evals/hil/` only runs against an attached board, so nothing in this suite
covered it, and the probe's tool-surface comparison sat broken underneath that:
it read the contract from its own directory, where no such file has ever
existed, and raised `FileNotFoundError` before it could compare anything.

A stand-in server is enough to reach the comparison, so these tests reach it.
The probe is otherwise untouched -- real subprocess, real stdio exchange, real
contract file -- because the comparison is the thing that has to work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals" / "hil"))

import mcp_probe  # noqa: E402

FAKE_MCP_SERVER = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def contract_names() -> list[str]:
    """The tool names the probe holds a server to."""
    return sorted(mcp_probe.TOOL_CONTRACT.read_text(encoding="utf-8").split())


def tool(name: str, *, annotated: bool = True) -> dict:
    entry: dict = {"name": name, "description": name, "inputSchema": {"type": "object"}}
    if annotated:
        entry["annotations"] = {"title": name.replace("_", " ")}
    return entry


def run_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: list[dict]) -> int:
    """Run the probe end to end against a server exposing exactly `tools`."""
    manifest = tmp_path / "tools.json"
    manifest.write_text(json.dumps(tools), encoding="utf-8")
    monkeypatch.setattr(mcp_probe, "SERVER_COMMAND", [sys.executable, str(FAKE_MCP_SERVER), str(manifest)])
    monkeypatch.setattr(mcp_probe, "FIXTURE", tmp_path)
    # The probe writes its result next to itself. Only that write is diverted;
    # the contract stays the repository's own file, which is the point.
    monkeypatch.setattr(mcp_probe, "HARNESS", tmp_path)
    return mcp_probe.main()


def probe_result(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "mcp_probe.json").read_text(encoding="utf-8"))


def test_the_probe_reads_the_one_contract_the_repository_keeps() -> None:
    """`evals/install/tools.list.expected` is where the tool contract lives, and
    the shipped tool tables are already held against it in both directions. A
    copy beside the probe would be the next thing to drift."""
    assert mcp_probe.TOOL_CONTRACT == ROOT / "evals" / "install" / "tools.list.expected"
    assert mcp_probe.TOOL_CONTRACT.is_file()
    assert not (ROOT / "evals" / "hil" / "tools.list.expected").exists()


def test_a_matching_tool_surface_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run_probe(monkeypatch, tmp_path, [tool(name) for name in contract_names()]) == 0

    assert "MCP PROBE: ALL PASS" in capsys.readouterr().out
    assert probe_result(tmp_path)["overall"] == "PASS"


def test_the_probe_reports_tool_surface_differences(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The comparison the probe exists for. It used to raise `FileNotFoundError`
    here instead, so a server exposing the wrong tools was never caught."""
    names = contract_names()
    dropped = names[0]
    served = [tool(name) for name in names[1:]] + [tool("tool_that_does_not_belong")]

    assert run_probe(monkeypatch, tmp_path, served) == 1

    output = capsys.readouterr().out
    assert "FAIL: tool surface matches snapshot" in output
    assert f"missing=['{dropped}']" in output
    assert "added=['tool_that_does_not_belong']" in output
    assert probe_result(tmp_path)["overall"] == "FAIL"


def test_a_tool_shipped_without_annotations_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The second check runs on the same exchange; a bare table is the failure
    the annotations prevent, and it has to reach the report as that."""
    names = contract_names()
    served = [tool(name, annotated=name != names[2]) for name in names]

    assert run_probe(monkeypatch, tmp_path, served) == 1

    output = capsys.readouterr().out
    assert "PASS: tool surface matches snapshot" in output
    assert f"FAIL: every tool carries annotations (without annotations=['{names[2]}'])" in output
