"""What the product says about the probe in front of it, and about the bench around it.

Discovery is the half of this bridge that answers before anything is driven: which
probe is attached, which backend will drive it, which document decided that, which
serial ports this host publishes, and whether the plans in this repository can even
be loaded against what the configuration declares. None of it can be established
without hardware. A probe listing is a reading of this host's USB inventory; a
`probe_target` is OpenOCD opening an adapter and examining a core; `doctor` is the
one answer an operator compares a running server against; and `adopt-hardware
--dry-run` is a read of the attached board held up against a file nobody wants
rewritten by accident.

Two surfaces are exercised and no third. The CLI is driven the way the rest of this
tier drives it, and the MCP server is driven as a live `agentic-hil mcp-stdio`
process spoken to in JSON-RPC lines, because `probe_target` and `debugger_info`
have no command of their own and an agent reaches them nowhere else. Nothing here
runs openocd, opens a serial device or touches a CAN interface: a test that reached
the board past the product would be measuring something other than the product.

Nothing here asserts a value that identifies this machine. Probe serials, port
device names, executables and paths are read out of the product's own answers and
compared against each other or asserted for shape; they are never written down.
These files are public and the bench is not.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, child_command, isolated_environment

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The protocol version this client asks for. The server negotiates and answers
# with one it supports, so this is a request and never an assertion.
CLIENT_PROTOCOL_VERSION = "2025-06-18"

# Long enough for OpenOCD to open an adapter, examine a core and shut down on a
# slow link, short enough that a wedged probe fails this file rather than holding
# the session open until somebody notices.
RESPONSE_TIMEOUT_S = 300.0
SHUTDOWN_TIMEOUT_S = 60.0

# How much of the server's own error output a failure here carries. Kept rather
# than read on demand: a pipe nobody empties fills and stops the process it
# belongs to, and a read of one whose writer is still alive never returns.
STDERR_TAIL_LINES = 40

# The plan this project declares, named rather than discovered by globbing the
# project directory: other modules in this tier write plans of their own into the
# same copy, and a test that swept them up would pass or fail by collection order.
DEMO_PLAN = "testconfig.yaml"
DEMO_PLAN_NAME = "nucleo-f446re-hello-world"
DEMO_PLAN_STEPS = 4

# A serial no probe answers to. Made up here on purpose: the point of the test
# that uses it is that the configuration names hardware that is not there, and a
# value read off this bench would be the opposite of that.
NO_SUCH_PROBE = "agentic-hil-bench-no-probe-answers-to-this"

# What makes one row of a host's serial inventory a device somebody plugged in, as
# the rendering itself decides it. Written out here rather than imported, the way
# this tier writes out the words a failure is read from: the claim under test is
# that the rendering keeps every identified port and collapses the rest, and a test
# that asked the renderer which rows those are would be asking the code under test
# to grade itself.
USB_IDENTITY_KEYS = ("vid", "pid", "serial_number")

# The one line that stands for every port with no USB identity. Matched against the
# whole rendering with its whitespace normalised, because the line is wrapped to the
# terminal width and the stretch it names can straddle the break.
COLLAPSED_PORTS = re.compile(r"(\d+) legacy serial ports? without a USB identity \(([^)]*)\) not listed")


def environment_for(bench: Bench, config: Path) -> dict[str, str]:
    """This tier's environment, pointed at one named configuration.

    `Bench.run` already pins `AGENTIC_HIL_CONFIG` to the session's own file and
    cannot be told to use another, so the one test here that runs against a second
    configuration builds the environment from the same helper the fixture does.
    """
    return isolated_environment(bench.config_root, bench.state_root, AGENTIC_HIL_CONFIG=str(config))


def run_against(bench: Bench, config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """The CLI under test, in this project, against the configuration named."""
    return subprocess.run(
        child_command(*arguments),
        capture_output=True,
        text=True,
        cwd=str(bench.project),
        env=environment_for(bench, config),
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def document_against(bench: Bench, config: Path, *arguments: str) -> tuple[int, dict]:
    """One command's machine document against a named configuration, with its status."""
    answered = run_against(bench, config, *arguments, "--json")
    assert answered.stdout.strip(), f"{arguments} printed no document (exit {answered.returncode}):\n{answered.stderr}"
    return answered.returncode, json.loads(answered.stdout)


def read_line(stream, timeout_s: float, method: str) -> str:
    """One line off the server's stdout, or a failure that names what was waited for.

    A blocking `readline` on a server that has stopped answering would hold the
    whole session, and this tier runs on a machine somebody owns. The reader is a
    daemon thread so a timeout leaves nothing to join; the process it is reading is
    killed by the fixture that started it.
    """
    box: queue.Queue[str] = queue.Queue(maxsize=1)

    def read_one() -> None:
        try:
            box.put(stream.readline())
        except (OSError, ValueError):
            # The fixture closed or killed this process while the reader was
            # waiting on it. An empty line is what a closed stream means to
            # every caller below, and raising here would print a thread
            # traceback over whatever the test was actually failing on.
            box.put("")

    threading.Thread(target=read_one, daemon=True).start()
    try:
        return box.get(timeout=timeout_s)
    except queue.Empty:
        raise AssertionError(f"the MCP server did not answer `{method}` within {timeout_s:.0f}s") from None


class Server:
    """One live `agentic-hil mcp-stdio`, spoken to the way an agent host speaks to it.

    Line-delimited JSON-RPC over stdin and stdout, which is the transport the
    product implements, and no shortcut past it: the service is constructed by the
    server process out of the configuration it loads at startup, so a test that
    imported the tool service would be measuring a different object than the one an
    agent reaches.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._next_id = 0
        self._closed = False
        # Emptied for the whole life of the process, never on demand. A server
        # whose stderr pipe nobody drains stops when the pipe fills, and a
        # `read()` of one whose writer is still running does not return: both of
        # those turn a failing assertion into a session that hangs on a bench
        # somebody owns.
        self._errors: list[str] = []
        threading.Thread(target=self._drain_errors, daemon=True).start()

    def _drain_errors(self) -> None:
        stream = self.process.stderr
        if stream is None:  # pragma: no cover - this tier always pipes stderr
            return
        with suppress(OSError, ValueError):
            for line in stream:
                self._errors.append(line)

    def errors(self) -> str:
        """The tail of what this server wrote to stderr, without waiting on it."""
        return "".join(self._errors[-STDERR_TAIL_LINES:])

    @classmethod
    def start(cls, bench: Bench, config: Path) -> Server:
        process = subprocess.Popen(
            child_command("mcp-stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(bench.project),
            env=environment_for(bench, config),
        )
        server = cls(process)
        handshake = server.request(
            "initialize",
            {"protocolVersion": CLIENT_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "bench-discovery", "version": "0"}},
        )
        assert handshake["serverInfo"]["name"] == "agentic-hil", handshake
        return server

    def request(self, method: str, params: dict) -> dict:
        if self.process.poll() is not None:
            raise AssertionError(f"the MCP server exited before `{method}` with status {self.process.returncode}:\n{self.errors()}")
        self._next_id += 1
        request_id = self._next_id
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        line = read_line(self.process.stdout, RESPONSE_TIMEOUT_S, method)
        assert line.strip(), f"the MCP server closed its output during `{method}`:\n{self.errors()}"
        message = json.loads(line)
        assert "error" not in message, message
        assert message.get("id") == request_id, message
        return message["result"]

    def call(self, tool: str, arguments: dict | None = None) -> tuple[bool, dict]:
        """One tool call, as the error flag the host reads and the structured result."""
        answered = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        return bool(answered["isError"]), answered["structuredContent"]

    def close(self) -> None:
        """Idempotent, and it runs whether the test passed or failed."""
        if self._closed:
            return
        self._closed = True
        process = self.process
        try:
            if process.poll() is None:
                with suppress(OSError, ValueError):
                    process.stdin.close()
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with suppress(OSError, ValueError):
                        stream.close()


@pytest.fixture()
def servers(bench: Bench) -> Iterator[Callable[..., Server]]:
    """Start MCP servers, and close every one of them when the test ends.

    A factory rather than one server, because one test here runs a server against a
    second configuration. The teardown runs on a failed test too, which is the
    point: a server left running holds an open tool service against this bench.
    """
    started: list[Server] = []

    def start(config: Path | None = None) -> Server:
        server = Server.start(bench, config or bench.config)
        started.append(server)
        return server

    try:
        yield start
    finally:
        for server in reversed(started):
            server.close()


@pytest.fixture(autouse=True)
def the_bench_is_left_clear(bench: Bench) -> Iterator[None]:
    """Any quarantine a test in this file raises is cleared by this file.

    Two tests here reach the probe through the product, and one of them does it
    with a configuration naming hardware that is not attached; a backend that could
    not prove where it stopped quarantines the bench, and a quarantined bench
    refuses every test after it. So the standing incident, which is what
    `agentic-hil recover` exists for and the only thing it clears, is read off
    `lease-status` and cleared here, at the operator's own command line, on this
    tier's own configuration. A recovery refused because the incident was recorded
    against the second configuration is retried with the operator override that
    says so, because both files describe this same bench.
    """
    yield
    _, lease = bench.document("lease-status")
    if lease.get("incident_stands") is not True:
        return
    quarantine_id = lease.get("quarantine_id")
    assert isinstance(quarantine_id, str) and quarantine_id, lease
    recovery = ("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
    _, cleared = bench.document(*recovery)
    if cleared.get("error_type") == "config_changed":
        _, cleared = bench.document(*recovery, "--accept-config-change")
    _, after = bench.document("lease-status")
    assert after.get("incident_stands") is not True, (cleared, after)


@pytest.fixture()
def probe_id_that_matches_nothing(bench: Bench) -> Iterator[tuple[Path, str]]:
    """This session's configuration with one field changed: a serial nothing answers to.

    A copy, beside the file `init` wrote and never that file, so the session's own
    configuration is what every other test still reads. It is removed again whether
    the test passed or failed.
    """
    document = yaml.safe_load(bench.config.read_text(encoding="utf-8"))
    name = sorted(document["debuggers"])[0]
    document["debuggers"][name]["probe_id"] = NO_SUCH_PROBE
    variant = bench.config.parent / "bench-discovery-probe-id-mismatch.yaml"
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    try:
        yield variant, name
    finally:
        variant.unlink(missing_ok=True)


def configured_debugger(bench: Bench) -> tuple[str, dict]:
    """The entry this bench drives, out of the configuration `init` wrote.

    The probe serial is checked for shape here rather than in each test that
    reads it: `init` binds the probe it enumerated, so an entry that carries none
    is a bench this tier's own setup did not finish, and a test comparing against
    a missing value would report that as a probe that stopped being listed.
    """
    name = bench.debugger_name()
    entry = bench.configuration()["debuggers"][name]
    probe_id = entry.get("probe_id")
    assert isinstance(probe_id, str) and probe_id.strip(), f"`init` bound no probe serial to `debuggers.{name}`"
    return name, entry


def workspace_path(bench: Bench, reported: str) -> Path:
    """A path a result reported, resolved the way a reader of that result would."""
    candidate = Path(reported)
    return candidate if candidate.is_absolute() else bench.project / candidate


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def has_usb_identity(port: dict) -> bool:
    return any(port.get(key) not in (None, "") for key in USB_IDENTITY_KEYS)


def test_probe_target_confirms_a_detection_over_the_attached_probe_and_holds_no_device_afterwards(bench: Bench, servers) -> None:
    """The one call that opens the adapter and examines the core, read off the agent's surface.

    Catches a probe read that reports a detection its backend never confirmed (an
    exit status taken for a verdict, which is the whole reason the success marker
    exists), and one that answers and then keeps the board: `probe_target` is a
    one-shot, so the device it took has to be back before the next call asks for it.
    """
    name, entry = configured_debugger(bench)
    server = servers()

    failed, result = server.call("probe_target")

    assert failed is False, result
    assert result["ok"] is True, result
    assert result["tool"] == "probe_target", result
    assert result["backend"] == entry["type"], result
    assert result["target_detected"] is True, result
    assert result["success_confirmed"] is True, result
    assert "error_type" not in result, result
    assert result.get("quarantined") is not True, result
    assert result.get("audit_ok") is not False, result
    assert isinstance(result["elapsed_ms"], int), result
    for field in ("started_at", "finished_at", "log_path"):
        assert isinstance(result[field], str) and result[field].strip(), (field, result)

    server.close()

    _, lease = bench.document("lease-status")
    assert lease["held_devices"] == [], (name, lease)
    assert lease["device_holds"] == [], lease


def test_two_probe_reads_in_one_session_each_leave_their_own_debugger_log(bench: Bench, servers) -> None:
    """The second read is a read, not a replay of the first.

    Catches a probe path that answers the second call out of the first one's
    evidence, or writes both runs over one log file, either of which leaves an
    operator diagnosing a bench with a transcript that is not the run's.
    """
    server = servers()

    first_failed, first = server.call("probe_target")
    second_failed, second = server.call("probe_target")

    assert (first_failed, second_failed) == (False, False), (first, second)
    assert first["ok"] is True, first
    assert second["ok"] is True, second
    assert first["log_path"] != second["log_path"], (first["log_path"], second["log_path"])
    for result in (first, second):
        assert workspace_path(bench, result["log_path"]).is_file(), result["log_path"]


def test_debugger_info_names_the_backend_the_probe_and_the_document_it_was_decided_by(bench: Bench, servers) -> None:
    """The availability answer, and the configuration block it is only useful beside.

    Catches an answer that reports a backend or a probe other than the one the file
    in force names, and one whose configuration block claims the file is unchanged
    without having compared it: the two digests have to be the same bytes, and the
    state has to say so, or the operator comparing this against `doctor` is
    comparing two claims about different documents.
    """
    name, entry = configured_debugger(bench)
    server = servers()

    failed, result = server.call("debugger_info")

    assert failed is False, result
    assert result["ok"] is True, result
    assert result["tool"] == "debugger_info", result
    assert result["backend"] == entry["type"], (name, result)
    assert result["probe_id"] == entry["probe_id"], result
    assert isinstance(result["probe_id"], str) and result["probe_id"].strip(), result
    for field in ("executable", "version", "summary"):
        assert isinstance(result[field], str) and result[field].strip(), (field, result)

    status = result["config_status"]
    assert Path(status["path"]).resolve() == bench.config.resolve(), status
    assert status["state"] == "unchanged", status
    assert status["loaded_digest"] == status["current_digest"], status
    assert isinstance(status["loaded_digest"], str) and status["loaded_digest"].strip(), status
    assert status["reload_required"] is False, status
    assert status["description_source"] == "startup", status
    assert "config_stale" not in result, result


def test_doctor_on_the_configured_bench_exits_zero_and_names_the_checks_that_decided_it(bench: Bench) -> None:
    """The verdict a newcomer and `setup` both read, with the sections it was made of.

    Catches a green verdict pronounced over a check that is red: `unhealthy` is the
    list of what decided `ok`, so an empty list beside a failed debugger check, an
    unbound device or a refused state root is the defect this field exists to make
    impossible to hide. It also catches the exit status coming apart from the
    document, which is what a shell script and an agent read respectively.
    """
    rendered = bench.run("doctor")
    status, report = bench.document("doctor")

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert not rendered.stdout.startswith("Failed:"), rendered.stdout[:400]
    assert not rendered.stdout.startswith("Refused:"), rendered.stdout[:400]

    assert status == 0, report
    assert report["ok"] is True, report
    assert report["unhealthy"] == [], report
    assert report["state_root"]["ok"] is True, report["state_root"]
    assert report["bench_binding"]["ok"] is True, report["bench_binding"]
    assert report["bench_binding"]["unbound"] == [], report["bench_binding"]

    debuggers = report["debuggers"]
    assert debuggers, report
    assert [name for name, entry in debuggers.items() if entry["bound"]] == [bench.debugger_name()], debuggers
    for name, entry in debuggers.items():
        # Named rather than read straight through: an entry doctor never checked
        # carries no `check` at all, and a verdict pronounced over an unchecked
        # probe is the thing this assertion is here to catch.
        assert "check" in entry, (name, entry)
        assert entry["check"]["ok"] is True, (name, entry["check"])
        assert entry["target_support"]["ok"] is True, (name, entry["target_support"])
        assert isinstance(entry["probe_id"], str) and entry["probe_id"].strip(), (name, entry)

    assert report["debugger"]["tool"] == "debugger_info", report["debugger"]
    assert report["debugger"]["ok"] is True, report["debugger"]
    assert report["target"]["controller"] not in (None, "", "unknown-controller"), report["target"]
    assert report["mcp"]["transport"] == "stdio", report["mcp"]
    assert report["mcp"]["args"] == ["mcp-stdio"], report["mcp"]
    assert isinstance(report["installation"]["version"], str) and report["installation"]["version"].strip(), report["installation"]
    assert report["config_status"]["state"] == "unchanged", report["config_status"]


def test_doctor_and_a_running_server_name_the_same_backend_and_the_same_document(bench: Bench, servers) -> None:
    """The disagreement the configuration block was added for, asked of both surfaces at once.

    `doctor` reads the file every time it runs; the server answers out of the
    document it parsed at startup. They came apart silently once, naming two
    different backends in the same minute with nothing anywhere to say which was
    which. Catches that regression: with the file untouched between the two calls,
    the backend, the probe and the digest have to be the same on both.
    """
    server = servers()

    failed, from_server = server.call("debugger_info")
    status, from_doctor = bench.document("doctor")

    assert failed is False, from_server
    assert status == 0, from_doctor
    assert from_doctor["debugger"]["backend"] == from_server["backend"], (from_doctor["debugger"], from_server)
    assert from_doctor["debugger"]["probe_id"] == from_server["probe_id"], (from_doctor["debugger"], from_server)
    assert from_doctor["config_status"]["loaded_digest"] == from_server["config_status"]["loaded_digest"], (from_doctor["config_status"], from_server["config_status"])
    assert Path(from_doctor["config_status"]["path"]).resolve() == Path(from_server["config_status"]["path"]).resolve(), (from_doctor["config_status"], from_server["config_status"])


def test_a_probe_id_nothing_answers_to_leaves_doctor_green_and_is_refused_where_the_adapter_is_opened(bench: Bench, servers, probe_id_that_matches_nothing) -> None:
    """Which surface is entitled to say the named probe is not there, and which is not.

    `doctor` checks this host's toolchain and this file's description; it says
    nothing to a board, so a serial nothing answers to has to leave it green. That
    is not a shrug: `setup` rolls a freshly written configuration back when `doctor`
    fails, so a red verdict here would delete a good file over a board that is
    merely unplugged. The refusal belongs to the first call that opens the adapter,
    and it has to be the refusal that proves it never reached the target, or the
    bench is quarantined over hardware the call never touched.

    Catches both halves: a `doctor` that fails over a probe it never addressed, and
    a probe read whose classification cannot tell an adapter it could not open from
    an unknown debugger error. This test can leave the bench quarantined when that
    second half regresses, and the module's `the_bench_is_left_clear` fixture clears
    it through `agentic-hil recover`.
    """
    config, name = probe_id_that_matches_nothing

    rendered = run_against(bench, config, "doctor")
    status, report = document_against(bench, config, "doctor")

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert status == 0, report
    assert report["ok"] is True, report
    assert report["unhealthy"] == [], report
    assert report["debuggers"][name]["probe_id"] == NO_SUCH_PROBE, report["debuggers"][name]
    assert report["debuggers"][name]["check"]["ok"] is True, report["debuggers"][name]["check"]

    failed, result = servers(config).call("probe_target")

    assert failed is True, result
    assert result["ok"] is False, result
    assert result["tool"] == "probe_target", result
    assert result["error_type"] == "adapter_not_found", result
    assert result["target_contacted"] is False, result
    assert result["side_effect_committed"] is False, result
    assert result["side_effect_status"] == "not_started", result
    assert result["hardware_state"] == "unchanged", result
    assert result["retry_safe"] is True, result
    assert result.get("quarantined") is not True, result
    assert result["likely_causes"], result


def test_the_probe_listing_names_the_configured_probe_and_says_nothing_was_asked_of_a_board(bench: Bench, servers) -> None:
    """The enumeration a multi-board configuration is written from, on the agent's surface.

    Two claims. The serial this configuration was written with is still one this
    host enumerates, which is the fact that makes the file describe the board in
    front of it. And the listing states what it did to get there: it read a USB
    inventory and said nothing to a target, so it commits no side effect and is safe
    to repeat. Catches a listing that quietly stops reporting the bound probe, and
    one that claims contact, or an effect, it never had.
    """
    _, entry = configured_debugger(bench)
    server = servers()

    failed, result = server.call("debugger_probes_list")

    assert failed is False, result
    assert result["ok"] is True, result
    assert result["tool"] == "debugger_probes_list", result
    probes = result["probes"]
    assert probes, "no probe was enumerated, and this tier runs only where one is attached"
    for probe in probes:
        assert isinstance(probe["probe_id"], str) and probe["probe_id"].strip(), probe
    reported = {str(probe["probe_id"]).casefold() for probe in probes}
    assert str(entry["probe_id"]).casefold() in reported, "the configured probe is not among the ids this host now enumerates"

    assert result["discovered_by"] == "usb_serial_inventory", result
    assert isinstance(result["stlink_ports"], list), result
    assert result["complete"] is False, result
    assert result["target_contacted"] is False, result
    assert result["side_effect_committed"] is False, result
    assert result["side_effect_status"] == "not_started", result
    assert result["retry_safe"] is True, result


def test_the_command_line_and_the_server_enumerate_the_same_probes_for_this_host(bench: Bench, servers) -> None:
    """One question about one host, asked of both surfaces.

    The operator reads probe ids off `agentic-hil debugger-probes` and the agent
    reads them off `debugger_probes_list`, and they are the same enumeration only as
    long as nobody grows a second one beside it. Catches the two answering
    differently about the same bench, which is how an operator ends up configuring a
    serial only one of them can see, and catches the completeness disclaimer being
    dropped on the way through the command line.
    """
    server = servers()

    status, from_cli = bench.document("debugger-probes")
    failed, from_server = server.call("debugger_probes_list")

    assert status == 0, from_cli
    assert failed is False, from_server
    assert {probe["probe_id"] for probe in from_cli["probes"]} == {probe["probe_id"] for probe in from_server["probes"]}, (from_cli["probes"], from_server["probes"])
    assert from_cli["backend"] == from_server["backend"], (from_cli, from_server)
    assert from_cli["complete"] == from_server["complete"], (from_cli, from_server)


def test_adopt_hardware_dry_run_writes_nothing_and_shows_both_sides_of_every_key_it_compared(bench: Bench) -> None:
    """The read of the attached board held up against the file, with the file untouched.

    A dry run is the answer to "what would this change", and it is worth nothing
    unless two things hold. The file is byte-for-byte what it was, which is asserted
    over the bytes rather than over the tool's own word for it. And every key the
    plan says it compared carries both sides: what the configuration holds and what
    the board said, so a reader can see a value being kept, a placeholder being
    filled or a field already current without going and reading the YAML themselves.
    A key reported as unavailable is the one exception and is held to its own terms,
    because nothing was discovered for it to have a second side.

    Catches a dry run that writes, and a compared key that reports only one side of
    the comparison, which is the shape that made "adoption never overwrites what
    somebody chose" impossible to check from the result.
    """
    name, _ = configured_debugger(bench)
    before = digest_of(bench.config)

    status, report = bench.document("adopt-hardware", "--dry-run")

    assert digest_of(bench.config) == before, "the configuration changed under a dry run"
    assert status == 0, report
    assert report["ok"] is True, report
    assert report["applied"] is False, report
    assert Path(report["path"]).resolve() == bench.config.resolve(), report["path"]
    assert report["side_effect_status"] == "not_started", report
    assert report["cleanup_required"] is False, report
    assert report["next_steps"], report

    discovery = report["hardware_discovery"]
    assert discovery["ok"] is True, discovery
    assert isinstance(discovery["probe_id"], str) and discovery["probe_id"].strip(), discovery

    compared = [*report["carried"], *report["already_current"], *report["kept"], *report["unavailable"]]
    assert compared, "the dry run compared no key at all, so it reported nothing about this bench"
    for item in compared:
        assert isinstance(item["key"], str) and "." in item["key"], item
    for item in report["carried"]:
        assert "value" in item and "previous_value" in item, item
    for item in report["already_current"]:
        assert "value" in item, item
    for item in report["kept"]:
        assert "configured_value" in item and "discovered_value" in item, item
    for item in report["unavailable"]:
        # Not both sides here, deliberately: the reason a key is unavailable can
        # be that nothing was discovered for it at all, so a `discovered_value`
        # demanded of every entry would be a demand the honest answer cannot
        # meet. What each of them owes is why it was not carried, and the call
        # that would carry it.
        assert item["reason"], item
        assert item["next_step"], item

    # The probe serial is the key `init` wrote from this same reading, so a re-read
    # has to find it already current: reported as compared, and neither filled in
    # nor kept against a different value.
    probe_key = f"debuggers.{name}.probe_id"
    assert probe_key in {item["key"] for item in report["already_current"]}, report


def test_the_host_inventory_is_the_same_on_both_surfaces_and_publishes_the_configured_ports_device(bench: Bench, servers) -> None:
    """The port listing an operator reads, the one an agent reads, and the board's own line.

    `agentic-hil com-ports` and `com_ports_list` enumerate the same host, and the
    device this project's configuration names has to be one of the ports it
    publishes: a configured entry naming a device the host does not list is a
    session that fails at its open with nothing before it having said so. Catches
    the two surfaces enumerating differently, and a configured port that no longer
    corresponds to anything attached.
    """
    port_id = bench.com_port_name()
    configured = bench.configuration()["com_ports"][port_id]["device"]
    server = servers()

    status, from_cli = bench.document("com-ports")
    failed, listed = server.call("com_ports_list")

    assert status == 0, from_cli
    assert from_cli["ok"] is True, from_cli
    assert from_cli["tool"] == "com_ports_available", from_cli
    assert failed is False, listed
    assert listed["ok"] is True, listed
    available = listed["available_com_ports"]
    assert available["ok"] is True, available

    assert {port["device"] for port in from_cli["ports"]} == {port["device"] for port in available["ports"]}, (from_cli["ports"], available["ports"])
    assert port_id in listed["ports"], listed["ports"]

    names: set[str] = set()
    for port in from_cli["ports"]:
        assert isinstance(port["device"], str) and port["device"].strip(), port
        assert port["device"] not in names, f"one device was enumerated twice: {port['device']}"
        names.add(port["device"])
        if port.get("stable_device"):
            names.add(str(port["stable_device"]))
        if port.get("serial_number") is not None:
            assert isinstance(port["serial_number"], str) and port["serial_number"].strip(), port
        for numeric in ("vid", "pid"):
            if port.get(numeric) is not None:
                assert isinstance(port[numeric], int), port

    assert configured in names, "the device this configuration names is not among the ports this host publishes"


def test_the_ports_with_no_usb_identity_collapse_into_one_line_naming_the_stretch_it_stands_for(bench: Bench) -> None:
    """A host with thirty chipset stubs and one board, printed so the account still adds up.

    The collapsed line is the whole justification for not printing those rows: it
    has to say how many there were and which names the stretch runs between, in the
    order the host gave them, or it is an omission rather than a summary. Catches a
    count that disagrees with the document, endpoints that are not the first and
    last of the collapsed stretch, and the line going missing on a host that has
    such ports at all.
    """
    rendered = bench.run("com-ports")
    _, document = bench.document("com-ports")

    assert rendered.returncode == 0, rendered.stderr
    printed = " ".join(rendered.stdout.split())
    anonymous = [port for port in document["ports"] if not has_usb_identity(port)]
    match = COLLAPSED_PORTS.search(printed)

    if not anonymous:
        assert match is None, "a collapsed line was printed for a host that publishes no such port"
        return

    assert match is not None, printed
    assert int(match.group(1)) == len(anonymous), (match.group(0), len(anonymous))
    first, last = anonymous[0]["device"], anonymous[-1]["device"]
    expected = first if len(anonymous) == 1 else f"{first} to {last}"
    assert match.group(2) == expected, (match.group(0), expected)

    for port in document["ports"]:
        if has_usb_identity(port):
            assert port["device"] in printed, f"a port with a USB identity was left out of the rendering: {port['device']}"


def test_check_plan_accepts_the_projects_own_plan_against_the_devices_this_bench_declares(bench: Bench) -> None:
    """The board-free preflight, run on the one bench whose configuration it can compare against.

    `--strict` is the mode for the job that means this bench, and its value is
    entirely in the comparison: a plan naming `dut_uart2` where the configuration
    has `dut_uart` loads perfectly and is refused at the first step of the run.
    Catches a strict run exiting 0 without the comparison having happened, which is
    what a configuration it could not read used to look like from the outside, and
    catches the plan itself drifting away from the device names this project
    configures.
    """
    rendered = bench.run("check-plan", DEMO_PLAN, "--strict")
    status, report = bench.document("check-plan", DEMO_PLAN, "--strict")

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert not rendered.stdout.startswith("Failed:"), rendered.stdout[:400]
    assert status == 0, report
    assert report["ok"] is True, report
    assert report["strict"] is True, report
    assert report["summary"].startswith("All 1 test plan(s) load through the reactor's loader"), report["summary"]

    configuration = report["configuration"]
    assert configuration["ok"] is True, configuration
    declared = {*configuration["debuggers"], *configuration["com_ports"], *configuration["can_buses"]}
    assert declared, configuration

    assert len(report["plans"]) == 1, report["plans"]
    checked = report["plans"][0]
    assert checked["ok"] is True, checked
    assert checked["plan"] == DEMO_PLAN, checked
    assert checked["name"] == DEMO_PLAN_NAME, checked
    assert checked["steps"] == DEMO_PLAN_STEPS, checked
    assert "unconfigured_devices" not in checked, checked

    # The other half of the same claim, read off the plan rather than off the
    # command: every device the plan names is one this bench declares, so the run
    # above compared something and found it whole.
    plan = yaml.safe_load((bench.project / DEMO_PLAN).read_text(encoding="utf-8"))
    named = {str(step["device"]) for step in plan["steps"] if "device" in step}
    assert named, plan
    assert named <= declared, (sorted(named), sorted(declared))
