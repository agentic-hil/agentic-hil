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

So this tier runs the real tools. It needs uv, an OpenOCD binary, ``/proc`` and
the package index, and it needs no probe and no board: what needs those is the
bench tier under ``tests/bench``. ``tools/container/Dockerfile`` is the image it
is published with and the image the hosted CI job builds.

The gate has two halves, because a skip and an error are different answers.
``AGENTIC_HIL_CONTAINER_TESTS=1`` says a run means to be in the image, and every
test here skips without it, so a developer's ``pytest`` and the hosted matrix
are unaffected. A run that does set it and cannot find the marker the image
build writes, or uv, or the debugger, or ``/proc``, ends the collection with an
error naming what is missing. A required check that reported success over
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
from collections.abc import Callable
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


def fixture_configuration(
    workspace: Path,
    config_path: Path,
    state_root: Path,
    *,
    executable: str | None = None,
    timeout_s: int = 20,
    com_port_device: str | None = None,
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
    a pseudo-terminal has no other identity to offer.
    """
    executable_value = repr(shutil.which("openocd")) if executable is None else executable
    com_ports = (
        "com_ports: {}\n"
        if com_port_device is None
        else f"""com_ports:
  dut:
    device: {com_port_device!r}
    baudrate: 115200
    identity_source: device
    permissions:
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
