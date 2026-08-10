"""`listen_only: true` reaches the driver, or the session is refused saying so.

The defect these tests exist for: the direct python-can path
built its bus without `listen_only`, so a bus an operator had declared
must-not-be-disturbed was joined by a node that sends dominant ACK bits — while
the shipped catalogue called the flag "the way to prove a reading did not touch
anything".

Passing the keyword through would not have fixed it. python-can 4.6.1 accepts a
``listen_only=`` argument on exactly three backends (slcan, vector, nixnet) and
neither ``pcan`` nor ``socketcan`` is one of them; on both, the keyword lands in
``**kwargs``, is forwarded to ``BusABC.__init__(**kwargs: object)``, and is
dropped without a word. So each supported adapter is held to the flag by its own
mechanism, and the tests are per adapter for that reason rather than for
symmetry.

python-can is faked throughout: none of this needs hardware, and none of it can
prove what real silicon does. What it proves is that the request reaches the
driver and that an unbacked claim refuses instead of listening anyway.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config

from agentic_hil.can import (
    CanBusService,
    open_process_adapter,
    open_python_can_adapter,
    socketcan_link_listen_only,
)
from agentic_hil.config import load_config
from agentic_hil.knowledge import (
    LISTEN_ONLY_MODE_ERROR,
    LISTEN_ONLY_UNCONFIRMED_ERROR,
    LISTEN_ONLY_UNSUPPORTED_ERROR,
)

PCAN_ERROR_OK = 0
PCAN_LISTEN_ONLY = 8
PCAN_PARAMETER_ON = 1
PCAN_PARAMETER_OFF = 0

FAKE_CAN_BRIDGE = Path(__file__).parent / "fixtures" / "fake_can_bridge.py"

# Verbatim from `ip -details -json link show dev vcan0` on iproute2 6.1.0
# (Debian bookworm), recorded in a container with NET_ADMIN. It is a literal on
# purpose: what the parser reads is an envelope nobody here can reproduce from
# the documentation, and the one thing worth noticing is that `link_type` says
# `can` for a *virtual* interface. Keying the check on it instead of on
# `linkinfo.info_kind` would accept a vcan as a CAN controller, which is the
# mistake this fixture exists to keep fixed. Note also that a vcan carries no
# `info_data` at all.
IP_VCAN0 = (
    '[{"ifindex":3,"ifname":"vcan0","flags":["NOARP","UP","LOWER_UP"],"mtu":2060,"qdisc":"noqueue",'
    '"operstate":"UNKNOWN","linkmode":"DEFAULT","group":"default","txqlen":1000,"link_type":"can",'
    '"promiscuity":0,"allmulti":0,"min_mtu":0,"max_mtu":0,"linkinfo":{"info_kind":"vcan"},'
    '"inet6_addr_gen_mode":"eui64","num_tx_queues":1,"num_rx_queues":1,"gso_max_size":65536}]'
)


def can_config(tmp_path: Path, adapter: str, channel: str, *, listen_only: bool, executable: str | None = None, allow_write: bool = True) -> object:
    lines = [
        "can_buses:\n",
        "  bench:\n",
        f'    adapter: "{adapter}"\n',
        f'    channel: "{channel}"\n',
        f"    listen_only: {'true' if listen_only else 'false'}\n",
    ]
    if executable is not None:
        lines.append(f'    executable: "{executable}"\n')
    permissions = {**DEFAULT_TEST_PERMISSIONS, "allow_can_write": allow_write}
    return load_config(str(write_config(tmp_path, can_buses_yaml="".join(lines), permissions=permissions)))


class FakeBusState:
    ACTIVE = "active"
    PASSIVE = "passive"


def fake_can_module(bus_factory) -> SimpleNamespace:
    return SimpleNamespace(Bus=bus_factory, BusState=FakeBusState, CanInitializationError=type("CanInitializationError", (Exception,), {}))


def fake_pcan_bus(*, reported: int = PCAN_PARAMETER_ON, status: int = PCAN_ERROR_OK) -> SimpleNamespace:
    """A PCAN bus whose driver read-back answers what the test says it answers.

    ``state`` is a plain attribute here on purpose. On the real ``PcanBus`` it is
    a property whose setter calls ``SetValue(PCAN_LISTEN_ONLY, ...)``, so what is
    worth asserting is that the attribute is assigned again after construction —
    python-can applies the constructor's ``state`` before ``PCANBasic.Initialize``
    and throws its return code away.
    """
    calls: list[tuple[int, int]] = []

    def get_value(handle: int, parameter: int) -> tuple[int, int]:
        calls.append((handle, parameter))
        return status, reported

    bus = SimpleNamespace(
        state=FakeBusState.ACTIVE,
        closed=False,
        m_objPCANBasic=SimpleNamespace(GetValue=get_value),
        m_PcanHandle=0x51,
        get_value_calls=calls,
    )
    bus.shutdown = lambda: setattr(bus, "closed", True)
    return bus


def install_pcan_basic(monkeypatch: pytest.MonkeyPatch, *, complete: bool = True) -> None:
    """Put a PCANBasic constant module where `importlib.import_module` finds it.

    ``import_module`` consults ``sys.modules`` before it imports anything, so this
    stands in for the real ``can.interfaces.pcan.basic`` without a PEAK driver
    being installed.
    """
    module = SimpleNamespace()
    if complete:
        module = SimpleNamespace(PCAN_LISTEN_ONLY=PCAN_LISTEN_ONLY, PCAN_PARAMETER_ON=PCAN_PARAMETER_ON, PCAN_ERROR_OK=PCAN_ERROR_OK)
    monkeypatch.setitem(sys.modules, "can.interfaces.pcan.basic", module)


def install_ip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner) -> list[list[str]]:
    """Point the link probe at a real file and capture what it would have run.

    The stand-in is bound into `agentic_hil.can` rather than onto `subprocess`
    itself: this suite spawns real children elsewhere, and a fake `run` in the
    shared module would be waiting for whichever of them reached it first.
    """
    executable = tmp_path / "ip"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr("agentic_hil.can.IP_COMMAND_PATHS", (str(executable),))
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(list(command))
        return runner(command, **kwargs)

    forwarded = {name: getattr(subprocess, name) for name in ("PIPE", "DEVNULL", "Popen", "SubprocessError", "TimeoutExpired", "CalledProcessError")}
    monkeypatch.setattr("agentic_hil.can.subprocess", SimpleNamespace(run=run, **forwarded))
    return commands


def ip_json(ctrlmode: list[str] | None, *, kind: str = "can") -> str:
    """A `can` link's reading, modelled on iproute2's `iplink_can.c`.

    Constructed rather than recorded, and said so: a real `can` link needs a CAN
    controller, and there is no software one — `vcan` is a different link kind
    (see IP_VCAN0, which is recorded). What is modelled is the one thing the
    parser reads, `linkinfo.info_data.ctrlmode`, which iproute2 emits as a JSON
    array of flag names with `listen-only` among them.
    """
    info_data = {"state": "ERROR-ACTIVE"}
    if ctrlmode is not None:
        info_data["ctrlmode"] = ctrlmode
    return f'[{{"ifname": "can0", "linkinfo": {{"info_kind": "{kind}", "info_data": {info_data!r}}}}}]'.replace("'", '"')


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# --- SocketCAN: the mode is the kernel's, so it is measured and never set ------


def test_socketcan_listen_only_opens_when_the_kernel_reports_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "socketcan", "can0", listen_only=True)
    commands = install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json(["listen-only"])))
    opened: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: opened.update(kwargs) or SimpleNamespace(shutdown=lambda: None)))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True
    assert result["listen_only"] is True
    assert result["listen_only_enforcement"] == "link_verified"
    assert commands[0][1:] == ["-details", "-json", "link", "show", "dev", "can0"]
    # Nothing is *sent* to python-can: SocketcanBus takes no listen_only keyword
    # and would silently discard one. The claim rests on the reading above.
    assert "listen_only" not in opened
    assert "state" not in opened


def test_socketcan_refuses_when_the_interface_is_up_without_listen_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect, at the level it actually bites: an ACKing controller."""
    config = can_config(tmp_path, "socketcan", "can0", listen_only=True)
    install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json([])))
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: pytest.fail("the bus must not be opened")))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNSUPPORTED_ERROR
    assert result["listen_only_confirmed"] is False
    assert "dominant ACK bits" in result["link_state"]
    # Refused before contact, so this is a clean no rather than an unknown.
    assert result["side_effect_committed"] is False
    assert result["retry_safe"] is True
    assert any("listen-only on" in step for step in result["remediation"])


def test_socketcan_refuses_when_ctrlmode_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """iproute2 omits the key when no control-mode flag is set. Fail closed."""
    config = can_config(tmp_path, "socketcan", "can0", listen_only=True)
    install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json(None)))
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: pytest.fail("the bus must not be opened")))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNSUPPORTED_ERROR


def test_socketcan_refuses_on_a_virtual_interface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vcan has no controller, so it has no mode to be in and cannot back the claim.

    Driven from recorded iproute2 output rather than a hand-built shape, because
    the trap here is a real field: that output says `"link_type":"can"` for a
    virtual interface.
    """
    config = can_config(tmp_path, "socketcan", "vcan0", listen_only=True)
    install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(IP_VCAN0))
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: pytest.fail("the bus must not be opened")))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNSUPPORTED_ERROR
    assert "vcan interface" in result["link_state"]


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (lambda command, **kwargs: completed(stderr="Device does not exist", returncode=1), "exited 1"),
        (lambda command, **kwargs: completed("not json at all"), "returned no JSON"),
        (lambda command, **kwargs: completed("[]"), "exactly one interface"),
        (lambda command, **kwargs: (_ for _ in ()).throw(OSError("no exec")), "could not be run"),
    ],
)
def test_an_unreadable_control_mode_is_a_refusal_not_an_assumption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner, expected: str) -> None:
    install_ip(monkeypatch, tmp_path, runner)

    link = socketcan_link_listen_only("can0")

    assert link["listen_only"] is False
    assert expected in link["detail"]


def test_a_missing_ip_binary_refuses_rather_than_assuming_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_hil.can.IP_COMMAND_PATHS", (str(tmp_path / "absent" / "ip"),))

    link = socketcan_link_listen_only("can0")

    assert link["listen_only"] is False


def test_the_link_probe_never_resolves_ip_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A proof read from a PATH-resolved binary is not a proof.

    What is asserted is the argv, not that nothing ran: a host with iproute2
    installed — which every Linux runner is — *does* run `ip`, and running it
    from `/sbin` is the whole point. Asserting that nothing ran would only hold
    on a machine without it, which is a statement about the machine rather than
    about PATH.
    """
    from agentic_hil.can import IP_COMMAND_PATHS

    assert all(path.startswith("/") for path in IP_COMMAND_PATHS)
    fake_dir = tmp_path / "poisoned"
    fake_dir.mkdir()
    poisoned = fake_dir / "ip"
    poisoned.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(fake_dir))
    invoked: list[list[str]] = []

    def never_answers(command: list[str], **kwargs: object) -> None:
        invoked.append(list(command))
        raise OSError("this stand-in never answers; the argv is what is under test")

    # `SubprocessError` travels with the stand-in because the probe's own except
    # clause names it — a stand-in missing it turns any raise into an
    # AttributeError from the handler instead of the answer under test.
    monkeypatch.setattr("agentic_hil.can.subprocess", SimpleNamespace(run=never_answers, SubprocessError=subprocess.SubprocessError))

    assert socketcan_link_listen_only("can0")["listen_only"] is False
    assert all(command[0] in IP_COMMAND_PATHS for command in invoked), invoked
    assert str(poisoned) not in [command[0] for command in invoked]


def test_socketcan_without_listen_only_never_probes_the_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "socketcan", "can0", listen_only=False)
    commands = install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json(["listen-only"])))
    opened: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: opened.update(kwargs) or SimpleNamespace(shutdown=lambda: None)))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True
    assert commands == []
    assert "listen_only" not in result


# --- PEAK: the mode is settable, so it is set, re-asserted, and measured -------


def test_peak_listen_only_is_requested_reasserted_and_read_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus()
    opened: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: opened.update(kwargs) or bus))
    install_pcan_basic(monkeypatch)

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True
    assert result["listen_only"] is True
    assert result["listen_only_enforcement"] == "driver_verified"
    # Requested through the only argument PcanBus honours...
    assert opened["state"] == FakeBusState.PASSIVE
    assert "listen_only" not in opened
    # ...re-asserted once the channel exists, because python-can applies the
    # constructor's `state` before Initialize and discards the SetValue result...
    assert bus.state == FakeBusState.PASSIVE
    # ...and then measured rather than assumed.
    assert bus.get_value_calls == [(0x51, PCAN_LISTEN_ONLY)]
    assert bus.closed is False


def test_peak_refuses_and_closes_when_the_driver_reports_listen_only_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus(reported=PCAN_PARAMETER_OFF)
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: bus))
    install_pcan_basic(monkeypatch)

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR
    assert result["listen_only_confirmed"] is False
    assert "PCAN_LISTEN_ONLY off" in result["driver_state"]
    # The channel was on the bus from can.Bus() on, so this cannot claim it never
    # touched anything. What it can claim, and does, is that it is closed again.
    assert result["cleanup_confirmed"] is True
    assert bus.closed is True
    assert any("driver_state" in step for step in result["remediation"])


def test_peak_refuses_when_the_driver_will_not_report_the_parameter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus(status=0x40000)
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: bus))
    install_pcan_basic(monkeypatch)

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR
    assert "refused to report" in result["driver_state"]
    assert bus.closed is True


def test_peak_refuses_when_the_read_back_is_unavailable_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unverifiable is treated as unheld. The flag's whole value is the proof."""
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus()
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: bus))
    install_pcan_basic(monkeypatch, complete=False)

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR
    assert "unverifiable" in result["driver_state"]
    assert bus.closed is True


def test_peak_refuses_before_contact_when_python_can_has_no_passive_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: pytest.fail("the bus must not be opened")))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNSUPPORTED_ERROR
    assert result["side_effect_committed"] is False
    assert result["retry_safe"] is True


def test_peak_without_listen_only_asks_for_no_bus_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=False)
    bus = fake_pcan_bus()
    opened: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: opened.update(kwargs) or bus))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True
    assert "state" not in opened
    assert bus.get_value_calls == []


# --- The process bridge: told is not the same as complied ---------------------


class RecordingBridge:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def request(self, method: str, params: dict[str, object], timeout_s: float) -> dict[str, object]:
        self.requests.append((method, params))
        return dict(self.response)

    def close(self) -> dict[str, object]:
        self.closed = True
        return {"ok": True}


def bridge_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, listen_only: bool, response: dict[str, object]) -> tuple[dict[str, object], RecordingBridge]:
    executable = tmp_path / "bridge.py"
    executable.write_text("", encoding="utf-8")
    config = can_config(tmp_path, "process", "vcan0", listen_only=listen_only, executable=executable.as_posix())
    bridge = RecordingBridge(response)
    monkeypatch.setattr("agentic_hil.can.spawn_managed_process", lambda *args, **kwargs: SimpleNamespace(pid=1))
    monkeypatch.setattr("agentic_hil.can.ProcessCanAdapterSession", lambda child, timeout_s=10.0: bridge)
    return open_process_adapter(config, "bench", config.can_buses["bench"], False), bridge


def test_the_bridge_open_request_still_carries_listen_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, bridge = bridge_result(tmp_path, monkeypatch, listen_only=True, response={"ok": True, "protocol_version": 2, "listen_only": True})

    assert result["ok"] is True
    assert bridge.requests[0][1]["listen_only"] is True
    assert result["listen_only_enforcement"] == "bridge_confirmed"


def test_a_bridge_that_forwards_without_confirming_refuses_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same defect with an extra hop: told, answered ok, did nothing."""
    result, bridge = bridge_result(tmp_path, monkeypatch, listen_only=True, response={"ok": True, "protocol_version": 2})

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR
    assert result["listen_only_confirmed"] is False
    assert result["cleanup_confirmed"] is True
    # A bridge that answered `ok` to `open` put something on the bus, so the one
    # refusal that is about bus contact does not also claim there was none.
    assert "side_effect_committed" not in result
    assert bridge.closed is True
    assert any("listen_only" in step for step in result["remediation"])


def test_a_bridge_that_denies_the_mode_refuses_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = bridge_result(tmp_path, monkeypatch, listen_only=True, response={"ok": True, "protocol_version": 2, "listen_only": False})

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR


def test_a_bridge_never_asked_for_the_mode_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The confirmation is required only where the guarantee was demanded."""
    result, bridge = bridge_result(tmp_path, monkeypatch, listen_only=False, response={"ok": True, "protocol_version": 2, "backend": "vcan"})

    assert result["ok"] is True
    assert bridge.requests[0][1]["listen_only"] is False
    assert "listen_only_enforcement" not in result


def test_a_bridge_answering_a_non_boolean_listen_only_is_a_protocol_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = bridge_result(tmp_path, monkeypatch, listen_only=True, response={"ok": True, "protocol_version": 2, "listen_only": "yes"})

    assert result["ok"] is False
    assert result["error_type"] == "can_adapter_protocol_unsupported"


# --- A transmit on a listen-only bus ------------------------------------------
#
# The defect these exist for was measured, not reasoned about: a PCAN-USB channel
# configured `listen_only: true`, whose driver had confirmed the mode at open,
# was granted `permissions.allow_write: true` and answered `can_send` with
# `ok: true, "CAN frame sent."`. Single node, no ACKer, controller in PASSIVE —
# a claimed side effect nobody could have observed.
#
# What is asserted below is the gate, not the silicon. No test here can say what
# a controller does with a queued transmit; what they can say is that the frame
# never reaches a driver to find out, on every adapter, through every route.


def send_payload() -> dict[str, object]:
    return {"frame_id": "0x123", "data_hex": "01ff"}


@pytest.mark.parametrize(
    ("adapter", "channel"),
    [("peak", "PCAN_USBBUS1"), ("socketcan", "can0"), ("process", "vcan0")],
)
def test_can_send_refuses_by_name_on_a_listen_only_bus(tmp_path: Path, adapter: str, channel: str) -> None:
    """Every adapter, one answer. `listen_only` is a property of the bus entry.

    Writing is *permitted* here — `allow_write: true` — which is the whole point:
    the bus is what refuses, so the permission is never consulted and cannot be
    granted to make this go away. No session is started, and that is load-bearing
    too: an answer of `session_not_active` would mean the gate sits behind the
    session lookup, one step further in than "before any driver call" allows.
    """
    executable = tmp_path / "bridge.py"
    executable.write_text("", encoding="utf-8")
    config = can_config(tmp_path, adapter, channel, listen_only=True, executable=executable.as_posix() if adapter == "process" else None)
    assert config.can_buses["bench"].permissions.allow_write is True
    service = CanBusService(config)
    try:
        result = service.send("bench", send_payload())
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_MODE_ERROR
    assert result["error_type"] != "permission_denied"
    assert result["error_type"] != "session_not_active"
    assert result["listen_only"] is True
    assert result["field"] == "can_buses.bench.listen_only"
    # Refused before a driver was reached, so this is a clean no rather than an
    # unknown — and not retry-safe, because the same call gets the same answer
    # until the configuration changes.
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is False
    # Both ways out are named in the refusal itself, not only in the catalogue.
    assert "second can_buses entry" in result["summary"]
    assert "listen_only: false" in result["summary"]
    assert result["remediation"]


def test_the_listen_only_refusal_is_word_for_word_the_same_on_every_adapter(tmp_path: Path) -> None:
    """The flag is bus-level, so a caller cannot learn the adapter from the no.

    Written as a comparison rather than three copies of an expected string: what
    matters is that no per-adapter branch creeps in later, and a fixed literal
    would not catch one that changed all three together.
    """
    answers = []
    for index, (adapter, channel) in enumerate([("peak", "PCAN_USBBUS1"), ("socketcan", "can0"), ("process", "vcan0")]):
        directory = tmp_path / f"bus{index}"
        executable = directory / "bridge.py"
        directory.mkdir(parents=True, exist_ok=True)
        executable.write_text("", encoding="utf-8")
        config = can_config(directory, adapter, channel, listen_only=True, executable=executable.as_posix() if adapter == "process" else None)
        service = CanBusService(config)
        try:
            result = service.send("bench", send_payload())
        finally:
            service.close()
        answers.append({key: result[key] for key in ("error_type", "summary", "field", "listen_only", "side_effect_status", "retry_safe")})

    assert answers[0] == answers[1] == answers[2], answers


def test_the_permission_refusal_still_fires_on_a_bus_that_is_not_listen_only(tmp_path: Path) -> None:
    """Pinned unchanged. The mode gate wins first; it does not replace the other.

    A bus with no claim on it and `allow_write: false` answers exactly what it
    always answered, in the class it always answered in — `permission_denied` is
    a caller-scoped refusal and stays `retry_safe` once the grant is added.
    """
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=False, allow_write=False)
    service = CanBusService(config)
    try:
        result = service.send("bench", send_payload())
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"
    assert result["error_type"] != LISTEN_ONLY_MODE_ERROR
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is True


def test_a_live_peak_session_never_hands_the_frame_to_the_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The decisive one, with a session actually open on the bus.

    The refusal above is reached with nothing started, which proves the ordering
    but not that a *running* listen-only session refuses too. Here the whole
    service opens a PEAK channel, the driver confirms PASSIVE, and the adapter's
    own `send` is a tripwire: reaching it is the defect.
    """
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus()
    bus.send = lambda *args, **kwargs: pytest.fail("the frame reached the driver on a listen_only bus")
    module = fake_can_module(lambda **kwargs: bus)
    module.Message = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "can", module)
    install_pcan_basic(monkeypatch)
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
        result = service.send("bench", send_payload())
    finally:
        service.close()

    assert started["ok"] is True
    assert bus.state == FakeBusState.PASSIVE, "the session really is the listen-only one"
    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_MODE_ERROR


def test_a_live_bridge_session_never_forwards_the_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same, one process further out, against the real fake bridge.

    A bridge is code this project did not write, so the tripwire is the bridge's
    own transmit log rather than anything mocked in this process: if the send
    were forwarded, the file would have a line in it.
    """
    tx_log = tmp_path / "tx.jsonl"
    monkeypatch.setenv("FAKE_CAN_BRIDGE_TX_LOG", str(tx_log))
    config = can_config(tmp_path, "process", "vcan0", listen_only=True, executable=FAKE_CAN_BRIDGE.as_posix())
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
        result = service.send("bench", send_payload())
    finally:
        service.close()

    assert started["ok"] is True
    assert started["adapter_result"]["listen_only"] is True, "the bridge confirmed the mode, so the session is the listen-only one"
    assert result["ok"] is False
    assert result["error_type"] == LISTEN_ONLY_MODE_ERROR
    assert not tx_log.exists() or tx_log.read_text(encoding="utf-8").strip() == ""


def test_python_can_accepts_a_passive_transmit_without_a_word(tmp_path: Path) -> None:
    """Why the gate cannot be left to the backend, pinned against the real library.

    Not a fake: this drives the installed `PcanBus.send` with a stand-in
    PCANBasic and a bus whose state is `BusState.PASSIVE`. python-can 4.6.1 never
    consults the state — it builds the message, calls `PCANBasic.Write`, and
    raises only on a non-OK return code — so a listen-only channel's transmit is
    accepted and reported as sent.

    If a future python-can starts refusing this, the assertion below fails and
    the honesty note on `PythonCanAdapterSession.send` should be revisited rather
    than left standing on a premise that has moved. The gate itself would stay:
    a refusal that arrives from the driver is already one driver call too late.
    """
    pytest.importorskip("can", reason="python-can is an optional extra")
    from can import BusState, Message
    from can.bus import CanProtocol
    from can.interfaces.pcan.basic import PCAN_ERROR_OK
    from can.interfaces.pcan.pcan import PcanBus

    written: list[int] = []
    passive = SimpleNamespace(
        _state=BusState.PASSIVE,
        state=BusState.PASSIVE,
        _can_protocol=CanProtocol.CAN_20,
        m_PcanHandle=0x51,
        m_objPCANBasic=SimpleNamespace(Write=lambda handle, message: written.append(message.ID) or PCAN_ERROR_OK),
    )

    PcanBus.send(passive, Message(arbitration_id=0x123, data=b"\x01\xff", is_extended_id=False))

    assert written == [0x123], "the transmit reached PCANBasic.Write while the channel was PASSIVE"


# --- End to end, and what a caller can see before starting anything -----------


def test_a_configured_listen_only_reaches_the_driver_through_the_whole_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The decisive one: config field to driver, with nothing stubbed between.

    A field of this kind that is read and then dropped fails here, which is what
    this test exists for — the defect was invisible precisely because every layer above
    the constructor still reported success.
    """
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus()
    opened: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: opened.update(kwargs) or bus))
    install_pcan_basic(monkeypatch)
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
    finally:
        service.close()

    assert started["ok"] is True
    assert opened["state"] == FakeBusState.PASSIVE
    assert bus.get_value_calls == [(0x51, PCAN_LISTEN_ONLY)]


def test_a_driver_that_cannot_honour_it_refuses_the_session_through_the_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus(reported=PCAN_PARAMETER_OFF)
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: bus))
    install_pcan_basic(monkeypatch)
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
        assert service.read("bench")["error_type"] == "session_not_active"
    finally:
        service.close()

    assert started["ok"] is False
    assert started["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR
    assert started["quarantined"] is not True
    assert bus.closed is True


def test_can_buses_list_names_what_would_back_the_claim(tmp_path: Path) -> None:
    config = can_config(tmp_path, "socketcan", "can0", listen_only=True)
    service = CanBusService(config)
    try:
        listed = service.list_buses()
    finally:
        service.close()

    assert listed["buses"]["bench"]["listen_only"] is True
    assert listed["buses"]["bench"]["listen_only_enforcement"] == "link_verified"


def test_every_supported_adapter_declares_how_it_is_held_to_the_flag() -> None:
    """A new adapter must decide this, not inherit an unstated assumption."""
    from agentic_hil.can import LISTEN_ONLY_ENFORCEMENT, SUPPORTED_CAN_ADAPTERS

    assert sorted(LISTEN_ONLY_ENFORCEMENT) == sorted(SUPPORTED_CAN_ADAPTERS)


def test_the_reason_the_keyword_is_not_simply_passed_through() -> None:
    """Against the real python-can, not a fake: the premise of the whole design.

    If a future python-can gives either backend a real `listen_only` argument,
    this fails and the per-adapter mechanism above should be reconsidered rather
    than kept out of habit. Until then, passing the keyword would be discarded by
    `BusABC.__init__(**kwargs: object)` without a word — a silent downgrade, one
    level below the one that was reported.
    """
    import inspect

    pytest.importorskip("can", reason="python-can is an optional extra")
    from can import BusState
    from can.interfaces.pcan.pcan import PcanBus
    from can.interfaces.socketcan.socketcan import SocketcanBus

    for bus_class in (SocketcanBus, PcanBus):
        parameters = inspect.signature(bus_class.__init__).parameters
        assert "listen_only" not in parameters, bus_class
        assert any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()), bus_class
    # PCAN's actual spelling, and the one this code uses.
    assert "state" in inspect.signature(PcanBus.__init__).parameters
    assert BusState.PASSIVE is not None


def test_the_pcan_read_back_constants_travel_with_any_real_pcan_bus() -> None:
    """The measurement is only worth having if it is available where it is needed.

    `can.interfaces.pcan.pcan` imports the PCANBasic constants at module level,
    so any process that has constructed a `PcanBus` already has them in
    `sys.modules` and the read-back cannot fail for want of an import.
    """
    pytest.importorskip("can", reason="python-can is an optional extra")
    import can.interfaces.pcan.pcan  # noqa: F401

    basic = sys.modules["can.interfaces.pcan.basic"]

    assert hasattr(basic, "PCAN_LISTEN_ONLY")
    assert hasattr(basic, "PCAN_PARAMETER_ON")
    assert hasattr(basic, "PCAN_ERROR_OK")


@pytest.mark.parametrize(
    "key",
    [
        LISTEN_ONLY_UNSUPPORTED_ERROR,
        f"{LISTEN_ONLY_UNSUPPORTED_ERROR}:socketcan",
        LISTEN_ONLY_UNCONFIRMED_ERROR,
        f"{LISTEN_ONLY_UNCONFIRMED_ERROR}:peak",
        f"{LISTEN_ONLY_UNCONFIRMED_ERROR}:process",
        LISTEN_ONLY_MODE_ERROR,
    ],
)
def test_each_refusal_carries_its_own_fix(key: str) -> None:
    from agentic_hil.knowledge import catalogue_entry

    entry = catalogue_entry(key)

    assert entry is not None
    assert entry["remediation"]
    assert entry["do_not"]
