"""Whether this account may open the configured probe and serial port, asked without opening either.

On Linux an account that cannot open the probe's USB device or the serial port
used to learn it from the first hardware call that failed (#604 made that
failure name the permission refusal). `doctor` stayed green, because it opens no
device, and `init` bound a probe and a port this account could not open without
a word. This module is the question both now ask first.

It asks the kernel with `os.access(R_OK | W_OK)` on the node, which applies the
node's mode, its group and its ACL to this process's credentials and opens
nothing, and it reads `os.stat` for the owner, the group and the mode it names.
The probe's node is found the way tools/bench_in_container.py finds it: the USB
devices under /sys/bus/usb/devices with the vendor and product ids the product
recognises as an ST-Link, their `busnum` and `devnum`, and /dev/bus/usb/BBB/DDD,
narrowed by the configured probe serial when there is one. A serial port is
checked at its real node, so a /dev/serial/by-id link is followed to the tty it
names.

Where there is nothing to check the answer is None, never a failure: Windows,
where a device is not a node with a mode; the probe half anywhere but Linux,
where the sysfs tree it reads the USB identities out of does not exist; a probe
not found on USB here, which stays what `doctor` said before; several probes and
no serial to choose between them; a debugger entry with no `probe_id` whose
backend does not open an ST-Link, which is the one probe this module can
recognise; and a port whose node this host does not have, which the port's own
checks already cover.

The serial port half answers everywhere off Windows, because a mode, an owner, a
group and `os.access` are the same instruments on macOS as on Linux. What is
Linux-shaped is the *advice*: udev creates the node and a udev rule is what gives
it to a group, so the causes that name one are given on Linux only and every
other platform gets the same verdict with the group step alone.
"""

from __future__ import annotations

import errno
import os
import posixpath
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentic_hil.comports import STLINK_USB_PRODUCT_IDS, STLINK_USB_VENDOR_ID
from agentic_hil.knowledge import remediation_fields
from agentic_hil.types import JsonObject, fold_hardware_id

DEVICE_ACCESS_TOOL = "device_access"
DEVICE_ACCESS_DENIED = "device_access_denied"
# The catalogue scopes, one per kind of node: the remedy for a probe and for a
# serial port name different udev rules, groups and troubleshooting sections.
PROBE_SCOPE = "probe"
COM_PORT_SCOPE = "com_port"

SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")
USB_NODE_ROOT = "/dev/bus/usb"
TROUBLESHOOTING_SECTION = {PROBE_SCOPE: 6, COM_PORT_SCOPE: 11}
GROUP_READ_WRITE = stat.S_IRGRP | stat.S_IWGRP
# Where this process's user namespace publishes the group ids it has a mapping
# for. Absent outside Linux and outside a namespace, and a plain host's map
# covers the whole range, so the one place it says anything is a rootless
# container: see `_cause`'s `group_not_mapped`.
PROC_GID_MAP = Path("/proc/self/gid_map")
# The group id the kernel reports in place of an owner this namespace has no
# mapping for. Read rather than assumed to be 65534, which is only the usual
# build-time default, and read beside the map because the substitute number can
# itself be inside a mapped range: see `gid_is_mapped_by`.
PROC_OVERFLOW_GID = Path("/proc/sys/kernel/overflowgid")
# How many group ids there are, 0 to 4294967294: 4294967295 is `(gid_t) -1`,
# which is no group. A map that covers all of them is a namespace in which no
# owner can be unmapped.
GID_SPACE = 2**32 - 1
# The errnos that are a refusal of the node rather than a question that could not
# be asked. A directory on the way to the node this account may not search, or a
# security module that denies the node, refuses `os.stat` before any mode can be
# read, and a stat this account may not make is a node it may not open.
STAT_REFUSED_ERRNOS = frozenset({errno.EACCES, errno.EPERM})


@dataclass(frozen=True)
class NodeStatus:
    """What `os.stat` says about a device node: its mode, its owner and its group."""

    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class NodeFailure:
    """Why `os.stat` could not say anything about a node: the errno named, numbered and in the kernel's own words.

    A node that is absent is not one of these, and is the one silence this check
    keeps: a configured port whose node this host does not have is what the
    port's own checks report. Every other errno is a question that could not be
    asked, and EACCES or EPERM on the stat itself is a refusal in its own right,
    so neither may become the same nothing as an absent node: a stat this account
    may not make is a node it may not open.
    """

    name: str
    number: int | None
    message: str


def _failure(error: OSError) -> NodeFailure:
    """An `OSError` from a node, with the errno's own name where the platform has one."""
    number = error.errno if isinstance(error.errno, int) else None
    name = errno.errorcode.get(number, "") if number is not None else ""
    return NodeFailure(name=name or type(error).__name__, number=number, message=str(error))


def _proc_text(path: Path) -> str | None:
    """What a `/proc` file says, or None where this platform has no such file to say it."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def gid_is_mapped_by(gid: int, gid_map: str | None, overflow_gid: str | None) -> bool:
    """Whether a user namespace with this map has a group of its own for this group id.

    True wherever nothing says otherwise, which is every platform with no
    `/proc/self/gid_map` and every map this reader cannot parse: claiming a group
    is outside a namespace is a claim, and an unread file is not evidence for it.

    Each map line is `inside outside count`, and `inside` is the spelling every
    other reader here sees, because `os.stat` reports a node's group in this
    namespace's own numbering. Two shapes are an owner the namespace cannot name:

    * a gid no line of the map covers, and
    * the overflow gid, which is what the kernel reports in place of a mapping
      for an owner it has none for. It is reported whether or not that number is
      itself inside some mapped range, and in rootless Podman's map it is: the
      bench's recorded container maps inside-gids 1 to 65536, so the 65534 the
      kernel shows for a host-root-owned node falls inside the map while naming
      no group that can be joined. Asking only whether a line covers the number
      answered that container with "an administrator adds this account to
      nogroup", the one remedy that cannot work there.

    The exception is a namespace that maps the whole id space: nothing in it can
    be unmapped, so the overflow number there is an ordinary group (`nogroup` on
    a plain host) and is read as one. That is also every host outside a namespace,
    whose map is the whole range in one line.
    """
    if gid_map is None:
        return True
    ranges: list[tuple[int, int]] = []
    for line in gid_map.splitlines():
        fields = line.split()
        if len(fields) != 3:
            return True
        try:
            ranges.append((int(fields[0]), int(fields[2])))
        except ValueError:
            return True
    if not ranges:
        return True
    if not any(inside <= gid < inside + count for inside, count in ranges):
        return False
    overflow = _as_gid(overflow_gid)
    if overflow is None or gid != overflow:
        return True
    # The number is the overflow id: a group of its own only where nothing in
    # this namespace can be unmapped, which is a map with no gap in it.
    return _covers_every_gid(ranges)


def _as_gid(text: str | None) -> int | None:
    """One group id a `/proc` file holds, or None where it holds anything else."""
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _covers_every_gid(ranges: list[tuple[int, int]]) -> bool:
    """Whether these `(inside, count)` ranges together leave no group id unmapped."""
    reach = 0
    for inside, count in sorted(ranges):
        if inside > reach:
            return False
        reach = max(reach, inside + count)
    return reach >= GID_SPACE


class Host(Protocol):
    """Everything the check reads off the machine it runs on."""

    usb_devices: Path

    def asks(self) -> bool: ...

    def realpath(self, path: str) -> str: ...

    def status(self, path: str) -> NodeStatus | NodeFailure | None: ...

    def may_open(self, path: str) -> bool: ...

    def gid_is_mapped(self, gid: int) -> bool: ...

    def login_groups(self) -> frozenset[int]: ...

    def account_groups(self) -> frozenset[int] | None: ...

    def group_name(self, gid: int) -> str | None: ...

    def user_name(self, uid: int) -> str | None: ...


class LocalHost:
    """This machine, this process and the account database it reads."""

    usb_devices = SYSFS_USB_DEVICES

    def asks(self) -> bool:
        return os.name != "nt"

    def realpath(self, path: str) -> str:
        try:
            return os.path.realpath(path)
        except ValueError:
            # A name the host cannot look up at all (an embedded NUL) has no
            # other spelling to find, the way `devices._host_device_name` reads
            # it. Every reader below refuses such a name with ValueError rather
            # than OSError, and the commands that ask them catch neither.
            return path

    def status(self, path: str) -> NodeStatus | NodeFailure | None:
        """What the node is, why it could not be read, or None where there is no such node.

        The three answers are three different reports, and folding the middle one
        into the last is what made a refusal of the node silent: `doctor` printed
        no `device_access` entry and no sentence at all for a node whose
        attributes this account may not even read, which is exactly the refusal
        this module exists to give. Only an absent node is the silence, because
        that is the one case another check already covers."""
        try:
            found = os.stat(path)
        except FileNotFoundError:
            return None
        except OSError as error:
            return _failure(error)
        except ValueError:
            # A name with an embedded NUL, which the reader below refuses the
            # same way: there is no such node and no other spelling to find.
            return None
        return NodeStatus(mode=found.st_mode, uid=found.st_uid, gid=found.st_gid)

    def may_open(self, path: str) -> bool:
        try:
            return os.access(path, os.R_OK | os.W_OK)
        except ValueError:
            return False

    def gid_is_mapped(self, gid: int) -> bool:
        """Whether this process's user namespace has a group of its own for this group.

        The two files this rests on, each read with the same tolerance: a file
        that cannot be read says nothing, and `gid_is_mapped_by` answers True
        wherever nothing says otherwise."""
        return gid_is_mapped_by(gid, _proc_text(PROC_GID_MAP), _proc_text(PROC_OVERFLOW_GID))

    def login_groups(self) -> frozenset[int]:
        """The groups this process holds: the ones this login began with."""
        return frozenset({os.getgid(), os.getegid(), *os.getgroups()})

    def account_groups(self) -> frozenset[int] | None:
        """The groups the account database gives this account now, or None where it cannot be read."""
        try:
            import pwd

            account = pwd.getpwuid(os.getuid())
            return frozenset(os.getgrouplist(account.pw_name, account.pw_gid))
        except (ImportError, KeyError, OSError):
            return None

    def group_name(self, gid: int) -> str | None:
        try:
            import grp

            return grp.getgrgid(gid).gr_name
        except (ImportError, KeyError):
            return None

    def user_name(self, uid: int) -> str | None:
        try:
            import pwd

            return pwd.getpwuid(uid).pw_name
        except (ImportError, KeyError):
            return None


# Read at call time, so a test hands the check a recorded host by replacing it.
HOST: Host = LocalHost()


def _attribute(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _number(path: Path, base: int) -> int | None:
    text = _attribute(path)
    try:
        return int(text, base) if text else None
    except ValueError:
        return None


@dataclass(frozen=True)
class UsbProbe:
    node: str
    serial: str | None


def attached_probes(host: Host) -> list[UsbProbe]:
    """Every ST-Link on this host's USB, with the node libusb opens it through."""
    try:
        devices = sorted(host.usb_devices.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    probes: list[UsbProbe] = []
    for device in devices:
        if _number(device / "idVendor", 16) != STLINK_USB_VENDOR_ID:
            continue
        if _number(device / "idProduct", 16) not in STLINK_USB_PRODUCT_IDS:
            continue
        bus = _number(device / "busnum", 10)
        number = _number(device / "devnum", 10)
        if bus is None or number is None:
            continue
        probes.append(UsbProbe(node=f"{USB_NODE_ROOT}/{bus:03d}/{number:03d}", serial=_attribute(device / "serial")))
    return probes


def probe_access(probe_id: str | None, host: Host | None = None, *, opens_an_stlink: bool = True) -> JsonObject | None:
    """The check for the configured probe's USB node, or None where there is nothing to check.

    With a `probe_id` the probe carrying that serial is the one checked, whatever
    backend asked: a serial that matches one of the ST-Links on this USB names
    that ST-Link, and nothing else has to be decided.

    Without one, the only attached probe is, and several are not guessed between.
    `opens_an_stlink` is what makes that sound, because `attached_probes`
    recognises the ST-Link USB identity and no other: an entry whose backend
    opens a CMSIS-DAP probe has no `probe_id` to narrow by and no probe this
    module can find, so a lone unrelated ST-Link plugged into the same
    workstation became its verdict. `doctor` then exited non-zero over a
    `/dev/bus/usb/...` node the configuration never touches, `init` and `setup`
    repeated it as a warning, and in a multi-debugger file the one probe's verdict
    was copied onto every entry that pinned no serial. So an entry that cannot be
    shown to open an ST-Link answers nothing here, which is what this module
    already does everywhere else it cannot tell.
    """
    host = HOST if host is None else host
    if not host.asks():
        return None
    if not probe_id and not opens_an_stlink:
        return None
    probes = attached_probes(host)
    if probe_id:
        wanted = fold_hardware_id(probe_id)
        probes = [probe for probe in probes if probe.serial is not None and fold_hardware_id(probe.serial) == wanted]
    if len(probes) != 1:
        return None
    node = probes[0].node
    return _verdict(host, node, None, PROBE_SCOPE, f"the probe's USB device {node}")


def backend_opens_an_stlink(debugger_type: str, interface_cfg: str) -> bool:
    """Whether a debugger entry's backend opens an ST-Link, the one probe this module can recognise.

    Asked so `probe_access` knows whether an entry with no `probe_id` has a probe
    here at all. `attached_probes` reads the ST-Link USB identity and nothing
    else, so this is the difference between the one attached probe being the
    entry's own and it being an unrelated board on the same workstation.

    STM32CubeProgrammer has no other transport, so `stlink` is always one. An
    OpenOCD entry is one exactly when its `interface_cfg` names the ST-Link
    family, which is the same reading the OpenOCD backend already does to decide
    whether it can enumerate probes without OpenOCD's help. A pyOCD entry is not:
    pyOCD opens ST-Links and CMSIS-DAP probes alike and the configuration does not
    say which, so an entry that pins no serial says nothing this module can act
    on, and the honest answer is to check nothing rather than to check a probe the
    entry may have no connection to. A `probe_id` settles it either way, and then
    none of this is asked.
    """
    if debugger_type == "stlink":
        return True
    if debugger_type == "openocd":
        from agentic_hil.backends.openocd import openocd_interface_enumerates_by_usb

        return openocd_interface_enumerates_by_usb(interface_cfg)
    return False


def port_access(device: str | None, host: Host | None = None) -> JsonObject | None:
    """The check for a configured serial port's node, or None where there is nothing to check."""
    host = HOST if host is None else host
    if not host.asks() or not device or not posixpath.isabs(device):
        return None
    node = host.realpath(device)
    link = device if node != device else None
    what = f"the serial port {node}" + (f" (the node behind {link})" if link else "")
    return _verdict(host, node, link, COM_PORT_SCOPE, what)


def _verdict(host: Host, node: str, link: str | None, scope: str, what: str) -> JsonObject | None:
    status = host.status(node)
    if isinstance(status, NodeFailure):
        return _unreadable(status, node, link, scope, what)
    if status is None or not stat.S_ISCHR(status.mode):
        return None
    mode = stat.filemode(status.mode)
    owner = host.user_name(status.uid) or str(status.uid)
    group = host.group_name(status.gid) or str(status.gid)
    facts: JsonObject = {
        "tool": DEVICE_ACCESS_TOOL,
        "node": node,
        **({"link": link} if link else {}),
        "mode": mode,
        "owner": owner,
        "group": group,
    }
    if host.may_open(node):
        return {"ok": True, **facts, "summary": f"This account may open {what} for reading and writing ({mode} {owner}:{group})."}
    account = host.account_groups()
    account_in_group = None if account is None else status.gid in account
    login_in_group = status.gid in host.login_groups()
    cause = _cause(status, account_in_group, login_in_group, host.gid_is_mapped(status.gid), host.group_name(status.gid))
    return {
        "ok": False,
        **facts,
        "error_type": DEVICE_ACCESS_DENIED,
        "cause": cause,
        "account_in_group": account_in_group,
        "login_in_group": login_in_group,
        "summary": (
            f"This account may not open {what} for reading and writing: it is {mode} {owner}:{group}, and "
            f"{_why(cause, scope, node, group)}. TROUBLESHOOTING.md section {TROUBLESHOOTING_SECTION[scope]} is the rest of it."
        ),
        **remediation_fields(DEVICE_ACCESS_DENIED, scope),
    }


def _unreadable(failure: NodeFailure, node: str, link: str | None, scope: str, what: str) -> JsonObject:
    """What to answer about a node `os.stat` could say nothing about at all.

    Two answers, and neither of them is the silence an absent node gets. EACCES
    or EPERM on the stat itself is a refusal of this very node: a directory on the
    way to it this account may not search, or a security module that denies it,
    and a node whose attributes may not be read may certainly not be opened, so
    the verdict is the same denial as a mode that shuts this account out, with the
    same remedies. Every other errno is a question that could not be asked, which
    is not a failure of the bench and does not make `doctor` red, and is still
    reported rather than dropped: the errno and the kernel's own words for it are
    the whole evidence, and removing them is how a real refusal became a report
    with no sentence in it anywhere.
    """
    facts: JsonObject = {
        "tool": DEVICE_ACCESS_TOOL,
        "node": node,
        **({"link": link} if link else {}),
        "errno": failure.name,
    }
    if failure.number in STAT_REFUSED_ERRNOS:
        return {
            "ok": False,
            **facts,
            "error_type": DEVICE_ACCESS_DENIED,
            "cause": "stat_refused",
            "summary": (
                f"This account may not even read the attributes of {what}, so it may not open it either: "
                f"{failure.message} ({failure.name}). A directory on the way to the node this account may not "
                f"search, or a security module that denies the node, refuses this before any mode can be read. "
                f"TROUBLESHOOTING.md section {TROUBLESHOOTING_SECTION[scope]} is the rest of it."
            ),
            **remediation_fields(DEVICE_ACCESS_DENIED, scope),
        }
    return {
        "ok": True,
        **facts,
        "status": "undetermined",
        "summary": (
            f"Whether this account may open {what} could not be determined: reading its attributes failed with "
            f"{failure.message} ({failure.name}). Nothing was refused, so nothing here is failed, and the errno "
            "is what says why the question could not be asked."
        ),
    }


def _cause(status: NodeStatus, account_in_group: bool | None, login_in_group: bool, gid_is_mapped: bool, group: str | None) -> str:
    """Which of the ways a node refuses this account this one is, most specific first."""
    if status.uid == 0 and status.gid == 0 and _udev_creates_device_nodes() and group == "root":
        # udev leaves a device no rule applies to as root:root, and the group
        # named `root` is the whole of that claim. gid 0 is `wheel` on macOS,
        # where there is no udev at all and the sentence would contradict itself.
        return "no_udev_rule"
    if status.mode & GROUP_READ_WRITE != GROUP_READ_WRITE:
        return "mode"
    if login_in_group:
        return "denied_otherwise"
    if not gid_is_mapped:
        # A group this process's user namespace has no group of its own for, in
        # either of the two shapes `gid_is_mapped_by` reads: a gid no line of the
        # map covers, or the overflow gid the kernel reports in place of an owner
        # it cannot name, which is what a host group outside the map shows up as
        # in a rootless container. Adding the account to it changes nothing,
        # because there is no such group here to be added to; the fix is mapping
        # the gid in.
        return "group_not_mapped"
    if account_in_group is None:
        # The account database could not be read at all (a uid with no passwd
        # entry: `docker run -u 4242`, an arbitrary uid under OpenShift). Falling
        # through from here claimed the account is not in the group, which is a
        # positive statement about a database nothing read, and this is precisely
        # the case where "log in again" against "ask an administrator" is the
        # decision this check exists to make.
        return "account_unknown"
    if account_in_group:
        return "not_in_this_login"
    return "not_in_group"


def _udev_creates_device_nodes() -> bool:
    """Whether device nodes on this platform are udev's, which is what makes a udev remedy a remedy.

    Read at call time rather than folded into a constant so a test can state the
    platform it is asking about. Linux only: macOS has devfs and `IOKit`, Windows
    has no node at all, and a sentence about a udev rule on either is advice that
    cannot be carried out."""
    return sys.platform == "linux"


def _why(cause: str, scope: str, node: str, group: str) -> str:
    device, rule = (
        ("the probe", "the probe's udev rule (OpenOCD's 60-openocd.rules or ST's own)")
        if scope == PROBE_SCOPE
        else ("the adapter", "a udev rule for the adapter")
    )
    # What created the node, named only where it is true. Off Linux the mode came
    # from the platform's own device filesystem and naming udev would send an
    # operator looking for a rule file the system has no concept of.
    created_it = " which the udev rule that created it set" if _udev_creates_device_nodes() else ""
    return {
        "no_udev_rule": (
            "root:root is how udev leaves a device no rule applies to, so no udev rule gives it to a group: "
            f"install {rule}, replug {device}, add this account to the group the rule names and log in again"
        ),
        "mode": f"its mode gives the group {group} no read and write access,{created_it or ' which only an administrator changes'}",
        "denied_otherwise": (
            f"this login is in the group {group} and the kernel still refuses, so an ACL or a security module "
            f"({'AppArmor, SELinux' if _udev_creates_device_nodes() else 'this platform has its own'}) denies it: "
            f"`{'getfacl' if _udev_creates_device_nodes() else 'ls -le'} {node}` shows the ACL"
        ),
        "group_not_mapped": (
            f"the group {group} is not mapped into this user namespace, so it is the group every unmapped owner "
            "shows up as and not a group anything here can join: map the owning group's id into the namespace "
            "(podman's `--group-add keep-groups`, or a `--gidmap` that covers it) and run this again"
        ),
        "account_unknown": (
            f"this account has no entry in the account database, so whether it is in the group {group} cannot be "
            f"read here: if it has already been added to {group}, log in again (a new SSH session or desktop "
            f"login); if it has not, an administrator adds it to {group} once, and it logs in again"
        ),
        "not_in_this_login": (
            f"this account is in the group {group} in the account database and not in this login's groups, so it "
            "joined after this login began: log in again (a new SSH session or desktop login) and nothing else has to change"
        ),
        "not_in_group": f"this account is not in the group {group}: an administrator adds it to {group} once, and it logs in again",
    }[cause]
