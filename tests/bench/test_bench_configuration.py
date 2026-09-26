"""Where the configuration surfaces meet the real bench.

Every surface that reads or writes a project's configuration is proven in the
unit and container tiers against a file. What only a bench can show is where
those surfaces touch hardware: a configuration generated from the probe and the
port actually attached, and then used to drive the board, and a configuration
changed, described or re-read while a real session holds the port or the probe.
That second half is the one the policy exists for. A session took its devices
under the permissions a file stated, so a write or a reload that moved those
underneath it is refused while it holds them, and the refusal has to name the
hold without disturbing the session that holds the board.

Every server here is one `agentic-hil mcp-stdio` child, spoken to in JSON-RPC
over its own stdin and stdout, exactly as an agent host speaks to it; every
command is this checkout's CLI. Nothing here opens a probe or a port itself.

Nothing writes the configuration `init` selected for this session. A test that
changes a file works on a copy beside it, named through ``AGENTIC_HIL_CONFIG``,
which is the documented override; the one that generates a configuration does
so for a fresh copy of the demo under this session's temporary root, with its
own configuration and state roots, and checks where the file will land before
the server that writes it is started. Identifying values (probe serials, port
names, USB identities) are compared between two answers of the product and
never against anything written here.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml
from support import scaled_time_bound

from .conftest import (
    BENCH_ONLY,
    COMMAND_TIMEOUT_S,
    DEMO,
    DEMO_IMAGE,
    REPOSITORY_ROOT,
    Bench,
    child_command,
    isolated_environment,
    outside_this_runs_root,
)

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# What the demo says at boot, from the comparator of its own plan.
BANNER = "Hello World"
BANNER_TIMEOUT_S = 15.0
READ_SLICE_S = 0.5

# A flash or a probe read through OpenOCD is the slow case.
REPLY_TIMEOUT_S = 300.0
SHUTDOWN_TIMEOUT_S = scaled_time_bound(120.0)

MCP_PROTOCOL_VERSION = "2025-06-18"

# A counter the demo's SysTick advances every millisecond. Two equal reads of it
# are what a halted core looks like from the outside.
COUNTER_SYMBOL = "uptime_ms"

# The grants the copies below are expected to carry already. They are read and
# never widened: a copy that lacks one fails naming it.
RIGHTS_THESE_TESTS_USE = ("allow_config_write", "allow_config_description_write")

# Values written into a copy's description. Plain names, identifying nothing.
RENAMED_ON_DISK = "bench-target-renamed-on-disk"
RENAMED_OVER_MCP = "bench-target-renamed-over-mcp"


@dataclass(frozen=True)
class Surface:
    """One configuration, and the directory and environment everything under it runs with."""

    project: Path
    environment: dict[str, str]
    config: Path | None = None

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """The CLI under test. stdin is closed so a command reads as not typed at a terminal."""
        return subprocess.run(
            child_command(*arguments),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd=str(self.project),
            env=self.environment,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )

    def document(self, *arguments: str) -> tuple[int, dict]:
        answered = self.run(*arguments, "--json")
        assert answered.stdout.strip(), f"{arguments} printed no document (exit {answered.returncode}):\n{answered.stderr}"
        return answered.returncode, json.loads(answered.stdout)

    def configuration(self) -> dict:
        assert self.config is not None, "this surface has no configuration file yet"
        return yaml.safe_load(self.config.read_text(encoding="utf-8"))

    def digest(self) -> str:
        assert self.config is not None, "this surface has no configuration file yet"
        return hashlib.sha256(self.config.read_bytes()).hexdigest()


def variant(bench: Bench, name: str, rights: tuple[str, ...] = RIGHTS_THESE_TESTS_USE) -> Surface:
    """This session's configuration, copied byte for byte beside it, and a surface over the copy.

    Outside the workspace, because the product refuses a configuration stored
    inside the workspace it governs, and handed over as an absolute path in
    ``AGENTIC_HIL_CONFIG``. A server started against it keeps its ownership state
    under its own project key, so what a test here leaves standing is cleared
    through the same copy.
    """
    directory = bench.config_root / "configuration-surface-copies"
    directory.mkdir(parents=True, exist_ok=True)
    copy = directory / f"{name}.yaml"
    shutil.copyfile(bench.config, copy)
    surface = Surface(project=bench.project, environment=isolated_environment(bench.config_root, bench.state_root, AGENTIC_HIL_CONFIG=str(copy)), config=copy)
    permissions = surface.configuration().get("permissions") or {}
    missing = [right for right in rights if permissions.get(right) is not True]
    if missing:
        pytest.fail(f"this session's configuration does not grant {missing}, and these tests read grants rather than widen them", pytrace=False)
    return surface


def rewrite(path: Path, change: Callable[[dict], None]) -> None:
    """An operator's edit of a configuration file: read it, change it, write it back."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def rename_target(name: str) -> Callable[[dict], None]:
    def change(document: dict) -> None:
        document["target"]["name"] = name

    return change


def without(keys: list[str]) -> Callable[[dict], None]:
    """An edit that removes dotted keys, the way a hand-trimmed file lacks them."""

    def change(document: dict) -> None:
        for key in keys:
            *parents, leaf = key.split(".")
            holder = document
            for part in parents:
                holder = holder[part]
            del holder[leaf]

    return change


def value_at(document: dict, key: str) -> object:
    holder: object = document
    for part in key.split("."):
        assert isinstance(holder, dict) and part in holder, f"`{key}` is not in the configuration"
        holder = holder[part]
    return holder


def banner_plan(bench: Bench, name: str) -> str:
    """The demo's own claim as a plan of its own: open the port clean, reset, read the banner."""
    plan = bench.project / f"{name}.yaml"
    plan.write_text(
        chr(10).join([
            "version: 3",
            f"name: {name}",
            "steps:",
            f"  - device: {bench.com_port_name()}",
            "    action: uart_open",
            "    clear_buffer: true",
            f"  - device: {bench.debugger_name()}",
            "    action: reset",
            "    mode: run",
            f"  - device: {bench.com_port_name()}",
            "    action: uart_read",
            "    comparator:",
            f'      equals: "{BANNER}"',
            "    timeout_s: 5",
            "",
        ]),
        encoding="utf-8",
    )
    return plan.name


def removable_keys(planned: dict) -> list[str]:
    """What an adoption found already current, less the keys that decide which board is read.

    The probe serial selects the probe a read talks to, so it stays; a debugger's
    own target block is a statement about the part and is left too. Everything
    else that matched the attached hardware is what a trimmed file lacks and an
    adoption has to put back.
    """
    probe_key = f"debuggers.{planned['debugger_id']}.probe_id"
    removable = sorted(row["key"] for row in planned["already_current"] if row["key"] != probe_key and ".target." not in row["key"])
    assert f"com_ports.{planned['com_port_id']}.device" in removable, planned
    return removable


def kept_controller(planned: dict, configured: str) -> str:
    """The controller row adoption keeps (#442): both values, the configured one kept, and why."""
    kept = {row["key"]: row for row in planned["kept"]}
    assert "target.controller" in kept, planned
    row = kept["target.controller"]
    assert row["configured_value"] == configured, row
    discovered = row["discovered_value"]
    assert isinstance(discovered, str) and discovered and discovered != configured, row
    assert row["reason"], row
    return discovered


class Server:
    """One `agentic-hil mcp-stdio` child, driven as an agent host drives it.

    The same shape as the serial module's: answers are pulled off stdout by a
    thread and handed over by id, stderr goes to a file, and starting and greeting
    are two steps so the fixture can register the child before a handshake that
    may fail.
    """

    def __init__(self, surface: Surface, stderr_path: Path) -> None:
        self.surface = surface
        self.stderr_path = stderr_path
        self._stderr = stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            cwd=str(surface.project),
            env=surface.environment,
        )
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._pump = threading.Thread(target=self._collect, daemon=True)
        self._pump.start()
        self._last_id = 0
        self._closed = False

    def greet(self) -> None:
        hello = self.request(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "agentic-hil-bench-tier", "version": "0"}},
        )
        assert hello["result"]["serverInfo"]["name"] == "agentic-hil", hello
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _collect(self) -> None:
        try:
            for line in self.process.stdout or ():
                self._answers.put(line)
        finally:
            self._answers.put(None)

    def stderr_text(self) -> str:
        if not self._stderr.closed:
            self._stderr.flush()
        try:
            return self.stderr_path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - a stderr file this host cannot read is not the failure
            return ""

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> dict:
        self._last_id += 1
        request_id = self._last_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"the MCP server did not answer `{method}` within {timeout_s}s. Its stderr said: {self.stderr_text()}")
            try:
                line = self._answers.get(timeout=remaining)
            except queue.Empty:  # pragma: no cover - the deadline above is what ends this loop
                continue
            if line is None:
                raise AssertionError(f"the MCP server ended before answering `{method}`. Its stderr said: {self.stderr_text()}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> dict:
        """One tool call; the tool's own document, held to the `isError` flag beside it."""
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        assert "result" in answered, answered
        envelope = answered["result"]
        document = envelope["structuredContent"]
        assert isinstance(document, dict), envelope
        if document.get("ok") is not True:
            assert envelope["isError"] is True, envelope
        return document

    def close(self) -> int | None:
        if self._closed:
            return self.process.returncode
        self._closed = True
        if self.process.poll() is None:
            with suppress(OSError):  # a pipe already gone needs no closing
                if self.process.stdin is not None:
                    self.process.stdin.close()
            try:
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        self._pump.join(timeout=SHUTDOWN_TIMEOUT_S)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()
        return self.process.returncode


class Surfaces:
    """One test's servers, the sessions and runs they opened, and the bench left clear after them."""

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.servers: list[Server] = []
        self.debugging: list[Server] = []
        self.running: list[Server] = []
        self.surfaces: list[Surface] = []

    def start(self, surface: Surface) -> Server:
        server = Server(surface, self.scratch / f"server-{len(self.servers)}.stderr")
        self.servers.append(server)
        if not any(known is surface for known in self.surfaces):
            self.surfaces.append(surface)
        server.greet()
        return server

    def debug(self, server: Server, image_path: str) -> dict:
        """A debug session attached to the running demo, registered so teardown ends it."""
        started = server.call("debug_start_session", {"image_path": image_path, "mode": "attach"})
        if started.get("ok") is True:
            self.debugging.append(server)
        assert started["ok"] is True, started
        assert started["session"]["status"] == "halted", started
        return started

    def end_debugging(self, server: Server) -> None:
        """The session over and the demo running again, through the product."""
        stopped = server.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
        self.debugging.remove(server)
        reset = server.call("reset_target", {"mode": "run"})
        assert reset["ok"] is True, reset

    def close(self) -> None:
        problems: list[str] = []
        for server in list(self.running):
            try:
                stopped = server.call("bench_run_stop")
                if stopped.get("ok") is not True:
                    problems.append(f"bench_run_stop did not close the run: {stopped}")
            except Exception as error:
                problems.append(f"a run could not be closed: {type(error).__name__}: {error}")
        for server in list(self.debugging):
            try:
                stopped = server.call("debug_stop_session")
                if stopped.get("ok") is not True:
                    problems.append(f"debug_stop_session did not end the session: {stopped}")
                reset = server.call("reset_target", {"mode": "run"})
                if reset.get("ok") is not True:
                    problems.append(f"the target was not reset after the session: {reset}")
            except Exception as error:
                problems.append(f"a session could not be ended: {type(error).__name__}: {error}")
        for server in reversed(self.servers):
            try:
                server.close()
            except Exception as error:
                problems.append(f"a server could not be closed: {type(error).__name__}: {error}")
        for surface in self.surfaces:
            problems.extend(cleared(surface))
        assert not problems, "\n".join(problems)


def cleared(surface: Surface) -> list[str]:
    """Whatever `recover` could not settle under one configuration; empty when the bench is clear."""
    _, status = surface.document("lease-status")
    if not status.get("blocked") and not status.get("incident_stands"):
        return []
    quarantine_id = status.get("quarantine_id")
    if not isinstance(quarantine_id, str) or not quarantine_id:
        return [f"the bench is not free and names no quarantine id to clear: {status.get('cleanup_reasons')}"]
    _, recovered = surface.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
    if recovered.get("error_type") == "config_changed":
        _, recovered = surface.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id, "--accept-config-change")
    if recovered.get("ok") is not True:
        return [f"a quarantine this file raised could not be cleared: {recovered}"]
    return []


@pytest.fixture
def surfaces(tmp_path: Path) -> Iterator[Surfaces]:
    manager = Surfaces(tmp_path)
    try:
        yield manager
    finally:
        manager.close()


def read_until(server: Server, port: str, wanted: str, timeout_s: float = BANNER_TIMEOUT_S) -> str:
    """Everything the line said until `wanted` was among it, or the time ran out."""
    received = ""
    deadline = time.monotonic() + timeout_s
    while True:
        read = server.call("com_read", {"port_id": port, "wait_timeout_s": READ_SLICE_S})
        assert read["ok"] is True, read
        received += read["data"]["text"]
        if wanted in received or time.monotonic() >= deadline:
            return received


def banner_after_reset(server: Server, port: str) -> str:
    """The board restarted through the product and what its port said after it."""
    reset = server.call("reset_target", {"mode": "run"})
    assert reset["ok"] is True, reset
    return read_until(server, port, BANNER)


def counter(server: Server) -> str:
    """The demo's millisecond counter, read through the open debug session."""
    read = server.call("debug_symbol_value", {"symbol": COUNTER_SYMBOL})
    assert read["ok"] is True, read
    return read["hex"]


def described_value(described: dict, key: str) -> object:
    """The value `project_config_describe` reports for one dotted key."""
    for entry in [*described["writable_keys"], *described["locked_keys"]]:
        if entry["key"] == key:
            return entry["current_value"]
    raise AssertionError(f"`{key}` is not among the keys project_config_describe lists: {described['summary']}")


def assert_refused_by_the_hold(refused: dict, error_type: str, stop_call: str) -> None:
    """A refusal that names the hold, says how to end it, and started nothing."""
    assert refused["ok"] is False, refused
    assert refused["error_type"] == error_type, refused
    assert refused["open_holds"], refused
    # The errors resource promises the remediation a failing result carries
    # inline, and it is the half that names the call which ends the hold.
    assert stop_call in json.dumps(refused.get("remediation")), refused
    assert refused.get("do_not"), refused
    assert refused["retry_safe"] is True, refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["side_effect_status"] == "not_started", refused


def refuse_every_write_under_the_hold(server: Server, surface: Surface, stop_call: str) -> None:
    """Describe answers from the file on disk; set, reload and create are each refused by the hold.

    The copy was edited on disk before this is called, so the server's loaded
    description is stale: describe reports that and the edited value, while every
    call that would adopt or replace the file is refused naming the hold, and the
    file is byte for byte what the operator left.
    """
    left_by_the_operator = surface.digest()

    described = server.call("project_config_describe")
    assert described["ok"] is True, described
    assert described["writes_blocked_by_open_run"] is True, described
    assert described["open_holds"] and described["open_holds"]["open_leases"], described
    assert described.get("config_stale") is True, described
    assert described["config_status"]["state"] == "changed", described
    assert described["document_source"] == "disk", described
    assert described_value(described, "target.name") == RENAMED_ON_DISK, described

    refused = server.call("project_config_set", {"changes": [{"key": "target.name", "value": RENAMED_OVER_MCP}]})
    assert_refused_by_the_hold(refused, "config_write_in_open_run", stop_call)
    assert surface.digest() == left_by_the_operator

    refused = server.call("project_config_reload_description")
    assert_refused_by_the_hold(refused, "config_reload_in_open_run", stop_call)

    refused = server.call("project_config_create")
    assert_refused_by_the_hold(refused, "config_write_in_open_run", stop_call)
    assert surface.digest() == left_by_the_operator

    # The operator's half needs no hold check: it reads the file and changes
    # nothing, so it answers what the server would take once the hold is gone.
    status, preview = surface.document("config-reload")
    assert status == 0, preview
    assert preview["scope"] == "preview", preview
    assert preview["would_reload"]["target"]["name"] == RENAMED_ON_DISK, preview
    assert surface.digest() == left_by_the_operator


def accept_every_write_once_the_hold_is_gone(server: Server, surface: Surface) -> None:
    """With nothing held the same calls go through: reload adopts the edit, set writes the copy."""
    described = server.call("project_config_describe")
    assert described["ok"] is True, described
    assert described["open_holds"] is None, described
    assert described["writes_blocked_by_open_run"] is False, described
    assert described.get("config_stale") is True, described

    reloaded = server.call("project_config_reload_description")
    assert reloaded["ok"] is True, reloaded
    assert reloaded["reloaded"] is True, reloaded
    assert "target.name" in reloaded["description_changes"], reloaded
    assert reloaded["permission_differences"] == [], reloaded
    assert reloaded["permissions_reloaded"] is False, reloaded

    described = server.call("project_config_describe")
    assert described.get("config_stale") is not True, described
    assert described_value(described, "target.name") == RENAMED_ON_DISK, described

    written = server.call("project_config_set", {"changes": [{"key": "target.name", "value": RENAMED_OVER_MCP}]})
    assert written["ok"] is True, written
    assert written["changes"] == [{"key": "target.name", "previous_value": RENAMED_ON_DISK, "value": RENAMED_OVER_MCP, "right": written["changes"][0]["right"]}], written
    assert written["reload_required"] is True, written
    assert written["provenance"]["last_modified_via"] == "mcp:project_config_set", written
    assert surface.configuration()["target"]["name"] == RENAMED_OVER_MCP

    reloaded = server.call("project_config_reload_description")
    assert reloaded["ok"] is True, reloaded
    assert reloaded["description_changes"] == ["target.name"], reloaded


def test_a_project_without_a_configuration_gets_one_over_mcp_that_names_this_bench_and_runs_the_demo(
    bench: Bench, firmware: Path, surfaces: Surfaces, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`project_config_create` reads the attached probe and port, and what it writes drives the board.

    A fresh copy of the demo with no configuration anywhere, and a server
    started for it, which refuses a hardware call until it has one. The file it
    generates has to name the probe and the port this session's own `init`
    found, with the identity fields discovery read off the port, and it has to
    work: the same server probes the target through it, and the demo's own plan
    passes through it afterwards.
    """
    root = tmp_path_factory.mktemp("configuration-create")
    project = root / DEMO.name
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("build", ".agentic-hil"))
    config_root = root / "config"
    state_root = root / "state"
    environment = isolated_environment(config_root, state_root)
    environment.pop("AGENTIC_HIL_CONFIG", None)

    # Where the file will land, established before anything can write it. The
    # server writes to the first candidate root that can hold it, and the second
    # one is under the operator's home: this test must never reach it.
    where = subprocess.run(
        [sys.executable, "-s", "-c", WHERE_THE_CONFIGURATION_WILL_LAND],
        capture_output=True,
        text=True,
        cwd=str(project),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    assert where.returncode == 0 and where.stdout.strip(), f"could not establish where the configuration would be written:\n{where.stdout}\n{where.stderr}"
    landing = json.loads(where.stdout)
    if landing["existing"] or landing["refused"] or outside_this_runs_root(Path(landing["candidates"][0]), config_root) is not None:
        pytest.fail(f"the configuration this test generates would not land under this session's own root: {landing}", pytrace=False)
    if not Path(landing["state_root"]).resolve().is_relative_to(state_root.resolve()):
        pytest.fail(f"the state root this test's server would use is not under this session's own root: {landing}", pytrace=False)

    surface = Surface(project=project, environment=environment)
    server = surfaces.start(surface)

    refused = server.call("probe_target")
    assert refused["error_type"] == "config_file_not_found", refused

    created = server.call("project_config_create")
    assert created["ok"] is True, created
    assert created["created"] is True, created
    assert created["reload_required"] is False, created
    assert created["side_effect_status"] == "not_started", created
    assert created["cleanup_required"] is False, created
    written_to = Path(created["path"])
    assert outside_this_runs_root(written_to, config_root) is None, created
    assert Path(created["workspace_root"]).resolve() == project.resolve(), created
    assert Path(created["state_root"]).resolve().is_relative_to(state_root.resolve()), created
    assert created["optional_override"] == f"AGENTIC_HIL_CONFIG={created['path']}", created

    generated = yaml.safe_load(written_to.read_text(encoding="utf-8"))
    ours = bench.configuration()
    (debugger,) = generated["debuggers"].values()
    assert isinstance(debugger.get("probe_id"), str) and debugger["probe_id"], "the generated debugger entry names no probe"
    assert isinstance(debugger.get("type"), str) and debugger["type"], "the generated debugger entry names no backend"
    assert isinstance(debugger.get("executable"), str) and debugger["executable"], "the generated debugger entry names no executable"
    # The same probe this session's own `init` bound, compared and never printed.
    session_probes = {str(entry.get("probe_id") or "").casefold() for entry in ours["debuggers"].values()}
    assert debugger["probe_id"].casefold() in session_probes, "the generated configuration names a probe this session's own configuration does not"
    assert str(created["hardware_discovery"]["probe_id"]).casefold() == debugger["probe_id"].casefold(), "the file names another probe than the discovery it reports"

    (port,) = generated["com_ports"].values()
    assert isinstance(port.get("device"), str) and port["device"], "the generated port entry names no device"
    assert isinstance(port.get("serial_number"), str) and port["serial_number"], "the generated port entry carries no serial number"
    assert all(isinstance(port.get(field), int) and not isinstance(port.get(field), bool) for field in ("vid", "pid")), "the generated port entry carries no USB identity"
    identity = ("device", "serial_number", "vid", "pid")
    session_ports = [{field: entry.get(field) for field in identity} for entry in (ours.get("com_ports") or {}).values()]
    assert {field: port[field] for field in identity} in session_ports, "the generated port is not the one this session's own configuration names"
    discovered_port = created["hardware_discovery"]["com_port"]
    assert isinstance(discovered_port, dict) and discovered_port.get("serial_number") == port["serial_number"], "the file names another port than the discovery it reports"

    probed = server.call("probe_target")
    assert probed["ok"] is True, probed
    assert probed["target_detected"] is True, probed
    server.close()

    # The demo's own plan, through the generated file, found by discovery the
    # way an operator's command finds it.
    image = project / DEMO_IMAGE
    image.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(firmware, image)
    status, report = surface.document("test-reactor", "--test-config", "testconfig.yaml")
    assert status == 0, report
    assert report["ok"] is True, report

    _, lease = surface.document("lease-status")
    assert not lease.get("blocked") and not lease.get("incident_stands"), lease


# Run in a child with the test's own environment and directory, so the answer is
# the one the server will reach. Directories are created only under the two
# roots the environment names, and only after the first candidate was found to
# be under the first of them.
WHERE_THE_CONFIGURATION_WILL_LAND = """
import json, os
from pathlib import Path
from agentic_hil.config import ConfigError, project_config_candidates, user_state_root, writable_stable_directory
workspace = Path.cwd().resolve()
candidates = [str(path) for path in project_config_candidates(workspace)]
existing = [path for path in candidates if Path(path).is_file()]
refused = []
state_root = ""
first = Path(candidates[0])
if not existing and first.resolve().is_relative_to(Path(os.environ["XDG_CONFIG_HOME"]).resolve()):
    try:
        writable_stable_directory(first.parent, field="config_path")
    except ConfigError as error:
        refused.append(str(error))
    try:
        state_root = str(writable_stable_directory(user_state_root(), field="state_root"))
    except ConfigError as error:
        refused.append(str(error))
print(json.dumps({"candidates": candidates, "existing": existing, "refused": refused, "state_root": state_root}))
"""


def test_describe_set_and_reload_answer_by_the_hold_while_a_com_session_reads_the_board(bench: Bench, firmware: Path, surfaces: Surfaces) -> None:
    """An open COM session holds the port; every configuration write is refused naming it, and the line keeps reading.

    The copy is edited on disk while the session is open, as an operator would.
    Describe reports the edit and the staleness and that writes are blocked;
    set, reload and create are refused with the hold and the call that ends it;
    the command line's preview answers from the file. The session reads the
    board's banner before and after all of it, and once it is stopped the same
    calls go through.
    """
    surface = variant(bench, "com-session-hold")
    port = bench.com_port_name()
    server = surfaces.start(surface)

    opened = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert opened["ok"] is True, opened
    assert BANNER in banner_after_reset(server, port)

    rewrite(surface.config, rename_target(RENAMED_ON_DISK))
    refuse_every_write_under_the_hold(server, surface, "com_session_stop")

    assert BANNER in banner_after_reset(server, port), "the COM session stopped reading the board after the configuration calls"
    stopped = server.call("com_session_stop", {"port_id": port})
    assert stopped["ok"] is True, stopped

    accept_every_write_once_the_hold_is_gone(server, surface)


def test_describe_set_and_reload_answer_by_the_hold_while_a_debug_session_holds_the_core(bench: Bench, gdb: None, firmware: Path, surfaces: Surfaces) -> None:
    """An open debug session holds the probe; the same refusals, and the core stays halted through them.

    The demo's millisecond counter is read through the session before and after
    every configuration call: a core that some call had resumed would have moved
    it. The session is stopped and the board reset through the product, and the
    same calls then go through.
    """
    surface = variant(bench, "debug-session-hold")
    server = surfaces.start(surface)
    surfaces.debug(server, firmware.relative_to(bench.project).as_posix())
    halted_at = counter(server)

    rewrite(surface.config, rename_target(RENAMED_ON_DISK))
    refuse_every_write_under_the_hold(server, surface, "debug_stop_session")

    assert counter(server) == halted_at, "the core moved while the debug session held it"
    surfaces.end_debugging(server)

    accept_every_write_once_the_hold_is_gone(server, surface)


def test_adopt_hardware_on_the_command_line_puts_back_what_a_trimmed_file_lacks_and_keeps_the_controller(
    bench: Bench, firmware: Path, surfaces: Surfaces
) -> None:
    """The write path of `adopt-hardware`, against the probe and the port actually attached.

    The dry run keeps the configured controller beside the family name the board
    reports, in the document and in the rendering an operator reads (#442). An
    apply over a file that already matches writes nothing. A copy with every
    matching key removed but the probe serial gets exactly those keys back, with
    the provenance of the command line, the controller still the configured one,
    and the session's own file untouched; the demo's banner then passes through
    the copy.
    """
    surface = variant(bench, "adopt-on-the-command-line")
    session_file = hashlib.sha256(bench.config.read_bytes()).hexdigest()
    configured = surface.configuration()["target"]["controller"]

    status, planned = surface.document("adopt-hardware", "--dry-run")
    assert status == 0, planned
    assert planned["ok"] is True and planned["applied"] is False, planned
    discovered = kept_controller(planned, configured)
    rendered = " ".join(surface.run("adopt-hardware", "--dry-run").stdout.split())
    assert "Left alone, because somebody set them" in rendered, rendered
    assert f"target.controller configured {configured}, attached {discovered}, adoption keeps {configured}" in rendered, rendered

    # Whatever the file still needed, once; then an apply with nothing to carry.
    status, settled = surface.document("adopt-hardware")
    assert status == 0 and settled["ok"] is True, settled
    matching = surface.digest()
    status, again = surface.document("adopt-hardware")
    assert status == 0, again
    assert again["ok"] is True and again["applied"] is False and again["carried"] == [], again
    assert again["summary"].endswith("Nothing was written."), again
    assert surface.digest() == matching

    removed = removable_keys(again)
    settled_values = {key: value_at(surface.configuration(), key) for key in removed}
    rewrite(surface.config, without(removed))

    status, adopted = surface.document("adopt-hardware")
    assert status == 0, adopted
    assert adopted["ok"] is True and adopted["applied"] is True, adopted
    assert sorted(row["key"] for row in adopted["carried"]) == removed, adopted
    assert all(row["previous_value"] is None for row in adopted["carried"]), adopted
    assert f"debuggers.{adopted['debugger_id']}.probe_id" in {row["key"] for row in adopted["already_current"]}, adopted
    kept_controller(adopted, configured)
    assert adopted["created_entries"] == [] and adopted["permissions_changed"] == [], adopted
    assert adopted["reload_required"] is True, adopted
    assert adopted["provenance"]["last_modified_via"] == "cli:adopt-hardware", adopted
    assert adopted["provenance"]["last_modified_by"] == "cli", adopted
    assert adopted["provenance"]["last_modified_keys"] == removed, adopted
    written = surface.configuration()
    assert {key: value_at(written, key) for key in removed} == settled_values, "adoption put back other values than the attached hardware matched before"
    assert written["target"]["controller"] == configured
    assert hashlib.sha256(bench.config.read_bytes()).hexdigest() == session_file, "the session's own configuration was written"

    status, report = surface.document("test-reactor", "--test-config", banner_plan(bench, "banner-after-adoption-on-the-command-line"))
    assert status == 0, report
    assert report["ok"] is True, report


def test_project_config_adopt_hardware_over_mcp_puts_back_the_port_and_the_reloaded_server_reads_the_board(
    bench: Bench, firmware: Path, surfaces: Surfaces
) -> None:
    """The MCP door of the same write, into a file whose port was unbound when the server started.

    The server starts on a trimmed copy, so the port it loaded names no device.
    `project_config_adopt_hardware` with `apply` fills the keys in from the
    attached bench, under the agent's provenance, keeping the controller; the
    description reload adopts the port, and a COM session through it reads the
    banner the board prints after a reset.
    """
    surface = variant(bench, "adopt-over-mcp")
    configured = surface.configuration()["target"]["controller"]
    status, planned = surface.document("adopt-hardware", "--dry-run")
    assert status == 0 and planned["ok"] is True, planned
    removed = removable_keys(planned)
    port = planned["com_port_id"]
    expected = sorted({*removed, *(row["key"] for row in planned["carried"])})
    rewrite(surface.config, without(removed))

    server = surfaces.start(surface)
    adopted = server.call("project_config_adopt_hardware", {"apply": True})
    assert adopted["ok"] is True and adopted["applied"] is True, adopted
    assert sorted(row["key"] for row in adopted["carried"]) == expected, adopted
    assert f"debuggers.{adopted['debugger_id']}.probe_id" in {row["key"] for row in adopted["already_current"]}, adopted
    kept_controller(adopted, configured)
    assert adopted["created_entries"] == [] and adopted["permissions_changed"] == [], adopted
    assert adopted["reload_required"] is True, adopted
    assert adopted["provenance"]["last_modified_via"] == "mcp:project_config_adopt_hardware", adopted
    assert adopted["provenance"]["last_modified_by"] == "agent", adopted
    assert adopted["provenance"]["last_modified_keys"] == expected, adopted
    assert surface.configuration()["target"]["controller"] == configured

    reloaded = server.call("project_config_reload_description")
    assert reloaded["ok"] is True, reloaded
    assert any(path.startswith(f"com_ports.{port}.") for path in reloaded["description_changes"]), reloaded
    assert reloaded["permission_differences"] == [], reloaded

    opened = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert opened["ok"] is True, opened
    assert BANNER in banner_after_reset(server, port)
    stopped = server.call("com_session_stop", {"port_id": port})
    assert stopped["ok"] is True, stopped


def test_adoption_is_refused_by_the_hold_while_a_session_holds_the_board_and_says_how_to_end_it(
    bench: Bench, gdb: None, firmware: Path, surfaces: Surfaces
) -> None:
    """`project_config_adopt_hardware` under an open COM session and an open debug session.

    Adoption reads the board before it writes, so a hold refuses it before
    discovery, dry run and apply alike, with the same refusal every other
    configuration write gives under a hold: the holds, the ordered remediation
    naming the call that ends this one, and the wrong fix to avoid. The command
    line, a second process, meets the lease the debug session holds. Nothing is
    written, the line keeps reading and the core stays halted.
    """
    surface = variant(bench, "adopt-under-a-hold")
    port = bench.com_port_name()
    server = surfaces.start(surface)
    left = surface.digest()

    opened = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert opened["ok"] is True, opened
    for arguments in ({}, {"apply": True}):
        refused = server.call("project_config_adopt_hardware", arguments)
        assert_refused_by_the_hold(refused, "config_write_in_open_run", "com_session_stop")
    assert surface.digest() == left
    assert BANNER in banner_after_reset(server, port), "the COM session stopped reading the board after adoption was refused"
    stopped = server.call("com_session_stop", {"port_id": port})
    assert stopped["ok"] is True, stopped

    surfaces.debug(server, firmware.relative_to(bench.project).as_posix())
    halted_at = counter(server)
    for arguments in ({}, {"apply": True}):
        refused = server.call("project_config_adopt_hardware", arguments)
        assert_refused_by_the_hold(refused, "config_write_in_open_run", "debug_stop_session")
    status, refused = surface.document("adopt-hardware")
    assert status != 0, refused
    assert refused["ok"] is False, refused
    assert refused["error_type"] in {"resource_busy", "device_busy"}, refused
    assert refused["side_effect_committed"] is False, refused
    assert counter(server) == halted_at, "the core moved while the debug session held it"
    assert surface.digest() == left
    surfaces.end_debugging(server)


# ---------------------------------------------------------------------------
# server_upgrade under a hold.
#
# The one tool in this file that would change the installation rather than a
# file. `server_upgrade` answers three gates before it hands anything to a
# package manager: the permission, then the platform, then whether anything
# holds the bench, each a returned refusal. The refusal under test is the third,
# so what it proves is that a held bench stops the call there. Should it ever
# not, the server asked here has nothing a package manager could reach: every
# index and proxy is a closed loopback port, pip reads none of its own
# configuration files, no manager but this interpreter's own pip is on PATH, and
# pip's cache and scratch are under this test's temporary directory. A child
# started in exactly that environment establishes, before the server is, that
# the manager the installation would go to is the pip of this checkout's own
# virtual environment and that no launcher anywhere else would be moved, and the
# environment's distribution records and launchers are compared before and after.

PACKAGE_MANAGERS = ("uv", "uvx", "pipx")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

WHERE_AN_UPGRADE_WOULD_GO = """
import json, os, shutil, sys
from pathlib import Path
from agentic_hil import upgrade
from agentic_hil.config import ConfigError
try:
    manager, command = upgrade._upgrade_command()
except ConfigError as error:
    manager, command = "refused:" + str(error.to_dict().get("error_type")), []
directory = upgrade._manager_bin_directory()
dedicated = upgrade._dedicated_environment_root()
print(json.dumps({
    "prefix": str(Path(sys.prefix).resolve()),
    "base_prefix": str(Path(sys.base_prefix).resolve()),
    "executable": sys.executable,
    "manager": manager,
    "command": command,
    "locks_running_files": upgrade._host_locks_running_files(),
    "launcher_directory": None if directory is None else str(directory),
    "dedicated_environment": None if dedicated is None else str(dedicated),
    "managers_on_path": [name for name in sys.argv[1:] if shutil.which(name)],
    "no_index": os.environ.get("PIP_NO_INDEX"),
    "no_pip_configuration": os.environ.get("PIP_CONFIG_FILE") == os.devnull,
}))
"""


def a_closed_local_port() -> int:
    """A loopback port nothing listened on a moment ago: bound, read and given back."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def with_nothing_to_install_from(environment: dict[str, str], scratch: Path) -> dict[str, str]:
    """An environment in which a package manager finds no index, no configuration and no second manager.

    Every variable pip, uv or pipx would read is dropped, and the indexes and
    proxies come back as a closed loopback port. `PIP_NO_INDEX` stops pip asking
    an index at all, `PIP_CONFIG_FILE` on the null device stops it reading any
    configuration file, and `PIP_REQUIRE_VIRTUALENV` stops it outside one. PATH
    loses every directory that holds uv, uvx or pipx, and pip's cache and every
    temporary file go under this test's own directory.
    """
    closed = f"http://127.0.0.1:{a_closed_local_port()}"
    kept = {
        name: value
        for name, value in environment.items()
        if not name.upper().startswith(("PIP_", "UV_", "PIPX_")) and name.upper() not in PROXY_VARIABLES
    }
    searched = [entry for entry in kept.get("PATH", "").split(os.pathsep) if entry and not any((Path(entry) / name).exists() for name in PACKAGE_MANAGERS)]
    cache = scratch / "package-manager-cache"
    temporary = scratch / "package-manager-tmp"
    cache.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    return {
        **kept,
        "PATH": os.pathsep.join(searched),
        "PIP_NO_INDEX": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_INDEX_URL": f"{closed}/simple",
        "PIP_EXTRA_INDEX_URL": f"{closed}/simple",
        "PIP_CACHE_DIR": str(cache),
        "UV_OFFLINE": "1",
        "UV_NO_CONFIG": "1",
        "UV_DEFAULT_INDEX": f"{closed}/simple",
        "UV_INDEX_URL": f"{closed}/simple",
        **{name: closed for name in PROXY_VARIABLES},
        **{name.lower(): closed for name in PROXY_VARIABLES},
        "TMPDIR": str(temporary),
    }


def an_upgrade_could_reach_only_this_checkout(surface: Surface) -> None:
    """Fail before any server starts unless the upgrade path, taken wrongly, stays inside this checkout.

    Asked of the product in a child started exactly as the server will be. The
    installation must belong to this checkout's own virtual environment, the
    manager must be that environment's pip without `--user` (or a refusal to
    pick one), no launcher directory outside the environment may be in play,
    this host must be one where no launcher is renamed or swept, and no second
    manager may be reachable.
    """
    answered = subprocess.run(
        [sys.executable, "-s", "-c", WHERE_AN_UPGRADE_WOULD_GO, *PACKAGE_MANAGERS],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=str(surface.project),
        env=surface.environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    assert answered.returncode == 0, answered.stderr
    facts = json.loads(answered.stdout)
    prefix = Path(facts["prefix"])
    command = facts["command"]
    through_this_pip = facts["manager"] == "pip" and command[:5] == [facts["executable"], "-m", "pip", "install", "--upgrade"] and "--user" not in command
    confined = {
        "the installation is this checkout's own virtual environment": prefix != Path(facts["base_prefix"]) and prefix.is_relative_to(REPOSITORY_ROOT) and prefix == Path(sys.prefix).resolve(),
        "the manager is that environment's own pip, or none is picked": through_this_pip or str(facts["manager"]).startswith("refused:"),
        "no launcher is renamed or swept on this host": facts["locks_running_files"] is False,
        "no launcher directory outside the environment is in play": facts["launcher_directory"] is None and facts["dedicated_environment"] is None,
        "no other package manager is on PATH": facts["managers_on_path"] == [],
        "pip asks no index and reads no configuration file": facts["no_index"] == "1" and facts["no_pip_configuration"] is True,
    }
    unmet = [claim for claim, holds in confined.items() if not holds]
    if unmet:
        pytest.fail(f"server_upgrade is not asked on this bench, because a wrong answer could reach outside this checkout: {unmet}", pytrace=False)


def installation_record() -> dict[str, int]:
    """This environment's distribution records and launchers, each with its modification time.

    What a package manager rewrites when it installs, upgrades or removes a
    distribution here, and what nothing else here writes: every `.dist-info`
    directory and its files, the `.pth` files beside them, and the launchers in
    `bin`. The server under test runs out of this same environment.
    """
    prefix = Path(sys.prefix)
    record: dict[str, int] = {}
    for pattern in ("bin/*", "lib/python*/site-packages/*.dist-info", "lib/python*/site-packages/*.dist-info/*", "lib/python*/site-packages/*.pth"):
        for path in prefix.glob(pattern):
            with suppress(OSError):
                record[path.relative_to(prefix).as_posix()] = path.lstat().st_mtime_ns
    assert any(name.endswith(".dist-info") and "/agentic_hil-" in name for name in record), "the record does not cover the installation it is meant to watch"
    return record


def upgrade_refused_by_the_hold(server: Server, surface: Surface, stop_call: str) -> dict:
    """`server_upgrade` while a hold stands: refused naming what is held, and nothing started.

    The hold is read from a second process first, and the call is made only
    when that reading says the bench is held and names what holds it: this test
    never asks for an upgrade on a bench it has not seen held.
    """
    _, outside = surface.document("lease-status")
    assert outside["ok"] is True, outside
    if outside.get("bench_held") is not True or not outside.get("held_devices"):
        pytest.fail(f"the bench does not read as held from a second process, so server_upgrade is not asked: {outside.get('summary')}", pytrace=False)
    held = set(outside["held_devices"])

    refused = server.call("server_upgrade")
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "upgrade_in_open_run", refused
    assert refused["held_devices"] and set(refused["held_devices"]) == held, refused
    assert {hold.get("resource") for hold in refused["device_holds"]} == held, refused
    assert stop_call in json.dumps(refused.get("remediation")), refused
    assert refused.get("do_not"), refused
    assert refused["retry_safe"] is True, refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["side_effect_status"] == "not_started", refused
    assert isinstance(refused.get("running_version"), str) and refused["running_version"], refused
    for installed in ("upgraded_on_disk", "install", "manager", "command", "restart_required"):
        assert installed not in refused, refused
    return refused


@pytest.mark.skipif(os.name == "nt", reason="on Windows server_upgrade answers upgrade_cli_only_on_host before it reads the bench")
def test_server_upgrade_is_refused_while_a_session_or_a_declared_run_holds_the_board_and_installs_nothing(
    bench: Bench, gdb: None, firmware: Path, surfaces: Surfaces, tmp_path: Path
) -> None:
    """An open COM session, a debug session and a declared run each refuse `server_upgrade` by name.

    Each hold is read from a second process before the call, and the refusal
    names exactly the devices that reading found held, the call that ends this
    hold, and that nothing was started. The holder is not disturbed: the port
    still reads the banner, the core stays halted, the run is still declared.
    The environment's distribution records and launchers are the same after all
    three refusals as before them.
    """
    surface = variant(bench, "upgrade-under-a-hold", rights=("allow_upgrade",))
    surface = replace(surface, environment=with_nothing_to_install_from(surface.environment, tmp_path))
    an_upgrade_could_reach_only_this_checkout(surface)
    before = installation_record()
    port = bench.com_port_name()
    server = surfaces.start(surface)

    opened = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert opened["ok"] is True, opened
    upgrade_refused_by_the_hold(server, surface, "com_session_stop")
    assert BANNER in banner_after_reset(server, port), "the COM session stopped reading the board after server_upgrade was refused"
    stopped = server.call("com_session_stop", {"port_id": port})
    assert stopped["ok"] is True, stopped

    surfaces.debug(server, firmware.relative_to(bench.project).as_posix())
    halted_at = counter(server)
    upgrade_refused_by_the_hold(server, surface, "debug_stop_session")
    assert counter(server) == halted_at, "the core moved while the debug session held it"
    surfaces.end_debugging(server)

    devices = [{"kind": "debugger", "id": bench.debugger_name()}, {"kind": "uart", "id": port}]
    started = server.call("bench_run_start", {"devices": devices, "label": "upgrade-under-a-run"})
    if started.get("ok") is True:
        surfaces.running.append(server)
    assert started["ok"] is True, started
    declared = started["declared_devices"]
    refused = upgrade_refused_by_the_hold(server, surface, "bench_run_stop")
    assert set(declared) <= set(refused["held_devices"]), refused
    still = server.call("bench_run_status")
    assert still["run_active"] is True and still["declared_devices"] == declared, still
    stopped = server.call("bench_run_stop")
    assert stopped["ok"] is True, stopped
    surfaces.running.remove(server)

    assert installation_record() == before, "the environment's distributions or launchers changed across the refused upgrades"
    _, free = surface.document("lease-status")
    assert free["bench_held"] is False and free["held_devices"] == [], free
