"""A configuration a generation writes can flash the bench it describes.

The rule this file exists for is not in either of the two rules it is made of.
A generation opens every permission a configuration declares. The
flash interlock refuses `flash_firmware` while `allow_raw_debugger_commands` or
`allow_mass_erase` is true on the probe. Each was tested on its own and each
passed on its own, and together they meant that a freshly generated bench could
not flash at all — the interlock was written when the pair defaulted to false, so
"both on" was a state somebody had chosen, and the inversion made it the shipped
state without either rule being edited.

So the assertions here are deliberately *crossings*: a generation on one side, the
interlock's own constant on the other. A test that only read the template would
have missed it too, because the template is not where a bench with a board
attached gets its permissions — `bootstrap` and `grant_every_permission` each
write them again, and each used to decide the default for itself.

What settles which side gives way, and why this is not a narrowing: neither flag
grants anything. There is no MCP tool for raw debugger commands and none for mass
erase, so every read of either in `src/` is a deny site. Turning one on subtracts
flashing and adds no capability in exchange, which is why they are the exception
to allow-by-default rather than the interlock being the thing that moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agentic_hil.bootstrap import DEFAULT_PROJECT_PROFILE, apply_discovery_to_template
from agentic_hil.cli import init_config
from agentic_hil.config import (
    DEFAULT_CONFIG_TEMPLATE,
    GENERATED_WRITE_PERMISSIONS,
    generated_permissions,
    grant_every_permission,
    load_authoritative_config,
)
from agentic_hil.configwrite import deny_all_permissions
from agentic_hil.knowledge import EXCLUSIVE_FLASH_PERMISSIONS

BACKENDS = Path(__file__).resolve().parent.parent / "src" / "agentic_hil" / "backends"

# Enough of a discovery result for the path that fills the skeleton in from
# attached hardware. It decides no permission, which is the point: the values
# under test come from the generation, never from what was plugged in.
DISCOVERY = {
    "ok": True,
    "executable": str(Path(__file__).resolve()),
    "probe_id": "STLINK123",
    "target": {"probe_id": "STLINK123", "controller": "STM32F446RE"},
    "com_port": {"device": "COM3", "serial_number": "STLINK123"},
}


def skeleton() -> dict:
    document = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    assert isinstance(document, dict)
    return document


def from_template() -> dict:
    """The shipped skeleton, read as YAML rather than as a string."""
    return skeleton()


def from_hardware_discovery() -> dict:
    """`agentic-hil init` with a board attached — the path a real bench takes."""
    return apply_discovery_to_template(skeleton(), DEFAULT_PROJECT_PROFILE, DISCOVERY)


def from_project_config_create() -> dict:
    """The MCP generation, which rewrites every permission after the fill-in."""
    return grant_every_permission(apply_discovery_to_template(skeleton(), DEFAULT_PROJECT_PROFILE, DISCOVERY))


# Every path that decides a debugger's permissions on a *generation*. Named
# rather than collapsed into one, because the defect was that they disagreed and
# a test that exercised one of them would have gone on passing.
GENERATION_PATHS = {
    "shipped template": from_template,
    "hardware discovery": from_hardware_discovery,
    "project_config_create": from_project_config_create,
}


def debugger_permissions(document: dict) -> dict:
    entries = document["debuggers"]
    assert entries, "a generation that describes no probe proves nothing here"
    permissions = {}
    for name, entry in entries.items():
        assert isinstance(entry, dict), name
        permissions.update(entry["permissions"])
    return permissions


@pytest.mark.parametrize("path", sorted(GENERATION_PATHS))
def test_a_generated_configuration_is_not_refused_by_the_flash_interlock(path: str) -> None:
    """The crossing itself, on every path that generates.

    Read as: nothing a generation writes is a reason the interlock refuses to
    flash. Stated against `EXCLUSIVE_FLASH_PERMISSIONS` rather than against the
    two names, so that a third flag joining the interlock is covered the moment
    it is added instead of the day a bench stops flashing."""
    permissions = debugger_permissions(GENERATION_PATHS[path]())

    blocking = sorted(flag for flag in EXCLUSIVE_FLASH_PERMISSIONS if permissions.get(flag))
    assert blocking == [], f"{path} generates a bench that cannot flash, blocked by {blocking}"


@pytest.mark.parametrize("path", sorted(GENERATION_PATHS))
def test_a_generated_configuration_still_grants_what_it_can(path: str) -> None:
    """The other half, or the fix would be "write false everywhere".

    The open default is not undone by the two exceptions: everything that has a tool
    behind it is still open on a fresh file, and the exception is exactly the
    permissions that have none."""
    permissions = debugger_permissions(GENERATION_PATHS[path]())

    assert permissions["allow_flash"] is True, path
    assert permissions["allow_reset"] is True, path
    withheld = sorted(flag for flag, granted in permissions.items() if not granted)
    assert withheld == sorted(EXCLUSIVE_FLASH_PERMISSIONS), f"{path} withholds something other than the interlocked pair"


def test_every_generation_path_writes_the_same_permissions() -> None:
    """No path decides this for itself, which is how the defect got in.

    The skeleton was corrected once before against a `grant_every_permission`
    that wrote `True` unconditionally and a `bootstrap` that defaulted to `True`,
    and neither moved with it. They read `generated_permissions` now, and this is
    what says so."""
    written = {path: debugger_permissions(build()) for path, build in GENERATION_PATHS.items()}

    assert len({tuple(sorted(permissions.items())) for permissions in written.values()}) == 1, written
    assert written["shipped template"] == generated_permissions("debuggers")


def test_the_interlock_and_the_generated_default_name_the_same_permissions() -> None:
    """The two rules, held against each other at their sources.

    The backends name the flags they refuse flashing on as literals in the call
    that builds the refusal. If a flag joins that list without joining
    `EXCLUSIVE_FLASH_PERMISSIONS`, generation keeps writing it true and the bench
    stops flashing — which is the defect, exactly, and this is the assertion that
    would have caught it before it shipped."""
    refused: set[str] = set()
    for source in sorted(BACKENDS.glob("*.py")):
        refused.update(re.findall(r'exclusive_permission_summary\(\s*"Flashing",\s*"(allow_[a-z_]+)"', source.read_text(encoding="utf-8")))

    assert refused, "no backend flash gate was found; this test is not reading the interlock any more"
    assert refused == set(EXCLUSIVE_FLASH_PERMISSIONS)


def test_neither_interlocked_permission_is_a_capability_anything_grants() -> None:
    """Why the generation gave way rather than the interlock.

    Every read of these two under `src/` refuses something. None of them is a
    gate an action passes *through*, so a bench that leaves both false has given
    nothing up — if that ever stops being true, the trade this fix made needs
    revisiting and this test is where that surfaces."""
    source_root = BACKENDS.parent
    granting: list[str] = []
    for source in sorted(source_root.rglob("*.py")):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            for flag in EXCLUSIVE_FLASH_PERMISSIONS:
                # A read that gates an action on the flag being *true* would look
                # like `if not permissions.<flag>` or `require(... <flag>)`. The
                # deny sites all read `if permissions.<flag>:` — the flag being
                # true is the refusal, never the permission.
                if re.search(rf"not\s+\w+(?:\.\w+)*\.{flag}\b", code) or re.search(rf"require\w*\([^)]*\b{flag}\b", code):
                    granting.append(f"{source.relative_to(source_root)}:{number}: {line.strip()}")

    assert granting == [], "something now requires one of the interlocked permissions to be true: " + "; ".join(granting)


def test_an_entry_an_agent_creates_still_arrives_closed() -> None:
    """Generation is not the write path, and this fix did not touch the write path.

    A `project_config_set` that creates an entry still gives it nothing, so the
    only way either interlocked flag becomes true is a person editing YAML."""
    for section in GENERATED_WRITE_PERMISSIONS:
        assert deny_all_permissions(section) == dict.fromkeys(GENERATED_WRITE_PERMISSIONS[section], False)


def test_the_configuration_init_writes_loads_as_one_that_can_flash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, through the real writer and the real parser.

    The paths above are the functions; this is the file. `init_config` writes it,
    `load_authoritative_config` parses it under the authoritative rules, and the
    permissions the interlock will read at flash time are read off that object
    rather than off a document a test built."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = init_config()
    assert result["ok"] is True, result

    config = load_authoritative_config(workspace.resolve())
    permissions = config.debugger.permissions

    assert permissions.allow_flash is True
    assert [flag for flag in EXCLUSIVE_FLASH_PERMISSIONS if getattr(permissions, flag)] == []
