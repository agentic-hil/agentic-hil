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
where a device is not a node with a mode; a probe not found on USB here, which
stays what `doctor` said before; several probes and no serial to choose between
them; and a port whose node this host does not have, which the port's own
checks already cover.
"""

from __future__ import annotations

import os
import posixpath
import stat
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


@dataclass(frozen=True)
class NodeStatus:
    """What `os.stat` says about a device node: its mode, its owner and its group."""

    mode: int
    uid: int
    gid: int


class Host(Protocol):
    """Everything the check reads off the machine it runs on."""

    usb_devices: Path

    def asks(self) -> bool: ...

    def realpath(self, path: str) -> str: ...

    def status(self, path: str) -> NodeStatus | None: ...

    def may_open(self, path: str) -> bool: ...

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
        return os.path.realpath(path)

    def status(self, path: str) -> NodeStatus | None:
        try:
            found = os.stat(path)
        except OSError:
            return None
        return NodeStatus(mode=found.st_mode, uid=found.st_uid, gid=found.st_gid)

    def may_open(self, path: str) -> bool:
        return os.access(path, os.R_OK | os.W_OK)

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


def probe_access(probe_id: str | None, host: Host | None = None) -> JsonObject | None:
    """The check for the configured probe's USB node, or None where there is nothing to check.

    With a `probe_id` the probe carrying that serial is the one checked; without
    one, the only attached probe is, and several are not guessed between.
    """
    host = HOST if host is None else host
    if not host.asks():
        return None
    probes = attached_probes(host)
    if probe_id:
        wanted = fold_hardware_id(probe_id)
        probes = [probe for probe in probes if probe.serial is not None and fold_hardware_id(probe.serial) == wanted]
    if len(probes) != 1:
        return None
    node = probes[0].node
    return _verdict(host, node, None, PROBE_SCOPE, f"the probe's USB device {node}")


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
    cause = _cause(status, account_in_group, login_in_group)
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


def _cause(status: NodeStatus, account_in_group: bool | None, login_in_group: bool) -> str:
    """Which of the ways a node refuses this account this one is, most specific first."""
    if status.uid == 0 and status.gid == 0:
        return "no_udev_rule"
    if status.mode & GROUP_READ_WRITE != GROUP_READ_WRITE:
        return "mode"
    if login_in_group:
        return "denied_otherwise"
    if account_in_group:
        return "not_in_this_login"
    return "not_in_group"


def _why(cause: str, scope: str, node: str, group: str) -> str:
    device, rule = (
        ("the probe", "the probe's udev rule (OpenOCD's 60-openocd.rules or ST's own)")
        if scope == PROBE_SCOPE
        else ("the adapter", "a udev rule for the adapter")
    )
    return {
        "no_udev_rule": (
            "root:root is how udev leaves a device no rule applies to, so no udev rule gives it to a group: "
            f"install {rule}, replug {device}, add this account to the group the rule names and log in again"
        ),
        "mode": f"its mode gives the group {group} no read and write access, which the udev rule that created it set",
        "denied_otherwise": (
            f"this login is in the group {group} and the kernel still refuses, so an ACL or a security module "
            f"(AppArmor, SELinux) denies it: `getfacl {node}` shows the ACL"
        ),
        "not_in_this_login": (
            f"this account is in the group {group} in the account database and not in this login's groups, so it "
            "joined after this login began: log in again (a new SSH session or desktop login) and nothing else has to change"
        ),
        "not_in_group": f"this account is not in the group {group}: an administrator adds it to {group} once, and it logs in again",
    }[cause]
