from __future__ import annotations

import pytest
from conftest import SIM_NTC_ADAPTER, write_config

NTC_ADAPTER_YAML = f'''adapters:
  ntc_sim:
    executable: "{SIM_NTC_ADAPTER.as_posix()}"
    channels: ["temperature", "resistance"]
    faults: ["open", "short_to_gnd", "short_to_vcc"]
'''

# "-p no:hardci" blocks the installed pytest11 entry point so the explicit
# "-p hardci.pytest_plugin" load does not double-register the plugin module.
PLUGIN_ARGS = ("-p", "no:hardci", "-p", "hardci.pytest_plugin")

ADAPTER_LOOP_TEST = """
def test_adapter_loop(hardci):
    started = hardci.call("hardci_adapter_session_start", {"adapter_id": "ntc_sim"})
    assert started["ok"] is True
    set_result = hardci.call("hardci_adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 85})
    assert set_result["ok"] is True
    measured = hardci.call("hardci_adapter_measure", {"adapter_id": "ntc_sim", "channel": "temperature"})
    assert measured["value"] == 85.0
"""


def test_hardci_fixture_runs_adapter_loop(pytester: pytest.Pytester) -> None:
    write_config(pytester.path, adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile(ADAPTER_LOOP_TEST)
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(passed=1)


def test_hardci_fixture_skips_without_config(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("""
def test_needs_hardware(hardci):
    raise AssertionError("must not run without a HardCI configuration")
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(skipped=1)


def test_hardci_fixture_fails_loudly_on_invalid_config(pytester: pytest.Pytester) -> None:
    config_path = pytester.path / ".hardci" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('target:\n  controler: "typo"\n', encoding="utf-8")
    pytester.makepyfile("""
def test_needs_hardware(hardci):
    raise AssertionError("must not run with an invalid HardCI configuration")
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 0, "invalid config must not silently skip"
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("errors", 0) == 1 or outcomes.get("failed", 0) == 1


def test_hardci_config_option_points_to_custom_path(pytester: pytest.Pytester) -> None:
    config_path = write_config(pytester.path / "elsewhere", adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile(ADAPTER_LOOP_TEST)
    result = pytester.runpytest(*PLUGIN_ARGS, "--hardci-config", str(config_path))
    result.assert_outcomes(passed=1)


def test_relative_config_resolves_against_rootdir(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(pytester.path, adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makeini("[pytest]\n")
    test_file = pytester.makepyfile(ADAPTER_LOOP_TEST)
    subdir = pytester.mkdir("sub")
    monkeypatch.chdir(subdir)
    result = pytester.runpytest(*PLUGIN_ARGS, str(test_file))
    result.assert_outcomes(passed=1)


def test_adapter_state_does_not_leak_between_tests(pytester: pytest.Pytester) -> None:
    write_config(pytester.path, adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile("""
def test_a_injects_fault_without_cleanup(hardci):
    assert hardci.call("hardci_adapter_session_start", {"adapter_id": "ntc_sim"})["ok"] is True
    assert hardci.call("hardci_adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})["ok"] is True

def test_b_sees_fresh_adapter_state(hardci):
    assert hardci.call("hardci_adapter_session_start", {"adapter_id": "ntc_sim"})["ok"] is True
    measured = hardci.call("hardci_adapter_measure", {"adapter_id": "ntc_sim", "channel": "resistance"})
    assert 9000 < measured["value"] < 11000  # 10k NTC at default 25 degC, no fault
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(passed=2)
