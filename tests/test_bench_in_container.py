"""What `tools/bench_in_container.py` hands the container, and what it makes of the answer.

The bench tier is the part of the suite that needs an in-circuit debugger or
programmer and the board behind it, and this tool is how that tier runs in the
image tools/bench/Dockerfile builds from one commit. Nothing here needs a
container runtime, a probe or a board. The runtime is a fake that records every
command and answers from a script, the sysfs the probe is found through is a
directory tree laid out the way the kernel documents it, and the device nodes are
stat results made up for the test. That is also the limit of this file: whether a
real probe is found and opened through that layout is proven by a run on the
machine the board is attached to, not here.

Four things are held:

* the command lines, for rootless Podman and for Docker, and what each hands the
  container: the probe's nodes, the machine's device locks and a directory for
  the report, and never the host's process table or a privileged container;
* finding the probe, and every refusal on the way to it: no runtime, no probe,
  two probes, a node this user cannot open, a machine that is not Linux, root;
* the verdict, which is pytest's summary line or nothing: a run with no summary,
  without the image's marker, with a skip or with nothing passed is not green;
* the queue and the teardown: the machine's run lock held for the whole run, a
  leftover container stopped before a new one starts, an interrupt that lets the
  tier put the demo back first, and a container that outlives the run keeping
  the machine held.

The last section holds the image to what the runner relies on, and the page
the runner's refusals and the gate send people to, to what they send them
there for.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bench_in_container  # noqa: E402
import run_lock  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPOSITORY_ROOT / "tools" / "bench" / "Dockerfile"
IGNORE_FILE = REPOSITORY_ROOT / "tools" / "bench" / "Dockerfile.dockerignore"
README = REPOSITORY_ROOT / "tools" / "bench" / "README.md"

# A probe serial, a host name, a home and a user that belong to no machine. What
# they stand for is exactly what must never reach a log somebody else can read.
PROBE_SERIAL = "PROBESERIAL0001"
SECOND_SERIAL = "PROBESERIAL0002"
HOST_NAME = "workstation-7"
HOME = "/srv/mhuber"
USER = "mhuber"

COMMIT = "0123456789abcdef0123456789abcdef01234567"
IMAGE_ID = "sha256:" + "ab" * 32
UID, GID = 1000, 1000
# The groups a distribution hands the serial ports and the USB nodes to. Their
# numbers are made up; what matters is that this user belongs to both.
SERIAL_GROUP, USB_GROUP = 20, 46

USB_NODE = "/dev/bus/usb/001/005"
TTY_NODE = "/dev/ttyACM0"

MARKED = f"{bench_in_container.MARKER_TEXT}\n"
PASSING_TIER = (
    MARKED
    + "tests/bench/test_bench_flash_reset.py::test_the_demo_is_flashed PASSED\n"
    + "======================= 114 passed in 243.10s (0:04:03) =======================\n"
)


@pytest.fixture(autouse=True)
def a_machine_of_this_tests_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test in this file may reach the machine's real run lock or device locks.

    Both live under the home directory, so pointing it at `tmp_path` gives every
    test a machine of its own. The two variables are the two `expanduser` reads,
    Windows first.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


def a_usb_device(
    sysfs: Path,
    name: str,
    *,
    vendor: str = "0483",
    product: str = "374b",
    bus: int = 1,
    device: int = 5,
    serial: str | None = PROBE_SERIAL,
    ttys: tuple[str, ...] = ("ttyACM0",),
) -> Path:
    """One USB device the way /sys/bus/usb/devices shows it.

    The attribute files, and the `tty/<name>` directory under the interface that
    carries the port, are the kernel's layout for a CDC ACM serial port, which is
    what the probes on these boards present. The kernel names an interface
    directory `<device>:<configuration>.<interface>`; the colon is an underscore
    here because Windows does not allow it in a file name, and nothing in the
    runner reads an interface by its name.
    """
    directory = sysfs / "bus" / "usb" / "devices" / name
    directory.mkdir(parents=True)
    for attribute, value in (("idVendor", vendor), ("idProduct", product), ("busnum", str(bus)), ("devnum", str(device))):
        (directory / attribute).write_text(f"{value}\n", encoding="utf-8")
    if serial is not None:
        (directory / "serial").write_text(f"{serial}\n", encoding="utf-8")
    (directory / "power").mkdir()
    interface = directory / f"{name}_1.2"
    interface.mkdir()
    (interface / "bInterfaceNumber").write_text("02\n", encoding="utf-8")
    for tty in ttys:
        (interface / "tty" / tty).mkdir(parents=True)
    return directory


def a_node(*, mode: int = 0o660, uid: int = 0, gid: int = SERIAL_GROUP, rdev: int = 0) -> SimpleNamespace:
    """A character device's stat result, which is all the runner asks of a node."""
    return SimpleNamespace(st_mode=stat.S_IFCHR | mode, st_uid=uid, st_gid=gid, st_rdev=rdev)


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProcess:
    """A runtime command started with Popen: its output, then its exit status.

    `interrupted` makes the first wait raise KeyboardInterrupt, which is where an
    operator's Ctrl-C, or the SIGTERM the runner turns into one, reaches a run
    that is waiting for its container.
    """

    def __init__(self, output: str = "", returncode: int = 0, *, interrupted: bool = False) -> None:
        self.stdout = iter(output.splitlines(keepends=True))
        self._returncode = returncode
        self._interrupted = interrupted
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._interrupted:
            self._interrupted = False
            raise KeyboardInterrupt
        return self._returncode

    def kill(self) -> None:
        self.killed = True

    terminate = kill


class Runtime:
    """The container runtime, faked: every command recorded, every answer scripted."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.tier_output = PASSING_TIER
        self.tier_returncode = 0
        self.build_output = "STEP 1/14: FROM docker.io/library/python@sha256:...\nCOMMIT agentic-hil-bench-tier\n"
        self.build_returncode = 0
        self.leftovers: tuple[str, ...] = ()
        self.never_removed = False
        self.interrupted = False
        self.during_run: Callable[[list[str]], None] | None = None
        self.context: dict[str, str] = {}

    @staticmethod
    def verb(command: list[str]) -> str:
        rest = command[1:]
        if rest[:1] == ["--runtime"]:
            rest = rest[2:]
        return rest[0] if rest else ""

    def issued(self, verb: str) -> list[list[str]]:
        return [command for command in self.commands if self.verb(command) == verb]

    @property
    def tier(self) -> list[str]:
        runs = self.issued("run")
        assert len(runs) == 1, runs
        return runs[0]

    def run(self, command: list[str], **kwargs: object) -> Completed:
        self.commands.append(list(command))
        verb = self.verb(command)
        if verb == "ps":
            return Completed(0, "".join(f"{name}\n" for name in self.leftovers))
        if verb == "container" and "inspect" in command:
            if self.never_removed:
                return Completed(0, "running\n")
            return Completed(125, "", f"Error: no such container {command[-1]}\n")
        return Completed(0)

    def popen(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.commands.append(list(command))
        verb = self.verb(command)
        if verb == "build":
            context = Path(command[-1])
            for name in (".dockerignore", ".containerignore"):
                path = context / name
                self.context[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
            if self.build_returncode == 0:
                Path(command[command.index("--iidfile") + 1]).write_text(f"{IMAGE_ID}\n", encoding="utf-8")
            return FakeProcess(self.build_output, self.build_returncode)
        if verb == "run":
            if self.during_run is not None:
                self.during_run(command)
            return FakeProcess(self.tier_output, self.tier_returncode, interrupted=self.interrupted)
        return FakeProcess()


def stage(root: Path, commit: str, destination: Path) -> None:
    """The committed tree, reduced to what the runner itself reads out of it."""
    bench = destination / "tools" / "bench"
    bench.mkdir(parents=True)
    shutil.copyfile(IGNORE_FILE, bench / "Dockerfile.dockerignore")
    (bench / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, a_machine_of_this_tests_own: Path) -> SimpleNamespace:
    """A Linux machine with one probe on it, as the runner sees one."""
    sysfs = tmp_path / "sys"
    a_usb_device(sysfs, "1-1")
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    statuses = {USB_NODE: a_node(mode=0o664, gid=USB_GROUP), TTY_NODE: a_node(gid=SERIAL_GROUP)}
    openable = {USB_NODE, TTY_NODE}
    installed = {"podman", "crun", "docker"}
    runtime = Runtime()

    def status_of(path: str) -> SimpleNamespace:
        if path not in statuses:
            raise FileNotFoundError(2, "No such file or directory", path)
        return statuses[path]

    monkeypatch.setattr(bench_in_container, "SYSFS", sysfs)
    monkeypatch.setattr(bench_in_container, "SERIAL_BY_ID", by_id)
    monkeypatch.setattr(bench_in_container, "this_is_linux", lambda: True)
    monkeypatch.setattr(bench_in_container, "effective_uid", lambda: UID)
    monkeypatch.setattr(bench_in_container, "user_ids", lambda: (UID, GID))
    monkeypatch.setattr(bench_in_container, "user_groups", lambda: {GID, SERIAL_GROUP, USB_GROUP})
    monkeypatch.setattr(bench_in_container, "device_status", status_of)
    monkeypatch.setattr(bench_in_container, "canonical_node", lambda path: path)
    monkeypatch.setattr(bench_in_container, "host_can_open", lambda path: path in openable)
    monkeypatch.setattr(bench_in_container, "host_identities", lambda: [HOST_NAME, HOME, USER])
    monkeypatch.setattr(bench_in_container, "repository_root", lambda: tmp_path / "checkout")
    monkeypatch.setattr(bench_in_container, "current_commit", lambda root: COMMIT)
    monkeypatch.setattr(bench_in_container, "uncommitted_changes", lambda root: "")
    monkeypatch.setattr(bench_in_container, "stage_committed_tree", stage)
    monkeypatch.setattr(bench_in_container, "pause", lambda seconds: None)
    # The modules the tool reaches through, replaced on the tool rather than on
    # the real `subprocess` and `shutil`, so nothing else in this process runs
    # against a patched standard library.
    monkeypatch.setattr(
        bench_in_container,
        "shutil",
        SimpleNamespace(
            which=lambda name: f"/usr/bin/{name}" if name in installed else None,
            rmtree=shutil.rmtree,
            copyfile=shutil.copyfile,
        ),
    )
    monkeypatch.setattr(
        bench_in_container,
        "subprocess",
        SimpleNamespace(
            run=runtime.run,
            Popen=runtime.popen,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    return SimpleNamespace(
        sysfs=sysfs,
        by_id=by_id,
        statuses=statuses,
        openable=openable,
        installed=installed,
        runtime=runtime,
        home=a_machine_of_this_tests_own,
        output=tmp_path / "results",
    )


def run(machine: SimpleNamespace, *argv: str) -> int:
    return bench_in_container.main(["--output", str(machine.output), *argv])


def option_values(command: list[str], option: str) -> list[str]:
    """Every value the command passes with `option`, in order."""
    return [value for flag, value in zip(command, command[1:], strict=False) if flag == option]


def pytest_arguments(command: list[str]) -> list[str]:
    """Everything after the script and its `$0`, which is what `"$@"` expands to."""
    index = command.index(bench_in_container.CONTAINER_SCRIPT)
    assert command[index + 1] == bench_in_container.SCRIPT_ARGV0
    return command[index + 2 :]


def mounted_at(command: list[str], target: str) -> str:
    """The host side of the one volume mounted at `target`."""
    found = [value for value in option_values(command, "-v") if value.split(":")[-1] == target or value.endswith(f":{target}:ro")]
    assert len(found) == 1, option_values(command, "-v")
    return found[0][: found[0].rindex(f":{target}")]


# The command lines.


def test_podman_runs_the_tier_rootless_with_this_users_own_groups(machine: SimpleNamespace) -> None:
    """Rootless Podman: container root is the invoking user, and `keep-groups`
    keeps the supplementary groups that user opens the probe with. crun is named
    because runc does not implement `keep-groups`."""
    assert run(machine) == 0

    command = machine.runtime.tier
    assert command[:4] == ["podman", "--runtime", "crun", "run"]
    assert option_values(command, "--group-add") == ["keep-groups"]
    assert "label=disable" in option_values(command, "--security-opt")
    assert "--user" not in command
    assert option_values(command, "--device") == [USB_NODE, TTY_NODE]


def test_docker_runs_the_tier_as_the_invoking_user_with_the_nodes_groups(machine: SimpleNamespace) -> None:
    """Docker's container root is the host's root, so the tier runs as the
    invoking user instead, with each group a node is opened through and that the
    user already belongs to. Never more than the user has on the host."""
    assert run(machine, "--runtime", "docker") == 0

    command = machine.runtime.tier
    assert command[:2] == ["docker", "run"]
    assert option_values(command, "--user") == [f"{UID}:{GID}"]
    assert option_values(command, "--group-add") == [str(SERIAL_GROUP), str(USB_GROUP)]
    assert "keep-groups" not in command
    assert option_values(command, "--device") == [USB_NODE, TTY_NODE]


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_the_container_gets_the_board_and_nothing_of_the_machine_besides(machine: SimpleNamespace, runtime: str) -> None:
    """No host process table, no privileged mode, no network, no capabilities.

    `--pid=host` was the other way to share the device locks, and it would hand
    the tier every process of this user to see and to signal. The locks are
    shared through the one directory they live in instead.
    """
    assert run(machine, "--runtime", runtime) == 0

    command = machine.runtime.tier
    assert not [part for part in command if part.startswith("--pid") or part.startswith("--privileged")]
    assert "--init" not in command
    assert option_values(command, "--network") == ["none"]
    assert option_values(command, "--cap-drop") == ["ALL"]
    assert "no-new-privileges" in option_values(command, "--security-opt")
    assert "--rm" in command
    assert "--sig-proxy=false" in command
    assert option_values(command, "--hostname") == option_values(command, "--name")
    assert option_values(command, "--label") == [f"{bench_in_container.RUN_LABEL}={UID}"]


def test_the_tier_finds_the_machines_device_locks_under_its_own_home(machine: SimpleNamespace) -> None:
    """The device locks are the backstop against every other run on this
    machine, and the tier keeps HOME to reach them. So the container's home is
    where the machine's lock directory is mounted, and only that directory."""
    assert run(machine) == 0

    command = machine.runtime.tier
    locks = machine.home / ".agentic-hil" / "device-locks"
    assert locks.is_dir()
    assert mounted_at(command, bench_in_container.CONTAINER_LOCKS) == str(locks)
    assert f"{bench_in_container.CONTAINER_HOME}/.agentic-hil/device-locks" == bench_in_container.CONTAINER_LOCKS
    environment = option_values(command, "-e")
    assert f"HOME={bench_in_container.CONTAINER_HOME}" in environment
    assert "AGENTIC_HIL_BENCH=1" in environment


def test_the_tier_writes_its_report_into_a_directory_of_the_runs_own(machine: SimpleNamespace) -> None:
    """Never into the output directory, which is what a gate uploads.

    Whatever the tier leaves in its own directory is the tier's, and the gate
    may be running a commit nobody has merged. The report alone is copied out,
    by this process, into an output directory the container never sees; the
    rest goes with the run's staging.
    """
    seen: list[Path] = []

    def write(command: list[str]) -> None:
        results = Path(mounted_at(command, bench_in_container.RESULTS))
        seen.append(results)
        (results / bench_in_container.REPORT_NAME).write_text("<testsuites/>", encoding="utf-8")
        (results / "left-by-the-tier.txt").write_text("not evidence\n", encoding="utf-8")

    machine.runtime.during_run = write

    assert run(machine) == 0

    output = machine.output.resolve()
    assert seen[0] != output and output not in seen[0].parents
    assert not seen[0].exists()
    assert sorted(entry.name for entry in output.iterdir()) == sorted([bench_in_container.REPORT_NAME, bench_in_container.LOG_NAME])
    assert (output / bench_in_container.REPORT_NAME).read_text(encoding="utf-8") == "<testsuites/>"
    assert f"--junitxml={bench_in_container.RESULTS}/{bench_in_container.REPORT_NAME}" in pytest_arguments(machine.runtime.tier)


@pytest.mark.parametrize("kind", ["link", "pipe", "directory"])
def test_what_the_tier_leaves_in_place_of_its_report_is_never_followed(
    machine: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture, kind: str
) -> None:
    """A link there would have this process read and rewrite a file of the
    machine and the upload publish it; a pipe would block the run forever. Only
    a regular file is copied out, and nothing it names is followed."""
    if kind in ("link", "pipe") and os.name == "nt":
        pytest.skip("links and pipes in the tier's directory are a POSIX layout")
    of_the_machine = tmp_path / "of-the-machine"
    of_the_machine.write_text(f"{HOST_NAME} {HOME}\n", encoding="utf-8")

    def plant(command: list[str]) -> None:
        report = Path(mounted_at(command, bench_in_container.RESULTS)) / bench_in_container.REPORT_NAME
        if kind == "link":
            report.symlink_to(of_the_machine)
        elif kind == "pipe":
            os.mkfifo(report)
        else:
            report.mkdir()

    machine.runtime.during_run = plant

    assert run(machine) == 0

    assert not (machine.output / bench_in_container.REPORT_NAME).exists()
    assert of_the_machine.read_text(encoding="utf-8") == f"{HOST_NAME} {HOME}\n"
    assert "report was not copied out" in capsys.readouterr().err


def test_a_report_larger_than_any_report_is_not_copied_out(
    machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(bench_in_container, "REPORT_LIMIT_BYTES", 16)

    def write(command: list[str]) -> None:
        (Path(mounted_at(command, bench_in_container.RESULTS)) / bench_in_container.REPORT_NAME).write_text("x" * 100, encoding="utf-8")

    machine.runtime.during_run = write

    assert run(machine) == 0

    assert not (machine.output / bench_in_container.REPORT_NAME).exists()
    assert "report was not copied out" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="the links under /dev/serial/by-id are symbolic links, a POSIX layout")
def test_the_serial_ports_stable_names_come_along_and_no_other_devices(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    """The product names a port by its /dev/serial/by-id link, so the link has to
    exist in the container. Only the links to the ports handed in are staged, and
    their names, which carry the probe's serial, are never printed."""
    ours = f"usb-Vendor_Probe_{PROBE_SERIAL}-if02"
    (machine.by_id / ours).symlink_to(TTY_NODE)
    (machine.by_id / "usb-Other_Adapter_OTHERSERIAL-if00-port0").symlink_to("/dev/ttyUSB7")
    staged: dict[str, str] = {}
    where: list[Path] = []

    def look(command: list[str]) -> None:
        directory = Path(mounted_at(command, "/dev/serial/by-id"))
        where.append(directory)
        staged.update({entry.name: os.readlink(entry) for entry in directory.iterdir()})

    machine.runtime.during_run = look

    assert run(machine) == 0

    assert staged == {ours: TTY_NODE}
    assert f"{where[0]}:/dev/serial/by-id:ro" in option_values(machine.runtime.tier, "-v")
    assert not where[0].exists()
    printed = capsys.readouterr()
    assert PROBE_SERIAL not in printed.out + printed.err


def test_no_stable_names_on_the_machine_means_no_mount_for_them(machine: SimpleNamespace) -> None:
    assert run(machine) == 0

    assert not [value for value in option_values(machine.runtime.tier, "-v") if "/dev/serial/by-id" in value]


def test_the_run_uses_the_image_this_build_wrote_and_not_a_tag(machine: SimpleNamespace) -> None:
    """A tag can be moved by another build between this one and the run; the id
    the build wrote cannot."""
    assert run(machine) == 0

    command = machine.runtime.tier
    assert command[command.index(bench_in_container.CONTAINER_SCRIPT) - 3 :][:3] == [IMAGE_ID, "sh", "-c"]


def test_the_image_is_built_from_the_committed_tree_with_the_tiers_own_ignore_file(machine: SimpleNamespace) -> None:
    """The working tree is not what is tested: the commit is, staged apart from
    the checkout, and its bench ignore file is what both runtimes read at the
    root of that copy."""
    assert run(machine) == 0

    builds = machine.runtime.issued("build")
    assert len(builds) == 1
    build = builds[0]
    context = Path(build[-1])
    assert option_values(build, "--file") == [str(context / "tools" / "bench" / "Dockerfile")]
    assert option_values(build, "--tag") == [bench_in_container.IMAGE]
    assert option_values(build, "--label") == [f"org.opencontainers.image.revision={COMMIT}"]
    committed = IGNORE_FILE.read_text(encoding="utf-8")
    assert machine.runtime.context == {".dockerignore": committed, ".containerignore": committed}
    assert not context.exists()


def test_earlier_images_of_the_tier_are_pruned_after_the_build(machine: SimpleNamespace) -> None:
    assert run(machine) == 0

    prunes = [command for command in machine.runtime.commands if command[1:3] == ["image", "prune"]]
    assert prunes == [["podman", "image", "prune", "--force", "--filter", f"label={bench_in_container.IMAGE_LABEL}"]]
    order = [Runtime.verb(command) for command in machine.runtime.commands]
    assert order.index("build") < order.index("image") < order.index("run")


def test_uncommitted_changes_are_named_and_the_commit_is_what_runs(
    machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(bench_in_container, "uncommitted_changes", lambda root: " M src/agentic_hil/bench.py\n")

    assert run(machine) == 0

    err = capsys.readouterr().err
    assert "uncommitted" in err
    assert COMMIT[:12] in err


# Arguments.


def test_no_arguments_runs_the_whole_tier(machine: SimpleNamespace) -> None:
    """pytest's own default is the whole suite, which is not this tier."""
    assert run(machine) == 0

    assert pytest_arguments(machine.runtime.tier) == [*bench_in_container.FIXED_PYTEST_ARGS, "tests/bench", "-v"]


def test_arguments_after_the_options_replace_the_default_selection(machine: SimpleNamespace) -> None:
    assert run(machine, "tests/bench/test_bench_serial.py", "-k", "echo and reset", "-x") == 0

    assert pytest_arguments(machine.runtime.tier) == [
        *bench_in_container.FIXED_PYTEST_ARGS,
        "tests/bench/test_bench_serial.py",
        "-k",
        "echo and reset",
        "-x",
    ]


def test_only_the_wrappers_own_separator_is_dropped(machine: SimpleNamespace) -> None:
    assert run(machine, "--", "tests/bench", "--", "-x") == 0

    assert pytest_arguments(machine.runtime.tier) == [*bench_in_container.FIXED_PYTEST_ARGS, "tests/bench", "--", "-x"]


def test_the_script_prints_the_marker_and_forwards_the_arguments_untouched() -> None:
    """`"$@"` rather than the arguments pasted into the text, so each stays one
    argument; and the marker first, so a run that is not in this image says so."""
    script = bench_in_container.CONTAINER_SCRIPT

    assert script.startswith(f"cat {bench_in_container.MARKER} && ")
    assert script.endswith('exec python -m pytest "$@"')


# Finding the probe.


def test_a_probe_is_found_by_its_public_usb_identity(tmp_path: Path) -> None:
    """Vendor and product id, as the product itself matches them, and nothing
    that names one particular probe. Root hubs, other devices and interface
    entries are passed over."""
    sysfs = tmp_path / "sys"
    a_usb_device(sysfs, "3-2", product="374b", bus=3, device=17)
    a_usb_device(sysfs, "usb3", vendor="1d6b", product="0002", bus=3, device=1, serial="0000:00:14.0", ttys=())
    a_usb_device(sysfs, "3-4", vendor="0403", product="6001", bus=3, device=18, serial="ADAPTER01", ttys=("ttyUSB0",))
    (sysfs / "bus" / "usb" / "devices" / "3-2_1.0").mkdir()

    probes = bench_in_container.discover_probes(sysfs)

    assert len(probes) == 1
    assert probes[0].usb_nodes == ("/dev/bus/usb/003/017",)
    assert probes[0].serial_ports == ("/dev/ttyACM0",)
    assert probes[0].serial_numbers == (PROBE_SERIAL,)
    assert PROBE_SERIAL not in probes[0].where


def test_every_serial_port_of_a_probe_is_handed_in(tmp_path: Path) -> None:
    """Some probes carry two bridges to the board; both belong to the run."""
    sysfs = tmp_path / "sys"
    a_usb_device(sysfs, "1-3", product="3753", ttys=("ttyACM1", "ttyACM2"))

    assert bench_in_container.discover_probes(sysfs)[0].serial_ports == ("/dev/ttyACM1", "/dev/ttyACM2")


def test_the_probe_identity_is_the_one_the_product_matches() -> None:
    """Two copies of one list, because the runner is a stdlib script run from a
    checkout that need not be installed. This is what keeps them one list."""
    from agentic_hil.comports import STLINK_USB_PRODUCT_IDS, STLINK_USB_VENDOR_ID

    assert bench_in_container.PROBE_VENDOR_ID == STLINK_USB_VENDOR_ID
    assert bench_in_container.PROBE_PRODUCT_IDS == STLINK_USB_PRODUCT_IDS


def test_no_probe_is_refused_before_anything_is_built(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    shutil.rmtree(machine.sysfs)

    assert run(machine) == bench_in_container.EXIT_NO_PROBE

    assert machine.runtime.commands == []
    err = capsys.readouterr().err
    assert "in-circuit debugger or programmer" in err
    assert "--usb-device" in err


def test_two_probes_are_refused_naming_each_by_where_it_sits(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    """Choosing between two would be choosing a board. The refusal names both
    by bus and device, which is enough to pick one, and neither by serial."""
    a_usb_device(machine.sysfs, "1-2", device=6, serial=SECOND_SERIAL, ttys=("ttyACM1",))

    assert run(machine) == bench_in_container.EXIT_NO_PROBE

    assert machine.runtime.commands == []
    err = capsys.readouterr().err
    assert "bus 1 device 5" in err
    assert "bus 1 device 6" in err
    assert PROBE_SERIAL not in err
    assert SECOND_SERIAL not in err


def test_a_probe_without_a_serial_port_is_refused(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    shutil.rmtree(machine.sysfs)
    a_usb_device(machine.sysfs, "1-1", ttys=())

    assert run(machine) == bench_in_container.EXIT_NO_PROBE

    assert "--serial-device" in capsys.readouterr().err


def test_named_devices_replace_discovery(machine: SimpleNamespace) -> None:
    shutil.rmtree(machine.sysfs)
    machine.statuses["/dev/bus/usb/003/007"] = a_node(mode=0o664, gid=USB_GROUP)
    machine.statuses["/dev/ttyACM3"] = a_node()
    machine.openable.update({"/dev/bus/usb/003/007", "/dev/ttyACM3"})

    assert run(machine, "--usb-device", "/dev/bus/usb/003/007", "--serial-device", "/dev/ttyACM3") == 0

    assert option_values(machine.runtime.tier, "--device") == ["/dev/bus/usb/003/007", "/dev/ttyACM3"]


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["--usb-device", USB_NODE], "--serial-device"),
        (["--serial-device", TTY_NODE], "--usb-device"),
        (["--usb-device", USB_NODE, "--serial-device", "/dev/ttyACM9"], "/dev/ttyACM9"),
        (["--usb-device", "/srv/firmware.bin", "--serial-device", TTY_NODE], "not a character device"),
    ],
)
def test_named_devices_are_refused_unless_both_kinds_are_real_character_devices(
    machine: SimpleNamespace, capsys: pytest.CaptureFixture, argv: list[str], says: str
) -> None:
    machine.statuses["/srv/firmware.bin"] = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=UID, st_gid=GID, st_rdev=0)

    assert run(machine, *argv) == bench_in_container.EXIT_NO_PROBE

    assert machine.runtime.commands == []
    assert says in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, "makedev"), reason="device numbers and their sysfs links are a POSIX layout")
def test_a_named_node_is_traced_back_to_its_probe_for_the_serial_to_withhold(
    machine: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    """/sys/dev/char/<major>:<minor> leads from a node to the device it belongs
    to, and the serial found there is withheld like a discovered one."""
    device = machine.sysfs / "devices" / "usb3" / "3-4"
    port = device / "3-4:1.2" / "tty" / "ttyACM3"
    port.mkdir(parents=True)
    (device / "idVendor").write_text("0483\n", encoding="utf-8")
    (device / "serial").write_text(f"{SECOND_SERIAL}\n", encoding="utf-8")
    characters = machine.sysfs / "dev" / "char"
    characters.mkdir(parents=True)
    (characters / "166:3").symlink_to(port)
    machine.statuses["/dev/bus/usb/003/007"] = a_node(mode=0o664, gid=USB_GROUP, rdev=os.makedev(189, 262))
    machine.statuses["/dev/ttyACM3"] = a_node(rdev=os.makedev(166, 3))
    machine.openable.update({"/dev/bus/usb/003/007", "/dev/ttyACM3"})
    machine.runtime.tier_output = PASSING_TIER + f"probe {SECOND_SERIAL} answered\n"

    assert run(machine, "--usb-device", "/dev/bus/usb/003/007", "--serial-device", "/dev/ttyACM3") == 0

    out = capsys.readouterr().out
    assert SECOND_SERIAL not in out
    assert f"probe {bench_in_container.WITHHELD} answered" in out


# Refusals about this machine.


def test_no_runtime_is_refused_before_anything_runs(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    machine.installed.clear()

    assert run(machine) == bench_in_container.EXIT_CANNOT_RUN_HERE

    assert machine.runtime.commands == []
    err = capsys.readouterr().err
    assert "podman" in err
    assert "docker" in err


def test_podman_is_preferred_and_docker_is_accepted(machine: SimpleNamespace) -> None:
    machine.installed.discard("podman")

    assert run(machine) == 0

    assert machine.runtime.tier[:2] == ["docker", "run"]


def test_a_named_runtime_that_is_not_installed_is_refused(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    machine.installed.discard("docker")

    assert run(machine, "--runtime", "docker") == bench_in_container.EXIT_CANNOT_RUN_HERE

    assert machine.runtime.commands == []
    assert "docker" in capsys.readouterr().err


def test_podman_without_crun_is_refused(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    machine.installed.discard("crun")

    assert run(machine) == bench_in_container.EXIT_CANNOT_RUN_HERE

    assert machine.runtime.commands == []
    assert "crun" in capsys.readouterr().err


def test_a_run_off_linux_is_refused(machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The board's device nodes are on the machine it is attached to, and a
    daemon in a virtual machine of its own has none of them."""
    monkeypatch.setattr(bench_in_container, "this_is_linux", lambda: False)

    assert run(machine) == bench_in_container.EXIT_CANNOT_RUN_HERE

    assert machine.runtime.commands == []
    assert "Linux" in capsys.readouterr().err


def test_the_image_alone_is_built_anywhere(machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--build-only` touches no device and needs no probe, so it runs where a
    run of the tier would be refused, and it starts no container."""
    monkeypatch.setattr(bench_in_container, "this_is_linux", lambda: False)
    shutil.rmtree(machine.sysfs)

    assert run(machine, "--build-only") == 0

    assert len(machine.runtime.issued("build")) == 1
    assert machine.runtime.issued("run") == []
    assert machine.runtime.issued("ps") == []


def test_root_is_refused(machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The device locks a run must share are under the invoking user's home. A
    run as root would take them under root's, where no run of that user sees
    them, so it is refused rather than run beside them."""
    monkeypatch.setattr(bench_in_container, "effective_uid", lambda: 0)

    assert run(machine) == bench_in_container.EXIT_CANNOT_RUN_HERE

    assert machine.runtime.commands == []
    assert "root" in capsys.readouterr().err


def test_podman_refuses_a_node_this_user_cannot_open(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    machine.openable.discard(TTY_NODE)

    assert run(machine) == bench_in_container.EXIT_NO_PROBE

    assert machine.runtime.commands == []
    err = capsys.readouterr().err
    assert TTY_NODE in err
    assert "tools/bench/README.md" in err


def test_docker_refuses_a_node_only_an_access_control_list_opens(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    """Docker recreates a node in the container with its owner, group and mode,
    and without its ACL. A node this user opens through an ACL alone opens on
    the host and fails in the container, so it is refused here instead."""
    machine.statuses[USB_NODE] = a_node(mode=0o664, gid=0)

    assert run(machine, "--runtime", "docker") == bench_in_container.EXIT_NO_PROBE

    assert machine.runtime.commands == []
    err = capsys.readouterr().err
    assert USB_NODE in err
    assert "ACL" in err


def test_a_device_lock_directory_that_is_a_link_is_refused(machine: SimpleNamespace, tmp_path: Path) -> None:
    """The product walks its lock directory without following links, so a link
    there is refused on the host; mounting its target would give the container
    locks the host's runs never take."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (machine.home / ".agentic-hil").mkdir()
    try:
        (machine.home / ".agentic-hil" / "device-locks").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("this machine does not let an unprivileged user create a symbolic link")

    assert run(machine) == bench_in_container.EXIT_CANNOT_RUN_HERE

    assert machine.runtime.issued("run") == []


# The verdict.


@pytest.mark.parametrize(
    ("returncode", "output", "status", "says"),
    [
        (0, PASSING_TIER, 0, "passed: 114 passed in 243.10s"),
        (0, MARKED + "== 110 passed, 4 xfailed in 240.00s ==\n", 0, "passed: 110 passed, 4 xfailed in 240.00s"),
        (1, MARKED + "== 2 failed, 112 passed in 250.00s ==\n", 1, "failed (exit 1): 2 failed, 112 passed in 250.00s"),
        (0, MARKED + "== 113 passed, 1 skipped in 240.00s ==\n", bench_in_container.EXIT_NO_RESULT, "did not reach the board"),
        (0, MARKED + "== 114 deselected in 0.50s ==\n", bench_in_container.EXIT_NO_RESULT, "nothing passed"),
        (0, MARKED + "tests/bench/test_bench_serial.py::test_echo PASSED\n", bench_in_container.EXIT_NO_RESULT, "no summary line"),
        (137, MARKED + "tests/bench/test_bench_serial.py::test_echo PASSED\n", 137, "no summary line"),
        (0, "== 114 passed in 243.10s ==\n", bench_in_container.EXIT_NO_RESULT, "marker"),
    ],
)
def test_the_verdict_is_pytests_summary_line_or_nothing(returncode: int, output: str, status: int, says: str) -> None:
    """A skip in this tier is a test that did not reach the board, so a run that
    skipped is no result, however green its exit status. So is a run that never
    said what it did, and one that did not run in the image this commit built."""
    judged, verdict = bench_in_container.judge(returncode, output, ["tests/bench", "-v"])

    assert judged == status
    assert says in verdict


def test_collecting_the_tier_is_a_result_of_its_own() -> None:
    judged, verdict = bench_in_container.judge(0, MARKED + "114 tests collected in 0.42s\n", ["tests/bench", "--collect-only", "-q"])

    assert judged == 0
    assert verdict == "collected: 114 tests collected in 0.42s"


def test_the_verdict_is_printed_and_logged(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    assert run(machine) == 0

    assert "verdict: passed: 114 passed in 243.10s" in capsys.readouterr().err
    log = (machine.output / bench_in_container.LOG_NAME).read_text(encoding="utf-8")
    assert "verdict: passed: 114 passed in 243.10s" in log


def test_a_red_tier_leaves_with_pytests_status(machine: SimpleNamespace) -> None:
    machine.runtime.tier_output = MARKED + "== 1 failed, 113 passed in 250.00s ==\n"
    machine.runtime.tier_returncode = 1

    assert run(machine) == 1


def test_a_report_from_an_earlier_run_is_never_left_to_be_uploaded(machine: SimpleNamespace) -> None:
    machine.output.mkdir()
    (machine.output / bench_in_container.REPORT_NAME).write_text("<testsuites/>", encoding="utf-8")
    machine.runtime.tier_output = "Error: the runtime could not start the container\n"
    machine.runtime.tier_returncode = 126

    assert run(machine) == 126

    assert not (machine.output / bench_in_container.REPORT_NAME).exists()


# What is withheld.


def test_the_redactor_withholds_whole_values_and_keeps_every_line() -> None:
    redact = bench_in_container.Redactor([PROBE_SERIAL, HOST_NAME, HOME])

    assert redact(f"probe {PROBE_SERIAL} on {HOST_NAME} in {HOME}/work") == "probe [withheld] on [withheld] in [withheld]/work"
    assert redact(f"usb-Vendor_Probe_{PROBE_SERIAL.lower()}-if02") == "usb-Vendor_Probe_[withheld]-if02"
    assert redact(f"{HOST_NAME}7 {PROBE_SERIAL}2") == f"{HOST_NAME}7 {PROBE_SERIAL}2"
    assert redact("") == ""


def test_the_redactor_ignores_values_too_short_or_too_common_to_mean_a_machine() -> None:
    redact = bench_in_container.Redactor(["ab", "bench", "root", "runner", ""])

    assert redact("tests/bench as root on a runner") == "tests/bench as root on a runner"


def test_the_probe_serial_and_the_machine_never_reach_the_output_the_log_or_the_report(
    machine: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    """The gate's log and its artifact are readable by anyone who can read the
    repository. The tier prints the probe's serial and paths of this machine;
    they are withheld, line by line, and no line is removed."""
    machine.runtime.tier_output = (
        MARKED
        + f"doctor: probe {PROBE_SERIAL} found\n"
        + f"config written under {HOME}/.config on {HOST_NAME}\n"
        + "== 114 passed in 243.10s ==\n"
    )

    def report(command: list[str]) -> None:
        results = Path(mounted_at(command, bench_in_container.RESULTS))
        (results / bench_in_container.REPORT_NAME).write_text(
            f'<testsuites><testcase name="t"><failure message="probe {PROBE_SERIAL} on {HOST_NAME}"/></testcase></testsuites>',
            encoding="utf-8",
        )

    machine.runtime.during_run = report

    assert run(machine) == 0

    printed = capsys.readouterr()
    log = (machine.output / bench_in_container.LOG_NAME).read_text(encoding="utf-8")
    junit = (machine.output / bench_in_container.REPORT_NAME).read_text(encoding="utf-8")
    for text in (printed.out, printed.err, log, junit):
        for secret in (PROBE_SERIAL, HOST_NAME, HOME):
            assert secret not in text
    assert "doctor: probe [withheld] found" in printed.out
    assert "doctor: probe [withheld] found" in log
    assert 'message="probe [withheld] on [withheld]"' in junit
    assert len(log.splitlines()) == len(machine.runtime.tier_output.splitlines()) + 1


# The queue and the teardown.


def a_lock_held_by(pid: int, **fields: object) -> Path:
    """Leave a holder record behind, as a run that took the lock would have."""
    record: dict[str, object] = {
        "version": run_lock.LOCK_RECORD_VERSION,
        "owner_id": "0123456789abcdef",
        "pid": pid,
        "host": socket.gethostname(),
        "started_at": "2026-09-01T09:12:33Z",
        "root": "/srv/checkouts/agentic-hil",
        "tool": "tools/ci_linux.py",
        "runs_for": "5 to 7 minutes",
    }
    record.update(fields)
    path = run_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def test_the_machine_is_held_for_the_whole_run(machine: SimpleNamespace) -> None:
    """The run lock the other container tools take, held from before the build
    to after the container is gone, so a second run of any of them queues
    instead of meeting this one at the device locks."""
    refusals: list[str] = []

    def a_second_run_tries(command: list[str]) -> None:
        other = run_lock.RunLock()
        try:
            other.acquire(wait=False)
        except run_lock.RunLockBusy as busy:
            refusals.append(str(busy))
        else:
            other.release()
            refusals.append("the lock was free while the tier was running")

    machine.runtime.during_run = a_second_run_tries

    assert run(machine) == 0

    assert len(refusals) == 1
    assert bench_in_container.TOOL_NAME in refusals[0]
    assert not run_lock.lock_path().exists()


def test_the_machine_is_given_back_after_a_red_run(machine: SimpleNamespace) -> None:
    machine.runtime.tier_output = MARKED + "== 1 failed, 113 passed in 250.00s ==\n"
    machine.runtime.tier_returncode = 1

    assert run(machine) == 1

    assert not run_lock.lock_path().exists()


def test_no_wait_refuses_a_held_machine_and_builds_nothing(
    machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    lock = a_lock_held_by(4242)
    monkeypatch.setattr(run_lock, "process_is_running", lambda pid: pid == 4242)

    assert run(machine, "--no-wait") == bench_in_container.EXIT_LOCKED

    assert machine.runtime.commands == []
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == 4242
    assert "pid 4242" in capsys.readouterr().err


def test_the_lock_notices_are_withheld_like_everything_else(
    machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A queued gate prints who holds the machine into a log anyone can read,
    and the holder's record names a checkout under this machine's home."""
    a_lock_held_by(4242, root=f"{HOME}/agentic-hil")
    monkeypatch.setattr(run_lock, "process_is_running", lambda pid: pid == 4242)

    assert run(machine, "--no-wait") == bench_in_container.EXIT_LOCKED

    err = capsys.readouterr().err
    assert HOME not in err
    assert f"{bench_in_container.WITHHELD}/agentic-hil" in err


def test_a_leftover_container_of_this_user_is_stopped_before_anything_starts(machine: SimpleNamespace) -> None:
    """A run whose client was killed can leave its container behind, still on
    the board. The next run stops it, which is the stop signal the tier tears
    down on, and removes it before it builds or starts anything."""
    machine.runtime.leftovers = ("agentic-hil-bench-0badc0de",)

    assert run(machine) == 0

    commands = machine.runtime.commands
    listing = machine.runtime.issued("ps")
    assert listing == [["podman", "ps", "--all", "--filter", f"label={bench_in_container.RUN_LABEL}={UID}", "--format", "{{.Names}}"]]
    stop = commands.index(["podman", "stop", "--time", str(bench_in_container.STOP_TIMEOUT_S), "agentic-hil-bench-0badc0de"])
    remove = commands.index(["podman", "rm", "--force", "agentic-hil-bench-0badc0de"])
    build = commands.index(machine.runtime.issued("build")[0])
    assert stop < remove < build


def test_a_leftover_that_will_not_go_keeps_the_machine_held(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    machine.runtime.leftovers = ("agentic-hil-bench-0badc0de",)
    machine.runtime.never_removed = True

    assert run(machine) == bench_in_container.EXIT_LEFTOVER

    assert machine.runtime.issued("build") == []
    record = json.loads(run_lock.lock_path().read_text(encoding="utf-8"))
    assert "agentic-hil-bench-0badc0de" in record[run_lock.CLEANUP_REQUIRED_FIELD]
    assert "agentic-hil-bench-0badc0de" in capsys.readouterr().err


def test_an_interrupt_stops_the_container_and_waits_for_the_tiers_teardown(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    """The container stops on SIGINT, which the tier tears down on, so the demo
    goes back on the board before the container exits. The run waits for that,
    confirms the container gone, gives the machine back and says it was
    interrupted rather than judging a run that was cut short."""
    machine.runtime.interrupted = True
    machine.runtime.tier_returncode = 2
    machine.runtime.tier_output = MARKED + "!!!!!!! KeyboardInterrupt !!!!!!!\n== 3 passed in 12.00s ==\n"
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    assert run(machine) == bench_in_container.EXIT_INTERRUPTED

    name = option_values(machine.runtime.tier, "--name")[0]
    commands = machine.runtime.commands
    assert commands.index(["podman", "stop", "--time", str(bench_in_container.STOP_TIMEOUT_S), name]) > commands.index(machine.runtime.tier)
    assert ["podman", "container", "inspect", "--format", "{{.State.Status}}", name] in commands
    assert not run_lock.lock_path().exists()
    assert "verdict: interrupted" in capsys.readouterr().err
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before


def test_a_container_that_outlives_the_run_keeps_the_machine_held(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    """Released, the machine would go to the next run while this container may
    still hold the board. The lock is kept, marked, and the way out is named."""
    machine.runtime.never_removed = True

    assert run(machine) == bench_in_container.EXIT_LEFTOVER

    name = option_values(machine.runtime.tier, "--name")[0]
    assert ["podman", "rm", "--force", name] in machine.runtime.commands
    record = json.loads(run_lock.lock_path().read_text(encoding="utf-8"))
    assert name in record[run_lock.CLEANUP_REQUIRED_FIELD]
    assert f"rm --force {name}" in capsys.readouterr().err


@pytest.mark.parametrize("goes", [True, False], ids=["gone", "stays"])
def test_an_interrupt_the_tiers_own_handling_missed_still_accounts_for_the_container(
    machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, goes: bool
) -> None:
    """An interrupt can land while the container is starting or winding down, or
    on top of a first one, and so outside the handling that stops the container.
    The machine is still given back only once the container is confirmed gone."""

    def interrupted_while_starting(runtime: str, name: str, command: list[str], *rest: object) -> None:
        machine.runtime.commands.append(list(command))
        raise KeyboardInterrupt

    monkeypatch.setattr(bench_in_container, "run_tier", interrupted_while_starting)
    machine.runtime.never_removed = not goes
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    status = run(machine)

    name = option_values(machine.runtime.tier, "--name")[0]
    assert ["podman", "container", "inspect", "--format", "{{.State.Status}}", name] in machine.runtime.commands
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    if goes:
        assert status == bench_in_container.EXIT_INTERRUPTED
        assert not run_lock.lock_path().exists()
        assert "no verdict" in capsys.readouterr().err
    else:
        assert status == bench_in_container.EXIT_LEFTOVER
        assert ["podman", "rm", "--force", name] in machine.runtime.commands
        record = json.loads(run_lock.lock_path().read_text(encoding="utf-8"))
        assert name in record[run_lock.CLEANUP_REQUIRED_FIELD]


def test_a_failed_build_starts_nothing_and_names_its_own_error(machine: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    machine.runtime.build_output = "STEP 5/14: RUN apt-get update\nE: Unable to locate package gdb-multiarch\n"
    machine.runtime.build_returncode = 1

    assert run(machine) == bench_in_container.EXIT_BUILD_FAILED

    assert machine.runtime.issued("run") == []
    assert "E: Unable to locate package gdb-multiarch" in capsys.readouterr().err
    assert not run_lock.lock_path().exists()


# The committed tree, for real.


def test_the_committed_tree_is_staged_without_the_checkouts_own_state(tmp_path: Path) -> None:
    if not (REPOSITORY_ROOT / ".git").exists():
        pytest.skip("this copy of the tree is not a git checkout")
    commit = bench_in_container.current_commit(REPOSITORY_ROOT)
    destination = tmp_path / "context"

    bench_in_container.stage_committed_tree(REPOSITORY_ROOT, commit, destination)

    assert (destination / "tools" / "bench" / "Dockerfile").is_file()
    assert (destination / "tools" / "bench" / "Dockerfile.dockerignore").is_file()
    assert not (destination / ".git").exists()


# What the image gives the runner.


def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_the_marker_is_the_one_the_image_writes() -> None:
    marker = bench_in_container.MARKER
    assert f"printf '{bench_in_container.MARKER_TEXT}\\n' > {marker}" in dockerfile()


def test_the_home_the_locks_are_mounted_under_is_the_images() -> None:
    home = bench_in_container.CONTAINER_HOME
    text = dockerfile()

    assert f"ENV HOME={home}" in text
    assert f"install -d -m 1777 {home} {home}/.agentic-hil" in text


def test_the_image_carries_the_label_the_runner_prunes_by() -> None:
    key, value = bench_in_container.IMAGE_LABEL.split("=", 1)

    assert f'LABEL {key}="{value}"' in dockerfile()


def test_the_image_stops_the_way_an_interrupted_pytest_tears_down() -> None:
    text = dockerfile()

    assert "STOPSIGNAL SIGINT" in text
    # The whole process group, as Ctrl-C in a terminal: the product's command
    # pytest is waiting on hears the interrupt too.
    assert 'ENTRYPOINT ["/usr/bin/tini", "-g", "--"]' in text


def test_the_image_base_is_pinned_by_digest_and_named_with_its_registry() -> None:
    base = next(line for line in dockerfile().splitlines() if line.startswith("FROM "))

    assert base.startswith("FROM docker.io/")
    assert "@sha256:" in base


def test_no_layer_installs_from_the_index_without_hashes() -> None:
    installs = [line for line in dockerfile().splitlines() if "pip install" in line]

    assert installs
    for line in installs:
        if line.rstrip().endswith("-e ."):
            assert "--no-deps" in line and "--no-build-isolation" in line, line
        else:
            assert "--require-hashes" in line, line


def test_the_image_carries_the_tools_the_tier_builds_and_debugs_with() -> None:
    install = next(line for line in dockerfile().splitlines() if "apt-get install" in line)
    packages = set(install.split("--yes", 1)[1].split())

    assert "--no-install-recommends" in install
    assert {"openocd", "gcc-arm-none-eabi", "libnewlib-arm-none-eabi", "gdb-multiarch", "cmake", "ninja-build", "tini"} <= packages


def test_the_build_context_is_an_allowlist_of_what_the_tier_reads() -> None:
    ignore = [line.strip() for line in IGNORE_FILE.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]

    assert ignore[0] == "**", ignore
    admitted = {line.lstrip("!") for line in ignore if line.startswith("!")}
    assert {"pyproject.toml", "src", "tests", "requirements", "examples/nucleo-f446re_demo"} <= admitted, admitted


def test_the_page_the_refusals_point_at_says_what_the_machine_provides_once() -> None:
    """Every refusal about the machine, and the gate's header, name one page.

    Whoever meets one of them goes there for the pieces a run relies on: the
    runtime and the OCI runtime that keeps the user's groups, the user's own id
    ranges and linger, the rights to the probe's nodes, the check that builds
    without a board, and the gate that runs it all on one.
    """
    page = README.read_text(encoding="utf-8")
    runner = (REPOSITORY_ROOT / "tools" / "bench_in_container.py").read_text(encoding="utf-8")
    gate = (REPOSITORY_ROOT / ".github" / "workflows" / "bench-gate.yml").read_text(encoding="utf-8")

    assert "tools/bench/README.md" in runner and "tools/bench/README.md" in gate
    for needed in (
        "podman",
        "crun",
        "keep-groups",
        "uidmap",
        "slirp4netns",
        "/etc/subuid",
        "/etc/subgid",
        "enable-linger",
        "/dev/bus/usb",
        "--build-only",
        "--runtime docker",
        "bench-gate.yml",
    ):
        assert needed in page, f"tools/bench/README.md does not say {needed!r}"
