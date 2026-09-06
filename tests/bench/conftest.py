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
  unaffected. With it, a setup step that cannot be completed is a failure and
  not a skip: the variable is the operator's statement that a probe and a board
  are attached, and a tier that skipped its way past a false one would report
  success having executed nothing on hardware.
* the CLI every command here runs is this checkout. The user site directory is
  left out of the child and this tree's ``src`` goes to the front of its import
  path, and the session fixture then asks the child where its ``agentic_hil``
  came from and refuses a bench where the answer is outside this tree. A regular
  install of the released product outranks an editable install of a checkout, so
  without that the gate would flash the board with the release and report the
  branch green.
* the project is this repository's own Nucleo-F446RE demo, copied into a
  temporary directory, built, and configured by ``agentic-hil init``, which is
  what reads the attached bench and binds the probe and the port it publishes.
  The operator's own configurations are never read and never written: the
  configuration root and the state root are redirected into that temporary
  directory, under both the POSIX names and the Windows ones, and the
  configuration ``init`` selects is checked against that root before any test
  runs.
* HOME is deliberately *not* redirected for the commands this tier runs. The
  machine-wide device locks live under it, and they are what keeps this run off
  a board another run is holding. A tier that isolated HOME would be a tier that
  could meet the nightly job on the board without either of them knowing it.
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
CHECKOUT_SOURCES = REPOSITORY_ROOT / "src"
DEMO = REPOSITORY_ROOT / "examples" / "nucleo-f446re_demo"

# What a child of this tier asks the interpreter for its own package, so the
# session can compare the answer against this tree before anything reaches the
# board.
WHERE_THE_PRODUCT_CAME_FROM = "import agentic_hil; print(agentic_hil.__file__)"

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


def child_command(*arguments: str) -> list[str]:
    """The CLI under test, through this interpreter, with the user site left out.

    ``-s`` because the per-user site directory is reached through ``sys.path``
    ahead of an appended editable finder, so a regular install of the released
    product there wins over the checkout this tier is meant to be measuring. The
    import path in ``Bench.environment`` puts this tree's ``src`` in front of
    what is left, and the session fixture checks the result rather than trusting
    either.
    """
    return [sys.executable, "-s", "-m", "agentic_hil", *arguments]


def import_path(existing: str | None = None) -> str:
    """This checkout's sources, ahead of whatever the caller's path already said."""
    already = [entry for entry in (existing or "").split(os.pathsep) if entry]
    return os.pathsep.join([str(CHECKOUT_SOURCES), *already])


def isolated_environment(config_root: Path, state_root: Path, **overrides: str) -> dict[str, str]:
    """The operator's home, this session's configuration and state, this checkout's code.

    HOME comes back because the device locks are under it. The configuration
    root and the state root do not, and both platforms' names for them are set:
    the product reads ``XDG_CONFIG_HOME`` and ``XDG_STATE_HOME`` on POSIX and
    ``APPDATA`` and ``LOCALAPPDATA`` on Windows, so a redirect that moved only
    the first pair left every command here writing into the operator's own roots
    on a supported host.
    """
    environment = {
        **os.environ,
        "HOME": OPERATOR_HOME,
        "USERPROFILE": OPERATOR_HOME,
        "XDG_CONFIG_HOME": str(config_root),
        "XDG_STATE_HOME": str(state_root),
        "APPDATA": str(config_root),
        "LOCALAPPDATA": str(state_root),
        "PYTHONPATH": import_path(os.environ.get("PYTHONPATH")),
    }
    environment.update(overrides)
    return environment


def not_the_checkout(module_file: str) -> str | None:
    """Why the ``agentic_hil`` a child resolved is not this tree, or None where it is."""
    try:
        inside = Path(module_file).resolve().is_relative_to(REPOSITORY_ROOT)
    except (OSError, ValueError):  # pragma: no cover - a path this host cannot resolve is not this tree
        inside = False
    if inside:
        return None
    return (
        f"the bench tier resolved `agentic_hil` to {module_file}, which is outside this checkout at {REPOSITORY_ROOT}. "
        "A run against another copy would drive the board with code nobody is reviewing and report this branch on the result."
    )


def outside_this_runs_root(config: Path, config_root: Path) -> str | None:
    """Why the configuration ``init`` selected is not this session's, or None where it is."""
    try:
        inside = config.resolve().is_relative_to(config_root.resolve())
    except (OSError, ValueError):  # pragma: no cover - a path this host cannot resolve is not this root
        inside = False
    if inside:
        return None
    return (
        f"`agentic-hil init` selected {config}, which is not under this session's configuration root {config_root}. "
        "The redirect this tier promises did not hold, so the commands below would read and write a configuration somebody owns."
    )


def refuse(why: str) -> None:
    """A bench that was declared and could not be set up, reported as what it is.

    The variable is the operator's statement that a probe and a board are
    attached. When it turns out to be false there is nothing here to measure,
    and a skip is a run that exits 0 having executed nothing on hardware. The
    sibling entry point already treats these conditions this way:
    ``tools/bench_battery.py`` exits 2 when ``init`` cannot configure the
    project and records a FAIL when doctor exits non-zero.
    """
    pytest.fail(why, pytrace=False)


@dataclass(frozen=True)
class Bench:
    """One configured project on this machine's bench, and how to drive it."""

    project: Path
    config: Path
    config_root: Path
    state_root: Path

    def environment(self, **overrides: str) -> dict[str, str]:
        """The environment a command gets: the operator's home, this project's configuration."""
        return isolated_environment(self.config_root, self.state_root, AGENTIC_HIL_CONFIG=str(self.config), **overrides)

    def run(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        """The CLI under test, through this interpreter, in the project directory."""
        return subprocess.run(
            child_command(*arguments),
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
    result with doctor. Four things have to hold before any test runs, and each
    of them fails the session by name rather than skipping it: the CLI is this
    checkout, ``init`` answered, the configuration it selected is under this
    session's own root, and doctor says the result is bound to hardware.
    """
    root = tmp_path_factory.mktemp("bench")
    project = root / "nucleo-f446re_demo"
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("build", ".agentic-hil"))
    config_root = root / "config"
    state_root = root / "state"
    environment = isolated_environment(config_root, state_root)
    environment.pop("AGENTIC_HIL_CONFIG", None)
    resolved = subprocess.run(
        [sys.executable, "-s", "-c", WHERE_THE_PRODUCT_CAME_FROM],
        capture_output=True,
        text=True,
        cwd=str(project),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        refuse(f"the bench tier could not establish which `agentic_hil` its commands would run:\n{resolved.stdout}\n{resolved.stderr}")
    shadowed = not_the_checkout(resolved.stdout.strip())
    if shadowed is not None:
        refuse(shadowed)
    written = subprocess.run(
        child_command("init", "--json"),
        capture_output=True,
        text=True,
        cwd=str(project),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    if written.returncode != 0 or not written.stdout.strip():
        refuse(f"`agentic-hil init` could not configure this bench: {written.stdout or written.stderr}")
    result = json.loads(written.stdout)
    config = Path(result["config_path"])
    strayed = outside_this_runs_root(config, config_root)
    if strayed is not None:
        refuse(strayed)
    prepared = Bench(project=project, config=config, config_root=config_root, state_root=state_root)
    verdict = prepared.run("doctor")
    if verdict.returncode != 0:
        refuse(f"this bench is not bound to hardware, so no plan can run against it:\n{verdict.stdout}")
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
    # Put that firmware on the board, once per session, through the product's
    # own plan runner. Every debug session below opens this ELF for its symbols
    # and downloads nothing, so a breakpoint on `main` is an address in this
    # build; a board carrying some other firmware runs straight past it and the
    # resume times out. The flash tests used to be the only thing that made the
    # two agree, which made every debug test depend on running after them.
    plan = bench.project / "bench-firmware-on-the-board.yaml"
    plan.write_text(
        chr(10).join([
            "version: 3",
            "name: bench-firmware-on-the-board",
            "steps:",
            f"  - device: {bench.debugger_name()}",
            "    action: flash",
            f"    image_path: {image.relative_to(bench.project).as_posix()}",
            f"  - device: {bench.debugger_name()}",
            "    action: reset",
            "    mode: run",
            "",
        ]),
        encoding="utf-8",
    )
    try:
        flashed = bench.run("test-reactor", "--test-config", plan.name, "--json")
        try:
            report = json.loads(flashed.stdout)
        except ValueError:
            report = {}
        if report.get("ok") is not True:
            pytest.fail(f"the demo firmware could not be put on the board before this session: {report.get('summary') or flashed.stderr[-1500:]}", pytrace=False)
    finally:
        plan.unlink(missing_ok=True)
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
