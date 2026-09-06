"""The Windows process table as Win32 really answers it, read for a real child.

On Windows `agentic-hil upgrade` names the servers still running out of the
replaced installation under `restart_required_by`, with pid, image and start
time, and sets `restart_required` from what it found. That answer comes out of
raw ctypes calls (`CreateToolhelp32Snapshot`, `OpenProcess`,
`QueryFullProcessImageNameW`, `GetProcessTimes`) whose structure layout (the
`ProcessEntry32W` fields and their widths, the two halves of a `FILETIME`) and
whose access right are the assumptions. A wrong field offset reads the wrong
pid or the wrong parent, a `FILETIME` assembled the wrong way round is a start
time in the wrong century, and a flag the system refuses is an empty table
that reports `restart_required: false` beside a live server: the #459 defect
on the other host.

Every Windows-shaped test in the suite feeds hand-built `ProcessImage` tuples
into the upgrade, so the reader itself was executed by no test anywhere, only
by a bench. Here the operating system is the tool under test: a child this
process starts is read back out of the snapshot, and what the table answers is
held against what this process knows about the child it started.

Windows only, by platform, and against a real child started out of a scratch
environment, never against the machine's own installation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import time
import venv
from pathlib import Path

import pytest

import agentic_hil
from agentic_hil.process import filetime_epoch_seconds, process_working_directory, snapshot_process_images

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the Toolhelp snapshot reader answers only on Windows")

# How far the snapshot's start time may sit from the moment this process
# returned from `Popen`: scheduling and the clock's granularity, not drift.
AGREEMENT_S = 2.0
# How long the child runs before it is read. More than AGREEMENT_S, so a time
# that was really the moment of the lookup cannot pass as the start.
SETTLE_S = 2.0


def a_sleeping_child(interpreter: str) -> subprocess.Popen:
    return subprocess.Popen([interpreter, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def the_snapshot() -> dict:
    entries = snapshot_process_images()
    assert entries is not None, "the Toolhelp snapshot could not be taken on this host"
    return {entry.pid: entry for entry in entries}


def test_a_windows_host_reads_its_own_child_out_of_the_toolhelp_snapshot() -> None:
    """One real child: its pid, its parent, its image, and when it started.

    `parent_pid` is this process, because this process started it; the image
    casefolds to the interpreter it was started as, because that is the file
    Windows mapped; the creation time converts to within AGREEMENT_S of the
    spawn and is at least SETTLE_S minus AGREEMENT_S in the past by the time it
    is read; and it is younger than this process's own entry, which is the
    ordering the parent walk in the upgrade relies on. Windows publishes no
    working directory, no command line and no environment through this
    reader, so those are empty rather than invented.
    """
    child = a_sleeping_child(sys.executable)
    spawned = time.time()
    try:
        time.sleep(SETTLE_S)

        table = the_snapshot()

        assert child.pid in table, f"pid {child.pid} is running and the snapshot does not list it"
        entry = table[child.pid]
        assert entry.parent_pid == os.getpid(), entry
        assert entry.image.casefold() == os.path.realpath(sys.executable).casefold(), entry
        started = filetime_epoch_seconds(entry.created_ns)
        assert started is not None, entry
        assert abs(started - spawned) <= AGREEMENT_S, (started, spawned, entry)
        assert time.time() - started >= SETTLE_S - AGREEMENT_S, (started, entry)
        assert os.getpid() in table, "the snapshot lists every process this user can open, and this one is this user's"
        assert entry.created_ns > table[os.getpid()].created_ns, (entry, table[os.getpid()])
        assert entry.launch_arguments == (), entry
        assert entry.virtual_env == "", entry
        assert process_working_directory(child.pid) is None
    finally:
        child.kill()
        child.wait(timeout=30)


def a_scratch_installation(tmp_path: Path) -> Path:
    """`<tmp>/uv/tools/agentic-hil`, a real virtual environment in the layout uv creates.

    The layout is what `owning_manager` reads as a tool installation, so the
    prefix is an owned one. `symlinks=False` is the Windows default and the
    measured shape: `Scripts\\python.exe` is a launcher that starts the base
    interpreter as its child, so the process holding the environment's file
    open is the launcher, and the interpreter running the code is one level
    below it with its image outside the environment.
    """
    environment = tmp_path / "uv" / "tools" / "agentic-hil"
    venv.create(environment, with_pip=False, symlinks=False)
    assert (environment / "Scripts" / "python.exe").is_file()
    return environment


# Run by the base interpreter one level under the scratch environment's
# launcher, exactly where `agentic-hil upgrade` runs on Windows. It asks the
# same question the upgrade asks, and reports its own place in the table so the
# test can see that the launcher above it was there to be excluded.
ASK_FROM_INSIDE = """
import json, os, sys
from agentic_hil.process import snapshot_process_images
from agentic_hil.upgrade import _dedicated_environment_root, _processes_holding_installation
table = {entry.pid: entry for entry in snapshot_process_images()}
me = table[os.getpid()]
launcher = table.get(me.parent_pid)
print(json.dumps({
    "pid": os.getpid(),
    "prefix": sys.prefix,
    "owned": str(_dedicated_environment_root()),
    "image": me.image,
    "launcher_pid": me.parent_pid,
    "launcher_image": launcher.image if launcher else None,
    "holders": _processes_holding_installation(),
}))
"""


def test_a_windows_upgrade_names_a_server_out_of_its_installation_and_never_itself(tmp_path: Path) -> None:
    """The parent walk on a real table: the launcher above the upgrader is excluded, a sibling is named.

    Two processes are started out of the scratch environment. One sleeps: it
    is the MCP server the agent host left running, and it has to be named
    with its pid, its image inside the environment and its start time. The
    other asks the question, from the base interpreter under the
    environment's launcher, the way `agentic-hil upgrade` runs: it must name
    neither itself nor the launcher it came through, although that launcher's
    image is inside the environment, and must still name the sleeper.
    """
    environment = a_scratch_installation(tmp_path)
    launcher = str(environment / "Scripts" / "python.exe")
    server = a_sleeping_child(launcher)
    try:
        time.sleep(SETTLE_S)
        # The scratch environment has nothing installed. The asking process
        # borrows this interpreter's packages and this checkout's code on
        # PYTHONPATH; its prefix stays the scratch environment, which is the
        # only thing the question reads.
        asked = subprocess.run(
            [launcher, "-c", ASK_FROM_INSIDE],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(agentic_hil.__file__).resolve().parents[1]), sysconfig.get_path("purelib")])},
            check=False,
        )
        assert asked.returncode == 0, asked.stdout + asked.stderr
        answer = json.loads(asked.stdout)

        assert Path(answer["prefix"]) == environment, answer
        assert Path(answer["owned"]) == environment, answer
        # The measured Windows shape, and the reason the walk exists: the code
        # ran outside the environment, under a launcher inside it.
        assert not answer["image"].casefold().startswith(str(environment).casefold()), answer
        assert answer["launcher_image"] is not None, answer
        assert answer["launcher_image"].casefold() == launcher.casefold(), answer

        holders = answer["holders"]
        assert holders is not None, answer
        assert [holder["pid"] for holder in holders] == [server.pid], answer
        assert holders[0]["image"].casefold() == launcher.casefold(), holders[0]
        assert "working_directory" not in holders[0], holders[0]
        assert holders[0].get("started_at"), holders[0]
        assert answer["pid"] not in {holder["pid"] for holder in holders}, answer
        assert answer["launcher_pid"] not in {holder["pid"] for holder in holders}, answer
    finally:
        server.kill()
        server.wait(timeout=30)
