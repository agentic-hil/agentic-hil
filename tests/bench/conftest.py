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
  unaffected. With it, the tier has one rule for what a setup step cannot find,
  and the rule has two halves. The probe and the board are strict: a hardware
  setup step that cannot be completed is a failure and not a skip, because the
  variable is the operator's statement that a probe and a board are attached,
  and a tier that skipped its way past a false one would report success having
  executed nothing on hardware. A missing host tool is not that. An executable
  the host provides (the cross compiler and the build tools ``firmware`` looks
  for, the cross debugger ``gdb`` resolves the way the product resolves it, any
  other host package) skips the tests that need it, with one sentence naming
  the executable and where it was looked for, and the rest still run: the
  variable says nothing about the host's toolchain, and a bench without one
  still has hardware worth measuring. Each check runs once per session, before
  any test that needs it reaches the probe, and pytest reports its skip once,
  with a count, at the line of the check.
* the run ends with a ``bench tier`` section that says, for the hardware tests,
  the build half and the debug half, whether each ran, was skipped and why, or
  was not selected, so a bench that skipped a half cannot be read as a full
  tier that passed. There is one probe, so the tier runs serially: the section
  is written by the process that ran the tests, and a run split across workers
  writes none.
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
from collections import Counter
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from support import scaled_time_bound

# What says this run is on a bench. Set by the operator, or by the battery in
# `tools/bench_battery.py`, and by nothing that runs unattended.
BENCH_ENV = "AGENTIC_HIL_BENCH"

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_SOURCES = REPOSITORY_ROOT / "src"
DEMO = REPOSITORY_ROOT / "examples" / "nucleo-f446re_demo"
# The ELF the demo's Debug preset leaves, relative to a copy of the demo.
DEMO_IMAGE = Path("build") / "Debug" / "nucleo-f446re_demo.elf"

# The bench's own firmware, one C file per image, each built as the demo's
# `main.c` with the demo's startup code, linker script and toolchain file.
BENCH_FIRMWARE = Path(__file__).resolve().parent / "firmware"
# Where a built image is put inside the project before a plan flashes it. Under
# `build` because that is the artifact root a configuration that names none
# still permits, and a generated one permits the whole workspace anyway.
IMAGES_IN_THE_PROJECT = Path("build") / "bench-images"

# What a child of this tier asks the interpreter for its own package, so the
# session can compare the answer against this tree before anything reaches the
# board.
WHERE_THE_PRODUCT_CAME_FROM = "import agentic_hil; print(agentic_hil.__file__)"

# What a child of this tier runs to find the debugger the way a debug session
# finds it: the configuration loaded as the server loads it, pinning included,
# then the resolver every debug session calls. Where the product refuses the
# debugger, the child also reads what the document itself names, and whether
# that is there: by name on PATH, as the product looks a bare name up, or as a
# path from the workspace.
FIND_THE_DEBUGGER = r"""
import json
import shutil
import sys
from pathlib import Path

from agentic_hil.backends.gdbdebug import resolve_gdb_executable
from agentic_hil.config import GDB_AUTODETECT_CANDIDATES, ConfigError, load_authoritative_config, load_config, resolve_work_path

workspace, document = sys.argv[1:3]
try:
    config = load_authoritative_config(workspace)
except ConfigError as refused:
    product = refused.to_dict()
else:
    product = resolve_gdb_executable(config, "" if config.debugger is None else config.debugger.type)
configured, by_name, there = None, False, False
if product.get("error_type") == "gdb_not_found" or product.get("field") == "debug.gdb_executable":
    written = load_config(document)
    configured = written.debug.gdb_executable
    if configured is not None:
        by_name = not (Path(configured).is_absolute() or "/" in configured or "\\" in configured)
        there = shutil.which(configured) is not None if by_name else Path(resolve_work_path(written, configured)).exists()
answer = {"product": product, "candidates": GDB_AUTODETECT_CANDIDATES, "configured": configured, "by_name": by_name, "exists": there}
print(json.dumps(answer, default=str))
"""

# The operator's own home, read here, at collection, before the suite's own
# isolation has moved it. The commands this tier runs get it back, because the
# device locks that serialise this bench are under it; the test process itself
# stays inside the suite's sandbox, which is what keeps a stray write off the
# operator's profile.
OPERATOR_HOME = str(Path.home())

# Long enough for a flash and a boot on a slow link, short enough that a wedged
# probe fails the run rather than holding it.
COMMAND_TIMEOUT_S = scaled_time_bound(600.0)
BUILD_TIMEOUT_S = scaled_time_bound(900.0)


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


class MissingHostTool(pytest.skip.Exception):
    """The skip for an executable this host does not provide, and only that one.

    A class of its own so the report hook at the end of this module finds these
    skips by what raised them and never by what they say. pytest reports a skip
    raised in a fixture at each test that asked for it, which lists a half the
    host could not run test by test; the hook reports these at the check that
    raised them instead, so the tests one missing tool skipped fold into one
    line with a count. Every other skip keeps pytest's own report.
    """


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


def missing_gdb(bench: Bench) -> str | None:
    """Why the debug half cannot run on this bench, in one sentence, or None where it can.

    Asked of the product and not of PATH: a child with the bench's own
    environment loads the configuration the way the server does and calls the
    resolver every debug session calls, so a debugger the product would run is
    never missing here, whatever it is called and wherever it is. What the
    resolver reports missing is a missing host tool. So is a debugger the
    configuration names and this host has not got, told apart from one the
    product refuses for another reason by whether what the document names is
    there, never by the product's wording. The tier's own configuration never
    names such a debugger, because ``init`` writes ``debug.gdb_executable`` only
    for one it has just found on this host. Anything else the product refuses, a
    debugger that is there among it, is a bench that was not set up, and fails
    with the product's own line.
    """
    found = subprocess.run(
        [sys.executable, "-s", "-c", FIND_THE_DEBUGGER, str(bench.project), str(bench.config)],
        capture_output=True,
        text=True,
        cwd=str(bench.project),
        env=bench.environment(),
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    try:
        answer = json.loads(found.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        refuse(f"the bench tier could not ask the product for this bench's debugger (exit {found.returncode}):\n{found.stdout}\n{found.stderr}")
    product = answer["product"]
    if product.get("ok") is True:
        return None
    configured = answer["configured"]
    missing = product.get("error_type") == "gdb_not_found" or (
        product.get("field") == "debug.gdb_executable" and configured is not None and not answer["exists"]
    )
    if not missing:
        refuse(f"the bench tier could not check this bench's debugger: {product.get('error_type')}: {product.get('summary')}\n{json.dumps(product, indent=2)}")
    if configured is None:
        *others, last = answer["candidates"]
        names = f"{', '.join(others)} and {last} are" if others else f"{last} is"
        return f"{names} not on PATH and debug.gdb_executable is not set, so the debug half cannot run here: {product.get('summary')}"
    where = "which is not on PATH" if answer["by_name"] else "which does not exist"
    return f"debug.gdb_executable names {configured}, {where}, so the debug half cannot run here: {product.get('summary')}"


@pytest.fixture(scope="session")
def gdb(bench: Bench) -> None:
    """The debugger the debug half drives, checked once, before any test of that half reaches the probe.

    The debug half's counterpart of the build tools ``firmware`` looks for, and
    asked for ahead of it: a bench without a debugger skips every test that asks
    for this with the one sentence ``missing_gdb`` gives, and runs the rest.
    """
    why = missing_gdb(bench)
    if why is not None:
        raise MissingHostTool(why)


def built_where_it_stands(project: Path) -> str | None:
    """Build a copy of the demo with its own Debug preset; why it did not build, or None."""
    for command in (["cmake", "--preset", "Debug"], ["cmake", "--build", "--preset", "Debug"]):
        built = subprocess.run(command, capture_output=True, text=True, cwd=str(project), timeout=BUILD_TIMEOUT_S, check=False)
        if built.returncode != 0:
            return f"{' '.join(command)}\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}"
    return None


def put_on_board(bench: Bench, image: Path) -> dict:
    """Flash one image inside the project and start it, through the product's own plan runner.

    The report the run printed, whatever it says: the caller decides whether a
    board that refused the image is a failure or the point of the test.
    """
    plan = bench.project / f"bench-put-on-the-board-{image.stem}.yaml"
    plan.write_text(
        chr(10).join([
            "version: 3",
            f"name: bench-put-on-the-board-{image.stem}",
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
    finally:
        plan.unlink(missing_ok=True)
    try:
        return json.loads(flashed.stdout)
    except ValueError:
        return {"ok": False, "summary": f"the plan printed no report (exit {flashed.returncode}): {flashed.stderr[-1500:]}"}


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
            raise MissingHostTool(f"{tool} is not on PATH, so the demo firmware cannot be built here")
    failure = built_where_it_stands(bench.project)
    if failure is not None:
        pytest.skip(f"the demo firmware did not build here: {failure}")
    image = bench.project / DEMO_IMAGE
    assert image.is_file(), f"the build left no ELF at {image}"
    # Put that firmware on the board, once per session, through the product's
    # own plan runner. Every debug session below opens this ELF for its symbols
    # and downloads nothing, so a breakpoint on `main` is an address in this
    # build; a board carrying some other firmware runs straight past it and the
    # resume times out. The flash tests used to be the only thing that made the
    # two agree, which made every debug test depend on running after them.
    report = put_on_board(bench, image)
    if report.get("ok") is not True:
        pytest.fail(f"the demo firmware could not be put on the board before this session: {report.get('summary')}", pytrace=False)
    return image


@dataclass
class BoardImages:
    """The bench's own images, built once per session, and which one the board runs.

    Every other module in this tier assumes the board runs the demo, because its
    debug sessions read symbols from the demo's ELF. So an image is put on the
    board for one test and the demo goes back after it, by the ``board_images``
    fixture rather than by the test, which a failed assertion would leave
    halfway.
    """

    bench: Bench
    demo: Path
    build_root: Path
    built: dict[str, Path] = field(default_factory=dict)
    displaced: bool = False

    def image(self, name: str) -> Path:
        """``firmware/<name>.c`` built as the demo's ``main.c``; the ELF, inside the project."""
        if name in self.built:
            return self.built[name]
        source = BENCH_FIRMWARE / f"{name}.c"
        assert source.is_file(), f"there is no bench image called {name!r}: {source} does not exist"
        variant = self.build_root / name
        shutil.copytree(DEMO, variant, ignore=shutil.ignore_patterns("build", ".agentic-hil"))
        shutil.copyfile(source, variant / "Src" / "main.c")
        failure = built_where_it_stands(variant)
        if failure is not None:
            pytest.fail(f"the bench image {name!r} did not build: {failure}", pytrace=False)
        image = self.bench.project / IMAGES_IN_THE_PROJECT / f"{name}.elf"
        image.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(variant / DEMO_IMAGE, image)
        self.built[name] = image
        return image

    def put(self, name: str) -> dict:
        """Put one image on the board through the product; the plan's report."""
        image = self.image(name)
        # Marked before the flash rather than after it: a flash that fails
        # halfway leaves the board running neither image.
        self.displaced = True
        return put_on_board(self.bench, image)

    def restore(self) -> None:
        """The demo back on the board, if anything else was put there."""
        if not self.displaced:
            return
        report = put_on_board(self.bench, self.demo)
        if report.get("ok") is not True:
            pytest.fail(f"the demo firmware could not be put back on the board, so every module after this one would run against the wrong image: {report.get('summary')}", pytrace=False)
        self.displaced = False


@pytest.fixture(scope="session")
def board_image_builds(bench: Bench, firmware: Path, tmp_path_factory: pytest.TempPathFactory) -> BoardImages:
    """The session's builds of the bench's own images, shared by every test that puts one on the board."""
    return BoardImages(bench=bench, demo=firmware, build_root=tmp_path_factory.mktemp("bench-images"))


@pytest.fixture
def board_images(board_image_builds: BoardImages) -> Iterator[BoardImages]:
    """The bench's own images for one test, and the demo back on the board after it."""
    yield board_image_builds
    board_image_builds.restore()


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


# The parts of the tier the closing section accounts for, by what a test of each
# needs beyond the probe and the board: nothing, the demo firmware built, or a
# debugger on top of that.
HALVES = ("hardware tests", "build half", "debug half")

# What became of one test, in the order the section counts them, and the kinds
# that mean its body ran.
OUTCOME_KINDS = (
    "passed",
    "failed",
    "errored in setup",
    "errored in teardown",
    "skipped for a missing host tool",
    "skipped",
    "not reached",
)
EXECUTED = frozenset({"passed", "failed", "errored in teardown"})

SELECTED = pytest.StashKey[dict[str, list[str]]]()
OUTCOMES = pytest.StashKey[dict[str, tuple[str, str]]]()


def half_of(item: pytest.Item) -> str:
    needs = getattr(item, "fixturenames", ())
    if "gdb" in needs:
        return "debug half"
    if "firmware" in needs:
        return "build half"
    return "hardware tests"


def first_line_of_why(report: pytest.TestReport, excinfo: pytest.ExceptionInfo[BaseException] | None) -> str:
    """The first line of why a test did not pass, as the run reported it."""
    if report.skipped and isinstance(report.longrepr, tuple):
        why = report.longrepr[2].removeprefix("Skipped: ")
    elif excinfo is not None and isinstance(excinfo.value, pytest.fail.Exception):
        why = excinfo.value.msg or ""
    elif excinfo is not None:
        why = excinfo.exconly()
    else:
        why = str(report.longrepr or "")
    lines = why.strip().splitlines()
    return lines[0] if lines else ""


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """A missing host tool reported at its check, and every test's outcome kept for the closing section."""
    report = yield
    excinfo = call.excinfo
    missing_tool = excinfo is not None and isinstance(excinfo.value, MissingHostTool) and report.skipped
    if missing_tool:
        raised = excinfo.traceback[-1]
        report.longrepr = (str(raised.path), raised.lineno + 1, f"Skipped: {excinfo.value.msg}")
    outcomes = item.config.stash.setdefault(OUTCOMES, {})
    kind = None
    if report.when == "setup" and report.failed:
        kind = "errored in setup"
    elif report.when == "setup" and report.skipped:
        kind = "skipped for a missing host tool" if missing_tool else "skipped"
    elif report.when == "call":
        kind = report.outcome
    elif report.failed and outcomes.get(item.nodeid, ("", ""))[0] == "passed":
        kind = "errored in teardown"
    if kind is not None:
        outcomes[item.nodeid] = (kind, "" if report.passed else first_line_of_why(report, excinfo))
    return report


def pytest_collection_finish(session: pytest.Session) -> None:
    """The tests of the tier this run selected, by half, for the section that closes it."""
    selected: dict[str, list[str]] = {half: [] for half in HALVES}
    for item in session.items:
        if item.get_closest_marker("bench") is not None:
            selected[half_of(item)].append(item.nodeid)
    if any(selected.values()):
        session.config.stash[SELECTED] = selected


def verdict(outcomes: list[tuple[str, str]]) -> str:
    """One half's line: whether it ran, what came of its tests, and the first line of each reason one did not pass."""
    if not outcomes:
        return "not selected"
    counted = Counter(kind for kind, _ in outcomes)
    counts = ", ".join(f"{counted[kind]} {kind}" for kind in OUTCOME_KINDS if counted[kind])
    missing = counted["skipped for a missing host tool"]
    if EXECUTED & counted.keys():
        head = f"ran, {counts}"
    elif missing == len(outcomes):
        head = f"skipped {missing} for a missing host tool"
    elif missing + counted["skipped"] == len(outcomes):
        head = f"skipped {len(outcomes)}" + (f", {missing} for a missing host tool" if missing else "")
    else:
        head = f"did not run, {counts}"
    reasons = "; ".join(dict.fromkeys(why for _, why in outcomes if why))
    return f"{head}: {reasons}" if reasons else head


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    """Which halves of the tier ran, so a bench that skipped one is not read as a full tier that passed."""
    selected = config.stash.get(SELECTED, None)
    if os.environ.get(BENCH_ENV) != "1" or selected is None or config.option.collectonly:
        return
    outcomes = config.stash.get(OUTCOMES, {})
    terminalreporter.write_sep("=", "bench tier")
    for half in HALVES:
        reached = [outcomes.get(nodeid, ("not reached", "")) for nodeid in selected[half]]
        terminalreporter.write_line(f"{half}: {verdict(reached)}")
