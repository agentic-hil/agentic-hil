"""Every profile this repository ships must generate a configuration the loader accepts.

The Nucleo demo's ``agentic-hil.config.example.yaml`` named its two OpenOCD
scripts as ``/absolute/path/to/openocd/scripts/...``, a placeholder from the
days when ``init`` did not carry the profile's script names into the generated
file. Since it does, ``agentic-hil init`` inside the demo directory on a host
with OpenOCD and an attached board wrote those placeholders into the
configuration and then refused its own file: ``config_invalid`` on
``debuggers.dut.interface_cfg``, because a path spelling is a claim about a file
on this host and no such file exists. The STM32 starter's copy of the profile
uses the search names ``interface/stlink.cfg`` and ``target/stm32f4x.cfg``,
which OpenOCD resolves against its own script tree, and its ``init`` succeeded
on the same host (#427).

These tests hold every shipped profile to the loader's own rule for the two
fields, and hold the configuration generated from the profile through the
OpenOCD discovery path to the same rule, so a placeholder cannot ship again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentic_hil.bootstrap import apply_discovery_to_template
from agentic_hil.config import DEFAULT_CONFIG_TEMPLATE, OPENOCD_SCRIPT_SEARCH_NAME, openocd_script_kind

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_FIELDS = ("interface_cfg", "target_cfg")

# A bench as the USB enumeration answers it on a host with OpenOCD and no
# STM32CubeProgrammer: the shape ``discover_attached_hardware`` returns there.
OPENOCD_BENCH = {
    "ok": True,
    "backend": "openocd",
    "executable": str(Path(__file__).resolve()),
    "gdb_executable": None,
    "probe_id": "066AFF303435554157113106",
    "target": {"probe_id": "066AFF303435554157113106", "controller": "stm32f446ret6"},
    "com_port": {
        "device": "/dev/ttyACM0",
        "stable_device": "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066AFF303435554157113106-if02",
        "serial_number": "066AFF303435554157113106",
        "vid": 0x0483,
        "pid": 0x374B,
    },
}


def _shipped_profiles() -> list[Path]:
    profiles = sorted((REPOSITORY_ROOT / "examples").glob("*/agentic-hil.config.example.yaml"))
    assert profiles, "no shipped profile found under examples/"
    return profiles


def _openocd_entries(document: dict) -> list[tuple[str, dict]]:
    debuggers = document.get("debuggers")
    assert isinstance(debuggers, dict), "the profile declares no debuggers"
    entries = [(name, entry) for name, entry in debuggers.items() if isinstance(entry, dict) and entry.get("type", "openocd") == "openocd"]
    assert entries, "the profile declares no openocd debugger"
    return entries


@pytest.mark.parametrize("profile_path", _shipped_profiles(), ids=lambda path: path.parent.name)
def test_a_shipped_profile_names_openocd_scripts_by_search_name(profile_path: Path) -> None:
    """The loader accepts a search name as written and holds a path to a file on
    this host, so a profile that is copied to any host may only carry the former."""
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    for name, entry in _openocd_entries(profile):
        for field in SCRIPT_FIELDS:
            value = entry.get(field)
            assert isinstance(value, str) and value, f"{profile_path}: debuggers.{name}.{field} is not set"
            assert openocd_script_kind(value) == OPENOCD_SCRIPT_SEARCH_NAME, (
                f"{profile_path}: debuggers.{name}.{field} is {value!r}, a path on some host rather than a search name OpenOCD resolves itself"
            )


@pytest.mark.parametrize("profile_path", _shipped_profiles(), ids=lambda path: path.parent.name)
def test_the_configuration_generated_from_a_shipped_profile_keeps_search_names(profile_path: Path) -> None:
    """What ``init`` writes on an OpenOCD host carries the profile's script names
    unchanged, so the generated file is accepted for the same reason the profile is."""
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    generated = apply_discovery_to_template(template, profile, OPENOCD_BENCH)
    for name, entry in _openocd_entries(generated):
        assert entry.get("type") == "openocd"
        for field in SCRIPT_FIELDS:
            assert openocd_script_kind(str(entry.get(field))) == OPENOCD_SCRIPT_SEARCH_NAME, (
                f"{profile_path}: generated debuggers.{name}.{field} is {entry.get(field)!r}"
            )
