"""A first run on the bench, made the way a newcomer makes it.

The quick start in ``README.md`` and ``AI_AGENT_QUICKSTART.md`` is the
specification here, followed literally and in its order, with the commands it
names: confirm the install, build the demo, ``setup`` for an agent, ``doctor``,
then the four MCP calls, the demo's plan and ``pytest tests/`` on the board. Where
the documents and the product disagree, a test here is red.

The person making the run has a clean account: a home directory of its own, its
own configuration, state, data and cache roots, its own temporary directory, and
a fresh copy of the demo project under that home. Every agent configuration the
product writes is under that home, because the product finds each of them from
the home directory (``~/.claude.json``, ``~/.claude``, ``~/.codex``,
``~/.config/opencode``) and nowhere else. The product is this checkout, started
the way that person starts it: by the name ``agentic-hil`` on PATH, which is
this interpreter's own launcher, checked against this tree before anything runs.

The commands the rest of the tier never runs are all here: ``setup`` for every
agent it supports, and ``agent-install``, ``skill-install`` and ``mcp-config`` on
their own, each run twice so a second run is shown to change nothing and to say
so, and ``schema`` and ``test-schema`` against the demo's plan and the schemas the
package ships.

Device locks: the product takes them under the home directory of whoever runs it,
in ``~/.agentic-hil/device-locks``, a place with no configuration key and no
environment override. A redirected home therefore moves them, and this module
proves it did. So the newcomer's commands hold the board under locks that the
tier's own fixtures, and every other run on this machine, do not look at. What
still keeps two runs apart is one level up: the machine's run lock
(``tools/run_lock.py``), which ``tools/bench_in_container.py`` takes before it
builds and holds until the tier is done, so no other run of the tier reaches the
board while this one does, and inside the run pytest runs one test at a time, so
the tier's fixtures never drive the board while a newcomer command does. A run of
this module outside that lock, directly on a bench somebody else also uses, has
nothing that keeps a newcomer command and another run's session apart, and must
be started under it.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from support import scaled_time_bound

from .conftest import (
    BENCH_ONLY,
    BUILD_TIMEOUT_S,
    CHECKOUT_SOURCES,
    COMMAND_TIMEOUT_S,
    DEMO,
    DEMO_IMAGE,
    WHERE_THE_PRODUCT_CAME_FROM,
    BoardImages,
    import_path,
    not_the_checkout,
    refuse,
)

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The agents `setup` supports, in the order the quick start names them.
AGENTS = ("claude-code", "codex", "opencode")

# Where each agent keeps what this product writes for it, under the home
# directory, and the user-level MCP registration each one reads, as
# docs/mcp-hosts.md lists them.
AGENT_ROOTS = {"claude-code": Path(".claude"), "codex": Path(".codex"), "opencode": Path(".config") / "opencode"}
REGISTRATIONS = {
    "claude-code": Path(".claude.json"),
    "codex": Path(".codex") / "config.toml",
    "opencode": Path(".config") / "opencode" / "opencode.json",
}
SKILL = Path("skills") / "agentic-hil" / "SKILL.md"
CODEX_SKILL_REGISTRATION = Path(".codex") / "AGENTS.md"
CLAUDE_SETTINGS = Path(".claude") / "settings.json"
PACKAGED_SKILL = CHECKOUT_SOURCES / "agentic_hil" / "skills" / "agentic-hil" / "SKILL.md"
PACKAGED_SCHEMAS = CHECKOUT_SOURCES / "agentic_hil" / "schemas"

# What `agent-install` writes, and nothing else: the agent's skill and its
# user-level MCP registration (AI_AGENT_QUICKSTART.md, "The two halves"). Codex
# finds a skill through its AGENTS.md, so that file is part of its skill.
AGENT_INSTALL_WRITES = {
    agent: {(AGENT_ROOTS[agent] / SKILL).as_posix(), REGISTRATIONS[agent].as_posix()}
    | ({CODEX_SKILL_REGISTRATION.as_posix()} if agent == "codex" else set())
    for agent in AGENTS
}

# The two permissions flashing is interlocked against. A generated configuration
# writes both false and grants every other permission it declares.
INTERLOCKS = frozenset({"allow_raw_debugger_commands", "allow_mass_erase"})
# What says which hardware a com_ports entry is, at version 3.
PORT_IDENTITIES = ("serial_number", "resource_id", "identity_source")
BY_ID = "/dev/serial/by-id/"

# The names the demo's plan and the README's calls address.
DEBUGGER = "dut"
PORT = "dut_uart"
BANNER = "Hello World"
BANNER_TIMEOUT_S = scaled_time_bound(15.0)
REPLY_TIMEOUT_S = 300.0
SHUTDOWN_TIMEOUT_S = scaled_time_bound(120.0)
MCP_PROTOCOL_VERSION = "2025-06-18"

# What of the caller's environment a clean account does not have: this tier's
# own switches, pytest's, every XDG root, and whatever points a Python or a
# package manager somewhere this account never chose.
LEFT_BEHIND_PREFIXES = ("AGENTIC_HIL_", "PYTEST_", "XDG_", "UV_", "PIP_", "PIPX_")
LEFT_BEHIND = frozenset({"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "COLUMNS", "LINES"})


@dataclass(frozen=True)
class Newcomer:
    """One clean account on this machine, and the product it runs by name."""

    home: Path
    environment: dict[str, str]
    launcher: str

    @property
    def config_home(self) -> Path:
        return self.home / ".config"

    @property
    def state_home(self) -> Path:
        return self.home / ".local" / "state"

    def run(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        """`agentic-hil <arguments>`, as this account types it, from `cwd`."""
        return subprocess.run(
            [self.launcher, *arguments],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=self.environment,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )

    def document(self, *arguments: str, cwd: Path) -> tuple[int, dict]:
        """One command's machine document, with the status it exited on."""
        answered = self.run(*arguments, "--json", cwd=cwd)
        assert answered.stdout.strip(), f"{arguments} printed no document (exit {answered.returncode}):\n{answered.stderr}"
        return answered.returncode, json.loads(answered.stdout)


def newcomer_account(root: Path) -> Newcomer:
    """A home, roots and a temporary directory of its own, and this checkout on PATH.

    PATH starts with this interpreter's own directory, which is where the
    launcher of the product under test is, so `agentic-hil` is found there the
    way a newcomer finds a freshly installed command. The import path puts this
    tree's sources first, and the interpreter is asked where its `agentic_hil`
    came from before the account is used.
    """
    home = root / "home"
    temporary = root / "tmp"
    home.mkdir(parents=True)
    temporary.mkdir(parents=True)
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(LEFT_BEHIND_PREFIXES) and name not in LEFT_BEHIND
    }
    interpreter_directory = str(Path(sys.executable).parent)
    search_path = os.pathsep.join(
        [interpreter_directory, *(entry for entry in kept.get("PATH", "").split(os.pathsep) if entry and entry != interpreter_directory)]
    )
    environment = {
        **kept,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "PATH": search_path,
        "PYTHONPATH": import_path(),
    }
    launcher = shutil.which("agentic-hil", path=search_path)
    if launcher is None or Path(launcher).parent != Path(interpreter_directory):
        refuse(
            f"`agentic-hil` is not this interpreter's own launcher in {interpreter_directory} (PATH answered {launcher}), "
            "so a newcomer here would run some other installation than the one under test."
        )
    resolved = subprocess.run(
        [sys.executable, "-c", WHERE_THE_PRODUCT_CAME_FROM],
        capture_output=True,
        text=True,
        cwd=str(home),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        refuse(f"the first run could not establish which `agentic_hil` its launcher runs:\n{resolved.stdout}\n{resolved.stderr}")
    shadowed = not_the_checkout(resolved.stdout.strip())
    if shadowed is not None:
        refuse(shadowed)
    return Newcomer(home=home, environment=environment, launcher=str(launcher))


def a_fresh_demo(newcomer: Newcomer, name: str) -> Path:
    """A copy of the demo project under the account's home, as a clone leaves it."""
    project = newcomer.home / name / DEMO.name
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("build", ".agentic-hil"))
    return project


def is_lock_sidecar(path: Path) -> bool:
    """The file a locked write leaves beside the file it wrote, and nothing else."""
    return path.name.startswith(".") and path.name.endswith(".agentic-hil.lock")


def files_under(root: Path, *within: Path) -> dict[str, str]:
    """Every file under `root` (or under the named parts of it), by digest, lock sidecars left out."""
    starts = [root / part for part in within] if within else [root]
    found: dict[str, str] = {}
    for start in starts:
        candidates = [start] if start.is_file() else sorted(start.rglob("*")) if start.is_dir() else []
        for path in candidates:
            if path.is_file() and not path.is_symlink() and not is_lock_sidecar(path):
                found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def tree_digest(root: Path) -> dict[str, str]:
    """Every file and directory of a tree, files by digest."""
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[relative] = f"link:{os.readlink(path)}"
        elif path.is_file():
            entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            entries[f"{relative}/"] = "directory"
    return entries


def modes_along(launcher: str) -> dict[str, tuple[int, int]]:
    """The mode and owner of every component from the launcher, and from what it resolves to, up to the root."""
    chain: dict[str, tuple[int, int]] = {}
    for start in dict.fromkeys([Path(launcher), Path(os.path.realpath(launcher))]):
        for component in (start, *start.parents):
            with suppress(OSError):
                info = os.lstat(component)
                chain[str(component)] = (stat.S_IMODE(info.st_mode), info.st_uid)
    return chain


def permission_grants(document: object, where: str = "") -> list[tuple[str, str, object]]:
    """Every `allow_*` in every `permissions` mapping of a configuration, with where it is."""
    found: list[tuple[str, str, object]] = []
    if isinstance(document, dict):
        for key, value in document.items():
            here = f"{where}.{key}" if where else str(key)
            if key == "permissions" and isinstance(value, dict):
                found.extend((here, str(name), granted) for name, granted in value.items() if str(name).startswith("allow_"))
            else:
                found.extend(permission_grants(value, here))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(permission_grants(value, f"{where}[{index}]"))
    return found


def schema_errors(schema: dict, document: object) -> list[str]:
    """What a schema says is wrong with a document, after checking the schema itself."""
    from jsonschema.validators import validator_for

    validator = validator_for(schema)
    validator.check_schema(schema)
    # Round-tripped through JSON so a value YAML reads as a date or a number is
    # judged the way an editor holding the schema judges the text.
    as_text = json.loads(json.dumps(document, default=str))
    return [f"{'/'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}" for error in validator(schema).iter_errors(as_text)]


def read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10, where the product depends on tomli instead
        import tomli as tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))


class Server:
    """One MCP server started the way the host a newcomer registered starts it.

    The command is the one the registration names, the working directory is the
    firmware project, and the environment is the account's. Answers are pulled
    off stdout by a thread and handed over by id; stderr goes to a file.
    """

    def __init__(self, command: list[str], *, cwd: Path, environment: dict[str, str], stderr_path: Path) -> None:
        self.stderr_path = stderr_path
        self._stderr = stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            cwd=str(cwd),
            env=environment,
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

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """One tool call; the tool's own document, held to the `isError` flag beside it."""
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}})
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


@dataclass(frozen=True)
class FirstRun:
    """What the documented first run answered, in the order it was made."""

    newcomer: Newcomer
    project: Path
    version: subprocess.CompletedProcess[str]
    setup_help: subprocess.CompletedProcess[str]
    setups: dict[str, tuple[int, dict]]
    doctor: tuple[int, dict]
    project_before: dict[str, str]
    project_after: dict[str, str]
    modes_before: dict[str, tuple[int, int]]
    modes_after: dict[str, tuple[int, int]]

    @property
    def config_path(self) -> Path:
        return Path(self.setups[AGENTS[0]][1]["scopes"]["project"]["config_path"])

    def configuration(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def first_run(board_image_builds: BoardImages, tmp_path_factory: pytest.TempPathFactory) -> FirstRun:
    """The quick start, once, from a clean account: confirm, build, `setup` for every agent, `doctor`.

    It asks for the session's board images so the tier's own session is set up,
    and the demo is on the board, before the newcomer's first command reaches it.
    """
    newcomer = newcomer_account(tmp_path_factory.mktemp("first-run"))
    project = a_fresh_demo(newcomer, "firmware")
    # AI_AGENT_QUICKSTART.md, "Then confirm".
    version = newcomer.run("--version", cwd=project)
    setup_help = newcomer.run("setup", "--help", cwd=project)
    # README.md, "Quickstart: one real run", in its order.
    for command in (["cmake", "--preset", "Debug"], ["cmake", "--build", "--preset", "Debug"]):
        built = subprocess.run(command, capture_output=True, text=True, cwd=str(project), env=newcomer.environment, timeout=BUILD_TIMEOUT_S, check=False)
        if built.returncode != 0:
            pytest.fail(f"the README's build did not build the demo for a newcomer: {' '.join(command)}\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}", pytrace=False)
    project_before = tree_digest(project)
    modes_before = modes_along(newcomer.launcher)
    setups = {agent: newcomer.document("setup", "--agent", agent, cwd=project) for agent in AGENTS}
    project_after = tree_digest(project)
    modes_after = modes_along(newcomer.launcher)
    doctor = newcomer.document("doctor", cwd=project)
    return FirstRun(
        newcomer=newcomer,
        project=project,
        version=version,
        setup_help=setup_help,
        setups=setups,
        doctor=doctor,
        project_before=project_before,
        project_after=project_after,
        modes_before=modes_before,
        modes_after=modes_after,
    )


def test_the_install_confirms_itself_the_way_the_quickstart_says(first_run: FirstRun) -> None:
    """`agentic-hil --version` and `agentic-hil setup --help` answer before anything is set up."""
    assert first_run.version.returncode == 0, first_run.version.stderr
    assert first_run.version.stdout.strip(), first_run.version
    assert first_run.setup_help.returncode == 0, first_run.setup_help.stderr
    assert "--agent" in first_run.setup_help.stdout, first_run.setup_help.stdout


def test_setup_is_ok_throughout_for_every_agent(first_run: FirstRun) -> None:
    """One JSON result per agent, `ok: true` throughout: the result, both scopes and every step.

    The first `setup` found the board and wrote the configuration from it; the
    later ones kept that configuration. Each wrote a registration, so each says
    the agent has to be restarted.
    """
    for agent, (status, result) in first_run.setups.items():
        assert status == 0, (agent, result)
        assert result["ok"] is True, (agent, result)
        assert result["scopes"]["user"]["ok"] is True, (agent, result["scopes"]["user"])
        assert result["scopes"]["project"]["ok"] is True, (agent, result["scopes"]["project"])
        assert set(result["steps"]) == {"config", "skill_install", "mcp_config", "doctor", "agent_write_restriction"}, (agent, result["steps"])
        for name, step in result["steps"].items():
            assert step["ok"] is True, (agent, name, step)
        assert result["restart_required"] is True, (agent, result)
        assert str(result.get("restart_notice") or "").strip(), (agent, result)
    written = first_run.setups[AGENTS[0]][1]["steps"]["config"]
    assert written.get("skipped") is not True, written
    assert written["hardware_discovery"]["ok"] is True, written["hardware_discovery"]
    for agent in AGENTS[1:]:
        kept = first_run.setups[agent][1]["steps"]["config"]
        assert kept["skipped"] is True and kept["existing"] is True, (agent, kept)
        assert Path(first_run.setups[agent][1]["scopes"]["project"]["config_path"]) == first_run.config_path, agent


def test_setup_changes_no_modes_on_the_way_to_the_launcher_it_registers(first_run: FirstRun) -> None:
    """The launcher a default `umask 0002` install leaves is registered as it is.

    docs/mcp-hosts.md and TROUBLESHOOTING.md: group write by the owner's own
    private group is not another writer, so that layout "needs no `chmod`" before
    it can be registered. What the documents say `setup` writes is the skill, the
    registration, the configuration and the write restriction; a mode is none of
    those, so no `setup` reports a permission change and every component from the
    launcher to the root keeps its mode.
    """
    for agent, (_, result) in first_run.setups.items():
        assert result["permission_changes"] == [], (agent, result["permission_changes"])
    changed = {
        component: (f"{before[0]:04o}", f"{first_run.modes_after.get(component, (0, 0))[0]:04o}")
        for component, before in first_run.modes_before.items()
        if first_run.modes_after.get(component) != before
    }
    assert not changed, changed


def test_setup_leaves_the_firmware_project_as_it_found_it(first_run: FirstRun) -> None:
    """`setup` writes nothing inside the firmware project, and no project `.mcp.json` above all.

    AI_AGENT_QUICKSTART.md: the registration is user-level, outside the
    repository, and `setup` "writes no project `.mcp.json`"; the configuration
    is external. So every file and directory of the project, the build it just
    made included, is what it was before the first `setup`.
    """
    assert first_run.project_after == first_run.project_before, sorted(set(first_run.project_after.items()) ^ set(first_run.project_before.items()))
    assert not (first_run.project / ".mcp.json").exists()


def test_the_configuration_is_outside_the_project_bound_to_it_and_grants_what_the_documents_say(first_run: FirstRun) -> None:
    """Where the file is, what it binds, what it grants and how it names the hardware it found.

    One file under `${XDG_CONFIG_HOME}/agentic-hil/projects/<name>-<digest>/`,
    `version: 3`, `workspace_root` bound to the project, `state_root` under
    `${XDG_STATE_HOME}/agentic-hil`, every permission granted except the two
    flashing is interlocked against, every serial port carrying its identity,
    and the OpenOCD-only discovery path saying what it cannot see. The shipped
    configuration schema accepts it.
    """
    newcomer, config_path = first_run.newcomer, first_run.config_path
    assert config_path.name == "config.yaml", config_path
    assert config_path.parent.parent == newcomer.config_home / "agentic-hil" / "projects", config_path
    assert config_path.parent.name.startswith(f"{DEMO.name}-"), config_path
    assert not config_path.resolve().is_relative_to(first_run.project.resolve()), config_path
    document = first_run.configuration()
    assert document["version"] == 3, document.get("version")
    assert Path(document["workspace_root"]).resolve() == first_run.project.resolve(), document["workspace_root"]
    assert Path(document["state_root"]).resolve().is_relative_to((newcomer.state_home / "agentic-hil").resolve()), document["state_root"]

    grants = permission_grants(document)
    assert grants, document
    wrong = [(where, name, granted) for where, name, granted in grants if granted is not (name not in INTERLOCKS)]
    assert not wrong, wrong
    assert DEBUGGER in document["debuggers"], sorted(document["debuggers"])
    for name, debugger in document["debuggers"].items():
        written = debugger.get("permissions") or {}
        assert {interlock: written.get(interlock) for interlock in INTERLOCKS} == dict.fromkeys(INTERLOCKS, False), (name, written)
        if debugger.get("discovered_by") == "usb_serial_inventory":
            assert debugger.get("probe_inventory") == "incomplete", (name, debugger)
            assert str(debugger.get("probe_inventory_note") or "").strip(), (name, debugger)

    ports = document.get("com_ports") or {}
    assert PORT in ports, sorted(ports)
    for name, port in ports.items():
        identified = any(port.get(key) for key in PORT_IDENTITIES) or str(port.get("device") or "").startswith(BY_ID)
        assert identified, (name, sorted(port))

    shipped = json.loads((PACKAGED_SCHEMAS / "config.schema.json").read_text(encoding="utf-8"))
    assert not schema_errors(shipped, document), schema_errors(shipped, document)


def test_each_agent_gets_the_packaged_skill_in_its_own_directory(first_run: FirstRun) -> None:
    """The skill file is the packaged one, byte for byte, where that agent looks for skills."""
    home = first_run.newcomer.home
    packaged = PACKAGED_SKILL.read_bytes()
    for agent in AGENTS:
        step = first_run.setups[agent][1]["steps"]["skill_install"]
        target = home / AGENT_ROOTS[agent] / SKILL
        assert Path(step["target_path"]) == target, (agent, step)
        assert target.read_bytes() == packaged, agent
        assert step["registered"] is True, (agent, step)
    codex_skill = home / AGENT_ROOTS["codex"] / SKILL
    assert str(codex_skill) in (home / CODEX_SKILL_REGISTRATION).read_text(encoding="utf-8")


def test_each_agent_registers_the_launcher_the_newcomer_runs_and_no_working_directory(first_run: FirstRun) -> None:
    """The user-level registrations docs/mcp-hosts.md shows, with the verified absolute launcher and no `cwd`."""
    home, launcher = first_run.newcomer.home, first_run.newcomer.launcher
    claude = json.loads((home / REGISTRATIONS["claude-code"]).read_text(encoding="utf-8"))
    assert claude["mcpServers"]["agentic-hil"] == {"type": "stdio", "command": launcher, "args": ["mcp-stdio"]}, claude["mcpServers"]
    codex = read_toml(home / REGISTRATIONS["codex"])["mcp_servers"]["agentic-hil"]
    assert codex.get("command") == launcher, codex
    assert codex.get("args") == ["mcp-stdio"], codex
    assert codex.get("enabled") is True, codex
    assert "cwd" not in codex, codex
    opencode = json.loads((home / REGISTRATIONS["opencode"]).read_text(encoding="utf-8"))
    assert opencode["mcp"]["agentic-hil"] == {"type": "local", "command": [launcher, "mcp-stdio"], "enabled": True}, opencode["mcp"]
    for agent in AGENTS:
        step = first_run.setups[agent][1]["steps"]["mcp_config"]
        assert Path(step["path"]) == home / REGISTRATIONS[agent], (agent, step)


def test_each_agent_gets_the_write_restriction_security_md_describes(first_run: FirstRun) -> None:
    """Claude Code: one deny rule per protected tree. opencode: nothing written, and said so. Codex: nothing needed."""
    home = first_run.newcomer.home
    trees = [first_run.config_path.parent, Path(first_run.configuration()["state_root"])]
    deny = json.loads((home / CLAUDE_SETTINGS).read_text(encoding="utf-8"))["permissions"]["deny"]
    assert len(deny) == len(trees), deny
    for tree in trees:
        naming = [rule for rule in deny if rule.startswith("Edit(") and rule.endswith(f"{tree.as_posix()}/**)")]
        assert len(naming) == 1, (tree.as_posix(), deny)
    opencode = json.loads((home / REGISTRATIONS["opencode"]).read_text(encoding="utf-8"))
    assert "permission" not in opencode, opencode
    for agent in ("codex", "opencode"):
        step = first_run.setups[agent][1]["steps"]["agent_write_restriction"]
        assert step["ok"] is True, (agent, step)


def test_doctor_says_the_newcomers_bench_is_healthy(first_run: FirstRun) -> None:
    status, verdict = first_run.doctor
    assert status == 0, verdict.get("summary")
    assert verdict["ok"] is True, verdict


def test_the_demo_plan_is_green_from_the_newcomers_project(first_run: FirstRun, board_images: BoardImages) -> None:
    """`agentic-hil test-reactor --test-config testconfig.yaml`, from the project, on the board.

    The newcomer's build is flashed, so the board is marked as carrying another
    image and gets the session's demo back afterwards. The run took its device
    locks in the newcomer's home, which is where the module docstring says they
    go.
    """
    board_images.displaced = True
    status, report = first_run.newcomer.document("test-reactor", "--test-config", "testconfig.yaml", cwd=first_run.project)
    assert status == 0, report.get("summary")
    assert report["ok"] is True, report
    assert report["cleanup_ok"] is True, report
    assert report["audit_ok"] is True, report
    steps = report["steps"]
    assert [step["action"] for step in steps] == ["flash", "uart_open", "reset", "uart_read"], steps
    for step in steps:
        assert step["result"]["ok"] is True, step
    assert (first_run.newcomer.home / ".agentic-hil" / "device-locks").is_dir()


def test_the_readme_four_calls_hear_the_banner_through_the_registered_server(
    first_run: FirstRun, board_images: BoardImages, tmp_path: Path
) -> None:
    """The four calls the README lists, to the server the Claude Code registration starts, from the project."""
    board_images.displaced = True
    registered = json.loads((first_run.newcomer.home / REGISTRATIONS["claude-code"]).read_text(encoding="utf-8"))["mcpServers"]["agentic-hil"]
    server = Server(
        [registered["command"], *registered["args"]],
        cwd=first_run.project,
        environment=first_run.newcomer.environment,
        stderr_path=tmp_path / "server.stderr",
    )
    opened = False
    try:
        server.greet()
        flashed = server.call("flash_firmware", {"image_path": DEMO_IMAGE.as_posix()})
        assert flashed["ok"] is True, flashed
        started = server.call("com_session_start", {"port_id": PORT})
        assert started["ok"] is True, started
        opened = True
        reset = server.call("reset_target", {"mode": "run"})
        assert reset["ok"] is True, reset
        heard = ""
        deadline = time.monotonic() + BANNER_TIMEOUT_S
        while BANNER not in heard and time.monotonic() < deadline:
            feedback = server.call("com_read", {"port_id": PORT, "wait_timeout_s": 5})
            assert feedback["ok"] is True, feedback
            heard += feedback["data"]["text"]
        assert BANNER in heard, heard
    finally:
        if opened:
            with suppress(AssertionError, OSError, ValueError):
                server.call("com_session_stop", {"port_id": PORT})
        server.close()


def test_the_readme_pytest_regression_passes_in_the_project(first_run: FirstRun, board_images: BoardImages) -> None:
    """`pytest tests/` in the project flashes the ELF, resets the target and asserts the banner, and passes."""
    board_images.displaced = True
    runner = shutil.which("pytest", path=first_run.newcomer.environment["PATH"])
    assert runner is not None, "pytest is not on the newcomer's PATH, next to the product under test"
    ran = subprocess.run(
        [runner, "tests/"],
        capture_output=True,
        text=True,
        cwd=str(first_run.project),
        env=first_run.newcomer.environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    assert ran.returncode == 0, f"{ran.stdout[-3000:]}\n{ran.stderr[-2000:]}"
    summary = ran.stdout.strip().splitlines()[-1]
    assert re.search(r"\b1 passed\b", summary) and "skipped" not in summary, ran.stdout[-3000:]


def test_agent_install_after_setup_changes_nothing_and_says_so(first_run: FirstRun) -> None:
    """The user half again, for every agent `setup` already installed: nothing written, nothing to restart."""
    home = first_run.newcomer.home
    watched = [*AGENT_ROOTS.values(), *REGISTRATIONS.values()]
    before = files_under(home, *watched)
    for agent in AGENTS:
        status, result = first_run.newcomer.document("agent-install", "--agent", agent, cwd=first_run.project)
        assert status == 0, (agent, result)
        assert result["ok"] is True, (agent, result)
        assert result["restart_required"] is False, (agent, result)
        assert result["steps"]["skill_install"]["installed"] is False, (agent, result["steps"])
        assert result["steps"]["skill_install"]["updated"] is False, (agent, result["steps"])
        assert result["steps"]["mcp_config"]["skipped"] is True, (agent, result["steps"])
        assert result["permission_changes"] == [], (agent, result["permission_changes"])
    assert files_under(home, *watched) == before


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_install_alone_writes_the_skill_and_the_registration_and_a_second_run_changes_nothing(
    first_run: FirstRun, agent: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`agentic-hil agent-install --agent <agent>` in an account that has nothing yet, from its home.

    It writes the agent's skill and its user-level MCP registration and nothing
    else, needs and creates no workspace and no configuration, and asks for the
    restart a new registration needs. The second run finds both current, writes
    nothing and asks for no restart.
    """
    newcomer = newcomer_account(tmp_path_factory.mktemp(f"agent-install-{agent}"))
    status, first = newcomer.document("agent-install", "--agent", agent, cwd=newcomer.home)
    assert status == 0, first
    assert first["ok"] is True, first
    assert first["restart_required"] is True, first
    assert str(first.get("restart_notice") or "").strip(), first
    assert first["steps"]["skill_install"]["installed"] is True, first["steps"]
    assert first["steps"]["mcp_config"].get("skipped") is not True, first["steps"]
    assert first["permission_changes"] == [], first["permission_changes"]
    written = files_under(newcomer.home)
    assert set(written) == AGENT_INSTALL_WRITES[agent], sorted(written)
    assert (newcomer.home / AGENT_ROOTS[agent] / SKILL).read_bytes() == PACKAGED_SKILL.read_bytes()

    status, second = newcomer.document("agent-install", "--agent", agent, cwd=newcomer.home)
    assert status == 0, second
    assert second["ok"] is True, second
    assert second["restart_required"] is False, second
    assert second["steps"]["skill_install"]["installed"] is False, second["steps"]
    assert second["steps"]["skill_install"]["updated"] is False, second["steps"]
    assert second["steps"]["mcp_config"]["skipped"] is True, second["steps"]
    assert files_under(newcomer.home) == written


@pytest.mark.parametrize("agent", AGENTS)
def test_skill_install_writes_the_packaged_skill_once_and_says_so_the_second_time(
    first_run: FirstRun, agent: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`agentic-hil skill-install --agent <agent>` twice in a fresh account: installed, then already installed."""
    newcomer = newcomer_account(tmp_path_factory.mktemp(f"skill-install-{agent}"))
    target = newcomer.home / AGENT_ROOTS[agent] / SKILL
    status, first = newcomer.document("skill-install", "--agent", agent, cwd=newcomer.home)
    assert status == 0, first
    assert first["ok"] is True, first
    assert first["installed"] is True, first
    assert first["registered"] is True, first
    assert Path(first["target_path"]) == target, first
    assert target.read_bytes() == PACKAGED_SKILL.read_bytes()
    written = files_under(newcomer.home)
    expected = {(AGENT_ROOTS[agent] / SKILL).as_posix()} | ({CODEX_SKILL_REGISTRATION.as_posix()} if agent == "codex" else set())
    assert set(written) == expected, sorted(written)

    status, second = newcomer.document("skill-install", "--agent", agent, cwd=newcomer.home)
    assert status == 0, second
    assert second["ok"] is True, second
    assert second["installed"] is False and second["updated"] is False, second
    assert files_under(newcomer.home) == written


def test_mcp_config_prints_the_launcher_it_registers_and_writes_a_project_file_only_once(first_run: FirstRun) -> None:
    """The printed document is the one docs/mcp-hosts.md shows; `--output` writes it and will not overwrite it."""
    newcomer = first_run.newcomer
    printed = [newcomer.run("mcp-config", cwd=first_run.project) for _ in range(2)]
    for answer in printed:
        assert answer.returncode == 0, answer.stderr
    assert printed[0].stdout == printed[1].stdout
    document = json.loads(printed[0].stdout)
    assert document == {"mcpServers": {"agentic-hil": {"command": newcomer.launcher, "args": ["mcp-stdio"]}}}, document

    project = a_fresh_demo(newcomer, "second-project")
    target = project / ".mcp.json"
    status, written = newcomer.document("mcp-config", "--output", ".mcp.json", cwd=project)
    assert status == 0, written
    assert written["ok"] is True, written
    assert Path(written["path"]).resolve() == target.resolve(), written
    assert json.loads(target.read_text(encoding="utf-8")) == document
    content = target.read_bytes()
    status, again = newcomer.document("mcp-config", "--output", ".mcp.json", cwd=project)
    assert status != 0, again
    assert again["ok"] is False, again
    assert again["error_type"] == "mcp_config_exists", again
    assert target.read_bytes() == content


@pytest.mark.parametrize(("command", "shipped"), [("schema", "config.schema.json"), ("test-schema", "testconfig.schema.json")])
def test_the_schema_commands_print_the_shipped_schema_which_holds_the_demo_and_write_it_only_once(
    first_run: FirstRun, command: str, shipped: str
) -> None:
    """The printed schema is the packaged one, a valid schema, and it accepts what the first run holds.

    `schema` accepts the configuration `setup` generated, `test-schema` the
    demo's own `testconfig.yaml`. `--output` writes the same text, refuses to
    overwrite it without `--force`, and rewrites it with it.
    """
    newcomer = first_run.newcomer
    packaged = (PACKAGED_SCHEMAS / shipped).read_text(encoding="utf-8")
    printed = newcomer.run(command, cwd=first_run.project)
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout == (packaged if packaged.endswith("\n") else f"{packaged}\n")
    schema = json.loads(printed.stdout)
    held = first_run.configuration() if command == "schema" else yaml.safe_load((first_run.project / "testconfig.yaml").read_text(encoding="utf-8"))
    assert not schema_errors(schema, held), schema_errors(schema, held)

    target = newcomer.home / "schemas" / shipped
    status, written = newcomer.document(command, "--output", str(target), cwd=first_run.project)
    assert status == 0, written
    assert written["ok"] is True, written
    assert target.read_text(encoding="utf-8") == packaged
    status, again = newcomer.document(command, "--output", str(target), cwd=first_run.project)
    assert status != 0, again
    assert again["ok"] is False, again
    assert again["error_type"] == "schema_exists", again
    target.write_text("{}", encoding="utf-8")
    status, forced = newcomer.document(command, "--output", str(target), "--force", cwd=first_run.project)
    assert status == 0, forced
    assert forced["ok"] is True, forced
    assert target.read_text(encoding="utf-8") == packaged
