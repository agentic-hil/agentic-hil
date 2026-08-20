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


def test_agentic_hil_fixture_fails_loudly_on_missing_explicit_config(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str((pytester.path / "missing.yaml").resolve()))
    pytester.makepyfile(SERVICE_TEST)

    result = pytester.runpytest(*PLUGIN_ARGS, "--rootdir", str(pytester.path))

    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 0
    assert outcomes.get("errors", 0) == 1 or outcomes.get("failed", 0) == 1


def test_legacy_pytest_config_option_accepts_authoritative_path(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_authoritative_config(pytester.path, monkeypatch)
    pytester.makepyfile(SERVICE_TEST)

    result = pytester.runpytest(*PLUGIN_ARGS, "--rootdir", str(pytester.path), "--agentic-hil-config", str(config_path))

    result.assert_outcomes(passed=1)


def test_legacy_pytest_config_option_cannot_redirect_authority(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_authoritative_config(pytester.path, monkeypatch)
    legacy = pytester.path / ".agentic-hil" / "config.yaml"
    legacy.parent.mkdir()
    legacy.write_text("workspace_root: ignored\n", encoding="utf-8")
    pytester.makepyfile(SERVICE_TEST)

    result = pytester.runpytest(*PLUGIN_ARGS, "--rootdir", str(pytester.path), "--agentic-hil-config", str(legacy))

    assert "cannot change policy authority" in result.stdout.str()


def test_the_legacy_selector_refusal_names_the_deprecation_and_the_way_out(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loudest behaviour this plugin has was documented nowhere and said least.

    A suite that passes a path is not quietly ignored, it fails the session, and
    the message said only "remove the legacy option" without saying that the
    option is deprecated, that failing is the behaviour rather than an accident,
    or which of the two supported ways of selecting a configuration to use
    instead. `docs/configuration.md` was the single page in the tree that
    mentioned these options at all, and it promised the opposite outcome.

    Behaviour is unchanged, deliberately: a repository-controlled flag that
    redirected policy authority would decide what this bench may be told to do,
    and a silent fallback would run the suite under a policy nobody chose."""
    write_authoritative_config(pytester.path, monkeypatch)
    legacy = pytester.path / ".agentic-hil" / "config.yaml"
    legacy.parent.mkdir()
    legacy.write_text("workspace_root: ignored\n", encoding="utf-8")
    pytester.makepyfile(SERVICE_TEST)

    result = pytester.runpytest(*PLUGIN_ARGS, "--rootdir", str(pytester.path), "--agentic-hil-config", str(legacy))
    output = result.stdout.str()

    assert result.parseoutcomes().get("passed", 0) == 0
    # Named: what the option is now.
    assert "--agentic-hil-config" in output
    assert "agentic_hil_config" in output
    assert "are deprecated" in output
    # Named: that this is the behaviour, not a fallback that failed.
    assert "fails the session rather than falling back" in output
    # Named: both ways out, the one that needs no option at all first.
    assert "Drop the option" in output
    assert "AGENTIC_HIL_CONFIG" in output


def test_the_deprecated_selector_says_in_help_what_it_does(pytester: pytest.Pytester) -> None:
    """`--help` is where a CI author meets this option before CI does."""
    output = pytester.runpytest(*PLUGIN_ARGS, "--help").stdout.str()

    assert "Deprecated" in output
    assert "fails the session" in output
