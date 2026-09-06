"""The process table as Linux really publishes it, and the server it has to find.

Two defects lived here, and neither is visible to a fake process table.

The first: a procfs process directory is stamped with the time of the lookup
that created it, not with the time the process started, so a reader that took
the directory's own timestamps reported a server that had been up for hours as
having started at the moment `agentic-hil upgrade` stat'ed it, inside the very
sentence telling the operator whether that server predates the last upgrade.
What answers correctly is `starttime` out of `/proc/<pid>/stat` against `btime`
out of `/proc/stat`, and the only way to know which of the two a reader used is
to ask a second program that has no stake in it. `ps -o lstart` is that program.

The second: a virtual environment's `bin/python` is a symlink to the system
interpreter, so `/proc/<pid>/exe` resolves outside the environment for every
process the environment starts. Measured on a bench with a live, initialised
`agentic-hil mcp-stdio` running out of a uv tool environment, `agentic-hil
upgrade --json` answered `restart_required: false`, named nobody, and said no
restart was needed. Nothing shipped for naming running servers worked on Linux
at all. The test below starts exactly that server and asks exactly that
question.
"""

from __future__ import annotations

import calendar
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from agentic_hil.process import filetime_epoch_seconds, snapshot_process_images

from .conftest import ABOVE_EVERY_RELEASE, CONTAINER_ONLY, UvTool, Wheelhouse, a_line_within, fixture_configuration

# How long the server may take to answer `initialize` before that is a failure
# rather than a slow machine. Read under a bound because a server that starts and
# never answers would otherwise block this readline forever: no timeout plugin is
# configured anywhere in this repository, so the job would run to its ceiling and
# report a timeout instead of a test that says what did not answer.
INITIALIZE_TIMEOUT_S = 60.0

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

# How far apart the two readings of one start time may be. The clocks are the
# same clock, so this is rounding and scheduling, not drift: `ps` prints whole
# seconds and both readers convert through a boot time and a tick count.
AGREEMENT_S = 2.0
# How long the child is left running before its start time is read. It has to be
# comfortably more than AGREEMENT_S, because the defect this catches reports the
# moment of the lookup: a test that read immediately would find the wrong answer
# and the right one within the tolerance of each other.
SETTLE_S = 4.0


def start_time_ps_reports(pid: int) -> float:
    """When `ps` says a process started, in Unix seconds.

    `TZ=UTC` and `LC_ALL=C` in that command's own environment, so the field it
    prints is in a timezone and a language this can parse rather than in
    whatever the host is set to.
    """
    reported = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        timeout=30,
        check=False,
    )
    assert reported.returncode == 0, f"ps could not read pid {pid}: {reported.stderr}"
    printed = reported.stdout.strip()
    assert printed, f"ps printed no start time for pid {pid}"
    return float(calendar.timegm(datetime.strptime(printed, "%a %b %d %H:%M:%S %Y").timetuple()))


def test_a_real_childs_start_time_and_launch_are_read_out_of_procfs(tmp_path: Path) -> None:
    """One real child, read twice: by this code and by `ps`.

    The launch arguments and `VIRTUAL_ENV` are read from the same entry, because
    they are the two things that make a process attributable to an installation
    on a host where the image never is.
    """
    sleep = shutil.which("sleep")
    assert sleep is not None, "this image has no sleep(1), and the child has to be a real process"
    environment = str(tmp_path / "some-environment")
    child = subprocess.Popen([sleep, "30"], env={**os.environ, "VIRTUAL_ENV": environment})
    try:
        time.sleep(SETTLE_S)

        entries = snapshot_process_images()
        assert entries is not None, "this host published no process table"
        mine = [entry for entry in entries if entry.pid == child.pid]
        assert len(mine) == 1, f"pid {child.pid} is running and the snapshot holds {len(mine)} entries for it"
        entry = mine[0]

        started = filetime_epoch_seconds(entry.created_ns)
        assert started is not None, f"no start time was read for pid {child.pid}: created_ns={entry.created_ns}"
        by_ps = start_time_ps_reports(child.pid)
        assert abs(started - by_ps) <= AGREEMENT_S, f"procfs says {started} and ps says {by_ps}, {abs(started - by_ps):.1f}s apart"
        # And the reading is a start time rather than the moment of the lookup:
        # the child has been up for SETTLE_S, so a lookup-stamped value would sit
        # inside the tolerance of now and outside the tolerance of ps.
        assert time.time() - started >= SETTLE_S - AGREEMENT_S, f"pid {child.pid} started {time.time() - started:.1f}s ago after {SETTLE_S}s of running"

        assert entry.launch_arguments == (sleep, "30"), entry
        assert entry.virtual_env == environment, entry
    finally:
        child.kill()
        child.wait(timeout=30)


def test_a_server_started_from_the_installation_is_named_under_restart_required_by(uv_tool: UvTool, wheelhouse: Wheelhouse, tmp_path: Path) -> None:
    """The live MCP server an upgrade does not reach, found and reported.

    It is started the way an agent host starts one: the installation's own
    launcher, in the project directory, with that project's configuration, and
    it is spoken to before the question is put, so what is found is a server that
    has finished starting rather than a process that happens to be there.

    The image the kernel resolves for it is the system interpreter, outside the
    installation entirely, which is why finding it at all is the claim: what
    attributes it is how it was launched.
    """
    uv_tool.install("--find-links", str(wheelhouse.every_version), f"agentic-hil=={ABOVE_EVERY_RELEASE}")
    project = tmp_path / "project"
    project.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")

    server = subprocess.Popen(
        [str(uv_tool.launcher), "mcp-stdio"],
        cwd=str(project),
        env=uv_tool.environment(AGENTIC_HIL_CONFIG=str(config)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "container-tier", "version": "1"}},
        }
        server.stdin.write(json.dumps(request) + "\n")
        server.stdin.flush()
        answered = a_line_within(server.stdout, INITIALIZE_TIMEOUT_S)
        assert answered is not None, f"the server did not answer initialize within {INITIALIZE_TIMEOUT_S:.0f}s and is still running"
        assert answered.strip(), f"the server answered nothing to initialize: {server.stderr.read() if server.poll() is not None else 'it is still running'}"
        assert json.loads(answered)["result"]["protocolVersion"], answered

        entries = snapshot_process_images()
        assert entries is not None, "this host published no process table"
        found = [entry for entry in entries if entry.pid == server.pid]
        assert len(found) == 1, f"the server at pid {server.pid} is not in the snapshot"
        # The fact the Linux reader exists for: the kernel resolves this process
        # to an interpreter outside the installation, so the image cannot be
        # what attributes it.
        assert not str(found[0].image).startswith(str(uv_tool.environment_root)), found[0]
        assert any(str(uv_tool.launcher) == argument or str(uv_tool.environment_root) in argument for argument in found[0].launch_arguments), found[0]

        status, result = uv_tool.upgrade(UV_OFFLINE="1")

        assert status == 0, result
        assert result["restart_required"] is True, result
        holders = {holder["pid"]: holder for holder in result["restart_required_by"]}
        assert server.pid in holders, result["restart_required_by"]
        holder = holders[server.pid]
        assert Path(holder["working_directory"]) == project, holder
        assert holder["started_at"], holder
        assert str(server.pid) in result["restart_notice"], result["restart_notice"]
    finally:
        server.kill()
        server.wait(timeout=30)
