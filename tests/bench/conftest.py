"""What the bench tier runs against: a probe, the board behind it, and nothing else.

This tier is the small remainder the container tier cannot cover. Everything
that needs only a real tool runs there, with the image it runs in published
beside it; what is left here is what needs a debug probe and the board it is
wired to, which is a machine somebody owns and keeps.

Nothing in this directory names a bench. There is no host, no user, no address,
no path under a home directory and no probe serial in any of these files, and no
assertion compares against one: what is asserted is the shape of an answer and
that the parts which identify hardware are present, never what they say. That is
not tidiness. These files are public, the machine is not, and a test that pinned
a serial would be a test that only passes on one bench anyway.

How a run reaches the bench:

* ``AGENTIC_HIL_BENCH=1`` says this is one. Without it every test here skips, so
  a developer's ``pytest``, the hosted matrix and the container image are
  unaffected.
* the project is this repository's own Nucleo-F446RE demo, copied into a
  temporary directory, built, and configured by ``agentic-hil init``, which is
  what reads the attached bench and binds the probe and the port it publishes.
  The operator's own configurations are never read and never written: the
  configuration root and the state root are redirected into that temporary
  directory.
* HOME is deliberately *not* redirected for the commands this tier runs. The
  machine-wide device locks live under it, and they are what keeps this run off
  a board another run is holding. A tier that isolated HOME would be a tier that
  cannot see the nightly job, or it this.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# What says this run is on a bench. Set by the operator, or by the battery in
# `tools/bench_battery.py`, and by nothing that runs unattended.
BENCH_ENV = "AGENTIC_HIL_BENCH"

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPOSITORY_ROOT / "examples" / "nucleo-f446re_demo"

# The operator's own home, read here, at collection, before the suite's own
# isolation has moved it. The commands this tier runs get it back, because the
# device locks that serialise this bench are under it; the test process itself
# stays inside the suite's sandbox, which is what keeps a stray write off the
# operator's profile.
OPERATOR_HOME = str(Path.home())

# Long enough for a flash and a boot on a slow link, short enough that a wedged
# probe fails the run rather than holding it.
COMMAND_TIMEOUT_S = 600.0
BUILD_TIMEOUT_S = 900.0


def _why_this_is_not_a_bench() -> str | None:
    if os.environ.get(BENCH_ENV) != "1":
        return f"the bench tier runs only where {BENCH_ENV}=1 says a probe and a board are attached"
    if not DEMO.is_dir():
        return "this checkout carries no Nucleo-F446RE demo, and the bench tier drives it"
    return None


_NOT_A_BENCH = _why_this_is_not_a_bench()

# Every module in this tier carries this beside its `bench` marker, and it has
# to be a mark rather than an autouse fixture. A skip raised from a fixture is
# raised after the session-scoped fixtures a test asked for have already been set
# up, so an ordinary `pytest` in a checkout would copy the demo, build its
# firmware and look for a probe before finding out it was never going to run
# anything. A `skipif` is evaluated before any fixture of the item is touched.
BENCH_ONLY = pytest.mark.skipif(_NOT_A_BENCH is not None, reason=_NOT_A_BENCH or "a probe and a board are attached")


@dataclass(frozen=True)
class Bench:
    """One configured project on this machine's bench, and how to drive it."""

    project: Path
    config: Path
    config_root: Path
    state_root: Path

    def environment(self, **overrides: str) -> dict[str, str]:
        """The environment a command gets: the operator's home, this project's configuration.

        HOME comes back because the device locks are under it. The configuration
        root and the state root do not, so nothing here can read or replace a
        configuration the operator owns.
        """
        environment = {
            **os.environ,
            "HOME": OPERATOR_HOME,
            "USERPROFILE": OPERATOR_HOME,
            "XDG_CONFIG_HOME": str(self.config_root),
            "XDG_STATE_HOME": str(self.state_root),
            "AGENTIC_HIL_CONFIG": str(self.config),
        }
        environment.update(overrides)
        return environment

    def run(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        """The CLI under test, through this interpreter, in the project directory."""
        return subprocess.run(
            [sys.executable, "-m", "agentic_hil", *arguments],
            capture_output=True,
            text=True,
            cwd=str(self.project),
            env=self.environment(**overrides),
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )

    def document(self, *arguments: str, **overrides: str) -> tuple[int, dict]:
        """One command's machine document, with the status it exited on."""
        answered = self.run(*arguments, "--json", **overrides)
        assert answered.stdout.strip(), f"{arguments} printed no document (exit {answered.returncode}):\n{answered.stderr}"
        return answered.returncode, json.loads(answered.stdout)

    def debugger_name(self) -> str:
        return sorted(self.configuration()["debuggers"])[0]

    def com_port_name(self) -> str:
        ports = sorted(self.configuration().get("com_ports") or {})
        if not ports:
            pytest.skip("this bench's configuration declares no serial port, and these plans read the board's banner over one")
        return ports[0]

    def configuration(self) -> dict:
        import yaml

        return yaml.safe_load(self.config.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Bench:
    """The demo, copied, configured against the attached hardware, once per session.

    ``agentic-hil init`` is what binds it: it reads the bench, writes the probe
    and the port it found into a configuration of its own, and verifies the
    result with doctor. A bench where that does not produce a bound configuration
    is a bench these tests cannot run on, and the skip below says so rather than
    letting every test fail one at a time.
    """
    root = tmp_path_factory.mktemp("bench")
    project = root / "nucleo-f446re_demo"
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("build", ".agentic-hil"))
    config_root = root / "config"
    state_root = root / "state"
    environment = {
        **os.environ,
        "HOME": OPERATOR_HOME,
        "USERPROFILE": OPERATOR_HOME,
        "XDG_CONFIG_HOME": str(config_root),
        "XDG_STATE_HOME": str(state_root),
    }
    environment.pop("AGENTIC_HIL_CONFIG", None)
    written = subprocess.run(
        [sys.executable, "-m", "agentic_hil", "init", "--json"],
        capture_output=True,
        text=True,
        cwd=str(project),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    if written.returncode != 0 or not written.stdout.strip():
        pytest.skip(f"`agentic-hil init` could not configure this bench: {written.stdout or written.stderr}")
    result = json.loads(written.stdout)
    config = Path(result["config_path"])
    prepared = Bench(project=project, config=config, config_root=config_root, state_root=state_root)
    verdict = prepared.run("doctor")
    if verdict.returncode != 0:
        pytest.skip(f"this bench is not bound to hardware, so no plan can run against it:\n{verdict.stdout}")
    return prepared


@pytest.fixture(scope="session")
def firmware(bench: Bench) -> Path:
    """The demo's ELF, built here, because a plan that flashes needs one.

    Built rather than expected: this tier is run on a bench by an operator or by
    ``tools/bench_battery.py``, and either way the tree it is pointed at is a
    fresh copy. A bench without the cross toolchain skips the tests that flash
    and still runs the ones that do not.
    """
    for tool in ("cmake", "arm-none-eabi-gcc"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH, so the demo firmware cannot be built here")
    for command in (["cmake", "--preset", "Debug"], ["cmake", "--build", "--preset", "Debug"]):
        built = subprocess.run(command, capture_output=True, text=True, cwd=str(bench.project), timeout=BUILD_TIMEOUT_S, check=False)
        if built.returncode != 0:
            pytest.skip(f"the demo firmware did not build here: {' '.join(command)}\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}")
    image = bench.project / "build" / "Debug" / "nucleo-f446re_demo.elf"
    assert image.is_file(), f"the build left no ELF at {image}"
    return image


def failure_worded_lines(capture: str) -> list[str]:
    """The lines of a debugger capture that carry the words a failure is read out of.

    Written out here rather than imported from the backend, deliberately. The
    claim under test is that the backend's own success marker outranks these
    words, and a test that asked the backend which lines they are would be
    asking the code under test to grade itself.
    """
    return [line for line in capture.splitlines() if "error" in line.lower() or "failed" in line.lower()]


def debugger_capture(bench: Bench, log_path: str) -> str:
    """Everything the debugger wrote for one step, out of the log the step names."""
    recorded = json.loads((bench.project / log_path).read_text(encoding="utf-8"))
    return f"{recorded.get('stdout') or ''}\n{recorded.get('stderr') or ''}"
