"""A plain test run never calls the model.

tests/test_tool_definition_score_model.py scores the real tools/list with the
real model, which costs model calls and needs a login. The plugin
tests/tdqs_model_selection.py, which tests/conftest.py loads into every run,
deselects each test marked `tdqs_model` unless AGENTIC_HIL_TDQS_MODEL is exactly
1, which the score job sets for the one step that runs it. Deselected rather
than skipped: the run reports the test as left out on purpose, and nothing in
it starts.

The sessions below are real ones in a subprocess, with the plugin loaded by
`-p` the way the conftest loads it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from support import scaled_time_bound

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
PLUGIN = "tdqs_model_selection"
SWITCH = "AGENTIC_HIL_TDQS_MODEL"
XDIST_VARIABLES = ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID")
# A hang guard, not a claim about speed: a fresh interpreter takes seconds to
# start on a loaded Windows machine.
SESSION_TIMEOUT_S = 180.0
SAMPLE = '''
import pytest


@pytest.mark.tdqs_model
def test_calls_the_model():
    pass


def test_needs_no_model():
    pass
'''


@pytest.fixture(autouse=True)
def session_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing inherited from the session running this file, and the plugin importable."""
    for name in (*XDIST_VARIABLES, SWITCH, "GITHUB_STEP_SUMMARY", "AGENTIC_HIL_TEST_LEDGER"):
        monkeypatch.delenv(name, raising=False)
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(part for part in (str(TESTS), inherited) if part))


def run_session(pytester: pytest.Pytester) -> pytest.RunResult:
    pytester.makepyfile(test_sample=SAMPLE)
    return pytester.runpytest_subprocess("-p", PLUGIN, "-p", "no:cacheprovider", "-v", timeout=scaled_time_bound(SESSION_TIMEOUT_S))


def test_the_model_test_is_deselected_by_default(pytester: pytest.Pytester) -> None:
    result = run_session(pytester)

    result.assert_outcomes(passed=1, deselected=1)
    result.stdout.fnmatch_lines(["*test_needs_no_model PASSED*"])
    result.stdout.no_fnmatch_line("*test_calls_the_model*PASSED*")


@pytest.mark.parametrize("value", ["0", "", "true", "yes", " 1"])
def test_only_exactly_one_selects_it(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWITCH, value)

    run_session(pytester).assert_outcomes(passed=1, deselected=1)


def test_the_switch_selects_it(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")

    result = run_session(pytester)

    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*test_calls_the_model PASSED*"])


def test_the_suite_loads_the_plugin(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.has_plugin(PLUGIN)


def test_the_marker_is_declared() -> None:
    """Declared where the other tiers are, so `--strict-markers` accepts it."""
    markers = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["markers"]

    assert any(marker.startswith("tdqs_model:") for marker in markers)
