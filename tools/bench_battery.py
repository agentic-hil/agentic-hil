"""Run this project's bench checks against an installed Agentic HIL and a project.

The suite proves the code. This proves an installation: the release an operator
actually has, on the machine they actually have, against the board in front of
it. It is the same set of questions the two published test tiers ask, asked
through the product's own commands rather than through pytest, so a bench that
has no checkout of this repository can still be measured and can send the result
to somebody who has one.

    python tools/bench_battery.py --project ~/work/my-firmware
    python tools/bench_battery.py --project ~/work/my-firmware --hardware

Without ``--hardware`` it runs what needs no probe: the version, the host's port
inventory, the probe listing, and the board-free plan check. With it, it also
runs what needs the probe and the board: doctor, a real reset, the project's own
plan, a plan whose claim cannot hold, a permission the plan needs revoked and
granted back, and the evidence built from the run.

Every check goes through an ``agentic-hil`` command. Nothing here opens a
debugger, a serial device or a CAN interface of its own: a hardware action
outside the tool is one nothing validated against the configuration, nothing
locked against a second run, and nothing recorded in the audit chain, and a
battery that reached the board that way would be measuring something other than
the product.

What it touches, and what it does not:

* the configuration root and the state root are redirected into a temporary
  directory, so the configuration this run uses is written by ``agentic-hil
  init`` here and no configuration the operator owns is read or replaced. Both
  platforms' variables are set, because the product reads ``XDG_CONFIG_HOME``
  and ``XDG_STATE_HOME`` on POSIX and ``APPDATA`` and ``LOCALAPPDATA`` on
  Windows, and a redirect that moved only one pair would be inert on the other
  host. The redirect is then checked rather than assumed: if the configuration
  ``init`` reports is not under this run's own root, the battery says which file
  it found and runs no check at all;
* uv's tool directory, its bin directory and its cache are redirected there too.
  Nothing here upgrades anything, and that is the point: an installed product is
  what is being measured, so no command may be able to replace it;
* HOME is deliberately left alone. The machine-wide device locks live under it,
  and they are what keeps this run off a board another run is holding. A battery
  that isolated HOME would be a battery that could meet another run on the board
  without either of them knowing it;
* the project directory is the operator's own and is used as it stands. The
  product writes its reports and logs under ``.agentic-hil`` there, exactly as
  it does for any run. The battery writes its own plans as ``bench-battery-*``
  beside them and its own artifacts under ``bench-battery-artifacts/``, and
  removes both before it returns unless ``--keep`` says otherwise.

The report is ``bench-battery.json``: one entry per check with the command it
ran, the status it exited on, the fields that were judged, whether it passed,
and the line the verdict was read off. It carries no probe serial and no port
identity, and no absolute path: what is recorded about hardware is that an
identity was there, never what it said, and what is recorded about this machine
is the product's file name and the project directory's name.

Exit codes: 0 when every check passed, 1 when any check failed, and 2 when the
battery could not be set up or stopped on a failure of its own, which is the one
code that says the bench was not measured.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Long enough for a flash and a boot, short enough that a wedged probe ends the
# run rather than holding it until somebody notices.
COMMAND_TIMEOUT_S = 600.0

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

# The status a command gets when it never produced one of its own: it hung until
# the timeout, or the binary could not be started. Distinct from anything the
# product exits on, so `judge` can recognise it and fail exactly the one check
# that asked, with the reason on the line the verdict is read off.
COMMAND_DID_NOT_RUN = 126

# The release `check-plan` and `run-evidence` first shipped in. An older
# installation rejects them at argument parsing, and a battery that scored that
# as two hard failures was calling a healthy bench broken over a command the
# product never had.
MINIMUM_RELEASE = "0.21.2"

# What the product offers a refusal with. A fixed constant on its side, so the
# check compares against the constant and not against whatever `shutil.which`
# resolved, whose file name carries an extension on Windows and the wrapper's
# name for anyone who passed `--product`.
GRANT_COMMAND = "agentic-hil grant"

# Everything the battery writes into the operator's project that is not a plan,
# under one name, so the doc can name it and the cleanup can remove it without
# ever reaching a directory of theirs.
BATTERY_ARTIFACTS = "bench-battery-artifacts"

# A plan the project's configuration cannot possibly satisfy, written into a
# temporary file so the strict check has something to refuse that is not one of
# the operator's own plans.
UNDECLARED_DEVICE = "a_device_this_configuration_does_not_declare"

# Where a plan is not looked for. `.agentic-hil` and the dot directories are the
# product's and the tooling's own working trees, and a build directory holds
# generated copies: none of them is a plan the project ships.
NOT_A_PROJECT_PLAN = ("build", "dist", "node_modules", "__pycache__", BATTERY_ARTIFACTS)


@dataclass
class Check:
    """One question, the command that asked it, and what the answer was read as."""

    name: str
    question: str
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    judged: dict = field(default_factory=dict)
    outcome: str = SKIP
    decisive_line: str = ""


class BatteryError(RuntimeError):
    """The battery could not be set up, which is not a check failing."""


def failure_worded_lines(capture: str) -> list[str]:
    """The lines of a debugger capture carrying the words a failure is read out of.

    Spelled out here rather than imported, because this script runs against an
    installed product and must not depend on being able to import it. It is also
    the point of the check it serves: the claim is that the backend's own success
    marker outranks these words, and asking the product which lines they are
    would be asking it to grade itself.
    """
    return [line for line in capture.splitlines() if "error" in line.lower() or "failed" in line.lower()]


def listed_probes(result: dict) -> list[dict]:
    """The probes an answer carries, through either shape the listing comes in.

    One bound debugger and the CLI returns the backend's own document, whose
    probes are under ``probes``. More than one declared and none bound and it
    returns an aggregate, whose probes live under each entry of a ``debuggers``
    map with no top-level list at all. A reader that knew one shape reported zero
    probes on a bench where every probe answered.
    """
    probes = result.get("probes")
    if isinstance(probes, list):
        return [probe for probe in probes if isinstance(probe, dict)]
    collected: list[dict] = []
    for entry in (result.get("debuggers") or {}).values():
        if isinstance(entry, dict):
            collected.extend(probe for probe in (entry.get("probes") or []) if isinstance(probe, dict))
    return collected


def probes_carrying_an_identifier(probes: list[dict]) -> list[dict]:
    return [probe for probe in probes if isinstance(probe.get("probe_id"), str) and probe["probe_id"].strip()]


def release_of(version: str) -> tuple[int, int, int] | None:
    """The release a version line names, or None where it names none."""
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if found is None:
        return None
    first, second, third = found.groups()
    return int(first), int(second), int(third)


class Battery:
    """The isolated environment the checks run in, and the checks themselves."""

    def __init__(self, product: str, project: Path, root: Path, hardware: bool) -> None:
        self.product = product
        self.project = project
        self.root = root
        self.hardware = hardware
        self.config_root = root / "config"
        self.state_root = root / "state"
        self.uv_root = root / "uv"
        self.artifacts = project / BATTERY_ARTIFACTS
        self.config: Path | None = None
        self.checks: list[Check] = []
        self._configuration: dict = {}
        self._executed_steps = 0

    # -- the environment -------------------------------------------------

    def environment(self, *, with_config: bool = True) -> dict[str, str]:
        environment = {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.config_root),
            "XDG_STATE_HOME": str(self.state_root),
            # The same two roots under the names the product reads on Windows.
            # `config.project_config_directory` branches on `os.name` to APPDATA
            # and `config.user_state_root` to LOCALAPPDATA, so a redirect that
            # was XDG only ran every command against the operator's own store on
            # a supported host while this file said it could not.
            "APPDATA": str(self.config_root),
            "LOCALAPPDATA": str(self.state_root),
            "UV_TOOL_DIR": str(self.uv_root / "tools"),
            "UV_TOOL_BIN_DIR": str(self.uv_root / "bin"),
            "UV_CACHE_DIR": str(self.uv_root / "cache"),
        }
        environment.pop("AGENTIC_HIL_CONFIG", None)
        if with_config and self.config is not None:
            environment["AGENTIC_HIL_CONFIG"] = str(self.config)
        return environment

    def run(self, *arguments: str, with_config: bool = True) -> subprocess.CompletedProcess[str]:
        """One command, and a status for it even when it never produced one.

        The timeout is here so a wedged probe ends the run rather than holding
        it, and a timeout that escaped as an exception ended the whole battery
        with a traceback and no report: every check that had already passed was
        lost, and the run that most needs a report is the run that loses it. One
        command that did not answer fails one check.
        """
        command = [self.product, *arguments]
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=str(self.project),
                env=self.environment(with_config=with_config),
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self.did_not_run(command, f"`{' '.join(arguments)}` did not answer within {COMMAND_TIMEOUT_S:.0f}s and was ended")
        except OSError as error:
            return self.did_not_run(command, f"`{' '.join(arguments)}` could not be run: {error}")

    def did_not_run(self, command: list[str], why: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, COMMAND_DID_NOT_RUN, "", why + "\n")

    def document(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        answered = self.run(*arguments, "--json")
        try:
            return answered, json.loads(answered.stdout)
        except json.JSONDecodeError:
            return answered, {}

    # -- setting up ------------------------------------------------------

    def configure(self) -> str:
        """Write this run's own configuration for the project, and say what it found.

        ``agentic-hil init`` is what reads the attached bench and binds the probe
        and the port it publishes. It writes into the redirected configuration
        root, so a project that already has a configuration of the operator's
        keeps it untouched and unread.

        And then that is checked rather than believed. The configuration root has
        more than one candidate and a platform branch above them, so a redirect
        can be inert or can be outranked by a file that already exists elsewhere,
        and the run that follows would revoke and grant a permission in the
        operator's own policy file. A battery that cannot prove its isolation
        must not run a single check.
        """
        for directory in (self.config_root, self.state_root, self.uv_root):
            directory.mkdir(parents=True, exist_ok=True)
        written = self.run("init", "--json", with_config=False)
        if written.returncode != 0 or not written.stdout.strip():
            raise BatteryError(f"`{self.product} init` could not configure this project:\n{written.stdout}\n{written.stderr}")
        result = json.loads(written.stdout)
        reported = Path(result["config_path"])
        if not self.inside_this_run(reported):
            raise BatteryError(
                f"`{self.product} init` selected a configuration outside this run's own root: {reported}. "
                f"This battery redirects its configuration root to {self.config_root} and checks that the product agreed, because the alternative "
                "is revoking and granting a permission in a configuration somebody owns. No check ran."
            )
        self.config = reported
        return str(result.get("summary", ""))

    def inside_this_run(self, config: Path) -> bool:
        try:
            return config.resolve().is_relative_to(self.config_root.resolve())
        except (OSError, ValueError):  # pragma: no cover - a path this host cannot resolve is not this root
            return False

    def declared(self, section: str) -> list[str]:
        """What this configuration declares, read out of the product rather than the file.

        `check-plan` names the devices it compared a plan against, which is the
        same list and comes from the code that reads the file. Parsing the YAML
        here would mean this script needed a YAML parser of its own on whatever
        interpreter an operator happens to run it with, and would be a second
        reader of the operator's configuration format to keep in step.
        """
        return list(self._configuration.get(section) or [])

    def write_plan(self, name: str, body: str) -> str:
        (self.project / name).write_text(body, encoding="utf-8")
        return name

    # -- recording -------------------------------------------------------

    def record(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def without_local_paths(self, text: str) -> str:
        """The same text with this machine's own directories reduced to a label.

        A report is meant to be sendable, and every one of these carries the
        account name on an ordinary installation. What the reader needs from a
        decisive line is which file or which step, and both survive this.
        """
        for path, label in ((self.root, "<battery>"), (self.project, "<project>"), (Path.home(), "<home>")):
            text = re.sub(re.escape(str(path)), label, text, flags=re.IGNORECASE)
        return text

    def judge(self, check: Check, answered: subprocess.CompletedProcess[str], judged: dict, verdict: bool, decisive: str) -> Check:
        check.command = [Path(self.product).name, *answered.args[1:]] if answered.args else check.command
        check.exit_code = answered.returncode
        check.judged = judged
        if answered.returncode == COMMAND_DID_NOT_RUN:
            # The command produced no answer at all, so whatever was read out of
            # an empty document decides nothing. The sentence that says what
            # happened is the only decisive line there is.
            verdict = False
            decisive = answered.stderr.strip() or decisive
        check.outcome = PASS if verdict else FAIL
        if check.outcome == FAIL and answered.stderr.strip():
            # A failure whose message went to stderr was reported with an empty
            # decisive line while the reason sat unread in a capture this script
            # already held.
            judged.setdefault("stderr_tail", self.without_local_paths(answered.stderr.strip())[-400:])
        check.decisive_line = self.without_local_paths(decisive)
        return check

    def skip(self, check: Check, why: str) -> Check:
        check.outcome = SKIP
        check.decisive_line = self.without_local_paths(why)
        return check

    @staticmethod
    def first_printed_line(answered: subprocess.CompletedProcess[str]) -> str:
        """The first line the command printed, on either stream.

        stdout first, because that is where the product prints its rendered
        result. stderr after it, because argparse's usage error and a crash go
        there, and reporting those as "the command printed nothing" is a report
        the failure cannot be acted on from.
        """
        for stream in (answered.stdout, answered.stderr):
            if stream and stream.strip():
                return stream.strip().splitlines()[0]
        return ""

    # -- the checks ------------------------------------------------------

    def check_version(self) -> Check:
        check = self.record(Check("version", "the installed product answers with a version"))
        answered = self.run("--version", with_config=False)
        reported = answered.stdout.strip() or answered.stderr.strip()
        ok = answered.returncode == 0 and bool(reported)
        return self.judge(check, answered, {"version": reported}, ok, reported or "the product printed no version")

    def check_com_ports(self) -> Check:
        check = self.record(Check("com-ports", "the host's ports are listed, the ones with a USB identity in full"))
        answered, result = self.document("com-ports")
        ports = result.get("ports") or []
        identified = [port for port in ports if port.get("serial_number") or port.get("vid") or port.get("product")]
        anonymous = len(ports) - len(identified)
        printed = self.run("com-ports").stdout
        collapsed = anonymous == 0 or f"{anonymous} legacy serial ports without a USB identity" in printed
        in_full = all(port["device"] in printed and (not port.get("stable_device") or port["stable_device"] in printed) for port in identified)
        ok = answered.returncode == 0 and result.get("ok") is True and collapsed and in_full
        judged = {"ports": len(ports), "with_a_usb_identity": len(identified), "collapsed": anonymous, "printed_in_full": in_full}
        decisive = result.get("summary") or "" if ok else f"{len(identified)} of {len(ports)} port(s) carry a USB identity; the rest were {'collapsed' if collapsed else 'not collapsed'}"
        return self.judge(check, answered, judged, ok, decisive)

    def check_probe_listing(self) -> Check:
        check = self.record(Check("debugger-probes", "probe discovery answers and every probe it lists carries an identifier"))
        answered, result = self.document("debugger-probes")
        # Zero probes is not a failure here. The listing is authoritative about
        # nothing, says so, and exits 0; a script that treated the disclaimer as
        # a fault is what this pins. `complete` is recorded rather than required,
        # because the pyOCD and ST-Link listings never set it and a check that
        # demanded it went red on a bench where discovery worked perfectly.
        probes = listed_probes(result)
        identified = probes_carrying_an_identifier(probes)
        ok = answered.returncode == 0 and result.get("ok") is True and len(identified) == len(probes)
        judged = {"probes": len(probes), "probes_carrying_a_serial": len(identified), "complete": result.get("complete")}
        return self.judge(check, answered, judged, ok, str(result.get("summary", "")).split(". ")[0])

    def check_plan_strict(self) -> Check:
        check = self.record(Check("check-plan-strict", "a plan naming an undeclared device fails the board-free gate"))
        plan = self.write_plan(
            "bench-battery-undeclared.yaml",
            f"version: 3\nname: bench-battery-undeclared\nsteps:\n  - device: {UNDECLARED_DEVICE}\n    action: uart_open\n",
        )
        answered = self.run("check-plan", plan, "--strict")
        # The same command names the devices this configuration declares, which
        # is what the hardware checks below build their plans out of.
        _, document = self.document("check-plan", plan, "--strict")
        self._configuration = document.get("configuration") or {}
        heading = self.first_printed_line(answered)
        ok = answered.returncode == 1 and heading.startswith("Failed:") and UNDECLARED_DEVICE in answered.stdout
        return self.judge(check, answered, {"heading": heading}, ok, heading or "the command printed nothing")

    def project_plans(self) -> list[str]:
        """Every plan this project ships, wherever it keeps them and whichever extension it uses.

        A plan is a document declaring steps. A project can hold other YAML, an
        example configuration among it, and handing one of those to `check-plan`
        would report the project as broken over a file that was never a plan.
        What is left out beyond that is the tooling's own working trees, which
        hold generated copies rather than anything the project ships.
        """
        found: list[str] = []
        for extension in ("*.yaml", "*.yml"):
            for path in self.project.rglob(extension):
                relative = path.relative_to(self.project)
                if any(part.startswith(".") or part in NOT_A_PROJECT_PLAN for part in relative.parts[:-1]):
                    continue
                if relative.name.startswith("bench-battery-") or relative.name in {"config.yaml", "config.yml"}:
                    continue
                if any(line.startswith("steps:") for line in path.read_text(encoding="utf-8", errors="replace").splitlines()):
                    found.append(relative.as_posix())
        return sorted(found)

    def check_project_plans(self) -> Check:
        check = self.record(Check("check-plan", "every plan this project ships loads through the reactor's loader"))
        plans = self.project_plans()
        if not plans:
            return self.skip(check, "this project holds no plan to check")
        answered, result = self.document("check-plan", *plans)
        ok = answered.returncode == 0 and result.get("ok") is True
        return self.judge(check, answered, {"plans": plans}, ok, str(result.get("summary", "")).splitlines()[0] if result else answered.stdout[:200])

    def check_doctor(self) -> Check:
        check = self.record(Check("doctor", "the configuration loads and every device it declares names hardware"))
        answered = self.run("doctor")
        ok = answered.returncode == 0
        return self.judge(check, answered, {"bound": ok}, ok, self.first_printed_line(answered))

    def check_probe_inventory(self) -> Check:
        check = self.record(Check("probe-inventory", "the attached probe is enumerated and carries a serial"))
        answered, result = self.document("debugger-probes")
        probes = listed_probes(result)
        with_a_serial = probes_carrying_an_identifier(probes)
        ok = answered.returncode == 0 and len(with_a_serial) == len(probes) and bool(probes)
        # The count and nothing else. A serial belongs on the bench, not in a
        # file somebody may send on.
        judged = {"probes": len(probes), "probes_carrying_a_serial": len(with_a_serial)}
        return self.judge(check, answered, judged, ok, f"{len(with_a_serial)} probe(s) with a serial were enumerated")

    def check_reset(self) -> Check:
        check = self.record(Check("reset", "a real reset over the probe succeeds and carries what the backend said"))
        debuggers = self.declared("debuggers")
        if not debuggers:
            return self.skip(check, "this configuration declares no debugger")
        plan = self.write_plan(
            "bench-battery-reset.yaml",
            f"version: 3\nname: bench-battery-reset\nsteps:\n  - device: {debuggers[0]}\n    action: reset\n    mode: run\n",
        )
        answered, report = self.document("test-reactor", "--test-config", plan)
        step = (report.get("steps") or [{}])[0].get("result") or {}
        warnings = step.get("backend_warnings") or []
        printed = self.debugger_capture(step.get("log_path"))
        # A capture that could not be read is not a capture that carried
        # nothing. Empty against empty reported the comparison as passed having
        # compared nothing, and the one case that matters is a log with
        # failure-worded lines in it that nobody could open.
        capture_read = printed is not None
        carried = capture_read and failure_worded_lines(printed or "") == list(warnings)
        ok = answered.returncode == 0 and step.get("ok") is True and step.get("success_confirmed") is True and "error_type" not in step and carried
        judged = {"success_confirmed": step.get("success_confirmed"), "backend_warnings": len(warnings), "capture_read": capture_read, "carried_verbatim": carried}
        return self.judge(check, answered, judged, ok, str(step.get("summary", "")) or str(report.get("summary", "")))

    def check_project_plan(self) -> Check:
        check = self.record(Check("plan", "this project's own plan is green on the board"))
        plan = "testconfig.yaml"
        if not (self.project / plan).is_file():
            return self.skip(check, f"this project ships no {plan}")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        answered, report = self.document("test-reactor", "--test-config", plan)
        (self.artifacts / "plan-report.json").write_text(json.dumps(report), encoding="utf-8")
        steps = report.get("steps") or []
        # What the evidence check below has to time. A plan refused before its
        # first step executes none, and a table with no rows in it is not a table
        # whose rows are untimed.
        self._executed_steps = sum(1 for step in steps if step.get("result"))
        ok = answered.returncode == 0 and report.get("ok") is True and bool(steps) and all((step.get("result") or {}).get("ok") is True for step in steps)
        judged = {"steps": [step.get("action") for step in steps], "cleanup_ok": report.get("cleanup_ok"), "audit_ok": report.get("audit_ok")}
        return self.judge(check, answered, judged, ok, str(report.get("summary", "")).splitlines()[0] if report else answered.stderr[:200])

    def check_failing_claim(self) -> Check:
        check = self.record(Check("failing-claim", "a plan whose claim cannot hold is headed by the run's own outcome"))
        ports = self.declared("com_ports")
        if not ports:
            return self.skip(check, "this configuration declares no serial port to read a claim from")
        plan = self.write_plan(
            "bench-battery-claim.yaml",
            "version: 3\nname: bench-battery-claim\nsteps:\n"
            f"  - device: {ports[0]}\n    action: uart_open\n    clear_buffer: true\n"
            f"  - device: {ports[0]}\n    action: uart_read\n    comparator:\n      equals: \"this board never prints this\"\n    timeout_s: 3\n",
        )
        answered = self.run("test-reactor", "--test-config", plan)
        heading = self.first_printed_line(answered)
        ok = answered.returncode == 1 and heading.startswith("Failed: comparator_unmet")
        return self.judge(check, answered, {"heading": heading}, ok, heading or "the command printed nothing")

    def check_permission_refusal(self) -> Check:
        check = self.record(Check("permission-refusal", "a permission a plan needs is named at every level, with the grant line"))
        debuggers = self.declared("debuggers")
        if not debuggers:
            return self.skip(check, "this configuration declares no debugger")
        key = f"debuggers.{debuggers[0]}.permissions.allow_reset"
        plan = self.write_plan(
            "bench-battery-permission.yaml",
            f"version: 3\nname: bench-battery-permission\nsteps:\n  - device: {debuggers[0]}\n    action: reset\n    mode: run\n",
        )
        revoked = self.run("revoke", key)
        if revoked.returncode == COMMAND_DID_NOT_RUN:
            return self.judge(check, revoked, {"permission": key}, False, "")
        if revoked.returncode != 0:
            return self.skip(check, f"this configuration does not allow a permission to be revoked from here: {revoked.stdout[:200]}")
        try:
            answered, result = self.document("test-reactor", "--test-config", plan)
            rendered = self.run("test-reactor", "--test-config", plan)
        finally:
            granted = self.run("grant", key)
        finding = result.get("validation_error") or {}
        # The product's own constant, not the resolved file name. `shutil.which`
        # answers with an extension on Windows and with whatever a wrapper was
        # called anywhere, and neither can ever appear in the sentence the
        # product builds out of a fixed string.
        ok = (
            answered.returncode == 1
            and result.get("error_type") == "permission_denied"
            and result.get("permission") == key
            and key in str(result.get("summary", ""))
            and finding.get("permission") == key
            and f"{GRANT_COMMAND} {key}" in str(finding.get("next_step", ""))
            and rendered.stdout.startswith("Refused: permission_denied")
            and granted.returncode == 0
        )
        judged = {
            "permission": key,
            "named_in_the_summary": key in str(result.get("summary", "")),
            "named_on_the_finding": finding.get("permission") == key,
            "grant_line_offered": f"{GRANT_COMMAND} {key}" in str(finding.get("next_step", "")),
            "granted_back": granted.returncode == 0,
        }
        return self.judge(check, answered, judged, ok, str(result.get("summary", ""))[:400] or "the plan printed no summary")

    def check_run_evidence(self) -> Check:
        check = self.record(Check("run-evidence", "the job summary prints one digest prefix and an elapsed time per step"))
        report = self.artifacts / "plan-report.json"
        if not report.is_file():
            return self.skip(check, "no plan report was produced for the evidence to be built from")
        if self._executed_steps == 0:
            return self.skip(check, "the plan executed no step, so its evidence has no step row to read an elapsed time from")
        evidence = f"{BATTERY_ARTIFACTS}/evidence"
        answered = self.run("run-evidence", "--report", report.relative_to(self.project).as_posix(), "--out", evidence)
        summary_path = self.project / BATTERY_ARTIFACTS / "evidence" / "job-summary.md"
        if answered.returncode != 0 or not summary_path.is_file():
            return self.judge(check, answered, {}, False, self.first_printed_line(answered))
        summary = summary_path.read_text(encoding="utf-8")
        digest_rows = [line for line in summary.splitlines() if "Configuration digest" in line]
        rows = [line for line in summary.splitlines() if line.startswith("| ") and line.split("|")[1].strip().isdigit()]
        one_prefix = len(digest_rows) == 1 and digest_rows[0].count("sha256:") == 1
        timed = bool(rows) and all(row.split("|")[5].strip().isdigit() for row in rows)
        ok = one_prefix and timed
        judged = {"step_rows": len(rows), "every_row_timed": timed, "one_digest_prefix": one_prefix}
        return self.judge(check, answered, judged, ok, digest_rows[0].strip() if digest_rows else "the job summary carries no configuration digest row")

    # -- helpers ---------------------------------------------------------

    def debugger_capture(self, log_path: object) -> str | None:
        """Everything the debugger wrote for one step, or None where it could not be read.

        None rather than the empty string on every failure path, because a log
        that was read and held nothing and a log nobody could open are different
        answers, and the check that compares them has to be able to tell.
        """
        if not isinstance(log_path, str):
            return None
        recorded = self.project / log_path
        if not recorded.is_file():
            return None
        try:
            document = json.loads(recorded.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return f"{document.get('stdout') or ''}\n{document.get('stderr') or ''}"

    def clear_what_it_wrote(self) -> None:
        """The plans and the artifacts, out of a repository somebody may commit."""
        shutil.rmtree(self.artifacts, ignore_errors=True)
        for plan in self.project.glob("bench-battery-*.yaml"):
            # A file somebody is holding open stays, and the battery says so by
            # leaving it rather than by failing a run that is already over.
            with contextlib.suppress(OSError):  # pragma: no cover - the ordinary path removes it
                plan.unlink()

    # -- the run ---------------------------------------------------------

    def run_everything(self) -> None:
        self.check_version()
        self.check_com_ports()
        self.check_probe_listing()
        self.check_plan_strict()
        self.check_project_plans()
        if not self.hardware:
            return
        self.check_doctor()
        self.check_probe_inventory()
        self.check_reset()
        self.check_project_plan()
        self.check_failing_claim()
        self.check_permission_refusal()
        self.check_run_evidence()


def report_document(battery: Battery, version: str) -> dict:
    counts = {outcome: sum(1 for check in battery.checks if check.outcome == outcome) for outcome in (PASS, FAIL, SKIP)}
    return {
        "tool": "bench_battery",
        "ok": counts[FAIL] == 0,
        # Names, not paths. Both of these run through a home directory on an
        # ordinary installation and carry the account name with them, and a
        # report is meant to be sendable.
        "product": Path(battery.product).name,
        "product_version": version,
        "project": battery.project.name,
        "hardware": battery.hardware,
        "finished_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        "counts": counts,
        "checks": [asdict(check) for check in battery.checks],
    }


def print_summary(document: dict) -> None:
    print(f"Agentic HIL {document['product_version']} on {document['project']}, hardware checks {'on' if document['hardware'] else 'off'}")
    for check in document["checks"]:
        status = {PASS: "pass", FAIL: "FAIL", SKIP: "skip"}[check["outcome"]]
        print(f"  {status:4}  {check['name']:20}  {check['decisive_line'][:96]}")
    counts = document["counts"]
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")


def write_report(out: Path, document: dict) -> str | None:
    """The report, with its directory created, or the sentence saying why not.

    A mistyped `--out` used to throw away a completed hardware run: the write was
    outside the block that could report anything, and the traceback was all that
    was left of it.
    """
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        return f"the report could not be written to {out}: {error}"
    return None


def too_old_to_measure(version: str) -> str | None:
    """Why this installation cannot be measured by this battery, or None."""
    installed = release_of(version)
    minimum = release_of(MINIMUM_RELEASE)
    if installed is None or minimum is None or installed >= minimum:
        return None
    return (
        f"this battery measures Agentic HIL {MINIMUM_RELEASE} and newer, and the installation reports {version.strip()}. "
        "`check-plan` and `run-evidence` are not in that release, so the checks that call them would report a healthy bench as broken. "
        "No check ran."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="The project directory to run the checks in. Its own plans are checked and its own reports are written.")
    parser.add_argument("--product", default="agentic-hil", help="The installed agentic-hil to measure (default: whatever is on PATH).")
    parser.add_argument("--hardware", action="store_true", help="Also run the checks that need the probe and the board.")
    parser.add_argument("--out", default="bench-battery.json", help="Where the report is written (default: bench-battery.json).")
    parser.add_argument("--keep", action="store_true", help="Leave the plans and the artifacts the battery wrote in the project instead of removing them.")
    options = parser.parse_args(argv)

    product = shutil.which(options.product) or options.product
    if shutil.which(options.product) is None and not Path(options.product).is_file():
        print(f"bench_battery: {options.product} is not on PATH and is not a file", file=sys.stderr)
        return 2
    project = Path(options.project).expanduser().resolve()
    if not project.is_dir():
        print(f"bench_battery: {project} is not a directory", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="agentic-hil-battery-"))
    battery = Battery(product=product, project=project, root=root, hardware=options.hardware)
    version = ""
    internal: BaseException | None = None
    try:
        try:
            battery.configure()
            version = battery.run("--version", with_config=False).stdout.strip()
            refusal = too_old_to_measure(version)
            if refusal is not None:
                raise BatteryError(refusal)
            battery.run_everything()
        except BatteryError as error:
            print(f"bench_battery: {error}", file=sys.stderr)
            return 2
        except Exception as error:  # noqa: BLE001 - an internal failure is exit 2, and what already ran is still evidence
            internal = error
        document = report_document(battery, version)
        unwritten = write_report(Path(options.out), document)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if not options.keep:
            battery.clear_what_it_wrote()

    print_summary(document)
    if unwritten is not None:
        print(f"bench_battery: {unwritten}", file=sys.stderr)
        return 2
    print(f"Report written to {options.out}")
    if internal is not None:
        print(f"bench_battery: the battery stopped on a failure of its own: {internal!r}", file=sys.stderr)
        return 2
    return 0 if document["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
