from __future__ import annotations

import pytest
from conftest import SIM_NTC_ADAPTER, write_config

NTC_ADAPTER_YAML = f'''adapters:
  ntc_sim:
    executable: "{SIM_NTC_ADAPTER.as_posix()}"
    channels: ["temperature", "resistance"]
    faults: ["open", "short_to_gnd", "short_to_vcc"]
'''

PLUGIN_ARGS = ("-p", "no:agentic_hil", "-p", "agentic_hil.pytest_plugin")

ADAPTER_LOOP_TEST = """
def test_adapter_loop(agentic_hil):
    started = agentic_hil.call("adapter_session_start", {"adapter_id": "ntc_sim"})
    assert started["ok"] is True
    set_result = agentic_hil.call("adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 85})
    assert set_result["ok"] is True
    measured = agentic_hil.call("adapter_measure", {"adapter_id": "ntc_sim", "channel": "temperature"})
    assert measured["value"] == 85.0
"""


def test_agentic_hil_fixture_runs_adapter_loop(pytester: pytest.Pytester) -> None:
    write_config(pytester.path, adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile(ADAPTER_LOOP_TEST)
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(passed=1)


def test_agentic_hil_fixture_skips_without_config(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("""
def test_needs_hardware(agentic_hil):
    raise AssertionError("must not run without an Agentic HIL configuration")
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(skipped=1)


def test_agentic_hil_fixture_fails_loudly_on_invalid_config(pytester: pytest.Pytester) -> None:
    config_path = pytester.path / ".agentic-hil" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('target:\n  controler: "typo"\n', encoding="utf-8")
    pytester.makepyfile("""
def test_needs_hardware(agentic_hil):
    raise AssertionError("must not run with an invalid Agentic HIL configuration")
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 0, "invalid config must not silently skip"
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("errors", 0) == 1 or outcomes.get("failed", 0) == 1


def test_agentic_hil_config_option_points_to_custom_path(pytester: pytest.Pytester) -> None:
    config_path = write_config(pytester.path / "elsewhere", adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile(ADAPTER_LOOP_TEST)
    result = pytester.runpytest(*PLUGIN_ARGS, "--agentic-hil-config", str(config_path))
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
def test_a_injects_fault_without_cleanup(agentic_hil):
    assert agentic_hil.call("adapter_session_start", {"adapter_id": "ntc_sim"})["ok"] is True
    assert agentic_hil.call("adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})["ok"] is True

def test_b_sees_fresh_adapter_state(agentic_hil):
    assert agentic_hil.call("adapter_session_start", {"adapter_id": "ntc_sim"})["ok"] is True
    measured = agentic_hil.call("adapter_measure", {"adapter_id": "ntc_sim", "channel": "resistance"})
    assert 9000 < measured["value"] < 11000  # 10k NTC at default 25 degC, no fault
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(passed=2)


def test_cli_config_option_is_anchored_to_the_invocation_dir_not_ambient_cwd(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from agentic_hil.pytest_plugin import resolve_plugin_config_path

    (tmp_path / "sub").mkdir()
    (tmp_path / "custom").mkdir()
    (tmp_path / "elsewhere").mkdir()
    # A test that chdirs before the fixture instantiates must not change the anchor.
    monkeypatch.chdir(tmp_path / "elsewhere")
    fake_config = SimpleNamespace(
        getoption=lambda name: "../custom/config.yaml",
        getini=lambda name: None,
        rootpath=tmp_path,
        invocation_params=SimpleNamespace(dir=tmp_path / "sub"),
    )

    resolved = Path(resolve_plugin_config_path(fake_config)).resolve()

    assert resolved == (tmp_path / "custom" / "config.yaml").resolve()


def test_unsafe_cleanup_failure_fails_the_suite(pytester: pytest.Pytester) -> None:
    write_config(pytester.path, adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile("""
def test_leaves_an_unstoppable_session(agentic_hil):
    class StuckBridge:
        last_close_result = None

        def status(self):
            return {"active": True}

        def close(self):
            raise RuntimeError("bridge stuck")

    from agentic_hil.adapters import AdapterSession
    from agentic_hil.types import AdapterConfig

    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["t"], faults=["f"])
    agentic_hil.adapters.sessions["stuck"] = AdapterSession("stuck", adapter_config, StuckBridge(), "stuck.jsonl")
    assert True
""")
    result = pytester.runpytest(*PLUGIN_ARGS)
    result.assert_outcomes(passed=1, errors=1)


def test_audit_only_cleanup_failure_warns_instead_of_failing(pytester: pytest.Pytester) -> None:
    write_config(pytester.path, adapters_yaml=NTC_ADAPTER_YAML)
    pytester.makepyfile("""
def test_confirmed_audit_failure_only(agentic_hil):
    from agentic_hil.report import AuditWriteError

    def audit_failing_close():
        raise AuditWriteError("log full", None, "confirmed")

    agentic_hil.backend.close = audit_failing_close
    assert True
""")
    result = pytester.runpytest(*PLUGIN_ARGS, "-W", "ignore::pytest.PytestUnraisableExceptionWarning")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*cleanup audit records could not be written*"])


def test_custom_layout_config_keeps_the_rootdir_policy_anchor(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    generated = write_config(pytester.path / "generated", adapters_yaml=NTC_ADAPTER_YAML)
    custom = pytester.path / "hil-config.yaml"
    custom.write_text(generated.read_text(encoding="utf-8"), encoding="utf-8")
    test_file = pytester.makepyfile(ADAPTER_LOOP_TEST)
    subdir = pytester.mkdir("sub")
    monkeypatch.chdir(subdir)

    result = pytester.runpytest(*PLUGIN_ARGS, "--agentic-hil-config", str(custom), str(test_file))

    result.assert_outcomes(passed=1)
    assert (pytester.path / ".agentic-hil" / "reports" / "last-report.json").exists(), "policy must anchor at rootdir, not the ambient cwd"
    assert not (subdir / ".agentic-hil").exists()
