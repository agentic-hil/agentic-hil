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
import json
import os
import re
import stat
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


def flat(text: str) -> str:
    return " ".join(text.split())


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


def test_the_container_that_keeps_the_groups_may_open_both(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, KEEPING_THE_GROUPS)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)
    port = device_access.port_access(BY_ID_PATH, host=host)

    assert probe is not None and probe["ok"] is True, probe
    assert port is not None and port["ok"] is True, port


def test_the_container_that_withholds_the_groups_may_open_neither_and_says_which_group(tmp_path: Path) -> None:
    host = recorded_host(tmp_path, WITHHOLDING_THE_GROUPS)

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
        assert check["cause"] == "not_in_group"
        assert check["login_in_group"] is False
        assert check["account_in_group"] is False
        assert node in check["summary"] and "nogroup" in check["summary"]
        assert check["remediation"] == remediation_fields(device_access.DEVICE_ACCESS_DENIED, scope)["remediation"]
        assert check["do_not"] == remediation_fields(device_access.DEVICE_ACCESS_DENIED, scope)["do_not"]
    assert port is not None and port["link"] == BY_ID_PATH and BY_ID_PATH in port["summary"]


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


def test_a_probe_node_left_as_root_root_says_no_udev_rule_applies(tmp_path: Path) -> None:
    host = with_no_udev_rule(tmp_path)

    probe = device_access.probe_access(PROBE_SERIAL, host=host)

    assert probe is not None and probe["ok"] is False, probe
    assert probe["cause"] == "no_udev_rule"
    assert (probe["owner"], probe["group"], probe["mode"]) == ("root", "root", "crw-rw-r--")
    assert "no udev rule" in probe["summary"]


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
