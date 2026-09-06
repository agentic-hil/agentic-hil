"""What the screen says has to be what the document says (#504).

Twelve places where a person at a shell read something other than what the
`--json` document carried: a successful `revoke` printing a stale-server refusal,
`schema` printing a document and then a verdict after it, an MCP tool name in a
nested refusal that the top-level rendering already spells as a command, a probe
listing dropping the ports it read the ids from, `Refused:` over an upgrade that
changed the disk, `lease-status` opening with `OK.`, MCP advice about
`inputSchema` under a refusal typed at a shell, `adopt-hardware` telling an
operator with no toolchain to attach a board, a plan that does not parse being
advised about device names, and three things nothing drove at all: the exit code
of half the commands, the argparse wiring of several of them, and the uninstall
renderer.

Every test here is written from the issue text, against the fixtures the issue
names, and pins one of those places as a person sees it. The `--json` documents
are pinned beside them as the neighbours that must not move, with the single
exception the issue itself asks for: an `ok: true` grant no longer carrying
`config_status.error_type`.

Tier: unit-fake. No tool and no hardware: the fakes and the generated
configuration are what the rest of this suite already uses.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import re
import subprocess
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD, write_authoritative_config
from support import trusted_launcher

from agentic_hil import __version__, cli, upgrade
from agentic_hil.comports import usb_stlink_ports
from agentic_hil.config import config_schema_text, load_authoritative_config
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.humanize import render_result
from agentic_hil.knowledge import recovery_operator_command, remediation_fields

FLASH = "debuggers.dut.permissions.allow_flash"
MASS_ERASE = "debuggers.dut.permissions.allow_mass_erase"

# The plan tests/test_test_reactor.py already refuses for its duplicate key: the
# loader names line 3, column 1, which is where the second `name:` stands.
DUPLICATE_KEY_PLAN = "version: 3\nname: one\nname: two\nsteps:\n  - {device: dut, action: reset}\n"

# What `pip install --dry-run --report -` answers when a release is available,
# and the two ends of the version probe `_failed_upgrade` runs afterwards.
PIP_WOULD_INSTALL_A_RELEASE = subprocess.CompletedProcess[str](
    [], 0, json.dumps({"version": "1", "install": [{"metadata": {"name": "agentic-hil", "version": "9.9.9"}}]}), ""
)
MANAGER_FAILED = subprocess.CompletedProcess[str]([], 1, "", "post-install step failed")


def _shell(argv: list[str]) -> tuple[int, str, str]:
    """Run the command line the way a shell does and hand back what it wrote."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.entrypoint(argv)
    return code, out.getvalue(), err.getvalue()


def _reflowed(text: str) -> str:
    return " ".join(text.split())


def _section_text(out: str, title: str) -> str:
    """The body of one section of a rendering, reflowed: from its title line to
    the next line that stands at the margin, which is the next title."""
    lines = out.splitlines()
    start = lines.index(title)
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return _reflowed("\n".join(body))


def _standing_quarantine(config: object) -> None:
    """A quarantine whose evidence chain is damaged: the one incident `recover`
    still has work to do on. The project record and the resource marker are
    written the way tests/test_coordination.py writes them, with `audit_ok`
    false on the project's, which is what keeps the incident standing."""
    setup = HardwareCoordinator(config, "setup")  # type: ignore[arg-type]
    try:
        incident = {"quarantine_id": "q-experiment", "reason": "owner_process_exited_without_release"}
        setup._write_record("physical:dut", {**setup._base_record("quarantined", ["physical:dut"]), **incident})
        setup._write_record(setup.project_key, {**setup._base_record("cleanup_required", ["physical:dut"]), **incident, "audit_ok": False})
    finally:
        setup.close()


def _generated_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace `agentic-hil init` configured, with the finders the autouse fixture hides."""
    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    code, out, _ = _shell(["init", "--json"])
    assert code == 0, out
    return workspace


def _agent_host(monkeypatch: pytest.MonkeyPatch) -> str:
    """A launcher the registration trust rule accepts, as tests/test_agentic_hil.py sets one up."""
    command = str(trusted_launcher())
    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", lambda: command)
    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", list)
    return command


# ---------------------------------------------------------------------------
# A successful grant or revoke is not a stale server.


def test_a_successful_grant_at_a_shell_does_not_render_a_stale_server_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`config_status` on the grant result is computed against the configuration
    loaded before the write, so it is `changed` after every successful write, and
    the generic renderer printed its `config_stale` block, forty lines of advice
    about restarting an MCP server, under a command that has no server."""
    _generated_bench(tmp_path, monkeypatch)

    code, out, _ = _shell(["revoke", FLASH])

    assert code == 0, out
    assert "config_stale" not in out
    assert "Restart the MCP server to bind it" not in out
    # The neighbours: the change and the restart notice the result really carries.
    assert FLASH in out
    assert "restart_required" in out

    code, out, _ = _shell(["grant", FLASH, "--json"])

    document = json.loads(out)
    assert code == 0
    assert document["ok"] is True
    assert document["restart_required"] is True
    assert document["changed"] == [{"key": FLASH, "previous_value": False, "value": True}]
    assert "error_type" not in document["config_status"]


# ---------------------------------------------------------------------------
# `schema`, `test-schema` and `mcp-config` print one document and nothing after it.


def _bundled_test_schema() -> dict:
    return json.loads(resources.files("agentic_hil").joinpath("schemas", "testconfig.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("schema", lambda: json.loads(config_schema_text())),
        ("test-schema", _bundled_test_schema),
        ("mcp-config", lambda: json.loads(cli.mcp_config_text())),
    ],
)
@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["rendered", "json"])
def test_schema_printed_to_stdout_is_exactly_the_schema(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: Callable[[], dict], json_flag: list[str]
) -> None:
    """`agentic-hil schema > agentic-hil.schema.json` has to write a file that loads.

    Without `--output` the command writes the document to stdout and returns
    `{"ok": true}`, which the entrypoint then prints after it as `OK.` or as a
    second JSON document, so the redirect captured two things and `json.loads`
    failed on both spellings while the exit code was 0."""
    _agent_host(monkeypatch)

    code, out, _ = _shell([command, *json_flag])

    assert code == 0
    document, end = json.JSONDecoder().raw_decode(out)
    trailing = out[end:].strip()
    assert trailing == "", f"{command} printed more than the document: {trailing!r}"
    assert document == expected()


@pytest.mark.parametrize("command", ["schema", "test-schema", "mcp-config"])
def test_schema_written_to_a_file_still_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    """The neighbour: with `--output` the result is the only thing on stdout, as before."""
    _agent_host(monkeypatch)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / f"{command}.json"

    code, out, _ = _shell([command, "--output", target.name])

    assert code == 0, out
    assert "written" in out
    assert not out.lstrip().startswith("{")
    assert json.loads(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A refusal nested in a report names the command the reader can type.


def _nested_adapter_refusal() -> dict:
    """The check `doctor` embeds when OpenOCD could not open an adapter, with the
    catalogue's own entry merged in, as `ConfigError.to_dict` merges it."""
    return {
        "ok": False,
        "tool": "debugger_info",
        "error_type": "adapter_not_found",
        "backend": "openocd",
        "summary": "OpenOCD could not open a debug adapter.",
        **remediation_fields("adapter_not_found", "openocd"),
    }


def _doctor_with_a_refused_check() -> dict:
    return {
        "ok": False,
        "tool": "agentic_hil_doctor",
        "summary": "Agentic HIL configuration loaded, but the debugger check failed for: dut.",
        "config_status": {"state": "unchanged", "path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml", "loaded_digest": "sha256:44be3c", "reload_required": False},
        "config_path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
        "installation": {"version": __version__, "package_path": "/opt/agentic_hil", "editable": False},
        "mcp": {"transport": "stdio", "args": ["mcp-stdio"], "command": "/home/op/.local/bin/agentic-hil", "persistent": True},
        "target": {"name": "nucleo_f446re", "controller": "stm32f446re"},
        "debuggers": {
            "dut": {
                "type": "openocd",
                "probe_id": None,
                "bound": True,
                "permissions": {"allow_flash": True, "allow_mass_erase": False},
                "check": _nested_adapter_refusal(),
            }
        },
        "com_ports": {},
        "can_buses": {},
    }


def _init_with_a_refused_step() -> dict:
    return {
        "ok": False,
        "tool": "agentic_hil_init",
        "scope": "project",
        "summary": "Agentic HIL project setup failed; its own committed file changes were rolled back.",
        "agent": "codex",
        "config_path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
        "state_root_changes": [],
        "permission_changes": [],
        "rollback": {"attempted": True, "ok": True, "errors": []},
        "steps": {
            "config": {"ok": False, "summary": "The probe could not be opened.", "error_type": "adapter_not_found", "backend": "openocd", **remediation_fields("adapter_not_found", "openocd")},
            "doctor": {"ok": False, "skipped": True, "summary": "Doctor was not reached."},
        },
    }


def _setup_with_a_refused_step() -> dict:
    document = _init_with_a_refused_step()
    return {
        **document,
        "tool": "agentic_hil_setup",
        "scopes": {
            "user": {"ok": True, "scope": "user", "summary": "Installed for this user account.", "command": "/home/op/.local/bin/agentic-hil"},
            "project": {"ok": False, "scope": "project", "summary": "The project half was refused.", "config_path": document["config_path"]},
        },
    }


def _probe_listing_with_a_refused_debugger() -> dict:
    return {
        "ok": False,
        "tool": "debugger_probes_list",
        "summary": "Probe discovery failed for: dut.",
        "debuggers": {"dut": {**_nested_adapter_refusal(), "probes": []}},
        "configured_debuggers": ["dut"],
    }


NESTED_REFUSALS = [
    ("doctor", _doctor_with_a_refused_check),
    ("init", _init_with_a_refused_step),
    ("setup", _setup_with_a_refused_step),
    ("debugger-probes", _probe_listing_with_a_refused_debugger),
]


@pytest.mark.parametrize(("command", "document"), NESTED_REFUSALS, ids=[command for command, _ in NESTED_REFUSALS])
def test_a_refusal_nested_in_doctor_names_the_command_the_reader_can_type(command: str, document: Callable[[], dict]) -> None:
    """#451 spelled `debugger_probes_list` as `agentic-hil debugger-probes` for the
    reader at a shell, and only at the top level: the same refusal one level in,
    under a doctor check or an init step, still sent them to a name their shell
    does not have."""
    out = _reflowed(render_result(document(), command))

    assert "agentic-hil debugger-probes" in out
    assert "debugger_probes_list" not in out


@pytest.mark.parametrize(("command", "document"), NESTED_REFUSALS, ids=[command for command, _ in NESTED_REFUSALS])
def test_a_nested_refusal_rendered_for_nobody_keeps_the_tools_name(command: str, document: Callable[[], dict]) -> None:
    """The neighbour: `command` is the fact that a person is reading. Without it the
    step is printed as the catalogue wrote it, which is the agent's vocabulary."""
    del command
    out = _reflowed(render_result(document()))

    assert "debugger_probes_list" in out


# ---------------------------------------------------------------------------
# A probe listing shows the ports it read the ids from, and the tools it searched.


def _stlink_port_that_published_no_serial() -> dict:
    """An ST-Link VCP as pyserial lists it on Ubuntu 24.04, with no serial in the
    descriptor: the shape `tests/test_bootstrap.py`'s NUCLEO_VCP records, less the
    serial. Passed through `usb_stlink_ports` so the fixture is that function's own
    output rather than a copy of it. A bench recording of a serial-less VCP is
    still wanted for the inventory half; the renderer needs only the result shape."""
    port = {
        "device": "/dev/ttyACM0",
        "name": "ttyACM0",
        "description": "STM32 STLink - ST-Link VCP Ctrl",
        "hwid": "USB VID:PID=0483:374B LOCATION=1-2:1.2",
        "manufacturer": "STMicroelectronics",
        "product": "STM32 STLink",
        "interface": "ST-Link VCP Ctrl",
        "vid": 0x0483,
        "pid": 0x374B,
    }
    noise = [{"device": f"/dev/ttyS{index}", "name": f"ttyS{index}"} for index in range(4)]
    ports = usb_stlink_ports({"ok": True, "tool": "com_ports_available", "ports": [*noise, port]})
    assert ports == [port]
    return ports[0]


def test_a_probe_listing_prints_the_stlink_ports_it_read_the_ids_from() -> None:
    """#432 added `stlink_ports` so an empty `probes` beside a visible ST-Link is
    read as a probe that is there and cannot be named, not as no probe attached.
    The rendering printed `0 connected debugger probe(s)` and never named the port."""
    listing = {
        "ok": True,
        "tool": "debugger_probes_list",
        "backend": "openocd",
        "discovered_by": "usb_serial_inventory",
        "probes": [],
        "stlink_ports": [_stlink_port_that_published_no_serial()],
        "complete": False,
        "interface_cfg": "interface/stlink.cfg",
        "summary": "0 connected debugger probe(s) read from this host's USB serial inventory.",
    }

    out = render_result(listing, "debugger-probes")

    assert "/dev/ttyACM0" in out, out
    # And the fact about that port a reader needs: the serial it did not publish.
    port_section = out[out.index("/dev/ttyACM0") :].split("\n\n")[0]
    assert re.search(r"serial", port_section, re.IGNORECASE), out
    # The neighbours #445 and #432 put on screen are still there.
    assert "discovered_by" in out and "usb_serial_inventory" in out
    assert "complete" in out


def test_a_bootstrap_probe_listing_prints_the_tools_it_searched() -> None:
    """The no-config listing carries `tools_searched`: which toolchains discovery
    looked for and where each landed. It is the half a reader cannot reconstruct
    from `backend openocd`, and the rendering dropped it."""
    listing = {
        "ok": True,
        "tool": "debugger_probes_list",
        "source": "bootstrap",
        "backend": "openocd",
        "discovered_by": "usb_serial_inventory",
        "executable": "/usr/bin/openocd",
        "tools_searched": [
            {"name": "STM32_Programmer_CLI", "provided_by": "STM32CubeProgrammer", "path": None, "found": False},
            {"name": "openocd", "provided_by": "OpenOCD", "path": "/usr/bin/openocd", "found": True},
        ],
        "probes": [{"probe_id": "066AFF303435554157113106"}],
        "stlink_ports": [],
        "complete": False,
        "summary": "1 connected debugger probe(s) read from this host's USB serial inventory.",
    }

    out = _reflowed(render_result(listing, "debugger-probes"))

    assert "STM32_Programmer_CLI" in out
    assert "OpenOCD" in out
    assert "found" in out
    assert "066AFF303435554157113106" in out


# ---------------------------------------------------------------------------
# An upgrade that changed the disk is headed by the run's own outcome word.


def _manager_that_failed(monkeypatch: pytest.MonkeyPatch, *, version_after: subprocess.CompletedProcess[str]) -> None:
    """The recording manager tests/test_agentic_hil.py drives `_failed_upgrade`
    through: the resolution query says a release exists, the install fails, and
    the version probe afterwards answers whatever the case is about."""
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ("can",))
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")
    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: ("pip", [sys.executable, "-m", "pip", "install", "--upgrade", "agentic-hil"]))
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", list)

    def run(invoked: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        del cwd
        if "--dry-run" in invoked:
            return PIP_WOULD_INSTALL_A_RELEASE
        if invoked[-1] == "--version":
            return version_after
        return MANAGER_FAILED

    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", run)


def test_an_upgrade_that_changed_the_disk_is_headed_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """#447 reserved `Refused:` for a result where nothing ran. A manager that
    replaced files before it stopped ran, and the summary says the installation is
    half-changed; heading that `Refused:` puts the word for "your setup is wrong"
    over a disk that just changed."""
    _manager_that_failed(monkeypatch, version_after=subprocess.CompletedProcess([], 0, "9.9.9\n", ""))

    result = cli.upgrade_installation(["opencode"])
    out = render_result(result, "upgrade")

    assert result["error_type"] == "installation_changed_after_failed_upgrade"
    assert result["changed_on_disk"] is True
    assert out.startswith("Failed: installation_changed_after_failed_upgrade"), out.splitlines()[0]
    assert "half-changed" in _reflowed(out)


def test_an_upgrade_that_broke_the_installation_is_headed_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _manager_that_failed(monkeypatch, version_after=subprocess.CompletedProcess([], 1, "", "No module named agentic_hil"))

    result = cli.upgrade_installation(["opencode"])
    out = render_result(result, "upgrade")

    assert result["error_type"] == "installation_broken"
    assert out.startswith("Failed: installation_broken"), out.splitlines()[0]


def test_an_upgrade_that_left_the_installation_intact_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour tests/test_human_readable_cli.py pins from a fixture, here
    from the producer: nothing on disk moved, so nothing ran that a person has to
    read about, and the word stays `Refused:`."""
    _manager_that_failed(monkeypatch, version_after=subprocess.CompletedProcess([], 0, f"{__version__}\n", ""))

    result = cli.upgrade_installation(["opencode"])
    out = render_result(result, "upgrade")

    assert result["error_type"] == "upgrade_failed"
    assert result["installation_intact"] is True
    assert out.startswith("Refused: upgrade_failed"), out.splitlines()[0]


# ---------------------------------------------------------------------------
# `lease-status` opens with a sentence about the bench.


def test_lease_status_opens_with_a_sentence_about_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`status()` writes no `summary` and no `next_step`, so the generic renderer
    opened a clean bench with `OK.` and a quarantined one with the containment
    block and no sentence at all. The fixture that pins the generic renderer
    invents both fields, which is why nothing saw it."""
    workspace = tmp_path / "firmware"
    write_authoritative_config(workspace, monkeypatch)
    monkeypatch.chdir(workspace)
    config = load_authoritative_config(workspace)

    reader = HardwareCoordinator(config, "operator-cli")
    try:
        clean = reader.status()
    finally:
        reader.close()
    out = render_result(clean, "lease-status")
    opening = out.splitlines()[0]

    assert clean.get("cleanup_required") is not True
    assert opening != "OK.", out
    assert "bench" in opening.lower(), out
    # The facts a script reads are still on screen under it.
    assert "bench_held" in out and "incident_stands" in out

    _standing_quarantine(config)
    reader = HardwareCoordinator(config, "operator-cli")
    try:
        quarantined = reader.status()
    finally:
        reader.close()
    out = render_result(quarantined, "lease-status")
    opening = out.splitlines()[0]

    assert quarantined["cleanup_required"] is True
    assert quarantined["incident_stands"] is True
    assert quarantined["quarantine_id"] == "q-experiment"
    assert "did not come back clean" not in opening, out
    assert "quarantin" in opening.lower(), out
    assert recovery_operator_command("q-experiment") in _reflowed(out), out
    # The exit code over a standing quarantine, pinned where it was not. The
    # issue left the verdict open: today's 1 is what #445's rule (a read that
    # answered exits 0) argues against, and the rendering's own containment block
    # promises "the exit code says so", which is what a `set -e` gate on a bench
    # that owes a signature reads. This pins today's verdict as the test's own
    # reading (`complete: false` is a fact about an enumeration, a quarantine is
    # a fact about this bench) until the owner decides; if the decision goes to
    # 0, this assertion flips and the "exit code says so" sentence goes with it.
    code, out, _ = _shell(["lease-status"])
    assert code == 1
    assert "q-experiment" in out


# ---------------------------------------------------------------------------
# A refusal typed at a shell does not send the reader to tools/list.


@pytest.mark.parametrize(
    "argv",
    [["revoke", "nonsense.key"], ["test-reactor-status", "--run", "nonsense"]],
    ids=["revoke", "test-reactor-status"],
)
def test_a_shell_refusal_on_its_arguments_does_not_send_the_reader_to_tools_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """The `invalid_argument` entry explains a tool payload: `field` and
    `validator`, `inputSchema` in `tools/list`, `wait_s: true`. A refusal built
    from a shell argument carries no `validator`, and the one line a shell reader
    can act on came after four steps and three bullets about a surface they are
    not on."""
    _generated_bench(tmp_path, monkeypatch)

    code, out, _ = _shell(argv)
    flat = _reflowed(out)

    assert code == 1
    assert out.startswith("Refused: invalid_argument"), out.splitlines()[0]
    assert "inputSchema" not in flat, out
    assert "`validator`" not in flat, out
    if argv[0] == "revoke":
        # The refusal's own next step stands before any catalogue step, whatever
        # the catalogue steps say: no numbered step precedes it other than the
        # number that is its own.
        named = flat.find("Name one of `permission_keys_here`")
        assert named != -1, out
        earlier_steps = re.findall(r"(?<!\S)(\d+)\. ", flat[:named])
        assert earlier_steps in ([], ["1"]), out


@pytest.mark.parametrize(
    ("argv", "field"),
    [(["revoke", "nonsense.key"], "rejected_keys"), (["test-reactor-status", "--run", "nonsense"], "field")],
    ids=["revoke", "test-reactor-status"],
)
def test_a_shell_refusal_on_its_arguments_is_the_same_document_under_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], field: str) -> None:
    """The neighbour: the machine document keeps every field it had."""
    _generated_bench(tmp_path, monkeypatch)

    code, out, _ = _shell([*argv, "--json"])
    document = json.loads(out)

    assert code == 1
    assert document["error_type"] == "invalid_argument"
    assert field in document


# ---------------------------------------------------------------------------
# `adopt-hardware` refused for a missing toolchain names the toolchain.


def test_adopt_hardware_refused_for_a_missing_toolchain_does_not_say_attach_the_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#416 matched `init`'s remedy to why discovery failed; `adopt-hardware`
    kept one sentence for every failed discovery, and with neither toolchain on
    the host it sends the operator to reseat a board that may be plugged in."""
    _generated_bench(tmp_path, monkeypatch)

    result = cli.adopt_hardware(dry_run=True)

    assert result["ok"] is False
    assert result["error_type"] == "debugger_not_found"
    assert "Attach the board" not in result["next_step"], result["next_step"]
    assert re.search(r"OpenOCD|STM32CubeProgrammer", result["next_step"]), result["next_step"]
    # The neighbours: discovery's own reason is still the summary, and the
    # promise that nothing was written stands.
    assert "Neither STM32CubeProgrammer" in result["summary"]
    assert "Nothing was written" in result["summary"]

    code, out, _ = _shell(["adopt-hardware", "--dry-run"])

    assert code == 1
    assert out.startswith("Refused: debugger_not_found"), out.splitlines()[0]
    assert "Attach the board" not in out


# ---------------------------------------------------------------------------
# A plan that does not parse is not advised about device names.


def test_a_plan_that_does_not_parse_is_not_advised_about_device_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#448's class: the unscoped `test_config_invalid` entry is about a document
    the loader has read, and a parse error is the one refusal where it has not.
    The Details already locate the fault at line 3, column 1; the advice sent
    the reader to `adopt-hardware` and `init --force`."""
    workspace = _generated_bench(tmp_path, monkeypatch)
    (workspace / "dupe.testconfig.yaml").write_text(DUPLICATE_KEY_PLAN, encoding="utf-8")

    code, out, _ = _shell(["test-reactor", "--test-config", "dupe.testconfig.yaml"])
    flat = _reflowed(out)

    assert code == 1
    assert out.startswith("Refused: test_config_invalid"), out.splitlines()[0]
    assert "adopt-hardware" not in flat, out
    assert "init --force" not in flat, out
    # The `line` and `column` rows themselves, not the loader's sentence in the
    # `backend_error` row, which spells the same position.
    details = _section_text(out, "Details").replace("line 3, column 1", "")
    assert re.search(r"(?<!\S)line\s+3(?!\S)", details), out
    assert re.search(r"(?<!\S)column\s+1(?!\S)", details), out

    code, out, _ = _shell(["test-reactor", "--test-config", "dupe.testconfig.yaml", "--json"])
    document = json.loads(out)

    assert code == 1
    assert document["error_type"] == "test_config_invalid"
    assert document["line"] == 3
    assert document["column"] == 1


# ---------------------------------------------------------------------------
# Every command exits with its verdict.


def _revoked_flash(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    del monkeypatch, workspace
    code, out, _ = _shell(["revoke", FLASH, "--json"])
    assert code == 0, out


def _no_host_serial_ports(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    del workspace
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", list)


def _registered_agent_host(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    del workspace
    _agent_host(monkeypatch)
    import shutil

    real_which = shutil.which
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: None if name == "claude" else real_which(name))


def _bound_bench(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """A configuration whose debugger names a toolchain that answers: the fake
    OpenOCD the rest of the suite drives, so `doctor` has a check to pass."""
    write_authoritative_config(workspace, monkeypatch, debugger_executable=FAKE_OPENOCD, probe_id="066AFF303435554157113106")


def _quarantined_bench(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """A standing incident for `recover` to sign for. The `--json` pass of the
    same command afterwards finds nothing standing, which is the other 0."""
    del monkeypatch
    _standing_quarantine(load_authoritative_config(workspace))


EXIT_CODES: list[tuple[str, list[str], int, Callable[[pytest.MonkeyPatch, Path], None] | None]] = [
    ("revoke of a name that is not a permission", ["revoke", "nonsense.key"], 1, None),
    ("revoke", ["revoke", FLASH], 0, None),
    ("grant", ["grant", FLASH], 0, _revoked_flash),
    ("grant that changes nothing", ["grant", FLASH], 0, None),
    ("grant of a closed permission", ["grant", MASS_ERASE], 0, None),
    ("test-reactor-status over an unknown handle", ["test-reactor-status", "--run", "nonsense"], 1, None),
    ("test-reactor-status over a handle never issued", ["test-reactor-status", "--run", "run-00000000000000ff"], 1, None),
    ("test-reactor-stop over an unknown handle", ["test-reactor-stop", "--run", "nonsense"], 1, None),
    ("test-reactor-status listing", ["test-reactor-status"], 0, None),
    ("lease-status over a clean bench", ["lease-status"], 0, None),
    ("recover with nothing standing", ["recover", "--confirm-safe-state", "--quarantine-id", "q-none"], 0, None),
    ("recover over a standing incident", ["recover", "--confirm-safe-state", "--quarantine-id", "q-experiment"], 0, _quarantined_bench),
    ("com-ports", ["com-ports"], 0, _no_host_serial_ports),
    ("config-reload", ["config-reload"], 0, None),
    ("adopt-hardware refused for a missing toolchain", ["adopt-hardware", "--dry-run"], 1, None),
    ("doctor over a bound bench", ["doctor"], 0, _bound_bench),
    ("skill-install", ["skill-install", "--agent", "codex"], 0, _registered_agent_host),
    ("skill-install of an agent this program does not know", ["skill-install", "--agent", "nonsense"], 1, None),
    ("setup", ["setup", "--agent", "claude-code"], 0, _registered_agent_host),
    ("setup of an agent this program does not know", ["setup", "--agent", "nonsense"], 1, None),
    ("agent-install of an agent this program does not know", ["agent-install", "--agent", "nonsense"], 1, None),
    ("uninstall of an agent this program does not know", ["uninstall", "--agent", "nonsense"], 1, None),
    ("upgrade of an agent this program does not know", ["upgrade", "--agent", "nonsense"], 1, None),
]


@pytest.mark.parametrize(("argv", "expected", "prepare"), [case[1:] for case in EXIT_CODES], ids=[case[0] for case in EXIT_CODES])
def test_every_command_exits_with_its_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: int,
    prepare: Callable[[pytest.MonkeyPatch, Path], None] | None,
) -> None:
    """The matrix the CHANGELOG has had to correct twice (#445, #450), pinned at
    the entrypoint for the commands nothing drove there: one success and one
    refusal per command, over the generated configuration, and the hardware-bound
    commands through the refusal path that needs no tool."""
    workspace = _generated_bench(tmp_path, monkeypatch)
    if prepare is not None:
        prepare(monkeypatch, workspace)

    code, out, err = _shell(argv)

    assert code == expected, f"{' '.join(argv)} exited {code}, not {expected}:\n{out}\n{err}"
    # And the same verdict for the caller that parses.
    json_code, out, _ = _shell([*argv, "--json"])
    assert json_code == expected, f"{' '.join(argv)} --json exited {json_code}, not {expected}:\n{out}"
    assert json.loads(out)


def test_no_subcommand_is_a_usage_error_on_stderr() -> None:
    code, out, err = _shell([])

    assert code == 2
    assert out == ""
    assert err.startswith("usage: agentic-hil")


def test_the_version_flag_prints_the_version_and_stops() -> None:
    with pytest.raises(SystemExit) as stop:
        _shell(["--version"])
    assert stop.value.code == 0


# ---------------------------------------------------------------------------
# Every command reaches its handler with the options argparse parsed.


CONFIG = object()

WIRING: list[tuple[list[str], str, dict[str, object]]] = [
    (
        ["adopt-hardware", "--debugger", "dut", "--com-port", "uart", "--probe-id", "0667", "--dry-run"],
        "adopt_hardware",
        {"debugger_id": "dut", "com_port_id": "uart", "probe_id": "0667", "dry_run": True},
    ),
    (["grant", "a.b.c", "d.e.f"], "change_permission", {"command": "grant", "keys": ["a.b.c", "d.e.f"]}),
    (["revoke", "a.b.c"], "change_permission", {"command": "revoke", "keys": ["a.b.c"]}),
    (["com-ports"], "list_available_com_ports", {}),
    (["schema", "--output", "s.json", "--force"], "schema", {"output": "s.json", "force": True}),
    (["test-schema", "--output", "t.json", "--force"], "test_schema", {"output": "t.json", "force": True}),
    (["mcp-config", "--output", "m.json", "--force"], "mcp_config", {"output": "m.json", "force": True}),
    (["skill-install", "--agent", "codex", "--target", "T", "--force"], "install_skill", {"agent": "codex", "target": "T", "force": True}),
    (["setup", "--agent", "codex", "--force"], "setup_project", {"agent": "codex", "force": True}),
    (["agent-install", "--agent", "codex", "--force"], "install_agent", {"agent": "codex", "force": True}),
    (["init", "--agent", "codex", "--force"], "init_project", {"config_path": None, "agent": "codex", "force": True}),
    (["doctor"], "doctor", {"config_path": None}),
    (["config-reload"], "config_reload", {"config_path": None}),
    (["debugger-probes"], "debugger_probes", {}),
    (["upgrade", "--agent", "codex", "--agent", "claude-code"], "upgrade_installation", {"agents": ["codex", "claude-code"]}),
    (["uninstall", "--agent", "codex"], "uninstall_agent_integration", {"agents": ["codex"]}),
    (["test-reactor-status", "--run", "run-0123456789abcdef"], "run_status", {"config": CONFIG, "handle": "run-0123456789abcdef"}),
    (["test-reactor-status"], "run_status", {"config": CONFIG, "handle": None}),
    (["test-reactor-stop", "--run", "run-0123456789abcdef"], "request_run_stop", {"config": CONFIG, "handle": "run-0123456789abcdef"}),
]


@pytest.mark.parametrize(("argv", "handler", "expected"), WIRING, ids=[" ".join(case[0]) for case in WIRING])
def test_every_command_reaches_its_handler_with_the_parsed_options(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], handler: str, expected: dict[str, object]
) -> None:
    """A renamed `dest` raises AttributeError at run time and no test noticed,
    because the dispatch lines for these commands were never driven. The handler
    is replaced by a recorder; what reaches it is bound against the real
    handler's signature so the check is by parameter name."""
    original = getattr(cli, handler)
    signature = inspect.signature(original)
    seen: list[dict[str, object]] = []

    def recorded(*args: object, **kwargs: object) -> dict:
        seen.append(dict(signature.bind(*args, **kwargs).arguments))
        return {"ok": True, "summary": "recorded"}

    monkeypatch.setattr(cli, handler, recorded)
    monkeypatch.setattr(cli, "load_cli_authoritative_config", lambda path: CONFIG)

    code, out, _ = _shell([*argv, "--json"])

    assert code == 0, out
    assert seen == [expected]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["recover", "--confirm-safe-state", "--quarantine-id", "q-1", "--accept-config-change"],
            [("HardwareCoordinator", {"config": CONFIG, "frontend": "operator-cli"}), ("recover", {"safe_state_confirmed": True, "quarantine_id": "q-1", "accept_config_change": True})],
        ),
        (["lease-status"], [("HardwareCoordinator", {"config": CONFIG, "frontend": "operator-cli"})]),
    ],
    ids=["recover", "lease-status"],
)
def test_the_coordination_commands_reach_the_coordinator_with_the_parsed_options(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: list[tuple[str, dict[str, object]]]
) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    class RecordingCoordinator:
        def __init__(self, config: object, frontend: str) -> None:
            seen.append(("HardwareCoordinator", {"config": config, "frontend": frontend}))

        def status(self) -> dict:
            return {"ok": True, "tool": "hardware_lease_status", "summary": "recorded", "incident_stands": True}

        def recover(self, **kwargs: object) -> dict:
            seen.append(("recover", dict(kwargs)))
            return {"ok": True, "summary": "recorded"}

    monkeypatch.setattr(cli, "HardwareCoordinator", RecordingCoordinator)
    monkeypatch.setattr(cli, "load_cli_authoritative_config", lambda path: CONFIG)

    code, out, _ = _shell([*argv, "--json"])

    assert code == 0, out
    assert seen == expected


# ---------------------------------------------------------------------------
# `agentic-hil uninstall` renders every section.


def test_uninstall_at_a_terminal_renders_what_it_removed_and_what_it_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`render_uninstall` never ran under any test. The real command, over an
    installation `agent-install` wrote in the isolated home, rendered for a person."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _agent_host(monkeypatch)
    assert cli.install_agent(agent="claude-code")["ok"] is True
    skill = Path.home() / ".claude" / "skills" / "agentic-hil" / "SKILL.md"
    assert skill.is_file()

    code, out, _ = _shell(["uninstall", "--agent", "claude-code"])
    flat = _reflowed(out)

    assert code == 0, out
    assert '"ok"' not in out and "{" not in out
    assert "Removed" in out
    assert str(skill) in flat
    assert "Kept on purpose" in out
    assert "The package itself" in out
    assert upgrade.removal_command() in flat
    assert not skill.exists()


# The result shape `uninstall_agent_integration` emits, field for field as
# `_removed_entry`, `_left_entry`, `_failed_entry`, `_uninstall_one_agent` and
# `_uninstall_kept_trees` build it, for the two sections a clean run in the
# isolated home cannot produce: a foreign registration left alone and an agent
# step that could not be finished. A recording of a real `uninstall --json`
# from the bench is still wanted in place of this.
UNINSTALL_PARTLY = {
    "ok": False,
    "tool": "agentic_hil_uninstall",
    "scope": "user",
    "summary": (
        "Agentic HIL's user-wide half was taken back for Claude Code and opencode: 2 item(s) removed. 1 item(s) this "
        "installation did not write were left where they were; `left_alone` names each one. 1 item(s) this installation "
        "wrote could not be removed and are still on disk; `failed` names each one and why. It could not be finished for "
        "opencode; that agent's step says why. The package is still installed and this command cannot remove it: run "
        "`uv tool uninstall agentic-hil`."
    ),
    "agents": [
        {
            "agent": "claude-code",
            "ok": True,
            "summary": "Agentic HIL's half for Claude Code was taken back.",
            "removed": [{"what": "skill registration", "path": "/home/op/.claude/skills/agentic-hil/SKILL.md"}],
            "left_alone": [],
            "steps": {
                "mcp_config": {"ok": True, "summary": "Removed.", "removed": [{"what": "MCP registration", "path": "/home/op/.claude.json :: mcpServers.agentic-hil"}], "left_alone": []},
                "skill": {"ok": True, "summary": "Removed.", "removed": [{"what": "skill registration", "path": "/home/op/.claude/skills/agentic-hil/SKILL.md"}], "left_alone": []},
                "agent_write_restriction": {"ok": True, "summary": "Nothing to take back.", "removed": [], "left_alone": []},
            },
        },
        {
            "agent": "opencode",
            "ok": False,
            "summary": "Agentic HIL's half for opencode was not fully taken back; `steps` says which part and why.",
            "removed": [],
            "left_alone": [{"what": "MCP registration", "path": "/home/op/.config/opencode/opencode.json :: mcp.agentic-hil", "reason": "Agentic HIL did not write this, so it stays."}],
            "failed": [{"what": "skill registration", "path": "/home/op/.config/opencode/skills/agentic-hil/SKILL.md", "error": "[Errno 13] Permission denied"}],
            "steps": {
                "mcp_config": {"ok": True, "summary": "Left alone.", "removed": [], "left_alone": [{"what": "MCP registration", "path": "/home/op/.config/opencode/opencode.json :: mcp.agentic-hil", "reason": "Agentic HIL did not write this, so it stays."}]},
                "skill": {"ok": False, "error_type": "removal_failed", "summary": "The skill file could not be removed.", "removed": [], "left_alone": [], "failed": [{"what": "skill registration", "path": "/home/op/.config/opencode/skills/agentic-hil/SKILL.md", "error": "[Errno 13] Permission denied"}]},
                "agent_write_restriction": {"ok": True, "summary": "Nothing to take back.", "removed": [], "left_alone": []},
            },
        },
    ],
    "removed": [
        {"what": "MCP registration", "path": "/home/op/.claude.json :: mcpServers.agentic-hil"},
        {"what": "skill registration", "path": "/home/op/.claude/skills/agentic-hil/SKILL.md"},
    ],
    "left_alone": [{"what": "MCP registration", "path": "/home/op/.config/opencode/opencode.json :: mcp.agentic-hil", "reason": "Agentic HIL did not write this, so it stays."}],
    "failed": [{"what": "skill registration", "path": "/home/op/.config/opencode/skills/agentic-hil/SKILL.md", "error": "[Errno 13] Permission denied"}],
    "kept": [
        {
            "what": "project configurations",
            "path": "/home/op/.config/agentic-hil/projects",
            "count": 2,
            "reason": "A project configuration is operator policy, and its permissions only ever narrow. Delete it yourself if that is what you want.",
        }
    ],
    "package_removal": {"manager": "uv", "command": "uv tool uninstall agentic-hil", "package_directory": "/home/op/.local/share/uv/tools/agentic-hil/lib/python3.12/site-packages/agentic_hil"},
    "next_step": "Run `uv tool uninstall agentic-hil` at your shell; nothing running out of the installation can remove it. `kept` names the trees this command deliberately did not touch and why.",
}


def test_uninstall_renders_every_section_from_a_recorded_result() -> None:
    out = render_result(UNINSTALL_PARTLY, "uninstall")
    flat = _reflowed(out)

    assert "Removed" in out
    for entry in UNINSTALL_PARTLY["removed"]:
        assert entry["path"] in flat
    assert "Left alone, because this installation did not write it" in out
    assert "/home/op/.config/opencode/opencode.json :: mcp.agentic-hil" in flat
    assert "Agentic HIL did not write this, so it stays." in flat
    assert "Kept on purpose" in out
    assert "/home/op/.config/agentic-hil/projects" in flat
    assert "Not fully taken back" in out
    assert "opencode" in out[out.index("Not fully taken back") :]
    assert "uv tool uninstall agentic-hil" in flat
    assert "Next step" in out
