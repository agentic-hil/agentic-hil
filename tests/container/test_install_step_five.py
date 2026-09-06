"""Step 5 of install.sh against the real process table, with the agent CLIs shaped the way npm installs them.

Step 5 is the one line of the installer that stays the operator's: it names
every agent CLI that is running right now, because a CLI reads its MCP
registrations when a session starts and the tools it was just registered for
appear in the next session, not this one. It found them with `pgrep -x
<name>`, and that is a process whose `comm` is exactly `codex`.

The npm package `@openai/codex` (0.145.0, the version
evals/install/container/package-lock.json pins; its package.json declares
`"bin": {"codex": "bin/codex.js"}` and that file opens with
`#!/usr/bin/env node`, both read out of the published tarball on 2026-09-06)
is therefore a process whose `comm` is `node` and whose second argument is the
path of the `codex` launcher; its native child is `codex-x86_64-un...`,
which is `codex-x86_64-unknown-linux-musl` cut at the fifteen characters
`comm` holds. `pgrep -x codex` matches neither. So a machine with codex open
was told `no agent CLI of yours is running, so there is nothing to restart`,
the operator did not restart it, and the tools did not appear.

None of the five branches of step 5 had ever produced output in a test: the
restart lines were grepped out of the source. Here they run against the real
`pgrep` and the real `/proc`, with a process that has the shape npm gives a
CLI (a `node` running a `bin/codex` symlink into `node_modules`) and, as the
neighbour that must not change, one that has the shape a native binary gives
it. The `node` is a copy of this image's `sh` under that name, because what
`pgrep` and `/proc` see is the executable's name and its argument list, and
those are the whole of the recorded shape.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import CONTAINER_ONLY, INSTALL_TIMEOUT_S, REPOSITORY_ROOT

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"
# Long enough to outlive the install run by a wide margin; killed in any case.
LINGER_S = "600"
SETTLE_S = 0.5


def executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


class Machine:
    """A machine for install.sh with a stub uv, a stub package, and whichever CLIs a test starts.

    The uv answers `tool dir --bin` with its bin and writes a stub
    `agentic-hil` there on `tool install`; that stub answers `--version` and
    accepts `agent-install`. Step 5 is what is under test, and it reads the
    process table, not the package.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.home = Path(os.environ["HOME"])
        self.project = tmp_path / "project"
        self.tools = tmp_path / "tools"
        self.uv_bin = tmp_path / "uv-tools" / "bin"
        self.uv_root = tmp_path / "uv-tools" / "tools"
        self.clis = tmp_path / "clis"
        for directory in (self.project, self.tools, self.uv_bin, self.uv_root, self.clis):
            directory.mkdir(parents=True)
        self.registered = tmp_path / "registered"
        executable(
            self.tools / "uv",
            'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
            '  if [ "$3" = "--bin" ]; then echo "$UV_TOOL_BIN_DIR"; else echo "$UV_TOOL_DIR"; fi\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "tool" ] && [ "$2" = "list" ]; then echo "No tools installed"; exit 0; fi\n'
            'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
            '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
            "#!/bin/sh\n"
            'case "\\$1" in\n'
            '  --version) echo "9.9.9" ;;\n'
            f'  agent-install) echo "\\$3" >> "{self.registered}"; echo "registered" ;;\n'
            "esac\n"
            "exit 0\n"
            "STUB\n"
            '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        self.started: list[subprocess.Popen[bytes]] = []

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.tools}{os.pathsep}{self.clis}{os.pathsep}/usr/bin:/bin",
            "UV_TOOL_BIN_DIR": str(self.uv_bin),
            "UV_TOOL_DIR": str(self.uv_root),
            **extra,
        }

    def native_cli(self, name: str) -> subprocess.Popen[bytes]:
        """A running agent CLI that is a native binary: `comm` is its own name."""
        sleep = shutil.which("sleep")
        assert sleep is not None
        shutil.copy2(sleep, self.clis / name)
        return self._start([str(self.clis / name), LINGER_S])

    def npm_cli(self, name: str, package: str) -> subprocess.Popen[bytes]:
        """A running agent CLI installed through npm: `comm` is `node`, argv[1] the launcher symlink.

        The layout is npm's: `<prefix>/lib/node_modules/<package>/bin/<name>.js`
        opening with `#!/usr/bin/env node`, and `<prefix>/bin/<name>` a
        symlink to it. The `node` on PATH is a copy of `sh`, so the script
        body is shell and the process keeps the name and the arguments the
        kernel gave it.
        """
        node_dir = self.clis.parent / "node-runtime"
        node_dir.mkdir(exist_ok=True)
        shell = shutil.which("sh")
        assert shell is not None
        if not (node_dir / "node").exists():
            shutil.copy2(shell, node_dir / "node")
        script = self.clis.parent / "npm-prefix" / "lib" / "node_modules" / package / "bin" / f"{name}.js"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"#!/usr/bin/env node\nsleep {LINGER_S}\n", encoding="utf-8")
        script.chmod(0o755)
        launcher = self.clis / name
        launcher.symlink_to(os.path.relpath(script, self.clis))
        return self._start([str(launcher)], PATH=f"{node_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def _start(self, command: list[str], **extra: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(command, env={**os.environ, **extra}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started.append(process)
        time.sleep(SETTLE_S)
        assert process.poll() is None, f"{command} exited with {process.returncode} before the install ran"
        return process

    def install(self, *arguments: str, **extra: str) -> str:
        result = subprocess.run(
            ["sh", str(SHELL_SCRIPT), "--no-can", *arguments],
            cwd=str(self.project),
            capture_output=True,
            text=True,
            env=self.environment(**extra),
            timeout=INSTALL_TIMEOUT_S,
            check=False,
        )
        transcript = f"{result.stdout}{result.stderr}"
        assert result.returncode == 0, transcript
        return transcript

    def stop(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)


@pytest.fixture
def machine(tmp_path: Path):
    made = Machine(tmp_path)
    try:
        yield made
    finally:
        made.stop()


def comm_of(pid: int) -> str:
    return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()


def cmdline_of(pid: int) -> list[str]:
    return [part for part in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8").split("\0") if part]


def test_step_five_names_a_running_npm_installed_codex(machine: Machine) -> None:
    """The shape npm gives codex, in the real process table, and the block step 5 owes for it."""
    codex = machine.npm_cli("codex", "@openai/codex")
    # The premise, read out of the kernel: this is what a `pgrep -x codex`
    # cannot see.
    assert comm_of(codex.pid) == "node", comm_of(codex.pid)
    arguments = cmdline_of(codex.pid)
    assert arguments[0] == "node" and arguments[1].endswith("/codex"), arguments
    assert subprocess.run(["pgrep", "-x", "codex"], capture_output=True, text=True, check=False).stdout.strip() == ""

    transcript = machine.install("--agent", "codex")

    assert "codex registered" in transcript, transcript
    assert f"RESTART REQUIRED: codex is running right now (PID {codex.pid})." in transcript, transcript
    assert "nothing to restart" not in transcript, transcript


def test_step_five_names_a_running_native_codex(machine: Machine) -> None:
    """The neighbour: a native binary named codex was always found, and still is."""
    codex = machine.native_cli("codex")
    assert comm_of(codex.pid) == "codex"

    transcript = machine.install("--agent", "codex")

    assert f"RESTART REQUIRED: codex is running right now (PID {codex.pid})." in transcript, transcript


def test_step_five_lists_every_running_cli_whichever_way_each_was_installed(machine: Machine) -> None:
    """Two CLIs open, one native and one from npm: the plural block, naming both.

    A block that named one would be read as clearing the other, which is the
    reason the plural branch exists, and it had never run either.
    """
    claude = machine.native_cli("claude")
    codex = machine.npm_cli("codex", "@openai/codex")

    transcript = machine.install()

    assert "claude-code registered" in transcript and "codex registered" in transcript, transcript
    assert "RESTART REQUIRED: these agent CLIs are running right now:" in transcript, transcript
    assert f"    claude (PID {claude.pid})" in transcript, transcript
    assert f"    codex (PID {codex.pid})" in transcript, transcript
    assert "restart: 2 of your agent CLIs are running right now" in transcript, transcript


def test_step_five_inside_a_claude_code_session_names_this_session(machine: Machine) -> None:
    """The neighbour with no process to find: `CLAUDECODE` in the environment speaks for claude alone."""
    (machine.clis / "claude").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (machine.clis / "claude").chmod(0o755)

    transcript = machine.install("--agent", "claude-code", CLAUDECODE="1")

    assert "RESTART REQUIRED: claude is running right now (PID this session)." in transcript, transcript


def test_step_five_with_nothing_running_says_there_is_nothing_to_restart(machine: Machine) -> None:
    """The calm branch, which is only right when the table really holds none of them."""
    (machine.clis / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (machine.clis / "codex").chmod(0o755)

    transcript = machine.install("--agent", "codex")

    assert "restart: no agent CLI of yours is running, so there is nothing to restart" in transcript, transcript
    assert "RESTART REQUIRED" not in transcript, transcript
