"""The `config_reloaded` stop reason has no route to it, and that is pinned (#527).

`ComPortService.reconfigure` and `CanBusService.reconfigure` stop a live session
with the reason `config_reloaded` when the session's entry no longer matches the
configuration being swapped in. Nothing can call either method with a session
open: the description reload is the only caller, it is refused with
`config_reload_in_open_run` as soon as this server holds one lease, and a COM or
a CAN session holds one for its whole life.

The decision is to keep the refusal and keep the loops. The loops are the local
fail-safe that keeps a held device name meaning the same physical board if the
refusal is ever narrowed, and a comment is not behaviour, so the unreachability
itself is what has to be pinned rather than the sentence describing it. Four
things carry it here:

* the only callers of the two `reconfigure` methods in the shipped source are
  the two lines inside the configuration swap, and the swap runs on exactly one
  path, after the reload has already agreed to it;
* a description reload attempted while a COM session is open is refused and
  swaps no configuration, and the same for a CAN session;
* a session's lease is registered from before its device is opened until after
  its release is confirmed, so the window the refusal covers has no hole;
* `config_reloaded` is written down nowhere, so the day it becomes reachable is
  the day somebody has to write it down.

The neighbours that must not change are here too: the loops still stop a session
whose entry moved when they are called directly, under exactly that reason, and
a reload with nothing held still goes through and reaches both services.

The refusal against a declared run and against a bare lease taken outside one is
already pinned in tests/test_config_reload.py. What is new here is the refusal
against a real session, which is the only thing that actually holds a lease for
its whole life on the path this reason lives on.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_authoritative_config

from agentic_hil.config import load_authoritative_config
from agentic_hil.configreload import (
    PROJECT_CONFIG_RELOAD,
    RELOAD_IN_OPEN_RUN_ERROR,
    reload_description,
)
from agentic_hil.contracts import MCP_TOOLS
from agentic_hil.knowledge import MCP_RESOURCES, read_resource
from agentic_hil.tools import AgenticHILToolService

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "agentic_hil"

# The reason with no route to it. Written once here and referred to by name, so
# the walk below has one string to look for and this file has one place that
# spells it.
STOP_REASON = "config_reloaded"

# Device locks are machine-wide, so a port id, a device name and a channel
# shared with another checkout's tests would contend across sibling clones.
# These are this file's alone.
PORT_ID = "reloaded_uart"
DEVICE = "/dev/ttyRELOADEDSTOP0"
BUS_ID = "reloaded_bus"
CHANNEL = "vcan527reload"

COM_PORTS = f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n    baudrate: 115200\n'
CAN_BUSES = f'can_buses:\n  {BUS_ID}:\n    adapter: "socketcan"\n    channel: "{CHANNEL}"\n'


# ---------------------------------------------------------------------------
# The bench: one probe, one COM port, one CAN bus, and a file a reload can read.


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workspace = (tmp_path / "workspace").resolve()
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=(tmp_path / "user-config").resolve(),
        config_version=2,
        debugger_type="stlink",
        com_ports_yaml=COM_PORTS,
        can_buses_yaml=CAN_BUSES,
    )
    monkeypatch.chdir(workspace)
    return workspace, path


def service(workspace: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")


def rename_the_uart(path: Path) -> None:
    """A description change the reload would take if it were allowed to.

    Deliberately a rename rather than a field edit: a renamed entry is the case
    the two loops exist for, because the held name disappears from the new
    configuration entirely."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["com_ports"] = {"renamed_uart": document["com_ports"][PORT_ID]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def rename_the_bus(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["can_buses"] = {"renamed_bus": document["can_buses"][BUS_ID]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def audit_lines(log_path: str) -> list[dict]:
    text = Path(log_path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The doubles. Each one records what `open_hardware_holds` said at the two
# moments the window test is about: the open of the device, and its close.


class Holds:
    """What this server was holding, sampled at the points a session passes."""

    def __init__(self) -> None:
        self.service: AgenticHILToolService | None = None
        self.at_device_open: list[object] = []
        self.at_device_close: list[object] = []

    def sample(self) -> object:
        assert self.service is not None, "the recorder was never bound to a service"
        return self.service.open_hardware_holds()


class FakeSerialHandle:
    def __init__(self, holds: Holds) -> None:
        self._holds = holds
        self.is_open = False
        self.in_waiting = 0
        self.exclusive = None

    def open(self) -> None:
        self._holds.at_device_open.append(self._holds.sample())
        self.is_open = True

    def read(self, size: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self._holds.at_device_close.append(self._holds.sample())
        self.is_open = False


def install_fake_serial(monkeypatch: pytest.MonkeyPatch, holds: Holds) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: FakeSerialHandle(holds)))


class FakeCanBus:
    """What the tests need of a bound socket: nothing to read, a send that is
    taken, a close. The lock question is what is under test, not the socket."""

    def __init__(self, holds: Holds, **kwargs: object) -> None:
        self._holds = holds
        self.kwargs = kwargs
        self.closed = False
        holds.at_device_open.append(holds.sample())

    def recv(self, timeout: float = 0.0) -> None:
        return None

    def send(self, message: object, timeout: float | None = None) -> None:
        return None

    def shutdown(self) -> None:
        self._holds.at_device_close.append(self._holds.sample())
        self.closed = True


def install_fake_can(monkeypatch: pytest.MonkeyPatch, holds: Holds) -> None:
    monkeypatch.setitem(
        sys.modules,
        "can",
        SimpleNamespace(
            Bus=lambda **kwargs: FakeCanBus(holds, **kwargs),
            Message=lambda **kwargs: SimpleNamespace(**kwargs),
            CanInitializationError=type("CanInitializationError", (Exception,), {}),
        ),
    )


# ---------------------------------------------------------------------------
# 1. The callers. Read out of the source rather than trusted from the issue.


def source_modules() -> list[Path]:
    modules = sorted(SOURCE_ROOT.rglob("*.py"))
    assert modules, f"no source modules under {SOURCE_ROOT}"
    return modules


def nodes_with_owner(node: ast.AST, owner: str = "<module>"):
    """Every node in one tree, paired with the function it sits in."""
    for child in ast.iter_child_nodes(node):
        next_owner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
        yield next_owner, child
        yield from nodes_with_owner(child, next_owner)


def attribute_call_sites(owner_attribute: str, method: str) -> set[tuple[str, str]]:
    """Where the shipped source calls ``<something>.<owner_attribute>.<method>()``."""
    sites: set[tuple[str, str]] = set()
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for enclosing, node in nodes_with_owner(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != method:
                continue
            target = node.func.value
            if isinstance(target, ast.Attribute) and target.attr == owner_attribute:
                sites.add((path.relative_to(REPOSITORY_ROOT).as_posix(), enclosing))
    return sites


def method_call_sites(method: str) -> set[tuple[str, str]]:
    """Where the shipped source calls ``<something>.<method>()``, whatever holds it."""
    sites: set[tuple[str, str]] = set()
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for enclosing, node in nodes_with_owner(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method:
                sites.add((path.relative_to(REPOSITORY_ROOT).as_posix(), enclosing))
    return sites


def function_in(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [node for _, node in nodes_with_owner(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(found) == 1, f"{path.name} holds {len(found)} definitions of {name}"
    return found[0]


def test_the_two_reconfigure_calls_live_only_in_the_config_swap() -> None:
    """The reason is written by two loops nothing else reaches.

    Named by call site rather than by counting: a third caller added anywhere in
    the package is a caller that could reach the loops with a session open, and
    it fails here rather than being found in a session log that was not supposed
    to be able to carry this line."""
    assert attribute_call_sites("com_ports", "reconfigure") == {("src/agentic_hil/tools.py", "_swap_config")}
    assert attribute_call_sites("can_buses", "reconfigure") == {("src/agentic_hil/tools.py", "_swap_config")}
    # And the methods those two lines reach are the ones carrying the loops, so a
    # rename cannot leave the assertions above passing against nothing.
    for module, service_class in (("comports.py", "ComPortService"), ("can.py", "CanBusService")):
        source = (SOURCE_ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == service_class]
        assert len(classes) == 1, f"{module} holds {len(classes)} definitions of {service_class}"
        reconfigures = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "reconfigure"]
        assert len(reconfigures) == 1, f"{service_class} holds {len(reconfigures)} reconfigure methods"
        stops = [
            node
            for node in ast.walk(reconfigures[0])
            if isinstance(node, ast.Constant) and node.value == STOP_REASON
        ]
        assert len(stops) == 1, f"{service_class}.reconfigure writes {STOP_REASON} {len(stops)} time(s)"


def test_the_two_loops_say_why_they_are_unreachable_and_why_they_stay() -> None:
    """The writing around the loops, held to the same standard as the loops.

    Pinned on the claims rather than on the phrasing, because a claim is what a
    reader acts on. Three of them, one per assertion:

    * a reload re-reads no permission at all, so the comparison cannot catch a
      revoked grant, and the comment may not say that it does;
    * the stop is unreachable for as long as an open hold refuses the reload,
      and the refusal has a name, so the reader can follow it;
    * the loops stay because they are what keeps a held device name meaning the
      same board if that refusal is ever narrowed.
    """
    for module, service_class in (("comports.py", "ComPortService"), ("can.py", "CanBusService")):
        path = SOURCE_ROOT / module
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == service_class]
        assert len(classes) == 1, f"{module} holds {len(classes)} definitions of {service_class}"
        method = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "reconfigure"]
        assert len(method) == 1, f"{service_class} holds {len(method)} reconfigure methods"
        lines = source.split("\n")
        # From the decorator or def line to the end of the body, so a comment
        # above the loop, a docstring, or both are all read.
        prose = "\n".join(lines[method[0].lineno - 1 : method[0].end_lineno])

        assert "revoke" not in prose.lower(), f"{module}: {service_class}.reconfigure still claims the comparison catches a revoked grant"
        assert "unreachable" in prose.lower(), f"{module}: {service_class}.reconfigure does not say the stop is unreachable"
        assert "config_reload_in_open_run" in prose, f"{module}: {service_class}.reconfigure does not name the refusal that makes it unreachable"
        assert "narrow" in prose.lower(), f"{module}: {service_class}.reconfigure does not say why the loop stays if the refusal is narrowed"


def test_the_config_swap_runs_only_on_the_path_the_reload_agreed_to() -> None:
    """One caller, and the guard that agreed to it stands immediately in front.

    The swap is what carries the two `reconfigure` calls, so the refusal only
    covers them for as long as nothing else calls the swap and nothing reaches
    the swap past the refusal."""
    tools_py = SOURCE_ROOT / "tools.py"
    assert method_call_sites("_swap_config") == {("src/agentic_hil/tools.py", "reload_description")}

    body = function_in(tools_py, "reload_description").body
    swaps = [
        index
        for index, statement in enumerate(body)
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == "_swap_config"
    ]
    assert len(swaps) == 1, "reload_description swaps the configuration more than once"
    index = swaps[0]

    # The decision comes from `configreload.reload_description`, and it is handed
    # what this server is holding. That argument is the whole refusal.
    decisions = [
        statement
        for statement in body[:index]
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "reload_description"
    ]
    assert len(decisions) == 1, "reload_description takes its decision somewhere other than configreload"
    keywords = {keyword.arg for keyword in decisions[0].value.keywords}
    assert "open_holds" in keywords, "the reload decision is taken without being told what is held"

    # And the statement immediately before the swap is the refusal returning.
    guard = body[index - 1]
    assert isinstance(guard, ast.If), "the configuration swap is not guarded by the refusal"
    assert all(isinstance(statement, ast.Return) for statement in guard.body), "the guard in front of the swap does not return"
    assert not guard.orelse, "the guard in front of the swap has an else branch"


# ---------------------------------------------------------------------------
# 2. The refusal, against a real session rather than a bare lease.


def test_a_reload_while_a_com_session_is_open_is_refused_and_swaps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    holds = Holds()
    install_fake_serial(monkeypatch, holds)
    tools = service(workspace)
    holds.service = tools
    try:
        started = tools.call("com_session_start", {"port_id": PORT_ID})
        assert started["ok"] is True, started
        session = tools.com_ports.sessions[PORT_ID]
        rename_the_uart(path)

        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False, refused
        assert refused["error_type"] == RELOAD_IN_OPEN_RUN_ERROR, refused
        assert refused["side_effect_committed"] is False, refused
        assert refused["open_holds"]["open_leases"] == [session.lease.lease_id], refused
        # Nothing was swapped: the port keeps its name, its entry and its session.
        assert sorted(tools.config.com_ports) == [PORT_ID], sorted(tools.config.com_ports)
        assert sorted(tools.com_ports.config.com_ports) == [PORT_ID], sorted(tools.com_ports.config.com_ports)
        assert tools.com_ports.sessions[PORT_ID] is session
        assert tools.call("com_ports_list")["ports"][PORT_ID]["session_active"] is True
        # And the reason the loops would have written is not in the session log.
        assert all(line.get("reason") != STOP_REASON for line in audit_lines(session.log_path)), audit_lines(session.log_path)

        # Closed, the same call goes through, which is what makes the refusal a
        # refusal rather than a permanent block.
        assert tools.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert sorted(tools.config.com_ports) == ["renamed_uart"], sorted(tools.config.com_ports)
    finally:
        tools.close()


def test_a_reload_while_a_can_session_is_open_is_refused_and_swaps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    holds = Holds()
    install_fake_can(monkeypatch, holds)
    tools = service(workspace)
    holds.service = tools
    try:
        started = tools.call("can_session_start", {"bus_id": BUS_ID})
        assert started["ok"] is True, started
        session = tools.can_buses.sessions[BUS_ID]
        rename_the_bus(path)

        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False, refused
        assert refused["error_type"] == RELOAD_IN_OPEN_RUN_ERROR, refused
        assert refused["side_effect_committed"] is False, refused
        assert refused["open_holds"]["open_leases"] == [session.lease.lease_id], refused
        assert sorted(tools.config.can_buses) == [BUS_ID], sorted(tools.config.can_buses)
        assert sorted(tools.can_buses.config.can_buses) == [BUS_ID], sorted(tools.can_buses.config.can_buses)
        assert tools.can_buses.sessions[BUS_ID] is session
        assert tools.call("can_buses_list")["buses"][BUS_ID]["session_active"] is True
        assert all(line.get("reason") != STOP_REASON for line in audit_lines(session.log_path)), audit_lines(session.log_path)

        assert tools.call("can_session_stop", {"bus_id": BUS_ID})["ok"] is True
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert sorted(tools.config.can_buses) == ["renamed_bus"], sorted(tools.config.can_buses)
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# 3. The window the refusal covers, and that it has no hole.


def test_a_com_session_holds_its_lease_from_before_the_open_until_after_the_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is only complete if the lease is there for the whole session.

    Sampled at the two edges the session actually has: the moment the device is
    opened and the moment it is closed. At both of them the reload is asked the
    same question it is asked on the dispatch path, and refuses."""
    workspace, _ = bench(tmp_path, monkeypatch)
    holds = Holds()
    install_fake_serial(monkeypatch, holds)
    tools = service(workspace)
    holds.service = tools
    try:
        assert tools.open_hardware_holds() is None, "something was held before the session started"
        assert tools.call("com_session_start", {"port_id": PORT_ID})["ok"] is True

        assert len(holds.at_device_open) == 1, holds.at_device_open
        opening = holds.at_device_open[0]
        assert opening is not None, "the port was opened before the lease was registered"
        assert opening["open_leases"], opening
        reloaded, refused = reload_description(tools.config, open_holds=opening, quarantine=None)
        assert reloaded is None and refused["error_type"] == RELOAD_IN_OPEN_RUN_ERROR, refused

        # Held for the whole life of the session, not only across the open.
        during = tools.open_hardware_holds()
        assert during is not None and during["open_leases"], during

        assert tools.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        assert len(holds.at_device_close) == 1, holds.at_device_close
        closing = holds.at_device_close[0]
        assert closing is not None, "the lease was gone before the port was closed"
        assert closing["open_leases"], closing
        # And only once the release is confirmed does the hold end.
        assert tools.open_hardware_holds() is None, tools.open_hardware_holds()
    finally:
        tools.close()


def test_a_can_session_holds_its_lease_from_before_the_open_until_after_the_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench(tmp_path, monkeypatch)
    holds = Holds()
    install_fake_can(monkeypatch, holds)
    tools = service(workspace)
    holds.service = tools
    try:
        assert tools.open_hardware_holds() is None, "something was held before the session started"
        assert tools.call("can_session_start", {"bus_id": BUS_ID})["ok"] is True

        assert len(holds.at_device_open) == 1, holds.at_device_open
        opening = holds.at_device_open[0]
        assert opening is not None, "the bus was opened before the lease was registered"
        assert opening["open_leases"], opening
        reloaded, refused = reload_description(tools.config, open_holds=opening, quarantine=None)
        assert reloaded is None and refused["error_type"] == RELOAD_IN_OPEN_RUN_ERROR, refused

        during = tools.open_hardware_holds()
        assert during is not None and during["open_leases"], during

        assert tools.call("can_session_stop", {"bus_id": BUS_ID})["ok"] is True
        assert len(holds.at_device_close) == 1, holds.at_device_close
        closing = holds.at_device_close[0]
        assert closing is not None, "the lease was gone before the bus was closed"
        assert closing["open_leases"], closing
        assert tools.open_hardware_holds() is None, tools.open_hardware_holds()
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# 4. Nobody is told to expect it.

# Where the reason is allowed to appear, and why. The two source files are the
# only places it is written, and this file is the one that has to name what it
# forbids. Everything else in the tracked tree is a place a reader would take it
# as a promise that the line can arrive, and it cannot.
ALLOWED_TO_NAME_IT: dict[str, str] = {
    "src/agentic_hil/comports.py": "the loop that would write it, and the comment above it saying it is unreachable",
    "src/agentic_hil/can.py": "the same loop for a CAN session",
    "tests/test_config_reloaded_unreachable.py": "this file, which cannot check for a string without spelling it",
}

# The changelog is out of the walk rather than allowlisted. It records what the
# tree did, including the name of a symbol whose comment moved, and a line in it
# is not a document telling a reader that a stop reason will arrive in a log.
NOT_WALKED = ("CHANGELOG.md",)


def tracked_files() -> list[str]:
    """Every path git holds, or a skip where git cannot be asked.

    An unpacked source distribution and a checkout with no git binary can answer
    nothing about what is tracked, and a walk that guessed would be reading
    build output and virtualenvs. Every place this suite is meant to run is a
    clone. Written the way tests/test_prose_convention.py writes it, for the
    same reason.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - needs a host without git
        pytest.skip(f"git could not list the tracked tree: {error}")
    if listing.returncode != 0:  # pragma: no cover - needs a directory that is not a repository
        pytest.skip(f"git could not list the tracked tree: {listing.stderr.strip() or listing.returncode}")
    return [name for name in listing.stdout.split("\0") if name]


def test_the_stop_reason_is_written_down_in_no_tracked_file() -> None:
    """No document, no schema, no fixture, no example, no test but this one.

    A reason nobody can reach is a reason nobody may be told to wait for. The day
    the reload is taught to take the sections no open hold covers is the day this
    fails, and that failure is the reminder to write the line down where the
    person reading a session log will look for it.
    """
    carriers: dict[str, list[int]] = {}
    for relative in tracked_files():
        if relative in NOT_WALKED or relative in ALLOWED_TO_NAME_IT:
            continue
        try:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = [number for number, line in enumerate(text.split("\n"), 1) if STOP_REASON in line]
        if lines:
            carriers[relative] = lines

    assert not carriers, (
        f"{STOP_REASON} is a stop reason nothing can reach, and it now appears in "
        f"{sorted(carriers)}. Either the reload was narrowed so that it can be "
        f"written, in which case say so where the reader of a session log will "
        f"look, or the mention is a promise the product does not keep."
    )


def test_every_file_allowed_to_name_it_still_names_it() -> None:
    """An allowance nothing matches is an allowance that stopped being read."""
    for relative, reason in ALLOWED_TO_NAME_IT.items():
        path = REPOSITORY_ROOT / relative
        assert path.is_file(), f"{relative} is allowed to name the reason and is not in the tree"
        assert STOP_REASON in path.read_text(encoding="utf-8"), f"{relative} no longer names {STOP_REASON}"
        assert reason.strip(), f"{relative}: an allowance has to say why the reason belongs there"


def test_the_stop_reason_is_in_no_tool_description_and_no_knowledge_resource() -> None:
    """The generated surfaces, which the walk over files cannot see.

    Tool descriptions, input schemas and the reference resources are built in
    Python and handed to an agent at run time, so a mention of the reason there
    would reach a reader without ever being a line in a document."""
    assert STOP_REASON not in json.dumps(MCP_TOOLS, ensure_ascii=False)
    served = 0
    for resource in MCP_RESOURCES:
        contents = read_resource(str(resource["uri"]))
        assert contents is not None, resource
        served += 1
        assert STOP_REASON not in contents["text"], resource["uri"]
    assert served == len(MCP_RESOURCES) and served > 0, "no knowledge resource was actually read"


# ---------------------------------------------------------------------------
# The neighbours: the loops stay, and a reload with nothing held still works.


def test_the_com_loop_still_stops_a_session_whose_entry_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-safe, exercised the only way anything can exercise it.

    Called directly, because no caller in the package can get here with a
    session open. That is the point of the file, and it is also why this loop is
    kept: if the refusal is ever narrowed, this is what keeps a held device name
    from meaning another board."""
    workspace, _ = bench(tmp_path, monkeypatch)
    holds = Holds()
    install_fake_serial(monkeypatch, holds)
    tools = service(workspace)
    holds.service = tools
    session = None
    try:
        assert tools.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        session = tools.com_ports.sessions[PORT_ID]

        tools.com_ports.reconfigure(replace(tools.config, com_ports={}))

        assert PORT_ID not in tools.com_ports.sessions, sorted(tools.com_ports.sessions)
        assert session.active is False
        last = audit_lines(session.log_path)[-1]
        assert last["event"] == "stop", last
        assert last["reason"] == STOP_REASON, last
    finally:
        if session is not None:
            session.lease.release()
        tools.close()


def test_the_can_loop_still_stops_a_session_whose_entry_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench(tmp_path, monkeypatch)
    holds = Holds()
    install_fake_can(monkeypatch, holds)
    tools = service(workspace)
    holds.service = tools
    session = None
    try:
        assert tools.call("can_session_start", {"bus_id": BUS_ID})["ok"] is True
        session = tools.can_buses.sessions[BUS_ID]

        tools.can_buses.reconfigure(replace(tools.config, can_buses={}))

        assert BUS_ID not in tools.can_buses.sessions, sorted(tools.can_buses.sessions)
        assert session.active is False
        last = audit_lines(session.log_path)[-1]
        assert last["event"] == "stop", last
        assert last["reason"] == STOP_REASON, last
    finally:
        if session is not None:
            session.lease.release()
        tools.close()


def test_a_reload_with_nothing_held_still_reaches_both_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The behaviour the refusal is not allowed to take away.

    Nothing is held, so the swap runs, and both services answer out of the new
    description rather than the one this server started on."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        assert tools.open_hardware_holds() is None
        rename_the_uart(path)
        rename_the_bus(path)

        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True

        assert sorted(tools.com_ports.config.com_ports) == ["renamed_uart"], sorted(tools.com_ports.config.com_ports)
        assert sorted(tools.can_buses.config.can_buses) == ["renamed_bus"], sorted(tools.can_buses.config.can_buses)
        assert sorted(tools.call("com_ports_list")["ports"]) == ["renamed_uart"]
        assert sorted(tools.call("can_buses_list")["buses"]) == ["renamed_bus"]
    finally:
        tools.close()
