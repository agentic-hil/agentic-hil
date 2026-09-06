"""What the container tier runs against, and what it refuses to run without.

The suite under ``tests/`` proves this code against fakes written beside it, so
it can only be as right as the person who wrote the fake was about the tool.
Four defects reached a bench through exactly that gap: uv writes ``python`` at
the ``[tool]`` level of its receipt rather than under ``[tool.options]``, a
virtual environment's process resolves its image to the system interpreter so
``/proc/<pid>/exe`` never names the environment, a procfs directory's creation
time is the moment of the first lookup rather than the process's start, and
OpenOCD prints a failure-worded line after a reset that succeeded. A fake cannot
tell anyone any of that.

So this tier runs the real tools. It needs uv, an OpenOCD binary, pyOCD, curl,
``/proc`` and the package index, and it needs no probe and no board: what needs
those is the bench tier under ``tests/bench``. ``tools/container/Dockerfile`` is the image it
is published with and the image the hosted CI job builds.

The gate has two halves, because a skip and an error are different answers.
``AGENTIC_HIL_CONTAINER_TESTS=1`` says a run means to be in the image, and every
test here skips without it, so a developer's ``pytest`` and the hosted matrix
are unaffected. A run that does set it and cannot find the marker the image
build writes, or uv, or either debugger, or curl, or ``/proc``, ends the
collection with an error naming what is missing. A required check that reported success over
fifteen skipped tests would be a check measuring nothing and saying nothing
about it, and a machine that satisfied the variable outside the image could be a
bench with a probe attached.

Three arrangements are shared here because every test needs at least one:

* an isolated uv tool directory, so nothing these tests install can reach the
  operator's own installation. It is shaped ``<temporary>/uv/tools`` on purpose:
  ``agentic_hil.upgrade.owning_manager`` recognises a uv tool installation by
  that layout in ``sys.prefix``, which is the layout uv itself creates under its
  data directory, and a tool directory shaped any other way is read as a plain
  ``uv pip`` installation whose receipt is never consulted.
* a wheelhouse of this checkout built at chosen version numbers, which is how a
  test gets an installation that is provably below, at, or above what is
  available without waiting for a release. The wheels are built by the real
  build backend from the tree under test, so the code they carry is this code.
* one uv cache for the whole session, because every install here would otherwise
  re-download this distribution's dependency set.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

# What says a run means to be in the image this tier is published with. Set by
# the Dockerfile and by the job that runs it. It says what a run intends and
# nothing about what is here, which is why it is not the whole gate.
CONTAINER_ENV = "AGENTIC_HIL_CONTAINER_TESTS"

# What says a run really is in it. Written by the image build and by nothing
# else, so a run that declared the image and cannot find this is a run whose
# result would be a green tier that measured nothing, or worse, a machine with a
# bench attached.
IMAGE_MARKER = Path("/etc/agentic-hil/container-test-image")

# The process table this tier reads. Named so a test can put a directory that is
# not there in front of the gate without moving the real one.
PROC_ROOT = "/proc"

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Three version numbers, and each is chosen against the one thing it has to be
# true of. `BELOW_EVERY_RELEASE` has to sort under whatever the index publishes,
# because the result that names a recorded option as what holds an installation
# back is only reachable when the index publishes something newer. The two above
# it have to sort over everything the index publishes, so a resolution that
# reaches the index cannot quietly satisfy a requirement from there instead of
# from the wheelhouse, and they have to differ, because one of them is the
# release the other is asked to move to.
BELOW_EVERY_RELEASE = "0.0.1"
ABOVE_EVERY_RELEASE = "99.0.0"
ONE_RELEASE_ABOVE_THAT = "99.0.1"

# A resolution cut-off recorded in the receipt by `uv tool install
# --exclude-newer`. Which moment it names decides nothing the tests read: what
# they read is that uv recorded the option at all and that the result names it.
# It has to be late enough that this distribution's dependencies resolve under
# it, which is the only way the value can matter.
RECORDED_EXCLUDE_NEWER = "2026-01-01T00:00:00Z"

# How long a child of these tests may take before it is a failure rather than a
# slow machine. Generous: an install resolves a dependency set over the network.
INSTALL_TIMEOUT_S = 600.0
COMMAND_TIMEOUT_S = 300.0


def why_this_run_is_not_in_the_image() -> str | None:
    """Why this run never meant to be here, or None where it says it is.

    The one condition that is a skip. A developer's `pytest` in a checkout, the
    hosted matrix and anything else that did not set the variable never claimed
    to be in the image, and a tier that skips itself for them is the tier
    working. Everything else is `missing_from_the_image`.
    """
    if os.environ.get(CONTAINER_ENV) != "1":
        return f"the container tier runs only where {CONTAINER_ENV}=1 says the real tools are present"
    return None


def missing_from_the_image(which: Callable[[str], str | None] | None = None, proc_root: Path | None = None, marker: Path | None = None) -> str | None:
    """What the published image would have and this host has not, or None.

    The marker is first and is a file the image build writes, because the
    variable above is something an operator can export on a Linux bench with a
    probe plugged in, and one test in this tier runs `agentic-hil init`, which
    reads the attached bench, enumerates probes and connects. The root
    conftest's guard against that is a monkeypatch inside the test process and a
    subprocess never sees it. A file in the image cannot be exported by
    accident.
    """
    which = which or shutil.which
    proc_root = proc_root or Path(PROC_ROOT)
    marker = marker or IMAGE_MARKER
    if not marker.is_file():
        return f"{marker} is not here, so this is not the container test image and this tier cannot establish that no bench is attached"
    if which("uv") is None:
        return "uv is not on PATH, and this tier reads receipts uv writes"
    if which("openocd") is None:
        return "openocd is not on PATH, and this tier drives the real debugger backend"
    if which("pyocd") is None:
        return "pyocd is not on PATH, and this tier drives the pyOCD backend against nothing on USB"
    if which("curl") is None:
        return "curl is not on PATH, and this tier runs install.sh's fetch route, which downloads the pinned uv installer with it"
    if not proc_root.is_dir():
        return f"this host publishes no {proc_root}, and this tier reads the process table out of it"
    return None


def image_gate(not_in_the_image: str | None, missing: str | None) -> None:
    """Stop the collection where a run said it was in the image and it is not.

    A `skipif` cannot tell those two apart, and the difference is the whole
    value of the job that runs this tier: any condition that made the old gate
    return a reason turned all of its tests into skips, pytest exited 0, and the
    required check reported success over a tier that measured nothing. Green
    over fifteen skips and green over fifteen passes were indistinguishable to
    the workflow.
    """
    if not_in_the_image is None and missing is not None:
        raise RuntimeError(f"{CONTAINER_ENV}=1 says this run is in the container test image, and it is not: {missing}")


_NOT_IN_THE_IMAGE = why_this_run_is_not_in_the_image()
image_gate(_NOT_IN_THE_IMAGE, missing_from_the_image() if _NOT_IN_THE_IMAGE is None else None)

# Every module in this tier carries this beside its `container` marker, and it
# has to be a mark rather than an autouse fixture. A skip raised from a fixture
# is raised after the session-scoped fixtures a test asked for have already been
# set up, so an ordinary `pytest` in a checkout would build a wheelhouse and
# drive uv before finding out it was never going to run anything. A `skipif` is
# evaluated before any fixture of the item is touched.
CONTAINER_ONLY = pytest.mark.skipif(_NOT_IN_THE_IMAGE is not None, reason=_NOT_IN_THE_IMAGE or "the real tools are present")


def a_line_within(stream: IO[str], seconds: float) -> str | None:
    """One line off a pipe, or None when nothing arrived inside the bound.

    An unbounded `readline` sits ahead of every assertion in the process-table
    test, and no timeout plugin is configured anywhere in this repository, so a
    server that starts and never answers ran the job to its ceiling with no
    useful failure. A server that dies closes its stdout and the read returns at
    once; only a live but silent one needed this.
    """
    answered: list[str] = []
    reader = threading.Thread(target=lambda: answered.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(seconds)
    return answered[0] if answered else None


@pytest.fixture(scope="session")
def uv_binary() -> str:
    resolved = shutil.which("uv")
    if resolved is None:  # pragma: no cover - the autouse skip covers a run without it
        pytest.skip("uv is not on PATH")
    return resolved


@pytest.fixture(scope="session")
def uv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One cache for the session, so a dependency set is fetched once.

    Session-scoped rather than taken from the environment: the suite gives every
    test its own ``XDG_CACHE_HOME``, so uv's default cache location moves under
    each test and every install would start from nothing.
    """
    return tmp_path_factory.mktemp("uv-cache")


def _build_wheel(version: str, into: Path, work: Path) -> Path:
    """One wheel of this checkout, carrying the version number it is asked for.

    The tree is copied rather than edited in place, and only the two files that
    carry the number for a wheel are rewritten: the distribution version in
    ``pyproject.toml`` and ``__version__``, which is what the CLI reports and
    what an upgrade compares before and after. Everything else is this
    checkout's own code, which is the point: these wheels are how the code under
    test gets installed by the real uv at a version a test can reason about.

    ``--no-isolation`` because the image already carries the build backend this
    project declares, and a build that fetched one would be a second network
    dependency for a step that has nothing to resolve.
    """
    source = work / version
    source.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy2(REPOSITORY_ROOT / name, source / name)
    shutil.copytree(REPOSITORY_ROOT / "src", source / "src")
    project = source / "pyproject.toml"
    project.write_text(
        re.sub(r'^version = "[^"]+"', f'version = "{version}"', project.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )
    package = source / "src" / "agentic_hil" / "__init__.py"
    package.write_text(
        re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"', package.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(into), str(source)],
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )
    assert built.returncode == 0, f"building the {version} wheel failed:\n{built.stdout}\n{built.stderr}"
    wheels = sorted(into.glob(f"agentic_hil-{version}-*.whl"))
    assert len(wheels) == 1, f"building {version} left {wheels}"
    return wheels[0]


@dataclass(frozen=True)
class Wheelhouse:
    """Local wheels of this checkout, and the directories a test points uv at.

    ``every_version`` holds all of them; ``only`` builds a directory holding the
    named ones, which is how a test installs at one version and then offers uv a
    newer one without reinstalling anything.
    """

    every_version: Path
    wheels: dict[str, Path]
    _subsets: Path

    def only(self, *versions: str) -> Path:
        subset = self._subsets / "-".join(versions)
        if not subset.is_dir():
            subset.mkdir(parents=True)
            for version in versions:
                shutil.copy2(self.wheels[version], subset / self.wheels[version].name)
        return subset


@pytest.fixture(scope="session")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Wheelhouse:
    root = tmp_path_factory.mktemp("wheelhouse")
    into = root / "wheels"
    into.mkdir()
    work = root / "build"
    work.mkdir()
    wheels = {version: _build_wheel(version, into, work) for version in (BELOW_EVERY_RELEASE, ABOVE_EVERY_RELEASE, ONE_RELEASE_ABOVE_THAT)}
    return Wheelhouse(every_version=into, wheels=wheels, _subsets=root / "subsets")


def a_closed_local_port() -> int:
    """A port on the loopback interface that nothing is listening on.

    Bound and released, so the number is one this host really did hand out and a
    connection to it is refused rather than routed somewhere. It is how a test
    points every network route a child has at something that answers at once and
    answers nothing, without depending on the machine being offline.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class UvTool:
    """One isolated uv tool installation, and the commands that drive it."""

    directory: Path
    bin_directory: Path
    cache: Path
    uv: str

    @property
    def environment_root(self) -> Path:
        return self.directory / "agentic-hil"

    @property
    def launcher(self) -> Path:
        return self.bin_directory / "agentic-hil"

    @property
    def interpreter(self) -> Path:
        return self.environment_root / "bin" / "python"

    @property
    def receipt(self) -> Path:
        return self.environment_root / "uv-receipt.toml"

    def environment(self, **overrides: str) -> dict[str, str]:
        """The environment a child gets: this test's own, pointed at this tool.

        The suite has already moved HOME, the configuration root, the state root
        and temporary storage into this test's sandbox, so what is added here is
        only what says which uv installation is being talked about.
        ``AGENTIC_HIL_CONFIG`` is dropped because these commands are about an
        installation and not about a project.
        """
        environment = {
            **os.environ,
            "UV_TOOL_DIR": str(self.directory),
            "UV_TOOL_BIN_DIR": str(self.bin_directory),
            "UV_CACHE_DIR": str(self.cache),
        }
        environment.pop("AGENTIC_HIL_CONFIG", None)
        for key, value in overrides.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return environment

    def install(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        installed = subprocess.run(
            [self.uv, "tool", "install", *arguments],
            capture_output=True,
            text=True,
            env=self.environment(**overrides),
            timeout=INSTALL_TIMEOUT_S,
            check=False,
        )
        assert installed.returncode == 0, f"uv tool install {arguments} failed:\n{installed.stdout}\n{installed.stderr}"
        return installed

    def run(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.launcher), *arguments],
            capture_output=True,
            text=True,
            env=self.environment(**overrides),
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )

    def upgrade(self, **overrides: str) -> tuple[int, dict]:
        """``agentic-hil upgrade --json``, with the document it printed."""
        answered = self.run("upgrade", "--json", **overrides)
        assert answered.stdout.strip(), f"upgrade printed no document (exit {answered.returncode}):\n{answered.stderr}"
        return answered.returncode, json.loads(answered.stdout)

    def recorded_install(self) -> dict:
        """What this installation's own code reads out of uv's receipt.

        Run through the tool environment's interpreter rather than imported
        here, because the question is what the installed copy sees when it looks
        at the receipt beside itself.
        """
        read = subprocess.run(
            [
                str(self.interpreter),
                "-c",
                "import json; from agentic_hil.upgrade import _recorded_install; print(json.dumps(_recorded_install()._asdict()))",
            ],
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
        assert read.returncode == 0, f"reading the receipt failed:\n{read.stdout}\n{read.stderr}"
        return json.loads(read.stdout)


@pytest.fixture
def uv_tool(tmp_path: Path, uv_binary: str, uv_cache: Path) -> UvTool:
    # `<temporary>/uv/tools`, which is uv's own layout under its data directory
    # and the layout `owning_manager` recognises. A tool directory shaped any
    # other way is read as a plain `uv pip` installation, whose receipt is never
    # consulted, so the pin and the recorded options below would not be seen.
    directory = tmp_path / "uv" / "tools"
    bin_directory = tmp_path / "uv-bin"
    directory.mkdir(parents=True)
    bin_directory.mkdir(parents=True)
    return UvTool(directory=directory, bin_directory=bin_directory, cache=uv_cache, uv=uv_binary)


def _yaml_scalar(value: object) -> str:
    """One configuration value the way an operator writes it: quoted text, a bare number, a lowercase boolean."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return repr(str(value))


def fixture_configuration(
    workspace: Path,
    config_path: Path,
    state_root: Path,
    *,
    executable: str | None = None,
    timeout_s: int = 20,
    com_port_device: str | None = None,
    com_port_fields: dict[str, object] | None = None,
    com_port_identity_source: str | None = "device",
) -> Path:
    """A configuration for a project with no hardware behind it.

    It names the OpenOCD this image installs and a probe id no probe answers to,
    grants nothing that could reach a board, and declares no serial port. It
    exists so a server can start for a project and be found in the process
    table, which is a question about processes and not about hardware.

    ``executable`` is the YAML value for ``debuggers.dut.executable`` verbatim,
    so a test can name a wrapper, the bare name ``openocd`` or ``null``; the
    default is the absolute path of the OpenOCD this image installs.
    ``timeout_s`` is the deadline that entry gives its process. ``com_port_device``
    adds one serial port, ``dut``, on that device, writable, because the one
    bridge in this project that carries bytes into a port has to have a port.
    The port declares ``identity_source: device``: version 3 refuses a port that
    is identified by its device name alone unless the operator has said so, and
    a pseudo-terminal has no other identity to offer. ``com_port_fields`` adds
    further keys to that entry as an operator would write them (an encoding, a
    buffer size, a serial number), and ``com_port_identity_source`` is the
    declaration to write, or None to write none, for an entry whose identity
    one of those added keys carries instead.
    """
    executable_value = repr(shutil.which("openocd")) if executable is None else executable
    entry_lines = "".join(f"    {key}: {_yaml_scalar(value)}\n" for key, value in (com_port_fields or {}).items())
    identity_line = "" if com_port_identity_source is None else f"    identity_source: {com_port_identity_source}\n"
    com_ports = (
        "com_ports: {}\n"
        if com_port_device is None
        else f"""com_ports:
  dut:
    device: {com_port_device!r}
    baudrate: 115200
{identity_line}{entry_lines}    permissions:
      allow_write: true
"""
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""workspace_root: {str(workspace)!r}
state_root: {str(state_root)!r}
version: 3
permissions:
  allow_config_write: false
  allow_config_description_write: false
  allow_config_permissions_write: false
  allow_recover: false
  allow_upgrade: false
target:
  name: "container-fixture"
  controller: "stm32f446ret6"
debuggers:
  dut:
    type: openocd
    executable: {executable_value}
    probe_id: "FIXTUREPROBE0001"
    interface_cfg: interface/stlink.cfg
    target_cfg: target/stm32f4x.cfg
    timeout_s: {timeout_s}
    permissions:
      allow_flash: false
      allow_reset: false
      allow_debug_execution: false
      allow_raw_debugger_commands: false
      allow_mass_erase: false
debug:
  gdb_executable: null
  allowed_symbols: []
  allow_all_symbols: true
  max_dump_size_bytes: 1048576
artifacts:
  allowed_roots: ["."]
  upload_directory: ".agentic-hil/artifacts"
  allowed_extensions: [".elf"]
  max_upload_size_mb: 1
  allow_upload: false
{com_ports}can_buses: {{}}
reports:
  directory: ".agentic-hil/reports"
logs:
  directory: ".agentic-hil/logs"
""",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# The serial transport: a pseudo-terminal pair, the scripted peer on its far
# end, a live server over its own pipe, and the console script.
#
# The pair is made by socat and opened through the real pyserial, so what these
# fixtures put in front of a test is the kernel's terminal device and pyserial's
# own POSIX behaviour on it: the exclusive flock a second opener meets, the
# termios the open applies, the read that returns nothing when the far end is
# quiet. None of that is reachable through the fake serial handle the unit tier
# uses, which is the reason this arrangement exists.
#
# The peer on the far end is `tests/container/pty_responder.py`: a byte peer
# for the transport and nothing more. It runs no firmware, models nothing
# electrical, and its answers are an argument the test wrote, documented beside
# the assertions that read them back.

# How long the pair may take to appear, and how long a peer may take to open
# its end. Generous: both are a process start away.
PTY_SETUP_TIMEOUT_S = 15.0
# How long a live server may take to answer one request before that is a
# failure rather than a slow machine. A `com_read` waits out its own
# `wait_timeout_s` inside this, so it has to exceed every wait a test asks for.
SERVER_ANSWER_TIMEOUT_S = 60.0

RESPONDER = Path(__file__).resolve().parent / "pty_responder.py"


@dataclass
class PtyPair:
    """Two linked pseudo-terminals, and the socat that joins them.

    ``dut`` is the link the configuration names as its COM port; ``peer`` is
    the link the responder holds. Both are symbolic links socat made to the
    ``/dev/pts/N`` slaves it allocated, so a configuration written against
    them is stable across runs and never names a pts number.
    """

    dut: Path
    peer: Path
    socat: subprocess.Popen[bytes]

    @property
    def dut_slave(self) -> Path:
        """The ``/dev/pts/N`` behind the configured link, as the kernel names it."""
        return Path(os.path.realpath(self.dut))

    def stop(self) -> None:
        """End socat and wait for it, which removes both slaves.

        Idempotent, so a test that killed the pair itself to make the device
        vanish can leave the fixture's teardown to find it already gone.
        """
        if self.socat.poll() is None:
            self.socat.kill()
        self.socat.wait(timeout=30)
        for stream in (self.socat.stdout, self.socat.stderr):
            if stream is not None:
                stream.close()


def _wait_until(condition: Callable[[], bool], timeout_s: float, what: str, process: subprocess.Popen[bytes] | None = None) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if process is not None and process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr is not None else ""
            raise RuntimeError(f"{what}: the process exited with {process.returncode} first:\n{stderr}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"{what}: not within {timeout_s:.0f}s")
        time.sleep(0.02)


@pytest.fixture
def pty_pair(tmp_path: Path) -> Iterator[PtyPair]:
    """A fresh pair per test, so one test's holder can never be another's.

    ``raw,echo=0`` on both ends is not optional: the two slaves share nothing,
    but a slave left in its default line discipline echoes what it receives
    back to its writer, and the product would then read its own stimulus as
    the peer's answer.

    Skips, with the reason named, where the pair cannot be made: no socat on
    PATH, or a devpts the container does not mount. A skip here reaches the
    job's own gate, which refuses a tier that skipped anything, so a container
    that could not create the transport is a red run and not a quiet pass.
    """
    socat = shutil.which("socat")
    if socat is None:
        pytest.skip("socat is not on PATH, and the pseudo-terminal pair this module drives is made by it")
    try:
        pair = make_pty_pair(socat, tmp_path / "dut", tmp_path / "peer")
    except RuntimeError as error:
        pytest.skip(f"the pseudo-terminal pair could not be created here: {error}")
    try:
        yield pair
    finally:
        pair.stop()


def make_pty_pair(socat: str, dut: Path, peer: Path) -> PtyPair:
    """One socat pair with its two links at the given paths, or a RuntimeError naming why not.

    Separate from the fixture so a test that made its device vanish by ending
    the pair can put a fresh one behind the same configured link and prove
    the product opens it again.
    """
    process = subprocess.Popen(
        [socat, "-d", "-d", f"pty,raw,echo=0,link={dut}", f"pty,raw,echo=0,link={peer}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    pair = PtyPair(dut=dut, peer=peer, socat=process)
    try:
        _wait_until(lambda: dut.exists() and peer.exists(), PTY_SETUP_TIMEOUT_S, "waiting for socat's two links", process)
    except RuntimeError:
        pair.stop()
        raise
    return pair


@dataclass
class Responder:
    """The scripted peer, running, and what it has received so far."""

    process: subprocess.Popen[bytes]
    record: Path

    def received(self) -> bytes:
        """Every byte the product put on the wire, as the peer saw it."""
        return self.record.read_bytes() if self.record.exists() else b""

    def wait_for(self, expected: bytes, timeout_s: float = 10.0) -> bytes:
        """The record once ``expected`` is in it, or whatever it holds at the bound."""
        deadline = time.monotonic() + timeout_s
        while expected not in self.received() and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.received()

    def stop(self) -> bytes:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.process.stderr is not None:
            self.process.stderr.close()
        return self.received()


def start_responder(pair: PtyPair, tmp_path: Path, *replies: str, delay_s: float = 0.0, announce: str | None = None, announce_every_s: float = 0.0) -> Responder:
    """Put the peer on the far end of ``pair`` with the given answer table.

    Each reply is ``REQUEST=RESPONSE`` under Python's escape rules, so
    ``PING=PONG\\r\\n`` answers the line ``PING`` with the bytes ``PONG\\r\\n``.
    No replies at all is a peer that listens and records and never answers.
    ``announce`` is a line the peer writes on its own every ``announce_every_s``
    without being asked, which is how a test reaches a plan format that has no
    write step. Returns once the peer has opened its end, so a test that
    writes next is writing to a listener.
    """
    record = tmp_path / "responder-received.bin"
    ready = tmp_path / "responder-ready"
    arguments = [sys.executable, str(RESPONDER), "--device", str(pair.peer), "--record", str(record), "--ready", str(ready), "--delay-s", str(delay_s)]
    for reply in replies:
        arguments += ["--reply", reply]
    if announce is not None:
        arguments += ["--announce", announce, "--announce-every-s", str(announce_every_s)]
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _wait_until(ready.exists, PTY_SETUP_TIMEOUT_S, "waiting for the responder to open its end of the pair", process)
    return Responder(process=process, record=record)


class LiveServer:
    """One ``agentic-hil mcp-stdio`` over its own pipes, spoken to as a host does.

    Started by absolute interpreter in the project directory with the
    configuration named through ``AGENTIC_HIL_CONFIG``, exactly the way an
    agent host starts one. ``call`` is a ``tools/call`` and returns the
    ``structuredContent`` document, which is the same document the server puts
    in the content text and what a caller reads.
    """

    def __init__(self, config: Path, project: Path, *, command: list[str] | None = None, environment: dict[str, str] | None = None):
        self.project = project
        env = {**os.environ, "AGENTIC_HIL_CONFIG": str(config), **(environment or {})}
        self.process = subprocess.Popen(
            command or [sys.executable, "-m", "agentic_hil", "mcp-stdio"],
            cwd=str(project),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._next_id = 1

    def __enter__(self) -> LiveServer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(self, method: str, params: dict | None = None, timeout_s: float = SERVER_ANSWER_TIMEOUT_S) -> dict:
        assert self.process.stdin is not None and self.process.stdout is not None
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            line = a_line_within(self.process.stdout, remaining)
            if line is None or line == "":
                state = f"exited with {self.process.returncode}" if self.process.poll() is not None else "is still running"
                raise AssertionError(f"the server did not answer {method} (id {request_id}) within {timeout_s:.0f}s and {state}")
            answered = json.loads(line)
            if answered.get("id") == request_id:
                return answered
            if time.monotonic() > deadline:
                raise AssertionError(f"the server answered other messages but never id {request_id} for {method}")

    def initialize(self) -> dict:
        answered = self.request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "container-tier", "version": "1"}},
        )
        assert "result" in answered, answered
        return answered["result"]

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = SERVER_ANSWER_TIMEOUT_S) -> dict:
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s=timeout_s)
        assert "result" in answered, answered
        result = answered["result"]
        document = result["structuredContent"]
        # The content text is the same document, which a host that reads no
        # structuredContent parses; held to it here so the two cannot drift.
        assert json.loads(result["content"][0]["text"]) == document, result
        return document

    def close(self, timeout_s: float = 30.0) -> str:
        """End the server by closing its stdin, and return what it wrote to stderr."""
        if self.process.poll() is None:
            try:
                _stdout, stderr = self.process.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _stdout, stderr = self.process.communicate(timeout=timeout_s)
        else:
            stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        return stderr or ""

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30)


def run_cli(project: Path, config: Path | None, *arguments: str, stdin: bytes | None = None, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """The console script, in the project, with the configuration named.

    Bytes in and bytes out, so what a test asserts on is what a shell saw.
    ``config`` None leaves ``AGENTIC_HIL_CONFIG`` as the environment has it,
    for the commands that read no configuration.
    """
    env = {**os.environ, **(environment or {})}
    if config is not None:
        env["AGENTIC_HIL_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-m", "agentic_hil", *arguments],
        cwd=str(project),
        env=env,
        input=stdin,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def json_document(completed: subprocess.CompletedProcess[bytes]) -> dict:
    """The one JSON document a ``--json`` run printed."""
    text = completed.stdout.decode("utf-8")
    assert text.strip(), f"no document on stdout (exit {completed.returncode}):\n{completed.stderr.decode('utf-8', errors='replace')}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# An unprivileged user for the cases the kernel's permission checks decide.
#
# The container runs its tests as root, and root passes every mode bit, so a
# device this user cannot open, a log this user cannot append to and a logs
# directory this user cannot write into are only reachable by a server that is
# not root. `setpriv` drops one child to `nobody`; the tree that child works in
# is made under /tmp and handed to that user, because pytest's own temporary
# tree is created with mode 0o700 and cannot even be traversed by anyone else.


@dataclass(frozen=True)
class UnprivilegedUser:
    """The `nobody` uid and gid, and the `setpriv` that starts a process as them."""

    uid: int
    gid: int
    setpriv: str

    def command(self, *argv: str) -> list[str]:
        return [self.setpriv, f"--reuid={self.uid}", f"--regid={self.gid}", "--clear-groups", *argv]


def unprivileged_user() -> UnprivilegedUser:
    """The user a server is dropped to, or a skip naming what this run lacks for it."""
    if os.geteuid() != 0:
        pytest.skip("needs root: a device or a file the server's user cannot open is something only root can arrange here")
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        pytest.skip("setpriv is not on PATH, and it is what drops the server to an unprivileged user")
    import pwd

    try:
        entry = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("this image has no `nobody` user to drop the server to")
    return UnprivilegedUser(uid=entry.pw_uid, gid=entry.pw_gid, setpriv=setpriv)


@dataclass
class UnprivilegedTree:
    """A workspace, configuration, state root and HOME an unprivileged server can use.

    Everything under ``root`` is handed to the user by ``give_away``, which a
    test calls once the configuration is written; what a test then wants
    root-owned it makes afterwards.
    """

    root: Path
    user: UnprivilegedUser

    @property
    def project(self) -> Path:
        return self.root / "project"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def config_path(self) -> Path:
        return self.root / "config" / "config.yaml"

    def give_away(self) -> None:
        for path in [self.root, *self.root.rglob("*")]:
            os.chown(path, self.user.uid, self.user.gid)

    def environment(self) -> dict[str, str]:
        """HOME and the XDG roots under the tree, and temporary storage the user can write."""
        temporary = self.root / "tmp"
        return {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
        }

    def server(self) -> LiveServer:
        """A live server in the project, running as the unprivileged user."""
        return LiveServer(
            self.config_path,
            self.project,
            command=self.user.command(sys.executable, "-m", "agentic_hil", "mcp-stdio"),
            environment=self.environment(),
        )

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def unprivileged_tree(user: UnprivilegedUser, com_port_device: str) -> UnprivilegedTree:
    """A fresh tree under /tmp with the configuration written and everything handed to ``user``."""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="agentic-hil-unprivileged-", dir="/tmp"))
    tree = UnprivilegedTree(root=root, user=user)
    tree.project.mkdir()
    tree.home.mkdir()
    (root / "tmp").mkdir()
    fixture_configuration(tree.project, tree.config_path, tree.state, com_port_device=com_port_device)
    tree.give_away()
    return tree
