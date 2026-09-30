"""Whether this account may open the probe and the serial port, asked by `doctor` and `init` (#604's sibling).

On Linux an account that cannot open the probe's USB device or the serial port
learned it from the first hardware call that failed. `doctor` opened no device
and stayed green, and `init` bound a probe and a port this account could not
open without a word. The check asks the kernel with `os.access(R_OK | W_OK)`,
which applies the node's mode, its group and its ACL to this process, and opens
nothing. The probe's node is found through sysfs by the USB identity the product
recognises, the way tools/bench_in_container.py finds it.

Every platform fact below is replayed from `fixtures/device_access_linux_recording.json`,
taken on the bench in the three places the bench tier runs: on the host, and in
the tier's container with the device groups kept and withheld. Two cases are
composed from those recorded pieces rather than recorded whole, and say so where
they are built: an account added to the group after this login began, and a
probe node udev left as it leaves a device no rule applies to.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_OPENOCD, write_authoritative_config

from agentic_hil import cli, device_access
from agentic_hil.bootstrap import PROJECT_PROFILE
from agentic_hil.cli import (
    DOCTOR_DEVICE_ACCESS_FINDING,
    DOCTOR_FINDINGS_SETUP_KEEPS,
    doctor,
    doctor_findings_setup_keeps,
)
from agentic_hil.config import load_authoritative_config
from agentic_hil.humanize import render_result
from agentic_hil.knowledge import remediation_fields
from agentic_hil.types import JsonObject

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RECORDING: JsonObject = json.loads((FIXTURES / "device_access_linux_recording.json").read_text(encoding="utf-8"))["recording"]
# The serial inventory recorded on the same bench, which is what `init` binds
# the probe and the port from.
RECORDED_INVENTORY: JsonObject = json.loads((FIXTURES / "com_ports_ubuntu_24_04_recording.json").read_text(encoding="utf-8"))["recording"]

ON_THE_HOST = "host"
KEEPING_THE_GROUPS = "container_with_the_device_groups"
WITHHOLDING_THE_GROUPS = "container_without_the_device_groups"

# What the recording names, spelled once.
PROBE_SERIAL = "066BFF505050505050505050"
PROBE_NODE = "/dev/bus/usb/002/002"
PORT_NODE = "/dev/ttyACM0"
BY_ID_PATH = "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066BFF505050505050505050-if02"
# A root hub's node on the recorded host: root:root crw-rw-r--, the state udev
# leaves a USB device in when no rule applies to it.
ROOT_HUB_NODE = "/dev/bus/usb/002/001"
# The group the probe's udev rule gives its node to on the recorded host.
PROBE_GROUP_GID = 46

# The attributes the product reads off each USB device.
SYSFS_ATTRIBUTES = ("idVendor", "idProduct", "busnum", "devnum", "serial")

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX device nodes, modes and account groups; on Windows the check answers nothing before it reads one")
# A mode that admits nobody admits root all the same, so the refusal these pin
# cannot happen for a process that is root.
NOT_ROOT_ONLY = pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes, and root may open a node whatever its mode says")

STARTER_PROFILE: JsonObject = {
    "target": {"name": "nucleo-f446re-starter", "controller": "stm32f446ret6"},
    "debuggers": {"dut": {"timeout_s": 60, "permissions": {}}},
    "com_ports": {"dut_uart": {"baudrate": 115200, "permissions": {}}},
}


class RecordedHost:
    """One recorded place, answering every question the check asks of a host.

    The sysfs tree is written out under `sysfs_root` with the attributes the
    product reads, one file per attribute as sysfs has them. An interface's name
    carries a colon, which NTFS refuses in a file name, so it is written with an
    underscore instead; nothing reads an interface's directory by name, and none
    of them carries a vendor id, so the replay reads the same devices on every
    platform the suite runs on."""

    def __init__(self, place: JsonObject, sysfs_root: Path) -> None:
        self.place = place
        self.usb_devices = sysfs_root
        sysfs_root.mkdir(parents=True, exist_ok=True)
        for name, record in place["sysfs_usb_devices"].items():
            device = sysfs_root / name.replace(":", "_")
            device.mkdir()
            for attribute in SYSFS_ATTRIBUTES:
                if attribute in record:
                    (device / attribute).write_text(f"{record[attribute]}\n", encoding="utf-8")
        self.nodes = {node["path"]: node for node in place["nodes"]}
        credentials = place["credentials"]
        self.login = frozenset({*credentials["login_groups"], credentials["gid"], credentials["egid"]})
        self.account = frozenset(credentials["database_groups"])
        self.group_names = {int(gid): name for gid, name in credentials["group_names"].items()}
        self.owner_names = {node["st_uid"]: node["owner"] for node in place["nodes"]}
        self.group_names.update({node["st_gid"]: node["group"] for node in place["nodes"]})
        # A plain host's own map, which is the whole range in one line and the
        # answer `LocalHost` gives wherever there is no namespace to read. The
        # recording carries what the kernel said about each node and about this
        # account's groups, and not the namespace's gid map, which is a fact about
        # the container rather than about a device, so a place that runs in one
        # states its map (see `in_a_rootless_namespace`).
        self.mapped_gids = [(0, 2**32 - 1)]

    def asks(self) -> bool:
        return True

    def realpath(self, path: str) -> str:
        node = self.nodes.get(path)
        return node["realpath"] if node else path

    def status(self, path: str) -> device_access.NodeStatus | None:
        node = self.nodes.get(path)
        if node is None:
            return None
        return device_access.NodeStatus(mode=node["st_mode"], uid=node["st_uid"], gid=node["st_gid"])

    def may_open(self, path: str) -> bool:
        return self.nodes[path]["access_rw"]

    def gid_is_mapped(self, gid: int) -> bool:
        return any(first <= gid < first + count for first, count in self.mapped_gids)

    def login_groups(self) -> frozenset[int]:
        return self.login

    def account_groups(self) -> frozenset[int] | None:
        return self.account

    def group_name(self, gid: int) -> str | None:
        return self.group_names.get(gid)

    def user_name(self, uid: int) -> str | None:
        return self.owner_names.get(uid)


def recorded_host(tmp_path: Path, place: str) -> RecordedHost:
    return RecordedHost(copy.deepcopy(RECORDING[place]), tmp_path / f"sysfs-{place}")


def added_after_this_login(tmp_path: Path) -> RecordedHost:
    """The recorded host as it is after `usermod -aG` and before a new login.

    Composed from the host recording: the account database still lists the
    probe's group, this login's groups are the recorded ones without it, and
    the nodes that group opens answer the way the kernel answers a process
    outside it, as the container without the groups recorded for the same
    nodes."""
    host = recorded_host(tmp_path, ON_THE_HOST)
    host.login = host.login - {PROBE_GROUP_GID}
    for node in host.nodes.values():
        if node["st_gid"] == PROBE_GROUP_GID:
            node["access_rw"] = False
    return host


def with_no_udev_rule(tmp_path: Path) -> RecordedHost:
    """The recorded host with the probe's node as udev leaves a device no rule applies to.

    Composed from the host recording: the probe's node carries the mode, owner,
    group and kernel answer recorded for a root hub on the same host, which is
    root:root crw-rw-r-- because no rule gives a hub to a group."""
    host = recorded_host(tmp_path, ON_THE_HOST)
    hub = host.nodes[ROOT_HUB_NODE]
    host.nodes[PROBE_NODE] = {**host.nodes[PROBE_NODE], **{key: hub[key] for key in ("st_mode", "st_uid", "st_gid", "filemode", "owner", "group", "access_rw")}}
    return host


def in_a_rootless_namespace(tmp_path: Path, place: str) -> RecordedHost:
    """One recorded container place, with the user namespace it ran in stated.

    The recording holds what the kernel answered inside that container and not
    the namespace's own gid map, so the map is stated here the way
    `added_after_this_login` and `with_no_udev_rule` state what they compose. A
    rootless container maps one group id and no more, the one the invoking account
    runs as, which is why every host group outside that map appears as the
    overflow group the recording did capture: the node's group is 65534 `nogroup`
    in the place that withholds the device groups, and `nogroup` is not a group
    anything in the namespace can be joined to.
    """
    host = recorded_host(tmp_path, place)
    host.mapped_gids = [(0, 1)]
    return host


class AskedLocalHost(device_access.LocalHost):
    """This machine, asked.

    The suite's own host answers `asks()` False so no assertion is decided by the
    developer's groups or by whatever is plugged in (see `tests/conftest.py`).
    The readers below it are this machine's all the same, and a test that hands
    them a path under `tmp_path` exercises the code the bench runs without
    asking anything of a device: nothing here is a device node."""

    def asks(self) -> bool:
        return True


def flat(text: str) -> str:
    return " ".join(text.split())


def _database_name(read: Callable[[], str]) -> str | None:
    """What the account database answers, or None where it names no such number.

    The same two refusals `LocalHost` itself folds into None: a number with no
    entry, and one too large for the database to hold at all."""
    try:
        return read()
    except (KeyError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# What the check answers, place by place.


def test_the_host_with_the_device_groups_may_open_the_probe_and_the_port(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, ON_THE_HOST)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)
    port = device_access.port_access(BY_ID_PATH, host=host)

    assert probe is not None and port is not None
    assert probe["ok"] is True, probe
    assert probe["tool"] == device_access.DEVICE_ACCESS_TOOL
    assert (probe["node"], probe["group"], probe["mode"]) == (PROBE_NODE, "plugdev", "crw-rw----")
    assert PROBE_NODE in probe["summary"]
    assert "error_type" not in probe and "remediation" not in probe
    # The port is checked at the node the link resolves to, and the link is named beside it.
    assert port["ok"] is True, port
    assert (port["node"], port["link"], port["group"]) == (PORT_NODE, BY_ID_PATH, "plugdev")
    assert PORT_NODE in port["summary"] and BY_ID_PATH in port["summary"]


def test_the_probe_is_found_by_its_serial_in_any_case(tmp_path: Path) -> None:
    """A probe serial names a unit, and the product folds it the way it folds every hardware id."""
    host = recorded_host(tmp_path, ON_THE_HOST)

    probe = device_access.probe_access(PROBE_SERIAL.lower(), host=host)

    assert probe is not None and probe["node"] == PROBE_NODE


def test_without_a_probe_id_the_one_attached_probe_is_the_one_checked(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, ON_THE_HOST)

    probe = device_access.probe_access(None, host=host)

    assert probe is not None and probe["node"] == PROBE_NODE, probe


def test_without_a_probe_id_a_backend_that_opens_no_st_link_adopts_nobodys_probe(tmp_path: Path) -> None:
    """The sole attached ST-Link is not every entry's probe.

    `attached_probes` recognises the ST-Link USB identity and no other, so a
    pyOCD or CMSIS-DAP entry that pins no serial has no probe here this module can
    find. Answering with the one ST-Link on the workstation attached an unrelated
    board's verdict to it: `doctor` exited non-zero naming a `/dev/bus/usb/...`
    node the configuration never touches, `init` and `setup` repeated it as a
    warning, and in a multi-debugger file that one verdict was copied onto every
    entry with no serial. An entry that cannot be shown to open an ST-Link answers
    nothing, which is what this module does everywhere else it cannot tell.
    """
    host = recorded_host(tmp_path, WITHHOLDING_THE_GROUPS)

    assert device_access.probe_access(None, host=host, opens_an_stlink=False) is None
    # A serial settles it whatever the backend is: a serial that matches one of
    # the ST-Links on this USB names that ST-Link and nothing has to be decided.
    named = device_access.probe_access(PROBE_SERIAL, host=host, opens_an_stlink=False)
    assert named is not None and named["node"] == PROBE_NODE, named


@pytest.mark.parametrize(
    ("debugger_type", "interface_cfg", "opens_one"),
    [
        ("stlink", "", True),
        ("openocd", "interface/stlink.cfg", True),
        ("openocd", "interface/stlink-dap.cfg", True),
        ("openocd", "/opt/openocd/share/openocd/scripts/interface/stlink.cfg", True),
        ("openocd", "interface/cmsis-dap.cfg", False),
        ("openocd", "interface/jlink.cfg", False),
        ("pyocd", "", False),
    ],
)
def test_which_backends_open_an_st_link(debugger_type: str, interface_cfg: str, opens_one: bool) -> None:
    """STM32CubeProgrammer has no other transport; OpenOCD's is whatever its
    interface script names; pyOCD's is not written down anywhere the configuration
    can be read, because it opens ST-Links and CMSIS-DAP probes alike."""
    assert device_access.backend_opens_an_stlink(debugger_type, interface_cfg) is opens_one


def test_the_container_that_keeps_the_groups_may_open_both(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, KEEPING_THE_GROUPS)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)
    port = device_access.port_access(BY_ID_PATH, host=host)

    assert probe is not None and probe["ok"] is True, probe
    assert port is not None and port["ok"] is True, port


def test_the_container_that_withholds_the_groups_may_open_neither_and_says_the_group_is_not_mapped(tmp_path: Path) -> None:
    """The remedy has to be the one that works, and adding an account to `nogroup` is not it.

    In a rootless container every host group outside the namespace's map appears
    as the overflow group, which is what the recording captured: the nodes are
    owned by 65534 `nogroup` there. Naming that as the group an administrator adds
    this account to is advice that changes nothing, because there is no such group
    in the namespace to be in. What the bench runner itself does is map the groups
    in (`--group-add keep-groups`, tools/bench_in_container.py), and that is what
    the sentence has to say.
    """
    host = in_a_rootless_namespace(tmp_path, WITHHOLDING_THE_GROUPS)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)
    port = device_access.port_access(BY_ID_PATH, host=host)

    for check, node, scope in ((probe, PROBE_NODE, device_access.PROBE_SCOPE), (port, PORT_NODE, device_access.COM_PORT_SCOPE)):
        assert check is not None
        assert check["ok"] is False, check
        assert check["error_type"] == device_access.DEVICE_ACCESS_DENIED
        assert check["node"] == node
        # The group that owns the node as this process sees it: in a rootless
        # container every host group outside its map is the overflow group.
        assert check["group"] == "nogroup"
        assert check["cause"] == "group_not_mapped", check
        assert check["login_in_group"] is False
        assert check["account_in_group"] is False
        assert node in check["summary"] and "nogroup" in check["summary"]
        # The remedy that works, and not the one that cannot.
        assert "not mapped into this user namespace" in check["summary"], check["summary"]
        assert "keep-groups" in check["summary"], check["summary"]
        assert f"adds it to {check['group']}" not in check["summary"], check["summary"]
        assert check["remediation"] == remediation_fields(device_access.DEVICE_ACCESS_DENIED, scope)["remediation"]
        assert check["do_not"] == remediation_fields(device_access.DEVICE_ACCESS_DENIED, scope)["do_not"]
    assert port is not None and port["link"] == BY_ID_PATH and BY_ID_PATH in port["summary"]


def test_a_group_the_namespace_does_map_is_still_a_group_to_be_added_to(tmp_path: Path) -> None:
    """The other side of the same fork, so the new cause cannot swallow the old one.

    On a host, and in a container whose map covers the owning group, an account
    outside that group is exactly what it looks like: a group that exists here and
    that an administrator adds the account to once.
    """
    host = recorded_host(tmp_path, WITHHOLDING_THE_GROUPS)

    port = device_access.port_access(BY_ID_PATH, host=host)

    assert port is not None and port["cause"] == "not_in_group", port
    assert "an administrator adds it to nogroup once" in port["summary"], port["summary"]


def test_an_account_database_that_cannot_be_read_is_not_a_claim_about_the_account(tmp_path: Path) -> None:
    """A uid with no passwd entry is the case this check exists for, and the one it guessed at.

    `docker run -u 4242` and an arbitrary uid under OpenShift have no entry, so
    `account_groups` answers None, and None is falsy: the sentence fell through to
    "this account is not in the group plugdev", a positive statement about a
    database nothing read. The JSON stayed honest at `account_in_group: null`; the
    sentence a person reads did not. It is also exactly where "log in again"
    against "ask an administrator" is the decision being made, so guessing one is
    the worst of the three answers available.
    """
    host = recorded_host(tmp_path, ON_THE_HOST)
    host.account = None  # type: ignore[assignment]
    for node in host.nodes.values():
        if node["st_gid"] == PROBE_GROUP_GID:
            node["access_rw"] = False
    host.login = host.login - {PROBE_GROUP_GID}

    port = device_access.port_access(BY_ID_PATH, host=host)

    assert port is not None and port["ok"] is False, port
    assert port["cause"] == "account_unknown", port
    assert port["account_in_group"] is None, port
    assert "no entry in the account database" in port["summary"], port["summary"]
    # Both remedies named, because which one applies is what could not be read.
    assert "log in again" in port["summary"] and "an administrator adds it to plugdev" in port["summary"], port["summary"]
    assert "this account is not in the group" not in port["summary"], port["summary"]


def test_an_account_added_to_the_group_after_this_login_is_told_to_log_in_again(tmp_path: Path) -> None:
    host = added_after_this_login(tmp_path)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)
    port = device_access.port_access(BY_ID_PATH, host=host)

    for check in (probe, port):
        assert check is not None and check["ok"] is False, check
        assert check["cause"] == "not_in_this_login"
        assert check["account_in_group"] is True
        assert check["login_in_group"] is False
        assert "plugdev" in check["summary"]
        assert "log in again" in check["summary"]


def test_a_probe_node_left_as_root_root_says_no_udev_rule_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on_linux(monkeypatch)
    host = with_no_udev_rule(tmp_path)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)

    assert probe is not None and probe["ok"] is False, probe
    assert probe["cause"] == "no_udev_rule"
    assert (probe["owner"], probe["group"], probe["mode"]) == ("root", "root", "crw-rw-r--")
    assert "no udev rule" in probe["summary"]


def on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform whose device nodes are udev's, stated rather than inherited.

    The recordings are a Linux bench's, and the sentences that name a udev rule
    are given on Linux only, so a test about one says which platform it is asking
    about instead of passing or failing on where the suite happens to run."""
    monkeypatch.setattr(device_access.sys, "platform", "linux")


def a_node_owned_by_wheel(tmp_path: Path) -> RecordedHost:
    """The recorded host with the port's node as a macOS tty is left.

    Composed, and said to be: `crw-rw---- root:wheel` is how that platform leaves
    a serial device, and gid 0 there is `wheel` rather than `root`. Only the
    numbers and the names change; the kernel's answer is the recorded one for a
    node this account may not open."""
    host = recorded_host(tmp_path, ON_THE_HOST)
    host.group_names[0] = "wheel"
    host.nodes[PORT_NODE] = {**host.nodes[PORT_NODE], "st_mode": 0o020660, "st_uid": 0, "st_gid": 0, "access_rw": False}
    return host


def test_a_platform_without_udev_is_never_told_to_install_a_udev_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gid 0 is `wheel` on macOS, and there is no udev there to write a rule for.

    `no_udev_rule` reads "root:root is how udev leaves a device no rule applies
    to", and it was keyed on the two numbers alone. A `crw-rw---- root:wheel` tty
    this account cannot open therefore produced that sentence, which contradicts
    itself (the node it just printed is not root:root) and prescribes a rule file
    for a system with no udev at all. What is true there is the group step, which
    is what the account is actually missing.
    """
    monkeypatch.setattr(device_access.sys, "platform", "darwin")
    host = a_node_owned_by_wheel(tmp_path)

    port = device_access.port_access(BY_ID_PATH, host=host)

    assert port is not None and port["ok"] is False, port
    assert (port["owner"], port["group"], port["mode"]) == ("root", "wheel", "crw-rw----")
    assert port["cause"] == "not_in_group", port
    assert "udev" not in port["summary"], port["summary"]
    assert "an administrator adds it to wheel once" in port["summary"], port["summary"]


def test_the_same_node_on_linux_still_reads_as_a_group_udev_gave_it_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gating is the platform and not the group's name: a Linux node owned by
    a group named `wheel` is a group this account is missing, and one owned by
    `root` is the state udev leaves a device no rule applies to."""
    on_linux(monkeypatch)
    host = a_node_owned_by_wheel(tmp_path)

    port = device_access.port_access(BY_ID_PATH, host=host)

    assert port is not None and port["cause"] == "not_in_group", port
    host.group_names[0] = "root"
    assert device_access.port_access(BY_ID_PATH, host=host)["cause"] == "no_udev_rule"  # type: ignore[index]


def test_the_remediation_names_the_udev_rule_and_the_group_step(tmp_path: Path) -> None:
    """The steps #604's `adapter_access_denied` causes and TROUBLESHOOTING.md name, and nothing that loosens the node."""
    for scope, section in ((device_access.PROBE_SCOPE, "section 6"), (device_access.COM_PORT_SCOPE, "section 11")):
        fields = remediation_fields(device_access.DEVICE_ACCESS_DENIED, scope)
        steps = flat(" ".join(fields["remediation"]))
        assert "log in again" in steps
        assert "usermod -aG" in steps
        assert "udev rule" in steps
        assert section in steps
        assert any("chmod" in step for step in fields["do_not"])
        assert any("root" in step for step in fields["do_not"])


# ---------------------------------------------------------------------------
# A node the host could not stat at all.


def a_stat_that_fails(tmp_path: Path, failure: device_access.NodeFailure) -> RecordedHost:
    """The recorded host with `os.stat` refusing every node, the way a closed directory on the way to one makes it."""
    host = recorded_host(tmp_path, ON_THE_HOST)
    host.status = lambda path: failure  # type: ignore[method-assign, return-value]
    return host


def test_a_node_whose_attributes_may_not_even_be_read_is_a_refusal_that_names_the_errno(tmp_path: Path) -> None:
    """EACCES on the stat itself is the refusal this check exists to give, not a silence.

    A directory on the way to the node this account may not search, or a security
    module that denies the node, refuses `os.stat` before any mode can be read.
    Opening it would be refused too, so the answer is a refusal, and the errno is
    the whole evidence for it."""
    refused = device_access.NodeFailure(name="EACCES", number=errno.EACCES, message=f"[Errno 13] Permission denied: '{PORT_NODE}'")
    host = a_stat_that_fails(tmp_path, refused)

    port = device_access.port_access(BY_ID_PATH, host=host)

    assert port is not None and port["ok"] is False, port
    assert port["error_type"] == device_access.DEVICE_ACCESS_DENIED
    assert port["cause"] == "stat_refused"
    assert (port["errno"], port["node"], port["link"]) == ("EACCES", PORT_NODE, BY_ID_PATH)
    # The decisive line itself, in the sentence a person reads.
    assert refused.message in port["summary"], port["summary"]
    assert "EACCES" in port["summary"]
    assert port["remediation"] == remediation_fields(device_access.DEVICE_ACCESS_DENIED, device_access.COM_PORT_SCOPE)["remediation"]


def test_a_stat_that_failed_for_another_reason_is_a_check_that_could_not_be_made(tmp_path: Path) -> None:
    """Not a refusal and not a silence: nothing was denied, and the errno says why nothing could be said."""
    broken = device_access.NodeFailure(name="EIO", number=errno.EIO, message=f"[Errno 5] Input/output error: '{PROBE_NODE}'")
    host = a_stat_that_fails(tmp_path, broken)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)

    assert probe is not None, probe
    assert probe["ok"] is True, probe
    assert probe["status"] == "undetermined"
    assert probe["errno"] == "EIO"
    assert "error_type" not in probe and "remediation" not in probe
    assert broken.message in probe["summary"], probe["summary"]


@NOT_ROOT_ONLY
def test_this_machine_keeps_the_errno_of_a_stat_it_could_not_make(tmp_path: Path) -> None:
    """The reader's own half: a directory this account may not search, which is how EACCES reaches the stat."""
    closed = tmp_path / "closed"
    closed.mkdir()
    node = closed / "ttyACM0"
    node.write_text("", encoding="utf-8")
    closed.chmod(0o000)
    try:
        failure = AskedLocalHost().status(str(node))
    finally:
        closed.chmod(0o700)

    assert isinstance(failure, device_access.NodeFailure), failure
    assert (failure.name, failure.number) == ("EACCES", errno.EACCES)
    assert "Permission denied" in failure.message


def test_doctor_shows_a_check_it_could_not_make_and_stays_green_over_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was refused, so nothing is failed, and the errno is in the report rather than nowhere."""
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)
    broken = device_access.NodeFailure(name="ENOTDIR", number=errno.ENOTDIR, message=f"[Errno 20] Not a directory: '{PORT_NODE}'")
    monkeypatch.setattr(device_access, "HOST", a_stat_that_fails(tmp_path, broken))

    report = doctor()

    assert report["ok"] is True, report
    assert report["unhealthy"] == [], report["unhealthy"]
    port = report["com_ports"]["dut_uart"]["device_access"]
    assert (port["ok"], port["status"], port["errno"]) == (True, "undetermined", "ENOTDIR")
    assert flat(broken.message) in flat(render_result(report, "doctor")), port


# ---------------------------------------------------------------------------
# Where the check has nothing to say.


def test_a_probe_this_host_does_not_have_is_not_checked(tmp_path: Path) -> None:
    """A serial nothing on this bus carries is not a probe on USB here; `doctor` says what it said before."""
    host = recorded_host(tmp_path, ON_THE_HOST)

    assert device_access.probe_access("0670FF000000000000000000", host=host) is None


def test_a_bus_with_no_probe_on_it_is_not_checked(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, ON_THE_HOST)
    host.usb_devices = tmp_path / "no-sysfs-here"

    assert device_access.probe_access(PROBE_SERIAL, host=host) is None
    assert device_access.probe_access(None, host=host) is None


@pytest.mark.parametrize("device", [None, "", "COM7", "ttyACM0", "/dev/ttyACM9"])
def test_a_port_that_names_no_node_on_this_host_is_not_checked(tmp_path: Path, device: str | None) -> None:
    host = recorded_host(tmp_path, ON_THE_HOST)

    assert device_access.port_access(device, host=host) is None


def test_a_host_that_is_not_asked_answers_nothing(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, WITHHOLDING_THE_GROUPS)
    host.asks = lambda: False  # type: ignore[method-assign]

    assert device_access.probe_access(PROBE_SERIAL, host=host) is None
    assert device_access.port_access(BY_ID_PATH, host=host) is None


def test_windows_is_not_asked() -> None:
    """On Windows a device is not a node with a mode, and the backends' own refusals are the answer there."""
    assert device_access.LocalHost().asks() is (os.name != "nt")


def test_the_suite_never_asks_the_machine_it_runs_on() -> None:
    """A developer's own groups and devices would decide the unit tier's `doctor` otherwise."""
    assert device_access.HOST.asks() is False


# ---------------------------------------------------------------------------
# What this machine itself reads. Every place above replays a recorded host, so
# nothing exercised `LocalHost` beyond `asks()`: a reader narrowed to R_OK, or
# `os.getgrouplist` called with its arguments the wrong way round, failed
# nothing. These ask the real readers about paths under `tmp_path`, which are
# files rather than device nodes, so no device is touched and no group of the
# account running the suite decides an assertion.


@POSIX_ONLY
def test_a_device_name_the_host_cannot_look_up_at_all_is_not_checked_and_raises_nothing() -> None:
    """A configured device name with an embedded NUL: every reader refuses it, and `doctor` asks them of it.

    `os.path.realpath`, `os.stat` and `os.access` all raise ValueError rather than
    OSError on such a name, and `doctor` catches neither, so an unguarded reader
    took the command down on a traceback where it used to report the string. The
    node has no other spelling to find, so the answer is the one for a name this
    host does not have."""
    name = "/dev/tty\0ACM0"
    host = AskedLocalHost()

    assert host.realpath(name) == name
    assert host.status(name) is None
    assert host.may_open(name) is False
    assert device_access.port_access(name, host=host) is None


@POSIX_ONLY
def test_this_machine_reads_a_nodes_mode_owner_and_group_the_way_os_stat_does(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    node.chmod(0o640)
    found = os.stat(node)

    status = AskedLocalHost().status(str(node))

    assert isinstance(status, device_access.NodeStatus)
    assert (status.mode, status.uid, status.gid) == (found.st_mode, found.st_uid, found.st_gid)
    assert stat.filemode(status.mode) == "-rw-r-----"


@POSIX_ONLY
def test_this_machine_says_nothing_about_a_node_that_is_not_there(tmp_path: Path) -> None:
    """The one silence the check keeps: a configured port whose node this host does not have."""
    assert AskedLocalHost().status(str(tmp_path / "ttyACM9")) is None


@POSIX_ONLY
def test_this_machine_follows_a_link_to_its_node_and_leaves_a_dangling_one_named(tmp_path: Path) -> None:
    """A /dev/serial/by-id link is followed to the tty it names, and an unplugged one resolves to the name it points at.

    Not strictly: a link whose target is gone has to come back as the target's
    own path rather than raise, because that is a port the board is simply not
    plugged into and the check answers nothing for it."""
    root = Path(os.path.realpath(tmp_path))
    node = tmp_path / "ttyACM0"
    node.write_text("", encoding="utf-8")
    (tmp_path / "by-id").symlink_to(node)
    (tmp_path / "by-id-unplugged").symlink_to(tmp_path / "ttyACM1")
    host = AskedLocalHost()

    assert host.realpath(str(tmp_path / "by-id")) == str(root / "ttyACM0")
    assert host.realpath(str(tmp_path / "by-id-unplugged")) == str(root / "ttyACM1")
    assert host.realpath(str(node)) == str(root / "ttyACM0")


@NOT_ROOT_ONLY
def test_this_machine_asks_the_kernel_for_reading_and_writing_together(tmp_path: Path) -> None:
    """Both rights, not either: a serial port a session may read and not write is one it cannot run a plan over."""
    modes = {0o600: True, 0o400: False, 0o200: False, 0o000: False}
    host = AskedLocalHost()

    for mode, answer in modes.items():
        node = tmp_path / f"mode-{mode:03o}"
        node.write_text("", encoding="utf-8")
        node.chmod(mode)
        assert host.may_open(str(node)) is answer, oct(mode)


@POSIX_ONLY
def test_this_machine_reads_this_logins_groups_and_the_accounts_own_separately(tmp_path: Path) -> None:
    """The two sets the check's verdict parts on: what this process holds, and what the database says now."""
    import pwd

    host = AskedLocalHost()

    login = host.login_groups()
    account = host.account_groups()

    assert os.getgid() in login and os.getegid() in login
    assert set(os.getgroups()) <= login
    try:
        entry = pwd.getpwuid(os.getuid())
    except KeyError:
        # A uid the account database does not name (`docker run -u 4242`): the
        # reader says so with None rather than an empty set, which would read as
        # an account in no group at all.
        assert account is None
        return
    assert account is not None
    assert entry.pw_gid in account
    assert account == frozenset(os.getgrouplist(entry.pw_name, entry.pw_gid))


@POSIX_ONLY
def test_this_machine_names_the_owner_and_the_group_behind_a_number(tmp_path: Path) -> None:
    import grp
    import pwd

    host = AskedLocalHost()

    assert host.group_name(os.getgid()) == _database_name(lambda: grp.getgrgid(os.getgid()).gr_name)
    assert host.user_name(os.getuid()) == _database_name(lambda: pwd.getpwuid(os.getuid()).pw_name)
    unnamed = next((gid for gid in range(4200000, 4200064) if _database_name(lambda gid=gid: grp.getgrgid(gid).gr_name) is None), None)
    assert unnamed is not None, "no unnamed gid in the probed range, so the fallback to the number cannot be asked for"
    # None, so the check falls back to the number rather than printing "None" as a group.
    assert host.group_name(unnamed) is None


# ---------------------------------------------------------------------------
# `doctor`.


def a_bench_bound_to_the_recorded_probe_and_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        probe_id=PROBE_SERIAL,
        com_ports_yaml=f'com_ports:\n  dut_uart:\n    device: "{BY_ID_PATH}"\n    baudrate: 115200\n',
    )
    monkeypatch.chdir(workspace)


def test_doctor_fails_the_check_for_a_probe_and_a_port_this_account_may_not_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)
    monkeypatch.setattr(device_access, "HOST", recorded_host(tmp_path, WITHHOLDING_THE_GROUPS))

    report = doctor()

    assert report["ok"] is False, report
    assert report["unhealthy"] == [DOCTOR_DEVICE_ACCESS_FINDING], report["unhealthy"]
    probe = report["debuggers"]["dut"]["device_access"]
    port = report["com_ports"]["dut_uart"]["device_access"]
    assert (probe["ok"], probe["node"]) == (False, PROBE_NODE)
    assert (port["ok"], port["node"], port["link"]) == (False, PORT_NODE, BY_ID_PATH)
    # In the headline, where a caller that keeps only `summary` reads it.
    assert probe["summary"] in report["summary"]
    assert port["summary"] in report["summary"]


def test_doctor_does_not_attach_a_neighbouring_st_links_verdict_to_a_pyocd_debugger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench whose only hardware-driving debugger is pyOCD, on a workstation with an unrelated Nucleo plugged in.

    The entry pins no serial, so the sole attached ST-Link was checked and its
    verdict attached to it. `doctor` then exited non-zero over a node the
    configuration never touches, and `init` and `setup` repeated the same headline
    as a warning. The port beside it is the bench's own and still answers, so this
    is the probe half going quiet rather than the check going away.
    """
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_type="pyocd",
        com_ports_yaml=f'com_ports:\n  dut_uart:\n    device: "{BY_ID_PATH}"\n    baudrate: 115200\n',
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(device_access, "HOST", in_a_rootless_namespace(tmp_path, WITHHOLDING_THE_GROUPS))

    report = doctor()

    assert "device_access" not in report["debuggers"]["dut"], report["debuggers"]["dut"]
    assert PROBE_NODE not in report["summary"], report["summary"]
    # The bench's own port is named by the configuration, so it is still checked.
    port = report["com_ports"]["dut_uart"]["device_access"]
    assert (port["ok"], port["node"]) == (False, PORT_NODE), port
    assert report["unhealthy"] == [DOCTOR_DEVICE_ACCESS_FINDING], report["unhealthy"]


def test_doctor_stays_green_where_this_account_may_open_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)
    monkeypatch.setattr(device_access, "HOST", recorded_host(tmp_path, ON_THE_HOST))

    report = doctor()

    assert report["ok"] is True, report
    assert report["unhealthy"] == []
    assert report["debuggers"]["dut"]["device_access"]["ok"] is True
    assert report["com_ports"]["dut_uart"]["device_access"]["ok"] is True
    assert "may not open" not in report["summary"]


def test_doctor_adds_no_entry_where_nothing_was_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows, and the unit tier: the report is the one it was before the check existed."""
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)

    report = doctor()

    assert report["ok"] is True, report
    assert "device_access" not in report["debuggers"]["dut"]
    assert "device_access" not in report["com_ports"]["dut_uart"]


def test_init_and_setup_keep_the_file_over_the_finding() -> None:
    """The finding is about this account on this machine and says nothing about the document."""
    assert DOCTOR_DEVICE_ACCESS_FINDING in DOCTOR_FINDINGS_SETUP_KEEPS
    assert doctor_findings_setup_keeps({"ok": False, "unhealthy": [DOCTOR_DEVICE_ACCESS_FINDING]}) is True


def test_the_report_renders_each_check_with_its_verdict_and_remediation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)
    monkeypatch.setattr(device_access, "HOST", recorded_host(tmp_path, WITHHOLDING_THE_GROUPS))
    report = doctor()

    text = render_result(report, "doctor")

    lines = text.splitlines()
    rows = [line for line in lines if re.match(r"\s+device_access\s+FAILED\s", line)]
    assert len(rows) == 2, text
    assert text.count(device_access.DEVICE_ACCESS_DENIED) >= 2, text
    flattened = flat(text)
    for check in (report["debuggers"]["dut"]["device_access"], report["com_ports"]["dut_uart"]["device_access"]):
        assert flat(check["summary"]) in flattened
        assert flat(check["remediation"][0]) in flattened


# ---------------------------------------------------------------------------
# `init`.


def the_recorded_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The starter project on the recorded bench: OpenOCD on PATH, the recorded serial inventory, no spawn."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)

    def inventory(tool: str = "com_ports_available") -> JsonObject:
        return {**copy.deepcopy(RECORDED_INVENTORY), "tool": tool}

    def nothing_spawned(command: list[str], cwd: str, timeout_s: float) -> object:
        raise AssertionError(f"nothing should have been spawned: {command}")

    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: str(FAKE_OPENOCD))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", nothing_spawned)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", inventory)
    monkeypatch.setattr("agentic_hil.cli.list_available_com_ports", inventory)
    return workspace


def test_init_binds_the_probe_and_the_port_and_warns_it_may_not_open_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = the_recorded_bench(tmp_path, monkeypatch)
    monkeypatch.setattr(device_access, "HOST", recorded_host(tmp_path, WITHHOLDING_THE_GROUPS))

    result = cli.init_project()

    assert result["ok"] is True, result
    written = load_authoritative_config(workspace)
    assert written.debuggers["dut"].probe_id == PROBE_SERIAL
    assert written.com_ports["dut_uart"].device == BY_ID_PATH
    step = result["steps"]["doctor"]
    assert step["unhealthy"] == [DOCTOR_DEVICE_ACCESS_FINDING], step["unhealthy"]
    probe = step["debuggers"]["dut"]["device_access"]
    port = step["com_ports"]["dut_uart"]["device_access"]
    assert probe["summary"] in result["warnings"]
    assert port["summary"] in result["warnings"]


def test_init_says_nothing_where_this_account_may_open_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    the_recorded_bench(tmp_path, monkeypatch)
    monkeypatch.setattr(device_access, "HOST", recorded_host(tmp_path, ON_THE_HOST))

    result = cli.init_project()

    assert result["ok"] is True, result
    assert result["steps"]["doctor"]["unhealthy"] == []
    assert not any("may not open" in warning for warning in result.get("warnings", []))


def test_the_recorded_modes_are_character_devices() -> None:
    """The replay's premise: every recorded node is a character device, so the check reads each one."""
    for place in RECORDING.values():
        for node in place["nodes"]:
            assert stat.S_ISCHR(node["st_mode"]), node
