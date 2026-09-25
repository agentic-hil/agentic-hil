"""A pyserial that will not import says what the import raised (#568).

`list_available_com_ports` and `com_session_start` answer a failed pyserial
import with `serial_backend_not_available` and the sentence "pyserial is not
installed or could not be imported.", and they dropped the error itself. A
missing package, a module blocked or broken, and a pyserial that fails inside
its own imports all read the same, and no screen could say which module was
missing or what the import raised. Both results now carry the import error's
own line in `backend_error`, with its type name, the way `com_port_open_failed`
carries the line the OS gave, and every screen that prints the failure shows it.

Each failure is produced for real, through the import system: pyserial hidden
from every finder, `serial` blocked in `sys.modules`, and a stand-in `serial`
package whose own code imports a module no host has. Every change to
`sys.modules`, `sys.meta_path` and `sys.path` goes back through `monkeypatch`,
so the next test imports the real pyserial.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_OPENOCD, write_authoritative_config, write_config
from support import trusted_launcher

from agentic_hil import cli
from agentic_hil.bootstrap import PROJECT_PROFILE
from agentic_hil.comports import COM_PORT_IDENTITY_UNVERIFIED, ComPortService, list_available_com_ports
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import load_config
from agentic_hil.humanize import render_result
from agentic_hil.types import JsonObject

# What both results keep saying, unchanged by the fix.
SUMMARY = "pyserial is not installed or could not be imported."
LIKELY_CAUSES = ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"]

# The import system's own line for a package that is not there.
MISSING = "ModuleNotFoundError: No module named 'serial'"
# The module the stand-in pyserial's own code imports, the way the real one
# imports its platform half, and the line the import system raises for it.
INNER_MODULE = "list_ports_platform_backend"
BROKEN_INSIDE = f"ModuleNotFoundError: No module named '{INNER_MODULE}'"

# The three ways the issue says read the same.
KINDS = ("missing", "blocked", "broken_inside")

# Each import as the product spells it: the listing's in
# `list_available_com_ports`, the session's in `ComPortService._open_serial`.
LISTING_IMPORT = ("serial.tools", "list_ports")
SESSION_IMPORT = ("serial",)

PORT_ID = "pyserial_import_uart"
DEVICE = "/dev/ttyPYSERIALIMPORT0"
DECLARED_SERIAL = "066BFF505050505050505050"

# A profile that names its board, so discovery has nothing to say to one.
STARTER_PROFILE: JsonObject = {
    "target": {"name": "nucleo-f446re-starter", "controller": "stm32f446ret6"},
    "debuggers": {"dut": {"timeout_s": 60, "permissions": {}}},
    "com_ports": {"dut_uart": {"baudrate": 115200, "permissions": {}}},
}


# ---------------------------------------------------------------------------
# Breaking pyserial, for real.


def _forget_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take pyserial out of `sys.modules`, so the next import of it is a real one.

    Every name is registered with `monkeypatch` before it is removed, including
    the ones nothing has imported yet: whatever a test leaves under these names
    is taken out again at the end, and the real modules, where there were any,
    are put back."""
    names = {"serial", "serial.tools", "serial.tools.list_ports"}
    names |= {name for name in sys.modules if name == "serial" or name.startswith("serial.")}
    for name in sorted(names):
        # `setitem` records whether the name was there and what it held; the
        # `delitem` after it leaves the name absent for the test.
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)


class _WithoutPyserial:
    """The host's own finders, asked for everything except pyserial.

    Taking pyserial's directory off `sys.path` would hide every module installed
    beside it too, and a finder that only declined `serial` in front of the
    others would be passed over for the next one, which finds it. This stands in
    for all of them: `serial` is found nowhere and the import system raises its
    own error for it, while every other module, and every distribution
    `importlib.metadata` looks up, is answered by the finder that always did."""

    def __init__(self, finders: list[object]) -> None:
        self._finders = finders

    def find_spec(self, name: str, path: object = None, target: object = None) -> object:
        if name == "serial" or name.startswith("serial."):
            return None
        for finder in self._finders:
            find_spec = getattr(finder, "find_spec", None)
            spec = find_spec(name, path, target) if find_spec is not None else None
            if spec is not None:
                return spec
        return None

    def find_distributions(self, *args: object, **kwargs: object) -> Iterator[object]:
        for finder in self._finders:
            find_distributions = getattr(finder, "find_distributions", None)
            if find_distributions is not None:
                yield from find_distributions(*args, **kwargs)

    def invalidate_caches(self) -> None:
        for finder in self._finders:
            invalidate_caches = getattr(finder, "invalidate_caches", None)
            if invalidate_caches is not None:
                invalidate_caches()


def _stand_in_pyserial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, broken_in: str) -> None:
    """A `serial` package, first on `sys.path`, that fails inside its own imports.

    `broken_in` is the file of it that imports `INNER_MODULE`: the listing meets
    it in `tools/list_ports.py`, and the session's `import serial` meets it in
    `__init__.py`, where the real package imports its platform backend."""
    root = tmp_path / "stand-in-pyserial"
    package = root / "serial"
    (package / "tools").mkdir(parents=True)
    files = {
        "__init__.py": '"""A stand-in pyserial."""\n',
        "tools/__init__.py": "",
        "tools/list_ports.py": '"""The stand-in\'s port listing."""\n',
    }
    files[broken_in] += f"import {INNER_MODULE}\n"
    for relative, text in files.items():
        (package / relative).write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))


def _raised_importing(module: str, *names: str) -> str:
    """The line the import system raises for `from module import names`, or for
    `import module` without names, on the host as the test has left it."""
    try:
        __import__(module, fromlist=names)
    except ImportError as error:
        return f"{type(error).__name__}: {error}"
    raise AssertionError(f"{module} imported on a host that was set up so it could not")


def _break_pyserial(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, statement: tuple[str, ...]) -> str:
    """Leave pyserial unimportable the way `kind` names, and return the line the
    import system raises for `statement`, which is what `backend_error` has to say.

    `missing` and `broken_inside` raise lines the decided behaviour names, so
    the host is checked for them before the product is asked anything; `blocked`
    raises whatever the import system raises, and that is the expectation."""
    _forget_serial(monkeypatch)
    if kind == "missing":
        monkeypatch.setattr(sys, "meta_path", [_WithoutPyserial(list(sys.meta_path))])
    elif kind == "blocked":
        monkeypatch.setitem(sys.modules, "serial", None)
    else:
        _stand_in_pyserial(
            tmp_path, monkeypatch, broken_in="__init__.py" if statement == SESSION_IMPORT else "tools/list_ports.py"
        )
    raised = _raised_importing(*statement)
    if kind == "missing":
        assert raised == MISSING, raised
    elif kind == "broken_inside":
        assert raised == BROKEN_INSIDE, raised
    return raised


# ---------------------------------------------------------------------------
# The configurations and hosts the results are read on.


def _com_config(workspace: Path, **entry: str):
    """One configured port, declaring no hardware unless `entry` names some."""
    lines = "".join(f'    {key}: "{value}"\n' for key, value in {"device": DEVICE, **entry}.items())
    return load_config(str(write_config(workspace, com_ports_yaml=f"com_ports:\n  {PORT_ID}:\n{lines}")))


def _session_start(config) -> JsonObject:
    service = ComPortService(config)
    try:
        return service.session_start(PORT_ID)
    finally:
        service.close()


def _openocd_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with OpenOCD and no STM32CubeProgrammer, which the suite already
    hides: bootstrap then enumerates probes from the serial inventory. Nothing
    is spawned on the way there, because a listing that failed names no probe."""

    def nothing_spawned(command: list[str], cwd: str, timeout_s: float) -> object:
        raise AssertionError(f"nothing should have been spawned: {command}")

    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: str(FAKE_OPENOCD))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", nothing_spawned)


def _starter_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace


def _setup_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user-wide half of `setup`, installed into the sandboxed profile."""
    command = str(trusted_launcher())
    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", lambda: command)
    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", list)
    real_which = shutil.which
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: None if name == "claude" else real_which(name))


def _flat(text: str) -> str:
    """The text with the wrapper's line breaks taken back out."""
    return " ".join(text.split())


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = cli.entrypoint(list(argv))
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# The two results.


@pytest.mark.parametrize("kind", KINDS)
def test_the_listing_carries_the_import_error(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`list_available_com_ports` keeps its error type, summary and causes, and
    adds the import error's own line, with its type name."""
    raised = _break_pyserial(kind, tmp_path, monkeypatch, statement=LISTING_IMPORT)

    result = list_available_com_ports()

    assert result == {
        "ok": False,
        "tool": "com_ports_available",
        "error_type": "serial_backend_not_available",
        "summary": SUMMARY,
        "likely_causes": LIKELY_CAUSES,
        "backend_error": raised,
    }


@pytest.mark.parametrize("kind", KINDS)
def test_the_session_carries_the_import_error(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`com_session_start` on a port that declares no hardware reaches its own
    `import serial`, and answers with that import's line beside the same error
    type, summary and causes. Nothing was opened."""
    config = _com_config(tmp_path / "workspace")
    raised = _break_pyserial(kind, tmp_path, monkeypatch, statement=SESSION_IMPORT)

    result = _session_start(config)

    assert result["ok"] is False, result
    assert result["error_type"] == "serial_backend_not_available", result
    assert result["summary"] == SUMMARY, result
    assert result["likely_causes"] == LIKELY_CAUSES, result
    assert result["side_effect_committed"] is False, result
    assert result.get("backend_error") == raised, result


def test_the_three_failures_read_differently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's complaint, whole: pyserial missing, blocked, and failing
    inside its own imports read the same. On both results they now read as
    three different lines, and the two the decided behaviour names are those
    lines exactly: `No module named 'serial'`, and the inner module's name."""
    config = _com_config(tmp_path / "workspace")
    listed: dict[str, object] = {}
    started: dict[str, object] = {}
    for kind in KINDS:
        with monkeypatch.context() as patch:
            _break_pyserial(kind, tmp_path / kind / "listing", patch, statement=LISTING_IMPORT)
            listed[kind] = list_available_com_ports().get("backend_error")
        with monkeypatch.context() as patch:
            _break_pyserial(kind, tmp_path / kind / "session", patch, statement=SESSION_IMPORT)
            started[kind] = _session_start(config).get("backend_error")

    for answers in (listed, started):
        assert answers["missing"] == MISSING, answers
        assert answers["broken_inside"] == BROKEN_INSIDE, answers
        assert len(set(answers.values())) == 3, answers


def test_a_declared_port_refused_for_its_identity_names_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port that declares its board is proved against the host inventory
    before it is opened, and with pyserial unimportable that proof cannot run.
    The refusal's identity block said so with the inventory's summary under
    `backend_error`; it carries the inventory's `backend_error` now."""
    config = _com_config(tmp_path / "workspace", serial_number=DECLARED_SERIAL)
    raised = _break_pyserial("broken_inside", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    result = _session_start(config)

    assert result["error_type"] == COM_PORT_IDENTITY_UNVERIFIED, result
    assert result["identity"]["status"] == "backend_unavailable", result
    assert result["identity"].get("backend_error") == raised, result


def test_a_declared_port_refused_for_an_inventory_os_error_names_that_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity block carries the inventory's own `backend_error` whenever
    the inventory has one, not only for an import that failed: pyserial imported
    and its port enumeration raised an OS error, which the inventory reports as
    `com_port_discovery_failed` with the error's line."""
    config = _com_config(tmp_path / "workspace", serial_number=DECLARED_SERIAL)

    def enumeration_raises() -> list[object]:
        raise OSError("the port enumeration's own error line")

    monkeypatch.setattr("serial.tools.list_ports.comports", enumeration_raises)
    inventory = list_available_com_ports()
    assert inventory["error_type"] == "com_port_discovery_failed", inventory
    assert inventory["backend_error"] == "the port enumeration's own error line", inventory

    result = _session_start(config)

    assert result["error_type"] == COM_PORT_IDENTITY_UNVERIFIED, result
    assert result["identity"]["status"] == "backend_unavailable", result
    assert result["identity"].get("backend_error") == inventory["backend_error"], result


def test_the_configured_port_listing_nests_the_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`com_ports_list` carries the host inventory whole, failure included."""
    config = _com_config(tmp_path / "workspace")
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    service = ComPortService(config)
    try:
        result = service.list_ports()
    finally:
        service.close()

    available = result["available_com_ports"]
    assert available["error_type"] == "serial_backend_not_available", available
    assert available.get("backend_error") == raised, available


# ---------------------------------------------------------------------------
# The screens that print the failure.


def test_com_ports_prints_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agentic-hil com-ports` is the listing itself."""
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    code, out = _cli(capsys, "com-ports", "--json")
    assert code == 1
    assert json.loads(out).get("backend_error") == raised, out

    code, out = _cli(capsys, "com-ports")
    assert code == 1
    assert raised in _flat(out), out


def test_com_stdio_prints_the_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentic-hil com-stdio` writes a start that failed to stderr as the result document."""
    config = _com_config(tmp_path / "workspace")
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=SESSION_IMPORT)
    errors = io.StringIO()

    code = run_com_stdio(config, PORT_ID, input_stream=io.BytesIO(), output_stream=io.StringIO(), error_stream=errors)

    assert code == 1
    written = json.loads(errors.getvalue())
    assert written["error_type"] == "serial_backend_not_available", written
    assert written.get("backend_error") == raised, written


@pytest.mark.parametrize("command", ["init", "setup"])
def test_init_and_setup_print_the_import_error(command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both commands read the host inventory twice over: once for the COM-port
    advice and once as the probe enumeration on a host with no
    STM32CubeProgrammer. The document carries the import error in both places,
    and the screen shows it."""
    _starter_workspace(tmp_path, monkeypatch)
    _openocd_host(monkeypatch)
    if command == "setup":
        _setup_harness(monkeypatch)
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    result = cli.init_project() if command == "init" else cli.setup_project(agent="claude-code")

    config_step = result["steps"]["config"]
    assert config_step["available_com_ports"].get("backend_error") == raised, config_step["available_com_ports"]
    discovery = config_step["hardware_discovery"]
    assert discovery["error_type"] == "probe_discovery_failed", discovery
    assert discovery.get("backend_error") == raised, discovery
    assert raised in _flat(render_result(result, command))


def test_debugger_probes_with_no_configuration_prints_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before the first `setup`, `debugger-probes` answers with bootstrap's
    enumeration, which on this host is the serial inventory."""
    workspace = tmp_path / "bare"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _openocd_host(monkeypatch)
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    code, out = _cli(capsys, "debugger-probes", "--json")
    assert code == 1
    document = json.loads(out)
    assert document["error_type"] == "probe_discovery_failed", document
    assert document.get("backend_error") == raised, document

    code, out = _cli(capsys, "debugger-probes")
    assert code == 1
    assert raised in _flat(out), out


def test_debugger_probes_on_a_configured_openocd_prints_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bound OpenOCD entry enumerates its ST-Link from the same inventory."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace, monkeypatch, interface_cfg="interface/stlink.cfg", target_cfg="target/stm32f4x.cfg"
    )
    monkeypatch.chdir(workspace)
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    code, out = _cli(capsys, "debugger-probes", "--json")
    assert code == 1
    document = json.loads(out)
    assert document["error_type"] == "probe_discovery_failed", document
    assert document.get("backend_error") == raised, document

    code, out = _cli(capsys, "debugger-probes")
    assert code == 1
    assert raised in _flat(out), out


def test_debugger_probes_across_several_debuggers_prints_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With more than one debugger configured the answer is one listing per
    entry under `debuggers`, and each entry's failure is printed under its name."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_name="probe_a",
        probe_id="PROBE-A",
        interface_cfg="interface/stlink.cfg",
        target_cfg="target/stm32f4x.cfg",
        debuggers_yaml=(
            'debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
            '    interface_cfg: "interface/stlink.cfg"\n    target_cfg: "target/stm32f4x.cfg"\n'
        ),
    )
    monkeypatch.chdir(workspace)
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)

    code, out = _cli(capsys, "debugger-probes", "--json")
    assert code == 1
    listings = json.loads(out)["debuggers"]
    assert sorted(listings) == ["probe_a", "probe_b"], listings
    for name, listing in listings.items():
        assert listing["error_type"] == "probe_discovery_failed", (name, listing)
        assert listing.get("backend_error") == raised, (name, listing)

    code, out = _cli(capsys, "debugger-probes")
    assert code == 1
    assert raised in _flat(out), out


def test_adopt_hardware_prints_the_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`adopt-hardware` runs the same discovery `init` does, and answers its
    failure with discovery's own document."""
    _starter_workspace(tmp_path, monkeypatch)
    _openocd_host(monkeypatch)
    raised = _break_pyserial("missing", tmp_path, monkeypatch, statement=LISTING_IMPORT)
    assert cli.init_config()["ok"] is True

    result = cli.adopt_hardware()

    assert result["ok"] is False, result
    assert result["error_type"] == "probe_discovery_failed", result
    assert result.get("backend_error") == raised, result
    assert raised in _flat(render_result(result, "adopt-hardware"))
