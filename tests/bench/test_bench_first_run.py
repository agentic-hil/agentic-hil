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

``upgrade`` and ``uninstall`` need an installation of the newcomer's own rather
than this checkout, because they replace and remove whatever they run out of.
So a second clean account installs the product the way the "Install" section
of ``AI_AGENT_QUICKSTART.md`` says, its lines in their order up to the first
that works, from the package index ``tools/bench/Dockerfile`` builds into the
bench image: this commit's wheel and its locked dependencies, handed to pip
through pip's own index variables, the way a machine behind a local mirror is
configured. It sets up every agent, then upgrades to a wheel of this same tree
one release higher while an MCP server it started holds the board, and then
takes itself back with ``uninstall`` and the line that result ends on. The
tier's own installation is never touched. Only the bench image carries that
index, so those tests are marked ``wheelhouse`` and deselected everywhere else.

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
import shlex
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
from packaging.version import Version
from support import scaled_time_bound

from .conftest import (
    BENCH_ONLY,
    BUILD_TIMEOUT_S,
    CHECKOUT_SOURCES,
    COMMAND_TIMEOUT_S,
    DEMO,
    DEMO_IMAGE,
    NEEDS_THE_WHEELHOUSE,
    REPOSITORY_ROOT,
    WHEELHOUSE,
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


def account_environment(home: Path, temporary: Path) -> dict[str, str]:
    """The caller's environment without what a clean account lacks, and with that account's own roots."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(LEFT_BEHIND_PREFIXES) and name not in LEFT_BEHIND
    }
    return {
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
    }


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
    environment = account_environment(home, temporary)
    interpreter_directory = str(Path(sys.executable).parent)
    search_path = os.pathsep.join(
        [interpreter_directory, *(entry for entry in environment.get("PATH", "").split(os.pathsep) if entry and entry != interpreter_directory)]
    )
    environment.update(PATH=search_path, PYTHONPATH=import_path())
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

    def greet(self) -> dict:
        """The MCP handshake; the server's answer to `initialize`."""
        hello = self.request(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "agentic-hil-bench-tier", "version": "0"}},
        )
        assert hello["result"]["serverInfo"]["name"] == "agentic-hil", hello
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return hello

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


# An installation of the newcomer's own, upgraded while it holds the board and
# then taken back. Everything from here on runs only in the bench image, which
# is where the package index these tests install from is.

FROM_THE_WHEELHOUSE = getattr(pytest.mark, NEEDS_THE_WHEELHOUSE)

# The "Install" block of AI_AGENT_QUICKSTART.md, its lines in their order. The
# document is not in the bench image, so the lines are written out here. The
# block's last line, the uv installer fetched with curl, is left out: it needs
# the network this tier runs without, and a newcomer reaches it only after
# every line above it failed.
QUICKSTART_INSTALL = (
    "agentic-hil --version && agentic-hil setup --help",
    "python -m pip install --user --upgrade agentic-hil",
    "uv tool install --upgrade agentic-hil",
    "pipx install agentic-hil",
)
# What every new account on this machine starts its home from.
SKELETON = Path("/etc/skel")
# What the account's own `python` says about itself: where it is, and where a
# `--user` installation puts console scripts and packages.
ASK_THE_INTERPRETER = (
    "import json, sys, sysconfig; user = sysconfig.get_preferred_scheme('user'); "
    "print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix, "
    "'user_scripts': sysconfig.get_path('scripts', user), 'user_purelib': sysconfig.get_path('purelib', user)}))"
)
# The version stamps a release moves inside the wheel, of the positions
# `tools/check_version_consistency.py --list` names: the project's version, the
# package's `__version__`, and the release the packaged skill says it is from.
STAMPED_SKILL = Path("src") / "agentic_hil" / "skills" / "agentic-hil" / "SKILL.md"
VERSION_STAMPS = (
    (Path("pyproject.toml"), rb'^version = "[^"]+"', b'version = "%s"'),
    (Path("src") / "agentic_hil" / "__init__.py", rb'^__version__ = "[^"]+"', b'__version__ = "%s"'),
    (STAMPED_SKILL, rb'^  agentic_hil_version: "[^"]+"', b'  agentic_hil_version: "%s"'),
)
# What the operator writes beside the product's own entries before taking it
# back: another MCP server, another deny rule, their own instructions for Codex,
# a skill of their own, and an edit to the product's opencode entry, which
# makes that entry theirs.
OPERATOR_SERVER = "/opt/another/server"
OPERATOR_RULE = "Bash(rm -rf:*)"
OPERATOR_INSTRUCTIONS = "# The operator's own instructions\n\nKeep answers short.\n\n"
OPERATOR_TOML_HEAD = 'model = "operator-choice"\n\n'
OPERATOR_TOML_TAIL = f'\n[mcp_servers.another-server]\ncommand = "{OPERATOR_SERVER}"\n'
OPERATOR_SKILL = AGENT_ROOTS["claude-code"] / "skills" / "operator-skill" / "SKILL.md"


def login_shell(line: str, *, environment: dict[str, str], cwd: Path, answer: str = "") -> subprocess.CompletedProcess[str]:
    """One line typed into a new login shell of the account, with `answer` typed at whatever it asks."""
    return subprocess.run(
        ["bash", "-lc", line],
        input=answer,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def an_account_of_its_own(root: Path, index: Path) -> tuple[Path, dict[str, str]]:
    """A home made the way this machine makes one, the account's own roots, and pip pointed at the index.

    The home is a copy of the skeleton every new account here starts from, so a
    new login shell reads the profile a newcomer's shell reads, and nothing of
    this tier's environment is on its PATH. The index reaches pip through pip's
    own variables, `PIP_FIND_LINKS` and `PIP_NO_INDEX`, the way a machine that
    installs from a local mirror is configured; the product is told nothing.
    """
    if not SKELETON.is_dir():
        refuse(f"there is no {SKELETON} to make a new account's home from, so the newcomer's own installation cannot be made here")
    home = root / "home"
    temporary = root / "tmp"
    shutil.copytree(SKELETON, home, symlinks=True)
    temporary.mkdir()
    environment = account_environment(home, temporary)
    environment.update(PIP_FIND_LINKS=str(index), PIP_NO_INDEX="1")
    return home, environment


def the_wheel_of_this_commit(index: Path) -> str:
    """The version of the one wheel of the product the index carries."""
    wheels = sorted(index.glob("agentic_hil-*.whl"))
    if len(wheels) != 1:
        refuse(f"the package index carries {len(wheels)} wheels of the product rather than the one of this commit: {[wheel.name for wheel in wheels]}")
    return wheels[0].name.split("-")[1]


def a_release_above(version: str) -> str:
    """The release after `version`: its last release component, one higher."""
    release = Version(version).release
    return ".".join(str(part) for part in (*release[:-1], release[-1] + 1))


def build_the_next_release(version: str, *, into: Path, work: Path) -> tuple[Path, bytes]:
    """A wheel of this tree stamped `version` where a release stamps it, and the skill it carries."""
    work.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy2(REPOSITORY_ROOT / name, work / name)
    shutil.copytree(CHECKOUT_SOURCES, work / "src", ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
    for relative, pattern, stamp in VERSION_STAMPS:
        target = work / relative
        stamped, count = re.subn(pattern, stamp % version.encode(), target.read_bytes(), count=1, flags=re.MULTILINE)
        assert count == 1, f"{relative.as_posix()} carries no version stamp where a release moves one"
        target.write_bytes(stamped)
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(into), str(work)],
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_S,
        check=False,
    )
    wheels = sorted(into.glob(f"agentic_hil-{version}-*.whl"))
    if built.returncode != 0 or len(wheels) != 1:
        pytest.fail(f"a wheel of this tree as release {version} did not build:\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}", pytrace=False)
    return wheels[0], (work / STAMPED_SKILL).read_bytes()


def how_it_runs(pid: int) -> dict[str, object]:
    """What the kernel says about how one process was started: its image, its arguments, its VIRTUAL_ENV and where it runs."""
    process = Path("/proc") / str(pid)
    facts: dict[str, object] = {}
    with suppress(OSError):
        facts["exe"] = os.readlink(process / "exe")
    with suppress(OSError):
        facts["argv"] = [part.decode() for part in (process / "cmdline").read_bytes().split(b"\0") if part]
    with suppress(OSError):
        variables = (process / "environ").read_bytes().split(b"\0")
        facts["virtual_env"] = next((entry.split(b"=", 1)[1].decode() for entry in variables if entry.startswith(b"VIRTUAL_ENV=")), "")
    with suppress(OSError):
        facts["cwd"] = os.readlink(process / "cwd")
    return facts


@dataclass(frozen=True)
class OwnInstallation:
    """The account's own installation, and what it answered from the install to the upgrade."""

    newcomer: Newcomer
    project: Path
    attempts: list[tuple[str, subprocess.CompletedProcess[str]]]
    version: subprocess.CompletedProcess[str]
    setup_help: subprocess.CompletedProcess[str]
    interpreter: dict[str, str]
    where: str
    beside: list[str]
    installed: str
    released: str
    setups: dict[str, tuple[int, dict]]
    config_path: Path
    state_root: Path
    skills_before: dict[str, bytes]
    registrations_before: dict[str, str]
    registered: list[str]
    next_skill: bytes
    server_pid: int
    server_version: str
    server_facts: dict[str, object]
    upgrade: tuple[int, dict]
    skills_after: dict[str, bytes]
    registrations_after: dict[str, str]
    after_the_swap: dict
    free: dict
    restarted_version: str
    version_after: subprocess.CompletedProcess[str]


@pytest.fixture(scope="module")
def own_installation(board_image_builds: BoardImages, tmp_path_factory: pytest.TempPathFactory) -> OwnInstallation:
    """The product installed into an account of its own the way the quick start says, then upgraded under a held board.

    In order: the install block's lines up to the first that works, the
    confirmation in a new shell, `setup` for every agent, and a wheel of this
    tree one release higher. Then the MCP server the account's Claude Code
    registration starts opens a COM session, the board is read as held from a
    second process, the wheel is put on the index and `agentic-hil upgrade`
    runs, and the server the upgrade did not restart closes its session. Last,
    the server is started again and a new shell asks for the version. Nothing
    is flashed, so the board runs the demo throughout.

    It asks for the session's board images so the tier's own session is set up,
    and the demo is on the board, before the account's first command reaches it.
    """
    root = tmp_path_factory.mktemp("own-installation")
    index = root / "index"
    shutil.copytree(WHEELHOUSE, index)
    installed = the_wheel_of_this_commit(index)
    home, environment = an_account_of_its_own(root, index)

    attempts: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    for line in QUICKSTART_INSTALL:
        attempts.append((line, login_shell(line, environment=environment, cwd=home)))
        if attempts[-1][1].returncode == 0:
            break
    else:
        tried = "\n".join(f"$ {line}\nexit {done.returncode}\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}" for line, done in attempts)
        pytest.fail(f"no line of the quick start's install block installed the product:\n{tried}", pytrace=False)

    # "Then confirm, opening a new shell if asked."
    version = login_shell("agentic-hil --version", environment=environment, cwd=home)
    setup_help = login_shell("agentic-hil setup --help", environment=environment, cwd=home)
    login_path = login_shell('printf "%s" "$PATH"', environment=environment, cwd=home).stdout
    launcher = shutil.which("agentic-hil", path=login_path)
    asked = login_shell(f"python -c {shlex.quote(ASK_THE_INTERPRETER)}", environment=environment, cwd=home)
    resolved = login_shell(f"python -c {shlex.quote(WHERE_THE_PRODUCT_CAME_FROM)}", environment=environment, cwd=home)
    if launcher is None or asked.returncode != 0 or resolved.returncode != 0:
        pytest.fail(f"a new shell of the account does not find what it installed: `agentic-hil` is {launcher}\n{asked.stderr}\n{resolved.stderr}", pytrace=False)
    interpreter = json.loads(asked.stdout)
    where = resolved.stdout.strip()
    # Never upgrade or remove anything but the account's own installation.
    if not (
        Path(launcher).is_relative_to(home)
        and Path(launcher).parent == Path(interpreter["user_scripts"])
        and Path(where).resolve().is_relative_to(Path(interpreter["user_purelib"]).resolve())
    ):
        pytest.fail(f"the installation a new shell of the account runs is not the account's own (`agentic-hil` is {launcher}, `agentic_hil` is {where}), so it is neither upgraded nor removed", pytrace=False)
    beside = sorted(entry.name for entry in Path(interpreter["user_scripts"]).iterdir())

    newcomer = Newcomer(home=home, environment={**environment, "PATH": login_path}, launcher=launcher)
    project = a_fresh_demo(newcomer, "firmware")
    setups = {agent: newcomer.document("setup", "--agent", agent, cwd=project) for agent in AGENTS}
    for agent, (status, result) in setups.items():
        if status != 0 or result.get("ok") is not True:
            pytest.fail(f"`agentic-hil setup --agent {agent}` did not set the account's own installation up: {result.get('summary')}", pytrace=False)
    config_path = Path(setups[AGENTS[0]][1]["scopes"]["project"]["config_path"])
    state_root = Path(yaml.safe_load(config_path.read_text(encoding="utf-8"))["state_root"])
    skills_before = {agent: (home / AGENT_ROOTS[agent] / SKILL).read_bytes() for agent in AGENTS}
    registrations_before = files_under(home, *REGISTRATIONS.values())

    released = a_release_above(installed)
    next_wheel, next_skill = build_the_next_release(released, into=root / "next", work=root / "next-source")

    entry = json.loads((home / REGISTRATIONS["claude-code"]).read_text(encoding="utf-8"))["mcpServers"]["agentic-hil"]
    registered = [entry["command"], *entry["args"]]
    server = Server(registered, cwd=project, environment=newcomer.environment, stderr_path=root / "server.stderr")
    session_open = False
    try:
        server_version = server.greet()["result"]["serverInfo"]["version"]
        opened = server.call("com_session_start", {"port_id": PORT})
        if opened.get("ok") is not True:
            pytest.fail(f"the account's server could not open {PORT}, so there is no held board to upgrade under: {opened.get('summary')}", pytrace=False)
        session_open = True
        _, held = newcomer.document("lease-status", cwd=project)
        if held.get("ok") is not True or held.get("bench_held") is not True or not held.get("held_devices"):
            pytest.fail(f"the board does not read as held from a second process, so the upgrade is not asked: {held.get('summary')}", pytrace=False)
        server_facts = how_it_runs(server.process.pid)
        shutil.copy2(next_wheel, index)
        upgrade = newcomer.document("upgrade", cwd=project)
        skills_after = {agent: (home / AGENT_ROOTS[agent] / SKILL).read_bytes() for agent in AGENTS}
        registrations_after = files_under(home, *REGISTRATIONS.values())
        try:
            after_the_swap = server.call("com_session_stop", {"port_id": PORT})
        except AssertionError as failure:
            after_the_swap = {"ok": False, "summary": f"the server did not answer `com_session_stop` after the upgrade: {failure}"}
        session_open = False
    finally:
        if session_open:
            with suppress(AssertionError, OSError, ValueError):
                server.call("com_session_stop", {"port_id": PORT})
        server.close()
    _, free = newcomer.document("lease-status", cwd=project)

    restarted = Server(registered, cwd=project, environment=newcomer.environment, stderr_path=root / "restarted.stderr")
    try:
        restarted_version = restarted.greet()["result"]["serverInfo"]["version"]
    finally:
        restarted.close()
    version_after = login_shell("agentic-hil --version", environment=environment, cwd=home)
    return OwnInstallation(
        newcomer=newcomer,
        project=project,
        attempts=attempts,
        version=version,
        setup_help=setup_help,
        interpreter=interpreter,
        where=where,
        beside=beside,
        installed=installed,
        released=released,
        setups=setups,
        config_path=config_path,
        state_root=state_root,
        skills_before=skills_before,
        registrations_before=registrations_before,
        registered=registered,
        next_skill=next_skill,
        server_pid=server.process.pid,
        server_version=server_version,
        server_facts=server_facts,
        upgrade=upgrade,
        skills_after=skills_after,
        registrations_after=registrations_after,
        after_the_swap=after_the_swap,
        free=free,
        restarted_version=restarted_version,
        version_after=version_after,
    )


@FROM_THE_WHEELHOUSE
def test_the_first_install_line_that_works_installs_the_product_for_the_account_alone(own_installation: OwnInstallation) -> None:
    """AI_AGENT_QUICKSTART.md, "Install": the lines in order, stopping at the first that works, then the confirmation.

    Nothing is installed in the account yet, so the first line fails, and the
    pip line is the first that works. A new shell then finds `agentic-hil` in
    the account's own script directory, without any `Not on PATH?` step, and
    both confirmation lines answer: the version is the one the index carries.
    What the account's `python` imports is its own `--user` installation.
    """
    installation = own_installation
    first_line, first = installation.attempts[0]
    assert first.returncode != 0, f"`{first_line}` found an installation in an account that has none:\n{first.stdout}"
    worked = [line for line, done in installation.attempts if done.returncode == 0]
    assert worked == [QUICKSTART_INSTALL[1]], [(line, done.returncode, done.stderr[-500:]) for line, done in installation.attempts]
    assert installation.version.returncode == 0, installation.version.stderr
    assert installation.version.stdout.strip() == installation.installed, installation.version.stdout
    assert installation.setup_help.returncode == 0, installation.setup_help.stderr
    assert "--agent" in installation.setup_help.stdout, installation.setup_help.stdout
    launcher = Path(installation.newcomer.launcher)
    assert launcher.is_relative_to(installation.newcomer.home), launcher
    assert launcher.parent == Path(installation.interpreter["user_scripts"]), (launcher, installation.interpreter)
    assert Path(installation.where).resolve().is_relative_to(Path(installation.interpreter["user_purelib"]).resolve()), installation.where


@FROM_THE_WHEELHOUSE
def test_upgrade_while_the_accounts_server_holds_the_board_is_not_refused_and_lands_the_next_release(own_installation: OwnInstallation) -> None:
    """`agentic-hil upgrade` with the account's own MCP server holding the board: done, and a restart asked for.

    AI_AGENT_QUICKSTART.md: it "upgrades through the manager that owns the
    installation" and "names every server still running out of it rather than
    being refused because one is". So the upgrade is not refused: pip, which
    installed it, moves it from the version on the index to the next release
    in the account's user site, and the result says a restart is required. The
    server it did not reach still answers with the release it imported and
    closes its session, which frees the board; started again it runs the new
    release, and so does `agentic-hil` in a new shell.
    """
    installation = own_installation
    status, result = installation.upgrade
    assert status == 0, result.get("summary")
    assert result["ok"] is True, result
    assert "error_type" not in result, result
    assert result["upgraded_on_disk"] is True, result
    assert (result["previous_version"], result["version"]) == (installation.installed, installation.released), result
    assert installation.server_version == installation.installed
    assert result["manager"] == "pip", result
    command = result["command"]
    assert command[:4] == [installation.interpreter["executable"], "-m", "pip", "install"], command
    assert {"--upgrade", "--user"} <= set(command) and command[-1] == "agentic-hil", command
    assert result["install"]["returncode"] == 0, result["install"]
    assert result["restart_required"] is True, result
    assert installation.after_the_swap["ok"] is True, installation.after_the_swap
    assert installation.free["bench_held"] is False and installation.free["held_devices"] == [], installation.free
    assert installation.restarted_version == installation.released
    assert installation.version_after.returncode == 0, installation.version_after.stderr
    assert installation.version_after.stdout.strip() == installation.released, installation.version_after.stdout


@FROM_THE_WHEELHOUSE
def test_upgrade_names_the_server_holding_the_board_as_the_one_to_restart(own_installation: OwnInstallation) -> None:
    """`restart_required_by` names the server by pid, start time and the project directory it was started in.

    AI_AGENT_QUICKSTART.md: `restart_required` "is true when a server started
    out of this installation is still running, which `restart_required_by`
    names by pid, start time and, where the host can read it, the project
    directory". The account's server was started out of this installation,
    through the console script its registration names, and was still running
    when the upgrade replaced the package under it. So it is the one entry,
    started in the project, and the summary names it by pid. The failure
    message carries how the kernel says that server was started, which is what
    a fake process table of this case is made from.
    """
    installation = own_installation
    _, result = installation.upgrade
    how = json.dumps(
        {
            "server": installation.server_facts,
            "interpreter": installation.interpreter,
            "scripts_beside": installation.beside,
            "manager": result.get("manager"),
            "command": result.get("command"),
        },
        indent=1,
    )
    holders = result.get("restart_required_by")
    assert isinstance(holders, list) and len(holders) == 1, (
        f"`restart_required_by` does not name the server running out of this installation, pid {installation.server_pid}: {holders!r}. "
        f"Summary: {result.get('summary')}\nHow that server runs:\n{how}"
    )
    (holder,) = holders
    assert holder["pid"] == installation.server_pid, holder
    assert Path(holder["working_directory"]).resolve() == installation.project.resolve(), holder
    assert str(holder.get("started_at") or "").strip(), holder
    assert result["restart_required_by_count"] == 1, result
    assert f"pid {installation.server_pid}" in result["summary"], result["summary"]


@FROM_THE_WHEELHOUSE
def test_upgrade_refreshes_every_agents_skill_from_the_new_release_and_keeps_every_registration(own_installation: OwnInstallation) -> None:
    """The skills `setup` wrote are rewritten out of the new release; the registrations already name the right launcher.

    An upgrade refreshes, out of the new package, the skill and the MCP
    registration of every agent that has one. Each skill file was the packaged
    one of this tree and is now, byte for byte, the one the next release
    carries. The registrations name the account's launcher, which the upgrade
    did not move, so none is rewritten and every registration file is as it
    was.
    """
    installation = own_installation
    _, result = installation.upgrade
    refreshed = {entry["agent"]: entry for entry in result["refreshed"]}
    assert len(result["refreshed"]) == len(AGENTS) and set(refreshed) == set(AGENTS), result["refreshed"]
    packaged = PACKAGED_SKILL.read_bytes()
    assert installation.next_skill != packaged
    for agent, entry in refreshed.items():
        assert entry["ok"] is True, (agent, entry)
        assert (entry["skill"], entry["registration"], entry["registration_rewritten"]) == (True, True, False), (agent, entry)
        step = entry["install"]["result"]["steps"]["skill_install"]
        assert step["updated"] is True and step["version"] == installation.released, (agent, step)
        assert installation.skills_before[agent] == packaged, agent
        assert installation.skills_after[agent] == installation.next_skill, agent
    assert installation.registrations_after == installation.registrations_before, sorted(set(installation.registrations_after.items()) ^ set(installation.registrations_before.items()))
    assert installation.registered == [installation.newcomer.launcher, "mcp-stdio"], installation.registered


@dataclass(frozen=True)
class TakenBack:
    """What `uninstall`, and then the line its result ends on, did to the account."""

    installation: OwnInstallation
    ours_denied: list[str]
    planted: dict[str, dict]
    planted_bytes: dict[str, bytes]
    kept_before: dict[str, str]
    uninstall: tuple[int, dict]
    version_before_removal: subprocess.CompletedProcess[str]
    removal: subprocess.CompletedProcess[str] | None
    found_after: subprocess.CompletedProcess[str]
    imported_after: subprocess.CompletedProcess[str]
    kept_after: dict[str, str]


@pytest.fixture(scope="module")
def taken_back(own_installation: OwnInstallation) -> TakenBack:
    """The operator's own entries written beside the product's, then the two removal lines, in their order.

    docs/installation.md, "Uninstalling": `agentic-hil uninstall` first, then
    "the line the result ends on". Before that the operator has made the
    account theirs the way one does: another MCP server beside the product's in
    Claude Code's and Codex's registrations, a deny rule of their own, their own
    instructions above the product's in Codex's AGENTS.md, a skill of their own,
    and an edit to the product's opencode entry.
    """
    installation = own_installation
    newcomer, home = installation.newcomer, installation.newcomer.home

    claude_path = home / REGISTRATIONS["claude-code"]
    claude = json.loads(claude_path.read_text(encoding="utf-8"))
    claude["mcpServers"]["another-server"] = {"type": "stdio", "command": OPERATOR_SERVER, "args": []}
    claude["theme"] = "dark"
    claude_path.write_text(json.dumps(claude, indent=2) + "\n", encoding="utf-8")

    settings_path = home / CLAUDE_SETTINGS
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    ours_denied = list(settings["permissions"]["deny"])
    settings["permissions"]["deny"].append(OPERATOR_RULE)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    codex_path = home / REGISTRATIONS["codex"]
    codex_path.write_text(OPERATOR_TOML_HEAD + codex_path.read_text(encoding="utf-8") + OPERATOR_TOML_TAIL, encoding="utf-8")
    instructions_path = home / CODEX_SKILL_REGISTRATION
    instructions_path.write_text(OPERATOR_INSTRUCTIONS + instructions_path.read_text(encoding="utf-8"), encoding="utf-8")

    opencode_path = home / REGISTRATIONS["opencode"]
    opencode = json.loads(opencode_path.read_text(encoding="utf-8"))
    opencode["mcp"]["agentic-hil"]["environment"] = {"OPERATOR": "1"}
    opencode["mcp"]["another-server"] = {"type": "local", "command": [OPERATOR_SERVER], "enabled": True}
    opencode_path.write_text(json.dumps(opencode, indent=2) + "\n", encoding="utf-8")

    skill_path = home / OPERATOR_SKILL
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: operator-skill\ndescription: The operator's own skill.\n---\n\nKeep it.\n", encoding="utf-8")

    planted = {"claude": claude, "settings": settings, "codex": read_toml(codex_path)}
    planted_bytes = {"opencode": opencode_path.read_bytes(), "operator skill": skill_path.read_bytes()}
    kept_trees = (installation.config_path.parent.relative_to(home), installation.state_root.relative_to(home))
    kept_before = files_under(home, *kept_trees)
    uninstall = newcomer.document("uninstall", cwd=installation.project)
    version_before_removal = login_shell("agentic-hil --version", environment=newcomer.environment, cwd=home)
    line = (uninstall[1].get("package_removal") or {}).get("command")
    removal = login_shell(line, environment=newcomer.environment, cwd=home, answer="y\n") if isinstance(line, str) and line else None
    found_after = login_shell("command -v agentic-hil", environment=newcomer.environment, cwd=home)
    imported_after = login_shell(f"python -c {shlex.quote('import agentic_hil')}", environment=newcomer.environment, cwd=home)
    return TakenBack(
        installation=installation,
        ours_denied=ours_denied,
        planted=planted,
        planted_bytes=planted_bytes,
        kept_before=kept_before,
        uninstall=uninstall,
        version_before_removal=version_before_removal,
        removal=removal,
        found_after=found_after,
        imported_after=imported_after,
        kept_after=files_under(home, *kept_trees),
    )


@FROM_THE_WHEELHOUSE
def test_uninstall_names_everything_it_took_back_and_the_entry_it_left(taken_back: TakenBack) -> None:
    """docs/installation.md: the skill files, the MCP registrations and the write refusals, each named; the edited entry left.

    Every agent's skill file goes, and with Codex's the line in its AGENTS.md
    that registers it; Claude Code's and Codex's registrations of the product
    go; the two write refusals `setup` added to Claude Code's settings go. The
    opencode entry the operator edited is no longer recognisably the product's
    own, so it is named under `left_alone`, with the reason, and not removed.
    """
    home = taken_back.installation.newcomer.home
    status, result = taken_back.uninstall
    assert status == 0, result.get("summary")
    assert result["ok"] is True, result
    assert result["scope"] == "user", result
    assert sorted(entry["agent"] for entry in result["agents"]) == sorted(AGENTS), result["agents"]
    for entry in result["agents"]:
        assert entry["ok"] is True, entry
    expected = sorted(
        [
            *(str(home / AGENT_ROOTS[agent] / SKILL) for agent in AGENTS),
            str(home / CODEX_SKILL_REGISTRATION),
            f"{home / REGISTRATIONS['claude-code']} :: mcpServers.agentic-hil",
            f"{home / REGISTRATIONS['codex']} :: mcp_servers.agentic-hil",
            *(f"{home / CLAUDE_SETTINGS} :: {rule}" for rule in taken_back.ours_denied),
        ]
    )
    assert sorted(entry["path"] for entry in result["removed"]) == expected, result["removed"]
    for entry in result["removed"]:
        assert str(entry.get("what") or "").strip(), entry
    left = [(entry["path"], bool(str(entry.get("reason") or "").strip())) for entry in result["left_alone"]]
    assert left == [(f"{home / REGISTRATIONS['opencode']} :: mcp.agentic-hil", True)], result["left_alone"]


@FROM_THE_WHEELHOUSE
def test_uninstall_leaves_every_entry_the_operator_wrote_where_it_was(taken_back: TakenBack) -> None:
    """What the operator wrote beside the product's entries is there afterwards, and nothing of the product's is.

    Each file keeps everything but the product's own entry: the other MCP
    server and the theme in `.claude.json`, the operator's deny rule in the
    settings, the other server and the model in Codex's configuration, the
    operator's instructions in AGENTS.md. The opencode file, whose product
    entry the operator edited, and the operator's own skill are byte for byte
    what they were. The product's skill files are gone, and so are the lock
    sidecars it left in the agents' directories.
    """
    home = taken_back.installation.newcomer.home
    planted = taken_back.planted
    claude = json.loads((home / REGISTRATIONS["claude-code"]).read_text(encoding="utf-8"))
    servers = {name: entry for name, entry in planted["claude"]["mcpServers"].items() if name != "agentic-hil"}
    assert claude == {**planted["claude"], "mcpServers": servers}, claude
    settings = json.loads((home / CLAUDE_SETTINGS).read_text(encoding="utf-8"))
    assert settings == {**planted["settings"], "permissions": {**planted["settings"]["permissions"], "deny": [OPERATOR_RULE]}}, settings
    codex = read_toml(home / REGISTRATIONS["codex"])
    codex_servers = {name: entry for name, entry in planted["codex"]["mcp_servers"].items() if name != "agentic-hil"}
    assert codex == {**planted["codex"], "mcp_servers": codex_servers}, codex
    instructions = (home / CODEX_SKILL_REGISTRATION).read_text(encoding="utf-8")
    assert instructions.strip() == OPERATOR_INSTRUCTIONS.strip(), instructions
    assert (home / REGISTRATIONS["opencode"]).read_bytes() == taken_back.planted_bytes["opencode"]
    assert (home / OPERATOR_SKILL).read_bytes() == taken_back.planted_bytes["operator skill"]
    for agent in AGENTS:
        assert not (home / AGENT_ROOTS[agent] / SKILL).exists(), agent
    sidecars = sorted(
        path.relative_to(home).as_posix()
        for path in (*home.glob("*"), *(found for agent in AGENTS for found in (home / AGENT_ROOTS[agent]).rglob("*")))
        if is_lock_sidecar(path)
    )
    assert sidecars == [], sidecars


@FROM_THE_WHEELHOUSE
def test_uninstall_keeps_the_configuration_and_the_state_root_and_ends_on_the_line_that_removes_the_package(taken_back: TakenBack) -> None:
    """docs/installation.md: two trees are left alone and named with their paths; the result ends on the removal line.

    The project configuration and the state root are both under a path the
    result names under `kept`, with the reason, and neither changed. The
    package is still installed after `uninstall`, and the result's last
    sentence is the line that removes it through pip, the manager that owns
    this installation, with the account's own interpreter.
    """
    installation = taken_back.installation
    status, result = taken_back.uninstall
    assert status == 0, result.get("summary")
    kept = result["kept"]
    for entry in kept:
        assert str(entry.get("path") or "").strip() and str(entry.get("reason") or "").strip(), entry
    kept_paths = [Path(entry["path"]).resolve() for entry in kept]
    for tree in (installation.config_path.parent, installation.state_root):
        assert any(tree.resolve().is_relative_to(path) for path in kept_paths), (tree, kept)
    assert installation.config_path.relative_to(installation.newcomer.home).as_posix() in taken_back.kept_before, taken_back.kept_before
    assert taken_back.kept_after == taken_back.kept_before, sorted(set(taken_back.kept_after.items()) ^ set(taken_back.kept_before.items()))
    removal = result["package_removal"]
    assert removal["manager"] == "pip", removal
    command = removal["command"]
    assert shlex.split(command)[:4] == [installation.interpreter["executable"], "-m", "pip", "uninstall"], command
    assert result["summary"].rstrip().endswith(f"`{command}`."), result["summary"]
    assert taken_back.version_before_removal.returncode == 0, taken_back.version_before_removal.stderr
    assert taken_back.version_before_removal.stdout.strip() == installation.released, taken_back.version_before_removal.stdout


@FROM_THE_WHEELHOUSE
def test_the_line_the_uninstall_ends_on_removes_the_package_and_nothing_it_kept(taken_back: TakenBack) -> None:
    """Run at the account's shell, the removal line takes the package away and leaves the two trees.

    Afterwards a new shell finds no `agentic-hil` and the account's `python`
    cannot import the package, while the configuration and the state root are
    still where the result said they would stay.
    """
    installation = taken_back.installation
    removal = taken_back.removal
    assert removal is not None, taken_back.uninstall[1].get("package_removal")
    assert removal.returncode == 0, f"{removal.stdout[-2000:]}\n{removal.stderr[-2000:]}"
    assert taken_back.found_after.returncode != 0, taken_back.found_after.stdout
    assert not Path(installation.newcomer.launcher).exists(), installation.newcomer.launcher
    assert taken_back.imported_after.returncode != 0, taken_back.imported_after.stdout
    assert "ModuleNotFoundError" in taken_back.imported_after.stderr, taken_back.imported_after.stderr
    assert installation.config_path.is_file(), installation.config_path
    assert installation.state_root.is_dir(), installation.state_root


@FROM_THE_WHEELHOUSE
def test_the_tiers_own_installation_is_where_it_was(taken_back: TakenBack) -> None:
    """The account's install, upgrade and removal reached nothing of the installation this tier runs.

    The tier's interpreter still imports this checkout's `agentic_hil` at the
    version of this commit, and its launcher is still beside it.
    """
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    asked = subprocess.run(
        [sys.executable, "-c", "import importlib.metadata, agentic_hil; print(importlib.metadata.version('agentic-hil')); print(agentic_hil.__file__)"],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY_ROOT),
        env=environment,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    assert asked.returncode == 0, asked.stderr
    version, module_file = asked.stdout.splitlines()
    assert version == taken_back.installation.installed, asked.stdout
    assert not_the_checkout(module_file) is None, module_file
    assert (Path(sys.executable).parent / "agentic-hil").is_file(), sys.executable
