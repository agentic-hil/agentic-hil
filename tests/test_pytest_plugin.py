from __future__ import annotations

import pytest
from conftest import write_authoritative_config

PLUGIN_ARGS = ("-p", "no:agentic_hil", "-p", "agentic_hil.pytest_plugin")

SERVICE_TEST = """
def test_service(agentic_hil):
    result = agentic_hil.call("debugger_info")
    assert result["ok"] is True
"""


def test_agentic_hil_fixture_runs_with_external_config_bound_to_root(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_authoritative_config(pytester.path, monkeypatch)
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")
    pytester.makepyfile(SERVICE_TEST)
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(passed=1)


def test_agentic_hil_fixture_skips_without_config(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    pytester.makepyfile("""
def test_needs_hardware(agentic_hil):
    raise AssertionError("must not run without an Agentic HIL configuration")
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(skipped=1)


def test_agentic_hil_fixture_fails_loudly_on_invalid_set_config(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_authoritative_config(pytester.path, monkeypatch)
    config_path.write_text(
        f"workspace_root: {str(pytester.path.resolve())!r}\ntarget:\n  controler: \"typo\"\n",
        encoding="utf-8",
    )
    pytester.makepyfile("""
def test_needs_hardware(agentic_hil):
    raise AssertionError("must not run with an invalid Agentic HIL configuration")
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 0, "invalid config must not silently skip"
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("errors", 0) == 1 or outcomes.get("failed", 0) == 1


def test_agentic_hil_fixture_fails_for_different_workspace(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_workspace = pytester.path.parent / "other-workspace"
    write_authoritative_config(other_workspace, monkeypatch)
    pytester.makepyfile(SERVICE_TEST)
    result = pytester.runpytest(*PLUGIN_ARGS)
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("errors", 0) == 1 or outcomes.get("failed", 0) == 1
