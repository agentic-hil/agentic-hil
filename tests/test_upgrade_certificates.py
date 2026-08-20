"""`agentic-hil upgrade` behind the TLS-inspecting proxy the installer already handles.

A bench whose network re-signs TLS gets `invalid peer certificate: UnknownIssuer`
out of uv, because uv validates against roots compiled into its own binary while
curl and the system package manager on the same host read the machine's store and
keep working. `install.sh` has recognised that signature since #293 and retries
the manager once against the machine's own store. The upgrade command runs the
same managers against the same index, and used to do neither: no detection, no
retry, and a summary that stopped at "Agentic HIL package manager reported an
upgrade failure" while the certificate chain sat in a field the human rendering
did not print.

What is pinned here is the whole of that answer, in both directions:

*It retries exactly the failure the installer retries.* The signature list and
the bundle search order are a deliberate mirror of `trust_failure()` and
`system_cert_bundle()` in install.sh, and the last test in this file reads that
script and compares the two copies, so a change to one that is not made to the
other fails here rather than on somebody's proxied bench.

*It retries once, and only where there is something new to try.* A failure that
names no certificate is never retried. An operator who already exported
`UV_SYSTEM_CERTS` or `PIP_CERT` is not overridden and gets no second attempt
either, because the variable they set was in force for the attempt that just
failed and setting it again would repeat a run rather than retry it.

*It says what it did.* The success after a retry names the store that made it
work; the refusal after both attempts names the cause, carries both attempts'
own words nested under `install`, and points at the standing export
TROUBLESHOOTING recommends.

Nothing here runs a package manager. The manager, the process runner and the
bundle search are all replaced, which is also what lets the pip half of this run
on a host that has no `/etc/ssl` at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from test_server_upgrade import upgradable_config

from agentic_hil import __version__
from agentic_hil.humanize import render_result
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.upgrade import CLI_UPGRADE_TOOL, SERVER_UPGRADE, replace_installation

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

UV_TOOL_UPGRADE = ["uv.exe", "tool", "upgrade", "agentic-hil"]
PIP_UPGRADE = [sys.executable, "-m", "pip", "install", "--upgrade", "agentic-hil"]
PIP_WOULD_INSTALL_A_RELEASE = subprocess.CompletedProcess[str]([], 0, json.dumps({"version": "1", "install": [{"metadata": {"name": "agentic-hil", "version": "9.9.9"}}]}), "")

# What the reporting bench's uv said on 2026-08-20, verbatim from #326. The
# decisive line is the last one, and it is at the bottom of a stack of five
# `Caused by:` lines, which is exactly why it has to survive to the operator.
UV_TRUST_FAILURE = subprocess.CompletedProcess[str](
    [],
    2,
    "",
    "error: Failed to upgrade agentic-hil\n"
    "  Caused by: Request failed after 3 retries in 9.6s\n"
    "  Caused by: Failed to fetch: `https://pypi.org/simple/agentic-hil/`\n"
    "  Caused by: error sending request for url (https://pypi.org/simple/agentic-hil/)\n"
    "  Caused by: client error (Connect)\n"
    "  Caused by: invalid peer certificate: UnknownIssuer",
)
# The same failure again, from the second attempt: a store that does not carry
# the proxy's CA either fails identically, which is the end the refusal has to
# tell apart from the first one.
UV_TRUST_FAILURE_AGAIN = subprocess.CompletedProcess[str]([], 2, "", "error: Failed to upgrade agentic-hil\n  Caused by: invalid peer certificate: UnknownIssuer")
PIP_TRUST_FAILURE = subprocess.CompletedProcess[str](
    [],
    1,
    "",
    "SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)'))",
)
MANAGER_INSTALLED = subprocess.CompletedProcess[str]([], 0, "installed\n", "")


class Attempt:
    """One recorded call into the package manager: what was run, and with what."""

    def __init__(self, command: list[str], env: dict[str, str] | None) -> None:
        self.command = command
        self.env = env

    @property
    def inherited(self) -> bool:
        """Whether this child was given no environment of its own, which is the default."""
        return self.env is None

    @property
    def certificate_variables(self) -> dict[str, str]:
        """Only the two variables this feature is allowed to add to a child."""
        return {name: value for name, value in (self.env or {}).items() if name in {"UV_SYSTEM_CERTS", "PIP_CERT"}}


def stub_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answers: list[subprocess.CompletedProcess[str]],
    manager: str = "uv",
    command: list[str] | None = None,
    version_after: str = "9.9.9",
    resolution: subprocess.CompletedProcess[str] = PIP_WOULD_INSTALL_A_RELEASE,
) -> list[Attempt]:
    """A package manager with a queue of canned answers, recording every call.

    `answers` is consumed in order by the mutating invocations only, so a test
    that offers one answer and gets two calls fails on the missing answer rather
    than on an assertion at the end: running the manager twice when it was meant
    to run once is the defect, and it is caught where it happens.

    The resolution query and the import check answer themselves. Neither is a
    command that can replace an installation, and neither is what these tests are
    about.
    """
    attempts: list[Attempt] = []
    queued = list(answers)

    def run(invoked: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if "--dry-run" in invoked:
            return resolution
        if invoked[-1] == "--version":
            return subprocess.CompletedProcess([], 0, f"{version_after}\n", "")
        attempts.append(Attempt(invoked, env))
        if not queued:
            pytest.fail(f"the upgrade ran the package manager once more than it was given an answer for: {invoked}")
        return queued.pop(0)

    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: (manager, command or UV_TOOL_UPGRADE))
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", list)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", tuple)
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", run)
    return attempts


@pytest.fixture(autouse=True)
def _no_inherited_certificate_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench that exported one of these would otherwise decide these tests.

    Every test here states for itself whether the operator has already made that
    choice, so the machine the suite runs on must not make it for them.
    """
    monkeypatch.delenv("UV_SYSTEM_CERTS", raising=False)
    monkeypatch.delenv("PIP_CERT", raising=False)


# ---------------------------------------------------------------------------
# The retry, and the two ends it can have.


def test_a_trust_failure_is_retried_once_against_this_machines_own_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported transcript, answered instead of merely reported.

    uv fails on a certificate it cannot trace to a root it carries, the manager
    is run a second time with `UV_SYSTEM_CERTS=1` in its environment and nothing
    else changed, and that attempt upgrades the machine. The result has to say
    which store made it work: an operator who is only told the upgrade succeeded
    learns nothing about the proxy that will meet the next one.
    """
    attempts = stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, MANAGER_INSTALLED])

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert result["ok"] is True
    assert result["upgraded_on_disk"] is True
    assert result["version"] == "9.9.9"
    # Twice, with the same command, and the second differing from the first in
    # exactly one variable.
    assert [attempt.command for attempt in attempts] == [UV_TOOL_UPGRADE, UV_TOOL_UPGRADE]
    assert attempts[0].certificate_variables == {}
    assert attempts[1].certificate_variables == {"UV_SYSTEM_CERTS": "1"}
    # Said twice on purpose and in two lengths: the paragraph in the summary a
    # person reads first, one clause in the field that survives a surface
    # rewriting that summary.
    assert "TLS-intercepting proxy" in result["summary"]
    assert "UV_SYSTEM_CERTS=1" in result["summary"]
    assert "second attempt is the one that succeeded" in result["summary"]
    assert result["certificates"] == "Retried once against this machine's own certificate store with UV_SYSTEM_CERTS=1, and that is the attempt that succeeded."
    # And the standing fix, so the next upgrade needs no second attempt at all.
    assert any("Export UV_SYSTEM_CERTS=1" in step for step in result["next_steps"])
    assert any("TROUBLESHOOTING.md section 1" in step for step in result["next_steps"])


def test_the_attempt_the_retry_replaced_is_kept_rather_than_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A success still carries the words that made the second attempt happen.

    `install` is the attempt whose outcome the result reports, which after a
    retry is the one that worked. The one it replaced is the only thing on the
    document that names the proxy in the manager's own voice, so it is kept under
    `install.certificate_retry.first_attempt` with the variable that was added.
    """
    stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, MANAGER_INSTALLED])

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    install = result["install"]
    assert install["returncode"] == 0
    retry = install["certificate_retry"]
    assert retry["variable"] == "UV_SYSTEM_CERTS"
    assert retry["value"] == "1"
    assert retry["first_attempt"]["returncode"] == 2
    assert "invalid peer certificate: UnknownIssuer" in retry["first_attempt"]["stderr"]


def test_both_attempts_failing_is_a_refusal_that_carries_both_of_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end where the machine's own store does not trust the proxy either.

    Two attempts, two failures, and the refusal has to be able to tell that from
    a single failure: it names what it tried, carries both attempts' stderr
    nested under `install` so the decisive `Caused by` line survives without
    `--json`, and points at installing the proxy's CA and at the standing export
    TROUBLESHOOTING recommends.
    """
    attempts = stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, UV_TRUST_FAILURE_AGAIN], version_after=__version__)

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert result["ok"] is False
    assert result["error_type"] == "upgrade_failed"
    assert result["installation_intact"] is True
    assert len(attempts) == 2
    # Both attempts' own words, and the second one is the outcome being reported.
    install = result["install"]
    assert "invalid peer certificate: UnknownIssuer" in install["stderr"]
    assert install["stderr"] == UV_TRUST_FAILURE_AGAIN.stderr
    assert install["certificate_retry"]["first_attempt"]["stderr"] == UV_TRUST_FAILURE.stderr
    # The cause, named, and the two things that actually fix it.
    assert "TLS-intercepting proxy" in result["summary"]
    assert "the proxy's own CA is missing from this machine's store as well" in result["summary"]
    assert any("Install the proxy's own CA" in step for step in result["next_steps"])
    assert any("Export UV_SYSTEM_CERTS=1" in step for step in result["next_steps"])
    # Never a way to a switch that turns verification off, on any path here.
    assert "--allow-insecure-host" not in json.dumps(result)
    assert "--trusted-host" not in json.dumps(result)


def test_a_failure_that_names_no_certificate_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the guarantee, and the reason the list is narrow.

    A manager that failed for any other reason is run once and reported once. A
    retry there would double the time every unrelated failure takes and would
    put a certificate diagnosis on a result that has nothing to do with one.
    """
    attempts = stub_manager(monkeypatch, answers=[subprocess.CompletedProcess([], 1, "", "error: no solution found when resolving dependencies")], version_after=__version__)

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert result["ok"] is False
    assert result["error_type"] == "upgrade_failed"
    assert len(attempts) == 1
    assert "certificates" not in result
    assert "certificate_retry" not in result["install"]
    assert "next_steps" not in result


# ---------------------------------------------------------------------------
# The operator's own environment, which is theirs.


def test_an_exported_uv_system_certs_is_not_overridden_and_earns_no_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variable was in force for the attempt that failed, so repeating it is not a retry.

    TROUBLESHOOTING recommends exporting exactly this, so the bench that took
    that advice is the one most likely to arrive here. Setting it again would
    run the identical command a second time and call it a retry, which is a
    minute of somebody's life and a false claim in the result.
    """
    monkeypatch.setenv("UV_SYSTEM_CERTS", "1")
    attempts = stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE], version_after=__version__)

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert result["ok"] is False
    assert len(attempts) == 1
    # The one attempt inherited the environment whole, so what the operator
    # exported was in force for it, and nothing here rewrote it.
    assert attempts[0].inherited
    assert os.environ["UV_SYSTEM_CERTS"] == "1"
    assert "UV_SYSTEM_CERTS is already set in this environment" in result["summary"]
    assert "a second one would only repeat it" in result["summary"]
    assert result["certificates"].startswith("Not retried, because UV_SYSTEM_CERTS is already set here")
    assert "certificate_retry" not in result["install"]
    # What is left to do is the store itself, not another variable.
    assert result["next_steps"] == ["Install the proxy's own CA certificate into this machine's certificate store. Until that is done there is nothing this command can be pointed at that trusts what the proxy presents, and no switch that turns verification off is a fallback. TROUBLESHOOTING.md section 1 is the rest of it."]


def test_an_exported_pip_cert_keeps_the_bundle_the_operator_chose(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A bundle an operator aimed pip at is their decision and is not replaced.

    Overriding it would silently swap the store they chose for one this module
    picked off a list, on the run where they are already debugging trust.
    """
    chosen = tmp_path / "corporate-roots.pem"
    chosen.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv("PIP_CERT", str(chosen))
    monkeypatch.setattr("agentic_hil.upgrade._system_cert_bundle", lambda: "/etc/ssl/certs/ca-certificates.crt")
    attempts = stub_manager(monkeypatch, answers=[PIP_TRUST_FAILURE], manager="pip", command=PIP_UPGRADE, version_after=__version__)

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert len(attempts) == 1
    assert attempts[0].inherited
    assert os.environ["PIP_CERT"] == str(chosen)
    assert "PIP_CERT is already set in this environment" in result["summary"]
    assert result["certificates"].startswith("Not retried, because PIP_CERT is already set here")
    # The bundle this module would have picked is never named, because the
    # operator's choice was not weighed against it.
    assert "/etc/ssl/certs/ca-certificates.crt" not in json.dumps(result)


def test_the_retry_never_writes_to_this_processs_own_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child gets the variable; the process the operator started does not.

    `os.environ` belongs to whoever ran this command, and an upgrade that
    exported into it would change every later child of this run, the MCP server
    included, on the strength of one manager's error message.
    """
    before = dict(os.environ)
    stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, MANAGER_INSTALLED])

    replace_installation(tool=CLI_UPGRADE_TOOL)

    assert "UV_SYSTEM_CERTS" not in os.environ
    assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# pip takes a file, so the file has to be found first.


def test_pip_is_retried_against_the_bundle_the_installers_search_order_finds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pip has no switch for this, so the retry is only as good as the bundle it finds.

    The search order is the installer's, and the first readable candidate wins:
    the same file `system_cert_bundle()` would hand pip on this host.
    """
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    missing = tmp_path / "not-here.crt"
    monkeypatch.setattr("agentic_hil.upgrade._SYSTEM_CERT_BUNDLES", (str(missing), str(bundle)))
    attempts = stub_manager(monkeypatch, answers=[PIP_TRUST_FAILURE, MANAGER_INSTALLED], manager="pip", command=PIP_UPGRADE)

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert result["ok"] is True
    assert [attempt.certificate_variables for attempt in attempts] == [{}, {"PIP_CERT": str(bundle)}]
    assert f"PIP_CERT={bundle}" in result["certificates"]
    assert any(f"Export PIP_CERT={bundle}" in step for step in result["next_steps"])


def test_no_bundle_anywhere_means_no_second_attempt_and_the_refusal_says_why(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows every time, and any host whose store is not a file.

    There is nothing to point pip at, so there is nothing to retry, and the one
    thing the refusal must not do is stay silent about that: an operator who
    reads "retried once" nowhere and no explanation cannot tell this from a
    feature that did not run.
    """
    monkeypatch.setattr("agentic_hil.upgrade._SYSTEM_CERT_BUNDLES", (str(tmp_path / "not-here.crt"),))
    attempts = stub_manager(monkeypatch, answers=[PIP_TRUST_FAILURE], manager="pip", command=PIP_UPGRADE, version_after=__version__)

    result = replace_installation(tool=CLI_UPGRADE_TOOL)

    assert result["ok"] is False
    assert len(attempts) == 1
    assert "No certificate bundle was readable at any of" in result["summary"]
    assert "no second attempt was made" in result["summary"]
    assert result["certificates"] == "Not retried, because no certificate bundle was found on this host for pip to be pointed at."
    assert any("Set PIP_CERT to this machine's certificate bundle" in step for step in result["next_steps"])


def test_the_bundle_search_takes_the_first_readable_candidate_and_ignores_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`system_cert_bundle()` returns a file, and so does this.

    A directory at one of those paths is not a bundle, and handing pip one as
    `PIP_CERT` would turn a recognised trust failure into an unrecognisable one.
    """
    from agentic_hil.upgrade import _system_cert_bundle

    directory = tmp_path / "certs"
    directory.mkdir()
    first = tmp_path / "first.crt"
    second = tmp_path / "second.crt"
    for candidate in (first, second):
        candidate.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr("agentic_hil.upgrade._SYSTEM_CERT_BUNDLES", (str(tmp_path / "absent.crt"), str(directory), str(first), str(second)))

    assert _system_cert_bundle() == str(first)

    monkeypatch.setattr("agentic_hil.upgrade._SYSTEM_CERT_BUNDLES", (str(tmp_path / "absent.crt"),))
    assert _system_cert_bundle() is None


# ---------------------------------------------------------------------------
# What the operator actually sees.


def test_the_rendered_refusal_names_the_proxy_without_the_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect in #326 was a summary that stopped before the diagnosis.

    The rendering a person gets is the one that has to carry it. The summary is
    the paragraph, `certificates` is a scalar that reaches Details on every
    renderer this project has had, and the two fixes are steps rather than prose,
    so the cause and what to do about it are on the screen of somebody who typed
    `agentic-hil upgrade` and nothing else.
    """
    stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, UV_TRUST_FAILURE_AGAIN], version_after=__version__)

    rendered = render_result(replace_installation(tool=CLI_UPGRADE_TOOL), "upgrade")

    assert "Refused: upgrade_failed" in rendered
    assert "TLS-intercepting proxy" in rendered
    assert "Install the proxy's own CA certificate" in rendered
    assert "Export UV_SYSTEM_CERTS=1" in rendered


def test_both_attempts_stderr_sits_under_install_where_the_renderer_walks_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape #314 fixed the renderer to walk, pinned as a shape.

    That renderer prints a captured `stdout` or `stderr` as a literal block
    wherever a result nests it, and walks nested objects to whatever depth they
    go. So the contract this module owes it is structural: both attempts' words
    reachable under `install`, under those names, as strings. A refactor that
    flattened them into one field or renamed them would pass every other test
    here and put the operator back on `--json`.
    """
    stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, UV_TRUST_FAILURE_AGAIN], version_after=__version__)

    install = replace_installation(tool=CLI_UPGRADE_TOOL)["install"]

    def captured_streams(value: object) -> list[str]:
        if not isinstance(value, dict):
            return []
        found = [text for key, text in value.items() if key in {"stdout", "stderr"} and isinstance(text, str)]
        for key, nested in value.items():
            if key not in {"stdout", "stderr"}:
                found.extend(captured_streams(nested))
        return found

    streams = captured_streams(install)
    assert sum("Caused by: invalid peer certificate: UnknownIssuer" in text for text in streams) == 2


def test_the_mcp_surface_rewrites_the_summary_and_keeps_the_certificate_sentence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`server_upgrade` writes its own summary, and has to carry this one over.

    That surface replaces the shared summary with the one about the restart,
    because the fact a host most needs is that this server is still the old code.
    A rewrite that dropped the certificate sentence would leave the MCP half of
    the product knowing less about the operator's own network than the command
    line does, on the one call that found out about it.
    """
    monkeypatch.setattr("agentic_hil.upgrade._host_locks_running_files", lambda: False)
    stub_manager(monkeypatch, answers=[UV_TRUST_FAILURE, MANAGER_INSTALLED])
    tools = AgenticHILToolService(upgradable_config(tmp_path))
    try:
        result = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert result["ok"] is True
    assert result["upgraded_on_disk"] is True
    assert result["running_version"] == __version__
    assert "UV_SYSTEM_CERTS=1" in result["summary"]
    assert result["certificates"] in result["summary"]
    # Both next steps survive, in the order that matters: the restart is what
    # makes this server the new code, and the export is what keeps the next
    # upgrade from needing a second attempt at all.
    assert any("restart the MCP server" in step for step in result["next_steps"])
    assert any("Export UV_SYSTEM_CERTS=1" in step for step in result["next_steps"])


# ---------------------------------------------------------------------------
# The mirror, checked against the copy it mirrors.


def _shell_case_patterns(script: str, function: str) -> list[str]:
    """The literal strings one `case` arm of a POSIX shell function tests for."""
    body = script.split(f"{function}() {{", 1)[1].split("\n}", 1)[0]
    arm = re.search(r"case \"\$1\" in\s*\n\s*(.+?)\)\n", body, re.DOTALL)
    assert arm is not None, f"{function} no longer matches with a case statement"
    return [pattern.strip().strip('"').strip("*").strip('"') for pattern in arm.group(1).split("|")]


def test_the_signature_list_and_the_bundle_order_still_match_the_installers() -> None:
    """The two mirrored lists, read out of install.sh and compared.

    They are mirrored rather than shared because they cannot be shared:
    `install.sh` runs on a machine where this package does not exist yet, so it
    can read nothing from here, and this module cannot call a POSIX shell
    function. What a deliberate mirror owes in exchange is a test that fails when
    the copies drift, instead of a proxied bench finding out that the installer
    recognises a signature the upgrade does not.
    """
    from agentic_hil.upgrade import _SYSTEM_CERT_BUNDLES, _TRUST_FAILURE_SIGNATURES

    script = (REPOSITORY_ROOT / "install.sh").read_text(encoding="utf-8")

    assert list(_TRUST_FAILURE_SIGNATURES) == _shell_case_patterns(script, "trust_failure")

    bundles = script.split("system_cert_bundle() {", 1)[1].split("\n}", 1)[0]
    assert list(_SYSTEM_CERT_BUNDLES) == re.findall(r"(/etc/\S+\.(?:crt|pem))", bundles)
