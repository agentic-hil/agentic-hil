"""Writing the board and restarting it, through the two surfaces that may.

Flashing has no command line of its own. An operator flashes by running a plan,
an agent flashes by calling `flash_firmware` on the MCP server, and both end in
the same backend call under the same permissions. This file drives the MCP
surface, because that is where the whole of the slice is reachable: the two ways
an image is named (a path inside the workspace, and an id the artifact store
gave back), the flag that decides whether the core is restarted afterwards, the
three refusals a bad path earns, the permission that gates each half, and the
three reset modes the schema publishes. The command line is used for what only
it can do: `grant`, `revoke`, `lease-status` and `recover`.

What is asserted is the contract and never the prose around it: the `ok`, the
`error_type`, the `permission` key, the mode the result echoes, the digest, the
first sentence of a summary where that sentence is the thing under test. Nothing
here names a host, a user, a serial or a path under a home directory. The image
is named relative to the workspace and the result is asserted to name it back
the same way, which is both the contract and the reason no absolute path is
written in this file.

The image every flash writes is the demo's own ELF, built by the `firmware`
fixture out of the copy of this repository's demo the session made. No test here
writes anything else and no test erases: there is no tool on this surface that
erases, `allow_mass_erase` stays false on this bench, and the one test that is
about erasing asserts exactly that, without opening the key.

Two things every test in this file leaves behind it, in a teardown that runs
whether the test passed or failed. A quarantine, if a call left one standing, is
cleared through `agentic-hil recover` first, because a bench holding an incident
refuses the next hardware call and every one after it. Then the core is reset
into `run`, because a flash with `reset_after_flash: false` and a reset in mode
`halt` both leave it stopped, and a stopped core prints no banner for the files
that read one.
"""

from __future__ import annotations

import contextlib
import json
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, child_command, debugger_capture, failure_worded_lines

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The protocol version this client asks for. One the server supports, so the
# negotiated version comes back unchanged and a mismatch here is about the
# server rather than about a fallback.
PROTOCOL_VERSION = "2025-06-18"

# What an uploaded artifact id has to look like: the content digest and the
# extension the file was stored under, and nothing that came out of a filename.
UPLOADED_ARTIFACT_ID = re.compile(r"^[0-9a-f]{64}\.elf$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The three modes `reset_target` publishes. Written out so a mode added to or
# dropped from the schema is a failure here, and not a test that quietly stops
# covering one.
RESET_MODES = ["run", "halt", "init"]

# Names for the files these tests put inside and outside the workspace. Prefixed
# so they cannot collide with another author's, and removed again by the fixture
# that writes them.
DISALLOWED_EXTENSION_FILE = "flash-reset-slice-not-firmware.txt"
MISSING_IMAGE = "build/Debug/flash-reset-slice-never-built.elf"
OUTSIDE_WORKSPACE_IMAGE = "flash-reset-slice-outside-workspace.elf"

# The id an artifact store never held, used only where the call is refused
# before anything is resolved.
AN_ARTIFACT_ID_NOTHING_STORED = f"{'0' * 64}.elf"

# A tool name a surface that had grown a chip erase would use. It is asked for
# so the refusal is the server's own answer and not this file's reading of a
# list it also asserts.
AN_ERASE_TOOL_WOULD_BE_CALLED = "mass_erase"

# What OpenOCD prints when the flash it was asked for returned, and what
# `success_confirmed` is that backend's claim about. Written out here rather
# than imported from the backend for the reason this tier's conftest gives about
# its own failure-word predicate: asking the code under test which line proves
# its claim is asking it to grade itself.
OPENOCD_FLASH_CONFIRMED = "AGENTIC_HIL_RESULT:flash_firmware:ok"


class ServerGone(AssertionError):
    """The MCP server stopped answering, reported with what it last said.

    Its own class so the stderr tail travels with the failure. A server that
    died on a configuration it could not load writes the reason there and
    nowhere else, and a bare timeout would send the reader to the board.
    """


class MCPServer:
    """One `agentic-hil mcp-stdio`, initialised, driven, and closed again.

    Started per test rather than per session on purpose. The server parses the
    authoritative configuration once, at startup, and does not reload it, so a
    test that revokes a permission has to start its server afterwards or it
    would be measuring the policy the previous server loaded.
    """

    def __init__(self, bench: Bench) -> None:
        self._bench = bench
        self._process: subprocess.Popen[str] | None = None
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []
        self._next_id = 0
        self.instructions = ""

    def open(self) -> MCPServer:
        self._process = subprocess.Popen(
            child_command("mcp-stdio"),
            cwd=str(self._bench.project),
            env=self._bench.environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._threads = [
            threading.Thread(target=self._read_stdout, name="bench-mcp-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="bench-mcp-stderr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        handshake = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "bench-flash-reset", "version": "1"}},
        )
        assert handshake["serverInfo"]["name"] == "agentic-hil", handshake
        assert handshake["protocolVersion"] == PROTOCOL_VERSION, handshake
        self.instructions = str(handshake.get("instructions") or "")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __enter__(self) -> MCPServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(self, method: str, params: dict | None = None) -> dict:
        """One JSON-RPC request, with the response's own id held against it."""
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self._receive()
        assert response.get("id") == request_id, (request_id, response)
        assert "error" not in response, response["error"]
        return response["result"]

    def envelope(self, tool: str, arguments: dict | None = None) -> dict:
        """The whole `tools/call` result: the content, the document, and `isError`."""
        return self.request("tools/call", {"name": tool, "arguments": arguments or {}})

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        """The tool's own document, which is what most assertions here read."""
        return self.envelope(tool, arguments)["structuredContent"]

    def tool_schema(self, tool: str) -> dict:
        listed = self.request("tools/list")["tools"]
        found = [entry for entry in listed if entry["name"] == tool]
        assert len(found) == 1, [entry["name"] for entry in listed]
        return found[0]["inputSchema"]

    def tool_names(self) -> list[str]:
        return [str(entry["name"]) for entry in self.request("tools/list")["tools"]]

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin is not None and not process.stdin.closed:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
        for thread in self._threads:
            thread.join(timeout=5)

    def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ServerGone("the MCP server was closed before this call")
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except OSError as error:
            raise ServerGone(f"the MCP server stopped reading its input: {error}\n{self._stderr_tail()}") from error

    def _receive(self) -> dict:
        try:
            line = self._stdout.get(timeout=COMMAND_TIMEOUT_S)
        except queue.Empty as error:
            raise ServerGone(f"the MCP server did not answer within {COMMAND_TIMEOUT_S:g}s\n{self._stderr_tail()}") from error
        if line is None:
            raise ServerGone(f"the MCP server closed its output\n{self._stderr_tail()}")
        return json.loads(line)

    def _stderr_tail(self) -> str:
        return "".join(self._stderr[-20:])

    def _read_stdout(self) -> None:
        process = self._process
        try:
            if process is not None and process.stdout is not None:
                for line in process.stdout:
                    if line.strip():
                        self._stdout.put(line)
        finally:
            self._stdout.put(None)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line)


def debugger_log(bench: Bench, log_path: str) -> dict:
    """The audit record one backend call wrote, out of the log the result names.

    The command it holds is what the product asked the debugger to do, which is
    the only place a flag like `reset_after_flash` can be seen having an effect
    rather than being echoed back into the answer.
    """
    return json.loads((bench.project / log_path).read_text(encoding="utf-8"))


def assert_flash_holds(result: dict) -> None:
    """The claims every confirmed flash of the demo's ELF makes, however it was named."""
    assert result["ok"] is True, result
    assert result["tool"] == "flash_firmware", result
    assert result["verify"] is True, result
    assert "error_type" not in result, result
    assert SHA256.match(str(result["artifact"]["sha256"])), result["artifact"]
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] > 0, result
    assert result["log_path"], result


@pytest.fixture(name="mcp")
def mcp_servers(bench: Bench):
    """A factory for MCP servers, and the promise that every one of them is closed.

    A factory rather than one server, because the permission tests have to start
    theirs after they have changed the configuration: the server reads it once
    and never again.
    """
    started: list[MCPServer] = []

    def start() -> MCPServer:
        server = MCPServer(bench)
        started.append(server)
        return server.open()

    yield start
    for server in reversed(started):
        server.close()


@pytest.fixture(autouse=True)
def board_left_running(bench: Bench):
    """Whatever this test did to the board, the next one finds it running the demo.

    Autouse, and set up before the fixtures a test asks for, so it tears down
    after them: after the servers a test opened are closed, and after the grant
    a permission test restored. Two things happen, in this order. A quarantine a
    call left standing is cleared through `agentic-hil recover`, because a bench
    holding an incident refuses the reset below and every later call besides.
    Then the core is reset into `run`, because a flash with `reset_after_flash:
    false` and a reset in mode `halt` both leave it stopped, and a stopped core
    prints no banner for the files that read one.

    The server this teardown starts is closed in a `finally` and not by a `with`
    over `MCPServer(bench).open()`: `open()` runs before the context manager is
    entered, so a handshake that raises there would leave a child process behind
    with nothing to close it, on the one path that exists to hand the bench back.
    """
    yield
    _, status = bench.document("lease-status")
    quarantine_id = status.get("quarantine_id")
    if quarantine_id and (status.get("blocked") or status.get("incident_stands")):
        recovered = bench.run("recover", "--confirm-safe-state", "--quarantine-id", str(quarantine_id))
        assert recovered.returncode == 0, f"this test left a quarantine that would refuse every later call:\n{recovered.stdout}\n{recovered.stderr}"
    server = MCPServer(bench)
    try:
        server.open()
        restarted = server.call("reset_target", {"mode": "run"})
    finally:
        server.close()
    assert restarted["ok"] is True, f"the board was not left running: {restarted.get('summary')}"


@pytest.fixture
def image_with_a_disallowed_extension(bench: Bench):
    """A file inside the workspace and under the allowed root, with the wrong extension."""
    path = bench.project / DISALLOWED_EXTENSION_FILE
    path.write_text("not firmware\n", encoding="utf-8")
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def image_outside_the_workspace(firmware: Path, tmp_path: Path):
    """The demo's own ELF, byte for byte, at a path outside `workspace_root`.

    The same bytes deliberately: the refusal under test has to be about where the
    file is, and not about what is in it or whether it is there at all.
    """
    outside = tmp_path / OUTSIDE_WORKSPACE_IMAGE
    shutil.copyfile(firmware, outside)
    yield outside
    outside.unlink(missing_ok=True)


def test_a_flash_by_path_is_confirmed_by_the_backend_and_carries_its_log_and_its_elapsed_time(bench: Bench, firmware: Path, mcp) -> None:
    """The fields a reviewer with no access to this bench has to read a flash out of.

    Catches a flash reported as a success on nothing but an exit status. The
    backend's own confirmation, the log the whole capture is in, and the time the
    call took are what separate a write the debugger stood behind from a process
    that returned. The path is asserted to come back exactly as it was given and
    still relative to the workspace, because a result that answered with an
    absolute path would publish this machine's directory layout into every
    report it reaches.
    """
    image = firmware.relative_to(bench.project).as_posix()

    with mcp() as server:
        envelope = server.envelope("flash_firmware", {"image_path": image, "reset_after_flash": False})
    result = envelope["structuredContent"]

    assert envelope["isError"] is False, result
    assert_flash_holds(result)
    assert result["artifact"]["source"] == "path", result["artifact"]
    assert result["artifact"]["path"] == image, result["artifact"]
    assert not Path(result["artifact"]["path"]).is_absolute(), result["artifact"]
    assert result["started_at"] <= result["finished_at"], result
    if result["backend"] == "openocd":
        # The one field that says the debugger itself reported the operation
        # returned, rather than that the process exited. It is a claim about the
        # capture, so the capture has to carry the line it was read from.
        assert result["success_confirmed"] is True, result
        assert OPENOCD_FLASH_CONFIRMED in debugger_capture(bench, result["log_path"]), debugger_log(bench, result["log_path"])["command"]


def test_a_flash_without_a_post_flash_reset_says_so_and_never_asks_the_debugger_for_one(bench: Bench, firmware: Path, mcp) -> None:
    """The default leaves the core where the write left it, and the log is the proof.

    Catches a `reset_after_flash: false` that is echoed back into the result
    while the debugger was asked to reset anyway, which boots the board before a
    plan has opened the port it is about to print on. The claim is read off the
    command the product recorded for its own audit, not off the sentence it
    wrote about it.
    """
    image = firmware.relative_to(bench.project).as_posix()

    with mcp() as server:
        result = server.call("flash_firmware", {"image_path": image, "reset_after_flash": False})

    assert_flash_holds(result)
    assert result["reset_after_flash"] is False, result
    assert result["summary"].startswith("Firmware flashed and verified. Target was not reset."), result["summary"]
    if result["backend"] == "openocd":
        commanded = debugger_log(bench, result["log_path"])["command"]
        assert "verify reset" not in commanded, commanded


def test_a_flash_that_asks_for_a_post_flash_reset_gets_one_in_the_same_debugger_command(bench: Bench, firmware: Path, mcp) -> None:
    """The flag reaches the debugger, and the summary says the target was reset.

    The mirror of the test above, and it is the pair that makes either of them
    worth running: a backend that ignored the flag, or one that always reset,
    would send the same command in both cases and fail here.
    """
    image = firmware.relative_to(bench.project).as_posix()

    with mcp() as server:
        result = server.call("flash_firmware", {"image_path": image, "reset_after_flash": True})

    assert_flash_holds(result)
    assert result["reset_after_flash"] is True, result
    assert result["summary"].startswith("Firmware flashed, verified, and target reset."), result["summary"]
    if result["backend"] == "openocd":
        commanded = debugger_log(bench, result["log_path"])["command"]
        assert "verify reset" in commanded, commanded


def test_an_uploaded_artifact_is_addressed_by_its_digest_and_flashes_the_bytes_that_were_uploaded(bench: Bench, firmware: Path, mcp) -> None:
    """The other way an image is named, and the only thing that ties the two together.

    Catches an artifact id derived from a filename rather than from the content,
    and a flash by id that resolves to some other file in the store. The id, the
    digest the upload reported and the digest the flash reported are all held
    against each other, and the flash is asserted to know it came out of the
    store rather than off a path.
    """
    image = firmware.relative_to(bench.project).as_posix()

    with mcp() as server:
        uploaded = server.call("artifact_upload", {"image_path": image})
        assert uploaded["ok"] is True, uploaded
        artifact_id = str(uploaded["artifact_id"])
        result = server.call("flash_firmware", {"artifact_id": artifact_id, "reset_after_flash": False})

    assert UPLOADED_ARTIFACT_ID.match(artifact_id), artifact_id
    assert artifact_id.startswith(str(uploaded["artifact"]["sha256"])), (artifact_id, uploaded["artifact"])
    assert_flash_holds(result)
    assert result["artifact"]["source"] == "upload", result["artifact"]
    assert result["artifact"]["sha256"] == uploaded["artifact"]["sha256"], (result["artifact"], uploaded["artifact"])
    assert str(result["artifact"]["path"]).endswith(artifact_id), result["artifact"]
    assert not Path(result["artifact"]["path"]).is_absolute(), result["artifact"]


def test_an_image_that_was_never_built_is_refused_as_missing_before_the_probe_is_opened(mcp) -> None:
    """A path inside the workspace that names no file, answered as what it is.

    Catches a missing image being carried into the debugger and coming back as a
    flash failure, which sends an agent to look at a board over a build it never
    ran. The absence of `log_path` is the load-bearing part: no debugger was
    started, so nothing was said to the target and the board still holds what it
    held.
    """
    with mcp() as server:
        envelope = server.envelope("flash_firmware", {"image_path": MISSING_IMAGE})
    result = envelope["structuredContent"]

    assert envelope["isError"] is True, result
    assert result["ok"] is False, result
    assert result["tool"] == "flash_firmware", result
    assert result["error_type"] == "artifact_not_found", result
    assert result["validation"]["exists"] is False, result["validation"]
    assert "log_path" not in result, result


def test_an_image_outside_the_workspace_is_refused_on_where_it_is_and_not_on_whether_it_exists(image_outside_the_workspace: Path, mcp) -> None:
    """Containment, measured with a file that is really there and is really the right image.

    Catches a workspace boundary that is enforced only by the file not being
    there. The bytes are the demo's own ELF and the path is absolute and outside
    `workspace_root`, so an `artifact_not_found` here would mean the product
    refused for a reason that stops being true the moment somebody builds in
    that directory. Both flags the refusal carries are asserted, because either
    one alone would let a path that leaves the workspace through a link read as
    contained.
    """
    with mcp() as server:
        result = server.call("flash_firmware", {"image_path": str(image_outside_the_workspace)})

    assert result["ok"] is False, result
    assert result["tool"] == "flash_firmware", result
    assert result["error_type"] == "artifact_validation_failed", result
    assert result["validation"]["within_workspace"] is False, result["validation"]
    assert result["validation"]["allowed_root"] is False, result["validation"]
    assert "log_path" not in result, result


def test_an_extension_the_configuration_does_not_allow_is_refused_by_the_extension_check(bench: Bench, image_with_a_disallowed_extension: Path, mcp) -> None:
    """The third refusal a path can earn, with its premise read off the configuration.

    Catches an allowed-extension list that is written into the configuration and
    then not consulted, which is how a build product that is not a firmware image
    reaches the programmer. The file is inside the workspace and under the
    allowed root, so the extension is the only thing left for the refusal to be
    about.
    """
    allowed = (bench.configuration().get("artifacts") or {}).get("allowed_extensions")
    assert allowed, bench.configuration().get("artifacts")
    suffix = image_with_a_disallowed_extension.suffix
    assert suffix not in allowed, (suffix, allowed)

    with mcp() as server:
        result = server.call("flash_firmware", {"image_path": image_with_a_disallowed_extension.name})

    assert result["ok"] is False, result
    assert result["tool"] == "flash_firmware", result
    assert result["error_type"] == "artifact_validation_failed", result
    assert result["validation"]["allowed_extension"] is False, result["validation"]
    assert result["validation"]["allowed_root"] is True, result["validation"]
    assert "log_path" not in result, result


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ({}, "neither source named"),
        ({"image_path": MISSING_IMAGE, "artifact_id": AN_ARTIFACT_ID_NOTHING_STORED}, "both sources named"),
    ],
    ids=["neither", "both"],
)
def test_a_flash_naming_no_source_or_two_sources_is_refused_by_the_published_schema(arguments: dict, why: str, mcp) -> None:
    """Exactly one of the two ways to name an image, enforced where a caller can read it.

    Catches a flash that accepts both a path and an artifact id and quietly picks
    one, which would write an image nobody asked for and report the other one's
    name over it. The refusal is the published schema's, so `validator` says
    which rule decided and a caller can fix the call without guessing.
    """
    with mcp() as server:
        result = server.call("flash_firmware", arguments)

    assert result["ok"] is False, (why, result)
    assert result["tool"] == "flash_firmware", result
    assert result["error_type"] == "invalid_argument", result
    assert result["validator"] == "oneOf", result
    assert result["field"] == "$", result
    assert "log_path" not in result, result


def test_flashing_is_refused_while_its_permission_is_closed_and_flashes_again_once_it_is_opened(bench: Bench, firmware: Path, mcp) -> None:
    """The gate on the write, in both directions, on this tier's own configuration.

    Catches a closed `allow_flash` that never reaches the flash path, and a
    `grant` that reports success without restoring it. Both halves are needed: a
    refusal on its own would pass just as well on a bench where flashing is
    broken outright.

    The permission is revoked and granted back inside this test, with the grant
    in a `finally` so a failed assertion cannot leave this bench unable to flash.
    The `revoke` is checked inside that `finally`'s `try` for the same reason: a
    revoke that reported failure and moved the key anyway would otherwise leave
    every later test on this bench refused.
    No configuration the operator owns is reachable from here. The board is left
    holding the demo's own firmware, because the second half writes it.
    """
    image = firmware.relative_to(bench.project).as_posix()
    key = f"debuggers.{bench.debugger_name()}.permissions.allow_flash"

    revoked = bench.run("revoke", key)
    try:
        assert revoked.returncode == 0, revoked.stdout + revoked.stderr
        with mcp() as closed:
            refused = closed.envelope("flash_firmware", {"image_path": image})
    finally:
        granted = bench.run("grant", key)
        assert granted.returncode == 0, granted.stdout + granted.stderr

    result = refused["structuredContent"]
    assert refused["isError"] is True, result
    assert result["ok"] is False, result
    assert result["tool"] == "flash_firmware", result
    assert result["error_type"] == "permission_denied", result
    assert result["permission"] == key, result
    assert key in result["summary"], result["summary"]
    assert f"agentic-hil grant {key}" in result["next_step"], result["next_step"]
    # Nothing was said to the debugger, so the image on the board is untouched.
    assert "log_path" not in result, result

    with mcp() as opened:
        allowed = opened.call("flash_firmware", {"image_path": image, "reset_after_flash": False})
    assert_flash_holds(allowed)


def test_a_post_flash_reset_is_gated_by_the_reset_permission_and_the_image_is_not_written_first(bench: Bench, firmware: Path, mcp) -> None:
    """The second gate on the flash path, and the order it is asked in.

    Catches two things. A `reset_after_flash: true` refused by the flash
    permission instead of the reset one, which sends an operator to open the
    wrong key. And a refusal that arrives after the image has already been
    written, which would leave a board carrying a new image under a result
    saying the call did not happen: the absence of `log_path` is what says no
    debugger ran.

    The same server then flashes without the post-flash reset, which is how the
    test shows that only the reset half was closed, and which leaves the board
    holding the demo's firmware. `allow_reset` is granted back in a `finally`,
    and the `revoke` is checked inside that `finally`'s `try`: this file's own
    teardown resets the core, so a reset permission left closed here would fail
    every later test in every file on this bench.
    """
    image = firmware.relative_to(bench.project).as_posix()
    key = f"debuggers.{bench.debugger_name()}.permissions.allow_reset"

    revoked = bench.run("revoke", key)
    try:
        assert revoked.returncode == 0, revoked.stdout + revoked.stderr
        with mcp() as server:
            refused = server.call("flash_firmware", {"image_path": image, "reset_after_flash": True})
            without_reset = server.call("flash_firmware", {"image_path": image, "reset_after_flash": False})
    finally:
        granted = bench.run("grant", key)
        assert granted.returncode == 0, granted.stdout + granted.stderr

    assert refused["ok"] is False, refused
    assert refused["tool"] == "flash_firmware", refused
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == key, refused
    assert f"agentic-hil grant {key}" in refused["next_step"], refused["next_step"]
    assert "log_path" not in refused, refused

    assert_flash_holds(without_reset)
    assert without_reset["reset_after_flash"] is False, without_reset


def test_the_reset_modes_the_server_publishes_are_the_three_it_answers(mcp) -> None:
    """The enum a caller reads before it picks a mode.

    Catches a mode dropped from the published list, or one added to it, or a
    default that is no longer `run`. It asserts publication and nothing else;
    the answering half is the test below, which is parametrised over this same
    list, so a mode that is published and not answered fails there. Together
    they are the claim: a caller that picks from this list is not then refused.
    """
    with mcp() as server:
        schema = server.tool_schema("reset_target")

    assert schema["properties"]["mode"]["enum"] == RESET_MODES, schema["properties"]["mode"]
    assert schema["properties"]["mode"]["default"] == "run", schema["properties"]["mode"]


@pytest.mark.parametrize("mode", RESET_MODES)
def test_each_published_reset_mode_is_answered_by_the_mode_it_was_asked_for(mode: str, mcp) -> None:
    """What each of the three modes answers on the backend this bench configured.

    Catches the failure the refusal was written for: `init` is not a third
    spelling of `halt`. It runs the target's reset-init event script, which only
    the OpenOCD backend has, and the backends that do not have it used to send
    their plain halt and report `Target reset with mode 'init'.` over it. So the
    answer is asserted against the backend the result names itself: a success
    that echoes the mode where the backend has it, and a `not_supported` naming
    the two modes that remain where it does not.

    Modes `halt` and `init` both leave the core stopped. The module's teardown
    resets it into `run` afterwards, so the board this test hands on is running
    the demo whichever mode it was parametrised with.
    """
    with mcp() as server:
        envelope = server.envelope("reset_target", {"mode": mode})
    result = envelope["structuredContent"]

    if mode == "init" and result["backend"] != "openocd":
        assert envelope["isError"] is True, result
        assert result["ok"] is False, result
        assert result["error_type"] == "not_supported", result
        assert result["mode"] == "init", result
        assert result["supported_modes"] == ["run", "halt"], result
        assert "log_path" not in result, result
        return

    assert envelope["isError"] is False, result
    assert result["ok"] is True, result
    assert result["tool"] == "reset_target", result
    assert result["mode"] == mode, result
    assert result["summary"].startswith(f"Target reset with mode '{mode}'."), result["summary"]
    assert "error_type" not in result, result
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] > 0, result
    assert result["log_path"], result


def test_a_reset_mode_outside_the_enum_is_refused_and_the_three_it_offers_are_named_back(mcp) -> None:
    """A mode nobody published, refused before anything is said to the target.

    Catches a mode string passed through to the debugger, which is how a typo
    becomes a command line somebody else wrote. `erase` is asked for
    deliberately: it is the word a caller reaching for something this surface
    does not do would try, and the answer has to be a refusal that names the
    three modes rather than an attempt at a fourth.
    """
    with mcp() as server:
        result = server.call("reset_target", {"mode": "erase"})

    assert result["ok"] is False, result
    assert result["tool"] == "reset_target", result
    assert result["error_type"] == "invalid_argument", result
    assert result["validator"] == "enum", result
    assert result["allowed_values"] == RESET_MODES, result
    assert "log_path" not in result, result


def test_a_reset_is_refused_while_its_permission_is_closed_and_resets_again_once_it_is_opened(bench: Bench, mcp) -> None:
    """The gate on the restart, in both directions.

    Catches a closed `allow_reset` that the reset path never asks about, which
    would restart a board an operator deliberately froze. The refusal has to
    name the dotted key and the operator's own command for it, because nothing
    on the agent's surface may open it.

    Revoked and granted back on this tier's own configuration, with the grant in
    a `finally` and the `revoke` checked inside that `finally`'s `try`: this
    file's own teardown resets the core, so a reset permission left closed here
    would fail every later test in every file on this bench.
    """
    key = f"debuggers.{bench.debugger_name()}.permissions.allow_reset"

    revoked = bench.run("revoke", key)
    try:
        assert revoked.returncode == 0, revoked.stdout + revoked.stderr
        with mcp() as closed:
            refused = closed.envelope("reset_target", {"mode": "run"})
    finally:
        granted = bench.run("grant", key)
        assert granted.returncode == 0, granted.stdout + granted.stderr

    result = refused["structuredContent"]
    assert refused["isError"] is True, result
    assert result["ok"] is False, result
    assert result["tool"] == "reset_target", result
    assert result["error_type"] == "permission_denied", result
    assert result["permission"] == key, result
    assert key in result["summary"], result["summary"]
    assert f"agentic-hil grant {key}" in result["next_step"], result["next_step"]
    assert "log_path" not in result, result

    with mcp() as opened:
        allowed = opened.call("reset_target", {"mode": "run"})
    assert allowed["ok"] is True, allowed
    assert allowed["mode"] == "run", allowed


def test_a_confirmed_reset_carries_the_failure_worded_lines_its_own_log_holds_and_stays_a_success(bench: Bench, mcp) -> None:
    """What this backend prints on the way to a reset it confirmed, on the MCP surface.

    Catches the reading that refused a step on a board which had already
    restarted: a failure-worded line printed beside a success marker is evidence
    about the run and never a second verdict on it. Held in both directions.
    Every line the result kept is a line the log really has, so nothing is
    invented; and a capture carrying such lines does not turn the call into an
    error, at the tool's own `ok` and at the MCP envelope's `isError`, which is
    the field an agent host decides from and which no test on the reactor
    surface reaches.

    The predicate for what counts as a failure-worded line comes from this
    tier's own conftest and not from the backend, so the code under test is not
    grading itself.
    """
    with mcp() as server:
        envelope = server.envelope("reset_target", {"mode": "run"})
    result = envelope["structuredContent"]

    assert result["ok"] is True, result
    assert envelope["isError"] is False, result
    capture = debugger_capture(bench, result["log_path"])
    carried = result.get("backend_warnings") or []
    assert all(warning in capture.splitlines() for warning in carried), (carried, capture)
    if carried:
        assert "backend_warnings" in result["summary"], result["summary"]
    assert result["summary"].startswith("Target reset with mode 'run'."), result["summary"]
    if result["backend"] == "openocd":
        assert result["success_confirmed"] is True, result
    # The whole claim, in one line: a capture carrying the words a failure is
    # read out of did not turn a confirmed reset into a refusal.
    assert not failure_worded_lines(capture) or result["ok"] is True, (capture, result)


def test_nothing_on_this_surface_erases_the_chip_and_both_flash_interlocks_arrive_closed(bench: Bench, mcp) -> None:
    """Why no test in this file needs `allow_mass_erase`, asserted rather than assumed.

    Catches two ways this bench could stop being the one the rest of this file
    describes. A tool that erases, or an erase option grown onto flashing, would
    put a destructive operation on the agent's surface, and the properties of
    `flash_firmware` are written out here so an added one fails. And a generated
    configuration arriving with either flash interlock open would refuse every
    flash on this probe, which is a bench that cannot do the thing this file
    exists to measure.

    Neither key is granted, here or anywhere in this file.
    """
    debugger = bench.debugger_name()
    permissions = (bench.configuration()["debuggers"][debugger].get("permissions") or {})
    assert permissions.get("allow_mass_erase", False) is False, permissions
    assert permissions.get("allow_raw_debugger_commands", False) is False, permissions

    with mcp() as server:
        names = server.tool_names()
        schema = server.tool_schema("flash_firmware")
        answered = server.call(AN_ERASE_TOOL_WOULD_BE_CALLED)

    assert [name for name in names if "erase" in name] == [], names
    assert set(schema["properties"]) == {"image_path", "artifact_id", "reset_after_flash"}, schema["properties"]
    assert answered["ok"] is False, answered
    assert answered["error_type"] == "unknown_tool", answered


def test_a_flash_outside_a_declared_run_hands_the_bench_back_when_the_call_returns(bench: Bench, firmware: Path, mcp) -> None:
    """The lease one call takes for itself, and the promise that it gives it back.

    Catches an implicit one-shot lease that is taken and never released, which
    would leave this bench held against every later run on this machine, by this
    suite and by anything else reaching for the board. The server is still
    running when the question is asked, so a hold that outlives the call is
    visible here and a hold that ends with it is not.
    """
    image = firmware.relative_to(bench.project).as_posix()

    with mcp() as server:
        flashed = server.call("flash_firmware", {"image_path": image, "reset_after_flash": False})
        assert_flash_holds(flashed)
        status_code, status = bench.document("lease-status")

    assert status_code == 0, status
    assert status["bench_held"] is False, status
    assert status["held_devices"] == [], status
    assert status["blocked"] is False, status
    assert status["incident_stands"] is False, status
