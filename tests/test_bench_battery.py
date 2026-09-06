"""What the bench battery promises about isolation, evidence and the answers it reads.

`tools/bench_battery.py` is the one entry point in this repository that an
operator points at their own machine and their own project. It says, in its help
text and in `docs/bench-battery.md`, that it reads and replaces no configuration
of theirs, that the report it writes is sendable, and that a check failing is a
statement about the bench rather than about the battery. Each of those was a
sentence before it was a behaviour, and this module is where it becomes one.

Nothing here runs the installed product. The battery drives it through
`subprocess.run`, so a canned stand-in for that call is the whole surface: what
the battery asked, what environment it asked in, and what it made of the answer.
The one exception is the isolation test that asks this checkout's own
configuration module which roots it resolves under the battery's environment,
because the question there is precisely whether the product agrees.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bench_battery  # noqa: E402
from bench_battery import FAIL, PASS, SKIP, Battery, BatteryError  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# What the isolation test asks the product's own configuration module. Both
# functions branch on the platform, which is the whole point: an answer that
# came back under the battery's roots on POSIX and under the operator's on
# Windows is what B1 was.
WHERE_THE_PRODUCT_LOOKS = (
    "import json;"
    "from agentic_hil.config import project_config_directory, user_state_root;"
    "print(json.dumps({'config': str(project_config_directory()), 'state': str(user_state_root())}))"
)


def completed(command: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeProduct:
    """The installed product, canned: one answer per subcommand, and a record of the asking.

    Keyed on the subcommand, because that is what a check chooses. An answer may
    be a `CompletedProcess`, a callable taking the command and the environment it
    was given, or an exception to raise, which is how a wedged probe and an
    unrunnable binary are put in front of the battery without either existing.
    """

    def __init__(self, answers: dict[str, object] | None = None, default: object = None) -> None:
        self.answers = dict(answers or {})
        self.default = default
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = dict(kwargs.get("env") or {})
        self.calls.append(list(command))
        self.environments.append(environment)
        answer = self.answers.get(command[1] if len(command) > 1 else "", self.default)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(command, environment)
        if answer is None:
            answer = completed(command, 0, "{}")
        return completed(command, answer.returncode, answer.stdout, answer.stderr)

    def asked(self, subcommand: str) -> list[list[str]]:
        return [call for call in self.calls if len(call) > 1 and call[1] == subcommand]


def wrote_a_configuration(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`init` answering the way it does in the battery's own redirected root."""
    config = Path(environment["XDG_CONFIG_HOME"]) / "agentic-hil" / "projects" / "demo" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 3\n", encoding="utf-8")
    return completed(command, 0, json.dumps({"ok": True, "config_path": str(config), "summary": "a configuration was written"}))


def a_probe_listing(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return completed(command, 0, json.dumps({"ok": True, "tool": "debugger_probes", "backend": "stlink", "probes": [{"probe_id": "PROBE"}], "summary": "1 probe. Read the sentence."}))


def a_port_listing(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if "--json" in command:
        return completed(command, 0, json.dumps({"ok": True, "ports": [], "summary": "no port"}))
    return completed(command, 0, "no port\n")


def a_plan_check(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`check-plan`, refusing the undeclared device and accepting everything else."""
    if "--strict" in command:
        if "--json" in command:
            return completed(command, 1, json.dumps({"ok": False, "configuration": {"debuggers": ["dut"], "com_ports": ["console"]}}))
        return completed(command, 1, f"Failed: a step names {bench_battery.UNDECLARED_DEVICE}\n")
    return completed(command, 0, json.dumps({"ok": True, "summary": "every plan loaded"}))


def a_healthy_run() -> dict[str, object]:
    """The answers a board-free battery run gets from a product that works."""
    return {
        "init": wrote_a_configuration,
        "--version": completed([], 0, "agentic-hil 99.0.0\n"),
        "com-ports": a_port_listing,
        "debugger-probes": a_probe_listing,
        "check-plan": a_plan_check,
    }


@pytest.fixture
def product(tmp_path: Path) -> Path:
    """A file that stands in for the installed product, so `main` accepts `--product`."""
    stub = tmp_path / "agentic-hil"
    stub.write_text("", encoding="utf-8")
    return stub


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "firmware"
    directory.mkdir()
    return directory


def battery_for(project: Path, tmp_path: Path, *, hardware: bool = False) -> Battery:
    battery = Battery(product="agentic-hil", project=project, root=tmp_path / "battery", hardware=hardware)
    for directory in (battery.config_root, battery.state_root, battery.uv_root):
        directory.mkdir(parents=True, exist_ok=True)
    return battery


def run_battery(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, out: Path, answers: dict[str, object], *extra: str) -> tuple[int, FakeProduct]:
    fake = FakeProduct(answers)
    monkeypatch.setattr(bench_battery.subprocess, "run", fake)
    status = bench_battery.main(["--project", str(project), "--product", str(product), "--out", str(out), *extra])
    return status, fake


def report_from(out: Path) -> dict:
    return json.loads(out.read_text(encoding="utf-8"))


def check_named(document: dict, name: str) -> dict:
    return next(check for check in document["checks"] if check["name"] == name)


# -- B1 and m4: the isolation the docstring promises ----------------------


def test_the_environment_moves_the_platform_configuration_and_state_roots(project: Path, tmp_path: Path) -> None:
    """The two variables the product reads on Windows, beside the two it reads on POSIX.

    The XDG pair alone is inert on a supported host, and a battery whose redirect
    is inert configures, revokes and grants in the operator's own store while its
    help text says it cannot.
    """
    battery = battery_for(project, tmp_path)

    environment = battery.environment()

    assert environment["XDG_CONFIG_HOME"] == str(battery.config_root)
    assert environment["XDG_STATE_HOME"] == str(battery.state_root)
    assert environment["APPDATA"] == str(battery.config_root)
    assert environment["LOCALAPPDATA"] == str(battery.state_root)


def test_the_product_resolves_both_roots_inside_the_battery_under_this_environment(project: Path, tmp_path: Path) -> None:
    """Asked of the product's own configuration module, on the platform running this.

    The two functions branch on `os.name` and read different variables on either
    side of that branch, so the only answer worth having is the one this host
    gives. On Windows this fails against a redirect that is XDG only.
    """
    battery = battery_for(project, tmp_path)
    environment = {**battery.environment(), "PYTHONPATH": str(REPOSITORY_ROOT / "src")}

    read = subprocess.run([sys.executable, "-c", WHERE_THE_PRODUCT_LOOKS], capture_output=True, text=True, env=environment, check=False)

    assert read.returncode == 0, read.stderr
    answered = json.loads(read.stdout)
    assert Path(answered["config"]).is_relative_to(battery.config_root), answered
    assert Path(answered["state"]).is_relative_to(battery.state_root), answered


def test_configure_refuses_a_configuration_outside_this_runs_own_root(monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path) -> None:
    """The one line that catches an inert redirect, wherever the inertness came from.

    A platform variable the battery does not set, or a file already sitting under
    the fallback root, both end the same way: `init` reports a path that is not
    under this run's configuration root. A battery that cannot prove its
    isolation runs no check.
    """
    elsewhere = tmp_path / "operator" / "config.yaml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("version: 3\n", encoding="utf-8")
    battery = battery_for(project, tmp_path)
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"init": completed([], 0, json.dumps({"config_path": str(elsewhere), "summary": "kept"}))}))

    with pytest.raises(BatteryError) as raised:
        battery.configure()

    assert str(elsewhere) in str(raised.value)


def test_configure_accepts_the_configuration_it_wrote_in_its_own_root(monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path) -> None:
    """The neighbouring case, pinned unchanged: a redirect that worked is not refused."""
    battery = battery_for(project, tmp_path)
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"init": wrote_a_configuration}))

    summary = battery.configure()

    assert summary == "a configuration was written"
    assert battery.config is not None
    assert battery.config.is_relative_to(battery.config_root)


def test_a_battery_that_cannot_prove_its_isolation_runs_no_check(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """Exit 2, no report, and nothing asked of the product after `init`."""
    elsewhere = tmp_path / "operator" / "config.yaml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("version: 3\n", encoding="utf-8")
    out = tmp_path / "bench-battery.json"

    status, fake = run_battery(monkeypatch, product, project, out, {"init": completed([], 0, json.dumps({"config_path": str(elsewhere)}))})

    assert status == 2
    assert not out.exists()
    assert [call[1] for call in fake.calls] == ["init"]


# -- M3: what the report carries -----------------------------------------


def test_the_report_records_names_rather_than_the_operators_paths(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """A report is meant to be sendable, and an absolute path carries the account name."""
    out = tmp_path / "reports" / "bench-battery.json"

    status, _ = run_battery(monkeypatch, product, project, out, a_healthy_run())

    document = report_from(out)
    assert status == 0, document
    assert document["product"] == "agentic-hil"
    assert document["project"] == project.name
    assert str(project) not in out.read_text(encoding="utf-8")
    assert str(tmp_path) not in out.read_text(encoding="utf-8")


def test_the_summary_prints_the_project_by_name(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The first console line was the project's absolute path, and it is read out loud."""
    out = tmp_path / "bench-battery.json"

    run_battery(monkeypatch, product, project, out, a_healthy_run())

    printed = capsys.readouterr().out
    assert f"on {project.name}," in printed
    assert str(project) not in printed


# -- M4: a wedged command, an unrunnable one, and where the report goes ---


def test_a_command_that_never_answers_fails_one_check_and_not_the_run(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """The timeout exists so a wedged probe ends the run; it must not end the report."""
    out = tmp_path / "bench-battery.json"
    answers = {**a_healthy_run(), "com-ports": subprocess.TimeoutExpired(cmd=["agentic-hil", "com-ports"], timeout=bench_battery.COMMAND_TIMEOUT_S)}

    status, _ = run_battery(monkeypatch, product, project, out, answers)

    assert status == 1
    document = report_from(out)
    wedged = check_named(document, "com-ports")
    assert wedged["outcome"] == FAIL, wedged
    assert "did not answer" in wedged["decisive_line"], wedged
    # And the checks after it still ran, which is the whole claim.
    assert check_named(document, "debugger-probes")["outcome"] == PASS, document


def test_a_command_that_cannot_be_run_fails_one_check_with_the_reason(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    out = tmp_path / "bench-battery.json"
    answers = {**a_healthy_run(), "com-ports": OSError(13, "Permission denied")}

    status, _ = run_battery(monkeypatch, product, project, out, answers)

    assert status == 1
    wedged = check_named(report_from(out), "com-ports")
    assert wedged["outcome"] == FAIL, wedged
    assert "could not be run" in wedged["decisive_line"], wedged


def test_the_report_is_written_where_its_directory_does_not_exist_yet(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """A mistyped `--out` threw away a completed hardware run."""
    out = tmp_path / "nowhere" / "yet" / "bench-battery.json"

    status, _ = run_battery(monkeypatch, product, project, out, a_healthy_run())

    assert status == 0
    assert out.is_file()


def test_an_internal_failure_exits_two_and_still_leaves_what_ran(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """Exit 1 means a check failed. A battery that crashed says something else."""
    out = tmp_path / "bench-battery.json"

    def raises(self: Battery) -> None:
        raise ValueError("the battery itself broke")

    monkeypatch.setattr(bench_battery.Battery, "check_probe_listing", raises)

    status, _ = run_battery(monkeypatch, product, project, out, a_healthy_run())

    assert status == 2
    document = report_from(out)
    assert check_named(document, "com-ports")["outcome"] == PASS, document


# -- M5: the two probe checks against every backend's answer shape --------


def test_the_probe_listing_passes_on_a_backend_that_claims_no_completeness(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyOCD and ST-Link set no `complete`, and a healthy bench of either went red."""
    battery = battery_for(project, tmp_path)
    listing = {"ok": True, "tool": "debugger_probes", "backend": "stlink", "probes": [{"probe_id": "ONE"}], "summary": "1 probe found. Not an authoritative count."}
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"debugger-probes": completed([], 0, json.dumps(listing))}))

    check = battery.check_probe_listing()

    assert check.outcome == PASS, check
    assert check.judged["probes"] == 1, check.judged
    assert check.judged["complete"] is None, check.judged


def test_the_probe_listing_fails_when_a_listed_probe_carries_no_identifier(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    battery = battery_for(project, tmp_path)
    listing = {"ok": True, "probes": [{"probe_id": "ONE"}, {"probe_id": ""}], "summary": "2 probes."}
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"debugger-probes": completed([], 0, json.dumps(listing))}))

    check = battery.check_probe_listing()

    assert check.outcome == FAIL, check
    assert check.judged["probes_carrying_a_serial"] == 1, check.judged


def test_the_probe_inventory_reads_the_multi_debugger_aggregate(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With more than one debugger declared and none bound the probes live under `debuggers`."""
    battery = battery_for(project, tmp_path, hardware=True)
    aggregate = {
        "ok": True,
        "tool": "debugger_probes",
        "complete": False,
        "debuggers": {"one": {"probes": [{"probe_id": "A"}]}, "two": {"probes": [{"probe_id": "B"}]}},
        "summary": "2 debugger(s) answered.",
    }
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"debugger-probes": completed([], 0, json.dumps(aggregate))}))

    check = battery.check_probe_inventory()

    assert check.outcome == PASS, check
    assert check.judged == {"probes": 2, "probes_carrying_a_serial": 2}, check.judged


# -- M6: the grant line the product actually offers -----------------------


def test_the_permission_check_reads_the_products_own_grant_line(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The product emits a fixed constant; the resolved file name can never match it."""
    battery = Battery(product=str(tmp_path / "bin" / "agentic-hil.exe"), project=project, root=tmp_path / "battery", hardware=True)
    battery._configuration = {"debuggers": ["dut"]}
    key = "debuggers.dut.permissions.allow_reset"
    refusal = {
        "ok": False,
        "error_type": "permission_denied",
        "permission": key,
        "summary": f"Refused: permission_denied. {key} is not granted.",
        "validation_error": {"permission": key, "next_step": f"Run `agentic-hil grant {key}` and try again."},
    }

    def answered(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if "--json" in command:
            return completed(command, 1, json.dumps(refusal))
        return completed(command, 1, f"Refused: permission_denied. {key} is not granted.\n")

    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"test-reactor": answered, "revoke": completed([], 0, "revoked\n"), "grant": completed([], 0, "granted\n")}))

    check = battery.check_permission_refusal()

    assert check.outcome == PASS, check
    assert check.judged["grant_line_offered"] is True, check.judged
    assert check.command[0] == "agentic-hil.exe", check.command


# -- m13: the line the verdict was read off, wherever it was printed ------


def test_a_failure_printed_on_stderr_is_the_decisive_line(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`the command printed nothing` while the reason sat in the captured stderr."""
    battery = battery_for(project, tmp_path)
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"check-plan": completed([], 2, "", "usage: agentic-hil [-h] ...\nagentic-hil: error: argument command: invalid choice: 'check-plan'\n")}))

    check = battery.check_plan_strict()

    assert check.outcome == FAIL, check
    assert check.decisive_line.startswith("usage:"), check.decisive_line
    assert "invalid choice" in check.judged["stderr_tail"], check.judged


# -- m14: which releases this battery can measure -------------------------


def test_an_installation_below_the_minimum_release_is_refused_before_any_check(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A red verdict on a healthy bench, over commands the installed release never had."""
    out = tmp_path / "bench-battery.json"
    answers = {**a_healthy_run(), "--version": completed([], 0, "agentic-hil 0.20.0\n")}

    status, fake = run_battery(monkeypatch, product, project, out, answers)

    assert status == 2
    assert not out.exists()
    printed = capsys.readouterr().err
    assert "0.20.0" in printed
    assert bench_battery.MINIMUM_RELEASE in printed
    assert fake.asked("com-ports") == []


def test_the_minimum_release_itself_is_measured(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """The gate is `older than`, not `other than`."""
    out = tmp_path / "bench-battery.json"
    answers = {**a_healthy_run(), "--version": completed([], 0, f"agentic-hil {bench_battery.MINIMUM_RELEASE}\n")}

    status, _ = run_battery(monkeypatch, product, project, out, answers)

    assert status == 0, out.read_text(encoding="utf-8")


# -- m15: a comparison that never happened is not a comparison that passed


def test_the_reset_check_fails_when_the_debuggers_capture_cannot_be_read(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty against empty reported `carried_verbatim` true having compared nothing."""
    battery = battery_for(project, tmp_path, hardware=True)
    battery._configuration = {"debuggers": ["dut"]}
    report = {"ok": True, "steps": [{"result": {"ok": True, "success_confirmed": True, "log_path": "no/such/log.json", "summary": "Target reset with mode 'run'."}}]}
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"test-reactor": completed([], 0, json.dumps(report))}))

    check = battery.check_reset()

    assert check.outcome == FAIL, check
    assert check.judged["capture_read"] is False, check.judged
    assert check.judged["carried_verbatim"] is False, check.judged


def test_the_reset_check_passes_on_a_capture_that_was_read_and_carried_nothing(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbouring case, unchanged: a green reset whose capture is readable."""
    battery = battery_for(project, tmp_path, hardware=True)
    battery._configuration = {"debuggers": ["dut"]}
    log = project / "logs" / "reset.json"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"stdout": "Target reset\n", "stderr": ""}), encoding="utf-8")
    report = {"ok": True, "steps": [{"result": {"ok": True, "success_confirmed": True, "log_path": "logs/reset.json", "summary": "Target reset with mode 'run'."}}]}
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct({"test-reactor": completed([], 0, json.dumps(report))}))

    check = battery.check_reset()

    assert check.outcome == PASS, check
    assert check.judged["capture_read"] is True, check.judged


# -- m16: every plan the project ships ------------------------------------


def test_the_plan_check_reaches_subdirectories_and_both_extensions(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The doc promises every plan the project ships, and the glob saw the root only."""
    (project / "plans").mkdir()
    (project / "plans" / "smoke.yml").write_text("version: 3\nsteps:\n  - device: dut\n", encoding="utf-8")
    (project / "testconfig.yaml").write_text("version: 3\nsteps:\n  - device: dut\n", encoding="utf-8")
    (project / "config.yaml").write_text("version: 3\ntarget:\n  name: x\n", encoding="utf-8")
    (project / "bench-battery-undeclared.yaml").write_text("version: 3\nsteps:\n  - device: nope\n", encoding="utf-8")
    battery = battery_for(project, tmp_path)
    fake = FakeProduct({"check-plan": completed([], 0, json.dumps({"ok": True, "summary": "every plan loaded"}))})
    monkeypatch.setattr(bench_battery.subprocess, "run", fake)

    check = battery.check_project_plans()

    assert check.outcome == PASS, check
    assert check.judged["plans"] == ["plans/smoke.yml", "testconfig.yaml"], check.judged


def test_the_plan_check_leaves_the_products_own_directories_alone(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan the product wrote into its own working tree is not a plan the project ships."""
    (project / ".agentic-hil" / "reports").mkdir(parents=True)
    (project / ".agentic-hil" / "reports" / "old.yaml").write_text("version: 3\nsteps:\n  - device: dut\n", encoding="utf-8")
    (project / "build").mkdir()
    (project / "build" / "generated.yaml").write_text("version: 3\nsteps:\n  - device: dut\n", encoding="utf-8")
    battery = battery_for(project, tmp_path)
    monkeypatch.setattr(bench_battery.subprocess, "run", FakeProduct())

    check = battery.check_project_plans()

    assert check.outcome == SKIP, check
    assert "no plan" in check.decisive_line


# -- m7: what the battery leaves in the operator's project ----------------


def test_the_battery_removes_the_plans_and_the_artifacts_it_wrote(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """An undocumented top level directory in a repository somebody may commit."""
    out = tmp_path / "bench-battery.json"

    status, _ = run_battery(monkeypatch, product, project, out, a_healthy_run())

    assert status == 0
    assert sorted(path.name for path in project.iterdir()) == []


def test_keep_leaves_the_plans_and_the_artifacts_where_they_are(monkeypatch: pytest.MonkeyPatch, product: Path, project: Path, tmp_path: Path) -> None:
    """The flag exists because a failed check is read out of the file that failed it."""
    out = tmp_path / "bench-battery.json"

    status, _ = run_battery(monkeypatch, product, project, out, a_healthy_run(), "--keep")

    assert status == 0
    assert sorted(path.name for path in project.glob("bench-battery-*.yaml")) == ["bench-battery-undeclared.yaml"]


def test_the_battery_writes_its_artifacts_under_one_named_directory(project: Path, tmp_path: Path) -> None:
    """One name, so the doc can name it and the cleanup can remove it."""
    battery = battery_for(project, tmp_path)

    assert bench_battery.BATTERY_ARTIFACTS.startswith("bench-battery")
    assert battery.artifacts == project / bench_battery.BATTERY_ARTIFACTS


# -- m17 and m18: the operator-facing words -------------------------------


def test_the_help_text_carries_no_sentence_that_breaks_off(capsys: pytest.CaptureFixture[str]) -> None:
    """The paragraph is the one safety property of the script, printed by `--help`."""
    with pytest.raises(SystemExit):
        bench_battery.main(["--help"])

    printed = " ".join(capsys.readouterr().out.split())
    assert "or it this" not in printed
    assert "device locks" in printed


def test_the_doc_names_the_artifacts_and_the_third_exit_code() -> None:
    """The inventory is presented as complete, and the exit contract as the whole of it."""
    doc = (REPOSITORY_ROOT / "docs" / "bench-battery.md").read_text(encoding="utf-8")

    assert bench_battery.BATTERY_ARTIFACTS in doc
    assert "`2`" in doc
    assert "--keep" in doc
