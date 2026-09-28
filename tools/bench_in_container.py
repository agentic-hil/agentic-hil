"""Run the bench tier in its own image, against the board attached to this machine.

The bench tier, tests/bench, is the part of the suite that needs an in-circuit
debugger or programmer and the board behind it. Run directly on the machine the
board is attached to, it measured whatever that machine carried: its OpenOCD,
its cross compiler, its GDB, its Python. This runs it in the image
tools/bench/Dockerfile builds from one commit instead, and the machine provides
only the board, the probe and the right to open them.

A run, in order:

* checks `--expected-commit` against the selected checkout before any runtime
  or hardware discovery, then finds the probe and refuses what it cannot run
  against;
* takes this machine's run lock, and stops a container an earlier run of this
  tool left behind;
* builds the image from the selected source checkout's committed tree, never
  its working tree: `git archive` of HEAD, staged apart from the checkout, with
  tools/bench/Dockerfile.dockerignore as the context's ignore file. By default
  the source is this tool's checkout; `--source` selects another checkout and
  `--expected-commit` can bind it to a full SHA before any hardware check or
  build. Uncommitted changes are named, and are not what runs;
* runs the tier in a container that gets the probe's device nodes, the
  machine's device locks and a directory of the run's own for its report, and
  nothing else of the machine: no network, no capabilities, no host process
  table;
* copies the report alone out of that directory, as a regular file and without
  following a link, into the output directory, which the container never sees;
* reads pytest's summary line as the verdict.

The container runtime. Rootless Podman is preferred, Docker is accepted, and
`--runtime` chooses. Under rootless Podman the container's root is the invoking
user, so the tier opens the probe with exactly that user's rights, through the
host's own nodes: `--group-add keep-groups` keeps the supplementary groups a
serial port or a USB node is usually opened through, and it is implemented by
crun and not by runc, so a run names crun. Docker's container root is the
host's root, so there the tier runs as the invoking user (`--user`), with each
group a node is opened through and that the user already belongs to. Docker
recreates a device node in the container with its owner, group and mode and
without its ACL, so a node this user can open only through an ACL is refused
here rather than failing in the container. Rootless Docker and Docker Desktop
put the container in a user namespace or a virtual machine of their own, where
the nodes are not what they are on the host; neither is supported for a run,
and `--build-only` works with both.

The stage without the device group. `--without-device-group` hands the
container the same nodes and withholds every group they are opened through: no
`keep-groups` under Podman, no `--group-add` under Docker. The tier then meets
the nodes' modes the way an account that never joined those groups does, which
is where a newcomer on Linux starts, and runs
tests/bench/test_bench_without_device_group.py alone, which holds the product
to naming that refusal for what it is. The machine is checked as for a full
run: a user who cannot open the probe even through those groups is refused,
because the refusal the stage measures would then be the machine's.

Device passthrough. The probe is found through sysfs by its public USB identity,
the vendor and product ids the product itself recognises an in-circuit debugger
or programmer by, and never by a serial number: idVendor and idProduct under
/sys/bus/usb/devices, busnum and devnum for its node under /dev/bus/usb, and the
tty directory of the interface that carries its serial port for the port.
Exactly one probe with a serial port is run against. None is refused, and so
are two, because choosing between them would be choosing a board;
`--usb-device` and `--serial-device` name the nodes instead where discovery
should not decide. The port's links under /dev/serial/by-id come along, read
only and rebuilt for the ports handed in and no others, because the product
names a port by that link where it can, and a port named differently in the
container can be a different device lock from the one the host's runs take on
it. This tool never opens a device: it reads sysfs, looks at the nodes and hands
their paths to the runtime.

Serialisation. Two runs must never drive the board at once. The backstop is the
device locks the product takes under ~/.agentic-hil/device-locks: the machine's
lock directory is mounted at the same place under the container's home, and a
flock on a file in a bind mount is one lock on the host and in every container,
whatever their pid namespaces. The container keeps a pid namespace of its own
and gets a host name of its own, so a holder record written inside it names
that container, and a check that compares the host before the pid cannot take
it for a process on the host. `--pid=host` was the other way to share the
locks, and it buys nothing here: a device lock is the flock, never a judgement
on whether its holder's pid is alive, and `--pid=host` would hand the tier
every process of this user to see and to signal. Ahead of the device locks, a
run takes the machine's run lock (run_lock.py beside this file, the one
ci_linux.py and loop_in_container.py take), so runs of these tools, by hand or
from the gate, queue behind one another instead of meeting as device_busy
halfway through the tier. The gate's workflow shares its concurrency group with
the scheduled bench workflow, which queues the two on GitHub's side; anything
else that drives the board meets the device locks alone. A run as root is
refused, because root's home holds device locks no run of the invoking user
takes.

The tier's own redirections. The tier moves the product's configuration and
state roots into its temporary directory and keeps HOME, because the device
locks are under it. In the container HOME is /bench-home, where the image has
nothing but the place the machine's lock directory is mounted.

What is withheld. The gate's log and its artifact can be read by anyone who can
read the repository, and the tier prints the probe's serial number and paths of
this machine. Every line this tool prints or logs, and the JUnit report, has
the probe's serial numbers, this machine's host name, its home directory and
its user name replaced with [withheld], line by line. No line is removed.

The verdict is pytest's summary line or nothing. A run without one, without the
marker the image writes, with a skip, or with nothing passed is not green,
whatever its exit status: the tier's own setup fails rather than skips when the
board is missing, so a skip is a test that did not reach the board.

Interrupting a run stops the container with SIGINT, the image's stop signal,
which the image hands to pytest's whole process group as Ctrl-C in a terminal
would: the fixture that puts the demo back on the board runs, and this waits
for it. SIGTERM, which is what a runner's cancel sends second, is taken as the
same interrupt. A container that cannot be confirmed removed keeps the
machine's run lock held and marked for cleanup, so the next run does not start
beside it.

Usage, from a checkout on the machine the board is attached to:

    python3 tools/bench_in_container.py                      # the whole tier
    python3 tools/bench_in_container.py -- tests/bench/test_bench_serial.py -x
    python3 tools/bench_in_container.py --runtime docker
    python3 tools/bench_in_container.py --source ../candidate --expected-commit <full-sha>
    python3 tools/bench_in_container.py --without-device-group
    python3 tools/bench_in_container.py --build-only         # the image alone, anywhere

Everything after `--` goes to pytest and replaces the default selection,
`tests/bench -v`. The JUnit report and the log land in `--output`,
bench-results by default. What the machine has to provide once is in
tools/bench/README.md.

Exit status: pytest's own when it reported; 2 when this machine cannot run the
tier; 3 when the image did not build; 4 when there is no result; 5 when
`--no-wait` met a held machine; 6 when no probe could be handed in; 7 when a
container was left behind; 130 when interrupted.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

# A standalone script run from checkouts that need not be installed, like the
# two tools beside it, so their modules are reached by naming this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_linux import repository_root, result_summary, tests_accounted_for  # noqa: E402
from run_lock import RunLock, RunLockBusy  # noqa: E402

TOOL_NAME = "tools/bench_in_container.py"
# What a run queued behind this one is told. The tier flashes, debugs and reads
# the board for minutes, and a build with a cold cache adds several more; the
# range is an estimate until runs on the board have measured it.
RUNS_FOR = "10 to 30 minutes"

EXIT_CANNOT_RUN_HERE = 2
EXIT_BUILD_FAILED = 3
EXIT_NO_RESULT = 4
EXIT_LOCKED = 5
EXIT_NO_PROBE = 6
EXIT_LEFTOVER = 7
EXIT_INTERRUPTED = 130

RUNTIMES = ("podman", "docker")
IMAGE = "agentic-hil-bench-tier"
# The label tools/bench/Dockerfile puts on the image. Earlier builds lose the
# tag to the newest and are pruned by it; nothing else carries it.
IMAGE_LABEL = "agentic-hil.image=bench-tier"
# Every container of a run carries this, valued with the invoking user's uid,
# which is how the next run of the same user finds one that was left behind.
RUN_LABEL = "agentic-hil.bench-run"
CONTAINER_PREFIX = "agentic-hil-bench-"

CONTAINER_HOME = "/bench-home"
CONTAINER_LOCKS = f"{CONTAINER_HOME}/.agentic-hil/device-locks"
CONTAINER_SERIAL_BY_ID = "/dev/serial/by-id"
RESULTS = "/results"
REPORT_NAME = "bench-junit.xml"
LOG_NAME = "bench-tier.log"
# The most this reads of a report out of the tier's directory. The whole tier's
# JUnit report is a few hundred kilobytes; this is a ceiling on what a directory
# the tier writes can make this process hold, not an estimate.
REPORT_LIMIT_BYTES = 64 * 1024 * 1024

# Written into the image by tools/bench/Dockerfile and by nothing else, and
# printed before pytest starts. A run whose output lacks it did not run in the
# image this commit built, whatever its summary line says.
MARKER = "/etc/agentic-hil/bench-test-image"
MARKER_TEXT = "The bench tier runs here. Written by tools/bench/Dockerfile."

DEFAULT_PYTEST_ARGS = ("tests/bench", "-v")
# Always passed, ahead of the selection: no cache written into the image's
# tree, the JUnit report in the tier's own directory, and the reason for every
# skip in the summary, because a skip is what fails this tier.
FIXED_PYTEST_ARGS = ("-p", "no:cacheprovider", f"--junitxml={RESULTS}/{REPORT_NAME}", "-ra")
# `"$@"` rather than the arguments pasted into the text, so each stays one
# argument; `exec`, so the stop signal tini forwards reaches pytest itself.
CONTAINER_SCRIPT = f'cat {MARKER} && exec python -m pytest "$@"'
# `sh -c` takes the word after the script as `$0`, not `$1`.
SCRIPT_ARGV0 = "bench_in_container"
# The environment of the tier. HOME is where the device locks are mounted, and
# AGENTIC_HIL_BENCH is the statement that a probe and a board are attached,
# which only a run that hands the devices in can make.
ENVIRONMENT = {
    "HOME": CONTAINER_HOME,
    "AGENTIC_HIL_BENCH": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
# What a run that withholds the groups the probe is opened through tells the
# tier, whose conftest then runs the one stage about that and nothing else.
# Copied in tests/bench/conftest.py, which does not import this script; a test
# keeps the two copies one name and one value.
DEVICE_GROUPS_ENV = "AGENTIC_HIL_BENCH_DEVICE_GROUPS"
DEVICE_GROUPS_WITHHELD = "withheld"

# The USB identity of the in-circuit debuggers or programmers the product
# recognises, copied from `agentic_hil.comports` because this script runs from
# a checkout that need not be installed; a test keeps the two copies one list.
PROBE_VENDOR_ID = 0x0483
PROBE_PRODUCT_IDS = frozenset({0x3744, 0x3748, 0x374B, 0x374D, 0x374E, 0x374F, 0x3752, 0x3753, 0x3754})
SYSFS = Path("/sys")
SERIAL_BY_ID = Path("/dev/serial/by-id")
TTY_NAME = re.compile(r"^tty[A-Za-z0-9_]+$")

WITHHELD = "[withheld]"
# A value shorter than this, or one of these words, names nothing a reader
# could find a machine by, and rewriting it would garble ordinary output.
SHORTEST_WITHHELD = 4
TOO_COMMON = frozenset(
    {"root", "user", "admin", "runner", "bench", "test", "tests", "work", "localhost", "linux", "ubuntu", "debian"}
)

# How long the tier gets to tear down after the stop signal before the runtime
# kills it. Putting the demo back on the board is a build that is already done
# and a flash, which takes seconds; a minute is the margin for a slow probe.
STOP_TIMEOUT_S = 60
STOP_COMMAND_TIMEOUT_S = STOP_TIMEOUT_S + 30
CLIENT_GRACE_S = 30
READER_GRACE_S = 30
REMOVAL_POLLS = 15
COMMAND_TIMEOUT_S = 120
BUILD_PRUNE_TIMEOUT_S = 300


class Refused(RuntimeError):
    """A run this tool will not start, with the status it leaves on."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


class LeftBehind(RuntimeError):
    """A container this run could not confirm removed, so the machine stays held."""

    def __init__(self, runtime: str, leftovers: list[tuple[str, str]]) -> None:
        self.names = [name for name, _state in leftovers]
        described = "; ".join(f"{name} ({state})" for name, state in leftovers)
        removal = " && ".join(f"{runtime} rm --force {name}" for name in self.names)
        self.detail = (
            f"{TOOL_NAME} exited with a container it could not confirm removed, which may still hold the board: "
            f"{described}. Remove it with `{removal}`."
        )
        super().__init__(self.detail)


# Seams: what this tool asks of the machine, each in one place, so a test can
# answer for a machine it is not running on.


def this_is_linux() -> bool:
    return sys.platform.startswith("linux")


def effective_uid() -> int:
    return os.geteuid()


def user_ids() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def user_groups() -> set[int]:
    return {*os.getgroups(), os.getgid()}


def device_status(path: str) -> os.stat_result:
    return os.stat(path)


def host_can_open(path: str) -> bool:
    return os.access(path, os.R_OK | os.W_OK)


def canonical_node(path: str) -> str:
    return os.path.realpath(path)


def pause(seconds: float) -> None:
    time.sleep(seconds)


def host_identities() -> list[str]:
    """What names this machine in a line of output: host, home and user."""
    names: list[str] = []
    with suppress(OSError):
        host = socket.gethostname()
        names += [host, host.split(".")[0]]
    home = os.path.expanduser("~")
    names += [home, Path(home).as_posix()]
    with suppress(Exception):
        names.append(getpass.getuser())
    return names


def current_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise Refused(EXIT_BUILD_FAILED, f"the commit to build could not be read in {root}: {last_line(result.stderr)}")
    return result.stdout.strip()


def uncommitted_changes(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def stage_committed_tree(root: Path, commit: str, destination: Path) -> None:
    """The tree of `commit`, and nothing of the checkout it came from, in `destination`.

    `git archive` rather than a copy of the working tree, so what is built is
    what was committed; extracted with the data filter where this Python has
    it, which refuses a member that would land outside the destination.
    """
    destination.mkdir(parents=True)
    archive = destination.with_name(f"{destination.name}.tar")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", f"--output={archive}", commit],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise Refused(EXIT_BUILD_FAILED, f"git archive of {commit[:12]} failed: {last_line(result.stderr)}")
        with tarfile.open(archive) as tree:
            if hasattr(tarfile, "data_filter"):
                tree.extractall(destination, filter="data")
            else:  # pragma: no cover - a Python from before the extraction filters
                tree.extractall(destination)
    finally:
        with suppress(FileNotFoundError):
            archive.unlink()


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "(nothing)"


# What is withheld.


class Redactor:
    """Replace whole values that name this machine or its probe, case-insensitively.

    A value counts only where it stands on its own: a letter or a digit on
    either side makes it part of a longer word, which is left alone. Longer
    values first, so a home directory goes as a whole rather than around the
    user name inside it.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        kept = {
            value
            for value in (str(raw).strip() for raw in values)
            if len(value) >= SHORTEST_WITHHELD and value.lower() not in TOO_COMMON
        }
        self.values = tuple(sorted(kept, key=lambda value: (-len(value), value)))
        alternatives = "|".join(f"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])" for value in self.values)
        self.pattern = re.compile(alternatives, re.IGNORECASE) if alternatives else None

    def adding(self, values: Iterable[str]) -> Redactor:
        return Redactor([*self.values, *values])

    def __call__(self, text: str) -> str:
        if self.pattern is None or not text:
            return text
        return self.pattern.sub(WITHHELD, text)


class Voice:
    """This tool's own lines, on stderr and withheld like everything else."""

    def __init__(self, redact: Redactor) -> None:
        self.redact = redact

    def withhold(self, values: Iterable[str]) -> None:
        self.redact = self.redact.adding(values)

    def __call__(self, text: str) -> None:
        print(f"bench_in_container: {self.redact(text)}", file=sys.stderr, flush=True)


# Finding the probe.


@dataclass(frozen=True)
class Probe:
    """One in-circuit debugger or programmer as sysfs shows it."""

    where: str
    usb_nodes: tuple[str, ...]
    serial_ports: tuple[str, ...]
    serial_numbers: tuple[str, ...]

    @property
    def nodes(self) -> tuple[str, ...]:
        return (*self.usb_nodes, *self.serial_ports)


def read_attribute(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_number(path: Path, base: int) -> int | None:
    text = read_attribute(path)
    if not text:
        return None
    try:
        return int(text, base)
    except ValueError:
        return None


def serial_ports_of(device: Path) -> list[str]:
    """The ttys of a USB device's interfaces: `<interface>/tty/<name>`, a CDC ACM port.

    Only the device's own subdirectories are looked in. Its links lead to the
    driver, the subsystem and the hub port, and following them would find
    other devices' ports.
    """
    ports: list[str] = []
    try:
        children = sorted(device.iterdir(), key=lambda child: child.name)
    except OSError:
        return ports
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            names = sorted(entry.name for entry in (child / "tty").iterdir())
        except OSError:
            continue
        ports += [f"/dev/{name}" for name in names if TTY_NAME.match(name)]
    return ports


def discover_probes(sysfs: Path | None = None) -> list[Probe]:
    """Every attached in-circuit debugger or programmer the product recognises."""
    devices = (SYSFS if sysfs is None else sysfs) / "bus" / "usb" / "devices"
    try:
        entries = sorted(devices.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    probes: list[Probe] = []
    for device in entries:
        if read_number(device / "idVendor", 16) != PROBE_VENDOR_ID:
            continue
        if read_number(device / "idProduct", 16) not in PROBE_PRODUCT_IDS:
            continue
        bus = read_number(device / "busnum", 10)
        number = read_number(device / "devnum", 10)
        if bus is None or number is None:
            continue
        serial = read_attribute(device / "serial")
        probes.append(
            Probe(
                where=f"bus {bus} device {number} (sysfs {device.name})",
                usb_nodes=(f"/dev/bus/usb/{bus:03d}/{number:03d}",),
                serial_ports=tuple(serial_ports_of(device)),
                serial_numbers=(serial,) if serial else (),
            )
        )
    return probes


def serial_number_behind(rdev: int) -> str | None:
    """The serial of the USB device a named node belongs to, found through /sys/dev/char."""
    if not rdev or not hasattr(os, "major"):
        return None
    link = SYSFS / "dev" / "char" / f"{os.major(rdev)}:{os.minor(rdev)}"
    try:
        device = link.resolve(strict=True)
        top = SYSFS.resolve(strict=True)
    except OSError:
        return None
    for candidate in (device, *device.parents):
        if candidate == top or top not in candidate.parents:
            break
        if (candidate / "idVendor").is_file():
            return read_attribute(candidate / "serial")
    return None


def checked_node(path: str) -> str:
    """The node's own path, once it is known to be a character device."""
    node = canonical_node(path)
    try:
        status = device_status(node)
    except FileNotFoundError:
        raise Refused(EXIT_NO_PROBE, f"{path}: no such device node on this machine") from None
    except OSError as error:
        raise Refused(EXIT_NO_PROBE, f"{path} could not be looked at: {error}") from None
    if not stat.S_ISCHR(status.st_mode):
        raise Refused(EXIT_NO_PROBE, f"{path} is not a character device, and only a device node is handed to the tier")
    return node


@dataclass(frozen=True)
class Devices:
    """What the container gets of the probe, and the serials it is withheld by."""

    usb_nodes: tuple[str, ...]
    serial_ports: tuple[str, ...]
    serial_numbers: tuple[str, ...]

    @property
    def nodes(self) -> tuple[str, ...]:
        return (*self.usb_nodes, *self.serial_ports)


def the_devices(usb_devices: list[str], serial_devices: list[str]) -> Devices:
    """The one probe this run hands in, named or found."""
    if usb_devices or serial_devices:
        if not (usb_devices and serial_devices):
            raise Refused(
                EXIT_NO_PROBE,
                "--usb-device and --serial-device are given together or not at all: the tier needs the probe's USB "
                "node for the debugger and its serial port for the board's output",
            )
        usb = tuple(checked_node(path) for path in usb_devices)
        ports = tuple(checked_node(path) for path in serial_devices)
        serials = [serial_number_behind(device_status(node).st_rdev) for node in (*usb, *ports)]
        return Devices(usb, ports, tuple(dict.fromkeys(serial for serial in serials if serial)))
    probes = discover_probes()
    if not probes:
        raise Refused(
            EXIT_NO_PROBE,
            "no in-circuit debugger or programmer the product recognises is attached to this machine's USB. If one is "
            "attached and was not found, name its nodes with --usb-device and --serial-device",
        )
    if len(probes) > 1:
        raise Refused(
            EXIT_NO_PROBE,
            f"{len(probes)} in-circuit debuggers or programmers are attached ({'; '.join(probe.where for probe in probes)}), "
            "and choosing between them would be choosing a board. Name the one to run against with --usb-device "
            "and --serial-device",
        )
    probe = probes[0]
    if not probe.serial_ports:
        raise Refused(
            EXIT_NO_PROBE,
            f"the in-circuit debugger or programmer at {probe.where} shows no serial port, and the tier reads the "
            "board's output over one. Name the port with --serial-device, together with --usb-device",
        )
    usb = tuple(checked_node(node) for node in probe.usb_nodes)
    ports = tuple(checked_node(node) for node in probe.serial_ports)
    return Devices(usb, ports, probe.serial_numbers)


def docker_opens(status: os.stat_result, uid: int, gid: int, groups: set[int]) -> bool:
    """Whether the node's owner, group and mode alone let this user read and write it.

    The rights Docker carries into the container: the node is recreated with
    those three and without an ACL, and the tier runs with this user's uid, its
    primary group and the node groups it belongs to.
    """
    mode = status.st_mode
    if status.st_uid == uid:
        bits = (mode >> 6) & 0o7
    elif status.st_gid == gid or (status.st_gid != 0 and status.st_gid in groups):
        bits = (mode >> 3) & 0o7
    else:
        bits = mode & 0o7
    return bits & 0o6 == 0o6


def docker_groups(devices: Devices) -> list[int]:
    """The node groups the tier is given under Docker: those this user already has."""
    _uid, gid = user_ids()
    groups = user_groups()
    wanted = {device_status(node).st_gid for node in devices.nodes}
    return sorted(group for group in wanted if group not in (0, gid) and group in groups)


def check_access(runtime: str, devices: Devices) -> None:
    if runtime == "podman":
        for node in devices.nodes:
            if not host_can_open(node):
                raise Refused(
                    EXIT_NO_PROBE,
                    f"this user cannot open {node} for reading and writing, and under rootless Podman the tier opens it "
                    "with exactly this user's rights. tools/bench/README.md says what the machine provides once; a "
                    "group joined after this session started reaches it only from a new session",
                )
        return
    uid, gid = user_ids()
    groups = user_groups()
    for node in devices.nodes:
        status = device_status(node)
        if not docker_opens(status, uid, gid, groups):
            raise Refused(
                EXIT_NO_PROBE,
                f"{node} belongs to user {status.st_uid} and group {status.st_gid} with mode "
                f"{stat.S_IMODE(status.st_mode):04o}, and none of that lets this user read and write it. Docker "
                "recreates the node in the container with that owner, group and mode and without an ACL, so a right "
                "this user holds through an ACL does not reach the tier. Give the node a group this user belongs to "
                "(tools/bench/README.md), or run under rootless Podman, which keeps the host's rights",
            )


# This machine.


def pick_runtime(named: str | None) -> str:
    if named is not None:
        if shutil.which(named) is None:
            raise Refused(EXIT_CANNOT_RUN_HERE, f"{named} was not found on PATH")
        return named
    for candidate in RUNTIMES:
        if shutil.which(candidate) is not None:
            return candidate
    raise Refused(
        EXIT_CANNOT_RUN_HERE,
        "neither podman nor docker was found on PATH. The bench tier runs in a container: rootless Podman is what "
        "tools/bench/README.md sets up, and Docker is accepted",
    )


def check_this_machine(runtime: str) -> None:
    if not this_is_linux():
        raise Refused(
            EXIT_CANNOT_RUN_HERE,
            "the bench tier runs on Linux, on the machine the board is attached to: that is where its device nodes "
            "are, and a runtime anywhere else runs the container in a virtual machine that has none of them. "
            "--build-only builds the image anywhere",
        )
    if effective_uid() == 0:
        raise Refused(
            EXIT_CANNOT_RUN_HERE,
            "this runs as root. The tier takes its device locks under the home of whoever runs it, and root's are not "
            "the ones the board's own user takes, so a run as root could drive a board another run is holding. Run "
            "it as the user the board's rights were given to",
        )
    if runtime == "podman" and shutil.which("crun") is None:
        raise Refused(
            EXIT_CANNOT_RUN_HERE,
            "podman is installed and crun is not. --group-add keep-groups, which keeps the groups this user opens "
            "the probe through, is implemented by crun and not by runc. Install crun (tools/bench/README.md), or "
            "pass --runtime docker",
        )


def device_lock_directory() -> Path:
    """The machine's device-lock directory, created as the product would create it.

    A link anywhere in it is refused: the product walks this directory without
    following links and refuses one on the host, and mounting a link's target
    would give the container locks the host's runs never take.
    """
    root = Path(os.path.expanduser("~")) / ".agentic-hil"
    locks = root / "device-locks"
    for directory in (root, locks):
        try:
            with suppress(FileExistsError):
                directory.mkdir(mode=0o700)
            status = os.lstat(directory)
        except OSError as error:
            raise Refused(EXIT_CANNOT_RUN_HERE, f"the device-lock directory {directory} could not be prepared: {error}") from None
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise Refused(
                EXIT_CANNOT_RUN_HERE,
                f"{directory} is not a plain directory. The product refuses a link there, and mounting what it points "
                "at would give the tier device locks no run on this machine takes",
            )
    return locks


def prepare_output(option: str) -> Path:
    """The output directory, with no report or log of an earlier run left in it."""
    output = Path(option).resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
        for name in (REPORT_NAME, LOG_NAME):
            with suppress(FileNotFoundError):
                (output / name).unlink()
    except OSError as error:
        raise Refused(EXIT_CANNOT_RUN_HERE, f"the output directory {output} could not be prepared: {error}") from None
    return output


def stage_stable_names(serial_ports: tuple[str, ...], destination: Path) -> Path | None:
    """The /dev/serial/by-id links of the ports handed in, rebuilt apart from the host's.

    Each points at the node's own path, which is where the runtime puts the node
    in the container. Links to any other device are left out, and no name is
    printed: a name carries the probe's serial number.
    """
    try:
        entries = sorted(SERIAL_BY_ID.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return None
    wanted = set(serial_ports)
    links = {entry.name: target for entry in entries if (target := os.path.realpath(entry)) in wanted}
    if not links:
        return None
    destination.mkdir(mode=0o700)
    for name, target in links.items():
        os.symlink(target, destination / name)
    return destination


# The runtime.


def runtime_run(command: list[str], timeout: float = COMMAND_TIMEOUT_S):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def container_state(runtime: str, name: str) -> str | None:
    """None once the container is gone; otherwise what the runtime says it is."""
    try:
        result = runtime_run([runtime, "container", "inspect", "--format", "{{.State.Status}}", name])
    except (OSError, subprocess.SubprocessError) as error:
        return f"unknown: {error}"
    if result.returncode == 0:
        return result.stdout.strip() or "present"
    answer = f"{result.stdout}\n{result.stderr}"
    if "no such" in answer.lower() or "not found" in answer.lower():
        return None
    return f"unknown: {last_line(answer)}"


def stop_and_remove(runtime: str, name: str) -> None:
    """Stop with the image's stop signal, then remove. Never raises."""
    for command, timeout in (
        ([runtime, "stop", "--time", str(STOP_TIMEOUT_S), name], STOP_COMMAND_TIMEOUT_S),
        ([runtime, "rm", "--force", name], COMMAND_TIMEOUT_S),
    ):
        with suppress(OSError, subprocess.SubprocessError):
            runtime_run(command, timeout)


def confirm_removed(runtime: str, name: str) -> str | None:
    """None once the run's container is gone, after removing it if `--rm` did not."""
    for _ in range(REMOVAL_POLLS):
        if container_state(runtime, name) is None:
            return None
        pause(1)
    stop_and_remove(runtime, name)
    return container_state(runtime, name)


def sweep_leftovers(runtime: str, uid: int, voice: Voice) -> None:
    """Stop and remove any container an earlier run of this user left behind.

    A leftover that is still running is given the stop timeout to finish on its
    own first: it may be an interrupted run putting the demo back, and a second
    stop signal would cut that short.
    """
    try:
        listing = runtime_run([runtime, "ps", "--all", "--filter", f"label={RUN_LABEL}={uid}", "--format", "{{.Names}}"])
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused(EXIT_CANNOT_RUN_HERE, f"`{runtime} ps` could not be run: {error}") from None
    if listing.returncode != 0:
        raise Refused(
            EXIT_CANNOT_RUN_HERE,
            f"`{runtime} ps` failed, so this run cannot tell whether an earlier one is still on the board: "
            f"{last_line(listing.stderr or listing.stdout)}",
        )
    unresolved: list[tuple[str, str]] = []
    for name in listing.stdout.split():
        state = container_state(runtime, name)
        if state in ("running", "stopping"):
            voice(f"{name}, left by an earlier run, is still {state}; giving it {STOP_TIMEOUT_S}s to finish first")
            for _ in range(STOP_TIMEOUT_S):
                pause(1)
                state = container_state(runtime, name)
                if state not in ("running", "stopping"):
                    break
        voice(f"stopping and removing {name}, left by an earlier run, before anything else starts")
        stop_and_remove(runtime, name)
        state = container_state(runtime, name)
        if state is not None:
            unresolved.append((name, state))
    if unresolved:
        raise LeftBehind(runtime, unresolved)


def build_image(runtime: str, root: Path, commit: str, workdir: Path, voice: Voice) -> str:
    """Build the image from the committed tree; the id the build wrote."""
    context = workdir / "context"
    try:
        stage_committed_tree(root, commit, context)
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise Refused(EXIT_BUILD_FAILED, f"the tree of {commit[:12]} could not be staged: {error}") from None
    ignore = context / "tools" / "bench" / "Dockerfile.dockerignore"
    if not ignore.is_file():
        raise Refused(EXIT_BUILD_FAILED, f"commit {commit[:12]} carries no tools/bench/Dockerfile.dockerignore to build with")
    for name in (".dockerignore", ".containerignore"):
        shutil.copyfile(ignore, context / name)
    image_id_file = workdir / "image-id"
    command = [
        runtime,
        "build",
        "--file",
        str(context / "tools" / "bench" / "Dockerfile"),
        "--tag",
        IMAGE,
        "--label",
        f"org.opencontainers.image.revision={commit}",
        "--iidfile",
        str(image_id_file),
        str(context),
    ]
    voice(f"building the bench tier's image from commit {commit[:12]} with {runtime}")
    environment = {**os.environ, "DOCKER_BUILDKIT": "1"} if runtime == "docker" else None
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )
    last = ""
    try:
        for line in process.stdout:
            text = voice.redact(line.rstrip("\n"))
            print(text, flush=True)
            if text.strip():
                last = text.strip()
        returncode = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=CLIENT_GRACE_S)
        raise
    if returncode != 0:
        raise Refused(EXIT_BUILD_FAILED, f"the image did not build: {runtime} build exited {returncode}, and its last line was: {last}")
    try:
        image_id = image_id_file.read_text(encoding="utf-8").strip()
    except OSError:
        image_id = ""
    if not image_id:
        raise Refused(EXIT_BUILD_FAILED, f"{runtime} build succeeded and wrote no image id, so there is no image to run")
    return image_id


def prune_images(runtime: str, voice: Voice) -> None:
    """Remove earlier builds of the tier's image, which the newest one untagged."""
    try:
        result = runtime_run([runtime, "image", "prune", "--force", "--filter", f"label={IMAGE_LABEL}"], BUILD_PRUNE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as error:
        voice(f"earlier images of the tier were not pruned: {error}")
        return
    if result.returncode != 0:
        voice(f"earlier images of the tier were not pruned: {last_line(result.stderr or result.stdout)}")


def tier_command(
    runtime: str,
    image_id: str,
    name: str,
    devices: Devices,
    locks: Path,
    results: Path,
    stable_names: Path | None,
    pytest_args: list[str],
    source_commit: str,
    run_id: str,
    *,
    withhold_groups: bool = False,
) -> list[str]:
    uid, gid = user_ids()
    command = [runtime]
    if runtime == "podman":
        command += ["--runtime", "crun"]
    command += [
        "run",
        "--rm",
        "--name",
        name,
        "--hostname",
        name,
        "--label",
        f"{RUN_LABEL}={uid}",
        "--sig-proxy=false",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ]
    if runtime == "podman":
        if not withhold_groups:
            command += ["--group-add", "keep-groups"]
        command += ["--security-opt", "label=disable"]
    else:
        command += ["--user", f"{uid}:{gid}"]
        if not withhold_groups:
            for group in docker_groups(devices):
                command += ["--group-add", str(group)]
    for node in devices.nodes:
        command += ["--device", node]
    command += ["-v", f"{locks}:{CONTAINER_LOCKS}", "-v", f"{results}:{RESULTS}"]
    if stable_names is not None:
        command += ["-v", f"{stable_names}:{CONTAINER_SERIAL_BY_ID}:ro"]
    for key, value in ENVIRONMENT.items():
        command += ["-e", f"{key}={value}"]
    command += ["-e", f"AGENTIC_HIL_BENCH_COMMIT={source_commit}", "-e", f"AGENTIC_HIL_BENCH_RUN_ID={run_id}"]
    if withhold_groups:
        command += ["-e", f"{DEVICE_GROUPS_ENV}={DEVICE_GROUPS_WITHHELD}"]
    return [*command, image_id, "sh", "-c", CONTAINER_SCRIPT, SCRIPT_ARGV0, *FIXED_PYTEST_ARGS, *pytest_args]


def relay(stream: Iterable[str], captured: list[str], log: TextIO, redact: Redactor) -> None:
    """Print and log the tier's output as it arrives, withheld, and keep it as it was.

    Runs in its own thread so the pipe keeps draining while the main thread
    waits for the container or stops it, and it never stops draining: a line
    that cannot be printed or logged is dropped from there, not from the pipe.
    """
    logging = True
    for line in stream:
        captured.append(line)
        text = redact(line.rstrip("\n"))
        with suppress(OSError, ValueError):
            sys.stdout.write(f"{text}\n")
            sys.stdout.flush()
        if logging:
            try:
                log.write(f"{text}\n")
                log.flush()
            except (OSError, ValueError):
                logging = False


class Signals:
    """SIGTERM as an interrupt while a run is on the machine, and neither once it winds down."""

    def __init__(self) -> None:
        self.saved: dict[int, object] = {}
        if threading.current_thread() is not threading.main_thread():
            return
        for number in (signal.SIGINT, signal.SIGTERM):
            self.saved[number] = signal.getsignal(number)
        signal.signal(signal.SIGTERM, _interrupted)

    def hold_off(self) -> None:
        for number in self.saved:
            signal.signal(number, signal.SIG_IGN)

    def restore(self) -> None:
        for number, handler in self.saved.items():
            if handler is not None:
                signal.signal(number, handler)


def _interrupted(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def run_tier(runtime: str, name: str, command: list[str], log_path: Path, redact: Redactor, voice: Voice, signals: Signals):
    """Run the container; its exit status, its output as printed, and whether it was interrupted."""
    captured: list[str] = []
    interrupted = False
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Its own session, so an operator's Ctrl-C reaches this process and
            # not the client: the container is stopped once, with the signal
            # the tier tears down on, and never by a second one.
            start_new_session=True,
        )
        reader = threading.Thread(
            target=relay, args=(process.stdout, captured, log, redact), name="bench-tier-output", daemon=True
        )
        try:
            reader.start()
            returncode = process.wait()
        except KeyboardInterrupt:
            interrupted = True
            signals.hold_off()
            if reader.ident is None:
                # Interrupted before the output was being drained: drained it
                # must be, or the tier's teardown blocks on a full pipe.
                with suppress(RuntimeError):
                    reader.start()
            voice(
                f"interrupted: stopping {name} with SIGINT, which the tier tears down on, and waiting up to "
                f"{STOP_TIMEOUT_S}s for it to put the demo back on the board"
            )
            with suppress(OSError, subprocess.SubprocessError):
                runtime_run([runtime, "stop", "--time", str(STOP_TIMEOUT_S), name], STOP_COMMAND_TIMEOUT_S)
            try:
                returncode = process.wait(timeout=CLIENT_GRACE_S)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
        if reader.ident is not None:
            reader.join(timeout=READER_GRACE_S)
    return returncode, "".join(captured), interrupted


def read_what_the_tier_wrote(path: Path) -> bytes:
    """A regular file out of the tier's directory, read as the tier left it.

    Opened without following a link, so a link there never has this process read
    a file of the machine, and without waiting for a writer, so a pipe there
    never holds the run. Anything but a regular file, or one larger than any
    report, raises.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, 1 << 20):
            chunks.append(chunk)
            size += len(chunk)
            if size > REPORT_LIMIT_BYTES:
                raise OSError(f"{path.name} is larger than the {REPORT_LIMIT_BYTES} bytes a report is read up to")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def collect_report(source: Path, destination: Path, redact: Redactor, voice: Voice) -> None:
    """Copy the tier's JUnit report out of its directory, withheld like everything else.

    The destination is in the output directory, which the container never sees,
    and is written by this process alone, so what a gate uploads from there is
    what this wrote. Nothing else in the tier's directory is read.
    """
    try:
        text = read_what_the_tier_wrote(source).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError as error:
        voice(f"the tier's JUnit report was not copied out: {error}")
        return
    try:
        destination.write_text(redact(text), encoding="utf-8")
    except OSError as error:
        voice(f"the tier's JUnit report was not copied out: {error}")
        with suppress(OSError):
            destination.unlink()


def remove_tree(path: Path) -> None:
    """Remove the run's staging, whatever modes the tier left on its own directory.

    A directory the tier took every permission from would otherwise stay behind
    in the machine's temporary directory. Links are never followed: a link's
    mode is not changed and its target is not removed.
    """
    for directory, subdirectories, _ in os.walk(path):
        for name in subdirectories:
            entry = os.path.join(directory, name)
            if not os.path.islink(entry):
                with suppress(OSError):
                    os.chmod(entry, 0o700)
    shutil.rmtree(path, ignore_errors=True)


# The verdict.


def summary_count(summary: str, outcome: str) -> int:
    return sum(int(count) for count in re.findall(rf"(\d+)\s+{outcome}\b", summary))


def collecting(pytest_args: list[str]) -> bool:
    return any(argument in ("--collect-only", "--co", "--collectonly") for argument in pytest_args)


def judge(returncode: int, output: str, pytest_args: list[str]) -> tuple[int, str]:
    """This tool's exit status for a finished run of the tier, and the verdict line."""
    if MARKER_TEXT not in output:
        return (
            returncode or EXIT_NO_RESULT,
            f"no result (exit {returncode}): the run never printed the marker tools/bench/Dockerfile writes, so it did "
            f"not run in the image this commit built. Its last line: {last_line(output)}",
        )
    summary = result_summary(output)
    if summary is None:
        return (
            returncode or EXIT_NO_RESULT,
            f"no result (exit {returncode}): pytest printed no summary line, so nothing is known to have run on the "
            f"board. Its last line: {last_line(output)}",
        )
    if returncode != 0:
        return returncode, f"failed (exit {returncode}): {summary}"
    if collecting(pytest_args):
        if tests_accounted_for(summary) > 0:
            return 0, f"collected: {summary}"
        return EXIT_NO_RESULT, f"no result: nothing was collected: {summary}"
    if summary_count(summary, "skipped") > 0:
        return (
            EXIT_NO_RESULT,
            f"no result: {summary}. A skip in the bench tier is a test that did not reach the board, so a run with "
            "one is not green; the reasons are in the short test summary above",
        )
    if summary_count(summary, "passed") == 0:
        return EXIT_NO_RESULT, f"no result: nothing passed: {summary}"
    return 0, f"passed: {summary}"


def parse_options(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench_in_container.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # Everything unrecognised belongs to pytest, and prefix matching would
        # take one of its options for one of these.
        allow_abbrev=False,
    )
    parser.add_argument("--runtime", choices=RUNTIMES, default=None, help="Container runtime (default: podman, else docker).")
    parser.add_argument(
        "--usb-device",
        action="append",
        default=[],
        metavar="NODE",
        help="The probe's USB node, instead of finding it. Repeatable; needs --serial-device.",
    )
    parser.add_argument(
        "--serial-device",
        action="append",
        default=[],
        metavar="NODE",
        help="The probe's serial port, instead of finding it. Repeatable; needs --usb-device.",
    )
    parser.add_argument("--output", default="bench-results", help="Where the JUnit report and the log go (default bench-results).")
    parser.add_argument(
        "--source",
        default=None,
        metavar="CHECKOUT",
        help="Git checkout whose committed tree builds the image (default: this tool's checkout).",
    )
    parser.add_argument(
        "--expected-commit",
        default=None,
        metavar="SHA",
        help="Require the selected source checkout's HEAD to equal this full commit SHA before doing any work.",
    )
    parser.add_argument("--no-wait", action="store_true", help="Refuse instead of queueing when another run holds this machine.")
    parser.add_argument("--build-only", action="store_true", help="Build the image and stop; needs no probe and runs anywhere.")
    parser.add_argument(
        "--without-device-group",
        action="store_true",
        help="Withhold the groups the probe's nodes are opened through, and run the stage about that alone.",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="After --: handed to pytest, replacing tests/bench -v.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for handle in (sys.stdout, sys.stderr):
        if hasattr(handle, "reconfigure"):
            handle.reconfigure(encoding="utf-8", errors="replace")
    options = parse_options(argv)
    voice = Voice(Redactor(host_identities()))
    # `--` separates these options from pytest's, and only the leading one is
    # this tool's: pytest has a `--` of its own.
    forwarded = list(options.pytest_args)
    if forwarded and forwarded[0] == "--":
        forwarded.pop(0)
    pytest_args = forwarded or list(DEFAULT_PYTEST_ARGS)
    running = not options.build_only

    # Every refusal that costs nothing comes before the queue: a run that cannot
    # start here should not wait an hour to say so.
    try:
        root = Path(options.source).resolve() if options.source is not None else repository_root()
        commit = current_commit(root)
        if options.expected_commit is not None:
            expected = options.expected_commit.lower()
            if not re.fullmatch(r"[0-9a-f]{40}", expected):
                raise Refused(EXIT_BUILD_FAILED, "the expected commit must be a full 40-character hexadecimal SHA")
            if commit.lower() != expected:
                raise Refused(
                    EXIT_BUILD_FAILED,
                    f"source checkout HEAD is {commit}, not the expected commit {expected}; nothing was built or run",
                )
        runtime = pick_runtime(options.runtime)
        if running:
            check_this_machine(runtime)
            devices = the_devices(options.usb_device, options.serial_device)
            voice.withhold(devices.serial_numbers)
            check_access(runtime, devices)
            locks = device_lock_directory()
    except Refused as refusal:
        voice(str(refusal))
        return refusal.status
    if uncommitted_changes(root).strip():
        voice(f"this checkout has uncommitted changes, and they are not what runs: the image is built from commit {commit[:12]}")

    # The window the lock covers is the window this run has something on the
    # runtime or the board: from before the build to after the container is gone.
    lock = RunLock(
        tool=TOOL_NAME,
        runs_for=RUNS_FOR,
        root=root,
        details={"image": IMAGE, "commit": commit, "runtime": runtime, "pytest_args": pytest_args if running else []},
        announce=voice,
    )
    try:
        lock.acquire(wait=not options.no_wait)
    except RunLockBusy as busy:
        voice(f"{busy}. Refusing, because --no-wait was given")
        return EXIT_LOCKED
    except OSError as error:
        voice(f"the lock at {lock.path} could not be taken: {error}")
        return EXIT_LOCKED
    except KeyboardInterrupt:
        voice("interrupted while waiting for the machine, so nothing ran")
        return EXIT_INTERRUPTED

    signals = Signals()
    workdir = Path(tempfile.mkdtemp(prefix=CONTAINER_PREFIX))
    left_behind: LeftBehind | None = None
    name: str | None = None
    try:
        if running:
            output = prepare_output(options.output)
            sweep_leftovers(runtime, user_ids()[0], voice)
        image_id = build_image(runtime, root, commit, workdir, voice)
        prune_images(runtime, voice)
        if not running:
            voice(f"built {image_id} from commit {commit[:12]} with {runtime}; --build-only, so nothing ran")
            return 0

        # Again, now: the wait for the machine can be long, and a probe that
        # re-enumerated during it has new nodes.
        devices = the_devices(options.usb_device, options.serial_device)
        voice.withhold(devices.serial_numbers)
        check_access(runtime, devices)
        stable_names = stage_stable_names(devices.serial_ports, workdir / "by-id")
        # The one directory the tier writes to, and it is the run's own: the
        # output directory, which a gate uploads, is never mounted.
        results = workdir / "results"
        results.mkdir(mode=0o700)
        name = f"{CONTAINER_PREFIX}{secrets.token_hex(4)}"
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        command = tier_command(
            runtime, image_id, name, devices, locks, results, stable_names, pytest_args,
            commit, f"{run_id}-{run_attempt}", withhold_groups=options.without_device_group,
        )
        voice(f"$ {shlex.join(command)}")
        returncode, printed, interrupted = run_tier(runtime, name, command, output / LOG_NAME, voice.redact, voice, signals)
        # What follows decides what is published and whether the machine is
        # handed on, and a second interrupt must not cut it short.
        signals.hold_off()
        state = confirm_removed(runtime, name)
        collect_report(results / REPORT_NAME, output / REPORT_NAME, voice.redact, voice)
        if interrupted:
            status, verdict = EXIT_INTERRUPTED, "interrupted: the run was stopped before the tier finished, so there is no verdict"
        else:
            status, verdict = judge(returncode, printed, pytest_args)
        with suppress(OSError), open(output / LOG_NAME, "a", encoding="utf-8") as log:
            log.write(f"{voice.redact(f'verdict: {verdict}')}\n")
        voice(f"verdict: {verdict}")
        if state is not None:
            raise LeftBehind(runtime, [(name, state)])
        return status
    except Refused as refusal:
        voice(str(refusal))
        return refusal.status
    except LeftBehind as left:
        left_behind = left
    except KeyboardInterrupt:
        signals.hold_off()
        if name is None:
            voice("interrupted before the tier started, so nothing ran on the board")
            return EXIT_INTERRUPTED
        # The tier's own handling in run_tier did not see this one: it came while
        # the container was starting or winding down, or on top of a first
        # interrupt. Either way the container may still be there.
        voice(f"interrupted while {name} was starting or winding down; making sure it is gone")
        state = confirm_removed(runtime, name)
        if state is None:
            voice("interrupted: the run was stopped before the tier finished, so there is no verdict")
            return EXIT_INTERRUPTED
        left_behind = LeftBehind(runtime, [(name, state)])
    finally:
        signals.restore()
        remove_tree(workdir)
        if left_behind is None:
            lock.release()
        else:
            lock.retain_for_cleanup(left_behind.detail)
    voice(
        f"{left_behind.detail} The machine's run lock at {lock.path} stays held and marked for cleanup, so no run starts "
        "beside it; remove the lock file once the container is gone"
    )
    return EXIT_LEFTOVER


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
