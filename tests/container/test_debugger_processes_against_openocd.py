"""What the debugger backend does with the real OpenOCD as a process.

Two conditions from #485 that a fake can only approximate. A real OpenOCD that
hangs is the shape of a probe that stopped answering mid-command: the process
is there, its helpers are there, and nothing it prints says when it will end.
The backend's contract is that `debuggers.<name>.timeout_s` ends it, with its
process tree, that the log records `timed_out` true, and that the tool answers
`error_type` `timeout` with `likely_causes` and a `log_path`. A hung OpenOCD is
produced here without a probe by its own `sleep` command, which this image's
OpenOCD runs before `init` and which nothing interrupts.

The second is the spelling. `executable: openocd`, with nothing but the name, is
how a Linux host that installed the distribution's package configures it, and
the whole of the resolution happens when the configuration loads: the pinned
path is the one PATH names, and the program that runs is that one. `executable:
null` on an entry that names its board is the same lookup with nothing
written at all.

Where the hang tests stood before any change, so their red is read for what it
is. The deadline was enforced, the tree was signalled, the log recorded the
timeout and the tool answered it; under `docker run --init` both hang tests
pass on the code before this branch. What made them red as the job runs them
is the reap's last question. This tier's pytest is PID 1 of its container, so
the OpenOCD whose wrapper shell died in the same SIGKILL is adopted by pytest,
which never waits for a process it did not start; the zombie keeps its process
group, `killpg(pgid, 0)` keeps counting it, and the reap raised "Process group
remained active after SIGKILL" out of `spawn_command` and again out of
`service.close()`, before either test reached an assertion of its own. The
doctor test ended the same way, with `doctor --json` writing nothing on stdout.
Reproduced in twelve lines as PID 1: a shell around `sleep` in its own session,
SIGKILL to the group, wait the shell; the grandchild is `State: Z` with
`PPid: 1` and `killpg(pgid, 0)` still succeeds.

The decision these tests hold is the product's, not the job's: a group whose
remaining members are all zombies is an emptied group, and the tier runs as
PID 1 on purpose, because that is the shape of `agentic-hil mcp-stdio` as a
container's entrypoint with a debugger hanging under it. Adding `--init` to
the job would turn the tier green and change nothing an operator gets, which
is why the first test below asserts the pid.

Recorded against the OpenOCD this image installs; the version line is asserted
rather than noted, so the record cannot go stale silently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import scaled_time_bound

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

# The deadline the hung OpenOCD is given, and how long the whole call may take
# before the deadline is judged not to have been enforced. OpenOCD is told to
# sleep for 60 s, so a call that only ends when OpenOCD does ends far outside.
HANG_TIMEOUT_S = 1
CALL_CEILING_S = 15.0
REAP_CEILING_S = 5.0


def hung_openocd_wrapper(directory: Path, token: str) -> Path:
    """A program that starts the real OpenOCD and leaves it sleeping.

    A shell script rather than `exec`, so the process tree under the backend
    is two deep: the shell, and the OpenOCD it started. A termination that
    reaches only the direct child leaves the OpenOCD behind, which is what the
    process-table check after the call is for. `token` is put on OpenOCD's
    command line (as an `echo` it never reaches) and in the wrapper's own
    name, so both can be found in `/proc` by a string nothing else carries.
    """
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / f"openocd-hung-{token}"
    wrapper.write_text(f'#!/bin/sh\nopenocd -c "sleep 60000" -c "echo {token}"\n', encoding="utf-8")
    os.chmod(wrapper, 0o755)
    return wrapper


def running_processes_carrying(token: str) -> list[str]:
    """The command lines in `/proc` that carry `token` and belong to a process that runs.

    A zombie is a process that has ended and has not been waited for; it runs
    nothing and holds nothing, so it is not counted.
    """
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command_line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
            state = next(line for line in (entry / "status").read_text(encoding="utf-8").splitlines() if line.startswith("State:"))
        except (OSError, StopIteration):
            continue
        if token in command_line and "Z" not in state.split(":", 1)[1]:
            found.append(command_line.strip())
    return found


def wait_until_none_carry(token: str, timeout_s: float) -> list[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        survivors = running_processes_carrying(token)
        if not survivors:
            return []
        time.sleep(0.05)
    return running_processes_carrying(token)


def written_log(config, result: dict) -> dict:
    log_path = Path(result["log_path"])
    if not log_path.is_absolute():
        log_path = Path(config.work_dir) / log_path
    return json.loads(log_path.read_text(encoding="utf-8"))


def test_the_openocd_this_image_installs_is_the_one_these_tests_were_recorded_against() -> None:
    """Recorded 2026-09-06 against the Debian package this image's base carries."""
    version = subprocess.run([shutil.which("openocd"), "--version"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)

    assert "Open On-Chip Debugger 0.12.0" in version.stdout + version.stderr, version.stdout + version.stderr


def test_this_tier_is_the_first_process_of_its_container() -> None:
    """The orphans a reap leaves are this process's own, and nobody else collects them.

    The job runs pytest as PID 1 without an init, and the hang tests below are
    only a test of the product's reap because of it: under an init the kernel
    hands every orphan to something that waits for it, and a reap that counted
    zombies as members would never be found out. A run of this tier under
    `--init` fails here, by design, rather than passing the hang tests for a
    reason the product had no part in.
    """
    assert os.getpid() == 1, f"this tier runs as PID 1 on purpose; pid {os.getpid()} means an init is reaping orphans in the product's place"


def test_a_hanging_openocd_is_reaped_and_answers_timeout(tmp_path: Path) -> None:
    """The timeout contract against the real tool: the answer, the log, the process table."""
    from agentic_hil.config import load_config
    from agentic_hil.tools import AgenticHILToolService

    project = tmp_path / "project"
    project.mkdir()
    token = f"agentic-hil-hung-{os.getpid()}-{int(time.time() * 1000)}"
    wrapper = hung_openocd_wrapper(tmp_path / "toolchain", token)
    config_path = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", executable=repr(str(wrapper)), timeout_s=HANG_TIMEOUT_S)
    config = load_config(str(config_path))
    service = AgenticHILToolService(config)
    started = time.monotonic()
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    elapsed = time.monotonic() - started

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "timeout", json.dumps(result)
    assert result["summary"] == "Debugger command timed out."
    assert result["likely_causes"], result
    assert result["log_path"], result
    assert elapsed < scaled_time_bound(CALL_CEILING_S), f"the call took {elapsed:.1f} s against a {HANG_TIMEOUT_S} s deadline"

    log = written_log(config, result)
    assert log["timed_out"] is True, log
    assert str(wrapper) in log["command"], log["command"]

    survivors = wait_until_none_carry(token, REAP_CEILING_S)
    assert survivors == [], f"processes survived the reap: {survivors}"


def test_a_bare_openocd_name_pins_the_program_path_names_and_runs_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`executable: openocd` resolves to this image's OpenOCD and that OpenOCD is what runs.

    Loaded the way every production entry point loads it, through
    `load_authoritative_config`, because that is where pinning happens; a
    `load_config` on the bare path reads the file and pins nothing.

    With no probe attached the run cannot succeed, and which refusal it ends in
    belongs to other tests; what this one holds is that the refusal is not
    `debugger_not_found` and that the log names the resolved path as the program.
    """
    from agentic_hil.config import load_authoritative_config
    from agentic_hil.tools import AgenticHILToolService

    project = tmp_path / "project"
    project.mkdir()
    config_path = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", executable="openocd")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path))
    config = load_authoritative_config(project)

    assert config.debugger.executable == shutil.which("openocd"), config.debugger.executable
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] != "debugger_not_found", json.dumps(result)
    assert result.get("log_path"), json.dumps(result)
    assert written_log(config, result)["command"].startswith(shutil.which("openocd")), written_log(config, result)["command"]


def test_an_omitted_executable_runs_the_openocd_path_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`executable: null` on an entry that names its board: nobody wrote a path, and this image's OpenOCD is what runs.

    The same claim as the bare name's, reached from the spelling `init` leaves
    behind. What is asserted is the outcome: the pinned executable is the one
    PATH names, the run is not `debugger_not_found`, and the log's command
    starts with that path. Where the lookup happens, at load, is the code's
    choice and is pinned in the unit tier.
    """
    from agentic_hil.config import load_authoritative_config
    from agentic_hil.tools import AgenticHILToolService

    project = tmp_path / "project"
    project.mkdir()
    config_path = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", executable="null")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path))
    config = load_authoritative_config(project)

    assert config.debugger.executable == shutil.which("openocd"), config.debugger.executable
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] != "debugger_not_found", json.dumps(result)
    assert result.get("log_path"), json.dumps(result)
    assert written_log(config, result)["command"].startswith(shutil.which("openocd")), written_log(config, result)["command"]


def test_a_bare_openocd_name_the_path_lacks_is_refused_when_the_configuration_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same spelling on a host that has not installed the package."""
    from agentic_hil.config import ConfigError, load_authoritative_config

    project = tmp_path / "project"
    project.mkdir()
    config_path = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", executable="openocd")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(ConfigError) as raised:
        load_authoritative_config(project)

    assert raised.value.error_type == "config_invalid"
    assert raised.value.summary == "Configured executable could not be resolved at startup."
    assert raised.value.details["field"] == "debuggers.dut.executable"


def test_doctor_reports_a_hanging_openocd_as_a_timeout_document(tmp_path: Path) -> None:
    """The operator's view of the same hang: a document on stdout and exit 1, not a wait of a minute.

    `doctor` runs the configured debugger's version check under the entry's own
    deadline, so the wrapper that never answers is met on the first command a
    new bench runs.
    """
    project = tmp_path / "project"
    project.mkdir()
    token = f"agentic-hil-hung-doctor-{os.getpid()}-{int(time.time() * 1000)}"
    wrapper = hung_openocd_wrapper(tmp_path / "toolchain", token)
    config_path = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", executable=repr(str(wrapper)), timeout_s=HANG_TIMEOUT_S)
    environment = {**os.environ, "AGENTIC_HIL_CONFIG": str(config_path)}
    started = time.monotonic()

    answered = subprocess.run(
        [sys.executable, "-m", "agentic_hil", "doctor", "--json"],
        capture_output=True,
        text=True,
        cwd=str(project),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert answered.returncode == 1, answered.stdout + answered.stderr
    assert answered.stdout.strip(), f"doctor --json wrote no document on stdout; stderr was:\n{answered.stderr}"
    document = json.loads(answered.stdout)
    check = document["debuggers"]["dut"]["check"]
    assert check["ok"] is False, check
    assert check["error_type"] == "timeout", check
    assert check["summary"] == "Debugger version check timed out.", check
    assert elapsed < scaled_time_bound(CALL_CEILING_S), f"the command took {elapsed:.1f} s against a {HANG_TIMEOUT_S} s deadline"
    assert wait_until_none_carry(token, REAP_CEILING_S) == []
