"""A server started outside any project asks the host which folder it has open.

The working directory is the only place the server has ever looked for its
project, and every host that starts it from the project folder keeps it that
way. A host whose registration is user-wide does not: VS Code starts a server
registered in its user profile in the home directory, and resolves no
`${workspaceFolder}` there. What it does do is answer the MCP `roots/list`
request with the folder it has open, and that answer is what these tests hold
the server to.

The rule is narrow on purpose. A working directory with a configuration is
served as it always was and the host is never asked. A host that did not
declare `roots` is never asked either. The answer moves the server only while no
configuration is bound, so a session never changes projects underneath the
agent, and a tool call that arrives before the answer waits for it rather than
running against the home directory.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import write_authoritative_config

from agentic_hil.config import load_authoritative_config
from agentic_hil.mcp import SERVER_INSTRUCTIONS, UNPROVISIONED_SERVER_INSTRUCTIONS
from agentic_hil.stdio import DEFAULT_MAX_MESSAGE_CHARS, message_size_limit, run_stdio_server
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService

JSONRPC_INVALID_REQUEST = -32600


def initialize(*, roots: bool = True) -> dict:
    capabilities: dict = {"roots": {"listChanged": True}} if roots else {}
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": capabilities, "clientInfo": {"name": "roots-test", "version": "0"}},
    }


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
ROOTS_CHANGED = {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}


def describe(request_id: int) -> dict:
    """A read every configuration answers without a grant, and that names the folder it serves."""
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "project_config_describe", "arguments": {}}}


def host_uri(folder: Path) -> str:
    """A folder the way VS Code names it in a root: on Windows the drive letter lower-case and its colon escaped."""
    if sys.platform == "win32":
        posix = folder.as_posix()
        return "file:///" + posix[0].lower() + "%3A" + quote(posix[2:])
    return folder.as_uri()


class Host:
    """The other end of the stdio pipe, answering as the conversation goes.

    The server reads one line at a time and has written everything it had to say
    before it reads the next, so a step of the script that is a function sees the
    conversation so far and may answer what the server just asked. A step that
    returns None sends nothing. Lines are handed out no longer than the limit the
    server reads with, the way a pipe hands them out.
    """

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.output = io.StringIO()
        self._buffer = ""

    def readline(self, limit: int = -1) -> str:
        while not self._buffer and self.steps:
            step = self.steps.pop(0)
            message = step(self) if callable(step) else step
            if message is not None:
                self._buffer = json.dumps(message) + "\n"
        if not self._buffer:
            return ""
        end = self._buffer.find("\n") + 1
        if limit is not None and limit >= 0:
            end = min(end, limit)
        line, self._buffer = self._buffer[:end], self._buffer[end:]
        return line

    def sent(self) -> list:
        return [json.loads(line) for line in self.output.getvalue().splitlines() if line]

    def questions(self) -> list[dict]:
        return [message for message in self.sent() if isinstance(message, dict) and message.get("method") == "roots/list"]

    def reply(self, request_id: int) -> dict:
        return next(message for message in self.sent() if isinstance(message, dict) and message.get("id") == request_id and "method" not in message)


def answer_roots(*folders: Path, uris: tuple[str, ...] = ()) -> object:
    """The host's answer to the last `roots/list` the server sent."""

    def step(host: Host) -> dict:
        question = host.questions()[-1]
        roots = [{"uri": host_uri(folder), "name": folder.name} for folder in folders] + [{"uri": uri} for uri in uris]
        return {"jsonrpc": "2.0", "id": question["id"], "result": {"roots": roots}}

    return step


def refuse_roots(host: Host) -> dict:
    question = host.questions()[-1]
    return {"jsonrpc": "2.0", "id": question["id"], "error": {"code": -32601, "message": "Method not found"}}


def serve(host: Host, tools: object, config=None) -> list[dict]:
    assert run_stdio_server(config, input_stream=host, output_stream=host.output, tools=tools) == 0  # type: ignore[arg-type]
    return host.sent()


@pytest.fixture
def places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home directory with no project in it, and three project folders.

    The configurations go under the sandbox's configuration home, where the
    discovery a real start makes finds them by folder, and the override that
    would name one file for every folder is taken away again.
    """
    config_root = Path(os.environ["APPDATA"]) / "roots"
    home = (tmp_path / "home").resolve()
    home.mkdir()
    folders = {"home": home}
    for name in ("configured project", "other configured project"):
        folder = (tmp_path / name).resolve()
        write_authoritative_config(folder, monkeypatch, config_root=config_root)
        folders[name] = folder
    bare = (tmp_path / "bare project").resolve()
    bare.mkdir()
    folders["bare project"] = bare
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    monkeypatch.chdir(home)
    return folders


def served_folder(reply: dict) -> Path:
    return Path(reply["result"]["structuredContent"]["workspace_root"])


def test_a_server_started_outside_any_project_serves_the_folder_the_host_has_open(places: dict[str, Path]) -> None:
    host = Host(initialize(), INITIALIZED, answer_roots(places["configured project"]), describe(2))
    serve(host, UnprovisionedToolService(places["home"]))

    assert len(host.questions()) == 1
    reply = host.reply(2)
    assert reply["result"]["structuredContent"]["ok"] is True, reply
    assert served_folder(reply) == places["configured project"]


def test_a_call_that_arrives_before_the_host_answers_waits_for_the_answer(places: dict[str, Path]) -> None:
    """Run at once, it would run against the home directory, and a create there would write a configuration for home."""
    host = Host(initialize(), INITIALIZED, describe(2), {"jsonrpc": "2.0", "id": 3, "method": "ping"}, answer_roots(places["configured project"]))
    sent = serve(host, UnprovisionedToolService(places["home"]))

    reply = host.reply(2)
    assert reply["result"]["structuredContent"]["ok"] is True, reply
    assert served_folder(reply) == places["configured project"]
    # The ping needs no folder and is not held back behind the call that does.
    order = [message.get("id") for message in sent]
    assert order.index(3) < order.index(2)


def test_a_working_directory_with_a_configuration_is_served_without_asking_the_host(places: dict[str, Path]) -> None:
    project = places["configured project"]
    host = Host(initialize(), INITIALIZED, describe(2))
    serve(host, UnprovisionedToolService(project))

    assert host.questions() == []
    assert served_folder(host.reply(2)) == project

    config = load_authoritative_config(project)
    host = Host(initialize(), INITIALIZED, describe(2))
    serve(host, AgenticHILToolService(config, frontend="mcp"), config)

    assert host.questions() == []
    assert served_folder(host.reply(2)) == project


def test_a_host_that_did_not_declare_roots_is_never_asked(places: dict[str, Path]) -> None:
    host = Host(initialize(roots=False), INITIALIZED, ROOTS_CHANGED, describe(2))
    serve(host, UnprovisionedToolService(places["home"]))

    assert host.questions() == []
    refusal = host.reply(2)["result"]["structuredContent"]
    assert refusal["error_type"] == "config_file_not_found"
    assert Path(refusal["workspace_root"]) == places["home"]


def test_of_several_folders_the_one_with_a_configuration_is_served(places: dict[str, Path]) -> None:
    host = Host(initialize(), INITIALIZED, answer_roots(places["bare project"], places["configured project"]), describe(2))
    serve(host, UnprovisionedToolService(places["home"]))

    assert served_folder(host.reply(2)) == places["configured project"]


@pytest.mark.parametrize("names", [("configured project", "other configured project"), ("bare project", "home")])
def test_several_folders_that_name_no_single_configuration_leave_the_server_where_it_started(places: dict[str, Path], names: tuple[str, str]) -> None:
    host = Host(initialize(), INITIALIZED, answer_roots(*(places[name] for name in names)), describe(2))
    serve(host, UnprovisionedToolService(places["home"]))

    refusal = host.reply(2)["result"]["structuredContent"]
    assert refusal["error_type"] == "config_file_not_found"
    assert Path(refusal["workspace_root"]) == places["home"]


def test_a_single_folder_without_a_configuration_is_the_one_a_configuration_is_generated_for(places: dict[str, Path]) -> None:
    host = Host(initialize(), INITIALIZED, answer_roots(places["bare project"]), describe(2))
    serve(host, UnprovisionedToolService(places["home"]))

    refusal = host.reply(2)["result"]["structuredContent"]
    assert refusal["error_type"] == "config_file_not_found"
    assert Path(refusal["workspace_root"]) == places["bare project"]


def test_a_root_that_is_not_a_local_folder_is_passed_over(places: dict[str, Path]) -> None:
    missing = places["home"].parent / "gone"
    host = Host(
        initialize(),
        INITIALIZED,
        answer_roots(places["configured project"], uris=("https://example.invalid/repo", host_uri(missing), "not a uri")),
        describe(2),
    )
    serve(host, UnprovisionedToolService(places["home"]))

    assert served_folder(host.reply(2)) == places["configured project"]


def test_a_host_that_refuses_the_question_leaves_the_server_where_it_started(places: dict[str, Path]) -> None:
    host = Host(initialize(), INITIALIZED, describe(2), refuse_roots)
    serve(host, UnprovisionedToolService(places["home"]))

    refusal = host.reply(2)["result"]["structuredContent"]
    assert refusal["error_type"] == "config_file_not_found"
    assert Path(refusal["workspace_root"]) == places["home"]


def test_the_hosts_answer_is_not_answered(places: dict[str, Path]) -> None:
    """A reply to a reply is an unsolicited message on the stream the host is parsing."""
    for answer in (answer_roots(places["configured project"]), refuse_roots):
        host = Host(initialize(), INITIALIZED, answer)
        sent = serve(host, UnprovisionedToolService(places["home"]))

        question = host.questions()[0]
        assert [message for message in sent if message.get("id") == question["id"]] == [question]
        assert not [message for message in sent if message.get("error", {}).get("code") == JSONRPC_INVALID_REQUEST]


def test_the_greeting_does_not_send_an_agent_to_regenerate_a_configuration_the_folder_may_have(places: dict[str, Path]) -> None:
    """The folder is not known when initialize is answered: the host is asked only after it.

    A user-wide registration is there for the projects that have a bench, and in
    one of those "start with project_config_create" regenerates what the operator
    set up. A folder that turns out to have no configuration says so at the
    first call, with the tool that generates one.
    """
    host = Host(initialize(), INITIALIZED, answer_roots(places["configured project"]))
    serve(host, UnprovisionedToolService(places["home"]))
    assert host.reply(1)["result"]["instructions"] == SERVER_INSTRUCTIONS

    host = Host(initialize(roots=False), INITIALIZED)
    serve(host, UnprovisionedToolService(places["home"]))
    assert host.reply(1)["result"]["instructions"] == UNPROVISIONED_SERVER_INSTRUCTIONS


def test_a_changed_folder_list_is_asked_again_while_no_configuration_is_bound(places: dict[str, Path]) -> None:
    host = Host(initialize(), INITIALIZED, answer_roots(places["bare project"]), ROOTS_CHANGED, answer_roots(places["configured project"]), describe(2))
    serve(host, UnprovisionedToolService(places["home"]))

    assert len(host.questions()) == 2
    assert served_folder(host.reply(2)) == places["configured project"]


def test_a_bound_configuration_is_not_moved_by_a_changed_folder_list(places: dict[str, Path]) -> None:
    host = Host(
        initialize(),
        INITIALIZED,
        answer_roots(places["configured project"]),
        describe(2),
        ROOTS_CHANGED,
        describe(3),
    )
    serve(host, UnprovisionedToolService(places["home"]))

    assert len(host.questions()) == 1
    assert served_folder(host.reply(3)) == places["configured project"]


def test_the_folders_configuration_sets_the_message_limit_a_start_there_would(places: dict[str, Path]) -> None:
    """The limit leaves room for the configured upload size, which the home directory has none of."""
    path = Path(load_authoritative_config(places["configured project"]).config_path)
    path.write_text(path.read_text(encoding="utf-8").replace("max_upload_size_mb: 1\n", "max_upload_size_mb: 64\n"), encoding="utf-8")
    limit = message_size_limit(load_authoritative_config(places["configured project"]))
    assert limit > DEFAULT_MAX_MESSAGE_CHARS
    padded = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"padding": "x" * (DEFAULT_MAX_MESSAGE_CHARS + 1024)}}
    host = Host(initialize(), INITIALIZED, answer_roots(places["configured project"]), describe(3), padded)
    serve(host, UnprovisionedToolService(places["home"]))

    assert host.reply(2) == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_a_call_still_waiting_when_the_host_hangs_up_is_not_run(places: dict[str, Path]) -> None:
    """A host that closed the pipe reads no answer, and a hardware call nobody is waiting for is not started."""
    calls: list[str] = []

    class Recording(UnprovisionedToolService):
        def call(self, name: str, arguments: dict | None = None) -> dict:
            calls.append(name)
            return super().call(name, arguments)

    host = Host(initialize(), INITIALIZED, describe(2))
    sent = serve(host, Recording(places["home"]))

    assert calls == []
    assert [message for message in sent if message.get("id") == 2] == []


def test_an_answer_inside_a_batch_is_taken_out_of_it(places: dict[str, Path]) -> None:
    """Batches are gone from MCP since 2025-06-18, and a host on an earlier revision may still send one."""

    def batched(host: Host) -> list[dict]:
        answer = answer_roots(places["configured project"])(host)
        return [answer, describe(3)]  # type: ignore[list-item]

    host = Host(initialize(), INITIALIZED, describe(2), batched)
    sent = serve(host, UnprovisionedToolService(places["home"]))

    assert served_folder(host.reply(2)) == places["configured project"]
    batch = next(message for message in sent if isinstance(message, list))
    assert [served_folder(reply) for reply in batch] == [places["configured project"]]


def test_the_vs_code_registration_leaves_the_folder_to_the_roots_answer() -> None:
    """VS Code resolves no `${workspaceFolder}` in the user profile, and a server registered with it does not start.

    Measured on VS Code 1.139: the error is "Variable workspaceFolder can not be
    resolved", with a folder open. Without `cwd` the server starts in the home
    directory and asks for the folder, which is what the page tells the operator.
    """
    guide = (Path(__file__).resolve().parents[1] / "docs" / "mcp-hosts.md").read_text(encoding="utf-8")
    section = guide.split("\n## VS Code and GitHub Copilot\n", 1)[1].split("\n## ", 1)[0]
    block = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])

    assert block == {"servers": {"agentic-hil": {"type": "stdio", "command": "/absolute/path/to/persistent/agentic-hil", "args": ["mcp-stdio"]}}}
    assert "roots/list" in section
